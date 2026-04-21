"""
Timeshare blueprint — Phases 1 + 2.

Phase 1 (shipped 2026-04-21):
- GET  /timeshare/                              indexable landing
- GET  /timeshare/groups/new                    creation form
- POST /timeshare/groups/new                    create + auto-add creator as owner
- GET  /timeshare/g/<uuid>/                     dashboard (member-only, 404 on miss)
- GET  /timeshare/g/<uuid>/members              list + invite form
- POST /timeshare/g/<uuid>/members/invite       create invite row + send shortlink email
- GET  /timeshare/g/<uuid>/members/accept/<token>   accept (login-required; invite-token gated)

Phase 2 (this commit):
- GET  /timeshare/g/<uuid>/{property,finances,trips,people,portals,contacts,documents,timeline}
- POST /timeshare/g/<uuid>/fact/<fact_key>/new        create fact row
- POST /timeshare/g/<uuid>/fact/<fact_key>/<pk>       update fact row
- POST /timeshare/g/<uuid>/fact/<fact_key>/<pk>/delete   delete fact row

Ingestion, chatbot, II catalog, cycle bridge — Phases 3+.
"""

import logging
from datetime import datetime, timedelta, timezone

import psycopg2.extras
from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for

from route_helpers import login_required
from utilities.invite_utils import generate_token
from utilities.postgres_utils import get_db_connection
from utilities.shorturl_utils import create_short_url
from utilities.timeshare_access import group_member_required
from utilities.timeshare_facts import (
    FACT_SCHEMAS, list_facts, insert_fact, update_fact, delete_fact, get_group_counts,
)

logger = logging.getLogger('crab_travel.timeshare_routes')

bp = Blueprint('timeshare', __name__, url_prefix='/timeshare')

MAX_GROUPS_PER_DAY = 3
MAX_GROUPS_PER_LIFETIME = 10
INVITE_EXPIRY_DAYS = 14
INVITE_ROLES = ('admin', 'family', 'readonly')


# ── Landing ─────────────────────────────────────────────────

@bp.route('/')
def landing():
    return render_template(
        'timeshare/landing.html',
        active_page='timeshare',
    )


# ── Group lifecycle ─────────────────────────────────────────

@bp.route('/groups/new', methods=['GET'])
@login_required
def groups_new_form():
    return render_template(
        'timeshare/group_new.html',
        active_page='timeshare',
    )


@bp.route('/groups/new', methods=['POST'])
@login_required
def groups_new_submit():
    user = session['user']
    name = (request.form.get('name') or '').strip()
    if not name or len(name) > 200:
        flash('Group name is required (max 200 characters).', 'error')
        return redirect(url_for('timeshare.groups_new_form'))

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '1 day') AS today_count,
                COUNT(*) AS total_count
              FROM crab.timeshare_groups
             WHERE created_by = %s
        """, (user['id'],))
        today_count, total_count = cur.fetchone()
        if today_count >= MAX_GROUPS_PER_DAY:
            flash(f'Rate limit: {MAX_GROUPS_PER_DAY} groups per day.', 'error')
            return redirect(url_for('timeshare.groups_new_form'))
        if total_count >= MAX_GROUPS_PER_LIFETIME:
            flash(f'Account limit: {MAX_GROUPS_PER_LIFETIME} groups per user.', 'error')
            return redirect(url_for('timeshare.groups_new_form'))

        cur.execute("""
            INSERT INTO crab.timeshare_groups (name, created_by)
            VALUES (%s, %s)
            RETURNING group_id
        """, (name, user['id']))
        group_id = cur.fetchone()[0]

        cur.execute("""
            INSERT INTO crab.timeshare_group_members (group_id, user_id, email, role, invited_by, accepted_at)
            VALUES (%s, %s, %s, 'owner', %s, NOW())
        """, (group_id, user['id'], user['email'].lower(), user['id']))
        conn.commit()
        logger.info(f"timeshare: user {user['id']} created group {group_id} ({name!r})")
        return redirect(url_for('timeshare.dashboard', group_uuid=str(group_id)))
    finally:
        conn.close()


@bp.route('/g/<group_uuid>/')
@group_member_required()
def dashboard(group_uuid):
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT g.*, u.email AS created_by_email
              FROM crab.timeshare_groups g
              LEFT JOIN crab.users u ON u.pk_id = g.created_by
             WHERE g.group_id = %s::uuid
        """, (group_uuid,))
        group = cur.fetchone()
        cur.execute("""
            SELECT COUNT(*) FILTER (WHERE accepted_at IS NOT NULL) AS accepted,
                   COUNT(*) FILTER (WHERE accepted_at IS NULL) AS pending
              FROM crab.timeshare_group_members
             WHERE group_id = %s::uuid
        """, (group_uuid,))
        counts = cur.fetchone()
    finally:
        conn.close()

    fact_counts = get_group_counts(group_uuid)

    return render_template(
        'timeshare/dashboard.html',
        active_page='timeshare',
        group=group,
        member_counts=counts,
        fact_counts=fact_counts,
        role=request.timeshare_role,
    )


# ── Members ─────────────────────────────────────────────────

@bp.route('/g/<group_uuid>/members')
@group_member_required()
def members_list(group_uuid):
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT gm.pk_id, gm.email, gm.role, gm.invited_at, gm.accepted_at,
                   gm.invite_token,
                   u.full_name, u.picture_url
              FROM crab.timeshare_group_members gm
              LEFT JOIN crab.users u ON u.pk_id = gm.user_id
             WHERE gm.group_id = %s::uuid
             ORDER BY gm.invited_at ASC
        """, (group_uuid,))
        members = cur.fetchall()
        cur.execute("""
            SELECT name FROM crab.timeshare_groups WHERE group_id = %s::uuid
        """, (group_uuid,))
        group = cur.fetchone()
    finally:
        conn.close()

    expiry_cutoff = datetime.now(timezone.utc) - timedelta(days=INVITE_EXPIRY_DAYS)
    decorated = []
    for m in members:
        status = 'accepted' if m['accepted_at'] else 'pending'
        if status == 'pending' and m['invited_at'] and m['invited_at'] < expiry_cutoff:
            status = 'expired'
        decorated.append({**m, 'status': status})

    return render_template(
        'timeshare/members.html',
        active_page='timeshare',
        group_uuid=group_uuid,
        group=group,
        members=decorated,
        role=request.timeshare_role,
        invite_roles=INVITE_ROLES,
    )


@bp.route('/g/<group_uuid>/members/invite', methods=['POST'])
@group_member_required('admin')
def members_invite(group_uuid):
    inviter = session['user']
    email = (request.form.get('email') or '').strip().lower()
    role = (request.form.get('role') or 'family').strip()

    if not email or '@' not in email:
        flash('Please provide a valid email address.', 'error')
        return redirect(url_for('timeshare.members_list', group_uuid=group_uuid))
    if role not in INVITE_ROLES:
        flash('Invalid role.', 'error')
        return redirect(url_for('timeshare.members_list', group_uuid=group_uuid))

    token = generate_token()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT pk_id, invite_token, accepted_at
              FROM crab.timeshare_group_members
             WHERE group_id = %s::uuid AND email = %s
        """, (group_uuid, email))
        existing = cur.fetchone()

        if existing:
            pk_id, existing_token, accepted_at = existing
            if accepted_at is not None:
                flash(f'{email} is already a member of this group.', 'info')
                return redirect(url_for('timeshare.members_list', group_uuid=group_uuid))
            # Resend: refresh the token + invited_at so old short-link stops working
            cur.execute("""
                UPDATE crab.timeshare_group_members
                   SET invite_token = %s,
                       invited_at = NOW(),
                       invited_by = %s,
                       role = %s
                 WHERE pk_id = %s
            """, (token, inviter['id'], role, pk_id))
        else:
            cur.execute("""
                INSERT INTO crab.timeshare_group_members
                    (group_id, email, role, invite_token, invited_by)
                VALUES (%s::uuid, %s, %s, %s, %s)
            """, (group_uuid, email, role, token, inviter['id']))
        conn.commit()

        cur.execute("""
            SELECT name FROM crab.timeshare_groups WHERE group_id = %s::uuid
        """, (group_uuid,))
        group_name = cur.fetchone()[0]
    finally:
        conn.close()

    accept_url = url_for(
        'timeshare.invite_accept',
        group_uuid=group_uuid,
        token=token,
        _external=True,
    )
    short_code = create_short_url(accept_url)
    short_url = f"https://crab.travel/s/{short_code}" if short_code else accept_url

    _send_invite_email(
        to_email=email,
        inviter_name=inviter.get('name') or inviter['email'],
        group_name=group_name,
        short_url=short_url,
    )
    flash(f'Invite sent to {email}.', 'success')
    return redirect(url_for('timeshare.members_list', group_uuid=group_uuid))


@bp.route('/g/<group_uuid>/members/accept/<token>')
@login_required
def invite_accept(group_uuid, token):
    """Invite acceptance — NOT gated by group_member_required because the
    whole point is the user isn't a member yet. We validate the token, email,
    and expiry ourselves."""
    user = session['user']
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute("""
                SELECT gm.pk_id, gm.email, gm.invited_at, gm.accepted_at, g.name AS group_name
                  FROM crab.timeshare_group_members gm
                  JOIN crab.timeshare_groups g ON g.group_id = gm.group_id
                 WHERE gm.group_id = %s::uuid
                   AND gm.invite_token = %s
                   AND g.status = 'active'
            """, (group_uuid, token))
            row = cur.fetchone()
        except Exception:
            row = None

        if not row:
            abort(404)

        # Already accepted → just send them to the dashboard
        if row['accepted_at'] is not None:
            return redirect(url_for('timeshare.dashboard', group_uuid=group_uuid))

        # Expired?
        expiry_cutoff = datetime.now(timezone.utc) - timedelta(days=INVITE_EXPIRY_DAYS)
        if row['invited_at'] and row['invited_at'] < expiry_cutoff:
            return render_template(
                'timeshare/invite_accept.html',
                active_page='timeshare',
                error='This invite has expired. Ask the group admin to send a new one.',
            ), 410

        # Email must match — per plan §12.1 mitigation against forwarded links
        if (user.get('email') or '').lower() != row['email'].lower():
            return render_template(
                'timeshare/invite_accept.html',
                active_page='timeshare',
                error=f"This invite was sent to {row['email']}. Sign in with that account to accept.",
            ), 403

        cur.execute("""
            UPDATE crab.timeshare_group_members
               SET user_id = %s,
                   accepted_at = NOW(),
                   invite_token = NULL
             WHERE pk_id = %s
        """, (user['id'], row['pk_id']))
        conn.commit()
        logger.info(f"timeshare: user {user['id']} accepted invite to group {group_uuid}")
    finally:
        conn.close()

    return redirect(url_for('timeshare.dashboard', group_uuid=group_uuid))


# ── Helpers ─────────────────────────────────────────────────

def _send_invite_email(to_email, inviter_name, group_name, short_url):
    from utilities.gmail_utils import send_simple_email
    subject = f"{inviter_name} invited you to {group_name} on crab.travel"
    body = (
        f"{inviter_name} invited you to join the timeshare group \"{group_name}\" on crab.travel.\n\n"
        f"Accept the invite here:\n{short_url}\n\n"
        f"This link expires in {INVITE_EXPIRY_DAYS} days and only works when you sign in as {to_email}.\n\n"
        f"— crab.travel"
    )
    try:
        send_simple_email(subject, body, to_email, from_name="crab.travel")
    except Exception as e:
        logger.error(f"Failed to send timeshare invite email to {to_email}: {e}")


# ── Phase 2: fact views ─────────────────────────────────────

def _get_group_name(group_uuid):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT name FROM crab.timeshare_groups WHERE group_id = %s::uuid",
            (group_uuid,),
        )
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


@bp.route('/g/<group_uuid>/property')
@group_member_required()
def view_property(group_uuid):
    group_name = _get_group_name(group_uuid)
    properties = list_facts(group_uuid, 'properties')
    contracts_by_property = {}
    for prop in properties:
        contracts_by_property[prop['pk_id']] = list_facts(
            group_uuid, 'contracts', parent_id=prop['pk_id'])
    return render_template(
        'timeshare/fact_views/property.html',
        active_page='timeshare',
        group_uuid=group_uuid,
        group={'name': group_name},
        role=request.timeshare_role,
        nav_active='property',
        properties=properties,
        contracts_by_property=contracts_by_property,
    )


@bp.route('/g/<group_uuid>/finances')
@group_member_required()
def view_finances(group_uuid):
    group_name = _get_group_name(group_uuid)
    properties = list_facts(group_uuid, 'properties')
    fees_by_property = {}
    for prop in properties:
        fees_by_property[prop['pk_id']] = list_facts(
            group_uuid, 'maintenance_fees', parent_id=prop['pk_id'])
    contracts = []
    for prop in properties:
        contracts.extend(list_facts(group_uuid, 'contracts', parent_id=prop['pk_id']))
    loan_payments_by_contract = {}
    for c in contracts:
        loan_payments_by_contract[c['pk_id']] = list_facts(
            group_uuid, 'loan_payments', parent_id=c['pk_id'])
    return render_template(
        'timeshare/fact_views/finances.html',
        active_page='timeshare',
        group_uuid=group_uuid,
        group={'name': group_name},
        role=request.timeshare_role,
        nav_active='finances',
        properties=properties,
        fees_by_property=fees_by_property,
        contracts=contracts,
        loan_payments_by_contract=loan_payments_by_contract,
    )


@bp.route('/g/<group_uuid>/trips')
@group_member_required()
def view_trips(group_uuid):
    group_name = _get_group_name(group_uuid)
    trips = list_facts(group_uuid, 'trips')
    properties = list_facts(group_uuid, 'properties')
    return render_template(
        'timeshare/fact_views/trips.html',
        active_page='timeshare',
        group_uuid=group_uuid,
        group={'name': group_name},
        role=request.timeshare_role,
        nav_active='trips',
        trips=trips,
        properties=properties,
    )


@bp.route('/g/<group_uuid>/people')
@group_member_required()
def view_people(group_uuid):
    group_name = _get_group_name(group_uuid)
    people = list_facts(group_uuid, 'people')
    return render_template(
        'timeshare/fact_views/people.html',
        active_page='timeshare',
        group_uuid=group_uuid,
        group={'name': group_name},
        role=request.timeshare_role,
        nav_active='people',
        people=people,
    )


@bp.route('/g/<group_uuid>/portals')
@group_member_required()
def view_portals(group_uuid):
    group_name = _get_group_name(group_uuid)
    portals = list_facts(group_uuid, 'portals')
    # Password fields are never exposed — `encrypted_password_ref` is a pointer
    # to Secret Manager and the reveal endpoint is deferred to Phase 8 (plan §12.4).
    # Strip it from the rendered rows so no template can accidentally leak it.
    for p in portals:
        p.pop('encrypted_password_ref', None)
    return render_template(
        'timeshare/fact_views/portals.html',
        active_page='timeshare',
        group_uuid=group_uuid,
        group={'name': group_name},
        role=request.timeshare_role,
        nav_active='portals',
        portals=portals,
    )


@bp.route('/g/<group_uuid>/contacts')
@group_member_required()
def view_contacts(group_uuid):
    group_name = _get_group_name(group_uuid)
    contacts = list_facts(group_uuid, 'contacts')
    return render_template(
        'timeshare/fact_views/contacts.html',
        active_page='timeshare',
        group_uuid=group_uuid,
        group={'name': group_name},
        role=request.timeshare_role,
        nav_active='contacts',
        contacts=contacts,
    )


@bp.route('/g/<group_uuid>/documents')
@group_member_required()
def view_documents(group_uuid):
    group_name = _get_group_name(group_uuid)
    docs = list_facts(group_uuid, 'document_refs')
    return render_template(
        'timeshare/fact_views/documents.html',
        active_page='timeshare',
        group_uuid=group_uuid,
        group={'name': group_name},
        role=request.timeshare_role,
        nav_active='documents',
        docs=docs,
    )


@bp.route('/g/<group_uuid>/timeline')
@group_member_required()
def view_timeline(group_uuid):
    group_name = _get_group_name(group_uuid)
    events = list_facts(group_uuid, 'timeline_events')
    return render_template(
        'timeshare/fact_views/timeline.html',
        active_page='timeshare',
        group_uuid=group_uuid,
        group={'name': group_name},
        role=request.timeshare_role,
        nav_active='timeline',
        events=events,
    )


# ── Phase 2: generic fact CRUD ──────────────────────────────

# Maps each fact_key to the fact-view route that should receive the post-mutation redirect.
_FACT_VIEW_ROUTE = {
    'properties': 'timeshare.view_property',
    'contracts': 'timeshare.view_property',
    'people': 'timeshare.view_people',
    'maintenance_fees': 'timeshare.view_finances',
    'loan_payments': 'timeshare.view_finances',
    'trips': 'timeshare.view_trips',
    'exchanges': 'timeshare.view_finances',
    'portals': 'timeshare.view_portals',
    'contacts': 'timeshare.view_contacts',
    'document_refs': 'timeshare.view_documents',
    'timeline_events': 'timeshare.view_timeline',
}


def _redirect_back(group_uuid, fact_key):
    route = _FACT_VIEW_ROUTE.get(fact_key, 'timeshare.dashboard')
    return redirect(url_for(route, group_uuid=group_uuid))


@bp.route('/g/<group_uuid>/fact/<fact_key>/new', methods=['POST'])
@group_member_required()
def fact_new(group_uuid, fact_key):
    if fact_key not in FACT_SCHEMAS:
        abort(404)
    parent_id = request.form.get('parent_id', type=int)
    pk, err = insert_fact(group_uuid, fact_key, request.form, parent_id=parent_id)
    if err:
        flash(f"Couldn't save: {err}", 'error')
    else:
        flash("Saved.", 'success')
    return _redirect_back(group_uuid, fact_key)


@bp.route('/g/<group_uuid>/fact/<fact_key>/<int:pk>', methods=['POST'])
@group_member_required()
def fact_update(group_uuid, fact_key, pk):
    if fact_key not in FACT_SCHEMAS:
        abort(404)
    ok, err = update_fact(group_uuid, fact_key, pk, request.form)
    if err:
        flash(f"Couldn't update: {err}", 'error')
    else:
        flash("Updated.", 'success')
    return _redirect_back(group_uuid, fact_key)


@bp.route('/g/<group_uuid>/fact/<fact_key>/<int:pk>/delete', methods=['POST'])
@group_member_required()
def fact_delete(group_uuid, fact_key, pk):
    if fact_key not in FACT_SCHEMAS:
        abort(404)
    ok, err = delete_fact(group_uuid, fact_key, pk)
    if err:
        flash(f"Couldn't delete: {err}", 'error')
    else:
        flash("Deleted.", 'success')
    return _redirect_back(group_uuid, fact_key)


# ── Phase 3: ingestion ──────────────────────────────────────

MAX_PASTE_CHARS = 200_000   # plenty of room for a long CSF history email
MAX_PDF_BYTES = 20 * 1024 * 1024  # 20 MB — per-request App Engine limit is 32MB


@bp.route('/g/<group_uuid>/ingest')
@group_member_required()
def ingest_wizard(group_uuid):
    group_name = _get_group_name(group_uuid)
    return render_template(
        'timeshare/ingest/wizard.html',
        active_page='timeshare',
        group_uuid=group_uuid,
        group={'name': group_name},
        role=request.timeshare_role,
        nav_active='ingest',
    )


@bp.route('/g/<group_uuid>/ingest/paste', methods=['POST'])
@group_member_required()
def ingest_paste(group_uuid):
    from utilities.timeshare_ingest import run_extraction_and_persist
    user = session['user']
    text = (request.form.get('content') or '').strip()
    if not text:
        flash('Paste some text first.', 'error')
        return redirect(url_for('timeshare.ingest_wizard', group_uuid=group_uuid))
    if len(text) > MAX_PASTE_CHARS:
        flash(f'Paste is too long (max {MAX_PASTE_CHARS:,} characters).', 'error')
        return redirect(url_for('timeshare.ingest_wizard', group_uuid=group_uuid))

    job_id = run_extraction_and_persist(
        group_id=group_uuid,
        source_type='text_paste',
        source_content=text,
        source_ref=None,
        created_by=user['id'],
    )
    return redirect(url_for('timeshare.ingest_job_review', group_uuid=group_uuid, job_id=job_id))


@bp.route('/g/<group_uuid>/ingest/upload', methods=['POST'])
@group_member_required()
def ingest_upload(group_uuid):
    from utilities.timeshare_ingest import run_extraction_and_persist, extract_pdf_text
    user = session['user']
    f = request.files.get('pdf')
    if not f or not f.filename:
        flash('Choose a PDF to upload.', 'error')
        return redirect(url_for('timeshare.ingest_wizard', group_uuid=group_uuid))
    if not f.filename.lower().endswith('.pdf'):
        flash('Only .pdf files are supported.', 'error')
        return redirect(url_for('timeshare.ingest_wizard', group_uuid=group_uuid))
    pdf_bytes = f.read(MAX_PDF_BYTES + 1)
    if len(pdf_bytes) > MAX_PDF_BYTES:
        flash(f'PDF too large (max {MAX_PDF_BYTES // (1024*1024)} MB).', 'error')
        return redirect(url_for('timeshare.ingest_wizard', group_uuid=group_uuid))
    try:
        extracted_text = extract_pdf_text(pdf_bytes)
    except Exception as e:
        logger.error(f"PDF parse failed: {e}")
        flash('Could not read that PDF.', 'error')
        return redirect(url_for('timeshare.ingest_wizard', group_uuid=group_uuid))
    # Discard bytes explicitly — no GCS write, no disk cache.
    del pdf_bytes

    if not extracted_text.strip():
        flash('No text found in that PDF (might be a scanned image).', 'error')
        return redirect(url_for('timeshare.ingest_wizard', group_uuid=group_uuid))

    job_id = run_extraction_and_persist(
        group_id=group_uuid,
        source_type='pdf_upload',
        source_content=extracted_text,
        source_ref=f.filename[:500],
        created_by=user['id'],
    )
    return redirect(url_for('timeshare.ingest_job_review', group_uuid=group_uuid, job_id=job_id))


@bp.route('/g/<group_uuid>/ingest/jobs')
@group_member_required()
def ingest_jobs(group_uuid):
    from utilities.timeshare_ingest import list_jobs
    jobs = list_jobs(group_uuid)
    group_name = _get_group_name(group_uuid)
    return render_template(
        'timeshare/ingest/job_list.html',
        active_page='timeshare',
        group_uuid=group_uuid,
        group={'name': group_name},
        role=request.timeshare_role,
        nav_active='ingest',
        jobs=jobs,
    )


@bp.route('/g/<group_uuid>/ingest/jobs/<int:job_id>')
@group_member_required()
def ingest_job_review(group_uuid, job_id):
    from utilities.timeshare_ingest import get_job
    job = get_job(group_uuid, job_id)
    if not job:
        abort(404)
    group_name = _get_group_name(group_uuid)
    # Flatten extracted_facts into a list of (fact_key, index, row_dict) for the form
    facts = job.get('extracted_facts') or {}
    proposed_rows = []
    for fact_key, rows in facts.items():
        if fact_key.startswith('_') or not isinstance(rows, list):
            continue
        for i, data in enumerate(rows):
            proposed_rows.append({
                'fact_key': fact_key,
                'index': i,
                'data': data,
            })
    return render_template(
        'timeshare/ingest/job_review.html',
        active_page='timeshare',
        group_uuid=group_uuid,
        group={'name': group_name},
        role=request.timeshare_role,
        nav_active='ingest',
        job=job,
        proposed_rows=proposed_rows,
        no_facts_reason=facts.get('_no_facts_reason'),
    )


@bp.route('/g/<group_uuid>/ingest/jobs/<int:job_id>/commit', methods=['POST'])
@group_member_required()
def ingest_job_commit(group_uuid, job_id):
    from utilities.timeshare_ingest import get_job, commit_job
    job = get_job(group_uuid, job_id)
    if not job:
        abort(404)
    if job['status'] not in ('review',):
        flash(f"Job is {job['status']} — nothing to commit.", 'error')
        return redirect(url_for('timeshare.ingest_job_review', group_uuid=group_uuid, job_id=job_id))

    # Build the accepted-row list from the form — checkboxes named accept_<fact_key>_<index>
    facts = job.get('extracted_facts') or {}
    accepted = []
    for fact_key, rows in facts.items():
        if fact_key.startswith('_') or not isinstance(rows, list):
            continue
        for i, data in enumerate(rows):
            if request.form.get(f'accept_{fact_key}_{i}') == 'on':
                accepted.append({'fact_key': fact_key, 'data': data})
    if not accepted:
        flash('No rows selected for commit.', 'error')
        return redirect(url_for('timeshare.ingest_job_review', group_uuid=group_uuid, job_id=job_id))

    committed, errors = commit_job(group_uuid, job_id, accepted)
    if committed:
        flash(f'Committed {committed} row{"s" if committed != 1 else ""}.', 'success')
    for err in errors[:5]:
        flash(err, 'error')
    return redirect(url_for('timeshare.ingest_job_review', group_uuid=group_uuid, job_id=job_id))


@bp.route('/g/<group_uuid>/ingest/jobs/<int:job_id>/reject', methods=['POST'])
@group_member_required()
def ingest_job_reject(group_uuid, job_id):
    from utilities.timeshare_ingest import reject_job
    ok = reject_job(group_uuid, job_id, review_notes=request.form.get('notes'))
    if ok:
        flash('Job rejected.', 'success')
    else:
        abort(404)
    return redirect(url_for('timeshare.ingest_jobs', group_uuid=group_uuid))
