# Nodus Learnings — claudecodenodus

## 2026-06-21 — tool.register schema type names

In `tool.register({..., schema: {...}})`, use Nodus type names: `"string"`, `"int"`, `"float"`, `"bool"`, `"map"`, `"list"`, `"nil"`, `"any"`. Using `"integer"` silently fails registration (tool.register returns an error Record that gets discarded if unchecked). Bad registration causes `tool.call` to return an error Record which is not JSON-safe, crashing any subsequent `mem.put` call.

## 2026-06-21 — nodus test discovery

The `nodus test <dir>` command only discovers files matching `*_test.nd`. Files named `*.test.nd` are silently ignored. Use the `nodus test` command (not `nodus run`) to run test suites — `nodus run` on a test file exits silently with code 0 and no output.

## 2026-06-21 — workflow_wait, yield, goals, checkpoint replay

**yield inside workflow/goal steps crashes.** The YIELD opcode fires "Task yielded during graph execution" at runtime. Do not use `yield` in any workflow or goal step body. `yield` is coroutine-only.

**Suspension primitive is `workflow_wait()`.** Call `return workflow_wait(event_type, corr_key, payload_map)` from a step. The runner persists state and marks the run "waiting". Subsequent tasks are not yet run.

**Resume payload via `workflow_resume_payload()`.** After `resume_workflow(run_id, payload_map)`, call the 0-arg builtin `workflow_resume_payload()` in any subsequent step. It returns the payload map. Returns nil on first run (before any resume). Available in all steps that run after a resume, not just the one immediately after the wait.

**`resume_workflow` call forms:**
- `resume_workflow(id, map)` — 2 args: second arg is the payload (no checkpoint)
- `resume_workflow(id, "label", map)` — 3 args: rolls back to named checkpoint, then resumes with payload

**Checkpoint rollback resets the checkpointed task and all its dependents to pending.** Workflow state reverts to the snapshot at the checkpoint. The re-run proceeds from that task.

**Goals have no loop mechanism.** `goal` compiles to the exact same DAG structure as `workflow`. Steps run once. There is no `success:` condition, no built-in iteration. Use checkpoint replay for iterative loops.

**Completed workflow result has no `"status"` key.** It has: `steps`, `state`, `tasks`, `graph_id`, `checkpoints`, `workflow`. Waiting result has `"status": "waiting"`. Indexing `result["status"]` on a completed run is a KeyError.

## 2026-06-21 — cross-run resume dropped module imports (FIXED in v4.0.7)

**History (≤ v4.0.6):** each `run_source()` built a fresh VM; resuming a waiting workflow in a *separate* call triggered `VM._rebuild_workflow_graph`, which recompiled via `ModuleLoader.compile_only()` — bytecode only, no `_resolve_import_bindings`. The rebuilt VM never bound import aliases (`tool`/`mem`/`json`), so any post-resume step using them failed with `Undefined variable: tool` (surfaced only in `spawned_errors`; `run_source` still reported `ok: True`, so cross-process resume silently no-op'd). We worked around it in-process by reusing the start VM from `get_registered_vm(run_id)`.

**Fixed upstream in v4.0.7 (issue #285, PR #286).** `_rebuild_workflow_graph` (now `nodus/vm/vm.py`) rebuilds through `ModuleLoader(project_root=None, vm=self, host_globals=...).load_module_from_source(...)` — the normal load path with the workflow VM as target — so named/aliased imports re-bind into `module_globals` and bare imports (`import "std:json"`) re-populate `_bare_import_hints`, exactly as on first run. Verified in the installed package and by a genuine two-OS-process resume that completed `publish` and wrote output.

**Cross-process resume now works.** Driver in `src/runtime.py`:
- `_prime_resume_vm()` runs the workflow *declarations* (`workflow_source_code + "\n_prime_vm()\n"`, no `run_workflow` → nothing starts) through `run_source` to get a VM with imports + the tool registry bound.
- Call `vm.builtin_resume_workflow(run_id, checkpoint_or_None, payload)` on it → returns the rich result map (`steps`/`state`/...). The runner rebuilds whenever the passed VM differs from the run's registered start VM: `if graph is None or (registered_vm is not None and registered_vm is not vm): graph = rebuild_graph(...)`. So a freshly-primed VM (≠ start VM) always exercises the rebuild path — true even in one process, which is how the tests force it.
- `_resume_on_vm` keeps the in-process fast path (`get_registered_vm`) and falls back to priming when no VM is registered. `resume_in_fresh_process()` always primes (explicit durable path).
- NB: the *source-level* `resume_workflow(...)` builtin returns nil to `_capture`; call `builtin_resume_workflow` on the VM directly to get the result map.

**Residual host-layer constraint (by design):** the rebuild reconstructs the DAG + imports from source, but NOT host-injected (non-import) globals. The rehydrating process must re-supply tools, the LLM client, and effect handlers (`ResearchRuntime.__init__` does this), and approval-gate state must live in a durable store — `InMemoryApprovalStore` is per-process. We added `src/approval_store.py::FileApprovalStore` (JSON-per-request under `<workspace>/.approvals`, atomic `os.replace` writes) as the default store so an approval recorded in one process is visible to a resume in another.

## 2026-06-21 — LLM-as-tool wiring (nodus_llm)

LLM reasoning is exposed to the workflow as a Nodus tool, not a language primitive — keeps the DAG fixed. `nodus_llm.FailoverClient(store, provider_fn)` requires a `provider_fn(profile) -> client`; there is no default. Map `profile.provider` to `nodus_llm.providers.anthropic.AnthropicProvider` / `.openai.OpenAIProvider` / `.compat.OpenAICompatProvider` (lazy import — they import their SDK in `__init__`). `FailoverClient.chat(messages, model=None, temperature=0.7, max_tokens=None) -> str`; Anthropic provider pulls `role:"system"` messages out into the `system` kwarg automatically.

In `src/runtime.py` the `research.synthesize` tool (effect `llm.complete`, ungated) wraps the client and **falls back to a deterministic offline draft when no client is configured** (no `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`) — keeps tests hermetic. `ResearchRuntime(..., llm_client=...)` accepts an injected fake for tests; default sentinel `_AUTO_LLM` builds from env. The Nodus step passes structured `analyze` output to the tool as a string via `json.stringify(analyze)`, and reads the result with the Record→map round-trip `json.parse(json.stringify(tool.call(...)))["draft"]`.

## 2026-09-20 — Nodus 5.14.0 upgrade (from 4.0.8)

**Checkpoint rollback on a WAITING run is refused (v5, #482).** `resume_workflow(id, "label", map)` on a run parked at `workflow_wait` returns `{"ok": false, "category": "waiting_run_checkpoint_resume", ...}` instead of replaying. The v4 reject-and-revise one-liner no longer works. Two-phase replacement: `resume_workflow(id, {"approved": false, "feedback": ...})` to satisfy the wait (every post-wait step must gate its side effects on the payload and no-op), then `resume_workflow(id, "label", {"feedback": ...})` — now allowed because the run is no longer waiting. Probe: `probes/probe_checkpoint_replay.nd`. Host side: `ResearchRuntime._reject_and_replay`.

**Cross-process resume via `vm.builtin_resume_workflow` runs on a child VM with an EMPTY `tool_registry` (v5, #328).** Any VM that has a program loaded (i.e. ran the workflow declarations through `run_source`) is diverted to a child that inherits host globals and builtins but not tools. `tool.call` in a resumed step then returns an error VALUE — `type(r) == "error"`, never a throw — and the run reports `ok`. Fix: hand the primed VM to the runner directly, `vm.resolve_workflow_runner().resume_workflow(vm, id, checkpoint, resume_payload=..., rebuild_graph=vm._rebuild_workflow_graph)`. Always check `type(r) == "error"` after `tool.call` in steps with side effects and `throw`. Repro: `probes/repro_v5_child_vm_tool_registry.py`. Reported in `docs/upstream-handoff.md`.

**`--time-limit` is now SECONDS** (was milliseconds in 4.x). `nodus run --time-limit 5 x.nd`.

**Workflow store: choose it explicitly.** 5.x defaults to the JSON store but 6.0 flips to SQLite and strands unmigrated runs. `NODUS_WORKFLOW_STORE_BACKEND=sqlite` (this host sets it in `src/runtime.py`; CLI runs need it in the shell). `nodus workflow migrate-store --to sqlite` is non-destructive but age-filters (30 days) via `list_runs()`, while the "stranded runs" warning counts raw files — old runs stay "stranded" forever.

**`nodus check --staged file.nd`** previews the 6.0 breaking flips; run the suites with `NODUS_STAGED_FLIP_REPORT=<path>` to catch the runtime-only one (record equality).

**Companion pins.** `nodus-extension` / `nodus-mcp` 0.1.0 pin `nodus-lang<5`; upgrade them (0.1.2 / 0.1.4) alongside or `pip check` fails.

## 2026-09-21 — @exactly_once durability (EXACT-001 resolved)

**`@exactly_once` is per-VM unless the host injects a store.** `nodus-retry` 0.2.0 ships `SqliteEffectStore(path)`; call `runtime.set_effect_store(store)` BEFORE the first `run_source` and every VM the runtime builds carries it. Verified: a second OS process on the same file gets the cached result with zero executions; a `pending` row with no `complete` (crash mid-effect) re-executes; without a store, each VM starts empty.

**Key = fn name + args map (`effect_action_id(name, {params}, "default")`).** Cached return is stored as `{"result": v}` via `json.dumps` — return maps, not records. Works on a top-level fn called from a workflow step. A `throw` inside skips `effect_complete`, so failures retry.

**The #328 child resume VM drops the injected `effect_store` too** (fresh `InMemoryEffectStore`), alongside `tool_registry`. Resume a primed VM through the runner directly.

**Rehydrated step results are key-sorted.** The run store persists with `sort_keys=True`; live maps keep insertion order; `std:json.stringify` has no sort option. `json.stringify(prior_step_result)` therefore differs after a restart for identical content — canonicalise on the host before using it as an identity. (Bit us: an exactly-once publish keyed on the draft text re-fired on a rehydrated replay.)

**Shared VM = shared effect store in `nodus test`.** All cases in one `*_test.nd` share the per-VM store, so an `@exactly_once` fn called with identical args in two cases is cached across them. Use case-unique inputs.
