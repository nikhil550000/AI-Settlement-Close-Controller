"""Canonical `bank_line` construction, per spec.md §3.1 and FR-01's
bank-line decomposition (REV-17: ~98 settlement credits + ~28 orphan-case
lines + ~50 non-settlement noise, ~175 total).

Session 2.2 generates `bank_line` records directly in their
**post-adapter canonical shape** (`pipeline.schemas.BankLine`) — not raw
bank-statement CSV/XLSX text. The FR-08 column-mapping adapter that
produces this shape from a real bank export is session 3.1's job; §3.1
itself describes `bank_line` as "the post-adapter canonical shape the
pipeline consumes," which is exactly the layer this module targets.

UTR narration variety (§4.6's 50/25/15/10 clean/embedded/truncated/absent
split, the "generator obligation" for exercising the FR-09 cascade) is
explicitly session 2.3's job ("Global shuffle pass, shared narration
pool, UTR variety, fingerprint assertions", §6.3). This module embeds
settlement UTRs as a clean, full token — the FR-09 cascade's easy tier —
by design; session 2.3 introduces the harder variants.
"""

from __future__ import annotations

import random
from datetime import date

from pipeline.money import Paise
from pipeline.schemas import BankLine, BankProfile

_BANK_PROFILES = tuple(BankProfile)


def bank_line_id(rng: random.Random) -> str:
    return f"bank_{rng.getrandbits(32):08x}"


def random_bank_profile(rng: random.Random) -> BankProfile:
    """A bank format profile tag only (§3.1: "not a COA dimension"). Uniform choice — no stated weighting."""
    return rng.choice(_BANK_PROFILES)


def random_closing_balance(rng: random.Random) -> Paise:
    """A plausible standalone balance figure.

    No running-balance continuity across lines is modeled: nothing in
    session 2.2's checkpoint (§6.3: "REV-17 bank-line split holds")
    depends on `closing_balance_paise` beyond it being a valid
    `NonNegPaise`, and continuity would depend on a chronological line
    order that session 2.3's global shuffle pass deliberately scrambles.
    """
    return Paise(rng.randint(1_00_000_00, 50_00_000_00))


def settlement_credit_bank_line(rng: random.Random, *, value_date: date, amount: Paise, utr: str) -> BankLine:
    """A landed bank credit for a settlement, tier-0 matchable (§4.6): clean, full UTR embedded in narration."""
    return BankLine(
        line_id=bank_line_id(rng),
        value_date=value_date,
        narration=f"NEFT CR-RAZORPAY SOFTWARE PVT LTD-{utr}",
        bank_ref_no=None,
        withdrawal_paise=Paise(0),
        deposit_paise=amount,
        closing_balance_paise=random_closing_balance(rng),
        bank_profile=random_bank_profile(rng),
    )


def settlement_credit_bank_line_no_utr(rng: random.Random, *, value_date: date, amount: Paise) -> BankLine:
    """A landed bank credit for a settlement with no recoverable UTR (`SETTLEMENT_UTR_MISSING`, §3.3).

    The settlement itself carries no UTR (`settlement.utr == ""`), so
    there is nothing to embed in the narration — this is what "no
    bank-side anchor exists" (§3.3's trigger text) means concretely.
    """
    return BankLine(
        line_id=bank_line_id(rng),
        value_date=value_date,
        narration="NEFT CR-RAZORPAY SOFTWARE PVT LTD",
        bank_ref_no=None,
        withdrawal_paise=Paise(0),
        deposit_paise=amount,
        closing_balance_paise=random_closing_balance(rng),
        bank_profile=random_bank_profile(rng),
    )
