"""Session 2.2 checkpoint (spec.md §6.3), settlement-anchored half:
§3.5's case-allocation rows not built by session 2.1 — family-4 no-op
(12), family-4 date-error (5), FR-06 tax positions (12),
`SETTLEMENT_UTR_MISSING` (5), `BANK_CREDIT_OVERDUE` (5),
`SETTLEMENT_AMOUNT_MISMATCH` (4), `DISPUTE_PENDING` (5), and
`AMBIGUOUS_CASE` (9) — 57 cases exactly.
"""

from __future__ import annotations

import random
from collections import defaultdict
from datetime import date

import pytest

from generator.exceptions import (
    N_AMBIGUOUS,
    N_BANK_CREDIT_OVERDUE,
    N_DISPUTE_PENDING,
    N_FAMILY_4_DATE_ERROR,
    N_FAMILY_4_NO_OP,
    N_FR06_TAX,
    N_SETTLEMENT_AMOUNT_MISMATCH,
    N_SETTLEMENT_UTR_MISSING,
    generate_all_exception_batches,
    generate_ambiguous_batch,
    generate_bank_credit_overdue_batch,
    generate_dispute_pending_batch,
    generate_family_4_date_error_batch,
    generate_family_4_no_op_batch,
    generate_fr06_tax_batch,
    generate_settlement_amount_mismatch_batch,
    generate_settlement_utr_missing_batch,
)
from pipeline.timing import is_within_settlement_window
from pipeline.ground_truth import DeclineReason, ExceptionClass, ExceptionSubtype, OutcomeState
from pipeline.money import Paise
from pipeline.schemas import RazorpayEntityType

SNAPSHOT = date(2026, 8, 28)

_POPULATIONS = {
    "family_4_no_op": (generate_family_4_no_op_batch, N_FAMILY_4_NO_OP),
    "family_4_date_error": (generate_family_4_date_error_batch, N_FAMILY_4_DATE_ERROR),
    "fr06_tax": (generate_fr06_tax_batch, N_FR06_TAX),
    "settlement_utr_missing": (generate_settlement_utr_missing_batch, N_SETTLEMENT_UTR_MISSING),
    "bank_credit_overdue": (generate_bank_credit_overdue_batch, N_BANK_CREDIT_OVERDUE),
    "settlement_amount_mismatch": (generate_settlement_amount_mismatch_batch, N_SETTLEMENT_AMOUNT_MISMATCH),
    "dispute_pending": (generate_dispute_pending_batch, N_DISPUTE_PENDING),
    "ambiguous": (generate_ambiguous_batch, N_AMBIGUOUS),
}


@pytest.mark.parametrize("name", _POPULATIONS)
def test_population_holds_its_exact_count(name):
    generate, n = _POPULATIONS[name]
    batch = generate(random.Random(1), SNAPSHOT)
    assert len(batch.settlements) == n
    assert len(batch.ground_truth) == n
    case_ids = [gt.case_id for gt in batch.ground_truth]
    assert len(case_ids) == len(set(case_ids))


def test_all_exception_batches_combine_to_fifty_seven_cases():
    batch = generate_all_exception_batches(random.Random(1), SNAPSHOT)
    assert len(batch.settlements) == 57
    assert len(batch.ground_truth) == 57
    case_ids = [gt.case_id for gt in batch.ground_truth]
    assert len(case_ids) == len(set(case_ids))


def _lines_by_settlement(batch):
    by_settlement = defaultdict(list)
    for line in batch.recon_lines:
        by_settlement[line.settlement_id].append(line)
    return by_settlement


def test_settlement_amount_invariant_holds_except_for_the_deliberate_mismatch_population():
    """§3.5's hard invariant holds for every population except `SETTLEMENT_AMOUNT_MISMATCH`, its one deliberate violation."""
    for name, (generate, _n) in _POPULATIONS.items():
        batch = generate(random.Random(1), SNAPSHOT)
        lines_by_settlement = _lines_by_settlement(batch)
        for settlement in batch.settlements:
            lines = lines_by_settlement[settlement.id]
            total = (
                sum((line.credit for line in lines), Paise(0))
                - sum((line.debit for line in lines), Paise(0))
                - sum((line.fee for line in lines), Paise(0))
                - sum((line.tax for line in lines), Paise(0))
            )
            if name == "settlement_amount_mismatch":
                assert settlement.amount != total
            else:
                assert settlement.amount == total


def test_ledger_balances_globally_across_all_exception_populations():
    batch = generate_all_exception_batches(random.Random(1), SNAPSHOT)
    total_debit = sum((e.debit for e in batch.ledger_entries), Paise(0))
    total_credit = sum((e.credit for e in batch.ledger_entries), Paise(0))
    assert total_debit == total_credit
    assert total_debit > 0


def test_no_float_touches_any_money_field():
    batch = generate_all_exception_batches(random.Random(1), SNAPSHOT)
    for line in batch.recon_lines:
        for field_name in ("debit", "credit", "amount", "fee", "tax"):
            assert isinstance(getattr(line, field_name), int)
    for entry in batch.ledger_entries:
        assert isinstance(entry.debit, int)
        assert isinstance(entry.credit, int)
    for bank_line in batch.bank_lines:
        assert isinstance(bank_line.withdrawal_paise, int)
        assert isinstance(bank_line.deposit_paise, int)
        assert isinstance(bank_line.closing_balance_paise, int)


def test_generation_is_deterministic_given_the_same_seed():
    batch_a = generate_all_exception_batches(random.Random(1), SNAPSHOT)
    batch_b = generate_all_exception_batches(random.Random(1), SNAPSHOT)
    assert [s.model_dump() for s in batch_a.settlements] == [s.model_dump() for s in batch_b.settlements]
    assert [g.model_dump() for g in batch_a.ground_truth] == [g.model_dump() for g in batch_b.ground_truth]
    assert [b.model_dump() for b in batch_a.bank_lines] == [b.model_dump() for b in batch_b.bank_lines]


def test_family_4_no_op_has_no_bank_line_and_is_within_window():
    """`EXPECTED_TIMING_DIFFERENCE`/`AUTO_MATCHED`; REV-17 no-credit population."""
    from datetime import datetime, timezone

    batch = generate_family_4_no_op_batch(random.Random(1), SNAPSHOT)
    assert len(batch.bank_lines) == 0
    for gt in batch.ground_truth:
        assert gt.expected_outcome_state == OutcomeState.AUTO_MATCHED
        assert gt.ground_truth_exception_class == ExceptionClass.EXPECTED_TIMING_DIFFERENCE
        assert gt.should_auto_apply is False
        assert gt.expected_journal_entries == ()
    for settlement in batch.settlements:
        created_date = datetime.fromtimestamp(settlement.created_at, tz=timezone.utc).date()
        assert is_within_settlement_window(created_date, SNAPSHOT)


def test_bank_credit_overdue_has_no_bank_line_and_is_past_window():
    from datetime import datetime, timezone

    batch = generate_bank_credit_overdue_batch(random.Random(1), SNAPSHOT)
    assert len(batch.bank_lines) == 0
    for settlement in batch.settlements:
        created_date = datetime.fromtimestamp(settlement.created_at, tz=timezone.utc).date()
        assert not is_within_settlement_window(created_date, SNAPSHOT)
    for gt in batch.ground_truth:
        assert gt.expected_outcome_state == OutcomeState.EXTERNAL_ACTION_REQUIRED
        assert gt.ground_truth_exception_subtype == ExceptionSubtype.BANK_CREDIT_OVERDUE


def test_family_4_date_error_has_landed_credit_and_a_shifted_date():
    batch = generate_family_4_date_error_batch(random.Random(1), SNAPSHOT)
    assert len(batch.bank_lines) == N_FAMILY_4_DATE_ERROR
    referenced_dates = defaultdict(set)
    for entry in batch.ledger_entries:
        referenced_dates[entry.reference].add(entry.date)
    for gt in batch.ground_truth:
        entity_id = gt.expected_linked_source_records[0]
        # The anomalous payment's legs all share one shifted date, distinct from a same-day clean posting.
        assert len(referenced_dates[entity_id]) == 1
        assert gt.expected_outcome_state == OutcomeState.REVIEW_REQUIRED
        assert gt.expected_decline_reason == DeclineReason.POLICY
        assert gt.ground_truth_exception_subtype == ExceptionSubtype.MISPOSTING
        assert gt.expected_journal_entries == ()


def test_fr06_tax_cases_carry_a_tax_signature_and_are_policy_declined():
    batch = generate_fr06_tax_batch(random.Random(1), SNAPSHOT)
    assert len(batch.bank_lines) == N_FR06_TAX
    signatures_seen = set()
    for gt in batch.ground_truth:
        entity_id = gt.expected_linked_source_records[0]
        adj_line = next(line for line in batch.recon_lines if line.entity_id == entity_id)
        assert adj_line.description is not None
        signatures_seen.add(adj_line.description)
        assert gt.expected_outcome_state == OutcomeState.REVIEW_REQUIRED
        assert gt.expected_decline_reason == DeclineReason.POLICY
        assert gt.expected_journal_entries == ()
        assert gt.expected_template_ids == ()
    assert len(signatures_seen) == 2  # both 194-O and ITC-eligibility signatures appear


def test_settlement_utr_missing_has_empty_utr_and_a_landed_credit():
    batch = generate_settlement_utr_missing_batch(random.Random(1), SNAPSHOT)
    assert len(batch.bank_lines) == N_SETTLEMENT_UTR_MISSING
    for settlement in batch.settlements:
        assert settlement.utr == ""
    for gt in batch.ground_truth:
        assert gt.ground_truth_exception_subtype == ExceptionSubtype.SETTLEMENT_UTR_MISSING


def test_dispute_pending_flags_exactly_one_payment_per_case():
    batch = generate_dispute_pending_batch(random.Random(1), SNAPSHOT)
    for gt in batch.ground_truth:
        disputed_lines = [
            line
            for line in batch.recon_lines
            if line.settlement_id == gt.case_id and line.dispute_id is not None
        ]
        assert len(disputed_lines) == 1
        assert gt.ground_truth_exception_subtype == ExceptionSubtype.DISPUTE_PENDING
        assert gt.expected_journal_entries == ()


def test_ambiguous_ledger_entry_is_uncorroborated_but_attributable():
    """The pair must be *unresolvable* (no refund recon line backs it) and *attributable*
    (its `reference` names a real payment in its own settlement) at the same time.

    Session 2.2 had only the first half: the reference resolved to nothing
    anywhere in the batch, which also meant it belonged to no case, so
    these nine `ABSTAINED` labels were unreachable from evidence. Both
    halves are asserted here so neither can be lost again.
    """
    batch = generate_ambiguous_batch(random.Random(1), SNAPSHOT)
    entity_ids = {line.entity_id for line in batch.recon_lines}
    payments_by_settlement = {
        settlement.id: {line.entity_id for line in batch.recon_lines if line.settlement_id == settlement.id}
        for settlement in batch.settlements
    }
    assert not any(line.type is RazorpayEntityType.REFUND for line in batch.recon_lines)

    for gt in batch.ground_truth:
        journal_entry_ids = {r for r in gt.expected_linked_source_records if r.startswith("je_")}
        assert journal_entry_ids, "the phantom pair must be cited in the ground-truth record list"
        for je_id in journal_entry_ids:
            entry = next(e for e in batch.ledger_entries if e.journal_entry_id == je_id)
            # Attributable: joins to its own case through the one ledger -> recon join.
            assert entry.reference in entity_ids
            assert entry.reference in payments_by_settlement[gt.case_id]
        # Uncorroborated: no refund recon line exists for that payment, so T-02 cannot fire.
        assert gt.expected_outcome_state == OutcomeState.ABSTAINED
        assert gt.ground_truth_exception_class == ExceptionClass.AMBIGUOUS_CASE
        assert gt.expected_template_ids == ()
