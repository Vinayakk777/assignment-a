from __future__ import annotations

import sqlite3

import pytest

from au_ledger.repository import (
    count_orphan_events,
    insert_event,
    insert_lead,
    link_event_to_lead,
)
from au_ledger.transactions import transaction

from conftest import make_ingest_file


def _insert_event(conn, file_id: int, **overrides):
    values = {
        "event_id": "evt_001",
        "source_file_id": file_id,
        "line_number": 1,
        "workflow_id": "motor_renewal",
        "event_type": "call_connected",
        "call_id": "call_001",
        "occurred_at_utc": "2026-01-01T04:00:00+00:00",
        "occurred_at_ist": "2026-01-01T09:30:00+05:30",
        "occurred_date_ist": "2026-01-01",
        "payload": {"status": "connected"},
    }
    values.update(overrides)
    return insert_event(conn, **values)


def _insert_lead(
    conn,
    hasher,
    cipher,
    *,
    phone: str,
    external_ref: str,
    workflow_id: str = "motor_renewal",
):
    with transaction(conn):
        return insert_lead(
            conn,
            workflow_id=workflow_id,
            phone=phone,
            external_ref=external_ref,
            batch_id="batch_001",
            hasher=hasher,
            cipher=cipher,
            pii_fields={"customer_name": "Asha Rao"},
            attributes={"preferred_language": "en"},
        )


def test_events_are_immutable_complete_rows_without_lead_column(seeded_conn, hasher):
    file_id = make_ingest_file(
        seeded_conn, workflow_id="motor_renewal", batch_id="batch_001"
    )
    phone_hmac = hasher.blind_index("9876543210")

    _insert_event(seeded_conn, file_id, phone_hmac=phone_hmac)

    row = seeded_conn.execute(
        "SELECT * FROM events WHERE event_id = 'evt_001'"
    ).fetchone()
    assert row["call_id"] == "call_001"
    assert row["payload_json"] == '{"status":"connected"}'
    assert row["phone_hmac"] == phone_hmac
    assert row["line_number"] == 1

    columns = {
        str(info["name"]) for info in seeded_conn.execute("PRAGMA table_info(events)")
    }
    assert "lead_id" not in columns
    assert "phone" not in columns
    assert "payload_json" in columns


def test_event_update_and_delete_are_blocked_by_triggers(seeded_conn):
    file_id = make_ingest_file(seeded_conn, workflow_id="motor_renewal")
    _insert_event(seeded_conn, file_id)

    with pytest.raises(sqlite3.IntegrityError):
        seeded_conn.execute(
            "UPDATE events SET call_id = 'rewritten' WHERE event_id = 'evt_001'"
        )
    with pytest.raises(sqlite3.IntegrityError):
        seeded_conn.execute("DELETE FROM events WHERE event_id = 'evt_001'")

    row = seeded_conn.execute(
        "SELECT call_id, payload_json FROM events WHERE event_id = 'evt_001'"
    ).fetchone()
    assert row["call_id"] == "call_001"
    assert row["payload_json"] == '{"status":"connected"}'
    assert seeded_conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_duplicate_event_id_is_rejected(seeded_conn):
    file_id = make_ingest_file(seeded_conn, workflow_id="motor_renewal")
    _insert_event(seeded_conn, file_id)

    with pytest.raises(sqlite3.IntegrityError):
        _insert_event(seeded_conn, file_id, line_number=2)


def test_event_requires_an_existing_source_file(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _insert_event(conn, 9999, workflow_id=None)


def test_event_workflow_must_exist(seeded_conn):
    file_id = make_ingest_file(seeded_conn, workflow_id="motor_renewal")

    with pytest.raises(sqlite3.IntegrityError):
        _insert_event(seeded_conn, file_id, workflow_id="missing_workflow")


@pytest.mark.parametrize("bad_hmac", ["f" * 63, "f" * 65, "F" * 64, "z" * 64])
def test_event_rejects_malformed_phone_hmac(seeded_conn, bad_hmac):
    file_id = make_ingest_file(seeded_conn, workflow_id="motor_renewal")

    with pytest.raises(ValueError):
        _insert_event(seeded_conn, file_id, phone_hmac=bad_hmac)

    assert seeded_conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_orphan_event_can_be_resolved_by_a_later_lead(seeded_conn, hasher, cipher):
    file_id = make_ingest_file(
        seeded_conn, workflow_id="motor_renewal", batch_id="batch_001"
    )
    phone_hmac = hasher.blind_index("+91 98765 43210")
    _insert_event(seeded_conn, file_id, phone_hmac=phone_hmac)

    assert count_orphan_events(seeded_conn) == 1
    orphan = seeded_conn.execute(
        "SELECT event_id, phone_hmac FROM orphan_events"
    ).fetchone()
    assert orphan["event_id"] == "evt_001"
    assert orphan["phone_hmac"] == phone_hmac

    lead_id = _insert_lead(
        seeded_conn, hasher, cipher, phone="9876543210", external_ref="ref-001"
    )
    with transaction(seeded_conn):
        link_event_to_lead(
            seeded_conn,
            event_id="evt_001",
            lead_id=lead_id,
            link_method="phone",
        )

    assert count_orphan_events(seeded_conn) == 0
    link = seeded_conn.execute(
        "SELECT lead_id, link_method FROM event_links WHERE event_id = 'evt_001'"
    ).fetchone()
    assert (link["lead_id"], link["link_method"]) == (lead_id, "phone")

    # Resolving an orphan must never rewrite the original event.
    event = seeded_conn.execute(
        "SELECT call_id, payload_json, phone_hmac FROM events WHERE event_id = 'evt_001'"
    ).fetchone()
    assert event["call_id"] == "call_001"
    assert event["payload_json"] == '{"status":"connected"}'
    assert event["phone_hmac"] == phone_hmac


def test_relinking_updates_only_the_link_table(seeded_conn, hasher, cipher):
    file_id = make_ingest_file(seeded_conn, workflow_id="motor_renewal")
    _insert_event(seeded_conn, file_id, phone_hmac=hasher.blind_index("9876543210"))

    first_lead = _insert_lead(
        seeded_conn, hasher, cipher, phone="9876543210", external_ref="ref-001"
    )
    second_lead = _insert_lead(
        seeded_conn, hasher, cipher, phone="9123456780", external_ref="ref-002"
    )

    with transaction(seeded_conn):
        link_event_to_lead(
            seeded_conn,
            event_id="evt_001",
            lead_id=first_lead,
            link_method="phone",
        )
    before = seeded_conn.execute(
        "SELECT call_id, payload_json FROM events WHERE event_id = 'evt_001'"
    ).fetchone()

    with transaction(seeded_conn):
        link_event_to_lead(
            seeded_conn,
            event_id="evt_001",
            lead_id=second_lead,
            link_method="call",
        )

    links = seeded_conn.execute(
        "SELECT lead_id, link_method FROM event_links WHERE event_id = 'evt_001'"
    ).fetchall()
    assert [tuple(row) for row in links] == [(second_lead, "call")]
    after = seeded_conn.execute(
        "SELECT call_id, payload_json FROM events WHERE event_id = 'evt_001'"
    ).fetchone()
    assert tuple(before) == tuple(after)


def test_event_cannot_link_to_a_lead_from_another_workflow(
    seeded_conn, hasher, cipher
):
    file_id = make_ingest_file(seeded_conn, workflow_id="motor_renewal")
    _insert_event(seeded_conn, file_id)

    other_workflow_lead = _insert_lead(
        seeded_conn,
        hasher,
        cipher,
        phone="9123456780",
        external_ref="order-001",
        workflow_id="policy_feedback",
    )

    with pytest.raises(ValueError):
        with transaction(seeded_conn):
            link_event_to_lead(
                seeded_conn,
                event_id="evt_001",
                lead_id=other_workflow_lead,
                link_method="phone",
            )

    assert seeded_conn.execute("SELECT COUNT(*) FROM event_links").fetchone()[0] == 0
    assert count_orphan_events(seeded_conn) == 1


def test_event_phone_is_hashed_but_never_stored(seeded_conn, hasher):
    file_id = make_ingest_file(seeded_conn, workflow_id="motor_renewal")
    _insert_event(
        seeded_conn,
        file_id,
        phone="+91 98765 43210",
        hasher=hasher,
        payload={"duration_seconds": 42},
    )

    row = seeded_conn.execute(
        "SELECT phone_hmac, payload_json FROM events WHERE event_id = 'evt_001'"
    ).fetchone()
    assert row["phone_hmac"] == hasher.blind_index("9876543210")
    assert "9876543210" not in row["payload_json"]
    assert "9876543210" not in row["phone_hmac"]
