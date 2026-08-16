"""Configuration loading.

Reads `.env` with the standard library rather than pydantic-settings — the file
format we need is `KEY=value` with `#` comments, which is a dozen lines of
parsing, not a dependency.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Populate os.environ from a .env file. Real environment wins."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        # An explicitly exported variable overrides the file, which is what
        # lets you do `GEMINI_API_KEY=... assistant` for a one-off.
        os.environ.setdefault(key, value)


def _str(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "").strip())
    except ValueError:
        return default


def _csv(key: str, default: str = "") -> list[str]:
    return [item.strip() for item in _str(key, default).split(",") if item.strip()]


def _paths(key: str, default: str) -> list[Path]:
    """Parse a colon-separated path list, expanding `~` and resolving symlinks.

    Roots are resolved here so the sandbox compares realpath-to-realpath. If a
    configured root does not exist it is dropped rather than silently creating
    an unenforceable rule.
    """
    roots: list[Path] = []
    for chunk in _str(key, default).split(":"):
        chunk = chunk.strip()
        if not chunk:
            continue
        candidate = Path(chunk).expanduser()
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if resolved.is_dir():
            roots.append(resolved)
    return roots


@dataclass(frozen=True)
class Config:
    app_name: str

    primary_llm: str
    fast_llm: str
    fallback_llm: str
    gemini_api_key: str
    groq_api_key: str
    gemini_reasoning_models: list[str]
    gemini_fast_models: list[str]
    groq_reasoning_models: list[str]
    groq_fast_models: list[str]

    gemini_live_model: str
    gemini_live_voice: str
    live_max_concurrent_sessions: int
    live_max_session_seconds: int
    live_max_daily_minutes: int
    live_context_trigger_tokens: int
    live_context_target_tokens: int
    live_transcriptions: bool
    link_public_url: str
    link_session_token: str
    link_allowed_origins: list[str]
    web_dist_dir: Path

    data_dir: Path
    memory_retention_days: int
    log_max_bytes: int
    log_backup_count: int

    allowed_dirs: list[Path]

    enable_shell_tools: bool
    enable_web_search: bool
    enable_reminders: bool
    require_confirmation: bool
    max_agent_iterations: int
    shell_timeout_seconds: int

    timezone: ZoneInfo

    debug: bool = field(default=False)

    # -- derived paths ----------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "assistant.db"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def model_cache_path(self) -> Path:
        return self.data_dir / "model_cache.json"

    def has_provider(self, name: str) -> bool:
        return bool({"gemini": self.gemini_api_key, "groq": self.groq_api_key}.get(name, ""))


def load_config(env_file: Path | None = None) -> Config:
    _load_dotenv(env_file or PROJECT_ROOT / ".env")

    data_dir = Path(_str("ASSISTANT_DATA_DIR", "~/.astra-link")).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "logs").mkdir(exist_ok=True)

    # The data directory holds conversation history and tool-call arguments,
    # and `.env` holds live API keys. Both default to 0644 under a normal
    # umask, which makes them readable by every account on the machine. Narrow
    # them on every start so a fresh checkout or a restored backup cannot
    # quietly leave them exposed.
    for path, mode in ((data_dir, 0o700), (PROJECT_ROOT / ".env", 0o600)):
        try:
            if path.exists():
                path.chmod(mode)
        except OSError:  # e.g. a read-only mount; not worth failing startup
            pass

    tz_name = _str("TIMEZONE", "Asia/Kolkata")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    return Config(
        app_name=_str("APP_NAME", "ASTRA Link"),
        primary_llm=_str("PRIMARY_LLM", "gemini"),
        fast_llm=_str("FAST_LLM", "groq"),
        fallback_llm=_str("FALLBACK_LLM", "groq"),
        gemini_api_key=_str("GEMINI_API_KEY"),
        groq_api_key=_str("GROQ_API_KEY"),
        gemini_reasoning_models=_csv(
            "GEMINI_REASONING_MODELS", "gemini-3.5-flash,gemini-2.5-flash,gemini-2.5-pro"
        ),
        gemini_fast_models=_csv(
            "GEMINI_FAST_MODELS", "gemini-3.5-flash-lite,gemini-2.5-flash-lite,gemini-2.5-flash"
        ),
        groq_reasoning_models=_csv(
            "GROQ_REASONING_MODELS", "llama-3.3-70b-versatile,openai/gpt-oss-120b"
        ),
        groq_fast_models=_csv("GROQ_FAST_MODELS", "llama-3.1-8b-instant,openai/gpt-oss-20b"),
        gemini_live_model=_str("GEMINI_LIVE_MODEL", "gemini-3.1-flash-live-preview"),
        gemini_live_voice=_str("GEMINI_LIVE_VOICE", "Aoede"),
        live_max_concurrent_sessions=max(1, _int("LIVE_MAX_CONCURRENT_SESSIONS", 1)),
        live_max_session_seconds=max(60, _int("LIVE_MAX_SESSION_SECONDS", 600)),
        live_max_daily_minutes=max(1, _int("LIVE_MAX_DAILY_MINUTES", 60)),
        live_context_trigger_tokens=max(
            4_000, _int("LIVE_CONTEXT_TRIGGER_TOKENS", 25_000)
        ),
        live_context_target_tokens=max(
            1_000, _int("LIVE_CONTEXT_TARGET_TOKENS", 8_000)
        ),
        live_transcriptions=_bool("LIVE_TRANSCRIPTIONS", True),
        link_public_url=_str("LINK_PUBLIC_URL").rstrip("/"),
        link_session_token=_str("LINK_SESSION_TOKEN"),
        link_allowed_origins=_csv(
            "LINK_ALLOWED_ORIGINS",
            "http://127.0.0.1:8080,http://localhost:8080,"
            "http://127.0.0.1:3000,http://localhost:3000",
        ),
        web_dist_dir=Path(
            _str("WEB_DIST_DIR") or str(PROJECT_ROOT / "web" / "out")
        ).expanduser().resolve(),
        data_dir=data_dir,
        memory_retention_days=_int("MEMORY_RETENTION_DAYS", 90),
        log_max_bytes=_int("LOG_MAX_BYTES", 2_000_000),
        log_backup_count=_int("LOG_BACKUP_COUNT", 3),
        allowed_dirs=_paths("ALLOWED_DIRS", "~/Documents/Projects"),
        enable_shell_tools=_bool("ENABLE_SHELL_TOOLS", True),
        enable_web_search=_bool("ENABLE_WEB_SEARCH", True),
        enable_reminders=_bool("ENABLE_REMINDERS", True),
        require_confirmation=_bool("REQUIRE_CONFIRMATION_FOR_DANGEROUS_TOOLS", True),
        max_agent_iterations=_int("MAX_AGENT_ITERATIONS", 8),
        shell_timeout_seconds=_int("SHELL_TIMEOUT_SECONDS", 30),
        timezone=tz,
        debug=_bool("DEBUG", False),
    )
