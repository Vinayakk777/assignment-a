"""Append-only JSONL event ingestion.

The processing order is fixed by the assignment and implemented literally for
every non-empty line:

1. **Malformed**: invalid JSON, a non-object document, or a missing/empty
   ``event_id``. Counted and skipped.
2. **Duplicate**: an ``event_id`` already present in the ledger (from any
   drop). Counted and skipped. The original immutable row is never edited.
3. **Orphan**: the event is inserted, but cannot yet be linked to a lead.
   It stays in the ledger so a later lead or call event can resolve it.
4. **Unknown disposition**: a linked ``disposition.*`` event whose code is not
   in that workflow's catalogue. Counted but excluded from business facts.

Canonical time storage
----------------------
``occurred_at_utc`` and ``occurred_at_ist`` are ISO-8601 strings with explicit
offsets (``+00:00`` and ``+05:30``); ``occurred_date_ist`` is the IST calendar
date. ``...Z`` and offset-aware inputs are converted. Inputs without an offset
are interpreted as IST, as specified by the assignment. An unparseable or
missing timestamp is stored as SQL ``NULL``; it does not make an otherwise
valid event malformed.

PII
---
An event phone is normalised with the same rules as a lead phone and reduced
to the deterministic HMAC blind index. The raw value is never stored. Payload
keys that match known PII field names or aliases are dropped before the payload
is written, which also guarantees the repository's PII guard cannot fail the
whole file on an event-level payload quirk.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from .crypto import PhoneHasher, canonical_phone
from .errors import IngestError
from .event_resolution import (
    EventQualityStats,
    refresh_event_quality_stats,
    resolve_orphan_events,
)
from .repository import (
    create_ingest_file,
    increment_stat,
    insert_event,
    set_stat,
)
from .schema import WorkflowSchema
from .transactions import transaction

LOG = logging.getLogger("au_ledger.events")

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
_BASE_PII_NAMES = frozenset({"phone", "customer_name"})
_OVERRIDE_EVENT_TYPE = "disposition.override"


@dataclass(frozen=True)
class EventFileResult:
    drop_key: str
    relative_path: str
    skipped: bool
    events_total: int = 0
    malformed_events: int = 0
    duplicate_events_dropped: int = 0
    events_inserted: int = 0


@dataclass(frozen=True)
class EventDropResult:
    drop_key: str
    files_seen: int
    files_processed: int
    files_skipped: int
    events_total: int
    malformed_events: int
    duplicate_events_dropped: int
    events_inserted: int
    # Global current snapshots after ingesting this drop (not drop-specific).
    orphan_events: int
    unknown_disposition_events: int


def parse_event_timestamp(
    raw: object,
) -> tuple[str | None, str | None, str | None]:
    """Return ``(utc, ist, ist_date)`` ISO strings for an event timestamp.

    - ``...Z`` and offset-aware values are converted (UTC + IST).
    - naive values are interpreted as IST.
    - anything unparseable returns three ``None`` values.
    """
    if not isinstance(raw, str):
        return None, None, None
    text = raw.strip()
    if not text:
        return None, None, None

    normalized = text[:-1] + "+00:00" if text[-1:] in {"Z", "z"} else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None, None, None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)

    utc_value = parsed.astimezone(timezone.utc)
    ist_value = parsed.astimezone(IST)
    return utc_value.isoformat(), ist_value.isoformat(), ist_value.date().isoformat()


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _event_id(value: object) -> str | None:
    """Return the opaque event id unchanged, or ``None`` when absent/blank."""
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _event_phone_hmac(raw: object, hasher: PhoneHasher) -> str | None:
    """Normalise like a lead phone and return its HMAC, or ``None`` if invalid."""
    if not isinstance(raw, str):
        return None
    if not raw.strip():
        return None
    try:
        canonical = canonical_phone(raw)
    except ValueError:
        return None
    return hasher.blind_index(canonical)


def _workflow_ids(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT workflow_id FROM workflows").fetchall()
    }


def _pii_names_for_workflow(
    conn: sqlite3.Connection,
    workflow_id: str | None,
    cache: dict[str | None, set[str]],
) -> set[str]:
    if workflow_id in cache:
        return cache[workflow_id]
    names = set(_BASE_PII_NAMES)
    if workflow_id is not None:
        for row in conn.execute(
            "SELECT name, aliases_json FROM workflow_fields "
            "WHERE workflow_id = ? AND pii = 1",
            (workflow_id,),
        ).fetchall():
            name = str(row[0]).strip().lower()
            if name:
                names.add(name)
            try:
                aliases = json.loads(row[1] or "[]")
            except (TypeError, json.JSONDecodeError):
                aliases = []
            if isinstance(aliases, list):
                names.update(
                    str(alias).strip().lower()
                    for alias in aliases
                    if str(alias).strip()
                )
    cache[workflow_id] = names
    return names


def _sanitize_payload(value: Any, pii_names: set[str]) -> Any:
    """Recursively drop keys that name a PII field.

    The event phone is already represented by ``phone_hmac``; keeping a second
    copy in a payload would defeat that protection.
    """
    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for key, nested in value.items():
            name = str(key).strip().lower()
            if name in pii_names:
                continue
            cleaned[str(key)] = _sanitize_payload(nested, pii_names)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_sanitize_payload(item, pii_names) for item in value]
    return value


def _record_event_stats(
    conn: sqlite3.Connection,
    *,
    drop_key: str,
    relative_path: str,
    events_total: int,
    malformed_events: int,
    duplicate_events_dropped: int,
    events_inserted: int,
) -> None:
    file_scope = f"{drop_key}/{relative_path}"
    values = {
        "events_total": events_total,
        "malformed_events": malformed_events,
        "duplicate_events_dropped": duplicate_events_dropped,
        "events_inserted": events_inserted,
    }
    for stat_name, value in values.items():
        set_stat(
            conn,
            scope_type="file",
            scope_key=file_scope,
            stat_name=stat_name,
            value=value,
        )
        increment_stat(
            conn,
            scope_type="drop",
            scope_key=drop_key,
            stat_name=stat_name,
            delta=value,
        )
        increment_stat(
            conn,
            scope_type="global",
            scope_key="all",
            stat_name=stat_name,
            delta=value,
        )
    set_stat(
        conn,
        scope_type="file",
        scope_key=file_scope,
        stat_name="files_processed",
        value=1,
    )
    increment_stat(
        conn,
        scope_type="drop",
        scope_key=drop_key,
        stat_name="files_processed",
        delta=1,
    )
    increment_stat(
        conn,
        scope_type="global",
        scope_key="all",
        stat_name="files_processed",
        delta=1,
    )


def discover_event_files(drop_dir: str | Path) -> list[Path]:
    """Discover ``events/*.jsonl`` files in filename order."""
    directory = Path(drop_dir)
    if not directory.is_dir():
        raise IngestError(f"drop directory not found: {directory}")
    events_dir = directory / "events"
    if not events_dir.is_dir():
        return []
    return [path for path in sorted(events_dir.glob("*.jsonl")) if path.is_file()]


def ingest_event_file(
    conn: sqlite3.Connection,
    drop_dir: str | Path,
    path: str | Path,
    *,
    hasher: PhoneHasher,
    known_workflows: set[str] | None = None,
    refresh_stats: bool = True,
) -> EventFileResult:
    """Ingest one JSONL file inside a single transaction."""
    directory = Path(drop_dir)
    source_path = Path(path)
    drop_key = directory.name
    relative_path = source_path.relative_to(directory).as_posix()

    data = source_path.read_bytes()
    content_sha256 = hashlib.sha256(data).hexdigest()
    if known_workflows is None:
        known_workflows = _workflow_ids(conn)
    pii_cache: dict[str | None, set[str]] = {}

    with transaction(conn):
        existing = conn.execute(
            """
            SELECT id, content_sha256
            FROM ingest_files
            WHERE drop_key = ? AND relative_path = ?
            """,
            (drop_key, relative_path),
        ).fetchone()
        if existing is not None:
            if str(existing["content_sha256"]) == content_sha256:
                LOG.info(
                    "skipping already-ingested event file %s/%s",
                    drop_key,
                    relative_path,
                )
                return EventFileResult(
                    drop_key=drop_key,
                    relative_path=relative_path,
                    skipped=True,
                )
            raise IngestError(
                f"event file content changed since first ingest: {relative_path}"
            )

        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise IngestError(f"event file is not valid UTF-8: {relative_path}") from exc

        ingest_file_id = create_ingest_file(
            conn,
            drop_key=drop_key,
            drop_name=directory.name,
            kind="events",
            relative_path=relative_path,
            content_sha256=content_sha256,
            workflow_id=None,
            batch_id=None,
            nonempty_lines=0,
        )

        events_total = 0
        malformed_events = 0
        duplicate_events_dropped = 0
        events_inserted = 0

        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            events_total += 1

            try:
                document = json.loads(line)
            except json.JSONDecodeError:
                malformed_events += 1
                continue
            if not isinstance(document, dict):
                malformed_events += 1
                continue

            event_id = _event_id(document.get("event_id"))
            if event_id is None:
                malformed_events += 1
                continue

            duplicate = conn.execute(
                "SELECT 1 FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if duplicate is not None:
                duplicate_events_dropped += 1
                continue

            event_type = _optional_text(document.get("event_type"))
            workflow_id = _optional_text(document.get("workflow_id"))
            if workflow_id not in known_workflows:
                # Keep the event for later resolution, but it cannot be linked
                # to a workflow lead and the FK requires a known workflow.
                workflow_id = None
            call_id = _optional_text(document.get("call_id"))

            occurred_at_utc, occurred_at_ist, occurred_date_ist = (
                parse_event_timestamp(document.get("occurred_at"))
            )

            if event_type == _OVERRIDE_EVENT_TYPE:
                # Overrides never link by phone; their call_id is authoritative.
                phone_hmac = None
            else:
                phone_hmac = _event_phone_hmac(document.get("phone"), hasher)
            payload: Any = document.get("payload")

            if not isinstance(payload, Mapping):
                payload = {}
            pii_names = _pii_names_for_workflow(conn, workflow_id, pii_cache)
            sanitized_payload = _sanitize_payload(payload, pii_names)

            insert_event(
                conn,
                event_id=event_id,
                source_file_id=ingest_file_id,
                line_number=line_number,
                event_type=event_type,
                workflow_id=workflow_id,
                call_id=call_id,
                occurred_at_utc=occurred_at_utc,
                occurred_at_ist=occurred_at_ist,
                occurred_date_ist=occurred_date_ist,
                phone_hmac=phone_hmac,
                payload=sanitized_payload,
            )
            events_inserted += 1

        conn.execute(
            "UPDATE ingest_files SET nonempty_lines = ? WHERE id = ?",
            (events_total, ingest_file_id),
        )
        _record_event_stats(
            conn,
            drop_key=drop_key,
            relative_path=relative_path,
            events_total=events_total,
            malformed_events=malformed_events,
            duplicate_events_dropped=duplicate_events_dropped,
            events_inserted=events_inserted,
        )

        if refresh_stats:
            resolve_orphan_events(conn)
            refresh_event_quality_stats(conn)

    LOG.info(
        "processed event file %s/%s lines=%d malformed=%d duplicates=%d inserted=%d",
        drop_key,
        relative_path,
        events_total,
        malformed_events,
        duplicate_events_dropped,
        events_inserted,
    )
    return EventFileResult(
        drop_key=drop_key,
        relative_path=relative_path,
        skipped=False,
        events_total=events_total,
        malformed_events=malformed_events,
        duplicate_events_dropped=duplicate_events_dropped,
        events_inserted=events_inserted,
    )


def ingest_event_drop(
    conn: sqlite3.Connection,
    drop_dir: str | Path,
    schemas: dict[str, WorkflowSchema],
    *,
    hasher: PhoneHasher,
) -> EventDropResult:
    """Ingest every event file in one drop, then refresh derived quality state.

    Resolving after the whole drop (rather than after every file) lets an
    override that appears before its call event in the same drop resolve
    without repeated full scans. The final transaction is still crash-safe:
    re-running a skipped drop performs resolution again.
    """
    directory = Path(drop_dir)
    files = discover_event_files(directory)
    known_workflows = _workflow_ids(conn) | set(schemas)
    results: list[EventFileResult] = []
    for path in files:
        results.append(
            ingest_event_file(
                conn,
                directory,
                path,
                hasher=hasher,
                known_workflows=known_workflows,
                refresh_stats=False,
            )
        )

    with transaction(conn):
        resolve_orphan_events(conn)
        quality: EventQualityStats = refresh_event_quality_stats(conn)

    processed = [result for result in results if not result.skipped]
    return EventDropResult(
        drop_key=directory.name,
        files_seen=len(results),
        files_processed=len(processed),
        files_skipped=len(results) - len(processed),
        events_total=sum(result.events_total for result in processed),
        malformed_events=sum(result.malformed_events for result in processed),
        duplicate_events_dropped=sum(
            result.duplicate_events_dropped for result in processed
        ),
        events_inserted=sum(result.events_inserted for result in processed),
        orphan_events=quality.orphan_events,
        unknown_disposition_events=quality.unknown_disposition_events,
    )


def ingest_event_drops(
    conn: sqlite3.Connection,
    drop_dirs: list[str | Path],
    schemas: dict[str, WorkflowSchema],
    *,
    hasher: PhoneHasher,
) -> list[EventDropResult]:
    """Ingest event drops in the exact order supplied by the caller."""
    return [
        ingest_event_drop(conn, drop_dir, schemas, hasher=hasher)
        for drop_dir in drop_dirs
    ]
