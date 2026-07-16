"""v1.1 features: void/amend, verification reopening, plans, chronic leakers,
backups, auth, alerts, shop profile."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from reftrack import alerts, auth, compliance, reports, service
from reftrack.main import app
from reftrack.models import CaseStatus, ComplianceCase, EventType


def _charge(world, day, lbs):
    return service.log_charge_addition(
        world["db"], appliance=world["appliance"], technician=world["tech"],
        cylinder=world["supply"], event_date=day, pounds=lbs,
    )


def _maint(world, day, etype, **kw):
    return service.log_maintenance_event(
        world["db"], appliance=world["appliance"], technician=world["tech"],
        event_date=day, event_type=etype, **kw,
    )


# ---- Void / amend -------------------------------------------------------------

def test_void_charge_restores_inventory_and_voids_case(world):
    db = world["db"]
    ev = _charge(world, date(2025, 3, 15), 5.0)  # 25% -> case opens
    assert world["supply"].current_lbs == 95.0

    service.void_event(db, event=ev, reason="fat-fingered, was 0.5 lbs")
    assert world["supply"].current_lbs == 100.0
    assert ev.voided is True
    case = db.query(ComplianceCase).one()
    assert case.status == CaseStatus.VOIDED

    st = compliance.appliance_status(db, world["appliance"], date(2025, 4, 1))
    assert st.current_leak_rate_pct is None
    assert st.status == compliance.Status.OK


def test_void_requires_reason(world):
    ev = _charge(world, date(2025, 3, 15), 5.0)
    with pytest.raises(service.DomainError, match="reason"):
        service.void_event(world["db"], event=ev, reason="  ")


def test_void_recomputes_downstream_rates(world):
    db = world["db"]
    ev1 = _charge(world, date(2025, 3, 15), 5.0)   # 73 days -> 25%
    ev2 = _charge(world, date(2025, 6, 1), 3.0)    # 78 days after ev1 -> 14.04%
    service.void_event(db, event=ev1, reason="wrong appliance")
    db.refresh(ev2)
    # ev2's baseline falls back to install date: 151 days -> (3/100)*(365/151)*100
    assert ev2.leak_rate_pct == pytest.approx(7.25, abs=0.01)
    assert ev2.threshold_exceeded is False


def test_void_repair_reopens_case(world):
    db = world["db"]
    _charge(world, date(2025, 3, 15), 5.0)
    repair = _maint(world, date(2025, 3, 20), EventType.LEAK_REPAIR)
    case = db.query(ComplianceCase).one()
    assert case.status == CaseStatus.REPAIRED

    service.void_event(db, event=repair, reason="repair was on a different unit")
    db.refresh(case)
    assert case.status == CaseStatus.OPEN
    assert case.resolved_date is None


def test_void_retirement_reactivates_appliance(world):
    db = world["db"]
    retire = _maint(world, date(2025, 2, 1), EventType.RETIRED)
    assert world["appliance"].active is False
    service.void_event(db, event=retire, reason="clerical error")
    assert world["appliance"].active is True


def test_void_does_not_create_spurious_case_for_covered_event(world):
    db = world["db"]
    _charge(world, date(2025, 3, 15), 5.0)   # opens case
    _charge(world, date(2025, 3, 20), 4.0)   # exceeds, covered by open case
    _maint(world, date(2025, 3, 25), EventType.LEAK_REPAIR)
    rec = service.log_recovery(
        db, appliance=world["appliance"], technician=world["tech"],
        cylinder=world["recovery"], event_date=date(2025, 4, 1), pounds=5.0,
    )
    service.void_event(db, event=rec, reason="never happened")
    # Recompute must not open a second case for the 3/20 exceedance.
    assert db.query(ComplianceCase).count() == 1


def test_void_already_voided_rejected(world):
    ev = _charge(world, date(2025, 3, 15), 5.0)
    service.void_event(world["db"], event=ev, reason="mistake")
    with pytest.raises(service.DomainError, match="already"):
        service.void_event(world["db"], event=ev, reason="again")


# ---- Failed verification reopens obligation ------------------------------------

def test_failed_followup_verification_opens_new_case(world):
    db = world["db"]
    _charge(world, date(2025, 3, 15), 5.0)
    _maint(world, date(2025, 3, 20), EventType.LEAK_REPAIR)
    _maint(world, date(2025, 4, 5), EventType.VERIFICATION_FOLLOWUP, passed=False)

    cases = db.query(ComplianceCase).order_by(ComplianceCase.id).all()
    assert len(cases) == 2
    assert cases[0].status == CaseStatus.REPAIRED
    assert cases[1].status == CaseStatus.OPEN
    assert cases[1].due_date == date(2025, 5, 5)
    assert cases[1].leak_rate_pct == cases[0].leak_rate_pct


def test_passed_followup_does_not_reopen(world):
    db = world["db"]
    _charge(world, date(2025, 3, 15), 5.0)
    _maint(world, date(2025, 3, 20), EventType.LEAK_REPAIR)
    _maint(world, date(2025, 4, 5), EventType.VERIFICATION_FOLLOWUP, passed=True)
    assert db.query(ComplianceCase).count() == 1


def test_passed_flag_rejected_on_non_verification(world):
    with pytest.raises(service.DomainError, match="verification"):
        _maint(world, date(2025, 3, 20), EventType.LEAK_REPAIR, passed=True)


# ---- Retrofit/retirement plan ---------------------------------------------------

def test_record_plan_on_open_case(world):
    db = world["db"]
    _charge(world, date(2025, 3, 15), 5.0)
    case = db.query(ComplianceCase).one()
    service.record_plan(db, case=case, plan_date=date(2025, 4, 10),
                        plan_notes="Retrofit to R-454B, PO #221")
    assert case.plan_date == date(2025, 4, 10)

    with pytest.raises(service.DomainError, match="precede"):
        service.record_plan(db, case=case, plan_date=date(2025, 1, 1),
                            plan_notes="x")


def test_plan_rejected_on_resolved_case(world):
    db = world["db"]
    _charge(world, date(2025, 3, 15), 5.0)
    _maint(world, date(2025, 3, 20), EventType.LEAK_REPAIR)
    case = db.query(ComplianceCase).one()
    with pytest.raises(service.DomainError, match="open"):
        service.record_plan(db, case=case, plan_date=date(2025, 4, 1),
                            plan_notes="too late")


# ---- Chronic leakers -------------------------------------------------------------

def test_chronic_leaker_detection_and_csv(world):
    db = world["db"]
    _charge(world, date(2025, 1, 11), 5.0)  # 10 days: 182.5% -> chronic
    events = compliance.chronic_events(db)
    assert len(events) == 1
    text = reports.chronic_csv(db)
    assert "182.50" in text
    assert "RTU-1" in text

    summary = compliance.shop_summary(db, date(2025, 2, 1))
    assert summary["chronic_count"] == 1


# ---- Backups ---------------------------------------------------------------------

def test_backup_db_creates_and_dedupes(world, tmp_path, monkeypatch):
    import reftrack.database as dbmod
    src = tmp_path / "shop.db"
    src.write_bytes(b"fake sqlite bytes")
    monkeypatch.setattr(dbmod, "DB_PATH", str(src))
    first = dbmod.backup_db()
    assert first is not None
    assert (tmp_path / "backups").exists()
    assert dbmod.backup_db() is None  # same day: no duplicate


# ---- Auth ------------------------------------------------------------------------

def test_auth_disabled_by_default(world, monkeypatch):
    monkeypatch.delenv("REFTRACK_PASSWORD", raising=False)
    c = TestClient(app)
    assert c.get("/").status_code == 200


def test_auth_enforced_when_password_set(world, monkeypatch):
    monkeypatch.setenv("REFTRACK_PASSWORD", "s3cret")
    c = TestClient(app, follow_redirects=False)

    assert c.get("/").status_code == 303           # UI -> redirect to login
    assert c.get("/api/customers").status_code == 401
    assert c.get("/health").status_code == 200     # exempt

    r = c.post("/login", data={"password": "wrong"})
    assert r.status_code == 401

    r = c.post("/login", data={"password": "s3cret"})
    assert r.status_code == 303
    assert auth.COOKIE_NAME in r.cookies

    c2 = TestClient(app)
    c2.cookies.set(auth.COOKIE_NAME, r.cookies[auth.COOKIE_NAME])
    assert c2.get("/").status_code == 200
    assert c2.get("/api/customers").status_code == 200


# ---- Alerts ----------------------------------------------------------------------

def test_alerts_disabled_without_env(monkeypatch):
    for k in ("BREVO_API_KEY", "REFTRACK_ALERT_TO", "REFTRACK_ALERT_FROM"):
        monkeypatch.delenv(k, raising=False)
    assert alerts.enabled() is False
    assert alerts.send_case_alert(
        appliance_name="X", customer_name="Y", leak_rate_pct=50.0,
        threshold_pct=10.0, due_date="2025-01-01",
    ) is False


def _enable_alerts(monkeypatch):
    monkeypatch.setenv("BREVO_API_KEY", "key-123")
    monkeypatch.setenv("REFTRACK_ALERT_TO", "boss@shop.example")
    monkeypatch.setenv("REFTRACK_ALERT_FROM", "noreply@shop.example")


def test_alert_dispatched_on_exceedance(world, monkeypatch):
    _enable_alerts(monkeypatch)
    sent = {}

    class _Resp:
        def raise_for_status(self): pass

    def _fake_post(url, json, headers, timeout):
        sent["url"] = url
        sent["subject"] = json["subject"]
        sent["key"] = headers["api-key"]
        return _Resp()

    monkeypatch.setattr(alerts.httpx, "post", _fake_post)
    _charge(world, date(2025, 3, 15), 5.0)  # 25% -> exceeds
    alerts._last_thread.join(timeout=5)

    assert sent["key"] == "key-123"
    assert "RTU-1" in sent["subject"]
    assert "25.0" in sent["subject"]


def test_alert_failure_never_breaks_recording(world, monkeypatch):
    """Brevo being down must not lose the compliance record."""
    _enable_alerts(monkeypatch)

    def _boom(*a, **kw):
        raise RuntimeError("brevo is down")

    monkeypatch.setattr(alerts.httpx, "post", _boom)
    ev = _charge(world, date(2025, 3, 15), 5.0)
    if alerts._last_thread:
        alerts._last_thread.join(timeout=5)

    # The event and its case are saved regardless.
    assert ev.leak_rate_pct == 25.0
    assert world["db"].query(ComplianceCase).count() == 1


def test_alert_not_sent_when_under_threshold(world, monkeypatch):
    _enable_alerts(monkeypatch)
    calls = []
    monkeypatch.setattr(alerts.httpx, "post",
                        lambda *a, **kw: calls.append(1))
    _charge(world, date(2026, 1, 1), 5.0)  # 5% -> no exceedance
    assert calls == []


# ---- Shop profile ----------------------------------------------------------------

def test_shop_profile_api_and_ui_form(world):
    c = TestClient(app)
    assert c.get("/api/shop").json()["name"] == "My HVAC Shop"
    r = c.put("/api/shop", json={
        "name": "Frost Bros HVAC", "address": "1 Cold St",
        "phone": "555-0100", "epa_contact": "J. Frost",
    })
    assert r.status_code == 200
    assert c.get("/api/shop").json()["name"] == "Frost Bros HVAC"

    r = c.get("/registry")
    assert "Frost Bros HVAC" in r.text


# ---- Void via API + UI ------------------------------------------------------------

def test_void_via_api_and_ui(world):
    c = TestClient(app)
    ev = _charge(world, date(2025, 3, 15), 5.0)

    r = c.post(f"/api/events/{ev.id}/void", json={"reason": ""})
    assert r.status_code == 422  # pydantic min_length

    r = c.post(f"/api/events/{ev.id}/void", json={"reason": "entry error"})
    assert r.status_code == 200
    assert r.json()["voided"] is True

    ev2 = _charge(world, date(2025, 5, 1), 5.0)
    r = c.post(
        f"/appliances/{world['appliance'].id}/events/{ev2.id}/void",
        headers={"HX-Prompt": "wrong unit"},
    )
    assert r.status_code == 200
    assert "voided" in r.text.lower()
