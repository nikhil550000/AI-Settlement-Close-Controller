"""The classifier, per spec.md §4.1 component 5.

> **Classifier.** Exception class and subtype assignment.

This session (5.1, §6.3) builds the two pieces §4.2 asks for in order —
"build the deterministic keyword baseline first, then Slot A on top of
it" — and stops at the baseline. Slot A itself (the graded LLM call,
constrained decoding, the SHA-keyed cache) is session 5.2's.

## What component 5 actually decides, and what it does not

Six of the seven `OPERATIONAL_EXCEPTION` subtypes already have a
deterministic answer by the time a case reaches this module:
`pipeline/predicates.py`'s `_settlement_anchored_triggers` and
`_orphan_triggers` fire `SETTLEMENT_UTR_MISSING`, `BANK_CREDIT_OVERDUE`,
`SETTLEMENT_AMOUNT_MISMATCH`, `DISPUTE_PENDING`, `REVERSAL_UNMATCHED` and
`DUPLICATE_CREDIT` as *facts*, not labels — component 4's own docstring
is explicit that evaluating is not assigning. Component 5's real job,
the one no arithmetic answers, is the single split §4.2 names outright:

> "`UNMATCHED_INBOUND_CREDIT` versus `AMBIGUOUS_CASE` on an orphan bank
> credit turns entirely on whether the free-text narration identifies a
> counterparty... and no residual computation decides it."

So `classify_case_baseline` below does two things, not one: it *adopts*
a fired trigger where one exists (component 5 assigning what component 4
already found, rather than a second competing detector disagreeing with
the first), and it *decides* the one open question — the narration read
— for cases with no trigger at all. Both are "classification" under
§4.1's job description; only the second is a judgment call.

**What this leaves unresolved, on purpose.** The 17 `REVIEW_REQUIRED`
cases (family-4 date-error, FR-06 tax) are `ACCOUNTING_CORRECTION` /
`OMISSION`-or-`MISPOSTING` in ground truth — subtypes that are not
members of Slot A's eight-value output space at all (§4.2: "the seven
`OPERATIONAL_EXCEPTION` subtypes plus `AMBIGUOUS_CASE`"). No trigger
fires on them and no narration signals a counterparty, so the baseline's
fallthrough assigns `AMBIGUOUS_CASE` — a forced wrong answer for those
17, not a bug: Slot A's vocabulary simply has no correct thing to say
about a policy-excluded correction, and `AMBIGUOUS_CASE` is documented
here as the least-wrong member of a fixed eight-value enum rather than
picked to look right. Phase 6's metric computation is the place that
must decide whether these 17 belong in `exception_subtype_precision`/
`recall`'s denominator at all; this module does not pre-judge that.

## The evidence bundle boundary

Invariant 1.7.2 keeps accounts and amounts out of the model's hands on
the money path; §4.2 draws the same boundary for Slot A ("it never sees
or emits an account, an amount, or a postable narration"). `EvidenceBundle`
is that boundary made concrete: every field is either a fact already
computed by an earlier component (a fired trigger, a match tier, whether
a template already fired) or free text a human would read to make this
exact call (a bank line's own narration) — never a paise figure, an
account code, or the constant ledger narration `pipeline/apply.py` posts.
The same bundle is what session 5.2's Slot A prompt is built from, so the
boundary is enforced once, here, rather than re-drawn per caller.

## The interface question this session does not answer

`pipeline/run.py`'s `KNOWN_GAPS` and BUILDLOG session 4.3's `Next` field
both flag that closing the `UNMATCHED_INBOUND_CREDIT` gap requires
`pipeline/apply.py`'s `assign_state` to read a classification alongside
`CaseEvidence.subtype_triggers`, and leave the timing of that wiring open
— "session 5.1's or 5.2's call". This session calls it: **not yet.**
`classify_batch_baseline` runs downstream of `apply_batch`, over its
`BatchOutcome` (component 5 needs to know which ~70 cases are *not*
`AUTO_CLOSED`, which is component 8's answer, not component 4's) and
produces classifications nobody yet consumes. Wiring the baseline back
into state assignment now would let an unvalidated ablation arm change
what `EXTERNAL_ACTION_REQUIRED` means before Slot A — the arm §5.4 grades
it against — exists to compare it with. Session 5.2 threads the real
classifier (baseline or Slot A) through `apply_batch` once both arms are
buildable and the choice is real rather than one-sided.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from pipeline.apply import CaseOutcome
from pipeline.case_assembly import Case, CaseKind
from pipeline.ground_truth import ExceptionSubtype, OutcomeState
from pipeline.predicates import CaseEvidence

_NON_AUTO_CLOSE_STATES = frozenset(
    {OutcomeState.REVIEW_REQUIRED, OutcomeState.EXTERNAL_ACTION_REQUIRED, OutcomeState.ABSTAINED}
)
"""§4.2's "~70 non-auto-close cases": every terminal state but the two closed-clean
ones, `AUTO_MATCHED` and `AUTO_CLOSED`. 150 - 30 - 50 = 70 exactly, by the §3.6
batch totals, regardless of seed — every population's count is fixed by §3.5/§3.6,
not drawn."""


class SubtypeLabel(StrEnum):
    """Slot A's eight-value output vocabulary (§4.2): the seven `OPERATIONAL_EXCEPTION`
    subtypes plus `AMBIGUOUS_CASE`.

    Deliberately its own type rather than `pipeline.ground_truth.ExceptionSubtype`
    reused whole: that enum carries ten members, including `NONE`, `OMISSION` and
    `MISPOSTING`, none of which Slot A may ever emit under constrained decoding.
    String values match `ExceptionSubtype`'s for the seven shared members, so a
    fired trigger converts to a label with no translation table.
    """

    SETTLEMENT_UTR_MISSING = "SETTLEMENT_UTR_MISSING"
    BANK_CREDIT_OVERDUE = "BANK_CREDIT_OVERDUE"
    SETTLEMENT_AMOUNT_MISMATCH = "SETTLEMENT_AMOUNT_MISMATCH"
    UNMATCHED_INBOUND_CREDIT = "UNMATCHED_INBOUND_CREDIT"
    REVERSAL_UNMATCHED = "REVERSAL_UNMATCHED"
    DUPLICATE_CREDIT = "DUPLICATE_CREDIT"
    DISPUTE_PENDING = "DISPUTE_PENDING"
    AMBIGUOUS_CASE = "AMBIGUOUS_CASE"


class ClassificationSource(StrEnum):
    """How a classification was reached — carried in the audit trail (§1.7.3),
    not just the label itself."""

    DETERMINISTIC_TRIGGER = "deterministic_trigger"
    """Adopted from a §3.3 subtype trigger component 4 already fired."""

    KEYWORD_BASELINE = "keyword_baseline"
    """This session's deterministic classifier: the §5.4 ablation arm, and the
    disclosed fallback if Phase 5 falls behind (§6.3)."""


class EvidenceBundle(BaseModel):
    """One case's classification-relevant evidence — the "structured evidence
    bundle" §4.2 describes Slot A as receiving. See this module's docstring
    for the boundary it enforces: facts and free text, never an account, an
    amount, or a postable narration.
    """

    model_config = ConfigDict(frozen=True)

    case_id: str
    case_kind: CaseKind
    fired_subtypes: tuple[ExceptionSubtype, ...]
    """§3.3 subtype triggers component 4 already fired on this case, in evaluator order."""
    has_template_hit: bool
    """Whether any §3.4 template predicate fired — context, not a decision input:
    a case reaching this bundle already failed to `AUTO_CLOSED` regardless."""
    narrations: tuple[str, ...]
    """This case's own `bank_line.narration` text — the only free text Slot A reads,
    and the entire evidence behind the `UNMATCHED_INBOUND_CREDIT` / `AMBIGUOUS_CASE` split."""
    match_tier: int | None
    """The FR-09 tier the matcher resolved at (§4.6), settlement-anchored cases only."""
    in_settlement_window: bool | None
    """Set only for a tier-3 (no-match) settlement-anchored case (§3.3's timing-residual rule)."""


class ClassificationResult(BaseModel):
    """One case's assigned subtype label, plus how the assignment was reached."""

    model_config = ConfigDict(frozen=True)

    case_id: str
    subtype: SubtypeLabel
    source: ClassificationSource
    matched_keyword: str | None = None
    """The narration token that identified a counterparty, when `source` is
    `KEYWORD_BASELINE` and a counterparty was found. `None` otherwise — including
    when the baseline ran and found nothing, which is itself the `AMBIGUOUS_CASE` evidence."""


def non_auto_close_case_ids(outcomes: Sequence[CaseOutcome]) -> set[str]:
    """§4.2's "~70 non-auto-close cases", by `case_id`."""
    return {outcome.case_id for outcome in outcomes if outcome.state in _NON_AUTO_CLOSE_STATES}


def build_evidence_bundle(case: Case, evidence: CaseEvidence) -> EvidenceBundle:
    """One case's `EvidenceBundle`, from component 2/3's `Case` and component 4's `CaseEvidence`."""
    return EvidenceBundle(
        case_id=case.case_id,
        case_kind=case.kind,
        fired_subtypes=tuple(trigger.subtype for trigger in evidence.subtype_triggers),
        has_template_hit=bool(evidence.template_hits),
        narrations=tuple(line.narration for line in case.bank_lines),
        match_tier=case.match_tier,
        in_settlement_window=case.in_settlement_window,
    )


def build_evidence_bundles(
    cases: Sequence[Case],
    evidences: Sequence[CaseEvidence],
    outcomes: Sequence[CaseOutcome],
) -> list[EvidenceBundle]:
    """Every eligible case's bundle, restricted to the ~70 non-auto-close cases (§4.2)."""
    eligible = non_auto_close_case_ids(outcomes)
    evidence_by_case = {evidence.case_id: evidence for evidence in evidences}
    return [
        build_evidence_bundle(case, evidence_by_case.get(case.case_id) or CaseEvidence(case_id=case.case_id))
        for case in cases
        if case.case_id in eligible
    ]


# --- The deterministic keyword baseline. ---

_ALNUM_TOKEN_RE = re.compile(r"[A-Z0-9]+")

_BANKING_BOILERPLATE_WORDS = frozenset(
    {
        "NEFT", "RTGS", "IMPS", "UPI", "ACH", "ECS",
        "CR", "DR", "CREDIT", "DEBIT",
        "TRANSFER", "TRF", "TXN", "PAYMENT", "PMT",
        "INWARD", "OUTWARD", "IN", "OUT", "BY", "TO", "FROM", "VIA",
        "REF", "REFERENCE", "NO", "MISC", "FUNDS", "AMT", "ONLINE",
        "P2A", "P2P", "A2A", "P2M",
    }
)
"""Generic NEFT/RTGS/IMPS bank-statement jargon — real vocabulary any bank
narration uses, not a list reverse-engineered from this batch's templates.
The distinction this baseline draws holds because of what these words mean
(none of them is ever a counterparty's name), the same rule session 3.2 set
for `pipeline/case_assembly.py`'s keyword sets and session 4.3 restated for
`pipeline/policy.py`'s tax-position markers."""

_MIN_COUNTERPARTY_TOKEN_LENGTH = 3
"""Excludes short bank-jargon fragments (e.g. a two-letter code) that slip
past the boilerplate list without being a plausible name token."""


def _identify_counterparty_token(narration: str) -> str | None:
    """The first alphabetic, non-boilerplate token in a narration, or `None`.

    A reference/UTR fragment is excluded by the presence of a digit — a
    counterparty's name in this domain is never digit-bearing, and every
    reference number the generator mints is (§4.6's UTR, `bank_ref_no`).
    This is the entire discriminator §4.2 names: "does the free-text
    narration identify a counterparty."
    """
    for token in _ALNUM_TOKEN_RE.findall(narration.upper()):
        if any(char.isdigit() for char in token):
            continue
        if len(token) < _MIN_COUNTERPARTY_TOKEN_LENGTH:
            continue
        if token in _BANKING_BOILERPLATE_WORDS:
            continue
        return token
    return None


def classify_case_baseline(bundle: EvidenceBundle) -> ClassificationResult:
    """The deterministic keyword baseline (§5.4's ablation arm; §6.3's disclosed fallback).

    Three branches, in order:

    1. **A trigger already fired.** Adopt it — component 5 assigning what
       component 4 already found, not a second detector re-deciding it.
       Where more than one fires (not observed against the reference batch;
       nothing in §3.3 asserts exclusivity the way REV-16 does for
       templates), the first in evaluator order wins, deterministically.
    2. **An untriggered orphan case carrying exactly one bank-line
       narration** — the `UNMATCHED_INBOUND_CREDIT` / `AMBIGUOUS_CASE`
       shape (§3.6: `REVERSAL_UNMATCHED` and `DUPLICATE_CREDIT` orphans
       already fired a trigger in branch 1, so only the plain single-credit
       shape reaches here). The narration keyword read decides it.
    3. **Everything else** falls through to `AMBIGUOUS_CASE` — the least
       -wrong answer in an eight-value space that has no correct one for
       an untriggered, non-orphan case (see this module's docstring).
    """
    if bundle.fired_subtypes:
        return ClassificationResult(
            case_id=bundle.case_id,
            subtype=SubtypeLabel(bundle.fired_subtypes[0].value),
            source=ClassificationSource.DETERMINISTIC_TRIGGER,
        )

    if bundle.case_kind is CaseKind.ORPHAN and len(bundle.narrations) == 1:
        keyword = _identify_counterparty_token(bundle.narrations[0])
        if keyword is not None:
            return ClassificationResult(
                case_id=bundle.case_id,
                subtype=SubtypeLabel.UNMATCHED_INBOUND_CREDIT,
                source=ClassificationSource.KEYWORD_BASELINE,
                matched_keyword=keyword,
            )

    return ClassificationResult(
        case_id=bundle.case_id,
        subtype=SubtypeLabel.AMBIGUOUS_CASE,
        source=ClassificationSource.KEYWORD_BASELINE,
    )


def classify_batch_baseline(bundles: Sequence[EvidenceBundle]) -> list[ClassificationResult]:
    """The baseline over every bundle in a batch. Never raises on well-formed input —
    session 5.1's checkpoint (§6.3): "baseline classifies all ~70 non-auto-close
    cases without crashing.\""""
    return [classify_case_baseline(bundle) for bundle in bundles]


def classification_distribution(results: Sequence[ClassificationResult]) -> dict[str, int]:
    """Count of assigned labels per `SubtypeLabel` — mirrors `pipeline.predicates`'s
    and `pipeline.policy`'s own `*_distribution` helpers."""
    counts: dict[str, int] = {}
    for result in results:
        counts[str(result.subtype)] = counts.get(str(result.subtype), 0) + 1
    return counts
