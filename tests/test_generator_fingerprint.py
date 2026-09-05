"""The checkpoint: fingerprint checks pass; no ID or timestamp block
correlates with scenario.

Run against the full 150-case reference batch as `uv run generate` builds
it — through `generate_reference_batch`, the same function the command
calls — because a fingerprint assertion against a differently-assembled
batch asserts nothing about the batch that ships.

Two kinds of check here, and they answer different questions.

**Ordering checks** ask whether any *artifact* of the batch carries
scenario information. Order the records by the artifact — position in the
emitted file, lexicographic identifier, `created_at`, narration text — and
count how often neighbours share a population;
`pipeline.fingerprint.scenario_block_statistic` compares that against a
random permutation of the same label multiset. These are statistical, so
`test_the_unfinalized_batch_is_caught_by_the_same_checks` runs them
against the pre-pass batch to prove they have teeth: a check that passes
on scenario-ordered input is measuring nothing.

**Exact checks** pin the constructions the ordering checks cannot see —
that no narration escapes the shared pool, that the UTR-narration split lands
where it should, that the global ID pass left every cross-reference
resolvable.

Populations are labelled from the injection plan
(`FinalBatch.population_of`), never re-derived from the records — the
label-emission rule applies to the checkpoint as much as to the ground
truth.
"""

from __future__ import annotations

import random
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timezone

import pytest

from generator.cli import generate_reference_batch
from generator.clean import SETTLEMENT_MAX_DAYS_BACK
from generator.exceptions import N_AMBIGUOUS
from generator.finalize import NOISE_POPULATION, FinalBatch, finalize_batch
from generator.narration import (
    TAX_SIGNATURES,
    TRUNCATED_MIN_LENGTH,
    UTR_LENGTH,
    UtrShape,
    narration_template,
)
from pipeline.accounts import ACCOUNT_RAZORPAY_CLEARING, ACCOUNT_SALES_RETURNS_AND_ALLOWANCES
from pipeline.fingerprint import scenario_block_statistic
from pipeline.schemas import RazorpayEntityType
from tests.test_generator_batch import _EXPECTED_STATE_TOTALS

SNAPSHOT = date(2026, 8, 28)
SEED = 1

_ID_LIKE = re.compile(r"[a-z]+_[0-9a-f]{8}")
"""The shape every generator identifier takes, for finding them inside ground-truth prose."""


@pytest.fixture(scope="module")
def batch() -> FinalBatch:
    return generate_reference_batch(random.Random(SEED), SNAPSHOT)


def _utc_date(unix_ts: int) -> date:
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).date()


def _settlement_credits(batch: FinalBatch) -> list:
    return [line for line in batch.bank_lines if line.line_id in batch.settlement_credit_of]


DATE_ERROR_POPULATION = "family_4_date_error"
"""The one population whose posting *date* is its anomaly (`MISPOSTING` widened to cover period).

Excluded from the posting-date ordering check, and only from that one:
its ledger legs sit a calendar month before the rest of the batch by
construction, so an ordering by posting date is supposed to separate it.
`test_the_date_error_variant_is_separable_by_posting_date` asserts that
separation exists rather than leaving the exclusion unaccounted for.
"""


def _earliest_posting_date(batch: FinalBatch) -> dict[str, date]:
    """`settlement.id` -> the earliest date anything was posted against it.

    The ordering check works per case rather than per ledger entry
    because a settlement's forty-odd legs share two or three dates by
    construction — a payment's legs carry its capture date. Ordering the
    raw entries therefore measures how tightly a *case* clusters, which is
    a true fact about settlement bookkeeping and says nothing about
    whether the population leaked; the question worth asking is whether a
    case's posting dates reveal which population it came from.
    """
    settlement_of = {line.entity_id: line.settlement_id for line in batch.recon_lines}
    earliest: dict[str, date] = {}
    for entry in batch.ledger_entries:
        settlement_id = settlement_of.get(entry.reference)
        if settlement_id is None:
            continue  # a phantom reference: the subtype definitions' `AMBIGUOUS_CASE` construction
        if settlement_id not in earliest or entry.date < earliest[settlement_id]:
            earliest[settlement_id] = entry.date
    return earliest


# --- Ordering checks: does any artifact carry the scenario? ---


def _artifact_orderings(batch: FinalBatch) -> dict[str, list[str]]:
    """Every artifact ordering the checkpoint tests, as label sequences.

    An *artifact* is anything about a record that is not evidence about
    the case: where it sits in a file, what its identifier sorts next to,
    which sentence shape it was written from. Timestamps appear here as
    artifacts even though their window placement is evidence — the check
    is whether the *ordering* clusters populations, and the subtype definitions' window
    leaves the two window-anchored populations overlapping every other
    population's date range rather than occupying one of their own.
    """
    population = batch.population_of
    credits = _settlement_credits(batch)

    def by(records, key):
        """Stable sort: records tied on the artifact keep the batch's shuffled order, which is the null."""
        return sorted(records, key=key)

    return {
        "emitted order: settlements": [population[s.id] for s in batch.settlements],
        "emitted order: recon lines": [population[r.entity_id] for r in batch.recon_lines],
        "emitted order: ledger entries": [population[e.journal_entry_id] for e in batch.ledger_entries],
        "emitted order: bank lines": [population[b.line_id] for b in batch.bank_lines],
        "emitted order: ground truth": [population[g.case_id] for g in batch.ground_truth],
        # Split by namespace: an orphan case has no settlement, so its id
        # cannot be a `setl_` one, and every `orphan_` id sorts below every
        # `setl_` id. That block is the orphan populations case type, which the pipeline is
        # never given and could not infer from an id it does not receive —
        # ordering the two namespaces together would measure the naming
        # convention instead of asking whether an id reveals its population.
        "case_id, lexicographic (settlement-anchored)": [
            population[g.case_id]
            for g in by([g for g in batch.ground_truth if g.case_id.startswith("setl_")], lambda g: g.case_id)
        ],
        "case_id, lexicographic (orphan)": [
            population[g.case_id]
            for g in by([g for g in batch.ground_truth if g.case_id.startswith("orphan_")], lambda g: g.case_id)
        ],
        "journal_entry_id, lexicographic": [
            population[e.journal_entry_id] for e in by(batch.ledger_entries, lambda e: e.journal_entry_id)
        ],
        "bank line_id, lexicographic": [
            population[b.line_id] for b in by(batch.bank_lines, lambda b: b.line_id)
        ],
        "settlement created_at": [population[s.id] for s in by(batch.settlements, lambda s: s.created_at)],
        "earliest ledger posting date per case": [
            population[settlement_id]
            for settlement_id, _posting_date in by(
                [
                    (settlement_id, posting_date)
                    for settlement_id, posting_date in _earliest_posting_date(batch).items()
                    if population[settlement_id] != DATE_ERROR_POPULATION
                ],
                lambda pair: pair[1],
            )
        ],
        "bank line value_date": [population[b.line_id] for b in by(batch.bank_lines, lambda b: b.value_date)],
        "settlement-credit narration": [population[b.line_id] for b in by(credits, lambda b: b.narration)],
        "ledger narration template": [
            population[e.journal_entry_id]
            for e in by(batch.ledger_entries, lambda e: narration_template(e.narration))
        ],
        "bank profile": [population[b.line_id] for b in by(batch.bank_lines, lambda b: b.bank_profile)],
        "closing balance": [
            population[b.line_id] for b in by(batch.bank_lines, lambda b: b.closing_balance_paise)
        ],
    }


ARTIFACT_NAMES = (
    "emitted order: settlements",
    "emitted order: recon lines",
    "emitted order: ledger entries",
    "emitted order: bank lines",
    "emitted order: ground truth",
    "case_id, lexicographic (settlement-anchored)",
    "case_id, lexicographic (orphan)",
    "journal_entry_id, lexicographic",
    "bank line_id, lexicographic",
    "settlement created_at",
    "earliest ledger posting date per case",
    "bank line value_date",
    "settlement-credit narration",
    "ledger narration template",
    "bank profile",
    "closing balance",
)


@pytest.fixture(scope="module")
def orderings(batch) -> dict[str, list[str]]:
    computed = _artifact_orderings(batch)
    assert set(computed) == set(ARTIFACT_NAMES), "the parametrised names have drifted from the orderings"
    return computed


@pytest.mark.parametrize("artifact", ARTIFACT_NAMES)
def test_no_artifact_ordering_correlates_with_scenario(orderings, artifact):
    statistic = scenario_block_statistic(orderings[artifact])
    assert not statistic.is_blocked, f"{artifact} clusters populations: {statistic}"


@pytest.mark.parametrize("seed", [7, 42, 2026])
def test_the_checks_hold_on_seeds_other_than_the_pinned_one(seed):
    """A fingerprint pass that only holds for one seed is seed luck, not a control."""
    other = generate_reference_batch(random.Random(seed), SNAPSHOT)
    for artifact, labels in _artifact_orderings(other).items():
        statistic = scenario_block_statistic(labels)
        assert not statistic.is_blocked, f"seed {seed}, {artifact}: {statistic}"


def test_the_unfinalized_batch_is_caught_by_the_same_checks():
    """The teeth: without the global pass, emission order is scenario order and every check must fire.

    Built by handing `finalize_batch` the same populations with its
    shuffling disabled — an RNG whose `shuffle` does nothing — so the only
    difference from the real batch is the pass itself.
    """
    rng = _NonShufflingRandom(SEED)
    unfinalized = generate_reference_batch(rng, SNAPSHOT)

    blocked = {
        artifact
        for artifact, labels in _artifact_orderings(unfinalized).items()
        if scenario_block_statistic(labels).is_blocked
    }
    assert {
        "emitted order: settlements",
        "emitted order: recon lines",
        "emitted order: ledger entries",
        "emitted order: bank lines",
        "emitted order: ground truth",
    } <= blocked


class _NonShufflingRandom(random.Random):
    """A seeded RNG whose `shuffle` is a no-op, to produce the batch the global pass would have prevented."""

    def shuffle(self, x, *args, **kwargs) -> None:  # noqa: D102 - overrides random.Random
        return None


# --- Timestamps. ---


def test_no_settlement_is_pinned_to_midnight_utc(batch):
    """two window-anchored populations sat at exactly midnight; nothing may again."""
    assert not [s for s in batch.settlements if s.created_at % 86_400 == 0]


def test_settlements_dated_on_the_snapshot_day_are_not_one_populations_alone(batch):
    """The other half of the same artifact: day 0 belonged only to the family-4 no-op population."""
    same_day = {batch.population_of[s.id] for s in batch.settlements if _utc_date(s.created_at) == SNAPSHOT}
    assert len(same_day) > 1, f"only {same_day} can produce a settlement dated on the snapshot"


def test_every_settlement_falls_in_the_declared_window(batch):
    earliest = SNAPSHOT.toordinal() - SETTLEMENT_MAX_DAYS_BACK
    for settlement in batch.settlements:
        assert earliest <= _utc_date(settlement.created_at).toordinal() <= SNAPSHOT.toordinal()


def test_the_date_error_variant_is_separable_by_posting_date(batch):
    """The other side of that exclusion: a revision's anomaly is a wrong *period*, so the date must give it away.

    Not a fingerprint — it is the evidence the pipeline is required to
    find, and if it were absent the population would be unlabellable.
    """
    earliest = _earliest_posting_date(batch)
    date_error = [d for s, d in earliest.items() if batch.population_of[s] == DATE_ERROR_POPULATION]
    everything_else = [d for s, d in earliest.items() if batch.population_of[s] != DATE_ERROR_POPULATION]
    assert date_error
    assert max(date_error) < min(everything_else)


def test_no_bank_line_is_dated_after_the_snapshot(batch):
    """A statement drawn as of the snapshot cannot carry a line dated after it."""
    assert not [line for line in batch.bank_lines if line.value_date > SNAPSHOT]


# --- Narration: one shared pool. ---


def test_every_narration_comes_from_the_shared_pool(batch):
    """`narration_template` raises on a string the pool could not have written."""
    for entry in batch.ledger_entries:
        narration_template(entry.narration)
    for line in batch.bank_lines:
        narration_template(line.narration)


def test_no_ledger_narration_template_belongs_to_a_single_population(batch):
    populations_by_template = defaultdict(set)
    for entry in batch.ledger_entries:
        populations_by_template[narration_template(entry.narration)].add(batch.population_of[entry.journal_entry_id])
    assert populations_by_template
    for template, populations in populations_by_template.items():
        assert len(populations) > 1, f"{template!r} is written by {populations} alone"


def test_no_ledger_narration_carries_an_amount_or_names_an_anomaly(batch):
    """narrations restated the posted amounts and named the family in words.

    A digit in a ledger narration means an amount or an identifier has
    been copied into free text, where it becomes a second, unvalidated
    channel for the evidence; the vocabulary list is the wording that
    named the anomaly outright.
    """
    banned = ("fee", "gst", "gross", "net ", "premature", "corroborat", "unposted", "not recognized")
    for entry in batch.ledger_entries:
        assert not any(character.isdigit() for character in entry.narration), entry.narration
        lowered = entry.narration.lower()
        assert not [word for word in banned if word in lowered], entry.narration


def test_every_adjustment_carries_a_description_and_only_fr06_names_a_tax_position(batch):
    """The model-slot boundary makes the tax signature's *content* the policy exclusions detection surface; its presence must not be."""
    adjustments = [line for line in batch.recon_lines if line.type is RazorpayEntityType.ADJUSTMENT]
    assert adjustments
    for line in adjustments:
        assert line.description is not None

    signature_populations = {
        batch.population_of[line.entity_id] for line in adjustments if line.description in TAX_SIGNATURES
    }
    assert signature_populations == {"fr06_tax"}
    assert {line.description for line in adjustments if line.description in TAX_SIGNATURES} == set(TAX_SIGNATURES)


# --- the match cascade UTR narration variety. ---


def test_utr_variety_matches_the_section_4_6_split(batch):
    """"roughly 50% clean UTR, 25% embedded, 15% truncated, 10% absent" over the 98 settlement credits."""
    counts = Counter(batch.utr_shapes.values())
    assert sum(counts.values()) == 98
    assert counts[UtrShape.CLEAN] == 49
    assert counts[UtrShape.EMBEDDED] == 24
    assert counts[UtrShape.TRUNCATED] == 15
    assert counts[UtrShape.ABSENT] == 10


def test_no_utr_shape_belongs_to_a_single_population(batch):
    """Above all the absent shape: if only `SETTLEMENT_UTR_MISSING` lacked a narration UTR, absence would be the label."""
    populations_by_shape = defaultdict(set)
    for line_id, shape in batch.utr_shapes.items():
        populations_by_shape[shape].add(batch.population_of[line_id])
    for shape, populations in populations_by_shape.items():
        assert len(populations) > 1, f"the {shape} shape is written for {populations} alone"
    assert "settlement_utr_missing" in populations_by_shape[UtrShape.ABSENT]


def test_clean_narrations_carry_the_full_utr_as_a_whitespace_token(batch):
    """Tier 0 reads "`settlement.utr` appears as a token", which this shape satisfies on any tokenizer."""
    settlements = {s.id: s for s in batch.settlements}
    for line in _settlement_credits(batch):
        if batch.utr_shapes[line.line_id] is not UtrShape.CLEAN:
            continue
        utr = settlements[batch.settlement_credit_of[line.line_id]].utr
        assert utr in line.narration.split()


def test_embedded_narrations_carry_the_full_utr_but_never_as_a_whitespace_token(batch):
    """The shape's whole point: a clean-join tokenizer misses it, tier 1's alphanumeric split finds it."""
    settlements = {s.id: s for s in batch.settlements}
    for line in _settlement_credits(batch):
        if batch.utr_shapes[line.line_id] is not UtrShape.EMBEDDED:
            continue
        utr = settlements[batch.settlement_credit_of[line.line_id]].utr
        assert utr in line.narration
        assert utr not in line.narration.split()


def test_truncated_narrations_carry_a_prefix_that_still_identifies_one_settlement(batch):
    """Tier 1 accepts a prefix of length >= 8; a prefix matching two settlements would corrupt the match."""
    settlements = {s.id: s for s in batch.settlements}
    all_utrs = [s.utr for s in batch.settlements if s.utr]
    for line in _settlement_credits(batch):
        if batch.utr_shapes[line.line_id] is not UtrShape.TRUNCATED:
            continue
        utr = settlements[batch.settlement_credit_of[line.line_id]].utr
        written = next(
            token for token in _alphanumeric_runs(line.narration) if utr.startswith(token) and token != utr
        )
        assert TRUNCATED_MIN_LENGTH <= len(written) < UTR_LENGTH
        assert len([candidate for candidate in all_utrs if candidate.startswith(written)]) == 1


def test_absent_narrations_leave_the_credit_recoverable_at_tier_2(batch):
    """Tier 2 matches on amount inside the window, "accepted only if exactly one candidate exists"."""
    settlements = {s.id: s for s in batch.settlements}
    deposits = Counter(line.deposit_paise for line in batch.bank_lines)
    for line in _settlement_credits(batch):
        if batch.utr_shapes[line.line_id] is not UtrShape.ABSENT:
            continue
        settlement = settlements[batch.settlement_credit_of[line.line_id]]
        if not settlement.utr:
            continue  # `SETTLEMENT_UTR_MISSING`: unmatchable by construction, and labelled as such
        assert line.deposit_paise == settlement.amount
        assert deposits[line.deposit_paise] == 1
        assert not [token for token in _alphanumeric_runs(line.narration) if settlement.utr.startswith(token)]


def test_bank_ref_no_carries_a_utr_only_where_the_narration_already_does(batch):
    """Tier 0's `bank_ref_no` branch is exercised, but never as a back door around the narration split."""
    utrs = {s.utr for s in batch.settlements if s.utr}
    carrying = [line for line in batch.bank_lines if line.bank_ref_no in utrs]
    assert carrying, "tier 0's `or equals bank_ref_no` branch is unexercised"
    for line in carrying:
        assert batch.utr_shapes.get(line.line_id) is UtrShape.CLEAN


def _alphanumeric_runs(text: str) -> list[str]:
    run, runs = "", []
    for character in text + " ":
        if character.isalnum():
            run += character
        elif run:
            runs.append(run)
            run = ""
    return runs


# --- The global ID pass. ---


def test_every_identifier_in_the_batch_is_unique(batch):
    identifiers = (
        [s.id for s in batch.settlements]
        + [r.entity_id for r in batch.recon_lines]
        + [e.journal_entry_id for e in batch.ledger_entries]
        + [b.line_id for b in batch.bank_lines]
        + [g.case_id for g in batch.ground_truth if not g.case_id.startswith("setl_")]
    )
    assert len(identifiers) == len(set(identifiers))


def test_the_global_id_pass_keeps_every_cross_reference_resolvable(batch):
    """The pass rewrites every identifier in the batch; nothing may be left pointing at a pre-pass one."""
    settlement_ids = {s.id for s in batch.settlements}
    entity_ids = {r.entity_id for r in batch.recon_lines}
    journal_ids = {e.journal_entry_id for e in batch.ledger_entries}
    line_ids = {b.line_id for b in batch.bank_lines}
    # `order_id` and `dispute_id` are identifiers a recon line owns rather than
    # records of their own, and ground-truth prose cites the dispute id.
    owned_ids = {r.order_id for r in batch.recon_lines if r.order_id} | {
        r.dispute_id for r in batch.recon_lines if r.dispute_id
    }
    known = settlement_ids | entity_ids | journal_ids | line_ids | owned_ids
    # Every `ledger_entry.reference` in the batch resolves to a recon line as of
    # — the subtype definitions' `AMBIGUOUS_CASE` pair included, which is what makes it
    # attributable to its case. Kept as a named (now empty) set so that a
    # regression reintroducing a dangling reference fails the loop below rather
    # than being silently tolerated.
    dangling = {e.reference for e in batch.ledger_entries if e.reference not in entity_ids}
    assert dangling == set(), f"ledger references resolving to no recon line: {sorted(dangling)}"

    for line in batch.recon_lines:
        assert line.settlement_id in settlement_ids
        assert line.payment_id is None or line.payment_id in entity_ids
    for line_id, settlement_id in batch.settlement_credit_of.items():
        assert line_id in line_ids
        assert settlement_id in settlement_ids
    for case in batch.ground_truth:
        assert case.case_id in settlement_ids or case.case_id.startswith("orphan_")
        for record_id in case.expected_linked_source_records:
            assert record_id in known
        if case.expected_resolution is not None:
            for identifier in _ID_LIKE.findall(case.expected_resolution):
                assert identifier in known | dangling, (
                    f"{identifier} in {case.expected_resolution!r} survives from before the id pass"
                )


def test_the_ambiguous_cases_phantom_pairs_survive_the_id_pass(batch):
    """The subtype definitions' `AMBIGUOUS_CASE`: the contra-revenue pair stays uncorroborated *and* attributable. achieved the first half by pointing the pair's `reference`
    at an id that existed nowhere, which also detached it from its own
    case; repointed it at a real payment in the same
    settlement. Both properties are pinned here, after the global ID pass,
    because the pass rewrites every identifier in the batch and either one
    could be lost in it.
    """
    contra_legs = [
        entry
        for entry in batch.ledger_entries
        if entry.account_code == ACCOUNT_SALES_RETURNS_AND_ALLOWANCES.code
    ]
    assert len(contra_legs) == N_AMBIGUOUS, "only the ambiguous population posts contra-revenue"

    payments_by_settlement = defaultdict(set)
    for line in batch.recon_lines:
        if line.type is RazorpayEntityType.PAYMENT:
            payments_by_settlement[line.settlement_id].add(line.entity_id)
    refund_parents = {line.payment_id for line in batch.recon_lines if line.type is RazorpayEntityType.REFUND}
    entries_by_reference = defaultdict(list)
    for entry in batch.ledger_entries:
        entries_by_reference[entry.reference].append(entry)

    for leg in contra_legs:
        assert batch.population_of[leg.journal_entry_id] == "ambiguous"
        # Attributable: the reference names a payment inside its own settlement.
        case_id = next(
            settlement_id
            for settlement_id, payments in payments_by_settlement.items()
            if leg.reference in payments
        )
        assert batch.population_of[case_id] == "ambiguous"
        # Uncorroborated: no refund recon line anywhere backs that posting, so T-02 cannot fire.
        assert leg.reference not in refund_parents
        # Internally balanced against its own clearing leg. A clean payment
        # *debits* clearing, so the one clearing *credit* on this reference is
        # unambiguously the phantom pair's other half.
        clearing_credits = [
            entry
            for entry in entries_by_reference[leg.reference]
            if entry.account_code == ACCOUNT_RAZORPAY_CLEARING.code and entry.credit > 0
        ]
        assert len(clearing_credits) == 1
        assert clearing_credits[0].credit == leg.debit


# --- The pass changes nothing it should not. ---


def test_the_finalized_batch_still_holds_the_section_3_5_counts(batch):
    assert len(batch.settlements) == 125
    assert len(batch.ground_truth) == 150
    assert len(_settlement_credits(batch)) == 98
    assert len(batch.bank_lines) == 176  # a later revision: 98 settlement credits + 28 orphan-case + 50 noise
    assert dict(Counter(g.expected_outcome_state.value for g in batch.ground_truth)) == _EXPECTED_STATE_TOTALS
    assert {batch.population_of[b.line_id] for b in batch.bank_lines} >= {NOISE_POPULATION}


def test_the_ledger_still_balances_after_the_pass(batch):
    assert sum(e.debit for e in batch.ledger_entries) == sum(e.credit for e in batch.ledger_entries)
    assert sum(e.debit for e in batch.ledger_entries) > 0


def test_two_runs_with_the_same_seed_produce_an_identical_batch():
    first = generate_reference_batch(random.Random(3), SNAPSHOT)
    second = generate_reference_batch(random.Random(3), SNAPSHOT)
    for records in ("settlements", "recon_lines", "ledger_entries", "bank_lines", "ground_truth"):
        assert [r.model_dump() for r in getattr(first, records)] == [
            r.model_dump() for r in getattr(second, records)
        ]
    assert first.utr_shapes == second.utr_shapes
    assert first.population_of == second.population_of


def test_finalize_refuses_a_batch_it_cannot_shape_to_the_split():
    """More UTR-less settlements than match cascade's absent share leaves is a hard error, not a silently skewed split."""
    rng = random.Random(SEED)
    from generator.exceptions import generate_settlement_utr_missing_batch

    utr_missing_only = generate_settlement_utr_missing_batch(rng, SNAPSHOT, n_cases=5)
    with pytest.raises(ValueError, match="carry no UTR"):
        finalize_batch(rng, parts=[("settlement_utr_missing", utr_missing_only)], noise_bank_lines=[])
