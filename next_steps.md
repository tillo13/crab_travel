# crab.travel — Next Steps
*Updated: 2026-04-01*

## Done This Session (April 1)

### Twilio A2P Campaign — Attempt 6
1. **Checked campaign status** — attempt 5 (Mar 27) FAILED with error 30909 (CTA verification) for the 5th time.
2. **Researched what actually works** — web-searched Twilio docs, community forums, and Reddit. Key finding: when opt-in is behind a login wall, reviewers need publicly accessible screenshots/mockups of the consent UI, not just text descriptions.
3. **Overhauled `/sms` page** — added 4-step visual walkthrough with HTML/CSS mockups of the Profile page consent flow (initial state → phone entered → consent checked → validation blocking). Added interactive demo with live validation. Restructured for reviewer clarity.
4. **Verified all reviewer-accessible pages** — `/sms` (200), `/terms#sms` (200, anchor exists), `/privacy#sms` (200, anchor exists), `/contact` (200). All public, no login required. All internal links verified working.
5. **Deployed to GCP** — `version-91mxnseng0`, live at crab.travel.
6. **Deleted failed campaign** — `DELETE` on `QE2c6890da8086d771620e9b13fadeba0b`, returned 204.
7. **Resubmitted campaign** — new `QE2c6890da8086d771620e9b13fadeba0b`, status IN_PROGRESS. Updated Message Flow explicitly references visual walkthrough + interactive demo on `/sms`. Added frequency cap ("not exceeding 10 messages per day").
8. **Updated docs** — `docs/twilio_a2p_campaign.md` updated with failure history, new submission details, costs (~$68 total in A2P fees).

---

## Done Previous Session (March 27)

### Itinerary Google Search Links
1. **Clickable itinerary items** — every item on the trip summary page is now a clickable Google search link. Clicking opens a new tab with a Google search for the venue/location.
2. **Smart search query** — uses `item.location` (e.g., "Bar Kuld") instead of the full title ("Evening Drinks at Bar Kuld") so Google actually finds the place. Falls back to title if no location set. Destination city appended for specificity.
3. **Location links too** — the location metadata text (e.g., "· Pirita Beach") is also a clickable Google search link.

### Flight Times & Date Variance
4. **Flight departure/arrival times** — every flight watch now stores `departure_time`, `arrival_time`, `return_departure_time`, and `return_arrival_time` in the JSONB `data` column. Times are generated from 20 realistic departure slots (6 AM – 8 PM) with randomized flight durations (1.5–5.5 hrs).
5. **12-hour format** — times display as "3:30 PM → 6:41 PM" (not 24h). Jinja macro `fmt_time()` on summary page, JS `fmt12()` on invite page.
6. **Return flight line** — both summary and invite pages show a second line for return flights: "Return May 23 11:15 AM → 2:27 PM".
7. **Times on invite/watch cards** — the per-member watch cards on the invite page (`/to/<token>`) now show dates + times for flights, and date ranges for hotels. Previously only showed destination.
8. **Increased date variance** — bumped from ~30% to ~45% of members having varied arrival/departure dates. Some arrive a day early, some a day late, some leave early, some stay extra. Hotels have similar variance with 10% chance of half-trip stays. Makes group trips look realistic — not everyone flies on the same day.
9. **Backfilled existing trips** — `_humanize_watches()` upgraded to store times in JSONB. Stale-fix query in `/tasks/seed-booked-trips` detects flights missing `departure_time` and backfills them.

### Demo/Bot Trip UX
10. **Join form collapsed on demo trips** — the "Update Your Info" / "Join" section starts collapsed on all bot/demo trips (previously expanded by default, taking up screen space for demo viewers).
11. **Demo mode banner** — when expanded on a bot trip, shows a teal info box: "This is a demo trip — feel free to join and click around to see what the experience looks like! Your profile and preferences will be saved to your account, but this isn't a real trip."
12. **Leave Trip feature** — new `POST /api/plan/<id>/leave` endpoint removes a user from any trip (cascades to watches, availability, blackouts via `ON DELETE CASCADE`). Organizers cannot leave their own trips.
13. **Leave Trip UI** — on demo trips, the demo banner includes "Want to remove this from your trips? Leave this demo trip" link. On real trips, a subtle "Leave this trip" link appears in the form section. Both confirm before acting and redirect to `/plans`.

### Seed Task Performance
14. **Batch limits** — stale-fix capped at 10 plans per cron run, itinerary generation capped at 2 per run (LLM calls are slow). Prevents App Engine 60s timeout that was crashing the seed task.
15. **Removed redundant re-humanize** — previously re-humanized ALL booked bot plans every run (expensive and unnecessary). Now only targets plans missing flight times or with $0 watches.

---

## Done Previous Session (March 26)

### /live Page Overhaul
1. **New lifecycle tabs** — replaced `All | Active | Booked | Completed` with `All | Active | Voting | Charting | Booked`
2. **Randomized trip destiny** — each bot trip randomly ends at Voting (45%), Charting (30%), or Booked (25%) — mirrors real human behavior
3. **Nurture system** — each cron run, CrabAI revisits 5 past trips, generates LLM chat messages as members (nudging voters, asking about dates, etc.), 15% chance organizer advances stage
4. **Pool exhaustion fix** — consolidated /live API from 5 DB connections per request to 1. Polling slowed from 3s to 10s.
5. **Cron slowed** — crawl cron from every 30min to every 2hrs (trips evolve like real humans, not robots)
6. **Booked trips seeded** — realistic flight ($180-650) and hotel ($120-400) prices on all watches, confirmation numbers
7. **Prune protection** — booked plans never get deleted by the cleanup cron

### Site Copy Refresh
8. **CrabAI branding** — replaced ALL "the AI", "our AI", "AI-powered" references with "CrabAI" across every template (about, roadmap, index, live, privacy)
9. **About page fixed** — "per-person cost tracking coming next" → now correctly says shipped
10. **Roadmap badges fixed** — expense tracking: "in progress" → "shipped", who-owes-who marked done
11. **Homepage** — new "No more who owes what?" expense tracking section
12. **/live intro** — explains Voting/Charting/Booked lifecycle with color-coded labels

---

## Done Previous Session (March 25)

### Infrastructure Fixes
1. **Pool auto-recovery** — if `getconn()` fails (corrupted pool), nuke and recreate automatically
2. **`/_ah/warmup` handler** — App Engine pre-tests DB pool on new instances before routing traffic
3. **Stuck bot run cleanup** — `/tasks/crawl` auto-fails runs stuck in "running" for >1 hour
4. **LLM pipeline fix** — `llm_usage_caps.py` had globals clobbering `_db_write_fn`/`_db_read_fn` after `init()`. DB sync was permanently broken. Fixed.
5. **Xotelo removed** — requires RapidAPI auth (not free as assumed). Hotel watches now Travelpayouts → LiteAPI.
6. **Wattson deployed** (user did this)
7. **Old App Engine versions clean** — deploy tool auto-prunes to 3 versions

### Demo System (Judy Tunaboat)
8. **Demo viewer: Judy Tunaboat** — fake user (user_id=81869) auto-joins all trips with availability + blackout dates
9. **`/demo` route** — switches any user (even logged-in) to Judy, stashes real session, auto-restores on nav away
10. **Auto-login on bot trips** — anonymous visitors hitting any `/to/<token>` bot trip become Judy automatically
11. **"Viewing as Judy Tunaboat" banner** — coral pill with eye icon
12. **Stage switcher** — pill tabs: Voting / Planning / Booked → `/demo/voting`, `/demo/planning`, `/demo/booked`
13. **Booked trip UI** — correct status badge ("Booked — trip confirmed!"), cost stats in header, hide join form + suggest input
14. **Scottsdale AZ added** to demo trip destinations with full card content
15. **All 4 destination cards populated** — Scottsdale (91% match), Sagano, Salvador, Lapland with stays/activities/food/highlights
16. **Votes seeded** — Scottsdale wins with 6 #1 votes, proper distribution across all destinations
17. **LLM-generated chat messages** — `/tasks/seed-demo-chat` uses the LLM router to generate fun group chat threads for all 3 demo trips. Confirmed working via Cerebras, Groq, Groq-Kimi, Groq-GPToss, LLM7.

### CTAs
18. **Landing page** — "See a demo trip" button (teal, links to `/demo`) alongside "Start planning"
19. **Landing page bottom** — "See it live" button alongside "Start planning"
20. **/live page** — "Start your own trip →" coral button
21. **Trip summary footer** — "Start your own trip" CTA
22. **Invite page footer** — "Start your own trip" CTA (demo viewers only)

---

## BLOCKER: SMS / Twilio A2P Campaign

**Status: IN_PROGRESS. Attempt 6 submitted 2026-04-01. Waiting on TCR review.**

**Attempt 5 (Mar 27) FAILED** — error 30909 (CTA verification) again. The `/sms` page only had text descriptions of the opt-in flow. Reviewer still couldn't verify the actual consent UI.

**What changed for attempt 6 (Apr 1):**
- Researched Twilio docs + community: when CTA is behind a login wall, you must provide **publicly accessible visual proof** of the opt-in UI (screenshots/mockups), not just text descriptions.
- Overhauled `/sms` page with:
  - **4-step visual walkthrough** — HTML/CSS mockups of the actual Profile page showing: initial state (SMS off), phone entered (consent appears), consent checked + SMS selected (complete), and validation blocking SMS without consent.
  - **Interactive demo** — functional replica of the form with real validation logic. Reviewer can type a phone number and see the consent flow themselves.
  - Both opt-in methods clearly separated (web + keyword START).
  - All CTIA/TCR compliance disclosures (brand, frequency cap, rates, terms, privacy, opt-out).
- Updated Message Flow text to explicitly say: "Because the web opt-in form requires authentication, a complete visual walkthrough with step-by-step UI mockups... is publicly available at https://crab.travel/sms"
- Added frequency cap: "not exceeding 10 messages per day" (previously just "varies").
- Deleted failed campaign, resubmitted fresh.

**Campaign SID:** `QE2c6890da8086d771620e9b13fadeba0b`
**Expected approval:** Several days from 2026-04-01. No carrier post-approval needed for LOW_VOLUME use case.

**Full documentation:** See `docs/twilio_a2p_campaign.md` for complete history, all SIDs, what was submitted, why all previous attempts failed, and how to check status.

This is the #1 thing to tell Adam and team about. When this clears, SMS goes live instantly with zero code changes. Price drop alerts to your phone, chat messages as texts, vote reminders via SMS. Everything is built and waiting.

### How to Check If It's Approved (do this every session)

Run this from the crab_travel project root:

```bash
python3 -c "
import sys; sys.path.insert(0, '.')
from utilities.google_auth_utils import get_secret
import requests

account_sid = get_secret('CRAB_TWILIO_ACCOUNT_SID')
auth_token = get_secret('CRAB_TWILIO_AUTH_TOKEN')

# Step 1: Send a test SMS
resp = requests.post(
    f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json',
    auth=(account_sid, auth_token),
    data={
        'MessagingServiceSid': 'MG4c8502a7ba7c8d229fd89e2d7b8c47cc',
        'To': '+14252461275',
        'Body': 'crab.travel SMS test. If you got this, A2P is APPROVED!'
    }, timeout=15
)
msg = resp.json()
msg_sid = msg.get('sid', '')
print(f'Sent: {msg_sid} (status: {msg.get(\"status\")})')

# Step 2: Wait and check delivery
import time; time.sleep(5)
resp2 = requests.get(
    f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages/{msg_sid}.json',
    auth=(account_sid, auth_token), timeout=15
)
d = resp2.json()
status = d.get('status')
error = d.get('error_code')

if status == 'delivered':
    print('SMS DELIVERED! A2P IS APPROVED! UPDATE NEXT_STEPS AND TELL ADAM!')
elif error == 30034:
    print(f'STILL BLOCKED. Error 30034. Campaign not approved yet. Keep waiting.')
else:
    print(f'Status: {status}, Error: {error}, Message: {d.get(\"error_message\",\"none\")}')
"
```

**What SUCCESS looks like:**
- Status changes from `undelivered` to `delivered`
- Error code is `None` (not 30034)
- You actually receive the text on your phone at 425-246-1275
- When this happens: update this file, tell Adam, celebrate

**What FAILURE still looks like (current state):**
- API accepts the message (status: `accepted`, 201 response). This is misleading. It does NOT mean it worked.
- After 3-5 seconds, status changes to `undelivered` with error_code `30034`
- No text arrives on your phone
- This means carriers are still filtering. Campaign not approved yet.

### What's Built and Ready
- **Phone number**: `+1 (425) 600-CRAB` (425-600-2722). Active, SMS-capable.
- **Business verification**: TILLO CONSULTING LLC. Twilio-approved.
- **Customer profile**: `BUf5cd2668261710eff4bb1c97eea9bf10`. Twilio-approved.
- **A2P Trust Product**: `BU7406bb09eaf450c62a6fc4f40019fb1b`. Twilio-approved (policy: "A2P Messaging: Local - Business").
- **Messaging Service**: `MG4c8502a7ba7c8d229fd89e2d7b8c47cc`. "Low Volume Mixed A2P". `us_app_to_person_registered: true`.
- **A2P Campaign**: `QE2c6890da8086d771620e9b13fadeba0b`. IN_PROGRESS.
  - Use case: LOW_VOLUME
  - Opt-in flow: web form on profile page + START keyword
  - Message samples: chat forwarding, vote reminders, trip status updates
  - All compliance text (opt-in/opt-out/help) properly configured
- **SMS code in app**: `utilities/sms_utils.py` fully built. send_sms(), inbound webhook at `/api/sms/inbound`.
- **Notification preferences**: per-user channel (email/SMS/both) + frequency (real-time/daily/weekly/off) already in DB.

### When It's Approved (zero code changes needed)
- `campaign_id` gets assigned by TCR, carriers whitelist the number
- All existing SMS code starts working immediately
- Price drop alerts, chat-to-SMS bridge, vote reminders all go live
- The demo moment for Adam: "watch your phone" then SMS arrives with a trip update

### Note: Failed Brand Registration
`BN9925256294a428e50c9d8624fc58b5f1` was accidentally created with the wrong A2P profile (error 30794). It's stuck in FAILED state and can't be deleted via API. It does NOT affect the existing campaign (`QE2c68...`) which uses a different brand (`BN05299cc8c46ebf46b61fb87fb11d6ff9`). If it causes issues later, delete it from the Twilio console: console.twilio.com then Messaging then Trust Center then Brands.

---

## Remaining — Prioritized

### BEFORE ADAM DEMO
- **SMS working** — wait for A2P campaign approval, then test. This is the demo moment: "watch your phone" → SMS arrives with trip update.
- **Expense tracking UI** — ~~DB + CRUD exist, no frontend~~ **DONE** — expenses seeded on demo trip, "who owes who" balances on trip summary. Next: add expense form for manual entry.

### High Impact (Next Session)
- **Carpool/pickup coordination** — flight times are now stored per-member. Next: detect members arriving at the same airport within a few hours and suggest shared rides. Show "3 members arriving at PHX between 2-4 PM" on the summary page. This is a natural extension of the flight time data we just added.
- **Real flight time integration** — when Duffel/Travelpayouts return real flight offers, store actual departure/arrival times from the API instead of random ones. The JSONB `data` column already has the fields. When users mark a watch as "booked," prompt them to enter their flight times.
- **Hotel check-in/out times** — similar to flight times, store and display hotel check-in (typically 3-4 PM) and check-out (typically 10-11 AM) times. Useful for day-of coordination.
- **Airbnb / vacation rental integration** — THIS IS THE BIG ONE. Large group trips (10+ people) always have one person fronting $3-5K for the Airbnb on their card. That single booking is the anchor expense that drives the entire "who owes who" flow. Airbnb integration isn't just another adapter — it's the expense tracking killer feature. The pain: one person books, then spends 3 months chasing 14 Venmo requests. The solution: crab.travel shows the booking, auto-splits it, and shows everyone exactly what they owe. Investigate Airbnb Affiliate API, VRBO/Vacasa alternatives. Show "Stays" as separate category from hotels. Group-optimized search: filter by guest count, bedrooms, shared spaces.
- **Itinerary editor** — 21 items exist on demo trip but no add/edit/reorder UI. Add "Add item" button, drag-and-drop.
- **Auto-generate itinerary via AI** — when all watches booked, LLM generates day-by-day plan from flight times + hotel + destination research.

### Medium Impact
- **Amadeus flight integration** — free tier 2K searches/month, add as adapter
- **Kiwi Tequila integration** — free for affiliates, good for multi-city/flexible routing

### Future Thinking: Beyond Flights + Hotels
The current model assumes everyone flies and books a hotel. Real humans have way more options:

**Getting There:**
- **Road trips / RV rentals** — group of 6 rents an RV, drives from Denver to Scottsdale, saves on flights AND hotels. RV share platforms (Outdoorsy, RVshare) have affiliate APIs. Could even show "drive vs. fly" cost comparison per member based on their home city.
- **Train / Amtrak** — for domestic trips, some members might prefer rail. Especially East Coast groups.
- **Carpooling** — 4 members in Phoenix could carpool to Scottsdale instead of flying. Platform could detect nearby members and suggest shared rides.
- **Multi-modal** — some fly, some drive, some take the train. The platform should handle mixed transport within one trip.

**Staying There:**
- **Airbnb / VRBO / vacation rentals** — already #1 priority above, but worth noting: large groups almost always do whole-home rentals, not hotels. This is THE accommodation type for group trips.
- **Staying with friends/family** — real humans do this! "I'll crash at my cousin's place." Platform should let members mark "I have my own accommodation" so they're excluded from hotel cost splits but still included in activities/meals.
- **Camping** — national park trips, festival camping, glamping. Completely different cost structure.
- **Hostels** — budget groups, backpacker trips, younger demographics.
- **Timeshares / points** — "My uncle has a timeshare in Scottsdale, we can stay free." Members should be able to contribute non-cash assets to the trip.
- **Split stays** — 3 nights in a hotel, then 2 nights at an Airbnb. Real groups do this.

**The Big Insight:** CrabAI shouldn't assume one transport mode or one accommodation type per trip. The platform should handle "Marcus is flying from SEA, Sarah is driving from PHX, David is taking the train from LAX, and Emily has her own place there" — all in the same trip. The expense tracking and who-owes-who math needs to account for all of it.

### Infrastructure
- **Connection pool monitoring** — `/admin/pool` showing `pg_stat_activity`, per-app counts, leak detection
- **Cron health monitoring** — `crab.cron_executions` table for persistent execution logs

---

## API Cost Awareness

| API | Monthly Cost | Status |
|---|---|---|
| Duffel | ~$0 (no bookings) | Demoted to fallback |
| Travelpayouts | $0 (affiliate) | Primary flight source |
| Xotelo | N/A | **Removed** — needs RapidAPI auth |
| LiteAPI | $0 (sandbox) | Hotel fallback |
| LLM Router | $0 (free tier round-robin) | 15+ backends, **confirmed working** |
| Twilio | ~$0.01/msg | A2P campaign IN_PROGRESS — waiting on carrier approval |

---

## DB Connection Budget

| App | maxconn | Status |
|---|---|---|
| galactica | 6 | ✅ |
| crab_travel | 6 | ✅ |
| kumori | 3 | ✅ |
| dandy | 2 | ✅ |
| 2manspades | 2 | ✅ |
| scatterbrain | 2 | ✅ |
| stealth | 2 | ✅ |
| kindness_social | 3 | ✅ |
| ooqio | 2 | ✅ |
| wattson | 2 | ✅ |
| **Total** | **30/50** | **20 headroom** |

All apps: `statement_timeout=30s`, `connect_timeout=10`.

---

## Demo Trip Reference

| Stage | Token | URL | Trip |
|---|---|---|---|
| Booked | `qL6zhRAI` | `crab.travel/demo` | Scottsdale, AZ (12 members, fully booked) |
| Voting | `xL2aRt-k` | `crab.travel/demo/voting` | Reykjavik / Marrakech / Luang Prabang (50 members) |
| Planning | `TpPeETPm` | `crab.travel/demo/planning` | Andes / Ushuaia (75 members) |

Judy Tunaboat (user_id=81869) is a member of all 109 trips. Organizer of every 20th.
