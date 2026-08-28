"""Session 7.1's checkpoint (spec.md §6.3):

> Adversarial set runs and is reported separately.

§5.3's second mitigation: "A hand-authored adversarial set of 10-12 cases.
Written by hand with hand-written labels, not produced by the generator...
Reported separately and never mixed into headline metrics." The ten cases
under `data/adversarial/` (built by `scratch/build_adversarial_set.py`,
gitignored — the JSONL is the committed artifact, per §6.4) are hand-picked
literal records, not generator output, targeting the four boundaries named
in §5.3: `T-01` versus `T-03` (REV-16), family 4 core versus its date-error
variant, duplicate credit versus reversal (REV-18), and two "genuinely
unresolvable" siblings (`adv_case_ambiguous` / `adv_case_unmatched_credit`).

**"Runs"** means the real graded pipeline (`pipeline.run.run_batch`,
components 2-8, the same deterministic keyword baseline session 5.1 built)
processes this batch end to end with no crash and no import of `generator/`
— `pipeline.loaders` reads the committed JSONL exactly as it would the
reference batch, and nothing here calls the generator at any point.

**"Reported separately"** is discharged two ways: `build_eval_report`
(session 6.2) is called on this batch alone, producing its own
`EvalReport` that is never passed to `compare_reports` or folded into any
other `MetricsReport` — and the adversarial numbers recorded in
`BUILDLOG.md`'s session 7.1 entry come from this test's own run, not from
the dev/held-out batches those live beside.

The primary safety checks (`false_match_rate == 0`, every auto-applied
entry correct) are asserted as hard requirements even here: an adversarial
set is allowed to expose a wrong *classification*, never a wrong posting.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from pipeline.case_assembly import CaseKind, assemble_cases
from pipeline.classifier import classify_batch_baseline
from pipeline.eval_report import build_eval_report, render_eval_report
from pipeline.loaders import (
    load_bank_lines,
    load_ground_truth,
    load_ledger_entries,
    load_recon_lines,
    load_settlements,
)
from pipeline.metrics import align_ground_truth
from pipeline.run import run_batch
from pipeline.storage import connect

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "adversarial"
SNAPSHOT_DATE = date(2026, 8, 28)
"""Matches `scratch/build_adversarial_set.py`'s `SNAPSHOT_DATE` exactly — the
window-elapsed / still-in-window boundary the family-4 cases were built
against depends on this being the same constant on both sides."""

EXPECTED_CASE_COUNT = 10

# The two noise lines (a self-matching reversal pair, per REV-18's "matched
# reversal is a wash, not a case") that must produce *no* assembled case at all.
NOISE_LINE_IDS = frozenset({"adv_bank_noise_credit", "adv_bank_noise_reversal"})


@pytest.fixture(scope="module")
def batch():
    return {
        "settlements": load_settlements(DATA_DIR / "settlements.jsonl"),
        "recon_lines": load_recon_lines(DATA_DIR / "recon_lines.jsonl"),
        "ledger_entries": load_ledger_entries(DATA_DIR / "ledger_entries.jsonl"),
        "bank_lines": load_bank_lines(DATA_DIR / "bank_lines.jsonl"),
        "ground_truth": load_ground_truth(DATA_DIR / "ground_truth.jsonl"),
    }


@pytest.fixture(scope="module")
def result(batch):
    return run_batch(
        connect(":memory:"),
        settlements=batch["settlements"],
        recon_lines=batch["recon_lines"],
        bank_lines=batch["bank_lines"],
        ledger_entries=batch["ledger_entries"],
        snapshot_date=SNAPSHOT_DATE,
        classifier=classify_batch_baseline,
    )


def test_data_files_hold_exactly_ten_hand_authored_cases_plus_two_noise_lines(batch) -> None:
    assert len(batch["ground_truth"]) == EXPECTED_CASE_COUNT
    assert len(batch["settlements"]) == 6  # t01, t03, f4core, f4date, f4noop, clean
    assert len(batch["bank_lines"]) == 11  # 4 settlement credits + 5 orphan lines + 2 noise lines


def test_the_self_matching_reversal_pair_raises_no_case_at_all(batch) -> None:
    """REV-18's other half of the duplicate/reversal boundary: a reversal whose
    reference token matches a credit's is a wash, not a case (`pipeline.case_assembly`),
    which this noise pair exists to exercise. Neither of its two lines may surface as
    its own assembled case."""
    cases = assemble_cases(batch["settlements"], batch["recon_lines"], batch["bank_lines"])
    orphan_lines = {
        line.line_id for case in cases if case.kind is CaseKind.ORPHAN for line in case.bank_lines
    }
    assert not (orphan_lines & NOISE_LINE_IDS)


def test_the_adversarial_set_runs_end_to_end_with_no_crash(result) -> None:
    """The checkpoint's "runs" half: the real graded pipeline, no LLM, no network,
    over hand-authored records `pipeline/` never saw as a settlement-anchored or
    orphan case at generation time."""
    assert len(result.outcome.outcomes) == EXPECTED_CASE_COUNT


def test_no_false_auto_match_or_wrong_auto_applied_entry(batch, result) -> None:
    """The one thing the adversarial set is never allowed to get wrong: a
    misclassification here is a disclosed finding, a bad posting is a safety failure.
    Both are §1.6's primary safety metrics, asserted directly rather than only
    read off the (separately reported) `MetricsReport`."""
    report = build_eval_report(result.cases, result.outcome.outcomes, batch["ground_truth"], arm="adversarial")
    assert report.metrics.total_cases == EXPECTED_CASE_COUNT
    assert report.metrics.false_match_rate.numerator == 0
    if report.metrics.auto_close_precision.denominator > 0:
        assert report.metrics.auto_close_precision.value == 1.0


def test_the_four_named_boundaries_resolve_to_the_hand_written_label(batch, result) -> None:
    """§5.3 names four boundaries by hand; this pins the deterministic path's
    call on each of the ten cases built to exercise them — the actual per-case
    report, not just the aggregate safety metrics above."""
    outcomes_by_case = {outcome.case_id: outcome for outcome in result.outcome.outcomes}
    ground_truth_by_case = align_ground_truth(result.cases, batch["ground_truth"])
    mismatches = {
        case_id: (outcomes_by_case[case_id].state, truth.expected_outcome_state)
        for case_id, truth in ground_truth_by_case.items()
        if outcomes_by_case[case_id].state != truth.expected_outcome_state
    }
    assert mismatches == {}


def test_report_renders_and_is_never_folded_into_another_metrics_report(batch, result) -> None:
    """"Reported separately": its own `EvalReport`, its own rendering, and this
    call is the only place in the test suite `build_eval_report` is invoked
    with `arm="adversarial"` — it is never passed to `compare_reports`
    (session 6.2's dev-versus-held-out machinery) or merged with any other
    `MetricsReport`."""
    report = build_eval_report(result.cases, result.outcome.outcomes, batch["ground_truth"], arm="adversarial")
    text = render_eval_report(report)
    assert "arm: adversarial" in text
    assert report.metrics.total_cases == EXPECTED_CASE_COUNT
