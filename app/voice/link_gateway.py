"""Single-origin HTTPS/WebSocket gateway for ASTRA Link web clients."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from app.agent.agent import Assistant
from app.config import Config
from app.voice.gemini_live import GeminiLiveSession, LiveQuotaExceeded, LiveQuotaGuard

log = logging.getLogger(__name__)

LINK_INPUT_SAMPLE_RATE = 16_000
AUTH_TIMEOUT_SECONDS = 10


def parse_auth_message(raw: str) -> str:
    """Return a token from the mandatory first WebSocket message."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("authentication message must be valid JSON") from exc
    if not isinstance(payload, Mapping) or payload.get("type") != "auth":
        raise ValueError("the first message must authenticate the session")
    token = str(payload.get("token") or "").strip()
    if not token:
        raise ValueError("session token is empty")
    return token


def safe_static_path(root: Path, requested: str) -> Path | None:
    """Resolve a static path without allowing traversal outside the web build."""
    relative = requested.lstrip("/") or "index.html"
    try:
        candidate = (root / relative).resolve()
        candidate.relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate


class _LinkSink:
    def __init__(self, socket: web.WebSocketResponse) -> None:
        self._socket = socket
        self._lock = asyncio.Lock()

    async def send_audio(self, pcm: bytes) -> None:
        if not self._socket.closed:
            async with self._lock:
                await self._socket.send_bytes(pcm)

    async def clear_audio(self) -> None:
        await self.send_event({"type": "clear"})

    async def send_event(self, event: Mapping[str, Any]) -> None:
        if not self._socket.closed:
            async with self._lock:
                await self._socket.send_json(event)


@web.middleware
async def security_headers(
    request: web.Request,
    handler: Any,
) -> web.StreamResponse:
    response = await handler(request)
    if not response.prepared:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), geolocation=()")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self' ws: wss:; img-src 'self' data:; media-src 'self' blob:; "
            "worker-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
        )
    return response


class LinkGateway:
    """Serves the installable web app and Gemini Live transport on one origin."""

    def __init__(
        self,
        cfg: Config,
        assistant: Assistant,
        *,
        host: str = "127.0.0.1",
        port: int = 8080,
        quota: LiveQuotaGuard | None = None,
    ) -> None:
        self._cfg = cfg
        self._assistant = assistant
        self._host = host
        self._port = port
        self._quota = quota or LiveQuotaGuard(cfg)
        self._http: aiohttp.ClientSession | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._closed = asyncio.Event()

    async def start(self) -> None:
        if not self._cfg.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is required for ASTRA Link")
        if len(self._cfg.link_session_token) < 32:
            raise RuntimeError("LINK_SESSION_TOKEN must be at least 32 characters")

        self._http = aiohttp.ClientSession()
        app = web.Application(
            client_max_size=2 * 1024 * 1024,
            middlewares=[security_headers],
        )
        app.router.add_get("/v1/live", self._live)
        app.router.add_get("/health", self._health)
        app.router.add_get("/{path:.*}", self._static)
        self._runner = web.AppRunner(app, access_log=None)
        try:
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self._host, self._port)
            await self._site.start()
        except Exception:
            await self._runner.cleanup()
            await self._http.close()
            self._runner = None
            self._http = None
            raise
        log.info("ASTRA Link gateway listening on %s:%d", self._host, self._port)

    async def serve_forever(self) -> None:
        await self._closed.wait()

    def _origin_allowed(self, request: web.Request) -> bool:
        origin = request.headers.get("Origin", "").rstrip("/")
        if not origin:
            return True
        allowed = {item.rstrip("/") for item in self._cfg.link_allowed_origins}
        if self._cfg.link_public_url:
            allowed.add(self._cfg.link_public_url)
        return origin in allowed

    async def _authenticate(self, socket: web.WebSocketResponse) -> bool:
        try:
            message = await asyncio.wait_for(
                socket.receive(), timeout=AUTH_TIMEOUT_SECONDS
            )
            if message.type is not aiohttp.WSMsgType.TEXT:
                raise ValueError("authentication must be a text message")
            supplied = parse_auth_message(str(message.data))
        except (TimeoutError, ValueError) as exc:
            await socket.send_json({"type": "error", "message": str(exc)})
            await socket.close(code=1008, message=b"authentication required")
            return False
        if not hmac.compare_digest(supplied, self._cfg.link_session_token):
            await socket.send_json({"type": "error", "message": "Invalid session token"})
            await socket.close(code=1008, message=b"invalid session token")
            return False
        await socket.send_json({"type": "authenticated"})
        return True

    async def _live(self, request: web.Request) -> web.StreamResponse:
        if not self._origin_allowed(request):
            raise web.HTTPForbidden(text="Origin is not allowed")
        socket = web.WebSocketResponse(heartbeat=20, max_msg_size=2 * 1024 * 1024)
        await socket.prepare(request)
        if not await self._authenticate(socket):
            return socket

        sink = _LinkSink(socket)
        assert self._http is not None
        session: GeminiLiveSession | None = None
        try:
            async with self._quota.lease() as allowed_seconds:
                async with asyncio.timeout(allowed_seconds):
                    session = GeminiLiveSession(
                        self._cfg,
                        self._assistant,
                        self._http,
                        sink,
                        source="link:web",
                    )
                    await session.connect()
                    await session.greet()
                    async for message in socket:
                        if message.type is aiohttp.WSMsgType.BINARY:
                            await session.send_audio(
                                bytes(message.data),
                                sample_rate=LINK_INPUT_SAMPLE_RATE,
                            )
                        elif message.type is aiohttp.WSMsgType.TEXT:
                            payload = json.loads(message.data)
                            if payload.get("type") == "text":
                                await session.send_text(str(payload.get("text") or ""))
                        elif message.type is aiohttp.WSMsgType.ERROR:
                            raise socket.exception() or RuntimeError(
                                "ASTRA Link WebSocket failed"
                            )
        except LiveQuotaExceeded as exc:
            await sink.send_event({"type": "error", "message": str(exc)})
            await socket.close(code=1013)
        except TimeoutError:
            await sink.send_event(
                {"type": "error", "message": "Configured session limit reached"}
            )
            await socket.close(code=1000)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("ASTRA Link live session failed")
            with contextlib.suppress(Exception):
                await sink.send_event({"type": "error", "message": str(exc)[:200]})
            if not socket.closed:
                await socket.close(code=1011)
        finally:
            if session is not None:
                await session.aclose()
        return socket

    async def _health(self, request: web.Request) -> web.Response:
        del request
        return web.json_response(
            {
                "status": "ok",
                "model": self._cfg.gemini_live_model,
                "quota": asdict(await self._quota.snapshot()),
                "transport": "authenticated-websocket",
                "web_ready": (self._cfg.web_dist_dir / "index.html").is_file(),
            }
        )

    async def _static(self, request: web.Request) -> web.StreamResponse:
        root = self._cfg.web_dist_dir
        if not (root / "index.html").is_file():
            raise web.HTTPServiceUnavailable(
                text="Web build missing. Run: cd web && npm run build"
            )
        candidate = safe_static_path(root, request.match_info.get("path", ""))
        if candidate is None:
            raise web.HTTPNotFound()
        if not candidate.is_file():
            candidate = root / "index.html"
        response = web.FileResponse(candidate)
        if candidate.name in {"index.html", "sw.js"}:
            response.headers["Cache-Control"] = "no-cache"
        elif "_next/static" in candidate.as_posix():
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    async def aclose(self) -> None:
        self._closed.set()
        if self._runner is not None:
            await self._runner.cleanup()
        if self._http is not None:
            await self._http.close()


__all__ = [
    "AUTH_TIMEOUT_SECONDS",
    "LINK_INPUT_SAMPLE_RATE",
    "LinkGateway",
    "parse_auth_message",
    "safe_static_path",
]
