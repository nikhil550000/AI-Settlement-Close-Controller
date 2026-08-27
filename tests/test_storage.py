"""SQLite DDL tests: the ledger table exists and its
UNIQUE(case_id, resolution_id) constraint enforces invariant 1.7.4.

Checkpoint (session 1.2): the unique constraint rejects a duplicate
(case_id, resolution_id) insert.
"""

import datetime
import sqlite3

import pytest

from pipeline.money import Paise
from pipeline.schemas import LedgerEntry, LedgerSource
from pipeline.storage import connect, insert_ledger_entry


def _controller_entry(journal_entry_id: str, case_id: str, resolution_id: str) -> LedgerEntry:
    return LedgerEntry(
        journal_entry_id=journal_entry_id,
        date=datetime.date(2026, 8, 20),
        account_code="5010",
        account_name="Payment Gateway Charges",
        debit=Paise(2000),
        credit=Paise(0),
        reference="pay_ABC123",
        narration="unposted MDR fee",
        source=LedgerSource.CONTROLLER_ADJUSTMENT,
        resolution_id=resolution_id,
        case_id=case_id,
    )


def test_insert_and_read_back(tmp_path):
    conn = connect(str(tmp_path / "ledger.db"))
    entry = _controller_entry("je_0001", "case_0001", "res_0001")
    insert_ledger_entry(conn, entry)

    row = conn.execute(
        "SELECT journal_entry_id, case_id, resolution_id, debit, credit FROM ledger_entry"
    ).fetchone()
    assert row == ("je_0001", "case_0001", "res_0001", 2000, 0)


def test_duplicate_case_resolution_pair_rejected(tmp_path):
    conn = connect(str(tmp_path / "ledger.db"))
    insert_ledger_entry(conn, _controller_entry("je_0001", "case_0001", "res_0001"))

    duplicate = _controller_entry("je_0002", "case_0001", "res_0001")
    with pytest.raises(sqlite3.IntegrityError):
        insert_ledger_entry(conn, duplicate)


def test_distinct_manual_entries_with_null_case_and_resolution_do_not_collide(tmp_path):
    conn = connect(str(tmp_path / "ledger.db"))
    manual_one = LedgerEntry(
        journal_entry_id="je_manual_1",
        date=datetime.date(2026, 8, 20),
        account_code="1020",
        account_name="Razorpay Clearing",
        debit=Paise(100000),
        credit=Paise(0),
        reference="order_ORD1",
        narration="sale recognized",
        source=LedgerSource.ERP_IMPORT,
    )
    manual_two = LedgerEntry(
        journal_entry_id="je_manual_2",
        date=datetime.date(2026, 8, 21),
        account_code="1020",
        account_name="Razorpay Clearing",
        debit=Paise(50000),
        credit=Paise(0),
        reference="order_ORD2",
        narration="sale recognized",
        source=LedgerSource.ERP_IMPORT,
    )
    insert_ledger_entry(conn, manual_one)
    insert_ledger_entry(conn, manual_two)

    count = conn.execute("SELECT COUNT(*) FROM ledger_entry").fetchone()[0]
    assert count == 2
