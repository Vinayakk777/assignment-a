# AI Usage Disclosure

This disclosure describes the AI assistance that was actually used for this submission. It
does **not** claim any human code review, manual verification, or candidate-authored code that
did not occur in this session.

## Tools used

- **OpenCode coding agent** (model: DeepSeek V4.1 Flash Free) running in an interactive
  terminal session on Windows.
- **PowerShell** for commands, file inspection, and test runs.
- **Python 3.11**, **pytest**, **SQLite**, and the **cryptography** package for the
  implementation and verification.
- A **standalone Python verifier** written during this work. It deliberately does not import
  the `au_ledger` package; it re-parses the raw drop files and recomputes expected facts from
  SQLite.

## What the AI was used for

### Implementation

The AI generated the code in `solution/src/au_ledger/`, including:

- the versioned SQLite schema, indexes, append-only triggers, and orphan view;
- dynamic workflow-schema loading and validation;
- lead CSV discovery, header/alias mapping, validation, normalisation, rejection, duplicate
  merging, and statistics;
- PII handling: phone canonicalisation, HMAC-SHA256 blind index, AES-256-GCM encryption,
  key loading/generation, and masking;
- JSONL event discovery, malformed/duplicate/orphan/unknown classification, timestamp
  normalisation, payload PII removal, and statistics;
- phone-based and `call_id`-based event linking, orphan re-resolution, and quality snapshots;
- the report fact builder and the five exact output writers;
- the CLI, transaction helper, repository primitives, and project metadata.

### Tests

The AI wrote the pytest suite in `solution/tests/` and ran it repeatedly while iterating. The
final suite contains 238 tests covering migrations, crypto, the append-only ledger, PII
storage, transactions, normalisation, lead ingestion, event ingestion, and reports.

### Debugging

The AI used failing tests, tracebacks, SQL queries, and snapshot comparisons to find and fix
issues during implementation. In the final end-to-end verification, the AI found and fixed
two bugs in its own standalone verifier (not in the pipeline):

1. **Double-counted `events/` path** — the verifier built `drop_key || '/events/' ||
   relative_path` while `relative_path` already included `events/`. The end-to-end check
   reported `event file non-empty line counts differ from the raw files`; the path join was
   corrected.
2. **Popped phone field** — the verifier removed `phone` from a decrypted lead record and then
   tried to read it while building the expected `lead_report.csv` row. A `KeyError: 'phone'`
   exposed the bug; the record was changed to keep the phone value.

An earlier draft of the expected-lead-row code also contained an `if False` placeholder for
the phone mask; it was replaced with the real decrypted mask before the final runs.

### Documentation

The AI drafted `README.md`, `SUBMISSION.md`, `AI_USAGE.md`, and the detailed
`solution/README.md`, based on the code and the commands/results actually observed. The AI
also removed stale scratch files and local secrets from the working tree as part of the final
submission cleanup.

## What the candidate decided or owns

- The choice of **Assignment A (Data)** was the candidate's stated goal.
- The candidate is responsible for reviewing, understanding, and defending the submitted
  code, as required by the take-home instructions.
- No independent human code review, manual test execution, or production review is claimed in
  this document. The candidate should perform that review before submitting or interviewing.

The design decisions recorded in `SUBMISSION.md` are the decisions that were implemented. They
were made during the AI-assisted session and are documented so a reviewer can challenge them;
they are not presented as independently validated by a human.

## Where the AI was wrong or unhelpful

- **Standalone verifier path bug** — as described above, the verifier initially joined
  `drop_key`, `events/`, and `relative_path`, producing paths like
  `drop_1/events/events/events_0001.jsonl`. The E2E check caught it; the fix was to join
  `drop_key` and `relative_path` only.
- **Verifier record mutation** — the verifier popped the decrypted `phone` from a record and
  later needed it for the report mask. The `KeyError` was the signal; the fix was to keep the
  value.
- **Placeholder logic** — an early expected-report builder contained `if False` instead of a
  real phone mask. It was caught during review of the verifier output and replaced.
- **Scale claims** — the AI can describe a production design for 100k leads/day, but it did
  not run a production-scale load test. The submission states this limitation explicitly
  rather than presenting the design as benchmarked.

## How the AI output was verified

All of the following checks were **executed by the AI agent** in this session:

- `python -m pytest solution\tests` → **238 passed**.
- Fresh-seed end-to-end runs for seeds **2026** and **456**:
  - `init-db`, `ingest drop_1`, repeat `ingest drop_1`, `ingest drop_2`, `report`;
  - snapshot comparison before/after the repeat → identical;
  - batch-ID comparison → unchanged for all pre-existing leads;
  - orphan-progress check → orphans linked when `drop_2` arrived;
  - sequential ingestion vs clean one-shot ingestion → identical snapshots (PII ciphertext
    excluded because AES-GCM uses random nonces);
  - independent verifier → `ALL CHECKS PASSED` for both seeds.
- The independent verifier re-parsed raw files and checked:
  - lead validation, merging, rejection reasons, and warnings;
  - event classification counts and inserted event counts;
  - raw timestamp conversion (`Z`, offset, naive IST) against stored UTC/IST values;
  - event linking and the final orphan set;
  - effective dispositions and all report facts;
  - exact report headers, rows, sorting, formatting, and empty values;
  - PII scans over logical SQLite text, decrypted values, raw SQLite bytes, and
    `rejections.csv`.
- Seed-42 was also run earlier in the work and its report counts are recorded in
  `solution/README.md`.

These checks verify the implementation against the assignment contract, but they are not a
substitute for an independent human review or a production security/load assessment.

## What was not verified

- No human reviewer independently read or ran the code in this session.
- No production-scale load test at 100,000 leads/day.
- No concurrent-writer test against SQLite.
- No key-rotation, re-encryption, or PII-erasure test.
- No disaster-recovery/backup-restore test.
- No external security audit or penetration test.
- No non-Windows CI run (the code is pure Python, but only Windows was exercised here).
- No real telephony-provider integration.
- Assignment B was not attempted.

## Disclosure summary

AI was used for essentially all implementation, test authoring, debugging, and documentation
in this submission. The AI also executed the verification commands and reported their actual
output. No human review or manual verification is claimed. The candidate should review the
code, rerun the tests, and be prepared to explain and modify the implementation live.
