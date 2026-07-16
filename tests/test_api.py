"""API integration tests via FastAPI TestClient."""

from fastapi.testclient import TestClient

from reftrack.main import app


def _client(db):  # db fixture ensures a fresh schema per test
    return TestClient(app)


def _setup_world(c):
    cust = c.post("/api/customers", json={"name": "Acme"}).json()
    tech = c.post(
        "/api/technicians", json={"name": "T", "epa_cert_number": "U-1"}
    ).json()
    app_ = c.post("/api/appliances", json={
        "customer_id": cust["id"], "name": "RTU-1",
        "refrigerant_type": "R-410A", "full_charge_lbs": 100.0,
        "category": "comfort_cooling", "install_date": "2025-01-01",
    }).json()
    cyl = c.post("/api/cylinders", json={
        "serial": "SUP-9", "refrigerant_type": "R-410A", "kind": "supply",
        "capacity_lbs": 100.0, "current_lbs": 100.0,
    }).json()
    return cust, tech, app_, cyl


def test_health(db):
    c = _client(db)
    assert c.get("/health").json()["status"] == "ok"


def test_crud_and_charge_flow(db):
    c = _client(db)
    _, tech, app_, cyl = _setup_world(c)

    assert app_["tracked"] is True

    r = c.post("/api/events/charge", json={
        "appliance_id": app_["id"], "technician_id": tech["id"],
        "cylinder_id": cyl["id"], "event_date": "2025-03-15", "pounds": 5.0,
    })
    assert r.status_code == 201
    ev = r.json()
    assert ev["leak_rate_pct"] == 25.0
    assert ev["threshold_exceeded"] is True

    cases = c.get("/api/cases").json()
    assert len(cases) == 1
    assert cases[0]["status"] == "open"
    assert cases[0]["due_date"] == "2025-04-14"

    cyls = c.get("/api/cylinders").json()
    assert cyls[0]["current_lbs"] == 95.0

    events = c.get(f"/api/appliances/{app_['id']}/events").json()
    assert len(events) == 1


def test_domain_errors_are_422(db):
    c = _client(db)
    _, tech, app_, cyl = _setup_world(c)
    r = c.post("/api/events/charge", json={
        "appliance_id": app_["id"], "technician_id": tech["id"],
        "cylinder_id": cyl["id"], "event_date": "2024-12-01", "pounds": 5.0,
    })
    assert r.status_code == 422
    assert "precedes installation" in r.json()["detail"]


def test_missing_refs_are_404(db):
    c = _client(db)
    r = c.post("/api/events/charge", json={
        "appliance_id": 999, "technician_id": 1, "cylinder_id": 1,
        "event_date": "2025-01-01", "pounds": 1.0,
    })
    assert r.status_code == 404


def test_report_endpoints(db):
    c = _client(db)
    _, tech, app_, cyl = _setup_world(c)
    c.post("/api/events/charge", json={
        "appliance_id": app_["id"], "technician_id": tech["id"],
        "cylinder_id": cyl["id"], "event_date": "2025-03-15", "pounds": 5.0,
    })

    r = c.get(f"/api/reports/appliance/{app_['id']}.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "25.00" in r.text

    r = c.get(f"/api/reports/appliance/{app_['id']}.pdf")
    assert r.status_code == 200
    assert r.content[:5] == b"%PDF-"

    r = c.get("/api/reports/cylinders.csv")
    assert "SUP-9" in r.text

    r = c.get("/api/compliance/summary")
    body = r.json()
    assert body["total"] == 1
    assert body["appliances"][0]["status"] in ("action_required", "overdue")

    assert c.get("/api/reports/appliance/999.csv").status_code == 404


def test_duplicate_cylinder_serial_409(db):
    c = _client(db)
    body = {"serial": "DUP-1", "refrigerant_type": "R-22", "kind": "supply",
            "capacity_lbs": 50.0, "current_lbs": 0.0}
    assert c.post("/api/cylinders", json=body).status_code == 201
    assert c.post("/api/cylinders", json=body).status_code == 409
