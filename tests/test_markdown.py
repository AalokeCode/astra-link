"""Tests for dependency-free Markdown rendering and document creation."""

from __future__ import annotations

import dataclasses

from app.config import load_config
from app.render.markdown import markdown_to_html
from app.tools.documents import CreateDocumentArgs, create_document


def test_block_constructs_render_expected_tags():
    source = """# Heading

> Quote

- One
  - Nested

1. First
2. Second

---

    indented **code**

Paragraph text.
"""
    output = markdown_to_html(source)
    for tag in ("<h1>", "<blockquote>", "<ul>", "<ol>", "<hr>", "<pre><code>", "<p>"):
        assert tag in output


def test_inline_constructs_render_expected_tags():
    output = markdown_to_html("**bold** *italic* ~~strike~~ `code` [link](https://example.com)")
    assert "<strong>bold</strong>" in output
    assert "<em>italic</em>" in output
    assert "<del>strike</del>" in output
    assert "<code>code</code>" in output
    assert '<a href="https://example.com">link</a>' in output


def test_html_injection_is_escaped():
    output = markdown_to_html("`<script>alert(1)</script>`\n\n<div>unsafe</div>")
    assert "<script>" not in output
    assert "<div>unsafe</div>" not in output
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in output
    assert "&lt;div&gt;unsafe&lt;/div&gt;" in output


def test_gfm_table_has_expected_cells_and_alignment():
    output = markdown_to_html("| A | B |\n|:---|---:|\n| 1 | 2 |")
    assert "<table>" in output
    assert output.count("<th ") == 2
    assert output.count("<td ") == 2
    assert "text-align:left" in output
    assert "text-align:right" in output


def test_fenced_code_does_not_apply_inline_formatting():
    output = markdown_to_html("```python\n**not bold** <tag>\n```")
    assert "<strong>" not in output
    assert "**not bold** &lt;tag&gt;" in output
    assert 'class="language-python"' in output


def test_complete_document_has_charset_style_and_escaped_title():
    output = markdown_to_html("Text", title="A < B")
    assert output.startswith("<!doctype html>")
    assert '<meta charset="utf-8">' in output
    assert "<style>" in output
    assert "<title>A &lt; B</title>" in output


def test_create_markdown_round_trip(tmp_path, monkeypatch):
    config = dataclasses.replace(load_config(), allowed_dirs=[tmp_path.resolve()])
    monkeypatch.setattr("app.security.paths.load_config", lambda: config)
    target = tmp_path / "round-trip.md"
    source = "# Exact\n\nContent with **markup**.\n"

    result = create_document(CreateDocumentArgs(path=str(target), content=source))

    assert target.read_text(encoding="utf-8") == source
    assert result == {"path": str(target), "format": "md", "bytes": len(source.encode()), "created": True}
