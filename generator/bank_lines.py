"""Canonical `bank_line` construction: bank-line decomposition into
roughly 98 settlement credits, 28 orphan-case lines, and 50 non-settlement
noise lines (about 175 total).

`bank_line` records are generated directly in their **post-adapter
canonical shape** (`pipeline.schemas.BankLine`) — not raw bank-statement
CSV/XLSX text. A column-mapping adapter that produces this shape from a
real bank export is a separate concern; this module targets the
already-adapted, canonical shape the pipeline consumes.

Every settlement credit is built here with a **clean, full UTR**; the
50/25/15/10 clean/embedded/truncated/absent split is applied in a
single later pass over the assembled batch (`generator/finalize.py`),
because the split is a property of the batch as a whole and cannot be
allocated correctly one population at a time. A batch that is generated
but never finalized is therefore all-clean — usable, and honest about
being unfinished, rather than half-shaped.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

from generator.batch import GeneratedBatch
from generator.narration import (
    SETTLEMENT_PARTIES,
    UtrShape,
    bank_reference_token,
    credit_narration,
)
from pipeline.money import Paise
from pipeline.schemas import BankLine, BankProfile, Settlement

_BANK_PROFILES = tuple(BankProfile)

BANK_REF_NO_PRESENT_PERCENT = 60
"""Share of bank lines carrying a `bank_ref_no` at all.

Applied identically to every line in the batch — settlement credit, orphan
case, and noise alike — so that whether the column is populated carries no
scenario signal. It is a secondary matching signal alongside the
narration-embedded UTR, and the matcher's first tier reads it, so leaving
it null everywhere would leave that branch of the cascade dead.
"""

SETTLEMENT_CREDIT_LAG_MAX_DAYS = 1
"""Calendar days a landed settlement credit may trail the settlement's own date.

Well inside the T+2 working-day window, which is what makes these
the *landed* population rather than a timing case.
"""


def bank_line_id(rng: random.Random) -> str:
    return f"bank_{rng.getrandbits(32):08x}"


def random_bank_profile(rng: random.Random) -> BankProfile:
    """A bank format profile tag only, not a chart-of-accounts dimension. Uniform choice — no stated weighting."""
    return rng.choice(_BANK_PROFILES)


def random_closing_balance(rng: random.Random) -> Paise:
    """A plausible standalone balance figure.

    No running-balance continuity across lines is modeled: nothing in any
    checkpoint reads `closing_balance_paise` beyond it being a valid
    `NonNegPaise`, and continuity would depend on a chronological line
    order that the global shuffle pass deliberately scrambles.
    """
    return Paise(rng.randint(1_00_000_00, 50_00_000_00))


def random_bank_ref_no(rng: random.Random) -> str | None:
    """The bank's own reference number, or `None`. Never a settlement UTR — see `finalize` for that."""
    if rng.randrange(100) >= BANK_REF_NO_PRESENT_PERCENT:
        return None
    return bank_reference_token(rng)


def new_bank_line(
    rng: random.Random,
    *,
    value_date: date,
    narration: str,
    withdrawal_paise: Paise,
    deposit_paise: Paise,
) -> BankLine:
    """One canonical bank line. Identity, profile, balance and reference are drawn identically for every caller."""
    return BankLine(
        line_id=bank_line_id(rng),
        value_date=value_date,
        narration=narration,
        bank_ref_no=random_bank_ref_no(rng),
        withdrawal_paise=withdrawal_paise,
        deposit_paise=deposit_paise,
        closing_balance_paise=random_closing_balance(rng),
        bank_profile=random_bank_profile(rng),
    )


def settlement_credit_bank_line(rng: random.Random, *, value_date: date, amount: Paise, utr: str) -> BankLine:
    """A landed bank credit for a settlement.

    Written with a clean, full UTR (or none at all when the settlement
    carries none — `SETTLEMENT_UTR_MISSING`, where no bank-side anchor
    exists); `generator/finalize.py` reshapes a planned share of these
    into the embedded, truncated and absent forms the matcher must handle.
    """
    shape = UtrShape.ABSENT if not utr else UtrShape.CLEAN
    return new_bank_line(
        rng,
        value_date=value_date,
        narration=credit_narration(rng, party=rng.choice(SETTLEMENT_PARTIES), utr=utr, shape=shape),
        withdrawal_paise=Paise(0),
        deposit_paise=amount,
    )


def add_settlement_credit(
    batch: GeneratedBatch,
    rng: random.Random,
    *,
    settlement: Settlement,
    snapshot_date: date,
    amount: Paise | None = None,
) -> BankLine:
    """Land `settlement`'s bank credit into `batch` and record the link for the finalize pass.

    Every settlement-anchored population outside the 27 no-credit
    cases (family 4 core, family-4 no-op, `BANK_CREDIT_OVERDUE`) routes
    through here, so the credit's date, amount, narration and profile are
    drawn the same way for all of them.

    `amount` overrides `settlement.amount` for the one population where
    the two legitimately differ: `SETTLEMENT_AMOUNT_MISMATCH`, where the
    bank credits the true recon-line total and the settlement *header* is
    the wrong record.

    The value date never runs past `snapshot_date`: a bank statement drawn
    as of the snapshot cannot contain a line dated after it, and
    settlements created on the snapshot date itself are now possible.
    """
    created_date = _utc_date(settlement.created_at)
    lag = timedelta(days=rng.randint(0, SETTLEMENT_CREDIT_LAG_MAX_DAYS))
    line = settlement_credit_bank_line(
        rng,
        value_date=min(created_date + lag, snapshot_date),
        amount=settlement.amount if amount is None else amount,
        utr=settlement.utr,
    )
    batch.bank_lines.append(line)
    batch.settlement_credit_of[line.line_id] = settlement.id
    return line


def _utc_date(unix_ts: int) -> date:
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).date()
