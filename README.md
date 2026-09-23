# Nodus Claude Code Lab

A repo that tests a simple question:

**Can Claude Code work effectively in Nodus?**

This is not primarily a product repo. It is an evaluation repo for the Nodus
language, runtime, workflow model, and the Claude-Code-facing developer
experience. It is the Claude Code counterpart of
[`codexnodus`](../codexnodus) (the same question, asked of Codex); the two repos
build different vertical slices so the findings cover more of the ecosystem.

The vertical slice here is an **autonomous research agent**, where:

- Nodus owns workflow sequencing, the fixed research DAG, checkpoint/replay,
  the human-in-the-loop wait, memory intent, and result contracts
- the Python host owns tool execution, the LLM client, sandboxing, persistence
  of approvals, the HTTP approval API, and the hard security boundaries

## Why This Repo Exists

The purpose is to exercise Nodus under realistic agent-style work instead of
toy scripts, and to answer questions like:

- Can Claude Code reliably read, write, debug and *upgrade* `.nd` workflows?
- Are the language rules (records vs maps, closures, integer suffixes,
  single-line expressions) clear enough for iterative agent development?
- Can durable pause/resume, approval gates, memory and auditable side effects be
  expressed cleanly with Nodus as the execution layer and Python as the host?
- What breaks across a **major version upgrade** (4.0.8 → 5.14.0 was done here),
  and does the tooling (`nodus check --staged`, migration commands) catch it?
- Where are the rough edges in the runtime, packaging, skills and docs?

When this repo exposes friction — parser traps, runtime gaps, silent behaviour
changes, packaging mismatches — those findings are written up to flow back into
the main Nodus repo. See [Findings](#findings-that-flow-upstream).

## Current Slice

The implemented slice is `research_task` (`workflows/research_task.nd`): a
fixed, auditable DAG — `gather_* → analyze → draft → review → publish` — with a
human approve/reject loop around the draft.

It supports:

- **fixed-DAG orchestration** — the LLM reasons *inside* steps; it never
  chooses the control flow
- **approval-driven pause and resume** — `review` suspends on
  `workflow_wait`; approval publishes, rejection replays the draft from a
  checkpoint with the reviewer's feedback
- **durable cross-process resume** — a run started in one process is
  approved and completed in another, with the DAG rehydrated from persisted
  source and the host re-supplying tools
- **effect-gated approval** — tools are gated by the *effects* their manifests
  declare (`fs.write`, `network.write`), not by name
- **exactly-once publish** — the file write and notification are wrapped in an
  `@exactly_once` function backed by a durable SQLite effect store, so a retry
  after a crash or a replay of an identical draft never fires them twice
- **cross-session memory** — findings are tagged (`session:`/`topic:`/`domain:`/
  `status:`) in a durable, tag-indexed store; a later session on a related
  question recalls earlier *published* findings and drafts with them in view
- **a real data plane** — every tool touches the real world:
  `web_search` (Tavily → Brave → keyless Wikipedia), `fetch_doc` (HTTP +
  HTML→text), `run_code` (hardened Docker sandbox), `synthesize` (Claude via
  `nodus_llm`, deterministic offline fallback), `notify`
  (console / webhook / Slack / email). No mocks remain.
- **a stdlib-only HTTP approval API** — start research, list pending drafts,
  approve/reject, plus an out-of-flow `/gate/*` layer over the durable store

Design and the decisions behind it are in `docs/plan.md`; the running status
and history are in `Session Handoff Summary.md`.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│ Python host  (src/)                                          │
│   ResearchRuntime  ── tools, LLM client, sandbox, notifier   │
│   FileApprovalStore ── durable approvals (JSON per request)  │
│   ApprovalService   ── stdlib HTTP API                       │
├──────────────────────────────────────────────────────────────┤
│ Nodus  (workflows/research_task.nd)                          │
│   gather_web ─┐                                              │
│   gather_code ├─► analyze ─► draft ─► review ─► publish      │
│   gather_data ┘              ▲ checkpoint   │ workflow_wait  │
│                              └── reject+feedback ◄──┘        │
├──────────────────────────────────────────────────────────────┤
│ Extensions  (extensions/*/manifest.json → src/{web,sandbox,  │
│   notify}.py)   effects declared per tool; gated by effect   │
└──────────────────────────────────────────────────────────────┘
```

The hard boundary: Nodus decides *what happens next*; Python decides *whether
it is allowed to* and *does it*. Host-injected globals (tools, LLM client,
approval store) are **not** reconstructed from workflow source on resume — the
rehydrating process must re-supply them, and `ResearchRuntime.__init__` does.

## Repo Layout

- `workflows/research_task.nd` — the research DAG
- `src/`
  - `runtime.py` — `ResearchRuntime`: NodusRuntime lifecycle, tool
    registration, start / resume / two-phase reject, cross-process resume
  - `web.py`, `sandbox.py`, `notify.py` — real tool implementations
    (each with an offline / disabled variant for hermetic tests)
  - `memory.py` — `SqliteMemoryStore`: durable Nodus `MemoryStore` + tag index,
    cross-session recall, and the deterministic `topic_tags()` heuristic
  - `approval_store.py` — durable `FileApprovalStore`
  - `policy.py` — `require_for_effects()` (candidate for upstreaming)
  - `approval_api.py` — `ApprovalService` + stdlib `http.server` adapter
- `extensions/<tool>/manifest.json` — tool contracts; load-bearing for
  registration and gating
- `tests/` — Python integration suites (`pytest`) and the Nodus suite
  (`research_task_test.nd`, which carries its own inline copy of the workflow)
- `probes/` — small `.nd` / `.py` scripts that pin down runtime behaviour;
  `repro_v5_child_vm_tool_registry.py` is an upstream bug repro and
  `probe_xsession_recall.py` drives cross-session recall across two real processes
- `docs/plan.md` — design; `docs/upstream-handoff.md` — findings to report
- `.nodus/learnings.md` — running log of Nodus behaviour pinned down during
  development (the rest of `.nodus/` is runtime state and is ignored)
- `CLAUDE.md` — repo-local Nodus rules for Claude Code (the traps, in one page)
- `Session Handoff Summary.md` — current status, constraints not to regress,
  and a dated update log

## Local Setup

Requires Python 3.10+ (developed on 3.11) and Nodus **5.14.0** or later.

```powershell
python -m venv venv
.\venv\Scripts\python -m pip install -U pip
.\venv\Scripts\python -m pip install -r requirements.txt
```

`nodus-lang` ships the CLI. If `nodus.exe` is blocked by application control
on your machine, `python -m nodus ...` is equivalent.

Optional, for the real data plane:

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | live LLM synthesis (default model `claude-opus-4-8`; `RESEARCH_LLM_MODEL` overrides). Unset → deterministic offline draft |
| `OPENAI_API_KEY` | failover LLM (`RESEARCH_OPENAI_MODEL`) |
| `TAVILY_API_KEY` / `BRAVE_API_KEY` | search providers; unset → keyless Wikipedia |
| `RESEARCH_NOTIFY_CHANNEL` / `RESEARCH_NOTIFY_TARGET` | `console` (default), `webhook`, `slack`, `email` |
| `RESEARCH_SMTP_*` | `HOST`, `PORT`, `USER`, `PASS`, `FROM` for the email channel |
| `NODUS_WORKFLOW_STORE_BACKEND` | set to `sqlite` for CLI runs (`nodus test` / `nodus run`); the Python host sets it itself |

`run_code` needs a Docker daemon; without one it fails soft and the workflow
continues. `.env` is gitignored — load it into your shell however you prefer.

## Verification

```powershell
# workflow syntax + the changes staged for the Nodus 6.0 major
.\venv\Scripts\nodus check workflows\research_task.nd
.\venv\Scripts\nodus check --staged workflows\research_task.nd

# Nodus suite (15 tests)
$env:NODUS_WORKFLOW_STORE_BACKEND='sqlite'
.\venv\Scripts\nodus test tests\

# Python suites (88 tests; hermetic — no network, no Docker, no API key needed)
$env:PYTHONPATH='.'
.\venv\Scripts\python -m pytest tests\ -q
```

As of 2026-09-22, on `nodus-lang 5.14.0`, the repo passes: workflow check,
staged-6.0 check, `15/15` Nodus tests, `88/88` Python tests.

To drive the agent by hand:

```powershell
$env:PYTHONPATH='.'
.\venv\Scripts\python -m src.approval_api     # http://127.0.0.1:8000
# POST /research {"question": ..., "session_id": ...}  → draft, status "waiting"
# GET  /approvals            POST /approvals/{run_id}/approve
# POST /approvals/{run_id}/reject {"feedback": ...}   → revised draft, "waiting" again
```

## Findings That Flow Upstream

The point of the repo. Notable so far:

- **Cross-process resume dropped workflow imports** (Nodus 4.0.x) — the
  rehydrated VM lacked `tool`/`mem`/`json`. Fixed upstream in 4.0.7 (#285 /
  #286); this repo verified the fix with a genuine two-OS-process resume.
- **Nodus 5 refuses a checkpoint rollback on a waiting run** (#482) — the
  documented 4.x reject-and-revise pattern silently stopped working; the
  two-phase replacement is implemented here and proposed for the docs.
- **Nodus 5 resumes on a child VM without `tool_registry`** (#328) — a resumed
  step's `tool.call` fails soft and the run reports success. Repro in
  `probes/`, workaround in `src/runtime.py`.
- **Store migration and the stranded-runs warning disagree** — one age-filters,
  the other doesn't, so old JSON runs can never be migrated and the warning
  becomes a hard error at 6.0.
- **Rehydrated step results are key-sorted** and `json.stringify` has no canonical
  mode, so strings derived from a prior step differ after a restart — found when
  an exactly-once key built from the draft re-fired on a replay.
- `ApprovalPolicy.require_for_effects` — effect-based gating, PR-ready.

All of these, with file:line evidence and repros, are in
`docs/upstream-handoff.md`. The language-level traps that bit during
development (map vs record access, closure shadowing, float-by-default
numbers, single-line expressions, the v5 resume and `tool.call` fail-soft
semantics) are distilled into one page in `CLAUDE.md`. Earlier evaluation
findings — `@retry` being a no-op, `@exactly_once` being per-VM only unless a
store is injected (now done), type
annotations unenforced — are recorded in `docs/plan.md`.

## What Success Looks Like

This repo is successful if it makes the real problems visible and gets them
fixed upstream:

- language ergonomics and workflow-authoring traps
- resume / durability edge cases, especially across processes and versions
- silent behaviour changes across major versions
- packaging and companion-package pin mismatches
- missing docs, skill guidance, or error messages that don't say what to do

If it produces a solid testbed and a steady feedback loop into the Nodus
project, it is doing its job — independent of whether the research agent
itself is ever "finished".

## Naming

`Nodus Claude Code Lab` is the working name. The repo is closer to:

- a Nodus compatibility and workflow testbed for Claude Code
- an agent-workflow proving ground for Nodus
- a feedback loop from real Claude Code use back into the Nodus ecosystem

The purpose stays the same even as the slice evolves.
