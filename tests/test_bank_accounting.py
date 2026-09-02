"""`pipeline/bank_accounting.py` — the partition proof, and the regression it exists for.

**The defect this file guards.** `data/contested/` shipped with Rs 12,693.20 of
real bank credit attached to nothing. Four credits narrate the gateway, so
`assemble_orphan_cases` excluded them from orphan consideration; FR-09 tier-2
demotion then dropped them from every settlement that had claimed them. The
money was in no case, no metric and no report, and **no test could see it**,
because every §1.6 metric is denominated in cases and a bank line that reaches
no case is invisible to all of them. The README carried it as a known
limitation for exactly this reason: it was found by reading, not by failing.

So the assertions below are deliberately denominated in *source records*. The
load-bearing one is `test_the_partition_is_total_on_every_committed_batch`:
every bank line lands in exactly one disposition, and `UNACCOUNTED` is empty.
A future change that makes a line reachable by no rule turns that red, instead
of quietly removing money from the batch.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from pipeline.bank_accounting import BankLineDisposition, account_bank_lines
from pipeline.case_assembly import assemble_cases
from pipeline.llm_cache import CacheMode, PromptCache
from pipeline.loaders import load_bank_lines, load_recon_lines, load_settlements
from pipeline.matcher import match_cases
from pipeline.semantics import KEYWORD, LlmSemantics

REPO_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_DATE = date(2026, 8, 28)

CONTESTED_CREDIT_PAISE = 1_269_320
"""Rs 12,693.20 — the four contested credits, and the figure the README's
"known limitation" quoted while nothing in the code could count it."""

AWARDED_BY_THE_MODEL_PAISE = 732_300
"""Rs 7,323.00 — the two decidable credits the LLM arm returns to a settlement
and the keyword arm leaves unattached. `test_the_model_arm_returns_money_to_a
_settlement` is what measures it."""


def _llm_semantics():
    return LlmSemantics(
        PromptCache(REPO_ROOT / "data" / "semantics_cache.json"), mode=CacheMode.STRICT, client=None
    )


def _accounting(batch_name: str, semantics):
    data_dir = REPO_ROOT / "data" / batch_name
    settlements = load_settlements(data_dir / "settlements.jsonl")
    recon_lines = load_recon_lines(data_dir / "recon_lines.jsonl")
    bank_lines = load_bank_lines(data_dir / "bank_lines.jsonl")
    cases = match_cases(
        assemble_cases(settlements, recon_lines, bank_lines, semantics=semantics),
        bank_lines,
        snapshot_date=SNAPSHOT_DATE,
        semantics=semantics,
    )
    return account_bank_lines(bank_lines, cases, semantics=semantics), bank_lines, cases


@pytest.mark.parametrize("batch_name", ["reference", "contested", "heldout_vocab", "adversarial"])
def test_the_partition_is_total_on_every_committed_batch(batch_name):
    """Every bank line lands in exactly one disposition, and none lands in `UNACCOUNTED`.

    This is the assertion the Rs 12,693.20 defect would have failed. It is
    parametrised over every committed batch rather than the one that exhibited
    the bug, because the point is the invariant, not the fixture.
    """
    accounting, bank_lines, _ = _accounting(batch_name, KEYWORD)

    assert accounting.total_lines == len(bank_lines), "a line was counted twice or not at all"
    assert accounting.unaccounted_line_ids == ()
    assert accounting.is_total

    # Exhaustive in the other direction too: the enum and the counts agree, so a
    # disposition added without a bucket cannot silently report nothing.
    assert set(accounting.counts) == {str(value) for value in BankLineDisposition}
    assert set(accounting.credit_paise) == {str(value) for value in BankLineDisposition}


def test_the_contested_credits_are_named_not_dropped():
    """The regression, stated as the money it moves.

    On the keyword arm all four contested credits are unawarded — correctly, since
    no §4.6 tier can say which settlement owns one. What must never happen again is
    them being unawarded *and* uncounted.
    """
    accounting, _, _ = _accounting("contested", KEYWORD)

    assert accounting.counts[str(BankLineDisposition.CONTESTED_UNAWARDED)] == 4
    assert accounting.contested_unawarded_paise == CONTESTED_CREDIT_PAISE
    assert accounting.contested_unawarded_line_ids == (
        "con_bank_dec_c",
        "con_bank_dec_d",
        "con_bank_undec_a",
        "con_bank_undec_b",
    )


def test_a_contested_credit_reaches_the_case_that_claimed_it():
    """"In no case" was half the defect. A demoted claimant must still carry the line.

    `contested_bank_lines`, never `bank_lines` — the case is not matched to this
    credit, and putting it back in `bank_lines` would hand the predicates and the
    instantiator evidence the matcher just ruled inadmissible.
    """
    _, _, cases = _accounting("contested", KEYWORD)
    claimants = [case for case in cases if case.contested_bank_lines]

    assert len(claimants) == 8, "two settlements contend for each of the four credits"
    for case in claimants:
        assert case.bank_lines == (), "a demoted claimant is matched to nothing"
        assert all(line.deposit_paise > 0 for line in case.contested_bank_lines)

    carried = {line.line_id for case in claimants for line in case.contested_bank_lines}
    assert carried == {"con_bank_undec_a", "con_bank_undec_b", "con_bank_dec_c", "con_bank_dec_d"}


def test_the_model_arm_returns_money_to_a_settlement():
    """The contested result, restated in rupees rather than in cases.

    `tests/test_contested.py` measures the model arm as 6/12 -> 8/12 on state
    accuracy. This is the same result denominated in money: the grounded read
    awards two credits the keyword arm cannot, so Rs 7,323.00 of bank credit
    reaches a settlement instead of sitting unattached. Strictly additive — the
    keyword arm's four unawarded credits are a superset of the model arm's two.
    """
    keyword, _, _ = _accounting("contested", KEYWORD)
    llm, _, _ = _accounting("contested", _llm_semantics())

    assert llm.counts[str(BankLineDisposition.CONTESTED_UNAWARDED)] == 2
    assert set(llm.contested_unawarded_line_ids) < set(keyword.contested_unawarded_line_ids)

    returned = keyword.contested_unawarded_paise - llm.contested_unawarded_paise
    assert returned == AWARDED_BY_THE_MODEL_PAISE

    settled = str(BankLineDisposition.SETTLEMENT_EVIDENCE)
    assert llm.credit_paise[settled] - keyword.credit_paise[settled] == AWARDED_BY_THE_MODEL_PAISE

    # Both arms still place every line: resolving a contest moves money between
    # buckets, it never creates or destroys any.
    assert keyword.is_total and llm.is_total
    assert sum(keyword.credit_paise.values()) == sum(llm.credit_paise.values())


def test_the_reference_batch_has_no_contention_and_is_unaffected():
    """The batch the repository has always shipped is untouched by all of this.

    `generator/clean.py` draws amounts lognormally, so an exact tier-2 collision
    inside one window essentially never occurs — which is why the defect survived
    602 tests and six seeds. Pinned so that a generator change introducing
    contention into the reference batch is a visible event rather than a silent
    shift in every §1.6 denominator.
    """
    accounting, _, cases = _accounting("reference", KEYWORD)

    assert accounting.counts[str(BankLineDisposition.CONTESTED_UNAWARDED)] == 0
    assert accounting.contested_unawarded_paise == 0
    assert not any(case.contested_bank_lines for case in cases)


def test_noise_dispositions_are_read_from_case_assemblys_own_rules():
    """The reference batch's noise buckets are populated, not merely empty.

    A partition that classified everything as `OUTBOUND_NOISE` would also pass
    `test_the_partition_is_total...`. This pins that the three noise rules each
    actually fire, so the buckets are meaningful rather than decorative.
    """
    accounting, _, _ = _accounting("reference", KEYWORD)
    counts = accounting.counts

    assert counts[str(BankLineDisposition.BANK_CHARGE)] > 0
    assert counts[str(BankLineDisposition.SELF_MATCHED_REVERSAL)] > 0
    assert counts[str(BankLineDisposition.OUTBOUND_NOISE)] > 0
    assert counts[str(BankLineDisposition.SETTLEMENT_EVIDENCE)] > 0
    assert counts[str(BankLineDisposition.ORPHAN_EVIDENCE)] > 0
