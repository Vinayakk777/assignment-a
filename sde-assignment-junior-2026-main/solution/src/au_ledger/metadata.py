"""Load workflow schemas and the disposition catalogue into SQLite metadata.

This is schema metadata, not lead data. It is safe to re-run: workflow fields
and dispositions are replaced transactionally from the supplied files.
"""

from __future__ import annotations

import csv
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from .errors import SchemaError
from .schema import load_workflow_schemas
from .transactions import transaction
from .util import json_dumps, utc_now_iso


def _load_dispositions(path: Path) -> dict[str, list[dict[str, Any]]]:
    if not path.exists():
        raise SchemaError(f"disposition catalogue not found: {path}")
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"workflow_id", "code", "label", "is_terminal", "is_success"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise SchemaError("disposition catalogue is missing required columns")
        for line_number, row in enumerate(reader, 2):
            workflow_id = (row.get("workflow_id") or "").strip()
            code = (row.get("code") or "").strip()
            label = (row.get("label") or "").strip()
            if not workflow_id or not code:
                raise SchemaError(f"empty workflow/code in dispositions.csv line {line_number}")
            grouped[workflow_id].append(
                {
                    "code": code,
                    "label": label,
                    "is_terminal": _parse_bool(row.get("is_terminal")),
                    "is_success": _parse_bool(row.get("is_success")),
                }
            )
    return grouped


def _parse_bool(value: Any) -> int:
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return 1
    if text in {"false", "0", "no", "n"}:
        return 0
    raise SchemaError("invalid boolean in disposition catalogue")


def seed_metadata(conn: sqlite3.Connection, schemas_dir: str | Path) -> dict[str, int]:
    """Upsert workflow/field/disposition metadata. Requires migrated tables."""
    directory = Path(schemas_dir)
    if not directory.is_dir():
        raise SchemaError(f"schemas directory not found: {directory}")
    schemas = load_workflow_schemas(directory)
    dispositions = _load_dispositions(directory / "dispositions.csv")

    unknown = set(dispositions) - set(schemas)
    if unknown:
        raise SchemaError(
            "dispositions reference unknown workflows: " + ", ".join(sorted(unknown))
        )

    now = utc_now_iso()
    try:
        with transaction(conn):
            for workflow_id, schema in schemas.items():
                conn.execute(
                    """
                    INSERT INTO workflows(workflow_id, description, schema_hash, created_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(workflow_id) DO UPDATE SET
                        description = excluded.description,
                        schema_hash = excluded.schema_hash
                    """,
                    (workflow_id, schema.description, schema.schema_hash, now),
                )
                conn.execute(
                    "DELETE FROM workflow_fields WHERE workflow_id = ?", (workflow_id,)
                )
                for field in schema.fields:
                    conn.execute(
                        """
                        INSERT INTO workflow_fields(
                            workflow_id, ordinal, name, field_type, required, pii,
                            is_reference, default_value, enum_values_json, aliases_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            workflow_id,
                            field.ordinal,
                            field.name,
                            field.field_type,
                            1 if field.required else 0,
                            1 if field.pii else 0,
                            1 if field.is_reference else 0,
                            None if field.default is None else str(field.default),
                            (
                                json_dumps(list(field.enum_values))
                                if field.field_type == "enum"
                                else None
                            ),
                            json_dumps(list(field.aliases)),
                        ),
                    )

            for workflow_id, rows in dispositions.items():
                conn.execute("DELETE FROM dispositions WHERE workflow_id = ?", (workflow_id,))
                for row in rows:
                    conn.execute(
                        """
                        INSERT INTO dispositions(
                            workflow_id, code, label, is_terminal, is_success
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            workflow_id,
                            row["code"],
                            row["label"],
                            row["is_terminal"],
                            row["is_success"],
                        ),
                    )
    except sqlite3.IntegrityError as exc:
        raise SchemaError("metadata violates a database constraint") from exc

    return {
        "workflows": int(conn.execute("SELECT COUNT(*) FROM workflows").fetchone()[0]),
        "fields": int(conn.execute("SELECT COUNT(*) FROM workflow_fields").fetchone()[0]),
        "dispositions": int(conn.execute("SELECT COUNT(*) FROM dispositions").fetchone()[0]),
    }
