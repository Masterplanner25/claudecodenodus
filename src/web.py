"""Real HTTP backends for the ``web_search`` and ``fetch_doc`` tools.

Two backends share one interface (``search`` / ``fetch``):

* ``HttpWebBackend`` — real network access.  ``search`` picks a provider from the
  environment: Tavily (``TAVILY_API_KEY``) → Brave (``BRAVE_API_KEY``) → keyless
  Wikipedia.  The keyed providers cover the open web; the keyless Wikipedia
  default is reliable and honest (an official API, not scraping) but scoped to
  encyclopedic content.  ``fetch`` retrieves a URL and reduces the HTML to clean
  text with a stdlib parser (no bs4/lxml dependency).  Every fetch returns a
  ``content_hash`` — the sha256 of the extracted text — which doubles as the
  ``@exactly_once`` idempotency key.
* ``OfflineWebBackend`` — deterministic, network-free.  Used by the hermetic test
  suite (and as an explicit offline mode) so orchestration tests never touch the
  network.

Both return plain JSON-serializable dicts matching the extension manifests'
``returns_schema``.
"""
from __future__ import annotations

import hashlib
import html
import os
import re
import urllib.parse
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Optional

import httpx

DEFAULT_TIMEOUT = 15.0
# A descriptive, honest User-Agent. Some endpoints reject the default httpx UA.
DEFAULT_USER_AGENT = (
    "ResearchAgent/1.0 (+https://github.com/Masterplanner25/Nodus; autonomous research agent)"
)
DEFAULT_MAX_CHARS = 10_000


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# HTML → text extraction (stdlib only)
# ---------------------------------------------------------------------------

_SKIP_TAGS = frozenset({
    "script", "style", "noscript", "head", "template", "svg",
    "nav", "aside", "form", "button",  # navigation / UI chrome, not content
})
_BLOCK_TAGS = frozenset({
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article",
    "header", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre",
})


class _TextExtractor(HTMLParser):
    """Collect visible text and the document ``<title>`` from an HTML string.

    Content inside ``_SKIP_TAGS`` is dropped; block-level tags emit a newline so
    the reduced text keeps paragraph structure.  Deliberately forgiving — real
    pages are messy, and this only needs to feed an LLM, not round-trip.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        # Title lives inside <head>, which is otherwise skipped — capture it first.
        if self._in_title:
            self.title += data
            return
        if self._skip_depth:
            return
        self._parts.append(data)

    def get_text(self) -> str:
        raw = "".join(self._parts)
        # Collapse runs of spaces/tabs, then squeeze blank lines.
        lines = [re.sub(r"[ \t\f\v]+", " ", ln).strip() for ln in raw.splitlines()]
        out: list[str] = []
        blank = False
        for ln in lines:
            if ln:
                out.append(ln)
                blank = False
            elif not blank:
                out.append("")
                blank = True
        return "\n".join(out).strip()


def html_to_text(html_str: str) -> tuple[str, str]:
    """Return ``(title, text)`` extracted from an HTML document."""
    parser = _TextExtractor()
    try:
        parser.feed(html_str)
    except Exception:
        # A malformed document should degrade, not crash the workflow.
        pass
    return parser.title.strip(), parser.get_text()


# ---------------------------------------------------------------------------
# Search providers.  Each returns a list of {url, title, snippet} dicts.
# ---------------------------------------------------------------------------

def _search_tavily(client: httpx.Client, query: str, max_results: int, api_key: str) -> list[dict]:
    resp = client.post(
        "https://api.tavily.com/search",
        json={
            "api_key": api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    return [
        {
            "url": r.get("url", ""),
            "title": r.get("title", ""),
            "snippet": r.get("content", ""),
        }
        for r in data.get("results", [])[:max_results]
    ]


def _search_brave(client: httpx.Client, query: str, max_results: int, api_key: str) -> list[dict]:
    resp = client.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": max_results},
        headers={"Accept": "application/json", "X-Subscription-Token": api_key},
    )
    resp.raise_for_status()
    data = resp.json()
    results = (data.get("web") or {}).get("results", [])
    return [
        {
            "url": r.get("url", ""),
            "title": r.get("title", ""),
            "snippet": r.get("description", ""),
        }
        for r in results[:max_results]
    ]


def _strip_tags(fragment: str) -> str:
    """Remove HTML tags and decode entities from a snippet fragment."""
    return html.unescape(re.sub(r"<[^>]+>", "", fragment))


# Wikipedia's MediaWiki search API — the keyless default.  Reliable and honest
# (no scraping), but scoped to encyclopedic content; for open-web search supply a
# TAVILY_API_KEY or BRAVE_API_KEY.  Requires a descriptive User-Agent per
# https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_User-Agent_Policy
_WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"


def _search_wikipedia(client: httpx.Client, query: str, max_results: int) -> list[dict]:
    resp = client.get(
        _WIKIPEDIA_API,
        params={
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": max_results,
            "srprop": "snippet",
            "format": "json",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    hits = (data.get("query") or {}).get("search", [])
    results: list[dict] = []
    for h in hits[:max_results]:
        title = h.get("title", "")
        slug = urllib.parse.quote(title.replace(" ", "_"))
        results.append({
            "url": f"https://en.wikipedia.org/wiki/{slug}",
            "title": title,
            "snippet": _strip_tags(h.get("snippet", "")).strip(),
        })
    return results


class HttpWebBackend:
    """Network-backed ``search`` / ``fetch`` for the web tools.

    ``client`` may be injected (e.g. an ``httpx.Client`` with a ``MockTransport``)
    for hermetic testing; otherwise one is built with a sensible timeout and a
    descriptive User-Agent.  Provider keys default to the environment but can be
    passed explicitly.
    """

    def __init__(
        self,
        *,
        client: Optional[httpx.Client] = None,
        timeout: float = DEFAULT_TIMEOUT,
        user_agent: str = DEFAULT_USER_AGENT,
        tavily_key: Optional[str] = None,
        brave_key: Optional[str] = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout,
            headers={"User-Agent": user_agent},
            follow_redirects=True,
        )
        self._tavily_key = tavily_key if tavily_key is not None else os.environ.get("TAVILY_API_KEY")
        self._brave_key = brave_key if brave_key is not None else os.environ.get("BRAVE_API_KEY")

    @property
    def provider(self) -> str:
        if self._tavily_key:
            return "tavily"
        if self._brave_key:
            return "brave"
        return "wikipedia"

    def search(self, query: str, max_results: int = 5) -> dict:
        max_results = max(1, int(max_results))
        if self._tavily_key:
            results = _search_tavily(self._client, query, max_results, self._tavily_key)
        elif self._brave_key:
            results = _search_brave(self._client, query, max_results, self._brave_key)
        else:
            results = _search_wikipedia(self._client, query, max_results)
        return {"results": results}

    def fetch(self, url: str, max_chars: int = DEFAULT_MAX_CHARS) -> dict:
        max_chars = int(max_chars)
        resp = self._client.get(url)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "html" in content_type or content_type == "":
            title, text = html_to_text(resp.text)
        else:
            title, text = "", resp.text
        if not title:
            title = url
        if max_chars > 0 and len(text) > max_chars:
            text = text[:max_chars]
        return {
            "text": text,
            "title": title,
            "fetched_at": _now_iso(),
            "content_hash": _sha256(text),
        }

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


class OfflineWebBackend:
    """Deterministic, network-free backend for hermetic tests and offline mode.

    Produces stable results derived from the query/URL so orchestration tests are
    reproducible and never touch the network.
    """

    def search(self, query: str, max_results: int = 5) -> dict:
        max_results = max(1, int(max_results))
        slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-") or "query"
        return {
            "results": [
                {
                    "url": f"https://example.com/{slug}/{i}",
                    "title": f"Result {i} for: {query}",
                    "snippet": f"Offline snippet {i} about {query}.",
                }
                for i in range(1, max_results + 1)
            ]
        }

    def fetch(self, url: str, max_chars: int = DEFAULT_MAX_CHARS) -> dict:
        text = f"Offline content for {url}"
        if max_chars > 0:
            text = text[:max_chars]
        return {
            "text": text,
            "title": "Offline document",
            "fetched_at": "1970-01-01T00:00:00+00:00",
            "content_hash": _sha256(text),
        }

    def close(self) -> None:  # symmetry with HttpWebBackend
        pass


def build_web_backend() -> HttpWebBackend:
    """Build the default real backend (provider auto-selected from the env)."""
    return HttpWebBackend()
