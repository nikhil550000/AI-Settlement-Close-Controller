"""The contested-credit batch: `data/contested/`, built by `tools/build_contested_set.py`.

**The finding.** FR-09 tier 2 (§4.6) matches a bank credit to a settlement on
an exact amount inside the T+2 window plus one slack day, and that key is not
unique to a settlement. Two settlements of the same amount created the same
day each see exactly one candidate credit, each match it at tier 2, each
report `residual_paise = 0`, and both terminate on the strength of one credit
that can belong to at most one of them. §4.6's tie rule ("a tie is not a
match; it routes to ambiguity") was enforced only *within* one settlement's
candidate list, which is the only tie a per-case cascade can see;
`pipeline.matcher.match_cases` now also resolves the symmetric tie *across*
settlements. This batch is the one that exercises it — the reference batch
never did, because `generator/clean.py` draws amounts lognormally and an exact
collision inside one window is vanishingly rare.

Twelve hand-authored cases in three groups of four: four
`CONTESTED_UNDECIDABLE` (two pairs, one credit each, no discriminator in the
evidence — ground truth `ABSTAINED` on both, because abstaining is the correct
answer and not merely the safe one), four `CONTESTED_DECIDABLE` (the same
shape, but the narration names the settlement's payment-method character, so a
human can resolve it and no rule here can), and four `NOT_CONTESTED` controls
that must still reach `AUTO_MATCHED` at tier 0. The controls are the load-
bearing half of the safety claim: a "fix" that made every settlement abstain
would also show a `false_match_rate` of zero.

**Reported separately**, on the same terms as `tests/test_adversarial.py`:
`build_eval_report` is called on this batch alone with `arm="contested"`, and
the resulting `EvalReport` is never passed to `compare_reports` nor folded
into any other `MetricsReport`.

**What the deterministic keyword arm actually does here, measured.** It
resolves the 4 controls (`AUTO_MATCHED`, correct) and the 2 `CONTESTED_
DECIDABLE` card settlements (`EXTERNAL_ACTION_REQUIRED` / `BANK_CREDIT_
OVERDUE`, correct) — 6 of 12 — and calls the other 6
`EXTERNAL_ACTION_REQUIRED` where ground truth says `ABSTAINED` (4) or
`AUTO_MATCHED` (2). `false_match_rate` is 0/12 and `abstention_rate` is 0/12:
the arm never abstains on this batch, it escalates. Every one of those six is
a *classification* miss on a case the system correctly refused to auto-match,
which is the disclosed trade §1.3 asks for; none is a wrong posting, and no
entry is auto-applied at all.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from pipeline.case_assembly import assemble_cases
from pipeline.classifier import classify_batch_baseline
from pipeline.eval_report import build_eval_report, render_eval_report
from pipeline.ground_truth import OutcomeState
from pipeline.loaders import (
    load_bank_lines,
    load_ground_truth,
    load_ledger_entries,
    load_recon_lines,
    load_settlements,
)
from pipeline.matcher import MatchTier, match_cases, match_settlement_anchored_case
from pipeline.metrics import align_ground_truth
from pipeline.run import run_batch
from pipeline.semantics import KEYWORD
from pipeline.storage import connect

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data" / "contested"

SNAPSHOT_DATE = date(2026, 8, 28)
"""Matches `tools/build_contested_set.py`'s `SNAPSHOT_DATE`, and every other
batch's. The contested settlements are placed *past* their T+2 window against
this date on purpose — see that script's docstring for why an elapsed window
is what makes the demotion visible in the terminal state at all."""

EXPECTED_CASE_COUNT = 12

CONTESTED_CASE_IDS = frozenset(
    {
        "con_setl_undec_a1",
        "con_setl_undec_a2",
        "con_setl_undec_b1",
        "con_setl_undec_b2",
        "con_setl_dec_c_upi",
        "con_setl_dec_c_card",
        "con_setl_dec_d_upi",
        "con_setl_dec_d_card",
    }
)
"""The eight settlements that contend for a credit — two per contested credit."""

CONTROL_CASE_IDS = frozenset(
    {"con_setl_ctrl_1", "con_setl_ctrl_2", "con_setl_ctrl_3", "con_setl_ctrl_4"}
)
"""The four ordinary settlements the contention fix must leave alone."""

_UNDECIDABLE_PAIR = ("con_setl_undec_a1", "con_setl_undec_a2")
_UNDECIDABLE_CREDIT = "con_bank_undec_a"


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
    """The real graded pipeline over this batch, on the deterministic keyword arm.

    `semantics=KEYWORD` is `--semantics keyword` — §5.4's baseline arm — passed
    explicitly rather than left to default, because this batch's whole point is
    what that arm can and cannot read off a bank narration.
    """
    return run_batch(
        connect(":memory:"),
        settlements=batch["settlements"],
        recon_lines=batch["recon_lines"],
        bank_lines=batch["bank_lines"],
        ledger_entries=batch["ledger_entries"],
        snapshot_date=SNAPSHOT_DATE,
        classifier=classify_batch_baseline,
        semantics=KEYWORD,
    )


def test_the_batch_holds_twelve_hand_authored_cases_in_three_groups_of_four(batch) -> None:
    assert len(batch["settlements"]) == EXPECTED_CASE_COUNT
    assert len(batch["ground_truth"]) == EXPECTED_CASE_COUNT
    assert len(batch["bank_lines"]) == 8  # 4 contested credits + 4 control credits
    assert {truth.case_id for truth in batch["ground_truth"]} == CONTESTED_CASE_IDS | CONTROL_CASE_IDS


def test_every_case_survives_the_ground_truth_join(batch, result) -> None:
    """`align_ground_truth`'s join rules, satisfied — or the batch cannot be scored
    at all and every §1.6 denominator is silently wrong (`MetricsError`'s reason
    for existing)."""
    aligned = align_ground_truth(result.cases, batch["ground_truth"])
    assert set(aligned) == {case.case_id for case in result.cases}
    assert len(aligned) == EXPECTED_CASE_COUNT


def test_the_contested_batch_runs_end_to_end_with_no_crash(result) -> None:
    assert len(result.outcome.outcomes) == EXPECTED_CASE_COUNT


def test_false_match_rate_is_zero_over_all_twelve_cases(batch, result) -> None:
    """The headline safety assertion, and the whole reason this batch exists.

    Before the cross-settlement tie rule, the eight contested settlements
    matched at tier 2 on four credits, fired no `BANK_CREDIT_OVERDUE` trigger,
    and — their books being accrual-correct — reached `AUTO_MATCHED` on evidence
    that could support at most half of them. That is four false matches on this
    batch, against §1.6's primary safety metric for matching.
    """
    report = build_eval_report(result.cases, result.outcome.outcomes, batch["ground_truth"], arm="contested")
    assert report.metrics.total_cases == EXPECTED_CASE_COUNT
    assert report.metrics.false_match_rate.numerator == 0
    assert report.metrics.false_match_rate.denominator == EXPECTED_CASE_COUNT
    # No entry is auto-applied anywhere in this batch, so the adjustment-side
    # safety metric has no denominator here — asserted as undefined rather than
    # read as a passing 1.00 (`Rate`'s zero-denominator rule).
    assert report.metrics.auto_applied_entry_count == 0
    assert report.metrics.auto_close_precision.value is None


def test_the_four_uncontested_controls_still_match_at_tier_zero(result) -> None:
    """The other half of the safety claim: the fix must not make everything abstain.

    Each control carries its UTR as a whitespace-delimited word — §4.6 tier 0's
    `CLEAN` shape — and a distinct amount, so it never reaches tier 2 and can
    never be contended for.
    """
    outcomes = {outcome.case_id: outcome for outcome in result.outcome.outcomes}
    cases = {case.case_id: case for case in result.cases}
    for case_id in sorted(CONTROL_CASE_IDS):
        assert cases[case_id].match_tier == int(MatchTier.UTR_EXACT), case_id
        assert len(cases[case_id].bank_lines) == 1, case_id
        assert outcomes[case_id].state is OutcomeState.AUTO_MATCHED, case_id


def test_every_contested_settlement_is_demoted_to_tier_three_with_no_bank_line(result) -> None:
    """The contention fix, observed: every claimant of a contended credit falls
    back to §4.6 tier 3 and releases the line it had claimed. Eight settlements,
    four credits, and not one credit left attached to either claimant."""
    cases = {case.case_id: case for case in result.cases}
    for case_id in sorted(CONTESTED_CASE_IDS):
        assert cases[case_id].match_tier == int(MatchTier.NO_MATCH), case_id
        assert cases[case_id].bank_lines == (), case_id


def test_no_bank_line_is_claimed_by_more_than_one_case(result) -> None:
    """The invariant the old behaviour violated, over the whole batch."""
    claims: dict[str, list[str]] = {}
    for case in result.cases:
        for line in case.bank_lines:
            claims.setdefault(line.line_id, []).append(case.case_id)
    contended = {line_id: holders for line_id, holders in claims.items() if len(holders) > 1}
    assert contended == {}


def test_regression_the_old_double_claim_is_gone(batch) -> None:
    """The direct regression pin, on the smallest batch that exhibits it.

    Two settlements of one amount, one credit, and nothing else. The per-case
    cascade — `match_settlement_anchored_case`, which is all `match_cases` used
    to be — still hands the same line to both, because from inside either case
    there is exactly one candidate and no tie to see. That call is made here
    deliberately: it is the old behaviour, exhibited rather than described, so
    this test fails if `match_cases` ever loses its batch-wide pass.
    """
    settlements = [s for s in batch["settlements"] if s.id in _UNDECIDABLE_PAIR]
    recon_lines = [line for line in batch["recon_lines"] if line.settlement_id in _UNDECIDABLE_PAIR]
    bank_lines = [line for line in batch["bank_lines"] if line.line_id == _UNDECIDABLE_CREDIT]
    assert len(settlements) == 2 and len(bank_lines) == 1
    assert settlements[0].amount == settlements[1].amount
    assert settlements[0].utr != settlements[1].utr

    cases = assemble_cases(settlements, recon_lines, bank_lines, semantics=KEYWORD)

    per_case = [
        match_settlement_anchored_case(case, bank_lines, snapshot_date=SNAPSHOT_DATE) for case in cases
    ]
    assert all(case.match_tier == int(MatchTier.AMOUNT_AND_WINDOW) for case in per_case)
    assert [line.line_id for case in per_case for line in case.bank_lines] == [
        _UNDECIDABLE_CREDIT,
        _UNDECIDABLE_CREDIT,
    ], "the per-case cascade alone still double-claims — that is the bug being fixed"

    resolved = match_cases(cases, bank_lines, snapshot_date=SNAPSHOT_DATE)
    claimants: dict[str, list[str]] = {}
    for case in resolved:
        for line in case.bank_lines:
            claimants.setdefault(line.line_id, []).append(case.case_id)
    assert claimants == {}
    assert all(case.match_tier == int(MatchTier.NO_MATCH) for case in resolved)


def test_report_renders_and_is_never_folded_into_another_metrics_report(batch, result) -> None:
    """"Reported separately", on `tests/test_adversarial.py`'s own terms: this
    batch gets its own `EvalReport` under `arm="contested"`, and this call is the
    only place in the suite that arm is built. It is never passed to
    `compare_reports` and never merged with the reference, held-out or
    adversarial numbers."""
    report = build_eval_report(result.cases, result.outcome.outcomes, batch["ground_truth"], arm="contested")
    text = render_eval_report(report)
    assert "arm: contested" in text
    assert report.metrics.total_cases == EXPECTED_CASE_COUNT


def test_the_measured_shortfall_is_a_classification_gap_not_a_matching_one(batch, result) -> None:
    """The disclosed negative, pinned so it cannot drift silently.

    The keyword arm reaches ground truth on 6 of 12 cases. The other 6 are the
    contested settlements it escalates as `BANK_CREDIT_OVERDUE` where ground
    truth says the case is either undecidable (`ABSTAINED`, 4 cases) or
    resolvable from prose the arm cannot read (`AUTO_MATCHED` on the UPI
    settlement of each decidable pair, 2 cases). All six are safe errors: no
    false match, no auto-applied entry, and every one lands in a state that
    routes the case to a human.
    """
    outcomes = {outcome.case_id: outcome for outcome in result.outcome.outcomes}
    truth = align_ground_truth(result.cases, batch["ground_truth"])
    mismatched = {
        case_id: (outcomes[case_id].state, expected.expected_outcome_state)
        for case_id, expected in truth.items()
        if outcomes[case_id].state is not expected.expected_outcome_state
    }
    assert set(mismatched) == {
        "con_setl_undec_a1",
        "con_setl_undec_a2",
        "con_setl_undec_b1",
        "con_setl_undec_b2",
        "con_setl_dec_c_upi",
        "con_setl_dec_d_upi",
    }
    assert all(
        predicted is OutcomeState.EXTERNAL_ACTION_REQUIRED for predicted, _ in mismatched.values()
    )
    report = build_eval_report(result.cases, result.outcome.outcomes, batch["ground_truth"], arm="contested")
    assert report.metrics.state_prediction_accuracy.numerator == 6
    assert report.metrics.state_prediction_accuracy.denominator == EXPECTED_CASE_COUNT


# --- The ablation this batch exists to measure. ---


def _llm_semantics():
    """The committed cache, strict mode, `client=None` — the same offline shape
    every other NFR-05 checkpoint in this codebase uses, so a miss cannot
    silently become a network call."""
    from pipeline.llm_cache import CacheMode, PromptCache
    from pipeline.semantics import LlmSemantics

    return LlmSemantics(
        PromptCache(REPO_ROOT / "data" / "semantics_cache.json"), mode=CacheMode.STRICT, client=None
    )


@pytest.fixture(scope="module")
def llm_result(batch):
    semantics = _llm_semantics()
    return run_batch(
        connect(":memory:"),
        settlements=batch["settlements"],
        recon_lines=batch["recon_lines"],
        bank_lines=batch["bank_lines"],
        ledger_entries=batch["ledger_entries"],
        snapshot_date=SNAPSHOT_DATE,
        classifier=lambda bundles: classify_batch_baseline(bundles, semantics),
        semantics=semantics,
    )


def _state_accuracy(batch, result) -> tuple[int, int]:
    report = build_eval_report(result.cases, result.outcome.outcomes, batch["ground_truth"], arm="contested")
    accuracy = report.metrics.state_prediction_accuracy
    return accuracy.numerator, accuracy.denominator


def test_the_model_arm_resolves_two_contests_the_keyword_arm_cannot(batch, result, llm_result) -> None:
    """§5.4 on the money path: 6/12 -> 8/12, and the two are the contested pair.

    The keyword arm answers `None` to every contest because §4.6 has no tier
    that can read a payment-method word out of a narration, so both UPI
    settlements sit at tier 3 and read `EXTERNAL_ACTION_REQUIRED`. The model
    reads the discriminator the bank actually wrote and the pair reaches
    `AUTO_MATCHED`, which is what ground truth says.

    This is the only place in the repository where the model changes a *money*
    outcome, so it is asserted against the batch rather than described.
    """
    assert _state_accuracy(batch, result) == (6, 12)
    assert _state_accuracy(batch, llm_result) == (8, 12)

    keyword_matched = {o.case_id for o in result.outcome.outcomes if o.state is OutcomeState.AUTO_MATCHED}
    model_matched = {o.case_id for o in llm_result.outcome.outcomes if o.state is OutcomeState.AUTO_MATCHED}
    assert len(model_matched - keyword_matched) == 2
    assert keyword_matched < model_matched  # strictly additive: nothing was lost


def test_the_model_arm_costs_no_false_match(batch, llm_result) -> None:
    """The referee. Resolving a contest wrongly books a real credit against the
    wrong settlement, which lands here and nowhere else — so an arm that gained
    two cases by guessing would show it on this line, not on state accuracy."""
    report = build_eval_report(llm_result.cases, llm_result.outcome.outcomes, batch["ground_truth"], arm="contested")
    assert report.metrics.false_match_rate.numerator == 0
    assert report.metrics.false_match_rate.denominator == 12


def test_the_model_arm_still_abstains_on_the_genuinely_undecidable_pairs(batch, llm_result) -> None:
    """The half that must NOT move.

    Four settlements contest two credits whose narrations carry no
    discriminator at all. Ground truth is `ABSTAINED` for all four, and the
    grounding gate in `LlmSemantics.resolve_contested_credit` is what keeps them
    there: measured without it, the model answered a settlement id anyway on a
    narration that named nothing. None of the four may reach `AUTO_MATCHED`.
    """
    undecidable = {
        truth.case_id
        for truth in batch["ground_truth"]
        if truth.expected_outcome_state is OutcomeState.ABSTAINED
    }
    assert len(undecidable) == 4
    for outcome in llm_result.outcome.outcomes:
        if outcome.case_id in undecidable:
            assert outcome.state is not OutcomeState.AUTO_MATCHED
