"""Single-origin HTTPS/WebSocket gateway for ASTRA Link web clients."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from app.agent.agent import Assistant
from app.config import PROJECT_ROOT, Config
from app.integrations.agent_workspace import AgentWorkspaceError, KittyAgentWorkspace
from app.voice.gemini_live import GeminiLiveSession, LiveQuotaExceeded, LiveQuotaGuard

log = logging.getLogger(__name__)

LINK_INPUT_SAMPLE_RATE = 16_000
AUTH_TIMEOUT_SECONDS = 10
CSP_KEY = web.AppKey("content_security_policy", str)
CONFIG_KEY = web.AppKey("config", Config)


def build_content_security_policy(index_path: Path) -> str:
    """Allow only the exact inline bootstrap scripts emitted by Next.js."""
    hashes: list[str] = []
    try:
        markup = index_path.read_text(encoding="utf-8")
    except OSError:
        markup = ""
    for script in re.findall(
        r"<script(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script>",
        markup,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        digest = hashlib.sha256(script.encode("utf-8")).digest()
        hashes.append(f"'sha256-{base64.b64encode(digest).decode('ascii')}'")
    script_sources = " ".join(["'self'", *hashes])
    return (
        f"default-src 'self'; script-src {script_sources}; "
        "style-src 'self' 'unsafe-inline'; connect-src 'self' ws: wss:; "
        "img-src 'self' data:; media-src 'self' blob:; worker-src 'self'; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    )


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


def origin_allowed(cfg: Config, origin: str) -> bool:
    """Accept only the configured browser origins, including the public URL."""
    normalized = origin.rstrip("/")
    if not normalized:
        return True
    allowed = {item.rstrip("/") for item in cfg.link_allowed_origins}
    if cfg.link_public_url:
        allowed.add(cfg.link_public_url)
    return normalized in allowed


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
    origin = request.headers.get("Origin", "")
    is_agent_api = request.path.startswith("/agents")
    if is_agent_api and not origin_allowed(request.app[CONFIG_KEY], origin):
        raise web.HTTPForbidden(text="Origin is not allowed")
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        if not is_agent_api:
            raise
        response = exc
    if is_agent_api and origin:
        response.headers.setdefault("Access-Control-Allow-Origin", origin.rstrip("/"))
        response.headers.setdefault("Access-Control-Allow-Headers", "Authorization, Content-Type")
        response.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        response.headers.setdefault("Access-Control-Max-Age", "600")
        response.headers.setdefault("Vary", "Origin")
    if is_agent_api:
        response.headers.setdefault("Cache-Control", "no-store")
    if not response.prepared:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), geolocation=()")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("Content-Security-Policy", request.app[CSP_KEY])
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
        self._agent_workspace: KittyAgentWorkspace | None = None

    async def start(self) -> None:
        if not self._cfg.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is required for ASTRA Link")
        if len(self._cfg.link_session_token) < 32:
            raise RuntimeError("LINK_SESSION_TOKEN must be at least 32 characters")

        self._agent_workspace = KittyAgentWorkspace(
            self._cfg, default_project=PROJECT_ROOT
        )
        self._http = aiohttp.ClientSession()
        app = web.Application(
            client_max_size=2 * 1024 * 1024,
            middlewares=[security_headers],
        )
        app[CSP_KEY] = build_content_security_policy(
            self._cfg.web_dist_dir / "index.html"
        )
        app[CONFIG_KEY] = self._cfg
        app.router.add_get("/v1/live", self._live)
        app.router.add_route("OPTIONS", "/agents/{tail:.*}", self._agent_options)
        app.router.add_get("/agents/status", self._agents_status)
        app.router.add_post("/agents/instances", self._agents_launch)
        app.router.add_post(
            "/agents/instances/{instance_id}/prompt", self._agents_prompt
        )
        app.router.add_post(
            "/agents/instances/{instance_id}/focus", self._agents_focus
        )
        app.router.add_post(
            "/agents/instances/{instance_id}/interrupt", self._agents_interrupt
        )
        app.router.add_post("/agents/shutdown", self._agents_shutdown)
        app.router.add_delete(
            "/agents/instances/{instance_id}", self._agents_close
        )
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
        return origin_allowed(self._cfg, request.headers.get("Origin", ""))

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
        await socket.send_json(
            {
                "type": "authenticated",
                "transport": {
                    "version": 2,
                    "inputSampleRate": LINK_INPUT_SAMPLE_RATE,
                    "inputFrameMs": 40,
                    "outputSampleRate": 24_000,
                },
            }
        )
        return True

    async def _live(self, request: web.Request) -> web.StreamResponse:
        if not self._origin_allowed(request):
            raise web.HTTPForbidden(text="Origin is not allowed")
        socket = web.WebSocketResponse(
            heartbeat=20,
            compress=False,
            max_msg_size=2 * 1024 * 1024,
            writer_limit=64 * 1024,
        )
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
                            try:
                                payload = json.loads(message.data)
                            except json.JSONDecodeError:
                                await sink.send_event(
                                    {"type": "error", "message": "Invalid client message"}
                                )
                                continue
                            if payload.get("type") == "text":
                                await session.send_text(str(payload.get("text") or ""))
                            elif payload.get("type") == "ping":
                                await sink.send_event(
                                    {
                                        "type": "pong",
                                        "id": payload.get("id"),
                                        "sentAt": payload.get("sentAt"),
                                    }
                                )
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
                "transport": "authenticated-websocket-pcm-v2",
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
        if candidate.name in {
            "index.html",
            "sw.js",
            "manifest.webmanifest",
            "pcm-capture-processor.js",
            "pcm-playback-processor.js",
        }:
            response.headers["Cache-Control"] = "no-cache"
        elif "_next/static" in candidate.as_posix():
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    def _require_agent_token(self, request: web.Request) -> None:
        if not self._origin_allowed(request):
            raise web.HTTPForbidden(text="Origin is not allowed")
        header = request.headers.get("Authorization", "")
        scheme, _, supplied = header.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            supplied, self._cfg.link_session_token
        ):
            raise web.HTTPUnauthorized(
                text=json.dumps(
                    {
                        "error": {
                            "code": "unauthorized",
                            "message": "Enter the ASTRA Link session token in Settings.",
                        }
                    }
                ),
                content_type="application/json",
            )

    async def _agent_payload(self, request: web.Request) -> dict[str, Any]:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, TypeError) as exc:
            raise web.HTTPBadRequest(
                text=json.dumps(
                    {
                        "error": {
                            "code": "invalid_json",
                            "message": "Send a JSON request body.",
                        }
                    }
                ),
                content_type="application/json",
            ) from exc
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(
                text=json.dumps(
                    {
                        "error": {
                            "code": "invalid_body",
                            "message": "The request body must be an object.",
                        }
                    }
                ),
                content_type="application/json",
            )
        return payload

    @staticmethod
    def _agent_error(exc: AgentWorkspaceError) -> web.Response:
        return web.json_response({"error": exc.as_dict()}, status=400)

    def _workspace(self) -> KittyAgentWorkspace:
        if self._agent_workspace is None:
            raise RuntimeError("agent workspace is not initialized")
        return self._agent_workspace

    @staticmethod
    def _agent_response(payload: Mapping[str, Any], *, status: int = 200) -> web.Response:
        response = web.json_response(payload, status=status)
        response.headers["Cache-Control"] = "no-store"
        return response

    async def _agent_options(self, request: web.Request) -> web.Response:
        if not self._origin_allowed(request):
            raise web.HTTPForbidden(text="Origin is not allowed")
        return web.Response(status=204)

    async def _agents_status(self, request: web.Request) -> web.Response:
        self._require_agent_token(request)
        workspace = self._workspace()
        capabilities, instances = await asyncio.gather(
            asyncio.to_thread(workspace.capabilities),
            asyncio.to_thread(workspace.list_instances),
        )
        return self._agent_response(
            {"capabilities": capabilities, "instances": instances}
        )

    async def _agents_launch(self, request: web.Request) -> web.Response:
        self._require_agent_token(request)
        payload = await self._agent_payload(request)
        try:
            instance = await asyncio.to_thread(
                self._workspace().launch,
                str(payload.get("provider", "")),
                str(payload.get("project_path", "")),
                initial_prompt=(
                    str(payload["prompt"])
                    if payload.get("prompt") is not None
                    else None
                ),
            )
        except AgentWorkspaceError as exc:
            return self._agent_error(exc)
        return self._agent_response({"instance": instance}, status=201)

    async def _agents_prompt(self, request: web.Request) -> web.Response:
        self._require_agent_token(request)
        payload = await self._agent_payload(request)
        try:
            await asyncio.to_thread(
                self._workspace().send_prompt,
                request.match_info["instance_id"],
                str(payload.get("message", "")),
                kind=str(payload.get("mode", "prompt")),
            )
        except AgentWorkspaceError as exc:
            return self._agent_error(exc)
        return self._agent_response({"sent": True})

    async def _agents_focus(self, request: web.Request) -> web.Response:
        self._require_agent_token(request)
        try:
            await asyncio.to_thread(
                self._workspace().focus, request.match_info["instance_id"]
            )
        except AgentWorkspaceError as exc:
            return self._agent_error(exc)
        return self._agent_response({"focused": True})

    async def _agents_interrupt(self, request: web.Request) -> web.Response:
        self._require_agent_token(request)
        try:
            await asyncio.to_thread(
                self._workspace().interrupt, request.match_info["instance_id"]
            )
        except AgentWorkspaceError as exc:
            return self._agent_error(exc)
        return self._agent_response({"interrupted": True})

    async def _agents_close(self, request: web.Request) -> web.Response:
        self._require_agent_token(request)
        try:
            await asyncio.to_thread(
                self._workspace().close, request.match_info["instance_id"]
            )
        except AgentWorkspaceError as exc:
            return self._agent_error(exc)
        return self._agent_response({"closed": True})

    async def _agents_shutdown(self, request: web.Request) -> web.Response:
        self._require_agent_token(request)
        try:
            result = await asyncio.to_thread(self._workspace().shutdown)
        except AgentWorkspaceError as exc:
            return self._agent_error(exc)
        return self._agent_response(result)

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
    "build_content_security_policy",
    "origin_allowed",
    "parse_auth_message",
    "safe_static_path",
]
