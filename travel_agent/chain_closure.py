"""
chain_closure — the small-airport chain closure engine: hop from a close
regional airport, premium cabin from the gateway. Born from a live planning
session's fresh multi-query sweep. hunt.py v2 calls this for every trip.

Given scraped hop options (home→spoke) and big-leg options (spoke→dest, per
cabin), computes which chains CLOSE: big leg departs >= hop arrival + buffer,
flags red-eye-class departures and post-curfew arrivals per prefs. Pure
parsing/logic — scraping stays in the pw layer, judgment stays with Claude.

Input format: lists of Google Flights aria-label strings (the pw scrape
pattern documented in gf_scrape_template.js).
"""

import re

BUFFER_MIN = 120           # separate tickets: minimum self-connect buffer
REDEYE_AFTER_MIN = 21 * 60 # departures 9 PM+ = red-eye class (prefs: banned-ish)
CURFEW_MIN = 23 * 60       # arrivals after ~11 PM = soft-dispreferred (prefs)


def parse_labels(items):
    """GF aria-label strings → normalized flight dicts (price, times, airline)."""
    out = []
    for it in items:
        p = re.search(r'From (\d+) US dollars', it)
        s = re.search(r'(Nonstop|\d+ stops?)', it)
        a = re.search(r'flights? with ([A-Za-z ]+?)\.', it)
        du = re.search(r'Total duration (\d+) hr(?: (\d+) min)?', it)
        times = re.findall(r'(\d{1,2}):(\d{2})\s?([AP])M', it)
        if not (p and times):
            continue

        def mins(t):
            h, m, ap = int(t[0]), int(t[1]), t[2]
            return (h % 12 + (12 if ap == 'P' else 0)) * 60 + m

        dep = mins(times[0])
        arr = mins(times[1]) if len(times) > 1 else None
        plus1 = arr is not None and arr < dep
        # CABIN-MIX AUDIT (the "Economy + First Class" catch): GF "first
        # class" queries include mixed-cabin itineraries; the composition is
        # IN the label. Extract it and flag economy legs — a premium option
        # with an economy leg must NEVER be presented as First.
        cab = re.search(
            r'((?:First Class|Business Class|Premium Economy|Economy)'
            r'(?:\s*\+\s*(?:First Class|Business Class|Premium Economy|Economy))+'
            r'|(?:First Class|Business Class|Premium Economy))\s*(?:Layover|\d+ carry)', it)
        cabins = cab.group(1).strip() if cab else None
        out.append({
            'cabins': cabins,
            'has_economy_leg': bool(cabins and 'Economy' in cabins and 'Premium Economy' not in cabins.replace('Premium Economy', '')),
            'price': int(p.group(1)),
            'stops': s.group(1) if s else '?',
            'airline': (a.group(1) if a else '?').strip(),
            'dep_min': dep, 'arr_min': arr, 'plus1': plus1,
            'dur_min': (int(du.group(1)) * 60 + int(du.group(2) or 0)) if du else None,
            'dep_str': f"{times[0][0]}:{times[0][1]} {times[0][2]}M",
            'arr_str': (f"{times[1][0]}:{times[1][1]} {times[1][2]}M" + ("+1" if plus1 else "")) if len(times) > 1 else '?',
        })
    return out


def close_chains(hops, big_legs, buffer_min=BUFFER_MIN):
    """For each nonstop hop, find big legs that depart >= arrival + buffer.
    Returns list of closures with red-eye / curfew flags for the judgment layer."""
    closures = []
    for h in sorted((x for x in parse_labels(hops) if x['stops'] == 'Nonstop'),
                    key=lambda x: x['dep_min']):
        if h['arr_min'] is None:
            continue
        ready = h['arr_min'] + buffer_min
        for b in sorted(parse_labels(big_legs), key=lambda x: (x['dep_min'], x['price'])):
            if b['dep_min'] < ready:
                continue
            closures.append({
                'hop': h, 'big': b,
                'total_fare': h['price'] + b['price'],
                'redeye_class': b['dep_min'] >= REDEYE_AFTER_MIN,
                'past_curfew': b['plus1'] or (b['arr_min'] is not None and b['arr_min'] > CURFEW_MIN),
                'buffer_min': b['dep_min'] - h['arr_min'],
            })
    return closures
