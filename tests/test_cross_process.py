"""Tests for durable cross-process resume (enabled by Nodus v4.0.7).

Two layers:

1. ``FileApprovalStore`` — durable approval state that a second, independently
   constructed store (i.e. a second process) reads back.
2. ``ResearchRuntime.resume_in_fresh_process`` — resumes a persisted waiting run
   on a freshly-primed VM, forcing the framework's ``_rebuild_workflow_graph``
   import-rebinding path rather than reusing the start VM.

``resume_in_fresh_process`` primes a *new* VM, which differs from the run's
registered start VM, so the runner rebuilds the graph from persisted state —
the same code path a genuinely separate process would take.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from nodus.runtime.embedding import NodusRuntime
from nodus_approvals import ApprovalGate, ApprovalPolicy
from nodus_approvals.request import ApprovalRequest

from src.approval_store import FileApprovalStore
from src.runtime import ResearchRuntime

QUESTION = "What is durable approval?"
SESSION = "xproc-sess-001"


# ── FileApprovalStore durability ────────────────────────────────────────────

def test_store_persists_request_across_instances(tmp_path):
    root = tmp_path / "approvals"
    req = ApprovalRequest.create("research.write_file", "workflow", {"path": "x"})
    FileApprovalStore(root).save(req)

    # A second store over the same dir = a second process.
    reread = FileApprovalStore(root).get(req.id)
    assert reread is not None
    assert reread.id == req.id
    assert reread.action == "research.write_file"
    assert reread.context == {"path": "x"}


def test_store_approval_visible_to_second_instance(tmp_path):
    root = tmp_path / "approvals"
    store_a = FileApprovalStore(root)
    gate_a = ApprovalGate(policy=ApprovalPolicy.require_for("research.*"), store=store_a)

    # Process A creates a pending request.
    assert gate_a.check("research.write_file", "workflow", {"path": "out.md"}) is None
    request_id = gate_a.last_request_id

    # Process B (fresh store + gate over same dir) sees it pending, then approves.
    store_b = FileApprovalStore(root)
    gate_b = ApprovalGate(policy=ApprovalPolicy.require_for("research.*"), store=store_b)
    assert [r.id for r in store_b.pending()] == [request_id]
    gate_b.approve(request_id, approver_id="human")

    # Process A polls and sees the approval recorded by B.
    result = gate_a.poll(request_id)
    assert result is not None and result.approved is True
    assert result.approver_id == "human"
    # Resolved → no longer pending for anyone.
    assert store_a.pending() == []


def test_store_expire_old_removes_only_unresolved_expired(tmp_path):
    root = tmp_path / "approvals"
    store = FileApprovalStore(root)
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    future = datetime.now(timezone.utc) + timedelta(hours=1)

    expired = ApprovalRequest.create("a.x", "w", expires_at=past)
    live = ApprovalRequest.create("a.y", "w", expires_at=future)
    store.save(expired)
    store.save(live)

    assert store.expire_old() == 1
    assert store.get(expired.id) is None
    assert store.get(live.id) is not None


# ── Cross-process workflow resume ───────────────────────────────────────────

@pytest.fixture()
def rt(tmp_path):
    NodusRuntime.clear_shared_state()
    runtime = ResearchRuntime(workspace=str(tmp_path), policy=ApprovalPolicy.allow_all())
    yield runtime
    runtime.shutdown()
    NodusRuntime.clear_shared_state()


def test_fresh_process_resume_completes_publish(rt, tmp_path):
    r1 = rt.start(QUESTION, SESSION)
    assert r1["status"] == "waiting"

    r2 = rt.resume_in_fresh_process(r1["graph_id"], {"approved": True})
    assert r2["steps"]["publish"]["published"] is True
    assert r2["steps"]["publish"]["session_id"] == SESSION

    out = tmp_path / "output" / SESSION / "final.md"
    assert out.exists()
    assert out.read_text(encoding="utf-8") == r1["state"]["draft"]


def test_fresh_process_resume_with_feedback_re_suspends(rt):
    r1 = rt.start(QUESTION, SESSION)
    draft1 = r1["state"]["draft"]

    r2 = rt.resume_in_fresh_process(
        r1["graph_id"], {"feedback": "add citations"}, checkpoint="before_draft"
    )
    assert r2.get("status") == "waiting"
    assert r2["state"]["draft"] != draft1
    assert "add citations" in r2["state"]["draft"]


def test_resume_auto_falls_back_to_priming_when_vm_evicted(rt, tmp_path, monkeypatch):
    """``resume`` (not the explicit fresh-process method) still works when the
    start VM is gone from the registry — the cross-process fallback kicks in."""
    r1 = rt.start(QUESTION, SESSION)
    run_id = r1["graph_id"]

    # Simulate a separate process: no VM registered for this run.
    monkeypatch.setattr("src.runtime.get_registered_vm", lambda _run_id: None)

    r2 = rt.resume(run_id, {"approved": True})
    assert r2["steps"]["publish"]["published"] is True
    assert (tmp_path / "output" / SESSION / "final.md").exists()
