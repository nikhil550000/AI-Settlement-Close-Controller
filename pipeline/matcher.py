"""The matcher, per spec.md §4.1 component 3 and §4.6 (FR-09 UTR fallback matching).

A four-tier cascade, first hit wins, run once per settlement-anchored
`Case` against the full `bank_lines` pool (case assembly's Razorpay-
narration exclusion only decided "not an orphan" — it never identified
*which* settlement a credit belongs to, so every candidate is still live
here):

| Tier | Rule |
|---|---|
| 0 | `settlement.utr` appears as a token in the narration, or equals `bank_ref_no`, after uppercasing and stripping non-alphanumerics |
| 1 | An alphanumeric run of length >= 8 in the narration is a contiguous prefix of the UTR of length >= 8 |
| 2 | A credit's `deposit_paise` equals `settlement.amount` exactly, `value_date` inside the T+2 window plus one slack day, and it is the only such candidate |
| 3 | No match |

**Tier 0 vs tier 1, concretely.** §4.6's tier 0 text and `generator/narration.py`'s
own `UtrShape` docstring both leave "token" ambiguous on purpose ("EMBEDDED sits
between tier 0 and tier 1 depending on how the matcher tokenizes... session 3.3's
call to make"). This module resolves it using the *same* contrast the generator's
shape definitions draw: `CLEAN` is "delimited by whitespace on both sides" and
`EMBEDDED` is "delimited by punctuation inside a longer run". So tier 0 splits the
narration on whitespace only and requires one whole whitespace-delimited word to
equal the UTR after stripping punctuation from it — `CLEAN` always satisfies this
(every clean template writes the UTR as its own space-delimited word), `EMBEDDED`
never does (every embedded template glues the UTR to its neighbour with `-`, `/`,
or `*`, so the enclosing whitespace-delimited chunk normalizes to something bigger
than the bare UTR). Tier 1 then extracts alphanumeric runs with the finer
`[A-Z0-9]{8,}` regex (any non-alphanumeric character, including whitespace,
is a boundary) — the same token shape `pipeline/case_assembly.py` already uses
for its reference-token pairing rule — which isolates the UTR (or its truncated
prefix) out of both `EMBEDDED` and `TRUNCATED` narrations.

**Tier 2's residual is forced to 0 paise when it lands inside the settlement
window** — see `_apply_timing_rule`. §3.3's timing-residual rule is stated as a
rule *the matcher* needs ("or it will compute a non-zero residual and never emit
`AUTO_MATCHED`"), not a downstream classifier: a tier-3 case still inside the T+2
working-day window is the "correct state of the world," and reporting its full
settlement amount as an unresolved gap would be wrong evidence, not merely an
unclassified one. Past the window, the residual is the full `settlement.amount` —
a real, unresolved gap (`BANK_CREDIT_OVERDUE`).

Matched tiers 0-2 report the residual as struck between `settlement.amount` and
the sum of matched `deposit_paise` with **no** timing adjustment: a tier-0/1 match
whose deposit differs from the settlement header (`SETTLEMENT_AMOUNT_MISMATCH`) is
supposed to leave that gap visible for the predicate evaluator (component 4) to
find, not have the matcher paper over it.

Entirely deterministic — no RNG, no model call, per §4.2 ("everything else is
deterministic, including... the full FR-09 cascade").
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from enum import IntEnum

from pipeline.case_assembly import Case, CaseKind
from pipeline.schemas import BankLine, Settlement
from pipeline.timing import is_within_settlement_window, settlement_window_deadline

_TOKEN_RE = re.compile(r"[A-Z0-9]{8,}")
"""§4.6 tier 1's own token shape, shared with `pipeline/case_assembly.py`'s
`_REFERENCE_TOKEN_RE`: an alphanumeric run of length >= 8."""

_TIER1_MIN_LENGTH = 8

_TIER2_SLACK_DAYS = 1
"""§4.6 tier 2: "inside the T+2 working-day window plus one slack day"."""


class MatchTier(IntEnum):
    UTR_EXACT = 0
    UTR_PREFIX = 1
    AMOUNT_AND_WINDOW = 2
    NO_MATCH = 3


def _normalize(text: str) -> str:
    return "".join(ch for ch in text.upper() if ch.isalnum())


def _settlement_created_date(settlement: Settlement) -> date:
    return datetime.fromtimestamp(settlement.created_at, tz=timezone.utc).date()


def _tier0_candidates(utr_normalized: str, bank_lines: Sequence[BankLine]) -> list[BankLine]:
    if not utr_normalized:
        return []  # SETTLEMENT_UTR_MISSING: nothing to compare against.
    matches = []
    for line in bank_lines:
        if line.deposit_paise <= 0:
            continue
        if line.bank_ref_no is not None and _normalize(line.bank_ref_no) == utr_normalized:
            matches.append(line)
            continue
        if any(_normalize(word) == utr_normalized for word in line.narration.split()):
            matches.append(line)
    return matches


def _tier1_candidates(utr_upper: str, bank_lines: Sequence[BankLine]) -> list[BankLine]:
    if len(utr_upper) < _TIER1_MIN_LENGTH:
        return []
    matches = []
    for line in bank_lines:
        if line.deposit_paise <= 0:
            continue
        for token in _TOKEN_RE.findall(line.narration.upper()):
            if len(token) >= _TIER1_MIN_LENGTH and utr_upper.startswith(token):
                matches.append(line)
                break
    return matches


def _tier2_candidates(settlement: Settlement, bank_lines: Sequence[BankLine], window_end: date) -> list[BankLine]:
    created_date = _settlement_created_date(settlement)
    return [
        line
        for line in bank_lines
        if line.deposit_paise == settlement.amount and created_date <= line.value_date <= window_end
    ]


def _apply_timing_rule(settlement_amount: int, *, in_window: bool) -> int:
    """§3.3's timing-residual rule: a tier-3 case still inside the window is the
    *correct* state of the world, so its residual reports as 0, not as a gap."""
    return 0 if in_window else settlement_amount


def match_settlement_anchored_case(case: Case, bank_lines: Sequence[BankLine], *, snapshot_date: date) -> Case:
    """Run the FR-09 cascade for one settlement-anchored case; pass an orphan case through untouched."""
    if case.kind is not CaseKind.SETTLEMENT_ANCHORED:
        return case
    settlement = case.settlement
    if settlement is None:
        raise ValueError(f"settlement-anchored case {case.case_id!r} carries no settlement")

    utr_upper = settlement.utr.upper()
    utr_normalized = _normalize(settlement.utr)

    tier0 = _tier0_candidates(utr_normalized, bank_lines)
    if tier0:
        return _matched(case, tier0, MatchTier.UTR_EXACT, settlement)

    tier1 = _tier1_candidates(utr_upper, bank_lines)
    if tier1:
        return _matched(case, tier1, MatchTier.UTR_PREFIX, settlement)

    window_end = settlement_window_deadline(_settlement_created_date(settlement)) + timedelta(days=_TIER2_SLACK_DAYS)
    tier2 = _tier2_candidates(settlement, bank_lines, window_end)
    if len(tier2) == 1:
        return _matched(case, tier2, MatchTier.AMOUNT_AND_WINDOW, settlement)
    # len(tier2) > 1 is a tie: §4.6 "a tie is not a match; it routes to ambiguity" — falls through to tier 3.

    in_window = is_within_settlement_window(_settlement_created_date(settlement), snapshot_date)
    residual = _apply_timing_rule(int(settlement.amount), in_window=in_window)
    return case.model_copy(
        update={
            "match_tier": int(MatchTier.NO_MATCH),
            "residual_paise": residual,
            "in_settlement_window": in_window,
        }
    )


def _matched(case: Case, matched_lines: Sequence[BankLine], tier: MatchTier, settlement: Settlement) -> Case:
    matched = tuple(matched_lines)
    residual = int(settlement.amount) - sum(int(line.deposit_paise) for line in matched)
    return case.model_copy(
        update={
            "bank_lines": matched,
            "match_tier": int(tier),
            "residual_paise": residual,
        }
    )


def match_cases(cases: Sequence[Case], bank_lines: Sequence[BankLine], *, snapshot_date: date) -> list[Case]:
    """Run the cascade over every case; orphan cases pass through unchanged (already evidence-complete)."""
    return [match_settlement_anchored_case(case, bank_lines, snapshot_date=snapshot_date) for case in cases]


def match_tier_distribution(cases: Sequence[Case]) -> dict[int, int]:
    """The count of matches at each tier (§4.6), settlement-anchored cases only."""
    counts: dict[int, int] = {}
    for case in cases:
        if case.match_tier is None:
            continue
        counts[case.match_tier] = counts.get(case.match_tier, 0) + 1
    return counts
