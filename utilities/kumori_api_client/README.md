# kumori_api_client — consumer-facing free-tier client

Drop this folder into any sibling project (galactica, kindness_social, heathers_plate, dandy, …) to call kumori.ai's free-tier `/api/v1/*` surface: text LLM with backend fallback, image edit (flux-2-klein-4b), text→image (flux-1-schnell / pollinations / stable horde), and vision describe.

One Python file (`client.py`, ~330 lines) + a tiny `__init__.py`. No external deps beyond `requests`.

---

## 1. Vendor it into your project

In your project's `deploy.json` (or equivalent), add:

```json
"shared_files": [
  {
    "from": "../kumori/shared/kumori_api_client/__init__.py",
    "to":   "utilities/kumori_api_client/__init__.py"
  },
  {
    "from": "../kumori/shared/kumori_api_client/client.py",
    "to":   "utilities/kumori_api_client/client.py"
  }
]
```

Deploy tool copies these in pre-deploy so prod has them. (Don't symlink — App Engine / Cloud Run won't follow symlinks.)

---

## 2. Provision a per-consumer API key

One secret per consuming app, all in the **`kumori-404602`** GCP project's Secret Manager:

| Consumer app    | Secret name (convention)        |
|-----------------|---------------------------------|
| galactica       | `PILGRIMS_KUMORI_API_KEY`       |
| kindness_social | `KINDNESS_KUMORI_API_KEY`       |
| heathers_plate  | `HEATHERS_KUMORI_API_KEY`       |
| dandy           | `DANDY_KUMORI_API_KEY`          |

The secret value is whatever bearer token kumori issues for that app. Each key has its own **20K calls/day** quota (tracked in `kumori_api_usage`), so consumers don't crowd each other.

To mint a new key: add a row to kumori's API-key table (TBD canonical helper) and put the bearer string in Secret Manager.

---

## 3. Initialize at app boot

The client lazily fetches the bearer key on the first call. You wire it up once at startup:

```python
from utilities.kumori_api_client import init

# Critical gotcha: your project's get_secret() probably defaults to YOUR
# project's Secret Manager. PILGRIMS_KUMORI_API_KEY etc. live in
# kumori-404602, so force the project_id:
init(
    get_secret_fn=lambda name: get_secret(name, project_id='kumori-404602'),
    api_key_name='PILGRIMS_KUMORI_API_KEY',   # ← your app's secret name
)
```

Local dev shortcut — skip the secret manager entirely:

```bash
export KUMORI_API_KEY=<paste-bearer-token>
```

The `KUMORI_API_KEY` env var overrides everything else, so it's the easy escape hatch.

---

## 4. The four entry points

### `llm_chat_resilient(backends, messages, ...)` — text LLM with fallback chain

```python
from utilities.kumori_api_client import llm_chat_resilient

text, backend, attempts, debug_info = llm_chat_resilient(
    backends=[
        'openrouter-hermes',                       # 405B Hermes (strongest)
        'mistral-mistral-large-latest',
        'sambanova-meta-llama-3.3-70b-instruct',
        'github-llama-70b',
    ],
    messages=[{'role': 'user', 'content': 'hello'}],
    system='You are a terse assistant.',
    max_tokens=500,
    temperature=0.4,
    min_chars=20,        # short replies trigger fallback to next backend
    debug=False,         # set True to capture every upstream HTTP call
)
```

Returns `(text, winning_backend, per_attempt_log, debug_info)`. First backend that returns ≥`min_chars` wins. `attempts` is a list of `{backend, ok, error, ms, chars}` for every backend tried.

### `imggen_edit(prompt, target_image_b64, reference_images_b64=None, ...)` — Klein-4B multi-ref edit

```python
from utilities.kumori_api_client import imggen_edit

res = imggen_edit(
    prompt='Captain stands beside the rover at dusk, Mars cartoon style.',
    target_image_b64=base64_jpeg_str,             # the anchor / first image
    reference_images_b64=[ref1_b64, ref2_b64],    # up to 3 refs (4 total cap)
    width=1024, height=768,                        # multiples of 16, ≤4 MP
    # ── Attribution (post 2026-05-11 — see §7 below; strongly encouraged) ──
    feature='aria_journal.generate_trace',         # sub-operation label
    verbiage='ARIA dusk journal — Mars cartoon style, 2 refs',  # human description
    caller_user_id='andy@gmail.com',               # end user behind the call
    tags={'aria_pool_size': 30, 'mood': 'observational'},  # arbitrary JSONB
    debug=False,
)
# res = {'ok': True, 'image_b64': '...', 'provider': 'cloudflare_flux2_klein_edit',
#        'ms': 1820, '_debug': {'upstream_calls': [...]}}
```

### `imggen_generate(prompt, width, height, mode='roundrobin')` — text→image

```python
from utilities.kumori_api_client import imggen_generate

res = imggen_generate(
    'A red Martian landscape at dusk', 1024, 1024,
    feature='aria_journal.scene_backdrop',        # attribution kwargs as above
    verbiage='Mars dusk landscape backdrop',
)
# Tries pollinations → cloudflare flux-1-schnell → stable horde in order.
```

### `imggen_usage(date=None, platform=None, limit=50)` — per-platform usage view

```python
from utilities.kumori_api_client import imggen_usage

u = imggen_usage(limit=10)
# {
#   'ok': True, 'date': '2026-05-12',
#   'totals': {'calls': 22, 'errors': 5, 'neurons_estimated': 2480.0,
#              'pct_of_daily_pool': 24.8},
#   'per_platform': {'galactica': {'calls': 18, 'errors': 3,
#                                   'neurons_estimated': 2200.5}, ...},
#   'per_model': {'cloudflare_flux2_klein_edit': {...}, ...},
#   'recent_calls': [{datetime, platform, feature, verbiage,
#                     model, ok, error_code, neurons_estimated,
#                     duration_ms, tags}, ...],
#   'cf_reconciliation': {
#     'cf_neurons_today': 2503.13,         # CF GraphQL authoritative
#     'kumori_neurons_estimated_today': 2480.0,
#     'drift_pct': 0.93,                   # under 5% = healthy
#     'fetched_at': '...', 'note': '...',
#   },
# }
```

This is what powers admin dashboards (galactica's `/admin/kumori-journal`, etc). **Do not roll your own neuron math** — that's exactly what this endpoint replaces. Kumori owns the data, the constants, and the CF GraphQL reconciliation. See §7.

### `describe_image(image_url=..., image_b64=..., prompt='Describe.', mime=...)` — vision LLM

```python
from utilities.kumori_api_client import describe_image

res = describe_image(image_b64=jpeg_b64, prompt='List every visible object.',
                     mime='image/jpeg')
# res = {'ok': True, 'text': '...', 'backend': 'groq-llama4-scout', 'ms': 1240}
```

Fallback chain (set inside the kumori service): `groq-llama4-scout` (fastest, ~1.9s) → `gemini-2.5-flash` (best prose) → openrouter Gemma / Nemotron / NVIDIA 90B.

---

## 5. Full HTTP trace (debug console pattern)

Every entry point accepts `debug=True` and returns a `_debug.upstream_calls` list — every outbound HTTP call kumori made to Cloudflare / Groq / Mistral / GitHub Models / OpenRouter, with full request and response bodies (base64 fields auto-redacted to `<N chars base64>` so logs stay readable).

For a top-down trace of *your* HTTP calls to kumori, use `set_request_log`:

```python
from utilities.kumori_api_client.client import set_request_log

log = []
set_request_log(log)
try:
    text, backend, *_ = llm_chat_resilient(['openrouter-hermes'],
                                            [{'role':'user','content':'hi'}],
                                            debug=True)
finally:
    set_request_log(None)

# `log` now contains every HTTP call your client made to kumori, with
# request_body / response_body (base64-redacted), status, ms, headers.
```

This is what powers galactica's `/admin/kumori-journal` debug console — bi-directional HTTP capture (your→kumori + kumori→upstream) in a single payload. Copy that template if you're building a tuning UI.

---

## 6. Daily-cap awareness

### The caps

- **Your bearer key:** 20K calls/day across all endpoints (tracked in `kumori_api_usage`). 429 once exceeded.
- **Cloudflare neurons:** **10K neurons / Cloudflare account / fixed UTC-calendar-day**. Shared across `flux-1-schnell` + `flux-2-klein-4b` + `dreamshaper-8-lcm`. Klein at 1024² ≈ 110-125 neurons/call (~80/day). At 2048² ≈ 420-440 neurons/call (~22/day). Shared across ALL kumori consumers (galactica + kindness + heathers + …) — one consumer's burst locks out everyone else.
- **Other providers:** individual per-provider caps tracked server-side; surface in `llm_usage()` if you need a real-time view.

### Reset semantics — IMPORTANT

The CF neuron cap is a **fixed window, resets at 00:00 UTC each day**. NOT a rolling 24-hour window. Concretely:

| Behavior | What this means for you |
|---|---|
| Fixed window | At 00:00:00 UTC the counter goes 9,999 → 0 instantaneously. No gradual recovery. |
| UTC-anchored | 5pm PT today = 00:00 UTC tomorrow. 8pm ET = 00:00 UTC tomorrow. The reset time-of-day depends on your timezone. |
| Account-wide | All consumers + all CF Workers AI models share the SAME 10K bucket. |
| No partial reset | If you blow the cap at 14:00 UTC, you have 10 hours of zero capacity until reset. There is no "wait an hour for some headroom" — it's all-or-nothing. |
| No real-time "remaining" API | CF does NOT expose a REST endpoint that returns "neurons left today." The only authoritative real-time signal is an actual inference call's response (200 = available, 429+errorCode 4006 = exhausted). Analytics (GraphQL `aiInferenceAdaptiveGroups`) lags by **hours** during sustained bursts — do not trust the "used so far today" number as ground truth. Verified 2026-05-12: analytics reported 25% used while inference rejected with daily-cap-exhausted. |

To check the live state without making a real inference: `imggen_usage()` includes a `cf_reconciliation.live_state` field sourced from kumori's cached probe. That field IS the authoritative signal; the `cf_neurons_today` number next to it is for context only.

### Building a usage panel

```python
from utilities.kumori_api_client import imggen_usage
u = imggen_usage()                              # today, all platforms
u = imggen_usage(platform='galactica')          # just your slice
u = imggen_usage(date='2026-05-11', limit=200)  # historical, more rows
```

See §4 for the response shape. The `cf_reconciliation` block surfaces drift between kumori's per-call estimate (computed locally) and Cloudflare's GraphQL-reported `totalNeurons` (analytics — laggy). If drift > 5% kumori's per-tile neuron constants need updating — log a kumori issue, don't reimplement the math in your app.

### A worked dashboard render (Jinja + vanilla JS — paste-and-go)

The galactica admin page does exactly this. Copy this pattern for any new consumer dashboard.

**Backend Flask route** (your app's admin endpoint — proxies kumori's response so the frontend doesn't expose the API key):

```python
@app.route('/admin/my-imggen-usage')
def admin_my_imggen_usage():
    if not is_admin(session.get('user_id')):
        return jsonify({'error': 'admin only'}), 403
    from utilities.kumori_api_client import imggen_usage
    return jsonify(imggen_usage(platform='your_app_name', limit=50))
```

**Template** (`admin_my_imggen_usage.html`):

```html
<div id="imggen-state"></div>
<table id="imggen-recent"><thead>
  <tr><th>Time</th><th>Feature</th><th>Verbiage</th><th>OK</th><th>Neurons</th></tr>
</thead><tbody></tbody></table>

<script>
async function loadImggenUsage() {
  const r = await fetch('/admin/my-imggen-usage');
  const j = await r.json();
  if (!j.ok) { document.getElementById('imggen-state').textContent = 'fetch failed'; return; }
  // ── Headline state (authoritative — from kumori's live probe, NOT analytics) ──
  const cf = j.cf_reconciliation || {};
  const live = cf.live_state || 'unknown';
  const banner = document.getElementById('imggen-state');
  const colors = { available: 'green', daily_cap_exhausted: 'red',
                   capacity_throttled: 'orange', other_error: 'gray' };
  banner.style.color = colors[live] || 'gray';
  banner.innerHTML = `<strong>State: ${live.toUpperCase()}</strong>` +
    (cf.reset_in_human ? ` — resets in ${cf.reset_in_human}` : '') +
    (cf.cf_error_message ? `<div style="font-size:12px;opacity:.7">CF: ${cf.cf_error_message}</div>` : '');
  // ── Recent calls ──
  const tbody = document.querySelector('#imggen-recent tbody');
  tbody.innerHTML = '';
  for (const c of (j.recent_calls || [])) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${c.datetime}</td><td>${c.feature || ''}</td>` +
      `<td>${(c.verbiage || '').slice(0, 80)}</td>` +
      `<td>${c.ok ? '✓' : '✗ ' + (c.error_code || '')}</td>` +
      `<td>${c.neurons_estimated ?? '—'}</td>`;
    tbody.appendChild(tr);
  }
}
document.addEventListener('DOMContentLoaded', loadImggenUsage);
</script>
```

That's the minimum for an honest dashboard. **Use `cf_reconciliation.live_state` as the banner source — never `cf_neurons_today` / `pct_used`** (those are the laggy analytics fields and will tell you "75% remaining" while CF is rejecting every call). Reference full implementation: `galactica/static/js/admin-kumori-journal-stream.js`.

### Do NOT query `kumori_api_usage` directly from your app

The table is in kumori's database. Schemas evolve. Columns get renamed. Indexes get tuned. Your consumer app should ONLY consume kumori's `/api/v1/imggen/usage` endpoint (via `imggen_usage()`). If you find yourself writing a `SELECT * FROM kumori_api_usage` query in a sibling project, **stop**: that's exactly the DRY violation this rewrite eliminated.

If `imggen_usage()` doesn't surface a field you need, **add it server-side in kumori**, not in your consumer's SQL.

---

## 7. Attribution model (NEW 2026-05-11)

Every imggen call lands a row in `kumori_api_usage` with these columns:

| Column | What it's for |
|---|---|
| `platform` | Top-level product the call came from. Derived server-side from your API-key label (e.g. `'galactica'`, `'kindness_social'`). Override with `X-Kumori-Caller` header if you need sub-product attribution (e.g. `galactica.studio` vs `galactica.tools`). |
| `feature` | Sub-operation label you set per call. E.g. `'aria_journal.generate_trace'`, `'avatar_pipeline.first_pass'`, `'admin.test_pixel'`. Required for the per-feature dashboards. |
| `verbiage` | Human-readable description (truncated to 500 chars). Use the prompt itself if you don't have a better label. Lets the admin grep "what was this call doing?" without joining other tables. |
| `tags` | Arbitrary JSONB metadata — anything you want stored alongside the call (`{'pool_size': 30, 'mood': 'observational'}`). |
| `user_id` | End user behind the call (`caller_user_id` in the kwargs). |
| `neurons_estimated` | Local estimate, written by kumori's per-tile formula. CF GraphQL is authoritative; `neurons_cf` gets written by the nightly reconciliation cron. |
| `error_code` | Classified failure mode: `cf_4006_capacity` (CF saturated, fast retry), `cf_5026_timeout`, `daily_cap_exhausted`, `other`. Lets dashboards group failures cleanly. |
| `ok` | bool result. |

### Why this matters

Older consumers (looking at you, hypothetical kindness_social) sometimes did their own × 147 / × 492 neuron math against `kumori_api_usage`. Verified 2026-05-11 that math was wrong: estimate said 2,352 neurons; CF said 2,503; per-call variance 43-438. The constants drift as Cloudflare adjusts. **Don't reimplement neuron math anywhere outside kumori** — call `imggen_usage()` and render its response. Kumori owns the math and the reconciliation cron.

---

## 8. Error handling — what every failed call returns

Every failed `imggen_edit` / `imggen_generate` raises a `KumoriAPIError` with both a clean string message AND a structured `.payload` field. **Read the payload — never invent your own error string.**

```python
from utilities.kumori_api_client import imggen_edit, KumoriAPIError

try:
    res = imggen_edit(
        prompt='...', target_image_b64=b64,
        feature='aria_journal.edit', verbiage='Mars dusk replacement',
    )
except KumoriAPIError as e:
    print(e)              # 'kumori /api/v1/imggen/edit HTTP 429 [daily_cap_exhausted] resets in 18h 45m : AiError: AiError: you have used up your daily free allocation...'
    print(e.status_code)  # 429
    print(e.payload)
    # {
    #   'ok': False,
    #   'error_code': 'daily_cap_exhausted',
    #   'error': '<verbatim CF body text — the literal "you have used up..." string>',
    #   'provider': 'cloudflare_flux2_klein_edit',
    #   'http_status': 429,           # CF's HTTP status (or null if kumori intercepted before calling CF)
    #   'cf_error_code': 4006,        # CF's JSON errorCode
    #   'reset_at_utc': '2026-05-13 00:00:00 UTC',
    #   'reset_in_human': '18h 45m',
    #   'reset_in_seconds': 67500,
    # }
```

### error_code values (kumori's classification)

| error_code | HTTP | Meaning | What the consumer should do |
|---|---|---|---|
| `daily_cap_exhausted` | 429 | CF account-wide 10K-neuron/day pool reached (CF returned 4006 with the daily-allocation message) | Surface `reset_in_human` to the user. **Do not retry today.** The lockout is set until UTC midnight. |
| `cf_4006_capacity` | 429 | CF temporary capacity throttle (4006 without the daily-allocation message — rare; usually means daily cap is genuinely the cause but message is missing) | Wait 15-60s and the next call may succeed. Local backoff already enforces. |
| `cf_5026_timeout` | 502 | CF inference timed out (typically ~120s) | Retry, but probably fewer refs / smaller dimensions. |
| `all_providers_failed` | 503 | Generate-router exhausted every backend (pollinations + flux-1-schnell + stable horde all 4xx/5xx in sequence) | Bigger upstream issue. Show "Image service unavailable, try again in a few minutes." |
| `kumori_internal_error` | 500 | Exception raised inside the kumori route handler. `payload.error` contains `ExceptionType: message`. `payload.traceback_tail` has the last 5 lines. | File a kumori bug. The exception text + traceback come back so the consumer doesn't need to guess. |
| `other` | 502 | Unclassified CF or upstream failure | `payload.error` has the verbatim upstream text. Show it. |

**The literal CF error message is always in `payload.error`** — never replaced with a generic "image edit failed." If you ever see "image edit failed" without context, that's a stale vendored client; redeploy.

### What gets persisted

When the call fails, kumori still writes a row to `kumori_api_usage` with `ok=false`, `error_code=<as above>`, `neurons_estimated=0`, and the verbatim CF message in the row (so `/admin/cloudflare` + the per-platform dashboard can group failures by `error_code`). Failed calls are NOT silent.

---

## 9. The /admin/cloudflare diagnostic page

Hosted at `https://kumori.ai/admin/cloudflare?key=<KUMORI_ADMIN_API_KEY>`. Probes every CF endpoint live and dumps the raw response of each one. Shows the inference-probe state (authoritative — bypasses lagged analytics), per-model neurons today, per-minute history, locked-scope endpoints (403'd — would unlock with `AI Gateway: Read` or `Billing: Read` scopes added to the token), and the non-existent routes (`/ai/usage`, `/ai/quota`, `/ai/neurons`, `/ai/remaining` — all 400, locked-in negative knowledge).

**First thing to check when klein starts 4006-ing in production.** Replaces hours of theorizing with one URL.

You can also drive the same probes from Python: `python -m utilities.cloudflare_utils` (terminal-rendered version of the same data).

---

## 10. Cost reminder

Free in normal use. If you start a batch loop, **set a hard iteration cap** and watch the first run live — kumori shells out to free upstreams but a runaway loop can still exhaust the 10K Cloudflare pool in ~10 minutes and lock out every consumer until midnight UTC. Disclose batch math to the user before running.

---

## 11. Migrating from a vendored in-process copy

A few older consumers (e.g. **kindness_social**) still vendor `kumori_free_imggen.py` + the CF `adapters/cloudflare.py` directly into their own `utilities/`. They call CF from inside their own process instead of going through kumori's HTTP API. That bypasses everything this rewrite gave us:

- **No row in `kumori_api_usage`** — call is invisible to the dashboard
- **No shared backoff state** — kindness's klein call can hit CF while galactica is already locked out (or vice versa), neither sees the other's 4006s
- **No per-app attribution** — even the kumori-side admin can't tell who used what
- **No automatic upgrades** — when kumori updates the klein adapter (e.g. tonight's clean-error refactor), the vendored copy stays stuck on old behavior

### Migration steps (per consumer)

1. **Audit the vendored files.** `find utilities -name "kumori_free_imggen*" -o -name "adapters/cloudflare*"`. List every site that imports them.

2. **Provision an API key for this consumer** in `kumori-404602` Secret Manager (§2 above). Scope: `imggen.edit imggen.generate imggen.read describe.describe llm.chat` as appropriate.

3. **Vendor the client** per §1. Add the two shared_files entries to `deploy.json`.

4. **Init at boot** per §3. One line at startup.

5. **Replace call sites**, one feature at a time. For each `imggen.edit_image(...)` or `imggen.generate_image(...)` call:
   ```python
   # OLD (vendored, in-process — calls CF directly):
   from utilities.kumori_free_imggen import edit_image
   result = edit_image(prompt, image_blobs, width=1024, height=1024)

   # NEW (HTTP via kumori — gets attribution, shared backoff, clean errors):
   from utilities.kumori_api_client import imggen_edit, KumoriAPIError
   import base64
   try:
       res = imggen_edit(
           prompt=prompt,
           target_image_b64=base64.b64encode(image_blobs[0]).decode(),
           reference_images_b64=[base64.b64encode(b).decode() for b in image_blobs[1:4]],
           width=1024, height=1024,
           feature='avatar_pipeline.first_pass',  # YOUR sub-op label
           verbiage=f'avatar for user {user_id}',  # human description
           caller_user_id=str(user_id),
       )
       image_bytes = base64.b64decode(res['image_b64'])
   except KumoriAPIError as e:
       # e.status_code, e.payload['error_code'], e.payload['error'] — see §8
       ...
   ```

6. **Delete the vendored files** once every call site is migrated. `rm utilities/kumori_free_imggen.py utilities/adapters/cloudflare.py`. Remove the imports. Run pre-deploy tests.

7. **Deploy** and verify your calls appear in `/admin/cloudflare` or `/api/v1/imggen/usage` filtered to your new `platform` — they should land in the right `platform` bucket with `feature` + `verbiage` populated.

### Migration status as of 2026-05-12

| Consumer | Status | Notes |
|---|---|---|
| galactica | ✅ migrated | Uses `imggen_edit()` via `utilities/kumori_image.py`. Reference implementation. |
| kindness_social | ⚠️ vendored copies still present | `utilities/kumori_free_imggen.py` + `utilities/adapters/cloudflare.py` exist. Migration pending — separate workstream. |
| heathers_plate / dandy / scatterbrain / crab / ooqio | ✅ no imggen usage | Have the kumori_api_client vendored for LLM but never call imggen. |
| inroads | ⚠️ minor | `utilities/backend_registry.py` references `kumori_free_imggen` symbol — audit when next touched. |

If you're adding a NEW consumer: start at §1 above and never vendor in-process copies in the first place. The HTTP path is the only supported integration mode for new code.

---

## 12. Reference consumer

`galactica/utilities/kumori_image.py` is the most polished consumer — opinionated wrapper around all four entry points with size presets, base64 packing helpers, and a default backend chain ordered by capability. Copy that pattern when wrapping the client for a new app.

The full debug console (every prompt, every response, every byte) lives at `galactica/templates/admin_kumori_journal.html` + `static/js/admin-kumori-journal.js` — reference UI for any consumer that needs to tune prompts before cutting over from a paid provider.
