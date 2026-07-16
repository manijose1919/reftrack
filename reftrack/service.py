"""Business logic: service-event logging, cylinder inventory, compliance cases.

All state transitions go through this module so the API and UI layers stay
thin and every rule lives in exactly one place.
"""

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from reftrack import leakrate
from reftrack.models import (
    Appliance,
    CaseStatus,
    ComplianceCase,
    Cylinder,
    CylinderKind,
    EventType,
    ServiceEvent,
    Technician,
)

# DOT/EPA safe-fill limit for recovery cylinders.
RECOVERY_FILL_LIMIT = 0.80
REPAIR_WINDOW_DAYS = 30


class DomainError(ValueError):
    """A business-rule violation with a user-facing message."""


def _last_addition_date(db: Session, appliance: Appliance, before: date) -> date:
    """Baseline for annualizing: last addition strictly before `before`,
    else installation date. Same-day additions are one service episode and
    must not serve as their own baseline."""
    stmt = (
        select(ServiceEvent.event_date)
        .where(
            ServiceEvent.appliance_id == appliance.id,
            ServiceEvent.event_type == EventType.CHARGE_ADDED,
            ServiceEvent.event_date < before,
        )
        .order_by(ServiceEvent.event_date.desc(), ServiceEvent.id.desc())
        .limit(1)
    )
    prev = db.execute(stmt).scalar_one_or_none()
    return prev or appliance.install_date


def _same_day_added_pounds(db: Session, appliance: Appliance, day: date) -> float:
    """Pounds already added to this appliance on the same date (one episode)."""
    stmt = select(ServiceEvent.pounds).where(
        ServiceEvent.appliance_id == appliance.id,
        ServiceEvent.event_type == EventType.CHARGE_ADDED,
        ServiceEvent.event_date == day,
    )
    return sum(p or 0.0 for p in db.execute(stmt).scalars())


def open_case(db: Session, appliance: Appliance) -> ComplianceCase | None:
    stmt = select(ComplianceCase).where(
        ComplianceCase.appliance_id == appliance.id,
        ComplianceCase.status == CaseStatus.OPEN,
    )
    return db.execute(stmt).scalars().first()


def _validate_event_order(db: Session, appliance: Appliance, event_date: date) -> None:
    stmt = (
        select(ServiceEvent.event_date)
        .where(ServiceEvent.appliance_id == appliance.id)
        .order_by(ServiceEvent.event_date.desc())
        .limit(1)
    )
    latest = db.execute(stmt).scalar_one_or_none()
    if latest and event_date < latest:
        raise DomainError(
            f"Event date {event_date} is earlier than the appliance's most "
            f"recent event ({latest}). Events must be logged in order."
        )
    if event_date < appliance.install_date:
        raise DomainError(
            f"Event date {event_date} precedes installation "
            f"({appliance.install_date})."
        )


def log_charge_addition(
    db: Session,
    *,
    appliance: Appliance,
    technician: Technician,
    cylinder: Cylinder,
    event_date: date,
    pounds: float,
    notes: str = "",
) -> ServiceEvent:
    """Record refrigerant added to an appliance; updates inventory, computes
    the annualized leak rate, and opens a compliance case on exceedance."""
    if pounds <= 0:
        raise DomainError("Pounds added must be positive.")
    if not appliance.active:
        raise DomainError("Appliance is retired; cannot add charge.")
    _validate_event_order(db, appliance, event_date)

    if cylinder.kind != CylinderKind.SUPPLY:
        raise DomainError(f"Cylinder {cylinder.serial} is a recovery cylinder.")
    if cylinder.refrigerant_type != appliance.refrigerant_type:
        raise DomainError(
            f"Refrigerant mismatch: cylinder holds {cylinder.refrigerant_type}, "
            f"appliance uses {appliance.refrigerant_type}."
        )
    if cylinder.current_lbs < pounds:
        raise DomainError(
            f"Cylinder {cylinder.serial} has only {cylinder.current_lbs:.1f} lbs."
        )
    if pounds > appliance.full_charge_lbs:
        raise DomainError(
            f"Cannot add {pounds:.1f} lbs to an appliance whose full charge "
            f"is {appliance.full_charge_lbs:.1f} lbs."
        )

    baseline = _last_addition_date(db, appliance, before=event_date)
    episode_pounds = pounds + _same_day_added_pounds(db, appliance, event_date)

    rate_pct: float | None = None
    exceeded = False
    if appliance.tracked:
        result = leakrate.annualized_leak_rate(
            pounds_added=episode_pounds,
            full_charge_lbs=appliance.full_charge_lbs,
            last_addition_date=baseline,
            this_addition_date=event_date,
        )
        rate_pct = result.rate_pct
        exceeded = leakrate.exceeds_threshold(
            rate_pct, appliance.category.leak_threshold_pct
        )

    cylinder.current_lbs = round(cylinder.current_lbs - pounds, 2)

    event = ServiceEvent(
        appliance_id=appliance.id,
        technician_id=technician.id,
        cylinder_id=cylinder.id,
        event_date=event_date,
        event_type=EventType.CHARGE_ADDED,
        pounds=pounds,
        notes=notes,
        leak_rate_pct=rate_pct,
        threshold_exceeded=exceeded,
    )
    db.add(event)
    db.flush()  # assign event.id before the case references it

    if exceeded and open_case(db, appliance) is None:
        db.add(
            ComplianceCase(
                appliance_id=appliance.id,
                opened_event_id=event.id,
                opened_date=event_date,
                due_date=event_date + timedelta(days=REPAIR_WINDOW_DAYS),
                leak_rate_pct=rate_pct,
            )
        )
    db.commit()
    return event


def log_recovery(
    db: Session,
    *,
    appliance: Appliance,
    technician: Technician,
    cylinder: Cylinder,
    event_date: date,
    pounds: float,
    notes: str = "",
) -> ServiceEvent:
    """Record refrigerant recovered from an appliance into a recovery cylinder."""
    if pounds <= 0:
        raise DomainError("Pounds recovered must be positive.")
    _validate_event_order(db, appliance, event_date)

    if cylinder.kind != CylinderKind.RECOVERY:
        raise DomainError(
            f"Cylinder {cylinder.serial} is a supply cylinder; recoveries must "
            "go into a recovery cylinder."
        )
    fill_limit = cylinder.capacity_lbs * RECOVERY_FILL_LIMIT
    if cylinder.current_lbs + pounds > fill_limit:
        raise DomainError(
            f"Recovery would put cylinder {cylinder.serial} at "
            f"{cylinder.current_lbs + pounds:.1f} lbs, over its 80% safe-fill "
            f"limit of {fill_limit:.1f} lbs."
        )

    cylinder.current_lbs = round(cylinder.current_lbs + pounds, 2)

    event = ServiceEvent(
        appliance_id=appliance.id,
        technician_id=technician.id,
        cylinder_id=cylinder.id,
        event_date=event_date,
        event_type=EventType.RECOVERED,
        pounds=pounds,
        notes=notes,
    )
    db.add(event)
    db.commit()
    return event


def log_maintenance_event(
    db: Session,
    *,
    appliance: Appliance,
    technician: Technician,
    event_date: date,
    event_type: EventType,
    notes: str = "",
) -> ServiceEvent:
    """Record a repair, verification test, retrofit, or retirement.

    Resolves the open compliance case where the regulation says it should:
    - LEAK_REPAIR closes the case as repaired (verification tests are logged
      but the repair itself is the resolving action tracked here).
    - RETROFIT / RETIRED close the case and RETIRED deactivates the appliance.
    """
    if event_type in (EventType.CHARGE_ADDED, EventType.RECOVERED):
        raise DomainError("Use the charge/recovery endpoints for refrigerant moves.")
    if not appliance.active and event_type != EventType.RETIRED:
        raise DomainError("Appliance is retired.")
    _validate_event_order(db, appliance, event_date)

    event = ServiceEvent(
        appliance_id=appliance.id,
        technician_id=technician.id,
        event_date=event_date,
        event_type=event_type,
        notes=notes,
    )
    db.add(event)
    db.flush()

    case = open_case(db, appliance)
    if case is not None:
        if event_type == EventType.LEAK_REPAIR:
            case.status = CaseStatus.REPAIRED
            case.resolved_event_id = event.id
            case.resolved_date = event_date
        elif event_type in (EventType.RETROFIT, EventType.RETIRED):
            case.status = CaseStatus.RETIRED
            case.resolved_event_id = event.id
            case.resolved_date = event_date

    if event_type == EventType.RETIRED:
        appliance.active = False

    db.commit()
    return event
