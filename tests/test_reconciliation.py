"""The books-versus-evidence residual §1.7.5's last check measures.

Two things are being pinned. First, that `expected_positions` is §3.2's
accrual model and not an approximation of it — the payment case is
checked against §3.2's own worked example, arithmetic and all. Second,
that the residual behaves the way §1.7.5 needs across the whole reference
batch: zero where the books are right, non-zero where they are not, and
driven to zero by exactly the corrections §3.4 instantiates.
"""

from __future__ import annotations

import random
from datetime import date

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
)
from pipeline.case_assembly import Case, CaseKind, assemble_cases
from pipeline.ground_truth import OutcomeState
from pipeline.instantiator import instantiate_cases
from pipeline.matcher import match_cases
from pipeline.predicates import evaluate_cases, index_ledger_entries
from pipeline.reconciliation import (
    ReconciliationError,
    apply_candidates,
    case_residual_paise,
    expected_positions,
    index_ledger_entries_by_case,
    orphan_residual_paise,
    residual_of,
)
from pipeline.schemas import BankLine, BankProfile, RazorpayEntityType, ReconLine

SNAPSHOT = date(2026, 8, 28)
RUPEE = 100


def _recon_line(**overrides: object) -> ReconLine:
    base: dict[str, object] = {
        "entity_id": "pay_1",
        "type": RazorpayEntityType.PAYMENT,
        "debit": 0,
        "credit": 0,
        "amount": 0,
        "fee": 0,
        "tax": 0,
        "settled": True,
        "settled_at": 1_756_000_000,
        "created_at": 1_755_900_000,
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


def _bank_line(deposit: int = 0, withdrawal: int = 0) -> BankLine:
    return BankLine(
        line_id="bank_1",
        value_date=SNAPSHOT,
        narration="NEFT CR SOMEONE",
        withdrawal_paise=withdrawal,
        deposit_paise=deposit,
        closing_balance_paise=0,
        bank_ref_no=None,
        bank_profile=BankProfile.HDFC,
    )


# --- §3.2's accrual model, transcribed and checked against its own example. ---


def test_payment_expected_position_matches_the_section_3_2_worked_example() -> None:
    """§3.2 family 3: "gross sale ₹1000, fee ₹20 (2% MDR), tax ₹3.60 ..., net credited ₹976.40".

    Its "Correct entry" line is `Dr Bank/Clearing 976.40, Dr Payment
    Gateway Charges 20, Dr GST on Gateway Charges 3.60 / Cr Sales Revenue
    1000` — which is what this function must produce, in paise.
    """
    line = _recon_line(amount=1000 * RUPEE, fee=20 * RUPEE, tax=360, credit=1000 * RUPEE)

    positions = expected_positions([line])

    assert positions[ACCOUNT_RAZORPAY_CLEARING.code] == 97_640
    assert positions[ACCOUNT_PAYMENT_GATEWAY_CHARGES.code] == 2_000
    assert positions[ACCOUNT_GST_ON_GATEWAY_CHARGES.code] == 360
    assert positions[ACCOUNT_SALES_REVENUE.code] == -100_000
    assert ACCOUNT_BANK_ACCOUNT.code not in positions


def test_refund_expected_position_is_family_2s_correct_posting() -> None:
    line = _recon_line(entity_id="rfnd_1", type=RazorpayEntityType.REFUND, debit=500 * RUPEE)

    positions = expected_positions([line])

    assert positions[ACCOUNT_SALES_RETURNS_AND_ALLOWANCES.code] == 50_000
    assert positions[ACCOUNT_RAZORPAY_CLEARING.code] == -50_000
    assert ACCOUNT_SALES_REVENUE.code not in positions, "§3.2 REV-15: family 2 never touches Sales Revenue"


@pytest.mark.parametrize(
    ("field", "clearing_sign"),
    [("credit", 1), ("debit", -1)],
)
def test_adjustment_expected_position_follows_its_direction(field: str, clearing_sign: int) -> None:
    """§3.2 family 5: a credit adjustment debits clearing, a debit adjustment credits it."""
    line = _recon_line(entity_id="adj_1", type=RazorpayEntityType.ADJUSTMENT, **{field: 250 * RUPEE})

    positions = expected_positions([line])

    assert positions[ACCOUNT_RAZORPAY_CLEARING.code] == clearing_sign * 25_000
    assert positions[ACCOUNT_RAZORPAY_SETTLEMENT_ADJUSTMENTS.code] == -clearing_sign * 25_000


def test_every_expected_position_is_internally_balanced() -> None:
    """Each §3.2 posting nets to zero across its accounts — which is why the residual is absolute."""
    lines = [
        _recon_line(amount=1000 * RUPEE, fee=20 * RUPEE, tax=360, credit=1000 * RUPEE),
        _recon_line(entity_id="rfnd_1", type=RazorpayEntityType.REFUND, debit=400 * RUPEE),
        _recon_line(entity_id="adj_1", type=RazorpayEntityType.ADJUSTMENT, credit=99 * RUPEE),
        _recon_line(entity_id="adj_2", type=RazorpayEntityType.ADJUSTMENT, debit=77 * RUPEE),
    ]

    for line in lines:
        assert sum(expected_positions([line]).values()) == 0, line.entity_id
    assert sum(expected_positions(lines).values()) == 0


def test_an_unsettled_line_contributes_nothing() -> None:
    line = _recon_line(amount=1000 * RUPEE, fee=20 * RUPEE, tax=360, settled=False)

    assert expected_positions([line]) == {}


def test_a_line_type_with_no_posting_rule_raises_rather_than_being_skipped() -> None:
    """A silently-skipped line would understate the residual, which is the one
    way §1.7.5's last check can read 0 for the wrong reason."""
    line = _recon_line(entity_id="trf_1", type=RazorpayEntityType.TRANSFER, credit=100 * RUPEE)

    with pytest.raises(ReconciliationError, match="no §3.2 posting rule"):
        expected_positions([line])


# --- The residual itself. ---


def test_residual_is_zero_only_when_every_account_agrees() -> None:
    expected = {"1020": 100, "4010": -100}

    assert residual_of(expected, {"1020": 100, "4010": -100}) == 0
    assert residual_of(expected, {"1020": 90, "4010": -100}) == 10
    assert residual_of(expected, {}) == 200


def test_the_signed_difference_would_measure_nothing() -> None:
    """Both sides balance, so a signed total is identically 0 however wrong the books are.

    This is the reason `residual_of` sums absolute per-account differences,
    pinned as a property rather than left in a comment.
    """
    expected = expected_positions([_recon_line(amount=1000 * RUPEE, fee=20 * RUPEE, tax=360)])
    badly_wrong = {ACCOUNT_RAZORPAY_CLEARING.code: 100_000, ACCOUNT_SALES_REVENUE.code: -100_000}

    signed = sum(expected.get(code, 0) - badly_wrong.get(code, 0) for code in set(expected) | set(badly_wrong))
    assert signed == 0
    assert residual_of(expected, badly_wrong) > 0


def test_apply_candidates_projects_without_writing() -> None:
    positions = {ACCOUNT_RAZORPAY_CLEARING.code: 100_000}

    projected = apply_candidates(positions, ())

    assert projected == positions
    assert projected is not positions, "must not mutate the caller's positions"


def test_orphan_residual_is_the_whole_unexplained_movement() -> None:
    case = Case(case_id="case_orphan_1", kind=CaseKind.ORPHAN, bank_lines=(_bank_line(deposit=12_345),))

    assert orphan_residual_paise(case) == 12_345
    assert case_residual_paise(case, {}, {}) == 12_345


# --- Against the reference batch. ---


def _batch_state(seed: int):
    batch = generate_reference_batch(random.Random(seed), SNAPSHOT)
    cases = match_cases(
        assemble_cases(batch.settlements, batch.recon_lines, batch.bank_lines),
        batch.bank_lines,
        snapshot_date=SNAPSHOT,
    )
    evidences = evaluate_cases(cases, batch.ledger_entries)
    candidates = instantiate_cases(evidences, cases, batch.ledger_entries)
    by_reference = index_ledger_entries(batch.ledger_entries)
    by_case = index_ledger_entries_by_case(batch.ledger_entries)
    return batch, cases, candidates, by_reference, by_case


def test_residual_is_zero_on_every_ground_truth_auto_matched_case() -> None:
    """The 30 cases §3.6's batch totals put in `AUTO_MATCHED` are exactly the
    cases whose books already agree with Razorpay's evidence."""
    batch, cases, _, by_reference, by_case = _batch_state(seed=0)
    ground_truth = {row.case_id: row for row in batch.ground_truth}

    checked = 0
    for case in cases:
        if case.kind is not CaseKind.SETTLEMENT_ANCHORED:
            continue
        expected_state = ground_truth[case.case_id].expected_outcome_state
        if expected_state is not OutcomeState.AUTO_MATCHED:
            continue
        checked += 1
        assert case_residual_paise(case, by_reference, by_case) == 0, case.case_id

    assert checked == 30


def test_every_correction_drives_its_cases_residual_to_zero() -> None:
    """§1.7.5's last check, stated as the property the whole session rests on.

    Every case carrying a candidate JV starts non-zero and lands on
    exactly 0 once its candidates are applied — family 4 included, whose
    bank credit has not landed and whose *matcher* residual therefore
    cannot move.
    """
    _, cases, candidates, by_reference, by_case = _batch_state(seed=0)
    by_case_id = {case.case_id: case for case in cases}

    grouped: dict[str, list] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.case_id, []).append(candidate)

    assert len(grouped) == 62, "50 family cases plus the 12 FR-06 tax cases"

    for case_id, case_candidates in grouped.items():
        case = by_case_id[case_id]
        before = case_residual_paise(case, by_reference, by_case)
        after = case_residual_paise(case, by_reference, by_case, pending_candidates=case_candidates)
        assert before > 0, f"{case_id} carries a correction but its books already agree"
        assert after == 0, f"{case_id} residual {before} -> {after}, expected 0"


@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_the_residual_partitions_the_batch_the_same_way_across_seeds(seed: int) -> None:
    batch, cases, candidates, by_reference, by_case = _batch_state(seed)
    with_candidates = {candidate.case_id for candidate in candidates}

    non_zero = {
        case.case_id
        for case in cases
        if case.kind is CaseKind.SETTLEMENT_ANCHORED and case_residual_paise(case, by_reference, by_case) > 0
    }

    # The nine ambiguous cases are the only settlement-anchored population with a
    # non-zero residual that no template explains — which is what makes ABSTAINED
    # reachable from evidence rather than by accident.
    unexplained = non_zero - with_candidates
    populations = {batch.population_of[case_id] for case_id in unexplained}
    assert populations == {"ambiguous"}
    assert len(unexplained) == 9
