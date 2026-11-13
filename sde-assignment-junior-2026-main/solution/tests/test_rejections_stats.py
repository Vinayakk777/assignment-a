from __future__ import annotations

import sqlite3

import pytest

from au_ledger.repository import (
    increment_stat,
    insert_rejection,
    set_stat,
)
from au_ledger.transactions import transaction

from conftest import make_ingest_file


def _rejection(conn, file_id: int, *, row_number: int = 1, workflow_id: str = "motor_renewal"):
    return insert_rejection(
        conn,
        ingest_file_id=file_id,
        workflow_id=workflow_id,
        batch_id="batch_001",
        row_number=row_number,
        reason="invalid_phone",
    )


def _stat_value(conn, scope_type: str, scope_key: str, stat_name: str) -> int:
    return int(
        conn.execute(
            "SELECT stat_value FROM processing_stats "
            "WHERE scope_type = ? AND scope_key = ? AND stat_name = ?",
            (scope_type, scope_key, stat_name),
        ).fetchone()[0]
    )


def test_rejections_are_linked_and_unique_per_file_row(seeded_conn):
    first_file = make_ingest_file(
        seeded_conn, kind="leads", workflow_id="motor_renewal", batch_id="batch_001"
    )
    second_file = make_ingest_file(
        seeded_conn,
        kind="leads",
        workflow_id="motor_renewal",
        batch_id="batch_001",
        suffix="_two",
    )

    rejection_id = _rejection(seeded_conn, first_file, row_number=2)
    row = seeded_conn.execute(
        "SELECT * FROM rejections WHERE id = ?", (rejection_id,)
    ).fetchone()
    assert row["ingest_file_id"] == first_file
    assert row["workflow_id"] == "motor_renewal"
    assert row["row_number"] == 2
    assert row["reason"] == "invalid_phone"

    with pytest.raises(sqlite3.IntegrityError):
        _rejection(seeded_conn, first_file, row_number=2)

    # Another file may reject its own row 2.
    _rejection(seeded_conn, second_file, row_number=2)
    assert seeded_conn.execute("SELECT COUNT(*) FROM rejections").fetchone()[0] == 2


def test_rejections_require_existing_file_and_workflow(seeded_conn):
    file_id = make_ingest_file(seeded_conn, kind="leads", workflow_id="motor_renewal")

    with pytest.raises(sqlite3.IntegrityError):
        _rejection(seeded_conn, 9999)
    with pytest.raises(sqlite3.IntegrityError):
        _rejection(seeded_conn, file_id, workflow_id="missing_workflow")
    with pytest.raises(sqlite3.IntegrityError):
        insert_rejection(
            seeded_conn,
            ingest_file_id=file_id,
            workflow_id="motor_renewal",
            batch_id="batch_001",
            row_number=1,
            reason="",
        )


def test_set_stat_inserts_and_then_updates(conn):
    with transaction(conn):
        set_stat(
            conn,
            scope_type="drop",
            scope_key="drop_001",
            stat_name="files_processed",
            value=1,
        )
    assert _stat_value(conn, "drop", "drop_001", "files_processed") == 1

    with transaction(conn):
        set_stat(
            conn,
            scope_type="drop",
            scope_key="drop_001",
            stat_name="files_processed",
            value=5,
        )
    assert _stat_value(conn, "drop", "drop_001", "files_processed") == 5
    assert conn.execute("SELECT COUNT(*) FROM processing_stats").fetchone()[0] == 1


def test_increment_stat_accumulates_and_scopes_are_independent(conn):
    with transaction(conn):
        increment_stat(conn, scope_type="global", scope_key="all", stat_name="rows")
        increment_stat(conn, scope_type="global", scope_key="all", stat_name="rows", delta=4)
        increment_stat(conn, scope_type="file", scope_key="1", stat_name="rows", delta=2)

    assert _stat_value(conn, "global", "all", "rows") == 5
    assert _stat_value(conn, "file", "1", "rows") == 2
    assert conn.execute("SELECT COUNT(*) FROM processing_stats").fetchone()[0] == 2


def test_invalid_stat_scope_is_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        set_stat(
            conn,
            scope_type="invalid",
            scope_key="x",
            stat_name="rows",
            value=1,
        )


def test_stat_updates_are_transactional(conn):
    with transaction(conn):
        increment_stat(conn, scope_type="global", scope_key="all", stat_name="rows", delta=3)

    with pytest.raises(RuntimeError):
        with transaction(conn):
            increment_stat(conn, scope_type="global", scope_key="all", stat_name="rows")
            raise RuntimeError("rollback")

    assert _stat_value(conn, "global", "all", "rows") == 3
