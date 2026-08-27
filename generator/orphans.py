"""Orphan-case generation and non-settlement noise, per spec.md §3.6 and
the REV-17/REV-18-corrected bank-line decomposition.

25 non-settlement-anchored cases (§3.6): `UNMATCHED_INBOUND_CREDIT` (8),
an opaque-narration `AMBIGUOUS_CASE` (8), `REVERSAL_UNMATCHED` (6), and
`DUPLICATE_CREDIT` (3 cases spanning 6 bank lines — REV-18's granularity
correction: "a duplicate credit and the original credit carrying the same
UTR form a single case") — 28 bank lines total, matching REV-17's ~28
orphan-case-line figure exactly.

Plus ~50 non-settlement-anchored **noise** lines (bank charges, unrelated
NEFT, self-matching reversal pairs) that carry no case at all — §3.6:
"Bank charges stay noise, not cases," and the matcher must learn to
correctly ignore them rather than raise a case.

None of these are settlement-anchored: `case_id` is minted directly
(`orphan_*`), and `expected_linked_source_records` cites `bank_line.line_id`
values only — there is no settlement, recon_line, or ledger_entry to link.
`generator/families.py`'s `FamilyBatch` is reused as the return shape
(`settlements`/`recon_lines`/`ledger_entries` simply stay empty) rather
than introducing a second near-identical container.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from generator.bank_lines import bank_line_id, random_bank_profile, random_closing_balance
from generator.clean import _hex_id, _payment_amount_paise
from generator.families import FamilyBatch
from pipeline.ground_truth import ExceptionClass, ExceptionSubtype, GroundTruthCase, OutcomeState
from pipeline.money import Paise
from pipeline.schemas import BankLine

N_UNMATCHED_INBOUND_CREDIT = 8
N_AMBIGUOUS_ORPHAN = 8
N_REVERSAL_UNMATCHED = 6
N_DUPLICATE_CREDIT_CASES = 3  # spans 6 bank lines, REV-18

N_NOISE_BANK_CHARGES = 20
N_NOISE_UNRELATED_NEFT = 18
N_NOISE_REVERSAL_PAIRS = 6  # 12 lines: a credit and its own matching reversal — a wash, correctly ignorable

_NAMED_COUNTERPARTIES = (
    "SHARMA ENTERPRISES",
    "BLUE OCEAN TRADERS",
    "APEX LOGISTICS PVT LTD",
    "RAVI KUMAR",
    "GREENFIELD EXPORTS",
    "SUNRISE TEXTILES",
    "NATIONAL HARDWARE CO",
    "PRIYA MENON",
)

_OPAQUE_NARRATIONS = ("NEFT CR", "MISC CREDIT", "FUNDS TRANSFER", "TRANSFER IN", "BY TRANSFER", "CREDIT-MISC")

_BANK_CHARGE_NARRATIONS = (
    "SMS ALERT CHARGES",
    "AMC FEE",
    "CHEQUE BOOK CHARGES",
    "ATM AMC CHARGES",
    "DEBIT CARD ANNUAL FEE",
)


def _orphan_case_id(rng: random.Random) -> str:
    return _hex_id(rng, "orphan_")


def _reference_token(rng: random.Random) -> str:
    return _hex_id(rng, "REF", n_bytes=6).upper()


def _random_value_date(rng: random.Random, snapshot_date: date, max_days_back: int = 10) -> date:
    return snapshot_date - timedelta(days=rng.randint(0, max_days_back))


def _new_bank_line(
    rng: random.Random,
    *,
    value_date: date,
    narration: str,
    withdrawal_paise: Paise,
    deposit_paise: Paise,
) -> BankLine:
    return BankLine(
        line_id=bank_line_id(rng),
        value_date=value_date,
        narration=narration,
        bank_ref_no=None,
        withdrawal_paise=withdrawal_paise,
        deposit_paise=deposit_paise,
        closing_balance_paise=random_closing_balance(rng),
        bank_profile=random_bank_profile(rng),
    )


def generate_unmatched_inbound_credit_batch(
    rng: random.Random, snapshot_date: date, n_cases: int = N_UNMATCHED_INBOUND_CREDIT
) -> FamilyBatch:
    """`OPERATIONAL_EXCEPTION`/`UNMATCHED_INBOUND_CREDIT`: inbound NEFT, counterparty named in narration."""
    batch = FamilyBatch()
    for _ in range(n_cases):
        counterparty = rng.choice(_NAMED_COUNTERPARTIES)
        line = _new_bank_line(
            rng,
            value_date=_random_value_date(rng, snapshot_date),
            narration=f"NEFT CR-{counterparty}-{_reference_token(rng)}",
            withdrawal_paise=Paise(0),
            deposit_paise=_payment_amount_paise(rng),
        )
        batch.bank_lines.append(line)
        batch.ground_truth.append(
            GroundTruthCase(
                case_id=_orphan_case_id(rng),
                expected_outcome_state=OutcomeState.EXTERNAL_ACTION_REQUIRED,
                ground_truth_exception_class=ExceptionClass.OPERATIONAL_EXCEPTION,
                ground_truth_exception_subtype=ExceptionSubtype.UNMATCHED_INBOUND_CREDIT,
                expected_linked_source_records=(line.line_id,),
                expected_resolution=(
                    f"Inbound credit {line.line_id} names counterparty '{counterparty}' with no "
                    "corresponding Razorpay settlement in the batch — confirm origin."
                ),
                expected_journal_entries=(),
                expected_template_ids=(),
                expected_decline_reason=None,
                should_auto_apply=False,
            )
        )
    return batch


def generate_ambiguous_orphan_batch(
    rng: random.Random, snapshot_date: date, n_cases: int = N_AMBIGUOUS_ORPHAN
) -> FamilyBatch:
    """`AMBIGUOUS_CASE`: inbound credit, opaque narration — no identifiable counterparty."""
    batch = FamilyBatch()
    for _ in range(n_cases):
        line = _new_bank_line(
            rng,
            value_date=_random_value_date(rng, snapshot_date),
            narration=rng.choice(_OPAQUE_NARRATIONS),
            withdrawal_paise=Paise(0),
            deposit_paise=_payment_amount_paise(rng),
        )
        batch.bank_lines.append(line)
        batch.ground_truth.append(
            GroundTruthCase(
                case_id=_orphan_case_id(rng),
                expected_outcome_state=OutcomeState.ABSTAINED,
                ground_truth_exception_class=ExceptionClass.AMBIGUOUS_CASE,
                ground_truth_exception_subtype=ExceptionSubtype.NONE,
                expected_linked_source_records=(line.line_id,),
                expected_resolution=(
                    f"Inbound credit {line.line_id} carries no identifiable counterparty in its "
                    "narration — insufficient evidence to classify."
                ),
                expected_journal_entries=(),
                expected_template_ids=(),
                expected_decline_reason=None,
                should_auto_apply=False,
            )
        )
    return batch


def generate_reversal_unmatched_batch(
    rng: random.Random, snapshot_date: date, n_cases: int = N_REVERSAL_UNMATCHED
) -> FamilyBatch:
    """`OPERATIONAL_EXCEPTION`/`REVERSAL_UNMATCHED`: a bank reversal with no matching prior credit in the batch."""
    batch = FamilyBatch()
    for _ in range(n_cases):
        phantom_utr = _hex_id(rng, "UTR", n_bytes=6).upper()
        line = _new_bank_line(
            rng,
            value_date=_random_value_date(rng, snapshot_date),
            narration=f"REVERSAL-{phantom_utr}",
            withdrawal_paise=_payment_amount_paise(rng),
            deposit_paise=Paise(0),
        )
        batch.bank_lines.append(line)
        batch.ground_truth.append(
            GroundTruthCase(
                case_id=_orphan_case_id(rng),
                expected_outcome_state=OutcomeState.EXTERNAL_ACTION_REQUIRED,
                ground_truth_exception_class=ExceptionClass.OPERATIONAL_EXCEPTION,
                ground_truth_exception_subtype=ExceptionSubtype.REVERSAL_UNMATCHED,
                expected_linked_source_records=(line.line_id,),
                expected_resolution=(
                    f"Bank reversal {line.line_id} has no matching prior credit in the batch — "
                    "confirm origin before actioning."
                ),
                expected_journal_entries=(),
                expected_template_ids=(),
                expected_decline_reason=None,
                should_auto_apply=False,
            )
        )
    return batch


def generate_duplicate_credit_batch(
    rng: random.Random, snapshot_date: date, n_cases: int = N_DUPLICATE_CREDIT_CASES
) -> FamilyBatch:
    """`OPERATIONAL_EXCEPTION`/`DUPLICATE_CREDIT`: same UTR credited twice — one case spans two bank lines (REV-18)."""
    batch = FamilyBatch()
    for _ in range(n_cases):
        utr = _hex_id(rng, "UTR", n_bytes=6).upper()
        amount = _payment_amount_paise(rng)
        value_date = _random_value_date(rng, snapshot_date)
        narration = f"NEFT CR-RAZORPAY SOFTWARE PVT LTD-{utr}"
        line_1 = _new_bank_line(rng, value_date=value_date, narration=narration, withdrawal_paise=Paise(0), deposit_paise=amount)
        line_2 = _new_bank_line(rng, value_date=value_date, narration=narration, withdrawal_paise=Paise(0), deposit_paise=amount)
        batch.bank_lines.extend((line_1, line_2))
        batch.ground_truth.append(
            GroundTruthCase(
                case_id=_orphan_case_id(rng),
                expected_outcome_state=OutcomeState.EXTERNAL_ACTION_REQUIRED,
                ground_truth_exception_class=ExceptionClass.OPERATIONAL_EXCEPTION,
                ground_truth_exception_subtype=ExceptionSubtype.DUPLICATE_CREDIT,
                expected_linked_source_records=(line_1.line_id, line_2.line_id),
                expected_resolution=(
                    f"Two bank credits share UTR {utr} for the same amount — confirm which (if either) "
                    "is a duplicate before reconciling."
                ),
                expected_journal_entries=(),
                expected_template_ids=(),
                expected_decline_reason=None,
                should_auto_apply=False,
            )
        )
    return batch


def generate_all_orphan_batches(rng: random.Random, snapshot_date: date) -> FamilyBatch:
    """All four §3.6 orphan populations, 25 cases / 28 bank lines combined."""
    combined = FamilyBatch()
    for generate in (
        generate_unmatched_inbound_credit_batch,
        generate_ambiguous_orphan_batch,
        generate_reversal_unmatched_batch,
        generate_duplicate_credit_batch,
    ):
        combined.extend(generate(rng, snapshot_date))
    return combined


def generate_noise_bank_lines(rng: random.Random, snapshot_date: date) -> list[BankLine]:
    """~50 non-settlement-anchored bank lines the matcher must ignore, not close as cases (§2.2, §3.6).

    No `GroundTruthCase` is emitted for any of these — that absence *is*
    the label: a correct matcher run produces zero cases referencing them.
    """
    lines: list[BankLine] = []

    for _ in range(N_NOISE_BANK_CHARGES):
        lines.append(
            _new_bank_line(
                rng,
                value_date=_random_value_date(rng, snapshot_date, max_days_back=20),
                narration=rng.choice(_BANK_CHARGE_NARRATIONS),
                withdrawal_paise=Paise(rng.randint(50, 500) * 100),
                deposit_paise=Paise(0),
            )
        )

    for _ in range(N_NOISE_UNRELATED_NEFT):
        is_deposit = rng.random() < 0.5
        amount = _payment_amount_paise(rng)
        counterparty = rng.choice(_NAMED_COUNTERPARTIES)
        direction = "NEFT CR" if is_deposit else "NEFT DR"
        lines.append(
            _new_bank_line(
                rng,
                value_date=_random_value_date(rng, snapshot_date, max_days_back=20),
                narration=f"{direction}-{counterparty}-{_reference_token(rng)}",
                withdrawal_paise=Paise(0) if is_deposit else amount,
                deposit_paise=amount if is_deposit else Paise(0),
            )
        )

    for _ in range(N_NOISE_REVERSAL_PAIRS):
        utr = _hex_id(rng, "UTR", n_bytes=6).upper()
        amount = _payment_amount_paise(rng)
        credit_date = _random_value_date(rng, snapshot_date, max_days_back=20)
        reversal_date = credit_date + timedelta(days=rng.randint(1, 3))
        lines.append(
            _new_bank_line(
                rng,
                value_date=credit_date,
                narration=f"NEFT CR-UNRELATED VENDOR-{utr}",
                withdrawal_paise=Paise(0),
                deposit_paise=amount,
            )
        )
        lines.append(
            _new_bank_line(
                rng,
                value_date=reversal_date,
                narration=f"REVERSAL-{utr}",
                withdrawal_paise=amount,
                deposit_paise=Paise(0),
            )
        )

    return lines
