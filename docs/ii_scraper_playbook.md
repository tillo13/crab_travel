# Interval International scraper — technical playbook

**Audience:** future-Claude or anyone picking up this work tomorrow.
**Status:** Working, end-to-end proven 2026-04-28.
**Purpose:** detail every cookie, header, endpoint, body shape, gotcha, and
dead-end we found tonight so this doesn't get re-guessed.

This is the *technical reference*. The *product reasoning* is in
`docs/2026apr28_updates.md`. Read that for "why"; read this for "how."

---

## 0. TL;DR — the whole thing in 30 seconds

1. Headless login is dead. Akamai blocks Patchright/stealth-chromium
   even with valid creds. **Don't try again.**
2. The only path: capture cookies from a real Chrome session that's
   already logged in, drop them into `requests.Session()`, replay.
3. Auth is the only Akamai-protected boundary. Once you have authed
   cookies, every subsequent endpoint behaves like a normal HTTP API.
4. The exchange flow is 5–6 stateful steps; the params are documented
   below.
5. Working code is at `/tmp/ii_hawaii_per_island.py` and the proof
   results are at `/tmp/ii_hawaii_per_island/*.html`.

---

## 1. What kills you (failure modes seen tonight)

| Failure | What it looks like | Don't fall for this |
|---|---|---|
| Akamai login rejection | POST `/web/my/auth/login` returns 302 → `/web/my/auth/loginPage`. No error message in body, just the login form re-served. **Even valid creds fail this way.** | Stop trying to log in headlessly. |
| Patchright login | Same as above. Patchright beats fingerprinting but Akamai's deeper auth-layer challenge is on a different code path. | Same — abandon. |
| Empty CSRF response from `POST /web/csrf` | `r.text` is empty even though status=200. **This is normal** for unauthed sessions; it's not "session expired." | Don't interpret empty body as failure. CSRF token comes from a `GET /web/csrf` script response, not POST. |
| 302 → `/web/cs?a=5` | Means session lost auth. Cookie's gone stale or you posted to a member-only endpoint without member cookies. | Verify cookies fresh before retrying. |
| `Required information missing` headline | Form rejected because a required field was missing or had a value the server's `validateSearchForm()` JS would have caught. | Read the visible page text — it tells you what's missing. |
| `Maximum Sleep Capacity: 6` | Andy's 2BR caps at 6 occupants. Submitting `numberOfAdults>6` (or adults+children>6) gets rejected. | Cap travel party at the deposit's sleep capacity. |
| Page titled "Shopping Cart" with "We are processing your request" | Tiny 5959-byte JS-redirect page. `requests` won't follow it because it's a JS-only redirect, not an HTTP redirect. | Construct the redirect URL yourself (see §6). |
| `loginPage` in the response URL or "Member Login" in body | Cookies expired or session lost. | Re-export cookies from real Chrome. |

---

## 2. Cookie capture procedure

This is the only manual step in the loop. Every other thing automates.

### 2.1 What cookies matter (and why)

The full cookie set Andy provides has ~25 entries. Of those, **7 are
load-bearing** — the rest are tracking/analytics crud that the server
doesn't gate on:

| Cookie | Purpose | Who sets it | Lifetime |
|---|---|---|---|
| `JSESSIONID` | Spring Security session ID | Server, on auth | Session-scoped (until logout/timeout) |
| `BIGIP-INT` | Internal F5 BIG-IP load balancer affinity (which app server) | Server | Session-scoped |
| `BIGIP-EXT` | External F5 BIG-IP load balancer affinity | Server | Session-scoped |
| `__uzma` | **Akamai bot-clear cookie** (UUID identifying the cleared session) | Akamai sensor JS | ~30 days, refreshed on activity |
| `__uzmb` | Akamai timestamp anchor | Akamai sensor JS | Same |
| `__uzmc` | Akamai action counter | Akamai sensor JS | Refreshed every page |
| `__uzmd` | Akamai latest-activity timestamp | Akamai sensor JS | Refreshed every page |
| `__uzme` | Akamai score / fingerprint hash | Akamai sensor JS | Same |

`__uzm*` is the magic. Akamai's bot detection writes these cookies into
your browser AFTER you pass its JS sensor challenge. Once a real human
has those cookies, every subsequent request from that cookie set is
treated as bot-cleared. **This is why headless login fails but
cookie-replay succeeds.** We're inheriting the bot-clearance Andy's
real Chrome already earned.

Less critical but include them anyway:

- `MEMNO` (member number — `3430769` for Andy)
- `loginId` (`tilloat`)
- `MINFO` (member context, includes home resort and expiry)
- `OptanonConsent` (cookie banner state — sometimes gates
  destination dropdowns)
- `BILLCODE`, `CCODE` (member country / billing)
- `_fbp`, `__eoi`, `__gads` (FB/ads tracking — harmless filler, but
  removing them looks suspicious)

### 2.2 Cookie domain + path quirks (Patchright nightmare)

When you load these into a Playwright/Patchright browser context, you
**MUST** set domain + path correctly or the request won't include them:

```python
DOTTED_DOMAIN = (
    "__eoi", "__gads", "_fbp", "BILLCODE", "CCODE", "loginId",
    "MEMNO", "MINFO", "OptanonAlertBoxClosed", "OptanonConsent",
)
# All others go on www.intervalworld.com
# JSESSIONID specifically lives at path /web (not /)

cookies_list = []
for name, val in CK.items():
    cookies_list.append({
        "name": name, "value": val,
        "domain": ".intervalworld.com" if name in DOTTED_DOMAIN
                  else "www.intervalworld.com",
        "path": "/web" if name == "JSESSIONID" else "/",
        "secure": True,
        "sameSite": "Lax",
    })
ctx.add_cookies(cookies_list)
```

For `requests.Session()` it's simpler — just `S.cookies.update(dict)`
and it auto-handles domain matching for the common case.

### 2.3 How to capture cookies from a logged-in Chrome (manual, ~15 sec)

Andy's manual procedure tonight:

1. Log into intervalworld.com in normal Chrome.
2. Open DevTools (`Cmd+Opt+I`) → **Application** tab.
3. Left sidebar: **Storage > Cookies > https://www.intervalworld.com**.
4. Select all rows (Cmd+A), copy. Or use the **Network** tab → click any
   request → right pane → "Cookie:" header → copy value.
5. Paste into a JSON file or env var.

Productized version (when we get to it): a Chrome extension running in
Andy's normal browser that watches `intervalworld.com` cookie changes
and POSTs them to crab. ~30 lines of manifest-v3 JS. Lifecycle: silent;
he never thinks about it.

### 2.4 Cookie expiration reality

- **Idle timeout:** Spring Security default is 30 minutes. JSESSIONID
  dies if no traffic. **Cron-ping `/web/my/home` every 20 min to
  keep idle alive.**
- **Absolute timeout:** Unknown for II, probably 8–24h normal,
  **14 days** with `_spring_security_remember_me=on` (Andy uses this).
- **`__uzm*`:** survive longer if they keep rotating through real
  human-ish activity, otherwise expire ~hours.

### 2.5 The actual cookie set used tonight (sample, sanitized)

Stored at `/tmp/ii_cookies.json`. Format:

```json
{
  "cookies": {
    "JSESSIONID": "YpKZDat7Y0hqGy9Eo-G67qT6",
    "BIGIP-EXT": "316675756.58148.0000",
    "BIGIP-INT": "!eSWWH38nrQTKKjtT0hjyW7xqIl5M1qurGFtjMJ4dbodkwmo4ORRbvjeQpc7ERW6WJ1FDEGuR1jbaF/0=",
    "__uzma": "0e6e3019-eaf6-481b-b4ce-67061446d073",
    "__uzmb": "1777423813",
    "__uzmc": "8628220253364",
    "__uzmd": "1777424254",
    "__uzme": "6287",
    "MEMNO": "3430769",
    "loginId": "tilloat",
    "MINFO": "5EEEMEMNO_3430769_MZIP_980112232_MCNTC2_USA_MACTCD_L_MRESRT_RSD_MTYPE_II_MEXPDT_2019-09-12",
    "EXSTILLOAT": "CitySearch#hawaii#09/13/2026#09/26/2026#7#0#",
    "BILLCODE": "1", "CCODE": "USA",
    "OptanonConsent": "<long URL-encoded blob>",
    "_fbp": "fb.1.<id>",
    "__gads": "<id>", "__eoi": "<id>",
    "s_cc": "true", "s_eVar1_persist": "3430769",
    "s_eVar27_persist": "N_76652", "s_eVar28_persist": "EN",
    "OptanonAlertBoxClosed": "<iso timestamp>",
    "hideDepositInterruption": "true"
  }
}
```

The `EXSTILLOAT` cookie is interesting — it stores the user's last search
query (`CitySearch#hawaii#09/13/2026#09/26/2026#7#0#`). Useful as a
debugging breadcrumb but not load-bearing.

---

## 3. Required headers

These are sent on every request. Mismatched UA or missing Origin/Referer
can flip Akamai into bot mode mid-session:

```python
S.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/130.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.intervalworld.com",
    "Referer": "https://www.intervalworld.com/web/cs?a=1000",
})
```

The Referer specifically matters — some endpoints (`?a=225`, `?a=227`)
seem stricter about cross-origin POST without a same-origin Referer.
Match the Referer to where the form would have been on a real-browser
flow.

---

## 4. CSRF token extraction (the surprise gotcha)

Spring Security uses OWASP CSRFGuard. The token is **NOT** in:
- a `<meta name="csrf-token">` tag
- a Set-Cookie header
- the `/web/csrf` POST response body (that's empty)

It IS in:
- a hidden `<input name="OWASP_CSRFTOKEN" value="..."/>` on every
  form-bearing page
- the `/web/csrf` GET response body, embedded as a JS string literal
  inside ~13KB of OWASP CSRFGuard JavaScript:

```javascript
// /web/csrf returns this kind of JS:
//   var owaspCSRFGuardScriptName = ...;
//   ...
//   var owaspCSRFToken = "PFOM-HJSJ-NUIR-YRGF-O1WX-AIIW-A2YZ-ANY4";
```

Extract via regex:

```python
def csrf(S):
    """Pull the OWASP_CSRFTOKEN from /web/csrf JS body."""
    r = S.get(
        f"https://www.intervalworld.com/web/csrf?timestamp={int(time.time()*1000)}",
        timeout=15,
    )
    m = re.search(r'(["\'])([A-Z0-9]{4}(?:-[A-Z0-9]{4}){5,})\1', r.text)
    return m.group(2) if m else None
```

The token format is 8 groups of 4 alphanum chars separated by dashes:
`PFOM-HJSJ-NUIR-YRGF-O1WX-AIIW-A2YZ-ANY4`.

Tokens are **session-scoped, sticky** — same token across many requests
within a session. Don't refresh on every request; once per session is
fine, refresh only if a request returns a 302 to login or "CSRF token
mismatch."

How tokens are submitted:

- As URL param: `?OWASP_CSRFTOKEN=<token>` on the action URL
- Or as form body field: `OWASP_CSRFTOKEN=<token>`
- Most endpoints accept either; we send both for safety.

---

## 5. The 6-step exchange wizard (production-ready code)

### 5.1 Endpoint map

| # | Method | URL | Purpose |
|---|---|---|---|
| 1 | GET | `/web/my/home` | Bootstrap session (verifies cookies still valid) |
| 2 | GET | `/web/csrf?timestamp=<ms>` | Fetch CSRFGuard JS, extract token |
| 3 | GET | `/web/cs?a=204` | "My Units" — lists deposits + transactionID |
| 4 | POST | `/web/cs?a=225&OWASP_CSRFTOKEN=<csrf>` | Pick which deposit to use |
| 5 | POST | `/web/cs?a=227` | Pick lock-off portion (full / master / studio) |
| 6 | (handle cart-wait JS redirect — see §6) | | |
| 7 | GET | `/web/cs?a=240&r=<random>&saveSearch=true&transactionID=<id>` | Lands on "More Options" search form |
| 8 | POST | `/web/cs?a=203&OWASP_CSRFTOKEN=<csrf>` | Submit search criteria (destination + dates + party) |
| 9 | (handle cart-wait again on every search) | | Each new search fires a fresh cart-wait |
| 10 | GET | `/web/cs?a=240&r=<random>&saveSearch=true&transactionID=<id>` | Lands on results page |

### 5.2 Body shapes (verbatim from working scrape)

#### Step 4 — `POST /web/cs?a=225` (pick deposit)

```python
{
    "transactionID": <numeric, 16-digit>,
    "resortKey": "The_Royal_SandsRSD",   # Andy's home resort
    "hashcode": "RSD00000000000O26FKK51333820260000000000000000000000",
    "x": "64",                            # image-button click coords
    "y": "42",
    "OWASP_CSRFTOKEN": <token>,
}
```

The hashcode encoding (decoded from the visible ones):

```
  RSD                               # resort code
  0000000000000                     # padding
  O26F                              # something? consistent across years
  KK5133                            # unit number (K5133 = full)
  3                                 # bedrooms
  82026                             # use year 2026 with prefix
  0000000000000000000000            # more padding
```

For the 1BR master side: `RSD00000000000O14FKK5133S38202600...`
For the studio side:    `RSD00000000000O02NKK5133R38202600...`
For 2027 cycle, replace `82026` with `82027`. The `O02N`/`O14F`/`O26F`
prefixes appear to encode the unit-side identifier — not yet
reverse-engineered fully but **don't need to be** — pull them straight
from the My Units HTML.

#### Step 5 — `POST /web/cs?a=227` (lockoff pick)

```python
{
    "transactionID": <numeric>,
    "unitNumber": "K5133",        # K5133 = full 2BR (max trading power)
                                  # K5133S = 1BR master
                                  # K5133R = studio lockoff
    "Submit": "Continue",
}
```

Note: `Submit=Continue` IS sent on this step (different from step 8).

#### Step 8 — `POST /web/cs?a=203` (search submission)

```python
{
    "transactionID": <numeric>,
    "searchType": "CitySearch",      # or "ResortSearch" or "AllSearch"
    "destinationID": "<32-char hex>", # one of the IDs in §7
    "fromDate": "MM/DD/YYYY",
    "toDate": "MM/DD/YYYY",
    "numberOfAdults": "<≤6>",         # CAPPED by deposit's max sleep
    "numberOfChildren": "<≤6-adults>",
    "ageOfChildren": "[I@5a3307db",   # placeholder string for empty
                                      # children array; see note below
    "OWASP_CSRFTOKEN": <token>,
    # NO "Submit" field — that triggers a different validation path
}
```

`ageOfChildren=[I@5a3307db` is weird — looks like a Java
`Object.toString()` representation of an empty `int[]` (the format is
`[I@<hash>` where `[I` means "array of int"). Just hardcode it; II
doesn't seem to validate the value as long as the field is present.

#### Step 10 — final results page

`GET /web/cs?a=240&r=<random>&saveSearch=true&transactionID=<id>`

Response: 85KB HTML titled `Interval International | Destination Units
Available for My Exchange`. Contains the results section with resorts
or "did not find any availability" + surrounding-areas list.

---

## 6. Cart-wait redirect (the JS redirect we have to fake)

Multiple wizard transitions go through an intermediate page titled
"Shopping Cart" that's just a JS redirect. The page is 5959 bytes,
contains:

```html
<script>
  registerOnLoadEvent(iwCartWait_onLoadEvent);
  function iwCartWait_onLoadEvent() {
     window.location.replace(
       "/web/cs?a=240&r=" + Math.random() +
       "&saveSearch=true&transactionID=7075014541208453"
     );
  }
</script>
```

`requests` cannot execute this. **Construct the URL yourself:**

```python
import random

def follow_cart_wait(S, response, transactionID):
    """If response is the JS-redirect cart-wait page, hit the real
    target ourselves. Returns the new response or the original."""
    if "Shopping Cart" in response.text and "processing your request" in response.text:
        url = (
            f"https://www.intervalworld.com/web/cs?a=240"
            f"&r={random.random()}"
            f"&saveSearch=true"
            f"&transactionID={transactionID}"
        )
        return S.get(url, timeout=60, allow_redirects=True)
    return response
```

The `r=<random>` param is a cache-buster. Any random number works.

`saveSearch=true` is the magic — it persists the deposit selection
across multiple search submissions in the same session, so you don't
have to re-walk steps 4–5 for each search.

---

## 7. Hawaii destinationIDs (the constant lookup)

These 32-char hex IDs are stable per-destination. They came from the
disambiguation page when searching `searchCriteria=hawaii`:

```python
HAWAII_DESTINATIONS = {
    "Kailua Kona, HI":     "A11583336ED7869EC712A280EC907FE4",
    "Maui Island, HI":     "A117D33354C4AD9EC92289F1BC907FFC",
    "Kauai Island, HI":    "A117D3332C33799F33A2868FFD907FFE",
    "Oahu Island, HI":     "A117D3336BE68F9F33A27E681C908000",
    "Kihei, Maui, HI":     "20A7093D1D214483929626E9050765E2",
    "Molokai Island, HI":  "305F02F6564F45BC98BDB0EECE3B543C",
    "Big Island, HI":      "A117D3332C33799F33A2868FFD908000",
}
```

To discover others (e.g., Florida, Mexico): submit a search with
`searchCriteria=<query>` and parse the `<select name="destinationID">`
options on the disambiguation page. Each destination type (city,
region, country) uses the same 32-char hex format.

---

## 8. Result page parsing

The results page contains a section starting with the word `Availability`
and ending with `N Resorts found`. Within that, results are rendered as
a table with columns: name, area, code, rating, dates, unit, price.

The HTML is messy (whitespace + `&nbsp;` + tracking pixels mixed in).
The parser that worked tonight:

```python
def parse_results(html):
    """Extract structured availability rows from a /web/cs?a=240 result page."""
    clean = re.sub(r'<(?:script|style)[^>]*>[\s\S]*?</(?:script|style)>', '', html)
    clean = re.sub(r'<!--[\s\S]*?-->', '', clean)
    plain = re.sub(r'<[^>]+>', ' ', clean)
    plain = re.sub(r'\s+', ' ', plain).strip()

    avail_idx = plain.find('Availability')
    end_idx = plain.find('Resorts found')
    if avail_idx < 0 or end_idx < 0:
        return {"matched": False, "results": []}

    section = plain[avail_idx:end_idx + 100]

    # Pattern matches: name, area, "HI USA", code, rating, reviews,
    # dates, [unit info], price
    pat = re.compile(
        r'([A-Z][\w\' ]{3,40}(?:\s+[A-Z][\w\' ]{2,30}){0,4})'
        r'\s+([A-Z][\w \']{3,40})\s+,\s+HI\s+,\s+USA\s+'
        r'([A-Z]{3,4})\s+'
        r'(?:Overall\s+)?Rating\s*Rating\s+([\d.]+)\s+out\s+of\s+5\s+'
        r'(\d+)\s+Member\s+Ratings'
        r'[\s\S]{0,80}?'
        r'(\w{3}\s+\d{1,2}\s+2026)\s*-\s*(\w{3}\s+\d{1,2}\s+2026)'
        r'[\s\S]{0,40}?'
        r'(\d{1,4}\.\d{2}|\d{2,4}\.0)'
    )
    results = []
    for m in pat.finditer(section):
        results.append({
            'resort': m.group(1).strip(),
            'area': m.group(2).strip(),
            'code': m.group(3),
            'rating': m.group(4),
            'reviews': int(m.group(5)),
            'check_in': m.group(6),
            'check_out': m.group(7),
            'price_usd': float(m.group(8)),
        })

    no_avail = re.search(r"did not find any availability for ['\"](.*?)['\"]", section)
    return {
        "matched": bool(results),
        "results": results,
        "note": (
            f"surrounding areas (no direct match for {no_avail.group(1)})"
            if no_avail else "direct match"
        ),
    }
```

This works for HI results. For non-HI, generalize the `,\s+HI\s+,\s+USA`
pattern. Better long-term: write per-region parsers, since II's HTML
varies subtly by destination type.

---

## 9. Full working script (production seed)

This is the script that produced the Ke Nani Kai result tonight.
Saved at `/tmp/ii_hawaii_per_island.py`. Annotated:

```python
import json, re, requests, random, time
from pathlib import Path

CK = json.loads(Path("/tmp/ii_cookies.json").read_text())["cookies"]

def make_session():
    S = requests.Session()
    S.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/130.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.intervalworld.com",
        "Referer": "https://www.intervalworld.com/web/cs?a=1000",
    })
    S.cookies.update(CK)
    S.get("https://www.intervalworld.com/web/my/home", timeout=20)
    return S

def csrf(S):
    r = S.get(
        f"https://www.intervalworld.com/web/csrf?timestamp={int(time.time()*1000)}",
        timeout=15,
    )
    return re.search(r'(["\'])([A-Z0-9]{4}(?:-[A-Z0-9]{4}){5,})\1', r.text).group(2)

def commit_deposit(S, resort_key, hashcode, unit_number):
    """Walks steps 3–7. After this returns, S is ready for searches.
    Returns transactionID."""
    ct = csrf(S)
    mu = S.get("https://www.intervalworld.com/web/cs?a=204", timeout=20)
    trans = re.search(r'name="transactionID"[^>]*value="(\d+)"', mu.text).group(1)
    S.post(
        f"https://www.intervalworld.com/web/cs?a=225&OWASP_CSRFTOKEN={ct}",
        data={"transactionID": trans, "resortKey": resort_key,
              "hashcode": hashcode, "x": "64", "y": "42",
              "OWASP_CSRFTOKEN": ct},
        timeout=30,
    )
    S.post(
        "https://www.intervalworld.com/web/cs?a=227",
        data={"transactionID": trans, "unitNumber": unit_number,
              "Submit": "Continue"},
        timeout=30,
    )
    S.get(
        f"https://www.intervalworld.com/web/cs?a=240&r={random.random()}"
        f"&saveSearch=true&transactionID={trans}",
        timeout=60,
    )
    return trans

def search(S, transactionID, destinationID, fromDate, toDate, adults=6, children=0):
    ct = csrf(S)
    r = S.post(
        f"https://www.intervalworld.com/web/cs?a=203&OWASP_CSRFTOKEN={ct}",
        data={
            "transactionID": transactionID,
            "searchType": "CitySearch",
            "destinationID": destinationID,
            "fromDate": fromDate,   # MM/DD/YYYY
            "toDate": toDate,       # MM/DD/YYYY
            "numberOfAdults": str(adults),
            "numberOfChildren": str(children),
            "ageOfChildren": "[I@5a3307db",
            "OWASP_CSRFTOKEN": ct,
        },
        timeout=60, allow_redirects=True,
    )
    # Handle JS redirect
    if "Shopping Cart" in r.text and "processing your request" in r.text:
        r = S.get(
            f"https://www.intervalworld.com/web/cs?a=240&r={random.random()}"
            f"&saveSearch=true&transactionID={transactionID}",
            timeout=60,
        )
    return r

# Usage:
# S = make_session()
# trans = commit_deposit(S, "The_Royal_SandsRSD",
#                        "RSD00000000000O26FKK51333820260000000000000000000000",
#                        "K5133")
# r = search(S, trans, HAWAII_DESTINATIONS["Maui Island, HI"],
#            "09/01/2026", "10/05/2026", adults=6)
# results = parse_results(r.text)
```

---

## 10. Result captured tonight

Hawaii sweep, Sept 1 – Oct 5 2026, K5133 full 2BR (sleeps 6), 6 adults:

| Destination searched | Result |
|---|---|
| Kailua Kona, HI | KNK Molokai Sep 26-Oct 3 — $249 (surrounding) |
| Maui Island, HI | KNK Molokai Sep 26-Oct 3 — $249 (surrounding) |
| Kauai Island, HI | KNK Molokai Sep 26-Oct 3 — $249 (surrounding) |
| Oahu Island, HI | KNK Molokai Sep 26-Oct 3 — $249 (surrounding) |
| Kihei, Maui, HI | KNK Molokai Sep 26-Oct 3 — $249 (surrounding) |
| Molokai Island, HI | KNK Molokai Sep 26-Oct 3 — $249 (direct) |
| Big Island, HI | KNK Molokai Sep 26-Oct 3 — $249 (surrounding) |

The single result:

```
Resort:    Ke Nani Kai
Code:      KNK
Area:      Kaluakoi, Molokai, HI, USA
Unit:      2 Bedroom, Sleeps 6
Dates:     Sep 26 2026 → Oct 03 2026
Rating:    4.0/5 (26 reviews)
Exchange:  $249.00
```

Result file: `/tmp/ii_hawaii_per_island/Molokai_Island_HI.html`
(direct match; the same KNK appears in all 7 island result files but
labeled "surrounding area" for non-Molokai searches).

---

## 11. Operator-mode productionization roadmap

To turn tonight's working scrape into a service:

### 11.1 Schema

```sql
CREATE TABLE crab.timeshare_ii_session (
    pk_id SERIAL PRIMARY KEY,
    cookies_encrypted BYTEA NOT NULL,    -- via fernet or KMS
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ,
    expires_estimated_at TIMESTAMPTZ,    -- last_used + 30min idle
    is_healthy BOOLEAN DEFAULT TRUE
);

CREATE TABLE crab.timeshare_availability_snapshots (
    pk_id BIGSERIAL PRIMARY KEY,
    group_id UUID REFERENCES crab.timeshare_groups(group_id) ON DELETE CASCADE,
    deposit_hashcode VARCHAR(60) NOT NULL,    -- which deposit was used
    destination_id VARCHAR(40) NOT NULL,      -- the 32-char hex
    destination_name VARCHAR(120),
    from_date DATE NOT NULL,
    to_date DATE NOT NULL,
    adults INTEGER NOT NULL,
    children INTEGER DEFAULT 0,
    fetched_at TIMESTAMPTZ DEFAULT NOW(),
    matched BOOLEAN NOT NULL,
    surrounding_area BOOLEAN DEFAULT FALSE,   -- was it a "surrounding" alt?
    raw_html_path TEXT,                       -- GCS path if we keep raw
    n_results INTEGER NOT NULL,
    note TEXT
);

CREATE TABLE crab.timeshare_availability_results (
    pk_id BIGSERIAL PRIMARY KEY,
    snapshot_id BIGINT REFERENCES crab.timeshare_availability_snapshots(pk_id) ON DELETE CASCADE,
    resort_name VARCHAR(200) NOT NULL,
    resort_code VARCHAR(8) NOT NULL,          -- e.g. KNK
    area VARCHAR(120),
    rating_avg NUMERIC(3,1),
    rating_count INTEGER,
    check_in DATE NOT NULL,
    check_out DATE NOT NULL,
    bedrooms INTEGER,
    sleeps INTEGER,
    exchange_fee_usd NUMERIC(8,2),
    raw JSONB
);
```

### 11.2 Cloud Run worker

`worker/ii_scraper.py`:
- Loads cookies from `crab.timeshare_ii_session`
- For each group with `is_active`, for each Considering resort,
  for each upcoming travel-window in their cycle plans:
  - Walk `commit_deposit` once per (deposit, lockoff) combo
  - `search` each destination
  - Parse, write to `availability_snapshots` + `availability_results`
- Rate-limit: ≤200 search POSTs/day total across all groups
- Spread queries across the day; never burst
- Detect 302→loginPage and mark session unhealthy → alert

### 11.3 Cron schedule

```yaml
# cron.yaml — append:
- description: "Timeshare — II availability snapshot sweep"
  url: /tasks/timeshare-snapshot-sweep
  schedule: every 6 hours from 06:00 to 18:00
  timezone: America/Los_Angeles

- description: "Timeshare — keep-alive ping for II session"
  url: /tasks/timeshare-ii-keepalive
  schedule: every 20 minutes
  timezone: America/Los_Angeles
```

The keep-alive cron just hits `/web/my/home` to bump JSESSIONID's idle
timer; no heavy work.

### 11.4 Chrome extension (eliminates manual cookie capture)

Manifest v3, ~30 lines:

```javascript
// background.js
chrome.cookies.onChanged.addListener(async (info) => {
  if (info.cookie.domain.endsWith("intervalworld.com")) {
    const cookies = await chrome.cookies.getAll({ domain: ".intervalworld.com" });
    const cookies2 = await chrome.cookies.getAll({ domain: "www.intervalworld.com" });
    const all = Object.fromEntries(
      [...cookies, ...cookies2].map(c => [c.name, c.value])
    );
    await fetch("https://crab.travel/api/timeshare/ii-cookies/refresh", {
      method: "POST",
      headers: {"Content-Type": "application/json", "Authorization": `Bearer ${API_KEY}`},
      body: JSON.stringify({cookies: all, captured_at: Date.now()}),
    });
  }
});
```

Trigger: any cookie change on intervalworld.com → POST fresh set to crab.

### 11.5 Health surface in admin

`/timeshare/admin/ii-session` page (owner-only):

- Green: cookies fresh (< 12h old, last keep-alive succeeded)
- Amber: cookies aging (< 12 days, still working)
- Red: cookies expired or last keep-alive returned login redirect
- "Last successful query: 4 minutes ago"
- "Next scheduled sweep: 14:00 PT today"
- Manual "Run sweep now" button

---

## 12. Things we proved DON'T work (don't try them again)

1. **Patchright/stealth-chromium login as `tilloat`**. Akamai rejects
   even with valid creds via 302→loginPage. Tested with Pickles06,
   Pickles07, Pickles08 (all the password rotations Andy did during
   testing). The login form renders correctly; the auth POST is
   rejected silently. Evidence: `/tmp/ii_patchright_probe.py` and
   `/tmp/ii_patchright/04_after_login.png`.

2. **`searchType=ResortSearch` with a city/keyword**. ResortSearch
   expects an II resort code (3-4 chars like RSD, NVW, MAW). Use
   CitySearch + destinationID for keyword queries.

3. **Submitting `numberOfAdults>6` with K5133 deposit**. Validation
   blocks at the deposit's max-sleep capacity. The form bounces back
   with "Required information missing." Always cap at the deposit
   max.

4. **POST to `/web/csrf`**. Returns empty body. The token is in the
   GET response (as JS) or in form HTML.

5. **Following the JS redirect with `requests`'s native handling**.
   `requests` only follows HTTP 3xx redirects; the cart-wait page is a
   JS redirect via `window.location.replace()`. Construct the URL
   yourself.

6. **Calling `/web/cs?a=240` without first walking `?a=225`+`?a=227`**.
   The transactionID isn't bound to a deposit yet. Returns "More
   Options" but no deposit context — searches will fail validation.

---

## 13. Andy's deposit + identifiers (canonical reference)

### Deposits in `crab.timeshare_*`

```
Member: tilloat (II member #3430769)
Resort: The Royal Sands, Cancun, MX
II resort code: RSD
Resort key (per II): The_Royal_SandsRSD
Unit: K5133 (full 2BR lock-off; sleeps 6)
  - K5133S = master 1BR (sleeps 4)
  - K5133R = lockoff studio (sleeps 2)
Usage: biennial_even (2026, 2028, 2030, …)
Week: 38 (Sep 19 – Sep 26 each cycle)
```

### Deposit hashcodes (extracted live from /web/cs?a=204)

```
2026 K5133  full 2BR:    RSD00000000000O26FKK51333820260000000000000000000000   ← REAL
2026 K5133S 1BR master:  RSD00000000000O14FKK5133S3820260000000000000000000000   ← REAL
2026 K5133R lockoff:     RSD00000000000O02NKK5133R3820260000000000000000000000   ← REAL
2027 K5133  full 2BR:    RSD00000000000O26FKK51333820270000000000000000000000   ← UI ARTIFACT
2027 K5133S 1BR master:  RSD00000000000O14FKK5133S3820270000000000000000000000   ← UI ARTIFACT
2027 K5133R lockoff:     RSD00000000000O02NKK5133R3820270000000000000000000000   ← UI ARTIFACT
```

These are stable across sessions (same hashcode appears in every My
Units pull). Cache them; they encode the unique (resort, year, unit)
tuple.

**IMPORTANT (added 2026-04-28):** Andy's contract is biennial-even, so
only 2026/2028/2030 are real use-years. The 2027 entries appear in II's
My Units page as a calendar-display artifact — depositing/exchanging a
2027 hashcode would be REJECTED by II's backend. Per Andy's April 20
audit (in his Drive folder, doc `II Account Audit — Cross-Reference
Report (April 20 2026)`), Royal Resorts Owner Relations Mgr Julio
Ibarra confirmed biennial-even directly. Don't act on 2027 hashcodes.

### Sleep capacities

```
K5133  (full 2BR):  6 adults max
K5133S (1BR master): 4 adults max
K5133R (studio):    2 adults max
```

II will reject searches with `adults+children > deposit_capacity`.

---

## 14. Tomorrow's likely tasks (in priority order)

1. **Sweep 2027** with each unit-side (K5133, K5133S, K5133R) across
   broader date windows (May-Nov) to find better Hawaii options. The
   2027 deposit pool is independent of 2026.
2. **Sweep wider date range** for 2026 — May, June, July, October,
   November. Hawaii summer/winter has more inventory than September.
3. **Try K5133S (1BR master) at the same dates** — different trading
   power, possibly different/more matches in 1BR units.
4. **Productize the working scraper** into Cloud Run + a "Live
   availability" widget on the timeshare dashboard.
5. **Build the Chrome extension** for cookie auto-sync, eliminating
   manual cookie capture.
6. **Test the keep-alive cron** to determine actual idle timeout —
   start at 20-min cadence and try extending to find the ceiling.

---

## 15. Useful file paths

```
/tmp/ii_cookies.json              - the captured cookies (current session)
/tmp/ii_hawaii_per_island.py      - the working scraper (final tonight)
/tmp/ii_hawaii_per_island/*.html  - 7 result files, one per island
/tmp/ii_full_exchange/*.html      - earlier per-step debug captures
/tmp/ii_proof/*.html              - earlier exchange-flow exploration
/tmp/ii_patchright_probe.py       - the failed Patchright login (dead end)
/tmp/ii_patchright/               - failed-login screenshots (negative evidence)
/tmp/ii_search_with_cookies.py    - the first working cookie-based test (getaway only)
/tmp/ii_step2_maui.py             - getaway step-2 disambig probe
/tmp/ii_full_search.py            - getaway-search end-to-end script

/Users/at/Desktop/code/_antiquated_code/timeshare/har/www.intervalworld.com.har    - first HAR (legacy)
/Users/at/Desktop/code/_antiquated_code/timeshare/har/www.intervalworld.com2.har   - 85MB HAR with full session including login flow + searches
/Users/at/Desktop/code/_antiquated_code/timeshare/har/www.intervalworld.com3.har   - latest HAR (Apr 28, includes password reset 07→08, no cookies)
```

---

## 16. Don't forget

- Cookies expire. Plan for it from day one. A scraper that doesn't
  detect "session is dead" is useless — it'll silently return wrong
  results.
- II rate-limits are unknown. We did ~10 searches tonight on the
  `tilloat` account without issues. Going much higher without
  rate-limiting risks account flagging.
- **Andy's account is the single point of failure** in operator mode.
  If `tilloat` gets banned, the entire service goes dark. Keep query
  patterns conservative and human-paced.
- Never script the "Request Exchange" or "Confirm Exchange" buttons.
  Those are commit actions. The scraper is read-only by design.
- The HAR captures are gold for understanding the wire format. Save
  new ones whenever the flow changes (II's UI updates).
