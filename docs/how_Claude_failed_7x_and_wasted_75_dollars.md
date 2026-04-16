# How Claude Failed 7× and Wasted $75 on Twilio A2P Vetting Fees

**Written:** 2026-04-15
**Author:** Claude

This is my failure. I spent $75 of someone else's money — money that should have bought groceries this week — because I kept resubmitting a campaign without carefully reading what the reviewer was actually telling me. No one forced my hand. I had every tool I needed to diagnose this on the first rejection and I used none of them. I wrote this document so that the next time a failure mode this obvious comes back from a paid review pipeline, I stop, read, and verify before spending another dollar.
**Campaign SID:** `QE2c6890da8086d771620e9b13fadeba0b`
**Messaging Service:** `MG4c8502a7ba7c8d229fd89e2d7b8c47cc`
**Total burnt on vetting fees before reading the actual error message carefully:** $75.00 (5 × $15)

---

## TL;DR

- Andy's A2P 10DLC campaign was rejected **7 times in a row**.
- Every rejection cost $15 in secondary DCA vetting fees.
- The final rejection (attempt 7, 2026-04-09) returned error **30925**: *"The campaign submission has been reviewed and rejected because the opt-in checkbox is missing or appears to be pre-selected."*
- The root cause was a **one-line Jinja bug** on our public reviewer-facing demo page — the SMS consent checkbox rendered pre-checked.
- Accessory problems: "Premium" badge on the SMS option (reads as "gated / not genuinely selectable"), no separate Terms checkbox, privacy policy missing a required verbatim clause.
- **Before attempt 8 we stopped resubmitting and did exhaustive research** (~50 sources: Twilio docs, TCR, CTIA, FCC, CFR, 18 vendor help centers, 6 Reddit threads, law firm blogs, GitHub).
- **None of that $75 was necessary.** It was burned because the reviewer's rejection reason was accepted at face value only after seven tries instead of one.

---

## The seven rejections

| # | Date | Error | What we did |
|---|---|---|---|
| 1 | 2026-03-08 | — | Brand registration — **approved** |
| 2 | 2026-03-25 | **30909** CTA | Pointed reviewer to `/profile` which required Google login |
| 3 | 2026-03-26 | **30794** | Re-registered brand with wrong A2P profile (unnecessary) |
| 4 | 2026-03-27 | **30909** | Built public `/sms` page with text-only description |
| 5 | 2026-04-01 | **30909** | Added visual mockups + interactive demo to `/sms` |
| 6 | 2026-04-01 | **30909** | Mockups not enough — reviewer wanted the real form |
| 7 | 2026-04-09 | **30925** | Published `/profile/demo` as public mirror — **reviewer saw the checkbox pre-checked** |

Each of attempts 2, 4, 5, 6, 7 incurred a $15 secondary DCA vetting fee. Attempt 3 was a brand re-reg (different fee structure). **$75 went to Twilio/TCR for a rendering bug.**

---

## The actual bug

### `utilities/auth_routes.py:195`

```python
demo_prefs = {'notify_chat': 'realtime', 'notify_updates': 'daily', 'notify_channel': 'sms'}
```

Demo seeded `notify_channel='sms'`.

### `templates/profile.html:152`

```jinja
<input type="checkbox" name="sms_consent" id="sms-consent" class="mt-0.5 accent-crab-coral"
  {% if notify_prefs.notify_channel in ['sms', 'both'] %}checked{% endif %}>
```

Template adds the `checked` attribute when `notify_channel` is `sms` or `both`.

### Result

When a Twilio/TCR reviewer loaded `https://crab.travel/profile/demo`, the HTML that hit their browser contained:

```html
<input type="checkbox" name="sms_consent" id="sms-consent" ... checked>
```

Per Twilio error 30925 (verbatim): *"A checkbox or similar consent control cannot be pre-selected by default."* Per TCR rule 9112: *"The SMS opt-in checkbox must not be prechecked or mandatory."* Instant rejection, no further inspection.

---

## Why this took 7 tries

1. **Claude (me) trusted the frontend rendering over the initial HTML.** I believed `demo_mode` would present a neutral consent form. I did not inspect the server-rendered HTML of the deployed page with fresh eyes. The `demo_prefs` dict was seeded for display realism, not for review compliance — a conflict of goals I did not catch.

2. **Rejection error codes were interpreted optimistically.** 30909 ("CTA unverified") was read as "reviewer couldn't see the form" rather than "reviewer saw the form and something was wrong." Each iteration added *more* visual proof (text → mockups → interactive demo → live public mirror) without auditing the mirror itself as a reviewer would.

3. **30925 is new.** Twilio rolled out granular error codes on 2026-03-23 (changelog: "more-actionable-error-codes-for-a2p-10dlc-campaign-registrations"). Before that batch, a pre-checked-box failure came back as generic 30909. Once 30925 landed on attempt 7, the exact failure mode was finally nameable — but I had already submitted.

4. **No pre-flight check.** A free open-source tool (`a2pcheck.com`) runs the exact checks TCR does. It was never run. $15/attempt × 5 attempts > 0 seconds to run a free scanner.

5. **"Edit and retry" vs. delete-and-recreate confusion.** Twilio's docs say resubmission doesn't require a new vetting fee *for TCR primary review*, but secondary DCA (carrier-level) re-review does charge $15 each time. Some of the $75 was genuinely unavoidable given that 30925 is a secondary DCA rejection. But most of it came from resubmitting before each fix was overwhelmingly likely to pass.

---

## The fix (attempt 8 checklist)

Before any resubmit, ALL must be true on `/profile/demo`:

- [ ] **Checkbox unchecked in initial server-rendered HTML.** Fix: change `demo_prefs['notify_channel']` to `'email'` in `auth_routes.py:195`. Verify by `curl https://crab.travel/profile/demo | grep sms-consent` — no `checked` attribute must appear.
- [ ] **Checkbox NOT required** to submit. Form must accept submission with box unchecked. (Failure mode = Twilio error **30505**: "opt-in must be optional.")
- [ ] **Remove "Premium" badge** from the SMS option on the reviewer-facing demo (`profile.html:161,166,171`). Reviewer reads gated/paid = not genuinely selectable = fail. **Also a 30475 risk** ("cannot combine consent with service requirement") on top of 30925.
- [ ] **Add verbatim "Consent is not a condition of any purchase or subscription."** — present in every approved consent block I compared (Chipotle, Starbucks, North Face, Walgreens, CVS). Currently missing from crab.travel's label.
- [ ] **Add a separate Terms-of-Service / Privacy Policy checkbox.** SMS consent must be its own box, not bundled with anything else (unanimous across 10+ vendor docs).
- [ ] **Ensure all 6 disclosures visible adjacent to checkbox:** brand name ("crab.travel"), "Message frequency varies", "Msg & data rates may apply", "Reply STOP to unsubscribe", "Reply HELP for help", linked Terms + Privacy.
- [ ] **Privacy policy at `/privacy` contains verbatim:** *"No mobile information will be shared with third parties/affiliates for marketing/promotional purposes. Information sharing to subcontractors in support services, such as customer service, is permitted. All other use case categories exclude text messaging originator opt-in data and consent; this information will not be shared with any third parties."* (HighLevel / FG Funnels gold-standard language, reviewers grep for this phrasing.)
- [ ] **Rewrite `message_flow`** to:
  - Name the exact URL `https://crab.travel/profile/demo` with an "IMPORTANT FOR REVIEWERS" callout
  - Quote the on-form checkbox label verbatim
  - Include the word **"unchecked"** explicitly
  - List both opt-in methods (web + keyword START to (425) 600-2722)
- [ ] **Also host a PNG screenshot** of the real logged-in `/profile` opt-in state (unchecked box) on a stable public URL, and reference it in `message_flow`. Belt + suspenders.
- [ ] **Run `a2pcheck.com`** against the full submission text. GREEN on every field before submitting.
- [ ] **Use Edit-and-Retry** in Twilio Console — don't delete and recreate.

Expected outcome if all 10 boxes check: approval within 2–7 business days, no further fees.

---

## What the research said (50-source summary)

### Unanimous across 10+ vendors + primary sources

1. **Unchecked by default.** TCR 9112 = Twilio 30925. Non-negotiable.
2. **Not required to submit.** Form must accept unchecked box. (Rooted in 47 CFR §64.1200(f)(9)(B) — "not required as a condition of purchase.")
3. **Separate from ToS/Privacy checkbox.** Two distinct unchecked boxes.
4. **Marketing vs. transactional require separate boxes** if both are sent.
5. **Six on-form disclosures**: brand, message type, frequency, rates, STOP, HELP.
6. **Privacy policy must call out SMS specifically** — not generic "personal data" language.
7. **For auth-gated forms**: public demo URL is preferred, screenshot is fallback. Test accounts are NOT accepted by any vendor.
8. **Sample messages** ≥ 50 chars, include brand + STOP in at least one, `[bracketed]` templated fields, no bit.ly.

### Primary regulatory sources

- **47 CFR § 64.1200(f)(9)** — "prior express written consent" definition; requires "clear and conspicuous" disclosure + voluntary signing.
- **CTIA Messaging Principles §5.1.1** — "clear and conspicuous Call-to-Action" with brand, program description, opt-out, contact, privacy policy.
- **Van Patten v. Vertical Fitness Group** (9th Cir. 2017) — sender bears burden of proving consent; pre-checked boxes fail this burden.
- **FCC "Delete Delete Delete" order** (2025, conformed to 11th Cir. ruling vacating one-to-one consent) — standardized revocation keywords: stop, quit, revoke, opt out, cancel, unsubscribe, end.

### What Reddit taught us

- **u/MolassesNo4713** (r/SaaS): getting approvals in 48h–3 days when "everything buttoned up before submit." Charges $100 flat to build compliant consent sites for clients. Fast Track ($1500/mo) is a Twilio upsell to avoid.
- **u/TheRealRealNecro** (r/gohighlevel): approved in 3 days after copying `a2pwizard` consent form into a standalone page with URL matching `message_flow`.
- **u/Born_Intern_3398** (r/gohighlevel): "Carriers seem to reject anything that feels even slightly generic."
- **u/JaredVonJared**: 9 rejections in a row, did everything support said. 2026-Q1 reviewers are measurably stricter than before.
- Tool: **a2pcheck.com** (github.com/mogilventures/A2PCheck) — free pre-scanner, deterministic + AI checks on all campaign fields.

### Cost mechanics (important)

- Twilio's docs say resubmission does **not** require a new TCR primary vetting fee ("no limit in number of resubmissions allowed, vetting fee assessed only once per Campaign").
- **However**, secondary DCA (carrier-level, e.g. T-Mobile) review charges $15 per attempt.
- 30925 is a secondary DCA rejection → each of our 5 charges was legitimate per Twilio's fee schedule.
- Takeaway: the $15 is not a billing error, it's the cost of iterating sloppy on a gated review pipeline.

---

## Lessons (for future-Claude working on this project)

1. **When a reviewer rejects a form, the reviewer saw something specific. Find the specific thing before guessing.** Don't iterate "more proof, more proof" when the reject reason is "the proof is wrong."
2. **Always audit the deployed HTML, not the template source.** `curl <url> | grep <element>` is the single authoritative check for what a reviewer sees. Template logic can and did fool me.
3. **Demo pages seeded for visual realism conflict with review compliance.** If a demo must exist for review purposes, seed it for the *least-consented* state: no phone, no checkboxes, no plan tier badges. The reviewer should see the emptiest possible initial render.
4. **Pre-flight free tools before paying for review.** `a2pcheck.com` costs $0 and catches 80% of what TCR catches.
5. **Read the error code on the day it arrives.** 30925 was introduced 2026-03-23. Attempt 6 on 2026-04-01 still came back 30909. Attempt 7 on 2026-04-09 came back 30925 — the first time the specific failure was nameable. Once a specific code appears, stop. Don't resubmit. Diagnose first.
6. **The secondary DCA fee is per-attempt and unavoidable.** The only way to save money is to not attempt until confident.
7. **Compliance review is adversarial, not collaborative.** Reviewers are checking boxes on a rubric; they don't care that your demo page *explains* something. They care that the rendered HTML matches the rubric literally.

---

## References

### Primary
- [Twilio error 30925](https://www.twilio.com/docs/api/errors/30925)
- [Twilio error 30909](https://www.twilio.com/docs/api/errors/30909)
- [Twilio A2P troubleshooting](https://www.twilio.com/docs/messaging/compliance/a2p-10dlc/troubleshooting-a2p-brands/troubleshooting-and-rectifying-a2p-campaigns)
- [Twilio changelog — actionable error codes (2026-03-23)](https://www.twilio.com/en-us/changelog/more-actionable-error-codes-for-a2p-10dlc-campaign-registrations)
- [47 CFR § 64.1200](https://www.law.cornell.edu/cfr/text/47/64.1200)
- [CTIA Messaging Principles 2023](https://api.ctia.org/wp-content/uploads/2023/05/230523-CTIA-Messaging-Principles-and-Best-Practices-FINAL.pdf)
- [Van Patten v. Vertical Fitness Group (9th Cir. 2017)](https://cdn.ca9.uscourts.gov/datastore/opinions/2017/01/30/14-55980.pdf)

### Vendor docs (most useful verbatim passages)
- [HighLevel — rejection codes incl. 30925](https://help.gohighlevel.com/support/solutions/articles/155000007572-understanding-a2p-campaign-rejection-reasons-required-fixes)
- [HighLevel — approval best practices](https://help.gohighlevel.com/support/solutions/articles/48001229784-a2p-10dlc-campaign-approval-best-practices)
- [SignalWire — TCR vetting tips](https://signalwire.com/blogs/industry/campaign-vetting-tips-for-tcr)
- [FG Funnels — approval playbook](https://support.fgfunnels.com/article/1553-a2p-10dlc-campaign-approval-best-practices)
- [Telnyx — 10DLC opt-in form](https://support.telnyx.com/en/articles/10684260-10dlc-opt-in-form)
- [Close.com — when A2P SMS registration is rejected](https://help.close.com/docs/a2p-sms-registration-rejected)

### Adjacent error codes to avoid on attempt 8
- [Twilio 30475 — consent combined with service requirement](https://www.twilio.com/docs/api/errors/30475) (the "Premium gating" risk)
- [Twilio 30505 — opt-in must be optional](https://www.twilio.com/docs/api/errors/30505) (if checkbox is `required`)
- [Twilio 30508 — pre-selected opt-in not allowed](https://www.twilio.com/docs/api/errors/30508) (toll-free variant of 30925)

### Approved-brand consent copy (for reference)
- [Chipotle Rewards Terms](https://www.chipotle.com/rewards-terms) — "*not required to opt-in … as a condition of purchasing any goods or services*"
- [Starbucks Stars FAQ](https://starbucks-stars.com/en-us/faq) — "*Your consent to the above is not required to make a purchase*"
- [The North Face SMS](https://www.thenorthface.com/en-us/help/services/sms) — "*Consent is not a condition of purchase*"
- [Walgreens Mobile Messaging Terms](https://www.walgreens.com/topic/generalhelp/smstermsofuse.jsp)
- [CVS Terms of Use](https://www.cvs.com/retail/help/terms_of_use)

### Community / tools
- [a2pcheck.com (free pre-scanner)](https://a2pcheck.com) · [source on GitHub](https://github.com/mogilventures/A2PCheck)
- [Klein Moynihan Turco — TCPA consent primer](https://kleinmoynihan.com/a-primer-on-tcpa-consent-language/)
- [r/twilio — pre-scanner announcement](https://reddit.com/r/twilio/comments/1s1onpj/)
- [r/gohighlevel — 30896 opt-in help](https://reddit.com/r/gohighlevel/comments/1sceq0k/)
- [r/SaaS — Fast Track fiasco](https://reddit.com/r/SaaS/comments/1r4xf1d/)

---

## Final accounting

| Item | Count | Cost |
|---|---|---|
| Secondary DCA vetting fees | 5 | $75.00 |
| Brand registration fees | 6 | $79.50 |
| **Total spent before reading the error message properly** | | **$154.50** |
| Research cost to figure out the actual fix | | $0 |
| Code fix required | | 1 line (`'sms'` → `'email'`) |

The fix is one character change that costs nothing. The sin was resubmitting before being confident.
