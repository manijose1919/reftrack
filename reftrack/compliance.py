"""Compliance status derivation.

Statuses are computed at read time from stored facts (events, cases) plus
today's date — never persisted — so "overdue" can't go stale.
"""

import enum
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from reftrack.models import (
    Appliance,
    CaseStatus,
    ComplianceCase,
    EventType,
    ServiceEvent,
)


class Status(str, enum.Enum):
    OK = "ok"                          # tracked, under threshold
    WATCH = "watch"                    # >= 75% of threshold: trending toward breach
    ACTION_REQUIRED = "action_required"  # open case, inside 30-day window
    OVERDUE = "overdue"                # open case, past due date
    EXEMPT = "exempt"                  # full charge < 50 lbs
    RETIRED = "retired"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").title()


WATCH_FRACTION = 0.75


@dataclass
class ApplianceStatus:
    appliance: Appliance
    status: Status
    current_leak_rate_pct: float | None
    threshold_pct: float
    open_case: ComplianceCase | None
    days_until_due: int | None  # negative if overdue


def latest_leak_rate(db: Session, appliance_id: int) -> float | None:
    stmt = (
        select(ServiceEvent.leak_rate_pct)
        .where(
            ServiceEvent.appliance_id == appliance_id,
            ServiceEvent.event_type == EventType.CHARGE_ADDED,
            ServiceEvent.leak_rate_pct.is_not(None),
        )
        .order_by(ServiceEvent.event_date.desc(), ServiceEvent.id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def _open_case(db: Session, appliance_id: int) -> ComplianceCase | None:
    stmt = select(ComplianceCase).where(
        ComplianceCase.appliance_id == appliance_id,
        ComplianceCase.status == CaseStatus.OPEN,
    )
    return db.execute(stmt).scalars().first()


def appliance_status(
    db: Session, appliance: Appliance, today: date
) -> ApplianceStatus:
    threshold = appliance.category.leak_threshold_pct
    rate = latest_leak_rate(db, appliance.id)
    case = _open_case(db, appliance.id)

    if not appliance.active:
        status = Status.RETIRED
    elif not appliance.tracked:
        status = Status.EXEMPT
    elif case is not None:
        status = Status.OVERDUE if today > case.due_date else Status.ACTION_REQUIRED
    elif rate is not None and rate >= threshold * WATCH_FRACTION:
        status = Status.WATCH
    else:
        status = Status.OK

    days_until_due = (case.due_date - today).days if case else None
    return ApplianceStatus(
        appliance=appliance,
        status=status,
        current_leak_rate_pct=rate,
        threshold_pct=threshold,
        open_case=case,
        days_until_due=days_until_due,
    )


def shop_summary(db: Session, today: date) -> dict:
    appliances = db.execute(select(Appliance).order_by(Appliance.name)).scalars().all()
    statuses = [appliance_status(db, a, today) for a in appliances]
    counts = {s: 0 for s in Status}
    for st in statuses:
        counts[st.status] += 1
    return {"statuses": statuses, "counts": counts, "total": len(statuses)}
