"""The predicate evaluator: the six evidence predicates plus the
`OPERATIONAL_EXCEPTION` subtype triggers.

Runs **downstream of the matcher**: `T-04`'s second conjunct and the
`BANK_CREDIT_OVERDUE` trigger are both statements about whether a bank
credit matched, which is `pipeline/matcher.py`'s answer, not a fact this
module re-derives. A settlement-anchored `Case` whose `match_tier` is
still `None` is rejected rather than evaluated.

**This module reports evidence facts; it does not assign labels.**
Evaluating and assigning are two separate jobs — this module evaluates,
the classifier assigns an exception class and subtype. The distinction
has teeth: the `BANK_CREDIT_OVERDUE` trigger ("settlement window has
elapsed with no matching bank credit") is *literally true* of every
family-4 core case, which is an `ACCOUNTING_CORRECTION`, not an
`OPERATIONAL_EXCEPTION` — those cases also fire `T-04`, and the
precedence rule that resolves the two is the classifier's to state.
Reporting only the facts here keeps that rule visible in one place
instead of half-buried in a trigger.

## The six evidence predicates

Each is evaluated per `(case, recon_line)` pair, against the ledger
entries whose `reference` names that recon line's `entity_id` — the join deliberately deferred to "the point it needs them" rather than
pre-attaching in case assembly.

| Template | Predicate |
|---|---|
| `T-01` | settled `payment`, `fee > 0`, **no** `Payment Gateway Charges` entry, **and** a `Sales Revenue` credit equal to gross `amount` |
| `T-02` | settled `refund`, `debit > 0`, **no** `Sales Returns and Allowances` entry |
| `T-03` | settled `payment`, `fee > 0`, and a `Sales Revenue` credit equal to `amount - fee - tax` |
| `T-04` | a `Bank Account` debit dated at or near capture, **and** no bank credit matched this settlement |
| `T-05` | settled `adjustment` with `credit > 0`, **no** `Razorpay Settlement Adjustments` entry |
| `T-06` | settled `adjustment` with `debit > 0`, **no** `Razorpay Settlement Adjustments` entry |

**The mutual-exclusivity rule is enforced here, in production code, not
only in the test.** The pipeline must assert at instantiation time that
at most one template predicate fires per `(case_id, entity_id)`; a double
fire is a hard error, not a resolved-by-precedence situation.
`evaluate_case` therefore evaluates all six and raises
`PredicateOverlapError` if two fire on one entity — the two templates post
to different credit accounts and both instantiations balance, so a wrong
selection would sail through the debit-equals-credit check and surface
only as a non-zero post-adjustment residual.

`T-01` versus `T-03` is the pair this rule was written for, and the thing
that separates them is arithmetic on one number: with `fee > 0`, gross
`amount` and net `amount - fee - tax` cannot be equal, so at most one of
the two `Sales Revenue` conjuncts can hold. `T-01`'s extra "no `Payment
Gateway Charges` entry" conjunct is what keeps a *correctly* posted
payment (which credits revenue at gross, like family 1, but does post the
fee) out of `T-01` altogether.

## What is deliberately not evaluated here

- **`UNMATCHED_INBOUND_CREDIT`.** The trigger is "bank credit with an
  identifiable counterparty but no Razorpay anchor," and that judgment is
  assigned precisely to the model: `UNMATCHED_INBOUND_CREDIT` versus
  `AMBIGUOUS_CASE` on an orphan bank credit turns entirely on whether the
  free-text narration identifies a counterparty, and no residual
  computation decides it. The "no Razorpay anchor" half is already
  settled — such a case exists only because case assembly found no
  settlement anchor for the line. Emitting a deterministic
  counterparty-detector here would both pre-empt
  keyword baseline and put a second, competing answer in front of the one
  graded LLM slot.
- **Policy-exclusion detection (194-O, ITC eligibility tax cases).**
  Explicitly deferred: this is decidable better after the first run than
  now. The consequence is visible and expected: the 12 tax cases are
  structurally identical unposted adjustments, so `T-05`/`T-06` fire on
  them exactly as they fire on family 5. What separates the two
  populations is the policy gate, not the evidence predicate, and the gate
  is not this module's.
- **Amount derivation.** The "amount source" for each template is the
  instantiator's contract. A hit cites the records a
  downstream instantiation must read; it does not compute the entry.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from pipeline.accounts import (
    ACCOUNT_BANK_ACCOUNT,
    ACCOUNT_PAYMENT_GATEWAY_CHARGES,
    ACCOUNT_RAZORPAY_SETTLEMENT_ADJUSTMENTS,
    ACCOUNT_SALES_RETURNS_AND_ALLOWANCES,
    ACCOUNT_SALES_REVENUE,
    Account,
)
from pipeline.case_assembly import Case, CaseKind, is_reversal_shaped, reference_tokens
from pipeline.semantics import KEYWORD, NarrationSemantics
from pipeline.ground_truth import ExceptionSubtype
from pipeline.matcher import MatchTier
from pipeline.schemas import LedgerEntry, RazorpayEntityType, ReconLine, Settlement, SettlementStatus
from pipeline.timing import settlement_window_deadline

# `ExceptionSubtype` above is the taxonomy vocabulary, not a ground-truth
# label: it happens to be declared in `pipeline/ground_truth.py`
# because that is where the metrics schema needed it first. Importing the enum
# reads no case's ground truth and creates no dependency on the generator.


class TemplateId(StrEnum):
    """The six accounting templates, by ID."""

    T01 = "T-01"
    T02 = "T-02"
    T03 = "T-03"
    T04 = "T-04"
    T05 = "T-05"
    T06 = "T-06"


class PredicateOverlapError(Exception):
    """Two template predicates fired on one `(case_id, entity_id)`.

    A hard error by design. The templates that can collide post to
    different credit accounts, and both instantiations balance, so the
    debit-equals-credit balance check would pass a wrong selection —
    there is no safe precedence rule to fall back on.
    """


class PredicateHit(BaseModel):
    """One template's evidence predicate, satisfied by one recon line's evidence."""

    model_config = ConfigDict(frozen=True)

    case_id: str
    entity_id: str
    template_id: TemplateId
    cited_record_ids: tuple[str, ...]
    """Exactly the records the predicate read, and nothing else.

    Positive conjuncts cite an ID; a negative conjunct ("no `Payment
    Gateway Charges` entry references this payment") has no record to
    cite, so `T-02`/`T-05`/`T-06` cite only their recon line. The case
    anchor is carried by `case_id`, not repeated here.
    """


class SubtypeTrigger(BaseModel):
    """One `OPERATIONAL_EXCEPTION` subtype trigger, satisfied by a case's evidence.

    A fired trigger is a fact about the evidence, not an assigned label —
    more than one can fire on a case, and a case that fires
    `BANK_CREDIT_OVERDUE` may still be an `ACCOUNTING_CORRECTION`. The
    classifier resolves that.
    """

    model_config = ConfigDict(frozen=True)

    case_id: str
    subtype: ExceptionSubtype
    cited_record_ids: tuple[str, ...]


class CaseEvidence(BaseModel):
    """Everything the predicate evaluator has to say about one case."""

    model_config = ConfigDict(frozen=True)

    case_id: str
    template_hits: tuple[PredicateHit, ...] = ()
    subtype_triggers: tuple[SubtypeTrigger, ...] = ()


LedgerIndex = Mapping[str, tuple[LedgerEntry, ...]]
"""Ledger entries grouped by `reference`, the `ledger_entry` -> `recon_line` join key."""


def index_ledger_entries(entries: Sequence[LedgerEntry]) -> dict[str, tuple[LedgerEntry, ...]]:
    """Group ledger entries by `reference` so each predicate's join is a dict lookup.

    Built once per batch rather than per case: every predicate below asks
    the same question ("what did the merchant post against this
    `entity_id`?"), and a linear scan per recon line would be quadratic in
    a batch that already carries ~1,500 ledger entries.
    """
    grouped: dict[str, list[LedgerEntry]] = {}
    for entry in entries:
        grouped.setdefault(entry.reference, []).append(entry)
    return {reference: tuple(group) for reference, group in grouped.items()}


# --- Ledger-evidence helpers, shared by the six predicates. ---


def _has_entry_for_account(entries: Sequence[LedgerEntry], account: Account) -> bool:
    return any(entry.account_code == account.code for entry in entries)


def _credit_entry_of_exactly(
    entries: Sequence[LedgerEntry], account: Account, amount_paise: int
) -> LedgerEntry | None:
    """The entry crediting `account` for exactly `amount_paise` — integer comparison, no tolerance."""
    return next(
        (entry for entry in entries if entry.account_code == account.code and int(entry.credit) == amount_paise),
        None,
    )


def _debit_entry_for_account(entries: Sequence[LedgerEntry], account: Account) -> LedgerEntry | None:
    return next(
        (entry for entry in entries if entry.account_code == account.code and int(entry.debit) > 0),
        None,
    )


def _capture_date(recon_line: ReconLine) -> date:
    return datetime.fromtimestamp(recon_line.created_at, tz=timezone.utc).date()


def _is_dated_at_or_near_capture(entry_date: date, capture_date: date) -> bool:
    """`T-04`'s conjunct: the `Bank Account` debit is "dated at or near capture".

    "Near" is bounded by the only timing notion defined for this system —
    the T+2 working-day settlement window. A bank debit dated inside that
    window is booked while the cash demonstrably could not have arrived
    yet (the family's whole premise: a *premature* bank debit); one dated
    past the window is dated at settlement time, not at capture, and the
    premature-posting reading no longer follows from the evidence. The
    lower bound is capture itself: a ledger cannot legitimately book a
    payment before the payment happened.

    This clause carries little weight on its own — `T-04`'s other conjunct
    already requires that *no* bank credit matched, which makes any
    `Bank Account` debit against the payment premature by construction.
    It is a guard against reading an unrelated, far-dated bank posting as
    this family's error, not the discriminating test.
    """
    return capture_date <= entry_date <= settlement_window_deadline(capture_date)


# --- The six evidence predicates. ---
#
# Each returns the record IDs it cited if it fires, or None if it does not.
# One shape for all six so `evaluate_case` can run them as a table and count
# the fires — the mutual-exclusivity assertion is then structural rather than
# bolted on.


def _predicate_t01(case: Case, line: ReconLine, entries: Sequence[LedgerEntry]) -> tuple[str, ...] | None:
    if line.type is not RazorpayEntityType.PAYMENT or not line.settled or int(line.fee) <= 0:
        return None
    if _has_entry_for_account(entries, ACCOUNT_PAYMENT_GATEWAY_CHARGES):
        return None
    revenue = _credit_entry_of_exactly(entries, ACCOUNT_SALES_REVENUE, int(line.amount))
    if revenue is None:
        return None
    return (line.entity_id, revenue.journal_entry_id)


def _predicate_t02(case: Case, line: ReconLine, entries: Sequence[LedgerEntry]) -> tuple[str, ...] | None:
    if line.type is not RazorpayEntityType.REFUND or not line.settled or int(line.debit) <= 0:
        return None
    if _has_entry_for_account(entries, ACCOUNT_SALES_RETURNS_AND_ALLOWANCES):
        return None
    return (line.entity_id,)


def _predicate_t03(case: Case, line: ReconLine, entries: Sequence[LedgerEntry]) -> tuple[str, ...] | None:
    if line.type is not RazorpayEntityType.PAYMENT or not line.settled or int(line.fee) <= 0:
        return None
    net_paise = int(line.amount) - int(line.fee) - int(line.tax)
    revenue = _credit_entry_of_exactly(entries, ACCOUNT_SALES_REVENUE, net_paise)
    if revenue is None:
        return None
    return (line.entity_id, revenue.journal_entry_id)


def _predicate_t04(case: Case, line: ReconLine, entries: Sequence[LedgerEntry]) -> tuple[str, ...] | None:
    if line.type is not RazorpayEntityType.PAYMENT:
        return None
    # A hard precondition, read off the matcher: family 4 applies only
    # where no bank credit matching the settlement exists in `bank_line`
    # as of the batch snapshot. Tiers 0-2 are exactly "matching the
    # settlement UTR or net amount"; tier 3 is their absence.
    if case.match_tier != int(MatchTier.NO_MATCH):
        return None
    bank_debit = _debit_entry_for_account(entries, ACCOUNT_BANK_ACCOUNT)
    if bank_debit is None or not _is_dated_at_or_near_capture(bank_debit.date, _capture_date(line)):
        return None
    cited = [line.entity_id, bank_debit.journal_entry_id]
    if case.settlement is not None:
        cited.append(case.settlement.id)
    return tuple(cited)


def _predicate_t05(case: Case, line: ReconLine, entries: Sequence[LedgerEntry]) -> tuple[str, ...] | None:
    if line.type is not RazorpayEntityType.ADJUSTMENT or not line.settled or int(line.credit) <= 0:
        return None
    if _has_entry_for_account(entries, ACCOUNT_RAZORPAY_SETTLEMENT_ADJUSTMENTS):
        return None
    return (line.entity_id,)


def _predicate_t06(case: Case, line: ReconLine, entries: Sequence[LedgerEntry]) -> tuple[str, ...] | None:
    if line.type is not RazorpayEntityType.ADJUSTMENT or not line.settled or int(line.debit) <= 0:
        return None
    if _has_entry_for_account(entries, ACCOUNT_RAZORPAY_SETTLEMENT_ADJUSTMENTS):
        return None
    return (line.entity_id,)


EVIDENCE_PREDICATES = (
    (TemplateId.T01, _predicate_t01),
    (TemplateId.T02, _predicate_t02),
    (TemplateId.T03, _predicate_t03),
    (TemplateId.T04, _predicate_t04),
    (TemplateId.T05, _predicate_t05),
    (TemplateId.T06, _predicate_t06),
)
"""The six evidence predicates, in template order. Every one is evaluated on
every recon line — none is skipped once another has fired, because skipping is
what would hide a double fire that must be raised as a hard error."""


# --- The `OPERATIONAL_EXCEPTION` subtype triggers. ---


def _settlement_amount_paise_from_recon_lines(recon_lines: Sequence[ReconLine]) -> int:
    """The `SETTLEMENT_AMOUNT_MISMATCH` right-hand side, and the generator's own invariant.

    > `settlement.amount == sum(credits) - sum(debits) - fees - tax`

    Fees and tax come from the recon lines themselves, not from the
    settlement header's own `fees`/`tax` fields — the trigger compares
    the header against "the sum of **its recon lines** net of fees and
    tax", so reading the header on both sides would compare a record to
    itself.
    """
    return sum(int(line.credit) - int(line.debit) - int(line.fee) - int(line.tax) for line in recon_lines)


def _settlement_anchored_triggers(case: Case, settlement: Settlement) -> list[SubtypeTrigger]:
    triggers: list[SubtypeTrigger] = []

    # SETTLEMENT_UTR_MISSING: "Settlement is `processed` but carries no UTR,
    # so no bank-side anchor exists." `utr` is typed as a plain, non-nullable
    # string, so "no UTR" is the empty value within that type.
    if settlement.status is SettlementStatus.PROCESSED and not settlement.utr.strip():
        triggers.append(
            SubtypeTrigger(
                case_id=case.case_id,
                subtype=ExceptionSubtype.SETTLEMENT_UTR_MISSING,
                cited_record_ids=(settlement.id,),
            )
        )

    # BANK_CREDIT_OVERDUE: "Settlement window has elapsed with no matching
    # bank credit." Both halves are the matcher's output, already carrying
    # the timing-residual rule.
    if case.match_tier == int(MatchTier.NO_MATCH) and case.in_settlement_window is False:
        triggers.append(
            SubtypeTrigger(
                case_id=case.case_id,
                subtype=ExceptionSubtype.BANK_CREDIT_OVERDUE,
                cited_record_ids=(settlement.id,),
            )
        )

    # SETTLEMENT_AMOUNT_MISMATCH: "Settlement header amount != sum of its
    # recon lines net of fees and tax." Exact integer-paise comparison.
    if int(settlement.amount) != _settlement_amount_paise_from_recon_lines(case.recon_lines):
        triggers.append(
            SubtypeTrigger(
                case_id=case.case_id,
                subtype=ExceptionSubtype.SETTLEMENT_AMOUNT_MISMATCH,
                cited_record_ids=(settlement.id, *(line.entity_id for line in case.recon_lines)),
            )
        )

    # DISPUTE_PENDING: a seventh subtype attached to the chargeback
    # population. `dispute_id` is the recon line's foreign key to a dispute
    # (a stretch-goal entity), so a populated `dispute_id` is the trigger.
    # The dispute entity itself is not generated in v1, so the citation is
    # the disputed recon line, not the dispute.
    disputed = tuple(line.entity_id for line in case.recon_lines if line.dispute_id is not None)
    if disputed:
        triggers.append(
            SubtypeTrigger(
                case_id=case.case_id,
                subtype=ExceptionSubtype.DISPUTE_PENDING,
                cited_record_ids=disputed,
            )
        )

    return triggers


def _orphan_triggers(case: Case, semantics: NarrationSemantics = KEYWORD) -> list[SubtypeTrigger]:
    """The two structurally-decidable orphan triggers.

    The third orphan subtype, `UNMATCHED_INBOUND_CREDIT`, is Slot A's —
    see this module's docstring.

    `REVERSAL_UNMATCHED`'s "no matching prior credit in the batch" half is
    not re-derived here: `pipeline/case_assembly.py` already answered it
    batch-wide, by declining to raise a case for any reversal whose
    narration shares a reference token with a credit on the same
    statement. A reversal-shaped line that reached this function as its
    own one-line case is, by that construction, unmatched. Re-running the
    batch-wide pairing search here could only produce a second answer that
    disagrees with the first.
    """
    triggers: list[SubtypeTrigger] = []
    deposits = [line for line in case.bank_lines if int(line.deposit_paise) > 0]

    # DUPLICATE_CREDIT: "Same UTR credited twice on the bank statement."
    # The duplicated pair is grouped into one case, so the duplicate is
    # visible inside the case: two deposit lines whose narrations carry a
    # common reference token.
    if len(deposits) >= 2:
        token_counts: dict[str, int] = {}
        for line in deposits:
            for token in reference_tokens(line.narration):
                token_counts[token] = token_counts.get(token, 0) + 1
        if any(count >= 2 for count in token_counts.values()):
            triggers.append(
                SubtypeTrigger(
                    case_id=case.case_id,
                    subtype=ExceptionSubtype.DUPLICATE_CREDIT,
                    cited_record_ids=tuple(line.line_id for line in deposits),
                )
            )

    # REVERSAL_UNMATCHED: "Bank reversal with no matching prior credit in the batch."
    if len(case.bank_lines) == 1:
        (line,) = case.bank_lines
        if int(line.withdrawal_paise) > 0 and is_reversal_shaped(line, semantics):
            triggers.append(
                SubtypeTrigger(
                    case_id=case.case_id,
                    subtype=ExceptionSubtype.REVERSAL_UNMATCHED,
                    cited_record_ids=(line.line_id,),
                )
            )

    return triggers


# --- Evaluation. ---


def evaluate_case(
    case: Case, ledger_index: LedgerIndex, semantics: NarrationSemantics = KEYWORD
) -> CaseEvidence:
    """Evaluate every template predicate and every subtype trigger against one case.

    Raises `PredicateOverlapError` if two template predicates fire on the
    same `(case_id, entity_id)` — a hard error, asserted in production
    code rather than left to the test suite.
    """
    if case.kind is CaseKind.ORPHAN:
        return CaseEvidence(case_id=case.case_id, subtype_triggers=tuple(_orphan_triggers(case, semantics)))

    settlement = case.settlement
    if settlement is None:
        raise ValueError(f"settlement-anchored case {case.case_id!r} carries no settlement")
    if case.match_tier is None:
        raise ValueError(
            f"case {case.case_id!r} has not been matched; the predicate evaluator runs "
            "downstream of pipeline.matcher.match_cases (T-04 and BANK_CREDIT_OVERDUE "
            "both read the match result)"
        )

    hits: list[PredicateHit] = []
    for line in case.recon_lines:
        entries = ledger_index.get(line.entity_id, ())
        fired = [
            (template_id, cited)
            for template_id, predicate in EVIDENCE_PREDICATES
            if (cited := predicate(case, line, entries)) is not None
        ]
        if len(fired) > 1:
            raise PredicateOverlapError(
                f"{len(fired)} template predicates fired on "
                f"(case_id={case.case_id!r}, entity_id={line.entity_id!r}): "
                f"{[str(template_id) for template_id, _ in fired]}"
            )
        hits += [
            PredicateHit(
                case_id=case.case_id,
                entity_id=line.entity_id,
                template_id=template_id,
                cited_record_ids=cited,
            )
            for template_id, cited in fired
        ]

    return CaseEvidence(
        case_id=case.case_id,
        template_hits=tuple(hits),
        subtype_triggers=tuple(_settlement_anchored_triggers(case, settlement)),
    )


def evaluate_cases(
    cases: Sequence[Case],
    ledger_entries: Sequence[LedgerEntry],
    *,
    semantics: NarrationSemantics = KEYWORD,
) -> list[CaseEvidence]:
    """Evaluate every case in the batch. Cases must already carry the matcher's output.

    `semantics` answers the one free-text question a trigger asks — the
    `REVERSAL_UNMATCHED` read, which must be the same notion of "reversal"
    `pipeline.case_assembly` used to raise the case, or the component that
    grouped the line and the component that says why would disagree.
    """
    ledger_index = index_ledger_entries(ledger_entries)
    return [evaluate_case(case, ledger_index, semantics) for case in cases]


def template_hit_distribution(evidences: Sequence[CaseEvidence]) -> dict[str, int]:
    """Count of firing predicates per template ID — the six templates, as exercised by a batch."""
    counts: dict[str, int] = {}
    for evidence in evidences:
        for hit in evidence.template_hits:
            counts[str(hit.template_id)] = counts.get(str(hit.template_id), 0) + 1
    return counts


def subtype_trigger_distribution(evidences: Sequence[CaseEvidence]) -> dict[str, int]:
    """Count of firing triggers per `OPERATIONAL_EXCEPTION` subtype."""
    counts: dict[str, int] = {}
    for evidence in evidences:
        for trigger in evidence.subtype_triggers:
            counts[str(trigger.subtype)] = counts.get(str(trigger.subtype), 0) + 1
    return counts
