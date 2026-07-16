"""Business logic: service-event logging, cylinder inventory, compliance cases.

All state transitions go through this module so the API and UI layers stay
thin and every rule lives in exactly one place.
"""

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from reftrack import alerts, leakrate
from reftrack.models import (
    Appliance,
    CaseStatus,
    ComplianceCase,
    Cylinder,
    CylinderKind,
    EventType,
    ServiceEvent,
    ShopProfile,
    Technician,
)

logger = logging.getLogger("reftrack.service")

# DOT/EPA safe-fill limit for recovery cylinders.
RECOVERY_FILL_LIMIT = 0.80
REPAIR_WINDOW_DAYS = 30


class DomainError(ValueError):
    """A business-rule violation with a user-facing message."""


def get_shop_profile(db: Session) -> ShopProfile:
    """Fetch (or lazily create) the single shop-profile row."""
    profile = db.get(ShopProfile, 1)
    if profile is None:
        profile = ShopProfile(id=1)
        db.add(profile)
        db.commit()
    return profile


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
            ServiceEvent.voided == False,  # noqa: E712
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
        ServiceEvent.voided == False,  # noqa: E712
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
        .where(
            ServiceEvent.appliance_id == appliance.id,
            ServiceEvent.voided == False,  # noqa: E712
        )
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
        due = event_date + timedelta(days=REPAIR_WINDOW_DAYS)
        db.add(
            ComplianceCase(
                appliance_id=appliance.id,
                opened_event_id=event.id,
                opened_date=event_date,
                due_date=due,
                leak_rate_pct=rate_pct,
            )
        )
        alerts.send_case_alert(
            appliance_name=appliance.name,
            customer_name=appliance.customer.name,
            leak_rate_pct=rate_pct,
            threshold_pct=appliance.category.leak_threshold_pct,
            due_date=due.isoformat(),
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
    passed: bool | None = None,
) -> ServiceEvent:
    """Record a repair, verification test, retrofit, or retirement.

    Case lifecycle effects:
    - LEAK_REPAIR closes the open case as repaired.
    - RETROFIT / RETIRED close the case; RETIRED deactivates the appliance.
    - VERIFICATION_FOLLOWUP with passed=False REOPENS the obligation: a new
      case is created (the repair did not hold), with a fresh 30-day clock.
    """
    if event_type in (EventType.CHARGE_ADDED, EventType.RECOVERED):
        raise DomainError("Use the charge/recovery endpoints for refrigerant moves.")
    if not appliance.active and event_type != EventType.RETIRED:
        raise DomainError("Appliance is retired.")
    if passed is not None and event_type not in (
        EventType.VERIFICATION_INITIAL, EventType.VERIFICATION_FOLLOWUP
    ):
        raise DomainError("Pass/fail results apply only to verification tests.")
    _validate_event_order(db, appliance, event_date)

    event = ServiceEvent(
        appliance_id=appliance.id,
        technician_id=technician.id,
        event_date=event_date,
        event_type=event_type,
        notes=notes,
        passed=passed,
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

    elif event_type == EventType.VERIFICATION_FOLLOWUP and passed is False:
        # The fix did not hold: reopen the obligation as a new case carrying
        # the leak rate of the most recent resolved case. RETIRED covers a
        # case closed by a retrofit -- a retrofit that still leaks reopens the
        # obligation just as a failed repair does.
        prior = db.execute(
            select(ComplianceCase)
            .where(
                ComplianceCase.appliance_id == appliance.id,
                ComplianceCase.status.in_(
                    (CaseStatus.REPAIRED, CaseStatus.RETIRED)
                ),
            )
            .order_by(ComplianceCase.opened_date.desc())
        ).scalars().first()
        if prior is not None:
            db.add(
                ComplianceCase(
                    appliance_id=appliance.id,
                    opened_event_id=event.id,
                    opened_date=event_date,
                    due_date=event_date + timedelta(days=REPAIR_WINDOW_DAYS),
                    leak_rate_pct=prior.leak_rate_pct,
                )
            )
        else:
            # A failed verification with no prior exceedance on record does not
            # itself create an EPA repair obligation (the obligation arises from
            # exceeding a threshold). Log it: callers must not claim otherwise.
            logger.info(
                "Failed follow-up verification on appliance %s (%s) with no "
                "prior compliance case; no repair obligation opened.",
                appliance.id, appliance.name,
            )

    if event_type == EventType.RETIRED:
        appliance.active = False

    db.commit()
    return event


def record_plan(
    db: Session,
    *,
    case: ComplianceCase,
    plan_date: date,
    plan_notes: str,
) -> ComplianceCase:
    """Attach the dated retrofit/retirement plan (40 CFR 82.157) to a case."""
    if case.status != CaseStatus.OPEN:
        raise DomainError("Plans can only be recorded on open cases.")
    if not plan_notes.strip():
        raise DomainError("Plan notes are required.")
    if plan_date < case.opened_date:
        raise DomainError("Plan date cannot precede the case opening date.")
    case.plan_date = plan_date
    case.plan_notes = plan_notes.strip()
    db.commit()
    return case


# ---- Void / amend ------------------------------------------------------------

def void_event(db: Session, *, event: ServiceEvent, reason: str) -> ServiceEvent:
    """Void a mistaken event (audit-safe: the row is kept, marked voided).

    Reverses cylinder inventory, reactivates the appliance if the voided event
    retired it, then recomputes all downstream leak rates and reconciles
    compliance cases.
    """
    if event.voided:
        raise DomainError("Event is already voided.")
    if not reason.strip():
        raise DomainError("A void reason is required for the audit trail.")

    appliance = event.appliance
    cylinder = event.cylinder

    if event.event_type == EventType.CHARGE_ADDED and cylinder is not None:
        restored = round(cylinder.current_lbs + (event.pounds or 0.0), 2)
        if restored > cylinder.capacity_lbs:
            raise DomainError(
                f"Voiding would put cylinder {cylinder.serial} at "
                f"{restored:.1f} lbs, over its {cylinder.capacity_lbs:.1f} lb "
                "capacity. Correct the cylinder record first."
            )
        cylinder.current_lbs = restored
    elif event.event_type == EventType.RECOVERED and cylinder is not None:
        drained = round(cylinder.current_lbs - (event.pounds or 0.0), 2)
        if drained < 0:
            raise DomainError(
                f"Voiding would put cylinder {cylinder.serial} below zero. "
                "Correct the cylinder record first."
            )
        cylinder.current_lbs = drained
    elif event.event_type == EventType.RETIRED:
        appliance.active = True

    event.voided = True
    event.void_reason = reason.strip()
    event.voided_at = datetime.now(timezone.utc)
    db.flush()

    recompute_appliance(db, appliance)
    db.commit()
    return event


def recompute_appliance(db: Session, appliance: Appliance) -> None:
    """Recompute leak-rate snapshots for all non-voided additions and
    reconcile compliance cases. Called after a void changes history."""
    additions = db.execute(
        select(ServiceEvent)
        .where(
            ServiceEvent.appliance_id == appliance.id,
            ServiceEvent.event_type == EventType.CHARGE_ADDED,
            ServiceEvent.voided == False,  # noqa: E712
        )
        .order_by(ServiceEvent.event_date, ServiceEvent.id)
    ).scalars().all()

    threshold = appliance.category.leak_threshold_pct
    prev_episode_date: date | None = None
    episode_pounds = 0.0
    for ev in additions:
        if ev.event_date != prev_episode_date:
            # New episode begins; its baseline is the previous episode's date.
            episode_pounds = 0.0
        episode_pounds += ev.pounds or 0.0

        if appliance.tracked:
            baseline = _last_addition_date(db, appliance, before=ev.event_date)
            result = leakrate.annualized_leak_rate(
                pounds_added=episode_pounds,
                full_charge_lbs=appliance.full_charge_lbs,
                last_addition_date=baseline,
                this_addition_date=ev.event_date,
            )
            ev.leak_rate_pct = result.rate_pct
            ev.threshold_exceeded = leakrate.exceeds_threshold(
                result.rate_pct, threshold
            )
        else:
            ev.leak_rate_pct = None
            ev.threshold_exceeded = False
        prev_episode_date = ev.event_date
    db.flush()

    cases = db.execute(
        select(ComplianceCase)
        .where(ComplianceCase.appliance_id == appliance.id)
        .order_by(ComplianceCase.opened_date)
    ).scalars().all()

    for case in cases:
        if case.status == CaseStatus.VOIDED:
            continue
        opener = db.get(ServiceEvent, case.opened_event_id)
        if opener is None or opener.voided or (
            opener.event_type == EventType.CHARGE_ADDED
            and not opener.threshold_exceeded
        ):
            case.status = CaseStatus.VOIDED
            continue

        # The opener's rate may have been recomputed above; a surviving case
        # must not keep the stale figure -- it prints onto the audit PDF.
        if (
            opener.event_type == EventType.CHARGE_ADDED
            and opener.leak_rate_pct is not None
        ):
            case.leak_rate_pct = opener.leak_rate_pct

        if case.resolved_event_id is not None:
            resolver = db.get(ServiceEvent, case.resolved_event_id)
            if resolver is not None and resolver.voided:
                # The event that closed this case was voided: obligation stands.
                case.status = CaseStatus.OPEN
                case.resolved_event_id = None
                case.resolved_date = None

    # Invariant: an appliance has at most ONE live repair obligation. Reopening
    # a case above can collide with a case opened later, and two OPEN rows make
    # open_case() arbitrary -- a subsequent repair would close only one, and the
    # survivor would then mask every future exceedance (log_charge_addition only
    # opens a case when none is open). Both rows describe the same unrepaired
    # leak, so keep the EARLIEST: its 30-day clock started first and is the
    # binding deadline.
    live = sorted(
        (c for c in cases if c.status == CaseStatus.OPEN),
        key=lambda c: (c.opened_date, c.id),
    )
    for duplicate in live[1:]:
        duplicate.status = CaseStatus.SUPERSEDED
        duplicate.resolved_event_id = None
        duplicate.resolved_date = None
        logger.info(
            "Appliance %s: case %s superseded by earlier open case %s "
            "(same unrepaired leak).", appliance.id, duplicate.id, live[0].id
        )
    db.flush()

    # If an exceedance now has no case covering it (e.g. its case was voided
    # but a later addition still exceeds), open one from the earliest
    # uncovered event. "Covered" = a live case was open on that event's date.
    def _covered(ev: ServiceEvent) -> bool:
        for c in cases:
            if c.status in (CaseStatus.VOIDED, CaseStatus.SUPERSEDED):
                continue
            if c.opened_date <= ev.event_date and (
                c.resolved_date is None or c.resolved_date >= ev.event_date
            ):
                return True
        return False

    has_open = any(c.status == CaseStatus.OPEN for c in cases)
    if not has_open:
        for ev in additions:
            if ev.threshold_exceeded and not _covered(ev):
                db.add(
                    ComplianceCase(
                        appliance_id=appliance.id,
                        opened_event_id=ev.id,
                        opened_date=ev.event_date,
                        due_date=ev.event_date + timedelta(days=REPAIR_WINDOW_DAYS),
                        leak_rate_pct=ev.leak_rate_pct or 0.0,
                    )
                )
                break
    db.flush()
