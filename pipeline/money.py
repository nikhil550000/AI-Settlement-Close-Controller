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
