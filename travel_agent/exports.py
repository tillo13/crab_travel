"""
exports — payer-routed trip reports. The capture/match engine never branches on
payer; this is the one place that does.

  client_wrapped → (a) light travel-total summary for the client
                    (b) full deduction package for the tax pipeline
  llc / personal  → full package only (archive)

Files land in travel_agent/_private/exports/<trip_slug>/ by default; pass
dest_dir to drop the tax package into tax_prep (
<tax_prep pipeline>/data/documents) when filing.
Meals are 50% deductible; air/lodging/ground 100%. CPA gets final word —
stated on every package.

CLI:  venv_crab/bin/python -m travel_agent.exports <trip_id> [dest_dir]
"""

import os
import re
import sys
from collections import defaultdict

from travel_agent.schema import get_trip, list_bookings, list_receipts
from travel_agent.reconcile import status as reconcile_status

_EXPORT_ROOT = os.path.join(os.path.dirname(__file__), "_private", "exports")
MEALS_DEDUCTIBLE = 0.5


def build_report(trip_id):
    trip = get_trip(trip_id)
    if not trip:
        raise ValueError(f"no trip {trip_id}")
    receipts = list_receipts(trip_id)
    bookings = list_bookings(trip_id)
    recon = reconcile_status(trip_id)

    by_cat = defaultdict(float)
    unpriced = []
    for r in receipts:
        usd = r.get("amount_usd")
        if usd is None:
            unpriced.append(r)
            continue
        by_cat[r["category"]] += float(usd)

    meals = by_cat.get("meals", 0.0)
    non_meals = sum(v for k, v in by_cat.items() if k != "meals")
    total = meals + non_meals
    deductible = non_meals + meals * MEALS_DEDUCTIBLE
    return {
        "trip": trip, "bookings": bookings, "receipts": receipts,
        "by_category": dict(by_cat), "total_usd": round(total, 2),
        "meals_usd": round(meals, 2),
        "deductible_usd": round(deductible, 2),
        "unpriced_receipts": unpriced, "reconciliation": recon,
    }


def _slug(trip):
    return re.sub(r'[^a-z0-9]+', '_', trip["name"].lower()).strip('_')


def render_client_summary(report):
    """Light total for the client — no receipt detail."""
    t = report["trip"]
    lines = [
        f"# Travel total — {t['name']}",
        f"{t['date_start']} → {t['date_end']}, {t['destination_city']}",
        "",
        f"**Total travel cost: ${report['total_usd']:,.2f} USD**",
        "",
    ]
    for cat, amt in sorted(report["by_category"].items(), key=lambda kv: -kv[1]):
        lines.append(f"- {cat}: ${amt:,.2f}")
    if report["unpriced_receipts"]:
        lines.append(f"- ({len(report['unpriced_receipts'])} receipt(s) pending USD amount — total will move)")
    return "\n".join(lines) + "\n"


def render_tax_package(report):
    """Full deduction package: every receipt with amount/date/place/purpose."""
    t = report["trip"]
    r = report["reconciliation"]
    lines = [
        f"# Trip deduction package — {t['name']}",
        f"- **Dates:** {t['date_start']} → {t['date_end']}",
        f"- **Place:** {t['destination_city']}" + (f" ({t['meeting_address']})" if t.get("meeting_address") else ""),
        f"- **Business purpose:** {t.get('notes') or 'business travel'}",
        f"- **Payer route:** {t['payer']}",
        "",
        "## Totals",
        f"- Total spend: **${report['total_usd']:,.2f}**",
        f"- Meals (50% deductible): ${report['meals_usd']:,.2f} → ${report['meals_usd'] * MEALS_DEDUCTIBLE:,.2f} deductible",
        f"- **Deductible total: ${report['deductible_usd']:,.2f}**",
        "",
        "## Receipts",
        "| date | merchant | category | native | USD | source |",
        "|---|---|---|---|---|---|",
    ]
    for rec in report["receipts"]:
        cap = rec["captured_at"].date().isoformat() if rec["captured_at"] else "?"
        usd = f"${float(rec['amount_usd']):,.2f}" if rec["amount_usd"] is not None else "?"
        lines.append(f"| {cap} | {rec['merchant']} | {rec['category']} | "
                     f"{float(rec['amount_native']):,.2f} {rec['currency']} | {usd} | {rec['source']} |")
    lines += [
        "",
        "## Card reconciliation",
        f"- {r['matched']} matched of {r['card_txns']} trip card txns / {r['receipts']} receipts",
    ]
    for t_ in r["orphan_txns"]:
        lines.append(f"- ⚠️ card charge with NO receipt: {t_['date']} ${t_['amount']:,.2f} "
                     f"{t_['merchant_clean']} ({t_['city']}, {t_['state']})")
    for rec in r["orphan_receipts"]:
        lines.append(f"- ⚠️ receipt with no card match: {rec['merchant']} "
                     f"{rec['amount_native']:,.2f} {rec['currency']}")
    if report["unpriced_receipts"]:
        lines.append(f"- ⚠️ {len(report['unpriced_receipts'])} receipt(s) missing a USD amount")
    lines += ["", "> General business-expense mechanics — CPA gets final word."]
    return "\n".join(lines) + "\n"


def export(trip_id, dest_dir=None):
    report = build_report(trip_id)
    trip = report["trip"]
    out_dir = dest_dir or os.path.join(_EXPORT_ROOT, _slug(trip))
    os.makedirs(out_dir, exist_ok=True)
    written = []

    pkg = os.path.join(out_dir, "deduction_package.md")
    with open(pkg, "w") as f:
        f.write(render_tax_package(report))
    written.append(pkg)

    if trip["payer"] == "client_wrapped":
        cl = os.path.join(out_dir, "travel_total_for_client.md")
        with open(cl, "w") as f:
            f.write(render_client_summary(report))
        written.append(cl)

    return {"deductible_usd": report["deductible_usd"], "total_usd": report["total_usd"],
            "files": written}


if __name__ == "__main__":
    trip_id = int(sys.argv[1])
    dest = sys.argv[2] if len(sys.argv) > 2 else None
    result = export(trip_id, dest)
    print(f"total ${result['total_usd']:,.2f} | deductible ${result['deductible_usd']:,.2f}")
    for p in result["files"]:
        print(f"  → {p}")
