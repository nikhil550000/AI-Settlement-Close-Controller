"""The container every population generator returns.

Sessions 1.3 and 2.1 defined two structurally identical dataclasses
(`CleanBatch`, `FamilyBatch`) and session 2.2 reused the second for the
orphan populations. Session 2.3 adds a field that every population needs
— `settlement_credit_of` — and needs to concatenate all of them into one
batch before the global pass, so the two collapse into one type here and
survive as aliases at their old import sites.

The four record lists are exactly the §3.1 canonical schemas plus §1.6's
ground truth: what the generator writes to disk. `settlement_credit_of`
is generator-side bookkeeping that never reaches a JSONL file — see its
own docstring.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pipeline.ground_truth import GroundTruthCase
from pipeline.schemas import BankLine, LedgerEntry, ReconLine, Settlement


@dataclass(frozen=True)
class GeneratedBatch:
    settlements: list[Settlement] = field(default_factory=list)
    recon_lines: list[ReconLine] = field(default_factory=list)
    ledger_entries: list[LedgerEntry] = field(default_factory=list)
    bank_lines: list[BankLine] = field(default_factory=list)
    ground_truth: list[GroundTruthCase] = field(default_factory=list)

    settlement_credit_of: dict[str, str] = field(default_factory=dict)
    """`bank_line.line_id` -> `settlement.id`, for landed settlement credits only.

    Not part of the dataset: §3.1 has no such field and the pipeline must
    discover this link for itself — recovering it is precisely what §4.6's
    cascade is graded on. It exists because `generator/finalize.py`'s UTR
    variety pass has to rewrite each settlement credit's narration knowing
    which settlement's UTR it carries, and rediscovering that after the
    fact by parsing narrations would be the generator grading its own
    matcher. REV-17's 27 no-credit cases are simply absent from it.
    """

    def extend(self, other: "GeneratedBatch") -> None:
        self.settlements.extend(other.settlements)
        self.recon_lines.extend(other.recon_lines)
        self.ledger_entries.extend(other.ledger_entries)
        self.bank_lines.extend(other.bank_lines)
        self.ground_truth.extend(other.ground_truth)
        self.settlement_credit_of.update(other.settlement_credit_of)
