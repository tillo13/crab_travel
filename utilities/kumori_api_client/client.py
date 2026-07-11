"""Thin HTTP client for kumori.ai/api/v1/* — replaces in-process vendoring of
kumori_free_llm + kumori_free_image_generations + kumori_free_image_describe
in sibling apps (kindness_social, pilgrims_world, crab_travel, scatterbrain).

Vendored INTO each consumer's utilities/ via deploy.json shared_files. Mirrors
the proven pattern from heathers_plate/utilities/kumori_api.py.

Auth: per-consumer API key in Secret Manager. Key name resolved by env var
KUMORI_API_KEY_NAME (e.g. "KINDNESS_KUMORI_API_KEY") — set in app.yaml. The
client looks for an injected get_secret function via init(), or falls back to
KUMORI_API_KEY env var directly.

Usage in a consumer:
    from utilities.kumori_api_client import init, llm_generate, imggen_generate
    init(get_secret_fn=get_secret, api_key_name='KINDNESS_KUMORI_API_KEY')

    text, backend = llm_generate('Tell me a joke')
    img = imggen_generate('a sunset over the ocean')
"""
import logging
import os

import requests

logger = logging.getLogger(__name__)

KUMORI_BASE = os.environ.get('KUMORI_API_BASE', 'https://kumori.ai')

_get_secret_fn = None
_api_key_name = None
_api_key_cache = None

# Optional client-side pacing — set via init(min_inter_call_sec=N) or per-call
# kwarg. Useful for batch consumers (anchor_bulk_fill, aria_daily_cron) that
# fire many calls in quick succession against a shared free-tier pool and
# want to avoid tripping the upstream's burst gate. Module-level last-call
# timestamp; not thread-safe (intended for the dominant single-threaded
# pacing case).
_min_inter_call_sec = 0.0
_last_call_at = 0.0
_pacing_lock = None  # lazy-init threading.Lock when needed


class KumoriAPIError(Exception):
    """Raised when kumori.ai/api/v1/* call fails after retry."""

    def __init__(self, message, status_code=None, payload=None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


def init(get_secret_fn=None, api_key_name=None, min_inter_call_sec=0.0):
    """Inject Secret Manager fetcher + which secret holds this app's API key.

    api_key_name: e.g. 'KINDNESS_KUMORI_API_KEY' (per-consumer secret in
    kumori-404602 Secret Manager). If None, falls back to KUMORI_API_KEY
    env var.

    min_inter_call_sec: floor on time between requests. The client sleeps
    `max(0, min_inter_call_sec - (now - last_call))` before each request.
    Set when a batch consumer is hammering a shared free-tier pool and
    needs to space calls to avoid tripping the upstream burst gate
    (e.g. dos_bros anchor_bulk_fill saw 6/13 503s with no pacing).
    Default 0 = no pacing; recommended ~6s for CF Klein bursts.
    """
    global _get_secret_fn, _api_key_name, _api_key_cache, _min_inter_call_sec
    _get_secret_fn = get_secret_fn
    _api_key_name = api_key_name
    _api_key_cache = None  # invalidate cache
    _min_inter_call_sec = float(min_inter_call_sec or 0.0)


def _pace_before_request():
    """Sleep just enough to respect the configured min_inter_call_sec floor.
    No-op when the floor is 0. Lazy-inits the lock on first use so consumers
    that never enable pacing don't pay the lock-init cost."""
    global _last_call_at, _pacing_lock
    if _min_inter_call_sec <= 0:
        return
    import time as _t
    import threading as _th
    if _pacing_lock is None:
        _pacing_lock = _th.Lock()
    with _pacing_lock:
        now = _t.monotonic()
        wait = _min_inter_call_sec - (now - _last_call_at)
        if wait > 0:
            _t.sleep(wait)
            now = _t.monotonic()
        _last_call_at = now


def _api_key():
    """Resolve the API key once and cache. Order:
      1. KUMORI_API_KEY env var (overrides everything — useful for local dev)
      2. _get_secret_fn(_api_key_name) if both are set
    """
    global _api_key_cache
    if _api_key_cache:
        return _api_key_cache
    val = os.environ.get('KUMORI_API_KEY')
    if not val and _get_secret_fn and _api_key_name:
        try:
            val = _get_secret_fn(_api_key_name)
        except Exception as e:
            logger.warning(f"kumori_api_client: get_secret({_api_key_name}) failed: {e}")
    _api_key_cache = val
    return val


# Optional instrumentation hook — when set to a list, every _request() call
# appends a record with the full request body, response body, status, and
# latency. Used by the galactica admin debug console to surface every byte
# hitting kumori. Module-level (process-wide) — not thread-safe; intended for
# single-user admin testing, not production multi-tenant traffic.
_request_log = None


def set_request_log(target_list):
    """Begin recording every _request() call to `target_list` (or pass None to
    stop). Base64 image fields in request/response bodies are truncated to
    `<base64:N chars>` placeholders so the log stays human-readable."""
    global _request_log
    _request_log = target_list


def _redact_b64(obj):
    """Walk a dict and replace any *_b64 / image_b64 string fields with size
    markers. Keeps the log readable when payloads contain ~500KB+ base64."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k.endswith('_b64') and isinstance(v, str):
                out[k] = f"<base64:{len(v)} chars>"
            elif k == 'reference_images_b64' and isinstance(v, list):
                out[k] = [f"<base64:{len(b)} chars>" if isinstance(b, str) else b for b in v]
            elif isinstance(v, (dict, list)):
                out[k] = _redact_b64(v)
            else:
                out[k] = v
        return out
    if isinstance(obj, list):
        return [_redact_b64(x) for x in obj]
    return obj


def _backoff_sleep(response):
    """Jittered backoff before the single retry. Instant, synchronized retries
    across many consumer apps thundering-herd an already-overloaded gateway —
    the 502/503 amplification in the cross-project digest. Jitter de-syncs the
    herd; a numeric Retry-After (when the server sends one) is honored, capped
    at 5s so a caller never blocks too long. Returns seconds to sleep."""
    import random
    delay = 0.5 + random.random() * 0.75  # 0.5–1.25s, de-synchronized
    if response is not None:
        ra = response.headers.get('Retry-After')
        if ra:
            try:
                delay = min(float(ra), 5.0)
            except ValueError:
                pass
    return delay


def _request(method, path, body=None, timeout=(5, 60), retry_on_5xx=True):
    """Generic kumori API call. Returns parsed JSON dict on success, raises
    KumoriAPIError on failure.

    timeout: (connect, read) tuple. Default 5s connect / 60s read.
    retry_on_5xx: one retry on 5xx, ConnectionError, Timeout.
    """
    key = _api_key()
    if not key:
        raise KumoriAPIError(
            'kumori_api_client not initialized — call init(get_secret_fn=..., '
            'api_key_name=...) or set KUMORI_API_KEY env var'
        )
    url = f'{KUMORI_BASE}{path}'
    headers = {'X-API-Key': key, 'Content-Type': 'application/json'}

    import time as _time
    last_exc = None
    for attempt in (1, 2):
        # Client-side pacing on attempt 1 only — retries from the same call
        # don't re-pace (the upstream already saw the first attempt land at
        # the spacing boundary). Configured via init(min_inter_call_sec=N).
        if attempt == 1:
            _pace_before_request()
        t0 = _time.time()
        try:
            r = requests.request(method, url, json=body, headers=headers, timeout=timeout)
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            if _request_log is not None:
                _request_log.append({
                    'method': method.upper(), 'url': url, 'attempt': attempt,
                    'request_body': _redact_b64(body) if isinstance(body, (dict, list)) else body,
                    'error': f"{type(e).__name__}: {str(e)[:200]}",
                    'ms': int((_time.time() - t0) * 1000),
                    'timestamp': _time.time(),
                })
            if attempt == 1 and retry_on_5xx:
                logger.warning(f"kumori {path} {type(e).__name__}, retrying")
                _time.sleep(_backoff_sleep(None))
                continue
            raise KumoriAPIError(f'Network error reaching kumori: {e}')
        ms = int((_time.time() - t0) * 1000)
        # Got a response — parse JSON
        try:
            data = r.json()
        except ValueError:
            data = {'raw': r.text[:300]}
        # Record this call for the admin debug console if instrumentation active
        if _request_log is not None:
            _request_log.append({
                'method': method.upper(),
                'url': url,
                'attempt': attempt,
                'request_body': _redact_b64(body) if isinstance(body, (dict, list)) else body,
                'response_status': r.status_code,
                'response_headers': dict(r.headers),
                'response_body': _redact_b64(data) if isinstance(data, (dict, list)) else data,
                'response_size_bytes': len(r.content) if r.content is not None else 0,
                'ms': ms,
                'timestamp': _time.time(),
            })
        if r.status_code == 200:
            return data
        if 500 <= r.status_code < 600 and attempt == 1 and retry_on_5xx:
            logger.warning(f"kumori {path} HTTP {r.status_code}, retrying")
            _time.sleep(_backoff_sleep(r))
            continue
        # Build a CLEAN error string from whatever structured fields the
        # server returned. Imggen failures now include error_code (kumori
        # classification: daily_cap_exhausted | cf_4006_capacity |
        # cf_5026_timeout | other), the verbatim upstream message, the CF
        # numeric error code, and (for daily_cap_exhausted) the UTC-midnight
        # reset time. Prefer these structured fields over a generic
        # "image edit failed" — consumers should never have to guess.
        if isinstance(data, dict):
            parts = [f'kumori {path} HTTP {r.status_code}']
            ec = data.get('error_code')
            if ec:
                parts.append(f'[{ec}]')
            cfc = data.get('cf_error_code')
            if cfc:
                parts.append(f'cf_code={cfc}')
            reset = data.get('reset_in_human')
            if reset:
                parts.append(f'resets in {reset}')
            verbatim = data.get('error') or data.get('detail') or 'unknown'
            parts.append(f': {verbatim}')
            msg = ' '.join(parts)
        else:
            msg = f'kumori {path} HTTP {r.status_code}: {str(data)[:200]}'
        raise KumoriAPIError(msg, status_code=r.status_code, payload=data)
    # Should not reach
    raise KumoriAPIError(f'kumori {path} failed after retry: {last_exc}')


# ─── LLM ──────────────────────────────────────────────────────────────────────

def llm_generate(prompt, max_tokens=500, temperature=1.0):
    """Auto-routes to a free backend. Returns (text, backend_name)."""
    data = _request('POST', '/api/v1/llm/generate',
                    {'prompt': prompt, 'max_tokens': max_tokens, 'temperature': temperature})
    return data.get('text'), data.get('backend')


def llm_chat(backend_name, messages, max_tokens=500, temperature=0.3, system=None,
             app_name=None, timeout=None):
    """Pinned-backend multi-turn chat. Returns (text, backend_name).

    app_name: optional consumer attribution (e.g. 'dos_bros', 'galactica').
    When set, lands in kumori_api_usage.app_name for this call's detail row.
    timeout: optional (connect, read) tuple for callers who tolerate slow
    lanes (e.g. probation-ward validation traffic); default (5, 60).
    """
    body = {'backend': backend_name, 'messages': messages,
            'max_tokens': max_tokens, 'temperature': temperature}
    if system:
        body['system'] = system
    if app_name:
        body['app_name'] = app_name
    data = _request('POST', '/api/v1/llm/chat', body, timeout=timeout or (5, 60))
    return data.get('text'), data.get('backend')


def llm_chat_resilient(backends=None, messages=None, max_tokens=500, temperature=0.3,
                       system=None, min_chars=1, debug=False, app_name=None,
                       min_quality_tier=None, require_capabilities=None,
                       budget_ms=None, allow_degrade=None):
    """Server-side fallback chat. Returns (text, winning_backend,
    attempt_log_list, debug_info). Pick models ONE of two ways:

    INTENT MODE (recommended) — ask for a capability tier, kumori resolves the
    best available free lane; you never name a provider:
        min_quality_tier: 'frontier' | 'high' | 'medium' | 'low'
            (storefront: Frontier / Pro / Standard / Fast — capability classes)
        require_capabilities: optional list, e.g. ['reasoning'] (thinking models)
        budget_ms: hard wall-clock cap for the whole cascade (never hangs past it)
        allow_degrade: True → drop below the tier if it's exhausted rather than fail

    EXPLICIT MODE — pin the exact fallback order yourself:
        backends: list of backend names, tried in order.

    Provide EITHER min_quality_tier/require_capabilities OR backends (server 400s
    if neither). min_chars: response-shape gate (shorter → rotate). app_name:
    consumer attribution → kumori_api_usage.app_name.
    """
    body = {'messages': messages, 'max_tokens': max_tokens,
            'temperature': temperature, 'min_chars': min_chars}
    if backends:
        body['backends'] = list(backends)
    if min_quality_tier:
        body['min_quality_tier'] = min_quality_tier
    if require_capabilities:
        body['require_capabilities'] = list(require_capabilities)
    if budget_ms is not None:
        body['budget_ms'] = int(budget_ms)
    if allow_degrade is not None:
        body['allow_degrade'] = bool(allow_degrade)
    if system:
        body['system'] = system
    if debug:
        body['debug'] = True
    if app_name:
        body['app_name'] = app_name
    data = _request('POST', '/api/v1/llm/chat-resilient', body, timeout=(5, 120))
    return data.get('text'), data.get('backend'), data.get('attempts', []), data.get('_debug')


def llm_chat_eval(prompt, system=None, caller=None):
    """Eval-pool scoring call. Signature mirrors kumori_free_llms.chat_eval.
    Returns (text, backend_name)."""
    body = {'prompt': prompt}
    if system:
        body['system'] = system
    data = _request('POST', '/api/v1/llm/chat-eval', body)
    return data.get('text'), data.get('backend')


def llm_backends(modality=None):
    """List available backends. Returns the list of backend dicts.

    modality: None (default) = the runtime chat catalog, unchanged.
    'image-gen' | 'image-edit' | 'embedding' | ... | 'all' = the
    kumori_llm_endpoints lifecycle feed across modalities (the probation-ward
    view: every enabled lane with lifecycle_status + status_since)."""
    path = '/api/v1/llm/backends'
    if modality:
        path += f'?modality={modality}'
    data = _request('GET', path)
    return data.get('backends', [])


def llm_usage():
    """Cluster-wide usage summary across backends."""
    data = _request('GET', '/api/v1/llm/usage')
    return data.get('usage', {})


def llm_registry():
    """Full backend_registry snapshot. Returns dict with keys: backends, models,
    fallback_order, cloud_run_only, litellm_backends, cloud_run_worker_url,
    available_backends, backend_naming, free_model_count, models_source."""
    data = _request('GET', '/api/v1/llm/registry')
    # Strip 'ok' key, return the rest
    return {k: v for k, v in data.items() if k != 'ok'}


def llm_backoff_state():
    """Current per-backend backoff state. Returns
    {backend_name: {until_ts, remaining_sec, backed_off}}."""
    data = _request('GET', '/api/v1/llm/backoff-state')
    return data.get('backoff_state', {})


def llm_is_backed_off(backend_name):
    """Convenience: True if backend is currently in backoff."""
    state = llm_backoff_state()
    return state.get(backend_name, {}).get('backed_off', False)


def llm_backoff_until():
    """Legacy-compat: returns {backend_name: until_timestamp} — same shape as
    the old kumori_free_llms._backoff_until dict that some callers iterate."""
    state = llm_backoff_state()
    return {name: data['until_ts'] for name, data in state.items()}


# ─── Embed / Rerank / Transcribe ──────────────────────────────────────────────

def embed_text(texts, input_type='search_document'):
    """Embed a list of strings via the shared free-LLM pool. Auto-selects
    a backend (Cohere v3+v4, Mistral, NVIDIA, etc.). Returns
    (vectors, backend_name). `input_type` only matters for Cohere
    (search_document/search_query/classification/clustering)."""
    if isinstance(texts, str):
        texts = [texts]
    data = _request('POST', '/api/v1/llm/embed-text',
                    {'texts': list(texts), 'input_type': input_type})
    return data.get('vectors'), data.get('backend')


def embed_image(images):
    """Embed images via Cohere v3-image variants. Accepts a list of base64
    strings or data: URIs. Returns (vectors, backend_name)."""
    if isinstance(images, (str, bytes)):
        images = [images]
    if images and isinstance(images[0], bytes):
        import base64
        images = [base64.b64encode(i).decode() for i in images]
    data = _request('POST', '/api/v1/llm/embed-image', {'images': list(images)})
    return data.get('vectors'), data.get('backend')


def rerank(query, documents, top_n=None):
    """Rerank documents by relevance to query (Cohere). Returns
    (results, backend_name) where results is a list of
    {'index': <int>, 'relevance_score': <float>} sorted descending."""
    body = {'query': query, 'documents': list(documents)}
    if top_n is not None:
        body['top_n'] = int(top_n)
    data = _request('POST', '/api/v1/llm/rerank', body)
    return data.get('results'), data.get('backend')


def transcribe(audio_bytes, language='en', content_type='audio/wav'):
    """Transcribe audio bytes (≤25MB) to text via the free pool (Cohere).
    Returns (text, backend_name)."""
    import base64
    audio_b64 = base64.b64encode(audio_bytes).decode()
    data = _request('POST', '/api/v1/llm/transcribe',
                    {'audio_b64': audio_b64, 'language': language,
                     'content_type': content_type},
                    timeout=(5, 120))
    return data.get('text'), data.get('backend')


def quality_catalog(days=7):
    """Read the dual-judged free-LLM quality catalog. Scoped via
    'catalog.read' on the calling kmr_live_* key (NOT admin-gated —
    principle of least privilege per industry consensus).

    Returns the raw response dict:
        {window_days, judge_kind, backends: [{backend, modality,
                                              quality_when_works, error_rate,
                                              n_ok, n_total}, ...]}

    Pre-release: response includes a 'note' field flagging it's not yet
    public. Will eventually drop auth + the gate at Phase 8.
    """
    return _request('GET', f'/catalog/quality.json?days={int(days)}', None)


def emit_quality_sample(backend, score, ok=True, judge_kind='kindness_live_v1',
                        response_excerpt=None, duration_ms=None,
                        judge_notes=None, error=None, modality='chat',
                        probe_id=None):
    """Emit a real-world quality sample to the catalog. Scoped via
    'quality.write'. Fire-and-forget: failures are logged and swallowed —
    never raises into the caller, never blocks the reply path.

    Caller must have already produced `score` (0-100). For failed LLM calls
    pass ok=False, score=0, and an error string. judge_kind must be one of
    the kumori allowlist ('kindness_live_v1', 'kindness_peer_v1' at time of
    writing); the server rejects others with 400.

    Short timeout (3s connect / 8s read) because this is telemetry, not a
    critical path.
    """
    body = {'backend': backend, 'modality': modality, 'judge_kind': judge_kind,
            'score': int(score), 'ok': bool(ok)}
    if response_excerpt: body['response_excerpt'] = response_excerpt[:2000]
    if duration_ms is not None: body['duration_ms'] = int(duration_ms)
    if judge_notes: body['judge_notes'] = judge_notes
    if error: body['error'] = error
    if probe_id: body['probe_id'] = probe_id
    try:
        _request('POST', '/api/v1/llm/quality-sample', body,
                 timeout=(3, 8), retry_on_5xx=False)
    except Exception as e:
        logger.warning(f"emit_quality_sample failed for {backend}/{judge_kind}: {e}")


# ─── Image generation ─────────────────────────────────────────────────────────

def imggen_generate(prompt, width=1024, height=1024, mode='roundrobin',
                    feature=None, verbiage=None, caller_user_id=None, tags=None):
    """Text→image via free providers. Klein-4B size rules apply (multiples of
    16; max 4 MP — see kumori_free_image_generations/SIZES.md). Returns
    {ok, image_b64, provider, mode, ms, bytes}.

    Attribution kwargs (post 2026-05-11 — strongly encouraged for every call):
        feature: sub-operation like 'aria_journal.generate' or
                 'admin.test_pixel'. Surfaces in /admin/api-costs dashboards
                 and kumori_api_usage.feature.
        verbiage: human-readable description (the prompt is fine if you
                  don't have a separate label). Stored in
                  kumori_api_usage.verbiage truncated to 500 chars.
        caller_user_id: end-user behind the call.
        tags: arbitrary dict; stored as kumori_api_usage.tags JSONB."""
    body = {'prompt': prompt, 'width': width, 'height': height, 'mode': mode}
    if feature: body['feature'] = feature
    if verbiage: body['verbiage'] = verbiage
    if caller_user_id: body['caller_user_id'] = caller_user_id
    if tags: body['tags'] = tags
    data = _request('POST', '/api/v1/imggen/generate', body, timeout=(5, 90))
    return data


def imggen_edit(prompt, target_image_b64, reference_images_b64=None,
                width=1024, height=1024, app_name=None, character=None,
                ref_filename=None, debug=False,
                feature=None, verbiage=None, caller_user_id=None, tags=None,
                provider=None):
    """Image+text → image edit. Default routes through Cloudflare flux-2-klein-4b;
    pass `provider=` to target a specific edit endpoint:
      - 'cloudflare_flux2_klein_edit' (default, 4 refs max, 60/day combined w/ _2)
      - 'cloudflare_flux2_klein_edit_2' (sibling CF account)
      - 'huggingface_qwen_2511' (20B, single ref, 4/day) — added 2026-05-13
      - 'huggingface_kontext_dev' (12B, single ref, 4/day) — added 2026-05-13
      - 'huggingface_qwen_2511_fast' (20B distilled, single ref, 30/day) — added 2026-05-13

    Up to 3 reference images allowed for CF (target + 3 refs = 4 inputs total);
    HF Spaces typically use only the target image. Size rules per endpoint_specs.json.

    Returns {ok, image_b64, provider, ms, [_debug]}.

    When debug=True, response includes `_debug.upstream_calls` listing every
    HTTP call kumori made to the upstream provider (with payloads + response details).

    Attribution kwargs (post 2026-05-11 — strongly encouraged for every call,
    see imggen_generate doc for what each one does)."""
    if not target_image_b64:
        raise ValueError('imggen_edit requires target_image_b64')
    refs = reference_images_b64 or []
    if len(refs) > 3:
        raise ValueError(f'imggen_edit accepts at most 3 reference images (got {len(refs)})')
    body = {'prompt': prompt, 'target_image_b64': target_image_b64,
            'reference_images_b64': refs, 'width': width, 'height': height}
    if app_name: body['app_name'] = app_name
    if character: body['character'] = character
    if ref_filename: body['ref_filename'] = ref_filename
    if debug: body['debug'] = True
    if feature: body['feature'] = feature
    if verbiage: body['verbiage'] = verbiage
    if caller_user_id: body['caller_user_id'] = caller_user_id
    if tags: body['tags'] = tags
    if provider: body['provider'] = provider
    data = _request('POST', '/api/v1/imggen/edit', body, timeout=(5, 300))
    return data


def imggen_usage(date=None, platform=None, limit=50):
    """Per-platform imggen usage view (primary source: kumori_api_usage with
    rich attribution; secondary: CF GraphQL reconciliation block).

    Replaces consumer-side neuron math (× 147 / × 492 constants) — kumori is
    the single source of truth. Galactica's /admin/kumori-journal etc render
    this directly instead of querying kumori_api_usage themselves.

    Returns the same JSON shape as /api/v1/imggen/usage:
      {ok, date, totals, per_platform, per_model, recent_calls,
       cf_reconciliation}

    Args:
        date: 'YYYY-MM-DD' UTC date (default: today UTC).
        platform: filter to one platform (default: all).
        limit: number of recent calls to include (default 50, max 200)."""
    qs = []
    if date: qs.append(f'date={date}')
    if platform: qs.append(f'platform={platform}')
    if limit and limit != 50: qs.append(f'limit={limit}')
    path = '/api/v1/imggen/usage' + (('?' + '&'.join(qs)) if qs else '')
    return _request('GET', path)


# ─── Image describe ───────────────────────────────────────────────────────────

def describe_image(image_url=None, image_b64=None, prompt=None, mime=None, skip=None,
                   app_name=None, n=None, min_n=None):
    """Describe an image via free vision LLMs. Pass either image_url OR
    image_b64. Returns {text, backend, ms, attempts} for n<=1; for n>1 the
    response carries voters: [{voter_idx, backend, text, ms}, ...] (multi-voter
    rotation — distinct vision lanes look at the same image).

    app_name: optional consumer attribution (e.g. 'dos_bros', 'galactica').
    When set, lands in kumori_api_usage.app_name for this call's detail row.
    Without it, server defaults to 'kumori_free_stack'.
    """
    if not image_url and not image_b64:
        raise ValueError('describe_image requires image_url or image_b64')
    body = {}
    if image_url:
        body['image_url'] = image_url
    else:
        body['image_b64'] = image_b64
    if prompt:
        body['prompt'] = prompt
    if mime:
        body['mime'] = mime
    if skip:
        body['skip'] = skip
    if app_name:
        body['app_name'] = app_name
    if n:
        body['n'] = int(n)
    if min_n:
        body['min_n'] = int(min_n)
    data = _request('POST', '/api/v1/describe/describe', body,
                    timeout=(5, 60 if not n or n <= 1 else 120))
    return data
