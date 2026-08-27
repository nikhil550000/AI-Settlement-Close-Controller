"""Case assembly, per spec.md §4.1 component 2 and §1.2.

> **Case assembly.** Recon lines grouped by `settlement_id` -> settlement-
> anchored cases; residual bank lines -> orphan cases (§1.2).

Two independent halves, combined by `assemble_cases`:

**Settlement-anchored** (§3.5: "each case is one settlement," confirmed by
every generator population using `case_id == settlement.id`): one case per
`Settlement`, holding every `recon_line` whose `settlement_id` matches it.
`ledger_entry` attachment is deliberately **not** this component's job —
see the module-level "Decided" note in BUILDLOG.md session 3.2. A later
component resolves a case's ledger evidence by looking up
`ledger_entry.reference` against the case's own recon-line `entity_id`s at
the point it needs them, rather than case assembly pre-attaching a subset
that isn't yet known to be relevant.

**Orphan** (§1.2, §3.6, REV-18): every `bank_line` not already evidence for
a settlement credit becomes a candidate. §3.6's population definitions
(`UNMATCHED_INBOUND_CREDIT`, `AMBIGUOUS_CASE`, `REVERSAL_UNMATCHED`,
`DUPLICATE_CREDIT`) are a **classification** the exception-subtype
classifier (component 5, §4.2 Slot A) assigns — not this component's job
either. What case assembly must decide is narrower and purely structural:
does a case exist for this line at all, and how many lines does it span
(REV-18's granularity correction: a duplicate credit and the original
share one case).

That narrower question is answered from four pieces of evidence, all
already present in `bank_line`, none of it borrowed from the generator:

1. **A credit whose narration names Razorpay** is a settlement-credit
   candidate, not an orphan. Every settlement credit the generator writes
   — `SETTLEMENT_UTR_MISSING`'s UTR-less ones included — narrates its
   remitter as one of a small set of "RAZORPAY ..." party strings, because
   that is what a real NEFT/RTGS credit *from* Razorpay would show in the
   remitter field. This is the one piece of evidence that lets case
   assembly run before the matcher (component 3, session 3.3) exists: it
   does not need to know *which* settlement a credit belongs to, only that
   it is presumptively spoken for.
2. **A bank-charge-shaped debit** (narration reads as a fee/charge line)
   carries no case — §3.6: "Bank charges stay noise, not cases."
3. **A reversal-shaped debit** (narration reads as a reversal/return) is
   checked against every remaining credit for a shared reference token. A
   match means the credit and its own reversal net to a wash in the same
   statement — noise, not a case, and neither line is a candidate for
   anything else. No match means `REVERSAL_UNMATCHED` — one case, one line.
4. **Any other debit** (an ordinary outbound transfer to an unrelated
   party) is noise; nothing in §3.6 makes an outbound-only line a case.
5. **Remaining credits** pair up when two of them share identical
   narration, amount and value date — REV-18's duplicate-credit signature
   — and form one two-line case. An unpaired remaining credit is a
   one-line case (`UNMATCHED_INBOUND_CREDIT` or `AMBIGUOUS_CASE`
   underneath, for the classifier to tell apart).
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from pipeline.schemas import BankLine, ReconLine, Settlement

_RAZORPAY_MARKER = "RAZORPAY"
"""Every settlement-credit narration names one of `SETTLEMENT_PARTIES` (generator/narration.py), all of which contain this."""

_BANK_CHARGE_KEYWORDS = ("CHARGE", "FEE")
"""§3.6's bank-charge narration pool reads as a fee/charge line in plain English; these two words cover all of it."""

_REVERSAL_KEYWORDS = ("REVERSAL", "RETURN", "REV-", "RET-")
"""A reversal/return narration names itself as one, in every §3.6 reversal template."""

_REFERENCE_TOKEN_RE = re.compile(r"[A-Z0-9]{8,}")
"""§4.6 tier 1's own token shape: an alphanumeric run of length >= 8.

Restricted below to tokens containing a digit, which a party name (letters
and spaces only) never does — a real UTR/reference always does. Without
that restriction, two lines that happen to share a counterparty name drawn
from the same eight-name pool would falsely look like a matching pair.
"""


class CaseKind(StrEnum):
    SETTLEMENT_ANCHORED = "settlement_anchored"
    ORPHAN = "orphan"


class Case(BaseModel):
    """One assembled reconciliation case (§1.2), before matching or classification.

    `settlement`/`recon_lines` are populated for `SETTLEMENT_ANCHORED`
    cases and empty for `ORPHAN` cases; `bank_lines` is the reverse. No
    case carries both — §1.2's two anchor kinds are exclusive.
    """

    model_config = ConfigDict(frozen=True)

    case_id: str
    kind: CaseKind
    settlement: Settlement | None = None
    recon_lines: tuple[ReconLine, ...] = ()
    bank_lines: tuple[BankLine, ...] = ()


def assemble_cases(
    settlements: Sequence[Settlement],
    recon_lines: Sequence[ReconLine],
    bank_lines: Sequence[BankLine],
) -> list[Case]:
    """Every reconciliation case in the batch: settlement-anchored cases, then orphan cases."""
    return assemble_settlement_anchored_cases(settlements, recon_lines) + assemble_orphan_cases(bank_lines)


def assemble_settlement_anchored_cases(
    settlements: Sequence[Settlement], recon_lines: Sequence[ReconLine]
) -> list[Case]:
    """One case per settlement, `case_id == settlement.id` (matches every generator population's own convention)."""
    lines_by_settlement: dict[str, list[ReconLine]] = defaultdict(list)
    for line in recon_lines:
        if line.settlement_id is not None:
            lines_by_settlement[line.settlement_id].append(line)

    return [
        Case(
            case_id=settlement.id,
            kind=CaseKind.SETTLEMENT_ANCHORED,
            settlement=settlement,
            recon_lines=tuple(lines_by_settlement.get(settlement.id, ())),
        )
        for settlement in settlements
    ]


def assemble_orphan_cases(bank_lines: Sequence[BankLine]) -> list[Case]:
    """Orphan cases from bank lines with no settlement-credit narration, per this module's docstring."""
    residual = [line for line in bank_lines if not _is_razorpay_credit(line)]

    charge_ids = {line.line_id for line in residual if _is_bank_charge(line)}
    debits = [line for line in residual if line.withdrawal_paise > 0 and line.line_id not in charge_ids]
    reversal_candidates = [line for line in debits if _is_reversal_shaped(line)]
    # Any other debit (line.line_id not in charge_ids and not reversal-shaped) is
    # plain outbound noise: never a case, and it needs no further handling.

    credit_candidates = [line for line in residual if line.deposit_paise > 0]
    dup_pairs, dup_ids = _find_duplicate_credit_pairs(credit_candidates)
    unpaired_credits = [line for line in credit_candidates if line.line_id not in dup_ids]

    self_matched_pairs, self_matched_ids = _find_self_matching_reversal_pairs(
        reversal_candidates, unpaired_credits
    )
    unmatched_reversals = [line for line in reversal_candidates if line.line_id not in self_matched_ids]
    remaining_credits = [line for line in unpaired_credits if line.line_id not in self_matched_ids]

    cases = [_orphan_case(pair) for pair in dup_pairs]
    cases += [_orphan_case((line,)) for line in unmatched_reversals]
    cases += [_orphan_case((line,)) for line in remaining_credits]
    return cases


def _is_razorpay_credit(line: BankLine) -> bool:
    return line.deposit_paise > 0 and _RAZORPAY_MARKER in line.narration.upper()


def _is_bank_charge(line: BankLine) -> bool:
    narration = line.narration.upper()
    return line.withdrawal_paise > 0 and any(keyword in narration for keyword in _BANK_CHARGE_KEYWORDS)


def _is_reversal_shaped(line: BankLine) -> bool:
    narration = line.narration.upper()
    return line.withdrawal_paise > 0 and any(keyword in narration for keyword in _REVERSAL_KEYWORDS)


def _reference_tokens(narration: str) -> set[str]:
    return {token for token in _REFERENCE_TOKEN_RE.findall(narration.upper()) if any(c.isdigit() for c in token)}


def _find_duplicate_credit_pairs(
    credit_lines: Sequence[BankLine],
) -> tuple[list[tuple[BankLine, BankLine]], set[str]]:
    """REV-18: two credits sharing narration, amount and value date are one case, not two."""
    groups: dict[tuple[str, int, object], list[BankLine]] = defaultdict(list)
    for line in credit_lines:
        groups[(line.narration, int(line.deposit_paise), line.value_date)].append(line)

    pairs: list[tuple[BankLine, BankLine]] = []
    paired_ids: set[str] = set()
    for lines in groups.values():
        for i in range(0, len(lines) - 1, 2):
            a, b = lines[i], lines[i + 1]
            pairs.append((a, b))
            paired_ids.update({a.line_id, b.line_id})
    return pairs, paired_ids


def _find_self_matching_reversal_pairs(
    reversal_lines: Sequence[BankLine], credit_lines: Sequence[BankLine]
) -> tuple[list[tuple[BankLine, BankLine]], set[str]]:
    """A reversal whose narration shares a reference token with some credit's narration is a wash, not a case."""
    credit_by_token: dict[str, BankLine] = {}
    for line in credit_lines:
        for token in _reference_tokens(line.narration):
            credit_by_token.setdefault(token, line)

    pairs: list[tuple[BankLine, BankLine]] = []
    excluded_ids: set[str] = set()
    for reversal in reversal_lines:
        match = next(
            (credit_by_token[token] for token in _reference_tokens(reversal.narration) if token in credit_by_token),
            None,
        )
        if match is not None:
            pairs.append((reversal, match))
            excluded_ids.update({reversal.line_id, match.line_id})
    return pairs, excluded_ids


def _orphan_case(lines: tuple[BankLine, ...]) -> Case:
    case_id = "case_orphan_" + min(line.line_id for line in lines)
    return Case(case_id=case_id, kind=CaseKind.ORPHAN, bank_lines=lines)
