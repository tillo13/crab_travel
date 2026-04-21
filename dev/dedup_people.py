#!/usr/bin/env python3
"""
One-shot dedup pass for the timeshare dossier's people/contacts/portals
tables. Claude extraction produced duplicates across docs — "Andrew Tillo"
shows up 16+ times with different relationship labels because each dossier
doc mentions him.

Strategy (MDM-lite):
  1. For each duplicate-key set within a group, pick a WINNER — the row
     with the most non-null scalar fields. Ties broken by lowest pk_id.
  2. Merge LOSERS into WINNER:
       - scalar cols: winner keeps its value unless null, then takes loser's
       - relationship / role / notes: concat distinct non-empty values with ' / '
  3. Re-point FK references (trip_participants, trip_participants) to winner.
  4. DELETE losers.
  5. Report before/after counts + dry-run mode.

Usage:
    source venv_crab/bin/activate
    python dev/dedup_people.py --dry-run            # preview only
    python dev/dedup_people.py --dry-run --group <uuid>
    python dev/dedup_people.py --execute            # do it
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from utilities.postgres_utils import get_db_connection
import psycopg2.extras


# Table configs — key = (cols, merge_cols, fk_columns_referencing_this_pk)
TABLES = {
    'people': {
        'table': 'crab.timeshare_people',
        'group_scope_col': 'group_id',
        'dedup_key_sql': "LOWER(TRIM(COALESCE(full_name, '')))",
        'scalar_cols': ['preferred_name', 'birth_date', 'user_id', 'email', 'phone'],
        'mergeable_text_cols': ['relationship', 'notes'],
        'fk_refs': [
            # table, fk_column
            ('crab.timeshare_trip_participants', 'person_id'),
            ('crab.timeshare_timeline_events', 'related_person_id'),
        ],
    },
    'contacts': {
        'table': 'crab.timeshare_contacts',
        'group_scope_col': 'group_id',
        'dedup_key_sql': "LOWER(TRIM(COALESCE(full_name, '')))",
        'scalar_cols': ['organization', 'email', 'phone', 'last_contacted'],
        'mergeable_text_cols': ['role', 'notes'],
        'fk_refs': [
            ('crab.timeshare_timeline_events', 'related_contact_id'),
        ],
    },
    'portals': {
        'table': 'crab.timeshare_portals',
        'group_scope_col': 'group_id',
        'dedup_key_sql': "LOWER(TRIM(COALESCE(portal_name, '')))",
        'scalar_cols': ['url', 'username', 'encrypted_password_ref',
                        'member_number', 'support_phone', 'last_rotated'],
        'mergeable_text_cols': ['notes'],
        'fk_refs': [],
    },
}


def dedup_one_table(conn, cfg, group_id=None, execute=False):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    params = []
    where = ''
    if group_id:
        where = f"WHERE {cfg['group_scope_col']} = %s::uuid"
        params.append(group_id)

    # Find duplicate sets
    cur.execute(f"""
        SELECT {cfg['group_scope_col']} AS g_id,
               {cfg['dedup_key_sql']} AS dedup_key,
               COUNT(*) AS n,
               ARRAY_AGG(pk_id ORDER BY pk_id) AS pk_ids
          FROM {cfg['table']}
        {where}
        GROUP BY {cfg['group_scope_col']}, {cfg['dedup_key_sql']}
        HAVING COUNT(*) > 1
        ORDER BY n DESC
    """, tuple(params))
    dup_sets = cur.fetchall()

    total_losers = 0
    merged_sets = 0
    samples = []

    scalar_cols = cfg['scalar_cols']
    text_cols = cfg['mergeable_text_cols']
    all_cols = scalar_cols + text_cols

    for ds in dup_sets:
        pk_ids = ds['pk_ids']
        # Pull all rows in this dup set
        cur.execute(f"""
            SELECT pk_id, * FROM {cfg['table']} WHERE pk_id = ANY(%s)
            ORDER BY pk_id ASC
        """, (pk_ids,))
        rows = cur.fetchall()
        if len(rows) < 2:
            continue

        # Winner = most non-null scalar+text fields filled
        def score(r):
            return sum(1 for c in all_cols if r.get(c) not in (None, ''))
        rows_sorted = sorted(rows, key=lambda r: (-score(r), r['pk_id']))
        winner = rows_sorted[0]
        losers = rows_sorted[1:]

        # Build UPDATE for winner — take any scalar values losers have that
        # winner is missing; concat distinct non-empty text values.
        set_clauses = []
        set_params = []
        for col in scalar_cols:
            if winner.get(col) in (None, ''):
                for loser in losers:
                    v = loser.get(col)
                    if v not in (None, ''):
                        set_clauses.append(f"{col} = %s")
                        set_params.append(v)
                        winner[col] = v  # for further loser passes
                        break
        for col in text_cols:
            values = []
            for r in rows_sorted:
                v = r.get(col)
                if v not in (None, ''):
                    v_str = str(v).strip()
                    if v_str and v_str not in values:
                        values.append(v_str)
            merged = ' / '.join(values) if values else None
            # Cap at 200 chars — matches column width on relationship/role.
            # Drop trailing partial labels cleanly.
            if merged and len(merged) > 200:
                truncated_parts = []
                running_len = 0
                for v in values:
                    if running_len + len(v) + 3 > 197:
                        break
                    truncated_parts.append(v)
                    running_len += len(v) + 3
                merged = ' / '.join(truncated_parts) + ' / …'
            if merged != winner.get(col):
                set_clauses.append(f"{col} = %s")
                set_params.append(merged)

        if execute and set_clauses:
            set_params.append(winner['pk_id'])
            cur.execute(f"""
                UPDATE {cfg['table']} SET {', '.join(set_clauses)}
                 WHERE pk_id = %s
            """, tuple(set_params))

        # Re-point FK refs then delete losers
        loser_ids = [l['pk_id'] for l in losers]
        if execute:
            for ref_table, ref_col in cfg['fk_refs']:
                cur.execute(f"""
                    UPDATE {ref_table} SET {ref_col} = %s
                     WHERE {ref_col} = ANY(%s)
                """, (winner['pk_id'], loser_ids))
            cur.execute(f"""
                DELETE FROM {cfg['table']} WHERE pk_id = ANY(%s)
            """, (loser_ids,))

        total_losers += len(losers)
        merged_sets += 1
        if len(samples) < 5:
            # Display-name sample for summary
            name_col = 'full_name' if 'full_name' in winner else 'portal_name'
            samples.append(
                f"{winner[name_col]!r}: {len(rows)} rows → 1 (kept pk={winner['pk_id']})"
            )

    if execute:
        conn.commit()

    return {
        'merged_sets': merged_sets,
        'losers_deleted': total_losers,
        'samples': samples,
    }


def count_rows(conn, cfg, group_id=None):
    cur = conn.cursor()
    params = []
    where = ''
    if group_id:
        where = f"WHERE {cfg['group_scope_col']} = %s::uuid"
        params.append(group_id)
    cur.execute(f"SELECT COUNT(*) FROM {cfg['table']} {where}", tuple(params))
    return cur.fetchone()[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--execute', action='store_true', help='Actually delete losers; default is dry-run preview.')
    ap.add_argument('--group', help='Scope to one group_id UUID.')
    ap.add_argument('--tables', default='people,contacts,portals',
                    help='Comma-sep list of tables to dedup.')
    args = ap.parse_args()

    tables = [t.strip() for t in args.tables.split(',') if t.strip()]
    conn = get_db_connection()
    try:
        for t in tables:
            cfg = TABLES[t]
            before = count_rows(conn, cfg, args.group)
            summary = dedup_one_table(conn, cfg, args.group, execute=args.execute)
            after = count_rows(conn, cfg, args.group)
            print(f"\n== {t} ==")
            print(f"  before: {before} rows")
            print(f"  duplicate sets found: {summary['merged_sets']}")
            print(f"  rows {'deleted' if args.execute else 'would be deleted'}: {summary['losers_deleted']}")
            print(f"  after:  {after} rows")
            for s in summary['samples']:
                print(f"    · {s}")
        if not args.execute:
            print("\n(dry-run; re-run with --execute to apply)")
    finally:
        conn.close()


if __name__ == '__main__':
    main()
