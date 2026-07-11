"""
gf_links — generate exact-filter Google Flights URLs via the `tfs=` protobuf.

GF encodes every search filter in a base64url protobuf; this module was built
by decoding live samples, corroborated against the reverse-engineered spec
(gist MomoDeve/a18053dea84dd28e320b8b2c489540eb, AWeirdDev/flights), and
verified byte-identical against real booking links.
Board book-buttons should use these — land on a one-flight list, not a search.

Field map (verified live): top level — 1:query_mode=28, 2:query_context=2,
3:legs[], 8:passengers(1=adult), 9:cabin(1=econ 2=PE 3=biz 4=first),
14:display_flag=1, 16:all-results sentinel, 19:trip(1=RT 2=oneway).
Leg — 2:date "YYYY-MM-DD", 5:max_stops(0=nonstop), 6:include_airlines,
7:exclude_airlines, 8/9:depart hour bucket start/end-inclusive,
10/11:arrival hour buckets, 13/14:origin/dest {1:1, 2:"IATA"}.
"""

import base64


def _varint(n):
    out = b''
    while True:
        b7 = n & 0x7f
        n >>= 7
        out += bytes([b7 | (0x80 if n else 0)])
        if not n:
            return out


def _field(fno, wt, payload=b'', v=None):
    tag = _varint((fno << 3) | wt)
    if wt == 0:
        return tag + _varint(v)
    return tag + _varint(len(payload)) + payload


def _s(fno, txt): return _field(fno, 2, txt.encode())
def _msg(fno, inner): return _field(fno, 2, inner)
def _vi(fno, v): return _field(fno, 0, v=v)
def _place(iata): return _vi(1, 1) + _s(2, iata)

CABIN = {"economy": 1, "premium": 2, "business": 3, "first": 4}


def flight_url(origin, dest, date, cabin="economy", airlines=None, max_stops=None,
               depart_hours=None, arrive_hours=None, one_way=True, adults=1):
    """Exact-filter GF search URL.

    depart_hours/arrive_hours: (start_hour, end_hour_inclusive) 24h buckets —
    e.g. (7, 7) = departures 7:00-7:59 AM.
    """
    leg = _s(2, date)
    if max_stops is not None:
        leg += _vi(5, max_stops)
    for a in (airlines or []):
        leg += _s(6, a)
    if depart_hours:
        leg += _vi(8, depart_hours[0]) + _vi(9, depart_hours[1])
    if arrive_hours:
        leg += _vi(10, arrive_hours[0]) + _vi(11, arrive_hours[1])
    leg += _msg(13, _place(origin)) + _msg(14, _place(dest))

    tfs = (_vi(1, 28) + _vi(2, 2) + _msg(3, leg))
    for _ in range(adults):
        tfs += _vi(8, 1)
    tfs += (_vi(9, CABIN[cabin]) + _vi(14, 1)
            + _msg(16, _vi(1, 18446744073709551615))
            + _vi(19, 2 if one_way else 1))
    tok = base64.urlsafe_b64encode(tfs).decode().rstrip("=")
    return f"https://www.google.com/travel/flights/search?tfs={tok}&curr=USD"


if __name__ == "__main__":
    # example: early-morning one-stop First on a specific carrier
    print(flight_url("JFK", "DEN", "2026-09-01", cabin="first",
                     airlines=["UA"], max_stops=1, depart_hours=(7, 9)))
