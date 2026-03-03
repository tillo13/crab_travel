# crab.travel — Life Planning & Group Coordination Platform

## Context

An AI-powered travel and life planning platform born from a real group — 20 close friends (~50, affluent, retired lawyers and tech people) who want to travel together regularly. Phoenix now, Helsinki 2027, and beyond.

The MVP solves their immediate problem: coordinating group trips without the group-text-and-spreadsheet chaos. The bigger play is an AI-enabled travel agency where the platform grows through each member's network, and resorts/travel partners pay to reach qualified groups of affluent repeat travelers.

Both use cases (group trips + retiree life concierge) share the same infrastructure. A "plan" is the universal unit — a trip, a monthly life plan, a family event, a weekend idea. The persistent user profile compounds over time, making the platform stickier with every trip.

**Domain:** crab.travel
**See also:** `docs/parking_lot.md` for future vision, partner model, and growth ideas

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
| `crab.recommendations` | Claude-generated recs per plan |
| `crab.itinerary_items` | Scheduled items from recs or manual adds |
| `crab.expenses` | Cost tracking with split logic |
| `crab.ai_usage` | Token/cost tracking |

### Plan Types
- `trip` — group trip (Phoenix weekend, bachelor party, family reunion)
- `monthly` — retiree monthly life plan (local events, activities, family moments)
- `event` — one-off event coordination (birthday party, reunion dinner)

All share the same members, preferences, recommendations, itinerary, and expense infrastructure.

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

### AI Recommendations
1. Organizer clicks "Generate Recommendations"
2. `plan_ai.py` merges all member preferences (profile + plan-specific):
   - Budget: find overlap range
   - Interests: frequency count across all members
   - Dietary: union of all restrictions
   - Accommodation: majority preference
   - Accessibility: flag any mobility needs
3. Sends to Claude sonnet with structured JSON output
4. Returns hotel/activity/restaurant recs with compatibility scores
5. Organizer approves/rejects → approved items become itinerary entries

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

### APIs (member_token or auth)
- `POST /api/recommendation/<id>/status` — Approve/reject
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
