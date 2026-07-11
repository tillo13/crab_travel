"""
email_sweep — pull booking confirmations + emailed receipts out of Gmail.

Deterministic fetch only: this module searches and returns candidate messages;
Claude (the session running /trip) reads them, judges what's a real
confirmation, and records it via schema.add_booking / add_receipt. No LLM
calls in here.

Auth: the unified token (google-credentials skill) — Gmail send-only helpers in
crab/kumori don't read mail, so this is the one new Gmail READER, on the
canonical token. LOCAL tool; never deployed.

Run from repo root:
  venv_crab/bin/python -m travel_agent.email_sweep confirmations 2026-09-01
  venv_crab/bin/python -m travel_agent.email_sweep receipts 2026-09-01 2026-09-04
  venv_crab/bin/python -m travel_agent.email_sweep body <message_id>
"""

import base64
import os
import pickle
import re
import sys

TOKEN_PATH = os.path.expanduser("~/Desktop/code/kumori/credentials/google_token.pickle")

_service = None

CONFIRMATION_SENDERS = ("delta.com OR alaskaair.com OR aircanada.com OR united.com "
                        "OR booking.com OR expedia.com OR marriott.com OR hilton.com "
                        "OR airbnb.com OR hotels.com")
RECEIPT_TERMS = "(receipt OR invoice OR \"your order\" OR \"payment to\" OR fare OR trip)"


def _get_service():
    global _service
    if _service:
        return _service
    from googleapiclient.discovery import build
    from google.auth.transport.requests import Request
    with open(TOKEN_PATH, "rb") as f:
        creds = pickle.load(f)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_PATH, "wb") as f:
            pickle.dump(creds, f)
    _service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return _service


def _header(msg, name):
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def search(query, max_results=25):
    svc = _get_service()
    resp = svc.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    out = []
    for m in resp.get("messages", []):
        msg = svc.users().messages().get(
            userId="me", id=m["id"], format="metadata",
            metadataHeaders=["Subject", "From", "Date"]).execute()
        out.append({
            "id": m["id"],
            "subject": _header(msg, "Subject"),
            "from": _header(msg, "From"),
            "date": _header(msg, "Date"),
            "snippet": msg.get("snippet", ""),
        })
    return out


def body(msg_id):
    """Full plain-text body (HTML stripped crudely if no text/plain part)."""
    svc = _get_service()
    msg = svc.users().messages().get(userId="me", id=msg_id, format="full").execute()

    def walk(part):
        texts = []
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and mime in ("text/plain", "text/html"):
            decoded = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            if mime == "text/html":
                decoded = re.sub(r"<style.*?</style>", " ", decoded, flags=re.S | re.I)
                decoded = re.sub(r"<[^>]+>", " ", decoded)
            texts.append((mime, decoded))
        for sub in part.get("parts", []) or []:
            texts.extend(walk(sub))
        return texts

    texts = walk(msg.get("payload", {}))
    plain = [t for m, t in texts if m == "text/plain"]
    chosen = plain[0] if plain else (texts[0][1] if texts else "")
    return re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", chosen)).strip()


def _gdate(iso):
    return iso.replace("-", "/")


def sweep_confirmations(booked_after, max_results=25):
    """Booking confirmations since a date (bookings happen before the trip window)."""
    seen, out = set(), []
    queries = [
        f"from:({CONFIRMATION_SENDERS}) after:{_gdate(booked_after)}",
        f"subject:(confirmation OR itinerary OR eticket OR \"booking confirmed\") after:{_gdate(booked_after)}",
    ]
    for q in queries:
        for hit in search(q, max_results):
            if hit["id"] not in seen:
                seen.add(hit["id"])
                out.append(hit)
    return out


def sweep_receipts(date_start, date_end, max_results=50):
    """Emailed receipts inside the trip window (pad a day each side for time zones)."""
    q = f"{RECEIPT_TERMS} after:{_gdate(date_start)} before:{_gdate(date_end)}"
    return search(q, max_results)


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "confirmations":
        hits = sweep_confirmations(sys.argv[2])
    elif mode == "receipts":
        hits = sweep_receipts(sys.argv[2], sys.argv[3])
    elif mode == "body":
        print(body(sys.argv[2]))
        sys.exit(0)
    else:
        sys.exit(f"unknown mode {mode}")
    for h in hits:
        print(f"[{h['id']}] {h['date'][:22]:<22} {h['from'][:40]:<40} {h['subject'][:60]}")
    print(f"— {len(hits)} candidates")
