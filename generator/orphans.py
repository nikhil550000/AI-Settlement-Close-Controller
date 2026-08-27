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

**Every string here comes from `generator/narration.py`'s shared pool.**
Two of session 2.2's constructions were narration fingerprints rather than
evidence, and both are gone: the noise reversal pairs' credit leg named a
counterparty (`"UNRELATED VENDOR"`) that appeared nowhere else in the
batch, and the `REVERSAL_UNMATCHED` cases used a reversal sentence shape
the noise pairs never used. §3.6 separates those two populations by
whether a matching prior credit exists in the batch — that is the
evidence, and it must be the only separator.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from generator.bank_lines import new_bank_line
from generator.clean import SETTLEMENT_MAX_DAYS_BACK, _hex_id, _payment_amount_paise
from generator.families import FamilyBatch
from generator.narration import (
    NAMED_COUNTERPARTIES,
    UtrShape,
    bank_charge_narration,
    credit_narration,
    debit_narration,
    opaque_credit_narration,
    random_utr,
    reversal_narration,
)
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

_ORPHAN_CREDIT_SHAPES = (UtrShape.CLEAN, UtrShape.EMBEDDED)
"""How a non-settlement credit carries its own reference.

Orphan and noise credits reference a token that belongs to no settlement,
so no §4.6 tier can match them however the token is written. Drawing
across two shapes keeps them from being the batch's only single-shaped
credits, which would itself be a tell.
"""


def _orphan_case_id(rng: random.Random) -> str:
    return _hex_id(rng, "orphan_")


def _random_value_date(rng: random.Random, snapshot_date: date) -> date:
    """A bank line's value date, drawn over the same span every other line uses.

    Session 2.2 dated noise lines up to 20 days back while every case-
    bearing line sat within 11, so "older than eleven days" selected noise
    outright — the ignore path became learnable from the date column
    instead of from the line.
    """
    return snapshot_date - timedelta(days=rng.randint(0, SETTLEMENT_MAX_DAYS_BACK))


def generate_unmatched_inbound_credit_batch(
    rng: random.Random, snapshot_date: date, n_cases: int = N_UNMATCHED_INBOUND_CREDIT
) -> FamilyBatch:
    """`OPERATIONAL_EXCEPTION`/`UNMATCHED_INBOUND_CREDIT`: inbound NEFT, counterparty named in narration."""
    batch = FamilyBatch()
    for _ in range(n_cases):
        counterparty = rng.choice(NAMED_COUNTERPARTIES)
        line = new_bank_line(
            rng,
            value_date=_random_value_date(rng, snapshot_date),
            narration=credit_narration(
                rng, party=counterparty, utr=random_utr(rng), shape=rng.choice(_ORPHAN_CREDIT_SHAPES)
            ),
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
        line = new_bank_line(
            rng,
            value_date=_random_value_date(rng, snapshot_date),
            narration=opaque_credit_narration(rng),
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
        line = new_bank_line(
            rng,
            value_date=_random_value_date(rng, snapshot_date),
            narration=reversal_narration(
                rng, party=rng.choice(NAMED_COUNTERPARTIES), reference=random_utr(rng)
            ),
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
        utr = random_utr(rng)
        amount = _payment_amount_paise(rng)
        value_date = _random_value_date(rng, snapshot_date)
        narration = credit_narration(
            rng, party=rng.choice(NAMED_COUNTERPARTIES), utr=utr, shape=rng.choice(_ORPHAN_CREDIT_SHAPES)
        )
        line_1 = new_bank_line(
            rng, value_date=value_date, narration=narration, withdrawal_paise=Paise(0), deposit_paise=amount
        )
        line_2 = new_bank_line(
            rng, value_date=value_date, narration=narration, withdrawal_paise=Paise(0), deposit_paise=amount
        )
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


ORPHAN_POPULATIONS = (
    ("unmatched_inbound_credit", generate_unmatched_inbound_credit_batch),
    ("ambiguous_orphan", generate_ambiguous_orphan_batch),
    ("reversal_unmatched", generate_reversal_unmatched_batch),
    ("duplicate_credit", generate_duplicate_credit_batch),
)
"""§3.6's four orphan populations in generation order, named — see `FAMILY_POPULATIONS`."""


def generate_all_orphan_batches(rng: random.Random, snapshot_date: date) -> FamilyBatch:
    """All four §3.6 orphan populations, 25 cases / 28 bank lines combined."""
    combined = FamilyBatch()
    for _name, generate in ORPHAN_POPULATIONS:
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
            new_bank_line(
                rng,
                value_date=_random_value_date(rng, snapshot_date),
                narration=bank_charge_narration(rng),
                withdrawal_paise=Paise(rng.randint(50, 500) * 100),
                deposit_paise=Paise(0),
            )
        )

    for _ in range(N_NOISE_UNRELATED_NEFT):
        is_deposit = rng.random() < 0.5
        amount = _payment_amount_paise(rng)
        counterparty = rng.choice(NAMED_COUNTERPARTIES)
        reference = random_utr(rng)
        narration = (
            credit_narration(rng, party=counterparty, utr=reference, shape=rng.choice(_ORPHAN_CREDIT_SHAPES))
            if is_deposit
            else debit_narration(rng, party=counterparty, reference=reference)
        )
        lines.append(
            new_bank_line(
                rng,
                value_date=_random_value_date(rng, snapshot_date),
                narration=narration,
                withdrawal_paise=Paise(0) if is_deposit else amount,
                deposit_paise=amount if is_deposit else Paise(0),
            )
        )

    for _ in range(N_NOISE_REVERSAL_PAIRS):
        utr = random_utr(rng)
        amount = _payment_amount_paise(rng)
        counterparty = rng.choice(NAMED_COUNTERPARTIES)
        credit_date = _random_value_date(rng, snapshot_date)
        # A statement drawn as of the snapshot cannot carry a line dated after it.
        reversal_date = min(credit_date + timedelta(days=rng.randint(1, 3)), snapshot_date)
        lines.append(
            new_bank_line(
                rng,
                value_date=credit_date,
                narration=credit_narration(
                    rng, party=counterparty, utr=utr, shape=rng.choice(_ORPHAN_CREDIT_SHAPES)
                ),
                withdrawal_paise=Paise(0),
                deposit_paise=amount,
            )
        )
        lines.append(
            new_bank_line(
                rng,
                value_date=reversal_date,
                narration=reversal_narration(rng, party=counterparty, reference=utr),
                withdrawal_paise=amount,
                deposit_paise=Paise(0),
            )
        )

    return lines
