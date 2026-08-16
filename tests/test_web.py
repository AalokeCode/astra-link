"""Tests for app.tools.web. No network access — parsers are fed fixture HTML."""

from __future__ import annotations

import os

import pytest

from app.tools.registry import ToolExecutionError
from app.tools.web import (
    ExtractPageArgs,
    OpenUrlArgs,
    SearchWebArgs,
    _DDGResultParser,
    _PageTextParser,
    _unwrap_ddg_redirect,
    _validate_fetchable_url,
    extract_page,
    open_url,
    search_web,
)

LIVE = os.environ.get("RUN_LIVE_NETWORK_TESTS") == "1"

# -- fixture: a representative slice of html.duckduckgo.com/html/ markup ----
# Trimmed from a real response (verified live 2026-08) to the parts the
# parser cares about, including a bolded snippet term and one unwrapped link.

DDG_FRAGMENT = """
<div class="results">
  <div class="result results_links results_links_deep web-result">
    <div class="links_main links_deep result__body">
      <h2 class="result__title">
        <a rel="nofollow" class="result__a" href="https://ai.google.dev/gemini-api/docs/function-calling">Function calling with the Gemini API | Google AI for Developers</a>
      </h2>
      <div class="result__extras">
        <div class="result__extras__url">
          <a class="result__url" href="https://ai.google.dev/gemini-api/docs/function-calling">
            ai.google.dev/gemini-api/docs/function-calling
          </a>
        </div>
      </div>
      <a class="result__snippet" href="https://ai.google.dev/gemini-api/docs/function-calling">Get Weather this example shows how to define a <b>function</b> that retrieves data &amp; more.</a>
      <div class="clear"></div>
    </div>
  </div>
  <div class="result results_links results_links_deep web-result">
    <div class="links_main links_deep result__body">
      <h2 class="result__title">
        <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2F&amp;rut=abc123">Python docs</a>
      </h2>
      <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2F">The   official   Python   documentation.</a>
      <div class="clear"></div>
    </div>
  </div>
</div>
"""


class TestDDGResultParser:
    def test_extracts_title_url_snippet(self):
        parser = _DDGResultParser()
        parser.feed(DDG_FRAGMENT)
        assert len(parser.results) == 2

        first = parser.results[0]
        assert first["title"] == "Function calling with the Gemini API | Google AI for Developers"
        assert first["url"] == "https://ai.google.dev/gemini-api/docs/function-calling"
        # <b> tag inside the snippet must not leak, and entities unescape.
        assert "<b>" not in first["snippet"]
        assert "function" in first["snippet"]
        assert "data & more" in first["snippet"]

    def test_unwraps_ddg_redirect_in_context(self):
        parser = _DDGResultParser()
        parser.feed(DDG_FRAGMENT)
        second = parser.results[1]
        assert second["url"] == "https://docs.python.org/3/"

    def test_collapses_whitespace_in_snippet(self):
        parser = _DDGResultParser()
        parser.feed(DDG_FRAGMENT)
        second = parser.results[1]
        assert second["snippet"] == "The official Python documentation."

    def test_malformed_markup_does_not_raise(self):
        parser = _DDGResultParser()
        # Unclosed tags, stray angle brackets, truncated mid-attribute.
        parser.feed('<div><a class="result__a" href="https://example.com">Broken<a class="resu')
        # Should not raise; may or may not have captured a partial result.

    def test_no_results_returns_empty_list(self):
        parser = _DDGResultParser()
        parser.feed("<div class='results'>No results found.</div>")
        assert parser.results == []


class TestUnwrapDdgRedirect:
    def test_unwraps_protocol_relative_redirect(self):
        wrapped = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&rut=x"
        assert _unwrap_ddg_redirect(wrapped) == "https://example.com/page"

    def test_unwraps_absolute_redirect(self):
        wrapped = "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2F&rut=x"
        assert _unwrap_ddg_redirect(wrapped) == "https://example.com/"

    def test_passes_through_direct_url(self):
        direct = "https://ai.google.dev/gemini-api/docs/function-calling"
        assert _unwrap_ddg_redirect(direct) == direct

    def test_passes_through_empty_string(self):
        assert _unwrap_ddg_redirect("") == ""

    def test_does_not_unwrap_unrelated_path(self):
        # A non-DDG URL that happens to start with /l/ must not be mangled.
        other = "https://example.com/l/?uddg=should-not-be-touched"
        assert _unwrap_ddg_redirect(other) == other


class TestPageTextParser:
    def test_drops_script_and_style_content(self):
        page = """
        <html><head><title>My Page</title>
        <style>body { color: red; }</style>
        </head><body>
        <script>alert('leak-me-not');</script>
        <p>Real content here.</p>
        </body></html>
        """
        parser = _PageTextParser()
        parser.feed(page)
        text = parser.text()
        assert "leak-me-not" not in text
        assert "color: red" not in text
        assert "Real content here." in text
        assert parser.title == "My Page"

    def test_drops_nav_footer_header_aside(self):
        page = """
        <body>
        <header>Site Header</header>
        <nav>Nav Links</nav>
        <p>Main content.</p>
        <aside>Sidebar stuff</aside>
        <footer>Footer text</footer>
        </body>
        """
        parser = _PageTextParser()
        parser.feed(page)
        text = parser.text()
        assert "Site Header" not in text
        assert "Nav Links" not in text
        assert "Sidebar stuff" not in text
        assert "Footer text" not in text
        assert "Main content." in text

    def test_block_tags_become_newlines(self):
        page = "<div>One</div><div>Two</div><p>Three</p><li>Four</li>"
        parser = _PageTextParser()
        parser.feed(page)
        lines = parser.text().split("\n")
        assert lines == ["One", "Two", "Three", "Four"]

    def test_br_becomes_newline(self):
        page = "<p>Line one<br>Line two</p>"
        parser = _PageTextParser()
        parser.feed(page)
        assert parser.text().split("\n") == ["Line one", "Line two"]

    def test_entities_unescaped(self):
        page = "<p>Fish &amp; chips &mdash; caf&eacute;</p>"
        parser = _PageTextParser()
        parser.feed(page)
        assert "&amp;" not in parser.text()
        assert "Fish & chips" in parser.text()

    def test_extracts_meta_description(self):
        page = '<head><meta name="description" content="A great page about things."></head>'
        parser = _PageTextParser()
        parser.feed(page)
        assert parser.description == "A great page about things."

    def test_html_comments_never_leak(self):
        page = "<p>Visible</p><!-- secret internal note --><p>Also visible</p>"
        parser = _PageTextParser()
        parser.feed(page)
        text = parser.text()
        assert "secret internal note" not in text
        assert "Visible" in text
        assert "Also visible" in text

    def test_malformed_html_does_not_raise(self):
        parser = _PageTextParser()
        parser.feed("<div><p>Unclosed paragraph <b>bold text <div>nested no close")
        parser.feed("<script>var x = '<not a tag>';")  # truncated script block
        # No assertion needed beyond "did not raise".

    def test_truncated_html_does_not_raise(self):
        parser = _PageTextParser()
        parser.feed("<html><body><p>Hello wor")
        assert "Hello wor" in parser.text()


class TestValidateFetchableUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/",
            "http://127.0.0.1:8080/admin",
            "http://localhost/",
            "http://localhost:3000/",
            "http://10.0.0.5/",
            "http://172.16.0.1/",
            "http://192.168.1.1/",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/",
        ],
    )
    def test_rejects_private_and_loopback_urls(self, url):
        with pytest.raises(ToolExecutionError):
            _validate_fetchable_url(url)

    def test_rejects_non_http_scheme(self):
        with pytest.raises(ToolExecutionError):
            _validate_fetchable_url("ftp://example.com/file.txt")

    def test_rejects_file_scheme(self):
        with pytest.raises(ToolExecutionError):
            _validate_fetchable_url("file:///etc/passwd")

    def test_allows_public_ip_literal(self):
        # 8.8.8.8 is a public IP literal; validation must not require DNS for it.
        _validate_fetchable_url("http://8.8.8.8/")

    def test_extract_page_rejects_private_url_before_any_network_call(self):
        with pytest.raises(ToolExecutionError):
            extract_page(ExtractPageArgs(url="http://127.0.0.1/"))


class TestOpenUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "javascript:alert(1)",
            "ftp://example.com/",
            "app://some-custom-scheme",
            "",
        ],
    )
    def test_rejects_non_http_schemes(self, url):
        with pytest.raises(ToolExecutionError):
            open_url(OpenUrlArgs(url=url))


class TestArgsValidation:
    def test_search_web_max_results_default(self):
        args = SearchWebArgs(query="test")
        assert args.max_results == 8

    def test_search_web_max_results_capped(self):
        with pytest.raises(Exception):
            SearchWebArgs(query="test", max_results=21)

    def test_extract_page_max_chars_capped(self):
        with pytest.raises(Exception):
            ExtractPageArgs(url="https://example.com", max_chars=200_001)


@pytest.mark.skipif(not LIVE, reason="live network test; set RUN_LIVE_NETWORK_TESTS=1 to run")
class TestLiveNetwork:
    def test_search_web_returns_real_results(self):
        result = search_web(SearchWebArgs(query="python asyncio tutorial", max_results=5))
        assert result["results"], "expected at least one live result"
        for item in result["results"]:
            assert "duckduckgo.com" not in item["url"]

    def test_extract_page_fetches_real_page(self):
        result = extract_page(ExtractPageArgs(url="https://example.com"))
        assert result["text"]
        assert result["final_url"]
