"""One-time hand-authoring script for the contested-credit batch, `data/contested/`.

Same status and same rules as `tools/build_adversarial_set.py`: **the JSONL
under `data/contested/` is the committed artifact, this script is the record
of how it was typed.** Every ID, amount, date and narration below is a
literal chosen by hand. It imports nothing from `generator/`, calls no RNG,
and re-running it reproduces the same twelve cases byte-for-byte because
there is nothing random to reproduce. Money is integer paise throughout.

## The finding this batch exists for

FR-09 tier 2 (§4.6) matches a bank credit to a settlement on an exact amount
inside the T+2 window plus one slack day. **That key is not unique to a
settlement.** Two settlements of the same amount created the same day each
see exactly one candidate credit, each match it at tier 2, each report
`residual_paise = 0`, and both reach a terminal state on the strength of one
bank credit that can belong to at most one of them. §4.6's own tie rule ("a
tie is not a match; it routes to ambiguity") was enforced only *within* one
settlement's candidate list — `len(tier2) == 1` — which is the tie a per-case
cascade can see. The symmetric tie *across* settlements needs a batch-wide
pass, and `pipeline.matcher.match_cases` now makes it: every settlement
claiming a contended line falls back to tier 3.

The reference batch never exercised this. `generator/clean.py` draws payment
amounts lognormally, so an exact collision between two settlements in the
same window is vanishingly rare — 150 cases at six seeds produced none. This
batch produces four, by hand.

## Composition: twelve cases, three groups of four

**1. `CONTESTED_UNDECIDABLE` (4 cases = 2 pairs).** Two settlements, identical
`amount`, same `created_at` day, both `processed`, different UTRs; exactly one
bank credit of that amount inside the window, its narration naming the gateway
and carrying no UTR and no discriminator of any kind. Ground truth for both
settlements of a pair is `ABSTAINED` / `AMBIGUOUS_CASE`. That is not a
concession — the evidence genuinely does not say which settlement the credit
belongs to, so abstaining on both is the *correct* answer, and §1.3's
optimization principle ranks a false match strictly worse than a deferral.

**2. `CONTESTED_DECIDABLE` (4 cases = 2 pairs).** The same construction, except
the bank narration carries a plain-English discriminator a human reads
instantly and no existing rule can use: the settlement's payment-method
character. One settlement of each pair is wholly UPI (every `recon_line.method`
is `upi`), the other wholly CARD, and the credit narrates
`... UPI COLLECTIONS AUG`. Ground truth says the UPI settlement owns the
credit (`AUTO_MATCHED`, with the credit cited in
`expected_linked_source_records`); the CARD settlement of the pair is
`BANK_CREDIT_OVERDUE` / `EXTERNAL_ACTION_REQUIRED`, because its credit
genuinely has not arrived. **The deterministic keyword arm cannot resolve
these and is not expected to** — tier 2 cannot read "UPI COLLECTIONS", so both
claimants are demoted and both land in tier 3. Ground truth records what the
right answer *is*, not what this pipeline reaches; that gap is the measurement.

**3. `NOT_CONTESTED` control (4 cases).** Four ordinary settlements with four
different amounts, each with its own clean credit carrying its UTR as a
whitespace-delimited word, so each matches at tier 0. Ground truth
`AUTO_MATCHED`. These exist because a "fix" that made every settlement abstain
would also show a `false_match_rate` of zero: the controls are what separates
a contention fix from a blanket refusal to match.

## Why every case's window has elapsed, and why the books are clean

Both are load-bearing, and neither is incidental.

The terminal state comes from `pipeline.apply.assign_state`, which reads the
*books-versus-evidence* residual (`pipeline.reconciliation`), not the matcher's
bank-versus-settlement one. If the merchant's books were incomplete, a
contested case would abstain because of the books and the match tier would
never enter the outcome. So every settlement here is booked accrual-correct —
`Dr Razorpay Clearing (net) / Dr Payment Gateway Charges (fee) / Dr GST (tax)
/ Cr Sales Revenue (gross)`, §3.2's own posting — and its residual is 0. The
only thing left that can move the outcome is the match.

Every contested settlement is then created early enough that its T+2 window
has elapsed by the 2026-08-28 snapshot. That is what makes the demotion
visible: at tier 3 with an elapsed window, §3.3's `BANK_CREDIT_OVERDUE`
trigger fires and the case terminates in `EXTERNAL_ACTION_REQUIRED`. Under the
*old* per-case-only tie rule the same case matched at tier 2, fired no
trigger, and — with a zero books residual — reached `AUTO_MATCHED`. Four
guaranteed false matches, on two credits. Were the window still open instead,
a clean-booked settlement would read as §3.3's `EXPECTED_TIMING_DIFFERENCE`
and `AUTO_MATCHED` would be *correct* on both sides of the fix, and this batch
would measure nothing.

## Money

Every fee is exactly 2% of its gross and every tax exactly 18% of its fee,
picked so both land on an exact paise integer with no rounding — the same
convention `tools/build_adversarial_set.py` uses, so `decimal.Decimal` is not
needed anywhere below. Net (`gross - fee - tax`) is therefore a fixed function
of gross, which is why the four contested pairs are given four *different*
gross totals: two pairs sharing a net would contest each other's credits too,
collapsing the cross-settlement tie into the within-case tie the cascade
already handled.

## Dates

`SNAPSHOT_DATE` is 2026-08-28 (a Friday), matching `data/reference/`,
`data/adversarial/` and `data/heldout_vocab/`. Window arithmetic is read from
`pipeline.timing` — the module the matcher itself uses — and asserted below,
rather than recomputed by hand.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from pipeline.accounts import (
    ACCOUNT_GST_ON_GATEWAY_CHARGES,
    ACCOUNT_PAYMENT_GATEWAY_CHARGES,
    ACCOUNT_RAZORPAY_CLEARING,
    ACCOUNT_SALES_REVENUE,
)
from pipeline.ground_truth import (
    ExceptionClass,
    ExceptionSubtype,
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
from pipeline.timing import is_within_settlement_window, settlement_window_deadline

OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "contested"

SNAPSHOT_DATE = date(2026, 8, 28)

settlements: list[Settlement] = []
recon_lines: list[ReconLine] = []
ledger_entries: list[LedgerEntry] = []
bank_lines: list[BankLine] = []
ground_truth: list[GroundTruthCase] = []


def _ts(d: date, seconds_into_day: int = 9 * 3600) -> int:
    """A UTC unix timestamp for `d`, at a fixed (arbitrary, hand-picked) time of day."""
    return int(datetime.combine(d, time.min, tzinfo=timezone.utc).timestamp()) + seconds_into_day


def _payment(
    *,
    entity_id: str,
    settlement_id: str,
    utr: str,
    created_at: int,
    gross: int,
    fee: int,
    tax: int,
    method: str,
) -> ReconLine:
    """One settled `payment` recon line. `method` is the group-2 discriminator."""
    return ReconLine(
        entity_id=entity_id,
        type=RazorpayEntityType.PAYMENT,
        debit=Paise(0),
        credit=Paise(gross),
        amount=Paise(gross),
        fee=Paise(fee),
        tax=Paise(tax),
        on_hold=False,
        settled=True,
        created_at=created_at,
        settled_at=created_at,
        settlement_id=settlement_id,
        settlement_utr=utr,
        payment_id=None,
        order_id=f"order_{entity_id}",
        posted_at=None,
        credit_type="default",
        dispute_id=None,
        description=None,
        method=method,
    )


def _clean_books(entity_id: str, book_date: date, *, gross: int, fee: int, tax: int, method: str) -> list[LedgerEntry]:
    """§3.2's accrual-correct posting for one settled payment, in integer paise.

    Four legs, balanced (`net + fee + tax == gross`), and deliberately *correct*:
    the merchant's bookkeeping is not what is in question in this batch, and a
    case whose books are wrong would abstain on the books rather than on the
    match. Posting `Payment Gateway Charges` is also what keeps `T-01` from
    firing, and crediting `Sales Revenue` at gross rather than net is what keeps
    `T-03` from firing — so no §3.4 template addresses these cases and the
    outcome turns on the match alone.
    """
    narration = f"ERP import - Razorpay {method.upper()} collection"
    return [
        LedgerEntry(
            journal_entry_id=f"je_{entity_id}_clearing",
            date=book_date,
            account_code=ACCOUNT_RAZORPAY_CLEARING.code,
            account_name=ACCOUNT_RAZORPAY_CLEARING.name,
            debit=Paise(gross - fee - tax),
            credit=Paise(0),
            reference=entity_id,
            narration=narration,
            source=LedgerSource.ERP_IMPORT,
        ),
        LedgerEntry(
            journal_entry_id=f"je_{entity_id}_pgc",
            date=book_date,
            account_code=ACCOUNT_PAYMENT_GATEWAY_CHARGES.code,
            account_name=ACCOUNT_PAYMENT_GATEWAY_CHARGES.name,
            debit=Paise(fee),
            credit=Paise(0),
            reference=entity_id,
            narration=narration,
            source=LedgerSource.ERP_IMPORT,
        ),
        LedgerEntry(
            journal_entry_id=f"je_{entity_id}_gst",
            date=book_date,
            account_code=ACCOUNT_GST_ON_GATEWAY_CHARGES.code,
            account_name=ACCOUNT_GST_ON_GATEWAY_CHARGES.name,
            debit=Paise(tax),
            credit=Paise(0),
            reference=entity_id,
            narration=narration,
            source=LedgerSource.ERP_IMPORT,
        ),
        LedgerEntry(
            journal_entry_id=f"je_{entity_id}_revenue",
            date=book_date,
            account_code=ACCOUNT_SALES_REVENUE.code,
            account_name=ACCOUNT_SALES_REVENUE.name,
            debit=Paise(0),
            credit=Paise(gross),
            reference=entity_id,
            narration=narration,
            source=LedgerSource.ERP_IMPORT,
        ),
    ]


def _add_settlement(
    *,
    settlement_id: str,
    utr: str,
    created: date,
    lines: tuple[tuple[str, int, int, int], ...],
    method: str,
) -> int:
    """Append one settlement, its recon lines and its (clean) books; return its net amount.

    `lines` is `(entity_id suffix, gross, fee, tax)` per recon line. The
    settlement header's `amount`/`fees`/`tax` are the sums over those lines,
    which is §3.5's own invariant (`amount == sum(credits) - sum(debits) - fees
    - tax`) and therefore keeps §3.3's `SETTLEMENT_AMOUNT_MISMATCH` trigger
    silent — this batch is about matching, and an amount-mismatch trigger would
    route these cases for an unrelated reason.
    """
    created_at = _ts(created)
    gross_total = sum(gross for _, gross, _, _ in lines)
    fee_total = sum(fee for _, _, fee, _ in lines)
    tax_total = sum(tax for _, _, _, tax in lines)
    net = gross_total - fee_total - tax_total

    settlements.append(
        Settlement(
            id=settlement_id,
            amount=Paise(net),
            status=SettlementStatus.PROCESSED,
            fees=Paise(fee_total),
            tax=Paise(tax_total),
            utr=utr,
            created_at=created_at,
        )
    )
    for suffix, gross, fee, tax in lines:
        entity_id = f"con_pay_{suffix}"
        recon_lines.append(
            _payment(
                entity_id=entity_id,
                settlement_id=settlement_id,
                utr=utr,
                created_at=created_at,
                gross=gross,
                fee=fee,
                tax=tax,
                method=method,
            )
        )
        ledger_entries.extend(_clean_books(entity_id, created, gross=gross, fee=fee, tax=tax, method=method))
    return net


def _credit(*, line_id: str, value_date: date, narration: str, deposit: int, profile: BankProfile) -> BankLine:
    bank_lines.append(
        BankLine(
            line_id=line_id,
            value_date=value_date,
            narration=narration,
            bank_ref_no=None,
            withdrawal_paise=Paise(0),
            deposit_paise=Paise(deposit),
            closing_balance_paise=Paise(10_00_000_00),
            bank_profile=profile,
        )
    )
    return bank_lines[-1]


def _entity_ids(settlement_id: str) -> tuple[str, ...]:
    return tuple(line.entity_id for line in recon_lines if line.settlement_id == settlement_id)


# =====================================================================
# Group 1 — CONTESTED_UNDECIDABLE. Two pairs; one credit each; no
# discriminator anywhere in the evidence. Correct answer: abstain on both.
# =====================================================================

_UNDEC_A_DATE = date(2026, 8, 17)  # Monday; window deadline 2026-08-19.
_UNDEC_B_DATE = date(2026, 8, 18)  # Tuesday; window deadline 2026-08-20.

_undec_a1 = _add_settlement(
    settlement_id="con_setl_undec_a1",
    utr="CONUTR000000A101",
    created=_UNDEC_A_DATE,
    lines=(("undec_a1", 250_000, 5_000, 900),),
    method="card",
)
_undec_a2 = _add_settlement(
    settlement_id="con_setl_undec_a2",
    utr="CONUTR000000A102",
    created=_UNDEC_A_DATE,
    lines=(("undec_a2", 250_000, 5_000, 900),),
    method="card",
)
assert _undec_a1 == _undec_a2 == 244_100, "pair A's two settlements must be identical in amount"

_credit(
    line_id="con_bank_undec_a",
    value_date=date(2026, 8, 18),
    narration="NEFT CR RAZORPAY SOFTWARE PVT LTD",
    deposit=_undec_a1,
    profile=BankProfile.HDFC,
)

_undec_b1 = _add_settlement(
    settlement_id="con_setl_undec_b1",
    utr="CONUTR000000B101",
    created=_UNDEC_B_DATE,
    lines=(("undec_b1", 300_000, 6_000, 1_080),),
    method="card",
)
_undec_b2 = _add_settlement(
    settlement_id="con_setl_undec_b2",
    utr="CONUTR000000B102",
    created=_UNDEC_B_DATE,
    lines=(("undec_b2", 300_000, 6_000, 1_080),),
    method="card",
)
assert _undec_b1 == _undec_b2 == 292_920, "pair B's two settlements must be identical in amount"

_credit(
    line_id="con_bank_undec_b",
    value_date=date(2026, 8, 19),
    narration="RTGS CR RAZORPAY SOFTWARE PVT LTD",
    deposit=_undec_b1,
    profile=BankProfile.ICICI,
)

for _case_id, _pair_credit in (
    ("con_setl_undec_a1", "con_bank_undec_a"),
    ("con_setl_undec_a2", "con_bank_undec_a"),
    ("con_setl_undec_b1", "con_bank_undec_b"),
    ("con_setl_undec_b2", "con_bank_undec_b"),
):
    ground_truth.append(
        GroundTruthCase(
            case_id=_case_id,
            expected_outcome_state=OutcomeState.ABSTAINED,
            ground_truth_exception_class=ExceptionClass.AMBIGUOUS_CASE,
            ground_truth_exception_subtype=ExceptionSubtype.NONE,
            expected_linked_source_records=(_case_id, *_entity_ids(_case_id), _pair_credit),
            expected_resolution=(
                f"Bank credit {_pair_credit} matches {_case_id} and one other settlement of the "
                "same amount in the same window equally well, and its narration carries no UTR "
                "and no other discriminator — the evidence cannot say which settlement it "
                "belongs to, so neither may claim it."
            ),
            expected_journal_entries=(),
            expected_template_ids=(),
            expected_decline_reason=None,
            should_auto_apply=False,
        )
    )


# =====================================================================
# Group 2 — CONTESTED_DECIDABLE. Same shape, but the narration says
# "UPI COLLECTIONS" and exactly one settlement of each pair is wholly UPI.
# A human reads it instantly; no rule in this pipeline can.
# =====================================================================

_DEC_C_DATE = date(2026, 8, 19)  # Wednesday; window deadline 2026-08-21.
_DEC_D_DATE = date(2026, 8, 20)  # Thursday; window deadline 2026-08-24 (weekend skipped).

_DEC_C_LINES = (("dec_c_1", 200_000, 4_000, 720), ("dec_c_2", 150_000, 3_000, 540))
_DEC_D_LINES = (("dec_d_1", 180_000, 3_600, 648), ("dec_d_2", 220_000, 4_400, 792))


def _suffixed(lines: tuple[tuple[str, int, int, int], ...], tag: str) -> tuple[tuple[str, int, int, int], ...]:
    return tuple((f"{suffix}_{tag}", gross, fee, tax) for suffix, gross, fee, tax in lines)


_dec_c_upi = _add_settlement(
    settlement_id="con_setl_dec_c_upi",
    utr="CONUTR000000C101",
    created=_DEC_C_DATE,
    lines=_suffixed(_DEC_C_LINES, "upi"),
    method="upi",
)
_dec_c_card = _add_settlement(
    settlement_id="con_setl_dec_c_card",
    utr="CONUTR000000C102",
    created=_DEC_C_DATE,
    lines=_suffixed(_DEC_C_LINES, "card"),
    method="card",
)
assert _dec_c_upi == _dec_c_card == 341_740, "pair C's two settlements must be identical in amount"

_credit(
    line_id="con_bank_dec_c",
    value_date=date(2026, 8, 20),
    narration="NEFT CR RAZORPAY SOFTWARE PVT LTD UPI COLLECTIONS AUG",
    deposit=_dec_c_upi,
    profile=BankProfile.HDFC,
)

_dec_d_upi = _add_settlement(
    settlement_id="con_setl_dec_d_upi",
    utr="CONUTR000000D101",
    created=_DEC_D_DATE,
    lines=_suffixed(_DEC_D_LINES, "upi"),
    method="upi",
)
_dec_d_card = _add_settlement(
    settlement_id="con_setl_dec_d_card",
    utr="CONUTR000000D102",
    created=_DEC_D_DATE,
    lines=_suffixed(_DEC_D_LINES, "card"),
    method="card",
)
assert _dec_d_upi == _dec_d_card == 390_560, "pair D's two settlements must be identical in amount"

_credit(
    line_id="con_bank_dec_d",
    value_date=date(2026, 8, 21),
    narration="RTGS CR RAZORPAY SOFTWARE PVT LTD UPI COLLECTIONS AUG",
    deposit=_dec_d_upi,
    profile=BankProfile.AXIS,
)

for _upi_case, _card_case, _pair_credit in (
    ("con_setl_dec_c_upi", "con_setl_dec_c_card", "con_bank_dec_c"),
    ("con_setl_dec_d_upi", "con_setl_dec_d_card", "con_bank_dec_d"),
):
    # The UPI settlement is the true owner: its credit did arrive, so its books
    # and the bank agree and nothing needs posting.
    ground_truth.append(
        GroundTruthCase(
            case_id=_upi_case,
            expected_outcome_state=OutcomeState.AUTO_MATCHED,
            ground_truth_exception_class=ExceptionClass.NONE,
            ground_truth_exception_subtype=ExceptionSubtype.NONE,
            expected_linked_source_records=(_upi_case, *_entity_ids(_upi_case), _pair_credit),
            expected_resolution=None,
            expected_journal_entries=(),
            expected_template_ids=(),
            expected_decline_reason=None,
            should_auto_apply=False,
        )
    )
    # The CARD settlement of the pair is genuinely unpaid: its window has
    # elapsed and the only candidate credit belongs to its UPI twin.
    ground_truth.append(
        GroundTruthCase(
            case_id=_card_case,
            expected_outcome_state=OutcomeState.EXTERNAL_ACTION_REQUIRED,
            ground_truth_exception_class=ExceptionClass.OPERATIONAL_EXCEPTION,
            ground_truth_exception_subtype=ExceptionSubtype.BANK_CREDIT_OVERDUE,
            expected_linked_source_records=(_card_case, *_entity_ids(_card_case)),
            expected_resolution=(
                f"The one candidate credit of this amount ({_pair_credit}) narrates UPI "
                f"collections and belongs to {_upi_case}; {_card_case} is wholly card and its "
                "settlement window has elapsed with no credit of its own — chase the gateway."
            ),
            expected_journal_entries=(),
            expected_template_ids=(),
            expected_decline_reason=None,
            should_auto_apply=False,
        )
    )


# =====================================================================
# Group 3 — NOT_CONTESTED control. Four ordinary settlements, four
# different amounts, each with its own UTR-bearing credit. Tier 0.
# =====================================================================

_CONTROLS = (
    ("con_setl_ctrl_1", "CONUTR000000E101", date(2026, 8, 24), ("ctrl_1", 500_000, 10_000, 1_800), "card"),
    ("con_setl_ctrl_2", "CONUTR000000E102", date(2026, 8, 25), ("ctrl_2", 550_000, 11_000, 1_980), "upi"),
    ("con_setl_ctrl_3", "CONUTR000000E103", date(2026, 8, 26), ("ctrl_3", 600_000, 12_000, 2_160), "netbanking"),
    ("con_setl_ctrl_4", "CONUTR000000E104", date(2026, 8, 27), ("ctrl_4", 650_000, 13_000, 2_340), "card"),
)
_CONTROL_PROFILES = (BankProfile.HDFC, BankProfile.ICICI, BankProfile.AXIS, BankProfile.HDFC)

for _index, (_setl_id, _utr, _created, _line, _method) in enumerate(_CONTROLS):
    _net = _add_settlement(
        settlement_id=_setl_id,
        utr=_utr,
        created=_created,
        lines=(_line,),
        method=_method,
    )
    _line_id = f"con_bank_{_line[0]}"
    # The UTR is its own whitespace-delimited word, which is §4.6 tier 0's
    # `CLEAN` shape exactly — `pipeline.matcher` splits the narration on
    # whitespace and requires one whole word to normalize to the UTR.
    _credit(
        line_id=_line_id,
        value_date=_created + timedelta(days=1),
        narration=f"NEFT CR RAZORPAY SOFTWARE PVT LTD {_utr}",
        deposit=_net,
        profile=_CONTROL_PROFILES[_index],
    )
    ground_truth.append(
        GroundTruthCase(
            case_id=_setl_id,
            expected_outcome_state=OutcomeState.AUTO_MATCHED,
            ground_truth_exception_class=ExceptionClass.NONE,
            ground_truth_exception_subtype=ExceptionSubtype.NONE,
            expected_linked_source_records=(_setl_id, *_entity_ids(_setl_id), _line_id),
            expected_resolution=None,
            expected_journal_entries=(),
            expected_template_ids=(),
            expected_decline_reason=None,
            should_auto_apply=False,
        )
    )


# --- Invariants the batch would be meaningless without, asserted here. ---

_CONTESTED_IDS = frozenset(
    {
        "con_setl_undec_a1",
        "con_setl_undec_a2",
        "con_setl_undec_b1",
        "con_setl_undec_b2",
        "con_setl_dec_c_upi",
        "con_setl_dec_c_card",
        "con_setl_dec_d_upi",
        "con_setl_dec_d_card",
    }
)

_created_dates = {
    settlement.id: datetime.fromtimestamp(settlement.created_at, tz=timezone.utc).date()
    for settlement in settlements
}

for _settlement in settlements:
    if _settlement.id in _CONTESTED_IDS:
        # The demotion has to be *visible*: at tier 3 inside the window a clean
        # case reads as EXPECTED_TIMING_DIFFERENCE / AUTO_MATCHED and the fix
        # changes nothing. Past it, BANK_CREDIT_OVERDUE fires and it does.
        assert not is_within_settlement_window(_created_dates[_settlement.id], SNAPSHOT_DATE), (
            f"{_settlement.id}'s window must have elapsed by the snapshot; "
            f"deadline is {settlement_window_deadline(_created_dates[_settlement.id])}"
        )

_amount_counts: dict[int, int] = {}
for _settlement in settlements:
    _amount_counts[int(_settlement.amount)] = _amount_counts.get(int(_settlement.amount), 0) + 1
assert sorted(_amount_counts.values()) == [1, 1, 1, 1, 2, 2, 2, 2], (
    "exactly the four contested pairs may share an amount; a third settlement on a "
    "contested amount would turn the cross-settlement tie into the within-case tie "
    "the cascade already handled"
)

assert len(settlements) == 12 and len(ground_truth) == 12
assert len(bank_lines) == 8  # 4 contested credits + 4 control credits
assert len({line.line_id for line in bank_lines}) == 8
assert len({entry.journal_entry_id for entry in ledger_entries}) == len(ledger_entries)


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
