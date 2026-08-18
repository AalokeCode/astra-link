from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess

import pytest

from app.integrations.agent_workspace import (
    AgentWorkspaceError,
    KittyAgentWorkspace,
    diagnose_terminal,
    terminal_state,
)


def test_diagnose_terminal_explains_provider_quota() -> None:
    diagnosis = diagnose_terminal("API error 429: rate limit exceeded")

    assert diagnosis is not None
    assert diagnosis.code == "quota"
    assert "usage limit" in diagnosis.summary


def test_diagnose_terminal_explains_mcp_failure() -> None:
    diagnosis = diagnose_terminal("MCP server filesystem failed to connect")

    assert diagnosis is not None
    assert diagnosis.code == "mcp"


def test_terminal_state_marks_confirmation_as_needing_input() -> None:
    assert terminal_state("Do you want to proceed? [y/n]", None) == "needs_input"


def test_terminal_state_does_not_invent_error_for_normal_output() -> None:
    assert diagnose_terminal("Building project\nTests passed") is None
    assert terminal_state("Building project\nTests passed", None) == "running"


def test_workspace_denies_project_outside_allowed_dirs(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    denied = tmp_path / "denied"
    allowed.mkdir()
    denied.mkdir()

    class Config:
        data_dir = tmp_path / "data"
        allowed_dirs = [allowed.resolve()]

    workspace = KittyAgentWorkspace(Config(), default_project=allowed)

    with pytest.raises(AgentWorkspaceError) as exc_info:
        workspace._resolve_project(str(denied))

    assert exc_info.value.code == "project_denied"


def test_workspace_rejects_empty_and_oversized_prompts(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    class Config:
        data_dir = tmp_path / "data"
        allowed_dirs = [tmp_path.resolve()]

    workspace = KittyAgentWorkspace(Config(), default_project=project)

    with pytest.raises(AgentWorkspaceError, match="Enter a prompt"):
        workspace.send_prompt("missing", "   ")
    with pytest.raises(AgentWorkspaceError, match="20,000"):
        workspace.send_prompt("missing", "x" * 20_001)
    with pytest.raises(AgentWorkspaceError, match="prompt or steer"):
        workspace.send_prompt("missing", "do work", kind="unknown")


def test_workspace_sends_real_enter_key_after_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()

    class Config:
        data_dir = tmp_path / "data"
        allowed_dirs = [tmp_path.resolve()]

    workspace = KittyAgentWorkspace(Config(), default_project=project)
    calls: list[tuple[str, tuple[str, ...], str | None]] = []

    monkeypatch.setattr(workspace, "_get_window", lambda _instance_id: {"id": 42})

    def fake_remote(command: str, *args: str, input_text=None, timeout=8.0):
        del timeout
        calls.append((command, args, input_text))
        return CompletedProcess([], 0, "", "")

    monkeypatch.setattr(workspace, "_remote", fake_remote)

    workspace.send_prompt("agent", "fix the test")

    assert calls == [
        ("send-text", ("--match", "id:42", "--stdin"), "fix the test"),
        ("send-key", ("--match", "id:42", "enter"), None),
    ]


def test_workspace_shutdown_uses_dedicated_kitty_quit_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()

    class Config:
        data_dir = tmp_path / "data"
        allowed_dirs = [tmp_path.resolve()]

    workspace = KittyAgentWorkspace(Config(), default_project=project)
    tree = [
        {
            "id": 1,
            "tabs": [
                {
                    "id": 2,
                    "windows": [
                        {"id": 3, "user_vars": {"ASTRA_INSTANCE": "agent-1"}}
                    ],
                }
            ],
        }
    ]
    calls: list[tuple[str, tuple[str, ...]]] = []
    monkeypatch.setattr(workspace, "_tree", lambda: tree)

    def fake_remote(command: str, *args: str, **_kwargs):
        calls.append((command, args))
        return CompletedProcess([], 0, "", "")

    monkeypatch.setattr(workspace, "_remote", fake_remote)

    assert workspace.shutdown() == {
        "shutdown": True,
        "closed_instances": 1,
        "already_stopped": False,
    }
    assert calls == [("action", ("--match", "all", "quit"))]


def test_workspace_tree_treats_unresponsive_socket_as_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()

    class Config:
        data_dir = tmp_path / "data"
        allowed_dirs = [tmp_path.resolve()]

    workspace = KittyAgentWorkspace(Config(), default_project=project)
    monkeypatch.setattr(workspace, "_kitty", "/opt/homebrew/bin/kitty")

    def timeout(*_args, **_kwargs):
        raise AgentWorkspaceError("kitty_timeout", "Kitty did not answer")

    monkeypatch.setattr(workspace, "_remote", timeout)

    assert workspace._tree() is None


def test_workspace_rotates_and_shares_unique_socket_pointer(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    class Config:
        data_dir = tmp_path / "data"
        allowed_dirs = [tmp_path.resolve()]

    first = KittyAgentWorkspace(Config(), default_project=project)
    legacy = first._socket_path
    first._rotate_socket()

    assert first._socket_path != legacy
    assert first._socket_path.name.startswith("agent-workspace-")
    assert first._socket_path.suffix == ".sock"

    second = KittyAgentWorkspace(Config(), default_project=project)

    assert second._socket_path == first._socket_path
