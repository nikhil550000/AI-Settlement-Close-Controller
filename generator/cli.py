"""`uv run generate` — the generator's CLI entry point (AGENT.md command surface).

Phase 1 (session 1.3) wired up the clean-case path. Session 2.1 added the
five family injections. Session 2.2 added the remaining
settlement-anchored populations (`generator/exceptions.py`) and the four
orphan populations plus non-settlement noise
(`generator/orphans.py`) — the full 150-case reference batch. Session 2.3
adds the step between generating those populations and writing them out:
`generator/finalize.py`'s single global pass over the assembled batch
(the fingerprint control and the UTR narration variety). Nothing is
written to disk that has not been through it.

The snapshot date defaults to a literal constant (Phase 1's own day,
2026-08-28) rather than `datetime.now()` — the determinism rules forbid
deriving it from wall-clock time, and a CLI default must still be a fixed
parameter, not an exemption from that rule.
"""

from __future__ import annotations

import random
from collections import Counter
from datetime import date
from pathlib import Path

import typer

from generator.batch import GeneratedBatch
from generator.clean import generate_clean_batch
from generator.exceptions import EXCEPTION_POPULATIONS
from generator.families import FAMILY_POPULATIONS, N_CASES_PER_FAMILY
from generator.finalize import FinalBatch, finalize_batch
from generator.orphans import ORPHAN_POPULATIONS, generate_noise_bank_lines

app = typer.Typer(add_completion=False)

DEFAULT_SNAPSHOT_DATE = date(2026, 8, 28)

CLEAN_POPULATION = "fully_clean"
"""The first case-allocation row, the one population without its own module."""


@app.command()
def generate(
    seed: int = typer.Option(..., help="RNG seed; the only source of randomness in the run."),
    snapshot_date: str = typer.Option(
        DEFAULT_SNAPSHOT_DATE.isoformat(),
        help="Batch snapshot date, ISO 8601 (YYYY-MM-DD). Never derived from wall-clock time.",
    ),
    n_settlements: int = typer.Option(18, help='Count of "Fully clean" settlements.'),
    n_cases_per_family: int = typer.Option(
        N_CASES_PER_FAMILY, help="Cases per anomaly family (case-allocation table: 10)."
    ),
    out_dir: Path = typer.Option(Path("scratch/generated"), help="Output directory for JSONL files."),
) -> None:
    rng = random.Random(seed)
    snapshot = date.fromisoformat(snapshot_date)
    batch = generate_reference_batch(
        rng, snapshot, n_settlements=n_settlements, n_cases_per_family=n_cases_per_family
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(out_dir / "settlements.jsonl", batch.settlements)
    _write_jsonl(out_dir / "recon_lines.jsonl", batch.recon_lines)
    _write_jsonl(out_dir / "ledger_entries.jsonl", batch.ledger_entries)
    _write_jsonl(out_dir / "bank_lines.jsonl", batch.bank_lines)
    _write_jsonl(out_dir / "ground_truth.jsonl", batch.ground_truth)

    shape_counts = Counter(shape.value for shape in batch.utr_shapes.values())
    typer.echo(
        f"seed={seed} snapshot={snapshot.isoformat()} "
        f"settlements={len(batch.settlements)} recon_lines={len(batch.recon_lines)} "
        f"ledger_entries={len(batch.ledger_entries)} bank_lines={len(batch.bank_lines)} "
        f"ground_truth_cases={len(batch.ground_truth)} "
        f"utr_shapes={dict(sorted(shape_counts.items()))} "
        f"-> {out_dir}"
    )


def generate_reference_batch(
    rng: random.Random,
    snapshot_date: date,
    *,
    n_settlements: int = 18,
    n_cases_per_family: int = N_CASES_PER_FAMILY,
) -> FinalBatch:
    """The whole batch: every settlement-anchored and orphan population, then the global pass over all of it.

    One function so that the checkpoint tests assemble the batch exactly
    as the command does — a fingerprint assertion against a
    differently-assembled batch would be asserting nothing.

    Populations are enumerated from the three registries rather than
    through the `generate_all_*` aggregates so that each one's name
    travels with its records into `FinalBatch.population_of`. The
    registries are what the aggregates loop over, so the RNG draw order is
    identical either way.
    """
    parts: list[tuple[str, GeneratedBatch]] = [
        (CLEAN_POPULATION, generate_clean_batch(rng, snapshot_date, n_settlements=n_settlements))
    ]
    parts += [(name, generate(rng, snapshot_date, n_cases_per_family)) for name, generate in FAMILY_POPULATIONS]
    parts += [(name, generate(rng, snapshot_date)) for name, generate in EXCEPTION_POPULATIONS]
    parts += [(name, generate(rng, snapshot_date)) for name, generate in ORPHAN_POPULATIONS]
    return finalize_batch(
        rng, parts=parts, noise_bank_lines=generate_noise_bank_lines(rng, snapshot_date)
    )


def _write_jsonl(path: Path, records: list) -> None:
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(record.model_dump_json())
            f.write("\n")


if __name__ == "__main__":
    app()
