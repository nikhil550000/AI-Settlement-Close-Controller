"""The seven-account chart of accounts, per spec.md §3.2.

> 7 accounts, deliberately small. Every account is used by at least one
> committed FR-04 family — nothing speculative.

Lives under `pipeline/` for the same reason `pipeline/schemas.py` and
`pipeline/timing.py` do: the COA is written *into* records by the
generator but read *as evidence* by the graded path (the predicate
evaluator asks "is there a `Payment Gateway Charges` entry referencing
this payment?", and the validator will ask "is this account permitted in
this direction?"), and `pipeline/` must never import `generator/` (§4.1).
One definition, imported both ways, so a code can never drift between the
side that writes it and the side that grades it.

`Account` is a `NamedTuple` rather than a Pydantic model deliberately:
these are compile-time constants, not validated records, and the tuple
shape is what `generator/clean.py` and `generator/families.py` already
pass around.

**Not defined here:** §3.4's per-template allowed-account sets and its
global account-direction allowlist. Those are validation layers for
invariant 1.7.5 and belong to the validator (component 7, session 4.3),
not to the chart of accounts itself.
"""

from __future__ import annotations

from typing import NamedTuple


class Account(NamedTuple):
    """One row of §3.2's account-code table: the code and its denormalized name."""

    code: str
    name: str


ACCOUNT_BANK_ACCOUNT = Account("1010", "Bank Account")
ACCOUNT_RAZORPAY_CLEARING = Account("1020", "Razorpay Clearing")
ACCOUNT_SALES_REVENUE = Account("4010", "Sales Revenue")
ACCOUNT_SALES_RETURNS_AND_ALLOWANCES = Account("4020", "Sales Returns and Allowances")
ACCOUNT_PAYMENT_GATEWAY_CHARGES = Account("5010", "Payment Gateway Charges")
ACCOUNT_GST_ON_GATEWAY_CHARGES = Account("5020", "GST on Gateway Charges")
ACCOUNT_RAZORPAY_SETTLEMENT_ADJUSTMENTS = Account("4900", "Razorpay Settlement Adjustments")

CHART_OF_ACCOUNTS: tuple[Account, ...] = (
    ACCOUNT_BANK_ACCOUNT,
    ACCOUNT_RAZORPAY_CLEARING,
    ACCOUNT_SALES_REVENUE,
    ACCOUNT_SALES_RETURNS_AND_ALLOWANCES,
    ACCOUNT_PAYMENT_GATEWAY_CHARGES,
    ACCOUNT_GST_ON_GATEWAY_CHARGES,
    ACCOUNT_RAZORPAY_SETTLEMENT_ADJUSTMENTS,
)
"""All seven §3.2 accounts. Any entry using an account outside this set is
rejected before §1.7.5's balance check runs (§3.4) — that rejection is the
validator's job; this tuple is the set it checks against."""

ACCOUNT_BY_CODE: dict[str, Account] = {account.code: account for account in CHART_OF_ACCOUNTS}
