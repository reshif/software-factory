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
    # approver -> roles, from PRODUCT CONFIG for this gate (red team #3 H2). The factory
    # always sets it; roles claimed elsewhere (e.g. in an inbox token) are ignored.
    # None means unrestricted and exists only for unit tests of the bare rules.
    eligible: dict | None = None
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


def require_open(req: ApprovalRequest) -> None:
    """Raise if `req` is already void, invalidated or consumed. The one open-ness check."""
    if req.status == "void":
        raise Voided(f"{req.request_id} was voided by a revise/cancel decision")
    if req.status == "invalidated":
        raise Voided(f"{req.request_id} was invalidated (e.g. by a rollback)")
    if req.status == "consumed":
        raise AlreadyConsumed(f"{req.request_id} already consumed")


def check_decision(req: ApprovalRequest, *, approver: str, roles, decision: str, content_hash: str,
                   now: datetime) -> Decision | None:
    """Validate a decision against `req` and return the `Decision` to record.

    Returns `None` for an idempotent duplicate approve (nothing to record, nothing
    changes). Raises the same `ApprovalError` subclasses `ApprovalStore.decide` always
    has. Pure: never mutates `req`, so both the in-memory store and the Postgres
    adapter can lock their own way, call this, and persist the result identically
    (`C1`: the rule lives in exactly one place).
    """
    if decision not in ("approve", "revise", "defer", "cancel"):
        raise ValueError(f"unknown decision {decision!r}")
    require_open(req)
    if req.eligible is not None:
        if approver not in req.eligible:
            raise IneligibleApprover(f"{approver} is not a listed approver for {req.gate} of this product")
        roles = req.eligible[approver]   # config wins over any claimed roles
    roles = frozenset(roles)
    if now >= req.expires:
        raise Expired(f"{req.request_id} expired at {req.expires.isoformat()}")
    if content_hash != req.content_hash:
        raise StaleApproval(f"{req.request_id}: decision is for different content")
    if decision in ("revise", "cancel", "defer"):
        return Decision(approver, roles, decision, now)
    if req.gate == "HM" and approver in req.editors:
        raise IneligibleApprover(f"{approver} edited this change and can't approve its merge")
    if req.risk_profile == "regulated" and approver == req.requester:
        raise IneligibleApprover(f"{approver} requested this and can't approve it (regulated)")
    if any(d.approver == approver for d in req.approvers()):
        return None  # idempotent duplicate
    return Decision(approver, roles, "approve", now)


def check_consumable(req: ApprovalRequest, *, now: datetime, state_version: int, content_hash: str,
                     policy_version: str) -> None:
    """Raise unless `req` may be consumed right now. Pure: doesn't consume it.

    The caller still has to hand out the actual fencing token (a plain counter in
    memory, a Postgres sequence value for the real store) and persist the
    `consumed` state; this only decides whether that's allowed.
    """
    if req.status == "consumed":
        raise AlreadyConsumed(f"{req.request_id} was consumed by {req.consumed_by} at {req.consumed_at}")
    require_open(req)
    if not req.quorum_met():
        raise QuorumNotMet(f"{req.request_id}: {len(req.approvers())}/{req.needed} approvals"
                           + (" (security required)" if req.required.security else ""))
    if now >= req.expires:
        raise Expired(f"{req.request_id}: operation must start before {req.expires.isoformat()}")
    if (state_version, content_hash, policy_version) != (req.state_version, req.content_hash,
                                                         req.policy_version):
        raise StaleApproval(f"{req.request_id}: state, content or policy changed since the request")


def _not_found(request_id: str) -> Exception:
    # Local import: factory.ports imports this module, so a module-level import
    # the other way round would be circular. By the time anything actually calls
    # into the store, both modules are fully loaded.
    from ..ports import NotFound
    return NotFound(request_id)


class ApprovalStore:
    """In-memory reference implementation. Phase 2 backs this with Postgres (same semantics)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._requests: dict[str, ApprovalRequest] = {}
        self._fencing = 0

    def _next_token_locked(self) -> int:
        self._fencing += 1
        return self._fencing

    def next_fencing_token(self) -> int:
        """A token from the same monotonic counter consume() draws from (deploys with no approval)."""
        with self._lock:
            return self._next_token_locked()

    def add(self, request: ApprovalRequest) -> ApprovalRequest:
        with self._lock:
            if request.request_id in self._requests:
                raise ApprovalError(f"duplicate request id {request.request_id}")
            self._requests[request.request_id] = request
            return request

    def _get(self, request_id: str) -> ApprovalRequest:
        try:
            return self._requests[request_id]
        except KeyError:
            raise _not_found(request_id) from None

    def get(self, request_id: str) -> ApprovalRequest:
        with self._lock:
            return self._get(request_id)

    def list_open(self, mission_id: str | None = None) -> list[ApprovalRequest]:
        with self._lock:
            return [r for r in self._requests.values()
                    if r.status == "open" and (mission_id is None or mission_id in r.mission_ids)]

    def decide(self, request_id: str, *, approver: str, roles, decision: str, content_hash: str,
               now: datetime) -> ApprovalRequest:
        with self._lock:
            req = self._get(request_id)
            d = check_decision(req, approver=approver, roles=roles, decision=decision,
                               content_hash=content_hash, now=now)
            if d is None:
                return req  # idempotent duplicate
            req.decisions.append(d)
            if d.decision in ("revise", "cancel"):
                req.status = "void"
            return req

    def consume(self, request_id: str, *, executor: str, now: datetime, state_version: int,
                content_hash: str, policy_version: str) -> int:
        """Atomically consume an approval right before the side effect. Returns a fencing token."""
        with self._lock:
            req = self._get(request_id)
            check_consumable(req, now=now, state_version=state_version, content_hash=content_hash,
                            policy_version=policy_version)
            token = self._next_token_locked()
            req.status = "consumed"
            req.consumed_by = executor
            req.consumed_at = now
            req.fencing_token = token
            return token

    def invalidate_for_artifact(self, artifact: str, reason: str) -> list[str]:
        """Rollback: every unconsumed approval for this artifact becomes invalid."""
        with self._lock:
            hit = []
            for req in self._requests.values():
                if req.artifact == artifact and req.status == "open":
                    req.status = "invalidated"
                    hit.append(req.request_id)
            return hit


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
