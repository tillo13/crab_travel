"""
Traveler preference brain — the template.

This file ships with GENERIC EXAMPLE values so the structure is readable and
reusable. The real traveler's configuration lives in
travel_agent/_private/prefs_private.json under "prefs_override" (gitignored)
and is deep-merged over these defaults by load_prefs(). Loyalty numbers, KTN,
and passport data live in the same vault — never in this file.

The structure below was distilled from a full live trip-planning session
(hunt → decide → book, with the human in the loop). Every field earned its
place by changing a real decision at least once.
"""

import json
import os

_PRIVATE_PATH = os.path.join(os.path.dirname(__file__), "_private", "prefs_private.json")

PREFS = {
    # ── Geography & ground access ─────────────────────────────
    "home_airport": "AAA",            # primary big airport
    "home_metro_state": "XX",
    # In-trip-window card charges in these cities = home/family spend, NOT trip
    # spend (the reconciler's killer classifier).
    "home_metro_cities": ["HOMETOWN", "NEIGHBORTOWN"],
    # A small nearby airport can beat the big one when ground access differs:
    # price the DRIVE/RIDE in dollars AND minutes on every option.
    "alt_origins": ["BBB"],
    "airport_access": {"BBB": {"drive_minutes": 15, "access_cost_rt_usd": 0},
                       "AAA": {"uber_each_way_usd": 100, "access_cost_rt_usd": 200}},
    "alt_origin_premium_worth_usd": 400,  # what the close airport is worth, all-in

    # ── Airlines ──────────────────────────────────────────────
    # Loyalty should be INSTRUMENTAL (miles/upgrades), never a trump card.
    # A good premium-cabin deal on any airline beats the preferred brand.
    "airlines": {"preferred": ["XX"], "feeder_from_alt_origin": "any carrier"},

    # ── The ultimate setup (route-shape preference) ───────────
    # Short hop from the close airport (any carrier, any cabin, window seat),
    # then premium cabin from the gateway onward. Chains score by DETOUR MILES
    # (extra flown vs point-to-point), not just price.
    "ultimate_setup": {"hop": "short, any carrier, any cabin (window)",
                       "main_leg": "premium cabin, good deal, preferred brand when sane"},

    # ── Flight rules ──────────────────────────────────────────
    "flight": {
        "cabin": "economy", "max_connections": 2,
        "layovers_ok": True,
        "layover_sweet_spot_hours": 2, "layover_wearing_hours": 3,   # soft
        # Seat: window > aisle > never-middle (the one absolute). Always
        # prefer an empty adjacent seat — read the live seat map.
        "seat": ["window", "aisle"], "never_middle": True, "prefer_empty_adjacent": True,
        "avoid_redeyes": True,                       # soft — steep deals reopen it
        "late_arrival_dispreferred_after": "~23:00", # soft, harder before commitments
        "avoid_dawn_departures_before": "~08:00",    # soft — never pay silly $/hr for it
        "first_class_deals": True, "first_deals_any_airline": True,
        "decline_checkout_insurance": True,
    },

    # ── Value lenses (the traveler's decision aids) ───────────
    # ¢/mi = RT all-in ÷ point-to-point RT miles.
    # $/flight-hr = (premium fare − Main, same flight) ÷ RT seat-hours.
    #   Community worth-it band ~$50–100/hr: under = snap-buy, over = aspiration.
    # Domestic/transborder First is recliner-First — pay deal prices only.
    "value_lenses": {"cents_per_mile": True, "dollars_per_flight_hour_band": [50, 100]},

    # ── Hard process rules (each one prevented a real mistake) ─
    "board_stats_mandatory": True,     # every option shows the full metric strip
    "verify_metal_and_pitch": True,    # "First" is a label; the aircraft is the product
    "check_adjacent_days": True,       # Sat/Sun starts priced vs hotel + lost evening
    "exhaust_competitors_before_buy": True,

    # ── Misc ──────────────────────────────────────────────────
    "hotel": {"walkable_to_meeting": True},
    "return_arrival": "midday_home_beats_traffic",
    "fx_match_tolerance": 0.03,
    "travel_card": "one card for ALL trip spend — the reconciliation trick",
    "unknowns": [],                    # collect conversationally, store in _private
}


def _deep_merge(base, override):
    out = dict(base)
    for k, v in override.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_prefs():
    prefs = dict(PREFS)
    if os.path.exists(_PRIVATE_PATH):
        vault = json.load(open(_PRIVATE_PATH))
        prefs = _deep_merge(prefs, vault.get("prefs_override", {}))
        # legacy: top-level keys written before the prefs_override convention
        prefs = _deep_merge(prefs, {k: v for k, v in vault.items()
                                    if k not in ("prefs_override", "loyalty", "identity")})
    return prefs


def is_home_metro(city, state, prefs=None):
    # Suffix match, not equality — parsed card-descriptor "cities" can carry
    # trailing merchant words; the boundary is ambiguous.
    p = prefs or load_prefs()
    if (state or "").strip().upper() != p["home_metro_state"]:
        return False
    c = (city or "").strip().upper()
    return any(c == m or c.endswith(" " + m) for m in p["home_metro_cities"])
