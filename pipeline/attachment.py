"""On what evidence each matched bank credit was attached to its settlement.

Every rate the metric surface reports is a statement about a case's
**terminal state**: did this case end where the answer key says it should.
That grading has a blind spot, and it is the one a reviewer is most likely
to press on. `false_match_rate` compares the set of cases the pipeline
called clean against the set the answer key calls clean. A settlement that
attached the *wrong* bank credit still lands in that set, provided the
amount drove its residual to zero and its state came out the same. The
label is right and the evidence underneath it is wrong, and no rate
denominated in cases can tell the difference.

The tier-2 double-claim defect was exactly that shape: two settlements
reaching a clean state on one credit that can belong to at most one of
them. It survived six seeds and the whole test suite, and it was found by
reading code rather than by any measurement.

This module measures the evidence instead of the label. For every credit
the matcher attached to a settlement, it asks a separate question: does
anything **in the record itself** tie that credit to that settlement?

    utr_exact           the credit names the settlement's UTR outright
    utr_prefix          it carries a truncated but unambiguous form of it
    amount_window_only  nothing identifies the settlement; the attachment
                        rests entirely on the amount matching inside the
                        settlement window
    contradicted        the credit names a different settlement's UTR

Two of those four rows carry the weight, and it is worth being exact about
which.

**utr_exact and utr_prefix are definitional, not independent.** They are
the same comparison tiers 0 and 1 already made, so a match at those tiers
corroborates itself. They are reported because the split between them and
amount_window_only is the useful number, not because either is evidence
the match is right.

**contradicted is independent, and it is the assertion that matters.** A
tier-2 attachment is made on amount and date alone: the matcher never
looks at the UTR to make it. So finding the UTR of some other settlement
in a credit attached here is a fact the matcher did not use and cannot
have arranged. It is reachable, because a settlement whose own UTR is
missing, or which already spent its credit elsewhere, leaves a UTR-bearing
line free for a different settlement to claim on amount alone.
contradicted must be empty, and unlike a saturated accuracy score it is
empty because nothing is wrong rather than because the batch cannot
express the failure.

**amount_window_only is the honest count**, and on the reference batch it
is 10 of 98. Those ten attachments cannot be confirmed or refuted from the
committed records: the credit carries no reference token, so the only
evidence is that the money and the dates line up. That is a real limit on
how much the perfect scores are worth, stated as a number instead of as a
caveat.

Like the bank-line partition, this grades nothing against ground truth. It
reads the pipeline's own decisions back and asks what they rest on.
"""

from __future__ import annotations

from collections import Counter
from enum import StrEnum
from typing import Sequence

from pydantic import BaseModel, ConfigDict

from pipeline.case_assembly import Case, CaseKind, reference_tokens
from pipeline.matcher import normalize_reference
from pipeline.schemas import Settlement

_PREFIX_MIN_LENGTH = 8
"""Shortest token allowed to corroborate by prefix, matching tier 1's own bound.

Below eight characters a run of digits stops identifying anything, because
it starts colliding with amounts, dates and serial numbers, so a shorter
token is treated as no evidence rather than as weak evidence.
"""


class AttachmentEvidence(StrEnum):
    """What ties an attached credit to the settlement that claimed it."""

    UTR_EXACT = "utr_exact"
    UTR_PREFIX = "utr_prefix"
    AMOUNT_WINDOW_ONLY = "amount_window_only"
    CONTRADICTED = "contradicted"


class ContradictedAttachment(BaseModel):
    """One credit attached to a settlement whose own text names a different one."""

    model_config = ConfigDict(frozen=True)

    line_id: str
    attached_to_case_id: str
    match_tier: int | None
    named_settlement_ids: tuple[str, ...]


class AttachmentAudit(BaseModel):
    """How every attached credit in a batch is evidenced.

    counts sums to total_attached: each attached credit is classified
    exactly once, strongest evidence first, so the rows are a partition
    and not overlapping tallies.
    """

    model_config = ConfigDict(frozen=True)

    total_attached: int
    counts: dict[str, int]
    corroborated: int
    """utr_exact plus utr_prefix: attachments with a reference token behind them."""
    contradictions: tuple[ContradictedAttachment, ...]
    """Must be empty. Non-empty is a false match the state metrics cannot see."""


def audit_attachments(
    cases: Sequence[Case], settlements: Sequence[Settlement]
) -> AttachmentAudit:
    """Classify every credit attached to a settlement-anchored case by its evidence.

    Orphan cases are skipped: an orphan has no settlement to be attached
    to, so the question this asks does not apply to it.
    """
    utr_owner: dict[str, str] = {}
    prefix_owners: list[tuple[str, str]] = []
    for settlement in settlements:
        if not settlement.utr:
            continue
        normalized = normalize_reference(settlement.utr)
        if normalized:
            utr_owner[normalized] = settlement.id
            prefix_owners.append((normalized, settlement.id))

    counts: Counter[str] = Counter()
    contradictions: list[ContradictedAttachment] = []

    for case in cases:
        if case.kind is not CaseKind.SETTLEMENT_ANCHORED:
            continue
        for line in case.bank_lines:
            tokens = {normalize_reference(word) for word in line.narration.split()}
            tokens |= {normalize_reference(token) for token in reference_tokens(line.narration)}
            if line.bank_ref_no is not None:
                tokens.add(normalize_reference(line.bank_ref_no))
            tokens.discard("")

            exact = {utr_owner[token] for token in tokens if token in utr_owner}
            prefix = {
                owner
                for utr, owner in prefix_owners
                for token in tokens
                if len(token) >= _PREFIX_MIN_LENGTH and utr.startswith(token)
            }
            named = exact or prefix

            if not named:
                counts[AttachmentEvidence.AMOUNT_WINDOW_ONLY] += 1
            elif case.case_id in named:
                counts[
                    AttachmentEvidence.UTR_EXACT if exact else AttachmentEvidence.UTR_PREFIX
                ] += 1
            else:
                counts[AttachmentEvidence.CONTRADICTED] += 1
                contradictions.append(
                    ContradictedAttachment(
                        line_id=line.line_id,
                        attached_to_case_id=case.case_id,
                        match_tier=case.match_tier,
                        named_settlement_ids=tuple(sorted(named)),
                    )
                )

    return AttachmentAudit(
        total_attached=sum(counts.values()),
        counts={evidence.value: counts[evidence] for evidence in AttachmentEvidence},
        corroborated=counts[AttachmentEvidence.UTR_EXACT] + counts[AttachmentEvidence.UTR_PREFIX],
        contradictions=tuple(contradictions),
    )
