"""State store adapters (final draft §12.2, module B1).

`open_store(settings)` picks the adapter: `MemoryStateStore` when
`settings.database_url` is unset (local mode / tests), otherwise a
`PostgresStateStore` against that database, after applying migrations.
"""
import psycopg

from ..ports import StateStore
from .memory import MemoryStateStore
from .postgres import PostgresStateStore, apply_migrations

__all__ = ["MemoryStateStore", "PostgresStateStore", "apply_migrations", "open_store"]


def open_store(settings) -> StateStore:
    """Build the `StateStore` for `settings` (in-memory unless `database_url` is set)."""
    if not settings.database_url:
        return MemoryStateStore()
    conn = psycopg.connect(settings.database_url)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    return PostgresStateStore(settings.database_url)
