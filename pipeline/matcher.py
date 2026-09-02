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

**The cascade itself is entirely deterministic** — no RNG, no model call, per
§4.2 ("everything else is deterministic, including... the full FR-09 cascade").
`match_cases` adds exactly one question that is not: when two settlements
contest the same credit at tier 2, it asks `NarrationSemantics` which one the
narration names. Under the default `KeywordSemantics` the answer is always
`None` and the contested settlements simply abstain, so the cascade's behaviour
is unchanged from the paragraph above; the question exists so that arm can be
swapped and the difference measured. See `pipeline/semantics.py`'s docstring for
why this is the one read that can misroute money, and what gates it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from enum import IntEnum

from pipeline.case_assembly import Case, CaseKind
from pipeline.schemas import BankLine, Settlement
from pipeline.semantics import KEYWORD, ContestedCandidate, NarrationSemantics
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


def _contested_tier2_line_ids(cases: Sequence[Case]) -> set[str]:
    """Bank lines that more than one settlement claimed at tier 2.

    Tier 0 and tier 1 are not included: both key off `settlement.utr`, which is
    unique per settlement, so two settlements cannot legitimately claim one line
    through them. Tier 2 keys off an amount and a date window, and neither is
    unique to a settlement — which is exactly why it is the tier that can
    collide.
    """
    claims: dict[str, int] = {}
    for case in cases:
        if case.match_tier != int(MatchTier.AMOUNT_AND_WINDOW):
            continue
        for line in case.bank_lines:
            claims[line.line_id] = claims.get(line.line_id, 0) + 1
    return {line_id for line_id, count in claims.items() if count > 1}


def match_cases(
    cases: Sequence[Case],
    bank_lines: Sequence[BankLine],
    *,
    snapshot_date: date,
    semantics: NarrationSemantics = KEYWORD,
) -> list[Case]:
    """Run the cascade over every case, then resolve tier-2 contention across them.

    **§4.6's tie rule has to be applied twice, and only one of them was.**
    `match_settlement_anchored_case` enforces "a tie is not a match" *within* one
    settlement's candidate list (`len(tier2) == 1`), because that is the tie the
    cascade can see from inside a single case. But tier 2's key — an exact amount
    inside a T+2 window — is not unique to a settlement, so the symmetric tie
    exists *across* settlements and no per-case pass can detect it: two
    settlements of the same amount created the same day each see exactly one
    candidate credit, each match it at tier 2, each report `residual_paise = 0`,
    and both reach `AUTO_MATCHED` on the strength of one bank credit that can
    belong to at most one of them. That is a guaranteed false match, and
    `false_match_rate` is §1.6's primary safety metric for exactly this.

    It survived 592 tests and six seeds because `generator/clean.py` draws payment
    amounts lognormally, so an exact collision between two settlements in the same
    window is vanishingly rare — the reference batch simply never produced one.
    `data/contested/` is the batch that does, and `tests/test_contested.py` pins
    this.

    The resolution is the one §4.6 already states — "a tie is not a match; it
    routes to ambiguity" — applied to the contended line: every settlement
    claiming it falls back to tier 3, under the same §3.3 timing rule any other
    tier-3 case gets. Abstaining on both is correct rather than merely safe: the
    evidence genuinely does not say which settlement the credit belongs to, and
    §1.3's optimization principle ranks a false match strictly worse than a
    deferral.
    """
    matched = [match_settlement_anchored_case(case, bank_lines, snapshot_date=snapshot_date) for case in cases]
    contested = _contested_tier2_line_ids(matched)
    if not contested:
        return matched

    # One question per contended line, asked once: which settlement does the
    # narration say this credit pays? `KeywordSemantics` always answers `None`
    # (§4.6 has no tier that can read it), so the keyword arm's behaviour is
    # exactly the abstention above and nothing below changes it.
    winners: dict[str, str] = {}
    for line_id in sorted(contested):
        claimants = [
            case
            for case in matched
            if case.match_tier == int(MatchTier.AMOUNT_AND_WINDOW)
            and any(line.line_id == line_id for line in case.bank_lines)
        ]
        narration = next(
            (line.narration for case in claimants for line in case.bank_lines if line.line_id == line_id),
            "",
        )
        candidates = tuple(
            ContestedCandidate(
                settlement_id=case.case_id,
                payment_methods=tuple(sorted({str(line.method) for line in case.recon_lines if line.method})),
            )
            for case in claimants
        )
        winner = semantics.resolve_contested_credit(narration, candidates)
        if winner is not None:
            winners[line_id] = winner

    resolved: list[Case] = []
    for case in matched:
        if case.match_tier == int(MatchTier.AMOUNT_AND_WINDOW) and any(
            line.line_id in contested and winners.get(line.line_id) != case.case_id
            for line in case.bank_lines
        ):
            settlement = case.settlement
            assert settlement is not None  # tier 2 is settlement-anchored by construction
            in_window = is_within_settlement_window(_settlement_created_date(settlement), snapshot_date)
            # The demoted claim is dropped from `bank_lines` — the case is not
            # matched to this credit — but kept on `contested_bank_lines`, so the
            # money stays visible to the report and to `pipeline.bank_accounting`
            # instead of falling out of the batch. See `Case.contested_bank_lines`.
            lost = tuple(line for line in case.bank_lines if line.line_id in contested)
            resolved.append(
                case.model_copy(
                    update={
                        "bank_lines": (),
                        "contested_bank_lines": case.contested_bank_lines + lost,
                        "match_tier": int(MatchTier.NO_MATCH),
                        "residual_paise": _apply_timing_rule(int(settlement.amount), in_window=in_window),
                        "in_settlement_window": in_window,
                    }
                )
            )
        else:
            resolved.append(case)
    return resolved


def match_tier_distribution(cases: Sequence[Case]) -> dict[int, int]:
    """The count of matches at each tier (§4.6), settlement-anchored cases only."""
    counts: dict[int, int] = {}
    for case in cases:
        if case.match_tier is None:
            continue
        counts[case.match_tier] = counts.get(case.match_tier, 0) + 1
    return counts
