# Nodus Research Agent — Design Plan

## Vision

An autonomous research agent that takes a question, fans out across sources in parallel, synthesizes findings with an LLM, and gates every write action on human approval before publishing. The workflow dependency graph is fixed and auditable; the LLM reasons within steps only.

---

## Architecture

Three distinct layers. Nodus is the middle one.

```
┌─────────────────────────────────────────┐
│  DRIVER LAYER                           │
│  LLM (via nodus_llm.FailoverClient)     │
│  — reasons within each step             │
│  — does NOT control the workflow shape  │
└────────────────┬────────────────────────┘
                 │  tool_call / agent_call
┌────────────────▼────────────────────────┐
│  ORCHESTRATION LAYER (Nodus)            │
│  workflow research_task { ... }         │
│  — fixed DAG of named steps             │
│  — approval gate before write actions   │
│  — @exactly_once on every fetch         │
│  — memory read/write between steps      │
└────────────────┬────────────────────────┘
                 │  extension invoke (Docker)
┌────────────────▼────────────────────────┐
│  TOOL LAYER (Extensions)                │
│  research.web_search   [network.read]   │
│  research.fetch_doc    [network.read]   │
│  research.run_code     [subprocess.exec]│
│  research.write_file   [fs.write] ←gate │
│  research.notify       [network.write]←g│
└─────────────────────────────────────────┘
```

---

## Workflow Structure

### Single workflow (linear DAG with checkpoint-replay iteration)

The draft-review loop is implemented using `workflow_wait()` + checkpoint replay, not a
nested goal. `yield` is a coroutine primitive that crashes inside workflow steps. Goals
have no loop mechanism — they are DAGs that run once, structurally identical to workflows.

```nodus
workflow research_task {
  state {
    question:   nil
    session_id: nil
    sources:    []
    analysis:   {}
    draft:      nil
  }

  step init {
    // generate session_id
    // write research/{session_id}/meta to memory
  }

  step gather_web after init { }
  step gather_code after init { }
  step gather_data after init { }
  // each step fails-open: writes result or empty marker to memory
  // @exactly_once per fetch, action_id = content_hash of URL

  step analyze after gather_web, gather_code, gather_data {
    // reads research/{session_id}/sources/*
    // processes whatever arrived (fail-open means some may be empty)
    // writes research/{session_id}/analysis/{hash} per source
  }

  step draft_step after analyze {
    checkpoint "before_draft"
    // On first run: workflow_resume_payload() returns nil
    // On replay (rejection): workflow_resume_payload() returns {approved: false, feedback: "..."}
    let p = workflow_resume_payload()
    if (p != nil) {
      let fb = p["feedback"]
      // LLM revises draft incorporating feedback
    } else {
      // LLM writes initial draft from analysis
    }
    // archives revision to memory: research/{session_id}/revisions/{n}/draft
    // updates state.draft
  }

  step review after draft_step {
    // suspend and hand draft to human approver
    return workflow_wait("approval_required", state.session_id, {
      "event":    "approval_required",
      "run_id":   state.session_id,
      "draft":    state.draft
    })
  }

  step publish after review {
    // both tools below are gated (declared effects: fs.write / network.write)
    let p = workflow_resume_payload()      // the payload that satisfied review's wait
    // Gate EVERY side effect on p["approved"] == true: a rejection also passes
    // through here (see protocol below) and must be a no-op.
    if (!approved) { return {"published": false, "revision_requested": true} }
    action tool "research.write_file" with { path: ..., content: state.draft }
    action tool "research.notify" with { channel: ..., message: ... }
  }
}
```

### Iteration protocol (Python caller)

```
run_workflow(research_task, {question: "...", session_id: "..."})
  → status "waiting" — human sees draft

if rejected (two resumes — Nodus v5 #482):
  resume_workflow(run_id, {approved: false, feedback: "..."})
  → satisfies the wait; publish sees approved=false and no-ops; run completes
  resume_workflow(run_id, "before_draft", {feedback: "..."})
  → rolls back, replays draft_step + review, status "waiting" again

if approved:
  resume_workflow(run_id, {approved: true})
  → publish runs to completion
```

> **Why two calls (Nodus ≥ 5.0):** a checkpoint rollback on a run that is
> *waiting* is refused (`category: waiting_run_checkpoint_resume`) — the engine
> would re-enter `review`, re-arm the wait and drop the payload. The v4 one-call
> form `resume_workflow(id, "before_draft", {feedback})` no longer works on a
> waiting run. `ResearchRuntime._reject_and_replay` encapsulates the pair.

The loop is managed by the Python caller, not inside Nodus. Each `resume_workflow` call is
synchronous. The workflow run_id is stable across all iterations.

---

## Memory Schema

All memory nodes use path-based addressing within a session, plus tags for cross-session retrieval.

```
research/{session_id}/meta
research/{session_id}/sources/{content_hash}
research/{session_id}/analysis/{content_hash}
research/{session_id}/draft
research/{session_id}/final
research/{session_id}/revisions/{n}/draft
research/{session_id}/revisions/{n}/feedback
research/{session_id}/revisions/{n}/timestamp
```

**Tags on each node:**
- `session:{session_id}`
- `topic:{extracted_topic}`
- `domain:{web|code|data}`
- `status:{raw|analyzed|draft|final}`

The `content_hash` for source nodes is also the memory address for retrieval.

> **Revised (2026-09-21):** `content_hash` is a *cache* key, not an idempotency key — it is only known
> after the fetch it would guard, and re-fetching is harmless. `@exactly_once` belongs on the effects
> with external consequences: `publish_once(session_id, draft, question, channel, target)` wraps the
> file write + notification, keyed on its full inputs, and the host injects a durable
> `nodus_retry.SqliteEffectStore` (see EXACT-001 below).

Cross-session recall: `search_by_tags(["topic:llm-safety"])` returns relevant nodes across all sessions.

---

## Extension Manifests

All five tools run in the `container` sandbox tier (Docker, `--cap-drop ALL --network none` for code execution). The approval gate is wired to effects, not tool names — any tool declaring `fs.write` or `network.write` automatically requires human approval.

### research.web_search
```json
{
  "name": "research.web_search",
  "description": "Query a search engine, return ranked URL + snippet list",
  "version": "1.0",
  "sandbox_tier": "container",
  "schema": { "query": "string", "max_results": "integer" },
  "returns_schema": { "results": "array" },
  "effects": ["network.read"],
  "provenance": { "trust_class": "trusted", "origin": "local" }
}
```

### research.fetch_doc
```json
{
  "name": "research.fetch_doc",
  "description": "Fetch and parse a URL into clean text. Returns content_hash for idempotency key.",
  "version": "1.0",
  "sandbox_tier": "container",
  "schema": { "url": "string", "max_chars": "integer" },
  "returns_schema": {
    "text": "string",
    "title": "string",
    "fetched_at": "string",
    "content_hash": "string"
  },
  "effects": ["network.read"],
  "provenance": { "trust_class": "trusted", "origin": "local" }
}
```

### research.run_code
```json
{
  "name": "research.run_code",
  "description": "Execute Python in an isolated container, return stdout/stderr/exit_code",
  "version": "1.0",
  "sandbox_tier": "container",
  "schema": { "code": "string", "timeout_seconds": "integer" },
  "returns_schema": { "stdout": "string", "stderr": "string", "exit_code": "integer" },
  "effects": ["subprocess.exec"],
  "provenance": { "trust_class": "trusted", "origin": "local" }
}
```

### research.write_file ← APPROVAL GATED
```json
{
  "name": "research.write_file",
  "description": "Write final output to a file in the workspace",
  "version": "1.0",
  "sandbox_tier": "container",
  "schema": { "path": "string", "content": "string", "format": "string" },
  "returns_schema": { "written_bytes": "integer", "path": "string" },
  "effects": ["fs.write"],
  "provenance": { "trust_class": "trusted", "origin": "local" }
}
```

### research.notify ← APPROVAL GATED
```json
{
  "name": "research.notify",
  "description": "Send a notification via webhook, email, or Slack",
  "version": "1.0",
  "sandbox_tier": "container",
  "schema": {
    "channel": "string",
    "target": "string",
    "message": "string",
    "run_id": "string"
  },
  "returns_schema": { "delivered": "boolean", "response": "string" },
  "effects": ["network.write"],
  "provenance": { "trust_class": "trusted", "origin": "local" }
}
```

---

## Approval Gate Policy

```python
ApprovalPolicy.require_for_effects(["network.write", "fs.write"])
```

`require_for_effects()` does not yet exist in `nodus_approvals` — needs to be added alongside `require_for()`. Same pattern; routes through the tool manifest's `effects` list rather than matching on the action string.

---

## Design Decisions Log

| Decision | Choice | Reason |
|---|---|---|
| LLM placement | Nodus calls LLM as a tool | Workflow shape stays fixed and auditable; LLM reasons within steps only |
| Gather granularity | Separate steps per domain | Different failure modes and timeouts per domain; fail-open per step keeps analyze simple |
| Memory scheme | Path-primary + tags | content_hash is the memory address (a cache key; idempotency lives on `publish_once`) |
| Approval interaction | workflow_wait() + resume_workflow() | workflow_wait() returns sentinel that suspends the DAG; resume_workflow() delivers payload read via workflow_resume_payload() in subsequent steps |
| Draft-review loop | checkpoint replay in single workflow | Goals have no loop mechanism (DAG, runs once). yield crashes in workflow steps. Rejection = satisfy the wait with `{approved:false, feedback}` (publish gated off), then `resume_workflow(id, "before_draft", {feedback})` re-runs draft_step through to the next review suspension. (v5 refuses the rollback while the run is still waiting.) |
| Iteration control | Python caller, not Nodus | The loop is managed outside Nodus — Python checks approved flag and decides whether to replay checkpoint or proceed to publish. |
| Feedback path | workflow_resume_payload() in draft_step | After checkpoint rollback, the replayed draft_step reads workflow_resume_payload() to get the feedback from the previous rejection. |
| Gate mechanism | Effect-based, not name-based | New tools with write effects are automatically gated; no allowlist maintenance |

---

## Known Nodus Defects Affecting This Project

| ID | Impact on this project | Workaround |
|---|---|---|
| COMPILER-001 | `@retry` annotation is a no-op — passes wrong keys to policy builder | Use `retry.call(fn(){...}, {"max_attempts": N, "backoff_ms": M})` directly |
| VM-001 | GLOBAL_MEMORY_STORE shared across NodusRuntime instances in same process | One runtime per session; call `NodusRuntime.clear_shared_state()` between test runs |
| TYPES-001 | Type annotations are unenforced | Don't rely on them for correctness; document intent in comments only |
| EXACT-001 | @exactly_once is per-VM in-memory only **unless the host injects a persistent EffectStore** | **Resolved 2026-09-21:** `ResearchRuntime` injects `nodus_retry.SqliteEffectStore` (`<workspace>/.effects.sqlite3`) via `NodusRuntime.set_effect_store()`; verified across OS processes and through a crash-mid-effect (pending row → re-executes). Two caveats found: the #328 child resume VM does not inherit the injected store (our runner-direct resume path avoids it), and rehydrated step results come back key-sorted, so `json.stringify(step_result)` is not stable across a restart — `_ext_synthesize` canonicalises the analysis JSON so the draft (part of the key) is byte-stable across processes. |

---

## Confirmed Nodus Behaviors (from runtime probes, v4.0.6)

| Behavior | Confirmed |
|---|---|
| `yield` inside a workflow/goal step | Hard crash: "Task yielded during graph execution". Do not use. |
| `workflow_wait(event, corr_key, payload)` | Suspends workflow; runner marks status "waiting"; step result = nil |
| `resume_workflow(run_id, map)` | Stores map as resume_payload in graph metadata; re-runs pending tasks |
| `workflow_resume_payload()` | Returns the resume payload in any step that runs after a resume; nil on first run |
| `resume_workflow(run_id, "label", map)` | Checkpoint rollback: resets the checkpointed task and all dependents to pending, then resumes with payload |
| Goals have no loop mechanism | GoalDef compiles to the same DAG structure as WorkflowDef; runs once and terminates |
| Completed workflow result | No `"status"` key. Has: `steps`, `state`, `tasks`, `graph_id`, `checkpoints`, `workflow` |
| Waiting workflow result | Has `"status": "waiting"`, `"graph_id"`, `"wait"` |

---

## Open Items Before Implementation

1. ~~**`require_for_effects()` on ApprovalPolicy**~~ — DONE (2026-06-21). Implemented in-repo at `src/policy.py::require_for_effects(manifests, effects)` (the upstream `nodus_approvals.ApprovalPolicy` still ships only `require_for()`). Routes through each manifest's `effects` list to derive the gated tool-name patterns. Follow-up: upstream it as an `ApprovalPolicy.require_for_effects` classmethod.

### Implementation status (2026-06-21)

- Extension manifests: all 5 created under `extensions/*/manifest.json` and made load-bearing via `src/runtime.py::load_tool_manifests()`. NB: `schema` uses the confirmed-working Nodus type names (`"int"`, not the `"integer"` shown above) since the runtime feeds it to tool registration; `returns_schema` stays declarative.
- Approval API: `src/approval_api.py` — `ApprovalService` + stdlib `http.server` adapter (no third-party web framework). The draft `review` step is the human authorization for `publish`'s writes, so the service runtime defaults to an auto-approve effect policy; the effect gate (durable `FileApprovalStore`, `src/approval_store.py`) is exposed under `/gate/*` for stricter, out-of-flow tool approvals.
- Durable cross-process resume: enabled by Nodus v4.0.7 (issue #285 / PR #286); `ResearchRuntime.resume_in_fresh_process()` + `FileApprovalStore`. See `.nodus/learnings.md`.
- Nodus v5 (5.14.0, upgraded 2026-09-20): the v5 `builtin_resume_workflow` diverts a rebuild off any VM with a program loaded onto a child VM that does not inherit `tool_registry`; the host resumes a primed VM through the runner directly (`ResearchRuntime._resume_on_primed_vm`). Workflow store is SQLite, chosen explicitly in `src/runtime.py` (the 6.0 default).

---

## Planned File Structure

```
/
├── CLAUDE.md
├── docs/
│   └── plan.md                  ← this file
├── probes/
│   ├── probe_wait_resume.nd     ← confirmed workflow_wait + resume_workflow
│   └── probe_checkpoint_replay.nd ← confirmed iterative loop via checkpoint replay
├── workflows/
│   └── research_task.nd         ← main workflow (single flat DAG)
├── extensions/
│   ├── web_search/
│   ├── fetch_doc/
│   ├── run_code/
│   ├── write_file/
│   └── notify/
├── src/
│   ├── runtime.py               ← NodusRuntime setup, tool registration
│   ├── approval_api.py          ← approval polling / resume endpoint
│   └── memory.py                ← memory store setup
└── tests/
    └── research_task.test.nd    ← std:test suite
```
