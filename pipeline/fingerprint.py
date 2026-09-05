"""The fingerprint-control statistic.

The fingerprint-control rule names the failure mode: anomalous cases
becoming identifiable by *artifact* rather than by *evidence* — sequential
IDs assigned per scenario, timestamps generated in scenario blocks,
narration strings unique to one anomaly type. The check is promoted on
that rule to a reported one: "the metrics JSON carries a pass/fail line
confirming that no ID ordering or timestamp block correlates with
scenario."

This module holds the one statistic those checks are built from. It lives
under `pipeline/` rather than `generator/` for the same reason the
canonical schemas and the ground-truth schema do (
Decided): it is written against in Phase 2 by the generator's checkpoint
but read in Phase 6 by the eval harness that emits the metrics JSON, and
`pipeline/` must never import `generator/`. It takes plain label
sequences, so it depends on nothing else in either package.

**What the statistic measures.** Order the batch's cases (or records) by
some *artifact* feature — position in the emitted file, lexicographic ID,
`created_at`, narration text — and label each one with the scenario that
produced it. If the feature carries scenario information, equal labels
land next to each other more often than a random permutation would put
them there. `same_label_adjacencies` counts those neighbours;
`scenario_block_statistic` compares the count against its exact mean and
variance under a uniformly random permutation of the same label multiset,
and reports the deviation as a z-score.

The null distribution is exact rather than simulated: for a permutation of
a fixed label multiset the moments are closed-form (see `_falling` below),
so the check is deterministic and needs no second RNG — which the
determinism rules would otherwise make awkward.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

BLOCK_Z_THRESHOLD = 4.0
"""Above this z-score, a feature is treated as carrying scenario structure.

One-sided: only an *excess* of same-label neighbours indicates blocking.
Four standard deviations is a two-in-a-hundred-thousand false-alarm rate
under the null, which keeps a passing checkpoint from being seed luck
while leaving the real failure mode — scenario-ordered emission, which
scores in the tens or hundreds of sigma — nowhere near the line.
"""


def _falling(n: int, k: int) -> int:
    """`n` falling factorial `k`: n(n-1)...(n-k+1), exact integer arithmetic."""
    result = 1
    for i in range(k):
        result *= n - i
    return result


@dataclass(frozen=True)
class BlockStatistic:
    """How strongly one artifact ordering clusters cases of the same scenario."""

    n: int
    same_label_adjacencies: int
    expected: float
    stdev: float

    @property
    def z(self) -> float:
        """Standard deviations above the random-permutation mean. 0.0 when the null has no spread."""
        if self.stdev == 0.0:
            return 0.0
        return (self.same_label_adjacencies - self.expected) / self.stdev

    @property
    def is_blocked(self) -> bool:
        return self.z > BLOCK_Z_THRESHOLD


def scenario_block_statistic(labels: Sequence[str]) -> BlockStatistic:
    """Compare same-label adjacencies in `labels` against a random permutation of the same multiset.

    `labels[i]` is the scenario that produced the i-th item under whatever
    artifact ordering the caller chose. Requires at least four items —
    below that the fourth moment is undefined and no ordering claim is
    meaningful anyway.
    """
    n = len(labels)
    if n < 4:
        raise ValueError(f"scenario_block_statistic needs at least 4 items, got {n}")

    counts = Counter(labels)
    sum_f2 = sum(_falling(c, 2) for c in counts.values())
    sum_f3 = sum(_falling(c, 3) for c in counts.values())
    sum_f4 = sum(_falling(c, 4) for c in counts.values())
    sum_f2_squared = sum(_falling(c, 2) ** 2 for c in counts.values())

    # p_k: probability that k specific distinct positions carry labels in the
    # stated equality pattern, under a uniformly random permutation.
    p2 = sum_f2 / _falling(n, 2)
    p3 = sum_f3 / _falling(n, 3)
    # Two disjoint equal pairs: same label for both pairs, or two different labels.
    p4 = (sum_f4 + (sum_f2**2 - sum_f2_squared)) / _falling(n, 4)

    n_adjacent = n - 1
    n_overlapping_pairs = n - 2  # (k, k+1) and (k+1, k+2) share a position
    n_disjoint_pairs = n_adjacent * (n_adjacent - 1) // 2 - n_overlapping_pairs

    expected = n_adjacent * p2
    second_moment = expected + 2 * (n_overlapping_pairs * p3 + n_disjoint_pairs * p4)
    variance = max(0.0, second_moment - expected**2)

    observed = sum(1 for a, b in zip(labels, labels[1:]) if a == b)
    return BlockStatistic(
        n=n,
        same_label_adjacencies=observed,
        expected=expected,
        stdev=math.sqrt(variance),
    )
