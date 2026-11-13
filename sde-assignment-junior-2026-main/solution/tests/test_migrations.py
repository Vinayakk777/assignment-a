from __future__ import annotations

import sqlite3

import pytest

from au_ledger.db import connect, init_db, migrate, schema_version, table_names
from au_ledger.metadata import seed_metadata

from conftest import SCHEMAS_DIR

REQUIRED_TABLES = {
    "workflows",
    "workflow_fields",
    "dispositions",
    "ingest_files",
    "leads",
    "lead_pii",
    "events",
    "event_links",
    "rejections",
    "processing_stats",
    "schema_migrations",
}


def test_init_db_creates_versioned_schema(tmp_path):
    db_path = tmp_path / "ledger.sqlite"
    result = init_db(db_path, SCHEMAS_DIR)

    assert result["schema_version"] == 1
    assert result["workflows"] == 2
    assert result["fields"] == 12
    assert result["dispositions"] == 14

    connection = connect(db_path)
    try:
        assert schema_version(connection) == 1
        assert REQUIRED_TABLES.issubset(set(table_names(connection)))
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1
    finally:
        connection.close()


def test_migrate_is_idempotent(conn):
    assert schema_version(conn) == 1
    assert migrate(conn) == 1
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1


def test_metadata_seed_is_idempotent_and_reference_fields_are_marked(seeded_conn):
    first = seed_metadata(seeded_conn, SCHEMAS_DIR)
    second = seed_metadata(seeded_conn, SCHEMAS_DIR)
    assert first == second
    assert first == {"workflows": 2, "fields": 12, "dispositions": 14}
    refs = seeded_conn.execute(
        "SELECT workflow_id, name FROM workflow_fields WHERE is_reference = 1 "
        "ORDER BY workflow_id"
    ).fetchall()
    assert [tuple(row) for row in refs] == [
        ("motor_renewal", "policy_number"),
        ("policy_feedback", "order_id"),
    ]
    pii = seeded_conn.execute(
        "SELECT workflow_id, name FROM workflow_fields WHERE pii = 1 "
        "ORDER BY workflow_id, ordinal"
    ).fetchall()
    assert [tuple(row) for row in pii] == [
        ("motor_renewal", "customer_name"),
        ("motor_renewal", "phone"),
        ("policy_feedback", "customer_name"),
        ("policy_feedback", "phone"),
    ]


def test_foreign_keys_are_enforced(seeded_conn):
    with pytest.raises(sqlite3.IntegrityError):
        seeded_conn.execute(
            """
            INSERT INTO rejections(
                ingest_file_id, workflow_id, batch_id, row_number, reason, created_at
            ) VALUES (9999, 'motor_renewal', 'batch_001', 1, 'invalid_phone', 'now')
            """
        )


def test_lead_phone_hmac_format_is_enforced(seeded_conn):
    with pytest.raises(sqlite3.IntegrityError):
        seeded_conn.execute(
            """
            INSERT INTO leads(
                workflow_id, phone_hmac, external_ref, batch_id,
                attributes_json, created_at, updated_at
            ) VALUES ('motor_renewal', ?, 'ref-1', 'batch_1', '{}', 'now', 'now')
            """,
            ("Z" * 64,),
        )


def test_required_indexes_exist(conn):
    names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'ix_%'"
        )
    }
    assert {
        "ix_leads_report",
        "ix_events_call",
        "ix_events_phone",
        "ix_event_links_lead",
        "ix_rejections_sort",
        "ix_ingest_files_kind",
        "ix_processing_stats_lookup",
    }.issubset(names)
