""" checkpoint: "the generator plan and the orphan populations tables assert
exactly; a later revision bank-line split holds" — against the **full** 150-case
reference batch (18 clean + 50 the generator plan families + 57 the generator plan remainder + 25
the orphan populations orphan), assembled the same way `generator/cli.py`'s `generate`
command does.
"""

from __future__ import annotations

import random
from collections import Counter
from datetime import date

from typer.testing import CliRunner

from generator.cli import app
from generator.clean import generate_clean_batch
from generator.exceptions import generate_all_exception_batches
from generator.families import generate_all_family_batches
from generator.orphans import generate_all_orphan_batches, generate_noise_bank_lines
from pipeline.money import Paise

SNAPSHOT = date(2026, 8, 28)

# the generator plan (settlement-anchored) + the orphan populations (orphan) case-allocation tables, transcribed exactly.
_EXPECTED_POPULATION_COUNTS = {
    ("NONE", "NONE", "AUTO_MATCHED"): 18,  # Fully clean
    ("EXPECTED_TIMING_DIFFERENCE", "NONE", "AUTO_MATCHED"): 12,  # Family-4 no-op
    ("ACCOUNTING_CORRECTION", "OMISSION", "AUTO_CLOSED"): 30,  # Families 1, 2, 5 (10 each)
    ("ACCOUNTING_CORRECTION", "MISPOSTING", "AUTO_CLOSED"): 20,  # Families 3, 4 (10 each)
    ("ACCOUNTING_CORRECTION", "MISPOSTING", "REVIEW_REQUIRED"): 5,  # Family-4 date error
    ("ACCOUNTING_CORRECTION", "OMISSION", "REVIEW_REQUIRED"): 12,  # the policy exclusions tax positions
    ("OPERATIONAL_EXCEPTION", "SETTLEMENT_UTR_MISSING", "EXTERNAL_ACTION_REQUIRED"): 5,
    ("OPERATIONAL_EXCEPTION", "BANK_CREDIT_OVERDUE", "EXTERNAL_ACTION_REQUIRED"): 5,
    ("OPERATIONAL_EXCEPTION", "SETTLEMENT_AMOUNT_MISMATCH", "EXTERNAL_ACTION_REQUIRED"): 4,
    ("OPERATIONAL_EXCEPTION", "DISPUTE_PENDING", "EXTERNAL_ACTION_REQUIRED"): 5,
    ("OPERATIONAL_EXCEPTION", "UNMATCHED_INBOUND_CREDIT", "EXTERNAL_ACTION_REQUIRED"): 8,  # orphan
    ("OPERATIONAL_EXCEPTION", "REVERSAL_UNMATCHED", "EXTERNAL_ACTION_REQUIRED"): 6,  # orphan
    ("OPERATIONAL_EXCEPTION", "DUPLICATE_CREDIT", "EXTERNAL_ACTION_REQUIRED"): 3,  # orphan
    ("AMBIGUOUS_CASE", "NONE", "ABSTAINED"): 17,  # 9 settlement-anchored + 8 orphan
}
assert sum(_EXPECTED_POPULATION_COUNTS.values()) == 150

# the generator plan "Batch totals".
_EXPECTED_STATE_TOTALS = {
    "AUTO_MATCHED": 30,
    "AUTO_CLOSED": 50,
    "REVIEW_REQUIRED": 17,
    "EXTERNAL_ACTION_REQUIRED": 36,
    "ABSTAINED": 17,
}
assert sum(_EXPECTED_STATE_TOTALS.values()) == 150


def _full_batch(seed: int):
    rng = random.Random(seed)
    clean_batch = generate_clean_batch(rng, SNAPSHOT, n_settlements=18)
    family_batch = generate_all_family_batches(rng, SNAPSHOT, n_cases_per_family=10)
    exception_batch = generate_all_exception_batches(rng, SNAPSHOT)
    orphan_batch = generate_all_orphan_batches(rng, SNAPSHOT)
    noise_bank_lines = generate_noise_bank_lines(rng, SNAPSHOT)
    return clean_batch, family_batch, exception_batch, orphan_batch, noise_bank_lines


def test_settlement_anchored_total_is_125_and_case_total_is_150():
    clean_batch, family_batch, exception_batch, orphan_batch, _noise = _full_batch(1)
    settlement_anchored = len(clean_batch.settlements) + len(family_batch.settlements) + len(exception_batch.settlements)
    assert settlement_anchored == 125
    total_cases = (
        len(clean_batch.ground_truth)
        + len(family_batch.ground_truth)
        + len(exception_batch.ground_truth)
        + len(orphan_batch.ground_truth)
    )
    assert total_cases == 150


def test_population_counts_match_section_3_5_and_3_6_case_allocation_tables_exactly():
    clean_batch, family_batch, exception_batch, orphan_batch, _noise = _full_batch(1)
    all_ground_truth = (
        list(clean_batch.ground_truth)
        + list(family_batch.ground_truth)
        + list(exception_batch.ground_truth)
        + list(orphan_batch.ground_truth)
    )
    counts = Counter(
        (gt.ground_truth_exception_class.value, gt.ground_truth_exception_subtype.value, gt.expected_outcome_state.value)
        for gt in all_ground_truth
    )
    assert dict(counts) == _EXPECTED_POPULATION_COUNTS


def test_outcome_state_totals_match_batch_totals_table():
    clean_batch, family_batch, exception_batch, orphan_batch, _noise = _full_batch(1)
    all_ground_truth = (
        list(clean_batch.ground_truth)
        + list(family_batch.ground_truth)
        + list(exception_batch.ground_truth)
        + list(orphan_batch.ground_truth)
    )
    state_counts = Counter(gt.expected_outcome_state.value for gt in all_ground_truth)
    assert dict(state_counts) == _EXPECTED_STATE_TOTALS


def test_case_ids_are_unique_across_the_full_batch():
    clean_batch, family_batch, exception_batch, orphan_batch, _noise = _full_batch(1)
    all_ground_truth = (
        list(clean_batch.ground_truth)
        + list(family_batch.ground_truth)
        + list(exception_batch.ground_truth)
        + list(orphan_batch.ground_truth)
    )
    case_ids = [gt.case_id for gt in all_ground_truth]
    assert len(case_ids) == len(set(case_ids)) == 150


def test_bank_line_decomposition_matches_rev_17():
    """A later revision: ~98 settlement credits + ~28 orphan-case lines + ~50 non-settlement noise, ~175 total."""
    clean_batch, family_batch, exception_batch, orphan_batch, noise = _full_batch(1)
    settlement_credit_lines = len(clean_batch.bank_lines) + len(family_batch.bank_lines) + len(exception_batch.bank_lines)
    orphan_lines = len(orphan_batch.bank_lines)
    noise_lines = len(noise)

    assert settlement_credit_lines == 98
    assert orphan_lines == 28
    assert noise_lines == 50
    total = settlement_credit_lines + orphan_lines + noise_lines
    assert 170 <= total <= 180  # a revision's stated "~175"


def test_no_credit_populations_hold_the_27_case_rev_17_membership():
    """A later revision names exactly: 10 family-4 core + 12 family-4 no-op + 5 `BANK_CREDIT_OVERDUE` = 27 no-credit cases."""
    _clean, family_batch, exception_batch, _orphan, _noise = _full_batch(1)
    n_settlement_anchored = len(family_batch.settlements) + len(exception_batch.settlements)
    n_with_credit = len(family_batch.bank_lines) + len(exception_batch.bank_lines)
    assert n_settlement_anchored - n_with_credit == 27


def test_ledger_balances_globally_across_the_full_batch():
    clean_batch, family_batch, exception_batch, _orphan, _noise = _full_batch(1)
    entries = list(clean_batch.ledger_entries) + list(family_batch.ledger_entries) + list(exception_batch.ledger_entries)
    total_debit = sum((e.debit for e in entries), Paise(0))
    total_credit = sum((e.credit for e in entries), Paise(0))
    assert total_debit == total_credit
    assert total_debit > 0


def test_generate_cli_produces_all_five_jsonl_files_and_the_expected_totals(tmp_path):
    runner = CliRunner()
    result = runner.invoke(app, ["--seed", "1", "--out-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    for name in ("settlements", "recon_lines", "ledger_entries", "bank_lines", "ground_truth"):
        assert (tmp_path / f"{name}.jsonl").exists()
    assert "settlements=125" in result.output
    assert "ground_truth_cases=150" in result.output


def test_generate_cli_is_byte_identical_across_two_runs_with_the_same_seed(tmp_path):
    runner = CliRunner()
    out_a, out_b = tmp_path / "a", tmp_path / "b"
    runner.invoke(app, ["--seed", "7", "--out-dir", str(out_a)])
    runner.invoke(app, ["--seed", "7", "--out-dir", str(out_b)])
    for name in ("settlements", "recon_lines", "ledger_entries", "bank_lines", "ground_truth"):
        assert (out_a / f"{name}.jsonl").read_text(encoding="utf-8") == (out_b / f"{name}.jsonl").read_text(encoding="utf-8")
