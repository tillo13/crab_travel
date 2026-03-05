# crab.travel — Group Travel Coordination Platform

## Strategic Focus

**Current focus: Group travel.** We are building the group trip coordination product first. The retiree concierge use case is real, validated, and coming — but it's Phase 2.

**Why group travel first:**
- Faster feedback loop — a trip happens in weeks, not months
- Viral by nature — 15 people use it once, 15 potential future organizers
- Proves the core engine (preference aggregation, AI recommendations, data aggregation) that powers both use cases
- Lower support burden — group travelers are more tech-comfortable

**Architecture rule:** Always design with both use cases in mind. Every table, every API, every preference model must accommodate the retiree concierge without refactoring. The retiree product is the same engine with a different onboarding flow, a monthly delivery cadence, and family calendar integration on top. Never build something that locks out Phase 2.

**Retiree concierge is documented and parked in:** `docs/parking_lot.md`

---

## Context

An AI-powered group travel coordination platform born from a real group — 20 close friends (~50, affluent, retired lawyers and tech people) who want to travel together regularly. Phoenix now, Helsinki 2027, and beyond.

The MVP solves their immediate problem: coordinating group trips without the group-text-and-spreadsheet chaos. The bigger play is the most comprehensive travel data aggregation layer available — every flight, hotel, and activity from every source, normalized and curated by AI, with the group's collective preferences as the filter.

The persistent user profile compounds over time, making the platform stickier with every trip. Data is the moat.

**Domain:** crab.travel
**See also:** `docs/parking_lot.md` for retiree concierge vision, partner model, and future phases

---

## Core Performance Principle

**The UI is always fast. All heavy work is background.**

The user is never blocked waiting for anything. Every slow operation — adapter searches, AI recommendation generation, preference aggregation, price history polling — runs in a background thread. The UI immediately shows a status indicator ("searching...", "finding options...", "almost there") and updates live as results arrive.

This is the same philosophy as inroads: submit → instant acknowledgment → background work → UI updates when ready. The user can navigate freely at all times.

Specific rules:
- No route handler does slow work synchronously
- AI generation: start thread → return immediately → SSE or poll for updates
- Search fan-out: start adapter threads → return immediately → SSE pushes results as they land
- Any operation >200ms gets pushed to background with a status indicator
- "We're looking" is always better than a spinner that blocks

---

## Stack

- **Flask** — single `app.py`, no blueprints
- **PostgreSQL** — psycopg2 + ThreadedConnectionPool, kumori-404602 Cloud SQL
- **Google OAuth** — SimpleGoogleAuth via authlib
- **Claude API** — anthropic Python SDK (sonnet for recommendations)
- **Jinja2 + Tailwind CSS** — server-rendered templates (inroads pattern)
- **Vanilla JS** — fetch() for API calls
- **GCP App Engine** — app.yaml + gcloud_deploy.py

---

## Project Structure

```
crab_co/
├── app.py                          # All routes
├── requirements.txt
├── app.yaml                        # GAE config
├── gcloud_deploy.py                # Deploy script
├── .gitignore / .gcloudignore
├── CLAUDE.md                       # Project brief
├── utilities/
│   ├── __init__.py
│   ├── google_auth_utils.py        # Copy from inroads (SimpleGoogleAuth + get_secret)
│   ├── postgres_utils.py           # Adapted from kumori (pool + all CRUD)
│   ├── claude_utils.py             # Trimmed from kumori (generate_text only)
│   ├── plan_ai.py                  # Prompt engineering + recommendation parsing
│   └── invite_utils.py             # Token generation, member cookie, access decorator
├── templates/
│   ├── base.html                   # Tailwind + nav + dark mode
│   ├── index.html                  # Landing page
│   ├── login.html
│   ├── dashboard.html              # My plans
│   ├── plan_new.html               # Create plan form
│   ├── join.html                   # Invite landing (no auth needed)
│   ├── preferences.html            # Plan-specific preference intake
│   ├── profile.html                # Persistent user profile (interests, dietary, etc.)
│   └── plan_dashboard.html         # Shared plan view (tabs: overview, recs, itinerary, expenses)
├── static/
│   ├── css/style.css
│   ├── js/app.js
│   └── favicon.svg
└── docs/
    ├── architecture.md             # This file
    └── tables.sql                  # Full schema reference
```

---

## Two-Layer Preference Model

This is the core design decision that serves both use cases:

### Layer 1: User Profile (persistent, compounds over time)
Stored in `crab.user_profiles`. Created once, updated anytime. This is *you*.
- Interests (hiking, cooking, museums, nightlife, golf, etc.)
- Dietary needs
- Mobility/accessibility notes
- Travel style (adventure, relaxation, cultural, etc.)
- Accommodation preference (hotel, airbnb, hostel, flexible)
- Budget comfort level (budget, moderate, premium, luxury)
- Home location (for local event matching)

### Layer 2: Plan Preferences (per-plan overrides)
Stored in `crab.plan_preferences`. Specific to one plan.
- Budget range for *this* plan (in cents)
- Accommodation style for *this* plan
- Room preference (single, shared)
- Any plan-specific dietary or mobility notes
- Additional interests relevant to this destination

**How they merge:** When generating AI recommendations, the system reads the user profile first, then overlays plan-specific preferences. Anonymous members (no account) get a full preference form since they have no profile.

**Why this matters:** A user who's done 3 trips already has a rich profile. When they join trip #4, the preference form is mostly pre-filled. The platform gets easier to use over time — that's the retention moat.

---

## Database Schema (crab schema, kumori-404602 Cloud SQL)

Full SQL in `docs/tables.sql`. Key tables:

| Table | Purpose |
|---|---|
| `crab.users` | Google OAuth users + home_location |
| `crab.user_profiles` | Persistent preferences (1:1 with users) — the data asset |
| `crab.plans` | Universal container: trips, monthly plans, events (plan_type field) |
| `crab.plan_members` | Members per plan — authed or anonymous (member_token cookie) |
| `crab.plan_preferences` | Per-plan preference overrides (1:1 with plan_members) |
| `crab.search_results` | Raw normalized results from all adapters per plan — the live feed data |
| `crab.recommendations` | Claude-curated shortlist per plan (subset of search_results + AI reasoning) |
| `crab.itinerary_items` | Scheduled items added by any member from recs or search results |
| `crab.expenses` | Cost tracking with split logic |
| `crab.ai_usage` | Token/cost tracking |

### Plan Types
- `trip` — group trip (Phoenix weekend, bachelor party, family reunion)
- `monthly` — retiree monthly life plan (local events, activities, family moments)
- `event` — one-off event coordination (birthday party, reunion dinner)

All share the same members, preferences, recommendations, itinerary, and expense infrastructure.

---

## Core UX Decisions (Locked)

### 1. Single UI — Group Travel Only
One product, one experience. No dual-mode UI. Group travel is the entire focus of what the user sees and interacts with. Retiree concierge is a Phase 2 backend/prompt change, not a UI fork.

### 2. Search Paradigm — AI-Curated + Live Background Feed
- **Primary surface:** AI-curated shortlist. Claude reads the group's merged preferences and picks the best 5–8 hotels, activities, flights. This is the default view.
- **Background search feed:** Results from all adapters stream in continuously and non-blockingly. The user is never paused or gated. The feed appears as a live panel — new items surface as they arrive, like a ticker or expanding list. Users can browse the plan, fill in preferences, chat — anything — while results accumulate.
- **Multi-member concurrency:** Multiple group members may trigger or view the live feed simultaneously. Results are stored in the DB as they arrive (not held in memory), so any member opening the plan sees the current accumulated state plus new arrivals.
- **No search button.** Search triggers automatically when destination + dates are set on a plan. Re-runs when preferences change significantly.
- **Implementation:** Server-Sent Events (SSE) for the live feed. Each adapter runs as a background task, writes results to `crab.search_results` as they complete, SSE pushes new rows to all connected clients for that plan.
- **UX still TBD:** Exact visual treatment of the live feed panel (sidebar? bottom drawer? inline list?) — to be decided when wireframing.

### 3. Booking Model — Show Everything, Deep Link to Buy
- We are the most comprehensive aggregation layer available — every flight, hotel, activity from every source.
- **We show all prices.** That's the value. Users see what's out there across all sources in one place.
- **We do not process transactions.** Users click through to the source to actually book. Deep links only.
- **Why:** People have SkyMiles, Marriott points, Chase Sapphire credits. They need to book where their loyalty lives. We respect that. We surface the options; they complete the transaction where it makes sense for them.
- **Future:** In-platform booking is a later revenue upgrade, not MVP.

### 4. Group Decision Flow — Open, No Gatekeeping
- No vote required before items can be added to the itinerary.
- No organizer approval gate.
- Any group member can interact with recommendations and itinerary.
- The group figures it out — the platform doesn't impose a workflow on how they decide.
- Organizer retains ability to create the plan and manage the invite list, but has no special power over content decisions.

---

## Key Flows

### Onboarding (persistent profile)
1. User signs up via Google OAuth
2. Lands on profile setup — dating-app-style intake (tap interest tiles, set travel style, dietary needs)
3. Profile saved to `crab.user_profiles`
4. Redirected to dashboard — immediately sees AI-suggested plan ideas based on profile

### Plan Creation + Invite Flow
1. Organizer creates plan → gets `https://crab.travel/join/{invite_token}`
2. Invitee clicks link → sees plan info + name form (no signup required)
3. Submits name → gets `member_token` cookie (30 days) → redirected to preferences
4. Authed members: plan preferences pre-filled from their user profile
5. Anonymous members: full preference form (acts as their profile for this plan)
6. Everyone lands on plan dashboard

### Background Search (non-blocking, streaming)
1. Organizer sets destination + dates on a plan → triggers background search automatically
2. Each adapter (Duffel, LiteAPI, Viator, etc.) runs as an independent daemon thread (same pattern as inroads scoring/resume parsing)
3. As each adapter returns results, rows are written to `crab.search_results` immediately
4. SSE endpoint (`/api/plan/<id>/search/stream`) pushes new rows to all connected clients watching that plan
5. Frontend live feed panel appends new results as they arrive — no page refresh, no blocking
6. Multiple group members can be on the plan simultaneously — all see the same live feed (SSE is per-client, results are shared via DB)
7. Search re-triggers automatically when preferences change significantly (new budget range, new interests)
8. Results persist in `crab.search_results` — any member opening the plan later sees the full accumulated set + SSE for any still-running adapters

**Background task implementation:** Python `threading.Thread(daemon=True)` — same pattern as inroads. No Cloud Tasks needed. Each adapter is a short-lived call (5–15s); if an instance restarts mid-search the search re-triggers on next plan load. Cloud Scheduler (cron.yaml) handles the nightly price-history polling jobs, same as inroads uses for URL validation.

### AI Curation (runs on top of search results)
1. Once enough results have accumulated (or on demand), Claude reads:
   - All `crab.search_results` for this plan
   - Merged group preferences (budget overlap, interest frequency, dietary union, accessibility flags)
2. Returns a curated shortlist: best 5–8 hotels, 5–8 activities, flight options — with a "why this fits your group" note per item
3. Shortlist is the default view; full search feed is always accessible
4. Any member can add any item (curated or from the full feed) directly to the itinerary — no approval gate

### Ongoing Engagement (the retention loop)
- Platform periodically generates suggestions based on user profile:
  - "There's a jazz festival near you this weekend"
  - "Based on your interests, here's a 3-day trip idea to Sedona"
  - "Your friend Sarah just created a trip to Nashville — want to join?"
- Each interaction refines the profile (implicit: what they click, book, rate)
- Monthly digest email with personalized suggestions (future: Twilio/SendGrid)

### Expense Splitting
- Any member adds expenses (who paid, amount, category)
- `calculate_balances()` computes net per member
- Greedy settlement algorithm minimizes transactions
- Dashboard shows "Alex owes Sarah $35"

---

## Route Map

### Public (no auth)
- `GET /` — Landing page
- `GET /login` — Login page
- `GET /login/google` — Initiate Google OAuth
- `GET /auth/callback` — OAuth callback
- `GET /logout` — Clear session
- `GET /health` — Health check

### User Profile (auth required)
- `GET /profile` — View/edit persistent preference profile
- `POST /api/profile` — Save profile preferences

### Invite Flow (no auth — member_token cookie)
- `GET /join/<invite_token>` — Invite landing page
- `POST /join/<invite_token>` — Submit name to join
- `GET /plan/<plan_id>/preferences` — Plan-specific preference form
- `POST /api/preferences` — Save plan preferences

### Plan Views (member_token cookie required)
- `GET /plan/<plan_id>` — Plan dashboard (overview tab)
- `GET /plan/<plan_id>/recommendations` — AI recommendations
- `GET /plan/<plan_id>/itinerary` — Itinerary view
- `GET /plan/<plan_id>/expenses` — Expenses + balances

### Organizer (auth required)
- `GET /dashboard` — My plans overview + AI-suggested plan ideas
- `GET /plan/new` — Create plan form (select plan_type)
- `POST /api/plan/create` — Create plan
- `POST /api/plan/<plan_id>/update` — Update plan details
- `POST /api/plan/<plan_id>/generate` — Generate AI recommendations

### Search & Live Feed (member_token or auth)
- `GET /api/plan/<plan_id>/search/stream` — SSE endpoint; pushes new search_results rows as they arrive
- `GET /api/plan/<plan_id>/search/results` — Full accumulated search results (JSON, for initial page load)
- `POST /api/plan/<plan_id>/search/trigger` — Manually re-trigger search (if destination/dates change)

### APIs (member_token or auth)
- `POST /api/recommendation/<id>/status` — Approve/reject (kept for AI curation shortlist management)
- `POST /api/itinerary/add` — Add itinerary item
- `POST /api/itinerary/<item_id>/update` — Update item
- `POST /api/itinerary/<item_id>/delete` — Remove item
- `POST /api/expense/add` — Add expense
- `POST /api/expense/<expense_id>/delete` — Remove expense
- `GET /api/plan/<plan_id>/balances` — Who owes whom (JSON)

---

## How Both Use Cases Share Infrastructure

| Component | Group Trip | Retiree Concierge |
|---|---|---|
| User profile | Same table, same fields | Same table, same fields |
| Plan creation | Organizer creates, invites friends | AI creates monthly, user reviews |
| Plan type | `trip` | `monthly` |
| Invite flow | Share link with group | Share link with family |
| Preferences | Budget + interests for this trip | Already in profile |
| AI recommendations | Hotels, activities, restaurants at destination | Local events, activities, trips near home |
| Itinerary | Day-by-day trip schedule | Monthly calendar |
| Expenses | Split costs among group | Track personal budget |
| Recurring | No | Yes (monthly) |

The AI prompt is the only thing that changes significantly between use cases. Everything else is the same tables, same routes, same templates with conditional rendering based on `plan_type`.

---

## Files to Copy/Adapt From

| New File | Source | Action |
|---|---|---|
| `utilities/google_auth_utils.py` | `../inroads/utilities/google_auth_utils.py` | Copy, update secret names to CRAB_* |
| `utilities/postgres_utils.py` | `../kumori/utilities/postgres_utils.py` | Keep pool pattern, replace CRUD |
| `utilities/claude_utils.py` | `../kumori/utilities/claude_utils.py` | Trim to generate_text() only |
| `templates/base.html` | `../inroads/templates/base.html` | Adapt nav/branding |
| `app.yaml` | `../kumori/docs/start_new_kumori_based_project.md` Phase 3 | Change project ID |
| `gcloud_deploy.py` | `../kumori/gcloud_deploy.py` | Change EXPECTED_PROJECT_ID |

---

## Build Order

### Phase 1: Skeleton + Auth + Profile
- Project scaffolding (app.py, requirements.txt, app.yaml, .gitignore, .gcloudignore)
- `utilities/google_auth_utils.py` (copy from inroads)
- `utilities/postgres_utils.py` (pool + init_database with users + user_profiles)
- Flask app setup, Google OAuth routes
- Templates: base.html, index.html, login.html, profile.html
- GCP project setup (follow kumori setup doc phases 1-2)

### Phase 2: Plan Creation + Invite Flow
- Add plans + plan_members tables
- Plan CRUD + member CRUD in postgres_utils.py
- `utilities/invite_utils.py` (tokens, cookies, access decorator)
- Routes: /dashboard, /plan/new, /join/<token>
- Templates: dashboard.html, plan_new.html, join.html

### Phase 3: Preferences + Plan Dashboard
- Add plan_preferences table
- Preference CRUD + profile-to-plan-preferences merge logic
- Plan-specific preference form (pre-filled from profile for authed users)
- Plan dashboard template with overview tab

### Phase 4: AI Recommendations
- `utilities/claude_utils.py` (trimmed from kumori)
- `utilities/plan_ai.py` (prompts per plan_type, preference aggregation, JSON parsing)
- Add recommendations table
- Generate + display + approve/reject flow

### Phase 5: Itinerary + Expenses
- Add itinerary_items + expenses tables
- Itinerary CRUD + expense CRUD + calculate_balances()
- Itinerary tab + expenses tab on plan dashboard
- Settlement algorithm

### Phase 6: Polish + Deploy
- gcloud_deploy.py, GCP secrets (CRAB_*), error pages
- Mobile responsiveness pass
- ai_usage tracking
- Deploy to App Engine

### Future Phases (post-MVP)
- AI-generated plan suggestions on dashboard (based on profile)
- Monthly digest emails (SendGrid)
- Family calendar integration (Google Calendar API)
- Local event ingestion layer (parse URLs, emails, flyers)
- Notification preferences + Twilio SMS

---

## Data Source Registry — The Aggregation Layer

**Philosophy:** crab.travel is not a booking site — it's an aggregation and curation layer. The goal is to surface *every possible* flight, hotel, activity, and experience from *every possible* source, normalize it into a canonical schema, deduplicate by property/route, and surface the best options ranked by price, fit, and deal score.

This is how Kayak, Google Flights, and Skyscanner work internally. We do the same.

---

### Canonical Schemas (normalize everything into these)

```python
# Flight
{
  "type": "flight",
  "origin": "PHX",
  "destination": "HEL",
  "depart_at": "2027-06-01T08:00:00Z",
  "arrive_at": "2027-06-02T06:00:00Z",
  "airline": "Finnair",
  "stops": 1,
  "price_usd": 842.00,
  "source": "duffel",
  "deep_link": "https://...",
  "bookable": True,          # False = redirect only
  "raw": { ... }             # Original API response
}

# Hotel
{
  "type": "hotel",
  "property_id": "liteapi_h_12345",  # source-prefixed to avoid collision
  "name": "The Saguaro Scottsdale",
  "lat": 33.4942,
  "lng": -111.9261,
  "star_rating": 4,
  "price_per_night_usd": 189.00,
  "total_price_usd": 756.00,
  "nights": 4,
  "source": "liteapi",
  "deep_link": "https://...",
  "bookable": True,
  "raw": { ... }
}

# Activity
{
  "type": "activity",
  "name": "Hot Air Balloon Ride at Sunrise",
  "location": "Scottsdale, AZ",
  "lat": 33.4942,
  "lng": -111.9261,
  "price_usd": 189.00,
  "duration_hours": 3,
  "category": ["outdoor", "adventure", "romantic"],
  "source": "viator",
  "deep_link": "https://...",
  "bookable": True,
  "raw": { ... }
}

# Car Rental
{
  "type": "car",
  "pickup_location": "PHX Airport",
  "dropoff_location": "PHX Airport",
  "pickup_at": "2027-01-10T10:00:00Z",
  "dropoff_at": "2027-01-13T10:00:00Z",
  "car_class": "SUV",
  "price_total_usd": 312.00,
  "source": "discover_cars",
  "deep_link": "https://...",
  "bookable": True,
  "raw": { ... }
}
```

---

### Layer 1: Affiliate Aggregators (One Signup → Many Brands)

#### Travelpayouts
- **What it covers:** 100+ travel brands via one API — flights (Aviasales, JetRadar, WayAway + 15 more), hotels (Hotels.com, Hotellook), activities (Viator, Klook, Tiqets), cars (Rentalcars.com, EconomyBookings)
- **Pricing:** Free, commission-based (1–8% depending on vertical)
- **Signup:** Self-serve, instant — [travelpayouts.com](https://travelpayouts.com)
- **Data type:** Cached flight prices, real-time hotel/activity redirect
- **Bookable:** Redirect only (deep links to partner sites)
- **Priority:** Sign up first — covers 80% of market immediately
- **Adapter key:** `travelpayouts`

#### CJ Affiliate / Impact / Rakuten / Awin
- **What it covers:** Generic affiliate networks with travel programs (Booking.com, Expedia, individual airlines)
- **Pricing:** Free, commission
- **Use:** Fallback for brands not on Travelpayouts
- **Adapter key:** `cj`, `impact`, `rakuten`, `awin`

---

### Layer 2: Direct Real-Time Booking APIs (Live Prices + Actual Booking)

#### Flights

| API | Inventory | Pricing | Signup | Adapter Key |
|---|---|---|---|---|
| **Duffel** | 300+ airlines incl. AA/Delta/BA via NDC | Pay-per-booking, no minimums | Self-serve, instant sandbox | `duffel` |
| **Amadeus Self-Service** | 400+ airlines (missing AA/Delta/BA) | 2K free searches/month then pay | Self-serve, instant | `amadeus` |
| **Skyscanner Partners** | All airlines (meta-search) | Free for approved partners | Apply, takes weeks | `skyscanner` |
| **Kiwi/Tequila** | 800+ airlines + trains/buses + virtual interlining | Free, 3% commission | Invitation only — get on radar now | `kiwi` |

**Notes:**
- Amadeus Self-Service dies July 2026 — use while available, migrate to Duffel before then
- Duffel + Amadeus together cover effectively all major global carriers
- Skyscanner fills regional/budget gaps neither has
- Southwest Airlines has no GDS/API presence — Travelpayouts affiliate redirect only

#### Hotels

| API | Inventory | Model | Signup | Adapter Key |
|---|---|---|---|---|
| **LiteAPI (Nuitee)** | 2M+ hotels | Commission | Self-serve, hours to live | `liteapi` |
| **RateHawk** | 2.6M properties | Net rates or commission | Sales contact, sandbox available | `ratehawk` |
| **Hotelbeds** | 250K hotels | Net rates | Self-serve test (50 req/day) | `hotelbeds` |
| **Expedia Rapid API** | Hotels + 900K Vrbo rentals | Commission | Partnership approval | `expedia` |
| **Priceline Partner Network** | 980K hotels | Commission | Partnership approval | `priceline` |
| **WebBeds** | 370K–500K properties | Net rates | Contact only, XML API | `webbeds` |
| **Booking.com Affiliate** | Millions | 25-30% of their margin | Apply — currently paused for new connectivity | `booking` |

**Notes:**
- LiteAPI + RateHawk + Hotelbeds = three different bed banks with different wholesale rates for the same property → show all three prices
- Expedia Rapid includes Vrbo — fills the vacation rental gap (Airbnb has no public API)
- WebBeds is XML-only — adapter complexity higher, worth it for wholesale rates

#### Activities & Experiences

| API | Inventory | Commission | Signup | Adapter Key |
|---|---|---|---|---|
| **Viator** | 300K+ experiences globally | 8% | Instant basic access | `viator` |
| **GetYourGuide** | Large, strong in Europe | 8% | Needs 100K+ monthly visits — apply early | `getyourguide` |
| **Klook** | Strong Asia-Pacific | 2–5% (via Travelpayouts) | Via Travelpayouts | `klook` |
| **Tiqets** | Museums and attractions | 8% (via Travelpayouts) | Via Travelpayouts | `tiqets` |
| **Musement (TUI)** | 40K+ in 80 countries | 50% of their margin | Application | `musement` |
| **Civitatis** | Europe + Latin America | Commission | Affiliate application | `civitatis` |

**Geographic coverage logic:** Viator for global baseline → GetYourGuide for Europe depth → Klook for Asia-Pacific → Musement/Civitatis for niche cultural inventory. Together = no dead zones.

#### Cars

| API | Model | Signup | Adapter Key |
|---|---|---|---|
| **Discover Cars** | 70% revenue share | Self-serve | `discover_cars` |
| **Rentalcars.com (Priceline)** | Commission | Via Travelpayouts or direct | `rentalcars` |
| **Carnect** | B2B API | Application | `carnect` |

#### Travel Insurance (hidden revenue gem)

| API | Commission | Signup | Adapter Key |
|---|---|---|---|
| **Cover Genius XCover** | 15–30% of premium | Apply — high priority | `cover_genius` |

Notes: Upsell at checkout on any booking. Average premium ~$80 = $12–24 per policy. 1000 bookings/month = $12K–24K additional revenue with zero inventory cost.

#### Cruises

| Source | Commission | Model |
|---|---|---|
| Royal Caribbean | 4% on ~$2.5K avg ($100/sale) | Affiliate program |
| Carnival | Commission | Affiliate program |
| Norwegian | Commission | Affiliate program |

All cruise lines are redirect/affiliate only — no booking API. Deep link from recommendations.

---

### Layer 3: Direct Supplier Programs (What Aggregators Miss)

#### Airlines with No API/GDS Presence
- **Southwest Airlines** — affiliate redirect via Travelpayouts only. Cannot be booked via Duffel or Amadeus.
- **Frontier, Spirit, Allegiant** — budget carriers with limited API presence; Skyscanner meta-search catches them.

#### Hotel Chain Direct Affiliate Programs
Hotel chains sometimes offer rates lower than OTAs (no OTA commission markup). Apply to all:
- Marriott Bonvoy Affiliate Program
- Hilton Honors Affiliate Program
- Hyatt Affiliate Program
- IHG Affiliate Program
- Best Western Affiliate Program

**Adapter key:** `marriott_direct`, `hilton_direct`, etc. Low implementation priority but strong for loyal traveler segment.

#### Airbnb
- **No public search or booking API.** Property managers only.
- Strategy: Affiliate redirect via Travelpayouts or direct Airbnb affiliate link.
- Expedia Rapid API covers Vrbo (900K rentals) as the functional alternative.

---

### Layer 4: Events, Restaurants & Local Discovery

#### Events

| API | Inventory | Rate Limits | Adapter Key |
|---|---|---|---|
| **Ticketmaster** | Live events, sports, concerts | Free, 5K/day | `ticketmaster` |
| **Eventbrite** | Community events, classes, experiences | Free, 500/day | `eventbrite` |
| **SeatGeek** | Sports + concerts, resale | Partnership | `seatgeek` |
| **Meetup** | Local group events | Free tier | `meetup` |

#### Restaurants
- **Google Places API** — discovery, ratings, hours, photos. Primary source.
- **OpenTable / Resy** — deep link to reservations. No booking API — redirect only.
- **Yelp Fusion API** — supplemental ratings and reviews.

#### Weather
- **Visual Crossing** — 1K free records/day, historical + forecast. Better than OpenWeatherMap for historical analysis.
- **Adapter key:** `visual_crossing`

---

### Layer 5: Price Intelligence Database (The Moat)

No external API provides this — we build it ourselves by polling the above sources on a schedule.

#### What We Store
```sql
CREATE TABLE crab.price_history (
  id              BIGSERIAL PRIMARY KEY,
  record_type     TEXT NOT NULL,          -- flight | hotel | activity | car
  canonical_key   TEXT NOT NULL,          -- route (PHX-HEL) or property_id
  source          TEXT NOT NULL,          -- adapter key
  price_usd       NUMERIC(10,2) NOT NULL,
  observed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  travel_date     DATE,                   -- the date the price is for
  raw             JSONB
);

CREATE INDEX ON crab.price_history (canonical_key, record_type, observed_at);
CREATE INDEX ON crab.price_history (travel_date, canonical_key);
```

#### Deal Detection Logic
```python
def is_deal(canonical_key, current_price, record_type):
    avg_90d = query_90day_average(canonical_key, record_type)
    if avg_90d and current_price < avg_90d * 0.70:  # 30%+ below 90-day avg
        return True, (avg_90d - current_price), round((1 - current_price/avg_90d) * 100)
    return False, 0, 0
```

#### Polling Schedule
- Flights: nightly for routes matching active plan destinations
- Hotels: nightly for destination cities with active plans
- Activities: weekly (prices change less frequently)
- Alert: push notification / email when deal detected for a user's saved destination

This is exactly how Scott's Cheap Flights works. No magic — just monitoring + history. Our moat is that the history is tied to *our users' specific preferences and saved destinations*, not generic routes.

---

### Adapter Pattern (Implementation)

Each data source gets one adapter class with a consistent interface:

```python
# utilities/adapters/base.py
class TravelAdapter:
    source_key: str  # e.g. "duffel", "liteapi"

    def search_flights(self, origin, destination, depart_date, return_date=None, passengers=1) -> list[dict]:
        raise NotImplementedError

    def search_hotels(self, destination, checkin, checkout, guests=1) -> list[dict]:
        raise NotImplementedError

    def search_activities(self, destination, date_from=None, date_to=None, interests=None) -> list[dict]:
        raise NotImplementedError

    def normalize_flight(self, raw) -> dict:
        # Returns canonical flight schema
        raise NotImplementedError

    def normalize_hotel(self, raw) -> dict:
        raise NotImplementedError

    def normalize_activity(self, raw) -> dict:
        raise NotImplementedError
```

```python
# utilities/adapters/duffel.py
class DuffelAdapter(TravelAdapter):
    source_key = "duffel"

    def search_flights(self, origin, destination, depart_date, ...):
        # Call Duffel API, return normalized list
        ...
```

#### Query Engine (fan out + merge)

```python
# utilities/travel_query.py

FLIGHT_ADAPTERS = [DuffelAdapter(), AmadeusAdapter(), SkyscannerAdapter(), TravelpayoutsAdapter()]
HOTEL_ADAPTERS = [LiteAPIAdapter(), RateHawkAdapter(), HotelbedsAdapter(), ExpediaAdapter()]
ACTIVITY_ADAPTERS = [ViatorAdapter(), GetYourGuideAdapter(), KlookAdapter(), TiqetsAdapter()]

def search_all_flights(origin, destination, depart_date, ...):
    results = []
    for adapter in FLIGHT_ADAPTERS:
        try:
            results.extend(adapter.search_flights(...))
        except Exception as e:
            log(f"⚠️ {adapter.source_key} failed: {e}")
    return deduplicate_flights(results)

def deduplicate_flights(results):
    # Group by (origin, destination, depart_at, airline, stops)
    # Within group: keep all, sort by price
    # Return sorted by price asc
    ...

def deduplicate_hotels(results):
    # Group by (name, lat, lng) with fuzzy matching
    # Within group: one record per source showing each source's price
    # Return sorted by lowest price across all sources
    ...
```

---

### Signup Priority Queue

Do these in order — each one adds coverage immediately:

| Priority | Service | Action | Time to Live |
|---|---|---|---|
| 1 | **Travelpayouts** | Self-serve signup | Today — hours |
| 2 | **Duffel** | Self-serve, instant sandbox | Today — hours |
| 3 | **LiteAPI** | Self-serve | Today — hours |
| 4 | **Viator** | Instant basic access | Today — hours |
| 5 | **Discover Cars** | Self-serve | Today — hours |
| 6 | **Cover Genius XCover** | Apply now | Days |
| 7 | **Ticketmaster API** | Self-serve | Today — hours |
| 8 | **Eventbrite API** | Self-serve | Today — hours |
| 9 | **Visual Crossing** | Self-serve | Today — hours |
| 10 | **RateHawk** | Contact sales, sandbox available | Days |
| 11 | **Hotelbeds** | Self-serve test account | Days |
| 12 | **Skyscanner Partners** | Apply — weeks to approve | Weeks |
| 13 | **GetYourGuide** | Apply — needs traffic proof | Later |
| 14 | **Kiwi/Tequila** | Get on their radar, invite only | When ready |
| 15 | **Booking.com Affiliate** | Apply — currently paused | When reopens |
| 16 | **Expedia Rapid API** | Partnership approval | Weeks |
| 17 | **Hotel chain directs** | Marriott, Hilton, Hyatt, IHG affiliate programs | Later |

---

### Legal Notes

- **ScrapingBee / Google Flights scraping:** Google sued SerpAPI in December 2025 for scraping Google Flights/Hotels. Remove this before launch. Replace with Duffel + Skyscanner Partners.
- **All listed APIs above:** Legitimate partner programs — no legal exposure.
- **Kiwi/Tequila:** Legitimate API but invite-only — don't scrape their data to replicate it.
- **Airbnb:** No public API. Use Expedia Rapid (Vrbo) as the vacation rental alternative. Do not scrape Airbnb.
