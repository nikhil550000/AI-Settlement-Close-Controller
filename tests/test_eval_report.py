"""Session 6.2's checkpoint (spec.md §6.3):

> Dev-versus-held-out gap recorded in BUILDLOG.

The checkpoint test is `test_development_versus_held_out_gap_is_the_figure_recorded_in_buildlog`.
It runs §5.1's development batch (seed 1) and held-out batch (seed 2) on both
arms — session 5.1's deterministic keyword baseline and Slot A — and asserts
every figure this session writes into `BUILDLOG.md` as a literal transcribed
here. A gap "recorded in BUILDLOG" that no test pins is a number that drifts
the first time anything upstream changes; pinning it makes the log and the
code one artifact.

**Both arms run offline.** The baseline needs nothing. Slot A runs against the
committed `data/llm_cache.json` in `CacheMode.STRICT` with `client=None`, which
by construction never builds a network path (§4.3's "hard error rather than a
fallthrough to the API") — the same NFR-05 discipline `tests/test_llm_slot_a.py`
and `tests/test_llm_slot_b.py` follow, except that here the cache is the real
committed artifact rather than a stub's output.

**§5.1's rule with teeth, discharged.** Seed 2 is held out and was not
inspected case by case, and no prompt, threshold or classifier behaviour was
changed in response to any number below. Both batches were generated, run and
reported; nothing was tuned. The one §5.5 verdict that comes back outside its
band on the Slot A arm (`abstention_rate`, BELOW) is reported as measured.

Around the checkpoint: the two §5.2 matrices against their own hand-checked
margins, negative controls proving each matrix moves when one outcome is
perturbed, unit coverage of every branch of `predict_exception_class`, the
§5.5 table transcription, and the renderers' one hard requirement — no rate
printed without its denominator.
"""

from __future__ import annotations

import random
from datetime import date
from functools import partial
from pathlib import Path

import pytest

from generator.cli import generate_reference_batch
from pipeline.apply import CaseOutcome
from pipeline.case_assembly import Case, CaseKind
from pipeline.classifier import classify_batch_baseline, classify_batch_llm
from pipeline.eval_report import (
    EXCEPTION_CLASS_LABELS,
    PROVISIONAL_THRESHOLDS,
    STATE_LABELS,
    TARGETED_METRICS,
    BatchComparison,
    TargetKind,
    ThresholdVerdict,
    build_eval_report,
    build_exception_class_matrix,
    build_state_matrix,
    compare_reports,
    named_rates,
    render_comparison,
    render_confusion_matrix,
    render_eval_report,
    render_subtype_breakdown,
    render_threshold_review,
    review_thresholds,
)
from pipeline.exception_class import (
    OPERATIONAL_SUBTYPES,
    is_timing_attributed,
    predict_exception_class,
)
from pipeline.ground_truth import ExceptionClass, ExceptionSubtype, OutcomeState
from pipeline.llm_cache import CacheMode, PromptCache
from pipeline.metrics import MacroRate, MetricsError, Rate, align_ground_truth, rate
from pipeline.run import run_batch
from pipeline.storage import connect
from pipeline.subtype_label import SubtypeLabel

SNAPSHOT = date(2026, 8, 28)
CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "llm_cache.json"

DEVELOPMENT_SEED = 1
HELD_OUT_SEED = 2
"""§5.1's table: seed 1 is the development batch, seed 2 is held out."""


# --- §3.3's population map, transcribed for the class matrix's hand-check. ---

GROUND_TRUTH_CLASS_COUNTS: dict[str, int] = {
    "NONE": 18,
    "ACCOUNTING_CORRECTION": 67,
    "OPERATIONAL_EXCEPTION": 36,
    "EXPECTED_TIMING_DIFFERENCE": 12,
    "AMBIGUOUS_CASE": 17,
}
"""§3.3's "Family and population mapping" table plus §3.6's, added up by hand.

`ACCOUNTING_CORRECTION` — families 1-5 at 10 each (50), the family-4
date-error variant (5) and FR-06's tax positions (12) = 67.
`OPERATIONAL_EXCEPTION` — the four settlement-anchored subtypes (5 + 5 + 4 + 5)
and §3.6's three orphan subtypes (8 + 6 + 3) = 36, which is §3.6's
`EXTERNAL_ACTION_REQUIRED` row exactly.
`AMBIGUOUS_CASE` — 9 settlement-anchored plus §3.6's 8 opaque-narration
orphans = 17, §3.6's `ABSTAINED` row exactly.
`EXPECTED_TIMING_DIFFERENCE` — the family-4 no-op (12).
`NONE` — the fully clean population (18).

Transcribed from the spec, never read back from the generator, for the reason
session 6.1's checkpoint gives about denominators: a figure checked against
the code that produced it is asserting nothing.
"""


# --- The figures this session records in BUILDLOG.md. ---

BASELINE_DEV = {"state": (150, 150), "precision_macro": 1.0, "recall_macro": 1.0, "abstained": 17}
BASELINE_HELD_OUT = BASELINE_DEV
"""The keyword baseline is identical on both batches — see the checkpoint test."""

SLOT_A_DEV = {"state": (144, 150), "precision_macro": 0.8063, "recall_macro": 0.8905, "abstained": 11}
SLOT_A_HELD_OUT = {"state": (142, 150), "precision_macro": 0.7956, "recall_macro": 0.8857, "abstained": 9}

SLOT_A_LARGEST_TARGETED_GAP = ("state_prediction_accuracy", -0.0133)
"""Slot A's largest development-to-held-out movement on a §5.5-targeted metric."""

VALUE_COVERAGE_GAP = -0.0265
"""`value_coverage` moves this much between seeds 1 and 2 on **both** arms.

It is a property of the amounts the generator drew, not of the system — which
is why `BatchComparison.largest_targeted_gap` exists beside `largest_gap`, and
why §5.5 gives `value_coverage` no target.
"""

SLOT_A_DEV_SUBTYPE_RATES: dict[SubtypeLabel, tuple[tuple[int, int], tuple[int, int]]] = {
    SubtypeLabel.SETTLEMENT_UTR_MISSING: ((5, 25), (5, 5)),
    SubtypeLabel.BANK_CREDIT_OVERDUE: ((5, 5), (5, 5)),
    SubtypeLabel.SETTLEMENT_AMOUNT_MISMATCH: ((4, 4), (4, 4)),
    SubtypeLabel.UNMATCHED_INBOUND_CREDIT: ((8, 18), (8, 8)),
    SubtypeLabel.REVERSAL_UNMATCHED: ((5, 5), (5, 6)),
    SubtypeLabel.DUPLICATE_CREDIT: ((3, 3), (3, 3)),
    SubtypeLabel.DISPUTE_PENDING: ((2, 2), (2, 5)),
}
"""Slot A's per-subtype `(precision, recall)` as `(numerator, denominator)`, seed 1."""

SLOT_A_HELD_OUT_SUBTYPE_RATES: dict[SubtypeLabel, tuple[tuple[int, int], tuple[int, int]]] = {
    SubtypeLabel.SETTLEMENT_UTR_MISSING: ((3, 24), (3, 5)),
    SubtypeLabel.BANK_CREDIT_OVERDUE: ((5, 5), (5, 5)),
    SubtypeLabel.SETTLEMENT_AMOUNT_MISMATCH: ((4, 4), (4, 4)),
    SubtypeLabel.UNMATCHED_INBOUND_CREDIT: ((8, 18), (8, 8)),
    SubtypeLabel.REVERSAL_UNMATCHED: ((6, 6), (6, 6)),
    SubtypeLabel.DUPLICATE_CREDIT: ((3, 3), (3, 3)),
    SubtypeLabel.DISPUTE_PENDING: ((3, 3), (3, 5)),
}
"""The same, seed 2. §5.2 requires these denominators visible; here they are asserted."""


def _slot_a_classifier():
    """Slot A against the committed cache, strict, with no client — no network path exists."""
    return partial(
        classify_batch_llm, cache=PromptCache(CACHE_PATH), mode=CacheMode.STRICT, client=None
    )


def _report(seed: int, arm: str):
    classifier = classify_batch_baseline if arm == "baseline" else _slot_a_classifier()
    batch = generate_reference_batch(random.Random(seed), SNAPSHOT)
    result = run_batch(
        connect(":memory:"),
        settlements=batch.settlements,
        recon_lines=batch.recon_lines,
        bank_lines=batch.bank_lines,
        ledger_entries=batch.ledger_entries,
        snapshot_date=SNAPSHOT,
        classifier=classifier,
    )
    return batch, result, build_eval_report(
        result.cases, result.outcome.outcomes, batch.ground_truth, arm=arm, seed=seed
    )


def _assert_recorded(report, recorded: dict) -> None:
    metrics = report.metrics
    assert (metrics.state_prediction_accuracy.numerator, metrics.state_prediction_accuracy.denominator) == recorded["state"]
    assert round(metrics.exception_subtype_precision_macro.value, 4) == recorded["precision_macro"]
    assert round(metrics.exception_subtype_recall_macro.value, 4) == recorded["recall_macro"]
    assert metrics.abstention_rate.numerator == recorded["abstained"]


# --- The checkpoint. ---


def test_development_versus_held_out_gap_is_the_figure_recorded_in_buildlog() -> None:
    """spec.md §6.3, session 6.2: "Dev-versus-held-out gap recorded in BUILDLOG."

    Both §5.1 batches, both arms, every figure this session's BUILDLOG entry
    states, asserted against the literals above. §5.1: "Both development and
    held-out metrics are reported side by side. A gap between them is itself a
    finding and is printed in the report rather than explained away."
    """
    _, _, baseline_dev = _report(DEVELOPMENT_SEED, "baseline")
    _, _, baseline_held = _report(HELD_OUT_SEED, "baseline")
    _, _, slot_a_dev = _report(DEVELOPMENT_SEED, "slot_a")
    _, _, slot_a_held = _report(HELD_OUT_SEED, "slot_a")

    _assert_recorded(baseline_dev, BASELINE_DEV)
    _assert_recorded(baseline_held, BASELINE_HELD_OUT)
    _assert_recorded(slot_a_dev, SLOT_A_DEV)
    _assert_recorded(slot_a_held, SLOT_A_HELD_OUT)

    baseline = compare_reports(baseline_dev.metrics, baseline_held.metrics, arm="baseline")
    slot_a = compare_reports(slot_a_dev.metrics, slot_a_held.metrics, arm="slot_a")

    # The baseline is byte-for-byte the same system on both batches: every
    # §5.5-targeted metric has a zero gap. §5.1 says as much before the fact —
    # "for the deterministic path it is close to vacuous" — and this is the
    # measurement of that claim, not a substitute for it.
    assert baseline.largest_targeted_gap is None
    for gap in baseline.gaps:
        if gap.metric in TARGETED_METRICS:
            assert gap.gap == 0.0, gap.metric

    # Slot A is the arm §5.1's rule is actually about, and it does move.
    largest = slot_a.largest_targeted_gap
    assert largest is not None
    assert largest.metric == SLOT_A_LARGEST_TARGETED_GAP[0]
    assert round(largest.gap, 4) == SLOT_A_LARGEST_TARGETED_GAP[1]
    # Every targeted gap is negative or zero: the held-out batch never scores
    # better than the batch the prompt was written against. That direction is
    # the finding; its size is small.
    for gap in slot_a.gaps:
        if gap.metric in TARGETED_METRICS and gap.gap is not None:
            assert gap.gap <= 0.0, gap.metric

    # `value_coverage` moves identically on both arms, which is what shows it
    # is a property of the generated amounts rather than of either classifier.
    for comparison in (baseline, slot_a):
        coverage = next(gap for gap in comparison.gaps if gap.metric == "value_coverage")
        assert round(coverage.gap, 4) == VALUE_COVERAGE_GAP
        assert comparison.largest_gap is not None
        assert comparison.largest_gap.metric == "value_coverage"

    # §5.2's per-subtype denominators, both batches, as recorded.
    for report, recorded in (
        (slot_a_dev, SLOT_A_DEV_SUBTYPE_RATES),
        (slot_a_held, SLOT_A_HELD_OUT_SUBTYPE_RATES),
    ):
        measured = {
            metric.subtype: (
                (metric.precision.numerator, metric.precision.denominator),
                (metric.recall.numerator, metric.recall.denominator),
            )
            for metric in report.metrics.subtype_metrics
        }
        assert measured == recorded


def test_slot_a_arm_runs_with_no_client_and_no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The checkpoint's offline half: strict mode over the committed cache.

    `CacheMode.STRICT` raises `CacheMissError` on a miss rather than falling
    through to the API (§4.3), and `client=None` means there is nothing to fall
    through to — so a passing run proves every seed-1 and seed-2 prompt is in
    `data/llm_cache.json`, which is what makes the checkpoint reproducible on a
    clean clone (§5.6.2).
    """
    monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
    for seed in (DEVELOPMENT_SEED, HELD_OUT_SEED):
        _, _, report = _report(seed, "slot_a")
        assert report.metrics.total_cases == 150


def test_both_arms_are_deterministic_across_two_builds() -> None:
    """Two builds of the same seed and arm produce identical reports (NFR-01)."""
    for arm in ("baseline", "slot_a"):
        _, _, first = _report(DEVELOPMENT_SEED, arm)
        _, _, second = _report(DEVELOPMENT_SEED, arm)
        assert first.model_dump_json() == second.model_dump_json()


# --- §5.2's two confusion matrices. ---


def test_state_matrix_margins_are_the_metrics_report_distributions() -> None:
    """The state matrix's margins are figures session 6.1 already computed.

    Row totals are `ground_truth_state_counts`, column totals are
    `predicted_state_counts`, and the diagonal over the total is
    `state_prediction_accuracy` — so the matrix is checked against the
    hand-checked surface rather than against a second hand count of its own.
    """
    for arm in ("baseline", "slot_a"):
        _, _, report = _report(DEVELOPMENT_SEED, arm)
        matrix = report.state_matrix
        assert matrix.labels == STATE_LABELS == tuple(str(s) for s in OutcomeState)
        assert len(matrix.labels) == 5
        assert matrix.row_totals() == report.metrics.ground_truth_state_counts
        assert matrix.column_totals() == report.metrics.predicted_state_counts
        assert matrix.total == report.metrics.total_cases == 150
        assert matrix.accuracy == report.metrics.state_prediction_accuracy


def test_exception_class_matrix_row_totals_hand_check_against_the_population_map() -> None:
    """§5.2's second matrix, its ground-truth margin asserted against §3.3's own table."""
    for arm in ("baseline", "slot_a"):
        _, _, report = _report(DEVELOPMENT_SEED, arm)
        matrix = report.exception_class_matrix
        assert matrix.labels == EXCEPTION_CLASS_LABELS == tuple(str(c) for c in ExceptionClass)
        assert len(matrix.labels) == 5
        assert matrix.row_totals() == GROUND_TRUTH_CLASS_COUNTS
        assert sum(GROUND_TRUTH_CLASS_COUNTS.values()) == 150
        assert matrix.total == 150


def test_the_class_axis_is_not_a_restatement_of_the_state_axis() -> None:
    """The reason §5.2's second matrix is built on a predicted class of its own.

    On the Slot A arm the two matrices disagree, and they must be able to: 12
    ground-truth `AMBIGUOUS_CASE` cases at seed 1 are classified into an
    `OPERATIONAL_EXCEPTION` subtype while only 6 of them change terminal state,
    because §4.2 lets one label of the eight — `UNMATCHED_INBOUND_CREDIT` —
    move a case out of `ABSTAINED` and no other. Had the predicted class been
    derived from the predicted state inside `pipeline/metrics.py`, those two
    numbers would be the same by construction and the class matrix would carry
    no information the state matrix does not.

    §3.3: "Outcome state answers *what the Controller did*; exception class
    answers *what was actually wrong with the case*. Every case carries both
    labels independently."
    """
    _, _, report = _report(DEVELOPMENT_SEED, "slot_a")
    state_errors = sum(count for _, _, count in report.state_matrix.confusions())
    class_errors = sum(count for _, _, count in report.exception_class_matrix.confusions())
    assert state_errors == 6
    assert class_errors == 12
    assert report.exception_class_matrix.count("AMBIGUOUS_CASE", "OPERATIONAL_EXCEPTION") == 12
    assert report.state_matrix.count("ABSTAINED", "EXTERNAL_ACTION_REQUIRED") == 6


def test_a_perturbed_state_moves_exactly_one_matrix_cell() -> None:
    """Negative control: the state matrix is not hard-wired to the diagonal."""
    batch, result, report = _report(DEVELOPMENT_SEED, "baseline")
    by_truth = align_ground_truth(result.cases, batch.ground_truth)
    outcomes = list(result.outcome.outcomes)
    victim = next(o for o in outcomes if o.state is OutcomeState.AUTO_CLOSED)
    outcomes[outcomes.index(victim)] = victim.model_copy(
        update={"state": OutcomeState.ABSTAINED}
    )

    perturbed = build_state_matrix({o.case_id: o for o in outcomes}, by_truth)
    assert perturbed.count("AUTO_CLOSED", "AUTO_CLOSED") == report.state_matrix.count("AUTO_CLOSED", "AUTO_CLOSED") - 1
    assert perturbed.count("AUTO_CLOSED", "ABSTAINED") == 1
    assert perturbed.accuracy.numerator == report.state_matrix.accuracy.numerator - 1
    assert perturbed.total == 150


def test_a_perturbed_class_moves_exactly_one_class_matrix_cell() -> None:
    """Negative control for the second matrix, on the axis it grades."""
    batch, result, report = _report(DEVELOPMENT_SEED, "baseline")
    by_truth = align_ground_truth(result.cases, batch.ground_truth)
    outcomes = list(result.outcome.outcomes)
    victim = next(
        o for o in outcomes if o.exception_class is ExceptionClass.ACCOUNTING_CORRECTION
    )
    outcomes[outcomes.index(victim)] = victim.model_copy(
        update={"exception_class": ExceptionClass.AMBIGUOUS_CASE}
    )

    perturbed = build_exception_class_matrix({o.case_id: o for o in outcomes}, by_truth)
    before = report.exception_class_matrix
    assert perturbed.count("ACCOUNTING_CORRECTION", "ACCOUNTING_CORRECTION") == before.count("ACCOUNTING_CORRECTION", "ACCOUNTING_CORRECTION") - 1
    assert perturbed.count("ACCOUNTING_CORRECTION", "AMBIGUOUS_CASE") == 1
    assert perturbed.confusions() == (("ACCOUNTING_CORRECTION", "AMBIGUOUS_CASE", 1),)


def test_class_matrix_rejects_an_outcome_with_no_assigned_class() -> None:
    """An unassigned class is not the `NONE` sentinel and must not be read as one."""
    batch, result, _ = _report(DEVELOPMENT_SEED, "baseline")
    by_truth = align_ground_truth(result.cases, batch.ground_truth)
    outcomes = list(result.outcome.outcomes)
    outcomes[0] = outcomes[0].model_copy(update={"exception_class": None})
    with pytest.raises(MetricsError, match="no predicted exception_class"):
        build_exception_class_matrix({o.case_id: o for o in outcomes}, by_truth)


def test_matrix_rejects_a_label_outside_its_axis() -> None:
    """A label neither axis defines raises rather than growing the matrix a sixth row."""
    batch, result, _ = _report(DEVELOPMENT_SEED, "baseline")
    by_truth = align_ground_truth(result.cases, batch.ground_truth)
    first = result.outcome.outcomes[0]
    with pytest.raises(MetricsError, match="not one of"):
        build_exception_class_matrix(
            {first.case_id: first.model_copy(update={"exception_class": "NOT_A_CLASS"})},
            {first.case_id: by_truth[first.case_id]},
        )


def test_confusions_is_empty_on_a_perfect_matrix() -> None:
    _, _, report = _report(DEVELOPMENT_SEED, "baseline")
    assert report.state_matrix.confusions() == ()
    assert report.exception_class_matrix.confusions() == ()
    assert report.state_matrix.accuracy.value == 1.0


# --- `pipeline.exception_class`, branch by branch. ---


def _predict(**overrides) -> ExceptionClass:
    kwargs = {
        "declined_by_policy": False,
        "has_entries": False,
        "triggered_subtypes": (),
        "classified_subtype": None,
        "timing_attributed": False,
        "residual_paise": 0,
    }
    kwargs.update(overrides)
    return predict_exception_class(**kwargs)


def test_policy_exclusion_is_an_accounting_correction() -> None:
    """§3.3's population map: FR-06 tax and the family-4 date-error variant are both
    `ACCOUNTING_CORRECTION` while terminating in `REVIEW_REQUIRED`/`policy`."""
    assert _predict(declined_by_policy=True) is ExceptionClass.ACCOUNTING_CORRECTION
    assert (
        _predict(declined_by_policy=True, residual_paise=9_99) is ExceptionClass.ACCOUNTING_CORRECTION
    )


def test_a_template_instantiation_is_an_accounting_correction() -> None:
    assert _predict(has_entries=True) is ExceptionClass.ACCOUNTING_CORRECTION


def test_a_correction_outranks_a_fired_trigger() -> None:
    """§3.3 defines `OPERATIONAL_EXCEPTION` as a discrepancy no journal entry can
    resolve, so a case one did resolve is not one — the same precedence
    `assign_state`'s branch 2 applies, for the same reason (family 4 fires both
    `T-04` and `BANK_CREDIT_OVERDUE`; session 4.1 measured this)."""
    assert (
        _predict(has_entries=True, triggered_subtypes=(ExceptionSubtype.BANK_CREDIT_OVERDUE,))
        is ExceptionClass.ACCOUNTING_CORRECTION
    )


def test_a_fired_trigger_is_an_operational_exception() -> None:
    assert (
        _predict(triggered_subtypes=(ExceptionSubtype.DISPUTE_PENDING,))
        is ExceptionClass.OPERATIONAL_EXCEPTION
    )


def test_an_operational_label_from_slot_a_is_an_operational_exception() -> None:
    for label in OPERATIONAL_SUBTYPES:
        assert (
            _predict(classified_subtype=label, residual_paise=1)
            is ExceptionClass.OPERATIONAL_EXCEPTION
        )


def test_slot_a_ambiguous_case_is_not_an_operational_exception() -> None:
    """`AMBIGUOUS_CASE` is a §3.3 *class*, not a subtype beneath the second one."""
    assert (
        _predict(classified_subtype=SubtypeLabel.AMBIGUOUS_CASE, residual_paise=1)
        is ExceptionClass.AMBIGUOUS_CASE
    )


def test_a_timing_attributed_residual_is_an_expected_timing_difference() -> None:
    assert _predict(timing_attributed=True) is ExceptionClass.EXPECTED_TIMING_DIFFERENCE


def test_a_clean_zero_residual_is_none() -> None:
    assert _predict(residual_paise=0) is ExceptionClass.NONE


def test_an_unexplained_residual_falls_through_to_ambiguous() -> None:
    assert _predict(residual_paise=12_345) is ExceptionClass.AMBIGUOUS_CASE


def test_is_timing_attributed_needs_a_no_match_inside_the_window() -> None:
    """Exactly §3.3's timing-residual shape — the matcher's own reason for zeroing."""
    base = Case(case_id="c", kind=CaseKind.SETTLEMENT_ANCHORED)
    assert not is_timing_attributed(base)
    assert not is_timing_attributed(base.model_copy(update={"match_tier": 0, "in_settlement_window": True}))
    assert not is_timing_attributed(base.model_copy(update={"match_tier": 3, "in_settlement_window": False}))
    assert is_timing_attributed(base.model_copy(update={"match_tier": 3, "in_settlement_window": True}))
    assert not is_timing_attributed(
        Case(case_id="o", kind=CaseKind.ORPHAN).model_copy(
            update={"match_tier": 3, "in_settlement_window": True}
        )
    )


def test_apply_batch_assigns_a_class_to_every_case() -> None:
    """Component 8's output carries §3.3's second label on all 150 cases."""
    for arm in ("baseline", "slot_a"):
        _, result, _ = _report(DEVELOPMENT_SEED, arm)
        assert len(result.outcome.outcomes) == 150
        assert all(o.exception_class is not None for o in result.outcome.outcomes)


def test_apply_case_leaves_the_class_unset() -> None:
    """`apply_case` decides state; `apply_batch` labels. A hand-built outcome is unlabelled."""
    assert CaseOutcome(case_id="c", state=OutcomeState.ABSTAINED).exception_class is None


def test_the_class_never_changes_a_terminal_state() -> None:
    """It classifies; it decides nothing. Assigning every case the wrong class by
    hand leaves `assign_state`'s output untouched, because nothing reads the field."""
    _, result, _ = _report(DEVELOPMENT_SEED, "baseline")
    before = [o.state for o in result.outcome.outcomes]
    relabelled = [
        o.model_copy(update={"exception_class": ExceptionClass.AMBIGUOUS_CASE})
        for o in result.outcome.outcomes
    ]
    assert [o.state for o in relabelled] == before


# --- §5.5's threshold review. ---


def test_threshold_review_covers_every_row_of_the_5_5_table() -> None:
    """§5.5's table, minus the one row that is not on `MetricsReport`."""
    _, _, report = _report(DEVELOPMENT_SEED, "baseline")
    checks = report.threshold_review
    assert len(checks) == len(PROVISIONAL_THRESHOLDS) == 12
    assert [c.target.metric for c in checks] == [t.metric for t in PROVISIONAL_THRESHOLDS]
    # Every reviewed metric is a real metric on the report.
    rates = named_rates(report.metrics)
    for check in checks:
        assert check.target.metric in rates
        assert check.measured == rates[check.target.metric]
    # `throughput`/`end_to_end_latency` are §5.5's only omitted row — they live in
    # `PerformanceMetrics`, outside the byte-identical metrics JSON (session 6.1).
    assert "throughput" not in {c.target.metric for c in checks}


def test_targeted_metrics_are_exactly_the_rows_with_a_target() -> None:
    reported = {t.metric for t in PROVISIONAL_THRESHOLDS if t.kind is TargetKind.REPORTED}
    assert reported == {"match_rate", "value_coverage"}
    assert TARGETED_METRICS == {t.metric for t in PROVISIONAL_THRESHOLDS} - reported


def test_declined_by_policy_is_checked_against_a_construction_not_a_tolerance() -> None:
    """§5.5's `≈ 11.3%` with its own Note: "By construction, not by performance."

    So the check is exact equality with the batch's ground-truth
    `REVIEW_REQUIRED` population (§3.6's 17 of 150), not an invented tolerance
    band around 0.113.
    """
    _, _, report = _report(DEVELOPMENT_SEED, "baseline")
    check = next(c for c in report.threshold_review if c.target.metric == "declined_by_policy_rate")
    assert check.target.kind is TargetKind.GROUND_TRUTH_COUNT
    assert check.target.lower is None and check.target.upper is None
    assert check.verdict is ThresholdVerdict.MET
    assert check.measured.numerator == 17
    assert "17 ground-truth REVIEW_REQUIRED" in check.detail


def test_slot_a_abstention_rate_is_reported_below_its_range_not_tuned_away() -> None:
    """§5.5: below 8% "suggests the system is forcing calls it should decline".

    Slot A assigns `UNMATCHED_INBOUND_CREDIT` to more orphan credits than
    §3.6 planted, and each extra assignment moves a case out of `ABSTAINED`.
    The verdict is reported as measured; nothing was changed in response to it
    (§5.1's logging rule — see this module's docstring).
    """
    for seed in (DEVELOPMENT_SEED, HELD_OUT_SEED):
        _, _, report = _report(seed, "slot_a")
        check = next(c for c in report.threshold_review if c.target.metric == "abstention_rate")
        assert check.verdict is ThresholdVerdict.BELOW
        assert check.measured.value < 0.08


def test_a_metric_above_its_band_is_above_not_a_failure() -> None:
    """A perfect deterministic run lands above several §5.5 bands, and says so."""
    _, _, report = _report(DEVELOPMENT_SEED, "baseline")
    by_metric = {c.target.metric: c for c in report.threshold_review}
    assert by_metric["state_prediction_accuracy"].verdict is ThresholdVerdict.ABOVE
    assert by_metric["auto_close_precision"].verdict is ThresholdVerdict.MET
    assert by_metric["false_match_rate"].verdict is ThresholdVerdict.MET
    assert by_metric["match_rate"].verdict is ThresholdVerdict.REPORTED


def test_an_undefined_metric_gets_its_own_verdict() -> None:
    """A zero denominator is undefined, not a failure — session 6.1's `Rate` rule,
    carried into the review so a metric nobody predicted cannot read as a miss."""
    _, _, report = _report(DEVELOPMENT_SEED, "baseline")
    broken = report.metrics.model_copy(
        update={"auto_close_precision": rate(0, 0)}
    )
    check = next(
        c for c in review_thresholds(broken) if c.target.metric == "auto_close_precision"
    )
    assert check.verdict is ThresholdVerdict.UNDEFINED
    assert check.measured.value is None
    assert "undefined, not zero" in check.detail


# --- §5.1's comparison. ---


def test_gap_is_held_out_minus_development() -> None:
    _, _, dev = _report(DEVELOPMENT_SEED, "slot_a")
    _, _, held = _report(HELD_OUT_SEED, "slot_a")
    comparison = compare_reports(dev.metrics, held.metrics, arm="slot_a")
    assert comparison.development_seed == 1 and comparison.held_out_seed == 2
    for gap in comparison.gaps:
        assert gap.gap == pytest.approx(gap.held_out.value - gap.development.value)
        assert gap.absolute_gap == pytest.approx(abs(gap.gap))


def test_comparison_covers_every_rate_named_rates_exposes() -> None:
    """A metric added to `MetricsReport` and wired into `named_rates` is compared
    automatically; one that is not wired in is caught here rather than silently
    dropped from §5.1's side-by-side."""
    _, _, dev = _report(DEVELOPMENT_SEED, "baseline")
    _, _, held = _report(HELD_OUT_SEED, "baseline")
    comparison = compare_reports(dev.metrics, held.metrics, arm="baseline")
    assert [gap.metric for gap in comparison.gaps] == list(named_rates(dev.metrics))
    assert len(comparison.gaps) == 15


def test_an_undefined_metric_leaves_the_gap_undefined() -> None:
    """Not zero: "no case was predicted S" is not "the two batches agree"."""
    _, _, dev = _report(DEVELOPMENT_SEED, "baseline")
    _, _, held = _report(HELD_OUT_SEED, "baseline")
    broken = dev.metrics.model_copy(update={"auto_close_precision": rate(0, 0)})
    comparison = compare_reports(broken, held.metrics, arm="baseline")
    gap = next(g for g in comparison.gaps if g.metric == "auto_close_precision")
    assert gap.gap is None
    assert gap.absolute_gap is None


def test_a_tie_between_two_gaps_is_broken_by_order_not_by_float_noise() -> None:
    """Found while pinning the checkpoint, and it changed which metric gets reported.

    On the Slot A arm `state_prediction_accuracy` and `abstention_rate` both
    move by the same two cases in 150 between seeds 1 and 2 — they are two
    views of the same two cases — so their gaps are equal. As raw floats the
    two differ in the sixteenth decimal place, and a plain `max` reported
    whichever way the subtraction happened to round. `largest_gap_among`
    compares at `_GAP_PRECISION` and takes the first in §1.6's order instead.
    """
    _, _, dev = _report(DEVELOPMENT_SEED, "slot_a")
    _, _, held = _report(HELD_OUT_SEED, "slot_a")
    comparison = compare_reports(dev.metrics, held.metrics, arm="slot_a")
    by_metric = {gap.metric: gap for gap in comparison.gaps}
    tied = ("state_prediction_accuracy", "abstention_rate", "deferred_to_human_rate")
    assert {round(by_metric[m].gap, 6) for m in tied} == {-0.013333}
    assert by_metric[tied[0]].gap != by_metric[tied[1]].gap  # raw floats differ
    assert comparison.largest_targeted_gap is not None
    assert comparison.largest_targeted_gap.metric == "state_prediction_accuracy"
    # Stable across repeated construction, which is the property that matters.
    for _ in range(3):
        again = compare_reports(dev.metrics, held.metrics, arm="slot_a")
        assert again.largest_targeted_gap.metric == "state_prediction_accuracy"


def test_largest_gap_is_none_when_nothing_moved() -> None:
    empty = BatchComparison(
        arm="test",
        development_seed=1,
        held_out_seed=2,
        gaps=(),
    )
    assert empty.largest_gap is None
    assert empty.largest_targeted_gap is None
    assert empty.max_absolute_gap == 0.0


# --- Rendering: §5.2's one hard requirement. ---


def test_no_renderer_prints_a_rate_without_its_denominator() -> None:
    """§5.2: reported "with denominators visible … rather than as a single headline
    number", and §3.6's reason — the seven denominators divide 36 cases."""
    _, _, report = _report(DEVELOPMENT_SEED, "slot_a")
    breakdown = render_subtype_breakdown(report.metrics)
    for metric in report.metrics.subtype_metrics:
        assert f"({metric.precision.numerator}/{metric.precision.denominator})" in breakdown
        assert f"({metric.recall.numerator}/{metric.recall.denominator})" in breakdown
    assert "macro average" in breakdown

    review = render_threshold_review(report.threshold_review)
    for check in report.threshold_review:
        measured = check.measured
        if isinstance(measured, MacroRate):
            # A macro is a mean of ratios, not a ratio (see `MacroRate`); it carries
            # its coverage instead of a denominator, and §5.2's rule is satisfied by
            # the per-subtype table above, where the seven real denominators live.
            assert f"({measured.subtypes_averaged} of {measured.subtypes_eligible} subtypes)" in review
        else:
            assert f"({measured.numerator}/{measured.denominator})" in review


def test_confusion_matrix_renders_every_label_and_its_confusions() -> None:
    _, _, report = _report(DEVELOPMENT_SEED, "slot_a")
    text = render_confusion_matrix(report.state_matrix)
    assert "rows = ground truth, columns = predicted" in text
    for label in STATE_LABELS:
        assert label in text
    assert "6 × ground truth ABSTAINED → predicted EXTERNAL_ACTION_REQUIRED" in text
    assert "agreement: 0.9600 (144/150)" in text


def test_comparison_renders_both_the_largest_gap_and_the_targeted_one() -> None:
    _, _, dev = _report(DEVELOPMENT_SEED, "slot_a")
    _, _, held = _report(HELD_OUT_SEED, "slot_a")
    text = render_comparison(compare_reports(dev.metrics, held.metrics, arm="slot_a"))
    assert "largest gap: value_coverage -0.0265" in text
    assert "largest gap on a §5.5-targeted metric: state_prediction_accuracy -0.0133" in text


def test_full_report_renders_all_four_sections() -> None:
    _, _, report = _report(DEVELOPMENT_SEED, "baseline")
    text = render_eval_report(report)
    assert "arm: baseline, seed 1" in text
    assert "outcome_state" in text
    assert "exception_class" in text
    assert "exception subtype" in text
    assert "threshold review" in text


def test_ratio_of_an_undefined_rate_says_undefined() -> None:
    _, _, report = _report(DEVELOPMENT_SEED, "baseline")
    broken = report.metrics.model_copy(update={"auto_close_precision": rate(0, 0)})
    assert "undefined (0/0)" in render_threshold_review(review_thresholds(broken))
