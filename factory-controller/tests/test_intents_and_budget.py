import pytest

from factory.controller.approval_budget import can_admit
from factory.controller.intents import IntentLog, execute_once


class Crash(Exception):
    pass


def test_execute_once_is_idempotent():
    log, calls = IntentLog(), []
    run = lambda: execute_once(log, "op-1", action=lambda: calls.append(1) or "receipt", probe=lambda: None)
    assert run() == "receipt"
    assert run() == "receipt"
    assert calls == [1]


def test_crash_after_effect_is_reconciled_not_repeated():
    log, world = IntentLog(), []

    def deploy_then_crash():
        world.append("deployed")
        raise Crash("controller died before writing the receipt")

    with pytest.raises(Crash):
        execute_once(log, "deploy-1", action=deploy_then_crash, probe=lambda: None)
    assert [i.operation_id for i in log.pending()] == ["deploy-1"]

    receipt = execute_once(log, "deploy-1", action=lambda: world.append("deployed") or "r2",
                           probe=lambda: "observed" if "deployed" in world else None)
    assert receipt == "observed"
    assert world == ["deployed"]
    assert not log.pending()


def test_crash_before_effect_is_retried():
    log, world = IntentLog(), []
    with pytest.raises(Crash):
        execute_once(log, "op", action=lambda: (_ for _ in ()).throw(Crash()), probe=lambda: None)
    execute_once(log, "op", action=lambda: world.append("done") or "ok", probe=lambda: None)
    assert world == ["done"]


def test_approval_budget_admission():
    # 10 reviewer hours = 600 minutes.
    assert can_admit("feature", committed_minutes=500, reviewer_hours_per_week=10)
    assert not can_admit("feature", committed_minutes=560, reviewer_hours_per_week=10)
    assert can_admit("patch", committed_minutes=590, reviewer_hours_per_week=10)
