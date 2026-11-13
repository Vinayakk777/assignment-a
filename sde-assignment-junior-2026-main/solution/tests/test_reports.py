"""Business-logic and report-contract tests.

These tests build small ledgers directly through the repository primitives so
each rule can be isolated: effective dispositions, override precedence, bucket
priority, IST daily dates, distinct success leads, current disposition, masking,
sorting, formatting, and report idempotency.
"""

from __future__ import annotations

import csv
import itertools
import json
from pathlib import Path

from au_ledger.event_ingestion import parse_event_timestamp
from au_ledger.reports import (
    BATCH_SUMMARY_HEADER,
    DAILY_OVERVIEW_HEADER,
    LEAD_REPORT_HEADER,
    REJECTIONS_HEADER,
    batch_summary_rows,
    build_report_facts,
    daily_overview_rows,
    data_quality,
    generate_reports,
    lead_report_rows,
    rejection_rows,
)
from au_ledger.repository import (
    insert_event,
    insert_lead,
    insert_rejection,
    link_event_to_lead,
    set_stat,
)
from au_ledger.transactions import transaction

from conftest import make_ingest_file

_SEQUENCE = itertools.count(1)


def add_lead(
    conn,
    hasher,
    cipher,
    *,
    workflow_id: str = "motor_renewal",
    phone: str = "9876543210",
    external_ref: str = "POL-1",
    batch_id: str = "batch_001",
    name: str = "Asha Rao",
) -> int:
    with transaction(conn):
        return insert_lead(
            conn,
            workflow_id=workflow_id,
            phone=phone,
            external_ref=external_ref,
            batch_id=batch_id,
            hasher=hasher,
            cipher=cipher,
            pii_fields={"customer_name": name},
            attributes={},
            phone_field_name="phone",
        )


def add_event(
    conn,
    file_id: int,
    lead_id: int,
    *,
    event_type: str,
    call_id: str = "call_1",
    occurred_at: str = "2026-10-01T10:00:00Z",
    event_id: str | None = None,
    code: str | None = None,
    workflow_id: str = "motor_renewal",
) -> str:
    sequence = next(_SEQUENCE)
    identifier = event_id or f"evt_{sequence:06d}"
    utc, ist, date = parse_event_timestamp(occurred_at)
    payload = {} if code is None else {"disposition_code": code}
    with transaction(conn):
        insert_event(
            conn,
            event_id=identifier,
            source_file_id=file_id,
            line_number=sequence,
            event_type=event_type,
            workflow_id=workflow_id,
            call_id=call_id,
            occurred_at_utc=utc,
            occurred_at_ist=ist,
            occurred_date_ist=date,
            payload=payload,
        )
        link_event_to_lead(
            conn,
            event_id=identifier,
            lead_id=lead_id,
            link_method="call" if event_type == "disposition.override" else "phone",
        )
    return identifier


def add_unlinked_event(
    conn,
    file_id: int,
    hasher,
    *,
    event_type: str,
    call_id: str = "call_1",
    occurred_at: str = "2026-10-01T10:00:00Z",
    phone: str = "9876543210",
    event_id: str | None = None,
    code: str | None = None,
    workflow_id: str = "motor_renewal",
) -> str:
    sequence = next(_SEQUENCE)
    identifier = event_id or f"evt_{sequence:06d}"
    utc, ist, date = parse_event_timestamp(occurred_at)
    payload = {} if code is None else {"disposition_code": code}
    with transaction(conn):
        insert_event(
            conn,
            event_id=identifier,
            source_file_id=file_id,
            line_number=sequence,
            event_type=event_type,
            workflow_id=workflow_id,
            call_id=call_id,
            occurred_at_utc=utc,
            occurred_at_ist=ist,
            occurred_date_ist=date,
            phone=phone,
            hasher=hasher,
            payload=payload,
        )
    return identifier


def add_call(
    conn,
    file_id: int,
    lead_id: int,
    *,
    call_id: str,
    initiated_at: str = "2026-10-01T10:00:00Z",
    connected: bool = False,
    telephony_failed: bool = False,
    set_code: str | None = None,
    override_code: str | None = None,
    set_at: str | None = None,
    override_at: str | None = None,
    workflow_id: str = "motor_renewal",
) -> None:
    add_event(
        conn,
        file_id,
        lead_id,
        event_type="call.initiated",
        call_id=call_id,
        occurred_at=initiated_at,
        workflow_id=workflow_id,
    )
    if connected:
        add_event(
            conn,
            file_id,
            lead_id,
            event_type="call.connected",
            call_id=call_id,
            occurred_at=initiated_at,
            workflow_id=workflow_id,
        )
    if telephony_failed:
        add_event(
            conn,
            file_id,
            lead_id,
            event_type="call.telephony_failed",
            call_id=call_id,
            occurred_at=initiated_at,
            workflow_id=workflow_id,
        )
    if set_code is not None:
        add_event(
            conn,
            file_id,
            lead_id,
            event_type="disposition.set",
            call_id=call_id,
            occurred_at=set_at or initiated_at,
            code=set_code,
            workflow_id=workflow_id,
        )
    if override_code is not None:
        add_event(
            conn,
            file_id,
            lead_id,
            event_type="disposition.override",
            call_id=call_id,
            occurred_at=override_at or initiated_at,
            code=override_code,
            workflow_id=workflow_id,
        )


def lead_by_ref(facts, external_ref: str):
    return next(lead for lead in facts.leads if lead.external_ref == external_ref)


def call_by_id(facts, call_id: str):
    return next(call for call in facts.calls if call.call_id == call_id)


def read_csv(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.reader(handle))


# ---------------------------------------------------------------------------
# Effective disposition
# ---------------------------------------------------------------------------


def test_latest_override_beats_sets_and_latest_set_is_fallback(
    seeded_conn, hasher, cipher
):
    lead_id = add_lead(seeded_conn, hasher, cipher)
    file_id = make_ingest_file(seeded_conn)
    add_event(
        seeded_conn,
        file_id,
        lead_id,
        event_type="call.initiated",
        call_id="call_override",
        occurred_at="2026-10-01T09:00:00Z",
    )
    add_event(
        seeded_conn,
        file_id,
        lead_id,
        event_type="disposition.set",
        call_id="call_override",
        occurred_at="2026-10-01T10:00:00Z",
        code="payment_link_sent",
    )
    add_event(
        seeded_conn,
        file_id,
        lead_id,
        event_type="disposition.set",
        call_id="call_override",
        occurred_at="2026-10-01T11:00:00Z",
        code="callback_requested",
    )
    add_event(
        seeded_conn,
        file_id,
        lead_id,
        event_type="disposition.override",
        call_id="call_override",
        occurred_at="2026-10-01T12:00:00Z",
        code="not_interested",
    )
    add_event(
        seeded_conn,
        file_id,
        lead_id,
        event_type="disposition.override",
        call_id="call_override",
        occurred_at="2026-10-01T13:00:00Z",
        code="renewed",
    )

    add_event(
        seeded_conn,
        file_id,
        lead_id,
        event_type="call.initiated",
        call_id="call_set_only",
        occurred_at="2026-10-02T09:00:00Z",
    )
    add_event(
        seeded_conn,
        file_id,
        lead_id,
        event_type="disposition.set",
        call_id="call_set_only",
        occurred_at="2026-10-02T10:00:00Z",
        code="callback_requested",
    )
    add_event(
        seeded_conn,
        file_id,
        lead_id,
        event_type="disposition.set",
        call_id="call_set_only",
        occurred_at="2026-10-02T11:00:00Z",
        code="payment_link_sent",
    )

    facts = build_report_facts(seeded_conn, cipher=cipher)

    override_call = call_by_id(facts, "call_override")
    assert override_call.disposition_code == "renewed"
    assert override_call.is_terminal is True
    assert override_call.is_success is True

    set_call = call_by_id(facts, "call_set_only")
    assert set_call.disposition_code == "payment_link_sent"
    assert set_call.is_terminal is False
    assert set_call.is_success is False


def test_override_wins_even_when_its_timestamp_is_earlier(seeded_conn, hasher, cipher):
    lead_id = add_lead(seeded_conn, hasher, cipher)
    file_id = make_ingest_file(seeded_conn)
    add_event(
        seeded_conn,
        file_id,
        lead_id,
        event_type="call.initiated",
        call_id="call_1",
        occurred_at="2026-10-01T09:00:00Z",
    )
    add_event(
        seeded_conn,
        file_id,
        lead_id,
        event_type="disposition.set",
        call_id="call_1",
        occurred_at="2026-10-01T12:00:00Z",
        code="not_interested",
    )
    add_event(
        seeded_conn,
        file_id,
        lead_id,
        event_type="disposition.override",
        call_id="call_1",
        occurred_at="2026-10-01T11:00:00Z",
        code="renewed",
    )

    facts = build_report_facts(seeded_conn, cipher=cipher)

    assert call_by_id(facts, "call_1").disposition_code == "renewed"


def test_equal_override_timestamps_use_higher_event_id(seeded_conn, hasher, cipher):
    lead_id = add_lead(seeded_conn, hasher, cipher)
    file_id = make_ingest_file(seeded_conn)
    add_event(
        seeded_conn,
        file_id,
        lead_id,
        event_type="call.initiated",
        call_id="call_1",
        occurred_at="2026-10-01T09:00:00Z",
    )
    add_event(
        seeded_conn,
        file_id,
        lead_id,
        event_type="disposition.override",
        call_id="call_1",
        occurred_at="2026-10-01T12:00:00Z",
        event_id="evt_a",
        code="not_interested",
    )
    add_event(
        seeded_conn,
        file_id,
        lead_id,
        event_type="disposition.override",
        call_id="call_1",
        occurred_at="2026-10-01T12:00:00Z",
        event_id="evt_b",
        code="renewed",
    )

    facts = build_report_facts(seeded_conn, cipher=cipher)

    assert call_by_id(facts, "call_1").disposition_code == "renewed"


def test_unknown_override_does_not_erase_a_known_set(seeded_conn, hasher, cipher):
    lead_id = add_lead(seeded_conn, hasher, cipher)
    file_id = make_ingest_file(seeded_conn)
    add_event(
        seeded_conn,
        file_id,
        lead_id,
        event_type="call.initiated",
        call_id="call_1",
        occurred_at="2026-10-01T09:00:00Z",
    )
    add_event(
        seeded_conn,
        file_id,
        lead_id,
        event_type="disposition.set",
        call_id="call_1",
        occurred_at="2026-10-01T10:00:00Z",
        code="renewed",
    )
    add_event(
        seeded_conn,
        file_id,
        lead_id,
        event_type="disposition.override",
        call_id="call_1",
        occurred_at="2026-10-01T11:00:00Z",
        code="RENEWD",
    )

    facts = build_report_facts(seeded_conn, cipher=cipher)
    call = call_by_id(facts, "call_1")

    assert call.disposition_code == "renewed"
    assert call.is_success is True
    assert data_quality(seeded_conn)["unknown_disposition_events"] == 1


# ---------------------------------------------------------------------------
# Calls, connectivity, buckets and lead facts
# ---------------------------------------------------------------------------


def test_a_call_requires_a_linked_initiated_event(seeded_conn, hasher, cipher):
    lead_id = add_lead(seeded_conn, hasher, cipher)
    file_id = make_ingest_file(seeded_conn)
    add_event(
        seeded_conn,
        file_id,
        lead_id,
        event_type="call.connected",
        call_id="call_no_init",
        occurred_at="2026-10-01T10:00:00Z",
    )
    add_event(
        seeded_conn,
        file_id,
        lead_id,
        event_type="disposition.set",
        call_id="call_no_init",
        occurred_at="2026-10-01T11:00:00Z",
        code="renewed",
    )

    facts = build_report_facts(seeded_conn, cipher=cipher)
    lead = lead_by_ref(facts, "POL-1")

    assert facts.calls == ()
    assert lead.total_calls == 0
    assert lead.connected_calls == 0
    assert lead.bucket == "yet_to_reach_out"
    assert lead.last_call_at == ""
    assert lead.current_disposition == ""
    assert lead.ever_success is False


def test_connected_calls_are_counted_from_linked_connected_events(
    seeded_conn, hasher, cipher
):
    lead_id = add_lead(seeded_conn, hasher, cipher)
    file_id = make_ingest_file(seeded_conn)
    add_call(
        seeded_conn,
        file_id,
        lead_id,
        call_id="call_connected",
        connected=True,
        initiated_at="2026-10-01T10:00:00Z",
    )
    add_call(
        seeded_conn,
        file_id,
        lead_id,
        call_id="call_not_connected",
        telephony_failed=True,
        initiated_at="2026-10-01T11:00:00Z",
    )

    facts = build_report_facts(seeded_conn, cipher=cipher)
    lead = lead_by_ref(facts, "POL-1")

    assert lead.total_calls == 2
    assert lead.connected_calls == 1
    assert lead.bucket == "in_progress_dialed"


def test_duplicate_initiated_events_count_one_call_and_use_the_earliest(
    seeded_conn, hasher, cipher
):
    lead_id = add_lead(seeded_conn, hasher, cipher)
    file_id = make_ingest_file(seeded_conn)
    add_event(
        seeded_conn,
        file_id,
        lead_id,
        event_type="call.initiated",
        call_id="call_1",
        occurred_at="2026-10-02T10:00:00Z",
        event_id="evt_late",
    )
    add_event(
        seeded_conn,
        file_id,
        lead_id,
        event_type="call.initiated",
        call_id="call_1",
        occurred_at="2026-10-01T10:00:00Z",
        event_id="evt_early",
    )

    facts = build_report_facts(seeded_conn, cipher=cipher)
    lead = lead_by_ref(facts, "POL-1")

    assert len(facts.calls) == 1
    assert lead.total_calls == 1
    assert lead.last_call_at == "2026-10-01T15:30:00+05:30"


def test_bucket_priority_is_exact(seeded_conn, hasher, cipher):
    file_id = make_ingest_file(seeded_conn)

    no_calls = add_lead(
        seeded_conn, hasher, cipher, phone="9800000001", external_ref="NO-CALLS"
    )

    completed = add_lead(
        seeded_conn, hasher, cipher, phone="9800000002", external_ref="COMPLETED"
    )
    add_call(
        seeded_conn,
        file_id,
        completed,
        call_id="call_terminal",
        initiated_at="2026-10-01T10:00:00Z",
        set_code="not_interested",
    )
    add_call(
        seeded_conn,
        file_id,
        completed,
        call_id="call_failed_but_terminal_lead",
        initiated_at="2026-10-01T11:00:00Z",
        telephony_failed=True,
    )

    telephony_failed = add_lead(
        seeded_conn, hasher, cipher, phone="9800000003", external_ref="FAILED"
    )
    add_call(
        seeded_conn,
        file_id,
        telephony_failed,
        call_id="call_failed_1",
        initiated_at="2026-10-01T10:00:00Z",
        telephony_failed=True,
    )
    add_call(
        seeded_conn,
        file_id,
        telephony_failed,
        call_id="call_failed_2",
        initiated_at="2026-10-01T11:00:00Z",
        telephony_failed=True,
    )

    dialed = add_lead(
        seeded_conn, hasher, cipher, phone="9800000004", external_ref="DIALED"
    )
    add_call(
        seeded_conn,
        file_id,
        dialed,
        call_id="call_dialed_failed",
        initiated_at="2026-10-01T10:00:00Z",
        telephony_failed=True,
    )
    add_call(
        seeded_conn,
        file_id,
        dialed,
        call_id="call_dialed_connected",
        initiated_at="2026-10-01T11:00:00Z",
        connected=True,
    )

    connected_without_disposition = add_lead(
        seeded_conn, hasher, cipher, phone="9800000005", external_ref="CONNECTED"
    )
    add_call(
        seeded_conn,
        file_id,
        connected_without_disposition,
        call_id="call_connected_only",
        initiated_at="2026-10-01T10:00:00Z",
        connected=True,
    )

    facts = build_report_facts(seeded_conn, cipher=cipher)

    assert lead_by_ref(facts, "NO-CALLS").bucket == "yet_to_reach_out"
    assert lead_by_ref(facts, "COMPLETED").bucket == "completed"
    assert lead_by_ref(facts, "FAILED").bucket == "in_progress_telephony_failed"
    assert lead_by_ref(facts, "DIALED").bucket == "in_progress_dialed"
    assert lead_by_ref(facts, "CONNECTED").bucket == "in_progress_dialed"


# ---------------------------------------------------------------------------
# Daily overview
# ---------------------------------------------------------------------------


def test_daily_overview_uses_ist_dates_and_distinct_success_leads(
    seeded_conn, hasher, cipher
):
    file_id = make_ingest_file(seeded_conn)
    repeated_success = add_lead(
        seeded_conn, hasher, cipher, phone="9811111111", external_ref="A"
    )
    add_call(
        seeded_conn,
        file_id,
        repeated_success,
        call_id="call_a_1",
        initiated_at="2026-10-01T02:00:00Z",
        connected=True,
        set_code="renewed",
    )
    add_call(
        seeded_conn,
        file_id,
        repeated_success,
        call_id="call_a_2",
        initiated_at="2026-10-01T03:00:00Z",
        set_code="renewed",
    )

    next_ist_day = add_lead(
        seeded_conn, hasher, cipher, phone="9822222222", external_ref="B"
    )
    # 20:00 UTC on 1 October is 01:30 IST on 2 October.
    add_call(
        seeded_conn,
        file_id,
        next_ist_day,
        call_id="call_b_1",
        initiated_at="2026-10-01T20:00:00Z",
        set_code="renewed",
    )

    facts = build_report_facts(seeded_conn, cipher=cipher)
    rows = daily_overview_rows(facts)

    assert rows == [
        ["motor_renewal", "2026-10-01", 2, 1, "0.5000", 1],
        ["motor_renewal", "2026-10-02", 1, 0, "0.0000", 1],
    ]


def test_connectivity_rate_rounds_to_four_decimals(seeded_conn, hasher, cipher):
    file_id = make_ingest_file(seeded_conn)
    lead_id = add_lead(seeded_conn, hasher, cipher)
    for index, connected in enumerate([True, False, False], start=1):
        add_call(
            seeded_conn,
            file_id,
            lead_id,
            call_id=f"call_{index}",
            initiated_at=f"2026-10-01T1{index}:00:00Z",
            connected=connected,
        )

    facts = build_report_facts(seeded_conn, cipher=cipher)

    assert daily_overview_rows(facts) == [
        ["motor_renewal", "2026-10-01", 3, 1, "0.3333", 0]
    ]


def test_daily_overview_omits_calls_without_a_usable_timestamp(
    seeded_conn, hasher, cipher
):
    lead_id = add_lead(seeded_conn, hasher, cipher)
    file_id = make_ingest_file(seeded_conn)
    add_event(
        seeded_conn,
        file_id,
        lead_id,
        event_type="call.initiated",
        call_id="call_no_time",
        occurred_at=None,
    )

    facts = build_report_facts(seeded_conn, cipher=cipher)

    assert facts.leads[0].total_calls == 1
    assert daily_overview_rows(facts) == []


# ---------------------------------------------------------------------------
# Lead report
# ---------------------------------------------------------------------------


def test_current_disposition_skips_the_latest_call_when_it_has_none(
    seeded_conn, hasher, cipher
):
    lead_id = add_lead(seeded_conn, hasher, cipher)
    file_id = make_ingest_file(seeded_conn)
    add_call(
        seeded_conn,
        file_id,
        lead_id,
        call_id="call_1",
        initiated_at="2026-10-01T10:00:00Z",
        connected=True,
        set_code="renewed",
    )
    add_call(
        seeded_conn,
        file_id,
        lead_id,
        call_id="call_2",
        initiated_at="2026-10-02T10:00:00Z",
        connected=True,
    )

    facts = build_report_facts(seeded_conn, cipher=cipher)
    lead = lead_by_ref(facts, "POL-1")

    assert lead.current_disposition == "renewed"
    assert lead.last_call_at == "2026-10-02T15:30:00+05:30"
    assert lead.ever_success is True
    assert lead.bucket == "completed"


def test_last_call_at_is_ist_iso_without_microseconds(seeded_conn, hasher, cipher):
    lead_id = add_lead(seeded_conn, hasher, cipher)
    file_id = make_ingest_file(seeded_conn)
    add_call(
        seeded_conn,
        file_id,
        lead_id,
        call_id="call_1",
        initiated_at="2026-10-14T12:25:24.123456Z",
    )

    facts = build_report_facts(seeded_conn, cipher=cipher)

    assert facts.leads[0].last_call_at == "2026-10-14T17:55:24+05:30"


def test_ever_success_uses_effective_disposition_after_override(
    seeded_conn, hasher, cipher
):
    lead_id = add_lead(seeded_conn, hasher, cipher)
    file_id = make_ingest_file(seeded_conn)
    add_call(
        seeded_conn,
        file_id,
        lead_id,
        call_id="call_1",
        initiated_at="2026-10-01T10:00:00Z",
        set_code="renewed",
        override_code="not_interested",
        override_at="2026-10-01T11:00:00Z",
    )

    facts = build_report_facts(seeded_conn, cipher=cipher)
    lead = lead_by_ref(facts, "POL-1")

    assert lead.current_disposition == "not_interested"
    assert lead.ever_success is False
    assert lead.bucket == "completed"


def test_lead_report_rows_are_masked_and_sorted_by_workflow_then_reference(
    seeded_conn, hasher, cipher
):
    add_lead(
        seeded_conn,
        hasher,
        cipher,
        phone="9876543210",
        external_ref="MOT/2",
    )
    add_lead(
        seeded_conn,
        hasher,
        cipher,
        phone="9812345678",
        external_ref="MOT/1",
    )
    add_lead(
        seeded_conn,
        hasher,
        cipher,
        workflow_id="policy_feedback",
        phone="9123456789",
        external_ref="FB/1",
    )

    facts = build_report_facts(seeded_conn, cipher=cipher)
    rows = lead_report_rows(facts)

    assert [row[0] for row in rows] == [
        "motor_renewal",
        "motor_renewal",
        "policy_feedback",
    ]
    assert [row[1] for row in rows] == ["MOT/1", "MOT/2", "FB/1"]
    assert rows[0][3] == "+91******5678"
    assert rows[1][3] == "+91******3210"
    assert rows[0][4] == "yet_to_reach_out"
    assert rows[0][7] == ""
    assert rows[0][8] == ""
    assert rows[0][9] == "false"


def test_batch_summary_groups_leads_and_orders_buckets(seeded_conn, hasher, cipher):
    no_calls = add_lead(
        seeded_conn,
        hasher,
        cipher,
        phone="9811111111",
        external_ref="A",
        batch_id="batch_002",
    )
    completed = add_lead(
        seeded_conn,
        hasher,
        cipher,
        phone="9822222222",
        external_ref="B",
        batch_id="batch_001",
    )
    file_id = make_ingest_file(seeded_conn)
    add_call(
        seeded_conn,
        file_id,
        completed,
        call_id="call_1",
        initiated_at="2026-10-01T10:00:00Z",
        set_code="renewed",
    )
    add_lead(
        seeded_conn,
        hasher,
        cipher,
        phone="9833333333",
        external_ref="C",
        batch_id="batch_001",
    )

    facts = build_report_facts(seeded_conn, cipher=cipher)

    assert batch_summary_rows(facts) == [
        ["motor_renewal", "batch_001", 2, 1, 0, 0, 1],
        ["motor_renewal", "batch_002", 1, 1, 0, 0, 0],
    ]
    assert no_calls is not None


# ---------------------------------------------------------------------------
# File contract, audit metrics and idempotency
# ---------------------------------------------------------------------------


def test_report_files_have_exact_headers_and_values(tmp_path, seeded_conn, hasher, cipher):
    completed = add_lead(
        seeded_conn, hasher, cipher, phone="9876543210", external_ref="POL-1"
    )
    add_lead(
        seeded_conn,
        hasher,
        cipher,
        phone="9811111111",
        external_ref="POL-2",
    )
    file_id = make_ingest_file(seeded_conn)
    add_call(
        seeded_conn,
        file_id,
        completed,
        call_id="call_1",
        initiated_at="2026-10-01T10:00:00Z",
        connected=True,
        set_code="renewed",
    )

    out = tmp_path / "reports"
    generate_reports(seeded_conn, out, cipher=cipher)

    assert read_csv(out / "batch_summary.csv")[0] == BATCH_SUMMARY_HEADER
    assert read_csv(out / "daily_overview.csv")[0] == DAILY_OVERVIEW_HEADER
    assert read_csv(out / "lead_report.csv")[0] == LEAD_REPORT_HEADER
    assert read_csv(out / "rejections.csv")[0] == REJECTIONS_HEADER

    lead_rows = read_csv(out / "lead_report.csv")[1:]
    assert lead_rows[0] == [
        "motor_renewal",
        "POL-1",
        "batch_001",
        "+91******3210",
        "completed",
        "1",
        "1",
        "2026-10-01T15:30:00+05:30",
        "renewed",
        "true",
    ]
    assert lead_rows[1][4] == "yet_to_reach_out"
    assert lead_rows[1][9] == "false"

    daily = read_csv(out / "daily_overview.csv")[1:]
    assert daily == [["motor_renewal", "2026-10-01", "1", "1", "1.0000", "1"]]


def test_rejections_and_data_quality_exact_contract(seeded_conn, hasher, cipher):
    first_file = make_ingest_file(
        seeded_conn,
        kind="leads",
        workflow_id="motor_renewal",
        batch_id="batch_001",
        suffix="_one",
    )
    second_file = make_ingest_file(
        seeded_conn,
        kind="leads",
        workflow_id="motor_renewal",
        batch_id="batch_001",
        suffix="_two",
    )
    with transaction(seeded_conn):
        insert_rejection(
            seeded_conn,
            ingest_file_id=first_file,
            workflow_id="motor_renewal",
            batch_id="batch_001",
            row_number=10,
            reason="missing_required:customer_name",
        )
        insert_rejection(
            seeded_conn,
            ingest_file_id=first_file,
            workflow_id="motor_renewal",
            batch_id="batch_001",
            row_number=2,
            reason="invalid_phone",
        )
        insert_rejection(
            seeded_conn,
            ingest_file_id=second_file,
            workflow_id="motor_renewal",
            batch_id="batch_001",
            row_number=1,
            reason="invalid_phone",
        )
        for name, value in {
            "lead_rows_total": 5,
            "lead_rows_rejected": 3,
            "duplicate_lead_rows_merged": 1,
            "field_warnings": 4,
            "malformed_events": 2,
            "duplicate_events_dropped": 3,
        }.items():
            set_stat(
                seeded_conn,
                scope_type="global",
                scope_key="all",
                stat_name=name,
                value=value,
            )

    event_file = make_ingest_file(seeded_conn)
    seeded_conn.execute(
        "UPDATE ingest_files SET nonempty_lines = 10 WHERE id = ?", (event_file,)
    )

    lead_id = add_lead(seeded_conn, hasher, cipher)
    add_event(
        seeded_conn,
        event_file,
        lead_id,
        event_type="call.initiated",
        call_id="call_1",
        occurred_at="2026-10-01T10:00:00Z",
    )
    add_event(
        seeded_conn,
        event_file,
        lead_id,
        event_type="disposition.set",
        call_id="call_1",
        occurred_at="2026-10-01T11:00:00Z",
        code="RENEWD",
    )

    quality = data_quality(seeded_conn)

    assert list(quality) == [
        "lead_rows_total",
        "lead_rows_rejected",
        "rejections_by_reason",
        "duplicate_lead_rows_merged",
        "field_warnings",
        "events_total",
        "malformed_events",
        "duplicate_events_dropped",
        "orphan_events",
        "unknown_disposition_events",
    ]
    assert quality == {
        "lead_rows_total": 5,
        "lead_rows_rejected": 3,
        "rejections_by_reason": {
            "invalid_phone": 2,
            "missing_required:customer_name": 1,
        },
        "duplicate_lead_rows_merged": 1,
        "field_warnings": 4,
        "events_total": 10,
        "malformed_events": 2,
        "duplicate_events_dropped": 3,
        "orphan_events": 0,
        "unknown_disposition_events": 1,
    }

    assert rejection_rows(seeded_conn) == [
        ["motor_renewal", "batch_001", 1, "invalid_phone"],
        ["motor_renewal", "batch_001", 2, "invalid_phone"],
        ["motor_renewal", "batch_001", 10, "missing_required:customer_name"],
    ]


def test_report_overwrites_deterministically(tmp_path, seeded_conn, hasher, cipher):
    lead_id = add_lead(seeded_conn, hasher, cipher)
    file_id = make_ingest_file(seeded_conn)
    add_call(
        seeded_conn,
        file_id,
        lead_id,
        call_id="call_1",
        initiated_at="2026-10-01T10:00:00Z",
        set_code="renewed",
    )

    out = tmp_path / "reports"
    first = generate_reports(seeded_conn, out, cipher=cipher)
    contents = {
        path.name: path.read_bytes() for path in out.iterdir() if path.is_file()
    }
    second = generate_reports(seeded_conn, out, cipher=cipher)
    again = {path.name: path.read_bytes() for path in out.iterdir() if path.is_file()}

    assert first == second
    assert contents == again
    assert len(contents) == 5


def test_report_resolves_a_late_lead_before_building_facts(
    tmp_path, seeded_conn, hasher, cipher
):
    file_id = make_ingest_file(seeded_conn)
    add_unlinked_event(
        seeded_conn,
        file_id,
        hasher,
        event_type="call.initiated",
        call_id="call_1",
        occurred_at="2026-10-01T10:00:00Z",
    )

    facts_before = build_report_facts(seeded_conn, cipher=cipher)
    assert facts_before.calls == ()

    add_lead(seeded_conn, hasher, cipher, phone="9876543210", external_ref="POL-1")

    out = tmp_path / "reports"
    generate_reports(seeded_conn, out, cipher=cipher)

    lead_rows = read_csv(out / "lead_report.csv")[1:]
    assert lead_rows[0][1] == "POL-1"
    assert lead_rows[0][5] == "1"
    assert lead_rows[0][7] == "2026-10-01T15:30:00+05:30"


def test_data_quality_with_no_data_is_all_zero(tmp_path, seeded_conn, cipher):
    out = tmp_path / "reports"
    generate_reports(seeded_conn, out, cipher=cipher)

    quality = json.loads((out / "data_quality.json").read_text(encoding="utf-8"))
    assert quality == {
        "lead_rows_total": 0,
        "lead_rows_rejected": 0,
        "rejections_by_reason": {},
        "duplicate_lead_rows_merged": 0,
        "field_warnings": 0,
        "events_total": 0,
        "malformed_events": 0,
        "duplicate_events_dropped": 0,
        "orphan_events": 0,
        "unknown_disposition_events": 0,
    }
    assert read_csv(out / "batch_summary.csv") == [BATCH_SUMMARY_HEADER]
    assert read_csv(out / "lead_report.csv") == [LEAD_REPORT_HEADER]
    assert read_csv(out / "daily_overview.csv") == [DAILY_OVERVIEW_HEADER]
    assert read_csv(out / "rejections.csv") == [REJECTIONS_HEADER]
