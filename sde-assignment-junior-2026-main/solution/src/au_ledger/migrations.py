"""Versioned SQLite schema migrations.

Each entry is ``(version, name, sql)``. The migration runner records applied
versions in ``schema_migrations``. Statements use ``IF NOT EXISTS`` and the
runner is retry-safe: re-running initialization is a no-op.
"""

from __future__ import annotations

Migration = tuple[int, str, str]

INITIAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflows (
    workflow_id   TEXT PRIMARY KEY,
    description   TEXT NOT NULL DEFAULT '',
    schema_hash   TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_fields (
    workflow_id       TEXT NOT NULL REFERENCES workflows(workflow_id),
    ordinal           INTEGER NOT NULL CHECK (ordinal >= 1),
    name              TEXT NOT NULL,
    field_type        TEXT NOT NULL CHECK (field_type IN ('string','phone','date','decimal','enum')),
    required          INTEGER NOT NULL CHECK (required IN (0,1)),
    pii               INTEGER NOT NULL CHECK (pii IN (0,1)),
    is_reference      INTEGER NOT NULL CHECK (is_reference IN (0,1)),
    default_value     TEXT,
    enum_values_json  TEXT,
    aliases_json      TEXT NOT NULL,
    PRIMARY KEY (workflow_id, name),
    UNIQUE (workflow_id, ordinal)
);

CREATE TABLE IF NOT EXISTS dispositions (
    workflow_id  TEXT NOT NULL REFERENCES workflows(workflow_id),
    code         TEXT NOT NULL,
    label        TEXT NOT NULL,
    is_terminal  INTEGER NOT NULL CHECK (is_terminal IN (0,1)),
    is_success   INTEGER NOT NULL CHECK (is_success IN (0,1)),
    PRIMARY KEY (workflow_id, code)
);

CREATE TABLE IF NOT EXISTS ingest_files (
    id              INTEGER PRIMARY KEY,
    drop_key        TEXT NOT NULL,
    drop_name       TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('leads','events')),
    relative_path   TEXT NOT NULL,
    content_sha256  TEXT NOT NULL,
    workflow_id     TEXT REFERENCES workflows(workflow_id),
    batch_id        TEXT,
    nonempty_lines  INTEGER NOT NULL DEFAULT 0 CHECK (nonempty_lines >= 0),
    processed_at    TEXT NOT NULL,
    UNIQUE (drop_key, relative_path)
);

CREATE TABLE IF NOT EXISTS leads (
    id               INTEGER PRIMARY KEY,
    workflow_id      TEXT NOT NULL REFERENCES workflows(workflow_id),
    phone_hmac       TEXT NOT NULL CHECK (
        length(phone_hmac) = 64 AND phone_hmac NOT GLOB '*[^0-9a-f]*'
    ),
    external_ref     TEXT NOT NULL,
    batch_id         TEXT NOT NULL,
    attributes_json  TEXT NOT NULL DEFAULT '{}',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (workflow_id, phone_hmac)
);

CREATE TABLE IF NOT EXISTS lead_pii (
    lead_id     INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    field_name  TEXT NOT NULL CHECK (length(field_name) > 0),
    ciphertext  TEXT NOT NULL CHECK (length(ciphertext) > 0),
    PRIMARY KEY (lead_id, field_name)
);

CREATE TABLE IF NOT EXISTS events (
    id                 INTEGER PRIMARY KEY,
    event_id           TEXT NOT NULL UNIQUE CHECK (length(event_id) > 0),
    workflow_id        TEXT REFERENCES workflows(workflow_id),
    call_id            TEXT,
    event_type         TEXT,
    occurred_at_utc    TEXT,
    occurred_at_ist    TEXT,
    occurred_date_ist  TEXT,
    phone_hmac         TEXT CHECK (
        phone_hmac IS NULL OR (
            length(phone_hmac) = 64 AND phone_hmac NOT GLOB '*[^0-9a-f]*'
        )
    ),
    payload_json       TEXT NOT NULL DEFAULT '{}',
    source_file_id     INTEGER NOT NULL REFERENCES ingest_files(id),
    line_number        INTEGER NOT NULL CHECK (line_number >= 1),
    ingested_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_links (
    event_id     TEXT PRIMARY KEY REFERENCES events(event_id) ON DELETE CASCADE,
    lead_id      INTEGER NOT NULL REFERENCES leads(id),
    link_method  TEXT NOT NULL CHECK (link_method IN ('phone','call')),
    resolved_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rejections (
    id              INTEGER PRIMARY KEY,
    ingest_file_id  INTEGER NOT NULL REFERENCES ingest_files(id) ON DELETE CASCADE,
    workflow_id     TEXT NOT NULL REFERENCES workflows(workflow_id),
    batch_id        TEXT NOT NULL,
    row_number      INTEGER NOT NULL CHECK (row_number >= 1),
    reason          TEXT NOT NULL CHECK (length(reason) > 0),
    created_at      TEXT NOT NULL,
    UNIQUE (ingest_file_id, row_number)
);

CREATE TABLE IF NOT EXISTS processing_stats (
    id           INTEGER PRIMARY KEY,
    scope_type   TEXT NOT NULL CHECK (scope_type IN ('global','drop','file')),
    scope_key    TEXT NOT NULL,
    stat_name    TEXT NOT NULL CHECK (length(stat_name) > 0),
    stat_value   INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL,
    UNIQUE (scope_type, scope_key, stat_name)
);

CREATE INDEX IF NOT EXISTS ix_leads_report
    ON leads(workflow_id, external_ref);
CREATE INDEX IF NOT EXISTS ix_leads_batch
    ON leads(workflow_id, batch_id);
CREATE INDEX IF NOT EXISTS ix_lead_pii_field
    ON lead_pii(field_name);

CREATE INDEX IF NOT EXISTS ix_events_call
    ON events(workflow_id, call_id);
CREATE INDEX IF NOT EXISTS ix_events_phone
    ON events(workflow_id, phone_hmac);
CREATE INDEX IF NOT EXISTS ix_events_time
    ON events(occurred_at_utc);
CREATE INDEX IF NOT EXISTS ix_events_type
    ON events(event_type);
CREATE INDEX IF NOT EXISTS ix_events_source
    ON events(source_file_id);

CREATE INDEX IF NOT EXISTS ix_event_links_lead
    ON event_links(lead_id);

CREATE INDEX IF NOT EXISTS ix_rejections_sort
    ON rejections(workflow_id, batch_id, row_number);
CREATE INDEX IF NOT EXISTS ix_rejections_reason
    ON rejections(reason);

CREATE INDEX IF NOT EXISTS ix_ingest_files_kind
    ON ingest_files(kind);

CREATE INDEX IF NOT EXISTS ix_processing_stats_lookup
    ON processing_stats(scope_type, scope_key);

CREATE VIEW IF NOT EXISTS orphan_events AS
SELECT e.*
FROM events AS e
LEFT JOIN event_links AS l ON l.event_id = e.event_id
WHERE l.event_id IS NULL;

-- The ledger is append-only. Linkage lives in event_links and may change as
-- late leads or overrides arrive; the event row itself must never change.
CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'event ledger is append-only: UPDATE is forbidden');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'event ledger is append-only: DELETE is forbidden');
END;
"""


MIGRATIONS: list[Migration] = [
    (1, "initial_schema", INITIAL_SCHEMA),
]
