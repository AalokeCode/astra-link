"""CLI entry point (spec §37) — the reference interface layer.

Everything interface-specific lives here and nowhere else: the `You > ` /
`Assistant > ` prompts, readline history, and the terminal confirmation
handler that answers `ConfirmationRequest`s for HIGH_RISK tools. `Assistant`
itself knows nothing about any of this (spec §38).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import readline  # noqa: F401 - imported for its input() line-editing side effect
import sys
from dataclasses import dataclass

from app.agent.agent import Assistant
from app.agent.permissions import ConfirmationRequest, PermissionBroker
from app.agent.router import Router
from app.config import Config, load_config
from app.llm.base import LLMProvider
from app.llm.gemini import GeminiProvider
from app.llm.groq import GroqProvider
from app.llm.models import ModelResolver
from app.logging_setup import configure_logging
from app.memory.database import Database
from app.tools.loader import load_tools
from app.voice.gemini_live import LiveQuotaGuard
from app.voice.link_gateway import LinkGateway
from app.tools.registry import ToolRegistry

log = logging.getLogger(__name__)

HISTORY_FILENAME = ".assistant_history"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="astra-link",
        description="Tunnel-native ASTRA web assistant.",
    )
    parser.add_argument("--debug", action="store_true", help="Verbose console logging.")
    parser.add_argument("-c", "--command", help="Run one command non-interactively and exit.")
    parser.add_argument("--prune", action="store_true", help="Prune memory past retention and exit.")
    parser.add_argument("--serve", action="store_true", help="Run the web app and Live gateway.")
    parser.add_argument("--host", default="127.0.0.1", help="Gateway listen host.")
    parser.add_argument("--port", type=int, default=8080, help="Gateway listen port.")
    return parser


# -- terminal styling (raw ANSI, no dependency) ------------------------------


def _style(code: str, text: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"{code}{text}\033[0m"


def _you_prompt() -> str:
    return _style("\033[1;36m", "You") + " > "


def _assistant_prefix() -> str:
    return _style("\033[1;32m", "Assistant") + " > "


# -- confirmation handler (spec §31: implemented independently of the LLM) --


async def _terminal_confirm(request: ConfirmationRequest) -> bool:
    """The only thing allowed to approve a HIGH_RISK tool.

    Fails closed when stdin isn't a TTY (a background/non-interactive
    invocation must never silently approve something destructive).
    """
    if not sys.stdin.isatty():
        print(
            f"[confirmation required for {request.tool_name} but stdin is not a terminal — denying]",
            file=sys.stderr,
        )
        return False
    print(f"\n{_style('\033[1;33m', 'CONFIRM')}  {request.summary}")
    try:
        answer = input("   Allow this? [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def _build_providers(cfg: Config) -> dict[str, LLMProvider]:
    return {
        "gemini": GeminiProvider(cfg.gemini_api_key),
        "groq": GroqProvider(cfg.groq_api_key),
    }


@dataclass
class Runtime:
    cfg: Config
    db: Database
    assistant: Assistant
    tool_registry: ToolRegistry
    providers: dict[str, LLMProvider]

    async def shutdown(self) -> None:
        for provider in self.providers.values():
            await provider.aclose()
        self.db.close()


async def _startup(cfg: Config) -> Runtime:
    configure_logging(cfg)
    db = Database(cfg.db_path)

    removed = db.prune(cfg.memory_retention_days)
    if any(removed.values()):
        log.info("pruned old memory on startup: %s", removed)

    tool_registry = load_tools(cfg)

    providers = _build_providers(cfg)
    if not any(p.available for p in providers.values()):
        print(
            "No LLM provider is configured — both GEMINI_API_KEY and GROQ_API_KEY are empty.\n"
            "Add at least one to the project's .env file (see .env.example), then try again.",
            file=sys.stderr,
        )
        for provider in providers.values():
            await provider.aclose()
        db.close()
        sys.exit(1)

    resolver = ModelResolver(cfg.model_cache_path)
    await resolver.refresh(list(providers.values()))

    router = Router(cfg, providers, resolver)
    broker = PermissionBroker(
        require_confirmation=cfg.require_confirmation,
        confirm_handler=_terminal_confirm,
    )
    assistant = Assistant(cfg, db, tool_registry, router, broker)
    return Runtime(cfg=cfg, db=db, assistant=assistant, tool_registry=tool_registry, providers=providers)


# -- REPL ---------------------------------------------------------------


def _handle_command(text: str, runtime: Runtime, source: str) -> bool:
    """Returns True if the REPL should exit."""
    cmd = text.split(maxsplit=1)[0]

    if cmd in {"/exit", "/quit"}:
        return True
    if cmd == "/new":
        runtime.assistant.new_conversation(source)
        print("Started a new conversation.")
        return False
    if cmd == "/tools":
        names = runtime.tool_registry.names()
        print(f"{len(names)} tool(s) available:")
        for name in names:
            print(f"  {name}")
        return False
    if cmd == "/status":
        cfg = runtime.cfg
        print(f"  data dir : {cfg.data_dir}")
        print(f"  database : {cfg.db_path}")
        print(f"  gemini   : {'configured' if cfg.has_provider('gemini') else 'not configured'}")
        print(f"  groq     : {'configured' if cfg.has_provider('groq') else 'not configured'}")
        print(f"  max iterations per turn: {cfg.max_agent_iterations}")
        print(f"  confirmation required for HIGH_RISK tools: {cfg.require_confirmation}")
        return False
    if cmd == "/help":
        print("  /new     start a fresh conversation")
        print("  /tools   list available tools")
        print("  /status  show configuration status")
        print("  /help    show this message")
        print("  /exit    quit")
        return False

    print(f"Unknown command: {cmd}. Type /help for a list.")
    return False


async def _repl(runtime: Runtime) -> None:
    source = "cli"
    history_path = runtime.cfg.data_dir / HISTORY_FILENAME
    try:
        readline.read_history_file(history_path)
    except (OSError, FileNotFoundError):
        pass

    print(f"{runtime.cfg.app_name} — type /help for commands, /exit to quit.")
    try:
        while True:
            try:
                text = input(_you_prompt()).strip()
            except EOFError:
                print()
                break
            except KeyboardInterrupt:
                print()
                continue

            if not text:
                continue
            if text.startswith("/"):
                if _handle_command(text, runtime, source):
                    break
                continue

            try:
                reply = await runtime.assistant.process_input(text, source=source)
            except Exception:
                log.exception("unhandled error while processing input")
                reply = "Something went wrong on my end handling that. Check the logs for details."
            print(f"{_assistant_prefix()}{reply}")
    finally:
        try:
            readline.write_history_file(history_path)
        except OSError:
            pass


# -- entry points ---------------------------------------------------------


async def _async_main(args: argparse.Namespace) -> int:
    cfg = load_config()
    if args.debug:
        cfg = dataclasses.replace(cfg, debug=True)

    if args.serve:
        configure_logging(cfg)
        runtime = await _startup(cfg)
        gateway = LinkGateway(
            cfg,
            runtime.assistant,
            host=args.host,
            port=args.port,
            quota=LiveQuotaGuard(cfg),
        )
        try:
            await gateway.start()
            print(
                f"ASTRA Link ready at http://{args.host}:{args.port}. "
                "Ctrl-C to stop."
            )
            await gateway.serve_forever()
        except asyncio.CancelledError:
            pass
        finally:
            await gateway.aclose()
            await runtime.shutdown()
        return 0

    if args.prune:
        configure_logging(cfg)
        db = Database(cfg.db_path)
        removed = db.prune(cfg.memory_retention_days)
        db.close()
        print(f"Pruned: {removed}")
        return 0

    runtime = await _startup(cfg)
    try:
        if args.command:
            reply = await runtime.assistant.process_input(args.command, source="cli")
            print(reply)
        else:
            await _repl(runtime)
    finally:
        await runtime.shutdown()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_async_main(args))
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
