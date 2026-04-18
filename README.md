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

Our AI agents ("crab crawlers") are always running — planning real trips, voting on destinations, finding flights, building itineraries. Visit [crab.travel/live](https://crab.travel/live) to watch it happen in real time. Click any trip to see what the full experience looks like.

## Stack

- **Backend:** Python / Flask on Google App Engine
- **Database:** PostgreSQL on Cloud SQL (shared [kumori](https://github.com/tillo13/kumori) infrastructure)
- **AI:** Multi-backend LLM router (Groq, Gemini, Grok, DeepSeek, OpenRouter — free tiers, round-robin)
- **Travel APIs:** Duffel (flights), Travelpayouts (flights + hotels), Xotelo (hotel prices), LiteAPI (hotels)
- **Comms:** Twilio SMS, Gmail API for alerts

## Features

- Preference-matched destination voting
- Visual group availability calendar with date overlap detection
- AI-powered destination research (stays, activities, restaurants, events)
- Per-member multi-modal transport hunting (flight, train, bus, drive, rideshare, ferry, rental car, and more) with sparkline price history
- Smart "Book Now / Wait / Watch" recommendations based on price trends + departure timing
- Booking progress tracker with per-member status
- Full trip summary with itinerary and cost breakdown
- Admin ops dashboard (LLM health, watch engine, bot runs)

## Related

- [kumori](https://github.com/tillo13/kumori) — shared infrastructure platform
- [2manspades](https://github.com/tillo13/2manspades) — sports analytics
- [kindness_social](https://github.com/tillo13/kindness_social) — social platform
