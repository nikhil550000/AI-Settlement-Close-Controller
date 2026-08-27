"""`uv run generate` — the generator's CLI entry point (AGENT.md command surface).

Phase 1 (session 1.3) wires up only the clean-case path; anomaly
populations, orphan cases, and bank-line generation arrive in Phase 2/3.
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
    out_dir: Path = typer.Option(Path("scratch/generated"), help="Output directory for JSONL files."),
) -> None:
    rng = random.Random(seed)
    snapshot = date.fromisoformat(snapshot_date)
    batch = generate_clean_batch(rng, snapshot, n_settlements=n_settlements)

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(out_dir / "settlements.jsonl", batch.settlements)
    _write_jsonl(out_dir / "recon_lines.jsonl", batch.recon_lines)
    _write_jsonl(out_dir / "ledger_entries.jsonl", batch.ledger_entries)

    typer.echo(
        f"seed={seed} snapshot={snapshot.isoformat()} "
        f"settlements={len(batch.settlements)} recon_lines={len(batch.recon_lines)} "
        f"ledger_entries={len(batch.ledger_entries)} -> {out_dir}"
    )


def _write_jsonl(path: Path, records: list) -> None:
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(record.model_dump_json())
            f.write("\n")


if __name__ == "__main__":
    app()
