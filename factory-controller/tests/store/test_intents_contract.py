"""Contract tests for `StateStore.intents`: same functions, both backends.

Ported from `tests/test_intents_and_budget.py` against the reference
`IntentLog`. `execute_once` only needs `.get` / `.begin` / `.complete`, so it
runs unmodified against `store.intents` for both backends (final draft §10).
"""
import pytest

from factory.controller.intents import execute_once


class Crash(Exception):
    pass


def test_execute_once_is_idempotent(store):
    calls = []
    run = lambda: execute_once(store.intents, "op-1", action=lambda: calls.append(1) or "receipt",
                               probe=lambda: None)
    assert run() == "receipt"
    assert run() == "receipt"
    assert calls == [1]


def test_crash_after_effect_is_reconciled_not_repeated(store):
    world = []

    def deploy_then_crash():
        world.append("deployed")
        raise Crash("controller died before writing the receipt")

    with pytest.raises(Crash):
        execute_once(store.intents, "deploy-1", action=deploy_then_crash, probe=lambda: None)
    assert [i.operation_id for i in store.intents.pending()] == ["deploy-1"]

    receipt = execute_once(store.intents, "deploy-1", action=lambda: world.append("deployed") or "r2",
                           probe=lambda: "observed" if "deployed" in world else None)
    assert receipt == "observed"
    assert world == ["deployed"]
    assert not store.intents.pending()


def test_crash_before_effect_is_retried(store):
    world = []
    with pytest.raises(Crash):
        execute_once(store.intents, "op", action=lambda: (_ for _ in ()).throw(Crash()), probe=lambda: None)
    execute_once(store.intents, "op", action=lambda: world.append("done") or "ok", probe=lambda: None)
    assert world == ["done"]


def test_get_missing_intent_returns_none(store):
    assert store.intents.get("nope") is None


def test_complete_unknown_operation_raises(store):
    with pytest.raises(KeyError):
        store.intents.complete("nope", "receipt")
