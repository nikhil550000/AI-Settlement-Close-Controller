"""Phase 1 checkpoint (spec.md §6, line ~902): the three assertions.

1. `generate --seed 1` runs end to end (CLI, via `typer.testing.CliRunner`).
2. `settlement.amount == sum(credits) - sum(debits) - fees - tax` holds on
   every generated settlement.
3. The generated ledger balances globally: `sum(debits) == sum(credits)`,
   in integer paise.
"""

from __future__ import annotations

import random
from collections import defaultdict
from datetime import date

from typer.testing import CliRunner

from generator.cli import app
from generator.clean import generate_clean_batch
from pipeline.money import Paise


def test_generate_cli_runs_end_to_end(tmp_path):
    runner = CliRunner()
    result = runner.invoke(app, ["--seed", "1", "--out-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "settlements.jsonl").exists()
    assert (tmp_path / "recon_lines.jsonl").exists()
    assert (tmp_path / "ledger_entries.jsonl").exists()


def test_settlement_amount_equals_credits_minus_debits_minus_fees_minus_tax():
    rng = random.Random(1)
    batch = generate_clean_batch(rng, date(2026, 8, 28), n_settlements=18)

    lines_by_settlement: dict[str, list] = defaultdict(list)
    for line in batch.recon_lines:
        lines_by_settlement[line.settlement_id].append(line)

    assert len(batch.settlements) == 18
    for settlement in batch.settlements:
        lines = lines_by_settlement[settlement.id]
        assert lines, f"settlement {settlement.id} has no recon lines"
        total_credit = sum((line.credit for line in lines), Paise(0))
        total_debit = sum((line.debit for line in lines), Paise(0))
        total_fee = sum((line.fee for line in lines), Paise(0))
        total_tax = sum((line.tax for line in lines), Paise(0))
        assert settlement.amount == total_credit - total_debit - total_fee - total_tax
        assert settlement.fees == total_fee
        assert settlement.tax == total_tax


def test_ledger_balances_globally():
    rng = random.Random(1)
    batch = generate_clean_batch(rng, date(2026, 8, 28), n_settlements=18)

    total_debit = sum((e.debit for e in batch.ledger_entries), Paise(0))
    total_credit = sum((e.credit for e in batch.ledger_entries), Paise(0))
    assert total_debit == total_credit
    assert total_debit > 0


def test_generation_is_deterministic_given_the_same_seed():
    batch_a = generate_clean_batch(random.Random(1), date(2026, 8, 28), n_settlements=18)
    batch_b = generate_clean_batch(random.Random(1), date(2026, 8, 28), n_settlements=18)
    assert [s.model_dump() for s in batch_a.settlements] == [s.model_dump() for s in batch_b.settlements]
    assert [e.model_dump() for e in batch_a.ledger_entries] == [e.model_dump() for e in batch_b.ledger_entries]


def test_no_float_touches_any_money_field():
    rng = random.Random(1)
    batch = generate_clean_batch(rng, date(2026, 8, 28), n_settlements=18)

    money_fields_recon = ("debit", "credit", "amount", "fee", "tax")
    money_fields_settlement = ("amount", "fees", "tax")
    money_fields_ledger = ("debit", "credit")

    for line in batch.recon_lines:
        for name in money_fields_recon:
            assert isinstance(getattr(line, name), int)
    for settlement in batch.settlements:
        for name in money_fields_settlement:
            assert isinstance(getattr(settlement, name), int)
    for entry in batch.ledger_entries:
        for name in money_fields_ledger:
            assert isinstance(getattr(entry, name), int)
