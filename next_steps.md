# crab.travel — Next Steps
*Updated: 2026-03-25*

## What Was Built This Session

### Features Shipped
1. **Calendar overhaul** — names fill cells instead of "+N", click any date for modal with full member details, auto-skips to first month with data
2. **Booking progress tracker** — progress bars for flights/hotels, per-member status chips, Mark Booked modal captures price + confirmation #, all-booked celebration
3. **AI watch recommendations** — smart Book Now/Wait/Watch verdicts per watch based on price trend + days to departure, auto-computed on every cron price check
4. **IATA code resolution** — LLM-powered destination→airport lookup (Scottsdale AZ → PHX), expanded 70-city local map, state-suffix stripping
5. **Xotelo hotel adapter** — free hotel prices from Booking.com/Expedia/Agoda (no API key needed)
6. **Trip Summary page** (`/plan/{id}/summary`) — flights, hotels, day-by-day itinerary, cost breakdown per person
7. **Admin overhaul** — tabs (Overview/Users/Plans/Activity), search, pagination, status filters, sort dropdowns
8. **Ops Dashboard** (`/admin/ops`) — LLM backend health, hourly volume chart, error log, watch engine stats, AI rec breakdown
9. **/live reframe** — "See It Live" product showcase, "Example Trip" banners, "Booked" filter tab with trip summary links
10. **Dark/light toggle fixed** — sun in dark mode, moon in light mode
11. **Repo public** — github.com/tillo13/crab_travel with README, single `main` branch
12. **Playwright auth** — `?apikey=SECRET&user_id=1` bypasses OAuth for testing
13. **DB pool audit** — all 10 kumori apps fixed: statement_timeout=30s, pool sizes right-sized, 2manspades leak fixed

### Demo Trip Seeded
- **URL:** `crab.travel/to/qL6zhRAI`
- **Summary:** `crab.travel/plan/25438c20-0bb3-4137-9f5f-2ebdbeb0010b/summary`
- 12 members, all flights + hotels booked with real prices, hotel names, confirmation numbers
- 4-day itinerary (Scottsdale, May 20-23) with 21 items: restaurants, hikes, tours, nightlife
- Plan status: "booked"

---

## Immediate Fix Needed (Start of Next Session)

### 1. /live page shows 0 trips after deploy storm
**Root cause:** Fresh App Engine instances start with an empty psycopg2 connection pool. If ANY of the first few requests fail (pool exhaustion from other apps deploying simultaneously), the pool marks those connections as "checked out" permanently — the pool object is corrupted even though the DB connections are fine.

**Fix options:**
- A) Add pool health check on startup — if `pool.getconn()` fails, recreate the pool
- B) Add a `/tasks/warmup` handler that pre-opens a connection to test the pool
- C) Simply wait — once the deploy storm settles and old versions are deleted, new instances get clean pools

**Also:** 790 bot runs were stuck in "running" status (now marked failed). The bot runner should auto-fail runs that have been "running" for > 1 hour. Add cleanup to the `/tasks/crawl` cron.

### 2. Crab_travel needs one final clean deploy
After all other app deploys finish and old versions drain, deploy crab_travel one more time to get a fresh pool. Then run the E2E test.

---

## Short-Term (Next 1-2 Sessions)

### 3. Hotel adapter verification
Xotelo adapter was added but hasn't been tested in production via the watch cron yet. Next `check-watches` run (every 8h) should use Xotelo for hotels. Verify:
- Does Xotelo return prices for "Scottsdale AZ"?
- Do hotel prices show up on watch cards?
- Do non-demo trips get hotel data?

If Xotelo doesn't work for the destination format, the `_destination_iata` function may need to pass city names differently to the hotel adapter (hotels use city names, not IATA codes).

### 4. LLM pipeline is broken
Last LLM call was March 24 15:35 UTC. All backends stopped working. The bots still run (creating plans, joining, voting don't need LLMs) but:
- No AI destination research on new trips
- No AI-generated chat messages
- No destination card content

**Investigation needed:** Check `crab.llm_calls` error types from March 24. Likely all backends hit daily caps simultaneously, or the `_log` function broke. The `llm_router.py` may need its caps/quotas refreshed.

### 5. Deploy wattson
`deploy.json` was created at `/Users/at/Desktop/code/wattson/deploy.json` but wattson hasn't been deployed yet. Run:
```bash
cd ~/Desktop/code/wattson && deploy "DB pool: add 30s statement_timeout"
```

### 6. Clean up old App Engine versions across ALL apps
Each app may have 2-3 old versions still "SERVING" (0% traffic but holding stale pools). For each app:
```bash
gcloud app versions list --service=default --project=PROJECT_ID
gcloud app versions delete OLD_VERSION --service=default --project=PROJECT_ID --quiet
```

---

## Medium-Term (Next Week)

### 7. Interactive demo mode
Adam Agust demo: instead of just a static seeded trip, build a guided walkthrough where a visitor can click through the entire flow — create trip → watch members join → vote → lock → see prices → book → summary. Could be a "demo" button on the landing page that auto-populates a personal trip in real-time.

### 8. Expense tracking UI
`crab.expenses` table exists, CRUD functions exist in postgres_utils.py, but no UI. Add:
- Expense form on trip summary page (who paid, amount, category)
- Per-person balance ("Alice owes Bob $47")
- Integration with booked watch prices as auto-expenses

### 9. Itinerary editor
Currently itinerary items are seeded via script. Add:
- "Add item" button on trip summary
- Drag-and-drop reordering
- Link AI recommendations to itinerary items
- Auto-generate itinerary from booked watches + recommendations via LLM

### 10. Auto-generate itinerary via AI
When a trip has all watches booked, use the LLM router to generate a day-by-day itinerary based on:
- Flight arrival/departure times
- Hotel check-in/out
- Destination research from `crab.destination_suggestions`
- Group preferences from `crab.plan_preferences`

### 11. "Start your own trip" CTA
Add prominent call-to-action on:
- Trip summary page footer ("Plan your group trip →")
- /live page ("Start planning →")
- About page

### 12. Amadeus integration
Free tier: 2,000 flight searches/month. Add as another adapter in the round-robin:
```python
# utilities/adapters/amadeus.py
class AmadeusAdapter(TravelAdapter):
    source_key = "amadeus"
    # Self-service API, 10 req/s test, 40 req/s prod
```

### 13. Kiwi Tequila integration
Free for affiliates. Great for flexible routing (multi-city, "anywhere" searches):
```python
# utilities/adapters/kiwi.py
class KiwiAdapter(TravelAdapter):
    source_key = "kiwi"
```

### 14. Airbnb / vacation rental integration
Large group trips (10+ people) often need whole-home rentals, not hotel rooms. Airbnb is a better fit for groups splitting a house. Investigate:
- Airbnb Affiliate API (if available) or scraping approach
- VRBO/Vacasa affiliate programs as alternatives
- Show "Stays" as a separate category from hotels in watch engine
- Group-optimized search: filter by guest count, bedrooms, shared spaces

---

## Infrastructure / DevOps

### 14. Connection pool monitoring
Add a `/admin/pool` page or section to ops dashboard showing:
- Current `pg_stat_activity` breakdown
- Per-app connection count
- Leaked connection detection
- One-click cleanup button

### 15. Cron health monitoring
The `/tasks/crawl` and `/tasks/check-watches` crons don't persist execution logs. Add a `crab.cron_executions` table:
```sql
CREATE TABLE crab.cron_executions (
    pk_id SERIAL PRIMARY KEY,
    task_name VARCHAR(50),
    started_at TIMESTAMPTZ DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    duration_ms INTEGER,
    success BOOLEAN,
    summary JSONB,
    error_message TEXT
);
```

### 16. Bot run cleanup
Auto-fail bot runs stuck in "running" for > 1 hour. Add to `/tasks/crawl`:
```python
# At start of crawl task:
cur.execute("UPDATE crab.bot_runs SET status='failed', finished_at=NOW() WHERE status='running' AND started_at < NOW() - INTERVAL '1 hour'")
```

---

## API Cost Awareness

| API | Model | Monthly Cost | Status |
|---|---|---|---|
| Duffel | Pay per booking + excess search fee | ~$0 (no bookings yet) | Demoted to fallback |
| Travelpayouts | Free (affiliate) | $0 | Primary flight source |
| Xotelo | Free | $0 | Primary hotel source (NEW) |
| LiteAPI | Sandbox (free) | $0 | Hotel fallback |
| LLM Router | Free tier round-robin | $0 | 15+ backends |
| Twilio | Pay per SMS | ~$0.01/msg | Alert delivery |

**Watch:** Duffel search-to-booking ratio. If we hit 1500 searches with 0 bookings, they charge $0.005/search. Travelpayouts is now primary to minimize Duffel usage.

---

## DB Connection Budget (Post-Audit)

| App | maxconn | Status |
|---|---|---|
| galactica | 6 | ✅ Deployed |
| crab_travel | 6 | ✅ Deployed |
| kumori | 3 | ✅ Deployed |
| dandy | 2 | ✅ Deployed |
| 2manspades | 2 | ✅ Deployed (leak fixed) |
| scatterbrain | 2 | ✅ Deployed |
| stealth | 2 | ✅ Deployed |
| kindness_social | 3 | ⏳ Deploying |
| ooqio | 2 | ⏳ Deploying |
| wattson | 2 | ❌ Not deployed yet |
| **Total** | **30/50** | **20 headroom** |

All apps now have `statement_timeout=30000` (30s) and `connect_timeout=10`.
