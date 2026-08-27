"""Money primitives shared across the pipeline and generator.

Per AGENT.md's money rule: integer paise end to end, no floats, ever.
`decimal.Decimal` with ROUND_HALF_UP is permitted only inside the
generator's fee/GST rounding and must be cast to int immediately there —
nothing in this module touches floats or Decimal.
"""

from __future__ import annotations

from typing import Annotated, NewType

from pydantic import Field

Paise = NewType("Paise", int)
"""An amount of money in integer paise. Never a float."""

NonNegPaise = Annotated[Paise, Field(ge=0)]
"""Paise constrained to zero or positive.

Every money field in the canonical schemas (spec.md §3.1) is a magnitude
(a debit leg, a credit leg, a fee, a settlement total) — never a signed
net figure — so non-negativity is a boundary validation, not an invented
constraint.
"""


def rupees_string_to_paise(text: str) -> Paise:
    """Parse a bank-statement rupee string (comma-grouped, up to two decimal places) to paise.

    FR-08's adapter must handle "comma-grouped amount strings" without a
    float ever touching money (NFR-04). Every rupee figure a bank export
    prints already carries at most two decimal places, so this is exact
    string arithmetic — split on the decimal point, pad the fractional
    part to two digits — never a float or `Decimal` parse.
    """
    stripped = text.strip().replace(",", "")
    if not stripped:
        return Paise(0)
    whole, _, fraction = stripped.partition(".")
    whole = whole or "0"
    fraction = (fraction + "00")[:2]
    return Paise(int(whole) * 100 + int(fraction))


def paise_to_rupees_string(paise: int) -> str:
    """Render paise as an Indian-grouped rupee string (`"12,34,567.89"`), the inverse of `rupees_string_to_paise`."""
    whole, fraction = divmod(paise, 100)
    return f"{_indian_grouped(whole)}.{fraction:02d}"


def _indian_grouped(n: int) -> str:
    digits = str(n)
    if len(digits) <= 3:
        return digits
    last_three, rest = digits[-3:], digits[:-3]
    groups: list[str] = []
    while len(rest) > 2:
        groups.insert(0, rest[-2:])
        rest = rest[:-2]
    if rest:
        groups.insert(0, rest)
    return ",".join([*groups, last_three])
