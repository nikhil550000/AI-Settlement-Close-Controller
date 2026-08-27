"""`uv run generate` — the generator's CLI entry point (AGENT.md command surface).

Phase 1 (session 1.3) wired up the clean-case path. Session 2.1 added the
five FR-04 family injections. Session 2.2 adds the remaining §3.5
settlement-anchored populations (`generator/exceptions.py`), the four
§3.6 orphan populations plus non-settlement noise (`generator/orphans.py`),
and `bank_line` generation throughout — the full 150-case reference batch.
The snapshot date defaults to a literal constant (Phase 1's own day,
2026-08-28) rather than `datetime.now()` — the determinism rules forbid
deriving it from wall-clock time, and a CLI default must still be a fixed
parameter, not an exemption from that rule.
"""

from __future__ import annotations

import random
from datetime import date
from pathlib import Path

import typer

from generator.clean import generate_clean_batch
from generator.exceptions import generate_all_exception_batches
from generator.families import N_CASES_PER_FAMILY, generate_all_family_batches
from generator.orphans import generate_all_orphan_batches, generate_noise_bank_lines

app = typer.Typer(add_completion=False)

DEFAULT_SNAPSHOT_DATE = date(2026, 8, 28)


@app.command()
def generate(
    seed: int = typer.Option(..., help="RNG seed; the only source of randomness in the run."),
    snapshot_date: str = typer.Option(
        DEFAULT_SNAPSHOT_DATE.isoformat(),
        help="Batch snapshot date, ISO 8601 (YYYY-MM-DD). Never derived from wall-clock time.",
    ),
    n_settlements: int = typer.Option(18, help='Count of "Fully clean" settlements (spec.md §3.5).'),
    n_cases_per_family: int = typer.Option(
        N_CASES_PER_FAMILY, help="Cases per FR-04 family (spec.md §3.5 case-allocation table: 10)."
    ),
    out_dir: Path = typer.Option(Path("scratch/generated"), help="Output directory for JSONL files."),
) -> None:
    rng = random.Random(seed)
    snapshot = date.fromisoformat(snapshot_date)
    clean_batch = generate_clean_batch(rng, snapshot, n_settlements=n_settlements)
    family_batch = generate_all_family_batches(rng, snapshot, n_cases_per_family=n_cases_per_family)
    exception_batch = generate_all_exception_batches(rng, snapshot)
    orphan_batch = generate_all_orphan_batches(rng, snapshot)
    noise_bank_lines = generate_noise_bank_lines(rng, snapshot)

    settlements = clean_batch.settlements + family_batch.settlements + exception_batch.settlements
    recon_lines = clean_batch.recon_lines + family_batch.recon_lines + exception_batch.recon_lines
    ledger_entries = clean_batch.ledger_entries + family_batch.ledger_entries + exception_batch.ledger_entries
    bank_lines = (
        clean_batch.bank_lines
        + family_batch.bank_lines
        + exception_batch.bank_lines
        + orphan_batch.bank_lines
        + noise_bank_lines
    )
    ground_truth = clean_batch.ground_truth + family_batch.ground_truth + exception_batch.ground_truth + orphan_batch.ground_truth

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(out_dir / "settlements.jsonl", settlements)
    _write_jsonl(out_dir / "recon_lines.jsonl", recon_lines)
    _write_jsonl(out_dir / "ledger_entries.jsonl", ledger_entries)
    _write_jsonl(out_dir / "bank_lines.jsonl", bank_lines)
    _write_jsonl(out_dir / "ground_truth.jsonl", ground_truth)

    typer.echo(
        f"seed={seed} snapshot={snapshot.isoformat()} "
        f"settlements={len(settlements)} recon_lines={len(recon_lines)} "
        f"ledger_entries={len(ledger_entries)} bank_lines={len(bank_lines)} "
        f"ground_truth_cases={len(ground_truth)} "
        f"-> {out_dir}"
    )


def _write_jsonl(path: Path, records: list) -> None:
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(record.model_dump_json())
            f.write("\n")


if __name__ == "__main__":
    app()
