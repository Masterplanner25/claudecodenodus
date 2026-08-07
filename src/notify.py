"""Real delivery for the ``research.notify`` tool (effect: ``network.write``).

``HttpNotifier`` dispatches on ``channel``:

* ``console`` / ``log`` — print to stdout (always deliverable; the sensible
  default so the agent notifies *something* real without external config).
* ``webhook`` — HTTP POST ``{run_id, message}`` to ``target``; delivered on 2xx.
* ``slack`` — HTTP POST ``{"text": message}`` to a Slack incoming-webhook URL
  (``target``); delivered when the response is ``200 ok``.
* ``email`` — SMTP send to ``target``, configured via ``RESEARCH_SMTP_*`` env
  vars; a clear not-delivered result when unconfigured (never raises).

Every path **fails soft** — a network/SMTP error returns
``{"delivered": False, "response": "<error>"}`` rather than raising, so a failed
notification never aborts the workflow's publish step (``notify`` declares
``network.write`` and is approval-gated; its result is advisory).

``ConsoleNotifier`` is the deterministic, network-free stand-in for tests.
"""
from __future__ import annotations

import os
from typing import Optional

import httpx

DEFAULT_TIMEOUT = 10.0
_CONSOLE_CHANNELS = frozenset({"console", "log", "print", "stdout"})


class HttpNotifier:
    """Network-backed notifier. ``client`` is injectable for hermetic tests."""

    def __init__(self, *, client: Optional[httpx.Client] = None, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)

    def notify(self, channel: str, target: str, message: str, run_id: str = "") -> dict:
        ch = (channel or "console").lower()
        try:
            if ch in _CONSOLE_CHANNELS:
                print(f"[notify:{run_id}] {message}")
                return {"delivered": True, "response": "logged to console"}
            if ch == "webhook":
                resp = self._client.post(target, json={"run_id": run_id, "message": message})
                delivered = 200 <= resp.status_code < 300
                return {"delivered": delivered, "response": f"HTTP {resp.status_code}"}
            if ch == "slack":
                resp = self._client.post(target, json={"text": message})
                body = (resp.text or "").strip()
                delivered = resp.status_code == 200 and body.lower() == "ok"
                return {"delivered": delivered, "response": f"HTTP {resp.status_code}: {body[:120]}"}
            if ch == "email":
                return self._send_email(target, message, run_id)
            return {"delivered": False, "response": f"unknown channel '{channel}'"}
        except Exception as exc:  # fail soft — a bad notify must not abort publish
            return {"delivered": False, "response": f"{type(exc).__name__}: {exc}"}

    def _send_email(self, target: str, message: str, run_id: str) -> dict:
        host = os.environ.get("RESEARCH_SMTP_HOST")
        if not host:
            return {
                "delivered": False,
                "response": "email channel not configured (set RESEARCH_SMTP_HOST / _PORT / _USER / _PASS / _FROM)",
            }
        import smtplib
        from email.message import EmailMessage

        port = int(os.environ.get("RESEARCH_SMTP_PORT", "587"))
        user = os.environ.get("RESEARCH_SMTP_USER")
        password = os.environ.get("RESEARCH_SMTP_PASS")
        sender = os.environ.get("RESEARCH_SMTP_FROM", user or "research-agent@localhost")

        em = EmailMessage()
        em["From"] = sender
        em["To"] = target
        em["Subject"] = f"Research notification ({run_id})"
        em.set_content(message)

        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.starttls()
            if user and password:
                smtp.login(user, password)
            smtp.send_message(em)
        return {"delivered": True, "response": f"email sent to {target}"}

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


class ConsoleNotifier:
    """Deterministic, network-free notifier for hermetic tests.

    Records every notification on ``.sent`` and always reports delivered.
    """

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def notify(self, channel: str, target: str, message: str, run_id: str = "") -> dict:
        self.sent.append({"channel": channel, "target": target, "message": message, "run_id": run_id})
        return {"delivered": True, "response": "console"}

    def close(self) -> None:  # symmetry with HttpNotifier
        pass


def build_notifier() -> HttpNotifier:
    """Build the default real notifier."""
    return HttpNotifier()
