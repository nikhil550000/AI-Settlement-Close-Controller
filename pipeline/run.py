"""One end-to-end pass of components 1-8 over a batch.

**Not a new component.** §4.1 fixes ten components and one direction of
data flow with no cycles; this module is that flow, made executable, and
adds no logic of its own beyond calling each stage in order and handing
one stage's output to the next. FR-10's CLI surface, built in a later
session, calls this rather than re-deriving the wiring — and the Phase 4
checkpoint ("first end-to-end run producing all five terminal states")
needs it to exist to be checkable at all.

The graded path only. `pipeline/` never imports `generator/` (§4.1), so
`run_batch` takes already-loaded records; where they came from — the
committed reference dataset, a raw bank export through the FR-08 adapter,
or the generator in a test — is the caller's business.

Component 5 (classifier) is absent from the chain, and that is
deliberate rather than pending: §4.2 assigns it the one graded LLM slot,
session 5.2 builds it, and the exception *class and subtype* it assigns
are a second axis that §3.3 states does not determine the outcome state.
The five terminal states this run produces are complete without it. What
is missing without it is named in `KNOWN_GAPS` rather than left for a
reader to discover from a metric.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import date

from pydantic import BaseModel, ConfigDict

from pipeline.apply import BatchOutcome, apply_batch, seed_ledger
from pipeline.case_assembly import Case, assemble_cases
from pipeline.instantiator import CandidateJournalEntry, instantiate_cases
from pipeline.matcher import match_cases, match_tier_distribution
from pipeline.predicates import CaseEvidence, evaluate_cases
from pipeline.schemas import BankLine, LedgerEntry, ReconLine, Settlement

KNOWN_GAPS: tuple[str, ...] = (
    "UNMATCHED_INBOUND_CREDIT is not assigned: §4.2 puts 'does this narration identify a "
    "counterparty?' in Slot A, the one graded LLM slot (session 5.2, with session 5.1's "
    "keyword baseline as its comparator). The 8 orphan cases carrying it therefore fire no "
    "subtype trigger, and terminate in ABSTAINED rather than EXTERNAL_ACTION_REQUIRED.",
)
"""What this pipeline does not yet decide, and who owns each gap.

Stated in code, beside the run that exhibits it, so the shortfall shows
up as a named limitation rather than as an unexplained dent in
`exception_subtype_recall`.
"""


class RunResult(BaseModel):
    """Everything one pass produced, for the eval harness and the FR-11 report."""

    model_config = ConfigDict(frozen=True)

    cases: tuple[Case, ...]
    evidences: tuple[CaseEvidence, ...]
    candidates: tuple[CandidateJournalEntry, ...]
    outcome: BatchOutcome

    def match_tier_distribution(self) -> dict[int, int]:
        return match_tier_distribution(self.cases)

    def state_distribution(self) -> dict[str, int]:
        return self.outcome.state_distribution()


def run_batch(
    conn: sqlite3.Connection,
    *,
    settlements: Sequence[Settlement],
    recon_lines: Sequence[ReconLine],
    bank_lines: Sequence[BankLine],
    ledger_entries: Sequence[LedgerEntry],
    snapshot_date: date,
    seed_ledger_first: bool = True,
) -> RunResult:
    """Components 2-8 over one batch, against the ledger held in `conn`.

    `snapshot_date` is a parameter and never a wall-clock read: it decides
    the T+2 settlement window (§3.3), and a batch reprocessed tomorrow
    must reconcile identically to one processed today. It doubles as the
    posting date of every correcting entry, which is what makes a rerun
    byte-identical.

    `seed_ledger_first=False` runs against a ledger already loaded — the
    second pass of the idempotency check, which must find the first pass's
    adjustments in place.
    """
    if seed_ledger_first:
        seed_ledger(conn, ledger_entries)

    cases = match_cases(
        assemble_cases(settlements, recon_lines, bank_lines),
        bank_lines,
        snapshot_date=snapshot_date,
    )
    evidences = evaluate_cases(cases, ledger_entries)
    candidates = instantiate_cases(evidences, cases, ledger_entries)
    outcome = apply_batch(conn, cases, evidences, candidates, posting_date=snapshot_date)

    return RunResult(
        cases=tuple(cases),
        evidences=tuple(evidences),
        candidates=tuple(candidates),
        outcome=outcome,
    )
