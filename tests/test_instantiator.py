""" checkpoint: "Every candidate JV balances; a
`tax = 0` `T-01` collapses correctly." Unit coverage of amount derivation,
zero-leg omission and per-case aggregation in `pipeline/instantiator.py`,
plus template-by-template and full-batch agreement with the generator's own
`expected_journal_entries`.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timezone

from generator.cli import generate_reference_batch
from pipeline.accounts import (
    ACCOUNT_BANK_ACCOUNT,
    ACCOUNT_GST_ON_GATEWAY_CHARGES,
    ACCOUNT_PAYMENT_GATEWAY_CHARGES,
    ACCOUNT_RAZORPAY_CLEARING,
    ACCOUNT_RAZORPAY_SETTLEMENT_ADJUSTMENTS,
    ACCOUNT_SALES_RETURNS_AND_ALLOWANCES,
    ACCOUNT_SALES_REVENUE,
    Account,
)
from pipeline.case_assembly import Case, CaseKind, assemble_cases
from pipeline.instantiator import (
    TEMPLATE_LEG_ACCOUNTS,
    CandidateJournalEntry,
    index_ledger_entries_by_id,
    instantiate_case,
    instantiate_cases,
)
from pipeline.matcher import MatchTier, match_cases
from pipeline.money import Paise
from pipeline.predicates import TemplateId, evaluate_case, evaluate_cases, index_ledger_entries
from pipeline.schemas import LedgerEntry, LedgerSource, RazorpayEntityType, ReconLine, Settlement, SettlementStatus

SNAPSHOT = date(2026, 8, 28)
CAPTURE = date(2026, 8, 26)

GROSS = 1000_00
FEE = 20_00
TAX = 3_60
NET = GROSS - FEE - TAX


def _ts(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc).timestamp())


def _payment(entity_id: str = "pay_0001", *, fee: int = FEE, tax: int = TAX) -> ReconLine:
    return ReconLine(
        entity_id=entity_id,
        type=RazorpayEntityType.PAYMENT,
        debit=Paise(0),
        credit=Paise(GROSS),
        amount=Paise(GROSS),
        fee=Paise(fee),
        tax=Paise(tax),
        on_hold=False,
        settled=True,
        created_at=_ts(CAPTURE),
        settled_at=_ts(CAPTURE),
        settlement_id="setl_0001",
        settlement_utr="AXIS0001202608280001",
        credit_type="default",
    )


def _refund(entity_id: str = "rfnd_0001", *, debit: int = GROSS) -> ReconLine:
    return ReconLine(
        entity_id=entity_id,
        type=RazorpayEntityType.REFUND,
        debit=Paise(debit),
        credit=Paise(0),
        amount=Paise(debit),
        fee=Paise(0),
        tax=Paise(0),
        on_hold=False,
        settled=True,
        created_at=_ts(CAPTURE),
        settled_at=_ts(CAPTURE),
        settlement_id="setl_0001",
        settlement_utr="AXIS0001202608280001",
        payment_id="pay_0001",
        credit_type="default",
    )


def _adjustment(entity_id: str = "adj_0001", *, credit: int = 0, debit: int = 0) -> ReconLine:
    return ReconLine(
        entity_id=entity_id,
        type=RazorpayEntityType.ADJUSTMENT,
        debit=Paise(debit),
        credit=Paise(credit),
        amount=Paise(credit or debit),
        fee=Paise(0),
        tax=Paise(0),
        on_hold=False,
        settled=True,
        created_at=_ts(CAPTURE),
        settled_at=_ts(CAPTURE),
        settlement_id="setl_0001",
        settlement_utr=None,
        credit_type="default",
    )


def _entry(
    journal_entry_id: str,
    account: Account,
    *,
    reference: str,
    debit: int = 0,
    credit: int = 0,
    entry_date: date = CAPTURE,
) -> LedgerEntry:
    return LedgerEntry(
        journal_entry_id=journal_entry_id,
        date=entry_date,
        account_code=account.code,
        account_name=account.name,
        debit=Paise(debit),
        credit=Paise(credit),
        reference=reference,
        narration="ERP import - Razorpay UPI",
        source=LedgerSource.ERP_IMPORT,
    )


def _settlement(*, amount: int, utr: str = "AXIS0001202608280001") -> Settlement:
    return Settlement(
        id="setl_0001",
        amount=Paise(amount),
        status=SettlementStatus.PROCESSED,
        fees=Paise(0),
        tax=Paise(0),
        utr=utr,
        created_at=_ts(CAPTURE),
    )


def _case(
    recon_lines: tuple[ReconLine, ...],
    *,
    settlement: Settlement | None = None,
    match_tier: int | None = int(MatchTier.UTR_EXACT),
    in_settlement_window: bool | None = None,
) -> Case:
    if settlement is None:
        recon_total = sum(int(l.credit) - int(l.debit) - int(l.fee) - int(l.tax) for l in recon_lines)
        settlement = _settlement(amount=max(0, recon_total))
    return Case(
        case_id=settlement.id,
        kind=CaseKind.SETTLEMENT_ANCHORED,
        settlement=settlement,
        recon_lines=recon_lines,
        match_tier=match_tier,
        in_settlement_window=in_settlement_window,
    )


def _instantiate(case: Case, entries: list[LedgerEntry]) -> tuple[CandidateJournalEntry, ...]:
    evidence = evaluate_case(case, index_ledger_entries(entries))
    return instantiate_case(evidence, case, index_ledger_entries_by_id(entries))


def _leg_map(entry: CandidateJournalEntry) -> dict[str, tuple[int, int]]:
    return {leg.account_code: (leg.debit, leg.credit) for leg in entry.legs}


def _balances(entry: CandidateJournalEntry) -> bool:
    return sum(leg.debit for leg in entry.legs) == sum(leg.credit for leg in entry.legs)


# --- T-01: three legs normally, two when tax collapses to zero. ---


def test_t01_instantiates_three_balanced_legs():
    payment = _payment()
    entries = [_entry("je_1", ACCOUNT_SALES_REVENUE, reference=payment.entity_id, credit=GROSS)]

    (entry,) = _instantiate(_case((payment,)), entries)

    assert entry.template_id is TemplateId.T01
    assert _leg_map(entry) == {
        ACCOUNT_PAYMENT_GATEWAY_CHARGES.code: (FEE, 0),
        ACCOUNT_GST_ON_GATEWAY_CHARGES.code: (TAX, 0),
        ACCOUNT_RAZORPAY_CLEARING.code: (0, FEE + TAX),
    }
    assert _balances(entry)
    assert entry.cited_record_ids == (payment.entity_id, "je_1")


def test_t01_with_zero_tax_collapses_to_two_legs():
    """The template definitions: "Zero-amount legs are omitted, not posted... `T-01` with `tax = 0`
    collapses to `Dr Payment Gateway Charges / Cr Razorpay Clearing` — a legal
    instantiation of `T-01`, not a seventh template." The session checkpoint itself.
    """
    payment = _payment(tax=0)
    entries = [_entry("je_1", ACCOUNT_SALES_REVENUE, reference=payment.entity_id, credit=GROSS)]

    (entry,) = _instantiate(_case((payment,)), entries)

    assert entry.template_id is TemplateId.T01
    legs = _leg_map(entry)
    assert len(entry.legs) == 2
    assert ACCOUNT_GST_ON_GATEWAY_CHARGES.code not in legs
    assert legs == {
        ACCOUNT_PAYMENT_GATEWAY_CHARGES.code: (FEE, 0),
        ACCOUNT_RAZORPAY_CLEARING.code: (0, FEE),
    }
    assert _balances(entry)


# --- T-02: settled refund unposted (family 2). ---


def test_t02_instantiates_two_legs_at_the_debit_amount():
    refund = _refund(debit=750_00)

    (entry,) = _instantiate(_case((refund,)), [])

    assert entry.template_id is TemplateId.T02
    assert _leg_map(entry) == {
        ACCOUNT_SALES_RETURNS_AND_ALLOWANCES.code: (750_00, 0),
        ACCOUNT_RAZORPAY_CLEARING.code: (0, 750_00),
    }
    assert entry.cited_record_ids == (refund.entity_id,)
    assert _balances(entry)


# --- T-03: gross-vs-net posting error (family 3). ---


def test_t03_credits_sales_revenue_instead_of_clearing():
    payment = _payment()
    entries = [_entry("je_1", ACCOUNT_SALES_REVENUE, reference=payment.entity_id, credit=NET)]

    (entry,) = _instantiate(_case((payment,)), entries)

    assert entry.template_id is TemplateId.T03
    assert _leg_map(entry) == {
        ACCOUNT_PAYMENT_GATEWAY_CHARGES.code: (FEE, 0),
        ACCOUNT_GST_ON_GATEWAY_CHARGES.code: (TAX, 0),
        ACCOUNT_SALES_REVENUE.code: (0, FEE + TAX),
    }
    assert _balances(entry)


# --- T-04: reclassifies the ledger's own premature debit amount, not a recomputed figure. ---


def test_t04_uses_the_cited_ledger_entrys_own_debit_amount():
    payment = _payment()
    premature_debit = 12345  # deliberately not derivable from GROSS/FEE/TAX
    entries = [
        _entry("je_1", ACCOUNT_BANK_ACCOUNT, reference=payment.entity_id, debit=premature_debit),
        _entry("je_2", ACCOUNT_PAYMENT_GATEWAY_CHARGES, reference=payment.entity_id, debit=FEE),
        _entry("je_3", ACCOUNT_GST_ON_GATEWAY_CHARGES, reference=payment.entity_id, debit=TAX),
        _entry("je_4", ACCOUNT_SALES_REVENUE, reference=payment.entity_id, credit=GROSS),
    ]
    case = _case((payment,), match_tier=int(MatchTier.NO_MATCH), in_settlement_window=True)

    (entry,) = _instantiate(case, entries)

    assert entry.template_id is TemplateId.T04
    assert _leg_map(entry) == {
        ACCOUNT_RAZORPAY_CLEARING.code: (premature_debit, 0),
        ACCOUNT_BANK_ACCOUNT.code: (0, premature_debit),
    }
    assert entry.cited_record_ids == (payment.entity_id, "je_1", "setl_0001")
    assert _balances(entry)


# --- T-05 / T-06: settlement adjustment, direction from sign. ---


def test_t05_credit_adjustment():
    adjustment = _adjustment(credit=500_00)

    (entry,) = _instantiate(_case((adjustment,)), [])

    assert entry.template_id is TemplateId.T05
    assert _leg_map(entry) == {
        ACCOUNT_RAZORPAY_CLEARING.code: (500_00, 0),
        ACCOUNT_RAZORPAY_SETTLEMENT_ADJUSTMENTS.code: (0, 500_00),
    }
    assert _balances(entry)


def test_t06_debit_adjustment():
    adjustment = _adjustment(debit=500_00)

    (entry,) = _instantiate(_case((adjustment,)), [])

    assert entry.template_id is TemplateId.T06
    assert _leg_map(entry) == {
        ACCOUNT_RAZORPAY_SETTLEMENT_ADJUSTMENTS.code: (500_00, 0),
        ACCOUNT_RAZORPAY_CLEARING.code: (0, 500_00),
    }
    assert _balances(entry)


# --- Aggregation: one entry per case per template, summed over every firing hit. ---


def test_aggregates_two_t01_hits_within_one_case_into_one_entry():
    payment_a = _payment("pay_0001", fee=FEE, tax=TAX)
    payment_b = _payment("pay_0002", fee=30_00, tax=5_40)
    entries = [
        _entry("je_1", ACCOUNT_SALES_REVENUE, reference=payment_a.entity_id, credit=GROSS),
        _entry("je_2", ACCOUNT_SALES_REVENUE, reference=payment_b.entity_id, credit=GROSS),
    ]

    result = _instantiate(_case((payment_a, payment_b)), entries)

    assert len(result) == 1
    (entry,) = result
    assert entry.template_id is TemplateId.T01
    assert _leg_map(entry) == {
        ACCOUNT_PAYMENT_GATEWAY_CHARGES.code: (FEE + 30_00, 0),
        ACCOUNT_GST_ON_GATEWAY_CHARGES.code: (TAX + 5_40, 0),
        ACCOUNT_RAZORPAY_CLEARING.code: (0, FEE + TAX + 30_00 + 5_40),
    }
    assert set(entry.cited_record_ids) == {payment_a.entity_id, "je_1", payment_b.entity_id, "je_2"}
    assert _balances(entry)


# --- Multiple templates per case are permitted, in TemplateId order. ---


def test_a_case_with_both_an_unposted_fee_and_an_unposted_refund_yields_two_entries():
    payment = _payment()
    refund = _refund()
    entries = [_entry("je_1", ACCOUNT_SALES_REVENUE, reference=payment.entity_id, credit=GROSS)]

    result = _instantiate(_case((payment, refund)), entries)

    assert [entry.template_id for entry in result] == [TemplateId.T01, TemplateId.T02]
    assert all(_balances(entry) for entry in result)


# --- No hits, no candidate entries. ---


def test_a_case_with_no_predicate_hits_instantiates_nothing():
    payment = _payment()
    entries = [
        _entry("je_1", ACCOUNT_RAZORPAY_CLEARING, reference=payment.entity_id, debit=NET),
        _entry("je_2", ACCOUNT_PAYMENT_GATEWAY_CHARGES, reference=payment.entity_id, debit=FEE),
        _entry("je_3", ACCOUNT_GST_ON_GATEWAY_CHARGES, reference=payment.entity_id, debit=TAX),
        _entry("je_4", ACCOUNT_SALES_REVENUE, reference=payment.entity_id, credit=GROSS),
    ]

    assert _instantiate(_case((payment,)), entries) == ()


# --- The session checkpoint itself, against the full reference batch. ---


def _instantiated_reference_batch(seed: int):
    rng = random.Random(seed)
    batch = generate_reference_batch(rng, SNAPSHOT)
    cases = assemble_cases(batch.settlements, batch.recon_lines, batch.bank_lines)
    matched = match_cases(cases, batch.bank_lines, snapshot_date=SNAPSHOT)
    evidences = evaluate_cases(matched, batch.ledger_entries)
    candidates = instantiate_cases(evidences, matched, batch.ledger_entries)
    return batch, candidates


def test_every_candidate_jv_in_the_reference_batch_balances():
    for seed in range(4):
        _batch, candidates = _instantiated_reference_batch(seed)
        assert candidates, f"seed={seed}: no candidate JVs instantiated at all"
        for entry in candidates:
            assert _balances(entry), f"seed={seed}: {entry.case_id}/{entry.template_id} does not balance"
            assert sum(leg.debit for leg in entry.legs) > 0, (
                f"seed={seed}: {entry.case_id}/{entry.template_id} is a zero-value entry"
            )
            assert len(entry.legs) >= 2
            for leg in entry.legs:
                assert leg.debit == 0 or leg.credit == 0, "a leg must not carry both a debit and a credit"


def test_every_candidate_leg_uses_an_account_permitted_for_its_own_template():
    _batch, candidates = _instantiated_reference_batch(0)
    for entry in candidates:
        debit_accounts, credit_accounts = TEMPLATE_LEG_ACCOUNTS[entry.template_id]
        allowed_debit = {account.code for account in debit_accounts}
        allowed_credit = {account.code for account in credit_accounts}
        for leg in entry.legs:
            if leg.debit > 0:
                assert leg.account_code in allowed_debit, f"{entry.template_id}: {leg.account_code} not debit-allowed"
            if leg.credit > 0:
                assert leg.account_code in allowed_credit, f"{entry.template_id}: {leg.account_code} not credit-allowed"


def test_auto_closed_family_candidates_match_the_generators_own_expected_journal_entries():
    """The five auto-closable families (1-5, 50 cases) are the only populations whose
    ground truth carries `expected_journal_entries` (the metric surface: normally empty for
    non-`AUTO_CLOSED` states). For those, the instantiator's own derivation must match
    the generator's hand-derived label exactly — not merely balance.
    """
    batch, candidates = _instantiated_reference_batch(0)

    candidates_by_case: dict[str, list[CandidateJournalEntry]] = {}
    for entry in candidates:
        candidates_by_case.setdefault(entry.case_id, []).append(entry)

    checked = 0
    for gt in batch.ground_truth:
        if not gt.expected_journal_entries:
            continue
        checked += 1
        produced = {c.template_id.value: c for c in candidates_by_case.get(gt.case_id, [])}
        for expected_entry in gt.expected_journal_entries:
            actual = produced[expected_entry.template_id]
            expected_legs = {(leg.account_code, leg.debit, leg.credit) for leg in expected_entry.legs}
            actual_legs = {(leg.account_code, leg.debit, leg.credit) for leg in actual.legs}
            assert actual_legs == expected_legs, (
                f"{gt.case_id}/{expected_entry.template_id}: expected {expected_legs}, got {actual_legs}"
            )
    assert checked == 50, f"expected exactly the 50 AUTO_CLOSED family cases to carry expected_journal_entries, got {checked}"
