"""Web research tools: search, page extraction, and browser hand-off (spec §8).

Runtime deps are `httpx` and `pydantic` only (see `pyproject.toml`) — no
`ddgs`, no `beautifulsoup4`, no `lxml`. HTML parsing here is hand-written on
top of the standard library's `html.parser.HTMLParser`, which is more than
enough for the narrow, well-defined slices of markup these tools care about
(a results page's title/snippet anchors; a page's block structure and a
handful of tags to strip).
"""

from __future__ import annotations

import html
import ipaddress
import logging
import re
import socket
import subprocess
import urllib.parse
from html.parser import HTMLParser

import httpx
from pydantic import BaseModel, Field

from app.tools.registry import RiskLevel, ToolExecutionError, ToolGroup, registry

log = logging.getLogger(__name__)

# DuckDuckGo's HTML endpoint bot-checks requests with no User-Agent / Referer
# outright (verified live: identical request minus these headers gets a 202
# "Unfortunately, bots use DuckDuckGo too" challenge page instead of results).
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_DDG_URL = "https://html.duckduckgo.com/html/"
_DDG_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://html.duckduckgo.com/",
    "Origin": "https://html.duckduckgo.com",
}

_REQUEST_TIMEOUT = 15.0
_MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024  # 5 MB cap so a huge file can't blow up memory.


def _collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# -- search_web ---------------------------------------------------------


def _unwrap_ddg_redirect(url: str) -> str:
    """Recover the real destination from a DuckDuckGo `/l/?uddg=...` link.

    DuckDuckGo's HTML results wrap outbound links in a redirect of the form
    `//duckduckgo.com/l/?uddg=<urlencoded-real-url>&rut=...`. A URL that
    isn't wrapped this way is returned unchanged.
    """
    if not url:
        return url

    parsed = urllib.parse.urlsplit(url)
    host = parsed.netloc.lower()
    is_ddg_link = parsed.path.startswith("/l/") and (host == "" or "duckduckgo.com" in host)
    if not is_ddg_link:
        return url

    real = urllib.parse.parse_qs(parsed.query).get("uddg", [None])[0]
    return urllib.parse.unquote(real) if real else url


class _DDGResultParser(HTMLParser):
    """Extracts (title, url, snippet) triples from a DDG HTML results page.

    Each organic result renders as `<a class="result__a" href="...">title</a>`
    followed by `<a class="result__snippet" href="...">snippet</a>` (the
    snippet may contain `<b>` tags DDG uses to bold matched terms). We track
    only those two classes rather than modeling the surrounding DOM, so the
    parser tolerates markup changes elsewhere on the page. Unexpected or
    malformed markup degrades to fewer/no results rather than raising.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._capture: str | None = None  # "title" | "snippet" | None
        self._buf: list[str] = []
        self._pending_url: str | None = None

    @staticmethod
    def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
        for key, value in attrs:
            if key == "class" and value:
                return set(value.split())
        return set()

    @staticmethod
    def _href(attrs: list[tuple[str, str | None]]) -> str | None:
        for key, value in attrs:
            if key == "href":
                return value
        return None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        classes = self._classes(attrs)
        if "result__a" in classes:
            self._capture = "title"
            self._buf = []
            self._pending_url = _unwrap_ddg_redirect(self._href(attrs) or "")
        elif "result__snippet" in classes:
            self._capture = "snippet"
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._capture is None:
            return
        text = _collapse_whitespace(html.unescape("".join(self._buf)))
        if self._capture == "title":
            self.results.append({"title": text, "url": self._pending_url or "", "snippet": ""})
            self._pending_url = None
        elif self._capture == "snippet" and self.results:
            self.results[-1]["snippet"] = text
        self._capture = None
        self._buf = []


def _looks_rate_limited(response: httpx.Response) -> bool:
    if response.status_code in (202, 403, 429, 503):
        return True
    lowered = response.text[:2000].lower()
    return "unfortunately, bots use duckduckgo" in lowered or "anomaly-modal" in lowered


class SearchWebArgs(BaseModel):
    query: str = Field(description="The search query text.")
    max_results: int = Field(
        default=8, ge=1, le=20, description="Maximum number of results to return (capped at 20)."
    )


@registry.tool(
    name="search_web",
    description=(
        "Search the web via DuckDuckGo. Returns titles, URLs, and snippets. "
        "Use for current information, documentation lookups, or anything the "
        "model doesn't already know reliably. Prefer official sources for API docs."
    ),
    args_model=SearchWebArgs,
    risk=RiskLevel.READ_ONLY,
    group=ToolGroup.WEB,
)
def search_web(args: SearchWebArgs) -> dict:
    try:
        with httpx.Client(
            follow_redirects=True, timeout=_REQUEST_TIMEOUT, headers=_DDG_HEADERS
        ) as client:
            response = client.post(_DDG_URL, data={"q": args.query})
    except httpx.RequestError as exc:
        raise ToolExecutionError(f"network request to DuckDuckGo failed: {exc}") from exc

    rate_limited = _looks_rate_limited(response)
    if response.status_code != 200 or rate_limited:
        hint = (
            " this looks like DuckDuckGo rate-limiting or bot-blocking the request "
            "(got a challenge/error page instead of results); wait before retrying"
            if rate_limited
            else ""
        )
        raise ToolExecutionError(
            f"DuckDuckGo search failed with HTTP {response.status_code}.{hint}"
        )

    parser = _DDGResultParser()
    try:
        parser.feed(response.text)
    except Exception as exc:  # pragma: no cover - HTMLParser is normally tolerant
        log.warning("DuckDuckGo result parsing failed, returning empty results: %s", exc)
        return {"query": args.query, "results": []}

    # Empty results are a valid, non-error outcome.
    results = [r for r in parser.results if r["url"] and r["title"]][: args.max_results]
    return {"query": args.query, "results": results}


# -- extract_page ---------------------------------------------------------

_BLOCK_TAGS = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}
_SKIP_TAGS = {"script", "style", "nav", "footer", "header", "aside"}
_LOCALHOST_NAMES = {"localhost", "localhost.localdomain"}


class _PageTextParser(HTMLParser):
    """Reduces an HTML page to readable text, a title, and a meta description.

    `<script>`/`<style>`/`<nav>`/`<footer>`/`<header>`/`<aside>` content is
    dropped entirely (comments are dropped automatically — `HTMLParser`
    routes them to `handle_comment`, which we don't override, so they never
    reach the text buffer). Block-level tags become newlines so the output
    stays readable instead of collapsing into one run-on line.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.description = ""
        self._skip_stack: list[str] = []
        self._in_title = False
        self._chunks: list[str] = []

    @staticmethod
    def _attr(attrs: list[tuple[str, str | None]], key: str) -> str | None:
        for k, v in attrs:
            if k == key:
                return v
        return None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_stack:
            if tag in _SKIP_TAGS:
                self._skip_stack.append(tag)
            return
        if tag in _SKIP_TAGS:
            self._skip_stack.append(tag)
            return
        if tag == "meta":
            name = (self._attr(attrs, "name") or "").lower()
            if name == "description" and not self.description:
                self.description = (self._attr(attrs, "content") or "").strip()
            return
        if tag == "title":
            self._in_title = True
            return
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # Self-closed tags (e.g. an XHTML-style <br/>) go through the same path.
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if self._skip_stack:
            if tag == self._skip_stack[-1]:
                self._skip_stack.pop()
            return
        if tag == "title":
            self._in_title = False
            return
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_stack:
            return
        if self._in_title:
            self.title += data
            return
        self._chunks.append(data)

    def text(self) -> str:
        lines = []
        for part in "".join(self._chunks).split("\n"):
            collapsed = _collapse_whitespace(part)
            if collapsed:
                lines.append(collapsed)
        return "\n".join(lines)


def _host_is_disallowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _validate_fetchable_url(url: str) -> None:
    """SSRF guard (basic hygiene, spec-required): http(s) only, no local targets.

    The assistant runs on the user's own machine, often next to local
    services (dev servers, admin panels with no auth because "it's
    localhost"). A model-supplied URL must not be usable to probe those.

    IP-literal hosts are checked directly with no DNS involved. Hostnames get
    a best-effort DNS check; if resolution fails (offline, or a sandboxed
    test environment with no network) we let the real HTTP request surface
    that failure rather than guessing.
    """
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ToolExecutionError(
            f"unsupported URL scheme '{parsed.scheme or '<none>'}'; only http and https are allowed"
        )

    hostname = (parsed.hostname or "").strip()
    if not hostname:
        raise ToolExecutionError(f"'{url}' has no hostname")

    if hostname.lower() in _LOCALHOST_NAMES:
        raise ToolExecutionError(f"refusing to fetch '{url}': localhost is off-limits for this tool")

    try:
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        literal_ip = None

    if literal_ip is not None:
        if _host_is_disallowed(literal_ip):
            raise ToolExecutionError(
                f"refusing to fetch '{url}': {literal_ip} is a private/loopback/reserved address"
            )
        return

    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError:
        return

    for info in infos:
        raw_ip = info[4][0].split("%", 1)[0]  # strip IPv6 zone id, e.g. "fe80::1%en0"
        try:
            resolved = ipaddress.ip_address(raw_ip)
        except ValueError:
            continue
        if _host_is_disallowed(resolved):
            raise ToolExecutionError(
                f"refusing to fetch '{url}': hostname resolves to {resolved}, "
                "a private/loopback/reserved address"
            )


class ExtractPageArgs(BaseModel):
    url: str = Field(description="Absolute http(s) URL to fetch and extract readable text from.")
    max_chars: int = Field(
        default=20_000,
        ge=1,
        le=100_000,
        description="Maximum characters of extracted text to return (capped at 100000).",
    )


@registry.tool(
    name="extract_page",
    description=(
        "Fetch a web page and return its readable text, title, and description. "
        "Use this to read a page found via search_web in full detail."
    ),
    args_model=ExtractPageArgs,
    risk=RiskLevel.READ_ONLY,
    group=ToolGroup.WEB,
)
def extract_page(args: ExtractPageArgs) -> dict:
    _validate_fetchable_url(args.url)

    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    }

    try:
        with httpx.Client(
            follow_redirects=True, timeout=_REQUEST_TIMEOUT, headers=headers
        ) as client, client.stream("GET", args.url) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if "html" not in content_type.lower():
                raise ToolExecutionError(
                    f"'{args.url}' is not an HTML page (content-type: {content_type or 'unknown'})"
                )
            final_url = str(response.url)
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > _MAX_DOWNLOAD_BYTES:
                    break
                chunks.append(chunk)
            raw = b"".join(chunks)
            encoding = response.encoding or "utf-8"
    except httpx.HTTPStatusError as exc:
        raise ToolExecutionError(
            f"fetching '{args.url}' failed with HTTP {exc.response.status_code}"
        ) from exc
    except httpx.RequestError as exc:
        raise ToolExecutionError(f"fetching '{args.url}' failed: {exc}") from exc

    try:
        html_text = raw.decode(encoding, errors="replace")
    except (LookupError, TypeError):
        html_text = raw.decode("utf-8", errors="replace")

    parser = _PageTextParser()
    try:
        parser.feed(html_text)
    except Exception as exc:  # pragma: no cover - HTMLParser is normally tolerant
        log.warning("page text extraction failed for %s: %s", args.url, exc)

    text = html.unescape(parser.text())
    title = _collapse_whitespace(html.unescape(parser.title))
    description = html.unescape(parser.description)

    truncated = len(text) > args.max_chars
    if truncated:
        text = text[: args.max_chars]

    return {
        "url": args.url,
        "final_url": final_url,
        "title": title,
        "description": description,
        "text": text,
        "truncated": truncated,
        "content_type": content_type,
    }


# -- open_url ---------------------------------------------------------


class OpenUrlArgs(BaseModel):
    url: str = Field(description="http(s) URL to open in the user's default web browser.")


@registry.tool(
    name="open_url",
    description="Open a URL in the user's default web browser.",
    args_model=OpenUrlArgs,
    risk=RiskLevel.LOW_RISK_WRITE,
    group=ToolGroup.WEB,
    confirm_template="Open {url} in the default browser?",
)
def open_url(args: OpenUrlArgs) -> dict:
    parsed = urllib.parse.urlsplit(args.url)
    if parsed.scheme.lower() not in ("http", "https"):
        # `open` will happily launch file:// or a custom app scheme otherwise.
        raise ToolExecutionError(
            f"unsupported URL scheme '{parsed.scheme or '<none>'}'; only http and https URLs can be opened"
        )

    try:
        subprocess.run(
            ["open", args.url], check=True, capture_output=True, text=True, timeout=10
        )
    except subprocess.CalledProcessError as exc:
        raise ToolExecutionError(f"failed to open '{args.url}': {exc.stderr.strip()}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolExecutionError(f"opening '{args.url}' timed out") from exc

    return {"url": args.url, "opened": True}
