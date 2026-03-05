# Travel API & Data Source Landscape — Exhaustive Research

**Date: March 2026**

This document covers every travel API, data source, deal-finding method, and affiliate program available today. For each, it covers what it provides, pricing, signup process, API quality, and pros/cons for a startup like crab.travel.

---

# TABLE OF CONTENTS

1. [FLIGHTS](#1-flights)
2. [HOTELS](#2-hotels)
3. [ACTIVITIES & EXPERIENCES](#3-activities--experiences)
4. [DEALS & PRICE TRACKING](#4-deals--price-tracking)
5. [AGGREGATOR / AFFILIATE PLATFORMS](#5-aggregator--affiliate-platforms)
6. [SCRAPING & DATA](#6-scraping--data)
7. [CAR RENTALS](#7-car-rentals)
8. [CRUISES](#8-cruises)
9. [TRAVEL INSURANCE](#9-travel-insurance)
10. [WEATHER](#10-weather)
11. [CURRENCY EXCHANGE](#11-currency-exchange)
12. [MAPS & PLACES](#12-maps--places)
13. [RESTAURANTS & DINING](#13-restaurants--dining)
14. [FLIGHT TRACKING (operational, not booking)](#14-flight-tracking)
15. [EVENTS & TICKETING](#15-events--ticketing)
16. [HOW TRAVEL AGENTS ACTUALLY FIND DEALS](#16-how-travel-agents-actually-find-deals)
17. [RECOMMENDED STACK FOR CRAB.TRAVEL](#17-recommended-stack-for-crabtravel)

---

# 1. FLIGHTS

## 1A. GDS Systems (Global Distribution Systems)

GDS systems are the backbone of airline distribution. They connect airlines, hotels, and car rental companies to travel agents and OTAs. Three companies control ~65% of the global flight distribution market.

### Amadeus GDS (Enterprise)
- **URL:** https://amadeus.com / https://developers.amadeus.com
- **What it provides:** Full airline inventory from 400+ airlines, including all major carriers (American, Delta, BA, etc.), NDC content from 35+ airlines, negotiated fares, private deals. ~200 APIs covering flights, hotels, cars, transfers, destination content.
- **Pricing:** Enterprise requires commercial agreement. Setup fee $5,000-$20,000. Transaction fees per booking. Requires IATA/ARC license to issue tickets.
- **Signup:** Must contact sales. Requires business plan, demonstrated volume, and technical capability. Approval takes weeks to months.
- **API quality:** SOAP + REST mix. Mature but complex. 24/7 dedicated support for enterprise clients.
- **Pros:** Most comprehensive content. Only way to get certain airlines. NDC access.
- **Cons:** Expensive. Complex integration. Requires IATA license for ticketing. Not startup-friendly.

### Amadeus Self-Service APIs
- **URL:** https://developers.amadeus.com/self-service
- **What it provides:** Subset of Amadeus content. Flight search, pricing, booking. BUT: missing American Airlines, Delta, British Airways, and most LCCs. Only published GDS rates (no negotiated/private fares). No NDC content.
- **Pricing:** Free test environment. Production free quotas per month:
  - Flight Offers Search: 2,000 free requests/month
  - Flight Offers Price: 3,000 free requests/month
  - Flight Order Management: up to 10,000 free calls
  - Overage: EUR 0.0008 to EUR 0.025 per call depending on endpoint
  - 90% discount on search calls if you're also creating bookings
- **Signup:** Self-serve. Sign up at developers.amadeus.com, get API key instantly. Test immediately.
- **API quality:** REST/JSON. Well-documented. Good SDKs (Python, Node, Java). Rate limits apply.
- **Pros:** Instant access. Free tier for development. Covers full travel lifecycle. Good docs.
- **Cons:** MISSING major airlines (AA, Delta, BA). No LCCs. No NDC. No negotiated fares. Must use consolidator to issue tickets. Content gap vs Enterprise is massive.

### Sabre
- **URL:** https://developer.sabre.com
- **What it provides:** Full GDS content. Flights, hotels, cars, ancillaries. Strong in North American market.
- **Pricing:** Sandbox is free. Production requires commercial agreement. Setup fees + transaction fees. Generally $500-$5,000+/month depending on volume. No public pricing.
- **Signup:** Register on developer portal for sandbox. Production requires contacting sales, business plan, approval process.
- **API quality:** REST/JSON and SOAP/XML. Good developer portal. Self-service guide available.
- **Pros:** Strong North American content. Good hotel and car content too.
- **Cons:** Not public pricing. Enterprise-oriented. Similar IATA requirements for ticketing. Slower approval.

### Travelport (Galileo / Apollo / Worldspan)
- **URL:** https://developer.travelport.com
- **What it provides:** Three GDS systems unified under one API (Universal API). Galileo (global), Apollo (North America), Worldspan (automation-focused). Flights, hotels, cars.
- **Pricing:** Initial integration $4,000-$5,000. Ongoing transaction fees + potential licensing. Generally cheaper than Amadeus/Sabre for smaller agencies.
- **Signup:** Must register and be approved as partner. 15-day certification after docs submitted. Full process 2-4 months.
- **API quality:** REST and SOAP. Good developer network. Certification required.
- **Pros:** Cheapest of the three GDSs. Good for smaller agencies. Unified API across three systems.
- **Cons:** Smaller market share. Same IATA requirements. Not startup-friendly.

---

## 1B. NDC Aggregators (Modern Alternative to GDS)

NDC (New Distribution Capability) is an IATA standard that lets airlines sell directly via API, bypassing the GDS. Airlines can offer personalized deals, bundles, richer ancillaries, photos, and dynamic pricing that the legacy GDS can't support.

### Duffel
- **URL:** https://duffel.com
- **What it provides:** Access to 300+ airlines via a single modern API. NDC direct connections to 20-30+ airlines including American Airlines, United, Air France-KLM, Qantas, British Airways, Lufthansa, Iberia, Singapore Airlines, Air Canada, Cathay Pacific. Also connects to GDS content via Travelport.
- **Pricing:** Pay-as-you-go. No monthly minimums. Revenue comes from commission shared on bookings. Excess search fee of $0.005/search if ratio exceeds 1500 searches per order. 2% FX fee on currency conversion. You can set your own markup.
- **Signup:** Self-serve at duffel.com. Instant test access. Production requires approval.
- **API quality:** REST/JSON. Excellent documentation. Modern, developer-first design. Test environment with real-ish data. Webhooks, SDKs.
- **Pros:** BEST developer experience in the industry. Gets you AA, Delta, BA that Amadeus Self-Service doesn't have. No IATA license needed (Duffel handles ticketing). NDC content. Modern REST API. No minimums.
- **Cons:** Commission model means less margin control. Smaller airline count than full GDS. Search-to-book ratio requirement.

### Kiwi.com / Tequila
- **URL:** https://partners.kiwi.com / https://tequila.kiwi.com
- **What it provides:** 800+ airlines plus trains, buses, ferries, taxis. Virtual interlining (combines carriers that don't normally cooperate into single itineraries). Multi-city, "anywhere" searches, flexible date ranges. Hidden-city-like combinations.
- **Pricing:** Free to build with. Commission model: 3% affiliate commission OR booking-based revenue share.
- **Signup:** Was self-serve, but NEW partnerships are now INVITATION ONLY. Existing partners grandfathered.
- **API quality:** REST. Good search API (location, multi-city, nomad search). Well-documented.
- **Pros:** Virtual interlining is unique and powerful. Massive carrier coverage. Includes ground transport. Great for inspiration/flexible search. Free to use.
- **Cons:** NOW INVITATION ONLY for new partners. Kiwi handles the booking (you're an affiliate, not the seller). Virtual interlining carries risk (Kiwi provides their own guarantee). 3% affiliate commission is low.

### Skyscanner API
- **URL:** https://partners.skyscanner.net
- **What it provides:** Meta-search across all major airlines and OTAs. Price comparison. Flights, hotels, car hire data.
- **Pricing:** Free for approved partners. No per-call fees. Skyscanner earns from redirect commissions.
- **Signup:** Apply at partners.skyscanner.net. Approval takes ~2 weeks. NOT available for small sites or student projects. Must be established travel business with significant traffic. Case-by-case approval.
- **API quality:** REST. Good docs at developers.skyscanner.net.
- **Pros:** Free. Comprehensive price comparison. Well-known brand.
- **Cons:** Very selective approval. Not for early-stage startups. Redirect model (users leave your site). No booking capability.

---

## 1C. Other Flight APIs

### Travelpayouts (Flight API)
- **URL:** https://www.travelpayouts.com
- **What it provides:** Affiliate network aggregating 17+ travel brand APIs. Flight price data (cheapest in last 48 hours), redirects to partner booking sites. Not real-time availability — cached/historical pricing.
- **Pricing:** Free to join. Earn affiliate commissions per booking (varies by partner, typically 1-3%).
- **Signup:** Self-serve registration. Instant API access after approval.
- **API quality:** REST/JSON. Simple. Good for displaying prices and redirecting.
- **Pros:** Free. Easy. Good for content sites showing deals. Multiple brands in one integration.
- **Cons:** Not real-time. Affiliate redirect model only. Low commissions. Can't book on your platform.

### FlightAPI.io
- **URL:** https://www.flightapi.io
- **What it provides:** Flight price and schedule data. Search by route, date, cabin class.
- **Pricing:** Tiered plans. Generally affordable for startups.
- **Signup:** Self-serve via RapidAPI or directly.
- **API quality:** REST/JSON. Simple integration.
- **Pros:** Simple. Quick to integrate.
- **Cons:** Limited inventory vs. the big players. Less documentation.

---

## 1D. ATPCO & Fare Filing

### ATPCO (Airline Tariff Publishing Company)
- **URL:** https://atpco.net / https://devportal.atpco.net
- **What it provides:** The source of truth for airline pricing. 440+ airlines, 351 million active fares, 18 million price changes per day. Fare rules, filing data, routing data. Also provides NDC solutions.
- **Pricing:** Enterprise/commercial agreements only. Very expensive. Not for startups.
- **Signup:** Contact sales. Enterprise onboarding.
- **API quality:** APIs at devportal.atpco.net. Supports EDIFACT, NDC, and modern formats.
- **Pros:** THE definitive fare database. If you need to understand fare rules, this is it.
- **Cons:** Enterprise-only pricing. Complex. Not meant for consumer-facing apps.

---

## 1E. Google Flights / ITA Matrix

### Google QPX / ITA Matrix
- **URL:** https://matrix.itasoftware.com (public tool) / enterprise QPX still exists
- **What it provides:** Google's flight pricing engine (acquired ITA Software). Powers Google Flights. ITA Matrix is the public interface — advanced fare search with routing codes, fare class filtering.
- **Pricing:** QPX Express API was $0.035/query (shut down April 2018). Enterprise QPX still available by contacting Google. ITA Matrix is free to use as a web tool.
- **API quality:** No public API anymore. Enterprise QPX is SOAP-based and requires Google relationship.
- **How Google Flights gets data:** Directly from airlines, GDS feeds, and their own ITA/QPX engine. Google has direct data-sharing agreements with most major airlines.
- **Pros:** ITA Matrix is the best manual fare search tool. Free to use.
- **Cons:** No public API. Must scrape or use enterprise agreement. Scraping is legally risky (see SerpAPI section below).

---

## 1F. How Sites Like Skiplagged and Hopper Get Data

### Skiplagged
- **How it works:** Scrapes airlines and GDS systems for flight data. Finds hidden-city ticketing opportunities (booking a cheaper connecting flight and getting off at the layover). Has been sued by American Airlines, Southwest, United, and Orbitz.
- **Data source:** Web scraping of airline sites + GDS data. No public API.
- **Legality:** Legal for consumers to use. Violates airline contracts of carriage. Skiplagged itself has survived lawsuits.
- **Relevance to us:** Interesting concept but legally risky to replicate. Hidden-city ticketing only works with carry-on luggage and one-way trips.

### Hopper
- **How it works:** Processes 300 billion flight prices/month. Uses 5+ years of historical data, search demand, airline capacity data. ML algorithms predict whether prices will rise or fall. Claims 95% accuracy.
- **Data source:** Direct airline feeds, GDS data, historical price databases. Proprietary.
- **API:** No public API. Hopper Cloud B2B exists for enterprise partners.
- **Relevance to us:** We can't access Hopper's prediction engine. But we could build basic price-tracking by querying flight APIs over time and alerting on drops.

---

# 2. HOTELS

## 2A. Bed Banks & Wholesalers

Bed banks buy hotel inventory at wholesale ("net") rates and resell to travel agencies, OTAs, and platforms via API. They're the backbone of online hotel distribution.

### Hotelbeds (HBX Group)
- **URL:** https://developer.hotelbeds.com
- **What it provides:** 250,000+ hotels globally. World's largest B2B bed bank. 80,000+ bookings and 14 million searches daily. Also has Activities and Transfers API suites.
- **Pricing:** Net rate model (you add your markup). Commercial agreement required for production. Test environment has 50 requests/day limit.
- **Signup:** Self-serve registration gives you evaluation API keys instantly. 50 req/day test limit. Production requires contacting sales for commercial terms.
- **API quality:** REST (APItude suite). Well-documented. Sandbox available.
- **Pros:** Largest bed bank. Massive inventory. Activities and transfers too. Well-documented API.
- **Cons:** Production requires commercial agreement. 50 req/day test limit is very restrictive. Net rates require you to handle pricing/margin.

### RateHawk (Emerging Technologies Group)
- **URL:** https://www.ratehawk.com / https://blog.ratehawk.com
- **What it provides:** 2.6 million+ accommodations. B2B platform for travel agencies. Competitive wholesale pricing. 100+ tech platforms connected.
- **Pricing:** Flexible models: net prices, commission, or affiliate. Free trial available. Sandbox environment launched Q4 2025.
- **Signup:** Contact sales for API credentials. Sandbox available for new partners.
- **API quality:** REST. New sandbox environment. Content API for hotel data.
- **Pros:** Huge inventory (2.6M properties). Flexible business models. Growing fast. Sandbox for testing.
- **Cons:** Still requires sales contact. Newer than Hotelbeds.

### WebBeds
- **URL:** https://www.webbeds.com
- **What it provides:** 370,000-500,000+ hotels. Second-largest bed bank globally. 30,000 directly contracted hotels. 14,000 destinations, 170+ countries.
- **Pricing:** Net rate model. Commercial agreement required.
- **Signup:** Must contact WebBeds directly. No public documentation. API docs shared after commercial terms finalized.
- **API quality:** XML-based. Not publicly accessible. Documentation shared after onboarding.
- **Pros:** Second-largest. Good directly-contracted inventory.
- **Cons:** Closed ecosystem. No self-serve. XML (not REST). Less developer-friendly.

### TBO Holidays
- **URL:** https://www.tbo.com / https://www.tboholidays.com
- **What it provides:** 700,000+ hotels and apartments. Designed for small and medium travel agents.
- **Pricing:** Contact partners@tboholidays.com. Commercial terms.
- **Signup:** Contact sales. Partnership application.
- **API quality:** XML API. User-friendly interface.
- **Pros:** Large inventory. Aimed at SMB agents.
- **Cons:** Contact-only. XML API.

---

## 2B. OTA APIs & Affiliate Programs

### Booking.com
- **URL:** https://www.booking.com/affiliate-program
- **What it provides:** World's largest hotel OTA. Millions of properties.
- **Pricing:** Free API access for approved affiliates. Commission model:
  - Tier 1 (0-50 stays): 25% commission
  - Tier 2 (51+ stays): 30% commission
  - Commission is on Booking.com's margin, not the room rate
- **Signup:** Apply online at booking.com/affiliate-program. Approval required. NOTE: Connectivity partner applications currently PAUSED due to T&C updates.
- **API quality:** REST. Good documentation for affiliates.
- **Pros:** Massive inventory. Strong brand. Good commission rates. Free.
- **Cons:** Affiliate model (redirect to booking.com). Connectivity applications paused. 2-month payout delay.

### Expedia Group (Rapid API)
- **URL:** https://partner.expediagroup.com
- **What it provides:** Access to Expedia's full inventory including hotels, vacation rentals (Vrbo — 900,000+ properties). Commissionable rates including exclusive distribution rates and opaque package rates.
- **Pricing:** Commission-based. Average 20% off consumer rates for package deals. Competitive rates.
- **Signup:** Apply for partnership. Requires approval.
- **API quality:** REST/JSON (Rapid API). Modern. Well-documented.
- **Pros:** Includes Vrbo/vacation rentals. Competitive rates. One integration for hotels + vacation rentals.
- **Cons:** Requires partnership approval. Commission model.

### Expedia TAAP (Travel Agent Affiliate Program)
- **URL:** https://www.expediataap.com
- **What it provides:** Travel agent portal. Package rates at 350,000+ properties averaging 20% off consumer rates. Commission on bookings.
- **Pricing:** Commission-based. Incentives and promotions available.
- **Signup:** Apply as travel agent.
- **API quality:** Web portal primarily. API available for larger partners.
- **Pros:** Good rates. Easy for travel agent model.
- **Cons:** Primarily a portal, not a pure API play.

### Priceline Partner Solutions
- **URL:** https://pricelinepartnersolutions.com
- **What it provides:** 980,000+ hotel properties. Also flights, rental cars, vacation packages. Dynamic rates.
- **Pricing:** Commission-based partnership.
- **Signup:** Apply for partnership. Requires approval and API credentials.
- **API quality:** REST/HTTPS. Fast, cached rates. Good performance.
- **Pros:** Massive inventory. Includes Rentalcars content. Good rates.
- **Cons:** Partnership approval required. Not self-serve.

### Agoda
- **URL:** Partner-only access
- **What it provides:** Hotels optimized for Asia-Pacific. Mobile-first. JSON Search API, Content API, CDS APIs.
- **Pricing:** Commission-based partnership.
- **Signup:** Must establish partnership with Agoda.
- **API quality:** JSON REST. Mobile-optimized.
- **Pros:** Best for Asia-Pacific inventory.
- **Cons:** Not publicly available. Partnership required. Asia-focused.

---

## 2C. Startup-Friendly Hotel APIs

### LiteAPI (by Nuitee)
- **URL:** https://www.liteapi.travel / https://nuitee.com
- **What it provides:** 2 million+ hotels worldwide. Search by hotel ID, city, coordinates, Place ID, IATA code, or natural language. Built for developers.
- **Pricing:** Transparent pricing. Free tier available (details require checking their site). Commission model on bookings.
- **Signup:** Self-serve at liteapi.travel. Quick onboarding — "zero to live in hours."
- **API quality:** REST/JSON. Excellent documentation. Async support. Developer-friendly. Clean data.
- **Pros:** MOST STARTUP-FRIENDLY hotel API. Self-serve. Modern REST. Good docs. Quick integration. 2M+ hotels.
- **Cons:** Newer company. Commission reduces margins. Free tier details unclear.

### Impala
- **URL:** https://getimpala.com / https://docs.getimpala.com
- **What it provides:** Single API connecting to hotel PMS systems directly. REST API with test/live modes. Sandbox with test hotel ("The Charleston").
- **Pricing:** Contact-based. Previously self-serve.
- **Signup:** CURRENTLY NOT ACCEPTING NEW CUSTOMERS. Waitlist available.
- **API quality:** REST. Well-documented. Test and live modes.
- **Pros:** Direct hotel PMS connection (bypasses bed banks). Clean API.
- **Cons:** NOT ACCEPTING NEW CUSTOMERS. Waitlist only.

---

## 2D. Hotel Chain Direct APIs

### Hilton
- **URL:** https://developer.hilton.io
- **What it provides:** Access to Hilton properties. Rates, availability, content.
- **Signup:** Developer Hub registration. Partnership required for full access.
- **API quality:** Modern. Developer hub with API catalog.

### Marriott, IHG, Hyatt
- These chains primarily distribute through GDS and OTA partnerships. No widely-available public developer APIs for startups. Integration is through GDS connections or bed banks. Travel agents get rates 35-50% off published rates through direct agent programs.

---

## 2E. Vacation Rental APIs

### Airbnb
- **URL:** No public developer API for searching/booking
- **What it provides:** N/A for search. API exists only for property managers (channel management).
- **Signup:** Must be a property management software provider.
- **API quality:** N/A for our use case.
- **How to access Airbnb data:** Scraping (legally grey), or through data providers like AirDNA ($). AirDNA offers the Airbnb API for extracting short-term rental data.
- **Cons:** No way to legally search/book Airbnb inventory via API. Must redirect users.

### Vrbo (via Expedia Rapid API)
- **URL:** https://integration-central.vrbo.com / via Expedia Rapid API
- **What it provides:** 900,000+ vacation rental properties. Available through Expedia Rapid API.
- **Pricing:** Through Expedia partnership (commission-based).
- **Signup:** Through Expedia partner program. Strict requirements around payments, booking rules, compliance.
- **API quality:** REST via Expedia Rapid API.
- **Pros:** Huge vacation rental inventory. Accessible through Expedia.
- **Cons:** Not direct access. Must go through Expedia partnership.

---

# 3. ACTIVITIES & EXPERIENCES

## 3A. Major Platforms

### Viator (TripAdvisor)
- **URL:** https://partnerresources.viator.com / https://docs.viator.com/partner-api/
- **What it provides:** Largest tours and activities marketplace. Hundreds of thousands of experiences globally.
- **Pricing:** 8% affiliate commission on bookings within 30-day cookie window. Free to join.
- **Signup:** Self-serve at partnerresources.viator.com. Basic API access immediately. Full + Booking access requires Viator approval.
- **API quality:** REST/JSON. Three access levels:
  - **Basic Access:** Instant. Condensed product summaries. No pre-authorization needed.
  - **Full Access:** Requires Viator approval. Full descriptions, images, pricing.
  - **Full + Booking:** Requires approval. Customers can transact on YOUR site instead of redirecting to viator.com.
- **Payout:** Monthly (bank transfer, $50 minimum) or weekly (PayPal, no minimum).
- **Pros:** Largest inventory. Three access tiers. Full+Booking means users stay on your site. Free. 8% commission. Well-documented.
- **Cons:** 8% commission isn't huge. Viator takes 20% from operators (keeps 12%, gives 8% to affiliates). Approval needed for advanced access.

### GetYourGuide
- **URL:** https://partner.getyourguide.support / https://integrator.getyourguide.com
- **What it provides:** Major tours and activities marketplace. Global coverage.
- **Pricing:** 8% affiliate commission (via Travelpayouts) with 31-day cookie.
- **Signup:** Free to join affiliate program. BUT API access has traffic requirements:
  - **Basic (Teaser):** Minimum 100,000 monthly visits
  - **Reading Access:** Minimum 1 million visits and 300 monthly bookings
- **API quality:** REST. OpenAPI spec on GitHub. Good documentation.
- **Pros:** Good inventory. OpenAPI spec.
- **Cons:** API access requires significant traffic (100K+ visits/month). Not startup-friendly for API. Affiliate link/widget is the realistic starting point.

### Klook
- **URL:** Via Travelpayouts or direct partnership
- **What it provides:** Tours, activities, experiences. Strong in Asia-Pacific.
- **Pricing:** 2-5% affiliate commission via Travelpayouts. 30-day cookie.
- **Signup:** Via Travelpayouts or direct partnership application.
- **Pros:** Good Asia-Pacific coverage.
- **Cons:** Low commission (2-5%). Asia-focused.

### Tiqets
- **URL:** Via Travelpayouts or direct
- **What it provides:** Museum tickets, attractions, experiences. Strong in Europe.
- **Pricing:** 8% affiliate commission via Travelpayouts. 30-day cookie.
- **Signup:** Via Travelpayouts or direct.
- **Pros:** Good for cultural attractions.
- **Cons:** Narrower focus (museums/attractions).

### Musement (TUI)
- **URL:** https://affiliate.musement.com / https://partner.tuimusement.com
- **What it provides:** 40,000+ tours and attractions in 80 countries, 2000+ cities.
- **Pricing:** 50% of advertiser's margin per order (affiliate). Backed by TUI.
- **Signup:** Application required. Short form. API available via TUI Musement partner portal.
- **API quality:** REST. Real-time availability, pricing, instant booking.
- **Pros:** High commission (50% of margin). TUI backing. Good inventory.
- **Cons:** Approval required. Smaller than Viator/GYG.

### Civitatis
- **URL:** https://www.civitatis.com/en/affiliates/
- **What it provides:** Tours, activities, free walking tours. Strong in Spanish-speaking markets and Europe.
- **Pricing:** Affiliate commission. API and feed available.
- **Signup:** Affiliate application. Direct links, widgets, banners, API, and feed options.
- **Pros:** Good European/Latin American coverage. Multiple integration options.
- **Cons:** Smaller global coverage.

### Headout
- **URL:** https://www.headout.com (check for partner program)
- **What it provides:** Tours, activities, attractions. Mobile-first.
- **Pricing:** Contact for partnership details.
- **Signup:** Direct contact required.
- **Cons:** Limited public information on API/affiliate program.

---

## 3B. Activity API Standards

### OCTO API (via Ventrata)
- **URL:** https://docs.ventrata.com
- **What it provides:** Open standard API for tours and activities. Major resellers (Viator, GetYourGuide, Klook, Headout, Go City, TUI Musement, Tiqets) have integrated with Ventrata's OCTO API.
- **Pricing:** Free for resellers working with Ventrata clients.
- **Relevance:** If you're a tour operator, this is how you connect to multiple resellers. As an aggregator, you'd connect to the reseller APIs (Viator, GYG, etc.) instead.

---

# 4. DEALS & PRICE TRACKING

## 4A. How Deal Sites Find Deals

### Going (formerly Scott's Cheap Flights)
- **URL:** https://www.going.com
- **How they find deals:** Combination of software monitoring + human "Flight Experts" who manually review deals. They scan for:
  - Pricing glitches / error fares
  - Currency mismatches
  - Competitive fare wars between airlines
  - Flash sales
  - Human data entry errors
  - Unusual routing discounts
- **Do they have an API?** NO. Subscription email/notification service only.
- **Pricing for consumers:** Free (limited deals), Premium ($49/year for all deals including error fares and premium cabin).
- **Relevance to us:** We can't access their deal feed. But we can replicate their approach: monitor flight APIs for price anomalies, track historical prices, alert on significant drops.

### Secret Flying
- **URL:** https://www.secretflying.com
- **How they find deals:** Community-powered deal finding + editorial curation. Focus on error fares and extreme discounts.
- **API?** NO.
- **Relevance:** Content inspiration only.

### Dollar Flight Club
- **URL:** https://dollarflightclub.com
- **How they find deals:** Automated scanning throughout the day for flash sales, error fares, and airline competition price drops. Email/SMS alerts.
- **API?** NO public API.
- **Pricing:** Subscription service.

### FareDrop (now Daily Drop Pro)
- **URL:** https://www.dailydrop.pro (rebranded)
- **How they find deals:** Tracks thousands of routes. Uses predictive analytics and historical pricing data to estimate best booking times. Has shifted focus to points-based searches and credit card reward optimization.
- **API?** NO public API.
- **Pricing:** Plans start at $4.99/month for 15 routes tracked.

## 4B. Building Your Own Deal Finding

**The basic approach used by all deal services:**

1. **Continuous Monitoring:** Query flight APIs (Amadeus, Duffel, Kiwi) for prices on target routes on a schedule
2. **Historical Comparison:** Store prices over time, detect when current price is X% below 30/60/90-day average
3. **Error Fare Detection:** Flag prices that are >50% below typical for a route
4. **Fare Rule Analysis:** Look for unusual fare rules that indicate pricing mistakes
5. **Competition Monitoring:** Track when multiple airlines drop prices on same route (fare war)
6. **Seasonal Pattern Matching:** Know that certain routes have predictable cheap seasons

**Technical requirements:**
- Database of historical prices (PostgreSQL with time-series extension, or InfluxDB)
- Scheduled API calls (cron jobs or cloud scheduler)
- Alert logic (simple threshold-based alerts)
- Flight API access (Amadeus Self-Service or Duffel would work)

---

## 4C. Price Prediction / Tracking

### Hopper (internal only)
- 300 billion flight prices/month processed
- 5+ years historical data
- Not available as an API for third parties
- Hopper Cloud B2B exists for enterprise partnerships only

### Google Flights Price Tracking
- Google offers price tracking/alerts through Google Flights
- No API access for this feature
- Would need to scrape (risky, see Section 6)

### Build Your Own
- Use Amadeus or Duffel APIs to collect prices over time
- Apply simple ML (time series forecasting) or rule-based logic
- Store in time-series database
- Much simpler than Hopper but functional for "buy now vs wait"

---

# 5. AGGREGATOR / AFFILIATE PLATFORMS

## 5A. Travelpayouts

- **URL:** https://www.travelpayouts.com
- **What it provides:** ONE platform aggregating 100+ travel brand affiliate programs. Flights, hotels, car rental, insurance, tours. Partners include: Booking.com, Expedia, Kiwi.com, Viator, GetYourGuide, Klook, Tiqets, Agoda, WayAway, and many more.
- **APIs available from 17+ programs:**
  - Flight price data (cached, not real-time — cheapest tickets in last 48 hours)
  - Hotel redirect links
  - Activity affiliate links
  - Booking statistics API
  - Partner link conversion API
- **Pricing:** Free to join. Commission varies by program:
  - Flights: typically 1-3% affiliate commission
  - Hotels: varies (Booking.com pays 25-30% of their margin)
  - Activities: 2-8% depending on platform
- **Signup:** Self-serve registration at travelpayouts.com
- **API quality:** REST/JSON. Simple. Good for price display and redirect.
- **White Label:** Available for flights and hotels (customer books on your site, styled as your brand)
- **Pros:** EASIEST way to monetize travel content. One platform, many brands. Free. Good dashboard. White label option.
- **Cons:** Affiliate model (low margins). Flight data is cached/historical. Not real-time booking API. Low commissions.

## 5B. CJ Affiliate (Commission Junction)
- **URL:** https://www.cj.com
- **What it provides:** Large affiliate network with travel programs including CheapOair, Hawaiian Airlines, Fairmont/Raffles/Swissotel (5% per stay), and others.
- **Pricing:** Free for publishers to join. Commission varies by advertiser.
- **Signup:** Self-serve. Apply to individual programs within CJ.
- **Pros:** Established network. Many travel brands. Good tracking.
- **Cons:** Must apply to each program separately. Generic affiliate platform.

## 5C. Impact
- **URL:** https://impact.com
- **What it provides:** Affiliate marketing platform. Strong in travel, personal finance, retail. Travel brands include various hotel chains, airlines, and OTAs.
- **Pricing:** Free for publishers.
- **Signup:** Self-serve registration. Apply to individual brands.
- **Pros:** Modern platform. Good for travel + finance.
- **Cons:** Must find and apply to each travel brand.

## 5D. Rakuten Advertising
- **URL:** https://rakutenadvertising.com
- **What it provides:** Affiliate network. Features Booking.com, Rakuten Travel (Japan/Asia-focused). Various hotel and travel brands.
- **Pricing:** Free for publishers. Commissions up to 10% depending on program.
- **Signup:** Self-serve.
- **Pros:** Good for Asia/Japan travel. Booking.com available.
- **Cons:** Less travel-focused than Travelpayouts.

## 5E. Awin
- **URL:** https://www.awin.com
- **What it provides:** Global affiliate network. Travel programs include Etihad, hotels, OTAs.
- **Pricing:** Publisher deposit ($1-5, refunded). Commission varies.
- **Signup:** Self-serve with deposit.

---

# 6. SCRAPING & DATA

## 6A. SerpAPI

- **URL:** https://serpapi.com
- **What it provides:** Structured scraping of Google search results including Google Flights, Google Hotels, Google Travel Explore, Google Maps. Returns JSON with prices, ratings, images, etc.
- **Google Hotels data:** hotel names, check-in/out times, prices, sources, ratings, images
- **Google Flights data:** flight times, prices, airlines, layovers in structured JSON
- **Pricing:** Plans from $50/month (5,000 searches) to $500/month (50,000 searches) and enterprise.
- **Signup:** Self-serve at serpapi.com
- **API quality:** REST/JSON. Well-documented. Multiple search engines supported.
- **MAJOR LEGAL WARNING:** Google sued SerpAPI in December 2025, alleging DMCA violations and circumvention of Google's "SearchGuard" anti-scraping system. This is an active lawsuit as of March 2026.
- **Pros:** Gets Google Flights/Hotels data that has no official API. Structured JSON. Easy to use.
- **Cons:** ACTIVE LAWSUIT FROM GOOGLE. Legally risky. Dependent on Google not blocking. Could shut down. Prices may not be bookable.

## 6B. ScrapingBee (you already have this)

- **URL:** https://www.scrapingbee.com
- **What it provides:** General web scraping API. Handles proxies, headless browsers, JavaScript rendering.
- **Pricing:** Plans from $49/month.
- **Can you scrape travel sites?** Technically yes — Kayak, Skyscanner, Google Hotels, individual hotel sites. ScrapingBee handles the proxy rotation and browser rendering.
- **Legal considerations:** Scraping publicly available data is generally considered legal per US precedent (hiQ v. LinkedIn). BUT: violating Terms of Service is grey area. Google is actively suing scrapers. Airlines have sued Skiplagged. Travel sites actively deploy anti-bot measures.
- **Pros:** You already have it. Can scrape anything. Good proxy network.
- **Cons:** Legal risk for travel sites specifically. Anti-bot detection. Results require parsing. Not a structured travel API.

## 6C. Legal Considerations for Scraping

- **US law:** hiQ v. LinkedIn (2022) — scraping public data is not a CFAA violation. BUT: can still violate TOS, DMCA, or state laws.
- **Google specifically:** Actively suing SerpAPI (Dec 2025). Deployed SearchGuard anti-scraping system (Jan 2025). Invested "millions of dollars and tens of thousands of person hours" in anti-scraping.
- **Airlines:** Have sued multiple scrapers. Contracts of carriage prohibit automated queries.
- **Recommendation:** Use official APIs where possible. Scraping as a last resort or for data that truly has no API. Don't build core functionality on scraped data that could disappear.

---

# 7. CAR RENTALS

### Rentalcars.com (Priceline Partner Network)
- **URL:** Via Priceline Partner Network
- **What it provides:** Car rental data across 60,000 locations in 165+ countries. Aggregates Alamo, Avis, Sixt, Hertz, etc.
- **Options:** Banner/widget integration OR full Rentalcars Connect API partner.
- **Pricing:** Affiliate commission model.
- **Signup:** Apply via Priceline Partner Network or Travelpayouts.

### Auto Europe
- **URL:** https://www.autoeurope.com (contact for partnership)
- **What it provides:** Car rental wholesaler in 180+ countries. Integrates Alamo, Avis, Sixt, Hertz, etc.
- **Pricing:** Partnership/commission model.
- **Signup:** Contact Auto Europe for API access.

### Discover Cars
- **URL:** https://www.discovercars.com
- **What it provides:** Car rental comparison.
- **Pricing:** 70% share from offers, 30% from revenue per rented vehicle (for affiliates).
- **Signup:** Affiliate program available.

### Booking.com Demand API (Cars)
- **URL:** https://developers.booking.com
- **What it provides:** Car search endpoints. POST requests with availability and pricing.
- **Pricing:** Part of Booking.com partnership.

### Amadeus / GDS Car APIs
- All three GDS systems include car rental inventory from major chains.

---

# 8. CRUISES

### Cruise API Providers
- **Traveltek Cruise API:** https://www.traveltek.com — Aggregates rates, promotions, availability, itineraries from multiple cruise lines (Royal Caribbean, Carnival, MSC, Norwegian) in a single API call.
- **CruiseHost:** https://www.cruise-api.com — Dedicated cruise booking API.
- **Custom integrations:** Most cruise APIs are behind partnership agreements. No self-serve.

### Cruise Affiliate Programs
- **Royal Caribbean:** 4% commission, $2,500 average order, 45-day cookie. Via TradeDoubler. ($100+ per sale typical.)
- **CruiseDirect:** Affiliate program at cruisedirect.com/affiliates.
- **Carnival, Norwegian, Celebrity:** All have affiliate programs via various networks (CJ, Impact, ShareASale).
- **Commissions:** Generally 3-5% of cruise booking value. Given average cruise cost ($2,000-5,000), this is $60-250 per booking.

### Reality Check
- Cruise APIs are NOT self-serve. Every cruise line requires partnership/contract.
- Most startups use affiliate links rather than API integration for cruises.
- The affiliate model works well given high order values.

---

# 9. TRAVEL INSURANCE

### Cover Genius (XCover API)
- **URL:** https://covergenius.com / XCover API
- **What it provides:** Dynamically bundled travel insurance. Parametric delay coverage. Policies for any source/destination. Claims processing automation. AI-enabled recommendations.
- **Pricing:** Commission on policies sold.
- **Signup:** Partnership application.
- **API quality:** REST. Well-documented.
- **Pros:** Fully embedded. Handles everything (design, claims, payments, support). Modern.
- **Cons:** Partnership required.

### Allianz Travel Insurance
- **URL:** https://developers.allianz-trade.com
- **What it provides:** Travel insurance plans (Deluxe, Preferred, Essential). REST API for quotes and booking.
- **Pricing:** Commission on policies.
- **Signup:** Developer portal registration.
- **API quality:** REST. JSON and XML support.

### Travel Insurance as a Revenue Stream
- Embedding travel insurance at checkout is a proven revenue model.
- Commission is typically 15-30% of the insurance premium.
- Average travel insurance policy is $50-150.
- High margin, low effort if API integration is clean.

---

# 10. WEATHER

### OpenWeatherMap
- **URL:** https://openweathermap.org
- **Pricing:** Free tier: 60 calls/minute, 5-day forecast, weather alerts. Paid from $0 (limited) to enterprise.
- **Signup:** Self-serve. Instant API key.
- **API quality:** REST/JSON. Simple. Well-documented.
- **Pros:** Free tier works for basic needs. Well-known. Good enough for trip planning.
- **Cons:** Limited free tier. Location-based pricing scales up fast. No historical data on free tier.

### Visual Crossing
- **URL:** https://www.visualcrossing.com
- **Pricing:** 1,000 records/day FREE. Then $0.0001/record. Monthly plans available.
- **Signup:** Self-serve.
- **API quality:** REST. Good documentation.
- **Pros:** MORE GENEROUS FREE TIER than OpenWeatherMap. Historical data included. Cheaper at scale.
- **Cons:** Less well-known.

### Tomorrow.io
- **URL:** https://www.tomorrow.io
- **Pricing:** Free: 500 API calls/day. 80+ data layers.
- **Signup:** Self-serve.
- **API quality:** REST. Top-rated interface.
- **Pros:** Most data layers. Best-rated interface. Good free tier.
- **Cons:** Paid tiers can be expensive.

### Recommendation: Visual Crossing
Best free tier (1000 records/day), cheapest at scale, includes historical data for seasonal travel planning.

---

# 11. CURRENCY EXCHANGE

### ExchangeRate-API
- **URL:** https://www.exchangerate-api.com
- **Pricing:** Free open access (1 request/day without rate limit, or 1/hour safely). Paid plans for more.
- **Signup:** No key required for open access. Key for paid tiers.
- **Pros:** Simplest free option. No key needed.

### Fixer
- **URL:** https://fixer.io
- **Pricing:** Free tier available. 170+ currencies. Updates every 60 seconds.
- **Signup:** Self-serve.

### Open Exchange Rates
- **URL:** https://openexchangerates.org
- **Pricing:** Free tier: 1,000 requests/month, hourly updates, 200+ currencies.
- **Signup:** Self-serve.

### GitHub Exchange API (fawazahmed0)
- **URL:** https://github.com/fawazahmed0/exchange-api
- **Pricing:** COMPLETELY FREE. No rate limits. 200+ currencies.
- **Signup:** None. Public API.
- **Pros:** Free. No limits. Open source.
- **Cons:** Community-maintained. No SLA.

### Recommendation: Open Exchange Rates or GitHub Exchange API
Open Exchange Rates for reliability, GitHub API for zero cost.

---

# 12. MAPS & PLACES

### Google Maps / Places API
- **URL:** https://developers.google.com/maps
- **Pricing (post-March 2025):** New category-based free tiers:
  - Essentials: 10,000 free API calls/month per SKU
  - Pro and Enterprise tiers have different pricing
  - Pay-as-you-go: $2-$30 per 1,000 requests depending on SKU
- **Signup:** Self-serve. Google Cloud project + API key.
- **API quality:** Excellent. REST/JSON. Best documentation. Most complete.
- **Pros:** Most comprehensive POI data. Best geocoding. Best directions. Industry standard.
- **Cons:** Expensive at scale. New pricing model (2025) is complex. Can add up fast.

### Yelp Fusion API
- **URL:** https://business.yelp.com/data/products/places-api/
- **Pricing:** NO free tier anymore. Plans: Starter $7.99/1000 calls, Plus $9.99, Enterprise $14.99. 30-day free trial (5,000 calls).
- **Signup:** Self-serve.
- **API quality:** REST/JSON. Good for restaurant/business reviews and photos.
- **Pros:** Good review data. Restaurant-focused.
- **Cons:** No longer free. Pricing controversy (developers were angry about the change). Limited data per call.

---

# 13. RESTAURANTS & DINING

### OpenTable
- **URL:** https://docs.opentable.com / https://dev.opentable.com
- **What it provides:** Restaurant reservations. NOT an open API — must become affiliate.
- **Signup:** Apply for affiliate program. If approved, get credentials. Build integration. Submit to OpenTable for testing before production.
- **API quality:** REST. Sandbox available.
- **Pros:** Market leader in reservations.
- **Cons:** Not open. Affiliate approval required. Testing/approval cycle.

### Resy
- **URL:** No public developer API
- **What it provides:** Restaurant reservations (owned by American Express).
- **API?** No public API for third parties.
- **Workaround:** Deep links to Resy pages.

### Yelp (restaurant data)
- See Section 12. Yelp Fusion API provides restaurant info, reviews, photos, but NOT reservations.

### Google Places (restaurant data)
- See Section 12. Good for discovery but NOT reservations.

### Recommendation for Dining
- Use Google Places for restaurant discovery (free tier)
- Deep link to OpenTable/Resy for reservations
- Apply for OpenTable affiliate if reservations are core

---

# 14. FLIGHT TRACKING

These APIs track live flights (operational data), NOT for booking.

### FlightAware AeroAPI
- **URL:** https://www.flightaware.com/aeroapi
- **Pricing:** Free: 500 calls/month (personal use). $100/month for 10,000 calls (B2C). $1,000/month for 100,000 calls (B2B).
- **Signup:** Self-serve developer portal.
- **API quality:** REST. Well-documented. Trusted industry data.
- **Pros:** Gold standard for flight tracking. Real-time data.
- **Cons:** Expensive for commercial use. Not for booking.

### AviationStack
- **URL:** https://aviationstack.com
- **Pricing:** Free: 100 calls/month. $49.99/month for 10,000 calls. Up to $499.99/month.
- **Signup:** Self-serve.
- **API quality:** REST/JSON. Simple. 30-60 second delay.
- **Pros:** Cheapest option. Good free tier for testing. Simple.
- **Cons:** Not as reliable as FlightAware. Lower data quality.

### Relevance to crab.travel
- Useful for "your flight lands at 3pm, here's your transfer/activity at 5pm" type features.
- Not needed for MVP.

---

# 15. EVENTS & TICKETING

### Ticketmaster Discovery API
- **URL:** https://developer.ticketmaster.com
- **Pricing:** FREE. 5,000 calls/day. 5 requests/second rate limit.
- **Signup:** Self-serve. Get API key instantly.
- **API quality:** REST/JSON. Well-documented.
- **Pros:** Free. Major events, concerts, sports. Good coverage. Affiliate program for commissions.
- **Cons:** Mostly concerts/sports (not local community events).

### Eventbrite API
- **URL:** https://www.eventbrite.com/platform/api
- **Pricing:** Free: 500 requests/day. Good for event discovery.
- **Signup:** Self-serve.
- **API quality:** REST/JSON. Good documentation.
- **Pros:** Free tier. Great for local/community events. Wide variety. Good for retiree use case.
- **Cons:** 500 req/day limit. Event types vary widely in quality.

### SeatGeek
- **URL:** Developer API available
- **Pricing:** Not publicly detailed. Dual-sided model (consumer + enterprise).
- **Signup:** Apply for developer access.
- **API quality:** REST/JSON.
- **Pros:** Good for sports/concert events.
- **Cons:** Limited public pricing info.

### Meetup API
- **URL:** https://www.meetup.com/api/
- **What it provides:** Local group events, community meetups.
- **Pros:** Great for retiree use case (local activities, interest groups).
- **Cons:** Requires Meetup Pro or API access. Less standardized data.

---

# 16. HOW TRAVEL AGENTS ACTUALLY FIND DEALS

This is the crucial knowledge that bridges the gap between APIs and actual deal-finding.

## Hotel Deals
- **Bed banks** (Hotelbeds, RateHawk, WebBeds) provide net rates 20-40% below published prices
- **Chain agent programs** give 35-50% off published rates (IHG gives 35% off Best Flex; Marriott up to 50%)
- **Consolidators** like Major Travel aggregate wholesale rates
- **Agent markup:** Buy at net rate, sell at markup. Typical margin 15-25% on hotels.

## Flight Deals
- **Airline consolidators** (Sky Bird, Centrav, TravelAgentMall) buy bulk inventory from airlines at 10-70% below published fares
- **GDS access** lets agents see all fare classes including deeply discounted buckets
- **Net fares** accessed through GDS, NDC, or consolidator platforms
- **Agent commission:** Mark up the net fare and keep the difference
- **WINGS Booking Platform:** Used by some consolidators for agent access

## Key Consolidators
- **Sky Bird Travel:** https://skybirdtravel.com — 15% commission on pre-paid hotels + airfare commissions
- **Centrav:** https://www.centrav.com — Major airline consolidator
- **TravelAgentMall:** https://www.travelagentmall.com — Airline consolidator

## Activities / Experiences
- **Operator rates:** Travel agents get 20-30% off retail from activity operators
- **Viator takes 20%** from operators, gives 8% to affiliates
- **GetYourGuide takes 20-30%** from operators

## The Real Economics
For a platform like crab.travel:

| Channel | Typical Margin | Effort |
|---------|---------------|--------|
| Hotel bed bank (net rates) | 15-25% | High (commercial agreement, net rate handling) |
| Hotel affiliate (Booking.com) | 25-30% of Booking's margin (~4-6% of room cost) | Low (affiliate link) |
| Flight consolidator (net fares) | 5-15% | High (consolidator relationship, IATA possible) |
| Flight affiliate (Travelpayouts) | 1-3% | Very low (redirect link) |
| Activity affiliate (Viator) | 8% | Low (API integration) |
| Activity bed bank (Musement) | 50% of margin (~10-15% of price) | Medium |
| Cruise affiliate | 3-5% of $2K-5K booking | Very low (link) |
| Travel insurance embed | 15-30% of $50-150 premium | Medium (API integration) |

---

# 17. RECOMMENDED STACK FOR CRAB.TRAVEL

Based on all the above research, here's what makes sense for a startup building a group trip coordination platform:

## Phase 1: MVP (affiliate model, fast to market)

### Flights
- **Primary: Duffel** — Best developer experience, gets you AA/Delta/BA, no IATA needed, pay-per-booking, modern REST API. Start here.
- **Secondary: Amadeus Self-Service** — Free tier for testing, backup data source for pricing. Missing major airlines but good for airport/destination data APIs.
- **Monetization: Travelpayouts** — Easiest flight affiliate if you just want to show prices and redirect. Zero risk.

### Hotels
- **Primary: LiteAPI (Nuitee)** — Most startup-friendly. Self-serve. 2M+ hotels. Modern REST. Quick integration.
- **Secondary: Booking.com Affiliate** — If you just need links/redirects.
- **Backup: Google Hotels via scraping** — You already have ScrapingBee, but be aware of legal risks.

### Activities
- **Primary: Viator** — Best API, three access levels, can do on-site booking with Full+Booking access, 8% commission, free to join, instant basic access.
- **Secondary: Travelpayouts** — Aggregates Viator, GYG, Klook, Tiqets, Musement in one platform.

### Events (for retiree use case)
- **Eventbrite API** (free, 500 req/day) + **Ticketmaster API** (free, 5000 req/day)

### Other
- **Weather:** Visual Crossing (1000 free records/day)
- **Maps:** Google Places (10K free calls/month)
- **Currency:** Open Exchange Rates or GitHub Exchange API (free)

## Phase 2: Scale (commercial agreements, better margins)

### Flights
- Upgrade to Duffel enterprise pricing
- Add Kiwi/Tequila if invitation becomes available (virtual interlining is great for group trips)
- Consider Amadeus Enterprise if volume justifies it

### Hotels
- Add Hotelbeds (250K hotels, net rates, better margins than affiliate)
- Add RateHawk (2.6M properties, flexible models)
- Expedia Rapid API for Vrbo/vacation rental inventory

### Activities
- Upgrade Viator to Full+Booking access
- Add GetYourGuide API (once you hit 100K visits/month)
- Add Musement/Civitatis for European coverage

### Revenue Optimization
- Embed Cover Genius travel insurance at checkout (15-30% commission)
- Add cruise affiliate links for retiree demographic
- Price comparison across multiple hotel sources to show best deal

## Phase 3: Full Travel Platform

- GDS integration (Travelport, cheapest of the three) for deepest content
- Airline consolidator relationships for net fares
- Hotel bed bank arbitrage (compare rates across Hotelbeds, RateHawk, WebBeds)
- Build proprietary deal-finding engine (historical price tracking + anomaly detection)
- NDC direct connections to airlines for best fares and ancillary revenue

---

## QUICK REFERENCE: API SIGNUP SPEED

| API | Time to First Call |
|-----|-------------------|
| Amadeus Self-Service | Minutes (self-serve) |
| Duffel | Minutes (self-serve test) |
| Travelpayouts | Hours (approval) |
| Viator Basic | Hours (self-serve) |
| LiteAPI | Hours (self-serve) |
| Ticketmaster | Minutes (self-serve) |
| Eventbrite | Minutes (self-serve) |
| Visual Crossing | Minutes (self-serve) |
| Google Maps | Minutes (self-serve) |
| Hotelbeds (test) | Hours (self-serve, 50 req/day) |
| Booking.com Affiliate | Days (approval) |
| Skyscanner | Weeks (approval, need traffic) |
| GetYourGuide API | Weeks+ (need 100K visits/month) |
| GDS Enterprise | Months (commercial agreement) |
| Kiwi/Tequila | Invitation only |

---

## SOURCES

- [Amadeus Self-Service Pricing](https://developers.amadeus.com/pricing)
- [Duffel Pricing](https://duffel.com/pricing)
- [Duffel NDC Guide](https://duffel.com/ndc)
- [Kiwi Tequila Partners](https://partners.kiwi.com/our-solutions/tequila/)
- [Skyscanner Partners](https://www.partners.skyscanner.net/product/travel-api)
- [Travelpayouts Platform](https://www.travelpayouts.com/en/)
- [Viator Partner Resources](https://partnerresources.viator.com/)
- [GetYourGuide Integrator Portal](https://integrator.getyourguide.com/home)
- [Hotelbeds Developer Portal](https://developer.hotelbeds.com/)
- [RateHawk API](https://www.ratehawk.com/lp/en-us/API/)
- [LiteAPI by Nuitee](https://www.liteapi.travel/)
- [Booking.com Affiliate](https://www.booking.com/affiliate-program/v2/index.html)
- [Expedia Rapid API](https://partner.expediagroup.com/en-us/solutions/build-your-travel-experience/rapid-api)
- [ATPCO Developer Portal](https://devportal.atpco.net/)
- [SerpAPI Google Flights](https://serpapi.com/google-flights-api)
- [Google Sues SerpAPI](https://ipwatchdog.com/2025/12/26/google-sues-serpapi-parasitic-scraping-circumvention-protection-measures/)
- [Amadeus Self-Service vs Enterprise](https://www.altexsoft.com/blog/amadeus-api-integration/)
- [GDS Comparison](https://www.altexsoft.com/blog/travelport-vs-amadeus-vs-sabre-gds/)
- [NDC Aggregator Ecosystem](https://www.altexsoft.com/blog/ndc-aggregators/)
- [Hotel Bed Banks Comparison](https://www.altexsoft.com/blog/bed-banks-hotelbeds-travco-bonotel-hotelspro/)
- [Car Rental APIs](https://www.altexsoft.com/blog/car-rental-apis-integrations-with-gdss-otas-and-tech-providers/)
- [Travel Agent Commission Rates 2025](https://dmcquote.com/agent-commission-rates)
- [ScrapingBee Flight APIs](https://www.scrapingbee.com/blog/top-flights-apis-for-travel-apps/)
- [Cover Genius XCover API](https://covergenius.com/xcover-api/)
- [Going/Scott's Cheap Flights](https://www.going.com/)
- [Ticketmaster API](https://developer.ticketmaster.com/products-and-docs/apis/discovery-api/v2/)
- [Eventbrite API](https://www.eventbrite.com/platform/api)
- [AviationStack Pricing](https://aviationstack.com/pricing)
- [FlightAware AeroAPI](https://www.flightaware.com/commercial/aeroapi)
- [Visual Crossing Weather](https://www.visualcrossing.com/weather-data-editions/)
- [Google Maps Pricing](https://developers.google.com/maps/billing-and-pricing/pricing)
- [OpenTable Developer](https://dev.opentable.com/)
- [Musement Affiliate](https://affiliate.musement.com/)
- [Cruise Affiliate Programs](https://www.authorityhacker.com/cruise-affiliate-programs/)
