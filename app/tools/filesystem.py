"""Filesystem tools (spec §17). Every path goes through `app.security.paths`.

Handlers never construct their own containment or sensitivity checks — that
logic lives exactly once, in the sandbox module, so it can't drift per tool.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from app.config import load_config
from app.security.paths import is_sensitive, resolve_read_path, resolve_write_path
from app.tools.registry import RiskLevel, ToolExecutionError, ToolGroup, registry

log = logging.getLogger(__name__)

# Directories we never want to enumerate or crawl into — build artifacts and
# dependency trees that are huge, irrelevant, and never what the user means.
_SKIP_DIR_NAMES = frozenset(
    {"node_modules", ".git", "__pycache__", ".next", "dist", "build", "venv", ".venv"}
)

_MAX_LIST_ENTRIES = 300
_DEFAULT_MAX_BYTES = 100_000


# -- read_file ----------------------------------------------------------


class ReadFileArgs(BaseModel):
    path: str = Field(description="Absolute or ~-relative path to the file to read.")
    max_bytes: int = Field(
        default=_DEFAULT_MAX_BYTES,
        ge=1,
        le=2_000_000,
        description="Maximum bytes to return before truncating.",
    )


@registry.tool(
    name="read_file",
    description="Read a UTF-8 text file from an allowed directory. Refuses binary files.",
    args_model=ReadFileArgs,
    risk=RiskLevel.READ_ONLY,
    group=ToolGroup.FILESYSTEM,
)
def read_file(args: ReadFileArgs) -> dict:
    resolved = resolve_read_path(args.path)
    if resolved.is_dir():
        raise ToolExecutionError(f"'{resolved}' is a directory, not a file")

    with resolved.open("rb") as fh:
        sniff = fh.read(8192)
    if b"\x00" in sniff:
        raise ToolExecutionError(
            f"'{resolved.name}' looks like a binary file; read_file only supports text"
        )

    raw = resolved.read_bytes()
    truncated = len(raw) > args.max_bytes
    content = raw[: args.max_bytes].decode("utf-8", errors="replace")

    return {
        "path": str(resolved),
        "content": content,
        "truncated": truncated,
        "size_bytes": len(raw),
    }


# -- list_directory -------------------------------------------------------


class ListDirectoryArgs(BaseModel):
    path: str = Field(description="Directory to list.")


@registry.tool(
    name="list_directory",
    description="List entries in a directory (name, type, size, mtime). Skips build/dependency clutter.",
    args_model=ListDirectoryArgs,
    risk=RiskLevel.READ_ONLY,
    group=ToolGroup.FILESYSTEM,
)
def list_directory(args: ListDirectoryArgs) -> dict:
    resolved = resolve_read_path(args.path)
    if not resolved.is_dir():
        raise ToolExecutionError(f"'{resolved}' is not a directory")

    entries: list[dict] = []
    truncated = False
    children = sorted(resolved.iterdir(), key=lambda p: p.name)
    for child in children:
        if child.name in _SKIP_DIR_NAMES:
            continue
        if len(entries) >= _MAX_LIST_ENTRIES:
            truncated = True
            break
        try:
            stat = child.stat()
        except OSError:
            continue
        entries.append(
            {
                "name": child.name,
                "type": "directory" if child.is_dir() else "file",
                "size": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            }
        )

    return {"path": str(resolved), "entries": entries, "truncated": truncated}


# -- search_files -----------------------------------------------------------


class SearchFilesArgs(BaseModel):
    path: str = Field(description="Root directory to search under.")
    pattern: str = Field(default="*", description="Glob pattern matched against file names, e.g. '*.py'.")
    content: str | None = Field(
        default=None, description="Optional substring the file's text content must contain."
    )
    max_results: int = Field(default=100, ge=1, le=500, description="Maximum matches to return.")
    max_depth: int = Field(default=6, ge=1, le=20, description="Maximum directory depth to traverse.")


@registry.tool(
    name="search_files",
    description="Search for files under a directory by name glob and optional content substring.",
    args_model=SearchFilesArgs,
    risk=RiskLevel.READ_ONLY,
    group=ToolGroup.FILESYSTEM,
)
def search_files(args: SearchFilesArgs) -> dict:
    root = resolve_read_path(args.path)
    if not root.is_dir():
        raise ToolExecutionError(f"'{root}' is not a directory")

    matches: list[str] = []
    truncated = False
    root_depth = len(root.parts)

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES]

        depth = len(Path(dirpath).parts) - root_depth
        if depth >= args.max_depth:
            dirnames[:] = []  # don't descend further, but still scan this level

        for filename in fnmatch.filter(filenames, args.pattern):
            candidate = Path(dirpath) / filename
            if is_sensitive(candidate):
                continue

            if args.content is not None:
                try:
                    text = candidate.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if args.content not in text:
                    continue

            matches.append(str(candidate))
            if len(matches) >= args.max_results:
                truncated = True
                break

        if truncated:
            break

    return {"root": str(root), "matches": matches, "truncated": truncated}


# -- write_file ---------------------------------------------------------


class WriteFileArgs(BaseModel):
    path: str = Field(description="Absolute or ~-relative path to write.")
    content: str = Field(description="Text content to write, UTF-8 encoded.")
    overwrite: bool = Field(default=False, description="Must be true to replace an existing file.")


@registry.tool(
    name="write_file",
    description="Write a UTF-8 text file within an allowed directory. Refuses to overwrite unless requested.",
    args_model=WriteFileArgs,
    risk=RiskLevel.LOW_RISK_WRITE,
    group=ToolGroup.FILESYSTEM,
)
def write_file(args: WriteFileArgs) -> dict:
    resolved = resolve_write_path(args.path)

    if resolved.exists():
        if resolved.is_dir():
            raise ToolExecutionError(f"'{resolved}' is a directory, not a file")
        if not args.overwrite:
            raise ToolExecutionError(f"'{resolved}' already exists; pass overwrite=true to replace it")

    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(args.content, encoding="utf-8")

    return {"path": str(resolved), "bytes_written": len(args.content.encode("utf-8"))}


# -- create_directory ---------------------------------------------------


class CreateDirectoryArgs(BaseModel):
    path: str = Field(description="Directory path to create.")
    parents: bool = Field(default=True, description="Create missing parent directories as needed.")


@registry.tool(
    name="create_directory",
    description="Create a directory within an allowed directory.",
    args_model=CreateDirectoryArgs,
    risk=RiskLevel.LOW_RISK_WRITE,
    group=ToolGroup.FILESYSTEM,
)
def create_directory(args: CreateDirectoryArgs) -> dict:
    resolved = resolve_write_path(args.path)

    if resolved.exists() and not resolved.is_dir():
        raise ToolExecutionError(f"'{resolved}' already exists and is not a directory")

    resolved.mkdir(parents=args.parents, exist_ok=True)
    return {"path": str(resolved), "created": True}


# -- move_file ------------------------------------------------------------


class MoveFileArgs(BaseModel):
    source: str = Field(description="Existing file or directory to move.")
    destination: str = Field(description="Destination path.")
    overwrite: bool = Field(default=False, description="Must be true to replace an existing destination.")


@registry.tool(
    name="move_file",
    description="Move or rename a file/directory. Both source and destination must be within allowed directories.",
    args_model=MoveFileArgs,
    risk=RiskLevel.HIGH_RISK,
    group=ToolGroup.FILESYSTEM,
    confirm_template="Move {source} to {destination}?",
)
def move_file(args: MoveFileArgs) -> dict:
    src = resolve_read_path(args.source)
    dst = resolve_write_path(args.destination)

    if dst.exists():
        if dst.is_dir():
            raise ToolExecutionError(f"destination '{dst}' is an existing directory")
        if not args.overwrite:
            raise ToolExecutionError(f"'{dst}' already exists; pass overwrite=true to replace it")

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))

    return {"source": str(src), "destination": str(dst)}


# -- copy_file ------------------------------------------------------------


class CopyFileArgs(BaseModel):
    source: str = Field(description="Existing file to copy.")
    destination: str = Field(description="Destination path.")
    overwrite: bool = Field(default=False, description="Must be true to replace an existing destination.")


@registry.tool(
    name="copy_file",
    description="Copy a file. Both source and destination must be within allowed directories.",
    args_model=CopyFileArgs,
    risk=RiskLevel.LOW_RISK_WRITE,
    group=ToolGroup.FILESYSTEM,
)
def copy_file(args: CopyFileArgs) -> dict:
    src = resolve_read_path(args.source)
    if not src.is_file():
        raise ToolExecutionError(f"'{src}' is not a file")

    dst = resolve_write_path(args.destination)
    if dst.exists():
        if dst.is_dir():
            raise ToolExecutionError(f"destination '{dst}' is an existing directory")
        if not args.overwrite:
            raise ToolExecutionError(f"'{dst}' already exists; pass overwrite=true to replace it")

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(src), str(dst))

    return {"source": str(src), "destination": str(dst)}


# -- delete_file ------------------------------------------------------------


class DeleteFileArgs(BaseModel):
    path: str = Field(description="File or directory to delete.")
    recursive: bool = Field(
        default=False, description="Required to delete a non-empty directory and its contents."
    )


@registry.tool(
    name="delete_file",
    description="Permanently delete a file or directory within an allowed directory.",
    args_model=DeleteFileArgs,
    risk=RiskLevel.HIGH_RISK,
    group=ToolGroup.FILESYSTEM,
    confirm_template="Permanently delete {path}?",
)
def delete_file(args: DeleteFileArgs) -> dict:
    resolved = resolve_read_path(args.path)

    cfg = load_config()
    if resolved in cfg.allowed_dirs:
        raise ToolExecutionError(f"refusing to delete a sandbox root itself: {resolved}")

    if resolved.is_dir():
        if not args.recursive:
            raise ToolExecutionError(
                f"'{resolved}' is a directory; pass recursive=true to delete it and its contents"
            )
        shutil.rmtree(resolved)
    else:
        resolved.unlink()

    return {"path": str(resolved), "deleted": True}
