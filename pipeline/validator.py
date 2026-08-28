"""The validator, per spec.md §4.1 component 7.

> **Validator.** The invariant 1.7.5 chain plus both §3.4 validation layers.

Invariant 1.7.5, in full:

> **Any failed safety validation prevents auto-action.** Validations
> applied to every candidate JV before it reaches `AUTO_CLOSED`:
> - `sum(debits_paise) == sum(credits_paise)`;
> - selected template is in the allowlist;
> - each account used is permitted for the selected template (per
>   template-specific allowed accounts and posting directions);
> - all cited source records exist and are unposted for this specific
>   correction;
> - the entry has not previously been posted for this
>   `(case_id, resolution_id)`;
> - post-adjustment residual equals 0 paise on re-reconciliation.

Five of the six run here. The sixth — the post-adjustment residual — is a
statement about the ledger *after* the entry lands, so it runs in
`pipeline.apply` where the write happens, inside the transaction that the
answer decides whether to keep. It is named in `ValidationCheck` all the
same, so the chain reads as one list in the audit trail (§1.8) rather
than as five checks plus an unlabelled afterthought.

**§3.4's two layers are separate checks, not one.** The per-template
layer ("each template declares an allowed debit-account set, an allowed
credit-account set") cannot catch a malformed template — "a broken
template passes its own rules by definition" — so the global
account-direction allowlist runs beside it as an independent guard. Both
are transcribed here from §3.4 rather than derived from
`TEMPLATE_LEG_ACCOUNTS`: deriving the direction table from the very
tables it exists to check would make it decorative, which is exactly the
failure §3.4 calls out. The overlap between them is the point.

**Ordering.** §3.4 requires that "any candidate entry using an account
outside the seven, or in a direction outside this table, is rejected
before the balance check runs", so the account checks precede the balance
check in `ValidationCheck`'s declared order and in every report. Every
check nonetheless *runs*, and every result is recorded: §1.8's audit
trail is "the specific safety validations passed", which a chain that
short-circuits on the first failure cannot produce. `passed` is the
conjunction, so nothing is weakened by evaluating the rest.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from pipeline.accounts import (
    ACCOUNT_BANK_ACCOUNT,
    ACCOUNT_BY_CODE,
    ACCOUNT_GST_ON_GATEWAY_CHARGES,
    ACCOUNT_PAYMENT_GATEWAY_CHARGES,
    ACCOUNT_RAZORPAY_CLEARING,
    ACCOUNT_RAZORPAY_SETTLEMENT_ADJUSTMENTS,
    ACCOUNT_SALES_RETURNS_AND_ALLOWANCES,
    ACCOUNT_SALES_REVENUE,
)
from pipeline.case_assembly import Case
from pipeline.instantiator import TEMPLATE_LEG_ACCOUNTS, CandidateJournalEntry
from pipeline.predicates import TemplateId
from pipeline.schemas import LedgerEntry, LedgerSource


class PostingDirection(StrEnum):
    """§3.4's global account-direction allowlist, as the three values that table uses."""

    DEBIT_ONLY = "debit only"
    CREDIT_ONLY = "credit only"
    BOTH = "both"


ACCOUNT_DIRECTIONS: dict[str, PostingDirection] = {
    ACCOUNT_SALES_REVENUE.code: PostingDirection.CREDIT_ONLY,
    ACCOUNT_SALES_RETURNS_AND_ALLOWANCES.code: PostingDirection.DEBIT_ONLY,
    ACCOUNT_PAYMENT_GATEWAY_CHARGES.code: PostingDirection.DEBIT_ONLY,
    ACCOUNT_GST_ON_GATEWAY_CHARGES.code: PostingDirection.DEBIT_ONLY,
    ACCOUNT_BANK_ACCOUNT.code: PostingDirection.CREDIT_ONLY,
    ACCOUNT_RAZORPAY_CLEARING.code: PostingDirection.BOTH,
    ACCOUNT_RAZORPAY_SETTLEMENT_ADJUSTMENTS.code: PostingDirection.BOTH,
}
"""§3.4's second validation layer, transcribed row for row.

`Bank Account` is credit-only because in v1 only `T-04` touches it, and
`T-04` credits it — §3.4 states both the rule and that reason. The two
`both`-permitted accounts are the ones §3.4 keeps safe by splitting
family 5 into `T-05` and `T-06`: bidirectional *across* templates, one
fixed direction *within* each, which the per-template layer enforces.
"""

TEMPLATE_ALLOWLIST: frozenset[TemplateId] = frozenset(TEMPLATE_LEG_ACCOUNTS)
"""§1.7.5's "selected template is in the allowlist" — §3.4's six, and nothing else."""


class ValidationCheck(StrEnum):
    """§1.7.5's chain plus §3.4's global layer, in the order §3.4 requires them reported."""

    ACCOUNT_IN_CHART = "account_in_chart_of_accounts"
    ACCOUNT_DIRECTION_PERMITTED = "account_direction_permitted"
    TEMPLATE_ALLOWLISTED = "template_allowlisted"
    TEMPLATE_ACCOUNTS_PERMITTED = "template_accounts_permitted"
    ENTRY_BALANCED = "entry_balanced"
    CITED_RECORDS_EXIST = "cited_records_exist"
    CITED_RECORDS_UNPOSTED = "cited_records_unposted"
    NOT_PREVIOUSLY_POSTED = "not_previously_posted"
    RESIDUAL_ZERO = "post_adjustment_residual_zero"
    """Evaluated in `pipeline.apply`, after the write, inside the transaction that keeps or discards it."""


class CheckResult(BaseModel):
    """One validation's verdict and the reason for it, for §1.8's audit trail."""

    model_config = ConfigDict(frozen=True)

    check: ValidationCheck
    passed: bool
    detail: str = ""


class ValidationReport(BaseModel):
    """Every 1.7.5 check run against one candidate entry."""

    model_config = ConfigDict(frozen=True)

    case_id: str
    template_id: TemplateId
    resolution_id: str
    results: tuple[CheckResult, ...]

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(result for result in self.results if not result.passed)


def resolution_id_for(template_id: TemplateId) -> str:
    """§3.4: "`resolution_id` is unique per `(case_id, template_id)`".

    A function of the template alone, which makes it unique *within* a
    case — the case is named by the `case_id` column beside it, so
    repeating it here would only duplicate the key's other half. Derived
    rather than minted so that reprocessing the same case reconstructs the
    identical id and collides with its own prior posting, which is what
    makes invariant 1.7.4's constraint bite (REV-24).
    """
    return f"res_{template_id.value}"


def _check_accounts_in_chart(candidate: CandidateJournalEntry) -> CheckResult:
    unknown = sorted({leg.account_code for leg in candidate.legs if leg.account_code not in ACCOUNT_BY_CODE})
    return CheckResult(
        check=ValidationCheck.ACCOUNT_IN_CHART,
        passed=not unknown,
        detail="" if not unknown else f"accounts outside §3.2's seven: {unknown}",
    )


def _check_account_directions(candidate: CandidateJournalEntry) -> CheckResult:
    violations: list[str] = []
    for leg in candidate.legs:
        permitted = ACCOUNT_DIRECTIONS.get(leg.account_code)
        if permitted is None:
            continue  # Reported by ACCOUNT_IN_CHART; not double-counted here.
        if leg.debit > 0 and permitted is PostingDirection.CREDIT_ONLY:
            violations.append(f"{leg.account_code} debited but is {permitted.value}")
        if leg.credit > 0 and permitted is PostingDirection.DEBIT_ONLY:
            violations.append(f"{leg.account_code} credited but is {permitted.value}")
    return CheckResult(
        check=ValidationCheck.ACCOUNT_DIRECTION_PERMITTED,
        passed=not violations,
        detail="; ".join(violations),
    )


def _check_template_allowlisted(candidate: CandidateJournalEntry) -> CheckResult:
    allowed = candidate.template_id in TEMPLATE_ALLOWLIST
    return CheckResult(
        check=ValidationCheck.TEMPLATE_ALLOWLISTED,
        passed=allowed,
        detail="" if allowed else f"{candidate.template_id} is not one of §3.4's six templates",
    )


def _check_template_accounts(candidate: CandidateJournalEntry) -> CheckResult:
    allowed = TEMPLATE_LEG_ACCOUNTS.get(candidate.template_id)
    if allowed is None:
        return CheckResult(
            check=ValidationCheck.TEMPLATE_ACCOUNTS_PERMITTED,
            passed=False,
            detail=f"no allowed-account set declared for {candidate.template_id}",
        )
    debit_codes = {account.code for account in allowed[0]}
    credit_codes = {account.code for account in allowed[1]}

    violations: list[str] = []
    for leg in candidate.legs:
        if leg.debit > 0 and leg.account_code not in debit_codes:
            violations.append(f"{leg.account_code} debited, not a {candidate.template_id} debit account")
        if leg.credit > 0 and leg.account_code not in credit_codes:
            violations.append(f"{leg.account_code} credited, not a {candidate.template_id} credit account")
        if leg.debit > 0 and leg.credit > 0:
            violations.append(f"{leg.account_code} carries both a debit and a credit")
    return CheckResult(
        check=ValidationCheck.TEMPLATE_ACCOUNTS_PERMITTED,
        passed=not violations,
        detail="; ".join(violations),
    )


def _check_balanced(candidate: CandidateJournalEntry) -> CheckResult:
    debits = sum(leg.debit for leg in candidate.legs)
    credits = sum(leg.credit for leg in candidate.legs)
    balanced = debits == credits and debits > 0
    return CheckResult(
        check=ValidationCheck.ENTRY_BALANCED,
        passed=balanced,
        detail="" if balanced else f"debits={debits} credits={credits}",
    )


def _check_cited_records_exist(candidate: CandidateJournalEntry, known_record_ids: frozenset[str]) -> CheckResult:
    if not candidate.cited_record_ids:
        return CheckResult(
            check=ValidationCheck.CITED_RECORDS_EXIST,
            passed=False,
            detail="entry cites no source record (§1.7.3 forbids an unsourced auto-action)",
        )
    missing = sorted(set(candidate.cited_record_ids) - known_record_ids)
    return CheckResult(
        check=ValidationCheck.CITED_RECORDS_EXIST,
        passed=not missing,
        detail="" if not missing else f"cited records absent from the batch: {missing}",
    )


def _check_cited_records_unposted(
    candidate: CandidateJournalEntry,
    resolution_id: str,
    adjustments_by_reference: Mapping[str, Sequence[LedgerEntry]],
) -> CheckResult:
    """"all cited source records ... are unposted **for this specific correction**".

    Record-side, and genuinely distinct from `NOT_PREVIOUSLY_POSTED`,
    which is case-side: this one also catches a correction already posted
    against one of these records by a *different* case, which re-running
    the same case would never reveal.
    """
    already: list[str] = []
    for record_id in candidate.cited_record_ids:
        for entry in adjustments_by_reference.get(record_id, ()):
            if entry.resolution_id == resolution_id and entry.case_id != candidate.case_id:
                already.append(f"{record_id} already corrected under {resolution_id} by case {entry.case_id}")
    return CheckResult(
        check=ValidationCheck.CITED_RECORDS_UNPOSTED,
        passed=not already,
        detail="; ".join(already),
    )


def _check_not_previously_posted(
    candidate: CandidateJournalEntry,
    resolution_id: str,
    posted_pairs: frozenset[tuple[str, str]],
) -> CheckResult:
    pair = (candidate.case_id, resolution_id)
    posted = pair in posted_pairs
    return CheckResult(
        check=ValidationCheck.NOT_PREVIOUSLY_POSTED,
        passed=not posted,
        detail="" if not posted else f"{pair} is already posted in the ledger",
    )


def index_controller_adjustments(entries: Sequence[LedgerEntry]) -> dict[str, tuple[LedgerEntry, ...]]:
    """Previously-applied controller adjustments, grouped by the record they reference."""
    grouped: dict[str, list[LedgerEntry]] = {}
    for entry in entries:
        if entry.source is LedgerSource.CONTROLLER_ADJUSTMENT:
            grouped.setdefault(entry.reference, []).append(entry)
    return {reference: tuple(group) for reference, group in grouped.items()}


def posted_resolution_pairs(entries: Sequence[LedgerEntry]) -> frozenset[tuple[str, str]]:
    """Every `(case_id, resolution_id)` already present in the ledger — §1.7.5's idempotency key."""
    return frozenset(
        (entry.case_id, entry.resolution_id)
        for entry in entries
        if entry.case_id is not None and entry.resolution_id is not None
    )


def validate_candidate(
    candidate: CandidateJournalEntry,
    *,
    known_record_ids: frozenset[str],
    adjustments_by_reference: Mapping[str, Sequence[LedgerEntry]],
    posted_pairs: frozenset[tuple[str, str]],
) -> ValidationReport:
    """Run §1.7.5's chain (less the residual) and §3.4's two layers against one candidate."""
    resolution_id = resolution_id_for(candidate.template_id)
    return ValidationReport(
        case_id=candidate.case_id,
        template_id=candidate.template_id,
        resolution_id=resolution_id,
        results=(
            _check_accounts_in_chart(candidate),
            _check_account_directions(candidate),
            _check_template_allowlisted(candidate),
            _check_template_accounts(candidate),
            _check_balanced(candidate),
            _check_cited_records_exist(candidate, known_record_ids),
            _check_cited_records_unposted(candidate, resolution_id, adjustments_by_reference),
            _check_not_previously_posted(candidate, resolution_id, posted_pairs),
        ),
    )


def batch_record_ids(
    cases: Sequence[Case],
    ledger_entries: Sequence[LedgerEntry],
) -> frozenset[str]:
    """Every source-record ID a candidate may legitimately cite (§1.7.5's "exist").

    The three kinds a §3.4 predicate can cite: a `recon_line.entity_id`, a
    `settlement.id`, and a `ledger_entry.journal_entry_id` (`T-04` cites
    the premature bank debit it reclassifies). Bank line IDs are included
    because orphan subtype triggers cite them, though no template does.
    """
    ids: set[str] = {entry.journal_entry_id for entry in ledger_entries}
    for case in cases:
        if case.settlement is not None:
            ids.add(case.settlement.id)
        ids.update(line.entity_id for line in case.recon_lines)
        ids.update(line.line_id for line in case.bank_lines)
    return frozenset(ids)
