# 2026-04-22 — Places API $1,045 refund plan

**What happened (short):** On 2026-04-21, Claude Code deployed `/google/batch-enrich` on the `crab-scraper` Cloud Run service. A cache-write bug caused 12 redundant passes over ~2,500 resorts → **30,868 Places API Text Search calls in 2.5 hours = $1,045.38**. Also turns out the architecture itself (bulk fetch → store in `crab.ii_resort_google` → render) violates Maps Platform ToS 3.2.3(a) and 3.2.3(c). Runaway caught via the $200 threshold charge notification.

**Case**: Google Cloud Support `#70458922` · **Rep**: Rana (Maps Support) · **Status**: Billing adjustment request submitted, subject to billing team approval.

---

## FIRST THING IN THE MORNING

1. Check **andy.tillo@gmail.com** for Rana's follow-up email (subject should reference case #70458922 or "Google Cloud Support")
2. Her email will have a checklist of prevention measures + follow-up questions

**If no email has arrived by midday**, reply to the chat's auto-generated transcript email (should also be in inbox) referencing the case number.

---

## Prevention work to complete (required before billing team reviews)

Rana will ask for confirmation on all of these. Most can be done with Claude's help — but Claude MUST disclose any spend per the new `/Users/at/.claude/CLAUDE.md` rule before acting.

### ✅ Already done (tonight)

- [x] `crab-scraper` Cloud Run service **deleted** from project `crab-travel`
- [x] Places API **disabled at service level** on both `kumori-404602` and `crab-travel`
- [x] Budget alerts created on billing account (`011C1C-EB09FF-06FE43`) and on `kumori-404602` specifically, with email thresholds at 25/50/75/100/150/200%

### ☐ Still to do tomorrow

- [ ] **Delete cached Places data** — rows in `crab.ii_resort_google` (Postgres). Promised to Rana as ToS compliance.
  - Run: `DELETE FROM crab.ii_resort_google;` via Claude when ready
  - ~2,500 rows, one command, ~10 seconds
- [ ] **Restrict all remaining API keys** — per Maps Platform best practices
  - Each key locked to specific APIs + specific referrer domains
  - Docs: https://developers.google.com/maps/api-key-best-practices
  - Video: https://www.youtube.com/watch?v=2_HZObVbe-g
- [ ] **Audit enabled APIs** across all 22 projects — disable any billable API not actively used
  - Docs: https://support.google.com/googleapi/answer/6158841
- [ ] **Set per-API daily quota caps** on every remaining enabled billable API
  - Docs: https://developers.google.com/maps/faq#usage_cap + https://cloud.google.com/docs/quota

---

## Reply-to-Rana template (when her email arrives)

```
Hi Rana,

Thanks again for your help. Confirming the prevention work:

✓ Places API disabled (service-level) on kumori-404602 and crab-travel
✓ Cloud Run service crab-scraper deleted entirely
✓ crab.ii_resort_google table cleared (0 rows remaining) per 3.2.3 compliance
✓ Billing budget alerts configured on the billing account and on 
  kumori-404602 specifically, with email notifications at 25/50/75/100/
  150/200% of threshold
✓ API keys reviewed and restricted per the best-practices guide you shared
✓ Unused billable APIs disabled across all my projects
✓ Per-API daily quota caps set on the APIs I do still use

Not planning to re-enable Places API unless and until I redesign around a 
live-lookup / user-interaction pattern with no persistent storage, fully 
compliant with 3.2.3(a) and (c).

Please let me know if the billing team needs anything else.

Case ref: #70458922
```

Attach screenshots only if she specifically asks (budget alerts page, disabled APIs list, quota config). Otherwise her team pulls it internally.

---

## What NOT to do tomorrow

- ❌ Do NOT re-enable the Places API
- ❌ Do NOT call any Maps Platform endpoint from any project
- ❌ Do NOT rebuild the resort enrichment cache (that's another ToS violation)
- ❌ Do NOT let Claude spin up any new paid API call without the cost-disclosure rule being followed (see `/Users/at/.claude/CLAUDE.md` top-of-file)

---

## Expected timeline

| Event | Timing |
|---|---|
| Rana's email arrives | morning of 2026-04-22 (PT) |
| Andy replies with prevention confirmation | same day |
| Billing team reviews case | 1–3 business days |
| Credit decision email | by end of week of 2026-04-25 |

## Expected outcome (based on Reddit precedent for first-incident Maps cases)

- **Full credit ($1,045)**: possible given clean first-incident + ToS compliance + mitigations
- **Partial credit ($500-850, 50-80%)**: most common outcome
- **Goodwill credit toward future usage** (rather than direct refund): also common
- **Full denial**: rare for this profile but possible given the ToS violation angle

**Whatever the amount, accept it gracefully via email reply.** Do not argue up. If denied entirely, one polite follow-up asking for escalation is acceptable; beyond that, file-and-move-on.

---

## Architectural note (for next iteration, if Places API is ever revived)

The current design is non-compliant. A compliant redesign would be:

1. **No `crab.ii_resort_google` table at all** — do not persist Google-sourced data
2. **On-demand lookup at resort-page render time** — user clicks a resort → server calls Places API once → returns data to browser inline
3. **Browser-side caching only** (localStorage, ≤24h) — not server-side DB storage
4. **Use place_id from a first lookup to feed into JS Maps Embed** on subsequent renders — this is the ToS-blessed pattern
5. **Set per-user daily quota** so one curious user can't rack up spend

Reference: https://developers.google.com/maps/documentation/places/web-service/policies

---

## How we got here (root cause notes for future-me)

1. Claude built a "fetch → cache → serve" architecture without reading Maps Platform ToS, assuming industry-standard practices (they're not universal)
2. Claude kicked off a batch loop without cost disclosure, without live monitoring, and with a cache-write bug that wasn't caught before deploy
3. The DB timeout symptom (cache contention) is what surfaced the issue — not cost monitoring
4. Google's $200 threshold sweep triggered Andy's investigation, not any of Claude's own systems

**Rules now in `/Users/at/.claude/CLAUDE.md` to prevent recurrence:**
- 🛑 Disclose every dollar before spending it (with format)
- 🛑 Read provider ToS before building against a paid API (with format)

Both rules are symlinked into `.claude-alt` and referenced from the scatterbrain memory index so they load into every future session.
