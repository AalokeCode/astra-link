"""Filesystem sandbox (spec §17, §30).

Every path a tool touches — read, write, cwd for a shell command — must pass
through `resolve_read_path` or `resolve_write_path` before it is used. Both
funnel into the same two checks: containment inside `cfg.allowed_dirs`, and
the sensitive-file denylist. Neither check is optional and neither can be
bypassed by a tool-level argument.

The critical ordering is: expand `~`, THEN `Path.resolve()`, THEN compare.
Resolving before comparing is what defeats both `../` traversal (the `..`
segments collapse during resolution) and a symlink planted inside an allowed
directory that points outside it (resolution follows the symlink to its real
target before the containment check ever runs).
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.config import Config, load_config
from app.tools.registry import ToolExecutionError

log = logging.getLogger(__name__)

# -- sensitive-file denylist (spec §17) --------------------------------------
#
# Blocked regardless of where they sit inside the sandbox. This list is
# deliberately name/pattern based rather than content based — we never want
# to read a candidate file just to decide whether it's safe to read.

_ENV_FILE_PREFIX = ".env"

_SENSITIVE_EXACT_NAMES = {
    "id_rsa", "id_rsa.pub",
    "id_ed25519", "id_ed25519.pub",
    "id_dsa", "id_dsa.pub",
    ".netrc",
    ".npmrc",
}

_SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".keychain")

_SENSITIVE_STEMS = {"credentials", "secrets"}

_SENSITIVE_DIR_NAMES = {".ssh", ".aws", ".gnupg"}


def is_sensitive(path: Path) -> bool:
    """True if `path` matches the sensitive-file denylist, by name alone."""
    name = path.name
    lower_name = name.lower()

    if lower_name == _ENV_FILE_PREFIX or lower_name.startswith(_ENV_FILE_PREFIX + "."):
        return True

    if lower_name in _SENSITIVE_EXACT_NAMES:
        return True

    if lower_name.endswith(_SENSITIVE_SUFFIXES):
        return True

    if path.stem.lower() in _SENSITIVE_STEMS:
        return True

    if _SENSITIVE_DIR_NAMES.intersection(path.parts):
        return True

    return False


# -- containment ---------------------------------------------------------


def is_within_allowed(path: Path, roots: list[Path]) -> bool:
    """Path-component containment check — never string-prefix matching.

    `Path.is_relative_to` compares path parts, so a root of
    `/Users/x/Documents/Projects` does not match `/Users/x/Documents/Projects-evil`
    the way a naive `str.startswith` would.
    """
    return any(path == root or path.is_relative_to(root) for root in roots)


def _roots(cfg: Config | None) -> list[Path]:
    return (cfg or load_config()).allowed_dirs


def _denied(raw: Path, roots: list[Path]) -> ToolExecutionError:
    if not roots:
        return ToolExecutionError(
            "no allowed directories are configured; all filesystem access is denied"
        )
    allowed_str = ", ".join(str(r) for r in roots)
    return ToolExecutionError(
        f"'{raw}' is outside the allowed directories (allowed: {allowed_str})"
    )


def _resolve_nearest_existing(raw: Path) -> Path:
    """Resolve `raw` (which may not exist yet) through its real ancestor.

    A non-existent path can't be resolved directly. We walk up to the nearest
    ancestor that does exist, resolve THAT (following any symlinks), and
    reattach the non-existent tail. This is what makes a write to
    `<allowed_root>/new/nested/file.txt` safe even though nothing under
    `new/` exists yet — the containment check still runs against a real,
    symlink-resolved path.
    """
    if raw.exists():
        return raw.resolve(strict=True)

    tail: list[str] = []
    ancestor = raw
    while not ancestor.exists():
        tail.append(ancestor.name)
        parent = ancestor.parent
        if parent == ancestor:
            raise ToolExecutionError(f"no existing ancestor directory found for: {raw}")
        ancestor = parent

    resolved = ancestor.resolve(strict=True)
    for name in reversed(tail):
        resolved = resolved / name
    return resolved


def resolve_read_path(path: str | Path, *, cfg: Config | None = None) -> Path:
    """Resolve a path for reading. The path must already exist."""
    roots = _roots(cfg)
    raw = Path(path).expanduser()

    try:
        resolved = raw.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ToolExecutionError(f"path does not exist: {raw}") from exc

    if not is_within_allowed(resolved, roots):
        raise _denied(raw, roots)

    if is_sensitive(resolved):
        raise ToolExecutionError(
            f"'{resolved.name}' is a sensitive file type and is blocked for security reasons"
        )

    return resolved


def resolve_write_path(
    path: str | Path, *, must_exist: bool = False, cfg: Config | None = None
) -> Path:
    """Resolve a path for writing (or as a cwd/existing-target lookup).

    When `must_exist` is False (the default), the target itself is allowed to
    not exist yet — only its nearest existing ancestor is resolved and
    checked, since that's the earliest point a symlink attack could occur.
    """
    roots = _roots(cfg)
    raw = Path(path).expanduser()

    if must_exist:
        try:
            resolved = raw.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ToolExecutionError(f"path does not exist: {raw}") from exc
    else:
        resolved = _resolve_nearest_existing(raw)

    if not is_within_allowed(resolved, roots):
        raise _denied(raw, roots)

    if is_sensitive(resolved):
        raise ToolExecutionError(
            f"'{resolved.name}' is a sensitive file type and is blocked for security reasons"
        )

    return resolved
