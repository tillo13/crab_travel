#!/usr/bin/env python3
"""
Claude-powered fuzzy MDM dedup for people / contacts / portals / trips.

The exact-name dedup in dev/dedup_people.py only catches `LOWER(TRIM)` matches.
Real dossiers have variant spellings:
  Andrew Tillo / Andy Tillo / A. Tillo / Andrew Tillo (Andy)
  Britney / Britney Tillo
  Royal Sands / The Royal Sands / Royal Sands Cancún

Strategy: send all rows in a table to Claude as a short JSON blob. Claude
returns clusters of rows that refer to the same entity. We pick a winner per
cluster (most fields populated), merge the rest in, re-point FKs, delete
losers. Same mechanics as dedup_people.py but the CLUSTERING is Claude-led
instead of SQL-GROUP-BY.

Cost: ~$0.02–0.05 per table per group. Runs once per group on request.

Usage:
    python dev/dedup_fuzzy.py --group <uuid>                      # dry-run people
    python dev/dedup_fuzzy.py --group <uuid> --execute            # apply
    python dev/dedup_fuzzy.py --group <uuid> --tables people,trips
"""
import argparse
import json
import os
import sys
import time

import requests
import psycopg2.extras

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from utilities.postgres_utils import get_db_connection
from utilities.claude_utils import _get_api_key, log_api_usage


MODEL = "claude-sonnet-4-6"

TABLES = {
    'people': {
        'table': 'crab.timeshare_people',
        'display_cols': ['full_name', 'preferred_name', 'relationship', 'email', 'phone'],
        'scalar_cols': ['preferred_name', 'birth_date', 'user_id', 'email', 'phone'],
        'mergeable_text_cols': ['relationship', 'notes'],
        'fk_refs': [
            ('crab.timeshare_trip_participants', 'person_id'),
            ('crab.timeshare_timeline_events', 'related_person_id'),
        ],
        'entity_kind': 'person',
    },
    'contacts': {
        'table': 'crab.timeshare_contacts',
        'display_cols': ['full_name', 'role', 'organization', 'email', 'phone'],
        'scalar_cols': ['organization', 'email', 'phone', 'last_contacted'],
        'mergeable_text_cols': ['role', 'notes'],
        'fk_refs': [('crab.timeshare_timeline_events', 'related_contact_id')],
        'entity_kind': 'external contact / company representative',
    },
    'portals': {
        'table': 'crab.timeshare_portals',
        'display_cols': ['portal_name', 'url', 'username', 'member_number', 'support_phone'],
        'scalar_cols': ['url', 'username', 'encrypted_password_ref',
                        'member_number', 'support_phone', 'last_rotated'],
        'mergeable_text_cols': ['notes'],
        'fk_refs': [],
        'entity_kind': 'online portal/login account',
    },
    'document_refs': {
        'table': 'crab.timeshare_document_refs',
        'display_cols': ['title', 'doc_type', 'external_url', 'date_on_document'],
        'scalar_cols': ['doc_type', 'external_url', 'external_provider',
                        'external_id', 'date_on_document',
                        'related_property_id', 'related_trip_id'],
        'mergeable_text_cols': ['notes'],
        'fk_refs': [],
        'entity_kind': 'document reference (file link, not the file itself)',
    },
    'trips': {
        'table': 'crab.timeshare_trips',
        'display_cols': ['resort_name', 'location', 'trip_date_start', 'trip_date_end', 'trip_type', 'notes'],
        'scalar_cols': ['property_id', 'trip_date_start', 'trip_date_end',
                        'resort_name', 'resort_ii_code', 'location',
                        'trip_type', 'exchange_number', 'cost_usd',
                        'uncertainty_level'],
        'mergeable_text_cols': ['notes'],
        'fk_refs': [
            ('crab.timeshare_trip_participants', 'trip_id'),
            ('crab.timeshare_exchanges', 'trip_id'),
        ],
        'entity_kind': 'family trip / timeshare stay',
    },
}


CLUSTER_SYSTEM = """You are deduplicating a family timeshare dossier table.
Each row describes a {entity_kind}. Rows often repeat because different
source documents mention the same entity with variant spellings, nicknames,
abbreviations, or partial info (first name only, "The" prefix, missing
suffix).

Cluster rows that describe the SAME entity. Return a JSON array of clusters.
Each cluster is {{"ids": [pk_id, pk_id, ...], "why": "<one-line reason>"}}.
Only include clusters with 2+ ids (omit singletons entirely).

STRICT rules:
- Different dates = different trips (unless uncertain-level overlap).
- Same last name but clearly different first name = different people (Diana ≠ Donneta ≠ Andrew).
- "Britney" and "Britney Tillo" = same (first-name-only row is abbreviation).
- "Andrew Tillo" / "Andy Tillo" / "A. Tillo" / "Andrew Tillo (Andy)" = same.
- "Royal Sands" / "The Royal Sands" / "Royal Sands Cancún" with matching date = same trip.
- If you're unsure, DON'T cluster. False merges destroy data; leaving dupes is fine.

Return ONLY the JSON array. No prose before or after."""


def call_claude_cluster(rows, entity_kind):
    if not rows:
        return []
    system = CLUSTER_SYSTEM.format(entity_kind=entity_kind)
    user_payload = json.dumps(rows, default=str)
    body = {
        'model': MODEL, 'max_tokens': 4096, 'system': system,
        'messages': [{'role': 'user', 'content': user_payload}],
    }
    r = requests.post('https://api.anthropic.com/v1/messages',
        headers={'x-api-key': _get_api_key(),
                 'anthropic-version': '2023-06-01',
                 'content-type': 'application/json'},
        json=body, timeout=120)
    r.raise_for_status()
    data = r.json()
    # Land usage in kumori_api_usage so admin reconciliation matches.
    try:
        log_api_usage(model=MODEL, usage=data.get('usage', {}),
                      feature='dedup_fuzzy_cluster', streaming=False)
    except Exception:
        pass
    text = ''
    for b in data.get('content', []):
        if b.get('type') == 'text':
            text += b.get('text', '')
    text = text.strip()
    # Trim to outermost JSON array
    start = text.find('[')
    end = text.rfind(']')
    if start < 0 or end < 0:
        return []
    try:
        clusters = json.loads(text[start:end+1])
    except Exception as e:
        print(f"  ⚠️  Claude response not valid JSON: {e}")
        return []
    return [c for c in clusters if isinstance(c, dict) and isinstance(c.get('ids'), list) and len(c['ids']) >= 2]


def load_rows(conn, cfg, group_id):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    display = ', '.join(cfg['display_cols'])
    cur.execute(f"""
        SELECT pk_id, {display}
          FROM {cfg['table']}
         WHERE group_id = %s::uuid
         ORDER BY pk_id ASC
    """, (group_id,))
    return [dict(r) for r in cur.fetchall()]


def merge_cluster(conn, cfg, cluster_ids, execute=False):
    """Same merge logic as dedup_people.py — winner takes most-populated,
    mergeable text cols concat distinct."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(f"SELECT * FROM {cfg['table']} WHERE pk_id = ANY(%s) ORDER BY pk_id ASC", (cluster_ids,))
    rows = cur.fetchall()
    if len(rows) < 2:
        return 0, (rows[0] if rows else {})
    all_cols = cfg['scalar_cols'] + cfg['mergeable_text_cols']
    def score(r): return sum(1 for c in all_cols if r.get(c) not in (None, ''))
    rows_sorted = sorted(rows, key=lambda r: (-score(r), r['pk_id']))
    winner = rows_sorted[0]
    losers = rows_sorted[1:]

    set_clauses, set_params = [], []
    for col in cfg['scalar_cols']:
        if winner.get(col) in (None, ''):
            for l in losers:
                if l.get(col) not in (None, ''):
                    set_clauses.append(f"{col} = %s")
                    set_params.append(l[col])
                    winner[col] = l[col]
                    break
    for col in cfg['mergeable_text_cols']:
        values = []
        for r in rows_sorted:
            v = r.get(col)
            if v not in (None, ''):
                s = str(v).strip()
                if s and s not in values:
                    values.append(s)
        merged = ' / '.join(values) if values else None
        # Cap at 200 — `relationship` and `role` are VARCHAR(200). Notes fields
        # can be longer but capping there too is safer than pg errors.
        if merged and len(merged) > 200:
            truncated, run = [], 0
            for v in values:
                if run + len(v) + 3 > 197: break
                truncated.append(v); run += len(v) + 3
            merged = (' / '.join(truncated) + ' / …') if truncated else values[0][:197] + '…'
        if merged != winner.get(col):
            set_clauses.append(f"{col} = %s")
            set_params.append(merged)

    if execute and set_clauses:
        set_params.append(winner['pk_id'])
        cur.execute(f"UPDATE {cfg['table']} SET {', '.join(set_clauses)} WHERE pk_id = %s", tuple(set_params))

    loser_ids = [l['pk_id'] for l in losers]
    if execute:
        for ref_table, ref_col in cfg['fk_refs']:
            cur.execute(f"UPDATE {ref_table} SET {ref_col} = %s WHERE {ref_col} = ANY(%s)", (winner['pk_id'], loser_ids))
        cur.execute(f"DELETE FROM {cfg['table']} WHERE pk_id = ANY(%s)", (loser_ids,))
        conn.commit()
    return len(losers), winner


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--group', required=True)
    ap.add_argument('--execute', action='store_true')
    ap.add_argument('--tables', default='people,contacts,portals,trips')
    args = ap.parse_args()

    conn = get_db_connection()
    try:
        for t in (x.strip() for x in args.tables.split(',')):
            if t not in TABLES:
                print(f"skip unknown table: {t}"); continue
            cfg = TABLES[t]
            rows = load_rows(conn, cfg, args.group)
            print(f"\n== {t} ==   {len(rows)} rows in group")
            if len(rows) < 2:
                print("  (nothing to dedup)"); continue
            t0 = time.time()
            clusters = call_claude_cluster(rows, cfg['entity_kind'])
            print(f"  Claude returned {len(clusters)} cluster(s) in {time.time()-t0:.1f}s")
            total_deleted = 0
            for cluster in clusters:
                ids = cluster.get('ids', [])
                why = cluster.get('why', '')
                deleted_n, winner = merge_cluster(conn, cfg, ids, execute=args.execute)
                if deleted_n:
                    total_deleted += deleted_n
                    kept = winner.get('full_name') or winner.get('portal_name') or winner.get('resort_name') or f"pk={winner['pk_id']}"
                    print(f"    · {len(ids)} → 1  [{kept}]  — {why[:80]}")
            print(f"  total rows {'deleted' if args.execute else 'would delete'}: {total_deleted}")
        if not args.execute:
            print("\n(dry-run; add --execute to apply)")
    finally:
        conn.close()


if __name__ == '__main__':
    main()
