"""Flight Hunter digest — Mom-first roll-up of observation data.

Structured for the actual user (Andy → Mom decisions), not the data dump.

Sections (in order, top of email):
  1. Mom-primary  — HLN→SEA, today's floor + 30d range + days-to-depart
  2. Drive-alt    — BZN/GTF/MSO → SEA, only if materially cheaper than HLN
  3. Brother routes — HLN→PDX/EUG/SMF/OAK grouped by kid
  4. Notable elsewhere — deal-blog + reddit catches not in Mom's network
  Footer — source health, weighted by jobs-completed

Filters out kayak_alt_destination rows whose dest IS a primary route
(those are duplicates of the primary signal — noise, not discovery).
"""
from datetime import date

NEW_LOW_THRESHOLD = 0.80          # ≤ 80% of 30d route median = "new low"
MEDIAN_WINDOW_DAYS = 30
RECENT_WINDOW_HOURS = 24
DRIVE_ALT_MIN_SAVINGS_USD = 25    # alt-origin only surfaces if it's $25+ cheaper

ORIGIN = 'HLN'                    # Mom's home airport
MOM_PRIMARY = ('HLN', 'SEA')      # the one route that matters most

# Routes we actively crawl (config.py is source of truth, but listed here for filtering).
PRIMARY_DESTS = {'SEA', 'PDX', 'EUG', 'SMF', 'OAK'}
DRIVE_ALT_ORIGINS = ['BZN', 'GTF', 'MSO']    # drive-options from Helena

BROTHER_ROUTES = [
    ('HLN', 'PDX', 'Brother A (Salem)'),
    ('HLN', 'EUG', 'Brother A (Salem, fallback)'),
    ('HLN', 'SMF', 'Brother B (Sacramento)'),
    ('HLN', 'OAK', 'Brother B (Sacramento, fallback)'),
]


def _cheapest_per_date(conn, origin, destination, days=90, exclude_alt_dest=True):
    """For one route, return DISTINCT-ON depart_date with the cheapest current
    observation. Optionally drops kayak_alt_destination rows so they don't
    pollute the primary route's signal."""
    cur = conn.cursor()
    extra = "AND source NOT LIKE 'kayak_alt_destination'" if exclude_alt_dest else ''
    cur.execute(f"""
        SELECT DISTINCT ON (depart_date)
            depart_date, price_usd, airline, stops, source, deep_link
        FROM kumori_ops.flight_hunter_observations
        WHERE origin = %s AND destination = %s
          AND observed_at >= NOW() - INTERVAL %s
          AND depart_date BETWEEN CURRENT_DATE + 2 AND CURRENT_DATE + %s
          AND price_usd >= 50
          {extra}
        ORDER BY depart_date, price_usd ASC
    """, (origin, destination, f'{RECENT_WINDOW_HOURS} hours', days))
    return [
        {'depart_date': d, 'price_usd': float(p), 'airline': a,
         'stops': s, 'source': src, 'deep_link': dl}
        for d, p, a, s, src, dl in cur.fetchall()
    ]


def _route_median(conn, origin, destination):
    """30-day rolling median for one route."""
    cur = conn.cursor()
    cur.execute("""
        SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY price_usd),
               COUNT(*)
        FROM kumori_ops.flight_hunter_observations
        WHERE origin = %s AND destination = %s
          AND observed_at >= NOW() - INTERVAL %s
          AND price_usd >= 50
    """, (origin, destination, f'{MEDIAN_WINDOW_DAYS} days'))
    m, n = cur.fetchone()
    return (float(m) if m else None, n)


def _summarize_route(conn, origin, destination, days=90):
    """Returns dict: cheapest_now, cheapest_date, median_30d, n_obs_24h, n_obs_30d, top3_dates."""
    per_date = _cheapest_per_date(conn, origin, destination, days)
    median, n_30d = _route_median(conn, origin, destination)

    if not per_date:
        return {
            'has_data': False, 'origin': origin, 'destination': destination,
            'median_30d': median, 'n_30d': n_30d,
        }

    # Sort all dates by price ascending; top 3 are the cheapest opportunities
    top3 = sorted(per_date, key=lambda x: x['price_usd'])[:3]
    cheapest = top3[0]
    delta_pct = ((cheapest['price_usd'] - median) / median * 100) if median else None

    return {
        'has_data': True,
        'origin': origin, 'destination': destination,
        'cheapest_now': cheapest['price_usd'],
        'cheapest_date': cheapest['depart_date'],
        'cheapest_airline': cheapest['airline'],
        'cheapest_source': cheapest['source'],
        'cheapest_link': cheapest['deep_link'],
        'median_30d': median,
        'delta_pct': delta_pct,
        'new_low': bool(median) and cheapest['price_usd'] <= median * NEW_LOW_THRESHOLD,
        'n_24h': len(per_date),
        'n_30d': n_30d,
        'top3_dates': top3,
    }


def _notable_elsewhere(conn, exclude_origins=('HLN', 'BZN', 'GTF', 'MSO')):
    """Pull deal-blog + reddit observations that aren't Mom-network — the
    'interesting elsewhere' section. Cap at 5."""
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT ON (origin, destination)
            origin, destination, depart_date, price_usd, airline,
            source, deep_link, raw
        FROM kumori_ops.flight_hunter_observations
        WHERE observed_at >= NOW() - INTERVAL %s
          AND source IN ('theflightdeal', 'reddit_flightdeals',
                         'reddit_awardtravel', 'reddit_shoestring')
          AND origin NOT IN %s
        ORDER BY origin, destination, price_usd ASC
    """, (f'{RECENT_WINDOW_HOURS} hours', tuple(exclude_origins)))
    rows = cur.fetchall()
    # Now rank by absolute cheapness across these
    notable = [
        {'origin': o, 'destination': d, 'depart_date': dd, 'price_usd': float(p),
         'airline': a, 'source': src, 'deep_link': dl, 'raw': raw}
        for o, d, dd, p, a, src, dl, raw in rows
    ]
    notable.sort(key=lambda x: x['price_usd'])
    return notable[:5]


def _source_health(conn):
    """Source counts last 24h, plus distinct-job counts (so high-fanout adapters
    don't look like the only ones working)."""
    cur = conn.cursor()
    cur.execute("""
        SELECT source,
               COUNT(*) AS rows,
               COUNT(DISTINCT (origin, destination, depart_date)) AS jobs
        FROM kumori_ops.flight_hunter_observations
        WHERE observed_at >= NOW() - INTERVAL %s
        GROUP BY source ORDER BY jobs DESC, rows DESC
    """, (f'{RECENT_WINDOW_HOURS} hours',))
    return list(cur.fetchall())


def _totals(conn):
    cur = conn.cursor()
    cur.execute("""
        SELECT
            (SELECT COUNT(*) FROM kumori_ops.flight_hunter_observations
             WHERE observed_at >= NOW() - INTERVAL '24 hours'),
            (SELECT reltuples::BIGINT FROM pg_class
             WHERE oid = 'kumori_ops.flight_hunter_observations'::regclass)
    """)
    n_24h, n_all = cur.fetchone()
    return {'n_24h': n_24h, 'n_all': n_all}


# ── Rendering ───────────────────────────────────────────────────────────
def _fmt_price(p):
    return f'${p:.0f}' if p else '—'


def _fmt_delta(delta_pct):
    if delta_pct is None:
        return ''
    sign = '+' if delta_pct > 0 else ''
    return f' ({sign}{delta_pct:.0f}% vs 30d median)'


def render(facts):
    """Returns (subject, plain_body, html_body) tuple."""
    today = date.today().isoformat()
    mom = facts['mom_primary']
    drive_alts = facts['drive_alts']         # list of route dicts (already filtered to cheaper-than-mom)
    brothers = facts['brothers']
    notable = facts['notable']
    health = facts['source_health']
    totals = facts['totals']

    # ─── Subject ───────────────────────────────────────────────────────
    if mom.get('has_data'):
        subject = (
            f"crab.travel flight_hunter — {today} · "
            f"HLN→SEA from ${mom['cheapest_now']:.0f} "
            f"({mom['cheapest_date']})"
        )
    else:
        subject = f"crab.travel flight_hunter — {today} · no Mom-route data yet"

    # ─── Plain text ────────────────────────────────────────────────────
    lines = []
    lines.append(f"Flight Hunter — {today}")
    lines.append(f"{totals['n_24h']:,} observations in last 24h · {totals['n_all']:,} all-time")
    lines.append('')
    lines.append('=' * 60)

    # Section 1: Mom-primary
    lines.append("MOM'S PRIMARY ROUTE — HLN → SEA")
    lines.append('=' * 60)
    if mom.get('has_data'):
        flag = '  🔥 NEW LOW' if mom.get('new_low') else ''
        lines.append(f"  Cheapest right now: ${mom['cheapest_now']:.0f} "
                     f"on {mom['cheapest_date']}{flag}")
        if mom.get('median_30d'):
            lines.append(f"  30-day median: ${mom['median_30d']:.0f}{_fmt_delta(mom.get('delta_pct'))}")
        if mom.get('cheapest_airline'):
            lines.append(f"  Airline: {mom['cheapest_airline']}  (via {mom['cheapest_source']})")
        lines.append('')
        lines.append('  Top 3 cheapest dates:')
        for d in mom['top3_dates']:
            airline = f" {d['airline']}" if d.get('airline') else ''
            lines.append(f"    ${d['price_usd']:.0f}  {d['depart_date']}{airline}  ({d['source']})")
    else:
        lines.append("  (no observations yet — check that the crawler is running)")
    lines.append('')

    # Section 2: Drive alternatives — render only if any qualify
    if drive_alts:
        lines.append('=' * 60)
        lines.append('DRIVE-ALTERNATIVE ORIGINS (Mom drives instead of HLN)')
        lines.append('=' * 60)
        for alt in drive_alts:
            savings = mom['cheapest_now'] - alt['cheapest_now'] if mom.get('cheapest_now') else 0
            lines.append(f"  {alt['origin']} → {alt['destination']}: "
                         f"${alt['cheapest_now']:.0f} on {alt['cheapest_date']}  "
                         f"(${savings:.0f} cheaper than HLN)")
        lines.append('')

    # Section 3: Brother routes — only routes with data; skip whole section if all empty
    brothers_with_data = [b for b in brothers if b.get('has_data')]
    if brothers_with_data:
        lines.append('=' * 60)
        lines.append('BROTHER ROUTES (for context)')
        lines.append('=' * 60)
        for b in brothers_with_data:
            flag = '  🔥' if b.get('new_low') else ''
            lines.append(f"  HLN → {b['destination']} ({b['kid_label']}): "
                         f"${b['cheapest_now']:.0f} on {b['cheapest_date']}{flag}"
                         f"{_fmt_delta(b.get('delta_pct'))}")
        lines.append('')

    # Section 4: Notable elsewhere
    if notable:
        lines.append('=' * 60)
        lines.append('NOTABLE ELSEWHERE (deal blogs + Reddit, non-Mom routes)')
        lines.append('=' * 60)
        for n in notable:
            airline = f" {n['airline']}" if n.get('airline') else ''
            lines.append(f"  {n['origin']} → {n['destination']}: ${n['price_usd']:.0f}  "
                         f"{n['depart_date']}{airline}  ({n['source']})")
        lines.append('')

    # Footer: source health
    lines.append('-' * 60)
    lines.append('Source health (last 24h) — sorted by jobs-completed:')
    for src, n_rows, n_jobs in health:
        lines.append(f"  {src:30s}  {n_jobs:>4} jobs   {n_rows:>5,} rows")
    lines.append('')
    lines.append('crab.travel · kumori_ops.flight_hunter_observations')
    plain_body = '\n'.join(lines)

    # ─── HTML body ────────────────────────────────────────────────────
    h = []
    h.append(_html_header(today, totals))

    # Section 1: Mom-primary (big, prominent)
    h.append(_html_mom_section(mom))

    # Section 2: Drive alternatives
    h.append(_html_drive_alt_section(mom, drive_alts))

    # Section 3: Brother routes
    h.append(_html_brother_section(brothers))

    # Section 4: Notable elsewhere
    if notable:
        h.append(_html_notable_section(notable))

    # Footer
    h.append(_html_footer(health))

    html_body = (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;'
        'font-size:14px;line-height:1.55;color:#0f172a;max-width:820px;'
        'padding:12px;margin:0 auto">'
        + '\n'.join(h)
        + '</div>'
    )
    return subject, plain_body, html_body


def _html_header(today, totals):
    return (
        f'<h2 style="margin:0 0 0.2em 0;font-size:20px;color:#0f172a">Flight Hunter · {today}</h2>'
        f'<p style="margin:0 0 1.2em 0;color:#64748b;font-size:13px">'
        f'{totals["n_24h"]:,} observations in last 24h · {totals["n_all"]:,} all-time</p>'
    )


def _html_mom_section(mom):
    if not mom.get('has_data'):
        return ('<div style="background:#fef2f2;padding:12px;border-radius:8px;margin-bottom:12px">'
                '<h3 style="margin:0;color:#dc2626">Mom — HLN → SEA</h3>'
                '<p style="margin:8px 0 0 0">No observations yet — check that the crawler is running.</p>'
                '</div>')
    new_low_badge = (
        '<span style="background:#dc2626;color:white;padding:2px 8px;border-radius:4px;'
        'font-size:11px;margin-left:8px;vertical-align:middle">🔥 NEW LOW</span>'
        if mom.get('new_low') else ''
    )
    delta = _fmt_delta(mom.get('delta_pct'))
    median_str = f'30-day median ${mom["median_30d"]:.0f}{delta}' if mom.get('median_30d') else 'gathering median data...'
    airline_str = f' on <b>{mom["cheapest_airline"]}</b>' if mom.get('cheapest_airline') else ''
    link_open, link_close = (
        (f'<a href="{mom["cheapest_link"]}" style="color:inherit;text-decoration:none">', '</a>')
        if mom.get('cheapest_link') else ('', '')
    )

    rows = ''
    for d in mom['top3_dates']:
        airline = f' · {d["airline"]}' if d.get('airline') else ''
        link_o, link_c = (
            (f'<a href="{d["deep_link"]}" style="color:#1e40af;text-decoration:none">', '</a>')
            if d.get('deep_link') else ('', '')
        )
        rows += (
            f'<tr><td style="padding:4px 8px;border-top:1px solid #e2e8f0">'
            f'<b>${d["price_usd"]:.0f}</b></td>'
            f'<td style="padding:4px 8px;border-top:1px solid #e2e8f0">{link_o}{d["depart_date"]}{link_c}</td>'
            f'<td style="padding:4px 8px;border-top:1px solid #e2e8f0;color:#64748b;font-size:12px">'
            f'{d["source"]}{airline}</td></tr>'
        )

    return (
        '<div style="background:#f0f9ff;padding:16px;border-radius:8px;margin-bottom:14px;'
        'border-left:4px solid #2563eb">'
        '<h3 style="margin:0 0 8px 0;font-size:17px">Mom\'s primary route — HLN → SEA</h3>'
        f'<p style="margin:0 0 4px 0;font-size:24px;color:#0f172a">'
        f'{link_open}<b>${mom["cheapest_now"]:.0f}</b>{link_close}'
        f' on {mom["cheapest_date"]}{airline_str}{new_low_badge}</p>'
        f'<p style="margin:0 0 12px 0;color:#64748b;font-size:13px">{median_str}</p>'
        '<p style="margin:0 0 4px 0;color:#475569;font-size:12px;font-weight:600">Cheapest dates:</p>'
        '<table style="border-collapse:collapse;font-size:13px">' + rows + '</table>'
        '</div>'
    )


def _html_drive_alt_section(mom, drive_alts):
    if not drive_alts:
        return ''
    mom_price = mom.get('cheapest_now') or 0
    rows = ''
    for alt in drive_alts:
        savings = mom_price - alt['cheapest_now']
        link_o, link_c = (
            (f'<a href="{alt["cheapest_link"]}" style="color:#16a34a;text-decoration:none">', '</a>')
            if alt.get('cheapest_link') else ('', '')
        )
        rows += (
            f'<tr><td style="padding:6px 8px;border-top:1px solid #d1fae5"><b>{alt["origin"]} → {alt["destination"]}</b></td>'
            f'<td style="padding:6px 8px;border-top:1px solid #d1fae5">{link_o}<b>${alt["cheapest_now"]:.0f}</b>{link_c}</td>'
            f'<td style="padding:6px 8px;border-top:1px solid #d1fae5">{alt["cheapest_date"]}</td>'
            f'<td style="padding:6px 8px;border-top:1px solid #d1fae5;color:#16a34a;font-weight:600">'
            f'-${savings:.0f} vs HLN</td></tr>'
        )
    return (
        '<div style="background:#f0fdf4;padding:12px;border-radius:8px;margin-bottom:12px;'
        'border-left:4px solid #16a34a">'
        '<h3 style="margin:0 0 8px 0;font-size:15px">Drive-alternative origins (Mom drives instead of HLN)</h3>'
        '<table style="border-collapse:collapse;width:100%;font-size:13px">' + rows + '</table>'
        '</div>'
    )


def _html_brother_section(brothers):
    if not any(b.get('has_data') for b in brothers):
        return ''
    rows = ''
    for b in brothers:
        if not b.get('has_data'):
            continue
        flag = ' 🔥' if b.get('new_low') else ''
        delta = b.get('delta_pct')
        delta_str = f' ({delta:+.0f}% vs median)' if delta is not None else ''
        delta_color = '#16a34a' if delta and delta < 0 else '#64748b'
        link_o, link_c = (
            (f'<a href="{b["cheapest_link"]}" style="color:inherit;text-decoration:none">', '</a>')
            if b.get('cheapest_link') else ('', '')
        )
        rows += (
            f'<tr><td style="padding:4px 8px;border-top:1px solid #e2e8f0">'
            f'<b>HLN → {b["destination"]}</b> <span style="color:#94a3b8;font-size:11px">{b["kid_label"]}</span></td>'
            f'<td style="padding:4px 8px;border-top:1px solid #e2e8f0">{link_o}<b>${b["cheapest_now"]:.0f}</b>{link_c}{flag}</td>'
            f'<td style="padding:4px 8px;border-top:1px solid #e2e8f0">{b["cheapest_date"]}</td>'
            f'<td style="padding:4px 8px;border-top:1px solid #e2e8f0;color:{delta_color};font-size:12px">{delta_str}</td></tr>'
        )
    return (
        '<div style="padding:8px 0;margin-bottom:12px">'
        '<h3 style="margin:0 0 6px 0;font-size:15px;color:#0f172a">Brother routes (for context)</h3>'
        '<table style="border-collapse:collapse;width:100%;font-size:13px">' + rows + '</table>'
        '</div>'
    )


def _html_notable_section(notable):
    rows = ''
    for n in notable:
        airline = f' · {n["airline"]}' if n.get('airline') else ''
        link_o, link_c = (
            (f'<a href="{n["deep_link"]}" style="color:#475569;text-decoration:none">', '</a>')
            if n.get('deep_link') else ('', '')
        )
        rows += (
            f'<tr><td style="padding:3px 8px;border-top:1px solid #f1f5f9">{link_o}{n["origin"]} → {n["destination"]}{link_c}</td>'
            f'<td style="padding:3px 8px;border-top:1px solid #f1f5f9"><b>${n["price_usd"]:.0f}</b></td>'
            f'<td style="padding:3px 8px;border-top:1px solid #f1f5f9;color:#64748b">{n["depart_date"]}{airline}</td>'
            f'<td style="padding:3px 8px;border-top:1px solid #f1f5f9;color:#94a3b8;font-size:11px">{n["source"]}</td></tr>'
        )
    return (
        '<div style="padding:8px 0;margin-bottom:12px">'
        '<h3 style="margin:0 0 6px 0;font-size:14px;color:#64748b">Notable elsewhere (deal-blog + Reddit)</h3>'
        '<table style="border-collapse:collapse;width:100%;font-size:12px">' + rows + '</table>'
        '</div>'
    )


def _html_footer(health):
    rows = ''
    for src, n_rows, n_jobs in health:
        rows += (
            f'<tr><td style="padding:2px 8px;font-family:monospace;font-size:11px;color:#475569">{src}</td>'
            f'<td align="right" style="padding:2px 12px;font-size:11px">{n_jobs} jobs</td>'
            f'<td align="right" style="padding:2px 12px;font-size:11px;color:#94a3b8">{n_rows:,} rows</td></tr>'
        )
    return (
        '<details style="margin-top:16px;color:#94a3b8;font-size:12px">'
        '<summary style="cursor:pointer">Source health (24h)</summary>'
        '<table style="border-collapse:collapse;margin-top:6px">' + rows + '</table>'
        '</details>'
        '<p style="margin:16px 0 0 0;color:#cbd5e1;font-size:11px">'
        'crab.travel · kumori_ops.flight_hunter_observations</p>'
    )


# ── Top-level orchestration ─────────────────────────────────────────────
def _fetch_facts(conn):
    """Pull every fact the renderer needs — one bulk-style call set per section."""
    facts = {'totals': _totals(conn)}

    # Mom-primary
    facts['mom_primary'] = _summarize_route(conn, *MOM_PRIMARY)

    # Drive alternatives — only include if materially cheaper than Mom-primary
    mom_price = facts['mom_primary'].get('cheapest_now')
    drive_alts = []
    for o in DRIVE_ALT_ORIGINS:
        s = _summarize_route(conn, o, 'SEA')
        if not s.get('has_data'):
            continue
        if mom_price and (mom_price - s['cheapest_now']) < DRIVE_ALT_MIN_SAVINGS_USD:
            continue
        drive_alts.append(s)
    drive_alts.sort(key=lambda x: x['cheapest_now'])
    facts['drive_alts'] = drive_alts

    # Brother routes
    brothers = []
    for o, d, kid_label in BROTHER_ROUTES:
        s = _summarize_route(conn, o, d)
        s['kid_label'] = kid_label
        brothers.append(s)
    facts['brothers'] = brothers

    # Notable elsewhere
    facts['notable'] = _notable_elsewhere(conn)

    # Source health
    facts['source_health'] = _source_health(conn)

    return facts


def build_and_render(conn):
    """One-shot: fetch + render. Returns (subject, plain, html)."""
    facts = _fetch_facts(conn)
    return render(facts)
