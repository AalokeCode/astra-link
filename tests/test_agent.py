"""Tests for the agent loop, router, and conversation windowing.

Everything here uses `FakeProvider`, a scripted stand-in for `LLMProvider` —
no network call is ever made. `asyncio_mode = "auto"` (pyproject.toml) means
`async def test_...` functions run without an explicit marker.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import BaseModel, Field

from app.agent.agent import Assistant
from app.agent.conversation import Conversation
from app.agent.permissions import PermissionBroker
from app.agent.router import Router
from app.config import Config
from app.llm.base import LLMError, LLMProvider, LLMResponse, Message, Role, ToolCall, ToolResult
from app.llm.models import ModelResolver
from app.memory.database import Database
from app.tools.registry import RiskLevel, ToolExecutionError, ToolGroup, ToolRegistry


# -- test doubles -----------------------------------------------------------


class FakeProvider(LLMProvider):
    """Consumes a scripted list of responses/errors, one per `chat()` call."""

    def __init__(self, name: str, script: list[LLMResponse | LLMError]) -> None:
        self.name = name
        self._script = list(script)
        self.calls: list[list[Message]] = []

    @property
    def available(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None

    async def list_models(self) -> list[str]:
        return []

    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        system: str | None = None,
        temperature: float = 0.7,
        max_output_tokens: int = 2048,
    ) -> LLMResponse:
        self.calls.append(list(messages))
        if not self._script:
            raise AssertionError(f"FakeProvider[{self.name}] ran out of scripted responses")
        item = self._script.pop(0)
        if isinstance(item, LLMError):
            raise item
        return item


def make_config(tmp_path: Path, **overrides: Any) -> Config:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "logs").mkdir(exist_ok=True)
    base: dict[str, Any] = dict(
        app_name="TestAssistant",
        primary_llm="gemini",
        fast_llm="groq",
        fallback_llm="groq",
        gemini_api_key="test-gemini-key",
        groq_api_key="test-groq-key",
        gemini_reasoning_models=["fake-gemini-reasoning"],
        gemini_fast_models=["fake-gemini-fast"],
        groq_reasoning_models=["fake-groq-reasoning"],
        groq_fast_models=["fake-groq-fast"],
        gemini_live_model="fake-gemini-live",
        gemini_live_voice="Kore",
        live_max_concurrent_sessions=1,
        live_max_session_seconds=600,
        live_max_daily_minutes=60,
        live_context_trigger_tokens=25_000,
        live_context_target_tokens=8_000,
        live_transcriptions=True,
        link_public_url="https://link.example.test",
        link_session_token="test-link-token-that-is-long-enough",
        link_allowed_origins=["https://link.example.test"],
        web_dist_dir=tmp_path / "web-out",
        data_dir=data_dir,
        memory_retention_days=90,
        log_max_bytes=1_000_000,
        log_backup_count=1,
        allowed_dirs=[],
        enable_shell_tools=False,
        enable_web_search=False,
        enable_reminders=False,
        require_confirmation=True,
        max_agent_iterations=4,
        shell_timeout_seconds=5,
        timezone=ZoneInfo("UTC"),
        debug=False,
    )
    base.update(overrides)
    return Config(**base)


def build_tool_registry() -> tuple[ToolRegistry, dict[str, list[Any]]]:
    """A fresh registry per test with three tools exercising each risk level."""
    reg = ToolRegistry()
    calls: dict[str, list[Any]] = {"weather": [], "search": [], "delete": []}

    class WeatherArgs(BaseModel):
        city: str = Field(description="City to check")

    @reg.tool(
        name="get_weather",
        description="Get the weather for a city",
        args_model=WeatherArgs,
        risk=RiskLevel.READ_ONLY,
        group=ToolGroup.CORE,
    )
    def get_weather(args: WeatherArgs) -> dict:
        calls["weather"].append(args.city)
        return {"city": args.city, "forecast": "sunny"}

    class SearchArgs(BaseModel):
        query: str = Field(description="Search query")

    @reg.tool(
        name="search_docs",
        description="Search local documents",
        args_model=SearchArgs,
        risk=RiskLevel.READ_ONLY,
        group=ToolGroup.CORE,
    )
    def search_docs(args: SearchArgs) -> dict:
        calls["search"].append(args.query)
        if args.query == "boom":
            raise ToolExecutionError("no such document exists")
        return {"query": args.query, "hits": 3}

    class DeleteArgs(BaseModel):
        path: str = Field(description="Path to delete")

    @reg.tool(
        name="delete_file",
        description="Permanently delete a file",
        args_model=DeleteArgs,
        risk=RiskLevel.HIGH_RISK,
        group=ToolGroup.CORE,
        confirm_template="Delete {path} permanently?",
    )
    def delete_file(args: DeleteArgs) -> dict:
        calls["delete"].append(args.path)
        return {"deleted": args.path}

    return reg, calls


def make_assistant(
    tmp_path: Path,
    *,
    providers: dict[str, LLMProvider],
    tool_registry: ToolRegistry,
    confirm_handler=None,
    max_iterations: int = 4,
) -> tuple[Assistant, Database]:
    cfg = make_config(tmp_path, max_agent_iterations=max_iterations)
    db = Database(cfg.db_path)
    resolver = ModelResolver(cfg.model_cache_path)  # never refreshed -> resolve() falls back to prefs[0]
    router = Router(cfg, providers, resolver)
    broker = PermissionBroker(require_confirmation=cfg.require_confirmation, confirm_handler=confirm_handler)
    assistant = Assistant(cfg, db, tool_registry, router, broker)
    return assistant, db


# A phrase guaranteed to classify as COMPLEX (keywords: research/analyze) so
# tests can rely on the primary provider ("gemini" in make_config) being chosen.
COMPLEX_TEXT = "please research and analyze this thoroughly for me"


# -- tests --------------------------------------------------------------


async def test_plain_answer_no_tool_call(tmp_path: Path) -> None:
    gemini = FakeProvider("gemini", [LLMResponse(text="Hello there!", provider="gemini", model="fake")])
    groq = FakeProvider("groq", [])
    reg = ToolRegistry()
    assistant, db = make_assistant(tmp_path, providers={"gemini": gemini, "groq": groq}, tool_registry=reg)

    reply = await assistant.process_input(COMPLEX_TEXT, source="cli")

    assert reply == "Hello there!"
    db.close()


async def test_single_tool_call_then_answer(tmp_path: Path) -> None:
    reg, calls = build_tool_registry()
    call = ToolCall(id="call-1", name="get_weather", arguments={"city": "Chennai"})
    gemini = FakeProvider(
        "gemini",
        [
            LLMResponse(text="", tool_calls=[call], provider="gemini", model="fake"),
            LLMResponse(text="It's sunny in Chennai.", provider="gemini", model="fake"),
        ],
    )
    groq = FakeProvider("groq", [])
    assistant, db = make_assistant(tmp_path, providers={"gemini": gemini, "groq": groq}, tool_registry=reg)

    reply = await assistant.process_input(COMPLEX_TEXT, source="cli")

    assert reply == "It's sunny in Chennai."
    assert calls["weather"] == ["Chennai"]
    db.close()


async def test_live_voice_excludes_and_rejects_high_risk_tools(tmp_path: Path) -> None:
    reg, calls = build_tool_registry()

    async def confirmation_must_not_run(_request) -> bool:
        raise AssertionError("live voice attempted to open a terminal confirmation")

    assistant, db = make_assistant(
        tmp_path,
        providers={"gemini": FakeProvider("gemini", []), "groq": FakeProvider("groq", [])},
        tool_registry=reg,
        confirm_handler=confirmation_must_not_run,
    )

    definitions = assistant.live_tool_definitions()
    assert {item["name"] for item in definitions} == {"get_weather", "search_docs"}

    result = await assistant.execute_live_tool_calls(
        [ToolCall(id="danger", name="delete_file", arguments={"path": "notes.txt"})],
        source="link:test",
    )
    assert result[0].ok is False
    assert "unavailable during a live voice session" in result[0].content
    assert calls["delete"] == []
    db.close()


async def test_multi_step_tool_chain(tmp_path: Path) -> None:
    reg, calls = build_tool_registry()
    call1 = ToolCall(id="c1", name="get_weather", arguments={"city": "Pune"})
    call2 = ToolCall(id="c2", name="search_docs", arguments={"query": "umbrella"})
    gemini = FakeProvider(
        "gemini",
        [
            LLMResponse(text="", tool_calls=[call1], provider="gemini", model="fake"),
            LLMResponse(text="", tool_calls=[call2], provider="gemini", model="fake"),
            LLMResponse(text="Bring an umbrella, it might rain later in Pune.", provider="gemini", model="fake"),
        ],
    )
    groq = FakeProvider("groq", [])
    assistant, db = make_assistant(
        tmp_path, providers={"gemini": gemini, "groq": groq}, tool_registry=reg, max_iterations=4
    )

    reply = await assistant.process_input(COMPLEX_TEXT, source="cli")

    assert reply == "Bring an umbrella, it might rain later in Pune."
    assert calls["weather"] == ["Pune"]
    assert calls["search"] == ["umbrella"]
    db.close()


async def test_tool_error_is_fed_back_not_raised(tmp_path: Path) -> None:
    reg, calls = build_tool_registry()
    call = ToolCall(id="c1", name="search_docs", arguments={"query": "boom"})
    gemini = FakeProvider(
        "gemini",
        [
            LLMResponse(text="", tool_calls=[call], provider="gemini", model="fake"),
            LLMResponse(text="I couldn't find that document.", provider="gemini", model="fake"),
        ],
    )
    groq = FakeProvider("groq", [])
    assistant, db = make_assistant(tmp_path, providers={"gemini": gemini, "groq": groq}, tool_registry=reg)

    reply = await assistant.process_input(COMPLEX_TEXT, source="cli")

    assert reply == "I couldn't find that document."
    assert calls["search"] == ["boom"]

    # The second chat() call must have received a *failed* tool result, not
    # a crash or a dropped message.
    second_call_messages = gemini.calls[1]
    tool_msgs = [m for m in second_call_messages if m.tool_result is not None]
    assert tool_msgs, "expected a tool-result message to be fed back to the model"
    assert tool_msgs[-1].tool_result.ok is False
    assert "no such document" in str(tool_msgs[-1].tool_result.content)
    db.close()


async def test_iteration_cap_terminates_cleanly(tmp_path: Path) -> None:
    reg, calls = build_tool_registry()
    responses = [
        LLMResponse(
            text="",
            tool_calls=[ToolCall(id=f"c{i}", name="get_weather", arguments={"city": "X"})],
            provider="gemini",
            model="fake",
        )
        for i in range(10)
    ]
    gemini = FakeProvider("gemini", responses)
    groq = FakeProvider("groq", [])
    assistant, db = make_assistant(
        tmp_path, providers={"gemini": gemini, "groq": groq}, tool_registry=reg, max_iterations=3
    )

    reply = await assistant.process_input(COMPLEX_TEXT, source="cli")

    assert "3" in reply
    assert len(gemini.calls) == 3  # loop stopped exactly at the cap, no runaway
    db.close()


async def test_high_risk_tool_declined_does_not_execute(tmp_path: Path) -> None:
    reg, calls = build_tool_registry()
    call = ToolCall(id="c1", name="delete_file", arguments={"path": "/tmp/important.txt"})
    gemini = FakeProvider(
        "gemini",
        [
            LLMResponse(text="", tool_calls=[call], provider="gemini", model="fake"),
            LLMResponse(text="Okay, I did not delete the file.", provider="gemini", model="fake"),
        ],
    )
    groq = FakeProvider("groq", [])

    async def deny(_request) -> bool:
        return False

    assistant, db = make_assistant(
        tmp_path,
        providers={"gemini": gemini, "groq": groq},
        tool_registry=reg,
        confirm_handler=deny,
    )

    reply = await assistant.process_input(COMPLEX_TEXT, source="cli")

    assert reply == "Okay, I did not delete the file."
    assert calls["delete"] == [], "delete_file's underlying function must never have run"
    db.close()


async def test_provider_fallback_on_retryable_error(tmp_path: Path) -> None:
    reg = ToolRegistry()
    gemini = FakeProvider("gemini", [LLMError("transport blip", provider="gemini", retryable=True)])
    groq = FakeProvider("groq", [LLMResponse(text="Handled by groq.", provider="groq", model="fake")])
    assistant, db = make_assistant(tmp_path, providers={"gemini": gemini, "groq": groq}, tool_registry=reg)

    reply = await assistant.process_input(COMPLEX_TEXT, source="cli")

    assert reply == "Handled by groq."
    db.close()


async def test_both_providers_unavailable_raises_clear_error(tmp_path: Path) -> None:
    reg = ToolRegistry()
    gemini = FakeProvider("gemini", [LLMError("boom", provider="gemini", retryable=True)])
    groq = FakeProvider("groq", [LLMError("boom too", provider="groq", retryable=True)])
    assistant, db = make_assistant(tmp_path, providers={"gemini": gemini, "groq": groq}, tool_registry=reg)

    reply = await assistant.process_input(COMPLEX_TEXT, source="cli")

    assert "couldn't reach" in reply.lower() or "provider" in reply.lower()
    db.close()


def test_windowing_keeps_tool_calls_with_their_results(tmp_path: Path) -> None:
    db = Database(tmp_path / "window.db")
    convo = Conversation(db, source="cli", window=6)

    for i in range(20):
        convo.add_user(f"question {i}")
        call = ToolCall(id=f"call-{i}", name="get_weather", arguments={"city": "X"})
        convo.add_assistant("", tool_calls=[call])
        convo.add_tool_result(ToolResult(call_id=f"call-{i}", name="get_weather", ok=True, content={"ok": True}))
        convo.add_assistant(f"answer {i}")

    messages = convo.messages
    assert len(messages) > 0

    pending: set[str] = set()
    for msg in messages:
        if msg.role is Role.ASSISTANT and msg.tool_calls:
            pending |= {c.id for c in msg.tool_calls}
        if msg.role is Role.TOOL and msg.tool_result is not None:
            assert msg.tool_result.call_id in pending, (
                f"tool result for {msg.tool_result.call_id} has no matching call in the window"
            )
            pending.discard(msg.tool_result.call_id)
    assert not pending, "an assistant tool_calls message survived windowing without its results"

    db.close()
