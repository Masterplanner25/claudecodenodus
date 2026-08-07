"""Human-approval API for the research agent.

Two layers, deliberately separated so the core is testable without a socket:

* ``ApprovalService`` — framework-agnostic application logic over a
  ``ResearchRuntime``.  Starts runs, lists drafts awaiting review, and
  approves / rejects them by driving the workflow resume loop.  Also surfaces
  the durable effect-gate (pending tool requests, approve / deny).
* ``serve`` / ``ResearchApprovalHandler`` — a thin stdlib ``http.server``
  adapter that maps JSON HTTP requests onto the service.  No third-party web
  framework, so nothing new to install and tests stay hermetic.

Approval model
--------------
The human-in-the-loop checkpoint is the draft ``review`` step: the workflow
suspends on ``workflow_wait`` and the service hands back the draft.  Approving
resumes to ``publish``; rejecting replays the draft from its checkpoint with
feedback.  Because that review *is* the human authorization for the writes in
``publish``, the service defaults its runtime to an auto-approve effect policy.

The effect gate (durable ``FileApprovalStore``, see ``approval_store``) is a
separate, out-of-flow authorization layer exposed under ``/gate`` for operators
who run with a stricter ``require_for_effects`` policy and approve individual
tool invocations themselves.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from nodus_approvals import ApprovalPolicy

from src.runtime import ResearchRuntime


class ApprovalError(Exception):
    """Raised for bad requests against the service (maps to HTTP 4xx)."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class ApprovalService:
    """Application logic for starting research runs and approving their drafts.

    A single long-lived service process owns one ``ResearchRuntime``.  The
    in-memory ``_runs`` index caches per-run convenience metadata (question,
    latest draft, status); the durable source of truth for resume is the Nodus
    workflow run store on disk, which ``runtime.resume*`` reads.
    """

    def __init__(self, runtime: ResearchRuntime | None = None, *, workspace: str | None = None) -> None:
        # Default to auto-approve at the tool layer: the draft review is the human gate.
        self._runtime = runtime or ResearchRuntime(
            workspace=workspace, policy=ApprovalPolicy.allow_all()
        )
        self._runs: dict[str, dict[str, Any]] = {}

    # ── draft review lifecycle ──────────────────────────────────────────
    def start_research(self, question: str, session_id: str) -> dict:
        if not question or not session_id:
            raise ApprovalError("both 'question' and 'session_id' are required")
        result = self._runtime.start(question, session_id)
        run_id = result.get("graph_id")
        record = {
            "run_id": run_id,
            "session_id": session_id,
            "question": question,
            "status": result.get("status", "unknown"),
            "draft": result.get("state", {}).get("draft"),
        }
        self._runs[run_id] = record
        return dict(record)

    def list_pending(self) -> list[dict]:
        """Runs currently suspended awaiting a draft decision."""
        return [dict(r) for r in self._runs.values() if r["status"] == "waiting"]

    def get_run(self, run_id: str) -> dict:
        record = self._runs.get(run_id)
        if record is None:
            raise ApprovalError(f"unknown run '{run_id}'", status=404)
        return dict(record)

    def approve(self, run_id: str) -> dict:
        """Approve the draft and resume the workflow to completion (publish)."""
        self._require_waiting(run_id)
        result = self._runtime.resume(run_id, {"approved": True})
        published = bool(result.get("steps", {}).get("publish", {}).get("published"))
        record = self._runs[run_id]
        record["status"] = "published" if published else "completed"
        return {"run_id": run_id, "status": record["status"], "published": published}

    def reject(self, run_id: str, feedback: str) -> dict:
        """Reject the draft with feedback; the workflow revises and re-suspends."""
        if not feedback:
            raise ApprovalError("'feedback' is required to reject a draft")
        self._require_waiting(run_id)
        result = self._runtime.resume_with_feedback(run_id, feedback)
        record = self._runs[run_id]
        record["status"] = result.get("status", "unknown")
        record["draft"] = result.get("state", {}).get("draft")
        return {"run_id": run_id, "status": record["status"], "draft": record["draft"]}

    def _require_waiting(self, run_id: str) -> None:
        record = self._runs.get(run_id)
        if record is None:
            raise ApprovalError(f"unknown run '{run_id}'", status=404)
        if record["status"] != "waiting":
            raise ApprovalError(
                f"run '{run_id}' is '{record['status']}', not awaiting approval",
                status=409,
            )

    # ── effect-gate (out-of-flow tool approvals) ────────────────────────
    def list_gate_requests(self) -> list[dict]:
        return [
            {
                "id": r.id,
                "action": r.action,
                "requester_id": r.requester_id,
                "context": r.context,
                "created_at": r.created_at.isoformat(),
            }
            for r in self._runtime.pending_gate_requests()
        ]

    def approve_gate(self, request_id: str, approver_id: str = "human") -> dict:
        self._runtime.gate_approve(request_id, approver_id=approver_id)
        return {"request_id": request_id, "approved": True, "approver_id": approver_id}

    def deny_gate(self, request_id: str, approver_id: str = "human") -> dict:
        self._runtime.gate_deny(request_id, approver_id=approver_id)
        return {"request_id": request_id, "approved": False, "approver_id": approver_id}

    def shutdown(self) -> None:
        self._runtime.shutdown()


# ── stdlib HTTP adapter ─────────────────────────────────────────────────────

class ResearchApprovalHandler(BaseHTTPRequestHandler):
    """Maps JSON HTTP requests onto a ``service`` set on the server.

    Routes::

        POST /research                      {question, session_id}
        GET  /approvals                     -> waiting runs
        GET  /approvals/{run_id}            -> one run (incl. draft)
        POST /approvals/{run_id}/approve
        POST /approvals/{run_id}/reject     {feedback}
        GET  /gate/pending                  -> pending tool requests
        POST /gate/{request_id}/approve     {approver_id?}
        POST /gate/{request_id}/deny        {approver_id?}
    """

    # Silence default stderr request logging during tests.
    def log_message(self, *args: Any) -> None:  # noqa: D401
        return

    @property
    def service(self) -> ApprovalService:
        return self.server.service  # type: ignore[attr-defined]

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            raise ApprovalError(f"invalid JSON body: {exc}") from exc

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, method: str) -> None:
        parts = [p for p in self.path.split("?")[0].strip("/").split("/") if p]
        try:
            payload = self._route(method, parts)
            self._send(200, payload)
        except ApprovalError as exc:
            self._send(exc.status, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 — surface unexpected errors as 500
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _route(self, method: str, parts: list[str]) -> Any:
        svc = self.service

        if method == "POST" and parts == ["research"]:
            body = self._read_json()
            return svc.start_research(body.get("question", ""), body.get("session_id", ""))

        if method == "GET" and parts == ["approvals"]:
            return svc.list_pending()

        if method == "GET" and len(parts) == 2 and parts[0] == "approvals":
            return svc.get_run(parts[1])

        if method == "POST" and len(parts) == 3 and parts[0] == "approvals":
            run_id, action = parts[1], parts[2]
            if action == "approve":
                return svc.approve(run_id)
            if action == "reject":
                return svc.reject(run_id, self._read_json().get("feedback", ""))

        if method == "GET" and parts == ["gate", "pending"]:
            return svc.list_gate_requests()

        if method == "POST" and len(parts) == 3 and parts[0] == "gate":
            request_id, action = parts[1], parts[2]
            approver = self._read_json().get("approver_id", "human")
            if action == "approve":
                return svc.approve_gate(request_id, approver)
            if action == "deny":
                return svc.deny_gate(request_id, approver)

        raise ApprovalError(f"no route for {method} /{'/'.join(parts)}", status=404)

    def do_GET(self) -> None:  # noqa: N802 — stdlib naming
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")


def serve(service: ApprovalService, host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    """Build (do not start) a threading HTTP server bound to *host*:*port*.

    Call ``server.serve_forever()`` to run it.  Pass ``port=0`` for an ephemeral
    port (read it back from ``server.server_address``).
    """
    server = ThreadingHTTPServer((host, port), ResearchApprovalHandler)
    server.service = service  # type: ignore[attr-defined]
    return server


def main() -> None:  # pragma: no cover — manual entry point
    service = ApprovalService()
    server = serve(service, port=8000)
    print(f"Approval API listening on http://{server.server_address[0]}:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        service.shutdown()


if __name__ == "__main__":  # pragma: no cover
    main()
