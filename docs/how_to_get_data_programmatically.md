# How to get data programmatically — Andy's guidebook

**Audience:** Andy (and future-Claude resuming work).
**Purpose:** every working technique, in priority order, for getting data
out of a third-party site without doing it manually. With copy-paste code
for each. Updated whenever a new technique gets battle-tested.

**Last updated:** 2026-04-28 ~9:45pm — based on the Royal Sands /
Interval International / Redweek / Marriott / VRBO sweep done tonight.

---

## 0. TL;DR — pick the right rung

Before writing any scraper, ask: **what is between you and the data?**

| What's blocking you | Use this | Time cost |
|---|---|---|
| Nothing — public site, friendly | `requests.get()` with a UA header | 30 sec |
| `403 Forbidden` from raw curl/requests | Stealth Playwright (`pw` + `stealth-chromium`) | 2 min |
| Login required, no bot wall | Headless login with Playwright or requests Session | 5 min |
| Login required + Akamai/Cloudflare/DataDome on the auth boundary | **Cookie replay** from real Chrome | 10 min |
| Need cookies repeatedly without manual capture | `browser_cookie3` reading Chrome's local SQLite | 10 min |
| Need cookies refreshed automatically when they rotate | Mac LaunchAgent watching cookies + POSTing to crab | 30 min |
| Need cookies kept "warm" 24/7 server-side | App Engine cron pinging the site every 18-29 min | 1 hr |
| Site uses CAPTCHA or aggressive IP reputation | Stop. Pay a service or use the user's own browser via CDP attach | n/a |

**The first rule:** never write a custom login flow when you can replay
cookies from the user's real browser. Akamai, Cloudflare Turnstile,
DataDome, etc. all gate AT THE LOGIN BOUNDARY. Inheriting a cleared
session sidesteps the entire bot-detection layer.

---

## 1. The stealth ladder (matches `~/.claude/skills/playwright/SKILL.md`)

Always start at Rung 2 for any third-party site. Vanilla headless is
expected to fail.

### Rung 1 — Vanilla `requests` or `playwright headless: true`
```python
import requests
r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0 ...'}, timeout=20)
```
**Use for:** your own deployed sites, friendly third parties, public docs,
TUG forums (sometimes), outagedown.com, ComplaintsBoard, RSS feeds.
**Expected to fail on:** Marriott, VRBO, Cloudflare-protected sites,
anything with Akamai bot manager.

### Rung 2 — Stealth headless via `pw` + `stealth-chromium` 🥇
```javascript
// /tmp/scrape.js
const { chromium } = require('stealth-chromium');  // built-in to ~/Desktop/code/_infrastructure/deploy/playwright
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage();
await page.goto(url, { timeout: 30000, waitUntil: 'domcontentloaded' });
await page.waitForTimeout(4000);   // let lazy content load
const body = await page.innerText('body');
```
Run with `pw /tmp/scrape.js`.

**This is your default for any third-party site.** Tested 2026-04-28 against
Booking.com (real availability returned), VRBO (real listings returned),
Redweek (full rental + resale listings).

**Beat tonight:** Booking.com, VRBO, Redweek's resort-detail pages.
**Failed on:** Marriott.com (Access Denied — Akamai), KAYAK (returned
junk results). Use the Booking.com aggregator pattern (§3) when the
property's own site is locked.

### Rung 3 — Stealth `headless: 'new'` (Chrome's new headless mode)
```javascript
const { chromium } = require('stealth-chromium');
const browser = await chromium.launch({ headless: 'new' });
```
Chrome's new headless mode shares a binary with headed Chrome — fingerprint
is much closer to real. Use when Rung 2 fails on the same target.

### Rung 4 — Xvfb + headed (Linux/Cloud Run only)
```bash
apt install xvfb
xvfb-run -a pw /path/to/script.js
```
```javascript
const browser = await chromium.launch({ headless: false });
```
**Not needed on macOS** — local headed already gets a real display fingerprint.
**Critical for Cloud Run** — when you ship a stealth scraper there.

### Rung 5 — Patchright / Camoufox / nodriver (the hardest targets)
Python alternatives when stealth-chromium fails:
- **Patchright:** patched Chromium fork, drop-in `from patchright.async_api import async_playwright as pw`. `pip install patchright && python -m patchright install chromium`. **Note:** Patchright loses against Akamai on the LOGIN POST specifically (proven against Interval International tonight, see §6).
- **Camoufox:** Firefox stealth fork, highest community-reported evasion rates 2025/26. `pip install camoufox && python -m camoufox fetch`.
- **nodriver:** Selenium-descended Python lib, strong against Cloudflare Turnstile.

Use these for: Cloudflare Turnstile, Kasada, full PerimeterX. **Never used in
crab tonight** — stealth-chromium handled all our targets.

### Rung 6 — Headed (DEBUGGING ONLY)
```bash
pw --inspect-brk /path/to/script.js
```
Use this only to **watch** what breaks, never to defeat detection. On macOS
your fingerprint is already fine in Rung 2; on Linux use Rung 4 instead.

---

## 2. Cookie sources — every way to get authed cookies into a script

The critical insight: **once you have authed cookies, every endpoint is
plain HTTP.** Auth boundaries are the only place anti-bot systems usually
gate. So getting cookies is the whole game.

### 2.1 Manual paste from DevTools (the bootstrap)

Used at least once per credential rotation. Procedure:
1. Log into the target site in normal Chrome (with "Remember me" checked
   if available — adds 14-day persistent token).
2. DevTools (`Cmd+Opt+I`) → **Application** tab.
3. Left sidebar: **Storage > Cookies > https://target.com**.
4. Select all (`Cmd+A`), copy.
5. Paste into a JSON file the script reads.

```python
# /tmp/cookies.json
{"cookies": {"JSESSIONID": "abc...", "session_id": "def...", ...}}
```

```python
import json, requests
CK = json.load(open('/tmp/cookies.json'))['cookies']
S = requests.Session()
S.cookies.update(CK)
S.headers.update({'User-Agent': 'Mozilla/5.0 ...'})
S.get('https://target.com/protected/endpoint')
```

**When it works:** Anything where the login boundary is bot-protected but
the rest of the site is plain HTTP. Tested against Interval International
tonight — full exchange flow works with cookie-replay.

**Caveats:**
- Cookies expire (Spring Security default 30 min idle, plus longer
  remember-me windows). Plan for re-capture.
- Some cookies require specific domain/path attributes — see playbook
  §2.2 for II-specific quirks.

### 2.2 `browser_cookie3` — read cookies from Chrome's local SQLite

Eliminates the manual DevTools paste. Reads cookies straight off disk
and decrypts via macOS Keychain.

```bash
pip install browser-cookie3
```

```python
import browser_cookie3, requests

cj = browser_cookie3.chrome(domain_name='intervalworld.com')
S = requests.Session()
S.cookies = cj   # requests-compatible CookieJar
S.headers.update({'User-Agent': 'Mozilla/5.0 ...'})
S.get('https://intervalworld.com/web/my/home')
```

**One-time gotcha on macOS:** first run prompts the macOS Keychain for
"Python wants to access Chrome Safe Storage" — click Always Allow.
Persistent forever after.

**When it works:** Andy logs in normally in Chrome, the script reads
cookies whenever it needs them. Great for cron-style jobs running locally.

**Doesn't work for:** server-side cron (App Engine can't read Andy's
laptop's Chrome). For that, use 2.4 (LaunchAgent push pattern).

### 2.3 CDP attach to real running Chrome

Drives Andy's actual Chrome session via Chrome DevTools Protocol. Best for
when you need Chrome to actually navigate (click, scroll, fill forms)
inside an authed session.

```bash
# Step 1: Andy starts Chrome with debug port using his real profile
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/Library/Application Support/Google/Chrome"
```

```javascript
// Step 2: Python or JS attaches to it
const { chromium } = require('playwright');
const browser = await chromium.connectOverCDP('http://localhost:9222');
const ctx = browser.contexts()[0];   // existing context with all his cookies
const page = await ctx.newPage();
await page.goto('https://intervalworld.com/web/cs?a=204');
```

**When it works:** Akamai-protected member areas. The browser IS Andy —
it has his bot-clearance, cookies, history, fingerprint. Indistinguishable
from real use because it IS real use.

**Why we didn't use it tonight for II:** the cookie-replay pattern (§2.1)
was simpler and worked. CDP-attach is heavier — kept in reserve as the
escalation path if cookie-replay alone ever stops working.

### 2.4 Mac LaunchAgent — auto-sync cookies to crab

The "never paste cookies again" pattern. A small Python script runs as a
LaunchAgent on Andy's Mac, reading Chrome cookies via `browser_cookie3`
and POSTing them to crab.travel whenever they change.

```python
# ~/.local/bin/sync_ii_cookies.py
import browser_cookie3, requests, time, os

cj = browser_cookie3.chrome(domain_name='intervalworld.com')
cookies = {c.name: c.value for c in cj}
if not cookies.get('JSESSIONID'):
    print('not logged in, skipping')
    exit(0)

resp = requests.post(
    'https://crab.travel/api/timeshare/ii-cookies/refresh',
    headers={'Authorization': f'Bearer {os.environ["CRAB_TASK_SECRET"]}'},
    json={'cookies': cookies, 'source': 'mac_launchagent', 'captured_at': time.time()},
    timeout=15,
)
print(resp.json())
```

```xml
<!-- ~/Library/LaunchAgents/com.tillo.crab.ii-cookie-sync.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.tillo.crab.ii-cookie-sync</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/at/.local/bin/sync_ii_cookies.py</string>
    </array>
    <key>StartInterval</key>
    <integer>300</integer>  <!-- every 5 min -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>CRAB_TASK_SECRET</key>
        <string>dev</string>   <!-- match crab's env -->
    </dict>
    <key>StandardOutPath</key>
    <string>/tmp/ii-cookie-sync.log</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.tillo.crab.ii-cookie-sync.plist
```

**Built but not deployed in crab as of 2026-04-28.** The endpoint
(`/api/timeshare/ii-cookies/refresh`) is live; the LaunchAgent script and
plist are documented here but not yet installed. Install when Andy decides
he wants the truly hands-off II flow.

### 2.5 Server-side keep-alive cron (cookies stay warm without re-login)

Once cookies are seeded into crab's DB, a cron pings the target every
18-29 min so the JSESSIONID idle-timeout never fires.

```python
# In crab: utilities/timeshare_ii_session.py (already deployed)
def keepalive_ping(member_login='tilloat'):
    row = get_session_row(member_login)
    cookies = row['cookies']

    last = row['last_keepalive_at']
    elapsed = (now() - last).total_seconds()
    target = 18*60 + random.random() * 11*60   # 18-29 min jitter
    if elapsed < target:
        return {'status': 'deferred', 'next_in_sec': round(target - elapsed)}

    S = requests.Session()
    S.headers.update({'User-Agent': 'Mozilla/5.0 ...'})
    S.cookies.update(cookies)
    r = S.get('https://intervalworld.com/web/my/home', timeout=20)

    healthy = r.status_code == 200 and 'My Account' in r.text and 'loginPage' not in r.url
    update_health(healthy=healthy, last_used=now())
    if r.cookies:   # server may have rotated some cookies — capture them
        merge_and_upsert(dict(r.cookies))
    return {'status': 'healthy' if healthy else 'unhealthy'}
```

**cron.yaml entry:**
```yaml
- description: "Timeshare — II keep-alive (cookies stay hot, never re-login)"
  url: /tasks/timeshare-ii-keepalive
  schedule: every 18 minutes
  timezone: America/Los_Angeles
```

**Cost:** ~$0.04/month worst case on App Engine F1, capped at $1/mo via
GCP budget alert. Math + approval in `docs/timeshare_buildout.md` §16.

**Result:** cookies effectively live forever. Andy logs in to II in
normal Chrome maybe once every 14 days; the cron + LaunchAgent (when
deployed) keep crab's copy fresh in between.

---

## 3. The aggregator pattern — when the target's own site bot-blocks you

Many premium booking sites (Marriott.com, Hilton.com, Disney) have
sophisticated Akamai/Cloudflare protection. **Their data is on aggregators
that DO let stealth Playwright through.**

```
TARGET BLOCKED:                AGGREGATOR INSTEAD:
Marriott.com                →  Booking.com / KAYAK / Hotels.com / Trip.com
Hilton.com                  →  Booking.com
VRBO direct                 →  VRBO via stealth-chromium worked tonight
Airbnb direct               →  Use search via stealth, listing detail still hard
Disney Aulani direct        →  Vacatia / Mouse House Magic aggregators
```

**Tonight's example:** Marriott's Maui Ocean Club returned `Access Denied`
on direct hit, but Booking.com search for the same property + dates
returned full availability with prices in seconds.

```javascript
// Search Booking.com with stealth headless, dates pre-filled
const url = `https://www.booking.com/searchresults.html?ss=${encodeURIComponent('Marriott Maui Ocean Club')}&checkin=2026-09-19&checkout=2026-09-26&group_adults=6&no_rooms=1`;
await page.goto(url, { timeout: 30000, waitUntil: 'domcontentloaded' });
await page.waitForTimeout(5000);
const body = await page.innerText('body');
// Extract: $XXX per night, $X,XXX for N nights, "Sleeps 6", etc.
```

Direct booking aggregator URLs that worked tonight:
- `https://www.booking.com/searchresults.html?ss=NAME&checkin=YYYY-MM-DD&checkout=YYYY-MM-DD&group_adults=N&no_rooms=1`
- `https://www.booking.com/hotel/us/<slug>.html?checkin=YYYY-MM-DD&checkout=YYYY-MM-DD&group_adults=N&no_rooms=1`
- `https://www.vrbo.com/search?destination=<URL-encoded>&startDate=YYYY-MM-DD&endDate=YYYY-MM-DD&adults=N&minBedrooms=2`

---

## 4. Multi-source web search — `multi_search.py`

When you don't know which page has the data, fan out across 5 keyless
sources in parallel. Lives at
`~/Desktop/code/_infrastructure/deep_search/multi_search.py`.

```bash
python3 ~/Desktop/code/_infrastructure/deep_search/multi_search.py \
  "<query>" --max 8

# Source-tune for tech vs reference:
python3 multi_search.py "<query>" --sources ddg,hackernews,stackexchange,github
python3 multi_search.py "<fact lookup>" --sources ddg,wikipedia
```

Sources hit in parallel threads, capped at 20s overall:
- DuckDuckGo
- Hacker News (Algolia API)
- Stack Exchange
- Wikipedia
- GitHub

**Why not `WebSearch` built-in tool:** it returns 0 results intermittently.
Multi-source has redundancy.

**Pattern for finding Reddit URLs without Reddit's broken search:**
```bash
python3 multi_search.py "site:reddit.com <topic>" --max 8
# DDG results contain the actual best Reddit URLs.
# Then scrape those URLs explicitly with the deep-search scrape.py.
```

---

## 5. The Reddit pattern — `deep-search/scrape.py`

Reddit blocks raw JSON for some queries (403). The scraper has 3 fallback
strategies; `scrape.py` handles them automatically when given a URL.

```bash
cd ~/Desktop/code/_infrastructure/deep_search && \
source venv_deep_search/bin/activate

# Best: scrape a known URL — uses old.reddit JSON → Arctic Shift → Playwright fallback
python scrape.py "https://reddit.com/r/<sub>/comments/<id>/<slug>/"

# When Reddit's keyword search is broken (403 or junk results):
# 1. Find URLs via DDG site:reddit.com search (§4)
# 2. Scrape those URLs explicitly (URL-mode has playwright fallback)
```

**Hard rule:** never `--pick 1` blindly on `--search`. Reddit's relevance
ranker is garbage for niche queries — pulls in BORUpdates gossip, r/UFOs,
etc. Either:
1. Drive scrape selection from DDG `site:reddit.com` results (Pattern B)
2. List with `--search` then examine titles, scrape the right one (Pattern C)

---

## 6. HAR file mining — when you don't know the wire format

When the target uses a JS-heavy frontend (Spring + Thymeleaf, React,
custom SPA), you can't just guess endpoints. Capture a real session in
HAR format and reverse-engineer.

### How to capture a HAR
1. Open Chrome DevTools → Network tab.
2. Click ⏺ to start recording, ✓ "Preserve log".
3. Perform the user flow you want to automate (login, search, etc.).
4. Right-click any request → "Save all as HAR with content".
5. Save into your project's `har/` directory.

### How to mine it
```python
import json, re
h = json.load(open('session.har'))
for e in h['log']['entries']:
    if e['request']['method'] == 'POST' and 'target.com' in e['request']['url']:
        url = e['request']['url']
        body = e['request'].get('postData', {}).get('text', '')
        print(f'{url}\n  BODY: {body[:300]}\n')
```

**Tonight's win:** the II forgot-password flow was buried in HAR2
(81 MB capture). Mining it surfaced:
- `POST /web/my/auth/login` body (j_username, j_password, _spring_security_remember_me)
- `POST /web/my/account/answerSecurityQuestions` body (security answer in plaintext: "bryar")
- `POST /web/my/account/changePasswordSubmit` body (new password)

Without HAR mining, this would have been guesswork.

---

## 7. What I tried tonight that DID NOT work (avoid these)

### ❌ Headless login on Akamai-protected sites
Every variant fails the same way: 302 → loginPage even with valid creds.
- Vanilla Playwright + `j_username/j_password` POST — fails
- Patchright + same form — fails (Pickles06, 07, 08 all rejected)
- Camoufox not tested but unlikely to help — Akamai's auth-layer fingerprint
  check is on a different code path than its general bot-clearance check

**Always use cookie replay or CDP attach for Akamai-protected logins.**

### ❌ KAYAK direct hit with city codes
URL format `https://www.kayak.com/hotels/<city-cXXXXX>/<dates>/2adults`
returned junk results (Vrsar, Croatia instead of Lahaina, Hawaii). City
codes are stale or non-public. Use Booking.com search-by-name pattern
instead.

### ❌ Aulani Disney direct
`disneyaulani.com/rooms/` returned "Application Error" on stealth-chromium.
Use Vacatia or Mouse House Magic aggregators.

### ❌ Marriott.com direct
Akamai blocks all stealth attempts at the rates page. Use Booking.com /
KAYAK / Trip.com aggregator search-by-name pattern.

### ❌ Trying to download timeshare contract docs from email
Tonight didn't try this, but worth noting: Royal Resorts CSF statements
and contract scans came in via Andy's existing dossier (manual work
he did April 10-20). Don't try to re-fetch from Gmail unless the
dossier is missing something specific.

### ❌ Polling Reddit search via JSON when blocked
When `scrape.py --search ... --subreddit X` hits 403, **do not retry
with different params**. The JSON endpoint is rate-limited at the IP/UA
level. Use the DDG `site:reddit.com` workaround instead.

### ❌ Long Cron sleeps + retry loops in foreground
If you need to wait for something async (cron tick, eventual consistency),
use `Bash run_in_background: true` with an `until` loop or `Monitor` —
NEVER chain sleeps in a foreground command.

---

## 8. Operational patterns

### 8.1 GCP $1 budget alert (safety net before any scrape pings start)

Always set a budget alert before deploying any cron that hits external
sites. Andy's pattern:

```bash
# Enable the API once
gcloud services enable billingbudgets.googleapis.com --project=PROJECT_ID

# Create the budget alert
BILLING_ACCT=$(gcloud billing projects describe PROJECT_ID --format='value(billingAccountName)' | sed 's|billingAccounts/||')
gcloud billing budgets create \
  --billing-account="$BILLING_ACCT" \
  --billing-project=PROJECT_ID \
  --display-name="PROJECT_ID — \$1 cap" \
  --budget-amount=1USD \
  --threshold-rule=percent=0.5 \
  --threshold-rule=percent=0.9 \
  --threshold-rule=percent=1.0 \
  --threshold-rule=percent=1.5,basis=current-spend \
  --filter-projects=projects/PROJECT_ID
```

Andy gets an email at 50%, 90%, 100%, and 150% of $1.

### 8.2 App Engine cron pattern (`cron.yaml` + Flask handler)

```yaml
# cron.yaml
cron:
- description: "Description for Cloud Console"
  url: /tasks/<endpoint>
  schedule: every 18 minutes
  timezone: America/Los_Angeles
```

```python
# tasks_routes.py
@bp.route('/tasks/<endpoint>')
def task_handler():
    task_secret = os.environ.get('CRAB_TASK_SECRET', 'dev')
    # Cron-only OR manual w/ ?secret=
    if not request.headers.get('X-Appengine-Cron') and request.args.get('secret') != task_secret:
        return 'Forbidden', 403
    # ... do the work
    return jsonify({'success': True, ...})
```

### 8.3 The cookie refresh API pattern

Standard route in `tasks_routes.py`:
```python
@bp.route('/api/<service>/cookies/refresh', methods=['POST'])
def cookies_refresh():
    expected = os.environ.get('CRAB_TASK_SECRET', 'dev')
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer ') or auth[7:] != expected:
        return jsonify({'success': False}), 401
    body = request.get_json()
    cookies = body.get('cookies', {})
    if not cookies.get('CRITICAL_COOKIE_NAME'):
        return jsonify({'success': False, 'error': 'missing critical cookie'}), 400
    upsert_cookies(cookies, source=body.get('source', 'manual'))
    return jsonify({'success': True, 'keys': len(cookies)})
```

### 8.4 Cloud SQL (kumori shared instance) connection pattern

```python
# In utilities/postgres_utils.py — already battle-tested across all crab projects
from utilities.postgres_utils import get_db_connection
import psycopg2.extras

conn = get_db_connection()
try:
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM crab.<table> WHERE ... = %s", (val,))
    rows = cur.fetchall()
finally:
    conn.close()
```

**Pool discipline:** maxconn=6 per app, statement_timeout=30s, total
shared pool is 50 conn / 12 apps. Never hold a connection across an HTTP
boundary; always close in finally.

---

## 9. The Google Drive read pattern (Andy's dossiers)

Andy keeps research dossiers in Drive — pulling them programmatically
saves re-asking him for context.

```python
import pickle
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

with open('/Users/at/Desktop/code/kumori/credentials/google_token.pickle', 'rb') as f:
    creds = pickle.load(f)
if creds.expired:
    creds.refresh(Request())

drive = build('drive', 'v3', credentials=creds)

# List a folder
res = drive.files().list(
    q=f"'{FOLDER_ID}' in parents and trashed=false",
    fields="files(id,name,mimeType,modifiedTime,size)",
    pageSize=200,
).execute()

# Export a Doc as plain text
text = drive.files().export(fileId=DOC_ID, mimeType='text/plain').execute().decode('utf-8')

# Export a Sheet as CSV
csv = drive.files().export(fileId=SHEET_ID, mimeType='text/csv').execute().decode('utf-8')

# Download a binary file (PDF, image)
raw = drive.files().get_media(fileId=FILE_ID).execute()
```

**Token location:** `/Users/at/Desktop/code/kumori/credentials/google_token.pickle`
(unified token, all scopes, see `~/.claude/skills/google-credentials/SKILL.md`).

**Re-auth if token dies:**
```bash
cd /Users/at/Desktop/code/kumori
source venv_kumori/bin/activate
python credentials/create_unified_token.py
```

---

## 10. Quick recipes

### Recipe A — Scrape a single bot-protected URL (most common case)
```javascript
// /tmp/quick_scrape.js
const { chromium } = require('stealth-chromium');
const fs = require('fs');
(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({
    userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    viewport: { width: 1440, height: 900 },
  });
  await page.goto(URL, { timeout: 30000, waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(4000);
  const body = await page.innerText('body');
  fs.writeFileSync('/tmp/scrape_out.html', await page.content());
  fs.writeFileSync('/tmp/scrape_out.txt', body);
  await browser.close();
})();
```
```bash
pw /tmp/quick_scrape.js
```

### Recipe B — Replay cookies from JSON file (auth-locked endpoint)
```python
import json, requests
CK = json.load(open('/tmp/site_cookies.json'))['cookies']
S = requests.Session()
S.headers.update({
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Origin': 'https://target.com',
    'Referer': 'https://target.com/landing',
})
S.cookies.update(CK)
r = S.get('https://target.com/protected/endpoint', timeout=20)
```

### Recipe C — Aggregator search with stealth (when target itself blocks)
```javascript
const { chromium } = require('stealth-chromium');
const url = `https://www.booking.com/searchresults.html?ss=${encodeURIComponent(NAME)}&checkin=${CHECKIN}&checkout=${CHECKOUT}&group_adults=${ADULTS}&no_rooms=1`;
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage();
await page.goto(url, { timeout: 30000, waitUntil: 'domcontentloaded' });
await page.waitForTimeout(5000);
// Pull prices: $X per night $Y total
const body = (await page.innerText('body')).replace(/\s+/g, ' ');
const prices = [...body.matchAll(/\$([\d,]+)\s*per\s*night\s*\$([\d,]+)/g)];
```

### Recipe D — Multi-source DDG search for a topic
```bash
python3 ~/Desktop/code/_infrastructure/deep_search/multi_search.py "<topic>" --max 8
```

### Recipe E — Read a cookie blob via browser_cookie3
```python
import browser_cookie3, requests
cj = browser_cookie3.chrome(domain_name='target.com')
S = requests.Session()
S.cookies = cj
S.get('https://target.com/protected')
```

### Recipe F — HAR mine for endpoints
```python
import json, re
h = json.load(open('session.har'))
seen = set()
for e in h['log']['entries']:
    if e['request']['method'] == 'POST':
        path = e['request']['url'].split('?')[0]
        if path in seen: continue
        seen.add(path)
        body = e['request'].get('postData', {}).get('text', '')[:200]
        print(f"POST {path}\n  {body}")
```

### Recipe G — Drive doc fetch
```python
text = drive.files().export(fileId=ID, mimeType='text/plain').execute().decode('utf-8')
```

### Recipe H — Listing/extracting prices from any page text
```python
import re
collapsed = re.sub(r'\s+', ' ', body)
for m in re.finditer(r'\$\s*([\d,]+(?:\.\d{2})?)', collapsed):
    p = int(m.group(1).replace(',', '').split('.')[0])
    if 100 <= p <= 10000:
        ctx = collapsed[max(0,m.start()-150):m.end()+100]
        if any(kw in ctx.lower() for kw in ['night', 'total', 'week', '/wk']):
            print(f"${p}: {ctx[-200:]}")
```

---

## 11. Site-specific notes (what worked tonight)

### Interval International (intervalworld.com)
- **Login:** ❌ headless impossible (Akamai). Cookie replay only.
- **Cookie capture:** DevTools paste OR `browser_cookie3.chrome(domain_name='intervalworld.com')`.
- **Member-area endpoints:** plain HTTP, all work via `requests.Session()` + cookies.
- **Wire-format reference:** `docs/ii_scraper_playbook.md` (full endpoint map, body shapes, gotchas).
- **Keep-alive:** App Engine cron every 18-29 min hitting `/web/my/home`.

### Royal Sands / Royal Resorts
- Public site (no member auth needed) — friendly.
- Customer service responds to email within 3-7 days.
- Contacts in `docs/2026apr28_855pm.md` §6.

### Redweek (redweek.com)
- **Resort detail pages (`/resort/PXXX-<slug>`):** Stealth-chromium worked ✅.
- **Filtered search (`/search?...`):** returned 0 results in stealth tests — use the resort-detail page instead.
- **Listing pages (`/posting/RXXXXXXX`):** Stealth-chromium worked, full pricing visible.
- **Rentals + resales sub-pages:** scroll to load lazy content (4-8 scrolls of 1500px each).

### Booking.com
- **Search by name + dates:** Stealth-chromium worked ✅, returned full availability.
- **Single property page with date params:** Stealth-chromium worked ✅.
- **Pattern:** `?ss=<name>&checkin=YYYY-MM-DD&checkout=YYYY-MM-DD&group_adults=N&no_rooms=1`.

### VRBO (vrbo.com)
- **Search results:** Stealth-chromium worked ✅ — returned 9 listings with prices.
- **Pattern:** `?destination=<URL-encoded>&startDate=YYYY-MM-DD&endDate=YYYY-MM-DD&adults=N&minBedrooms=2`.

### KAYAK
- Returned junk results (city codes broken). Skip — use Booking.com instead.

### Marriott direct (marriott.com)
- ❌ Stealth-chromium hit `Access Denied` on Edge/Akamai. Use Booking.com aggregator.

### Disney Aulani direct (disneyaulani.com)
- ❌ "Application Error" on stealth-chromium.
- Use Vacatia.com or Mouse House Magic aggregator pages.

### Reddit (reddit.com / old.reddit.com)
- **URL scrape:** `scrape.py "<url>"` works (3-strategy fallback).
- **Search:** flaky — use DDG `site:reddit.com` first to find URLs, then scrape.

### TUG forums (tugbbs.com)
- ❌ Reddit-shaped scraper doesn't work (different platform).
- Public threads are accessible via `requests.get()` with UA, but full thread scrape needs custom XenForo parsing OR manual read.

### Outagedown.com / ComplaintsBoard
- Friendly to vanilla `requests.get()` with UA. No stealth needed.

---

## 12. The escalation flowchart

```
GET https://target.com → 200, content present? ──── YES → done.
                  │
                  ▼ NO
Try with UA header → 200? ──────────────────────── YES → done.
                  │
                  ▼ NO
Try `pw` + stealth-chromium headless → 200, content? ── YES → done.
                  │
                  ▼ NO
Stealth + headless: 'new' → 200? ─────────────────── YES → done.
                  │
                  ▼ NO
Linux/Cloud Run + Xvfb + headed → 200? ─────────── YES → done (Linux only).
                  │
                  ▼ NO
Patchright / Camoufox / nodriver → 200? ──────────── YES → done.
                  │
                  ▼ NO
Is there an aggregator (Booking, KAYAK, Vacatia, Mouse House)
that indexes this target? ────────────────────────── YES → use that with stealth.
                  │
                  ▼ NO
Is the user logged in to this site in real Chrome? ── YES → cookie replay (§2.1)
                                                            or CDP attach (§2.3).
                  │
                  ▼ NO
Stop. Either the user logs in once (then cookie replay
works forever via keep-alive), or pay a service like
Scrapfly/ZenRows/Hypersolutions, or accept manual.
```

---

## 13. Anti-patterns — never do these

1. **Don't manually copy/paste data into a spreadsheet** when stealth-chromium
   could do it in 30 seconds. Andy hates manual.
2. **Don't fall back to vanilla headless when Rung 2 is available.** Always
   start at stealth-chromium for any third-party site.
3. **Don't write a custom login flow for an Akamai-protected site.** Cookie
   replay or CDP attach. Always.
4. **Don't trust KAYAK city codes** — they're stale. Use named-resort search
   on Booking.com instead.
5. **Don't `--pick 1` blindly on Reddit search.** Always examine titles or
   drive selection from DDG.
6. **Don't sleep + retry in foreground** for async waits. Use `run_in_background`
   with `until` loop, or Monitor.
7. **Don't skip the GCP budget alert** when deploying any new cron that
   hits external sites. $1 alert is the safety net.
8. **Don't paste credentials into shell history** — always read from env
   vars or `/Users/at/Desktop/code/kumori/credentials/`.
9. **Don't `rm -rf`, `git push --force`, `gcloud reset` etc. without
   explicit user consent.** Per `~/.claude/CLAUDE.md` global rules.
10. **Don't attribute commits to Claude.** No `Co-Authored-By: Claude`,
    no "Generated with Claude Code" footer. Per project + global rules.

---

## 14. When a new target shows up — first 5 minutes

1. **`curl -A 'Mozilla/5.0 ...' https://target.com`** — does it return content?
   - 200 + content → vanilla Python `requests` will work. Skip the stealth ladder.
   - 403 / "Access Denied" / "Bot or Not?" / Cloudflare challenge HTML → continue.
2. **Try Rung 2 (`pw` + `stealth-chromium`)** — Recipe A above.
3. **If still blocked, search for an aggregator** — `multi_search.py "<target> rate price availability site:booking.com OR site:kayak.com OR site:vacatia.com"`.
4. **If user-auth required** — ask user to log in once in Chrome with "Remember me", then DevTools-paste cookies into a JSON file. Never write a custom headless login flow against Akamai/Cloudflare-protected auth.
5. **Document what worked** — append to §11 of this doc so future sessions don't re-test.

---

*— Andy Tillo + Claude session 2026-04-28 ~9:45 PM PT*
