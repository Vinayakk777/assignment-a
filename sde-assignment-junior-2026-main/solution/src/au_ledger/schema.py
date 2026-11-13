"""Dynamic workflow schema loading and header-to-field matching."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .errors import SchemaError
from .normalization import normalize_default_value, normalize_header

ALLOWED_FIELD_TYPES = frozenset({"string", "phone", "date", "decimal", "enum"})
_FIELD_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class FieldSpec:
    """One validated workflow field in schema order."""

    name: str
    ordinal: int
    field_type: str
    required: bool
    pii: bool
    is_reference: bool
    aliases: tuple[str, ...]
    enum_values: tuple[str, ...]
    default: object | None

    @property
    def header_keys(self) -> tuple[str, ...]:
        keys = (self.name, *self.aliases)
        # Preserve order while removing duplicates after normalisation.
        return tuple(dict.fromkeys(normalize_header(key) for key in keys))


@dataclass(frozen=True)
class WorkflowSchema:
    """A validated workflow schema ready for ingestion."""

    workflow_id: str
    description: str
    fields: tuple[FieldSpec, ...]
    phone_field: FieldSpec
    reference_field: FieldSpec
    header_map: Mapping[str, FieldSpec]
    schema_hash: str
    source_path: Path

    @property
    def pii_fields(self) -> tuple[FieldSpec, ...]:
        return tuple(field for field in self.fields if field.pii)

    @property
    def non_pii_fields(self) -> tuple[FieldSpec, ...]:
        return tuple(field for field in self.fields if not field.pii)

    @property
    def field_by_name(self) -> Mapping[str, FieldSpec]:
        return MappingProxyType({field.name: field for field in self.fields})

    @property
    def defaults(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                field.name: field.default
                for field in self.fields
                if field.default is not None
            }
        )

    def match_header(self, raw_header: str) -> FieldSpec | None:
        return self.header_map.get(normalize_header(raw_header))


def _as_bool(value: Any, label: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise SchemaError(f"{label} must be a boolean")


def _load_schema(path: Path) -> WorkflowSchema:
    raw = path.read_bytes()
    try:
        document = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchemaError(f"invalid workflow schema: {path.name}") from exc
    if not isinstance(document, dict):
        raise SchemaError(f"workflow schema must be an object: {path.name}")

    workflow_id = document.get("workflow_id")
    if not isinstance(workflow_id, str) or not workflow_id.strip():
        raise SchemaError(f"workflow_id missing in {path.name}")
    workflow_id = workflow_id.strip()

    description = document.get("description", "")
    if description is None:
        description = ""
    if not isinstance(description, str):
        raise SchemaError(f"description must be a string in {path.name}")

    raw_fields = document.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise SchemaError(f"fields missing in {path.name}")

    fields: list[FieldSpec] = []
    names: set[str] = set()
    header_map: dict[str, FieldSpec] = {}
    phone_fields: list[FieldSpec] = []
    reference_fields: list[FieldSpec] = []

    for ordinal, raw_field in enumerate(raw_fields, 1):
        if not isinstance(raw_field, dict):
            raise SchemaError(f"field {ordinal} must be an object in {path.name}")
        name = raw_field.get("name")
        if not isinstance(name, str) or not name.strip():
            raise SchemaError(f"empty field name in {path.name}")
        name = name.strip()
        if name in names:
            raise SchemaError(f"duplicate field name {name} in {path.name}")
        if not _FIELD_NAME_RE.fullmatch(name):
            raise SchemaError(f"invalid field name {name} in {path.name}")
        names.add(name)

        field_type = raw_field.get("type")
        if field_type not in ALLOWED_FIELD_TYPES:
            raise SchemaError(f"unsupported field type for {name} in {path.name}")

        required = _as_bool(raw_field.get("required", False), f"required for {name}")
        pii = _as_bool(raw_field.get("pii", False), f"pii for {name}")
        is_reference = _as_bool(
            raw_field.get("reference", False), f"reference for {name}"
        )

        raw_aliases = raw_field.get("aliases", [])
        if raw_aliases is None:
            raw_aliases = []
        if not isinstance(raw_aliases, list) or any(
            not isinstance(alias, str) for alias in raw_aliases
        ):
            raise SchemaError(f"aliases must be a string list for {name} in {path.name}")
        aliases = tuple(
            dict.fromkeys(alias.strip() for alias in raw_aliases if alias.strip())
        )

        enum_values: tuple[str, ...] = ()
        if field_type == "enum":
            raw_values = raw_field.get("values")
            if not isinstance(raw_values, list) or not raw_values:
                raise SchemaError(f"values missing for enum {name} in {path.name}")
            cleaned_values = tuple(
                str(value).strip() for value in raw_values if str(value).strip()
            )
            if not cleaned_values:
                raise SchemaError(f"values missing for enum {name} in {path.name}")
            lowered = [value.lower() for value in cleaned_values]
            if len(set(lowered)) != len(lowered):
                raise SchemaError(f"duplicate enum values for {name} in {path.name}")
            enum_values = cleaned_values

        default: object | None = None
        if "default" in raw_field and raw_field["default"] is not None:
            try:
                default = normalize_default_value(
                    field_type, raw_field["default"], enum_values
                )
            except (TypeError, ValueError) as exc:
                raise SchemaError(
                    f"invalid default for {name} in {path.name}"
                ) from exc

        field = FieldSpec(
            name=name,
            ordinal=ordinal,
            field_type=field_type,
            required=required,
            pii=pii,
            is_reference=is_reference,
            aliases=aliases,
            enum_values=enum_values,
            default=default,
        )
        fields.append(field)

        if field_type == "phone":
            phone_fields.append(field)
        if is_reference:
            reference_fields.append(field)

        for key in field.header_keys:
            existing = header_map.get(key)
            if existing is not None and existing.name != name:
                raise SchemaError(
                    f"header alias {key!r} maps to multiple fields in {path.name}"
                )
            header_map[key] = field

    if len(phone_fields) != 1:
        raise SchemaError(f"expected exactly one phone field in {path.name}")
    if len(reference_fields) != 1:
        raise SchemaError(f"expected exactly one reference field in {path.name}")
    phone_field = phone_fields[0]
    reference_field = reference_fields[0]
    if not phone_field.required:
        raise SchemaError(f"phone field must be required in {path.name}")
    if reference_field.pii:
        raise SchemaError(f"reference field cannot be PII in {path.name}")

    return WorkflowSchema(
        workflow_id=workflow_id,
        description=description,
        fields=tuple(fields),
        phone_field=phone_field,
        reference_field=reference_field,
        header_map=MappingProxyType(header_map),
        schema_hash=hashlib.sha256(raw).hexdigest(),
        source_path=path,
    )


def load_workflow_schemas(schemas_dir: str | Path) -> dict[str, WorkflowSchema]:
    """Load every ``*.json`` workflow schema from a directory.

    The filename order is deterministic. Every field, alias, enum value and
    default is validated before ingestion starts.
    """
    directory = Path(schemas_dir)
    if not directory.is_dir():
        raise SchemaError(f"schemas directory not found: {directory}")
    paths = sorted(directory.glob("*.json"))
    if not paths:
        raise SchemaError(f"no workflow schema JSON files found in {directory}")

    schemas: dict[str, WorkflowSchema] = {}
    for path in paths:
        schema = _load_schema(path)
        if schema.workflow_id in schemas:
            raise SchemaError(f"duplicate workflow_id {schema.workflow_id}")
        schemas[schema.workflow_id] = schema
    return schemas
