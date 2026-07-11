"""
Fact-table CRUD for the timeshare feature — strict allowlist of tables and
columns a member can write. Every SELECT/INSERT/UPDATE/DELETE is scoped to
the current group_id (either directly or via a parent FK chain) so a
logged-in member of group A can't accidentally mutate rows in group B,
even if the caller fabricates a URL.

Scope flavors:
- "group": the row has a direct group_id column (properties, people,
  maintenance_fees via property, portals, contacts, document_refs,
  timeline_events, trips, loan_payments via contract, exchanges via property)
- "property_of_group": row has property_id; we join to verify
  properties.group_id matches
- "contract_of_group": row has contract_id; join contract → property → group
- "trip_of_group": row has trip_id; join trip → group
"""

import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from utilities.postgres_utils import get_db_connection

logger = logging.getLogger('crab_travel.timeshare_facts')


# ── Fact schema registry ────────────────────────────────────────────

# Coercion markers: 'str' | 'text' | 'int' | 'date' | 'numeric' | 'bool'
# Columns with None default are rejected if blank; required columns flag
# validation failure.

FACT_SCHEMAS = {
    'properties': {
        'table': 'crab.timeshare_properties',
        'scope': 'group',
        'columns': [
            ('name', 'str', True),
            ('developer', 'str', False),
            ('unit_number', 'str', False),
            ('unit_configuration', 'str', False),
            ('week_number', 'int', False),
            ('usage_pattern', 'str', False),
            ('trust_expiry_date', 'date', False),
            ('exchange_network', 'str', False),
            ('country', 'str', False),
            ('city', 'str', False),
            ('notes', 'text', False),
        ],
        'list_order': 'pk_id ASC',
    },
    'contracts': {
        'table': 'crab.timeshare_contracts',
        'scope': 'property_of_group',
        'parent_fk': 'property_id',
        'columns': [
            ('contract_number', 'str', False),
            ('purchase_date', 'date', False),
            ('purchase_price_usd', 'numeric', False),
            ('down_payment_usd', 'numeric', False),
            ('financing_terms', 'text', False),
            ('co_owners', 'text', False),
            ('contract_external_url', 'str', False),
            ('notes', 'text', False),
        ],
        'list_order': 'purchase_date DESC NULLS LAST, pk_id DESC',
    },
    'people': {
        'table': 'crab.timeshare_people',
        'scope': 'group',
        'columns': [
            ('full_name', 'str', True),
            ('preferred_name', 'str', False),
            ('relationship', 'str', False),
            ('email', 'str', False),
            ('phone', 'str', False),
            ('birth_date', 'date', False),
            ('notes', 'text', False),
        ],
        'list_order': 'full_name ASC',
    },
    'maintenance_fees': {
        'table': 'crab.timeshare_maintenance_fees',
        'scope': 'property_of_group',
        'parent_fk': 'property_id',
        'columns': [
            ('year', 'int', True),
            ('billed_amount_usd', 'numeric', False),
            ('paid_amount_usd', 'numeric', False),
            ('billed_date', 'date', False),
            ('paid_date', 'date', False),
            ('late_fees_usd', 'numeric', False),
            ('discount_usd', 'numeric', False),
            ('notes', 'text', False),
        ],
        'list_order': 'year DESC',
    },
    'loan_payments': {
        'table': 'crab.timeshare_loan_payments',
        'scope': 'contract_of_group',
        'parent_fk': 'contract_id',
        'columns': [
            ('payment_date', 'date', False),
            ('amount_usd', 'numeric', False),
            ('principal_usd', 'numeric', False),
            ('interest_usd', 'numeric', False),
            ('balance_after_usd', 'numeric', False),
            ('method', 'str', False),
            ('notes', 'text', False),
        ],
        'list_order': 'payment_date DESC NULLS LAST, pk_id DESC',
    },
    'trips': {
        'table': 'crab.timeshare_trips',
        'scope': 'group',
        'columns': [
            ('property_id', 'int', False),
            ('trip_date_start', 'date', False),
            ('trip_date_end', 'date', False),
            ('resort_name', 'str', False),
            ('location', 'str', False),
            ('trip_type', 'str', False),
            ('exchange_number', 'str', False),
            ('cost_usd', 'numeric', False),
            ('uncertainty_level', 'str', False),
            ('notes', 'text', False),
        ],
        'list_order': 'trip_date_start DESC NULLS LAST, pk_id DESC',
    },
    'exchanges': {
        'table': 'crab.timeshare_exchanges',
        'scope': 'property_of_group',
        'parent_fk': 'property_id',
        'columns': [
            ('network', 'str', False),
            ('deposit_date', 'date', False),
            ('week_deposited', 'int', False),
            ('exchange_date', 'date', False),
            ('exchange_fee_usd', 'numeric', False),
            ('destination_resort', 'str', False),
            ('destination_resort_code', 'str', False),
            ('trip_id', 'int', False),
            ('status', 'str', False),
            ('notes', 'text', False),
        ],
        'list_order': 'deposit_date DESC NULLS LAST, pk_id DESC',
    },
    'portals': {
        'table': 'crab.timeshare_portals',
        'scope': 'group',
        'columns': [
            ('portal_name', 'str', True),
            ('url', 'str', False),
            ('username', 'str', False),
            ('member_number', 'str', False),
            ('support_phone', 'str', False),
            ('last_rotated', 'date', False),
            ('notes', 'text', False),
        ],
        'list_order': 'portal_name ASC',
    },
    'contacts': {
        'table': 'crab.timeshare_contacts',
        'scope': 'group',
        'columns': [
            ('full_name', 'str', True),
            ('role', 'str', False),
            ('organization', 'str', False),
            ('email', 'str', False),
            ('phone', 'str', False),
            ('last_contacted', 'date', False),
            ('notes', 'text', False),
        ],
        'list_order': 'full_name ASC',
    },
    'document_refs': {
        'table': 'crab.timeshare_document_refs',
        'scope': 'group',
        'columns': [
            ('doc_type', 'str', False),
            ('title', 'str', True),
            ('external_url', 'str', False),
            ('external_provider', 'str', False),
            ('date_on_document', 'date', False),
            ('related_property_id', 'int', False),
            ('related_trip_id', 'int', False),
            ('notes', 'text', False),
        ],
        'list_order': 'date_on_document DESC NULLS LAST, pk_id DESC',
    },
    'timeline_events': {
        'table': 'crab.timeshare_timeline_events',
        'scope': 'group',
        'columns': [
            ('event_date', 'date', False),
            ('event_type', 'str', False),
            ('title', 'str', True),
            ('description', 'text', False),
            ('related_person_id', 'int', False),
            ('related_property_id', 'int', False),
            ('related_contact_id', 'int', False),
        ],
        'list_order': 'event_date DESC NULLS LAST, pk_id DESC',
    },
}


# ── Value coercion ──────────────────────────────────────────────────

def _coerce(kind, raw):
    """Return (value, error). Blank input → (None, None) for optional cols.
    Caller enforces required-ness separately."""
    if raw is None:
        return (None, None)
    raw = raw.strip() if isinstance(raw, str) else raw
    if raw == '':
        return (None, None)

    if kind in ('str', 'text'):
        return (raw, None)
    if kind == 'int':
        try:
            return (int(raw), None)
        except (ValueError, TypeError):
            return (None, f"expected integer, got {raw!r}")
    if kind == 'numeric':
        try:
            return (Decimal(str(raw).replace('$', '').replace(',', '')), None)
        except (InvalidOperation, TypeError):
            return (None, f"expected number, got {raw!r}")
    if kind == 'date':
        for fmt in ('%Y-%m-%d', '%m/%d/%Y', '%b %d %Y', '%B %d %Y'):
            try:
                return (datetime.strptime(raw, fmt).date(), None)
            except ValueError:
                continue
        return (None, f"expected date (YYYY-MM-DD), got {raw!r}")
    if kind == 'bool':
        return (str(raw).lower() in ('1', 'true', 'on', 'yes'), None)
    return (None, f"unknown coercion kind: {kind}")


def _coerce_row(schema, form):
    """Returns (cleaned_dict, [errors])."""
    cleaned = {}
    errors = []
    for col, kind, required in schema['columns']:
        raw = form.get(col)
        val, err = _coerce(kind, raw)
        if err:
            errors.append(f"{col}: {err}")
            continue
        if required and val in (None, ''):
            errors.append(f"{col} is required")
            continue
        cleaned[col] = val
    return cleaned, errors


# ── Scope verification ──────────────────────────────────────────────

def _verify_parent_scope(cur, scope, parent_id, group_id):
    """Return True if the parent_id belongs to the given group_id."""
    if scope == 'property_of_group':
        cur.execute("""
            SELECT 1 FROM crab.timeshare_properties
             WHERE pk_id = %s AND group_id = %s::uuid
        """, (parent_id, group_id))
    elif scope == 'contract_of_group':
        cur.execute("""
            SELECT 1 FROM crab.timeshare_contracts c
              JOIN crab.timeshare_properties p ON p.pk_id = c.property_id
             WHERE c.pk_id = %s AND p.group_id = %s::uuid
        """, (parent_id, group_id))
    elif scope == 'trip_of_group':
        cur.execute("""
            SELECT 1 FROM crab.timeshare_trips
             WHERE pk_id = %s AND group_id = %s::uuid
        """, (parent_id, group_id))
    else:
        return False
    return cur.fetchone() is not None


def _scoped_where(schema, pk_alias='pk_id', group_placeholder='%s::uuid'):
    """Return the WHERE clause fragment + extra joins for mutating a single row
    while enforcing group scope. Used by UPDATE and DELETE helpers."""
    scope = schema['scope']
    table = schema['table']
    if scope == 'group':
        return (
            f"FROM {table} WHERE pk_id = %s AND group_id = {group_placeholder}",
            # used only for UPDATE — see _update_fact
        )
    if scope == 'property_of_group':
        return (
            f"FROM {table} t "
            f"USING crab.timeshare_properties p "
            f"WHERE t.pk_id = %s AND t.property_id = p.pk_id "
            f"AND p.group_id = {group_placeholder}",
        )
    if scope == 'contract_of_group':
        return (
            f"FROM {table} t "
            f"USING crab.timeshare_contracts c, crab.timeshare_properties p "
            f"WHERE t.pk_id = %s AND t.contract_id = c.pk_id "
            f"AND c.property_id = p.pk_id AND p.group_id = {group_placeholder}",
        )
    raise ValueError(f"unknown scope: {scope}")


# ── Public API ──────────────────────────────────────────────────────

def list_facts(group_id, fact_key, parent_id=None):
    """Return rows scoped to this group (+ optional parent). Safe to call
    from templates — binds group_id as first parameter every time."""
    schema = FACT_SCHEMAS[fact_key]
    import psycopg2.extras
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        order = schema['list_order']
        if schema['scope'] == 'group':
            cur.execute(f"""
                SELECT * FROM {schema['table']}
                 WHERE group_id = %s::uuid
                 ORDER BY {order}
            """, (group_id,))
        elif schema['scope'] == 'property_of_group':
            cur.execute(f"""
                SELECT t.* FROM {schema['table']} t
                  JOIN crab.timeshare_properties p ON p.pk_id = t.{schema['parent_fk']}
                 WHERE p.group_id = %s::uuid
                   {'AND t.' + schema['parent_fk'] + ' = %s' if parent_id else ''}
                 ORDER BY {order}
            """, (group_id, parent_id) if parent_id else (group_id,))
        elif schema['scope'] == 'contract_of_group':
            cur.execute(f"""
                SELECT t.* FROM {schema['table']} t
                  JOIN crab.timeshare_contracts c ON c.pk_id = t.{schema['parent_fk']}
                  JOIN crab.timeshare_properties p ON p.pk_id = c.property_id
                 WHERE p.group_id = %s::uuid
                   {'AND t.' + schema['parent_fk'] + ' = %s' if parent_id else ''}
                 ORDER BY {order}
            """, (group_id, parent_id) if parent_id else (group_id,))
        else:
            return []
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def insert_fact(group_id, fact_key, form, parent_id=None):
    """Create a new row. Returns (pk_id, None) on success, (None, error) on failure."""
    if fact_key not in FACT_SCHEMAS:
        return (None, f"unknown fact table: {fact_key}")
    schema = FACT_SCHEMAS[fact_key]
    cleaned, errors = _coerce_row(schema, form)
    if errors:
        return (None, "; ".join(errors))

    cols = list(cleaned.keys())
    vals = [cleaned[c] for c in cols]

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if schema['scope'] == 'group':
            cols_sql = ['group_id'] + cols
            placeholders = ['%s::uuid'] + ['%s'] * len(cols)
            params = [group_id] + vals
        elif schema['scope'] in ('property_of_group', 'contract_of_group', 'trip_of_group'):
            if parent_id is None:
                return (None, f"{schema['parent_fk']} is required")
            if not _verify_parent_scope(cur, schema['scope'], parent_id, group_id):
                return (None, "parent record not in this group")
            cols_sql = [schema['parent_fk']] + cols
            placeholders = ['%s'] + ['%s'] * len(cols)
            params = [parent_id] + vals
        else:
            return (None, f"unsupported scope: {schema['scope']}")

        sql = (
            f"INSERT INTO {schema['table']} ({', '.join(cols_sql)}) "
            f"VALUES ({', '.join(placeholders)}) RETURNING pk_id"
        )
        cur.execute(sql, params)
        pk = cur.fetchone()[0]
        conn.commit()
        return (pk, None)
    except Exception as e:
        conn.rollback()
        logger.warning(f"insert_fact({fact_key}) failed: {e}")
        return (None, str(e).split('\n')[0])
    finally:
        conn.close()


def update_fact(group_id, fact_key, pk, form):
    """Update a row with group-scope enforcement. Returns (True, None) or (False, error)."""
    if fact_key not in FACT_SCHEMAS:
        return (False, f"unknown fact table: {fact_key}")
    schema = FACT_SCHEMAS[fact_key]
    cleaned, errors = _coerce_row(schema, form)
    if errors:
        return (False, "; ".join(errors))
    if not cleaned:
        return (False, "no fields to update")

    set_cols = list(cleaned.keys())
    set_vals = [cleaned[c] for c in set_cols]
    set_clause = ', '.join(f"{c} = %s" for c in set_cols)

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if schema['scope'] == 'group':
            sql = (
                f"UPDATE {schema['table']} SET {set_clause} "
                f"WHERE pk_id = %s AND group_id = %s::uuid"
            )
            params = set_vals + [pk, group_id]
        elif schema['scope'] == 'property_of_group':
            sql = (
                f"UPDATE {schema['table']} t SET {set_clause} "
                f"FROM crab.timeshare_properties p "
                f"WHERE t.pk_id = %s AND t.{schema['parent_fk']} = p.pk_id "
                f"AND p.group_id = %s::uuid"
            )
            params = set_vals + [pk, group_id]
        elif schema['scope'] == 'contract_of_group':
            sql = (
                f"UPDATE {schema['table']} t SET {set_clause} "
                f"FROM crab.timeshare_contracts c, crab.timeshare_properties p "
                f"WHERE t.pk_id = %s AND t.{schema['parent_fk']} = c.pk_id "
                f"AND c.property_id = p.pk_id AND p.group_id = %s::uuid"
            )
            params = set_vals + [pk, group_id]
        else:
            return (False, f"unsupported scope: {schema['scope']}")

        cur.execute(sql, params)
        if cur.rowcount == 0:
            return (False, "row not found in this group")
        conn.commit()
        return (True, None)
    except Exception as e:
        conn.rollback()
        logger.warning(f"update_fact({fact_key}, pk={pk}) failed: {e}")
        return (False, str(e).split('\n')[0])
    finally:
        conn.close()


def delete_fact(group_id, fact_key, pk):
    """Delete a row with group-scope enforcement."""
    if fact_key not in FACT_SCHEMAS:
        return (False, f"unknown fact table: {fact_key}")
    schema = FACT_SCHEMAS[fact_key]
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if schema['scope'] == 'group':
            sql = f"DELETE FROM {schema['table']} WHERE pk_id = %s AND group_id = %s::uuid"
            params = (pk, group_id)
        elif schema['scope'] == 'property_of_group':
            sql = (
                f"DELETE FROM {schema['table']} t USING crab.timeshare_properties p "
                f"WHERE t.pk_id = %s AND t.{schema['parent_fk']} = p.pk_id "
                f"AND p.group_id = %s::uuid"
            )
            params = (pk, group_id)
        elif schema['scope'] == 'contract_of_group':
            sql = (
                f"DELETE FROM {schema['table']} t USING crab.timeshare_contracts c, crab.timeshare_properties p "
                f"WHERE t.pk_id = %s AND t.{schema['parent_fk']} = c.pk_id "
                f"AND c.property_id = p.pk_id AND p.group_id = %s::uuid"
            )
            params = (pk, group_id)
        else:
            return (False, f"unsupported scope: {schema['scope']}")
        cur.execute(sql, params)
        if cur.rowcount == 0:
            return (False, "row not found in this group")
        conn.commit()
        return (True, None)
    except Exception as e:
        conn.rollback()
        logger.warning(f"delete_fact({fact_key}, pk={pk}) failed: {e}")
        return (False, str(e).split('\n')[0])
    finally:
        conn.close()


def get_group_counts(group_id):
    """Return a dict of record counts for the dashboard cards."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT
              (SELECT COUNT(*) FROM crab.timeshare_properties WHERE group_id = %(g)s::uuid) AS properties,
              (SELECT COUNT(*) FROM crab.timeshare_people WHERE group_id = %(g)s::uuid) AS people,
              (SELECT COUNT(*) FROM crab.timeshare_trips WHERE group_id = %(g)s::uuid) AS trips,
              (SELECT COUNT(*) FROM crab.timeshare_portals WHERE group_id = %(g)s::uuid) AS portals,
              (SELECT COUNT(*) FROM crab.timeshare_contacts WHERE group_id = %(g)s::uuid) AS contacts,
              (SELECT COUNT(*) FROM crab.timeshare_document_refs WHERE group_id = %(g)s::uuid) AS documents,
              (SELECT COUNT(*) FROM crab.timeshare_timeline_events WHERE group_id = %(g)s::uuid) AS timeline,
              (SELECT COUNT(*) FROM crab.timeshare_maintenance_fees f
                 JOIN crab.timeshare_properties p ON p.pk_id = f.property_id
                WHERE p.group_id = %(g)s::uuid) AS maintenance_fees
        """, {'g': group_id})
        row = cur.fetchone()
        return {
            'properties': row[0],
            'people': row[1],
            'trips': row[2],
            'portals': row[3],
            'contacts': row[4],
            'documents': row[5],
            'timeline': row[6],
            'maintenance_fees': row[7],
        }
    finally:
        conn.close()
