"""Permission enforcement (spec §10, §31).

The design constraint that shapes this file: **the model has no input here.**
The broker's only inputs are the tool's registered risk level, the operator's
config, and — for HIGH_RISK — an answer that comes from the interface layer
(a human at a CLI prompt or a microphone), never from the conversation.

There is deliberately no `skip_confirmation` argument, no override flag a tool
can set, and no way for text the model emits to reach this decision. A model
that says "no confirmation needed" is just producing tokens; nothing reads them.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum

from app.tools.registry import RiskLevel, Tool

log = logging.getLogger(__name__)


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True)
class ConfirmationRequest:
    """Handed to the interface when a HIGH_RISK tool needs a human answer."""

    tool_name: str
    summary: str
    risk: RiskLevel

    def prompt(self) -> str:
        return f"{self.summary}\n   Allow this? [y/N] "


@dataclass(frozen=True)
class Authorization:
    decision: Decision
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW


# The interface supplies this: given a request, return True to proceed.
ConfirmHandler = Callable[[ConfirmationRequest], Awaitable[bool]]


async def deny_all(_: ConfirmationRequest) -> bool:
    """Default handler for non-interactive contexts.

    When nobody can be asked, the answer is no. A background service must not
    silently approve destructive work because there was no terminal attached.
    """
    return False


class PermissionBroker:
    def __init__(
        self,
        *,
        require_confirmation: bool = True,
        confirm_handler: ConfirmHandler | None = None,
        disabled_groups: frozenset[str] = frozenset(),
    ) -> None:
        self._require_confirmation = require_confirmation
        self._confirm: ConfirmHandler = confirm_handler or deny_all
        self._disabled_groups = disabled_groups
        # Remembers per-session grants so the assistant doesn't re-ask for the
        # same operation mid-task. Scoped to one process; never persisted.
        self._session_grants: set[str] = set()

    def set_confirm_handler(self, handler: ConfirmHandler) -> None:
        self._confirm = handler

    def reset_session_grants(self) -> None:
        self._session_grants.clear()

    async def authorize(self, tool: Tool, summary: str) -> Authorization:
        if tool.group.value in self._disabled_groups:
            return Authorization(
                Decision.DENY, f"{tool.group.value} tools are disabled in configuration"
            )

        if tool.risk is RiskLevel.READ_ONLY:
            return Authorization(Decision.ALLOW, "read-only")

        if tool.risk is RiskLevel.LOW_RISK_WRITE:
            # Arguments were already schema-validated and any path they carry
            # was sandbox-checked before reaching here (spec §10).
            return Authorization(Decision.ALLOW, "low-risk write, arguments validated")

        # HIGH_RISK from here down.
        if not self._require_confirmation:
            # Operator-level opt-out, set in .env by a human. Loud on purpose.
            log.warning("confirmation disabled by config; auto-allowing %s", tool.name)
            return Authorization(Decision.ALLOW, "confirmation disabled in configuration")

        if tool.name in self._session_grants:
            return Authorization(Decision.ALLOW, "granted earlier this session")

        request = ConfirmationRequest(tool_name=tool.name, summary=summary, risk=tool.risk)
        try:
            approved = await self._confirm(request)
        except Exception as exc:
            # A broken prompt handler must fail closed.
            log.error("confirmation handler failed for %s: %s", tool.name, exc)
            return Authorization(Decision.DENY, "confirmation could not be obtained")

        if approved:
            log.info("user approved %s", tool.name)
            return Authorization(Decision.ALLOW, "confirmed by user")

        log.info("user declined %s", tool.name)
        return Authorization(Decision.DENY, "declined by user")

    def grant_for_session(self, tool_name: str) -> None:
        """Remember a 'yes, and stop asking' answer for this process only."""
        self._session_grants.add(tool_name)
