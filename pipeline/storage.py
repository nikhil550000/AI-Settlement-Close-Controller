"""SQLite storage for the synthetic ledger, per spec.md §4.5.

Raw `sqlite3`, no ORM. The `UNIQUE(case_id, resolution_id)` constraint
makes invariant 1.7.4 (idempotent controller adjustments) a database
constraint rather than an application-level check — the reason §4.5
gives for choosing SQLite here at all.

Note on NULLs: SQLite treats each NULL as distinct for UNIQUE purposes,
so `manual`/`erp_import` entries (which always carry `case_id = NULL,
resolution_id = NULL` per `LedgerEntry`'s validator) never collide with
each other under this constraint. It only ever bites on a real
`(case_id, resolution_id)` pair, which is exactly the idempotency
invariant it exists to enforce.
"""

from __future__ import annotations

import sqlite3

from pipeline.schemas import LedgerEntry

DDL = """
CREATE TABLE IF NOT EXISTS ledger_entry (
    journal_entry_id TEXT PRIMARY KEY,
    date             TEXT NOT NULL,
    account_code     TEXT NOT NULL,
    account_name     TEXT NOT NULL,
    debit            INTEGER NOT NULL,
    credit           INTEGER NOT NULL,
    reference        TEXT NOT NULL,
    narration        TEXT NOT NULL,
    source           TEXT NOT NULL,
    resolution_id    TEXT,
    case_id          TEXT,
    UNIQUE (case_id, resolution_id)
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """Open (creating if needed) the synthetic ledger database and ensure the schema exists."""
    conn = sqlite3.connect(db_path)
    conn.executescript(DDL)
    return conn


def insert_ledger_entry(conn: sqlite3.Connection, entry: LedgerEntry) -> None:
    """Insert one journal entry. Raises `sqlite3.IntegrityError` on a
    duplicate `journal_entry_id` or a duplicate `(case_id, resolution_id)` pair.
    """
    conn.execute(
        """
        INSERT INTO ledger_entry (
            journal_entry_id, date, account_code, account_name,
            debit, credit, reference, narration, source,
            resolution_id, case_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entry.journal_entry_id,
            entry.date.isoformat(),
            entry.account_code,
            entry.account_name,
            int(entry.debit),
            int(entry.credit),
            entry.reference,
            entry.narration,
            entry.source.value,
            entry.resolution_id,
            entry.case_id,
        ),
    )
    conn.commit()
