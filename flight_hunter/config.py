"""Flight Hunter — route set + scan parameters.

Mom flies from HLN primary, but BZN/GTF/MSO are realistic drive-alternatives
(90-120 min drives). Mom-flexibility includes WHICH airport she leaves from,
not just dates. The hunter scans all four origins → SEA so Andy can answer
"should Mom drive to Bozeman this week?" with real data, not luck.

Brother routes (PDX/EUG/SMF/OAK) only crawl from HLN since the brothers
visit Mom, not drive-alt → brother.

See docs/flight_hunter_buildout.md for the design.
"""

# (origin, destination, kid_label, fallback)
# `fallback=True` means only surface this route in the digest if it's
# materially cheaper than its primary peer.
ROUTE_PAIRS = [
    # ── Mom → Andy (Everett) — Mom-primary + 3 drive alternatives ──
    ('HLN', 'SEA', "Andy (Everett)",            False),
    ('BZN', 'SEA', "Andy (Everett) — Mom drives 90min to Bozeman",  True),
    ('GTF', 'SEA', "Andy (Everett) — Mom drives 90min to Great Falls", True),
    ('MSO', 'SEA', "Andy (Everett) — Mom drives 2hr to Missoula",   True),
    # ── Mom → Brother A (Salem OR) ──
    ('HLN', 'PDX', "Brother A (Salem OR)",      False),
    ('HLN', 'EUG', "Brother A (Salem OR), fallback", True),
    # ── Mom → Brother B (Sacramento) ──
    ('HLN', 'SMF', "Brother B (Sacramento)",    False),
    ('HLN', 'OAK', "Brother B (Sacramento), fallback", True),
]

# Default origin reference (still used by docs / scripts that want Mom-primary)
ORIGIN = 'HLN'

# Origins we actually fly from — Andy (Seattle) + Mom (Helena & her realistic
# drive-alternative airports). The dynamic job source (active member watches) only
# emits routes whose origin is in this set, so junk/test watches with arbitrary
# origins (JFK, JNB, MAD, ORD, …) never consume a scan slot on the VPS. This is the
# geographic scoping for "hunt my active watches" — keep it in lockstep with the
# origins present in ROUTE_PAIRS above.
HOME_ORIGINS = {'SEA', 'HLN', 'BZN', 'GTF', 'MSO'}

# Back-compat for any older code that did `from config import ROUTES` and got
# the old (dest, label, mins, fallback) shape — left empty so it never silently
# returns wrong data.
ROUTES = []   # deprecated — use ROUTE_PAIRS

SCAN_HORIZON_DAYS = 90
NEW_LOW_THRESHOLD = 0.80

MAX_STOPS = 1
MIN_LAYOVER_MINUTES = 45
DIGEST_TOP_N = 5

# Surfacing threshold — drive-alts only show when they beat HLN by this much.
DRIVE_ALT_MIN_SAVINGS_USD = 25
