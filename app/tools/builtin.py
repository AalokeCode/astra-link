"""Core tools that don't depend on any other agent's module.

These exist so the agent loop can be exercised end-to-end (spec §6, §21)
without waiting on filesystem/git/reminders/etc, and to implement spec §18's
"user preferences" slice of memory.
"""

from __future__ import annotations

import logging
from datetime import datetime

from pydantic import BaseModel, Field

from app.config import load_config
from app.memory.database import Database
from app.tools.registry import RiskLevel, ToolExecutionError, ToolGroup, registry

log = logging.getLogger(__name__)

# Lazily constructed so importing this module never touches disk or .env —
# a plain tool-registration import must stay side-effect free. sqlite's WAL
# mode makes a second connection to the same file (main.py opens its own)
# safe to hold concurrently.
_db: Database | None = None


def _database() -> Database:
    global _db
    if _db is None:
        cfg = load_config()
        _db = Database(cfg.db_path)
    return _db


class GetCurrentTimeArgs(BaseModel):
    """No parameters — the tool always reports the operator's local time."""


@registry.tool(
    name="get_current_time",
    description=(
        "Get the current date and time in the user's configured local timezone. "
        "Use this before reasoning about relative dates like 'tomorrow' or 'in an hour'."
    ),
    args_model=GetCurrentTimeArgs,
    risk=RiskLevel.READ_ONLY,
    group=ToolGroup.CORE,
)
def get_current_time(args: GetCurrentTimeArgs) -> dict:
    cfg = load_config()
    now = datetime.now(cfg.timezone)
    return {
        "iso": now.isoformat(),
        "human": now.strftime("%A, %B %d %Y %H:%M %Z"),
        "timezone": str(cfg.timezone),
    }


class RememberPreferenceArgs(BaseModel):
    key: str = Field(description="Short identifier for the preference, e.g. 'preferred_name'.")
    value: str = Field(description="The value to remember for this key.")


@registry.tool(
    name="remember_preference",
    description=(
        "Store a durable user preference (e.g. preferred name, coding style, timezone habits) "
        "so it is available in future conversations, not just this one."
    ),
    args_model=RememberPreferenceArgs,
    risk=RiskLevel.LOW_RISK_WRITE,
    group=ToolGroup.CORE,
)
def remember_preference(args: RememberPreferenceArgs) -> dict:
    key = args.key.strip()
    if not key:
        raise ToolExecutionError("preference key must not be empty")
    _database().set_preference(key, args.value)
    return {"stored": True, "key": key, "value": args.value}


class RecallPreferencesArgs(BaseModel):
    """No parameters — always returns the full preference set."""


@registry.tool(
    name="recall_preferences",
    description="List every remembered user preference, as key/value pairs.",
    args_model=RecallPreferencesArgs,
    risk=RiskLevel.READ_ONLY,
    group=ToolGroup.CORE,
)
def recall_preferences(args: RecallPreferencesArgs) -> dict:
    prefs = _database().all_preferences()
    return {"count": len(prefs), "preferences": prefs}
