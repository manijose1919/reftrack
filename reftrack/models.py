"""Domain models for EPA Section 608 refrigerant tracking.

Regulatory grounding (40 CFR Part 82, Subpart F):
- Appliances with a full charge >= 50 lbs require leak-rate tracking.
- Annualized leak-rate thresholds by equipment category:
    comfort cooling 10%, commercial refrigeration 20%, industrial process 30%.
- Exceeding a threshold starts a 30-day clock to repair (or develop a
  retrofit/retirement plan).
"""

import enum
from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from reftrack.database import Base


class EquipmentCategory(str, enum.Enum):
    COMFORT_COOLING = "comfort_cooling"
    COMMERCIAL_REFRIGERATION = "commercial_refrigeration"
    INDUSTRIAL_PROCESS = "industrial_process"

    @property
    def leak_threshold_pct(self) -> float:
        return {
            EquipmentCategory.COMFORT_COOLING: 10.0,
            EquipmentCategory.COMMERCIAL_REFRIGERATION: 20.0,
            EquipmentCategory.INDUSTRIAL_PROCESS: 30.0,
        }[self]

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").title()


class EventType(str, enum.Enum):
    CHARGE_ADDED = "charge_added"          # refrigerant added to appliance
    RECOVERED = "recovered"                # refrigerant recovered from appliance
    LEAK_REPAIR = "leak_repair"            # repair performed
    VERIFICATION_INITIAL = "verification_initial"    # initial verification test
    VERIFICATION_FOLLOWUP = "verification_followup"  # follow-up verification test
    RETROFIT = "retrofit"                  # appliance retrofitted
    RETIRED = "retired"                    # appliance retired from service

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").title()


class CylinderKind(str, enum.Enum):
    SUPPLY = "supply"      # virgin/reclaimed refrigerant drawn for charging
    RECOVERY = "recovery"  # receives refrigerant recovered from appliances


class CaseStatus(str, enum.Enum):
    OPEN = "open"
    REPAIRED = "repaired"
    RETIRED = "retired"


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    address: Mapped[str] = mapped_column(String(300), default="")
    contact: Mapped[str] = mapped_column(String(200), default="")

    appliances: Mapped[list["Appliance"]] = relationship(back_populates="customer")


class Technician(Base):
    __tablename__ = "technicians"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    epa_cert_number: Mapped[str] = mapped_column(String(50))
    cert_type: Mapped[str] = mapped_column(String(20), default="Universal")

    events: Mapped[list["ServiceEvent"]] = relationship(back_populates="technician")


class Appliance(Base):
    __tablename__ = "appliances"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    name: Mapped[str] = mapped_column(String(200))
    location: Mapped[str] = mapped_column(String(300), default="")
    refrigerant_type: Mapped[str] = mapped_column(String(30))  # e.g. R-410A
    full_charge_lbs: Mapped[float] = mapped_column(Float)
    category: Mapped[EquipmentCategory] = mapped_column(
        Enum(EquipmentCategory, values_callable=lambda e: [m.value for m in e])
    )
    install_date: Mapped[date] = mapped_column(Date)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    customer: Mapped["Customer"] = relationship(back_populates="appliances")
    events: Mapped[list["ServiceEvent"]] = relationship(
        back_populates="appliance", order_by="ServiceEvent.event_date"
    )
    cases: Mapped[list["ComplianceCase"]] = relationship(back_populates="appliance")

    @property
    def tracked(self) -> bool:
        """EPA leak-rate tracking applies at full charge >= 50 lbs."""
        return self.full_charge_lbs >= 50.0


class Cylinder(Base):
    __tablename__ = "cylinders"

    id: Mapped[int] = mapped_column(primary_key=True)
    serial: Mapped[str] = mapped_column(String(50), unique=True)
    refrigerant_type: Mapped[str] = mapped_column(String(30))
    kind: Mapped[CylinderKind] = mapped_column(
        Enum(CylinderKind, values_callable=lambda e: [m.value for m in e])
    )
    capacity_lbs: Mapped[float] = mapped_column(Float)
    current_lbs: Mapped[float] = mapped_column(Float, default=0.0)

    events: Mapped[list["ServiceEvent"]] = relationship(back_populates="cylinder")


class ServiceEvent(Base):
    __tablename__ = "service_events"
    __table_args__ = (
        Index("ix_events_appliance_date", "appliance_id", "event_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    appliance_id: Mapped[int] = mapped_column(ForeignKey("appliances.id"))
    technician_id: Mapped[int] = mapped_column(ForeignKey("technicians.id"))
    cylinder_id: Mapped[int | None] = mapped_column(
        ForeignKey("cylinders.id"), nullable=True
    )
    event_date: Mapped[date] = mapped_column(Date)
    event_type: Mapped[EventType] = mapped_column(
        Enum(EventType, values_callable=lambda e: [m.value for m in e])
    )
    pounds: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    # Snapshot of the annualized leak rate computed at this event (additions only).
    leak_rate_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold_exceeded: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )

    appliance: Mapped["Appliance"] = relationship(back_populates="events")
    technician: Mapped["Technician"] = relationship(back_populates="events")
    cylinder: Mapped["Cylinder | None"] = relationship(back_populates="events")


class ComplianceCase(Base):
    __tablename__ = "compliance_cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    appliance_id: Mapped[int] = mapped_column(ForeignKey("appliances.id"))
    opened_event_id: Mapped[int] = mapped_column(ForeignKey("service_events.id"))
    opened_date: Mapped[date] = mapped_column(Date)
    due_date: Mapped[date] = mapped_column(Date)  # opened_date + 30 days
    leak_rate_pct: Mapped[float] = mapped_column(Float)
    status: Mapped[CaseStatus] = mapped_column(
        Enum(CaseStatus, values_callable=lambda e: [m.value for m in e]),
        default=CaseStatus.OPEN,
    )
    resolved_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("service_events.id"), nullable=True
    )
    resolved_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    appliance: Mapped["Appliance"] = relationship(back_populates="cases")
    opened_event: Mapped["ServiceEvent"] = relationship(foreign_keys=[opened_event_id])
    resolved_event: Mapped["ServiceEvent | None"] = relationship(
        foreign_keys=[resolved_event_id]
    )

    def is_overdue(self, today: date) -> bool:
        return self.status == CaseStatus.OPEN and today > self.due_date
