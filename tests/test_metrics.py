"""Session 6.1's checkpoint (spec.md §6.3):

> Denominators hand-checked against §3.6's batch totals.

The checkpoint test is `test_every_denominator_hand_checks_against_the_batch_totals_table`.
It transcribes §3.6's batch-totals table and §3.5/§3.6's per-population
allocations as literals — typed out from the spec, never read back from the
generator — and asserts every §1.6 denominator in `MetricsReport` against
them one by one. A denominator computed from the same code that produced the
numbers it divides would be asserting nothing; §3.5's "Label emission" rule
makes the same point about labels, and it applies with more force to the
figures those labels are divided by.

`test_denominator_convention_holds_for_every_metric` is the second half of
the hand-check: §1.6's convention note (`*_rate` over total cases, `*_recall`
over the ground-truth population, `*_precision` over the predicted
population) asserted as a property of every named metric, so a future metric
added with the wrong denominator fails here rather than in a report.

Around those: unit coverage of `Rate`'s undefined case, the macro average's
seven-subtype denominator (§5.2, REV-25), `auto_applied_entries`' inclusion
of replayed entries, `count_matching_entries`' multiset semantics,
`case_value_paise`'s two anchor kinds, `_aligned`'s batch-mismatch guards,
and determinism of the whole report across two runs.

Everything is offline. The classifier used throughout is session 5.1's
deterministic keyword baseline, so no test here needs a network, a key, or a
cache file (NFR-05) — the same discipline `tests/test_llm_slot_a.py` and
`tests/test_llm_slot_b.py` follow.
"""

from __future__ import annotations

import random
from datetime import date

import pytest

from generator.cli import generate_reference_batch
from pipeline.apply import CaseOutcome
from pipeline.case_assembly import Case, CaseKind
from pipeline.classifier import classify_batch_baseline
from pipeline.ground_truth import (
    DeclineReason,
    ExceptionClass,
    ExceptionSubtype,
    ExpectedJournalEntry,
    ExpectedJournalLeg,
    GroundTruthCase,
    OutcomeState,
)
from pipeline.instantiator import CandidateJournalEntry, CandidateJournalLeg
from pipeline.metrics import (
    GRADED_SUBTYPES,
    MetricsError,
    RunProvenance,
    align_ground_truth,
    auto_applied_entries,
    case_value_paise,
    compute_metrics,
    count_matching_entries,
    ground_truth_subtype,
    performance_metrics,
    rate,
)
from pipeline.run import run_batch
from pipeline.schemas import BankLine, BankProfile, Settlement, SettlementStatus
from pipeline.storage import connect
from pipeline.subtype_label import SubtypeLabel

SNAPSHOT = date(2026, 8, 28)


# --- §3.6's batch totals and §3.5/§3.6's populations, transcribed from the spec. ---

TOTAL_CASES = 150
"""§3.6 Batch totals, and §2.2's FR-01 `Reconciliation cases` row (125 + 25)."""

GROUND_TRUTH_STATE_COUNTS: dict[str, int] = {
    "AUTO_MATCHED": 30,
    "AUTO_CLOSED": 50,
    "REVIEW_REQUIRED": 17,
    "EXTERNAL_ACTION_REQUIRED": 36,
    "ABSTAINED": 17,
}
"""§3.6's Batch-totals table, typed out. Shares 20.0 / 33.3 / 11.3 / 24.0 / 11.3%."""

GROUND_TRUTH_SUBTYPE_COUNTS: dict[SubtypeLabel, int] = {
    SubtypeLabel.SETTLEMENT_UTR_MISSING: 5,
    SubtypeLabel.BANK_CREDIT_OVERDUE: 5,
    SubtypeLabel.SETTLEMENT_AMOUNT_MISMATCH: 4,
    SubtypeLabel.UNMATCHED_INBOUND_CREDIT: 8,
    SubtypeLabel.REVERSAL_UNMATCHED: 6,
    SubtypeLabel.DUPLICATE_CREDIT: 3,
    SubtypeLabel.DISPUTE_PENDING: 5,
}
"""§3.5's four `OPERATIONAL_EXCEPTION` rows plus §3.6's three, in §3.3's subtype order.

These are `exception_subtype_recall`'s seven denominators, and their sum is
the `EXTERNAL_ACTION_REQUIRED` row above — the arithmetic REV-25 corrected:
seven subtypes divide 36 cases, six cannot.
"""

EXPECTED_AUTO_CLOSED_ENTRIES = 50
"""`auto_close_precision`'s denominator on a perfect run.

§3.5 allocates 10 cases to each of the five FR-04 families and §3.4 gives
each family exactly one template, so a run that auto-closes every case it
should posts exactly one entry per case. FR-05 is not built (§2.4's stated
fallback), so no `EXTERNAL_ACTION_REQUIRED` case carries a recognition entry
that REV-10's entry-level denominator would otherwise pick up.
"""


def _run(seed: int = 0, *, classifier=classify_batch_baseline):
    batch = generate_reference_batch(random.Random(seed), SNAPSHOT)
    conn = connect(":memory:")
    result = run_batch(
        conn,
        settlements=batch.settlements,
        recon_lines=batch.recon_lines,
        bank_lines=batch.bank_lines,
        ledger_entries=batch.ledger_entries,
        snapshot_date=SNAPSHOT,
        classifier=classifier,
    )
    return batch, result


def _metrics(seed: int = 0, *, classifier=classify_batch_baseline):
    batch, result = _run(seed, classifier=classifier)
    report = compute_metrics(result.cases, result.outcome.outcomes, batch.ground_truth)
    return batch, result, report


# --- The checkpoint. ---


def test_every_denominator_hand_checks_against_the_batch_totals_table() -> None:
    """spec.md §6.3, session 6.1: "Denominators hand-checked against §3.6's batch totals."

    Every denominator in the §1.6 surface, asserted against §3.6's table and
    §3.5/§3.6's population allocations transcribed above as literals.
    """
    _, _, report = _metrics()

    # The one denominator every `*_rate` shares (§1.6's convention note).
    assert report.total_cases == TOTAL_CASES
    for metric in (
        report.match_rate,
        report.false_match_rate,
        report.state_prediction_accuracy,
        report.declined_by_policy_rate,
        report.declined_by_confidence_rate,
        report.abstention_rate,
        report.deferred_to_human_rate,
        report.open_case_rate,
    ):
        assert metric.denominator == TOTAL_CASES

    # The ground-truth state distribution is §3.6's table exactly.
    assert report.ground_truth_state_counts == GROUND_TRUTH_STATE_COUNTS
    assert sum(GROUND_TRUTH_STATE_COUNTS.values()) == TOTAL_CASES

    # `*_recall` denominators are the ground-truth population for that state.
    assert report.auto_match_recall.denominator == GROUND_TRUTH_STATE_COUNTS["AUTO_MATCHED"] == 30
    assert report.auto_close_recall.denominator == GROUND_TRUTH_STATE_COUNTS["AUTO_CLOSED"] == 50

    # `*_precision` denominators are the population the system predicted.
    assert report.auto_match_precision.denominator == report.predicted_state_counts["AUTO_MATCHED"]
    assert report.auto_close_precision.denominator == report.auto_applied_entry_count

    # §5.2's per-subtype denominators, seven of each, visible.
    assert len(report.subtype_metrics) == 7
    assert tuple(metric.subtype for metric in report.subtype_metrics) == GRADED_SUBTYPES
    recall_denominators = {metric.subtype: metric.recall.denominator for metric in report.subtype_metrics}
    assert recall_denominators == GROUND_TRUTH_SUBTYPE_COUNTS
    assert sum(recall_denominators.values()) == GROUND_TRUTH_STATE_COUNTS["EXTERNAL_ACTION_REQUIRED"] == 36
    for metric in report.subtype_metrics:
        assert metric.precision.denominator == report.classification_counts[str(metric.subtype)]

    # REV-25: the macro is over seven subtypes, not six.
    assert report.exception_subtype_precision_macro.denominator == 7
    assert report.exception_subtype_recall_macro.denominator == 7

    # `value_coverage`'s denominator is every case's value, in integer paise —
    # checked against the cases themselves in
    # `test_value_coverage_is_integer_paise_on_both_sides`.
    assert isinstance(report.value_coverage.denominator, int)
    assert report.value_coverage.denominator > 0


def test_ground_truth_state_counts_are_recomputed_from_labels_not_asserted_counts() -> None:
    """The checkpoint's authority: the counts above come from §3.6, the counts
    checked come from the generator's own emitted labels, and they must agree.

    Recomputed here straight off `GroundTruthCase.expected_outcome_state`
    rather than through `compute_metrics`, so the hand-check does not depend
    on the module it is checking.
    """
    batch, _, report = _metrics()
    recomputed: dict[str, int] = {}
    for truth in batch.ground_truth:
        key = str(truth.expected_outcome_state)
        recomputed[key] = recomputed.get(key, 0) + 1
    assert recomputed == GROUND_TRUTH_STATE_COUNTS
    assert report.ground_truth_state_counts == recomputed


def test_ground_truth_subtype_populations_match_the_spec_tables() -> None:
    """§3.5's four `OPERATIONAL_EXCEPTION` rows plus §3.6's three, off the labels."""
    batch, _, _ = _metrics()
    counts: dict[SubtypeLabel, int] = {}
    for truth in batch.ground_truth:
        subtype = ground_truth_subtype(truth)
        if subtype is not None:
            counts[subtype] = counts.get(subtype, 0) + 1
    assert counts == GROUND_TRUTH_SUBTYPE_COUNTS


def test_denominator_convention_holds_for_every_metric() -> None:
    """§1.6's convention note, asserted as a property rather than read as prose.

    > Metrics named `*_rate` use **total cases** as the denominator. Metrics
    > named `*_recall` use the ground-truth population for that state.
    > Metrics named `*_precision` use the population the system *predicted*.
    """
    _, _, report = _metrics()
    predicted = report.predicted_state_counts
    expected = report.ground_truth_state_counts

    rate_metrics = {
        "match_rate": report.match_rate,
        "false_match_rate": report.false_match_rate,
        "declined_by_policy_rate": report.declined_by_policy_rate,
        "declined_by_confidence_rate": report.declined_by_confidence_rate,
        "abstention_rate": report.abstention_rate,
        "deferred_to_human_rate": report.deferred_to_human_rate,
        "open_case_rate": report.open_case_rate,
    }
    for name, metric in rate_metrics.items():
        assert metric.denominator == report.total_cases, name

    assert report.auto_match_recall.denominator == expected["AUTO_MATCHED"]
    assert report.auto_close_recall.denominator == expected["AUTO_CLOSED"]
    assert report.auto_match_precision.denominator == predicted["AUTO_MATCHED"]

    # `auto_close_precision` is the one deliberate exception, and REV-10 states it:
    # its denominator is auto-applied *entries*, not the predicted case population.
    assert report.auto_close_precision.denominator == report.auto_applied_entry_count


def test_false_match_rate_is_not_the_complement_of_auto_match_precision() -> None:
    """§1.6 says so outright, and it is only true because the denominators differ."""
    _, _, report = _metrics()
    assert report.false_match_rate.denominator == report.total_cases
    assert report.auto_match_precision.denominator == report.predicted_state_counts["AUTO_MATCHED"]
    assert report.false_match_rate.denominator != report.auto_match_precision.denominator
    # The two numerators are the same population, counted against different bases.
    assert (
        report.false_match_rate.numerator
        == report.auto_match_precision.denominator - report.auto_match_precision.numerator
    )


# --- The measured surface at seed 0, against §5.5's provisional targets. ---


def test_primary_safety_metrics_hold_at_seed_zero() -> None:
    """§5.5: `false_match_rate` target 0, `auto_close_precision` >= 0.98.

    Asserted rather than eyeballed. §5.5 calls every figure provisional and
    session 6.2 owns the threshold review; these two are the ones §1.6 names
    "primary safety metric", so a regression in either should fail a test.
    """
    _, _, report = _metrics()
    assert report.false_match_rate.value == 0.0
    assert report.auto_close_precision.value is not None
    assert report.auto_close_precision.value >= 0.98


def test_auto_close_precision_denominator_is_the_fifty_family_entries() -> None:
    """One entry per auto-closed case: five FR-04 families x 10 cases, one template each."""
    _, _, report = _metrics()
    assert report.auto_applied_entry_count == EXPECTED_AUTO_CLOSED_ENTRIES
    assert report.predicted_state_counts["AUTO_CLOSED"] == GROUND_TRUTH_STATE_COUNTS["AUTO_CLOSED"]


def test_declined_by_policy_rate_is_the_seventeen_policy_excluded_cases() -> None:
    """§5.5 expects ~11.3% "by construction, not by performance": 5 date-error + 12 FR-06."""
    _, _, report = _metrics()
    assert report.declined_by_policy_rate.numerator == 17
    assert report.declined_by_policy_rate.value == pytest.approx(17 / 150)


def test_declined_by_confidence_rate_has_no_ground_truth_population() -> None:
    """§1.6: every ground-truth `REVIEW_REQUIRED` case is a *policy* decline, so a
    confidence decline is always a case the system got wrong somewhere else."""
    _, result, report = _metrics()
    confidence_declines = [
        outcome for outcome in result.outcome.outcomes if outcome.decline_reason is DeclineReason.CONFIDENCE
    ]
    assert report.declined_by_confidence_rate.numerator == len(confidence_declines)
    assert report.declined_by_policy_rate.numerator + report.declined_by_confidence_rate.numerator == (
        report.predicted_state_counts["REVIEW_REQUIRED"]
    )


def test_deferral_and_open_case_rates_are_consistent_with_the_state_distribution() -> None:
    _, _, report = _metrics()
    predicted = report.predicted_state_counts
    assert report.abstention_rate.numerator == predicted["ABSTAINED"]
    assert report.deferred_to_human_rate.numerator == predicted["ABSTAINED"] + predicted["REVIEW_REQUIRED"]
    assert report.open_case_rate.numerator == report.total_cases - (
        predicted["AUTO_MATCHED"] + predicted["AUTO_CLOSED"]
    )


def test_abstention_rate_sits_inside_the_stated_operating_range() -> None:
    """§5.5: operating range 8-18%; ground truth is 11.3% (§3.6)."""
    _, _, report = _metrics()
    assert report.abstention_rate.value is not None
    assert 0.08 <= report.abstention_rate.value <= 0.18


def test_value_coverage_is_integer_paise_on_both_sides() -> None:
    """NFR-04: no float touches money. The ratio is derived; the two sides are ints."""
    batch, result, report = _metrics()
    assert isinstance(report.value_coverage.numerator, int)
    assert isinstance(report.value_coverage.denominator, int)
    assert report.value_coverage.denominator == sum(case_value_paise(case) for case in result.cases)
    assert 0 < report.value_coverage.numerator < report.value_coverage.denominator


def test_state_prediction_accuracy_counts_exact_state_agreement() -> None:
    batch, result, report = _metrics()
    aligned = align_ground_truth(result.cases, batch.ground_truth)
    agreements = sum(
        1
        for outcome in result.outcome.outcomes
        if outcome.state is aligned[outcome.case_id].expected_outcome_state
    )
    assert report.state_prediction_accuracy.numerator == agreements
    assert report.state_prediction_accuracy.denominator == TOTAL_CASES


def test_report_is_identical_across_two_runs_at_the_same_seed() -> None:
    """NFR-01, and the precondition for §5.6.3's byte-identical reproduce test."""
    _, _, first = _metrics()
    _, _, second = _metrics()
    assert first.model_dump_json() == second.model_dump_json()


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_denominators_hold_at_other_seeds(seed: int) -> None:
    """§3.5/§3.6's allocations are the generator's contract, not a seed-0 accident.

    Seed 1 is §5.1's development batch and seed 2 its held-out batch; the
    denominators must be identical on both, since only the records vary.
    """
    _, _, report = _metrics(seed)
    assert report.total_cases == TOTAL_CASES
    assert report.ground_truth_state_counts == GROUND_TRUTH_STATE_COUNTS
    assert {
        metric.subtype: metric.recall.denominator for metric in report.subtype_metrics
    } == GROUND_TRUTH_SUBTYPE_COUNTS


# --- Negative controls: the surface reads 1.0 at every seed, so prove it can move. ---


def test_a_false_match_moves_every_matching_metric_by_the_exact_expected_amount() -> None:
    """The whole §1.6 surface reads 1.0000 on this batch at seeds 0, 1 and 2. That is
    a believable result for a deterministic pipeline graded against the generator that
    produced its inputs (§5.3 says so outright), but it is indistinguishable from a
    metric hard-wired to 1.0 unless the metric is shown to move.

    So: take a real run, restate one ground-truth-`AUTO_CLOSED` case as `AUTO_MATCHED`,
    and assert each affected metric lands on its exact predicted numerator — including
    the two that must *not* move, and the two whose denominators differ so they move by
    different amounts from the same single error.
    """
    batch, result, clean = _metrics()
    aligned = align_ground_truth(result.cases, batch.ground_truth)
    victim = next(
        outcome
        for outcome in result.outcome.outcomes
        if aligned[outcome.case_id].expected_outcome_state is OutcomeState.AUTO_CLOSED
    )
    perturbed = [
        outcome.model_copy(update={"state": OutcomeState.AUTO_MATCHED})
        if outcome.case_id == victim.case_id
        else outcome
        for outcome in result.outcome.outcomes
    ]
    report = compute_metrics(result.cases, perturbed, batch.ground_truth)

    assert clean.false_match_rate.numerator == 0
    assert report.false_match_rate.numerator == 1
    assert report.false_match_rate.denominator == 150  # a *_rate: total cases
    assert report.match_rate.numerator == 31
    assert report.auto_match_precision.numerator == 30
    assert report.auto_match_precision.denominator == 31  # a *_precision: predicted
    assert report.auto_match_recall.numerator == 30
    assert report.auto_match_recall.denominator == 30  # a *_recall: ground truth, unmoved
    assert report.auto_close_recall.numerator == 49
    assert report.auto_close_recall.denominator == 50
    assert report.state_prediction_accuracy.numerator == 149
    # The entry was still applied, so entry-level precision is untouched by a state flip.
    assert report.auto_close_precision.numerator == clean.auto_close_precision.numerator == 50


def test_a_wrong_journal_moves_auto_close_precision_but_not_auto_close_recall() -> None:
    """§1.6's stated reason for an entry-level denominator: "a correct match followed
    by an incorrect journal is a different failure mode"."""
    batch, result, clean = _metrics()
    victim = next(o for o in result.outcome.outcomes if auto_applied_entries(o))
    entries = auto_applied_entries(victim)
    corrupted = entries[0].model_copy(
        update={
            "legs": tuple(
                leg.model_copy(update={"debit": leg.debit + 1 if leg.debit else 0})
                for leg in entries[0].legs
            )
        }
    )
    perturbed = [
        o.model_copy(update={"applied_entries": (corrupted,) + entries[1:], "replayed_entries": ()})
        if o.case_id == victim.case_id
        else o
        for o in result.outcome.outcomes
    ]
    report = compute_metrics(result.cases, perturbed, batch.ground_truth)

    assert clean.auto_close_precision.numerator == 50
    assert report.auto_close_precision.numerator == 49
    assert report.auto_close_precision.denominator == 50
    assert report.auto_close_recall.numerator == clean.auto_close_recall.numerator == 50
    assert report.state_prediction_accuracy.numerator == 150


def test_a_misclassified_subtype_moves_one_precision_and_one_recall() -> None:
    """§5.2's asymmetry, checked: assigning S to a case whose truth is T costs T's
    recall and S's precision, and leaves the other five subtypes alone."""
    batch, result, clean = _metrics()
    aligned = align_ground_truth(result.cases, batch.ground_truth)
    victim = next(
        o
        for o in result.outcome.outcomes
        if aligned[o.case_id].ground_truth_exception_subtype is ExceptionSubtype.DUPLICATE_CREDIT
    )
    perturbed = [
        o.model_copy(update={"classified_subtype": SubtypeLabel.REVERSAL_UNMATCHED})
        if o.case_id == victim.case_id
        else o
        for o in result.outcome.outcomes
    ]
    report = compute_metrics(result.cases, perturbed, batch.ground_truth)
    moved = {m.subtype: m for m in report.subtype_metrics}
    before = {m.subtype: m for m in clean.subtype_metrics}

    assert moved[SubtypeLabel.DUPLICATE_CREDIT].recall.numerator == 2
    assert moved[SubtypeLabel.DUPLICATE_CREDIT].recall.denominator == 3  # ground-truth, fixed
    assert moved[SubtypeLabel.DUPLICATE_CREDIT].precision.denominator == 2  # predicted, shrinks
    assert moved[SubtypeLabel.REVERSAL_UNMATCHED].precision.numerator == 6
    assert moved[SubtypeLabel.REVERSAL_UNMATCHED].precision.denominator == 7
    assert moved[SubtypeLabel.REVERSAL_UNMATCHED].recall == before[SubtypeLabel.REVERSAL_UNMATCHED].recall
    for subtype in GRADED_SUBTYPES:
        if subtype not in (SubtypeLabel.DUPLICATE_CREDIT, SubtypeLabel.REVERSAL_UNMATCHED):
            assert moved[subtype] == before[subtype]
    assert report.exception_subtype_recall_macro.value is not None
    assert report.exception_subtype_recall_macro.value < 1.0
    assert report.exception_subtype_recall_macro.denominator == 7


def test_an_unclassified_subtype_costs_recall_without_costing_any_precision() -> None:
    """The asymmetry in the other direction: withholding a label is a recall miss and
    nothing else. A metric that penalised both would be double-counting one error."""
    batch, result, clean = _metrics()
    aligned = align_ground_truth(result.cases, batch.ground_truth)
    victim = next(
        o
        for o in result.outcome.outcomes
        if aligned[o.case_id].ground_truth_exception_subtype is ExceptionSubtype.DISPUTE_PENDING
    )
    perturbed = [
        o.model_copy(update={"classified_subtype": None}) if o.case_id == victim.case_id else o
        for o in result.outcome.outcomes
    ]
    report = compute_metrics(result.cases, perturbed, batch.ground_truth)
    moved = {m.subtype: m for m in report.subtype_metrics}

    assert moved[SubtypeLabel.DISPUTE_PENDING].recall.numerator == 4
    assert moved[SubtypeLabel.DISPUTE_PENDING].recall.denominator == 5
    assert moved[SubtypeLabel.DISPUTE_PENDING].precision.numerator == 4
    assert moved[SubtypeLabel.DISPUTE_PENDING].precision.denominator == 4
    assert moved[SubtypeLabel.DISPUTE_PENDING].precision.value == 1.0
    assert clean.exception_subtype_precision_macro.value == 1.0
    assert report.exception_subtype_precision_macro.value == 1.0


def test_a_subtype_the_system_never_assigns_leaves_the_macro_over_fewer_subtypes() -> None:
    """§5.2's visible-denominator rule at the macro level: dropping every
    `DUPLICATE_CREDIT` prediction makes that precision undefined, and the macro says
    it averaged six of seven rather than quietly scoring the seventh as 0."""
    batch, result, _ = _metrics()
    perturbed = [
        o.model_copy(update={"classified_subtype": None})
        if o.classified_subtype is SubtypeLabel.DUPLICATE_CREDIT
        else o
        for o in result.outcome.outcomes
    ]
    report = compute_metrics(result.cases, perturbed, batch.ground_truth)
    duplicate = next(m for m in report.subtype_metrics if m.subtype is SubtypeLabel.DUPLICATE_CREDIT)

    assert duplicate.precision.denominator == 0
    assert duplicate.precision.value is None
    assert duplicate.recall.numerator == 0
    assert duplicate.recall.denominator == 3
    assert duplicate.recall.value == 0.0
    assert report.exception_subtype_precision_macro.numerator == 6
    assert report.exception_subtype_precision_macro.denominator == 7
    assert report.exception_subtype_precision_macro.value == 1.0
    assert report.exception_subtype_recall_macro.numerator == 7
    assert report.exception_subtype_recall_macro.value == pytest.approx(6 / 7)


def test_value_coverage_moves_with_the_value_of_the_cases_covered() -> None:
    batch, result, clean = _metrics()
    aligned = align_ground_truth(result.cases, batch.ground_truth)
    victim = next(o for o in result.outcome.outcomes if o.state is OutcomeState.AUTO_CLOSED)
    victim_value = case_value_paise(next(c for c in result.cases if c.case_id == victim.case_id))
    perturbed = [
        o.model_copy(update={"state": OutcomeState.ABSTAINED}) if o.case_id == victim.case_id else o
        for o in result.outcome.outcomes
    ]
    report = compute_metrics(result.cases, perturbed, batch.ground_truth)

    assert victim_value > 0
    assert report.value_coverage.numerator == clean.value_coverage.numerator - victim_value
    assert report.value_coverage.denominator == clean.value_coverage.denominator
    assert aligned[victim.case_id] is not None


# --- The disclosed limits of these numbers. ---


def test_six_of_seven_subtypes_are_adopted_from_deterministic_triggers() -> None:
    """Why the macro reads 1.0000, stated as a test rather than left to be discovered.

    Session 5.1's baseline adopts an already-fired §3.3 trigger wherever component 4
    fired one, and component 4 is deterministic. Six of the seven graded subtypes have
    such a trigger, so on the baseline arm those six are correct by construction and
    only `UNMATCHED_INBOUND_CREDIT` — the one §4.2 says "turns entirely on whether the
    free-text narration identifies a counterparty" — is decided by classification at
    all. The macro is therefore not primarily a measure of Slot A on this arm, which
    is what §5.4's ablation exists to expose and what §5.3's adversarial set (Phase 7)
    is the independent check on.
    """
    batch, result, _ = _metrics()
    aligned = align_ground_truth(result.cases, batch.ground_truth)
    trigger_backed = {
        subtype
        for outcome in result.outcome.outcomes
        for subtype in outcome.triggered_subtypes
        if subtype.value in {label.value for label in GRADED_SUBTYPES}
    }
    assert {s.value for s in trigger_backed} == {
        label.value for label in GRADED_SUBTYPES if label is not SubtypeLabel.UNMATCHED_INBOUND_CREDIT
    }
    unmatched = [
        o
        for o in result.outcome.outcomes
        if aligned[o.case_id].ground_truth_exception_subtype is ExceptionSubtype.UNMATCHED_INBOUND_CREDIT
    ]
    assert len(unmatched) == 8
    assert all(not o.triggered_subtypes for o in unmatched)


def test_the_seventeen_policy_declined_cases_are_invisible_to_the_subtype_metrics() -> None:
    """A disclosed consequence of §5.2's definition, pinned rather than papered over.

    The 5 family-4 date-error and 12 FR-06 cases carry ground-truth subtypes
    (`MISPOSTING`, `OMISSION`) that are `ACCOUNTING_CORRECTION`-side and outside Slot
    A's eight-value vocabulary entirely, so they enter no per-subtype denominator on
    either side even though a classifier saw all 17. They are graded by
    `state_prediction_accuracy` and `declined_by_policy_rate`, and on the class axis by
    session 6.2's exception-class confusion matrix.
    """
    batch, result, report = _metrics()
    aligned = align_ground_truth(result.cases, batch.ground_truth)
    policy_declined = [o for o in result.outcome.outcomes if o.decline_reason is DeclineReason.POLICY]
    assert len(policy_declined) == 17
    assert all(o.classified_subtype is not None for o in policy_declined)
    assert all(
        ground_truth_subtype(aligned[o.case_id]) is None for o in policy_declined
    )
    graded_cases = sum(m.recall.denominator for m in report.subtype_metrics)
    assert graded_cases == 36
    assert graded_cases + 17 < report.total_cases


# --- `Rate` and the macro average. ---


def test_rate_reports_an_undefined_metric_as_none_not_zero() -> None:
    assert rate(0, 0).value is None
    assert rate(0, 0).is_defined is False
    assert rate(0, 5).value == 0.0
    assert rate(0, 5).is_defined is True


def test_rate_rejects_negative_inputs() -> None:
    with pytest.raises(MetricsError):
        rate(-1, 5)
    with pytest.raises(MetricsError):
        rate(1, -5)


def test_macro_average_denominator_is_seven_and_numerator_is_the_defined_subtypes() -> None:
    """REV-25: seven subtypes. The numerator says how many actually entered the mean,
    so a macro computed over fewer says so on its face (§5.2's visible-denominator rule)."""
    _, _, report = _metrics()
    for macro, per_subtype in (
        (report.exception_subtype_precision_macro, [m.precision for m in report.subtype_metrics]),
        (report.exception_subtype_recall_macro, [m.recall for m in report.subtype_metrics]),
    ):
        defined = [r.value for r in per_subtype if r.value is not None]
        assert macro.denominator == 7
        assert macro.numerator == len(defined)
        assert macro.value == pytest.approx(sum(defined) / len(defined))


def test_macro_average_never_includes_ambiguous_case() -> None:
    """`AMBIGUOUS_CASE` is a §3.3 *class*, not an `OPERATIONAL_EXCEPTION` subtype —
    graded by session 6.2's class confusion matrix, not here."""
    assert SubtypeLabel.AMBIGUOUS_CASE not in GRADED_SUBTYPES
    assert len(GRADED_SUBTYPES) == 7
    assert len(SubtypeLabel) == 8
    _, _, report = _metrics()
    assert all(metric.subtype is not SubtypeLabel.AMBIGUOUS_CASE for metric in report.subtype_metrics)
    # Still counted, so the seven precision denominators reconcile against Slot A's caseload.
    assert "AMBIGUOUS_CASE" in report.classification_counts


def test_classification_counts_reconcile_against_the_cases_a_classifier_saw() -> None:
    _, result, report = _metrics()
    classified = [o for o in result.outcome.outcomes if o.classified_subtype is not None]
    assert sum(report.classification_counts.values()) == len(classified)
    assert len(classified) == len(result.classifications)


# --- `auto_applied_entries` and entry matching. ---


def _candidate(case_id: str, template_id: str, legs: tuple[tuple[str, int, int], ...]) -> CandidateJournalEntry:
    return CandidateJournalEntry(
        case_id=case_id,
        template_id=template_id,
        legs=tuple(
            CandidateJournalLeg(account_code=code, account_name=f"Account {code}", debit=debit, credit=credit)
            for code, debit, credit in legs
        ),
        cited_record_ids=("pay_1",),
    )


def _expected(template_id: str, legs: tuple[tuple[str, int, int], ...]) -> ExpectedJournalEntry:
    return ExpectedJournalEntry(
        template_id=template_id,
        legs=tuple(
            ExpectedJournalLeg(account_code=code, account_name=f"Account {code}", debit=debit, credit=credit)
            for code, debit, credit in legs
        ),
    )


_T01 = (("5010", 200, 0), ("5020", 36, 0), ("1200", 0, 236))


def test_auto_applied_entries_includes_replayed_entries() -> None:
    """The second `apply_batch` pass of a classifier run replays rather than reposts
    (§1.7.4), so an auto-closed case's entries arrive as `replayed_entries`. Counting
    only `applied_entries` would make `auto_close_precision`'s denominator 0."""
    entry = _candidate("case_1", "T-01", _T01)
    posted = CaseOutcome(case_id="case_1", state=OutcomeState.AUTO_CLOSED, applied_entries=(entry,))
    replayed = CaseOutcome(case_id="case_1", state=OutcomeState.AUTO_CLOSED, replayed_entries=(entry,))
    assert auto_applied_entries(posted) == (entry,)
    assert auto_applied_entries(replayed) == (entry,)


def test_auto_applied_entries_excludes_proposed_entries() -> None:
    """FR-07 proposals are unapplied by definition; §1.6's denominator is auto-applied."""
    entry = _candidate("case_1", "T-01", _T01)
    proposed = CaseOutcome(
        case_id="case_1",
        state=OutcomeState.REVIEW_REQUIRED,
        decline_reason=DeclineReason.POLICY,
        proposed_entries=(entry,),
    )
    assert auto_applied_entries(proposed) == ()


def test_auto_close_precision_denominator_is_nonzero_on_a_classifier_run() -> None:
    """The regression the replay reading exists to prevent, checked end to end."""
    _, _, with_classifier = _metrics(classifier=classify_batch_baseline)
    _, _, without = _metrics(classifier=None)
    assert with_classifier.auto_close_precision.denominator == EXPECTED_AUTO_CLOSED_ENTRIES
    assert without.auto_close_precision.denominator == EXPECTED_AUTO_CLOSED_ENTRIES
    assert with_classifier.auto_close_precision.value == without.auto_close_precision.value


def test_entry_matching_ignores_leg_order() -> None:
    reordered = (_T01[2], _T01[0], _T01[1])
    assert count_matching_entries([_candidate("c", "T-01", _T01)], [_expected("T-01", reordered)]) == 1


def test_entry_matching_rejects_a_wrong_template_or_a_wrong_amount() -> None:
    assert count_matching_entries([_candidate("c", "T-03", _T01)], [_expected("T-01", _T01)]) == 0
    wrong = (("5010", 201, 0), ("5020", 36, 0), ("1200", 0, 237))
    assert count_matching_entries([_candidate("c", "T-01", wrong)], [_expected("T-01", _T01)]) == 0


def test_entry_matching_consumes_each_expected_entry_at_most_once() -> None:
    """A multiset match: two identical applied entries against one expected entry is
    one match and one error, not two matches."""
    applied = [_candidate("c", "T-01", _T01), _candidate("c", "T-01", _T01)]
    assert count_matching_entries(applied, [_expected("T-01", _T01)]) == 1
    assert count_matching_entries(applied, [_expected("T-01", _T01), _expected("T-01", _T01)]) == 2


def test_entry_matching_ignores_account_name() -> None:
    """§3.1 makes `account_code` the key; the name is derived from it in
    `pipeline.accounts`, which both sides read, so comparing it would only add a way
    for a cosmetic rename to read as a wrong journal."""
    applied = _candidate("c", "T-01", _T01)
    renamed = ExpectedJournalEntry(
        template_id="T-01",
        legs=tuple(
            ExpectedJournalLeg(account_code=code, account_name="Renamed", debit=debit, credit=credit)
            for code, debit, credit in _T01
        ),
    )
    assert count_matching_entries([applied], [renamed]) == 1


# --- `case_value_paise`. ---


def _settlement(amount: int) -> Settlement:
    return Settlement(
        id="setl_1",
        amount=amount,
        status=SettlementStatus.PROCESSED,
        fees=0,
        tax=0,
        utr="UTR123456789",
        created_at=0,
    )


def _bank_line(line_id: str, *, deposit: int = 0, withdrawal: int = 0) -> BankLine:
    return BankLine(
        line_id=line_id,
        value_date=SNAPSHOT,
        narration="NEFT CR FROM ACME TRADERS",
        withdrawal_paise=withdrawal,
        deposit_paise=deposit,
        closing_balance_paise=0,
        bank_profile=BankProfile.HDFC,
    )


def test_settlement_anchored_case_is_worth_its_settlement_amount() -> None:
    case = Case(case_id="setl_1", kind=CaseKind.SETTLEMENT_ANCHORED, settlement=_settlement(123_456))
    assert case_value_paise(case) == 123_456


def test_orphan_case_is_worth_the_magnitude_across_its_bank_lines() -> None:
    """A `DUPLICATE_CREDIT` case spans two lines (REV-18) and a `REVERSAL_UNMATCHED`
    case is a withdrawal; both count at magnitude, so neither nets away."""
    duplicate = Case(
        case_id="case_orphan_a",
        kind=CaseKind.ORPHAN,
        bank_lines=(_bank_line("a", deposit=500), _bank_line("b", deposit=500)),
    )
    reversal = Case(
        case_id="case_orphan_c", kind=CaseKind.ORPHAN, bank_lines=(_bank_line("c", withdrawal=700),)
    )
    assert case_value_paise(duplicate) == 1_000
    assert case_value_paise(reversal) == 700


def test_settlement_anchored_case_without_a_settlement_is_an_error() -> None:
    case = Case(case_id="setl_1", kind=CaseKind.SETTLEMENT_ANCHORED)
    with pytest.raises(MetricsError):
        case_value_paise(case)


# --- Batch alignment: the orphan case-ID join (§1.6's `expected_linked_source_records`). ---


def test_the_two_sides_disagree_on_every_orphan_case_id() -> None:
    """The defect the alignment step exists for, pinned so it cannot be forgotten.

    The generator mints `orphan_<hex>`; `pipeline.case_assembly` synthesizes
    `case_orphan_<lowest line_id>` having never seen the generator (§4.1). A
    join on `case_id` alone silently drops all 25 orphan cases — a sixth of
    the batch, and the sixth §4.2's one graded LLM slot is mostly about.
    """
    batch, result = _run()
    assembled_ids = {case.case_id for case in result.cases}
    orphan_truth = [
        truth for truth in batch.ground_truth if truth.case_id not in assembled_ids
    ]
    assert len(orphan_truth) == 25
    assert all(truth.case_id.startswith("orphan_") for truth in orphan_truth)
    assert len([case for case in result.cases if case.kind is CaseKind.ORPHAN]) == 25


def test_alignment_resolves_all_one_hundred_fifty_cases() -> None:
    batch, result = _run()
    aligned = align_ground_truth(result.cases, batch.ground_truth)
    assert len(aligned) == TOTAL_CASES
    assert aligned.keys() == {case.case_id for case in result.cases}


def test_alignment_resolves_a_duplicate_credit_pair_to_one_case() -> None:
    """REV-18: a duplicate credit and its original are one case spanning two bank
    lines, so two cited line IDs must resolve to a single assembled case."""
    batch, result = _run()
    aligned = align_ground_truth(result.cases, batch.ground_truth)
    two_line_truths = [
        truth for truth in aligned.values() if len(truth.expected_linked_source_records) == 2
    ]
    duplicate_credits = [
        truth
        for truth in two_line_truths
        if truth.ground_truth_exception_subtype is ExceptionSubtype.DUPLICATE_CREDIT
    ]
    assert len(duplicate_credits) == 3
    by_truth_id = {truth.case_id: case_id for case_id, truth in aligned.items()}
    assert len({by_truth_id[truth.case_id] for truth in duplicate_credits}) == 3


def _orphan_truth(case_id: str, lines: tuple[str, ...]) -> GroundTruthCase:
    return _truth(case_id).model_copy(update={"expected_linked_source_records": lines})


def test_alignment_rejects_a_ground_truth_case_spanning_two_assembled_cases() -> None:
    """REV-18's granularity rule failing. Picking either target would report a
    passing metric over a broken split, so it raises instead."""
    cases = [
        Case(case_id="case_orphan_a", kind=CaseKind.ORPHAN, bank_lines=(_bank_line("a", deposit=1),)),
        Case(case_id="case_orphan_b", kind=CaseKind.ORPHAN, bank_lines=(_bank_line("b", deposit=1),)),
    ]
    with pytest.raises(MetricsError, match="spans 2 assembled cases"):
        align_ground_truth(cases, [_orphan_truth("orphan_1", ("a", "b"))])


def test_alignment_rejects_two_ground_truth_cases_claiming_one_assembled_case() -> None:
    cases = [
        Case(
            case_id="case_orphan_a",
            kind=CaseKind.ORPHAN,
            bank_lines=(_bank_line("a", deposit=1), _bank_line("b", deposit=1)),
        )
    ]
    with pytest.raises(MetricsError, match="claimed by two ground-truth cases"):
        align_ground_truth(cases, [_orphan_truth("orphan_1", ("a",)), _orphan_truth("orphan_2", ("b",))])


def test_alignment_does_not_resolve_through_a_settlement_anchored_case_s_bank_lines() -> None:
    """Only orphan lines are indexed. A settlement credit cited by an orphan label
    means the populations have crossed, and that must surface, not silently join."""
    cases = [
        Case(
            case_id="setl_1",
            kind=CaseKind.SETTLEMENT_ANCHORED,
            settlement=_settlement(100),
            bank_lines=(_bank_line("credit_1", deposit=100),),
        )
    ]
    with pytest.raises(MetricsError, match="matches no assembled case"):
        align_ground_truth(cases, [_orphan_truth("orphan_1", ("credit_1",))])


# --- Batch alignment: coverage guards. ---


def _truth(case_id: str) -> GroundTruthCase:
    return GroundTruthCase(
        case_id=case_id,
        expected_outcome_state=OutcomeState.AUTO_MATCHED,
        ground_truth_exception_class=ExceptionClass.NONE,
        ground_truth_exception_subtype=ExceptionSubtype.NONE,
        expected_linked_source_records=(),
        expected_resolution=None,
        expected_journal_entries=(),
        expected_template_ids=(),
        expected_decline_reason=None,
        should_auto_apply=False,
    )


def test_a_labelled_case_the_run_never_scored_is_an_error() -> None:
    """Caught by the alignment step: a label with neither a matching `case_id` nor a
    linked source record naming an assembled orphan case has nothing to score."""
    case = Case(case_id="setl_1", kind=CaseKind.SETTLEMENT_ANCHORED, settlement=_settlement(100))
    outcome = CaseOutcome(case_id="setl_1", state=OutcomeState.AUTO_MATCHED)
    with pytest.raises(MetricsError, match="matches no assembled case"):
        compute_metrics([case], [outcome], [_truth("setl_1"), _truth("setl_2")])


def test_a_scored_case_with_no_label_is_an_error() -> None:
    cases = [
        Case(case_id="setl_1", kind=CaseKind.SETTLEMENT_ANCHORED, settlement=_settlement(100)),
        Case(case_id="setl_2", kind=CaseKind.SETTLEMENT_ANCHORED, settlement=_settlement(100)),
    ]
    outcomes = [
        CaseOutcome(case_id="setl_1", state=OutcomeState.AUTO_MATCHED),
        CaseOutcome(case_id="setl_2", state=OutcomeState.AUTO_MATCHED),
    ]
    with pytest.raises(MetricsError, match="different batches"):
        compute_metrics(cases, outcomes, [_truth("setl_1")])


def test_a_duplicate_case_id_is_an_error() -> None:
    case = Case(case_id="setl_1", kind=CaseKind.SETTLEMENT_ANCHORED, settlement=_settlement(100))
    outcome = CaseOutcome(case_id="setl_1", state=OutcomeState.AUTO_MATCHED)
    with pytest.raises(MetricsError, match="duplicate case_id"):
        compute_metrics([case], [outcome, outcome], [_truth("setl_1")])


def test_an_outcome_naming_no_assembled_case_is_an_error() -> None:
    outcome = CaseOutcome(case_id="setl_1", state=OutcomeState.AUTO_MATCHED)
    with pytest.raises(MetricsError, match="no assembled case"):
        compute_metrics([], [outcome], [_truth("setl_1")])


# --- Provenance and performance. ---


def test_provenance_defaults_to_empty_and_round_trips_what_it_is_given() -> None:
    """§5.6.1/FR-13's pin is session 7.2's; the fields exist now so the JSON's shape
    does not change when 7.2 fills them, and nothing is derived from the working tree."""
    _, _, blank = _metrics()
    assert blank.provenance.seed is None
    assert blank.provenance.git_sha is None

    batch, result = _run()
    pinned = compute_metrics(
        result.cases,
        result.outcome.outcomes,
        batch.ground_truth,
        provenance=RunProvenance(
            seed=0,
            git_sha="abc1234",
            model_id="accounts/fireworks/models/gpt-oss-120b",
            cache_hit_rate=1.0,
            snapshot_date=SNAPSHOT.isoformat(),
        ),
    )
    assert pinned.provenance.seed == 0
    assert pinned.provenance.cache_hit_rate == 1.0
    assert pinned.provenance.snapshot_date == "2026-08-28"


def test_performance_metrics_are_a_separate_document_from_the_report() -> None:
    """§1.6 lists `throughput` and `end_to_end_latency`; §5.6.3 requires the metrics
    JSON to reproduce byte-identically. Keeping wall-clock out of `MetricsReport` is
    what lets both be literally true."""
    _, _, report = _metrics()
    assert "elapsed" not in report.model_dump_json()
    assert "throughput" not in report.model_dump_json()

    measured = performance_metrics(case_count=150, elapsed_seconds=3.0, hardware="test host")
    assert measured.throughput_cases_per_second == 50.0
    assert measured.hardware == "test host"


def test_performance_metrics_report_an_unmeasurable_throughput_as_none() -> None:
    assert performance_metrics(case_count=150, elapsed_seconds=0.0).throughput_cases_per_second is None
    with pytest.raises(MetricsError):
        performance_metrics(case_count=-1, elapsed_seconds=1.0)
