import logging
import os

from utilities.google_auth_utils import get_secret

logger = logging.getLogger(__name__)

_twilio_client = None


def _get_twilio_client():
    global _twilio_client
    if _twilio_client:
        return _twilio_client
    try:
        from twilio.rest import Client
        account_sid = get_secret('CRAB_TWILIO_ACCOUNT_SID')
        auth_token = get_secret('CRAB_TWILIO_AUTH_TOKEN')
        if not account_sid or not auth_token:
            logger.warning("Twilio credentials not found")
            return None
        _twilio_client = Client(account_sid, auth_token)
        return _twilio_client
    except Exception as e:
        logger.error(f"Twilio client init failed: {e}")
        return None


def send_sms(to_number, body):
    """Send an SMS via Twilio. Returns message SID or None."""
    client = _get_twilio_client()
    if not client:
        return None
    try:
        messaging_sid = get_secret('CRAB_TWILIO_MESSAGING_SERVICE_SID')
        msg = client.messages.create(
            to=to_number,
            messaging_service_sid=messaging_sid,
            body=body,
        )
        logger.info(f"SMS sent to {to_number}: {msg.sid}")
        return msg.sid
    except Exception as e:
        logger.error(f"SMS send failed to {to_number}: {e}")
        return None


def notify_plan_members_sms(plan_id, sender_name, message_text, exclude_user_id=None):
    """Send SMS notifications to plan members who opted in.

    Respects notify_channel (sms/both) and notify_chat = realtime.
    """
    from utilities.postgres_utils import get_db_connection
    import psycopg2.extras

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT DISTINCT u.pk_id, u.full_name, u.phone_number
            FROM crab.plan_members m
            JOIN crab.users u ON u.pk_id = m.user_id
            WHERE m.plan_id = %s::uuid
              AND u.phone_number IS NOT NULL
              AND u.phone_number != ''
              AND u.notify_chat = 'realtime'
              AND u.notify_channel IN ('sms', 'both')
              AND u.full_name NOT LIKE '%%[BOT]%%'
        """, (str(plan_id),))
        members = cursor.fetchall()

        sent = 0
        for member in members:
            if exclude_user_id and member['pk_id'] == exclude_user_id:
                continue
            # Truncate long messages
            preview = message_text[:140] + '...' if len(message_text) > 140 else message_text
            body = f"[crab.travel] {sender_name}: {preview}"
            if send_sms(member['phone_number'], body):
                sent += 1

        logger.info(f"SMS notifications: {sent}/{len(members)} sent for plan {plan_id}")
        return sent
    except Exception as e:
        logger.error(f"SMS notification failed: {e}")
        return 0
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def notify_plan_members_email(plan_id, sender_name, message_text, exclude_user_id=None, message_id=None):
    """Send email notifications to plan members who opted in.

    Respects notify_channel (email/both) and notify_chat = realtime.
    Includes deep link to the exact message and an unsubscribe link.
    """
    from utilities.postgres_utils import get_db_connection
    from utilities.gmail_utils import send_simple_email
    import psycopg2.extras

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT DISTINCT u.pk_id, u.full_name, u.email, m.member_token
            FROM crab.plan_members m
            JOIN crab.users u ON u.pk_id = m.user_id
            WHERE m.plan_id = %s::uuid
              AND u.notify_chat = 'realtime'
              AND u.notify_channel IN ('email', 'both')
              AND u.full_name NOT LIKE '%%[BOT]%%'
        """, (str(plan_id),))
        members = cursor.fetchall()

        # Get plan name for context
        cursor.execute("SELECT title FROM crab.plans WHERE plan_id = %s::uuid", (str(plan_id),))
        plan_row = cursor.fetchone()
        plan_name = plan_row['title'] if plan_row else 'your trip'

        # Build deep link to the chat message
        plan_url = f"https://crab.travel/plan/{plan_id}"
        if message_id:
            plan_url += f"#msg-{message_id}"

        sent = 0
        for member in members:
            if exclude_user_id and member['pk_id'] == exclude_user_id:
                continue
            preview = message_text[:300] + '...' if len(message_text) > 300 else message_text
            unsub_url = f"https://crab.travel/notifications/off/{member['member_token']}"
            subject = f"[crab.travel] {sender_name} in {plan_name}"
            body = (
                f"{sender_name} said:\n\n"
                f"{preview}\n\n"
                f"—\n"
                f"Reply at {plan_url}\n\n"
                f"Unsubscribe from this trip: {unsub_url}"
            )
            if send_simple_email(subject, body, member['email']):
                sent += 1

        logger.info(f"Email notifications: {sent}/{len(members)} sent for plan {plan_id}")
        return sent
    except Exception as e:
        logger.error(f"Email notification failed: {e}")
        return 0
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def notify_plan_members(plan_id, sender_name, message_text, exclude_user_id=None, message_id=None):
    """Unified dispatcher — sends both SMS and email notifications."""
    sms_count = notify_plan_members_sms(plan_id, sender_name, message_text, exclude_user_id)
    email_count = notify_plan_members_email(plan_id, sender_name, message_text, exclude_user_id, message_id=message_id)
    return sms_count + email_count
