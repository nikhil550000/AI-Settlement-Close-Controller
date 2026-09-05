"""SHA-256-keyed prompt/response cache for Slot A: the second determinism layer.

> A SHA-256-keyed prompt/response cache, committed to the repository. The
> key is the hash of the exact prompt string. The eval path runs
> `--llm-cache=strict`, where a cache miss is a hard error rather than a
> fallthrough to the API. `--llm-cache=refresh` is the only mode that
> calls Fireworks.

The cache is deliberately dumb: a `dict[str, str]` from `sha256(prompt)`
to the raw model response text, persisted as one committed JSON file. It
does not know what a `SubtypeLabel` is — parsing the response into the
eight-value enum is `pipeline.classifier`'s job. Keeping the cache
prompt-shaped rather than result-shaped is what makes it reusable for any
future Slot A prompt revision without a schema migration: change the
prompt, get a new key, old entries just go unused rather than needing
translation.

**Determinism, not the API, is what `--llm-cache=strict` actually
buys.** No inference provider guarantees bitwise reproducibility (the
own framing) — the guarantee comes from never calling the provider at
all once a response is committed.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path


class CacheMode(StrEnum):
    """The two cache modes. There is no third: a miss under `STRICT` is an error,
    never a silent fallthrough to the API."""

    STRICT = "strict"
    REFRESH = "refresh"


class CacheMissError(RuntimeError):
    """Raised under `CacheMode.STRICT` when a prompt has no cached response.

    A hard error, not a fallthrough. This is the
    point of strict mode: the offline mode is only real if a miss
    cannot silently reach the network.
    """


def cache_key(prompt: str) -> str:
    """SHA-256 of the exact prompt string, hex-encoded. The cache's only key shape."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


class PromptCache:
    """A committed JSON file mapping `cache_key(prompt)` to the raw response text.

    Every `put` writes through to disk immediately rather than batching
    saves for one call at the end of a run: a refresh run that calls
    Fireworks for ~70 cases and dies partway through (network failure,
    Ctrl-C) should not lose the responses it already paid for and got —
    a retry with the same cache file resumes from the miss, not from zero.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, str] = {}
        self.hits = 0
        self.misses = 0
        if path.exists():
            self._data = json.loads(path.read_text(encoding="utf-8"))

    def get(self, prompt: str) -> str | None:
        """The cached response for this exact prompt, or `None` on a miss.

        Counts the lookup toward `hit_rate` regardless of outcome, because
        point 3 requires hit rate in the metrics JSON, and a rate is only
        honest if every lookup is counted, not just the misses that
        happened to be resolved.
        """
        value = self._data.get(cache_key(prompt))
        if value is None:
            self.misses += 1
        else:
            self.hits += 1
        return value

    def put(self, prompt: str, response: str) -> None:
        """Record a response and persist immediately. See class docstring."""
        self._data[cache_key(prompt)] = response
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # sort_keys so the committed file diffs cleanly and two refresh
        # runs that cache the same prompts produce a byte-identical file.
        self.path.write_text(
            json.dumps(self._data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def __len__(self) -> int:
        return len(self._data)

    @property
    def hit_rate(self) -> float:
        """Fraction of `get` calls that were satisfied from the committed cache.
        `0.0` (not an error) when nothing has been looked up yet."""
        total = self.hits + self.misses
        return self.hits / total if total else 0.0
