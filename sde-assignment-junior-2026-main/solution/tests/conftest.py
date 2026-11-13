from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the src-layout package importable even when pytest is run from the repo
# root without an editable install.
SOLUTION_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOLUTION_ROOT / "src"))

from au_ledger.config import KeyConfig
from au_ledger.crypto import PhoneHasher, PiiCipher
from au_ledger.db import connect, migrate
from au_ledger.metadata import seed_metadata
from au_ledger.repository import create_ingest_file
from au_ledger.schema import load_workflow_schemas

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS_DIR = REPO_ROOT / "assignment-a-data" / "schemas"


@pytest.fixture
def keys() -> KeyConfig:
    return KeyConfig(enc_key=b"E" * 32, hmac_key=b"H" * 32)


@pytest.fixture
def cipher(keys: KeyConfig) -> PiiCipher:
    return PiiCipher(keys.enc_key)


@pytest.fixture
def hasher(keys: KeyConfig) -> PhoneHasher:
    return PhoneHasher(keys.hmac_key)


@pytest.fixture
def conn():
    connection = connect(":memory:")
    migrate(connection)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def seeded_conn(conn):
    seed_metadata(conn, SCHEMAS_DIR)
    return conn


@pytest.fixture
def schemas():
    return load_workflow_schemas(SCHEMAS_DIR)


def make_ingest_file(
    conn,
    *,
    kind: str = "events",
    workflow_id: str | None = None,
    batch_id: str | None = None,
    suffix: str = "",
) -> int:
    return create_ingest_file(
        conn,
        drop_key="drop_1",
        drop_name="drop_1",
        kind=kind,
        relative_path=f"{kind}/file{suffix}.{'csv' if kind == 'leads' else 'jsonl'}",
        content_sha256="a" * 64,
        workflow_id=workflow_id,
        batch_id=batch_id,
        nonempty_lines=1,
    )
