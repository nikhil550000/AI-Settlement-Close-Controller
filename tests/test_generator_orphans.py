"""Session 2.2 checkpoint (spec.md §6.3), non-settlement-anchored half:
§3.6's four orphan populations (25 cases / 28 bank lines, REV-18's
granularity correction) plus non-settlement noise (~50 lines, no cases).
"""

from __future__ import annotations

import random
import re
from datetime import date

import pytest

from generator.narration import (
    NAMED_COUNTERPARTIES,
    OPAQUE_CREDIT_NARRATIONS,
    REVERSAL_TEMPLATES,
    narration_template,
)
from generator.orphans import (
    N_AMBIGUOUS_ORPHAN,
    N_DUPLICATE_CREDIT_CASES,
    N_NOISE_BANK_CHARGES,
    N_NOISE_REVERSAL_PAIRS,
    N_NOISE_UNRELATED_NEFT,
    N_REVERSAL_UNMATCHED,
    N_UNMATCHED_INBOUND_CREDIT,
    generate_all_orphan_batches,
    generate_ambiguous_orphan_batch,
    generate_duplicate_credit_batch,
    generate_noise_bank_lines,
    generate_reversal_unmatched_batch,
    generate_unmatched_inbound_credit_batch,
)
from pipeline.ground_truth import ExceptionClass, ExceptionSubtype, OutcomeState

SNAPSHOT = date(2026, 8, 28)

_POPULATIONS = {
    "unmatched_inbound_credit": (generate_unmatched_inbound_credit_batch, N_UNMATCHED_INBOUND_CREDIT, N_UNMATCHED_INBOUND_CREDIT),
    "ambiguous_orphan": (generate_ambiguous_orphan_batch, N_AMBIGUOUS_ORPHAN, N_AMBIGUOUS_ORPHAN),
    "reversal_unmatched": (generate_reversal_unmatched_batch, N_REVERSAL_UNMATCHED, N_REVERSAL_UNMATCHED),
    "duplicate_credit": (generate_duplicate_credit_batch, N_DUPLICATE_CREDIT_CASES, N_DUPLICATE_CREDIT_CASES * 2),
}


@pytest.mark.parametrize("name", _POPULATIONS)
def test_population_holds_its_exact_case_and_line_count(name):
    generate, n_cases, n_lines = _POPULATIONS[name]
    batch = generate(random.Random(1), SNAPSHOT)
    assert len(batch.ground_truth) == n_cases
    assert len(batch.bank_lines) == n_lines


def test_all_orphan_batches_combine_to_twenty_five_cases_and_twenty_eight_lines():
    """§3.6's four-population total; REV-18: duplicate credit spans 6 lines across 3 cases, not 6 cases."""
    batch = generate_all_orphan_batches(random.Random(1), SNAPSHOT)
    assert len(batch.ground_truth) == 25
    assert len(batch.bank_lines) == 28
    case_ids = [gt.case_id for gt in batch.ground_truth]
    assert len(case_ids) == len(set(case_ids))
    line_ids = [line.line_id for line in batch.bank_lines]
    assert len(line_ids) == len(set(line_ids))


def test_no_orphan_case_has_a_settlement_recon_or_ledger_record():
    batch = generate_all_orphan_batches(random.Random(1), SNAPSHOT)
    assert batch.settlements == []
    assert batch.recon_lines == []
    assert batch.ledger_entries == []


def test_unmatched_inbound_credit_narration_names_a_counterparty():
    """§4.2: this population versus the opaque one "turns entirely on whether the narration identifies a counterparty"."""
    batch = generate_unmatched_inbound_credit_batch(random.Random(1), SNAPSHOT)
    for line in batch.bank_lines:
        assert any(counterparty in line.narration for counterparty in NAMED_COUNTERPARTIES)
        assert line.deposit_paise > 0
        assert line.withdrawal_paise == 0
    for gt in batch.ground_truth:
        assert gt.expected_outcome_state == OutcomeState.EXTERNAL_ACTION_REQUIRED
        assert gt.ground_truth_exception_subtype == ExceptionSubtype.UNMATCHED_INBOUND_CREDIT


def test_ambiguous_orphan_narration_is_opaque():
    batch = generate_ambiguous_orphan_batch(random.Random(1), SNAPSHOT)
    for line in batch.bank_lines:
        assert line.narration in OPAQUE_CREDIT_NARRATIONS
        assert not any(counterparty in line.narration for counterparty in NAMED_COUNTERPARTIES)
    for gt in batch.ground_truth:
        assert gt.expected_outcome_state == OutcomeState.ABSTAINED
        assert gt.ground_truth_exception_class == ExceptionClass.AMBIGUOUS_CASE


def test_reversal_unmatched_is_a_withdrawal_and_reads_like_the_noise_reversals():
    """§3.6 separates the two reversal populations by evidence — a matching prior credit — never by wording."""
    batch = generate_reversal_unmatched_batch(random.Random(1), SNAPSHOT)
    noise_reversal_templates = {
        narration_template(line.narration)
        for line in generate_noise_bank_lines(random.Random(1), SNAPSHOT)
        if narration_template(line.narration) in REVERSAL_TEMPLATES
    }
    assert noise_reversal_templates, "the noise pairs must contain reversals for the comparison to mean anything"

    for line in batch.bank_lines:
        assert narration_template(line.narration) in REVERSAL_TEMPLATES
        assert line.withdrawal_paise > 0
        assert line.deposit_paise == 0
    for gt in batch.ground_truth:
        assert gt.ground_truth_exception_subtype == ExceptionSubtype.REVERSAL_UNMATCHED


def test_duplicate_credit_pair_shares_utr_and_amount_and_one_case():
    batch = generate_duplicate_credit_batch(random.Random(1), SNAPSHOT)
    assert len(batch.bank_lines) == N_DUPLICATE_CREDIT_CASES * 2
    for i, gt in enumerate(batch.ground_truth):
        line_1, line_2 = batch.bank_lines[2 * i], batch.bank_lines[2 * i + 1]
        assert line_1.narration == line_2.narration
        assert line_1.deposit_paise == line_2.deposit_paise
        assert gt.expected_linked_source_records == (line_1.line_id, line_2.line_id)
        assert gt.ground_truth_exception_subtype == ExceptionSubtype.DUPLICATE_CREDIT


def test_noise_bank_lines_count_and_carry_no_case():
    lines = generate_noise_bank_lines(random.Random(1), SNAPSHOT)
    assert len(lines) == N_NOISE_BANK_CHARGES + N_NOISE_UNRELATED_NEFT + 2 * N_NOISE_REVERSAL_PAIRS
    assert len(lines) == 50
    line_ids = [line.line_id for line in lines]
    assert len(line_ids) == len(set(line_ids))


def test_noise_reversal_pairs_net_to_zero_and_share_a_utr():
    """A credit and its own reversal: a wash the matcher must ignore, paired by the reference they share."""
    lines = generate_noise_bank_lines(random.Random(1), SNAPSHOT)
    reversal_lines = [line for line in lines if narration_template(line.narration) in REVERSAL_TEMPLATES]
    assert len(reversal_lines) == N_NOISE_REVERSAL_PAIRS

    reversal_refs = {ref for line in reversal_lines for ref in _reference_tokens(line.narration)}
    credit_refs = {
        ref
        for line in lines
        if line.deposit_paise > 0
        for ref in _reference_tokens(line.narration)
    }
    assert reversal_refs <= credit_refs  # every reversal has its originating credit in the batch
    for reference in reversal_refs:
        matching = [line for line in lines if reference in _reference_tokens(line.narration)]
        assert len(matching) == 2
        assert sum(line.deposit_paise for line in matching) == sum(line.withdrawal_paise for line in matching)


def _reference_tokens(narration: str) -> set[str]:
    """The UTR-shaped tokens in a narration, found without any §4.6 matcher logic."""
    return set(re.findall(r"[A-Z]{4}[0-9]{12}", narration))


def test_orphan_generation_is_deterministic_given_the_same_seed():
    batch_a = generate_all_orphan_batches(random.Random(1), SNAPSHOT)
    batch_b = generate_all_orphan_batches(random.Random(1), SNAPSHOT)
    assert [b.model_dump() for b in batch_a.bank_lines] == [b.model_dump() for b in batch_b.bank_lines]
    assert [g.model_dump() for g in batch_a.ground_truth] == [g.model_dump() for g in batch_b.ground_truth]

    lines_a = generate_noise_bank_lines(random.Random(2), SNAPSHOT)
    lines_b = generate_noise_bank_lines(random.Random(2), SNAPSHOT)
    assert [b.model_dump() for b in lines_a] == [b.model_dump() for b in lines_b]


def test_no_float_touches_any_bank_line_money_field():
    batch = generate_all_orphan_batches(random.Random(1), SNAPSHOT)
    lines = list(batch.bank_lines) + generate_noise_bank_lines(random.Random(1), SNAPSHOT)
    for line in lines:
        assert isinstance(line.withdrawal_paise, int)
        assert isinstance(line.deposit_paise, int)
        assert isinstance(line.closing_balance_paise, int)
