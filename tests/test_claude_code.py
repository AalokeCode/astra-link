"""Tests for the Claude Code environment inspection tool (spec §13) and the
`inspect_project` piece of `app.tools.system` (spec §12) it shares parsing
logic with.

Resilience is the point of this module: a missing `~/.claude`, an
unresolvable directory-name decode, or a malformed transcript line must all
degrade to empty/partial results rather than raise.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

import app.tools.claude_code as claude_code
from app.tools.claude_code import (
    ClaudeCodeStatusArgs,
    _read_jsonl_tail,
    get_claude_code_status,
    resolve_project_dir,
)
from app.tools.system import InspectProjectArgs, inspect_project

# ---------------------------------------------------------------------------
# cwd-directory-name decoding
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_tmp_dir():
    """A tmp dir with no dashes anywhere in its own path.

    pytest's own `tmp_path` fixture nests under ancestors like
    `pytest-of-<user>/pytest-<n>/...`, which contain literal dashes and would
    corrupt the naive '-' -> '/' decode being tested here (the ambiguity is
    real, not a test artifact — see `resolve_project_dir`'s docstring). Using
    a dash-free base isolates the round-trip assertion from that ambiguity;
    the ambiguity itself is covered separately below.
    """
    base = Path(tempfile.mkdtemp(prefix="claudecodetest", dir="/private/tmp"))
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_resolve_project_dir_round_trips_when_directory_exists(clean_tmp_dir: Path) -> None:
    real_project = clean_tmp_dir / "workspace" / "myapp"
    real_project.mkdir(parents=True)

    encoded_name = str(real_project).replace("/", "-")
    claude_project_dir = clean_tmp_dir / "claude_home" / encoded_name
    claude_project_dir.mkdir(parents=True)

    resolved = resolve_project_dir(encoded_name, claude_project_dir)

    assert resolved.resolved is True
    assert resolved.path == real_project
    assert resolved.raw_name == encoded_name


def test_resolve_project_dir_reports_unresolved_instead_of_guessing(tmp_path: Path) -> None:
    # Naive decode of this name doesn't correspond to any real directory, and
    # the project dir has no transcripts to fall back on — this must be
    # reported as unresolved, never a best-guess path.
    dirname = "-Users-nobody-does-not-exist-anywhere-at-all"
    claude_project_dir = tmp_path / dirname
    claude_project_dir.mkdir()

    resolved = resolve_project_dir(dirname, claude_project_dir)

    assert resolved.resolved is False
    assert resolved.path is None
    assert resolved.raw_name == dirname


def test_resolve_project_dir_falls_back_to_transcript_cwd(tmp_path: Path) -> None:
    # Covers the genuinely ambiguous case: a directory name whose naive
    # decode does NOT correspond to a real directory (e.g. because a real
    # path segment contained a literal '-'), but whose own transcript
    # records the true cwd. This is the case the naive decode alone cannot
    # solve, and it's exactly why we verify against disk instead of assuming.
    real_project = tmp_path / "real" / "my-project-with-dashes"
    real_project.mkdir(parents=True)

    claude_project_dir = tmp_path / "-this-does-not-decode-to-anything-real"
    claude_project_dir.mkdir()
    transcript = claude_project_dir / "session.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "cwd": str(real_project), "sessionId": "abc"}) + "\n"
    )

    resolved = resolve_project_dir(claude_project_dir.name, claude_project_dir)

    assert resolved.resolved is True
    assert resolved.path == real_project


# ---------------------------------------------------------------------------
# transcript reading
# ---------------------------------------------------------------------------


def test_read_jsonl_tail_tolerates_malformed_lines(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text(
        "{this is not valid json\n"
        + json.dumps({"type": "mode", "mode": "normal", "sessionId": "s1"})
        + "\n"
        + '{"truncated": tr'  # simulates a partially-written / cut-off line
    )

    records = _read_jsonl_tail(path)

    assert records == [{"type": "mode", "mode": "normal", "sessionId": "s1"}]


def test_read_jsonl_tail_missing_file_returns_empty(tmp_path: Path) -> None:
    assert _read_jsonl_tail(tmp_path / "does-not-exist.jsonl") == []


# ---------------------------------------------------------------------------
# get_claude_code_status
# ---------------------------------------------------------------------------

_STATUS_KEYS = {"running", "projects", "processes", "recent_activity", "git_changes", "sessions"}


def test_get_claude_code_status_missing_claude_home_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_code, "PROJECTS_DIR", tmp_path / "does-not-exist")
    monkeypatch.setattr(claude_code, "_live_processes", lambda: [])

    result = get_claude_code_status(ClaudeCodeStatusArgs())

    assert result == {
        "running": False,
        "projects": [],
        "processes": [],
        "recent_activity": [],
        "git_changes": [],
        "sessions": [],
    }


def test_get_claude_code_status_against_fake_projects_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_project = tmp_path / "projects" / "demo-app"
    real_project.mkdir(parents=True)

    projects_dir = tmp_path / "claude_home" / "projects"
    encoded_name = str(real_project).replace("/", "-")
    project_dir = projects_dir / encoded_name
    project_dir.mkdir(parents=True)

    session_path = project_dir / "11111111-1111-1111-1111-111111111111.jsonl"
    session_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "sessionId": "s1",
                        "cwd": str(real_project),
                        "gitBranch": "main",
                    }
                ),
                json.dumps(
                    {"type": "last-prompt", "lastPrompt": "fix the build", "sessionId": "s1"}
                ),
                json.dumps({"type": "ai-title", "aiTitle": "Fix build error", "sessionId": "s1"}),
            ]
        )
        + "\n"
    )

    monkeypatch.setattr(claude_code, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(claude_code, "_live_processes", lambda: [])

    result = get_claude_code_status(ClaudeCodeStatusArgs())

    assert set(result.keys()) == _STATUS_KEYS
    assert result["running"] is False
    assert len(result["projects"]) == 1

    project = result["projects"][0]
    assert project["path"] == str(real_project)
    assert project["path_resolved"] is True
    assert project["last_active"]  # non-empty ISO timestamp

    assert len(result["sessions"]) == 1
    session = result["sessions"][0]
    assert session["last_prompt"] == "fix the build"
    assert session["title"] == "Fix build error"
    assert session["git_branch"] == "main"

    assert result["recent_activity"]
    assert result["recent_activity"][0]["description"] in {"fix the build", "Fix build error"}


def test_get_claude_code_status_reports_running_from_live_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_code, "PROJECTS_DIR", tmp_path / "does-not-exist")
    monkeypatch.setattr(
        claude_code, "_live_processes", lambda: [{"pid": 123, "command": "claude"}]
    )

    result = get_claude_code_status(ClaudeCodeStatusArgs())

    assert result["running"] is True
    assert result["processes"] == [{"pid": 123, "command": "claude"}]


# ---------------------------------------------------------------------------
# inspect_project (app.tools.system) — framework detection
# ---------------------------------------------------------------------------


def test_inspect_project_identifies_nextjs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "my-next-app"
    project_root.mkdir()
    (project_root / "package.json").write_text(
        json.dumps(
            {
                "name": "my-next-app",
                "dependencies": {"next": "^14.0.0", "react": "^18.0.0"},
            }
        )
    )
    (project_root / "tsconfig.json").write_text("{}")
    (project_root / "app").mkdir()
    (project_root / "node_modules").mkdir()

    monkeypatch.setenv("ALLOWED_DIRS", str(tmp_path))

    result = inspect_project(InspectProjectArgs(path=str(project_root)))

    assert result["project_type"] == "Node.js application"
    assert result["framework"] == "Next.js"
    assert result["language"] == "TypeScript"
    assert "app" in result["entry_points"]
    assert "node_modules" not in result["top_level_entries"]
