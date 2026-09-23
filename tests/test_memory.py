"""Durable, tag-indexed memory and cross-session recall (`src/memory.py`).

Three layers, bottom-up:

1. ``topic_tags`` — the deterministic keyword heuristic that makes two
   differently-phrased questions about the same subject find each other.
2. ``SqliteMemoryStore`` — durability across "processes" (a second store object
   on the same file) and the tag-search semantics.
3. The agent — session A publishes, session B on a related question recalls A's
   findings and they reach B's draft.  This is the behaviour the plan's memory
   schema exists for.
"""
from __future__ import annotations

import json

import pytest

from nodus.runtime.embedding import NodusRuntime
from nodus_approvals import ApprovalPolicy
from nodus_retry import InMemoryEffectStore

from src.memory import SqliteMemoryStore, session_of, topic_tags
from src.notify import ConsoleNotifier
from src.runtime import ResearchRuntime, _ext_memory_recall
from src.sandbox import DisabledCodeRunner
from src.web import OfflineWebBackend


# ── topic tags ──────────────────────────────────────────────────────────────

def test_topic_tags_drops_stopwords_and_short_words():
    assert topic_tags("What is LLM safety?") == ["topic:llm", "topic:safety"]


def test_topic_tags_overlap_across_phrasings():
    a = set(topic_tags("What is LLM safety?"))
    b = set(topic_tags("Recent advances in LLM alignment and safety"))
    assert a & b == {"topic:llm", "topic:safety"}


def test_topic_tags_are_deterministic_deduped_and_capped():
    q = "Safety, safety, and more SAFETY in large language model deployment pipelines today"
    assert topic_tags(q) == topic_tags(q)
    assert len(topic_tags(q)) <= 8
    assert topic_tags(q).count("topic:safety") == 1


def test_topic_tags_of_an_empty_question_is_empty():
    assert topic_tags("") == []
    assert topic_tags("is a the of") == []


def test_session_of_parses_research_keys():
    assert session_of("research/s1/final") == "s1"
    assert session_of("research/s1/sources/abc") == "s1"
    assert session_of("something/else") is None


# ── SqliteMemoryStore ───────────────────────────────────────────────────────

@pytest.fixture()
def store(tmp_path):
    s = SqliteMemoryStore(tmp_path / "m.sqlite3")
    yield s
    s.close()


def tag(store, key, tags):
    """Tag as `std:memory`'s tag() does — the store indexes the prefixed key."""
    store.put(f"__nodus_tags__:{key}", tags)


def test_values_and_tags_survive_reopen(tmp_path):
    path = tmp_path / "m.sqlite3"
    a = SqliteMemoryStore(path)
    a.put("research/s1/final", "draft one")
    tag(a, "research/s1/final", ["session:s1", "topic:llm", "status:final"])
    a.close()

    b = SqliteMemoryStore(path)  # a second process
    try:
        assert b.get("research/s1/final") == "draft one"
        assert b.tags_for("research/s1/final") == ["session:s1", "status:final", "topic:llm"]
        assert b.search_by_tags(["topic:llm"]) == ["research/s1/final"]
    finally:
        b.close()


def test_search_any_all_and_overlap_ordering(store):
    store.put("a", 1)
    tag(store, "a", ["topic:llm", "topic:safety"])
    store.put("b", 2)
    tag(store, "b", ["topic:llm"])

    # "any" returns both, most overlap first; "all" demands every tag.
    assert store.search_by_tags(["topic:llm", "topic:safety"]) == ["a", "b"]
    assert store.search_by_tags(["topic:llm", "topic:safety"], match="all") == ["a"]
    assert store.search_by_tags(["topic:nope"]) == []
    assert store.search_by_tags([]) == []


def test_require_narrows_an_any_match(store):
    store.put("research/s1/final", "published")
    tag(store, "research/s1/final", ["topic:llm", "topic:safety", "status:final"])
    store.put("research/s1/draft", "half-written")
    tag(store, "research/s1/draft", ["topic:llm", "topic:safety", "status:draft"])

    # Any topic overlap, but only among published nodes: an unfinished draft
    # is not prior research.
    assert store.search_by_tags(["topic:llm", "topic:safety"]) == [
        "research/s1/draft", "research/s1/final",
    ]
    assert store.search_by_tags(
        ["topic:llm", "topic:safety"], require=["status:final"]
    ) == ["research/s1/final"]
    assert store.search_by_tags(["topic:llm"], require=["status:nonexistent"]) == []


def test_search_excludes_a_session(store):
    store.put("research/s1/final", "one")
    tag(store, "research/s1/final", ["topic:llm"])
    store.put("research/s2/final", "two")
    tag(store, "research/s2/final", ["topic:llm"])

    assert store.search_by_tags(["topic:llm"], exclude_session="s1") == ["research/s2/final"]


def test_recall_returns_values_respects_limit_and_skips_dangling(store):
    for i in range(3):
        store.put(f"research/s{i}/final", f"draft {i}")
        tag(store, f"research/s{i}/final", ["topic:llm"])

    recalled = store.recall_by_tags(["topic:llm"], limit=2)
    assert len(recalled) == 2
    assert recalled[0]["value"].startswith("draft")
    assert recalled[0]["session_id"] in {"s0", "s1", "s2"}
    assert "topic:llm" in recalled[0]["tags"]

    # A tagged key whose value is gone is not a memory.
    store.delete("research/s0/final")
    keys = [r["key"] for r in store.recall_by_tags(["topic:llm"], limit=10)]
    assert "research/s0/final" not in keys


def test_retagging_replaces_rather_than_accumulates(store):
    store.put("k", 1)
    tag(store, "k", ["topic:old"])
    tag(store, "k", ["topic:new"])
    assert store.tags_for("k") == ["topic:new"]
    assert store.search_by_tags(["topic:old"]) == []


def test_load_snapshot_truncates_values_and_tags(tmp_path):
    path = tmp_path / "m.sqlite3"
    a = SqliteMemoryStore(path)
    a.put("gone", 1)
    tag(a, "gone", ["topic:x"])
    a.load_snapshot({"kept": 2})
    a.close()

    b = SqliteMemoryStore(path)
    try:
        assert b.keys() == ["kept"]
        assert b.search_by_tags(["topic:x"]) == []
    finally:
        b.close()


def test_recall_handler_fails_open_without_a_store():
    out = _ext_memory_recall({"tags": ["topic:llm"]}, memory_store=None)
    assert out == {"results": [], "count": 0, "error": "memory_recall_unavailable"}


# ── the agent: cross-session recall ─────────────────────────────────────────

def make_rt(workspace) -> ResearchRuntime:
    NodusRuntime.clear_shared_state()
    return ResearchRuntime(
        workspace=str(workspace),
        policy=ApprovalPolicy.allow_all(),
        llm_client=None,
        web_backend=OfflineWebBackend(),
        code_runner=DisabledCodeRunner(),
        notifier=ConsoleNotifier(),
        effect_store=InMemoryEffectStore(),
    )


@pytest.fixture()
def workspace(tmp_path):
    yield tmp_path
    NodusRuntime.clear_shared_state()


def test_first_session_has_nothing_to_recall_and_still_runs(workspace):
    rt = make_rt(workspace)
    try:
        r = rt.start("What is LLM safety?", "sess-a")
        assert r["status"] == "waiting"
        assert r["steps"]["recall"]["count"] == 0
        assert "Prior research on this topic" not in r["state"]["draft"]
    finally:
        rt.shutdown()


def test_second_session_recalls_the_first_across_processes(workspace):
    # Session A: research a topic and publish it (status:final is what recall looks for).
    rt_a = make_rt(workspace)
    try:
        r1 = rt_a.start("What is LLM safety?", "sess-a")
        rt_a.resume(r1["graph_id"], {"approved": True})
        draft_a = r1["state"]["draft"]
    finally:
        rt_a.shutdown()

    # Session B: a differently-phrased question sharing topic words, in a new
    # runtime over the same workspace — a later process, not a later call.
    rt_b = make_rt(workspace)
    try:
        r2 = rt_b.start("Recent advances in LLM safety evaluation", "sess-b")
        recall = r2["steps"]["recall"]
        assert recall["count"] >= 1
        keys = [item["key"] for item in recall["results"]]
        # Only A's *published* final — not its meta node or its interim draft.
        assert keys == ["research/sess-a/final"]
        assert all(item["session_id"] != "sess-b" for item in recall["results"])

        # ...and it is actually used: A's findings are in B's draft.  The prior
        # block is embedded as JSON, so compare against the encoded form.
        draft_b = r2["state"]["draft"]
        assert "Prior research on this topic" in draft_b
        assert "research/sess-a/final" in draft_b
        assert json.dumps(draft_a)[1:60] in draft_b
    finally:
        rt_b.shutdown()


def test_unrelated_question_recalls_nothing(workspace):
    rt_a = make_rt(workspace)
    try:
        r1 = rt_a.start("What is LLM safety?", "sess-a")
        rt_a.resume(r1["graph_id"], {"approved": True})
    finally:
        rt_a.shutdown()

    rt_b = make_rt(workspace)
    try:
        r2 = rt_b.start("How do sourdough starters ferment?", "sess-b")
        assert r2["steps"]["recall"]["count"] == 0
    finally:
        rt_b.shutdown()


def test_session_nodes_are_written_and_tagged(workspace):
    rt = make_rt(workspace)
    try:
        r = rt.start("What is LLM safety?", "sess-a")
        rt.resume(r["graph_id"], {"approved": True})
        store = rt._memory_store

        assert store.get("research/sess-a/meta") == {"question": "What is LLM safety?"}
        assert store.get("research/sess-a/final") == r["state"]["draft"]

        final_tags = store.tags_for("research/sess-a/final")
        assert "status:final" in final_tags
        assert "session:sess-a" in final_tags
        assert "topic:llm" in final_tags

        # Each gather domain stored its fetched document under its content hash.
        source_keys = [k for k in store.keys() if k.startswith("research/sess-a/sources/")]
        assert len(source_keys) == 3
        domains = {d for k in source_keys for d in store.tags_for(k) if d.startswith("domain:")}
        assert domains == {"domain:web", "domain:code", "domain:data"}
    finally:
        rt.shutdown()
