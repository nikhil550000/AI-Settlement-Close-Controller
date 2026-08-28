"""Session 7.2's FR-10 checkpoint: the CLI `pipeline/cli.py` builds.

Never built until this session — `pipeline/run.py`'s own docstring since
session 4.3 has said "FR-10's CLI surface, built in a later session, calls
this rather than re-deriving the wiring." This module is that later
session, and its own checkpoint is narrower than the Phase 7 checkpoint
(`tests/test_reproduce.py` owns the byte-identical clean-clone claim): only
that the command runs end to end against the committed reference dataset,
touches no network under the default `--llm-cache=strict`, emits both a
console summary and a report file (FR-10's own words), and that
`RunProvenance` actually gets filled in from the flags a caller passes.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from pipeline.cli import ClassifierArm, app, metrics_json, run_reconciliation
from pipeline.llm_cache import CacheMode

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "reference"
CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "llm_cache.json"

runner = CliRunner()


def test_reconcile_runs_offline_end_to_end_and_writes_both_artifacts(tmp_path):
    """FR-10, verbatim: "A single command runs a batch end to end and emits
    both a console summary and a report file." No FIREWORKS_API_KEY needed —
    the default `--llm-cache=strict` never constructs a network path."""
    out_dir = tmp_path / "out"
    result = runner.invoke(app, ["--out-dir", str(out_dir)])
    assert result.exit_code == 0, result.output

    # Console summary: the §5.2 eval-report rendering, on stdout.
    assert "eval report" in result.output
    assert "outcome_state" in result.output
    assert "§5.5 threshold review" in result.output

    report_path = out_dir / "report.html"
    metrics_path = out_dir / "metrics.json"
    assert report_path.exists()
    assert metrics_path.exists()
    assert "AI Settlement Close Controller" in report_path.read_text(encoding="utf-8")
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert metrics["total_cases"] == 150


def test_reconcile_baseline_arm_also_runs_offline(tmp_path):
    """The other of §4.2's two arms — never touches Slot A's model at all,
    but Slot B still does (no deterministic fallback exists for it), which
    is exactly why session 7.1's cache-gap fix (this session) had to cover
    the `llm` arm specifically rather than the baseline's own, already-cached
    EXTERNAL_ACTION_REQUIRED/ABSTAINED partition."""
    out_dir = tmp_path / "out"
    result = runner.invoke(app, ["--classifier", "baseline", "--out-dir", str(out_dir)])
    assert result.exit_code == 0, result.output
    metrics = json.loads((out_dir / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["total_cases"] == 150


def test_two_runs_against_the_committed_data_produce_byte_identical_metrics(tmp_path):
    """NFR-01's claim, exercised directly rather than only through the
    heavier clean-clone reproduce test (`tests/test_reproduce.py`)."""
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    for out in (out_a, out_b):
        result = runner.invoke(app, ["--out-dir", str(out)])
        assert result.exit_code == 0, result.output
    assert (out_a / "metrics.json").read_bytes() == (out_b / "metrics.json").read_bytes()
    assert (out_a / "report.html").read_bytes() == (out_b / "report.html").read_bytes()


def test_run_reconciliation_fills_in_provenance_from_the_flags_it_is_given():
    """`pipeline.metrics.RunProvenance`'s own docstring: every field is
    caller-supplied and defaults to `None` because "session 7.2 owns the
    FR-13 pin." This is that wiring — every field must come from what the
    caller (the CLI, or the reproduce test) passed in, never derived."""
    eval_report, _html = run_reconciliation(
        data_dir=DATA_DIR,
        snapshot_date=__import__("datetime").date(2026, 8, 28),
        classifier_arm=ClassifierArm.LLM,
        cache_path=CACHE_PATH,
        cache_mode=CacheMode.STRICT,
        seed=0,
        git_sha="deadbeef",
    )
    provenance = eval_report.metrics.provenance
    assert provenance.seed == 0
    assert provenance.git_sha == "deadbeef"
    assert provenance.model_id is not None
    assert provenance.snapshot_date == "2026-08-28"
    assert 0.0 <= provenance.cache_hit_rate <= 1.0
    assert eval_report.arm == "slot_a"


def test_metrics_json_is_deterministically_sorted_and_ascii():
    """Matches `pipeline.llm_cache.PromptCache._save`'s own reasoning: sorted
    keys are what make two runs against the same inputs byte-identical, and
    `ensure_ascii` sidesteps the cp1252-console trap this session's console
    summary fix (module-level stream reconfigure) already had to work around."""
    eval_report, _html = run_reconciliation(
        data_dir=DATA_DIR,
        snapshot_date=__import__("datetime").date(2026, 8, 28),
        classifier_arm=ClassifierArm.BASELINE,
        cache_path=CACHE_PATH,
        cache_mode=CacheMode.STRICT,
        seed=0,
        git_sha=None,
    )
    text = metrics_json(eval_report.metrics)
    assert text == json.dumps(json.loads(text), indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    text.encode("ascii")  # raises on any non-ASCII byte


def test_reconcile_refresh_mode_with_no_client_and_a_fully_cached_batch_never_errors(tmp_path):
    """`--llm-cache=refresh` constructs a `FireworksClient` (per `run_reconciliation`),
    which requires `FIREWORKS_API_KEY` — but the committed reference batch is fully
    cached at seed 0 for both arms (this session's own cache-population work), so no
    call is ever actually made. Guards against a future prompt change silently
    depending on refresh mode when strict mode already covers everything."""
    import os

    if not os.environ.get("FIREWORKS_API_KEY"):
        import pytest

        pytest.skip("FIREWORKS_API_KEY not set in this environment; strict-mode coverage is what matters")
    out_dir = tmp_path / "out"
    result = runner.invoke(app, ["--cache-mode", "refresh", "--out-dir", str(out_dir)])
    assert result.exit_code == 0, result.output
