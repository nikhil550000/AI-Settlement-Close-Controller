"""§2.5's policy exclusions from the auto-close path (FR-06, REV-11).

> **FR-06.** The following are detected and classified but MUST NOT be
> auto-posted in v1, regardless of model confidence. They terminate in
> `REVIEW_REQUIRED` and are counted under `declined_by_policy_rate`.

**This gate is not the validator, and the separation is deliberate.** A
policy-excluded entry passes every one of invariant 1.7.5's checks: it
balances, its template is allowlisted, its accounts are permitted in the
direction used, its cited records exist and are unposted, and applying it
would drive the residual to 0. Nothing is *wrong* with it. It is declined
because §2.5 places the judgment it embeds outside what this system will
automate — "auto-post only what the source report supplies as a number;
recommend-only what requires a tax judgment." Folding that into the
invariant chain would make a scope decision look like a safety failure,
and `declined_by_policy_rate` exists (REV-02) precisely so the two are
counted apart.

**§4.1 assigns this to no component**, which is why it lives in its own
module rather than being buried in one. §2.3 names "policy-exclusion
routing" as part of what `REVIEW_REQUIRED` requires, and §4.1 gives
terminal state assignment to component 8, so component 8 consults this
gate. Without it two populations reach the wrong state and both are
unsafe rather than merely inaccurate: the 12 FR-06 tax cases would
auto-post a tax position (§2.5: "a wrongly auto-posted tax entry is the
most expensive failure mode in this domain"), and the 5 family-4
date-error cases would read as `AUTO_MATCHED`, a false match under §1.6's
primary safety metric.

**Two exclusions are detectable from this batch's evidence; three are
not, and are stated rather than silently absent.** §2.5 lists five. "TDS
treatment" and "GST input tax credit eligibility on MDR" are both carried
by the FR-06 adjustment population and are detected here as one exclusion
— they share a detection surface and a consequence. "Revenue recognition
timing decisions" and "any entry that embeds a tax position" have no case
population in §3.5 and therefore no evidence to fire on; they are
scope statements bounding future families, not detectors. "Date-only
reclassification across a period boundary" is REV-11's addition and has
its own population (5 cases), detected below.

**FR-06 detection is §4.2's Slot C, in its deterministic v1 form.**

> **Slot C — FR-06 policy-exclusion detection. Deterministic in v1; LLM
> deferred.** A 194-O deduction has a signature in the adjustment line
> and a predicate probably suffices.

The predicate reads that signature. §4.2's fallback — an LLM gate if the
predicate proves brittle against the generator's narration variety — is
explicitly deferred and is not built here; the deferral is safe on §4.2's
own terms, because the output routes to `REVIEW_REQUIRED` either way.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from pipeline.case_assembly import Case, CaseKind
from pipeline.predicates import CaseEvidence
from pipeline.semantics import KEYWORD, NarrationSemantics
from pipeline.schemas import LedgerEntry, RazorpayEntityType, ReconLine


class PolicyExclusion(StrEnum):
    """The §2.5 exclusions this batch carries evidence for."""

    TAX_POSITION = "tax_position"
    """§2.5's first, second and fourth bullets: TDS under 194-O/194-H, GST ITC
    eligibility on MDR, and any entry embedding a tax position."""

    DATE_ONLY_RECLASSIFICATION = "date_only_reclassification"
    """§2.5's fifth bullet, added by REV-11: correct accounts, wrong period,
    settlement already credited to the bank."""


class PolicyDecision(BaseModel):
    """Why a case may not auto-post, and the evidence that says so."""

    model_config = ConfigDict(frozen=True)

    case_id: str
    exclusion: PolicyExclusion
    cited_record_ids: tuple[str, ...]
    detail: str


# The tax-position vocabulary this module used to own now lives in
# `pipeline/semantics.py`, with the four other keyword sets that answer a
# question about English by testing for a literal substring. See that module's
# docstring for why they were worth measuring together.


def _has_tax_signature(text: str | None, semantics: NarrationSemantics = KEYWORD) -> bool:
    if not text:
        return False
    return semantics.is_tax_position(text)


def _capture_date(line: ReconLine) -> date:
    return datetime.fromtimestamp(line.created_at, tz=timezone.utc).date()


def _tax_position_decisions(case: Case, semantics: NarrationSemantics = KEYWORD) -> list[PolicyDecision]:
    """FR-06: an adjustment line whose description carries a tax-position signature.

    §4.2 fixes the detection surface as the adjustment line's own text.
    The signature is read off `description` — §3.1's free-text field on
    the recon line — and every adjustment in the batch carries one, tax
    position or not, so it is the *content* that discriminates and never
    the field's presence.
    """
    cited = tuple(
        line.entity_id
        for line in case.recon_lines
        if line.type is RazorpayEntityType.ADJUSTMENT and _has_tax_signature(line.description, semantics)
    )
    if not cited:
        return []
    return [
        PolicyDecision(
            case_id=case.case_id,
            exclusion=PolicyExclusion.TAX_POSITION,
            cited_record_ids=cited,
            detail=(
                "adjustment line carries a tax-position signature (§2.5 TDS/194-O or GST ITC); "
                "detected and classified, never auto-posted"
            ),
        )
    ]


def _date_only_decisions(
    case: Case,
    evidence: CaseEvidence,
    entries_by_reference: Mapping[str, Sequence[LedgerEntry]],
) -> list[PolicyDecision]:
    """REV-11: correct accounts, wrong period, and the settlement already banked.

    Three conjuncts, each read straight off §2.5's own sentence — "a
    ledger entry posted to the correct accounts on the wrong date, where
    the settlement has already credited the bank":

    1. **Correct accounts.** No §3.4 evidence predicate fired on the case.
       A predicate firing means the accounts or amounts *are* wrong and a
       template restores them, which is an `ACCOUNTING_CORRECTION` on the
       auto path, not a period question. This is the conjunct that keeps
       the exclusion from swallowing a family case that happens to
       straddle a month boundary.
    2. **Wrong date, across a period boundary.** Some ledger entry
       referencing one of the case's own recon lines is dated in a
       different calendar month from that line's capture date. §2.5 scopes
       the exclusion to a *period* boundary specifically, and it is the
       month boundary that makes the treatment a reclassification rather
       than a correction.
    3. **Settlement already credited to the bank.** The matcher found a
       bank credit for the settlement (any tier). This is REV-15's mirror
       image: family 4 auto-closes only where no credit landed, and this
       exclusion covers precisely the case where one did — §3.2 spells out
       why posting `Dr Razorpay Clearing / Cr Bank Account` there would
       *create* a break rather than close one.
    """
    if case.kind is not CaseKind.SETTLEMENT_ANCHORED:
        return []
    if evidence.template_hits:
        return []
    if case.match_tier is None or case.match_tier >= 3:
        return []

    misdated: list[str] = []
    for line in case.recon_lines:
        captured = _capture_date(line)
        for entry in entries_by_reference.get(line.entity_id, ()):
            if (entry.date.year, entry.date.month) != (captured.year, captured.month):
                misdated.append(entry.journal_entry_id)

    if not misdated:
        return []
    return [
        PolicyDecision(
            case_id=case.case_id,
            exclusion=PolicyExclusion.DATE_ONLY_RECLASSIFICATION,
            cited_record_ids=tuple(sorted(set(misdated))),
            detail=(
                "ledger entries posted to the correct accounts in a different period from capture, "
                "with the settlement already credited to the bank (§2.5 / REV-11); "
                "the correct treatment is a period reclassification, which is policy-excluded"
            ),
        )
    ]


def evaluate_policy(
    case: Case,
    evidence: CaseEvidence,
    entries_by_reference: Mapping[str, Sequence[LedgerEntry]],
    *,
    semantics: NarrationSemantics = KEYWORD,
) -> tuple[PolicyDecision, ...]:
    """Every §2.5 exclusion that applies to one case. Empty means the auto path stays open.

    `semantics` answers only the FR-06 tax-position read; REV-11's date-only
    exclusion is arithmetic over dates and takes no view on text.
    """
    return tuple(
        _tax_position_decisions(case, semantics)
        + _date_only_decisions(case, evidence, entries_by_reference)
    )


def policy_exclusion_distribution(decisions: Sequence[PolicyDecision]) -> dict[str, int]:
    """Count of cases excluded per §2.5 exclusion — `declined_by_policy_rate`'s numerator, by cause."""
    counts: dict[str, int] = {}
    for decision in decisions:
        counts[str(decision.exclusion)] = counts.get(str(decision.exclusion), 0) + 1
    return counts
