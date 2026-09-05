"""Round-trip tests for the four the canonical schemas canonical schemas.

Checkpoint: each schema round-trips — construct, serialize
to JSON, parse back — and the result equals the original instance.
"""

import datetime

import pytest

from pipeline.money import Paise
from pipeline.schemas import (
    BankLine,
    BankProfile,
    LedgerEntry,
    LedgerSource,
    RazorpayEntityType,
    ReconLine,
    Settlement,
    SettlementStatus,
)


def _round_trip(model):
    cls = type(model)
    return cls.model_validate_json(model.model_dump_json())


def test_recon_line_round_trips():
    line = ReconLine(
        entity_id="pay_ABC123",
        type=RazorpayEntityType.PAYMENT,
        debit=Paise(0),
        credit=Paise(0),
        amount=Paise(100000),
        fee=Paise(2000),
        tax=Paise(360),
        on_hold=False,
        settled=True,
        created_at=1_735_000_000,
        settled_at=1_735_100_000,
        settlement_id="setl_XYZ789",
        settlement_utr="UTR123456789",
        payment_id=None,
        order_id="order_ORD1",
        posted_at=None,
        credit_type="default",
        dispute_id=None,
        description="payment for order",
        method="upi",
    )
    assert _round_trip(line) == line


def test_settlement_round_trips():
    settlement = Settlement(
        id="setl_XYZ789",
        amount=Paise(97640),
        status=SettlementStatus.PROCESSED,
        fees=Paise(2000),
        tax=Paise(360),
        utr="UTR123456789",
        created_at=1_735_100_000,
    )
    assert _round_trip(settlement) == settlement


def test_bank_line_round_trips():
    line = BankLine(
        line_id="bl_0001",
        value_date=datetime.date(2026, 8, 20),
        narration="NEFT CR UTR123456789 RAZORPAY",
        bank_ref_no="REF00001",
        withdrawal_paise=Paise(0),
        deposit_paise=Paise(97640),
        closing_balance_paise=Paise(5_000_000),
        bank_profile=BankProfile.HDFC,
    )
    assert _round_trip(line) == line


def test_ledger_entry_round_trips_manual():
    entry = LedgerEntry(
        journal_entry_id="je_0001",
        date=datetime.date(2026, 8, 20),
        account_code="1020",
        account_name="Razorpay Clearing",
        debit=Paise(100000),
        credit=Paise(0),
        reference="order_ORD1",
        narration="sale recognized",
        source=LedgerSource.ERP_IMPORT,
    )
    assert _round_trip(entry) == entry


def test_ledger_entry_round_trips_controller_adjustment():
    entry = LedgerEntry(
        journal_entry_id="je_0002",
        date=datetime.date(2026, 8, 20),
        account_code="5010",
        account_name="Payment Gateway Charges",
        debit=Paise(2000),
        credit=Paise(0),
        reference="pay_ABC123",
        narration="unposted MDR fee",
        source=LedgerSource.CONTROLLER_ADJUSTMENT,
        resolution_id="res_0001",
        case_id="case_0001",
    )
    assert _round_trip(entry) == entry


def test_ledger_entry_rejects_resolution_fields_without_controller_adjustment():
    with pytest.raises(ValueError):
        LedgerEntry(
            journal_entry_id="je_0003",
            date=datetime.date(2026, 8, 20),
            account_code="1020",
            account_name="Razorpay Clearing",
            debit=Paise(0),
            credit=Paise(100000),
            reference="order_ORD1",
            narration="sale recognized",
            source=LedgerSource.MANUAL,
            resolution_id="res_0001",
            case_id="case_0001",
        )


def test_ledger_entry_rejects_controller_adjustment_without_resolution_fields():
    with pytest.raises(ValueError):
        LedgerEntry(
            journal_entry_id="je_0004",
            date=datetime.date(2026, 8, 20),
            account_code="1020",
            account_name="Razorpay Clearing",
            debit=Paise(0),
            credit=Paise(100000),
            reference="order_ORD1",
            narration="controller correction",
            source=LedgerSource.CONTROLLER_ADJUSTMENT,
        )
