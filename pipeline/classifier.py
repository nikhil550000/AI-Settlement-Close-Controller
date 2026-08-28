"""The classifier, per spec.md §4.1 component 5.

> **Classifier.** Exception class and subtype assignment.

This session (5.1, §6.3) builds the two pieces §4.2 asks for in order —
"build the deterministic keyword baseline first, then Slot A on top of
it" — and stops at the baseline. Slot A itself (the graded LLM call,
constrained decoding, the SHA-keyed cache) is session 5.2's.

## What component 5 actually decides, and what it does not

Six of the seven `OPERATIONAL_EXCEPTION` subtypes already have a
deterministic answer by the time a case reaches this module:
`pipeline/predicates.py`'s `_settlement_anchored_triggers` and
`_orphan_triggers` fire `SETTLEMENT_UTR_MISSING`, `BANK_CREDIT_OVERDUE`,
`SETTLEMENT_AMOUNT_MISMATCH`, `DISPUTE_PENDING`, `REVERSAL_UNMATCHED` and
`DUPLICATE_CREDIT` as *facts*, not labels — component 4's own docstring
is explicit that evaluating is not assigning. Component 5's real job,
the one no arithmetic answers, is the single split §4.2 names outright:

> "`UNMATCHED_INBOUND_CREDIT` versus `AMBIGUOUS_CASE` on an orphan bank
> credit turns entirely on whether the free-text narration identifies a
> counterparty... and no residual computation decides it."

So `classify_case_baseline` below does two things, not one: it *adopts*
a fired trigger where one exists (component 5 assigning what component 4
already found, rather than a second competing detector disagreeing with
the first), and it *decides* the one open question — the narration read
— for cases with no trigger at all. Both are "classification" under
§4.1's job description; only the second is a judgment call.

**What this leaves unresolved, on purpose.** The 17 `REVIEW_REQUIRED`
cases (family-4 date-error, FR-06 tax) are `ACCOUNTING_CORRECTION` /
`OMISSION`-or-`MISPOSTING` in ground truth — subtypes that are not
members of Slot A's eight-value output space at all (§4.2: "the seven
`OPERATIONAL_EXCEPTION` subtypes plus `AMBIGUOUS_CASE`"). No trigger
fires on them and no narration signals a counterparty, so the baseline's
fallthrough assigns `AMBIGUOUS_CASE` — a forced wrong answer for those
17, not a bug: Slot A's vocabulary simply has no correct thing to say
about a policy-excluded correction, and `AMBIGUOUS_CASE` is documented
here as the least-wrong member of a fixed eight-value enum rather than
picked to look right. Phase 6's metric computation is the place that
must decide whether these 17 belong in `exception_subtype_precision`/
`recall`'s denominator at all; this module does not pre-judge that.

## The evidence bundle boundary

Invariant 1.7.2 keeps accounts and amounts out of the model's hands on
the money path; §4.2 draws the same boundary for Slot A ("it never sees
or emits an account, an amount, or a postable narration"). `EvidenceBundle`
is that boundary made concrete: every field is either a fact already
computed by an earlier component (a fired trigger, a match tier, whether
a template already fired) or free text a human would read to make this
exact call (a bank line's own narration) — never a paise figure, an
account code, or the constant ledger narration `pipeline/apply.py` posts.
The same bundle is what session 5.2's Slot A prompt is built from, so the
boundary is enforced once, here, rather than re-drawn per caller.

## The interface question this session does not answer

`pipeline/run.py`'s `KNOWN_GAPS` and BUILDLOG session 4.3's `Next` field
both flag that closing the `UNMATCHED_INBOUND_CREDIT` gap requires
`pipeline/apply.py`'s `assign_state` to read a classification alongside
`CaseEvidence.subtype_triggers`, and leave the timing of that wiring open
— "session 5.1's or 5.2's call". This session calls it: **not yet.**
`classify_batch_baseline` runs downstream of `apply_batch`, over its
`BatchOutcome` (component 5 needs to know which ~70 cases are *not*
`AUTO_CLOSED`, which is component 8's answer, not component 4's) and
produces classifications nobody yet consumes. Wiring the baseline back
into state assignment now would let an unvalidated ablation arm change
what `EXTERNAL_ACTION_REQUIRED` means before Slot A — the arm §5.4 grades
it against — exists to compare it with. Session 5.2 threads the real
classifier (baseline or Slot A) through `apply_batch` once both arms are
buildable and the choice is real rather than one-sided.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from pipeline.apply import CaseOutcome
from pipeline.case_assembly import Case, CaseKind
from pipeline.ground_truth import ExceptionSubtype, OutcomeState
from pipeline.llm_cache import CacheMissError, CacheMode, PromptCache
from pipeline.llm_client import LLMClient
from pipeline.predicates import CaseEvidence
from pipeline.subtype_label import SubtypeLabel

__all__ = [
    "SubtypeLabel",
    "ClassificationSource",
    "EvidenceBundle",
    "ClassificationResult",
    "non_auto_close_case_ids",
    "build_evidence_bundle",
    "build_evidence_bundles",
    "classify_case_baseline",
    "classify_batch_baseline",
    "classification_distribution",
    "SUBTYPE_DEFINITIONS",
    "build_slot_a_prompt",
    "parse_slot_a_response",
    "classify_case_llm",
    "classify_batch_llm",
]

_NON_AUTO_CLOSE_STATES = frozenset(
    {OutcomeState.REVIEW_REQUIRED, OutcomeState.EXTERNAL_ACTION_REQUIRED, OutcomeState.ABSTAINED}
)
"""§4.2's "~70 non-auto-close cases": every terminal state but the two closed-clean
ones, `AUTO_MATCHED` and `AUTO_CLOSED`. 150 - 30 - 50 = 70 exactly, by the §3.6
batch totals, regardless of seed — every population's count is fixed by §3.5/§3.6,
not drawn."""


class ClassificationSource(StrEnum):
    """How a classification was reached — carried in the audit trail (§1.7.3),
    not just the label itself."""

    DETERMINISTIC_TRIGGER = "deterministic_trigger"
    """Adopted from a §3.3 subtype trigger component 4 already fired."""

    KEYWORD_BASELINE = "keyword_baseline"
    """This session's deterministic classifier: the §5.4 ablation arm, and the
    disclosed fallback if Phase 5 falls behind (§6.3)."""

    LLM_SLOT_A = "llm_slot_a"
    """§4.2's graded slot: `pipeline.llm_client.FIREWORKS_MODEL_ID` on Fireworks, under
    constrained decoding, resolved through `pipeline.llm_cache.PromptCache` (§4.3). See
    that constant's docstring for why it is not §4.4's literal `llama-v3p3-70b-instruct`."""


class EvidenceBundle(BaseModel):
    """One case's classification-relevant evidence — the "structured evidence
    bundle" §4.2 describes Slot A as receiving. See this module's docstring
    for the boundary it enforces: facts and free text, never an account, an
    amount, or a postable narration.
    """

    model_config = ConfigDict(frozen=True)

    case_id: str
    case_kind: CaseKind
    fired_subtypes: tuple[ExceptionSubtype, ...]
    """§3.3 subtype triggers component 4 already fired on this case, in evaluator order."""
    has_template_hit: bool
    """Whether any §3.4 template predicate fired — context, not a decision input:
    a case reaching this bundle already failed to `AUTO_CLOSED` regardless."""
    narrations: tuple[str, ...]
    """This case's own `bank_line.narration` text — the only free text Slot A reads,
    and the entire evidence behind the `UNMATCHED_INBOUND_CREDIT` / `AMBIGUOUS_CASE` split."""
    match_tier: int | None
    """The FR-09 tier the matcher resolved at (§4.6), settlement-anchored cases only."""
    in_settlement_window: bool | None
    """Set only for a tier-3 (no-match) settlement-anchored case (§3.3's timing-residual rule)."""


class ClassificationResult(BaseModel):
    """One case's assigned subtype label, plus how the assignment was reached."""

    model_config = ConfigDict(frozen=True)

    case_id: str
    subtype: SubtypeLabel
    source: ClassificationSource
    matched_keyword: str | None = None
    """The narration token that identified a counterparty, when `source` is
    `KEYWORD_BASELINE` and a counterparty was found. `None` otherwise — including
    when the baseline ran and found nothing, which is itself the `AMBIGUOUS_CASE` evidence."""


def non_auto_close_case_ids(outcomes: Sequence[CaseOutcome]) -> set[str]:
    """§4.2's "~70 non-auto-close cases", by `case_id`."""
    return {outcome.case_id for outcome in outcomes if outcome.state in _NON_AUTO_CLOSE_STATES}


def build_evidence_bundle(case: Case, evidence: CaseEvidence) -> EvidenceBundle:
    """One case's `EvidenceBundle`, from component 2/3's `Case` and component 4's `CaseEvidence`."""
    return EvidenceBundle(
        case_id=case.case_id,
        case_kind=case.kind,
        fired_subtypes=tuple(trigger.subtype for trigger in evidence.subtype_triggers),
        has_template_hit=bool(evidence.template_hits),
        narrations=tuple(line.narration for line in case.bank_lines),
        match_tier=case.match_tier,
        in_settlement_window=case.in_settlement_window,
    )


def build_evidence_bundles(
    cases: Sequence[Case],
    evidences: Sequence[CaseEvidence],
    outcomes: Sequence[CaseOutcome],
) -> list[EvidenceBundle]:
    """Every eligible case's bundle, restricted to the ~70 non-auto-close cases (§4.2)."""
    eligible = non_auto_close_case_ids(outcomes)
    evidence_by_case = {evidence.case_id: evidence for evidence in evidences}
    return [
        build_evidence_bundle(case, evidence_by_case.get(case.case_id) or CaseEvidence(case_id=case.case_id))
        for case in cases
        if case.case_id in eligible
    ]


# --- The deterministic keyword baseline. ---

_ALNUM_TOKEN_RE = re.compile(r"[A-Z0-9]+")

_BANKING_BOILERPLATE_WORDS = frozenset(
    {
        "NEFT", "RTGS", "IMPS", "UPI", "ACH", "ECS",
        "CR", "DR", "CREDIT", "DEBIT",
        "TRANSFER", "TRF", "TXN", "PAYMENT", "PMT",
        "INWARD", "OUTWARD", "IN", "OUT", "BY", "TO", "FROM", "VIA",
        "REF", "REFERENCE", "NO", "MISC", "FUNDS", "AMT", "ONLINE",
        "P2A", "P2P", "A2A", "P2M",
    }
)
"""Generic NEFT/RTGS/IMPS bank-statement jargon — real vocabulary any bank
narration uses, not a list reverse-engineered from this batch's templates.
The distinction this baseline draws holds because of what these words mean
(none of them is ever a counterparty's name), the same rule session 3.2 set
for `pipeline/case_assembly.py`'s keyword sets and session 4.3 restated for
`pipeline/policy.py`'s tax-position markers."""

_MIN_COUNTERPARTY_TOKEN_LENGTH = 3
"""Excludes short bank-jargon fragments (e.g. a two-letter code) that slip
past the boilerplate list without being a plausible name token."""


def _identify_counterparty_token(narration: str) -> str | None:
    """The first alphabetic, non-boilerplate token in a narration, or `None`.

    A reference/UTR fragment is excluded by the presence of a digit — a
    counterparty's name in this domain is never digit-bearing, and every
    reference number the generator mints is (§4.6's UTR, `bank_ref_no`).
    This is the entire discriminator §4.2 names: "does the free-text
    narration identify a counterparty."
    """
    for token in _ALNUM_TOKEN_RE.findall(narration.upper()):
        if any(char.isdigit() for char in token):
            continue
        if len(token) < _MIN_COUNTERPARTY_TOKEN_LENGTH:
            continue
        if token in _BANKING_BOILERPLATE_WORDS:
            continue
        return token
    return None


def classify_case_baseline(bundle: EvidenceBundle) -> ClassificationResult:
    """The deterministic keyword baseline (§5.4's ablation arm; §6.3's disclosed fallback).

    Three branches, in order:

    1. **A trigger already fired.** Adopt it — component 5 assigning what
       component 4 already found, not a second detector re-deciding it.
       Where more than one fires (not observed against the reference batch;
       nothing in §3.3 asserts exclusivity the way REV-16 does for
       templates), the first in evaluator order wins, deterministically.
    2. **An untriggered orphan case carrying exactly one bank-line
       narration** — the `UNMATCHED_INBOUND_CREDIT` / `AMBIGUOUS_CASE`
       shape (§3.6: `REVERSAL_UNMATCHED` and `DUPLICATE_CREDIT` orphans
       already fired a trigger in branch 1, so only the plain single-credit
       shape reaches here). The narration keyword read decides it.
    3. **Everything else** falls through to `AMBIGUOUS_CASE` — the least
       -wrong answer in an eight-value space that has no correct one for
       an untriggered, non-orphan case (see this module's docstring).
    """
    if bundle.fired_subtypes:
        return ClassificationResult(
            case_id=bundle.case_id,
            subtype=SubtypeLabel(bundle.fired_subtypes[0].value),
            source=ClassificationSource.DETERMINISTIC_TRIGGER,
        )

    if bundle.case_kind is CaseKind.ORPHAN and len(bundle.narrations) == 1:
        keyword = _identify_counterparty_token(bundle.narrations[0])
        if keyword is not None:
            return ClassificationResult(
                case_id=bundle.case_id,
                subtype=SubtypeLabel.UNMATCHED_INBOUND_CREDIT,
                source=ClassificationSource.KEYWORD_BASELINE,
                matched_keyword=keyword,
            )

    return ClassificationResult(
        case_id=bundle.case_id,
        subtype=SubtypeLabel.AMBIGUOUS_CASE,
        source=ClassificationSource.KEYWORD_BASELINE,
    )


def classify_batch_baseline(bundles: Sequence[EvidenceBundle]) -> list[ClassificationResult]:
    """The baseline over every bundle in a batch. Never raises on well-formed input —
    session 5.1's checkpoint (§6.3): "baseline classifies all ~70 non-auto-close
    cases without crashing.\""""
    return [classify_case_baseline(bundle) for bundle in bundles]


def classification_distribution(results: Sequence[ClassificationResult]) -> dict[str, int]:
    """Count of assigned labels per `SubtypeLabel` — mirrors `pipeline.predicates`'s
    and `pipeline.policy`'s own `*_distribution` helpers."""
    counts: dict[str, int] = {}
    for result in results:
        counts[str(result.subtype)] = counts.get(str(result.subtype), 0) + 1
    return counts


# --- Slot A: the graded LLM classifier (§4.2, §4.3, §4.4, session 5.2). ---

SUBTYPE_DEFINITIONS: dict[SubtypeLabel, str] = {
    SubtypeLabel.SETTLEMENT_UTR_MISSING: (
        "Settlement is processed but carries no UTR, so no bank-side anchor exists."
    ),
    SubtypeLabel.BANK_CREDIT_OVERDUE: "Settlement window has elapsed with no matching bank credit.",
    SubtypeLabel.SETTLEMENT_AMOUNT_MISMATCH: (
        "Settlement header amount does not equal the sum of its recon lines net of fees and tax."
    ),
    SubtypeLabel.UNMATCHED_INBOUND_CREDIT: (
        "Bank credit with an identifiable counterparty but no Razorpay anchor."
    ),
    SubtypeLabel.REVERSAL_UNMATCHED: "Bank reversal with no matching prior credit in the batch.",
    SubtypeLabel.DUPLICATE_CREDIT: "Same UTR credited twice on the bank statement.",
    SubtypeLabel.DISPUTE_PENDING: "A payment carries a dispute/chargeback pending resolution.",
    SubtypeLabel.AMBIGUOUS_CASE: (
        "Evidence is insufficient or internally inconsistent so that no single defensible "
        "treatment exists: several mutually exclusive readings fit the evidence, or a "
        "required piece of evidence is absent. Separating question: do we know what "
        "happened and who must act? If yes, one of the subtypes above. If no, this one."
    ),
}
"""§3.3's subtype table and `AMBIGUOUS_CASE` definition, transcribed verbatim (word for
word, not paraphrased) into the prompt. These sentences are what Slot A is graded
against, so the model sees exactly the same test a human reviewer would apply — not a
looser or tighter restatement of it."""

_SLOT_A_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {"subtype": {"type": "string", "enum": [label.value for label in SubtypeLabel]}},
    "required": ["subtype"],
    "additionalProperties": False,
}
"""§4.3 layer 1: constrained decoding to the eight-value enum. Passed as Fireworks'
`response_format` JSON schema (§4.4) — the output space is eight values even under
provider-side nondeterminism, which is what bounds it rather than leaving it open."""

_SLOT_A_INSTRUCTIONS = (
    "You are classifying one reconciliation case for a settlement-accounting system. "
    "The case has already failed to auto-close: no deterministic accounting correction "
    "applies. Your only job is to assign the single subtype that best matches the "
    "evidence below, from this fixed list:\n\n"
    + "\n".join(f"- {label.value}: {SUBTYPE_DEFINITIONS[label]}" for label in SubtypeLabel)
    + "\n\nYou never see or report an account code, a paise amount, or a ledger "
    "narration — only the evidence fields given. Respond with JSON matching the given "
    "schema: {\"subtype\": <one of the values above>}."
)
"""Fixed across every case — only the evidence blob varies — so the SHA-256 cache key
in `build_slot_a_prompt` is driven entirely by case-specific evidence, and a prompt
wording change (a deliberate edit, not a per-case accident) is what invalidates the
whole cache at once."""


def build_slot_a_prompt(bundle: EvidenceBundle) -> str:
    """The exact, deterministic prompt string Slot A sends — and the exact string
    `pipeline.llm_cache.cache_key` hashes. Two runs over the same batch produce the
    same `EvidenceBundle` for a given case (§3.5/§3.6's populations are fixed by seed,
    not redrawn), so the same case always hashes to the same cache entry.

    Never includes `bundle.case_id`: two cases with identical evidence should read as
    one cache entry, not two, and the boundary this bundle enforces (§4.2: "never sees
    or emits an account, an amount, or a postable narration") extends to the prompt —
    an internal case ID is bookkeeping, not evidence, and mixing it into the hashed
    text would fragment the cache for no classification-relevant reason.
    """
    evidence = {
        "case_kind": bundle.case_kind.value,
        "fired_subtypes": [subtype.value for subtype in bundle.fired_subtypes],
        "has_template_hit": bundle.has_template_hit,
        "narrations": list(bundle.narrations),
        "match_tier": bundle.match_tier,
        "in_settlement_window": bundle.in_settlement_window,
    }
    return (
        _SLOT_A_INSTRUCTIONS
        + "\n\nEvidence:\n"
        + json.dumps(evidence, sort_keys=True, ensure_ascii=True)
    )


class SlotAResponseError(RuntimeError):
    """A Slot A response did not parse to a valid `SubtypeLabel`.

    Should not happen under constrained decoding (`_SLOT_A_JSON_SCHEMA` fixes the
    output to exactly these eight values) — this exists for the cache path, where a
    committed entry could in principle be hand-edited or come from a differently
    configured client, and a bad cached string should fail loudly rather than crash
    on an `AttributeError` three frames away.
    """


def parse_slot_a_response(raw: str) -> SubtypeLabel:
    """Parse one raw Fireworks (or cached) response string into a `SubtypeLabel`."""
    try:
        payload = json.loads(raw)
        return SubtypeLabel(payload["subtype"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise SlotAResponseError(f"could not parse a SubtypeLabel from Slot A response: {raw!r}") from exc


def classify_case_llm(
    bundle: EvidenceBundle,
    cache: PromptCache,
    *,
    mode: CacheMode,
    client: LLMClient | None = None,
) -> ClassificationResult:
    """One case through Slot A: cache lookup, then (only under `CacheMode.REFRESH`) a
    real Fireworks call on a miss. `CacheMode.STRICT` never constructs a network path —
    a miss is `CacheMissError`, per §4.3's "hard error rather than a fallthrough to the
    API" — so a `client` is not even required when every case is already cached.
    """
    prompt = build_slot_a_prompt(bundle)
    raw = cache.get(prompt)
    if raw is None:
        if mode is CacheMode.STRICT:
            raise CacheMissError(
                f"no cached Slot A response for case {bundle.case_id!r}; "
                "run with --llm-cache=refresh to populate it"
            )
        if client is None:
            raise ValueError("CacheMode.REFRESH on a cache miss requires a client")
        raw = client.complete(prompt, response_schema=_SLOT_A_JSON_SCHEMA)
        cache.put(prompt, raw)
    return ClassificationResult(
        case_id=bundle.case_id,
        subtype=parse_slot_a_response(raw),
        source=ClassificationSource.LLM_SLOT_A,
    )


def classify_batch_llm(
    bundles: Sequence[EvidenceBundle],
    cache: PromptCache,
    *,
    mode: CacheMode,
    client: LLMClient | None = None,
) -> list[ClassificationResult]:
    """Slot A over a whole batch. See `classify_case_llm` — the same cache/mode/client
    contract applies per case."""
    return [classify_case_llm(bundle, cache, mode=mode, client=client) for bundle in bundles]
