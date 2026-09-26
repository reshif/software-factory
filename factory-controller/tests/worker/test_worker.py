"""The background worker loop (build spec §3 B7 `worker.py`).

`Worker.run_once` has no dedicated tests yet -- these exercise it against a
REAL `Factory` (via the demo harness, exactly like `tests/pipeline` and
`tests/e2e` do), not a hand-rolled stub, so a passing test here means the
worker's phases actually interoperate with the real mission lifecycle:
dispatching queued webhooks, processing queued approval decisions, applying
side-state timeouts (notify the backup approver, then fire the timeout
event), releasing admitted missions, running ready tasks, reconciling pending
intents, and advancing OBSERVING missions past their window.
"""
from __future__ import annotations

import logging

from factory.controller import mission_fsm
from factory.demo.harness import new_context
from factory.demo.scenarios import _architect, _implementer, _intake, _reviewer, advance_to_merged, task
from factory.worker import Worker


def _spy(calls: list, name: str, original):
    def wrapped(*args, **kwargs):
        calls.append(name)
        return original(*args, **kwargs)
    return wrapped


def test_run_once_calls_every_phase_in_order():
    ctx = new_context()
    try:
        factory = ctx.factory
        calls: list[str] = []
        for name in ("dispatch_webhooks", "process_approvals", "release_admitted_missions", "run_ready_tasks"):
            setattr(factory, name, _spy(calls, name, getattr(factory, name)))

        Worker(factory, clock=ctx.clock).run_once()

        assert calls == ["dispatch_webhooks", "process_approvals", "release_admitted_missions", "run_ready_tasks"]
    finally:
        ctx.cleanup()


def _feature_awaiting_h1(ctx) -> str:
    runtime = ctx.adapters["runtime"]
    runtime.set_script("intake", _intake("feature", "AC4"))
    runtime.set_script("architect", _architect(tasks=[task("T-1", "Add a small helper.")]))
    item = ctx.add_work_item(title="A change awaiting H1", labels=("factory:feature",))
    mission = ctx.start(item)
    assert mission.state == "AWAITING_H1", mission.state
    return mission.mission_id


# ── walkthrough-8-style: a timed-out approval notifies the backup, then HELDs ────
def test_timeout_notifies_backup_approver_then_holds_the_mission():
    ctx = new_context()
    try:
        mission_id = _feature_awaiting_h1(ctx)
        timeout, event = mission_fsm.TIMEOUTS["AWAITING_H1"]
        assert event == "timeout"
        ctx.clock.advance(seconds=timeout.total_seconds() + 1)

        Worker(ctx.factory, clock=ctx.clock).run_once()

        assert ctx.mission(mission_id).state == "HELD"
        backup = ctx.product.owners["backup"]
        assert any(mission_id in msg and backup in msg for msg in ctx.adapters["notifier"].info_messages), (
            ctx.adapters["notifier"].info_messages)
        # never approved: nobody ever decided, and the mission did NOT reach ADMITTED.
        assert not ctx.factory.store.approvals.list_open(mission_id) or all(
            r.gate != "H1" or r.status != "approved" for r in ctx.factory.store.approvals.list_open(mission_id))
    finally:
        ctx.cleanup()


def test_timeout_notification_is_not_repeated_on_every_later_tick():
    ctx = new_context()
    try:
        mission_id = _feature_awaiting_h1(ctx)
        timeout, _event = mission_fsm.TIMEOUTS["AWAITING_H1"]
        ctx.clock.advance(seconds=timeout.total_seconds() + 1)

        worker = Worker(ctx.factory, clock=ctx.clock)
        worker.run_once()
        assert ctx.mission(mission_id).state == "HELD"
        before = list(ctx.adapters["notifier"].info_messages)

        # HELD is a NO_WORKER state: further ticks must not re-notify or re-fire.
        worker.run_once()
        worker.run_once()

        assert ctx.adapters["notifier"].info_messages == before
    finally:
        ctx.cleanup()


# ── advancing OBSERVING missions ─────────────────────────────────────────────────
def test_advance_observing_delivers_a_healthy_mission_past_its_window():
    ctx = new_context()
    try:
        runtime = ctx.adapters["runtime"]
        runtime.set_script("intake", _intake("feature", "AC4"))
        runtime.set_script("architect", _architect(tasks=[task("T-1", "Add a small helper.")]))
        runtime.set_script("implementer", _implementer({"app/note.md": "hi\n"}))
        runtime.set_script("reviewer", _reviewer("pass"))

        item = ctx.add_work_item(title="A change to deliver", labels=("factory:feature",))
        mission = ctx.start(item)
        ctx.decide(mission.mission_id, "H1", "approve")
        ctx.factory.run_ready_tasks(mission.mission_id)
        advance_to_merged(ctx, mission.mission_id)
        sha = ctx.merge_sha(mission.mission_id)
        ctx.pass_checks(sha, ("lint", "unit", "review_agent"))
        ctx.fire_check_suite(sha)
        ctx.decide(mission.mission_id, "H2", "approve")
        assert ctx.mission(mission.mission_id).state == "OBSERVING"

        ctx.clock.advance(hours=25)
        Worker(ctx.factory, clock=ctx.clock).run_once()

        assert ctx.mission(mission.mission_id).state == "DELIVERED"
    finally:
        ctx.cleanup()


# ── reconciling pending intents: visible, not silently dropped ──────────────────
def test_run_once_logs_a_still_pending_intent(caplog):
    ctx = new_context()
    try:
        ctx.factory.store.intents.begin("push:MIS-does-not-exist:deadbeef")
        with caplog.at_level(logging.WARNING, logger="factory.worker"):
            Worker(ctx.factory, clock=ctx.clock).run_once()
        assert any("push:MIS-does-not-exist:deadbeef" in message for message in caplog.messages)
    finally:
        ctx.cleanup()


# ── the loop survives one bad tick ──────────────────────────────────────────────
def test_run_forever_survives_a_failing_tick(caplog):
    ctx = new_context()
    try:
        def boom():
            raise RuntimeError("simulated failure")

        ctx.factory.dispatch_webhooks = boom
        sleeps = []
        with caplog.at_level(logging.ERROR, logger="factory.worker"):
            Worker(ctx.factory, clock=ctx.clock).run_forever(interval_s=0, sleep=sleeps.append, iterations=3)
        assert len(sleeps) == 2  # sleeps between ticks, not after the last one
        assert sum("worker tick failed" in m for m in caplog.messages) == 3
    finally:
        ctx.cleanup()
