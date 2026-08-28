"""Session 5.1's checkpoint (spec.md §6.3):

> Baseline classifies all ~70 non-auto-close cases without crashing.

The checkpoint itself is `test_baseline_classifies_every_non_auto_close_case`.
Around it: that the baseline's two easy branches — adopting an already-fired
trigger, and reading `AMBIGUOUS_CASE` off an opaque narration — actually agree
with ground truth on the populations built for exactly that purpose, and unit
coverage of the keyword read in isolation, where a mismatch is cheapest to
diagnose.
"""

from __future__ import annotations

import random
from datetime import date

import pytest

from generator.cli import generate_reference_batch
from pipeline.classifier import (
    ClassificationSource,
    SubtypeLabel,
    build_evidence_bundles,
    classification_distribution,
    classify_batch_baseline,
    non_auto_close_case_ids,
)
from pipeline.ground_truth import ExceptionSubtype, OutcomeState
from pipeline.run import run_batch
from pipeline.storage import connect

SNAPSHOT = date(2026, 8, 28)


def _run(seed: int = 0):
    batch = generate_reference_batch(random.Random(seed), SNAPSHOT)
    conn = connect(":memory:")
    result = run_batch(
        conn,
        settlements=batch.settlements,
        recon_lines=batch.recon_lines,
        bank_lines=batch.bank_lines,
        ledger_entries=batch.ledger_entries,
        snapshot_date=SNAPSHOT,
    )
    return batch, result


def _classify(seed: int = 0):
    batch, result = _run(seed)
    bundles = build_evidence_bundles(result.cases, result.evidences, result.outcome.outcomes)
    results = classify_batch_baseline(bundles)
    return batch, result, {r.case_id: r for r in results}


def _ground_truth_by_case_id(batch):
    return {case.case_id: case for case in batch.ground_truth}


def _case_ids_for_population(batch, result, population: str) -> list[str]:
    """`batch.population_of` is keyed by *record* id (settlement, recon line, ledger
    leg, bank line), not by case id — a settlement-anchored population's records
    include every leg of every payment, and an orphan population's cases are keyed
    by a synthesized `case_orphan_...` id that appears in no population map at all.
    Resolve through each case's own record ids instead: `case_id` itself for a
    settlement-anchored case (it *is* the settlement's record id), or any of its
    bank lines' `line_id`s for an orphan case.
    """
    record_ids = {rid for rid, name in batch.population_of.items() if name == population}
    return [
        case.case_id
        for case in result.cases
        if case.case_id in record_ids or any(line.line_id in record_ids for line in case.bank_lines)
    ]


# --- The checkpoint. ---


def test_baseline_classifies_every_non_auto_close_case_without_crashing() -> None:
    _, result, results_by_case = _classify()

    eligible = non_auto_close_case_ids(result.outcome.outcomes)

    assert len(eligible) == 70, "§3.6's batch totals: 150 - 30 AUTO_MATCHED - 50 AUTO_CLOSED = 70"
    assert set(results_by_case) == eligible
    for classification in results_by_case.values():
        assert isinstance(classification.subtype, SubtypeLabel)


def test_evidence_bundles_exclude_auto_matched_and_auto_closed_cases() -> None:
    _, result = _run()
    outcomes = result.outcome.outcomes
    auto_states = {OutcomeState.AUTO_MATCHED, OutcomeState.AUTO_CLOSED}
    auto_case_ids = {o.case_id for o in outcomes if o.state in auto_states}

    bundles = build_evidence_bundles(result.cases, result.evidences, outcomes)

    assert len(bundles) == 70
    assert {b.case_id for b in bundles}.isdisjoint(auto_case_ids)


# --- Branch 1: adopting an already-fired trigger. ---


@pytest.mark.parametrize(
    ("population", "expected_subtype"),
    [
        ("settlement_utr_missing", ExceptionSubtype.SETTLEMENT_UTR_MISSING),
        ("bank_credit_overdue", ExceptionSubtype.BANK_CREDIT_OVERDUE),
        ("settlement_amount_mismatch", ExceptionSubtype.SETTLEMENT_AMOUNT_MISMATCH),
        ("dispute_pending", ExceptionSubtype.DISPUTE_PENDING),
        ("reversal_unmatched", ExceptionSubtype.REVERSAL_UNMATCHED),
        ("duplicate_credit", ExceptionSubtype.DUPLICATE_CREDIT),
    ],
)
def test_baseline_adopts_the_fired_trigger_for_deterministic_populations(
    population: str, expected_subtype: ExceptionSubtype
) -> None:
    batch, run_result, results_by_case = _classify()
    case_ids = _case_ids_for_population(batch, run_result, population)

    assert case_ids, f"no cases found for population {population!r}"
    for case_id in case_ids:
        classification = results_by_case[case_id]
        assert classification.subtype.value == expected_subtype.value, case_id
        assert classification.source is ClassificationSource.DETERMINISTIC_TRIGGER, case_id


# --- Branch 2: the narration read, on the two populations it exists to split. ---


def test_baseline_reads_the_named_counterparty_on_unmatched_inbound_credit() -> None:
    batch, run_result, results_by_case = _classify()
    case_ids = _case_ids_for_population(batch, run_result, "unmatched_inbound_credit")

    assert len(case_ids) == 8
    for case_id in case_ids:
        classification = results_by_case[case_id]
        assert classification.subtype is SubtypeLabel.UNMATCHED_INBOUND_CREDIT, case_id
        assert classification.source is ClassificationSource.KEYWORD_BASELINE, case_id
        assert classification.matched_keyword, case_id


def test_baseline_falls_through_to_ambiguous_on_opaque_orphan_narration() -> None:
    batch, run_result, results_by_case = _classify()
    case_ids = _case_ids_for_population(batch, run_result, "ambiguous_orphan")

    assert len(case_ids) == 8
    for case_id in case_ids:
        classification = results_by_case[case_id]
        assert classification.subtype is SubtypeLabel.AMBIGUOUS_CASE, case_id
        assert classification.source is ClassificationSource.KEYWORD_BASELINE, case_id
        assert classification.matched_keyword is None, case_id


def test_baseline_falls_through_to_ambiguous_on_settlement_anchored_ambiguous_cases() -> None:
    """The 9 settlement-anchored `ambiguous` cases (§3.5): no trigger fires, and the
    case is not an orphan, so branch 2 never applies — only branch 3's fallthrough can
    reach `AMBIGUOUS_CASE` here, and it is the ground-truth-correct answer for this
    population specifically (unlike the 17 REVIEW_REQUIRED cases documented in the
    module docstring, whose ground truth is outside Slot A's vocabulary entirely)."""
    batch, run_result, results_by_case = _classify()
    case_ids = _case_ids_for_population(batch, run_result, "ambiguous")

    assert len(case_ids) == 9
    for case_id in case_ids:
        classification = results_by_case[case_id]
        assert classification.subtype is SubtypeLabel.AMBIGUOUS_CASE, case_id


def test_baseline_cannot_get_policy_excluded_corrections_right_by_construction() -> None:
    """Documents the known gap rather than asserting a correct answer that does not
    exist: family-4 date-error and FR-06 tax cases are `ACCOUNTING_CORRECTION` in
    ground truth, a class Slot A's eight-value enum has no member for."""
    batch, run_result, results_by_case = _classify()
    ground_truth = _ground_truth_by_case_id(batch)

    for population in ("family_4_date_error", "fr06_tax"):
        for case_id in _case_ids_for_population(batch, run_result, population):
            assert ground_truth[case_id].ground_truth_exception_subtype in (
                ExceptionSubtype.OMISSION,
                ExceptionSubtype.MISPOSTING,
            )
            # The baseline still returns a value from its fixed vocabulary rather
            # than crashing or returning nothing:
            assert isinstance(results_by_case[case_id].subtype, SubtypeLabel)


# --- Determinism, and the distribution helper. ---


def test_baseline_is_deterministic_given_the_same_seed() -> None:
    _, _, first = _classify(seed=0)
    _, _, second = _classify(seed=0)

    assert {cid: r.subtype for cid, r in first.items()} == {cid: r.subtype for cid, r in second.items()}
    assert {cid: r.matched_keyword for cid, r in first.items()} == {
        cid: r.matched_keyword for cid, r in second.items()
    }


def test_classification_distribution_sums_to_the_eligible_count() -> None:
    _, result, results_by_case = _classify()

    distribution = classification_distribution(list(results_by_case.values()))

    assert sum(distribution.values()) == len(results_by_case)
    assert set(distribution) <= {label.value for label in SubtypeLabel}


# --- The keyword read, in isolation. ---


@pytest.mark.parametrize(
    "narration",
    [
        "NEFT CR",
        "MISC CREDIT",
        "FUNDS TRANSFER",
        "TRANSFER IN",
        "BY TRANSFER",
        "CREDIT-MISC",
    ],
)
def test_opaque_narrations_identify_no_counterparty(narration: str) -> None:
    from pipeline.classifier import _identify_counterparty_token

    assert _identify_counterparty_token(narration) is None


@pytest.mark.parametrize(
    "narration",
    [
        "NEFT CR SHARMA ENTERPRISES ABCD1234EFGH5678",
        "BY NEFT BLUE OCEAN TRADERS ABCD1234EFGH5678",
        "RTGS-ABCD1234EFGH5678-APEX LOGISTICS PVT LTD-CR",
        "IMPS IN SHARMA ENTERPRISES",
    ],
)
def test_named_narrations_identify_a_counterparty(narration: str) -> None:
    from pipeline.classifier import _identify_counterparty_token

    assert _identify_counterparty_token(narration) is not None
