"""Session 2.2: the T+2 working-day settlement-window rule (spec.md §3.3).

Relocated to `pipeline/timing.py` in session 3.3 so `pipeline/matcher.py`
can import it without violating the generator/pipeline import guard.
"""

from __future__ import annotations

from datetime import date

from pipeline.timing import add_working_days, is_within_settlement_window, subtract_working_days


def test_add_working_days_skips_weekend():
    # Friday 2026-08-28 + 2 working days -> Tuesday 2026-09-01 (skips Sat/Sun).
    friday = date(2026, 8, 28)
    assert add_working_days(friday, 2) == date(2026, 9, 1)


def test_add_working_days_within_one_week():
    monday = date(2026, 8, 24)
    assert add_working_days(monday, 2) == date(2026, 8, 26)


def test_subtract_working_days_skips_weekend():
    tuesday = date(2026, 9, 1)
    assert subtract_working_days(tuesday, 2) == date(2026, 8, 28)


def test_subtract_working_days_is_inverse_of_add_working_days():
    """Only meaningful for a weekday anchor: a weekend start has no fixed working-day position to return to."""
    for start_offset in range(10):
        start = date(2026, 8, 20 + start_offset)
        if start.weekday() >= 5:
            continue
        for n in range(5):
            assert subtract_working_days(add_working_days(start, n), n) == start


def test_is_within_settlement_window_true_at_exact_deadline():
    friday = date(2026, 8, 28)
    deadline = date(2026, 9, 1)  # friday + 2 working days
    assert is_within_settlement_window(friday, deadline)


def test_is_within_settlement_window_false_one_day_past_deadline():
    friday = date(2026, 8, 28)
    past_deadline = date(2026, 9, 2)
    assert not is_within_settlement_window(friday, past_deadline)


def test_no_op_construction_stays_inside_window_for_elapsed_zero_to_two():
    """Mirrors `generator/exceptions.py`'s no-op construction: subtract k<=2 working days, then check the window."""
    snapshot = date(2026, 8, 28)
    for k in (0, 1, 2):
        created = subtract_working_days(snapshot, k)
        assert is_within_settlement_window(created, snapshot)


def test_overdue_construction_lands_outside_window_for_elapsed_three_plus():
    snapshot = date(2026, 8, 28)
    for k in (3, 4, 5, 6):
        created = subtract_working_days(snapshot, k)
        assert not is_within_settlement_window(created, snapshot)
