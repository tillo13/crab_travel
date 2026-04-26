# 2026-04-24 — Places API refund case update (actions + reply)

Continuation of `20260422_places_api_refund_plan.md`. Case `#70458922` (Rana, Maps Support).

## Where things stood this morning

- Rana's Apr 22 11:22 follow-up said the two mitigations from her chat had not been applied: "api keys are still unrestricted. Restrict the apikeys." + "Enable the api(s) you like to use and set a meaningful request per day / Map loads per day / elements per day limit."
- An Apr 24 12:22 auto-reply warned the case would auto-close in 2 days without a response.

## Audit of kumori-404602 API keys (before)

4 keys, all unrestricted:

| UID prefix | Display name | Key suffix | Used by |
|---|---|---|---|
| `ebf62e11` | SEO PageSpeed | ...Rgb60 | Referenced only in `_infrastructure/seo_master/` docs. Not wired live. |
| `60289c54` | API key 2 | ...dmBU | Stored as `CRAB_YOUTUBE_API_KEY` secret. Used in `crab_travel/watches_routes.py` (YouTube), `timeshare_drive.py` (Drive), `timeshare_google.py` (Places — the runaway path). Also in `digital_empire_tv/.env` locally (YouTube analyzer scripts — not deployed). |
| `99131857` | API key 1 | ...VFA4k | Zero code references anywhere. Orphan from 2024-01-26. |
| `da588d1c` | API key 1 | ...z3ais | Zero code references anywhere. Orphan from 2024-01-26. |

Root cause of the incident: key `...dmBU` had no API restriction, so when `/google/batch-enrich` reused the YouTube key to call Places, there was nothing at the key level blocking it.

## Actions taken (2026-04-24 ~06:45 PDT)

### 1. API key restrictions (answers Rana's ask #1)
- Deleted key `...VFA4k` (orphan)
- Deleted key `...z3ais` (orphan)
- Restricted key `...Rgb60` → PageSpeed Insights API only
- Restricted key `...dmBU` → YouTube Data API v3 + Drive API only (Places explicitly excluded)

### 2. Daily quota caps (answers Rana's ask #2)
Consumer overrides on kumori-404602:
- `pagespeedonline.googleapis.com/default` @ `1/d/{project}` → **100/day** (was 25,000 default)
- `youtube.googleapis.com/default` @ `1/d/{project}` → **10,000/day**
- Drive has no per-day metric exposed; per-minute defaults remain.
- No Maps Platform APIs enabled, so Map-loads/elements caps are N/A.

### 3. Dead code cleanup
To prevent future accidental re-runs if Places API were ever re-enabled:
- Deleted `utilities/timeshare_google.py` entirely (fetch_place, get_or_fetch_google, resorts_with_coords)
- Removed import + `get_or_fetch_google` call in `timeshare_routes.py` `catalog_resort()` — now passes `google=None` (template has nil guards so no UI change)
- Deleted `/google/batch-enrich` endpoint in `worker/app.py` — this was the actual runaway endpoint
- Syntax verified on both edited files, grep confirms zero dangling references

### 4. Email reply
Draft created via Gmail API, threaded to case #70458922. Covers only Rana's two verbatim asks. Cites each key by display name + suffix so she can cross-check in the console. Sent by Andy at ~06:55 PDT.

## Intentionally deferred

- **`DELETE FROM crab.ii_resort_google;`** — held until refund decision lands. Google has no visibility into Cloud SQL row contents (tenant-isolated), so holding is not detectable. Not mentioned in the reply to Rana, keeping us neutral on the data question.
- **`timeshare_catalog.py` LEFT JOINs** against `crab.ii_resort_google` — left in place. They return NULL rows once the table is cleared, which is what we want. Will clean up when the table itself is dropped.
- **`utilities/timeshare_schema.py` `CREATE TABLE crab.ii_resort_google (...)`** — left in place so migrations don't break. Remove when dropping the table.
- **Deploy** — not yet run. Cleanup edits only take effect after `deploy`. The `crab-scraper` Cloud Run service that was running the runaway endpoint is already deleted, so there's no live container running the old code.

## What Rana's reply sees (verifiable from console)

Anyone clicking through Cloud Console → APIs & Services → Credentials on kumori-404602 will now see:
- 2 keys, both with API target restrictions (the `restrictions.apiTargets` JSON field is populated)
- APIs & Services → Quotas shows consumer overrides of 100 and 10,000 on PageSpeed and YouTube respectively
- Places API (`places.googleapis.com`) still disabled
- No Maps Platform APIs enabled

## Exchange follow-up (same day)

### 07:09 PDT — Rana's clarifying reply (12 min after Andy sent)

> I understand you have applied cap on PageSpeed Insights API, YouTube Data API, Drive API. These are not Google map platform(GMP) apis. You were billed for GMP api [Places api (NEW)].
>
> I noticed you have disable all the GMP apis in your project (kumori-404602). No Map apis will work now. Is this intended?
>
> If not enable the api(s) you wish to use and apply a daily limit.

Read: she acknowledged the caps, flagged that they're non-GMP, and asked one yes/no question — is the full GMP disable intentional? Not a rejection, a due-diligence checkbox before forwarding to billing.

### 07:15 PDT — Andy's reply (sent)

> Yes, intentional. I have no business need for any Maps Platform API right now. Places was the only one I had used, and per the original chat I won't re-enable it without a ToS-compliant redesign. The daily caps I set are on the non-GMP APIs I actively use.

Clean yes, reaffirmed the original ToS-compliance stance, green-lit her to forward to billing.

## Expected next touchpoint

1. Rana forwards to billing team (1–3 business days after 2026-04-24)
2. Billing team decides on credit amount (end of week of 2026-04-25)
3. If credit lands: run the DELETE, clean up residual `timeshare_catalog.py` JOINs + schema row, close out case
4. If credit denied or partial: one polite escalation email, then accept the outcome and close out regardless
