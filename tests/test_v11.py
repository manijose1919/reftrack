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


def test_void_leaves_no_compliance_hole(world):
    """Voiding a case's opener must not orphan a still-real exceedance.

    A (3/15, 25%) opens case 1. B (3/20) also exceeds but is covered by that
    open case. Repair closes case 1. Voiding A voids case 1 -- but B STILL
    exceeds on recompute, so it must get its own case rather than silently
    losing its 30-day obligation.
    """
    db = world["db"]
    a = _charge(world, date(2025, 3, 15), 5.0)
    b = _charge(world, date(2025, 3, 20), 8.0)
    assert b.threshold_exceeded is True
    _maint(world, date(2025, 3, 25), EventType.LEAK_REPAIR)
    assert db.query(ComplianceCase).count() == 1

    service.void_event(db, event=a, reason="logged against the wrong unit")
    db.refresh(b)

    # B re-annualizes from the install date: (8/100)*(365/78)*100 = 37.44%
    assert b.leak_rate_pct == pytest.approx(37.44, abs=0.01)
    assert b.threshold_exceeded is True

    cases = db.query(ComplianceCase).order_by(ComplianceCase.id).all()
    assert cases[0].status == CaseStatus.VOIDED       # opener was voided
    live = [c for c in cases if c.status == CaseStatus.OPEN]
    assert len(live) == 1, "real exceedance must not lose its case"
    assert live[0].opened_event_id == b.id
    assert live[0].due_date == date(2025, 4, 19)      # 3/20 + 30 days

    st = compliance.appliance_status(db, world["appliance"], date(2025, 4, 1))
    assert st.status == compliance.Status.ACTION_REQUIRED


def test_voiding_earlier_addition_can_clear_exceedance(world):
    """Voiding an earlier addition pushes the baseline back, LOWERING the
    downstream rate -- an exceedance can legitimately disappear."""
    db = world["db"]
    a = _charge(world, date(2025, 3, 15), 5.0)
    b = _charge(world, date(2025, 3, 20), 1.0)  # 5 days -> 73% -> exceeds
    assert b.threshold_exceeded is True

    service.void_event(db, event=a, reason="duplicate")
    db.refresh(b)
    # Baseline -> install: (1/100)*(365/78)*100 = 4.68% -> no longer exceeds
    assert b.threshold_exceeded is False
    assert db.query(ComplianceCase).filter(
        ComplianceCase.status == CaseStatus.OPEN
    ).count() == 0


def test_voiding_a_repair_cannot_create_duplicate_open_cases(world):
    """An appliance must never have two live repair obligations at once.

    A exceeds -> case 1. Repair R closes it. B exceeds -> case 2 opens. Voiding
    R would reopen case 1 alongside the still-open case 2. Two OPEN cases make
    open_case() arbitrary: a later repair closes only one, and the survivor then
    masks every future exceedance, because log_charge_addition only opens a case
    when none is open. That is a silent compliance hole.
    """
    db = world["db"]
    _charge(world, date(2025, 3, 1), 5.0)                      # -> case 1
    repair = _maint(world, date(2025, 3, 5), EventType.LEAK_REPAIR)
    _charge(world, date(2025, 3, 20), 5.0)                     # -> case 2
    assert db.query(ComplianceCase).filter(
        ComplianceCase.status == CaseStatus.OPEN
    ).count() == 1

    service.void_event(db, event=repair, reason="repair was on a different unit")

    open_cases = db.query(ComplianceCase).filter(
        ComplianceCase.status == CaseStatus.OPEN
    ).all()
    assert len(open_cases) == 1, (
        f"expected exactly one live obligation, found {len(open_cases)}"
    )
    # The surviving obligation must be the EARLIEST one: the leak was never
    # actually repaired, so the clock has been running since 3/1 -- the
    # stricter, and correct, deadline.
    assert open_cases[0].opened_date == date(2025, 3, 1)
    assert open_cases[0].due_date == date(2025, 3, 31)


def test_future_exceedance_still_tracked_after_duplicate_reconciliation(world):
    """Follow-on guard: after the reconciliation above, the appliance must
    still behave correctly -- a repair closes the live case, and a genuinely
    new exceedance afterward opens a fresh one."""
    db = world["db"]
    _charge(world, date(2025, 3, 1), 5.0)
    repair = _maint(world, date(2025, 3, 5), EventType.LEAK_REPAIR)
    _charge(world, date(2025, 3, 20), 5.0)
    service.void_event(db, event=repair, reason="wrong unit")

    _maint(world, date(2025, 3, 25), EventType.LEAK_REPAIR)   # real repair
    assert service.open_case(db, world["appliance"]) is None

    _charge(world, date(2025, 4, 1), 5.0)                     # new exceedance
    live = service.open_case(db, world["appliance"])
    assert live is not None, "a new exceedance must open a new case"
    assert live.opened_date == date(2025, 4, 1)


def test_surviving_case_rate_is_not_stale_after_recompute(world):
    """A case that survives a recompute must carry the RECOMPUTED rate.

    case.leak_rate_pct prints onto the audit PDF handed to an inspector; if the
    event says 13.77% and the PDF says 94.19%, the report is wrong.
    """
    db = world["db"]
    ev0 = _charge(world, date(2025, 7, 1), 1.0)    # 2.02%, no exceedance
    ev1 = _charge(world, date(2025, 8, 1), 8.0)    # baseline ev0, 31d -> 94.19%
    assert ev1.leak_rate_pct == pytest.approx(94.19, abs=0.01)
    case = db.query(ComplianceCase).one()
    assert case.leak_rate_pct == pytest.approx(94.19, abs=0.01)

    service.void_event(db, event=ev0, reason="logged against wrong appliance")
    db.refresh(ev1)
    db.refresh(case)

    # ev1 re-annualizes from install: (8/100)*(365/212)*100 = 13.77%, still >10%
    assert ev1.leak_rate_pct == pytest.approx(13.77, abs=0.01)
    assert case.status == CaseStatus.OPEN
    assert case.leak_rate_pct == pytest.approx(13.77, abs=0.01), \
        "case kept the stale pre-void rate"

    pdf = reports.appliance_pdf(db, world["appliance"], date(2025, 8, 15))
    assert pdf[:5] == b"%PDF-"


def test_ui_never_claims_a_repair_clock_that_does_not_exist(world):
    """The panel message must reflect what the service layer actually did."""
    c = TestClient(app)
    a_id = world["appliance"].id

    # Failed follow-up with NO prior exceedance: no obligation is created,
    # and the UI must not pretend one was.
    r = c.post(f"/appliances/{a_id}/events/maintenance", data={
        "technician_id": world["tech"].id, "event_type": "verification_followup",
        "event_date": "2025-03-01", "result": "fail",
    })
    assert r.status_code == 200
    assert world["db"].query(ComplianceCase).count() == 0
    assert "no repair obligation was opened" in r.text
    assert "30-day repair obligation is now open" not in r.text


def test_ui_reports_real_clock_when_one_opens(world):
    c = TestClient(app)
    a_id = world["appliance"].id
    _charge(world, date(2025, 3, 15), 5.0)
    _maint(world, date(2025, 3, 20), EventType.LEAK_REPAIR)

    r = c.post(f"/appliances/{a_id}/events/maintenance", data={
        "technician_id": world["tech"].id, "event_type": "verification_followup",
        "event_date": "2025-04-05", "result": "fail",
    })
    assert "30-day repair obligation is now open, due 2025-05-05" in r.text


def test_retrofit_then_failed_verification_reopens_obligation(world):
    """A retrofit that still leaks reopens the obligation, same as a failed
    repair -- the case it closed has status RETIRED, not REPAIRED."""
    db = world["db"]
    _charge(world, date(2025, 3, 15), 5.0)
    _maint(world, date(2025, 3, 20), EventType.RETROFIT)
    assert db.query(ComplianceCase).one().status == CaseStatus.RETIRED

    _maint(world, date(2025, 4, 5), EventType.VERIFICATION_FOLLOWUP, passed=False)
    live = service.open_case(db, world["appliance"])
    assert live is not None, "failed verification after a retrofit must reopen"
    assert live.due_date == date(2025, 5, 5)


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

def test_reftrack_logs_actually_reach_a_handler(world, capsys, monkeypatch):
    """Operator-critical messages must be visible, not swallowed.

    Under uvicorn only uvicorn's loggers are configured; reftrack's records
    would propagate to a handler-less root and vanish -- silently hiding
    'BACKUP FAILED' and the auth posture.
    """
    import logging

    from reftrack.main import _configure_logging

    reftrack_logger = logging.getLogger("reftrack")
    saved = reftrack_logger.handlers[:]
    reftrack_logger.handlers.clear()
    try:
        monkeypatch.setenv("REFTRACK_PASSWORD", "")
        _configure_logging()
        auth.log_status()
        err = capsys.readouterr().err
        assert "authentication is DISABLED" in err.lower() or \
               "AUTHENTICATION IS DISABLED" in err.upper()
    finally:
        reftrack_logger.handlers[:] = saved


def test_backup_failure_is_logged_loudly(tmp_path, monkeypatch, caplog):
    """A failed backup must never be silent -- these are 3-year records."""
    import logging

    import reftrack.database as dbmod

    src = tmp_path / "shop.db"
    src.write_bytes(b"data")
    monkeypatch.setattr(dbmod, "DB_PATH", str(src))

    def _boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(dbmod.shutil if hasattr(dbmod, "shutil") else __import__("shutil"),
                        "copy2", _boom)
    with caplog.at_level(logging.ERROR, logger="reftrack.database"):
        assert dbmod.backup_db() is None
    assert "BACKUP FAILED" in caplog.text


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


def test_login_rate_limited_and_secure_cookie_flag(world, monkeypatch):
    monkeypatch.setenv("REFTRACK_PASSWORD", "s3cret")
    monkeypatch.setenv("REFTRACK_SECURE_COOKIES", "1")
    auth.reset_login_attempts()
    c = TestClient(app, follow_redirects=False)

    for _ in range(auth.LOGIN_MAX_ATTEMPTS):
        assert c.post("/login", data={"password": "wrong"}).status_code == 401
    assert c.post("/login", data={"password": "wrong"}).status_code == 429
    # Even the correct password is rejected while the window is open.
    assert c.post("/login", data={"password": "s3cret"}).status_code == 429

    auth.reset_login_attempts()
    r = c.post("/login", data={"password": "s3cret"})
    assert r.status_code == 303
    # Starlette exposes cookie flags on the raw Set-Cookie header.
    set_cookie = r.headers.get("set-cookie", "")
    assert "secure" in set_cookie.lower()


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
        sent["html"] = json["htmlContent"]
        sent["key"] = headers["api-key"]
        return _Resp()

    monkeypatch.setattr(alerts.httpx, "post", _fake_post)
    _charge(world, date(2025, 3, 15), 5.0)  # 25% -> exceeds
    alerts._last_thread.join(timeout=5)

    assert sent["key"] == "key-123"
    assert "RTU-1" in sent["subject"]
    assert "25.0" in sent["subject"]


def test_alert_html_escapes_names(monkeypatch):
    _enable_alerts(monkeypatch)
    sent = {}

    class _Resp:
        def raise_for_status(self):
            pass

    def _fake_post(url, json, headers, timeout):
        sent["html"] = json["htmlContent"]
        return _Resp()

    monkeypatch.setattr(alerts.httpx, "post", _fake_post)
    alerts.send_case_alert(
        appliance_name="<rtu>",
        customer_name="A & B",
        leak_rate_pct=50.0,
        threshold_pct=10.0,
        due_date="2025-01-01",
    )
    alerts._last_thread.join(timeout=5)
    assert "&lt;rtu&gt;" in sent["html"]
    assert "A &amp; B" in sent["html"]
    assert "<rtu>" not in sent["html"]


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
