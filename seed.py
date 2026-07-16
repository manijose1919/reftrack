"""Seed RefTrack with realistic demo data for a small HVAC shop.

Run:  python seed.py          (adds demo data if DB is empty)
      python seed.py --reset  (drops and recreates the database first)
"""

import sys
from datetime import date

from reftrack.database import Base, SessionLocal, engine, init_db
from reftrack.models import (
    Appliance,
    Customer,
    Cylinder,
    CylinderKind,
    EquipmentCategory,
    Technician,
)


def seed() -> None:
    init_db()
    db = SessionLocal()
    try:
        if db.query(Customer).count() > 0:
            print("Database already has data - skipping seed. Use --reset to start over.")
            return

        customers = [
            Customer(name="Lakeside Grocery", address="410 Shore Rd", contact="Pat Nguyen 555-0141"),
            Customer(name="Bricktown Office Plaza", address="88 Main St", contact="Facilities 555-0177"),
            Customer(name="Frostline Cold Storage", address="2 Industrial Way", contact="M. Okafor 555-0102"),
        ]
        techs = [
            Technician(name="J. Alvarez", epa_cert_number="U-118842", cert_type="Universal"),
            Technician(name="S. Kim", epa_cert_number="U-220913", cert_type="Universal"),
        ]
        appliances = [
            Appliance(
                customer=customers[0], name="Rack A - Dairy Cases", location="Back room",
                refrigerant_type="R-448A", full_charge_lbs=220.0,
                category=EquipmentCategory.COMMERCIAL_REFRIGERATION,
                install_date=date(2019, 5, 14),
            ),
            Appliance(
                customer=customers[1], name="Rooftop Chiller 1", location="Roof NE",
                refrigerant_type="R-410A", full_charge_lbs=95.0,
                category=EquipmentCategory.COMFORT_COOLING,
                install_date=date(2021, 8, 2),
            ),
            Appliance(
                customer=customers[2], name="Blast Freezer Line", location="Hall C",
                refrigerant_type="R-507A", full_charge_lbs=600.0,
                category=EquipmentCategory.INDUSTRIAL_PROCESS,
                install_date=date(2017, 3, 30),
            ),
            Appliance(
                customer=customers[1], name="Server Room Split", location="2nd floor",
                refrigerant_type="R-410A", full_charge_lbs=12.0,  # below 50 lb: untracked
                category=EquipmentCategory.COMFORT_COOLING,
                install_date=date(2023, 1, 10),
            ),
        ]
        cylinders = [
            Cylinder(serial="SUP-448A-001", refrigerant_type="R-448A",
                     kind=CylinderKind.SUPPLY, capacity_lbs=125.0, current_lbs=125.0),
            Cylinder(serial="SUP-410A-004", refrigerant_type="R-410A",
                     kind=CylinderKind.SUPPLY, capacity_lbs=100.0, current_lbs=100.0),
            Cylinder(serial="SUP-507A-002", refrigerant_type="R-507A",
                     kind=CylinderKind.SUPPLY, capacity_lbs=125.0, current_lbs=125.0),
            Cylinder(serial="REC-MIX-007", refrigerant_type="MIXED",
                     kind=CylinderKind.RECOVERY, capacity_lbs=123.0, current_lbs=0.0),
        ]

        db.add_all(customers + techs + appliances + cylinders)
        db.commit()
        print(f"Seeded {len(customers)} customers, {len(techs)} technicians, "
              f"{len(appliances)} appliances, {len(cylinders)} cylinders.")
    finally:
        db.close()


if __name__ == "__main__":
    if "--reset" in sys.argv:
        from reftrack import models  # noqa: F401

        Base.metadata.drop_all(engine)
        print("Database reset.")
    seed()
