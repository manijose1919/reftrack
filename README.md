# RefTrack ❄

**Free, self-hosted EPA Section 608 refrigerant compliance tracking for small HVAC and refrigeration contractors.**

Log every pound of refrigerant, get the annualized leak rate calculated correctly
every time, never miss a 30-day repair deadline, and hand an EPA auditor a
complete report in one click.

---

## Who this is for

Small HVAC/refrigeration shops — roughly 1–15 technicians — who service
commercial equipment holding **50 lbs or more** of refrigerant.

If that's you, 40 CFR Part 82, Subpart F legally requires you to track every
refrigerant addition and recovery, calculate annualized leak rates, and act
within 30 days when a leak threshold is exceeded. Records must be kept for
**three years**. Civil penalties reach tens of thousands of dollars per day, per
violation.

The existing tools are built for facility portfolios and priced accordingly
($100–500/month). So most small shops track refrigerant on paper tickets and
spreadsheets — and the annualized leak-rate formula is genuinely easy to get
wrong, because it's a *rolling* calculation based on the time between services,
not a calendar-year total. RefTrack exists to close that gap: it's free, runs on
one PC in your office, and does the math for you.

**Not for you if:** you're a facility operator with many sites and multiple
crews, or you need multi-user accounts and role permissions. RefTrack is
deliberately a single-shop, single-database tool.

---

## What it does

### Tracks the things the EPA asks about
- **Customers, appliances, technicians** (with EPA certification numbers) and
  **refrigerant cylinders**.
- Every **charge addition** and **recovery**, tied to a technician and a specific
  cylinder — which gives you cylinder-level inventory reconciliation for free.
- Appliances under 50 lbs are automatically marked **exempt** and skipped by the
  leak-rate engine, so your dashboard isn't cluttered with equipment that doesn't
  need tracking.

### Gets the leak-rate math right
Every time refrigerant is added, RefTrack computes the EPA **annualizing method**:

```
leak rate (%) = (lbs added ÷ full charge) × (365 ÷ days since last addition) × 100
```

Details that are easy to get wrong, handled for you:
- The baseline is the **previous addition**, not January 1st.
- The first addition annualizes from the **installation date**.
- **Same-day top-offs merge into one service episode** rather than annualizing
  over zero days (which would report an absurd rate and a false violation).

### Enforces the compliance clock
Exceeding your equipment's threshold — **10%** comfort cooling, **20%**
commercial refrigeration, **30%** industrial process — automatically opens a
compliance case with the regulatory **30-day repair deadline**.

- Repairs, retrofits, and retirements resolve the case.
- A **failed follow-up verification reopens the obligation** with a fresh 30-day
  clock, because a repair that didn't hold isn't a repair.
- You can record the dated **retrofit/retirement plan** required when equipment
  isn't repaired within 30 days.
- Appliances over a **125% annualized leak rate** are flagged for chronically
  leaking appliance reporting, with a dedicated export.

### Fixes mistakes without destroying the audit trail
Techs fat-finger entries. RefTrack uses **void, not delete**: the original event
stays on the record marked VOID with a reason and timestamp, and RefTrack then
- restores the cylinder inventory,
- **recomputes every downstream leak rate**, and
- reconciles the affected compliance cases (including reopening a case whose
  closing repair was voided).

### Produces audit-ready records
- **Per-appliance PDF** — your shop's identity, appliance details, full service
  history with the leak rate at each service, the exceedance/repair timeline with
  plans, and a signature line.
- **CSV exports** — per-appliance history, cylinder inventory, chronic leakers.
- Every report cites the 3-year retention requirement (40 CFR 82.157).

---

## Quick start

**Windows:** double-click **`run.bat`**. It creates the virtual environment on
first run, installs dependencies, starts the server, and opens your browser.

Manually, on any platform:

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt      # Linux/macOS: .venv/bin/pip
.venv/Scripts/python seed.py                       # optional demo data
.venv/Scripts/python -m uvicorn reftrack.main:app --port 8377
```

Open **http://127.0.0.1:8377**.

> Requires Python 3.11+. Tested on Python 3.14, Windows 11.

### First five minutes

1. Go to **Registry** and set your **Shop Profile** — it prints on every audit report.
2. Add your **technicians** (with EPA cert numbers), **customers**, and **cylinders**.
3. Add your **appliances**. Full charge and install date matter: the charge decides
   whether tracking applies, and the install date is the first leak-rate baseline.
4. Log service from the appliance page. The leak rate is calculated as you save.
5. Watch the **Dashboard** — anything not green needs attention.

### Demo data

```bash
.venv/Scripts/python seed.py --reset    # wipes the DB and loads a sample shop
```

---

## Daily use

The **Dashboard** is a status board for the whole shop:

| Status | Meaning |
|---|---|
| **OK** | Tracked, under threshold |
| **Watch** | At 75%+ of its threshold — trending toward a violation |
| **Action Required** | Threshold exceeded, inside the 30-day repair window |
| **Overdue** | 30-day repair window blown — you are out of compliance |
| **Exempt** | Under 50 lbs, no tracking required |
| **Retired** | Out of service |

Click any appliance for its full history, to log service, to record a plan, or
to pull its audit PDF.

---

## Configuration

All optional. RefTrack runs with zero configuration on localhost.

| Env var | Default | Purpose |
|---|---|---|
| `REFTRACK_DB` | `reftrack.db` | Path to the SQLite database file |
| `REFTRACK_PASSWORD` | *(unset)* | Set to require a shared shop password to use the app |
| `BREVO_API_KEY` | *(unset)* | Brevo API key — enables email alerts on threshold breach |
| `REFTRACK_ALERT_TO` | *(unset)* | Alert recipient address |
| `REFTRACK_ALERT_FROM` | *(unset)* | Verified Brevo sender address |

**Email alerts** need all three Brevo variables set; Brevo's free tier covers 300
emails/day, which is far more than a small shop will ever send. Alerts are
best-effort by design — if the email fails, the compliance record is still saved.
Recording must never depend on a third-party API.

**Authentication** is off by default (localhost, one PC, one shop). Set
`REFTRACK_PASSWORD` and everyone shares one password; the session cookie is an
HMAC derived from it, so changing the password signs everyone out. This is
deliberately not a user-account system.

---

## Backups

Your database is a single file. RefTrack **automatically copies it to
`backups/reftrack-YYYYMMDD.db` on startup**, once per day, keeping the last 30.

That protects you from mistakes and corruption — **not from losing the PC**.
These are legally required records with a 3-year retention obligation, so copy
`reftrack.db` (or the `backups/` folder) somewhere off the machine on a schedule.

---

## Architecture

```
reftrack/
  leakrate.py    Pure EPA math — no DB, no framework, independently auditable
  service.py     All business rules: inventory, episodes, case lifecycle, voids
  compliance.py  Status derivation (computed at read time, never stored)
  models.py      SQLAlchemy 2.0 models      database.py  engine, migrations, backups
  api.py         JSON REST API              ui.py        htmx server-rendered UI
  reports.py     CSV + ReportLab PDF        alerts.py    optional Brevo email
  auth.py        Optional shared-password sessions
  templates/     Jinja2      static/  vendored Pico.css + htmx — works fully offline
tests/           57 tests, incl. EPA worked examples
```

Two principles hold the design together:

**Facts are stored; judgments are derived.** Events and their leak-rate snapshots
are persisted. Status ("overdue", "watch") is computed from those facts plus
today's date, every time it's displayed — so it can never go stale.

**Business rules live in exactly one place.** `service.py` is the only module
that mutates state. The REST API and the web UI are both thin callers, which is
why they can't drift apart.

Schema changes migrate additively on startup (`ALTER TABLE ADD COLUMN` for
anything missing) — upgrading in place never drops or rewrites your data.

### Stack

Python 3.11+ · FastAPI · SQLAlchemy 2.0 · SQLite · Jinja2 + htmx · Pico.css ·
ReportLab. No Node toolchain, no build step, no external services, no cloud
account. Interactive API docs at `/docs`.

---

## Tests

```bash
.venv/Scripts/python -m pytest tests/ -q
```

57 tests. The leak-rate engine is verified against hand-computed EPA worked
examples; the suite also covers void/recompute cascades, case lifecycle
transitions, cylinder inventory limits, auth enforcement, and report generation.

---

## Limits and honest caveats

- **Single shop, single database.** No multi-tenancy, no per-user accounts, no
  role permissions.
- **Security model is a shared password**, and only if you enable it. Anyone who
  can reach the port and knows the password can edit records. Don't expose it to
  the open internet; keep it on your LAN or behind an authenticated reverse proxy
  with TLS.
- **No offline/mobile app.** Techs log service through a browser; on a phone that
  means a connection back to the shop machine.
- **RefTrack assists with recordkeeping. It is not legal advice, and it is not a
  substitute for knowing the regulation.** Rules change and vary by refrigerant
  and equipment type. Verify current requirements at
  [epa.gov/section608](https://www.epa.gov/section608).

## Roadmap

Multi-shop support · offline-capable mobile service entry · automatic
verification-test scheduling · refrigerant purchase/reclaim tracking for full
cradle-to-grave cylinder accounting.

## License

MIT.
