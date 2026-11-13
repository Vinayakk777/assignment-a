"""Lead file ingestion: discovery, validation, merging and idempotency.

The pipeline is intentionally data-driven: every workflow field, alias, type,
default and PII flag comes from the workflow schemas. No workflow-specific field
names are hardcoded here.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .crypto import PiiCipher, PhoneHasher
from .errors import IngestError
from .event_resolution import refresh_event_quality_stats, resolve_orphan_events
from .normalization import normalize_field_value
from .repository import (
    create_ingest_file,
    get_lead_by_phone,
    get_lead_pii_field_names,
    increment_stat,
    insert_lead,
    insert_rejection,
    set_stat,
    update_lead_fields,
    upsert_lead_pii,
)
from .schema import FieldSpec, WorkflowSchema
from .transactions import transaction
from .util import utc_now_iso

LOG = logging.getLogger("au_ledger.ingest")


@dataclass(frozen=True)
class LeadFileResult:
    drop_key: str
    relative_path: str
    batch_id: str
    skipped: bool
    rows_total: int = 0
    rows_rejected: int = 0
    duplicates_merged: int = 0
    field_warnings: int = 0
    leads_created: int = 0


@dataclass(frozen=True)
class LeadDropResult:
    drop_key: str
    files_seen: int
    files_processed: int
    files_skipped: int
    rows_total: int
    rows_rejected: int
    duplicates_merged: int
    field_warnings: int
    leads_created: int


@dataclass(frozen=True)
class ParsedRow:
    values: dict[str, object]
    field_warnings: int


class RowRejected(Exception):
    """Internal control-flow exception carrying the persistent reason code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def validate_row(schema: WorkflowSchema, raw_values: dict[str, str]) -> ParsedRow:
    """Validate and normalise one non-blank row in schema field order."""
    values: dict[str, object] = {}
    field_warnings = 0

    for field in schema.fields:
        raw = raw_values.get(field.name, "")
        text = "" if raw is None else str(raw).strip()

        if not text:
            if field.required:
                raise RowRejected(f"missing_required:{field.name}")
            values[field.name] = None
            continue

        try:
            value = normalize_field_value(
                field.field_type, text, field.enum_values
            )
        except (TypeError, ValueError):
            if field.field_type == "phone":
                # Phone is the lead identity, so an invalid present phone can
                # never be silently turned into a null lead.
                raise RowRejected("invalid_phone")
            if field.required:
                raise RowRejected(f"invalid_{field.name}")
            field_warnings += 1
            values[field.name] = None
            continue

        values[field.name] = value

    return ParsedRow(values=values, field_warnings=field_warnings)


def _is_blank_row(row: list[str]) -> bool:
    return not any(str(cell).strip() for cell in row)


def _header_mapping(
    schema: WorkflowSchema, header: list[str]
) -> dict[int, FieldSpec]:
    """Map CSV column indexes to fields. Unknown columns and duplicates vanish."""
    mapping: dict[int, FieldSpec] = {}
    mapped_names: set[str] = set()
    for index, raw_header in enumerate(header):
        field = schema.match_header(raw_header)
        if field is None or field.name in mapped_names:
            continue
        mapping[index] = field
        mapped_names.add(field.name)
    return mapping


def _apply_defaults_to_row(
    schema: WorkflowSchema, values: dict[str, object]
) -> dict[str, object]:
    """Fill still-null fields with schema defaults after a row merge."""
    merged = dict(values)
    for field in schema.fields:
        if field.name == schema.phone_field.name:
            continue
        if merged.get(field.name) is None and field.default is not None:
            merged[field.name] = field.default
    return merged


def _insert_new_lead(
    conn: sqlite3.Connection,
    schema: WorkflowSchema,
    parsed: ParsedRow,
    *,
    batch_id: str,
    hasher: PhoneHasher,
    cipher: PiiCipher,
) -> int:
    values = _apply_defaults_to_row(schema, parsed.values)
    phone = str(values[schema.phone_field.name])
    attributes: dict[str, object] = {}
    pii_fields: dict[str, str] = {}

    for field in schema.fields:
        value = values.get(field.name)
        if value is None or field.name == schema.phone_field.name:
            continue
        if field.pii:
            pii_fields[field.name] = str(value)
        else:
            attributes[field.name] = value

    reference_value = values.get(schema.reference_field.name)
    external_ref = "" if reference_value is None else str(reference_value)

    return insert_lead(
        conn,
        workflow_id=schema.workflow_id,
        phone=phone,
        external_ref=external_ref,
        batch_id=batch_id,
        hasher=hasher,
        cipher=cipher,
        pii_fields=pii_fields,
        attributes=attributes,
        phone_field_name=schema.phone_field.name,
    )


def _merge_existing_lead(
    conn: sqlite3.Connection,
    schema: WorkflowSchema,
    lead: sqlite3.Row,
    parsed: ParsedRow,
    *,
    cipher: PiiCipher,
) -> None:
    """Overwrite only non-null fields; keep identity and first batch forever."""
    lead_id = int(lead["id"])
    try:
        attributes = json.loads(lead["attributes_json"] or "{}")
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise IngestError("stored lead attributes are invalid JSON") from exc
    if not isinstance(attributes, dict):
        raise IngestError("stored lead attributes are not an object")

    # Legacy/partial rows may only have the mirrored reference column.
    if (
        schema.reference_field.name not in attributes
        and str(lead["external_ref"] or "").strip()
    ):
        attributes[schema.reference_field.name] = str(lead["external_ref"])

    pii_names = get_lead_pii_field_names(conn, lead_id)

    for field in schema.fields:
        if field.name == schema.phone_field.name:
            continue
        value = parsed.values.get(field.name)
        if value is not None:
            if field.pii:
                upsert_lead_pii(
                    conn,
                    lead_id=lead_id,
                    field_name=field.name,
                    value=str(value),
                    cipher=cipher,
                )
                pii_names.add(field.name)
            else:
                attributes[field.name] = value

    # Lead rows created by an older/partial write still get a phone ciphertext.
    phone_field_name = schema.phone_field.name
    if phone_field_name not in pii_names:
        upsert_lead_pii(
            conn,
            lead_id=lead_id,
            field_name=phone_field_name,
            value=str(parsed.values[phone_field_name]),
            cipher=cipher,
        )
        pii_names.add(phone_field_name)

    # Defaults apply after merging, to fields that are still null.
    for field in schema.fields:
        if field.name == phone_field_name or field.default is None:
            continue
        if field.pii:
            if field.name not in pii_names:
                upsert_lead_pii(
                    conn,
                    lead_id=lead_id,
                    field_name=field.name,
                    value=str(field.default),
                    cipher=cipher,
                )
                pii_names.add(field.name)
        elif attributes.get(field.name) is None:
            attributes[field.name] = field.default

    reference_value = attributes.get(schema.reference_field.name)
    if reference_value is not None and str(reference_value).strip():
        external_ref = str(reference_value)
    else:
        external_ref = str(lead["external_ref"] or "")

    if external_ref and schema.reference_field.name not in attributes:
        attributes[schema.reference_field.name] = external_ref

    update_lead_fields(
        conn,
        lead_id=lead_id,
        external_ref=external_ref,
        attributes=attributes,
        updated_at=utc_now_iso(),
    )


def _record_stats(
    conn: sqlite3.Connection,
    *,
    drop_key: str,
    relative_path: str,
    rows_total: int,
    rows_rejected: int,
    duplicates_merged: int,
    field_warnings: int,
    leads_created: int,
) -> None:
    file_scope = f"{drop_key}/{relative_path}"
    values = {
        "lead_rows_total": rows_total,
        "lead_rows_rejected": rows_rejected,
        "duplicate_lead_rows_merged": duplicates_merged,
        "field_warnings": field_warnings,
        "leads_created": leads_created,
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


def _process_file(
    conn: sqlite3.Connection,
    *,
    drop_dir: Path,
    workflow: WorkflowSchema,
    path: Path,
    hasher: PhoneHasher,
    cipher: PiiCipher,
) -> LeadFileResult:
    drop_key = drop_dir.name
    relative_path = path.relative_to(drop_dir).as_posix()
    batch_id = path.stem
    data = path.read_bytes()
    content_sha256 = hashlib.sha256(data).hexdigest()

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
                LOG.info("skipping already-ingested lead file %s/%s", drop_key, relative_path)
                return LeadFileResult(
                    drop_key=drop_key,
                    relative_path=relative_path,
                    batch_id=batch_id,
                    skipped=True,
                )
            raise IngestError(
                f"lead file content changed since first ingest: {relative_path}"
            )

        try:
            text = data.decode("utf-8-sig")
            reader = csv.reader(io.StringIO(text, newline=""))
            try:
                header = next(reader)
            except StopIteration:
                header = []
            except csv.Error as exc:
                raise IngestError(f"malformed lead CSV: {relative_path}") from exc
            header_fields = _header_mapping(workflow, header)

            ingest_file_id = create_ingest_file(
                conn,
                drop_key=drop_key,
                drop_name=drop_dir.name,
                kind="leads",
                relative_path=relative_path,
                content_sha256=content_sha256,
                workflow_id=workflow.workflow_id,
                batch_id=batch_id,
                nonempty_lines=0,
            )

            rows_total = 0
            rows_rejected = 0
            duplicates_merged = 0
            field_warnings = 0
            leads_created = 0

            while True:
                try:
                    row = next(reader)
                except StopIteration:
                    break
                except csv.Error as exc:
                    raise IngestError(
                        f"malformed lead CSV: {relative_path}"
                    ) from exc

                if _is_blank_row(row):
                    continue
                rows_total += 1

                raw_values = {
                    field.name: (row[index] if index < len(row) else "")
                    for index, field in header_fields.items()
                }
                try:
                    parsed = validate_row(workflow, raw_values)
                except RowRejected as rejection:
                    insert_rejection(
                        conn,
                        ingest_file_id=ingest_file_id,
                        workflow_id=workflow.workflow_id,
                        batch_id=batch_id,
                        row_number=rows_total,
                        reason=rejection.reason,
                    )
                    rows_rejected += 1
                    continue

                phone = str(parsed.values[workflow.phone_field.name])
                lead = get_lead_by_phone(
                    conn,
                    workflow_id=workflow.workflow_id,
                    phone=phone,
                    hasher=hasher,
                )
                if lead is None:
                    _insert_new_lead(
                        conn,
                        workflow,
                        parsed,
                        batch_id=batch_id,
                        hasher=hasher,
                        cipher=cipher,
                    )
                    leads_created += 1
                else:
                    _merge_existing_lead(
                        conn,
                        workflow,
                        lead,
                        parsed,
                        cipher=cipher,
                    )
                    duplicates_merged += 1

                field_warnings += parsed.field_warnings

            conn.execute(
                "UPDATE ingest_files SET nonempty_lines = ? WHERE id = ?",
                (rows_total, ingest_file_id),
            )
            _record_stats(
                conn,
                drop_key=drop_key,
                relative_path=relative_path,
                rows_total=rows_total,
                rows_rejected=rows_rejected,
                duplicates_merged=duplicates_merged,
                field_warnings=field_warnings,
                leads_created=leads_created,
            )
        except UnicodeDecodeError as exc:
            raise IngestError(f"lead file is not valid UTF-8: {relative_path}") from exc

    LOG.info(
        "processed lead file %s/%s rows=%d rejected=%d merged=%d warnings=%d created=%d",
        drop_key,
        relative_path,
        rows_total,
        rows_rejected,
        duplicates_merged,
        field_warnings,
        leads_created,
    )
    return LeadFileResult(
        drop_key=drop_key,
        relative_path=relative_path,
        batch_id=batch_id,
        skipped=False,
        rows_total=rows_total,
        rows_rejected=rows_rejected,
        duplicates_merged=duplicates_merged,
        field_warnings=field_warnings,
        leads_created=leads_created,
    )


def discover_lead_files(
    drop_dir: str | Path, schemas: dict[str, WorkflowSchema]
) -> list[tuple[WorkflowSchema, Path]]:
    """Discover ``leads/<workflow_id>/*.csv`` files in filename order."""
    directory = Path(drop_dir)
    if not directory.is_dir():
        raise IngestError(f"drop directory not found: {directory}")
    leads_dir = directory / "leads"
    if not leads_dir.is_dir():
        return []

    discovered: list[tuple[WorkflowSchema, Path]] = []
    workflow_dirs = sorted(path for path in leads_dir.iterdir() if path.is_dir())
    for workflow_dir in workflow_dirs:
        workflow = schemas.get(workflow_dir.name)
        if workflow is None:
            raise IngestError(f"unknown workflow directory: {workflow_dir.name}")
        for path in sorted(workflow_dir.glob("*.csv")):
            if path.is_file():
                discovered.append((workflow, path))
    return discovered


def ingest_drop(
    conn: sqlite3.Connection,
    drop_dir: str | Path,
    schemas: dict[str, WorkflowSchema],
    *,
    hasher: PhoneHasher,
    cipher: PiiCipher,
) -> LeadDropResult:
    """Ingest every lead file in one drop, in deterministic filename order."""
    directory = Path(drop_dir)
    files = discover_lead_files(directory, schemas)
    results: list[LeadFileResult] = []

    for workflow, path in files:
        results.append(
            _process_file(
                conn,
                drop_dir=directory,
                workflow=workflow,
                path=path,
                hasher=hasher,
                cipher=cipher,
            )
        )

    # A lead may claim events that arrived in an earlier drop. Events are
    # immutable; only event_links and the current quality snapshots change.
    with transaction(conn):
        resolve_orphan_events(conn)
        refresh_event_quality_stats(conn)

    processed = [result for result in results if not result.skipped]
    return LeadDropResult(
        drop_key=directory.name,
        files_seen=len(results),
        files_processed=len(processed),
        files_skipped=len(results) - len(processed),
        rows_total=sum(result.rows_total for result in processed),
        rows_rejected=sum(result.rows_rejected for result in processed),
        duplicates_merged=sum(result.duplicates_merged for result in processed),
        field_warnings=sum(result.field_warnings for result in processed),
        leads_created=sum(result.leads_created for result in processed),
    )


def ingest_drops(
    conn: sqlite3.Connection,
    drop_dirs: list[str | Path],
    schemas: dict[str, WorkflowSchema],
    *,
    hasher: PhoneHasher,
    cipher: PiiCipher,
) -> list[LeadDropResult]:
    """Ingest drops in the exact order supplied by the caller."""
    return [
        ingest_drop(conn, drop_dir, schemas, hasher=hasher, cipher=cipher)
        for drop_dir in drop_dirs
    ]
