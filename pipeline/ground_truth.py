"""Per-case ground-truth schema, per spec.md §1.6.

Distinct from the four §3.1 canonical *input* schemas (`pipeline/schemas.py`):
this is the label the generator emits alongside the records it writes, per
§3.5's "Label emission" rule — labels come from the injection plan, never
re-derived from generated records. It lives under `pipeline/`, not
`generator/`, for the same reason the four canonical schemas do (see
BUILDLOG.md session 1.2, Decided): ground truth is written by the generator
in Phase 2 but read by the eval harness under `pipeline/` in Phase 6, and
`pipeline/` must never import `generator/` (§4.1).

`expected_journal_entries` and its `ExpectedJournalLeg`/`ExpectedJournalEntry`
shape are a completion of §1.6's "list; see note below" into a concrete
Pydantic shape — analogous to how session 1.2 completed `ledger_entry`'s
`resolution_id`/`case_id` fields — not an invented field.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from pipeline.money import NonNegPaise


class OutcomeState(StrEnum):
    """The five terminal states, §1.3."""

    AUTO_MATCHED = "AUTO_MATCHED"
    AUTO_CLOSED = "AUTO_CLOSED"
    EXTERNAL_ACTION_REQUIRED = "EXTERNAL_ACTION_REQUIRED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    ABSTAINED = "ABSTAINED"


class ExceptionClass(StrEnum):
    """The four-class taxonomy plus the `NONE` sentinel, §3.3."""

    NONE = "NONE"
    ACCOUNTING_CORRECTION = "ACCOUNTING_CORRECTION"
    OPERATIONAL_EXCEPTION = "OPERATIONAL_EXCEPTION"
    EXPECTED_TIMING_DIFFERENCE = "EXPECTED_TIMING_DIFFERENCE"
    AMBIGUOUS_CASE = "AMBIGUOUS_CASE"


class ExceptionSubtype(StrEnum):
    """Subtypes beneath the four classes, plus `NONE`, §3.3."""

    NONE = "NONE"
    OMISSION = "OMISSION"
    MISPOSTING = "MISPOSTING"
    SETTLEMENT_UTR_MISSING = "SETTLEMENT_UTR_MISSING"
    BANK_CREDIT_OVERDUE = "BANK_CREDIT_OVERDUE"
    SETTLEMENT_AMOUNT_MISMATCH = "SETTLEMENT_AMOUNT_MISMATCH"
    UNMATCHED_INBOUND_CREDIT = "UNMATCHED_INBOUND_CREDIT"
    REVERSAL_UNMATCHED = "REVERSAL_UNMATCHED"
    DUPLICATE_CREDIT = "DUPLICATE_CREDIT"
    DISPUTE_PENDING = "DISPUTE_PENDING"


class DeclineReason(StrEnum):
    """`expected_decline_reason`, §1.6 — nullable, so used as `DeclineReason | None`."""

    POLICY = "policy"
    CONFIDENCE = "confidence"


class ExpectedJournalLeg(BaseModel):
    """One debit-or-credit leg of an expected correcting entry, §3.4's templates."""

    model_config = ConfigDict(frozen=True)

    account_code: str
    account_name: str
    debit: NonNegPaise
    credit: NonNegPaise


class ExpectedJournalEntry(BaseModel):
    """One template instantiation: the template ID plus its expected legs."""

    model_config = ConfigDict(frozen=True)

    template_id: str
    legs: tuple[ExpectedJournalLeg, ...]


class GroundTruthCase(BaseModel):
    """Ground-truth schema per reconciliation case, §1.6, verbatim field list."""

    model_config = ConfigDict(frozen=True)

    case_id: str
    expected_outcome_state: OutcomeState
    ground_truth_exception_class: ExceptionClass
    ground_truth_exception_subtype: ExceptionSubtype
    expected_linked_source_records: tuple[str, ...]
    expected_resolution: str | None
    expected_journal_entries: tuple[ExpectedJournalEntry, ...]
    expected_template_ids: tuple[str, ...]
    expected_decline_reason: DeclineReason | None
    should_auto_apply: bool
