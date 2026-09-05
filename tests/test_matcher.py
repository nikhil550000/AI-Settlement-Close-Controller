""" checkpoint: "Matches at more than one tier; 30
cases reach `AUTO_MATCHED`" — plus targeted unit coverage of each tier in
`pipeline/matcher.py`'s match cascade cascade.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timezone

from generator.cli import generate_reference_batch
from pipeline.case_assembly import Case, CaseKind, assemble_cases
from pipeline.ground_truth import OutcomeState
from pipeline.matcher import MatchTier, match_cases, match_settlement_anchored_case, match_tier_distribution
from pipeline.money import Paise
from pipeline.schemas import BankLine, BankProfile, Settlement, SettlementStatus

SNAPSHOT = date(2026, 8, 28)


def _settlement(*, utr: str = "AXIS0001202608280001", amount: int = 1000_00, created_date: date = SNAPSHOT) -> Settlement:
    created_at = int(datetime(created_date.year, created_date.month, created_date.day, tzinfo=timezone.utc).timestamp())
    return Settlement(
        id="setl_test0001",
        amount=Paise(amount),
        status=SettlementStatus.PROCESSED,
        fees=Paise(0),
        tax=Paise(0),
        utr=utr,
        created_at=created_at,
    )


def _line(
    line_id: str,
    *,
    narration: str,
    deposit: int = 0,
    bank_ref_no: str | None = None,
    value_date: date = SNAPSHOT,
) -> BankLine:
    return BankLine(
        line_id=line_id,
        value_date=value_date,
        narration=narration,
        bank_ref_no=bank_ref_no,
        withdrawal_paise=Paise(0),
        deposit_paise=Paise(deposit),
        closing_balance_paise=Paise(10_000_00),
        bank_profile=BankProfile.HDFC,
    )


def _case(settlement: Settlement) -> Case:
    return Case(case_id=settlement.id, kind=CaseKind.SETTLEMENT_ANCHORED, settlement=settlement)


# --- Tier 0: clean/whitespace-delimited UTR, or bank_ref_no equality ---


def test_tier0_matches_clean_whitespace_delimited_utr():
    settlement = _settlement(utr="AXIS0001202608280001")
    line = _line("bank_1", narration="NEFT CR RAZORPAY SOFTWARE PVT LTD AXIS0001202608280001", deposit=1000_00)

    result = match_settlement_anchored_case(_case(settlement), [line], snapshot_date=SNAPSHOT)

    assert result.match_tier == MatchTier.UTR_EXACT
    assert [l.line_id for l in result.bank_lines] == ["bank_1"]
    assert result.residual_paise == 0


def test_tier0_matches_via_bank_ref_no():
    settlement = _settlement(utr="AXIS0001202608280001")
    line = _line("bank_1", narration="NEFT CR RAZORPAY SOFTWARE", bank_ref_no="axis0001202608280001", deposit=1000_00)

    result = match_settlement_anchored_case(_case(settlement), [line], snapshot_date=SNAPSHOT)

    assert result.match_tier == MatchTier.UTR_EXACT


def test_tier0_does_not_match_embedded_utr_glued_to_punctuation():
    """An embedded UTR is never its own whitespace-delimited word — tier 0 must not catch it."""
    settlement = _settlement(utr="AXIS0001202608280001")
    line = _line("bank_1", narration="NEFT-CR-RAZORPAY SOFTWARE-AXIS0001202608280001", deposit=1000_00)

    result = match_settlement_anchored_case(_case(settlement), [line], snapshot_date=SNAPSHOT)

    assert result.match_tier != MatchTier.UTR_EXACT


# --- Tier 1: embedded or truncated alphanumeric run, prefix of the UTR ---


def test_tier1_matches_embedded_full_utr():
    settlement = _settlement(utr="AXIS0001202608280001")
    line = _line("bank_1", narration="NEFT-CR-RAZORPAY SOFTWARE-AXIS0001202608280001", deposit=1000_00)

    result = match_settlement_anchored_case(_case(settlement), [line], snapshot_date=SNAPSHOT)

    assert result.match_tier == MatchTier.UTR_PREFIX
    assert result.residual_paise == 0


def test_tier1_matches_truncated_prefix():
    settlement = _settlement(utr="AXIS0001202608280001")
    line = _line("bank_1", narration="NEFT CR RAZORPAY SOFTWARE AXIS00012026", deposit=1000_00)  # 12-char prefix

    result = match_settlement_anchored_case(_case(settlement), [line], snapshot_date=SNAPSHOT)

    assert result.match_tier == MatchTier.UTR_PREFIX


def test_tier1_rejects_token_shorter_than_eight():
    settlement = _settlement(utr="AXIS0001202608280001")
    line = _line("bank_1", narration="NEFT CR RAZORPAY SOFTWARE AXIS000", deposit=1000_00)  # 7-char prefix

    result = match_settlement_anchored_case(_case(settlement), [line], snapshot_date=SNAPSHOT)

    assert result.match_tier != MatchTier.UTR_PREFIX


def test_tier1_does_not_match_a_prefix_of_a_different_utr():
    """Deposit deliberately differs from the settlement amount so a false tier-1 hit
    can't be masked by an incidental tier-2 amount+window match."""
    settlement = _settlement(utr="AXIS0001202608280001", amount=1000_00)
    line = _line("bank_1", narration="NEFT CR RAZORPAY SOFTWARE HDFC0009999999999", deposit=1234_00)

    result = match_settlement_anchored_case(_case(settlement), [line], snapshot_date=SNAPSHOT)

    assert result.match_tier == MatchTier.NO_MATCH


# --- Tier 2: amount + window, unique candidate only ---


def test_tier2_matches_absent_utr_on_amount_and_window():
    settlement = _settlement(utr="", amount=1500_00, created_date=SNAPSHOT)
    line = _line("bank_1", narration="NEFT CR RAZORPAY SOFTWARE PVT LTD", deposit=1500_00)  # no UTR anywhere

    result = match_settlement_anchored_case(_case(settlement), [line], snapshot_date=SNAPSHOT)

    assert result.match_tier == MatchTier.AMOUNT_AND_WINDOW
    assert result.residual_paise == 0


def test_tier2_rejects_a_tie_between_two_equal_amount_candidates():
    settlement = _settlement(utr="", amount=1500_00, created_date=SNAPSHOT)
    lines = [
        _line("bank_1", narration="NEFT CR SOMEONE", deposit=1500_00),
        _line("bank_2", narration="NEFT CR SOMEONE ELSE", deposit=1500_00),
    ]

    result = match_settlement_anchored_case(_case(settlement), lines, snapshot_date=SNAPSHOT)

    assert result.match_tier == MatchTier.NO_MATCH


def test_tier2_rejects_a_candidate_outside_the_window_plus_slack():
    from datetime import timedelta

    created = date(2026, 8, 24)  # Monday
    settlement = _settlement(utr="", amount=1500_00, created_date=created)
    # T+2 working days from Monday = Wednesday 8/26; +1 slack day = Thursday 8/27. Friday 8/28 is one day past.
    late_line = _line("bank_1", narration="NEFT CR SOMEONE", deposit=1500_00, value_date=created + timedelta(days=4))

    result = match_settlement_anchored_case(_case(settlement), [late_line], snapshot_date=SNAPSHOT)

    assert result.match_tier == MatchTier.NO_MATCH


# --- Tier 3: no match, timing-residual rule ---


def test_tier3_inside_window_is_zero_residual_expected_timing_difference():
    created = date(2026, 8, 27)  # Thursday; T+2 working days -> Monday 8/31, snapshot 8/28 is inside
    settlement = _settlement(utr="AXIS0001202608280001", amount=1000_00, created_date=created)

    result = match_settlement_anchored_case(_case(settlement), [], snapshot_date=SNAPSHOT)

    assert result.match_tier == MatchTier.NO_MATCH
    assert result.in_settlement_window is True
    assert result.residual_paise == 0


def test_tier3_past_window_is_full_residual_bank_credit_overdue():
    created = date(2026, 8, 17)  # Monday, well past T+2 by the 8/28 snapshot
    settlement = _settlement(utr="AXIS0001202608280001", amount=1000_00, created_date=created)

    result = match_settlement_anchored_case(_case(settlement), [], snapshot_date=SNAPSHOT)

    assert result.match_tier == MatchTier.NO_MATCH
    assert result.in_settlement_window is False
    assert result.residual_paise == 1000_00


def test_orphan_case_passes_through_untouched():
    orphan = Case(case_id="case_orphan_bank_1", kind=CaseKind.ORPHAN, bank_lines=(_line("bank_1", narration="x", deposit=100),))
    result = match_settlement_anchored_case(orphan, [], snapshot_date=SNAPSHOT)
    assert result is orphan


# --- The session checkpoint itself, against the full reference batch and its ground truth. ---


def test_matches_at_more_than_one_tier_and_thirty_cases_reach_auto_matched():
    rng = random.Random(0)
    batch = generate_reference_batch(rng, SNAPSHOT)

    cases = assemble_cases(batch.settlements, batch.recon_lines, batch.bank_lines)
    matched = match_cases(cases, batch.bank_lines, snapshot_date=SNAPSHOT)

    distribution = match_tier_distribution(matched)
    tiers_hit = {tier for tier, count in distribution.items() if count > 0}
    assert len(tiers_hit) > 1, f"expected matches at more than one tier, got {distribution}"
    assert {0, 1, 2}.issubset(tiers_hit), f"expected tiers 0, 1 and 2 all populated, got {distribution}"

    ground_truth_by_id = {gt.case_id: gt for gt in batch.ground_truth}
    auto_matched_case_ids = {
        case_id for case_id, gt in ground_truth_by_id.items() if gt.expected_outcome_state is OutcomeState.AUTO_MATCHED
    }
    assert len(auto_matched_case_ids) == 30

    matched_by_id = {case.case_id: case for case in matched}
    for case_id in auto_matched_case_ids:
        case = matched_by_id[case_id]
        assert case.residual_paise == 0, f"{case_id}: expected zero residual for a ground-truth AUTO_MATCHED case"


def test_matcher_checkpoint_holds_across_multiple_seeds():
    for seed in range(1, 4):
        rng = random.Random(seed)
        batch = generate_reference_batch(rng, SNAPSHOT)
        cases = assemble_cases(batch.settlements, batch.recon_lines, batch.bank_lines)
        matched = match_cases(cases, batch.bank_lines, snapshot_date=SNAPSHOT)

        ground_truth_by_id = {gt.case_id: gt for gt in batch.ground_truth}
        matched_by_id = {case.case_id: case for case in matched}
        for case_id, gt in ground_truth_by_id.items():
            if gt.expected_outcome_state is OutcomeState.AUTO_MATCHED and case_id in matched_by_id:
                assert matched_by_id[case_id].residual_paise == 0, f"seed={seed} case={case_id}"
