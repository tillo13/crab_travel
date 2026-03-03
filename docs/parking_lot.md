# crab.travel — Parking Lot (Future Vision & Ideas)

## The Big Picture

20 close friends build crab.travel to plan their own group trips. The tool works so well that each of them brings their own networks. The platform grows into an AI-powered travel agency where resorts and travel partners pay for access to qualified groups of affluent travelers. The founders travel the world as the business runs itself.

---

## Founding Group

- 20 members, ~50 years old, affluent (retired lawyers, tech people, etc.)
- First trip: Phoenix (current)
- Second trip: Helsinki, Finland 2027 (tentative)
- Goal: travel together regularly (yearly or more), potentially become business partners in crab.travel
- Each member can bring their own network → 20 groups of 20 = 400 users organically

---

## Future Features

### Persistent Groups
- `crab.groups` table — a group has many plans, members carry over between trips
- Group history: past trips, photos, memories
- "Start another trip" button that pre-fills the same group members
- Group chat or message board between trips

### Partner & Deal Model
- Organizers attach negotiated deals to recommendations (manual at first — just a URL + price)
- "Group summary" PDF export for partner outreach (member count, avg budget, interests, travel frequency)
- Partner portal: resorts/hotels submit deals targeting groups that match their profile
- Featured placement: partners pay for visibility to relevant groups
- Commission tracking: platform takes a cut of each booking driven through a partner deal
- Negotiated group rates (10-30% below retail for 15+ rooms)

### AI Travel Agent
- Claude researches destinations, compares options, builds full trip proposals
- "We want a beach trip in March for 20 people, $3K/person budget" → full proposal with flights, hotels, activities
- Price monitoring: alert when deals drop for destinations matching group interests
- Seasonal suggestions based on group preferences and travel history

### Growth & Network Effects
- Each member's trips create new users who become future organizers
- Referral program: bring a group, get a perk
- Public trip templates: "See how a group of 20 did Iceland" → inspires new groups
- Travel journal / trip recap auto-generated from itinerary + photos

### Revenue Streams (Post-MVP)
1. Affiliate commissions from booking platforms (3-8%)
2. Partner featured placement fees
3. Negotiated group rate margin (keep a % of the discount)
4. Premium subscription for organizers (advanced AI features, unlimited plans)
5. White-label for corporate offsites / HR departments

### Retiree Concierge (Same Platform, Different Entry Point)
- Monthly AI-generated life plans based on persistent profile
- Local event ingestion (parse URLs, emails, flyers via Claude)
- Family calendar integration (Google Calendar API)
- Family member invite links to submit grandkid events
- Human advisor layer: AI does the work, human does the relationship
- Financial advisor white-label: advisors offer it as a premium client benefit
- AARP / AAA / senior living community partnerships

### Communication & Notifications
- Twilio SMS for trip reminders and updates
- SendGrid monthly digest emails with personalized suggestions
- WhatsApp Business API for group coordination (optional)

### Booking Integrations (When Ready)
- Expedia Partner Solutions API
- Booking.com Affiliate API
- Viator API (tours and experiences)
- Amadeus Travel API (flights)
- Skyscanner API (price monitoring)

### Data & Intelligence
- Pinecone/Weaviate vector DB for long-term preference memory
- Implicit preference learning (what users click, book, rate)
- Group compatibility scoring across trips
- Travel trend insights from aggregated data

---

## Open Questions
- Business structure if the 20 founders become partners?
- How to handle the transition from "our tool" to "a product for anyone"?
- Pricing: free for users, charge partners only? Or freemium?
- Privacy model for group preference data shared with partners
- Name: is "crab.travel" the final brand or a working name?
