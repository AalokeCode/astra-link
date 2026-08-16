"""Git tools (spec §9, §12, §13).

All git invocations go through `subprocess` with argv lists (never
`shell=True`), always scoped to a sandbox-resolved repository directory via
`cwd=`, and always bounded by a timeout. `app.integrations.macos.run_native`
carries the actual process handling.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.integrations.macos import NativeResult, run_native
from app.security.paths import resolve_read_path, resolve_write_path
from app.tools.registry import RiskLevel, ToolExecutionError, ToolGroup, registry

log = logging.getLogger(__name__)

_GIT_TIMEOUT = 15.0
_DIFF_TRUNCATE_BYTES = 30_000
_LOG_FIELD_SEP = "\x1f"
_LOG_RECORD_SEP = "\x1e"


def _resolve_repo_dir(path: str) -> Path:
    """Sandbox-resolve `path` as a directory that should be a git repo root."""
    resolved = resolve_read_path(path)
    if not resolved.is_dir():
        raise ToolExecutionError(f"{resolved} is not a directory")
    return resolved


def _run_git(argv: list[str], *, cwd: Path, timeout: float = _GIT_TIMEOUT) -> NativeResult:
    return run_native(["git", *argv], cwd=cwd, timeout=timeout)


def _require_git_repo(repo_dir: Path) -> None:
    result = _run_git(["rev-parse", "--is-inside-work-tree"], cwd=repo_dir, timeout=5.0)
    if result.timed_out:
        raise ToolExecutionError(f"git timed out checking {repo_dir}")
    if not result.ok or result.stdout.strip() != "true":
        raise ToolExecutionError(f"{repo_dir} is not a git repository")


# ---------------------------------------------------------------------------
# git_status
# ---------------------------------------------------------------------------


class GitStatusArgs(BaseModel):
    repo_path: str = Field(description="Path to the git repository (absolute or ~-relative).")


def _parse_status_porcelain_v2(output: str) -> dict:
    branch = None
    upstream = None
    ahead = 0
    behind = 0
    staged: list[str] = []
    unstaged: list[str] = []
    untracked: list[str] = []

    for line in output.splitlines():
        if not line:
            continue
        if line.startswith("# branch.head"):
            branch = line.split(" ", 2)[2].strip()
        elif line.startswith("# branch.upstream"):
            upstream = line.split(" ", 2)[2].strip()
        elif line.startswith("# branch.ab"):
            # "# branch.ab +N -M"
            parts = line.split()
            for part in parts[2:]:
                if part.startswith("+"):
                    ahead = int(part[1:] or 0)
                elif part.startswith("-"):
                    behind = int(part[1:] or 0)
        elif line.startswith("1 ") or line.startswith("2 "):
            # ordinary / renamed-copied: "1 XY sub mH mI mW hH hI path" or
            # "2 XY sub mH mI mW hH hI Xscore path\torigPath"
            fields = line.split(" ", 8)
            if len(fields) < 9:
                continue
            xy = fields[1]
            path_field = fields[8]
            path = path_field.split("\t", 1)[0]
            x, y = xy[0], xy[1]
            if x != ".":
                staged.append(path)
            if y != ".":
                unstaged.append(path)
        elif line.startswith("u "):
            # unmerged: "u XY sub m1 m2 m3 mW h1 h2 h3 path"
            fields = line.split(" ", 9)
            if len(fields) >= 10:
                unstaged.append(fields[9])
        elif line.startswith("? "):
            untracked.append(line[2:])
        # "!" ignored entries are not requested and are dropped.

    return {
        "branch": branch,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
    }


@registry.tool(
    name="git_status",
    description=(
        "Get the git status of a repository: current branch, upstream tracking "
        "info, ahead/behind counts, and staged/unstaged/untracked file lists."
    ),
    args_model=GitStatusArgs,
    risk=RiskLevel.READ_ONLY,
    group=ToolGroup.GIT,
)
def git_status(args: GitStatusArgs) -> dict:
    repo_dir = _resolve_repo_dir(args.repo_path)
    _require_git_repo(repo_dir)

    result = _run_git(["status", "--porcelain=v2", "--branch"], cwd=repo_dir)
    if not result.ok:
        raise ToolExecutionError(f"git status failed in {repo_dir}: {result.stderr.strip()}")

    parsed = _parse_status_porcelain_v2(result.stdout)
    parsed["repo_path"] = str(repo_dir)
    parsed["clean"] = not (parsed["staged"] or parsed["unstaged"] or parsed["untracked"])
    return parsed


# ---------------------------------------------------------------------------
# git_log
# ---------------------------------------------------------------------------


class GitLogArgs(BaseModel):
    repo_path: str = Field(description="Path to the git repository (absolute or ~-relative).")
    limit: int = Field(default=15, ge=1, le=200, description="Maximum number of commits.")


@registry.tool(
    name="git_log",
    description="List recent commits (hash, author, relative date, subject) for a repository.",
    args_model=GitLogArgs,
    risk=RiskLevel.READ_ONLY,
    group=ToolGroup.GIT,
)
def git_log(args: GitLogArgs) -> dict:
    repo_dir = _resolve_repo_dir(args.repo_path)
    _require_git_repo(repo_dir)

    fmt = _LOG_FIELD_SEP.join(["%H", "%h", "%an", "%ar", "%s"]) + _LOG_RECORD_SEP
    result = _run_git(
        ["log", f"--pretty=format:{fmt}", f"-n{args.limit}"],
        cwd=repo_dir,
    )
    if not result.ok:
        # An empty repo (no commits yet) exits non-zero with a specific stderr.
        if "does not have any commits yet" in result.stderr:
            return {"repo_path": str(repo_dir), "commits": []}
        raise ToolExecutionError(f"git log failed in {repo_dir}: {result.stderr.strip()}")

    commits = []
    for record in result.stdout.split(_LOG_RECORD_SEP):
        record = record.strip("\n")
        if not record:
            continue
        fields = record.split(_LOG_FIELD_SEP)
        if len(fields) != 5:
            continue
        full_hash, short_hash, author, relative_date, subject = fields
        commits.append(
            {
                "hash": full_hash,
                "short_hash": short_hash,
                "author": author,
                "date": relative_date,
                "subject": subject,
            }
        )
    return {"repo_path": str(repo_dir), "commits": commits}


# ---------------------------------------------------------------------------
# git_diff
# ---------------------------------------------------------------------------


class GitDiffArgs(BaseModel):
    repo_path: str = Field(description="Path to the git repository (absolute or ~-relative).")
    staged: bool = Field(default=False, description="Show staged (--cached) diff instead of the working tree diff.")
    path: str | None = Field(default=None, description="Limit the diff to a single file path within the repo.")


@registry.tool(
    name="git_diff",
    description="Show a diff (working tree or staged), optionally scoped to a single file.",
    args_model=GitDiffArgs,
    risk=RiskLevel.READ_ONLY,
    group=ToolGroup.GIT,
)
def git_diff(args: GitDiffArgs) -> dict:
    repo_dir = _resolve_repo_dir(args.repo_path)
    _require_git_repo(repo_dir)

    argv = ["diff"]
    if args.staged:
        argv.append("--cached")
    if args.path:
        argv.extend(["--", args.path])

    result = _run_git(argv, cwd=repo_dir, timeout=20.0)
    if not result.ok:
        raise ToolExecutionError(f"git diff failed in {repo_dir}: {result.stderr.strip()}")

    diff_text = result.stdout
    truncated = False
    if len(diff_text.encode("utf-8")) > _DIFF_TRUNCATE_BYTES:
        diff_bytes = diff_text.encode("utf-8")[:_DIFF_TRUNCATE_BYTES]
        diff_text = diff_bytes.decode("utf-8", errors="ignore")
        diff_text += "\n\n[... diff truncated at 30 KB ...]"
        truncated = True

    return {
        "repo_path": str(repo_dir),
        "staged": args.staged,
        "path": args.path,
        "diff": diff_text,
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# git_branches
# ---------------------------------------------------------------------------


class GitBranchesArgs(BaseModel):
    repo_path: str = Field(description="Path to the git repository (absolute or ~-relative).")


@registry.tool(
    name="git_branches",
    description="List local branches, flagging which one is currently checked out.",
    args_model=GitBranchesArgs,
    risk=RiskLevel.READ_ONLY,
    group=ToolGroup.GIT,
)
def git_branches(args: GitBranchesArgs) -> dict:
    repo_dir = _resolve_repo_dir(args.repo_path)
    _require_git_repo(repo_dir)

    # The literal separator character is embedded directly in the format
    # string (no shell involved, so no quoting concerns) rather than relying
    # on git's `%x1f` hex-escape placeholder, which some git builds mishandle
    # inside `--format=` for `branch` (unlike `log`).
    result = _run_git(
        ["branch", f"--format=%(HEAD){_LOG_FIELD_SEP}%(refname:short)"],
        cwd=repo_dir,
    )
    if not result.ok:
        raise ToolExecutionError(f"git branch failed in {repo_dir}: {result.stderr.strip()}")

    branches = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        head_marker, _, name = line.partition(_LOG_FIELD_SEP)
        branches.append({"name": name, "current": head_marker.strip() == "*"})

    return {"repo_path": str(repo_dir), "branches": branches}


# ---------------------------------------------------------------------------
# git_show_file
# ---------------------------------------------------------------------------


class GitShowFileArgs(BaseModel):
    repo_path: str = Field(description="Path to the git repository (absolute or ~-relative).")
    ref: str = Field(default="HEAD", description="Git ref (branch, tag, or commit) to read the file from.")
    file_path: str = Field(description="Path to the file, relative to the repository root.")


@registry.tool(
    name="git_show_file",
    description="Read a file's contents at a given git ref (e.g. HEAD, a branch, or a commit).",
    args_model=GitShowFileArgs,
    risk=RiskLevel.READ_ONLY,
    group=ToolGroup.GIT,
)
def git_show_file(args: GitShowFileArgs) -> dict:
    repo_dir = _resolve_repo_dir(args.repo_path)
    _require_git_repo(repo_dir)

    result = _run_git(["show", f"{args.ref}:{args.file_path}"], cwd=repo_dir)
    if not result.ok:
        raise ToolExecutionError(
            f"could not read {args.file_path!r} at {args.ref!r} in {repo_dir}: "
            f"{result.stderr.strip()}"
        )
    return {
        "repo_path": str(repo_dir),
        "ref": args.ref,
        "file_path": args.file_path,
        "content": result.stdout,
    }


# ---------------------------------------------------------------------------
# git_push
# ---------------------------------------------------------------------------


class GitPushArgs(BaseModel):
    repo_path: str = Field(description="Path to the git repository (absolute or ~-relative).")
    remote: str = Field(default="origin", description="Remote name to push to.")
    branch: str | None = Field(default=None, description="Branch to push; defaults to the current branch.")


@registry.tool(
    name="git_push",
    description="Push the current (or specified) branch to a remote.",
    args_model=GitPushArgs,
    risk=RiskLevel.HIGH_RISK,
    group=ToolGroup.GIT,
    confirm_template="Push {remote}/{branch} from {repo_path}?",
)
def git_push(args: GitPushArgs) -> dict:
    repo_dir = _resolve_repo_dir(args.repo_path)
    _require_git_repo(repo_dir)

    branch = args.branch
    if branch is None:
        head = _run_git(["branch", "--show-current"], cwd=repo_dir)
        branch = head.stdout.strip() if head.ok else None
        if not branch:
            raise ToolExecutionError(
                f"could not determine current branch in {repo_dir}; pass branch explicitly"
            )

    result = _run_git(["push", args.remote, branch], cwd=repo_dir, timeout=60.0)
    if not result.ok:
        raise ToolExecutionError(
            f"git push to {args.remote}/{branch} failed: {result.stderr.strip()}"
        )
    return {
        "repo_path": str(repo_dir),
        "remote": args.remote,
        "branch": branch,
        "output": (result.stdout + result.stderr).strip(),
    }


# ---------------------------------------------------------------------------
# git_reset
# ---------------------------------------------------------------------------


class GitResetArgs(BaseModel):
    repo_path: str = Field(description="Path to the git repository (absolute or ~-relative).")
    mode: Literal["soft", "mixed", "hard"] = Field(
        description="Reset mode. 'hard' discards all uncommitted changes irrecoverably."
    )
    ref: str = Field(default="HEAD", description="Ref to reset to.")


@registry.tool(
    name="git_reset",
    description=(
        "Reset the repository to a ref with the given mode. 'hard' permanently "
        "discards uncommitted working-tree and staged changes."
    ),
    args_model=GitResetArgs,
    risk=RiskLevel.HIGH_RISK,
    group=ToolGroup.GIT,
    confirm_template=(
        "Reset {repo_path} to {ref} in {mode} mode"
        " — this will PERMANENTLY DISCARD uncommitted changes if mode is 'hard'?"
    ),
)
def git_reset(args: GitResetArgs) -> dict:
    repo_dir = _resolve_write_repo_dir(args.repo_path)
    _require_git_repo(repo_dir)

    result = _run_git(["reset", f"--{args.mode}", args.ref], cwd=repo_dir)
    if not result.ok:
        raise ToolExecutionError(
            f"git reset --{args.mode} {args.ref} failed in {repo_dir}: {result.stderr.strip()}"
        )
    return {
        "repo_path": str(repo_dir),
        "mode": args.mode,
        "ref": args.ref,
        "output": (result.stdout + result.stderr).strip(),
    }


def _resolve_write_repo_dir(path: str) -> Path:
    resolved = resolve_write_path(path, must_exist=True)
    if not resolved.is_dir():
        raise ToolExecutionError(f"{resolved} is not a directory")
    return resolved
