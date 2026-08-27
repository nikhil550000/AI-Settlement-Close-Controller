"""Canonical input schemas, per spec.md §3.1.

Four separate schemas — not one denormalized table. A recon line and a
ledger entry are different things with different lifecycles; collapsing
them would hide the exact mismatches the Controller exists to find.

All four are treated as immutable records: `recon_line` is raw external
evidence explicitly never mutated by the Controller, and the other three
are likewise append-only facts (settlement headers, adapted bank lines,
posted journal entries) rather than working state that gets edited in
place.
"""

from __future__ import annotations

import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator

from pipeline.money import NonNegPaise


class RazorpayEntityType(StrEnum):
    PAYMENT = "payment"
    REFUND = "refund"
    TRANSFER = "transfer"
    ADJUSTMENT = "adjustment"


class SettlementStatus(StrEnum):
    CREATED = "created"
    PROCESSED = "processed"
    FAILED = "failed"


class BankProfile(StrEnum):
    HDFC = "hdfc"
    ICICI = "icici"
    AXIS = "axis"


class LedgerSource(StrEnum):
    MANUAL = "manual"
    ERP_IMPORT = "erp_import"
    CONTROLLER_ADJUSTMENT = "controller_adjustment"


class ReconLine(BaseModel):
    """One row per `entity_id` from Razorpay's `/v1/settlements/recon/combined`.

    Raw external evidence, never mutated by the Controller.

    `posted_at` is emitted as constant null by the generator and MUST NOT
    be read as evidence anywhere in the pipeline (REV-14) — families
    1, 2, 3 and 5 are defined by absence from the merchant ledger, and
    this field is Razorpay-side, not ledger-side. It is kept only for
    payload-shape fidelity.
    """

    model_config = ConfigDict(frozen=True)

    entity_id: str
    type: RazorpayEntityType
    debit: NonNegPaise
    credit: NonNegPaise
    amount: NonNegPaise
    fee: NonNegPaise
    tax: NonNegPaise
    on_hold: bool
    settled: bool
    created_at: int
    settled_at: int | None = None
    settlement_id: str | None = None
    settlement_utr: str | None = None
    payment_id: str | None = None
    order_id: str | None = None
    posted_at: int | None = None
    credit_type: str
    dispute_id: str | None = None
    description: str | None = None
    method: str | None = None


class Settlement(BaseModel):
    """One row per `settlement_id`. Matches the verified Razorpay entity exactly."""

    model_config = ConfigDict(frozen=True)

    id: str
    amount: NonNegPaise
    status: SettlementStatus
    fees: NonNegPaise
    tax: NonNegPaise
    utr: str
    created_at: int


class BankLine(BaseModel):
    """Post-adapter canonical shape a bank statement line normalizes to.

    Not a Razorpay API shape — FR-08's column-mapping adapter produces
    this from any of the three bank format profiles.
    """

    model_config = ConfigDict(frozen=True)

    line_id: str
    value_date: datetime.date
    narration: str
    bank_ref_no: str | None = None
    withdrawal_paise: NonNegPaise
    deposit_paise: NonNegPaise
    closing_balance_paise: NonNegPaise
    bank_profile: BankProfile


class LedgerEntry(BaseModel):
    """Restates the canonical journal schema locked in §1.5.

    `resolution_id` and `case_id` complete that schema (not revise it) to
    make invariant 1.7.4 — idempotency on `(case_id, resolution_id)` —
    implementable; both are set if and only if
    `source == controller_adjustment`.
    """

    model_config = ConfigDict(frozen=True)

    journal_entry_id: str
    date: datetime.date
    account_code: str
    account_name: str
    debit: NonNegPaise
    credit: NonNegPaise
    reference: str
    narration: str
    source: LedgerSource
    resolution_id: str | None = None
    case_id: str | None = None

    @model_validator(mode="after")
    def _resolution_fields_match_source(self) -> "LedgerEntry":
        is_controller_adjustment = self.source is LedgerSource.CONTROLLER_ADJUSTMENT
        has_resolution_id = self.resolution_id is not None
        has_case_id = self.case_id is not None
        if has_resolution_id != is_controller_adjustment or has_case_id != is_controller_adjustment:
            raise ValueError(
                "resolution_id and case_id must be set if and only if "
                "source is controller_adjustment (spec.md §3.1)"
            )
        return self
