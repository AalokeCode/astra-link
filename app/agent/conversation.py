"""In-memory conversation state plus persistence (spec §18, §21).

Holds the `list[Message]` handed straight to `LLMProvider.chat`, and mirrors
every message into `Database` so history survives a restart.

The one rule that overrides everything else here: an assistant message
carrying `tool_calls` must never be separated from the tool-result messages
that answer it. Both providers reject a dangling tool result (Gemini pairs
`functionResponse` to the preceding `functionCall` by position; Groq's
`tool_call_id` must resolve to a call that's actually in the request). So
windowing trims at safe boundaries only, even if that means keeping slightly
more than `window` messages for a turn or two.
"""

from __future__ import annotations

import json
import logging

from app.llm.base import Message, Role, ToolCall, ToolResult
from app.memory.database import Database

log = logging.getLogger(__name__)

# Kept deliberately small. Tool results dominate request size — a single
# get_claude_code_status over 10 projects is several KB — and Groq's free tier
# rejects the whole request at 12k tokens per minute. 40 messages of history
# reliably exceeded that; 16 leaves room for the tool schemas and the answer.
DEFAULT_WINDOW = 16


class Conversation:
    def __init__(
        self,
        db: Database,
        *,
        source: str = "cli",
        window: int = DEFAULT_WINDOW,
        conversation_id: int | None = None,
    ) -> None:
        self._db = db
        self._source = source
        self._window = window
        self._messages: list[Message] = []
        self.conversation_id = conversation_id if conversation_id is not None else db.create_conversation(source)

    @property
    def messages(self) -> list[Message]:
        """A defensive copy — callers must not mutate conversation state directly."""
        return list(self._messages)

    # -- mutation -------------------------------------------------------

    def add_user(self, text: str) -> None:
        self._append(Message(role=Role.USER, content=text))
        self._db.add_message(self.conversation_id, "user", content=text)

    def add_assistant(self, text: str, tool_calls: list[ToolCall] | None = None) -> None:
        calls = tool_calls or []
        self._append(Message(role=Role.ASSISTANT, content=text, tool_calls=calls))
        self._db.add_message(
            self.conversation_id,
            "assistant",
            content=text,
            tool_calls=[{"id": c.id, "name": c.name, "arguments": c.arguments} for c in calls] or None,
        )

    def add_tool_result(self, result: ToolResult) -> None:
        self._append(Message(role=Role.TOOL, tool_result=result))
        self._db.add_message(
            self.conversation_id,
            "tool",
            content=result.name,
            tool_result={
                "call_id": result.call_id,
                "name": result.name,
                "ok": result.ok,
                "content": result.content,
            },
        )

    def add_system_note(self, text: str) -> None:
        """Inject a system-role note without persisting it as a real turn.

        Used internally for the windowing summary; exposed because the agent
        loop may also want to prepend one-off context notes.
        """
        self._append(Message(role=Role.SYSTEM, content=text))

    def _append(self, msg: Message) -> None:
        self._messages.append(msg)
        self._window_if_needed()

    # -- windowing --------------------------------------------------------

    def _window_if_needed(self) -> None:
        if len(self._messages) <= self._window:
            return

        naive_cut = len(self._messages) - self._window
        cut = self._safe_cut_index(naive_cut)
        if cut <= 0:
            return

        dropped = self._messages[:cut]
        kept = self._messages[cut:]
        note = Message(role=Role.SYSTEM, content=f"[earlier conversation summary] {self._summarize(dropped)}")
        self._messages = [note, *kept]

    def _safe_cut_index(self, proposed: int) -> int:
        """Move `proposed` to the nearest boundary that doesn't split a
        tool-calls/tool-results pair.

        The only way a naive cut can be unsafe is if the last message about
        to be dropped is an assistant message with `tool_calls` whose
        matching tool-result messages sit just past the cut (i.e. would be
        kept). Detect that and push the cut forward past those results. If
        the results aren't there yet (mid-turn) or aren't contiguous, pull
        the cut back instead so the assistant message stays with them.
        """
        n = len(self._messages)
        proposed = max(0, min(proposed, n))

        while 0 < proposed < n:
            prev = self._messages[proposed - 1]
            if not (prev.role is Role.ASSISTANT and prev.tool_calls):
                break

            pending_ids = {c.id for c in prev.tool_calls}
            scan = proposed
            while scan < n and pending_ids:
                candidate = self._messages[scan]
                if (
                    candidate.role is Role.TOOL
                    and candidate.tool_result is not None
                    and candidate.tool_result.call_id in pending_ids
                ):
                    pending_ids.discard(candidate.tool_result.call_id)
                    scan += 1
                else:
                    break

            if pending_ids:
                # Not all results are here yet, or they're not contiguous —
                # safest is to keep the assistant message itself too.
                return proposed - 1

            proposed = scan

        return proposed

    def _summarize(self, dropped: list[Message]) -> str:
        """A plain-text digest, not a model call — spending tokens to
        summarize the thing we're trimming to save tokens would defeat the
        point.
        """
        user_turns = sum(1 for m in dropped if m.role is Role.USER)
        assistant_turns = sum(1 for m in dropped if m.role is Role.ASSISTANT)
        tool_call_count = sum(len(m.tool_calls) for m in dropped if m.role is Role.ASSISTANT)
        last_user = next((m.content for m in reversed(dropped) if m.role is Role.USER and m.content), "")

        parts = [f"{user_turns} earlier user message(s), {assistant_turns} assistant reply(ies)"]
        parts.append(f"{tool_call_count} tool call(s) made" if tool_call_count else "no tools used")
        if last_user:
            parts.append(f"most recent earlier topic: {last_user[:200]!r}")
        return "; ".join(parts) + "."

    # -- persistence / restore --------------------------------------------

    @classmethod
    def restore(cls, db: Database, *, source: str = "cli", window: int = DEFAULT_WINDOW) -> "Conversation":
        """Resume the most recent conversation for this source, if any, so
        context survives a restart (spec §18 short-term memory).
        """
        row = db.latest_conversation(source)
        if row is None:
            return cls(db, source=source, window=window)

        convo = cls(db, source=source, window=window, conversation_id=row["id"])
        restored: list[Message] = []
        for record in db.recent_messages(row["id"], limit=window):
            msg = _row_to_message(record)
            if msg is not None:
                restored.append(msg)
        convo._messages = _drop_dangling_tool_results(restored)
        return convo


def _row_to_message(record) -> Message | None:
    try:
        role = Role(record["role"])
    except ValueError:
        log.warning("unknown message role %r in database, skipping", record["role"])
        return None

    content = record["content"] or ""

    tool_calls: list[ToolCall] = []
    raw_calls = record["tool_calls"]
    if raw_calls:
        try:
            tool_calls = [
                ToolCall(id=c["id"], name=c["name"], arguments=c.get("arguments", {}))
                for c in json.loads(raw_calls)
            ]
        except (json.JSONDecodeError, KeyError, TypeError):
            log.warning("could not decode stored tool_calls for message %s", record["id"])

    tool_result: ToolResult | None = None
    raw_result = record["tool_result"]
    if raw_result:
        try:
            blob = json.loads(raw_result)
            tool_result = ToolResult(
                call_id=blob.get("call_id", ""),
                name=blob.get("name", ""),
                ok=bool(blob.get("ok", True)),
                content=blob.get("content"),
            )
        except (json.JSONDecodeError, TypeError):
            log.warning("could not decode stored tool_result for message %s", record["id"])

    return Message(role=role, content=content, tool_calls=tool_calls, tool_result=tool_result)


def _drop_dangling_tool_results(messages: list[Message]) -> list[Message]:
    """Drop leading tool-result messages whose originating call fell outside
    the restored window — the provider would otherwise reject them as
    unmatched on the very first turn after a restart.
    """
    pending: set[str] = set()
    result: list[Message] = []
    for msg in messages:
        if msg.role is Role.ASSISTANT and msg.tool_calls:
            pending = {c.id for c in msg.tool_calls}
            result.append(msg)
            continue
        if msg.role is Role.TOOL:
            call_id = msg.tool_result.call_id if msg.tool_result else None
            if call_id not in pending:
                continue
            pending.discard(call_id)
            result.append(msg)
            continue
        result.append(msg)
    return result
