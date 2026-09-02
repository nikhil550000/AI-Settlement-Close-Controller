"""The held-out-vocabulary ablation, pinned as a test.

README's headline table is the strongest claim this repository makes:

| batch | `--semantics keyword` | `--semantics llm` |
|---|---|---|
| `data/reference/` | 150/150, macro P/R 1.0000 | 150/150, macro P/R 1.0000 |
| `data/heldout_vocab/` | **cannot complete a run** | 150/150, macro P/R 1.0000 |

A claim that only holds in whoever ran it last's terminal is not evidence, and
this file is the guard that keeps it honest in both directions. It asserts the
*failure* as hard as the success: if someone later widens
`pipeline.semantics.GATEWAY_MARKER` to cover `RZRPAY`, the keyword arm starts
completing this batch, `test_the_keyword_arm_cannot_assemble_the_held_out_vocabulary_batch`
goes red, and the ablation has to be re-measured and re-stated rather than
silently becoming a comparison of two arms that now agree.

Everything here runs offline against the committed `data/semantics_cache.json`
in `CacheMode.STRICT` with `client=None`, the same way every other NFR-05
checkpoint in this codebase discharges its claim against a real artifact rather
than a stub.

**What this ablation does and does not establish.** The two batches share their
arithmetic, their case allocation and their answer key exactly — only the
surface strings differ (`tools/heldout_vocabulary.py`). So it measures
generalization across *vocabulary* and nothing else. It is not an independent
draw, and a batch whose correct label is genuinely undecidable from structure
alone is still unbuilt; README's Known limitations says both.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from pipeline.classifier import classify_batch_baseline
from pipeline.llm_cache import CacheMode, PromptCache
from pipeline.loaders import (
    load_bank_lines,
    load_ground_truth,
    load_ledger_entries,
    load_recon_lines,
    load_settlements,
)
from pipeline.metrics import MetricsError, compute_metrics
from pipeline.run import run_batch
from pipeline.semantics import KEYWORD, LlmSemantics
from pipeline.storage import connect

REPO_ROOT = Path(__file__).resolve().parent.parent
BATCH_DIR = REPO_ROOT / "data" / "heldout_vocab"
REFERENCE_DIR = REPO_ROOT / "data" / "reference"
SEMANTICS_CACHE = REPO_ROOT / "data" / "semantics_cache.json"
SNAPSHOT = date(2026, 8, 28)


def _load(data_dir: Path):
    return (
        load_settlements(data_dir / "settlements.jsonl"),
        load_recon_lines(data_dir / "recon_lines.jsonl"),
        load_bank_lines(data_dir / "bank_lines.jsonl"),
        load_ledger_entries(data_dir / "ledger_entries.jsonl"),
        load_ground_truth(data_dir / "ground_truth.jsonl"),
    )


def _llm_semantics() -> LlmSemantics:
    """Slot-equivalent of every other offline checkpoint: the committed cache,
    strict mode, and `client=None` so a miss cannot silently become a call."""
    return LlmSemantics(PromptCache(SEMANTICS_CACHE), mode=CacheMode.STRICT, client=None)


def _run(data_dir: Path, semantics):
    settlements, recon_lines, bank_lines, ledger_entries, ground_truth = _load(data_dir)
    conn: sqlite3.Connection = connect(":memory:")
    result = run_batch(
        conn,
        settlements=settlements,
        recon_lines=recon_lines,
        bank_lines=bank_lines,
        ledger_entries=ledger_entries,
        snapshot_date=SNAPSHOT,
        classifier=lambda bundles: classify_batch_baseline(bundles, semantics),
        semantics=semantics,
    )
    return compute_metrics(result.cases, result.outcome.outcomes, ground_truth)


def test_the_held_out_batch_shares_the_reference_batch_answer_key_exactly() -> None:
    """The ablation is only fair if nothing but the words moved.

    `tools/build_heldout_vocab_batch.py` copies `ground_truth.jsonl` and
    `settlements.jsonl` byte-for-byte; this asserts it, because a rewrite that
    quietly changed a label would turn a generalization test into an easier
    batch and every number below would mean nothing.
    """
    for name in ("ground_truth.jsonl", "settlements.jsonl"):
        assert (BATCH_DIR / name).read_bytes() == (REFERENCE_DIR / name).read_bytes()


def test_the_surface_strings_actually_differ() -> None:
    """The other half of fairness: the words must genuinely have moved."""
    assert (BATCH_DIR / "bank_lines.jsonl").read_bytes() != (REFERENCE_DIR / "bank_lines.jsonl").read_bytes()
    narrations = {line.narration for line in load_bank_lines(BATCH_DIR / "bank_lines.jsonl")}
    # The literal every keyword boundary in `pipeline.semantics` was drawn on.
    assert not any("RAZORPAY" in n.upper() for n in narrations)


def test_the_keyword_arm_cannot_assemble_the_held_out_vocabulary_batch() -> None:
    """The failure half of README's table, asserted as hard as the success half.

    `GATEWAY_MARKER` stops separating the gateway from a merchant, case
    assembly mis-splits the 125/25 populations, and `align_ground_truth`
    refuses to score a batch it cannot join. Failing loud is to
    `align_ground_truth`'s credit; scoring 1.0000 on the batch where the same
    code scores nothing is the finding.
    """
    with pytest.raises(MetricsError, match="matches no assembled case"):
        _run(BATCH_DIR, KEYWORD)


def test_the_llm_arm_recovers_the_held_out_vocabulary_batch_completely() -> None:
    """The success half: 150/150, macro precision and recall 1.0000, offline."""
    metrics = _run(BATCH_DIR, _llm_semantics())

    assert metrics.state_prediction_accuracy.numerator == 150
    assert metrics.state_prediction_accuracy.denominator == 150
    assert metrics.exception_subtype_precision_macro.value == pytest.approx(1.0)
    assert metrics.exception_subtype_recall_macro.value == pytest.approx(1.0)
    assert metrics.exception_subtype_precision_macro.subtypes_eligible == 7
    assert metrics.exception_subtype_recall_macro.subtypes_averaged == 7


def test_both_safety_metrics_hold_on_both_arms_of_the_reference_batch() -> None:
    """Swapping the semantics arm must not move a safety metric.

    §1.3's optimization principle ranks a false match worse than a missed one,
    so the two primary safety metrics are the ones that must not depend on
    which arm answered a question about English.
    """
    for semantics in (KEYWORD, _llm_semantics()):
        metrics = _run(REFERENCE_DIR, semantics)
        assert metrics.false_match_rate.numerator == 0
        assert metrics.auto_close_precision.value == pytest.approx(1.0)


def test_the_llm_arm_answers_the_held_out_batch_from_cache_without_falling_back() -> None:
    """A run that fell back to keywords for most answers would produce the same
    headline number for the wrong reason. `LlmSemantics.misses` exists so that
    cannot be assumed away, and here it must be zero: every question this batch
    asks is in the committed cache.
    """
    semantics = _llm_semantics()
    _run(BATCH_DIR, semantics)
    assert semantics.misses == 0
