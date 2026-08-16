"""Shell command allowlist (spec §11, §30).

`run_command` never sets `shell=True` — argv goes straight to `subprocess.run`
as a list. That already rules out most injection, but the allowlist and the
metacharacter check make the intent explicit and stop an argument like
`"; rm -rf ~"` from ever being handed to a real shell downstream (e.g. a
script the model asks `bash` to run, or a tool that shells out internally).
"""

from __future__ import annotations

import logging
import os
import re

from app.tools.registry import ToolExecutionError

log = logging.getLogger(__name__)

# -- allowlist -------------------------------------------------------------

ALLOWED_BINARIES = frozenset(
    {
        "git", "npm", "pnpm", "yarn", "python", "python3", "node", "bun", "pytest",
        "ls", "pwd", "find", "grep", "rg", "cat", "head", "tail", "wc", "which", "echo",
    }
)

# Hard-blocked even if a future config change accidentally adds one of these
# to the allowlist above — this check runs unconditionally.
HARD_BLOCKED_BINARIES = frozenset(
    {
        "rm", "sudo", "su", "diskutil", "shutdown", "reboot", "security", "launchctl",
        "chmod", "chown", "dd", "mkfs", "kill", "killall", "pkill", "curl", "wget", "ssh",
        "scp", "nc",
    }
)

_SHELL_METACHARACTERS = frozenset(";|&$`<>\n")


def validate_command(argv: list[str]) -> list[str]:
    """Validate an argv list and return it normalized. Raises on rejection."""
    if not argv:
        raise ToolExecutionError("command must not be empty")

    binary = argv[0]

    # A path separator means "resolve this binary from user input" — reject
    # outright so the allowlist below is the only way to name an executable.
    if "/" in binary or "\\" in binary:
        raise ToolExecutionError(
            f"command must name a bare binary, not a path: {binary!r}. "
            f"Allowed commands: {', '.join(sorted(ALLOWED_BINARIES))}"
        )

    if binary in HARD_BLOCKED_BINARIES:
        raise ToolExecutionError(f"'{binary}' is blocked for security reasons and cannot be run")

    if binary not in ALLOWED_BINARIES:
        raise ToolExecutionError(
            f"'{binary}' is not an allowed command. "
            f"Allowed commands: {', '.join(sorted(ALLOWED_BINARIES))}"
        )

    for arg in argv:
        bad = _SHELL_METACHARACTERS.intersection(arg)
        if bad:
            raise ToolExecutionError(
                f"argument {arg!r} contains shell metacharacter(s) {sorted(bad)!r}, which is "
                "not permitted — commands run without a shell, so this can only be an attempt "
                "to break out of the sandbox"
            )

    return list(argv)


# -- high-risk classification -----------------------------------------------

_HIGH_RISK_GIT_SUBCOMMANDS = frozenset({"push", "reset", "rebase", "clean"})
_INSTALL_VERBS = frozenset({"install", "add", "remove"})


def is_high_risk_command(argv: list[str]) -> bool:
    """Finer-grained risk classification within the (always-confirmed) shell tool.

    `run_command` is declared HIGH_RISK at the tool level regardless — every
    invocation gets a confirmation prompt (spec §31). This function exists so
    logging/UI can flag *which* commands are the ones actually doing
    something consequential, per the specific list in the contract.
    """
    if not argv:
        return False

    binary, rest = argv[0], argv[1:]

    if binary == "git":
        if rest and rest[0] in _HIGH_RISK_GIT_SUBCOMMANDS:
            return True
        if rest and rest[0] == "checkout" and "-f" in rest:
            return True
        return False

    if binary in {"npm", "pnpm", "yarn"}:
        return bool(rest) and rest[0] in _INSTALL_VERBS

    if binary in {"pip", "pip3"}:
        return bool(rest) and rest[0] == "install"

    if binary in {"python", "python3"}:
        # `python -m pip install ...`
        return "pip" in rest and "install" in rest

    return False


# -- environment sanitization ------------------------------------------------

_ENV_PASSTHROUGH_KEYS = ("PATH", "HOME", "LANG", "TERM", "TMPDIR")
_SECRET_KEY_PATTERN = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.IGNORECASE)


def sanitized_env() -> dict[str, str]:
    """A minimal child-process environment that cannot leak our API keys.

    Built from an explicit allowlist (never a copy-then-strip of the full
    parent environment), then filtered again against the secret-key pattern
    as a second line of defense in case that allowlist ever grows.
    """
    env = {
        key: os.environ[key]
        for key in _ENV_PASSTHROUGH_KEYS
        if key in os.environ
    }
    return {k: v for k, v in env.items() if not _SECRET_KEY_PATTERN.search(k)}
