"""Tool registry: declaration, schema generation, validation, dispatch.

One declaration per tool produces both the JSON Schema the model sees and the
validator that guards execution — they cannot drift apart, which is the whole
reason pydantic is worth its footprint here.

Nothing in this module executes anything. It validates arguments and hands a
verified call to the permission broker; the broker decides whether it runs
(spec §30).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any

from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)


class RiskLevel(IntEnum):
    """Spec §10. Ordered so comparisons like `>= HIGH_RISK` read naturally."""

    READ_ONLY = 0
    LOW_RISK_WRITE = 1
    HIGH_RISK = 2


class ToolGroup(str, Enum):
    """Coarse buckets used to gate which schemas ship with each request.

    Sending all ~30 schemas every turn costs ~2,500 input tokens before any
    conversation history. Gating by group cuts most of that.
    """

    CORE = "core"
    FILESYSTEM = "filesystem"
    GIT = "git"
    SHELL = "shell"
    CLAUDE_CODE = "claude_code"
    REMINDERS = "reminders"
    DOCUMENTS = "documents"
    WEB = "web"
    SYSTEM = "system"


class ToolExecutionError(Exception):
    """Raised by a tool to report a clean, model-readable failure."""


@dataclass
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[..., Any | Awaitable[Any]]
    risk: RiskLevel
    group: ToolGroup
    confirm_template: str | None = None

    _schema: dict[str, Any] | None = field(default=None, repr=False)

    def schema(self) -> dict[str, Any]:
        if self._schema is None:
            self._schema = flatten_schema(self.args_model.model_json_schema())
        return self._schema

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.schema(),
        }

    def describe_call(self, args: dict[str, Any]) -> str:
        """Human-readable summary shown in a confirmation prompt."""
        if self.confirm_template:
            try:
                return self.confirm_template.format(**args)
            except (KeyError, IndexError):
                pass
        rendered = ", ".join(f"{k}={v!r}" for k, v in args.items())
        return f"{self.name}({rendered})"


def flatten_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline `$ref`/`$defs` and drop annotation-only noise.

    Pydantic emits `$defs` plus `$ref` for nested models and enums. Neither
    provider's function-calling parser handles references reliably, so we
    resolve them into a self-contained schema once, at registration time.
    """
    defs = schema.get("$defs") or schema.get("definitions") or {}

    def resolve(node: Any, seen: frozenset[str] = frozenset()) -> Any:
        if isinstance(node, list):
            return [resolve(item, seen) for item in node]
        if not isinstance(node, dict):
            return node

        if "$ref" in node:
            ref = node["$ref"]
            key = ref.rsplit("/", 1)[-1]
            if key in seen:
                # Recursive model: degrade to an untyped object instead of
                # recursing forever.
                return {"type": "object"}
            target = defs.get(key)
            if target is None:
                return {"type": "object"}
            merged = resolve(target, seen | {key})
            # Sibling keys alongside $ref (e.g. a description) win.
            extra = {k: resolve(v, seen) for k, v in node.items() if k != "$ref"}
            return {**merged, **extra}

        out: dict[str, Any] = {}
        for k, v in node.items():
            if k in {"$defs", "definitions", "title"}:
                # These are schema *keywords* here. Dropping "title" removes
                # pydantic's cosmetic annotation, which providers ignore anyway.
                continue
            if k == "properties" and isinstance(v, dict):
                # Inside `properties` the keys are user-chosen field names, not
                # keywords — a field genuinely called "title" or "definitions"
                # must survive. Recurse into the values only. Skipping this
                # distinction silently deletes the field while leaving it in
                # `required`, which Gemini rejects outright.
                out[k] = {name: resolve(sub, seen) for name, sub in v.items()}
                continue
            out[k] = resolve(v, seen)
        return out

    resolved = resolve(schema)
    resolved.setdefault("type", "object")
    resolved.setdefault("properties", {})
    return resolved


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    # -- declaration ------------------------------------------------------

    def tool(
        self,
        *,
        name: str,
        description: str,
        args_model: type[BaseModel],
        risk: RiskLevel,
        group: ToolGroup,
        confirm_template: str | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            if name in self._tools:
                raise ValueError(f"duplicate tool name: {name}")
            self._tools[name] = Tool(
                name=name,
                description=description,
                args_model=args_model,
                handler=fn,
                risk=risk,
                group=group,
                confirm_template=confirm_template,
            )
            return fn

        return decorator

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    # -- lookup -----------------------------------------------------------

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def definitions(self, groups: set[ToolGroup] | None = None) -> list[dict[str, Any]]:
        """Schemas to send with a request, optionally narrowed to some groups.

        CORE is always included so the model never loses its baseline
        capabilities regardless of how gating classified the turn.
        """
        tools = self.all()
        if groups is not None:
            allowed = groups | {ToolGroup.CORE}
            tools = [t for t in tools if t.group in allowed]
        return [t.definition() for t in sorted(tools, key=lambda t: t.name)]

    # -- validation & dispatch -------------------------------------------

    def validate(self, name: str, raw_args: dict[str, Any]) -> BaseModel:
        tool = self.get(name)
        if tool is None:
            raise ToolExecutionError(
                f"unknown tool {name!r}; available: {', '.join(self.names())}"
            )
        # Defence in depth: providers vary in how they represent "no arguments"
        # (missing, null, empty string). Normalise here too so one provider
        # quirk cannot break every zero-arg tool.
        if raw_args is None:
            raw_args = {}
        try:
            return tool.args_model.model_validate(raw_args)
        except ValidationError as exc:
            # Compact the pydantic report — the model reads this and retries,
            # so it needs to be actionable, not exhaustive.
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
                for err in exc.errors()[:5]
            )
            raise ToolExecutionError(f"invalid arguments for {name}: {problems}") from exc

    async def execute(self, name: str, args: BaseModel) -> Any:
        """Run a tool whose arguments are already validated and authorized.

        Callers must go through the permission broker first — this method does
        not check risk levels.
        """
        tool = self.get(name)
        if tool is None:
            raise ToolExecutionError(f"unknown tool {name!r}")

        if inspect.iscoroutinefunction(tool.handler):
            return await tool.handler(args)
        # Sync tools (subprocess, sqlite, file IO) run in a worker thread so a
        # slow call can't stall the event loop. Dispatch the callable itself —
        # calling it here first would defeat the point.
        return await asyncio.to_thread(tool.handler, args)


registry = ToolRegistry()
