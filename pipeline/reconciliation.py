"""Re-reconciliation: the residual invariant 1.7.5's last check measures.

> - post-adjustment residual equals 0 paise on re-reconciliation.

**§1.7.5 names this residual but never defines it, and the definition is
load-bearing**, so it is derived here rather than assumed.

The spec's only *defined* notion of "residual" is the matcher's
(`Case.residual_paise`, §4.1 component 3): settlement amount minus the
bank deposits that matched it. That reading cannot be the one §1.7.5
means. Family 4's hard precondition (§3.2, REV-15) is that **no bank
credit matching the settlement exists** — that is what makes the ledger's
`Bank Account` debit premature — so a family-4 case's matcher residual is
the full settlement amount whenever the settlement window has also
elapsed, and no journal entry can move it: the money genuinely has not
arrived. Measured on the seed-0 reference batch, 7 of the 10 family-4
cases sit in exactly that position. Under the matcher reading those 7
could never reach `AUTO_CLOSED`, contradicting §3.5's case-allocation
table, which assigns all 10 to it.

**The residual is therefore books-versus-evidence, not bank-versus-
settlement**: the accrual-correct position the case's Razorpay evidence
implies, minus the position the merchant's ledger actually holds, account
by account. That is the question every §3.4 template exists to close, and
it is the reading §3.2 itself uses when it describes what a *wrong*
family-4 posting would do — "understate bank against the statement and
leave clearing permanently open" is a per-account books check, not a
bank-versus-settlement one.

Verified against the reference batch before adoption: this residual is 0
on all 30 ground-truth `AUTO_MATCHED` cases, non-zero on all 71 cases
carrying a correction or an abstention (50 family + 12 FR-06 + 9
ambiguous), and 0 on every one of the 50 `AUTO_CLOSED` cases once its
candidate entries are applied — family 4 included, window or no window.

**The expected position is §3.2, transcribed.** One posting per settled
recon-line type, exactly as §3.2 states it:

| Line type | Accrual-correct posting (§3.0, §3.2) |
|---|---|
| `payment` | `Dr Razorpay Clearing (amount - fee - tax), Dr Payment Gateway Charges (fee), Dr GST on Gateway Charges (tax) / Cr Sales Revenue (amount)` |
| `refund` | `Dr Sales Returns and Allowances (debit) / Cr Razorpay Clearing (debit)` |
| `adjustment`, credit | `Dr Razorpay Clearing (credit) / Cr Razorpay Settlement Adjustments (credit)` |
| `adjustment`, debit | `Dr Razorpay Settlement Adjustments (debit) / Cr Razorpay Clearing (debit)` |

The payment row is §3.2's family-3 worked example's "Correct entry" line;
the other three are the correct postings the family 2 and family 5
templates restore. `Bank Account` is expected at zero throughout: under
§3.0's accrual assumption the sale books to `Razorpay Clearing`, and this
dataset's merchant never records the bank receipt that would later clear
it (§3.2 family 4's no-op case is defined by clearing "simply not having
flipped to `Bank Account` at the batch snapshot"). That is what makes
family 4's premature `Bank Account` debit visible as a residual at all.

**Comparison is per account, in integer paise, and absolute.** Both sides
are internally balanced — every §3.2 posting nets to zero across its
accounts, and so does every posted entry — so a *signed* total difference
is identically zero and would measure nothing. The residual is the sum of
the per-account absolute differences: zero if and only if the merchant's
books agree with Razorpay's evidence on every account the case touches.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from pipeline.accounts import (
    ACCOUNT_GST_ON_GATEWAY_CHARGES,
    ACCOUNT_PAYMENT_GATEWAY_CHARGES,
    ACCOUNT_RAZORPAY_CLEARING,
    ACCOUNT_RAZORPAY_SETTLEMENT_ADJUSTMENTS,
    ACCOUNT_SALES_RETURNS_AND_ALLOWANCES,
    ACCOUNT_SALES_REVENUE,
)
from pipeline.case_assembly import Case, CaseKind
from pipeline.instantiator import CandidateJournalEntry
from pipeline.schemas import LedgerEntry, RazorpayEntityType, ReconLine


class ReconciliationError(Exception):
    """A recon line whose accrual-correct posting this module cannot derive.

    Raised rather than silently skipped: a line type with no posting rule
    would make the residual quietly understate the discrepancy, and a
    residual that reads 0 for the wrong reason is the one failure mode
    §1.7.5's last check exists to prevent.
    """


AccountPositions = dict[str, int]
"""Net debit position in integer paise, keyed by account code. Credit-side is negative."""


def _add(positions: AccountPositions, account_code: str, net_debit_paise: int) -> None:
    positions[account_code] = positions.get(account_code, 0) + net_debit_paise


def expected_positions(recon_lines: Sequence[ReconLine]) -> AccountPositions:
    """§3.2's accrual-correct position per account, derived from Razorpay's evidence alone.

    Reads only `recon_line` fields — Razorpay's own report — never the
    merchant ledger, which is the thing being checked against it.
    """
    positions: AccountPositions = {}
    for line in recon_lines:
        if not line.settled:
            # Razorpay has not settled the line, so nothing about it is yet
            # owed through the clearing account. Every line in the current
            # batch is settled; the branch keeps an unsettled one from being
            # silently counted as if it were.
            continue

        if line.type is RazorpayEntityType.PAYMENT:
            amount, fee, tax = int(line.amount), int(line.fee), int(line.tax)
            _add(positions, ACCOUNT_RAZORPAY_CLEARING.code, amount - fee - tax)
            _add(positions, ACCOUNT_PAYMENT_GATEWAY_CHARGES.code, fee)
            _add(positions, ACCOUNT_GST_ON_GATEWAY_CHARGES.code, tax)
            _add(positions, ACCOUNT_SALES_REVENUE.code, -amount)

        elif line.type is RazorpayEntityType.REFUND:
            debit = int(line.debit)
            _add(positions, ACCOUNT_SALES_RETURNS_AND_ALLOWANCES.code, debit)
            _add(positions, ACCOUNT_RAZORPAY_CLEARING.code, -debit)

        elif line.type is RazorpayEntityType.ADJUSTMENT:
            credit, debit = int(line.credit), int(line.debit)
            if credit > 0:
                _add(positions, ACCOUNT_RAZORPAY_CLEARING.code, credit)
                _add(positions, ACCOUNT_RAZORPAY_SETTLEMENT_ADJUSTMENTS.code, -credit)
            if debit > 0:
                _add(positions, ACCOUNT_RAZORPAY_SETTLEMENT_ADJUSTMENTS.code, debit)
                _add(positions, ACCOUNT_RAZORPAY_CLEARING.code, -debit)

        else:
            raise ReconciliationError(
                f"no §3.2 posting rule for recon line {line.entity_id!r} of type {line.type!r}; "
                "§3.5 excludes `transfer` from the generator, so this is a new line type "
                "needing a stated accrual treatment, not a case to skip"
            )

    return positions


def index_ledger_entries_by_case(entries: Sequence[LedgerEntry]) -> dict[str, tuple[LedgerEntry, ...]]:
    """Group ledger entries by `case_id` — populated only on `controller_adjustment` rows.

    The second of the two joins a case needs. `reference == entity_id`
    (`pipeline.predicates.index_ledger_entries`) finds what the merchant
    posted against a record; this finds what the Controller posted against
    a *case*. A correction aggregated over several records (§3.4) has one
    `reference` but names every case it belongs to here, so this is the
    join that cannot miss a leg.
    """
    grouped: dict[str, list[LedgerEntry]] = {}
    for entry in entries:
        if entry.case_id is not None:
            grouped.setdefault(entry.case_id, []).append(entry)
    return {case_id: tuple(group) for case_id, group in grouped.items()}


def case_ledger_entries(
    case: Case,
    entries_by_reference: Mapping[str, Sequence[LedgerEntry]],
    entries_by_case: Mapping[str, Sequence[LedgerEntry]],
) -> tuple[LedgerEntry, ...]:
    """Every ledger entry belonging to one case, through both joins, deduplicated."""
    seen: set[str] = set()
    collected: list[LedgerEntry] = []
    for line in case.recon_lines:
        for entry in entries_by_reference.get(line.entity_id, ()):
            if entry.journal_entry_id not in seen:
                seen.add(entry.journal_entry_id)
                collected.append(entry)
    for entry in entries_by_case.get(case.case_id, ()):
        if entry.journal_entry_id not in seen:
            seen.add(entry.journal_entry_id)
            collected.append(entry)
    return tuple(collected)


def actual_positions(entries: Iterable[LedgerEntry]) -> AccountPositions:
    """The merchant ledger's net debit position per account, over one case's entries."""
    positions: AccountPositions = {}
    for entry in entries:
        _add(positions, entry.account_code, int(entry.debit) - int(entry.credit))
    return positions


def apply_candidates(positions: AccountPositions, candidates: Iterable[CandidateJournalEntry]) -> AccountPositions:
    """The positions a set of candidate entries would produce, without posting them.

    Used to evaluate §1.7.5's residual check on a candidate that has been
    written inside an open transaction, and equally to answer the same
    question without writing at all.
    """
    projected = dict(positions)
    for candidate in candidates:
        for leg in candidate.legs:
            _add(projected, leg.account_code, leg.debit - leg.credit)
    return projected


def residual_of(expected: AccountPositions, actual: AccountPositions) -> int:
    """Sum of per-account absolute differences, in integer paise. Zero iff the books agree.

    Absolute rather than signed: both sides are internally balanced, so
    their signed totals are both zero and their signed difference is
    identically zero regardless of how wrong the books are.
    """
    return sum(abs(expected.get(code, 0) - actual.get(code, 0)) for code in set(expected) | set(actual))


def orphan_residual_paise(case: Case) -> int:
    """An orphan case's residual: the whole unexplained bank movement.

    An orphan case exists (§1.2, §3.6) precisely because no settlement, no
    recon line and no ledger entry explains its bank line — case assembly
    raised it after failing to attach it to anything. There is no
    evidence-implied position to compare against, so the entire amount is
    unreconciled by construction, and stays so: no §3.4 template addresses
    an orphan, and none of the four §3.6 populations is closeable by a
    journal entry.
    """
    return sum(abs(int(line.deposit_paise) - int(line.withdrawal_paise)) for line in case.bank_lines)


def case_residual_paise(
    case: Case,
    entries_by_reference: Mapping[str, Sequence[LedgerEntry]],
    entries_by_case: Mapping[str, Sequence[LedgerEntry]],
    *,
    pending_candidates: Sequence[CandidateJournalEntry] = (),
) -> int:
    """The case's books-versus-evidence residual, in integer paise.

    `pending_candidates` projects entries that are not in the ledger yet,
    which is how the validator asks "would this correction close the case?"
    before anything is written.
    """
    if case.kind is CaseKind.ORPHAN:
        return orphan_residual_paise(case)

    expected = expected_positions(case.recon_lines)
    actual = actual_positions(case_ledger_entries(case, entries_by_reference, entries_by_case))
    if pending_candidates:
        actual = apply_candidates(actual, pending_candidates)
    return residual_of(expected, actual)
