"""Derived event linkage and current event-quality counts.

Events themselves are append-only. Anything that can change as more data
arrives lives outside the ``events`` table:

- ``event_links`` records the current lead for an event (``phone`` or ``call``
  method).
- ``processing_stats`` stores cumulative line counters and *current snapshots*
  of the unresolved orphan and unknown-disposition counts.

The helpers in this module never rewrite event rows. They are safe to call
repeatedly: linking is an upsert and the snapshot counters are set, not
incremented.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from .repository import count_orphan_events, link_event_to_lead, set_stat

OVERRIDE_EVENT_TYPE = "disposition.override"
_DISPOSITION_PREFIX = "disposition.%"


@dataclass(frozen=True)
class EventQualityStats:
    """Current global event-quality snapshots."""

    orphan_events: int
    unknown_disposition_events: int


def _payload_dict(raw: object) -> dict[str, object]:
    """Parse a stored payload, returning an empty object when it is unusable."""
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _lead_id_for_phone(
    conn: sqlite3.Connection,
    *,
    workflow_id: str | None,
    phone_hmac: str | None,
) -> int | None:
    if workflow_id is None or phone_hmac is None:
        return None
    row = conn.execute(
        "SELECT id FROM leads WHERE workflow_id = ? AND phone_hmac = ?",
        (workflow_id, phone_hmac),
    ).fetchone()
    return None if row is None else int(row[0])


def _lead_id_for_call(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    call_id: str | None,
    workflow_id: str | None,
) -> int | None:
    """Find the lead of another linked event on the same call.

    Overrides carry no phone. The assignment links them through their
    ``call_id`` to the lead of that call's other events. If the event's
    workflow is known, the candidate must belong to the same workflow so a
    coincidental call id in another campaign cannot cross-link.
    """
    if call_id is None:
        return None
    row = conn.execute(
        """
        SELECT el.lead_id
        FROM events AS other
        JOIN event_links AS el ON el.event_id = other.event_id
        WHERE other.call_id = ?
          AND other.event_id <> ?
          AND (? IS NULL OR other.workflow_id = ?)
        ORDER BY other.id DESC
        LIMIT 1
        """,
        (call_id, event_id, workflow_id, workflow_id),
    ).fetchone()
    return None if row is None else int(row[0])


def resolve_orphan_events(conn: sqlite3.Connection) -> int:
    """Link every currently resolvable orphan and return how many were linked.

    Resolution runs in two phases:

    1. Non-override events are linked by ``(workflow_id, phone_hmac)``.
    2. Overrides are linked by ``call_id`` to any other linked event on the
       same call. This phase repeats because one override can resolve another
       override on the same call.

    The caller should own the transaction: a re-resolution may create many
    ``event_links`` rows and they belong to one logical unit of work.
    """
    orphans = conn.execute(
        """
        SELECT id, event_id, workflow_id, call_id, phone_hmac, event_type
        FROM orphan_events
        ORDER BY id
        """
    ).fetchall()

    resolved = 0
    overrides: list[sqlite3.Row] = []

    for row in orphans:
        if row["event_type"] == OVERRIDE_EVENT_TYPE:
            overrides.append(row)
            continue
        lead_id = _lead_id_for_phone(
            conn,
            workflow_id=row["workflow_id"],
            phone_hmac=row["phone_hmac"],
        )
        if lead_id is None:
            continue
        link_event_to_lead(
            conn,
            event_id=str(row["event_id"]),
            lead_id=lead_id,
            link_method="phone",
        )
        resolved += 1

    # Resolve overrides in passes. New links can unlock further overrides on
    # the same call, so continue while the set shrinks.
    pending = overrides
    while pending:
        still_pending: list[sqlite3.Row] = []
        progress = False
        for row in pending:
            lead_id = _lead_id_for_call(
                conn,
                event_id=str(row["event_id"]),
                call_id=row["call_id"],
                workflow_id=row["workflow_id"],
            )
            if lead_id is None:
                still_pending.append(row)
                continue
            link_event_to_lead(
                conn,
                event_id=str(row["event_id"]),
                lead_id=lead_id,
                link_method="call",
            )
            resolved += 1
            progress = True
        pending = still_pending
        if not progress:
            break

    return resolved


def count_orphan_events_for_drop(conn: sqlite3.Connection, drop_key: str) -> int:
    """Count events from one drop that are still unresolved right now."""
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM orphan_events AS o
        JOIN ingest_files AS f ON f.id = o.source_file_id
        WHERE f.drop_key = ?
        """,
        (drop_key,),
    ).fetchone()
    return int(row[0])


def _disposition_codes(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    return {
        (str(row[0]), str(row[1]))
        for row in conn.execute("SELECT workflow_id, code FROM dispositions").fetchall()
    }


def _unknown_disposition_rows(
    conn: sqlite3.Connection,
) -> list[tuple[str, str, str]]:
    """Return ``(drop_key, workflow_id, payload_json)`` for linked dispositions.

    Orphans are excluded by the inner join on ``event_links``. That gives
    orphan precedence over the unknown-disposition rule, as required by the
    processing order.
    """
    rows = conn.execute(
        """
        SELECT f.drop_key, e.workflow_id, e.payload_json
        FROM events AS e
        JOIN event_links AS l ON l.event_id = e.event_id
        JOIN ingest_files AS f ON f.id = e.source_file_id
        WHERE e.event_type LIKE ?
        ORDER BY e.id
        """,
        (_DISPOSITION_PREFIX,),
    ).fetchall()
    return [
        (str(row[0]), row[1] if row[1] is None else str(row[1]), str(row[2] or ""))
        for row in rows
    ]


def _is_unknown_disposition(
    workflow_id: str | None,
    payload_json: str,
    known_codes: set[tuple[str, str]],
) -> bool:
    if workflow_id is None:
        return True
    payload = _payload_dict(payload_json)
    code = payload.get("disposition_code")
    if not isinstance(code, str):
        return True
    # Codes are validated exactly (case-sensitively) against the catalogue.
    return (workflow_id, code) not in known_codes


def count_unknown_disposition_events(
    conn: sqlite3.Connection,
    *,
    drop_key: str | None = None,
) -> int:
    """Count linked disposition events whose code is not in the catalogue.

    Each event is counted at most once. Unresolved orphans are not counted
    here; they are counted by the orphan rule instead.
    """
    known_codes = _disposition_codes(conn)
    total = 0
    for row_drop_key, workflow_id, payload_json in _unknown_disposition_rows(conn):
        if drop_key is not None and row_drop_key != drop_key:
            continue
        if _is_unknown_disposition(workflow_id, payload_json, known_codes):
            total += 1
    return total


def refresh_event_quality_stats(conn: sqlite3.Connection) -> EventQualityStats:
    """Replace the current orphan/unknown snapshot counters.

    Global counters always exist. Every drop that has event files also gets a
    snapshot row, including zeroes, so reports can show per-drop quality.
    """
    event_drop_keys = [
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT drop_key FROM ingest_files WHERE kind = 'events' "
            "ORDER BY drop_key"
        ).fetchall()
    ]
    orphan_by_drop = {drop_key: 0 for drop_key in event_drop_keys}
    for row in conn.execute(
        """
        SELECT f.drop_key, COUNT(*)
        FROM orphan_events AS o
        JOIN ingest_files AS f ON f.id = o.source_file_id
        GROUP BY f.drop_key
        """
    ).fetchall():
        orphan_by_drop[str(row[0])] = int(row[1])

    known_codes = _disposition_codes(conn)
    unknown_by_drop = {drop_key: 0 for drop_key in event_drop_keys}
    for row_drop_key, workflow_id, payload_json in _unknown_disposition_rows(conn):
        if _is_unknown_disposition(workflow_id, payload_json, known_codes):
            unknown_by_drop[row_drop_key] = unknown_by_drop.get(row_drop_key, 0) + 1

    global_orphans = count_orphan_events(conn)
    global_unknown = sum(unknown_by_drop.values())

    set_stat(
        conn,
        scope_type="global",
        scope_key="all",
        stat_name="orphan_events",
        value=global_orphans,
    )
    set_stat(
        conn,
        scope_type="global",
        scope_key="all",
        stat_name="unknown_disposition_events",
        value=global_unknown,
    )
    for drop_key in sorted(orphan_by_drop):
        set_stat(
            conn,
            scope_type="drop",
            scope_key=drop_key,
            stat_name="orphan_events",
            value=orphan_by_drop[drop_key],
        )
        set_stat(
            conn,
            scope_type="drop",
            scope_key=drop_key,
            stat_name="unknown_disposition_events",
            value=unknown_by_drop.get(drop_key, 0),
        )

    return EventQualityStats(
        orphan_events=global_orphans,
        unknown_disposition_events=global_unknown,
    )
