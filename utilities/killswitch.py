"""Central paid-API killswitch — crab_travel side.

Reads/writes the same `kumori_api_killswitch` config table that kumori owns,
in the shared kumori-404602 Cloud SQL instance. Call `check_killswitch(provider)`
before every paid-API call. If MTD spend across all kumori-family apps for that
provider has crossed the cap, the row is flipped to disabled, an alert email is
sent (once), and the call is blocked here.

Mirror of kumori/utilities/killswitch.py — same logic, same table.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from utilities.postgres_utils import get_db_connection

logger = logging.getLogger('crab_travel.killswitch')


class KillswitchTripped(RuntimeError):
    def __init__(self, provider: str, reason: str):
        super().__init__(f"[killswitch] {provider} blocked: {reason}")
        self.provider = provider
        self.reason = reason


def check_killswitch(provider: str, est_cost: float = 0.0) -> None:
    """Raise KillswitchTripped if `provider` is disabled OR MTD + est >= cap.
    Call this BEFORE every paid-API call."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT monthly_cap_usd, enabled, trip_reason "
            "FROM kumori_api_killswitch WHERE provider = %s",
            (provider,),
        )
        row = cur.fetchone()
        if not row:
            return  # not configured = no enforcement
        cap, enabled, trip_reason = float(row[0]), bool(row[1]), row[2]

        if not enabled:
            raise KillswitchTripped(provider, trip_reason or 'manually disabled')

        cur.execute(
            "SELECT COALESCE(SUM(estimated_cost_usd), 0) FROM kumori_api_usage "
            "WHERE provider = %s AND created_at >= date_trunc('month', NOW())",
            (provider,),
        )
        mtd = float(cur.fetchone()[0] or 0)

        if mtd + est_cost >= cap:
            reason = (f"MTD ${mtd:.2f} + est ${est_cost:.4f} >= cap ${cap:.2f} "
                      f"(crab_travel detected, UTC {datetime.utcnow().isoformat(timespec='seconds')})")
            cur.execute(
                "UPDATE kumori_api_killswitch SET enabled = FALSE, trip_reason = %s, "
                "tripped_at = NOW(), updated_at = NOW() "
                "WHERE provider = %s AND enabled = TRUE",
                (reason, provider),
            )
            just_tripped = cur.rowcount > 0
            conn.commit()
            if just_tripped:
                _send_trip_alert(provider, mtd, cap, reason)
                logger.error(f"[killswitch] {provider} TRIPPED by crab_travel: {reason}")
            raise KillswitchTripped(provider, reason)
    finally:
        if conn:
            conn.close()


def _send_trip_alert(provider: str, mtd: float, cap: float, reason: str) -> None:
    """Email Andy when a provider just tripped. Best-effort, never raises."""
    try:
        from utilities.gmail_utils import send_simple_email
        subject = f"🚨 KUMORI KILLSWITCH TRIPPED — {provider} (${mtd:.2f} of ${cap:.2f})"
        body = (
            f"The {provider} killswitch just tripped.\n\n"
            f"MTD spent: ${mtd:.2f}\n"
            f"Cap:       ${cap:.2f}\n"
            f"Detected by: crab_travel\n"
            f"Reason:    {reason}\n\n"
            f"All future {provider} calls from every kumori-family app are now "
            f"blocked. Re-enable at https://kumori.ai/admin/killswitch."
        )
        send_simple_email(subject, body, 'andy.tillo@gmail.com', from_name='Kumori Killswitch')
    except Exception as e:
        logger.warning(f"[killswitch] trip alert email failed: {e}")
