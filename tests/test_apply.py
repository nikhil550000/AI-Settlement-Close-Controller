"""Session 4.3's checkpoint (spec.md §6.3):

> All five states produced; 0-paise residual on every `AUTO_CLOSED`;
> second run posts nothing.

and Phase 4's, which is the same three plus session 4.1's predicate
assertion:

> First end-to-end run producing all five terminal states. Every
> `AUTO_CLOSED` case shows a 0-paise post-adjustment residual. Running the
> same batch twice posts nothing on the second pass.

The three checkpoint tests are named for the sentences they check. Around
them sit the two failure paths nothing in the reference batch exercises —
a candidate that fails the 1.7.5 chain, and one that passes it but leaves
a non-zero residual — because a validator whose rejection path is never
run is a validator nobody has tested.
"""

from __future__ import annotations

import random
import sqlite3
from collections import Counter
from datetime import date

import pytest

from generator.cli import generate_reference_batch
from pipeline.accounts import ACCOUNT_PAYMENT_GATEWAY_CHARGES, ACCOUNT_RAZORPAY_CLEARING
from pipeline.apply import (
    APPLIED_NARRATION,
    LedgerState,
    apply_case,
    assign_state,
    orphan_cases_never_post,
)
from pipeline.case_assembly import CaseKind
from pipeline.ground_truth import DeclineReason, ExceptionSubtype, OutcomeState
from pipeline.instantiator import CandidateJournalEntry, CandidateJournalLeg
from pipeline.predicates import TemplateId
from pipeline.run import run_batch
from pipeline.schemas import LedgerSource
from pipeline.storage import connect, fetch_ledger_entries
from pipeline.validator import ValidationCheck, batch_record_ids

SNAPSHOT = date(2026, 8, 28)


def _run(seed: int = 0):
    """One end-to-end pass over a freshly generated batch, plus its open connection."""
    batch = generate_reference_batch(random.Random(seed), SNAPSHOT)
    conn = connect(":memory:")
    result = run_batch(
        conn,
        settlements=batch.settlements,
        recon_lines=batch.recon_lines,
        bank_lines=batch.bank_lines,
        ledger_entries=batch.ledger_entries,
        snapshot_date=SNAPSHOT,
    )
    return batch, conn, result


def _rerun(batch, conn):
    return run_batch(
        conn,
        settlements=batch.settlements,
        recon_lines=batch.recon_lines,
        bank_lines=batch.bank_lines,
        ledger_entries=batch.ledger_entries,
        snapshot_date=SNAPSHOT,
        seed_ledger_first=False,
    )


# --- The checkpoint. ---


def test_all_five_terminal_states_are_produced() -> None:
    _, _, result = _run()

    distribution = result.state_distribution()

    assert set(distribution) == {state.value for state in OutcomeState}
    assert all(count > 0 for count in distribution.values())
    assert sum(distribution.values()) == 150


def test_every_auto_closed_case_shows_a_zero_paise_post_adjustment_residual() -> None:
    _, _, result = _run()

    auto_closed = [o for o in result.outcome.outcomes if o.state is OutcomeState.AUTO_CLOSED]

    assert len(auto_closed) == 50, "§3.6's batch totals put 50 cases in AUTO_CLOSED"
    for outcome in auto_closed:
        assert outcome.residual_paise == 0, f"{outcome.case_id} closed at {outcome.residual_paise} paise"
        assert outcome.applied_entries, f"{outcome.case_id} is AUTO_CLOSED with nothing applied"
        residual_checks = [
            result_
            for report in outcome.validations
            for result_ in report.results
            if result_.check is ValidationCheck.RESIDUAL_ZERO
        ]
        assert residual_checks and all(r.passed for r in residual_checks), outcome.case_id


def test_running_the_same_batch_twice_posts_nothing_on_the_second_pass() -> None:
    batch, conn, first = _run()
    ledger_after_first = fetch_ledger_entries(conn)

    second = _rerun(batch, conn)

    assert first.outcome.posted_leg_count == 120
    assert second.outcome.posted_leg_count == 0
    assert fetch_ledger_entries(conn) == ledger_after_first, "the second pass changed the ledger"

    # Idempotent in outcome, not only in writes: identical states, and every
    # AUTO_CLOSED case recognised as a replay rather than declined.
    assert first.state_distribution() == second.state_distribution()
    assert {o.case_id: o.state for o in first.outcome.outcomes} == {
        o.case_id: o.state for o in second.outcome.outcomes
    }
    for outcome in second.outcome.outcomes:
        if outcome.state is OutcomeState.AUTO_CLOSED:
            assert outcome.replayed_entries and not outcome.applied_entries, outcome.case_id


# --- The other side of 1.7.4: a key that is posted but is *not* this entry. ---
#
# `apply_case` splits an already-posted `(case_id, resolution_id)` two ways.
# `_is_identical_replay` true is the idempotency path the test above covers.
# False is a different claim entirely: something is posted under this key that
# this correction did not write. Replaying it would endorse a posting the
# Controller cannot account for; re-posting it would double-post. It does
# neither, and until these two tests that branch was the one part of the
# idempotency machinery no test reached — invisible precisely because the
# reference batch can only ever produce the identical-replay case.
#
# Both tests tamper with the committed ledger between passes, which is the
# only way to reach the branch: it is unreachable from any batch, by design.


def _one_controller_entry(conn):
    return next(e for e in fetch_ledger_entries(conn) if e.source is LedgerSource.CONTROLLER_ADJUSTMENT)


def _integrity_failures(outcome):
    return [
        result
        for report in outcome.validations
        for result in report.results
        if result.check is ValidationCheck.NOT_PREVIOUSLY_POSTED and not result.passed
    ]


def test_the_same_key_posted_with_different_amounts_is_refused_not_replayed() -> None:
    """One paise of drift on a posted leg is not a replay, and must not be treated as one.

    `_is_identical_replay` compares leg *values*, not just the key or the leg
    count, so a posting that differs by the smallest representable amount is
    still a different entry. The case must leave `AUTO_CLOSED`, and nothing
    may be written to reconcile the difference.
    """
    batch, conn, _ = _run()
    target = _one_controller_entry(conn)
    column = "debit" if int(target.debit) else "credit"
    conn.execute(
        f"UPDATE ledger_entry SET {column} = {column} + 1 WHERE journal_entry_id = ?",
        (target.journal_entry_id,),
    )
    conn.commit()
    ledger_before_rerun = fetch_ledger_entries(conn)

    second = _rerun(batch, conn)
    outcome = {o.case_id: o for o in second.outcome.outcomes}[target.case_id]

    assert outcome.state is OutcomeState.REVIEW_REQUIRED
    assert outcome.decline_reason is DeclineReason.CONFIDENCE
    assert not outcome.replayed_entries, "a drifted posting was accepted as a replay"
    assert not outcome.applied_entries, "the Controller wrote over a posting it did not recognise"

    failures = _integrity_failures(outcome)
    assert len(failures) == 1
    assert "is posted with different legs" in failures[0].detail
    assert str((target.case_id, target.resolution_id)) in failures[0].detail

    assert second.outcome.posted_leg_count == 0
    assert fetch_ledger_entries(conn) == ledger_before_rerun, "the refused case still changed the ledger"


def test_a_partially_deleted_entry_is_refused_rather_than_topped_up() -> None:
    """The leg-count half of the same branch, and the more dangerous half.

    A three-leg `T-01` missing one leg is an unbalanced entry sitting in the
    ledger under a key the Controller would otherwise recognise. The tempting
    repair — post the missing leg — would produce a balanced entry assembled
    from two different runs. `_is_identical_replay` rejects on length before it
    compares any value, so the case is declined and the ledger is left exactly
    as broken as it was found, for a human to resolve.
    """
    batch, conn, _ = _run()
    posted = [e for e in fetch_ledger_entries(conn) if e.source is LedgerSource.CONTROLLER_ADJUSTMENT]
    legs_per_entry = Counter((e.case_id, e.resolution_id) for e in posted)
    key = next(k for k, count in legs_per_entry.items() if count == 3)
    victim = next(e for e in posted if (e.case_id, e.resolution_id) == key)

    conn.execute("DELETE FROM ledger_entry WHERE journal_entry_id = ?", (victim.journal_entry_id,))
    conn.commit()
    ledger_before_rerun = fetch_ledger_entries(conn)

    second = _rerun(batch, conn)
    outcome = {o.case_id: o for o in second.outcome.outcomes}[key[0]]

    assert outcome.state is OutcomeState.REVIEW_REQUIRED
    assert outcome.decline_reason is DeclineReason.CONFIDENCE
    assert _integrity_failures(outcome), "a truncated entry was accepted"
    assert second.outcome.posted_leg_count == 0, "the missing leg was silently re-posted"
    assert fetch_ledger_entries(conn) == ledger_before_rerun


def test_one_tampered_case_does_not_disturb_the_other_forty_nine() -> None:
    """The check has to discriminate, not just fail. Every other `AUTO_CLOSED`
    case replays cleanly in the same pass that refuses the tampered one."""
    batch, conn, first = _run()
    target = _one_controller_entry(conn)
    conn.execute(
        "UPDATE ledger_entry SET debit = debit + 1 WHERE journal_entry_id = ?",
        (target.journal_entry_id,),
    )
    conn.commit()

    second = _rerun(batch, conn)
    closed_first = {o.case_id for o in first.outcome.outcomes if o.state is OutcomeState.AUTO_CLOSED}
    closed_second = {o.case_id for o in second.outcome.outcomes if o.state is OutcomeState.AUTO_CLOSED}

    assert closed_first - closed_second == {target.case_id}
    assert not closed_second - closed_first
    for outcome in second.outcome.outcomes:
        if outcome.state is OutcomeState.AUTO_CLOSED:
            assert outcome.replayed_entries and not outcome.applied_entries, outcome.case_id


# --- REV-24: the constraint that made AUTO_CLOSED unreachable. ---


def test_a_three_leg_entry_posts_all_three_legs() -> None:
    """REV-24's regression. Under `UNIQUE(case_id, resolution_id)` the second
    and third legs of every `T-01` were rejected and no case could close."""
    _, conn, result = _run()

    posted = [e for e in fetch_ledger_entries(conn) if e.source is LedgerSource.CONTROLLER_ADJUSTMENT]
    legs_per_entry = Counter((e.case_id, e.resolution_id) for e in posted)

    assert legs_per_entry, "nothing was posted at all"
    assert max(legs_per_entry.values()) == 3, "no three-leg entry survived the constraint"
    assert set(legs_per_entry.values()) == {2, 3}
    assert len(posted) == result.outcome.posted_leg_count == 120


def test_the_widened_constraint_still_rejects_a_duplicate_leg() -> None:
    """Widening it to the leg must not have made it toothless: the same leg
    of the same correction still cannot be posted twice (§1.7.4)."""
    _, conn, _ = _run()
    posted = next(e for e in fetch_ledger_entries(conn) if e.source is LedgerSource.CONTROLLER_ADJUSTMENT)

    duplicate = posted.model_copy(update={"journal_entry_id": "je_a_different_id"})

    with pytest.raises(sqlite3.IntegrityError, match="case_id, ledger_entry.resolution_id"):
        conn.execute(
            "INSERT INTO ledger_entry (journal_entry_id, date, account_code, account_name, debit, "
            "credit, reference, narration, source, resolution_id, case_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                duplicate.journal_entry_id,
                duplicate.date.isoformat(),
                duplicate.account_code,
                duplicate.account_name,
                int(duplicate.debit),
                int(duplicate.credit),
                duplicate.reference,
                duplicate.narration,
                duplicate.source.value,
                duplicate.resolution_id,
                duplicate.case_id,
            ),
        )


# --- §1.3's five states, assigned from evidence (§3.3). ---


@pytest.mark.parametrize(
    ("kwargs", "expected_state", "expected_reason"),
    [
        pytest.param(
            {"has_candidates": True, "applied_or_replayed": True, "declined_by_policy": True},
            OutcomeState.REVIEW_REQUIRED,
            DeclineReason.POLICY,
            id="policy outranks everything, §2.5 'regardless of model confidence'",
        ),
        pytest.param(
            {"has_candidates": True, "applied_or_replayed": True},
            OutcomeState.AUTO_CLOSED,
            None,
            id="a correction that landed",
        ),
        pytest.param(
            {
                "has_candidates": True,
                "applied_or_replayed": True,
                "triggered_subtypes": [ExceptionSubtype.BANK_CREDIT_OVERDUE],
            },
            OutcomeState.AUTO_CLOSED,
            None,
            id="correction outranks a fired trigger, §3.3's OPERATIONAL_EXCEPTION definition",
        ),
        pytest.param(
            {"has_candidates": True, "applied_or_replayed": False},
            OutcomeState.REVIEW_REQUIRED,
            DeclineReason.CONFIDENCE,
            id="a candidate that failed the chain",
        ),
        pytest.param(
            {"triggered_subtypes": [ExceptionSubtype.SETTLEMENT_UTR_MISSING], "residual_paise": 0},
            OutcomeState.EXTERNAL_ACTION_REQUIRED,
            None,
            id="a fired trigger with no correction",
        ),
        pytest.param(
            {"residual_paise": 0},
            OutcomeState.AUTO_MATCHED,
            None,
            id="nothing to correct, nothing to escalate",
        ),
        pytest.param(
            {"residual_paise": 12_345},
            OutcomeState.ABSTAINED,
            None,
            id="a residual no template explains and no trigger categorises",
        ),
    ],
)
def test_terminal_state_assignment_follows_section_3_3(
    kwargs: dict, expected_state: OutcomeState, expected_reason: DeclineReason | None
) -> None:
    defaults = {
        "has_candidates": False,
        "applied_or_replayed": False,
        "declined_by_policy": False,
        "triggered_subtypes": [],
        "residual_paise": 0,
    }
    defaults.update(kwargs)

    assert assign_state(**defaults) == (expected_state, expected_reason)  # type: ignore[arg-type]


# --- FR-07 and the two failure paths the batch never exercises. ---


def test_a_policy_declined_case_carries_its_proposed_entry_unapplied() -> None:
    """FR-07: "every `REVIEW_REQUIRED` case MUST carry a machine-readable
    proposed journal entry ... flagged as unapplied with an explicit decline
    reason". A reviewer gets a decision, not a research task."""
    batch, _, result = _run()

    declined = [o for o in result.outcome.outcomes if o.state is OutcomeState.REVIEW_REQUIRED]

    assert len(declined) == 17
    tax_cases = [o for o in declined if batch.population_of.get(o.case_id) == "fr06_tax"]
    assert len(tax_cases) == 12
    for outcome in tax_cases:
        assert outcome.decline_reason is DeclineReason.POLICY
        assert outcome.proposed_entries, f"{outcome.case_id} declined with no proposed entry"
        assert not outcome.applied_entries
        assert outcome.policy_decisions

    # REV-11's five carry no proposed entry: no predicate fires on them, because
    # their accounts and amounts are already correct and only the period is wrong.
    date_error = [o for o in declined if batch.population_of.get(o.case_id) == "family_4_date_error"]
    assert len(date_error) == 5
    for outcome in date_error:
        assert outcome.decline_reason is DeclineReason.POLICY
        assert outcome.proposed_entries == ()


def _case_and_evidence(result, batch, population: str):
    case_id = next(cid for cid, name in batch.population_of.items() if name == population)
    case = next(c for c in result.cases if c.case_id == case_id)
    evidence = next(e for e in result.evidences if e.case_id == case_id)
    return case, evidence


def test_a_candidate_that_fails_the_chain_posts_nothing_and_routes_to_review() -> None:
    batch = generate_reference_batch(random.Random(0), SNAPSHOT)
    conn = connect(":memory:")
    result = run_batch(
        conn,
        settlements=batch.settlements,
        recon_lines=batch.recon_lines,
        bank_lines=batch.bank_lines,
        ledger_entries=batch.ledger_entries,
        snapshot_date=SNAPSHOT,
    )
    case, evidence = _case_and_evidence(result, batch, "settlement_utr_missing")

    tampered = CandidateJournalEntry(
        case_id=case.case_id,
        template_id=TemplateId.T01,
        legs=(
            CandidateJournalLeg(
                account_code="9999", account_name="Suspense", debit=1_000, credit=0
            ),
            CandidateJournalLeg(
                account_code=ACCOUNT_RAZORPAY_CLEARING.code,
                account_name=ACCOUNT_RAZORPAY_CLEARING.name,
                debit=0,
                credit=1_000,
            ),
        ),
        cited_record_ids=(case.recon_lines[0].entity_id,),
    )
    state = LedgerState(fetch_ledger_entries(conn))
    before = len(state.entries)

    outcome = apply_case(
        conn,
        state,
        case,
        evidence,
        [tampered],
        posting_date=SNAPSHOT,
        known_record_ids=batch_record_ids(result.cases, state.entries),
    )

    assert outcome.state is OutcomeState.REVIEW_REQUIRED
    assert outcome.decline_reason is DeclineReason.CONFIDENCE
    assert outcome.applied_entries == ()
    assert outcome.proposed_entries == (tampered,)
    assert len(fetch_ledger_entries(conn)) == before, "a failed validation left rows behind"


def test_a_valid_candidate_that_leaves_a_residual_is_rolled_back() -> None:
    """§1.3 applies before re-reconciling; §1.7.5 requires a failed validation
    to prevent auto-action. The transaction is what satisfies both, and this
    is the only test that exercises the rollback."""
    batch = generate_reference_batch(random.Random(0), SNAPSHOT)
    conn = connect(":memory:")
    result = run_batch(
        conn,
        settlements=batch.settlements,
        recon_lines=batch.recon_lines,
        bank_lines=batch.bank_lines,
        ledger_entries=batch.ledger_entries,
        snapshot_date=SNAPSHOT,
    )
    # A clean case, given a perfectly well-formed T-01 that corrects nothing:
    # every account is allowed in the direction used, it balances, it cites a
    # real unposted record — and applying it moves the books away from the
    # evidence rather than towards it.
    case, evidence = _case_and_evidence(result, batch, "fully_clean")
    bogus = CandidateJournalEntry(
        case_id=case.case_id,
        template_id=TemplateId.T01,
        legs=(
            CandidateJournalLeg(
                account_code=ACCOUNT_PAYMENT_GATEWAY_CHARGES.code,
                account_name=ACCOUNT_PAYMENT_GATEWAY_CHARGES.name,
                debit=7_777,
                credit=0,
            ),
            CandidateJournalLeg(
                account_code=ACCOUNT_RAZORPAY_CLEARING.code,
                account_name=ACCOUNT_RAZORPAY_CLEARING.name,
                debit=0,
                credit=7_777,
            ),
        ),
        cited_record_ids=(case.recon_lines[0].entity_id,),
    )
    state = LedgerState(fetch_ledger_entries(conn))
    before = fetch_ledger_entries(conn)

    outcome = apply_case(
        conn,
        state,
        case,
        evidence,
        [bogus],
        posting_date=SNAPSHOT,
        known_record_ids=batch_record_ids(result.cases, state.entries),
    )

    assert outcome.state is OutcomeState.REVIEW_REQUIRED
    assert outcome.decline_reason is DeclineReason.CONFIDENCE
    assert outcome.applied_entries == ()
    assert fetch_ledger_entries(conn) == before, "the rolled-back legs are still in the ledger"
    assert state.entries == before, "the in-memory view kept rows SQLite discarded"
    residual_failure = next(
        r
        for report in outcome.validations
        for r in report.results
        if r.check is ValidationCheck.RESIDUAL_ZERO
    )
    assert not residual_failure.passed
    assert "7777" in residual_failure.detail or "paise, not 0" in residual_failure.detail


# --- Properties of what the run wrote. ---


def test_the_reconciled_ledger_still_balances_globally() -> None:
    """§1.8's first artifact. Every posted correction balances individually, so
    the whole ledger must still balance after 120 new legs."""
    _, conn, _ = _run()
    entries = fetch_ledger_entries(conn)

    debits = sum(int(e.debit) for e in entries)
    credits = sum(int(e.credit) for e in entries)

    assert debits == credits > 0


def test_every_posted_row_is_a_sourced_controller_adjustment() -> None:
    """§1.7.2 and §1.7.3: deterministic accounts and amounts, a fixed narration
    the model never wrote, and a cited source record on every row."""
    batch, conn, _ = _run()
    entity_ids = {line.entity_id for line in batch.recon_lines}

    for entry in fetch_ledger_entries(conn):
        if entry.source is not LedgerSource.CONTROLLER_ADJUSTMENT:
            continue
        assert entry.narration == APPLIED_NARRATION
        assert entry.case_id is not None and entry.resolution_id is not None
        assert entry.reference in entity_ids
        assert entry.date == SNAPSHOT
        assert (int(entry.debit) > 0) != (int(entry.credit) > 0)


def test_no_orphan_case_ever_posts() -> None:
    """§3.6: no §3.4 template addresses an orphan, and none of the four orphan
    populations is closeable by a journal entry."""
    _, _, result = _run()

    assert orphan_cases_never_post(result.outcome.outcomes, result.cases)
    orphan_ids = {c.case_id for c in result.cases if c.kind is CaseKind.ORPHAN}
    orphan_states = {o.state for o in result.outcome.outcomes if o.case_id in orphan_ids}
    assert orphan_states == {OutcomeState.EXTERNAL_ACTION_REQUIRED, OutcomeState.ABSTAINED}


# --- Against ground truth. ---


def test_predicted_states_match_ground_truth_except_slot_as_eight_cases() -> None:
    """142 of 150 cases land in their §3.5/§3.6 state, and the entire shortfall
    is the one population §4.2 assigns to the graded LLM slot.

    `UNMATCHED_INBOUND_CREDIT` turns on whether a narration identifies a
    counterparty (§4.2 Slot A, session 5.2). No deterministic component
    decides it, so those 8 orphan cases fire no subtype trigger and fall
    through to `ABSTAINED`. Asserted exactly, so the gap stays a named
    limitation rather than becoming an unexplained metric dent.
    """
    batch, _, result = _run()
    outcomes = result.outcome.by_case_id()

    # Settlement-anchored cases share ground truth's id; orphan cases do not
    # (the generator mints `orphan_*` ids unrelated to the bank lines), so they
    # join through `expected_linked_source_records`, as session 4.1 established.
    ground_truth_by_line: dict[str, object] = {}
    ground_truth_by_case = {}
    for row in batch.ground_truth:
        ground_truth_by_case[row.case_id] = row
        for record_id in row.expected_linked_source_records:
            ground_truth_by_line[record_id] = row

    mismatches: list[tuple[str, str, str]] = []
    for case in result.cases:
        if case.kind is CaseKind.SETTLEMENT_ANCHORED:
            expected = ground_truth_by_case[case.case_id].expected_outcome_state
        else:
            expected = ground_truth_by_line[case.bank_lines[0].line_id].expected_outcome_state
        actual = outcomes[case.case_id].state
        if actual is not expected:
            mismatches.append((case.case_id, str(expected), str(actual)))

    assert len(mismatches) == 8, mismatches
    assert {(expected, actual) for _, expected, actual in mismatches} == {
        ("EXTERNAL_ACTION_REQUIRED", "ABSTAINED")
    }
    populations = {
        ground_truth_by_line[
            next(c for c in result.cases if c.case_id == case_id).bank_lines[0].line_id
        ].ground_truth_exception_subtype
        for case_id, _, _ in mismatches
    }
    assert populations == {ExceptionSubtype.UNMATCHED_INBOUND_CREDIT}


def test_the_state_distribution_matches_the_batch_totals_table() -> None:
    """§3.6's "Batch totals", with the 8 Slot A cases moved from
    `EXTERNAL_ACTION_REQUIRED` to `ABSTAINED` and nothing else shifted."""
    _, _, result = _run()

    assert result.state_distribution() == {
        "AUTO_MATCHED": 30,
        "AUTO_CLOSED": 50,
        "REVIEW_REQUIRED": 17,
        "EXTERNAL_ACTION_REQUIRED": 36 - 8,
        "ABSTAINED": 17 + 8,
    }
    assert result.outcome.decline_reason_distribution() == {"policy": 17}


@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_the_checkpoint_holds_across_seeds(seed: int) -> None:
    batch, conn, first = _run(seed)

    assert set(first.state_distribution()) == {state.value for state in OutcomeState}
    assert first.state_distribution()["AUTO_CLOSED"] == 50
    assert all(
        o.residual_paise == 0 for o in first.outcome.outcomes if o.state is OutcomeState.AUTO_CLOSED
    )

    second = _rerun(batch, conn)

    assert second.outcome.posted_leg_count == 0
    assert first.state_distribution() == second.state_distribution()


def test_the_same_seed_produces_an_identical_run() -> None:
    """NFR-01. No wall-clock read anywhere on the path: the snapshot date is a
    parameter and it is also the posting date, so two runs of one seed write
    byte-identical ledgers."""
    _, conn_a, result_a = _run(7)
    _, conn_b, result_b = _run(7)

    assert result_a.state_distribution() == result_b.state_distribution()
    assert [o.model_dump() for o in result_a.outcome.outcomes] == [
        o.model_dump() for o in result_b.outcome.outcomes
    ]
    assert [e.model_dump() for e in fetch_ledger_entries(conn_a)] == [
        e.model_dump() for e in fetch_ledger_entries(conn_b)
    ]
