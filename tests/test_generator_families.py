""" checkpoint: the five the anomaly families family injections.

Checkpoint: **per-family counts assert to 10 each.** The supporting tests
below additionally guard the arithmetic each family's ground truth depends
on (settlement-amount invariant, global ledger balance, and each family's
own evidence-predicate conjuncts from the template definitions), so a wrong hand-derived
correction amount fails loudly here rather than surfacing as a silent
metric error in Phase 6.
"""

from __future__ import annotations

import random
from collections import defaultdict
from datetime import date

import pytest

from generator.families import (
    N_CASES_PER_FAMILY,
    generate_all_family_batches,
    generate_family_1_batch,
    generate_family_2_batch,
    generate_family_3_batch,
    generate_family_4_batch,
    generate_family_5_batch,
)
from pipeline.ground_truth import ExceptionSubtype, OutcomeState
from pipeline.money import Paise

SNAPSHOT = date(2026, 8, 28)

_FAMILY_GENERATORS = {
    "family_1": generate_family_1_batch,
    "family_2": generate_family_2_batch,
    "family_3": generate_family_3_batch,
    "family_4": generate_family_4_batch,
    "family_5": generate_family_5_batch,
}


@pytest.mark.parametrize("name", _FAMILY_GENERATORS)
def test_family_batch_holds_exactly_ten_cases(name):
    generate = _FAMILY_GENERATORS[name]
    batch = generate(random.Random(1), SNAPSHOT)
    assert len(batch.settlements) == N_CASES_PER_FAMILY == 10
    assert len(batch.ground_truth) == 10
    assert all(gt.expected_outcome_state == OutcomeState.AUTO_CLOSED for gt in batch.ground_truth)


def test_all_family_batches_combine_to_fifty_cases():
    batch = generate_all_family_batches(random.Random(1), SNAPSHOT)
    assert len(batch.settlements) == 50
    assert len(batch.ground_truth) == 50
    case_ids = [gt.case_id for gt in batch.ground_truth]
    assert len(case_ids) == len(set(case_ids)), "case_id must be unique across the batch"


def _settlement_lines(batch):
    lines_by_settlement = defaultdict(list)
    for line in batch.recon_lines:
        lines_by_settlement[line.settlement_id].append(line)
    return lines_by_settlement


def test_settlement_amount_invariant_holds_for_every_family():
    batch = generate_all_family_batches(random.Random(1), SNAPSHOT)
    lines_by_settlement = _settlement_lines(batch)
    for settlement in batch.settlements:
        lines = lines_by_settlement[settlement.id]
        total_credit = sum((line.credit for line in lines), Paise(0))
        total_debit = sum((line.debit for line in lines), Paise(0))
        total_fee = sum((line.fee for line in lines), Paise(0))
        total_tax = sum((line.tax for line in lines), Paise(0))
        assert settlement.amount == total_credit - total_debit - total_fee - total_tax


def test_ledger_balances_globally_across_all_families():
    batch = generate_all_family_batches(random.Random(1), SNAPSHOT)
    total_debit = sum((e.debit for e in batch.ledger_entries), Paise(0))
    total_credit = sum((e.credit for e in batch.ledger_entries), Paise(0))
    assert total_debit == total_credit
    assert total_debit > 0


def test_expected_journal_entries_balance_per_case():
    """Sanity check on the generator's own hand-derived corrections, ahead of Phase 4's real validator."""
    batch = generate_all_family_batches(random.Random(1), SNAPSHOT)
    for gt in batch.ground_truth:
        for entry in gt.expected_journal_entries:
            total_debit = sum((leg.debit for leg in entry.legs), Paise(0))
            total_credit = sum((leg.credit for leg in entry.legs), Paise(0))
            assert total_debit == total_credit, f"{gt.case_id}/{entry.template_id} does not balance"
            assert total_debit > 0


def test_family_1_matches_t01_evidence_predicate():
    """T-01: settled payment, fee > 0, no Payment Gateway Charges leg, Sales Revenue credit == gross."""
    batch = generate_family_1_batch(random.Random(1), SNAPSHOT)
    referenced = {(e.account_code, e.reference) for e in batch.ledger_entries}
    for gt in batch.ground_truth:
        entity_id = gt.expected_linked_source_records[0]
        line = next(line for line in batch.recon_lines if line.entity_id == entity_id)
        assert line.fee > 0
        assert ("5010", entity_id) not in referenced  # Payment Gateway Charges
        revenue_leg = next(e for e in batch.ledger_entries if e.reference == entity_id and e.account_code == "4010")
        assert revenue_leg.credit == line.amount  # gross conjunct
        assert gt.expected_template_ids == ("T-01",)
        assert gt.ground_truth_exception_subtype == ExceptionSubtype.OMISSION


def test_family_3_matches_t03_evidence_predicate():
    """T-03: settled payment, fee > 0, Sales Revenue credit == amount - fee - tax."""
    batch = generate_family_3_batch(random.Random(1), SNAPSHOT)
    for gt in batch.ground_truth:
        entity_id = gt.expected_linked_source_records[0]
        line = next(line for line in batch.recon_lines if line.entity_id == entity_id)
        assert line.fee > 0
        revenue_leg = next(e for e in batch.ledger_entries if e.reference == entity_id and e.account_code == "4010")
        assert revenue_leg.credit == line.amount - line.fee - line.tax  # net conjunct
        assert gt.expected_template_ids == ("T-03",)
        assert gt.ground_truth_exception_subtype == ExceptionSubtype.MISPOSTING


def test_family_4_has_no_bank_line_and_posts_bank_account_leg():
    """T-04's evidence predicate holds vacuously: no `bank_line` is generated this session (the chart of accounts precondition)."""
    batch = generate_family_4_batch(random.Random(1), SNAPSHOT)
    for gt in batch.ground_truth:
        entity_id = gt.expected_linked_source_records[0]
        bank_leg = next(e for e in batch.ledger_entries if e.reference == entity_id and e.account_code == "1010")
        assert bank_leg.debit > 0
        assert gt.expected_template_ids == ("T-04",)
        assert gt.ground_truth_exception_subtype == ExceptionSubtype.MISPOSTING


def test_family_2_refund_has_no_ledger_entries_and_links_parent_payment():
    """T-02's evidence predicate: settled refund, debit > 0, no Sales Returns and Allowances leg referencing it."""
    batch = generate_family_2_batch(random.Random(1), SNAPSHOT)
    referenced_refs = {e.reference for e in batch.ledger_entries}
    for gt in batch.ground_truth:
        refund_id, payment_id = gt.expected_linked_source_records[0], gt.expected_linked_source_records[1]
        refund_line = next(line for line in batch.recon_lines if line.entity_id == refund_id)
        assert refund_line.debit > 0
        assert refund_line.payment_id == payment_id
        assert refund_id not in referenced_refs
        assert gt.expected_template_ids == ("T-02",)
        assert gt.ground_truth_exception_subtype == ExceptionSubtype.OMISSION


def test_family_5_adjustment_has_no_ledger_entries_and_exclusive_direction():
    """T-05/T-06's evidence predicate: adjustment recon line, exactly one of debit/credit > 0, no ledger leg."""
    batch = generate_family_5_batch(random.Random(1), SNAPSHOT)
    referenced_refs = {e.reference for e in batch.ledger_entries}
    for gt in batch.ground_truth:
        entity_id = gt.expected_linked_source_records[0]
        line = next(line for line in batch.recon_lines if line.entity_id == entity_id)
        assert (line.debit > 0) != (line.credit > 0)
        assert entity_id not in referenced_refs
        assert gt.expected_template_ids in (("T-05",), ("T-06",))
        assert gt.ground_truth_exception_subtype == ExceptionSubtype.OMISSION


def test_family_generation_is_deterministic_given_the_same_seed():
    batch_a = generate_all_family_batches(random.Random(1), SNAPSHOT)
    batch_b = generate_all_family_batches(random.Random(1), SNAPSHOT)
    assert [s.model_dump() for s in batch_a.settlements] == [s.model_dump() for s in batch_b.settlements]
    assert [g.model_dump() for g in batch_a.ground_truth] == [g.model_dump() for g in batch_b.ground_truth]


def test_no_float_touches_any_money_field_in_family_batches():
    batch = generate_all_family_batches(random.Random(1), SNAPSHOT)
    for line in batch.recon_lines:
        for name in ("debit", "credit", "amount", "fee", "tax"):
            assert isinstance(getattr(line, name), int)
    for entry in batch.ledger_entries:
        for name in ("debit", "credit"):
            assert isinstance(getattr(entry, name), int)
    for gt in batch.ground_truth:
        for entry in gt.expected_journal_entries:
            for leg in entry.legs:
                assert isinstance(leg.debit, int)
                assert isinstance(leg.credit, int)
