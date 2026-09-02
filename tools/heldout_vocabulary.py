"""A second, disjoint surface vocabulary for the reference batch (§5.3, extended).

## Why this exists

`data/reference/` and every seed of it grade at 1.0000 on the deterministic
arm — state accuracy, macro subtype precision and macro subtype recall, at
seeds 0, 1, 2, 5, 7 and 11. That is not a result. An adversarial review of the
graded path found the mechanism: four of the pipeline's decision boundaries are
keyword lists whose coverage of the generator's own string pools is 100% hit,
0% miss, by construction.

| pipeline constant | generator pool it separates perfectly |
|---|---|
| `pipeline.case_assembly._RAZORPAY_MARKER` | `SETTLEMENT_PARTIES` vs `NAMED_COUNTERPARTIES` |
| `pipeline.case_assembly._REVERSAL_KEYWORDS` | `REVERSAL_TEMPLATES` |
| `pipeline.case_assembly._BANK_CHARGE_KEYWORDS` | `_BANK_CHARGE_NARRATIONS` |
| `pipeline.classifier._BANKING_BOILERPLATE_WORDS` | `OPAQUE_CREDIT_NARRATIONS` |
| `pipeline.policy._TAX_POSITION_MARKERS` | `TAX_SIGNATURES` |

The §4.1 import guard cannot see any of this: it is a *data* coupling, not an
import. Nor can a seed sweep, because every pool is a module constant that a
seed does not vary. `tests/` contains no case that feeds the pipeline a string
the generator could not have written, which is why 543 tests pass over it.

## What this vocabulary is, and what makes it a fair test

The same 150 cases, the same injected anomalies, the same ground truth — only
the surface strings change. Structure is untouched, so every label stays
correct by construction; what moves is whether a boundary drawn as a literal
still finds it.

Every replacement below is real Indian bank-statement and settlement-adjustment
vocabulary, and every one stays *decidable by a competent reader*: a human
looking at `RZRPAY SOFTWARE PVT LTD` knows who that is, and a human looking at
`SUSPENSE-CR` knows it names nobody. The test is not "can the pipeline read
mangled text" — it is "was the boundary drawn at the concept or at the string".

That distinction is the whole §5.4 question. A keyword list generalizes to
exactly the strings it enumerates. Whether a model generalizes past them is
the one claim this repository makes about its LLM slot that its own reference
batch has never been able to test.
"""

from __future__ import annotations

# --- Parties. Banks truncate and re-space beneficiary names constantly; none
# of these contains the literal "RAZORPAY", and all four are unmistakable. ---

SETTLEMENT_PARTIES = (
    "RZRPAY SOFTWARE PVT LTD",
    "RAZOR PAY SOFTWARE PRIVATE LTD",
    "RZP SOFTWARE PVT LTD MUMBAI",
    "RZPY SOFTWARE",
)

NAMED_COUNTERPARTIES = (
    "MEHTA TRADING CO",
    "KAVERI AGRO INDUSTRIES",
    "ANAND IYER",
    "SILVERLINE PACKAGING",
    "DECCAN AUTO PARTS",
    "NEHA REDDY",
    "ORIENT CERAMICS LTD",
    "VIJAY DISTRIBUTORS",
)

# --- Ledger narrations. A different ERP's export wording. ---

LEDGER_NARRATION_TEMPLATES = (
    "PG {method} collection",
    "Gateway settlement posted ({method})",
    "Accounting import - PG {method}",
    "Acquirer posting - {method}",
    "PG {method} transaction",
)

# --- The two FR-06 policy exclusions, in words a different finance team would
# use. Same two concepts (§2.5's withholding deduction and its indirect-tax
# credit review); no "TDS", no "194", no "ITC", no "INPUT TAX CREDIT". ---

TAX_SIGNATURES = (
    "Withholding deduction, marketplace facilitator remittance",
    "Indirect-tax credit eligibility review, merchant discount component",
)

NEUTRAL_ADJUSTMENT_DESCRIPTIONS = (
    "Payout adjustment",
    "Held balance released",
    "Rolling reserve movement",
    "Merchant ledger correction",
    "Payout recovery entry",
)

# --- Bank narrations. Shape is preserved slot-for-slot: a template whose
# `{ref}` is whitespace-delimited keeps it whitespace-delimited, because that
# is exactly what separates FR-09 tier 0 from tier 1 (§4.6). Only the words
# around the slots change. ---

CLEAN_CREDIT_TEMPLATES = (
    "INW REM {party} {ref}",
    "RTGS INW {party} UTR {ref}",
    "IMPS INW {party} {ref}",
    "FT CR {party} {ref}",
    "NEFT INW REM {party} {ref}",
)

EMBEDDED_CREDIT_TEMPLATES = (
    "INW-REM-{party}-{ref}",
    "FT/{ref}/{party}",
    "RTGS-{ref}-{party}-INW",
    "IMPS/A2A/{ref}/{party}",
    "FT REMIT-INW*{ref}*{party}",
)

ABSENT_CREDIT_TEMPLATES = (
    "INW REM {party}",
    "RTGS INW {party}",
    "IMPS INW {party}",
    "FT CR {party}",
    "NEFT INW REM {party}",
)

DEBIT_TEMPLATES = (
    "OUTW REM {party} {ref}",
    "RTGS OUTW {party} UTR {ref}",
    "IMPS OUTW {party} {ref}",
    "OUTW-REM-{party}-{ref}",
    "FT REMIT-OUTW*{ref}*{party}",
)

# None of these contains "REVERSAL", "RETURN", "REV-" or "RET-", and every one
# still says, in bank English, that a credit was undone.
REVERSAL_TEMPLATES = (
    "CR CANCELLED-{ref}",
    "NEFT RTN {ref} {party}",
    "RTN-FT/{ref}/{party}",
    "UNDO-{ref}-{party}",
    "FUNDS SENT BACK {ref}",
)

# Opaque credits: a real statement's suspense/clearing wording. Every token
# here is ordinary bank jargon naming no counterparty, and not one of them is
# in `pipeline.classifier._BANKING_BOILERPLATE_WORDS` — which is the point.
OPAQUE_CREDIT_NARRATIONS = (
    "INW CLG",
    "SUNDRY RECEIPT",
    "CLG ADJ ENTRY",
    "UNIDENTIFIED CR",
    "PROCEEDS RECD",
    "SUSPENSE-CR",
)

# Charges that never say "CHARGE" or "FEE".
BANK_CHARGE_NARRATIONS = (
    "SMS ALERT RECOVERY",
    "ANNUAL MAINT RECOVERY",
    "CHQ BOOK ISSUE COST",
    "ATM MAINT COST",
    "DEBIT CARD ANNUAL COST",
)
