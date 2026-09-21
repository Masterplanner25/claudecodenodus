# Session Handoff Summary

Repo: `C:\dev\claudecodenodus` — Nodus-based autonomous research agent.
Date: 2026-06-21 (updated 2026-09-20). Nodus: `nodus-lang 5.14.0`.

> **2026-09-20 — UPGRADED Nodus 4.0.8 → 5.14.0 (+ all `nodus-*` companions).**
> Suites: **Python 68/68, Nodus 11/11 (+2), `nodus check` OK, `nodus check
> --staged` 4/4 checkable v6 flips OK** (record-equality: no runtime warnings
> fired under `NODUS_STAGED_FLIP_REPORT`). Three things broke and were fixed:
>
> 1. **Reject-with-feedback loop (v5 #482).** v5 *refuses*
>    `resume_workflow(id, "before_draft", payload)` while the run is waiting on
>    `workflow_wait` (`category: waiting_run_checkpoint_resume`). Rejection is
>    now two-phase — `ResearchRuntime._reject_and_replay`: (1) satisfy the wait
>    with `{approved:false, feedback}`; `publish` now **gates every side effect
>    on `workflow_resume_payload()["approved"] == true`** and returns
>    `{"published": false, "revision_requested": true}` otherwise; (2) roll back
>    to `before_draft` with the feedback → draft replays, re-suspends at review.
>    `resume_with_feedback` and `resume_in_fresh_process(checkpoint=)` both use it.
>    Nodus test has a mirrored `reject_draft()` helper + a test pinning the refusal.
> 2. **Cross-process resume lost its tools (v5 #328).** v5's
>    `builtin_resume_workflow` → `_resume_target_vm` diverts the rebuild off any VM
>    with a program loaded onto a child VM that inherits host globals/builtins but
>    **not `tool_registry`**. `tool.call` then failed *soft* (`tool_not_found`),
>    and `publish` reported `published: true` with no file written. Fix:
>    `_resume_on_primed_vm` hands the primed VM to
>    `vm.resolve_workflow_runner().resume_workflow(...)` directly (the primed VM's
>    program has finished; nothing to clobber). Plus `publish` now `throw`s if
>    `type(_wr) == "error"` so a phantom publish can't recur.
> 3. **Workflow store.** Explicitly SQLite (`os.environ.setdefault(
>    "NODUS_WORKFLOW_STORE_BACKEND", "sqlite")` at the top of `src/runtime.py`;
>    v6 default). Local JSON records migrated (`nodus workflow migrate-store`);
>    the old dir is at `.nodus/workflow_framework/runs.json-backup-2026-09-20`
>    (gitignored, all test residue — delete when convenient). **For CLI runs
>    (`nodus test`/`nodus run`) set `NODUS_WORKFLOW_STORE_BACKEND=sqlite` in the
>    shell**, or the CLI writes to the JSON store and re-triggers the warning.
>
> Other v5 notes: `--time-limit` is now **seconds**; `spawn()` accepts a bare
> `fn(){}`; `copy()`, `sleep_until`, `spawn_after`, `std:loop`, `std:runtime.
> capabilities()` exist; `nodus check --staged` previews the 6.0 breaks. Two
> upstream-worthy findings: (a) the "stranded runs" warning counts raw files but
> `migrate-store` lists via `list_runs()`, which drops files older than
> `terminal_max_age_days` → 435 records were "stranded" but unmigratable;
> (b) the #328 child VM should inherit `tool_registry` (or `#482` should document
> the host-side resume path). Rollback: `pip install -r` the pre-upgrade freeze
> (was in the session scratchpad) or `pip install nodus-lang==4.0.8 nodus-extension==0.1.0 nodus-mcp==0.1.0`.

> **2026-07-04 (later) — data plane FULLY REAL, no mocks left.** `research.notify`
> is now real (`src/notify.py::HttpNotifier`): channel dispatch — `console`
> (print), `webhook` (POST `{run_id,message}`, delivered on 2xx), `slack` (POST
> `{"text":…}`, delivered on `200 ok`), `email` (SMTP via `RESEARCH_SMTP_*`, clear
> not-configured result otherwise). All paths **fail soft** (never raise → can't
> abort publish). Injected via `ResearchRuntime(notifier=...)`; default real,
> tests use `ConsoleNotifier`. DAG now sources notify channel/target from injected
> globals (`RESEARCH_NOTIFY_CHANNEL`/`RESEARCH_NOTIFY_TARGET`, default
> `console`/`researcher`) — real console notify out of the box, real webhook/slack
> when configured. Tests: `tests/test_notify.py` (11). **Live-verified end-to-end**:
> full agent with `RESEARCH_NOTIFY_CHANNEL=webhook` → publish POSTed to a loopback
> server, payload received. Suites: **Python 68/68, Nodus 9/9, check OK.**
>
> **2026-07-04 — (c) COMPLETE, all tools live-verified.** LLM online confirmed
> against `claude-opus-4-8` (user's `.env` key) — real cited synthesis, not the
> offline fallback; default model bumped to `claude-opus-4-8`. `run_code`
> live-verified against Docker Desktop 29.2.1: all hardening holds (`--network
> none` blocks egress, uid 65534, read-only rootfs, `/tmp` writable, non-zero
> exit passthrough, in-container `timeout` kills runaway code → exit 124).
> **run_code fix:** container startup on WSL2 is ~39s here, so the code timeout is
> now enforced *inside* the container (`timeout -k 5 <N>`) and the subprocess
> deadline is `<N> + startup_overhead` (90s) — startup no longer eats the code
> budget. Suites: **Python 57/57, Nodus 9/9, check OK.** Only `research.notify`
> remains a stub (out of scope for (c)). Original per-step detail below.
>
> **2026-07-03 update — (c) mostly done (web + run_code + fetch wiring):**
> - **`web_search` + `fetch_doc` real** (`src/web.py`: `HttpWebBackend` +
>   `OfflineWebBackend`). Search provider: Tavily → Brave → keyless **Wikipedia**
>   (DuckDuckGo is bot-blocked now). `fetch_doc` = real GET + stdlib HTML→text
>   (no bs4/lxml), `content_hash = sha256(text)`. `ResearchRuntime(web_backend=...)`.
> - **`run_code` Docker-sandboxed** (`src/sandbox.py::DockerCodeRunner`):
>   `--cap-drop ALL --network none`, read-only rootfs, nobody user,
>   no-new-privileges, memory/pid/cpu limits, code fed over **stdin** (no shell
>   injection). Fails soft when the daemon is down. `ResearchRuntime(code_runner=...)`.
>   Unit-tested (`tests/test_sandbox.py`, 13) but **not live-verified** — Docker
>   Desktop's daemon won't start on this machine (WSL bootstrap error).
> - **`fetch_doc` wired into the DAG**: each gather step now searches AND fetches
>   the top result's full page → `{results, top_doc}`. Helpers `gather_sources` /
>   `fetch_top` in `workflows/research_task.nd` (mirrored in the Nodus test).
> - All handlers fail open. **Suites: Python 56/56, Nodus 9/9, check OK.**
>   Live-verified end-to-end: real fetched Wikipedia text reaches the draft.
> - **Only remaining:** `notify` still a print stub; LLM still **offline** (set
>   `ANTHROPIC_API_KEY` + run synthesis smoke test); live-verify `run_code` once
>   Docker is up. Details below are pre-update unless noted.

---

## Status in one line

Control plane is **complete, tested, green**; data plane (the tools that do real
research) is still **stubbed**. Tests: **Python 31/31, Nodus 9/9, `nodus check` OK.**

Completion: ~90% as a reference architecture / control-plane skeleton; ~45–50%
as an agent that actually researches (everything touching the real world is mocked).

---

## What happened this session

1. **Reviewed project state against the newly-installed Nodus v4.0.7.** Confirmed
   (source + empirical two-OS-process probe) that v4.0.7 ships the rehydrate fix
   (issue #285 / PR #286): `_rebuild_workflow_graph` now rebuilds through the
   normal module-load path and re-binds workflow imports. The cross-run
   import-drop that blocked durable human-in-the-loop is **fixed**.

2. **(a) Durable cross-process resume + persistent approval store.**
   - `src/approval_store.py::FileApprovalStore` — durable, file-backed
     `ApprovalStore` (JSON-per-request under `<workspace>/.approvals`, atomic
     `os.replace` writes). Now the default store in `ResearchRuntime`.
   - `ResearchRuntime.resume_in_fresh_process()` + `_prime_resume_vm()` — resume a
     persisted run on a freshly-primed VM (forces the v4.0.7 rebuild path).
     `_resume_on_vm` keeps the in-process fast path and auto-falls-back to priming.
   - Tests: `tests/test_cross_process.py` (6).

3. **(b) Extensions layer + approval API.**
   - `extensions/*/manifest.json` (5) — created and **load-bearing** via
     `src/runtime.py::load_tool_manifests()`.
   - `src/policy.py::require_for_effects()` — in-repo effect→policy factory
     (replaces `build_effects_policy`).
   - `src/approval_api.py` — `ApprovalService` + stdlib `http.server` adapter
     (zero new deps). Drives the draft review/resume loop; exposes `/gate/*` over
     the durable store. Tests: `tests/test_approval_api.py` (11).

---

## Current repo layout (source)

```
src/
  runtime.py        ← ResearchRuntime, tool registration, resume (in- & cross-process)
  approval_store.py ← FileApprovalStore (durable)
  policy.py         ← require_for_effects()
  approval_api.py   ← ApprovalService + stdlib HTTP adapter
workflows/research_task.nd   ← the fixed DAG (real)
extensions/<tool>/manifest.json  ← 5 manifests (load-bearing)
tests/
  test_runtime.py         (14)  test_cross_process.py (6)  test_approval_api.py (11)
  research_task_test.nd   (9 Nodus)
docs/plan.md      ← design doc (open-items updated)
.nodus/learnings.md  ← Nodus-specific findings (rehydrate fix, resume mechanics)
```

---

## What is NOT done (gap to a real agent)

1. **All tool handlers real** (`src/runtime.py::_dispatch`): `web_search` +
   `fetch_doc` (`src/web.py`), `run_code` hardened Docker sandbox (`src/sandbox.py`),
   `notify` multi-channel delivery (`src/notify.py`) — all live-verified. LLM
   synthesis runs online against a live model. No mock tools remain.
2. **LLM runs offline by default** — real synthesis only with `ANTHROPIC_API_KEY`
   set; otherwise a deterministic string. Never run against a live model here.
3. **`src/memory.py`** — listed in plan, not created (memory is inline via
   `std:memory`; tag-based cross-session recall not wired).
4. **`@exactly_once` is per-VM only** (EXACT-001) — idempotency doesn't survive
   restart; fine for single-session.

The remaining work is integration, not invention: the tool contracts (manifests +
handler signatures) are fixed, so real implementations are drop-in.

---

## Logical next session (if resumed)

**(c) Make the tools real: DONE — data plane fully real.** ~~`web_search` +
`fetch_doc`~~ ✅ (live). ~~`run_code` Docker sandbox~~ ✅ (live-verified). ~~wire
`fetch_doc` into the DAG~~ ✅. ~~flip the LLM online~~ ✅ (live via
`claude-opus-4-8`). ~~make `research.notify` real~~ ✅ (multi-channel, live-verified
end-to-end). No mock tools remain. **Residual non-tool gaps (optional):** EXACT-001
(`@exactly_once` per-VM only, not distributed-durable) and the unbuilt tag-based
cross-session recall (`src/memory.py`; memory is inline via `std:memory`).

Follow-up worth upstreaming: `ApprovalPolicy.require_for_effects` as a real
classmethod in `nodus_approvals` (currently in-repo at `src/policy.py`).

---

## Key constraint to remember

v4.0.7 rehydrates the **DAG + imports** from source on cross-process resume, but
**host-injected globals are not reconstructed**: the rehydrating process must
re-supply tools, the LLM client, effect handlers, and a **durable** approval store
(`InMemoryApprovalStore` is per-process). `ResearchRuntime.__init__` does this;
don't regress it.

---

## How to run

```bash
PYTHONPATH=. venv/Scripts/python -m pytest tests/ -q   # 68 Python tests (SQLite store chosen in src/runtime.py)
NODUS_WORKFLOW_STORE_BACKEND=sqlite venv/Scripts/nodus test tests/   # 11 Nodus tests
venv/Scripts/nodus check workflows/research_task.nd     # syntax
# Approval API (manual): python -m src.approval_api  → http://127.0.0.1:8000
```
