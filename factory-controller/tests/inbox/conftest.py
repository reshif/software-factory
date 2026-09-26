from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from factory.clock import FakeClock
from factory.controller.approvals import ApprovalRequest
from factory.inbox.router import build_inbox_router
from factory.inbox.signing import TokenSigner
from factory.models import CheckResult, DecisionPacket, EvidenceBundle
from factory.policy import parse_requirement
from factory.store.memory import MemoryStateStore

CONTENT_HASH = "sha256:packet-content"
SIGNING_SECRET = "test-signing-secret"


class _MemoryStoreWithUseOnce:
    """`MemoryStateStore`, plus the `use_once` the `StateStore` port now declares.

    B1 is adding `use_once` (an atomic check-and-set: True the first time a key is
    seen, False ever after) to `MemoryStateStore`/`PostgresStateStore` in parallel;
    it isn't there yet. The inbox router is written straight against the port
    (`store.use_once(...)`), so this test-only shim supplies a correct, thread-safe
    implementation of just that one method and delegates everything else to a real
    `MemoryStateStore`. Once B1 lands `use_once` on `MemoryStateStore` itself, this
    class can be deleted and `store` can go back to being a bare `MemoryStateStore()`
    — nothing in `factory.inbox` would need to change.
    """

    def __init__(self) -> None:
        self._inner = MemoryStateStore()
        self._lock = threading.Lock()
        self._used_once: set[str] = set()

    def use_once(self, key: str) -> bool:
        with self._lock:
            if key in self._used_once:
                return False
            self._used_once.add(key)
            return True

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.fixture
def clock(now):
    return FakeClock(now)


@pytest.fixture
def signer():
    return TokenSigner(SIGNING_SECRET)


@pytest.fixture
def store():
    return _MemoryStoreWithUseOnce()


@pytest.fixture
def decide(store, clock):
    """The callable the router is built with: store.approvals.decide plus the clock's `now`.

    In the real pipeline (B7) this wrapper would also fan out side effects (voiding
    notifications, waking dependent tasks); the router doesn't need any of that to be
    correct, so the tests exercise the store's own semantics directly.
    """
    def _decide(request_id: str, approver: str, roles, decision: str, content_hash: str):
        return store.approvals.decide(request_id, approver=approver, roles=roles, decision=decision,
                                      content_hash=content_hash, now=clock.now())
    return _decide


@pytest.fixture
def app(store, decide, signer, clock):
    application = FastAPI()
    application.include_router(build_inbox_router(store, decide, signer, clock))
    return application


@pytest.fixture
def client(app):
    return TestClient(app)


def make_approval(store, now, *, request_id="REQ-1", gate="HM", required="1", requester="@po",
                  editors=(), artifact="sha256:artifact-1", state_version=3,
                  expires_in=timedelta(hours=8), eligible: dict | None = None) -> ApprovalRequest:
    request = ApprovalRequest(
        request_id=request_id, gate=gate, mission_ids=("MIS-1",), operation_id=f"merge:{artifact}",
        artifact=artifact, content_hash=CONTENT_HASH, policy_version="p-1", state_version=state_version,
        required=parse_requirement(required), risk_profile="standard", requester=requester,
        editors=frozenset(editors), expires=now + expires_in, eligible=eligible,
    )
    return store.approvals.add(request)


def make_packet(request: ApprovalRequest, *, title="Fix the flaky retry test") -> DecisionPacket:
    evidence = EvidenceBundle(
        mission_id="MIS-1", revision="r1", content_hash=request.content_hash,
        diff_ref="evidence/MIS-1/r1.diff",
        checks={"unit": CheckResult("unit", "success", detail="42 passed")},
        action_class="AC4", rule_fired="AC4 -> HM(1)", holdout=(18, 20),
    )
    return DecisionPacket(
        request_id=request.request_id, gate=request.gate, mission_ids=request.mission_ids,
        title=title,
        recommendation="APPROVE: tests pass and the diff is small",
        summary="Retries a flaky assertion in test_widget.py.",
        required=str(request.required), expires=request.expires, content_hash=request.content_hash,
        raw_diff="--- a/widget.py\n+++ b/widget.py\n@@\n-assert x == 1\n+assert x == 1  # retry\n",
        alternatives=("Do nothing and quarantine the test",),
        evidence=evidence, recovery_plan="git revert <sha>",
        cost_usd=1.23, untrusted_inputs=("issue #42 body",),
    )


def issue_token(signer, now, *, approver="@tl", roles=("tech_lead",), request_id="REQ-1",
                expires_in=timedelta(hours=8)):
    return signer.issue(approver=approver, roles=roles, expires=now + expires_in, request_id=request_id)
