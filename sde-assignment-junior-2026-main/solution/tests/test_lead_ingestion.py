from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from au_ledger.errors import IngestError
from au_ledger.ingestion import discover_lead_files, ingest_drop, ingest_drops
from au_ledger.metadata import seed_metadata
from au_ledger.repository import get_lead_pii
from au_ledger.schema import load_workflow_schemas

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


def all_leads(conn):
    return conn.execute("SELECT * FROM leads ORDER BY id").fetchall()


def lead_attributes(lead) -> dict[str, object]:
    return json.loads(lead["attributes_json"])


def pii_value(conn, lead, field_name: str, cipher) -> str | None:
    return get_lead_pii(conn, lead_id=int(lead["id"]), field_name=field_name, cipher=cipher)


def stat_value(conn, scope_type: str, scope_key: str, stat_name: str) -> int | None:
    row = conn.execute(
        """
        SELECT stat_value FROM processing_stats
        WHERE scope_type = ? AND scope_key = ? AND stat_name = ?
        """,
        (scope_type, scope_key, stat_name),
    ).fetchone()
    return None if row is None else int(row[0])


def snapshot(conn) -> dict[str, object]:
    tables = (
        "leads",
        "lead_pii",
        "rejections",
        "ingest_files",
        "processing_stats",
    )
    counts = {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in tables
    }
    stats = conn.execute(
        """
        SELECT scope_type, scope_key, stat_name, stat_value
        FROM processing_stats
        ORDER BY scope_type, scope_key, stat_name
        """
    ).fetchall()
    return {"counts": counts, "stats": [tuple(row) for row in stats]}


def write_schemas_dir(directory: Path, workflow_document: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    workflow_id = workflow_document["workflow_id"]
    (directory / f"{workflow_id}.json").write_text(
        json.dumps(workflow_document), encoding="utf-8"
    )
    (directory / "dispositions.csv").write_text(
        "workflow_id,code,label,is_terminal,is_success\n"
        f"{workflow_id},done,Done,true,true\n",
        encoding="utf-8",
    )
    return directory


def test_aliases_unknown_columns_and_blank_rows_are_handled(
    tmp_path, seeded_conn, schemas, hasher, cipher
):
    drop = tmp_path / "drop_1"
    write_lead_file(
        drop,
        "motor_renewal",
        "batch_001.csv",
        ["Name", "Mobile No", "Policy No", "Expiry Date", "Premium", "Language", "Mystery"],
        [
            ["Asha Rao", "09876543210", "POL-1", "01-Dec-2026", "₹1,234.5", "HI", "ignored"],
            [""],
            ["  Ravi Kumar  ", "+91 91234 56780", "POL-2", "2026-12-02", "Rs 10", "ta", "x"],
        ],
    )

    result = ingest_drop(
        seeded_conn, drop, schemas, hasher=hasher, cipher=cipher
    )

    assert result.drop_key == "drop_1"
    assert result.files_seen == 1
    assert result.files_processed == 1
    assert result.files_skipped == 0
    assert result.rows_total == 2
    assert result.rows_rejected == 0
    assert result.duplicates_merged == 0
    assert result.field_warnings == 0
    assert result.leads_created == 2

    leads = all_leads(seeded_conn)
    assert len(leads) == 2
    first, second = leads

    assert first["workflow_id"] == "motor_renewal"
    assert first["batch_id"] == "batch_001"
    assert first["external_ref"] == "POL-1"
    assert lead_attributes(first) == {
        "policy_number": "POL-1",
        "policy_expiry": "2026-12-01",
        "premium_amount": "1234.50",
        "preferred_language": "hi",
    }
    assert pii_value(seeded_conn, first, "customer_name", cipher) == "Asha Rao"
    assert pii_value(seeded_conn, first, "phone", cipher) == "+919876543210"
    assert first["phone_hmac"] == hasher.blind_index("+919876543210")

    assert second["external_ref"] == "POL-2"
    assert lead_attributes(second)["premium_amount"] == "10.00"
    assert lead_attributes(second)["preferred_language"] == "ta"
    assert pii_value(seeded_conn, second, "customer_name", cipher) == "Ravi Kumar"

    assert seeded_conn.execute(
        "SELECT COUNT(*) FROM ingest_files WHERE nonempty_lines = 2"
    ).fetchone()[0] == 1


def test_row_numbering_skips_blanks_and_rejections_have_no_pii(
    tmp_path, seeded_conn, schemas, hasher, cipher
):
    drop = tmp_path / "drop_1"
    write_lead_file(
        drop,
        "motor_renewal",
        "batch_001.csv",
        MOTOR_HEADER,
        [
            [""],
            ["Asha", "123", "POL-1", "", "", ""],
            ["", "9876543210", "POL-2", "", "", ""],
            ["   ", "", "", "", "", ""],
            ["Ravi", "9123456780", "POL-3", "31/02/2026", "N/A", "xx"],
        ],
    )

    result = ingest_drop(
        seeded_conn, drop, schemas, hasher=hasher, cipher=cipher
    )

    assert result.rows_total == 3
    assert result.rows_rejected == 2
    assert result.leads_created == 1
    assert result.field_warnings == 3

    rejections = seeded_conn.execute(
        """
        SELECT workflow_id, batch_id, row_number, reason
        FROM rejections ORDER BY row_number
        """
    ).fetchall()
    assert [tuple(row) for row in rejections] == [
        ("motor_renewal", "batch_001", 1, "invalid_phone"),
        ("motor_renewal", "batch_001", 2, "missing_required:customer_name"),
    ]
    rejection_text = " ".join(str(value) for row in rejections for value in row)
    assert "9876543210" not in rejection_text
    assert "Asha" not in rejection_text
    assert "Ravi" not in rejection_text

    leads = all_leads(seeded_conn)
    assert len(leads) == 1
    assert lead_attributes(leads[0]) == {
        "policy_number": "POL-3",
        "preferred_language": "en",
    }


def test_first_failure_in_schema_order_becomes_the_single_reason(
    tmp_path, seeded_conn, schemas, hasher, cipher
):
    drop = tmp_path / "drop_1"
    write_lead_file(
        drop,
        "motor_renewal",
        "batch_001.csv",
        MOTOR_HEADER,
        [
            ["", "123", "", "", "", ""],
            ["Asha", "", "", "", "", ""],
            ["Asha", "123", "", "", "", ""],
            ["Asha", "9876543210", "", "", "", ""],
        ],
    )

    result = ingest_drop(
        seeded_conn, drop, schemas, hasher=hasher, cipher=cipher
    )

    assert result.rows_rejected == 4
    assert result.leads_created == 0
    reasons = [
        row[0]
        for row in seeded_conn.execute(
            "SELECT reason FROM rejections ORDER BY row_number"
        ).fetchall()
    ]
    assert reasons == [
        "missing_required:customer_name",
        "missing_required:phone",
        "invalid_phone",
        "missing_required:policy_number",
    ]


def test_duplicate_rows_merge_last_non_null_and_keep_first_batch(
    tmp_path, seeded_conn, schemas, hasher, cipher
):
    drop = tmp_path / "drop_1"
    write_lead_file(
        drop,
        "motor_renewal",
        "batch_001.csv",
        MOTOR_HEADER,
        [
            ["Asha", "09876543210", "POL-1", "01-12-2026", "₹1,000.50", "HI"],
        ],
    )
    # Renamed and re-ordered columns are still matched by alias.
    write_lead_file(
        drop,
        "motor_renewal",
        "batch_002.csv",
        ["name", "contact", "policy", "expiry", "premium", "language"],
        [
            ["Asha Updated", "+91 98765 43210", "POL-2", "", "", ""],
        ],
    )

    result = ingest_drop(
        seeded_conn, drop, schemas, hasher=hasher, cipher=cipher
    )

    assert result.files_seen == 2
    assert result.rows_total == 2
    assert result.rows_rejected == 0
    assert result.duplicates_merged == 1
    assert result.leads_created == 1
    assert result.field_warnings == 0

    leads = all_leads(seeded_conn)
    assert len(leads) == 1
    lead = leads[0]
    assert lead["batch_id"] == "batch_001"
    assert lead["external_ref"] == "POL-2"
    assert lead_attributes(lead) == {
        "policy_number": "POL-2",
        "policy_expiry": "2026-12-01",
        "premium_amount": "1000.50",
        "preferred_language": "hi",
    }
    assert pii_value(seeded_conn, lead, "customer_name", cipher) == "Asha Updated"
    assert pii_value(seeded_conn, lead, "phone", cipher) == "+919876543210"
    assert seeded_conn.execute("SELECT COUNT(*) FROM lead_pii").fetchone()[0] == 2


def test_defaults_apply_after_merging_and_do_not_overwrite_values(
    tmp_path, seeded_conn, schemas, hasher, cipher
):
    drop = tmp_path / "drop_1"
    write_lead_file(
        drop,
        "motor_renewal",
        "batch_001.csv",
        MOTOR_HEADER,
        [
            ["Default En", "9988776655", "POL-A", "", "", ""],
            ["Explicit Hi", "9876500001", "POL-B", "", "", "hi"],
        ],
    )
    write_lead_file(
        drop,
        "motor_renewal",
        "batch_002.csv",
        MOTOR_HEADER,
        [
            ["Default En", "+91 99887 76655", "POL-A", "", "", ""],
            ["Explicit Hi", "09876500001", "POL-B", "", "", ""],
        ],
    )

    result = ingest_drop(
        seeded_conn, drop, schemas, hasher=hasher, cipher=cipher
    )

    assert result.leads_created == 2
    assert result.duplicates_merged == 2

    languages = {
        lead_attributes(lead)["preferred_language"] for lead in all_leads(seeded_conn)
    }
    assert languages == {"en", "hi"}


def test_same_phone_in_two_workflows_is_two_leads(
    tmp_path, seeded_conn, schemas, hasher, cipher
):
    drop = tmp_path / "drop_1"
    write_lead_file(
        drop,
        "motor_renewal",
        "batch_001.csv",
        MOTOR_HEADER,
        [["Asha", "9876543210", "POL-1", "", "", "hi"]],
    )
    write_lead_file(
        drop,
        "policy_feedback",
        "batch_001.csv",
        FEEDBACK_HEADER,
        [["Asha", "9876543210", "ORD-1", "Earbuds", "2026-09-13", "hi"]],
    )

    result = ingest_drop(
        seeded_conn, drop, schemas, hasher=hasher, cipher=cipher
    )

    assert result.files_seen == 2
    assert result.leads_created == 2
    assert result.duplicates_merged == 0

    leads = all_leads(seeded_conn)
    assert {lead["workflow_id"] for lead in leads} == {
        "motor_renewal",
        "policy_feedback",
    }
    assert {lead["phone_hmac"] for lead in leads} == {hasher.blind_index("9876543210")}


def test_duplicate_headers_map_to_the_first_matching_column(
    tmp_path, seeded_conn, schemas, hasher, cipher
):
    drop = tmp_path / "drop_1"
    write_lead_file(
        drop,
        "motor_renewal",
        "batch_001.csv",
        ["customer_name", "name", "phone", "mobile", "policy_number"],
        [["First", "Second", "9876543210", "9123456780", "POL-1"]],
    )

    result = ingest_drop(
        seeded_conn, drop, schemas, hasher=hasher, cipher=cipher
    )

    assert result.leads_created == 1
    lead = all_leads(seeded_conn)[0]
    assert pii_value(seeded_conn, lead, "customer_name", cipher) == "First"
    assert pii_value(seeded_conn, lead, "phone", cipher) == "+919876543210"


def test_custom_workflow_schema_is_ingested_dynamically(
    tmp_path, conn, hasher, cipher
):
    schema_dir = write_schemas_dir(
        tmp_path / "schemas",
        {
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
                    "aliases": ["contact number"],
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
                    "aliases": ["amount"],
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
        },
    )
    seed_metadata(conn, schema_dir)
    schemas = load_workflow_schemas(schema_dir)

    drop = tmp_path / "drop_1"
    write_lead_file(
        drop,
        "loan_recovery",
        "batch_001.csv",
        ["contact number", "Borrower", "account", "due", "amount", "bucket", "ignored"],
        [["9123456780", "Ravi", "LN-1", "31/12/2026", "Rs 1,200", "HIGH", "x"]],
    )

    result = ingest_drop(conn, drop, schemas, hasher=hasher, cipher=cipher)

    assert result.leads_created == 1
    lead = all_leads(conn)[0]
    assert lead["workflow_id"] == "loan_recovery"
    assert lead["external_ref"] == "LN-1"
    assert lead_attributes(lead) == {
        "loan_account": "LN-1",
        "due_date": "2026-12-31",
        "outstanding_amount": "1200.00",
        "priority": "high",
    }
    assert pii_value(conn, lead, "borrower_name", cipher) == "Ravi"
    assert pii_value(conn, lead, "mobile_number", cipher) == "+919123456780"
    assert pii_value(conn, lead, "phone", cipher) is None


def test_required_invalid_non_phone_field_rejects_with_field_reason(
    tmp_path, conn, hasher, cipher
):
    schema_dir = write_schemas_dir(
        tmp_path / "schemas",
        {
            "workflow_id": "kyc_review",
            "description": "KYC follow-up.",
            "fields": [
                {"name": "full_name", "type": "string", "required": True},
                {"name": "phone", "type": "phone", "required": True},
                {
                    "name": "case_id",
                    "type": "string",
                    "required": True,
                    "reference": True,
                },
                {"name": "start_date", "type": "date", "required": True},
            ],
        },
    )
    seed_metadata(conn, schema_dir)
    schemas = load_workflow_schemas(schema_dir)

    drop = tmp_path / "drop_1"
    write_lead_file(
        drop,
        "kyc_review",
        "batch_001.csv",
        ["full_name", "phone", "case_id", "start_date"],
        [["Asha", "9876543210", "CASE-1", "31/02/2026"]],
    )

    result = ingest_drop(conn, drop, schemas, hasher=hasher, cipher=cipher)

    assert result.rows_rejected == 1
    assert result.leads_created == 0
    reason = conn.execute("SELECT reason FROM rejections").fetchone()[0]
    assert reason == "invalid_start_date"
    assert all_leads(conn) == []


def test_reingesting_the_same_drop_is_a_noop(
    tmp_path, seeded_conn, schemas, hasher, cipher
):
    drop = tmp_path / "drop_1"
    write_lead_file(
        drop,
        "motor_renewal",
        "batch_001.csv",
        MOTOR_HEADER,
        [
            ["Asha", "9876543210", "POL-1", "", "", ""],
            ["", "123", "POL-2", "", "", ""],
        ],
    )

    first = ingest_drop(
        seeded_conn, drop, schemas, hasher=hasher, cipher=cipher
    )
    before = snapshot(seeded_conn)

    second = ingest_drop(
        seeded_conn, drop, schemas, hasher=hasher, cipher=cipher
    )
    after = snapshot(seeded_conn)

    assert first.files_processed == 1
    assert first.leads_created == 1
    assert first.rows_rejected == 1
    assert second.files_seen == 1
    assert second.files_processed == 0
    assert second.files_skipped == 1
    assert second.rows_total == 0
    assert second.rows_rejected == 0
    assert second.leads_created == 0
    assert after == before


def test_changed_content_at_the_same_path_is_rejected_without_writes(
    tmp_path, seeded_conn, schemas, hasher, cipher
):
    drop = tmp_path / "drop_1"
    path = write_lead_file(
        drop,
        "motor_renewal",
        "batch_001.csv",
        MOTOR_HEADER,
        [["Asha", "9876543210", "POL-1", "", "", ""]],
    )
    ingest_drop(seeded_conn, drop, schemas, hasher=hasher, cipher=cipher)
    before = snapshot(seeded_conn)

    path.write_text(
        "customer_name,phone,policy_number\n"
        "Ravi,9123456780,POL-2\n",
        encoding="utf-8",
    )

    with pytest.raises(IngestError, match="content changed"):
        ingest_drop(seeded_conn, drop, schemas, hasher=hasher, cipher=cipher)

    assert snapshot(seeded_conn) == before


def test_per_file_drop_and_global_stats(
    tmp_path, seeded_conn, schemas, hasher, cipher
):
    drop = tmp_path / "drop_1"
    write_lead_file(
        drop,
        "motor_renewal",
        "batch_001.csv",
        MOTOR_HEADER,
        [
            ["Asha", "9000000001", "POL-1", "31/02/2026", "", ""],
            ["", "123", "POL-2", "", "", ""],
            [""],
        ],
    )
    write_lead_file(
        drop,
        "policy_feedback",
        "batch_001.csv",
        FEEDBACK_HEADER,
        [
            ["Ravi", "9000000002", "ORD-1", "", "", ""],
            ["Ravi", "+91 90000 00002", "ORD-2", "", "", ""],
        ],
    )

    result = ingest_drop(
        seeded_conn, drop, schemas, hasher=hasher, cipher=cipher
    )

    assert result.files_seen == 2
    assert result.files_processed == 2
    assert result.rows_total == 4
    assert result.rows_rejected == 1
    assert result.duplicates_merged == 1
    assert result.field_warnings == 1
    assert result.leads_created == 2

    motor_scope = "drop_1/leads/motor_renewal/batch_001.csv"
    assert stat_value(seeded_conn, "file", motor_scope, "lead_rows_total") == 2
    assert stat_value(seeded_conn, "file", motor_scope, "lead_rows_rejected") == 1
    assert stat_value(seeded_conn, "file", motor_scope, "duplicate_lead_rows_merged") == 0
    assert stat_value(seeded_conn, "file", motor_scope, "field_warnings") == 1
    assert stat_value(seeded_conn, "file", motor_scope, "leads_created") == 1
    assert stat_value(seeded_conn, "file", motor_scope, "files_processed") == 1

    feedback_scope = "drop_1/leads/policy_feedback/batch_001.csv"
    assert stat_value(seeded_conn, "file", feedback_scope, "lead_rows_total") == 2
    assert stat_value(seeded_conn, "file", feedback_scope, "leads_created") == 1
    assert stat_value(seeded_conn, "file", feedback_scope, "duplicate_lead_rows_merged") == 1

    assert stat_value(seeded_conn, "drop", "drop_1", "lead_rows_total") == 4
    assert stat_value(seeded_conn, "drop", "drop_1", "lead_rows_rejected") == 1
    assert stat_value(seeded_conn, "drop", "drop_1", "duplicate_lead_rows_merged") == 1
    assert stat_value(seeded_conn, "drop", "drop_1", "field_warnings") == 1
    assert stat_value(seeded_conn, "drop", "drop_1", "leads_created") == 2
    assert stat_value(seeded_conn, "drop", "drop_1", "files_processed") == 2

    assert stat_value(seeded_conn, "global", "all", "lead_rows_total") == 4
    assert stat_value(seeded_conn, "global", "all", "lead_rows_rejected") == 1
    assert stat_value(seeded_conn, "global", "all", "duplicate_lead_rows_merged") == 1
    assert stat_value(seeded_conn, "global", "all", "field_warnings") == 1
    assert stat_value(seeded_conn, "global", "all", "leads_created") == 2
    assert stat_value(seeded_conn, "global", "all", "files_processed") == 2


def test_discovery_returns_files_in_workflow_and_filename_order(
    tmp_path, schemas
):
    drop = tmp_path / "drop_1"
    write_lead_file(drop, "policy_feedback", "batch_001.csv", FEEDBACK_HEADER, [])
    write_lead_file(drop, "motor_renewal", "batch_002.csv", MOTOR_HEADER, [])
    write_lead_file(drop, "motor_renewal", "batch_001.csv", MOTOR_HEADER, [])

    discovered = discover_lead_files(drop, schemas)

    assert [
        (workflow.workflow_id, path.name) for workflow, path in discovered
    ] == [
        ("motor_renewal", "batch_001.csv"),
        ("motor_renewal", "batch_002.csv"),
        ("policy_feedback", "batch_001.csv"),
    ]


def test_missing_leads_directory_returns_no_files(tmp_path, schemas):
    drop = tmp_path / "drop_1"
    drop.mkdir()
    assert discover_lead_files(drop, schemas) == []


def test_unknown_workflow_directory_raises(tmp_path, schemas):
    drop = tmp_path / "drop_1"
    write_lead_file(drop, "not_a_workflow", "batch_001.csv", MOTOR_HEADER, [])

    with pytest.raises(IngestError, match="unknown workflow directory"):
        discover_lead_files(drop, schemas)


def test_missing_drop_directory_raises(tmp_path, schemas):
    with pytest.raises(IngestError, match="drop directory not found"):
        discover_lead_files(tmp_path / "missing", schemas)


def test_ingest_drops_processes_drops_in_caller_order(
    tmp_path, seeded_conn, schemas, hasher, cipher
):
    first_drop = tmp_path / "first"
    second_drop = tmp_path / "second"
    write_lead_file(
        first_drop,
        "motor_renewal",
        "batch_001.csv",
        MOTOR_HEADER,
        [["Asha", "9876543210", "POL-1", "", "", "hi"]],
    )
    write_lead_file(
        second_drop,
        "motor_renewal",
        "batch_002.csv",
        MOTOR_HEADER,
        [["Asha", "9876543210", "POL-2", "", "", ""]],
    )

    results = ingest_drops(
        seeded_conn,
        [first_drop, second_drop],
        schemas,
        hasher=hasher,
        cipher=cipher,
    )

    assert [result.drop_key for result in results] == ["first", "second"]
    assert results[0].leads_created == 1
    assert results[1].duplicates_merged == 1

    lead = all_leads(seeded_conn)[0]
    assert lead["batch_id"] == "batch_001"
    assert lead["external_ref"] == "POL-2"
    assert lead_attributes(lead)["preferred_language"] == "hi"


def test_header_only_and_completely_empty_files_create_no_leads(
    tmp_path, seeded_conn, schemas, hasher, cipher
):
    drop = tmp_path / "drop_1"
    write_lead_file(drop, "motor_renewal", "batch_001.csv", MOTOR_HEADER, [])
    empty = drop / "leads" / "policy_feedback" / "batch_001.csv"
    empty.parent.mkdir(parents=True, exist_ok=True)
    empty.write_bytes(b"")

    result = ingest_drop(
        seeded_conn, drop, schemas, hasher=hasher, cipher=cipher
    )

    assert result.files_seen == 2
    assert result.files_processed == 2
    assert result.rows_total == 0
    assert result.leads_created == 0
    assert all_leads(seeded_conn) == []
    assert seeded_conn.execute("SELECT COUNT(*) FROM ingest_files").fetchone()[0] == 2
    assert stat_value(
        seeded_conn,
        "file",
        "drop_1/leads/motor_renewal/batch_001.csv",
        "lead_rows_total",
    ) == 0
    assert stat_value(seeded_conn, "drop", "drop_1", "lead_rows_rejected") == 0
    assert stat_value(seeded_conn, "global", "all", "field_warnings") == 0
    assert stat_value(seeded_conn, "global", "all", "files_processed") == 2


def test_utf8_bom_header_is_matched(
    tmp_path, seeded_conn, schemas, hasher, cipher
):
    drop = tmp_path / "drop_1"
    path = drop / "leads" / "motor_renewal" / "batch_001.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        "\ufeffcustomer_name,phone,policy_number\nAsha,9876543210,POL-1\n".encode(
            "utf-8"
        )
    )

    result = ingest_drop(
        seeded_conn, drop, schemas, hasher=hasher, cipher=cipher
    )

    assert result.leads_created == 1
    lead = all_leads(seeded_conn)[0]
    assert lead["external_ref"] == "POL-1"
    assert pii_value(seeded_conn, lead, "customer_name", cipher) == "Asha"


def test_invalid_utf8_rolls_back_without_writes(
    tmp_path, seeded_conn, schemas, hasher, cipher
):
    drop = tmp_path / "drop_1"
    path = drop / "leads" / "motor_renewal" / "batch_001.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe\x00\x00invalid")

    with pytest.raises(IngestError, match="not valid UTF-8"):
        ingest_drop(seeded_conn, drop, schemas, hasher=hasher, cipher=cipher)

    assert all_leads(seeded_conn) == []
    assert seeded_conn.execute("SELECT COUNT(*) FROM ingest_files").fetchone()[0] == 0
    assert seeded_conn.execute("SELECT COUNT(*) FROM processing_stats").fetchone()[0] == 0

