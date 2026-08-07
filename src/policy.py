"""Effect-based approval policy construction.

The approval gate is wired to *effects*, not tool names: any tool whose manifest
declares a gated effect (``fs.write``, ``network.write``) automatically requires
human approval — no allowlist of tool names to maintain.

``require_for_effects`` is the factory the design plan calls for.  The upstream
``nodus_approvals.ApprovalPolicy`` only ships ``require_for(*patterns)`` (matches
the action string); this routes through each tool manifest's ``effects`` list to
derive those patterns.  Implemented in-repo for now; the natural follow-up is to
upstream it as an ``ApprovalPolicy.require_for_effects`` classmethod (mirroring
how the v4.0.7 rehydrate fix was upstreamed).
"""
from __future__ import annotations

from collections.abc import Iterable

from nodus_approvals import ApprovalPolicy


def tools_with_effects(manifests: list[dict], effects: Iterable[str]) -> list[str]:
    """Names of tools whose declared effects intersect *effects*."""
    gated = frozenset(effects)
    return [m["name"] for m in manifests if frozenset(m.get("effects", [])) & gated]


def require_for_effects(manifests: list[dict], effects: Iterable[str]) -> ApprovalPolicy:
    """Build an ``ApprovalPolicy`` requiring approval for any tool declaring a gated effect.

    Auto-approves everything else.  If no tool declares a gated effect, the policy
    auto-approves all actions.
    """
    names = tools_with_effects(manifests, effects)
    return ApprovalPolicy.require_for(*names) if names else ApprovalPolicy.allow_all()
