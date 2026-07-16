"""Server-rendered UI (Jinja2 + htmx). All business rules live in service.py."""

from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from reftrack import auth, compliance, service
from reftrack.database import get_db
from reftrack.models import (
    Appliance,
    ComplianceCase,
    Customer,
    Cylinder,
    CylinderKind,
    EquipmentCategory,
    EventType,
    ServiceEvent,
    Technician,
)

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["auth_enabled"] = auth.enabled

MAINTENANCE_TYPES = [
    EventType.LEAK_REPAIR,
    EventType.VERIFICATION_INITIAL,
    EventType.VERIFICATION_FOLLOWUP,
    EventType.RETROFIT,
    EventType.RETIRED,
]


def _appliance_or_404(db: Session, appliance_id: int) -> Appliance:
    obj = db.get(Appliance, appliance_id)
    if obj is None:
        raise HTTPException(404, "Appliance not found")
    return obj


def _panel_ctx(
    db: Session,
    appliance: Appliance,
    *,
    error: str | None = None,
    message: str | None = None,
) -> dict:
    events = db.execute(
        select(ServiceEvent)
        .where(ServiceEvent.appliance_id == appliance.id)
        .order_by(ServiceEvent.event_date.desc(), ServiceEvent.id.desc())
    ).scalars().all()
    technicians = db.execute(
        select(Technician).order_by(Technician.name)
    ).scalars().all()
    supply = db.execute(
        select(Cylinder).where(
            Cylinder.kind == CylinderKind.SUPPLY,
            Cylinder.refrigerant_type == appliance.refrigerant_type,
        ).order_by(Cylinder.serial)
    ).scalars().all()
    recovery = db.execute(
        select(Cylinder).where(Cylinder.kind == CylinderKind.RECOVERY)
        .order_by(Cylinder.serial)
    ).scalars().all()
    return {
        "appliance": appliance,
        "st": compliance.appliance_status(db, appliance, date.today()),
        "events": events,
        "technicians": technicians,
        "supply_cylinders": supply,
        "recovery_cylinders": recovery,
        "maintenance_types": MAINTENANCE_TYPES,
        "today": date.today().isoformat(),
        "error": error,
        "message": message,
    }


# ---- Pages -------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    summary = compliance.shop_summary(db, date.today())
    return templates.TemplateResponse(request, "dashboard.html", {
        "statuses": summary["statuses"],
        "counts": summary["counts"],
        "chronic_count": summary["chronic_count"],
    })


@router.get("/appliances/{appliance_id}", response_class=HTMLResponse)
def appliance_detail(
    request: Request, appliance_id: int, db: Session = Depends(get_db)
):
    appliance = _appliance_or_404(db, appliance_id)
    return templates.TemplateResponse(
        request, "appliance_detail.html", _panel_ctx(db, appliance)
    )


def _registry_ctx(db: Session, *, error: str | None = None,
                  message: str | None = None) -> dict:
    return {
        "customers": db.execute(select(Customer).order_by(Customer.name)).scalars().all(),
        "technicians": db.execute(select(Technician).order_by(Technician.name)).scalars().all(),
        "cylinders": db.execute(select(Cylinder).order_by(Cylinder.serial)).scalars().all(),
        "shop": service.get_shop_profile(db),
        "error": error,
        "message": message,
    }


@router.post("/registry/shop")
def ui_update_shop(
    name: str = Form(...), address: str = Form(""), phone: str = Form(""),
    epa_contact: str = Form(""), db: Session = Depends(get_db),
):
    profile = service.get_shop_profile(db)
    profile.name = name.strip() or profile.name
    profile.address = address.strip()
    profile.phone = phone.strip()
    profile.epa_contact = epa_contact.strip()
    db.commit()
    return RedirectResponse("/registry", status_code=303)


@router.get("/registry", response_class=HTMLResponse)
def registry(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "registry.html", _registry_ctx(db))


# ---- Event logging (htmx partials) --------------------------------------------

def _panel_response(request: Request, db: Session, appliance: Appliance,
                    *, error: str | None = None, message: str | None = None):
    return templates.TemplateResponse(
        request, "_appliance_panel.html",
        _panel_ctx(db, appliance, error=error, message=message),
    )


@router.post("/appliances/{appliance_id}/events/charge", response_class=HTMLResponse)
def ui_charge(
    request: Request,
    appliance_id: int,
    technician_id: int = Form(...),
    cylinder_id: int = Form(...),
    event_date: date = Form(...),
    pounds: float = Form(...),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    appliance = _appliance_or_404(db, appliance_id)
    technician = db.get(Technician, technician_id)
    cylinder = db.get(Cylinder, cylinder_id)
    if technician is None or cylinder is None:
        return _panel_response(request, db, appliance,
                               error="Unknown technician or cylinder.")
    try:
        ev = service.log_charge_addition(
            db, appliance=appliance, technician=technician, cylinder=cylinder,
            event_date=event_date, pounds=pounds, notes=notes,
        )
    except service.DomainError as exc:
        return _panel_response(request, db, appliance, error=str(exc))
    msg = f"Logged {pounds:.1f} lbs added."
    if ev.leak_rate_pct is not None:
        msg += f" Annualized leak rate: {ev.leak_rate_pct:.2f}%."
    if ev.threshold_exceeded:
        msg += " THRESHOLD EXCEEDED — 30-day repair clock started."
    return _panel_response(request, db, appliance, message=msg)


@router.post("/appliances/{appliance_id}/events/recovery", response_class=HTMLResponse)
def ui_recovery(
    request: Request,
    appliance_id: int,
    technician_id: int = Form(...),
    cylinder_id: int = Form(...),
    event_date: date = Form(...),
    pounds: float = Form(...),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    appliance = _appliance_or_404(db, appliance_id)
    technician = db.get(Technician, technician_id)
    cylinder = db.get(Cylinder, cylinder_id)
    if technician is None or cylinder is None:
        return _panel_response(request, db, appliance,
                               error="Unknown technician or cylinder.")
    try:
        service.log_recovery(
            db, appliance=appliance, technician=technician, cylinder=cylinder,
            event_date=event_date, pounds=pounds, notes=notes,
        )
    except service.DomainError as exc:
        return _panel_response(request, db, appliance, error=str(exc))
    return _panel_response(request, db, appliance,
                           message=f"Logged {pounds:.1f} lbs recovered.")


@router.post("/appliances/{appliance_id}/events/maintenance",
             response_class=HTMLResponse)
def ui_maintenance(
    request: Request,
    appliance_id: int,
    technician_id: int = Form(...),
    event_type: EventType = Form(...),
    event_date: date = Form(...),
    notes: str = Form(""),
    result: str = Form(""),  # "", "pass", or "fail" (verifications only)
    db: Session = Depends(get_db),
):
    appliance = _appliance_or_404(db, appliance_id)
    technician = db.get(Technician, technician_id)
    if technician is None:
        return _panel_response(request, db, appliance, error="Unknown technician.")
    passed = {"pass": True, "fail": False}.get(result)
    try:
        service.log_maintenance_event(
            db, appliance=appliance, technician=technician,
            event_date=event_date, event_type=event_type, notes=notes,
            passed=passed,
        )
    except service.DomainError as exc:
        return _panel_response(request, db, appliance, error=str(exc))
    msg = f"Logged: {event_type.label}."
    if event_type == EventType.VERIFICATION_FOLLOWUP and passed is False:
        msg += " Failed verification — a new 30-day repair case was opened."
    return _panel_response(request, db, appliance, message=msg)


@router.post("/appliances/{appliance_id}/events/{event_id}/void",
             response_class=HTMLResponse)
def ui_void_event(
    request: Request,
    appliance_id: int,
    event_id: int,
    db: Session = Depends(get_db),
):
    appliance = _appliance_or_404(db, appliance_id)
    event = db.get(ServiceEvent, event_id)
    if event is None or event.appliance_id != appliance.id:
        return _panel_response(request, db, appliance, error="Unknown event.")
    reason = request.headers.get("HX-Prompt", "").strip()
    try:
        service.void_event(db, event=event, reason=reason)
    except service.DomainError as exc:
        return _panel_response(request, db, appliance, error=str(exc))
    return _panel_response(
        request, db, appliance,
        message="Event voided; inventory restored and leak rates recomputed.",
    )


@router.post("/appliances/{appliance_id}/cases/{case_id}/plan",
             response_class=HTMLResponse)
def ui_record_plan(
    request: Request,
    appliance_id: int,
    case_id: int,
    plan_date: date = Form(...),
    plan_notes: str = Form(...),
    db: Session = Depends(get_db),
):
    appliance = _appliance_or_404(db, appliance_id)
    case = db.get(ComplianceCase, case_id)
    if case is None or case.appliance_id != appliance.id:
        return _panel_response(request, db, appliance, error="Unknown case.")
    try:
        service.record_plan(db, case=case, plan_date=plan_date,
                            plan_notes=plan_notes)
    except service.DomainError as exc:
        return _panel_response(request, db, appliance, error=str(exc))
    return _panel_response(request, db, appliance,
                           message="Retrofit/retirement plan recorded.")


# ---- Registry creation (plain form posts) --------------------------------------

@router.post("/registry/customers")
def ui_add_customer(
    name: str = Form(...), address: str = Form(""), contact: str = Form(""),
    db: Session = Depends(get_db),
):
    db.add(Customer(name=name.strip(), address=address.strip(),
                    contact=contact.strip()))
    db.commit()
    return RedirectResponse("/registry", status_code=303)


@router.post("/registry/technicians")
def ui_add_technician(
    name: str = Form(...), epa_cert_number: str = Form(...),
    cert_type: str = Form("Universal"), db: Session = Depends(get_db),
):
    db.add(Technician(name=name.strip(), epa_cert_number=epa_cert_number.strip(),
                      cert_type=cert_type))
    db.commit()
    return RedirectResponse("/registry", status_code=303)


@router.post("/registry/cylinders", response_class=HTMLResponse)
def ui_add_cylinder(
    request: Request,
    serial: str = Form(...), refrigerant_type: str = Form(...),
    kind: CylinderKind = Form(...), capacity_lbs: float = Form(...),
    current_lbs: float = Form(0.0), db: Session = Depends(get_db),
):
    serial = serial.strip()
    exists = db.execute(
        select(Cylinder).where(Cylinder.serial == serial)
    ).scalars().first()
    if exists:
        return templates.TemplateResponse(
            request, "registry.html",
            _registry_ctx(db, error=f"Cylinder serial {serial} already exists."),
            status_code=409,
        )
    if capacity_lbs <= 0 or current_lbs < 0 or current_lbs > capacity_lbs:
        return templates.TemplateResponse(
            request, "registry.html",
            _registry_ctx(db, error="Invalid capacity/current pounds."),
            status_code=422,
        )
    db.add(Cylinder(serial=serial, refrigerant_type=refrigerant_type.strip(),
                    kind=kind, capacity_lbs=capacity_lbs, current_lbs=current_lbs))
    db.commit()
    return RedirectResponse("/registry", status_code=303)


@router.post("/registry/appliances", response_class=HTMLResponse)
def ui_add_appliance(
    request: Request,
    customer_id: int = Form(...), name: str = Form(...),
    location: str = Form(""), refrigerant_type: str = Form(...),
    full_charge_lbs: float = Form(...), category: EquipmentCategory = Form(...),
    install_date: date = Form(...), db: Session = Depends(get_db),
):
    if db.get(Customer, customer_id) is None:
        return templates.TemplateResponse(
            request, "registry.html",
            _registry_ctx(db, error="Unknown customer."), status_code=422,
        )
    if full_charge_lbs <= 0:
        return templates.TemplateResponse(
            request, "registry.html",
            _registry_ctx(db, error="Full charge must be positive."),
            status_code=422,
        )
    db.add(Appliance(
        customer_id=customer_id, name=name.strip(), location=location.strip(),
        refrigerant_type=refrigerant_type.strip(),
        full_charge_lbs=full_charge_lbs, category=category,
        install_date=install_date,
    ))
    db.commit()
    return RedirectResponse("/registry", status_code=303)
