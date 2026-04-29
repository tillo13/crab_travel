# What the timeshare angle in crab.travel actually does

**Source-of-truth report.** Every claim below is grounded in the
project docs (`docs/architecture.md`, `docs/timeshare_buildout.md`,
`docs/parking_lot.md`, `README.md`) and the live code in
`timeshare_routes.py`, `utilities/timeshare_*.py`, and
`templates/timeshare/`. Last verified 2026-04-28.

---

## One-sentence definition

A multi-tenant family-private workspace inside crab.travel that turns
a chaotic pile of timeshare ownership data — statements in six
inboxes, portal logins nobody can find, half-remembered trips — into a
structured dossier with an AI assistant, plus a bridge that lets a
family run a full crab-style group-trip plan against it for a future
vacation cycle.

It is **a feature of crab.travel, not a separate app.** All routes
live under `/timeshare/*` on the same Flask app, all 22+ tables live
inside the existing `crab` schema with a `timeshare_` prefix
(`crab.timeshare_groups`, `crab.timeshare_properties`, etc.), and it
shares Google OAuth + member sessions with the rest of crab.

Tillo Family is **customer #1** of a multi-tenant feature; any
crab.travel user can create their own timeshare group at
`/timeshare/groups/new` and the design does not gate on a hardcoded
allowlist.

---

## What it stores (the data model)

### Groups & members
- **`crab.timeshare_groups`** — one row per family-or-equivalent
  workspace. UUID identifier used directly in URLs. Has a name,
  optional Drive folder URL, status (`active`/`archived`/`deleted`),
  and a settings JSONB blob.
- **`crab.timeshare_group_members`** — who can see what. Roles:
  `owner`, `admin`, `family`, `readonly`. Stores per-(user, group)
  state inline (chat daily count, settings). Invite tokens live here
  too, single-use, 14-day expiry, OAuth-email-locked.
- **Share-by-link token** lives on `crab.timeshare_groups`
  (`share_view_token` + `share_view_token_expires_at`, 7-day default).
  Anyone with the link gets a session-only readonly view of the group
  — no Google account required, no email lock. Owner can rotate or
  disable it.

### The ownership facts (the "dossier")
Eight tables that capture what a real family timeshare actually
involves:

- **`crab.timeshare_properties`** — the unit you own. Developer
  ("Royal Resorts"), unit number ("K5133"), week, usage pattern
  (`biennial_even`, `annual`, etc.), trust-expiry date, exchange
  network (II, RCI, or none), country, city.
- **`crab.timeshare_contracts`** — purchase contract. Number,
  purchase date, price, financing terms, co-owners. Optional link to
  the user's own contract scan (we don't store the file).
- **`crab.timeshare_maintenance_fees`** — annual fee history. Year,
  billed amount, paid amount, late fees, paid date, JSONB breakdown
  (asset tax, hurricane fund, base, etc.). Tillo's data goes back to
  2006.
- **`crab.timeshare_loan_payments`** — loan amortization history per
  contract. Date, amount, principal, interest, balance after, method
  (visa_payoff, check, ach, etc.).
- **`crab.timeshare_trips`** — every trip the family has actually
  taken (home week, exchange, bonus stay, rental, purchase trip).
  Resort name, II code, dates, cost, uncertainty level (`confirmed`,
  `probable`, `family_memory`, `unverified`).
- **`crab.timeshare_trip_participants`** — many-to-many bridge to
  the `people` table for "who actually went."
- **`crab.timeshare_exchanges`** — II/RCI exchange history. Deposit
  date, week deposited, exchange fee, destination resort, status.
- **`crab.timeshare_people`** — group-scoped roster of every human
  who's part of this family's timeshare story. Includes those who
  haven't logged into crab (just tracked as participants in trips,
  contacts in portals, etc.).
- **`crab.timeshare_portals`** — login info for member portals (II,
  Royal Resorts, Interval Servicing, etc.). Username + member number
  + support phone. **Passwords are NOT stored** — only an optional
  Secret Manager reference.
- **`crab.timeshare_contacts`** — humans you call/email about the
  property (owner relations rep, CSF rep, customer service, etc.).
- **`crab.timeshare_document_refs`** — link registry only. Pointers
  to where the family keeps original docs (Drive URL, Dropbox URL).
  Crab does NOT host files.
- **`crab.timeshare_timeline_events`** — chronological feed of
  everything: emails sent, phone calls, decisions, purchases, notes.
  Source of truth for "what happened when."

### The II resort catalog (shared across groups)
Three tables in `crab` (no `timeshare_` prefix because future RCI/
Marriott catalogs are natural peers):

- **`crab.ii_regions`** — top-level world regions per II's
  taxonomy.
- **`crab.ii_areas`** — sub-areas within regions (e.g., "Florida
  Keys", "Cancún Riviera").
- **`crab.ii_resorts`** — every II resort with photos, sleep
  capacity, ratings, check-in day, nearest airport, lat/lng.

Catalog is populated by an off-App-Engine VPS scraper that posts to
`POST /timeshare/api/ii-catalog-sync` (bearer-authed) on roughly a
monthly cadence. ~2,491 resorts indexed currently.

### Group-specific overlay on the catalog
- **`crab.timeshare_group_shortlist`** — "Considering" list. Per
  group, which catalog resorts the family has flagged as interesting
  for future cycles. Toggled via `POST /g/<uuid>/shortlist/toggle`.
  Soft signal; readonly viewers can flip it.

### Ingestion pipeline
- **`crab.timeshare_ingest_jobs`** — Claude tool-use extraction
  from pasted text, uploaded PDFs, or public Drive URLs. Stores
  source content + extracted facts (proposed) + status
  (`pending_review`, `committed`, `rejected`). The user reviews each
  proposed fact before it lands in the structured tables. Original
  files are processed in-request and discarded — only the extracted
  text is kept for re-review.

### Chat conversation log
- **`crab.timeshare_chat_conversations`** + **`...chat_messages`** —
  the AI assistant ("Ask the dossier") keeps a per-(user, group)
  conversation history. Tool calls are logged for audit. Daily
  rate-limit per user.

### Bridge to crab.plans
- **`crab.plans`** has a `timeshare_group_id` UUID FK and a
  `plan_type='timeshare_cycle'` value. A "cycle" is a biennial or
  annual use-year (e.g., "2026 Week 38 cycle"). When a cycle plan is
  created, the entire crab plan engine — preference voting,
  availability overlap, multi-modal transport hunting (Duffel/
  LiteAPI/Viator/Travelpayouts), AI recommendations — applies to that
  cycle exactly like any other crab group trip.

---

## Who can see/do what (the access model)

Four roles, ranked: `readonly < family < admin < owner`. Default
invite role is `family`.

| Action | readonly | family | admin | owner |
|---|---|---|---|---|
| View dashboard, finances, trips, people, etc. | ✓ | ✓ | ✓ | ✓ |
| Toggle Considering on a resort | ✓ | ✓ | ✓ | ✓ |
| Use the Ask AI assistant | ✓ | ✓ | ✓ | ✓ |
| Add/edit fact rows (fees, trips, contacts, …) | — | ✓ | ✓ | ✓ |
| Use the Ingest wizard (paste/upload/Drive) | — | ✓ | ✓ | ✓ |
| Delete fact rows | — | — | ✓ | ✓ |
| Invite/remove members | — | — | ✓ | ✓ |
| Generate/rotate/disable the share link | — | — | ✓ | ✓ |
| Create a new cycle plan | — | — | ✓ | ✓ |
| Delete the group | — | — | — | ✓ |

Two ways to grant access:
- **Email-locked invite** (`/g/<uuid>/members/invite` →
  short-link emailed → recipient signs in with that exact email →
  becomes a member with the role you chose).
- **Public share link** (`/g/<uuid>/share/<32-char token>` → any
  visitor lands as a session-only readonly viewer with the welcome
  banner; auto-expires in 7 days; rotatable any time).

Server-side enforcement: every group-scoped route is decorated with
`@group_member_required(role)`. Non-members get **404** (not 403) so
the existence of a group leaks nothing.

---

## Routes and what each does

### Public (indexable)
- `GET /timeshare/` — feature landing page. *"Timeshare, the sane
  way."* Marketing copy + four value props + one CTA.

### Group lifecycle (login required)
- `GET /timeshare/groups/new` — create-a-group form
- `POST /timeshare/groups/new` — creates a `timeshare_groups` row,
  auto-adds creator as `owner`. Caps: 3/day, 10/lifetime per user.

### Group dashboard + nav (member-only)
- `GET /timeshare/g/<uuid>/` — dashboard. World map of II resorts
  with the family's home property pulsed gold, search box with
  filters (tier, sleeps, country chips), live result panel,
  "Considering" toggle, dossier quick-link tiles. Welcome banner
  fires on first visit (`?welcome=1`).
- `GET /timeshare/g/<uuid>/destinations/<country>` — country-level
  detail page (resort grid, family history at country, considering
  for that country).
- `GET /timeshare/g/<uuid>/api/resorts/search` — JSON search API
  powering the dashboard map+list (filter by `q`, `country`, `tier`,
  `min_sleeps`).

The nav inside any group renders a unified 5-tab primary
(`Overview · Catalog · Trips · Finances · Ask`) plus a `More ▾`
dropdown containing the secondary tabs (Property, People, Portals,
Contacts, Documents, Timeline, Cycles, plus Members and Ingest for
non-readonly users).

### Dossier views (read pages, all member-required)
| Route | Shows |
|---|---|
| `/g/<uuid>/property` | Property + contracts |
| `/g/<uuid>/finances` | Maintenance-fee table + loan-payment table per contract |
| `/g/<uuid>/trips` | Every trip taken, with notes |
| `/g/<uuid>/people` | Family/friends roster |
| `/g/<uuid>/portals` | Member-portal logins (no passwords) |
| `/g/<uuid>/contacts` | Reps, agents, customer service |
| `/g/<uuid>/documents` | Link registry to Drive/Dropbox originals |
| `/g/<uuid>/timeline` | Chronological event feed |
| `/g/<uuid>/timeline/<pk>/detail` | Single timeline event modal |

Each fact_view page renders mobile-optimized stacked cards under
640px, full table on desktop. Add/edit forms gated to `family+`.
Delete buttons gated to `admin+`.

### Fact editing (writes, family+)
- `POST /g/<uuid>/fact/<key>/new` — insert a fact row (key is one of
  `properties`, `contracts`, `maintenance_fees`, `loan_payments`,
  `trips`, `people`, `portals`, `contacts`, `document_refs`,
  `timeline_events`).
- `POST /g/<uuid>/fact/<key>/<pk>` — update.
- `POST /g/<uuid>/fact/<key>/<pk>/delete` — delete (admin+).

### Ingest wizard (family+)
- `GET /g/<uuid>/ingest` — wizard landing
- `POST /g/<uuid>/ingest/paste` — paste text → Claude extracts → job
- `POST /g/<uuid>/ingest/upload` — PDF upload → text extracted →
  Claude → job
- `POST /g/<uuid>/ingest/drive` — paste a *public* Drive folder URL,
  fetch listing, extract each file
- `GET /g/<uuid>/ingest/jobs` — review queue
- `GET /g/<uuid>/ingest/jobs/<id>` — review one job, accept/edit
  proposed facts row-by-row
- `POST /g/<uuid>/ingest/jobs/<id>/commit` — write accepted facts to
  the structured tables, all tagged with `source_ingest_job_id` for
  provenance
- `POST /g/<uuid>/ingest/jobs/<id>/reject` — discard proposed facts

The pattern: **unstructured doc → Claude tool-use extraction →
structured rows.** Original PDF bytes never persist; only extracted
text + extracted facts.

### Ask AI assistant (any role)
- `GET /g/<uuid>/ask` — chat page with sample-prompt chips
- `POST /g/<uuid>/api/chat/send` — sends a message; Claude calls
  scoped tools (`get_maintenance_fees(year)`, `get_trips(resort_code)`,
  `get_shortlist`, etc.) bound to this group only; returns answer
  with citation chips that link to the specific fact rows referenced.
  Daily message cap per user, cost tracked in
  `crab.ai_usage`/`crab.llm_calls`.

### II catalog (browsing, login required, no group context)
- `GET /timeshare/catalog` — all regions
- `GET /timeshare/catalog/region/<ii_code>` — areas in region
- `GET /timeshare/catalog/resort/<ii_code>` — public resort detail
  page (photos, ratings, sleeping capacity, nearest airport,
  description, "On your shortlist in: …" if applicable)

### II catalog (group-framed)
- `GET /g/<uuid>/catalog` — group view: their Considering list at
  top, regions grid below
- `POST /g/<uuid>/shortlist/toggle` — toggle Considering on a resort
  (any role including readonly)

### Members (admin+ for writes, all members for view)
- `GET /g/<uuid>/members` — member roster. For admins: invite form,
  share-link panel (URL + Copy + Text-it sms-launcher + expiry
  status + Rotate + Disable buttons).
- `POST /g/<uuid>/members/invite` — send an email invite.
- `GET /g/<uuid>/members/accept/<token>` — public accept landing
  (login_required); validates token + email-lock + expiry, then
  promotes the user to a real member and redirects to the dashboard
  with the welcome banner.

### Public share-link viewer (no auth)
- `GET /g/<uuid>/share/<token>` — drops a synthetic readonly session
  if the token is valid and unexpired, then redirects to the
  dashboard with `?welcome=1`. Welcome banner shows a soft "Sign in
  with Google for the full experience →" hint for share-link
  visitors.
- `POST /g/<uuid>/share/regenerate` — admin: rotate the token
  (kills any old link in the wild, sets new 7-day expiry).
- `POST /g/<uuid>/share/disable` — admin: NULL out the token.

### Cycle bridge to crab.plans (the trip-planning entry point)
- `GET /g/<uuid>/cycles` — list of `crab.plans` rows where
  `plan_type='timeshare_cycle'` AND `timeshare_group_id=<uuid>`. Shows
  travel window, member count, "Open plan →" link.
- `POST /g/<uuid>/cycles/new` (admin+) — creates a `crab.plans` row
  bound to this group with `plan_type='timeshare_cycle'`.
- `GET /g/<uuid>/cycles/<plan_id>` — scope-checks the plan belongs
  to this group, then redirects to the standard crab plan page
  `/to/<invite_token>` (where the existing crab plan engine takes
  over: voting, availability, recommendations, transport hunting,
  itinerary, expenses).

### Server-to-server
- `POST /timeshare/api/ii-catalog-sync` — bearer-authed; the VPS
  scraper writes to `crab.ii_regions/areas/resorts` here.

### Test/dev helpers
- `GET /timeshare/test/seed-readonly?apikey=…&group=…` —
  apikey-gated; idempotently creates a fake "Test Readonly Viewer"
  user and adds them as a readonly member of the specified group, so
  Playwright can walk through the readonly POV without OAuth.

---

## What's actually shipped vs designed-but-not-built

### Shipped + working in production
- Group create / invite / accept (Phase 1)
- All 8 fact_view pages with inline add/edit/delete (Phase 2)
- Ingest pipeline: paste, PDF upload, Drive public-link (Phase 3, 4)
- AI assistant with scoped tool-use + citation chips (Phase 5)
- II resort catalog (~2,491 resorts), region/area/resort pages,
  group Considering list (Phase 6)
- Cycle list page + create-a-cycle form + open-cycle redirect to
  crab plan view (Phase 7 partial — the bridge exists, used to use
  default crab plan UI)
- Public share link with 7-day expiry, rotate, disable (added 2026-04-28)
- Mobile-polished dashboard, finances, trips, ask page (added 2026-04-28)

### Designed, NOT yet shipped
- **II availability scraping** — the question "what dates are open
  for exchange in Sept 2026?" requires logging into II as a real
  member and querying their exchange pool. The HAR files captured
  show this is doable but II runs Akamai bot detection that defeated
  vanilla Playwright + stealth-chromium tonight. Per
  `docs/timeshare_buildout.md` §10, this is meant to be a Phase-7
  follow-on (**not** part of the current cycle bridge), and
  intentionally NOT a blocker for the cycle plan engine itself —
  cycles work today with the full crab adapter fan-out (Duffel,
  LiteAPI, Viator, Travelpayouts) for non-II inventory.
- **Destination picker filtered to II catalog** — when a plan has
  `timeshare_group_id`, the destination autocomplete should surface
  `crab.ii_resorts` rather than free-text. Designed in §8 of the
  buildout; not yet wired in `destinations_routes.py`.
- **Shortlist → Considering rebrand** — UI strings updated
  2026-04-28; underlying URL paths still use `/shortlist/toggle`
  (cosmetic-only change, intentional).

### Customer #1 seed data (already loaded)
Per `docs/timeshare_buildout.md` §15, the Tillo group is seeded with:
- Royal Sands Cancún unit K5133 week 38 biennial-even
- Contract #390653, purchase 2004-07-26, $10,000
- Maintenance fees from 2006–2024
- Loan payment history
- 10 family members (Andy, Heather, Lilla, Debra, Luke + Cynthia +
  Britney, Tanner + Celeste, Don Gabbert)
- Portal credentials for II, Royal Resorts Members Area, Interval
  Servicing, HICV
- Customer-relations contacts at Royal Resorts

---

## How a Sept 2026 trip decision *would* flow through this system

Given everything above, the architecture has a defined path even
without II availability scraping:

1. Andy (owner) hits `/g/<tillo>/cycles` and clicks "New cycle" — a
   `crab.plans` row is created with `plan_type='timeshare_cycle'`,
   `timeshare_group_id=<tillo>`, `travel_window_start=2026-09-05`,
   `travel_window_end=2026-09-25`. Members are seeded from the group.
2. Family members (or readonly share-link visitors) browse the
   group's Considering list + the world catalog and add candidate
   destinations to the cycle plan. The destination-add flow already
   triggers `_research_destination()` which fans out to
   Duffel/LiteAPI/Viator/Travelpayouts in background threads.
3. Within minutes: real flight prices, real hotel options near the
   candidate, real activity options appear via the SSE live feed (the
   same one the group-trip MVP uses).
4. The crab AI generates a "best fit" recommendation per candidate,
   referencing group preferences (budget, kid-friendly, mobility).
5. Family votes. Organizer locks. Multi-modal transport hunt
   continues to monitor prices via the watch engine + deal detection
   (`crab.price_history`).
6. **The one thing the system can't tell them yet: "is your II week
   actually exchangeable to that resort in that window?"** That's the
   Phase-7-availability question — out of scope for the cycle bridge
   itself. Today's workaround: Andy logs into II manually, checks
   exchange feasibility for the locked destination, reports back.

This is the design intent on the page. Whether the cycle plan
machinery is fully wired today is a separate verification step.

---

## What this report does NOT do

- It does not propose any new features, refactors, or fixes.
- It does not evaluate whether the design choices are correct.
- It does not benchmark against competitors.
- It is a faithful description of the as-designed and as-built state
  on 2026-04-28.
