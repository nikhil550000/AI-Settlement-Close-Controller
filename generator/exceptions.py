"""Exception, tax, ambiguous populations — the settlement-anchored rows of
spec.md §3.5's case-allocation table not already built by session 2.1's
`generator/families.py`: family-4 no-op (12), family-4 date-error (5),
FR-06 tax positions (12), `SETTLEMENT_UTR_MISSING` (5),
`BANK_CREDIT_OVERDUE` (5), `SETTLEMENT_AMOUNT_MISMATCH` (4),
`DISPUTE_PENDING` chargebacks (5), and `AMBIGUOUS_CASE` (9) — 57 cases,
which combined with session 2.1's 50 and the "Fully clean" 18 completes
the 125 settlement-anchored total.

This is the first module to generate `bank_line` records for cases that
*do* carry a landed credit — REV-17's "98 with-credit" settlement-anchored
population, membership `18 (clean) + 40 (families 1/2/3/5) + 5
(date-error) + 12 (FR-06) + 5 (UTR-missing) + 4 (amount-mismatch) + 5
(dispute-pending) + 9 (ambiguous) = 98`, computed independently from
REV-17's own arithmetic (`125 - 27 = 98`) as a cross-check.

Every case reuses `generator/clean.py`'s and `generator/families.py`'s
existing building blocks (`_generate_payment`, `_new_settlement_shell`,
`_n_payments`, `_finalize_settlement`, `_account_leg`,
`add_settlement_credit`, `build_adjustment_line`) rather than
duplicating them — per the same reasoning session 2.1 logged for reusing
session 1.3's clean-path helpers.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

from generator.bank_lines import add_settlement_credit
from generator.clean import (
    ACCOUNT_RAZORPAY_CLEARING,
    PostingVariant,
    _generate_payment,
    _hex_id,
    _payment_amount_paise,
    settlement_created_timestamp,
)
from generator.families import (
    ACCOUNT_SALES_RETURNS_AND_ALLOWANCES,
    FamilyBatch,
    _finalize_settlement,
    _n_payments,
    _new_settlement_shell,
    build_adjustment_line,
)
from generator.narration import TAX_SIGNATURES, ledger_narration, random_payment_method, random_utr
from generator.timing import subtract_working_days
from pipeline.ground_truth import (
    DeclineReason,
    ExceptionClass,
    ExceptionSubtype,
    GroundTruthCase,
    OutcomeState,
)
from pipeline.money import Paise
from pipeline.schemas import LedgerEntry, LedgerSource

N_FAMILY_4_NO_OP = 12
N_FAMILY_4_DATE_ERROR = 5
N_FR06_TAX = 12
N_SETTLEMENT_UTR_MISSING = 5
N_BANK_CREDIT_OVERDUE = 5
N_SETTLEMENT_AMOUNT_MISMATCH = 4
N_DISPUTE_PENDING = 5
N_AMBIGUOUS = 9

_TAX_SIGNATURES = TAX_SIGNATURES
"""§4.2 Slot C: "A 194-O deduction has a signature in the adjustment line."

FR-06's second exclusion (GST ITC eligibility on MDR) is given the same
adjustment-line shape for consistency — same schema, same unposted-line
mechanism, only the narration signature differs — rather than inventing a
second construction with no COA support (§3.2 fixes the chart of accounts
at seven, none of them ITC/TDS-specific). Logged in BUILDLOG.md, Decided.
The strings themselves moved to `generator/narration.py` in session 2.3,
which owns every free-text string the generator writes.
"""


def _windowed_settlement_shell(
    rng: random.Random, snapshot_date: date, *, elapsed_working_days: int
) -> tuple[str, str, int]:
    """A settlement shell dated `elapsed_working_days` before the snapshot (§3.3's T+2 rule).

    The *date* is chosen deliberately here rather than drawn, because the
    timing-residual rule compares dates and precise placement relative to
    the T+2 boundary is this population's evidence. The time of day is
    drawn exactly as every other population draws it: session 2.2 pinned
    these settlements to midnight UTC, which left `created_at % 86400 == 0`
    identifying the two window-anchored populations outright — a timestamp
    block of the kind §3.5's fingerprint control forbids.
    """
    created_date = subtract_working_days(snapshot_date, elapsed_working_days)
    settlement_id = _hex_id(rng, "setl_")
    utr = random_utr(rng)
    return settlement_id, utr, settlement_created_timestamp(rng, created_date)


def _clean_payments(
    rng: random.Random, settlement_id: str, utr: str, settlement_created_at: int, n_payments: int
) -> tuple[list, list]:
    recon_lines: list = []
    ledger_entries: list = []
    for _ in range(n_payments):
        recon_line, legs = _generate_payment(rng, settlement_id, utr, settlement_created_at, posting=PostingVariant.CLEAN)
        recon_lines.append(recon_line)
        ledger_entries.extend(legs)
    return recon_lines, ledger_entries


# --- Family-4 no-op: lag still inside the T+2 working-day window. ---


def generate_family_4_no_op_batch(rng: random.Random, snapshot_date: date, n_cases: int = N_FAMILY_4_NO_OP) -> FamilyBatch:
    """Family-4 no-op — `EXPECTED_TIMING_DIFFERENCE`, `AUTO_MATCHED`. No accounting error; no bank credit yet, by design."""
    batch = FamilyBatch()
    for _ in range(n_cases):
        elapsed = rng.choice((0, 1, 2))
        settlement_id, utr, settlement_created_at = _windowed_settlement_shell(
            rng, snapshot_date, elapsed_working_days=elapsed
        )
        recon_lines, ledger_entries = _clean_payments(rng, settlement_id, utr, settlement_created_at, _n_payments(rng))
        settlement = _finalize_settlement(settlement_id, utr, settlement_created_at, recon_lines)

        batch.settlements.append(settlement)
        batch.recon_lines.extend(recon_lines)
        batch.ledger_entries.extend(ledger_entries)
        # No bank_line: still inside the window (REV-17's 27-case no-credit set).
        batch.ground_truth.append(
            GroundTruthCase(
                case_id=settlement.id,
                expected_outcome_state=OutcomeState.AUTO_MATCHED,
                ground_truth_exception_class=ExceptionClass.EXPECTED_TIMING_DIFFERENCE,
                ground_truth_exception_subtype=ExceptionSubtype.NONE,
                expected_linked_source_records=(settlement.id, *(line.entity_id for line in recon_lines)),
                expected_resolution=None,
                expected_journal_entries=(),
                expected_template_ids=(),
                expected_decline_reason=None,
                should_auto_apply=False,
            )
        )
    return batch


# --- BANK_CREDIT_OVERDUE: window elapsed, no credit landed. ---


def generate_bank_credit_overdue_batch(
    rng: random.Random, snapshot_date: date, n_cases: int = N_BANK_CREDIT_OVERDUE
) -> FamilyBatch:
    """`OPERATIONAL_EXCEPTION`/`BANK_CREDIT_OVERDUE`, `EXTERNAL_ACTION_REQUIRED`. Books clean; money hasn't landed."""
    batch = FamilyBatch()
    for _ in range(n_cases):
        elapsed = rng.choice((3, 4, 5, 6))
        settlement_id, utr, settlement_created_at = _windowed_settlement_shell(
            rng, snapshot_date, elapsed_working_days=elapsed
        )
        recon_lines, ledger_entries = _clean_payments(rng, settlement_id, utr, settlement_created_at, _n_payments(rng))
        settlement = _finalize_settlement(settlement_id, utr, settlement_created_at, recon_lines)

        batch.settlements.append(settlement)
        batch.recon_lines.extend(recon_lines)
        batch.ledger_entries.extend(ledger_entries)
        # No bank_line: window has elapsed (REV-17's 27-case no-credit set).
        batch.ground_truth.append(
            GroundTruthCase(
                case_id=settlement.id,
                expected_outcome_state=OutcomeState.EXTERNAL_ACTION_REQUIRED,
                ground_truth_exception_class=ExceptionClass.OPERATIONAL_EXCEPTION,
                ground_truth_exception_subtype=ExceptionSubtype.BANK_CREDIT_OVERDUE,
                expected_linked_source_records=(settlement.id, *(line.entity_id for line in recon_lines)),
                expected_resolution=(
                    f"Settlement window elapsed with no matching bank credit for settlement "
                    f"{settlement.id} — escalate to Razorpay/bank."
                ),
                expected_journal_entries=(),
                expected_template_ids=(),
                expected_decline_reason=None,
                should_auto_apply=False,
            )
        )
    return batch


# --- Family-4 date-error: credit landed, accounts and amount both correct, date wrong. ---


def generate_family_4_date_error_batch(
    rng: random.Random, snapshot_date: date, n_cases: int = N_FAMILY_4_DATE_ERROR
) -> FamilyBatch:
    """`ACCOUNTING_CORRECTION`/`MISPOSTING` (REV-19: wrong period), `REVIEW_REQUIRED`/`policy` (REV-11)."""
    batch = FamilyBatch()
    for _ in range(n_cases):
        settlement_id, utr, settlement_created_at = _new_settlement_shell(rng, snapshot_date)
        n_payments = _n_payments(rng)
        anomaly_index = rng.randrange(n_payments)

        recon_lines: list = []
        ledger_entries: list = []
        anomaly_recon_line = None
        anomaly_ledger_entries: list = []
        for i in range(n_payments):
            recon_line, legs = _generate_payment(rng, settlement_id, utr, settlement_created_at, posting=PostingVariant.CLEAN)
            recon_lines.append(recon_line)
            if i == anomaly_index:
                # Correct accounts, correct amounts — only the posted date
                # is wrong, shifted a full month back across a period
                # boundary (REV-05/REV-19: "accounts and amount are both
                # correct" is the entire premise of this variant).
                shifted_date = datetime.fromtimestamp(recon_line.created_at, tz=timezone.utc).date() - timedelta(days=32)
                legs = [leg.model_copy(update={"date": shifted_date}) for leg in legs]
                anomaly_recon_line = recon_line
                anomaly_ledger_entries = legs
            ledger_entries.extend(legs)

        assert anomaly_recon_line is not None
        settlement = _finalize_settlement(settlement_id, utr, settlement_created_at, recon_lines)

        batch.settlements.append(settlement)
        batch.recon_lines.extend(recon_lines)
        batch.ledger_entries.extend(ledger_entries)
        # Precondition for this variant (REV-05): the credit has landed.
        add_settlement_credit(batch, rng, settlement=settlement, snapshot_date=snapshot_date)
        batch.ground_truth.append(
            GroundTruthCase(
                case_id=settlement.id,
                expected_outcome_state=OutcomeState.REVIEW_REQUIRED,
                ground_truth_exception_class=ExceptionClass.ACCOUNTING_CORRECTION,
                ground_truth_exception_subtype=ExceptionSubtype.MISPOSTING,
                expected_linked_source_records=(
                    anomaly_recon_line.entity_id,
                    settlement.id,
                    *(je.journal_entry_id for je in anomaly_ledger_entries),
                ),
                expected_resolution=None,
                expected_journal_entries=(),  # REV-11: no delta entry exists to post
                expected_template_ids=(),
                expected_decline_reason=DeclineReason.POLICY,
                should_auto_apply=False,
            )
        )
    return batch


# --- FR-06 tax positions: same shape as family 5's adjustment, policy-declined. ---


def generate_fr06_tax_batch(rng: random.Random, snapshot_date: date, n_cases: int = N_FR06_TAX) -> FamilyBatch:
    """`ACCOUNTING_CORRECTION`/`OMISSION`, `REVIEW_REQUIRED`/`policy` (§2.5/FR-06: 194-O, GST ITC eligibility)."""
    batch = FamilyBatch()
    for i in range(n_cases):
        settlement_id, utr, settlement_created_at = _new_settlement_shell(rng, snapshot_date)
        recon_lines, ledger_entries = _clean_payments(rng, settlement_id, utr, settlement_created_at, _n_payments(rng))

        signature = _TAX_SIGNATURES[i % len(_TAX_SIGNATURES)]
        adj_line = build_adjustment_line(rng, settlement_id, utr, settlement_created_at, recon_lines, description=signature)
        recon_lines.append(adj_line)
        # No ledger legs posted for adj_line — same unposted shape as family 5, but policy-declined, not auto-closed.

        settlement = _finalize_settlement(settlement_id, utr, settlement_created_at, recon_lines)

        batch.settlements.append(settlement)
        batch.recon_lines.extend(recon_lines)
        batch.ledger_entries.extend(ledger_entries)
        add_settlement_credit(batch, rng, settlement=settlement, snapshot_date=snapshot_date)
        batch.ground_truth.append(
            GroundTruthCase(
                case_id=settlement.id,
                expected_outcome_state=OutcomeState.REVIEW_REQUIRED,
                ground_truth_exception_class=ExceptionClass.ACCOUNTING_CORRECTION,
                ground_truth_exception_subtype=ExceptionSubtype.OMISSION,
                expected_linked_source_records=(adj_line.entity_id, settlement.id),
                expected_resolution=None,
                expected_journal_entries=(),
                expected_template_ids=(),
                expected_decline_reason=DeclineReason.POLICY,
                should_auto_apply=False,
            )
        )
    return batch


# --- SETTLEMENT_UTR_MISSING: processed, no UTR, no bank-side anchor. ---


def generate_settlement_utr_missing_batch(
    rng: random.Random, snapshot_date: date, n_cases: int = N_SETTLEMENT_UTR_MISSING
) -> FamilyBatch:
    """`OPERATIONAL_EXCEPTION`/`SETTLEMENT_UTR_MISSING`, `EXTERNAL_ACTION_REQUIRED`.

    `settlement.utr == ""` is the trigger (§3.1's schema states `utr` as
    plain, non-nullable `string`; empty string is the "no UTR" value
    within that type). The bank credit still lands — REV-17 counts this
    population in the 98 with-credit set — but carries no UTR to embed.
    """
    batch = FamilyBatch()
    for _ in range(n_cases):
        settlement_id, _utr, settlement_created_at = _new_settlement_shell(rng, snapshot_date)
        utr = ""
        recon_lines, ledger_entries = _clean_payments(rng, settlement_id, utr, settlement_created_at, _n_payments(rng))
        settlement = _finalize_settlement(settlement_id, utr, settlement_created_at, recon_lines)

        batch.settlements.append(settlement)
        batch.recon_lines.extend(recon_lines)
        batch.ledger_entries.extend(ledger_entries)
        add_settlement_credit(batch, rng, settlement=settlement, snapshot_date=snapshot_date)
        batch.ground_truth.append(
            GroundTruthCase(
                case_id=settlement.id,
                expected_outcome_state=OutcomeState.EXTERNAL_ACTION_REQUIRED,
                ground_truth_exception_class=ExceptionClass.OPERATIONAL_EXCEPTION,
                ground_truth_exception_subtype=ExceptionSubtype.SETTLEMENT_UTR_MISSING,
                expected_linked_source_records=(settlement.id, *(line.entity_id for line in recon_lines)),
                expected_resolution=(
                    f"Settlement {settlement.id} is processed but carries no UTR — "
                    "request UTR from Razorpay before reconciling."
                ),
                expected_journal_entries=(),
                expected_template_ids=(),
                expected_decline_reason=None,
                should_auto_apply=False,
            )
        )
    return batch


# --- SETTLEMENT_AMOUNT_MISMATCH: header amount != recon-line total (the one deliberate §3.5 invariant violation). ---


def generate_settlement_amount_mismatch_batch(
    rng: random.Random, snapshot_date: date, n_cases: int = N_SETTLEMENT_AMOUNT_MISMATCH
) -> FamilyBatch:
    """`OPERATIONAL_EXCEPTION`/`SETTLEMENT_AMOUNT_MISMATCH`, `EXTERNAL_ACTION_REQUIRED`.

    The bank actually credits the true (recon-line) total — it's the
    settlement header record that's wrong, per §3.3's trigger text
    ("Settlement header amount ≠ sum of its recon lines net of fees and
    tax"), not the merchant's books or the cash that moved.
    """
    batch = FamilyBatch()
    for _ in range(n_cases):
        settlement_id, utr, settlement_created_at = _new_settlement_shell(rng, snapshot_date)
        recon_lines, ledger_entries = _clean_payments(rng, settlement_id, utr, settlement_created_at, _n_payments(rng))
        true_settlement = _finalize_settlement(settlement_id, utr, settlement_created_at, recon_lines)

        delta = Paise(rng.randint(1, 50) * 10_000)  # ₹1.00-₹50.00, always nonzero and never drives amount negative
        mismatched_settlement = true_settlement.model_copy(update={"amount": Paise(true_settlement.amount + delta)})

        batch.settlements.append(mismatched_settlement)
        batch.recon_lines.extend(recon_lines)
        batch.ledger_entries.extend(ledger_entries)
        # The bank credits the *true* recon-line total; the header is the wrong record.
        add_settlement_credit(
            batch,
            rng,
            settlement=mismatched_settlement,
            snapshot_date=snapshot_date,
            amount=true_settlement.amount,
        )
        batch.ground_truth.append(
            GroundTruthCase(
                case_id=mismatched_settlement.id,
                expected_outcome_state=OutcomeState.EXTERNAL_ACTION_REQUIRED,
                ground_truth_exception_class=ExceptionClass.OPERATIONAL_EXCEPTION,
                ground_truth_exception_subtype=ExceptionSubtype.SETTLEMENT_AMOUNT_MISMATCH,
                expected_linked_source_records=(
                    mismatched_settlement.id,
                    *(line.entity_id for line in recon_lines),
                ),
                expected_resolution=(
                    f"Settlement {mismatched_settlement.id} header amount does not match its recon-line "
                    "total — confirm correct figure with Razorpay before reconciling."
                ),
                expected_journal_entries=(),
                expected_template_ids=(),
                expected_decline_reason=None,
                should_auto_apply=False,
            )
        )
    return batch


# --- DISPUTE_PENDING: FR-05's chargeback population, detection/classification only (FR-05 recognition not built). ---


def generate_dispute_pending_batch(
    rng: random.Random, snapshot_date: date, n_cases: int = N_DISPUTE_PENDING
) -> FamilyBatch:
    """`OPERATIONAL_EXCEPTION`/`DISPUTE_PENDING`, `EXTERNAL_ACTION_REQUIRED`.

    FR-05 (stretch, not committed) is not built this session: "If FR-05 is
    not built, chargeback cases remain in the dataset and are detected and
    classified, terminating in `EXTERNAL_ACTION_REQUIRED` without a posted
    entry" (§2.4). Books stay correctly booked; `dispute_id` on one payment
    is the only anomaly.
    """
    batch = FamilyBatch()
    for _ in range(n_cases):
        settlement_id, utr, settlement_created_at = _new_settlement_shell(rng, snapshot_date)
        n_payments = _n_payments(rng)
        disputed_index = rng.randrange(n_payments)

        recon_lines: list = []
        ledger_entries: list = []
        disputed_line = None
        for i in range(n_payments):
            recon_line, legs = _generate_payment(rng, settlement_id, utr, settlement_created_at, posting=PostingVariant.CLEAN)
            if i == disputed_index:
                recon_line = recon_line.model_copy(update={"dispute_id": _hex_id(rng, "disp_")})
                disputed_line = recon_line
            recon_lines.append(recon_line)
            ledger_entries.extend(legs)

        assert disputed_line is not None
        settlement = _finalize_settlement(settlement_id, utr, settlement_created_at, recon_lines)

        batch.settlements.append(settlement)
        batch.recon_lines.extend(recon_lines)
        batch.ledger_entries.extend(ledger_entries)
        add_settlement_credit(batch, rng, settlement=settlement, snapshot_date=snapshot_date)
        batch.ground_truth.append(
            GroundTruthCase(
                case_id=settlement.id,
                expected_outcome_state=OutcomeState.EXTERNAL_ACTION_REQUIRED,
                ground_truth_exception_class=ExceptionClass.OPERATIONAL_EXCEPTION,
                ground_truth_exception_subtype=ExceptionSubtype.DISPUTE_PENDING,
                expected_linked_source_records=(disputed_line.entity_id, settlement.id),
                expected_resolution=(
                    f"Dispute {disputed_line.dispute_id} open on payment {disputed_line.entity_id} — "
                    "await resolution before any recognition entry."
                ),
                expected_journal_entries=(),
                expected_template_ids=(),
                expected_decline_reason=None,
                should_auto_apply=False,
            )
        )
    return batch


# --- AMBIGUOUS_CASE (settlement-anchored): a ledger entry with no corroborating recon-line evidence. ---


def generate_ambiguous_batch(rng: random.Random, snapshot_date: date, n_cases: int = N_AMBIGUOUS) -> FamilyBatch:
    """`AMBIGUOUS_CASE`, `ABSTAINED`.

    §3.3: "a required piece of evidence is absent." Concretely: an
    otherwise-clean settlement plus one extra, internally-balanced ledger
    entry pair whose `reference` names an `entity_id` that does not exist
    anywhere in the batch's recon lines — nothing corroborates it, and
    nothing refutes it either, so no template's evidence predicate can
    fire and no single defensible treatment exists.
    """
    batch = FamilyBatch()
    for _ in range(n_cases):
        settlement_id, utr, settlement_created_at = _new_settlement_shell(rng, snapshot_date)
        recon_lines, ledger_entries = _clean_payments(rng, settlement_id, utr, settlement_created_at, _n_payments(rng))
        settlement = _finalize_settlement(settlement_id, utr, settlement_created_at, recon_lines)

        phantom_ref = _hex_id(rng, "rfnd_")
        phantom_amount = _payment_amount_paise(rng)
        entry_date = datetime.fromtimestamp(settlement_created_at, tz=timezone.utc).date()
        # Shared pool, same as every other ledger entry (§3.5): the case's
        # evidence is that `reference` resolves to nothing in the batch, and
        # session 2.2's narration said so in words, which is the artifact the
        # fingerprint control exists to remove.
        narration = ledger_narration(rng, method=random_payment_method(rng))
        phantom_entries = [
            LedgerEntry(
                journal_entry_id=_hex_id(rng, "je_"),
                date=entry_date,
                account_code=ACCOUNT_SALES_RETURNS_AND_ALLOWANCES[0],
                account_name=ACCOUNT_SALES_RETURNS_AND_ALLOWANCES[1],
                debit=phantom_amount,
                credit=Paise(0),
                reference=phantom_ref,
                narration=narration,
                source=LedgerSource.ERP_IMPORT,
                resolution_id=None,
                case_id=None,
            ),
            LedgerEntry(
                journal_entry_id=_hex_id(rng, "je_"),
                date=entry_date,
                account_code=ACCOUNT_RAZORPAY_CLEARING[0],
                account_name=ACCOUNT_RAZORPAY_CLEARING[1],
                debit=Paise(0),
                credit=phantom_amount,
                reference=phantom_ref,
                narration=narration,
                source=LedgerSource.ERP_IMPORT,
                resolution_id=None,
                case_id=None,
            ),
        ]
        ledger_entries.extend(phantom_entries)

        batch.settlements.append(settlement)
        batch.recon_lines.extend(recon_lines)
        batch.ledger_entries.extend(ledger_entries)
        add_settlement_credit(batch, rng, settlement=settlement, snapshot_date=snapshot_date)
        batch.ground_truth.append(
            GroundTruthCase(
                case_id=settlement.id,
                expected_outcome_state=OutcomeState.ABSTAINED,
                ground_truth_exception_class=ExceptionClass.AMBIGUOUS_CASE,
                ground_truth_exception_subtype=ExceptionSubtype.NONE,
                expected_linked_source_records=(settlement.id, *(je.journal_entry_id for je in phantom_entries)),
                expected_resolution=(
                    f"Ledger references {phantom_ref} with no corroborating recon-line evidence in the "
                    f"batch for settlement {settlement.id} — insufficient evidence to determine treatment."
                ),
                expected_journal_entries=(),
                expected_template_ids=(),
                expected_decline_reason=None,
                should_auto_apply=False,
            )
        )
    return batch


EXCEPTION_POPULATIONS = (
    ("family_4_no_op", generate_family_4_no_op_batch),
    ("family_4_date_error", generate_family_4_date_error_batch),
    ("fr06_tax", generate_fr06_tax_batch),
    ("settlement_utr_missing", generate_settlement_utr_missing_batch),
    ("bank_credit_overdue", generate_bank_credit_overdue_batch),
    ("settlement_amount_mismatch", generate_settlement_amount_mismatch_batch),
    ("dispute_pending", generate_dispute_pending_batch),
    ("ambiguous", generate_ambiguous_batch),
)
"""This module's eight populations in generation order, named — see `FAMILY_POPULATIONS`."""


def generate_all_exception_batches(rng: random.Random, snapshot_date: date) -> FamilyBatch:
    """All eight populations this module owns, 57 cases combined (spec.md §3.5's remaining settlement-anchored rows)."""
    combined = FamilyBatch()
    for _name, generate in EXCEPTION_POPULATIONS:
        combined.extend(generate(rng, snapshot_date))
    return combined
