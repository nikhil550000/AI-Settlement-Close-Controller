"""`pipeline/attachment.py` -- the evidence under a match, not the label on it.

**The blind spot this file exists for.** Every graded rate is denominated in
cases and compares terminal states. `false_match_rate` asks whether the set of
cases the pipeline called clean equals the set the answer key calls clean. A
settlement that attached the *wrong* bank credit still lands in that set as
long as the amount drove its residual to zero, so the rate reads 0/150 with a
mis-attached credit sitting underneath it.

That is not hypothetical. The tier-2 double-claim defect had exactly this
shape, survived six seeds and the full suite, and was found by reading code
rather than by any measurement.

The load-bearing assertions here are the two that can actually fail:

- `test_no_attached_credit_names_a_different_settlement` on every committed
  batch. A tier-2 attachment is made on amount and date alone, so another
  settlement's UTR appearing in a credit attached here is evidence the matcher
  never consulted and cannot have arranged.
- `test_a_credit_carrying_another_settlements_utr_is_reported_as_contradicted`,
  which builds that situation on purpose. Without it the check above passes
  because nothing can ever fire it, which is the failure mode of every
  assertion written against a batch that is already clean.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from pipeline.attachment import AttachmentEvidence, audit_attachments
from pipeline.case_assembly import Case, CaseKind, assemble_cases
from pipeline.loaders import load_bank_lines, load_recon_lines, load_settlements
from pipeline.matcher import match_cases
from pipeline.schemas import BankLine, BankProfile, Settlement

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_DATE = date(2026, 8, 28)

COMMITTED_BATCHES = ["reference", "heldout_vocab", "contested", "adversarial"]

REFERENCE_ATTACHED = 98
"""Credits the matcher attaches to a settlement on the reference batch."""

REFERENCE_UNCORROBORATED = 10
"""Of those 98, the ones resting on amount and date alone.

Pinned deliberately. This is the number that says how much the batch's perfect
scores are worth: ten attachments the committed records can neither confirm
nor refute. If a change moves it, the README's claim moves with it.
"""


def _audit(batch: str):
    data = REPO_ROOT / "data" / batch
    settlements = load_settlements(data / "settlements.jsonl")
    bank_lines = load_bank_lines(data / "bank_lines.jsonl")
    cases = match_cases(
        assemble_cases(settlements, load_recon_lines(data / "recon_lines.jsonl"), bank_lines),
        bank_lines,
        snapshot_date=SNAPSHOT_DATE,
    )
    return audit_attachments(cases, settlements)


@pytest.mark.parametrize("batch", COMMITTED_BATCHES)
def test_no_attached_credit_names_a_different_settlement(batch: str) -> None:
    audit = _audit(batch)
    assert audit.contradictions == (), (
        f"{batch}: a credit attached to one settlement carries another's UTR -- "
        f"{[c.model_dump() for c in audit.contradictions]}"
    )
    assert audit.counts[AttachmentEvidence.CONTRADICTED] == 0


@pytest.mark.parametrize("batch", COMMITTED_BATCHES)
def test_every_attached_credit_is_classified_exactly_once(batch: str) -> None:
    audit = _audit(batch)
    assert sum(audit.counts.values()) == audit.total_attached
    assert audit.corroborated == (
        audit.counts[AttachmentEvidence.UTR_EXACT] + audit.counts[AttachmentEvidence.UTR_PREFIX]
    )


def test_the_reference_batch_reports_ten_uncorroborated_attachments() -> None:
    audit = _audit("reference")
    assert audit.total_attached == REFERENCE_ATTACHED
    assert audit.counts[AttachmentEvidence.AMOUNT_WINDOW_ONLY] == REFERENCE_UNCORROBORATED
    assert audit.corroborated == REFERENCE_ATTACHED - REFERENCE_UNCORROBORATED


def test_held_out_vocabulary_does_not_change_what_the_evidence_rests_on() -> None:
    """Rewording the bank must not move the attachment evidence.

    The held-out batch changes narration prose only, and the reference tokens
    are preserved verbatim so the tier cascade sees identical tokens. If these
    two diverge, the vocabulary rewrite has damaged the reference tokens and
    the vocabulary ablation is no longer comparing like with like.
    """
    assert _audit("heldout_vocab").counts == _audit("reference").counts


def _settlement(settlement_id: str, utr: str, amount: int) -> Settlement:
    return Settlement(
        id=settlement_id,
        amount=amount,
        status="processed",
        fees=0,
        tax=0,
        utr=utr,
        created_at=int(datetime(2026, 8, 20, tzinfo=timezone.utc).timestamp()),
    )


def test_a_credit_carrying_another_settlements_utr_is_reported_as_contradicted() -> None:
    """The check above must be able to fail, or it measures nothing.

    Built by hand rather than by seed: a credit whose narration names
    settlement B's UTR, attached to settlement A's case. That is what a tier-2
    match on a colliding amount produces when the credit really belongs to
    someone else, and it is invisible to every rate denominated in cases,
    because A's residual still strikes to zero and A still reports as cleanly
    matched.
    """
    owner = _settlement("setl_owner", "UTRAAAA111122223333", 50_000)
    claimant = _settlement("setl_claimant", "UTRBBBB444455556666", 50_000)
    credit = BankLine(
        line_id="bank_contested",
        value_date=date(2026, 8, 21),
        narration="NEFT CR RAZORPAY UTRAAAA111122223333 SETTLEMENT",
        bank_ref_no="N000000000001",
        withdrawal_paise=0,
        deposit_paise=50_000,
        closing_balance_paise=1_000_000,
        bank_profile=BankProfile.HDFC,
    )
    mis_attached = Case(
        case_id="setl_claimant",
        kind=CaseKind.SETTLEMENT_ANCHORED,
        settlement=claimant,
        bank_lines=(credit,),
        match_tier=2,
        residual_paise=0,
    )

    audit = audit_attachments([mis_attached], [owner, claimant])

    assert audit.total_attached == 1
    assert audit.counts[AttachmentEvidence.CONTRADICTED] == 1
    assert audit.corroborated == 0
    (contradiction,) = audit.contradictions
    assert contradiction.line_id == "bank_contested"
    assert contradiction.attached_to_case_id == "setl_claimant"
    assert contradiction.match_tier == 2
    assert contradiction.named_settlement_ids == ("setl_owner",)


def test_a_credit_naming_its_own_settlement_is_corroborated_not_contradicted() -> None:
    """The mirror of the case above, so the check discriminates rather than always firing."""
    owner = _settlement("setl_owner", "UTRAAAA111122223333", 50_000)
    credit = BankLine(
        line_id="bank_clean",
        value_date=date(2026, 8, 21),
        narration="NEFT CR RAZORPAY UTRAAAA111122223333 SETTLEMENT",
        bank_ref_no="N000000000001",
        withdrawal_paise=0,
        deposit_paise=50_000,
        closing_balance_paise=1_000_000,
        bank_profile=BankProfile.HDFC,
    )
    case = Case(
        case_id="setl_owner",
        kind=CaseKind.SETTLEMENT_ANCHORED,
        settlement=owner,
        bank_lines=(credit,),
        match_tier=0,
        residual_paise=0,
    )

    audit = audit_attachments([case], [owner])

    assert audit.contradictions == ()
    assert audit.counts[AttachmentEvidence.UTR_EXACT] == 1
    assert audit.corroborated == 1


def test_a_credit_with_no_reference_token_is_uncorroborated_not_contradicted() -> None:
    """An unidentifiable credit is unknown, not wrong.

    This is the distinction the whole module turns on: `amount_window_only` is
    an admission that the records cannot settle the question, and collapsing it
    into either of the other rows would overstate what the batch proves.
    """
    owner = _settlement("setl_owner", "UTRAAAA111122223333", 50_000)
    credit = BankLine(
        line_id="bank_silent",
        value_date=date(2026, 8, 21),
        narration="NEFT CR RAZORPAY SOFTWARE PVT LTD SETTLEMENT",
        bank_ref_no=None,
        withdrawal_paise=0,
        deposit_paise=50_000,
        closing_balance_paise=1_000_000,
        bank_profile=BankProfile.HDFC,
    )
    case = Case(
        case_id="setl_owner",
        kind=CaseKind.SETTLEMENT_ANCHORED,
        settlement=owner,
        bank_lines=(credit,),
        match_tier=2,
        residual_paise=0,
    )

    audit = audit_attachments([case], [owner])

    assert audit.contradictions == ()
    assert audit.counts[AttachmentEvidence.AMOUNT_WINDOW_ONLY] == 1
    assert audit.corroborated == 0


def test_orphan_cases_are_not_audited() -> None:
    """An orphan has no settlement to be attached to, so the question does not apply."""
    credit = BankLine(
        line_id="bank_orphan",
        value_date=date(2026, 8, 21),
        narration="NEFT CR SUSPENSE-CR 987654321098",
        bank_ref_no=None,
        withdrawal_paise=0,
        deposit_paise=50_000,
        closing_balance_paise=1_000_000,
        bank_profile=BankProfile.HDFC,
    )
    orphan = Case(case_id="case_orphan_bank_orphan", kind=CaseKind.ORPHAN, bank_lines=(credit,))

    audit = audit_attachments([orphan], [])

    assert audit.total_attached == 0
    assert audit.contradictions == ()
