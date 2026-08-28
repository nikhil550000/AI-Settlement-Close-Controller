"""Slot B, per spec.md §4.2 and session 5.3 (§6.3).

> **Slot B — resolution text, abstention rationale, per-case reasoning
> prose. LLM. Ungraded, off the money path.**
> `EXTERNAL_ACTION_REQUIRED` requires a recommended external action in
> readable English (§1.3); `ABSTAINED` requires a rationale. Both are
> language tasks over facts the deterministic path has already fixed.
> The FR-11 report MUST label every Slot B string as model-generated
> prose over deterministic facts, so narration can never be mistaken for
> evidence in the audit trail.

Unlike Slot A (component 5, `pipeline/classifier.py`), Slot B changes no
outcome state and posts nothing — §4.2 calls it "ungraded, off the money
path", and no §1.6 metric grades it. It is not a numbered §4.1 component
of its own; its output is raw material for component 9's (Reporter)
"Exception report" artifact (§1.8 item 3), still unbuilt (Phase 6). This
session's job, per §6.3's row for 5.3, is narrower than that report: the
text itself, generated deterministically-prompted and cached exactly like
Slot A, plus a schema shape that carries FR-11's model-generated label as
data rather than as a convention the eventual report author has to
remember.

**Reuses Slot A's infrastructure rather than duplicating it**, per
session 5.2's own "Next" field: `pipeline.llm_client.LLMClient`/
`FireworksClient` and `pipeline.llm_cache.PromptCache`/`CacheMode` are
backend-agnostic already. The same committed `data/llm_cache.json` holds
both slots' entries side by side — the cache keys on the exact prompt
string (SHA-256), and Slot A's and Slot B's prompts are never
byte-identical, so nothing about one slot's entries can collide with or
overwrite the other's.

**Not wired into `pipeline.run.run_batch`.** Slot A had to be threaded
through `apply_batch` because one of its eight labels
(`UNMATCHED_INBOUND_CREDIT`) changes `assign_state`'s terminal-state
routing. Slot B changes nothing — it runs strictly after a batch's
terminal states are already fixed, over whichever cases landed in
`EXTERNAL_ACTION_REQUIRED` or `ABSTAINED`. `build_narration_bundles`
takes a finished `RunResult`'s `cases` and `outcome.outcomes` directly;
there is no second `apply_batch` pass to make correct, because there is
nothing here for a second pass to change.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from pipeline.apply import CaseOutcome
from pipeline.case_assembly import Case, CaseKind
from pipeline.classifier import SUBTYPE_DEFINITIONS
from pipeline.ground_truth import ExceptionSubtype, OutcomeState
from pipeline.llm_cache import CacheMissError, CacheMode, PromptCache
from pipeline.llm_client import LLMClient
from pipeline.subtype_label import SubtypeLabel

__all__ = [
    "NarrationKind",
    "NarrationBundle",
    "CaseNarration",
    "SlotBResponseError",
    "build_narration_bundle",
    "build_narration_bundles",
    "build_slot_b_prompt",
    "parse_slot_b_response",
    "narrate_case_llm",
    "narrate_batch_llm",
]

class NarrationKind(StrEnum):
    """§4.2's two Slot B text kinds — one per eligible terminal state, never both
    on one case, since a case has exactly one terminal state (§1.3)."""

    RECOMMENDED_ACTION = "recommended_action"
    """`EXTERNAL_ACTION_REQUIRED`: "a recommended external action in readable English.\""""

    ABSTENTION_RATIONALE = "abstention_rationale"
    """`ABSTAINED`: a rationale for why no defensible candidate can be recommended."""


_NARRATION_STATES: dict[OutcomeState, NarrationKind] = {
    OutcomeState.EXTERNAL_ACTION_REQUIRED: NarrationKind.RECOMMENDED_ACTION,
    OutcomeState.ABSTAINED: NarrationKind.ABSTENTION_RATIONALE,
}
"""The only two terminal states §4.2's Slot B paragraph names. A module-level
constant rather than an inline literal so `build_narration_bundle`'s filter and
its state-to-kind mapping cannot drift apart."""


class NarrationBundle(BaseModel):
    """One case's Slot-B-relevant evidence — deliberately the same shape of thing
    `pipeline.classifier.EvidenceBundle` is for Slot A: facts the deterministic path
    has already fixed, never an account, an amount, or a posted ledger narration.
    §4.2 draws that boundary for Slot A explicitly; nothing in §4.2's Slot B
    paragraph asks for money or accounts either — a recommended action or an
    abstention rationale is prose about *why*, not about *how much* — so the same
    boundary is kept here as the narrower, defensible reading rather than invented
    scope in the other direction.
    """

    model_config = ConfigDict(frozen=True)

    case_id: str
    kind: NarrationKind
    case_kind: CaseKind
    classified_subtype: SubtypeLabel | None
    """Component 5's label for this case, when a classifier ran (§6.3 session 5.2).
    `None` when `run_batch` was called with no classifier — Slot B still has a
    narration to write in that case (the triggered subtypes and narrations below
    are enough), just without the classifier's own answer to ground it in."""
    triggered_subtypes: tuple[ExceptionSubtype, ...]
    """§3.3 subtype triggers component 4 fired on this case (`CaseOutcome.triggered_subtypes`)."""
    narrations: tuple[str, ...]
    """This case's own `bank_line.narration` text, exactly as `EvidenceBundle` carries it."""
    match_tier: int | None
    in_settlement_window: bool | None


class CaseNarration(BaseModel):
    """One case's Slot B text, plus the FR-11 labelling obligation carried as data.

    > The FR-11 report MUST label every Slot B string as model-generated
    > prose over deterministic facts.

    There is no FR-11 report yet (Phase 6, session 6.3) for this label to be
    rendered into, but the obligation is a schema/data one regardless of
    whether anything renders it today: `model_generated` travels with every
    string this module produces, fixed `True` by construction (v1 has no
    deterministic fallback for Slot B — unlike Slot A, §4.2 names no ablation
    baseline for it), so a future reporter reads the flag off the record
    rather than needing to know out-of-band that every string here is prose,
    not evidence.
    """

    model_config = ConfigDict(frozen=True)

    case_id: str
    kind: NarrationKind
    text: str
    model_generated: Literal[True] = True


def build_narration_bundle(case: Case, outcome: CaseOutcome) -> NarrationBundle | None:
    """One case's `NarrationBundle`, or `None` if its terminal state needs no Slot B text."""
    kind = _NARRATION_STATES.get(outcome.state)
    if kind is None:
        return None
    return NarrationBundle(
        case_id=case.case_id,
        kind=kind,
        case_kind=case.kind,
        classified_subtype=outcome.classified_subtype,
        triggered_subtypes=outcome.triggered_subtypes,
        narrations=tuple(line.narration for line in case.bank_lines),
        match_tier=case.match_tier,
        in_settlement_window=case.in_settlement_window,
    )


def build_narration_bundles(cases: Sequence[Case], outcomes: Sequence[CaseOutcome]) -> list[NarrationBundle]:
    """Every eligible case's bundle, restricted to `EXTERNAL_ACTION_REQUIRED` and
    `ABSTAINED` — the two states §4.2's Slot B paragraph names. Takes a finished
    `pipeline.run.RunResult`'s own `cases`/`outcome.outcomes` (see this module's
    docstring for why there is no second `apply_batch` pass here)."""
    outcome_by_case = {outcome.case_id: outcome for outcome in outcomes}
    bundles: list[NarrationBundle] = []
    for case in cases:
        outcome = outcome_by_case.get(case.case_id)
        if outcome is None:
            continue
        bundle = build_narration_bundle(case, outcome)
        if bundle is not None:
            bundles.append(bundle)
    return bundles


# --- The Slot B prompt and response. ---

_SLOT_B_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}
"""Free text, not an enum — Slot B has no fixed vocabulary the way Slot A's eight
subtypes do. The schema still constrains the *shape* of the response (one string
field) so parsing is as mechanical as Slot A's, even though the content is prose."""

_SLOT_B_INSTRUCTIONS: dict[NarrationKind, str] = {
    NarrationKind.RECOMMENDED_ACTION: (
        "You are writing a short, plain-English recommended next step for a "
        "settlement-accounting reconciliation case. The deterministic pipeline has "
        "already determined this case cannot be closed automatically and requires "
        "action outside the system's authority (e.g., raising a support ticket with "
        "Razorpay for a missing settlement UTR, contacting the acquiring bank about "
        "a delayed credit, following up on a pending chargeback outcome). Do not "
        "invent new facts, account names, or amounts — only recommend, in one or two "
        "sentences, the external action a human reviewer should take next, grounded "
        "in the evidence below."
    ),
    NarrationKind.ABSTENTION_RATIONALE: (
        "You are writing a short, plain-English rationale explaining why a "
        "settlement-accounting reconciliation case could not be automatically "
        "resolved. The deterministic pipeline has already determined the evidence is "
        "insufficient or internally inconsistent: several mutually exclusive "
        "readings could fit it, or a required piece of evidence is absent. Do not "
        "invent new facts, account names, or amounts — only explain, in one or two "
        "sentences, why the evidence below does not support a single defensible "
        "treatment, so a human reviewer knows what to look into."
    ),
}
"""Fixed per `NarrationKind`, mirroring `pipeline.classifier._SLOT_A_INSTRUCTIONS`:
only the evidence blob varies per case, so the SHA-256 cache key is driven by
case-specific evidence and a wording change is what invalidates a whole kind's
cache entries at once, not a per-case accident."""

_RESPONSE_SCHEMA_HINT = '\n\nRespond with JSON matching the given schema: {"text": <one or two sentences>}.'


def build_slot_b_prompt(bundle: NarrationBundle) -> str:
    """The exact, deterministic prompt string Slot B sends for one bundle — and the
    exact string `pipeline.llm_cache.cache_key` hashes. Mirrors
    `pipeline.classifier.build_slot_a_prompt`: never includes `bundle.case_id`, so two
    cases with identical evidence and the same `kind` share one cache entry rather
    than paying for the same answer twice.
    """
    instructions = _SLOT_B_INSTRUCTIONS[bundle.kind] + _RESPONSE_SCHEMA_HINT
    if bundle.classified_subtype is not None:
        definition = SUBTYPE_DEFINITIONS.get(bundle.classified_subtype)
        if definition is not None:
            instructions += (
                f"\n\nThis case has been classified as {bundle.classified_subtype.value}: {definition}"
            )
    evidence = {
        "case_kind": bundle.case_kind.value,
        "classified_subtype": bundle.classified_subtype.value if bundle.classified_subtype else None,
        "triggered_subtypes": [subtype.value for subtype in bundle.triggered_subtypes],
        "narrations": list(bundle.narrations),
        "match_tier": bundle.match_tier,
        "in_settlement_window": bundle.in_settlement_window,
    }
    return instructions + "\n\nEvidence:\n" + json.dumps(evidence, sort_keys=True, ensure_ascii=True)


class SlotBResponseError(RuntimeError):
    """A Slot B response did not parse to a non-empty string. Mirrors
    `pipeline.classifier.SlotAResponseError` — should not happen under the
    `_SLOT_B_JSON_SCHEMA` constraint, but a hand-edited or misconfigured cached
    entry should fail loudly here rather than surface an empty string as prose."""


def parse_slot_b_response(raw: str) -> str:
    """Parse one raw Fireworks (or cached) response string into Slot B's text."""
    try:
        payload = json.loads(raw)
        text = payload["text"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise SlotBResponseError(f"could not parse Slot B text from response: {raw!r}") from exc
    if not isinstance(text, str) or not text.strip():
        raise SlotBResponseError(f"Slot B response text is empty or not a string: {raw!r}")
    return text.strip()


def narrate_case_llm(
    bundle: NarrationBundle,
    cache: PromptCache,
    *,
    mode: CacheMode,
    client: LLMClient | None = None,
) -> CaseNarration:
    """One case through Slot B: cache lookup, then (only under `CacheMode.REFRESH`) a
    real call on a miss. Mirrors `pipeline.classifier.classify_case_llm`'s contract
    exactly — `CacheMode.STRICT` never constructs a network path; a miss is
    `CacheMissError`, never a fallthrough to the API (§4.3).
    """
    prompt = build_slot_b_prompt(bundle)
    raw = cache.get(prompt)
    if raw is None:
        if mode is CacheMode.STRICT:
            raise CacheMissError(
                f"no cached Slot B response for case {bundle.case_id!r}; "
                "run with --llm-cache=refresh to populate it"
            )
        if client is None:
            raise ValueError("CacheMode.REFRESH on a cache miss requires a client")
        raw = client.complete(prompt, response_schema=_SLOT_B_JSON_SCHEMA)
        cache.put(prompt, raw)
    return CaseNarration(
        case_id=bundle.case_id,
        kind=bundle.kind,
        text=parse_slot_b_response(raw),
    )


def narrate_batch_llm(
    bundles: Sequence[NarrationBundle],
    cache: PromptCache,
    *,
    mode: CacheMode,
    client: LLMClient | None = None,
) -> list[CaseNarration]:
    """Slot B over a whole batch. See `narrate_case_llm` — the same cache/mode/client
    contract applies per case."""
    return [narrate_case_llm(bundle, cache, mode=mode, client=client) for bundle in bundles]
