"""Real wall-clock measurement for throughput (scale batch) and
end_to_end_latency (reference batch). Gitignored — a one-off
measurement script, not a test; both figures are "reported, no
target," and `pipeline.metrics.performance_metrics`'s own arithmetic is
already unit-tested (session 6.1, `tests/test_metrics.py`).

Session 7.1: the scale batch (~360 cases / ~12,800 raw records)
only has two dials exposed by `generate_reference_batch` —
`n_settlements` (the "Fully clean" population) and `n_cases_per_family`
(each of the five families) — so those two are scaled up and the
eight exception/tax/ambiguous populations plus the four orphan
populations stay at their fixed counts (57 + 25 = 82 cases).
`n_settlements=80, n_cases_per_family=40` gives 80 + 5*40 + 82 = 362
cases, matching the "~360" target with no new generator code and no change
to any committed population size.

Runs the deterministic path only (`classify_batch_baseline`, session
5.1's keyword baseline) — no network, no LLM cache dependency, matching
the offline requirement for exactly the mode this measurement cares
about (component throughput, not Slot A's).
"""

from __future__ import annotations

import platform
import random
import time
from datetime import date

from generator.cli import generate_reference_batch
from pipeline.classifier import classify_batch_baseline
from pipeline.metrics import performance_metrics
from pipeline.run import run_batch
from pipeline.storage import connect

SNAPSHOT_DATE = date(2026, 8, 28)


def _hardware_string() -> str:
    return f"{platform.system()} {platform.machine()}, {platform.processor() or platform.machine()}, Python {platform.python_version()}"


def _run(*, seed: int, n_settlements: int, n_cases_per_family: int, label: str):
    rng = random.Random(seed)
    batch = generate_reference_batch(rng, SNAPSHOT_DATE, n_settlements=n_settlements, n_cases_per_family=n_cases_per_family)
    case_count = len(batch.ground_truth)

    start = time.perf_counter()
    run_batch(
        connect(":memory:"),
        settlements=batch.settlements,
        recon_lines=batch.recon_lines,
        bank_lines=batch.bank_lines,
        ledger_entries=batch.ledger_entries,
        snapshot_date=SNAPSHOT_DATE,
        classifier=classify_batch_baseline,
    )
    elapsed = time.perf_counter() - start

    hardware = _hardware_string()
    metrics = performance_metrics(case_count=case_count, elapsed_seconds=elapsed, hardware=hardware)
    print(f"[{label}] seed={seed} cases={case_count} raw_records="
          f"{len(batch.settlements) + len(batch.recon_lines) + len(batch.ledger_entries) + len(batch.bank_lines)} "
          f"elapsed={elapsed:.4f}s throughput={metrics.throughput_cases_per_second:.2f} cases/s hardware={hardware!r}")
    return metrics


def main() -> None:
    # end_to_end_latency on the reference batch (seed 0, ~150 cases).
    _run(seed=0, n_settlements=18, n_cases_per_family=10, label="reference (end_to_end_latency)")

    # throughput on the scale batch (seed 3, ~360 cases).
    _run(seed=3, n_settlements=80, n_cases_per_family=40, label="scale (throughput)")


if __name__ == "__main__":
    main()
