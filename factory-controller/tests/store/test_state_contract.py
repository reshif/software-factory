"""Contract tests for the mission/task/evidence/packet/event side of `StateStore`.

Same test functions run against both backends via the `store` fixture
(final draft §10, §12.1, §12.2).
"""
import pytest

from factory.models import CheckResult, DecisionPacket, EvidenceBundle, MissionRecord, TaskRecord
from factory.ports import ConcurrentUpdate, NotFound


def make_mission(**overrides) -> MissionRecord:
    fields = dict(mission_id="MIS-1", product="pilot-api", repo="org/pilot-api",
                 work_item_id="org/pilot-api#42", lane="patch", risk_profile="standard",
                 autonomy_level="L3", kit_version="factory-kit@1.0.0", policy_version="p-1")
    fields.update(overrides)
    return MissionRecord(**fields)


# ── missions ─────────────────────────────────────────────────────────────
def test_create_and_get_mission(store):
    store.create_mission(make_mission())
    got = store.get_mission("MIS-1")
    assert got.mission_id == "MIS-1"
    assert got.state == "NEW"
    assert got.state_version == 0


def test_get_missing_mission_raises_not_found(store):
    with pytest.raises(NotFound):
        store.get_mission("no-such-mission")


def test_create_duplicate_mission_raises(store):
    store.create_mission(make_mission())
    with pytest.raises(Exception):
        store.create_mission(make_mission())


def test_update_mission_is_a_cas_and_bumps_version(store):
    store.create_mission(make_mission())
    updated = store.update_mission("MIS-1", expected_version=0, state="ACTIVE", spent_usd=1.5)
    assert updated.state == "ACTIVE"
    assert updated.spent_usd == 1.5
    assert updated.state_version == 1


def test_update_mission_stale_version_raises_concurrent_update(store):
    store.create_mission(make_mission())
    store.update_mission("MIS-1", expected_version=0, state="ACTIVE")
    with pytest.raises(ConcurrentUpdate):
        store.update_mission("MIS-1", expected_version=0, state="ARCHIVED")


def test_update_missing_mission_raises_not_found(store):
    with pytest.raises(NotFound):
        store.update_mission("no-such-mission", expected_version=0, state="ACTIVE")


def test_update_mission_editors_round_trips(store):
    store.create_mission(make_mission())
    updated = store.update_mission("MIS-1", expected_version=0, editors=("@dev1", "@dev2"))
    assert set(updated.editors) == {"@dev1", "@dev2"}


def test_list_missions_filters_by_state_and_product(store):
    store.create_mission(make_mission(mission_id="MIS-1", product="pilot-api"))
    store.create_mission(make_mission(mission_id="MIS-2", product="other-api", work_item_id="o/r#2"))
    store.update_mission("MIS-2", expected_version=0, state="ACTIVE")
    assert {m.mission_id for m in store.list_missions()} == {"MIS-1", "MIS-2"}
    assert [m.mission_id for m in store.list_missions(state="ACTIVE")] == ["MIS-2"]
    assert [m.mission_id for m in store.list_missions(product="pilot-api")] == ["MIS-1"]
    assert store.list_missions(product="nope") == []


def test_find_mission_by_work_item(store):
    store.create_mission(make_mission())
    found = store.find_mission_by_work_item("org/pilot-api#42")
    assert found is not None
    assert found.mission_id == "MIS-1"
    assert store.find_mission_by_work_item("no/such#0") is None


# ── tasks ────────────────────────────────────────────────────────────────
def test_create_get_and_update_task(store):
    store.create_mission(make_mission())
    store.create_task(TaskRecord(task_id="T-1", mission_id="MIS-1", contract={"objective": "x"}))
    got = store.get_task("T-1")
    assert got.state == "WAITING_DEPS"
    assert got.contract == {"objective": "x"}
    updated = store.update_task("T-1", state="RUNNING", session_id="sess-1")
    assert updated.state == "RUNNING"
    assert updated.session_id == "sess-1"


def test_get_missing_task_raises_not_found(store):
    with pytest.raises(NotFound):
        store.get_task("no-such-task")


def test_update_missing_task_raises_not_found(store):
    with pytest.raises(NotFound):
        store.update_task("no-such-task", state="RUNNING")


def test_list_tasks_filters_by_mission(store):
    store.create_mission(make_mission())
    store.create_mission(make_mission(mission_id="MIS-2", work_item_id="org/pilot-api#43"))
    store.create_task(TaskRecord(task_id="T-1", mission_id="MIS-1", contract={}))
    store.create_task(TaskRecord(task_id="T-2", mission_id="MIS-1", contract={}, depends_on=("T-1",)))
    store.create_task(TaskRecord(task_id="T-3", mission_id="MIS-2", contract={}))
    tasks = store.list_tasks("MIS-1")
    assert [t.task_id for t in tasks] == ["T-1", "T-2"]
    assert tasks[1].depends_on == ("T-1",)


# ── evidence & packets ───────────────────────────────────────────────────
def make_evidence(**overrides) -> EvidenceBundle:
    fields = dict(mission_id="MIS-1", revision="r1", content_hash="sha256:aaa", diff_ref="evidence/d1.diff",
                 checks={"lint": CheckResult(name="lint", conclusion="success", detail="ok")},
                 action_class="AC3", rule_fired="AC3.h1.standing", task_ids=("T-1",), holdout=(8, 10),
                 blast_radius=("src/billing/**",), rollback_plan="revert commit", rollback_tested=True,
                 cost_usd=1.23, tokens=4000, ci_minutes=2.5, untrusted_inputs=("issue body",),
                 agent_versions={"architect": "opus-5-5"})
    fields.update(overrides)
    return EvidenceBundle(**fields)


def test_save_and_get_evidence_round_trips(store):
    store.create_mission(make_mission())
    bundle = make_evidence()
    store.save_evidence(bundle)
    got = store.get_evidence("MIS-1")
    assert got.revision == "r1"
    assert got.checks["lint"] == CheckResult(name="lint", conclusion="success", detail="ok")
    assert got.holdout == (8, 10)
    assert got.task_ids == ("T-1",)
    assert got.agent_versions == {"architect": "opus-5-5"}
    assert store.get_evidence("MIS-1", revision="r1").content_hash == "sha256:aaa"
    assert store.get_evidence("MIS-1", revision="no-such-revision") is None


def test_get_evidence_without_revision_returns_latest(store):
    store.create_mission(make_mission())
    store.save_evidence(make_evidence(revision="r1"))
    store.save_evidence(make_evidence(revision="r2", content_hash="sha256:bbb"))
    latest = store.get_evidence("MIS-1")
    assert latest.revision == "r2"


def test_get_evidence_missing_mission_returns_none(store):
    assert store.get_evidence("no-such-mission") is None


def test_save_evidence_upserts_same_revision(store):
    store.create_mission(make_mission())
    store.save_evidence(make_evidence(cost_usd=1.0))
    store.save_evidence(make_evidence(cost_usd=2.0))
    assert store.get_evidence("MIS-1", revision="r1").cost_usd == 2.0


def test_save_and_get_packet_with_nested_evidence(store, now):
    store.create_mission(make_mission())
    bundle = make_evidence()
    packet = DecisionPacket(request_id="REQ-1", gate="H1", mission_ids=("MIS-1",), title="Add rate limiting",
                            recommendation="APPROVE: safe, additive change", summary="…", required="1",
                            expires=now, content_hash="sha256:aaa", raw_diff="diff --git a/x b/x",
                            alternatives=("do nothing",), evidence=bundle, recovery_plan="revert",
                            cost_usd=1.23, untrusted_inputs=("issue body",), links={"pr": "https://…"})
    store.save_packet(packet)
    got = store.get_packet("REQ-1")
    assert got.title == "Add rate limiting"
    assert got.evidence is not None
    assert got.evidence.mission_id == "MIS-1"
    assert got.evidence.checks["lint"].conclusion == "success"
    assert got.links == {"pr": "https://…"}


def test_save_and_get_packet_without_evidence(store, now):
    packet = DecisionPacket(request_id="REQ-2", gate="H2", mission_ids=("MIS-1",), title="Deploy",
                            recommendation="APPROVE", summary="…", required="1", expires=now,
                            content_hash="sha256:bbb")
    store.save_packet(packet)
    got = store.get_packet("REQ-2")
    assert got.evidence is None


def test_get_missing_packet_raises_not_found(store):
    with pytest.raises(NotFound):
        store.get_packet("no-such-request")


# ── events ───────────────────────────────────────────────────────────────
def test_append_and_list_events_are_ordered(store):
    store.create_mission(make_mission())
    store.append_event("MIS-1", "mission_created", {"lane": "patch"})
    store.append_event("MIS-1", "task_dispatched", {"task_id": "T-1"})
    events = store.list_events("MIS-1")
    assert [e["kind"] for e in events] == ["mission_created", "task_dispatched"]
    assert events[0]["payload"] == {"lane": "patch"}
    assert events[1]["payload"] == {"task_id": "T-1"}


def test_list_events_filters_by_mission(store):
    store.append_event("MIS-1", "a", {})
    store.append_event("MIS-2", "b", {})
    assert [e["kind"] for e in store.list_events("MIS-1")] == ["a"]


def test_list_events_for_unknown_mission_is_empty(store):
    assert store.list_events("no-such-mission") == []
