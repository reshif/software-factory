"""Fixtures for the store contract tests: the same tests run against both backends.

`FACTORY_TEST_DATABASE_URL` (e.g. from a local `docker run postgres:16`)
enables the Postgres side; without it, only the memory backend runs and the
Postgres param is skipped (`@pytest.mark.postgres`, per the build spec).
"""
import os

import psycopg
import pytest

from factory.store import MemoryStateStore, PostgresStateStore
from factory.store.postgres import apply_migrations

PG_URL = os.environ.get("FACTORY_TEST_DATABASE_URL")

# Tables truncated between Postgres-backed tests; schema_migrations is excluded
# on purpose so migrations are applied once per session, not replayed.
_TABLES = ("approval_decisions", "approvals", "intents", "events", "packets", "evidence", "tasks",
          "missions")


def _reset_postgres(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        apply_migrations(conn)
        with conn.cursor() as cur:
            cur.execute(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE")
            cur.execute("ALTER SEQUENCE approval_fencing_seq RESTART WITH 1")
        conn.commit()


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.postgres)])
def store(request):
    """A fresh `StateStore`: parametrized so every test runs against both backends."""
    if request.param == "memory":
        return MemoryStateStore()
    if not PG_URL:
        pytest.skip("FACTORY_TEST_DATABASE_URL not set")
    _reset_postgres(PG_URL)
    return PostgresStateStore(PG_URL)


@pytest.fixture
def postgres_only_store():
    """A fresh Postgres-backed store, for behavior with no memory-backend equivalent.

    The memory `IntentLog` stores a receipt object as-is; only the Postgres adapter
    has to serialize it, so only it can reject an unsupported type.
    """
    if not PG_URL:
        pytest.skip("FACTORY_TEST_DATABASE_URL not set")
    _reset_postgres(PG_URL)
    return PostgresStateStore(PG_URL)
