from __future__ import annotations

import sqlite3

import pytest

from au_ledger.errors import TransactionError
from au_ledger.repository import (
    create_ingest_file,
    insert_event,
    insert_lead,
    insert_rejection,
)
from au_ledger.transactions import transaction

from conftest import make_ingest_file


def _create_file(conn, suffix: str = "") -> int:
    return create_ingest_file(
        conn,
        drop_key="drop_tx",
        drop_name="drop_tx",
        kind="events",
        relative_path=f"events/file{suffix}.jsonl",
        content_sha256="b" * 64,
        nonempty_lines=1,
    )


def test_transaction_commits(conn):
    with transaction(conn):
        _create_file(conn)

    assert conn.in_transaction is False
    assert conn.execute("SELECT COUNT(*) FROM ingest_files").fetchone()[0] == 1


def test_exception_rolls_back_every_write(conn):
    with pytest.raises(RuntimeError):
        with transaction(conn):
            _create_file(conn)
            raise RuntimeError("simulated crash")

    assert conn.in_transaction is False
    assert conn.execute("SELECT COUNT(*) FROM ingest_files").fetchone()[0] == 0


def test_constraint_failure_rolls_back_the_whole_file(seeded_conn):
    file_id = make_ingest_file(
        seeded_conn, workflow_id="motor_renewal", batch_id="batch_tx"
    )

    with pytest.raises(sqlite3.IntegrityError):
        with transaction(seeded_conn):
            insert_rejection(
                seeded_conn,
                ingest_file_id=file_id,
                workflow_id="motor_renewal",
                batch_id="batch_tx",
                row_number=1,
                reason="valid_row_accepted",
            )
            insert_rejection(
                seeded_conn,
                ingest_file_id=file_id,
                workflow_id="does_not_exist",
                batch_id="batch_tx",
                row_number=2,
                reason="foreign_key_failure",
            )

    assert seeded_conn.in_transaction is False
    assert seeded_conn.execute("SELECT COUNT(*) FROM rejections").fetchone()[0] == 0
    # The file row was created before the transaction and is still retryable.
    assert seeded_conn.execute("SELECT COUNT(*) FROM ingest_files").fetchone()[0] == 1


def test_event_and_lead_are_atomic(seeded_conn, hasher, cipher):
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(seeded_conn):
            insert_lead(
                seeded_conn,
                workflow_id="motor_renewal",
                phone="9876543210",
                external_ref="ref-001",
                batch_id="batch_tx",
                hasher=hasher,
                cipher=cipher,
                pii_fields={"customer_name": "Asha Rao"},
            )
            insert_event(
                seeded_conn,
                event_id="evt_tx",
                source_file_id=9999,  # missing parent forces rollback
                line_number=1,
                workflow_id="motor_renewal",
            )

    assert seeded_conn.in_transaction is False
    assert seeded_conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0
    assert seeded_conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert seeded_conn.execute("SELECT COUNT(*) FROM lead_pii").fetchone()[0] == 0


def test_insert_lead_requires_an_active_transaction(seeded_conn, hasher, cipher):
    with pytest.raises(TransactionError):
        insert_lead(
            seeded_conn,
            workflow_id="motor_renewal",
            phone="9876543210",
            external_ref="ref-001",
            batch_id="batch_tx",
            hasher=hasher,
            cipher=cipher,
            pii_fields={"customer_name": "Asha Rao"},
        )
    assert seeded_conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0


def test_nested_transactions_are_rejected(conn):
    with transaction(conn):
        _create_file(conn)
        with pytest.raises(TransactionError):
            with transaction(conn):
                _create_file(conn, suffix="_nested")

    assert conn.in_transaction is False
    assert conn.execute("SELECT COUNT(*) FROM ingest_files").fetchone()[0] == 1
