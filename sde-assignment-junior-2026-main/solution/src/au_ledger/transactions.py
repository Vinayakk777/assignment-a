"""Transaction helpers.

SQLite is opened in autocommit mode (``isolation_level=None``) so the
application controls transaction boundaries explicitly. Ingestion code must
wrap all writes for one input file in :func:`transaction`; a crash or exception
then rolls the whole file back and the file ledger remains retryable.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .errors import TransactionError


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a write transaction using ``BEGIN IMMEDIATE``.

    Nested transactions are rejected deliberately: the ingestion design uses
    one transaction per input file and does not need savepoints.
    """
    if conn.in_transaction:
        raise TransactionError("nested transactions are not supported")
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:  # pragma: no cover - defensive
        raise TransactionError("could not start transaction") from exc
    try:
        yield conn
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error as rollback_exc:  # pragma: no cover - defensive
            raise TransactionError("transaction rollback failed") from rollback_exc
        raise
    else:
        try:
            conn.execute("COMMIT")
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            try:
                conn.execute("ROLLBACK")
            finally:
                raise TransactionError("transaction commit failed") from exc
