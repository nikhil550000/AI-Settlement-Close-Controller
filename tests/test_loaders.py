"""`pipeline/loaders.py`: JSONL round trip for the four §3.1 schemas plus §1.6 ground truth."""

from __future__ import annotations

import random
from datetime import date
from pathlib import Path

from generator.cli import generate_reference_batch
from pipeline.loaders import (
    load_bank_lines,
    load_ground_truth,
    load_ledger_entries,
    load_recon_lines,
    load_settlements,
)

SNAPSHOT = date(2026, 8, 28)


def _write_jsonl(path: Path, records: list) -> None:
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(record.model_dump_json())
            f.write("\n")


def test_loaders_round_trip_the_full_reference_batch(tmp_path: Path):
    rng = random.Random(0)
    batch = generate_reference_batch(rng, SNAPSHOT)

    _write_jsonl(tmp_path / "settlements.jsonl", batch.settlements)
    _write_jsonl(tmp_path / "recon_lines.jsonl", batch.recon_lines)
    _write_jsonl(tmp_path / "ledger_entries.jsonl", batch.ledger_entries)
    _write_jsonl(tmp_path / "bank_lines.jsonl", batch.bank_lines)
    _write_jsonl(tmp_path / "ground_truth.jsonl", batch.ground_truth)

    assert load_settlements(tmp_path / "settlements.jsonl") == batch.settlements
    assert load_recon_lines(tmp_path / "recon_lines.jsonl") == batch.recon_lines
    assert load_ledger_entries(tmp_path / "ledger_entries.jsonl") == batch.ledger_entries
    assert load_bank_lines(tmp_path / "bank_lines.jsonl") == batch.bank_lines
    assert load_ground_truth(tmp_path / "ground_truth.jsonl") == batch.ground_truth


def test_loaders_skip_blank_lines(tmp_path: Path):
    path = tmp_path / "settlements.jsonl"
    settlement = generate_reference_batch(random.Random(0), SNAPSHOT).settlements[0]
    path.write_text(settlement.model_dump_json() + "\n\n", encoding="utf-8")
    assert load_settlements(path) == [settlement]
