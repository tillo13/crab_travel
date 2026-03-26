# crab.travel — Next Steps
*Updated: 2026-03-25 (end of session)*

## Done This Session (March 25)

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

## Remaining — Prioritized

### High Impact (Next Session)
- **Expense tracking UI** — DB + CRUD exist, no frontend. Add form on trip summary (who paid, amount, category) + per-person balances.
- **Itinerary editor** — 21 items exist on demo trip but no add/edit/reorder UI. Add "Add item" button, drag-and-drop.
- **Auto-generate itinerary via AI** — when all watches booked, LLM generates day-by-day plan from flight times + hotel + destination research.

### Medium Impact
- **Amadeus flight integration** — free tier 2K searches/month, add as adapter
- **Kiwi Tequila integration** — free for affiliates, good for multi-city/flexible routing
- **Airbnb / vacation rental integration** — large groups need whole-home rentals. Investigate Airbnb Affiliate API, VRBO/Vacasa alternatives. Show "Stays" as separate category from hotels.

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
| Twilio | ~$0.01/msg | Alert delivery |

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
