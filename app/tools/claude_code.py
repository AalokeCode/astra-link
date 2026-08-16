"""Claude Code environment inspection (spec §13).

The headline query this serves is "what is Claude Code doing?" — which must
be answered from real, current signals or not at all. Every field returned
here traces back to a live process, a file on disk, or content read directly
out of a session transcript. Nothing is inferred or fabricated; anything that
can't be determined degrades to an empty list/None rather than a guess.

Data sources (all optional — each is allowed to be absent or unreadable):
  1. Live processes:      `pgrep -fl claude`
  2. Session transcripts:  ~/.claude/projects/<encoded-cwd>/*.jsonl
  3. Git state per project, via app.tools.git's parsing helpers.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.integrations.macos import run_native
from app.tools.git import _parse_status_porcelain_v2, _run_git
from app.tools.registry import RiskLevel, ToolGroup, registry

log = logging.getLogger(__name__)

CLAUDE_HOME = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_HOME / "projects"

# Session files can grow large over a long-running session; only the tail
# carries recent activity (last prompt, mode, title), so that's all we read.
_TAIL_BYTES = 16_000
_MAX_PROJECTS = 10
_MAX_RECENT_ACTIVITY = 20


# ---------------------------------------------------------------------------
# cwd-directory-name decoding
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedProject:
    """Result of trying to recover a real filesystem path from a projects/
    subdirectory name.

    `raw_name` is always populated. `path` and `resolved` describe whether we
    could confirm a real directory backs it — the encoding is lossy (a
    literal '-' in a path segment is indistinguishable from an encoded '/'),
    so a decode is only trusted once it's checked against disk.
    """

    raw_name: str
    path: Path | None
    resolved: bool


def _naive_decode(dirname: str) -> Path:
    """Reverse the '/' -> '-' substitution used to encode a cwd.

    Pure and side-effect free; verification against disk happens separately
    so this stays easy to unit test.
    """
    return Path(dirname.replace("-", "/"))


def _cwd_from_transcripts(project_dir: Path) -> Path | None:
    """Fall back to the `cwd` field embedded in the project's own transcripts.

    This is the ground truth the encoding is lossy about, so when the naive
    decode doesn't check out, scanning for an actual recorded cwd is more
    reliable than guessing at dash placement.
    """
    try:
        jsonl_files = sorted(project_dir.glob("*.jsonl"))
    except OSError:
        return None

    for jsonl_path in jsonl_files:
        for record in _read_jsonl_tail(jsonl_path):
            cwd = record.get("cwd")
            if not cwd:
                continue
            candidate = Path(cwd)
            try:
                if candidate.is_dir():
                    return candidate
            except OSError:
                continue
    return None


def resolve_project_dir(dirname: str, project_dir: Path) -> ResolvedProject:
    """Best-effort reverse of the ~/.claude/projects encoding for one entry.

    Tries the direct decode first (cheap, no file reads); falls back to
    reading a real `cwd` out of the project's transcripts; reports
    unresolved rather than returning a guessed path that doesn't exist.
    """
    candidate = _naive_decode(dirname)
    try:
        if candidate.is_dir():
            return ResolvedProject(raw_name=dirname, path=candidate, resolved=True)
    except OSError:
        pass

    fallback = _cwd_from_transcripts(project_dir)
    if fallback is not None:
        return ResolvedProject(raw_name=dirname, path=fallback, resolved=True)

    return ResolvedProject(raw_name=dirname, path=None, resolved=False)


# ---------------------------------------------------------------------------
# transcript reading
# ---------------------------------------------------------------------------


def _read_jsonl_tail(path: Path, tail_bytes: int = _TAIL_BYTES) -> list[dict[str, Any]]:
    """Read the last `tail_bytes` of a jsonl file and parse whole lines.

    Large sessions can run to many MB; we only need recent activity, so the
    whole file is never loaded. The first partial line after seeking into
    the middle of the file is expected to fail to parse and is dropped
    silently, along with any other malformed/truncated line — a corrupt
    transcript must never crash status reporting.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > tail_bytes:
                fh.seek(size - tail_bytes)
            raw = fh.read()
    except OSError as exc:
        log.debug("could not read %s: %s", path, exc)
        return []

    records: list[dict[str, Any]] = []
    for raw_line in raw.decode("utf-8", errors="ignore").splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


@dataclass
class _ProjectSummary:
    raw_name: str
    path: Path | None
    resolved: bool
    last_active_ts: float
    sessions: list[dict[str, Any]] = field(default_factory=list)
    recent_activity: list[dict[str, Any]] = field(default_factory=list)


def _summarize_project(project_dir: Path) -> _ProjectSummary | None:
    try:
        jsonl_files = sorted(
            (p for p in project_dir.iterdir() if p.suffix == ".jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError as exc:
        log.debug("could not list %s: %s", project_dir, exc)
        return None

    resolved = resolve_project_dir(project_dir.name, project_dir)

    if not jsonl_files:
        try:
            last_active_ts = project_dir.stat().st_mtime
        except OSError:
            return None
        return _ProjectSummary(
            raw_name=resolved.raw_name,
            path=resolved.path,
            resolved=resolved.resolved,
            last_active_ts=last_active_ts,
        )

    try:
        last_active_ts = max(p.stat().st_mtime for p in jsonl_files)
    except OSError:
        last_active_ts = project_dir.stat().st_mtime if project_dir.exists() else 0.0

    sessions: list[dict[str, Any]] = []
    recent_activity: list[dict[str, Any]] = []
    label = str(resolved.path) if resolved.path else resolved.raw_name

    for jsonl_path in jsonl_files:
        try:
            mtime = jsonl_path.stat().st_mtime
        except OSError:
            continue
        records = _read_jsonl_tail(jsonl_path)

        session_id = jsonl_path.stem
        last_prompt: str | None = None
        title: str | None = None
        git_branch: str | None = None
        for record in records:
            session_id = record.get("session_id") or record.get("sessionId") or session_id
            rtype = record.get("type")
            if rtype == "last-prompt":
                prompt = record.get("lastPrompt")
                if isinstance(prompt, str):
                    last_prompt = prompt
            elif rtype == "ai-title":
                ai_title = record.get("aiTitle")
                if isinstance(ai_title, str):
                    title = ai_title
            branch = record.get("gitBranch")
            if isinstance(branch, str) and branch and branch != "HEAD":
                git_branch = branch

        sessions.append(
            {
                "session_id": session_id,
                "project": label,
                "path": str(resolved.path) if resolved.path else None,
                "last_active": _iso(mtime),
                "last_prompt": last_prompt,
                "title": title,
                "git_branch": git_branch,
            }
        )

        if last_prompt or title:
            recent_activity.append(
                {
                    "project": label,
                    "path": str(resolved.path) if resolved.path else None,
                    "description": title or last_prompt,
                    "timestamp": _iso(mtime),
                }
            )

    return _ProjectSummary(
        raw_name=resolved.raw_name,
        path=resolved.path,
        resolved=resolved.resolved,
        last_active_ts=last_active_ts,
        sessions=sessions,
        recent_activity=recent_activity,
    )


def _live_processes() -> list[dict[str, Any]]:
    result = run_native(["pgrep", "-fl", "claude"], timeout=5.0)
    if not result.ok:
        # No matching process is a normal, expected outcome (exit code 1),
        # not an error — anything else is logged but still degrades quietly.
        if result.returncode not in (1, -1):
            log.debug("pgrep failed: %s", result.stderr)
        return []

    processes = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_str, _, command = line.partition(" ")
        if not pid_str.isdigit():
            continue
        processes.append({"pid": int(pid_str), "command": command.strip()})
    return processes


def _git_changes_for(path: Path) -> dict[str, Any] | None:
    """Reuse git.py's porcelain-v2 parsing to summarize uncommitted changes.

    Goes straight to the subprocess helpers rather than through the
    `git_status` tool handler, since that handler sandbox-resolves its path
    argument against `cfg.allowed_dirs` — project directories discovered via
    ~/.claude/projects are already verified to exist on disk and may live
    outside the configured sandbox roots (other users' home directories,
    scratch checkouts, etc.).
    """
    check = _run_git(["rev-parse", "--is-inside-work-tree"], cwd=path, timeout=5.0)
    if not check.ok or check.stdout.strip() != "true":
        return None

    status = _run_git(["status", "--porcelain=v2", "--branch"], cwd=path, timeout=10.0)
    if not status.ok:
        return None

    parsed = _parse_status_porcelain_v2(status.stdout)
    if not (parsed["staged"] or parsed["unstaged"] or parsed["untracked"]):
        return None

    return {
        "path": str(path),
        "branch": parsed["branch"],
        "staged": len(parsed["staged"]),
        "unstaged": len(parsed["unstaged"]),
        "untracked": len(parsed["untracked"]),
    }


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------


class ClaudeCodeStatusArgs(BaseModel):
    pass


@registry.tool(
    name="get_claude_code_status",
    description=(
        "Inspect the local environment to determine whether Claude Code is "
        "currently running, which projects it has recently touched, and what "
        "uncommitted git changes exist in those projects. Never fabricated — "
        "reflects only live processes and on-disk session transcripts."
    ),
    args_model=ClaudeCodeStatusArgs,
    risk=RiskLevel.READ_ONLY,
    group=ToolGroup.CLAUDE_CODE,
)
def get_claude_code_status(args: ClaudeCodeStatusArgs) -> dict:
    processes = _live_processes()

    result: dict[str, Any] = {
        "running": len(processes) > 0,
        "projects": [],
        "processes": processes,
        "recent_activity": [],
        "git_changes": [],
        "sessions": [],
    }

    if not PROJECTS_DIR.is_dir():
        return result

    try:
        project_dirs = [p for p in PROJECTS_DIR.iterdir() if p.is_dir()]
    except OSError as exc:
        log.warning("could not list %s: %s", PROJECTS_DIR, exc)
        return result

    summaries: list[_ProjectSummary] = []
    for project_dir in project_dirs:
        try:
            summary = _summarize_project(project_dir)
        except Exception:  # noqa: BLE001 - one bad project must not sink status
            log.exception("failed to summarize %s", project_dir)
            continue
        if summary is not None:
            summaries.append(summary)

    summaries.sort(key=lambda s: s.last_active_ts, reverse=True)
    top = summaries[:_MAX_PROJECTS]

    all_activity: list[dict[str, Any]] = []
    all_sessions: list[dict[str, Any]] = []
    git_changes: list[dict[str, Any]] = []

    for summary in top:
        label = str(summary.path) if summary.path else summary.raw_name
        result["projects"].append(
            {
                "name": label,
                "raw_name": summary.raw_name,
                "path": str(summary.path) if summary.path else None,
                "path_resolved": summary.resolved,
                "last_active": _iso(summary.last_active_ts),
            }
        )
        all_activity.extend(summary.recent_activity)
        all_sessions.extend(summary.sessions)

        if summary.path is not None:
            try:
                change = _git_changes_for(summary.path)
            except Exception:  # noqa: BLE001
                log.exception("git status failed for %s", summary.path)
                change = None
            if change is not None:
                git_changes.append(change)

    all_activity.sort(key=lambda a: a["timestamp"], reverse=True)
    all_sessions.sort(key=lambda s: s["last_active"], reverse=True)

    result["recent_activity"] = all_activity[:_MAX_RECENT_ACTIVITY]
    result["sessions"] = all_sessions[:_MAX_PROJECTS]
    result["git_changes"] = git_changes
    return result


class ListClaudeProjectsArgs(BaseModel):
    pass


@registry.tool(
    name="list_claude_projects",
    description="List all known Claude Code project directories with their last-activity time.",
    args_model=ListClaudeProjectsArgs,
    risk=RiskLevel.READ_ONLY,
    group=ToolGroup.CLAUDE_CODE,
)
def list_claude_projects(args: ListClaudeProjectsArgs) -> dict:
    if not PROJECTS_DIR.is_dir():
        return {"projects": []}

    try:
        project_dirs = [p for p in PROJECTS_DIR.iterdir() if p.is_dir()]
    except OSError as exc:
        log.warning("could not list %s: %s", PROJECTS_DIR, exc)
        return {"projects": []}

    projects = []
    for project_dir in project_dirs:
        resolved = resolve_project_dir(project_dir.name, project_dir)
        jsonl_files: list[Path] = []
        last_active_ts: float | None = None
        try:
            jsonl_files = list(project_dir.glob("*.jsonl"))
            last_active_ts = (
                max((p.stat().st_mtime for p in jsonl_files), default=None)
                or project_dir.stat().st_mtime
            )
        except OSError:
            pass

        projects.append(
            {
                "raw_name": resolved.raw_name,
                "path": str(resolved.path) if resolved.path else None,
                "path_resolved": resolved.resolved,
                "last_active": _iso(last_active_ts) if last_active_ts else None,
                "session_count": len(jsonl_files),
            }
        )

    projects.sort(key=lambda p: p["last_active"] or "", reverse=True)
    return {"projects": projects}
