# 2026-05-04 — Daily heartbeat shipped + OpenCrab per-plan emails silenced

## What shipped

### 1. `/cron/daily-heartbeat` (8am PT, daily)

New blueprint at `daily_heartbeat.py`, registered in `app.py`. Sends ONE
exception-report email per day to `andy.tillo@gmail.com` covering all of
crab.travel in two sections:

- **Is crab healthy?** — ✅/⚠️/🔴/🟠 status of every cron, db pool,
  OpenCrab pipeline, LLM routing. One line each. If everything green you
  stop reading.
- **Things we're waiting on** — active price watches (count, oldest pending,
  never-matched), II scrape queue depth, open plans, leg_hunts due in
  24h+. Only renders sections with non-zero pending state.

Subject-line badge for inbox-preview triage:
- `🟢 all green`
- `🟠 N stale` (something hasn't refreshed in expected window)
- `🔴 N down` (something's actively broken)
- `⚠️  N broken-check` (a query in the digest itself failed)

Each query is wrapped in `_safe()` so a single missing column or schema
drift never silences the whole digest — broken checks render as `⚠️` rows
with the error excerpt instead.

Verified: live test at `https://crab.travel/cron/daily-heartbeat?force=1`
returned `{"badge":"🟢 all green","health_items":3,"waiting_items":4,
"sent":true}` and the email landed.

### 2. OpenCrab per-plan test emails silenced

The 5 daily `[OPENCRAB TEST]` emails (one per active test plan: Boston
college reunion, Tahoe ski, NYC bachelor, SEA→MIA, etc.) were proving
"OpenCrab is firing" but adding no information past the first one. Folded
into the heartbeat's OpenCrab line.

**New mode in `opencrab_routes.py`:** `CRAB_OPENCRAB_TEST_MODE='digest_only'`.
When set, `/api/opencrab/notify` records to `crab.notifications_sent`
(notification_type='email_suppressed') but skips the actual `send_simple_email`
call. The heartbeat's `_check_opencrab` rolls up: "OpenCrab: N notifications
recorded across M plans in last 24h."

**Secret config (live in `crab-travel` project):**
- Secret `CRAB_OPENCRAB_TEST_MODE` created with value `digest_only`
- App Engine service account `crab-travel@appspot.gserviceaccount.com`
  granted `roles/secretmanager.secretAccessor` on this secret
- `_digest_only_mode()` does no caching → next notify call picks up changes
  with no redeploy required

### 3. `gmail_utils.send_simple_email` now accepts `html=...`

Signature: `send_simple_email(subject, body, to_email, from_name='crab.travel', html=None)`.
When `html` is provided, sends `multipart/alternative` with both plain and HTML
parts. Required for clean Gmail deliverability per global CLAUDE.md
"Email sending — Gmail API pattern" rule. Existing call sites (contact
form, killswitch) keep working — they just send plain text as before.

## Three valid `CRAB_OPENCRAB_TEST_MODE` values

| Value | Behavior | When to use |
|---|---|---|
| `on` | Reroute member emails to admin with `[TEST → would've gone to <real>]` prefix | Default until OpenCrab + this app are both stable enough to actually email real members |
| `digest_only` | Record to notifications_sent, suppress email | **Current setting.** Heartbeat shows OpenCrab is alive without inbox flood |
| `off` | Real members get real emails | Production go-live |

Flip via:
```bash
gcloud secrets versions add CRAB_OPENCRAB_TEST_MODE \
  --data-file=- --project=crab-travel <<< '<value>'
```

## How to debug if the heartbeat goes quiet

1. Check App Engine logs for `/cron/daily-heartbeat` invocation around 8am PT
2. If the cron did fire but no email arrived, check for `Email send failed:`
   in the logs (Gmail API auth, kumoridotai@gmail.com mailbox issues)
3. Hit the route manually: `curl https://crab.travel/cron/daily-heartbeat?force=1`
4. To verify the OpenCrab silencing is still in effect:
   `gcloud secrets versions access latest --secret=CRAB_OPENCRAB_TEST_MODE --project=crab-travel`
   should return `digest_only`

## Files touched

- `daily_heartbeat.py` (new)
- `app.py` — register `heartbeat_bp`
- `cron.yaml` — add `/cron/daily-heartbeat` daily 08:00 PT
- `opencrab_routes.py` — add `_digest_only_mode()` + suppress branch in `/api/opencrab/notify`
- `utilities/gmail_utils.py` — `send_simple_email(html=…)` extension

## Followups

- After a few weeks, if the 🟢 line is consistently uninformative, consider
  collapsing the all-green case to a one-line subject-only email so you
  don't even open it on green days. Right now keep the body so you can
  spot the "🟠 watches haven't been checked in 11h" type drift early.
- The pending TODOs from `20260428_places_api_refund_closeout.md` remain:
  `DELETE FROM crab.ii_resort_google;` + `timeshare_catalog.py` JOIN cleanup.
- If scatterbrain (or other projects) want the same shape, copy `daily_heartbeat.py`
  as a template — the `_safe()` + `_check_*` + `_waiting_*` pattern is
  re-usable as long as each project provides its own queries.
