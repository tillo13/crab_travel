"""Twilio usage logger — crab_travel side.

Writes spend rows to the shared kumori_api_usage table with provider='twilio'
so kumori's killswitch can see MTD across every app.

Pricing reference (US, refresh annually from twilio.com/pricing):
  SMS outbound (US/Canada):  $0.0083 per segment
  SMS inbound:               $0.0075 per segment
  MMS outbound:              $0.02   per segment
  Voice outbound (US):       $0.014  per minute
"""
from __future__ import annotations

import logging
import threading

from utilities.postgres_utils import get_db_connection

logger = logging.getLogger('crab_travel.twilio_logger')


PER_UNIT_PRICES = {
    'sms_outbound':   0.0083,
    'sms_inbound':    0.0075,
    'mms_outbound':   0.02,
    'voice_minute':   0.014,
}


def estimate_sms_cost(body: str, kind: str = 'sms_outbound') -> float:
    """Rough segment count from body length. SMS = 160 chars/segment for GSM-7,
    70 for unicode. We use 160 — slight underestimate for emoji-heavy texts."""
    segments = max(1, (len(body or '') + 159) // 160)
    return PER_UNIT_PRICES.get(kind, 0.0083) * segments


def log_twilio_async(*, app_name: str = 'crab_travel', kind: str = 'sms_outbound',
                     body: str = '', cost_usd: float = None, feature: str = None,
                     user_id: str = None) -> float:
    """Log a Twilio call's spend to kumori_api_usage. Fire-and-forget.
    Returns the estimated cost (caller can surface if useful)."""
    cost = cost_usd if cost_usd is not None else estimate_sms_cost(body, kind)

    def _do():
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO kumori_api_usage
                (provider, app_name, feature, model, image_count,
                 estimated_cost_usd, streaming, user_id, duration_ms)
                VALUES ('twilio', %s, %s, %s, 0, %s, FALSE, %s, 0)
            """, (app_name, feature or kind, kind, cost, user_id))
            conn.commit()
            logger.info(f"twilio_logger: logged {app_name}/{kind} ${cost:.4f}")
        except Exception as e:
            logger.warning(f"twilio_logger: kumori_api_usage INSERT failed: {e}")
        finally:
            if conn:
                conn.close()

    threading.Thread(target=_do, daemon=True).start()
    return cost


__all__ = ['estimate_sms_cost', 'log_twilio_async']
