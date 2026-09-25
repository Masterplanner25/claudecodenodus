# Upstream handoff — findings to report to Nodus

> **All five are resolved upstream (2026-09-23/24). Nothing here is open.**
> Verified against `Masterplanner25/Nodus` `main` and, for item 4, against
> nodus-approvals 0.2.0 on PyPI. Kept as the record of what was reported and
> what it turned into, not as a to-do list.
>
> | # | Reported as | Upstream | Outcome |
> |---|---|---|---|
> | 1 | child VM lacks `tool_registry` | [#868](https://github.com/Masterplanner25/Nodus/issues/868) → PR #874 | fixed — **12** attributes, not 2, across 4 derivation sites |
> | 2 | stranded runs vs `migrate-store` | [#869](https://github.com/Masterplanner25/Nodus/issues/869) → PR #876 | fixed, and raised to `severity:high` |
> | 3 | #482 reject-and-revise | [#870](https://github.com/Masterplanner25/Nodus/issues/870) → PR #877 | fixed as a **bug**, not a docs item |
> | 4 | `require_for_effects` | [nodus-approvals#1](https://github.com/Masterplanner25/nodus-approvals/issues/1) → PR #2 | shipped in **0.2.0** |
> | 5 | key-sorted rehydration | [#871](https://github.com/Masterplanner25/Nodus/issues/871) → PR #878 | fixed |
>
> Two further defects were found while fixing these and are also closed:
> [#873](https://github.com/Masterplanner25/Nodus/issues/873) (a resume inherited
> no run budget — five bounds lost, so a guest escaped every limit by parking and
> resuming) and
> [#875](https://github.com/Masterplanner25/Nodus/issues/875)
> (`max_terminal_runs` could not see the runs it existed to delete).
>
> **Three corrections to what this document says.** They are left in place below
> rather than edited away, because each one is a reason the report was useful:
>
> 1. **Item 1's "secondary symptom" was misdiagnosed here.** It is not `std:tool`
>    re-import stickiness. The repro resumes the *same* `graph_id` twice; the
>    first (broken) resume drives the run to `completed`, and the second returns
>    its recorded result without re-executing. With two distinct run ids the
>    unpatched direct path is green. One bug, not two.
> 2. **Item 1's suggested fix was too narrow, and the alternative was worse.**
>    Copying `tool_registry` and `effect_store` fixes 2 of 12 lost attributes;
>    `child._caller_vm = self`, which #868 floated as the wider option, fixes
>    **1 of 12** — `tool_registry` is the only one read through the `root_vm()`
>    chain, which is why it looked complete. The shipped fix names the whole set
>    once (`HOST_STATE_ATTRIBUTES` + `inherit_host_state`) at all four sites.
> 3. **Item 3's premise was right and upstream's was wrong.** #482 refused
>    checkpoint-plus-payload on the stated grounds that the payload was "silently
>    discarded". Measured on 4.0.8, it reached the replayed step; what it did not
>    do was *satisfy the wait*. The refusal now covers only `checkpoint` with no
>    payload, and the 5.5.0 changelog entry carries a correction note.
>
> **What changed in this repo as a result:** `src/policy.py` is deleted and
> `src/runtime.py` imports `ApprovalPolicy.require_for_effects` from
> nodus-approvals 0.2.0 (`requirements.txt` floors it at `>=0.2.0`). The
> workaround in item 1 (`ResearchRuntime._resume_on_primed_vm`) and the
> canonicalisation in item 5 (`_canonical_json`) are **still in place and still
> correct**; they are now belt-and-braces rather than load-bearing, and can be
> removed whenever someone wants to, once this project is on a nodus-lang release
> that includes the fixes. Nothing here has shipped in a nodus-lang release yet —
> all seven fixes are on `main` in `[Unreleased]`.


Repo: `C:\dev\claudecodenodus` (Nodus research agent). Written 2026-09-20 after
upgrading `nodus-lang` 4.0.8 → **5.14.0** (all `nodus-*` companions bumped;
`nodus-approvals` is still 0.1.0). Everything below was verified against the
installed 5.14.0 wheel on Windows 11 / Python 3.11; file:line refs are into
`venv/Lib/site-packages/`.

Items, most important first:

| # | What | Kind | Severity |
|---|------|------|----------|
| 1 | Cross-process resume runs on a child VM without `tool_registry`; `tool.call` fails soft, run reports success | bug | **High** — silent no-op of side effects |
| 2 | "Stranded runs" warning counts raw files; `migrate-store` age-filters, so old runs are reported as stranded but never migrated | bug | Medium — unclearable warning, becomes an *error* at 6.0 |
| 3 | #482 refusal gives no recipe for reject-and-revise; two-phase pattern should be documented (or given a sanctioned API) | docs / API | Medium |
| 4 | `ApprovalPolicy.require_for_effects` classmethod | feature (PR-ready) | Low |
| 5 | Rehydrated step results are key-sorted; `json.stringify` has no canonical mode → strings derived from step results differ after a restart | bug / API | Low–Medium |

---

## 1. `builtin_resume_workflow` resumes on a child VM that lacks `tool_registry`

**Affects:** 5.x (introduced with #328 `_resume_target_vm`; verified 5.14.0).
**Repro in this repo:** `probes/repro_v5_child_vm_tool_registry.py` (self-contained, no project code).

### Symptom

A host embeds `NodusRuntime`, registers Python tools via `runtime.tool_registry.register(...)`,
starts a workflow that parks at `workflow_wait`, and later — from a *second* runtime
(the durable human-in-the-loop case) — primes a VM with the same declarations and calls
`vm.builtin_resume_workflow(graph_id, None, payload)`. The resumed step's
`tool.call("host.echo", …)` returns an **error value** (`type(r) == "error"`,
`"Tool 'host.echo' is not registered"`), the host handler is never called, and the run
finishes `ok` with every step `completed`. Nothing raises, nothing is logged. In our
agent this manifested as `publish` reporting `published: true` with no file written.

```
$ python probes/repro_v5_child_vm_tool_registry.py
start status: waiting
primed vm has tool: True
via builtin_resume_workflow  -> {'tool_result_type': 'error', 'tool_result': "Tool 'host.echo' is not registered"} | handler calls: 0
via runner.resume_workflow   -> {'tool_result': "Tool 'host.echo' is not registered", ...}              | handler calls: 0   # <- see "secondary" below

$ python probes/repro_v5_child_vm_tool_registry.py --direct-only
via runner.resume_workflow   -> {'tool_result_type': 'record', 'tool_result': 'record {"echo": record {"x": "hi"}}'} | handler calls: 1
```

### Cause

- `nodus/vm/vm.py:1662-1702` `VM._resume_target_vm`: when the graph needs a rebuild and
  `self.code` is non-empty, it builds `child = VM([], {}, host_globals=..., …)` and copies
  `host_globals`, `memory_store`, `worker_dispatcher`, `builtins` and authority
  (`inherit_authority`) — but **not `self.tool_registry`** (`vm.py:366` initialises it to `{}`)
  **and not `self.effect_store`** either (`vm.py:318` gives the child a fresh
  `InMemoryEffectStore`), so a store injected with `NodusRuntime.set_effect_store()` is
  silently dropped on this path and `@exactly_once` loses its durability exactly when it
  matters (a resume in a new process). Verified: `vm._resume_target_vm(gid)` on a primed VM
  carrying a `SqliteEffectStore` returns a child whose `effect_store` is `InMemoryEffectStore`.
- `nodus/builtins/tool_module.py:278-289` `builtin_tool_invoke` looks tools up on
  `_root_vm(vm).tool_registry` → the child → `tool_not_found` → `make_err(...)` (a value,
  not a raise).
- The only way a primed VM avoids the diversion is `not self.code` — but any VM that ran the
  workflow *declarations* through `run_source` (which is how an embedder rebinds imports and
  attaches tools) has code loaded.

**Secondary symptom (observed, cause not pinned down):** once one `builtin_resume_workflow`
attempt has run in a process, handing a *different* freshly-primed VM straight to
`runner.resume_workflow(...)` also fails with `tool_not_found` (compare the two runs above).
`vm_chain.root_vm`'s docstring says stdlib builtins "close over whichever VM was current at
registration time"; it looks like the child VM's re-import of `std:tool` sticks. Worth a
look while fixing the primary.

### Suggested fix

In `_resume_target_vm`, after `inherit_authority(child, self)`:

```python
with self._tool_registry_lock:
    child.tool_registry.update(self.tool_registry)
child.effect_store = self.effect_store
```

(plus whatever `std:tool` binding fix the secondary symptom needs). A regression test:
register a Python tool on a runtime, load a workflow that calls it after a
`workflow_wait`, resume via `builtin_resume_workflow` from a second runtime, assert the
handler ran and an injected effect store was consulted.

More generally: `AUTHORITY_ATTRIBUTES` (`runtime/capability.py:858`) exists precisely so
derived VMs don't forget an attribute; a sibling list of *host-state* attributes
(`tool_registry`, `effect_store`, …) copied at the same site would close this class of bug.

### Workaround we ship

`src/runtime.py::ResearchRuntime._resume_on_primed_vm` bypasses the diversion:
`vm.resolve_workflow_runner().resume_workflow(vm, run_id, checkpoint, resume_payload=…, rebuild_graph=vm._rebuild_workflow_graph)`
on a VM whose program has already finished (nothing to clobber). We also made `publish`
`throw` when `type(_wr) == "error"` so a fail-soft `tool.call` can never again masquerade
as success.

---

## 2. Stranded-runs warning and `migrate-store` disagree on what a run is

**Affects:** 5.12+ (the `default-store-sqlite` staged flip, #174/#797); verified 5.14.0.

### Symptom

With `NODUS_WORKFLOW_STORE_BACKEND=sqlite` and an old JSON store present:

```
warning: 549 run record(s) in the file-backed JSON store are not in the SQLite store …
$ nodus workflow migrate-store --to sqlite      # migrated_count: 114, skipped: 0, failed: 0
warning: 435 run record(s) … are not in the SQLite store …
$ nodus workflow migrate-store --to sqlite --dry-run   # migrated_count: 0, skipped_count: 114
```

The 435 leftovers can never be migrated and the warning never clears. Per the 5.12 notes
this same condition **becomes an error at 6.0.0**, so a user with a >30-day-old JSON store
will be hard-blocked with no supported way out except deleting the directory by hand.

### Cause

- Warning side — `nodus_lang_workflow/runner.py:1513-1533` `_unmigrated_local_runs` does a raw
  `os.listdir(runs_dir)` for `*.json` (deliberately not constructing a store, per its comment).
- Migrate side — `nodus_lang_workflow/store.py:1446+` `migrate_workflow_store` iterates
  `source.list_runs()`, and `LocalWorkflowStore._list_runs_unlocked` (`store.py:953-967`)
  **skips files whose mtime is older than `terminal_max_age_days`** (default 30,
  `store.py:544`). In our case the 435 were June test runs.

### Suggested fix

Either make `migrate_workflow_store` enumerate with the age filter off (a migration should
be exhaustive; `terminal_max_age_days` is a *scan-cost* bound, not a retention policy —
`store.py:682` says so), or make the warning use the same filtered listing. The former is
right: a parked run older than 30 days is exactly the one a user cannot afford to lose.
Add a `--all` / `--max-age-days 0` flag if changing the default is too much.

Repro recipe: start any workflow under the local store, `os.utime` its
`.nodus/workflow_framework/runs/<id>.json` to 40 days ago, set the backend to sqlite,
run `migrate-store` → skipped / not listed; run anything → warning names it.

---

## 3. #482 refusal: document the reject-and-revise pattern (or give it an API)

**Affects:** 5.x; verified 5.14.0. `nodus_lang_workflow/runner.py:1235-1268`.

The refusal itself is correct — on 5.14.0 a checkpoint rollback while the run waits *would*
re-arm the wait and drop the payload. But the error text only says "drop the checkpoint
argument", which advances the run; it never says how to do the thing people reach for
a checkpoint to do: **reject a draft and replay from before it with feedback**. On 4.0.8
`resume_workflow(id, "before_draft", {feedback})` did exactly that (payload delivered to
`workflow_resume_payload()` in the replayed step), so this is a silent behaviour change
for a documented human-in-the-loop pattern, and the 5.0 changelog doesn't list it.

What works on 5.x (verified, `probes/probe_checkpoint_replay.nd`):

```
resume_workflow(id, {"approved": false, "feedback": "..."})   // satisfy the wait; post-wait steps
                                                            // must gate on the payload and no-op
resume_workflow(id, "before_draft", {"feedback": "..."})      // now allowed: replays, re-suspends
```

Ask upstream for one of:

1. **Docs:** add the two-phase recipe to the #482 error message and to the workflow guide,
   and note that every post-wait step must gate its side effects on the wait payload.
2. **API (nicer):** `resume_workflow(id, checkpoint, payload)` on a waiting run could
   *release* the wait and roll back atomically when the caller says so — e.g. an
   `on_wait: "release"` option — instead of forcing the post-wait steps to run as no-ops.
   The Python runner already accepts `event_type=`, which skips the refusal
   (`runner.py:1237`), but the `.nd` builtin (`vm.py:1703`) does not expose it.

---

## 4. `ApprovalPolicy.require_for_effects` — PR-ready

**Still needed as of 2026-09-20:** `nodus-approvals` is unchanged at 0.1.0;
`nodus_approvals/policy.py` ships only `allow_all`, `deny_all`, `require_for(*patterns)`.
Our in-repo version is `src/policy.py` (used by `src/runtime.py` and `src/approval_api.py`).

Motivation: gating on **declared effects** rather than tool names means a new tool that
declares `fs.write` is gated automatically — no allowlist to maintain. Proposed addition
to `nodus_approvals/policy.py` (mirrors the style of the existing factories):

```python
    @classmethod
    def require_for_effects(
        cls,
        manifests: Iterable[Mapping[str, Any]],
        effects: Iterable[str],
    ) -> "ApprovalPolicy":
        """Require approval for every tool whose manifest declares one of *effects*.

        *manifests* are tool manifests (``{"name": ..., "effects": [...]}``); the
        effect vocabulary is the caller's (``nodus_lang_schema.VALID_EFFECTS`` or a
        finer host-specific set such as ``fs.write`` / ``network.write``).
        Auto-approves everything else; if nothing matches, behaves as ``allow_all``.
        """
        gated = frozenset(effects)
        names = [m["name"] for m in manifests if gated & frozenset(m.get("effects", ()))]
        return cls.require_for(*names) if names else cls.allow_all()
```

Tests to include: (a) a manifest with a gated effect → `resolve(name).mode == REQUIRE`;
(b) a manifest without → `AUTO`; (c) empty match → equivalent to `allow_all()`;
(d) effect vocabulary is opaque (works with both `"filesystem"` and `"fs.write"`).
Once merged, delete `src/policy.py` and import from `nodus_approvals`.

One design note for the PR discussion: `nodus_lang_schema.VALID_EFFECTS`
(`contracts.py:9`) is `{pure, reads_state, writes_state, network, filesystem, spawns_task}`
— coarser than the read/write split we use. Keeping the classmethod vocabulary-agnostic
sidesteps that; if upstream wants to standardise, `filesystem` / `network` could gain
`.read` / `.write` refinements.

---

## 5. Rehydrated step results are key-sorted; no canonical `json.stringify`

**Affects:** 5.14.0 (likely all 5.x); `nodus_lang_workflow/store.py:592` (local) and the SQLite
store both persist graph state with `sort_keys=True`.

### Symptom

A step does `let s = json.stringify(analyze)` where `analyze` is a prior step's result map.
In the original process the map has insertion order (`web, code, data`); after a
cross-process resume the same result comes back from the store with sorted keys
(`code, data, web`; `snippet, title, url`). Same content, different string. Anything
derived from it — a draft, a hash, an `@exactly_once` key — differs after a restart.
Found because an `@exactly_once` publish keyed on the draft re-fired on a rehydrated replay
of an identical draft.

### Suggested fix (either)

1. Persist without `sort_keys=True` — JSON already preserves object order, and
   round-tripping insertion order is what makes a rehydrated run behave like the live one.
2. Or give `std:json` a canonical mode — `json.stringify(value, {"sort_keys": true})` — and
   document that step results are not order-stable across rehydration.

Workaround we ship: the host canonicalises the JSON (`src/runtime.py::_canonical_json`)
before it is embedded in anything identity-bearing.

---

## Rollback reference (in case any of the above blocks someone)

```
pip install nodus-lang==4.0.8 nodus-extension==0.1.0 nodus-mcp==0.1.0
```
and revert commit `b19cab1` (this repo). The 4.x code paths are documented in the
update log in `Session Handoff Summary.md`.
