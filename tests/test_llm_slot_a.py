"""The checkpoint:

Two consecutive strict runs give identical metrics.

`--llm-cache=strict` produces identical metrics on two consecutive runs.
The cache is committed. A run with networking disabled succeeds end to
end.

The checkpoint test is `test_two_consecutive_strict_runs_give_identical_metrics`.
Everything under `LLMClient` here is a stub — never `pipeline.llm_client.FireworksClient`
— because the entire point of `CacheMode.STRICT` is that a checkpoint run must not
require network or credentials. A stub also makes "two consecutive runs"
checkable deterministically: a real endpoint gives no such guarantee by itself —
the cache is what supplies it, not the provider.

Around the checkpoint: `build_slot_a_prompt`'s determinism and its exclusion of
`case_id`, `parse_slot_a_response`'s success and failure paths, `classify_case_llm`'s
three paths (refresh-miss, refresh-hit, strict-miss), and the interface
decision itself — that threading a classifier through `run_batch` closes the
`UNMATCHED_INBOUND_CREDIT` gap the deterministic baseline left open.
"""

from __future__ import annotations

import json
import random
from datetime import date
from functools import partial
from pathlib import Path

import pytest

from generator.cli import generate_reference_batch
from pipeline.case_assembly import CaseKind
from pipeline.classifier import (
    ClassificationSource,
    EvidenceBundle,
    SlotAResponseError,
    SubtypeLabel,
    build_evidence_bundles,
    build_slot_a_prompt,
    classify_batch_baseline,
    classify_batch_llm,
    classify_case_llm,
    parse_slot_a_response,
)
from pipeline.llm_cache import CacheMissError, CacheMode, PromptCache
from pipeline.run import run_batch
from pipeline.storage import connect

SNAPSHOT = date(2026, 8, 28)


class _StubClient:
    """A fake `LLMClient` that always answers the same label. Records every prompt
    it was asked to complete, so tests can assert it was (or was not) called."""

    def __init__(self, subtype: SubtypeLabel = SubtypeLabel.UNMATCHED_INBOUND_CREDIT) -> None:
        self.subtype = subtype
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, response_schema: dict) -> str:
        self.prompts.append(prompt)
        assert response_schema["properties"]["subtype"]["enum"] == [label.value for label in SubtypeLabel]
        return json.dumps({"subtype": self.subtype.value})


def _bundle(**overrides) -> EvidenceBundle:
    fields = dict(
        case_id="case_test",
        case_kind=CaseKind.ORPHAN,
        fired_subtypes=(),
        has_template_hit=False,
        narrations=("NEFT CR FROM ACME TRADERS",),
        match_tier=None,
        in_settlement_window=None,
    )
    fields.update(overrides)
    return EvidenceBundle(**fields)


def _run(seed: int = 0, *, classifier=None):
    batch = generate_reference_batch(random.Random(seed), SNAPSHOT)
    conn = connect(":memory:")
    result = run_batch(
        conn,
        settlements=batch.settlements,
        recon_lines=batch.recon_lines,
        bank_lines=batch.bank_lines,
        ledger_entries=batch.ledger_entries,
        snapshot_date=SNAPSHOT,
        classifier=classifier,
    )
    return batch, result


# --- `build_slot_a_prompt` and `parse_slot_a_response`. ---


def test_prompt_is_deterministic_for_identical_evidence() -> None:
    assert build_slot_a_prompt(_bundle()) == build_slot_a_prompt(_bundle())


def test_prompt_varies_with_evidence() -> None:
    assert build_slot_a_prompt(_bundle()) != build_slot_a_prompt(_bundle(narrations=("SOMETHING ELSE",)))


def test_prompt_does_not_depend_on_case_id() -> None:
    """Two cases with identical evidence but different IDs must hash to the same cache
    entry — an internal bookkeeping ID is not evidence."""
    assert build_slot_a_prompt(_bundle(case_id="case_a")) == build_slot_a_prompt(_bundle(case_id="case_b"))


def test_prompt_lists_all_eight_subtype_definitions() -> None:
    prompt = build_slot_a_prompt(_bundle())
    for label in SubtypeLabel:
        assert label.value in prompt


def test_prompt_never_mentions_an_account_code_or_a_posted_narration() -> None:
    """The boundary `EvidenceBundle` enforces (never an account, an amount, or a
    postable narration) must survive into the actual text sent to the model.

    "credit"/"debit"/"paise" as English words are legitimate vocabulary here — both
    business terms (`UNMATCHED_INBOUND_CREDIT`, "bank credit") and, in the
    instructions themselves, the very sentence telling the model it will never be
    given one. What must never appear as *data* is an actual chart-of-accounts
    code/name or the constant ledger narration `apply.py` posts.
    """
    from pipeline.accounts import CHART_OF_ACCOUNTS
    from pipeline.apply import APPLIED_NARRATION

    prompt = build_slot_a_prompt(_bundle())
    assert APPLIED_NARRATION not in prompt
    for account in CHART_OF_ACCOUNTS:
        assert account.code not in prompt
        assert account.name not in prompt


def test_parse_slot_a_response_reads_the_subtype_field() -> None:
    assert parse_slot_a_response('{"subtype": "AMBIGUOUS_CASE"}') is SubtypeLabel.AMBIGUOUS_CASE


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "{}",
        '{"subtype": "NOT_A_REAL_SUBTYPE"}',
        '{"subtype": "NONE"}',  # a real ExceptionSubtype value, but not a SubtypeLabel one
        "{\"subtype\": 5}",
    ],
)
def test_parse_slot_a_response_rejects_malformed_or_out_of_vocabulary_input(raw: str) -> None:
    with pytest.raises(SlotAResponseError):
        parse_slot_a_response(raw)


# --- `classify_case_llm` / `classify_batch_llm`: the three paths. ---


def test_refresh_on_a_miss_calls_the_client_and_populates_the_cache(tmp_path) -> None:
    cache = PromptCache(tmp_path / "cache.json")
    client = _StubClient(SubtypeLabel.DISPUTE_PENDING)
    bundle = _bundle()

    result = classify_case_llm(bundle, cache, mode=CacheMode.REFRESH, client=client)

    assert result.subtype is SubtypeLabel.DISPUTE_PENDING
    assert result.source is ClassificationSource.LLM_SLOT_A
    assert len(client.prompts) == 1
    assert cache.get(build_slot_a_prompt(bundle)) is not None


def test_refresh_on_a_hit_does_not_call_the_client(tmp_path) -> None:
    cache = PromptCache(tmp_path / "cache.json")
    cache.put(build_slot_a_prompt(_bundle()), json.dumps({"subtype": "REVERSAL_UNMATCHED"}))
    client = _StubClient()

    result = classify_case_llm(_bundle(), cache, mode=CacheMode.REFRESH, client=client)

    assert result.subtype is SubtypeLabel.REVERSAL_UNMATCHED
    assert client.prompts == []


def test_strict_on_a_hit_never_needs_a_client(tmp_path) -> None:
    cache = PromptCache(tmp_path / "cache.json")
    cache.put(build_slot_a_prompt(_bundle()), json.dumps({"subtype": "DUPLICATE_CREDIT"}))

    result = classify_case_llm(_bundle(), cache, mode=CacheMode.STRICT, client=None)

    assert result.subtype is SubtypeLabel.DUPLICATE_CREDIT


def test_strict_on_a_miss_raises_cache_miss_error_not_a_network_call(tmp_path) -> None:
    cache = PromptCache(tmp_path / "cache.json")
    with pytest.raises(CacheMissError):
        classify_case_llm(_bundle(), cache, mode=CacheMode.STRICT, client=None)


def test_refresh_on_a_miss_with_no_client_raises_value_error(tmp_path) -> None:
    cache = PromptCache(tmp_path / "cache.json")
    with pytest.raises(ValueError):
        classify_case_llm(_bundle(), cache, mode=CacheMode.REFRESH, client=None)


def test_classify_batch_llm_shares_one_cache_entry_across_identical_bundles(tmp_path) -> None:
    """Two cases with the same evidence (a shared opaque narration from the generator's
    pool, e.g.) must cost one API call, not two."""
    cache = PromptCache(tmp_path / "cache.json")
    client = _StubClient(SubtypeLabel.AMBIGUOUS_CASE)
    bundles = [_bundle(case_id="case_a"), _bundle(case_id="case_b")]

    results = classify_batch_llm(bundles, cache, mode=CacheMode.REFRESH, client=client)

    assert {r.case_id for r in results} == {"case_a", "case_b"}
    assert all(r.subtype is SubtypeLabel.AMBIGUOUS_CASE for r in results)
    assert len(client.prompts) == 1


# --- The interface decision: threading a classifier through `run_batch`. ---


def test_no_classifier_reproduces_the_session_5_1_gap_exactly() -> None:
    """`KNOWN_GAPS`' documented shortfall, unchanged when `run_batch` is called the
    old way (the deterministic baseline's own checkpoint test, re-asserted here as
    the baseline this module's other tests are a delta against)."""
    _, result = _run()
    assert result.classifications == ()
    # No case was classified at all — not just the 8 that would flip state.
    assert all(outcome.classified_subtype is None for outcome in result.outcome.outcomes)


def test_baseline_classifier_closes_the_eight_case_gap() -> None:
    """Threading the deterministic keyword baseline through `run_batch` (the
    interface decision) makes every case land in its expected ground-truth state —
    the same 8 cases `tests/test_apply.py`'s checkpoint documents as a gap, closed."""
    batch, result = _run(classifier=classify_batch_baseline)

    ground_truth_by_line = {}
    ground_truth_by_case = {}
    for row in batch.ground_truth:
        ground_truth_by_case[row.case_id] = row
        for record_id in row.expected_linked_source_records:
            ground_truth_by_line[record_id] = row

    outcomes = result.outcome.by_case_id()
    mismatches = []
    for case in result.cases:
        if case.kind is CaseKind.SETTLEMENT_ANCHORED:
            expected = ground_truth_by_case[case.case_id].expected_outcome_state
        else:
            expected = ground_truth_by_line[case.bank_lines[0].line_id].expected_outcome_state
        if outcomes[case.case_id].state is not expected:
            mismatches.append(case.case_id)

    assert mismatches == []
    assert len(result.classifications) == 70


def test_classification_does_not_disturb_auto_matched_or_auto_closed_cases() -> None:
    """The classifier only ever sees non-auto-close cases, and wiring it in must
    not change the 80 cases outside that population."""
    _, without = _run()
    _, with_classifier = _run(classifier=classify_batch_baseline)

    without_states = without.outcome.state_distribution()
    with_states = with_classifier.outcome.state_distribution()

    assert without_states["AUTO_MATCHED"] == with_states["AUTO_MATCHED"] == 30
    assert without_states["AUTO_CLOSED"] == with_states["AUTO_CLOSED"] == 50


def test_classified_subtype_is_recorded_even_when_it_does_not_change_state() -> None:
    """An `AMBIGUOUS_CASE` classification never *changes* a state (only
    `UNMATCHED_INBOUND_CREDIT` does, via `assign_state`) but must still be recorded —
    the audit trail for what component 5 said, not just for the one label that acts
    on it.

    It lands on two different states, both correctly: `ABSTAINED` for the 17 orphan/
    settlement-anchored cases with no other evidence at all, and `REVIEW_REQUIRED` for
    the 17 policy-excluded/date-error cases the deterministic baseline names
    explicitly — a candidate existed and was declined by policy or confidence *before* classification
    is ever consulted, so `AMBIGUOUS_CASE` here is the baseline's documented "least-
    wrong answer in a vocabulary with no correct one," not evidence of a bug.
    """
    _, result = _run(classifier=classify_batch_baseline)
    ambiguous_outcomes = [
        outcome for outcome in result.outcome.outcomes if outcome.classified_subtype is SubtypeLabel.AMBIGUOUS_CASE
    ]
    assert ambiguous_outcomes
    observed_states = {outcome.state.value for outcome in ambiguous_outcomes}
    assert observed_states <= {"ABSTAINED", "REVIEW_REQUIRED"}
    assert "ABSTAINED" in observed_states


# --- The checkpoint. ---


def test_two_consecutive_strict_runs_give_identical_metrics(tmp_path) -> None:
    """The checkpoint, verbatim: 'Two consecutive strict runs
    give identical metrics.' And: 'The cache is committed. A run
    with networking disabled succeeds end to end.'

    Modelled here as: populate a committed cache file once (the one-time step a real
    `--llm-cache=refresh` run performs), then run the batch **twice** in
    `CacheMode.STRICT` against that same file, each time with `client=None` — so a
    strict run is structurally incapable of reaching a network path, not just
    conventionally avoiding one.
    """
    cache_path = tmp_path / "committed_cache.json"

    # One-time population, standing in for a real `--llm-cache=refresh` run.
    seed_cache = PromptCache(cache_path)
    stub = _StubClient()
    _run(classifier=partial(classify_batch_llm, cache=seed_cache, mode=CacheMode.REFRESH, client=stub))
    assert len(seed_cache) > 0
    calls_during_population = len(stub.prompts)

    def strict_pass():
        cache = PromptCache(cache_path)  # reloaded from disk each time, not shared in memory
        classifier = partial(classify_batch_llm, cache=cache, mode=CacheMode.STRICT, client=None)
        return _run(classifier=classifier)

    _, first = strict_pass()
    _, second = strict_pass()

    assert first.state_distribution() == second.state_distribution()
    assert first.match_tier_distribution() == second.match_tier_distribution()

    first_by_case = {r.case_id: r.subtype for r in first.classifications}
    second_by_case = {r.case_id: r.subtype for r in second.classifications}
    assert first_by_case == second_by_case
    assert len(first_by_case) == 70

    # No strict pass touched the network-shaped path: the client was never even given.
    assert calls_during_population == len(stub.prompts)


def test_strict_run_with_an_incomplete_cache_fails_loudly(tmp_path: Path) -> None:
    """A committed cache missing even one of the ~70 prompts must not silently
    degrade: a cache miss is a hard error rather than a fallthrough to the API."""
    empty_cache = PromptCache(tmp_path / "empty.json")
    classifier = partial(classify_batch_llm, cache=empty_cache, mode=CacheMode.STRICT, client=None)
    with pytest.raises(CacheMissError):
        _run(classifier=classifier)
