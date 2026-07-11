"""
Trip planning DB ops: availability, destinations, voting, recs, dates.
Split from utilities/postgres_utils.py for kumori 1000-line compliance.
"""
import logging
import os
import psycopg2
import psycopg2.extras
from utilities.postgres_utils import db_cursor, get_db_connection

logger = logging.getLogger(__name__)


# === Availability ===

def save_member_availability(plan_id, user_id, windows, source='calendar'):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # Clear old entries for this source
        cursor.execute("""
            DELETE FROM crab.member_availability
            WHERE plan_id = %s AND user_id = %s AND source = %s
        """, (plan_id, user_id, source))
        for w in windows:
            cursor.execute("""
                INSERT INTO crab.member_availability (plan_id, user_id, available_start, available_end, source)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (plan_id, user_id, w['start'], w['end'], source))
        conn.commit()
        logger.info(f"💾 Saved {len(windows)} availability windows for user {user_id} in plan {plan_id}")
        return True
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Save availability failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_plan_availability(plan_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT a.*, u.full_name, u.home_airport
            FROM crab.member_availability a
            JOIN crab.users u ON u.pk_id = a.user_id
            WHERE a.plan_id = %s
            ORDER BY a.available_start
        """, (plan_id,))
        return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"❌ Get availability failed: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_availability_overlap(plan_id):
    """Find date ranges where the most members are available."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Get all availability windows
        cursor.execute("""
            SELECT user_id, available_start, available_end
            FROM crab.member_availability
            WHERE plan_id = %s
        """, (plan_id,))
        rows = cursor.fetchall()
        if not rows:
            return []

        # Get total member count
        cursor.execute("SELECT COUNT(DISTINCT user_id) as total FROM crab.plan_members WHERE plan_id = %s", (plan_id,))
        total = cursor.fetchone()['total']

        # Build date→user count map
        from datetime import timedelta, date as date_type
        date_users = {}
        for r in rows:
            d = r['available_start']
            while d <= r['available_end']:
                if d not in date_users:
                    date_users[d] = set()
                date_users[d].add(r['user_id'])
                d += timedelta(days=1)

        if not date_users:
            return []

        # Find contiguous windows with counts
        sorted_dates = sorted(date_users.keys())
        windows = []
        window_start = sorted_dates[0]
        prev_date = sorted_dates[0]
        prev_count = len(date_users[sorted_dates[0]])

        for d in sorted_dates[1:]:
            count = len(date_users[d])
            if d == prev_date + timedelta(days=1) and count == prev_count:
                prev_date = d
            else:
                windows.append({
                    'start': window_start.isoformat(),
                    'end': prev_date.isoformat(),
                    'days': (prev_date - window_start).days + 1,
                    'available_count': prev_count,
                    'total_members': total,
                })
                window_start = d
                prev_date = d
                prev_count = count

        windows.append({
            'start': window_start.isoformat(),
            'end': prev_date.isoformat(),
            'days': (prev_date - window_start).days + 1,
            'available_count': prev_count,
            'total_members': total,
        })

        # Sort by most people available, then longest duration
        windows.sort(key=lambda w: (-w['available_count'], -w['days']))
        return windows
    except Exception as e:
        logger.error(f"❌ Get availability overlap failed: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

# === Destinations ===

def create_destination_suggestion(plan_id, user_id, destination_name):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            INSERT INTO crab.destination_suggestions (plan_id, suggested_by, destination_name)
            VALUES (%s, %s, %s)
            RETURNING suggestion_id, destination_name, status
        """, (plan_id, user_id, destination_name))
        row = cursor.fetchone()
        conn.commit()
        return dict(row)
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Create destination suggestion failed: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def update_destination_suggestion(suggestion_id, data):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE crab.destination_suggestions SET
                destination_data = %s, avg_flight_cost = %s, avg_hotel_cost = %s,
                avg_total_cost = %s, compatibility_score = %s, status = %s
            WHERE suggestion_id = %s
        """, (
            psycopg2.extras.Json(data.get('destination_data', {})),
            data.get('avg_flight_cost'),
            data.get('avg_hotel_cost'),
            data.get('avg_total_cost'),
            data.get('compatibility_score'),
            data.get('status', 'ready'),
            suggestion_id,
        ))
        conn.commit()
        return True
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Update destination suggestion failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_destination_suggestions(plan_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT d.*, u.full_name as suggested_by_name
            FROM crab.destination_suggestions d
            LEFT JOIN crab.users u ON u.pk_id = d.suggested_by
            WHERE d.plan_id = %s
            ORDER BY d.created_at
        """, (plan_id,))
        return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"❌ Get destination suggestions failed: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_destination_suggestion_by_id(suggestion_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("SELECT * FROM crab.destination_suggestions WHERE suggestion_id = %s", (suggestion_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"❌ Get suggestion failed: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def update_destination_data(suggestion_id, destination_data):
    """Update only the destination_data JSONB field without touching other columns."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE crab.destination_suggestions SET destination_data = %s WHERE suggestion_id = %s",
            (psycopg2.extras.Json(destination_data), suggestion_id),
        )
        conn.commit()
        return True
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Update destination data failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def delete_destination_suggestion(suggestion_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # Delete related votes first
        cursor.execute("DELETE FROM crab.votes WHERE target_type = 'destination' AND target_id = %s", (str(suggestion_id),))
        cursor.execute("DELETE FROM crab.destination_suggestions WHERE suggestion_id = %s", (suggestion_id,))
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Delete destination suggestion failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

# === Voting + lock_plan ===

def upsert_vote(plan_id, user_id, target_type, target_id, vote):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO crab.votes (plan_id, user_id, target_type, target_id, vote)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (plan_id, user_id, target_type, target_id) DO UPDATE SET
                vote = EXCLUDED.vote, created_at = NOW()
        """, (plan_id, user_id, target_type, target_id, vote))
        conn.commit()
        return True
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Upsert vote failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def delete_vote(plan_id, user_id, target_type, target_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            DELETE FROM crab.votes
            WHERE plan_id = %s AND user_id = %s AND target_type = %s AND target_id = %s
        """, (plan_id, user_id, target_type, target_id))
        conn.commit()
        return True
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Delete vote failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def clear_rank_from_others(plan_id, user_id, target_type, target_id, rank):
    """When a user assigns rank N to a destination, remove rank N from their other destinations."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            DELETE FROM crab.votes
            WHERE plan_id = %s AND user_id = %s AND target_type = %s
              AND target_id != %s AND vote = %s
        """, (plan_id, user_id, target_type, target_id, rank))
        conn.commit()
        return True
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Clear rank from others failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_vote_tallies(plan_id, target_type=None):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        sql = """
            SELECT target_type, target_id, vote as rank, COUNT(*) as count
            FROM crab.votes
            WHERE plan_id = %s AND vote > 0
        """
        params = [plan_id]
        if target_type:
            sql += " AND target_type = %s"
            params.append(target_type)
        sql += " GROUP BY target_type, target_id, vote ORDER BY target_id, vote"
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        # Group by target_id and build rank distribution
        tallies = {}
        for r in rows:
            tid = r['target_id']
            if tid not in tallies:
                tallies[tid] = {'target_type': r['target_type'], 'target_id': tid, 'ranks': {}, 'total_votes': 0}
            tallies[tid]['ranks'][int(r['rank'])] = int(r['count'])
            tallies[tid]['total_votes'] += int(r['count'])
        return list(tallies.values())
    except Exception as e:
        logger.error(f"❌ Get vote tallies failed: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_user_votes(plan_id, user_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT target_type, target_id, vote
            FROM crab.votes
            WHERE plan_id = %s AND user_id = %s
        """, (plan_id, user_id))
        return {f"{r['target_type']}:{r['target_id']}": r['vote'] for r in cursor.fetchall()}
    except Exception as e:
        logger.error(f"❌ Get user votes failed: {e}")
        return {}
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_all_member_votes(plan_id):
    """Get all destination votes grouped by user_id for organizer dashboard."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT v.user_id, v.target_id, v.vote, m.display_name
            FROM crab.votes v
            JOIN crab.plan_members m ON m.plan_id = v.plan_id AND m.user_id = v.user_id
            WHERE v.plan_id = %s AND v.target_type = 'destination'
        """, (plan_id,))
        results = {}
        for r in cursor.fetchall():
            uid = r['user_id']
            if uid not in results:
                results[uid] = {'display_name': r['display_name'], 'votes': {}}
            results[uid]['votes'][r['target_id']] = r['vote']
        return results
    except Exception as e:
        logger.error(f"Get all member votes failed: {e}")
        return {}
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def lock_plan(plan_id, destination, start_date=None, end_date=None):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE crab.plans SET
                locked_destination = %s, locked_start_date = %s, locked_end_date = %s,
                status = 'locked', updated_at = NOW()
            WHERE plan_id = %s
        """, (destination, start_date, end_date, plan_id))
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Lock plan failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

# === Recommendations ===

def save_recommendations(plan_id, recs):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        for r in recs:
            cursor.execute("""
                INSERT INTO crab.recommendations (plan_id, category, title, description,
                    price_estimate, compatibility_score, ai_reasoning)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                plan_id, r['category'], r['title'], r.get('description'),
                r.get('price_estimate'), r.get('compatibility_score'),
                r.get('ai_reasoning'),
            ))
        conn.commit()
        logger.info(f"💾 Saved {len(recs)} recommendations for plan {plan_id}")
        return True
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Save recommendations failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_recommendations(plan_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT * FROM crab.recommendations
            WHERE plan_id = %s
            ORDER BY category, compatibility_score DESC
        """, (plan_id,))
        return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"❌ Get recommendations failed: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def update_recommendation_status(recommendation_id, status):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE crab.recommendations SET status = %s
            WHERE recommendation_id = %s
        """, (status, recommendation_id))
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Update recommendation status failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def delete_recommendations_for_plan(plan_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM crab.recommendations WHERE plan_id = %s", (plan_id,))
        conn.commit()
        return True
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Delete recommendations failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

# === Blackouts + tentative dates + stage + delete + member details ===

def save_member_blackouts(plan_id, user_id, blackouts):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM crab.member_blackouts WHERE plan_id = %s AND user_id = %s", (plan_id, user_id))
        for b in blackouts:
            cursor.execute("""
                INSERT INTO crab.member_blackouts (plan_id, user_id, blackout_start, blackout_end)
                VALUES (%s, %s, %s, %s)
            """, (plan_id, user_id, b['start'], b['end']))
        conn.commit()
        return True
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Save blackouts failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_plan_blackouts(plan_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT b.*, u.full_name
            FROM crab.member_blackouts b
            JOIN crab.users u ON u.pk_id = b.user_id
            WHERE b.plan_id = %s
            ORDER BY b.blackout_start
        """, (plan_id,))
        return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"❌ Get blackouts failed: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_member_blackouts(plan_id, user_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT blackout_start, blackout_end
            FROM crab.member_blackouts
            WHERE plan_id = %s AND user_id = %s
            ORDER BY blackout_start
        """, (plan_id, user_id))
        return [{'start': r['blackout_start'].isoformat(), 'end': r['blackout_end'].isoformat()} for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"❌ Get member blackouts failed: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def save_member_tentative_dates(plan_id, user_id, dates):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM crab.member_tentative_dates WHERE plan_id = %s AND user_id = %s", (plan_id, user_id))
        for d in dates:
            cursor.execute("""
                INSERT INTO crab.member_tentative_dates (plan_id, user_id, date_start, date_end, preference)
                VALUES (%s, %s, %s, %s, %s)
            """, (plan_id, user_id, d['start'], d['end'], d.get('preference', 'works')))
        conn.commit()
        return True
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Save tentative dates failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_member_tentative_dates(plan_id, user_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT date_start, date_end, COALESCE(preference, 'works') as preference
            FROM crab.member_tentative_dates
            WHERE plan_id = %s AND user_id = %s
            ORDER BY date_start
        """, (plan_id, user_id))
        return [{'start': r['date_start'].isoformat(), 'end': r['date_end'].isoformat(), 'preference': r['preference']} for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Get tentative dates failed: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def update_plan_stage(plan_id, stage):
    """Update plan status/stage (voting, planning, locked)."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE crab.plans SET status = %s, updated_at = NOW() WHERE plan_id = %s", (stage, plan_id))
        conn.commit()
        return True
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Update plan stage failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def get_plan_tentative_dates(plan_id):
    """Get all tentative dates for all members in a plan."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cursor.execute("""
            SELECT t.*, COALESCE(t.preference, 'works') as preference, u.full_name
            FROM crab.member_tentative_dates t
            JOIN crab.users u ON u.pk_id = t.user_id
            WHERE t.plan_id = %s
            ORDER BY t.date_start
        """, (plan_id,))
        return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Get plan tentative dates failed: {e}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def delete_plan(plan_id, organizer_id):
    """Delete a plan and all related data (CASCADE handles children)."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM crab.plans WHERE plan_id = %s AND organizer_id = %s",
            (plan_id, organizer_id)
        )
        deleted = cursor.rowcount
        conn.commit()
        logger.info(f"🗑️ Plan deleted: {plan_id} (rows={deleted})")
        return deleted > 0
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Delete plan failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def update_member_details(member_id, home_airport=None, is_flexible=None):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        parts = []
        vals = []
        if home_airport is not None:
            parts.append("home_airport = %s")
            vals.append(home_airport.upper().strip() if home_airport else None)
        if is_flexible is not None:
            parts.append("is_flexible = %s")
            vals.append(is_flexible)
        if not parts:
            return True
        vals.append(member_id)
        cursor.execute(f"UPDATE crab.plan_members SET {', '.join(parts)} WHERE pk_id = %s", vals)
        conn.commit()
        return True
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Update member details failed: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
