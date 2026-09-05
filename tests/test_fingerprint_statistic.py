"""`pipeline.fingerprint`'s null distribution, checked against simulation.

The statistic's mean and variance are closed-form (a permutation of a
fixed label multiset), and the whole fingerprint checkpoint rests on them:
a wrong variance makes every generator plan fingerprint assertion either vacuous or
flaky, and neither failure is visible from the assertions themselves. So
the formulas are checked against a Monte-Carlo of the same null — a
seeded, self-contained simulation, not a second source of truth about the
batch.
"""

from __future__ import annotations

import random
import statistics

import pytest

from pipeline.fingerprint import BLOCK_Z_THRESHOLD, scenario_block_statistic

_LABEL_MULTISETS = {
    "batch_shaped": ["clean"] * 18 + [f"family_{i}" for i in range(1, 6) for _ in range(10)] + ["orphan"] * 25,
    "two_labels": ["a"] * 40 + ["b"] * 60,
    "one_rare_label": ["a"] * 96 + ["b"] * 4,
    "many_singletons": [f"pop_{i}" for i in range(30)] + ["bulk"] * 70,
}


@pytest.mark.parametrize("name", _LABEL_MULTISETS)
def test_closed_form_moments_match_a_simulated_random_permutation(name):
    labels = list(_LABEL_MULTISETS[name])
    predicted = scenario_block_statistic(labels)

    rng = random.Random(20260827)
    observed = []
    for _ in range(4000):
        rng.shuffle(labels)
        observed.append(scenario_block_statistic(labels).same_label_adjacencies)

    simulated_mean = statistics.fmean(observed)
    simulated_stdev = statistics.stdev(observed)
    # 4000 draws pins the mean to well under 0.1 of a standard deviation.
    assert abs(simulated_mean - predicted.expected) < 0.1 * predicted.stdev
    assert abs(simulated_stdev - predicted.stdev) < 0.05 * predicted.stdev


@pytest.mark.parametrize("name", _LABEL_MULTISETS)
def test_a_random_permutation_is_not_flagged_as_blocked(name):
    labels = list(_LABEL_MULTISETS[name])
    rng = random.Random(11)
    rng.shuffle(labels)
    assert not scenario_block_statistic(labels).is_blocked


@pytest.mark.parametrize("name", _LABEL_MULTISETS)
def test_a_scenario_ordered_sequence_is_flagged_past_the_threshold(name):
    """The teeth of every fingerprint assertion: perfect blocking must be flagged, not sit near the line."""
    labels = sorted(_LABEL_MULTISETS[name])
    statistic = scenario_block_statistic(labels)
    assert statistic.is_blocked
    assert statistic.z > 2 * BLOCK_Z_THRESHOLD


def test_detection_power_grows_with_the_number_of_scenarios():
    """Two labels already sit near-saturated under the null; the real batch's ~18 populations do not.

    Worth pinning because it is the statistic's one soft spot: with a
    handful of labels a random permutation *also* produces long same-label
    runs, so blocking is only a few sigma. The the generator plan populations are many
    and small, which is the regime where the check bites hardest.
    """
    two = scenario_block_statistic(sorted(_LABEL_MULTISETS["two_labels"]))
    batch = scenario_block_statistic(sorted(_LABEL_MULTISETS["batch_shaped"]))
    assert batch.z > 2 * two.z


def test_a_single_label_has_no_spread_and_is_never_blocked():
    """Every item the same scenario: adjacencies are maximal but carry no information, so z is 0, not infinite."""
    statistic = scenario_block_statistic(["only"] * 50)
    assert statistic.same_label_adjacencies == 49
    assert statistic.stdev == 0.0
    assert statistic.z == 0.0
    assert not statistic.is_blocked


def test_too_few_items_is_an_error_rather_than_a_silent_pass():
    with pytest.raises(ValueError):
        scenario_block_statistic(["a", "b", "a"])
