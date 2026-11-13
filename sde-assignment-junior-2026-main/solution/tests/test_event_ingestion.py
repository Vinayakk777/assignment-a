from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from au_ledger.event_ingestion import (
    discover_event_files,
    ingest_event_drop,
    ingest_event_drops,
    parse_event_timestamp,
)
from au_ledger.event_resolution import (
    count_orphan_events_for_drop,
    count_unknown_disposition_events,
    refresh_event_quality_stats,
    resolve_orphan_events,
)
from au_ledger.errors import IngestError
from au_ledger.ingestion import ingest_drop
from au_ledger.repository import count_orphan_events
from au_ledger.transactions import transaction

MOTOR_HEADER = [
    "customer_name",
    "phone",
    "policy_number",
    "policy_expiry",
    "premium_amount",
    "preferred_language",
]

FEEDBACK_HEADER = [
    "customer_name",
    "phone",
    "order_id",
    "product",
    "order_date",
    "preferred_language",
]


def write_lead_file(
    drop_dir: Path,
    workflow_id: str,
    name: str,
    header: list[str],
    rows: list[list[str]],
) -> Path:
    path = drop_dir / "leads" / workflow_id / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return path


def write_event_file(drop_dir: Path, name: str, lines: list[str]) -> Path:
    path = drop_dir / "events" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def event(
    event_id: str,
    event_type: str,
    *,
    workflow_id: str | None = "motor_renewal",
    call_id: str | None = "call_1",
    occurred_at: str | None = "2026-10-01T10:00:00Z",
    phone: str | None = "9876543210",
    payload: dict | None = None,
    **extra,
) -> str:
    document: dict[str, object] = {"event_id": event_id, "event_type": event_type}
    if workflow_id is not None:
        document["workflow_id"] = workflow_id
    if call_id is not None:
        document["call_id"] = call_id
    if occurred_at is not None:
        document["occurred_at"] = occurred_at
    if phone is not None:
        document["phone"] = phone
    document["payload"] = {} if payload is None else payload
    document.update(extra)
    return json.dumps(document)


def seed_one_lead(tmp_path, seeded_conn, schemas, hasher, cipher, *, phone="9876543210"):
    drop = tmp_path / "leads_drop"
    write_lead_file(
        drop,
        "motor_renewal",
        "batch_001.csv",
        MOTOR_HEADER,
        [["Asha Rao", phone, "POL-1", "", "", "en"]],
    )
    ingest_drop(seeded_conn, drop, schemas, hasher=hasher, cipher=cipher)


def stat_value(conn, scope_type: str, scope_key: str, stat_name: str) -> int | None:
    row = conn.execute(
        """
        SELECT stat_value FROM processing_stats
        WHERE scope_type = ? AND scope_key = ? AND stat_name = ?
        """,
        (scope_type, scope_key, stat_name),
    ).fetchone()
    return None if row is None else int(row[0])


def event_rows(conn):
    return conn.execute("SELECT * FROM events ORDER BY id").fetchall()


def file_nonempty_lines(conn) -> int:
    row = conn.execute(
        "SELECT nonempty_lines FROM ingest_files WHERE kind = 'events'"
    ).fetchone()
    return int(row[0])


def links_by_event(conn) -> dict[str, tuple[int, str]]:
    return {
        str(row["event_id"]): (int(row["lead_id"]), str(row["link_method"]))
        for row in conn.execute(
            "SELECT event_id, lead_id, link_method FROM event_links ORDER BY event_id"
        ).fetchall()
    }


def snapshot(conn) -> dict[str, object]:
    tables = ("events", "event_links", "ingest_files", "processing_stats")
    return {
        "counts": {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        },
        "stats": [
            tuple(row)
            for row in conn.execute(
                """
                SELECT scope_type, scope_key, stat_name, stat_value
                FROM processing_stats
                ORDER BY scope_type, scope_key, stat_name
                """
            ).fetchall()
        ],
    }


def test_all_six_event_types_are_stored_and_linked(
    tmp_path, seeded_conn, schemas, hasher, cipher
):
    seed_one_lead(tmp_path, seeded_conn, schemas, hasher, cipher)
    drop = tmp_path / "drop_1"
    write_event_file(
        drop,
        "events_0001.jsonl",
        [
            event("evt_1", "call.initiated"),
            event(
                "evt_2",
                "call.telephony_failed",
                payload={"reason": "sip_503"},
            ),
            event("evt_3", "call.connected", call_id="call_1"),
            event(
                "evt_4",
                "call.ended",
                payload={"duration_sec": 42, "end_reason": "completed"},
            ),
            event(
                "evt_5",
                "disposition.set",
                payload={"disposition_code": "renewed"},
            ),
            event(
                "evt_6",
                "disposition.override",
                phone=None,
                payload={
                    "disposition_code": "not_interested",
                    "overridden_by": "supervisor_1",
                    "reason": "qa_review",
                },
            ),
        ],
    )

    result = ingest_event_drop(seeded_conn, drop, schemas, hasher=hasher)

    assert result.files_seen == 1
    assert result.files_processed == 1
    assert result.files_skipped == 0
    assert result.events_total == 6
    assert result.malformed_events == 0
    assert result.duplicate_events_dropped == 0
    assert result.events_inserted == 6
    assert result.orphan_events == 0
    assert result.unknown_disposition_events == 0

    rows = event_rows(seeded_conn)
    assert {row["event_type"] for row in rows} == {
        "call.initiated",
        "call.telephony_failed",
        "call.connected",
        "call.ended",
        "disposition.set",
        "disposition.override",
    }
    assert all(row["occurred_at_utc"].endswith("+00:00") for row in rows)
    assert all(row["occurred_at_ist"].endswith("+05:30") for row in rows)

    links = links_by_event(seeded_conn)
    assert len(links) == 6
    assert all(links[f"evt_{index}"][1] == "phone" for index in range(1, 6))
    assert links["evt_6"][1] == "call"
    assert count_orphan_events(seeded_conn) == 0


def test_malformed_lines_are_counted_skipped_and_do_not_abort_the_file(
    tmp_path, seeded_conn, schemas, hasher
):
    drop = tmp_path / "drop_1"
    write_event_file(
        drop,
        "events_0001.jsonl",
        [
            "{not json",
            '{"event_type": "call.initiated"}',
            '{"event_id": "", "event_type": "call.initiated"}',
            "[1, 2, 3]",
            event("evt_ok", "call.initiated", phone="9123456780"),
            "",
            "   ",
        ],
    )

    result = ingest_event_drop(seeded_conn, drop, schemas, hasher=hasher)

    assert result.events_total == 5
    assert result.malformed_events == 4
    assert result.duplicate_events_dropped == 0
    assert result.events_inserted == 1
    assert result.orphan_events == 1

    rows = event_rows(seeded_conn)
    assert [row["event_id"] for row in rows] == ["evt_ok"]
    # Physical 1-based line number, with blank lines skipped but still counted
    # in the file's physical layout.
    assert rows[0]["line_number"] == 5
    assert file_nonempty_lines(seeded_conn) == 5


def test_duplicate_event_ids_are_dropped_within_and_across_drops(
    tmp_path, seeded_conn, schemas, hasher
):
    first_drop = tmp_path / "drop_1"
    second_drop = tmp_path / "drop_2"
    repeated = event("evt_1", "call.initiated")
    write_event_file(
        first_drop,
        "events_0001.jsonl",
        [repeated, repeated, event("evt_2", "call.connected")],
    )
    write_event_file(second_drop, "events_1001.jsonl", [repeated])

    first = ingest_event_drop(seeded_conn, first_drop, schemas, hasher=hasher)
    second = ingest_event_drop(seeded_conn, second_drop, schemas, hasher=hasher)

    assert first.events_total == 3
    assert first.duplicate_events_dropped == 1
    assert first.events_inserted == 2
    assert second.events_total == 1
    assert second.duplicate_events_dropped == 1
    assert second.events_inserted == 0

    rows = event_rows(seeded_conn)
    assert [row["event_id"] for row in rows] == ["evt_1", "evt_2"]
    # The original delivery remains the immutable source of truth.
    assert rows[0]["source_file_id"] == rows[1]["source_file_id"]
    assert rows[0]["line_number"] == 1

    assert stat_value(seeded_conn, "global", "all", "events_total") == 4
    assert stat_value(seeded_conn, "global", "all", "duplicate_events_dropped") == 2
    assert stat_value(seeded_conn, "global", "all", "events_inserted") == 2


def test_orphan_event_is_preserved_and_resolved_by_a_late_lead(
    tmp_path, seeded_conn, schemas, hasher, cipher
):
    first_drop = tmp_path / "drop_1"
    write_event_file(
        first_drop,
        "events_0001.jsonl",
        [
            event(
                "evt_late",
                "call.initiated",
                phone="+91 91234 56780",
                payload={"note": "arrived first"},
            )
        ],
    )
    first = ingest_event_drop(seeded_conn, first_drop, schemas, hasher=hasher)

    assert first.events_inserted == 1
    assert first.orphan_events == 1
    assert count_orphan_events(seeded_conn) == 1
    assert count_orphan_events_for_drop(seeded_conn, "drop_1") == 1
    before = event_rows(seeded_conn)[0]

    second_drop = tmp_path / "drop_2"
    write_lead_file(
        second_drop,
        "motor_renewal",
        "batch_002.csv",
        MOTOR_HEADER,
        [["Ravi Kumar", "9123456780", "POL-2", "", "", "hi"]],
    )
    ingest_drop(seeded_conn, second_drop, schemas, hasher=hasher, cipher=cipher)

    assert count_orphan_events(seeded_conn) == 0
    assert count_orphan_events_for_drop(seeded_conn, "drop_1") == 0
    links = links_by_event(seeded_conn)
    assert links["evt_late"][1] == "phone"
    assert stat_value(seeded_conn, "global", "all", "orphan_events") == 0
    assert stat_value(seeded_conn, "drop", "drop_1", "orphan_events") == 0

    after = event_rows(seeded_conn)[0]
    assert tuple(before) == tuple(after)


def test_overrides_link_through_call_id_to_the_call_lead(
    tmp_path, seeded_conn, schemas, hasher, cipher
):
    seed_one_lead(tmp_path, seeded_conn, schemas, hasher, cipher)
    first_drop = tmp_path / "drop_1"
    write_event_file(
        first_drop,
        "events_0001.jsonl",
        [
            # Arrives before the call event it depends on, in the same file.
            event(
                "evt_override_first",
                "disposition.override",
                call_id="call_a",
                phone=None,
                payload={"disposition_code": "dnd", "overridden_by": "sup_1"},
            ),
            event("evt_initiated", "call.initiated", call_id="call_a"),
            event(
                "evt_override_no_call",
                "disposition.override",
                call_id="call_missing",
                phone=None,
                payload={"disposition_code": "dnd"},
            ),
            # A phone on an override must never be used for linking.
            event(
                "evt_override_phone_only",
                "disposition.override",
                call_id="call_missing_2",
                phone="9876543210",
                payload={"disposition_code": "dnd"},
            ),
        ],
    )

    result = ingest_event_drop(seeded_conn, first_drop, schemas, hasher=hasher)

    assert result.events_inserted == 4
    assert result.orphan_events == 2
    links = links_by_event(seeded_conn)
    assert links["evt_initiated"][1] == "phone"
    assert links["evt_override_first"][1] == "call"
    assert links["evt_override_first"][0] == links["evt_initiated"][0]
    assert "evt_override_no_call" not in links
    assert "evt_override_phone_only" not in links

    # A later call event resolves the override that had no earlier evidence.
    second_drop = tmp_path / "drop_2"
    write_event_file(
        second_drop,
        "events_1001.jsonl",
        [event("evt_later_call", "call.initiated", call_id="call_missing")],
    )
    second = ingest_event_drop(seeded_conn, second_drop, schemas, hasher=hasher)

    assert second.orphan_events == 1  # only evt_override_phone_only
    links = links_by_event(seeded_conn)
    assert links["evt_override_no_call"][1] == "call"
    assert links["evt_override_no_call"][0] == links["evt_later_call"][0]
    assert "evt_override_phone_only" not in links


def test_unknown_dispositions_are_stored_counted_once_and_case_sensitive(
    tmp_path, seeded_conn, schemas, hasher, cipher
):
    seed_one_lead(tmp_path, seeded_conn, schemas, hasher, cipher)
    drop = tmp_path / "drop_1"
    write_event_file(
        drop,
        "events_0001.jsonl",
        [
            event(
                "evt_known",
                "disposition.set",
                payload={"disposition_code": "renewed"},
            ),
            event(
                "evt_unknown_1",
                "disposition.set",
                payload={"disposition_code": "RENEWD"},
            ),
            event(
                "evt_unknown_2",
                "disposition.set",
                payload={"disposition_code": "RENEWED"},
            ),
            event(
                "evt_unknown_override",
                "disposition.override",
                phone=None,
                payload={"disposition_code": "call_back"},
            ),
            event(
                "evt_unknown_unknown",
                "disposition.set",
                payload={"disposition_code": "unknown"},
            ),
        ],
    )

    result = ingest_event_drop(seeded_conn, drop, schemas, hasher=hasher)

    assert result.events_inserted == 5
    assert result.orphan_events == 0
    assert result.unknown_disposition_events == 4
    assert count_unknown_disposition_events(seeded_conn) == 4
    assert count_unknown_disposition_events(seeded_conn, drop_key="drop_1") == 4
    assert stat_value(seeded_conn, "global", "all", "unknown_disposition_events") == 4
    assert stat_value(seeded_conn, "drop", "drop_1", "unknown_disposition_events") == 4

    # Unknown codes are still preserved in the append-only ledger exactly once.
    rows = event_rows(seeded_conn)
    assert len(rows) == 5
    assert len({row["event_id"] for row in rows}) == 5
    assert seeded_conn.execute("SELECT COUNT(*) FROM dispositions").fetchone()[0] == 14


def test_unknown_disposition_orphan_counts_as_orphan_then_as_unknown(
    tmp_path, seeded_conn, schemas, hasher, cipher
):
    first_drop = tmp_path / "drop_1"
    write_event_file(
        first_drop,
        "events_0001.jsonl",
        [
            event(
                "evt_unknown_late",
                "disposition.set",
                phone="9000000001",
                payload={"disposition_code": "RENEWD"},
            )
        ],
    )
    first = ingest_event_drop(seeded_conn, first_drop, schemas, hasher=hasher)

    assert first.orphan_events == 1
    assert first.unknown_disposition_events == 0
    assert count_unknown_disposition_events(seeded_conn) == 0

    second_drop = tmp_path / "drop_2"
    write_lead_file(
        second_drop,
        "motor_renewal",
        "batch_002.csv",
        MOTOR_HEADER,
        [["Late Lead", "9000000001", "POL-9", "", "", ""]],
    )
    ingest_drop(seeded_conn, second_drop, schemas, hasher=hasher, cipher=cipher)

    assert count_orphan_events(seeded_conn) == 0
    assert count_unknown_disposition_events(seeded_conn) == 1
    assert stat_value(seeded_conn, "global", "all", "orphan_events") == 0
    assert stat_value(seeded_conn, "global", "all", "unknown_disposition_events") == 1


@pytest.mark.parametrize(
    ("raw", "expected_utc", "expected_ist", "expected_date"),
    [
        (
            "2026-10-01T10:00:00Z",
            "2026-10-01T10:00:00+00:00",
            "2026-10-01T15:30:00+05:30",
            "2026-10-01",
        ),
        (
            "2026-10-01T10:00:00+05:30",
            "2026-10-01T04:30:00+00:00",
            "2026-10-01T10:00:00+05:30",
            "2026-10-01",
        ),
        (
            "2026-10-01 10:00:00",
            "2026-10-01T04:30:00+00:00",
            "2026-10-01T10:00:00+05:30",
            "2026-10-01",
        ),
        (
            "2026-09-30T22:00:00Z",
            "2026-09-30T22:00:00+00:00",
            "2026-10-01T03:30:00+05:30",
            "2026-10-01",
        ),
    ],
)
def test_timestamp_forms_normalize_to_utc_and_ist(
    raw, expected_utc, expected_ist, expected_date
):
    assert parse_event_timestamp(raw) == (expected_utc, expected_ist, expected_date)


def test_invalid_or_missing_timestamps_are_null_not_malformed(
    tmp_path, seeded_conn, schemas, hasher
):
    drop = tmp_path / "drop_1"
    write_event_file(
        drop,
        "events_0001.jsonl",
        [
            event("evt_bad_time", "call.initiated", occurred_at="not-a-time"),
            event("evt_no_time", "call.initiated", occurred_at=None),
        ],
    )

    result = ingest_event_drop(seeded_conn, drop, schemas, hasher=hasher)

    assert result.events_total == 2
    assert result.malformed_events == 0
    assert result.events_inserted == 2
    rows = event_rows(seeded_conn)
    assert [(row["occurred_at_utc"], row["occurred_at_ist"], row["occurred_date_ist"]) for row in rows] == [
        (None, None, None),
        (None, None, None),
    ]


def test_event_phones_use_lead_normalization_and_never_store_raw_values(
    tmp_path, seeded_conn, schemas, hasher, cipher
):
    seed_one_lead(tmp_path, seeded_conn, schemas, hasher, cipher)
    drop = tmp_path / "drop_1"
    write_event_file(
        drop,
        "events_0001.jsonl",
        [
            event("evt_v1", "call.initiated", phone="09876543210"),
            event("evt_v2", "call.initiated", phone="91-98765-43210"),
            event("evt_v3", "call.initiated", phone="(+91) 9876543210"),
            event("evt_v4", "call.initiated", phone="+91 98765 43210"),
            event("evt_invalid", "call.initiated", phone="N/A"),
        ],
    )

    result = ingest_event_drop(seeded_conn, drop, schemas, hasher=hasher)

    assert result.events_inserted == 5
    assert result.orphan_events == 1
    rows = event_rows(seeded_conn)
    expected_hmac = hasher.blind_index("9876543210")
    assert [row["phone_hmac"] for row in rows[:4]] == [expected_hmac] * 4
    assert rows[4]["phone_hmac"] is None

    # Raw phone values never appear in event columns or payloads.
    for row in rows:
        stored = " ".join(
            "" if row[column] is None else str(row[column])
            for column in ("workflow_id", "call_id", "phone_hmac", "payload_json")
        )
        assert "9876543210" not in stored
        assert "N/A" not in stored

    assert "phone" not in {
        str(info["name"]) for info in seeded_conn.execute("PRAGMA table_info(events)")
    }


def test_same_phone_in_another_workflow_does_not_link_across_workflows(
    tmp_path, seeded_conn, schemas, hasher, cipher
):
    drop = tmp_path / "drop_1"
    write_lead_file(
        drop,
        "policy_feedback",
        "batch_001.csv",
        FEEDBACK_HEADER,
        [["Asha", "9876543210", "ORD-1", "Earbuds", "2026-09-13", "en"]],
    )
    ingest_drop(seeded_conn, drop, schemas, hasher=hasher, cipher=cipher)
    write_event_file(
        drop,
        "events_0001.jsonl",
        [event("evt_wrong_workflow", "call.initiated", workflow_id="motor_renewal")],
    )

    result = ingest_event_drop(seeded_conn, drop, schemas, hasher=hasher)

    assert result.events_inserted == 1
    assert result.orphan_events == 1
    assert links_by_event(seeded_conn) == {}


def test_unknown_or_missing_workflow_is_preserved_as_an_orphan(
    tmp_path, seeded_conn, schemas, hasher
):
    drop = tmp_path / "drop_1"
    write_event_file(
        drop,
        "events_0001.jsonl",
        [
            event("evt_unknown_wf", "call.initiated", workflow_id="not_a_workflow"),
            event("evt_no_wf", "call.initiated", workflow_id=None),
        ],
    )

    result = ingest_event_drop(seeded_conn, drop, schemas, hasher=hasher)

    assert result.events_inserted == 2
    assert result.orphan_events == 2
    rows = event_rows(seeded_conn)
    assert [row["workflow_id"] for row in rows] == [None, None]


def test_reingesting_an_event_drop_is_a_noop(
    tmp_path, seeded_conn, schemas, hasher, cipher
):
    seed_one_lead(tmp_path, seeded_conn, schemas, hasher, cipher)
    drop = tmp_path / "drop_1"
    write_event_file(
        drop,
        "events_0001.jsonl",
        [
            event("evt_1", "call.initiated"),
            event("evt_2", "disposition.set", payload={"disposition_code": "renewed"}),
            event("evt_3", "call.initiated", phone="9000000009"),
        ],
    )

    first = ingest_event_drop(seeded_conn, drop, schemas, hasher=hasher)
    before = snapshot(seeded_conn)

    second = ingest_event_drop(seeded_conn, drop, schemas, hasher=hasher)
    after = snapshot(seeded_conn)

    assert first.events_inserted == 3
    assert first.orphan_events == 1
    assert second.files_seen == 1
    assert second.files_processed == 0
    assert second.files_skipped == 1
    assert second.events_total == 0
    assert second.events_inserted == 0
    assert after == before


def test_changed_event_file_at_the_same_path_is_rejected_without_writes(
    tmp_path, seeded_conn, schemas, hasher
):
    drop = tmp_path / "drop_1"
    path = write_event_file(drop, "events_0001.jsonl", [event("evt_1", "call.initiated")])
    ingest_event_drop(seeded_conn, drop, schemas, hasher=hasher)
    before = snapshot(seeded_conn)

    path.write_text(
        json.dumps(
            {
                "event_id": "evt_2",
                "event_type": "call.initiated",
                "workflow_id": "motor_renewal",
                "call_id": "call_1",
                "occurred_at": "2026-10-01T10:00:00Z",
                "phone": "9123456780",
                "payload": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(IngestError, match="content changed"):
        ingest_event_drop(seeded_conn, drop, schemas, hasher=hasher)

    assert snapshot(seeded_conn) == before


def test_per_file_drop_and_global_event_stats(tmp_path, seeded_conn, schemas, hasher):
    drop = tmp_path / "drop_1"
    write_event_file(
        drop,
        "events_0001.jsonl",
        [
            event("evt_1", "call.initiated", phone="9000000001"),
            "{bad json",
            event("evt_1", "call.initiated", phone="9000000001"),
        ],
    )
    write_event_file(
        drop,
        "events_0002.jsonl",
        [event("evt_2", "call.initiated", phone="9000000002")],
    )

    result = ingest_event_drop(seeded_conn, drop, schemas, hasher=hasher)

    assert result.files_seen == 2
    assert result.files_processed == 2
    assert result.events_total == 4
    assert result.malformed_events == 1
    assert result.duplicate_events_dropped == 1
    assert result.events_inserted == 2
    assert result.orphan_events == 2

    first_scope = "drop_1/events/events_0001.jsonl"
    second_scope = "drop_1/events/events_0002.jsonl"
    assert stat_value(seeded_conn, "file", first_scope, "events_total") == 3
    assert stat_value(seeded_conn, "file", first_scope, "malformed_events") == 1
    assert stat_value(seeded_conn, "file", first_scope, "duplicate_events_dropped") == 1
    assert stat_value(seeded_conn, "file", first_scope, "events_inserted") == 1
    assert stat_value(seeded_conn, "file", second_scope, "events_total") == 1

    assert stat_value(seeded_conn, "drop", "drop_1", "events_total") == 4
    assert stat_value(seeded_conn, "drop", "drop_1", "malformed_events") == 1
    assert stat_value(seeded_conn, "drop", "drop_1", "duplicate_events_dropped") == 1
    assert stat_value(seeded_conn, "drop", "drop_1", "events_inserted") == 2
    assert stat_value(seeded_conn, "drop", "drop_1", "files_processed") == 2

    assert stat_value(seeded_conn, "global", "all", "events_total") == 4
    assert stat_value(seeded_conn, "global", "all", "malformed_events") == 1
    assert stat_value(seeded_conn, "global", "all", "duplicate_events_dropped") == 1
    assert stat_value(seeded_conn, "global", "all", "events_inserted") == 2
    assert stat_value(seeded_conn, "global", "all", "files_processed") == 2
    assert stat_value(seeded_conn, "global", "all", "orphan_events") == 2
    assert stat_value(seeded_conn, "drop", "drop_1", "orphan_events") == 2


def test_resolution_is_idempotent_and_only_changes_derived_links(
    tmp_path, seeded_conn, schemas, hasher, cipher
):
    seed_one_lead(tmp_path, seeded_conn, schemas, hasher, cipher)
    drop = tmp_path / "drop_1"
    write_event_file(
        drop,
        "events_0001.jsonl",
        [
            event("evt_1", "disposition.override", phone=None),
            event("evt_2", "call.initiated"),
        ],
    )
    ingest_event_drop(seeded_conn, drop, schemas, hasher=hasher)
    events_before = [tuple(row) for row in event_rows(seeded_conn)]
    links_before = links_by_event(seeded_conn)

    with transaction(seeded_conn):
        assert resolve_orphan_events(seeded_conn) == 0
        quality = refresh_event_quality_stats(seeded_conn)

    assert quality.orphan_events == 0
    assert quality.unknown_disposition_events == 1
    assert [tuple(row) for row in event_rows(seeded_conn)] == events_before
    assert links_by_event(seeded_conn) == links_before


def test_discovery_returns_files_in_filename_order_and_missing_events_is_empty(
    tmp_path,
):
    drop = tmp_path / "drop_1"
    (drop / "events").mkdir(parents=True)
    (drop / "events" / "events_0002.jsonl").write_text("", encoding="utf-8")
    (drop / "events" / "events_0001.jsonl").write_text("", encoding="utf-8")
    (drop / "events" / "notes.txt").write_text("ignored", encoding="utf-8")

    assert [path.name for path in discover_event_files(drop)] == [
        "events_0001.jsonl",
        "events_0002.jsonl",
    ]

    empty_drop = tmp_path / "empty_drop"
    empty_drop.mkdir()
    assert discover_event_files(empty_drop) == []

    with pytest.raises(IngestError, match="drop directory not found"):
        discover_event_files(tmp_path / "missing")


def test_ingest_event_drops_processes_drops_in_caller_order(
    tmp_path, seeded_conn, schemas, hasher
):
    first_drop = tmp_path / "first"
    second_drop = tmp_path / "second"
    write_event_file(first_drop, "events_0001.jsonl", [event("evt_1", "call.initiated")])
    write_event_file(second_drop, "events_1001.jsonl", [event("evt_2", "call.initiated")])

    results = ingest_event_drops(
        seeded_conn, [second_drop, first_drop], schemas, hasher=hasher
    )

    assert [result.drop_key for result in results] == ["second", "first"]
    assert [row["event_id"] for row in event_rows(seeded_conn)] == ["evt_2", "evt_1"]


def test_invalid_utf8_event_file_rolls_back_without_writes(
    tmp_path, seeded_conn, schemas, hasher
):
    drop = tmp_path / "drop_1"
    path = drop / "events" / "events_0001.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe\x00\x00invalid")

    with pytest.raises(IngestError, match="not valid UTF-8"):
        ingest_event_drop(seeded_conn, drop, schemas, hasher=hasher)

    assert seeded_conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert seeded_conn.execute("SELECT COUNT(*) FROM ingest_files").fetchone()[0] == 0
    assert seeded_conn.execute("SELECT COUNT(*) FROM processing_stats").fetchone()[0] == 0


def test_event_payload_pii_keys_are_removed_before_storage(
    tmp_path, seeded_conn, schemas, hasher
):
    drop = tmp_path / "drop_1"
    write_event_file(
        drop,
        "events_0001.jsonl",
        [
            event(
                "evt_payload_pii",
                "call.ended",
                payload={
                    "duration_sec": 30,
                    "phone": "9876543210",
                    "customer_name": "Asha Rao",
                    "nested": {"contact": "9876543210", "reason": "completed"},
                },
            )
        ],
    )

    result = ingest_event_drop(seeded_conn, drop, schemas, hasher=hasher)

    assert result.events_inserted == 1
    payload_json = event_rows(seeded_conn)[0]["payload_json"]
    assert "9876543210" not in payload_json
    assert "Asha" not in payload_json
    assert json.loads(payload_json) == {
        "duration_sec": 30,
        "nested": {"reason": "completed"},
    }
