"""Audit-ready report generation: CSV and PDF.

The PDF is structured around what an EPA Section 608 records request asks for:
appliance identity and full charge, every refrigerant addition with the
annualized leak rate at that service, and the leak-repair timeline.
"""

import csv
import io
from datetime import date

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import select
from sqlalchemy.orm import Session

from reftrack.compliance import appliance_status, chronic_events
from reftrack.models import Appliance, ComplianceCase, Cylinder, ServiceEvent
from reftrack.service import get_shop_profile


def _events(db: Session, appliance_id: int) -> list[ServiceEvent]:
    stmt = (
        select(ServiceEvent)
        .where(ServiceEvent.appliance_id == appliance_id)
        .order_by(ServiceEvent.event_date, ServiceEvent.id)
    )
    return list(db.execute(stmt).scalars())


def _cases(db: Session, appliance_id: int) -> list[ComplianceCase]:
    stmt = (
        select(ComplianceCase)
        .where(ComplianceCase.appliance_id == appliance_id)
        .order_by(ComplianceCase.opened_date)
    )
    return list(db.execute(stmt).scalars())


# ---- CSV --------------------------------------------------------------------

def appliance_csv(db: Session, appliance: Appliance) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([
        "date", "event_type", "technician", "epa_cert", "pounds",
        "cylinder_serial", "annualized_leak_rate_pct", "threshold_exceeded",
        "voided", "void_reason", "notes",
    ])
    for ev in _events(db, appliance.id):
        w.writerow([
            ev.event_date.isoformat(),
            ev.event_type.value,
            ev.technician.name,
            ev.technician.epa_cert_number,
            f"{ev.pounds:.2f}" if ev.pounds is not None else "",
            ev.cylinder.serial if ev.cylinder else "",
            f"{ev.leak_rate_pct:.2f}" if ev.leak_rate_pct is not None else "",
            "yes" if ev.threshold_exceeded else "no",
            "VOID" if ev.voided else "",
            ev.void_reason,
            ev.notes,
        ])
    return buf.getvalue()


def chronic_csv(db: Session) -> str:
    """Additions above 125% annualized on 50+ lb appliances (chronically
    leaking appliance reporting, 40 CFR 82.157(j))."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([
        "appliance", "customer", "refrigerant", "full_charge_lbs",
        "event_date", "pounds_added", "annualized_leak_rate_pct",
    ])
    for ev in chronic_events(db):
        a = ev.appliance
        w.writerow([
            a.name, a.customer.name, a.refrigerant_type,
            f"{a.full_charge_lbs:.1f}", ev.event_date.isoformat(),
            f"{ev.pounds:.2f}" if ev.pounds is not None else "",
            f"{ev.leak_rate_pct:.2f}",
        ])
    return buf.getvalue()


def cylinders_csv(db: Session) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([
        "serial", "kind", "refrigerant_type", "capacity_lbs", "current_lbs",
        "pct_full",
    ])
    for cyl in db.execute(select(Cylinder).order_by(Cylinder.serial)).scalars():
        pct = (cyl.current_lbs / cyl.capacity_lbs * 100.0) if cyl.capacity_lbs else 0.0
        w.writerow([
            cyl.serial, cyl.kind.value, cyl.refrigerant_type,
            f"{cyl.capacity_lbs:.1f}", f"{cyl.current_lbs:.1f}", f"{pct:.0f}",
        ])
    return buf.getvalue()


# ---- PDF --------------------------------------------------------------------

_GRID = TableStyle([
    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef4")),
    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ("FONTSIZE", (0, 0), (-1, -1), 8),
    ("VALIGN", (0, 0), (-1, -1), "TOP"),
])


def appliance_pdf(db: Session, appliance: Appliance, today: date) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch,
        title=f"RefTrack Compliance Report - {appliance.name}",
    )
    styles = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8)
    story = []

    st = appliance_status(db, appliance, today)
    shop = get_shop_profile(db)

    story.append(Paragraph("Refrigerant Compliance Report", styles["Title"]))
    shop_line = shop.name
    if shop.address:
        shop_line += f" &middot; {shop.address}"
    if shop.phone:
        shop_line += f" &middot; {shop.phone}"
    if shop.epa_contact:
        shop_line += f" &middot; EPA contact: {shop.epa_contact}"
    story.append(Paragraph(shop_line, styles["Normal"]))
    story.append(Paragraph(
        "EPA Section 608 (40 CFR Part 82, Subpart F) service and leak-rate "
        f"record &mdash; generated {today.isoformat()}", small))
    story.append(Spacer(1, 12))

    ident = Table([
        ["Appliance", appliance.name, "Customer", appliance.customer.name],
        ["Location", appliance.location or "-", "Refrigerant", appliance.refrigerant_type],
        ["Full charge (lbs)", f"{appliance.full_charge_lbs:.1f}",
         "Category", appliance.category.label],
        ["Installed", appliance.install_date.isoformat(),
         "Leak threshold", f"{st.threshold_pct:.0f}%"],
        ["Tracking required", "Yes (>= 50 lbs)" if appliance.tracked else "No (< 50 lbs)",
         "Current status", st.status.label],
    ], colWidths=[1.3 * inch, 2.2 * inch, 1.3 * inch, 2.2 * inch])
    ident.setStyle(_GRID)
    story.append(ident)
    story.append(Spacer(1, 16))

    story.append(Paragraph("Service History", styles["Heading2"]))
    rows = [["Date", "Event", "Technician (EPA cert)", "Lbs",
             "Leak rate", "Exceeded", "Notes"]]
    for ev in _events(db, appliance.id):
        note = ev.notes or ""
        if ev.voided:
            note = f"VOIDED: {ev.void_reason}. {note}".strip()
        rows.append([
            ev.event_date.isoformat(),
            ("VOID - " if ev.voided else "") + ev.event_type.label,
            f"{ev.technician.name} ({ev.technician.epa_cert_number})",
            f"{ev.pounds:.1f}" if ev.pounds is not None else "-",
            f"{ev.leak_rate_pct:.2f}%" if ev.leak_rate_pct is not None else "-",
            "YES" if ev.threshold_exceeded and not ev.voided else "",
            Paragraph(note, small),
        ])
    if len(rows) == 1:
        rows.append(["-", "No service events on record", "", "", "", "", ""])
    hist = Table(rows, colWidths=[0.75 * inch, 1.0 * inch, 1.7 * inch,
                                  0.45 * inch, 0.7 * inch, 0.65 * inch, 1.75 * inch],
                 repeatRows=1)
    hist.setStyle(_GRID)
    story.append(hist)
    story.append(Spacer(1, 16))

    story.append(Paragraph("Leak Exceedance &amp; Repair Timeline", styles["Heading2"]))
    case_rows = [["Opened", "Leak rate", "Repair due", "Status", "Resolved",
                  "Retrofit/retirement plan"]]
    for case in _cases(db, appliance.id):
        plan = "-"
        if case.plan_date:
            plan = f"{case.plan_date.isoformat()}: {case.plan_notes}"
        case_rows.append([
            case.opened_date.isoformat(),
            f"{case.leak_rate_pct:.2f}%",
            case.due_date.isoformat(),
            ("OVERDUE" if case.is_overdue(today) else case.status.value.title()),
            case.resolved_date.isoformat() if case.resolved_date else "-",
            Paragraph(plan, small),
        ])
    if len(case_rows) == 1:
        case_rows.append(["-", "No threshold exceedances on record", "", "", "", ""])
    cases_tbl = Table(case_rows, colWidths=[0.85 * inch, 0.85 * inch, 0.85 * inch,
                                            0.9 * inch, 0.85 * inch, 2.7 * inch],
                      repeatRows=1)
    cases_tbl.setStyle(_GRID)
    story.append(cases_tbl)
    story.append(Spacer(1, 28))

    story.append(Paragraph(
        "Certified by: ______________________________&nbsp;&nbsp;&nbsp;"
        "Date: ______________", styles["Normal"]))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        "Records generated by RefTrack. Retain for a minimum of 3 years per "
        "40 CFR 82.157.", small))

    doc.build(story)
    return buf.getvalue()
