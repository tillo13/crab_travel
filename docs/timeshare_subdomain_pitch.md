# timeshare.crab.travel — product positioning

**Status:** product-positioning doc. Captures the pitch as defined by Andy on
2026-04-28 after a full review of the timeshare angle with Celeste as the
first real outside user.

**One sentence:** *Completely understand everything about your timeshare —
and decide what's next.*

---

## The pitch

You bought a timeshare years ago. Most years, you don't use it well.

**timeshare.crab.travel gives you complete clarity on what you own — and
helps you decide what to do with it next.**

Two halves, one platform.

### Half 1 — Completely understand

Every CSF back to 2006. Every loan payment. Every trip with who went where
and what it cost. Every portal login your family can never find. Every
contact at Royal Resorts. Every contract. Every document, linked back to
where you actually keep it. The full Interval International catalog of
2,491 resorts, overlaid with your family's history. **One private place.
Nobody hosts your files but us, and we don't even host the files.**

An AI assistant reads only your family's records and answers in plain
language with citations:

- "What did we pay in 2018?" → "$1,093, paid Sep 22 2018." [📄 CSF row]
- "Who went on the 2016 Antigua trip?" → "Tanner and Celeste (honeymoon)."
- "Show me the II portal login." → "Username TILLOAT, member #3430769."

### Half 2 — Decide what's next

Given complete understanding, the platform helps you decide. Home week
again? Exchange somewhere? Rent? Skip the year?

A **vacation cycle** (the biennial or annual use-year your contract gives
you) becomes a real plan inside crab — destination voting, family
preference aggregation, multi-modal transport hunting (flights, hotels,
activities, cars across every major source), price-drop alerts, AI
recommendations. The same engine that powers crab.travel's group-trip
product, applied to your timeshare cycle. The family votes from their
phones, the platform watches prices, and you converge on a real trip
instead of forfeiting another year.

---

## Selling lines

- **Primary:** *Timeshare, completely understood. Then, exactly what's
  next.*
- **Landing copy:** *You bought a timeshare years ago. Most years, you
  don't use it well. Crab gives you complete clarity on what you own —
  and tells you what to do with it next.*
- **For non-tech family members (text-message tone):** *I put all our
  family timeshare stuff in one place — fees, trips, who's who. There's
  a little assistant that answers anything. Tap around, no login.*

---

## Why this works

The status quo for ~10M U.S. timeshare owners is the same
"five-statements-six-inboxes-portal-logins-nobody-can-find" problem.
TUG community confirms even paid features (II's Getaway Alerts, the II
mobile app) work *"VERY inconsistently"* and most owners just stay in
their own properties because the alternatives are too hard to navigate.

Nobody else has built the dossier + decision layer on top of it.
Existing options:

| Option | What they do | What's missing |
|---|---|---|
| II / RCI member portals | Inventory + booking | No history, no family, no decision support |
| Spreadsheets / Google Docs | One person remembers everything | Doesn't scale to family, no AI, no decisions |
| TUG forums | Crowdsourced wisdom | Generic, not yours |
| Concierge timeshare-management services | Manual, expensive ($X,000/year) | Doesn't help you decide; just executes |

We sit on top of all of these. We're the *understand-and-decide* layer.
Bookings still happen on II, Royal Resorts, Marriott — wherever loyalty
lives. We don't compete with them; we make their data legible.

---

## Who it's for

Order of fit:

1. **Owners who pay every year and rarely use it well.** This is the
   widest pool — people who bought in the 90s/2000s, kids are grown, the
   asset is a quiet drag. Crab makes it productive.
2. **Multi-family timeshare ownership** (e.g., siblings inherited a
   parent's week). The "who paid what, who went when" coordination
   problem is acute and crab is built for it natively (multi-tenant
   groups, role-based access, AI-with-citations).
3. **Single owners who use it heavily but lose track of details.** The
   AI-with-citations is huge here. Customer #1 (Andy + Tillo Family)
   fits this.

---

## What's already built (proof we can ship the pitch)

As of 2026-04-28, customer #1 (Tillo Family — Royal Sands Cancún) is
fully populated:

- 22 dossier tables in `crab.timeshare_*` filled with real seed data
  (property, contracts, fees 2006–present, loan payments, trips, people,
  portals, contacts)
- AI assistant with scoped tool-use + citation chips (`/g/<uuid>/ask`)
- Ingest pipeline: paste text, upload PDF, or scan a public Drive folder
  — Claude extracts proposed facts, user reviews row-by-row, commits
- 2,491-resort II catalog browsable by region/area/resort with photos +
  ratings, overlaid with the family's Considering list
- Email-locked invite (admin-friendly) + 7-day public share link
  (non-tech-friendly, no Google account required)
- Mobile-polished read-only experience for non-tech family members
- Cycle bridge to crab.plans — `plan_type='timeshare_cycle'` plans
  inherit the full crab adapter fan-out (Duffel, LiteAPI, Viator,
  Travelpayouts) for vacation-decision support

---

## What unlocks the *Decide* half going from good to great

Each of these strengthens "decide what's next" without needing to scrape
Interval International:

1. **A "What's next" tile on the group dashboard.** Today the dashboard
   is retrospective (catalog map, dossier counts). Add a forward-looking
   card: *"Your 2026 cycle — undecided. Tap to start picking."* Surfaces
   the family's pending decision the moment they land.
2. **Cycle plans as the actual decision artifact.** When you create a
   cycle, all the existing crab plumbing kicks in — voting, availability
   overlap, transport hunting, AI recommendations. The Sept 2026 trip
   is one cycle plan; 2028 is another. The family votes, crab watches,
   you converge.
3. **Decision-aware AI prompt chips.** Today's chips are retrospective
   ("What did we pay in 2024?"). Add prospective ones: *"Where would we
   get the best value if we exchanged?"* / *"What weeks are we wasting?"*
   / *"Should we use our home week or trade it this year?"*
4. **Phase-7 II availability check** (eventually). Once a cycle has
   narrowed to 2–3 candidate destinations, an on-demand "is the exchange
   actually feasible?" lookup. Last feature on the list, not first —
   the engine works without it.

---

## What this product is NOT

- Not a booking site. We deep-link out for actual transactions.
- Not a document storage service. Files stay where you keep them.
- Not a replacement for II / RCI / Marriott Vacations. We sit above them.
- Not a generic SaaS pitch. The first 100 customers will be families
  exactly like Tillo: ~$1,500/year fee, decades of ownership, fragmented
  data, no clear next step.

---

## Where this fits in crab.travel

This is one vertical (`plan_type='timeshare_cycle'`) of crab.travel's
broader group-coordination + travel-aggregation engine. The other
verticals share infrastructure:

- `plan_type='trip'` — group trips for friends (current MVP, the founding
  20-friend group)
- `plan_type='monthly'` — retiree life concierge (parked Phase 2)
- `plan_type='timeshare_cycle'` — this product

Same users, same plans, same preferences, same recommendations engine,
same adapters, same price-history moat. Different entry point, different
landing copy, different brand surface. timeshare.crab.travel is the
narrowest, sharpest entry point — the one with the clearest "you
already pay for this" wedge.

---

## Marketing surfaces (where the pitch needs to live)

- `/timeshare/` landing page — primary copy on the public marketing page
- `/timeshare/groups/new` form intro — the framing the new owner sees
  before creating their first group
- Invite email body — what someone tapping the share link expects to find
- Welcome banner on first dashboard visit — frames the experience for
  non-tech family members

These are all places to lean into *Understand + Decide* and away from
the older *"five statements, six inboxes"* framing — that was the
problem statement; the pitch now is the solved-state.
