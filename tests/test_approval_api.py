"""Tests for the human-approval API — service logic + the stdlib HTTP adapter."""
from __future__ import annotations

import json
import threading
import urllib.request

import pytest
from nodus.runtime.embedding import NodusRuntime
from nodus_approvals import ApprovalPolicy

from src.approval_api import ApprovalError, ApprovalService, serve
from src.runtime import ResearchRuntime

QUESTION = "What is the approval API for?"
SESSION = "api-sess-001"


@pytest.fixture()
def service(tmp_path):
    NodusRuntime.clear_shared_state()
    svc = ApprovalService(workspace=str(tmp_path))
    yield svc
    svc.shutdown()
    NodusRuntime.clear_shared_state()


# ── draft review lifecycle ──────────────────────────────────────────────────

def test_start_returns_waiting_draft(service):
    r = service.start_research(QUESTION, SESSION)
    assert r["status"] == "waiting"
    assert isinstance(r["draft"], str) and r["draft"]
    assert r["run_id"]


def test_start_appears_in_pending(service):
    r = service.start_research(QUESTION, SESSION)
    pending = service.list_pending()
    assert [p["run_id"] for p in pending] == [r["run_id"]]


def test_start_requires_question_and_session(service):
    with pytest.raises(ApprovalError):
        service.start_research("", SESSION)
    with pytest.raises(ApprovalError):
        service.start_research(QUESTION, "")


def test_approve_publishes_and_clears_pending(service):
    r = service.start_research(QUESTION, SESSION)
    out = service.approve(r["run_id"])
    assert out["published"] is True
    assert out["status"] == "published"
    assert service.list_pending() == []


def test_reject_revises_and_re_suspends(service):
    r = service.start_research(QUESTION, SESSION)
    draft1 = r["draft"]
    out = service.reject(r["run_id"], "add concrete examples")
    assert out["status"] == "waiting"
    assert out["draft"] != draft1
    assert "add concrete examples" in out["draft"]
    # Still pending after a rejection.
    assert [p["run_id"] for p in service.list_pending()] == [r["run_id"]]


def test_reject_then_approve_full_cycle(service):
    r = service.start_research(QUESTION, SESSION)
    service.reject(r["run_id"], "too vague")
    out = service.approve(r["run_id"])
    assert out["published"] is True


def test_reject_requires_feedback(service):
    r = service.start_research(QUESTION, SESSION)
    with pytest.raises(ApprovalError):
        service.reject(r["run_id"], "")


def test_unknown_run_is_404(service):
    with pytest.raises(ApprovalError) as exc:
        service.get_run("g_nope")
    assert exc.value.status == 404


def test_double_approve_is_conflict(service):
    r = service.start_research(QUESTION, SESSION)
    service.approve(r["run_id"])
    with pytest.raises(ApprovalError) as exc:
        service.approve(r["run_id"])
    assert exc.value.status == 409


# ── effect gate (out-of-flow tool approvals over the durable store) ─────────

def test_gate_pending_then_approve(tmp_path):
    NodusRuntime.clear_shared_state()
    # Strict runtime: write_file/notify require approval (default effect policy).
    rt = ResearchRuntime(workspace=str(tmp_path))
    svc = ApprovalService(runtime=rt)
    try:
        # A gated tool call creates a pending request in the durable store.
        assert rt._gate.check("research.write_file", "workflow", {"path": "out.md"}) is None
        request_id = rt._gate.last_request_id

        listed = svc.list_gate_requests()
        assert [g["id"] for g in listed] == [request_id]
        assert listed[0]["action"] == "research.write_file"

        svc.approve_gate(request_id, approver_id="ops")
        result = rt._gate.poll(request_id)
        assert result is not None and result.approved is True
        assert svc.list_gate_requests() == []
    finally:
        svc.shutdown()
        NodusRuntime.clear_shared_state()


# ── HTTP round-trip over the stdlib adapter ─────────────────────────────────

def _http(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_http_round_trip_start_list_approve(service):
    server = serve(service, port=0)
    host, port = server.server_address
    base = f"http://{host}:{port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, started = _http("POST", f"{base}/research",
                                {"question": QUESTION, "session_id": SESSION})
        assert status == 200 and started["status"] == "waiting"
        run_id = started["run_id"]

        status, pending = _http("GET", f"{base}/approvals")
        assert status == 200 and [p["run_id"] for p in pending] == [run_id]

        status, approved = _http("POST", f"{base}/approvals/{run_id}/approve")
        assert status == 200 and approved["published"] is True

        # Unknown route → 404 JSON error.
        status, err = _http("GET", f"{base}/nope")
        assert status == 404 and "error" in err
    finally:
        server.shutdown()
        server.server_close()
