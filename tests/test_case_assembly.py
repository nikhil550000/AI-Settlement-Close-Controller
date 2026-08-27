"""Session 3.2 checkpoint (spec.md §6.3): "150 cases assemble; orphan
granularity matches REV-18" — plus targeted unit coverage of each
classification rule `pipeline/case_assembly.py` uses to tell an orphan
case apart from bank-statement noise.
"""

from __future__ import annotations

import random
from datetime import date

from generator.cli import generate_reference_batch
from pipeline.case_assembly import CaseKind, assemble_cases, assemble_orphan_cases, assemble_settlement_anchored_cases
from pipeline.money import Paise
from pipeline.schemas import BankLine, BankProfile, RazorpayEntityType, ReconLine, Settlement, SettlementStatus

SNAPSHOT = date(2026, 8, 28)


def _line(
    line_id: str,
    *,
    narration: str,
    withdrawal: int = 0,
    deposit: int = 0,
    value_date: date = SNAPSHOT,
) -> BankLine:
    return BankLine(
        line_id=line_id,
        value_date=value_date,
        narration=narration,
        bank_ref_no=None,
        withdrawal_paise=Paise(withdrawal),
        deposit_paise=Paise(deposit),
        closing_balance_paise=Paise(10_000_00),
        bank_profile=BankProfile.HDFC,
    )


def _settlement(settlement_id: str, *, utr: str = "AXIS000000000001") -> Settlement:
    return Settlement(
        id=settlement_id,
        amount=Paise(1000_00),
        status=SettlementStatus.PROCESSED,
        fees=Paise(0),
        tax=Paise(0),
        utr=utr,
        created_at=1_700_000_000,
    )


def _recon_line(entity_id: str, *, settlement_id: str | None) -> ReconLine:
    return ReconLine(
        entity_id=entity_id,
        type=RazorpayEntityType.PAYMENT,
        debit=Paise(0),
        credit=Paise(1000_00),
        amount=Paise(1000_00),
        fee=Paise(0),
        tax=Paise(0),
        on_hold=False,
        settled=settlement_id is not None,
        created_at=1_700_000_000,
        settlement_id=settlement_id,
        settlement_utr=None,
        payment_id=None,
        order_id=None,
        posted_at=None,
        credit_type="default",
        dispute_id=None,
        description=None,
        method="upi",
    )


# --- Settlement-anchored assembly ---


def test_settlement_anchored_case_id_is_the_settlement_id_and_groups_its_recon_lines():
    settlement = _settlement("setl_aaaaaaaa")
    other = _settlement("setl_bbbbbbbb")
    lines = [
        _recon_line("pay_1", settlement_id=settlement.id),
        _recon_line("pay_2", settlement_id=settlement.id),
        _recon_line("pay_3", settlement_id=other.id),
        _recon_line("pay_unsettled", settlement_id=None),
    ]

    cases = assemble_settlement_anchored_cases([settlement, other], lines)

    by_id = {case.case_id: case for case in cases}
    assert set(by_id) == {settlement.id, other.id}
    assert by_id[settlement.id].kind is CaseKind.SETTLEMENT_ANCHORED
    assert {line.entity_id for line in by_id[settlement.id].recon_lines} == {"pay_1", "pay_2"}
    assert {line.entity_id for line in by_id[other.id].recon_lines} == {"pay_3"}


def test_settlement_with_no_recon_lines_still_assembles_an_empty_case():
    settlement = _settlement("setl_cccccccc")
    cases = assemble_settlement_anchored_cases([settlement], [])
    assert len(cases) == 1
    assert cases[0].recon_lines == ()


# --- Orphan assembly: each exclusion/inclusion rule in isolation ---


def test_razorpay_named_credit_is_excluded_not_an_orphan_case():
    lines = [_line("bank_1", narration="NEFT CR RAZORPAY SOFTWARE PVT LTD AXIS000000000001", deposit=1000_00)]
    assert assemble_orphan_cases(lines) == []


def test_bank_charge_is_excluded_not_an_orphan_case():
    lines = [_line("bank_1", narration="SMS ALERT CHARGES", withdrawal=25_00)]
    assert assemble_orphan_cases(lines) == []


def test_self_matching_reversal_pair_is_excluded_not_an_orphan_case():
    lines = [
        _line("bank_credit", narration="NEFT CR SHARMA ENTERPRISES VNDU003525893632", deposit=500_00),
        _line("bank_reversal", narration="NEFT REVERSAL VNDU003525893632", withdrawal=500_00),
    ]
    assert assemble_orphan_cases(lines) == []


def test_plain_outbound_transfer_is_excluded_not_an_orphan_case():
    lines = [_line("bank_1", narration="NEFT DR SHARMA ENTERPRISES XZTK007070484253", withdrawal=300_00)]
    assert assemble_orphan_cases(lines) == []


def test_unmatched_reversal_is_one_case_one_line():
    lines = [_line("bank_1", narration="NEFT REVERSAL ORPHAN00000000001", withdrawal=500_00)]
    cases = assemble_orphan_cases(lines)
    assert len(cases) == 1
    assert cases[0].kind is CaseKind.ORPHAN
    assert [line.line_id for line in cases[0].bank_lines] == ["bank_1"]


def test_named_counterparty_credit_is_one_case_one_line():
    lines = [_line("bank_1", narration="NEFT CR SHARMA ENTERPRISES VNDU003525893632", deposit=500_00)]
    cases = assemble_orphan_cases(lines)
    assert len(cases) == 1
    assert [line.line_id for line in cases[0].bank_lines] == ["bank_1"]


def test_opaque_credit_is_one_case_one_line():
    lines = [_line("bank_1", narration="MISC CREDIT", deposit=500_00)]
    cases = assemble_orphan_cases(lines)
    assert len(cases) == 1
    assert [line.line_id for line in cases[0].bank_lines] == ["bank_1"]


def test_duplicate_credit_is_one_case_spanning_both_lines_rev18():
    narration = "NEFT-CR-BLUE OCEAN TRADERS-VNDU003525893632"
    lines = [
        _line("bank_1", narration=narration, deposit=750_00),
        _line("bank_2", narration=narration, deposit=750_00),
    ]
    cases = assemble_orphan_cases(lines)
    assert len(cases) == 1
    assert cases[0].kind is CaseKind.ORPHAN
    assert {line.line_id for line in cases[0].bank_lines} == {"bank_1", "bank_2"}


def test_duplicate_credit_pairing_requires_matching_date_amount_and_narration():
    narration = "NEFT-CR-BLUE OCEAN TRADERS-VNDU003525893632"
    lines = [
        _line("bank_1", narration=narration, deposit=750_00, value_date=SNAPSHOT),
        _line("bank_2", narration=narration, deposit=750_00, value_date=SNAPSHOT.replace(day=SNAPSHOT.day - 1)),
    ]
    cases = assemble_orphan_cases(lines)
    assert len(cases) == 2  # different value_date: two independent one-line cases, not a pair


def test_case_id_is_deterministic_across_repeated_assembly():
    lines = [_line("bank_1", narration="NEFT CR SHARMA ENTERPRISES VNDU003525893632", deposit=500_00)]
    first = assemble_orphan_cases(lines)
    second = assemble_orphan_cases(lines)
    assert [c.case_id for c in first] == [c.case_id for c in second]


# --- The session checkpoint itself: 150 cases assemble against the full reference batch. ---


def test_150_cases_assemble_with_rev18_orphan_granularity():
    rng = random.Random(0)
    batch = generate_reference_batch(rng, SNAPSHOT)

    cases = assemble_cases(batch.settlements, batch.recon_lines, batch.bank_lines)

    assert len(cases) == 150
    settlement_cases = [c for c in cases if c.kind is CaseKind.SETTLEMENT_ANCHORED]
    orphan_cases = [c for c in cases if c.kind is CaseKind.ORPHAN]
    assert len(settlement_cases) == 125
    assert len(orphan_cases) == 25

    assert {c.case_id for c in settlement_cases} == {s.id for s in batch.settlements}

    two_line = [c for c in orphan_cases if len(c.bank_lines) == 2]
    one_line = [c for c in orphan_cases if len(c.bank_lines) == 1]
    assert len(two_line) == 3  # REV-18: the three DUPLICATE_CREDIT cases
    assert len(one_line) == 22
    assert sum(len(c.bank_lines) for c in orphan_cases) == 28  # REV-17's ~28 orphan-case-line figure, exactly

    all_orphan_line_ids = [line.line_id for c in orphan_cases for line in c.bank_lines]
    assert len(all_orphan_line_ids) == len(set(all_orphan_line_ids))  # no bank line claimed by two cases


def test_150_cases_assemble_across_multiple_seeds():
    for seed in range(1, 4):
        rng = random.Random(seed)
        batch = generate_reference_batch(rng, SNAPSHOT)
        cases = assemble_cases(batch.settlements, batch.recon_lines, batch.bank_lines)
        assert len(cases) == 150, f"seed={seed}"
