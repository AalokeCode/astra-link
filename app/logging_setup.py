"""Structured logging setup (spec §33).

Three rotating files under `cfg.log_dir`:

    assistant.log   INFO and above — the general narrative log
    tool_calls.log  every tool invocation (spec §30), independent of the rest
    errors.log      ERROR and above from anywhere

Plus a console handler at WARNING (or DEBUG with `--debug`). A redaction
filter runs on every handler so a stray API key never lands on disk or in a
terminal, regardless of which logger emitted it.
"""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler

from app.config import Config

# Tool-call audit entries go to this logger specifically so they land in
# tool_calls.log without also duplicating into assistant.log.
TOOL_CALL_LOGGER = "assistant.tool_calls"

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

# Patterns for things that look like API keys: provider-prefixed tokens
# (Gemini's `AIza...`/`AQ....`, Groq's `gsk_...`) and generic long base64-ish runs that
# nothing legitimate in our log messages should ever contain.
_SECRET_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z_\-]{10,}"),
    re.compile(r"AQ\.[0-9A-Za-z_\-]{10,}"),
    re.compile(r"gsk_[0-9A-Za-z]{10,}"),
    re.compile(r"sk-[0-9A-Za-z_\-]{10,}"),
    re.compile(r"\b[A-Za-z0-9+/_\-]{32,}={0,2}\b"),
]


class _RedactFilter(logging.Filter):
    """Strips anything resembling a secret out of the formatted message.

    Runs on `record.msg`/`record.args` before formatting, not on the already
    rendered string, so it applies uniformly across every handler that shares
    this filter instance.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            return True
        redacted = rendered
        for pattern in _SECRET_PATTERNS:
            redacted = pattern.sub("***REDACTED***", redacted)
        if redacted != rendered:
            record.msg = redacted
            record.args = ()
        return True


def _rotating_handler(path, level: int, cfg: Config) -> RotatingFileHandler:
    handler = RotatingFileHandler(
        path,
        maxBytes=cfg.log_max_bytes,
        backupCount=cfg.log_backup_count,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    handler.addFilter(_RedactFilter())
    return handler


def configure_logging(cfg: Config) -> None:
    """Wire up the three rotating files plus a console handler.

    Idempotent — safe to call more than once (e.g. a test harness or a REPL
    restart within the same process) because it clears handlers it owns
    before re-adding them instead of stacking duplicates.
    """
    log_dir = cfg.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    root.addHandler(_rotating_handler(log_dir / "assistant.log", logging.INFO, cfg))
    root.addHandler(_rotating_handler(log_dir / "errors.log", logging.ERROR, cfg))

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if cfg.debug else logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    console.addFilter(_RedactFilter())
    root.addHandler(console)

    tool_logger = logging.getLogger(TOOL_CALL_LOGGER)
    tool_logger.setLevel(logging.INFO)
    tool_logger.propagate = False  # keep the audit trail out of assistant.log
    for handler in list(tool_logger.handlers):
        tool_logger.removeHandler(handler)
    tool_logger.addHandler(_rotating_handler(log_dir / "tool_calls.log", logging.INFO, cfg))
