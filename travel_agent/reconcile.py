"""
reconcile — card-statement CSV ↔ receipts matcher. Runs entirely LOCAL against
_private/cards.db; only receipt rows (already sanitized) come down from the
cloud. Raw statements never leave this machine.

Statement CSV format (field-verified against a major US issuer's export):
  Status,Date,Description,Debit,Credit
  - Date MM/DD/YYYY, no time of day
  - city/state embedded in the Description tail ("SPRINGFIELD IL", "VANCOUVER BC")
  - Debit = charge, Credit = payment/refund

CLI, from repo root:
  venv_crab/bin/python -m travel_agent.reconcile ingest <csv_path> [start] [end]
  venv_crab/bin/python -m travel_agent.reconcile classify <trip_id>
  venv_crab/bin/python -m travel_agent.reconcile match <trip_id> [fx_rate]
  venv_crab/bin/python -m travel_agent.reconcile status <trip_id>
"""

import csv
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from difflib import SequenceMatcher

from travel_agent.prefs import load_prefs, is_home_metro
from travel_agent.schema import get_trip, list_receipts, local_db

# CAD→USD plausibility band for receipts with no known USD amount (card posts
# converted USD; exact rate varies by settlement day).
CAD_USD_BAND = (0.66, 0.80)
MATCH_THRESHOLD = 0.55


# ── Descriptor cleaning (adapted from a proven personal-finance loader) ──

def _strip_suffixes(d):
    # 'null' and card-number suffixes can stack ("... null XXXXXXXXXXXX5303")
    while True:
        new = re.sub(r'\s+(XXXXXXXXXXXX\d+|null)$', '', d, flags=re.I)
        if new == d:
            return d
        d = new


def clean_desc(d):
    cleaned = _strip_suffixes(d)
    cleaned = re.sub(r'^(SQ \*|TST\*|PY \*|GDP\*|@|PP\*|SP |PAYPAL \*|DD \*)', '', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned.strip().upper())
    return cleaned


def parse_location(desc):
    """Pull (city, state/province) off the descriptor tail. City = up to 3
    trailing alpha words — merchant/city boundary is ambiguous in card
    descriptors, so downstream matching uses substring/suffix, never equality.
    Returns (None, state) for phone-number 'cities', (None, None) for web
    merchants with no location."""
    d = _strip_suffixes(desc.strip()).upper()
    m = re.search(r'((?:[A-Z][A-Z.\'-]*\s+){1,3})([A-Z]{2})$', d)
    if not m:
        return None, None
    city, state = m.group(1).strip(), m.group(2)
    if re.search(r'\d', city):
        return None, state
    return city, state


def merchant_only(desc):
    """clean_desc minus the trailing city/state — the matchable merchant string."""
    cleaned = clean_desc(desc)
    city, state = parse_location(desc)
    if city and state:
        cleaned = re.sub(re.escape(f"{city} {state}") + r'$', '', cleaned).strip()
    elif state:
        cleaned = re.sub(r'\s[A-Z]{2}$', '', cleaned).strip()
    return cleaned


# ── Ingest ────────────────────────────────────────────────────

def load_statement_csv(path, start=None, end=None):
    """Parse a statement CSV export → list of txn dicts (dates ISO). start/end filter inclusive."""
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                date = datetime.strptime(r["Date"].strip(), "%m/%d/%Y").date().isoformat()
            except (ValueError, KeyError):
                continue
            if (start and date < start) or (end and date > end):
                continue
            debit = (r.get("Debit") or "").strip()
            credit = (r.get("Credit") or "").strip()
            amount = float(debit) if debit else -float(credit or 0)
            desc = r["Description"].strip().strip('"')
            city, state = parse_location(desc)
            rows.append({
                "date": date, "merchant_raw": desc, "merchant_clean": merchant_only(desc),
                "city": city, "state": state, "amount": amount,
            })
    return rows


def ingest(path, start=None, end=None):
    """Idempotent load into local card_transactions: for each (date, raw, amount)
    key, inserts only the count the CSV has beyond what's already stored —
    re-running the same file adds nothing, real same-day duplicates survive."""
    rows = load_statement_csv(path, start, end)
    incoming = Counter((r["date"], r["merchant_raw"], r["amount"]) for r in rows)
    by_key = {(r["date"], r["merchant_raw"], r["amount"]): r for r in rows}
    conn = local_db()
    added = 0
    try:
        for key, want in incoming.items():
            have = conn.execute(
                "SELECT COUNT(*) FROM card_transactions WHERE date=? AND merchant_raw=? AND amount=?",
                key).fetchone()[0]
            r = by_key[key]
            for _ in range(max(0, want - have)):
                conn.execute("""INSERT INTO card_transactions
                    (statement_source, date, merchant_raw, merchant_clean, city, state, amount)
                    VALUES ('card',?,?,?,?,?,?)""",
                    (r["date"], r["merchant_raw"], r["merchant_clean"], r["city"], r["state"], r["amount"]))
                added += 1
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM card_transactions").fetchone()[0]
        return {"parsed": len(rows), "added": added, "total_in_db": total}
    finally:
        conn.close()


# ── Classify (the home-metro exclusion) ──────────────────────

def classify(trip_id):
    """Tag in-window txns: destination city → trip (set trip_id); home metro →
    excluded_home_metro; anything else stays untagged for the conversation."""
    trip = get_trip(trip_id)
    prefs = load_prefs()
    dest = (trip["destination_city"] or "").upper()
    start, end = trip["date_start"].isoformat(), trip["date_end"].isoformat()
    # pad a day for late posting
    end_pad = (trip["date_end"] + timedelta(days=1)).isoformat()

    conn = local_db()
    tagged = excluded = unknown = 0
    try:
        txns = conn.execute(
            "SELECT * FROM card_transactions WHERE date BETWEEN ? AND ? AND trip_id IS NULL",
            (start, end_pad)).fetchall()
        for t in txns:
            is_fx_fee = "FOREIGN TRANSACTION FEE" in t["merchant_raw"].upper()
            if (t["city"] and dest and dest in t["city"]) or is_fx_fee:
                # in-window FX-fee lines ride with the trip's foreign charges
                conn.execute("UPDATE card_transactions SET trip_id=? WHERE id=?", (trip_id, t["id"]))
                tagged += 1
            elif is_home_metro(t["city"], t["state"], prefs):
                exists = conn.execute(
                    "SELECT 1 FROM reconciliations WHERE card_txn_id=? AND status='excluded_home_metro'",
                    (t["id"],)).fetchone()
                if not exists:
                    conn.execute(
                        "INSERT INTO reconciliations (card_txn_id, status) VALUES (?,'excluded_home_metro')",
                        (t["id"],))
                excluded += 1
            else:
                unknown += 1
        conn.commit()
        return {"window": (start, end_pad), "tagged_trip": tagged,
                "excluded_home_metro": excluded, "needs_judgment": unknown}
    finally:
        conn.close()


# ── Match ─────────────────────────────────────────────────────

def _amount_score(card_amt, receipt, fx_rate=None):
    usd = receipt.get("amount_usd")
    native = float(receipt["amount_native"])
    if usd:
        diff = abs(card_amt - float(usd)) / float(usd)
        return 1.0 if diff <= 0.001 else (0.8 if diff <= 0.03 else 0.0)
    if receipt["currency"] != "USD" and native:
        implied = card_amt / native
        if fx_rate and abs(implied - fx_rate) / fx_rate <= 0.03:
            return 0.9
        lo, hi = CAD_USD_BAND
        return 0.7 if lo <= implied <= hi else 0.0
    return 0.0


def _date_score(card_date, captured_at):
    if not captured_at:
        return 0.5  # unknown receipt time — neutral
    days = abs((datetime.fromisoformat(card_date).date() - captured_at.date()).days)
    return {0: 1.0, 1: 0.9, 2: 0.8, 3: 0.7}.get(days, 0.4 if days <= 5 else 0.0)


def _merchant_score(clean_card, merchant):
    a, b = clean_card.upper(), (merchant or "").upper()
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def match(trip_id, fx_rate=None):
    """Greedy best-first matching of trip card txns ↔ trip receipts.
    Writes 'matched' rows to local reconciliations; orphans are computed by status()."""
    receipts = list_receipts(trip_id)
    conn = local_db()
    try:
        already = {r["card_txn_id"] for r in conn.execute(
            "SELECT card_txn_id FROM reconciliations WHERE status='matched'")}
        matched_receipts = {r["receipt_id"] for r in conn.execute(
            "SELECT receipt_id FROM reconciliations WHERE status='matched'")}
        txns = [t for t in conn.execute(
            "SELECT * FROM card_transactions WHERE trip_id=? AND amount > 0", (trip_id,))
            if t["id"] not in already]

        pairs = []
        for t in txns:
            for r in receipts:
                if r["pk_id"] in matched_receipts:
                    continue
                conf = round(0.5 * _amount_score(t["amount"], r, fx_rate)
                             + 0.3 * _merchant_score(t["merchant_clean"], r["merchant"])
                             + 0.2 * _date_score(t["date"], r["captured_at"]), 3)
                if conf >= MATCH_THRESHOLD:
                    pairs.append((conf, t["id"], r["pk_id"], t["merchant_clean"], r["merchant"]))

        pairs.sort(reverse=True)
        used_t, used_r, results = set(), set(), []
        for conf, tid, rid, tm, rm in pairs:
            if tid in used_t or rid in used_r:
                continue
            used_t.add(tid); used_r.add(rid)
            conn.execute("""INSERT INTO reconciliations
                (card_txn_id, receipt_id, match_confidence, status) VALUES (?,?,?,'matched')""",
                (tid, rid, conf))
            results.append({"confidence": conf, "card": tm, "receipt": rm})
        conn.commit()
        return results
    finally:
        conn.close()


def status(trip_id):
    """Where things stand: matches + both orphan lists, ready for the conversation."""
    receipts = list_receipts(trip_id)
    conn = local_db()
    try:
        matched_t = {r["card_txn_id"] for r in conn.execute(
            "SELECT card_txn_id FROM reconciliations WHERE status='matched'")}
        matched_r = {r["receipt_id"] for r in conn.execute(
            "SELECT receipt_id FROM reconciliations WHERE status='matched'")}
        txns = list(conn.execute(
            "SELECT * FROM card_transactions WHERE trip_id=? AND amount > 0 ORDER BY date", (trip_id,)))
        orphan_txns = [dict(t) for t in txns if t["id"] not in matched_t]
        orphan_receipts = [{"pk_id": r["pk_id"], "merchant": r["merchant"],
                            "amount_native": float(r["amount_native"]), "currency": r["currency"]}
                           for r in receipts if r["pk_id"] not in matched_r]
        return {"card_txns": len(txns), "receipts": len(receipts),
                "matched": len(matched_t & {t["id"] for t in txns}),
                "orphan_txns": orphan_txns, "orphan_receipts": orphan_receipts}
    finally:
        conn.close()


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "ingest":
        print(ingest(sys.argv[2], *(sys.argv[3:5] or [])))
    elif mode == "classify":
        print(classify(int(sys.argv[2])))
    elif mode == "match":
        fx = float(sys.argv[3]) if len(sys.argv) > 3 else None
        for m in match(int(sys.argv[2]), fx):
            print(m)
    elif mode == "status":
        s = status(int(sys.argv[2]))
        print(f"card txns: {s['card_txns']}  receipts: {s['receipts']}  matched: {s['matched']}")
        for t in s["orphan_txns"]:
            print(f"  orphan txn: {t['date']} ${t['amount']:.2f} {t['merchant_clean']} ({t['city']}, {t['state']})")
        for r in s["orphan_receipts"]:
            print(f"  orphan receipt: {r['merchant']} {r['amount_native']} {r['currency']}")
    else:
        sys.exit(f"unknown mode {mode}")
