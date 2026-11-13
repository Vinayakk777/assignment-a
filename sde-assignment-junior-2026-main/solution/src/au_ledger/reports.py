"""Business facts and the five Assignment A report files.

The report layer is deliberately built on the immutable ledger:

* A **call** exists only when a linked ``call.initiated`` event exists. It is
  keyed by ``(workflow_id, call_id)`` and dated by the IST date of the
  initiated event.
* A call is **connected** when a linked ``call.connected`` event exists for the
  same call and lead.
* A call's **effective disposition** is the latest valid override by
  ``occurred_at`` (``event_id`` breaks ties), otherwise the latest valid
  ``disposition.set``. Unknown catalogue codes never affect the result.
* A lead's **bucket** follows the assignment's strict precedence:
  ``yet_to_reach_out`` -> ``completed`` -> ``in_progress_telephony_failed`` ->
  ``in_progress_dialed``.

Facts are rebuilt from the ledger every time a report is generated, which makes
the output self-healing after a crash and correct after late leads/events.

Formatting is handled in Python rather than SQL so the contract is explicit:
lowercase booleans, four-decimal connectivity, IST ISO timestamps without a
``Z`` or microseconds, and exactly the required CSV columns.
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .crypto import PiiCipher, mask_phone
from .event_resolution import (
    count_unknown_disposition_events,
    refresh_event_quality_stats,
    resolve_orphan_events,
)
from .repository import count_orphan_events
from .transactions import transaction

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

BUCKET_YET_TO_REACH_OUT = "yet_to_reach_out"
BUCKET_COMPLETED = "completed"
BUCKET_TELEPHONY_FAILED = "in_progress_telephony_failed"
BUCKET_DIALED = "in_progress_dialed"
BUCKETS = (
    BUCKET_YET_TO_REACH_OUT,
    BUCKET_COMPLETED,
    BUCKET_TELEPHONY_FAILED,
    BUCKET_DIALED,
)

BATCH_SUMMARY_HEADER = [
    "workflow_id",
    "batch_id",
    "total_leads",
    "yet_to_reach_out",
    "in_progress_telephony_failed",
    "in_progress_dialed",
    "completed",
]
DAILY_OVERVIEW_HEADER = [
    "workflow_id",
    "date",
    "total_calls",
    "connected_calls",
    "connectivity_rate",
    "success_leads",
]
LEAD_REPORT_HEADER = [
    "workflow_id",
    "external_ref",
    "batch_id",
    "phone_masked",
    "bucket",
    "total_calls",
    "connected_calls",
    "last_call_at",
    "current_disposition",
    "ever_success",
]
REJECTIONS_HEADER = ["workflow_id", "batch_id", "row_number", "reason"]

REPORT_FILENAMES = (
    "batch_summary.csv",
    "daily_overview.csv",
    "lead_report.csv",
    "rejections.csv",
    "data_quality.json",
)


@dataclass(frozen=True)
class CallFact:
    """One counted call and its effective outcome."""

    workflow_id: str
    call_id: str
    lead_id: int
    initiated_at_utc: str | None
    initiated_at_ist: str | None
    initiated_date_ist: str | None
    connected: bool = False
    telephony_failed: bool = False
    disposition_code: str | None = None
    is_terminal: bool = False
    is_success: bool = False


@dataclass(frozen=True)
class LeadFact:
    """One lead's derived business state."""

    lead_id: int
    workflow_id: str
    external_ref: str
    batch_id: str
    phone_masked: str
    bucket: str
    total_calls: int
    connected_calls: int
    last_call_at: str
    current_disposition: str
    ever_success: bool


@dataclass(frozen=True)
class ReportFacts:
    calls: tuple[CallFact, ...]
    leads: tuple[LeadFact, ...]


@dataclass(frozen=True)
class ReportResult:
    out_dir: Path
    batch_summary_rows: int
    daily_overview_rows: int
    lead_report_rows: int
    rejection_rows: int


def _parse_payload(raw: object) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _disposition_catalogue(
    conn: sqlite3.Connection,
) -> dict[tuple[str, str], tuple[bool, bool]]:
    """Return ``(is_terminal, is_success)`` for every catalogue code."""
    return {
        (str(row["workflow_id"]), str(row["code"])): (
            bool(row["is_terminal"]),
            bool(row["is_success"]),
        )
        for row in conn.execute(
            "SELECT workflow_id, code, is_terminal, is_success FROM dispositions"
        ).fetchall()
    }


def _time_key(occurred_at_utc: str | None, event_id: str) -> tuple[bool, str, str]:
    """Order events by timestamp, treating a missing timestamp as oldest.

    The final ``event_id`` component makes equal timestamps deterministic: a
    lexically higher id wins because the effective-disposition selector takes
    the maximum key.
    """
    return (occurred_at_utc is not None, occurred_at_utc or "", event_id)


def _format_ist_iso(utc_value: str | None) -> str:
    """Format a stored UTC timestamp as IST ISO-8601 with whole seconds."""
    if not utc_value:
        return ""
    text = utc_value[:-1] + "+00:00" if utc_value[-1:] in {"Z", "z"} else utc_value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(IST).replace(microsecond=0).isoformat()


def _call_order_key(call: CallFact) -> tuple[bool, str, str]:
    return (
        call.initiated_at_utc is not None,
        call.initiated_at_utc or "",
        call.call_id,
    )


def _counted_calls(conn: sqlite3.Connection) -> list[tuple[str, str, int, sqlite3.Row]]:
    """Select one initiated event per ``(workflow_id, call_id)``.

    Multiple initiated events are not generated, but the earliest one wins if
    they occur; ``event_id`` is a deterministic tie-break.
    """
    rows = conn.execute(
        """
        SELECT e.event_id, e.workflow_id, e.call_id,
               e.occurred_at_utc, e.occurred_at_ist, e.occurred_date_ist,
               l.lead_id
        FROM events AS e
        JOIN event_links AS l ON l.event_id = e.event_id
        WHERE e.event_type = 'call.initiated'
          AND e.workflow_id IS NOT NULL
          AND e.call_id IS NOT NULL
        ORDER BY e.id
        """
    ).fetchall()

    chosen: dict[tuple[str, str], sqlite3.Row] = {}
    for row in rows:
        key = (str(row["workflow_id"]), str(row["call_id"]))
        current = chosen.get(key)
        if current is None or _time_key(
            row["occurred_at_utc"], str(row["event_id"])
        ) < _time_key(current["occurred_at_utc"], str(current["event_id"])):
            chosen[key] = row
    return [
        (workflow_id, call_id, int(row["lead_id"]), row)
        for (workflow_id, call_id), row in sorted(chosen.items())
    ]


def _call_event_flags(
    conn: sqlite3.Connection,
) -> tuple[set[tuple[str, str, int]], set[tuple[str, str, int]]]:
    """Return connected and telephony-failed ``(workflow, call, lead)`` sets."""
    connected: set[tuple[str, str, int]] = set()
    failed: set[tuple[str, str, int]] = set()
    for row in conn.execute(
        """
        SELECT e.event_type, e.workflow_id, e.call_id, l.lead_id
        FROM events AS e
        JOIN event_links AS l ON l.event_id = e.event_id
        WHERE e.event_type IN ('call.connected', 'call.telephony_failed')
          AND e.workflow_id IS NOT NULL
          AND e.call_id IS NOT NULL
        """
    ).fetchall():
        key = (str(row["workflow_id"]), str(row["call_id"]), int(row["lead_id"]))
        if row["event_type"] == "call.connected":
            connected.add(key)
        else:
            failed.add(key)
    return connected, failed


def _effective_dispositions(
    conn: sqlite3.Connection,
) -> dict[tuple[str, str, int], tuple[str, bool, bool]]:
    """Resolve the effective disposition for each call/lead pair.

    Overrides take precedence over sets. Only catalogue codes participate, so
    an unknown override cannot erase a known disposition.
    """
    catalogue = _disposition_catalogue(conn)
    best: dict[tuple[str, str, int], tuple[tuple[bool, str, str], str, bool, bool]] = {}

    rows = conn.execute(
        """
        SELECT e.event_id, e.event_type, e.workflow_id, e.call_id,
               e.occurred_at_utc, e.payload_json, l.lead_id
        FROM events AS e
        JOIN event_links AS l ON l.event_id = e.event_id
        WHERE e.event_type IN ('disposition.set', 'disposition.override')
          AND e.workflow_id IS NOT NULL
          AND e.call_id IS NOT NULL
        ORDER BY e.id
        """
    ).fetchall()

    overrides: dict[tuple[str, str, int], tuple[tuple[bool, str, str], str, bool, bool]] = {}
    sets: dict[tuple[str, str, int], tuple[tuple[bool, str, str], str, bool, bool]] = {}
    for row in rows:
        workflow_id = str(row["workflow_id"])
        code = _parse_payload(row["payload_json"]).get("disposition_code")
        if not isinstance(code, str):
            continue
        catalogue_row = catalogue.get((workflow_id, code))
        if catalogue_row is None:
            continue
        key = (workflow_id, str(row["call_id"]), int(row["lead_id"]))
        candidate = (
            _time_key(row["occurred_at_utc"], str(row["event_id"])),
            code,
            catalogue_row[0],
            catalogue_row[1],
        )
        target = overrides if row["event_type"] == "disposition.override" else sets
        current = target.get(key)
        if current is None or candidate[0] > current[0]:
            target[key] = candidate

    for key, candidate in sets.items():
        best[key] = candidate
    for key, candidate in overrides.items():
        best[key] = candidate

    return {
        key: (code, is_terminal, is_success)
        for key, (_order, code, is_terminal, is_success) in best.items()
    }


def _phone_masks(conn: sqlite3.Connection, cipher: PiiCipher) -> dict[int, str]:
    """Decrypt each lead phone at report time and return the masked form."""
    rows = conn.execute(
        """
        SELECT l.id AS lead_id, p.field_name, p.ciphertext
        FROM leads AS l
        JOIN workflow_fields AS wf
          ON wf.workflow_id = l.workflow_id AND wf.field_type = 'phone'
        JOIN lead_pii AS p
          ON p.lead_id = l.id AND p.field_name = wf.name
        """
    ).fetchall()
    masks: dict[int, str] = {}
    for row in rows:
        plaintext = cipher.decrypt(
            str(row["ciphertext"]), field_name=str(row["field_name"])
        )
        masks[int(row["lead_id"])] = mask_phone(plaintext)
    return masks


def build_report_facts(conn: sqlite3.Connection, *, cipher: PiiCipher) -> ReportFacts:
    """Rebuild every business fact from the current ledger state."""
    connected_keys, failed_keys = _call_event_flags(conn)
    effective = _effective_dispositions(conn)

    calls: list[CallFact] = []
    for workflow_id, call_id, lead_id, row in _counted_calls(conn):
        key = (workflow_id, call_id, lead_id)
        disposition = effective.get(key)
        calls.append(
            CallFact(
                workflow_id=workflow_id,
                call_id=call_id,
                lead_id=lead_id,
                initiated_at_utc=row["occurred_at_utc"],
                initiated_at_ist=row["occurred_at_ist"],
                initiated_date_ist=row["occurred_date_ist"],
                connected=key in connected_keys,
                telephony_failed=key in failed_keys,
                disposition_code=None if disposition is None else disposition[0],
                is_terminal=False if disposition is None else disposition[1],
                is_success=False if disposition is None else disposition[2],
            )
        )

    calls_by_lead: dict[int, list[CallFact]] = defaultdict(list)
    for call in calls:
        calls_by_lead[call.lead_id].append(call)

    masks = _phone_masks(conn, cipher)
    leads: list[LeadFact] = []
    lead_rows = conn.execute(
        """
        SELECT id, workflow_id, external_ref, batch_id
        FROM leads
        ORDER BY workflow_id, external_ref, id
        """
    ).fetchall()

    for row in lead_rows:
        lead_id = int(row["id"])
        lead_calls = calls_by_lead.get(lead_id, [])
        total_calls = len(lead_calls)
        connected_calls = sum(1 for call in lead_calls if call.connected)

        if total_calls == 0:
            bucket = BUCKET_YET_TO_REACH_OUT
        elif any(call.is_terminal for call in lead_calls):
            bucket = BUCKET_COMPLETED
        elif all(call.telephony_failed for call in lead_calls):
            bucket = BUCKET_TELEPHONY_FAILED
        else:
            bucket = BUCKET_DIALED

        latest_call = max(lead_calls, key=_call_order_key) if lead_calls else None
        disposition_calls = [
            call for call in lead_calls if call.disposition_code is not None
        ]
        current_call = (
            max(disposition_calls, key=_call_order_key) if disposition_calls else None
        )

        leads.append(
            LeadFact(
                lead_id=lead_id,
                workflow_id=str(row["workflow_id"]),
                external_ref=str(row["external_ref"] or ""),
                batch_id=str(row["batch_id"]),
                phone_masked=masks.get(lead_id, ""),
                bucket=bucket,
                total_calls=total_calls,
                connected_calls=connected_calls,
                last_call_at=(
                    "" if latest_call is None else _format_ist_iso(latest_call.initiated_at_utc)
                ),
                current_disposition=(
                    "" if current_call is None else str(current_call.disposition_code or "")
                ),
                ever_success=any(call.is_success for call in lead_calls),
            )
        )

    return ReportFacts(calls=tuple(calls), leads=tuple(leads))


def batch_summary_rows(facts: ReportFacts) -> list[list[object]]:
    """Build ``batch_summary.csv`` rows in ``workflow_id, batch_id`` order."""
    groups: dict[tuple[str, str], Counter[str]] = {}
    for lead in facts.leads:
        key = (lead.workflow_id, lead.batch_id)
        groups.setdefault(key, Counter())[lead.bucket] += 1

    rows: list[list[object]] = []
    for workflow_id, batch_id in sorted(groups):
        counts = groups[(workflow_id, batch_id)]
        rows.append(
            [
                workflow_id,
                batch_id,
                sum(counts.values()),
                counts[BUCKET_YET_TO_REACH_OUT],
                counts[BUCKET_TELEPHONY_FAILED],
                counts[BUCKET_DIALED],
                counts[BUCKET_COMPLETED],
            ]
        )
    return rows


def daily_overview_rows(facts: ReportFacts) -> list[list[object]]:
    """Build ``daily_overview.csv`` rows in ``workflow_id, date`` order."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for call in facts.calls:
        if call.initiated_date_ist is None:
            continue
        key = (call.workflow_id, call.initiated_date_ist)
        group = groups.setdefault(
            key, {"total_calls": 0, "connected_calls": 0, "success_leads": set()}
        )
        group["total_calls"] += 1
        if call.connected:
            group["connected_calls"] += 1
        if call.is_success:
            group["success_leads"].add(call.lead_id)

    rows: list[list[object]] = []
    for workflow_id, date in sorted(groups):
        group = groups[(workflow_id, date)]
        total_calls = int(group["total_calls"])
        connected_calls = int(group["connected_calls"])
        rows.append(
            [
                workflow_id,
                date,
                total_calls,
                connected_calls,
                f"{connected_calls / total_calls:.4f}",
                len(group["success_leads"]),
            ]
        )
    return rows


def lead_report_rows(facts: ReportFacts) -> list[list[object]]:
    """Build ``lead_report.csv`` rows in ``workflow_id, external_ref`` order."""
    rows: list[list[object]] = []
    for lead in facts.leads:
        rows.append(
            [
                lead.workflow_id,
                lead.external_ref,
                lead.batch_id,
                lead.phone_masked,
                lead.bucket,
                lead.total_calls,
                lead.connected_calls,
                lead.last_call_at,
                lead.current_disposition,
                "true" if lead.ever_success else "false",
            ]
        )
    return rows


def rejection_rows(conn: sqlite3.Connection) -> list[list[object]]:
    """Build ``rejections.csv`` rows with no PII and deterministic ordering."""
    rows: list[list[object]] = []
    for row in conn.execute(
        """
        SELECT workflow_id, batch_id, row_number, reason
        FROM rejections
        ORDER BY workflow_id, batch_id, row_number, ingest_file_id, id
        """
    ).fetchall():
        rows.append(
            [str(row["workflow_id"]), str(row["batch_id"]), int(row["row_number"]), str(row["reason"])]
        )
    return rows


def _global_stat(conn: sqlite3.Connection, stat_name: str) -> int:
    row = conn.execute(
        """
        SELECT stat_value FROM processing_stats
        WHERE scope_type = 'global' AND scope_key = 'all' AND stat_name = ?
        """,
        (stat_name,),
    ).fetchone()
    return 0 if row is None else int(row[0])


def _events_total(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(nonempty_lines), 0) FROM ingest_files WHERE kind = 'events'"
    ).fetchone()
    return int(row[0])


def data_quality(conn: sqlite3.Connection) -> dict[str, Any]:
    """Build the exact ``data_quality.json`` object from ledger/audit state."""
    reasons = [
        (str(row["reason"]), int(row["count"]))
        for row in conn.execute(
            """
            SELECT reason, COUNT(*) AS count
            FROM rejections
            GROUP BY reason
            ORDER BY reason
            """
        ).fetchall()
    ]
    return {
        "lead_rows_total": _global_stat(conn, "lead_rows_total"),
        "lead_rows_rejected": _global_stat(conn, "lead_rows_rejected"),
        "rejections_by_reason": {reason: count for reason, count in reasons},
        "duplicate_lead_rows_merged": _global_stat(conn, "duplicate_lead_rows_merged"),
        "field_warnings": _global_stat(conn, "field_warnings"),
        "events_total": _events_total(conn),
        "malformed_events": _global_stat(conn, "malformed_events"),
        "duplicate_events_dropped": _global_stat(conn, "duplicate_events_dropped"),
        "orphan_events": count_orphan_events(conn),
        "unknown_disposition_events": count_unknown_disposition_events(conn),
    }


def _atomic_write_csv(
    path: Path, header: Sequence[str], rows: Iterable[Sequence[object]]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(list(header))
            for row in rows:
                writer.writerow(list(row))
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def generate_reports(
    conn: sqlite3.Connection,
    out_dir: str | Path,
    *,
    cipher: PiiCipher,
) -> ReportResult:
    """Re-resolve derived state, rebuild facts, and write the five files.

    Re-running this function is deterministic and overwrites the same files
    atomically. It never modifies event rows.
    """
    with transaction(conn):
        resolve_orphan_events(conn)
        refresh_event_quality_stats(conn)

    facts = build_report_facts(conn, cipher=cipher)
    batch_rows = batch_summary_rows(facts)
    daily_rows = daily_overview_rows(facts)
    lead_rows = lead_report_rows(facts)
    rejected_rows = rejection_rows(conn)
    quality = data_quality(conn)

    target = Path(out_dir)
    _atomic_write_csv(target / "batch_summary.csv", BATCH_SUMMARY_HEADER, batch_rows)
    _atomic_write_csv(target / "daily_overview.csv", DAILY_OVERVIEW_HEADER, daily_rows)
    _atomic_write_csv(target / "lead_report.csv", LEAD_REPORT_HEADER, lead_rows)
    _atomic_write_csv(target / "rejections.csv", REJECTIONS_HEADER, rejected_rows)
    _atomic_write_json(target / "data_quality.json", quality)

    return ReportResult(
        out_dir=target,
        batch_summary_rows=len(batch_rows),
        daily_overview_rows=len(daily_rows),
        lead_report_rows=len(lead_rows),
        rejection_rows=len(rejected_rows),
    )
