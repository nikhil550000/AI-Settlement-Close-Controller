"""The Fireworks backend for Slot A.

> `llama-v3p3-70b-instruct` on Fireworks, primary. The model ID is a
> config parameter, not a literal, so a swap is a one-line change.

> **LLM:** the `openai` SDK against the Fireworks OpenAI-compatible
> endpoint.

This module is the only place that constructs an OpenAI SDK client or
knows Fireworks' base URL. Everything downstream (`pipeline/classifier.py`)
depends on `LLMClient`, a one-method `Protocol`, so a test can inject a
stub that never touches a socket — `pipeline/llm_cache.py`'s
`CacheMode.STRICT` is what makes that the *normal* path for a checkpoint
run, not just a testing convenience.
"""

from __future__ import annotations

import os
from typing import Protocol

FIREWORKS_BASE_URL = "https://api.fireworks.ai/inference/v1"

FIREWORKS_MODEL_ID = "accounts/fireworks/models/gpt-oss-120b"
"""The design named `llama-v3p3-70b-instruct` as primary, and its own "Assumption" callout
anticipates exactly this: "Fireworks deprecates model versions on its own schedule.
The exact model ID MUST be re-checked against the live catalog before it is pinned." did that check against this project's actual Fireworks account and found
`llama-v3p3-70b-instruct` (and every Qwen3/Qwen2.5 variant tried) returning
`404 Model not found, inaccessible, and/or not deployed` — not a per-model gap but an
account-wide one, since it was identical across three unrelated model families.
`gpt-oss-120b` is the model verified live (smoke-tested through this exact client,
constrained decoding included) to actually be servable on this account. A config
parameter, not a literal, so swapping it back once Llama 3.3 becomes available on
this account is a one-line change."""

SLOT_A_RESPONSE_SCHEMA_NAME = "exception_subtype_classification"


class LLMClient(Protocol):
    """What Slot A needs from a model backend: one prompt in, one raw response string out.

    Deliberately this narrow — no chat history, no streaming, no tool
    calls — because Slot A is one constrained-decoding call per case with
    no multi-turn state: it receives a structured evidence bundle and
    returns one value from an eight-value enum").
    """

    def complete(self, prompt: str, *, response_schema: dict) -> str: ...


class FireworksClient:
    """The real backend. Constructing this never requires network access;
    only `complete` does, and only when the cache misses under `CacheMode.REFRESH`.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = FIREWORKS_MODEL_ID,
        base_url: str = FIREWORKS_BASE_URL,
    ) -> None:
        self._api_key = api_key or os.environ.get("FIREWORKS_API_KEY")
        if not self._api_key:
            raise RuntimeError(
                "FIREWORKS_API_KEY is not set. Required only in --llm-cache=refresh mode; "
                "--llm-cache=strict never constructs this client's network path."
            )
        self._model = model
        self._base_url = base_url
        self._client = None

    def _openai_client(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(api_key=self._api_key, base_url=self._base_url)
        return self._client

    def complete(self, prompt: str, *, response_schema: dict) -> str:
        """One constrained-decoding call. Temperature 0 and a fixed seed are
        The first, necessary-but-insufficient determinism layer. The cache
        (`pipeline/llm_cache.py`) is what makes reproducibility actually hold."""
        response = self._openai_client().chat.completions.create(
            model=self._model,
            temperature=0,
            seed=0,
            messages=[{"role": "user", "content": prompt}],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": SLOT_A_RESPONSE_SCHEMA_NAME,
                    "schema": response_schema,
                },
            },
        )
        content = response.choices[0].message.content
        if content is None:
            raise RuntimeError("Fireworks returned no message content for a Slot A call")
        return content
