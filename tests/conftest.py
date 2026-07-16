"""Test fixtures. Points REFTRACK_DB at a temp file BEFORE reftrack imports."""

import os
import tempfile

_fd, _path = tempfile.mkstemp(prefix="reftrack_test_", suffix=".db")
os.close(_fd)
os.environ["REFTRACK_DB"] = _path

from datetime import date  # noqa: E402

import pytest  # noqa: E402

from reftrack.database import Base, SessionLocal, engine, init_db  # noqa: E402
from reftrack.models import (  # noqa: E402
    Appliance,
    Customer,
    Cylinder,
    CylinderKind,
    EquipmentCategory,
    Technician,
)


@pytest.fixture()
def db():
    init_db()
    session = SessionLocal()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


@pytest.fixture()
def world(db):
    """A customer, tech, tracked appliance (100 lb R-410A comfort cooling),
    matching supply cylinder, and a recovery cylinder."""
    customer = Customer(name="Acme Corp")
    tech = Technician(name="T. Est", epa_cert_number="U-000001")
    appliance = Appliance(
        customer=customer,
        name="RTU-1",
        refrigerant_type="R-410A",
        full_charge_lbs=100.0,
        category=EquipmentCategory.COMFORT_COOLING,
        install_date=date(2025, 1, 1),
    )
    supply = Cylinder(
        serial="SUP-1", refrigerant_type="R-410A", kind=CylinderKind.SUPPLY,
        capacity_lbs=100.0, current_lbs=100.0,
    )
    recovery = Cylinder(
        serial="REC-1", refrigerant_type="MIXED", kind=CylinderKind.RECOVERY,
        capacity_lbs=100.0, current_lbs=0.0,
    )
    db.add_all([customer, tech, appliance, supply, recovery])
    db.commit()
    return {
        "db": db, "customer": customer, "tech": tech,
        "appliance": appliance, "supply": supply, "recovery": recovery,
    }
