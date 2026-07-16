"""Business-logic tests: inventory, leak-rate snapshots, compliance cases."""

from datetime import date, timedelta

import pytest

from reftrack import service
from reftrack.models import (
    Appliance,
    CaseStatus,
    ComplianceCase,
    CylinderKind,
    EquipmentCategory,
    EventType,
)
from reftrack.service import DomainError


def _charge(world, day, lbs, **kw):
    return service.log_charge_addition(
        world["db"], appliance=world["appliance"], technician=world["tech"],
        cylinder=world["supply"], event_date=day, pounds=lbs, **kw,
    )


def test_charge_decrements_cylinder_and_snapshots_rate(world):
    # 5 lbs on a 100 lb charge, 73 days after install baseline (2025-01-01).
    ev = _charge(world, date(2025, 3, 15), 5.0)
    assert world["supply"].current_lbs == 95.0
    assert ev.leak_rate_pct == 25.0
    assert ev.threshold_exceeded is True  # comfort cooling threshold = 10%


def test_below_threshold_opens_no_case(world):
    # 5 lbs after a full year: 5% < 10% threshold.
    ev = _charge(world, date(2026, 1, 1), 5.0)
    assert ev.threshold_exceeded is False
    assert world["db"].query(ComplianceCase).count() == 0


def test_exceedance_opens_single_case_with_30_day_clock(world):
    day = date(2025, 3, 15)
    _charge(world, day, 5.0)                      # 25% -> opens case
    _charge(world, day + timedelta(days=5), 4.0)  # still leaking -> no duplicate
    cases = world["db"].query(ComplianceCase).all()
    assert len(cases) == 1
    assert cases[0].status == CaseStatus.OPEN
    assert cases[0].due_date == day + timedelta(days=30)


def test_repair_resolves_open_case(world):
    _charge(world, date(2025, 3, 15), 5.0)
    service.log_maintenance_event(
        world["db"], appliance=world["appliance"], technician=world["tech"],
        event_date=date(2025, 3, 20), event_type=EventType.LEAK_REPAIR,
    )
    case = world["db"].query(ComplianceCase).one()
    assert case.status == CaseStatus.REPAIRED
    assert case.resolved_date == date(2025, 3, 20)


def test_retirement_deactivates_appliance_and_blocks_charges(world):
    service.log_maintenance_event(
        world["db"], appliance=world["appliance"], technician=world["tech"],
        event_date=date(2025, 2, 1), event_type=EventType.RETIRED,
    )
    assert world["appliance"].active is False
    with pytest.raises(DomainError, match="retired"):
        _charge(world, date(2025, 3, 1), 5.0)


def test_same_day_additions_merge_into_one_episode(world):
    day = date(2025, 3, 15)  # 73 days after install
    ev1 = _charge(world, day, 2.0)
    ev2 = _charge(world, day, 3.0)
    # Episode total is 5 lbs -> (5/100)*(365/73)*100 = 25%
    assert ev1.leak_rate_pct == 10.0
    assert ev2.leak_rate_pct == 25.0


def test_refrigerant_mismatch_rejected(world):
    world["supply"].refrigerant_type = "R-22"
    world["db"].commit()
    with pytest.raises(DomainError, match="mismatch"):
        _charge(world, date(2025, 3, 1), 5.0)


def test_cannot_draw_more_than_cylinder_holds(world):
    world["supply"].current_lbs = 2.0
    world["db"].commit()
    with pytest.raises(DomainError, match="only"):
        _charge(world, date(2025, 3, 1), 5.0)


def test_out_of_order_event_rejected(world):
    _charge(world, date(2025, 6, 1), 3.0)
    with pytest.raises(DomainError, match="earlier"):
        _charge(world, date(2025, 5, 1), 3.0)


def test_recovery_respects_80_percent_fill_limit(world):
    rec = world["recovery"]  # 100 lb capacity -> 80 lb safe fill
    service.log_recovery(
        world["db"], appliance=world["appliance"], technician=world["tech"],
        cylinder=rec, event_date=date(2025, 2, 1), pounds=79.0,
    )
    assert rec.current_lbs == 79.0
    with pytest.raises(DomainError, match="safe-fill"):
        service.log_recovery(
            world["db"], appliance=world["appliance"], technician=world["tech"],
            cylinder=rec, event_date=date(2025, 2, 2), pounds=2.0,
        )


def test_recovery_into_supply_cylinder_rejected(world):
    with pytest.raises(DomainError, match="supply"):
        service.log_recovery(
            world["db"], appliance=world["appliance"], technician=world["tech"],
            cylinder=world["supply"], event_date=date(2025, 2, 1), pounds=5.0,
        )


def test_untracked_appliance_gets_no_leak_rate(world):
    db = world["db"]
    small = Appliance(
        customer=world["customer"], name="Mini Split",
        refrigerant_type="R-410A", full_charge_lbs=12.0,
        category=EquipmentCategory.COMFORT_COOLING,
        install_date=date(2025, 1, 1),
    )
    db.add(small)
    db.commit()
    ev = service.log_charge_addition(
        db, appliance=small, technician=world["tech"],
        cylinder=world["supply"], event_date=date(2025, 3, 1), pounds=2.0,
    )
    assert ev.leak_rate_pct is None
    assert ev.threshold_exceeded is False
    assert db.query(ComplianceCase).count() == 0
