# Next Steps — Internal Dev Tracker

Not the public roadmap. This is what we're actually working on right now, what's blocked, and what to pick up next.

Last updated: 2026-03-24

---

## Blocked — Waiting on External

### Twilio A2P Campaign Approval
- **Status:** IN_PROGRESS (resubmitted 2026-03-24, ~2nd attempt)
- **Campaign SID:** QE2c6890da8086d771620e9b13fadeba0b
- **Brand SID:** BN05299cc8c46ebf46b61fb87fb11d6ff9 (APPROVED)
- **Phone number:** +1 (425) 600-2722 ("crab.travel 600-CRAB")
- **What's blocked:** All outbound SMS — chat notifications, vote prompts, price alerts
- **Error on send attempt:** 30034 (carrier blocked, unverified campaign)
- **Expected approval:** ~3/27–3/31 (3-7 business days from resubmission)
- **Test command:** See quick commands below or hit `/api/admin/test-sms`

**First submission (2026-03-20) — FAILED:**
- Rejection reason: error 30908 — "a compliant privacy policy can not be verified" (field: MESSAGE_FLOW)
- The privacy policy at `/privacy` existed but didn't have enough SMS-specific detail
- The `MessageFlow` field in the submission only linked to `/terms#sms`, not `/privacy#sms`

**What we fixed for resubmission (2026-03-24):**
1. Expanded `/privacy#sms` (section 4) with sub-sections: what data we collect (phone number, delivery logs), how we use it (transactional only), who we share it with (Twilio, named explicitly), how to opt out (STOP or profile toggle), and that consent is not required to use the platform
2. Added cross-links: privacy page links to `/terms#sms`, terms page links to `/privacy#sms`
3. Added `id="sms"` anchor to privacy page so `/privacy#sms` deep link works
4. Updated `MessageFlow` in the API submission to reference BOTH URLs: `/terms#sms` AND `/privacy#sms`
5. Updated `HelpMessage` to include contact URL

**If it fails again:** The reviewer comment will be in the `errors` array. Check with the quick command below. Common next steps would be adding a screenshot of the opt-in checkbox to the terms page, or adding a physical mailing address to the contact page.

### Kayak Affiliate
- **Applied:** 2026-03-05
- **Status:** Waiting for approval
- **What it unlocks:** "Anywhere/anytime" cheapest flights — the browse-all-destinations feature
- **Next step:** Once approved, grab API key → build KayakAdapter

### Expedia Rapid API
- **Status:** REJECTED (minimum size threshold)
- **Alternatives:** Expedia Affiliate Program (deep-link commissions), or revisit when we have traction
- **LiteAPI covers hotel inventory in the meantime**

---

## Dev Infrastructure

### Crab Crawlers v1 — SHIPPED 2026-03-22

Single bot trip with 10 personas, 11 phases, full E2E against prod. Public dashboard at `/live`.

**What's built:**
- `dev/trip_bots.py` — orchestrator, 10 personas, CLI (--full/--quick/--phase/--cleanup)
- `/live` — public page anyone can watch (admin controls hidden for non-admins)
- `/admin/bots` — same page with run/stop/cleanup buttons
- `/api/bot/login` — secret-gated bot auth (CRAB_BOT_SECRET in GCP)
- `/api/live/status` — public status endpoint (polls every 3s)
- `crab.bot_runs` + `crab.bot_events` tables + CRUD helpers
- 66 assertions across 11 phases, runs in ~30s (quick) or ~60s (full with AI)

**First run results:** 66/66 passed, 26.8s quick, ~60s full. AI research generated 11 real recommendations.

### Crab Crawlers v2 — NEXT: Multi-Trip Randomizer + Live Departures Board

Scale from 1 bot trip to 20-50 concurrent trips running on staggered schedules, visible as a live "departures board" on `/live`. Different group sizes, destinations, date ranges. Always something happening.

**What to build:**
- Trip config randomizer: random group size (2-20), random destinations from a pool, random date ranges (10 days to 20 months), random persona combos
- Staggered scheduler: start a new trip every N minutes, max M concurrent
- `/live` redesign: grid/feed of ALL active trips (like airport departures board), click into any to watch its bots
- Connection pool tuning for higher concurrency
- Skip AI research phase on most trips (save cost), run it on 1-in-5

**Bottleneck analysis:**
- Cloud SQL connections: pool size caps concurrent DB activity. Need to tune for 20-50 trips.
- AI cost: ~$0.02/trip (Haiku). 50 trips/hour = ~$25/month. Skip AI on most trips to keep it ~$5/month.
- App Engine: auto-scales but costs ~$0.05/hr per instance. 20 trips/hour won't trigger extra instances.
- Search adapter sandbox rate limits: 100-500 req/day. Skip search on most trips, run on 1-in-10.
- Sweet spot: 20 trips/hour, AI on 20%, search on 10% = ~$8/month total.

**Public "departures board" vision:** Visitors see 10-20 trips in various stages — one just started voting, another has 8 people chatting, another is searching flights. Click any trip to see the live detail. Like watching a stock ticker for travel planning. "We don't hide in our shell."

**The idea:** A set of AI-driven bot personas that create trips, join via invite links, fill out preferences, vote, chat, trigger searches, add expenses, settle up — the whole thing, soup to nuts. Not unit tests. Not "is the page up?" checks. Full behavioral simulation of 10-15 fake people planning a fake trip over days/weeks.

**Bot personas (each with distinct preferences):**
- "Organizer Olivia" — creates trips, manages invites, sets destinations, triggers votes
- "Budget Ben" — always picks the cheapest option, tight budget range, price-sensitive
- "Luxury Lisa" — premium everything, high budget, specific dietary needs
- "Late Larry" — joins days after the invite, votes last, minimal engagement (the lurker)
- "Chatty Charlie" — posts in trip chat constantly, reacts to everything
- "Indecisive Irene" — changes preferences mid-planning, updates votes
- "SMS Steve" — interacts only via text (once A2P clears), never opens the browser
- "Family Fran" — mobility considerations, family-friendly activities only
- 5-7 more generic members with varied preferences

**What the bots exercise:**
1. **Trip creation** — Olivia creates a trip with 3-4 candidate destinations
2. **Invite flow** — bots hit the invite link, some sign in with Google, some anonymous
3. **Preference intake** — each bot fills out budget, interests, dietary, accommodation style
4. **Voting** — rank-order destination votes with different rankings per persona
5. **Availability calendar** — each bot marks different dates as ideal/if-needed/can't-go
6. **Chat** — Charlie posts messages, others reply, threaded conversations
7. **AI research trigger** — once votes settle, trigger destination research
8. **Search adapters** — verify Duffel/LiteAPI/Viator/Travelpayouts return results
9. **Expense tracking** — add shared expenses, per-person items, verify balances
10. **Settlement** — verify calculate_balances() produces correct who-owes-whom
11. **Notifications** — verify email sends (and SMS once A2P clears)
12. **Edge cases** — Larry joins late (do vote tallies update?), Irene changes prefs (does AI re-research?), bad data (empty fields, special characters, duplicate joins)

**How it runs:**
- `dev/trip_bots.py` — main orchestrator
- `python dev/trip_bots.py --full` — run complete lifecycle (create → settle), takes ~10 min
- `python dev/trip_bots.py --quick` — just invite + join + vote, takes ~2 min
- `python dev/trip_bots.py --phase vote` — run only the voting phase on an existing bot trip
- Bots hit the real prod API (https://crab.travel) with test accounts, tagged `[BOT]` in display name
- Bot trips tagged with `is_test = true` or a naming convention (`[BOT] Phoenix Test Trip`) so they don't pollute real data
- Assertions at every step — if a bot action fails, it logs exactly what broke and where
- Can run on a cron (daily or on every deploy) as a full regression suite

**Why this is high priority:**
- The existing smoke test (`dev/smoke_test.py`) only checks "is the page up?" and "do redirects work?" — it doesn't exercise any real user flows
- With no real users for months, this is the only way to know if the app actually works end-to-end
- Every new feature (expenses, itinerary, SMS voting) gets a bot scenario added to the suite
- When we do invite the 20 founding members, the first impression has to be flawless

---

## Ready to Build (no blockers)

### 1. Per-Person Cost Breakdown (spreadsheet killer)
- Expense line items: description, amount, who paid, split method (even/per-person/custom)
- Running total per person, net balances between members
- `calculate_balances()` and greedy settlement algo already exist in code
- Venmo/Zelle deep links pre-filled with amount + note
- **Tables:** `crab.expenses` already exists
- **Why next:** This is the #1 thing that replaces the group spreadsheet. High value, straightforward to build.

### 2. Day-by-Day Itinerary Builder
- Drag destination card pins into day/time slots (morning/afternoon/evening)
- Shared view — all members see the same schedule
- AI-suggested ordering by geography + operating hours
- Free-form notes per day
- Mobile-optimized for on-the-ground use during the trip
- **Tables:** `crab.itinerary_items` already exists
- **Why next:** Makes the trip feel real. Goes from "we're going to Nashville" to "here's what we're doing Thursday."

### 3. Price Drop Alerts
- Recurring search runs for locked-in trips (daily via cron)
- Compare new prices against `crab.price_history`
- Alert threshold: 10%+ drop or user-configured amount
- Email alerts now, SMS alerts once A2P clears
- Price trend visualization on destination cards
- **Infra:** price_history table + search adapters already running. Just need the comparison + notification loop.

### 4. Email Digest for Lurkers
- Weekly summary: vote standings, price changes, chat highlights, new members
- Smart send — only fires when there's actual news
- One-click actions from email (vote, view prices, see itinerary)
- Configurable frequency per user
- **Infra:** Email sending via Gmail already works. Need digest template + cron job.

---

## Ready to Build Once A2P Clears

### 5. SMS Voting & Inbound Commands
- Outbound vote prompts: "Reply 1 for Nashville, 2 for Phoenix"
- Inbound vote parsing (webhook at `/api/sms/inbound` already exists)
- RSVP confirmation via text
- Chat-to-SMS bridge (messages forwarded as texts, replies flow back into chat)
- Flight info collection via text
- **Infra:** `sms_utils.py` fully built, inbound webhook wired up. Just need the command parser layer on top.

---

## LLM Rate Limit Recalibration — SHIPPED 2026-03-24

Crawlers were burning through free tier limits and generating massive 429 storms (~90% failure on most backends). Root cause: 1-minute cron with 4-7 concurrent crawls all round-robining through the same backends.

**What we changed:**

### Cron frequency: 1 min → 5 min
- Drops from ~1,440 to ~288 LLM calls/day
- Still 1-2 live trips overlapping at any time on `/live`

### Backend order reshuffled (reliability tiers)
- **Tier 1** (bulk): Cerebras (1K cap, fastest), then 4 Groq models (500 each)
- **Tier 2** (moderate): Gemini (200), LLM7 (300)
- **Tier 3** (deep fallbacks): OpenRouter (40 each), NVIDIA (50!), Grok (100), DeepSeek (100), Mistral (100)

### RPM spacing widened for concurrency
- Groq all 4 models: 2s → 4s (share one API key, RPM pool is shared)
- NVIDIA: 1.5s → 5s (lifetime credits)
- Gemini: 6s → 10s, OpenRouter: 3s → 10s
- Added grok: 10s, deepseek: 10s (were missing entirely, defaulted to 1s!)

### Daily caps right-sized
| Backend | Old Cap | New Cap | Why |
|---|---|---|---|
| cerebras | 9,500 | 1,000 | Only need ~288/day total |
| groq (×4) | 900 each | 500 each | Leave room for user calls |
| nvidia | 500 | **50** | 1K LIFETIME credits, not daily! ~20 days runway |
| together | 900 | **0** | Dead — 401 Unauthorized, credits exhausted |
| grok/deepseek | 500 | 100 | Slow (60s timeouts), just fallbacks |
| mistral | 2,800 | 100 | 2 RPM is practically useless |
| gpt4o-mini | 200 | 50 | Paid — only when all free fail |

### 24h stats before the fix (March 24)
- groq: 10% success (1,082 429s out of 1,209 calls)
- mistral: 0% success (587/588 were 429s)
- openrouter: 0% success across all 3 models
- together: 0% (all 401s — dead)
- deepseek: 0% (all timeouts)
- NVIDIA: 51% success but burning lifetime credits at 500/day
- Paid fallbacks triggered 66 times (52 gpt4o-mini + 14 haiku)

### What still needs attention
1. **Vote failures** — ~4,000 "Vote FAILED for X" errors per day. Now logging response text in `detail` column for diagnosis. Suspect it's race conditions with large groups (30-100 bots) or plans getting pruned mid-run. Need to investigate.
2. **NVIDIA lifetime credits** — At 50/day cap we have ~20 days of runway. Once exhausted, remove from rotation or buy more credits.
3. **Grok/DeepSeek Cloud Run worker** — Both timeout frequently (60s). The PoW bypass worker at `kindness-worker-243380010344.us-central1.run.app` may need a health check or scaling adjustment.
4. **OpenRouter** — 0% success rate even at 50/day cap. May need a $10 credit purchase to unlock 1K/day, or just remove from rotation to save the timeout overhead.
5. **Mistral** — 2 RPM makes it nearly useless in a round-robin. Consider removing to avoid wasting time on a backend that can handle ~1 call/minute.
6. **Monitor post-fix** — After a full day on the new settings, re-check the 24h stats to confirm 429s dropped. Target: <10% failure rate on Tier 1 backends.

### Quick check command
```bash
# LLM stats for last 24h (run from project root)
python3 -c "
import psycopg2, psycopg2.extras, sys; sys.path.insert(0,'.')
from utilities.postgres_utils import get_db_connection
conn = get_db_connection(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute('''SELECT backend, COUNT(*) as total,
    COUNT(*) FILTER (WHERE success=true) as ok,
    COUNT(*) FILTER (WHERE success=false AND error_message LIKE \'%%429%%\') as rate_limited
    FROM public.crab_llm_telemetry WHERE created_at > NOW() - interval \'24 hours\'
    GROUP BY backend ORDER BY total DESC''')
for s in cur.fetchall():
    pct = round(s['ok']/s['total']*100) if s['total'] else 0
    print(f'{s[\"backend\"]:18} | total:{s[\"total\"]:4} | ok:{s[\"ok\"]:4} ({pct}%) | 429s:{s[\"rate_limited\"]:4}')
conn.close()
"
```

---

## API Keys — Status

| Provider | Status | Key Type | Go-Live Action |
|---|---|---|---|
| Duffel | Active | Test (`duffel_test_...`) | Swap to production key |
| LiteAPI | Active | Sandbox (`sand_...`) | Swap to production key |
| Viator | Active | Sandbox | Fill in contact details → get production key |
| Travelpayouts | Active | Production | Already live |
| Kayak | Pending | — | Waiting on affiliate approval |
| Twilio | Active | Production | A2P campaign approval pending |

---

## Quick Commands

```bash
# Check Twilio A2P campaign status (run from project root)
export $(grep '^TWILIO' .env | xargs) && curl -s -u "$TWILIO_ACCOUNT_SID:$TWILIO_AUTH_TOKEN" \
  "https://messaging.twilio.com/v1/Services/$TWILIO_MESSAGING_SERVICE_SID/Compliance/Usa2p" \
  | python3 -c "import sys,json; [print(f'Status: {c[\"campaign_status\"]}\nErrors: {c[\"errors\"]}\nRate limits: {c[\"rate_limits\"]}') for c in json.load(sys.stdin).get('compliance',[])]"

# Check phone number details
export $(grep '^TWILIO' .env | xargs) && curl -s -u "$TWILIO_ACCOUNT_SID:$TWILIO_AUTH_TOKEN" \
  "https://api.twilio.com/2010-04-01/Accounts/$TWILIO_ACCOUNT_SID/IncomingPhoneNumbers.json" \
  | python3 -c "import sys,json; [print(f'{n[\"friendly_name\"]}: {n[\"phone_number\"]} — SMS:{n[\"capabilities\"][\"sms\"]} Voice:{n[\"capabilities\"][\"voice\"]}') for n in json.load(sys.stdin)['incoming_phone_numbers']]"

# If campaign fails again — delete and resubmit (see git history for the full curl POST)
# git log --oneline --all | head  # find the resubmission commit for the exact curl command

# Send test SMS (will fail with 30034 until A2P approved)
curl -X POST https://crab.travel/api/admin/test-sms \
  -H "Content-Type: application/json" \
  -d '{"phone": "+14252461275"}'

# Deploy
deploy "commit message"
```

---

## Competitor Notes

### TripGuru (tripguru.app) — spotted 2026-03-22
- Solo dev side project, posted on r/SideProject
- AI itinerary generator: describe a trip → get a day-by-day plan → refine via chat
- Single-user only. No group features, no preference matching, no cost splitting, no booking
- Drag-and-drop rearranging, shareable read-only links
- Built on Clerk auth, free tier
- **Threat level: Low.** Competing with ChatGPT prompts, not with group coordination.
