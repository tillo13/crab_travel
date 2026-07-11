"""
Bot logs, itinerary, expenses, LLM call logging.
Split from utilities/postgres_utils.py for kumori 1000-line compliance.
"""
import logging
import os
import psycopg2
import psycopg2.extras
from utilities.postgres_utils import db_cursor, get_db_connection

logger = logging.getLogger(__name__)


# === Bot runs + events ===

def insert_bot_run(mode='full'):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            INSERT INTO crab.bot_runs (mode) VALUES (%s) RETURNING run_id
        """, (mode,))
        run_id = str(cursor.fetchone()['run_id'])
        conn.commit()
        return run_id
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Insert bot run failed: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def update_bot_run(run_id, **kwargs):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        sets = []
        vals = []
        for key in ('status', 'plan_id', 'phases_passed', 'phases_failed', 'phases_warned', 'finished_at', 'summary'):
            if key in kwargs:
                sets.append(f"{key} = %s")
                val = kwargs[key]
                if key == 'summary' and isinstance(val, dict):
                    import json as _json
                    val = _json.dumps(val)
                vals.append(val)
        if not sets:
            return
        vals.append(run_id)
        cursor.execute(f"UPDATE crab.bot_runs SET {', '.join(sets)} WHERE run_id = %s::uuid", vals)
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Update bot run failed: {e}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def insert_bot_event(run_id, phase, bot_name, action, status='ok', detail=None):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        import json as _json
        cursor.execute("""
            INSERT INTO crab.bot_events (run_id, phase, bot_name, action, status, detail)
            VALUES (%s::uuid, %s, %s, %s, %s, %s)
        """, (run_id, phase, bot_name, action, status, _json.dumps(detail) if detail else None))
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Insert bot event failed: {e}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_bot_runs(limit=10):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT * FROM crab.bot_runs ORDER BY started_at DESC LIMIT %s
        """, (limit,))
        return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Get bot runs failed: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_bot_events(run_id, limit=200):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT * FROM crab.bot_events WHERE run_id = %s::uuid
            ORDER BY event_id DESC LIMIT %s
        """, (run_id, limit))
        return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Get bot events failed: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_bot_run_status(run_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT status FROM crab.bot_runs WHERE run_id = %s::uuid", (run_id,))
        row = cursor.fetchone()
        return row['status'] if row else None
    except Exception as e:
        logger.error(f"Get bot run status failed: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

# === Trip summary + itinerary + expenses ===

def get_trip_summary(plan_id):
    """Build a comprehensive trip summary: watches grouped by member with cost breakdowns."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Get all watches with member info
        cursor.execute("""
            SELECT w.*, m.display_name, m.pk_id as member_pk_id,
                   COALESCE(u.full_name, m.display_name) as member_name,
                   m.home_airport
            FROM crab.member_watches w
            JOIN crab.plan_members m ON m.pk_id = w.member_id
            LEFT JOIN crab.users u ON u.pk_id = m.user_id
            WHERE w.plan_id = %s
            ORDER BY m.joined_at, w.watch_type
        """, (plan_id,))
        watches = [dict(r) for r in cursor.fetchall()]

        # Get member count
        cursor.execute("SELECT COUNT(*) as cnt FROM crab.plan_members WHERE plan_id = %s", (plan_id,))
        member_count = cursor.fetchone()['cnt']

        # Group watches by member
        members = {}
        flights_total = 0
        hotels_total = 0
        booked_count = 0

        for w in watches:
            mid = w['member_pk_id']
            if mid not in members:
                members[mid] = {
                    'name': w['member_name'],
                    'home_airport': w.get('home_airport'),
                    'flights': [],
                    'hotels': [],
                    'flight_cost': 0,
                    'hotel_cost': 0,
                    'total_cost': 0,
                }
            price = float(w.get('best_price_usd') or w.get('last_price_usd') or 0)
            booked_price = None
            confirmation = None
            if w.get('data') and isinstance(w['data'], dict):
                booked_price = w['data'].get('booked_price')
                confirmation = w['data'].get('confirmation')
            # Use booked_price if available, else best/last price
            effective_price = float(booked_price) if booked_price is not None else price

            data = w.get('data') or {}
            watch_info = {
                'pk_id': w['pk_id'],
                'watch_type': w['watch_type'],
                'origin': w.get('origin'),
                'destination': w['destination'],
                'checkin': w.get('checkin'),
                'checkout': w.get('checkout'),
                'status': w['status'],
                'price': effective_price,
                'deep_link': w.get('deep_link'),
                'confirmation': confirmation,
                'departure_time': data.get('departure_time'),
                'arrival_time': data.get('arrival_time'),
                'return_departure_time': data.get('return_departure_time'),
                'return_arrival_time': data.get('return_arrival_time'),
                'data': data,
            }

            if w['watch_type'] == 'flight':
                members[mid]['flights'].append(watch_info)
                members[mid]['flight_cost'] += effective_price
                flights_total += effective_price
            else:
                members[mid]['hotels'].append(watch_info)
                members[mid]['hotel_cost'] += effective_price
                hotels_total += effective_price

            if w['status'] == 'booked':
                booked_count += 1

            members[mid]['total_cost'] = members[mid]['flight_cost'] + members[mid]['hotel_cost']

        grand_total = flights_total + hotels_total
        per_person = grand_total / member_count if member_count > 0 else 0

        return {
            'members': members,
            'member_count': member_count,
            'booked_count': booked_count,
            'total_watches': len(watches),
            'flights_total': round(flights_total, 2),
            'hotels_total': round(hotels_total, 2),
            'grand_total': round(grand_total, 2),
            'per_person': round(per_person, 2),
        }
    except Exception as e:
        logger.error(f"Get trip summary failed: {e}")
        return {
            'members': {}, 'member_count': 0, 'booked_count': 0, 'total_watches': 0,
            'flights_total': 0, 'hotels_total': 0, 'grand_total': 0, 'per_person': 0,
        }
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_itinerary_items(plan_id):
    """Get all itinerary items for a plan, ordered by date and time."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT i.*, m.display_name as added_by_name
            FROM crab.itinerary_items i
            LEFT JOIN crab.plan_members m ON m.pk_id = i.added_by
            WHERE i.plan_id = %s
            ORDER BY i.scheduled_date, i.scheduled_time NULLS LAST
        """, (plan_id,))
        return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Get itinerary items failed: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def insert_itinerary_item(plan_id, title, category, scheduled_date, scheduled_time=None,
                          duration_minutes=None, location=None, url=None, notes=None, added_by=None):
    """Insert a new itinerary item."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            INSERT INTO crab.itinerary_items
                (plan_id, title, category, scheduled_date, scheduled_time,
                 duration_minutes, location, url, notes, added_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
        """, (plan_id, title, category, scheduled_date, scheduled_time,
              duration_minutes, location, url, notes, added_by))
        row = cursor.fetchone()
        conn.commit()
        return dict(row) if row else None
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Insert itinerary item failed: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def delete_itinerary_item(item_id):
    """Delete an itinerary item by item_id (UUID)."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM crab.itinerary_items WHERE item_id = %s", (item_id,))
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Delete itinerary item failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_expenses(plan_id):
    """Get all expenses for a plan with member names joined."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT e.*, COALESCE(u.full_name, m.display_name) as paid_by_name
            FROM crab.expenses e
            JOIN crab.plan_members m ON m.pk_id = e.paid_by
            LEFT JOIN crab.users u ON u.pk_id = m.user_id
            WHERE e.plan_id = %s
            ORDER BY e.expense_date DESC, e.created_at DESC
        """, (plan_id,))
        return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Get expenses failed: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def insert_expense(plan_id, paid_by, title, amount, category='other', split_type='equal', split_among=None):
    """Insert an expense record."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            INSERT INTO crab.expenses (plan_id, paid_by, title, amount, category, split_type, split_among)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING *
        """, (plan_id, paid_by, title, amount, category, split_type,
              psycopg2.extras.Json(split_among) if split_among else psycopg2.extras.Json([])))
        row = cursor.fetchone()
        conn.commit()
        return dict(row) if row else None
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Insert expense failed: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_trip_cost_summary(plan_id):
    """Aggregate all booked watch prices + expenses into totals."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Watch costs (booked items)
        cursor.execute("""
            SELECT
                COALESCE(SUM(CASE WHEN watch_type = 'flight' THEN COALESCE(best_price_usd, last_price_usd, 0) END), 0) as flights_total,
                COALESCE(SUM(CASE WHEN watch_type != 'flight' THEN COALESCE(best_price_usd, last_price_usd, 0) END), 0) as hotels_total,
                COALESCE(SUM(COALESCE(best_price_usd, last_price_usd, 0)), 0) as watches_total
            FROM crab.member_watches
            WHERE plan_id = %s AND status = 'booked'
        """, (plan_id,))
        watch_totals = dict(cursor.fetchone())

        # Expense costs
        cursor.execute("""
            SELECT COALESCE(SUM(amount), 0) as expenses_total
            FROM crab.expenses WHERE plan_id = %s
        """, (plan_id,))
        expense_totals = dict(cursor.fetchone())

        # Member count
        cursor.execute("SELECT COUNT(*) as cnt FROM crab.plan_members WHERE plan_id = %s", (plan_id,))
        member_count = cursor.fetchone()['cnt']

        grand_total = float(watch_totals['watches_total']) + float(expense_totals['expenses_total'])
        return {
            'flights_total': float(watch_totals['flights_total']),
            'hotels_total': float(watch_totals['hotels_total']),
            'watches_total': float(watch_totals['watches_total']),
            'expenses_total': float(expense_totals['expenses_total']),
            'grand_total': grand_total,
            'per_person': round(grand_total / member_count, 2) if member_count > 0 else 0,
            'member_count': member_count,
        }
    except Exception as e:
        logger.error(f"Get trip cost summary failed: {e}")
        return {
            'flights_total': 0, 'hotels_total': 0, 'watches_total': 0,
            'expenses_total': 0, 'grand_total': 0, 'per_person': 0, 'member_count': 0,
        }
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

# === LLM call logging ===

def log_llm_call(backend, model=None, prompt_length=0, response_length=0,
                 duration_ms=0, success=True, error_message=None, caller=None,
                 error_type=None, status_code=None):
    """Log an LLM call attempt to telemetry.

    error_type: 'rate_limit', 'timeout', 'auth', 'payment', 'connection',
                'skip_rpm', 'skip_cap', 'server_error', 'other'
    status_code: HTTP status code (429, 401, 402, 500, etc.) or None
    """
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO crab_llm_telemetry
                (backend, model, prompt_length, response_length, duration_ms,
                 success, error_message, caller, error_type, status_code)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (backend, model, prompt_length, response_length, duration_ms, success,
              (error_message or '')[:500] if error_message else None, caller,
              error_type, status_code))
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        # Fallback without new columns (before migration runs)
        try:
            conn2 = get_db_connection()
            cur2 = conn2.cursor()
            cur2.execute("""
                INSERT INTO crab_llm_telemetry
                    (backend, model, prompt_length, response_length, duration_ms,
                     success, error_message, caller)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (backend, model, prompt_length, response_length, duration_ms, success,
                  (error_message or '')[:500] if error_message else None, caller))
            conn2.commit()
            cur2.close()
            conn2.close()
        except Exception:
            pass
        logger.debug(f"LLM telemetry log failed (new cols?): {e}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
