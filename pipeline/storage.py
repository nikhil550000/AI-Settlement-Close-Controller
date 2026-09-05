"""SQLite storage for the synthetic ledger.

Raw `sqlite3`, no ORM. The `UNIQUE(case_id, resolution_id, account_code)`
constraint makes idempotent controller adjustments a
database constraint rather than an application-level check — the reason
SQLite was chosen here at all.

**Why the third column is there.** A constraint on `(case_id, resolution_id)`
alone is a *row*-level constraint on a table that has one row per *leg*,
and every template posts two or three legs sharing
one resolution — so the first leg of a correcting entry inserted and the
rest were rejected, leaving an unbalanced fragment and putting
`AUTO_CLOSED` out of reach for every case assigned to it. The pair
still identifies the correction; `account_code` separates that
correction's own legs and nothing else. No template posts the same
account twice within one entry, so the three columns are unique per leg
by construction, and a second run of the same batch re-mints identical
triples and is rejected leg-for-leg.

Note on NULLs: SQLite treats each NULL as distinct for UNIQUE purposes,
so `manual`/`erp_import` entries (which always carry `case_id = NULL,
resolution_id = NULL` per `LedgerEntry`'s validator) never collide with
each other under this constraint. It only ever bites on a real
`(case_id, resolution_id, account_code)` triple, which is exactly the
idempotency invariant it exists to enforce.
"""

from __future__ import annotations

import datetime
import sqlite3
from collections.abc import Iterable

from pipeline.schemas import LedgerEntry, LedgerSource

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
    UNIQUE (case_id, resolution_id, account_code)
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """Open (creating if needed) the synthetic ledger database and ensure the schema exists."""
    conn = sqlite3.connect(db_path)
    conn.executescript(DDL)
    return conn


_INSERT = """
INSERT INTO ledger_entry (
    journal_entry_id, date, account_code, account_name,
    debit, credit, reference, narration, source,
    resolution_id, case_id
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_COLUMNS = (
    "journal_entry_id, date, account_code, account_name, "
    "debit, credit, reference, narration, source, resolution_id, case_id"
)


def _row(entry: LedgerEntry) -> tuple[object, ...]:
    return (
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
    )


def insert_ledger_entry(conn: sqlite3.Connection, entry: LedgerEntry, *, commit: bool = True) -> None:
    """Insert one journal entry. Raises `sqlite3.IntegrityError` on a duplicate
    `journal_entry_id` or a duplicate `(case_id, resolution_id, account_code)` triple.

    `commit=False` leaves the row inside the caller's open transaction —
    what `pipeline.apply` needs to write a correcting entry, re-reconcile
    against it, and then keep or discard the whole entry as one unit.
    """
    conn.execute(_INSERT, _row(entry))
    if commit:
        conn.commit()


def insert_ledger_entries(
    conn: sqlite3.Connection, entries: Iterable[LedgerEntry], *, commit: bool = True
) -> None:
    """Insert many journal entries under the same transaction and constraint rules."""
    conn.executemany(_INSERT, [_row(entry) for entry in entries])
    if commit:
        conn.commit()


def fetch_ledger_entries(conn: sqlite3.Connection) -> list[LedgerEntry]:
    """Read the whole ledger back as `LedgerEntry` records, in insertion order.

    The synthetic merchant ledger with applied `AUTO_CLOSED` adjustments
    is this, after a run.
    """
    rows = conn.execute(f"SELECT {_COLUMNS} FROM ledger_entry ORDER BY rowid").fetchall()
    return [
        LedgerEntry(
            journal_entry_id=row[0],
            date=datetime.date.fromisoformat(row[1]),
            account_code=row[2],
            account_name=row[3],
            debit=row[4],
            credit=row[5],
            reference=row[6],
            narration=row[7],
            source=LedgerSource(row[8]),
            resolution_id=row[9],
            case_id=row[10],
        )
        for row in rows
    ]
