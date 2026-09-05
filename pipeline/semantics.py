"""The free-text judgments the pipeline makes about bank and adjustment prose.

## Why this module exists

Six of this pipeline's decision boundaries are questions about English, not
about arithmetic:

| question | asked by | money path |
|---|---|---|
| does this narration name an external counterparty? | `pipeline.classifier` (the classifier's split between a named party and generic wording) | no |
| is this credit the payment gateway paying us? | `pipeline.case_assembly` (the settlement/orphan split) | no |
| does this line say a credit was undone? | `pipeline.case_assembly`, `pipeline.predicates` (the `REVERSAL_UNMATCHED` trigger) | no |
| is this withdrawal a bank charge? | `pipeline.case_assembly` ("charges stay noise") | no |
| is this adjustment a tax position? | `pipeline.policy` (its exclusion of tax positions) | no |
| which settlement does this contested credit pay? | `pipeline.matcher` (tier-2 contention) | **yes** |

Until now each was answered in place by a literal-substring test against a
tuple of keywords, and each of those tuples separated the generator's
corresponding string pool with 100% hit and 0% miss. That is what made the
reference batch score 1.0000 on the deterministic arm at seeds 0, 1, 2, 5, 7
and 11 — a bijection between evidence and label, with no irreducible
ambiguity for judgment to resolve. The pipeline's own import guard against
`generator/` cannot detect it, because a shared *vocabulary* is not an
import, and a seed sweep cannot detect it, because a seed does not vary a
module constant.

`data/heldout_vocab/` is the batch that made it visible: the same 150 cases
and the same ground truth under a second, disjoint surface vocabulary. On it
the keyword answers do not merely degrade — `is_gateway_credit` stops
separating `RZRPAY SOFTWARE PVT LTD` from a merchant, the 125/25 case split
collapses, and `align_ground_truth` raises. See `tools/heldout_vocabulary.py`.

## What this module changes, and what it deliberately does not

The five questions move behind one `NarrationSemantics` interface with two
implementations. **`KeywordSemantics` is the default everywhere and is the
existing logic moved, not rewritten** — same constants, same comparisons,
same order — so the committed reference run stays byte-identical and
determinism holds unchanged. `LlmSemantics` answers the same five questions with a
constrained, cached model call, and falls back to `KeywordSemantics` on a
strict-mode cache miss.

That makes the ablation an experiment about the whole pipeline rather than
about one classifier, and it puts the model on the questions where no
residual computation can decide them, and nowhere else.

**The model may classify and may write prose, but it may never originate an
account, an amount, or a narration on the automated path — and this module is
why that stays true.** Every method here returns a `bool`, a counterparty
*name lifted out of text the bank wrote*, or the id of a settlement the
deterministic cascade had already validated. None returns an account, an
amount, a template, or a posting direction, and nothing downstream can turn
one into any of those.

**Five of the six only route a case; the sixth can misroute money, and is
treated differently.** `resolve_contested_credit` runs only where tier 2 of
the matcher's cascade found more than one settlement claiming one credit — a
contest the deterministic arm resolves by abstaining on all of them. A wrong
answer there does not fabricate a number, but it does book a real credit
against the wrong settlement, which lands as a **false match**, this
pipeline's primary safety metric. That is the point rather than an oversight:
`data/contested/` exists so the question "can judgment be trusted on the
money path, and how much verification does it need first?" is answered with a
measurement instead of an opinion. Its failure direction is always toward
abstention — a cache miss, a malformed answer or an unrecognised id all yield
`None`, which is exactly what the keyword arm returns.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from pipeline.llm_cache import CacheMode, PromptCache
from pipeline.llm_client import LLMClient

__all__ = [
    "NarrationSemantics",
    "ContestedCandidate",
    "KeywordSemantics",
    "LlmSemantics",
    "KEYWORD",
    "GATEWAY_MARKER",
    "BANK_CHARGE_KEYWORDS",
    "REVERSAL_KEYWORDS",
    "TAX_POSITION_MARKERS",
    "BANKING_BOILERPLATE_WORDS",
]


# --- The keyword vocabulary, moved verbatim from its three former homes. ---

GATEWAY_MARKER = "RAZORPAY"
"""Was `pipeline.case_assembly._RAZORPAY_MARKER`. Every string in the
generator's `SETTLEMENT_PARTIES` contains it; nothing in `NAMED_COUNTERPARTIES`
does. That is exactly the coupling this module exists to name."""

BANK_CHARGE_KEYWORDS = ("CHARGE", "FEE")
"""Was `pipeline.case_assembly._BANK_CHARGE_KEYWORDS`."""

REVERSAL_KEYWORDS = ("REVERSAL", "RETURN", "REV-", "RET-")
"""Was `pipeline.case_assembly._REVERSAL_KEYWORDS`. The last two are literal
prefixes of the generator's own `REVERSAL_TEMPLATES`."""

TAX_POSITION_MARKERS = (
    "194-O",
    "194O",
    "194-H",
    "194H",
    "TDS",
    "INPUT TAX CREDIT",
    "ITC",
)
"""Was `pipeline.policy._TAX_POSITION_MARKERS`. Sections 194-O and 194-H and
GST input-tax-credit eligibility on MDR are named directly, so the vocabulary
is domain-real — but it still covers both of the generator's two adjustment
signatures and neither of its five neutral ones, and a batch that writes
`194 O` with a space instead of a hyphen defeats it entirely. That failure
auto-posts twelve tax positions, which `pipeline/policy.py`'s own module
docstring calls the most expensive failure mode in this domain."""

BANKING_BOILERPLATE_WORDS = frozenset(
    {
        "NEFT", "RTGS", "IMPS", "UPI", "ACH", "ECS",
        "CR", "DR", "CREDIT", "DEBIT",
        "TRANSFER", "TRF", "TXN", "PAYMENT", "PMT",
        "INWARD", "OUTWARD", "IN", "OUT", "BY", "TO", "FROM", "VIA",
        "REF", "REFERENCE", "NO", "MISC", "FUNDS", "AMT", "ONLINE",
        "P2A", "P2P", "A2A", "P2M",
    }
)
"""Was `pipeline.classifier._BANKING_BOILERPLATE_WORDS`. Measured: exactly five
of these thirty-four (`NEFT`, `MISC`, `CREDIT`, `FUNDS`, `TRANSFER`) are the
complete token set of the generator's six `OPAQUE_CREDIT_NARRATIONS`, and the
other twenty-nine never match anything in the batch."""

_ALNUM_TOKEN_RE = re.compile(r"[A-Z0-9]+")
_MIN_COUNTERPARTY_TOKEN_LENGTH = 3


class ContestedCandidate(BaseModel):
    """One settlement competing for a contested bank credit (matcher tier 2).

    Carries only what a reader would use to tell the candidates apart, and
    deliberately no amount: every candidate has *the same* amount — that is
    what made them contest in the first place — so an amount could not
    discriminate even if the model were allowed to see it, and it is not:
    the model may classify and may write prose, but it may never originate
    an account, an amount, or a narration on the automated path.
    """

    model_config = ConfigDict(frozen=True)

    settlement_id: str
    payment_methods: tuple[str, ...]
    """The distinct `recon_line.method` values this settlement rolled up, sorted.
    A settlement that is wholly UPI reads differently from one that is wholly
    card, and a bank narration that says so is the only evidence separating
    them."""


@runtime_checkable
class NarrationSemantics(Protocol):
    """The six free-text reads, as one substitutable interface.

    A `Protocol` rather than a base class for the same reason
    `pipeline.llm_client.LLMClient` is one: every test injects a stub, and a
    stub should not have to inherit anything.
    """

    def names_counterparty(self, narration: str) -> str | None:
        """The external counterparty this narration identifies, or `None`.

        `None` means the text names nobody — generic clearing, suspense or
        transfer wording. The payment gateway itself is not a counterparty
        for this purpose: the `UNMATCHED_INBOUND_CREDIT` trigger is about a
        credit with *no Razorpay anchor*, so a line naming the gateway is a
        settlement credit, not an unmatched inbound one.
        """

    def is_gateway_credit(self, narration: str) -> bool:
        """Whether a deposit's narration says the payment gateway sent it."""

    def is_reversal(self, narration: str) -> bool:
        """Whether the narration says a credit was undone (reversed/returned)."""

    def is_bank_charge(self, narration: str) -> bool:
        """Whether a withdrawal is the bank charging its own fee (noise, not a case)."""

    def is_tax_position(self, description: str) -> bool:
        """Whether an adjustment description states a tax position (excluded from auto-close)."""

    def resolve_contested_credit(
        self, narration: str, candidates: Sequence[ContestedCandidate]
    ) -> str | None:
        """Which candidate settlement a contested credit belongs to, or `None`.

        **This is the one read on the money path, and it is opt-in.** The
        matcher's tier 2 keys off an exact amount inside a T+2 window, which
        is not unique to a settlement; when two settlements contest one
        credit, `match_cases` demotes both to tier 3 and the batch abstains.
        This asks whether the narration says which one — the question a
        human resolves in seconds and no deterministic tier rule can express.

        `None` means "the evidence does not say", and it is the right answer
        whenever the narration carries no discriminator. Returning `None` is
        never penalised by a safety metric; returning the wrong settlement is
        a **false match**, this pipeline's primary safety metric, which is
        precisely why this experiment is worth running with that metric
        watching.

        The answer is a *selection among candidates the deterministic cascade
        already validated* — each one already matched on amount and window — so
        the model narrows a set it did not construct. It still cannot
        originate an account, an amount, or a settlement: the model may
        classify and may write prose, but it may never originate an account,
        an amount, or a narration on the automated path.
        """


class KeywordSemantics:
    """The literal-substring implementation — today's behaviour, relocated.

    This is `--semantics keyword`, the default, and the baseline arm for
    every one of the five questions. It is deliberately *not* improved while
    being moved: the point of the ablation is to measure this exact logic
    against a model, and quietly strengthening it here would measure
    something nobody ran before.
    """

    def names_counterparty(self, narration: str) -> str | None:
        """The first alphabetic, non-boilerplate token, or `None`.

        Moved from `pipeline.classifier._identify_counterparty_token`,
        including its reasoning: a reference/UTR fragment is excluded by the
        presence of a digit, because a counterparty's name in this domain is
        never digit-bearing and every reference the generator mints is.
        """
        for token in _ALNUM_TOKEN_RE.findall(narration.upper()):
            if any(char.isdigit() for char in token):
                continue
            if len(token) < _MIN_COUNTERPARTY_TOKEN_LENGTH:
                continue
            if token in BANKING_BOILERPLATE_WORDS:
                continue
            return token
        return None

    def is_gateway_credit(self, narration: str) -> bool:
        return GATEWAY_MARKER in narration.upper()

    def is_reversal(self, narration: str) -> bool:
        upper = narration.upper()
        return any(keyword in upper for keyword in REVERSAL_KEYWORDS)

    def is_bank_charge(self, narration: str) -> bool:
        upper = narration.upper()
        return any(keyword in upper for keyword in BANK_CHARGE_KEYWORDS)

    def is_tax_position(self, description: str) -> bool:
        if not description:
            return False
        upper = description.upper()
        return any(marker in upper for marker in TAX_POSITION_MARKERS)

    def resolve_contested_credit(
        self, narration: str, candidates: Sequence[ContestedCandidate]
    ) -> str | None:
        """Always `None` — and that is the honest baseline, not a stub.

        The matcher defines four tiers and none of them can read a
        payment-method word out of a narration and line it up against a
        settlement's recon-line mix. Writing such a rule here would be
        inventing scope, and it would also
        quietly hand the keyword arm the very capability the ablation is trying
        to measure. So the deterministic answer to "which of these two?" is "the
        evidence available to me does not say", the batch abstains, and
        `false_match_rate` stays 0 by construction.
        """
        return None


KEYWORD: NarrationSemantics = KeywordSemantics()
"""The default every caller uses unless one is passed explicitly.

A module-level singleton because `KeywordSemantics` is stateless, and a
default argument that constructed one per call would make two runs differ by
object identity in a codebase where identity is never the question.
"""


# --- The LLM implementation (Slot A, widened from one question to five). ---

_COUNTERPARTY_SCHEMA: dict = {
    "type": "object",
    "properties": {"counterparty": {"type": ["string", "null"]}},
    "required": ["counterparty"],
    "additionalProperties": False,
}

_BOOLEAN_SCHEMA: dict = {
    "type": "object",
    "properties": {"answer": {"type": "boolean"}},
    "required": ["answer"],
    "additionalProperties": False,
}

_COUNTERPARTY_INSTRUCTIONS = (
    "You read one line of free text from an Indian bank statement.\n"
    "Return the name of the external counterparty the line identifies - the person "
    "or business the money came from or went to.\n"
    "Return null if the line names no counterparty: generic clearing, suspense, "
    "sundry, adjustment or transfer wording that identifies nobody.\n"
    "The payment gateway itself (Razorpay, however the bank abbreviates or re-spaces "
    "it) is NOT a counterparty for this purpose - return null for it.\n"
    "If you return a string it MUST appear verbatim in the line.\n"
    'Answer as {"counterparty": "<name>"} or {"counterparty": null}.\n\nLine: '
)

_GATEWAY_INSTRUCTIONS = (
    "You read one line of free text from an Indian bank statement.\n"
    "Answer true if the line says the money came from the payment gateway Razorpay "
    "(a payment aggregator settling a merchant's collections), however the bank has "
    "abbreviated, truncated or re-spaced the name.\n"
    "Answer false if it names some other party, or names nobody at all.\n"
    '\nAnswer as {"answer": true} or {"answer": false}.\n\nLine: '
)

_REVERSAL_INSTRUCTIONS = (
    "You read one line of free text from an Indian bank statement.\n"
    "Answer true if the line says an earlier credit was undone - reversed, returned, "
    "cancelled, backed out, or sent back. Indian bank statements abbreviate this "
    "heavily: RTN, RET, REV, RVSL and RTRN all mean a return or reversal, and so do "
    "phrases like CR CANCELLED, UNDO and FUNDS SENT BACK.\n"
    "Answer false for an ordinary payment, transfer or charge.\n"
    '\nAnswer as {"answer": true} or {"answer": false}.\n\nLine: '
)
"""Revised once, after measurement, and the revision is disclosed here.

The first version named only the full words. It answered `true` for
`RTN-FT/<ref>/<party>` and `UNDO-<ref>-<party>` but `false` for two
`NEFT RTN <ref> <party>` lines whose shape it had already accepted
elsewhere — 4 of 6 held-out reversal shapes, inconsistently. Naming the
abbreviations is the same move the keyword arm makes with
`REVERSAL_KEYWORDS`, at the level of a concept's vocabulary rather than a
batch's literals, so it is stated in the prompt rather than left to be
guessed. It was written against `data/heldout_vocab/`, a development
artifact — not the held-out seed-2 batch used for evaluation. No threshold
review and no seed-2 measurement informed it.
"""

_CHARGE_INSTRUCTIONS = (
    "You read one line of free text from an Indian bank statement.\n"
    "Answer true if the line is the bank debiting its own fee from the account - "
    "service charges, maintenance or annual recovery, card or cheque-book costs, "
    "minimum-balance penalties.\n"
    "Answer false if it is money moving to or from any other party.\n"
    '\nAnswer as {"answer": true} or {"answer": false}.\n\nLine: '
)

_TAX_INSTRUCTIONS = (
    "You read the description of a settlement adjustment on a merchant's payment "
    "gateway statement.\n"
    "Answer true if it states a TAX POSITION - a withholding or tax deduction at "
    "source, or a review of indirect-tax/GST credit eligibility. These require a "
    "qualified human tax decision.\n"
    "Answer false for an ordinary commercial adjustment: a payout, a reserve or "
    "on-hold movement, a recovery, a ledger correction.\n"
    '\nAnswer as {"answer": true} or {"answer": false}.\n\nDescription: '
)


class LlmSemantics:
    """The same five questions, answered by a constrained, cached model call.

    Mirrors `pipeline.classifier.classify_case_llm`'s contract exactly, for
    the same reasons: the prompt is deterministic, the response space is
    closed by a JSON schema, every answer resolves through the SHA-keyed
    `PromptCache` first, and `CacheMode.STRICT` never constructs a network
    path.

    It differs in one respect, deliberately — **a strict-mode miss falls back
    to `KeywordSemantics` rather than raising.** Slot A's miss could raise
    because the eval path was its only caller; these five run on every bank
    line in a batch, and a miss on one narration must not take down a run.
    Every fallthrough increments `misses`, so a caller reports how much of a
    run was actually model-answered instead of assuming all of it was.

    The counterparty answer is verified before it is trusted: a returned name
    must appear verbatim in the narration it was read from. That check is what
    keeps the one free-text *string* this interface returns from being a place
    the model can invent content — it may only lift text the bank wrote.
    """

    def __init__(
        self,
        cache: PromptCache,
        *,
        mode: CacheMode,
        client: LLMClient | None = None,
        fallback: NarrationSemantics | None = None,
    ) -> None:
        self._cache = cache
        self._mode = mode
        self._client = client
        self._fallback = fallback if fallback is not None else KEYWORD
        self.misses = 0
        """How many questions fell through to `fallback` — reported, never guessed."""

    def _ask(self, instructions: str, text: str, schema: dict) -> dict | None:
        prompt = instructions + text
        raw = self._cache.get(prompt)
        if raw is None:
            if self._mode is CacheMode.STRICT:
                self.misses += 1
                return None
            if self._client is None:
                raise ValueError("CacheMode.REFRESH on a cache miss requires a client")
            raw = self._client.complete(prompt, response_schema=schema)
            self._cache.put(prompt, raw)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            self.misses += 1
            return None

    def _ask_bool(self, instructions: str, text: str, fallback: bool) -> bool:
        payload = self._ask(instructions, text, _BOOLEAN_SCHEMA)
        if payload is None or not isinstance(payload.get("answer"), bool):
            return fallback
        return payload["answer"]

    def names_counterparty(self, narration: str) -> str | None:
        payload = self._ask(_COUNTERPARTY_INSTRUCTIONS, narration, _COUNTERPARTY_SCHEMA)
        if payload is None:
            return self._fallback.names_counterparty(narration)
        name = payload.get("counterparty")
        if name is None:
            return None
        if not isinstance(name, str) or name.upper() not in narration.upper():
            # The one verifiable claim in this interface, and it failed: the
            # model returned a name the bank did not write. Treat it as no
            # answer rather than as evidence.
            self.misses += 1
            return self._fallback.names_counterparty(narration)
        return name

    def is_gateway_credit(self, narration: str) -> bool:
        return self._ask_bool(_GATEWAY_INSTRUCTIONS, narration, self._fallback.is_gateway_credit(narration))

    def is_reversal(self, narration: str) -> bool:
        return self._ask_bool(_REVERSAL_INSTRUCTIONS, narration, self._fallback.is_reversal(narration))

    def is_bank_charge(self, narration: str) -> bool:
        return self._ask_bool(_CHARGE_INSTRUCTIONS, narration, self._fallback.is_bank_charge(narration))

    def is_tax_position(self, description: str) -> bool:
        if not description:
            return False
        return self._ask_bool(_TAX_INSTRUCTIONS, description, self._fallback.is_tax_position(description))

    def resolve_contested_credit(
        self, narration: str, candidates: Sequence[ContestedCandidate]
    ) -> str | None:
        """Resolve a tier-2 contest from the narration, or decline to.

        Three things bound what a wrong answer can do, and all three are
        deliberate given this is the one read that touches the money path:

        1. **Constrained decoding to the candidate ids plus `null`.** The
           returned settlement id is drawn from an enum built out of *this
           call's* candidates, so the model cannot name a settlement that was
           not already validated on amount and window by tier 2.
        2. **The answer is re-checked against that set** before it is returned,
           because a schema is a request rather than a guarantee.
        3. **A miss, a malformed answer, or an unrecognised id all yield
           `None`**, which routes to the same abstention the deterministic arm
           produces. The failure direction is always toward abstaining.

        `None` on a genuinely undecidable contest is the correct answer, not a
        shortfall — `data/contested/` contains both kinds precisely so that
        under-abstention shows up as a false match rather than hiding.
        """
        if len(candidates) < 2:
            return None
        ids = [candidate.settlement_id for candidate in candidates]
        described = "\n".join(
            f"- {candidate.settlement_id}: settles {', '.join(candidate.payment_methods) or 'unknown'} collections"
            for candidate in candidates
        )
        prompt = (
            "An inbound bank credit matched more than one settlement on amount and date, "
            "so the amount cannot say which settlement it pays. Decide from the narration "
            "alone.\n\nBank narration:\n"
            f"{narration}\n\nCandidate settlements:\n{described}\n\n"
            "Return the id of the settlement the narration identifies. Return null if the "
            "narration carries nothing that distinguishes them — null is the correct answer "
            "whenever the text does not actually say, and a wrong id books money against the "
            "wrong settlement.\n"
            'Answer as {"settlement_id": "<id>"} or {"settlement_id": null}.'
        )
        schema = {
            "type": "object",
            "properties": {"settlement_id": {"type": ["string", "null"], "enum": [*ids, None]}},
            "required": ["settlement_id"],
            "additionalProperties": False,
        }
        raw = self._cache.get(prompt)
        if raw is None:
            if self._mode is CacheMode.STRICT:
                self.misses += 1
                return None
            if self._client is None:
                raise ValueError("CacheMode.REFRESH on a cache miss requires a client")
            raw = self._client.complete(prompt, response_schema=schema)
            self._cache.put(prompt, raw)
        try:
            answer = json.loads(raw).get("settlement_id")
        except json.JSONDecodeError:
            self.misses += 1
            return None
        if answer not in ids:
            return None

        # --- The gate. Measured before it was written. ---
        #
        # Asked to choose between a UPI settlement and a card settlement, the
        # model resolves correctly whenever the narration says which — and on
        # `"NEFT CR RAZORPAY SOFTWARE PVT LTD SETTLEMENT"`, which says nothing,
        # it answered `setl_CRD01` anyway. That is a coin flip presented as an
        # answer, and on this read a coin flip books a real credit against the
        # wrong settlement: a false match, this pipeline's primary safety metric.
        #
        # The fix is not a better prompt. It is to stop trusting the answer and
        # start checking the *justification*: the discriminator the model must
        # have used has to be visible in the text it read. A settlement is only
        # allowed to win if one of its own payment methods appears as a word in
        # the narration — the same shape of check as the counterparty read's
        # "the name must be a substring of the line", and unfakeable for the
        # same reason. The model may point at evidence; it may not assert
        # without it.
        #
        # An answer that cannot clear this is not downgraded to a guess, it is
        # discarded: the result is `None`, which is exactly what the keyword arm
        # returns, so an ungrounded resolution costs nothing but the abstention
        # the deterministic path would have produced anyway.
        chosen = next(candidate for candidate in candidates if candidate.settlement_id == answer)
        words = set(_ALNUM_TOKEN_RE.findall(narration.upper()))
        if not any(method.upper() in words for method in chosen.payment_methods):
            return None
        return answer
