"""macOS system status, app launching, and project inspection (spec §12, §39).

Everything here shells out to native macOS utilities via
`app.integrations.macos.run_native` and parses their output defensively —
these commands' text formats are not a stable contract, so a missing field
degrades to `None` rather than raising.
"""

from __future__ import annotations

import json
import logging
import platform
import re
import socket
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.integrations.macos import parse_df, parse_pmset_batt, parse_vm_stat, run_native
from app.security.paths import resolve_read_path
from app.tools.git import _parse_status_porcelain_v2, _run_git
from app.tools.registry import RiskLevel, ToolExecutionError, ToolGroup, registry

log = logging.getLogger(__name__)

_APP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+\-]{0,127}$")

# Spec §14 documentation ignore rules, reused here so project inspection
# never walks into vendored/build output.
IGNORE_DIR_NAMES = {
    "node_modules",
    ".git",
    "dist",
    "build",
    ".next",
    "venv",
    ".venv",
    "__pycache__",
}
IGNORE_FILE_NAMES = {".env"}

_MAX_TOP_LEVEL_ENTRIES = 60
_MAX_MANIFEST_BYTES = 20_000
_MAX_README_BYTES = 5_000

_ENTRY_POINT_CANDIDATES = [
    "app",
    "src",
    "pages",
    "cmd",
    "lib",
    "server.js",
    "server.ts",
    "index.js",
    "index.ts",
    "main.py",
    "main.go",
    "manage.py",
]


# ---------------------------------------------------------------------------
# get_system_status
# ---------------------------------------------------------------------------


class SystemStatusArgs(BaseModel):
    pass


def _macos_version() -> dict[str, str]:
    # platform.mac_ver() reads the same source sw_vers does (SystemVersion
    # plist) without spawning a process; kept as a stdlib-first choice.
    release, _, _ = platform.mac_ver()
    build = ""
    result = run_native(["sw_vers", "-buildVersion"], timeout=5.0)
    if result.ok:
        build = result.stdout.strip()
    return {"release": release or "unknown", "build": build}


def _uptime() -> dict[str, Any]:
    info: dict[str, Any] = {"raw": None, "seconds": None}
    result = run_native(["uptime"], timeout=5.0)
    if result.ok:
        info["raw"] = result.stdout.strip()

    boot = run_native(["sysctl", "-n", "kern.boottime"], timeout=5.0)
    if boot.ok:
        # "{ sec = 1690000000, usec = 123456 } Mon Jul 22 ..."
        match = re.search(r"sec\s*=\s*(\d+)", boot.stdout)
        if match:
            import time

            info["seconds"] = max(0, int(time.time()) - int(match.group(1)))
    return info


def _cpu_load() -> dict[str, Any]:
    info: dict[str, Any] = {"load_1m": None, "load_5m": None, "load_15m": None, "cpu_count": None}
    load = run_native(["sysctl", "-n", "vm.loadavg"], timeout=5.0)
    if load.ok:
        nums = re.findall(r"[\d.]+", load.stdout)
        if len(nums) >= 3:
            info["load_1m"], info["load_5m"], info["load_15m"] = (float(n) for n in nums[:3])

    ncpu = run_native(["sysctl", "-n", "hw.ncpu"], timeout=5.0)
    if ncpu.ok and ncpu.stdout.strip().isdigit():
        info["cpu_count"] = int(ncpu.stdout.strip())
    return info


def _memory_status() -> dict[str, Any]:
    info: dict[str, Any] = {
        "total_gb": None,
        "free_gb": None,
        "active_gb": None,
        "wired_gb": None,
        "compressed_gb": None,
        "pressure_percent": None,
    }

    memsize = run_native(["sysctl", "-n", "hw.memsize"], timeout=5.0)
    total_bytes = int(memsize.stdout.strip()) if memsize.ok and memsize.stdout.strip().isdigit() else None

    vmstat = run_native(["vm_stat"], timeout=5.0)
    if not vmstat.ok:
        if total_bytes:
            info["total_gb"] = round(total_bytes / 1024**3, 1)
        return info

    stats = parse_vm_stat(vmstat.stdout)
    page_size = stats.get("page_size", 4096)

    def gb(pages_key: str) -> float | None:
        pages = stats.get(pages_key)
        return round(pages * page_size / 1024**3, 2) if pages is not None else None

    info["free_gb"] = gb("pages_free")
    info["active_gb"] = gb("pages_active")
    info["wired_gb"] = gb("pages_wired_down")
    info["compressed_gb"] = gb("pages_occupied_by_compressor")

    if total_bytes:
        info["total_gb"] = round(total_bytes / 1024**3, 1)
        wired = stats.get("pages_wired_down", 0) * page_size
        active = stats.get("pages_active", 0) * page_size
        info["pressure_percent"] = round(min(100.0, (wired + active) / total_bytes * 100), 1)

    return info


def _disk_free(mount: str = "/") -> dict[str, Any] | None:
    result = run_native(["df", "-H", mount], timeout=5.0)
    if not result.ok:
        return None
    return parse_df(result.stdout)


def _battery() -> dict[str, Any] | None:
    result = run_native(["pmset", "-g", "batt"], timeout=5.0)
    if not result.ok:
        return None
    return parse_pmset_batt(result.stdout)


@registry.tool(
    name="get_system_status",
    description=(
        "Get current macOS system status: OS version, hostname, uptime, CPU "
        "load, memory pressure, free disk space, and battery if present."
    ),
    args_model=SystemStatusArgs,
    risk=RiskLevel.READ_ONLY,
    group=ToolGroup.SYSTEM,
)
def get_system_status(args: SystemStatusArgs) -> dict:
    return {
        "macos_version": _macos_version(),
        "hostname": socket.gethostname(),
        "uptime": _uptime(),
        "cpu": _cpu_load(),
        "memory": _memory_status(),
        "disk": _disk_free(),
        "battery": _battery(),
    }


# ---------------------------------------------------------------------------
# open_application
# ---------------------------------------------------------------------------


class OpenApplicationArgs(BaseModel):
    app_name: str = Field(description="Application name as it appears in /Applications, e.g. 'Visual Studio Code'.")
    path: str | None = Field(
        default=None, description="Optional file or directory to open with the application."
    )


@registry.tool(
    name="open_application",
    description="Open a macOS application, optionally with a file or project path.",
    args_model=OpenApplicationArgs,
    risk=RiskLevel.LOW_RISK_WRITE,
    group=ToolGroup.SYSTEM,
)
def open_application(args: OpenApplicationArgs) -> dict:
    app_name = args.app_name.strip()
    if not _APP_NAME_RE.match(app_name):
        raise ToolExecutionError(
            f"{app_name!r} is not a valid application name "
            "(letters, digits, spaces, '.', '_', '+', '-' only)"
        )

    argv = ["open", "-a", app_name]
    resolved_path: Path | None = None
    if args.path:
        resolved_path = resolve_read_path(args.path)
        argv.append(str(resolved_path))

    result = run_native(argv, timeout=10.0)
    if not result.ok:
        raise ToolExecutionError(
            f"could not open {app_name!r}: {(result.stderr or result.stdout).strip()}"
        )

    return {
        "app_name": app_name,
        "path": str(resolved_path) if resolved_path else None,
        "opened": True,
    }


# ---------------------------------------------------------------------------
# inspect_project
# ---------------------------------------------------------------------------


class InspectProjectArgs(BaseModel):
    path: str = Field(description="Path to the project root to inspect.")


def _read_bounded(path: Path, max_bytes: int) -> str | None:
    try:
        with path.open("rb") as fh:
            raw = fh.read(max_bytes)
    except OSError:
        return None
    return raw.decode("utf-8", errors="ignore")


def _detect_js_framework(package_json: dict[str, Any]) -> str | None:
    deps: dict[str, str] = {}
    for key in ("dependencies", "devDependencies"):
        section = package_json.get(key)
        if isinstance(section, dict):
            deps.update(section)

    checks = [
        ("next", "Next.js"),
        ("nuxt", "Nuxt"),
        ("@angular/core", "Angular"),
        ("@nestjs/core", "NestJS"),
        ("svelte", "Svelte"),
        ("vue", "Vue"),
        ("express", "Express"),
        ("react", "React"),
    ]
    for dep_name, label in checks:
        if dep_name in deps:
            return label
    return None


def _js_package_manager(top_level: set[str]) -> str | None:
    if "pnpm-lock.yaml" in top_level:
        return "pnpm"
    if "yarn.lock" in top_level:
        return "yarn"
    if "bun.lockb" in top_level:
        return "bun"
    if "package-lock.json" in top_level:
        return "npm"
    return "npm"


def _py_package_manager(top_level: set[str]) -> str | None:
    if "poetry.lock" in top_level:
        return "poetry"
    if "Pipfile" in top_level:
        return "pipenv"
    if "uv.lock" in top_level:
        return "uv"
    if "requirements.txt" in top_level:
        return "pip"
    return "pip"


def _classify_project(
    manifests: dict[str, str], top_level: set[str]
) -> tuple[str, str | None, str | None, str | None]:
    """Returns (project_type, package_manager, framework, language)."""
    if "package.json" in manifests:
        try:
            package_json = json.loads(manifests["package.json"])
        except json.JSONDecodeError:
            package_json = {}
        framework = _detect_js_framework(package_json)
        language = "TypeScript" if "tsconfig.json" in top_level else "JavaScript"
        return "Node.js application", _js_package_manager(top_level), framework, language

    if "pyproject.toml" in manifests or "requirements.txt" in manifests:
        return "Python project", _py_package_manager(top_level), None, "Python"

    if "go.mod" in manifests:
        return "Go module", "go modules", None, "Go"

    if "Cargo.toml" in manifests:
        return "Rust crate", "cargo", None, "Rust"

    return "Unknown project type", None, None, None


def _entry_points(top_level: set[str]) -> list[str]:
    return [name for name in _ENTRY_POINT_CANDIDATES if name in top_level]


def _git_summary(root: Path) -> dict[str, Any] | None:
    check = _run_git(["rev-parse", "--is-inside-work-tree"], cwd=root, timeout=5.0)
    if not check.ok or check.stdout.strip() != "true":
        return None

    branch_result = _run_git(["branch", "--show-current"], cwd=root, timeout=5.0)
    branch = branch_result.stdout.strip() if branch_result.ok else None

    status_result = _run_git(["status", "--porcelain=v2", "--branch"], cwd=root, timeout=10.0)
    changed_files = 0
    if status_result.ok:
        parsed = _parse_status_porcelain_v2(status_result.stdout)
        changed_files = len(
            set(parsed["staged"]) | set(parsed["unstaged"]) | set(parsed["untracked"])
        )

    return {"branch": branch or None, "changed_files": changed_files}


@registry.tool(
    name="inspect_project",
    description=(
        "Inspect a project directory to identify its type, package manager, "
        "framework, language, git status, and likely entry points. Reads only "
        "manifest files and a bounded top-level directory listing — never "
        "walks the whole repository."
    ),
    args_model=InspectProjectArgs,
    risk=RiskLevel.READ_ONLY,
    group=ToolGroup.SYSTEM,
)
def inspect_project(args: InspectProjectArgs) -> dict:
    root = resolve_read_path(args.path)
    if not root.is_dir():
        raise ToolExecutionError(f"{root} is not a directory")

    try:
        children = list(root.iterdir())
    except OSError as exc:
        raise ToolExecutionError(f"could not list {root}: {exc}") from exc

    top_level_names = {c.name for c in children}

    manifests: dict[str, str] = {}
    for manifest_name in ("package.json", "pyproject.toml", "requirements.txt", "go.mod", "Cargo.toml"):
        if manifest_name in IGNORE_FILE_NAMES:
            continue
        candidate = root / manifest_name
        if candidate.is_file():
            content = _read_bounded(candidate, _MAX_MANIFEST_BYTES)
            if content is not None:
                manifests[manifest_name] = content

    readme_excerpt = None
    for candidate in sorted(root.glob("README*")):
        if candidate.is_file():
            content = _read_bounded(candidate, _MAX_README_BYTES)
            if content is not None:
                readme_excerpt = content
            break

    visible_entries = sorted(
        name
        for name in top_level_names
        if name not in IGNORE_DIR_NAMES and name not in IGNORE_FILE_NAMES
    )[:_MAX_TOP_LEVEL_ENTRIES]

    project_type, package_manager, framework, language = _classify_project(
        manifests, top_level_names
    )

    git_info = None
    try:
        git_info = _git_summary(root)
    except Exception:  # noqa: BLE001 - inspection must not fail on git errors
        log.exception("git summary failed for %s", root)

    return {
        "path": str(root),
        "project_type": project_type,
        "package_manager": package_manager,
        "framework": framework,
        "language": language,
        "git": git_info,
        "readme_excerpt": readme_excerpt,
        "top_level_entries": visible_entries,
        "entry_points": _entry_points(top_level_names),
    }
