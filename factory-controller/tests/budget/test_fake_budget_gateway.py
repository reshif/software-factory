import pytest

from factory.budget.fake import BudgetExceeded, FakeBudgetGateway


def test_create_key_returns_distinct_keys():
    gateway = FakeBudgetGateway()
    key1 = gateway.create_key("MIS-1", 10.0)
    key2 = gateway.create_key("MIS-2", 10.0)
    assert key1 != key2


def test_spent_starts_at_zero():
    gateway = FakeBudgetGateway()
    key = gateway.create_key("MIS-1", 10.0)
    assert gateway.spent(key) == 0.0


def test_add_spend_accumulates():
    gateway = FakeBudgetGateway()
    key = gateway.create_key("MIS-1", 10.0)
    gateway.add_spend(key, 2.5)
    gateway.add_spend(key, 1.5)
    assert gateway.spent(key) == 4.0


def test_add_spend_past_budget_raises_budget_exceeded():
    gateway = FakeBudgetGateway()
    key = gateway.create_key("MIS-1", 5.0)
    with pytest.raises(BudgetExceeded):
        gateway.add_spend(key, 5.01)


def test_revoke_marks_key_revoked_and_blocks_further_spend():
    gateway = FakeBudgetGateway()
    key = gateway.create_key("MIS-1", 10.0)
    gateway.revoke(key)
    assert gateway.is_revoked(key)
    with pytest.raises(KeyError):
        gateway.add_spend(key, 1.0)


def test_unknown_key_raises_keyerror():
    gateway = FakeBudgetGateway()
    with pytest.raises(KeyError):
        gateway.spent("no-such-key")
