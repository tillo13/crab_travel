"""
Timeshare ingestion pipeline — Claude tool-use extraction.

Plan §5 of docs/timeshare_buildout.md. Inputs: raw text (pasted) or PDF
text (via pdfplumber). Output: structured facts grouped by target table,
stored in `crab.timeshare_ingest_jobs.extracted_facts` for user review.

Contract: each tool's input schema uses DB column names exactly — no
mapping layer between Claude's tool_use output and our fact tables.

Model: claude-sonnet-4-5 — plan §5.5 estimates ~$0.05 per CSF-statement
ingest.

PDF posture: bytes are extracted to text via pdfplumber in-request, then
discarded. Extracted text is stored in `ingest_jobs.source_content` for
provenance. crab.travel never writes binaries to disk or GCS.
"""

import hashlib
import io
import json
import logging
import time
from typing import Optional

import requests

from utilities.claude_utils import _get_api_key, log_api_usage

logger = logging.getLogger('crab_travel.timeshare_ingest')

# Ingest model — Sonnet 4.6 (current latest). Pricing matches §5.5
# estimate ($3/M in, $15/M out).
MODEL = "claude-sonnet-4-6"
API_URL = "https://api.anthropic.com/v1/messages"
MAX_TOKENS = 8192

# Maps each Claude tool name → the fact_key (table) it writes to. Tools
# whose target table needs a parent (property_of_group / contract_of_group)
# are resolved at commit time by picking the group's first property/contract.
TOOL_TO_FACT_KEY = {
    'record_maintenance_fee': 'maintenance_fees',
    'record_loan_payment': 'loan_payments',
    'record_trip': 'trips',
    'record_person': 'people',
    'record_portal_login': 'portals',
    'record_contact': 'contacts',
    'record_timeline_event': 'timeline_events',
    'record_document_reference': 'document_refs',
}


TOOLS = [
    {
        "name": "record_maintenance_fee",
        "description": "Record a maintenance-fee / club-service-fee year for this group's property. Call once per distinct year mentioned in the content.",
        "input_schema": {
            "type": "object",
            "properties": {
                "year": {"type": "integer", "description": "Calendar year of the fee (e.g. 2024)."},
                "billed_amount_usd": {"type": "number", "description": "Amount billed in USD, no currency symbol."},
                "paid_amount_usd": {"type": "number"},
                "billed_date": {"type": "string", "description": "ISO YYYY-MM-DD."},
                "paid_date": {"type": "string", "description": "ISO YYYY-MM-DD."},
                "late_fees_usd": {"type": "number"},
                "discount_usd": {"type": "number"},
                "notes": {"type": "string"},
            },
            "required": ["year"],
        },
    },
    {
        "name": "record_loan_payment",
        "description": "Record a single loan payment against the property's purchase-financing contract.",
        "input_schema": {
            "type": "object",
            "properties": {
                "payment_date": {"type": "string", "description": "ISO YYYY-MM-DD."},
                "amount_usd": {"type": "number"},
                "principal_usd": {"type": "number"},
                "interest_usd": {"type": "number"},
                "balance_after_usd": {"type": "number"},
                "method": {"type": "string", "description": "e.g. 'visa_payoff', 'check', 'ach'."},
                "notes": {"type": "string"},
            },
        },
    },
    {
        "name": "record_trip",
        "description": "Record a family trip — a home-week use, an exchange, a bonus stay, or a rental. Use uncertainty_level='family_memory' if content is anecdotal.",
        "input_schema": {
            "type": "object",
            "properties": {
                "trip_date_start": {"type": "string", "description": "ISO YYYY-MM-DD."},
                "trip_date_end": {"type": "string", "description": "ISO YYYY-MM-DD."},
                "resort_name": {"type": "string"},
                "location": {"type": "string", "description": "City, country, or region."},
                "trip_type": {
                    "type": "string",
                    "enum": ["home_week", "exchange", "bonus_stay", "rental", "purchase_trip"],
                },
                "exchange_number": {"type": "string"},
                "cost_usd": {"type": "number"},
                "uncertainty_level": {
                    "type": "string",
                    "enum": ["confirmed", "probable", "family_memory", "unverified"],
                },
                "notes": {"type": "string"},
            },
        },
    },
    {
        "name": "record_person",
        "description": "Record a family member, co-owner, or other person mentioned in the content.",
        "input_schema": {
            "type": "object",
            "properties": {
                "full_name": {"type": "string"},
                "preferred_name": {"type": "string"},
                "relationship": {"type": "string", "description": "e.g. 'mother', 'brother', 'sister-in-law'."},
                "email": {"type": "string"},
                "phone": {"type": "string"},
                "birth_date": {"type": "string", "description": "ISO YYYY-MM-DD."},
                "notes": {"type": "string"},
            },
            "required": ["full_name"],
        },
    },
    {
        "name": "record_portal_login",
        "description": "Record a portal / account login reference. NEVER include the password in any field.",
        "input_schema": {
            "type": "object",
            "properties": {
                "portal_name": {"type": "string"},
                "url": {"type": "string"},
                "username": {"type": "string"},
                "member_number": {"type": "string"},
                "support_phone": {"type": "string"},
                "last_rotated": {"type": "string", "description": "ISO YYYY-MM-DD."},
                "notes": {"type": "string"},
            },
            "required": ["portal_name"],
        },
    },
    {
        "name": "record_contact",
        "description": "Record an external contact — resort staff, owner-relations manager, attorney, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "full_name": {"type": "string"},
                "role": {"type": "string"},
                "organization": {"type": "string"},
                "email": {"type": "string"},
                "phone": {"type": "string"},
                "last_contacted": {"type": "string", "description": "ISO YYYY-MM-DD."},
                "notes": {"type": "string"},
            },
            "required": ["full_name"],
        },
    },
    {
        "name": "record_timeline_event",
        "description": "Record a noteworthy event — a phone call, email, decision, purchase — that doesn't fit the other tools.",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_date": {"type": "string", "description": "ISO YYYY-MM-DD."},
                "event_type": {
                    "type": "string",
                    "enum": ["email_sent", "email_received", "phone_call", "purchase", "decision", "note"],
                },
                "title": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "record_document_reference",
        "description": "Note that the content mentioned a document (contract PDF, statement, exchange confirmation) the user should link back to their Drive/Dropbox copy.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "doc_type": {
                    "type": "string",
                    "enum": ["contract", "csf_statement", "exchange_confirm", "screenshot", "other"],
                },
                "date_on_document": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "no_facts_extracted",
        "description": "Call exactly once with a short reason if the content contains no extractable timeshare facts.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
            },
            "required": ["reason"],
        },
    },
]


SYSTEM_PROMPT_TEMPLATE = """You extract structured timeshare facts from user-provided content (pasted text or text extracted from a PDF).

The user's group is "{group_name}"{property_clause}.

Rules:
- Extract ONLY facts explicitly stated in the content. Do not infer or guess.
- Call one tool per distinct fact. You may make many tool calls in a single response.
- If a year/date is ambiguous, set uncertainty_level='probable' or 'family_memory' on trip/event tools.
- NEVER include a password, API key, or other secret in any tool-call field. If the content contains a password, omit it from the portal_login call.
- If the content contains no timeshare facts, call `no_facts_extracted` with a short reason.
- Be concise: one fact per tool call. No prose."""


def _hash_content(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract text from a PDF's bytes via pdfplumber. Bytes are NOT persisted.
    Returns the concatenated text across all pages."""
    import pdfplumber
    pages_text = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ''
            pages_text.append(t)
    return '\n\n'.join(pages_text).strip()


def call_claude_extractor(group_name: str, property_hint: Optional[str],
                          source_content: str, user_id: Optional[int] = None):
    """Run the Claude tool-use extraction. Returns a dict:
      {
        'extracted_facts': {'maintenance_fees': [...], 'trips': [...], ...},
        'no_facts_reason': str or None,
        'usage': {input_tokens, output_tokens},
        'cost_usd': float,
        'tool_calls': [raw tool_use blocks],  # for audit trail
      }
    Raises on network/api errors — caller decides how to surface them."""
    property_clause = f" tracking {property_hint}" if property_hint else ""
    system = SYSTEM_PROMPT_TEMPLATE.format(
        group_name=group_name,
        property_clause=property_clause,
    )

    headers = {
        'x-api-key': _get_api_key(),
        'anthropic-version': '2023-06-01',
        'content-type': 'application/json',
    }
    body = {
        'model': MODEL,
        'max_tokens': MAX_TOKENS,
        'system': system,
        'tools': TOOLS,
        # Let Claude pick the right tool(s) — no tool_choice forces it
        'messages': [
            {'role': 'user', 'content': source_content},
        ],
    }

    start = time.time()
    r = requests.post(API_URL, headers=headers, json=body, timeout=300)
    elapsed = time.time() - start
    r.raise_for_status()
    data = r.json()
    usage = data.get('usage', {})
    log_api_usage(
        MODEL, usage, feature='timeshare_ingest',
        duration_ms=int(elapsed * 1000), user_id=user_id,
    )

    extracted = {}   # fact_key -> list of dicts
    tool_calls = []
    no_facts_reason = None
    for block in data.get('content', []):
        if block.get('type') != 'tool_use':
            continue
        tool_name = block.get('name')
        tool_input = block.get('input') or {}
        tool_calls.append({'name': tool_name, 'input': tool_input})
        if tool_name == 'no_facts_extracted':
            no_facts_reason = tool_input.get('reason')
            continue
        fact_key = TOOL_TO_FACT_KEY.get(tool_name)
        if not fact_key:
            logger.warning(f"Unknown tool in Claude response: {tool_name}")
            continue
        extracted.setdefault(fact_key, []).append(tool_input)

    # Cost calc — mirrors claude_utils._get_pricing for 'sonnet-4-5'
    input_tokens = usage.get('input_tokens', 0)
    output_tokens = usage.get('output_tokens', 0)
    cost_usd = (input_tokens * 3 + output_tokens * 15) / 1_000_000

    logger.info(
        f"timeshare_ingest: {input_tokens}→{output_tokens} tokens, "
        f"${cost_usd:.4f}, {elapsed:.1f}s, "
        f"facts={sum(len(v) for v in extracted.values())}"
    )
    return {
        'extracted_facts': extracted,
        'no_facts_reason': no_facts_reason,
        'usage': usage,
        'cost_usd': cost_usd,
        'tool_calls': tool_calls,
    }


# ── DB helpers ──────────────────────────────────────────────────────

def create_ingest_job(group_id, source_type, source_content, source_ref,
                       created_by):
    """Persist an initial ingest_job row in 'extracting' status. Returns pk_id."""
    from utilities.postgres_utils import get_db_connection
    content_hash = _hash_content(source_content)
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO crab.timeshare_ingest_jobs
                (group_id, source_type, source_ref, source_snapshot_hash,
                 source_content, status, created_by)
            VALUES (%s::uuid, %s, %s, %s, %s, 'extracting', %s)
            RETURNING pk_id
        """, (group_id, source_type, source_ref, content_hash,
              source_content, created_by))
        pk = cur.fetchone()[0]
        conn.commit()
        return pk
    finally:
        conn.close()


def finalize_ingest_job(job_id, extracted_facts, tool_calls, cost_usd,
                         no_facts_reason=None, error_message=None):
    """Write extraction results to an existing job row. Status transitions to
    'review' (facts present) / 'rejected' (no_facts_extracted) / 'error'."""
    from utilities.postgres_utils import get_db_connection
    if error_message:
        status = 'error'
    elif not extracted_facts and no_facts_reason:
        status = 'rejected'
    else:
        status = 'review'

    combined = dict(extracted_facts)
    if tool_calls:
        combined['_tool_calls'] = tool_calls
    if no_facts_reason:
        combined['_no_facts_reason'] = no_facts_reason

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE crab.timeshare_ingest_jobs
               SET extracted_facts = %s::jsonb,
                   status = %s,
                   claude_cost_usd = %s,
                   error_message = %s
             WHERE pk_id = %s
        """, (json.dumps(combined, default=str), status, cost_usd,
              error_message, job_id))
        conn.commit()
    finally:
        conn.close()


def run_extraction_and_persist(group_id, source_type, source_content,
                                source_ref=None, created_by=None):
    """Happy-path orchestrator for the sync paste/upload route. Returns job_id.
    Errors are caught and written to the job row; the caller redirects to
    the review page regardless."""
    from utilities.postgres_utils import get_db_connection
    job_id = create_ingest_job(
        group_id=group_id, source_type=source_type,
        source_content=source_content, source_ref=source_ref,
        created_by=created_by,
    )

    # Pull the group's name + first property for context
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT name FROM crab.timeshare_groups WHERE group_id = %s::uuid
        """, (group_id,))
        group_name_row = cur.fetchone()
        group_name = group_name_row[0] if group_name_row else 'this group'
        cur.execute("""
            SELECT name, unit_number, week_number, exchange_network
              FROM crab.timeshare_properties
             WHERE group_id = %s::uuid
             ORDER BY pk_id ASC LIMIT 1
        """, (group_id,))
        prop = cur.fetchone()
    finally:
        conn.close()

    property_hint = None
    if prop:
        name, unit, week, network = prop
        bits = [name]
        if unit:
            bits.append(f"unit {unit}")
        if week:
            bits.append(f"week {week}")
        if network:
            bits.append(f"on {network.replace('_', ' ')}")
        property_hint = ', '.join(bits)

    try:
        result = call_claude_extractor(
            group_name=group_name,
            property_hint=property_hint,
            source_content=source_content,
            user_id=created_by,
        )
        finalize_ingest_job(
            job_id=job_id,
            extracted_facts=result['extracted_facts'],
            tool_calls=result['tool_calls'],
            cost_usd=result['cost_usd'],
            no_facts_reason=result['no_facts_reason'],
        )
    except Exception as e:
        logger.exception(f"Ingest extraction failed for job {job_id}: {e}")
        finalize_ingest_job(
            job_id=job_id, extracted_facts={}, tool_calls=[],
            cost_usd=0, error_message=str(e).split('\n')[0][:500],
        )
    return job_id


def commit_job(group_id, job_id, accepted_rows):
    """Iterate accepted rows, map to fact tables, insert via the Phase 2 fact CRUD.
    Returns (committed_count, errors_list). Sets status='committed' on success."""
    from utilities.postgres_utils import get_db_connection
    from utilities.timeshare_facts import FACT_SCHEMAS, insert_fact

    # Look up the group's first property + contract for tables that need parents
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT pk_id FROM crab.timeshare_properties
             WHERE group_id = %s::uuid ORDER BY pk_id ASC LIMIT 1
        """, (group_id,))
        prop_row = cur.fetchone()
        default_property_id = prop_row[0] if prop_row else None

        default_contract_id = None
        if default_property_id:
            cur.execute("""
                SELECT pk_id FROM crab.timeshare_contracts
                 WHERE property_id = %s ORDER BY pk_id ASC LIMIT 1
            """, (default_property_id,))
            c_row = cur.fetchone()
            default_contract_id = c_row[0] if c_row else None
    finally:
        conn.close()

    committed = 0
    errors = []
    for row in accepted_rows:
        fact_key = row['fact_key']
        data = row['data']
        schema = FACT_SCHEMAS.get(fact_key)
        if not schema:
            errors.append(f"unknown fact_key {fact_key}")
            continue
        parent_id = None
        if schema['scope'] == 'property_of_group':
            parent_id = default_property_id
            if not parent_id:
                errors.append(f"{fact_key}: add a property to the group first")
                continue
        elif schema['scope'] == 'contract_of_group':
            parent_id = default_contract_id
            if not parent_id:
                errors.append(f"{fact_key}: add a contract to the group first")
                continue
        pk, err = insert_fact(group_id, fact_key, data, parent_id=parent_id)
        if err:
            errors.append(f"{fact_key}: {err}")
            continue
        # Stamp source_ingest_job_id on the new row
        _stamp_source_job(schema['table'], pk, job_id)
        committed += 1

    # Mark job committed if anything landed; otherwise leave status alone
    if committed:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                UPDATE crab.timeshare_ingest_jobs
                   SET status = 'committed',
                       committed_at = NOW()
                 WHERE pk_id = %s AND group_id = %s::uuid
            """, (job_id, group_id))
            conn.commit()
        finally:
            conn.close()
    return committed, errors


def reject_job(group_id, job_id, review_notes=None):
    from utilities.postgres_utils import get_db_connection
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE crab.timeshare_ingest_jobs
               SET status = 'rejected',
                   review_notes = COALESCE(%s, review_notes)
             WHERE pk_id = %s AND group_id = %s::uuid
        """, (review_notes, job_id, group_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _stamp_source_job(table, pk, job_id):
    """Best-effort UPDATE to set source_ingest_job_id on the row we just
    committed. Silently no-ops if the table lacks the column."""
    from utilities.postgres_utils import get_db_connection
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE {table} SET source_ingest_job_id = %s WHERE pk_id = %s",
            (job_id, pk),
        )
        conn.commit()
    except Exception as e:
        logger.debug(f"source_ingest_job_id stamp skipped on {table}: {e}")
        conn.rollback()
    finally:
        conn.close()


def list_jobs(group_id, limit=50):
    from utilities.postgres_utils import get_db_connection
    import psycopg2.extras
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT pk_id, source_type, source_ref, status, created_at,
                   committed_at, claude_cost_usd, error_message,
                   extracted_facts
              FROM crab.timeshare_ingest_jobs
             WHERE group_id = %s::uuid
             ORDER BY created_at DESC
             LIMIT %s
        """, (group_id, limit))
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            facts = d.get('extracted_facts') or {}
            # Count user-facing fact buckets (exclude _tool_calls / _no_facts_reason meta keys)
            d['fact_bucket_count'] = sum(
                1 for k in facts.keys() if not k.startswith('_')
            )
            d['fact_row_count'] = sum(
                len(v) for k, v in facts.items()
                if not k.startswith('_') and isinstance(v, list)
            )
            rows.append(d)
        return rows
    finally:
        conn.close()


def get_job(group_id, job_id):
    from utilities.postgres_utils import get_db_connection
    import psycopg2.extras
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT * FROM crab.timeshare_ingest_jobs
             WHERE group_id = %s::uuid AND pk_id = %s
        """, (group_id, job_id))
        return cur.fetchone()
    finally:
        conn.close()
