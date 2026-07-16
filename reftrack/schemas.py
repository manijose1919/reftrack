"""Pydantic schemas for the REST API."""

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from reftrack.models import CaseStatus, CylinderKind, EquipmentCategory, EventType


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---- Customers -------------------------------------------------------------

class CustomerIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    address: str = ""
    contact: str = ""


class CustomerOut(ORMModel):
    id: int
    name: str
    address: str
    contact: str


# ---- Technicians -----------------------------------------------------------

class TechnicianIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    epa_cert_number: str = Field(min_length=1, max_length=50)
    cert_type: str = "Universal"


class TechnicianOut(ORMModel):
    id: int
    name: str
    epa_cert_number: str
    cert_type: str


# ---- Appliances ------------------------------------------------------------

class ApplianceIn(BaseModel):
    customer_id: int
    name: str = Field(min_length=1, max_length=200)
    location: str = ""
    refrigerant_type: str = Field(min_length=1, max_length=30)
    full_charge_lbs: float = Field(gt=0)
    category: EquipmentCategory
    install_date: date


class ApplianceOut(ORMModel):
    id: int
    customer_id: int
    name: str
    location: str
    refrigerant_type: str
    full_charge_lbs: float
    category: EquipmentCategory
    install_date: date
    active: bool
    tracked: bool


# ---- Cylinders -------------------------------------------------------------

class CylinderIn(BaseModel):
    serial: str = Field(min_length=1, max_length=50)
    refrigerant_type: str = Field(min_length=1, max_length=30)
    kind: CylinderKind
    capacity_lbs: float = Field(gt=0)
    current_lbs: float = Field(ge=0, default=0.0)


class CylinderOut(ORMModel):
    id: int
    serial: str
    refrigerant_type: str
    kind: CylinderKind
    capacity_lbs: float
    current_lbs: float


# ---- Service events --------------------------------------------------------

class RefrigerantMoveIn(BaseModel):
    """Charge addition or recovery."""
    appliance_id: int
    technician_id: int
    cylinder_id: int
    event_date: date
    pounds: float = Field(gt=0)
    notes: str = ""


class MaintenanceEventIn(BaseModel):
    appliance_id: int
    technician_id: int
    event_date: date
    event_type: EventType
    notes: str = ""
    passed: bool | None = None  # verification tests only


class VoidEventIn(BaseModel):
    reason: str = Field(min_length=1, max_length=300)


class PlanIn(BaseModel):
    plan_date: date
    plan_notes: str = Field(min_length=1)


class ServiceEventOut(ORMModel):
    id: int
    appliance_id: int
    technician_id: int
    cylinder_id: int | None
    event_date: date
    event_type: EventType
    pounds: float | None
    notes: str
    leak_rate_pct: float | None
    threshold_exceeded: bool
    passed: bool | None
    voided: bool
    void_reason: str


# ---- Compliance ------------------------------------------------------------

class ComplianceCaseOut(ORMModel):
    id: int
    appliance_id: int
    opened_date: date
    due_date: date
    leak_rate_pct: float
    status: CaseStatus
    resolved_date: date | None
    plan_date: date | None
    plan_notes: str


class ShopProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    address: str = ""
    phone: str = ""
    epa_contact: str = ""


class ShopProfileOut(ORMModel):
    name: str
    address: str
    phone: str
    epa_contact: str
