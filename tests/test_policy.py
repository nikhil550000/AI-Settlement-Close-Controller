"""The policy exclusions' policy exclusions — the policy exclusions' tax positions and a revision's date-only reclassification.

Both gates decline entries that are *valid*: they balance, they cite real
records, and applying them would drive the residual to 0. So the tests
that matter are the negative ones — the cases that must **not** be
excluded — because a gate that over-fires silently converts correct
auto-closes into declines and `auto_close_recall` is the only place it
would show.

The batch-level test is the real assertion: exactly the 12 the policy exclusions cases
fire `TAX_POSITION`, exactly the 5 family-4 date-error cases fire
`DATE_ONLY_RECLASSIFICATION`, and no other case in the batch fires
either.
"""

from __future__ import annotations

import random
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import pytest

from generator.cli import generate_reference_batch
from generator.narration import TAX_SIGNATURES
from pipeline.accounts import ACCOUNT_RAZORPAY_CLEARING
from pipeline.case_assembly import Case, CaseKind, assemble_cases
from pipeline.instantiator import instantiate_cases
from pipeline.matcher import MatchTier, match_cases
from pipeline.policy import (
    PolicyExclusion,
    evaluate_policy,
    policy_exclusion_distribution,
)
from pipeline.predicates import (
    CaseEvidence,
    PredicateHit,
    TemplateId,
    evaluate_cases,
    index_ledger_entries,
)
from pipeline.schemas import (
    LedgerEntry,
    LedgerSource,
    RazorpayEntityType,
    ReconLine,
    Settlement,
    SettlementStatus,
)

SNAPSHOT = date(2026, 8, 28)
CAPTURE_TS = int(datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc).timestamp())
CAPTURE_DATE = date(2026, 8, 20)


def _recon_line(**overrides: object) -> ReconLine:
    base: dict[str, object] = {
        "entity_id": "pay_1",
        "type": RazorpayEntityType.PAYMENT,
        "debit": 0,
        "credit": 100_000,
        "amount": 100_000,
        "fee": 2_000,
        "tax": 360,
        "settled": True,
        "settled_at": CAPTURE_TS + 86_400,
        "created_at": CAPTURE_TS,
        "settlement_id": "setl_1",
        "settlement_utr": "UTRX000000000001",
        "payment_id": None,
        "order_id": None,
        "posted_at": None,
        "credit_type": "default",
        "dispute_id": None,
        "description": None,
        "method": "upi",
        "on_hold": False,
    }
    base.update(overrides)
    return ReconLine(**base)  # type: ignore[arg-type]


def _settlement() -> Settlement:
    return Settlement(
        id="setl_1",
        amount=97_640,
        status=SettlementStatus.PROCESSED,
        fees=2_000,
        tax=360,
        utr="UTRX000000000001",
        created_at=CAPTURE_TS + 86_400,
    )


def _ledger_entry(entry_date: date, reference: str = "pay_1") -> LedgerEntry:
    return LedgerEntry(
        journal_entry_id=f"je_{reference}_{entry_date.isoformat()}",
        date=entry_date,
        account_code=ACCOUNT_RAZORPAY_CLEARING.code,
        account_name=ACCOUNT_RAZORPAY_CLEARING.name,
        debit=97_640,
        credit=0,
        reference=reference,
        narration="Card settlement",
        source=LedgerSource.ERP_IMPORT,
    )


def _case(lines: tuple[ReconLine, ...], *, match_tier: int = int(MatchTier.UTR_EXACT)) -> Case:
    return Case(
        case_id="setl_1",
        kind=CaseKind.SETTLEMENT_ANCHORED,
        settlement=_settlement(),
        recon_lines=lines,
        match_tier=match_tier,
        residual_paise=0,
        in_settlement_window=True,
    )


def _no_evidence() -> CaseEvidence:
    return CaseEvidence(case_id="setl_1")


# --- the policy exclusions: tax positions. ---


@pytest.mark.parametrize("signature", TAX_SIGNATURES)
def test_each_generator_tax_signature_is_detected(signature: str) -> None:
    """The two the policy exclusions as the generator writes them, checked against a
    keyword set chosen from the tax domain rather than copied from that pool."""
    line = _recon_line(
        entity_id="adj_1", type=RazorpayEntityType.ADJUSTMENT, debit=5_000, credit=0, description=signature
    )

    decisions = evaluate_policy(_case((line,)), _no_evidence(), {})

    assert [decision.exclusion for decision in decisions] == [PolicyExclusion.TAX_POSITION]
    assert decisions[0].cited_record_ids == ("adj_1",)


@pytest.mark.parametrize(
    "description",
    ["Settlement adjustment", "On-hold balance release", "Reserve balance adjustment", None],
)
def test_a_neutral_adjustment_description_is_not_a_tax_position(description: str | None) -> None:
    """Family 5's adjustments must stay on the auto path — the generator plan gives them a
    description precisely so the field's *presence* is not the tell."""
    line = _recon_line(
        entity_id="adj_1", type=RazorpayEntityType.ADJUSTMENT, debit=5_000, credit=0, description=description
    )

    assert evaluate_policy(_case((line,)), _no_evidence(), {}) == ()


def test_a_tax_signature_on_a_non_adjustment_line_is_ignored() -> None:
    """The model-slot boundary fixes the detection surface as the *adjustment* line specifically."""
    line = _recon_line(description="TDS deduction under Section 194-O (e-commerce operator)")

    assert evaluate_policy(_case((line,)), _no_evidence(), {}) == ()


# --- a later revision: date-only reclassification. ---


def test_a_month_crossing_posting_on_a_banked_settlement_is_excluded() -> None:
    line = _recon_line()
    index = index_ledger_entries([_ledger_entry(CAPTURE_DATE - timedelta(days=32))])

    decisions = evaluate_policy(_case((line,)), _no_evidence(), index)

    assert [decision.exclusion for decision in decisions] == [PolicyExclusion.DATE_ONLY_RECLASSIFICATION]


def test_a_posting_in_the_capture_month_is_not_excluded() -> None:
    line = _recon_line()
    index = index_ledger_entries([_ledger_entry(CAPTURE_DATE + timedelta(days=3))])

    assert evaluate_policy(_case((line,)), _no_evidence(), index) == ()


def test_an_unmatched_settlement_is_not_excluded() -> None:
    """The policy exclusions' conjunct: "where the settlement has **already credited the bank**".

    With no credit landed, this is family 4's own territory — an account
    error to auto-close, not a period question — and a later revision says so
    explicitly.
    """
    line = _recon_line()
    index = index_ledger_entries([_ledger_entry(CAPTURE_DATE - timedelta(days=32))])

    case = _case((line,), match_tier=int(MatchTier.NO_MATCH))

    assert evaluate_policy(case, _no_evidence(), index) == ()


def test_a_case_with_a_firing_template_is_not_excluded() -> None:
    """The policy exclusions' conjunct: "posted to the **correct accounts** on the wrong date".

    A firing predicate means the accounts or amounts are wrong and a
    template restores them. This is the conjunct that stops the exclusion
    swallowing a family case that happens to straddle a month boundary.
    """
    line = _recon_line()
    index = index_ledger_entries([_ledger_entry(CAPTURE_DATE - timedelta(days=32))])
    evidence = CaseEvidence(
        case_id="setl_1",
        template_hits=(
            PredicateHit(
                case_id="setl_1",
                entity_id="pay_1",
                template_id=TemplateId.T01,
                cited_record_ids=("pay_1", "je_x"),
            ),
        ),
    )

    assert evaluate_policy(_case((line,)), evidence, index) == ()


def test_an_orphan_case_is_never_date_excluded() -> None:
    case = Case(case_id="case_orphan_1", kind=CaseKind.ORPHAN)

    assert evaluate_policy(case, CaseEvidence(case_id="case_orphan_1"), {}) == ()


def test_a_case_touching_neither_gate_is_left_on_the_auto_path() -> None:
    line = _recon_line()
    index = index_ledger_entries([_ledger_entry(CAPTURE_DATE)])

    assert evaluate_policy(_case((line,)), _no_evidence(), index) == ()


# --- The batch. ---


@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_exactly_the_two_policy_populations_are_excluded(seed: int) -> None:
    """The generator plan's table: 12 the policy exclusions tax positions and 5 family-4 date-error cases,
    both `REVIEW_REQUIRED`/`policy`, and 17 `REVIEW_REQUIRED` cases in total."""
    batch = generate_reference_batch(random.Random(seed), SNAPSHOT)
    cases = match_cases(
        assemble_cases(batch.settlements, batch.recon_lines, batch.bank_lines),
        batch.bank_lines,
        snapshot_date=SNAPSHOT,
    )
    evidences = {e.case_id: e for e in evaluate_cases(cases, batch.ledger_entries)}
    index = index_ledger_entries(batch.ledger_entries)

    by_population: dict[PolicyExclusion, Counter[str]] = {
        PolicyExclusion.TAX_POSITION: Counter(),
        PolicyExclusion.DATE_ONLY_RECLASSIFICATION: Counter(),
    }
    all_decisions = []
    for case in cases:
        decisions = evaluate_policy(case, evidences[case.case_id], index)
        all_decisions.extend(decisions)
        for decision in decisions:
            by_population[decision.exclusion][batch.population_of.get(case.case_id, "orphan")] += 1

    assert dict(by_population[PolicyExclusion.TAX_POSITION]) == {"fr06_tax": 12}
    assert dict(by_population[PolicyExclusion.DATE_ONLY_RECLASSIFICATION]) == {"family_4_date_error": 5}
    assert policy_exclusion_distribution(all_decisions) == {
        "tax_position": 12,
        "date_only_reclassification": 5,
    }
    assert len({decision.case_id for decision in all_decisions}) == 17


def test_no_auto_closable_family_case_is_policy_excluded() -> None:
    """The five the anomaly families families must reach the auto path untouched — an
    over-firing gate would show up only as depressed `auto_close_recall`."""
    batch = generate_reference_batch(random.Random(0), SNAPSHOT)
    cases = match_cases(
        assemble_cases(batch.settlements, batch.recon_lines, batch.bank_lines),
        batch.bank_lines,
        snapshot_date=SNAPSHOT,
    )
    evidences = {e.case_id: e for e in evaluate_cases(cases, batch.ledger_entries)}
    index = index_ledger_entries(batch.ledger_entries)
    candidates = {c.case_id for c in instantiate_cases(list(evidences.values()), cases, batch.ledger_entries)}

    families = {f"family_{n}" for n in range(1, 6)}
    for case in cases:
        if batch.population_of.get(case.case_id) not in families:
            continue
        assert case.case_id in candidates, f"{case.case_id} lost its candidate"
        assert evaluate_policy(case, evidences[case.case_id], index) == () , case.case_id
