"""telemetry.metrics, exercised through the real `MemoryStateStore` (build spec C7).

`MemoryStateStore.append_event` always stamps events with the real wall clock
(`datetime.now(timezone.utc)`) and takes no injectable clock -- it's B1's module, out
of this module's ownership to change. `add_event()` below calls the real
`append_event` (so every test genuinely exercises the real nested `{"kind", "payload",
"at"}` shape it produces) and then overwrites just the `"at"` field on the dict
`list_events` hands back -- the *same* dict object the store holds internally, not a
copy -- so tests can still pin exact, reproducible durations instead of depending on
however long the test happens to take to run.
"""
from datetime import datetime, timedelta, timezone

import pytest

from factory.store.memory import MemoryStateStore
from factory.telemetry.metrics import compute_dora_metrics, from_store, render_prometheus

T0 = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)


def add_event(store: MemoryStateStore, mission_id: str, kind: str, at: datetime, **payload) -> None:
    store.append_event(mission_id, kind, payload)
    store.list_events(mission_id)[-1]["at"] = at


def report(store: MemoryStateStore, mission_ids: list[str]):
    return from_store(store, mission_ids)


def test_append_event_gives_the_nested_payload_shape_metrics_reads():
    store = MemoryStateStore()
    add_event(store, "MIS-1", "deployed", T0, environment="production", artifact="sha256:x")
    raw = store.list_events("MIS-1")[0]
    assert raw["kind"] == "deployed"
    assert raw["payload"] == {"environment": "production", "artifact": "sha256:x"}
    assert "environment" not in raw  # confirms it's nested, not flattened


def test_change_lead_time_from_first_push_to_first_production_deploy():
    store = MemoryStateStore()
    add_event(store, "MIS-1", "revision_pushed", T0, sha="aaa")
    add_event(store, "MIS-1", "deployed", T0 + timedelta(hours=2), environment="production", artifact="sha256:aaa")
    metrics = report(store, ["MIS-1"]).dora
    assert metrics.change_lead_time_seconds == pytest.approx(7200.0)


def test_change_lead_time_ignores_non_production_deploys():
    store = MemoryStateStore()
    add_event(store, "MIS-1", "revision_pushed", T0)
    add_event(store, "MIS-1", "deployed", T0 + timedelta(minutes=30), environment="staging", artifact="sha256:x")
    add_event(store, "MIS-1", "deployed", T0 + timedelta(hours=3), environment="production", artifact="sha256:x")
    metrics = report(store, ["MIS-1"]).dora
    assert metrics.change_lead_time_seconds == pytest.approx(3 * 3600)


def test_change_lead_time_is_none_without_data():
    store = MemoryStateStore()
    add_event(store, "MIS-1", "revision_pushed", T0)
    metrics = report(store, ["MIS-1"]).dora
    assert metrics.change_lead_time_seconds is None


def test_deployment_frequency_single_deploy_counts_as_one_per_day():
    store = MemoryStateStore()
    add_event(store, "MIS-1", "deployed", T0, environment="production", artifact="sha256:a")
    metrics = report(store, ["MIS-1"]).dora
    assert metrics.deployment_frequency_per_day == 1.0
    assert metrics.deployment_count == 1


def test_deployment_frequency_two_deploys_one_day_apart():
    store = MemoryStateStore()
    add_event(store, "MIS-1", "deployed", T0, environment="production", artifact="sha256:a")
    add_event(store, "MIS-2", "deployed", T0 + timedelta(days=1), environment="production", artifact="sha256:b")
    metrics = report(store, ["MIS-1", "MIS-2"]).dora
    assert metrics.deployment_frequency_per_day == pytest.approx(2.0)


def test_deployment_frequency_zero_without_any_deploys():
    store = MemoryStateStore()
    metrics = report(store, ["MIS-1"]).dora
    assert metrics.deployment_frequency_per_day == 0.0
    assert metrics.deployment_count == 0


def test_failed_deployment_recovery_time_pairs_incident_with_recovery():
    store = MemoryStateStore()
    add_event(store, "MIS-1", "deploy_failed", T0, environment="production", artifact="sha256:a", reason="crash")
    add_event(store, "MIS-1", "recovered", T0 + timedelta(minutes=30), environment="production")
    metrics = report(store, ["MIS-1"]).dora
    assert metrics.failed_deployment_recovery_seconds == pytest.approx(1800.0)


def test_reverted_also_counts_as_an_incident():
    store = MemoryStateStore()
    add_event(store, "MIS-1", "reverted", T0, environment="production", sha="bad-sha")
    add_event(store, "MIS-1", "recovered", T0 + timedelta(minutes=10), environment="production")
    metrics = report(store, ["MIS-1"]).dora
    assert metrics.failed_deployment_recovery_seconds == pytest.approx(600.0)


def test_change_fail_rate():
    store = MemoryStateStore()
    add_event(store, "MIS-1", "deployed", T0, environment="production", artifact="sha256:a")
    add_event(store, "MIS-1", "deploy_failed", T0 + timedelta(minutes=5), environment="production", artifact="sha256:a")
    add_event(store, "MIS-2", "deployed", T0, environment="production", artifact="sha256:b")
    metrics = report(store, ["MIS-1", "MIS-2"]).dora
    assert metrics.change_fail_rate == pytest.approx(0.5)


def test_change_fail_rate_is_none_without_deploys():
    store = MemoryStateStore()
    metrics = report(store, ["MIS-1"]).dora
    assert metrics.change_fail_rate is None


def test_deployment_rework_rate():
    store = MemoryStateStore()
    add_event(store, "MIS-1", "deployed", T0, environment="production", artifact="sha256:a")
    add_event(store, "MIS-1", "rework", T0 + timedelta(hours=1), artifact="sha256:a-fix")
    add_event(store, "MIS-2", "deployed", T0, environment="production", artifact="sha256:b")
    metrics = report(store, ["MIS-1", "MIS-2"]).dora
    assert metrics.deployment_rework_rate == pytest.approx(0.5)


def test_interventions_outside_approvals_and_per_delivered_change():
    store = MemoryStateStore()
    add_event(store, "MIS-1", "human_intervention", T0, action="manual_restart", actor="@ops")
    add_event(store, "MIS-1", "delivered", T0)
    add_event(store, "MIS-2", "human_intervention", T0, action="manual_edit", actor="@dev")
    metrics = report(store, ["MIS-1", "MIS-2"]).autonomy
    assert metrics.interventions_outside_approvals == 2
    assert metrics.delivered_count == 1
    assert metrics.interventions_per_delivered_change == pytest.approx(2.0)


def test_approvals_per_delivered_change():
    store = MemoryStateStore()
    add_event(store, "MIS-1", "approval_requested", T0, gate="H1", request_id="REQ-1")
    add_event(store, "MIS-1", "approval_requested", T0, gate="HM", request_id="REQ-2")
    add_event(store, "MIS-1", "delivered", T0)
    metrics = report(store, ["MIS-1"]).autonomy
    assert metrics.approvals_per_delivered_change == pytest.approx(2.0)


def test_approval_wait_time_matches_by_request_id():
    store = MemoryStateStore()
    add_event(store, "MIS-1", "approval_requested", T0, gate="H1", request_id="REQ-1")
    add_event(store, "MIS-1", "approval_decided", T0 + timedelta(minutes=10), request_id="REQ-1",
              decision="approve", approver="@tl")
    metrics = report(store, ["MIS-1"]).autonomy
    assert metrics.approval_wait_seconds == pytest.approx(600.0)


def test_rejection_rate():
    store = MemoryStateStore()
    add_event(store, "MIS-1", "approval_decided", T0, request_id="R1", decision="approve", approver="@a")
    add_event(store, "MIS-1", "approval_decided", T0, request_id="R2", decision="revise", approver="@b")
    add_event(store, "MIS-1", "approval_decided", T0, request_id="R3", decision="cancel", approver="@c")
    metrics = report(store, ["MIS-1"]).autonomy
    assert metrics.rejection_rate == pytest.approx(2 / 3)


def test_rejection_rate_is_none_without_any_decisions():
    store = MemoryStateStore()
    metrics = report(store, ["MIS-1"]).autonomy
    assert metrics.rejection_rate is None


def test_cost_per_accepted_change_counts_spend_from_undelivered_missions_too():
    store = MemoryStateStore()
    add_event(store, "MIS-1", "cost_spent", T0, usd=5.0)
    add_event(store, "MIS-1", "delivered", T0)
    add_event(store, "MIS-2", "cost_spent", T0, usd=15.0)  # blocked, never delivered -- still wasted spend
    metrics = report(store, ["MIS-1", "MIS-2"]).autonomy
    assert metrics.delivered_count == 1
    assert metrics.cost_per_accepted_change_usd == pytest.approx(20.0)


def test_ratio_metrics_are_none_without_a_denominator():
    store = MemoryStateStore()
    metrics = report(store, ["MIS-1"]).autonomy
    assert metrics.interventions_per_delivered_change is None
    assert metrics.approvals_per_delivered_change is None
    assert metrics.cost_per_accepted_change_usd is None
    assert metrics.approval_wait_seconds is None


def test_unknown_event_kinds_are_ignored():
    store = MemoryStateStore()
    add_event(store, "MIS-1", "some_future_kind", T0, whatever=True)
    result = report(store, ["MIS-1"])
    assert result.dora.deployment_count == 0
    assert result.autonomy.delivered_count == 0


def test_mission_id_used_only_to_group_events_not_referenced_in_dora_math():
    """Regression for the unused-loop-variable cleanup in compute_dora_metrics: the
    mission id itself must never leak into, or be required by, the DORA math -- only
    the per-mission event lists matter."""
    store = MemoryStateStore()
    add_event(store, "any-mission-id-works", "deployed", T0, environment="production", artifact="x")
    metrics = report(store, ["any-mission-id-works"]).dora
    assert metrics.deployment_count == 1


def test_from_store_only_reads_the_requested_mission_ids():
    store = MemoryStateStore()
    add_event(store, "MIS-1", "deployed", T0, environment="production", artifact="x")
    add_event(store, "MIS-2", "deployed", T0, environment="production", artifact="y")
    metrics = report(store, ["MIS-1"]).dora  # MIS-2 not requested
    assert metrics.deployment_count == 1


def test_render_prometheus_includes_known_values_and_omits_none():
    store = MemoryStateStore()
    add_event(store, "MIS-1", "deployed", T0, environment="production", artifact="x")
    text = render_prometheus(report(store, ["MIS-1"]))
    assert "factory_deployment_count 1" in text
    assert "factory_deployment_frequency_per_day 1.0" in text
    # change_lead_time_seconds is None here (no revision_pushed) -- must not appear
    assert "factory_change_lead_time_seconds" not in text
    assert text.endswith("\n")


def test_render_prometheus_has_help_and_type_lines():
    store = MemoryStateStore()
    add_event(store, "MIS-1", "deployed", T0, environment="production", artifact="x")
    text = render_prometheus(report(store, ["MIS-1"]))
    assert "# HELP factory_deployment_count" in text
    assert "# TYPE factory_deployment_count gauge" in text


# -- Q-M7: naive timestamps are treated as UTC -------------------------------------------

def test_naive_datetime_object_is_treated_as_utc():
    """`MemoryStateStore` always produces tz-aware timestamps, so this bypasses it to
    pin the naive -> UTC normalization directly: a naive `datetime` (no tzinfo) must
    still combine correctly with aware ones instead of raising
    "can't subtract offset-naive and offset-aware datetimes"."""
    naive_push = datetime(2026, 1, 1, 0, 0)  # no tzinfo
    events = {"MIS-1": [
        {"kind": "revision_pushed", "at": naive_push},
        {"kind": "deployed", "at": T0 + timedelta(hours=2), "environment": "production", "artifact": "x"},
    ]}
    metrics = compute_dora_metrics(events)
    assert metrics.change_lead_time_seconds == pytest.approx(7200.0)


def test_naive_iso_string_timestamp_is_treated_as_utc():
    events = {"MIS-1": [
        {"kind": "revision_pushed", "at": "2026-01-01T00:00:00"},  # no "Z", no offset
        {"kind": "deployed", "at": "2026-01-01T02:00:00Z", "environment": "production", "artifact": "x"},
    ]}
    metrics = compute_dora_metrics(events)
    assert metrics.change_lead_time_seconds == pytest.approx(7200.0)
