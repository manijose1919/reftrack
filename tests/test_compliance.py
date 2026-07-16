"""Compliance status derivation and report generation tests."""

import csv
import io
from datetime import date, timedelta

from reftrack import compliance, reports, service
from reftrack.compliance import Status
from reftrack.models import EventType


def _charge(world, day, lbs):
    return service.log_charge_addition(
        world["db"], appliance=world["appliance"], technician=world["tech"],
        cylinder=world["supply"], event_date=day, pounds=lbs,
    )


def test_status_ok_when_no_events(world):
    st = compliance.appliance_status(world["db"], world["appliance"], date(2025, 6, 1))
    assert st.status == Status.OK
    assert st.current_leak_rate_pct is None


def test_status_watch_near_threshold(world):
    # 8 lbs over a full year on a 100 lb charge = 8% -> >= 75% of the 10% threshold.
    _charge(world, date(2026, 1, 1), 8.0)
    st = compliance.appliance_status(world["db"], world["appliance"], date(2026, 1, 2))
    assert st.status == Status.WATCH


def test_status_action_required_then_overdue(world):
    day = date(2025, 3, 15)
    _charge(world, day, 5.0)  # 25% -> case opens, due day+30
    st = compliance.appliance_status(world["db"], world["appliance"], day + timedelta(days=10))
    assert st.status == Status.ACTION_REQUIRED
    assert st.days_until_due == 20

    st = compliance.appliance_status(world["db"], world["appliance"], day + timedelta(days=31))
    assert st.status == Status.OVERDUE
    assert st.days_until_due == -1


def test_status_returns_to_ok_after_repair(world):
    _charge(world, date(2025, 3, 15), 5.0)
    service.log_maintenance_event(
        world["db"], appliance=world["appliance"], technician=world["tech"],
        event_date=date(2025, 3, 20), event_type=EventType.LEAK_REPAIR,
    )
    st = compliance.appliance_status(world["db"], world["appliance"], date(2025, 4, 1))
    # No open case; last rate (25%) is above watch fraction so WATCH, not OK.
    assert st.status == Status.WATCH


def test_shop_summary_counts(world):
    _charge(world, date(2025, 3, 15), 5.0)
    summary = compliance.shop_summary(world["db"], date(2025, 3, 20))
    assert summary["total"] == 1
    assert summary["counts"][Status.ACTION_REQUIRED] == 1


def test_appliance_csv_contents(world):
    _charge(world, date(2025, 3, 15), 5.0)
    text = reports.appliance_csv(world["db"], world["appliance"])
    rows = list(csv.DictReader(io.StringIO(text)))
    assert len(rows) == 1
    assert rows[0]["annualized_leak_rate_pct"] == "25.00"
    assert rows[0]["threshold_exceeded"] == "yes"
    assert rows[0]["epa_cert"] == "U-000001"


def test_cylinders_csv(world):
    text = reports.cylinders_csv(world["db"])
    rows = list(csv.DictReader(io.StringIO(text)))
    serials = {r["serial"] for r in rows}
    assert {"SUP-1", "REC-1"} <= serials


def test_appliance_pdf_generates(world):
    _charge(world, date(2025, 3, 15), 5.0)
    pdf = reports.appliance_pdf(world["db"], world["appliance"], date(2025, 4, 20))
    assert pdf[:5] == b"%PDF-"
    assert len(pdf) > 1500
