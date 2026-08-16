"""Permission-gated document creation and discovery tools."""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.render.markdown import markdown_to_html
from app.security.paths import resolve_read_path, resolve_write_path
from app.tools.registry import RiskLevel, ToolExecutionError, ToolGroup, registry

log = logging.getLogger(__name__)


# Spec §14: never surface build output or vendored dependencies.
_IGNORED_DIRS = frozenset({
    "node_modules", ".git", "dist", "build", ".next", "venv", ".venv",
    "__pycache__", ".pytest_cache", ".gradle", "Pods", "target",
})

DOCUMENT_SUFFIXES = {".md", ".txt", ".html", ".docx", ".pdf"}


class CreateDocumentArgs(BaseModel):
    path: str = Field(description="Target document path, absolute or relative to the current directory.")
    content: str = Field(description="Markdown source content for the document.")
    format: Literal["md", "txt", "html", "docx", "pdf"] = Field(default="md", description="Output document format.")
    overwrite: bool = Field(default=False, description="Replace the target if it already exists.")


class ReadDocumentArgs(BaseModel):
    path: str = Field(description="Path to an existing Markdown, text, or HTML document.")
    max_bytes: int = Field(default=200_000, ge=1, le=1_000_000, description="Maximum number of bytes to read.")


class ListDocumentsArgs(BaseModel):
    directory: str = Field(description="Directory containing documents to list.")
    recursive: bool = Field(default=False, description="Include documents in nested directories.")


class AppendToDocumentArgs(BaseModel):
    path: str = Field(description="Path to an existing Markdown or text document.")
    content: str = Field(description="Markdown or text content to append.")


def _target_path(path: str, output_format: str) -> Path:
    raw = Path(path)
    if output_format != "md" and raw.suffix.lower() in {"", ".md"}:
        raw = raw.with_suffix(f".{output_format}")
    return resolve_write_path(raw)


def _run_conversion(argv: list[str], *, operation: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(argv, capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ToolExecutionError(f"{operation} failed: {exc}") from exc


def _conversion_error(operation: str, result: subprocess.CompletedProcess[bytes]) -> ToolExecutionError:
    detail = result.stderr.decode("utf-8", errors="replace").strip()
    return ToolExecutionError(f"{operation} failed{f': {detail}' if detail else ''}")


_CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
)


def _find_chrome() -> str | None:
    for path in _CHROME_CANDIDATES:
        if Path(path).is_file():
            return path
    return None


def _html_to_pdf(source: Path, target: Path, markdown_source: str) -> None:
    """Render HTML to PDF using whatever this Mac actually provides.

    macOS 15 removed both the HTML->PDF and RTF->PDF filters from `cupsfilter`
    (it reports "No filter to convert from text/html to application/pdf"), and
    /System/Library/Printers/Libraries/convert is gone too. Only text/plain
    still converts.

    So: a Chromium-family browser is the high-fidelity path — it renders the
    same CSS we generate, giving real tables, code blocks and headings. When no
    browser is installed we fall back to cupsfilter over the plain Markdown,
    which always works but produces an unstyled text PDF. Both cost nothing on
    disk, which is the point.
    """
    chrome = _find_chrome()
    if chrome is not None:
        result = _run_conversion(
            [
                chrome,
                "--headless",
                "--disable-gpu",
                "--no-sandbox",
                "--no-pdf-header-footer",
                f"--print-to-pdf={target}",
                source.as_uri(),
            ],
            operation="PDF conversion",
        )
        if result.returncode == 0 and target.exists() and target.stat().st_size > 0:
            return
        log.warning("headless browser PDF failed, falling back to plain text: %s", result.stderr[:200])

    # Fallback: cupsfilter still handles text/plain. Feed it the Markdown
    # rather than the HTML so the output reads as prose, not as tag soup.
    with tempfile.TemporaryDirectory() as plain_dir:
        plain = Path(plain_dir) / "document.txt"
        plain.write_text(markdown_source, encoding="utf-8")
        cupsfilter = shutil.which("cupsfilter") or "/usr/sbin/cupsfilter"
        result = _run_conversion([cupsfilter, str(plain)], operation="PDF conversion")
        if result.returncode != 0 or not result.stdout:
            raise _conversion_error("PDF conversion", result)
        target.write_bytes(result.stdout)


@registry.tool(
    name="create_document",
    description="Create a Markdown, text, HTML, Word, or PDF document from Markdown source.",
    args_model=CreateDocumentArgs,
    risk=RiskLevel.LOW_RISK_WRITE,
    group=ToolGroup.DOCUMENTS,
)
def create_document(args: CreateDocumentArgs) -> dict:
    target = _target_path(args.path, args.format)
    if target.exists() and not args.overwrite:
        raise ToolExecutionError(f"document already exists: {target}; set overwrite=true to replace it")
    target.parent.mkdir(parents=True, exist_ok=True)

    if args.format in {"md", "txt"}:
        target.write_text(args.content, encoding="utf-8")
    elif args.format == "html":
        target.write_text(markdown_to_html(args.content, title=target.stem), encoding="utf-8")
    else:
        rendered = markdown_to_html(args.content, title=target.stem)
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "document.html"
            source.write_text(rendered, encoding="utf-8")
            if args.format == "docx":
                result = _run_conversion(
                    ["textutil", "-convert", "docx", "-output", str(target), str(source)],
                    operation="DOCX conversion",
                )
                if result.returncode != 0 or not target.exists() or target.stat().st_size == 0:
                    raise _conversion_error("DOCX conversion", result)
            else:
                _html_to_pdf(source, target, args.content)

    return {"path": str(target), "format": args.format, "bytes": target.stat().st_size, "created": True}


@registry.tool(
    name="read_document",
    description="Read a Markdown, text, or HTML document, with a configurable size cap.",
    args_model=ReadDocumentArgs,
    risk=RiskLevel.READ_ONLY,
    group=ToolGroup.DOCUMENTS,
)
def read_document(args: ReadDocumentArgs) -> dict:
    target = resolve_read_path(args.path)
    if target.suffix.lower() not in {".md", ".txt", ".html"}:
        raise ToolExecutionError("read_document supports only .md, .txt, and .html files")
    try:
        data = target.read_bytes()
        content = data[: args.max_bytes].decode("utf-8", errors="replace")
    except OSError as exc:
        raise ToolExecutionError(f"could not read document {target}: {exc}") from exc
    return {"path": str(target), "content": content, "bytes": len(data), "truncated": len(data) > args.max_bytes}


@registry.tool(
    name="list_documents",
    description="List document-like files in a directory, optionally recursively.",
    args_model=ListDocumentsArgs,
    risk=RiskLevel.READ_ONLY,
    group=ToolGroup.DOCUMENTS,
)
def list_documents(args: ListDocumentsArgs) -> dict:
    directory = resolve_read_path(args.directory)
    if not directory.is_dir():
        raise ToolExecutionError(f"not a directory: {directory}")
    iterator = directory.rglob("*") if args.recursive else directory.iterdir()
    documents: list[dict] = []
    try:
        for path in iterator:
            if not path.is_file() or path.suffix.lower() not in DOCUMENT_SUFFIXES:
                continue
            # Spec §14's ignore list. Without it a recursive listing of any JS
            # project is dominated by node_modules READMEs, which is both
            # useless to the user and expensive to feed back to the model.
            if any(part in _IGNORED_DIRS for part in path.parts):
                continue
            stat = path.stat()
            documents.append(
                {"name": path.name, "path": str(path), "size": stat.st_size, "mtime": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat()}
            )
            if len(documents) == 200:
                break
    except OSError as exc:
        raise ToolExecutionError(f"could not list documents in {directory}: {exc}") from exc
    documents.sort(key=lambda item: item["path"])
    return {"directory": str(directory), "documents": documents, "count": len(documents), "truncated": len(documents) == 200}


@registry.tool(
    name="append_to_document",
    description="Append Markdown or text to an existing .md or .txt document.",
    args_model=AppendToDocumentArgs,
    risk=RiskLevel.LOW_RISK_WRITE,
    group=ToolGroup.DOCUMENTS,
)
def append_to_document(args: AppendToDocumentArgs) -> dict:
    target = resolve_write_path(args.path, must_exist=True)
    if target.suffix.lower() not in {".md", ".txt"}:
        raise ToolExecutionError("append_to_document supports only .md and .txt files")
    try:
        with target.open("a", encoding="utf-8") as handle:
            handle.write(args.content)
    except OSError as exc:
        raise ToolExecutionError(f"could not append to document {target}: {exc}") from exc
    return {"path": str(target), "bytes": target.stat().st_size, "appended": True}
