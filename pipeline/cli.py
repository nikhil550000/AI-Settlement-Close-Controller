"""`uv run reconcile` — the pipeline's FR-10 CLI entry point.

> **FR-10.** The CLI is the product. A single command runs a batch end to
> end and emits both a console summary and a report file.

Never built until this session (session 7.1's own handoff, and `pipeline/
run.py`'s module docstring: "FR-10's CLI surface, built in a later
session, calls this rather than re-deriving the wiring"). It is the thing
NFR-06's "clean clone reproduces the committed run with a single documented
command" and the Phase 7 checkpoint both name directly, so it is built here
rather than assumed.

**Loads, never generates.** `pipeline/` must never import `generator/`
(§4.1's import guard), so this command reads an already-generated batch off
disk through `pipeline.loaders` — by default the committed reference
dataset at `data/reference/` (FR-12: "the seeded reference dataset, checked
in"). `uv run generate --seed <n>` is the separate, `generator`-side command
that produces a batch in the first place; this one reconciles whatever
batch it is pointed at.

**Slot A and Slot B both run through the committed cache in `--llm-cache
=strict` by default** (§4.3), so the default invocation never touches a
socket and needs no `FIREWORKS_API_KEY` — the same "offline by default"
behaviour every other NFR-05 checkpoint in this codebase relies on.
`--llm-cache=refresh` is the only mode that calls Fireworks, exactly as
`pipeline.llm_cache.CacheMode` already documents.

**Git SHA is never read from the working tree here.** `pipeline.metrics.
RunProvenance`'s own docstring is explicit about why: "a git SHA read from
the working tree at metric time would be a different fact from the SHA of
the committed run." `--git-sha` is therefore a caller-supplied string, left
`None` unless the caller (a human, or the pin step this session also runs)
passes one in.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date
from enum import Enum
from pathlib import Path

import typer

from pipeline.classifier import classify_batch_baseline, classify_batch_hybrid, classify_batch_llm
from pipeline.eval_report import EvalReport, build_eval_report, render_eval_report
from pipeline.llm_cache import CacheMode, PromptCache
from pipeline.llm_client import FIREWORKS_MODEL_ID, FireworksClient
from pipeline.loaders import load_bank_lines, load_ground_truth, load_ledger_entries, load_recon_lines, load_settlements
from pipeline.metrics import MetricsReport, RunProvenance
from pipeline.narration import build_narration_bundles, narrate_batch_llm
from pipeline.report import build_report_context, render_report_html
from pipeline.run import run_batch
from pipeline.storage import connect, fetch_ledger_entries

for _stream in (sys.stdout, sys.stderr):
    # Windows' default console codepage (cp1252) cannot encode the "§"/"×"/"—"
    # characters `pipeline.eval_report`'s plain-text rendering uses, and this is
    # the first place in the codebase that prints that rendering to a real
    # console rather than a file — sessions 6.3/7.1 hit the same trap writing
    # scratch scripts and worked around it with `PYTHONIOENCODING=utf-8`
    # externally; a command FR-10 calls "the product" cannot depend on the
    # caller having set that first. `reconfigure` is a no-op-safe best effort:
    # a stream that does not support it (a captured pipe in some environments)
    # is left alone rather than raising.
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass

app = typer.Typer(add_completion=False)

DEFAULT_DATA_DIR = Path("data/reference")
"""Session 7.2's committed reference dataset — the same seed-0, 2026-08-28
batch every prior session's checkpoint measurements already use."""

DEFAULT_SNAPSHOT_DATE = date(2026, 8, 28)
"""Must match the snapshot date the pointed-at batch was generated with —
it is what the matcher's T+2 window and the family-4 no-op split are
computed against (§3.3). A parameter, per AGENT.md's determinism rules;
never `datetime.now()`."""

DEFAULT_SEED = 0
"""Provenance metadata only. This command never generates a batch — see the
module docstring — so `--seed` records what seed `data_dir` was generated
with rather than driving any randomness here."""


class ClassifierArm(str, Enum):
    """Which of §4.2's two Slot A arms classifies this run. Matches the
    `arm` strings `pipeline.eval_report`/`pipeline.report` already use
    (`"baseline"`, `"slot_a"`) so a run's console output, report and
    `MetricsReport.provenance` never disagree about which arm produced it."""

    BASELINE = "baseline"
    LLM = "llm"
    HYBRID = "hybrid"

    @property
    def arm_name(self) -> str:
        """The `arm` string `pipeline.eval_report`/`pipeline.report` expect —
        `"slot_a"` for `LLM`, matching every existing test and BUILDLOG entry
        (session 6.2 onward). Kept distinct from `.value` (`"llm"`) because
        `--classifier llm` is the CLI's own, more readable flag spelling."""
        return {
            ClassifierArm.BASELINE: "baseline",
            ClassifierArm.LLM: "slot_a",
            ClassifierArm.HYBRID: "hybrid",
        }[self]


def _load_batch(data_dir: Path):
    return (
        load_settlements(data_dir / "settlements.jsonl"),
        load_recon_lines(data_dir / "recon_lines.jsonl"),
        load_bank_lines(data_dir / "bank_lines.jsonl"),
        load_ledger_entries(data_dir / "ledger_entries.jsonl"),
        load_ground_truth(data_dir / "ground_truth.jsonl"),
    )


def run_reconciliation(
    *,
    data_dir: Path,
    snapshot_date: date,
    classifier_arm: ClassifierArm,
    cache_path: Path,
    cache_mode: CacheMode,
    seed: int | None,
    git_sha: str | None,
) -> tuple[EvalReport, str]:
    """Components 2-9 end to end over `data_dir`, plus the FR-11 report HTML.

    The one function both `reconcile` (the CLI command) and the clean-clone
    reproduce test call, for the same reason `pipeline.run.run_batch` is the
    one function the CLI and every other test call: a checkpoint that
    exercises a second, hand-rolled copy of the wiring is not exercising the
    real command.

    Returns the built `EvalReport` (provenance filled in) and the rendered
    FR-11 HTML — the two artifacts `reconcile` writes to disk.
    """
    settlements, recon_lines, bank_lines, ledger_entries, ground_truth = _load_batch(data_dir)

    conn: sqlite3.Connection = connect(":memory:")
    cache = PromptCache(cache_path)
    client = FireworksClient() if cache_mode is CacheMode.REFRESH else None

    if classifier_arm is ClassifierArm.BASELINE:
        classifier = classify_batch_baseline
    elif classifier_arm is ClassifierArm.HYBRID:
        classifier = lambda bundles: classify_batch_hybrid(
            bundles, cache, mode=cache_mode, client=client, on_cache_miss="fallback"
        )
    else:
        classifier = lambda bundles: classify_batch_llm(
            bundles, cache, mode=cache_mode, client=client, on_cache_miss="fallback"
        )

    result = run_batch(
        conn,
        settlements=settlements,
        recon_lines=recon_lines,
        bank_lines=bank_lines,
        ledger_entries=ledger_entries,
        snapshot_date=snapshot_date,
        classifier=classifier,
    )

    narration_bundles = build_narration_bundles(result.cases, result.outcome.outcomes)
    narrations = narrate_batch_llm(
        narration_bundles, cache, mode=cache_mode, client=client, on_cache_miss="fallback"
    )

    provenance = RunProvenance(
        seed=seed,
        git_sha=git_sha,
        model_id=FIREWORKS_MODEL_ID,
        cache_hit_rate=cache.hit_rate,
        snapshot_date=snapshot_date.isoformat(),
    )
    eval_report = build_eval_report(
        result.cases,
        result.outcome.outcomes,
        ground_truth,
        arm=classifier_arm.arm_name,
        seed=seed,
        provenance=provenance,
    )

    ledger_after = fetch_ledger_entries(conn)
    context = build_report_context(
        result.cases,
        result.outcome.outcomes,
        ledger_after,
        eval_report,
        arm=classifier_arm.arm_name,
        snapshot_date=snapshot_date.isoformat(),
        seed=seed,
        narrations=narrations,
    )
    return eval_report, render_report_html(context)


def _console_summary(eval_report: EvalReport) -> str:
    """FR-10's "console summary" half. `pipeline.eval_report.render_eval_report`
    already renders the confusion matrices, the per-subtype breakdown and the
    §5.5 threshold review as plain text — this command's own job is only to
    print that, plus where the report file landed, never to recompute a figure."""
    return render_eval_report(eval_report)


@app.command()
def reconcile(
    data_dir: Path = typer.Option(
        DEFAULT_DATA_DIR, help="Directory holding the five canonical JSONL files (pipeline.loaders)."
    ),
    snapshot_date: str = typer.Option(
        DEFAULT_SNAPSHOT_DATE.isoformat(),
        help="Batch snapshot date, ISO 8601. Must match the date `data_dir` was generated with.",
    ),
    classifier: ClassifierArm = typer.Option(
        ClassifierArm.BASELINE.value,
        help="Classification arm: 'baseline' (deterministic triggers + keyword read), "
        "'hybrid' (triggers win; Slot A decides only the untriggered orphan split), "
        "or 'llm' (Slot A over all eight labels, §4.2). Default is whichever arm "
        "measures best on the reference batch — see README's arm comparison.",
    ),
    cache_path: Path = typer.Option(Path("data/llm_cache.json"), help="§4.3 SHA-keyed prompt/response cache."),
    cache_mode: CacheMode = typer.Option(
        CacheMode.STRICT.value,
        help="strict: cache miss is a hard error, no network (NFR-05 default). "
        "refresh: calls Fireworks on a miss and writes the cache.",
    ),
    seed: int = typer.Option(DEFAULT_SEED, help="Provenance only — the seed `data_dir` was generated with."),
    git_sha: str = typer.Option(
        None, help="Provenance only — the git SHA of the code that produced this run (FR-13)."
    ),
    out_dir: Path = typer.Option(
        Path(".run"), help="Where report.html and metrics.json are written. Gitignored by default; "
        "pass --out-dir data to reproduce the committed pinned artifacts in place."
    ),
) -> None:
    """Run components 2-9 end to end over `data_dir` and emit both a console
    summary and `report.html` (FR-11) plus `metrics.json` (FR-13) under `out_dir`."""
    eval_report, html = run_reconciliation(
        data_dir=data_dir,
        snapshot_date=date.fromisoformat(snapshot_date),
        classifier_arm=classifier,
        cache_path=cache_path,
        cache_mode=cache_mode,
        seed=seed,
        git_sha=git_sha,
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "report.html"
    metrics_path = out_dir / "metrics.json"
    report_path.write_text(html, encoding="utf-8")
    metrics_path.write_text(metrics_json(eval_report.metrics), encoding="utf-8")

    typer.echo(_console_summary(eval_report))
    typer.echo(f"\nreport:  {report_path}")
    typer.echo(f"metrics: {metrics_path}")


def metrics_json(metrics: MetricsReport) -> str:
    """`MetricsReport` as the exact bytes committed to `data/metrics.json`.

    `sort_keys=True` (matching `pipeline.llm_cache.PromptCache._save` and
    `pipeline.report.render_report_html`'s own JSON blob) is what makes two
    runs against the same committed inputs produce byte-identical files —
    NFR-01/NFR-06's "reproduce identical metrics on a clean clone" is a
    claim about these exact bytes, not about the numbers read out of them.
    """
    return json.dumps(metrics.model_dump(mode="json"), indent=2, sort_keys=True, ensure_ascii=True) + "\n"


if __name__ == "__main__":
    app()
