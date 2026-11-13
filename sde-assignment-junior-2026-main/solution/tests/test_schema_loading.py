from __future__ import annotations

import json
from pathlib import Path

import pytest

from au_ledger.errors import SchemaError
from au_ledger.schema import load_workflow_schemas

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS_DIR = REPO_ROOT / "assignment-a-data" / "schemas"


def _write_schema(directory: Path, name: str, document: object) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _valid_schema(workflow_id: str = "test_workflow") -> dict:
    return {
        "workflow_id": workflow_id,
        "description": "Test workflow.",
        "fields": [
            {
                "name": "full_name",
                "type": "string",
                "required": True,
                "pii": True,
                "aliases": ["name", "customer name"],
            },
            {
                "name": "phone",
                "type": "phone",
                "required": True,
                "pii": True,
                "aliases": ["mobile", "contact"],
            },
            {
                "name": "reference",
                "type": "string",
                "required": True,
                "reference": True,
                "aliases": ["reference no"],
            },
            {"name": "amount", "type": "decimal", "required": False},
        ],
    }


def test_actual_schemas_are_loaded_from_the_supplied_directory():
    schemas = load_workflow_schemas(SCHEMAS_DIR)

    assert set(schemas) == {"motor_renewal", "policy_feedback"}

    motor = schemas["motor_renewal"]
    assert [field.name for field in motor.fields] == [
        "customer_name",
        "phone",
        "policy_number",
        "policy_expiry",
        "premium_amount",
        "preferred_language",
    ]
    assert motor.phone_field.name == "phone"
    assert motor.reference_field.name == "policy_number"
    assert {field.name for field in motor.pii_fields} == {"customer_name", "phone"}
    assert motor.defaults == {"preferred_language": "en"}
    assert motor.field_by_name["preferred_language"].enum_values == (
        "en",
        "hi",
        "ta",
        "kn",
        "te",
        "mr",
    )

    feedback = schemas["policy_feedback"]
    assert feedback.reference_field.name == "order_id"
    assert feedback.defaults == {"preferred_language": "en"}


@pytest.mark.parametrize(
    ("header", "field_name"),
    [
        ("customer_name", "customer_name"),
        (" CUSTOMER___NAME ", "customer_name"),
        ("name", "customer_name"),
        ("insured__name", "customer_name"),
        ("mobile", "phone"),
        ("MOBILE NO", "phone"),
        ("phone_number", "phone"),
        (" contact ", "phone"),
        ("policy_no", "policy_number"),
        ("Policy", "policy_number"),
        ("expiry_date", "policy_expiry"),
        ("EXPIRY", "policy_expiry"),
        ("premium", "premium_amount"),
        ("language", "preferred_language"),
    ],
)
def test_headers_match_names_and_aliases_after_normalisation(header, field_name):
    schema = load_workflow_schemas(SCHEMAS_DIR)["motor_renewal"]
    matched = schema.match_header(header)
    assert matched is not None
    assert matched.name == field_name


@pytest.mark.parametrize("header", ["", "unknown", "phone 2", "customer-name", "Nothing"])
def test_unknown_headers_are_ignored(header):
    schema = load_workflow_schemas(SCHEMAS_DIR)["motor_renewal"]
    assert schema.match_header(header) is None


def test_custom_schema_is_loaded_dynamically(tmp_path: Path):
    document = {
        "workflow_id": "loan_recovery",
        "description": "Collections calls.",
        "fields": [
            {
                "name": "borrower_name",
                "type": "string",
                "required": True,
                "pii": True,
                "aliases": ["borrower"],
            },
            {
                "name": "mobile_number",
                "type": "phone",
                "required": True,
                "pii": True,
                "aliases": ["mobile"],
            },
            {
                "name": "loan_account",
                "type": "string",
                "required": True,
                "reference": True,
                "aliases": ["account"],
            },
            {
                "name": "due_date",
                "type": "date",
                "required": False,
                "aliases": ["due"],
            },
            {
                "name": "outstanding_amount",
                "type": "decimal",
                "required": False,
            },
            {
                "name": "priority",
                "type": "enum",
                "required": False,
                "values": ["Low", "High"],
                "default": "Low",
                "aliases": ["bucket"],
            },
        ],
    }
    _write_schema(tmp_path, "loan_recovery.json", document)

    schemas = load_workflow_schemas(tmp_path)
    schema = schemas["loan_recovery"]

    assert [field.name for field in schema.fields] == [
        "borrower_name",
        "mobile_number",
        "loan_account",
        "due_date",
        "outstanding_amount",
        "priority",
    ]
    assert schema.phone_field.name == "mobile_number"
    assert schema.reference_field.name == "loan_account"
    assert schema.defaults == {"priority": "low"}
    assert schema.match_header("account").name == "loan_account"
    assert schema.match_header("MOBILE").name == "mobile_number"
    assert schema.match_header("legacy_id") is None


def _without_field(document: dict, name: str) -> None:
    document["fields"] = [
        field for field in document["fields"] if field["name"] != name
    ]


def _duplicate_field(document: dict, name: str) -> None:
    clone = dict(next(field for field in document["fields"] if field["name"] == name))
    document["fields"].append(clone)


def _second_phone(document: dict) -> None:
    document["fields"].append(
        {"name": "backup_phone", "type": "phone", "required": False}
    )


def _second_reference(document: dict) -> None:
    document["fields"].append(
        {"name": "backup_reference", "type": "string", "required": False, "reference": True}
    )


def _phone_not_required(document: dict) -> None:
    next(field for field in document["fields"] if field["name"] == "phone")[
        "required"
    ] = False


def _reference_is_pii(document: dict) -> None:
    next(field for field in document["fields"] if field["name"] == "reference")[
        "pii"
    ] = True


def _duplicate_alias(document: dict) -> None:
    next(field for field in document["fields"] if field["name"] == "full_name")[
        "aliases"
    ].append("PHONE")


def _invalid_enum_default(document: dict) -> None:
    document["fields"].append(
        {
            "name": "language",
            "type": "enum",
            "values": ["en", "hi"],
            "default": "fr",
        }
    )


@pytest.mark.parametrize(
    ("case_id", "mutate", "message"),
    [
        ("missing_workflow_id", lambda doc: doc.pop("workflow_id"), "workflow_id"),
        ("missing_fields", lambda doc: doc.pop("fields"), "fields missing"),
        ("empty_fields", lambda doc: doc.__setitem__("fields", []), "fields missing"),
        ("duplicate_field", lambda doc: _duplicate_field(doc, "phone"), "duplicate field"),
        (
            "unsupported_type",
            lambda doc: doc["fields"].append({"name": "extra", "type": "integer"}),
            "unsupported field type",
        ),
        (
            "enum_without_values",
            lambda doc: doc["fields"].append({"name": "status", "type": "enum"}),
            "values missing",
        ),
        (
            "invalid_default",
            lambda doc: doc["fields"].append(
                {"name": "count", "type": "decimal", "default": "many"}
            ),
            "invalid default",
        ),
        ("no_phone", lambda doc: _without_field(doc, "phone"), "exactly one phone"),
        ("two_phones", _second_phone, "exactly one phone"),
        ("phone_not_required", _phone_not_required, "phone field must be required"),
        (
            "no_reference",
            lambda doc: _without_field(doc, "reference"),
            "exactly one reference",
        ),
        ("two_references", _second_reference, "exactly one reference"),
        ("reference_is_pii", _reference_is_pii, "reference field cannot be PII"),
        ("duplicate_alias", _duplicate_alias, "maps to multiple fields"),
        ("invalid_enum_default", _invalid_enum_default, "invalid default"),
    ],
)
def test_invalid_schemas_are_rejected(tmp_path: Path, case_id, mutate, message):
    document = _valid_schema()
    mutate(document)
    _write_schema(tmp_path, f"{case_id}.json", document)

    with pytest.raises(SchemaError, match=message):
        load_workflow_schemas(tmp_path)


def test_duplicate_workflow_ids_are_rejected(tmp_path: Path):
    _write_schema(tmp_path, "one.json", _valid_schema("same_workflow"))
    _write_schema(tmp_path, "two.json", _valid_schema("same_workflow"))

    with pytest.raises(SchemaError, match="duplicate workflow_id"):
        load_workflow_schemas(tmp_path)


def test_missing_directory_and_empty_directory_are_rejected(tmp_path: Path):
    with pytest.raises(SchemaError, match="schemas directory not found"):
        load_workflow_schemas(tmp_path / "missing")

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SchemaError, match="no workflow schema JSON files"):
        load_workflow_schemas(empty)


def test_invalid_json_is_rejected_without_echoing_file_contents(tmp_path: Path):
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(SchemaError, match="invalid workflow schema"):
        load_workflow_schemas(tmp_path)
