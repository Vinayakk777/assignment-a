# Submission

## Candidate

- **Name:** `[Candidate name — not provided in this session]`
- **Assignment chosen:** A (Data) — Interaction Ledger & Reporting Pipeline
- **Time spent (honest estimate):** Not precisely tracked. Approximately 8–10 hours of
  equivalent engineering effort across implementation, debugging, tests, and verification.
  See [`AI_USAGE.md`](AI_USAGE.md) for the exact split between AI assistance and independent
  verification.

> Only the candidate name is intentionally left as a placeholder: no name was supplied
> during this work. Everything else below describes the code that is actually in this
> repository and the checks that were actually executed.

## How to run

These commands work from a clean clone at the repository root. Windows PowerShell is shown
first; the POSIX equivalent follows.

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r solution\requirements.txt

$env:PYTHONPATH = "solution/src"

# 1. Local PII keys (gitignored; never commit solution\.env)
.\.venv\Scripts\python.exe -m au_ledger init-keys --out solution\.env

# 2. Schema and workflow/disposition metadata
.\.venv\Scripts\python.exe -m au_ledger init-db `
  --db solution\ledger.sqlite --schemas assignment-a-data\schemas

# 3. Generate a deterministic synthetic dataset (gitignored)
.\.venv\Scripts\python.exe assignment-a-data\tools\generate_data.py `
  --seed 42 --scale small --out data

# 4. Ingest both drops in order
.\.venv\Scripts\python.exe -m au_ledger ingest data\drop_1 data\drop_2 `
  --db solution\ledger.sqlite --schemas assignment-a-data\schemas --env solution\.env

# 5. Write exactly the five report files
.\.venv\Scripts\python.exe -m au_ledger report `
  --db solution\ledger.sqlite --env solution\.env --out reports

# 6. Tests
.\.venv\Scripts\python.exe -m pytest solution\tests
```

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r solution/requirements.txt

export PYTHONPATH=solution/src

python -m au_ledger init-keys --out solution/.env
python -m au_ledger init-db --db solution/ledger.sqlite --schemas assignment-a-data/schemas
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data
python -m au_ledger ingest data/drop_1 data/drop_2 \
  --db solution/ledger.sqlite --schemas assignment-a-data/schemas --env solution/.env
python -m au_ledger report --db solution/ledger.sqlite --env solution/.env --out reports
python -m pytest solution/tests
```

`init-keys` refuses to overwrite an existing file. Use `--force` only when intentionally
rotating the local key pair. `solution/.env`, `solution/ledger.sqlite`, `data/`, and
`reports/` are gitignored. The implementation does not depend on the optional Postgres
container in `docker-compose.yml`; that file is part of the provided repository.

## Executive summary

The solution is a small, dependency-light Python package with a SQLite store. It separates
three concerns:

1. **Raw-file ingestion** — discover files deterministically, validate/normalise every row,
   track each file by SHA-256, and reject bad rows without leaking PII.
2. **An append-only event ledger** — store every valid event once, then keep event-to-lead
   linkage in a separate mutable projection (`event_links`) so late leads and late overrides
   can be resolved without rewriting history.
3. **Report facts as a projection** — rebuild calls, effective dispositions, buckets, daily
   metrics, and lead summaries from the full current ledger on every `report` run.

The key correctness idea is that **reports are projections, not running counters**. Late
leads, late overrides, re-runs, and incremental drops therefore converge to the same answer.

```text
raw drops
  ├─ leads/<workflow>/<batch>.csv ──► header mapping ─► validation/normalisation
  │                                                   ─► merge by (workflow, phone_hmac)
  │                                                   ─► leads + lead_pii + rejections
  └─ events/*.jsonl ───────────────► malformed/duplicate/orphan/unknown classification
                                                      ─► events (append-only)
                                                      ─► event_links (phone / call)
                                                      ─► processing_stats snapshots

ledger ──► report fact builder ──► batch_summary.csv
                                  daily_overview.csv
                                  lead_report.csv
                                  rejections.csv
                                  data_quality.json
```

## Architecture

### Package layout

| Module | Responsibility |
| --- | --- |
| `cli.py` | `init-keys`, `init-db`, `check-db`, `ingest-leads`, `ingest-events`, `ingest`, `report` |
| `config.py` | Key generation/loading, `.env` parsing, key validation |
| `crypto.py` | Phone canonicalisation, HMAC blind index, AES-256-GCM tokens, masking |
| `db.py` | Connection PRAGMAs, migration runner, metadata seeding, `init_db()` |
| `migrations.py` | Versioned schema SQL, indexes, append-only triggers, `orphan_events` view |
| `metadata.py` | Load workflow schemas and `dispositions.csv` into the database |
| `schema.py` | Dynamic workflow-schema validation and loading |
| `normalization.py` | Header, phone, date, decimal, and enum parsing |
| `ingestion.py` | Lead discovery, validation, merging, PII writes, stats |
| `event_ingestion.py` | JSONL discovery, line classification, timestamps, payload sanitisation |
| `event_resolution.py` | Phone/call linking, orphan passes, quality snapshots |
| `repository.py` | Lead/event/link/rejection/stat primitives with PII guards |
| `reports.py` | Fact builder and the five exact report writers |
| `transactions.py` | `BEGIN IMMEDIATE` transaction context manager |
| `util.py` | UTC timestamps and stable JSON serialisation |

### Runtime flow

- The CLI loads the workflow schemas and PII keys, ensures the database exists and metadata
  is seeded, then processes each drop in the order supplied.
- `ingest` processes a drop's lead files first, then its event files. This ordering lets a
  drop's events see leads from the same drop. Cross-drop late leads are handled by
  re-resolving orphans after every drop.
- Each input file is one logical unit of work. A `BEGIN IMMEDIATE` transaction covers the
  file row, accepted leads, encrypted PII, rejections, events, and statistics. A failure
  rolls the whole file back so it can be retried safely.
- `report` re-resolves any newly linkable orphans, refreshes quality snapshots, rebuilds
  facts from the ledger, and atomically replaces the five output files.

## Database design

SQLite is used because the assignment explicitly allows it and a single-file database keeps
the submission easy to run and inspect. The schema is deliberately portable to PostgreSQL.

### Connection settings

- `PRAGMA foreign_keys = ON` on every connection (checked before returning it)
- `PRAGMA journal_mode = WAL` for file databases
- `PRAGMA synchronous = NORMAL`
- `PRAGMA busy_timeout = 5000`
- `isolation_level = None`, so transactions are explicit

### Tables

| Table | Purpose | Important constraints |
| --- | --- | --- |
| `workflows` | One row per workflow schema | `workflow_id` PK |
| `workflow_fields` | Field order, type, required/PII/reference flags, aliases | PK `(workflow_id, name)`, unique ordinal, FK to workflows |
| `dispositions` | Per-workflow disposition catalogue | PK `(workflow_id, code)`, FK to workflows |
| `ingest_files` | One row per processed file, with content hash and provenance | unique `(drop_key, relative_path)`, FK to workflows |
| `leads` | Canonical lead identity and non-PII attributes | unique `(workflow_id, phone_hmac)`, FK to workflows |
| `lead_pii` | Encrypted PII values, one row per field | PK `(lead_id, field_name)`, FK to leads with cascade delete |
| `events` | Append-only event ledger | `event_id` unique, FKs to workflows and ingest files |
| `event_links` | Derived, mutable event-to-lead link | PK `event_id`, FKs to events and leads |
| `rejections` | Rejected rows and reasons | unique `(ingest_file_id, row_number)`, FKs |
| `processing_stats` | Scoped counters (`global`, `drop`, `file`) | unique `(scope_type, scope_key, stat_name)` |

`events` has no `lead_id` column. Linkage is a separate projection so an event can be
resolved later without updating the original row. `events.workflow_id` is nullable so an
event whose workflow cannot be determined is still preserved as an orphan rather than
discarded.

### Append-only ledger

SQLite triggers `events_no_update` and `events_no_delete` abort any `UPDATE` or `DELETE` on
`events`. Duplicate `event_id` inserts are rejected by the unique constraint. The pipeline
detects duplicates before insertion so it can count them, but the database constraint is the
final guard. Correcting linkage means upserting `event_links`, never editing `events`.

The `orphan_events` view is:

```sql
SELECT e.*
FROM events AS e
LEFT JOIN event_links AS l ON l.event_id = e.event_id
WHERE l.event_id IS NULL;
```

### Indexes

Indexes cover the access paths used by ingestion and reports:

- leads by `(workflow_id, external_ref)` and `(workflow_id, batch_id)`
- PII by `field_name`
- events by `(workflow_id, call_id)`, `(workflow_id, phone_hmac)`, `occurred_at_utc`,
  `event_type`, and `source_file_id`
- links by `lead_id`
- rejections by `(workflow_id, batch_id, row_number)` and `reason`
- ingest files by `kind`
- stats by `(scope_type, scope_key)`

### Migrations and metadata

`schema_migrations(version, name, applied_at)` records applied migrations. Migration `1`
creates all tables, indexes, triggers, and the orphan view. Statements use `IF NOT EXISTS`
and the runner records the version after execution, so a retry after a partial failure is
safe. `init-db` loads workflow fields and dispositions from the schema directory; `--no-metadata`
creates only the tables.

## Data processing

### Lead discovery and row handling

- Drop key is the drop directory basename (`drop_1`), used as a stable identifier.
- Only `*.csv` files directly inside `leads/<workflow_id>/` are ingested. Workflow
  directories and filenames are processed in sorted order; rows in file order.
- Completely blank rows are skipped and not counted. Row numbers count non-blank data rows
  starting at 1; the header is never counted.
- A header is normalised by lower-casing, trimming, and collapsing every run of `_` or
  whitespace to one space. It is matched against field names and aliases. Unknown columns
  are ignored; if two columns map to the same field, the first column wins.
- Fields are validated in schema order. The first failure becomes the row's single reason:
  - missing required value → `missing_required:<field>`
  - present but invalid phone → `invalid_phone`
  - present but invalid required non-phone → `invalid_<field>`
- An invalid optional non-phone field becomes null and adds one field warning. Warnings are
  counted only for accepted rows.
- Rejections store only workflow, batch, row number, and reason — never values.

### Normalisation

| Type | Rule | Stored form |
| --- | --- | --- |
| `string` | trim | string |
| `phone` | strip non-digits; drop leading `91` from 12 digits or `0` from 11; require 10 digits starting 6–9 | `+91XXXXXXXXXX` |
| `date` | `YYYY-MM-DD`, `DD/MM/YYYY`, `DD-MM-YYYY`, `DD-Mon-YYYY`; day-first; case-insensitive month; impossible dates invalid | `YYYY-MM-DD` |
| `decimal` | optional `₹`, `Rs`, `Rs.`, or `INR`; remove commas; require ≥ 0; round half-up to 2 places | decimal string |
| `enum` | case-insensitive comparison with schema values | lower-case string |

### Duplicate merging

Identity is `(workflow_id, canonical phone HMAC)`. The first valid row creates the lead and
keeps its file stem as `batch_id`. Every later valid row for the same identity:

- counts as one merged duplicate;
- overwrites only fields whose incoming value is non-null (last non-null wins);
- never changes `batch_id`, `phone_hmac`, or the lead id.

Defaults are applied **after** merging, only to fields still null. This means a default fills
a missing/invalid optional field without overwriting an explicit earlier value.

### Event classification order

Every non-empty physical line is processed in this exact order:

1. **Malformed** — not valid JSON, not a JSON object, or missing/blank/non-string
   `event_id`. Counted and skipped.
2. **Duplicate** — `event_id` already seen in any drop. Counted and skipped before linking.
3. **Orphan** — cannot be linked to a valid lead. Stored in the ledger but kept out of
   reports until resolved.
4. **Unknown disposition** — a linked `disposition.*` event whose code is not in the
   workflow's catalogue. Counted and ignored by business calculations.

### Timestamp normalisation

Canonical storage is UTC in `occurred_at_utc` (ISO-8601 with `+00:00`). The same instant is
mirrored to `occurred_at_ist` (`+05:30`) and `occurred_date_ist`.

| Input | Interpretation |
| --- | --- |
| `2026-10-01T10:00:00Z` | UTC |
| `2026-10-01T10:00:00+05:30` | Offset-aware, converted to UTC |
| `2026-10-01 10:00:00` | Naive, interpreted as IST |

An unparseable or missing timestamp becomes `NULL` in all three columns; it does not make an
otherwise valid event malformed.

### Event payloads

Only the nested `payload` object is persisted. Keys that match a PII field name or alias are
removed recursively before storage. This prevents `phone` or `customer_name` values from
appearing in `events.payload_json` even if an upstream producer puts them there.

## PII security

### Phone identity: HMAC-SHA256 blind index

Canonical phone values are hashed with a domain-separated HMAC:

```text
HMAC-SHA256(key, b"au-ledger:phone:v1:" + canonical_phone)
```

The 64-character lowercase hex digest is stored in `leads.phone_hmac` and
`events.phone_hmac`; both columns have a format check constraint. This gives deterministic
equality matching without storing the phone, and unlike a plain hash it cannot be computed
without the HMAC key.

### PII encryption: AES-256-GCM

Every field marked `"pii": true` is encrypted before storage. In the supplied schemas these
are `customer_name` and `phone`.

- 32-byte key from `AU_PII_ENC_KEY`
- fresh random 96-bit nonce per encryption
- associated data `au-ledger|<field_name>|v1`, binding a token to its field
- stored format: `v1:` + base64 of `version(1) || nonce(12) || ciphertext+tag`

Random nonces mean the same value encrypts differently each time. Equality matching uses the
HMAC index, never ciphertext. Tampering or moving a token to another field fails
authentication and raises a generic `CryptoError`.

### Key handling and leakage controls

- `init-keys` writes `solution/.env` with owner-only permissions where supported.
- `.env.example` contains placeholders only; `.env` is gitignored.
- Existing process environment variables take precedence over `.env`.
- Keys are never logged, written to the database, or printed by the CLI.
- Rejections contain no PII.
- Reports decrypt only the phone, only at report time, to produce `+91******` + last four.
- Repository helpers reject PII keys in plain attribute dictionaries and event payloads,
  recursively and case-insensitively, including schema aliases such as `contact`.

## Event linking and orphan resolution

`event_links` stores at most one active link per event (`event_id` PK). `link_event_to_lead`
upserts and refuses to link an event to a lead in a different workflow.

### Linking rules

- Non-override events link by `(workflow_id, phone_hmac)` using the same canonical phone
  rules as lead ingestion.
- `disposition.override` never links by phone. It links through `call_id` to the lead of
  another already-linked event on the same call, within the same workflow when the workflow
  is known.
- An event with no workflow, unknown workflow, invalid/missing phone, or no linked call
  evidence remains an orphan.

### Resolution algorithm

After every lead drop and every event drop, `resolve_orphan_events` retries every current
orphan:

1. Non-override events are linked by phone.
2. Overrides are retried in passes; each pass can unlock overrides that share a `call_id`
   with a newly linked event. The loop stops when a pass makes no progress.

This handles:

- an event arriving before its lead;
- an override arriving before its call event;
- an event that was an orphan after `drop_1` becoming linked in `drop_2`;
- repeated re-runs without rewriting event rows.

The `orphan_events` view and the current quality snapshot always reflect the final state.
An unknown disposition that is still an orphan counts only as an orphan; if a late lead links
it later, the current unknown-disposition snapshot increases by one.

## Idempotency and transactions

### File-level idempotency

`ingest_files` has a unique `(drop_key, relative_path)` row and stores the SHA-256 of the exact
bytes. Re-ingesting identical bytes returns a skipped result and changes nothing. If a file
at an already-tracked path has different bytes, ingestion raises `IngestError` rather than
silently reinterpreting history. Corrections should use a new filename/batch id.

### Event-level idempotency

`event_id` is unique across every drop. A replayed ID is counted as a duplicate and skipped
before linking, so it cannot create a second ledger row or a second link.

### Transactions

Each file is processed in one `BEGIN IMMEDIATE` transaction. The transaction covers:

- the `ingest_files` row;
- accepted leads and encrypted PII;
- rejections;
- events and links;
- file/drop/global statistics.

If any step fails, everything for that file rolls back and the file can be retried. Nested
transactions are rejected deliberately; the design uses one transaction per file rather than
savepoints.

### Statistics

Cumulative counters are written only when a file is first processed. Current-state counters
(`orphan_events`, `unknown_disposition_events`) are replaced after resolution and are safe to
re-run. `data_quality.json` uses the global current snapshots, as required.

### Verified equivalence

The implementation was verified in two orders:

- `drop_1`, then `drop_2`, including a repeat of `drop_1` between them;
- a clean database ingesting `drop_1 drop_2` in one command.

Snapshots of table counts, leads, events, links, rejections, ingest files, stats, orphan IDs,
and report hashes were identical. PII ciphertext was excluded from the cross-database
comparison because AES-GCM uses random nonces; decrypted values and HMAC indexes matched.

## Reports

`report` re-resolves linkable orphans, refreshes quality snapshots, rebuilds all facts, and
writes exactly five files atomically with `os.replace`:

| File | Sort order |
| --- | --- |
| `batch_summary.csv` | `workflow_id, batch_id` |
| `daily_overview.csv` | `workflow_id, date` |
| `lead_report.csv` | `workflow_id, external_ref`, then lead id as a deterministic tie-break |
| `rejections.csv` | `workflow_id, batch_id, row_number`, then file/id tie-breakers |
| `data_quality.json` | fixed key order from the contract |

### Business rules

- **Counted call:** a linked `call.initiated` event, keyed by `(workflow_id, call_id)`. If
  duplicate initiated events exist, the earliest `occurred_at` wins with an `event_id`
  tie-break; the call counts once.
- **Connected call:** a linked `call.connected` event for the same call and lead. A connected
  event without an initiated event does not create a call.
- **Effective disposition:** the latest valid `disposition.override` by `occurred_at`, else
  the latest valid `disposition.set`. Unknown codes are ignored and cannot erase a known
  disposition. Overrides never edit the ledger.
- **Bucket priority:** `yet_to_reach_out` → `completed` (any terminal disposition) →
  `in_progress_telephony_failed` (all calls failed) → `in_progress_dialed`.
- **`current_disposition`:** effective disposition of the most recent call by initiated time
  that has one; a later call without a disposition does not erase it.
- **`ever_success`:** any call's effective disposition has `is_success=true`.
- **Daily metrics:** grouped by the IST date of `call.initiated`, never the disposition date.
  `success_leads` counts distinct leads with a success call on that date.

### Formatting

- Exact headers and column counts; no extra columns.
- Booleans are lowercase `true`/`false`.
- `connectivity_rate` is `f"{connected / total:.4f}"`, including `1.0000` and `0.0000`.
- `last_call_at` is IST ISO-8601 with `+05:30`, whole seconds, no `Z`, empty when no calls.
- `phone_masked` is `+91******` plus the last four digits.
- `rejections.csv` contains no PII.
- `rejections_by_reason` contains only observed reasons, sorted lexicographically; `{}` when
  none.
- A batch with zero accepted leads is omitted from `batch_summary.csv`.
- Empty reports still write headers; `data_quality.json` is written with zeros.

## Decisions and assumptions

| Decision | Why | Alternative considered |
| --- | --- | --- |
| Use SQLite | Allowed by the brief; zero-ops, easy to inspect, sufficient for the data volume | PostgreSQL from day one |
| Keep reports as full-ledger projections | Late leads, late overrides, and re-runs converge without incremental-state bugs | Running counters / incremental facts |
| Identity is `(workflow_id, phone_hmac)` | Phone is the natural cross-batch identity; workflow scopes the same phone to separate campaigns | `(workflow, external_ref)` or email |
| Encrypt PII with AES-256-GCM | Authenticated encryption with per-field AAD and random nonces | Fernet, deterministic encryption |
| Use HMAC-SHA256 for phone matching | Deterministic equality without storing plaintext; keyed, unlike a plain hash | Plain SHA-256, plaintext index |
| Store canonical phone encrypted | Needed to produce the masked last four at report time | Store only HMAC and drop the mask |
| Keep `events` append-only | Auditability and safe replay; linkage is the only mutable state | Mutable `lead_id` on events |
| Link overrides only by `call_id` | The brief says `call_id` is authoritative; a phone on an override is not used | Phone fallback for overrides |
| Unknown workflow → `NULL` + orphan | Preserves history without violating the FK or guessing a campaign | Reject the event |
| Unparseable timestamp → `NULL` | The event is still useful for linkage; a bad clock should not discard it | Treat the event as malformed |
| Unknown disposition is ignored, not erasing a known one | Unknown codes are counted but must not corrupt business facts | Treat unknown as a reset |
| Equal `occurred_at` → higher lexical `event_id` | Deterministic tie-break with no extra clock precision | Insertion order / random |
| Duplicate initiated events → earliest wins | Matches “the call exists once” and is deterministic | Latest wins |
| Defaults after merging | A default should fill a gap, not overwrite an explicit value from an earlier row | Apply defaults per row before merge |
| First batch id wins | The brief says the first valid row creates the lead and its file stem is the batch | Latest batch id |
| Zero-lead batches omitted | A batch summary row with all zeroes would be noise; the brief does not require it | Emit zero rows |
| `rejections_by_reason` observed only | The contract shows reasons as data, not a fixed catalogue | Emit every possible reason with zero |
| Changed file at the same path is an error | Protects history from silent reinterpretation | Overwrite / append |
| Blank rows skipped | They are not data rows; counting them would distort row numbers | Count them |
| Only `*.csv` / `*.jsonl` directly under expected folders | Matches the documented drop layout; avoids ingesting unrelated files | Recursive discovery |
| Report decrypts phone at runtime | Avoids storing a plaintext last-four column | Store last four separately |
| `last_call_at` uses initiated time | The brief defines it as the latest `call.initiated` | Use connected/ended time |

## Failure modes considered

### Handled

- **Bad JSON / bad CSV** — malformed lines are counted and skipped; malformed CSV raises an
  `IngestError` for that file and rolls it back.
- **Duplicate files** — identical SHA-256 at the same path is a no-op.
- **Changed file at the same path** — explicit `IngestError`; history is never silently
  rewritten.
- **Duplicate events** — counted and skipped before linking; unique constraint is the final
  guard.
- **Late leads / late overrides** — orphan re-resolution after every drop and before reports.
- **Unknown dispositions** — counted, ignored by facts, and unable to erase a known
  disposition.
- **Invalid leads** — one reason per row, no PII in the rejection.
- **Invalid PII key / missing key** — `ConfigError` before any data is written.
- **Corrupt or moved ciphertext** — AES-GCM authentication fails with a generic error.
- **Crash during a file** — transaction rollback; retry is safe.
- **Crash during report writing** — temp file + `os.replace`; no half-written report.
- **Concurrent report readers** — report writes replace whole files atomically.
- **SQLite lock contention** — `busy_timeout=5000` and short per-file transactions.
- **Foreign-key violations** — enforced on every connection.
- **PII in event payloads** — keys matching PII names/aliases are removed recursively.
- **PII in rejections/logs** — rejections store only reasons; errors avoid values.

### Not handled / known limitations

- **Schema evolution beyond v1** — only one migration exists. A production system needs
  forward migrations, backfills, and compatibility tests.
- **Key rotation** — ciphertext has no key id/version beyond the `v1:` format; rotating keys
  requires a re-encryption job. HMAC key rotation would change all blind indexes.
- **PII erasure / retention** — no delete/export workflow or crypto-shredding policy.
- **Multi-tenancy** — one database is assumed; no tenant isolation or per-tenant keys.
- **Exactly-once network delivery** — idempotency is at file and event-id level, not a
  distributed transaction with an upstream queue.
- **Very large files** — lead and event files are read fully into memory. A 100k-lead single
  file would need streaming/batched inserts.
- **Concurrent writers** — SQLite serialises writes; multiple ingestion processes are not a
  supported production topology.
- **Incremental report performance** — every report rebuilds all facts. This is correct and
  simple but O(all events); at very large scale it needs incremental/materialized facts.
- **DST / non-IST time zones** — the brief fixes IST; no other zone handling is implemented.
- **Telephony integration** — this is a data pipeline; it does not call the telephony
  provider.
- **Assignment B** — not attempted; the provided `assignment-b-workflow/` files are
  untouched.
- **100k leads/day load test** — the design is documented below, but no production-scale
  benchmark was run in this submission.

## Testing

### Test suite

```text
.\.venv\Scripts\python.exe -m pytest solution\tests
238 passed
```

Coverage by area:

| Area | What is tested |
| --- | --- |
| Migrations | versioned init, idempotent reruns, required tables/indexes, FK enforcement, HMAC format check |
| Crypto | phone formats, masking, HMAC determinism/key separation, AES-GCM round trip/randomisation/tamper detection, key loading/generation |
| Ledger | append-only triggers, duplicate ids, source/workflow FKs, orphan view, late-lead resolution, re-linking, cross-workflow rejection |
| PII | ciphertext-only storage, no plaintext in a full SQL dump, blind-index lookup, recursive/alias PII rejection, safe errors |
| Transactions | commit, rollback, whole-file rollback, atomic lead+event writes, active-transaction guard |
| Normalisation | headers, all phone formats, four date formats, impossible dates, currency/decimal rounding, enums |
| Lead ingestion | aliases, unknown columns, blank rows, row numbers, schema-order reasons, warnings/defaults, merges, custom schemas, idempotency, changed files, stats, BOM/empty/invalid files |
| Event ingestion | all six event types, malformed lines, duplicate ids, orphan persistence, call-id override linking, unknown dispositions, UTC/offset/naive timestamps, phone normalisation, payload PII removal, idempotency, stats |
| Reports | effective dispositions, override precedence, equal-timestamp tie-break, counted/connected calls, bucket priority, IST daily dates, distinct success leads, connectivity formatting, current disposition, ever-success, masking, sorting/headers/booleans/empty values, late-lead self-healing |

### End-to-end verification

A standalone verifier was written outside the package. It deliberately does **not** import
`au_ledger`; it re-parses the raw drop files and recomputes expected facts from SQLite.

It checks:

- raw lead validation, canonical phones, merge results, rejection reasons, warnings;
- raw event classification counts and inserted event counts;
- raw timestamp conversion (`Z`, offset, naive IST) against stored UTC/IST values;
- event linking and the final orphan set from raw events plus final leads;
- effective dispositions and report facts;
- exact report rows, headers, sorting, formatting, and empty-value behavior;
- PII scans over logical SQLite text, decrypted values, raw SQLite bytes, and
  `rejections.csv`;
- snapshot equality for re-ingest and one-shot-vs-sequential.

Two fresh seeds were used:

| Check | Seed 2026 | Seed 456 |
| --- | --- | --- |
| `drop_1` leads created | 1,426 | 1,388 |
| `drop_1` rejects / merges | 67 / 32 | 89 / 22 |
| `drop_1` events inserted | 4,176 | 4,111 |
| Repeat `drop_1` | snapshots identical | snapshots identical |
| `drop_2` leads created | 486 | 494 |
| `drop_2` rejects / merges | 21 / 38 | 29 / 26 |
| `drop_2` events inserted | 4,572 | 4,530 |
| Original batch IDs | unchanged for 1,426 leads | unchanged for 1,388 leads |
| Orphans after drop_1 → final | 1,177 → 422 | 1,235 → 602 |
| Final leads / events / links | 1,912 / 8,748 / 8,326 | 1,882 / 8,641 / 8,039 |
| Final unknown dispositions | 21 | 17 |
| Final merges / rejections | 70 / 88 | 48 / 118 |
| Report rows (batch/daily) | 6 / 34 | 6 / 32 |
| Bucket totals | 777 completed, 588 dialed, 488 yet, 59 failed | 775 completed, 564 dialed, 482 yet, 61 failed |
| Effective overrides | 73, all change outcome | 74, all change outcome |
| Calls whose IST date differs from UTC date | 301 | 279 |
| Independent verifier | ALL CHECKS PASSED | ALL CHECKS PASSED |
| Sequential vs one-shot | snapshots identical | snapshots identical |

### Timing (local Windows, small seed 2026)

```text
ingest drop_1 + drop_2 (1,912 leads, 8,748 events): ~1.8 s
report (5 files):                                  ~0.5 s
pytest suite:                                      ~2.0 s
```

These are local smoke-test timings, not a production benchmark.

### What was not tested

- A real 100,000-leads/day load or burst test.
- Multiple concurrent ingestion processes against one SQLite file.
- Key rotation, re-encryption, or PII erasure.
- Disaster recovery / backup restore.
- A security audit or penetration test.
- A non-Windows CI matrix (the code is pure Python, but only Windows was exercised here).
- Real telephony provider integration.
- Assignment B.

## Production deployment for 100,000 leads per day

The current SQLite design is appropriate for the take-home and for small/medium batches. A
production deployment at 100k leads/day should keep the same logical model but move the
storage and orchestration to managed, concurrent infrastructure.

### Capacity estimate

- 100,000 leads/day ≈ 1.16 leads/s average.
- Assume 3–5 events per lead: 300,000–500,000 events/day ≈ 3.5–5.8 events/s average.
- Bursts are the real constraint; plan for 10–20× average (20–40 leads/s) during uploads.
- Lead row + attributes + PII ciphertext: roughly 1–2 KB per lead → 100–200 MB/day before
  indexes. Events are smaller but more numerous.
- A single PostgreSQL primary with proper indexes and batched writes comfortably handles this
  volume; the bottleneck is usually report aggregation and orphan resolution, not raw inserts.

### Recommended production architecture

```text
upstream CRM / telephony
        │
        ▼
object storage (S3/GCS)  ──►  manifest / queue (SQS/Kafka)
        │                              │
        │                              ▼
        │                     ingestion workers (containers)
        │                     ├─ schema validation
        │                     ├─ normalisation
        │                     ├─ PII encryption (KMS)
        │                     └─ idempotent DB writes
        │                              │
        ▼                              ▼
raw-file archive             PostgreSQL primary
                                      │
                                      ├─ read replica for reports
                                      ├─ partitioned events table
                                      └─ audit / metrics store
```

### Storage

- Use PostgreSQL 16+ (managed: RDS/Aurora/Cloud SQL) instead of SQLite.
- Keep the same tables and constraints. Port `TEXT` timestamps to `timestamptz` and store
  `occurred_date_ist` as a generated/derived date or compute it in queries.
- Partition `events` by month (or by `workflow_id` hash) once it grows; keep local indexes on
  each partition for `(workflow_id, phone_hmac)`, `(workflow_id, call_id)`, `event_type`, and
  `occurred_at_utc`.
- Keep `lead_pii` separate from `leads` so access to PII can be granted independently.
- Use `ON CONFLICT` for idempotent inserts and a unique `(drop_key, relative_path)` file
  ledger.
- Store raw files in object storage with lifecycle policies; the database stores only the
  content hash, path, and provenance.

### Ingestion

- One worker per file or per bounded batch; use a queue with visibility timeout and retries.
- Use `COPY` / `execute_values` for bulk inserts; commit per file or per 5k–10k rows.
- Use an advisory lock per `(drop_key, relative_path)` to prevent two workers processing the
  same file.
- Keep the same classification order and idempotency rules.
- Stream large files instead of reading them fully into memory; process in batches and keep
  the file-level transaction boundary or use a staging table plus a final atomic promote.
- Emit metrics for rows in, accepted, rejected, warnings, malformed, duplicates, inserted,
  orphans, unknown dispositions, and processing latency.
- Send poison files to a dead-letter queue with the failure reason; never retry forever.

### Orphan resolution at scale

- Maintain an index/queue of unresolved events rather than scanning the whole `orphan_events`
  view after every drop.
- On lead arrival, look up unresolved events by `(workflow_id, phone_hmac)` and link them.
- On call event arrival, look up unresolved overrides by `(workflow_id, call_id)` and link
  them.
- Run the same “passes until no progress” logic for override chains, but only over the
  affected call IDs.
- Track orphan age and alert when the unresolved count or age exceeds a threshold.

### PII and key management

- Use a KMS/HSM-backed envelope scheme: a master key wraps per-tenant/per-environment data
  keys; ciphertext records a key id/version.
- Keep the HMAC key in KMS/HSM; never expose it to application logs or analysts.
- Rotate encryption keys with a background re-encryption job; rotate HMAC keys only with a
  planned blind-index rebuild.
- Restrict PII decryption to the report service and a break-glass role; audit every access.
- Apply retention and deletion policies; support crypto-shredding by deleting the data key.
- Keep PII out of logs, traces, error trackers, and analytics events.
- For India, align with the DPDP Act 2023: purpose limitation, consent records, data
  minimisation, breach notification, and data residency requirements.

### Reports

- Run reports on a read replica to avoid impacting ingestion.
- For the exact contract, rebuild facts for the required reporting window from the ledger.
- At 100k/day, a full rebuild may still be acceptable for a nightly batch, but add:
  - materialized call/lead fact tables refreshed incrementally from new/changed links;
  - date/workflow partitions so a daily report scans only the relevant window;
  - cached report artifacts in object storage with a content hash and generation timestamp.
- Keep atomic publication: write to a temp object/key, then promote.
- Add reconciliation checks: report totals vs ledger counts, orphan/unknown snapshots, and
  per-batch bucket sums.

### Operations and reliability

- Containerize the worker and report job; deploy with infrastructure-as-code.
- Use managed PostgreSQL backups, point-in-time recovery, and tested restore drills.
- Set SLOs: ingestion freshness, report completion time, error rate, orphan age.
- Alert on queue depth, worker failures, DB replication lag, lock waits, disk usage, and
  report failures.
- Use structured logs with correlation IDs (drop, file, workflow) but no PII.
- Run a canary/blue-green deployment for schema or report changes.
- Load-test with synthetic data at 10× the expected peak before cutover.

### Migration from the current implementation

1. Add a PostgreSQL adapter behind the repository interface; keep the SQLite adapter for
   local tests.
2. Port migrations and add a schema-compatibility test suite.
3. Dual-write or replay a bounded historical window into PostgreSQL.
4. Compare reports from SQLite and PostgreSQL for the same drops.
5. Cut over ingestion, then reports; keep SQLite as a read-only fallback for one cycle.

### Cost/operability trade-off

For a single-tenant 100k/day workload, a small managed PostgreSQL instance plus 2–4 worker
containers is likely cheaper and simpler than a distributed warehouse. The main engineering
investment is idempotent ingestion, PII key management, and incremental reporting—not
horizontal scale.

## If I had another day

1. Add a PostgreSQL adapter and run the same 238-test suite against it.
2. Stream large CSV/JSONL files and batch inserts; benchmark 100k leads/day and 10× bursts.
3. Add key rotation with versioned ciphertext and a re-encryption job.
4. Replace full report rebuilds with incremental/materialized facts while keeping the exact
   contract and a reconciliation check.
5. Add an unresolved-event queue/index so orphan resolution is O(affected events), not
   O(all orphans).
6. Add property-based/fuzz tests for phone/date/decimal parsing and report invariants.
7. Add a PII retention/erasure workflow and access audit.
8. Add CI on Linux and Windows, plus a container image and a load-test harness.
