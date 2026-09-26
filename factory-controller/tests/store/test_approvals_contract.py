"""Contract tests for `StateStore.approvals`: same functions, both backends.

Ported from `tests/test_approvals.py` against the reference `ApprovalStore`.
`PostgresApprovalStore` must reproduce the exact same behavior, so these
tests run unmodified against `store.approvals` for both the memory and the
Postgres backend (final draft §10, §12.1, §12.2).
"""
from datetime import timedelta

import pytest

from factory.controller.approvals import (AlreadyConsumed, ApprovalRequest, Expired, IneligibleApprover,
                                          QuorumNotMet, StaleApproval, Voided)
from factory.policy import parse_requirement
from factory.ports import NotFound

HASH = "sha256:aaa"


def make(store, now, *, gate="H2", required="1", profile="standard", requester="@po", editors=(),
         artifact="sha256:digest1", request_id="REQ-1", state_version=7):
    return store.approvals.add(ApprovalRequest(
        request_id=request_id, gate=gate, mission_ids=("MIS-1",), operation_id=f"deploy:{artifact}",
        artifact=artifact, content_hash=HASH, policy_version="p-1", state_version=state_version,
        required=parse_requirement(required), risk_profile=profile, requester=requester,
        expires=now + timedelta(hours=8), editors=frozenset(editors)))


def consume(store, now, request_id="REQ-1", **kw):
    args = dict(executor="deploy-controller", now=now, state_version=7, content_hash=HASH, policy_version="p-1")
    args.update(kw)
    return store.approvals.consume(request_id, **args)


def test_single_approval_consumes_once(store, now):
    make(store, now)
    store.approvals.decide("REQ-1", approver="@tl", roles={"tech_lead"}, decision="approve",
                          content_hash=HASH, now=now)
    token = consume(store, now)
    assert token == 1
    with pytest.raises(AlreadyConsumed):
        consume(store, now)


def test_quorum_needs_distinct_people(store, now):
    make(store, now, required="2")
    store.approvals.decide("REQ-1", approver="@tl", roles={"tech_lead"}, decision="approve",
                          content_hash=HASH, now=now)
    store.approvals.decide("REQ-1", approver="@tl", roles={"tech_lead"}, decision="approve",
                          content_hash=HASH, now=now)
    with pytest.raises(QuorumNotMet):
        consume(store, now)
    store.approvals.decide("REQ-1", approver="@po", roles={"product"}, decision="approve",
                          content_hash=HASH, now=now)
    assert consume(store, now) == 1


def test_security_requirement(store, now):
    make(store, now, gate="HM", required="1+sec")
    store.approvals.decide("REQ-1", approver="@tl", roles={"tech_lead"}, decision="approve",
                          content_hash=HASH, now=now)
    with pytest.raises(QuorumNotMet, match="security"):
        consume(store, now)
    store.approvals.decide("REQ-1", approver="@sec", roles={"security"}, decision="approve",
                          content_hash=HASH, now=now)
    assert consume(store, now)


def test_revise_voids_the_round(store, now):
    make(store, now, required="2")
    store.approvals.decide("REQ-1", approver="@tl", roles={"tech_lead"}, decision="approve",
                          content_hash=HASH, now=now)
    store.approvals.decide("REQ-1", approver="@po", roles={"product"}, decision="revise",
                          content_hash=HASH, now=now)
    with pytest.raises(Voided):
        store.approvals.decide("REQ-1", approver="@sec", roles={"security"}, decision="approve",
                              content_hash=HASH, now=now)
    with pytest.raises(Voided):
        consume(store, now)


def test_decision_for_other_content_is_rejected(store, now):
    make(store, now)
    with pytest.raises(StaleApproval):
        store.approvals.decide("REQ-1", approver="@tl", roles={"tech_lead"}, decision="approve",
                              content_hash="sha256:other", now=now)


def test_editor_cannot_approve_merge(store, now):
    make(store, now, gate="HM", editors={"@dev"})
    with pytest.raises(IneligibleApprover):
        store.approvals.decide("REQ-1", approver="@dev", roles={"tech_lead"}, decision="approve",
                              content_hash=HASH, now=now)


def test_regulated_requester_cannot_approve(store, now):
    make(store, now, profile="regulated", required="2", requester="@po")
    with pytest.raises(IneligibleApprover):
        store.approvals.decide("REQ-1", approver="@po", roles={"product"}, decision="approve",
                              content_hash=HASH, now=now)


def test_standard_role_collapsing_allowed(store, now):
    make(store, now, gate="H1", requester="@lead")
    store.approvals.decide("REQ-1", approver="@lead", roles={"product", "tech_lead"}, decision="approve",
                          content_hash=HASH, now=now)
    assert consume(store, now)


def test_expiry_is_start_before(store, now):
    make(store, now)
    store.approvals.decide("REQ-1", approver="@tl", roles={"tech_lead"}, decision="approve",
                          content_hash=HASH, now=now)
    with pytest.raises(Expired):
        consume(store, now + timedelta(hours=9))


def test_defer_does_not_approve(store, now):
    make(store, now)
    store.approvals.decide("REQ-1", approver="@tl", roles={"tech_lead"}, decision="defer",
                          content_hash=HASH, now=now)
    with pytest.raises(QuorumNotMet):
        consume(store, now)


def test_list_open_filters_by_status_and_mission(store, now):
    make(store, now, request_id="REQ-1")
    make(store, now, request_id="REQ-2")
    store.approvals.decide("REQ-2", approver="@tl", roles={"tech_lead"}, decision="cancel",
                          content_hash=HASH, now=now)
    assert [r.request_id for r in store.approvals.list_open()] == ["REQ-1"]
    assert [r.request_id for r in store.approvals.list_open("MIS-1")] == ["REQ-1"]
    assert store.approvals.list_open("MIS-other") == []


def test_decide_approve_is_idempotent_for_the_same_approver(store, now):
    make(store, now, required="2")
    store.approvals.decide("REQ-1", approver="@tl", roles={"tech_lead"}, decision="approve",
                          content_hash=HASH, now=now)
    req = store.approvals.decide("REQ-1", approver="@tl", roles={"tech_lead"}, decision="approve",
                                content_hash=HASH, now=now)
    assert len(req.approvers()) == 1


def test_rollback_invalidates_open_approvals_for_the_artifact(store, now):
    make(store, now, request_id="REQ-1", artifact="sha256:digestA")
    make(store, now, request_id="REQ-2", artifact="sha256:digestA")
    make(store, now, request_id="REQ-3", artifact="sha256:digestB")
    hit = store.approvals.invalidate_for_artifact("sha256:digestA", "post-merge failure")
    assert sorted(hit) == ["REQ-1", "REQ-2"]
    with pytest.raises(Voided):
        store.approvals.decide("REQ-1", approver="@tl", roles={"tech_lead"}, decision="approve",
                              content_hash=HASH, now=now)
    # Untouched artifact keeps its open request.
    assert [r.request_id for r in store.approvals.list_open()] == ["REQ-3"]


def test_get_missing_request_raises_not_found(store):
    with pytest.raises(NotFound):
        store.approvals.get("no-such-request")


def test_decide_missing_request_raises_not_found(store, now):
    with pytest.raises(NotFound):
        store.approvals.decide("no-such-request", approver="@tl", roles={"tech_lead"},
                              decision="approve", content_hash=HASH, now=now)


def test_consume_missing_request_raises_not_found(store, now):
    with pytest.raises(NotFound):
        consume(store, now, request_id="no-such-request")


def test_auto_requirements_never_create_requests(now):
    with pytest.raises(ValueError):
        ApprovalRequest(request_id="R", gate="HM", mission_ids=("M",), operation_id="op", artifact=None,
                        content_hash=HASH, policy_version="p", state_version=0,
                        required=parse_requirement("auto"), risk_profile="standard", requester="@x",
                        expires=now)
