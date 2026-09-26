from datetime import datetime, timedelta, timezone

import pytest

from factory.telemetry.metrics import (compute_autonomy_metrics, compute_dora_metrics, compute_metrics,
                                        from_store, render_prometheus)

T0 = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)


def ev(event_kind: str, at: datetime, **payload) -> dict:
    return {"kind": event_kind, "at": at, **payload}


def test_change_lead_time_from_first_push_to_first_production_deploy():
    events = {
        "MIS-1": [
            ev("revision_pushed", T0, sha="aaa"),
            ev("deployed", T0 + timedelta(hours=2), environment="production", artifact="sha256:aaa"),
        ]
    }
    metrics = compute_dora_metrics(events)
    assert metrics.change_lead_time_seconds == pytest.approx(7200.0)


def test_change_lead_time_ignores_non_production_deploys():
    events = {
        "MIS-1": [
            ev("revision_pushed", T0),
            ev("deployed", T0 + timedelta(minutes=30), environment="staging", artifact="sha256:x"),
            ev("deployed", T0 + timedelta(hours=3), environment="production", artifact="sha256:x"),
        ]
    }
    metrics = compute_dora_metrics(events)
    assert metrics.change_lead_time_seconds == pytest.approx(3 * 3600)


def test_change_lead_time_is_none_without_data():
    metrics = compute_dora_metrics({"MIS-1": [ev("revision_pushed", T0)]})
    assert metrics.change_lead_time_seconds is None


def test_deployment_frequency_single_deploy_counts_as_one_per_day():
    events = {"MIS-1": [ev("deployed", T0, environment="production", artifact="sha256:a")]}
    metrics = compute_dora_metrics(events)
    assert metrics.deployment_frequency_per_day == 1.0
    assert metrics.deployment_count == 1


def test_deployment_frequency_two_deploys_one_day_apart():
    events = {
        "MIS-1": [ev("deployed", T0, environment="production", artifact="sha256:a")],
        "MIS-2": [ev("deployed", T0 + timedelta(days=1), environment="production", artifact="sha256:b")],
    }
    metrics = compute_dora_metrics(events)
    assert metrics.deployment_frequency_per_day == pytest.approx(2.0)


def test_deployment_frequency_zero_without_any_deploys():
    metrics = compute_dora_metrics({"MIS-1": []})
    assert metrics.deployment_frequency_per_day == 0.0
    assert metrics.deployment_count == 0


def test_failed_deployment_recovery_time_pairs_incident_with_recovery():
    events = {
        "MIS-1": [
            ev("deploy_failed", T0, environment="production", artifact="sha256:a", reason="crash"),
            ev("recovered", T0 + timedelta(minutes=30), environment="production"),
        ]
    }
    metrics = compute_dora_metrics(events)
    assert metrics.failed_deployment_recovery_seconds == pytest.approx(1800.0)


def test_reverted_also_counts_as_an_incident():
    events = {
        "MIS-1": [
            ev("reverted", T0, environment="production", sha="bad-sha"),
            ev("recovered", T0 + timedelta(minutes=10), environment="production"),
        ]
    }
    metrics = compute_dora_metrics(events)
    assert metrics.failed_deployment_recovery_seconds == pytest.approx(600.0)


def test_change_fail_rate():
    events = {
        "MIS-1": [
            ev("deployed", T0, environment="production", artifact="sha256:a"),
            ev("deploy_failed", T0 + timedelta(minutes=5), environment="production", artifact="sha256:a"),
        ],
        "MIS-2": [ev("deployed", T0, environment="production", artifact="sha256:b")],
    }
    metrics = compute_dora_metrics(events)
    assert metrics.change_fail_rate == pytest.approx(0.5)


def test_change_fail_rate_is_none_without_deploys():
    metrics = compute_dora_metrics({"MIS-1": []})
    assert metrics.change_fail_rate is None


def test_deployment_rework_rate():
    events = {
        "MIS-1": [
            ev("deployed", T0, environment="production", artifact="sha256:a"),
            ev("rework", T0 + timedelta(hours=1), artifact="sha256:a-fix"),
        ],
        "MIS-2": [ev("deployed", T0, environment="production", artifact="sha256:b")],
    }
    metrics = compute_dora_metrics(events)
    assert metrics.deployment_rework_rate == pytest.approx(0.5)


def test_interventions_outside_approvals_and_per_delivered_change():
    events = {
        "MIS-1": [ev("human_intervention", T0, action="manual_restart", actor="@ops"), ev("delivered", T0)],
        "MIS-2": [ev("human_intervention", T0, action="manual_edit", actor="@dev")],
    }
    metrics = compute_autonomy_metrics(events)
    assert metrics.interventions_outside_approvals == 2
    assert metrics.delivered_count == 1
    assert metrics.interventions_per_delivered_change == pytest.approx(2.0)


def test_approvals_per_delivered_change():
    events = {
        "MIS-1": [
            ev("approval_requested", T0, gate="H1", request_id="REQ-1"),
            ev("approval_requested", T0, gate="HM", request_id="REQ-2"),
            ev("delivered", T0),
        ],
    }
    metrics = compute_autonomy_metrics(events)
    assert metrics.approvals_per_delivered_change == pytest.approx(2.0)


def test_approval_wait_time_matches_by_request_id():
    events = {
        "MIS-1": [
            ev("approval_requested", T0, gate="H1", request_id="REQ-1"),
            ev("approval_decided", T0 + timedelta(minutes=10), request_id="REQ-1",
               decision="approve", approver="@tl"),
        ],
    }
    metrics = compute_autonomy_metrics(events)
    assert metrics.approval_wait_seconds == pytest.approx(600.0)


def test_rejection_rate():
    events = {
        "MIS-1": [
            ev("approval_decided", T0, request_id="R1", decision="approve", approver="@a"),
            ev("approval_decided", T0, request_id="R2", decision="revise", approver="@b"),
            ev("approval_decided", T0, request_id="R3", decision="cancel", approver="@c"),
        ],
    }
    metrics = compute_autonomy_metrics(events)
    assert metrics.rejection_rate == pytest.approx(2 / 3)


def test_rejection_rate_is_none_without_any_decisions():
    metrics = compute_autonomy_metrics({"MIS-1": []})
    assert metrics.rejection_rate is None


def test_cost_per_accepted_change_counts_spend_from_undelivered_missions_too():
    events = {
        "MIS-1": [ev("cost_spent", T0, usd=5.0), ev("delivered", T0)],
        "MIS-2": [ev("cost_spent", T0, usd=15.0)],  # blocked, never delivered -- still wasted spend
    }
    metrics = compute_autonomy_metrics(events)
    assert metrics.delivered_count == 1
    assert metrics.cost_per_accepted_change_usd == pytest.approx(20.0)


def test_ratio_metrics_are_none_without_a_denominator():
    metrics = compute_autonomy_metrics({"MIS-1": []})
    assert metrics.interventions_per_delivered_change is None
    assert metrics.approvals_per_delivered_change is None
    assert metrics.cost_per_accepted_change_usd is None
    assert metrics.approval_wait_seconds is None


def test_unknown_event_kinds_are_ignored():
    events = {"MIS-1": [ev("some_future_kind", T0, whatever=True)]}
    metrics = compute_metrics(events)
    assert metrics.dora.deployment_count == 0
    assert metrics.autonomy.delivered_count == 0


def test_nested_payload_shape_is_also_supported():
    events = {"MIS-1": [{"kind": "deployed", "at": T0, "payload": {"environment": "production", "artifact": "x"}}]}
    metrics = compute_dora_metrics(events)
    assert metrics.deployment_count == 1


def test_string_timestamps_are_parsed():
    events = {
        "MIS-1": [
            {"kind": "deployed", "at": T0.isoformat().replace("+00:00", "Z"),
             "environment": "production", "artifact": "x"},
        ]
    }
    metrics = compute_dora_metrics(events)
    assert metrics.deployment_count == 1


def test_from_store_pulls_events_per_mission():
    class FakeStore:
        def __init__(self, events_by_mission):
            self._events = events_by_mission

        def list_events(self, mission_id):
            return self._events.get(mission_id, [])

    store = FakeStore({"MIS-1": [ev("deployed", T0, environment="production", artifact="x")]})
    report = from_store(store, ["MIS-1", "MIS-2"])
    assert report.dora.deployment_count == 1


def test_render_prometheus_includes_known_values_and_omits_none():
    events = {"MIS-1": [ev("deployed", T0, environment="production", artifact="x")]}
    report = compute_metrics(events)
    text = render_prometheus(report)
    assert "factory_deployment_count 1" in text
    assert "factory_deployment_frequency_per_day 1.0" in text
    # change_lead_time_seconds is None here (no revision_pushed) -- must not appear
    assert "factory_change_lead_time_seconds" not in text
    assert text.endswith("\n")


def test_render_prometheus_has_help_and_type_lines():
    report = compute_metrics({"MIS-1": [ev("deployed", T0, environment="production", artifact="x")]})
    text = render_prometheus(report)
    assert "# HELP factory_deployment_count" in text
    assert "# TYPE factory_deployment_count gauge" in text
