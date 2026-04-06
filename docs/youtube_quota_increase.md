# YouTube Data API Quota Increase — Full Documentation

**Last updated:** 2026-04-06
**Status:** IN REVIEW (submitted 2026-03-31, acknowledged 2026-04-01, silent since)
**Ask:** 10,000 → **100,000 units/day** on `youtube.googleapis.com`
**Project:** **kumori-404602** (owns `CRAB_YOUTUBE_API_KEY`)

---

## Why We Submitted This

crab.travel's automated bots seed itineraries for demo + booked trips. Each itinerary item triggers a YouTube preview search on the invite/plan pages (`/api/youtube-search`, called from `templates/invite.html` and `templates/plan.html`). With the default **10,000 units/day** quota and `search.list` costing **100 units per call**, we were capped at roughly **5 seeded trips per day** before the quota ran out.

The commits leading up to the Mar 31 submission literally describe the workarounds:

```
387d787 Cap itinerary seeding to 2 per run to prevent App Engine timeout
4f220a4 Limit seed task to 10 stale plans per run to avoid timeout
```

These "timeout" caps are partially quota-exhaustion workarounds. Lifting the quota to 100k/day gives us ~**50 seeded trips per day** of headroom and lets us remove the artificial caps.

### Evidence the ceiling was real

Last 30 days of YouTube API usage on **kumori-404602** (via Cloud Monitoring):

| Metric | Value |
|---|---|
| Total requests (30d) | 666 |
| 200 OK | 655 |
| 400 Bad Request | 2 |
| **403 Quota Exceeded** | **9** |

Nine quota-exceeded hits in 30 days = we were regularly banging on the ceiling. This is exactly the number we referenced in the Google form justification.

---

## All YouTube API Consumers Across All Projects (audited 2026-04-06)

Only **crab_travel** drives real quota cost against an API key. Other projects either use OAuth (different quota bucket) or have negligible usage.

| Project | File | Purpose | Auth | Impact |
|---|---|---|---|---|
| **crab_travel** | `app.py` → `/api/youtube-search` | Video previews on invite/plan pages, called during itinerary seeding | `CRAB_YOUTUBE_API_KEY` (kumori-404602) | **THIS IS THE REASON** |
| digital_empire_tv | `main.py` + `youtube_utils.py` | Red Ninja gaming network dashboard (7 channels, hourly cache) | `digital-empire-461123` key | ~0 req/month (95% cache hit) |
| digital_empire | `tools/youtube_market_analyzer/ff_transcriber.py` | Offline market analysis + transcription | local | negligible |
| kumori_ai | `kumori_ai_youtube_creator/utilities/youtube/youtube_scheduler_utils.py` | YouTube video upload scheduler | OAuth (`.upload`, `.force-ssl`) | different quota bucket |
| crab_travel | `dev/smoke_test.py` | Smoke test | same key | 1 call per run |

**Monitoring confirmation (30 days):**
- kumori-404602: 666 requests (the 9 × 403 all here)
- digital-empire-461123: 0 requests
- galactica-character-game: 0 requests

---

## Submission Timeline

| Date | Event |
|---|---|
| **2026-03-31 11:10 AM PT** | Submitted YouTube API Services Form. Auto-reply received: *"We will follow up with a response once the application has been reviewed."* |
| **2026-04-01 10:36 AM PT** | Follow-up from `youtube-disputes@`: *"We are already in the process of reviewing your request and as of this writing, we have seen: Project Key / Quota Request / Attachment."* |
| **2026-04-06** | Day 6 — still silent. `defaultLimit=10000, override=none` on all 3 projects with the API enabled. |

Emails are in Gmail — search `from:youtube-disputes`.

---

## Current State (as of 2026-04-06)

Verified via `gcloud alpha services quota list --service=youtube.googleapis.com` on every project:

| Project | Daily Quota | Override? |
|---|---|---|
| kumori-404602 | 10,000 | ❌ none |
| galactica-character-game | 10,000 | ❌ none |
| digital-empire-461123 | 10,000 | ❌ none |

**Nothing has been approved yet.** When approved, one project (expected: kumori-404602) will show `consumerOverride.overrideValue=100000`.

---

## How to Check Status

```bash
# Poll the current effective quota limit on kumori-404602
gcloud --project=kumori-404602 alpha services quota list \
  --service=youtube.googleapis.com \
  --consumer=projects/kumori-404602 \
  --format=json | python3 -c "
import sys, json
for item in json.load(sys.stdin):
    for q in item.get('consumerQuotaLimits', []):
        for b in q.get('quotaBuckets', []):
            eff = b.get('effectiveLimit')
            override = b.get('consumerOverride', {}).get('overrideValue', 'none')
            print(f\"{item.get('metric','?').split('/')[-1]}: effective={eff} override={override}\")
"
```

**Expected transition on approval:**
```
default: effective=10000  override=none        ← current
default: effective=100000 override=100000      ← approved
```

### Real usage check (confirms 403s aren't still happening post-bump)

```python
from google.cloud import monitoring_v3
from datetime import datetime, timedelta, timezone
c = monitoring_v3.MetricServiceClient()
end = datetime.now(timezone.utc); start = end - timedelta(days=7)
interval = monitoring_v3.TimeInterval({"end_time":{"seconds":int(end.timestamp())},"start_time":{"seconds":int(start.timestamp())}})
r = c.list_time_series(request={
    "name": "projects/kumori-404602",
    "filter": 'metric.type="serviceruntime.googleapis.com/api/request_count" AND resource.labels.service="youtube.googleapis.com"',
    "interval": interval,
    "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
})
by = {}
for ts in r:
    code = ts.metric.labels.get('response_code','?')
    for p in ts.points:
        by[code] = by.get(code,0) + (p.value.int64_value or 0)
print('Last 7d by response code:', by)
```

---

## Decision Rule

- **Day 1–14 (through Apr 14)** — silent is normal, no action.
- **Day 14 (2026-04-14)** — if still no response, post a polite reply to the `youtube-disputes@` thread asking for status.
- **Day 21 (2026-04-21)** — if still silent, consider a second email citing the specific 403 quota errors as justification.
- **If REJECTED** — read the reason, resubmit with fixes. Alternative: apply on a *different* project (e.g., create a dedicated `crab-travel` project key rather than using the shared kumori key).

---

## Next Steps (Once Approved)

1. **Remove seed-throttle caps** in `app.py`:
   - `Cap itinerary seeding to 2 per run` → bump to 20
   - `Limit seed task to 10 stale plans per run` → bump to 50
2. **Re-enable bot-driven YouTube previews** on all itinerary items (currently partially throttled)
3. **Update this doc** to "APPROVED" with the new effective limit
4. **Consider caching** — even at 100k/day, add a short TTL cache on `/api/youtube-search` keyed by query string. Today we search live every time. A 24-hour cache on popular queries ("things to do in Reykjavik") would cut quota by ~80% and extend headroom further.

---

## Probe (to be built in scatterbrain)

Daily cron in scatterbrain should:
1. Hit `gcloud alpha services quota list` for `youtube.googleapis.com` on kumori-404602
2. Compare `effectiveLimit` to last known value stored in `scatterbrain.health_checks`
3. Email only on **state change** (10k → 100k = 🎉 approved; → anything else = investigate)
4. Also report 7d error-code breakdown — alert if 403 count > 0

See `scatterbrain` consolidated daily health check plan (TBD).

---

## Related Files

- `app.py:2670` — `/api/youtube-search` endpoint definition
- `templates/invite.html:1183` — caller (invite card video previews)
- `templates/plan.html:748` — caller (plan page video previews)
- `dev/smoke_test.py:275` — smoke test caller
- `docs/twilio_a2p_campaign.md` — sister "pending external approval" saga
- `docs/twilio_escape_hatch.md` — escape hatch pattern (same idea could apply here: create a new project if this one gets rejected)
