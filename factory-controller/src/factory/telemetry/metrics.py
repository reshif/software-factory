"""DORA and autonomy metrics computed from `StateStore` events (final draft §18).

**Event contract.** `StateStore.append_event(mission_id, kind, payload)` records one
event; `StateStore.list_events(mission_id)` returns them back. This module expects
each returned event to be a mapping containing at least:

  - ``"kind"``: one of the event kinds below.
  - ``"at"``: a `datetime` (any timezone, or naive-as-UTC) or an ISO-8601 string --
    the moment the event happened.

...plus that event kind's payload fields, either flattened into the same mapping
(``{"kind": "deployed", "at": ..., "environment": "production", "artifact": "sha256:.."}``)
or nested under a ``"payload"`` key (``{"kind": ..., "at": ..., "payload": {...}}``).
Both shapes are read transparently, since the concrete store (B1) may choose either.

**Event kinds this module reads:**

| kind                  | payload fields                          | used for |
|-----------------------|------------------------------------------|----------|
| ``revision_pushed``   | ``sha``                                   | change lead time (start: first per mission) |
| ``deployed``          | ``environment``, ``artifact``             | change lead time (end), deployment frequency |
| ``deploy_failed``     | ``environment``, ``artifact``, ``reason`` | change fail rate, recovery time (start) |
| ``reverted``          | ``environment``, ``sha``                  | change fail rate, recovery time (start) |
| ``recovered``         | ``environment``                           | recovery time (end) |
| ``rework``            | ``artifact``                              | deployment rework rate |
| ``approval_requested``| ``gate``, ``request_id``                  | approvals per change, approval wait (start) |
| ``approval_decided``  | ``request_id``, ``decision``, ``approver``| approval wait (end), rejection rate |
| ``human_intervention``| ``action``, ``actor``                     | interventions outside approvals |
| ``cost_spent``        | ``usd``                                   | cost per accepted change |
| ``delivered``         | (none)                                    | denominator for the "per accepted change" metrics |

Unrecognized event kinds are ignored, not treated as errors -- new event kinds can be
added by other modules without breaking telemetry.

Note: a payload must never use the key ``"kind"`` or ``"at"`` -- those names are
reserved for the event envelope and, in the flattened shape, a payload value would
silently overwrite the event's own kind or timestamp.

A ratio metric whose denominator is zero (e.g. no changes have been delivered yet) is
reported as ``None`` rather than 0.0, since "no data" and "zero rate" mean different
things; `render_prometheus` simply omits metrics that are `None`.
"""
from dataclasses import dataclass
from datetime import datetime, timezone

from ..ports import StateStore

REVISION_PUSHED = "revision_pushed"
DEPLOYED = "deployed"
DEPLOY_FAILED = "deploy_failed"
REVERTED = "reverted"
RECOVERED = "recovered"
REWORK = "rework"
APPROVAL_REQUESTED = "approval_requested"
APPROVAL_DECIDED = "approval_decided"
HUMAN_INTERVENTION = "human_intervention"
COST_SPENT = "cost_spent"
DELIVERED = "delivered"

REJECTING_DECISIONS = ("revise", "cancel")
DEFAULT_ENVIRONMENT = "production"


def _at(event: dict) -> datetime:
    value = event.get("at")
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise ValueError(f"event has no usable 'at' timestamp: {event!r}")
    # A naive datetime (no tzinfo) is treated as UTC -- whether it arrived as a
    # datetime object directly or was parsed from a naive ISO string above -- so it
    # can always be compared and subtracted against the aware datetimes elsewhere in
    # this module without raising.
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _field(event: dict, key: str, default=None):
    if key in event:
        return event[key]
    return event.get("payload", {}).get(key, default)


def _kind(event: dict) -> str:
    return event.get("kind", "")


@dataclass(frozen=True)
class DoraMetrics:
    """The 5 DORA metrics (final draft §18), plus `deployment_count` -- the supporting
    count of production deploys the other 5 fields were computed from, not itself one
    of the 5."""
    change_lead_time_seconds: float | None
    deployment_frequency_per_day: float
    failed_deployment_recovery_seconds: float | None
    change_fail_rate: float | None
    deployment_rework_rate: float | None
    deployment_count: int


@dataclass(frozen=True)
class AutonomyMetrics:
    """The autonomy and oversight-health metrics (final draft §18)."""
    interventions_outside_approvals: int
    interventions_per_delivered_change: float | None
    approvals_per_delivered_change: float | None
    approval_wait_seconds: float | None
    rejection_rate: float | None
    cost_per_accepted_change_usd: float | None
    delivered_count: int


@dataclass(frozen=True)
class MetricsReport:
    dora: DoraMetrics
    autonomy: AutonomyMetrics


def _all_events(events_by_mission: dict[str, list[dict]]) -> list[tuple[str, dict]]:
    return [(mission_id, event) for mission_id, events in events_by_mission.items() for event in events]


def compute_dora_metrics(events_by_mission: dict[str, list[dict]], *,
                          environment: str = DEFAULT_ENVIRONMENT) -> DoraMetrics:
    """Compute the 5 DORA metrics, scoped to `environment` (default "production")."""
    lead_times: list[float] = []
    deploy_ats: list[datetime] = []
    recovery_durations: list[float] = []
    deploy_count = 0
    incident_count = 0
    rework_count = 0

    for events in events_by_mission.values():
        by_time = sorted(events, key=_at)

        first_push = next((e for e in by_time if _kind(e) == REVISION_PUSHED), None)
        first_deploy = next((e for e in by_time
                              if _kind(e) == DEPLOYED and _field(e, "environment") == environment), None)
        if first_push is not None and first_deploy is not None and _at(first_deploy) >= _at(first_push):
            lead_times.append((_at(first_deploy) - _at(first_push)).total_seconds())

        deploy_count += sum(1 for e in by_time if _kind(e) == DEPLOYED and _field(e, "environment") == environment)
        deploy_ats.extend(_at(e) for e in by_time if _kind(e) == DEPLOYED and _field(e, "environment") == environment)
        rework_count += sum(1 for e in by_time if _kind(e) == REWORK)

        incidents = [e for e in by_time
                     if _kind(e) in (DEPLOY_FAILED, REVERTED) and _field(e, "environment") == environment]
        recoveries = [e for e in by_time
                      if _kind(e) == RECOVERED and _field(e, "environment") == environment]
        incident_count += len(incidents)
        recovery_iter = iter(recoveries)
        pending_recovery = next(recovery_iter, None)
        for incident in incidents:
            while pending_recovery is not None and _at(pending_recovery) < _at(incident):
                pending_recovery = next(recovery_iter, None)
            if pending_recovery is not None:
                recovery_durations.append((_at(pending_recovery) - _at(incident)).total_seconds())
                pending_recovery = next(recovery_iter, None)

    if deploy_count == 0:
        deployment_frequency = 0.0
    elif len(deploy_ats) == 1:
        deployment_frequency = 1.0
    else:
        span_seconds = max((max(deploy_ats) - min(deploy_ats)).total_seconds(), 1.0)
        deployment_frequency = deploy_count / (span_seconds / 86400.0)

    return DoraMetrics(
        change_lead_time_seconds=_mean(lead_times),
        deployment_frequency_per_day=deployment_frequency,
        failed_deployment_recovery_seconds=_mean(recovery_durations),
        change_fail_rate=(incident_count / deploy_count) if deploy_count else None,
        deployment_rework_rate=(rework_count / deploy_count) if deploy_count else None,
        deployment_count=deploy_count,
    )


def compute_autonomy_metrics(events_by_mission: dict[str, list[dict]]) -> AutonomyMetrics:
    """Compute the autonomy and oversight-health metrics (final draft §18).

    "Cost per accepted change" is total cost across *all* missions (including ones
    that were blocked, declined or failed) divided by the number of delivered
    missions -- it deliberately counts wasted spend, since that waste is part of the
    true cost of each change that does ship.
    """
    all_events = _all_events(events_by_mission)

    delivered_count = sum(1 for _, e in all_events if _kind(e) == DELIVERED)
    interventions = sum(1 for _, e in all_events if _kind(e) == HUMAN_INTERVENTION)
    total_cost = sum(_field(e, "usd", 0.0) for _, e in all_events if _kind(e) == COST_SPENT)
    approvals_requested = sum(1 for _, e in all_events if _kind(e) == APPROVAL_REQUESTED)

    decided = [e for _, e in all_events if _kind(e) == APPROVAL_DECIDED]
    rejecting = [e for e in decided if _field(e, "decision") in REJECTING_DECISIONS]

    requested_at: dict[str, datetime] = {}
    wait_times: list[float] = []
    for _, e in all_events:
        if _kind(e) == APPROVAL_REQUESTED:
            request_id = _field(e, "request_id")
            if request_id is not None:
                requested_at[request_id] = _at(e)
    for _, e in sorted(all_events, key=lambda pair: _at(pair[1])):
        if _kind(e) == APPROVAL_DECIDED:
            request_id = _field(e, "request_id")
            started = requested_at.pop(request_id, None) if request_id is not None else None
            if started is not None:
                wait_times.append((_at(e) - started).total_seconds())

    return AutonomyMetrics(
        interventions_outside_approvals=interventions,
        interventions_per_delivered_change=(interventions / delivered_count) if delivered_count else None,
        approvals_per_delivered_change=(approvals_requested / delivered_count) if delivered_count else None,
        approval_wait_seconds=_mean(wait_times),
        rejection_rate=(len(rejecting) / len(decided)) if decided else None,
        cost_per_accepted_change_usd=(total_cost / delivered_count) if delivered_count else None,
        delivered_count=delivered_count,
    )


def compute_metrics(events_by_mission: dict[str, list[dict]], *,
                     environment: str = DEFAULT_ENVIRONMENT) -> MetricsReport:
    return MetricsReport(
        dora=compute_dora_metrics(events_by_mission, environment=environment),
        autonomy=compute_autonomy_metrics(events_by_mission),
    )


def from_store(store: StateStore, mission_ids: list[str], *,
                environment: str = DEFAULT_ENVIRONMENT) -> MetricsReport:
    """Convenience wrapper: pull events for `mission_ids` from `store` and compute."""
    events_by_mission = {mission_id: store.list_events(mission_id) for mission_id in mission_ids}
    return compute_metrics(events_by_mission, environment=environment)


def _mean(values: list[float]) -> float | None:
    return (sum(values) / len(values)) if values else None


# -- Prometheus text exposition format ------------------------------------------------

_PROM_METRICS = (
    ("factory_change_lead_time_seconds", "gauge",
     "Mean time from first commit to production deploy, in seconds.",
     lambda r: r.dora.change_lead_time_seconds),
    ("factory_deployment_frequency_per_day", "gauge",
     "Production deployments per day.", lambda r: r.dora.deployment_frequency_per_day),
    ("factory_failed_deployment_recovery_seconds", "gauge",
     "Mean time from a failed deployment or revert to recovery, in seconds.",
     lambda r: r.dora.failed_deployment_recovery_seconds),
    ("factory_change_fail_rate", "gauge",
     "Fraction of production deployments that failed or were reverted.",
     lambda r: r.dora.change_fail_rate),
    ("factory_deployment_rework_rate", "gauge",
     "Fraction of production deployments that required a forward-fix.",
     lambda r: r.dora.deployment_rework_rate),
    ("factory_deployment_count", "gauge",
     "Number of production deployments observed.", lambda r: r.dora.deployment_count),
    ("factory_human_interventions_outside_approvals", "gauge",
     "Count of human interventions that were not a recorded approval decision.",
     lambda r: r.autonomy.interventions_outside_approvals),
    ("factory_interventions_per_delivered_change", "gauge",
     "Human interventions outside approvals, per delivered change.",
     lambda r: r.autonomy.interventions_per_delivered_change),
    ("factory_approvals_per_delivered_change", "gauge",
     "Decision packets requested, per delivered change.",
     lambda r: r.autonomy.approvals_per_delivered_change),
    ("factory_approval_wait_seconds", "gauge",
     "Mean time from an approval request to its decision, in seconds.",
     lambda r: r.autonomy.approval_wait_seconds),
    ("factory_rejection_rate", "gauge",
     "Fraction of approval decisions that were revise or cancel.",
     lambda r: r.autonomy.rejection_rate),
    ("factory_cost_per_accepted_change_usd", "gauge",
     "Total spend across all missions, per delivered change, in USD.",
     lambda r: r.autonomy.cost_per_accepted_change_usd),
    ("factory_delivered_count", "gauge",
     "Number of delivered (accepted) changes observed.", lambda r: r.autonomy.delivered_count),
)


def render_prometheus(report: MetricsReport) -> str:
    """Render `report` as Prometheus text exposition format.

    Metrics whose value is `None` (an undefined ratio, e.g. no deliveries yet) are
    omitted entirely -- Prometheus has no "unknown" value, and a fabricated 0 would
    misrepresent "no data" as "measured zero".
    """
    lines: list[str] = []
    for name, metric_type, help_text, getter in _PROM_METRICS:
        value = getter(report)
        if value is None:
            continue
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {metric_type}")
        lines.append(f"{name} {value}")
    return "\n".join(lines) + "\n"
