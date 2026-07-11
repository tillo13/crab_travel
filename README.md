# crab.travel

**Group trips, without the chaos.** AI-powered trip coordination that replaces your group text + spreadsheet nightmare.

🌐 **Live:** [crab.travel](https://crab.travel) | 🤖 **Watch it work:** [crab.travel/live](https://crab.travel/live)

## What it does

12 friends going to Scottsdale. Right now that's managed via a chaotic mix of group texts, shared docs, and Venmo requests. crab.travel fixes that:

1. **Create a trip** — organizer sets the destination options and invites the group via link
2. **Everyone votes** — members rank destinations, mark availability on a shared calendar, set their budget and preferences
3. **AI finds the best option** — synthesizes everyone's preferences, researches destinations, recommends hotels and activities
4. **Lock it in** — organizer locks the destination and dates, CrabAI starts hunting every modality (flight, train, bus, drive, rideshare, rental car, ferry, and more) for every member
5. **Smart booking alerts** — CrabAI monitors prices continuously, tells each person when to book and why ("fare dropped 3 scans in a row, 18 days to departure — book now")
6. **Trip summary** — once booked, everyone sees the full picture: flights, hotels, day-by-day itinerary, cost breakdown per person

## Watch it live

Our AI agents ("crab crawlers") are always running — planning real trips, voting on destinations, finding flights, building itineraries. Visit [crab.travel/live](https://crab.travel/live) to watch it happen in real time.

## travel_agent/ — the personal trip-advisor toolkit

A newer arm of the project: a preference-driven flight advisor + expense reconciler,
built and battle-tested by planning (and booking) real trips end to end. The interesting
parts if you're poking around:

- **`travel_agent/prefs.py`** — a "preference brain" template: geography + ground-access
  economics, seat rules, arrival curfews, layover bands, and the two value lenses that make
  fare decisions legible (**¢ per mile** and **$ per flight-hour** of a cabin upgrade).
  Ships with generic examples; a real traveler's values live in a gitignored vault and
  deep-merge at load.
- **`travel_agent/chain_closure.py`** — the two-ticket "chain" engine: hop from a small
  airport, premium cabin from the gateway. Parses Google Flights results, verifies the
  chain *closes* on both travel days (buffer, red-eye, curfew flags), and catches
  mixed-cabin itineraries ("Economy + First Class") that fare labels hide.
- **`travel_agent/gf_links.py`** — an encoder for Google Flights' undocumented `tfs=`
  URL protobuf: generate exact-filter deep links (date, airlines, stops, cabin,
  departure-hour buckets) that land on a one-flight list instead of a search page.
- **`travel_agent/gf_scrape_template.js`** — the scrape recipe (via a stealth Playwright
  wrapper) with the hard-won gotchas documented in comments.
- **`travel_agent/reconcile.py`** — card-statement ↔ receipt matcher: descriptor cleaning,
  city/state extraction, home-metro exclusion, FX-tolerance matching, orphan surfacing.
  Card data stays local by design; only sanitized results touch the cloud.
- **Trip boards** — each planning run renders a stat-dense decision board (options
  ladder, rejected-options graveyard with reasons, calendar conflict check) published at
  an unguessable `/trip-board/<token>` URL for sharing.

## Features

- Preference-matched destination voting
- Visual group availability calendar with date overlap detection
- AI-powered destination research (stays, activities, restaurants, events)
- Per-member multi-modal transport hunting (flight, train, bus, drive, rideshare, ferry, rental car, and more) with sparkline price history
- Smart "Book Now / Wait / Watch" recommendations based on price trends + departure timing
- Booking progress tracker with per-member status
- Full trip summary with itinerary and cost breakdown
- Admin ops dashboard (LLM health, watch engine, bot runs)

## Stack

- **Backend:** Python / Flask on Google App Engine
- **Database:** PostgreSQL on Cloud SQL (shared [kumori](https://github.com/tillo13/kumori) infrastructure)
- **AI:** Multi-backend LLM router (free tiers, round-robin) + Claude-driven planning sessions
- **Travel APIs:** Duffel (flights), Travelpayouts (flights + hotels), LiteAPI (hotels)
- **Scraping:** centralized stealth Playwright for live fare/schedule data
- **Comms:** Twilio SMS, Gmail API for alerts and booking-confirmation ingestion

## Privacy posture

Anything personal — identity documents, loyalty numbers, card statements, real
preference values, trip documents — lives in gitignored `_private/` directories or the
database, never in this repo. The code here is the machinery, not the traveler.

## Related

- [kumori](https://github.com/tillo13/kumori) — shared infrastructure platform
- [2manspades](https://github.com/tillo13/2manspades) — sports analytics
- [kindness_social](https://github.com/tillo13/kindness_social) — social platform
