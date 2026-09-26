"""Contract tests for the webhook inbox, once-only keys and fencing tokens.

Same test functions run against both backends via the `store`/`clocked_store`
fixtures (final draft §10; red team #3 H1/H2/M4). A couple of tests are
Postgres-only because they specifically exercise `FOR UPDATE SKIP LOCKED`
across real concurrent connections, which the in-memory backend can't
meaningfully distinguish from "the lock serialized everything".
"""
import threading
from datetime import timedelta

import pytest

from factory.controller.approvals import ApprovalRequest
from factory.policy import parse_requirement

HASH = "sha256:webhooks"


def make_request(store, now, *, gate="H2", required="1", risk_profile="standard", requester="@po",
                 editors=(), eligible=None, artifact="sha256:digest1", request_id="REQ-1",
                 state_version=0):
    return store.approvals.add(ApprovalRequest(
        request_id=request_id, gate=gate, mission_ids=("MIS-1",), operation_id=f"deploy:{artifact}",
        artifact=artifact, content_hash=HASH, policy_version="p-1", state_version=state_version,
        required=parse_requirement(required), risk_profile=risk_profile, requester=requester,
        expires=now + timedelta(hours=8), editors=frozenset(editors), eligible=eligible))


# ── webhook inbox ────────────────────────────────────────────────────────
def test_enqueue_webhook_returns_true_then_false_for_a_duplicate(store):
    assert store.enqueue_webhook("d1", "push", {"a": 1}) is True
    assert store.enqueue_webhook("d1", "push", {"a": 1}) is False


def test_claim_webhooks_returns_oldest_first_and_leases_them(store):
    store.enqueue_webhook("d1", "push", {"n": 1})
    store.enqueue_webhook("d2", "push", {"n": 2})
    claimed = store.claim_webhooks()
    assert [c["delivery_id"] for c in claimed] == ["d1", "d2"]
    assert claimed[0]["event"] == "push"
    assert claimed[0]["payload"] == {"n": 1}
    # Leased: a second claim right away gets nothing.
    assert store.claim_webhooks() == []


def test_claim_webhooks_respects_the_limit(store):
    for i in range(3):
        store.enqueue_webhook(f"d{i}", "push", {})
    assert len(store.claim_webhooks(limit=2)) == 2


def test_acked_delivery_is_never_reclaimable(clocked_store):
    store, clock = clocked_store
    store.enqueue_webhook("d1", "push", {})
    store.claim_webhooks(lease_seconds=1)
    store.ack_webhook("d1")
    clock.advance(hours=1)  # long past any lease
    assert store.claim_webhooks() == []


def test_expired_lease_is_reclaimable(clocked_store):
    store, clock = clocked_store
    store.enqueue_webhook("d1", "push", {})
    first = store.claim_webhooks(lease_seconds=60)
    assert [c["delivery_id"] for c in first] == ["d1"]
    clock.advance(seconds=59)
    assert store.claim_webhooks() == []  # lease still live
    clock.advance(seconds=2)
    second = store.claim_webhooks()
    assert [c["delivery_id"] for c in second] == ["d1"]  # lease expired: reclaimable


def test_ack_unknown_delivery_raises_not_found(store):
    from factory.ports import NotFound
    with pytest.raises(NotFound):
        store.ack_webhook("no-such-delivery")


@pytest.mark.postgres
def test_concurrent_claim_never_hands_out_the_same_delivery_twice(postgres_only_store):
    """`FOR UPDATE SKIP LOCKED` must make two concurrent claimers partition the queue."""
    store = postgres_only_store
    for i in range(20):
        store.enqueue_webhook(f"d{i}", "push", {})

    results: list[list[dict]] = [[], []]
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def worker(slot: int) -> None:
        try:
            barrier.wait(timeout=5)
            results[slot] = store.claim_webhooks(limit=15)
        except Exception as exc:  # noqa: BLE001 - surfaced via `errors`
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    ids_a = [c["delivery_id"] for c in results[0]]
    ids_b = [c["delivery_id"] for c in results[1]]
    assert set(ids_a).isdisjoint(ids_b)
    assert len(ids_a) + len(ids_b) == 20


# ── once-only keys ───────────────────────────────────────────────────────
def test_use_once_returns_true_then_false(store):
    assert store.use_once("jti-1") is True
    assert store.use_once("jti-1") is False
    assert store.use_once("jti-2") is True


def test_use_once_under_a_race_admits_exactly_one_winner(store):
    outcomes: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait(timeout=5)
        won = store.use_once("shared-key")
        with lock:
            outcomes.append(won)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(outcomes) == 8
    assert outcomes.count(True) == 1


# ── fencing tokens ───────────────────────────────────────────────────────
def test_next_fencing_token_is_strictly_increasing(store):
    tokens = [store.next_fencing_token() for _ in range(5)]
    assert tokens == sorted(tokens)
    assert len(set(tokens)) == len(tokens)


def test_next_fencing_token_interleaves_with_consume_without_duplicates(store, now):
    make_request(store, now)
    store.approvals.decide("REQ-1", approver="@tl", roles={"tech_lead"}, decision="approve",
                          content_hash=HASH, now=now)
    before = store.next_fencing_token()
    consumed = store.approvals.consume("REQ-1", executor="deploy-controller", now=now, state_version=0,
                                       content_hash=HASH, policy_version="p-1")
    after = store.next_fencing_token()
    tokens = [before, consumed, after]
    assert tokens == sorted(tokens)
    assert len(set(tokens)) == 3


# ── eligible approvers (round-trip through the store) ───────────────────
def test_eligible_round_trips_through_the_store(store, now):
    req = make_request(store, now, gate="HM", required="1+sec",
                       eligible={"@tl": frozenset({"tech_lead"}), "@sec": frozenset({"security"})})
    got = store.approvals.get(req.request_id)
    assert got.eligible == {"@tl": frozenset({"tech_lead"}), "@sec": frozenset({"security"})}


def test_eligible_enforcement_works_through_the_store(store, now):
    from factory.controller.approvals import IneligibleApprover

    make_request(store, now, gate="HM", required="1+sec",
                eligible={"@tl": frozenset({"tech_lead"}), "@sec": frozenset({"security"})})
    with pytest.raises(IneligibleApprover):
        store.approvals.decide("REQ-1", approver="@outsider", roles={"security"}, decision="approve",
                              content_hash=HASH, now=now)
    # @tl claims security in the token, but config says tech_lead only.
    store.approvals.decide("REQ-1", approver="@tl", roles={"security"}, decision="approve",
                          content_hash=HASH, now=now)
    got = store.approvals.get("REQ-1")
    [decision] = got.approvers()
    assert decision.roles == frozenset({"tech_lead"})


def test_eligible_none_is_unrestricted_through_the_store(store, now):
    make_request(store, now, eligible=None)
    store.approvals.decide("REQ-1", approver="@anyone", roles={"tech_lead"}, decision="approve",
                          content_hash=HASH, now=now)
    got = store.approvals.get("REQ-1")
    assert len(got.approvers()) == 1
