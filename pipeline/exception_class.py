"""The four-class exception taxonomy, assigned by the pipeline to every case.

**Why this module exists at all.** The grading surface requires a 5x5
exception-class confusion matrix over the four classes plus `NONE`, and a
report of *categorized* cases — both need a predicted class per case, and
before this module there was none. `CaseOutcome` carried `state`,
`triggered_subtypes` and `classified_subtype`; the ground truth carries
`ground_truth_exception_class` with nothing on the other side to grade it
against.

**The answer, and the reason for it.** The class is assigned here, in
component 8's own vocabulary, from the same evidence `assign_state` reads —
not derived inside `pipeline/metrics.py` from the state the system already
predicted. Deriving it in the grader was the cheaper option and it was
rejected: a predicted class computed from the predicted state makes the
class matrix a re-rendering of its state matrix, carrying no information the
state matrix does not already carry, and the whole reason for a
two-level taxonomy is that outcome state answers *what the Controller did*;
exception class answers *what was actually wrong with the case*. Every case
carries both labels independently. A grader that invents the label it
grades is a metric that reads near-100% while measuring nothing. So the
label is a product of the run, the grader only compares it, and the two
axes stay independent by construction.

**This is a classification, not a decision.** Nothing downstream reads
`CaseOutcome.exception_class`; it changes no state, gates no posting, and
touches no money. `assign_state` is untouched by this module and calls
nothing in it. The class is a second label on a decision already made.

**Branch order mirrors `assign_state`'s, deliberately.** The two functions
answer different questions off one body of evidence, and where their
precedence would differ the difference would be a bug in one of them: a case
whose correction landed is `AUTO_CLOSED` *and* `ACCOUNTING_CORRECTION` for
the same reason, and `OPERATIONAL_EXCEPTION` ("a real discrepancy that
no journal entry can resolve") is by its own definition unreachable for a
case a journal entry did resolve. See `predict_exception_class` for the
branch-by-branch mapping.
"""

from __future__ import annotations

from collections.abc import Sequence

from pipeline.case_assembly import Case, CaseKind
from pipeline.ground_truth import ExceptionClass, ExceptionSubtype
from pipeline.matcher import MatchTier
from pipeline.subtype_label import SubtypeLabel

OPERATIONAL_SUBTYPES: tuple[SubtypeLabel, ...] = tuple(
    label for label in SubtypeLabel if label is not SubtypeLabel.AMBIGUOUS_CASE
)
"""The seven subtypes that sit beneath `OPERATIONAL_EXCEPTION`.

`SubtypeLabel`'s declaration order is the table order followed by
`DISPUTE_PENDING`, the "seventh subtype" named in the sentence beneath that
table. `AMBIGUOUS_CASE` is excluded because it is a *class*, not a
subtype: a case labelled `AMBIGUOUS_CASE` by Slot A is being placed in the
fourth class, not under the second. `pipeline.metrics.GRADED_SUBTYPES` is
this same tuple, aliased there so the macro average and the class assignment
cannot drift apart.
"""

_OPERATIONAL_SUBTYPE_SET = frozenset(OPERATIONAL_SUBTYPES)


def is_timing_attributed(case: Case) -> bool:
    """Whether this case's residual is explained by the timing-residual rule.

    True for exactly this shape — a settlement-anchored case
    with no bank credit found (tier 3, the no-match tier) whose settlement is
    still inside the T+2 working-day window as of the snapshot. That is the
    condition the matcher itself used to force `residual_paise` to 0, so
    reading it back here is reading the matcher's own attribution rather than
    re-deriving it: the residual is zero *because* of timing, which is the
    distinction between `EXPECTED_TIMING_DIFFERENCE` and the `NONE` of
    a case that simply matched.

    Without this the two populations sent to `AUTO_MATCHED` — 18 fully
    clean cases and 12 family-4 no-ops — would be indistinguishable on the
    class axis, and the positive classification of *this break is not a
    break*, which is what makes correct inaction gradeable rather than
    accidental, would have nothing to grade.
    """
    return (
        case.kind is CaseKind.SETTLEMENT_ANCHORED
        and case.match_tier == int(MatchTier.NO_MATCH)
        and case.in_settlement_window is True
    )


def predict_exception_class(
    *,
    declined_by_policy: bool,
    has_entries: bool,
    triggered_subtypes: Sequence[ExceptionSubtype],
    classified_subtype: SubtypeLabel | None,
    timing_attributed: bool,
    residual_paise: int,
) -> ExceptionClass:
    """The exception class for one case, from the evidence component 8 already holds.

    Taken as primitives rather than a `CaseOutcome` for the same reason
    `assign_state` is: `pipeline.apply` imports this module, so this module
    cannot import `pipeline.apply` back, and the parameter list is the honest
    statement of exactly which evidence the label depends on.

    The branches, in order, each against the taxonomy's own definition:

    1. **A policy exclusion is `ACCOUNTING_CORRECTION`.** These
       exclusions — tax positions and date-only
       reclassification — are accounting treatments the Controller declines
       to *post*, not discrepancies of a different kind, and the
       population map labels both `ACCOUNTING_CORRECTION` while sending them
       to `REVIEW_REQUIRED`. This is the case that proves
       the two axes are independent, so it comes first here just as it
       does in `assign_state`.
    2. **Any template instantiation is `ACCOUNTING_CORRECTION`** — applied,
       replayed, or proposed-and-declined. The error is fully
       determined by source evidence, and a journal entry against the fixed
       chart of accounts restores them. A candidate exists precisely when
       one of the six evidence predicates fired, and all six are
       `ACCOUNTING_CORRECTION` treatments. This outranks a fired subtype
       trigger for the reason `assign_state`'s branch 2 does: `OPERATIONAL_EXCEPTION`
       is defined as a discrepancy **no journal entry can
       resolve**, so a case one did resolve is not one.
    3. **A fired trigger, or an operational label from Slot A, is
       `OPERATIONAL_EXCEPTION`.** Component 4's triggers are the taxonomy's own
       trigger column; Slot A's seven non-`AMBIGUOUS_CASE` labels are the
       subtypes beneath this class. Either is a positive identification of
       what is wrong and who must act — the separating question.
    4. **A timing-attributed residual is `EXPECTED_TIMING_DIFFERENCE`**; see
       `is_timing_attributed`.
    5. **A zero residual with nothing above it is `NONE`** — the sentinel
       for "a fully clean case is none of the four."
    6. **Anything left is `AMBIGUOUS_CASE`**: a residual no template
       explains, no trigger categorises and no classifier could name. A
       required piece of evidence is absent, reached on evidence rather
       than as a default, the same fallthrough `assign_state`'s branch 6 is.
    """
    if declined_by_policy or has_entries:
        return ExceptionClass.ACCOUNTING_CORRECTION
    if triggered_subtypes or classified_subtype in _OPERATIONAL_SUBTYPE_SET:
        return ExceptionClass.OPERATIONAL_EXCEPTION
    if timing_attributed:
        return ExceptionClass.EXPECTED_TIMING_DIFFERENCE
    if residual_paise == 0:
        return ExceptionClass.NONE
    return ExceptionClass.AMBIGUOUS_CASE
