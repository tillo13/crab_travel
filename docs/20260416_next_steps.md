# crab.travel — Session Notes & Next Steps (2026-04-16)

Carries the thread from [`docs/20260322_next_steps.md`](20260322_next_steps.md) (5 weeks ago) and [`docs/20260407_next_steps.md`](20260407_next_steps.md) (9 days ago).

---

## Today's Big Unlocks

### 🎉 Twilio A2P: VERIFIED
- Campaign `C4JM2GC` cleared on **2026-04-09** (attempt 7)
- First live production SMS delivered at 17:23 PT from **+1 425-600-CRAB** (2722 → CRAB on the keypad — intentional)
- Total sunk cost: **~$154.50** in A2P fees (brand + 5× campaign vetting @ $15 + one-time fees) + ~$2.30/mo recurring for the number
- Attempt 8 commit (`2f08196`, 4/15) — defensive prep for another submission — turned out moot. The campaign had already cleared 6 days earlier. `probe_twilio_a2p` in scatterbrain would have caught it tomorrow morning (07:15 PT daily).
- Rate limits: AT&T **1.25 msg/sec**, T-Mobile **LOW** brand tier. Fine for our volume.
- All downstream SMS code paths (`utilities/sms_utils.py`, `/api/sms/inbound`, `utilities/notification_utils.py`) have been ready this whole time. Flipping any organizer's `subscription_tier='premium'` activates SMS for their plan members with zero code changes.

### OpenClaw pilot scoped (new greenfield infra)
- Reviewed 23 apps in `~/Desktop/code/` against OpenClaw's value prop
- Original pick: `crab_travel` daily price-watch (P4 from March plan)
- Better fit after seeing the March priority list: **P3's "AI-suggested ordering" itinerary brain** — multi-step agentic reasoning over Google Places + weather + activities + flights. Doesn't exist in the codebase yet, exactly OpenClaw's sweet spot.
- Same infra plan, different skill target. Full walkthrough: [`_private/openclaw_stepbystep.md`](../_private/openclaw_stepbystep.md).
- Infra (isolated from kumori GCP project on purpose):
  - Contabo Cloud VPS 10 "OpenClaw Personal Use" — €3.60/mo, 4 vCPU, 8 GB RAM
  - Bare Ubuntu 22.04 + Clawdboss hardened install (94/100 pentest, WAL Protocol, prompt-injection defense)
  - LiteLLM gateway for LLM ($0 via free models + $1 budget cap virtual key)
  - Telegram bot for operator alerts
  - Read-only Postgres user (`openclaw_readonly`, SELECT on 3 tables)
  - Writes back to crab_travel ONLY via bearer-auth'd endpoint (no direct DB writes)

---

## Progress Since 2026-03-22

The March 22 priority table and where each item lands now:

| # | March 22 scope | Today |
|---|---|---|
| P1 | Per-member flight/hotel watches + price tracking | ✅ Shipped — `utilities/watch_engine.py`, `crab.member_watches`, `crab.watch_history`, cron live every 6h |
| P2 | Per-person cost breakdown (spreadsheet killer) | ✅ Shipped — expenses + balances + Venmo/Zelle deep links |
| P3 | Day-by-day itinerary builder | 🟡 Partial — `crab.itinerary_items` table + Google-search links on item cards. AI-ordering brain and drag-to-timeslot UI still TODO |
| P4 | Price drop alerts | ✅ Shipped — unified through `utilities/notification_utils.py` |
| P5 | Email digest for lurkers | ❌ Not started |
| P6 | SMS voting & inbound commands | 🔓 **Unblocked today** — was "ready to build the moment Twilio A2P clears" |

Other shipped work since 3/22 (not on the original priority list):
- **Premium-tier architecture** — `users.subscription_tier` column; dispatcher honors `notify_channel='sms'/'both'` only when organizer = premium
- **Unified notification dispatcher** (`utilities/notification_utils.py`) — single source of truth for chat / price drops / vote reminders
- **Vote reminder cron** at `/tasks/vote-reminders` daily 09:00 PT, idempotent via `crab.notifications_sent`
- **Daily health-check cron** in scatterbrain (`probe_twilio_a2p`, `probe_twilio_sms_health`, `probe_youtube_quota`, `probe_youtube_errors`) — emails only on state change
- **YouTube Data API quota** — 10k → 100k units/day (approved 4/6). Seed throttles relaxed.
- **SEO canonical fix** — `force_canonical_host()` 301 + hardcoded canonical/og:url in base template
- **Crab Crawlers v2** with Haiku-powered random destinations (Zanzibar, Samarkand, Tromsø, etc.)
- **/live departures board** with Voting/Charting/Booked lifecycle tabs
- **Judy Tunaboat demo viewer** + auto-login on bot trips
- **CrabAI branding pass** across all templates
- **Pool auto-recovery** + `/_ah/warmup` handler for App Engine cold starts
- **Bot cron stability** — `subprocess.run` blocks, no more SIGKILL at scaledown
- **LiteLLM integration** — crab_travel now routes all LLM calls through `llm.kumori.ai` with per-app metadata tagging

---

## Priority Order — What's Next

### PRIORITY 1: Production SMS validation end-to-end (30 min)

The campaign is VERIFIED but we've only sent one raw-API test. Need to confirm the full production path: `dispatcher → sms_utils → MessagingService → carrier → recipient`.

Steps:
1. Flip Andy's `users.subscription_tier = 'premium'`
2. Set Andy's `notify_channel = 'sms'` on `/profile`
3. Trigger a real flow via the dispatcher:
   - Post a chat message in a trip Andy's in
   - OR force a price drop on a watched route via `/tasks/check-watches`
   - OR trigger a vote reminder manually
4. Confirm text lands with proper formatting:
   - No `[BOT]` prefix leaking through
   - Proper opt-out footer ("Reply STOP to unsubscribe")
   - Deep link present and correct
5. Verify inbound: text `STOP` to +14256002722, confirm user's `notify_channel` flips off and `/api/sms/inbound` logs the event

Success gates the Adam pitch ("SMS is live for premium organizers") and everything in P2 below.

---

### PRIORITY 2: SMS Voting & Inbound Commands (P6 carryover, now unblocked)

The March 22 note: *"Ready to build the moment Twilio A2P campaign clears."* It cleared.

Scope (all plumbing exists — this is a command parser):
- **Outbound vote prompts** — hook into vote reminder cron: "Reply 1 for Nashville, 2 for Phoenix"
- **Inbound vote parsing** at `/api/sms/inbound` — recognize `1`, `2`, `3`, `VOTE NASHVILLE`, etc. Map to existing vote-rank endpoints.
- **RSVP confirmation via text** — "Reply YES to confirm you're in"
- **Chat-to-SMS bridge** — outbound piece is wired; inbound piece should post into trip chat as the sending member
- **Flight info collection** — "Reply with your flight confirmation code" → parse 6-char PNR, store in member's watch JSONB

Proposed new file: `utilities/sms_commands.py` — stateless parser returning `(intent, entities, plan_id)`. Dispatched from `/api/sms/inbound`.

Write tests first for this one. Inbound SMS can be anything — emoji, typos, replies to old threads, auto-replies from user devices. The parser needs to fail safely.

---

### PRIORITY 3: OpenClaw Itinerary Planner (new AI brain for P3)

Multi-step agentic workload. Never tried OpenClaw in the stack before — this is the pilot. If it works, the pattern replicates across other apps.

**Goal:** Always-on agent that drafts day-by-day itineraries for locked trips using geography, weather, activity inventory, and member preferences — and iterates based on group feedback.

**Skill design:**
1. Every 6h, scan for plans where `status='locked'` AND `itinerary_items` is empty OR `locked_at > latest_item.created_at - 24h`
2. For each, pull: destinations, dates, members, preferences, existing flight/hotel data
3. Call external APIs:
   - Google Places — geography + operating hours
   - OpenWeatherMap — weather-aware ordering
   - Viator — activity inventory
4. Synthesize day-by-day draft (LLM reasoning)
5. `POST /api/plan/<id>/itinerary-suggest` with bearer token — writes `crab.itinerary_items` rows marked `status='draft'`
6. Ping Andy (or organizer) via Telegram with review link
7. On human approval via dashboard, endpoint flips `draft` → `published`, chat broadcasts "Suggested itinerary ready"

**New endpoint in `app.py`:**
- `POST /api/plan/<id>/itinerary-suggest` — accepts bearer auth, writes draft items only, returns review URL
- `POST /api/plan/<id>/itinerary-publish` — flips drafts to published (human-only)

**Blast radius: zero.** Agent reads DB, writes ONLY through the above endpoint, can only propose drafts. If it hallucinates, a human reviews before anything ships to users.

**Success criteria for keeping OpenClaw in the stack 30 days in:**
- Drafts land within 6h of plan lock
- >70% of drafts accepted with minor edits (not tossed)
- $0 LLM cost (virtual key $1 budget holds)
- Zero prompt-injection incidents
- Zero stray writes to tables other than `itinerary_items` (DB-level enforced via GRANT)

**Blocked on:** Andy completing Contabo signup + pre-flight checklist in [`_private/openclaw_stepbystep.md`](../_private/openclaw_stepbystep.md).

---

### PRIORITY 4: P3 Drag-to-Timeslot UI (frontend half)

Once OpenClaw is dropping drafts into `crab.itinerary_items`, users need a way to rearrange them.

Scope:
- Drag destination pins between day/time slots (morning/afternoon/evening)
- Shared view — all members see the same schedule, update in near-real-time (poll or SSE)
- Free-form notes per day
- Mobile-optimized for on-the-ground use during the trip
- Reorder persists via `PATCH /api/plan/<id>/itinerary-items` with ordered ID list

Standard React-DnD or similar. Not hard once the backend is in place.

---

### PRIORITY 4.5: OpenCrab route-explorer (new skill — 2026-04-17)

**Name:** The on-VPS agent is branded **OpenCrab**. All paths/files/env renamed 2026-04-17. Only leftover: root-owned `pincer-dashboard.service` systemd unit (cosmetic — runs the same script; rename needs sudo).

**Why this exists:** Reddit deep-dive (2026-04-17) confirmed that OpenClaw's actual differentiator isn't LLM access (crab already has `kumori_free_llms` at `app.py:756`) or one-shot search (crab already has 4 adapters: Duffel, LiteAPI, Viator, Travelpayouts in `utilities/search_engine.trigger_search`). It's **stateful, conversational, multi-skill pursuit of an unbooked goal** over time. The current `price-drop-watch` skill is underutilization — it replicates what a 50-line cron could do.

**Goal:** Turn OpenCrab from alerter → concierge. Each unbooked `crab.member_watches` row gets *continuous exploration* across a broader space, with a conversational Telegram loop.

**Skill scope (phase 1 — keep cheap):**
1. Hourly tick: for each active unbooked watch, reuse crab's existing adapters via a new `POST /api/watches/<id>/explore` bearer endpoint. Agent does NOT re-scrape Google Flights.
2. Expand route space automatically:
   - Nearby origins (within ~150mi): PDX, BLI, YVR, PAE for SEA-based watches
   - Nearby destinations: FLL/PBI for MIA, RSW for SWFL, etc.
   - Date shifts: ±3 days
3. Layer in context crab doesn't natively: weather at destination on those dates (OpenWeatherMap — already in API list), event conflicts vs user's stored `crab.member_blackouts`.
4. **Conversational Telegram DM loop** — the actual differentiator:
   - "any drops today?" → digest
   - "try FLL instead" → re-explore
   - "book it" → hand off to crab's existing affiliate booking flow
   - Preference memory: "no red-eyes", "aisle seat" — stored in OpenCrab's local memory, reused every exploration

**Explicit non-goals (phase 1):**
- No scraping Going.com / Secret Flying / Reddit mistake-fare threads. Rabbit hole, brittle, rate-limited. Defer to phase 2 after proving conversational value.
- No booking writes. Hand off to crab's existing commission path — don't duplicate.

**Blast radius:** zero new write paths. OpenCrab reads via read-only DB user + hits crab's bearer endpoints for suggestions. Booking stays in crab.

**Dependency:** crab needs a new `POST /api/watches/<id>/explore` endpoint that wraps `trigger_search` and returns structured results. Smallest possible delta.

**Success criteria (30-day read):**
- Andy has ≥1 real Telegram conversation per week with OpenCrab about SEA→MIA watch
- ≥1 non-obvious option surfaced that the single-route Google Flights scraper would have missed (e.g. PDX→MIA savings, FLL alternative, date shift)
- Zero bookings lost to OpenCrab going rogue (it can only suggest, not book)

**Design doc:** [`_private/opencrab_route_explorer.md`](../_private/opencrab_route_explorer.md) (to be drafted before any VPS changes).

---

### PRIORITY 5: Email Digest for Lurkers (P5 carryover)

Weekly summary for users who never open the site.

- Vote standings, price changes, chat highlights, new members
- Smart send — fires only when there's actual news (dedupe via `crab.notifications_sent`)
- One-click actions from email (vote link, view prices, see itinerary)
- Configurable frequency per user (daily / weekly / milestones only)
- Infra: Gmail sending already works via `utilities/gmail_utils.py`. Need template + cron at `/tasks/email-digest`.

---

## What Each Priority Depends On

- P1 → nothing, ready now
- P2 → P1 (confirm production SMS works before building on it)
- P3 → Andy completing Contabo signup + Clawdboss install + API wiring (see `_private/openclaw_stepbystep.md` phases)
- P4 → P3 backend (UI reorders what the OpenClaw agent drafts)
- P5 → independent, can run in parallel with any of the above

---

## Ideas From This Session (Not Yet Prioritized)

### Telegram for internal ops notifications
Today's Reddit scrape confirmed: Telegram is a free SMS-replacement for *internal* notifications only (customers don't have Telegram installed, so it cannot replace Twilio for user-facing SMS). But internal ops candidates:
- scatterbrain health checks → Telegram instead of email (faster phone push)
- `/tasks/crawl` failure alerts → Telegram
- Deploy notifications from `master_gcp_deploy` → Telegram
- Cost savings: modest (Twilio was only customer-facing anyway), but better DX

### OpenClaw as portfolio-wide pattern
If the crab_travel P3 pilot works, the pattern (always-on agent + free LiteLLM + read-only DB + Telegram ops channel) replicates across 22 other apps. Tier 2 candidates to evaluate:
- `scatterbrain` — could self-heal from its own health-check findings
- `galactica` — if it has cron-y reasoning workloads
- Explicit non-fits: `dandy`, `kumori` (already chat apps — different shape)

### Make bot trips joinable (carryover from 3/22)
Real people see a trip on `/live`, click it, actually participate alongside the bots. Natural onboarding funnel. Still a good idea, still unshipped.

---

## Known Issues / Stale Docs to Clean Up

1. **`next_steps.md`** (live doc) still references "SMS ⏸ Deferred" and 30034 blocking — flip to reflect VERIFIED state
2. **`docs/twilio_a2p_campaign.md`** header says `IN_PROGRESS` attempt 7 — update to VERIFIED, campaign ID `C4JM2GC`, cleared 2026-04-09
3. **`docs/twilio_escape_hatch.md`** — Telgorithm migration is off the table; move to archive subfolder or prepend a "RESOLVED — not needed" banner
4. **Attempt 8 demo-form changes** (commit `2f08196`, 4/15) — reviewed whether `/profile/demo` should revert to the pre-attempt-8 state now that the campaign is live. The unchecked-consent-box change was purely for a reviewer that's no longer checking. Worth a 5-min polish pass.
5. **Reviewer-specific `/profile/demo` banner** — if we're not resubmitting, the "IMPORTANT FOR REVIEWERS" callout can come off

---

## API Key / Provider Status (as of 2026-04-16)

| Provider | Status | Notes |
|---|---|---|
| Duffel | Sandbox (`duffel_test_...`) | Swap to production for go-live |
| LiteAPI | Sandbox (`sand_...`) | Swap to production for go-live |
| Viator | Sandbox | Fill contact details → production |
| Travelpayouts | Production | Already live |
| Kayak affiliate | Pending since 2026-03-05 | Nudge if still silent by 2026-04-30 |
| Expedia Rapid API | Rejected | Revisit with traction |
| **Twilio A2P** | **VERIFIED 2026-04-09** | Campaign `C4JM2GC` — live |
| YouTube Data API | 100k units/day | Approved 2026-04-06 |
| LiteLLM gateway | Live at `llm.kumori.ai` | Per-app metadata tagging active |
| OpenClaw VPS | Pre-provision | Awaiting Contabo signup |

---

## Session Stats (2026-04-16)

- Discovered Twilio A2P VERIFIED (unexpectedly — had been stuck since 2026-03-25, attempt 7 was the winner)
- First live production SMS delivered: `+14256002722` → `+14252461275`, SID `SM9c85ee035a598388133aecf40e90e046`
- Vanity number recognized post-hoc: `2722 = CRAB` on the phone keypad (intentional, not coincidence)
- OpenClaw pilot scoped across 3 rounds of research (Reddit + web) + cross-checked against all 23 apps in `~/Desktop/code/`
- [`_private/openclaw_stepbystep.md`](../_private/openclaw_stepbystep.md) authored (~350 lines, 6 phases)
- `_private/` added to `.gitignore`
- Pilot target reframed mid-session: P4 price-watch duplicate → P3 itinerary AI brain (greenfield, higher leverage)
- No commits this session (writing + planning only)

---

**Next session opener:** start with Priority 1 (production SMS validation). Then proceed to Priority 2 (SMS command parser) or Priority 3 (OpenClaw pilot launch) depending on whether Andy's completed Contabo signup.
