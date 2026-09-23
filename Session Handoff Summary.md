# Session Handoff Summary

Repo: `C:\dev\claudecodenodus` — a Nodus-based autonomous research agent, built as
a testbed for the question *can Claude Code work effectively in Nodus?* (see
`README.md`). Started 2026-06-21, current as of **2026-09-23**.
Nodus: **`nodus-lang 5.14.0`**.

> Rewritten 2026-09-23. Earlier revisions of this file stacked dated blocks on top
> of text that had stopped being true; the history now lives in the **Update log**
> at the bottom and everything above it describes the repo as it stands.

---

## Status

**Every item in the original plan is implemented, live-verified and green.**

| | |
|---|---|
| Suites | **Python 88/88**, **Nodus 15/15**, `nodus check` OK, `nodus check --staged` clean for the 6.0 flips |
| Control plane | Complete — fixed DAG, human-in-the-loop wait, durable cross-process resume, effect-gated approval, HTTP approval API |
| Data plane | Complete — all five tools real and live-verified; no mocks remain |
| Durability | Approvals, `@exactly_once` effect records, memory and workflow runs all persist per workspace |
| Remaining | Upstream reporting (`docs/upstream-handoff.md`). Nothing is blocked. |

The agent researches a question across three domains, drafts with an LLM, suspends
for human review, replays the draft on rejection with the reviewer's feedback, and
on approval publishes exactly once — remembering what it learned for later sessions.

---

## Architecture

```
┌─ Python host (src/) ───────────────────────────────────────────────┐
│  ResearchRuntime   tools, LLM client, sandbox, notifier, resume     │
│  FileApprovalStore   .approvals/        (durable, JSON per request) │
│  SqliteEffectStore   .effects.sqlite3   (@exactly_once idempotency) │
│  SqliteMemoryStore   .memory.sqlite3    (values + tag index)        │
│  ApprovalService   stdlib http.server, no deps                      │
├─ Nodus (workflows/research_task.nd) ───────────────────────────────┤
│  init ─┬─ recall ──────────┐                                        │
│        ├─ gather_web ──────┤                                        │
│        ├─ gather_code ─────┼─► analyze ─► draft_step ─► review ─►   │
│        └─ gather_data ─────┘       ▲ checkpoint  │ workflow_wait    │
│                                    └─ reject + feedback ◄──┘        │
│                                                  └─► publish        │
├─ Extensions (extensions/*/manifest.json) ──────────────────────────┤
│  6 manifests + in-process research.synthesize; gated by declared    │
│  effect (fs.write, network.write), not by tool name                 │
└─────────────────────────────────────────────────────────────────────┘
```

Nodus decides *what happens next*; Python decides *whether it is allowed* and
*does it*. The LLM reasons inside steps and never chooses control flow.

---

## Repo layout

```
workflows/research_task.nd    the DAG (the Nodus test keeps its OWN inline copy —
                              tests/research_task_test.nd — and must be hand-synced)
src/
  runtime.py        ResearchRuntime: tool registration, start/resume, two-phase
                    reject, cross-process resume, all _ext_* handlers
  memory.py         SqliteMemoryStore (durable MemoryStore + tag index), topic_tags()
  approval_store.py FileApprovalStore (durable)
  policy.py         require_for_effects()  ← candidate for upstreaming
  approval_api.py   ApprovalService + stdlib HTTP adapter
  web.py            HttpWebBackend / OfflineWebBackend
  sandbox.py        DockerCodeRunner / DisabledCodeRunner
  notify.py         HttpNotifier / ConsoleNotifier
extensions/<tool>/manifest.json   6: web_search, fetch_doc, run_code, write_file,
                                  notify, memory_recall (load-bearing: they drive
                                  registration and gating)
tests/  test_runtime.py (14)   test_memory.py (17)      test_web.py (13)
        test_sandbox.py (13)   test_approval_api.py (11) test_notify.py (11)
        test_cross_process.py (6)  test_exactly_once.py (3)
        research_task_test.nd (15 Nodus cases)
probes/ probe_checkpoint_replay.nd  probe_wait_resume.nd  probe_fetch_wiring.nd
        probe_xsession_recall.py            cross-session recall, two real processes
        repro_v5_child_vm_tool_registry.py  upstream bug repro
docs/   plan.md  upstream-handoff.md
.nodus/learnings.md   running log of Nodus behaviour pinned down here (tracked;
                      the rest of .nodus/ is runtime state and is ignored)
```

---

## How to run

```bash
# syntax + the changes staged for the Nodus 6.0 major
venv/Scripts/nodus check workflows/research_task.nd
venv/Scripts/nodus check --staged workflows/research_task.nd

# suites — both hermetic: no network, no Docker, no API key needed
PYTHONPATH=. venv/Scripts/python -m pytest tests/ -q                 # 88
NODUS_WORKFLOW_STORE_BACKEND=sqlite venv/Scripts/nodus test tests/   # 15

# the agent, by hand
PYTHONPATH=. venv/Scripts/python -m src.approval_api   # http://127.0.0.1:8000
#   POST /research {"question":…, "session_id":…}   → draft, status "waiting"
#   GET  /approvals            POST /approvals/{run_id}/approve
#   POST /approvals/{run_id}/reject {"feedback": …}  → revised draft, waiting again

# cross-session recall across two real OS processes
PYTHONPATH=. venv/Scripts/python probes/probe_xsession_recall.py <workspace> a
PYTHONPATH=. venv/Scripts/python probes/probe_xsession_recall.py <workspace> b
```

`src/runtime.py` selects the SQLite workflow store itself; **CLI runs do not go
through it**, so set `NODUS_WORKFLOW_STORE_BACKEND=sqlite` in the shell for
`nodus test` / `nodus run` or they write to the JSON store and warn.

Optional env for the real data plane: `ANTHROPIC_API_KEY` (live synthesis; default
model `claude-opus-4-8`, override `RESEARCH_LLM_MODEL`), `OPENAI_API_KEY`,
`TAVILY_API_KEY` / `BRAVE_API_KEY` (else keyless Wikipedia),
`RESEARCH_NOTIFY_CHANNEL` / `RESEARCH_NOTIFY_TARGET`, `RESEARCH_SMTP_*`.
`.env` is gitignored.

---

## Constraints to remember — don't regress these

1. **Host-injected state is not reconstructed on resume.** Nodus rehydrates the DAG
   and its `import`s from persisted source, but the rehydrating process must
   re-supply tools, the LLM client, effect handlers, and **durable** stores
   (approval, effect, memory). `ResearchRuntime.__init__` does all of this.
2. **Resume on a primed VM, through the runner.** `vm.builtin_resume_workflow`
   diverts a rebuild onto a child VM that inherits neither `tool_registry` nor the
   injected `effect_store` (v5 #328), and `tool.call` then fails *soft* — a publish
   reports success having written nothing. `_resume_on_primed_vm` calls
   `vm.resolve_workflow_runner().resume_workflow(...)` directly instead.
3. **Rejection is two-phase** (v5 #482): satisfy the wait with `approved: false`
   (every post-wait step gates its side effects on that payload and no-ops), *then*
   roll back to `before_draft` with the feedback. A checkpoint rollback while the
   run is still waiting is refused.
4. **Anything identity-bearing must be canonicalised.** Step results rehydrated from
   the run store come back key-sorted while live maps keep insertion order, and
   `std:json.stringify` has no canonical mode — so a string derived from a prior
   step differs after a restart. `_canonical_json` handles the analysis and prior
   blocks, which feed the draft, which is the `@exactly_once` publish key.
5. **`tests/research_task_test.nd` holds its own copy of the workflow.** Any DAG
   change must be mirrored there by hand.
6. **All `_init_*` globals must be supplied in both paths** — `start()` and
   `_prime_resume_vm()` — or module load fails.

---

## What's left

**Upstream reporting** — five items with verified repros in `docs/upstream-handoff.md`:
the child-VM `tool_registry`/`effect_store` gap, the stranded-runs vs `migrate-store`
mismatch, the #482 reject-and-revise recipe, `ApprovalPolicy.require_for_effects`
(PR-ready; still absent from `nodus-approvals` 0.1.0), and the key-order/canonical-JSON
issue. None of these block the repo — workarounds ship for all of them.

**Optional directions**, none required by the plan: swap `topic_tags()` for
LLM-extracted canonical topics; expose memory recall through the HTTP API; take the
6.0 upgrade when it lands (`nodus check --staged` is clean today).

---

## Update log

Newest first. Each entry is what changed and, where it matters, why.

### 2026-09-22 — `src/memory.py`: durable tag-indexed memory + cross-session recall
Python 88/88 (+17), Nodus 15/15 (+3). The last open item from the original plan.

- `SqliteMemoryStore` **subclasses** Nodus's `MemoryStore` rather than reimplementing
  it: `memory_runtime.recall_from`/`recall_all` read `store._values` directly, so
  that dict stays authoritative and SQLite is a write-through mirror, reloaded on
  open. Injected via `NodusRuntime(memory_store=...)` — which also sidesteps VM-001
  (the default store is a process-global in-memory singleton).
- **Tagging rides on the stdlib**: `mem.tag(k, tags)` is just
  `put("__nodus_tags__:<k>", tags)`, so the store recognises that prefix and indexes
  it. No new builtin or tool was needed to make `.nd` tagging searchable.
- `topic_tags(question)` derives `topic:` tags deterministically (lowercase, drop
  stopwords and sub-3-char words, cap 8) and is injected as `_init_topic_tags` so a
  rehydrated resume derives identical tags. LLM extraction is a drop-in replacement
  for that one function.
- DAG: new `recall` step between `init` and `analyze` (`draft_step after analyze,
  recall`); `init`, the gathers, `draft_step` and `publish` tag their nodes per the
  plan's schema; gathers store their fetched doc at
  `research/{session}/sources/{content_hash}` with `domain:` tags. New tool
  `research.memory_recall` (effect `memory.read`, ungated); `_ext_synthesize` takes
  `prior`.
- **`require` matters**: recall matches *any* topic tag but *requires* `status:final`.
  Without it a session recalls its own meta node and half-written draft as "prior
  research" — caught by the two-process probe, not by a unit test.
- Live-verified across two real OS processes: `probes/probe_xsession_recall.py`.
- Nodus gotchas: no `concat()` (use `+`), `contains()` is in `std:strings`,
  `std:test` has no `assert_true`, and **a step only sees steps named in its own
  `after`** — transitive dependencies are not in scope.

### 2026-09-21 — EXACT-001 resolved: `@exactly_once` durable across processes
Python 71/71 (+3), Nodus 12/12 (+1).

- `nodus-retry` 0.2.0 ships `SqliteEffectStore`; injected via
  `NodusRuntime.set_effect_store()` **before the first `run_source`** so both
  `start` and the primed resume VM carry it. Injectable, closed on `shutdown()`.
- Publish's external effects moved into `@exactly_once fn publish_once(session_id,
  draft, question, channel, target)`. Identical replayed draft → served from the
  store (no rewrite, no re-notify); revised draft → new key. A `throw` inside leaves
  the record pending, so failures retry rather than cache.
- **Finding, fixed here**: rehydrated step results are key-sorted, so
  `json.stringify(analyze)` produced a different draft after a restart for identical
  findings and the idempotency key missed → `_canonical_json`.
- **Finding, upstream**: the #328 child resume VM also drops the injected
  `effect_store`.
- The plan's "content_hash as `@exactly_once` action_id" conflated a cache key with
  an idempotency key; `plan.md` revised.

### 2026-09-20 — upgraded Nodus 4.0.8 → 5.14.0 (+ all `nodus-*` companions)
Python 68/68, Nodus 11/11 (+2), staged-6.0 checks clean. Three breaks:

1. **Reject-with-feedback** — v5 refuses a checkpoint rollback on a waiting run
   (#482). Now two-phase via `_reject_and_replay`; `publish` gates every side effect
   on the approval payload.
2. **Cross-process resume lost its tools** (#328) — child VM without
   `tool_registry`; `tool.call` failed soft and `publish` reported success with no
   file written. Fixed with `_resume_on_primed_vm`; `publish` now `throw`s on a tool
   error value.
3. **Workflow store** — explicitly SQLite (the 6.0 default), JSON records migrated.

Other v5 notes: `--time-limit` is now **seconds**; `spawn()` accepts a bare `fn(){}`;
`copy()`, `sleep_until`, `spawn_after`, `std:loop`, `std:runtime.capabilities()`
exist; `nodus check --staged` previews the 6.0 breaks. Rollback if ever needed:
`pip install nodus-lang==4.0.8 nodus-extension==0.1.0 nodus-mcp==0.1.0` and revert
commit `b19cab1`.

### 2026-07-04 — data plane fully real, no mocks left
- `research.notify` real (`src/notify.py`): console / webhook / Slack / email, every
  path **fails soft** so a bad notify can't abort publish. Channel and target come
  from injected globals. Live-verified end-to-end against a loopback server.
- LLM online, verified against `claude-opus-4-8` — real cited synthesis, not the
  offline fallback. Offline deterministic fallback still applies with no key, which
  is what keeps the suites hermetic.
- `run_code` live-verified against Docker Desktop 29.2.1: egress blocked, uid 65534,
  read-only rootfs, `/tmp` writable, non-zero exit passthrough, runaway code killed.
  **Container startup on WSL2 is ~39s here**, so the code timeout is enforced
  *inside* the container (`timeout -k 5 <N>`) and the subprocess deadline is
  `<N> + startup_overhead` — don't fold startup back into the code budget.

### 2026-07-03 — real web tools, Docker sandbox, fetch wired into the DAG
- `src/web.py`: `HttpWebBackend` + `OfflineWebBackend`. Search provider auto-selects
  Tavily → Brave → keyless **Wikipedia** (DuckDuckGo is bot-blocked now). `fetch_doc`
  is a real GET plus stdlib HTML→text, no bs4/lxml; `content_hash = sha256(text)`.
- `src/sandbox.py::DockerCodeRunner`: `--cap-drop ALL --network none`, read-only
  rootfs, nobody user, no-new-privileges, memory/pid/cpu limits, code over **stdin**
  (no shell-injection surface). Fails soft when the daemon is down.
- Each gather step now searches *and* fetches the top result's full page
  (`gather_sources` / `fetch_top`), so the draft is built from real page text.

### 2026-06-21 — durable resume, extensions layer, approval API
- Confirmed Nodus 4.0.7 ships the rehydrate fix (#285/#286): cross-run import-drop
  gone, verified with a genuine two-OS-process resume.
- `FileApprovalStore` (durable, atomic writes) became the default store;
  `resume_in_fresh_process()` + `_prime_resume_vm()` added.
- `extensions/*/manifest.json` made load-bearing via `load_tool_manifests()`;
  `require_for_effects()` maps declared effects → gated tools;
  `src/approval_api.py` drives the review/resume loop over stdlib `http.server`.
