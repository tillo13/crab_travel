"""
extract — validate + record a receipt extraction into crab.ta_receipts.

The PARSING is done in-session (Claude reads the receipt image / email text
directly — free on Max, no API key in the loop). This module is the
deterministic half: field validation, category guard, USD passthrough, writer.

Session contract — hand record() a dict like:
  {"merchant": "Corner Bistro", "amount_native": 84.30, "currency": "CAD",
   "amount_usd": 61.40,            # null if unknown; reconcile can band-match CAD
   "category": "meals",            # meals|lodging|air|ground|other (meals = 50% deductible)
   "captured_at": "2026-09-02T19:22:00-04:00",   # or null
   "source": "email"|"photo", "gcs_path": null, "extracted_json": {...raw fields...}}

CLI (JSON on stdin or a file path):
  venv_crab/bin/python -m travel_agent.extract <trip_id> [receipt.json]
"""

import json
import sys

from travel_agent.schema import CATEGORIES, add_receipt

REQUIRED = ("merchant", "amount_native", "currency")


def normalize(raw):
    missing = [k for k in REQUIRED if not raw.get(k)]
    if missing:
        raise ValueError(f"receipt missing required fields: {missing}")
    out = dict(raw)
    out["amount_native"] = round(float(out["amount_native"]), 2)
    out["currency"] = out["currency"].strip().upper()
    if out.get("amount_usd") is not None:
        out["amount_usd"] = round(float(out["amount_usd"]), 2)
    elif out["currency"] == "USD":
        out["amount_usd"] = out["amount_native"]
    cat = (out.get("category") or "other").strip().lower()
    out["category"] = cat if cat in CATEGORIES else "other"
    out.setdefault("source", "email")
    return out


def record(trip_id, raw):
    r = normalize(raw)
    return add_receipt(
        trip_id,
        merchant=r["merchant"],
        amount_native=r["amount_native"],
        currency=r["currency"],
        amount_usd=r.get("amount_usd"),
        category=r["category"],
        source=r["source"],
        gcs_path=r.get("gcs_path"),
        captured_at=r.get("captured_at"),
        extracted_json=r.get("extracted_json"),
    )


if __name__ == "__main__":
    trip_id = int(sys.argv[1])
    payload = open(sys.argv[2]).read() if len(sys.argv) > 2 else sys.stdin.read()
    data = json.loads(payload)
    for raw in (data if isinstance(data, list) else [data]):
        rid = record(trip_id, raw)
        print(f"receipt {rid}: {raw['merchant']} {raw['amount_native']} {raw['currency']}")
