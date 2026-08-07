"""Unit tests for the notifier backends (``src/notify.py``).

Network paths are mocked with ``httpx.MockTransport`` so these stay hermetic.
"""
from __future__ import annotations

import httpx
import pytest

from src.notify import HttpNotifier, ConsoleNotifier
from src.runtime import _ext_notify


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# ── console ──────────────────────────────────────────────────────────────────

def test_console_channel_delivers(capsys):
    out = HttpNotifier().notify("console", "researcher", "hello", run_id="s1")
    assert out["delivered"] is True
    captured = capsys.readouterr().out
    assert "hello" in captured and "s1" in captured


def test_unknown_channel_not_delivered():
    out = HttpNotifier().notify("carrier-pigeon", "x", "hi")
    assert out["delivered"] is False
    assert "unknown channel" in out["response"]


# ── webhook ──────────────────────────────────────────────────────────────────

def test_webhook_posts_payload_and_reports_2xx():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode()
        return httpx.Response(204)

    n = HttpNotifier(client=_client(handler))
    out = n.notify("webhook", "https://hooks.example/x", "done", run_id="r9")
    assert out["delivered"] is True
    assert out["response"] == "HTTP 204"
    assert "r9" in seen["body"] and "done" in seen["body"]
    assert seen["url"] == "https://hooks.example/x"


def test_webhook_non_2xx_not_delivered():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    out = HttpNotifier(client=_client(handler)).notify("webhook", "https://x/y", "m")
    assert out["delivered"] is False
    assert "500" in out["response"]


# ── slack ────────────────────────────────────────────────────────────────────

def test_slack_ok_body_is_delivered():
    def handler(request: httpx.Request) -> httpx.Response:
        assert '"text"' in request.content.decode()
        return httpx.Response(200, text="ok")

    out = HttpNotifier(client=_client(handler)).notify("slack", "https://hooks.slack/x", "hi")
    assert out["delivered"] is True


def test_slack_non_ok_body_not_delivered():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="invalid_payload")

    out = HttpNotifier(client=_client(handler)).notify("slack", "https://hooks.slack/x", "hi")
    assert out["delivered"] is False


# ── email (unconfigured) + fail-soft ─────────────────────────────────────────

def test_email_unconfigured_fails_soft(monkeypatch):
    monkeypatch.delenv("RESEARCH_SMTP_HOST", raising=False)
    out = HttpNotifier().notify("email", "someone@example.com", "hi")
    assert out["delivered"] is False
    assert "not configured" in out["response"]


def test_webhook_network_error_fails_soft():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    out = HttpNotifier(client=_client(handler)).notify("webhook", "https://x/y", "m")
    assert out["delivered"] is False
    assert "ConnectError" in out["response"]


# ── console notifier + dispatch wrapper ──────────────────────────────────────

def test_console_notifier_records():
    n = ConsoleNotifier()
    n.notify("webhook", "t", "m", run_id="z")
    assert n.sent == [{"channel": "webhook", "target": "t", "message": "m", "run_id": "z"}]


def test_ext_notify_defaults_to_console():
    out = _ext_notify({"channel": "console", "message": "hi"}, notifier=None)
    assert out["delivered"] is True


def test_ext_notify_uses_injected_notifier():
    n = ConsoleNotifier()
    out = _ext_notify({"channel": "slack", "target": "u", "message": "m", "run_id": "r"}, notifier=n)
    assert out["delivered"] is True
    assert n.sent[0]["channel"] == "slack"
