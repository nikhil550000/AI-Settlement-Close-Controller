"""One end-to-end pass of components 1-8 over a batch.

**Not a new component.** The pipeline fixes ten components and one direction of
data flow with no cycles; this module is that flow, made executable, and
adds no logic of its own beyond calling each stage in order and handing
one stage's output to the next. The CLI surface calls this rather than
re-deriving the wiring — and "first end-to-end run producing all five terminal
states" needs it to exist to be checkable at all.

The graded path only. `pipeline/` never imports `generator/`, so
`run_batch` takes already-loaded records; where they came from — the
committed reference dataset, a raw bank export through the bank-statement
adapter, or the generator in a test — is the caller's business.

Component 5 (classifier) is threaded through, but only
as a caller-supplied function: it fills the one graded LLM slot,
and both arms it can take (the keyword baseline, or
Slot A) are equally valid callers, so `run_batch` does not hard-code
either. Passing no classifier reproduces the earlier run exactly —
`UNMATCHED_INBOUND_CREDIT` unassigned, those 8 orphan cases in
`ABSTAINED` rather than `EXTERNAL_ACTION_REQUIRED` — which is what
`KNOWN_GAPS` still describes when that is how a caller runs it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from datetime import date

from pydantic import BaseModel, ConfigDict

from pipeline.apply import BatchOutcome, CaseOutcome, apply_batch, seed_ledger
from pipeline.attachment import AttachmentAudit, audit_attachments
from pipeline.bank_accounting import BankLineAccounting, account_bank_lines
from pipeline.case_assembly import Case, assemble_cases
from pipeline.classifier import ClassificationResult, EvidenceBundle, build_evidence_bundles
from pipeline.instantiator import CandidateJournalEntry, instantiate_cases
from pipeline.matcher import match_cases, match_tier_distribution
from pipeline.predicates import CaseEvidence, evaluate_cases
from pipeline.schemas import BankLine, LedgerEntry, ReconLine, Settlement
from pipeline.semantics import KEYWORD, NarrationSemantics

KNOWN_GAPS: tuple[str, ...] = (
    "UNMATCHED_INBOUND_CREDIT is unassigned only when run_batch() is called with no "
    "classifier: 'does this narration identify a counterparty?' is Slot A's question, "
    "the one graded LLM slot, with the keyword baseline as its comparator. "
    "Passing either as `classifier=` closes this gap — the 8 affected "
    "orphan cases then terminate in EXTERNAL_ACTION_REQUIRED, matching ground truth.",
)
"""What this pipeline does not decide without a classifier, and who owns closing it.

Stated in code, beside the run that can exhibit it, so the shortfall shows
up as a named limitation rather than as an unexplained dent in
`exception_subtype_recall`.
"""


def _carry_forward_validations(first: BatchOutcome, second: BatchOutcome) -> BatchOutcome:
    """Keep the audit trail from the pass that actually posted.

    `run_batch` applies twice when a classifier is given, and idempotent
    controller adjustments make the second pass *replay* rather than repost. That is correct
    behaviour, but it costs the report its evidence: `apply_case`'s replay
    short-circuit emits a `ValidationReport` carrying the single check
    `NOT_PREVIOUSLY_POSTED` and never runs `validate_candidate` again, so
    every `AUTO_CLOSED` case in the rendered artifact read

        PASS not_previously_posted: already posted identically by a previous
        run; replayed, not re-posted

    and showed none of `entry_balanced`, `post_adjustment_residual_zero`,
    `account_direction_permitted`, `template_allowlisted` or
    `cited_records_exist`. There was no previous run — it was this same
    command's internal second pass — so the audit trail that must show
    "the specific safety validations passed" was showing one check, and the
    one least able to support the claim.

    The checks that actually gated the posting are the first pass's. This
    substitutes them back wherever the second pass produced nothing but the
    replay marker, leaving every other field of the second pass's outcome
    (state, classified subtype, exception class) untouched — those are the
    fields the second pass exists to compute.
    """
    first_by_case = {outcome.case_id: outcome.validations for outcome in first.outcomes}
    merged: list[CaseOutcome] = []
    for outcome in second.outcomes:
        original = first_by_case.get(outcome.case_id, ())
        if outcome.replayed_entries and original:
            outcome = outcome.model_copy(update={"validations": original})
        merged.append(outcome)
    return second.model_copy(update={"outcomes": tuple(merged)})


class RunResult(BaseModel):
    """Everything one pass produced, for the eval harness and the report."""

    model_config = ConfigDict(frozen=True)

    cases: tuple[Case, ...]
    evidences: tuple[CaseEvidence, ...]
    candidates: tuple[CandidateJournalEntry, ...]
    outcome: BatchOutcome
    classifications: tuple[ClassificationResult, ...] = ()
    """Component 5's per-case output, when `run_batch` was given a `classifier`.
    Empty when it was not — the same "no classifier, no change" rule `apply_batch`
    follows for its own `classifications` parameter."""
    attachment: AttachmentAudit
    """What each attached credit rests on (`pipeline.attachment`). Computed
    alongside the partition and for the same reason: this is the one place
    holding both the matched cases and the settlements they were matched
    against, and the audit is only meaningful if it reads the attachments the
    run actually made rather than a second pass that could disagree."""

    bank_accounting: BankLineAccounting
    """Where every bank line went (`pipeline.bank_accounting`). Computed here
    because this is the one place that holds both the matched cases and the raw
    `bank_lines` they were drawn from — the partition needs both, and asking a
    later component to re-load the statement to check it would be the second
    copy of a rule this repository single-sources everywhere else."""

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
    semantics: NarrationSemantics = KEYWORD,
) -> RunResult:
    """Components 2-8 over one batch, against the ledger held in `conn`.

    `snapshot_date` is a parameter and never a wall-clock read: it decides
    the T+2 settlement window, and a batch reprocessed tomorrow
    must reconcile identically to one processed today. It doubles as the
    posting date of every correcting entry, which is what makes a rerun
    byte-identical.

    `seed_ledger_first=False` runs against a ledger already loaded — the
    second pass of the idempotency check, which must find the first pass's
    adjustments in place.

    `classifier`, when given, is one call over component 5's evidence
    bundles — `pipeline.classifier.classify_batch_baseline`, a
    `pipeline.classifier.classify_batch_llm` partial, or a stub in a test.
    Slot A's criterion for what it sees ("non-`AUTO_CLOSED` cases") is
    stated in terms of a *terminal state*, which only exists after
    component 8 runs once — so this function runs `apply_batch` **twice**
    when a classifier is supplied: once to find the non-auto-close cases,
    once more with the classifier's output threaded in. The first pass's
    `AUTO_CLOSED` writes are idempotent, so the
    second pass recognises and replays them rather than reposting —
    exactly the mechanism `apply_case`'s replay path already exists for.
    """
    if seed_ledger_first:
        seed_ledger(conn, ledger_entries)

    cases = match_cases(
        assemble_cases(settlements, recon_lines, bank_lines, semantics=semantics),
        bank_lines,
        snapshot_date=snapshot_date,
        semantics=semantics,
    )
    evidences = evaluate_cases(cases, ledger_entries, semantics=semantics)
    candidates = instantiate_cases(evidences, cases, ledger_entries)
    outcome = apply_batch(conn, cases, evidences, candidates, posting_date=snapshot_date, semantics=semantics)

    classifications: tuple[ClassificationResult, ...] = ()
    if classifier is not None:
        bundles = build_evidence_bundles(cases, evidences, outcome.outcomes)
        classifications = tuple(classifier(bundles))
        by_case_id = {result.case_id: result.subtype for result in classifications}
        second_pass = apply_batch(
            conn,
            cases,
            evidences,
            candidates,
            posting_date=snapshot_date,
            classifications=by_case_id,
            semantics=semantics,
        )
        outcome = _carry_forward_validations(outcome, second_pass)

    return RunResult(
        cases=tuple(cases),
        evidences=tuple(evidences),
        candidates=tuple(candidates),
        outcome=outcome,
        classifications=classifications,
        bank_accounting=account_bank_lines(bank_lines, cases, semantics=semantics),
        attachment=audit_attachments(cases, settlements),
    )
