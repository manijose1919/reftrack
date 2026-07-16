"""REST API endpoints (JSON). The htmx UI lives in ui.py; both call service.py."""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from reftrack import compliance, reports, schemas, service
from reftrack.database import get_db
from reftrack.models import (
    Appliance,
    ComplianceCase,
    Customer,
    Cylinder,
    ServiceEvent,
    Technician,
)

router = APIRouter(prefix="/api", tags=["api"])


def _get_or_404(db: Session, model, obj_id: int):
    obj = db.get(model, obj_id)
    if obj is None:
        raise HTTPException(404, f"{model.__name__} {obj_id} not found")
    return obj


# ---- Customers -------------------------------------------------------------

@router.get("/customers", response_model=list[schemas.CustomerOut])
def list_customers(db: Session = Depends(get_db)):
    return db.execute(select(Customer).order_by(Customer.name)).scalars().all()


@router.post("/customers", response_model=schemas.CustomerOut, status_code=201)
def create_customer(payload: schemas.CustomerIn, db: Session = Depends(get_db)):
    obj = Customer(**payload.model_dump())
    db.add(obj)
    db.commit()
    return obj


# ---- Technicians -----------------------------------------------------------

@router.get("/technicians", response_model=list[schemas.TechnicianOut])
def list_technicians(db: Session = Depends(get_db)):
    return db.execute(select(Technician).order_by(Technician.name)).scalars().all()


@router.post("/technicians", response_model=schemas.TechnicianOut, status_code=201)
def create_technician(payload: schemas.TechnicianIn, db: Session = Depends(get_db)):
    obj = Technician(**payload.model_dump())
    db.add(obj)
    db.commit()
    return obj


# ---- Appliances ------------------------------------------------------------

@router.get("/appliances", response_model=list[schemas.ApplianceOut])
def list_appliances(db: Session = Depends(get_db)):
    return db.execute(select(Appliance).order_by(Appliance.name)).scalars().all()


@router.get("/appliances/{appliance_id}", response_model=schemas.ApplianceOut)
def get_appliance(appliance_id: int, db: Session = Depends(get_db)):
    return _get_or_404(db, Appliance, appliance_id)


@router.post("/appliances", response_model=schemas.ApplianceOut, status_code=201)
def create_appliance(payload: schemas.ApplianceIn, db: Session = Depends(get_db)):
    _get_or_404(db, Customer, payload.customer_id)
    obj = Appliance(**payload.model_dump())
    db.add(obj)
    db.commit()
    return obj


@router.get(
    "/appliances/{appliance_id}/events",
    response_model=list[schemas.ServiceEventOut],
)
def appliance_events(appliance_id: int, db: Session = Depends(get_db)):
    _get_or_404(db, Appliance, appliance_id)
    stmt = (
        select(ServiceEvent)
        .where(ServiceEvent.appliance_id == appliance_id)
        .order_by(ServiceEvent.event_date, ServiceEvent.id)
    )
    return db.execute(stmt).scalars().all()


# ---- Cylinders -------------------------------------------------------------

@router.get("/cylinders", response_model=list[schemas.CylinderOut])
def list_cylinders(db: Session = Depends(get_db)):
    return db.execute(select(Cylinder).order_by(Cylinder.serial)).scalars().all()


@router.post("/cylinders", response_model=schemas.CylinderOut, status_code=201)
def create_cylinder(payload: schemas.CylinderIn, db: Session = Depends(get_db)):
    if payload.current_lbs > payload.capacity_lbs:
        raise HTTPException(422, "current_lbs cannot exceed capacity_lbs")
    exists = db.execute(
        select(Cylinder).where(Cylinder.serial == payload.serial)
    ).scalars().first()
    if exists:
        raise HTTPException(409, f"Cylinder serial {payload.serial} already exists")
    obj = Cylinder(**payload.model_dump())
    db.add(obj)
    db.commit()
    return obj


# ---- Service events --------------------------------------------------------

@router.post(
    "/events/charge", response_model=schemas.ServiceEventOut, status_code=201
)
def charge_addition(payload: schemas.RefrigerantMoveIn, db: Session = Depends(get_db)):
    appliance = _get_or_404(db, Appliance, payload.appliance_id)
    technician = _get_or_404(db, Technician, payload.technician_id)
    cylinder = _get_or_404(db, Cylinder, payload.cylinder_id)
    try:
        return service.log_charge_addition(
            db,
            appliance=appliance,
            technician=technician,
            cylinder=cylinder,
            event_date=payload.event_date,
            pounds=payload.pounds,
            notes=payload.notes,
        )
    except service.DomainError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post(
    "/events/recovery", response_model=schemas.ServiceEventOut, status_code=201
)
def recovery(payload: schemas.RefrigerantMoveIn, db: Session = Depends(get_db)):
    appliance = _get_or_404(db, Appliance, payload.appliance_id)
    technician = _get_or_404(db, Technician, payload.technician_id)
    cylinder = _get_or_404(db, Cylinder, payload.cylinder_id)
    try:
        return service.log_recovery(
            db,
            appliance=appliance,
            technician=technician,
            cylinder=cylinder,
            event_date=payload.event_date,
            pounds=payload.pounds,
            notes=payload.notes,
        )
    except service.DomainError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post(
    "/events/maintenance", response_model=schemas.ServiceEventOut, status_code=201
)
def maintenance(payload: schemas.MaintenanceEventIn, db: Session = Depends(get_db)):
    appliance = _get_or_404(db, Appliance, payload.appliance_id)
    technician = _get_or_404(db, Technician, payload.technician_id)
    try:
        return service.log_maintenance_event(
            db,
            appliance=appliance,
            technician=technician,
            event_date=payload.event_date,
            event_type=payload.event_type,
            notes=payload.notes,
        )
    except service.DomainError as exc:
        raise HTTPException(422, str(exc)) from exc


# ---- Compliance cases ------------------------------------------------------

@router.get("/cases", response_model=list[schemas.ComplianceCaseOut])
def list_cases(db: Session = Depends(get_db)):
    stmt = select(ComplianceCase).order_by(ComplianceCase.opened_date.desc())
    return db.execute(stmt).scalars().all()


# ---- Compliance summary & reports -------------------------------------------

@router.get("/compliance/summary")
def compliance_summary(db: Session = Depends(get_db)):
    summary = compliance.shop_summary(db, date.today())
    return {
        "total": summary["total"],
        "counts": {s.value: n for s, n in summary["counts"].items()},
        "appliances": [
            {
                "id": st.appliance.id,
                "name": st.appliance.name,
                "customer": st.appliance.customer.name,
                "status": st.status.value,
                "current_leak_rate_pct": st.current_leak_rate_pct,
                "threshold_pct": st.threshold_pct,
                "days_until_due": st.days_until_due,
            }
            for st in summary["statuses"]
        ],
    }


@router.get("/reports/appliance/{appliance_id}.csv")
def appliance_report_csv(appliance_id: int, db: Session = Depends(get_db)):
    appliance = _get_or_404(db, Appliance, appliance_id)
    content = reports.appliance_csv(db, appliance)
    return Response(
        content,
        media_type="text/csv",
        headers={
            "Content-Disposition":
                f'attachment; filename="appliance_{appliance_id}_history.csv"'
        },
    )


@router.get("/reports/appliance/{appliance_id}.pdf")
def appliance_report_pdf(appliance_id: int, db: Session = Depends(get_db)):
    appliance = _get_or_404(db, Appliance, appliance_id)
    content = reports.appliance_pdf(db, appliance, date.today())
    return Response(
        content,
        media_type="application/pdf",
        headers={
            "Content-Disposition":
                f'inline; filename="appliance_{appliance_id}_compliance.pdf"'
        },
    )


@router.get("/reports/cylinders.csv")
def cylinder_report_csv(db: Session = Depends(get_db)):
    return Response(
        reports.cylinders_csv(db),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="cylinder_inventory.csv"'
        },
    )
