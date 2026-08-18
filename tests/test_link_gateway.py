from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from app.voice.link_gateway import (
    CONFIG_KEY,
    CSP_KEY,
    build_content_security_policy,
    origin_allowed,
    parse_auth_message,
    safe_static_path,
    security_headers,
)


def test_origin_allowlist_includes_configured_public_url():
    cfg = SimpleNamespace(
        link_allowed_origins=["http://localhost:3000/"],
        link_public_url="https://astra.example.test",
    )

    assert origin_allowed(cfg, "")
    assert origin_allowed(cfg, "http://localhost:3000")
    assert origin_allowed(cfg, "https://astra.example.test/")
    assert not origin_allowed(cfg, "https://attacker.example")


async def test_agent_auth_errors_keep_cors_headers():
    cfg = SimpleNamespace(
        link_allowed_origins=["http://localhost:3000"],
        link_public_url="",
    )
    app = web.Application()
    app[CONFIG_KEY] = cfg
    app[CSP_KEY] = "default-src 'self'"
    request = make_mocked_request(
        "GET",
        "/agents/status",
        headers={"Origin": "http://localhost:3000"},
        app=app,
    )

    async def unauthorized(_request):
        raise web.HTTPUnauthorized(text="no")

    response = await security_headers(request, unauthorized)

    assert response.status == 401
    assert response.headers["Access-Control-Allow-Origin"] == "http://localhost:3000"
    assert response.headers["Cache-Control"] == "no-store"


def test_auth_message_requires_nonempty_token():
    assert parse_auth_message(json.dumps({"type": "auth", "token": " secret "})) == "secret"

    with pytest.raises(ValueError, match="first message"):
        parse_auth_message(json.dumps({"type": "text", "text": "hello"}))
    with pytest.raises(ValueError, match="empty"):
        parse_auth_message(json.dumps({"type": "auth", "token": ""}))
    with pytest.raises(ValueError, match="valid JSON"):
        parse_auth_message("not-json")


def test_static_path_cannot_escape_build_directory(tmp_path):
    root = tmp_path / "out"
    root.mkdir()

    assert safe_static_path(root, "_next/static/app.js") == root / "_next/static/app.js"
    assert safe_static_path(root, "../../.env") is None


def test_csp_hashes_inline_bootstrap_without_allowing_arbitrary_scripts(tmp_path):
    index = tmp_path / "index.html"
    index.write_text(
        '<script src="/_next/app.js"></script><script>self.__next_f.push([1])</script>',
        encoding="utf-8",
    )

    policy = build_content_security_policy(index)

    assert "script-src 'self' 'sha256-" in policy
    assert "'unsafe-inline'" not in re.search(r"script-src ([^;]+)", policy).group(1)
