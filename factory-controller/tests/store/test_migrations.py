"""Tests for `apply_migrations`: packaging, the not-found guard, and the advisory lock (Q-H5).

All Postgres-only: there's nothing to migrate in the memory backend.
"""
import threading

import psycopg
import pytest

from factory.store.postgres import _load_migration_files, apply_migrations

pytestmark = pytest.mark.postgres


def test_migrations_are_loaded_from_the_packaged_resource():
    """The SQL ships inside `factory.store.migrations`, not a checkout-relative path."""
    files = _load_migration_files()
    assert files, "expected at least one packaged migration"
    names = [name for name, _ in files]
    assert names == sorted(names)
    assert all(name.endswith(".sql") for name in names)


def test_apply_migrations_raises_when_no_sql_files_found(tmp_path, pg_dsn):
    with psycopg.connect(pg_dsn) as conn:
        with pytest.raises(RuntimeError):
            apply_migrations(conn, migrations_dir=tmp_path)


def test_apply_migrations_is_idempotent(pg_dsn):
    with psycopg.connect(pg_dsn) as conn:
        apply_migrations(conn)  # make sure it's applied at least once (idempotent either way)
        second = apply_migrations(conn)
    assert second == []


def test_concurrent_apply_migrations_is_serialized_by_the_advisory_lock(pg_dsn):
    """Two controllers starting at once must not race inserting into `schema_migrations`.

    Without the advisory lock, two concurrent callers can both see "not yet
    applied", and the second's INSERT into the `schema_migrations` primary key
    fails. With the lock, one waits for the other and then just sees it's done.
    """
    with psycopg.connect(pg_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS schema_migrations")
        conn.commit()

    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            with psycopg.connect(pg_dsn) as conn:
                apply_migrations(conn)
        except Exception as exc:  # noqa: BLE001 - surfaced via `errors`, not raised in-thread
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    with psycopg.connect(pg_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM schema_migrations WHERE filename = %s", ("0001_init.sql",))
            assert cur.fetchone()[0] == 1
