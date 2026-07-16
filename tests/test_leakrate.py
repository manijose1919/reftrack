"""Unit tests for the EPA annualizing leak-rate method (pure math)."""

from datetime import date

import pytest

from reftrack.leakrate import annualized_leak_rate, exceeds_threshold


def test_epa_worked_example_25_percent():
    # 5 lbs added to a 100 lb full charge, 73 days after last addition:
    # (5/100) * (365/73) * 100 = 25.0%
    r = annualized_leak_rate(5.0, 100.0, date(2026, 1, 1), date(2026, 3, 15))
    assert r.days_elapsed == 73
    assert r.rate_pct == 25.0


def test_epa_worked_example_industrial():
    # 75 lbs added to a 500 lb charge after 180 days:
    # (75/500) * (365/180) * 100 = 30.42%
    r = annualized_leak_rate(75.0, 500.0, date(2026, 1, 1), date(2026, 6, 30))
    assert r.days_elapsed == 180
    assert r.rate_pct == pytest.approx(30.42, abs=0.01)


def test_full_year_simple_percentage():
    # Exactly 365 days: annualization factor is 1, rate is just added/charge.
    r = annualized_leak_rate(10.0, 100.0, date(2025, 1, 1), date(2026, 1, 1))
    assert r.rate_pct == 10.0


def test_same_day_clamps_to_one_day():
    d = date(2026, 5, 1)
    r = annualized_leak_rate(1.0, 100.0, d, d)
    assert r.days_elapsed == 0
    assert r.rate_pct == 365.0  # (1/100) * 365/1 * 100


def test_rejects_nonpositive_inputs():
    d1, d2 = date(2026, 1, 1), date(2026, 2, 1)
    with pytest.raises(ValueError):
        annualized_leak_rate(0.0, 100.0, d1, d2)
    with pytest.raises(ValueError):
        annualized_leak_rate(5.0, 0.0, d1, d2)


def test_rejects_out_of_order_dates():
    with pytest.raises(ValueError):
        annualized_leak_rate(5.0, 100.0, date(2026, 2, 1), date(2026, 1, 1))


def test_threshold_is_strictly_greater():
    assert not exceeds_threshold(10.0, 10.0)  # exactly at threshold: not exceeded
    assert exceeds_threshold(10.01, 10.0)
