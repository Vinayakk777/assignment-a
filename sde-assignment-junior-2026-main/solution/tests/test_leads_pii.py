from __future__ import annotations

import sqlite3

import pytest

from au_ledger.crypto import canonical_phone, mask_phone
from au_ledger.repository import (
    get_lead_by_phone,
    get_lead_pii,
    insert_event,
    insert_lead,
)
from au_ledger.transactions import transaction

from conftest import make_ingest_file

NAME = "Asha Rao"
PHONE = "9876543210"


def _insert_lead(
    conn,
    hasher,
    cipher,
    *,
    workflow_id: str = "motor_renewal",
    phone: str = PHONE,
    external_ref: str = "ref-001",
    attributes: dict | None = None,
    pii_fields: dict | None = None,
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
            pii_fields={"customer_name": NAME} if pii_fields is None else pii_fields,
            attributes=(
                {"preferred_language": "en"} if attributes is None else attributes
            ),
        )


def test_lead_stores_only_hmac_and_authenticated_ciphertext(seeded_conn, hasher, cipher):
    lead_id = _insert_lead(seeded_conn, hasher, cipher)

    lead = seeded_conn.execute(
        "SELECT * FROM leads WHERE id = ?", (lead_id,)
    ).fetchone()
    assert lead["phone_hmac"] == hasher.blind_index(PHONE)
    assert PHONE not in lead["phone_hmac"]
    assert NAME not in lead["attributes_json"]
    assert PHONE not in lead["attributes_json"]
    assert canonical_phone(PHONE) not in lead["attributes_json"]

    pii = {
        row["field_name"]: row["ciphertext"]
        for row in seeded_conn.execute(
            "SELECT field_name, ciphertext FROM lead_pii WHERE lead_id = ?", (lead_id,)
        )
    }
    assert set(pii) == {"phone", "customer_name"}
    for token in pii.values():
        assert token.startswith("v1:")
        assert NAME not in token
        assert PHONE not in token

    assert get_lead_pii(
        seeded_conn, lead_id=lead_id, field_name="phone", cipher=cipher
    ) == canonical_phone(PHONE)
    assert get_lead_pii(
        seeded_conn, lead_id=lead_id, field_name="customer_name", cipher=cipher
    ) == NAME
    assert (
        get_lead_pii(
            seeded_conn, lead_id=lead_id, field_name="unknown", cipher=cipher
        )
        is None
    )


def test_database_dump_contains_no_plaintext_pii(seeded_conn, hasher, cipher):
    _insert_lead(seeded_conn, hasher, cipher)
    dump = "\n".join(seeded_conn.iterdump())

    assert PHONE not in dump
    assert canonical_phone(PHONE) not in dump
    assert NAME not in dump
    # Schema metadata still names the PII fields; only values are protected.
    assert "customer_name" in dump


def test_phone_lookup_uses_the_blind_index(seeded_conn, hasher, cipher):
    lead_id = _insert_lead(seeded_conn, hasher, cipher)

    found = get_lead_by_phone(
        seeded_conn,
        workflow_id="motor_renewal",
        phone="+91 98765 43210",
        hasher=hasher,
    )
    assert found is not None
    assert found["id"] == lead_id

    assert (
        get_lead_by_phone(
            seeded_conn,
            workflow_id="motor_renewal",
            phone="9123456780",
            hasher=hasher,
        )
        is None
    )


def test_lead_identity_is_unique_per_workflow(seeded_conn, hasher, cipher):
    first = _insert_lead(seeded_conn, hasher, cipher, external_ref="ref-001")

    with pytest.raises(sqlite3.IntegrityError):
        _insert_lead(seeded_conn, hasher, cipher, external_ref="ref-002")

    other = _insert_lead(
        seeded_conn,
        hasher,
        cipher,
        workflow_id="policy_feedback",
        external_ref="order-001",
    )
    assert other != first
    assert (
        seeded_conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 2
    )


@pytest.mark.parametrize(
    "attributes",
    [
        {"customer_name": NAME},
        {"Customer_Name": NAME},
        {"PHONE": PHONE},
        {"contact": PHONE},
        {"insured name": NAME},
        {"meta": {"customer_name": NAME}},
    ],
)
def test_pii_must_not_be_placed_in_plain_attributes(
    seeded_conn, hasher, cipher, attributes
):
    with pytest.raises(ValueError):
        _insert_lead(seeded_conn, hasher, cipher, attributes=attributes)
    assert seeded_conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"customer_name": NAME},
        {"phone": PHONE},
        {"contact": PHONE},
        {"nested": {"customer_name": NAME}},
        {"items": [{"phone": PHONE}]},
    ],
)
def test_event_payload_rejects_pii_keys(seeded_conn, payload):
    file_id = make_ingest_file(seeded_conn, workflow_id="motor_renewal")

    with pytest.raises(ValueError):
        insert_event(
            seeded_conn,
            event_id="evt_pii",
            source_file_id=file_id,
            line_number=1,
            workflow_id="motor_renewal",
            payload=payload,
        )
    assert seeded_conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_invalid_phone_errors_do_not_echo_the_value():
    bad_phone = "1234567890"
    with pytest.raises(ValueError) as excinfo:
        canonical_phone(bad_phone)
    assert bad_phone not in str(excinfo.value)
    assert "invalid Indian mobile number" in str(excinfo.value)


def test_mask_phone_is_safe_for_logs():
    masked = mask_phone("+91 98765 43210")
    assert masked == "+91******3210"
    assert PHONE not in masked
    assert "98765" not in masked
