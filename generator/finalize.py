"""The global pass, per spec.md §3.5's fingerprint control and §4.6's
generator obligation.

> **Mitigation:** generate all records first, then assign IDs and
> timestamps in a single global shuffled pass, and draw narration text
> from one shared pool regardless of scenario. *(§3.5)*

Every population generator builds its own cases in its own loop, which is
the right shape for getting each case *right* and exactly the wrong shape
for what the batch looks like from outside: records leave those loops in
scenario order, and the §4.6 narration split cannot be allocated one
population at a time because its denominator is the whole batch. This
module is the pass that runs once, over everything, after all of it
exists:

1. **IDs** are re-minted for every record in one globally shuffled order,
   so no identifier — and no relationship between identifiers — can carry
   the order its scenario was generated in. Every cross-reference moves
   with them, including the ones embedded in ground-truth prose.
2. **UTR narration variety** (§4.6's 50/25/15/10) is allocated across all
   settlement credits at once, under the two constraints the data itself
   imposes — see `_plan_utr_shapes`.
3. **Emission order** is shuffled, so position in a JSONL file carries no
   scenario information either.

**Timestamps are deliberately not reassigned here**, and that is the one
place this pass reads §3.5's mitigation as a means rather than a rule. A
settlement's `created_at` is evidence: which side of the T+2 working-day
window it falls on decides `EXPECTED_TIMING_DIFFERENCE` versus
`BANK_CREDIT_OVERDUE` (§3.3), the family-4 date-error variant is defined
by a shifted posting date (REV-19), and a bank credit's `value_date` is
read by §4.6 tier 2. Reassigning those globally would not de-correlate the
batch, it would destroy it. What the mitigation is *for* — that no
timestamp block correlate with scenario — is instead met at the point of
generation: every population now draws its settlement date from one
scenario-blind distribution and its time of day from another (see
`generator/clean.py`'s `random_settlement_date` and
`settlement_created_timestamp`), leaving only the window placement that is
evidence. `tests/test_generator_fingerprint.py` asserts the result rather
than trusting the construction.
"""

from __future__ import annotations

import random
import re
from collections import Counter
from dataclasses import dataclass

from generator.batch import GeneratedBatch
from generator.narration import (
    SETTLEMENT_PARTIES,
    UTR_SHAPE_TARGET_SHARE,
    UtrShape,
    credit_narration,
)
from pipeline.ground_truth import GroundTruthCase
from pipeline.schemas import BankLine, LedgerEntry, ReconLine, Settlement

UTR_IN_BANK_REF_NO_PERCENT = 50
"""Share of clean-UTR settlement credits that also carry the UTR in `bank_ref_no`.

§4.6 tier 0 matches on "`settlement.utr` appears as a token in
`bank_line.narration`, **or equals `bank_ref_no`**"; with the column null
everywhere, that second branch was dead. Only clean-shape lines are given
it: putting the UTR in `bank_ref_no` on an embedded, truncated or absent
line would hand the matcher a tier-0 hit on a line the narration split
placed deliberately further down the cascade, and the reported
`match_tier_distribution` would stop reflecting the split it exists to
demonstrate.
"""

_ID_PATTERN = re.compile(r"[a-z]+_[0-9a-f]{8}")
"""Every identifier the generator mints: a lowercase namespace, an underscore, eight hex digits."""


@dataclass(frozen=True)
class FinalBatch:
    """What `uv run generate` writes: the four §3.1 record types plus §1.6 ground truth."""

    settlements: list[Settlement]
    recon_lines: list[ReconLine]
    ledger_entries: list[LedgerEntry]
    bank_lines: list[BankLine]
    ground_truth: list[GroundTruthCase]

    utr_shapes: dict[str, UtrShape]
    """`bank_line.line_id` -> the §4.6 shape its narration was written in.

    A record of what the pass did, for the checkpoint and for the FR-13
    run manifest — not part of the dataset, and never written to a JSONL
    file. The pipeline must recover the tier from the narration itself;
    that recovery is what §4.6 grades.
    """

    settlement_credit_of: dict[str, str]
    """`bank_line.line_id` -> `settlement.id`, carried through from `GeneratedBatch`.

    Generator-side only, for the same reason: recovering this link is what
    §4.6's cascade is graded on, so the pipeline never receives it.
    """

    population_of: dict[str, str]
    """Any record identifier -> the name of the population that generated it.

    The injection plan's own label at the granularity the plan uses (§3.5's
    fourteen settlement-anchored rows, §3.6's four orphan rows, and the
    noise lines), which is finer than the `(class, subtype, state)` triple
    ground truth carries — families 1, 2 and 5 share one triple and are
    three different constructions. The fingerprint checkpoint needs the
    finer label to ask whether an artifact separates *constructions*, not
    just outcomes.

    Generator-side only, like `utr_shapes`: never written to a JSONL file,
    never available to the pipeline. §5.3's reported check runs the same
    statistic over the ground-truth triple, which the eval harness already
    reads.
    """


NOISE_POPULATION = "non_settlement_noise"
"""§3.6's bank-statement noise: real lines, no case, and the label the absence of a case implies."""


def finalize_batch(
    rng: random.Random,
    *,
    parts: list[tuple[str, GeneratedBatch]],
    noise_bank_lines: list[BankLine],
) -> FinalBatch:
    """Combine every named population into one batch and run the global pass over it."""
    combined = GeneratedBatch()
    population_of: dict[str, str] = {}
    for name, part in parts:
        combined.extend(part)
        population_of.update(_record_ids(part, name))
    combined.bank_lines.extend(noise_bank_lines)
    population_of.update({line.line_id: NOISE_POPULATION for line in noise_bank_lines})

    mapping = _reassign_ids_in_shuffled_order(rng, combined)
    utr_shapes = _apply_utr_variety(rng, combined)

    return FinalBatch(
        settlements=_shuffled(rng, combined.settlements),
        recon_lines=_shuffled(rng, combined.recon_lines),
        ledger_entries=_shuffled(rng, combined.ledger_entries),
        bank_lines=_shuffled(rng, combined.bank_lines),
        ground_truth=_shuffled(rng, combined.ground_truth),
        utr_shapes=utr_shapes,
        settlement_credit_of=dict(combined.settlement_credit_of),
        population_of={mapping[old_id]: name for old_id, name in population_of.items()},
    )


def _record_ids(part: GeneratedBatch, name: str) -> dict[str, str]:
    """Every record identity in `part`, mapped to `name`. Identifiers are batch-unique, so one dict covers all five types."""
    return {
        **{settlement.id: name for settlement in part.settlements},
        **{line.entity_id: name for line in part.recon_lines},
        **{entry.journal_entry_id: name for entry in part.ledger_entries},
        **{line.line_id: name for line in part.bank_lines},
        **{case.case_id: name for case in part.ground_truth},
    }


def _shuffled(rng: random.Random, records: list) -> list:
    shuffled = list(records)
    rng.shuffle(shuffled)
    return shuffled


# --- 1. The global shuffled ID pass. ---


def _reassign_ids_in_shuffled_order(rng: random.Random, batch: GeneratedBatch) -> dict[str, str]:
    """Re-mint every identifier in the batch, in one globally shuffled order.

    Each new identifier keeps its namespace (`pay_`, `setl_`, `je_`,
    `bank_`, ...) and takes a fresh random suffix, so identity is preserved
    in kind while every draw order is destroyed. Uniqueness is enforced
    against the whole batch rather than left to chance: nearly six thousand
    independent 32-bit journal-entry ids collide with a probability of
    around one run in two hundred, and a collided `journal_entry_id` would
    silently merge two ledger legs in every downstream join.

    Returns the old-to-new mapping, which is what the caller would need to
    trace a record back to the pre-pass batch.
    """
    old_ids = _collect_ids(batch)
    shuffle_order = _shuffled(rng, old_ids)

    minted: set[str] = set()
    mapping: dict[str, str] = {}
    for old_id in shuffle_order:
        mapping[old_id] = _mint(rng, old_id, minted)
    assert len(set(mapping.values())) == len(mapping), "id re-mint produced a collision"

    batch.settlements[:] = [s.model_copy(update={"id": mapping[s.id]}) for s in batch.settlements]
    batch.recon_lines[:] = [
        line.model_copy(
            update={
                "entity_id": mapping[line.entity_id],
                "settlement_id": _remap(mapping, line.settlement_id),
                "payment_id": _remap(mapping, line.payment_id),
                "order_id": _remap(mapping, line.order_id),
                "dispute_id": _remap(mapping, line.dispute_id),
            }
        )
        for line in batch.recon_lines
    ]
    batch.ledger_entries[:] = [
        entry.model_copy(
            update={
                "journal_entry_id": mapping[entry.journal_entry_id],
                "reference": mapping[entry.reference],
            }
        )
        for entry in batch.ledger_entries
    ]
    batch.bank_lines[:] = [line.model_copy(update={"line_id": mapping[line.line_id]}) for line in batch.bank_lines]
    batch.ground_truth[:] = [
        case.model_copy(
            update={
                "case_id": mapping[case.case_id],
                "expected_linked_source_records": tuple(
                    mapping[record_id] for record_id in case.expected_linked_source_records
                ),
                "expected_resolution": _remap_ids_in_text(mapping, case.expected_resolution),
            }
        )
        for case in batch.ground_truth
    ]
    remapped_credits = {
        mapping[line_id]: mapping[settlement_id] for line_id, settlement_id in batch.settlement_credit_of.items()
    }
    batch.settlement_credit_of.clear()
    batch.settlement_credit_of.update(remapped_credits)

    return mapping


def _collect_ids(batch: GeneratedBatch) -> list[str]:
    """Every distinct identifier in the batch, in first-seen order.

    Includes `ledger_entry.reference` values that resolve to no recon line
    — the phantom references §3.3's `AMBIGUOUS_CASE` construction depends
    on. They are identifiers like any other and must move with the rest;
    leaving them behind would turn "references nothing in the batch" into
    "is the only reference that survived the pass".
    """
    ids: list[str] = []
    for settlement in batch.settlements:
        ids.append(settlement.id)
    for line in batch.recon_lines:
        ids.extend([line.entity_id, line.settlement_id, line.payment_id, line.order_id, line.dispute_id])
    for entry in batch.ledger_entries:
        ids.extend([entry.journal_entry_id, entry.reference])
    for line in batch.bank_lines:
        ids.append(line.line_id)
    for case in batch.ground_truth:
        ids.append(case.case_id)
        ids.extend(case.expected_linked_source_records)

    seen: dict[str, None] = {}
    for identifier in ids:
        if identifier is None:
            continue
        if not _ID_PATTERN.fullmatch(identifier):
            raise ValueError(f"identifier {identifier!r} does not match the generator's id shape")
        seen.setdefault(identifier)
    return list(seen)


def _mint(rng: random.Random, old_id: str, minted: set[str]) -> str:
    namespace = old_id.rpartition("_")[0]
    while True:
        candidate = f"{namespace}_{rng.getrandbits(32):08x}"
        if candidate not in minted:
            minted.add(candidate)
            return candidate


def _remap(mapping: dict[str, str], value: str | None) -> str | None:
    return None if value is None else mapping[value]


def _remap_ids_in_text(mapping: dict[str, str], text: str | None) -> str | None:
    """Rewrite identifiers appearing inside ground-truth prose (§1.6's `expected_resolution`).

    UTRs are untouched: they are uppercase and carry no underscore, so the
    identifier pattern cannot match one.
    """
    if text is None:
        return None
    return _ID_PATTERN.sub(lambda match: mapping.get(match.group(0), match.group(0)), text)


# --- 2. §4.6's UTR narration variety. ---


def _apply_utr_variety(rng: random.Random, batch: GeneratedBatch) -> dict[str, UtrShape]:
    """Rewrite each settlement credit's narration in its planned §4.6 shape."""
    settlements_by_id = {settlement.id: settlement for settlement in batch.settlements}
    lines_by_id = {line.line_id: line for line in batch.bank_lines}
    credit_line_ids = [line.line_id for line in batch.bank_lines if line.line_id in batch.settlement_credit_of]

    plan = _plan_utr_shapes(rng, batch, settlements_by_id=settlements_by_id, credit_line_ids=credit_line_ids)

    rewritten: dict[str, BankLine] = {}
    for line_id in credit_line_ids:
        line = lines_by_id[line_id]
        settlement = settlements_by_id[batch.settlement_credit_of[line_id]]
        shape = plan[line_id]
        update = {
            "narration": credit_narration(
                rng, party=rng.choice(SETTLEMENT_PARTIES), utr=settlement.utr, shape=shape
            )
        }
        if shape is UtrShape.CLEAN and rng.randrange(100) < UTR_IN_BANK_REF_NO_PERCENT:
            update["bank_ref_no"] = settlement.utr
        rewritten[line_id] = line.model_copy(update=update)

    batch.bank_lines[:] = [rewritten.get(line.line_id, line) for line in batch.bank_lines]
    return plan


def _plan_utr_shapes(
    rng: random.Random,
    batch: GeneratedBatch,
    *,
    settlements_by_id: dict[str, Settlement],
    credit_line_ids: list[str],
) -> dict[str, UtrShape]:
    """Allocate §4.6's 50/25/15/10 split across every settlement credit at once.

    Two constraints come from the data, not from the scenario — the plan
    never asks which population a line belongs to:

    - a credit whose settlement carries no UTR **must** be `ABSENT`; there
      is nothing to write. This is `SETTLEMENT_UTR_MISSING`, and it is why
      the absent share is allocated rather than sampled freely: five of the
      ten absent slots are already spoken for, and if those were the *only*
      absent lines then "no UTR in the narration" would identify the
      population outright.
    - a credit may only be made `ABSENT` if §4.6 tier 2 can still recover
      it: its deposit must equal its settlement's amount, and that amount
      must be unique across the batch's bank lines, since "tier 2 is
      accepted only if exactly one candidate exists in the window". Both
      exclusions are read off the records. In practice the first excludes
      `SETTLEMENT_AMOUNT_MISMATCH`, whose bank credit deliberately differs
      from its settlement header — dropping its UTR as well would leave the
      case with no anchor of any kind and no way to be the mismatch it is
      labelled as.
    """
    targets = _largest_remainder_allocation(len(credit_line_ids), UTR_SHAPE_TARGET_SHARE)
    deposit_counts = Counter(line.deposit_paise for line in batch.bank_lines)
    lines_by_id = {line.line_id: line for line in batch.bank_lines}

    forced_absent = [
        line_id
        for line_id in credit_line_ids
        if not settlements_by_id[batch.settlement_credit_of[line_id]].utr
    ]
    if len(forced_absent) > targets[UtrShape.ABSENT]:
        raise ValueError(
            f"{len(forced_absent)} settlements carry no UTR but §4.6 allows only "
            f"{targets[UtrShape.ABSENT]} absent narrations in a batch of {len(credit_line_ids)}"
        )

    eligible_absent = [
        line_id
        for line_id in credit_line_ids
        if line_id not in forced_absent
        and lines_by_id[line_id].deposit_paise == settlements_by_id[batch.settlement_credit_of[line_id]].amount
        and deposit_counts[lines_by_id[line_id].deposit_paise] == 1
    ]
    chosen_absent = set(forced_absent) | set(
        rng.sample(eligible_absent, targets[UtrShape.ABSENT] - len(forced_absent))
    )

    remaining = [line_id for line_id in credit_line_ids if line_id not in chosen_absent]
    shapes = [
        shape
        for shape in (UtrShape.CLEAN, UtrShape.EMBEDDED, UtrShape.TRUNCATED)
        for _ in range(targets[shape])
    ]
    assert len(shapes) == len(remaining), "UTR shape allocation does not cover every settlement credit"
    rng.shuffle(shapes)

    plan = {line_id: UtrShape.ABSENT for line_id in chosen_absent}
    plan.update(dict(zip(remaining, shapes)))
    return plan


def _largest_remainder_allocation(n: int, shares: dict[UtrShape, int]) -> dict[UtrShape, int]:
    """Split `n` items across `shares` (percentages summing to 100) with exact integer arithmetic.

    Largest remainder rather than rounding each share independently, so
    the parts always sum back to `n` — 98 credits at 50/25/15/10 rounds to
    49/24/14/9, which is 96.
    """
    total = sum(shares.values())
    allocation = {shape: n * share // total for shape, share in shares.items()}
    remainders = sorted(
        shares, key=lambda shape: (-(n * shares[shape] % total), list(shares).index(shape))
    )
    for shape in remainders[: n - sum(allocation.values())]:
        allocation[shape] += 1
    return allocation
