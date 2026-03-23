# crab.travel — Session Notes & Next Steps (2026-03-22)

This documents everything discussed, built, discovered, and planned during the March 22 session. Reference for picking up where we left off.

---

## What We Built Today

### Crab Crawlers v1 — Fixed Persona E2E Testing
- **dev/trip_bots.py** — bot orchestrator with 10 fixed personas (Marcus Chen, Sarah Kim, David Okafor, Emily Rodriguez, Jake Thompson, Priya Patel, Tom Nguyen, Lisa Washington, Carlos Mendez, Amy Foster)
- 11 phases: setup → create → join → suggest → preferences → vote → chat → ai_research → lock_search → browse → stop
- 66 assertions, runs in ~27s (quick) or ~60s (full with AI research)
- CLI: `--full`, `--quick`, `--phase <name>`, `--cleanup`, `--deep-cleanup`, `--plan-id <uuid>`
- First run result: 66/66 passed, AI research generated 11 real recommendations

### Crab Crawlers v2 — Haiku-Powered Random Trips
- Haiku picks destinations ANYWHERE on Earth — Zanzibar, Samarkand, Kyrgyzstan, Patagonia, Morocco, Iceland
- Random group sizes (2-15 people) with random names, airports, budgets, interests, dietary needs
- `--crawl` mode for continuous random trips with auto-pruning (keeps last 15)
- Trips generated so far: "Spice Markets & Northern Lights Quest" (Zanzibar + Reykjavik), "Silk Road Chaos: Samarkand to Bishkek Adventure" (Uzbekistan), "Desert Dunes & Mountain Magic" (Morocco), "Desert Meets Arctic: The Impossible Trip" (Merzouga + Tromsø), "Eight Wanderers Hit the Silk Road" (Samarkand)

### Public /live Page — Departures Board
- **crab.travel/live** — public, no login required, "Live" link in main nav
- Departures board showing all bot trips as clickable cards with destination tags, group sizes, vibes, status
- Cards link to `/to/<invite_token>` — visitors click into any trip and see the full plan
- Activity feed showing real-time bot actions (votes, chat messages, joins)
- Stats bar: trips planned, unique destinations, total crabs crawling
- Admin controls (Run/Stop/Cleanup) only visible to logged-in admins
- Tagline: "We don't hide in our shell."

### Bot Trip Voyeur Mode
- Removed "Sign in to vote" gate on bot trips — unauthenticated visitors see everything
- Chat messages load without auth (GET /api/plan/<id>/messages allows unauthenticated for [BOT] plans)
- Calendar data, member list, vote tallies all visible to non-logged-in users
- [BOT] prefix stripped from titles and member names on display
- "Live Demo — Bot Trip" banner with link back to /live board
- Organizer name shown in full (not abbreviated) for bot trips

### Cron Job — Crabs Always Crawling
- `/tasks/crawl` endpoint in app.py — generates one random Haiku trip
- cron.yaml: runs every 10 minutes
- Auto-prunes old bot plans (keeps last 15) on each run
- Cost: ~$4/month (144 Haiku calls/day × $0.001 + negligible Postgres)

### Infrastructure Built
- `POST /api/bot/login` — secret-gated bot authentication (CRAB_BOT_SECRET in GCP Secret Manager)
- `GET /api/live/status` — public endpoint for departures board polling
- `POST /api/admin/bots/run` — launch bot run from admin dashboard
- `POST /api/admin/bots/stop` — graceful stop (bot checks DB between phases)
- `POST /api/admin/bots/cleanup` — delete all bot plan data
- `crab.bot_runs` table — tracks run status, mode, phases passed/failed/warned, summary JSONB
- `crab.bot_events` table — per-action log (phase, bot_name, action, status, detail)
- CRUD helpers: insert_bot_run, update_bot_run, insert_bot_event, get_bot_runs, get_bot_events, get_bot_run_status

### docs/next_steps.md Created
- Internal dev tracker (not the public roadmap)
- Blocked items (Twilio A2P, Kayak affiliate, Expedia rejection)
- Ready-to-build features prioritized
- Crab Crawlers v1 shipped + v2 planned details
- API key status table
- Competitor notes (TripGuru)

---

## What We Discovered Today

### Twilio A2P Status
- **Campaign status: IN_PROGRESS** (submitted 2026-03-20)
- Campaign SID: QE2c6890da8086d771620e9b13fadeba0b
- Phone number: +1 (425) 600-2722
- No errors on submission — clean app, waiting on carrier review
- Test SMS sent: accepted by Twilio but **undelivered** (error 30034 — carrier blocked, unverified campaign)
- Expected approval: ~3/25–3/27 (3-7 business days)
- All SMS infrastructure is fully built and wired up, just waiting on this

### TripGuru Competitor
- Solo dev side project from r/SideProject (user ha1ogen)
- AI itinerary generator: describe trip → stream day-by-day plan → refine via chat
- Single-user only, no group features, no preference matching, no cost splitting, no booking integration
- Built on Clerk auth, free tier, drag-and-drop rearranging, shareable read-only links
- **Threat level: Low** — competing with ChatGPT prompts, not group coordination

### Kayak Affiliate
- Applied: 2026-03-05
- Status: Still waiting for approval
- What it unlocks: "anywhere/anytime" cheapest flights browse feature

### Expedia Rapid API
- Status: REJECTED — "minimum size threshold for new integrations"
- LiteAPI covers hotel inventory in the meantime
- Can revisit when we have traction

---

## What's Next — Priority Order

### PRIORITY 1: Per-Member Flight/Hotel Watches + Price Tracking (PLANNED, READY TO BUILD)

This is the core value proposition after a destination is locked. The plan is fully designed and approved.

**The problem:** Search results are plan-level today ("here are flights to Scottsdale"). What we need is per-member: "Marcus, your SEA→PHX flight dropped from $247 to $189. Book now."

**New tables:**

#### `crab.member_watches`
Each row = one thing the system monitors for one member. Auto-created when plan locks.

| Column | Type | Purpose |
|---|---|---|
| pk_id | BIGSERIAL PK | |
| plan_id | UUID FK | Which trip |
| member_id | INTEGER FK | Which member |
| watch_type | VARCHAR(20) | 'flight', 'hotel', 'activity' |
| origin | VARCHAR(10) | Member's home airport (flights only) |
| destination | VARCHAR(100) | Locked destination |
| checkin | DATE | Trip start |
| checkout | DATE | Trip end |
| budget_max | INTEGER | Member's max budget in cents |
| status | VARCHAR(20) | 'active', 'paused', 'booked' |
| best_price_usd | NUMERIC(10,2) | Lowest price ever seen |
| best_price_at | TIMESTAMPTZ | When lowest was seen |
| last_price_usd | NUMERIC(10,2) | Most recent price |
| last_checked_at | TIMESTAMPTZ | Last poll time |
| alert_threshold_pct | INTEGER | Alert when price drops X% (default 10) |
| deep_link | TEXT | Best deal booking link |
| data | JSONB | Full best result details |

#### `crab.watch_history`
Price observations over time per watch — powers the price trend chart.

| Column | Type | Purpose |
|---|---|---|
| pk_id | BIGSERIAL PK | |
| watch_id | BIGINT FK | Which watch |
| price_usd | NUMERIC(10,2) | Observed price |
| source | VARCHAR(50) | Which adapter |
| deep_link | TEXT | Booking link at this price |
| data | JSONB | Full result |
| observed_at | TIMESTAMPTZ | When observed |

**How it works:**

1. **Auto-create on lock:** When organizer calls POST /api/plan/<id>/lock, for each member:
   - Flight watch: member.home_airport → locked destination, locked dates
   - Hotel watch: locked destination, locked dates, member.budget_max
   - Budget pulled from plan_preferences

2. **Cron: /tasks/check-watches (every 6 hours):**
   - Query all member_watches WHERE status = 'active'
   - Group by (destination, dates) to minimize API calls
   - Call adapters once per unique route, fan results to each member's watch
   - Update last_price, best_price, record watch_history
   - If price dropped > threshold: queue email/SMS notification

3. **Notifications:** Email now, SMS once A2P clears. "Marcus, your SEA→PHX flight dropped to $189 (was $247). Book now → [deep link]"

4. **Plan page UI:** Per-member section showing their watches with price, trend sparkline, "Book Now" deep link, "Mark as Booked" button

5. **Bot integration:** New phases in trip_bots.py — watch_create + watch_check

**Files to create/modify:**
- `utilities/postgres_utils.py` — tables + CRUD helpers
- `utilities/watch_engine.py` — NEW: auto-create logic + price check loop
- `app.py` — watch routes + cron endpoint
- `cron.yaml` — add /tasks/check-watches every 6 hours
- `templates/invite.html` — per-member watch section
- `dev/trip_bots.py` — watch phases

**Reuses existing infrastructure:**
- Adapters (Duffel flights, LiteAPI hotels, Viator activities)
- save_price_history() pattern
- notify_plan_members_email() / send_sms()
- Background thread pattern from search_engine.py

---

### PRIORITY 2: Per-Person Cost Breakdown (Spreadsheet Killer)

The #1 thing that replaces the group text + Excel sheet for real trips.

- Expense line items: description, amount, who paid, split method (even/per-person/custom)
- Running total per person, net balances between members
- `calculate_balances()` and greedy settlement algo — need to build
- Venmo/Zelle deep links pre-filled with amount + note
- **Tables:** `crab.expenses` already exists in schema, no API endpoints yet
- **Why:** This is what the Phoenix founding group needs. Money is the #1 source of friction in group travel.

---

### PRIORITY 3: Day-by-Day Itinerary Builder

Takes the trip from "we're going to Nashville" to "here's what we're doing Thursday."

- Drag destination card pins into day/time slots (morning/afternoon/evening)
- Shared view — all members see the same schedule
- AI-suggested ordering by geography + operating hours
- Free-form notes per day
- Mobile-optimized for on-the-ground use during the trip
- **Tables:** `crab.itinerary_items` already exists in schema

---

### PRIORITY 4: Price Drop Alerts (extends member_watches)

Once member_watches is built, this is mostly the notification layer:

- Recurring search runs for locked-in trips (every 6 hours via cron)
- Compare new prices against watch best_price
- Alert threshold: configurable per watch (default 10% drop)
- Email alerts now, SMS alerts once A2P clears
- Price trend visualization on plan page (sparklines from watch_history)

---

### PRIORITY 5: Email Digest for Lurkers

Weekly summary for people who never open the site.

- Vote standings, price changes, chat highlights, new members
- Smart send — only fires when there's actual news
- One-click actions from email (vote, view prices, see itinerary)
- Configurable frequency per user (daily, weekly, milestones only)
- **Infra:** Email sending via Gmail already works. Need digest template + cron job.

---

### PRIORITY 6: SMS Voting & Inbound Commands (blocked on A2P)

Ready to build the moment Twilio A2P campaign clears (~3/25-3/27).

- Outbound vote prompts: "Reply 1 for Nashville, 2 for Phoenix"
- Inbound vote parsing (webhook at /api/sms/inbound already exists)
- RSVP confirmation via text
- Chat-to-SMS bridge (messages forwarded as texts, replies flow back into chat)
- Flight info collection via text
- **Infra:** sms_utils.py fully built, inbound webhook wired up. Just need command parser.

---

## Ideas From Today (Not Yet Prioritized)

### Make Bot Trips Joinable
Real people see a trip on /live, click it, and actually participate alongside the bots. The bots become the starter culture, real humans join in. Natural onboarding funnel — zero friction, they're already looking at the trip.

### Cross-Pollinate with kindness_social
Share bot user infrastructure across projects. Have bots from kindness_social interact with crab.travel and vice versa. Shared identity layer for test users.

### Stress Testing at Scale
- 20-50 concurrent trips for "live stock ticker" effect on /live
- Trip config randomizer with varied group sizes (2, 10, 100, 1000)
- Bottleneck analysis: Cloud SQL pool (5-10 concurrent OK), AI cost ($0.02/trip), App Engine auto-scale, sandbox rate limits
- Sweet spot: 20 trips/hour, AI on 20% of trips, ~$8/month
- "Departures board" redesign showing grid of all active trips like airport departures

### Let Visitors Vote/Interact on Bot Trips
Like kindness_social does — anonymous interaction without account creation. Let visitors vote on destinations, post chat messages, react to things. Auto-assigned as "Viewer" by IP/session. Full participation without the login wall.

### Room Assignments & Flight Tracking
- Room assignment interface (drag members into rooms)
- Flight info entry per member (confirmation code or airline + flight number)
- "Who's arriving when" timeline for pickup coordination
- PDF export: master trip summary (rooms, flights, costs, itinerary, contacts)

### Shareable Trip Cards (OG Images)
- Dynamic OG image per trip (destination photo, dates, member count)
- Optimized for iMessage, WhatsApp, Slack previews
- Every shared link becomes a mini-ad for the platform

---

## Known Issues / Polish Items

1. **[BOT] prefix in chat messages** — titles and member names are stripped, but the actual message content still contains "[BOT]". Need to strip in the chat rendering or stop prefixing message content.

2. **Vote failures in crawl trips** — some bots get vote failures (likely duplicate rank conflicts when random personas try to assign the same rank to multiple destinations). Non-blocking — the votes still succeed on retry.

3. **Cron crawl failures** — the `/tasks/crawl` endpoint runs `build_random_trip` via subprocess on App Engine, but App Engine instances may not have the full Python path configured. Need to verify cron is actually firing and trips are being created. May need to run as an in-process background thread instead of subprocess.

4. **Sandbox API keys** — search adapters (Duffel, LiteAPI, Viator) return 0 results on sandbox keys. Full search testing requires production keys. Go-live actions documented in docs/next_steps.md API Keys table.

---

## API Key Status

| Provider | Status | Key Type | Go-Live Action |
|---|---|---|---|
| Duffel | Active | Test (`duffel_test_...`) | Swap to production key |
| LiteAPI | Active | Sandbox (`sand_...`) | Swap to production key |
| Viator | Active | Sandbox | Fill in contact details → get production key |
| Travelpayouts | Active | Production | Already live |
| Kayak | Pending | — | Waiting on affiliate approval (applied 3/5) |
| Twilio | Active | Production | A2P campaign approval pending (submitted 3/20) |

---

## Cost Analysis

| Item | Monthly Cost | Notes |
|---|---|---|
| Crab Crawlers cron (Haiku) | ~$4 | 144 calls/day × $0.001 |
| Cloud SQL (Postgres) | Existing | Shared instance, bot data is negligible |
| App Engine | Existing | Bot requests don't trigger extra instances |
| Duffel sandbox | Free | Production key: pay-per-booking (bots don't book) |
| LiteAPI sandbox | Free | Production key: commission-based |
| Viator sandbox | Free | Production key: 8% commission |
| Travelpayouts | Free | Commission-based, already live |
| Watch engine cron (future) | ~$2 | 4 checks/day × adapter calls |
| **Total incremental** | **~$6/month** | |

---

## Session Stats

- Commits: 8
- Deploys: 7
- New files: 4 (dev/trip_bots.py, templates/admin_bots.html, docs/next_steps.md, docs/20260322_next_steps.md)
- Modified files: 6 (app.py, postgres_utils.py, templates/admin.html, templates/base.html, templates/invite.html, cron.yaml)
- Bot trips created: ~10
- Destinations generated by Haiku: Zanzibar, Reykjavik, Marrakech, Samarkand, Bishkek, Issyk-Kul Lake, Merzouga, Chefchaouen, Essaouira, Tromsø, Udaipur, Chiang Mai, Luang Prabang, Yazd, Bhaktapur, Banff, Jaipur, Lviv, and more
- Total crabs crawling: 52+
