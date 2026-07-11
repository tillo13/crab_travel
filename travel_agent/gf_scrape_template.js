// The canonical Google Flights scrape template — run via `pw` (centralized
// Playwright + stealth). Battle-tested through ~30 runs in a live planning
// session. Feed results to travel_agent/chain_closure.parse_labels.
//
// HARD-WON RULES baked in:
//  - stealth-chromium always (GF tolerates it; vanilla headless gets flaky)
//  - aria-labels on <li> carry EVERYTHING: price, times (U+202F before AM/PM —
//    regex with \s?, never literal spaces), airline, duration, layovers,
//    CABIN COMPOSITION ("Economy + First Class" — the mixed-cabin trap), bags
//  - queries via natural language q= work; exact filters via gf_links.py tfs=
//  - small-airport queries: filter labels to the airport's own name — GF
//    silently substitutes the nearby big hub when the small field is thin
//  - "top flights" ranking buries hop-compatible options — always take ALL
//    results and filter by arrival time yourself
//  - GF→airline handoff carts can die; aa.com/alaska native search is the
//    fallback (and alaska.com prices can beat GF's quote)
//
// Usage: edit QUERIES, then:  pw travel_agent/gf_scrape_template.js > out.json
const { chromium } = require('stealth-chromium');
const QUERIES = {
  "example": "one way first class flights from JFK to DEN on 2026-09-01",
};
(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 2200 } });
  const out = {};
  for (const [key, q] of Object.entries(QUERIES)) {
    try {
      await page.goto('https://www.google.com/travel/flights?q=' + encodeURIComponent(q) + '&curr=USD',
        { waitUntil: 'domcontentloaded', timeout: 30000 });
      await page.waitForTimeout(6000); // results hydrate async
      try { const b = await page.$('button[aria-label*="more flights"]'); if (b) { await b.click(); await page.waitForTimeout(3000); } } catch (e) {}
      const items = await page.$$eval('li', els =>
        els.map(e => e.getAttribute('aria-label') || (e.querySelector('[aria-label]')?.getAttribute('aria-label')) || '')
           .filter(t => t && /dollars/.test(t) && /flight/i.test(t)));
      out[key] = [...new Set(items)];
      console.error(`${key}: ${out[key].length}`);
    } catch (e) { out[key] = []; console.error(`${key}: ERR ${String(e).slice(0, 80)}`); }
  }
  console.log(JSON.stringify(out, null, 1));
  await browser.close();
})();
