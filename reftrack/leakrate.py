"""EPA Section 608 annualized leak-rate calculation (pure functions).

Implements the "annualizing" method from 40 CFR 82.152:

    leak rate (%) = (pounds added / full charge) x (365 / days since
                     refrigerant was last added) x 100

Notes:
- Only refrigerant ADDITIONS drive the leak rate; recoveries do not.
- For the first addition on record, the baseline date is the appliance
  installation date (the last time it held a known full charge).
- Same-day follow-up additions (days == 0) are treated as part of the same
  service episode: the pounds are combined with the prior addition rather
  than annualized over zero days, per EPA guidance on multiple additions
  within a single service event.
"""

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class LeakRateResult:
    rate_pct: float
    days_elapsed: int
    method: str = "annualizing"


def annualized_leak_rate(
    pounds_added: float,
    full_charge_lbs: float,
    last_addition_date: date,
    this_addition_date: date,
) -> LeakRateResult:
    """Compute the annualized leak rate for a refrigerant addition.

    Raises ValueError on non-positive charge/pounds or out-of-order dates.
    """
    if full_charge_lbs <= 0:
        raise ValueError("full charge must be positive")
    if pounds_added <= 0:
        raise ValueError("pounds added must be positive")
    if this_addition_date < last_addition_date:
        raise ValueError(
            f"addition date {this_addition_date} precedes baseline "
            f"{last_addition_date}"
        )

    days = (this_addition_date - last_addition_date).days
    # Same-day additions are one service episode: annualize over 1 day would
    # explode the rate, so treat the elapsed period as 1 day minimum only when
    # the caller has not already merged episodes. We surface days=0 to let the
    # service layer merge; here we clamp to 1 for a standalone calculation.
    effective_days = max(days, 1)

    rate = (pounds_added / full_charge_lbs) * (365.0 / effective_days) * 100.0
    return LeakRateResult(rate_pct=round(rate, 2), days_elapsed=days)


def exceeds_threshold(rate_pct: float, threshold_pct: float) -> bool:
    """EPA thresholds are exceeded when the rate is strictly greater."""
    return rate_pct > threshold_pct
