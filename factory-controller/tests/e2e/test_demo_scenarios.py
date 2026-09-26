"""Every demo scenario (build spec §3 B7 "Demo") runs as a test here.

Each scenario function in `factory.demo.scenarios` already asserts its own end
state (mission state, task state, receipts, ...) and returns `True`/raises;
this just makes sure pytest fails loudly if a scenario regresses.
"""
import pytest

from factory.demo.scenarios import SCENARIOS


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scenario(name):
    assert SCENARIOS[name]() is True
