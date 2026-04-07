"""Unified notification dispatcher.

Owns the rule: a member's notify_channel ('sms'|'both') is honored only if the
trip's organizer is on subscription_tier='premium'. Otherwise the effective
channel collapses to 'email'. This is the single source of truth for tier
gating; all three notification flows (chat, price drops, vote reminders) route
through this module.

When a CSP (Twilio/Telgorithm/etc.) approves us, the only thing that changes is
flipping organizer rows to subscription_tier='premium' — no code change needed.
"""
import logging

logger = logging.getLogger(__name__)


def _organizer_tier(plan_id):
    """Look up the subscription_tier of a plan's organizer. Returns 'free'|'premium'."""
    from utilities.postgres_utils import get_db_connection
    import psycopg2.extras
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT u.subscription_tier
            FROM crab.plans p
            JOIN crab.users u ON u.pk_id = p.organizer_id
            WHERE p.plan_id = %s::uuid
            LIMIT 1
        """, (str(plan_id),))
        row = cursor.fetchone()
        return (row['subscription_tier'] if row else 'free') or 'free'
    except Exception as e:
        logger.warning(f"organizer tier lookup failed for plan {plan_id}: {e}")
        return 'free'
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def _user_tier(user_id):
    """Look up an individual user's subscription_tier. Returns 'free'|'premium'."""
    from utilities.postgres_utils import get_db_connection
    import psycopg2.extras
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT subscription_tier FROM crab.users WHERE pk_id = %s", (user_id,))
        row = cursor.fetchone()
        return (row['subscription_tier'] if row else 'free') or 'free'
    except Exception as e:
        logger.warning(f"user tier lookup failed for {user_id}: {e}")
        return 'free'
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def _sms_unlocked(tier):
    """Tier rule: only premium tier unlocks SMS sending."""
    return tier == 'premium'


# ─── Use Case 1: Chat message forwarding ──────────────────────────────────

def notify_chat_message(plan_id, sender_name, message_text, exclude_user_id=None, message_id=None):
    """Forward a posted chat message to plan members.

    Routes via email always; also via SMS only if the plan's organizer is premium.
    """
    from utilities.sms_utils import notify_plan_members_email, notify_plan_members_sms

    email_count = notify_plan_members_email(
        plan_id, sender_name, message_text,
        exclude_user_id=exclude_user_id, message_id=message_id,
    )
    sms_count = 0
    if _sms_unlocked(_organizer_tier(plan_id)):
        sms_count = notify_plan_members_sms(
            plan_id, sender_name, message_text, exclude_user_id=exclude_user_id,
        )
    else:
        logger.info(f"chat notify: SMS gated (organizer not premium) for plan {plan_id}")
    return email_count + sms_count


# ─── Use Case 2: Price drop alerts ────────────────────────────────────────

def notify_price_drop(watch, old_price, new_price, deep_link=None):
    """Send a price drop notification for a single watcher.

    Always emails. Only SMSes if the user is on premium tier directly (price
    drops are per-user, not per-trip — gate on the user's own tier).
    """
    from utilities.gmail_utils import send_simple_email
    from utilities.sms_utils import send_sms

    member_name = watch.get('member_name', 'Traveler')
    if '[BOT]' in member_name:
        logger.info(f"price drop alert skipped for bot member {member_name}")
        return

    origin = watch.get('origin', '')
    destination = watch.get('destination', '')

    if watch.get('watch_type') == 'flight':
        subject = f"Price drop: {origin}→{destination} now ${new_price:.0f}"
        body = (
            f"Hey {member_name}!\n\n"
            f"Your {origin} → {destination} flight dropped to ${new_price:.2f} "
            f"(was ${old_price:.2f}).\n\n"
        )
    else:
        subject = f"Price drop: Hotels in {destination} now ${new_price:.0f}/night"
        body = (
            f"Hey {member_name}!\n\n"
            f"Hotels in {destination} dropped to ${new_price:.2f}/night "
            f"(was ${old_price:.2f}/night).\n\n"
        )
    if deep_link:
        body += f"Book now: {deep_link}\n\n"
    body += "— crab.travel"

    # Email path — always
    email = watch.get('user_email')
    if email:
        try:
            send_simple_email(subject, body, email)
            logger.info(f"price drop email sent to {email}")
        except Exception as e:
            logger.error(f"price drop email failed: {e}")

    # SMS path — gated on user tier (price drops are per-user, not per-trip)
    user_id = watch.get('user_id') or watch.get('pk_user_id')
    phone = watch.get('phone_number')
    notify_channel = watch.get('notify_channel', 'email')
    if not phone or notify_channel not in ('sms', 'both'):
        return
    tier = _user_tier(user_id) if user_id else 'free'
    if not _sms_unlocked(tier):
        logger.info(f"price drop: SMS gated (user not premium) for {phone}")
        return
    sms_body = f"[crab.travel] {subject}"
    if deep_link:
        sms_body += f" Book: {deep_link}"
    try:
        send_sms(phone, sms_body[:160])
        logger.info(f"price drop SMS sent to {phone}")
    except Exception as e:
        logger.error(f"price drop SMS failed: {e}")


# ─── Use Case 3: Vote reminders ───────────────────────────────────────────

def notify_vote_reminder(plan_id, days_remaining=None):
    """Email plan members who haven't voted yet on the destination poll.

    Idempotent via crab.notifications_sent — at most one reminder per
    (plan_id, user_id) per UTC day.
    """
    from utilities.postgres_utils import get_db_connection
    from utilities.gmail_utils import send_simple_email
    from utilities.sms_utils import send_sms
    import psycopg2.extras

    conn = None
    cursor = None
    sent_count = 0
    organizer_premium = _sms_unlocked(_organizer_tier(plan_id))
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # Plan title
        cursor.execute("SELECT title FROM crab.plans WHERE plan_id = %s::uuid", (str(plan_id),))
        prow = cursor.fetchone()
        plan_title = prow['title'] if prow else 'your trip'

        # Members opted in for trip-update notifications who have NOT been
        # reminded already today and have NOT cast any votes for this plan.
        cursor.execute("""
            SELECT DISTINCT u.pk_id, u.full_name, u.email, u.phone_number,
                   u.notify_channel, m.member_token
            FROM crab.plan_members m
            JOIN crab.users u ON u.pk_id = m.user_id
            WHERE m.plan_id = %s::uuid
              AND u.full_name NOT LIKE '%%[BOT]%%'
              AND u.notify_updates IN ('realtime', 'daily')
              AND NOT EXISTS (
                  SELECT 1 FROM crab.votes v
                  WHERE v.plan_id = %s::uuid AND v.user_id = u.pk_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM crab.notifications_sent n
                  WHERE n.plan_id = %s::uuid AND n.user_id = u.pk_id
                    AND n.notification_type = 'vote_reminder'
                    AND n.sent_at::date = NOW()::date
              )
        """, (str(plan_id), str(plan_id), str(plan_id)))
        members = cursor.fetchall()

        plan_url = f"https://crab.travel/plan/{plan_id}"
        deadline_str = f" You have {days_remaining} day{'s' if days_remaining != 1 else ''} left." if days_remaining else ""
        subject = f"[crab.travel] Reminder: vote on {plan_title}"
        for member in members:
            unsub_url = f"https://crab.travel/notifications/off/{member['member_token']}"
            body = (
                f"Hey {member['full_name'] or 'there'}!\n\n"
                f"Your group is waiting on your vote for {plan_title}.{deadline_str}\n\n"
                f"Cast your vote: {plan_url}\n\n"
                f"—\n"
                f"Stop reminders: {unsub_url}"
            )
            channel_used = None
            try:
                if send_simple_email(subject, body, member['email']):
                    channel_used = 'email'
                    sent_count += 1
            except Exception as e:
                logger.error(f"vote reminder email failed: {e}")

            # Premium SMS path
            if (organizer_premium and member.get('phone_number')
                    and member.get('notify_channel') in ('sms', 'both')):
                try:
                    sms_body = f"[crab.travel] Vote on {plan_title[:60]}: {plan_url}"
                    if send_sms(member['phone_number'], sms_body[:160]):
                        channel_used = 'sms' if not channel_used else 'both'
                except Exception as e:
                    logger.error(f"vote reminder sms failed: {e}")

            if channel_used:
                cursor.execute("""
                    INSERT INTO crab.notifications_sent (plan_id, user_id, notification_type, channel)
                    VALUES (%s::uuid, %s, 'vote_reminder', %s)
                """, (str(plan_id), member['pk_id'], channel_used))

        conn.commit()
        logger.info(f"vote reminders: {sent_count} sent for plan {plan_id}")
        return sent_count
    except Exception as e:
        logger.error(f"vote reminder dispatch failed for plan {plan_id}: {e}")
        if conn:
            conn.rollback()
        return 0
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
