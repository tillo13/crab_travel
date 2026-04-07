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
- `/sms` page repositioned: "Coming with Premium" headline + banner; all compliance content preserved for any future CSP review. Now linked from footer.
- Caught-and-fixed mid-deploy: my `notifications_sent` index used a non-IMMUTABLE expression (`(sent_at AT TIME ZONE 'UTC')::date`) that silently aborted the entire migration transaction. Fixed to `(sent_at::date)`, redeployed.

---

## Remaining — Prioritized

### Before Adam Demo
- **Expense form UI** — backend + balances done; just needs a manual entry form.
- **Live test of premium SMS path** — flip Andy's `subscription_tier='premium'`, post a chat, verify Twilio attempts (will still fail with 30034, but proves the swap is wired). Then revert to `'free'`.

### High Impact (Next Session)
- **🌟 Airbnb / vacation rental integration** — THE killer feature. For 10+ person groups, one person fronts $3-5K on their card → that single anchor expense drives the entire "who owes who" flow. Investigate Airbnb Affiliate API + VRBO/Vacasa fallbacks. Show "Stays" as a separate category from hotels.
- **Carpool / pickup coordination** — flight times now stored per-member. Detect members landing at same airport within a few hours, surface "3 members arriving at PHX 2-4 PM" on the trip summary page.
- **Itinerary editor** — items exist but no add/edit/reorder UI. "Add item" button + drag-and-drop.
- **Auto-generate itinerary via AI** — when all watches are booked, LLM produces a day-by-day plan from flight times + hotel + destination research.
- **Real flight time integration** — when Duffel/Travelpayouts return real offers, store actual times in the JSONB `data` column (currently random within realistic windows).
- **Hotel check-in/out times** — store + display (typically 3-4 PM in / 10-11 AM out) for day-of coordination.

### Medium Impact
- **Amadeus flight adapter** — free tier 2K searches/month.
- **Kiwi Tequila adapter** — free for affiliates, multi-city/flexible routing.

### Infrastructure
- **`/admin/pool` page** — `pg_stat_activity` view, per-app counts, leak detection.
- **`crab.cron_executions` table** — persistent execution log for cron health monitoring.

### Future Thinking — Multi-Modal Trips (notes only, no scope)
The current model assumes everyone flies + books a hotel. Real groups are messier:
- **Getting there:** RV rentals (Outdoorsy/RVshare), Amtrak, carpooling, mixed-mode within one trip.
- **Staying there:** vacation rentals (whole-home for big groups), staying with friends/family ("I'm crashing at my cousin's"), camping, hostels, timeshares, split stays.
- The Big Insight: CrabAI shouldn't assume one transport / one accommodation per trip. Members should be able to mark "I have my own accommodation" and be excluded from hotel splits while still in activity/meal splits. The expense math has to account for all of it.

---

## Reference

### API Cost Awareness

| API | Monthly Cost | Status |
|---|---|---|
| Duffel | ~$0 (no bookings) | Demoted to fallback |
| Travelpayouts | $0 (affiliate) | Primary flight source |
| Xotelo | N/A | Removed (RapidAPI auth required) |
| LiteAPI | $0 (sandbox) | Hotel fallback |
| LLM Router | $0 (free tier round-robin) | 15+ backends |
| YouTube Data API | $0 (100k/day approved) | Active |
| Twilio | ~$0.01/msg | A2P stuck in TCR queue; SMS deferred to Premium tier |
| Gmail SMTP | $0 | Primary notification channel today (~500/day cap on personal account) |

### DB Connection Budget

| App | maxconn | | App | maxconn |
|---|---|---|---|---|
| galactica | 6 | | scatterbrain | 2 |
| crab_travel | 6 | | stealth | 2 |
| kumori | 3 | | kindness_social | 3 |
| dandy | 2 | | ooqio | 2 |
| 2manspades | 2 | | wattson | 2 |

**Total: 30/50** — 20 headroom. All apps: `statement_timeout=30s`, `connect_timeout=10`.

### Demo Trip Reference

| Stage | Token | URL | Trip |
|---|---|---|---|
| Booked | `qL6zhRAI` | `crab.travel/demo` | Scottsdale, AZ (12 members, fully booked) |
| Voting | `xL2aRt-k` | `crab.travel/demo/voting` | Reykjavik / Marrakech / Luang Prabang (50 members) |
| Planning | `TpPeETPm` | `crab.travel/demo/planning` | Andes / Ushuaia (75 members) |

Judy Tunaboat (`user_id=81869`) is a member of all bot trips, organizer of every 20th.

### SMS / Twilio Resources (deferred but documented)

- Campaign: `QE2c6890da8086d771620e9b13fadeba0b` (still IN_PROGRESS in TCR queue)
- Phone: `+1 (425) 600-CRAB` (425-600-2722)
- Compliance page: [crab.travel/sms](https://crab.travel/sms)
- Full history: [`docs/twilio_a2p_campaign.md`](docs/twilio_a2p_campaign.md)
- Provider escape hatch: [`docs/twilio_escape_hatch.md`](docs/twilio_escape_hatch.md) (Telgorithm needs sales call; Telnyx/Plivo same TCR queue as Twilio)
- Health probe: scatterbrain `probe_twilio_a2p` runs daily 07:15 PT, alerts only on state change
