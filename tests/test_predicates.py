"""The checkpoint: no `(case_id, entity_id)` fires two predicates — a
mutual-exclusivity assertion — plus targeted unit coverage of each of the six
evidence predicates and each deterministic `OPERATIONAL_EXCEPTION` subtype
trigger in `pipeline/predicates.py`.
"""

from __future__ import annotations

import random
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import pytest

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
from pipeline.ground_truth import ExceptionSubtype
from pipeline.matcher import MatchTier, match_cases
from pipeline.money import Paise
from pipeline.predicates import (
    PredicateOverlapError,
    TemplateId,
    evaluate_case,
    evaluate_cases,
    index_ledger_entries,
    subtype_trigger_distribution,
    template_hit_distribution,
)
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

SNAPSHOT = date(2026, 8, 28)
CAPTURE = date(2026, 8, 26)  # a Wednesday

GROSS = 1000_00
FEE = 20_00
TAX = 3_60
NET = GROSS - FEE - TAX


def _ts(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc).timestamp())


def _payment(entity_id: str = "pay_0001", *, fee: int = FEE, tax: int = TAX, dispute_id: str | None = None) -> ReconLine:
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
        dispute_id=dispute_id,
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
    """A settlement-anchored case whose header amount agrees with its lines by default.

    `match_tier` defaults to a tier-0 match so `T-04`'s "no bank credit
    matched" conjunct is *off* unless a test deliberately turns it on.
    """
    if settlement is None:
        # Clamped at zero because `Settlement.amount` is `NonNegPaise`: a
        # one-line fixture carrying only a refund or a debit adjustment nets
        # negative, which no real settlement header ever does. Tests that
        # hit the clamp read `template_hits` only, never the amount-mismatch
        # trigger the clamp would set off.
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


def _evaluate(case: Case, entries: list[LedgerEntry]):
    return evaluate_case(case, index_ledger_entries(entries))


def _fired(case: Case, entries: list[LedgerEntry]) -> set[TemplateId]:
    return {hit.template_id for hit in _evaluate(case, entries).template_hits}


def _triggered(case: Case, entries: list[LedgerEntry] | None = None) -> set[ExceptionSubtype]:
    return {trigger.subtype for trigger in _evaluate(case, entries or []).subtype_triggers}


# --- T-01: unposted MDR fee + GST, revenue booked at gross (family 1). ---


def test_t01_fires_when_fee_unposted_and_revenue_booked_at_gross():
    payment = _payment()
    entries = [
        _entry("je_1", ACCOUNT_RAZORPAY_CLEARING, reference=payment.entity_id, debit=GROSS),
        _entry("je_2", ACCOUNT_SALES_REVENUE, reference=payment.entity_id, credit=GROSS),
    ]

    evidence = _evaluate(_case((payment,)), entries)

    assert [hit.template_id for hit in evidence.template_hits] == [TemplateId.T01]
    assert evidence.template_hits[0].cited_record_ids == (payment.entity_id, "je_2")


def test_t01_does_not_fire_when_the_fee_was_posted():
    """A correctly-booked payment credits revenue at gross too — the `Payment Gateway
    Charges` conjunct is the only thing keeping it out of T-01."""
    payment = _payment()
    entries = [
        _entry("je_1", ACCOUNT_RAZORPAY_CLEARING, reference=payment.entity_id, debit=NET),
        _entry("je_2", ACCOUNT_PAYMENT_GATEWAY_CHARGES, reference=payment.entity_id, debit=FEE),
        _entry("je_3", ACCOUNT_GST_ON_GATEWAY_CHARGES, reference=payment.entity_id, debit=TAX),
        _entry("je_4", ACCOUNT_SALES_REVENUE, reference=payment.entity_id, credit=GROSS),
    ]

    assert _fired(_case((payment,)), entries) == set()


def test_t01_does_not_fire_when_revenue_is_booked_at_net():
    payment = _payment()
    entries = [
        _entry("je_1", ACCOUNT_RAZORPAY_CLEARING, reference=payment.entity_id, debit=NET),
        _entry("je_2", ACCOUNT_SALES_REVENUE, reference=payment.entity_id, credit=NET),
    ]

    assert TemplateId.T01 not in _fired(_case((payment,)), entries)


def test_t01_does_not_fire_on_a_zero_fee_payment():
    payment = _payment(fee=0, tax=0)
    entries = [_entry("je_1", ACCOUNT_SALES_REVENUE, reference=payment.entity_id, credit=GROSS)]

    assert _fired(_case((payment,)), entries) == set()


# --- T-02: settled refund absent from the ledger (family 2). ---


def test_t02_fires_when_a_settled_refund_has_no_contra_revenue_entry():
    refund = _refund()

    evidence = _evaluate(_case((refund,)), [])

    assert [hit.template_id for hit in evidence.template_hits] == [TemplateId.T02]
    assert evidence.template_hits[0].cited_record_ids == (refund.entity_id,)


def test_t02_does_not_fire_when_the_refund_was_posted():
    refund = _refund()
    entries = [
        _entry("je_1", ACCOUNT_SALES_RETURNS_AND_ALLOWANCES, reference=refund.entity_id, debit=GROSS),
        _entry("je_2", ACCOUNT_RAZORPAY_CLEARING, reference=refund.entity_id, credit=GROSS),
    ]

    assert _fired(_case((refund,)), entries) == set()


def test_t02_does_not_fire_on_an_unsettled_refund():
    refund = _refund().model_copy(update={"settled": False})

    assert _fired(_case((refund,)), []) == set()


# --- T-03: gross-vs-net posting error (family 3). ---


def test_t03_fires_when_revenue_is_booked_at_net():
    payment = _payment()
    entries = [
        _entry("je_1", ACCOUNT_RAZORPAY_CLEARING, reference=payment.entity_id, debit=NET),
        _entry("je_2", ACCOUNT_SALES_REVENUE, reference=payment.entity_id, credit=NET),
    ]

    evidence = _evaluate(_case((payment,)), entries)

    assert [hit.template_id for hit in evidence.template_hits] == [TemplateId.T03]
    assert evidence.template_hits[0].cited_record_ids == (payment.entity_id, "je_2")


def test_t03_does_not_fire_on_a_correctly_booked_payment():
    payment = _payment()
    entries = [
        _entry("je_1", ACCOUNT_RAZORPAY_CLEARING, reference=payment.entity_id, debit=NET),
        _entry("je_2", ACCOUNT_PAYMENT_GATEWAY_CHARGES, reference=payment.entity_id, debit=FEE),
        _entry("je_3", ACCOUNT_GST_ON_GATEWAY_CHARGES, reference=payment.entity_id, debit=TAX),
        _entry("je_4", ACCOUNT_SALES_REVENUE, reference=payment.entity_id, credit=GROSS),
    ]

    assert _fired(_case((payment,)), entries) == set()


# --- T-04: premature Bank Account debit, no credit landed (family 4). ---


def _bank_mispost_entries(payment: ReconLine, *, entry_date: date = CAPTURE) -> list[LedgerEntry]:
    return [
        _entry("je_1", ACCOUNT_BANK_ACCOUNT, reference=payment.entity_id, debit=NET, entry_date=entry_date),
        _entry("je_2", ACCOUNT_PAYMENT_GATEWAY_CHARGES, reference=payment.entity_id, debit=FEE),
        _entry("je_3", ACCOUNT_GST_ON_GATEWAY_CHARGES, reference=payment.entity_id, debit=TAX),
        _entry("je_4", ACCOUNT_SALES_REVENUE, reference=payment.entity_id, credit=GROSS),
    ]


def test_t04_fires_on_a_premature_bank_debit_with_no_matching_credit():
    payment = _payment()
    case = _case((payment,), match_tier=int(MatchTier.NO_MATCH), in_settlement_window=True)

    evidence = _evaluate(case, _bank_mispost_entries(payment))

    assert [hit.template_id for hit in evidence.template_hits] == [TemplateId.T04]
    assert evidence.template_hits[0].cited_record_ids == (payment.entity_id, "je_1", "setl_0001")


def test_t04_does_not_fire_once_the_bank_credit_has_landed():
    """A hard precondition: with the credit landed this is a date error, not an
    account error, and posting T-04 would create a reconciliation break."""
    payment = _payment()
    case = _case((payment,), match_tier=int(MatchTier.UTR_EXACT))

    assert _fired(case, _bank_mispost_entries(payment)) == set()


def test_t04_does_not_fire_when_the_bank_debit_is_dated_past_the_settlement_window():
    payment = _payment()
    case = _case((payment,), match_tier=int(MatchTier.NO_MATCH), in_settlement_window=False)
    # Capture Wed 8/26; T+2 working days = Fri 8/28. Sat 8/29 is past it.
    entries = _bank_mispost_entries(payment, entry_date=date(2026, 8, 29))

    assert _fired(case, entries) == set()


def test_t04_does_not_fire_when_the_bank_debit_predates_capture():
    payment = _payment()
    case = _case((payment,), match_tier=int(MatchTier.NO_MATCH), in_settlement_window=False)
    entries = _bank_mispost_entries(payment, entry_date=CAPTURE - timedelta(days=1))

    assert _fired(case, entries) == set()


def test_t04_accepts_a_bank_debit_on_the_window_deadline_itself():
    payment = _payment()
    case = _case((payment,), match_tier=int(MatchTier.NO_MATCH), in_settlement_window=False)
    entries = _bank_mispost_entries(payment, entry_date=date(2026, 8, 28))  # T+2 working days from Wed 8/26

    assert _fired(case, entries) == {TemplateId.T04}


def test_t04_does_not_fire_on_a_clean_payment_with_no_credit_landed():
    """The family-4 no-op and `BANK_CREDIT_OVERDUE` populations are exactly this
    shape — correct books, no credit yet — and must produce no correction."""
    payment = _payment()
    case = _case((payment,), match_tier=int(MatchTier.NO_MATCH), in_settlement_window=True)
    entries = [
        _entry("je_1", ACCOUNT_RAZORPAY_CLEARING, reference=payment.entity_id, debit=NET),
        _entry("je_2", ACCOUNT_PAYMENT_GATEWAY_CHARGES, reference=payment.entity_id, debit=FEE),
        _entry("je_3", ACCOUNT_GST_ON_GATEWAY_CHARGES, reference=payment.entity_id, debit=TAX),
        _entry("je_4", ACCOUNT_SALES_REVENUE, reference=payment.entity_id, credit=GROSS),
    ]

    assert _fired(case, entries) == set()


# --- T-05 / T-06: unposted settlement adjustment (family 5). ---


def test_t05_fires_on_an_unposted_credit_adjustment():
    adjustment = _adjustment(credit=500_00)

    assert _fired(_case((adjustment,)), []) == {TemplateId.T05}


def test_t06_fires_on_an_unposted_debit_adjustment():
    adjustment = _adjustment(debit=500_00)

    assert _fired(_case((adjustment,)), []) == {TemplateId.T06}


def test_t05_and_t06_do_not_fire_once_the_adjustment_is_posted():
    adjustment = _adjustment(credit=500_00)
    entries = [
        _entry("je_1", ACCOUNT_RAZORPAY_CLEARING, reference=adjustment.entity_id, debit=500_00),
        _entry("je_2", ACCOUNT_RAZORPAY_SETTLEMENT_ADJUSTMENTS, reference=adjustment.entity_id, credit=500_00),
    ]

    assert _fired(_case((adjustment,)), entries) == set()


# --- A double fire is a hard error, raised in production code. ---


def test_two_predicates_firing_on_one_entity_raises():
    """T-01 and T-03 are separated by one number. Give a payment both a gross and a
    net `Sales Revenue` credit and no fee posting, and both fire — the exact
    failure mode this guards against, since both instantiations balance and post
    to different accounts."""
    payment = _payment()
    entries = [
        _entry("je_1", ACCOUNT_SALES_REVENUE, reference=payment.entity_id, credit=GROSS),
        _entry("je_2", ACCOUNT_SALES_REVENUE, reference=payment.entity_id, credit=NET),
    ]

    with pytest.raises(PredicateOverlapError) as excinfo:
        _evaluate(_case((payment,)), entries)

    assert "T-01" in str(excinfo.value) and "T-03" in str(excinfo.value)


def test_an_unmatched_case_is_rejected_rather_than_evaluated():
    """T-04 and BANK_CREDIT_OVERDUE both read the matcher's output; evaluating a
    case that never went through the matcher would silently under-fire."""
    with pytest.raises(ValueError, match="has not been matched"):
        _evaluate(_case((_payment(),), match_tier=None), [])


# --- OPERATIONAL_EXCEPTION subtype triggers. ---


def test_settlement_utr_missing_triggers_on_a_processed_settlement_with_no_utr():
    case = _case((_payment(),), settlement=_settlement(amount=NET, utr=""))

    assert ExceptionSubtype.SETTLEMENT_UTR_MISSING in _triggered(case)


def test_settlement_utr_missing_does_not_trigger_when_a_utr_is_present():
    assert ExceptionSubtype.SETTLEMENT_UTR_MISSING not in _triggered(_case((_payment(),)))


def test_bank_credit_overdue_triggers_only_once_the_window_has_elapsed():
    payment = _payment()
    overdue = _case((payment,), match_tier=int(MatchTier.NO_MATCH), in_settlement_window=False)
    still_open = _case((payment,), match_tier=int(MatchTier.NO_MATCH), in_settlement_window=True)

    assert ExceptionSubtype.BANK_CREDIT_OVERDUE in _triggered(overdue)
    assert ExceptionSubtype.BANK_CREDIT_OVERDUE not in _triggered(still_open)


def test_settlement_amount_mismatch_triggers_on_an_exact_integer_paise_difference():
    payment = _payment()
    off_by_one = _case((payment,), settlement=_settlement(amount=NET + 1))
    exact = _case((payment,), settlement=_settlement(amount=NET))

    assert ExceptionSubtype.SETTLEMENT_AMOUNT_MISMATCH in _triggered(off_by_one)
    assert ExceptionSubtype.SETTLEMENT_AMOUNT_MISMATCH not in _triggered(exact)


def test_dispute_pending_triggers_on_a_populated_dispute_id():
    disputed = _payment(dispute_id="disp_0001")
    case = _case((disputed,), settlement=_settlement(amount=NET))

    evidence = _evaluate(case, [])
    triggers = {t.subtype: t for t in evidence.subtype_triggers}

    assert ExceptionSubtype.DISPUTE_PENDING in triggers
    assert triggers[ExceptionSubtype.DISPUTE_PENDING].cited_record_ids == (disputed.entity_id,)


def _bank_line(line_id: str, *, narration: str, deposit: int = 0, withdrawal: int = 0) -> BankLine:
    return BankLine(
        line_id=line_id,
        value_date=SNAPSHOT,
        narration=narration,
        bank_ref_no=None,
        withdrawal_paise=Paise(withdrawal),
        deposit_paise=Paise(deposit),
        closing_balance_paise=Paise(10_000_00),
        bank_profile=BankProfile.HDFC,
    )


def _orphan(*lines: BankLine) -> Case:
    return Case(case_id="orphan_0001", kind=CaseKind.ORPHAN, bank_lines=lines)


def test_duplicate_credit_triggers_on_two_credits_sharing_a_reference_token():
    narration = "NEFT CR ACME TRADING PVT LTD HDFC0001202608280009"
    case = _orphan(
        _bank_line("bank_1", narration=narration, deposit=500_00),
        _bank_line("bank_2", narration=narration, deposit=500_00),
    )

    evidence = _evaluate(case, [])

    assert [t.subtype for t in evidence.subtype_triggers] == [ExceptionSubtype.DUPLICATE_CREDIT]
    assert evidence.subtype_triggers[0].cited_record_ids == ("bank_1", "bank_2")


def test_duplicate_credit_does_not_trigger_on_two_credits_with_unrelated_references():
    case = _orphan(
        _bank_line("bank_1", narration="NEFT CR ACME HDFC0001202608280009", deposit=500_00),
        _bank_line("bank_2", narration="NEFT CR ACME ICIC0002202608280077", deposit=500_00),
    )

    assert _triggered(case) == set()


def test_reversal_unmatched_triggers_on_a_lone_reversal_shaped_withdrawal():
    case = _orphan(_bank_line("bank_1", narration="NEFT RETURN HDFC0001202608280009 ACME", withdrawal=500_00))

    evidence = _evaluate(case, [])

    assert [t.subtype for t in evidence.subtype_triggers] == [ExceptionSubtype.REVERSAL_UNMATCHED]


def test_unmatched_inbound_credit_is_never_triggered_deterministically():
    """Whether a narration identifies a counterparty belongs to Slot A, the one
    graded LLM slot. Component 4 must leave that question open, not pre-answer it."""
    case = _orphan(_bank_line("bank_1", narration="NEFT CR ACME TRADING PVT LTD HDFC0001202608280009", deposit=500_00))

    assert _triggered(case) == set()


# --- The session checkpoint itself, against the full reference batch. ---


def _evaluated_reference_batch(seed: int):
    rng = random.Random(seed)
    batch = generate_reference_batch(rng, SNAPSHOT)
    cases = assemble_cases(batch.settlements, batch.recon_lines, batch.bank_lines)
    matched = match_cases(cases, batch.bank_lines, snapshot_date=SNAPSHOT)
    return batch, evaluate_cases(matched, batch.ledger_entries)


def _expected_subtype_lookup(batch):
    """Ground-truth subtype per assembled case, keyed the two ways cases are anchored.

    Settlement-anchored cases share their `case_id` with ground truth
    (`case_id == settlement.id`, every population's own convention). Orphan
    cases do not: `pipeline/case_assembly.py` mints its own `case_orphan_*`
    id from the bank line, while the generator minted an unrelated
    `orphan_*` id. The two are joined through
    `expected_linked_source_records`, which for an orphan case is exactly
    its `bank_line.line_id`s. Reconciling the two id spaces properly is the
    reporter's job (component 9); this is only enough of it to look up a
    label in a test.
    """
    by_case_id = {gt.case_id: gt.ground_truth_exception_subtype for gt in batch.ground_truth}
    by_line_id = {
        record_id: gt.ground_truth_exception_subtype
        for gt in batch.ground_truth
        for record_id in gt.expected_linked_source_records
        if record_id.startswith("bank_")
    }
    return by_case_id, by_line_id


def test_no_case_entity_pair_fires_two_predicates():
    """The checkpoint.

    `evaluate_cases` raises `PredicateOverlapError` on a double fire, so
    completing at all is half the assertion; the explicit count below is
    the other half, so this test still fails loudly if that guard is ever
    weakened to a warning or a precedence rule.
    """
    for seed in range(4):
        _batch, evidences = _evaluated_reference_batch(seed)

        pair_counts = Counter(
            (hit.case_id, hit.entity_id) for evidence in evidences for hit in evidence.template_hits
        )
        double_fires = {pair: count for pair, count in pair_counts.items() if count > 1}
        assert not double_fires, f"seed={seed}: double predicate fire on {double_fires}"
        assert pair_counts, f"seed={seed}: no predicate fired at all"


def test_every_template_predicate_fires_and_agrees_with_ground_truth():
    """All six templates are exercised, and the templates that fire on a case are
    exactly the ones ground truth expects — with one documented exception.

    The 12 policy-excluded tax cases are structurally identical unposted adjustments
    (`generator/exceptions.py` builds them with family 5's own
    `build_adjustment_line`), so `T-05`/`T-06` fire on them. What separates
    the two populations is a policy exclusion, which a later component
    explicitly defers — not the evidence predicate. Asserting the
    exception by name keeps it a known, bounded difference rather than
    something discovered later as drift.
    """
    batch, evidences = _evaluated_reference_batch(0)

    distribution = template_hit_distribution(evidences)
    assert set(distribution) == {t.value for t in TemplateId}, f"not every template fires: {distribution}"

    ground_truth = {gt.case_id: gt for gt in batch.ground_truth}
    unexpected: dict[str, tuple] = {}
    for evidence in evidences:
        fired = tuple(sorted(str(hit.template_id) for hit in evidence.template_hits))
        if evidence.case_id not in ground_truth:
            # An orphan case, whose id case assembly minted itself. It holds no
            # recon lines, so no template predicate can reach it at all.
            assert not fired, f"{evidence.case_id}: an orphan case fired {fired}"
            continue
        expected = tuple(sorted(ground_truth[evidence.case_id].expected_template_ids))
        if fired != expected:
            unexpected[evidence.case_id] = (batch.population_of.get(evidence.case_id), expected, fired)

    populations = Counter(entry[0] for entry in unexpected.values())
    assert populations == {"fr06_tax": 12}, f"unexpected predicate disagreement: {unexpected}"
    assert all(fired in (("T-05",), ("T-06",)) for _pop, _expected, fired in unexpected.values())


def test_deterministic_subtype_triggers_agree_with_ground_truth():
    """Each deterministically-decidable trigger fires on exactly its own population.

    `BANK_CREDIT_OVERDUE` is deliberately a superset: its trigger
    ("settlement window has elapsed with no matching bank credit") is
    literally true of family-4 core cases too. That is not a defect — it is
    why evaluation is split from classification. The assertion that
    matters is that every extra case also carries a template hit, so the
    classifier (component 5) has the evidence to prefer
    `ACCOUNTING_CORRECTION` over `OPERATIONAL_EXCEPTION` rather than
    guessing.
    """
    batch, evidences = _evaluated_reference_batch(0)

    distribution = subtype_trigger_distribution(evidences)
    assert distribution["SETTLEMENT_UTR_MISSING"] == 5
    assert distribution["SETTLEMENT_AMOUNT_MISMATCH"] == 4
    assert distribution["DISPUTE_PENDING"] == 5
    assert distribution["DUPLICATE_CREDIT"] == 3
    assert distribution["REVERSAL_UNMATCHED"] == 6
    assert ExceptionSubtype.UNMATCHED_INBOUND_CREDIT.value not in distribution

    by_case_id, by_line_id = _expected_subtype_lookup(batch)

    def expected_subtype(evidence) -> ExceptionSubtype:
        if evidence.case_id in by_case_id:
            return by_case_id[evidence.case_id]
        cited = [record_id for trigger in evidence.subtype_triggers for record_id in trigger.cited_record_ids]
        return by_line_id[cited[0]]

    for evidence in evidences:
        fired = {trigger.subtype for trigger in evidence.subtype_triggers}
        for subtype in fired - {ExceptionSubtype.BANK_CREDIT_OVERDUE}:
            assert subtype is expected_subtype(evidence), (
                f"{evidence.case_id}: fired {subtype}, ground truth {expected_subtype(evidence)}"
            )

    overdue = [e for e in evidences if any(t.subtype is ExceptionSubtype.BANK_CREDIT_OVERDUE for t in e.subtype_triggers)]
    genuine = [e for e in overdue if by_case_id[e.case_id] is ExceptionSubtype.BANK_CREDIT_OVERDUE]
    assert len(genuine) == 5
    for evidence in overdue:
        if evidence not in genuine:
            assert evidence.template_hits, (
                f"{evidence.case_id}: fires BANK_CREDIT_OVERDUE but is not one, and carries no "
                "template hit for the classifier to prefer"
            )
