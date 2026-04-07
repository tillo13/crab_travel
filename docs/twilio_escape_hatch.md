# Twilio Escape Hatch — Alternative SMS Providers

**Created:** 2026-04-06
**Context:** Attempt 6 of A2P 10DLC campaign registration submitted Apr 1. As of Apr 6, status = `IN_PROGRESS`, zero errors, Day 5 in TCR queue. This is the first attempt that hasn't been auto-rejected — a good sign, but if it drags or fails, here's where we go next.

---

## Decision Rule

- **Day 5–14 in IN_PROGRESS, no errors** → wait. This is the normal Twilio TCR window per reddit reports (1–3 weeks typical).
- **Day 14+ (after 2026-04-15) still IN_PROGRESS** → start seriously evaluating Telgorithm migration.
- **FAILED with errors** → read errors, decide between fix-and-resubmit (attempt 7) vs. cut losses and migrate.

---

## Provider Shortlist (ranked for our use case: low volume, single sender, need speed)

### 1. Telgorithm — TOP PICK if we migrate
- **Approval time:** ≤72 hours (vs Twilio's 1–3 weeks)
- **Per-message:** ~$0.005 (vs Twilio $0.0079)
- **Specialty:** A2P 10DLC only, nothing else. Every customer is registered as a CSP with TCR directly (we own the Brand + Campaign, not the provider).
- **Support:** Dedicated account manager + shared Slack channel.
- **Key upside:** Reddit users who switched from Twilio consistently report going from weeks-of-waiting to <72h approvals.
- **Link:** https://www.telgorithm.com/telgorithm-vs-twilio

### 2. Telnyx — if we want Twilio-shaped API at half the price
- **Per-message:** ~$0.004 + carrier fees
- **Support:** Free 24/7 in-house engineering support via chat/phone
- **Use case:** Drop-in replacement for Twilio SDK with less lock-in. Good option if speed is not the primary driver and we just want to keep paying less going forward.

### 3. Plivo — cheapest at scale, simple API
- **Per-message:** ~$0.0066
- **No monthly minimums**, pay-as-you-go
- **Tradeoff:** Less hand-holding on 10DLC registration than Telgorithm.

### Skip list
- **Sinch** — enterprise-only, millions of msgs/month required to make sense
- **Bandwidth** — same, enterprise focus
- **MessageBird / Bird** — pushes you into an omnichannel platform, more than we need

---

## What We'd Keep vs. Rebuild If We Migrate

### Keeps (owned at TCR level, not Twilio)
- **TCR Brand `B9D07O1`** (approved 2026-03-08) — this lives at The Campaign Registry, not inside Twilio. A new CSP should be able to reference the same brand, saving the $4 brand fee and (more importantly) any re-vetting time. **TODO before migrating: confirm with Telgorithm support that an existing TCR brand can be attached to a campaign submitted via their CSP.**

### Rebuilds
- **Messaging service** — Twilio-specific (`MG4c8502a7ba7c8d229fd89e2d7b8c47cc`)
- **Phone number** — `+14256002722` is a Twilio-leased number. Either port it out (Twilio allows 10DLC number porting) or buy a new one from the new provider (~$1–2/mo).
- **Campaign** — has to be resubmitted through the new CSP's flow (but with Telgorithm, approval is ≤72h).
- **Code path** — `utilities/sms_utils.py` currently uses `twilio.rest.Client`. A migration would swap this for the new provider's SDK (Telnyx and Plivo have near-identical APIs; Telgorithm has its own but documented).

---

## Sunk Cost (do not factor into decision)

- ~$68 in A2P fees already spent on Twilio (brand $4, campaign vetting ~$15 × 4 attempts, failed brand re-reg $4)
- Time spent writing the `/sms` visual walkthrough page — this is an **asset that transfers**. Any future CSP will want the exact same reviewer-accessible page, so the work isn't wasted.

---

## Migration Checklist (execute only if triggered by decision rule above)

1. Email Telgorithm sales: ask if TCR brand `B9D07O1` can be reused under their CSP registration
2. Sign up for Telgorithm account
3. Port `+14256002722` out of Twilio (or provision new number)
4. Swap `utilities/sms_utils.py` to use Telgorithm SDK
5. Submit new campaign via Telgorithm (re-use existing `/sms` page, samples, message flow from `docs/twilio_a2p_campaign.md`)
6. Wait ≤72h for approval
7. Cancel Twilio messaging service + release old number to stop recurring fees
8. Update `docs/twilio_a2p_campaign.md` → archive, create `docs/telgorithm_campaign.md`

---

## Sources
- https://www.telgorithm.com/telgorithm-vs-twilio
- https://knock.app/blog/the-top-sms-providers-for-developers
- https://www.ringly.io/blog/plivo-alternatives
- https://emitrr.com/blog/twilio-alternative/
- r/twilio thread "How long is A2P 10DLC campaign registration taking right now?" — multiple users confirming Twilio = weeks, Telgorithm = ≤72h

---

**Automated monitoring:** `scatterbrain/docs/daily_health_check.md` → `probe_twilio_a2p()` watches the Twilio campaign state daily. The Day-14 escape-hatch decision (2026-04-15) is manual — consult this doc when that email arrives.
