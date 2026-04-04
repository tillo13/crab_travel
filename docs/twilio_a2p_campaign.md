# Twilio A2P 10DLC Campaign — Full Documentation

**Last updated:** 2026-04-01
**Status:** IN_PROGRESS (attempt 6, submitted 2026-04-01)

---

## What This Is

A2P 10DLC is the carrier-mandated registration system for sending Application-to-Person SMS from 10-digit long code numbers. Without it, carriers (AT&T, T-Mobile, etc.) filter your messages with error 30034. This doc tracks every piece of the registration so you never have to reverse-engineer it again.

---

## Current Registration State

| Component | SID | Status |
|---|---|---|
| **Twilio Account** | `[REDACTED — see GCP Secret Manager: CRAB_TWILIO_ACCOUNT_SID]` | Active |
| **Customer Profile** | `BUf5cd2668261710eff4bb1c97eea9bf10` | twilio-approved |
| **A2P Trust Product** | `BU7406bb09eaf450c62a6fc4f40019fb1b` | twilio-approved (policy `RNb0d4771c2c98518d916a3d4cd70a8f8b`) |
| **Brand Registration** | `BN05299cc8c46ebf46b61fb87fb11d6ff9` | **APPROVED** (TCR ID: `B9D07O1`, since 2026-03-08) |
| **Campaign** | `QE2c6890da8086d771620e9b13fadeba0b` | **IN_PROGRESS** (attempt 6, submitted 2026-04-01) |
| **Messaging Service** | `MG4c8502a7ba7c8d229fd89e2d7b8c47cc` | "Low Volume Mixed A2P Messaging Service" |
| **Phone Number** | `+14256002722` (`PN62f5dfd99912cceb5213c3b1e1f9bbe5`) | In messaging service sender pool |

### Other objects (ignore, dead/failed):
- Brand `BN9925256294a428e50c9d8624fc58b5f1` — FAILED (Mar 26, used wrong A2P profile)
- Trust Product `BU59b00066af590a43ff6735316e6969c6` — twilio-rejected
- Trust Product `BU436f0f446e361bf09d3c0f9f66773657` — approved but wrong regulation SID
- Messaging Service `MGe2d8c168c1dae056a298ca1856814f57` — "crab.travel" service, empty, unused
- Previous campaign `QE2c6890da...` — FAILED (Mar 25, error 30909 CTA verification)

---

## What Was Submitted in the Campaign (2026-04-01, attempt 6)

### Use Case
`LOW_VOLUME` — "Low Volume Mixed" — any combination of use cases, low throughput (<2000 msgs/day on T-Mobile), no carrier post-approval required. Lowest monthly fee.

### Description
> crab.travel is a group trip planning platform. We send transactional SMS notifications to trip members who have explicitly opted in. Messages include trip chat messages from other group members, voting reminders, trip status updates, booking confirmations, and price drop alerts. All messages relate to trips the user has joined. Message frequency varies based on trip activity, not exceeding 10 messages per day. SMS is never enabled by default.

### Message Flow (this is the CTA — what the reviewer checks)
> End users opt in to SMS through two methods:
>
> 1. WEB OPT-IN: Users log in to crab.travel via Google OAuth, navigate to their Profile page (https://crab.travel/profile), enter their phone number, check a consent checkbox that reads: "I agree to receive SMS/text messages from crab.travel, including trip chat messages, voting reminders, and status updates. Message frequency varies. Msg & data rates may apply. Reply STOP to unsubscribe at any time, HELP for help. Terms & Privacy." — and select SMS or Both as their notification channel. All three steps are required. Selecting SMS without checking the consent box is blocked by validation.
>
> 2. KEYWORD OPT-IN: Users text START to (425) 600-2722.
>
> Because the web opt-in form requires authentication, a complete visual walkthrough with step-by-step UI mockups of the actual consent form is publicly available at https://crab.travel/sms — showing the initial state (SMS off by default), the consent checkbox appearing after phone entry, the completed opt-in, and the validation that blocks SMS selection without consent. An interactive demo of the same form with live validation is also on that page.
>
> SMS terms of service: https://crab.travel/terms#sms
> SMS privacy policy: https://crab.travel/privacy#sms
> Public SMS program info: https://crab.travel/sms

### Sample Messages
1. `[crab.travel] Sarah: Hey everyone, should we push dinner to 7pm instead? Reply STOP to opt out.`
2. `crab.travel: Price drop alert! The Hilton Scottsdale is now $189/night, down from $245. View your trip at crab.travel. Reply STOP to unsubscribe.`

### Opt-In
- **Keywords:** START, SUBSCRIBE, YES
- **Auto-reply:** "Welcome to crab.travel SMS alerts! You will receive trip updates, price alerts, and group chat notifications. Message frequency varies. Msg & data rates may apply. Reply STOP to unsubscribe, HELP for help."

### Opt-Out
- **Keywords:** STOP, UNSUBSCRIBE, CANCEL, QUIT, END (Twilio also auto-adds OPTOUT, REVOKE, STOPALL)
- **Auto-reply:** "You have successfully been unsubscribed. You will not receive any more messages from this number. Reply START to resubscribe."

### Help
- **Keywords:** HELP, INFO
- **Auto-reply:** "crab.travel SMS help: We send trip updates, group chat messages, and price alerts. Msg frequency varies. Msg & data rates may apply. Reply STOP to cancel. Visit https://crab.travel/sms or email support@crab.travel for more info."

### Flags
- Has embedded links: **true**
- Has embedded phone: **false**
- Age gated: **false**
- Direct lending: **false**

---

## Why Previous Attempts Failed

### Attempt 1: Brand Registration (Mar 8) — SUCCESS
- Used correct A2P profile `BU7406bb09eaf450c62a6fc4f40019fb1b` with regulation `RNb0d4771c2c98518d916a3d4cd70a8f8b`
- Approved same day

### Attempt 2: Campaign (Mar 25) — FAILED
- Error 30909: "rejected due to issues verifying the Call to Action (CTA)"
- **Root cause:** The `messageFlow` pointed reviewers to `crab.travel/profile` which requires Google login. The Twilio reviewer couldn't see the SMS consent checkbox because they couldn't log in. There was no public page showing the opt-in flow.
- **Also:** The `help_message` was generic boilerplate ("Reply STOP to unsubscribe. Msg&Data Rates May Apply") instead of actually describing how to get help.

### Attempt 3: Brand Re-registration (Mar 26) — FAILED
- Error 30794: Used wrong A2P profile (`BU436f...` with policy `RN7a975...`)
- This was unnecessary — the brand was already approved from March 8. Someone tried to re-register it with a different profile.

### Attempt 4: Campaign Resubmission (Mar 27) — FAILED
- Error 30909 again: CTA verification failed
- Created public page at `https://crab.travel/sms` with text-only description of opt-in flow
- `messageFlow` directed reviewers to `/sms`, `/terms#sms`, `/privacy#sms`
- **Root cause:** The `/sms` page only had text descriptions, not visual proof. Reviewer still couldn't verify the actual UI. Per Twilio docs: "If the CTA is behind a login, provide a screenshot of the CTA hosted on a publicly accessible URL."

### Attempt 5: Campaign Resubmission (Apr 1) — PREPARING
- **Major `/sms` page overhaul:** Now includes:
  - Step-by-step visual mockups of the Profile page showing exact UI at each stage (initial state, phone entered, consent checked, validation blocking)
  - Interactive demo replicating the actual form with real validation logic
  - Clear documentation of both opt-in methods (web + keyword)
  - All required disclosures (brand, frequency, rates, terms, privacy, opt-out)
- **Updated Message Flow** to explicitly call out the visual walkthrough on `/sms`
- **Key insight from research:** Twilio reviewers need to visually verify the consent UI. Text descriptions aren't enough when the actual form is behind auth.

---

## How to Check Status

### Quick API check (run from crab_travel root):

```bash
python3 -c "
import sys; sys.path.insert(0, '.')
from utilities.google_auth_utils import get_secret
import requests, json

account_sid = get_secret('CRAB_TWILIO_ACCOUNT_SID')
auth_token = get_secret('CRAB_TWILIO_AUTH_TOKEN')

resp = requests.get(
    'https://messaging.twilio.com/v1/Services/MG4c8502a7ba7c8d229fd89e2d7b8c47cc/Compliance/Usa2p/QE2c6890da8086d771620e9b13fadeba0b',
    auth=(account_sid, auth_token), timeout=15
)
d = resp.json()
print(f'Campaign Status: {d[\"campaign_status\"]}')
print(f'Campaign ID: {d.get(\"campaign_id\", \"not yet assigned\")}')
print(f'Errors: {d.get(\"errors\", [])}')
print(f'Rate limits: {d.get(\"rate_limits\", {})}')
if d['campaign_status'] == 'VERIFIED':
    print('*** CAMPAIGN APPROVED — SMS SHOULD WORK NOW ***')
elif d['campaign_status'] == 'FAILED':
    print('*** CAMPAIGN REJECTED — check errors above ***')
else:
    print(f'Still waiting... (status: {d[\"campaign_status\"]})')
"
```

### SMS delivery test (only meaningful after campaign is VERIFIED):

```bash
python3 -c "
import sys, time; sys.path.insert(0, '.')
from utilities.google_auth_utils import get_secret
import requests

account_sid = get_secret('CRAB_TWILIO_ACCOUNT_SID')
auth_token = get_secret('CRAB_TWILIO_AUTH_TOKEN')

resp = requests.post(
    f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json',
    auth=(account_sid, auth_token),
    data={
        'MessagingServiceSid': 'MG4c8502a7ba7c8d229fd89e2d7b8c47cc',
        'To': '+14252461275',
        'Body': 'crab.travel SMS test. If you got this, A2P is APPROVED!'
    }, timeout=15
)
msg = resp.json()
sid = msg.get('sid', '')
print(f'Sent: {sid} (status: {msg.get(\"status\")})')
time.sleep(8)
resp2 = requests.get(
    f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages/{sid}.json',
    auth=(account_sid, auth_token), timeout=15
)
d = resp2.json()
print(f'Delivery status: {d.get(\"status\")}')
print(f'Error code: {d.get(\"error_code\")}')
if d.get('status') == 'delivered':
    print('*** A2P IS LIVE! SMS WORKING! ***')
elif d.get('error_code') == 30034:
    print('Still blocked — campaign not approved yet or not propagated to carriers')
"
```

### Expected status progression:
1. `IN_PROGRESS` — just submitted, in TCR queue
2. `VERIFIED` — approved, carriers notified, SMS should start working
3. `FAILED` — rejected, check `errors` array for reason

### Timeline
- TCR typically reviews campaigns within **several days** (not weeks)
- After VERIFIED, carriers may take 24-48 hours to propagate

---

## Costs Incurred

| Item | Cost | Date | Notes |
|---|---|---|---|
| Brand registration (one-time) | $4.00 | 2026-03-08 | TCR fee |
| Campaign vetting | $15.00 | 2026-03-25 | First campaign (failed) |
| Campaign vetting | ~$15.00 | 2026-03-27 | Second campaign (failed, 30909 again) |
| Brand re-registration (failed) | $4.00 | 2026-03-26 | Unnecessary, wasted |
| Phone number (monthly) | $2.30 | Monthly | +14256002722 local |
| Toll-free number (monthly) | $4.30 | Monthly | If still active |
| Per SMS | $0.0079 | Per message | Even undelivered ones |

| Campaign vetting | ~$15.00 | 2026-04-01 | Third campaign (attempt 6, visual walkthrough) |

Total spent on A2P registration: ~$53 in fees + ~$15 pending for this campaign.

---

## Key Public Pages (reviewer-accessible)

| URL | Content |
|---|---|
| https://crab.travel/sms | Full SMS program info, opt-in flow, samples, opt-out |
| https://crab.travel/terms#sms | SMS terms of service section |
| https://crab.travel/privacy#sms | SMS privacy policy section |
| https://crab.travel/profile | Actual opt-in form (requires login) |

---

## What Happens When It's Approved

Zero code changes needed. The SMS pipeline is fully built:
- `utilities/sms_utils.py` sends via MessagingServiceSid `MG4c8502a7ba7c8d229fd89e2d7b8c47cc`
- Users with `notify_channel` = 'sms' or 'both' and a phone number get texts
- Chat messages, voting reminders, trip updates, and price alerts all have SMS paths
- Inbound SMS handler at `/api/sms/inbound` posts messages to trip chat

---

## If It Fails Again

1. Check the `errors` array in the campaign status response
2. Common rejection reasons:
   - **30909 (CTA verification):** Reviewer can't verify opt-in. Make sure https://crab.travel/sms is live and matches what's in `messageFlow`
   - **30910 (sample messages):** Samples don't match described use case
   - **30911 (opt-in mismatch):** Described opt-in method doesn't match actual site
3. Delete the failed campaign: `DELETE /v1/Services/{MsgSvcSid}/Compliance/Usa2p/{CampaignSid}`
4. Fix the issue
5. Resubmit (will incur another ~$15 vetting fee)

---

## Twilio Console Links

- Account overview: https://console.twilio.com
- Messaging services: https://console.twilio.com/us1/develop/sms/services
- A2P registration: https://console.twilio.com/us1/develop/sms/regulatory-compliance/a2p-onboarding
- Message logs: https://console.twilio.com/us1/monitor/logs/sms
