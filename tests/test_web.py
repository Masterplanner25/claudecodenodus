"""Unit tests for the real web backends (``src/web.py``).

All network access is mocked with ``httpx.MockTransport`` so these stay hermetic
and deterministic — they exercise provider selection, HTML parsing, the fail-open
dispatch wrappers, and the offline backend.
"""
from __future__ import annotations

import httpx
import pytest

from src.web import HttpWebBackend, OfflineWebBackend, html_to_text
from src.runtime import _ext_web_search, _ext_fetch_doc


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# ── HTML → text extraction ───────────────────────────────────────────────────

def test_html_to_text_extracts_title_and_body():
    html = """
    <html><head><title>My Page</title><style>.x{color:red}</style></head>
    <body><script>ignore()</script><h1>Heading</h1><p>First para.</p>
    <p>Second para.</p></body></html>
    """
    title, text = html_to_text(html)
    assert title == "My Page"
    assert "Heading" in text
    assert "First para." in text
    assert "Second para." in text
    # Script/style content is stripped.
    assert "ignore()" not in text
    assert "color:red" not in text


def test_html_to_text_handles_malformed():
    title, text = html_to_text("<p>unclosed <b>tags")
    assert "unclosed" in text


# ── Wikipedia (keyless default) search ───────────────────────────────────────

_WIKI_JSON = {
    "query": {
        "search": [
            {"title": "LLM safety", "snippet": 'A <span class="s">field</span> of &quot;AI&quot; work.'},
            {"title": "Alignment (AI)", "snippet": "Second snippet here."},
        ]
    }
}


def test_wikipedia_search_parses_results():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "en.wikipedia.org" in str(request.url)
        assert "srsearch" in str(request.url)
        return httpx.Response(200, json=_WIKI_JSON)

    backend = HttpWebBackend(client=_client(handler))
    assert backend.provider == "wikipedia"
    out = backend.search("anything", max_results=5)
    results = out["results"]
    assert len(results) == 2
    assert results[0]["url"] == "https://en.wikipedia.org/wiki/LLM_safety"
    assert results[0]["title"] == "LLM safety"
    # Tags stripped, entities decoded.
    assert results[0]["snippet"] == 'A field of "AI" work.'
    assert results[1]["url"] == "https://en.wikipedia.org/wiki/Alignment_%28AI%29"


def test_wikipedia_search_respects_max_results():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_WIKI_JSON)

    backend = HttpWebBackend(client=_client(handler))
    out = backend.search("anything", max_results=1)
    assert len(out["results"]) == 1


# ── Tavily / Brave provider selection (keyed) ────────────────────────────────

def test_tavily_selected_when_key_present():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "api.tavily.com" in str(request.url)
        return httpx.Response(200, json={
            "results": [
                {"url": "https://t.example/1", "title": "T1", "content": "tav snippet"},
            ]
        })

    backend = HttpWebBackend(client=_client(handler), tavily_key="tv-key")
    assert backend.provider == "tavily"
    out = backend.search("q", max_results=3)
    assert out["results"][0]["url"] == "https://t.example/1"
    assert out["results"][0]["snippet"] == "tav snippet"


def test_brave_selected_when_only_brave_key():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "api.search.brave.com" in str(request.url)
        assert request.headers.get("X-Subscription-Token") == "br-key"
        return httpx.Response(200, json={
            "web": {"results": [
                {"url": "https://b.example/1", "title": "B1", "description": "brave snippet"},
            ]}
        })

    backend = HttpWebBackend(client=_client(handler), brave_key="br-key")
    assert backend.provider == "brave"
    out = backend.search("q", max_results=3)
    assert out["results"][0]["snippet"] == "brave snippet"


# ── fetch ────────────────────────────────────────────────────────────────────

def test_fetch_returns_text_title_and_hash():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<html><head><title>Doc</title></head><body><p>Hello world.</p></body></html>",
            headers={"content-type": "text/html; charset=utf-8"},
        )

    backend = HttpWebBackend(client=_client(handler))
    out = backend.fetch("https://example.org/doc")
    assert out["title"] == "Doc"
    assert "Hello world." in out["text"]
    assert len(out["content_hash"]) == 64  # sha256 hex
    assert out["fetched_at"]  # non-empty ISO timestamp


def test_fetch_truncates_to_max_chars():
    long_body = "<html><body><p>" + ("x" * 5000) + "</p></body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=long_body, headers={"content-type": "text/html"})

    backend = HttpWebBackend(client=_client(handler))
    out = backend.fetch("https://example.org/big", max_chars=100)
    assert len(out["text"]) == 100


def test_fetch_content_hash_is_stable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<body>stable</body>", headers={"content-type": "text/html"})

    b1 = HttpWebBackend(client=_client(handler))
    b2 = HttpWebBackend(client=_client(handler))
    assert b1.fetch("https://x/y")["content_hash"] == b2.fetch("https://x/y")["content_hash"]


# ── fail-open dispatch wrappers ──────────────────────────────────────────────

class _BoomBackend:
    def search(self, query, max_results=5):
        raise httpx.ConnectError("no network")

    def fetch(self, url, max_chars=10000):
        raise httpx.ConnectError("no network")


def test_web_search_fails_open():
    out = _ext_web_search({"query": "q", "max_results": 5}, web_backend=_BoomBackend())
    assert out["results"] == []
    assert "error" in out


def test_fetch_doc_fails_open():
    out = _ext_fetch_doc({"url": "https://x/y"}, web_backend=_BoomBackend())
    assert out["text"] == ""
    assert out["title"] == "https://x/y"
    assert len(out["content_hash"]) == 64
    assert "error" in out


# ── offline backend ──────────────────────────────────────────────────────────

def test_offline_backend_is_deterministic():
    b = OfflineWebBackend()
    assert b.search("Hello World", max_results=2) == b.search("Hello World", max_results=2)
    r = b.search("Hello World", max_results=2)["results"]
    assert len(r) == 2
    assert r[0]["url"] == "https://example.com/hello-world/1"


def test_offline_fetch_deterministic_hash():
    b = OfflineWebBackend()
    assert b.fetch("https://x/y")["content_hash"] == b.fetch("https://x/y")["content_hash"]
