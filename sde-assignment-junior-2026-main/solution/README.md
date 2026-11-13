# Assignment A — Lead, Event & Report Pipeline

This folder contains the SQLite storage, security foundation, and the complete
**lead + event ingestion and reporting pipeline** for Agentic Universe
Assignment A:

- versioned schema migrations
- PII protection (AES-256-GCM + HMAC phone blind index)
- an append-only event ledger with mutable derived links
- dynamic workflow-schema loading
- header/alias mapping, validation, normalisation, rejection, duplicate merging
- JSONL event ingestion with malformed/duplicate/orphan/unknown-disposition rules
- phone and call-id event linking, persistent orphans, late-lead resolution
- UTC/IST timestamp normalisation
- per-file ingestion tracking, idempotency, and scoped statistics
- all five business reports with the exact sort and formatting contract

`report` rebuilds facts from everything ingested so far, so late leads and late
overrides are reflected automatically.

## Project layout

```
solution/
├── pyproject.toml
├── requirements.txt
├── .env.example              # placeholders only; real keys are gitignored
├── .gitignore
├── README.md
├── ledger.sqlite             # created locally, gitignored
├── .env                      # generated keys, gitignored
├── src/au_ledger/
│   ├── config.py             # key generation/loading, .env handling
│   ├── crypto.py             # phone canonicalisation, HMAC, AES-256-GCM
│   ├── db.py                 # connection PRAGMAs, migrations, init_db()
│   ├── errors.py             # exception hierarchy without PII values
│   ├── event_ingestion.py    # JSONL discovery, dedupe, parsing, stats
│   ├── event_resolution.py   # phone/call orphan linking, quality snapshots
│   ├── ingestion.py          # lead discovery, validation, merging, stats
│   ├── metadata.py           # seeds workflow schemas and dispositions
│   ├── migrations.py         # versioned schema SQL, indexes, triggers, view
│   ├── normalization.py      # header, phone, date, decimal and enum parsing
│   ├── reports.py            # business facts and the five report writers
│   ├── repository.py         # safe lead/event/link/rejection/stat primitives
│   ├── schema.py             # dynamic workflow schema loading and validation
│   ├── transactions.py       # BEGIN IMMEDIATE transaction context manager
│   ├── util.py               # UTC timestamps and stable JSON
│   └── cli.py                # init-keys, init-db, check-db, ingest-*, report
└── tests/
    ├── conftest.py
    ├── test_migrations.py
    ├── test_crypto.py
    ├── test_ledger.py
    ├── test_leads_pii.py
    ├── test_transactions.py
    ├── test_rejections_stats.py
    ├── test_normalization.py
    ├── test_schema_loading.py
    ├── test_lead_ingestion.py
    ├── test_event_ingestion.py
    └── test_reports.py
```

## Setup

The repository uses a local virtual environment at the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r solution\requirements.txt
```

The package uses a `src/` layout but does not need to be installed for the
tests; `solution/tests/conftest.py` adds `solution/src` to `sys.path`. To use
the CLI from a checkout without installing:

```powershell
$env:PYTHONPATH = "solution/src"
.\.venv\Scripts\python.exe -m au_ledger --help
```

Alternatively install it in editable mode:

```powershell
.\.venv\Scripts\python.exe -m pip install -e solution
```

### 1. Generate keys

```powershell
$env:PYTHONPATH = "solution/src"
.\.venv\Scripts\python.exe -m au_ledger init-keys --out solution\.env
```

This writes two independent base64-encoded 32-byte keys to `solution/.env`:

| Variable | Purpose |
| --- | --- |
| `AU_PII_ENC_KEY` | AES-256-GCM encryption of PII values |
| `AU_PII_HMAC_KEY` | Deterministic HMAC-SHA256 phone blind index |

The command never prints key material. Existing key files are not overwritten
unless `--force` is passed. `solution/.env`, `solution/.keys/`, database files,
WAL/SHM files, and virtual environments are excluded by `solution/.gitignore`.

Keep a secure backup of the keys. Losing them makes existing encrypted PII and
phone indexes unrecoverable.

### 2. Initialise the database

`init-db` does **not** need the PII keys. It creates the schema and seeds the
workflow/disposition metadata dynamically from the schema directory:

```powershell
.\.venv\Scripts\python.exe -m au_ledger init-db `
  --db solution\ledger.sqlite `
  --schemas assignment-a-data\schemas
```

Use `--no-metadata` to create only the tables. Re-running is idempotent.

Inspect safe counts without printing any PII:

```powershell
.\.venv\Scripts\python.exe -m au_ledger check-db --db solution\ledger.sqlite
```

### 3. Ingest lead and event drops

The combined `ingest` command processes each drop's leads first, then its
events, in the order the drops are given:

```powershell
$env:PYTHONPATH = "solution/src"
.\.venv\Scripts\python.exe -m au_ledger ingest `
  data\drop_1 data\drop_2 `
  --db solution\ledger.sqlite `
  --schemas assignment-a-data\schemas `
  --env solution\.env
```

The two stages can also be run separately:

```powershell
.\.venv\Scripts\python.exe -m au_ledger ingest-leads data\drop_1 data\drop_2 `
  --db solution\ledger.sqlite --schemas assignment-a-data\schemas --env solution\.env

.\.venv\Scripts\python.exe -m au_ledger ingest-events data\drop_1 data\drop_2 `
  --db solution\ledger.sqlite --schemas assignment-a-data\schemas --env solution\.env
```

Drops are processed in the order given. Within a drop, workflow directories and
CSV filenames are processed in sorted order and rows in file order; event JSONL
files are processed in sorted filename order and lines in file order. Re-running
a drop with unchanged file bytes is a no-op. If a file at an already-ingested
path has different bytes, ingestion raises `IngestError` rather than silently
re-interpreting history.

All three commands require the PII keys: accepted leads write encrypted PII and
a blind index, and event phones must be reduced to the same HMAC. They
create/migrate the database and seed metadata first. Event-only ingestion also
re-runs orphan resolution, so a late lead from a separately ingested drop can
claim earlier events.

### 4. Generate the five reports

`report` re-resolves any newly linkable orphan events, refreshes the quality
snapshots, rebuilds every business fact from the full ledger, and writes the
files atomically:

```powershell
.\.venv\Scripts\python.exe -m au_ledger report `
  --db solution\ledger.sqlite `
  --env solution\.env `
  --out reports
```

The output directory receives exactly `batch_summary.csv`,
`daily_overview.csv`, `lead_report.csv`, `rejections.csv`, and
`data_quality.json`.

### 5. Run the tests

```powershell
.\.venv\Scripts\python.exe -m pytest solution\tests
```

## Lead ingestion pipeline

The pipeline is entirely data-driven. Field names, types, required/PII/reference
flags, enum values, aliases, and defaults come from the workflow schema files.
No `motor_renewal` or `policy_feedback` field name is hardcoded in the ingestion
logic.

### Dynamic schema loading

`schema.load_workflow_schemas(schemas_dir)` loads every `*.json` workflow schema
in deterministic filename order and validates it before ingestion starts:

- `workflow_id`, a non-empty description, and a non-empty `fields` list
- unique field names, valid field types (`string`, `phone`, `date`, `decimal`, `enum`)
- boolean `required`, `pii`, and `reference` flags
- non-empty enum `values`, with no case-insensitive duplicates
- type-valid schema defaults
- **exactly one** `phone` field, and it must be required
- **exactly one** `reference: true` field, and it must not be PII
- header/alias keys that normalise to the same key must map to the same field

A field's position in the array is its schema order. That order controls the
single rejection reason a row receives. The supplied schemas produce two
workflows, 12 fields, and 14 dispositions. `customer_name` and `phone` are PII;
the reference fields are `policy_number` and `order_id`; `preferred_language`
defaults to `en`.

### Header matching

A header is normalised by lower-casing, trimming, and collapsing every run of
`_` or whitespace into one space. The result is matched against the field `name`
and each of its `aliases`, normalised the same way. Unmatched columns are
ignored. If two columns map to the same field, the first column wins and the
later duplicate is ignored.

### Rows, row numbers, and rejection

- Completely blank rows (every cell empty or whitespace) are skipped and are not
  counted.
- Row numbers count non-blank data rows within a file, starting at 1. The header
  is never counted.
- Cell values are trimmed; an empty value counts as missing.
- Fields are validated in schema order. The first failure becomes the row's one
  persistent reason:
  - required value missing → `missing_required:<field_name>`
  - phone present but invalid → `invalid_phone`
  - another required value present but invalid → `invalid_<field_name>`
- A present invalid phone always rejects the row because phone is the lead
  identity. Invalid non-phone optional fields become null and each adds one
  **field warning**. Warnings are counted only for accepted rows.
- Rejected rows create no lead and write a rejection record containing only the
  file, workflow, batch, row number, and reason — never the values.

### Type normalisation

| Type | Rule | Stored form |
| --- | --- | --- |
| `string` | trimmed value | string |
| `phone` | strip non-digits; drop `91` from 12 digits or `0` from 11; require exactly 10 digits starting `6`–`9` | `+91XXXXXXXXXX` |
| `date` | accept `YYYY-MM-DD`, `DD/MM/YYYY`, `DD-MM-YYYY`, `DD-Mon-YYYY`; day-first; month name case-insensitive; impossible dates invalid | `YYYY-MM-DD` |
| `decimal` | optional `₹`, `Rs`, `Rs.`, or `INR` prefix; remove commas; require ≥ 0; round to 2 places (half-up) | decimal string, e.g. `"1234.50"` |
| `enum` | compared case-insensitively with schema `values` | lower-case string |

### Duplicate merging

Lead identity is `(workflow_id, canonical phone HMAC)`. The first valid row for
an identity creates the lead and permanently keeps its file stem
(`batch_001`) as `batch_id`. Every later valid row for the same identity:

- counts as one merged duplicate
- overwrites only non-null fields (last non-null value wins)
- never changes `batch_id`, `phone_hmac`, or the lead id
- keeps existing values when the incoming duplicate has null fields

Defaults are applied **after** merging, only to fields that are still null. A
default therefore fills a missing or invalid optional field without overwriting
a value supplied by an earlier row. The reference field is also mirrored to
`leads.external_ref`.

### PII and storage

- Non-PII fields, including the reference value, are stored in
  `leads.attributes_json`.
- PII fields (including the phone) are encrypted with AES-256-GCM in `lead_pii`,
  one row per field. The phone ciphertext uses the schema's phone field name, so
  a custom workflow that calls its phone field `mobile_number` works too.
- Phone matching uses a deterministic HMAC-SHA256 blind index. Raw phones are
  never stored in a database column, log line, or exception message.
- Rejections contain no PII.

### Idempotency, transactions, and statistics

Each file is processed inside one `BEGIN IMMEDIATE` transaction. If any parse,
validation, or constraint step fails, all leads, PII, rejections, file tracking,
and stats for that file roll back.

`ingest_files` has a unique `(drop_key, relative_path)` row and stores the file's
SHA-256. Re-ingesting identical bytes returns a skipped result that changes
nothing. Statistics are written in the same transaction:

| Scope | Key | Counters |
| --- | --- | --- |
| `file` | `<drop_key>/<relative_path>` | `lead_rows_total`, `lead_rows_rejected`, `duplicate_lead_rows_merged`, `field_warnings`, `leads_created`, `files_processed` |
| `drop` | `drop_key` | the same counters accumulated across the drop |
| `global` | `all` | the same counters accumulated across all drops |

### Decided edge cases

These choices resolve points the brief leaves open; they are documented in code
comments where behaviour is non-obvious:

- `drop_key` is the drop directory's basename (`drop_1`, `drop_2`). Drop names
  are treated as stable identifiers, so stats are human-readable and re-running
  the same conceptual drop is idempotent.
- Files under a drop with no `leads/` directory produce an empty result. An
  unknown directory under `leads/` raises `IngestError` rather than silently
  discarding data.
- Only `*.csv` files directly inside `leads/<workflow_id>/` are ingested.
- A missing required value is `missing_required:<field>`; an invalid present
  required non-phone value is `invalid_<field>` (a present invalid phone is
  always `invalid_phone`). The supplied schemas only mark string and phone
  fields required, so `invalid_<field>` appears only for custom schemas.
- Decimal rounding is half-up, the common currency convention.
- Date parsing accepts abbreviated or full English month names and one- or
  two-digit day/month components; it never accepts a different order.
- A changed file at an already-tracked path is an error. Re-uploading the same
  batch with corrections should use a new filename/batch id, preserving history.
- Header rows are trusted as the column definition; duplicate headers follow the
  first-column-wins rule above.

## Event ingestion pipeline

Event files are JSON Lines under `events/*.jsonl` and are discovered in sorted
filename order. Each non-empty physical line is classified in the assignment's
exact order. A blank or whitespace-only line is skipped and not counted.

### 1. Malformed

A line is malformed when any of these is true:

- it is not valid JSON
- the JSON value is not an object
- `event_id` is missing, not a string, or blank

Malformed lines increment `malformed_events`, do **not** create a ledger row,
and do not stop the rest of the file.

### 2. Duplicate event ID

`event_id` is the ledger's natural key and is unique across every drop. A
replayed ID increments `duplicate_events_dropped` and is skipped. The original
row's payload, timestamps, source file, and line number are immutable and are
never rewritten by the duplicate. Duplicate detection happens before any
linking, so a duplicate never creates a second link.

### 3. Orphan handling

Every other line is inserted into the append-only `events` table. Linking is
derived state in `event_links`:

- a non-override event links by `(workflow_id, phone_hmac)` using the same phone
  normalisation as lead ingestion
- a `disposition.override` never links by phone. It links by `call_id` to the
  lead of another linked event on the same call
- an event whose workflow is missing or unknown in the metadata, whose phone is
  invalid/missing, or whose call has no linked evidence remains unlinked

Unlinked events are intentionally kept. After every event drop, and after every
lead drop, `resolve_orphan_events` retries every current orphan. Resolution runs
phone links first and then call links in passes, so:

- an event that arrives before its lead resolves when the lead arrives
- an override that arrives before its call event resolves when the call event
  arrives
- an event that was an orphan after `drop_1` can be linked by `drop_2`
- event rows never change; only `event_links` is inserted/updated

### 4. Unknown dispositions

A linked `disposition.*` event is compared against the `dispositions` table
loaded from `dispositions.csv` for that event's workflow. Comparison is
case-sensitive and exact. A code that is absent, missing from the payload, or
not in that workflow's catalogue increments `unknown_disposition_events`.

Unknown disposition events remain in the ledger (append-only history) but are
excluded from business calculations. Because orphan rules run first, a
disposition event that is still unlinked counts only as an orphan. If a late lead
links it later, the current unknown-disposition snapshot increases by one.
Every linked unknown event is counted exactly once.

### Timestamp normalisation

The canonical storage format is **UTC in `occurred_at_utc`**, an ISO-8601 string
with an explicit `+00:00` offset. For report convenience, the same instant is
also mirrored to `occurred_at_ist` (`+05:30`) and `occurred_date_ist` (the IST
calendar date).

| Input | Interpretation |
| --- | --- |
| `2026-10-01T10:00:00Z` | UTC, converted to IST |
| `2026-10-01T10:00:00+05:30` | Offset-aware, converted to UTC |
| `2026-10-01 10:00:00` | Naive, interpreted as IST |

An unparseable or missing timestamp is stored as `NULL` in all three columns.
It does not make an otherwise valid event malformed.

### Event PII

- The event `phone` is normalised with the lead phone rules and reduced to the
  same deterministic HMAC blind index. No raw event phone is stored in any
  column.
- Only the nested `payload` object is persisted. Payload keys matching a PII
  field name or alias (for example `phone`, `customer_name`, `contact`) are
  removed recursively before storage. This guarantees the repository's PII guard
  cannot store plaintext and cannot abort a file on an event-level payload
  quirk.
- Event payloads are non-PII operational data such as `reason`, `duration_sec`,
  `end_reason`, `disposition_code`, and `overridden_by`.

### Event statistics

Event processing writes both cumulative line counters and current snapshots:

| Scope | Key | Counters |
| --- | --- | --- |
| `file` | `<drop_key>/<relative_path>` | `events_total`, `malformed_events`, `duplicate_events_dropped`, `events_inserted`, `files_processed` |
| `drop` | `drop_key` | the same counters accumulated across the drop |
| `global` | `all` | the same counters accumulated across all drops |
| `drop` | `drop_key` | current `orphan_events`, `unknown_disposition_events` snapshots |
| `global` | `all` | current `orphan_events`, `unknown_disposition_events` snapshots |

`events_total` counts every non-empty line, including malformed and duplicate
lines. Cumulative counters increment only when a file is first processed; the
snapshot counters are replaced after resolution and are safe to re-run.
`data_quality.json` will use the global snapshots for `orphan_events` and
`unknown_disposition_events`, because the contract says those counts reflect the
final state.

### Event idempotency and transactions

Each JSONL file is processed in one `BEGIN IMMEDIATE` transaction. The
`ingest_files` row stores the SHA-256 of the exact bytes, under the same unique
`(drop_key, relative_path)` constraint as lead files. Re-ingesting identical
bytes returns a skipped result; changed bytes at the same path raise
`IngestError` and roll back. Drop-level orphan resolution and quality-snapshot
refresh run in a second transaction after the file loop, so a skipped drop still
re-resolves against any leads ingested elsewhere.

### Decided event edge cases

- Only `*.jsonl` files directly under a drop's `events/` directory are ingested.
  A missing `events/` directory produces an empty result; a missing drop
  directory raises `IngestError`.
- Event IDs are preserved exactly (surrounding whitespace is part of the ID);
  only a missing or whitespace-only ID is malformed.
- An event whose `workflow_id` is not present in the workflow metadata is stored
  with a `NULL` workflow and remains an orphan. This preserves history without
  violating the workflow foreign key and without guessing a campaign.
- Overrides ignore any phone even if one is present, because the assignment says
  their `call_id` is authoritative.
- The order of events inside a file does not matter for linking; resolution runs
  after the whole drop for efficiency and still supports forward references.

## Database design

SQLite is opened with:

- `PRAGMA foreign_keys = ON` on every connection (verified before returning it)
- `PRAGMA journal_mode = WAL` for file databases
- `PRAGMA synchronous = NORMAL`
- `PRAGMA busy_timeout = 5000`
- `isolation_level = None`, so transactions are explicit

### Versioned schema

`schema_migrations(version, name, applied_at)` records every applied migration.
Migration `1` (`initial_schema`) creates all tables, indexes, triggers, and the
orphan view. All statements are idempotent (`IF NOT EXISTS`) and the runner
records the version after executing the migration, so a retry after a partial
failure is safe.

### Tables

| Table | Purpose | Important constraints |
| --- | --- | --- |
| `workflows` | One row per workflow from the JSON schemas | `workflow_id` PK |
| `workflow_fields` | Field metadata: ordinal, type, required, PII flag, reference flag, aliases | PK `(workflow_id, name)`, unique ordinal, FK to `workflows` |
| `dispositions` | Disposition catalogue per workflow | PK `(workflow_id, code)`, FK to `workflows` |
| `ingest_files` | One row per processed input file, with content hash and provenance | unique `(drop_key, relative_path)`, FK to `workflows` |
| `leads` | Canonical lead identity and non-PII attributes | unique `(workflow_id, phone_hmac)`, FK to `workflows` |
| `lead_pii` | Encrypted PII values, one row per field | PK `(lead_id, field_name)`, FK to `leads ON DELETE CASCADE` |
| `events` | Append-only event ledger row | `event_id` unique, FKs to `workflows` and `ingest_files` |
| `event_links` | Derived, mutable event-to-lead link | PK `event_id` (one active link), FKs to `events` and `leads` |
| `rejections` | Rejected input rows and reasons | unique `(ingest_file_id, row_number)`, FKs to file/workflow |
| `processing_stats` | Scoped counters (`global`, `drop`, `file`) | unique `(scope_type, scope_key, stat_name)` |

`events` deliberately has **no** `lead_id` column. Linkage is stored separately
so an event can be resolved later without rewriting the original event.
`events.workflow_id` is nullable so an event can still be preserved when its
workflow cannot be determined; when supplied, it must exist in `workflows`.

### Indexes

Indexes cover the expected access paths:

- lead lookup by workflow/reference and workflow/batch
- PII lookup by field name
- event lookup by workflow/call, workflow/phone HMAC, time, type, and source file
- links by lead
- rejections by workflow/batch/row and by reason
- ingest files by kind
- processing statistics by scope

Foreign-key enforcement is verified by a test that attempts to insert a
rejection for a non-existent file.

## Event ledger and orphan resolution

The event ledger is append-only:

- `events` stores the structured event payload, timestamps, source file id,
  and source line number. PII values are not copied into `payload_json`: the
  event phone is represented only by `phone_hmac`, and payload keys that match
  PII field names or aliases are removed recursively before insertion.
- SQLite triggers `events_no_update` and `events_no_delete` raise
  `IntegrityError` for any `UPDATE` or `DELETE` on `events`.
- `event_id` is unique, providing natural idempotency for replayed input.
- Duplicate `event_id` inserts are rejected by the unique constraint, and the
  ingestion pipeline detects them before insertion so they can be counted.

A late-arriving or corrected lead may need to claim an existing event. Instead
of modifying the event, the pipeline writes or updates `event_links`:

```python
with transaction(conn):
    link_event_to_lead(
        conn,
        event_id="evt_001",
        lead_id=lead_id,
        link_method="phone",  # or "call"
    )
```

`link_event_to_lead` upserts on `event_links.event_id`, so an event has at most
one active link and re-resolution changes only derived state. It also refuses
to link an event to a lead from a different workflow. The `orphan_events` view
returns every event with no link:

```sql
SELECT * FROM orphan_events;
```

This preserves the historical event while allowing phone-based or call-id-based
orphans to be resolved after later files are ingested. The ingestion pipeline
calls `resolve_orphan_events` after every lead and event drop, then replaces the
current quality snapshots with `refresh_event_quality_stats`.

## Report generation

`reports.py` rebuilds facts from the current ledger state on every run. It does
not cache derived answers, so late leads, late overrides, and late event links
are always reflected. Before building facts, `report` calls
`resolve_orphan_events` and `refresh_event_quality_stats` again; this makes the
command self-healing after a crash and correct when only `ingest-events` was
re-run.

### Business rules

- **Counted call:** a linked `call.initiated` event. The call key is
  `(workflow_id, call_id)`. If duplicate initiated events exist, the earliest
  `occurred_at` wins with an `event_id` tie-break, and the call still counts
  once.
- **Connected call:** a linked `call.connected` event for the same call and
  lead. A connected event without an initiated event does not create a call.
- **Effective disposition:** if the call has any valid
  `disposition.override`, take the latest override by `occurred_at` (higher
  `event_id` breaks ties). Otherwise take the latest valid `disposition.set`.
  Unknown catalogue codes are ignored and cannot erase a known disposition.
  Overrides never edit the append-only event ledger.
- **Lead bucket:** strictly first match of:
  1. `yet_to_reach_out` — no counted calls.
  2. `completed` — at least one call has a terminal effective disposition.
  3. `in_progress_telephony_failed` — at least one call and every call has a
     `call.telephony_failed` event.
  4. `in_progress_dialed` — everything else.
- **`current_disposition`:** the effective disposition of the most recent call
  by initiated time that has one. A later call with no disposition is skipped,
  not treated as an eraser.
- **`ever_success`:** any call's effective disposition has `is_success=true`
  after overrides.
- **Daily metrics:** grouped by the IST date of `call.initiated`, never the
  disposition/override date. `success_leads` counts distinct leads with at
  least one success call on that date.

### Exact output contract

| File | Sort order |
| --- | --- |
| `batch_summary.csv` | `workflow_id, batch_id` |
| `daily_overview.csv` | `workflow_id, date` |
| `lead_report.csv` | `workflow_id, external_ref`, then lead id as a deterministic tie-break |
| `rejections.csv` | `workflow_id, batch_id, row_number`, then file/id tie-breakers |

Formatting details:

- Headers and columns are exactly the five contracts in `assignment-a-data/README.md`; no extra columns.
- Booleans are lowercase `true`/`false`.
- `connectivity_rate` is `f"{connected_calls / total_calls:.4f}"`, so `1.0000` and `0.0000` are explicit.
- `last_call_at` is IST ISO-8601 with `+05:30`, whole seconds, no `Z`, and empty when there are no calls.
- `phone_masked` is `+91******` plus the last four digits. The phone is decrypted only while building the report; no plaintext last-four is stored.
- `rejections.csv` contains only workflow, batch, row number, and reason — no PII.
- `rejections_by_reason` includes only observed reasons, sorted lexicographically; it is `{}` when there are none.
- A batch with zero accepted leads is omitted from `batch_summary.csv`.
- Empty reports still write headers; `data_quality.json` is written with zeros.
- Files are written to a temporary file in the target directory and moved into place with `os.replace`, so a crash cannot leave a half-written report.

### Verified seed-42 output

The report command was run against the full seed-42 `small` ingestion:

```text
batch_summary.csv     6 rows
daily_overview.csv   33 workflow-date rows
lead_report.csv    1892 rows
rejections.csv      108 rows
data_quality.json  2057 lead rows, 108 rejected, 57 merged, 122 warnings,
                   8745 event lines, 8 malformed, 237 duplicate events,
                   462 final orphans, 18 unknown dispositions
```

The first daily row is `motor_renewal,2026-10-01,80,45,0.5625,8`, matching the
contract's four-decimal example style.

### Fresh-seed end-to-end verification

After the seed-42 run, the full sequence was repeated on two fresh seeds (`2026` and `456`)
using a standalone verifier that does not import `au_ledger`. For each seed it:

- ingested `drop_1`, re-ingested `drop_1`, then ingested `drop_2`;
- compared pre/post snapshots (tables, leads, events, links, rejections, ingest files, stats,
  orphan ids, and report hashes) — identical after the repeat;
- confirmed every original batch id was unchanged;
- confirmed orphan events became linked when `drop_2` arrived;
- generated all five reports and compared every row to independently recomputed facts;
- re-parsed raw event timestamps (`Z`, offset, naive IST) and checked stored UTC/IST values;
- recomputed event linking and the final orphan set from raw events plus final leads;
- scanned logical SQLite values, raw SQLite bytes, and `rejections.csv` for plaintext PII;
- compared sequential ingestion with a clean one-shot `ingest drop_1 drop_2` — identical
  snapshots (PII ciphertext excluded because AES-GCM uses random nonces).

```text
seed 2026: 1912 leads, 8748 events, 8326 links, 422 orphans, 21 unknown,
           70 merges, 88 rejections, 6 batch rows, 34 daily rows
seed 456:  1882 leads, 8641 events, 8039 links, 602 orphans, 17 unknown,
           48 merges, 118 rejections, 6 batch rows, 32 daily rows
```

Both seeds ended with `ALL CHECKS PASSED`. See `SUBMISSION.md` for the full result table.

## Security design

### Phone matching: deterministic HMAC-SHA256

Raw phone values are canonicalised to `+91XXXXXXXXXX`:

- remove all non-digits
- drop a leading `91` from a 12-digit value
- drop a leading `0` from an 11-digit value
- require exactly 10 digits starting with 6, 7, 8, or 9

The canonical value is hashed with HMAC-SHA256 using a domain separator:

```
HMAC-SHA256(key, b"au-ledger:phone:v1:" + canonical_phone)
```

The 64-character lowercase hex digest is stored in `leads.phone_hmac` and
`events.phone_hmac`; both columns have a check constraint on the format. It is
deterministic for equality matching but cannot be computed by an attacker
without the HMAC key, unlike a plain SHA-256 of a phone number.

The canonical phone itself is also stored only as authenticated ciphertext in
`lead_pii`, under the workflow schema's phone field name (normally `phone`).
Raw phones are never written to a database column, log line, or exception
message.

### PII encryption: AES-256-GCM

Every workflow field marked `"pii": true` is encrypted before storage. In the
supplied schemas these are `customer_name` and `phone`.

- Algorithm: AES-256-GCM (authenticated encryption)
- Key: 32 bytes from `AU_PII_ENC_KEY`
- Nonce: fresh random 96-bit nonce per encryption
- Associated data: `au-ledger|<field_name>|v1`, binding ciphertext to its field
- Stored format: `v1:` followed by base64 of
  `version(1 byte) || nonce(12 bytes) || ciphertext+tag`

Because the nonce is random, encrypting the same value twice yields different
tokens. Equality matching therefore uses the HMAC blind index, never
ciphertext. Tampering or moving a token to a different field name fails
authentication and raises `CryptoError`.

`lead_pii` stores one ciphertext row per PII field. Non-PII attributes live in
`leads.attributes_json`. Repository helpers reject PII field names in plain
attribute dictionaries and event payloads, recursively, case-insensitively,
and including schema aliases such as `contact` and `insured name`, so `phone`
or `customer_name` values are not accidentally persisted in cleartext.

### Key handling

- Keys are loaded from `AU_PII_ENC_KEY` and `AU_PII_HMAC_KEY`.
- `init-keys` writes a local `.env` file with placeholders replaced; the file
  is gitignored and created with owner-only permissions where supported.
- `.env.example` contains only placeholder text.
- Existing process environment variables take precedence; a `.env` file is only
  used for missing variables.
- Keys are never written to Git, the database, or command output.
- `init-db` and `check-db` work without keys; `ingest-leads`, `ingest-events`,
  `ingest`, and PII reads need them.

### Logging and error hygiene

- Phone validation errors do not echo the supplied value.
- `mask_phone()` returns `+91******` plus the last four digits for any
  report-safe display.
- Crypto failures expose only generic reasons (`PII authentication failed`,
  `invalid PII token encoding`, etc.).
- The CLI logs paths, counts, and schema versions only, never key material or
  PII.

## Transactions

Writes for one input file must be wrapped in `transaction(conn)`:

```python
from au_ledger.transactions import transaction

with transaction(conn):
    # insert ingest_files row, leads, events, links, rejections, stats
    ...
```

The helper uses `BEGIN IMMEDIATE`, which acquires the write lock up front and
avoids SQLite's read-to-write upgrade deadlocks. On any exception it issues
`ROLLBACK` and re-raises. Nested transactions are rejected deliberately; the
design uses one transaction per file rather than savepoints. `insert_lead`
also refuses to run without an active transaction because it writes both the
lead row and its encrypted PII rows.

Each lead file has a file row created in the same transaction as its derived
data. If parsing, validation, or a constraint fails, all leads, PII, rejections,
stats, and the file row for that file disappear and the file can be retried.
`ingest_files.content_sha256` and unique source paths support idempotent
re-processing.

## Repository primitives

`solution/src/au_ledger/repository.py` provides the storage contract used by the
ingestion layer:

- `create_ingest_file(...)`
- `insert_lead(...)` with encrypted PII, HMAC identity, and dynamic phone field name
- `update_lead_fields(...)` for merge updates that preserve identity and batch
- `upsert_lead_pii(...)` / `get_lead_pii_field_names(...)`
- `get_lead_by_phone(...)`, `get_lead_pii(...)`
- `insert_event(...)` with immutable ledger semantics and PII-key rejection
- `link_event_to_lead(...)` for orphan resolution
- `insert_rejection(...)`
- `set_stat(...)` / `increment_stat(...)`
- `count_orphan_events(...)`

Event-specific derived-state helpers live in `event_resolution.py`:

- `resolve_orphan_events(...)` — phone then call-id linking, in passes
- `count_orphan_events_for_drop(...)`
- `count_unknown_disposition_events(...)`
- `refresh_event_quality_stats(...)`

JSONL parsing, line classification, timestamp canonicalisation, payload
sanitisation, and per-file statistics live in `event_ingestion.py`.

## Test coverage

| File | Coverage |
| --- | --- |
| `test_migrations.py` | versioned init, idempotent migrations and metadata, required tables/indexes, FK enforcement, HMAC format check, PII/reference metadata |
| `test_crypto.py` | phone canonicalisation variants, masking, HMAC determinism/key separation, AES-GCM round trip/randomisation/tamper detection, key loading/generation, `.env` no-overwrite |
| `test_ledger.py` | event append, immutability triggers, duplicate ids, source/workflow FKs, malformed HMAC rejection, orphan view, late-lead resolution, re-linking, cross-workflow link rejection, HMAC-only phone storage |
| `test_leads_pii.py` | ciphertext-only lead storage, no plaintext in a full SQL dump, blind-index lookup, per-workflow uniqueness, recursive/alias PII rejection, safe errors/masking |
| `test_transactions.py` | commit, rollback on exception, whole-file rollback on constraint failure, atomic lead+event write, active-transaction guard, nested transaction rejection |
| `test_rejections_stats.py` | rejection uniqueness/FKs, stat insert/update/increment, scope validation, transactional stats |
| `test_normalization.py` | header normalisation, all phone formats, four date formats and impossible dates, currency/decimal parsing and half-up rounding, enum case handling |
| `test_schema_loading.py` | actual schemas, dynamic custom schema, alias/name matching, unknown headers, duplicate headers, every schema validation failure |
| `test_lead_ingestion.py` | aliases/unknown columns/blank rows, row numbering, schema-order rejection reasons, optional warnings/null/defaults, duplicate merge + first batch, cross-workflow phones, duplicate header columns, custom dynamic workflow ingestion, required-invalid reasons, idempotency, changed-content rejection, per-file/drop/global stats, discovery order, UTF-8 BOM/empty/invalid files |
| `test_event_ingestion.py` | all six event types, malformed-line counting, physical line numbers, duplicate ids within/across drops, orphan persistence and late-lead resolution, call-id override linking and forward references, unknown disposition counting/case sensitivity/orphan precedence, UTC/offset/naive timestamps and null timestamps, phone normalisation and raw-phone exclusion, cross-workflow isolation, unknown workflows, idempotency, changed-content rejection, file/drop/global stats, payload PII removal, discovery order |
| `test_reports.py` | effective disposition (latest set, override precedence, latest override, equal-timestamp tie-break, unknown override fallback), counted/connected calls, duplicate initiated events, exact bucket priority, IST daily dates, distinct success leads, connectivity rounding, current-disposition skipping, ever-success after overrides, phone masking, report sorting/headers/booleans/empty values, rejection and data-quality contracts, late-lead self-healing, deterministic overwrite, empty reports |

### Actual test result

Run from the repository root:

```text
.\.venv\Scripts\python.exe -m pytest solution\tests
238 passed
```

Database initialisation also succeeds:

```text
schema_version=1, workflows=2, fields=12, dispositions=14
```

Lead ingestion was verified against the supplied seed-42 `small` data
(`data/drop_1` then `data/drop_2`):

```text
drop_1: 4 files, 1515 non-blank rows,  71 rejected, 29 merged, 83 warnings, 1415 created
drop_2: 2 files,  542 non-blank rows,  37 rejected, 28 merged, 39 warnings,  477 created
total:  6 files, 2057 non-blank rows, 108 rejected, 57 merged, 122 warnings, 1892 leads
```

The same run with event ingestion produced:

```text
drop_1 events: 4247 non-empty lines,   4 malformed,  83 duplicates, 4160 inserted
drop_2 events: 4498 non-empty lines,   4 malformed, 154 duplicates, 4340 inserted
total events:  8745 non-empty lines,   8 malformed, 237 duplicates, 8500 inserted
event links:   8038 (7972 phone, 66 call)
final quality: 462 unresolved orphans, 18 unknown-disposition events
```

All 70 `disposition.override` events were preserved. 66 linked by `call_id`
(including overrides that appeared before their call event or in a later drop);
the remaining 4 belong to calls that never produced a linkable lead and stay as
orphans. Re-running both drops skipped all eight files, kept the same 8500
events and 8038 links, and left every cumulative counter unchanged.

## Assignment A status

The lead pipeline, event pipeline, append-only ledger, PII protection, all five reports, the
submission narrative, and the AI-usage disclosure are complete. The core requirements were
verified with:

- `238 passed` in the pytest suite;
- fresh-seed end-to-end runs on seeds `2026` and `456`;
- re-ingest, batch-id, orphan-progress, sequential-vs-one-shot, report-contract, and PII
  checks;
- the independent raw-file verifier described above.

The remaining work is production hardening, not core functionality: PostgreSQL migration,
streaming/batched ingestion, key rotation, PII retention/erasure, incremental report facts,
and a 100k-leads/day load test. These are discussed in `SUBMISSION.md` under
"Production deployment" and "If I had another day".
