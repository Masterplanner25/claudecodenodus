"""Integration tests for ResearchRuntime — exercises the full Python host layer:
NodusRuntime lifecycle, tool dispatch, approval gate, workspace writes, and the
start / resume / resume_with_feedback iteration protocol.
"""
from __future__ import annotations

import pytest
from nodus.runtime.embedding import NodusRuntime
from nodus_approvals import ApprovalPolicy

from src.runtime import ResearchRuntime
from src.web import OfflineWebBackend
from src.sandbox import DisabledCodeRunner
from src.notify import ConsoleNotifier

QUESTION = "What is LLM safety?"
SESSION = "integ-sess-001"


@pytest.fixture()
def rt(tmp_path):
    """Fresh ResearchRuntime with allow_all policy and isolated workspace.

    Injects the deterministic offline web backend so orchestration tests stay
    hermetic (the real backend hits the network).
    """
    NodusRuntime.clear_shared_state()
    runtime = ResearchRuntime(
        workspace=str(tmp_path),
        policy=ApprovalPolicy.allow_all(),
        web_backend=OfflineWebBackend(),
        code_runner=DisabledCodeRunner(),
        notifier=ConsoleNotifier(),
    )
    yield runtime
    runtime.shutdown()
    NodusRuntime.clear_shared_state()


# ── start ──────────────────────────────────────────────────────────────────

def test_start_returns_waiting(rt):
    result = rt.start(QUESTION, SESSION)
    assert result.get("status") == "waiting"


def test_start_has_graph_id(rt):
    result = rt.start(QUESTION, SESSION)
    assert isinstance(result.get("graph_id"), str)
    assert len(result["graph_id"]) > 0


def test_start_wait_event_is_approval(rt):
    result = rt.start(QUESTION, SESSION)
    assert result["wait"]["event_type"] == "approval_required"


def test_start_draft_state_is_set(rt):
    result = rt.start(QUESTION, SESSION)
    draft = result["state"]["draft"]
    assert isinstance(draft, str)
    assert len(draft) > 0


# ── resume (approve) ───────────────────────────────────────────────────────

def test_approve_completes_publish(rt):
    r1 = rt.start(QUESTION, SESSION)
    r2 = rt.resume(r1["graph_id"], {"approved": True})
    assert r2["steps"]["publish"]["published"] is True


def test_approve_publish_carries_session_id(rt):
    r1 = rt.start(QUESTION, SESSION)
    r2 = rt.resume(r1["graph_id"], {"approved": True})
    assert r2["steps"]["publish"]["session_id"] == SESSION


def test_approve_writes_file_to_workspace(rt, tmp_path):
    r1 = rt.start(QUESTION, SESSION)
    rt.resume(r1["graph_id"], {"approved": True})
    out = tmp_path / "output" / SESSION / "final.md"
    assert out.exists(), f"expected output file at {out}"
    assert len(out.read_text(encoding="utf-8")) > 0


def test_approve_file_content_matches_draft(rt, tmp_path):
    r1 = rt.start(QUESTION, SESSION)
    draft = r1["state"]["draft"]
    rt.resume(r1["graph_id"], {"approved": True})
    out = tmp_path / "output" / SESSION / "final.md"
    assert out.read_text(encoding="utf-8") == draft


# ── resume_with_feedback (reject + revise) ─────────────────────────────────

def test_feedback_re_suspends(rt):
    r1 = rt.start(QUESTION, SESSION)
    r2 = rt.resume_with_feedback(r1["graph_id"], "needs more citations")
    assert r2.get("status") == "waiting"


def test_feedback_produces_revised_draft(rt):
    r1 = rt.start(QUESTION, SESSION)
    draft1 = r1["state"]["draft"]
    r2 = rt.resume_with_feedback(r1["graph_id"], "needs more citations")
    draft2 = r2["state"]["draft"]
    assert draft2 != draft1


def test_feedback_text_appears_in_revised_draft(rt):
    r1 = rt.start(QUESTION, SESSION)
    r2 = rt.resume_with_feedback(r1["graph_id"], "add concrete examples")
    assert "add concrete examples" in r2["state"]["draft"]


# ── full iteration ─────────────────────────────────────────────────────────

def test_full_iteration_reject_then_approve(rt, tmp_path):
    r1 = rt.start(QUESTION, SESSION)
    r2 = rt.resume_with_feedback(r1["graph_id"], "too vague")
    assert r2["status"] == "waiting"
    r3 = rt.resume(r1["graph_id"], {"approved": True})
    assert r3["steps"]["publish"]["published"] is True
    out = tmp_path / "output" / SESSION / "final.md"
    assert out.exists()


# ── LLM wiring (synthesize tool) ───────────────────────────────────────────

class _FakeLLM:
    """Records the chat() calls it receives and returns a canned draft."""

    def __init__(self):
        self.calls = []

    def chat(self, messages, model=None, temperature=0.7, max_tokens=None):
        self.calls.append({"messages": messages, "max_tokens": max_tokens})
        return "## Synthesized brief\n\nThis draft came from the LLM."


def test_draft_step_uses_injected_llm(tmp_path):
    NodusRuntime.clear_shared_state()
    fake = _FakeLLM()
    rt = ResearchRuntime(
        workspace=str(tmp_path),
        policy=ApprovalPolicy.allow_all(),
        llm_client=fake,
        web_backend=OfflineWebBackend(),
        code_runner=DisabledCodeRunner(),
        notifier=ConsoleNotifier(),
    )
    try:
        r1 = rt.start(QUESTION, SESSION)
        assert r1["state"]["draft"] == "## Synthesized brief\n\nThis draft came from the LLM."
        # The LLM saw a system prompt + a user prompt carrying the question.
        assert len(fake.calls) == 1
        roles = [m["role"] for m in fake.calls[0]["messages"]]
        assert roles == ["system", "user"]
        assert QUESTION in fake.calls[0]["messages"][1]["content"]
    finally:
        rt.shutdown()
        NodusRuntime.clear_shared_state()


def test_draft_step_feedback_reaches_llm(tmp_path):
    NodusRuntime.clear_shared_state()
    fake = _FakeLLM()
    rt = ResearchRuntime(
        workspace=str(tmp_path),
        policy=ApprovalPolicy.allow_all(),
        llm_client=fake,
        web_backend=OfflineWebBackend(),
        code_runner=DisabledCodeRunner(),
        notifier=ConsoleNotifier(),
    )
    try:
        r1 = rt.start(QUESTION, SESSION)
        rt.resume_with_feedback(r1["graph_id"], "add concrete examples")
        # Second synthesize call carries the reviewer feedback.
        assert len(fake.calls) == 2
        assert "add concrete examples" in fake.calls[1]["messages"][1]["content"]
    finally:
        rt.shutdown()
        NodusRuntime.clear_shared_state()
