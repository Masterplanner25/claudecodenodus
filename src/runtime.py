"""Python host for the research agent.

Manages NodusRuntime lifecycle, tool registration, and the start/resume
iteration protocol.  Extension handlers dispatch to the real implementations
in ``src/web.py``, ``src/sandbox.py`` and ``src/notify.py``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# Workflow run store: SQLite (crash-safe; the Nodus 6.0 default) rather than
# the file-backed JSON store that 5.x still defaults to.  Durable
# human-in-the-loop resume is this host's whole point, so choose explicitly —
# an unchosen backend is exactly what the 6.0 flip strands runs on (#174).
# Must be set before the first workflow runner is created; honours an
# explicit override from the environment.
os.environ.setdefault("NODUS_WORKFLOW_STORE_BACKEND", "sqlite")

from nodus.runtime.embedding import NodusRuntime
from nodus.orchestration.task_graph import get_registered_vm
from nodus_approvals import ApprovalGate, ApprovalPolicy
from nodus_llm import CredentialProfile, CredentialStore, FailoverClient
from nodus_retry import SqliteEffectStore

from src.approval_store import FileApprovalStore
from src.memory import SqliteMemoryStore, topic_tags
from src.web import HttpWebBackend, OfflineWebBackend, build_web_backend
from src.sandbox import DockerCodeRunner, build_code_runner
from src.notify import HttpNotifier, ConsoleNotifier, build_notifier

WORKFLOWS_DIR = Path(__file__).parent.parent / "workflows"
EXTENSIONS_DIR = Path(__file__).parent.parent / "extensions"

GATED_EFFECTS = frozenset({"fs.write", "network.write"})

# Sentinel: build the LLM client from the environment unless the caller passes one
# explicitly (including ``None`` to force the offline deterministic draft).
_AUTO_LLM = "__auto__"

# Sentinel: build the real HTTP web backend unless the caller passes one
# explicitly (e.g. ``OfflineWebBackend()`` for hermetic tests / offline mode).
_AUTO_WEB = "__auto__"

# Sentinel: build the real Docker code runner unless the caller passes one
# explicitly (e.g. ``DisabledCodeRunner()`` for hermetic tests).
_AUTO_CODE = "__auto__"

# Sentinel: build the real HTTP notifier unless the caller passes one explicitly
# (e.g. ``ConsoleNotifier()`` for hermetic tests).
_AUTO_NOTIFY = "__auto__"

# The synthesize tool is the in-process LLM bridge (effect llm.complete) — not a
# Docker-sandboxed extension, so it has no extensions/ manifest and is declared here.
_SYNTHESIZE_MANIFEST = {
    "name": "research.synthesize",
    "description": "Synthesize a research draft from gathered findings using an LLM.",
    "effects": ["llm.complete"],
    "schema": {"question": "string", "analysis": "string", "feedback": "string", "prior": "string"},
}


def load_tool_manifests(extensions_dir: Path = EXTENSIONS_DIR) -> list[dict]:
    """Load the Docker-sandboxed tool manifests from ``extensions/*/manifest.json``.

    Each extension directory owns its own manifest (name / description / effects /
    schema + declarative metadata).  The in-process ``research.synthesize`` LLM
    tool is appended — it has no extension sandbox.  Sorted by name for a
    deterministic registration order.
    """
    manifests: list[dict] = []
    for manifest_path in sorted(extensions_dir.glob("*/manifest.json")):
        with open(manifest_path, encoding="utf-8") as f:
            manifests.append(json.load(f))
    manifests.append(_SYNTHESIZE_MANIFEST)
    return sorted(manifests, key=lambda m: m["name"])


TOOL_MANIFESTS = load_tool_manifests()


def _llm_provider_factory(profile: CredentialProfile):
    """Map a credential profile to its provider client (lazy SDK imports)."""
    if profile.provider == "anthropic":
        from nodus_llm.providers.anthropic import AnthropicProvider
        return AnthropicProvider(profile)
    if profile.provider == "openai":
        from nodus_llm.providers.openai import OpenAIProvider
        return OpenAIProvider(profile)
    from nodus_llm.providers.compat import OpenAICompatProvider
    return OpenAICompatProvider(profile)


def build_llm_client() -> FailoverClient | None:
    """Build a FailoverClient from environment credentials, or ``None`` if none set.

    Reads ``ANTHROPIC_API_KEY`` (primary) and ``OPENAI_API_KEY`` (fallback).
    Models override via ``RESEARCH_LLM_MODEL`` / ``RESEARCH_OPENAI_MODEL``.
    When no keys are configured, returns ``None`` and the synthesize tool falls
    back to a deterministic offline draft — keeping tests hermetic.
    """
    profiles: list[CredentialProfile] = []
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if anthropic_key:
        profiles.append(CredentialProfile(
            "anthropic-primary", "anthropic", anthropic_key,
            os.environ.get("RESEARCH_LLM_MODEL", "claude-opus-4-8"), priority=0,
        ))
    openai_key = os.environ.get("OPENAI_API_KEY")
    if openai_key:
        profiles.append(CredentialProfile(
            "openai-fallback", "openai", openai_key,
            os.environ.get("RESEARCH_OPENAI_MODEL", "gpt-4o"), priority=1,
        ))
    if not profiles:
        return None
    return FailoverClient(CredentialStore(profiles), _llm_provider_factory)


class ResearchRuntime:
    """Host runtime for the research agent.

    Lifecycle::

        rt = ResearchRuntime(workspace="/tmp/research")
        result = rt.start("What is LLM safety?", "sess-001")
        # result["status"] == "waiting" — draft ready for review
        run_id = result["graph_id"]

        result2 = rt.resume(run_id, {"approved": True})
        # or: result2 = rt.resume_with_feedback(run_id, "needs more detail")
    """

    def __init__(self, *, workspace: str | None = None, policy=None, store=None, effect_store=None, memory_store=None, llm_client=_AUTO_LLM, web_backend=_AUTO_WEB, code_runner=_AUTO_CODE, notifier=_AUTO_NOTIFY) -> None:
        self.workspace = Path(workspace) if workspace else Path.cwd() / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / "output").mkdir(exist_ok=True)

        self._policy = policy if policy is not None else ApprovalPolicy.require_for_effects(TOOL_MANIFESTS, GATED_EFFECTS)
        # Default to a durable, file-backed store under the workspace so approvals
        # survive across processes — a human approving a gated tool in a different
        # process (the agent's whole point) sees the same approval state.
        self._store = store if store is not None else FileApprovalStore(self.workspace / ".approvals")
        self._gate = ApprovalGate(policy=self._policy, store=self._store)
        # Durable idempotency for ``@exactly_once`` (EXACT-001).  The annotation
        # dedups per VM unless the host injects a persistent EffectStore, so
        # without this a retry after a crash mid-publish -- in a new process --
        # would write and notify again.  SQLite under the workspace, like the
        # approval store: an effect recorded here is visible to every process
        # that resumes this workspace.  ``:memory:`` or InMemoryEffectStore for tests.
        self._effect_store = effect_store if effect_store is not None else SqliteEffectStore(str(self.workspace / ".effects.sqlite3"))
        # Durable, tag-indexed memory.  Two reasons not to take the default:
        # ``std:memory``'s default store is a *process-global* singleton shared by
        # every NodusRuntime (VM-001), and it is in-memory, so nothing a session
        # learns outlives it.  This one is per-workspace and persists, which is
        # what makes cross-session recall mean anything.
        self._memory_store = memory_store if memory_store is not None else SqliteMemoryStore(self.workspace / ".memory.sqlite3")
        self._llm_client = build_llm_client() if llm_client == _AUTO_LLM else llm_client
        # Real HTTP backend by default; tests/offline mode inject OfflineWebBackend.
        self._web_backend = build_web_backend() if web_backend == _AUTO_WEB else web_backend
        # Real Docker sandbox by default; tests inject DisabledCodeRunner.
        self._code_runner = build_code_runner() if code_runner == _AUTO_CODE else code_runner
        # Real HTTP notifier by default; tests inject ConsoleNotifier.
        self._notifier = build_notifier() if notifier == _AUTO_NOTIFY else notifier

        self._runtime = NodusRuntime(
            project_root=str(Path(__file__).parent.parent),
            max_steps=None,
            timeout_ms=None,
            allow_network=True,
            allow_subprocess=False,
            memory_store=self._memory_store,
        )
        # Must precede the first run_source: the store is attached to each VM
        # the runtime builds (start AND the primed cross-process resume VM).
        self._runtime.set_effect_store(self._effect_store)

        # Cache workflow source so vm.source_code can be set before run_workflow runs.
        # This lets _rebuild_workflow_graph find the source across run_source boundaries.
        with open(WORKFLOWS_DIR / "research_task.nd", encoding="utf-8") as _f:
            self._workflow_source_code = _f.read()

        self._last_result: dict[str, Any] = {}
        self._runtime.register_function(
            "_capture",
            lambda r: self._last_result.update({"value": r}),
            arity=1,
        )
        self._runtime.register_function(
            "_prime_vm",
            lambda: self._runtime._get_active_vm().__setattr__("source_code", self._workflow_source_code),
            arity=0,
        )

        self._register_tools()

    def _register_tools(self) -> None:
        for manifest in TOOL_MANIFESTS:
            name = manifest["name"]
            gated = bool(frozenset(manifest.get("effects", [])) & GATED_EFFECTS)
            self._runtime.tool_registry.register({
                "name": name,
                "description": manifest["description"],
                "handler": self._make_handler(name, gated=gated),
                "schema": manifest.get("schema", {}),
            })

    def _make_handler(self, name: str, *, gated: bool):
        workspace = self.workspace
        gate = self._gate

        def handler(args: dict) -> dict:
            if gated:
                result = gate.check(name, requester_id="workflow", context=args)
                if result is None:
                    raise RuntimeError(
                        f"Tool '{name}' requires human approval. "
                        "This should not happen inside a post-approval publish step."
                    )
                if not result.approved:
                    raise RuntimeError(f"Tool '{name}' was denied by policy.")
            return _dispatch(
                name, args,
                workspace=workspace,
                llm_client=self._llm_client,
                web_backend=self._web_backend,
                code_runner=self._code_runner,
                notifier=self._notifier,
                memory_store=self._memory_store,
            )

        return handler

    def _run(self, source: str, initial_globals: dict) -> dict:
        self._last_result.clear()
        result = self._runtime.run_source(source, initial_globals=initial_globals)
        if not result["ok"]:
            err = result.get("error", {})
            raise RuntimeError(f"Nodus error: {err.get('message', result)}")
        return self._last_result.get("value", {})

    def _workflow_source(self, driver: str) -> str:
        return self._workflow_source_code + "\n_prime_vm()\n" + driver

    @staticmethod
    def _initial_globals(question: str, session_id: str) -> dict:
        """Globals injected into the workflow before ``run_workflow``.

        The notify channel/target default to a real console notification so the
        agent always delivers *something* real without external setup; point them
        at a webhook/Slack URL via ``RESEARCH_NOTIFY_CHANNEL`` /
        ``RESEARCH_NOTIFY_TARGET`` for out-of-process delivery.
        """
        return {
            "_init_question": question,
            "_init_session_id": session_id,
            "_init_notify_channel": os.environ.get("RESEARCH_NOTIFY_CHANNEL", "console"),
            "_init_notify_target": os.environ.get("RESEARCH_NOTIFY_TARGET", "researcher"),
            # Topic tags are derived host-side so they are identical on the
            # original run and on any rehydrated resume (the workflow tags and
            # recalls with them, so drift would split a topic in two).
            "_init_topic_tags": topic_tags(question),
        }

    def start(self, question: str, session_id: str) -> dict:
        """Start a new research workflow. Returns the run result (status=waiting after draft)."""
        source = self._workflow_source("_capture(run_workflow(research_task))")
        return self._run(source, self._initial_globals(question, session_id))

    def resume(self, run_id: str, payload: dict) -> dict:
        """Resume a waiting run with an approval payload.

        Pass ``{"approved": True}`` to proceed to publish, or use
        ``resume_with_feedback`` to replay the draft from its checkpoint.

        Works both in-process (fast path: reuse the start VM) and cross-process
        (prime a fresh VM and let the framework rebuild the graph).
        """
        return self._resume_on_vm(run_id, None, payload)

    def resume_with_feedback(self, run_id: str, feedback: str) -> dict:
        """Reject the draft and replay from the 'before_draft' checkpoint with feedback."""
        return self._reject_and_replay(run_id, feedback, self._resume_on_vm)

    def _reject_and_replay(self, run_id: str, feedback: str, resume) -> dict:
        """Two-phase rejection, as Nodus v5 requires (#482).

        A checkpoint rollback on a run that is *waiting* is refused in v5: the
        rollback would re-enter ``review``, re-arm the wait and drop the payload.
        So a rejection is (1) satisfy the wait with ``approved: false`` — the
        run completes with ``publish`` gated off, no side effects — then (2) roll
        back to ``before_draft`` carrying the feedback, which replays the draft
        and re-suspends at ``review``.  Both phases go through ``resume`` so the
        in-process and fresh-process paths behave identically.
        """
        rejected = resume(run_id, None, {"approved": False, "feedback": feedback})
        if isinstance(rejected, dict) and rejected.get("ok") is False:
            return rejected
        return resume(run_id, "before_draft", {"feedback": feedback})

    def resume_in_fresh_process(self, run_id: str, payload: dict, *, checkpoint: str | None = None) -> dict:
        """Resume a persisted run as if from a brand-new process.

        Always primes a fresh VM (ignoring the in-memory task-graph registry), so
        the framework's ``_rebuild_workflow_graph`` runs and re-binds the
        workflow's ``import`` statements — the durable human-in-the-loop path: a
        person approves a draft hours later, in a different process or machine.

        Requirements the *host* must satisfy (not reconstructable from workflow
        source): tools and the LLM client must be re-supplied — done by
        ``ResearchRuntime.__init__`` — and any gated tool needs a durable
        approval store (the default ``FileApprovalStore``) so an approval
        recorded elsewhere is visible here.
        """
        if checkpoint is not None and "feedback" in payload:
            return self._reject_and_replay(run_id, payload["feedback"], self._resume_fresh)
        return self._resume_fresh(run_id, checkpoint, payload)

    def _resume_fresh(self, run_id: str, checkpoint: str | None, payload: dict) -> dict:
        vm = self._prime_resume_vm()
        raw = self._resume_on_primed_vm(vm, run_id, checkpoint, payload)
        return self._runtime._to_host_value(raw)

    @staticmethod
    def _resume_on_primed_vm(vm, run_id: str, checkpoint: str | None, payload: dict):
        """Resume ``run_id`` with the rebuild targeting *this* primed VM.

        Nodus v5's ``builtin_resume_workflow`` routes a rebuild away from any VM
        that has a program loaded (``_resume_target_vm``, #328) onto a child VM
        that inherits host globals and builtins — but **not** ``tool_registry``
        nor the injected ``effect_store`` (it gets a fresh per-VM one).
        On that child, ``tool.call`` fails soft with ``tool_not_found`` and the
        resumed ``publish`` silently does nothing.  A primed VM has *finished*
        its program (``_prime_vm()`` is its last statement), so there is no
        continuation to clobber; hand it to the runner directly, exactly as the
        builtin does for a bare VM, and the tools stay bound.
        """
        return vm.resolve_workflow_runner().resume_workflow(
            vm, run_id, checkpoint,
            resume_payload=payload,
            rebuild_graph=vm._rebuild_workflow_graph,
        )

    def _prime_resume_vm(self):
        """Build a fresh VM with the workflow's imports + this host's tools bound.

        Runs the workflow *declarations* (no ``run_workflow`` — so nothing starts)
        through ``run_source``; the resulting VM has ``tool``/``mem``/``json``
        bound and the tool registry attached.  ``builtin_resume_workflow`` called
        on this VM triggers the framework rebuild because it differs from the
        run's originally-registered VM.
        """
        self._runtime.run_source(
            self._workflow_source_code + "\n_prime_vm()\n",
            initial_globals=self._initial_globals("", ""),
        )
        vm = self._runtime._get_active_vm()
        if vm is None:
            raise RuntimeError("Failed to prime a VM for cross-process resume.")
        return vm

    def _resume_on_vm(self, run_id: str, checkpoint: str | None, payload: dict) -> dict:
        """Resume a workflow run, in-process or cross-process.

        Fast path (in-process): the VM that started the run is still in the
        task-graph registry, with imports already bound — reuse it directly.

        Fallback (cross-process / VM evicted): no registered VM, so prime a fresh
        one and let ``builtin_resume_workflow`` rebuild the graph (v4.0.7+
        rebuilds through the module-load path, which re-binds the workflow's imports).  (This
        used to be impossible: the pre-4.0.7 rebuild path used ``compile_only``,
        which was import-blind, so the rebuilt VM lacked ``tool``/``mem``/``json``.)
        """
        vm = get_registered_vm(run_id)
        if vm is None:
            vm = self._prime_resume_vm()
            raw = self._resume_on_primed_vm(vm, run_id, checkpoint, payload)
        else:
            raw = vm.builtin_resume_workflow(run_id, checkpoint, payload)
        return self._runtime._to_host_value(raw)

    def gate_approve(self, request_id: str, approver_id: str = "human") -> None:
        """Approve a pending gate request (for gated tools called during publish)."""
        self._gate.approve(request_id, approver_id=approver_id)

    def gate_deny(self, request_id: str, approver_id: str = "human") -> None:
        self._gate.deny(request_id, approver_id=approver_id)

    def pending_gate_requests(self) -> list:
        return self._store.pending()

    def shutdown(self) -> None:
        if self._web_backend is not None:
            self._web_backend.close()
        if self._notifier is not None:
            self._notifier.close()
        for closeable in (self._effect_store, self._memory_store):
            close = getattr(closeable, "close", None)
            if close is not None:
                close()
        self._runtime.shutdown()
        NodusRuntime.clear_shared_state()


# ---------------------------------------------------------------------------
# Extension dispatch
# ---------------------------------------------------------------------------
# web_search / fetch_doc are real (src/web.py), run_code is Docker-sandboxed
# (src/sandbox.py), and notify does real delivery (src/notify.py).

def _dispatch(name: str, args: dict, *, workspace: Path, llm_client=None, web_backend=None, code_runner=None, notifier=None, memory_store=None) -> dict:
    if name == "research.web_search":
        return _ext_web_search(args, web_backend=web_backend)
    if name == "research.fetch_doc":
        return _ext_fetch_doc(args, web_backend=web_backend)
    if name == "research.run_code":
        return _ext_run_code(args, code_runner=code_runner)
    if name == "research.synthesize":
        return _ext_synthesize(args, llm_client=llm_client)
    if name == "research.write_file":
        return _ext_write_file(args, workspace=workspace)
    if name == "research.notify":
        return _ext_notify(args, notifier=notifier)
    if name == "research.memory_recall":
        return _ext_memory_recall(args, memory_store=memory_store)
    raise ValueError(f"Unknown extension: {name}")


def _ext_web_search(args: dict, *, web_backend=None) -> dict:
    """Real web search via the configured backend (see ``src/web.py``).

    Fails open: a network/provider error returns an empty result set with an
    ``error`` marker rather than crashing the gather step, matching the
    workflow's fail-open contract.
    """
    if web_backend is None:
        web_backend = OfflineWebBackend()
    query = args.get("query", "")
    max_results = int(args.get("max_results", 5))
    try:
        return web_backend.search(query, max_results=max_results)
    except Exception as exc:  # fail-open per gather-step contract
        return {"results": [], "error": f"{type(exc).__name__}: {exc}"}


def _ext_fetch_doc(args: dict, *, web_backend=None) -> dict:
    """Real URL fetch + text extraction via the configured backend.

    Fails open: returns an empty-text document (with an ``error`` marker and a
    deterministic ``content_hash``) so a single bad URL does not abort a gather.
    """
    if web_backend is None:
        web_backend = OfflineWebBackend()
    url = args.get("url", "")
    max_chars = int(args.get("max_chars", 10000))
    try:
        return web_backend.fetch(url, max_chars=max_chars)
    except Exception as exc:  # fail-open per gather-step contract
        from src.web import _sha256
        return {
            "text": "",
            "title": url,
            "fetched_at": "",
            "content_hash": _sha256(url),
            "error": f"{type(exc).__name__}: {exc}",
        }


def _ext_run_code(args: dict, *, code_runner=None) -> dict:
    """Execute Python in a Docker sandbox via the configured runner.

    Fails soft: a missing/dead Docker daemon returns a result dict with an
    ``error`` marker (from the runner) rather than raising.
    """
    if code_runner is None:
        from src.sandbox import DisabledCodeRunner
        code_runner = DisabledCodeRunner()
    code = args.get("code", "")
    timeout_seconds = args.get("timeout_seconds")
    return code_runner.run(code, timeout_seconds=timeout_seconds)


_SYNTH_SYSTEM = (
    "You are a research analyst. Using only the supplied findings, write a clear, "
    "well-structured markdown research brief that answers the question. Cite source "
    "URLs inline where the findings support a claim. Be concise and factual."
)


def _canonical_json(text: str) -> str:
    """Re-serialise a JSON document with sorted keys and fixed separators.

    Step results rehydrated from the workflow store come back key-sorted (the
    store persists with ``sort_keys=True``), while a live run's maps keep
    insertion order, and ``std:json.stringify`` has no canonical mode.  The
    draft embeds the analysis, and the draft is part of ``publish_once``'s
    idempotency key — so the same findings must serialise to the same bytes
    in the original process and in a rehydrated one.  Non-JSON input is
    returned unchanged.
    """
    try:
        return json.dumps(json.loads(text), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return text


def _ext_synthesize(args: dict, *, llm_client=None) -> dict:
    """Produce a research draft from gathered findings.

    Uses the configured LLM (``FailoverClient``) when available; otherwise falls
    back to a deterministic offline draft so the workflow is runnable — and
    tested — without credentials.  Returns ``{"draft": <markdown>}``.
    """
    question = args.get("question", "")
    analysis = _canonical_json(args.get("analysis", ""))
    feedback = args.get("feedback", "") or ""
    # Recalled work from earlier sessions on the same topic.  Canonicalised
    # for the same reason as the analysis: the draft is an idempotency key.
    prior = _canonical_json(args.get("prior", "") or "")
    prior_block = f"Prior research on this topic:\n{prior}\n\n" if prior and prior not in ("[]", '""') else ""

    if llm_client is None:
        if feedback:
            body = (
                f"Revised draft on: {question}\n\n"
                f"Addressing feedback: {feedback}\n\n"
                f"{prior_block}"
                f"Findings considered:\n{analysis}"
            )
        else:
            body = f"Initial draft on: {question}\n\n{prior_block}Findings considered:\n{analysis}"
        return {"draft": body}

    recalled = (
        f"Findings from earlier research sessions on this topic (JSON):\n{prior}\n"
        "Treat these as background: reuse what still holds, and say so if the new findings"
        " contradict them.\n\n"
        if prior_block else ""
    )
    if feedback:
        user = (
            f"Question: {question}\n\n{recalled}Findings (JSON):\n{analysis}\n\n"
            f"Revise the research brief to address this reviewer feedback: {feedback}"
        )
    else:
        user = (
            f"Question: {question}\n\n{recalled}Findings (JSON):\n{analysis}\n\n"
            "Write the initial research brief answering the question from these findings."
        )
    text = llm_client.chat(
        [{"role": "system", "content": _SYNTH_SYSTEM}, {"role": "user", "content": user}],
        max_tokens=1500,
    )
    return {"draft": text}


def _ext_write_file(args: dict, *, workspace: Path) -> dict:
    rel_path = args.get("path", "output/result.md")
    content = args.get("content", "")
    full_path = workspace / rel_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content, encoding="utf-8")
    return {"written_bytes": len(content.encode()), "path": str(full_path)}


def _ext_memory_recall(args: dict, *, memory_store=None) -> dict:
    """Recall prior sessions' nodes by tag.  Fails open: no memory, no recall.

    ``exclude_session`` keeps a run from recalling itself — the current
    session's own nodes are already in its state.
    """
    if memory_store is None or not hasattr(memory_store, "recall_by_tags"):
        return {"results": [], "count": 0, "error": "memory_recall_unavailable"}
    tags = args.get("tags") or []
    exclude = args.get("exclude_session") or None
    limit = int(args.get("limit", 5) or 5)
    match = args.get("match") or "any"
    require = args.get("require") or None
    try:
        results = memory_store.recall_by_tags(
            tags, match=match, require=require, exclude_session=exclude, limit=limit,
        )
    except Exception as exc:  # a recall must never be able to fail a research run
        return {"results": [], "count": 0, "error": str(exc)}
    return {"results": results, "count": len(results)}


def _ext_notify(args: dict, *, notifier=None) -> dict:
    """Deliver a notification via the configured notifier (see ``src/notify.py``).

    Fails soft: the notifier returns ``delivered: False`` with an error string
    rather than raising, so a failed notification never aborts publish.
    """
    if notifier is None:
        from src.notify import ConsoleNotifier
        notifier = ConsoleNotifier()
    return notifier.notify(
        channel=args.get("channel", "console"),
        target=args.get("target", ""),
        message=args.get("message", ""),
        run_id=args.get("run_id", ""),
    )
