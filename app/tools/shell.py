"""Shell tool (spec §11). The only path from the model to a subprocess.

`command` is `list[str]`, not a string, so there is never a shell string to
parse — the model must name an argv directly, and `validate_command` checks
every element of it before `subprocess.run` ever sees it.
"""

from __future__ import annotations

import logging
import subprocess
import time

from pydantic import BaseModel, Field

from app.config import load_config
from app.security.commands import sanitized_env, validate_command
from app.security.paths import resolve_read_path
from app.tools.registry import RiskLevel, ToolExecutionError, ToolGroup, registry

log = logging.getLogger(__name__)

_MAX_OUTPUT_BYTES = 20_000


class RunCommandArgs(BaseModel):
    command: list[str] = Field(
        description="Argv to execute, e.g. ['git', 'status']. Not a shell string — no pipes or redirection."
    )
    cwd: str | None = Field(
        default=None,
        description="Working directory, must be within an allowed directory. Defaults to the first allowed root.",
    )
    timeout: int = Field(
        default=30, ge=1, le=600, description="Timeout in seconds; clamped to the configured shell timeout."
    )


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    data = text.encode("utf-8", errors="replace")
    if len(data) <= limit:
        return text, False
    clipped = data[:limit].decode("utf-8", errors="ignore")
    return clipped + f"\n...[truncated, {len(data) - limit} more bytes]", True


@registry.tool(
    name="run_command",
    description=(
        "Run an allowlisted development command (git, npm, python, ls, grep, ...) with no "
        "shell interpretation. Always requires human confirmation."
    ),
    args_model=RunCommandArgs,
    risk=RiskLevel.HIGH_RISK,
    group=ToolGroup.SHELL,
    confirm_template="Run `{command}`?",
)
def run_command(args: RunCommandArgs) -> dict:
    cfg = load_config()
    argv = validate_command(args.command)

    if args.cwd is not None:
        cwd = resolve_read_path(args.cwd)
        if not cwd.is_dir():
            raise ToolExecutionError(f"cwd is not a directory: {cwd}")
    elif cfg.allowed_dirs:
        cwd = cfg.allowed_dirs[0]
    else:
        raise ToolExecutionError("no allowed directories configured; cannot choose a working directory")

    timeout = min(args.timeout, cfg.shell_timeout_seconds)

    start = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            env=sanitized_env(),
            timeout=timeout,
            capture_output=True,
            text=True,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolExecutionError(f"command timed out after {timeout}s: {' '.join(argv)}") from exc
    except OSError as exc:
        raise ToolExecutionError(f"failed to execute command: {exc}") from exc

    duration_ms = int((time.monotonic() - start) * 1000)
    stdout, stdout_truncated = _truncate(proc.stdout, _MAX_OUTPUT_BYTES)
    stderr, stderr_truncated = _truncate(proc.stderr, _MAX_OUTPUT_BYTES)

    # A non-zero exit is a result the model needs to see and reason about,
    # not an exception — only validation failure or a timeout raises.
    return {
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": stdout_truncated or stderr_truncated,
        "duration_ms": duration_ms,
        "command": argv,
    }
