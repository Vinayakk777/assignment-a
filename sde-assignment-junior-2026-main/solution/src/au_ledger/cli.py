"""Command-line entry point for database and key initialization."""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

from .config import (
    DEFAULT_DB_PATH,
    DEFAULT_ENV_FILE,
    DEFAULT_SCHEMAS_DIR,
    KeyConfig,
    generate_keypair,
    write_env_file,
)
from .crypto import PhoneHasher, PiiCipher
from .db import connect, init_db, schema_version
from .errors import AuLedgerError
from .event_ingestion import EventDropResult, ingest_event_drop, ingest_event_drops
from .ingestion import ingest_drop, ingest_drops
from .reports import generate_reports
from .schema import load_workflow_schemas

LOG = logging.getLogger("au_ledger")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="au-ledger",
        description="Agentic Universe Assignment A database foundation",
    )
    parser.add_argument("--verbose", action="store_true", help="enable debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    init_db_parser = sub.add_parser("init-db", help="create/migrate SQLite and seed metadata")
    init_db_parser.add_argument("--db", default=DEFAULT_DB_PATH, help="SQLite database path")
    init_db_parser.add_argument(
        "--schemas",
        default=str(DEFAULT_SCHEMAS_DIR),
        help="directory containing workflow JSON files and dispositions.csv",
    )
    init_db_parser.add_argument(
        "--no-metadata",
        action="store_true",
        help="create schema only; do not load workflow/disposition metadata",
    )

    keys_parser = sub.add_parser("init-keys", help="generate local PII keys in an ignored .env file")
    keys_parser.add_argument("--out", default=str(DEFAULT_ENV_FILE), help="output .env path")
    keys_parser.add_argument("--force", action="store_true", help="replace an existing key file")

    check_parser = sub.add_parser("check-db", help="show schema version and safe table counts")
    check_parser.add_argument("--db", default=DEFAULT_DB_PATH, help="SQLite database path")

    def add_drop_arguments(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument(
            "drops",
            nargs="+",
            metavar="DROP",
            help="drop directory (or several, processed in the order given)",
        )
        command_parser.add_argument(
            "--db", default=DEFAULT_DB_PATH, help="SQLite database path"
        )
        command_parser.add_argument(
            "--schemas",
            default=str(DEFAULT_SCHEMAS_DIR),
            help="directory containing workflow schemas and dispositions.csv",
        )
        command_parser.add_argument(
            "--env",
            default=str(DEFAULT_ENV_FILE),
            help="optional .env file containing the PII keys",
        )

    ingest_parser = sub.add_parser(
        "ingest-leads",
        help="validate, normalise and merge lead CSV files",
    )
    add_drop_arguments(ingest_parser)

    events_parser = sub.add_parser(
        "ingest-events",
        help="append JSONL events, link leads, and quarantine orphans",
    )
    add_drop_arguments(events_parser)

    combined_parser = sub.add_parser(
        "ingest",
        help="ingest lead files then event files for each drop, in order",
    )
    add_drop_arguments(combined_parser)

    report_parser = sub.add_parser(
        "report",
        help="rebuild business facts and write the five report files",
    )
    report_parser.add_argument("--db", default=DEFAULT_DB_PATH, help="SQLite database path")
    report_parser.add_argument(
        "--out",
        default="reports",
        help="directory that receives batch_summary.csv and the other four files",
    )
    report_parser.add_argument(
        "--env",
        default=str(DEFAULT_ENV_FILE),
        help="optional .env file containing the PII keys",
    )
    return parser


def _log_event_drop(result: EventDropResult) -> None:
    LOG.info(
        "drop=%s event_files_seen=%d processed=%d skipped=%d "
        "lines=%d malformed=%d duplicates=%d inserted=%d "
        "current_orphans=%d current_unknown_dispositions=%d",
        result.drop_key,
        result.files_seen,
        result.files_processed,
        result.files_skipped,
        result.events_total,
        result.malformed_events,
        result.duplicate_events_dropped,
        result.events_inserted,
        result.orphan_events,
        result.unknown_disposition_events,
    )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        if args.command == "init-db":
            result = init_db(
                args.db,
                None if args.no_metadata else args.schemas,
            )
            LOG.info(
                "database initialized at %s (schema_version=%s, workflows=%s, fields=%s, dispositions=%s)",
                result["db_path"],
                result["schema_version"],
                result.get("workflows", 0),
                result.get("fields", 0),
                result.get("dispositions", 0),
            )
            return 0

        if args.command == "init-keys":
            enc_key, hmac_key = generate_keypair()
            target = write_env_file(args.out, enc_key, hmac_key, force=args.force)
            # Never print the keys themselves.
            LOG.info("PII keys written to %s; keep this file out of version control", target)
            return 0

        if args.command == "check-db":
            conn = connect(args.db)
            try:
                version = schema_version(conn)
                counts = {
                    name: int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
                    for name in (
                        "workflows",
                        "workflow_fields",
                        "dispositions",
                        "leads",
                        "events",
                        "event_links",
                        "rejections",
                        "ingest_files",
                        "processing_stats",
                    )
                }
                LOG.info("db=%s schema_version=%s counts=%s", args.db, version, counts)
            finally:
                conn.close()
            return 0

        if args.command == "ingest-leads":
            env_path = Path(args.env)
            keys = KeyConfig.from_env(env_path if env_path.exists() else None)
            schemas = load_workflow_schemas(args.schemas)
            init_db(args.db, args.schemas)
            conn = connect(args.db)
            try:
                results = ingest_drops(
                    conn,
                    list(args.drops),
                    schemas,
                    hasher=PhoneHasher(keys.hmac_key),
                    cipher=PiiCipher(keys.enc_key),
                )
            finally:
                conn.close()
            for result in results:
                LOG.info(
                    "drop=%s files_seen=%d files_processed=%d files_skipped=%d "
                    "rows=%d rejected=%d merged=%d warnings=%d leads_created=%d",
                    result.drop_key,
                    result.files_seen,
                    result.files_processed,
                    result.files_skipped,
                    result.rows_total,
                    result.rows_rejected,
                    result.duplicates_merged,
                    result.field_warnings,
                    result.leads_created,
                )
            return 0

        if args.command == "ingest-events":
            env_path = Path(args.env)
            keys = KeyConfig.from_env(env_path if env_path.exists() else None)
            schemas = load_workflow_schemas(args.schemas)
            init_db(args.db, args.schemas)
            conn = connect(args.db)
            try:
                event_results = ingest_event_drops(
                    conn,
                    list(args.drops),
                    schemas,
                    hasher=PhoneHasher(keys.hmac_key),
                )
            finally:
                conn.close()
            for result in event_results:
                _log_event_drop(result)
            return 0

        if args.command == "ingest":
            env_path = Path(args.env)
            keys = KeyConfig.from_env(env_path if env_path.exists() else None)
            schemas = load_workflow_schemas(args.schemas)
            init_db(args.db, args.schemas)
            hasher = PhoneHasher(keys.hmac_key)
            cipher = PiiCipher(keys.enc_key)
            conn = connect(args.db)
            try:
                for drop_dir in args.drops:
                    lead_result = ingest_drop(
                        conn,
                        drop_dir,
                        schemas,
                        hasher=hasher,
                        cipher=cipher,
                    )
                    event_result = ingest_event_drop(
                        conn, drop_dir, schemas, hasher=hasher
                    )
                    LOG.info(
                        "drop=%s leads_created=%d rows_rejected=%d rows_merged=%d",
                        lead_result.drop_key,
                        lead_result.leads_created,
                        lead_result.rows_rejected,
                        lead_result.duplicates_merged,
                    )
                    _log_event_drop(event_result)
            finally:
                conn.close()
            return 0

        if args.command == "report":
            env_path = Path(args.env)
            keys = KeyConfig.from_env(env_path if env_path.exists() else None)
            init_db(args.db)
            conn = connect(args.db)
            try:
                result = generate_reports(
                    conn, args.out, cipher=PiiCipher(keys.enc_key)
                )
            finally:
                conn.close()
            LOG.info(
                "report written to %s (batch=%d daily=%d leads=%d rejections=%d)",
                result.out_dir,
                result.batch_summary_rows,
                result.daily_overview_rows,
                result.lead_report_rows,
                result.rejection_rows,
            )
            return 0

        raise AuLedgerError("unknown command")
    except (AuLedgerError, sqlite3.Error, OSError) as exc:
        # Expected errors contain no PII values.
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
