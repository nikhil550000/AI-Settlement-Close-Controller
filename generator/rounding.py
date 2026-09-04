"""Fee/GST rounding.

`decimal.Decimal` with `ROUND_HALF_UP` is permitted only here, and only
because the generator needs it to derive fee and GST from a percentage.
The result is cast to `Paise` (`int`) immediately — no `Decimal` or float
value crosses out of this module.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from pipeline.money import Paise


def percentage_of_paise(base: Paise, percent: Decimal) -> Paise:
    """`base * percent / 100`, rounded half-up to the nearest paisa."""
    exact = Decimal(int(base)) * percent / Decimal(100)
    return Paise(int(exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))
