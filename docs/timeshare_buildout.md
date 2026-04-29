# Timeshare Feature for crab.travel (v3 — multi-tenant, Tillo is customer #1)

**Supersedes:** v2 of this doc (Tillo-only hardcoded merge) and v1 (`tillotime_site_buildout_guide.md`, standalone App Engine app).
**Status:** Planning document. Nothing built yet. Execute from this file.
**Last updated:** 2026-04-20
**Target URL:** `https://crab.travel/timeshare/*` (path, not subdomain)

**DB decision (verified Apr 20 2026 against live kumori Postgres):** the shared kumori Cloud SQL instance hosts every Andy-project's schema (18 schemas total: `crab`, `wattson`, `scatterbrain`, `dandy`, `inroads`, `consulting`, `litellm`, etc.). `CRAB_POSTGRES_*` and `KUMORI_POSTGRES_*` secrets point at the same instance. **All 21 timeshare tables live inside the existing `crab` schema with a `timeshare_` prefix** (`crab.timeshare_groups`, `crab.timeshare_properties`, etc.) — matching crab's own internal subsystem-prefix pattern that already organizes bot-testing (`crab.bot_runs`), price-watching (`crab.member_watches`), multi-modal hunting (`crab.trip_legs`, `crab.transport_options`, `crab.leg_hunts`), and LLM telemetry (`crab.llm_calls`). Same-schema FKs like `crab.timeshare_group_members.user_id → crab.users(pk_id)` are trivial. If crab ever graduates to its own instance, all crab+timeshare tables move together as one schema — `pg_dump -n crab`.

---

## 0. What changed (read this first)

Two architectural pivots from v2:

### 0.1 From hardcoded-Tillo to multi-tenant

v2 gated `/tillotime/*` on a hardcoded Python set of 10 Tillo family
emails. v3 replaces that with `crab.timeshare_groups` + `crab.timeshare_group_members`
tables. **Any crab.travel user can create a timeshare group, add their
property, and invite members.** Tillo becomes customer #1 — "Tillo Family —
Royal Sands Cancún" is one `crab.timeshare_groups` row, not a constant.

Why: if the pattern works for one family, it plausibly works for others.
Multi-tenant from day one costs ~4 extra hours (one table, one invite
flow) and preserves the option without committing to marketing it.
Default for every group: **private, invisible, owner-only-visible** until
the owner explicitly flips a flag.

### 0.2 From Drive-mirror to structured ingest

v2 mirrored 9 Google Docs, 1 Sheet, and 4 subfolders as HTML/JSON blobs
in `tillotime.dossier_docs`. v3 throws that out.

**Unstructured Google Docs don't scale past one family.** A second user's
"Finances" doc has different headers, different columns, different
conventions. Claude chatbots reading 9 blob-text docs hallucinate facts
and can't answer comparative questions ("how did our CSF compare to last
year's?").

v3 replaces the mirror with an **ingestion pipeline**: Google Docs,
Sheets, PDFs, and pasted text are INPUTS to Claude-driven fact
extraction. The structured tables (`crab.timeshare_maintenance_fees`,
`crab.timeshare_trips`, `crab.timeshare_people`, etc.) are the **canonical source
of truth**.

The chatbot stops stuffing 50KB of doc text and starts calling scoped
tools (`get_maintenance_fees(year)`, `get_trips(resort_code)`) that
return structured JSON from the group's tables. Answer quality goes up,
hallucination goes down, and citations link to specific fact rows.

### 0.3 We do not store user documents

**crab.travel is not a document storage service.** Uploaded PDFs are
processed in-request (pdfplumber → text → Claude tool-use → facts),
then the binary is discarded. Google Drive / Dropbox / user-provided
URLs are read via API on-demand for ingestion and never mirrored to
Cloud Storage. The only persistent records are:

1. **Extracted text** in `crab.timeshare_ingest_jobs.source_content`
   (small, for provenance and re-review if Claude got something wrong)
2. **Extracted facts** committed to the structured fact tables
3. **A link registry** (`crab.timeshare_document_refs`) pointing at
   where the user keeps the original — Drive link, Dropbox URL, etc.

Users keep their files wherever they already live. We're a tenant of
their data, not the landlord. Operational wins: zero GCS bytes to back
up, no PII-heavy file cabinet to breach, GDPR deletion requests answered
trivially (we never had the files). Product positioning win: *"We don't
store your documents"* is a real differentiator against
upload-everything SaaS competitors.

### 0.4 Net effect

Build is bigger than v2 (~3–4 days vs 2) but the result is a real product,
not a Tillo-custom mirror. Tillo gets the same family-private portal, but
built on infrastructure that could onboard a second owner tomorrow.

---

## 1. Host, URL, privacy

### 1.1 URL shape

```
https://crab.travel/timeshare/                        - landing (timeshare feature explainer; marketing)
https://crab.travel/timeshare/groups/new              - create a group (any logged-in crab user)
https://crab.travel/timeshare/g/<group_uuid>/         - group dashboard (members only)
https://crab.travel/timeshare/g/<group_uuid>/dossier  - structured data views (finances, trips, people)
https://crab.travel/timeshare/g/<group_uuid>/catalog  - II resort directory (shared across groups)
https://crab.travel/timeshare/g/<group_uuid>/ingest   - Drive/paste/upload ingestion wizard
https://crab.travel/timeshare/g/<group_uuid>/ask      - chatbot
https://crab.travel/timeshare/g/<group_uuid>/members  - invite / manage access
https://crab.travel/timeshare/g/<group_uuid>/cycles   - links to crab.plans with plan_type='timeshare_cycle'
```

Path, not subdomain. `stripe.com/atlas`, `github.com/actions` pattern.
Promote to `timeshare.crab.travel` later if the feature earns a subdomain.

**URL identifier is an opaque UUID, not a human slug.** Group names are not
globally unique ("tillo-family" collides for any of ~28M Tillos), and any
human-readable slug leaks identity into URLs that might get pasted into
unprotected chat threads. The full dashboard URL looks like
`crab.travel/timeshare/g/8f3a2b1e-7d24-4f1a-9c83-1ab0c2ef9e01/`, which
is hideous in a family text — so every outbound group URL (invite
emails, dashboard share) is passed through **crab's URL shortener**
(see §1.5) before sending. Recipient sees `crab.travel/s/h7k`.

### 1.2 Privacy — per-group, three layers

1. **Group-membership gate.** `@group_member_required(group_uuid)` decorator
   reads the UUID from the URL, looks up `crab.timeshare_groups.pk_id`, verifies
   `session['user'].id` is in `crab.timeshare_group_members` for that group
   with accepted status. Miss → **HTTP 404** (invisibility preferred
   over 403).

2. **robots.txt disallow.** Patch `app.py:328` to include
   `Disallow: /timeshare/g/` — the landing page at `/timeshare/` is
   indexable (it's marketing), but every group page is hidden.

3. **Page-level noindex.** Every group-scoped template overrides
   `meta_robots` to `noindex, nofollow`. The bare `/timeshare/` landing
   keeps default `index, follow`.

### 1.3 Default visibility

Every new group defaults to `is_public=FALSE`. No group ever appears in
sitemap or public directories. An owner can later opt into a "public
landing" for their group (e.g., to let friends request to join) — but
even then, dossier data stays member-only. MVP does not ship the public
listing — defer until someone asks.

### 1.4 Why path over subdomain (decision log)

- **Shared session:** same crab.travel cookie, no cross-origin auth dance
- **One App Engine service:** no additional deploy target, DNS, or SSL
- **SEO:** the public `/timeshare/` landing inherits `crab.travel`'s
  domain authority
- **Promotion path:** trivial to 301 → subdomain once the feature has
  retention data

### 1.5 URL shortener (Phase 0 prereq — port from kumori)

UUIDs in URLs are ugly. Every outbound link (invite emails, dashboard
shares, family texts) is passed through crab's URL shortener. We don't
have one in crab.travel yet — port the pattern from Andy's other apps:

| File in kumori                          | Becomes in crab_travel                  |
|-----------------------------------------|-----------------------------------------|
| `kumori/utilities/shorturl_utils.py`    | `crab_travel/utilities/shorturl_utils.py` |
| `kumori/blueprints/shorturl_bp.py`      | `crab_travel/shorturl_routes.py` (crab uses flat `*_routes.py`, not a `blueprints/` dir) |
| `kumori_short_urls` table               | `crab.short_urls` (lives in crab schema, not public) |

**Design carried over unchanged:**
- 30-char alphabet, skips ambiguous chars (`0/O`, `1/l`, `i`, `o`)
- Codes start at 2 chars (900 combos), grow to 3 when 80% full, cap at 8
- `/s/<short_code>` public redirect route
- `/api/shorten` auth-gated creation
- Click-count tracking (fire-and-forget UPDATE on redirect)

**One change for crab:** domain guard in `/api/shorten` switches from
`'kumori.ai' in long_url` to `'crab.travel' in long_url` — we only
shorten our own URLs, never arbitrary user-submitted links (opens
phishing vectors and we're not in the general-purpose shortener game).

The shortener is useful beyond timeshare — any feature emitting a
shareable link (plan invites, trip itineraries, watch-alert deep links)
picks it up for free. Hence Phase 0, not Phase 1: it ships before
timeshare routes touch production.

### 1.6 Schema deployment — kumori `ensure_table_exists()` pattern

No standalone migration tool. Every new DDL (the `crab.short_urls`
table, the 21 `crab.timeshare_*` tables, the 3 `crab.ii_*` tables, the
`ALTER crab.plans ADD timeshare_group_id`) goes into an `ensure_*()`
function called once at Flask app startup, matching the pattern Andy
already uses in kumori (`kumori/utilities/shorturl_utils.py::ensure_table_exists`).

All statements are idempotent `CREATE TABLE IF NOT EXISTS` / `CREATE
INDEX IF NOT EXISTS` / `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`.
Re-running the function on every deploy is safe and self-healing — a
fresh database bootstraps the full schema; an existing database skips
what's already there. No Alembic, no Django migrations, no separate
migration runner.

Locate the call alongside crab's existing `init_database()` invocation
in `postgres_schema.py`. Each new table gets its own
`_ensure_crab_short_urls()` / `_ensure_crab_timeshare_groups()` /
etc. function, composed into one top-level `init_timeshare_schema()`
called from app startup.

---

## 2. Data model (the big rewrite)

All 21 tables live **inside the existing `crab` schema with a `timeshare_`
prefix** on the shared kumori Cloud SQL Postgres — same instance that
hosts `crab`, `wattson`, `scatterbrain`, `dandy`, `inroads`, `consulting`,
etc. (`CRAB_POSTGRES_*` and `KUMORI_POSTGRES_*` secrets are aliases of
the same IP/DB/user.) This matches crab's existing internal subsystem-
prefix pattern: `crab.bot_runs`, `crab.member_watches`, `crab.trip_legs`,
`crab.llm_calls`, etc. all live alongside `crab.users`, `crab.plans`,
`crab.votes`. Adding `crab.timeshare_*` tables puts the timeshare
subsystem next to its peers. **No new schema needed.** Append the new
table definitions to `init_database()` in
`crab_travel/utilities/postgres_schema.py` after the existing block,
inside the `crab` schema.

The II public resort catalog goes into `crab` *without* the `timeshare_`
prefix (`crab.ii_regions`, `crab.ii_areas`, `crab.ii_resorts`) because
it's a general travel resource — future RCI, Marriott, or Hyatt
catalogs are natural peers (`crab.rci_resorts` etc.) and don't belong
to the timeshare feature alone.

### 2.1 Groups & membership

```sql
-- No new schema — all timeshare_* tables live inside the existing `crab`
-- schema (created at postgres_schema.py:21). Just append these CREATE
-- TABLE statements after the last existing crab.* table in init_database().

CREATE TABLE IF NOT EXISTS crab.timeshare_groups (
    pk_id SERIAL PRIMARY KEY,
    group_id UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),  -- used directly in URLs; no human slug
    name VARCHAR(200) NOT NULL,                 -- 'Tillo Family — Royal Sands' (display only, never in URL)
    created_by INTEGER NOT NULL REFERENCES crab.users(pk_id),
    is_public BOOLEAN DEFAULT FALSE,            -- always false at creation
    drive_folder_url TEXT,                      -- nullable: public Drive folder URL if the group uses Drive ingestion (no OAuth token stored — we only fetch public endpoints)
    status VARCHAR(20) DEFAULT 'active',        -- 'active' | 'archived' | 'deleted'
    settings JSONB DEFAULT '{}'::jsonb,         -- feature flags, UI prefs
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
-- group_id already has a UNIQUE constraint above, which creates an implicit btree index; no extra slug index needed.
CREATE INDEX IF NOT EXISTS idx_groups_created_by ON crab.timeshare_groups(created_by);

CREATE TABLE IF NOT EXISTS crab.timeshare_group_members (
    pk_id SERIAL PRIMARY KEY,
    group_id UUID NOT NULL REFERENCES crab.timeshare_groups(group_id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES crab.users(pk_id),  -- null until user accepts invite
    email VARCHAR(255) NOT NULL,
    role VARCHAR(20) DEFAULT 'family',          -- 'owner' | 'admin' | 'family' | 'readonly'
    invite_token VARCHAR(64) UNIQUE,
    invited_by INTEGER REFERENCES crab.users(pk_id),
    invited_at TIMESTAMPTZ DEFAULT NOW(),
    accepted_at TIMESTAMPTZ,
    -- Per-(user, group) prefs and rate-limit counters live inline here.
    -- Previously split into crab.timeshare_user_group_prefs; collapsed into this table
    -- because the relationship is strictly 1:1 with membership.
    chat_daily_count INTEGER DEFAULT 0,
    chat_daily_reset_at TIMESTAMPTZ DEFAULT NOW(),
    settings JSONB DEFAULT '{}'::jsonb,
    UNIQUE(group_id, email)
);
CREATE INDEX IF NOT EXISTS idx_gmembers_group ON crab.timeshare_group_members(group_id);
CREATE INDEX IF NOT EXISTS idx_gmembers_user ON crab.timeshare_group_members(user_id);
CREATE INDEX IF NOT EXISTS idx_gmembers_invite ON crab.timeshare_group_members(invite_token);
```

A user can belong to multiple groups (in-laws with a separate timeshare;
Andy could be a member of Luke's group if Luke ever had his own property).

### 2.2 Property, contract, people

```sql
-- A group may own or track multiple properties (e.g., two weeks at the same resort)
CREATE TABLE IF NOT EXISTS crab.timeshare_properties (
    pk_id SERIAL PRIMARY KEY,
    group_id UUID NOT NULL REFERENCES crab.timeshare_groups(group_id) ON DELETE CASCADE,
    name VARCHAR(300) NOT NULL,                 -- 'Royal Sands Cancún'
    developer VARCHAR(200),                     -- 'Royal Resorts'
    unit_number VARCHAR(50),                    -- 'K5133'
    unit_configuration VARCHAR(100),            -- '2BR lock-off — K5133R studio + K5133S 1BR'
    week_number INTEGER,                        -- 38
    usage_pattern VARCHAR(50),                  -- 'biennial_even' | 'biennial_odd' | 'annual' | 'float'
    trust_expiry_date DATE,                     -- 2050-01-24
    exchange_network VARCHAR(30),               -- 'interval_international' | 'rci' | null
    country VARCHAR(100),
    city VARCHAR(200),
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_props_group ON crab.timeshare_properties(group_id);

CREATE TABLE IF NOT EXISTS crab.timeshare_contracts (
    pk_id SERIAL PRIMARY KEY,
    property_id INTEGER NOT NULL REFERENCES crab.timeshare_properties(pk_id) ON DELETE CASCADE,
    contract_number VARCHAR(100),
    purchase_date DATE,
    purchase_price_usd NUMERIC(10,2),
    down_payment_usd NUMERIC(10,2),
    financing_terms TEXT,
    co_owners TEXT,                             -- 'Debra Tillo, Andrew Tillo' (free-text; people table is source of truth)
    contract_external_url VARCHAR(1000),        -- user's link to their own contract scan (Drive/Dropbox/etc); we never store the file
    notes TEXT,
    source_ingest_job_id INTEGER                -- provenance → crab.timeshare_ingest_jobs
);

-- People: group-scoped roster. Separate from group_members (which are crab.users with login).
-- A person may be a group_member too (linked via user_id), or just tracked as "someone who went on a trip."
CREATE TABLE IF NOT EXISTS crab.timeshare_people (
    pk_id SERIAL PRIMARY KEY,
    group_id UUID NOT NULL REFERENCES crab.timeshare_groups(group_id) ON DELETE CASCADE,
    full_name VARCHAR(200) NOT NULL,
    preferred_name VARCHAR(100),
    relationship VARCHAR(200),                  -- 'mother', 'brother', 'brother\'s wife', 'mother\'s brother'
    email VARCHAR(255),
    phone VARCHAR(50),
    birth_date DATE,
    user_id INTEGER REFERENCES crab.users(pk_id), -- set when this person logs in
    notes TEXT,
    source_ingest_job_id INTEGER,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_people_group ON crab.timeshare_people(group_id);
```

### 2.3 Financial history

```sql
CREATE TABLE IF NOT EXISTS crab.timeshare_maintenance_fees (
    pk_id SERIAL PRIMARY KEY,
    property_id INTEGER NOT NULL REFERENCES crab.timeshare_properties(pk_id) ON DELETE CASCADE,
    year INTEGER NOT NULL,
    billed_amount_usd NUMERIC(10,2),
    paid_amount_usd NUMERIC(10,2),
    billed_date DATE,
    paid_date DATE,
    late_fees_usd NUMERIC(10,2) DEFAULT 0,
    discount_usd NUMERIC(10,2) DEFAULT 0,
    components JSONB,                           -- {"asset_tax": 78, "hurricane_fund": 1, "base": 649}
    currency CHAR(3) DEFAULT 'USD',
    notes TEXT,
    source_ingest_job_id INTEGER,
    UNIQUE(property_id, year)
);

CREATE TABLE IF NOT EXISTS crab.timeshare_loan_payments (
    pk_id SERIAL PRIMARY KEY,
    contract_id INTEGER NOT NULL REFERENCES crab.timeshare_contracts(pk_id) ON DELETE CASCADE,
    payment_date DATE,
    amount_usd NUMERIC(10,2),
    principal_usd NUMERIC(10,2),
    interest_usd NUMERIC(10,2),
    balance_after_usd NUMERIC(10,2),
    method VARCHAR(50),                         -- 'visa_payoff' | 'check' | 'cash' | 'ach'
    notes TEXT,
    source_ingest_job_id INTEGER
);
```

### 2.4 Trips & exchanges

```sql
CREATE TABLE IF NOT EXISTS crab.timeshare_trips (
    pk_id SERIAL PRIMARY KEY,
    group_id UUID NOT NULL REFERENCES crab.timeshare_groups(group_id) ON DELETE CASCADE,
    property_id INTEGER REFERENCES crab.timeshare_properties(pk_id),  -- null if trip was an exchange away
    trip_date_start DATE,
    trip_date_end DATE,
    resort_name VARCHAR(300),
    resort_ii_code VARCHAR(10),                 -- FK to crab.ii_resorts(ii_code)
    resort_rci_code VARCHAR(10),                -- FK to crab.rci_resorts(rci_code) when we build it
    location VARCHAR(300),                      -- 'Big Island, HI' | 'Antigua'
    trip_type VARCHAR(30),                      -- 'home_week' | 'exchange' | 'bonus_stay' | 'rental' | 'purchase_trip'
    exchange_number VARCHAR(50),                -- II exchange confirmation #
    ii_member_confirmed BOOLEAN DEFAULT FALSE,
    rci_member_confirmed BOOLEAN DEFAULT FALSE,
    cost_usd NUMERIC(10,2),                     -- nullable
    notes TEXT,
    uncertainty_level VARCHAR(20) DEFAULT 'confirmed',  -- 'confirmed' | 'probable' | 'family_memory' | 'unverified'
    source_ingest_job_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_trips_group ON crab.timeshare_trips(group_id);
CREATE INDEX IF NOT EXISTS idx_trips_date ON crab.timeshare_trips(trip_date_start);

CREATE TABLE IF NOT EXISTS crab.timeshare_trip_participants (
    pk_id SERIAL PRIMARY KEY,
    trip_id INTEGER NOT NULL REFERENCES crab.timeshare_trips(pk_id) ON DELETE CASCADE,
    person_id INTEGER NOT NULL REFERENCES crab.timeshare_people(pk_id) ON DELETE CASCADE,
    UNIQUE(trip_id, person_id)
);

CREATE TABLE IF NOT EXISTS crab.timeshare_exchanges (
    pk_id SERIAL PRIMARY KEY,
    property_id INTEGER NOT NULL REFERENCES crab.timeshare_properties(pk_id) ON DELETE CASCADE,
    network VARCHAR(30),                        -- 'interval_international' | 'rci'
    deposit_date DATE,
    week_deposited INTEGER,
    exchange_date DATE,
    exchange_fee_usd NUMERIC(10,2),
    destination_resort VARCHAR(300),
    destination_resort_code VARCHAR(10),
    trip_id INTEGER REFERENCES crab.timeshare_trips(pk_id),
    status VARCHAR(30),                         -- 'pending' | 'completed' | 'canceled'
    notes TEXT,
    source_ingest_job_id INTEGER
);
```

### 2.5 Portals, contacts, documents, timeline

```sql
-- Portals: login credentials. Passwords go to Secret Manager, not this table.
CREATE TABLE IF NOT EXISTS crab.timeshare_portals (
    pk_id SERIAL PRIMARY KEY,
    group_id UUID NOT NULL REFERENCES crab.timeshare_groups(group_id) ON DELETE CASCADE,
    portal_name VARCHAR(200),                   -- 'Interval International'
    url VARCHAR(500),
    username VARCHAR(200),
    encrypted_password_ref VARCHAR(200),        -- Secret Manager secret name, or null if redacted
    member_number VARCHAR(100),
    support_phone VARCHAR(50),
    last_rotated DATE,
    notes TEXT,
    source_ingest_job_id INTEGER
);

CREATE TABLE IF NOT EXISTS crab.timeshare_contacts (
    pk_id SERIAL PRIMARY KEY,
    group_id UUID NOT NULL REFERENCES crab.timeshare_groups(group_id) ON DELETE CASCADE,
    full_name VARCHAR(200),
    role VARCHAR(200),                          -- 'Royal Resorts Owners Relations Mgr'
    organization VARCHAR(200),
    email VARCHAR(255),
    phone VARCHAR(50),
    last_contacted DATE,
    notes TEXT,
    source_ingest_job_id INTEGER
);

-- Document references: pointers to where the user keeps their docs.
-- crab.travel does NOT store the files. This table is a link registry
-- (Drive URL, Dropbox URL, user-supplied web URL) + metadata + a pointer
-- to the ingest job that extracted facts from the doc. Binary content
-- is never persisted here.
CREATE TABLE IF NOT EXISTS crab.timeshare_document_refs (
    pk_id SERIAL PRIMARY KEY,
    group_id UUID NOT NULL REFERENCES crab.timeshare_groups(group_id) ON DELETE CASCADE,
    doc_type VARCHAR(50),                       -- 'contract' | 'csf_statement' | 'exchange_confirm' | 'screenshot' | 'other'
    title VARCHAR(500),
    external_url VARCHAR(1000),                 -- Drive/Dropbox/user URL; NULL for one-shot uploads we processed-and-discarded
    external_provider VARCHAR(30),              -- 'google_drive' | 'dropbox' | 'user_url' | 'one_shot_upload'
    external_id VARCHAR(200),                   -- e.g. Drive file_id, for re-fetch on demand
    date_on_document DATE,
    related_property_id INTEGER REFERENCES crab.timeshare_properties(pk_id),
    related_trip_id INTEGER REFERENCES crab.timeshare_trips(pk_id),
    notes TEXT,
    source_ingest_job_id INTEGER,               -- which ingest job produced this reference + its facts
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_doc_refs_group ON crab.timeshare_document_refs(group_id);
CREATE INDEX IF NOT EXISTS idx_doc_refs_external ON crab.timeshare_document_refs(external_provider, external_id);

-- Timeline: catch-all event log for "X happened on Y date" that doesn't fit other tables
CREATE TABLE IF NOT EXISTS crab.timeshare_timeline_events (
    pk_id SERIAL PRIMARY KEY,
    group_id UUID NOT NULL REFERENCES crab.timeshare_groups(group_id) ON DELETE CASCADE,
    event_date DATE,
    event_type VARCHAR(50),                     -- 'email_sent' | 'email_received' | 'phone_call' | 'purchase' | 'decision' | 'note'
    title VARCHAR(500),
    description TEXT,
    related_person_id INTEGER REFERENCES crab.timeshare_people(pk_id),
    related_property_id INTEGER REFERENCES crab.timeshare_properties(pk_id),
    related_contact_id INTEGER REFERENCES crab.timeshare_contacts(pk_id),
    source_ingest_job_id INTEGER,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

### 2.6 Shared resort catalog (cross-group asset)

```sql
-- II public catalog — shared across all groups, scraped monthly
CREATE TABLE IF NOT EXISTS crab.ii_regions (
    pk_id SERIAL PRIMARY KEY,
    ii_code INTEGER UNIQUE NOT NULL,
    name VARCHAR(200) NOT NULL,
    scraped_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS crab.ii_areas (
    pk_id SERIAL PRIMARY KEY,
    region_id INTEGER REFERENCES crab.ii_regions(pk_id),
    ii_code INTEGER UNIQUE NOT NULL,
    name VARCHAR(300) NOT NULL,
    country VARCHAR(100),
    scraped_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS crab.ii_resorts (
    pk_id SERIAL PRIMARY KEY,
    area_id INTEGER REFERENCES crab.ii_areas(pk_id),
    ii_code VARCHAR(10) UNIQUE NOT NULL,
    name VARCHAR(300) NOT NULL,
    address TEXT,
    phone VARCHAR(50),
    website VARCHAR(500),
    nearest_airport VARCHAR(200),
    check_in_day VARCHAR(20),
    sleeping_capacity JSONB,
    tdi_score INTEGER,
    rating_overall DECIMAL(2,1),
    rating_services DECIMAL(2,1),
    rating_property DECIMAL(2,1),
    rating_accommodations DECIMAL(2,1),
    rating_experience DECIMAL(2,1),
    rating_response_count INTEGER,
    description TEXT,
    amenities JSONB,
    photo_urls JSONB,
    map_lat DECIMAL(9,6),
    map_lng DECIMAL(9,6),
    scraped_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_ii_resort_area ON crab.ii_resorts(area_id);
CREATE INDEX IF NOT EXISTS idx_ii_resort_rating ON crab.ii_resorts(rating_overall);

-- RCI placeholder — identical shape, deferred until a non-II group shows up
-- CREATE TABLE crab.rci_regions ...
-- CREATE TABLE crab.rci_areas ...
-- CREATE TABLE crab.rci_resorts ...

-- Per-group shortlist (this group likes these resorts for consideration)
CREATE TABLE IF NOT EXISTS crab.timeshare_group_shortlist (
    pk_id SERIAL PRIMARY KEY,
    group_id UUID NOT NULL REFERENCES crab.timeshare_groups(group_id) ON DELETE CASCADE,
    network VARCHAR(30),                        -- 'interval_international' | 'rci'
    resort_code VARCHAR(10),
    added_by INTEGER REFERENCES crab.users(pk_id),
    notes TEXT,
    priority INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(group_id, network, resort_code)
);
```

### 2.7 Ingestion pipeline

```sql
CREATE TABLE IF NOT EXISTS crab.timeshare_ingest_jobs (
    pk_id SERIAL PRIMARY KEY,
    group_id UUID NOT NULL REFERENCES crab.timeshare_groups(group_id) ON DELETE CASCADE,
    source_type VARCHAR(30),                    -- 'google_doc' | 'google_sheet' | 'pdf_upload' | 'text_paste' | 'email_forward' | 'form_fill'
    source_ref VARCHAR(500),                    -- drive file id OR gcs path OR null for paste
    source_snapshot_hash VARCHAR(64),           -- sha256 of content; skip re-runs
    source_content TEXT,                        -- raw paste or extracted text for audit
    status VARCHAR(30),                         -- 'pending' | 'extracting' | 'review' | 'committed' | 'rejected' | 'error'
    extracted_facts JSONB,                      -- Claude's proposed rows, grouped by target table: {"maintenance_fees": [{year: 2018, amount: 1093, ...}, ...], "trips": [...]}
    review_notes TEXT,
    rejected_rows JSONB,                        -- what the reviewer threw out, for retraining
    created_by INTEGER REFERENCES crab.users(pk_id),
    committed_by INTEGER REFERENCES crab.users(pk_id),
    committed_at TIMESTAMPTZ,
    claude_cost_usd NUMERIC(8,5),
    error_message TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ingest_group ON crab.timeshare_ingest_jobs(group_id);
CREATE INDEX IF NOT EXISTS idx_ingest_status ON crab.timeshare_ingest_jobs(status, created_at);
```

**Provenance convention:** every fact-table row carries
`source_ingest_job_id` (already defined in the schemas above). `NULL`
means manual entry; an integer links back to the exact ingest job that
produced the row. Enables "show me everything we extracted from this
PDF" queries for review and audit.

### 2.8 Chatbot & audit

```sql
CREATE TABLE IF NOT EXISTS crab.timeshare_chat_conversations (
    pk_id SERIAL PRIMARY KEY,
    group_id UUID NOT NULL REFERENCES crab.timeshare_groups(group_id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES crab.users(pk_id),
    title VARCHAR(500),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS crab.timeshare_chat_messages (
    pk_id SERIAL PRIMARY KEY,
    conversation_id INTEGER REFERENCES crab.timeshare_chat_conversations(pk_id) ON DELETE CASCADE,
    role VARCHAR(20),
    content TEXT,
    model VARCHAR(100),
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd NUMERIC(8,5),
    cited_fact_refs JSONB,                      -- [{"table": "maintenance_fees", "pk_id": 42}, ...]
    tool_calls JSONB,                           -- transparency: which scoped tools Claude called, with args
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_chat_conv ON crab.timeshare_chat_messages(conversation_id);

CREATE TABLE IF NOT EXISTS crab.timeshare_audit_log (
    pk_id SERIAL PRIMARY KEY,
    group_id UUID REFERENCES crab.timeshare_groups(group_id) ON DELETE SET NULL,
    user_id INTEGER REFERENCES crab.users(pk_id) ON DELETE SET NULL,
    action VARCHAR(100),
    resource_type VARCHAR(50),
    resource_id VARCHAR(200),
    metadata JSONB,
    ip_address VARCHAR(45),
    user_agent TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_group ON crab.timeshare_audit_log(group_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON crab.timeshare_audit_log(action);
```

### 2.9 Total table count

21 tables (2 group core + 4 property-contract-people-contract-addenda +
2 financial + 3 trip/exchange + 4 portals-contacts-docs-timeline +
3 II catalog + 1 shortlist + 1 ingest + 2 chat + 1 audit). Membership
prefs are columns on `group_members`, not a separate table.
Comparable to `crab`'s own 29 tables in the shared DB; `crab` is 907 MB
today (38% of the ~2.4 GB total). Timeshare is expected to add <50 MB
in its first year.

---

## 3. Access control

### 3.1 Decorators

New file: `utilities/timeshare_access.py`

```python
from functools import wraps
from flask import session, abort, request

from route_helpers import login_required
from utilities.postgres_utils import get_db_connection


def _get_membership(group_uuid, user_id):
    """Returns (group_id, role) or None if user is not a member of this group.
    Accepts the UUID straight from the URL as an opaque string — Postgres
    casts VARCHAR→UUID on the parameter. Invalid UUID format = no match = 404."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT g.group_id, gm.role
              FROM crab.timeshare_groups g
              JOIN crab.timeshare_group_members gm ON gm.group_id = g.group_id
             WHERE g.group_id = %s::uuid
               AND gm.user_id = %s
               AND gm.accepted_at IS NOT NULL
               AND g.status = 'active'
        """, (group_uuid, user_id))
        return cur.fetchone()
    except Exception:
        # Malformed UUID raises — treat as "not a member" so we 404 instead of 500
        return None
    finally:
        conn.close()


def group_member_required(min_role='family'):
    """Gates a route on group membership. Role hierarchy:
    owner > admin > family > readonly. Miss → 404."""
    role_rank = {'readonly': 1, 'family': 2, 'admin': 3, 'owner': 4}
    required = role_rank[min_role]

    def decorator(f):
        @wraps(f)
        @login_required
        def decorated(group_uuid, *args, **kwargs):
            user = session.get('user') or {}
            membership = _get_membership(group_uuid, user.get('id'))
            if not membership:
                abort(404)
            group_id, role = membership
            if role_rank.get(role, 0) < required:
                abort(404)
            # Attach to request context for the view
            request.timeshare_group_id = group_id
            request.timeshare_role = role
            return f(group_uuid, *args, **kwargs)
        return decorated
    return decorator
```

Usage:

```python
@bp.route('/g/<group_uuid>/')
@group_member_required()           # any member
def dashboard(group_uuid): ...

@bp.route('/g/<group_uuid>/members/')
@group_member_required('admin')    # admin or owner
def manage_members(group_uuid): ...

@bp.route('/g/<group_uuid>/danger/delete')
@group_member_required('owner')    # owner only
def delete_group(group_uuid): ...
```

### 3.2 Server-to-server (bearer)

Unchanged from v2 — reuses crab's existing `bearer_auth_required` at
`route_helpers.py:42`. One shared secret `CRAB_TIMESHARE_BEARER_TOKEN`
used by the II scraper writeback. Not group-scoped; the writeback
populates the shared `crab.ii_*` catalog tables, not any group's
private data.

### 3.3 Group creation rate-limit

Any crab user can create groups, but rate-limit: **3 groups per user per
day** via a check on `crab.timeshare_groups.created_by` + `created_at`. Hard
cap at 10 groups per user lifetime on the MVP — raised per request.
Prevents a compromised account from spawning thousands of empty groups.

---

## 4. Routes

New file: `/Users/at/Desktop/code/crab_travel/timeshare_routes.py`
following the existing `*_routes.py` convention at repo root.

```python
from flask import Blueprint, render_template, session, request, jsonify, redirect, abort
from route_helpers import login_required, api_auth_required, bearer_auth_required
from utilities.timeshare_access import group_member_required

bp = Blueprint('timeshare', __name__, url_prefix='/timeshare')
```

Route list:

```
# Public-ish landing (indexable; explains the feature)
GET  /timeshare/                              landing page + CTA to create group

# Group lifecycle
GET  /timeshare/groups/new                    creation form (login required)
POST /timeshare/groups/new                    create + auto-add creator as owner
GET  /timeshare/g/<group_uuid>/                     dashboard (member only)
GET  /timeshare/g/<group_uuid>/settings             group-level settings (admin+)
POST /timeshare/g/<group_uuid>/settings             update (owner only for delete/public toggle)

# Members
GET  /timeshare/g/<group_uuid>/members              list + invite UI
POST /timeshare/g/<group_uuid>/members/invite       create invite row + send email
GET  /timeshare/g/<group_uuid>/members/accept/<token>  accept (login required)
POST /timeshare/g/<group_uuid>/members/<member_id>/remove  remove member (admin+)

# Structured data views
GET  /timeshare/g/<group_uuid>/property             property + contract view
GET  /timeshare/g/<group_uuid>/finances             CSF timeline + loan history + totals
GET  /timeshare/g/<group_uuid>/trips                trip history with participants
GET  /timeshare/g/<group_uuid>/people               people directory
GET  /timeshare/g/<group_uuid>/portals              logins (passwords redacted inline)
GET  /timeshare/g/<group_uuid>/contacts             contacts directory
GET  /timeshare/g/<group_uuid>/documents            uploaded PDFs gallery
GET  /timeshare/g/<group_uuid>/timeline             chronological event log

# Edit views (inline forms — no separate edit pages)
POST /timeshare/g/<group_uuid>/fact/<table>/<pk>    update a fact row
POST /timeshare/g/<group_uuid>/fact/<table>/new     insert a fact row

# Ingestion
GET  /timeshare/g/<group_uuid>/ingest               wizard landing (paste / upload / drive)
POST /timeshare/g/<group_uuid>/ingest/paste         submit text paste → Claude extract
POST /timeshare/g/<group_uuid>/ingest/upload        upload PDF → Claude extract
POST /timeshare/g/<group_uuid>/ingest/drive/submit   user pastes public Drive folder/file URL (no OAuth)
POST /timeshare/g/<group_uuid>/ingest/drive/scan     re-scan the previously submitted folder URL
GET  /timeshare/g/<group_uuid>/ingest/jobs          list of ingest jobs
GET  /timeshare/g/<group_uuid>/ingest/jobs/<pk>     single job review UI (accept/reject rows)
POST /timeshare/g/<group_uuid>/ingest/jobs/<pk>/commit   commit approved rows to fact tables
POST /timeshare/g/<group_uuid>/ingest/jobs/<pk>/reject   reject job

# II catalog (shared; no group_uuid because catalog spans all groups)
GET  /timeshare/catalog                       region grid (public, indexable? — see §5)
GET  /timeshare/catalog/region/<ii_code>      areas in region
GET  /timeshare/catalog/resort/<ii_code>      resort detail

# Per-group shortlist (on top of the shared catalog)
GET  /timeshare/g/<group_uuid>/catalog              group-framed view of catalog with shortlist toggles
POST /timeshare/g/<group_uuid>/shortlist/toggle     add/remove resort from group_shortlist

# Chatbot (per-group)
GET  /timeshare/g/<group_uuid>/ask                  chatbot UI
POST /timeshare/g/<group_uuid>/api/chat/stream      SSE streaming answer with tool use

# Cycle planning — bridges to crab.plans
GET  /timeshare/g/<group_uuid>/cycles               list of timeshare_cycle plans for this group
POST /timeshare/g/<group_uuid>/cycles/new           create a new crab.plans row with plan_type='timeshare_cycle', group_id FK
GET  /timeshare/g/<group_uuid>/cycles/<cycle_id>    redirect to /plan/<crab_plan_id>

# Admin (per-group, not site-wide)
GET  /timeshare/g/<group_uuid>/audit                audit log viewer (admin+)

# Server-to-server (bearer)
POST /timeshare/api/ii-catalog-sync           VPS scraper writeback
POST /tasks/timeshare-kick-ii-scrape          cron kicks VPS scrape
```

### 4.1 Blueprint registration

`app.py` after line 754:

```python
from timeshare_routes import bp as timeshare_bp
app.register_blueprint(timeshare_bp)
```

---

## 5. The ingestion pipeline (new; centerpiece of v3)

### 5.1 Why this replaces the Drive-mirror

- Structured facts answer comparative/aggregated questions. Blob text
  does not.
- Each group has its own doc structure — one universal renderer can't
  handle them all without becoming a Jinja soup.
- Chatbot answer quality depends on clean facts, not paragraphs.
- Users need to correct facts they notice wrong — editing a Google Doc
  cell has no feedback loop back to the UI; editing a `maintenance_fees`
  row does.

### 5.2 Ingestion sources (four types)

| Source | How it works | What we persist |
|---|---|---|
| **Text paste** | User pastes raw text (an email thread, a phone-call note). Claude extracts facts, user reviews, commits. Fastest to MVP. | Text stays in `ingest_jobs.source_content` (small, for re-review). No `document_refs` row unless user supplies a URL. |
| **PDF upload** | User uploads a PDF (a CSF statement, exchange confirmation, contract). Backend runs `pdfplumber` → text → Claude → facts. **Binary is discarded after processing.** | Extracted text in `ingest_jobs.source_content` + extracted facts in structured tables. A `document_refs` row with `external_provider='one_shot_upload'` and `external_url=NULL` so audit can see "a PDF named X.pdf was processed on DATE." The user keeps the original file on their machine. |
| **Google Doc / Sheet (public link)** | User flips the doc/folder to "anyone with the link — viewer" in Drive and pastes the URL. Crab fetches the unauthenticated Drive export endpoint (no OAuth, no Picker, no scope review), extracts facts. **Drive file is never downloaded to our storage.** | Extracted text in `ingest_jobs.source_content` + `document_refs` row with the Drive URL + file_id + facts. User re-ingests anytime the Drive doc changes. Tradeoff: user must make the file publicly readable — fine for MVP (Andy is the only user, his dossier is already shared inside the family anyway, and the UUID-gated crab dossier never surfaces the Drive URL). |
| **User URL (Dropbox / any)** | User supplies a public or signed URL to a doc they host. We fetch once, extract, forget the bytes. | Same as Drive path — link kept, bytes discarded. |
| **Form fill** | Manual entry directly via the UI. Creates an `ingest_job` with `source_type='form_fill'` for provenance consistency. | No document at all — facts committed directly. |

All five paths write a single `crab.timeshare_ingest_jobs` row; the review UI
is identical across sources. **No path produces a file stored by crab.travel.**

### 5.3 The Claude extraction contract

Extraction runs via Anthropic SDK with **tool use** — Claude is given a
tool per target fact table. Example system prompt (simplified):

```
You are extracting structured timeshare facts from user-provided content.
The user's group tracks a timeshare at: {property.name}, unit {property.unit_number},
week {property.week_number}, owned since {contract.purchase_date or 'unknown'}.

Extract ONLY facts explicitly stated in the content. Do not infer or guess.
For each fact, call the appropriate tool. Uncertainty should be flagged
by setting uncertainty_level='probable' or 'family_memory'.

Tools available:
- record_maintenance_fee(year, billed_amount_usd, paid_date, late_fees_usd?, notes?)
- record_loan_payment(date, amount_usd, principal_usd?, balance_after_usd?)
- record_trip(date_start, date_end, resort_name, location?, trip_type, participants_names, exchange_number?)
- record_person(full_name, relationship?, email?, phone?, notes?)
- record_portal_login(name, url, username?, member_number?, support_phone?)
- record_contact(name, role?, org?, email?, phone?)
- record_timeline_event(date, type, title, description?)
- record_document_reference(title, doc_type)

If content contains no extractable facts, return a single
'no_facts_extracted' tool call with a reason.
```

Each tool call appends to `extracted_facts` JSONB keyed by target table.
Facts don't hit fact tables until a user reviews and clicks "commit"
in the review UI. Rejected rows get preserved in `rejected_rows` JSONB
for future model improvement.

### 5.4 Review UI

Per ingest job, review page shows proposed rows grouped by target
table with three actions per row: **accept**, **edit-then-accept**,
**reject**. "Accept all" button for trusted sources. Committed rows
land in fact tables with `source_ingest_job_id = this.pk_id`.

### 5.5 Cost model

Claude Sonnet 4.5 at $3/M input + $15/M output. A CSF statement PDF
(~3 pages ≈ 6KB text) + tool-use response ≈ $0.05 per ingest job.
Full initial-load of the Tillo dossier (20 docs/PDFs) ≈ $1. Ongoing
monthly ingest of new bills/emails for an active group: under $2/mo.

### 5.6 Provenance via links, not copies

Every row in every fact table has `source_ingest_job_id`. The review
UI and audit log can always answer "where did we get this number?"
One `ingest_job` row points at its source:
- **Drive/URL ingests:** `source_ref` holds the external URL/file_id,
  and `document_refs` has a row you can click through to the user's
  live copy
- **Text paste:** `source_content` holds the pasted text verbatim
- **PDF upload (one-shot):** `source_content` holds the extracted text;
  the binary is gone, but the user still has it on their machine

Every committed fact points back to the job. Full chain of custody —
without crab.travel being the file store. Chatbot citations link to the
user's own Drive URL (when applicable) so they land in their own
content, not ours. **If a user's Drive link ever breaks** (file moved
or deleted), the extracted facts survive as the canonical record and
the `document_refs` row shows as "link broken — facts were extracted
on DATE from `[title]`." That's the tradeoff we accept for not hosting
the file ourselves.

---

## 6. Chatbot — scoped tool use, not context stuffing

### 6.1 Why tool use

v2 stuffed ~50KB of dossier text into system prompts. v3's structured
data is JSON; stuffing all of it scales poorly and invites
hallucination ("what was the 2019 CSF?" → Claude invents one).

v3 defines a small set of read-only tools scoped to the current group.
Claude calls tools to fetch facts on demand, then synthesizes the
answer. Every answer has provable citations via tool-call logs.

### 6.2 Tool set (per-group, read-only)

```python
# In utilities/timeshare_chat_tools.py
TOOLS = [
    {
        "name": "get_maintenance_fees",
        "description": "Return maintenance fee (CSF) rows for this group's properties, optionally filtered by year range.",
        "input_schema": {
            "type": "object",
            "properties": {
                "year_start": {"type": "integer"},
                "year_end": {"type": "integer"},
            },
        },
    },
    {"name": "get_trips", "description": "...", "input_schema": {...}},
    {"name": "get_people", "description": "Return people in this group's roster, optional name filter.", "input_schema": {...}},
    {"name": "get_property", "description": "Return the group's property + contract info.", "input_schema": {}},
    {"name": "get_exchanges", "description": "...", "input_schema": {...}},
    {"name": "search_resort_catalog", "description": "Search the II public resort catalog by location/rating/sleeping capacity.", "input_schema": {...}},
    {"name": "get_shortlist", "description": "Return this group's current resort shortlist.", "input_schema": {}},
    {"name": "get_portals", "description": "Return portal logins (passwords always redacted).", "input_schema": {}},
    {"name": "get_contacts", "description": "Return the group's contacts.", "input_schema": {}},
    {"name": "get_timeline", "description": "Return timeline events, optionally filtered by date range or type.", "input_schema": {...}},
    {"name": "search_document_refs", "description": "Search the group's document link registry by title/type/year. Returns metadata + external URLs (Drive/Dropbox/etc) — not file contents. Fact-level data already lives in the structured fact tables (maintenance_fees, trips, etc.).", "input_schema": {...}},
]
```

Each tool's handler runs a single SQL query scoped to `group_id`
(binding prevents cross-group leaks). Never executes user-provided SQL.

### 6.3 System prompt

```
You are the assistant for the "{group.name}" group's timeshare records.
Current user: {user.email}. Today: {today}.

RULES:
- Answer only from tool results. Never invent dates, amounts, or names.
- Call the appropriate tools to find facts before answering. If tools
  return empty, say so explicitly: "I don't have that in the records."
- When stating a fact, include a citation like:
  {"fact_ref": {"table": "maintenance_fees", "pk_id": 42}}
  in a structured block Claude's caller parses out.
- Never return raw passwords. Portals tool already redacts them.
- Refer to people by first name.
- Format currency with $ and commas; format dates as "Month DD, YYYY."
```

### 6.4 Cost & caps

- Sonnet 4.5 default; ~8 tool calls per complex question
- Estimated cost: ~$0.02 per conversation turn
- Per-user daily cap: 100 messages (checked against
  `chat_daily_count` on the user's `crab.timeshare_group_members` row —
  actually wait, user_prefs isn't in v3 schema yet, add it)
- Admin kill switch at group level: toggleable flag on
  `crab.timeshare_groups.settings.chat_enabled`

### 6.5 Streaming

HTMX + SSE at `/timeshare/g/<group_uuid>/api/chat/stream`, same pattern as
crab's existing streaming endpoints.

### 6.6 Rate-limit + per-member prefs location

Chat rate-limit counters and per-member settings live on
`crab.timeshare_group_members` itself (see §2.1 — `chat_daily_count`,
`chat_daily_reset_at`, `settings JSONB`). No separate `user_group_prefs`
table needed — membership IS the per-(user, group) row, and adding
columns there beats a 1:1 join.

---

## 7. II catalog scraper (unchanged from v2)

Runs on OpenCrab VPS via bearer-authed kick. ~3h monthly. Writes to
shared `crab.ii_regions/ii_areas/ii_resorts`. All groups benefit
from one scrape.

### 7.1 Cron additions (cron.yaml)

```yaml
- description: "Timeshare — monthly II catalog refresh kick"
  url: /tasks/timeshare-kick-ii-scrape
  schedule: 1 of month 03:00
  timezone: America/Los_Angeles
```

No Drive-sync cron in v3 — Drive ingestion is user-initiated, not
scheduled. (A group owner clicks "scan my Drive folder" when they want
to re-ingest.)

---

## 8. Cross-wiring with crab.plans

A timeshare **cycle** (biennial use-year, annual for non-biennial
properties) is still a `crab.plans` row, now with:

- `plan_type = 'timeshare_cycle'`
- New nullable FK column: `timeshare_group_id UUID REFERENCES crab.timeshare_groups(group_id)`

```sql
ALTER TABLE crab.plans ADD COLUMN IF NOT EXISTS timeshare_group_id UUID REFERENCES crab.timeshare_groups(group_id);
CREATE INDEX IF NOT EXISTS idx_plans_timeshare_group ON crab.plans(timeshare_group_id);
```

Existing crab primitives (members, preferences, voting, availability,
blackouts, messages, destination_suggestions, trip_legs, transport_options)
work inside timeshare_cycle plans exactly like any other plan. The
destination picker filters to II catalog resorts when the plan has a
`timeshare_group_id`.

OpenCrab VPS hunts flights/hotels/transfers for timeshare_cycle plans
the same way it hunts for any plan. It never sees `/timeshare/g/*`
routes directly.

---

## 9. Templates

### 9.1 Inheritance

```
templates/timeshare/
├── base_group.html                # extends base.html; noindex; group sub-nav
├── landing.html                   # /timeshare/ landing (indexable)
├── group_new.html                 # creation form
├── dashboard.html
├── settings.html
├── members.html
├── invite_accept.html             # public; via invite token
├── fact_views/
│   ├── property.html
│   ├── finances.html
│   ├── trips.html
│   ├── people.html
│   ├── portals.html
│   ├── contacts.html
│   ├── documents.html
│   └── timeline.html
├── ingest/
│   ├── wizard.html
│   ├── job_list.html
│   └── job_review.html
├── catalog/
│   ├── regions.html
│   ├── region.html
│   └── resort.html
├── chat.html
├── cycles_list.html
└── audit.html
```

### 9.2 Main crab nav addition

In `templates/base.html`, inside the logged-in block at lines 311-316:

```jinja
{% if my_timeshare_groups %}
  {% if my_timeshare_groups|length == 1 %}
    <a href="/timeshare/g/{{ my_timeshare_groups[0].group_id }}/">{{ my_timeshare_groups[0].name }}</a>
  {% else %}
    <a href="/timeshare/">Timeshare</a>
  {% endif %}
{% endif %}
```

Context processor exposes `my_timeshare_groups` to every template by
querying `crab.timeshare_group_members` for `session['user'].id`.

---

## 10. Rollout

| Phase | Estimate | What ships | Customer value |
|---|---|---|---|
| **0 — URL shortener (prereq)** | 0.25d | Port `kumori/utilities/shorturl_utils.py` + `kumori/blueprints/shorturl_bp.py` → `crab_travel/utilities/shorturl_utils.py` + `crab_travel/shorturl_routes.py`. New table `crab.short_urls`. `/s/<code>` redirect + `/api/shorten` (authed). Domain guard: crab.travel only. | Reusable primitive; every Phase-1 invite email sends a `crab.travel/s/xxx` link instead of a raw UUID. Available to any future crab feature emitting share links. |
| **1 — Skeleton + groups** | 0.5d | `timeshare_routes.py`, group create/invite/accept, member mgmt, empty dashboard, robots patch, 404-on-miss. URLs use `group_id` UUID directly; outbound links shortened via Phase 0. | Andy can create "Tillo Family" group, invite Mom (she gets a short link, not a UUID). |
| **2 — Structured schema + form-fill** | 1d | All 21 tables (including `document_refs` link registry — no GCS bucket provisioned, no `storage_path` column) + all "fact view" pages with inline edit forms. Manual entry wizard. | Andy can type in property, contract, known CSF history, known trips, people, portals. Dossier = structured rows, not docs. |
| **3 — Ingestion (paste + PDF)** | 1d | `ingest_jobs` table, Claude tool-use extraction, review UI, commit/reject flow. Paste & PDF-upload-process-and-discard first. **Synchronous request — no background queue.** App Engine's 10-min request timeout is accepted for MVP (Andy is the only user and most docs fit inside the budget). Revisit if a real user hits it. | Andy pastes Royal Resorts' Apr 10 CSF statement → Claude extracts → Andy clicks commit → 2006–2024 CSF history populated in 30 seconds. The original PDF bytes are never written to any crab-managed storage. |
| **4 — Drive (public-link ingest)** | 0.25d | User pastes a **public Drive folder URL** (anyone-with-link-can-view). Crab fetches the folder listing + each file's export endpoint via the unauthenticated Drive export URL, runs extraction, stores `document_refs` rows with the Drive URL. **No OAuth, no Drive Picker, no app-verification review.** Tradeoff: folder is technically public to anyone who has the link, but the UUID-gated dossier never exposes the link. | Andy flips his dossier folder to "anyone with link, viewer", pastes the URL once, re-ingests 9 Docs + 1 Sheet into structured form. Originals stay in Drive. |
| **5 — Chatbot (tool use)** | 0.5d | Scoped tools, SSE streaming, citations, cost tracking, daily cap. | "Mom, what did we pay in 2018?" → "$1,093, paid September 22 2018 [📄 CSF statement]." |
| **6 — II catalog** | 0.5d | VPS scraper, catalog tables, resort detail pages, group shortlist. | Andy browses 10K II resorts by rating/capacity, shortlists for 2026 cycle. |
| **7 — Cycle plans bridge** | 0.3d | `crab.plans.timeshare_group_id` FK, "create 2026 cycle" flow, destination picker integration. | 2026 Week 38 cycle becomes a crab plan; family gets voting, availability, flight hunting. |

**Total: ~4.3 working days.** Tillo is fully ported + live by end of
Phase 4. Phases 5–7 compound value but aren't blocking.

---

## 11. Files touched

### 11.1 New

```
crab_travel/utilities/shorturl_utils.py          # Phase 0: ported from kumori/utilities/shorturl_utils.py
crab_travel/shorturl_routes.py                   # Phase 0: ported from kumori/blueprints/shorturl_bp.py (flat *_routes.py convention)
crab_travel/timeshare_routes.py
crab_travel/utilities/timeshare_access.py
crab_travel/utilities/timeshare_ingest.py        # Claude tool-use extraction
crab_travel/utilities/timeshare_chat_tools.py    # Scoped chatbot tools
crab_travel/utilities/timeshare_drive.py         # Drive API client for group-scoped sync
crab_travel/utilities/timeshare_ii_scraper.py    # VPS-side (runs off App Engine)
crab_travel/scripts/timeshare_seed_tillo.py      # One-off: create Tillo group + seed known facts
crab_travel/templates/timeshare/*                # Per §9.1
```

### 11.2 Edited

```
crab_travel/app.py                               # Register timeshare + shorturl blueprints; context processor; robots patch
crab_travel/cron.yaml                            # +1 entry for II scraper kick
crab_travel/smoke_test.py                        # Timeshare route + query-count assertions + /s/<code> redirect
crab_travel/templates/base.html                  # Conditional nav link (line ~316)
crab_travel/utilities/postgres_schema.py         # Append crab.short_urls (Phase 0) + 21 crab.timeshare_* + 3 crab.ii_* CREATE TABLE blocks inside the existing crab schema + ALTER crab.plans for timeshare_group_id FK
```

### 11.3 Key existing primitives reused (no edits)

| File | What we lean on |
|---|---|
| `route_helpers.py:23` | `login_required` |
| `route_helpers.py:42` | `bearer_auth_required` |
| `app.py:55-70` | Google OAuth base (used for user login only — no Drive scope added; public-link ingestion is unauthenticated) |
| `auth_routes.py:100-108` | `session['user']` shape |
| `postgres_utils.py:24-28` | Cloud SQL connection |
| `postgres_schema.py:77-97` | `crab.plans` — add `plan_type='timeshare_cycle'` + `timeshare_group_id` FK |
| `postgres_schema.py:111-510` | All crab.* tables used when a timeshare_cycle plan is active |
| `utilities/claude_utils.py` | Claude SDK wrapper; `log_api_usage` with `feature='timeshare_ingest'` and `feature='timeshare_chat'` |
| `utilities/search_engine.py`, `deals_engine.py`, `watch_engine.py` | Multi-modal transport hunting inherited by timeshare_cycle plans |
| `opencrab_routes.py` | Bearer-auth + rate-limit pattern template for II writeback |
| `templates/base.html` | Design system, glass nav, user menu |
| `cron.yaml` | Cron pattern |

---

## 12. Risks, open questions

### 12.1 Risks

1. **Extraction quality.** Claude tool-use extraction occasionally
   mis-types a field (year as string, amount as string-with-$, date in
   wrong format). Mitigation: server-side validation on each tool call
   before storing in `extracted_facts`; review UI highlights type
   mismatches; user always has a "reject" option.

2. **Cross-group leak via chatbot tool.** A bug in a chatbot tool
   handler that forgets to bind `group_id` leaks another group's facts
   to this user. Mitigation: every tool handler starts with
   `group_id = request.timeshare_group_id` (set by the decorator) and
   binds it as the first query parameter. Unit test every tool with a
   two-group fixture.

3. **Invite link leak.** Invite tokens in `crab.timeshare_group_members.invite_token`
   travel in emails — if forwarded, a non-intended person can join. Mitigation:
   invite tokens are single-use (set `accepted_at` + rotate token on accept),
   expire after 14 days, and acceptance requires matching email at OAuth login.

4. **Search-engine leak.** A new route added without the
   `group_member_required` decorator becomes publicly visible.
   Mitigation: `smoke_test.py` iterates every URL rule with
   `/timeshare/g/` in the pattern and asserts 404 for unauthenticated
   + 404 for logged-in-but-non-member.

5. ~~**Drive OAuth scope.**~~ **Resolved 2026-04-21** — MVP drops Drive OAuth
   entirely. Users flip the file/folder to "anyone with link — viewer" and
   paste the URL; crab fetches the public export endpoint. No OAuth, no
   Picker, no app-verification review, no consent screen. Revisit only if
   a user needs to ingest a file they can't make publicly readable.

6. **Broken external links over time.** `crab.timeshare_document_refs.external_url`
   points at the user's Drive/Dropbox/user-hosted copy. If they move or
   delete the file, the link breaks. Our extracted facts survive as the
   canonical record — that's the intended tradeoff for not hosting
   files. Mitigation: periodic link-health checks that mark
   `document_refs` rows as `link_status='broken'`, surface in admin UI
   so users can re-supply the URL. Low priority — not blocking.

### 12.2 Resolved from v2

- Cloud SQL instance: shared kumori Postgres (`CRAB_POSTGRES_*` and
  `KUMORI_POSTGRES_*` are aliases — same IP `34.30.206.215`, same DB
  `postgres`, same user). Verified live Apr 20 2026. Timeshare tables
  live inside the existing `crab` schema with `timeshare_` prefix
  (`crab.timeshare_groups`, etc.) — matches crab's own internal
  subsystem-prefix convention. II catalog tables are `crab.ii_*` with
  no timeshare prefix (shared travel resource).
- Session shape: `{id, email, name, picture}`, lowercase email.
- **Document storage posture:** crab.travel does not store user files.
  Uploaded PDFs are processed-and-discarded; Drive docs are read via API
  on-demand for extraction but never mirrored; `document_refs` is a
  link registry, not a file cabinet. Decision made 2026-04-21.

### 12.3 Explicitly deferred (day 1000 problems — ship before fix)

The following were raised, discussed, and **deliberately deferred** in the
2026-04-21 planning session. Andy's rule: *"we have no customers and you
have all my docs — don't over-engineer for the N+1 case."* Revisit when a
real user hits one.

- **Mom's first-time login UX.** Onboarding flow for a family member who
  has never logged into crab.travel is a deploy/UX problem, not a
  pre-build problem. Ship with the existing `login_required` redirect.
  Polish later.
- **Async ingestion queue.** App Engine's 10-min request timeout accepted.
  Ingestion runs synchronously in the request. Revisit when a user uploads
  a document that exceeds the budget.
- **Claude cost cap per group.** §5.5 has a cost estimate; hard daily cap
  gets wired using the same pattern as Andy's other apps
  (`utilities/claude_utils.py:log_api_usage` with preflight check).
  Number picked the day we start seeing real usage.
- **Soft-delete elaboration.** Every fact table gets a `status VARCHAR(20)
  DEFAULT 'active'` column. Hard-delete paths are Phase 8+.
- **Multi-group chatbot-tool leak test.** §12.1 risk #2 has the mitigation
  (every tool binds `group_id` as first parameter). Two-group fixture test
  gets written when onboarding customer #2, not before.

### 12.4 Still open (real pre-build questions)

- **Password storage for portals.** `encrypted_password_ref` points at
  a Secret Manager secret. Who can read that secret? Only group owners?
  Group admins? Readonly members? Recommendation: group owners only,
  enforced at the `/timeshare/g/<group_uuid>/portals/<pk_id>/reveal` endpoint,
  logged to audit_log. MVP ships without password reveal — stored refs
  are created but the reveal endpoint is Phase 8+.

- **Group deletion.** Destructive. Soft delete only (set
  `groups.status='deleted'`) until an explicit hard-delete path is
  built with email confirmation. Cascades would wipe every fact row;
  keep them survivable.

- **Property-level usage patterns beyond biennial.** Schema has
  `usage_pattern VARCHAR(50)` but the UI only handles 'biennial_even'
  and 'annual' at launch. Float weeks, points systems, HICV-style
  timeshares get added as real users show up.

- **Chatbot tool scope creep.** The first 11 tools cover Tillo's use
  cases. A second group may want tools we haven't written (RCI-specific
  queries, points-balance queries). Deferred until needed.

---

## 13. What stays in v1 (`tillotime_site_buildout_guide.md`)

Still authoritative:
- **Dossier inventory (§5.1)** — specific Drive file IDs, subfolder counts
- **Chatbot Q&A test cases (§7.1)** — good regression suite for v3 chatbot
- **II scrape field list (§6.4)** — exact fields per resort page
- **Portal Logins redaction rules (§5.4)** — carry forward as a
  property of `crab.timeshare_portals` (passwords never stored in raw form)

Obsolete in v1 (replaced by this doc):
- §3 (GCP layout), §10 (UI mock), §11 (deploy), §4 (Tillo-specific schema)

---

## 14. Verification checklist (per phase)

### Phase 0 — URL shortener (prereq)
- [ ] `crab.short_urls` table created in `crab` schema
- [ ] `GET /s/<code>` redirects to the stored long URL with 302
- [ ] `POST /api/shorten` (authed) returns a short code; calling it twice with the same URL returns the same code (idempotent)
- [ ] Domain guard rejects non-`crab.travel` URLs with 400
- [ ] Click counter increments on `/s/<code>` hits (fire-and-forget, doesn't block redirect)

### Phase 1 — skeleton
- [ ] Andy creates group "Tillo Family — Royal Sands" at /timeshare/groups/new; row inserted with a fresh `group_id` UUID
- [ ] Invite email to Mom contains a shortened link (`crab.travel/s/xxx` → `/timeshare/g/<uuid>/members/accept/<token>`), not the raw UUID URL
- [ ] Mom accepts, lands on group dashboard
- [ ] Random logged-in user hitting `/timeshare/g/<some-other-valid-uuid>/` gets 404
- [ ] Logged-in user hitting `/timeshare/g/not-a-uuid/` also gets 404 (decorator swallows the UUID-cast exception)
- [ ] Logged-out user hitting a valid group URL redirects to login
- [ ] `curl crab.travel/robots.txt | grep /timeshare/g/` shows Disallow

### Phase 2 — structured schema + form-fill
- [ ] All 22 tables present in schema
- [ ] Andy fills property form → row in `crab.timeshare_properties`
- [ ] Andy enters 2024 CSF ($1,381) → row in `crab.timeshare_maintenance_fees`
- [ ] Andy adds Mom, Luke, Cynthia, Tanner, Celeste, Heather as people
- [ ] `/timeshare/g/<group_uuid>/finances` renders CSF timeline from DB

### Phase 3 — paste/PDF ingestion
- [ ] Andy pastes Royal Resorts CSF history text → Claude extracts
  → proposes 10 `maintenance_fees` rows for years 2006–2024
- [ ] Review UI lists all 10 with accept/edit/reject per row
- [ ] Commit creates 10 rows all tagged with the same
  `source_ingest_job_id`
- [ ] Andy uploads Apr 10 loan statement PDF → Claude extracts loan
  payment history → rows committed to `crab.timeshare_loan_payments`

### Phase 4 — Drive (public link)
- [ ] Andy flips dossier folder `1C0IgQJn9mChJqAjs9OMCQas27oYjk1Jz` to "anyone with link — viewer"
- [ ] Andy pastes the folder URL into `/timeshare/g/<group_uuid>/ingest`; crab fetches the public folder listing (9 Docs + 1 Sheet + 4 subfolders)
- [ ] Selecting "Trip History" doc ingests → proposes ~8 `trips` rows
  + ~12 `people` rows + `trip_participants` linking them
- [ ] Commit happens cleanly; repeated scan skips unchanged files by `modifiedTime`
- [ ] No OAuth consent screen ever shown; no `drive.readonly` scope requested

### Phase 5 — chatbot
- [ ] "What did we pay in 2018?" → "$1,093, paid September 22 2018" +
  citation chip linking to the `maintenance_fees` row
- [ ] "Who went on the 2016 Antigua trip?" → "Tanner and Celeste
  (honeymoon)" + citation
- [ ] "What was our 2019 CSF?" → "I don't have a 2019 CSF in the
  records — the property is biennial (even years only)."
- [ ] Daily cap enforced
- [ ] Tool call log visible in audit_log

### Phase 6 — II catalog
- [ ] `/timeshare/catalog/resort/MAW` renders Marriott Waiohai with
  rating, sleeping capacity, photos
- [ ] Andy shortlists MAW from the group-framed catalog view
- [ ] Shortlist persists in `crab.timeshare_group_shortlist`

### Phase 7 — cycle plan bridge
- [ ] "Create 2026 Cycle" button creates a crab.plans row with
  plan_type='timeshare_cycle', timeshare_group_id=Tillo group_id
- [ ] Plan page shows II-catalog-filtered destination picker
- [ ] Mom, Luke, Cynthia, Tanner, Celeste, Heather, Andy get seeded as
  plan_members
- [ ] OpenCrab VPS starts hunting flights for the plan within an hour

---

## 15. Appendix: Tillo seed data (for Phase 2 script)

Known values to seed via `scripts/timeshare_seed_tillo.py` so Andy's
Phase 1 demo isn't empty:

- Group: "Tillo Family — Royal Sands Cancún", owner `andy.tillo@gmail.com`
- Property: Royal Sands Cancún, unit K5133, week 38, biennial_even,
  trust_expiry 2050-01-24, II network, Mexico
- Contract: #390653, purchase date 2004-07-26 (paid in full), $10,000
- People: Debra Tillo (mother), Andy Tillo (self), Heather Tillo (wife),
  Lilla Tillo (daughter, b. 2004-11-16), Luke Tillo (brother), Cynthia
  Tillo (sister-in-law), Britney Tillo (niece), Tanner Tillo (brother),
  Celeste Tillo (sister-in-law), Don Gabbert (uncle, Debra's brother)
- Portals: Interval International (TILLOAT, member #3430769), Royal
  Resorts Members Area, Interval Servicing (andytillo@gmail.com / PIN
  18719), HICV (no affiliation — confirmed Apr 20 2026)
- Contacts: Jorge Aguayo (RR CSF) jaguayo@royalresorts.com, Julio Ibarra
  (RR Owners Relations) jibarra@royalresorts.com, Royal Resorts support
  1-800-930-5050 →1→1→6 ($20 digital contract copy)

Rest flows in via Phase 3 ingestion of the Apr 10 statements + Phase 4
Drive ingestion of the existing 9-doc dossier.

---

## 16. Phase 7 — II live availability + session keep-alive (added 2026-04-28)

### Why this is a separate phase

II's only AI-friendly surface is HTML behind cookie auth. The login boundary
is protected by Akamai bot manager — headless login is unviable (proven via
Patchright with valid creds, see `docs/ii_scraper_playbook.md` §12). The only
working pattern is **cookie replay from a real Chrome session that already
passed Akamai's challenge**. Once authed, every II member-area endpoint is
just plain HTTP; no further bot-detection on logged-in traffic.

This phase ships the keep-alive layer: a cron that pings `/web/my/home`
every 18–29 min so JSESSIONID's idle timeout never fires, plus an endpoint
the user's local Mac (or a Chrome extension) can POST fresh cookies to
whenever they actually log in.

### Cost approval (2026-04-28)

Andy approved up to **$1.00/month** for this feature, with the explicit
ceiling enforced via a GCP budget alert on the `crab-travel` project.

**Cost math (worst case, no free-tier headroom):**

| Component | Math | $/month |
|---|---|---|
| App Engine cron schedule | $0/job, unlimited | $0.00 |
| F1 instance time | 80 invocations/day × 2.5s (cold start + GET + DB write) = 1.67 hr/mo × $0.05/hr | $0.083 |
| Egress to intervalworld.com | 65 real pings × 170KB = 0.33 GB/mo × $0.12/GB | $0.040 |
| Cloud SQL writes | shared kumori instance, marginal ≈ 0 | $0.00 |
| Email alert when session dies | existing `gmail_utils.py`, no incremental fee | $0.00 |
| **Worst-case TOTAL** | | **$0.123** |

Realistic actual cost: **$0.00–$0.06/month** because crab's F1 with
`min_instances=0` has plenty of free-tier headroom (28 instance-hours/day
free; this addition uses ~0.05 hr/day).

**Hard ceiling: $1.00/month** via GCP budget alert (instructions in
`scripts/setup_budget_alert.sh`). If costs ever exceed $1, Andy gets an
email and we kill the cron.

**No paid third-party APIs in the loop** — the keep-alive is a `requests.get`
to intervalworld.com (a site Andy already has free member access to).
There is no per-call cost on the upstream side. Worst case if II
rate-limits us, they slow us down or temp-block — no money at risk.

### Schema (in `utilities/timeshare_schema.py::_ensure_ii_session`)

```sql
CREATE TABLE crab.timeshare_ii_session (
    pk_id SERIAL PRIMARY KEY,
    member_login VARCHAR(50) UNIQUE NOT NULL,
    cookies JSONB NOT NULL,
    last_keepalive_at TIMESTAMPTZ,
    last_keepalive_status VARCHAR(20),     -- 'healthy' | 'unhealthy' | 'never'
    last_error TEXT,
    last_pushed_from VARCHAR(30),          -- 'manual' | 'mac_launchagent' | 'keepalive_refresh'
    consecutive_failures INTEGER DEFAULT 0,
    keepalive_count INTEGER DEFAULT 0,
    ...
);
```

Cookies stored as JSONB plaintext. They rotate every ~30 min idle anyway —
same security profile as JSESSIONID in any web app's memory. The DB itself
is the access boundary (Cloud SQL auth, no public exposure).

### Routes

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET/POST | `/tasks/timeshare-ii-keepalive` | `X-Appengine-Cron` or `?secret=` | Cron pings II, updates health |
| POST | `/api/timeshare/ii-cookies/refresh` | `Authorization: Bearer <CRAB_TASK_SECRET>` | Push fresh cookies (Mac LaunchAgent or manual) |

### Kill switch

Comment out the `cron.yaml` entry under "Timeshare — II keep-alive" and
redeploy. Zero pings fire, zero cost. The endpoint stays callable for
manual pings.
