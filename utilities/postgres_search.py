"""
Search/deals/watches/messages/invite DB ops.
Split from utilities/postgres_utils.py for kumori 1000-line compliance.
"""
import logging
import os
from datetime import datetime, timezone
import psycopg2
import psycopg2.extras
from utilities.postgres_utils import db_cursor, get_db_connection

logger = logging.getLogger(__name__)


# === Search results ===

def save_search_result(plan_id, result_type, source, data, canonical_key=None, title=None, price_usd=None, deep_link=None):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            INSERT INTO crab.search_results
                (plan_id, result_type, source, canonical_key, title, price_usd, deep_link, data)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING pk_id, found_at
        """, (
            plan_id, result_type, source, canonical_key,
            title, price_usd, deep_link,
            psycopg2.extras.Json(data),
        ))
        row = cursor.fetchone()
        conn.commit()
        return dict(row)
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Save search result failed: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_search_results(plan_id, result_type=None, since_id=0, limit=200):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        sql = """
            SELECT pk_id, result_type, source, canonical_key, title, price_usd, deep_link, data, found_at
            FROM crab.search_results
            WHERE plan_id = %s AND pk_id > %s
        """
        params = [plan_id, since_id]
        if result_type:
            sql += " AND result_type = %s"
            params.append(result_type)
        sql += " ORDER BY pk_id ASC LIMIT %s"
        params.append(limit)
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d['found_at'] = d['found_at'].isoformat() if d['found_at'] else None
            d['price_usd'] = float(d['price_usd']) if d['price_usd'] else None
            results.append(d)
        return results
    except Exception as e:
        logger.error(f"❌ Get search results failed: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def clear_search_results(plan_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM crab.search_results WHERE plan_id = %s", (plan_id,))
        conn.commit()
        return True
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Clear search results failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

# === Deals and pricing ===

def save_price_history(result_type, canonical_key, source, price_usd, travel_date=None):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO crab.price_history (result_type, canonical_key, source, price_usd, travel_date)
            VALUES (%s, %s, %s, %s, %s)
        """, (result_type, canonical_key, source, price_usd, travel_date))
        conn.commit()
        return True
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Save price history failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def upsert_deals_cache(deals):
    """
    Upsert a list of deal dicts into crab.deals_cache.
    deal_key = source:deal_type:origin:destination (unique per route/service).
    Tracks lowest_price_seen, last_seen_at, seen_count automatically.
    """
    if not deals:
        return 0
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        count = 0
        for d in deals:
            key = f"{d.get('source')}:{d.get('deal_type')}:{d.get('origin', '')}:{d.get('destination', '')}"
            cursor.execute("""
                INSERT INTO crab.deals_cache (
                    deal_key, source, deal_type, origin, destination, destination_name,
                    title, airline, price_per_person, lowest_price_seen, price_unit,
                    depart_date, deep_link, bookable
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (deal_key) DO UPDATE SET
                    price_per_person  = EXCLUDED.price_per_person,
                    lowest_price_seen = LEAST(crab.deals_cache.lowest_price_seen, EXCLUDED.price_per_person),
                    depart_date       = EXCLUDED.depart_date,
                    deep_link         = EXCLUDED.deep_link,
                    title             = EXCLUDED.title,
                    airline           = EXCLUDED.airline,
                    last_seen_at      = NOW(),
                    seen_count        = crab.deals_cache.seen_count + 1
            """, (
                key,
                d.get('source'), d.get('deal_type'),
                d.get('origin'), d.get('destination'), d.get('destination_name'),
                d.get('title'), d.get('airline'),
                d.get('price_per_person'), d.get('price_per_person'),
                d.get('price_unit', 'person'),
                d.get('depart_date'), d.get('deep_link'),
                d.get('bookable', False),
            ))
            count += 1
        conn.commit()
        logger.info(f"💾 Upserted {count} deals to cache")
        return count
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ upsert_deals_cache failed: {e}")
        return 0
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_deals_cache_grouped(origin=None):
    """
    Read deals from cache grouped by source, sorted by price.
    If origin provided, filter flight deals to that origin (hotels/activities are global).
    Returns list of tab dicts matching the deals_engine grouped format.
    """
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if origin:
            cursor.execute("""
                SELECT *, (NOW() - last_seen_at) AS age
                FROM crab.deals_cache
                WHERE (origin = %s OR origin IS NULL)
                ORDER BY source, price_per_person ASC
            """, (origin.upper(),))
        else:
            cursor.execute("""
                SELECT *, (NOW() - last_seen_at) AS age
                FROM crab.deals_cache
                ORDER BY source, price_per_person ASC
            """)

        rows = cursor.fetchall()
        buckets = {}
        latest_seen_at = None
        for r in rows:
            d = dict(r)
            d['price_per_person'] = float(d['price_per_person'])
            d['lowest_price_seen'] = float(d['lowest_price_seen'])
            lsa = d.pop('age', None)  # remove timedelta — not JSON serializable
            # track the most recent last_seen_at across all rows
            if d.get('last_seen_at'):
                ts = d['last_seen_at'].isoformat() if hasattr(d['last_seen_at'], 'isoformat') else str(d['last_seen_at'])
                if latest_seen_at is None or ts > latest_seen_at:
                    latest_seen_at = ts
                d['last_seen_at'] = ts
            buckets.setdefault(d['source'], []).append(d)

        TAB_ORDER = [
            ("travelpayouts",       "✈️ Aviasales Specials"),
            ("travelpayouts_cheap", "✈️ Aviasales All Flights"),
            ("duffel",              "✈️ Duffel Flights"),
            ("liteapi",             "🏨 LiteAPI Hotels"),
            ("viator",              "🎟️ Viator Activities"),
        ]
        tabs = []
        for src_key, label in TAB_ORDER:
            deals = buckets.get(src_key, [])
            if deals:
                tabs.append({"key": src_key, "label": f"{label} ({len(deals)})", "deals": deals})
        return {"tabs": tabs, "last_updated": latest_seen_at}
    except Exception as e:
        logger.error(f"❌ get_deals_cache_grouped failed: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_price_average(canonical_key, result_type, days=90):
    """90-day average price for a route/property — the deal detection baseline."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT AVG(price_usd) as avg_price, COUNT(*) as sample_count
            FROM crab.price_history
            WHERE canonical_key = %s
              AND result_type = %s
              AND observed_at > NOW() - INTERVAL '%s days'
        """, (canonical_key, result_type, days))
        row = cursor.fetchone()
        if row and row[0]:
            return {'avg_price': float(row[0]), 'sample_count': row[1]}
        return None
    except Exception as e:
        logger.error(f"❌ Get price average failed: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

# === Messages ===

def create_message(plan_id, user_id, display_name, content, parent_id=None):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            INSERT INTO crab.messages (plan_id, user_id, display_name, content, parent_id)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING *
        """, (plan_id, user_id, display_name, content, parent_id))
        msg = cursor.fetchone()
        conn.commit()
        return dict(msg) if msg else None
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Create message failed: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_plan_messages(plan_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT m.*, u.picture_url as user_picture
            FROM crab.messages m
            LEFT JOIN crab.users u ON m.user_id = u.pk_id
            WHERE m.plan_id = %s
            ORDER BY m.created_at ASC
        """, (plan_id,))
        rows = cursor.fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"❌ Get messages failed: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def delete_message(message_id, user_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM crab.messages WHERE message_id = %s AND user_id = %s", (message_id, user_id))
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Delete message failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

# === Invite view tracking ===

def log_invite_view(plan_id, user_id=None, ip_address=None, user_agent=None, is_authenticated=False):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO crab.invite_views (plan_id, user_id, ip_address, user_agent, is_authenticated)
            VALUES (%s, %s, %s, %s, %s)
        """, (plan_id, user_id, ip_address, (user_agent or '')[:500], is_authenticated))
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Log invite view failed: {e}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_invite_view_stats(plan_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT
                COUNT(*) as total_views,
                COUNT(DISTINCT ip_address) as unique_visitors,
                SUM(CASE WHEN is_authenticated THEN 1 ELSE 0 END) as authenticated_views,
                COUNT(DISTINCT CASE WHEN is_authenticated THEN user_id END) as unique_signed_in
            FROM crab.invite_views
            WHERE plan_id = %s
        """, (plan_id,))
        return dict(cursor.fetchone())
    except Exception as e:
        logger.error(f"❌ Get invite view stats failed: {e}")
        return {'total_views': 0, 'unique_visitors': 0, 'authenticated_views': 0, 'unique_signed_in': 0}
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

# === Watches ===

def create_member_watch(plan_id, member_id, watch_type, destination, checkin=None, checkout=None,
                        origin=None, budget_max=None):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            INSERT INTO crab.member_watches
                (plan_id, member_id, watch_type, origin, destination, checkin, checkout, budget_max)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (plan_id, member_id, watch_type, COALESCE(origin, '')) DO NOTHING
            RETURNING *
        """, (plan_id, member_id, watch_type, origin, destination, checkin, checkout, budget_max))
        row = cursor.fetchone()
        conn.commit()
        return dict(row) if row else None
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Create member watch failed: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_watches_for_plan(plan_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT w.*, m.display_name, u.full_name,
                   COALESCE(u.full_name, m.display_name) as member_name
            FROM crab.member_watches w
            JOIN crab.plan_members m ON m.pk_id = w.member_id
            LEFT JOIN crab.users u ON u.pk_id = m.user_id
            WHERE w.plan_id = %s
            ORDER BY m.joined_at, w.watch_type
        """, (plan_id,))
        return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Get watches for plan failed: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_watches_for_member(plan_id, member_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT * FROM crab.member_watches
            WHERE plan_id = %s AND member_id = %s
            ORDER BY watch_type
        """, (plan_id, member_id))
        return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Get watches for member failed: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_active_watches():
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT w.*, m.display_name, m.user_id,
                   COALESCE(u.full_name, m.display_name) as member_name,
                   u.email as user_email, u.phone_number, u.notify_channel,
                   u.subscription_tier
            FROM crab.member_watches w
            JOIN crab.plan_members m ON m.pk_id = w.member_id
            LEFT JOIN crab.users u ON u.pk_id = m.user_id
            WHERE w.status = 'active'
            ORDER BY w.destination, w.checkin, w.watch_type
        """)
        return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Get active watches failed: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def update_watch_price(watch_id, price_usd, deep_link=None, data=None, source='unknown'):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Update last price and conditionally update best price
        cursor.execute("""
            UPDATE crab.member_watches SET
                last_price_usd = %s,
                last_checked_at = NOW(),
                deep_link = COALESCE(%s, deep_link),
                data = COALESCE(%s, data),
                best_price_usd = CASE
                    WHEN best_price_usd IS NULL OR %s < best_price_usd THEN %s
                    ELSE best_price_usd
                END,
                best_price_at = CASE
                    WHEN best_price_usd IS NULL OR %s < best_price_usd THEN NOW()
                    ELSE best_price_at
                END
            WHERE pk_id = %s
            RETURNING *, (best_price_usd IS NOT NULL AND %s < best_price_usd) as is_new_best
        """, (price_usd, deep_link, psycopg2.extras.Json(data) if data else None,
              price_usd, price_usd, price_usd, watch_id, price_usd))
        watch = cursor.fetchone()
        # Record history
        cursor.execute("""
            INSERT INTO crab.watch_history (watch_id, price_usd, source, deep_link, data)
            VALUES (%s, %s, %s, %s, %s)
        """, (watch_id, price_usd, source, deep_link,
              psycopg2.extras.Json(data) if data else psycopg2.extras.Json({})))
        conn.commit()
        return dict(watch) if watch else None
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Update watch price failed: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def update_watch_status(watch_id, status, booked_price=None, confirmation=None):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # Store booking details in the JSONB data column when marking booked
        if status == 'booked' and (booked_price or confirmation):
            import json as _json
            booking_data = {}
            if booked_price is not None:
                booking_data['booked_price'] = float(booked_price)
            if confirmation:
                booking_data['confirmation'] = confirmation
            booking_data['booked_at'] = datetime.now(timezone.utc).isoformat()
            cursor.execute("""
                UPDATE crab.member_watches
                SET status = %s, data = COALESCE(data, '{}'::jsonb) || %s::jsonb
                WHERE pk_id = %s
            """, (status, _json.dumps(booking_data), watch_id))
        else:
            cursor.execute("""
                UPDATE crab.member_watches SET status = %s WHERE pk_id = %s
            """, (status, watch_id))
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Update watch status failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_watch_history(watch_id, limit=50):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT * FROM crab.watch_history
            WHERE watch_id = %s
            ORDER BY observed_at DESC LIMIT %s
        """, (watch_id, limit))
        return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Get watch history failed: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
