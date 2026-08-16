"""Tool discovery.

Other agents own the concrete tool modules (`filesystem.py`, `shell.py`,
`git.py`, `claude_code.py`, `system.py`, `web.py`, `documents.py`,
`reminders.py`). Each registers itself with `app.tools.registry.registry`
purely by being imported — `@registry.tool` decorators run at import time. So
loading tools is just importing the right modules and letting that happen.

A module that doesn't exist yet (an agent hasn't landed it) is a real gap,
not something to hide — hence WARNING, not silent skip.
"""

from __future__ import annotations

import importlib
import logging

from app.config import Config
from app.tools.registry import ToolRegistry, registry

log = logging.getLogger(__name__)

# Always attempted, regardless of feature flags.
_ALWAYS_ON_MODULES: tuple[str, ...] = (
    "app.tools.builtin",
    "app.tools.filesystem",
    "app.tools.git",
    "app.tools.claude_code",
    "app.tools.system",
    "app.tools.documents",
)


def load_tools(cfg: Config) -> ToolRegistry:
    """Import every tool module that should be active, then return the registry.

    Returns the same module-level `registry` singleton every tool module
    registers into — this function's job is purely to trigger those
    registrations, not to own a separate registry instance.
    """
    modules = list(_ALWAYS_ON_MODULES)
    if cfg.enable_shell_tools:
        modules.append("app.tools.shell")
    else:
        log.info("shell tools disabled by config (ENABLE_SHELL_TOOLS=false)")
    if cfg.enable_web_search:
        modules.append("app.tools.web")
    else:
        log.info("web search disabled by config (ENABLE_WEB_SEARCH=false)")
    if cfg.enable_reminders:
        modules.append("app.tools.reminders")
    else:
        log.info("reminders disabled by config (ENABLE_REMINDERS=false)")

    for module_name in modules:
        try:
            importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            # Covers both "the module file doesn't exist yet" and "it exists
            # but imports something that doesn't exist yet" — either way the
            # tools it would have registered are simply unavailable this run,
            # and that is worth a loud warning, not a silent gap.
            log.warning(
                "tool module %s is not available yet, skipping (%s: %s)",
                module_name,
                exc.name,
                exc,
            )
        except Exception:
            log.exception("tool module %s failed to import", module_name)
            raise

    log.info("loaded tools: %s", ", ".join(registry.names()) or "(none)")
    return registry
