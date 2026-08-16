"""Gemini provider over the raw REST API.

No SDK. The Gemini Developer API is plain JSON over HTTPS:

    POST /v1beta/models/{model}:generateContent
    x-goog-api-key: <key>

Tools go in `tools[].functionDeclarations[]`; the model answers with
`functionCall` parts. That is the entire surface we need, and wrapping it
ourselves keeps ~60 MB of SDK off the disk while giving us the normalized
types in `base.py` that we would have had to write regardless.
"""

from __future__ import annotations

import uuid
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

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

# Keys that are valid JSON Schema but that Gemini's OpenAPI-subset parser
# rejects or ignores. Pydantic emits several of them by default.
_UNSUPPORTED_SCHEMA_KEYS = {
    "title",
    "default",
    "additionalProperties",
    "examples",
    "$schema",
    "$defs",
    "definitions",
    "discriminator",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "const",
}


def _adapt_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Strip a JSON Schema down to what Gemini accepts.

    Assumes `$ref`s have already been inlined upstream (registry.flatten_schema).
    The remaining job is dropping annotation-only keys and collapsing the
    `anyOf: [T, null]` that pydantic emits for `Optional[T]` — Gemini has no
    union type, so we keep the non-null branch and let `required` carry
    optionality.
    """
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _UNSUPPORTED_SCHEMA_KEYS:
            continue
        if key == "anyOf":
            branches = [b for b in value if b.get("type") != "null"]
            if not branches:
                continue
            merged = _adapt_schema(branches[0])
            # Fold the collapsed branch up into the parent rather than nesting.
            for mk, mv in merged.items():
                out.setdefault(mk, mv)
            out["nullable"] = len(branches) < len(value)
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {k: _adapt_schema(v) for k, v in value.items()}
        elif key == "items" and isinstance(value, dict):
            out[key] = _adapt_schema(value)
        else:
            out[key] = value
    # Gemini requires an explicit type on every node.
    if "type" not in out and "enum" in out:
        out["type"] = "string"
    return out


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, api_key: str, *, timeout: float = 60.0) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=timeout,
            headers={"x-goog-api-key": api_key, "content-type": "application/json"},
        )

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- request construction ---------------------------------------------

    def _to_contents(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Translate normalized messages into Gemini `contents`.

        Gemini has no tool-call IDs — it pairs a `functionCall` with the
        `functionResponse` that follows it, by name and order. So we preserve
        ordering and never reorder tool results.
        """
        contents: list[dict[str, Any]] = []
        for msg in messages:
            if msg.role is Role.SYSTEM:
                continue  # carried separately in systemInstruction
            if msg.role is Role.TOOL:
                if msg.tool_result is None:
                    continue
                contents.append(
                    {
                        "role": "user",
                        "parts": [
                            {
                                "functionResponse": {
                                    "name": msg.tool_result.name,
                                    "response": msg.tool_result.payload(),
                                }
                            }
                        ],
                    }
                )
                continue

            parts: list[dict[str, Any]] = []
            if msg.content:
                parts.append({"text": msg.content})
            for call in msg.tool_calls:
                parts.append({"functionCall": {"name": call.name, "args": call.arguments}})
            if not parts:
                continue
            contents.append(
                {"role": "model" if msg.role is Role.ASSISTANT else "user", "parts": parts}
            )
        return contents

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
            raise LLMError("GEMINI_API_KEY not set", provider=self.name, retryable=False)

        model = model or "gemini-2.5-flash"
        body: dict[str, Any] = {
            "contents": self._to_contents(messages),
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_output_tokens,
            },
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            body["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": t["name"],
                            "description": t["description"],
                            "parameters": _adapt_schema(t["parameters"]),
                        }
                        for t in tools
                    ]
                }
            ]

        try:
            resp = await self._client.post(f"/models/{model}:generateContent", json=body)
        except httpx.HTTPError as exc:
            raise LLMError(f"transport: {exc}", provider=self.name, retryable=True) from exc

        if resp.status_code != 200:
            # 4xx other than 429 means our request is wrong; retrying on another
            # provider is fine, but retrying *this* one is pointless.
            retryable = resp.status_code >= 500 or resp.status_code == 429
            raise LLMError(
                f"HTTP {resp.status_code}: {resp.text[:300]}",
                provider=self.name,
                retryable=retryable,
                retry_after=parse_retry_after(resp.text) if resp.status_code == 429 else None,
            )

        return self._parse(resp.json(), model)

    def _parse(self, data: dict[str, Any], model: str) -> LLMResponse:
        candidates = data.get("candidates") or []
        if not candidates:
            # Usually a prompt-level safety block; surface it rather than
            # returning a confusing empty answer (spec §32).
            reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates")
            raise LLMError(f"empty response ({reason})", provider=self.name, retryable=False)

        candidate = candidates[0]
        finish = candidate.get("finishReason", "")
        parts = (candidate.get("content") or {}).get("parts") or []

        text_chunks: list[str] = []
        calls: list[ToolCall] = []
        for part in parts:
            if "text" in part:
                text_chunks.append(part["text"])
            elif "functionCall" in part:
                fc = part["functionCall"]
                calls.append(
                    ToolCall(
                        # Synthesised: Gemini pairs by name/order, but the rest
                        # of the system keys results by ID.
                        id=f"gemini-{uuid.uuid4().hex[:12]}",
                        name=fc.get("name", ""),
                        arguments=fc.get("args") or {},
                    )
                )

        meta = data.get("usageMetadata") or {}
        return LLMResponse(
            text="".join(text_chunks).strip(),
            tool_calls=calls,
            model=model,
            provider=self.name,
            usage=Usage(
                input_tokens=meta.get("promptTokenCount", 0),
                output_tokens=meta.get("candidatesTokenCount", 0),
            ),
            finish_reason=finish,
        )

    async def list_models(self) -> list[str]:
        if not self.available:
            return []
        try:
            resp = await self._client.get("/models", params={"pageSize": 200})
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMError(f"list_models: {exc}", provider=self.name) from exc
        return [
            m["name"].removeprefix("models/")
            for m in resp.json().get("models", [])
            if "generateContent" in m.get("supportedGenerationMethods", [])
        ]
