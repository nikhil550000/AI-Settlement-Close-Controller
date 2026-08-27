"""`uv run generate` — the generator's CLI entry point (AGENT.md command surface).

Phase 1 (session 1.3) wired up the clean-case path. Session 2.1 adds the
five FR-04 family injections (§3.5's case-allocation rows for families
1-5, 10 cases each). Exception/tax/ambiguous/orphan populations and
bank-line generation remain Phase 2.2/3 territory. The snapshot date
defaults to a literal constant (Phase 1's own day, 2026-08-28) rather than
`datetime.now()` — the determinism rules forbid deriving it from
wall-clock time, and a CLI default must still be a fixed parameter, not an
exemption from that rule.
"""

from __future__ import annotations

import random
from datetime import date
from pathlib import Path

import typer

from generator.clean import generate_clean_batch
from generator.families import N_CASES_PER_FAMILY, generate_all_family_batches

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

    settlements = clean_batch.settlements + family_batch.settlements
    recon_lines = clean_batch.recon_lines + family_batch.recon_lines
    ledger_entries = clean_batch.ledger_entries + family_batch.ledger_entries

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(out_dir / "settlements.jsonl", settlements)
    _write_jsonl(out_dir / "recon_lines.jsonl", recon_lines)
    _write_jsonl(out_dir / "ledger_entries.jsonl", ledger_entries)
    _write_jsonl(out_dir / "ground_truth.jsonl", family_batch.ground_truth)

    typer.echo(
        f"seed={seed} snapshot={snapshot.isoformat()} "
        f"settlements={len(settlements)} recon_lines={len(recon_lines)} "
        f"ledger_entries={len(ledger_entries)} ground_truth_cases={len(family_batch.ground_truth)} "
        f"-> {out_dir}"
    )


def _write_jsonl(path: Path, records: list) -> None:
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(record.model_dump_json())
            f.write("\n")


if __name__ == "__main__":
    app()
