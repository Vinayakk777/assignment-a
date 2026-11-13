# Assignment A — Interaction Ledger & Reporting Pipeline

This repository contains the completed submission for **Agentic Universe Assignment A
(Data)**. The implementation lives in [`solution/`](solution/), the detailed design is in
[`solution/README.md`](solution/README.md), and the submission narrative is in
[`SUBMISSION.md`](SUBMISSION.md).

The pipeline ingests messy lead CSVs and JSONL call events into an append-only SQLite
ledger, protects PII with AES-256-GCM and an HMAC phone blind index, resolves late-arriving
orphan events, and writes the five exact report files required by the brief.

## Quick start (Windows PowerShell)

From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r solution\requirements.txt

$env:PYTHONPATH = "solution/src"
.\.venv\Scripts\python.exe -m au_ledger init-keys --out solution\.env
.\.venv\Scripts\python.exe -m au_ledger init-db `
  --db solution\ledger.sqlite --schemas assignment-a-data\schemas

# Generated data is intentionally not committed.
.\.venv\Scripts\python.exe assignment-a-data\tools\generate_data.py `
  --seed 42 --scale small --out data

.\.venv\Scripts\python.exe -m au_ledger ingest data\drop_1 data\drop_2 `
  --db solution\ledger.sqlite --schemas assignment-a-data\schemas --env solution\.env

.\.venv\Scripts\python.exe -m au_ledger report `
  --db solution\ledger.sqlite --env solution\.env --out reports

.\.venv\Scripts\python.exe -m pytest solution\tests
```

## Quick start (macOS / Linux)

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

`init-keys` writes `solution/.env`; it is gitignored and must never be committed. Use
`--force` only when intentionally rotating the local key pair. The generated `data/` and
`reports/` directories are also gitignored.

## What is implemented

- Dynamic workflow-schema loading from `assignment-a-data/schemas`
- Lead CSV validation, normalization, rejection reasons, field warnings, and duplicate merging
- Append-only event ledger with malformed/duplicate/orphan/unknown-disposition classification
- Phone-based and `call_id`-based event linking, with late-lead orphan resolution
- PII encryption (AES-256-GCM) and deterministic phone matching (HMAC-SHA256 blind index)
- UTC/IST timestamp normalization and IST report dates
- Per-file SHA-256 idempotency, transactions, and scoped statistics
- All five reports with the exact headers, sort order, formatting, and empty-value behavior
- 238 pytest tests plus fresh-seed end-to-end verification (see `SUBMISSION.md`)

## Documentation

| File | Purpose |
| --- | --- |
| [`solution/README.md`](solution/README.md) | Detailed implementation, schema, rules, and test coverage |
| [`SUBMISSION.md`](SUBMISSION.md) | Architecture, decisions, failure modes, testing, and production plan |
| [`AI_USAGE.md`](AI_USAGE.md) | Honest account of AI assistance and what was/was not independently verified |
| [`assignment-a-data/README.md`](assignment-a-data/README.md) | Original Assignment A brief and output contract |

## Verified status

- `238 passed` with `python -m pytest solution/tests`
- Full ingest/report/re-ingest/sequential-vs-one-shot checks passed on fresh seeds `2026` and `456`
- PII scans found no plaintext phone/name values in SQLite bytes, logical columns, or `rejections.csv`
- See `SUBMISSION.md` for the exact commands and results

## Repository layout

```text
assignment-a-data/       # provided schemas, dispositions, generator, and brief
assignment-b-workflow/   # provided Assignment B material (out of scope, unmodified)
data/                    # generated drops (gitignored)
docs/                    # provided glossary
solution/                # implementation, tests, and detailed README
  src/au_ledger/         # pipeline package
  tests/                 # pytest suite
SUBMISSION.md            # submission narrative
AI_USAGE.md              # AI usage disclosure
```

---

## Original assignment brief

# Agentic Universe: Junior SDE Take-Home (Data & Workflow)

Thanks for your interest in Agentic Universe. We build AI voice agents for insurers, lenders and
consumer brands across India and APAC. Our platform has three layers:

- **Data:** leads, call events, dispositions and the interaction ledger that every client report
  is built on.
- **Workflow:** campaigns, scheduling, dialling, retries and callbacks, all orchestrated durably.
- **Application:** dashboards, consoles and APIs.

This take-home focuses on **data** and **workflow**. **Pick one assignment.** Don't do both.

| | Assignment | You'll mostly be doing |
|---|---|---|
| **A** | [Interaction Ledger & Reporting Pipeline](assignment-a-data/README.md) | Messy-data ingestion, schema design, idempotent pipelines, exact metrics |
| **B** | [Durable Campaign Dispatcher](assignment-b-workflow/README.md) | State machines, idempotency, failure handling, scheduling under constraints |

Both are judged on the same bar. Pick the one that excites you more.

## Ground rules

- **Time box:** plan for about **6–8 focused hours**. You have **72 hours** from when you receive
  this. We know you have a life. If you run out of time, stop and tell us in `SUBMISSION.md`
  what you'd do next. A well-explained partial solution beats a rushed complete one.
- **Language:** Python 3.11+. Use any libraries you like; list them in `requirements.txt`.
- **AI tools** (Claude, Cursor, Copilot, ChatGPT...) are **allowed**, as they would be on the
  job. You must fill in [`AI_USAGE.md`](AI_USAGE.md) honestly. In the follow-up interview we'll
  ask you to walk through your code and change it live. Submit only code you understand and can
  defend.
- **Ambiguity is intentional in places.** When the spec doesn't decide something, make a
  reasonable call and write it down in `SUBMISSION.md`. Don't wait for an answer from us.
- **Synthetic data only.** Everything here is generated. No real customer data is involved.

## Getting started

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# then follow your chosen assignment's README
```

`docker-compose.yml` starts a local Postgres if you want one (`docker compose up -d`). SQLite
is equally acceptable.

## What to submit

1. Push your work to a **private** GitHub or GitLab repository. Start from this repo and keep
   its structure. Put your code in `solution/` (or a similar top-level folder) and keep the
   original files unmodified.
2. Your repo must include:
   - `SUBMISSION.md`: fill in the template provided
   - `AI_USAGE.md`: fill in the template provided
   - Exact setup and run commands that work from a clean clone
   - Tests, runnable with one command (for example `pytest`)
3. Give read access to **`[[REVIEWER_HANDLES]]`** and email the repo link to
   **`[[SUBMISSION_EMAIL]]`** before the deadline.

Please **don't** publish your solution publicly, and **don't** commit generated data, `.env`
files, or databases.

## What happens next

If your submission clears the bar, we'll schedule a **45-minute technical conversation**:

- 15 min: you walk us through your design and the decisions in `SUBMISSION.md`
- 20 min: we change a requirement and you extend your code live (AI tools allowed; screen
  share)
- 10 min: your questions for us

## What we look for

| Signal | What good looks like |
|---|---|
| Correctness | It produces the right answer on data and seeds you haven't seen |
| Robustness | It behaves sensibly when inputs are dirty, things fail, or it's run twice |
| Design | Clear data and state model; you can explain why it's safe |
| Code quality | Readable, organised, tested where it matters |
| Communication | Honest about trade-offs, assumptions and what's missing |
| Ownership | You made decisions instead of waiting to be told |

Good luck. We're looking forward to reading your work.
