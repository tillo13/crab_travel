"""
Timeshare blueprint — Phase 1 scope.

Routes shipped in Phase 1:
- GET  /timeshare/                              indexable landing
- GET  /timeshare/groups/new                    creation form
- POST /timeshare/groups/new                    create + auto-add creator as owner
- GET  /timeshare/g/<uuid>/                     dashboard (member-only, 404 on miss)
- GET  /timeshare/g/<uuid>/members              list + invite form
- POST /timeshare/g/<uuid>/members/invite       create invite row + send shortlink email
- GET  /timeshare/g/<uuid>/members/accept/<token>   accept (login-required; invite-token gated)

Structured fact views, ingestion, chatbot, II catalog, cycle bridge — Phases 2+.
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

    return render_template(
        'timeshare/dashboard.html',
        active_page='timeshare',
        group=group,
        member_counts=counts,
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
