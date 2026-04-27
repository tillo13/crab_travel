# Deployment & Git Rules

**📋 Always read `next_steps.md` at the repo root first** — it's auto-maintained nightly with the latest shipped commits, pending queue (from `deploy --next`), and any uncommitted WIP or TODO markers.

## Deployment

```bash
# ALWAYS use the centralized deploy tool (git push + GCP deploy in one command)
deploy "commit message"
```
- NEVER use raw `git push`, `gcloud app deploy`, or old `git_push.sh`/`git_push.bat` scripts
- `deploy` always does BOTH git push and GCP deploy — that is the default and expected behavior
- See `~/.claude/skills/deploy-to-gcp.md` for flags like `--git-only` or `--gcp-only` (only if explicitly needed)
- Config: `deploy.json` in project root | Tool: `~/Desktop/code/master_gcp_deploy/deploy.py`

## Git Rules

- **Never include Co-Authored-By or any AI attribution in commits.** No Claude references, no AI credits.

---

# LifeConcierge Platform — Project Brief

## The Core Idea

An AI-powered life planning and group coordination platform that works like a personal concierge — but at a fraction of the cost of traditional concierge services. It started as a retirement concierge concept but the real insight is broader: **anyone coordinating life events, group travel, family schedules, or shared experiences is underserved.** Excel files and group texts are the current solution. We can do much better.

---

## The Two Primary Use Cases (and they share the same infrastructure)

### Use Case 1: Retiree Life Concierge
Retirees have disposable income, time, and a deep desire to stay connected to family and active in life — but often lack the structure, tech savvy, or activation energy to make it happen. Nobody is serving this demographic beyond financial planning. We cover everything else.

**Pain points we solve:**
- Missing grandkids' events because nobody told them
- Not knowing what's happening locally that matches their interests
- Overwhelming trip planning with no personalized curation
- Isolation and loss of relevance as family gets busy
- Tech barriers preventing them from using existing tools

**How it works:**
- Onboarding via a dating-profile-style intake (tap interests, swipe preferences, describe family)
- Family members invited to connect calendars and submit grandkid events via simple link
- AI synthesizes everything into a monthly "Life Plan" — events, trips, activities, family moments
- Delivered by a human advisor in a warm monthly touchpoint (AI does the work, human does the relationship)
- Financial advisor partnership channel — advisors white-label it as a premium add-on for retiree clients

---

### Use Case 2: Group Trip & Experience Coordinator
15 friends going to Phoenix. A family reunion in Nashville. A bachelor party in Vegas. A girls' weekend in Napa. Right now all of this is managed via a chaotic mix of Excel spreadsheets, endless group texts, and Venmo requests with no context. It's a solved problem waiting to happen.

**Pain points we solve:**
- Nobody knows the full budget or where money stands
- Hotels, cars, and activities booked by different people with no central view
- People have different preferences and nobody accounts for them
- Splitting costs is a nightmare
- Planning falls on one person who burns out

**How it works:**
- Group organizer creates a "trip" and invites members via text link — no app download required to participate
- Each member inputs preferences, budget range, dietary needs, activity interests
- AI generates options for hotels, cars, activities, and restaurants that balance the group's collective preferences and budget
- Shared dashboard shows who's paid what, what's booked, what's pending
- AI suggests itinerary options ranked by group compatibility score
- Integrated booking with travel affiliate partners for seamless execution

---

## What Makes This Different

This is not another travel app. It's not another group chat. It's a **coordination and curation layer** that sits on top of existing services and makes group and life planning feel effortless. The AI does the synthesis — reading preferences, matching to options, flagging conflicts, building itineraries. Humans make the final calls.

The retiree and group travel use cases share the same core infrastructure:
- Preference profile engine
- Calendar and event aggregation
- AI recommendation and synthesis layer
- Booking and affiliate integration
- Budget tracking and splitting

---

## Business Model

### Revenue Streams
1. **Subscription** — Monthly fee per retiree member or per group trip ($15–$50/trip for group, $150–$300/month for retiree concierge)
2. **Travel affiliate commissions** — Expedia, Booking.com, Viator, GetYourGuide, cruise lines pay 3–8% on bookings driven through platform
3. **Featured partner placement** — Local event organizers, tour companies, restaurants pay for promoted placement to relevant users
4. **White-label B2B** — Financial advisors, senior living communities, corporate HR departments license the platform for their clients/employees
5. **Group buying / negotiated rates** — Volume discounts negotiated with local venues and travel providers, passed partially to users, margin kept by platform

---

## API Architecture

### Calendar & Family Coordination
- Google Calendar API
- Microsoft Graph API (Outlook)
- Apple EventKit
- Cozi API (family calendar popular with parents)

### Local Events & Activities
- Eventbrite API
- Ticketmaster / SeatGeek API
- Google Places API
- Meetup API
- **Custom event ingestion layer** — AI parses event submissions from any format (Facebook link, email forward, PDF, photo of flyer, RSS/iCal feed). AI normalizes into structured data and auto-tags by category, physical demand, price, indoor/outdoor, family-friendly.

### Travel & Booking
- Expedia Partner Solutions API
- Booking.com Affiliate API
- Kayak API
- Viator API (tours and experiences — key for retirees and group trips)
- GetYourGuide API
- Skyscanner API (price monitoring)
- Airbnb Affiliate Program

### Financial & Payments
- Plaid API (budget awareness, spending tracking)
- Stripe API (platform billing, group cost splitting)
- Splitwise API (expense coordination for groups)

### AI & Intelligence
- Anthropic Claude API (core reasoning, synthesis, recommendation generation, event parsing)
- Pinecone or Weaviate (vector DB for long-term user profiles and preference memory)

### Communication & Delivery
- Twilio API (SMS reminders and notifications)
- SendGrid API (email delivery of plans and updates)
- WhatsApp Business API (optional group coordination channel)

### Health & Accessibility (retiree use case)
- Apple HealthKit / Google Fit API (activity level awareness for appropriate recommendations)

### Utility
- OpenWeatherMap API (seasonal suggestions, travel timing)
- Google Maps / Directions API
- Yelp Fusion API

---

## Onboarding Experience

Modeled after a dating profile — familiar, non-intimidating, even fun. Not a form. A conversation.

**For retirees:**
1. Tell us about you (lifestyle pace, personality, home base)
2. What lights you up? (tap interest tiles — golf, cooking, history, music, gardening, travel, theatre, pickleball, etc.)
3. Your family (add grandkids by name/age/city, invite adult children to connect calendars via text link)
4. Travel style (budget, distance comfort, hotel vs. rental, frequency, physical considerations)
5. Life rhythm (how often they want suggestions, how they want to be reached)

**For group trips:**
1. Organizer creates trip (destination, dates, rough headcount)
2. Members invited via link — they each complete a quick preference profile (budget, room preferences, dietary needs, activity interests, mobility considerations)
3. AI immediately generates a compatibility overview and first-pass recommendations
4. Group votes or organizer decides — platform handles the booking

**The Magic Moment:** Within minutes of completing onboarding, users see a personalized first look — a few events near them, a trip idea matched to their profile, an upcoming family moment flagged. Instant value, instant wow.

---

## Local Event Ecosystem

A two-sided marketplace layer where local businesses and event organizers submit events and reach a hyper-targeted audience of engaged, affluent users.

**Submission methods (AI normalizes all of them):**
- Paste any URL (Facebook event, website, Eventbrite)
- Forward an email
- Upload a PDF or image of a flyer
- Fill out a simple form
- RSS or iCal feed (for larger venues and city parks departments)

**AI auto-tagging on ingestion:**
- Category and subcategory
- Physical demand level
- Price range
- Duration
- Indoor / outdoor
- Family/grandkid appropriate
- Accessibility

**Monetization for this layer:**
- Free basic submission
- Paid featured placement for promoted events
- Verified partner badge (monthly fee) for trusted local businesses

---

## Key Partnership Channels

- **Financial advisors** — White-label the retiree concierge as a premium client benefit. Advisors look like heroes, we get distribution without direct customer acquisition cost.
- **AARP** — Natural alignment, massive retiree membership base
- **AAA** — Travel + retiree demographic overlap
- **Road Scholar** — Educational travel for 50+, exact demographic match
- **Senior living communities** — Offer as amenity to residents
- **Corporate HR** — Group trip coordination as an employee benefit for offsites and team events
- **Cruise lines** — Royal Caribbean, Carnival, Norwegian all have affiliate programs; retirees are their #1 demographic

---

## Competitive Landscape

| Competitor | What they do | What they miss |
|---|---|---|
| Luxury concierge (Quintessentially, One Concierge) | Full-service lifestyle management | $20K–$50K/year, not AI-powered, no family coordination |
| Senior concierge services | Errands, appointments, companionship | Care-focused, not life-enrichment or family-connection |
| Robo-advisors / financial AI tools | Money only | No lifestyle, no family, no activities |
| Eventbrite / Ticketmaster | Events | Generic, not personalized, not retiree-focused |
| TripIt / Google Trips | Trip organization | No group coordination, no preference matching, no curation |
| Splitwise | Expense splitting | No planning, no booking, no recommendations |
| Group text + Excel | Everything | Exactly. That's the competition. |

**Nobody has combined:** preference-matched life planning + family calendar integration + local event curation + group coordination + travel booking into one platform for underserved demographics.

---

## Technical Philosophy

- **AI does the work, humans do the relationship** — especially critical for the retiree segment
- **Meet users where they are** — no forced app downloads, SMS and email first, family members participate via simple links
- **Progressive complexity** — simple on the surface, powerful underneath
- **Data compounds over time** — the longer a user is on the platform the smarter and more personalized it gets; this is the core moat
- **Mobile-first but not mobile-only** — retirees often prefer desktop or tablet

---

## Suggested Stack (Starting Point)

- **Frontend:** React / Next.js
- **Backend:** Node.js or Python (FastAPI)
- **Database:** PostgreSQL (structured data) + Pinecone (vector embeddings for preference matching)
- **AI:** Anthropic Claude API (primary reasoning and synthesis)
- **Auth:** Auth0 or Clerk
- **Payments:** Stripe
- **Hosting:** AWS or Vercel
- **SMS/Email:** Twilio + SendGrid

---

## MVP Scope (What to Build First)

Start with the group trip use case — faster feedback loop, viral by nature (15 friends all use it once = 15 potential future organizers), and proves the core preference matching and coordination engine that powers both use cases.

**MVP features:**
1. Trip creation and member invitation via link
2. Preference intake per member (budget, interests, accommodation style)
3. AI-generated hotel and activity recommendations matched to group
4. Shared itinerary view
5. Basic cost tracking and splitting

Once that engine works, the retiree concierge layer is largely the same engine with a different onboarding flow, a monthly delivery cadence, and the family calendar integration on top.

---

## Open Questions to Resolve

- What do we call this? (Name should work for both use cases — retirees and group travelers)
- Do we launch both use cases simultaneously or pick one to validate first?
- Human advisor model for retiree concierge — in-house team or contractor marketplace?
- How do we handle privacy for family calendar data — especially minors?
- Freemium entry point for group trips to drive viral growth?
