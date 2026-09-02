"""Where every bank line went, per spec.md §1.2 and §3.6.

**Why this module exists.** `data/contested/` shipped with Rs 12,693.20 of real
bank credit attached to nothing: four credits that narrate the gateway, so
`assemble_orphan_cases` never considered them, and that after FR-09 tier-2
demotion no settlement held either. The money was in no case, no metric and no
report. Nothing was *wrong* — every individual decision was correct and safe —
but the batch quietly stopped adding up, and no test could see it, because every
metric in §1.6 is denominated in **cases** and a bank line that reaches no case
is a line no case-denominated metric can count.

That is the failure mode this module closes. It is not a §1.6 metric and does
not grade anything against ground truth: it is a **partition proof**. Every
bank line in the batch lands in exactly one disposition, the dispositions are
exhaustive by construction, and `UNACCOUNTED` is the bucket that must always be
empty. If a future change makes a line reachable by no rule, it lands there and
`tests/test_bank_accounting.py` goes red — instead of the money simply not
appearing anywhere, which is what happened last time.

**The dispositions are read off the pipeline's own decisions, never re-derived.**
`is_reversal_shaped`, `find_self_matching_reversal_pairs` and the semantics
surface's `is_bank_charge` are imported from the modules that actually made the
call. Two independent copies of a classification rule drift, and a drifted copy
here would report a clean partition over a batch that no longer has one — the
same reasoning `pipeline/accounts.py` is single-sourced under.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from pipeline.case_assembly import Case, CaseKind, find_self_matching_reversal_pairs, is_reversal_shaped
from pipeline.schemas import BankLine
from pipeline.semantics import KEYWORD, NarrationSemantics


class BankLineDisposition(StrEnum):
    """Where one bank line ended up. Exhaustive: every line gets exactly one."""

    SETTLEMENT_EVIDENCE = "settlement_evidence"
    """Matched to a settlement-anchored case by FR-09's cascade."""

    ORPHAN_EVIDENCE = "orphan_evidence"
    """Evidence on an orphan case (§1.2, §3.6)."""

    CONTESTED_UNAWARDED = "contested_unawarded"
    """Claimed by two or more settlements at tier 2 and awarded to none of them
    (§4.6: "a tie is not a match"). Real money, deliberately unattached, and the
    bucket this module was written for. A non-zero count here is not a bug — it
    is the correct outcome of genuinely ambiguous evidence — but it must be
    *visible*, because each of these is a rupee amount a human has to resolve."""

    BANK_CHARGE = "bank_charge"
    """§3.6: "Bank charges stay noise, not cases."."""

    SELF_MATCHED_REVERSAL = "self_matched_reversal"
    """A credit and its own reversal netting to a wash inside one statement
    (REV-18) — neither line is a candidate for anything else."""

    OUTBOUND_NOISE = "outbound_noise"
    """An ordinary outbound debit to an unrelated party. Nothing in §3.6 makes
    an outbound-only line a case."""

    UNACCOUNTED = "unaccounted"
    """Reachable by no rule above. **Must always be empty.** This is the bucket
    whose emptiness is the property worth testing."""


class BankLineAccounting(BaseModel):
    """The partition, with the credit value each bucket holds.

    `credit_paise` sums `deposit_paise` only. A debit's magnitude is not money
    the reconciliation is responsible for placing, and mixing the two directions
    into one figure would produce a number that means nothing — the same reason
    §1.6's `value_coverage` is denominated the way it is.
    """

    model_config = ConfigDict(frozen=True)

    counts: dict[str, int]
    """Line count per disposition, every disposition present, zeroes included —
    a bucket that vanishes when empty is a bucket nobody notices reappearing."""

    credit_paise: dict[str, int]
    """Integer paise of `deposit_paise` per disposition (NFR-04)."""

    contested_unawarded_line_ids: tuple[str, ...]
    """Sorted, so the report can name the specific credits a human must resolve."""

    unaccounted_line_ids: tuple[str, ...]
    """Sorted. Must be empty."""

    @property
    def total_lines(self) -> int:
        return sum(self.counts.values())

    @property
    def contested_unawarded_paise(self) -> int:
        return self.credit_paise[str(BankLineDisposition.CONTESTED_UNAWARDED)]

    @property
    def is_total(self) -> bool:
        """Every line landed somewhere it should have."""
        return not self.unaccounted_line_ids


def account_bank_lines(
    bank_lines: Sequence[BankLine],
    cases: Sequence[Case],
    *,
    semantics: NarrationSemantics = KEYWORD,
) -> BankLineAccounting:
    """Partition `bank_lines` by where the run actually put each one.

    `cases` must be the **matched** cases (component 3's output, not case
    assembly's): tier-2 demotion is what moves a line from
    `SETTLEMENT_EVIDENCE` to `CONTESTED_UNAWARDED`, and it has not happened
    yet in assembly's output.
    """
    settlement_evidence: set[str] = set()
    orphan_evidence: set[str] = set()
    contested: set[str] = set()
    for case in cases:
        target = orphan_evidence if case.kind is CaseKind.ORPHAN else settlement_evidence
        target.update(line.line_id for line in case.bank_lines)
        contested.update(line.line_id for line in case.contested_bank_lines)
    # A credit awarded to one claimant is evidence for that settlement, even
    # though the losing claimants still carry it as contested. Attachment wins.
    contested -= settlement_evidence | orphan_evidence

    # The two noise rules, evaluated exactly as `assemble_orphan_cases` evaluates
    # them, over the same residual population it uses.
    residual = [line for line in bank_lines if not (line.deposit_paise > 0 and semantics.is_gateway_credit(line.narration))]
    charge_ids = {
        line.line_id
        for line in residual
        if line.withdrawal_paise > 0 and semantics.is_bank_charge(line.narration)
    }
    debits = [line for line in residual if line.withdrawal_paise > 0 and line.line_id not in charge_ids]
    reversal_candidates = [line for line in debits if is_reversal_shaped(line, semantics)]
    credit_candidates = [line for line in residual if line.deposit_paise > 0]
    _, self_matched_ids = find_self_matching_reversal_pairs(reversal_candidates, credit_candidates)

    def disposition(line: BankLine) -> BankLineDisposition:
        if line.line_id in settlement_evidence:
            return BankLineDisposition.SETTLEMENT_EVIDENCE
        if line.line_id in orphan_evidence:
            return BankLineDisposition.ORPHAN_EVIDENCE
        if line.line_id in contested:
            return BankLineDisposition.CONTESTED_UNAWARDED
        if line.line_id in self_matched_ids:
            return BankLineDisposition.SELF_MATCHED_REVERSAL
        if line.line_id in charge_ids:
            return BankLineDisposition.BANK_CHARGE
        if line.withdrawal_paise > 0:
            return BankLineDisposition.OUTBOUND_NOISE
        return BankLineDisposition.UNACCOUNTED

    counts = {str(value): 0 for value in BankLineDisposition}
    credit_paise = {str(value): 0 for value in BankLineDisposition}
    contested_ids: list[str] = []
    unaccounted_ids: list[str] = []

    for line in bank_lines:
        where = disposition(line)
        counts[str(where)] += 1
        credit_paise[str(where)] += line.deposit_paise
        if where is BankLineDisposition.CONTESTED_UNAWARDED:
            contested_ids.append(line.line_id)
        elif where is BankLineDisposition.UNACCOUNTED:
            unaccounted_ids.append(line.line_id)

    return BankLineAccounting(
        counts=counts,
        credit_paise=credit_paise,
        contested_unawarded_line_ids=tuple(sorted(contested_ids)),
        unaccounted_line_ids=tuple(sorted(unaccounted_ids)),
    )
