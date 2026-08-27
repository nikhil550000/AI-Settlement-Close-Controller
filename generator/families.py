"""FR-04 family injections (families 1-5), per spec.md §3.2/§3.3/§3.4/§3.5.

Session 2.1 builds exactly the five FR-04 families at 10 cases each — the
§3.5 case-allocation rows for families 1 through 5, family 4's *core*
variant only. The family-4 date-error variant (precondition: the bank
credit already landed) and the family-4 no-op (lag within the settlement
window) are different populations with different preconditions and
`bank_line` evidence that doesn't exist until later sessions; they belong
to session 2.2 alongside the exception/tax/ambiguous/orphan populations
(§6.3's session table).

Each case is one settlement (`case_id == settlement.id`, "125
settlement-anchored" per §3.5). A case's anomaly — one payment posted via a
non-CLEAN `PostingVariant` (families 1, 3, 4), or one extra refund/adjustment
recon line with no ledger legs (families 2, 5) — sits among otherwise-clean
payments, matching "a settlement case rolls up roughly eleven payments"
(§3.4, Aggregation) and keeping each family's evidence predicate (§3.4)
satisfied by exactly one entity per case, with no cross-family
contamination (generalizing §3.2's family-4 "fee-clean" requirement to
every family).

Labels come from the injection plan, never re-derived from generated
records (§3.5, Label emission): each case builder constructs the
`GroundTruthCase` from the same values used to build the anomalous
record(s), not by inspecting the batch afterward.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from generator.clean import (
    ACCOUNT_BANK_ACCOUNT,
    ACCOUNT_GST_ON_GATEWAY_CHARGES,
    ACCOUNT_PAYMENT_GATEWAY_CHARGES,
    ACCOUNT_RAZORPAY_CLEARING,
    ACCOUNT_SALES_REVENUE,
    PAYMENTS_PER_SETTLEMENT_MAX,
    PAYMENTS_PER_SETTLEMENT_MIN,
    PAYMENTS_PER_SETTLEMENT_MU,
    PAYMENTS_PER_SETTLEMENT_SIGMA,
    PostingVariant,
    _generate_payment,
    _hex_id,
    _payment_amount_paise,
    _snapshot_unix_ts,
    _truncated_lognormal_int,
)
from pipeline.ground_truth import (
    ExceptionClass,
    ExceptionSubtype,
    ExpectedJournalEntry,
    ExpectedJournalLeg,
    GroundTruthCase,
    OutcomeState,
)
from pipeline.money import Paise
from pipeline.schemas import (
    LedgerEntry,
    RazorpayEntityType,
    ReconLine,
    Settlement,
    SettlementStatus,
)

# §3.2's two accounts not already defined in generator/clean.py.
ACCOUNT_SALES_RETURNS_AND_ALLOWANCES = ("4020", "Sales Returns and Allowances")
ACCOUNT_RAZORPAY_SETTLEMENT_ADJUSTMENTS = ("4900", "Razorpay Settlement Adjustments")

N_CASES_PER_FAMILY = 10  # §3.5 case-allocation table: every FR-04 family holds 10 cases.


@dataclass(frozen=True)
class FamilyBatch:
    settlements: list[Settlement] = field(default_factory=list)
    recon_lines: list[ReconLine] = field(default_factory=list)
    ledger_entries: list[LedgerEntry] = field(default_factory=list)
    ground_truth: list[GroundTruthCase] = field(default_factory=list)

    def extend(self, other: "FamilyBatch") -> None:
        self.settlements.extend(other.settlements)
        self.recon_lines.extend(other.recon_lines)
        self.ledger_entries.extend(other.ledger_entries)
        self.ground_truth.extend(other.ground_truth)


def _new_settlement_shell(rng: random.Random, snapshot_ts: int) -> tuple[str, str, int]:
    settlement_created_at = snapshot_ts - rng.randint(1 * 86_400, 10 * 86_400)
    settlement_id = _hex_id(rng, "setl_")
    utr = _hex_id(rng, "UTR", n_bytes=6).upper()
    return settlement_id, utr, settlement_created_at


def _n_payments(rng: random.Random) -> int:
    return _truncated_lognormal_int(
        rng,
        PAYMENTS_PER_SETTLEMENT_MU,
        PAYMENTS_PER_SETTLEMENT_SIGMA,
        PAYMENTS_PER_SETTLEMENT_MIN,
        PAYMENTS_PER_SETTLEMENT_MAX,
    )


def _finalize_settlement(
    settlement_id: str, utr: str, settlement_created_at: int, recon_lines: list[ReconLine]
) -> Settlement:
    """§3.5's hard invariant: `amount == sum(credits) - sum(debits) - fees - tax`.

    Computed purely from `recon_lines` — Razorpay's own evidence — never
    from what the merchant's ledger says, since that's exactly what these
    families get wrong.
    """
    total_credit = sum((line.credit for line in recon_lines), Paise(0))
    total_debit = sum((line.debit for line in recon_lines), Paise(0))
    total_fees = sum((line.fee for line in recon_lines), Paise(0))
    total_tax = sum((line.tax for line in recon_lines), Paise(0))
    return Settlement(
        id=settlement_id,
        amount=Paise(total_credit - total_debit - total_fees - total_tax),
        status=SettlementStatus.PROCESSED,
        fees=Paise(total_fees),
        tax=Paise(total_tax),
        utr=utr,
        created_at=settlement_created_at,
    )


def _account_leg(account: tuple[str, str], debit: Paise, credit: Paise) -> ExpectedJournalLeg:
    return ExpectedJournalLeg(account_code=account[0], account_name=account[1], debit=debit, credit=credit)


# --- Families 1, 3, 4: one payment among the settlement's payments is
# posted via a non-CLEAN `PostingVariant`; the rest are clean. ---


def _generate_misposted_case(
    rng: random.Random,
    snapshot_ts: int,
    *,
    anomaly_posting: PostingVariant,
    exception_subtype: ExceptionSubtype,
    template_id: str,
    correction_legs_fn: Callable[[ReconLine, list[LedgerEntry]], tuple[ExpectedJournalLeg, ...]],
) -> tuple[Settlement, list[ReconLine], list[LedgerEntry], GroundTruthCase]:
    settlement_id, utr, settlement_created_at = _new_settlement_shell(rng, snapshot_ts)
    n_payments = _n_payments(rng)
    anomaly_index = rng.randrange(n_payments)

    recon_lines: list[ReconLine] = []
    ledger_entries: list[LedgerEntry] = []
    anomaly_recon_line: ReconLine | None = None
    anomaly_ledger_entries: list[LedgerEntry] = []
    for i in range(n_payments):
        posting = anomaly_posting if i == anomaly_index else PostingVariant.CLEAN
        recon_line, legs = _generate_payment(rng, settlement_id, utr, settlement_created_at, posting=posting)
        recon_lines.append(recon_line)
        ledger_entries.extend(legs)
        if i == anomaly_index:
            anomaly_recon_line = recon_line
            anomaly_ledger_entries = legs

    assert anomaly_recon_line is not None
    settlement = _finalize_settlement(settlement_id, utr, settlement_created_at, recon_lines)
    correction_legs = correction_legs_fn(anomaly_recon_line, anomaly_ledger_entries)

    ground_truth = GroundTruthCase(
        case_id=settlement.id,
        expected_outcome_state=OutcomeState.AUTO_CLOSED,
        ground_truth_exception_class=ExceptionClass.ACCOUNTING_CORRECTION,
        ground_truth_exception_subtype=exception_subtype,
        expected_linked_source_records=(
            anomaly_recon_line.entity_id,
            settlement.id,
            *(je.journal_entry_id for je in anomaly_ledger_entries),
        ),
        expected_resolution=None,
        expected_journal_entries=(ExpectedJournalEntry(template_id=template_id, legs=correction_legs),),
        expected_template_ids=(template_id,),
        expected_decline_reason=None,
        should_auto_apply=True,
    )
    return settlement, recon_lines, ledger_entries, ground_truth


def _t01_correction_legs(recon_line: ReconLine, _ledger_entries: list[LedgerEntry]) -> tuple[ExpectedJournalLeg, ...]:
    """T-01: `Dr Payment Gateway Charges, Dr GST on Gateway Charges / Cr Razorpay Clearing` (§3.4)."""
    fee, tax = recon_line.fee, recon_line.tax
    legs = [_account_leg(ACCOUNT_PAYMENT_GATEWAY_CHARGES, fee, Paise(0))]
    if tax > 0:  # §3.4 "Zero-amount legs are omitted, not posted."
        legs.append(_account_leg(ACCOUNT_GST_ON_GATEWAY_CHARGES, tax, Paise(0)))
    legs.append(_account_leg(ACCOUNT_RAZORPAY_CLEARING, Paise(0), Paise(fee + tax)))
    return tuple(legs)


def _t03_correction_legs(recon_line: ReconLine, _ledger_entries: list[LedgerEntry]) -> tuple[ExpectedJournalLeg, ...]:
    """T-03: `Dr Payment Gateway Charges, Dr GST on Gateway Charges / Cr Sales Revenue` (§3.4)."""
    fee, tax = recon_line.fee, recon_line.tax
    legs = [_account_leg(ACCOUNT_PAYMENT_GATEWAY_CHARGES, fee, Paise(0))]
    if tax > 0:
        legs.append(_account_leg(ACCOUNT_GST_ON_GATEWAY_CHARGES, tax, Paise(0)))
    legs.append(_account_leg(ACCOUNT_SALES_REVENUE, Paise(0), Paise(fee + tax)))
    return tuple(legs)


def _t04_correction_legs(_recon_line: ReconLine, ledger_entries: list[LedgerEntry]) -> tuple[ExpectedJournalLeg, ...]:
    """T-04: `Dr Razorpay Clearing / Cr Bank Account`, amount = the ledger's premature debit (§3.4)."""
    bank_leg = next(je for je in ledger_entries if je.account_code == ACCOUNT_BANK_ACCOUNT[0])
    net = bank_leg.debit
    return (
        _account_leg(ACCOUNT_RAZORPAY_CLEARING, net, Paise(0)),
        _account_leg(ACCOUNT_BANK_ACCOUNT, Paise(0), net),
    )


def generate_family_1_batch(rng: random.Random, snapshot_date: date, n_cases: int = N_CASES_PER_FAMILY) -> FamilyBatch:
    """Family 1 — unposted MDR fee + GST on fee. `ACCOUNTING_CORRECTION`/`OMISSION`, `AUTO_CLOSED`, `T-01`."""
    snapshot_ts = _snapshot_unix_ts(snapshot_date)
    batch = FamilyBatch()
    for _ in range(n_cases):
        settlement, recon_lines, ledger_entries, gt = _generate_misposted_case(
            rng,
            snapshot_ts,
            anomaly_posting=PostingVariant.UNPOSTED_FEE_GROSS_CLEARING,
            exception_subtype=ExceptionSubtype.OMISSION,
            template_id="T-01",
            correction_legs_fn=_t01_correction_legs,
        )
        batch.settlements.append(settlement)
        batch.recon_lines.extend(recon_lines)
        batch.ledger_entries.extend(ledger_entries)
        batch.ground_truth.append(gt)
    return batch


def generate_family_3_batch(rng: random.Random, snapshot_date: date, n_cases: int = N_CASES_PER_FAMILY) -> FamilyBatch:
    """Family 3 — gross-vs-net posting error. `ACCOUNTING_CORRECTION`/`MISPOSTING`, `AUTO_CLOSED`, `T-03`."""
    snapshot_ts = _snapshot_unix_ts(snapshot_date)
    batch = FamilyBatch()
    for _ in range(n_cases):
        settlement, recon_lines, ledger_entries, gt = _generate_misposted_case(
            rng,
            snapshot_ts,
            anomaly_posting=PostingVariant.NET_REVENUE,
            exception_subtype=ExceptionSubtype.MISPOSTING,
            template_id="T-03",
            correction_legs_fn=_t03_correction_legs,
        )
        batch.settlements.append(settlement)
        batch.recon_lines.extend(recon_lines)
        batch.ledger_entries.extend(ledger_entries)
        batch.ground_truth.append(gt)
    return batch


def generate_family_4_batch(rng: random.Random, snapshot_date: date, n_cases: int = N_CASES_PER_FAMILY) -> FamilyBatch:
    """Family 4 (core variant) — premature bank debit, credit not landed.

    `ACCOUNTING_CORRECTION`/`MISPOSTING`, `AUTO_CLOSED`, `T-04`. The
    precondition "no `bank_line` credit matching the settlement" (§3.2)
    holds vacuously here: session 2.1 generates no `bank_line` records at
    all, so T-04's evidence predicate is satisfied by construction.
    """
    snapshot_ts = _snapshot_unix_ts(snapshot_date)
    batch = FamilyBatch()
    for _ in range(n_cases):
        settlement, recon_lines, ledger_entries, gt = _generate_misposted_case(
            rng,
            snapshot_ts,
            anomaly_posting=PostingVariant.BANK_MISPOST,
            exception_subtype=ExceptionSubtype.MISPOSTING,
            template_id="T-04",
            correction_legs_fn=_t04_correction_legs,
        )
        batch.settlements.append(settlement)
        batch.recon_lines.extend(recon_lines)
        batch.ledger_entries.extend(ledger_entries)
        batch.ground_truth.append(gt)
    return batch


# --- Families 2, 5: an extra recon line (refund / adjustment) with no
# ledger legs at all, on top of an otherwise fully-clean settlement. ---


def _generate_extra_line_case(
    rng: random.Random,
    snapshot_ts: int,
    *,
    build_extra_line: Callable[[random.Random, str, str, int, list[ReconLine]], ReconLine],
    exception_subtype: ExceptionSubtype,
    template_id_fn: Callable[[ReconLine], str],
    correction_legs_fn: Callable[[ReconLine], tuple[ExpectedJournalLeg, ...]],
    linked_records_fn: Callable[[ReconLine], tuple[str, ...]],
) -> tuple[Settlement, list[ReconLine], list[LedgerEntry], GroundTruthCase]:
    settlement_id, utr, settlement_created_at = _new_settlement_shell(rng, snapshot_ts)
    n_payments = _n_payments(rng)

    recon_lines: list[ReconLine] = []
    ledger_entries: list[LedgerEntry] = []
    for _ in range(n_payments):
        recon_line, legs = _generate_payment(rng, settlement_id, utr, settlement_created_at, posting=PostingVariant.CLEAN)
        recon_lines.append(recon_line)
        ledger_entries.extend(legs)

    extra_line = build_extra_line(rng, settlement_id, utr, settlement_created_at, recon_lines)
    recon_lines.append(extra_line)
    # No ledger legs posted for extra_line at all — that omission is the anomaly.

    settlement = _finalize_settlement(settlement_id, utr, settlement_created_at, recon_lines)
    template_id = template_id_fn(extra_line)
    correction_legs = correction_legs_fn(extra_line)

    ground_truth = GroundTruthCase(
        case_id=settlement.id,
        expected_outcome_state=OutcomeState.AUTO_CLOSED,
        ground_truth_exception_class=ExceptionClass.ACCOUNTING_CORRECTION,
        ground_truth_exception_subtype=exception_subtype,
        expected_linked_source_records=(*linked_records_fn(extra_line), settlement.id),
        expected_resolution=None,
        expected_journal_entries=(ExpectedJournalEntry(template_id=template_id, legs=correction_legs),),
        expected_template_ids=(template_id,),
        expected_decline_reason=None,
        should_auto_apply=True,
    )
    return settlement, recon_lines, ledger_entries, ground_truth


def _build_refund_line(
    rng: random.Random, settlement_id: str, utr: str, settlement_created_at: int, recon_lines: list[ReconLine]
) -> ReconLine:
    """§3.2 family 2: a settled refund of a parent payment's full gross amount.

    Fee/tax are zero on the assumption that Razorpay's MDR fee is
    non-refundable (§3.2's family-2 assumption note).
    """
    parent_payment = rng.choice(recon_lines)
    refund_amount = parent_payment.amount
    return ReconLine(
        entity_id=_hex_id(rng, "rfnd_"),
        type=RazorpayEntityType.REFUND,
        debit=refund_amount,
        credit=Paise(0),
        amount=refund_amount,
        fee=Paise(0),
        tax=Paise(0),
        on_hold=False,
        settled=True,
        created_at=settlement_created_at - rng.randint(0, 1 * 86_400),
        settled_at=settlement_created_at,
        settlement_id=settlement_id,
        settlement_utr=utr,
        payment_id=parent_payment.entity_id,  # §3.1: payment_id links a refund to its parent payment.
        order_id=parent_payment.order_id,
        posted_at=None,
        credit_type="default",
        dispute_id=None,
        description=None,
        method=parent_payment.method,
    )


def _t02_correction_legs(refund_line: ReconLine) -> tuple[ExpectedJournalLeg, ...]:
    """T-02: `Dr Sales Returns and Allowances / Cr Razorpay Clearing`, amount = `recon_line.debit` (§3.4)."""
    amount = refund_line.debit
    return (
        _account_leg(ACCOUNT_SALES_RETURNS_AND_ALLOWANCES, amount, Paise(0)),
        _account_leg(ACCOUNT_RAZORPAY_CLEARING, Paise(0), amount),
    )


def generate_family_2_batch(rng: random.Random, snapshot_date: date, n_cases: int = N_CASES_PER_FAMILY) -> FamilyBatch:
    """Family 2 — settled refund absent from the ledger. `ACCOUNTING_CORRECTION`/`OMISSION`, `AUTO_CLOSED`, `T-02`."""
    snapshot_ts = _snapshot_unix_ts(snapshot_date)
    batch = FamilyBatch()
    for _ in range(n_cases):
        settlement, recon_lines, ledger_entries, gt = _generate_extra_line_case(
            rng,
            snapshot_ts,
            build_extra_line=_build_refund_line,
            exception_subtype=ExceptionSubtype.OMISSION,
            template_id_fn=lambda _line: "T-02",
            correction_legs_fn=_t02_correction_legs,
            linked_records_fn=lambda line: (line.entity_id, line.payment_id),
        )
        batch.settlements.append(settlement)
        batch.recon_lines.extend(recon_lines)
        batch.ledger_entries.extend(ledger_entries)
        batch.ground_truth.append(gt)
    return batch


def _build_adjustment_line(
    rng: random.Random, settlement_id: str, utr: str, settlement_created_at: int, recon_lines: list[ReconLine]
) -> ReconLine:
    """§3.2 family 5: a Razorpay-side settlement adjustment, credit or debit, with no merchant-ledger entry.

    Per REV-14, adjustment rows carry null `payment_id` **and** null
    `settlement_utr` even though `settlement_id` is populated — the sample
    adjustment payload has no UTR anchor, so family 5 assembles on
    `settlement_id` alone.

    A debit adjustment is capped at half the settlement's net-of-payments
    total so it can never drive `settlement.amount` negative — that field
    is `NonNegPaise` (session 1.2): every settlement amount is a magnitude,
    and Razorpay would not apply a settlement-side deduction larger than
    the settlement itself.
    """
    is_credit = rng.random() < 0.5
    if is_credit:
        amount = _payment_amount_paise(rng)
    else:
        net_of_payments = (
            sum((line.credit for line in recon_lines), Paise(0))
            - sum((line.debit for line in recon_lines), Paise(0))
            - sum((line.fee for line in recon_lines), Paise(0))
            - sum((line.tax for line in recon_lines), Paise(0))
        )
        max_debit = max(Paise(1), Paise(net_of_payments // 2))
        amount = Paise(min(_payment_amount_paise(rng), max_debit))
    return ReconLine(
        entity_id=_hex_id(rng, "adj_"),
        type=RazorpayEntityType.ADJUSTMENT,
        debit=Paise(0) if is_credit else amount,
        credit=amount if is_credit else Paise(0),
        amount=amount,
        fee=Paise(0),
        tax=Paise(0),
        on_hold=False,
        settled=True,
        created_at=settlement_created_at - rng.randint(0, 1 * 86_400),
        settled_at=settlement_created_at,
        settlement_id=settlement_id,
        settlement_utr=None,
        payment_id=None,
        order_id=None,
        posted_at=None,
        credit_type="default",
        dispute_id=None,
        description=None,
        method=None,
    )


def _t05_t06_correction_legs(adj_line: ReconLine) -> tuple[ExpectedJournalLeg, ...]:
    """T-05 (credit adjustment) / T-06 (debit adjustment), direction from which of debit/credit is non-zero (§3.4)."""
    if adj_line.credit > 0:
        amount = adj_line.credit
        return (
            _account_leg(ACCOUNT_RAZORPAY_CLEARING, amount, Paise(0)),
            _account_leg(ACCOUNT_RAZORPAY_SETTLEMENT_ADJUSTMENTS, Paise(0), amount),
        )
    amount = adj_line.debit
    return (
        _account_leg(ACCOUNT_RAZORPAY_SETTLEMENT_ADJUSTMENTS, amount, Paise(0)),
        _account_leg(ACCOUNT_RAZORPAY_CLEARING, Paise(0), amount),
    )


def generate_family_5_batch(rng: random.Random, snapshot_date: date, n_cases: int = N_CASES_PER_FAMILY) -> FamilyBatch:
    """Family 5 — on-hold release / settlement adjustment unposted. `ACCOUNTING_CORRECTION`/`OMISSION`, `AUTO_CLOSED`, `T-05`/`T-06`."""
    snapshot_ts = _snapshot_unix_ts(snapshot_date)
    batch = FamilyBatch()
    for _ in range(n_cases):
        settlement, recon_lines, ledger_entries, gt = _generate_extra_line_case(
            rng,
            snapshot_ts,
            build_extra_line=_build_adjustment_line,
            exception_subtype=ExceptionSubtype.OMISSION,
            template_id_fn=lambda line: "T-05" if line.credit > 0 else "T-06",
            correction_legs_fn=_t05_t06_correction_legs,
            linked_records_fn=lambda line: (line.entity_id,),
        )
        batch.settlements.append(settlement)
        batch.recon_lines.extend(recon_lines)
        batch.ledger_entries.extend(ledger_entries)
        batch.ground_truth.append(gt)
    return batch


def generate_all_family_batches(
    rng: random.Random, snapshot_date: date, n_cases_per_family: int = N_CASES_PER_FAMILY
) -> FamilyBatch:
    """All five FR-04 families, 10 cases each by default — session 2.1's full checkpoint population."""
    combined = FamilyBatch()
    for generate in (
        generate_family_1_batch,
        generate_family_2_batch,
        generate_family_3_batch,
        generate_family_4_batch,
        generate_family_5_batch,
    ):
        combined.extend(generate(rng, snapshot_date, n_cases_per_family))
    return combined
