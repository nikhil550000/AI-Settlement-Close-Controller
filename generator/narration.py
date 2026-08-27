"""The one shared narration pool, per spec.md §3.5's fingerprint control.

> **Fingerprint control.** The failure mode that matters is anomalous cases
> becoming identifiable by *artifact* rather than by *evidence* —
> sequential IDs assigned per scenario, timestamps generated in scenario
> blocks, **narration strings unique to one anomaly type**. Any of these
> silently inflates every metric in 1.6. Mitigation: [...] draw narration
> text from one shared pool regardless of scenario.

Every free-text string the generator writes comes from this module, and
no function here takes a scenario, a family, a template id, or an
exception subtype as an argument. That is the whole design rule: a pool
that cannot see the scenario cannot leak it. Session 2.2's narrations did
the opposite — `"fee/GST not recognized"`, `"booked at net settlement
credit"`, `"bank debited prematurely at capture"` each named its own
anomaly, and the posted amount appeared in the text as a second copy of
the evidence.

**Three strings stay scenario-bearing, and each is evidence the spec
requires, not artifact:**

- the two FR-06 tax signatures (`TAX_SIGNATURES`), because §4.2 fixes them
  as the detection surface — "a 194-O deduction has a signature in the
  adjustment line";
- an opaque credit narration versus one naming a counterparty, because
  §3.6 splits sixteen orphan cases on exactly that line;
- a bank-charge narration, because §3.6's noise lines exist to exercise
  the matcher's ignore path.

Everything else — ledger narrations, settlement-credit narrations,
reversal narrations, counterparty names, adjustment descriptions — is
drawn from a pool shared across every population that emits that kind of
record.

## UTR narration variety (§4.6)

> **Generator obligation.** The cascade is only tested if the narration
> varies. The generator MUST produce roughly 50% clean UTR, 25% embedded,
> 15% truncated, 10% absent.

`UtrShape` is that split's vocabulary, and the four shapes are defined
here as *text* shapes so §4.6's matcher (session 3.3) has a contract to
implement against rather than a corpus to guess at:

| Shape | The UTR in the narration |
|---|---|
| `CLEAN` | present in full, delimited by whitespace on both sides |
| `EMBEDDED` | present in full, delimited by punctuation inside a longer run |
| `TRUNCATED` | a contiguous prefix, at least `TRUNCATED_MIN_LENGTH` characters |
| `ABSENT` | not present in any form |

`CLEAN` is reachable by §4.6 tier 0 under either reading of "appears as a
token"; `TRUNCATED` is reachable only at tier 1; `ABSENT` only at tier 2,
on amount and date window. `EMBEDDED` sits between tier 0 and tier 1
depending on how the matcher tokenizes, which is session 3.3's call to
make — the generator's obligation is the variety, not the tier.
"""

from __future__ import annotations

import random
import re
from enum import StrEnum

UTR_LENGTH = 16
"""Characters in a generated UTR — the length of a real NEFT/RTGS UTR.

Session 2.2 minted 9-character UTRs, which left §4.6 tier 1's "contiguous
prefix of length >= 8" with exactly one truncation length to work with,
making the truncated population a single degenerate case rather than a
range. Sixteen characters is both the realistic figure and enough room for
`TRUNCATED_MIN_LENGTH`..15 to be a real spread.
"""

TRUNCATED_MIN_LENGTH = 10
"""Shortest UTR prefix the generator will write into a narration.

§4.6 tier 1 accepts a prefix of length >= 8. The generator holds itself to
10 so that two settlements can never share a written prefix by accident:
at 8 characters a collision across ~100 settlements is remote but real,
and a collision would put two settlements in the same tier-1 candidate set
and silently corrupt a match the eval then grades.
"""

PAYMENT_METHODS = ("card", "upi", "netbanking", "wallet")

_UTR_BANK_CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_UTR_BODY_ALPHABET = "0123456789"

# --- Parties. Settlement credits and orphan/noise credits draw from
# separate pools because the payer genuinely differs; within each pool no
# name belongs to one scenario. ---

SETTLEMENT_PARTIES = (
    "RAZORPAY SOFTWARE PVT LTD",
    "RAZORPAY SOFTWARE PRIVATE LIMITED",
    "RAZORPAY SOFTWARE PVT LTD MUMBAI",
    "RAZORPAY SOFTWARE",
)

NAMED_COUNTERPARTIES = (
    "SHARMA ENTERPRISES",
    "BLUE OCEAN TRADERS",
    "APEX LOGISTICS PVT LTD",
    "RAVI KUMAR",
    "GREENFIELD EXPORTS",
    "SUNRISE TEXTILES",
    "NATIONAL HARDWARE CO",
    "PRIYA MENON",
)

# --- Ledger narrations. One pool for every ledger entry the generator
# writes, whatever the posting variant, account, or family. The entity
# reference is deliberately *not* interpolated: `ledger_entry.reference`
# already carries it as a field, and a second copy inside free text would
# have to be kept in step through the global ID pass for no gain. ---

_LEDGER_NARRATION_TEMPLATES = (
    "Razorpay {method} collection",
    "Razorpay settlement posting ({method})",
    "ERP import - Razorpay {method}",
    "Gateway posting - {method}",
    "Razorpay {method} txn",
)

# --- Adjustment descriptions. `recon_line.description` on an adjustment
# row is the FR-06 detection surface (§4.2), so every adjustment carries
# one: if only the tax positions had a description, its mere presence
# would be the tell rather than its content. ---

TAX_SIGNATURES = (
    "TDS deduction under Section 194-O (e-commerce operator)",
    "GST input tax credit eligibility review — MDR component",
)
"""§2.5/FR-06's two policy exclusions, as they appear in the adjustment line.

Scenario-bearing by requirement, not by accident: §4.2 states that "a
194-O deduction has a signature in the adjustment line" and anticipates a
predicate reading it. These are the only two narration strings in the
generator that identify the population that produced them.
"""

_NEUTRAL_ADJUSTMENT_DESCRIPTIONS = (
    "Settlement adjustment",
    "On-hold balance release",
    "Reserve balance adjustment",
    "Merchant account adjustment",
    "Settlement recovery adjustment",
)

# --- Bank narrations. `{ref}` is the UTR token (full, truncated, or
# dropped with the template's surrounding punctuation). ---

_CLEAN_CREDIT_TEMPLATES = (
    "NEFT CR {party} {ref}",
    "RTGS CR {party} REF {ref}",
    "IMPS IN {party} {ref}",
    "BY NEFT {party} {ref}",
    "NEFT INWARD {party} {ref}",
)

_EMBEDDED_CREDIT_TEMPLATES = (
    "NEFT-CR-{party}-{ref}",
    "NEFT/{ref}/{party}",
    "RTGS-{ref}-{party}-CR",
    "IMPS/P2A/{ref}/{party}",
    "BY TRANSFER-NEFT*{ref}*{party}",
)

_ABSENT_CREDIT_TEMPLATES = (
    "NEFT CR {party}",
    "RTGS CR {party}",
    "IMPS IN {party}",
    "BY NEFT {party}",
    "NEFT INWARD {party}",
)

OPAQUE_CREDIT_NARRATIONS = (
    "NEFT CR",
    "MISC CREDIT",
    "FUNDS TRANSFER",
    "TRANSFER IN",
    "BY TRANSFER",
    "CREDIT-MISC",
)
"""§3.6's "inbound credit, opaque narration" — no identifiable counterparty.

Scenario-bearing by requirement: §4.2 states the `UNMATCHED_INBOUND_CREDIT`
versus `AMBIGUOUS_CASE` split "turns entirely on whether the free-text
narration identifies a counterparty."
"""

_DEBIT_TEMPLATES = (
    "NEFT DR {party} {ref}",
    "RTGS DR {party} REF {ref}",
    "IMPS OUT {party} {ref}",
    "NEFT-DR-{party}-{ref}",
    "TO TRANSFER-NEFT*{ref}*{party}",
)

REVERSAL_TEMPLATES = (
    "REVERSAL-{ref}",
    "NEFT RETURN {ref} {party}",
    "RET-NEFT/{ref}/{party}",
    "REV-{ref}-{party}",
    "NEFT REVERSAL {ref}",
)

_BANK_CHARGE_NARRATIONS = (
    "SMS ALERT CHARGES",
    "AMC FEE",
    "CHEQUE BOOK CHARGES",
    "ATM AMC CHARGES",
    "DEBIT CARD ANNUAL FEE",
)
"""§3.6: "Bank charges stay noise, not cases" — these exist to be ignored."""


class UtrShape(StrEnum):
    """How a UTR appears in a `bank_line.narration` (§4.6's generator obligation)."""

    CLEAN = "clean"
    EMBEDDED = "embedded"
    TRUNCATED = "truncated"
    ABSENT = "absent"


UTR_SHAPE_TARGET_SHARE = {
    UtrShape.CLEAN: 50,
    UtrShape.EMBEDDED: 25,
    UtrShape.TRUNCATED: 15,
    UtrShape.ABSENT: 10,
}
"""§4.6, verbatim: "roughly 50% clean UTR, 25% embedded, 15% truncated, 10% absent"."""


def random_utr(rng: random.Random) -> str:
    """A settlement UTR: four uppercase letters (bank code shape) then twelve digits."""
    code = "".join(rng.choice(_UTR_BANK_CODE_ALPHABET) for _ in range(4))
    body = "".join(rng.choice(_UTR_BODY_ALPHABET) for _ in range(UTR_LENGTH - 4))
    return code + body


def bank_reference_token(rng: random.Random) -> str:
    """A bank's own reference number for the `bank_ref_no` column — never a UTR."""
    return f"N{rng.randrange(10**11, 10**12)}"


def ledger_narration(rng: random.Random, *, method: str) -> str:
    """Narration for any ledger entry the generator writes, whatever family produced it."""
    return rng.choice(_LEDGER_NARRATION_TEMPLATES).format(method=method.upper())


def random_payment_method(rng: random.Random) -> str:
    return rng.choice(PAYMENT_METHODS)


def neutral_adjustment_description(rng: random.Random) -> str:
    """A non-tax adjustment description, so that FR-06 is detectable by content and not by presence."""
    return rng.choice(_NEUTRAL_ADJUSTMENT_DESCRIPTIONS)


def utr_token(rng: random.Random, utr: str, shape: UtrShape) -> str | None:
    """The UTR text a narration of `shape` carries, or `None` when the shape drops it."""
    if shape is UtrShape.ABSENT or not utr:
        return None
    if shape is UtrShape.TRUNCATED:
        if len(utr) <= TRUNCATED_MIN_LENGTH:
            raise ValueError(f"UTR {utr!r} is too short to truncate to {TRUNCATED_MIN_LENGTH}+ characters")
        return utr[: rng.randint(TRUNCATED_MIN_LENGTH, len(utr) - 1)]
    return utr


def credit_narration(rng: random.Random, *, party: str, utr: str, shape: UtrShape) -> str:
    """An inbound-credit narration carrying `utr` in the given shape.

    Used for every credit that names a payer — settlement credits and
    orphan/noise inbound credits alike — so the sentence shapes are shared
    and only the party and the UTR shape differ.
    """
    token = utr_token(rng, utr, shape)
    if token is None:
        return rng.choice(_ABSENT_CREDIT_TEMPLATES).format(party=party)
    templates = _CLEAN_CREDIT_TEMPLATES if shape is UtrShape.CLEAN else _EMBEDDED_CREDIT_TEMPLATES
    if shape is UtrShape.TRUNCATED:
        # A truncated UTR is equally at home in either sentence shape; drawing
        # across both keeps template choice from encoding the shape.
        templates = _CLEAN_CREDIT_TEMPLATES + _EMBEDDED_CREDIT_TEMPLATES
    return rng.choice(templates).format(party=party, ref=token)


def opaque_credit_narration(rng: random.Random) -> str:
    """An inbound credit naming no counterparty (§3.6's `AMBIGUOUS_CASE` orphan population)."""
    return rng.choice(OPAQUE_CREDIT_NARRATIONS)


def debit_narration(rng: random.Random, *, party: str, reference: str) -> str:
    """An outbound transfer narration (noise: money leaving, unrelated to any settlement)."""
    return rng.choice(_DEBIT_TEMPLATES).format(party=party, ref=reference)


def reversal_narration(rng: random.Random, *, party: str, reference: str) -> str:
    """A reversal narration, shared by §3.6's `REVERSAL_UNMATCHED` cases and the self-matching noise pairs.

    One pool for both: the two are separated by whether a matching prior
    credit exists in the batch, which is evidence, and must not also be
    separable by how the line reads.
    """
    return rng.choice(REVERSAL_TEMPLATES).format(party=party, ref=reference)


def bank_charge_narration(rng: random.Random) -> str:
    return rng.choice(_BANK_CHARGE_NARRATIONS)


# --- Reading a narration back to the template that produced it. ---

ALL_TEMPLATES: tuple[str, ...] = (
    _LEDGER_NARRATION_TEMPLATES
    + _CLEAN_CREDIT_TEMPLATES
    + _EMBEDDED_CREDIT_TEMPLATES
    + _DEBIT_TEMPLATES
    + REVERSAL_TEMPLATES
    + _ABSENT_CREDIT_TEMPLATES
    + OPAQUE_CREDIT_NARRATIONS
    + _BANK_CHARGE_NARRATIONS
)
"""Every sentence shape the generator can write, most specific first.

Order is load-bearing for `narration_template`: the templates carrying a
`{ref}` slot must be tried before the otherwise-identical ones that drop
it, or an absent-UTR shape would claim a narration that has a UTR in it.
"""

_PLACEHOLDER_PATTERNS = {
    # Party names are letters and spaces only, which is what keeps
    # "NEFT CR {party}" from swallowing a trailing UTR and passing itself
    # off as the absent shape.
    "{party}": r"[A-Z ]+",
    "{ref}": r"[A-Z0-9]+",
    "{method}": r"[A-Z]+",
}


_PLACEHOLDER_SPLIT = re.compile("(" + "|".join(re.escape(p) for p in _PLACEHOLDER_PATTERNS) + ")")


def _template_regex(template: str) -> re.Pattern[str]:
    return re.compile(
        "".join(
            _PLACEHOLDER_PATTERNS.get(part, re.escape(part))
            for part in _PLACEHOLDER_SPLIT.split(template)
        )
    )


_TEMPLATE_REGEXES = tuple((template, _template_regex(template)) for template in ALL_TEMPLATES)


def narration_template(narration: str) -> str:
    """The pool template `narration` was written from.

    The fingerprint checkpoint groups narrations by template to ask
    whether the *choice* of sentence shape correlates with scenario, which
    needs the shape recovered from the text. It raises rather than
    returning `None` on a miss: an unrecognised narration means a string
    was written from outside the shared pool, which is the exact §3.5
    failure this module exists to prevent.
    """
    for template, regex in _TEMPLATE_REGEXES:
        if regex.fullmatch(narration):
            return template
    raise ValueError(f"narration {narration!r} was not written from the shared pool")
