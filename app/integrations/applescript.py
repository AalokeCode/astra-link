"""Safe, bounded AppleScript execution for macOS integrations."""

from __future__ import annotations

import subprocess

from app.tools.registry import ToolExecutionError


def escape_applescript_string(value: str) -> str:
    """Escape a Python string for embedding in an AppleScript literal."""
    clean = "".join(character for character in value if ord(character) >= 32 and ord(character) != 127)
    return clean.replace("\\", "\\\\").replace('"', '\\"')


def run_applescript(script: str, *, timeout: float = 20.0) -> str:
    """Execute AppleScript via osascript. Returns stdout, stripped."""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        seconds = f"{timeout:g}"
        raise ToolExecutionError(
            f"Reminders did not respond within {seconds}s — the app may be showing a permission dialog."
        ) from exc
    except OSError as exc:
        raise ToolExecutionError(f"could not execute AppleScript: {exc}") from exc

    if result.returncode != 0:
        error = result.stderr.strip() or result.stdout.strip() or "unknown AppleScript error"
        lowered = error.lower()
        if "-1743" in error or "not authorized" in lowered or "not authorised" in lowered:
            raise ToolExecutionError(
                "macOS denied access to Reminders. Grant permission in System Settings → "
                "Privacy & Security → Automation (or Reminders), then try again."
            )
        if "-1728" in error:
            raise ToolExecutionError("The requested Reminders list or reminder was not found.")
        raise ToolExecutionError(f"Reminders AppleScript failed: {error}")
    return result.stdout.strip()
