"""Upstream repro (nodus-lang 5.14.0): a cross-process resume via
``vm.builtin_resume_workflow`` runs the resumed steps on a child VM that does
not inherit ``tool_registry``, so ``tool.call`` inside a resumed step fails
soft with ``tool_not_found`` while the run reports success.

    PYTHONPATH=. venv/Scripts/python probes/repro_v5_child_vm_tool_registry.py
    PYTHONPATH=. venv/Scripts/python probes/repro_v5_child_vm_tool_registry.py --direct-only

Expected (both modes): step b returns the echoed record and the host handler is
called once.  Actual on 5.14.0: the builtin path returns an error VALUE
(``type(r) == "error"``, "Tool 'host.echo' is not registered") and the handler
is never called; the runner-direct path works when run alone (--direct-only),
but ALSO fails once a builtin_resume_workflow attempt has run in the process.
See docs/upstream-handoff.md, item 1.
"""
import os
import sys

os.environ.setdefault("NODUS_WORKFLOW_STORE_BACKEND", "sqlite")
from nodus.runtime.embedding import NodusRuntime  # noqa: E402

SRC = '''
import "std:tool" as tool
workflow w {
    step a { return workflow_wait("go", "k1", {}) }
    step b after a {
        let r = tool.call("host.echo", {"x": "hi"})
        return {"tool_result_type": type(r), "tool_result": str(r)}
    }
}
'''
CALLS: list = []
CAPTURED: dict = {}


def make_rt() -> NodusRuntime:
    rt = NodusRuntime()
    # The pinned source re-executes on rebuild, so every runtime must know _cap.
    rt.register_function("_cap", lambda r: CAPTURED.__setitem__("r", r), arity=1)
    rt.tool_registry.register({
        "name": "host.echo", "description": "echo", "schema": {},
        "handler": lambda args: CALLS.append(args) or {"echo": args},
    })
    return rt


def show(label: str, rt: NodusRuntime, raw) -> None:
    res = rt._to_host_value(raw)
    print(f"{label:<32} -> {res.get('steps', {}).get('b', res)} | handler calls: {len(CALLS)}")


# Process 1: start a run and park it at the wait.
rt1 = make_rt()
rt1.run_source(SRC + "\n_cap(run_workflow(w))\n")
gid = rt1._to_host_value(CAPTURED["r"])["graph_id"]
print("start status:", rt1._to_host_value(CAPTURED["r"])["status"])

# "Process 2": a fresh runtime primes a VM with the same declarations.
rt2 = make_rt()
rt2.run_source(SRC)  # the VM now has a program loaded (vm.code non-empty)
vm = rt2._get_active_vm()
print("primed vm has tool:", "host.echo" in vm.tool_registry)

if "--direct-only" not in sys.argv:
    # The documented entry point: diverts to a child VM (#328) with no tool_registry.
    show("via builtin_resume_workflow", rt2, vm.builtin_resume_workflow(gid, None, {"ok": True}))

# Same primed VM handed to the runner directly (what this project does).
rt3 = make_rt()
rt3.run_source(SRC)
vm3 = rt3._get_active_vm()
show("via runner.resume_workflow", rt3, vm3.resolve_workflow_runner().resume_workflow(
    vm3, gid, None, resume_payload={"ok": True}, rebuild_graph=vm3._rebuild_workflow_graph,
))
