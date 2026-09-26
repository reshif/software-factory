from datetime import timedelta

import pytest

from factory.controller.approvals import (AlreadyConsumed, ApprovalRequest, ApprovalStore, Expired,
                                          FencedTarget, IneligibleApprover, QuorumNotMet, StaleApproval,
                                          Voided)
from factory.policy import parse_requirement

HASH = "sha256:aaa"


def make(store, now, *, gate="H2", required="1", profile="standard", requester="@po", editors=(),
         artifact="sha256:digest1", request_id="REQ-1", state_version=7):
    return store.add(ApprovalRequest(
        request_id=request_id, gate=gate, mission_ids=("MIS-1",), operation_id=f"deploy:{artifact}",
        artifact=artifact, content_hash=HASH, policy_version="p-1", state_version=state_version,
        required=parse_requirement(required), risk_profile=profile, requester=requester,
        expires=now + timedelta(hours=8), editors=frozenset(editors)))


def consume(store, now, request_id="REQ-1", **kw):
    args = dict(executor="deploy-controller", now=now, state_version=7, content_hash=HASH, policy_version="p-1")
    args.update(kw)
    return store.consume(request_id, **args)


def test_single_approval_consumes_once(now):
    store = ApprovalStore()
    make(store, now)
    store.decide("REQ-1", approver="@tl", roles={"tech_lead"}, decision="approve", content_hash=HASH, now=now)
    token = consume(store, now)
    assert token == 1
    with pytest.raises(AlreadyConsumed):
        consume(store, now)


def test_quorum_needs_distinct_people(now):
    store = ApprovalStore()
    make(store, now, required="2")
    store.decide("REQ-1", approver="@tl", roles={"tech_lead"}, decision="approve", content_hash=HASH, now=now)
    store.decide("REQ-1", approver="@tl", roles={"tech_lead"}, decision="approve", content_hash=HASH, now=now)
    with pytest.raises(QuorumNotMet):
        consume(store, now)
    store.decide("REQ-1", approver="@po", roles={"product"}, decision="approve", content_hash=HASH, now=now)
    assert consume(store, now) == 1


def test_security_requirement(now):
    store = ApprovalStore()
    make(store, now, gate="HM", required="1+sec")
    store.decide("REQ-1", approver="@tl", roles={"tech_lead"}, decision="approve", content_hash=HASH, now=now)
    with pytest.raises(QuorumNotMet, match="security"):
        consume(store, now)
    store.decide("REQ-1", approver="@sec", roles={"security"}, decision="approve", content_hash=HASH, now=now)
    assert consume(store, now)


def test_revise_voids_the_round(now):
    store = ApprovalStore()
    make(store, now, required="2")
    store.decide("REQ-1", approver="@tl", roles={"tech_lead"}, decision="approve", content_hash=HASH, now=now)
    store.decide("REQ-1", approver="@po", roles={"product"}, decision="revise", content_hash=HASH, now=now)
    with pytest.raises(Voided):
        store.decide("REQ-1", approver="@sec", roles={"security"}, decision="approve", content_hash=HASH, now=now)
    with pytest.raises(Voided):
        consume(store, now)


def test_decision_for_other_content_is_rejected(now):
    store = ApprovalStore()
    make(store, now)
    with pytest.raises(StaleApproval):
        store.decide("REQ-1", approver="@tl", roles={"tech_lead"}, decision="approve",
                     content_hash="sha256:other", now=now)


def test_editor_cannot_approve_merge(now):
    store = ApprovalStore()
    make(store, now, gate="HM", editors={"@dev"})
    with pytest.raises(IneligibleApprover):
        store.decide("REQ-1", approver="@dev", roles={"tech_lead"}, decision="approve", content_hash=HASH, now=now)


def test_regulated_requester_cannot_approve(now):
    store = ApprovalStore()
    make(store, now, profile="regulated", required="2", requester="@po")
    with pytest.raises(IneligibleApprover):
        store.decide("REQ-1", approver="@po", roles={"product"}, decision="approve", content_hash=HASH, now=now)


def test_standard_role_collapsing_allowed(now):
    store = ApprovalStore()
    make(store, now, gate="H1", requester="@lead")
    store.decide("REQ-1", approver="@lead", roles={"product", "tech_lead"}, decision="approve",
                 content_hash=HASH, now=now)
    assert consume(store, now)


def test_expiry_is_start_before(now):
    store = ApprovalStore()
    make(store, now)
    store.decide("REQ-1", approver="@tl", roles={"tech_lead"}, decision="approve", content_hash=HASH, now=now)
    with pytest.raises(Expired):
        consume(store, now + timedelta(hours=9))


def test_defer_does_not_approve(now):
    store = ApprovalStore()
    make(store, now)
    store.decide("REQ-1", approver="@tl", roles={"tech_lead"}, decision="defer", content_hash=HASH, now=now)
    with pytest.raises(QuorumNotMet):
        consume(store, now)


def test_list_open_filters_by_status_and_mission(now):
    store = ApprovalStore()
    make(store, now, request_id="REQ-1")
    make(store, now, request_id="REQ-2")
    store.decide("REQ-2", approver="@tl", roles={"tech_lead"}, decision="cancel", content_hash=HASH, now=now)
    assert [r.request_id for r in store.list_open()] == ["REQ-1"]
    assert [r.request_id for r in store.list_open("MIS-1")] == ["REQ-1"]
    assert store.list_open("MIS-other") == []


def test_fenced_target_rejects_stale_token():
    target = FencedTarget()
    target.apply(2, "deploy A")
    with pytest.raises(StaleApproval):
        target.apply(1, "deploy B")
    assert target.effects == [(2, "deploy A")]


def test_auto_requirements_never_create_requests(now):
    with pytest.raises(ValueError):
        ApprovalRequest(request_id="R", gate="HM", mission_ids=("M",), operation_id="op", artifact=None,
                        content_hash=HASH, policy_version="p", state_version=0,
                        required=parse_requirement("auto"), risk_profile="standard", requester="@x",
                        expires=now)
