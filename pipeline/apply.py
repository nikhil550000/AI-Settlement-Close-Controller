"""Apply and re-reconcile, per spec.md §4.1 component 8.

> **Apply and re-reconcile.** Ledger write under the 1.7.4 idempotency
> constraint, residual recheck, terminal state assignment.

Three jobs, in that order, and the third is the one that produces §1.3's
five terminal states.

**The write is transactional, because §1.3 and §1.7.5 ask for different
things and both are satisfiable at once.** §1.3 defines `AUTO_CLOSED` as
an entry that "was ... applied to the synthetic ledger, and reconciliation
was re-run confirming the post-adjustment residual is 0 paise" — apply
first, then check. §1.7.5 requires that "any failed safety validation
prevents auto-action", and the residual is one of its validations — so a
failure must leave nothing behind. A transaction gives both: the legs are
written, the ledger is re-reconciled *against the written rows*, and the
transaction is committed only if the residual is 0. Otherwise it is rolled
back and the case is declined with nothing posted. The residual is
measured on the real post-write state, not on a projection of it.

**Reprocessing is idempotent in outcome, not only in writes.** §6.3's
checkpoint for this session is "running the same batch twice posts nothing
on the second pass", and invariant 1.7.4 is what guarantees it. A second
run reconstructs the identical `resolution_id` for each `(case_id,
template_id)` (§3.4), finds the same legs already in the ledger, and
recognises the case as *already applied*: it posts nothing and reports
`AUTO_CLOSED` again. Treating a prior identical posting as a validation
failure instead would satisfy the letter of "posts nothing" while making
the second run report different states from the first, which is not
idempotence in any useful sense. A prior posting under the same key whose
legs *differ* is not a replay — it is a real integrity problem, and the
case is declined.

**Terminal state assignment is deterministic and lives here**, not in the
classifier: §4.1 gives component 5 "exception class and subtype
assignment" and gives component 8 "terminal state assignment". The rules
are §3.3's, restated as code in `assign_state`.

**Correction outranks exception, and §3.3 says so.** Seven family-4 cases
fire both a `T-04` template hit and the `BANK_CREDIT_OVERDUE` subtype
trigger (session 4.1 measured this and left it for a downstream component
to resolve). §3.3 defines `OPERATIONAL_EXCEPTION` as "a real discrepancy
that **no journal entry can resolve**" — so a discrepancy a journal entry
*does* resolve, evidenced by the residual reaching 0, is not one. The
precedence is read off the class definition rather than invented as a tie
-break.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date

from pydantic import BaseModel, ConfigDict

from pipeline.case_assembly import Case, CaseKind
from pipeline.exception_class import is_timing_attributed, predict_exception_class
from pipeline.ground_truth import (
    DeclineReason,
    ExceptionClass,
    ExceptionSubtype,
    OutcomeState,
)
from pipeline.instantiator import CandidateJournalEntry
from pipeline.policy import PolicyDecision, evaluate_policy
from pipeline.semantics import KEYWORD, NarrationSemantics
from pipeline.predicates import CaseEvidence
from pipeline.reconciliation import case_residual_paise
from pipeline.schemas import LedgerEntry, LedgerSource
from pipeline.storage import fetch_ledger_entries, insert_ledger_entries
from pipeline.subtype_label import SubtypeLabel
from pipeline.validator import (
    CheckResult,
    ValidationCheck,
    ValidationReport,
    batch_record_ids,
    resolution_id_for,
    validate_candidate,
)

APPLIED_NARRATION = "Controller adjustment"
"""The narration on every posted leg.

A fixed string, not derived text. Invariant 1.7.2 forbids the model from
originating a narration on the automated path, and §4.2 puts all
model-written prose in Slot B, "ungraded, off the money path" — so the
narration a posted row carries is a constant, and the per-case English
lives in the report beside it, labelled as model-generated.
"""


class CaseOutcome(BaseModel):
    """One case's terminal state and the full record of how it got there (§1.8 artifacts 2 and 5)."""

    model_config = ConfigDict(frozen=True)

    case_id: str
    state: OutcomeState
    decline_reason: DeclineReason | None = None

    applied_entries: tuple[CandidateJournalEntry, ...] = ()
    """Entries actually posted to the ledger by this run."""

    replayed_entries: tuple[CandidateJournalEntry, ...] = ()
    """Entries already posted by a previous run, recognised and not re-posted (§1.7.4)."""

    proposed_entries: tuple[CandidateJournalEntry, ...] = ()
    """FR-07: machine-readable, unapplied, carrying `decline_reason`."""

    policy_decisions: tuple[PolicyDecision, ...] = ()
    validations: tuple[ValidationReport, ...] = ()

    triggered_subtypes: tuple[ExceptionSubtype, ...] = ()
    """§3.3 subtype triggers that fired — evidence, not yet a classification (component 5)."""

    classified_subtype: SubtypeLabel | None = None
    """Component 5's assigned label for this case, when a classifier ran (session 5.2).
    Recorded for every case a classifier saw, whether or not it changed `state` — only
    `UNMATCHED_INBOUND_CREDIT` does (see `assign_state`); this field is the audit trail
    for what component 5 said regardless."""

    exception_class: ExceptionClass | None = None
    """§3.3's class for this case — the second of the two labels §3.3 says every case
    carries independently (session 6.2, `pipeline.exception_class`).

    Filled by `apply_batch`, which holds both the `Case` and its finished `CaseOutcome`;
    `apply_case` leaves it `None` because the timing attribution it needs is a property
    of the case, not of the posting. A `CaseOutcome` built by hand in a test therefore
    carries `None`, and §5.2's class confusion matrix rejects that rather than reading
    it as `NONE` — an unassigned class and the `NONE` sentinel are different facts.

    It classifies; it decides nothing. No component reads it, it gates no posting, and
    `assign_state` neither takes nor returns it."""

    residual_paise: int = 0
    """The books-versus-evidence residual after this run (§1.7.5, `pipeline.reconciliation`)."""

    @property
    def posted_leg_count(self) -> int:
        return sum(len(entry.legs) for entry in self.applied_entries)


class BatchOutcome(BaseModel):
    """Every case's outcome for one run, plus what the run wrote."""

    model_config = ConfigDict(frozen=True)

    outcomes: tuple[CaseOutcome, ...]
    posted_leg_count: int

    def state_distribution(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for outcome in self.outcomes:
            counts[str(outcome.state)] = counts.get(str(outcome.state), 0) + 1
        return counts

    def decline_reason_distribution(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for outcome in self.outcomes:
            if outcome.decline_reason is not None:
                counts[str(outcome.decline_reason)] = counts.get(str(outcome.decline_reason), 0) + 1
        return counts

    def by_case_id(self) -> dict[str, CaseOutcome]:
        return {outcome.case_id: outcome for outcome in self.outcomes}


def seed_ledger(conn: sqlite3.Connection, entries: Sequence[LedgerEntry]) -> None:
    """Load the merchant's own bookkeeping into the ledger the Controller will correct.

    The generator emits `ledger_entry` records as JSONL; §4.5 puts the
    mutable ledger in SQLite. This is the one-time load between them, and
    it is separate from `apply_batch` so a run can be pointed at a ledger
    that already carries a previous run's adjustments — which is exactly
    what the second-pass idempotency check does.
    """
    insert_ledger_entries(conn, entries)


class LedgerState:
    """The ledger's indexes, built once per run and kept current as legs are posted.

    Every component downstream of the write asks one of four questions of
    the ledger — what was posted against this record, against this case,
    as a controller adjustment against this record, and under which
    `(case_id, resolution_id)` pairs — and re-reading ~5,800 rows out of
    SQLite for each of 150 cases to answer them is 150 times the work for
    the same answer. The rows a case posts are appended here on commit, so
    a later case sees them exactly as a re-read would.
    """

    def __init__(self, entries: Sequence[LedgerEntry]) -> None:
        self.entries: list[LedgerEntry] = list(entries)
        self.by_reference: dict[str, list[LedgerEntry]] = {}
        self.by_case: dict[str, list[LedgerEntry]] = {}
        self.adjustments_by_reference: dict[str, list[LedgerEntry]] = {}
        self.posted_pairs: set[tuple[str, str]] = set()
        for entry in self.entries:
            self._index(entry)

    def _index(self, entry: LedgerEntry) -> None:
        self.by_reference.setdefault(entry.reference, []).append(entry)
        if entry.case_id is not None:
            self.by_case.setdefault(entry.case_id, []).append(entry)
            if entry.resolution_id is not None:
                self.posted_pairs.add((entry.case_id, entry.resolution_id))
        if entry.source is LedgerSource.CONTROLLER_ADJUSTMENT:
            self.adjustments_by_reference.setdefault(entry.reference, []).append(entry)

    def add(self, entries: Sequence[LedgerEntry]) -> None:
        for entry in entries:
            self.entries.append(entry)
            self._index(entry)

    def remove(self, entries: Sequence[LedgerEntry]) -> None:
        """Undo `add`, so the indexes mirror a rolled-back transaction.

        Called only on the residual-check failure path: the rows were
        written and re-reconciled against, the residual was not 0, and
        SQLite discarded them — the in-memory view has to discard them too
        or every later case would reconcile against rows that do not
        exist.
        """
        discarded = {entry.journal_entry_id for entry in entries}
        self.entries = [entry for entry in self.entries if entry.journal_entry_id not in discarded]
        for index in (self.by_reference, self.by_case, self.adjustments_by_reference):
            for key, group in list(index.items()):
                kept = [entry for entry in group if entry.journal_entry_id not in discarded]
                if kept:
                    index[key] = kept
                else:
                    del index[key]
        self.posted_pairs = {
            (entry.case_id, entry.resolution_id)
            for entry in self.entries
            if entry.case_id is not None and entry.resolution_id is not None
        }

    def posted_legs_for(self, case_id: str, resolution_id: str) -> dict[str, tuple[int, int]]:
        """The `(debit, credit)` already posted per account under one `(case_id, resolution_id)`."""
        return {
            entry.account_code: (int(entry.debit), int(entry.credit))
            for entry in self.by_case.get(case_id, ())
            if entry.resolution_id == resolution_id
        }


def _is_identical_replay(candidate: CandidateJournalEntry, posted: Mapping[str, tuple[int, int]]) -> bool:
    """Whether an existing posting under this key is leg-for-leg the same entry."""
    if len(posted) != len(candidate.legs):
        return False
    return all(posted.get(leg.account_code) == (leg.debit, leg.credit) for leg in candidate.legs)


def _ledger_rows(
    candidate: CandidateJournalEntry,
    resolution_id: str,
    *,
    posting_date: date,
) -> list[LedgerEntry]:
    """One `LedgerEntry` per leg, per §3.1's per-leg schema.

    Every leg of one entry shares its `(case_id, resolution_id)` and is
    separated by `account_code` — REV-24's uniqueness triple. `reference`
    names the first cited source record, which is the recon line the
    predicate fired on; the authoritative case link is the `case_id`
    column, so an aggregate over several records is still fully attributed
    (§3.4's "every contributing record ID cited in the audit trail" lives
    on the candidate, which the audit trail carries).

    Only ever called on a candidate that has passed validation, so
    `cited_record_ids` is non-empty: `CITED_RECORDS_EXIST` rejects an
    entry citing nothing (§1.7.3).
    """
    reference = candidate.cited_record_ids[0]
    return [
        LedgerEntry(
            journal_entry_id=f"je_{candidate.case_id}_{resolution_id}_{leg.account_code}",
            date=posting_date,
            account_code=leg.account_code,
            account_name=leg.account_name,
            debit=leg.debit,
            credit=leg.credit,
            reference=reference,
            narration=APPLIED_NARRATION,
            source=LedgerSource.CONTROLLER_ADJUSTMENT,
            resolution_id=resolution_id,
            case_id=candidate.case_id,
        )
        for leg in candidate.legs
    ]


def assign_state(
    *,
    has_candidates: bool,
    applied_or_replayed: bool,
    declined_by_policy: bool,
    triggered_subtypes: Sequence[ExceptionSubtype],
    residual_paise: int,
    classified_unmatched_inbound_credit: bool = False,
) -> tuple[OutcomeState, DeclineReason | None]:
    """§1.3's five terminal states, assigned from evidence (§3.3's population mapping).

    `classified_unmatched_inbound_credit` is component 5's one contribution to state
    routing (session 5.2's interface decision, deferred by session 5.1): of the eight
    labels a classifier can assign, seven are adopted from a trigger component 4
    already fired (branch 4 already sees those via `triggered_subtypes`), and
    `AMBIGUOUS_CASE` changes nothing (branch 6 is already its fallthrough).
    `UNMATCHED_INBOUND_CREDIT` is the one label with no deterministic trigger behind
    it — "turns entirely on whether the free-text narration identifies a
    counterparty" (§4.2) — so it is the only classification that can move a case out
    of branch 6 and into branch 4. Default `False` so every pre-5.2 caller (and every
    case a classifier never saw) is unaffected.

    The order of the branches is the precedence, and each is §3.3's:

    1. A **policy exclusion** (§2.5) routes to `REVIEW_REQUIRED` with
       `decline_reason = policy`, before anything else is considered —
       "regardless of model confidence", and regardless of the fact that
       the entry would have validated.
    2. A **correction that landed** is `AUTO_CLOSED` (§1.3): instantiated
       from an allowlisted template, validated, applied, and re-reconciled
       to 0 paise. This outranks any subtype trigger the case also fired,
       because §3.3 defines `OPERATIONAL_EXCEPTION` as a discrepancy no
       journal entry can resolve.
    3. A **correction that did not land** is `REVIEW_REQUIRED` with
       `decline_reason = confidence`: a candidate existed but failed the
       1.7.5 chain. §1.6 fixes this reading — the only ground-truth
       `REVIEW_REQUIRED` population is policy, and confidence declines
       "appear instead as cases whose ground truth is `AUTO_CLOSED` that
       the system declined".
    4. A fired **subtype trigger** — or a classifier-assigned
       `UNMATCHED_INBOUND_CREDIT`, see above — with no correction is
       `EXTERNAL_ACTION_REQUIRED` (§3.3: `OPERATIONAL_EXCEPTION`'s terminal
       state).
    5. A **zero residual** with nothing to correct and nothing to escalate
       is `AUTO_MATCHED` — §1.3's "reconciles cleanly with no accounting
       action required", covering both the fully-clean population and
       §3.3's `EXPECTED_TIMING_DIFFERENCE`, whose residual the matcher's
       timing rule already zeroed.
    6. Anything left is `ABSTAINED`: a non-zero residual that no template
       explains and no trigger categorises is §3.3's `AMBIGUOUS_CASE` —
       "a required piece of evidence is absent", and §1.3's "no defensible
       candidate can be recommended". This is the fallthrough §2.3 calls
       for, reached on evidence rather than by accident.
    """
    if declined_by_policy:
        return OutcomeState.REVIEW_REQUIRED, DeclineReason.POLICY
    if applied_or_replayed:
        return OutcomeState.AUTO_CLOSED, None
    if has_candidates:
        return OutcomeState.REVIEW_REQUIRED, DeclineReason.CONFIDENCE
    if triggered_subtypes or classified_unmatched_inbound_credit:
        return OutcomeState.EXTERNAL_ACTION_REQUIRED, None
    if residual_paise == 0:
        return OutcomeState.AUTO_MATCHED, None
    return OutcomeState.ABSTAINED, None


def apply_case(
    conn: sqlite3.Connection,
    state: LedgerState,
    case: Case,
    evidence: CaseEvidence,
    candidates: Sequence[CandidateJournalEntry],
    *,
    posting_date: date,
    known_record_ids: frozenset[str],
    classification: SubtypeLabel | None = None,
    semantics: NarrationSemantics = KEYWORD,
) -> CaseOutcome:
    """Validate, apply, re-reconcile and assign a terminal state for one case.

    `state` carries the ledger as it stands, including anything a previous
    run posted, so the idempotency checks see prior adjustments.

    `classification` is component 5's label for this case, when a classifier
    ran (session 5.2) — `None` on a first pass, or for any case a classifier
    never saw. See `assign_state` for the one label (`UNMATCHED_INBOUND_CREDIT`)
    that can change the outcome; every other label is recorded on
    `CaseOutcome.classified_subtype` without affecting `state`.
    """
    policy_decisions = evaluate_policy(case, evidence, state.by_reference, semantics=semantics)
    triggered = tuple(trigger.subtype for trigger in evidence.subtype_triggers)
    classified_unmatched_inbound_credit = classification is SubtypeLabel.UNMATCHED_INBOUND_CREDIT

    def residual() -> int:
        return case_residual_paise(case, state.by_reference, state.by_case)

    if not candidates:
        current = residual()
        outcome_state, reason = assign_state(
            has_candidates=False,
            applied_or_replayed=False,
            declined_by_policy=bool(policy_decisions),
            triggered_subtypes=triggered,
            classified_unmatched_inbound_credit=classified_unmatched_inbound_credit,
            residual_paise=current,
        )
        return CaseOutcome(
            case_id=case.case_id,
            state=outcome_state,
            decline_reason=reason,
            policy_decisions=policy_decisions,
            triggered_subtypes=triggered,
            classified_subtype=classification,
            residual_paise=current,
        )

    if policy_decisions:
        # §2.5: detected and classified, never auto-posted. FR-07 requires the
        # proposed entry travel with the case in the same schema as an applied one.
        return CaseOutcome(
            case_id=case.case_id,
            state=OutcomeState.REVIEW_REQUIRED,
            decline_reason=DeclineReason.POLICY,
            proposed_entries=tuple(candidates),
            policy_decisions=policy_decisions,
            triggered_subtypes=triggered,
            classified_subtype=classification,
            residual_paise=residual(),
        )

    replayed: list[CandidateJournalEntry] = []
    to_post: list[CandidateJournalEntry] = []
    reports: list[ValidationReport] = []

    for candidate in candidates:
        resolution_id = resolution_id_for(candidate.template_id)
        already = state.posted_legs_for(candidate.case_id, resolution_id)
        if already:
            if _is_identical_replay(candidate, already):
                # Invariant 1.7.4: reprocessing cannot double-post. The prior
                # posting *is* this correction, so the case stays AUTO_CLOSED
                # and nothing is written.
                replayed.append(candidate)
                reports.append(
                    ValidationReport(
                        case_id=candidate.case_id,
                        template_id=candidate.template_id,
                        resolution_id=resolution_id,
                        results=(
                            CheckResult(
                                check=ValidationCheck.NOT_PREVIOUSLY_POSTED,
                                passed=True,
                                detail="already posted identically by a previous run; replayed, not re-posted",
                            ),
                        ),
                    )
                )
                continue
            # Same key, different legs. Not a replay — an integrity problem.
            reports.append(
                ValidationReport(
                    case_id=candidate.case_id,
                    template_id=candidate.template_id,
                    resolution_id=resolution_id,
                    results=(
                        CheckResult(
                            check=ValidationCheck.NOT_PREVIOUSLY_POSTED,
                            passed=False,
                            detail=f"{(candidate.case_id, resolution_id)} is posted with different legs",
                        ),
                    ),
                )
            )
            continue

        report = validate_candidate(
            candidate,
            known_record_ids=known_record_ids,
            adjustments_by_reference=state.adjustments_by_reference,
            posted_pairs=frozenset(state.posted_pairs),
        )
        reports.append(report)
        if report.passed:
            to_post.append(candidate)

    declined = [
        candidate
        for candidate in candidates
        if candidate not in to_post and candidate not in replayed
    ]

    # A case closes all-or-nothing. §3.4 permits several templates on one case,
    # and §1.7.5's residual check is a statement about the case's books, not
    # about one entry — posting the entries that validated while declining the
    # rest would leave the books in a state neither `AUTO_CLOSED` nor the
    # original, and the residual could not reach 0 either way.
    if declined or not (to_post or replayed):
        return CaseOutcome(
            case_id=case.case_id,
            state=OutcomeState.REVIEW_REQUIRED,
            decline_reason=DeclineReason.CONFIDENCE,
            proposed_entries=tuple(candidates),
            validations=tuple(reports),
            triggered_subtypes=triggered,
            classified_subtype=classification,
            residual_paise=residual(),
        )

    applied: tuple[CandidateJournalEntry, ...] = ()
    if to_post:
        rows: list[LedgerEntry] = []
        for candidate in to_post:
            rows += _ledger_rows(candidate, resolution_id_for(candidate.template_id), posting_date=posting_date)

        # §1.3 applies first and re-reconciles against what was written; §1.7.5
        # requires a failure to leave nothing behind. The transaction is both.
        insert_ledger_entries(conn, rows, commit=False)
        state.add(rows)

        post_residual = residual()
        if post_residual != 0:
            conn.rollback()
            state.remove(rows)
            return CaseOutcome(
                case_id=case.case_id,
                state=OutcomeState.REVIEW_REQUIRED,
                decline_reason=DeclineReason.CONFIDENCE,
                proposed_entries=tuple(candidates),
                validations=tuple(
                    reports
                    + [
                        ValidationReport(
                            case_id=case.case_id,
                            template_id=to_post[0].template_id,
                            resolution_id=resolution_id_for(to_post[0].template_id),
                            results=(
                                CheckResult(
                                    check=ValidationCheck.RESIDUAL_ZERO,
                                    passed=False,
                                    detail=f"post-adjustment residual is {post_residual} paise, not 0; rolled back",
                                ),
                            ),
                        )
                    ]
                ),
                triggered_subtypes=triggered,
                classified_subtype=classification,
                residual_paise=residual(),
            )
        conn.commit()
        applied = tuple(to_post)
        reports.append(
            ValidationReport(
                case_id=case.case_id,
                template_id=to_post[0].template_id,
                resolution_id=resolution_id_for(to_post[0].template_id),
                results=(
                    CheckResult(
                        check=ValidationCheck.RESIDUAL_ZERO,
                        passed=True,
                        detail="0 paise on re-reconciliation against the written rows",
                    ),
                ),
            )
        )

    return CaseOutcome(
        case_id=case.case_id,
        state=OutcomeState.AUTO_CLOSED,
        applied_entries=applied,
        replayed_entries=tuple(replayed),
        validations=tuple(reports),
        triggered_subtypes=triggered,
        classified_subtype=classification,
        residual_paise=residual(),
    )


def _exception_class(case: Case, outcome: CaseOutcome) -> ExceptionClass:
    """§3.3's class for a finished case (session 6.2, `pipeline.exception_class`).

    Assigned here rather than inside `apply_case` because the timing
    attribution is a property of the `Case` — the matcher's own reason for
    zeroing the residual — and `apply_case` reasons about the posting. The
    evidence handed over is the evidence `assign_state` saw, read back off the
    outcome it produced, plus that one field from the case.
    """
    return predict_exception_class(
        declined_by_policy=outcome.decline_reason is DeclineReason.POLICY,
        has_entries=bool(
            outcome.applied_entries or outcome.replayed_entries or outcome.proposed_entries
        ),
        triggered_subtypes=outcome.triggered_subtypes,
        classified_subtype=outcome.classified_subtype,
        timing_attributed=is_timing_attributed(case),
        residual_paise=outcome.residual_paise,
    )


def apply_batch(
    conn: sqlite3.Connection,
    cases: Sequence[Case],
    evidences: Sequence[CaseEvidence],
    candidates: Sequence[CandidateJournalEntry],
    *,
    posting_date: date,
    classifications: Mapping[str, SubtypeLabel] | None = None,
    semantics: NarrationSemantics = KEYWORD,
) -> BatchOutcome:
    """Run component 8 over a whole batch. `conn` must already hold the merchant ledger.

    `classifications` is component 5's output, keyed by `case_id` (session 5.2) —
    `None` (the default) reproduces every pre-5.2 call exactly. `pipeline.run.run_batch`
    calls this twice when a classifier is supplied: once with `classifications=None` to
    find the non-auto-close cases a classifier needs to see, and again with its output,
    so the interface here stays a plain lookup rather than `apply_batch` knowing how to
    run a classifier itself.
    """
    evidence_by_case = {evidence.case_id: evidence for evidence in evidences}
    candidates_by_case: dict[str, list[CandidateJournalEntry]] = {}
    for candidate in candidates:
        candidates_by_case.setdefault(candidate.case_id, []).append(candidate)

    classifications = classifications or {}
    state = LedgerState(fetch_ledger_entries(conn))
    known_record_ids = batch_record_ids(cases, state.entries)

    outcomes: list[CaseOutcome] = []
    for case in cases:
        evidence = evidence_by_case.get(case.case_id) or CaseEvidence(case_id=case.case_id)
        outcome = apply_case(
            conn,
            state,
            case,
            evidence,
            candidates_by_case.get(case.case_id, ()),
            posting_date=posting_date,
            known_record_ids=known_record_ids,
            classification=classifications.get(case.case_id),
            semantics=semantics,
        )
        outcomes.append(outcome.model_copy(update={"exception_class": _exception_class(case, outcome)}))

    return BatchOutcome(
        outcomes=tuple(outcomes),
        posted_leg_count=sum(outcome.posted_leg_count for outcome in outcomes),
    )


def orphan_cases_never_post(outcomes: Sequence[CaseOutcome], cases: Sequence[Case]) -> bool:
    """No orphan case may carry a posted entry — no §3.4 template addresses one (§3.6)."""
    orphan_ids = {case.case_id for case in cases if case.kind is CaseKind.ORPHAN}
    return not any(
        outcome.applied_entries or outcome.replayed_entries
        for outcome in outcomes
        if outcome.case_id in orphan_ids
    )
