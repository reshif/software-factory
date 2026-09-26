"""Phase 1 acceptance walkthroughs (final draft §19.3) as executable tests.

Scenarios 11 and 13 are enforced by infrastructure (sandbox network, repo isolation)
and are verified in Phase 2; they are skipped here with the reason recorded.
"""
from datetime import date, timedelta

import pytest

from factory.controller.approvals import (AlreadyConsumed, ApprovalRequest, ApprovalStore, Expired,
                                          FencedTarget, StaleApproval, Voided)
from factory.controller.coverage import StandingMandate, check_diff, check_request
from factory.controller.gate_resolver import release_requirement, resolve
from factory.controller.intents import IntentLog, execute_once
from factory.controller.mission_fsm import Mission
from factory.policy.action_classes import FileChange, classify

HASH = "sha256:content-v1"


def request(store, now, *, gate, required, request_id, artifact="sha256:d1", state_version=1,
            content_hash=HASH, profile="standard", requester="@po"):
    return store.add(ApprovalRequest(
        request_id=request_id, gate=gate, mission_ids=("MIS-1",), operation_id=f"{gate}:{artifact}",
        artifact=artifact, content_hash=content_hash, policy_version="p-2026.10.1",
        state_version=state_version, required=required, risk_profile=profile, requester=requester,
        expires=now + timedelta(hours=8)))


def approve(store, request_id, now, approver="@tl", roles=("tech_lead",), content_hash=HASH):
    store.decide(request_id, approver=approver, roles=set(roles), decision="approve",
                 content_hash=content_hash, now=now)


def consume(store, request_id, now, state_version=1, content_hash=HASH):
    return store.consume(request_id, executor="controller", now=now, state_version=state_version,
                         content_hash=content_hash, policy_version="p-2026.10.1")


def patch_mandate():
    return StandingMandate(mandate_id="SM-pilot-patch", action_classes=frozenset({"AC1", "AC2", "AC3"}),
                           paths=("src/**", "docs/**", "tests/**", "README.md"), max_diff_lines=150,
                           expires=date(2027, 1, 31), labels_any=("factory:patch",),
                           exclude_paths=("src/auth/**",))


# 1 ────────────────────────────────────────────────────────────────────────────────
def test_01_happy_path_feature(policy, now):
    changes = [FileChange("src/billing/rate_limit.py", "added", 180, 0),
               FileChange("tests/billing/test_rate_limit.py", "added", 90, 0)]
    cls = classify(changes, policy.floor)
    gates = resolve(policy, action_class=cls.action_class, risk_profile="standard")
    assert cls.action_class == "AC4"
    assert (str(gates.h1), str(gates.hm), str(gates.h2)) == ("1", "1", "1")

    store, m = ApprovalStore(), Mission("MIS-1")
    m.fire("not_covered")
    m.fire("packet_ready")
    request(store, now, gate="H1", required=gates.h1, request_id="REQ-H1")
    approve(store, "REQ-H1", now, approver="@po", roles=("product",))
    consume(store, "REQ-H1", now)
    for event in ("approved", "capacity_ok", "tasks_done", "hm_required"):
        m.fire(event)
    request(store, now, gate="HM", required=gates.hm, request_id="REQ-HM")
    approve(store, "REQ-HM", now)
    consume(store, "REQ-HM", now)
    for event in ("approved", "evidence_complete", "h2_required"):
        m.fire(event)
    request(store, now, gate="H2", required=release_requirement([gates]), request_id="REQ-H2")
    approve(store, "REQ-H2", now)
    consume(store, "REQ-H2", now)
    for event in ("approval_consumed", "deployed", "window_healthy"):
        m.fire(event)
    assert m.state == "DELIVERED"


# 2 ────────────────────────────────────────────────────────────────────────────────
def test_02_patch_diff_exceeds_coverage_goes_to_h1(policy):
    today = date(2026, 10, 1)
    mandate = patch_mandate()
    assert check_request(mandate, labels=["factory:patch"], suggested_class="AC3", today=today).covered

    m = Mission("MIS-2")
    m.fire("covered")
    m.fire("capacity_ok")
    changes = [FileChange("src/app/big_refactor.py", "modified", 260, 90)]
    result = check_diff(mandate, classification=classify(changes, policy.floor), changes=changes, today=today)
    assert not result.covered
    assert m.fire("coverage_exceeded") == "DISCOVERING"
    assert m.fire("packet_ready") == "AWAITING_H1"


# 3 ────────────────────────────────────────────────────────────────────────────────
def test_03_h1_revise_then_decline_archives():
    m = Mission("MIS-3")
    for event in ("not_covered", "packet_ready", "revise", "packet_ready", "decline"):
        m.fire(event)
    assert m.state == "ARCHIVED"


# 4 ────────────────────────────────────────────────────────────────────────────────
def test_04_stale_approval_is_rejected_and_new_round_works(policy, now):
    store = ApprovalStore()
    req = resolve(policy, action_class="AC4", risk_profile="standard").h2
    request(store, now, gate="H2", required=req, request_id="REQ-A", state_version=10)
    approve(store, "REQ-A", now)
    # base moved: state version is now 11 and content changed
    with pytest.raises(StaleApproval):
        consume(store, "REQ-A", now, state_version=11, content_hash="sha256:content-v2")
    request(store, now, gate="H2", required=req, request_id="REQ-B", state_version=11,
            content_hash="sha256:content-v2")
    approve(store, "REQ-B", now, content_hash="sha256:content-v2")
    assert consume(store, "REQ-B", now, state_version=11, content_hash="sha256:content-v2")


# 5 ────────────────────────────────────────────────────────────────────────────────
def test_05_replay_after_rollback_is_rejected(policy, now):
    store, target = ApprovalStore(), FencedTarget()
    req = resolve(policy, action_class="AC4", risk_profile="standard").h2
    request(store, now, gate="H2", required=req, request_id="REQ-DEPLOY", artifact="sha256:bad")
    request(store, now, gate="H2", required=req, request_id="REQ-PENDING", artifact="sha256:bad")
    approve(store, "REQ-DEPLOY", now)
    approve(store, "REQ-PENDING", now)
    target.apply(consume(store, "REQ-DEPLOY", now), "deploy sha256:bad")

    invalidated = store.invalidate_for_artifact("sha256:bad", reason="rollback")
    assert invalidated == ["REQ-PENDING"]
    with pytest.raises(AlreadyConsumed):
        consume(store, "REQ-DEPLOY", now)
    with pytest.raises(Voided):
        consume(store, "REQ-PENDING", now)
    assert target.effects == [(1, "deploy sha256:bad")]


# 6 ────────────────────────────────────────────────────────────────────────────────
def test_06_repair_budget_exhausted_goes_to_hx():
    m = Mission("MIS-6")
    for event in ("covered", "capacity_ok"):
        m.fire(event)
    assert m.fire("boundary") == "AWAITING_HX"
    assert not m.worker_allowed
    assert m.fire("approved") == "ACTIVE"


# 7 ────────────────────────────────────────────────────────────────────────────────
def test_07_crash_between_intent_and_receipt_has_no_duplicate_effect():
    log, world = IntentLog(), []

    def deploy_and_crash():
        world.append("deploy sha256:d1")
        raise RuntimeError("controller crashed before writing the receipt")

    with pytest.raises(RuntimeError):
        execute_once(log, "deploy:sha256:d1:prod", action=deploy_and_crash, probe=lambda: None)
    receipt = execute_once(log, "deploy:sha256:d1:prod", action=lambda: world.append("again") or "r",
                           probe=lambda: "already live" if world else None)
    assert receipt == "already live"
    assert world == ["deploy sha256:d1"]


# 8 ────────────────────────────────────────────────────────────────────────────────
def test_08_approval_timeout_never_approves(policy, now):
    store, m = ApprovalStore(), Mission("MIS-8", state="AWAITING_H2")
    req = resolve(policy, action_class="AC4", risk_profile="standard").h2
    request(store, now, gate="H2", required=req, request_id="REQ-T")
    late = now + timedelta(hours=9)
    with pytest.raises(Expired):
        approve(store, "REQ-T", late)
    assert m.fire("timeout") == "HELD"
    assert m.fire("resume") == "AWAITING_H2"
    assert store.get("REQ-T").approvers() == []


# 9 ────────────────────────────────────────────────────────────────────────────────
def test_09_post_merge_failure_auto_reverts():
    m = Mission("MIS-9", state="MERGED")
    assert m.fire("post_merge_failure") == "REVERTED"
    assert m.fire("repair") == "ACTIVE"


# 10 ───────────────────────────────────────────────────────────────────────────────
def test_10_production_regression_recovers_or_escalates():
    recovered = Mission("MIS-10a", state="OBSERVING")
    recovered.fire("regression")
    assert recovered.fire("recovered") == "ACTIVE"
    escalated = Mission("MIS-10b", state="OBSERVING")
    escalated.fire("regression")
    assert escalated.fire("outside_authority") == "AWAITING_HX"


# 11 ───────────────────────────────────────────────────────────────────────────────
@pytest.mark.skip(reason="Infrastructure control (sandbox without egress, controller-generated PR text); "
                         "verified by the Phase 2 red-team walkthrough")
def test_11_injection_in_issue_body_is_contained():
    pass


# 12 ───────────────────────────────────────────────────────────────────────────────
def test_12_workflow_edit_is_rejected_and_conftest_edit_needs_security(policy):
    workflow = classify([FileChange(".github/workflows/ci.yml", "modified", 2, 1)], policy.floor)
    assert workflow.action_class == "AC8"
    assert str(resolve(policy, action_class="AC8", risk_profile="experimental").hm) == "blocked"

    conftest = classify([FileChange("tests/conftest.py", "modified", 4, 0)], policy.floor)
    gates = resolve(policy, action_class=conftest.action_class, risk_profile="standard",
                    protected_touched=bool(conftest.protected_touched))
    assert conftest.action_class == "AC6"
    assert gates.h1.security and gates.hm.security


# 13 ───────────────────────────────────────────────────────────────────────────────
@pytest.mark.skip(reason="Infrastructure control (holdouts in a separate repo and runner identity); "
                         "verified in Phase 2")
def test_13_holdouts_unreachable_from_sandbox():
    pass
