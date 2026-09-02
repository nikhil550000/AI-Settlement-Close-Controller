"""The free-text judgments the pipeline makes about bank and adjustment prose.

## Why this module exists

Five of this pipeline's decision boundaries are questions about English, not
about arithmetic:

| question | asked by |
|---|---|
| does this narration name an external counterparty? | `pipeline.classifier` (§4.2's Slot A split) |
| is this credit the payment gateway paying us? | `pipeline.case_assembly` (the settlement/orphan split) |
| does this line say a credit was undone? | `pipeline.case_assembly`, `pipeline.predicates` (§3.3 `REVERSAL_UNMATCHED`) |
| is this withdrawal a bank charge? | `pipeline.case_assembly` (§3.6's "charges stay noise") |
| is this adjustment a tax position? | `pipeline.policy` (§2.5/FR-06's exclusion) |

Until now each was answered in place by a literal-substring test against a
tuple of keywords, and each of those tuples separated the generator's
corresponding string pool with 100% hit and 0% miss. That is what made the
reference batch score 1.0000 on the deterministic arm at seeds 0, 1, 2, 5, 7
and 11 — a bijection between evidence and label, with no irreducible
ambiguity for judgment to resolve. §4.1's import guard cannot detect it,
because a shared *vocabulary* is not an import, and a seed sweep cannot
detect it, because a seed does not vary a module constant.

`data/heldout_vocab/` is the batch that made it visible: the same 150 cases
and the same ground truth under a second, disjoint surface vocabulary. On it
the keyword answers do not merely degrade — `is_gateway_credit` stops
separating `RZRPAY SOFTWARE PVT LTD` from a merchant, the 125/25 case split
collapses, and `align_ground_truth` raises. See `tools/heldout_vocabulary.py`.

## What this module changes, and what it deliberately does not

The five questions move behind one `NarrationSemantics` interface with two
implementations. **`KeywordSemantics` is the default everywhere and is the
existing logic moved, not rewritten** — same constants, same comparisons,
same order — so the committed reference run stays byte-identical and NFR-01
holds unchanged. `LlmSemantics` answers the same five questions with a
constrained, cached model call, and falls back to `KeywordSemantics` on a
strict-mode cache miss.

That makes §5.4's ablation an experiment about the whole pipeline rather than
about one classifier, and it puts the model where §4.2's own reasoning says a
model belongs: on the questions where "no residual computation decides it" is
true, and nowhere else.

**Invariant 1.7.2 is untouched, and this module is why it stays that way.**
Every method here returns a `bool` or a counterparty *name lifted out of text
the bank wrote*. None returns an account, an amount, a template, or a posting
direction, and nothing downstream can turn one into any of those: the answers
feed case assembly's grouping, a §3.3 trigger, and a policy *exclusion* —
three places whose effect is to route a case, never to price one. The model
still cannot originate a number that reaches the ledger. `LlmSemantics` could
return adversarial nonsense on every call without producing a wrong journal
entry; it would produce wrong *routing*, which the §1.6 metric surface
measures and which the §1.7.5 validator chain still gates.
"""

from __future__ import annotations

import json
import re
from typing import Protocol, runtime_checkable

from pipeline.llm_cache import CacheMode, PromptCache
from pipeline.llm_client import LLMClient

__all__ = [
    "NarrationSemantics",
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
"""Was `pipeline.policy._TAX_POSITION_MARKERS`. §2.5 names Sections 194-O and
194-H and GST input-tax-credit eligibility on MDR directly, so the vocabulary
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


@runtime_checkable
class NarrationSemantics(Protocol):
    """The five free-text reads, as one substitutable interface.

    A `Protocol` rather than a base class for the same reason
    `pipeline.llm_client.LLMClient` is one: every test injects a stub, and a
    stub should not have to inherit anything.
    """

    def names_counterparty(self, narration: str) -> str | None:
        """The external counterparty this narration identifies, or `None`.

        `None` means the text names nobody — generic clearing, suspense or
        transfer wording. The payment gateway itself is not a counterparty
        for this purpose: §3.3's `UNMATCHED_INBOUND_CREDIT` is about a credit
        with *no Razorpay anchor*, so a line naming the gateway is a
        settlement credit, not an unmatched inbound one.
        """

    def is_gateway_credit(self, narration: str) -> bool:
        """Whether a deposit's narration says the payment gateway sent it."""

    def is_reversal(self, narration: str) -> bool:
        """Whether the narration says a credit was undone (reversed/returned)."""

    def is_bank_charge(self, narration: str) -> bool:
        """Whether a withdrawal is the bank charging its own fee (§3.6 noise)."""

    def is_tax_position(self, description: str) -> bool:
        """Whether an adjustment description states a tax position (§2.5/FR-06)."""


class KeywordSemantics:
    """The literal-substring implementation — today's behaviour, relocated.

    This is `--semantics keyword`, the default, and §5.4's baseline arm for
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


KEYWORD: NarrationSemantics = KeywordSemantics()
"""The default every caller uses unless one is passed explicitly.

A module-level singleton because `KeywordSemantics` is stateless, and a
default argument that constructed one per call would make two runs differ by
object identity in a codebase where identity is never the question.
"""


# --- The LLM implementation (§4.2's Slot A, widened from one question to five). ---

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
artifact — not §5.1's held-out seed-2 batch. No §5.5 threshold and no
seed-2 measurement informed it.
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
    the same §4.3 reasons: the prompt is deterministic, the response space is
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
