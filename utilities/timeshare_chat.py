"""
Timeshare chatbot — scoped tool use, not context stuffing.

Plan §6 of docs/timeshare_buildout.md. Claude answers questions using
nine read-only tools bound to the current `group_id`. Every tool handler
starts with `group_id` and binds it as the first SQL parameter — this
is the mitigation for plan §12.1 risk #2 (cross-group leak via a
forgotten WHERE clause). Non-negotiable.

Flow (synchronous, in-request):
 1. Persist the user message.
 2. Enter a Claude tool-use loop: call Claude → if `tool_use` blocks
    come back, execute them, feed `tool_result` blocks back, call
    Claude again. Max 5 iterations so a confused model can't burn the
    API key.
 3. Persist the assistant message with tokens/cost/tool_calls/cited
    fact refs.
 4. Increment the user's `chat_daily_count` on their group_members row.

Rate limit: 100 messages/user/day (plan §6.4). Kill switch:
`crab.timeshare_groups.settings.chat_enabled = false`.
"""

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from utilities.claude_utils import _get_api_key, log_api_usage

logger = logging.getLogger('crab_travel.timeshare_chat')

MODEL = "claude-sonnet-4-6"
API_URL = "https://api.anthropic.com/v1/messages"
MAX_TOKENS = 2048
MAX_TOOL_ITERATIONS = 5

DAILY_MESSAGE_CAP = 100
CITATION_RE = re.compile(r'\[ref:([a-z_]+):(\d+)\]')


# ── Tool definitions ────────────────────────────────────────────────

TOOLS = [
    {
        "name": "get_property",
        "description": "Return the group's property (or properties) plus any linked contracts. Use to answer 'what property do we have?', 'when was it purchased?', etc.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_maintenance_fees",
        "description": "Return maintenance-fee (CSF) rows for the group's properties. Optional year filter. Use for 'what did we pay in 2018?' or 'show the CSF history'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "year_start": {"type": "integer"},
                "year_end": {"type": "integer"},
            },
        },
    },
    {
        "name": "get_loan_payments",
        "description": "Return loan payments against the property's purchase-financing contract.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_trips",
        "description": "Return trip rows for this group. Optional year filter.",
        "input_schema": {
            "type": "object",
            "properties": {
                "year": {"type": "integer"},
            },
        },
    },
    {
        "name": "get_people",
        "description": "Return people (family, co-owners, etc.) in this group. Optional substring filter on name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name_contains": {"type": "string"},
            },
        },
    },
    {
        "name": "get_portals",
        "description": "Return portal logins with passwords ALWAYS redacted. Includes username, member number, support phone.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_contacts",
        "description": "Return external contacts (resort staff, owners-relations managers, attorneys).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_timeline",
        "description": "Return timeline events. Optional year or type filter.",
        "input_schema": {
            "type": "object",
            "properties": {
                "year": {"type": "integer"},
                "event_type": {"type": "string"},
            },
        },
    },
    {
        "name": "get_document_refs",
        "description": "Return document link registry entries. Optional filter by doc_type (contract, csf_statement, exchange_confirm, screenshot, other).",
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_type": {"type": "string"},
            },
        },
    },
    {
        "name": "search_resort_catalog",
        "description": "Search the shared II (Interval International) resort catalog across all groups. Use to answer 'what Hawaiian resorts rate 4+?' or 'is Royal Sands in the catalog?'. Catalog is not group-scoped — every group sees the same entries.",
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "Free-text location or name substring — matches resort name, area, country, or address."},
                "min_rating": {"type": "number", "description": "Minimum rating_overall (0–5)."},
                "min_sleeps": {"type": "integer", "description": "Minimum sleeping capacity across any unit type."},
            },
        },
    },
    {
        "name": "get_shortlist",
        "description": "Return this group's shortlisted resorts — the ones family members have flagged as interesting for future cycles.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


# ── Tool handlers — every one scopes to group_id as first parameter ─

def _fetchall(sql, params):
    """Helper: run a SELECT and return list of dicts."""
    from utilities.postgres_utils import get_db_connection
    import psycopg2.extras
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _handle_get_property(group_id, **_):
    props = _fetchall("""
        SELECT pk_id, name, developer, unit_number, unit_configuration,
               week_number, usage_pattern, trust_expiry_date,
               exchange_network, country, city, notes
          FROM crab.timeshare_properties
         WHERE group_id = %s::uuid
         ORDER BY pk_id ASC
    """, (group_id,))
    # Nest contracts under each property (still scoped to the group via parent FK)
    for p in props:
        p['contracts'] = _fetchall("""
            SELECT c.pk_id, c.contract_number, c.purchase_date,
                   c.purchase_price_usd, c.down_payment_usd,
                   c.financing_terms, c.co_owners, c.notes
              FROM crab.timeshare_contracts c
              JOIN crab.timeshare_properties p2 ON p2.pk_id = c.property_id
             WHERE c.property_id = %s AND p2.group_id = %s::uuid
             ORDER BY c.purchase_date ASC NULLS LAST
        """, (p['pk_id'], group_id))
    return {'properties': props}


def _handle_get_maintenance_fees(group_id, year_start=None, year_end=None, **_):
    params = [group_id]
    clause = "p.group_id = %s::uuid"
    if year_start is not None:
        clause += " AND f.year >= %s"
        params.append(year_start)
    if year_end is not None:
        clause += " AND f.year <= %s"
        params.append(year_end)
    rows = _fetchall(f"""
        SELECT f.pk_id, f.property_id, f.year,
               f.billed_amount_usd, f.paid_amount_usd,
               f.billed_date, f.paid_date, f.late_fees_usd,
               f.discount_usd, f.notes
          FROM crab.timeshare_maintenance_fees f
          JOIN crab.timeshare_properties p ON p.pk_id = f.property_id
         WHERE {clause}
         ORDER BY f.year DESC
    """, tuple(params))
    return {'maintenance_fees': rows}


def _handle_get_loan_payments(group_id, **_):
    rows = _fetchall("""
        SELECT lp.pk_id, lp.contract_id, lp.payment_date, lp.amount_usd,
               lp.principal_usd, lp.interest_usd, lp.balance_after_usd,
               lp.method, lp.notes
          FROM crab.timeshare_loan_payments lp
          JOIN crab.timeshare_contracts c ON c.pk_id = lp.contract_id
          JOIN crab.timeshare_properties p ON p.pk_id = c.property_id
         WHERE p.group_id = %s::uuid
         ORDER BY lp.payment_date DESC NULLS LAST
    """, (group_id,))
    return {'loan_payments': rows}


def _handle_get_trips(group_id, year=None, **_):
    params = [group_id]
    clause = "group_id = %s::uuid"
    if year is not None:
        clause += " AND (EXTRACT(YEAR FROM trip_date_start) = %s OR EXTRACT(YEAR FROM trip_date_end) = %s)"
        params.extend([year, year])
    rows = _fetchall(f"""
        SELECT pk_id, property_id, trip_date_start, trip_date_end,
               resort_name, resort_ii_code, location, trip_type,
               exchange_number, cost_usd, uncertainty_level, notes
          FROM crab.timeshare_trips
         WHERE {clause}
         ORDER BY trip_date_start DESC NULLS LAST
    """, tuple(params))
    return {'trips': rows}


def _handle_get_people(group_id, name_contains=None, **_):
    params = [group_id]
    clause = "group_id = %s::uuid"
    if name_contains:
        clause += " AND full_name ILIKE %s"
        params.append(f'%{name_contains}%')
    rows = _fetchall(f"""
        SELECT pk_id, full_name, preferred_name, relationship,
               email, phone, birth_date, notes
          FROM crab.timeshare_people
         WHERE {clause}
         ORDER BY full_name ASC
    """, tuple(params))
    return {'people': rows}


def _handle_get_portals(group_id, **_):
    # encrypted_password_ref is NEVER returned — hard-coded column list below
    # is the mitigation. Don't add it back without explicit reveal-flow design.
    rows = _fetchall("""
        SELECT pk_id, portal_name, url, username, member_number,
               support_phone, last_rotated, notes
          FROM crab.timeshare_portals
         WHERE group_id = %s::uuid
         ORDER BY portal_name ASC
    """, (group_id,))
    return {'portals': rows}


def _handle_get_contacts(group_id, **_):
    rows = _fetchall("""
        SELECT pk_id, full_name, role, organization, email, phone,
               last_contacted, notes
          FROM crab.timeshare_contacts
         WHERE group_id = %s::uuid
         ORDER BY full_name ASC
    """, (group_id,))
    return {'contacts': rows}


def _handle_get_timeline(group_id, year=None, event_type=None, **_):
    params = [group_id]
    clause = "group_id = %s::uuid"
    if year is not None:
        clause += " AND EXTRACT(YEAR FROM event_date) = %s"
        params.append(year)
    if event_type:
        clause += " AND event_type = %s"
        params.append(event_type)
    rows = _fetchall(f"""
        SELECT pk_id, event_date, event_type, title, description
          FROM crab.timeshare_timeline_events
         WHERE {clause}
         ORDER BY event_date DESC NULLS LAST
    """, tuple(params))
    return {'timeline_events': rows}


def _handle_get_document_refs(group_id, doc_type=None, **_):
    params = [group_id]
    clause = "group_id = %s::uuid"
    if doc_type:
        clause += " AND doc_type = %s"
        params.append(doc_type)
    rows = _fetchall(f"""
        SELECT pk_id, doc_type, title, external_url, external_provider,
               date_on_document, notes
          FROM crab.timeshare_document_refs
         WHERE {clause}
         ORDER BY date_on_document DESC NULLS LAST
    """, tuple(params))
    return {'document_refs': rows}


def _handle_search_resort_catalog(group_id, location=None, min_rating=None, min_sleeps=None, **_):
    # Catalog is shared across groups — no group_id binding needed for the query
    # itself, but we still accept group_id as the first arg for dispatch uniformity.
    from utilities.timeshare_catalog import search_resorts
    rows = search_resorts(location=location, min_rating=min_rating, min_sleeps=min_sleeps)
    return {'resorts': rows}


def _handle_get_shortlist(group_id, **_):
    from utilities.timeshare_catalog import list_shortlist
    return {'shortlist': list_shortlist(group_id)}


TOOL_HANDLERS = {
    'get_property': _handle_get_property,
    'get_maintenance_fees': _handle_get_maintenance_fees,
    'get_loan_payments': _handle_get_loan_payments,
    'get_trips': _handle_get_trips,
    'get_people': _handle_get_people,
    'get_portals': _handle_get_portals,
    'get_contacts': _handle_get_contacts,
    'get_timeline': _handle_get_timeline,
    'get_document_refs': _handle_get_document_refs,
    'search_resort_catalog': _handle_search_resort_catalog,
    'get_shortlist': _handle_get_shortlist,
}


# ── System prompt ───────────────────────────────────────────────────

SYSTEM_PROMPT_TEMPLATE = """You are the assistant for the "{group_name}" group's timeshare records.
Current user: {user_email}. Today: {today}.

RULES:
- Answer ONLY from tool results. Never invent dates, amounts, or names.
- Call the appropriate tool(s) to find facts before answering. If a tool returns empty, say so explicitly: "I don't have that in the records."
- When stating a specific fact, include an inline citation in the form `[ref:<table>:<pk_id>]` (e.g. `[ref:maintenance_fees:42]`). The UI turns these into links.
- Never return raw passwords. The portals tool already redacts them — don't try to work around this.
- Refer to people by first name when possible.
- Format currency with $ and commas; format dates as "Month DD, YYYY."
- Keep answers concise. One or two sentences is often enough."""


# ── Rate limit / kill switch ────────────────────────────────────────

class ChatError(Exception):
    """User-facing chat error. Handler returns HTTP 4xx with message."""
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


def _check_and_increment_rate_limit(group_id, user_id):
    """Increment the user's chat_daily_count, rolling the window if a day has passed.
    Raises ChatError(429) when over the cap."""
    from utilities.postgres_utils import get_db_connection
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT pk_id, chat_daily_count, chat_daily_reset_at
              FROM crab.timeshare_group_members
             WHERE group_id = %s::uuid AND user_id = %s
               AND accepted_at IS NOT NULL
        """, (group_id, user_id))
        row = cur.fetchone()
        if not row:
            raise ChatError("You're not a member of this group.", 403)
        pk_id, count, reset_at = row
        now = datetime.now(timezone.utc)
        if reset_at is None or (now - reset_at) > timedelta(hours=24):
            # Roll the window
            cur.execute("""
                UPDATE crab.timeshare_group_members
                   SET chat_daily_count = 1, chat_daily_reset_at = NOW()
                 WHERE pk_id = %s
            """, (pk_id,))
            conn.commit()
            return
        if count >= DAILY_MESSAGE_CAP:
            raise ChatError(
                f"Daily limit of {DAILY_MESSAGE_CAP} chat messages reached. "
                f"Resets in {24 - int((now - reset_at).total_seconds() / 3600)}h.",
                429,
            )
        cur.execute("""
            UPDATE crab.timeshare_group_members
               SET chat_daily_count = chat_daily_count + 1
             WHERE pk_id = %s
        """, (pk_id,))
        conn.commit()
    finally:
        conn.close()


def _check_kill_switch(group_id):
    from utilities.postgres_utils import get_db_connection
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT settings FROM crab.timeshare_groups
             WHERE group_id = %s::uuid
        """, (group_id,))
        row = cur.fetchone()
        if not row:
            raise ChatError("Group not found.", 404)
        settings = row[0] or {}
        if settings.get('chat_enabled') is False:
            raise ChatError("Chat is disabled for this group.", 403)
    finally:
        conn.close()


# ── Conversation / message persistence ──────────────────────────────

def get_or_create_conversation(group_id, user_id):
    """For MVP, one persistent conversation per (group, user). Opens on demand."""
    from utilities.postgres_utils import get_db_connection
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT pk_id FROM crab.timeshare_chat_conversations
             WHERE group_id = %s::uuid AND user_id = %s
             ORDER BY created_at DESC
             LIMIT 1
        """, (group_id, user_id))
        row = cur.fetchone()
        if row:
            return row[0]
        cur.execute("""
            INSERT INTO crab.timeshare_chat_conversations (group_id, user_id, title)
            VALUES (%s::uuid, %s, 'Dossier Q&A')
            RETURNING pk_id
        """, (group_id, user_id))
        pk = cur.fetchone()[0]
        conn.commit()
        return pk
    finally:
        conn.close()


def load_message_history(conversation_id, limit=30):
    """Return the last N messages for a conversation, oldest first, for feeding
    to Claude as the conversation context."""
    from utilities.postgres_utils import get_db_connection
    import psycopg2.extras
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT role, content, tool_calls, created_at
              FROM crab.timeshare_chat_messages
             WHERE conversation_id = %s
             ORDER BY pk_id DESC
             LIMIT %s
        """, (conversation_id, limit))
        rows = [dict(r) for r in cur.fetchall()]
        rows.reverse()
        return rows
    finally:
        conn.close()


def save_message(conversation_id, role, content, usage=None, cost_usd=None,
                  tool_calls=None, cited_fact_refs=None):
    from utilities.postgres_utils import get_db_connection
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO crab.timeshare_chat_messages
                (conversation_id, role, content, model,
                 input_tokens, output_tokens, cost_usd,
                 cited_fact_refs, tool_calls)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
            RETURNING pk_id
        """, (
            conversation_id, role, content, MODEL if role == 'assistant' else None,
            (usage or {}).get('input_tokens', 0) if usage else None,
            (usage or {}).get('output_tokens', 0) if usage else None,
            cost_usd,
            json.dumps(cited_fact_refs) if cited_fact_refs else None,
            json.dumps(tool_calls) if tool_calls else None,
        ))
        pk = cur.fetchone()[0]
        conn.commit()
        return pk
    finally:
        conn.close()


# ── Claude tool-use loop ────────────────────────────────────────────

def _anthropic_call(messages, system, user_id=None):
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
        'messages': messages,
    }
    start = time.time()
    r = requests.post(API_URL, headers=headers, json=body, timeout=120)
    elapsed = time.time() - start
    r.raise_for_status()
    data = r.json()
    log_api_usage(MODEL, data.get('usage', {}),
                  feature='timeshare_chat',
                  duration_ms=int(elapsed * 1000), user_id=user_id)
    return data


def _extract_citations(text):
    """Parse [ref:table:pk] markers out of Claude's text. Returns a list of
    {table, pk_id} dicts. The markers stay inline for the UI to render."""
    return [
        {'table': m.group(1), 'pk_id': int(m.group(2))}
        for m in CITATION_RE.finditer(text or '')
    ]


def _reconstruct_message_history_for_claude(db_messages):
    """Convert saved messages into the Claude API format. Previously-saved
    tool_calls / tool_results are flattened back into `content` blocks so
    Claude sees the same thread it produced."""
    msgs = []
    for m in db_messages:
        role = m['role']
        content = m['content'] or ''
        if role in ('user', 'assistant'):
            # For the MVP, only keep text in history; tool_use blocks were
            # resolved in the same turn, so they're not needed for the next turn.
            msgs.append({'role': role, 'content': content})
    return msgs


def ask(group_id, user_id, user_email, group_name, user_message):
    """Run one full question-answer turn. Returns:
      {
        'conversation_id': int,
        'user_message_id': int,
        'assistant_message_id': int,
        'assistant_text': str,
        'tool_calls': list,
        'cited_fact_refs': list,
        'cost_usd': float,
      }
    Raises ChatError for user-facing failures."""
    _check_kill_switch(group_id)
    _check_and_increment_rate_limit(group_id, user_id)

    conv_id = get_or_create_conversation(group_id, user_id)

    # Load prior turns BEFORE inserting the new one, then append current msg
    prior = load_message_history(conv_id, limit=20)
    messages = _reconstruct_message_history_for_claude(prior) + [
        {'role': 'user', 'content': user_message},
    ]
    user_msg_id = save_message(conv_id, 'user', user_message)

    system = SYSTEM_PROMPT_TEMPLATE.format(
        group_name=group_name,
        user_email=user_email,
        today=datetime.now(timezone.utc).strftime('%A, %B %d, %Y'),
    )

    tool_call_log = []   # accumulate tool_use blocks across iterations
    total_cost = 0.0
    total_usage = {'input_tokens': 0, 'output_tokens': 0}
    final_text = None

    for iteration in range(MAX_TOOL_ITERATIONS):
        data = _anthropic_call(messages, system=system, user_id=user_id)
        usage = data.get('usage', {})
        total_usage['input_tokens'] += usage.get('input_tokens', 0)
        total_usage['output_tokens'] += usage.get('output_tokens', 0)
        total_cost += (
            usage.get('input_tokens', 0) * 3 +
            usage.get('output_tokens', 0) * 15
        ) / 1_000_000

        content_blocks = data.get('content', [])
        stop_reason = data.get('stop_reason')

        # Did Claude want to use tools?
        tool_uses = [b for b in content_blocks if b.get('type') == 'tool_use']
        text_blocks = [b for b in content_blocks if b.get('type') == 'text']

        if not tool_uses:
            # Terminal text response
            final_text = '\n'.join(b.get('text', '') for b in text_blocks).strip()
            break

        # Append the assistant's full turn (including any text + tool_use)
        messages.append({'role': 'assistant', 'content': content_blocks})

        # Execute each tool call, scoped to group_id
        tool_results = []
        for tu in tool_uses:
            name = tu.get('name')
            tool_input = tu.get('input') or {}
            tool_call_log.append({'name': name, 'input': tool_input})
            handler = TOOL_HANDLERS.get(name)
            if not handler:
                result = {'error': f'unknown tool: {name}'}
            else:
                try:
                    result = handler(group_id, **tool_input)
                except Exception as e:
                    logger.warning(f"tool {name} failed: {e}")
                    result = {'error': 'tool execution failed'}
            tool_results.append({
                'type': 'tool_result',
                'tool_use_id': tu.get('id'),
                'content': json.dumps(result, default=str),
            })
        messages.append({'role': 'user', 'content': tool_results})

    if final_text is None:
        final_text = "I wasn't able to come up with an answer from the records."

    cited = _extract_citations(final_text)
    assistant_msg_id = save_message(
        conv_id, 'assistant', final_text,
        usage=total_usage, cost_usd=total_cost,
        tool_calls=tool_call_log, cited_fact_refs=cited,
    )
    logger.info(
        f"timeshare_chat: conv={conv_id} iters={iteration+1} "
        f"tools={len(tool_call_log)} tokens={total_usage['input_tokens']}→{total_usage['output_tokens']} "
        f"${total_cost:.4f}"
    )
    return {
        'conversation_id': conv_id,
        'user_message_id': user_msg_id,
        'assistant_message_id': assistant_msg_id,
        'assistant_text': final_text,
        'tool_calls': tool_call_log,
        'cited_fact_refs': cited,
        'cost_usd': total_cost,
        'iterations': iteration + 1,
    }
