"""UI route tests: pages render, htmx event forms work, errors surface inline."""

from fastapi.testclient import TestClient

from reftrack.main import app


def _client(world):
    return TestClient(app)


def test_dashboard_renders(world):
    c = _client(world)
    r = c.get("/")
    assert r.status_code == 200
    assert "Compliance Dashboard" in r.text
    assert "RTU-1" in r.text


def test_registry_renders_and_creates(world):
    c = _client(world)
    r = c.get("/registry")
    assert r.status_code == 200
    assert "SUP-1" in r.text

    r = c.post("/registry/customers", data={"name": "New Co"},
               follow_redirects=True)
    assert "New Co" in r.text

    r = c.post("/registry/cylinders", data={
        "serial": "SUP-1", "refrigerant_type": "R-410A", "kind": "supply",
        "capacity_lbs": "50", "current_lbs": "0",
    })
    assert r.status_code == 409
    assert "already exists" in r.text


def test_appliance_detail_and_charge_flow(world):
    c = _client(world)
    a_id = world["appliance"].id
    r = c.get(f"/appliances/{a_id}")
    assert r.status_code == 200
    assert "Add Refrigerant" in r.text

    r = c.post(f"/appliances/{a_id}/events/charge", data={
        "technician_id": world["tech"].id,
        "cylinder_id": world["supply"].id,
        "event_date": "2025-03-15", "pounds": "5", "notes": "topped off",
    })
    assert r.status_code == 200
    assert "25.00%" in r.text
    assert "THRESHOLD EXCEEDED" in r.text

    # Domain error surfaces as an inline banner, not a crash.
    r = c.post(f"/appliances/{a_id}/events/charge", data={
        "technician_id": world["tech"].id,
        "cylinder_id": world["recovery"].id,  # wrong cylinder kind
        "event_date": "2025-03-16", "pounds": "1",
    })
    assert r.status_code == 200
    assert "recovery cylinder" in r.text


def test_missing_appliance_404(world):
    c = _client(world)
    assert c.get("/appliances/999").status_code == 404


def test_static_assets_served(world):
    c = _client(world)
    assert c.get("/static/pico.min.css").status_code == 200
    assert c.get("/static/htmx.min.js").status_code == 200
