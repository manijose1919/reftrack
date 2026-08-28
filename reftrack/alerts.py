"""Optional email alerts on threshold exceedance, via Brevo's free tier.

Disabled unless all three env vars are set:
    BREVO_API_KEY          Brevo API key (free tier: 300 emails/day)
    REFTRACK_ALERT_TO      recipient email
    REFTRACK_ALERT_FROM    verified sender email

Alerts are best-effort and dispatched on a background thread: a slow or failing
Brevo must never delay (or fail) the recording of a compliance event. Failures
are logged, never raised.
"""

import logging
import os
import html
import threading

import httpx

logger = logging.getLogger("reftrack.alerts")

BREVO_URL = "https://api.brevo.com/v3/smtp/email"
TIMEOUT_SECONDS = 10

# Set by tests to wait for the dispatch thread deterministically.
_last_thread: threading.Thread | None = None


def enabled() -> bool:
    return all(
        os.environ.get(k)
        for k in ("BREVO_API_KEY", "REFTRACK_ALERT_TO", "REFTRACK_ALERT_FROM")
    )


def send_case_alert(
    *,
    appliance_name: str,
    customer_name: str,
    leak_rate_pct: float,
    threshold_pct: float,
    due_date: str,
) -> bool:
    """Queue a threshold-exceedance alert on a background thread.

    Returns True if an alert was *dispatched* (not delivered) — delivery is
    best-effort and must never block or fail the caller.
    """
    global _last_thread
    if not enabled():
        return False
    payload = {
        "sender": {"email": os.environ["REFTRACK_ALERT_FROM"], "name": "RefTrack"},
        "to": [{"email": os.environ["REFTRACK_ALERT_TO"]}],
        "subject": (
            f"RefTrack ALERT: {appliance_name} exceeded its leak threshold "
            f"({leak_rate_pct:.1f}% > {threshold_pct:.0f}%)"
        ),
        "htmlContent": (
            f"<p><b>{html.escape(appliance_name)}</b> at "
            f"<b>{html.escape(customer_name)}</b> has an "
            f"annualized leak rate of <b>{leak_rate_pct:.2f}%</b>, exceeding "
            f"its EPA threshold of {threshold_pct:.0f}%.</p>"
            f"<p>The 30-day repair clock has started. "
            f"<b>Repair due: {html.escape(due_date)}</b>.</p>"
            "<p>Log the repair (or retrofit/retirement plan) in RefTrack.</p>"
        ),
    }
    api_key = os.environ["BREVO_API_KEY"]

    def _deliver() -> None:
        try:
            resp = httpx.post(
                BREVO_URL,
                json=payload,
                headers={"api-key": api_key},
                timeout=TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            logger.info("Sent exceedance alert for %s", appliance_name)
        except Exception:  # noqa: BLE001 — must never surface to the caller
            logger.exception(
                "Failed to send exceedance alert for %s. The compliance record "
                "was still saved; check the dashboard.", appliance_name
            )

    thread = threading.Thread(
        target=_deliver, name="reftrack-alert", daemon=True
    )
    thread.start()
    _last_thread = thread
    return True
