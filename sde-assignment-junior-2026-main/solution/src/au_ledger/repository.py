"""Database primitives shared by the ingestion pipeline.

Discovery, parsing, validation, merging, and file orchestration live in
:mod:`au_ledger.ingestion`; these helpers provide safe, transactional write and
read primitives for that layer.

All functions expect the caller to supply a transaction
(:func:`au_ledger.transactions.transaction`) when writing multiple rows.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from typing import Any

from .crypto import PiiCipher, PhoneHasher, canonical_phone
from .errors import TransactionError
from .util import json_dumps, utc_now_iso

_PII_KEY_NAMES = {"phone", "customer_name"}
_HMAC_RE = re.compile(r"^[0-9a-f]{64}$")


def create_ingest_file(
    conn: sqlite3.Connection,
    *,
    drop_key: str,
    drop_name: str,
    kind: str,
    relative_path: str,
    content_sha256: str,
    workflow_id: str | None = None,
    batch_id: str | None = None,
    nonempty_lines: int = 0,
    processed_at: str | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO ingest_files(
            drop_key, drop_name, kind, relative_path, content_sha256,
            workflow_id, batch_id, nonempty_lines, processed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            drop_key,
            drop_name,
            kind,
            relative_path,
            content_sha256,
            workflow_id,
            batch_id,
            nonempty_lines,
            processed_at or utc_now_iso(),
        ),
    )
    return int(cursor.lastrowid)


def insert_lead(
    conn: sqlite3.Connection,
    *,
    workflow_id: str,
    phone: str,
    external_ref: str,
    batch_id: str,
    hasher: PhoneHasher,
    cipher: PiiCipher,
    pii_fields: Mapping[str, str] | None = None,
    attributes: Mapping[str, Any] | None = None,
    phone_field_name: str = "phone",
) -> int:
    """Insert a lead with encrypted PII.

    ``pii_fields`` must contain every non-phone PII value (for the supplied
    schemas that is ``customer_name``). ``attributes`` must contain only
    non-PII values; passing a field marked ``pii=true`` there is rejected.
    ``phone_field_name`` lets a schema name its phone field something other
    than ``phone``; the ciphertext is stored under that schema field name.
    """
    if not conn.in_transaction:
        raise TransactionError("insert_lead requires an active transaction")
    canonical = canonical_phone(phone)
    phone_hmac = hasher.blind_index(canonical)
    pii_values = dict(pii_fields or {})
    non_pii_attributes = dict(attributes or {})

    if phone_field_name in pii_values:
        raise ValueError("phone is managed separately and cannot be supplied in pii_fields")
    pii_names = (
        _pii_field_names(conn, workflow_id)
        | _PII_KEY_NAMES
        | {str(name).lower() for name in pii_values}
    )
    _reject_pii_keys(non_pii_attributes, pii_names)

    now = utc_now_iso()
    cursor = conn.execute(
        """
        INSERT INTO leads(
            workflow_id, phone_hmac, external_ref, batch_id,
            attributes_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            workflow_id,
            phone_hmac,
            external_ref,
            batch_id,
            json_dumps(non_pii_attributes),
            now,
            now,
        ),
    )
    lead_id = int(cursor.lastrowid)

    conn.execute(
        "INSERT INTO lead_pii(lead_id, field_name, ciphertext) VALUES (?, ?, ?)",
        (lead_id, phone_field_name, cipher.encrypt(canonical, field_name=phone_field_name)),
    )
    for field_name, value in pii_values.items():
        if not isinstance(value, str):
            raise TypeError("PII value must be a string")
        conn.execute(
            "INSERT INTO lead_pii(lead_id, field_name, ciphertext) VALUES (?, ?, ?)",
            (lead_id, field_name, cipher.encrypt(value, field_name=field_name)),
        )
    return lead_id


def update_lead_fields(
    conn: sqlite3.Connection,
    *,
    lead_id: int,
    external_ref: str,
    attributes: Mapping[str, Any],
    updated_at: str | None = None,
) -> None:
    """Apply a merged field set without changing identity or first batch."""
    conn.execute(
        """
        UPDATE leads
        SET external_ref = ?, attributes_json = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            external_ref,
            json_dumps(dict(attributes)),
            updated_at or utc_now_iso(),
            lead_id,
        ),
    )


def upsert_lead_pii(
    conn: sqlite3.Connection,
    *,
    lead_id: int,
    field_name: str,
    value: str,
    cipher: PiiCipher,
) -> None:
    """Encrypt and replace one PII value for an existing lead."""
    token = cipher.encrypt(value, field_name=field_name)
    conn.execute(
        """
        INSERT INTO lead_pii(lead_id, field_name, ciphertext)
        VALUES (?, ?, ?)
        ON CONFLICT(lead_id, field_name) DO UPDATE SET
            ciphertext = excluded.ciphertext
        """,
        (lead_id, field_name, token),
    )


def get_lead_pii_field_names(conn: sqlite3.Connection, lead_id: int) -> set[str]:
    """Return the PII field names already stored for a lead."""
    rows = conn.execute(
        "SELECT field_name FROM lead_pii WHERE lead_id = ?", (lead_id,)
    ).fetchall()
    return {str(row[0]) for row in rows}


def insert_event(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    source_file_id: int,
    line_number: int,
    event_type: str | None = None,
    workflow_id: str | None = None,
    call_id: str | None = None,
    occurred_at_utc: str | None = None,
    occurred_at_ist: str | None = None,
    occurred_date_ist: str | None = None,
    phone: str | None = None,
    phone_hmac: str | None = None,
    payload: Mapping[str, Any] | None = None,
    hasher: PhoneHasher | None = None,
    ingested_at: str | None = None,
) -> int:
    """Append one immutable event.

    The raw phone is never stored. Pass ``phone`` to compute the HMAC, or
    ``phone_hmac`` directly when the event's phone is invalid (the ingestion
    layer will do the latter to preserve an orphan). Payloads containing
    obvious PII keys are rejected.
    """
    if phone_hmac is None and phone:
        if hasher is None:
            raise ValueError("a PhoneHasher is required when phone is supplied")
        phone_hmac = hasher.blind_index(phone)
    if phone_hmac is not None and not _HMAC_RE.fullmatch(phone_hmac):
        raise ValueError("phone_hmac must be a 64-character lowercase hex digest")

    payload_value = dict(payload or {})
    pii_names = set(_PII_KEY_NAMES)
    if workflow_id is not None:
        pii_names |= _pii_field_names(conn, workflow_id)
    _reject_pii_keys(payload_value, pii_names)

    cursor = conn.execute(
        """
        INSERT INTO events(
            event_id, workflow_id, call_id, event_type,
            occurred_at_utc, occurred_at_ist, occurred_date_ist,
            phone_hmac, payload_json, source_file_id, line_number, ingested_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            workflow_id,
            call_id,
            event_type,
            occurred_at_utc,
            occurred_at_ist,
            occurred_date_ist,
            phone_hmac,
            json_dumps(payload_value),
            source_file_id,
            line_number,
            ingested_at or utc_now_iso(),
        ),
    )
    return int(cursor.lastrowid)


def link_event_to_lead(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    lead_id: int,
    link_method: str,
    resolved_at: str | None = None,
) -> None:
    """Create or update derived linkage; the event row remains untouched."""
    scope = conn.execute(
        """
        SELECT e.workflow_id AS event_workflow, l.workflow_id AS lead_workflow
        FROM events AS e
        JOIN leads AS l ON l.id = ?
        WHERE e.event_id = ?
        """,
        (lead_id, event_id),
    ).fetchone()
    if scope is None:
        raise ValueError("event or lead does not exist")
    event_workflow, lead_workflow = scope[0], scope[1]
    if event_workflow is not None and event_workflow != lead_workflow:
        raise ValueError("event and lead belong to different workflows")

    conn.execute(
        """
        INSERT INTO event_links(event_id, lead_id, link_method, resolved_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(event_id) DO UPDATE SET
            lead_id = excluded.lead_id,
            link_method = excluded.link_method,
            resolved_at = excluded.resolved_at
        """,
        (event_id, lead_id, link_method, resolved_at or utc_now_iso()),
    )


def insert_rejection(
    conn: sqlite3.Connection,
    *,
    ingest_file_id: int,
    workflow_id: str,
    batch_id: str,
    row_number: int,
    reason: str,
    created_at: str | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO rejections(
            ingest_file_id, workflow_id, batch_id, row_number, reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (ingest_file_id, workflow_id, batch_id, row_number, reason, created_at or utc_now_iso()),
    )
    return int(cursor.lastrowid)


def set_stat(
    conn: sqlite3.Connection,
    *,
    scope_type: str,
    scope_key: str,
    stat_name: str,
    value: int,
    updated_at: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO processing_stats(scope_type, scope_key, stat_name, stat_value, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(scope_type, scope_key, stat_name) DO UPDATE SET
            stat_value = excluded.stat_value,
            updated_at = excluded.updated_at
        """,
        (scope_type, scope_key, stat_name, value, updated_at or utc_now_iso()),
    )


def increment_stat(
    conn: sqlite3.Connection,
    *,
    scope_type: str,
    scope_key: str,
    stat_name: str,
    delta: int = 1,
    updated_at: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO processing_stats(scope_type, scope_key, stat_name, stat_value, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(scope_type, scope_key, stat_name) DO UPDATE SET
            stat_value = processing_stats.stat_value + excluded.stat_value,
            updated_at = excluded.updated_at
        """,
        (scope_type, scope_key, stat_name, delta, updated_at or utc_now_iso()),
    )


def get_lead_pii(
    conn: sqlite3.Connection,
    *,
    lead_id: int,
    field_name: str,
    cipher: PiiCipher,
) -> str | None:
    row = conn.execute(
        "SELECT ciphertext FROM lead_pii WHERE lead_id = ? AND field_name = ?",
        (lead_id, field_name),
    ).fetchone()
    if row is None:
        return None
    return cipher.decrypt(str(row[0]), field_name=field_name)


def get_lead_by_phone(
    conn: sqlite3.Connection,
    *,
    workflow_id: str,
    phone: str,
    hasher: PhoneHasher,
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM leads WHERE workflow_id = ? AND phone_hmac = ?",
        (workflow_id, hasher.blind_index(phone)),
    ).fetchone()


def count_orphan_events(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM orphan_events").fetchone()[0])


def _pii_field_names(conn: sqlite3.Connection, workflow_id: str) -> set[str]:
    """Return canonical PII names and their aliases for a workflow."""
    rows = conn.execute(
        "SELECT name, aliases_json FROM workflow_fields "
        "WHERE workflow_id = ? AND pii = 1",
        (workflow_id,),
    ).fetchall()
    names: set[str] = set()
    for row in rows:
        names.add(str(row[0]).strip().lower())
        try:
            aliases = json.loads(row[1] or "[]")
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(aliases, list):
            names.update(
                str(alias).strip().lower() for alias in aliases if str(alias).strip()
            )
    return names


def _reject_pii_keys(value: Any, pii_names: set[str]) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).lower() in pii_names:
                raise ValueError("payload contains a field marked as PII")
            _reject_pii_keys(nested, pii_names)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_pii_keys(nested, pii_names)
