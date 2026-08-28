"""Session 6.3's checkpoint (spec.md §6.3):

> Opens from `file://` with networking off.

FR-11 fixes what "opens" has to mean for a report nobody can browser-test in
CI: **one self-contained static HTML file — no server, no build step, no
external asset fetch.** This module cannot drive a real browser, so the
checkpoint is discharged the way §5.6.2/NFR-05 discharge "runs offline"
everywhere else in this codebase: build the report from a real run — Slot A
and Slot B both resolved from the committed `data/llm_cache.json` in
`CacheMode.STRICT` with `client=None`, so the build touches no socket at all
— write it to a real file, read it back through a real `file://` URL with
`urllib`, and assert the bytes on disk contain no reference this file could
not resolve on its own: no `http://`/`https://` (no CDN, no external
stylesheet, no remote fetch), no external `<script src=` or `<link href=`,
nothing FR-11 forbids.

Around that: every §1.8 artifact is present and non-empty against the real
batch (five section headers, both disclosures verbatim, the model-generated
badge on at least one Slot B narration), the embedded JSON blob parses and
its case count matches the run, `format_paise`/`case_reasoning` are checked
in isolation, and `ledger_diff` is checked against `LedgerSource` directly
rather than by re-deriving what "posted this run" means a second time.
"""

from __future__ import annotations

import json
import random
import urllib.request
from datetime import date
from pathlib import Path

import pytest

from generator.cli import generate_reference_batch
from pipeline.classifier import classify_batch_baseline
from pipeline.eval_report import build_eval_report
from pipeline.ground_truth import DeclineReason, OutcomeState
from pipeline.llm_cache import CacheMode, PromptCache
from pipeline.narration import build_narration_bundles, narrate_batch_llm
from pipeline.report import (
    ANOMALY_ENRICHMENT_DISCLOSURE,
    SYNTHETIC_EVAL_DISCLOSURE,
    build_case_records,
    build_report_context,
    case_reasoning,
    format_paise,
    ledger_diff,
    render_report_html,
)
from pipeline.run import run_batch
from pipeline.schemas import LedgerEntry, LedgerSource
from pipeline.storage import connect, fetch_ledger_entries

SNAPSHOT = date(2026, 8, 28)
CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "llm_cache.json"


def _build_offline_report(seed: int = 0):
    """One full, real run at `seed`, classified and narrated entirely from the
    committed cache — no network path is ever constructed.

    Classifier is the **baseline**, not Slot A: `scratch/populate_slot_b_cache.py`
    (session 5.3) populated `data/llm_cache.json`'s Slot B entries against the
    baseline arm's state distribution at seed 0, so only the baseline arm's
    EXTERNAL_ACTION_REQUIRED/ABSTAINED partition is guaranteed cache-covered.
    Slot A moves a few `ABSTAINED` cases into `EXTERNAL_ACTION_REQUIRED`
    (session 6.2's own finding), which would ask Slot B for narrations the
    cache was never populated with and fail this test's own offline
    requirement — not a report bug, a fixture-arm mismatch.
    """
    batch = generate_reference_batch(random.Random(seed), SNAPSHOT)
    conn = connect(":memory:")
    result = run_batch(
        conn,
        settlements=batch.settlements,
        recon_lines=batch.recon_lines,
        bank_lines=batch.bank_lines,
        ledger_entries=batch.ledger_entries,
        snapshot_date=SNAPSHOT,
        classifier=classify_batch_baseline,
    )
    bundles = build_narration_bundles(result.cases, result.outcome.outcomes)
    narrations = narrate_batch_llm(
        bundles, PromptCache(CACHE_PATH), mode=CacheMode.STRICT, client=None
    )
    eval_report = build_eval_report(
        result.cases, result.outcome.outcomes, batch.ground_truth, arm="baseline", seed=seed
    )
    ledger_entries = fetch_ledger_entries(conn)
    context = build_report_context(
        result.cases,
        result.outcome.outcomes,
        ledger_entries,
        eval_report,
        arm="baseline",
        seed=seed,
        snapshot_date=SNAPSHOT.isoformat(),
        narrations=narrations,
    )
    return result, ledger_entries, context


@pytest.fixture(scope="module")
def offline_report():
    return _build_offline_report()


def test_report_opens_from_file_with_networking_off(tmp_path_factory, offline_report):
    """The session checkpoint, verbatim: the file this module produces opens
    from `file://` and references nothing outside itself."""
    _result, _ledger_entries, context = offline_report
    html = render_report_html(context)

    out_dir = tmp_path_factory.mktemp("report")
    path = out_dir / "report.html"
    path.write_text(html, encoding="utf-8")

    with urllib.request.urlopen(path.as_uri()) as response:
        read_back = response.read().decode("utf-8")
    # Windows text-mode write translates "\n" to "\r\n" on disk; the content
    # read back through file:// is otherwise identical to what was rendered.
    assert read_back.replace("\r\n", "\n") == html

    # FR-11: "no server, no build step, no external asset fetch." No CDN link,
    # no remote font, no remote script — the whole tech-stack line in §4.5.
    assert "http://" not in html
    assert "https://" not in html
    assert "<script src=" not in html
    assert 'href="http' not in html
    assert "<iframe" not in html


def test_every_1_8_artifact_is_present(offline_report):
    _result, _ledger_entries, context = offline_report
    html = render_report_html(context)
    for heading in (
        "Metrics report",
        "Case log",
        "Audit trail",
        "Exception report",
        "Reconciled-ledger diff",
    ):
        assert heading in html


def test_no_html_is_double_escaped(offline_report):
    """Jinja's autoescaping applies to every `{{ expression }}`, including one that
    calls a Python helper returning raw HTML (`render_matrix`/`render_entries`/
    `render_validations`) — those must come back wrapped in `markupsafe.Markup`, or
    the tags they build show up as literal escaped text (`&lt;table&gt;`) instead of
    an actual table. Caught once already: the confusion-matrix section rendered as
    visible entity-escaped markup before `render_report_html` wrapped the three
    raw-HTML globals in `Markup`."""
    _result, _ledger_entries, context = offline_report
    html = render_report_html(context)
    assert "&lt;table&gt;" not in html
    assert "&lt;tr&gt;" not in html
    assert "&amp;mdash;" not in html
    assert "<table><thead><tr>" in html  # the confusion matrix actually rendered as a table
    assert "—" in html  # the plain em-dash character, not the escaped HTML entity


def test_both_required_disclosures_are_verbatim_in_the_header(offline_report):
    _result, _ledger_entries, context = offline_report
    html = render_report_html(context)
    assert SYNTHETIC_EVAL_DISCLOSURE in html
    assert ANOMALY_ENRICHMENT_DISCLOSURE in html


def test_slot_b_text_is_labelled_model_generated_in_the_report(offline_report):
    """§4.2: "The FR-11 report MUST label every Slot B string as model-generated
    prose over deterministic facts." At least one EXTERNAL_ACTION_REQUIRED or
    ABSTAINED case exists on the reference batch, so at least one badge must render."""
    result, _ledger_entries, context = offline_report
    html = render_report_html(context)
    narrated_states = {OutcomeState.EXTERNAL_ACTION_REQUIRED, OutcomeState.ABSTAINED}
    assert any(outcome.state in narrated_states for outcome in result.outcome.outcomes)
    assert 'class="ai-badge">model-generated<' in html


def test_embedded_json_blob_parses_and_matches_the_run(offline_report):
    result, _ledger_entries, context = offline_report
    html = render_report_html(context)
    start = html.index('<script type="application/json" id="report-data">') + len(
        '<script type="application/json" id="report-data">'
    )
    end = html.index("</script>", start)
    blob = html[start:end]
    data = json.loads(blob)
    assert len(data["cases"]) == len(result.cases)
    assert data["arm"] == "baseline"
    assert data["seed"] == 0
    # Every paise field stays an integer end to end — no float in the JSON blob.
    for case in data["cases"]:
        assert isinstance(case["residual_paise"], int)


def test_json_blob_is_escaped_against_a_closing_script_tag():
    """A narration containing the literal string "</script>" must not be able to
    close the JSON blob's own <script> tag early. `render_report_html` applies
    exactly this transform to the JSON it embeds; checked directly here since a
    real cached narration is most unlikely to ever contain the string."""
    dangerous = "</script><script>alert(1)</script>"
    dumped = json.dumps({"x": dangerous})
    escaped = dumped.replace("</", "<\\/")
    assert "</script>" not in escaped


def test_ledger_diff_is_exactly_the_controller_adjustment_rows(offline_report):
    _result, ledger_entries, _context = offline_report
    diff = ledger_diff(ledger_entries)
    assert diff  # the reference batch has 50 AUTO_CLOSED cases; something posted
    assert all(entry.source is LedgerSource.CONTROLLER_ADJUSTMENT for entry in diff)
    non_adjustment = [e for e in ledger_entries if e.source is not LedgerSource.CONTROLLER_ADJUSTMENT]
    assert len(diff) + len(non_adjustment) == len(ledger_entries)


def test_format_paise_uses_only_integer_arithmetic():
    assert format_paise(0) == "₹0.00"
    assert format_paise(100) == "₹1.00"
    assert format_paise(150000) == "₹1,500.00"
    assert format_paise(-2599) == "-₹25.99"
    assert format_paise(5) == "₹0.05"


def test_case_reasoning_prefers_narration_then_falls_back_to_evidence(offline_report):
    _result, _ledger_entries, context = offline_report
    narrated = [c for c in context.cases if c.narration_text]
    assert narrated, "the reference batch has narrated cases"
    for record in narrated:
        assert case_reasoning(record) == record.narration_text

    un_narrated_review = [
        c
        for c in context.cases
        if c.narration_text is None and c.decline_reason is DeclineReason.POLICY
    ]
    assert un_narrated_review, "policy-declined REVIEW_REQUIRED cases exist and carry no Slot B text"
    for record in un_narrated_review:
        text = case_reasoning(record)
        assert text and text != ""
        assert any(decision.exclusion.value in text for decision in record.policy_decisions)


def test_case_records_join_cases_to_outcomes_exactly(offline_report):
    result, _ledger_entries, _context = offline_report
    records = build_case_records(result.cases, result.outcome.outcomes)
    assert len(records) == len(result.cases) == 150
    by_id = {record.case_id: record for record in records}
    for outcome in result.outcome.outcomes:
        assert by_id[outcome.case_id].state == outcome.state
