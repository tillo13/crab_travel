# next_steps — crab_travel

<!-- Auto-maintained.
     • Append a pending item:  `deploy "msg" --next "thing to do later"`
     • Standalone queue (no commit):  `deploy --next "thing to do later"`
     • The nightly cron rewrites the Shipped and Unfinished sections. -->

*Last refreshed: 2026-05-04 04:07*

## 🎯 Pending

<!-- pending:start -->
- [ ] **Google Maps Platform refund — Case #70458922 (status 2026-04-25)**
  - Eljohn (Google Maps Platform Billing Support) replied 2026-04-25 00:29 GMT: adjustment request received and forwarded to specialized team for review.
  - Expectation set by Google: **partial adjustment likely, full refund not guaranteed** ("we don't provide a full adjustment amount, all adjustments are subject to approval"). Update promised within 24–48h of specialized-team decision.
  - Reply drafted in Gmail (threaded on case) confirming technical side is fully closed: all Maps Platform APIs disabled, runaway endpoint removed, no re-enable until ToS-compliant design + daily hard caps in place. Andy to send.
  - Next action: wait for Eljohn's follow-up; if no word by 2026-04-28, nudge politely on the same thread.

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
- [ ] 2026-04-21 14:09 — timeshare: 'Worth it' dashboard card — lifetime CSF / trips / cost-per-trip / exchange-vs-retail savings math. Celeste's is-it-worth-keeping answer in one glance.
- [ ] 2026-04-21 14:09 — timeshare: 'Start a 2026 cycle from this resort' button on resort detail — closes discovery→booking loop, auto-seeds Phase 7 cycle plan with resort locked as destination.
- [ ] 2026-04-21 14:09 — timeshare: searchbar autocomplete — type 'Carib' → dropdown of matching destinations + resorts. Fastest path with 2,491-resort catalog.
- [ ] 2026-04-21 14:09 — timeshare: expose chatbot search on dashboard as NL prompt — 'Find me 2BR Caribbean 4.5+ rating Friday check-in'. search_resort_catalog tool already exists, just surface it.
- [ ] 2026-04-21 14:09 — timeshare: weather/season filter on search — 'best in Jan-Mar' derived from Google Place types + area latitude. After core UX lands.
- [ ] 2026-04-21 14:09 — timeshare: auto-ingest Drive watcher — monthly cron scans dossier folder, new files auto-ingest + flag for review. No manual re-paste ever again.
<!-- pending:end -->

## ✅ Recently shipped

<!-- shipped:start -->
- `267c546` · 2026-05-03 10:16 — kill all II infra — RedWeek is the rental path now (removed keep-alive cron, Cloud Run scraper, s...
- `f4942cd` · 2026-05-01 13:50 — sync kumori_free_llms.py from canonical infra — picks up circuit breaker (cooldown_until / consec...
- `e5c5c88` · 2026-04-28 21:13 — timeshare: enable II keep-alive cron (every 18 min, jittered 18-29) + cookie refresh API
- `446c247` · 2026-04-28 13:53 — timeshare: unified nav — 5 decision tabs primary + 'More ▾' dropdown for the other 9
- `4e144ac` · 2026-04-28 13:27 — timeshare: mobile polish — readonly nav trim + Ask copy rewrite + Finances/Trips card layout unde...
- `7379e6c` · 2026-04-28 13:14 — timeshare: shorten share URL via /s/<code> + add 'Text it' SMS launcher on Members panel
- `c9a8b2d` · 2026-04-28 13:00 — timeshare: 7-day expiry on share links + expired-link page + soft sign-in hint
- `8226c87` · 2026-04-28 12:48 — timeshare: public read-only share link (Google-Docs style) — generate/rotate/disable from Members...
- `f263ae9` · 2026-04-28 12:20 — timeshare: add /test/seed-readonly endpoint for Playwright readonly walkthrough (apikey-gated)
- `01c4470` · 2026-04-28 12:10 — timeshare: readonly role works + Considering rebrand + welcome banner + HTML invite email
- `bf798fc` · 2026-04-28 11:07 — fix: dedup before_request hooks + cache CRAB_TEST_APIKEY + 500 handler + view_plan logging
- `602403d` · 2026-04-27 10:06 — rip dead Amadeus self-service API + ScrapingBee/Xotelo/git_push.sh/_antiquated cleanup
<!-- shipped:end -->

## ⚠️ Unfinished / WIP

<!-- wip:start -->
**4 file(s) with uncommitted changes:**
- ` M dev/dedup_fuzzy.py`
- ` M docs/20260502_redweek_next_steps.md`
- ` M utilities/backend_registry.py`
- ` M utilities/kumori_free_llms.py`

<!-- wip:end -->
