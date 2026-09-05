"""One-time hand-authoring script for the adversarial set.

**This script is gitignored; its output under `data/adversarial/` is the
committed artifact.** Following the version-control protocol, "the
hand-authored adversarial set ... is source, not output; nothing
regenerates it" — the ten cases below are literal, hand-picked values
(amounts, dates, narrations), not draws from any distribution, and this
script exists only so those literals are typed once as real Pydantic
records (with real validation) instead of as raw, easy-to-typo JSON. It
imports nothing from `generator/` and calls no RNG: every ID, amount, and
date is a constant chosen by hand and written directly into the source
below. Re-running this script reproduces the same ten cases byte-for-byte
(there is nothing random to reproduce), and it is checked in as a record
of how the ten cases were built, not as a thing meant to be re-run to
"regenerate" the data — the JSONL files it once produced are what is
committed and what `tests/test_adversarial.py` reads.

## The four boundaries, and which case(s) cover each

- **`T-01` versus `T-03`.** Cases `adv_setl_t01` / `adv_setl_t03`
  share an identical amount/fee/tax (gross ₹1,000, fee ₹20, tax ₹3.60) so
  that gross (₹1,000.00) and net (₹976.40) are the only thing separating
  which template's evidence predicate should fire — the same numbers,
  booked two different wrong ways.
- **Family 4 core versus its date-error variant.** `adv_setl_f4core` (no
  bank credit, window elapsed — stresses the T-04-outranks-
  `BANK_CREDIT_OVERDUE` precedence in `pipeline.apply.assign_state`) versus
  `adv_setl_f4date` (correct accounts and amount, wrong posting date,
  credit landed — a policy exclusion). `adv_setl_f4noop` is the
  third leg of the same timing triangle: a lag still inside the T+2
  window, which must read as `AUTO_MATCHED`, not as either of the other two.
- **Duplicate credit versus reversal.** `adv_case_dupcredit` (two
  bank lines, identical narration/amount/date) versus `adv_case_reversal`
  (a reversal-shaped debit with no matching prior credit *anywhere in this
  batch*). A third pair — `adv_bank_noise_credit` / `adv_bank_noise_reversal`
  — shares one reference token between a credit and its own reversal, which
  case assembly must recognise as a wash and raise as *no case at all*;
  it therefore carries no `GroundTruthCase` by design (see
  `tests/test_adversarial.py`'s noise-pair assertion).
- **At least one genuinely unresolvable case.** `adv_case_ambiguous` (an
  opaque-narration inbound credit — no counterparty, no Razorpay anchor,
  nothing else in the batch explains it). `adv_case_unmatched_credit` sits
  beside it as the sibling boundary: the same shape, except
  the narration *does* name a counterparty.

`adv_setl_clean` is a fully-clean control case (`AUTO_MATCHED` / `NONE`) —
not one of the four named boundaries, but included so the adversarial set
is not exclusively traps.

## Money

Every fee is exactly 2% of its gross amount and every tax exactly 18% of
its fee, chosen so both percentages land on an exact paise integer with no
rounding — e.g. gross 100000 paise x 2% = 2000 paise fee, x 18% = 360 paise
tax — so `decimal.Decimal`/`ROUND_HALF_UP` (the generator's own rounding
tool, to keep money in integer paise) is not needed here at all: every figure
below is already exact integer paise, picked by hand.

## Dates

`SNAPSHOT_DATE` (2026-08-28, a Friday) matches every other session's
constant. Settlement-window arithmetic (T+2 working days, weekends
excluded) is read from `pipeline.timing` — the same module the matcher
itself uses — rather than recomputed by hand, so "window elapsed" and
"still in window" are guaranteed to mean what the pipeline thinks they mean:

- `adv_setl_f4noop` is created ON the snapshot date: `is_within_settlement_window`
  is trivially true (nothing has had time to elapse yet).
- `adv_setl_f4core` is created 2026-08-17 (a Monday): `settlement_window_deadline`
  lands on 2026-08-19, well before the 2026-08-28 snapshot, so the window
  has elapsed and `BANK_CREDIT_OVERDUE` is a live (but out-ranked) trigger.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from pipeline.accounts import (
    ACCOUNT_BANK_ACCOUNT,
    ACCOUNT_GST_ON_GATEWAY_CHARGES,
    ACCOUNT_PAYMENT_GATEWAY_CHARGES,
    ACCOUNT_RAZORPAY_CLEARING,
    ACCOUNT_SALES_REVENUE,
)
from pipeline.ground_truth import (
    DeclineReason,
    ExceptionClass,
    ExceptionSubtype,
    ExpectedJournalEntry,
    ExpectedJournalLeg,
    GroundTruthCase,
    OutcomeState,
)
from pipeline.money import Paise
from pipeline.schemas import (
    BankLine,
    BankProfile,
    LedgerEntry,
    LedgerSource,
    RazorpayEntityType,
    ReconLine,
    Settlement,
    SettlementStatus,
)
from pipeline.timing import settlement_window_deadline

OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "adversarial"

SNAPSHOT_DATE = date(2026, 8, 28)


def _ts(d: date, seconds_into_day: int = 9 * 3600) -> int:
    """A UTC unix timestamp for `d`, at a fixed (arbitrary, hand-picked) time of day."""
    return int(datetime.combine(d, time.min, tzinfo=timezone.utc).timestamp()) + seconds_into_day


def _leg(account: tuple[str, str], debit: int, credit: int) -> ExpectedJournalLeg:
    return ExpectedJournalLeg(account_code=account[0], account_name=account[1], debit=Paise(debit), credit=Paise(credit))


settlements: list[Settlement] = []
recon_lines: list[ReconLine] = []
ledger_entries: list[LedgerEntry] = []
bank_lines: list[BankLine] = []
ground_truth: list[GroundTruthCase] = []


def _payment_recon_line(
    *,
    entity_id: str,
    settlement_id: str,
    utr: str,
    created_at: int,
    amount: int,
    fee: int,
    tax: int,
    order_id: str,
) -> ReconLine:
    return ReconLine(
        entity_id=entity_id,
        type=RazorpayEntityType.PAYMENT,
        debit=Paise(0),
        credit=Paise(amount),
        amount=Paise(amount),
        fee=Paise(fee),
        tax=Paise(tax),
        on_hold=False,
        settled=True,
        created_at=created_at,
        settled_at=created_at,
        settlement_id=settlement_id,
        settlement_utr=utr,
        payment_id=None,
        order_id=order_id,
        posted_at=None,
        credit_type="default",
        dispute_id=None,
        description=None,
        method="card",
    )


def _settlement(*, settlement_id: str, utr: str, created_at: int, amount: int, fee: int, tax: int) -> Settlement:
    return Settlement(
        id=settlement_id,
        amount=Paise(amount),
        status=SettlementStatus.PROCESSED,
        fees=Paise(fee),
        tax=Paise(tax),
        utr=utr,
        created_at=created_at,
    )


def _bank_credit(
    *, line_id: str, value_date: date, utr: str, deposit: int, profile: BankProfile = BankProfile.HDFC
) -> BankLine:
    return BankLine(
        line_id=line_id,
        value_date=value_date,
        narration=f"NEFT CR RAZORPAY SOFTWARE PVT LTD {utr}",
        bank_ref_no=None,
        withdrawal_paise=Paise(0),
        deposit_paise=Paise(deposit),
        closing_balance_paise=Paise(10_00_000_00),
        bank_profile=profile,
    )


# --- Case 1: T-01 — fee/GST never posted, gross wrongly booked as revenue. ---

_T01_UTR = "ADVUTR0000000101"
_T01_SETL_DATE = date(2026, 8, 24)
_T01_CREATED_AT = _ts(_T01_SETL_DATE)
_T01_AMOUNT, _T01_FEE, _T01_TAX = 100_000, 2_000, 360
_T01_NET = _T01_AMOUNT - _T01_FEE - _T01_TAX  # 97640

settlements.append(
    _settlement(
        settlement_id="adv_setl_t01",
        utr=_T01_UTR,
        created_at=_T01_CREATED_AT,
        amount=_T01_AMOUNT - _T01_FEE - _T01_TAX,
        fee=_T01_FEE,
        tax=_T01_TAX,
    )
)
recon_lines.append(
    _payment_recon_line(
        entity_id="adv_pay_t01",
        settlement_id="adv_setl_t01",
        utr=_T01_UTR,
        created_at=_T01_CREATED_AT,
        amount=_T01_AMOUNT,
        fee=_T01_FEE,
        tax=_T01_TAX,
        order_id="adv_order_t01",
    )
)
ledger_entries += [
    LedgerEntry(
        journal_entry_id="adv_je_t01_clearing",
        date=_T01_SETL_DATE,
        account_code=ACCOUNT_RAZORPAY_CLEARING.code,
        account_name=ACCOUNT_RAZORPAY_CLEARING.name,
        debit=Paise(_T01_AMOUNT),
        credit=Paise(0),
        reference="adv_pay_t01",
        narration="ERP import - Razorpay card txn",
        source=LedgerSource.ERP_IMPORT,
    ),
    LedgerEntry(
        journal_entry_id="adv_je_t01_revenue",
        date=_T01_SETL_DATE,
        account_code=ACCOUNT_SALES_REVENUE.code,
        account_name=ACCOUNT_SALES_REVENUE.name,
        debit=Paise(0),
        credit=Paise(_T01_AMOUNT),
        reference="adv_pay_t01",
        narration="ERP import - Razorpay card txn",
        source=LedgerSource.ERP_IMPORT,
    ),
]
bank_lines.append(
    _bank_credit(line_id="adv_bank_t01", value_date=_T01_SETL_DATE + timedelta(days=1), utr=_T01_UTR, deposit=_T01_NET)
)
ground_truth.append(
    GroundTruthCase(
        case_id="adv_setl_t01",
        expected_outcome_state=OutcomeState.AUTO_CLOSED,
        ground_truth_exception_class=ExceptionClass.ACCOUNTING_CORRECTION,
        ground_truth_exception_subtype=ExceptionSubtype.OMISSION,
        expected_linked_source_records=("adv_pay_t01", "adv_setl_t01", "adv_je_t01_clearing", "adv_je_t01_revenue"),
        expected_resolution=None,
        expected_journal_entries=(
            ExpectedJournalEntry(
                template_id="T-01",
                legs=(
                    _leg(ACCOUNT_PAYMENT_GATEWAY_CHARGES, _T01_FEE, 0),
                    _leg(ACCOUNT_GST_ON_GATEWAY_CHARGES, _T01_TAX, 0),
                    _leg(ACCOUNT_RAZORPAY_CLEARING, 0, _T01_FEE + _T01_TAX),
                ),
            ),
        ),
        expected_template_ids=("T-01",),
        expected_decline_reason=None,
        should_auto_apply=True,
    )
)

# --- Case 2: T-03 — the *net* wrongly booked as revenue. Same amount/fee/tax as case 1. ---

_T03_UTR = "ADVUTR0000000203"
_T03_SETL_DATE = date(2026, 8, 24)
_T03_CREATED_AT = _ts(_T03_SETL_DATE)
_T03_AMOUNT, _T03_FEE, _T03_TAX = 100_000, 2_000, 360
_T03_NET = _T03_AMOUNT - _T03_FEE - _T03_TAX  # 97640

settlements.append(
    _settlement(
        settlement_id="adv_setl_t03",
        utr=_T03_UTR,
        created_at=_T03_CREATED_AT,
        amount=_T03_NET,
        fee=_T03_FEE,
        tax=_T03_TAX,
    )
)
recon_lines.append(
    _payment_recon_line(
        entity_id="adv_pay_t03",
        settlement_id="adv_setl_t03",
        utr=_T03_UTR,
        created_at=_T03_CREATED_AT,
        amount=_T03_AMOUNT,
        fee=_T03_FEE,
        tax=_T03_TAX,
        order_id="adv_order_t03",
    )
)
ledger_entries += [
    LedgerEntry(
        journal_entry_id="adv_je_t03_clearing",
        date=_T03_SETL_DATE,
        account_code=ACCOUNT_RAZORPAY_CLEARING.code,
        account_name=ACCOUNT_RAZORPAY_CLEARING.name,
        debit=Paise(_T03_NET),
        credit=Paise(0),
        reference="adv_pay_t03",
        narration="Razorpay card collection",
        source=LedgerSource.ERP_IMPORT,
    ),
    LedgerEntry(
        journal_entry_id="adv_je_t03_revenue",
        date=_T03_SETL_DATE,
        account_code=ACCOUNT_SALES_REVENUE.code,
        account_name=ACCOUNT_SALES_REVENUE.name,
        debit=Paise(0),
        credit=Paise(_T03_NET),
        reference="adv_pay_t03",
        narration="Razorpay card collection",
        source=LedgerSource.ERP_IMPORT,
    ),
]
bank_lines.append(
    _bank_credit(line_id="adv_bank_t03", value_date=_T03_SETL_DATE + timedelta(days=1), utr=_T03_UTR, deposit=_T03_NET)
)
ground_truth.append(
    GroundTruthCase(
        case_id="adv_setl_t03",
        expected_outcome_state=OutcomeState.AUTO_CLOSED,
        ground_truth_exception_class=ExceptionClass.ACCOUNTING_CORRECTION,
        ground_truth_exception_subtype=ExceptionSubtype.MISPOSTING,
        expected_linked_source_records=("adv_pay_t03", "adv_setl_t03", "adv_je_t03_clearing", "adv_je_t03_revenue"),
        expected_resolution=None,
        expected_journal_entries=(
            ExpectedJournalEntry(
                template_id="T-03",
                legs=(
                    _leg(ACCOUNT_PAYMENT_GATEWAY_CHARGES, _T03_FEE, 0),
                    _leg(ACCOUNT_GST_ON_GATEWAY_CHARGES, _T03_TAX, 0),
                    _leg(ACCOUNT_SALES_REVENUE, 0, _T03_FEE + _T03_TAX),
                ),
            ),
        ),
        expected_template_ids=("T-03",),
        expected_decline_reason=None,
        should_auto_apply=True,
    )
)

# --- Case 3: family 4 core — premature Bank Account debit, no credit landed, window elapsed. ---

_F4C_UTR = "ADVUTR0000000304"
_F4C_SETL_DATE = date(2026, 8, 17)  # Monday; settlement_window_deadline lands 2026-08-19.
_F4C_CREATED_AT = _ts(_F4C_SETL_DATE)
_F4C_AMOUNT, _F4C_FEE, _F4C_TAX = 50_000, 1_000, 180
_F4C_NET = _F4C_AMOUNT - _F4C_FEE - _F4C_TAX  # 48820

assert settlement_window_deadline(_F4C_SETL_DATE) < SNAPSHOT_DATE, "case 3 requires the window to have elapsed"

settlements.append(
    _settlement(
        settlement_id="adv_setl_f4core",
        utr=_F4C_UTR,
        created_at=_F4C_CREATED_AT,
        amount=_F4C_NET,
        fee=_F4C_FEE,
        tax=_F4C_TAX,
    )
)
recon_lines.append(
    _payment_recon_line(
        entity_id="adv_pay_f4core",
        settlement_id="adv_setl_f4core",
        utr=_F4C_UTR,
        created_at=_F4C_CREATED_AT,
        amount=_F4C_AMOUNT,
        fee=_F4C_FEE,
        tax=_F4C_TAX,
        order_id="adv_order_f4core",
    )
)
ledger_entries += [
    LedgerEntry(
        journal_entry_id="adv_je_f4core_bank",
        date=_F4C_SETL_DATE,
        account_code=ACCOUNT_BANK_ACCOUNT.code,
        account_name=ACCOUNT_BANK_ACCOUNT.name,
        debit=Paise(_F4C_NET),
        credit=Paise(0),
        reference="adv_pay_f4core",
        narration="Gateway posting - CARD",
        source=LedgerSource.ERP_IMPORT,
    ),
    LedgerEntry(
        journal_entry_id="adv_je_f4core_pgc",
        date=_F4C_SETL_DATE,
        account_code=ACCOUNT_PAYMENT_GATEWAY_CHARGES.code,
        account_name=ACCOUNT_PAYMENT_GATEWAY_CHARGES.name,
        debit=Paise(_F4C_FEE),
        credit=Paise(0),
        reference="adv_pay_f4core",
        narration="Gateway posting - CARD",
        source=LedgerSource.ERP_IMPORT,
    ),
    LedgerEntry(
        journal_entry_id="adv_je_f4core_gst",
        date=_F4C_SETL_DATE,
        account_code=ACCOUNT_GST_ON_GATEWAY_CHARGES.code,
        account_name=ACCOUNT_GST_ON_GATEWAY_CHARGES.name,
        debit=Paise(_F4C_TAX),
        credit=Paise(0),
        reference="adv_pay_f4core",
        narration="Gateway posting - CARD",
        source=LedgerSource.ERP_IMPORT,
    ),
    LedgerEntry(
        journal_entry_id="adv_je_f4core_revenue",
        date=_F4C_SETL_DATE,
        account_code=ACCOUNT_SALES_REVENUE.code,
        account_name=ACCOUNT_SALES_REVENUE.name,
        debit=Paise(0),
        credit=Paise(_F4C_AMOUNT),
        reference="adv_pay_f4core",
        narration="Gateway posting - CARD",
        source=LedgerSource.ERP_IMPORT,
    ),
]
# No bank_line: family 4 core's hard precondition is that
# no bank credit matching the settlement exists yet.
ground_truth.append(
    GroundTruthCase(
        case_id="adv_setl_f4core",
        expected_outcome_state=OutcomeState.AUTO_CLOSED,
        ground_truth_exception_class=ExceptionClass.ACCOUNTING_CORRECTION,
        ground_truth_exception_subtype=ExceptionSubtype.MISPOSTING,
        expected_linked_source_records=(
            "adv_pay_f4core",
            "adv_setl_f4core",
            "adv_je_f4core_bank",
            "adv_je_f4core_pgc",
            "adv_je_f4core_gst",
            "adv_je_f4core_revenue",
        ),
        expected_resolution=None,
        expected_journal_entries=(
            ExpectedJournalEntry(
                template_id="T-04",
                legs=(
                    _leg(ACCOUNT_RAZORPAY_CLEARING, _F4C_NET, 0),
                    _leg(ACCOUNT_BANK_ACCOUNT, 0, _F4C_NET),
                ),
            ),
        ),
        expected_template_ids=("T-04",),
        expected_decline_reason=None,
        should_auto_apply=True,
    )
)

# --- Case 4: family 4 date-error variant — correct accounts/amount, wrong period, credit landed. ---

_F4D_UTR = "ADVUTR0000000405"
_F4D_SETL_DATE = date(2026, 8, 20)
_F4D_CREATED_AT = _ts(_F4D_SETL_DATE)
_F4D_AMOUNT, _F4D_FEE, _F4D_TAX = 60_000, 1_200, 216
_F4D_NET = _F4D_AMOUNT - _F4D_FEE - _F4D_TAX  # 58584
_F4D_LEDGER_DATE = _F4D_SETL_DATE - timedelta(days=32)  # 2026-07-19: a different calendar month.

settlements.append(
    _settlement(
        settlement_id="adv_setl_f4date",
        utr=_F4D_UTR,
        created_at=_F4D_CREATED_AT,
        amount=_F4D_NET,
        fee=_F4D_FEE,
        tax=_F4D_TAX,
    )
)
recon_lines.append(
    _payment_recon_line(
        entity_id="adv_pay_f4date",
        settlement_id="adv_setl_f4date",
        utr=_F4D_UTR,
        created_at=_F4D_CREATED_AT,
        amount=_F4D_AMOUNT,
        fee=_F4D_FEE,
        tax=_F4D_TAX,
        order_id="adv_order_f4date",
    )
)
ledger_entries += [
    LedgerEntry(
        journal_entry_id="adv_je_f4date_clearing",
        date=_F4D_LEDGER_DATE,
        account_code=ACCOUNT_RAZORPAY_CLEARING.code,
        account_name=ACCOUNT_RAZORPAY_CLEARING.name,
        debit=Paise(_F4D_NET),
        credit=Paise(0),
        reference="adv_pay_f4date",
        narration="Razorpay settlement posting (card)",
        source=LedgerSource.ERP_IMPORT,
    ),
    LedgerEntry(
        journal_entry_id="adv_je_f4date_pgc",
        date=_F4D_LEDGER_DATE,
        account_code=ACCOUNT_PAYMENT_GATEWAY_CHARGES.code,
        account_name=ACCOUNT_PAYMENT_GATEWAY_CHARGES.name,
        debit=Paise(_F4D_FEE),
        credit=Paise(0),
        reference="adv_pay_f4date",
        narration="Razorpay settlement posting (card)",
        source=LedgerSource.ERP_IMPORT,
    ),
    LedgerEntry(
        journal_entry_id="adv_je_f4date_gst",
        date=_F4D_LEDGER_DATE,
        account_code=ACCOUNT_GST_ON_GATEWAY_CHARGES.code,
        account_name=ACCOUNT_GST_ON_GATEWAY_CHARGES.name,
        debit=Paise(_F4D_TAX),
        credit=Paise(0),
        reference="adv_pay_f4date",
        narration="Razorpay settlement posting (card)",
        source=LedgerSource.ERP_IMPORT,
    ),
    LedgerEntry(
        journal_entry_id="adv_je_f4date_revenue",
        date=_F4D_LEDGER_DATE,
        account_code=ACCOUNT_SALES_REVENUE.code,
        account_name=ACCOUNT_SALES_REVENUE.name,
        debit=Paise(0),
        credit=Paise(_F4D_AMOUNT),
        reference="adv_pay_f4date",
        narration="Razorpay settlement posting (card)",
        source=LedgerSource.ERP_IMPORT,
    ),
]
bank_lines.append(
    _bank_credit(line_id="adv_bank_f4date", value_date=_F4D_SETL_DATE + timedelta(days=1), utr=_F4D_UTR, deposit=_F4D_NET)
)
ground_truth.append(
    GroundTruthCase(
        case_id="adv_setl_f4date",
        expected_outcome_state=OutcomeState.REVIEW_REQUIRED,
        ground_truth_exception_class=ExceptionClass.ACCOUNTING_CORRECTION,
        ground_truth_exception_subtype=ExceptionSubtype.MISPOSTING,
        expected_linked_source_records=(
            "adv_pay_f4date",
            "adv_setl_f4date",
            "adv_je_f4date_clearing",
            "adv_je_f4date_pgc",
            "adv_je_f4date_gst",
            "adv_je_f4date_revenue",
        ),
        expected_resolution=None,
        expected_journal_entries=(),  # no delta entry exists to post.
        expected_template_ids=(),
        expected_decline_reason=DeclineReason.POLICY,
        should_auto_apply=False,
    )
)

# --- Case 5: family 4 no-op — clean books, credit lag still inside the T+2 window. ---

_F4N_UTR = "ADVUTR0000000506"
_F4N_SETL_DATE = SNAPSHOT_DATE  # created on the snapshot date itself: trivially inside the window.
_F4N_CREATED_AT = _ts(_F4N_SETL_DATE)
_F4N_AMOUNT, _F4N_FEE, _F4N_TAX = 20_000, 400, 72
_F4N_NET = _F4N_AMOUNT - _F4N_FEE - _F4N_TAX  # 19528

settlements.append(
    _settlement(
        settlement_id="adv_setl_f4noop",
        utr=_F4N_UTR,
        created_at=_F4N_CREATED_AT,
        amount=_F4N_NET,
        fee=_F4N_FEE,
        tax=_F4N_TAX,
    )
)
recon_lines.append(
    _payment_recon_line(
        entity_id="adv_pay_f4noop",
        settlement_id="adv_setl_f4noop",
        utr=_F4N_UTR,
        created_at=_F4N_CREATED_AT,
        amount=_F4N_AMOUNT,
        fee=_F4N_FEE,
        tax=_F4N_TAX,
        order_id="adv_order_f4noop",
    )
)
ledger_entries += [
    LedgerEntry(
        journal_entry_id="adv_je_f4noop_clearing",
        date=_F4N_SETL_DATE,
        account_code=ACCOUNT_RAZORPAY_CLEARING.code,
        account_name=ACCOUNT_RAZORPAY_CLEARING.name,
        debit=Paise(_F4N_NET),
        credit=Paise(0),
        reference="adv_pay_f4noop",
        narration="ERP import - Razorpay card",
        source=LedgerSource.ERP_IMPORT,
    ),
    LedgerEntry(
        journal_entry_id="adv_je_f4noop_pgc",
        date=_F4N_SETL_DATE,
        account_code=ACCOUNT_PAYMENT_GATEWAY_CHARGES.code,
        account_name=ACCOUNT_PAYMENT_GATEWAY_CHARGES.name,
        debit=Paise(_F4N_FEE),
        credit=Paise(0),
        reference="adv_pay_f4noop",
        narration="ERP import - Razorpay card",
        source=LedgerSource.ERP_IMPORT,
    ),
    LedgerEntry(
        journal_entry_id="adv_je_f4noop_gst",
        date=_F4N_SETL_DATE,
        account_code=ACCOUNT_GST_ON_GATEWAY_CHARGES.code,
        account_name=ACCOUNT_GST_ON_GATEWAY_CHARGES.name,
        debit=Paise(_F4N_TAX),
        credit=Paise(0),
        reference="adv_pay_f4noop",
        narration="ERP import - Razorpay card",
        source=LedgerSource.ERP_IMPORT,
    ),
    LedgerEntry(
        journal_entry_id="adv_je_f4noop_revenue",
        date=_F4N_SETL_DATE,
        account_code=ACCOUNT_SALES_REVENUE.code,
        account_name=ACCOUNT_SALES_REVENUE.name,
        debit=Paise(0),
        credit=Paise(_F4N_AMOUNT),
        reference="adv_pay_f4noop",
        narration="ERP import - Razorpay card",
        source=LedgerSource.ERP_IMPORT,
    ),
]
# No bank_line: still inside the T+2 window as of the snapshot.
ground_truth.append(
    GroundTruthCase(
        case_id="adv_setl_f4noop",
        expected_outcome_state=OutcomeState.AUTO_MATCHED,
        ground_truth_exception_class=ExceptionClass.EXPECTED_TIMING_DIFFERENCE,
        ground_truth_exception_subtype=ExceptionSubtype.NONE,
        expected_linked_source_records=("adv_setl_f4noop", "adv_pay_f4noop"),
        expected_resolution=None,
        expected_journal_entries=(),
        expected_template_ids=(),
        expected_decline_reason=None,
        should_auto_apply=False,
    )
)

# --- Case 10 (numbered last; ordering here is authorship order, not a case index): ---
# a fully-clean control settlement. AUTO_MATCHED / NONE.

_CLEAN_UTR = "ADVUTR0000001010"
_CLEAN_SETL_DATE = date(2026, 8, 22)
_CLEAN_CREATED_AT = _ts(_CLEAN_SETL_DATE)
_CLEAN_AMOUNT, _CLEAN_FEE, _CLEAN_TAX = 40_000, 800, 144
_CLEAN_NET = _CLEAN_AMOUNT - _CLEAN_FEE - _CLEAN_TAX  # 39056

settlements.append(
    _settlement(
        settlement_id="adv_setl_clean",
        utr=_CLEAN_UTR,
        created_at=_CLEAN_CREATED_AT,
        amount=_CLEAN_NET,
        fee=_CLEAN_FEE,
        tax=_CLEAN_TAX,
    )
)
recon_lines.append(
    _payment_recon_line(
        entity_id="adv_pay_clean",
        settlement_id="adv_setl_clean",
        utr=_CLEAN_UTR,
        created_at=_CLEAN_CREATED_AT,
        amount=_CLEAN_AMOUNT,
        fee=_CLEAN_FEE,
        tax=_CLEAN_TAX,
        order_id="adv_order_clean",
    )
)
ledger_entries += [
    LedgerEntry(
        journal_entry_id="adv_je_clean_clearing",
        date=_CLEAN_SETL_DATE,
        account_code=ACCOUNT_RAZORPAY_CLEARING.code,
        account_name=ACCOUNT_RAZORPAY_CLEARING.name,
        debit=Paise(_CLEAN_NET),
        credit=Paise(0),
        reference="adv_pay_clean",
        narration="Razorpay card txn",
        source=LedgerSource.ERP_IMPORT,
    ),
    LedgerEntry(
        journal_entry_id="adv_je_clean_pgc",
        date=_CLEAN_SETL_DATE,
        account_code=ACCOUNT_PAYMENT_GATEWAY_CHARGES.code,
        account_name=ACCOUNT_PAYMENT_GATEWAY_CHARGES.name,
        debit=Paise(_CLEAN_FEE),
        credit=Paise(0),
        reference="adv_pay_clean",
        narration="Razorpay card txn",
        source=LedgerSource.ERP_IMPORT,
    ),
    LedgerEntry(
        journal_entry_id="adv_je_clean_gst",
        date=_CLEAN_SETL_DATE,
        account_code=ACCOUNT_GST_ON_GATEWAY_CHARGES.code,
        account_name=ACCOUNT_GST_ON_GATEWAY_CHARGES.name,
        debit=Paise(_CLEAN_TAX),
        credit=Paise(0),
        reference="adv_pay_clean",
        narration="Razorpay card txn",
        source=LedgerSource.ERP_IMPORT,
    ),
    LedgerEntry(
        journal_entry_id="adv_je_clean_revenue",
        date=_CLEAN_SETL_DATE,
        account_code=ACCOUNT_SALES_REVENUE.code,
        account_name=ACCOUNT_SALES_REVENUE.name,
        debit=Paise(0),
        credit=Paise(_CLEAN_AMOUNT),
        reference="adv_pay_clean",
        narration="Razorpay card txn",
        source=LedgerSource.ERP_IMPORT,
    ),
]
bank_lines.append(
    _bank_credit(
        line_id="adv_bank_clean", value_date=_CLEAN_SETL_DATE + timedelta(days=1), utr=_CLEAN_UTR, deposit=_CLEAN_NET
    )
)
ground_truth.append(
    GroundTruthCase(
        case_id="adv_setl_clean",
        expected_outcome_state=OutcomeState.AUTO_MATCHED,
        ground_truth_exception_class=ExceptionClass.NONE,
        ground_truth_exception_subtype=ExceptionSubtype.NONE,
        expected_linked_source_records=("adv_setl_clean", "adv_pay_clean"),
        expected_resolution=None,
        expected_journal_entries=(),
        expected_template_ids=(),
        expected_decline_reason=None,
        should_auto_apply=False,
    )
)

# --- Orphan cases (bank lines only; no settlement, recon line, or ledger entry). ---

_ORPHAN_DATE = date(2026, 8, 26)

# Case 6: duplicate credit — two bank lines, identical narration/amount/date.
_dup_narration = "NEFT CR SHARMA ENTERPRISES REF ADVDUP1234567"
bank_lines += [
    BankLine(
        line_id="adv_bank_dup_1",
        value_date=_ORPHAN_DATE,
        narration=_dup_narration,
        bank_ref_no=None,
        withdrawal_paise=Paise(0),
        deposit_paise=Paise(75_000),
        closing_balance_paise=Paise(10_00_000_00),
        bank_profile=BankProfile.ICICI,
    ),
    BankLine(
        line_id="adv_bank_dup_2",
        value_date=_ORPHAN_DATE,
        narration=_dup_narration,
        bank_ref_no=None,
        withdrawal_paise=Paise(0),
        deposit_paise=Paise(75_000),
        closing_balance_paise=Paise(10_00_000_00),
        bank_profile=BankProfile.ICICI,
    ),
]
ground_truth.append(
    GroundTruthCase(
        case_id="adv_case_dupcredit",
        expected_outcome_state=OutcomeState.EXTERNAL_ACTION_REQUIRED,
        ground_truth_exception_class=ExceptionClass.OPERATIONAL_EXCEPTION,
        ground_truth_exception_subtype=ExceptionSubtype.DUPLICATE_CREDIT,
        expected_linked_source_records=("adv_bank_dup_1", "adv_bank_dup_2"),
        expected_resolution=(
            "Two bank credits share reference ADVDUP1234567 for the same amount — confirm "
            "which (if either) is a duplicate before reconciling."
        ),
        expected_journal_entries=(),
        expected_template_ids=(),
        expected_decline_reason=None,
        should_auto_apply=False,
    )
)

# Case 7: reversal, unmatched — no matching prior credit anywhere in this batch.
bank_lines.append(
    BankLine(
        line_id="adv_bank_rev1",
        value_date=_ORPHAN_DATE,
        narration="NEFT REVERSAL ADVREV7654321 BLUE OCEAN TRADERS",
        bank_ref_no=None,
        withdrawal_paise=Paise(40_000),
        deposit_paise=Paise(0),
        closing_balance_paise=Paise(10_00_000_00),
        bank_profile=BankProfile.AXIS,
    )
)
ground_truth.append(
    GroundTruthCase(
        case_id="adv_case_reversal",
        expected_outcome_state=OutcomeState.EXTERNAL_ACTION_REQUIRED,
        ground_truth_exception_class=ExceptionClass.OPERATIONAL_EXCEPTION,
        ground_truth_exception_subtype=ExceptionSubtype.REVERSAL_UNMATCHED,
        expected_linked_source_records=("adv_bank_rev1",),
        expected_resolution="Bank reversal adv_bank_rev1 has no matching prior credit in the batch — confirm origin before actioning.",
        expected_journal_entries=(),
        expected_template_ids=(),
        expected_decline_reason=None,
        should_auto_apply=False,
    )
)

# Case 8: ambiguous — opaque narration, no counterparty. The "genuinely unresolvable" case.
bank_lines.append(
    BankLine(
        line_id="adv_bank_ambig",
        value_date=_ORPHAN_DATE,
        narration="MISC CREDIT",
        bank_ref_no=None,
        withdrawal_paise=Paise(0),
        deposit_paise=Paise(30_000),
        closing_balance_paise=Paise(10_00_000_00),
        bank_profile=BankProfile.HDFC,
    )
)
ground_truth.append(
    GroundTruthCase(
        case_id="adv_case_ambiguous",
        expected_outcome_state=OutcomeState.ABSTAINED,
        ground_truth_exception_class=ExceptionClass.AMBIGUOUS_CASE,
        ground_truth_exception_subtype=ExceptionSubtype.NONE,
        expected_linked_source_records=("adv_bank_ambig",),
        expected_resolution=(
            "Inbound credit adv_bank_ambig carries no identifiable counterparty in its "
            "narration — insufficient evidence to classify."
        ),
        expected_journal_entries=(),
        expected_template_ids=(),
        expected_decline_reason=None,
        should_auto_apply=False,
    )
)

# Case 9: unmatched inbound credit — same shape as case 8, but the narration names a counterparty.
bank_lines.append(
    BankLine(
        line_id="adv_bank_uic",
        value_date=_ORPHAN_DATE,
        narration="NEFT CR RAVI KUMAR ADVUIC9988776",
        bank_ref_no=None,
        withdrawal_paise=Paise(0),
        deposit_paise=Paise(45_000),
        closing_balance_paise=Paise(10_00_000_00),
        bank_profile=BankProfile.HDFC,
    )
)
ground_truth.append(
    GroundTruthCase(
        case_id="adv_case_unmatched_credit",
        expected_outcome_state=OutcomeState.EXTERNAL_ACTION_REQUIRED,
        ground_truth_exception_class=ExceptionClass.OPERATIONAL_EXCEPTION,
        ground_truth_exception_subtype=ExceptionSubtype.UNMATCHED_INBOUND_CREDIT,
        expected_linked_source_records=("adv_bank_uic",),
        expected_resolution=(
            "Inbound credit adv_bank_uic names counterparty 'RAVI KUMAR' with no "
            "corresponding Razorpay settlement in the batch — confirm origin."
        ),
        expected_journal_entries=(),
        expected_template_ids=(),
        expected_decline_reason=None,
        should_auto_apply=False,
    )
)

# --- Bonus: a self-matching reversal pair. Not a case at all (case assembly must
# recognise the wash) — carries no GroundTruthCase; see tests/test_adversarial.py. ---

bank_lines += [
    BankLine(
        line_id="adv_bank_noise_credit",
        value_date=date(2026, 8, 25),
        narration="NEFT CR NATIONAL HARDWARE CO ADVNOISE998877",
        bank_ref_no=None,
        withdrawal_paise=Paise(0),
        deposit_paise=Paise(15_000),
        closing_balance_paise=Paise(10_00_000_00),
        bank_profile=BankProfile.AXIS,
    ),
    BankLine(
        line_id="adv_bank_noise_reversal",
        value_date=date(2026, 8, 26),
        narration="REVERSAL ADVNOISE998877 NATIONAL HARDWARE CO",
        bank_ref_no=None,
        withdrawal_paise=Paise(15_000),
        deposit_paise=Paise(0),
        closing_balance_paise=Paise(10_00_000_00),
        bank_profile=BankProfile.AXIS,
    ),
]


def _write_jsonl(path: Path, records: list) -> None:
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(record.model_dump_json())
            f.write("\n")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    _write_jsonl(OUT_DIR / "settlements.jsonl", settlements)
    _write_jsonl(OUT_DIR / "recon_lines.jsonl", recon_lines)
    _write_jsonl(OUT_DIR / "ledger_entries.jsonl", ledger_entries)
    _write_jsonl(OUT_DIR / "bank_lines.jsonl", bank_lines)
    _write_jsonl(OUT_DIR / "ground_truth.jsonl", ground_truth)
    print(
        f"settlements={len(settlements)} recon_lines={len(recon_lines)} "
        f"ledger_entries={len(ledger_entries)} bank_lines={len(bank_lines)} "
        f"ground_truth_cases={len(ground_truth)} -> {OUT_DIR}"
    )


if __name__ == "__main__":
    main()
