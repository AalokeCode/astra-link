"""Runtime model resolution.

Spec §7: don't hard-code model IDs. Providers retire them on their own
schedule, and a constant in the source turns that into a crash. Instead we ask
each provider what it currently serves, then walk the configured preference
list and take the first ID that's actually available.

The answer is cached on disk for a day so we pay one extra HTTP call daily,
not one per launch.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from app.llm.base import LLMError, LLMProvider

log = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class ModelChoice:
    provider: str
    model: str

    def __str__(self) -> str:
        return f"{self.provider}/{self.model}"


class ModelResolver:
    def __init__(self, cache_path: Path) -> None:
        self._cache_path = cache_path
        self._catalog: dict[str, list[str]] = {}
        self._loaded_at: float = 0.0

    # -- cache ------------------------------------------------------------

    def _read_cache(self) -> bool:
        try:
            blob = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if time.time() - blob.get("fetched_at", 0) > CACHE_TTL_SECONDS:
            return False
        self._catalog = blob.get("catalog", {})
        self._loaded_at = blob["fetched_at"]
        return bool(self._catalog)

    def _write_cache(self) -> None:
        try:
            self._cache_path.write_text(
                json.dumps({"fetched_at": time.time(), "catalog": self._catalog}),
                encoding="utf-8",
            )
        except OSError as exc:  # a read-only data dir shouldn't be fatal
            log.warning("could not write model cache: %s", exc)

    # -- discovery --------------------------------------------------------

    async def refresh(self, providers: list[LLMProvider], *, force: bool = False) -> None:
        if not force and self._read_cache():
            return
        for provider in providers:
            if not provider.available:
                continue
            try:
                self._catalog[provider.name] = await provider.list_models()
            except LLMError as exc:
                # Discovery failing must not block startup — fall back to
                # trusting the configured preference verbatim.
                log.warning("model discovery failed for %s: %s", provider.name, exc)
                self._catalog.setdefault(provider.name, [])
        self._write_cache()

    def available_for(self, provider: str) -> list[str]:
        return self._catalog.get(provider, [])

    def resolve(self, provider: str, preferences: list[str]) -> str | None:
        """First preference the provider actually serves.

        Falls back to the first preference when the catalog is empty (discovery
        failed or was skipped) — better to try and get a clear API error than
        to refuse to start.
        """
        catalog = self._catalog.get(provider)
        if not catalog:
            return preferences[0] if preferences else None

        available = set(catalog)
        for pref in preferences:
            if pref in available:
                return pref

        # Nothing matched exactly. Prefix-match handles dated variants such as
        # `llama-3.3-70b-versatile-0125` standing in for the base name.
        for pref in preferences:
            for candidate in catalog:
                if candidate.startswith(pref):
                    log.info("model %r resolved to %r by prefix", pref, candidate)
                    return candidate

        log.warning("no configured model available for %s; catalog=%d entries", provider, len(catalog))
        return None
