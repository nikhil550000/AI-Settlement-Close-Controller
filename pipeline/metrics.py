"""The metric surface, computed against ground truth.

This module is the metric surface only. The two confusion matrices, the
per-subtype breakdown as a rendered table, the held-out run and the
threshold review live in the report layer; the HTML that renders any of it
lives there too. What is here is the arithmetic those depend on.

**Denominators are the whole job.** Past defects each corrected a metric
whose name and denominator disagreed, and shipping numbers that overstate
the system is the failure to guard against above all. Three consequences
run through every model below.

1. **Every rate carries its own numerator and denominator as integers**
   (`Rate`), and the float is derived from them. A metric is never a bare
   float whose denominator has to be reconstructed from a different part of
   the report to be checked.
2. **A zero denominator produces `value=None`, never `0.0`.** "No case was
   predicted `DUPLICATE_CREDIT`, so precision is undefined" and "every case
   predicted `DUPLICATE_CREDIT` was wrong, so precision is 0.0" are
   different findings, and collapsing them flatters the system.
3. **The denominator convention is transcribed, not paraphrased.**
   `*_rate` denominators are total cases; `*_recall` denominators are the
   ground-truth population for that state; `*_precision` denominators are
   the population the system predicted. Each metric names its own
   convention in its docstring, so a reader checking one does not have to
   hold a separate table in their head.

**The macro average is over seven subtypes, `DISPUTE_PENDING` included.**
An earlier draft stated this two ways, with a headline count of seven
against a cited clause of six — and six cannot divide the 36
`EXTERNAL_ACTION_REQUIRED` cases, which are allocated 5/5/4/5/8/6/3
across all seven. Corrected before any of this was written.

**Wall-clock metrics live in `PerformanceMetrics`, deliberately outside
`MetricsReport`.** The spec lists `throughput` and `end_to_end_latency`
alongside the correctness metrics, but the committed metrics JSON must
reproduce **byte-identically** on a clean clone, and a wall-clock figure
cannot. Splitting them keeps both requirements literally true instead of
quietly weakening one; see `PerformanceMetrics` for the full reasoning.

Nothing here reads a clock, and nothing here divides money. Numerators and
denominators stay integer paise (`value_coverage`) or integer case counts;
a ratio is a dimensionless measure of the two, computed once, at the end.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict

from pipeline.apply import CaseOutcome
from pipeline.attachment import AttachmentAudit
from pipeline.bank_accounting import BankLineAccounting
from pipeline.case_assembly import Case, CaseKind
from pipeline.exception_class import OPERATIONAL_SUBTYPES
from pipeline.ground_truth import (
    DeclineReason,
    ExceptionSubtype,
    ExpectedJournalEntry,
    GroundTruthCase,
    OutcomeState,
)
from pipeline.instantiator import CandidateJournalEntry
from pipeline.subtype_label import SubtypeLabel


class MetricsError(Exception):
    """The run and the ground truth do not describe the same batch.

    Raised rather than tolerated: every metric here has a case-count
    denominator, so a case present on one side and missing on the
    other silently changes a denominator, which is the one failure mode
    this module exists to prevent.
    """


GRADED_SUBTYPES: tuple[SubtypeLabel, ...] = OPERATIONAL_SUBTYPES
"""The seven `OPERATIONAL_EXCEPTION` subtypes the macro average covers.

Aliased to `pipeline.exception_class.OPERATIONAL_SUBTYPES` so the tuple the
macro averages over and the tuple the class assignment reads as
`OPERATIONAL_EXCEPTION` are one object and cannot drift apart.

`SubtypeLabel`'s declaration order is the table order followed by
`DISPUTE_PENDING`, named as the "seventh subtype" in the sentence beneath
that table, so this tuple keeps that ordering with nothing reshuffled.

`AMBIGUOUS_CASE` is excluded because it is a *class* in the taxonomy, not
a subtype beneath `OPERATIONAL_EXCEPTION`: its ground-truth subtype is
`NONE`, so it has no per-subtype population to be precise or recallful
about. It is graded on the class axis instead, by the exception-class
confusion matrix. It remains one of Slot A's eight decodable values, and
`classification_counts` below reports how often it was assigned, so the
seven precision denominators stay auditable against the 70 cases Slot A
actually saw.
"""

_SUBTYPE_FROM_GROUND_TRUTH: dict[ExceptionSubtype, SubtypeLabel] = {
    ExceptionSubtype(label.value): label for label in GRADED_SUBTYPES
}
"""`ground_truth_exception_subtype` narrowed to Slot A's vocabulary.

`ExceptionSubtype` carries ten members; `NONE`, `OMISSION` and `MISPOSTING`
are `ACCOUNTING_CORRECTION`-side (or the clean sentinel) and are not values
Slot A may emit, so a case carrying one has no graded subtype at all and
enters no per-subtype denominator on either side. This is a disclosed gap
in the baseline's vocabulary, and the reason those cases are excluded from
the subtype metrics rather than counted as misses against a subtype that
was never available.
"""


def ground_truth_subtype(truth: GroundTruthCase) -> SubtypeLabel | None:
    """This case's graded subtype, or `None` if it has none (`NONE`/`OMISSION`/`MISPOSTING`)."""
    return _SUBTYPE_FROM_GROUND_TRUTH.get(truth.ground_truth_exception_subtype)


def assigned_subtype(outcome: CaseOutcome) -> SubtypeLabel | None:
    """The subtype the system assigned this case, or `None` if it assigned none.

    Component 5's output (`CaseOutcome.classified_subtype`), not component
    4's fired triggers: the metrics grade the *classification*, and subtype
    classification lives in Slot A. `None` covers both the ~80
    `AUTO_CLOSED`/`AUTO_MATCHED` cases Slot A never sees (it is scoped to
    non-`AUTO_CLOSED` cases) and any run made with no classifier at all.
    """
    return outcome.classified_subtype


# --- Rates, with their denominators attached. ---


class Rate(BaseModel):
    """One metric as numerator, denominator, and the ratio of the two.

    `value` is `None` when `denominator == 0` — an undefined metric, which
    is a different fact from a zero one and is reported as such rather than
    rounded into it.

    The two integers are the auditable part. For every metric except
    `value_coverage` they are case counts; `value_coverage`'s are integer
    paise, and the ratio is dimensionless either way.
    """

    model_config = ConfigDict(frozen=True)

    numerator: int
    denominator: int
    value: float | None

    @property
    def is_defined(self) -> bool:
        return self.value is not None


class MacroRate(BaseModel):
    """An unweighted mean over per-subtype rates (the macro average).

    Deliberately **not** a `Rate`. A mean of ratios is not itself a ratio of
    two integers, and expressing it as one produced a committed artifact that
    contradicted itself on its face — `{"numerator": 7, "denominator": 7,
    "value": 0.80}`, where 7/7 is 1.00. `Rate`'s own docstring makes the two
    integers "the auditable part" and derives the float from them; a macro
    that borrowed the shape while breaking that derivation defeated the one
    discipline this module exists to enforce.

    So the two counts travel under names that say what they actually are:
    how many subtypes entered the mean, and how many were eligible to. A
    macro computed over five of seven subtypes still says so on its face,
    which is exactly why the per-subtype denominators need to stay visible,
    without claiming to be a ratio.
    """

    model_config = ConfigDict(frozen=True)

    value: float | None
    subtypes_averaged: int
    subtypes_eligible: int

    @property
    def is_defined(self) -> bool:
        return self.value is not None


def rate(numerator: int, denominator: int) -> Rate:
    """A `Rate` from two integers, with the undefined case handled once, here."""
    if denominator < 0 or numerator < 0:
        raise MetricsError(f"a rate takes non-negative integers, got {numerator}/{denominator}")
    return Rate(
        numerator=numerator,
        denominator=denominator,
        value=(numerator / denominator) if denominator else None,
    )


def _macro_average(rates: Sequence[Rate]) -> MacroRate:
    """The unweighted mean over the rates that are defined.

    Reported as a `Rate` whose `numerator` is how many subtypes entered the
    mean and whose `denominator` is how many were eligible, so a macro
    computed over five of seven subtypes says so on its face. The
    per-subtype denominators need to stay visible precisely because they are
    thin; a macro that silently drops a subtype would defeat that.
    """
    defined = [r.value for r in rates if r.value is not None]
    return MacroRate(
        value=(sum(defined) / len(defined)) if defined else None,
        subtypes_averaged=len(defined),
        subtypes_eligible=len(rates),
    )


class SubtypeMetrics(BaseModel):
    """One subtype's precision and recall, with both denominators visible."""

    model_config = ConfigDict(frozen=True)

    subtype: SubtypeLabel
    precision: Rate
    """Among cases the system assigned S, the fraction whose ground truth is S."""
    recall: Rate
    """Among cases whose ground truth is S, the fraction the system assigned S."""


# --- Per-case value, for `value_coverage`. ---


def case_value_paise(case: Case) -> int:
    """The rupee value at stake in one case, in integer paise.

    `value_coverage` is defined over "rupee value in cases" without
    fixing what one case's value is, and the two anchor kinds have
    different natural answers, so both are stated here rather than left
    implicit at the call site:

    - **Settlement-anchored** — `settlement.amount`, the net figure
      Razorpay owes for the batch. Not the gross of its recon lines: the
      settlement is the thing being reconciled against the bank, and the
      hard invariant (`settlement.amount == sum(credits) − sum(debits)
      − fees − tax`) makes it the one figure that already nets the case.
    - **Orphan** — the total magnitude across the case's bank lines,
      `deposit + withdrawal` per line. Magnitude, not a signed net, because
      a `REVERSAL_UNMATCHED` case is a withdrawal and a `DUPLICATE_CREDIT`
      case spans two credits; netting either to zero or to one leg
      would understate the money a reviewer has to account for.

    Orphan cases can never reach `AUTO_MATCHED` or `AUTO_CLOSED` (they are
    unresolvable by construction), so they only ever land in the
    denominator. Using magnitude keeps that denominator honest instead of
    shrinking the uncovered pile.
    """
    if case.kind is CaseKind.SETTLEMENT_ANCHORED:
        if case.settlement is None:
            raise MetricsError(f"settlement-anchored case {case.case_id} carries no settlement")
        return int(case.settlement.amount)
    return sum(int(line.deposit_paise) + int(line.withdrawal_paise) for line in case.bank_lines)


# --- Auto-applied entries, for `auto_close_precision`. ---


def auto_applied_entries(outcome: CaseOutcome) -> tuple[CandidateJournalEntry, ...]:
    """Every entry this case has auto-applied to the ledger, posted or replayed.

    `applied_entries` alone is not the intended population. `run_batch`
    calls `apply_batch` twice whenever a classifier is supplied: the first
    pass posts, and the second recognises the identical
    `(case_id, resolution_id)` and replays rather than
    reposting — so on the `BatchOutcome` a caller actually receives, an
    auto-closed case's entries sit in `replayed_entries` and
    `applied_entries` is empty. Reading only the latter would compute
    `auto_close_precision` over an empty denominator on every classifier
    run and report it as undefined, which is a denominator bug of the same
    shape this module exists to catch.

    The metric asks whether the entries the system auto-applied are right,
    not which pass happened to write them. `proposed_entries` is excluded:
    those proposals are unapplied by definition, routed instead to
    `REVIEW_REQUIRED`, and the denominator here is auto-applied entries.
    """
    return outcome.applied_entries + outcome.replayed_entries


def _entry_signature(template_id: str, legs: Iterable[tuple[str, int, int]]) -> tuple:
    """A template ID plus its legs as an order-independent, comparable key.

    Legs are sorted rather than compared in sequence because leg order is a
    presentation detail of the rendered table, not part of the entry's
    meaning — two entries posting the same debits and credits to the same
    accounts are the same correction.

    `account_name` is deliberately not compared: `account_code` is
    the foreign key and the name is derived from it through
    `pipeline.accounts`, which both the instantiator and the generator read,
    so comparing it would re-assert an equality that holds by construction
    while adding a way for a cosmetic rename to read as a wrong journal.
    """
    return (str(template_id), tuple(sorted(legs)))


def _candidate_signature(entry: CandidateJournalEntry) -> tuple:
    return _entry_signature(
        entry.template_id, ((leg.account_code, leg.debit, leg.credit) for leg in entry.legs)
    )


def _expected_signature(entry: ExpectedJournalEntry) -> tuple:
    return _entry_signature(
        entry.template_id, ((leg.account_code, int(leg.debit), int(leg.credit)) for leg in entry.legs)
    )


def count_matching_entries(
    applied: Sequence[CandidateJournalEntry], expected: Sequence[ExpectedJournalEntry]
) -> int:
    """How many auto-applied entries match an expected one, matching each at most once.

    A multiset match, not a set membership test: `expected_journal_entries`
    is plural because a single case can carry entries from more than one
    template, and two applied entries must not both be credited against
    one expected entry.
    """
    remaining: list[tuple] = [_expected_signature(entry) for entry in expected]
    matched = 0
    for entry in applied:
        signature = _candidate_signature(entry)
        if signature in remaining:
            remaining.remove(signature)
            matched += 1
    return matched


# --- The report. ---


class RunProvenance(BaseModel):
    """What the metrics JSON must carry alongside the numbers.

    Every field is required for reproducibility: seed, git SHA, model ID and
    the metrics JSON travel together as one pinned unit, the pinned model ID
    is recorded alongside seed and SHA, and cache hit rate is reported in
    the metrics JSON. Every field is caller-supplied and defaults to `None`.

    Defaulting rather than deriving is the point: a git SHA read from the
    working tree at metric time would be a different fact from the SHA of
    the committed run. The fields exist now so the JSON's shape does not
    change once a later stage fills them in.
    """

    model_config = ConfigDict(frozen=True)

    seed: int | None = None
    git_sha: str | None = None
    model_id: str | None = None
    cache_hit_rate: float | None = None
    snapshot_date: str | None = None
    """The batch snapshot, as an ISO date string. A parameter of the run,
    never a wall-clock read, and recorded because
    it decides the T+2 window and therefore the family-4 no-op population."""


class MetricsReport(BaseModel):
    """The full metric surface for one run against one ground-truth set.

    Every field below is a deterministic function of the run and the ground
    truth, so this model is what the byte-identical reproduce test
    compares. Wall-clock figures are in `PerformanceMetrics` instead.
    """

    model_config = ConfigDict(frozen=True)

    total_cases: int
    """Every `*_rate`'s denominator (the denominator convention)."""

    # Matching.
    match_rate: Rate
    """Cases the system placed in `AUTO_MATCHED` / total cases."""
    auto_match_recall: Rate
    """Correctly `AUTO_MATCHED` / ground-truth `AUTO_MATCHED`."""
    auto_match_precision: Rate
    """Correctly `AUTO_MATCHED` / all cases the system marked `AUTO_MATCHED`."""
    false_match_rate: Rate
    """Marked `AUTO_MATCHED` where ground truth is not / **total cases**. Primary
    safety metric for matching, and not the complement of `auto_match_precision`
    — the denominators differ, as the denominator convention states outright."""

    # Adjustment.
    auto_close_recall: Rate
    """Correctly `AUTO_CLOSED` / ground-truth `AUTO_CLOSED`."""
    auto_close_precision: Rate
    """Auto-applied **entries** matching ground truth / all auto-applied entries.
    Primary safety metric for adjustment. The denominator is entries, not cases,
    so recognition entries on `EXTERNAL_ACTION_REQUIRED` cases would be
    counted; that recognition path is not built in v1, so on
    this batch every auto-applied entry belongs to an `AUTO_CLOSED` case."""

    # Classification.
    state_prediction_accuracy: Rate
    """Predicted terminal state == expected terminal state / total cases."""
    subtype_metrics: tuple[SubtypeMetrics, ...]
    """Per-subtype precision and recall with denominators visible, over
    `GRADED_SUBTYPES` in declaration order."""
    exception_subtype_precision_macro: MacroRate
    """Unweighted mean of the seven per-subtype precisions."""
    exception_subtype_recall_macro: MacroRate
    """Unweighted mean of the seven per-subtype recalls. The ablation report
    reports its delta on this figure."""

    # Deferral.
    declined_by_policy_rate: Rate
    """`REVIEW_REQUIRED` with `decline_reason = policy` / total cases. Correct
    behaviour under v1 scope, not a failure; the expected range is around
    11.3% by construction, and a large deviation reads as a policy-routing bug."""
    declined_by_confidence_rate: Rate
    """`REVIEW_REQUIRED` with `decline_reason = confidence` / total cases. Has no
    ground-truth population by construction: every ground-truth
    `REVIEW_REQUIRED` case is a policy decline, and confidence declines surface
    as ground-truth `AUTO_CLOSED` cases the system declined, already penalised by
    `auto_close_recall`."""
    abstention_rate: Rate
    """`ABSTAINED` / total cases, read against an expected 8-18% operating range."""
    deferred_to_human_rate: Rate
    """(`ABSTAINED` + `REVIEW_REQUIRED`) / total cases."""
    open_case_rate: Rate
    """Cases not in (`AUTO_MATCHED` + `AUTO_CLOSED`) / total cases, including
    `EXTERNAL_ACTION_REQUIRED`, which counts as unresolved."""

    # Value.
    value_coverage: Rate
    """Paise in (`AUTO_MATCHED` + `AUTO_CLOSED`) cases / paise across all cases.
    Numerator and denominator are integer paise; see `case_value_paise`
    for what one case is worth."""

    # Distributions, for the hand-check and for the confusion matrices.
    predicted_state_counts: dict[str, int]
    ground_truth_state_counts: dict[str, int]
    """The batch-totals table, recomputed from the labels. A checkpoint
    asserts it equals that table exactly."""
    classification_counts: dict[str, int]
    """How often each `SubtypeLabel` was assigned, `AMBIGUOUS_CASE` included, so
    the seven precision denominators can be reconciled against the cases Slot A
    actually saw."""
    auto_applied_entry_count: int
    """`auto_close_precision`'s denominator, restated as a plain integer."""

    attachment: AttachmentAudit | None = None
    """What each attached credit rests on (`pipeline.attachment`).

    Not a metric here and not graded against ground truth. Every rate above
    compares terminal *states*, so a settlement that attached the wrong credit
    still reports clean as long as the amount struck its residual to zero. This
    is the one figure denominated in *evidence* rather than in labels, and its
    load-bearing row is `contradicted`, which must be empty."""

    bank_line_accounting: BankLineAccounting | None = None
    """Where every bank line went (`pipeline.bank_accounting`).

    Not a metric here and not graded against ground truth — every rate above
    is denominated in **cases**, and this is the one figure denominated in
    *source records*, which is exactly the gap a bank line reaching no case
    fell through. It lives here rather than beside `PerformanceMetrics`
    because, unlike a wall-clock read, it is a deterministic function of the
    run and so belongs inside the artifact that is compared byte-for-byte.

    Optional so that every caller predating it — and every test that builds a
    `MetricsReport` directly — still constructs one; `compute_metrics` fills it
    whenever it is given the batch's bank lines."""

    provenance: RunProvenance


class PerformanceMetrics(BaseModel):
    """`throughput` and `end_to_end_latency`.

    **Separate from `MetricsReport` on purpose.** The spec lists these beside
    the correctness metrics, but the committed metrics JSON must
    reproduce byte-identically on a clean clone and a wall-clock figure
    cannot do that on different hardware — or on the same hardware twice.
    Keeping them in one document would force a choice between a reproduce
    test that excludes fields (weakening "byte-identical") and a
    latency figure that is not really measured. Two documents keep both
    literally true, and these are treated as "reported, no target."

    Nothing here reads a clock. `elapsed_seconds` is measured by the caller
    that ran the batch and handed in, and `pipeline/` holds no clock read
    at all as a result.
    """

    model_config = ConfigDict(frozen=True)

    case_count: int
    elapsed_seconds: float
    throughput_cases_per_second: float | None
    """Cases per second, measured on the **scale** batch,
    not the reference batch; the hardware is stated alongside it."""
    hardware: str | None = None
    """The hardware must be stated alongside the throughput and latency figures."""


def performance_metrics(
    *, case_count: int, elapsed_seconds: float, hardware: str | None = None
) -> PerformanceMetrics:
    """`PerformanceMetrics` from a duration the caller measured."""
    if case_count < 0 or elapsed_seconds < 0:
        raise MetricsError("case count and elapsed time must be non-negative")
    return PerformanceMetrics(
        case_count=case_count,
        elapsed_seconds=elapsed_seconds,
        throughput_cases_per_second=(case_count / elapsed_seconds) if elapsed_seconds else None,
        hardware=hardware,
    )


def align_ground_truth(
    cases: Sequence[Case], ground_truth: Sequence[GroundTruthCase]
) -> dict[str, GroundTruthCase]:
    """Ground truth re-keyed by the `case_id` the pipeline actually assembled.

    **The two sides do not agree on orphan case IDs, and cannot.** A
    settlement-anchored case joins trivially — every population's
    `case_id` is its settlement's ID, and `pipeline.case_assembly` independently
    arrives at the same string (`case_id == settlement.id`). An orphan case
    has no such shared anchor: the generator mints `orphan_<hex>` from its own
    RNG when it plants the population, while case assembly synthesizes
    `case_orphan_<lowest line_id>` from the lines it finds, having never seen
    the generator (it is forbidden from importing one). Neither ID is wrong.
    There is simply no orphan identifier both sides can derive independently,
    and joining the 25 orphan cases on `case_id` silently drops every one of
    them — a sixth of the batch, and the sixth the one graded LLM slot is
    mostly about.

    The ground-truth schema already carries the join.
    `expected_linked_source_records` is "a list of record IDs across all
    three sources", and `generator/orphans.py` populates it with the
    `bank_line.line_id`s of the case's own lines — one for
    `UNMATCHED_INBOUND_CREDIT`, the opaque-narration ambiguous population
    and `REVERSAL_UNMATCHED`, two for a `DUPLICATE_CREDIT` pair. Those line
    IDs are the same records case assembly grouped, so the record is the
    anchor and the ID is derived from it, the same direction a case is
    defined in throughout.

    Strict on every ambiguity, because a silent mis-join is a denominator
    error wearing a different hat:

    - a cited line resolving to no assembled orphan case is an error;
    - a cited line resolving to a *settlement-anchored* case is an error (only
      orphan lines are indexed, so a settlement credit cited here means the
      populations have crossed);
    - a ground-truth case whose cited lines resolve to two different assembled
      cases is an error — that is the one-case-per-duplicate-pair granularity
      rule failing, and picking either one would report a passing metric
      over a broken split;
    - two ground-truth cases resolving to one assembled case is an error.
    """
    assembled = {case.case_id for case in cases}
    orphan_case_by_line: dict[str, str] = {}
    for case in cases:
        if case.kind is CaseKind.ORPHAN:
            for line in case.bank_lines:
                orphan_case_by_line[line.line_id] = case.case_id

    resolved: dict[str, GroundTruthCase] = {}
    for truth in ground_truth:
        if truth.case_id in assembled:
            case_id = truth.case_id
        else:
            targets = {
                orphan_case_by_line[record_id]
                for record_id in truth.expected_linked_source_records
                if record_id in orphan_case_by_line
            }
            if not targets:
                raise MetricsError(
                    f"ground-truth case {truth.case_id!r} matches no assembled case, by ID or by "
                    f"any of its {len(truth.expected_linked_source_records)} linked source record(s)"
                )
            if len(targets) > 1:
                raise MetricsError(
                    f"ground-truth case {truth.case_id!r} spans {len(targets)} assembled cases "
                    f"{sorted(targets)} — one-case-per-duplicate-pair granularity does not hold"
                )
            case_id = targets.pop()
        if case_id in resolved:
            raise MetricsError(
                f"assembled case {case_id!r} is claimed by two ground-truth cases: "
                f"{resolved[case_id].case_id!r} and {truth.case_id!r}"
            )
        resolved[case_id] = truth
    return resolved


def _aligned(
    cases: Sequence[Case],
    outcomes: Sequence[CaseOutcome],
    ground_truth: Sequence[GroundTruthCase],
) -> tuple[dict[str, Case], dict[str, CaseOutcome], dict[str, GroundTruthCase]]:
    """The three views keyed by the assembled `case_id`, having proved they cover the same cases."""
    by_case = {case.case_id: case for case in cases}
    by_outcome = {outcome.case_id: outcome for outcome in outcomes}

    for name, seen, source in (
        ("cases", len(by_case), len(cases)),
        ("outcomes", len(by_outcome), len(outcomes)),
    ):
        if seen != source:
            raise MetricsError(f"duplicate case_id in {name}: {source} records, {seen} distinct ids")
    if len({truth.case_id for truth in ground_truth}) != len(ground_truth):
        raise MetricsError(f"duplicate case_id in ground truth: {len(ground_truth)} records")

    if not by_outcome.keys() <= by_case.keys():
        orphaned = sorted(by_outcome.keys() - by_case.keys())
        raise MetricsError(f"{len(orphaned)} outcome(s) name no assembled case: {orphaned[:5]}")

    by_truth = align_ground_truth(cases, ground_truth)
    if by_outcome.keys() != by_truth.keys():
        missing = sorted(by_truth.keys() - by_outcome.keys())
        extra = sorted(by_outcome.keys() - by_truth.keys())
        raise MetricsError(
            f"run and ground truth describe different batches: "
            f"{len(missing)} unscored case(s) {missing[:5]}, {len(extra)} unlabelled case(s) {extra[:5]}"
        )
    return by_case, by_outcome, by_truth


def _state_counts(states: Iterable[OutcomeState]) -> dict[str, int]:
    """Counts over all five states, zeros included, in their declared order.

    Absent states are present as `0` rather than missing: a state distribution
    with four keys reads as a four-state system, and there are five.
    """
    counts = {str(state): 0 for state in OutcomeState}
    for state in states:
        counts[str(state)] += 1
    return counts


def compute_metrics(
    cases: Sequence[Case],
    outcomes: Sequence[CaseOutcome],
    ground_truth: Sequence[GroundTruthCase],
    *,
    provenance: RunProvenance | None = None,
    bank_line_accounting: BankLineAccounting | None = None,
    attachment: AttachmentAudit | None = None,
) -> MetricsReport:
    """The full metric surface for one run.

    `cases` supplies `value_coverage`'s per-case value and nothing else;
    `outcomes` is what the system decided; `ground_truth` is what was
    injected ("labels come from the injection plan"). All three must
    describe the same batch — see `_aligned`.
    """
    by_case, by_outcome, by_truth = _aligned(cases, outcomes, ground_truth)
    total = len(by_outcome)

    predicted = {case_id: outcome.state for case_id, outcome in by_outcome.items()}
    expected = {case_id: truth.expected_outcome_state for case_id, truth in by_truth.items()}

    def predicted_in(state: OutcomeState) -> set[str]:
        return {case_id for case_id, value in predicted.items() if value is state}

    def expected_in(state: OutcomeState) -> set[str]:
        return {case_id for case_id, value in expected.items() if value is state}

    predicted_matched = predicted_in(OutcomeState.AUTO_MATCHED)
    expected_matched = expected_in(OutcomeState.AUTO_MATCHED)
    predicted_closed = predicted_in(OutcomeState.AUTO_CLOSED)
    expected_closed = expected_in(OutcomeState.AUTO_CLOSED)

    # Matching.
    match_rate = rate(len(predicted_matched), total)
    correct_matched = predicted_matched & expected_matched
    auto_match_recall = rate(len(correct_matched), len(expected_matched))
    auto_match_precision = rate(len(correct_matched), len(predicted_matched))
    false_match_rate = rate(len(predicted_matched - expected_matched), total)

    # Adjustment.
    auto_close_recall = rate(len(predicted_closed & expected_closed), len(expected_closed))
    applied_total = 0
    applied_correct = 0
    for case_id, outcome in by_outcome.items():
        applied = auto_applied_entries(outcome)
        applied_total += len(applied)
        applied_correct += count_matching_entries(applied, by_truth[case_id].expected_journal_entries)
    auto_close_precision = rate(applied_correct, applied_total)

    # Classification.
    state_prediction_accuracy = rate(
        sum(1 for case_id in by_outcome if predicted[case_id] is expected[case_id]), total
    )
    subtype_metrics = _subtype_metrics(by_outcome, by_truth)
    precision_macro = _macro_average([metric.precision for metric in subtype_metrics])
    recall_macro = _macro_average([metric.recall for metric in subtype_metrics])

    # Deferral.
    review = predicted_in(OutcomeState.REVIEW_REQUIRED)
    abstained = predicted_in(OutcomeState.ABSTAINED)
    declined = {
        reason: sum(1 for outcome in by_outcome.values() if outcome.decline_reason is reason)
        for reason in DeclineReason
    }

    # Value.
    covered_paise = sum(case_value_paise(by_case[case_id]) for case_id in predicted_matched | predicted_closed)
    total_paise = sum(case_value_paise(by_case[case_id]) for case_id in by_outcome)

    return MetricsReport(
        bank_line_accounting=bank_line_accounting,
        attachment=attachment,
        total_cases=total,
        match_rate=match_rate,
        auto_match_recall=auto_match_recall,
        auto_match_precision=auto_match_precision,
        false_match_rate=false_match_rate,
        auto_close_recall=auto_close_recall,
        auto_close_precision=auto_close_precision,
        state_prediction_accuracy=state_prediction_accuracy,
        subtype_metrics=subtype_metrics,
        exception_subtype_precision_macro=precision_macro,
        exception_subtype_recall_macro=recall_macro,
        declined_by_policy_rate=rate(declined[DeclineReason.POLICY], total),
        declined_by_confidence_rate=rate(declined[DeclineReason.CONFIDENCE], total),
        abstention_rate=rate(len(abstained), total),
        deferred_to_human_rate=rate(len(abstained | review), total),
        open_case_rate=rate(total - len(predicted_matched | predicted_closed), total),
        value_coverage=rate(covered_paise, total_paise),
        predicted_state_counts=_state_counts(predicted.values()),
        ground_truth_state_counts=_state_counts(expected.values()),
        classification_counts=_classification_counts(by_outcome.values()),
        auto_applied_entry_count=applied_total,
        provenance=provenance or RunProvenance(),
    )


def _subtype_metrics(
    by_outcome: Mapping[str, CaseOutcome], by_truth: Mapping[str, GroundTruthCase]
) -> tuple[SubtypeMetrics, ...]:
    """Per-subtype precision and recall over `GRADED_SUBTYPES`.

    Both denominators are computed over the whole batch, not over the ~70
    cases Slot A saw. A ground-truth `DUPLICATE_CREDIT` case the system
    auto-closed instead is a recall miss and must count as one; restricting
    the denominator to cases the classifier was asked about would make
    recall blind to exactly the cases that never reached it.
    """
    assigned = {case_id: assigned_subtype(outcome) for case_id, outcome in by_outcome.items()}
    truth = {case_id: ground_truth_subtype(by_truth[case_id]) for case_id in by_outcome}
    return tuple(
        SubtypeMetrics(
            subtype=subtype,
            precision=rate(
                sum(
                    1
                    for case_id in by_outcome
                    if assigned[case_id] is subtype and truth[case_id] is subtype
                ),
                sum(1 for case_id in by_outcome if assigned[case_id] is subtype),
            ),
            recall=rate(
                sum(
                    1
                    for case_id in by_outcome
                    if truth[case_id] is subtype and assigned[case_id] is subtype
                ),
                sum(1 for case_id in by_outcome if truth[case_id] is subtype),
            ),
        )
        for subtype in GRADED_SUBTYPES
    )


def _classification_counts(outcomes: Iterable[CaseOutcome]) -> dict[str, int]:
    """How often each of Slot A's eight labels was assigned, zeros included."""
    counts = {str(label): 0 for label in SubtypeLabel}
    for outcome in outcomes:
        if outcome.classified_subtype is not None:
            counts[str(outcome.classified_subtype)] += 1
    return counts
