from datetime import datetime, timedelta, timezone

import pytest

from factory.policy import load_policy


@pytest.fixture(scope="session")
def policy():
    return load_policy()


@pytest.fixture
def now():
    return datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def later(now):
    return lambda **kw: now + timedelta(**kw)
