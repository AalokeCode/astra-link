"""Provider-neutral conversation types and the LLMProvider interface.

Everything above this module speaks only in these types. Gemini's `contents`/
`functionCall` shape and Groq's OpenAI-compatible `messages`/`tool_calls` shape
both stay sealed inside their own adapter (spec §5), so adding a third provider
means adding one file and touching nothing else.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# Cap on a single tool result as sent to the model. Free-tier providers reject
# the entire request past a token ceiling, so bounding this bounds the request.
MAX_TOOL_RESULT_CHARS = 6000


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class ToolCall:
    """A model's request to run one tool."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolResult:
    """The outcome of running one tool, fed back to the model.

    `ok=False` still round-trips to the model rather than raising: the model is
    usually able to recover (retry with a corrected path, pick another tool, or
    explain the failure), which is what spec §32 asks for.
    """

    call_id: str
    name: str
    ok: bool
    content: Any

    def payload(self) -> dict[str, Any]:
        """The result as the model sees it, bounded in size.

        A tool result is resent with every later request in the turn, so one
        large payload (a project scan, a directory listing) is paid for
        repeatedly and can push the whole request past a provider's per-minute
        token ceiling — which fails the request outright rather than degrading.
        Bounding here covers both providers, since Gemini sends this dict
        directly while Groq goes through `as_text`.
        """
        if self.ok:
            body = self.content if isinstance(self.content, dict) else {"result": self.content}
        else:
            body = {"error": str(self.content)}

        rendered = json.dumps(body, default=str, ensure_ascii=False)
        if len(rendered) <= MAX_TOOL_RESULT_CHARS:
            return body

        # Truncate visibly, so the model knows data was cut rather than
        # inferring the structure simply ended.
        dropped = len(rendered) - MAX_TOOL_RESULT_CHARS
        return {
            "truncated": True,
            "note": f"result too large; {dropped} characters omitted. Narrow the query for detail.",
            "partial_result": rendered[:MAX_TOOL_RESULT_CHARS],
        }

    def as_text(self) -> str:
        return json.dumps(self.payload(), default=str, ensure_ascii=False)


@dataclass
class Message:
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_result: ToolResult | None = None


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
        )


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    provider: str = ""
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = ""

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LLMError(RuntimeError):
    """Provider call failed. Carries whether a different provider might work."""

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        retryable: bool = True,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable
        # Seconds the provider asked us to wait, when it says so. Free tiers
        # routinely reject a request that would succeed a moment later, so
        # honouring this is the difference between working and not.
        self.retry_after = retry_after


_RETRY_AFTER_RE = re.compile(
    r"(?:try again in|retryDelay\"?:\s*\"?)\s*([0-9]*\.?[0-9]+)\s*(ms|milliseconds|s|seconds)?",
    re.IGNORECASE,
)


def parse_retry_after(body: str) -> float | None:
    """Pull a wait hint out of a 429 body.

    Groq says "Please try again in 360ms" / "in 6.025s"; Gemini returns a
    `retryDelay` of the form "7s". Both are worth obeying exactly — guessing a
    fixed backoff either wastes time or trips the limit again.
    """
    match = _RETRY_AFTER_RE.search(body)
    if match is None:
        return None
    value = float(match.group(1))
    unit = (match.group(2) or "s").lower()
    seconds = value / 1000.0 if unit.startswith("m") else value
    # Anything longer than this is a real quota exhaustion, not a burst limit;
    # fail over to the other provider instead of stalling the user.
    return seconds if 0 < seconds <= 30 else None


class LLMProvider(ABC):
    """Contract every provider adapter implements (spec §5)."""

    name: str = "base"

    @abstractmethod
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
        """One round trip. Returns text, tool calls, or both."""

    @abstractmethod
    async def list_models(self) -> list[str]:
        """Model IDs this provider currently serves.

        Used to resolve configured preferences at runtime so a retired model ID
        degrades to the next choice instead of erroring (spec §7).
        """

    @abstractmethod
    async def aclose(self) -> None: ...

    @property
    @abstractmethod
    def available(self) -> bool:
        """False when the provider has no credentials configured."""
