"""Shared endpoint-shape parser used by:
  - scripts/backfill_endpoints.py (one-shot historical backfill)
  - kindness_social/utilities/llm_catalog_audit.py (live catalog cron)

Given a (route, raw model_id) tuple from a provider catalog, returns the
canonical model_slug + family + status used by the kumori_llm_endpoints
schema. Parser regex live here; bumping the family list means editing
ONE place.

See _infrastructure/kumori_free_llm/docs/ENDPOINT_NORMALIZATION.md.
"""
import re

# Vendor-prefix routes use namespaced ids ("google/gemini-flash-1.5:free");
# direct routes use bare ids ("gemini-2.0-flash"). Strip the namespace AND
# any ":suffix" decoration to get a stable family/model match.
_LEADING_TILDE = re.compile(r'^~')
_CF_NAMESPACE  = re.compile(r'^@cf/[a-z0-9_\-]+/')
_NAMESPACE_PATTERN = re.compile(r'^[a-z0-9_\-]+/')
_VARIANT_SUFFIX = re.compile(r':[a-z0-9_\-]+$')

# Slug normalization
_DATE_STAMP   = re.compile(r'-202[0-9]{5,7}$')
_TRAILING_TAG = re.compile(r'-(exp|latest|instant|preview|alpha)$')

# Family detection by leading token. Order matters: more specific first.
FAMILY_PATTERNS = [
    (re.compile(r'^claude'),                      'anthropic'),
    (re.compile(r'^(gemini|gemma|lyria)'),        'google'),
    (re.compile(r'^(gpt|o[1-5]|chatgpt|davinci|babbage|text-)'), 'openai'),
    (re.compile(r'^grok'),                        'xai'),
    (re.compile(r'^deepseek'),                    'deepseek'),
    (re.compile(r'^(llama|meta-llama|codellama|l3-|l3\.)'), 'meta'),
    (re.compile(r'^(qwen|qwq)'),                  'qwen'),
    (re.compile(r'^(mistral|mixtral|codestral|ministral|mathstral|magistral|nemo|pixtral|devstral|voxtral|open-mistral)'), 'mistral'),
    (re.compile(r'^phi-?[0-9]'),                  'microsoft'),
    (re.compile(r'^(hermes|nous)'),               'nous'),
    (re.compile(r'^(command|aya|c4ai|embed|rerank|cohere)'), 'cohere_ai'),
    (re.compile(r'^jamba'),                       'ai21'),
    (re.compile(r'^falcon'),                      'tii_falcon'),
    (re.compile(r'^granite'),                     'ibm_granite'),
    (re.compile(r'^dbrx'),                        'databricks'),
    (re.compile(r'^yi-'),                         'zeroone_yi'),
    (re.compile(r'^sonar'),                       'perplexity'),
    (re.compile(r'^(lfm|liquid)'),                'liquid'),
    (re.compile(r'^reka'),                        'reka'),
    (re.compile(r'^arctic'),                      'snowflake_arctic'),
    (re.compile(r'^solar'),                       'upstage'),
    (re.compile(r'^nemotron'),                    'nvidia_nemo'),
    (re.compile(r'^btlm'),                        'cerebras_ai'),
    (re.compile(r'^inflection'),                  'inflection'),
    (re.compile(r'^nova-'),                       'amazon_nova'),
    (re.compile(r'^minimax'),                     'minimax'),
    (re.compile(r'^kimi'),                        'moonshot'),
    (re.compile(r'^glm-'),                        'zhipu'),
    (re.compile(r'^ernie'),                       'baidu_ernie'),
    (re.compile(r'^hunyuan'),                     'tencent_hunyuan'),
    (re.compile(r'^tongyi'),                      'alibaba_tongyi'),
    (re.compile(r'^palmyra'),                     'writer_palmyra'),
    (re.compile(r'^step-'),                       'stepfun'),
    (re.compile(r'^olmo'),                        'allen_ai'),
    (re.compile(r'^seed-'),                       'bytedance_seed'),
    (re.compile(r'^(sao10k|gryphe|eva-?unit|nothingiisreal|undi95|alpindale|neversleep|toppy|noromaid|magnum|mythomax|cydonia|dolphin|goliath|wizardlm|dracarys|aion|rocinante|skyfall|trinity|weaver|intellect|cogito|mimo|pareto|morph|ling|labs|relace|mercury|maestro|fuyu|hy3|allam|bodybuilder|auto|router|free|tiny-aya|together|litellm|unslopnemo|remm-slerp)'), 'community'),
]

# Map legacy `kumori_llm_provider_limits.status` -> new endpoint status
STATUS_MAP = {
    'active':                'active',
    'retired':               'retired',
    'probationary':          'probationary',
    'pending_validation':    'discovered',
    'pending_review':        'discovered',
    'retired_failed_smoke':  'retired',
}


def normalize_id(raw: str) -> str:
    """Strip route namespace + variant suffix. Returns lowercased cleaned id."""
    s = raw.strip().lower()
    s = _LEADING_TILDE.sub('', s)
    s = _CF_NAMESPACE.sub('', s)
    s = _NAMESPACE_PATTERN.sub('', s)
    s = _VARIANT_SUFFIX.sub('', s)
    s = s.replace('.', '-').replace('_', '-')
    return s


def to_slug(cleaned: str) -> str:
    """Collapse to a canonical slug. Drops date stamps + churn-y suffixes."""
    s = cleaned
    s = _DATE_STAMP.sub('', s)
    s = _TRAILING_TAG.sub('', s)
    return s


def detect_family(slug: str) -> str:
    for pattern, family in FAMILY_PATTERNS:
        if pattern.match(slug):
            return family
    return 'community'


def parse(route: str, raw_model_id: str) -> dict:
    """Top-level: derive (model_slug, family, display_name) from route + raw id.

    Returns dict with keys: model_slug, family, display_name.
    """
    cleaned = normalize_id(raw_model_id)
    slug = to_slug(cleaned)
    family = detect_family(slug)

    # If detection failed, the model_id may have a redundant route prefix.
    # Try again with route prefix stripped — but ONLY when initial detection
    # failed, so 'mistral-large-latest' on route='mistral' stays 'mistral'.
    if family == 'community' and cleaned.startswith(route + '-'):
        alt_cleaned = cleaned[len(route) + 1:]
        alt_slug = to_slug(alt_cleaned)
        alt_family = detect_family(alt_slug)
        if alt_family != 'community':
            slug, family = alt_slug, alt_family

    display_name = slug.replace('-', ' ').title()
    return {'model_slug': slug, 'family': family, 'display_name': display_name}


def map_status(legacy: str) -> str:
    """Map a kumori_llm_provider_limits.status value to the new endpoint status."""
    return STATUS_MAP.get(legacy or 'active', 'discovered')
