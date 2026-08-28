"""Session 6.2's evaluation artifacts: §5.2's two confusion matrices, the
per-subtype breakdown, the development-versus-held-out comparison, and the
§5.5 threshold review.

Component 9's second half. `pipeline/metrics.py` computes the §1.6 surface
for one batch; this module is what §5.1, §5.2 and §5.5 ask to be *shown* on
top of it, as structured data plus a plain-text rendering. Session 6.3's
Jinja2 HTML (FR-11) renders the same models — the tables here exist so the
numbers can be read, asserted and pasted into `BUILDLOG.md` before any HTML
exists, and so the report and the log can never disagree about a figure.

**Three things this module refuses to do.**

1. **It never recomputes a metric.** Every figure it shows comes off a
   `MetricsReport`, whose denominators session 6.1 hand-checked against
   §3.6's batch totals. A rendering that re-derives a number it is
   displaying is a second implementation of the metric, and the two would
   eventually disagree.
2. **It never joins a run to ground truth by `case_id`.** The join goes
   through `pipeline.metrics.align_ground_truth`, because the generator
   mints orphan case IDs as `orphan_<hex>` and case assembly synthesizes
   `case_orphan_<line_id>` — a `case_id` join silently drops all 25 orphan
   cases (session 6.1, Broke).
3. **It never prints a rate without its denominator.** §5.2 requires the
   per-subtype denominators be visible "rather than as a single headline
   number", and §3.6 gives the reason: seven subtypes divide 36 cases at
   roughly five each, so a bare 0.80 hides whether it was 4/5 or 8/10.

**Matrix orientation, stated once and applied everywhere:** rows are ground
truth, columns are what the system predicted. A row total is therefore a
recall denominator and a column total a precision denominator, which is
§1.6's own convention read off the axes.

**On the development/held-out gap (§5.1).** Both batches are reported side
by side and the gap is printed, "itself a finding … rather than explained
away". §5.1's rule with teeth applies to whoever *acts* on what they see:
any prompt or threshold change made in response to inspecting held-out cases
MUST be logged in `BUILDLOG.md` with its reason. This module inspects
nothing case by case — it aggregates — but the rule is restated here because
this is the module through which held-out numbers first become visible.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from pipeline.apply import CaseOutcome
from pipeline.case_assembly import Case
from pipeline.ground_truth import ExceptionClass, GroundTruthCase, OutcomeState
from pipeline.metrics import (
    GRADED_SUBTYPES,
    MetricsError,
    MetricsReport,
    Rate,
    RunProvenance,
    align_ground_truth,
    compute_metrics,
    rate,
)

# --- Confusion matrices (§5.2). ---


class ConfusionMatrix(BaseModel):
    """One §5.2 confusion matrix: rows are ground truth, columns are predicted.

    `labels` fixes both axes — the same list in the same order down and
    across — so the diagonal is the agreement and every off-diagonal cell
    names a specific confusion. Both of §5.2's matrices are square and 5×5 by
    construction: five §1.3 states, or §3.3's four classes plus `NONE`.

    Absent labels are kept as all-zero rows and columns rather than dropped.
    A matrix that silently loses a state the system never predicted reads as
    a smaller system, and the empty row is exactly the finding §5.2 wants
    visible: "where the system is actually wrong rather than a headline that
    hides it."
    """

    model_config = ConfigDict(frozen=True)

    axis: str
    """What is being classified — `outcome_state` or `exception_class`."""
    labels: tuple[str, ...]
    counts: tuple[tuple[int, ...], ...]
    """`counts[i][j]` is the number of cases whose ground truth is
    `labels[i]` and whose prediction was `labels[j]`."""

    def count(self, truth: str, predicted: str) -> int:
        return self.counts[self.labels.index(truth)][self.labels.index(predicted)]

    @property
    def total(self) -> int:
        return sum(sum(row) for row in self.counts)

    @property
    def correct(self) -> int:
        """The diagonal: cases whose predicted label equals their ground-truth label."""
        return sum(row[i] for i, row in enumerate(self.counts))

    @property
    def accuracy(self) -> Rate:
        """Diagonal over total, with the denominator attached (`pipeline.metrics.Rate`)."""
        return rate(self.correct, self.total)

    def row_totals(self) -> dict[str, int]:
        """Per-label ground-truth population — every `*_recall` denominator on this axis."""
        return {label: sum(row) for label, row in zip(self.labels, self.counts)}

    def column_totals(self) -> dict[str, int]:
        """Per-label predicted population — every `*_precision` denominator on this axis."""
        return {
            label: sum(row[j] for row in self.counts) for j, label in enumerate(self.labels)
        }

    def confusions(self) -> tuple[tuple[str, str, int], ...]:
        """Every non-empty off-diagonal cell as `(ground truth, predicted, count)`.

        The whole point of the matrix, extracted: on a batch the system gets
        entirely right this is empty, and every entry in it is a specific
        claim about a specific misclassification rather than a dent in an
        aggregate.
        """
        return tuple(
            (self.labels[i], self.labels[j], value)
            for i, row in enumerate(self.counts)
            for j, value in enumerate(row)
            if i != j and value
        )


def _matrix(axis: str, labels: Sequence[str], pairs: Sequence[tuple[str, str]]) -> ConfusionMatrix:
    """A `ConfusionMatrix` over `labels` from `(ground truth, predicted)` pairs."""
    index = {label: i for i, label in enumerate(labels)}
    counts = [[0] * len(labels) for _ in labels]
    for truth, predicted in pairs:
        if truth not in index:
            raise MetricsError(f"{axis}: ground-truth label {truth!r} is not one of {list(labels)}")
        if predicted not in index:
            raise MetricsError(f"{axis}: predicted label {predicted!r} is not one of {list(labels)}")
        counts[index[truth]][index[predicted]] += 1
    return ConfusionMatrix(
        axis=axis,
        labels=tuple(labels),
        counts=tuple(tuple(row) for row in counts),
    )


STATE_LABELS: tuple[str, ...] = tuple(str(state) for state in OutcomeState)
"""§1.3's five terminal states, in §1.3's order."""

EXCEPTION_CLASS_LABELS: tuple[str, ...] = tuple(str(cls) for cls in ExceptionClass)
"""§3.3's four classes plus the `NONE` sentinel, in §3.3's order — §5.2's second axis."""


def build_state_matrix(
    by_outcome: Mapping[str, CaseOutcome], by_truth: Mapping[str, GroundTruthCase]
) -> ConfusionMatrix:
    """§5.2's 5×5 outcome-state confusion matrix.

    Its diagonal over its total is `state_prediction_accuracy` (§1.6), and
    its row and column totals are `MetricsReport.ground_truth_state_counts`
    and `.predicted_state_counts` — three figures session 6.1 already
    computes, which is why the tests assert this matrix against them rather
    than against a second hand count.
    """
    return _matrix(
        "outcome_state",
        STATE_LABELS,
        [
            (str(by_truth[case_id].expected_outcome_state), str(outcome.state))
            for case_id, outcome in by_outcome.items()
        ],
    )


def build_exception_class_matrix(
    by_outcome: Mapping[str, CaseOutcome], by_truth: Mapping[str, GroundTruthCase]
) -> ConfusionMatrix:
    """§5.2's 5×5 exception-class confusion matrix, over §3.3's four classes plus `NONE`.

    The predicted class is `CaseOutcome.exception_class`, assigned by
    component 8 from evidence (`pipeline.exception_class`) — not derived here
    from the predicted state. That decision and its reasoning are session
    6.2's, recorded in `pipeline/exception_class.py`'s module docstring: a
    class the grader invents from the state it is grading turns this matrix
    into a re-rendering of the state matrix.

    An unassigned class raises rather than counting as `NONE`. `NONE` is
    §3.3's positive sentinel for a clean case; "component 8 never labelled
    this one" is a different fact, and reading the second as the first would
    park every unlabelled case on a diagonal cell and inflate the accuracy.
    """
    pairs: list[tuple[str, str]] = []
    for case_id, outcome in by_outcome.items():
        if outcome.exception_class is None:
            raise MetricsError(
                f"case {case_id!r} carries no predicted exception_class; §5.2's class matrix "
                "needs one per case — outcomes must come from apply_batch, which assigns it"
            )
        pairs.append(
            (str(by_truth[case_id].ground_truth_exception_class), str(outcome.exception_class))
        )
    return _matrix("exception_class", EXCEPTION_CLASS_LABELS, pairs)


# --- §5.5's provisional targets, transcribed. ---


class TargetKind(StrEnum):
    """How §5.5's "Provisional target" column is to be read for one metric."""

    AT_MOST = "at_most"
    AT_LEAST = "at_least"
    BAND = "band"
    GROUND_TRUTH_COUNT = "ground_truth_count"
    """§5.5's `≈ 11.3%`, which its own Note says holds "by construction, not by
    performance" — so the check is exact equality with the batch's ground-truth
    `REVIEW_REQUIRED` population (§3.6's 17 of 150), not a tolerance band. No
    tolerance is invented, because §5.5 supplies a construction instead."""
    REPORTED = "reported"
    """§5.5's "reported, no target"."""


class ThresholdVerdict(StrEnum):
    MET = "MET"
    BELOW = "BELOW"
    ABOVE = "ABOVE"
    REPORTED = "REPORTED"
    UNDEFINED = "UNDEFINED"
    """The metric's denominator was 0, so there is nothing to compare (`Rate.value is None`)."""


class ThresholdTarget(BaseModel):
    """One row of §5.5's table, transcribed rather than paraphrased.

    Every figure in §5.5 is explicitly provisional and "set properly after
    the first real run against the development batch"; §5.5's own reason for
    publishing them early is that "any change must be logged in Section 8
    with its reason, so a moved goalpost is visible rather than silent."
    These literals are that goalpost. Changing one is a Section 8 revision,
    not an edit here.
    """

    model_config = ConfigDict(frozen=True)

    metric: str
    kind: TargetKind
    lower: float | None = None
    upper: float | None = None
    target_text: str
    """§5.5's "Provisional target" cell, verbatim."""
    note: str = ""
    """§5.5's "Note" cell, verbatim. Empty where §5.5 leaves it empty."""


PROVISIONAL_THRESHOLDS: tuple[ThresholdTarget, ...] = (
    ThresholdTarget(
        metric="false_match_rate",
        kind=TargetKind.AT_MOST,
        upper=0.0,
        target_text="0",
        note=(
            "Primary safety metric. Any non-zero value is investigated and reported "
            "case by case, never as a rate"
        ),
    ),
    ThresholdTarget(
        metric="auto_close_precision",
        kind=TargetKind.AT_LEAST,
        lower=0.98,
        target_text="≥ 0.98",
        note=(
            "Primary safety metric for adjustment. The 1.7.5 chain should make 1.00 "
            "reachable; whether it holds is the interesting question"
        ),
    ),
    ThresholdTarget(
        metric="auto_match_precision",
        kind=TargetKind.AT_LEAST,
        lower=0.95,
        target_text="≥ 0.95",
    ),
    ThresholdTarget(
        metric="auto_close_recall",
        kind=TargetKind.BAND,
        lower=0.80,
        upper=0.95,
        target_text="0.80 – 0.95",
        note=(
            "Nothing structural caps this — all 50 auto-close cases are in scope — so a "
            "low value means detection weakness, not policy discipline"
        ),
    ),
    ThresholdTarget(
        metric="auto_match_recall",
        kind=TargetKind.BAND,
        lower=0.85,
        upper=0.95,
        target_text="0.85 – 0.95",
    ),
    ThresholdTarget(
        metric="state_prediction_accuracy",
        kind=TargetKind.BAND,
        lower=0.80,
        upper=0.90,
        target_text="0.80 – 0.90",
    ),
    ThresholdTarget(
        metric="exception_subtype_recall_macro",
        kind=TargetKind.BAND,
        lower=0.70,
        upper=0.85,
        target_text="0.70 – 0.85",
        note="The Slot A metric. Thin per-subtype denominators; read with §5.2's breakdown, not alone",
    ),
    ThresholdTarget(
        metric="exception_subtype_precision_macro",
        kind=TargetKind.BAND,
        lower=0.75,
        upper=0.90,
        target_text="0.75 – 0.90",
    ),
    ThresholdTarget(
        metric="abstention_rate",
        kind=TargetKind.BAND,
        lower=0.08,
        upper=0.18,
        target_text="operating range 8 – 18%",
        note=(
            "Ground truth is 11.3% (§3.6). Below 8% suggests the system is forcing calls "
            "it should decline; above 18%, over-abstention degrading value"
        ),
    ),
    ThresholdTarget(
        metric="declined_by_policy_rate",
        kind=TargetKind.GROUND_TRUTH_COUNT,
        target_text="≈ 11.3%",
        note=(
            "By construction, not by performance. A large deviation is a bug in policy "
            "routing, and MUST be read as one"
        ),
    ),
    ThresholdTarget(
        metric="match_rate",
        kind=TargetKind.REPORTED,
        target_text="reported, no target",
        note="Not comparable to any industry figure — see the §3.5 enrichment disclosure",
    ),
    ThresholdTarget(
        metric="value_coverage",
        kind=TargetKind.REPORTED,
        target_text="reported, no target",
    ),
)
"""§5.5's table, minus its last row.

`throughput` and `end_to_end_latency` are omitted because they are not on
`MetricsReport` at all: session 6.1 put them in `PerformanceMetrics` so the
committed metrics JSON can reproduce byte-identically on a clean clone
(§5.6.3), and §5.5 already treats both as "reported, no target" with the
hardware stated alongside. Nothing else in §5.5's table is dropped.
"""


TARGETED_METRICS: frozenset[str] = frozenset(
    target.metric
    for target in PROVISIONAL_THRESHOLDS
    if target.kind is not TargetKind.REPORTED
)
"""The §5.5 metrics that carry an actual target, as opposed to "reported, no target".

Derived from the table above rather than listed again, so adding a row to
§5.5 puts the metric in scope for `BatchComparison.largest_targeted_gap`
automatically.
"""


class ThresholdCheck(BaseModel):
    """One §5.5 target, the measured value, and the verdict — denominator visible."""

    model_config = ConfigDict(frozen=True)

    target: ThresholdTarget
    measured: Rate
    verdict: ThresholdVerdict
    detail: str = ""
    """Why the verdict is what it is, where the comparison is not just a number
    against a bound (the `GROUND_TRUTH_COUNT` row, and any undefined metric)."""


def named_rates(report: MetricsReport) -> dict[str, Rate]:
    """Every §1.6 rate on a `MetricsReport`, keyed by its §1.6 name.

    One mapping, used by both the threshold review and the
    development-versus-held-out comparison, so a metric can never be reviewed
    against §5.5 under one name and compared across batches under another.
    Ordered as §1.6 lists them: matching, adjustment, classification,
    deferral, value.
    """
    return {
        "match_rate": report.match_rate,
        "auto_match_recall": report.auto_match_recall,
        "auto_match_precision": report.auto_match_precision,
        "false_match_rate": report.false_match_rate,
        "auto_close_recall": report.auto_close_recall,
        "auto_close_precision": report.auto_close_precision,
        "state_prediction_accuracy": report.state_prediction_accuracy,
        "exception_subtype_precision_macro": report.exception_subtype_precision_macro,
        "exception_subtype_recall_macro": report.exception_subtype_recall_macro,
        "declined_by_policy_rate": report.declined_by_policy_rate,
        "declined_by_confidence_rate": report.declined_by_confidence_rate,
        "abstention_rate": report.abstention_rate,
        "deferred_to_human_rate": report.deferred_to_human_rate,
        "open_case_rate": report.open_case_rate,
        "value_coverage": report.value_coverage,
    }


def review_thresholds(report: MetricsReport) -> tuple[ThresholdCheck, ...]:
    """§5.5's table, checked against one run's measured values.

    A verdict is a comparison against a published provisional target, not a
    judgement about the system: §5.5 states outright that every figure is
    provisional and set properly after the first real run against the
    development batch. A metric landing outside its band is a fact to report
    and, if the band moves, a Section 8 revision — not something to tune away
    here.
    """
    rates = named_rates(report)
    checks: list[ThresholdCheck] = []
    for target in PROVISIONAL_THRESHOLDS:
        measured = rates[target.metric]
        value = measured.value
        detail = ""
        if target.kind is TargetKind.REPORTED:
            verdict = ThresholdVerdict.REPORTED
        elif value is None:
            verdict = ThresholdVerdict.UNDEFINED
            detail = "denominator is 0; the metric is undefined, not zero"
        elif target.kind is TargetKind.GROUND_TRUTH_COUNT:
            expected = report.ground_truth_state_counts[str(OutcomeState.REVIEW_REQUIRED)]
            verdict = (
                ThresholdVerdict.MET
                if measured.numerator == expected
                else (
                    ThresholdVerdict.ABOVE
                    if measured.numerator > expected
                    else ThresholdVerdict.BELOW
                )
            )
            detail = (
                f"{measured.numerator} policy declines against §3.6's "
                f"{expected} ground-truth REVIEW_REQUIRED cases"
            )
        elif target.lower is not None and value < target.lower:
            verdict = ThresholdVerdict.BELOW
        elif target.upper is not None and value > target.upper:
            verdict = ThresholdVerdict.ABOVE
        else:
            verdict = ThresholdVerdict.MET
        checks.append(
            ThresholdCheck(target=target, measured=measured, verdict=verdict, detail=detail)
        )
    return tuple(checks)


# --- Development versus held-out (§5.1). ---


_GAP_PRECISION = 6
"""Decimal places at which two gaps count as equal when ranking them.

Six is far finer than any real difference between two 150-case batches (one
case is 0.0067) and far coarser than the float noise it exists to absorb.
"""


class MetricGap(BaseModel):
    """One metric on both batches, and the gap between them.

    `gap` is held-out minus development, so a negative gap is the direction
    §5.1 cares about — the held-out batch scoring worse than the batch the
    prompt and thresholds were set against. `None` on either side (an
    undefined metric) leaves the gap `None` rather than treating the missing
    value as zero.
    """

    model_config = ConfigDict(frozen=True)

    metric: str
    development: Rate
    held_out: Rate
    gap: float | None

    @property
    def absolute_gap(self) -> float | None:
        return None if self.gap is None else abs(self.gap)


class BatchComparison(BaseModel):
    """§5.1's side-by-side, and the finding it is meant to expose.

    §5.1: "Both development and held-out metrics are reported side by side.
    **A gap between them is itself a finding** and is printed in the report
    rather than explained away." This model is that print, and
    `largest_gap` is the one figure `BUILDLOG.md` records per arm.
    """

    model_config = ConfigDict(frozen=True)

    arm: str
    """Which classifier produced these numbers — `baseline` or `slot_a`. §5.4's
    ablation is the same two arms on the same batch, so naming the arm here is
    what lets a later session subtract one comparison from the other."""
    development_seed: int
    held_out_seed: int
    gaps: tuple[MetricGap, ...]

    def largest_gap_among(self, metrics: Collection[str]) -> MetricGap | None:
        """The metric in `metrics` that moved most, or `None` if none did.

        **Ties are broken by §1.6's ordering, not by float noise.** Every gap
        here is a difference of two ratios of small integer counts, so exact
        ties are common and meaningful — at seed 1 versus seed 2 on the Slot A
        arm, `state_prediction_accuracy` and `abstention_rate` both move by the
        same two cases in 150, because they are two views of the same two
        cases. Compared as raw floats those two differ in the sixteenth decimal
        place and the winner is whichever way the subtraction rounded, which
        would make the headline figure in `BUILDLOG.md` unstable for no reason.
        Magnitudes are compared at `_GAP_PRECISION` and the first metric in
        `named_rates`' order wins.
        """
        ranked = [gap for gap in self.gaps if gap.metric in metrics and gap.absolute_gap]
        if not ranked:
            return None
        largest = max(round(gap.absolute_gap or 0.0, _GAP_PRECISION) for gap in ranked)
        return next(
            gap for gap in ranked if round(gap.absolute_gap or 0.0, _GAP_PRECISION) == largest
        )

    @property
    def largest_gap(self) -> MetricGap | None:
        """The metric that moved most between the two batches, or `None` if none did."""
        return self.largest_gap_among({gap.metric for gap in self.gaps})

    @property
    def largest_targeted_gap(self) -> MetricGap | None:
        """The same, restricted to the metrics §5.5 sets a target for.

        `value_coverage` and `match_rate` are "reported, no target" in §5.5,
        and `value_coverage` in particular moves between any two seeds simply
        because the generator drew different amounts — 2.65 points between
        seeds 1 and 2, larger than anything the system does. Reporting that as
        the headline development-versus-held-out gap would bury the finding
        §5.1 is asking for. Both figures are reported; this is the one that is
        about the system.
        """
        return self.largest_gap_among(TARGETED_METRICS)

    @property
    def max_absolute_gap(self) -> float:
        """The largest absolute gap across every comparable metric; 0.0 when none moved."""
        largest = self.largest_gap
        return 0.0 if largest is None else (largest.absolute_gap or 0.0)


def compare_reports(
    development: MetricsReport,
    held_out: MetricsReport,
    *,
    arm: str,
    development_seed: int = 1,
    held_out_seed: int = 2,
) -> BatchComparison:
    """§5.1's development-versus-held-out comparison over every §1.6 rate.

    Seeds default to §5.1's own table — development is seed 1, held-out is
    seed 2 — and are parameters rather than constants because the scale batch
    (seed 3) and the seed-0 reference batch every prior session measured
    against are equally legitimate things to compare.
    """
    dev_rates = named_rates(development)
    held_rates = named_rates(held_out)
    gaps = []
    for metric, dev in dev_rates.items():
        held = held_rates[metric]
        gap = None if (dev.value is None or held.value is None) else held.value - dev.value
        gaps.append(MetricGap(metric=metric, development=dev, held_out=held, gap=gap))
    return BatchComparison(
        arm=arm,
        development_seed=development_seed,
        held_out_seed=held_out_seed,
        gaps=tuple(gaps),
    )


# --- The per-batch report. ---


class EvalReport(BaseModel):
    """Everything session 6.2 produces for one batch, ready for 6.3's HTML."""

    model_config = ConfigDict(frozen=True)

    seed: int | None
    arm: str
    metrics: MetricsReport
    state_matrix: ConfusionMatrix
    exception_class_matrix: ConfusionMatrix
    threshold_review: tuple[ThresholdCheck, ...]


def build_eval_report(
    cases: Sequence[Case],
    outcomes: Sequence[CaseOutcome],
    ground_truth: Sequence[GroundTruthCase],
    *,
    arm: str,
    seed: int | None = None,
    provenance: RunProvenance | None = None,
) -> EvalReport:
    """The §1.6 surface plus §5.2's matrices and §5.5's review, for one run.

    One call, one alignment: `compute_metrics` and both matrices are built
    over the same `align_ground_truth` join, so the matrices cannot end up
    describing a different set of cases from the metrics printed beside them.

    `provenance` is passed straight through to `compute_metrics` — session
    7.2's FR-13 pin (seed, git SHA, model ID, cache hit rate) — and defaults
    to `None` exactly as `compute_metrics` already does, so every session
    6.1/6.2 caller of this function is unaffected.
    """
    by_outcome = {outcome.case_id: outcome for outcome in outcomes}
    by_truth = align_ground_truth(cases, ground_truth)
    metrics = compute_metrics(cases, outcomes, ground_truth, provenance=provenance)
    return EvalReport(
        seed=seed,
        arm=arm,
        metrics=metrics,
        state_matrix=build_state_matrix(by_outcome, by_truth),
        exception_class_matrix=build_exception_class_matrix(by_outcome, by_truth),
        threshold_review=review_thresholds(metrics),
    )


# --- Plain-text rendering. ---


def _ratio(value: Rate) -> str:
    """A rate as `value (n/d)`, or `undefined (0/0)` — never a bare float (§5.2)."""
    shown = "undefined" if value.value is None else f"{value.value:.4f}"
    return f"{shown} ({value.numerator}/{value.denominator})"


def render_confusion_matrix(matrix: ConfusionMatrix) -> str:
    """A confusion matrix as a fixed-width table, rows ground truth, columns predicted.

    Columns are numbered rather than labelled: five `EXTERNAL_ACTION_REQUIRED`
    headers is 130 characters of table nobody can read. The legend down the
    left carries the full names, and the numbering is the same order in both
    directions, so the diagonal is still the diagonal.
    """
    labels = matrix.labels
    width = max(len(label) for label in labels) + 4  # room for the "N. " row prefix
    header = "".join(f"{i + 1:>6}" for i in range(len(labels)))
    lines = [
        f"{matrix.axis}  (rows = ground truth, columns = predicted)",
        f"{'':{width}}  {header}{'':4}{'total':>7}",
    ]
    for i, label in enumerate(labels):
        row = matrix.counts[i]
        cells = "".join(f"{value:>6}" if value else f"{'.':>6}" for value in row)
        lines.append(f"{f'{i + 1}. {label}':{width}}  {cells}{'':4}{sum(row):>7}")
    totals = matrix.column_totals()
    lines.append(
        f"{'predicted total':{width}}  " + "".join(f"{totals[label]:>6}" for label in labels)
    )
    lines.append(f"agreement: {_ratio(matrix.accuracy)}")
    for truth, predicted, count in matrix.confusions():
        lines.append(f"  confusion: {count} × ground truth {truth} → predicted {predicted}")
    return "\n".join(lines)


def render_subtype_breakdown(report: MetricsReport) -> str:
    """§5.2's per-subtype table: precision and recall with both denominators visible.

    §5.2 requires exactly this shape — "reported per subtype with
    denominators visible, plus a macro average across the seven subtypes" —
    and §3.6 gives the reason it may not be collapsed to a headline: the
    seven denominators divide 36 cases at roughly five each.
    """
    width = max(len(str(metric.subtype)) for metric in report.subtype_metrics)
    lines = [
        "exception subtype  (§5.2, macro over seven subtypes per REV-25)",
        f"{'subtype':{width}}  {'precision':>20}  {'recall':>20}",
    ]
    for metric in report.subtype_metrics:
        lines.append(
            f"{str(metric.subtype):{width}}  {_ratio(metric.precision):>20}  {_ratio(metric.recall):>20}"
        )
    precision_macro = report.exception_subtype_precision_macro
    recall_macro = report.exception_subtype_recall_macro
    lines.append(
        f"{'macro average':{width}}  {_ratio(precision_macro):>20}  {_ratio(recall_macro):>20}"
    )
    lines.append(
        f"{'':{width}}  macro denominators are subtypes averaged / "
        f"{len(GRADED_SUBTYPES)} eligible, not cases"
    )
    return "\n".join(lines)


def render_threshold_review(checks: Sequence[ThresholdCheck]) -> str:
    """§5.5's table with the measured column filled in, and every figure marked provisional."""
    width = max(len(check.target.metric) for check in checks)
    target_width = max(len(check.target.target_text) for check in checks)
    lines = [
        "§5.5 threshold review  (every target below is provisional — §5.5)",
        f"{'metric':{width}}  {'target':>{target_width}}  {'measured':>28}  verdict",
    ]
    for check in checks:
        lines.append(
            f"{check.target.metric:{width}}  {check.target.target_text:>{target_width}}  "
            f"{_ratio(check.measured):>28}  {check.verdict}"
        )
        if check.detail:
            lines.append(f"{'':{width}}  {check.detail}")
    return "\n".join(lines)


def render_comparison(comparison: BatchComparison) -> str:
    """§5.1's side-by-side, with the gap printed rather than explained away."""
    width = max(len(gap.metric) for gap in comparison.gaps)
    lines = [
        f"development (seed {comparison.development_seed}) versus held-out "
        f"(seed {comparison.held_out_seed}) — arm: {comparison.arm}",
        f"{'metric':{width}}  {'development':>20}  {'held-out':>20}  {'gap':>9}",
    ]
    for gap in comparison.gaps:
        shown = "n/a" if gap.gap is None else f"{gap.gap:+.4f}"
        lines.append(
            f"{gap.metric:{width}}  {_ratio(gap.development):>20}  {_ratio(gap.held_out):>20}  {shown:>9}"
        )
    largest = comparison.largest_gap
    if largest is None:
        lines.append("largest gap: none — every comparable metric is identical across both batches")
    else:
        lines.append(f"largest gap: {largest.metric} {largest.gap:+.4f}")
    targeted = comparison.largest_targeted_gap
    if targeted is None:
        lines.append("largest gap on a §5.5-targeted metric: none")
    else:
        lines.append(f"largest gap on a §5.5-targeted metric: {targeted.metric} {targeted.gap:+.4f}")
    return "\n".join(lines)


def render_eval_report(report: EvalReport) -> str:
    """One batch's full §5.2/§5.5 rendering, in the order §5.2 introduces them."""
    header = f"eval report — arm: {report.arm}"
    if report.seed is not None:
        header += f", seed {report.seed}"
    return "\n\n".join(
        (
            header,
            render_confusion_matrix(report.state_matrix),
            render_confusion_matrix(report.exception_class_matrix),
            render_subtype_breakdown(report.metrics),
            render_threshold_review(report.threshold_review),
        )
    )
