"""Invariant 1.7.5's chain and §3.4's two validation layers.

§4.5 puts the test weight here on purpose — "`pytest`, concentrated on
the validator chain and the six templates" — so every check gets a
hand-built candidate that fails it and only it where the checks can be
isolated, plus a batch-level pass asserting the real candidates clear all
eight.

The test that carries the most weight is
`test_the_global_layer_catches_a_malformed_template_the_per_template_layer_cannot`:
§3.4 justifies keeping two overlapping layers on exactly that ground, and
without it the global account-direction table is decoration.
"""

from __future__ import annotations

import random
from datetime import date

import pytest

from generator.cli import generate_reference_batch
from pipeline.accounts import (
    ACCOUNT_BANK_ACCOUNT,
    ACCOUNT_GST_ON_GATEWAY_CHARGES,
    ACCOUNT_PAYMENT_GATEWAY_CHARGES,
    ACCOUNT_RAZORPAY_CLEARING,
    ACCOUNT_RAZORPAY_SETTLEMENT_ADJUSTMENTS,
    ACCOUNT_SALES_RETURNS_AND_ALLOWANCES,
    ACCOUNT_SALES_REVENUE,
    CHART_OF_ACCOUNTS,
    Account,
)
from pipeline.case_assembly import assemble_cases
from pipeline.instantiator import (
    TEMPLATE_LEG_ACCOUNTS,
    CandidateJournalEntry,
    CandidateJournalLeg,
    instantiate_cases,
)
from pipeline.matcher import match_cases
from pipeline.predicates import TemplateId, evaluate_cases
from pipeline.schemas import LedgerEntry, LedgerSource
from pipeline.validator import (
    ACCOUNT_DIRECTIONS,
    TEMPLATE_ALLOWLIST,
    PostingDirection,
    ValidationCheck,
    batch_record_ids,
    index_controller_adjustments,
    posted_resolution_pairs,
    resolution_id_for,
    validate_candidate,
)

SNAPSHOT = date(2026, 8, 28)


def _leg(account: Account, debit: int = 0, credit: int = 0) -> CandidateJournalLeg:
    return CandidateJournalLeg(
        account_code=account.code, account_name=account.name, debit=debit, credit=credit
    )


def _candidate(
    *legs: CandidateJournalLeg,
    template_id: TemplateId = TemplateId.T01,
    case_id: str = "setl_1",
    cited: tuple[str, ...] = ("pay_1",),
) -> CandidateJournalEntry:
    return CandidateJournalEntry(
        case_id=case_id, template_id=template_id, legs=legs, cited_record_ids=cited
    )


def _good_t01() -> CandidateJournalEntry:
    return _candidate(
        _leg(ACCOUNT_PAYMENT_GATEWAY_CHARGES, debit=2_000),
        _leg(ACCOUNT_GST_ON_GATEWAY_CHARGES, debit=360),
        _leg(ACCOUNT_RAZORPAY_CLEARING, credit=2_360),
    )


def _validate(candidate: CandidateJournalEntry, **overrides: object):
    kwargs: dict[str, object] = {
        "known_record_ids": frozenset({"pay_1", "rfnd_1", "adj_1", "setl_1", "je_1"}),
        "adjustments_by_reference": {},
        "posted_pairs": frozenset(),
    }
    kwargs.update(overrides)
    return validate_candidate(candidate, **kwargs)  # type: ignore[arg-type]


def _result(report, check: ValidationCheck):
    return next(result for result in report.results if result.check is check)


# --- §3.4's second layer, transcribed. ---


def test_the_global_direction_table_is_section_3_4s_seven_rows() -> None:
    assert ACCOUNT_DIRECTIONS == {
        ACCOUNT_SALES_REVENUE.code: PostingDirection.CREDIT_ONLY,
        ACCOUNT_SALES_RETURNS_AND_ALLOWANCES.code: PostingDirection.DEBIT_ONLY,
        ACCOUNT_PAYMENT_GATEWAY_CHARGES.code: PostingDirection.DEBIT_ONLY,
        ACCOUNT_GST_ON_GATEWAY_CHARGES.code: PostingDirection.DEBIT_ONLY,
        ACCOUNT_BANK_ACCOUNT.code: PostingDirection.CREDIT_ONLY,
        ACCOUNT_RAZORPAY_CLEARING.code: PostingDirection.BOTH,
        ACCOUNT_RAZORPAY_SETTLEMENT_ADJUSTMENTS.code: PostingDirection.BOTH,
    }
    assert set(ACCOUNT_DIRECTIONS) == {account.code for account in CHART_OF_ACCOUNTS}


def test_the_template_allowlist_is_section_3_4s_six() -> None:
    assert TEMPLATE_ALLOWLIST == frozenset(TemplateId)
    assert len(TEMPLATE_ALLOWLIST) == 6


def test_every_declared_template_obeys_the_global_direction_table() -> None:
    """The two §3.4 layers must agree with each other on the six real templates.

    A template declaring an account on a side the global table forbids
    would be a malformed template shipped in the allowlist — the exact
    thing the second layer exists to catch, checked here at rest.
    """
    for template_id, (debit_accounts, credit_accounts) in TEMPLATE_LEG_ACCOUNTS.items():
        for account in debit_accounts:
            assert ACCOUNT_DIRECTIONS[account.code] is not PostingDirection.CREDIT_ONLY, (
                f"{template_id} debits {account.name}, which is credit-only"
            )
        for account in credit_accounts:
            assert ACCOUNT_DIRECTIONS[account.code] is not PostingDirection.DEBIT_ONLY, (
                f"{template_id} credits {account.name}, which is debit-only"
            )


def test_the_global_layer_catches_a_malformed_template_the_per_template_layer_cannot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§3.4: "a broken template passes its own rules by definition".

    `T-04` is redeclared to debit `Bank Account` — which the global table
    permits only on the credit side. The candidate then satisfies its own
    template's account sets and is caught by the global layer alone,
    which is the entire argument for keeping both.
    """
    monkeypatch.setitem(
        TEMPLATE_LEG_ACCOUNTS,
        TemplateId.T04,
        ((ACCOUNT_BANK_ACCOUNT,), (ACCOUNT_RAZORPAY_CLEARING,)),
    )
    candidate = _candidate(
        _leg(ACCOUNT_BANK_ACCOUNT, debit=50_000),
        _leg(ACCOUNT_RAZORPAY_CLEARING, credit=50_000),
        template_id=TemplateId.T04,
    )

    report = _validate(candidate)

    assert _result(report, ValidationCheck.TEMPLATE_ACCOUNTS_PERMITTED).passed
    assert not _result(report, ValidationCheck.ACCOUNT_DIRECTION_PERMITTED).passed
    assert not report.passed


# --- The chain, one failure at a time. ---


def test_a_well_formed_candidate_passes_every_check() -> None:
    report = _validate(_good_t01())

    assert report.passed
    assert report.failures == ()
    assert report.resolution_id == "res_T-01"


def test_an_account_outside_the_chart_is_rejected() -> None:
    candidate = _candidate(
        _leg(Account("9999", "Suspense"), debit=2_360),
        _leg(ACCOUNT_RAZORPAY_CLEARING, credit=2_360),
    )

    report = _validate(candidate)

    assert not _result(report, ValidationCheck.ACCOUNT_IN_CHART).passed
    assert "9999" in _result(report, ValidationCheck.ACCOUNT_IN_CHART).detail


def test_a_credit_only_account_debited_is_rejected() -> None:
    candidate = _candidate(
        _leg(ACCOUNT_SALES_REVENUE, debit=2_360),
        _leg(ACCOUNT_RAZORPAY_CLEARING, credit=2_360),
        template_id=TemplateId.T03,
    )

    assert not _result(_validate(candidate), ValidationCheck.ACCOUNT_DIRECTION_PERMITTED).passed


def test_a_debit_only_account_credited_is_rejected() -> None:
    candidate = _candidate(
        _leg(ACCOUNT_RAZORPAY_CLEARING, debit=2_360),
        _leg(ACCOUNT_SALES_RETURNS_AND_ALLOWANCES, credit=2_360),
        template_id=TemplateId.T02,
    )

    assert not _result(_validate(candidate), ValidationCheck.ACCOUNT_DIRECTION_PERMITTED).passed


def test_a_template_outside_the_allowlist_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pipeline.validator.TEMPLATE_ALLOWLIST", frozenset({TemplateId.T02}))

    report = _validate(_good_t01())

    assert not _result(report, ValidationCheck.TEMPLATE_ALLOWLISTED).passed


def test_an_account_not_permitted_for_this_template_is_rejected() -> None:
    """The accounts are legal and in a legal direction — just not for `T-02`."""
    candidate = _candidate(
        _leg(ACCOUNT_PAYMENT_GATEWAY_CHARGES, debit=2_000),
        _leg(ACCOUNT_RAZORPAY_CLEARING, credit=2_000),
        template_id=TemplateId.T02,
    )

    report = _validate(candidate)

    assert _result(report, ValidationCheck.ACCOUNT_DIRECTION_PERMITTED).passed
    assert not _result(report, ValidationCheck.TEMPLATE_ACCOUNTS_PERMITTED).passed


def test_an_unbalanced_entry_is_rejected() -> None:
    candidate = _candidate(
        _leg(ACCOUNT_PAYMENT_GATEWAY_CHARGES, debit=2_000),
        _leg(ACCOUNT_RAZORPAY_CLEARING, credit=1_999),
    )

    report = _validate(candidate)

    assert not _result(report, ValidationCheck.ENTRY_BALANCED).passed
    assert "2000" in _result(report, ValidationCheck.ENTRY_BALANCED).detail


def test_a_zero_value_entry_is_rejected_even_though_it_balances() -> None:
    candidate = _candidate(
        _leg(ACCOUNT_PAYMENT_GATEWAY_CHARGES, debit=0),
        _leg(ACCOUNT_RAZORPAY_CLEARING, credit=0),
    )

    assert not _result(_validate(candidate), ValidationCheck.ENTRY_BALANCED).passed


def test_a_cited_record_absent_from_the_batch_is_rejected() -> None:
    candidate = _candidate(
        _leg(ACCOUNT_PAYMENT_GATEWAY_CHARGES, debit=2_360),
        _leg(ACCOUNT_RAZORPAY_CLEARING, credit=2_360),
        cited=("pay_1", "pay_nonexistent"),
    )

    report = _validate(candidate)

    assert not _result(report, ValidationCheck.CITED_RECORDS_EXIST).passed
    assert "pay_nonexistent" in _result(report, ValidationCheck.CITED_RECORDS_EXIST).detail


def test_an_entry_citing_nothing_is_rejected() -> None:
    """§1.7.3: "no unsourced conclusions reach an auto-action state"."""
    candidate = _candidate(
        _leg(ACCOUNT_PAYMENT_GATEWAY_CHARGES, debit=2_360),
        _leg(ACCOUNT_RAZORPAY_CLEARING, credit=2_360),
        cited=(),
    )

    assert not _result(_validate(candidate), ValidationCheck.CITED_RECORDS_EXIST).passed


def test_a_previously_posted_pair_is_rejected() -> None:
    report = _validate(_good_t01(), posted_pairs=frozenset({("setl_1", "res_T-01")}))

    assert not _result(report, ValidationCheck.NOT_PREVIOUSLY_POSTED).passed


def test_a_record_already_corrected_by_another_case_is_rejected() -> None:
    """The record-side check, and the one thing it catches that the case-side check cannot.

    Re-running the same case would never surface a correction posted
    against one of its records by a *different* case.
    """
    other_case_adjustment = LedgerEntry(
        journal_entry_id="je_other",
        date=SNAPSHOT,
        account_code=ACCOUNT_RAZORPAY_CLEARING.code,
        account_name=ACCOUNT_RAZORPAY_CLEARING.name,
        debit=0,
        credit=2_360,
        reference="pay_1",
        narration="Controller adjustment",
        source=LedgerSource.CONTROLLER_ADJUSTMENT,
        resolution_id="res_T-01",
        case_id="setl_somewhere_else",
    )

    report = _validate(
        _good_t01(),
        adjustments_by_reference={"pay_1": (other_case_adjustment,)},
    )

    assert _result(report, ValidationCheck.NOT_PREVIOUSLY_POSTED).passed
    assert not _result(report, ValidationCheck.CITED_RECORDS_UNPOSTED).passed


# --- Ordering, ids, and the batch. ---


def test_account_checks_are_reported_before_the_balance_check() -> None:
    """§3.4: an entry using a forbidden account "is rejected before the balance check runs"."""
    order = [result.check for result in _validate(_good_t01()).results]

    assert order.index(ValidationCheck.ACCOUNT_IN_CHART) < order.index(ValidationCheck.ENTRY_BALANCED)
    assert order.index(ValidationCheck.ACCOUNT_DIRECTION_PERMITTED) < order.index(ValidationCheck.ENTRY_BALANCED)
    assert order.index(ValidationCheck.TEMPLATE_ACCOUNTS_PERMITTED) < order.index(ValidationCheck.ENTRY_BALANCED)


def test_every_check_runs_even_after_one_fails() -> None:
    """§1.8's audit trail is "the specific safety validations passed", which a
    chain that short-circuits cannot produce."""
    candidate = _candidate(
        _leg(Account("9999", "Suspense"), debit=1),
        _leg(ACCOUNT_RAZORPAY_CLEARING, credit=999),
        cited=(),
    )

    report = _validate(candidate)

    assert len(report.results) == 8
    assert len(report.failures) >= 3


def test_resolution_id_is_a_deterministic_function_of_the_template() -> None:
    ids = {template_id: resolution_id_for(template_id) for template_id in TemplateId}

    assert len(set(ids.values())) == 6, "unique per (case_id, template_id) per §3.4"
    assert ids[TemplateId.T01] == resolution_id_for(TemplateId.T01)


@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_every_candidate_in_the_reference_batch_passes_the_whole_chain(seed: int) -> None:
    batch = generate_reference_batch(random.Random(seed), SNAPSHOT)
    cases = match_cases(
        assemble_cases(batch.settlements, batch.recon_lines, batch.bank_lines),
        batch.bank_lines,
        snapshot_date=SNAPSHOT,
    )
    evidences = evaluate_cases(cases, batch.ledger_entries)
    candidates = instantiate_cases(evidences, cases, batch.ledger_entries)
    known = batch_record_ids(cases, batch.ledger_entries)
    adjustments = index_controller_adjustments(batch.ledger_entries)
    posted = posted_resolution_pairs(batch.ledger_entries)

    assert candidates, "no candidates to validate"
    assert posted == frozenset(), "the generator's ledger holds no controller adjustments"

    for candidate in candidates:
        report = validate_candidate(
            candidate,
            known_record_ids=known,
            adjustments_by_reference=adjustments,
            posted_pairs=posted,
        )
        assert report.passed, f"{candidate.case_id} {candidate.template_id}: {report.failures}"
