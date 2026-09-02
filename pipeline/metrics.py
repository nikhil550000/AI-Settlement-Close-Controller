"""The §1.6 metric surface, computed against ground truth. Component 9, first half.

> **Reporter.** Metric surface against ground truth, the five §1.8
> artifacts, single-file HTML per FR-11.

This module is the metric surface only. §5.2's two confusion matrices, the
per-subtype breakdown as a rendered table, the held-out run and the §5.5
threshold review are session 6.2's; the FR-11 HTML that renders any of it
is session 6.3's. What is here is the arithmetic those three depend on.

**Denominators are the whole job.** REV-01, REV-09, REV-10 and REV-20 each
corrected a metric whose name and denominator disagreed, and §6.3 assigns
this session the strongest model for exactly that reason: "denominator
errors are the exact defect class REV-01 and REV-09 corrected, and shipping
numbers that overstate the system is the one failure the judging bar names
directly." Three consequences run through every model below.

1. **Every rate carries its own numerator and denominator as integers**
   (`Rate`), and the float is derived from them. A metric is never a bare
   float whose denominator has to be reconstructed from a different part of
   the report to be checked.
2. **A zero denominator produces `value=None`, never `0.0`.** "No case was
   predicted `DUPLICATE_CREDIT`, so precision is undefined" and "every case
   predicted `DUPLICATE_CREDIT` was wrong, so precision is 0.0" are
   different findings, and collapsing them flatters the system.
3. **§1.6's denominator convention is transcribed, not paraphrased.**
   `*_rate` denominators are total cases; `*_recall` denominators are the
   ground-truth population for that state; `*_precision` denominators are
   the population the system predicted. Each metric names its own
   convention in its docstring, so a reader checking one does not have to
   hold §1.6's table in their head.

**The macro average is over seven subtypes, `DISPUTE_PENDING` included**
(§5.2, REV-25). v0.8 stated this two ways — §5.2's headline said seven while
the §3.6 clause it cited said six — and six cannot divide the 36
`EXTERNAL_ACTION_REQUIRED` cases, which §3.5/§3.6 allocate 5/5/4/5/8/6/3
across all seven. Corrected in REV-25 before any of this was written.

**Wall-clock metrics live in `PerformanceMetrics`, deliberately outside
`MetricsReport`.** §1.6 lists `throughput` and `end_to_end_latency`
alongside the correctness metrics, but §5.6.3 requires the committed
metrics JSON to reproduce **byte-identically** on a clean clone, and a
wall-clock figure cannot. Splitting them keeps both requirements literally
true instead of quietly weakening one; see `PerformanceMetrics` for the
full reasoning and what it hands session 7.2.

Nothing here reads a clock, and nothing here divides money. Numerators and
denominators stay integer paise (`value_coverage`) or integer case counts;
a ratio is a dimensionless measure of the two, computed once, at the end.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict

from pipeline.apply import CaseOutcome
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

    Raised rather than tolerated: every §1.6 metric has a case-count
    denominator (§2.2), so a case present on one side and missing on the
    other silently changes a denominator, which is the one failure mode
    this module exists to prevent.
    """


GRADED_SUBTYPES: tuple[SubtypeLabel, ...] = OPERATIONAL_SUBTYPES
"""The seven `OPERATIONAL_EXCEPTION` subtypes the macro average covers (§5.2, REV-25).

Aliased to `pipeline.exception_class.OPERATIONAL_SUBTYPES` (session 6.2) so
the tuple the macro averages over and the tuple the class assignment reads as
`OPERATIONAL_EXCEPTION` are one object and cannot drift apart.

`SubtypeLabel`'s declaration order is §3.3's table order followed by
`DISPUTE_PENDING`, the "seventh subtype" §3.3 names in the sentence beneath
that table, so this tuple is §3.3's own ordering with nothing reshuffled.

`AMBIGUOUS_CASE` is excluded because it is a *class* in §3.3's taxonomy, not
a subtype beneath `OPERATIONAL_EXCEPTION`: its ground-truth subtype is
`NONE`, so it has no per-subtype population to be precise or recallful
about. It is graded on the class axis instead, by §5.2's exception-class
confusion matrix (session 6.2). It remains one of Slot A's eight decodable
values, and `classification_counts` below reports how often it was assigned,
so the seven precision denominators stay auditable against the 70 cases
Slot A actually saw.
"""

_SUBTYPE_FROM_GROUND_TRUTH: dict[ExceptionSubtype, SubtypeLabel] = {
    ExceptionSubtype(label.value): label for label in GRADED_SUBTYPES
}
"""§1.6's `ground_truth_exception_subtype` narrowed to Slot A's vocabulary.

`ExceptionSubtype` carries ten members; `NONE`, `OMISSION` and `MISPOSTING`
are `ACCOUNTING_CORRECTION`-side (or the clean sentinel) and are not values
Slot A may emit, so a case carrying one has no graded subtype at all and
enters no per-subtype denominator on either side. Session 5.1 recorded the
same asymmetry as a disclosed gap in the baseline's vocabulary; here it is
the reason those cases are excluded from the subtype metrics rather than
counted as misses against a subtype that was never available.
"""


def ground_truth_subtype(truth: GroundTruthCase) -> SubtypeLabel | None:
    """This case's graded subtype, or `None` if it has none (§3.3's `NONE`/`OMISSION`/`MISPOSTING`)."""
    return _SUBTYPE_FROM_GROUND_TRUTH.get(truth.ground_truth_exception_subtype)


def assigned_subtype(outcome: CaseOutcome) -> SubtypeLabel | None:
    """The subtype the system assigned this case, or `None` if it assigned none.

    Component 5's output (`CaseOutcome.classified_subtype`), not component
    4's fired triggers: §1.6 and §5.2 grade the *classification*, and §4.2
    puts subtype classification in Slot A. `None` covers both the ~80
    `AUTO_CLOSED`/`AUTO_MATCHED` cases Slot A never sees (§4.2 scopes it to
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
    paise, per NFR-04, and the ratio is dimensionless either way.
    """

    model_config = ConfigDict(frozen=True)

    numerator: int
    denominator: int
    value: float | None

    @property
    def is_defined(self) -> bool:
        return self.value is not None


class MacroRate(BaseModel):
    """An unweighted mean over per-subtype rates (§5.2's macro average).

    Deliberately **not** a `Rate`. A mean of ratios is not itself a ratio of
    two integers, and expressing it as one produced a committed artifact that
    contradicted itself on its face — `{"numerator": 7, "denominator": 7,
    "value": 0.80}`, where 7/7 is 1.00. `Rate`'s own docstring makes the two
    integers "the auditable part" and derives the float from them; a macro
    that borrowed the shape while breaking that derivation defeated the one
    discipline this module exists to enforce.

    So the two counts travel under names that say what they actually are:
    how many subtypes entered the mean, and how many were eligible to. A
    macro computed over five of seven subtypes still says so on its face —
    §5.2's reason for wanting them visible — without claiming to be a ratio.
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
    computed over five of seven subtypes says so on its face. §5.2 requires
    the per-subtype denominators be visible precisely because they are thin;
    a macro that silently drops a subtype would defeat that.
    """
    defined = [r.value for r in rates if r.value is not None]
    return MacroRate(
        value=(sum(defined) / len(defined)) if defined else None,
        subtypes_averaged=len(defined),
        subtypes_eligible=len(rates),
    )


class SubtypeMetrics(BaseModel):
    """One subtype's precision and recall, with both denominators visible (§5.2)."""

    model_config = ConfigDict(frozen=True)

    subtype: SubtypeLabel
    precision: Rate
    """Among cases the system assigned S, the fraction whose ground truth is S."""
    recall: Rate
    """Among cases whose ground truth is S, the fraction the system assigned S."""


# --- Per-case value, for `value_coverage`. ---


def case_value_paise(case: Case) -> int:
    """The rupee value at stake in one case, in integer paise.

    §1.6 defines `value_coverage` over "rupee value in cases" without
    fixing what one case's value is, and §1.2's two anchor kinds have
    different natural answers, so both are stated here rather than left
    implicit at the call site:

    - **Settlement-anchored** — `settlement.amount`, the net figure
      Razorpay owes for the batch. Not the gross of its recon lines: the
      settlement is the thing being reconciled against the bank, and §3.5's
      own hard invariant (`settlement.amount == sum(credits) − sum(debits)
      − fees − tax`) makes it the one figure that already nets the case.
    - **Orphan** — the total magnitude across the case's bank lines,
      `deposit + withdrawal` per line. Magnitude, not a signed net, because
      a `REVERSAL_UNMATCHED` case is a withdrawal and a `DUPLICATE_CREDIT`
      case spans two credits (REV-18); netting either to zero or to one leg
      would understate the money a reviewer has to account for.

    Orphan cases can never reach `AUTO_MATCHED` or `AUTO_CLOSED` (§3.6 —
    they are unresolvable by construction), so they only ever land in the
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

    `applied_entries` alone is not the population §1.6 means. `run_batch`
    calls `apply_batch` twice whenever a classifier is supplied (session
    5.2): the first pass posts, and the second recognises the identical
    `(case_id, resolution_id)` under invariant 1.7.4 and replays rather than
    reposting — so on the `BatchOutcome` a caller actually receives, an
    auto-closed case's entries sit in `replayed_entries` and
    `applied_entries` is empty. Reading only the latter would compute
    `auto_close_precision` over an empty denominator on every classifier
    run and report it as undefined, which is a denominator bug of exactly
    the REV-10 shape.

    The metric asks whether the entries the system auto-applied are right,
    not which pass happened to write them. `proposed_entries` is excluded:
    FR-07 proposals are unapplied by definition (§2.5 routes them to
    `REVIEW_REQUIRED`), and §1.6's denominator is auto-applied entries.
    """
    return outcome.applied_entries + outcome.replayed_entries


def _entry_signature(template_id: str, legs: Iterable[tuple[str, int, int]]) -> tuple:
    """A template ID plus its legs as an order-independent, comparable key.

    Legs are sorted rather than compared in sequence because leg order is a
    presentation detail of §3.4's table, not part of the entry's meaning —
    two entries posting the same debits and credits to the same accounts
    are the same correction.

    `account_name` is deliberately not compared: §3.1 makes `account_code`
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

    A multiset match, not a set membership test: §1.6 makes
    `expected_journal_entries` plural because "a single case can carry
    entries from more than one template", and two applied entries must not
    both be credited against one expected entry.
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

    Every field is required somewhere in the spec — §5.6.1 and FR-13 (seed,
    git SHA, model ID, metrics JSON as one pinned unit), §4.4's assumption
    callout (the pinned model ID recorded in the metrics JSON alongside seed
    and SHA), §4.3 point 3 (cache hit rate reported in the metrics JSON) —
    and every field is caller-supplied and defaults to `None`.

    Defaulting rather than deriving is the point: session 7.2 owns the FR-13
    pin, and a git SHA read from the working tree at metric time would be a
    different fact from the SHA of the committed run. The fields exist now
    so the JSON's shape does not change when 7.2 fills them.
    """

    model_config = ConfigDict(frozen=True)

    seed: int | None = None
    git_sha: str | None = None
    model_id: str | None = None
    cache_hit_rate: float | None = None
    snapshot_date: str | None = None
    """The §3.3 batch snapshot, as an ISO date string. A parameter of the run,
    never a wall-clock read (AGENT.md's determinism rules), and recorded because
    it decides the T+2 window and therefore the family-4 no-op population."""


class MetricsReport(BaseModel):
    """The full §1.6 metric surface for one run against one ground-truth set.

    Every field below is a deterministic function of the run and the ground
    truth, so this model is what §5.6.3's byte-identical reproduce test
    compares. Wall-clock figures are in `PerformanceMetrics` instead.
    """

    model_config = ConfigDict(frozen=True)

    total_cases: int
    """Every `*_rate`'s denominator (§1.6's denominator convention)."""

    # Matching (§1.6).
    match_rate: Rate
    """Cases the system placed in `AUTO_MATCHED` / total cases (REV-01)."""
    auto_match_recall: Rate
    """Correctly `AUTO_MATCHED` / ground-truth `AUTO_MATCHED` (REV-01)."""
    auto_match_precision: Rate
    """Correctly `AUTO_MATCHED` / all cases the system marked `AUTO_MATCHED` (REV-09)."""
    false_match_rate: Rate
    """Marked `AUTO_MATCHED` where ground truth is not / **total cases**. Primary
    safety metric for matching, and not the complement of `auto_match_precision`
    — the denominators differ, as §1.6's convention note states outright."""

    # Adjustment (§1.6).
    auto_close_recall: Rate
    """Correctly `AUTO_CLOSED` / ground-truth `AUTO_CLOSED` (REV-09)."""
    auto_close_precision: Rate
    """Auto-applied **entries** matching ground truth / all auto-applied entries.
    Primary safety metric for adjustment. The denominator is entries, not cases,
    per REV-10, so FR-05 recognition entries on `EXTERNAL_ACTION_REQUIRED` cases
    would be counted; FR-05 is not built in v1 (§2.4's stated fallback), so on
    this batch every auto-applied entry belongs to an `AUTO_CLOSED` case."""

    # Classification (§1.6, §5.2).
    state_prediction_accuracy: Rate
    """Predicted terminal state == expected terminal state / total cases."""
    subtype_metrics: tuple[SubtypeMetrics, ...]
    """Per-subtype precision and recall with denominators visible (§5.2), over
    `GRADED_SUBTYPES` in §3.3's order."""
    exception_subtype_precision_macro: MacroRate
    """Unweighted mean of the seven per-subtype precisions (§5.2, REV-25)."""
    exception_subtype_recall_macro: MacroRate
    """Unweighted mean of the seven per-subtype recalls (§5.2, REV-25). The §5.4
    ablation reports its delta on this figure."""

    # Deferral (§1.6, REV-02, REV-09).
    declined_by_policy_rate: Rate
    """`REVIEW_REQUIRED` with `decline_reason = policy` / total cases. Correct
    behaviour under v1 scope (§2.5), not a failure; §5.5 expects ≈ 11.3% by
    construction and reads a large deviation as a policy-routing bug."""
    declined_by_confidence_rate: Rate
    """`REVIEW_REQUIRED` with `decline_reason = confidence` / total cases. Has no
    ground-truth population by construction (§1.6): every ground-truth
    `REVIEW_REQUIRED` case is a policy decline, and confidence declines surface
    as ground-truth `AUTO_CLOSED` cases the system declined, already penalised by
    `auto_close_recall`."""
    abstention_rate: Rate
    """`ABSTAINED` / total cases, read against §5.5's 8–18% operating range."""
    deferred_to_human_rate: Rate
    """(`ABSTAINED` + `REVIEW_REQUIRED`) / total cases."""
    open_case_rate: Rate
    """Cases not in (`AUTO_MATCHED` + `AUTO_CLOSED`) / total cases, including
    `EXTERNAL_ACTION_REQUIRED` — §1.3 calls those unresolved (REV-09)."""

    # Value (§1.6).
    value_coverage: Rate
    """Paise in (`AUTO_MATCHED` + `AUTO_CLOSED`) cases / paise across all cases.
    Numerator and denominator are integer paise (NFR-04); see `case_value_paise`
    for what one case is worth."""

    # Distributions, for the hand-check and for §5.2's matrices (session 6.2).
    predicted_state_counts: dict[str, int]
    ground_truth_state_counts: dict[str, int]
    """§3.6's batch-totals table, recomputed from the labels. This session's
    checkpoint asserts it equals that table exactly."""
    classification_counts: dict[str, int]
    """How often each `SubtypeLabel` was assigned, `AMBIGUOUS_CASE` included, so
    the seven precision denominators can be reconciled against the cases Slot A
    actually saw."""
    auto_applied_entry_count: int
    """`auto_close_precision`'s denominator, restated as a plain integer."""

    provenance: RunProvenance


class PerformanceMetrics(BaseModel):
    """`throughput` and `end_to_end_latency` (§1.6, NFR-02, NFR-03).

    **Separate from `MetricsReport` on purpose.** §1.6 lists these beside the
    correctness metrics, but §5.6.3 requires the committed metrics JSON to
    reproduce byte-identically on a clean clone and a wall-clock figure
    cannot do that on different hardware — or on the same hardware twice.
    Keeping them in one document would force a choice between a reproduce
    test that excludes fields (weakening NFR-06's "byte-identical") and a
    latency figure that is not really measured. Two documents keep both
    literally true, and §5.5 already treats these as "reported, no target."

    Nothing here reads a clock. `elapsed_seconds` is measured by the caller
    that ran the batch and handed in; §6.3 puts the scale-batch throughput
    run in session 7.1 and the pin in 7.2, and `pipeline/` holds no clock
    read at all as a result.
    """

    model_config = ConfigDict(frozen=True)

    case_count: int
    elapsed_seconds: float
    throughput_cases_per_second: float | None
    """Cases per second. NFR-02 measures this on the **scale** batch (FR-02),
    not the reference batch; the hardware is stated alongside it."""
    hardware: str | None = None
    """NFR-02/NFR-03 both require the hardware be stated with the figure."""


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
    settlement-anchored case joins trivially — §3.5 makes every population's
    `case_id` its settlement's ID, and `pipeline.case_assembly` independently
    arrives at the same string (`case_id == settlement.id`). An orphan case
    has no such shared anchor: the generator mints `orphan_<hex>` from its own
    RNG when it plants the population, while case assembly synthesizes
    `case_orphan_<lowest line_id>` from the lines it finds, having never seen
    the generator (§4.1 forbids it from importing one). Neither ID is wrong.
    There is simply no orphan identifier both sides can derive independently,
    and joining the 25 orphan cases on `case_id` silently drops every one of
    them — a sixth of the batch, and the sixth §4.2's one graded LLM slot is
    mostly about.

    §1.6's schema already carries the join. `expected_linked_source_records`
    is "a list of record IDs across all three sources", and
    `generator/orphans.py` populates it with the `bank_line.line_id`s of the
    case's own lines — one for `UNMATCHED_INBOUND_CREDIT`, the opaque-narration
    ambiguous population and `REVERSAL_UNMATCHED`, two for a `DUPLICATE_CREDIT`
    pair (REV-18). Those line IDs are the same records case assembly grouped,
    so the record is the anchor and the ID is derived from it, which is the
    same direction §1.2 defines a case in.

    Strict on every ambiguity, because a silent mis-join is a denominator
    error wearing a different hat:

    - a cited line resolving to no assembled orphan case is an error;
    - a cited line resolving to a *settlement-anchored* case is an error (only
      orphan lines are indexed, so a settlement credit cited here means the
      populations have crossed);
    - a ground-truth case whose cited lines resolve to two different assembled
      cases is an error — that is REV-18's granularity rule failing, and
      picking either one would report a passing metric over a broken split;
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
                    f"{sorted(targets)} — REV-18's one-case-per-duplicate-pair granularity does not hold"
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
    """Counts over all five §1.3 states, zeros included, in §1.3's order.

    Absent states are present as `0` rather than missing: a state distribution
    with four keys reads as a four-state system, and §2.3's FR-03 ships five.
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
) -> MetricsReport:
    """The §1.6 surface for one run.

    `cases` supplies `value_coverage`'s per-case value and nothing else;
    `outcomes` is what the system decided; `ground_truth` is what was
    injected (§3.5's "labels come from the injection plan"). All three must
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
    """§5.2's per-subtype precision and recall over `GRADED_SUBTYPES`.

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
