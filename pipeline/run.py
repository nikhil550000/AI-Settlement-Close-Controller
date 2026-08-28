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

Component 5 (classifier) is threaded through as of session 5.2, but only
as a caller-supplied function: §4.2 assigns it the one graded LLM slot,
and both arms it can take (session 5.1's keyword baseline, session 5.2's
Slot A) are equally valid callers, so `run_batch` does not hard-code
either. Passing no classifier reproduces the pre-5.2 run exactly —
`UNMATCHED_INBOUND_CREDIT` unassigned, those 8 orphan cases in
`ABSTAINED` rather than `EXTERNAL_ACTION_REQUIRED` — which is what
`KNOWN_GAPS` still describes when that is how a caller runs it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from datetime import date

from pydantic import BaseModel, ConfigDict

from pipeline.apply import BatchOutcome, apply_batch, seed_ledger
from pipeline.case_assembly import Case, assemble_cases
from pipeline.classifier import ClassificationResult, EvidenceBundle, build_evidence_bundles
from pipeline.instantiator import CandidateJournalEntry, instantiate_cases
from pipeline.matcher import match_cases, match_tier_distribution
from pipeline.predicates import CaseEvidence, evaluate_cases
from pipeline.schemas import BankLine, LedgerEntry, ReconLine, Settlement

KNOWN_GAPS: tuple[str, ...] = (
    "UNMATCHED_INBOUND_CREDIT is unassigned only when run_batch() is called with no "
    "classifier: §4.2 puts 'does this narration identify a counterparty?' in Slot A, "
    "the one graded LLM slot, with session 5.1's keyword baseline as its comparator. "
    "Passing either as `classifier=` closes this gap (session 5.2) — the 8 affected "
    "orphan cases then terminate in EXTERNAL_ACTION_REQUIRED, matching ground truth.",
)
"""What this pipeline does not decide without a classifier, and who owns closing it.

Stated in code, beside the run that can exhibit it, so the shortfall shows
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
    classifications: tuple[ClassificationResult, ...] = ()
    """Component 5's per-case output, when `run_batch` was given a `classifier`.
    Empty when it was not — the same "no classifier, no change" rule `apply_batch`
    follows for its own `classifications` parameter."""

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
    classifier: Callable[[Sequence[EvidenceBundle]], Sequence[ClassificationResult]] | None = None,
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

    `classifier`, when given, is one call over component 5's evidence
    bundles — `pipeline.classifier.classify_batch_baseline`, a
    `pipeline.classifier.classify_batch_llm` partial, or a stub in a test.
    §4.2's criterion for what Slot A sees ("non-`AUTO_CLOSED` cases") is
    stated in terms of a *terminal state*, which only exists after
    component 8 runs once — so this function runs `apply_batch` **twice**
    when a classifier is supplied: once to find the non-auto-close cases,
    once more with the classifier's output threaded in. The first pass's
    `AUTO_CLOSED` writes are idempotent under invariant 1.7.4, so the
    second pass recognises and replays them rather than reposting —
    exactly the mechanism `apply_case`'s replay path already exists for.
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

    classifications: tuple[ClassificationResult, ...] = ()
    if classifier is not None:
        bundles = build_evidence_bundles(cases, evidences, outcome.outcomes)
        classifications = tuple(classifier(bundles))
        by_case_id = {result.case_id: result.subtype for result in classifications}
        outcome = apply_batch(
            conn,
            cases,
            evidences,
            candidates,
            posting_date=snapshot_date,
            classifications=by_case_id,
        )

    return RunResult(
        cases=tuple(cases),
        evidences=tuple(evidences),
        candidates=tuple(candidates),
        outcome=outcome,
        classifications=classifications,
    )
