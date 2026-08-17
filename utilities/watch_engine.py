"""
Watch engine — decision tier for member price-watches. Adapter scanning
moved to the OpenClaw VPS skill watch-scanner on 2026-05-11 per the
discovery-vs-decision rule; this module now only:

  1. Creates watches when a plan locks (create_watches_for_plan).
  2. Evaluates accumulated observations from crab.watch_history
     (evaluate_pending_alerts, called by /tasks/evaluate-alerts every 1h).
  3. Holds the alert decision logic (_make_alert_decision, _compute_baseline,
     _send_alert_v2) — pure SQL, no external API calls, no Playwright.

Observations are written by /api/opencrab/watch-observations (called from
the VPS dispatcher). See _local_infrastructure/openclaw/skills/watch-scanner.
"""

import logging
from datetime import date, datetime, timezone
from utilities.postgres_utils import (
    get_plan_by_id, get_plan_members, get_all_plan_preferences,
    create_member_watch, get_active_watches, get_watch_history,
)
logger = logging.getLogger(__name__)


def create_watches_for_plan(plan_id):
    """Auto-create flight + hotel watches for every member when a plan locks."""
    plan = get_plan_by_id(plan_id)
    if not plan:
        logger.error(f"Watch create: plan {plan_id} not found")
        return 0

    destination = plan.get('locked_destination') or plan.get('destination')
    checkin = plan.get('locked_start_date') or plan.get('start_date')
    checkout = plan.get('locked_end_date') or plan.get('end_date')

    if not destination:
        logger.warning(f"Watch create: plan {plan_id} has no destination, skipping")
        return 0

    members = get_plan_members(plan_id)
    prefs = get_all_plan_preferences(plan_id)
    prefs_by_member = {p['member_id']: p for p in prefs}

    # Resolve destination to IATA once. If unresolvable, flight watches
    # can't be priced (no Kayak/Travelpayouts lookups work without an
    # airport code) — skip flight watch creation so the queue stays clean.
    # Hotel watches still get created since Hotellook accepts city names.
    from utilities.search_engine import _destination_iata
    flight_dest_iata = _destination_iata(destination) if destination else None
    import re as _re
    has_flight_dest = bool(flight_dest_iata and _re.fullmatch(r'[A-Z]{3}', flight_dest_iata))
    if not has_flight_dest:
        logger.info(f"Watch create: {destination!r} has no IATA airport — flight watches skipped")

    created = 0
    for member in members:
        member_id = member['pk_id']
        pref = prefs_by_member.get(member_id, {})
        budget_max = pref.get('budget_max')

        # Flight watch — needs a home airport AND a destination airport
        home_airport = member.get('home_airport') or member.get('user_home_airport')
        if home_airport and has_flight_dest:
            watch = create_member_watch(
                plan_id=plan_id, member_id=member_id, watch_type='flight',
                origin=home_airport, destination=flight_dest_iata,
                checkin=checkin, checkout=checkout, budget_max=budget_max,
            )
            if watch:
                created += 1
                logger.info(f"Watch created: flight {home_airport}→{flight_dest_iata} for {member['display_name']}")
        elif not home_airport:
            logger.warning(f"Watch skip: no home airport for {member['display_name']}, skipping flight watch")

        # Hotel watch
        watch = create_member_watch(
            plan_id=plan_id, member_id=member_id, watch_type='hotel',
            destination=destination, checkin=checkin, checkout=checkout,
            budget_max=budget_max,
        )
        if watch:
            created += 1
            logger.info(f"Watch created: hotel in {destination} for {member['display_name']}")

    logger.info(f"Watch create complete: {created} watches for plan {plan_id}")
    return created


def _expire_past_travel_dates():
    """Lifecycle sweep — a watch/leg whose travel date has passed can never be
    scanned again (discovery filters on checkin/depart >= CURRENT_DATE), so
    leaving it 'active' pollutes every active-count downstream. The 2026-08-17
    heartbeat showed 🔴 'last check 279h ago' over 7 past-checkin watches that
    no scanner would ever touch again. Mirrors the discovery predicates in
    opencrab_routes.py (watches-to-scan, legs-to-hunt).
    """
    from utilities.postgres_utils import get_db_connection
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE crab.member_watches
            SET status = 'expired'
            WHERE status = 'active'
              AND checkin IS NOT NULL
              AND checkin < CURRENT_DATE
        """)
        watches = cur.rowcount
        cur.execute("""
            UPDATE crab.trip_legs
            SET status = 'expired', updated_at = NOW()
            WHERE status = 'active'
              AND depart_window_start IS NOT NULL
              AND depart_window_start < CURRENT_DATE
        """)
        legs = cur.rowcount
        conn.commit()
        if watches or legs:
            logger.info(f"lifecycle sweep: expired {watches} watch(es), "
                        f"{legs} leg(s) with past travel dates")
    except Exception as e:
        logger.error(f"lifecycle sweep failed: {e}")
    finally:
        conn.close()


def evaluate_pending_alerts(observation_window_minutes=90):
    """Decision-tier pass — called by /tasks/evaluate-alerts cron.

    Reads recent observations from crab.watch_history (written by OpenClaw
    via /api/opencrab/watch-observations), groups them by watch, runs the
    alert decision per watch. No external API calls; no Playwright. Pure
    decision work that completes in seconds.

    For each active watch:
      1. Pull most-recent observation per (source) within the window.
      2. Build the quotes list (one entry per source).
      3. Run _make_alert_decision.
      4. If it returns a decision, call _send_alert_v2 (ledger-gated).
      5. Recompute the AI recommendation against the new observations.
    """
    from utilities.postgres_utils import get_db_connection
    import psycopg2.extras

    _expire_past_travel_dates()

    watches = get_active_watches()  # joins plan_members + users; gives contact fields
    if not watches:
        return {'evaluated': 0, 'alerts_sent': 0, 'errors': []}

    today = date.today()
    evaluated = 0
    alerts_sent = 0
    errors = []

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    # Prefilter: only watches that actually have recent observations need
    # decision-pass work. Skips 4000+ empty iterations over watches that
    # haven't been scanned by OpenClaw yet (or are unscannable).
    try:
        cur.execute("""
            SELECT DISTINCT watch_id FROM crab.watch_history
            WHERE observed_at > NOW() - %s::interval
        """, (f'{observation_window_minutes} minutes',))
        active_watch_ids = {r['watch_id'] for r in cur.fetchall()}
    except Exception as e:
        conn.close()
        logger.error(f"evaluate_pending_alerts: prefilter failed: {e}")
        return {'evaluated': 0, 'alerts_sent': 0, 'errors': [str(e)]}

    watches = [w for w in watches if w['pk_id'] in active_watch_ids]
    if not watches:
        conn.close()
        logger.info("evaluate_pending_alerts: no watches with recent observations")
        return {'evaluated': 0, 'alerts_sent': 0, 'errors': []}

    try:
        for w in watches:
            try:
                checkin = w.get('checkin')
                if isinstance(checkin, str):
                    checkin = date.fromisoformat(checkin)
                if checkin:
                    days_out = (checkin - today).days
                    if days_out < 0 or days_out > 60:
                        continue

                cur.execute("""
                    SELECT DISTINCT ON (source)
                           source, price_usd, deep_link, data, observed_at
                    FROM crab.watch_history
                    WHERE watch_id = %s
                      AND observed_at > NOW() - %s::interval
                    ORDER BY source, observed_at DESC
                """, (w['pk_id'], f'{observation_window_minutes} minutes'))
                rows = cur.fetchall()
                if not rows:
                    continue

                quotes = []
                for r in rows:
                    jdata = r['data'] if isinstance(r['data'], dict) else (r['data'] or {})
                    honors = jdata.get('deep_link_honors_price') if isinstance(jdata, dict) else None
                    if honors is None:
                        src = r['source'] or ''
                        bare = src.replace('openclaw_', '')
                        honors = bare in _HONORABLE_DEEP_LINK_SOURCES
                    quotes.append({
                        'source': r['source'],
                        'price_usd': float(r['price_usd']),
                        'deep_link': r['deep_link'],
                        'deep_link_honors_price': bool(honors),
                        'data': jdata,
                    })

                evaluated += 1
                decision = _make_alert_decision(w, quotes, channel='email')
                if decision and _send_alert_v2(w, decision):
                    alerts_sent += 1

                try:
                    rec = compute_recommendation(w)
                    _update_recommendation(w['pk_id'], rec)
                except Exception as e:
                    logger.warning(f"Recommendation failed for watch {w['pk_id']}: {e}")

            except Exception as e:
                # Roll back the aborted transaction so the next iteration's
                # cursor isn't poisoned.
                try:
                    conn.rollback()
                except Exception:
                    pass
                logger.error(f"evaluate watch {w.get('pk_id')} failed: {e}")
                errors.append(f"watch {w.get('pk_id')}: {e}")
    finally:
        cur.close()
        conn.close()

    summary = {'evaluated': evaluated, 'alerts_sent': alerts_sent, 'errors': errors}
    logger.info(f"evaluate_pending_alerts complete: {summary}")
    return summary


# Fallback allowlist used only when a quote doesn't carry an explicit
# deep_link_honors_price flag (older adapters or external scan results).
# Current adapters all set the flag — TravelpayoutsAdapter / LiteAPIAdapter
# set True (links to their own affiliate surfaces); DuffelAdapter sets False
# (Kayak fallback URL the adapter doesn't own).
_HONORABLE_DEEP_LINK_SOURCES = {'travelpayouts', 'liteapi'}


def _quote_honors_deep_link(quote):
    """Whether this quote's deep_link target shows the quoted price.

    Adapters can override by setting quote['deep_link_honors_price']
    explicitly. Otherwise fall back to source-name allowlist.
    """
    if 'deep_link_honors_price' in quote:
        return bool(quote['deep_link_honors_price'])
    return quote.get('source') in _HONORABLE_DEEP_LINK_SOURCES


def _compute_baseline(watch_id, days=14, trim_pct=0.1, min_observations=5):
    """Trimmed median of watch_history prices over the last `days` days.

    Trim: drop the top + bottom `trim_pct` of observations before taking median.
    Robust against single-adapter glitches AND against a brief deep discount
    that we don't want to anchor on.

    Returns float or None (insufficient data). 'None' is the signal to skip
    alerting — we don't have a defensible baseline yet.
    """
    from utilities.postgres_utils import get_db_connection
    import psycopg2.extras
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT price_usd FROM crab.watch_history
            WHERE watch_id = %s AND observed_at >= NOW() - %s::interval
            ORDER BY observed_at DESC
        """, (watch_id, f'{days} days'))
        prices = [float(r['price_usd']) for r in cur.fetchall() if r['price_usd']]
    except Exception as e:
        logger.error(f"baseline compute failed for watch {watch_id}: {e}")
        return None
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

    if len(prices) < min_observations:
        return None
    prices.sort()
    trim = int(len(prices) * trim_pct)
    trimmed = prices[trim:len(prices) - trim] if trim > 0 else prices
    if not trimmed:
        return None
    n = len(trimmed)
    return trimmed[n // 2] if n % 2 else (trimmed[n // 2 - 1] + trimmed[n // 2]) / 2


def _record_skip(watch_id, reason, **kw):
    """Log to crab.watch_alert_skips for OpenClaw daily visibility."""
    from utilities.postgres_utils import get_db_connection
    import psycopg2.extras
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO crab.watch_alert_skips
            (watch_id, reason, quoted_usd, observed_usd, source, deep_link, detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (
            watch_id, reason, kw.get('quoted_usd'), kw.get('observed_usd'),
            kw.get('source'), kw.get('deep_link'),
            psycopg2.extras.Json(kw.get('detail') or {}),
        ))
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        logger.warning(f"watch_alert_skips insert failed for {watch_id}: {e}")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


def _make_alert_decision(watch, quotes, channel='email'):
    """Decide whether to send a price-drop alert, and return the decision dict.

    Returns None when no alert should fire. Returns dict with everything
    needed by _send_alert_v2 + the ledger insert otherwise.

    Decision logic (production-grade — no single bug above this layer should
    cause user spam):

      1. Need quotes. If none, skip.
      2. Baseline = 14d trimmed median from watch_history (min 5 obs).
         If insufficient history → quiet learning mode, no alert.
      3. drop_pct = (baseline - min_quote) / baseline. Must clear threshold.
      4. Corroboration:
         - ≥2 distinct sources within 20% of the min, OR
         - 1 source whose deep_link honors the price (allowlisted adapters).
         Otherwise → skip with reason='no_corroboration' (logged).
      5. Source disagreement guardrail: if max quote is >25% above min quote,
         this is an adapter quote-quality red flag → skip with
         reason='adapter_disagreement' even if min looks like a drop.
      6. Ledger insert with ON CONFLICT — same (watch, channel, price_band,
         week-bucket) can't fire twice.
    """
    if not quotes:
        return None

    threshold_pct = watch.get('alert_threshold_pct') or 10
    min_quote = min(quotes, key=lambda q: q['price_usd'])
    max_quote = max(quotes, key=lambda q: q['price_usd'])
    min_price = min_quote['price_usd']
    max_price = max_quote['price_usd']

    # Source disagreement check (only meaningful when ≥2 sources)
    if len(quotes) >= 2 and min_price > 0:
        spread_pct = ((max_price - min_price) / min_price) * 100
        if spread_pct > 25:
            _record_skip(
                watch['pk_id'], 'adapter_disagreement',
                quoted_usd=min_price, observed_usd=max_price,
                source=min_quote.get('source'),
                detail={
                    'spread_pct': round(spread_pct, 1),
                    'sources': sorted({q['source'] for q in quotes}),
                    'min': min_price, 'max': max_price,
                },
            )
            return None

    baseline = _compute_baseline(watch['pk_id'])
    if baseline is None or baseline <= 0:
        return None  # quiet — not enough history yet

    drop_pct = ((baseline - min_price) / baseline) * 100
    if drop_pct < threshold_pct:
        return None

    # Corroboration
    sources_close = {
        q['source'] for q in quotes
        if q['price_usd'] <= min_price * 1.20
    }
    has_honorable_link = any(
        _quote_honors_deep_link(q) and q['price_usd'] <= min_price * 1.05
        for q in quotes
    )
    if len(sources_close) < 2 and not has_honorable_link:
        _record_skip(
            watch['pk_id'], 'no_corroboration',
            quoted_usd=min_price, observed_usd=float(baseline),
            source=min_quote.get('source'), deep_link=min_quote.get('deep_link'),
            detail={
                'sources_close': sorted(sources_close),
                'has_honorable_link': has_honorable_link,
            },
        )
        return None

    # The advertised price MUST match the deep_link target. Prefer the
    # cheapest *honorable* quote; if none exists, fall back to the min and
    # accept that step 4 (live verification) will sanity-check before sending.
    honorable_quotes = sorted(
        (q for q in quotes if _quote_honors_deep_link(q)),
        key=lambda q: q['price_usd'],
    )
    preferred = honorable_quotes[0] if honorable_quotes else min_quote
    advertised_price = preferred['price_usd']

    # Re-check the threshold against the advertised price — the user-visible
    # number has to clear the bar, not just the cheapest internal quote.
    advertised_drop_pct = ((baseline - advertised_price) / baseline) * 100
    if advertised_drop_pct < threshold_pct:
        _record_skip(
            watch['pk_id'], 'honorable_quote_below_threshold',
            quoted_usd=advertised_price, observed_usd=float(baseline),
            source=preferred.get('source'), deep_link=preferred.get('deep_link'),
            detail={
                'min_quote_price': min_price,
                'min_quote_source': min_quote.get('source'),
                'advertised_drop_pct': round(advertised_drop_pct, 2),
                'threshold_pct': threshold_pct,
            },
        )
        return None

    price_band = (int(advertised_price) // 25) * 25
    return {
        'channel': channel,
        'baseline_usd': float(baseline),
        'alert_usd': float(advertised_price),
        'drop_pct': round(float(advertised_drop_pct), 2),
        'sources_corroborating': len(sources_close),
        'price_band': price_band,
        'deep_link': preferred.get('deep_link'),
        'deep_link_source': preferred.get('source'),
        'deep_link_honors_price': _quote_honors_deep_link(preferred),
        'min_quote_source': min_quote.get('source'),
        'min_quote_price': float(min_price),
    }




def compute_recommendation(watch, history=None):
    """Analyze price history + timing and return a booking recommendation.

    Returns dict: {verdict, reason, trend, scans, computed_at}
    verdict is one of: 'book_now', 'book_soon', 'wait', 'watching'
    """
    if history is None:
        history = get_watch_history(watch['pk_id'], limit=50)

    prices = [float(h['price_usd']) for h in reversed(history)] if history else []
    n_scans = len(prices)
    now = datetime.now(timezone.utc).date()

    # Days until departure
    checkin = watch.get('checkin')
    if isinstance(checkin, str):
        checkin = date.fromisoformat(checkin)
    days_out = (checkin - now).days if checkin else 999

    current_price = prices[-1] if prices else None
    best_price = float(watch['best_price_usd']) if watch.get('best_price_usd') else current_price

    if not current_price or n_scans < 2:
        return {
            'verdict': 'watching',
            'reason': f'Gathering data — {n_scans} scan{"s" if n_scans != 1 else ""} so far. Need a few more to spot trends.',
            'trend': 'flat',
            'scans': n_scans,
            'computed_at': datetime.now(timezone.utc).isoformat(),
        }

    # Compute trend from recent prices
    recent = prices[-min(5, n_scans):]
    if len(recent) >= 2:
        avg_first_half = sum(recent[:len(recent)//2]) / max(len(recent)//2, 1)
        avg_second_half = sum(recent[len(recent)//2:]) / max(len(recent) - len(recent)//2, 1)
        pct_change = ((avg_second_half - avg_first_half) / avg_first_half) * 100 if avg_first_half > 0 else 0
    else:
        pct_change = 0

    if pct_change < -3:
        trend = 'dropping'
    elif pct_change > 3:
        trend = 'rising'
    else:
        trend = 'stable'

    # How close to best price?
    pct_from_best = ((current_price - best_price) / best_price * 100) if best_price > 0 else 0

    # Consecutive drops
    consecutive_drops = 0
    for i in range(len(prices) - 1, 0, -1):
        if prices[i] < prices[i - 1]:
            consecutive_drops += 1
        else:
            break

    # Decision logic
    verdict = 'wait'
    reason = ''

    watch_type_label = 'flight' if watch.get('watch_type') == 'flight' else 'hotel'

    if days_out <= 7:
        verdict = 'book_now'
        reason = f'Only {days_out} days until departure. {watch_type_label.title()} prices almost never drop this close to travel.'
    elif days_out <= 14:
        if trend == 'dropping':
            verdict = 'book_soon'
            reason = f'Prices are dropping but you\'re {days_out} days out. Could dip a bit more, but don\'t wait too long.'
        else:
            verdict = 'book_now'
            reason = f'{days_out} days out and prices are {trend}. This is the booking window — waiting is risky.'
    elif pct_from_best <= 2 and n_scans >= 4:
        verdict = 'book_now'
        if pct_from_best == 0:
            reason = f'This is the lowest price we\'ve seen across {n_scans} scans. Strong buy signal.'
        else:
            reason = f'Within 2% of the best price we\'ve tracked. After {n_scans} scans, this is a good deal.'
    elif trend == 'dropping' and consecutive_drops >= 3:
        verdict = 'wait'
        reason = f'Prices dropped {consecutive_drops} scans in a row (↓{abs(pct_change):.0f}%). Trend is in your favor — let it ride.'
    elif trend == 'dropping':
        verdict = 'wait'
        reason = f'Prices trending down over recent scans. {days_out} days out gives you room to wait for a better deal.'
    elif trend == 'rising' and days_out > 30:
        verdict = 'book_soon'
        reason = f'Prices are climbing (↑{pct_change:.0f}% recently). Still {days_out} days out, but the trend isn\'t great.'
    elif trend == 'rising' and days_out <= 30:
        verdict = 'book_now'
        reason = f'Prices rising and only {days_out} days out. The longer you wait, the more you\'ll pay.'
    elif trend == 'stable' and n_scans >= 6:
        verdict = 'book_soon'
        reason = f'Prices have been flat across {n_scans} scans. Unlikely to drop much — book when ready.'
    else:
        verdict = 'wait'
        reason = f'{n_scans} scans over {days_out} days out. Prices look {trend} — watching for a better entry point.'

    return {
        'verdict': verdict,
        'reason': reason,
        'trend': trend,
        'scans': n_scans,
        'current_price': current_price,
        'best_price': best_price,
        'pct_from_best': round(pct_from_best, 1),
        'days_out': days_out,
        'computed_at': datetime.now(timezone.utc).isoformat(),
    }


def _update_recommendation(watch_id, recommendation):
    """Store the computed recommendation in the member_watches table."""
    from utilities.postgres_utils import get_db_connection
    import psycopg2.extras
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE crab.member_watches SET recommendation = %s WHERE pk_id = %s
        """, (psycopg2.extras.Json(recommendation), watch_id))
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Update recommendation failed for watch {watch_id}: {e}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def _send_alert_v2(watch, decision):
    """Send a price-drop alert only if it's a genuinely NEW LOW the user hasn't
    already seen.

    Gate = a true price FLOOR over a retention window: suppress unless this price
    is materially below the lowest price we've already alerted for this
    watch+channel. Replaces the old (price_band, dedupe_bucket) key that re-fired
    the same deal on a Thursday epoch rollover or a $25 band straddle (the
    SEA→MIA $260-3× incident). The material margin is the watch's own
    alert_threshold_pct (default 10%), so a tighter-threshold user isn't
    over-suppressed.

    The INSERT ... WHERE NOT EXISTS makes the decision + ledger write one atomic
    statement, so overlapping cron runs can't both fire. RETURNING tells us
    whether we actually inserted (and therefore should send).

    Returns True on send, False if suppressed/deduped.
    """
    from utilities.postgres_utils import get_db_connection

    ALERT_RETENTION_DAYS = 30
    alert_price = float(decision['alert_usd'])
    channel = decision['channel']
    # Clamp <100 to keep the floor anchor finite (alert_threshold_pct is normally 5–30).
    material_pct = min(float(watch.get('alert_threshold_pct') or 10), 90.0)
    # A prior alert "covers" this one (→ suppress) when its price is at or below
    # this anchor — i.e. the new price is not material_pct cheaper than something
    # we already told the user about.
    floor_anchor = alert_price / (1.0 - material_pct / 100.0)

    conn = None
    cur = None
    alert_pk = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO crab.watch_alerts
              (watch_id, baseline_usd, alert_usd, drop_pct,
               sources_corroborating, price_band, channel, deep_link, verified)
            SELECT %(wid)s, %(base)s, %(price)s, %(drop)s, %(src)s, %(band)s,
                   %(chan)s, %(link)s, FALSE
            WHERE NOT EXISTS (
                SELECT 1 FROM crab.watch_alerts
                WHERE watch_id = %(wid)s
                  AND channel = %(chan)s
                  AND sent_at > NOW() - make_interval(days => %(retention)s)
                  AND alert_usd <= %(anchor)s
            )
            RETURNING pk_id
        """, {
            'wid': watch['pk_id'], 'base': decision['baseline_usd'],
            'price': alert_price, 'drop': decision['drop_pct'],
            'src': decision['sources_corroborating'], 'band': decision['price_band'],
            'chan': channel, 'link': decision.get('deep_link'),
            'retention': ALERT_RETENTION_DAYS, 'anchor': floor_anchor,
        })
        row = cur.fetchone()
        conn.commit()
        alert_pk = row[0] if row else None
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"watch_alerts gate/insert failed for {watch.get('pk_id')}: {e}")
        return False
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

    if alert_pk is None:
        logger.info(
            f"alert dedupe: watch {watch['pk_id']} channel={channel} "
            f"${alert_price:.0f} not a material new low vs last {ALERT_RETENTION_DAYS}d "
            f"(material {material_pct:.0f}%) — suppressed"
        )
        return False

    # Ledger row in place. With OpenClaw scanning, Kayak observations (real
    # Playwright pulls of the booking page) ARE the verifier — they vote alongside
    # Travelpayouts in the corroboration check back in _make_alert_decision.
    try:
        from utilities.notification_utils import notify_price_drop
        notify_price_drop(
            watch, decision['baseline_usd'], alert_price,
            deep_link=decision.get('deep_link'),
        )
        # Mark ledger row verified — by pk_id (exact, race-free).
        try:
            conn2 = get_db_connection()
            cur2 = conn2.cursor()
            cur2.execute("""
                UPDATE crab.watch_alerts
                SET verified = TRUE, verified_at_price = %s
                WHERE pk_id = %s
            """, (alert_price, alert_pk))
            conn2.commit()
            cur2.close(); conn2.close()
        except Exception as e:
            logger.warning(f"ledger verify-flag update failed for watch {watch.get('pk_id')}: {e}")
        return True
    except Exception as e:
        logger.error(f"Watch alert send failed for watch {watch.get('pk_id')}: {e}")
        return False
