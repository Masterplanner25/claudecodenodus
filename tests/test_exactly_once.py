"""Durable ``@exactly_once`` for the publish side effects (EXACT-001).

The annotation dedups per VM only, *unless* the host injects a persistent
EffectStore.  ``ResearchRuntime`` injects ``nodus_retry.SqliteEffectStore``
under the workspace, so the publish effects — the file write and the
notification — fire once per distinct ``(session, draft, question, channel,
target)`` across processes: a retry after a crash mid-publish, or a checkpoint
replay that regenerates an identical draft, is served from the store; a
revised draft is a new key and publishes normally.

"Second process" here is a second ``ResearchRuntime`` over the same workspace
with its own notifier, resumed through the fresh-VM path.
"""
from __future__ import annotations

import pytest

from nodus.runtime.embedding import NodusRuntime
from nodus_approvals import ApprovalPolicy
from nodus_retry import InMemoryEffectStore, SqliteEffectStore

from src.notify import ConsoleNotifier
from src.runtime import ResearchRuntime
from src.sandbox import DisabledCodeRunner
from src.web import OfflineWebBackend

QUESTION = "What is durable idempotency?"
SESSION = "eo-sess-001"


def make_rt(workspace, **kw) -> ResearchRuntime:
    NodusRuntime.clear_shared_state()
    return ResearchRuntime(
        workspace=str(workspace),
        policy=ApprovalPolicy.allow_all(),
        llm_client=None,
        web_backend=OfflineWebBackend(),
        code_runner=DisabledCodeRunner(),
        notifier=ConsoleNotifier(),
        **kw,
    )


@pytest.fixture()
def workspace(tmp_path):
    yield tmp_path
    NodusRuntime.clear_shared_state()


def test_default_effect_store_is_sqlite_under_workspace(workspace):
    rt = make_rt(workspace)
    try:
        assert isinstance(rt._effect_store, SqliteEffectStore)
        r1 = rt.start(QUESTION, SESSION)
        assert len(rt._effect_store) == 0  # nothing published yet
        r2 = rt.resume(r1["graph_id"], {"approved": True})
        assert r2["steps"]["publish"]["published"] is True
        assert r2["steps"]["publish"]["effects"]["notified"]["delivered"] is True
        assert len(rt._effect_store) == 1
    finally:
        rt.shutdown()
    assert (workspace / ".effects.sqlite3").exists()


def test_identical_replay_in_fresh_process_does_not_republish(workspace):
    # Process A: research, approve, publish.
    rt_a = make_rt(workspace)
    try:
        r1 = rt_a.start(QUESTION, SESSION)
        run_id = r1["graph_id"]
        r2 = rt_a.resume(run_id, {"approved": True})
        assert r2["steps"]["publish"]["published"] is True
        assert len(rt_a._notifier.sent) == 1
        out = workspace / "output" / SESSION / "final.md"
        first_mtime = out.stat().st_mtime_ns
        draft_a = r1["state"]["draft"]
    finally:
        rt_a.shutdown()

    # Process B: same workspace, fresh runtime + fresh notifier.  Replay from
    # before the draft with no feedback -> the offline synthesizer regenerates
    # the identical draft -> approving again must not fire the effects.
    rt_b = make_rt(workspace)
    try:
        r3 = rt_b.resume_in_fresh_process(run_id, {"feedback": ""}, checkpoint="before_draft")
        assert r3.get("status") == "waiting"
        assert r3["state"]["draft"] == draft_a
        r4 = rt_b.resume_in_fresh_process(run_id, {"approved": True})
        assert r4["steps"]["publish"]["published"] is True
        assert r4["steps"]["publish"]["effects"]["notified"]["delivered"] is True  # replayed result
        assert rt_b._notifier.sent == []                        # ...not a new delivery
        assert out.stat().st_mtime_ns == first_mtime            # ...and no rewrite

        # A revised draft is a new key: publish fires again, once.
        r5 = rt_b.resume_in_fresh_process(run_id, {"feedback": "add examples"}, checkpoint="before_draft")
        assert r5.get("status") == "waiting"
        assert r5["state"]["draft"] != draft_a
        r6 = rt_b.resume_in_fresh_process(run_id, {"approved": True})
        assert r6["steps"]["publish"]["published"] is True
        assert len(rt_b._notifier.sent) == 1
        assert out.read_text(encoding="utf-8") == r5["state"]["draft"]
        assert len(rt_b._effect_store) == 2
    finally:
        rt_b.shutdown()


def test_injected_in_memory_store_is_per_runtime(workspace):
    # The pre-fix behaviour, kept honest: with a non-durable store a second
    # runtime over the same workspace has no memory of the first publish.
    rt_a = make_rt(workspace, effect_store=InMemoryEffectStore())
    try:
        r1 = rt_a.start(QUESTION, SESSION)
        run_id = r1["graph_id"]
        rt_a.resume(run_id, {"approved": True})
        assert len(rt_a._notifier.sent) == 1
    finally:
        rt_a.shutdown()

    rt_b = make_rt(workspace, effect_store=InMemoryEffectStore())
    try:
        rt_b.resume_in_fresh_process(run_id, {"feedback": ""}, checkpoint="before_draft")
        rt_b.resume_in_fresh_process(run_id, {"approved": True})
        assert len(rt_b._notifier.sent) == 1  # fired again: no durable record
    finally:
        rt_b.shutdown()
    assert not (workspace / ".effects.sqlite3").exists()
