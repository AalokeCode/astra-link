from __future__ import annotations

import json
import re

import pytest

from app.voice.link_gateway import (
    build_content_security_policy,
    parse_auth_message,
    safe_static_path,
)


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
