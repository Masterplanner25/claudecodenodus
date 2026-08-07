"""Durable, file-backed implementation of the ``nodus_approvals`` ApprovalStore.

The bundled ``InMemoryApprovalStore`` keeps approval requests and results in a
per-process dict, so a human approval recorded in one process is invisible to a
workflow resumed in another.  That defeats the agent's core use case — a person
approving a draft hours later, in a different process or on a different machine.

``FileApprovalStore`` persists each request and result as its own JSON file under
a shared directory, so any process pointed at the same directory sees the same
approval state.  It implements the ``ApprovalStore`` Protocol structurally
(save / get / resolve / get_result / pending / expire_old), so it drops straight
into ``ApprovalGate(store=...)``.

Concurrency model: writes are atomic (write-temp-then-``os.replace``), so a
reader never sees a half-written file; an in-process lock serialises this
process's own writes.  Cross-process coordination relies on the atomic replace —
sufficient for the approve-once / read-many approval flow (there is no
read-modify-write race on a single request).
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from nodus_approvals.request import ApprovalRequest, ApprovalResult


def _dt_to_iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _dt_from_iso(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None


def _request_to_dict(req: ApprovalRequest) -> dict:
    return {
        "id": req.id,
        "action": req.action,
        "requester_id": req.requester_id,
        "context": req.context,
        "created_at": _dt_to_iso(req.created_at),
        "expires_at": _dt_to_iso(req.expires_at),
        "metadata": req.metadata,
    }


def _request_from_dict(d: dict) -> ApprovalRequest:
    return ApprovalRequest(
        id=d["id"],
        action=d["action"],
        requester_id=d["requester_id"],
        context=dict(d.get("context") or {}),
        created_at=_dt_from_iso(d.get("created_at")) or datetime.now(timezone.utc),
        expires_at=_dt_from_iso(d.get("expires_at")),
        metadata=dict(d.get("metadata") or {}),
    )


def _result_to_dict(res: ApprovalResult) -> dict:
    return {
        "request_id": res.request_id,
        "approved": res.approved,
        "approver_id": res.approver_id,
        "timestamp": _dt_to_iso(res.timestamp),
        "reason": res.reason,
    }


def _result_from_dict(d: dict) -> ApprovalResult:
    return ApprovalResult(
        request_id=d["request_id"],
        approved=bool(d["approved"]),
        approver_id=d.get("approver_id"),
        timestamp=_dt_from_iso(d.get("timestamp")) or datetime.now(timezone.utc),
        reason=d.get("reason"),
    )


class FileApprovalStore:
    """Durable approval store backed by JSON files in a directory.

    Layout under *root*::

        <root>/requests/<request_id>.json
        <root>/results/<request_id>.json

    A request is "pending" when its request file exists, it has no matching
    result file, and it has not expired.
    """

    def __init__(self, root: str | os.PathLike) -> None:
        self._root = Path(root)
        self._requests_dir = self._root / "requests"
        self._results_dir = self._root / "results"
        self._requests_dir.mkdir(parents=True, exist_ok=True)
        self._results_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ── internal helpers ────────────────────────────────────────────────
    def _atomic_write(self, path: Path, payload: dict) -> None:
        tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, path)

    @staticmethod
    def _read_json(path: Path) -> dict | None:
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            # Missing or mid-write/corrupt — treat as absent.
            return None

    # ── ApprovalStore protocol ──────────────────────────────────────────
    def save(self, request: ApprovalRequest) -> None:
        with self._lock:
            self._atomic_write(
                self._requests_dir / f"{request.id}.json",
                _request_to_dict(request),
            )

    def get(self, request_id: str) -> ApprovalRequest | None:
        d = self._read_json(self._requests_dir / f"{request_id}.json")
        return _request_from_dict(d) if d is not None else None

    def resolve(self, request_id: str, result: ApprovalResult) -> None:
        with self._lock:
            self._atomic_write(
                self._results_dir / f"{request_id}.json",
                _result_to_dict(result),
            )

    def get_result(self, request_id: str) -> ApprovalResult | None:
        d = self._read_json(self._results_dir / f"{request_id}.json")
        return _result_from_dict(d) if d is not None else None

    def pending(self) -> list[ApprovalRequest]:
        """Requests with no result and not expired."""
        now = datetime.now(timezone.utc)
        out: list[ApprovalRequest] = []
        for req_file in self._requests_dir.glob("*.json"):
            request_id = req_file.stem
            if (self._results_dir / f"{request_id}.json").exists():
                continue
            d = self._read_json(req_file)
            if d is None:
                continue
            req = _request_from_dict(d)
            if req.expires_at is None or req.expires_at > now:
                out.append(req)
        return out

    def expire_old(self) -> int:
        """Remove expired, unresolved request files. Returns count removed."""
        now = datetime.now(timezone.utc)
        removed = 0
        with self._lock:
            for req_file in self._requests_dir.glob("*.json"):
                request_id = req_file.stem
                if (self._results_dir / f"{request_id}.json").exists():
                    continue
                d = self._read_json(req_file)
                if d is None:
                    continue
                req = _request_from_dict(d)
                if req.expires_at is not None and req.expires_at <= now:
                    try:
                        req_file.unlink()
                        removed += 1
                    except FileNotFoundError:
                        pass
        return removed
