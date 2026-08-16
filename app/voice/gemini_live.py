"""Provider-neutral, quota-limited Gemini Live session."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import aiohttp

from app.agent.agent import Assistant
from app.config import Config
from app.llm.base import ToolCall

log = logging.getLogger(__name__)

GEMINI_LIVE_ENDPOINT = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)
GEMINI_INPUT_SAMPLE_RATE = 16_000
GEMINI_OUTPUT_SAMPLE_RATE = 24_000


class LiveSink(Protocol):
    async def send_audio(self, pcm: bytes) -> None: ...

    async def clear_audio(self) -> None: ...

    async def send_event(self, event: Mapping[str, Any]) -> None: ...


class LiveQuotaExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class QuotaSnapshot:
    active_sessions: int
    max_concurrent_sessions: int
    sessions_started_today: int
    used_seconds_today: int
    max_daily_seconds: int


class LiveQuotaGuard:
    """Persistent, conservative guard for free/low-budget Gemini projects."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._lock = asyncio.Lock()
        self._day = datetime.now(UTC).date()
        self._active = 0
        self._started = 0
        self._used_seconds = 0.0
        self._state_path = cfg.data_dir / "live_quota.json"
        self._load()

    def _load(self) -> None:
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
            if state.get("day") == self._day.isoformat():
                self._started = max(0, int(state.get("sessions", 0)))
                self._used_seconds = max(0.0, float(state.get("used_seconds", 0)))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return

    def _persist(self) -> None:
        payload = json.dumps(
            {
                "day": self._day.isoformat(),
                "sessions": self._started,
                "used_seconds": round(self._used_seconds, 3),
            },
            separators=(",", ":"),
        )
        temporary = self._state_path.with_suffix(".tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self._state_path)

    def _roll_day(self) -> None:
        today = datetime.now(UTC).date()
        if today != self._day:
            self._day = today
            self._started = 0
            self._used_seconds = 0.0
            with contextlib.suppress(OSError):
                self._persist()

    @asynccontextmanager
    async def lease(self) -> AsyncIterator[float]:
        async with self._lock:
            self._roll_day()
            if self._active >= self._cfg.live_max_concurrent_sessions:
                raise LiveQuotaExceeded("all configured live-session slots are in use")
            daily_seconds = self._cfg.live_max_daily_minutes * 60
            reserved_seconds = self._active * self._cfg.live_max_session_seconds
            remaining_seconds = daily_seconds - self._used_seconds - reserved_seconds
            if remaining_seconds <= 0:
                raise LiveQuotaExceeded("the configured daily live-audio budget is exhausted")
            allowed_seconds = min(
                float(self._cfg.live_max_session_seconds), remaining_seconds
            )
            self._active += 1
            self._started += 1
            with contextlib.suppress(OSError):
                self._persist()
        started = time.monotonic()
        try:
            yield allowed_seconds
        finally:
            elapsed = min(time.monotonic() - started, allowed_seconds)
            async with self._lock:
                self._active = max(0, self._active - 1)
                self._used_seconds += elapsed
                with contextlib.suppress(OSError):
                    self._persist()

    async def snapshot(self) -> QuotaSnapshot:
        async with self._lock:
            self._roll_day()
            return QuotaSnapshot(
                active_sessions=self._active,
                max_concurrent_sessions=self._cfg.live_max_concurrent_sessions,
                sessions_started_today=self._started,
                used_seconds_today=round(self._used_seconds),
                max_daily_seconds=self._cfg.live_max_daily_minutes * 60,
            )


def append_transcript(current: str, fragment: str) -> str:
    if not fragment:
        return current
    if (
        not current
        or fragment[:1].isspace()
        or fragment[:1] in ".,!?;:"
        or current[-1:].isspace()
    ):
        return current + fragment
    return current + " " + fragment


def build_gemini_setup(cfg: Config, assistant: Assistant) -> dict[str, Any]:
    setup: dict[str, Any] = {
        "model": f"models/{cfg.gemini_live_model}",
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": cfg.gemini_live_voice}
                }
            },
            "thinkingConfig": {"thinkingLevel": "minimal"},
        },
        "systemInstruction": {"parts": [{"text": assistant.live_system_prompt()}]},
        "tools": [{"functionDeclarations": assistant.live_tool_definitions()}],
        "realtimeInputConfig": {
            "automaticActivityDetection": {
                "disabled": False,
                "prefixPaddingMs": 100,
                "silenceDurationMs": 550,
            }
        },
        "contextWindowCompression": {
            "triggerTokens": str(cfg.live_context_trigger_tokens),
            "slidingWindow": {"targetTokens": str(cfg.live_context_target_tokens)},
        },
        "sessionResumption": {},
    }
    if cfg.live_transcriptions:
        setup["inputAudioTranscription"] = {}
        setup["outputAudioTranscription"] = {}
    return {"setup": setup}


def safe_live_error(payload: Mapping[str, Any]) -> str:
    error = payload.get("error")
    if isinstance(error, Mapping):
        return str(error.get("message") or error.get("status") or "unknown error")[:300]
    return "unexpected response"


def decode_live_message(message: aiohttp.WSMessage) -> Mapping[str, Any] | None:
    """Decode Gemini's JSON from either text or binary WebSocket frames."""
    if message.type is aiohttp.WSMsgType.TEXT:
        raw: str | bytes = message.data
    elif message.type is aiohttp.WSMsgType.BINARY:
        raw = message.data
    else:
        return None

    try:
        payload = json.loads(raw)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Gemini Live returned invalid JSON") from exc
    if not isinstance(payload, Mapping):
        raise RuntimeError("Gemini Live returned a non-object message")
    return payload


class GeminiLiveSession:
    def __init__(
        self,
        cfg: Config,
        assistant: Assistant,
        http: aiohttp.ClientSession,
        sink: LiveSink,
        *,
        source: str,
    ) -> None:
        self._cfg = cfg
        self._assistant = assistant
        self._http = http
        self._sink = sink
        self._source = source
        self._gemini: aiohttp.ClientWebSocketResponse | None = None
        self._receiver: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._input_transcript = ""
        self._output_transcript = ""
        self._resumption_handle = ""

    async def connect(self) -> None:
        self._gemini = await self._http.ws_connect(
            GEMINI_LIVE_ENDPOINT,
            params={"key": self._cfg.gemini_api_key},
            heartbeat=20,
            receive_timeout=None,
            max_msg_size=16 * 1024 * 1024,
        )
        await self._send(build_gemini_setup(self._cfg, self._assistant))
        try:
            ack = await asyncio.wait_for(self._gemini.receive(), timeout=15)
        except TimeoutError as exc:
            raise RuntimeError("Gemini Live did not acknowledge setup") from exc
        payload = decode_live_message(ack)
        if payload is None:
            reason = str(ack.extra or ack.data or "no reason")[:300]
            raise RuntimeError(
                f"Gemini Live closed during setup ({ack.type.name}): {reason}"
            )
        if "setupComplete" not in payload:
            raise RuntimeError(f"Gemini Live rejected setup: {safe_live_error(payload)}")
        await self._sink.send_event({"type": "state", "state": "listening"})
        self._receiver = asyncio.create_task(
            self._receive(), name=f"gemini-live:{self._source}"
        )

    async def greet(self, prompt: str | None = None) -> None:
        await self.send_text(
            prompt
            or "The voice session connected. Briefly greet the user as ASTRA and ask how you can help."
        )

    async def send_text(self, text: str) -> None:
        if text.strip():
            await self._send({"realtimeInput": {"text": text.strip()}})

    async def send_audio(self, pcm: bytes, *, sample_rate: int) -> None:
        if not pcm:
            return
        if len(pcm) > sample_rate * 2 * 2:
            raise ValueError("live audio chunks must be no longer than two seconds")
        await self._send(
            {
                "realtimeInput": {
                    "audio": {
                        "data": base64.b64encode(pcm).decode("ascii"),
                        "mimeType": f"audio/pcm;rate={sample_rate}",
                    }
                }
            }
        )

    async def _receive(self) -> None:
        assert self._gemini is not None
        try:
            async for message in self._gemini:
                if message.type is aiohttp.WSMsgType.ERROR:
                    raise self._gemini.exception() or RuntimeError("Gemini WebSocket failed")
                payload = decode_live_message(message)
                if payload is not None:
                    await self._handle(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Gemini Live receive loop failed for %s", self._source)
            with contextlib.suppress(Exception):
                await self._sink.send_event(
                    {"type": "error", "message": str(exc)[:200]}
                )

    async def _handle(self, payload: Mapping[str, Any]) -> None:
        update = payload.get("sessionResumptionUpdate")
        if isinstance(update, Mapping) and update.get("resumable"):
            self._resumption_handle = str(update.get("newHandle") or "")

        go_away = payload.get("goAway")
        if isinstance(go_away, Mapping):
            await self._sink.send_event(
                {"type": "state", "state": "ending", "detail": "Gemini reconnect window"}
            )

        tool_call = payload.get("toolCall")
        if isinstance(tool_call, Mapping):
            await self._handle_tool_call(tool_call)

        server = payload.get("serverContent")
        if not isinstance(server, Mapping):
            return
        if server.get("interrupted"):
            await self._sink.clear_audio()
            await self._sink.send_event({"type": "state", "state": "listening"})

        model_turn = server.get("modelTurn")
        if isinstance(model_turn, Mapping):
            for part in model_turn.get("parts") or []:
                if not isinstance(part, Mapping):
                    continue
                inline = part.get("inlineData")
                if not isinstance(inline, Mapping) or not inline.get("data"):
                    continue
                await self._sink.send_event({"type": "state", "state": "speaking"})
                await self._sink.send_audio(base64.b64decode(inline["data"]))

        await self._handle_transcription(server, "inputTranscription", "user")
        await self._handle_transcription(server, "outputTranscription", "assistant")
        if server.get("turnComplete"):
            self._assistant.record_live_turn(
                self._input_transcript,
                self._output_transcript,
                source=self._source,
            )
            await self._sink.send_event(
                {
                    "type": "turn",
                    "input": self._input_transcript,
                    "output": self._output_transcript,
                }
            )
            self._input_transcript = ""
            self._output_transcript = ""
            await self._sink.send_event({"type": "state", "state": "listening"})

    async def _handle_transcription(
        self, server: Mapping[str, Any], key: str, role: str
    ) -> None:
        transcription = server.get(key)
        if not isinstance(transcription, Mapping):
            return
        fragment = str(transcription.get("text") or "")
        if role == "user":
            self._input_transcript = append_transcript(self._input_transcript, fragment)
            text = self._input_transcript
        else:
            self._output_transcript = append_transcript(self._output_transcript, fragment)
            text = self._output_transcript
        await self._sink.send_event(
            {"type": "transcript", "role": role, "text": text, "final": False}
        )

    async def _handle_tool_call(self, payload: Mapping[str, Any]) -> None:
        calls = [
            ToolCall(
                id=str(raw.get("id") or ""),
                name=str(raw.get("name") or ""),
                arguments=dict(raw.get("args") or {}),
            )
            for raw in payload.get("functionCalls") or []
            if isinstance(raw, Mapping)
        ]
        if not calls:
            return
        await self._sink.send_event({"type": "state", "state": "working"})
        results = await self._assistant.execute_live_tool_calls(calls, source=self._source)
        await self._send(
            {
                "toolResponse": {
                    "functionResponses": [
                        {
                            "id": result.call_id,
                            "name": result.name,
                            "response": result.payload(),
                        }
                        for result in results
                    ]
                }
            }
        )

    async def _send(self, payload: Mapping[str, Any]) -> None:
        if self._gemini is None or self._gemini.closed:
            raise RuntimeError("Gemini Live WebSocket is not connected")
        async with self._send_lock:
            await self._gemini.send_json(payload)

    async def aclose(self) -> None:
        if self._receiver is not None:
            self._receiver.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._receiver
        if self._gemini is not None and not self._gemini.closed:
            with contextlib.suppress(Exception):
                await self._gemini.send_json(
                    {"realtimeInput": {"audioStreamEnd": True}}
                )
            await self._gemini.close()
