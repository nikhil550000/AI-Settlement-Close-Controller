"""The T+2 working-day settlement-window rule, per spec.md §3.3.

> **Timing-residual rule.** A case reconciles cleanly when its residual is
> fully attributable to an expected timing item — that is, the
> settlement's `created_at` falls within the settlement window as of the
> batch snapshot. Past that window with no bank credit, the case flips to
> `OPERATIONAL_EXCEPTION` / `BANK_CREDIT_OVERDUE`.
>
> **Settlement window: T+2 working days**, weekends excluded, no
> public-holiday calendar. The weekends-only calendar is a deliberate
> simplification and is disclosed as such.

This module is generator-side only (session 2.2): it places the family-4
no-op and `BANK_CREDIT_OVERDUE` settlements on the correct side of the
window when *constructing* the batch. The Phase-3 matcher will need the
identical rule to *classify* cases at run time — `settlement_window_deadline`
and `is_within_settlement_window` are written so that session can import
them unchanged rather than re-deriving the working-day arithmetic.
"""

from __future__ import annotations

from datetime import date, timedelta

_SATURDAY = 5
_SUNDAY = 6
SETTLEMENT_WINDOW_WORKING_DAYS = 2


def _is_weekend(d: date) -> bool:
    return d.weekday() in (_SATURDAY, _SUNDAY)


def add_working_days(start: date, n: int) -> date:
    """`start` plus `n` working days (Mon-Fri); weekends are skipped entirely, not counted."""
    current = start
    remaining = n
    while remaining > 0:
        current += timedelta(days=1)
        if not _is_weekend(current):
            remaining -= 1
    return current


def subtract_working_days(start: date, n: int) -> date:
    """`start` minus `n` working days (Mon-Fri); weekends are skipped entirely, not counted."""
    current = start
    remaining = n
    while remaining > 0:
        current -= timedelta(days=1)
        if not _is_weekend(current):
            remaining -= 1
    return current


def settlement_window_deadline(settlement_created_date: date) -> date:
    """The last date (inclusive) on which the settlement window is still open (§3.3: T+2 working days)."""
    return add_working_days(settlement_created_date, SETTLEMENT_WINDOW_WORKING_DAYS)


def is_within_settlement_window(settlement_created_date: date, snapshot_date: date) -> bool:
    """True while `snapshot_date` still falls inside the T+2 working-day window."""
    return snapshot_date <= settlement_window_deadline(settlement_created_date)
