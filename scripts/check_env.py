#!/usr/bin/env python3
"""Preflight check: environment, sandbox roots, and live provider auth.

Run this before trusting anything else:

    .venv/bin/python scripts/check_env.py

It hits both provider APIs with a real request, because "the key is set" and
"the key works" are different claims and only the second one matters.
"""

from __future__ import annotations

import asyncio
import platform
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.config import load_config  # noqa: E402

GEMINI_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"

OK = "\033[32m✓\033[0m"
BAD = "\033[31m✗\033[0m"
WARN = "\033[33m!\033[0m"


def line(mark: str, label: str, detail: str = "") -> None:
    print(f"  {mark} {label}" + (f"  {detail}" if detail else ""))


async def check_gemini(key: str) -> tuple[bool, str]:
    if not key:
        return False, "GEMINI_API_KEY not set"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Key travels in a header, never the query string, so it stays out
            # of proxy logs and shell history.
            resp = await client.get(GEMINI_MODELS_URL, headers={"x-goog-api-key": key})
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {resp.text[:120]}"
        names = [
            m["name"].removeprefix("models/")
            for m in resp.json().get("models", [])
            if "generateContent" in m.get("supportedGenerationMethods", [])
        ]
        return True, f"{len(names)} models w/ generateContent"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


async def check_groq(key: str) -> tuple[bool, str]:
    if not key:
        return False, "GROQ_API_KEY not set"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(GROQ_MODELS_URL, headers={"Authorization": f"Bearer {key}"})
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {resp.text[:120]}"
        return True, f"{len(resp.json().get('data', []))} models"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


async def main() -> int:
    cfg = load_config()
    failures = 0

    print("\nRuntime")
    line(OK, f"Python {platform.python_version()}", f"({sys.executable})")
    line(OK, f"macOS {platform.mac_ver()[0]}", platform.machine())

    print("\nNative tools")
    # These replace ~55 MB of Python dependencies; if any are missing the
    # corresponding tool degrades rather than crashing, so this is informational.
    for name, purpose in [
        ("osascript", "Reminders"),
        ("textutil", "DOCX export"),
        ("cupsfilter", "PDF export"),
        ("git", "git tools"),
        ("pgrep", "Claude Code inspection"),
    ]:
        path = shutil.which(name) or (
            "/usr/sbin/cupsfilter" if name == "cupsfilter" and Path("/usr/sbin/cupsfilter").exists() else None
        )
        line(OK if path else WARN, f"{name:<11}", f"{purpose}" if path else f"MISSING — {purpose} unavailable")

    print("\nSandbox roots")
    if not cfg.allowed_dirs:
        line(BAD, "no readable roots configured", "check ALLOWED_DIRS in .env")
        failures += 1
    for root in cfg.allowed_dirs:
        line(OK, str(root))

    print("\nStorage")
    line(OK, f"data dir {cfg.data_dir}")
    line(OK, f"logs     {cfg.log_dir}")

    print("\nProviders")
    (g_ok, g_msg), (q_ok, q_msg) = await asyncio.gather(
        check_gemini(cfg.gemini_api_key), check_groq(cfg.groq_api_key)
    )
    line(OK if g_ok else BAD, "gemini", g_msg)
    line(OK if q_ok else BAD, "groq  ", q_msg)

    # One working provider is enough to run; zero is not.
    if not (g_ok or q_ok):
        failures += 1
        print(f"\n{BAD} No provider reachable. Set GEMINI_API_KEY or GROQ_API_KEY in .env")
    elif not (g_ok and q_ok):
        print(f"\n{WARN} Only one provider is reachable — fallback routing will not work.")
    else:
        print(f"\n{OK} Both providers reachable.")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
