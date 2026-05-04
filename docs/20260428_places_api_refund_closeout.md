# 2026-04-28 — Places API refund case closeout

Final entry in the case #70458922 trail. Continues `20260424_places_api_refund_update.md`.

## Outcome: $802.47 refunded to MASTERCARD ****6441 (76.8% recovery)

Case **#70458922** was marked **Resolved** on 2026-04-28 09:49 PT.

The refund hit the **original payment method** (Mastercard ****6441), not the GCP credit balance. That's why it's not visible in the Cloud Billing console under "Promotional credits" — it's a card-side chargeback. To verify: check the Mastercard ****6441 statement around 2026-04-28.

| Field | Value |
|---|---|
| Original loss | $1,045.38 (30,868 Places API calls in 2.5h, 2026-04-21) |
| Refunded | **$802.47** |
| Unrecovered | **$242.91** (~23.2% retained by Google) |
| Refund destination | MASTERCARD ****6441 (not GCP credit) |
| Refund delivered | 2026-04-28 02:36 PT (Google Payments email) |
| Case resolved | 2026-04-28 09:49 PT |
| GCP customer ID | 011C1C-EB09FF-06FE43 |
| Payments profile | 8800-6265-2664 |

## Why partial, not full

No explicit reason from Rana / billing in the resolution email. Most likely framing: Google's stated policy on accidental overage is "review case by case," and the partial likely reflects the portion they judged recoverable under their goodwill window vs. the portion "actually consumed." Worth filing as a data point — full refunds for accidental loop-runaway are not guaranteed even with full ToS-compliance reaffirmation and clean engineering postmortem. Future incidents should assume **~75% recovery is the realistic ceiling**, not 100%.

## Followups now unblocked

The 2026-04-24 update doc parked these pending refund:

- [ ] `DELETE FROM crab.ii_resort_google;` — held pending refund. Refund landed → safe to run.
- [ ] Clean up residual `timeshare_catalog.py` JOINs against `crab.ii_resort_google`
- [ ] Drop the `crab.ii_resort_google` schema row after JOIN cleanup confirms no consumers

## Lessons codified

- **Refunds for paid-API runaways land on the card, not as GCP credit** — check card statement, not Cloud Billing console
- **Plan for ~75% recovery, not 100%**, even with a clean postmortem
- The full prevention layer (mandatory cost disclosure before any paid-API call) is now codified in `~/.claude/CLAUDE.md` global rules — applies across all repos, all sessions

## Trail (chronological, for grep-ability)

1. `20260422_places_api_refund_plan.md` — incident, opening case
2. `20260424_places_api_refund_update.md` — Rana's questions, your reply, "waiting on billing"
3. **`20260428_places_api_refund_closeout.md`** (this doc) — $802.47 refund delivered, case resolved
