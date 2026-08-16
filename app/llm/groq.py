"""Groq provider over the raw REST API.

Groq exposes an OpenAI-compatible endpoint, so the wire format differs from
Gemini in every detail that matters: flat `messages` instead of `contents`,
real tool-call IDs, `tools[].function`, and tool results as their own message
role. All of that is contained here — `base.py` types are what escape.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.llm.base import (
    LLMError,
    parse_retry_after,
    LLMProvider,
    LLMResponse,
    Message,
    Role,
    ToolCall,
    Usage,
)

BASE_URL = "https://api.groq.com/openai/v1"


class GroqProvider(LLMProvider):
    name = "groq"

    def __init__(self, api_key: str, *, timeout: float = 60.0) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "content-type": "application/json",
            },
        )

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- request construction ---------------------------------------------

    def _to_messages(
        self, messages: list[Message], system: str | None
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if system:
            out.append({"role": "system", "content": system})

        for msg in messages:
            if msg.role is Role.SYSTEM:
                out.append({"role": "system", "content": msg.content})
            elif msg.role is Role.TOOL:
                if msg.tool_result is None:
                    continue
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg.tool_result.call_id,
                        "name": msg.tool_result.name,
                        "content": msg.tool_result.as_text(),
                    }
                )
            elif msg.role is Role.ASSISTANT:
                entry: dict[str, Any] = {"role": "assistant", "content": msg.content or None}
                if msg.tool_calls:
                    entry["tool_calls"] = [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {
                                "name": c.name,
                                "arguments": json.dumps(c.arguments, ensure_ascii=False),
                            },
                        }
                        for c in msg.tool_calls
                    ]
                out.append(entry)
            else:
                out.append({"role": "user", "content": msg.content})
        return out

    # -- API --------------------------------------------------------------

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
        if not self.available:
            raise LLMError("GROQ_API_KEY not set", provider=self.name, retryable=False)

        model = model or "llama-3.1-8b-instant"
        body: dict[str, Any] = {
            "model": model,
            "messages": self._to_messages(messages, system),
            "temperature": temperature,
            "max_tokens": max_output_tokens,
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["parameters"],
                    },
                }
                for t in tools
            ]
            body["tool_choice"] = "auto"

        try:
            resp = await self._client.post("/chat/completions", json=body)
        except httpx.HTTPError as exc:
            raise LLMError(f"transport: {exc}", provider=self.name, retryable=True) from exc

        if resp.status_code != 200:
            retryable = resp.status_code >= 500 or resp.status_code == 429
            retry_after = parse_retry_after(resp.text) if resp.status_code == 429 else None

            # `tool_use_failed` is a 400, but it is the *model* mis-formatting a
            # function call (it sometimes emits the arguments inside the tool
            # name), not a bad request from us. Sampling is stochastic, so one
            # immediate retry usually produces a well-formed call. Without this
            # a single malformed generation aborts the whole turn.
            if resp.status_code == 400 and "tool_use_failed" in resp.text:
                retryable = True
                retry_after = 0.0

            raise LLMError(
                f"HTTP {resp.status_code}: {resp.text[:300]}",
                provider=self.name,
                retryable=retryable,
                retry_after=retry_after,
            )

        return self._parse(resp.json(), model)

    def _parse(self, data: dict[str, Any], model: str) -> LLMResponse:
        choices = data.get("choices") or []
        if not choices:
            raise LLMError("empty response (no choices)", provider=self.name, retryable=False)

        choice = choices[0]
        message = choice.get("message") or {}

        calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            fn = raw.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                # A model emitting malformed JSON is a tool-call failure, not a
                # provider failure — hand it on with empty args so the registry
                # rejects it with a message the model can act on.
                args = {}
            if not isinstance(args, dict):
                # For a no-argument tool Groq sends the literal string "null",
                # which is truthy — so the `or "{}"` above never fires and
                # json.loads yields None. Validation then rejects every call to
                # every zero-arg tool, and the model retries until the iteration
                # cap. Normalise anything non-dict to an empty argument map.
                args = {}
            calls.append(ToolCall(id=raw.get("id", ""), name=fn.get("name", ""), arguments=args))

        usage = data.get("usage") or {}
        return LLMResponse(
            text=(message.get("content") or "").strip(),
            tool_calls=calls,
            model=model,
            provider=self.name,
            usage=Usage(
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
            ),
            finish_reason=choice.get("finish_reason", ""),
        )

    async def list_models(self) -> list[str]:
        if not self.available:
            return []
        try:
            resp = await self._client.get("/models")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMError(f"list_models: {exc}", provider=self.name) from exc
        return [m["id"] for m in resp.json().get("data", [])]
