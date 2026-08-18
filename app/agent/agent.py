"""The agent loop (spec §6, §21, §38).

`Assistant.process_input` is the entire public surface. CLI, voice, and any
future interface call this one method with `(text, source)` — no
interface-specific branching may live in this class (spec §38). Everything
that differs by interface stays in the caller.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import cast

from app.agent.conversation import Conversation
from app.agent.permissions import PermissionBroker
from app.agent.router import Intent, Router, classify_intent
from app.config import Config
from app.llm.base import LLMError, ToolCall, ToolResult
from app.logging_setup import TOOL_CALL_LOGGER
from app.memory.database import Database
from app.tools.registry import RiskLevel, ToolExecutionError, ToolGroup, ToolRegistry

log = logging.getLogger(__name__)
tool_log = logging.getLogger(TOOL_CALL_LOGGER)

# Keyword gating (this project's main cost control): sending every tool
# schema every turn costs ~2,500 input tokens before any history. CORE is
# always included by the registry regardless of what matches here. When a
# turn is ambiguous, `_select_tool_groups` returns everything rather than
# risk starving the model of a tool it needs.
_GROUP_KEYWORDS: dict[ToolGroup, tuple[str, ...]] = {
    ToolGroup.GIT: (
        "git",
        "commit",
        "branch",
        "merge",
        "pull request",
        "repo",
        "repository",
        "stash",
        # Spec §39 asks "What's changed in this project?" to return git status.
        # Without these the turn matched only DOCUMENTS/FILESYSTEM and the model
        # answered by listing files — including node_modules — instead.
        "changed",
        "changes",
        "diff",
        "uncommitted",
        "modified",
        "staged",
        "untracked",
    ),
    ToolGroup.REMINDERS: ("remind", "reminder", "reminders", "todo", "to-do"),
    ToolGroup.DOCUMENTS: ("document", "write up", "docx", "pdf ", ".pdf", "markdown", "report"),
    ToolGroup.FILESYSTEM: (
        "file",
        "folder",
        "directory",
        "path",
        "read the",
        "write the",
        "delete the",
        "list files",
        "rename",
    ),
    ToolGroup.CLAUDE_CODE: (
        "claude code",
        "claude-code",
        "claude instance",
        "codex",
        "coding agent",
        "agent workspace",
        "agent instance",
        "kitty tab",
        "steer it",
        "steer the",
        "pause the",
        "shut down workspace",
        "shutdown workspace",
    ),
    ToolGroup.WEB: ("search", "research", "latest", "web", "google", "browse", "online", "news"),
    ToolGroup.SHELL: ("shell", "terminal", "run command", "command line", "bash", "install", "npm ", "pip "),
    ToolGroup.SYSTEM: (
        "system",
        "battery",
        "cpu",
        "memory usage",
        "disk space",
        "process",
        "running app",
        "volume",
        "brightness",
    ),
}


def _select_tool_groups(text: str) -> set[ToolGroup] | None:
    """`None` means "send every group", used when the turn is ambiguous — a
    missing tool is worse than extra tokens on the first round.

    When the heuristic does match, the loop below keeps that narrowed set for
    follow-up rounds and only adds the groups the model actually called into.
    All 36 schemas cost ~4,000 input tokens; a matched group costs ~500.
    """
    lowered = f" {text.lower()} "
    matched = {group for group, keywords in _GROUP_KEYWORDS.items() if any(kw in lowered for kw in keywords)}
    return matched or None


def _provider_failure_message(exc: LLMError) -> str:
    """Spec §32: never fail silently, never dump a stack trace at the user."""
    return (
        "I couldn't reach any configured AI provider just now "
        f"({exc}). Check the API keys in .env and your network connection, then try again."
    )


class Assistant:
    def __init__(
        self,
        cfg: Config,
        db: Database,
        tool_registry: ToolRegistry,
        router: Router,
        broker: PermissionBroker,
    ) -> None:
        self._cfg = cfg
        self._db = db
        self._registry = tool_registry
        self._router = router
        self._broker = broker
        self._conversations: dict[str, Conversation] = {}

    def new_conversation(self, source: str = "cli") -> None:
        """Start a fresh conversation for a given source (the CLI's `/new`)."""
        self._conversations[source] = Conversation(self._db, source=source)

    def _conversation_for(self, source: str) -> Conversation:
        convo = self._conversations.get(source)
        if convo is None:
            convo = Conversation.restore(self._db, source=source)
            self._conversations[source] = convo
        return convo

    # -- the one public method (spec §38) ----------------------------------

    async def process_input(self, text: str, source: str = "cli") -> str:
        convo = self._conversation_for(source)
        convo.add_user(text)

        intent = classify_intent(text)
        system = self._build_system_prompt(source)
        groups = _select_tool_groups(text)

        # A live utterance is usually a short spoken command. When the heuristic
        # cannot narrow it, send the core set rather than every tool schema.
        if source.startswith("link:") and groups is None:
            groups = {ToolGroup.SYSTEM, ToolGroup.REMINDERS, ToolGroup.CLAUDE_CODE}

        # Any tool already visible in the restored history must stay on offer.
        # Groq rejects a call to a tool absent from this request's `tools` with
        # a hard 400, and the model will happily call something it saw earlier
        # in the conversation. Keeping the two consistent is what makes gating
        # safe to narrow.
        if groups is not None:
            for message in convo.messages:
                for call in message.tool_calls:
                    tool = self._registry.get(call.name)
                    if tool is not None:
                        groups.add(tool.group)
                if message.tool_result is not None:
                    tool = self._registry.get(message.tool_result.name)
                    if tool is not None:
                        groups.add(tool.group)

        for _ in range(self._cfg.max_agent_iterations):
            tool_defs = self._registry.definitions(groups)
            try:
                response = await self._router.chat(
                    convo.messages,
                    intent=intent,
                    tools=tool_defs,
                    system=system,
                )
            except LLMError as exc:
                log.error("router exhausted all providers: %s", exc)
                return _provider_failure_message(exc)

            if not response.wants_tools:
                text_out = response.text or "I don't have anything to add."
                convo.add_assistant(text_out)
                return text_out

            convo.add_assistant(response.text, response.tool_calls)

            # Keep the gated set and only add the groups the model actually
            # reached for. Widening to every group here costs ~3,400 extra input
            # tokens on each follow-up round, which alone exhausts Groq's
            # free-tier per-minute budget on a two-step task. The model has
            # committed to a direction — follow it rather than re-offering
            # everything.
            if groups is not None:
                for call in response.tool_calls:
                    tool = self._registry.get(call.name)
                    if tool is not None:
                        groups.add(tool.group)

            results = await self._run_tool_calls(response.tool_calls, convo.conversation_id)
            for result in results:
                convo.add_tool_result(result)

        limit_msg = (
            f"I stopped after {self._cfg.max_agent_iterations} tool-call steps without finishing "
            "this request. Try narrowing it, and I'll pick up from here."
        )
        convo.add_assistant(limit_msg)
        return limit_msg

    # -- realtime voice integration ---------------------------------------

    def live_system_prompt(self) -> str:
        """System instructions for an audio-native ASTRA Link session."""
        return self._build_system_prompt("link:web")

    def live_tool_definitions(self) -> list[dict]:
        """Expose tools that can complete without an invisible terminal prompt.

        Remote Live sessions have no terminal confirmation surface.
        Offering HIGH_RISK tools there can block the entire WebSocket receive
        loop on ``input()``, which freezes speech until someone answers on the
        Mac. Read-only and low-risk writes remain available.
        """
        return [
            tool.definition()
            for tool in sorted(self._registry.all(), key=lambda item: item.name)
            if tool.risk < RiskLevel.HIGH_RISK
        ]

    async def execute_live_tool_calls(
        self,
        calls: list[ToolCall],
        *,
        source: str,
    ) -> list[ToolResult]:
        """Run Gemini Live function calls through the normal permission boundary."""
        convo = self._conversation_for(source)
        safe_calls: list[ToolCall] = []
        safe_indices: list[int] = []
        results: list[ToolResult | None] = [None] * len(calls)
        for index, call in enumerate(calls):
            tool = self._registry.get(call.name)
            if tool is not None and tool.risk >= RiskLevel.HIGH_RISK:
                results[index] = ToolResult(
                    call_id=call.id,
                    name=call.name,
                    ok=False,
                    content=(
                        f"{call.name} requires confirmation on the Mac and is unavailable "
                        "during a live voice session."
                    ),
                )
            else:
                safe_indices.append(index)
                safe_calls.append(call)

        safe_results = await self._run_tool_calls(safe_calls, convo.conversation_id)
        for index, result in zip(safe_indices, safe_results):
            results[index] = result
        return cast(list[ToolResult], results)

    def record_live_turn(self, user_text: str, assistant_text: str, *, source: str) -> None:
        """Persist optional Live API transcripts without putting text in the audio path."""
        convo = self._conversation_for(source)
        if user_text.strip():
            convo.add_user(user_text.strip())
        if assistant_text.strip():
            convo.add_assistant(assistant_text.strip())

    # -- tool execution -----------------------------------------------------

    async def _run_tool_calls(self, calls: list[ToolCall], conversation_id: int) -> list[ToolResult]:
        """READ_ONLY calls run concurrently; anything at LOW_RISK_WRITE or
        above runs sequentially, one confirmation prompt at a time. Results
        are returned in the original call order regardless of execution
        order — both providers pair tool results to calls positionally.
        """
        results: list[ToolResult | None] = [None] * len(calls)

        read_only: list[int] = []
        writes: list[int] = []
        for i, call in enumerate(calls):
            tool = self._registry.get(call.name)
            if tool is not None and tool.risk is RiskLevel.READ_ONLY:
                read_only.append(i)
            else:
                writes.append(i)

        if read_only:
            outcomes = await asyncio.gather(
                *(self._execute_one(calls[i], conversation_id) for i in read_only)
            )
            for i, outcome in zip(read_only, outcomes):
                results[i] = outcome

        for i in writes:
            results[i] = await self._execute_one(calls[i], conversation_id)

        return cast(list[ToolResult], results)

    async def _execute_one(self, call: ToolCall, conversation_id: int) -> ToolResult:
        start = time.monotonic()
        tool = self._registry.get(call.name)

        if tool is None:
            message = f"unknown tool {call.name!r}; available: {', '.join(self._registry.names())}"
            self._log_tool_call(conversation_id, call.name, call.arguments, "error", 0, message)
            return ToolResult(call_id=call.id, name=call.name, ok=False, content=message)

        try:
            args = self._registry.validate(call.name, call.arguments)
        except ToolExecutionError as exc:
            duration_ms = _elapsed_ms(start)
            self._log_tool_call(conversation_id, call.name, call.arguments, "invalid_args", duration_ms, str(exc))
            return ToolResult(call_id=call.id, name=call.name, ok=False, content=str(exc))

        summary = tool.describe_call(args.model_dump())
        authorization = await self._broker.authorize(tool, summary)
        if not authorization.allowed:
            duration_ms = _elapsed_ms(start)
            message = f"The user declined to run {tool.name} ({authorization.reason})."
            self._log_tool_call(conversation_id, call.name, call.arguments, "declined", duration_ms, authorization.reason)
            return ToolResult(call_id=call.id, name=call.name, ok=False, content=message)

        try:
            outcome = await self._registry.execute(call.name, args)
        except ToolExecutionError as exc:
            duration_ms = _elapsed_ms(start)
            self._log_tool_call(conversation_id, call.name, call.arguments, "error", duration_ms, str(exc))
            return ToolResult(call_id=call.id, name=call.name, ok=False, content=str(exc))
        except Exception as exc:  # a tool crashing must not crash the turn (spec §32)
            log.exception("tool %s raised an unexpected exception", call.name)
            duration_ms = _elapsed_ms(start)
            message = f"{tool.name} failed unexpectedly: {exc}"
            self._log_tool_call(conversation_id, call.name, call.arguments, "error", duration_ms, message)
            return ToolResult(call_id=call.id, name=call.name, ok=False, content=message)

        duration_ms = _elapsed_ms(start)
        self._log_tool_call(conversation_id, call.name, call.arguments, "success", duration_ms, None)
        return ToolResult(call_id=call.id, name=call.name, ok=True, content=outcome)

    def _log_tool_call(
        self,
        conversation_id: int,
        tool_name: str,
        arguments: dict,
        outcome: str,
        duration_ms: int,
        error: str | None,
    ) -> None:
        self._db.log_tool_call(conversation_id, tool_name, arguments, outcome, duration_ms, error)
        tool_log.info(
            "%s outcome=%s duration_ms=%d%s",
            tool_name,
            outcome,
            duration_ms,
            f" error={error}" if error else "",
        )

    # -- system prompt --------------------------------------------------

    def _build_system_prompt(self, source: str) -> str:
        now = datetime.now(self._cfg.timezone)
        allowed = ", ".join(str(p) for p in self._cfg.allowed_dirs) or "(none configured)"
        companion_note = (
            "\nYou are Aaloke's ongoing personal assistant and technical companion, not a "
            "one-off question-answer bot. His name is spelled Aaloke and pronounced 'Ahlok'; "
            "use that pronunciation whenever speaking it. Aaloke is a B.Tech student at "
            "Rishihood. Be calm, "
            "capable, warm, and lightly witty. Show continuity, check in naturally when it fits, "
            "and help him think ahead by briefly surfacing a useful next step or risk. Do not "
            "force small talk, repeat his name in every reply, or imitate a fictional character."
        )
        voice_note = (
            "\nThis turn came in over voice — keep the reply short and speakable; avoid long "
            "lists, tables, or code blocks that read badly aloud. Speak naturally and do not "
            "read URLs, markdown, or tool payloads aloud."
            if source.startswith("link:")
            else ""
        )
        return (
            f"You are {self._cfg.app_name}, a local-first personal assistant with real tool "
            "access to the user's own Mac. Prefer calling a tool over guessing or describing "
            "what you would do — if a tool exists for the job, use it.\n"
            "Never claim to have done something (created a file, sent a reminder, run a command, "
            "checked git or Claude Code status) unless a tool call actually did it. Base every "
            "such claim only on the tool result you received, not on assumption (spec §13).\n"
            "When asked about git status, running processes, or Claude Code, report only what "
            "the relevant tool actually returned — never speculate.\n"
            "Use the coding-agent tools to launch visible Claude Code or Codex Kitty tabs, "
            "inspect their real terminal state, interrupt work, or steer an instance. If exactly "
            "one ASTRA-managed instance exists, references like 'it' or 'that instance' mean that "
            "instance. If several exist and the target is unclear, list them and ask which one.\n"
            "When answering questions about an API, library, or CLI tool, prefer official "
            "documentation over guesswork; use the web search tool if you are not certain "
            "(spec §8).\n"
            f"Current date/time: {now.isoformat()} (timezone: {self._cfg.timezone}).\n"
            f"Allowed working directories: {allowed}.{companion_note}{voice_note}"
        )


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)
