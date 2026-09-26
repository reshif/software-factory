"""Decision-packet construction (final draft §10, build spec §3 B7).

"What the decision packet contains": recommendation and alternatives, the raw
diff, evidence (checks + holdout), risk class and the rule that fired, blast
radius, every mission in the digest (H2), the recovery plan, cost, the
untrusted inputs the agents read, and request id / nonce / roles / expiry
(the last two live on the paired `ApprovalRequest`, not the packet itself).

This module only builds the `DecisionPacket`. The paired `ApprovalRequest`
(quorum, nonce, CAS state) is built by the orchestrator directly from
`factory.controller.approvals`, since that's the reference implementation
whose semantics `PostgresStateStore` must reproduce -- duplicating it here
would just be a second, driftable copy.
"""
from __future__ import annotations

import uuid

from ..models import DecisionPacket, EvidenceBundle, MissionRecord

HX_ALTERNATIVES = (
    "Extend the mission budget by 50% and reset repair attempts.",
    "Reduce scope: cancel the failing task(s) and integrate only what already passed.",
    "Cancel the mission.",
)


def new_request_id(gate: str) -> str:
    return f"REQ-{gate}-{uuid.uuid4().hex[:10]}"


def build_packet(*, request_id: str, gate: str, mission: MissionRecord, title: str, recommendation: str,
                 summary: str, required: str, expires, content_hash: str, raw_diff: str = "",
                 alternatives: tuple = (), evidence: EvidenceBundle | None = None,
                 recovery_plan: str | None = None, cost_usd: float = 0.0,
                 untrusted_inputs: tuple = (), extra_mission_ids: tuple = (),
                 links: dict | None = None) -> DecisionPacket:
    """Build one `DecisionPacket`, bound to `mission` (plus `extra_mission_ids` for a digest)."""
    return DecisionPacket(
        request_id=request_id,
        gate=gate,
        mission_ids=(mission.mission_id, *extra_mission_ids),
        title=title,
        recommendation=recommendation,
        summary=summary,
        required=required,
        expires=expires,
        content_hash=content_hash,
        raw_diff=raw_diff,
        alternatives=tuple(alternatives),
        evidence=evidence,
        recovery_plan=recovery_plan,
        cost_usd=cost_usd,
        untrusted_inputs=tuple(untrusted_inputs),
        links=dict(links or {}),
    )


def hx_title(mission: MissionRecord, reason: str) -> str:
    return f"HX exception for {mission.mission_id}: {reason}"


__all__ = ["HX_ALTERNATIVES", "build_packet", "hx_title", "new_request_id"]
