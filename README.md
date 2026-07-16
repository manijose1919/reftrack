# RefTrack ❄

**EPA Section 608 refrigerant compliance tracker for small HVAC/refrigeration shops.**

Appliances holding 50+ lbs of refrigerant legally require leak-rate tracking under
40 CFR Part 82, Subpart F. Enterprise tools cost $100–500/month; small shops use
paper and get the annualized-leak-rate math wrong. RefTrack is self-hosted,
local-first (single SQLite file), and free.

## What it does

- **Registry** — customers, technicians (with EPA cert numbers), appliances, and
  refrigerant cylinders.
- **Service logging** — every charge addition and recovery, tied to a technician
  and cylinder. Supply cylinders are debited, recovery cylinders credited (with
  the DOT 80% safe-fill limit enforced).
- **Leak-rate engine** — the EPA *annualizing* method, computed at every addition:
  `(lbs added ÷ full charge) × (365 ÷ days since last addition) × 100`.
  Same-day additions merge into one service episode. Sub-50-lb appliances are
  automatically exempt.
- **Compliance cases** — exceeding the category threshold (10% comfort cooling /
  20% commercial refrigeration / 30% industrial process) opens a case with the
  regulatory 30-day repair clock. Repairs, retrofits, and retirements resolve it.
- **Audit exports** — per-appliance PDF compliance report (service history,
  leak-rate snapshots, exceedance timeline, signature line) and CSV exports.

## Quick start (Windows)

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python seed.py            # optional demo data (--reset to wipe)
.\.venv\Scripts\python -m uvicorn reftrack.main:app --port 8377
```

Open http://127.0.0.1:8377 — dashboard, registry, and interactive API docs at `/docs`.

Or just run **`run.bat`**.

## Architecture

```
reftrack/
  leakrate.py    pure EPA math (no DB, no framework — independently auditable)
  service.py     all business rules: inventory, episodes, case lifecycle
  compliance.py  status derivation (computed at read time, never stored)
  models.py      SQLAlchemy 2.0 models  ·  database.py  engine/session
  api.py         JSON REST API          ·  ui.py        htmx server-rendered UI
  reports.py     CSV + ReportLab PDF generation
  templates/     Jinja2  ·  static/  vendored Pico.css + htmx (works offline)
tests/           38 tests incl. EPA worked examples
```

Facts (events, leak-rate snapshots) are persisted; judgments (status, overdue)
are derived from facts + today's date, so they can never go stale.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `REFTRACK_DB` | `reftrack.db` | Path to the SQLite database file |

## Tests

```powershell
.\.venv\Scripts\python -m pytest tests/ -q
```

## Notes & limits (v1.0)

- Single-user, trusted-LAN tool: no authentication. Put it behind a reverse
  proxy with auth if exposed beyond localhost.
- Records must be retained 3 years (40 CFR 82.157) — back up `reftrack.db`.
- v1.1 candidates: email alerts on threshold breach (e.g. Brevo free tier),
  chronic-leaker reporting to EPA for appliances >50 lbs exceeding 125%,
  multi-shop support.

This tool assists with recordkeeping; it is not legal advice. Verify current
EPA requirements at epa.gov/section608.
