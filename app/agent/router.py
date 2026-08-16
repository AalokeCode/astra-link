"""Provider/model routing (spec §7).

Two decisions live here, both intentionally cheap:

1. Which provider+model handles this turn (`choose`) — a heuristic on the
   user's text, not a model call. Complex/multi-step/document/research work
   goes to the primary reasoning provider; short simple asks go to the fast
   provider.
2. What order to try providers in when the first choice fails (`chat`,
   backed by `providers_in_order`) — spec §7's "Gemini unavailable -> try
   Groq -> if unavailable -> return controlled error".
"""

from __future__ import annotations

import asyncio

import logging
from enum import Enum
from typing import Any

from app.config import Config
from app.llm.base import LLMError, LLMProvider, LLMResponse, Message
from app.llm.models import ModelChoice, ModelResolver

log = logging.getLogger(__name__)


class Intent(str, Enum):
    SIMPLE = "simple"
    COMPLEX = "complex"


# Cheap keyword heuristic (spec §6): words that imply multi-step reasoning,
# research, or document work route to the primary reasoning model.
_COMPLEX_KEYWORDS = (
    "document",
    "documentation",
    "research",
    "analyze",
    "analyse",
    "why",
    "explain",
    "compare",
    "plan",
    "design",
    "refactor",
    "summarize",
    "summarise",
    "investigate",
    "review",
    "architecture",
    "strategy",
    "debug",
    "write up",
)

_SIMPLE_WORD_THRESHOLD = 12


def classify_intent(text: str) -> Intent:
    """Heuristic-only classification — no LLM call spent deciding which LLM to call."""
    lowered = text.lower()
    word_count = len(text.split())
    if word_count > _SIMPLE_WORD_THRESHOLD:
        return Intent.COMPLEX
    if any(keyword in lowered for keyword in _COMPLEX_KEYWORDS):
        return Intent.COMPLEX
    if "?" in text and word_count > 6:
        return Intent.COMPLEX
    return Intent.SIMPLE


class Router:
    def __init__(
        self,
        cfg: Config,
        providers: dict[str, LLMProvider],
        resolver: ModelResolver,
    ) -> None:
        self._cfg = cfg
        self._providers = providers
        self._resolver = resolver

    # -- model selection ----------------------------------------------------

    def _preferences(self, provider: str, *, reasoning: bool) -> list[str]:
        if provider == "gemini":
            return self._cfg.gemini_reasoning_models if reasoning else self._cfg.gemini_fast_models
        if provider == "groq":
            return self._cfg.groq_reasoning_models if reasoning else self._cfg.groq_fast_models
        return []

    def _resolve(self, provider: str, *, reasoning: bool) -> ModelChoice | None:
        prefs = self._preferences(provider, reasoning=reasoning)
        model = self._resolver.resolve(provider, prefs) or (prefs[0] if prefs else None)
        if not model:
            return None
        return ModelChoice(provider=provider, model=model)

    def choose(self, intent: Intent) -> ModelChoice:
        """The first provider/model to try for this turn."""
        if intent is Intent.COMPLEX:
            choice = self._resolve(self._cfg.primary_llm, reasoning=True)
        else:
            choice = self._resolve(self._cfg.fast_llm, reasoning=False)
        if choice is not None:
            return choice
        # No preferences configured for the chosen provider at all — fall
        # back to whatever the primary provider name is, with an empty
        # model; the provider call will raise a clear LLMError rather than
        # this function silently returning nothing.
        fallback_provider = self._cfg.primary_llm if intent is Intent.COMPLEX else self._cfg.fast_llm
        return ModelChoice(provider=fallback_provider, model="")

    def providers_in_order(self, primary: ModelChoice) -> list[ModelChoice]:
        """`primary` first, then the configured fallback, then any other
        provider we know about — so a single misconfigured FALLBACK_LLM
        doesn't remove real redundancy that exists (spec §7).
        """
        order = [primary]
        seen = {primary.provider}

        fallback_name = self._cfg.fallback_llm
        if fallback_name not in seen:
            choice = self._resolve(fallback_name, reasoning=True)
            if choice is not None:
                order.append(choice)
                seen.add(fallback_name)

        for name in self._providers:
            if name in seen:
                continue
            choice = self._resolve(name, reasoning=True)
            if choice is not None:
                order.append(choice)
                seen.add(name)

        return order

    # -- execution with fallback --------------------------------------------

    async def chat(
        self,
        messages: list[Message],
        *,
        intent: Intent,
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
        temperature: float = 0.7,
        max_output_tokens: int = 2048,
    ) -> LLMResponse:
        """One logical turn: pick a provider, call it, fall back on failure.

        Spec §7: any `LLMError` (retryable or not — a request Gemini rejects
        might still be fine for Groq) or transport failure moves to the next
        provider in the chain. Only when every configured, available
        provider has failed does this raise, so the caller always gets a
        clear, actionable error instead of silence (spec §32).
        """
        primary = self.choose(intent)
        chain = self.providers_in_order(primary)
        available = [
            choice
            for choice in chain
            if self._providers.get(choice.provider) is not None
            and self._providers[choice.provider].available
        ]

        if not available:
            raise LLMError(
                "No LLM provider is configured. Set GEMINI_API_KEY or GROQ_API_KEY in .env.",
                retryable=False,
            )

        last_error: LLMError | None = None
        for choice in available:
            provider = self._providers[choice.provider]
            try:
                return await provider.chat(
                    messages,
                    tools=tools,
                    model=choice.model or None,
                    system=system,
                    temperature=temperature,
                    max_output_tokens=max_output_tokens,
                )
            except LLMError as exc:
                # "Request too large" is not a provider failure — it means we sent
                # a small model more than its per-minute budget. Groq's fast model
                # allows 6k TPM while its reasoning model allows 12k, so the same
                # request often succeeds on the bigger model. Try that before
                # failing over to a different provider entirely.
                if "413" in str(exc) or "too large" in str(exc).lower():
                    bigger = self._resolve(choice.provider, reasoning=True)
                    if bigger is not None and bigger.model != choice.model:
                        log.info(
                            "request too large for %s; retrying on %s",
                            choice.model,
                            bigger.model,
                        )
                        try:
                            return await provider.chat(
                                messages,
                                tools=tools,
                                model=bigger.model,
                                system=system,
                                temperature=temperature,
                                max_output_tokens=max_output_tokens,
                            )
                        except LLMError as bigger_exc:
                            exc = bigger_exc

                # A rate-limited provider that told us how long to wait is not a
                # failed provider — it is a busy one. Free tiers reject bursts
                # that would succeed a moment later (Groq commonly asks for a few
                # hundred milliseconds), so waiting once is far better than
                # failing over or giving up. Only one retry: if it is still
                # limited after the wait, the quota is genuinely gone.
                if exc.retry_after is not None:
                    log.info(
                        "provider %s rate-limited; waiting %.2fs as instructed",
                        choice.provider,
                        exc.retry_after,
                    )
                    await asyncio.sleep(exc.retry_after + 0.1)
                    try:
                        return await provider.chat(
                            messages,
                            tools=tools,
                            model=choice.model,
                            system=system,
                            temperature=temperature,
                            max_output_tokens=max_output_tokens,
                        )
                    except LLMError as retry_exc:
                        exc = retry_exc

                log.warning(
                    "provider %s failed (retryable=%s): %s", choice.provider, exc.retryable, exc
                )
                last_error = exc
                continue

        raise LLMError(
            f"All configured LLM providers failed. Last error ({last_error.provider if last_error else '?'}): "
            f"{last_error}",
            retryable=False,
        ) from last_error
