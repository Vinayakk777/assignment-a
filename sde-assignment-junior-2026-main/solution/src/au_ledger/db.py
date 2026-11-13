"""SQLite connection, migration runner and initialization entry point."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .metadata import seed_metadata
from .migrations import MIGRATIONS
from .util import utc_now_iso


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open SQLite with explicit transactions and foreign keys enabled.

    ``isolation_level=None`` puts sqlite3 in autocommit mode so application
    code controls ``BEGIN``/``COMMIT`` via :func:`au_ledger.transactions.transaction`.
    Foreign-key enforcement is per connection and is always enabled here.
    """
    path = str(db_path)
    conn = sqlite3.connect(path, isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    enabled = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    if not enabled:
        conn.close()
        raise RuntimeError("SQLite foreign-key enforcement could not be enabled")
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Apply all pending migrations. Returns the resulting schema version."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    applied = {
        int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }
    for version, name, sql in MIGRATIONS:
        if version in applied:
            continue
        # executescript handles trigger bodies correctly. Statements are
        # idempotent (IF NOT EXISTS), so a failure between schema creation and
        # version recording is safe to retry.
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
            (version, name, utc_now_iso()),
        )
    return schema_version(conn)


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0] or 0)


def init_db(
    db_path: str | Path,
    schemas_dir: str | Path | None = None,
) -> dict[str, int | str]:
    """Create/migrate the database and optionally seed workflow metadata."""
    conn = connect(db_path)
    try:
        version = migrate(conn)
        counts: dict[str, int | str] = {}
        if schemas_dir is not None:
            counts.update(seed_metadata(conn, schemas_dir))
        return {
            "db_path": str(db_path),
            "schema_version": version,
            **counts,
        }
    finally:
        conn.close()


def table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    return [str(row[0]) for row in rows]
