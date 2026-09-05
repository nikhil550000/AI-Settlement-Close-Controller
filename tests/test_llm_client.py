"""`pipeline/llm_client.py`: the Fireworks backend, offline-checkable parts only.

`FireworksClient.complete` makes a real network call and is exercised
manually against the real Fireworks endpoint, never in the automated
suite (offline mode's offline mode would be a lie if `pytest` needed a live key).
What *is* checkable without a network: the client's own guardrail against
missing credentials, and that constructing it never itself reaches out.
"""

from __future__ import annotations

import pytest

from pipeline.llm_client import FIREWORKS_BASE_URL, FIREWORKS_MODEL_ID, FireworksClient


def test_missing_api_key_raises_before_any_network_access(monkeypatch) -> None:
    monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        FireworksClient()


def test_an_explicit_api_key_is_accepted_without_reading_the_environment(monkeypatch) -> None:
    monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
    client = FireworksClient(api_key="fw_test_key_not_real")
    assert client._api_key == "fw_test_key_not_real"


def test_model_id_and_base_url_are_config_not_hardcoded_at_call_sites() -> None:
    """The model choice: 'the model ID is a config parameter, not a literal, so a swap is a
    one-line change.'"""
    client = FireworksClient(api_key="fw_test_key_not_real", model="accounts/fireworks/models/other-model")
    assert client._model == "accounts/fireworks/models/other-model"
    assert client._base_url == FIREWORKS_BASE_URL


def test_default_model_id_matches_the_pinned_slot_a_model() -> None:
    """Pinned to `gpt-oss-120b`, not the model choice's stated `llama-v3p3-70b-instruct` — see
    `FIREWORKS_MODEL_ID`'s docstring for why: a live-catalog check this project's own
    Fireworks account (the model choice's own "Assumption" callout requires this check) found the
    stated primary, and every Qwen3/Qwen2.5 alternative tried, returning 404 across
    the board, while `gpt-oss-120b` verified working end to end."""
    assert FIREWORKS_MODEL_ID == "accounts/fireworks/models/gpt-oss-120b"


def test_constructing_the_client_does_not_build_the_openai_client_yet(monkeypatch) -> None:
    """Lazy construction: importing/instantiating this module must never require
    network access on its own — only the first real `complete()` call does."""
    client = FireworksClient(api_key="fw_test_key_not_real")
    assert client._client is None
