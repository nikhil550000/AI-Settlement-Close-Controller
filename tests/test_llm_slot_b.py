"""Session 5.3's checkpoint (spec.md §6.3):

> Full run with networking disabled succeeds.

Modelled the same way session 5.2's checkpoint was (`tests/test_llm_slot_a.py`):
populate a cache once (standing in for a real `--llm-cache=refresh` pass), then run
Slot B twice in `CacheMode.STRICT` with `client=None` — so a strict pass is
structurally incapable of reaching a network path, not just conventionally avoiding
one. The checkpoint test is `test_full_run_with_networking_disabled_succeeds`.

Everything under `LLMClient` here is a stub — never `pipeline.llm_client.FireworksClient`
— for the same NFR-05 reason session 5.2's suite gives: the automated suite must stay
genuinely network-free, and the real-account verification (population against the
actual Fireworks account) lives in a scratch script, not in `pytest`.

Around the checkpoint: `build_slot_b_prompt`'s determinism and its exclusion of
`case_id`, `parse_slot_b_response`'s success and failure paths, `narrate_case_llm`'s
three paths (refresh-miss, refresh-hit, strict-miss), `build_narration_bundles`'
state filter (only `EXTERNAL_ACTION_REQUIRED` and `ABSTAINED`, never the other three
terminal states), and the FR-11 `model_generated` label riding on every `CaseNarration`
this module produces.
"""

from __future__ import annotations

import json
import random
from datetime import date
from functools import partial

import pytest

from generator.cli import generate_reference_batch
from pipeline.apply import CaseOutcome
from pipeline.case_assembly import CaseKind
from pipeline.classifier import classify_batch_baseline
from pipeline.ground_truth import ExceptionSubtype, OutcomeState
from pipeline.llm_cache import CacheMissError, CacheMode, PromptCache
from pipeline.narration import (
    NarrationBundle,
    NarrationKind,
    SlotBResponseError,
    build_narration_bundle,
    build_narration_bundles,
    build_slot_b_prompt,
    narrate_batch_llm,
    narrate_case_llm,
    parse_slot_b_response,
)
from pipeline.run import run_batch
from pipeline.storage import connect
from pipeline.subtype_label import SubtypeLabel

SNAPSHOT = date(2026, 8, 28)


class _StubClient:
    """A fake `LLMClient` that always answers the same text. Records every prompt
    it was asked to complete, so tests can assert it was (or was not) called."""

    def __init__(self, text: str = "Raise a support ticket with Razorpay.") -> None:
        self.text = text
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, response_schema: dict) -> str:
        self.prompts.append(prompt)
        assert response_schema["properties"]["text"]["type"] == "string"
        return json.dumps({"text": self.text})


def _bundle(**overrides) -> NarrationBundle:
    fields = dict(
        case_id="case_test",
        kind=NarrationKind.ABSTENTION_RATIONALE,
        case_kind=CaseKind.ORPHAN,
        classified_subtype=None,
        triggered_subtypes=(),
        narrations=("NEFT CR FROM UNKNOWN",),
        match_tier=None,
        in_settlement_window=None,
    )
    fields.update(overrides)
    return NarrationBundle(**fields)


def _run(seed: int = 0, *, classifier=classify_batch_baseline):
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


# --- `build_narration_bundle(s)`: which cases get Slot B text. ---


@pytest.mark.parametrize(
    "state,expected_kind",
    [
        (OutcomeState.EXTERNAL_ACTION_REQUIRED, NarrationKind.RECOMMENDED_ACTION),
        (OutcomeState.ABSTAINED, NarrationKind.ABSTENTION_RATIONALE),
    ],
)
def test_eligible_states_get_the_right_kind(state, expected_kind) -> None:
    _, result = _run()
    case = next(c for c in result.cases)
    outcome = CaseOutcome(case_id=case.case_id, state=state)
    bundle = build_narration_bundle(case, outcome)
    assert bundle is not None
    assert bundle.kind is expected_kind


@pytest.mark.parametrize(
    "state",
    [OutcomeState.AUTO_MATCHED, OutcomeState.AUTO_CLOSED, OutcomeState.REVIEW_REQUIRED],
)
def test_ineligible_states_get_no_bundle(state) -> None:
    _, result = _run()
    case = result.cases[0]
    outcome = CaseOutcome(case_id=case.case_id, state=state)
    assert build_narration_bundle(case, outcome) is None


def test_build_narration_bundles_over_a_real_batch_covers_exactly_the_two_eligible_states() -> None:
    _, result = _run()
    bundles = build_narration_bundles(result.cases, result.outcome.outcomes)

    distribution = result.outcome.state_distribution()
    expected = distribution.get("EXTERNAL_ACTION_REQUIRED", 0) + distribution.get("ABSTAINED", 0)
    assert len(bundles) == expected
    assert expected > 0  # sanity: the reference batch actually exercises both states

    bundle_ids = {bundle.case_id for bundle in bundles}
    outcomes_by_id = result.outcome.by_case_id()
    for case_id in bundle_ids:
        assert outcomes_by_id[case_id].state in (OutcomeState.EXTERNAL_ACTION_REQUIRED, OutcomeState.ABSTAINED)


def test_bundle_carries_the_classified_subtype_when_a_classifier_ran() -> None:
    _, result = _run(classifier=classify_batch_baseline)
    bundles = build_narration_bundles(result.cases, result.outcome.outcomes)
    outcomes_by_id = result.outcome.by_case_id()
    assert any(outcomes_by_id[b.case_id].classified_subtype is not None for b in bundles)
    for bundle in bundles:
        assert bundle.classified_subtype == outcomes_by_id[bundle.case_id].classified_subtype


# --- `build_slot_b_prompt` and `parse_slot_b_response`. ---


def test_prompt_is_deterministic_for_identical_evidence() -> None:
    assert build_slot_b_prompt(_bundle()) == build_slot_b_prompt(_bundle())


def test_prompt_varies_with_evidence() -> None:
    assert build_slot_b_prompt(_bundle()) != build_slot_b_prompt(_bundle(narrations=("SOMETHING ELSE",)))


def test_prompt_varies_with_kind() -> None:
    action = _bundle(kind=NarrationKind.RECOMMENDED_ACTION)
    rationale = _bundle(kind=NarrationKind.ABSTENTION_RATIONALE)
    assert build_slot_b_prompt(action) != build_slot_b_prompt(rationale)


def test_prompt_does_not_depend_on_case_id() -> None:
    """Two cases with identical evidence and kind but different IDs must hash to the
    same cache entry — mirrors `pipeline.classifier.build_slot_a_prompt`'s rule."""
    assert build_slot_b_prompt(_bundle(case_id="case_a")) == build_slot_b_prompt(_bundle(case_id="case_b"))


def test_prompt_includes_the_classified_subtype_definition_when_present() -> None:
    bundle = _bundle(classified_subtype=SubtypeLabel.SETTLEMENT_UTR_MISSING)
    prompt = build_slot_b_prompt(bundle)
    assert "SETTLEMENT_UTR_MISSING" in prompt
    assert "no bank-side anchor exists" in prompt


def test_prompt_never_mentions_an_account_code_or_a_posted_narration() -> None:
    """Mirrors `tests/test_llm_slot_a.py`'s equivalent check: the boundary
    `NarrationBundle` draws must survive into the actual text sent to the model."""
    from pipeline.accounts import CHART_OF_ACCOUNTS
    from pipeline.apply import APPLIED_NARRATION

    prompt = build_slot_b_prompt(_bundle())
    assert APPLIED_NARRATION not in prompt
    for account in CHART_OF_ACCOUNTS:
        assert account.code not in prompt
        assert account.name not in prompt


def test_parse_slot_b_response_reads_and_strips_the_text_field() -> None:
    assert parse_slot_b_response('{"text": "  Contact the bank.  "}') == "Contact the bank."


@pytest.mark.parametrize(
    "raw",
    ["not json", "{}", '{"text": ""}', '{"text": "   "}', '{"text": 5}', '{"other": "x"}'],
)
def test_parse_slot_b_response_rejects_malformed_or_empty_input(raw: str) -> None:
    with pytest.raises(SlotBResponseError):
        parse_slot_b_response(raw)


# --- `narrate_case_llm` / `narrate_batch_llm`: the three paths. ---


def test_refresh_on_a_miss_calls_the_client_and_populates_the_cache(tmp_path) -> None:
    cache = PromptCache(tmp_path / "cache.json")
    client = _StubClient("Contact the acquiring bank about the delayed credit.")
    bundle = _bundle()

    result = narrate_case_llm(bundle, cache, mode=CacheMode.REFRESH, client=client)

    assert result.text == "Contact the acquiring bank about the delayed credit."
    assert result.model_generated is True
    assert result.kind is bundle.kind
    assert len(client.prompts) == 1
    assert cache.get(build_slot_b_prompt(bundle)) is not None


def test_refresh_on_a_hit_does_not_call_the_client(tmp_path) -> None:
    cache = PromptCache(tmp_path / "cache.json")
    cache.put(build_slot_b_prompt(_bundle()), json.dumps({"text": "Escalate to the ops team."}))
    client = _StubClient()

    result = narrate_case_llm(_bundle(), cache, mode=CacheMode.REFRESH, client=client)

    assert result.text == "Escalate to the ops team."
    assert client.prompts == []


def test_strict_on_a_hit_never_needs_a_client(tmp_path) -> None:
    cache = PromptCache(tmp_path / "cache.json")
    cache.put(build_slot_b_prompt(_bundle()), json.dumps({"text": "Investigate further."}))

    result = narrate_case_llm(_bundle(), cache, mode=CacheMode.STRICT, client=None)

    assert result.text == "Investigate further."


def test_strict_on_a_miss_raises_cache_miss_error_not_a_network_call(tmp_path) -> None:
    cache = PromptCache(tmp_path / "cache.json")
    with pytest.raises(CacheMissError):
        narrate_case_llm(_bundle(), cache, mode=CacheMode.STRICT, client=None)


def test_refresh_on_a_miss_with_no_client_raises_value_error(tmp_path) -> None:
    cache = PromptCache(tmp_path / "cache.json")
    with pytest.raises(ValueError):
        narrate_case_llm(_bundle(), cache, mode=CacheMode.REFRESH, client=None)


def test_narrate_batch_llm_shares_one_cache_entry_across_identical_bundles(tmp_path) -> None:
    cache = PromptCache(tmp_path / "cache.json")
    client = _StubClient("Raise a support ticket.")
    bundles = [_bundle(case_id="case_a"), _bundle(case_id="case_b")]

    results = narrate_batch_llm(bundles, cache, mode=CacheMode.REFRESH, client=client)

    assert {r.case_id for r in results} == {"case_a", "case_b"}
    assert all(r.text == "Raise a support ticket." for r in results)
    assert len(client.prompts) == 1


def test_every_produced_narration_is_labelled_model_generated(tmp_path) -> None:
    """The FR-11 obligation carried as data: every string this module produces is
    `model_generated = True`, not something a future reporter has to remember."""
    cache = PromptCache(tmp_path / "cache.json")
    client = _StubClient()
    bundles = [_bundle(case_id="a"), _bundle(case_id="b", kind=NarrationKind.RECOMMENDED_ACTION)]

    results = narrate_batch_llm(bundles, cache, mode=CacheMode.REFRESH, client=client)

    assert all(r.model_generated is True for r in results)


# --- The session 5.3 checkpoint. ---


def test_full_run_with_networking_disabled_succeeds(tmp_path) -> None:
    """spec.md §6.3's session 5.3 checkpoint, verbatim: 'Full run with networking
    disabled succeeds.'

    A full run: `run_batch` with a classifier (Slot A's ablation baseline, standing in
    for either arm — Slot B's input contract is the same regardless of which one
    produced the terminal states), then Slot B narration over every
    `EXTERNAL_ACTION_REQUIRED`/`ABSTAINED` case, entirely in `CacheMode.STRICT` with
    `client=None` on the second half — so the run is structurally incapable of
    reaching a network path, not just conventionally avoiding one (NFR-05).
    """
    cache_path = tmp_path / "committed_cache.json"

    _, result = _run(classifier=classify_batch_baseline)
    bundles = build_narration_bundles(result.cases, result.outcome.outcomes)
    assert bundles  # the reference batch exercises both eligible states

    # One-time population, standing in for a real `--llm-cache=refresh` run.
    seed_cache = PromptCache(cache_path)
    stub = _StubClient()
    narrate_batch_llm(bundles, seed_cache, mode=CacheMode.REFRESH, client=stub)
    assert len(seed_cache) > 0
    calls_during_population = len(stub.prompts)

    def strict_pass():
        cache = PromptCache(cache_path)  # reloaded from disk each time, not shared in memory
        return narrate_batch_llm(bundles, cache, mode=CacheMode.STRICT, client=None)

    first = strict_pass()
    second = strict_pass()

    first_by_case = {n.case_id: n.text for n in first}
    second_by_case = {n.case_id: n.text for n in second}
    assert first_by_case == second_by_case
    assert len(first_by_case) == len(bundles)
    assert all(n.model_generated is True for n in first)

    # No strict pass touched the network-shaped path: the client was never even given.
    assert calls_during_population == len(stub.prompts)


def test_strict_run_with_an_incomplete_cache_fails_loudly(tmp_path) -> None:
    empty_cache = PromptCache(tmp_path / "empty.json")
    with pytest.raises(CacheMissError):
        narrate_case_llm(_bundle(), empty_cache, mode=CacheMode.STRICT, client=None)
