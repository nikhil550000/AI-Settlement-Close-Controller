"""The instantiator: turns a fired predicate hit into deterministic ledger legs.

> **Instantiator.** Template -> candidate JV; deterministic amount
> derivation, zero-leg omission, per-case aggregation.

Runs downstream of the predicate evaluator: its input is
`pipeline.predicates.CaseEvidence.template_hits`, one `PredicateHit` per
`(case_id, entity_id, template_id)` that fired. This module never decides
*whether* a template applies — template mutual exclusivity is already
enforced upstream, so every hit here is instantiated. It also never
decides whether the resulting entry is safe to auto-apply — that is the
validator's chain. This module's only job is turning a fired predicate into
the specific, deterministic legs the model is allowed to produce here: the
model may have classified which template applies; it may not touch an
account or an amount.

**Amount derivation is the templates' "Amount source" column, transcribed
directly** (`TEMPLATE_LEG_ACCOUNTS` + `_LEG_DELTAS`), not re-derived from
matcher or predicate output. `T-04` is the one template whose amount is
not read off a `recon_line` field at all: it is the ledger's premature
`Bank Account` debit amount, whatever it is — not a recomputed net figure —
so this module looks up the exact ledger entry the predicate
already cited (`PredicateHit.cited_record_ids[1]`, its `journal_entry_id`)
and reads that entry's own `debit` field.

**Zero-amount legs are omitted, not posted.** Only `T-01`/`T-03`
can ever exercise this in practice — `T-02`/`T-04`/`T-05`/`T-06` each
carry one single-sourced amount that the firing predicate already
required to be positive (`debit > 0` / `credit > 0` / a bank debit whose
own `debit > 0`), so neither of that template's two legs can be zero. A
`T-01`/`T-03` case with `tax = 0` (Razorpay's own samples show this on
refund and AMEX lines) collapses from three legs to two: `Dr Payment
Gateway Charges / Cr Razorpay Clearing`, still a legal `T-01`
instantiation, not a seventh template.

**Aggregation: one entry per case per template.** A settlement
case can in principle carry more than one fired hit for the same template
(e.g. two unposted-fee payments within one settlement) — the current
generator never constructs that shape (each family case
plants exactly one anomalous entity), but the aggregation logic is
written to the general rule regardless, summing every hit's leg deltas
before applying zero-leg omission, with every contributing hit's cited
record IDs carried into the aggregate entry's own citation list.

**Multiple templates per case are permitted** — `instantiate_case`
returns one `CandidateJournalEntry` per distinct template ID that fired,
in `TemplateId` order for determinism.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict

from pipeline.accounts import (
    ACCOUNT_BANK_ACCOUNT,
    ACCOUNT_GST_ON_GATEWAY_CHARGES,
    ACCOUNT_PAYMENT_GATEWAY_CHARGES,
    ACCOUNT_RAZORPAY_CLEARING,
    ACCOUNT_RAZORPAY_SETTLEMENT_ADJUSTMENTS,
    ACCOUNT_SALES_REVENUE,
    ACCOUNT_SALES_RETURNS_AND_ALLOWANCES,
    Account,
)
from pipeline.case_assembly import Case
from pipeline.predicates import CaseEvidence, PredicateHit, TemplateId
from pipeline.schemas import LedgerEntry, ReconLine


class CandidateJournalLeg(BaseModel):
    """One debit-or-credit leg of a candidate correcting entry.

    Both `debit` and `credit` are carried (one always 0), matching
    `pipeline.schemas.LedgerEntry`'s own shape — this is what a leg looks
    like once posted, not a transformed representation of it.
    """

    model_config = ConfigDict(frozen=True)

    account_code: str
    account_name: str
    debit: int
    credit: int


class CandidateJournalEntry(BaseModel):
    """One template instantiation for one case: the aggregated legs plus every citing record."""

    model_config = ConfigDict(frozen=True)

    case_id: str
    template_id: TemplateId
    legs: tuple[CandidateJournalLeg, ...]
    cited_record_ids: tuple[str, ...]


class InstantiationError(Exception):
    """A template's derived legs failed to balance or collapsed below two legs.

    Not expected to fire against any case the predicate evaluator has
    passed through — every predicate's positive-amount conjunct
    (`fee > 0`, `debit > 0`, `credit > 0`, a bank debit with `debit > 0`)
    guarantees at least one leg pair survives zero-leg omission. Raised
    rather than silently swallowed so a future predicate change that
    breaks this guarantee fails loudly here, not as a malformed posting
    three components downstream.
    """


TEMPLATE_LEG_ACCOUNTS: dict[TemplateId, tuple[tuple[Account, ...], tuple[Account, ...]]] = {
    TemplateId.T01: ((ACCOUNT_PAYMENT_GATEWAY_CHARGES, ACCOUNT_GST_ON_GATEWAY_CHARGES), (ACCOUNT_RAZORPAY_CLEARING,)),
    TemplateId.T02: ((ACCOUNT_SALES_RETURNS_AND_ALLOWANCES,), (ACCOUNT_RAZORPAY_CLEARING,)),
    TemplateId.T03: ((ACCOUNT_PAYMENT_GATEWAY_CHARGES, ACCOUNT_GST_ON_GATEWAY_CHARGES), (ACCOUNT_SALES_REVENUE,)),
    TemplateId.T04: ((ACCOUNT_RAZORPAY_CLEARING,), (ACCOUNT_BANK_ACCOUNT,)),
    TemplateId.T05: ((ACCOUNT_RAZORPAY_CLEARING,), (ACCOUNT_RAZORPAY_SETTLEMENT_ADJUSTMENTS,)),
    TemplateId.T06: ((ACCOUNT_RAZORPAY_SETTLEMENT_ADJUSTMENTS,), (ACCOUNT_RAZORPAY_CLEARING,)),
}
"""The templates' debit-legs / credit-legs account tuples, one entry per template, in table order.

This is the fixed allowlist that keeps the model off the money path: an
account and its side within a template are never a runtime choice, model or
otherwise.
"""

LegDeltas = tuple[dict[str, int], dict[str, int]]
"""One hit's un-aggregated (debit_deltas_by_account_code, credit_deltas_by_account_code)."""

LegDeltasFn = Callable[[ReconLine, PredicateHit, Mapping[str, LedgerEntry]], LegDeltas]


def _deltas_t01(line: ReconLine, _hit: PredicateHit, _ledger_by_id: Mapping[str, LedgerEntry]) -> LegDeltas:
    fee, tax = int(line.fee), int(line.tax)
    debit = {ACCOUNT_PAYMENT_GATEWAY_CHARGES.code: fee, ACCOUNT_GST_ON_GATEWAY_CHARGES.code: tax}
    credit = {ACCOUNT_RAZORPAY_CLEARING.code: fee + tax}
    return debit, credit


def _deltas_t02(line: ReconLine, _hit: PredicateHit, _ledger_by_id: Mapping[str, LedgerEntry]) -> LegDeltas:
    amount = int(line.debit)
    return {ACCOUNT_SALES_RETURNS_AND_ALLOWANCES.code: amount}, {ACCOUNT_RAZORPAY_CLEARING.code: amount}


def _deltas_t03(line: ReconLine, _hit: PredicateHit, _ledger_by_id: Mapping[str, LedgerEntry]) -> LegDeltas:
    fee, tax = int(line.fee), int(line.tax)
    debit = {ACCOUNT_PAYMENT_GATEWAY_CHARGES.code: fee, ACCOUNT_GST_ON_GATEWAY_CHARGES.code: tax}
    credit = {ACCOUNT_SALES_REVENUE.code: fee + tax}
    return debit, credit


def _deltas_t04(_line: ReconLine, hit: PredicateHit, ledger_by_id: Mapping[str, LedgerEntry]) -> LegDeltas:
    # The ledger's premature Bank Account debit amount, whatever it is —
    # not a recomputed net figure. `_predicate_t04` cites
    # `(entity_id, bank_debit.journal_entry_id, [settlement.id])`.
    bank_debit = ledger_by_id[hit.cited_record_ids[1]]
    amount = int(bank_debit.debit)
    return {ACCOUNT_RAZORPAY_CLEARING.code: amount}, {ACCOUNT_BANK_ACCOUNT.code: amount}


def _deltas_t05(line: ReconLine, _hit: PredicateHit, _ledger_by_id: Mapping[str, LedgerEntry]) -> LegDeltas:
    amount = int(line.credit)
    return {ACCOUNT_RAZORPAY_CLEARING.code: amount}, {ACCOUNT_RAZORPAY_SETTLEMENT_ADJUSTMENTS.code: amount}


def _deltas_t06(line: ReconLine, _hit: PredicateHit, _ledger_by_id: Mapping[str, LedgerEntry]) -> LegDeltas:
    amount = int(line.debit)
    return {ACCOUNT_RAZORPAY_SETTLEMENT_ADJUSTMENTS.code: amount}, {ACCOUNT_RAZORPAY_CLEARING.code: amount}


_LEG_DELTAS: dict[TemplateId, LegDeltasFn] = {
    TemplateId.T01: _deltas_t01,
    TemplateId.T02: _deltas_t02,
    TemplateId.T03: _deltas_t03,
    TemplateId.T04: _deltas_t04,
    TemplateId.T05: _deltas_t05,
    TemplateId.T06: _deltas_t06,
}


def index_ledger_entries_by_id(entries: Sequence[LedgerEntry]) -> dict[str, LedgerEntry]:
    """Ledger entries keyed by `journal_entry_id` — `T-04`'s lookup, distinct from
    `pipeline.predicates.index_ledger_entries`'s grouping by `reference`."""
    return {entry.journal_entry_id: entry for entry in entries}


def _instantiate_template(
    template_id: TemplateId,
    hits: Sequence[PredicateHit],
    case: Case,
    ledger_by_id: Mapping[str, LedgerEntry],
) -> CandidateJournalEntry:
    recon_by_entity = {line.entity_id: line for line in case.recon_lines}
    leg_deltas_fn = _LEG_DELTAS[template_id]

    debit_totals: dict[str, int] = {}
    credit_totals: dict[str, int] = {}
    cited: list[str] = []
    seen: set[str] = set()

    for hit in hits:
        line = recon_by_entity[hit.entity_id]
        debit_deltas, credit_deltas = leg_deltas_fn(line, hit, ledger_by_id)
        for code, amount in debit_deltas.items():
            debit_totals[code] = debit_totals.get(code, 0) + amount
        for code, amount in credit_deltas.items():
            credit_totals[code] = credit_totals.get(code, 0) + amount
        for record_id in hit.cited_record_ids:
            if record_id not in seen:
                seen.add(record_id)
                cited.append(record_id)

    debit_accounts, credit_accounts = TEMPLATE_LEG_ACCOUNTS[template_id]
    legs = [
        CandidateJournalLeg(account_code=account.code, account_name=account.name, debit=amount, credit=0)
        for account in debit_accounts
        if (amount := debit_totals.get(account.code, 0)) > 0
    ] + [
        CandidateJournalLeg(account_code=account.code, account_name=account.name, debit=0, credit=amount)
        for account in credit_accounts
        if (amount := credit_totals.get(account.code, 0)) > 0
    ]

    total_debit = sum(leg.debit for leg in legs)
    total_credit = sum(leg.credit for leg in legs)
    if total_debit != total_credit:
        raise InstantiationError(
            f"{template_id} on case {case.case_id!r} does not balance: "
            f"debits={total_debit} credits={total_credit}"
        )
    if len(legs) < 2:
        raise InstantiationError(
            f"{template_id} on case {case.case_id!r} collapsed to {len(legs)} leg(s) after zero-leg omission"
        )

    return CandidateJournalEntry(
        case_id=case.case_id,
        template_id=template_id,
        legs=tuple(legs),
        cited_record_ids=tuple(cited),
    )


_TEMPLATE_ORDER: tuple[TemplateId, ...] = (
    TemplateId.T01,
    TemplateId.T02,
    TemplateId.T03,
    TemplateId.T04,
    TemplateId.T05,
    TemplateId.T06,
)


def instantiate_case(
    evidence: CaseEvidence,
    case: Case,
    ledger_by_id: Mapping[str, LedgerEntry],
) -> tuple[CandidateJournalEntry, ...]:
    """Every candidate JV for one case's fired template hits, one entry per template, `TemplateId` order."""
    hits_by_template: dict[TemplateId, list[PredicateHit]] = {}
    for hit in evidence.template_hits:
        hits_by_template.setdefault(hit.template_id, []).append(hit)

    return tuple(
        _instantiate_template(template_id, hits_by_template[template_id], case, ledger_by_id)
        for template_id in _TEMPLATE_ORDER
        if template_id in hits_by_template
    )


def instantiate_cases(
    evidences: Sequence[CaseEvidence],
    cases: Sequence[Case],
    ledger_entries: Sequence[LedgerEntry],
) -> list[CandidateJournalEntry]:
    """Every candidate JV across a batch. `cases` must be the same cases `evidences` was evaluated over."""
    cases_by_id = {case.case_id: case for case in cases}
    ledger_by_id = index_ledger_entries_by_id(ledger_entries)

    result: list[CandidateJournalEntry] = []
    for evidence in evidences:
        if not evidence.template_hits:
            continue
        case = cases_by_id[evidence.case_id]
        result.extend(instantiate_case(evidence, case, ledger_by_id))
    return result
