# next_steps — crab_travel

<!-- Auto-maintained.
     • Append a pending item:  `deploy "msg" --next "thing to do later"`
     • Standalone queue (no commit):  `deploy --next "thing to do later"`
     • The nightly cron rewrites the Shipped and Unfinished sections. -->

*Last refreshed: 2026-04-21 04:32*

## 🎯 Pending

<!-- pending:start -->
- [ ] **OpenClaw tenant expansion — survey complete, ready to pick next migration**
  - Canonical spec: [`~/Desktop/code/_infrastructure/docs/openclaw/MANIFEST.md`](../_infrastructure/docs/openclaw/MANIFEST.md) (least-privilege rules, layout, reuse checklist)
  - Community consensus (from 336-comment r/LocalLLaMA thread + r/OpenClawCentral scrapes, Apr 2026): scheduled digests, cron-driven deterministic ops, DB-backed monitors, IM bridges
  - **Top 3 candidates, ranked by practical fit + impact today:**
    1. **scatterbrain / `dev-intel` Reddit scan** (fit 10/10) — daily 20-sub scrape → kumori gateway Haiku tagging → `POST /api/dev-intel/results` write-back. Highest ROI, lowest risk (public Reddit, scraper logic already exists).
    2. **wattson / Shelly Cloud poll** (fit 9/10) — every-minute polling for gym device meters; isolates from App Engine web tier. Bigger blast radius (B2B customer data) — do this AFTER scatterbrain validates the multi-tenant pattern.
    3. **kumori / heartbeat** (fit 9/10) — `ping-apps` (10min) + `db-health-check` (5min) across the 12-app fleet. Recursive elegance: the thing monitoring App Engine isn't itself on App Engine. Add panel to `claw.crab.travel` dashboard.
  - **Medium fit** (do opportunistically): kindness_social scrape-topics + daily-digest, taco_and_red/tiktok_automation trend scraper, galactica QA bot, stealth LinkedIn refresh crawler
  - **Hard skips** (disqualified by manifest rules): kicksaw (Salesforce/QBO/Bill.com paid creds on request path), personal_finance (PII max), dr_nick (needs VPN kill-switch, fixed VPS IP defeats threat model), zynga_sweeper (ToS/ban risk with fixed IP), rog_gateway (peer infra not tenant)
  - **Order of operations:** scatterbrain → kumori → wattson. Each migration: design owning-service discovery + guardrailed write-back endpoint FIRST, save reference memory in that project's memory dir pointing back to the manifest, watch dashboard for 24h before adding the next tenant.

- [ ] _legacy next_steps contents preserved below — Andy, curate manually_

```
# crab.travel — Next Steps
*Updated: 2026-04-07*

> Prior session logs archived to [`docs/20260407_next_steps.md`](docs/20260407_next_steps.md). This file is the live working doc — kept short on purpose.

---

## Status at a Glance

| Topic | State |
|---|---|
| **YouTube Data API quota** | ✅ APPROVED Apr 6 — 100k units/day. Seed throttles relaxed (10/run, 25 stale). |
| **SMS — Twilio A2P** | ⏸ Deferred. SMS now ships as a Premium-tier feature; email handles all flows today. Tier flip enables SMS instantly per-organizer. |
| **Bot cron stability** | ✅ Subprocess now blocks (was Popen-detached → SIGKILL'd off-hours). Failure rate should drop to ~0. |
| **Premium tier scaffolding** | ✅ `users.subscription_tier` column live, dispatcher routes by tier, profile UI shows PREMIUM badges, /sms repositioned. |
| **Notification dispatcher** | ✅ Single source of truth at `utilities/notification_utils.py` for chat / price drops / vote reminders. |
| **Vote reminder cron** | ✅ Live at `/tasks/vote-reminders` daily 09:00 PT, idempotent via `crab.notifications_sent`. |
| **Adam demo readiness** | 🟡 SMS demo moment now requires flipping Andy's `subscription_tier='premium'` (and Twilio still needs to actually deliver — still 30034). Email parity is enough for the rest. |

---

## Done This Session (2026-04-07)

### YouTube quota relief
- Quota approved Apr 6 (100k/day on `kumori-404602`).
- `MAX_ITINERARIES_PER_RUN`: 2 → 10 ([app.py:3307](app.py#L3307)).
- Stale-fix batch: 10 → 25 ([app.py:3280](app.py#L3280)).

### Bot cron off-hours failure fix
- Symptom: ~25% of bot runs were "failed" — turned out to be `subprocess.Popen` detaching, then App Engine scaling down the instance and SIGKILLing the running bot. Off-hours skew confirmed it.
- Fix: switched to `subprocess.run(..., timeout=540)` so the cron handler blocks and keeps the instance alive ([app.py:2571](app.py#L2571)).

### Admin stats card upgrade
- `/admin/ops` Recent Bot Runs card now shows last 7 days breakdown + all-time totals + pass-rate ([utilities/admin_utils.py:469-491](utilities/admin_utils.py#L469), [templates/admin_ops.html](templates/admin_ops.html)).

### Email-first notifications + Premium tier
- New `crab.users.subscription_tier` column (default `'free'`) + `crab.notifications_sent` dedupe table.
- New unified dispatcher [`utilities/notification_utils.py`](utilities/notification_utils.py) — single source of truth for chat / price drops / vote reminders. Tier rule: a member's `notify_channel='sms'/'both'` is honored only if the trip's organizer is `subscription_tier='premium'`.
- Refactors: [`utilities/watch_engine.py:395`](utilities/watch_engine.py#L395) (price drops), [`app.py:2818`](app.py#L2818) (chat). All three flows now route through the dispatcher.
- New cron [`/tasks/vote-reminders`](app.py#L2633) — daily 09:00 PT, idempotent.
- Profile UI: SMS/Both radios now show teal "PREMIUM" badge + explainer text ([templates/profile.html](templates/profile.html)).
- `/sms` page repositioned: 
...
```
<!-- pending:end -->

## ✅ Recently shipped

<!-- shipped:start -->
- `2b5e68e` · 2026-04-18 15:35 — twilio A2P approved: update sms.html banner + roadmap + twilio_a2p_campaign.md / next_steps.md / ...
- `9170ca7` · 2026-04-18 15:33 — copy refresh: CrabAI hunting framing + OpenClaw nerd callout on about page
- `c33be6d` · 2026-04-18 14:43 — opencrab: expose source_watch_id on legs-to-hunt + board reads from transport_options
- `467f547` · 2026-04-18 13:25 — add trip_legs + transport_options schema, /transport-options + /legs-to-hunt endpoints, backfill ...
- `180b564` · 2026-04-18 08:25 — OpenCrab deal board: plan-page 🦀 CrabAI Deal Hunter accordion + /watch-results write-back endpoin...
- `de71074` · 2026-04-17 18:15 — duffel adapter: switch fallback deep link from deprecated Google Flights #flt= hash (lands on hom...
- `a7cd0ae` · 2026-04-17 17:02 — duffel adapter: build Google Flights deep links with real dates so users land on actual results
- `68494ff` · 2026-04-17 16:50 — opencrab: exclude [BOT] plans from plans-eligible — crawl only real plans
- `ba82056` · 2026-04-17 13:22 — opencrab: plans-eligible returns cached last/best prices per watch
- `e70741d` · 2026-04-17 13:06 — opencrab: fix plans-eligible days_out int cast
- `b7018f7` · 2026-04-17 13:02 — opencrab: /api/opencrab/notify endpoint + admin recipient secret + test-mode gate + daily caps
- `e2bf59c` · 2026-04-17 10:59 — add /api/watches/<id>/explore bearer-authed endpoint for OpenCrab route exploration; ignore _priv...
- `6907a79` · 2026-04-16 14:44 — disable vote-reminder cron
- `a203b5b` · 2026-04-16 14:44 — disable vote-reminder cron until prod — burst sends (3 emails in 6s to same inbox across plans) g...
- `2f08196` · 2026-04-15 20:29 — Twilio A2P attempt 8 prep: uncheck demo consent box, hide Premium badge on demo, add 'not a condi...
<!-- shipped:end -->

## ⚠️ Unfinished / WIP

<!-- wip:start -->
**2 file(s) with uncommitted changes:**
- ` M next_steps.md`
- `?? timeshare.md`

<!-- wip:end -->
