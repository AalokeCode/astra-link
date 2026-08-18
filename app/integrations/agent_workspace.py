"""Visible Claude Code and Codex sessions hosted in a dedicated Kitty window.

Kitty remains the source of truth.  Each managed terminal carries ASTRA user
variables, so sessions survive a gateway restart without a second state store.
All remote control travels over a user-only Unix socket; no TCP listener or
shell interpolation is involved.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from app.config import Config
from app.security.paths import resolve_write_path

AgentProvider = Literal["claude", "codex"]

_MAX_TERMINAL_CHARS = 16_000
_START_TIMEOUT_SECONDS = 6.0
_WORKSPACE_LAUNCH_LOCK = threading.RLock()
_ASTRA_CONTEXT = (
    "You are running as a coding partner inside Aaloke's ASTRA workspace. "
    "His name is pronounced 'Ahlok'. "
    "Work only in the current project, explain blockers precisely, preserve "
    "unrelated user changes, and ask for approval when an action requires it."
)


class AgentWorkspaceError(RuntimeError):
    """Expected, user-actionable workspace failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        action: str | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.action = action
        self.detail = detail

    def as_dict(self) -> dict[str, str]:
        result = {"code": self.code, "message": self.message}
        if self.action:
            result["action"] = self.action
        if self.detail:
            result["detail"] = self.detail
        return result


@dataclass(frozen=True)
class TerminalDiagnosis:
    code: str
    summary: str
    action: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "summary": self.summary, "action": self.action}


_DIAGNOSES: tuple[tuple[re.Pattern[str], TerminalDiagnosis], ...] = (
    (
        re.compile(r"rate.?limit|quota|usage limit|too many requests|\b429\b", re.I),
        TerminalDiagnosis(
            "quota",
            "The provider rejected the request because its usage limit was reached.",
            "Wait for the limit to reset, switch model/account, or reduce parallel work.",
        ),
    ),
    (
        re.compile(r"unauthorized|authentication failed|not logged in|invalid.*(?:key|token)", re.I),
        TerminalDiagnosis(
            "authentication",
            "The agent could not authenticate with its provider.",
            "Focus the Kitty tab and complete the provider login or refresh its credentials.",
        ),
    ),
    (
        re.compile(r"permission denied|operation not permitted|approval required", re.I),
        TerminalDiagnosis(
            "permission",
            "The requested operation was blocked by an OS or agent permission boundary.",
            "Review the request in Kitty and approve it only if the target and command are safe.",
        ),
    ),
    (
        re.compile(r"mcp.{0,80}(?:failed|error|unavailable|disconnect)|(?:failed|error).{0,80}mcp", re.I | re.S),
        TerminalDiagnosis(
            "mcp",
            "An MCP server failed to start or disconnected.",
            "Inspect the named MCP server configuration and restart this agent after fixing it.",
        ),
    ),
    (
        re.compile(r"network.{0,40}(?:error|unreachable)|econnreset|enotfound|timed out", re.I),
        TerminalDiagnosis(
            "network",
            "The agent lost its network path or a remote request timed out.",
            "Check connectivity, then retry the prompt; the project files remain local.",
        ),
    ),
    (
        re.compile(r"command not found|no such file or directory", re.I),
        TerminalDiagnosis(
            "dependency",
            "A required executable or file was not found.",
            "Check the last terminal lines for the missing name, then install or correct that dependency.",
        ),
    ),
)

_ATTENTION_PATTERN = re.compile(
    r"(?:allow|approve|confirm|proceed|continue).{0,60}(?:\?|\[y/n\]|\[Y/n\])|"
    r"(?:\[y/n\]|\[Y/n\]).{0,60}(?:allow|approve|confirm|proceed|continue)|"
    r"press enter to continue|waiting for (?:your )?(?:input|approval)",
    re.I,
)


def diagnose_terminal(text: str) -> TerminalDiagnosis | None:
    """Return a conservative explanation for recognizable terminal failures."""
    tail = text[-_MAX_TERMINAL_CHARS:]
    for pattern, diagnosis in _DIAGNOSES:
        if pattern.search(tail):
            return diagnosis
    return None


def terminal_state(text: str, diagnosis: TerminalDiagnosis | None) -> str:
    if diagnosis is not None:
        return "error"
    if _ATTENTION_PATTERN.search(text[-4_000:]):
        return "needs_input"
    return "running"


class KittyAgentWorkspace:
    """Service boundary around Kitty remote control and coding-agent CLIs."""

    def __init__(self, cfg: Config, *, default_project: Path) -> None:
        self._cfg = cfg
        self._default_project = default_project.resolve()
        self._kitty = shutil.which("kitty")
        self._binaries = {
            "claude": shutil.which("claude"),
            "codex": shutil.which("codex"),
        }
        run_dir = cfg.data_dir / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        try:
            run_dir.chmod(0o700)
        except OSError:
            pass
        self._run_dir = run_dir
        self._socket_pointer = run_dir / "agent-workspace.socket"
        self._socket_path = run_dir / "agent-workspace.sock"
        self._socket_address = f"unix:{self._socket_path}"
        self._sync_socket_pointer()

    def _read_socket_pointer(self) -> Path | None:
        try:
            raw = self._socket_pointer.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not raw:
            return None
        candidate = Path(raw)
        try:
            parent = candidate.parent.resolve(strict=True)
        except OSError:
            return None
        if parent != self._run_dir.resolve() or not candidate.name.startswith(
            "agent-workspace-"
        ) or candidate.suffix != ".sock":
            return None
        return candidate

    def _sync_socket_pointer(self) -> None:
        candidate = self._read_socket_pointer()
        if candidate is not None and candidate != self._socket_path:
            self._socket_path = candidate
            self._socket_address = f"unix:{candidate}"

    def _rotate_socket(self) -> None:
        """Select a unique socket path and publish it atomically.

        Reusing a stale path is unsafe: an old Kitty process can exit later and
        unlink a newer process's socket with the same pathname.
        """
        candidate = self._run_dir / f"agent-workspace-{uuid.uuid4().hex[:12]}.sock"
        temporary = self._socket_pointer.with_suffix(".tmp")
        temporary.write_text(str(candidate), encoding="utf-8")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(self._socket_pointer)
        self._socket_path = candidate
        self._socket_address = f"unix:{candidate}"

    def _remote(
        self,
        command: str,
        *args: str,
        input_text: str | None = None,
        timeout: float = 8.0,
    ) -> subprocess.CompletedProcess[str]:
        if not self._kitty:
            raise AgentWorkspaceError(
                "kitty_missing",
                "Kitty is not installed or is not on PATH.",
                action="Install Kitty, then restart the ASTRA gateway.",
            )
        argv = [self._kitty, "@", "--to", self._socket_address, command, *args]
        try:
            return subprocess.run(
                argv,
                input=input_text,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AgentWorkspaceError(
                "kitty_timeout",
                f"Kitty did not answer the {command!r} command in time.",
                action="Check whether the ASTRA Agent Workspace window is responsive.",
            ) from exc
        except OSError as exc:
            raise AgentWorkspaceError(
                "kitty_unavailable",
                "ASTRA could not contact Kitty.",
                action="Restart the ASTRA Agent Workspace from the web app.",
                detail=str(exc),
            ) from exc

    def _tree(self) -> list[dict[str, Any]] | None:
        if not self._kitty:
            return None
        self._sync_socket_pointer()
        try:
            result = self._remote("ls", timeout=3.0)
        except AgentWorkspaceError:
            # A stale Unix socket can still accept a connection while its
            # Kitty process no longer answers. Treat that exactly like an
            # offline workspace so launch can unlink it and recover.
            return None
        if result.returncode != 0:
            return None
        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, list) else None

    def _start_kitty(self, project: Path) -> list[dict[str, Any]]:
        if not self._kitty:
            self._remote("ls")  # Raises the canonical missing-Kitty error.
        self._rotate_socket()
        argv = [
            self._kitty or "kitty",
            "--listen-on",
            self._socket_address,
            "--override",
            "allow_remote_control=socket-only",
            "--title",
            "ASTRA Agent Workspace",
            "--directory",
            str(project),
        ]
        try:
            subprocess.Popen(  # noqa: S603 - fixed executable and argv-only invocation
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise AgentWorkspaceError(
                "kitty_launch_failed",
                "Kitty could not open the ASTRA Agent Workspace window.",
                action="Launch Kitty once manually and confirm macOS allows it to open.",
                detail=str(exc),
            ) from exc

        deadline = time.monotonic() + _START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            time.sleep(0.1)
            tree = self._tree()
            if tree is not None:
                return tree
        raise AgentWorkspaceError(
            "kitty_start_timeout",
            "Kitty opened but its private control socket did not become ready.",
            action="Close the ASTRA Agent Workspace window and try again.",
        )

    def _resolve_project(self, project_path: str) -> Path:
        try:
            project = resolve_write_path(project_path, must_exist=True, cfg=self._cfg)
        except Exception as exc:
            raise AgentWorkspaceError(
                "project_denied",
                str(exc),
                action="Choose an existing directory under ALLOWED_DIRS.",
            ) from exc
        if not project.is_dir():
            raise AgentWorkspaceError(
                "project_not_directory",
                f"The project path is not a directory: {project}",
                action="Choose the project folder rather than a file.",
            )
        return project

    @staticmethod
    def _all_windows(tree: list[dict[str, Any]]) -> list[dict[str, Any]]:
        windows: list[dict[str, Any]] = []
        for os_window in tree:
            for tab in os_window.get("tabs", []):
                for window in tab.get("windows", []):
                    item = dict(window)
                    item["tab_id"] = tab.get("id")
                    item["tab_title"] = tab.get("title")
                    item["os_window_id"] = os_window.get("id")
                    windows.append(item)
        return windows

    def _agent_windows(self, tree: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            window
            for window in self._all_windows(tree)
            if isinstance(window.get("user_vars"), dict)
            and window["user_vars"].get("ASTRA_INSTANCE")
        ]

    def _get_window(self, instance_id: str) -> dict[str, Any]:
        tree = self._tree()
        if tree is None:
            raise AgentWorkspaceError(
                "workspace_offline",
                "The ASTRA Agent Workspace is not running.",
                action="Launch a Claude Code or Codex instance first.",
            )
        for window in self._agent_windows(tree):
            if window["user_vars"].get("ASTRA_INSTANCE") == instance_id:
                return window
        raise AgentWorkspaceError(
            "instance_not_found",
            "That agent tab is no longer open in Kitty.",
            action="Refresh the workspace and launch a new instance if needed.",
        )

    def _terminal_text(self, window_id: int) -> str:
        result = self._remote(
            "get-text", "--match", f"id:{window_id}", "--extent", "screen", timeout=5.0
        )
        if result.returncode != 0:
            return ""
        return result.stdout[-_MAX_TERMINAL_CHARS:]

    def capabilities(self) -> dict[str, Any]:
        tree = self._tree()
        return {
            "kitty": {"available": bool(self._kitty), "connected": tree is not None},
            "providers": {
                name: {"available": bool(path)} for name, path in self._binaries.items()
            },
            "default_project": str(self._default_project),
            "mcp": "inherited from each agent's native local configuration",
        }

    def list_instances(self, *, include_terminal: bool = True) -> list[dict[str, Any]]:
        tree = self._tree()
        if tree is None:
            return []
        instances: list[dict[str, Any]] = []
        for window in self._agent_windows(tree):
            variables = window["user_vars"]
            text = self._terminal_text(window["id"]) if include_terminal else ""
            diagnosis = diagnose_terminal(text)
            instances.append(
                {
                    "id": variables["ASTRA_INSTANCE"],
                    "provider": variables.get("ASTRA_PROVIDER", "unknown"),
                    "project_path": variables.get("ASTRA_PROJECT", window.get("cwd", "")),
                    "window_id": window["id"],
                    "tab_id": window.get("tab_id"),
                    "title": window.get("tab_title") or window.get("title") or "Agent",
                    "state": terminal_state(text, diagnosis),
                    "terminal": text,
                    "diagnosis": diagnosis.as_dict() if diagnosis else None,
                    "focused": bool(window.get("is_focused")),
                }
            )
        return instances

    def launch(
        self,
        provider: str,
        project_path: str,
        *,
        initial_prompt: str | None = None,
    ) -> dict[str, Any]:
        # Web and voice can request a launch at the same moment. Serialize the
        # socket existence check and first Kitty startup across service objects.
        with _WORKSPACE_LAUNCH_LOCK:
            return self._launch(provider, project_path, initial_prompt=initial_prompt)

    def _launch(
        self,
        provider: str,
        project_path: str,
        *,
        initial_prompt: str | None = None,
    ) -> dict[str, Any]:
        if provider not in self._binaries:
            raise AgentWorkspaceError(
                "provider_invalid",
                f"Unsupported coding agent: {provider}",
                action="Choose Claude Code or Codex.",
            )
        binary = self._binaries[provider]
        if not binary:
            raise AgentWorkspaceError(
                "provider_missing",
                f"{provider.title()} is not installed or is not on PATH.",
                action=f"Install the {provider} CLI, then restart the gateway.",
            )
        project = self._resolve_project(project_path)
        prompt = (initial_prompt or "").strip()
        if len(prompt) > 20_000:
            raise AgentWorkspaceError(
                "prompt_too_large",
                "The prompt is larger than ASTRA's 20,000 character safety limit.",
                action="Put large context in a project file and reference its path instead.",
            )
        tree = self._tree()
        started_fresh = not tree
        if tree is None:
            tree = self._start_kitty(project)
        launch_type = "os-window" if not tree else "tab"

        instance_id = uuid.uuid4().hex
        short_project = project.name or "project"
        title = f"{provider.title()} · {short_project}"
        command = (
            [
                binary,
                "--name",
                f"ASTRA {short_project}",
                "--permission-mode",
                "manual",
                "--append-system-prompt",
                _ASTRA_CONTEXT,
            ]
            if provider == "claude"
            else [
                binary,
                "--cd",
                str(project),
                "--sandbox",
                "workspace-write",
                "--ask-for-approval",
                "on-request",
                "--no-alt-screen",
            ]
        )
        if prompt:
            command.append(prompt)
        result = self._remote(
            "launch",
            "--type",
            launch_type,
            "--tab-title",
            title,
            "--cwd",
            str(project),
            "--var",
            f"ASTRA_INSTANCE={instance_id}",
            "--var",
            f"ASTRA_PROVIDER={provider}",
            "--var",
            f"ASTRA_PROJECT={project}",
            "--hold",
            *command,
            timeout=8.0,
        )
        if result.returncode != 0 or not result.stdout.strip().isdigit():
            raise AgentWorkspaceError(
                "agent_launch_failed",
                f"Kitty could not launch the {provider.title()} tab.",
                action="Check the ASTRA gateway log and verify the CLI starts normally in Kitty.",
                detail=(result.stderr or result.stdout).strip()[:500] or None,
            )

        window_id = int(result.stdout.strip())
        if started_fresh:
            for initial in self._all_windows(tree):
                if initial.get("id") != window_id:
                    self._remote(
                        "close-window",
                        "--match",
                        f"id:{initial['id']}",
                        "--ignore-no-match",
                    )
        return {
            "id": instance_id,
            "provider": provider,
            "project_path": str(project),
            "window_id": window_id,
            "title": title,
            "state": "running",
            "terminal": "",
            "diagnosis": None,
            "focused": True,
        }

    def send_prompt(self, instance_id: str, text: str, *, kind: str = "prompt") -> None:
        prompt = text.strip()
        if not prompt:
            raise AgentWorkspaceError("prompt_empty", "Enter a prompt before sending it.")
        if len(prompt) > 20_000:
            raise AgentWorkspaceError(
                "prompt_too_large",
                "The prompt is larger than ASTRA's 20,000 character safety limit.",
                action="Put large context in a project file and reference its path instead.",
            )
        if kind not in {"prompt", "steer"}:
            raise AgentWorkspaceError(
                "prompt_mode_invalid",
                "Message mode must be prompt or steer.",
            )
        window = self._get_window(instance_id)
        if kind == "steer":
            prompt = f"Steering update — adjust the current approach:\n\n{prompt}"
        result = self._remote(
            "send-text",
            "--match",
            f"id:{window['id']}",
            "--stdin",
            input_text=prompt,
        )
        if result.returncode != 0:
            raise AgentWorkspaceError(
                "prompt_send_failed",
                "Kitty did not accept the prompt.",
                action="Focus the agent tab and retry from the terminal.",
                detail=result.stderr.strip()[:500] or None,
            )
        # A newline sent as text is interpreted by modern agent TUIs as a
        # multiline/Shift+Enter edit. Emit an actual key event to submit.
        submit = self._remote(
            "send-key",
            "--match",
            f"id:{window['id']}",
            "enter",
        )
        if submit.returncode != 0:
            raise AgentWorkspaceError(
                "prompt_submit_failed",
                "The prompt reached Kitty, but the Enter key event failed.",
                action="Focus the agent tab and press Enter once.",
                detail=submit.stderr.strip()[:500] or None,
            )

    def focus(self, instance_id: str) -> None:
        window = self._get_window(instance_id)
        result = self._remote("focus-window", "--match", f"id:{window['id']}")
        if result.returncode != 0:
            raise AgentWorkspaceError("focus_failed", "Kitty could not focus that agent tab.")

    def interrupt(self, instance_id: str) -> None:
        window = self._get_window(instance_id)
        result = self._remote(
            "signal-child", "--match", f"id:{window['id']}", "SIGINT"
        )
        if result.returncode != 0:
            raise AgentWorkspaceError(
                "interrupt_failed", "Kitty could not interrupt that agent process."
            )

    def close(self, instance_id: str) -> None:
        window = self._get_window(instance_id)
        result = self._remote(
            "close-window",
            "--match",
            f"id:{window['id']}",
            "--ignore-no-match",
        )
        if result.returncode != 0:
            raise AgentWorkspaceError("close_failed", "Kitty could not close that agent tab.")

    def shutdown(self) -> dict[str, Any]:
        """Quit only ASTRA's dedicated Kitty process and all of its tabs."""
        tree = self._tree()
        if tree is None:
            return {"shutdown": True, "closed_instances": 0, "already_stopped": True}
        closed_instances = len(self._agent_windows(tree))
        if not tree:
            # An empty Kitty control process has no active window on which to
            # dispatch the quit action. It is already functionally stopped.
            return {
                "shutdown": True,
                "closed_instances": 0,
                "already_stopped": True,
            }
        result = self._remote("action", "--match", "all", "quit")
        if result.returncode != 0:
            raise AgentWorkspaceError(
                "workspace_shutdown_failed",
                "Kitty could not shut down the ASTRA Agent Workspace.",
                action="Focus the ASTRA Agent Workspace and close its macOS window manually.",
                detail=result.stderr.strip()[:500] or None,
            )
        return {
            "shutdown": True,
            "closed_instances": closed_instances,
            "already_stopped": False,
        }


__all__ = [
    "AgentWorkspaceError",
    "KittyAgentWorkspace",
    "TerminalDiagnosis",
    "diagnose_terminal",
    "terminal_state",
]
