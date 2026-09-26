"""Single-use approvals with quorum rules (final draft §6.3, §10).

- Decisions bind to one content hash; a decision for other content is rejected.
- Required approvers are distinct identities; security requirements need a security holder.
- Regulated: the requester can't approve. HM: people who edited the code can't approve.
- A 'revise' or 'cancel' from any approver voids the round.
- Expiry means the operation must START before `expires`; a timeout never approves.
- Consumption is atomic in the store: compare-and-swap on state version + a fencing token.
- A rollback invalidates every unconsumed approval for that artifact.
"""
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime

from ..policy import Requirement


class ApprovalError(Exception):
    pass


class Expired(ApprovalError):
    pass


class StaleApproval(ApprovalError):
    pass


class AlreadyConsumed(ApprovalError):
    pass


class Voided(ApprovalError):
    pass


class QuorumNotMet(ApprovalError):
    pass


class IneligibleApprover(ApprovalError):
    pass


SECURITY_ROLE = "security"


@dataclass(frozen=True)
class Decision:
    approver: str
    roles: frozenset
    decision: str           # approve | revise | defer | cancel
    at: datetime


@dataclass
class ApprovalRequest:
    request_id: str
    gate: str                       # H1 | HM | H2 | HX
    mission_ids: tuple
    operation_id: str
    artifact: str | None
    content_hash: str
    policy_version: str
    state_version: int
    required: Requirement
    risk_profile: str
    requester: str
    expires: datetime
    editors: frozenset = frozenset()
    nonce: str = field(default_factory=lambda: secrets.token_hex(16))
    decisions: list = field(default_factory=list)
    status: str = "open"            # open | void | consumed | invalidated
    consumed_by: str | None = None
    consumed_at: datetime | None = None
    fencing_token: int | None = None

    def __post_init__(self):
        if not self.required.needs_human:
            raise ValueError(f"requirement {self.required} does not need a human approval request")

    @property
    def needed(self) -> int:
        return self.required.approvals if self.required.kind == "approve" else 1

    def approvers(self) -> list[Decision]:
        return [d for d in self.decisions if d.decision == "approve"]

    def quorum_met(self) -> bool:
        approvals = self.approvers()
        if len({d.approver for d in approvals}) < self.needed:
            return False
        if self.required.security and not any(SECURITY_ROLE in d.roles for d in approvals):
            return False
        return True


class ApprovalStore:
    """In-memory reference implementation. Phase 2 backs this with Postgres (same semantics)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._requests: dict[str, ApprovalRequest] = {}
        self._fencing = 0

    def add(self, request: ApprovalRequest) -> ApprovalRequest:
        with self._lock:
            if request.request_id in self._requests:
                raise ApprovalError(f"duplicate request id {request.request_id}")
            self._requests[request.request_id] = request
            return request

    def get(self, request_id: str) -> ApprovalRequest:
        return self._requests[request_id]

    def decide(self, request_id: str, *, approver: str, roles, decision: str, content_hash: str,
               now: datetime) -> ApprovalRequest:
        if decision not in ("approve", "revise", "defer", "cancel"):
            raise ValueError(f"unknown decision {decision!r}")
        with self._lock:
            req = self._requests[request_id]
            _require_open(req)
            if now >= req.expires:
                raise Expired(f"{request_id} expired at {req.expires.isoformat()}")
            if content_hash != req.content_hash:
                raise StaleApproval(f"{request_id}: decision is for different content")
            roles = frozenset(roles)
            if decision in ("revise", "cancel"):
                req.decisions.append(Decision(approver, roles, decision, now))
                req.status = "void"
                return req
            if decision == "defer":
                req.decisions.append(Decision(approver, roles, decision, now))
                return req
            if req.gate == "HM" and approver in req.editors:
                raise IneligibleApprover(f"{approver} edited this change and can't approve its merge")
            if req.risk_profile == "regulated" and approver == req.requester:
                raise IneligibleApprover(f"{approver} requested this and can't approve it (regulated)")
            if any(d.approver == approver for d in req.approvers()):
                return req  # idempotent duplicate
            req.decisions.append(Decision(approver, roles, "approve", now))
            return req

    def consume(self, request_id: str, *, executor: str, now: datetime, state_version: int,
                content_hash: str, policy_version: str) -> int:
        """Atomically consume an approval right before the side effect. Returns a fencing token."""
        with self._lock:
            req = self._requests[request_id]
            if req.status == "consumed":
                raise AlreadyConsumed(f"{request_id} was consumed by {req.consumed_by} at {req.consumed_at}")
            _require_open(req)
            if not req.quorum_met():
                raise QuorumNotMet(f"{request_id}: {len(req.approvers())}/{req.needed} approvals"
                                   + (" (security required)" if req.required.security else ""))
            if now >= req.expires:
                raise Expired(f"{request_id}: operation must start before {req.expires.isoformat()}")
            if (state_version, content_hash, policy_version) != (req.state_version, req.content_hash,
                                                                 req.policy_version):
                raise StaleApproval(f"{request_id}: state, content or policy changed since the request")
            self._fencing += 1
            req.status = "consumed"
            req.consumed_by = executor
            req.consumed_at = now
            req.fencing_token = self._fencing
            return self._fencing

    def invalidate_for_artifact(self, artifact: str, reason: str) -> list[str]:
        """Rollback: every unconsumed approval for this artifact becomes invalid."""
        with self._lock:
            hit = []
            for req in self._requests.values():
                if req.artifact == artifact and req.status == "open":
                    req.status = "invalidated"
                    hit.append(req.request_id)
            return hit


def _require_open(req: ApprovalRequest) -> None:
    if req.status == "void":
        raise Voided(f"{req.request_id} was voided by a revise/cancel decision")
    if req.status == "invalidated":
        raise Voided(f"{req.request_id} was invalidated (e.g. by a rollback)")
    if req.status == "consumed":
        raise AlreadyConsumed(f"{req.request_id} already consumed")


class FencedTarget:
    """A side-effect target that rejects stale executors (e.g. the deploy controller's target)."""

    def __init__(self):
        self.highest_token = 0
        self.effects: list[tuple[int, str]] = []

    def apply(self, token: int, effect: str) -> None:
        if token <= self.highest_token:
            raise StaleApproval(f"fencing token {token} <= {self.highest_token}")
        self.highest_token = token
        self.effects.append((token, effect))
