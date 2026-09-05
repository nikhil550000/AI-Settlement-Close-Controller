"""`pipeline/llm_cache.py`: the SHA-256-keyed prompt/response cache.

Unit coverage of the cache primitive in isolation, separate from Slot A
itself (`tests/test_llm_slot_a.py`) — a cache bug is cheapest to diagnose
here, against a bare `dict[str, str]` contract, rather than through a full
`EvidenceBundle`/prompt/schema stack.
"""

from __future__ import annotations

import json

import pytest

from pipeline.llm_cache import CacheMissError, CacheMode, PromptCache, cache_key


def test_cache_key_is_deterministic_sha256_of_the_exact_prompt(tmp_path) -> None:
    assert cache_key("hello") == cache_key("hello")
    assert cache_key("hello") != cache_key("Hello")
    # Not asserting the literal hex digest against a hand-computed value would let
    # a silent hash-function swap (sha256 -> md5, say) pass unnoticed.
    import hashlib

    assert cache_key("hello") == hashlib.sha256(b"hello").hexdigest()


def test_a_miss_returns_none_and_counts_toward_hit_rate(tmp_path) -> None:
    cache = PromptCache(tmp_path / "cache.json")
    assert cache.get("never stored") is None
    assert cache.misses == 1
    assert cache.hits == 0
    assert cache.hit_rate == 0.0


def test_put_then_get_is_a_hit_and_persists_to_disk(tmp_path) -> None:
    path = tmp_path / "cache.json"
    cache = PromptCache(path)
    cache.put("prompt-1", "response-1")

    assert cache.get("prompt-1") == "response-1"
    assert cache.hits == 1

    # A second, independently constructed cache reading the same path sees it too —
    # this is the entire mechanism `--llm-cache=strict` depends on across processes.
    reloaded = PromptCache(path)
    assert reloaded.get("prompt-1") == "response-1"


def test_committed_file_is_sorted_json_for_clean_diffs(tmp_path) -> None:
    path = tmp_path / "cache.json"
    cache = PromptCache(path)
    cache.put("z-prompt", "z-response")
    cache.put("a-prompt", "a-response")

    raw = path.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert list(parsed.keys()) == sorted(parsed.keys())
    # sort_keys=True in the writer is what guarantees this regardless of insertion order.
    assert parsed[cache_key("a-prompt")] == "a-response"
    assert parsed[cache_key("z-prompt")] == "z-response"


def test_hit_rate_reflects_a_mix_of_hits_and_misses(tmp_path) -> None:
    cache = PromptCache(tmp_path / "cache.json")
    cache.put("known", "value")
    cache.get("known")
    cache.get("known")
    cache.get("unknown")

    assert cache.hits == 2
    assert cache.misses == 1
    assert cache.hit_rate == pytest.approx(2 / 3)


def test_cache_mode_has_exactly_two_values() -> None:
    assert {mode.value for mode in CacheMode} == {"strict", "refresh"}


def test_cache_miss_error_is_a_runtime_error() -> None:
    assert issubclass(CacheMissError, RuntimeError)
