"""Clean-case generator, per spec.md §3.5 ("Record shape") and Phase 1's scope.

Phase 1 (spec.md §6, session 1.3) builds the **clean-case path only** — no
anomaly injection. That population is the "Fully clean" row of the §3.5
case-allocation table: `NONE` exception class, `AUTO_MATCHED` state, no
correction required. Every payment generated here is booked to the
merchant ledger exactly as accrual-basis bookkeeping (§3.0) requires, so
nothing is missing, mis-posted, or mis-timed for the model to find.

Refunds, adjustments, and bank-statement lines are deliberately out of
scope for this module. §3.5's refund/adjustment volumes and §3.6's orphan
population describe the *full* 150-case reference batch; injecting them
correctly requires the anomaly machinery (families 1-5, exception
subtypes) that Phase 2 sessions 2.1-2.2 build. Building a "correctly
booked refund" here would invent that machinery's shape ahead of time
without a spec'd definition of what a clean-refund posting looks like
beyond family 2's anomaly template — see BUILDLOG.md session 1.3, Decided.

Only one RNG instance is threaded through (no unseeded `random`, no second
instance created anywhere below), and the batch snapshot date is always a
parameter, never `datetime.now()`.
"""

from __future__ import annotations

import math
import random
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import Enum

from generator.bank_lines import add_settlement_credit
from generator.batch import GeneratedBatch
from generator.narration import PAYMENT_METHODS, ledger_narration, random_utr
from generator.rounding import percentage_of_paise
from pipeline import accounts
from pipeline.ground_truth import ExceptionClass, ExceptionSubtype, GroundTruthCase, OutcomeState
from pipeline.money import Paise
from pipeline.schemas import (
    LedgerEntry,
    LedgerSource,
    RazorpayEntityType,
    ReconLine,
    Settlement,
    SettlementStatus,
)

# §3.5 "Record shape": payments per settlement, truncated lognormal.
PAYMENTS_PER_SETTLEMENT_MU = math.log(10)
PAYMENTS_PER_SETTLEMENT_SIGMA = 0.5
PAYMENTS_PER_SETTLEMENT_MIN = 3
PAYMENTS_PER_SETTLEMENT_MAX = 25

# §3.5 "Money": lognormal, roughly ₹100-₹50,000, median near ₹1,500.
# Spec states the distribution shape and range but not sigma; chosen as
# generator config (§3.5's own framing: "a parameter change rather than a
# rewrite"), logged in BUILDLOG.md session 1.3, Decided.
PAYMENT_AMOUNT_MU = math.log(1500)
PAYMENT_AMOUNT_SIGMA = 0.8
PAYMENT_AMOUNT_MIN_RUPEES = 100
PAYMENT_AMOUNT_MAX_RUPEES = 50_000

FEE_PERCENT = Decimal("2")
GST_ON_FEE_PERCENT = Decimal("18")

# §3.2's chart of accounts is defined once, pipeline-side
# (`pipeline/accounts.py`), and re-exported here under the names session 1.3
# introduced so that the side that writes a code and the side that grades it
# cannot drift apart. Same reasoning as `pipeline/timing.py` (session 3.3).
ACCOUNT_BANK_ACCOUNT = accounts.ACCOUNT_BANK_ACCOUNT
ACCOUNT_RAZORPAY_CLEARING = accounts.ACCOUNT_RAZORPAY_CLEARING
ACCOUNT_SALES_REVENUE = accounts.ACCOUNT_SALES_REVENUE
ACCOUNT_PAYMENT_GATEWAY_CHARGES = accounts.ACCOUNT_PAYMENT_GATEWAY_CHARGES
ACCOUNT_GST_ON_GATEWAY_CHARGES = accounts.ACCOUNT_GST_ON_GATEWAY_CHARGES

_PAYMENT_METHODS = PAYMENT_METHODS  # moved to generator/narration.py, the one shared text pool (§3.5)

SETTLEMENT_MAX_DAYS_BACK = 10
"""Oldest a settlement may be, in calendar days before the batch snapshot.

Drawn identically by every settlement-anchored population, snapshot date
included as day 0. Session 2.2 drew 1..10 here while the two
window-anchored populations (family-4 no-op, `BANK_CREDIT_OVERDUE`) placed
their settlements by working-day arithmetic from the snapshot, which left
"created on the snapshot date" belonging to exactly one population — a
timestamp block of the kind §3.5's fingerprint control forbids.
"""


class PostingVariant(Enum):
    """How a payment's ledger legs are booked, keyed to the §3.2 families.

    The `recon_line` (Razorpay's own evidence) is identical across variants
    — it always reports what actually happened. Only the merchant's ledger
    legs vary, because these variants model *bookkeeping errors*, not
    different underlying transactions. Session 2.1 (§3.5/§3.4).
    """

    CLEAN = "clean"
    UNPOSTED_FEE_GROSS_CLEARING = "unposted_fee_gross_clearing"
    """Family 1 (T-01): fee/GST never posted; Clearing debited at gross
    (the only way `Dr Clearing / Cr Sales Revenue` balances without the
    expense legs) — matches T-01's evidence predicate (§3.4, REV-16):
    a `Sales Revenue` credit equal to gross `amount`."""

    NET_REVENUE = "net_revenue"
    """Family 3 (T-03): the net bank credit booked directly as revenue —
    `Dr Clearing (net) / Cr Sales Revenue (net)` — matches T-03's evidence
    predicate: a `Sales Revenue` credit equal to `amount - fee - tax`."""

    BANK_MISPOST = "bank_mispost"
    """Family 4 (T-04): fee/GST posted correctly, but the net leg lands on
    `Bank Account` instead of `Razorpay Clearing` — a premature bank debit
    before cash actually arrives (§3.2's family-4 wrong-account error)."""


CleanBatch = GeneratedBatch  # one container for every population (generator/batch.py)


def _hex_id(rng: random.Random, prefix: str, n_bytes: int = 8) -> str:
    return f"{prefix}{rng.getrandbits(n_bytes * 4):0{n_bytes}x}"


def _snapshot_unix_ts(snapshot_date: date) -> int:
    return int(datetime.combine(snapshot_date, time.min, tzinfo=timezone.utc).timestamp())


def settlement_created_timestamp(rng: random.Random, created_date: date) -> int:
    """A settlement's `created_at`: midnight UTC on `created_date` plus a random time of day.

    The intraday offset exists for one reason and it is a §3.5 fingerprint
    control, not realism: session 2.2's window-anchored populations sat at
    exactly midnight UTC while every other population carried an arbitrary
    offset, so `created_at % 86400 == 0` picked out seventeen cases from
    two named populations with no reference to any evidence.
    """
    return _snapshot_unix_ts(created_date) + rng.randint(0, 86_399)


def random_settlement_date(rng: random.Random, snapshot_date: date) -> date:
    """A settlement date drawn scenario-blind: uniform over the snapshot day and the ten before it."""
    return snapshot_date - timedelta(days=rng.randint(0, SETTLEMENT_MAX_DAYS_BACK))


def _truncated_lognormal_int(rng: random.Random, mu: float, sigma: float, lo: int, hi: int) -> int:
    value = round(rng.lognormvariate(mu, sigma))
    return max(lo, min(hi, value))


def _payment_amount_paise(rng: random.Random) -> Paise:
    rupees = rng.lognormvariate(PAYMENT_AMOUNT_MU, PAYMENT_AMOUNT_SIGMA)
    rupees = max(PAYMENT_AMOUNT_MIN_RUPEES, min(PAYMENT_AMOUNT_MAX_RUPEES, rupees))
    return Paise(int(round(rupees * 100)))


def _generate_payment(
    rng: random.Random,
    settlement_id: str,
    settlement_utr: str,
    settlement_created_at: int,
    posting: PostingVariant = PostingVariant.CLEAN,
) -> tuple[ReconLine, list[LedgerEntry]]:
    amount = _payment_amount_paise(rng)
    fee = percentage_of_paise(amount, FEE_PERCENT)
    tax = percentage_of_paise(fee, GST_ON_FEE_PERCENT)
    net = Paise(amount - fee - tax)

    entity_id = _hex_id(rng, "pay_")
    created_at = settlement_created_at - rng.randint(0, 2 * 86_400)
    entry_date = datetime.fromtimestamp(created_at, tz=timezone.utc).date()

    recon_line = ReconLine(
        entity_id=entity_id,
        type=RazorpayEntityType.PAYMENT,
        debit=Paise(0),
        credit=amount,
        amount=amount,
        fee=fee,
        tax=tax,
        on_hold=False,
        settled=True,
        created_at=created_at,
        settled_at=settlement_created_at,
        settlement_id=settlement_id,
        settlement_utr=settlement_utr,
        payment_id=None,
        order_id=_hex_id(rng, "order_"),
        posted_at=None,
        credit_type="default",
        dispute_id=None,
        description=None,
        method=rng.choice(_PAYMENT_METHODS),
    )

    if posting is PostingVariant.CLEAN:
        legs = [
            (ACCOUNT_RAZORPAY_CLEARING, net, Paise(0)),
            (ACCOUNT_PAYMENT_GATEWAY_CHARGES, fee, Paise(0)),
            (ACCOUNT_GST_ON_GATEWAY_CHARGES, tax, Paise(0)),
            (ACCOUNT_SALES_REVENUE, Paise(0), amount),
        ]
    elif posting is PostingVariant.UNPOSTED_FEE_GROSS_CLEARING:
        legs = [
            (ACCOUNT_RAZORPAY_CLEARING, amount, Paise(0)),
            (ACCOUNT_SALES_REVENUE, Paise(0), amount),
        ]
    elif posting is PostingVariant.NET_REVENUE:
        legs = [
            (ACCOUNT_RAZORPAY_CLEARING, net, Paise(0)),
            (ACCOUNT_SALES_REVENUE, Paise(0), net),
        ]
    elif posting is PostingVariant.BANK_MISPOST:
        legs = [
            (ACCOUNT_BANK_ACCOUNT, net, Paise(0)),
            (ACCOUNT_PAYMENT_GATEWAY_CHARGES, fee, Paise(0)),
            (ACCOUNT_GST_ON_GATEWAY_CHARGES, tax, Paise(0)),
            (ACCOUNT_SALES_REVENUE, Paise(0), amount),
        ]
    else:
        raise ValueError(f"unhandled posting variant: {posting}")

    # One shared pool, drawn identically for every posting variant (§3.5's
    # fingerprint control). The narration names neither the anomaly nor any
    # amount: session 2.2's text announced both, which made every family
    # trivially separable by string match and put a second, redundant copy
    # of the evidence into free text.
    narration = ledger_narration(rng, method=recon_line.method)

    ledger_entries = [
        LedgerEntry(
            journal_entry_id=_hex_id(rng, "je_"),
            date=entry_date,
            account_code=account_code,
            account_name=account_name,
            debit=debit,
            credit=credit,
            reference=entity_id,
            narration=narration,
            source=LedgerSource.ERP_IMPORT,
            resolution_id=None,
            case_id=None,
        )
        for (account_code, account_name), debit, credit in legs
    ]
    return recon_line, ledger_entries


def _generate_settlement(
    rng: random.Random, snapshot_date: date
) -> tuple[Settlement, list[ReconLine], list[LedgerEntry]]:
    created_at = settlement_created_timestamp(rng, random_settlement_date(rng, snapshot_date))
    settlement_id = _hex_id(rng, "setl_")
    utr = random_utr(rng)

    n_payments = _truncated_lognormal_int(
        rng,
        PAYMENTS_PER_SETTLEMENT_MU,
        PAYMENTS_PER_SETTLEMENT_SIGMA,
        PAYMENTS_PER_SETTLEMENT_MIN,
        PAYMENTS_PER_SETTLEMENT_MAX,
    )

    recon_lines: list[ReconLine] = []
    ledger_entries: list[LedgerEntry] = []
    for _ in range(n_payments):
        recon_line, legs = _generate_payment(rng, settlement_id, utr, created_at)
        recon_lines.append(recon_line)
        ledger_entries.extend(legs)

    total_credit = sum((line.credit for line in recon_lines), Paise(0))
    total_debit = sum((line.debit for line in recon_lines), Paise(0))
    total_fees = sum((line.fee for line in recon_lines), Paise(0))
    total_tax = sum((line.tax for line in recon_lines), Paise(0))
    settlement = Settlement(
        id=settlement_id,
        amount=Paise(total_credit - total_debit - total_fees - total_tax),
        status=SettlementStatus.PROCESSED,
        fees=Paise(total_fees),
        tax=Paise(total_tax),
        utr=utr,
        created_at=created_at,
    )
    return settlement, recon_lines, ledger_entries


def generate_clean_batch(rng: random.Random, snapshot_date: date, n_settlements: int = 18) -> CleanBatch:
    """Generate `n_settlements` fully-clean settlements (§3.5's "Fully clean", n=18).

    Every payment is booked as `Dr Razorpay Clearing (net), Dr Payment
    Gateway Charges (fee), Dr GST on Gateway Charges (tax) / Cr Sales
    Revenue (gross)` — the correct accrual-basis entry (§3.0, and the
    "Correct entry" worked example under family 3, §3.2) — so every case
    is `NONE` / `AUTO_MATCHED` by construction: nothing is omitted,
    mis-posted, or mis-timed.
    """
    batch = CleanBatch()
    for _ in range(n_settlements):
        settlement, recon_lines, ledger_entries = _generate_settlement(rng, snapshot_date)
        batch.settlements.append(settlement)
        batch.recon_lines.extend(recon_lines)
        batch.ledger_entries.extend(ledger_entries)

        # Every "Fully clean" settlement lands a matching bank credit — it
        # is not one of the 27 REV-17 no-credit populations.
        add_settlement_credit(batch, rng, settlement=settlement, snapshot_date=snapshot_date)

        batch.ground_truth.append(
            GroundTruthCase(
                case_id=settlement.id,
                expected_outcome_state=OutcomeState.AUTO_MATCHED,
                ground_truth_exception_class=ExceptionClass.NONE,
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
