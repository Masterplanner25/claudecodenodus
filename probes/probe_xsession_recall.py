"""Cross-session recall across two genuine OS processes.

    PYTHONPATH=. venv/Scripts/python probes/probe_xsession_recall.py <workspace> a
    PYTHONPATH=. venv/Scripts/python probes/probe_xsession_recall.py <workspace> b

A researches "What is LLM safety?" and publishes; B, in a separate process over
the same workspace, asks a differently-phrased question sharing topic words and
should recall exactly A's published final (not its meta node or interim draft)
and carry it into B's draft.  Hermetic: offline web backend, no LLM, no Docker.
"""
import os
import sys

sys.path.insert(0, r"C:\dev\claudecodenodus")
from nodus_approvals import ApprovalPolicy
from nodus_retry import InMemoryEffectStore

from src.notify import ConsoleNotifier
from src.runtime import ResearchRuntime
from src.sandbox import DisabledCodeRunner
from src.web import OfflineWebBackend

WS = sys.argv[1]
ROLE = sys.argv[2]

rt = ResearchRuntime(
    workspace=WS, policy=ApprovalPolicy.allow_all(), llm_client=None,
    web_backend=OfflineWebBackend(), code_runner=DisabledCodeRunner(),
    notifier=ConsoleNotifier(), effect_store=InMemoryEffectStore(),
)
try:
    if ROLE == "a":
        r = rt.start("What is LLM safety?", "sess-a")
        rt.resume(r["graph_id"], {"approved": True})
        print(f"[pid {os.getpid()}] A published; recall count was {r['steps']['recall']['count']}")
        print(f"[pid {os.getpid()}] A tags on final: {rt._memory_store.tags_for('research/sess-a/final')}")
    else:
        r = rt.start("Recent advances in LLM safety evaluation", "sess-b")
        rec = r["steps"]["recall"]
        print(f"[pid {os.getpid()}] B recalled {rec['count']}: {[x['key'] for x in rec['results']]}")
        print(f"[pid {os.getpid()}] B draft carries prior block: "
              f"{'Prior research on this topic' in r['state']['draft']}")
        print(f"[pid {os.getpid()}] B draft first line: {r['state']['draft'].splitlines()[0]}")
finally:
    rt.shutdown()
