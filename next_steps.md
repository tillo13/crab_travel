# next_steps — crab_travel

<!-- Auto-maintained.
     • Append a pending item:  `deploy "msg" --next "thing to do later"`
     • Standalone queue (no commit):  `deploy --next "thing to do later"`
     • The nightly cron rewrites the Shipped and Unfinished sections. -->

*Last refreshed: 2026-04-13 04:00*

## 🎯 Pending

<!-- pending:start -->
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
- `32e615c` · 2026-04-12 15:47 — chore: remove dead llm_usage_caps.py — replaced by litellm_plus_router DB-driven limits
- `74ca5c8` · 2026-04-12 15:21 — refactor: replace llm_router with thin wrapper over shared litellm_plus_router
- `d271488` · 2026-04-12 11:44 — Fix SEO: remove .rstrip from www redirect, add 301 to app.yaml
- `6e7a2a4` · 2026-04-10 14:27 — Collapse vote-reminder dedup+cap into one LEFT JOIN scan (db-speed-first)
- `2d09429` · 2026-04-10 14:20 — Fix pre_deploy_test command: python → python3
- `6032280` · 2026-04-10 14:17 — Wire smoke_test.py as pre_deploy_test hook
- `a5d9459` · 2026-04-10 14:13 — Add reminder cap smoke test + fix section 4 cleanup timeout
- `f9c7425` · 2026-04-10 09:20 — Cap vote reminders at 3 per plan/user — stop nagging after 3 days
- `a7e1658` · 2026-04-09 08:44 — Update Twilio A2P cost table with verified usage numbers
- `d011be9` · 2026-04-09 08:36 — Twilio A2P attempt 7: update docs for /profile/demo CTA strategy
- `bb60eed` · 2026-04-09 08:32 — Add /profile/demo public CTA preview for Twilio A2P reviewer
- `0e80e2c` · 2026-04-07 16:36 — Inline contact-form spam guard (kumori module not bundled)
- `ee25e17` · 2026-04-07 16:33 — Fix demo stage toggles + contact form 500s
- `c251c3f` · 2026-04-07 13:09 — fix: remove cross-site footer links + kumoridotai OAuth API send
- `036b888` · 2026-04-07 09:21 — Add Demo link to navbar
<!-- shipped:end -->

## ⚠️ Unfinished / WIP

<!-- wip:start -->
**1 file(s) with uncommitted changes:**
- ` M utilities/litellm_plus_router.py`

<!-- wip:end -->
