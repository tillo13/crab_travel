"""
backend_registry.py — Single source of truth for all kumori LLM backends.

Pure data + derivation. No I/O, no state, no side effects.
Adding a new backend = adding one row to MODELS.

⚠️  SOURCE LIVES IN _infrastructure/kumori_free_llm/.
    After editing, run _infrastructure/kumori_free_llm/sync_downstream.sh
    to propagate to crab_travel, kindness_social, scatterbrain.

Used by:
  - kumori_free_llms.py (router) — BACKENDS, FALLBACK_LIMITS, EVAL_POOL
  - kindness_social model_registry.py — display names, model IDs
  - kindness_social agent_factory.py — available backends, naming
"""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Provider definitions — shared config per API provider
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PROVIDERS = {
    'groq': {
        'url': 'https://api.groq.com/openai/v1/chat/completions',
        'secret': 'KINDNESS_GROQ_API_KEY',
        'limits': {'daily_limit': 1000, 'rpm_spacing_sec': 4.0, 'backoff_sec': 120},
        'display': 'Groq',
    },
    'cerebras': {
        'url': 'https://api.cerebras.ai/v1/chat/completions',
        'secret': 'KINDNESS_CEREBRAS_API_KEY',
        'limits': {'daily_limit': 500, 'rpm_spacing_sec': 3.0, 'backoff_sec': 120},
        'display': 'Cerebras',
    },
    'gemini': {
        'url': None,  # uses SDK, not URL
        'secret': 'KINDNESS_GEMINI_API_KEY',
        'limits': {'daily_limit': 500, 'rpm_spacing_sec': 5.0, 'backoff_sec': 300},
        'display': 'Google',
        'type': 'gemini',
    },
    'openrouter': {
        'url': 'https://openrouter.ai/api/v1/chat/completions',
        'secret': 'KINDNESS_OPENROUTER_API_KEY',
        'limits': {'daily_limit': 50, 'rpm_spacing_sec': 10.0, 'backoff_sec': 120},
        'display': 'OpenRouter',
    },
    'cloudflare': {
        'url': 'https://api.cloudflare.com/client/v4/accounts/80e562b6b3515f2fb5e11fc0475a8578/ai/run/',
        'secret': 'KINDNESS_CLOUDFLARE_API_KEY',
        'limits': {'daily_limit': 500, 'rpm_spacing_sec': 0.2, 'backoff_sec': 120},
        'display': 'Cloudflare',
        'type': 'cloudflare',
    },
    'cohere': {
        'url': 'https://api.cohere.com/v2/chat',
        'secret': 'KINDNESS_COHERE_API_KEY',
        'limits': {'daily_limit': 30, 'rpm_spacing_sec': 3.0, 'backoff_sec': 120},
        'display': 'Cohere',
        'type': 'cohere',
    },
    'github': {
        'url': 'https://models.github.ai/inference/chat/completions',
        'secret': 'SCATTERBRAIN_GITHUB_TOKEN',
        'limits': {'daily_limit': 50, 'rpm_spacing_sec': 5.0, 'backoff_sec': 120},
        'display': 'GitHub Models',
    },
    'nvidia': {
        'url': 'https://integrate.api.nvidia.com/v1/chat/completions',
        'secret': 'KINDNESS_NVIDIA_API_KEY',
        'limits': {'daily_limit': 50, 'rpm_spacing_sec': 5.0, 'backoff_sec': 120, 'lifetime_limit': 5000, 'conservation': True},
        'display': 'NVIDIA',
    },
    'mistral': {
        'url': 'https://api.mistral.ai/v1/chat/completions',
        'secret': 'KINDNESS_MISTRAL_API_KEY',
        'limits': {'daily_limit': 100, 'rpm_spacing_sec': 60.0, 'backoff_sec': 120},
        'display': 'Mistral',
    },
    'llm7': {
        'url': 'https://api.llm7.io/v1/chat/completions',
        'secret': None,
        'limits': {'daily_limit': 300, 'rpm_spacing_sec': 4.0, 'backoff_sec': 120},
        'display': 'LLM7',
    },
    'sambanova': {
        'url': 'https://api.sambanova.ai/v1/chat/completions',
        'secret': 'KINDNESS_SAMBANOVA_API_KEY',
        'limits': {'daily_limit': 200, 'rpm_spacing_sec': 3.0, 'backoff_sec': 120},
        'display': 'SambaNova',
    },
    'worker': {
        'url': 'https://kindness-worker-243380010344.us-central1.run.app/chat',
        'secret': None,
        'limits': {'daily_limit': 100, 'rpm_spacing_sec': 10.0, 'backoff_sec': 120},
        'display': 'Worker',
        'type': 'worker',
    },
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Model definitions — one row per backend
#
# Fields:
#   name          — unique backend key (used everywhere)
#   provider      — key into PROVIDERS dict
#   model_id      — model string sent to the API
#   display       — human-readable name for agent profiles
#   gateway_model — litellm gateway model name (for fallback), or None
#   naming        — (provider_slug, model_short) for agent naming
#   overrides     — dict of per-model limit overrides (optional)
#   eval          — True if eligible for eval pool (default True for free)
#   assign        — True if eligible for agent assignment (default False)
#   cf_path       — Cloudflare model path suffix (cloudflare only)
#   worker_type   — worker backend name for kindness-worker (worker only)
#   gemini_model  — model name for Gemini SDK (gemini only)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_FALLBACK_MODELS = [
    # ── Groq (fast, generous free tier — all share one API key / 30 RPM pool) ──
    {'name': 'groq',              'provider': 'groq', 'model_id': 'llama-3.3-70b-versatile',                    'display': 'Llama 3.3 70B (Groq)',       'gateway_model': 'groq-llama-70b',       'naming': ('groq', 'llama70b'),       'assign': True},
    {'name': 'groq-kimi',         'provider': 'groq', 'model_id': 'moonshotai/kimi-k2-instruct',               'display': 'Kimi K2 (Groq)',             'gateway_model': 'groq-kimi',            'naming': ('groq', 'kimi-k2'),        'assign': True},
    {'name': 'groq-qwen',         'provider': 'groq', 'model_id': 'qwen/qwen3-32b',                            'display': 'Qwen3 32B (Groq)',           'gateway_model': 'groq-qwen',            'naming': ('groq', 'qwen3-32b'),      'assign': True},
    {'name': 'groq-gptoss',       'provider': 'groq', 'model_id': 'openai/gpt-oss-120b',                       'display': 'GPT-OSS 120B (Groq)',        'gateway_model': 'groq-gptoss',          'naming': ('groq', 'gptoss-120b'),    'assign': True, 'overrides': {'daily_limit': 200}},
    {'name': 'groq-llama4-scout', 'provider': 'groq', 'model_id': 'meta-llama/llama-4-scout-17b-16e-instruct',  'display': 'Llama 4 Scout (Groq)',       'gateway_model': 'groq-llama4-scout',    'naming': ('groq', 'llama4-scout'),   'assign': True},
    {'name': 'groq-gptoss-20b',   'provider': 'groq', 'model_id': 'openai/gpt-oss-20b',                        'display': 'GPT-OSS 20B (Groq)',         'gateway_model': 'groq-gptoss-20b',      'naming': ('groq', 'gptoss-20b'),     'assign': True},
    {'name': 'groq-llama-8b',     'provider': 'groq', 'model_id': 'llama-3.1-8b-instant',                      'display': 'Llama 3.1 8B (Groq)',        'gateway_model': 'groq-llama-8b',        'naming': ('groq', 'llama8b'),        'assign': True},
    {'name': 'groq-allam',        'provider': 'groq', 'model_id': 'allam-2-7b',                                'display': 'Allam 2 7B (Groq)',          'gateway_model': 'groq-allam',           'naming': ('groq', 'allam-7b')},

    # ── Cerebras ──
    {'name': 'cerebras',            'provider': 'cerebras', 'model_id': 'llama3.1-8b',                          'display': 'Llama 3.1 8B (Cerebras)',    'gateway_model': 'cerebras-llama',       'naming': ('cerebras', 'llama8b'),     'assign': True, 'overrides': {'daily_limit': 10, 'conservation': True}},
    {'name': 'cerebras-qwen3-235b', 'provider': 'cerebras', 'model_id': 'qwen-3-235b-a22b-instruct-2507',      'display': 'Qwen3 235B (Cerebras)',      'gateway_model': 'cerebras-qwen3-235b',  'naming': ('cerebras', 'qwen3-235b'), 'assign': True},

    # ── Gemini (SDK-based, not URL) ──
    {'name': 'gemini',      'provider': 'gemini', 'model_id': 'gemini-2.5-flash',   'display': 'Gemini 2.5 Flash',     'gateway_model': 'gemini-flash',      'naming': ('google', 'flash-2.5'),    'gemini_model': 'gemini-2.5-flash',     'overrides': {'daily_limit': 20, 'rpm_spacing_sec': 10.0}},
    {'name': 'gemini-lite', 'provider': 'gemini', 'model_id': 'gemini-2.5-flash-lite', 'display': 'Gemini 2.5 Flash Lite', 'gateway_model': None,           'naming': ('google', 'flash-lite'),   'gemini_model': 'gemini-2.5-flash-lite', 'assign': True},
    {'name': 'gemma',       'provider': 'gemini', 'model_id': 'gemma-3-4b-it',      'display': 'Gemma 3 4B',           'gateway_model': None,                'naming': ('google', 'gemma3-4b'),    'gemini_model': 'gemma-3-4b-it',        'assign': True},
    {'name': 'gemma-27b',   'provider': 'gemini', 'model_id': 'gemma-3-27b-it',     'display': 'Gemma 3 27B',          'gateway_model': 'gemini-gemma-27b',  'naming': ('google', 'gemma3-27b'),   'gemini_model': 'gemma-3-27b-it',       'assign': True},
    {'name': 'gemma-4-31b', 'provider': 'gemini', 'model_id': 'gemma-4-31b-it',     'display': 'Gemma 4 31B',          'gateway_model': None,                'naming': ('google', 'gemma4-31b'),   'gemini_model': 'gemma-4-31b-it',       'assign': True},
    {'name': 'gemma-4-26b', 'provider': 'gemini', 'model_id': 'gemma-4-26b-a4b-it', 'display': 'Gemma 4 26B',          'gateway_model': None,                'naming': ('google', 'gemma4-26b'),   'gemini_model': 'gemma-4-26b-a4b-it'},
    {'name': 'gemma-3n',    'provider': 'gemini', 'model_id': 'gemma-3n-e4b-it',    'display': 'Gemma 3n E4B',         'gateway_model': None,                'naming': ('google', 'gemma3n-e4b'),  'gemini_model': 'gemma-3n-e4b-it'},
    {'name': 'gemma-12b',   'provider': 'gemini', 'model_id': 'gemma-3-12b-it',     'display': 'Gemma 3 12B',          'gateway_model': None,                'naming': ('google', 'gemma3-12b'),   'gemini_model': 'gemma-3-12b-it'},
    {'name': 'gemma-1b',    'provider': 'gemini', 'model_id': 'gemma-3-1b-it',      'display': 'Gemma 3 1B',           'gateway_model': None,                'naming': ('google', 'gemma3-1b'),    'gemini_model': 'gemma-3-1b-it'},

    # ── LLM7 (keyless) ──
    {'name': 'llm7', 'provider': 'llm7', 'model_id': 'deepseek-r1', 'display': 'DeepSeek R1 (LLM7)', 'gateway_model': 'llm7-deepseek', 'naming': ('llm7', 'deepseek-r1'), 'assign': True},

    # ── OpenRouter (share one key / ~50/day pool) ──
    {'name': 'openrouter-gemma',        'provider': 'openrouter', 'model_id': 'google/gemma-3-4b-it:free',                                  'display': 'Gemma 3 4B (OR)',             'gateway_model': 'openrouter-gemma',        'naming': ('openrouter', 'gemma3-4b'),    'assign': True},
    {'name': 'openrouter-llama',        'provider': 'openrouter', 'model_id': 'meta-llama/llama-3.2-3b-instruct:free',                      'display': 'Llama 3.2 3B (OR)',           'gateway_model': 'openrouter-llama',        'naming': ('openrouter', 'llama3b')},
    {'name': 'openrouter-gemma-nano',   'provider': 'openrouter', 'model_id': 'google/gemma-3n-e2b-it:free',                                'display': 'Gemma 3n E2B (OR)',           'gateway_model': 'openrouter-gemma-nano',   'naming': ('openrouter', 'gemma-nano')},
    {'name': 'openrouter-gemma4-31b',   'provider': 'openrouter', 'model_id': 'google/gemma-4-31b-it:free',                                 'display': 'Gemma 4 31B (OR)',            'gateway_model': 'openrouter-gemma4-31b',   'naming': ('openrouter', 'gemma4-31b')},
    {'name': 'openrouter-gemma4-26b',   'provider': 'openrouter', 'model_id': 'google/gemma-4-26b-a4b-it:free',                             'display': 'Gemma 4 26B (OR)',            'gateway_model': 'openrouter-gemma4-26b',   'naming': ('openrouter', 'gemma4-26b')},
    {'name': 'openrouter-gemma-27b',    'provider': 'openrouter', 'model_id': 'google/gemma-3-27b-it:free',                                 'display': 'Gemma 3 27B (OR)',            'gateway_model': 'openrouter-gemma-27b',    'naming': ('openrouter', 'gemma-27b')},
    {'name': 'openrouter-gemma-12b',    'provider': 'openrouter', 'model_id': 'google/gemma-3-12b-it:free',                                 'display': 'Gemma 3 12B (OR)',            'gateway_model': 'openrouter-gemma-12b',    'naming': ('openrouter', 'gemma-12b')},
    {'name': 'openrouter-gemma3n',      'provider': 'openrouter', 'model_id': 'google/gemma-3n-e4b-it:free',                                'display': 'Gemma 3n E4B (OR)',           'gateway_model': 'openrouter-gemma3n',      'naming': ('openrouter', 'gemma3n-e4b')},
    {'name': 'openrouter-llama70b',     'provider': 'openrouter', 'model_id': 'meta-llama/llama-3.3-70b-instruct:free',                     'display': 'Llama 3.3 70B (OR)',          'gateway_model': 'openrouter-llama70b',     'naming': ('openrouter', 'llama70b')},
    {'name': 'openrouter-hermes',       'provider': 'openrouter', 'model_id': 'nousresearch/hermes-3-llama-3.1-405b:free',                  'display': 'Hermes 3 405B (OR)',          'gateway_model': 'openrouter-hermes',       'naming': ('openrouter', 'hermes-405b')},
    {'name': 'openrouter-gptoss',       'provider': 'openrouter', 'model_id': 'openai/gpt-oss-120b:free',                                  'display': 'GPT-OSS 120B (OR)',           'gateway_model': 'openrouter-gptoss',       'naming': ('openrouter', 'gptoss-120b')},
    {'name': 'openrouter-gptoss-20b',   'provider': 'openrouter', 'model_id': 'openai/gpt-oss-20b:free',                                   'display': 'GPT-OSS 20B (OR)',            'gateway_model': 'openrouter-gptoss-20b',   'naming': ('openrouter', 'gptoss-20b')},
    {'name': 'openrouter-qwen3-coder',  'provider': 'openrouter', 'model_id': 'qwen/qwen3-coder:free',                                     'display': 'Qwen3 Coder (OR)',            'gateway_model': 'openrouter-qwen3-coder',  'naming': ('openrouter', 'qwen3-coder')},
    {'name': 'openrouter-qwen3-next',   'provider': 'openrouter', 'model_id': 'qwen/qwen3-next-80b-a3b-instruct:free',                     'display': 'Qwen3 Next 80B (OR)',         'gateway_model': 'openrouter-qwen3-next',   'naming': ('openrouter', 'qwen3-next')},
    {'name': 'openrouter-minimax',      'provider': 'openrouter', 'model_id': 'minimax/minimax-m2.5:free',                                  'display': 'MiniMax M2.5 (OR)',           'gateway_model': 'openrouter-minimax',      'naming': ('openrouter', 'minimax-m2.5')},
    {'name': 'openrouter-nemotron-120b','provider': 'openrouter', 'model_id': 'nvidia/nemotron-3-super-120b-a12b:free',                     'display': 'Nemotron 3 120B (OR)',         'gateway_model': 'openrouter-nemotron-120b','naming': ('openrouter', 'nemotron-120b')},
    {'name': 'openrouter-nemotron-30b', 'provider': 'openrouter', 'model_id': 'nvidia/nemotron-3-nano-30b-a3b:free',                        'display': 'Nemotron 3 30B (OR)',          'gateway_model': 'openrouter-nemotron-30b', 'naming': ('openrouter', 'nemotron-30b')},
    {'name': 'openrouter-nemotron-9b',  'provider': 'openrouter', 'model_id': 'nvidia/nemotron-nano-9b-v2:free',                            'display': 'Nemotron Nano 9B (OR)',        'gateway_model': 'openrouter-nemotron-9b',  'naming': ('openrouter', 'nemotron-9b')},
    {'name': 'openrouter-glm',         'provider': 'openrouter', 'model_id': 'z-ai/glm-4.5-air:free',                                      'display': 'GLM 4.5 Air (OR)',            'gateway_model': 'openrouter-glm',          'naming': ('openrouter', 'glm-4.5')},
    {'name': 'openrouter-arcee',       'provider': 'openrouter', 'model_id': 'arcee-ai/trinity-large-preview:free',                         'display': 'Arcee Trinity (OR)',           'gateway_model': 'openrouter-arcee',        'naming': ('openrouter', 'arcee-trinity')},
    {'name': 'openrouter-dolphin',     'provider': 'openrouter', 'model_id': 'cognitivecomputations/dolphin-mistral-24b-venice-edition:free','display': 'Dolphin Mistral 24B (OR)',    'gateway_model': 'openrouter-dolphin',      'naming': ('openrouter', 'dolphin-24b')},
    {'name': 'openrouter-lfm',         'provider': 'openrouter', 'model_id': 'liquid/lfm-2.5-1.2b-instruct:free',                           'display': 'Liquid LFM 1.2B (OR)',        'gateway_model': 'openrouter-lfm',          'naming': ('openrouter', 'lfm-1.2b')},

    # ── Cloudflare Workers AI ──
    {'name': 'cloudflare-llama-70b',  'provider': 'cloudflare', 'model_id': '@cf/meta/llama-3.3-70b-instruct-fp8-fast',  'display': 'Llama 3.3 70B (CF)',     'gateway_model': 'cloudflare-llama-70b',  'naming': ('cloudflare', 'llama70b'),    'cf_path': '@cf/meta/llama-3.3-70b-instruct-fp8-fast'},
    {'name': 'cloudflare-qwen-coder', 'provider': 'cloudflare', 'model_id': '@cf/qwen/qwen2.5-coder-32b-instruct',      'display': 'Qwen 2.5 Coder 32B (CF)', 'gateway_model': 'cloudflare-qwen-coder', 'naming': ('cloudflare', 'qwen-coder'), 'cf_path': '@cf/qwen/qwen2.5-coder-32b-instruct'},

    # ── Cohere ──
    {'name': 'cohere-command-a',      'provider': 'cohere', 'model_id': 'command-a-03-2025',      'display': 'Command A (Cohere)',      'gateway_model': 'cohere-command-a',      'naming': ('cohere', 'command-a')},
    {'name': 'cohere-command-r-plus', 'provider': 'cohere', 'model_id': 'command-r-plus-08-2024', 'display': 'Command R+ (Cohere)',     'gateway_model': 'cohere-command-r-plus', 'naming': ('cohere', 'command-r-plus')},

    # ── GitHub Models ──
    {'name': 'github-gpt4nano',      'provider': 'github', 'model_id': 'openai/gpt-4.1-nano',    'display': 'GPT-4.1 Nano (GitHub)',    'gateway_model': 'github-gpt4nano',      'naming': ('github', 'gpt4nano')},
    {'name': 'github-deepseek-r1',   'provider': 'github', 'model_id': 'deepseek/DeepSeek-R1',   'display': 'DeepSeek R1 (GitHub)',     'gateway_model': 'github-deepseek-r1',   'naming': ('github', 'deepseek-r1')},
    {'name': 'github-llama-70b',     'provider': 'github', 'model_id': 'meta/Llama-3.3-70B-Instruct', 'display': 'Llama 3.3 70B (GitHub)', 'gateway_model': 'github-llama-70b',  'naming': ('github', 'llama70b')},
    {'name': 'github-qwen3-32b',     'provider': 'github', 'model_id': 'qwen/Qwen3-32B',         'display': 'Qwen3 32B (GitHub)',       'gateway_model': 'github-qwen3-32b',     'naming': ('github', 'qwen3-32b')},

    # ── NVIDIA NIM (5K LIFETIME credits — precious) ──
    {'name': 'nvidia', 'provider': 'nvidia', 'model_id': 'meta/llama-3.3-70b-instruct', 'display': 'Llama 3.3 70B (NVIDIA)', 'gateway_model': 'nvidia-llama', 'naming': ('nvidia', 'llama70b'), 'assign': True},

    # ── Mistral ──
    {'name': 'mistral',              'provider': 'mistral', 'model_id': 'mistral-small-latest',    'display': 'Mistral Small',         'gateway_model': 'mistral-small',      'naming': ('mistral', 'small'),       'assign': True},
    {'name': 'mistral-medium',       'provider': 'mistral', 'model_id': 'mistral-medium-latest',   'display': 'Mistral Medium',        'gateway_model': 'mistral-medium',     'naming': ('mistral', 'medium')},
    {'name': 'mistral-devstral',     'provider': 'mistral', 'model_id': 'devstral-latest',         'display': 'Devstral',              'gateway_model': 'mistral-devstral',   'naming': ('mistral', 'devstral')},
    {'name': 'mistral-magistral',    'provider': 'mistral', 'model_id': 'magistral-small-latest',  'display': 'Magistral Small',       'gateway_model': 'mistral-magistral',  'naming': ('mistral', 'magistral')},

    # ── SambaNova (free tier — 6 models) ──
    {'name': 'sambanova-deepseek-r1',  'provider': 'sambanova', 'model_id': 'DeepSeek-R1-0528',            'display': 'DeepSeek R1 (SambaNova)',    'gateway_model': 'sambanova-deepseek-r1',  'naming': ('sambanova', 'deepseek-r1'),  'assign': True},
    {'name': 'sambanova-deepseek-v3',  'provider': 'sambanova', 'model_id': 'DeepSeek-V3-0324',            'display': 'DeepSeek V3 (SambaNova)',    'gateway_model': 'sambanova-deepseek-v3',  'naming': ('sambanova', 'deepseek-v3'),  'assign': True},
    {'name': 'sambanova-v3.1',         'provider': 'sambanova', 'model_id': 'DeepSeek-V3.1',               'display': 'DeepSeek V3.1 (SambaNova)', 'gateway_model': 'sambanova-v3.1',         'naming': ('sambanova', 'deepseek-v3.1')},
    {'name': 'sambanova-v3.1-cb',      'provider': 'sambanova', 'model_id': 'DeepSeek-V3.1-cb',            'display': 'DeepSeek V3.1-cb (SambaNova)', 'gateway_model': 'sambanova-v3.1-cb',   'naming': ('sambanova', 'deepseek-v3.1cb')},
    {'name': 'sambanova-v3.2',         'provider': 'sambanova', 'model_id': 'DeepSeek-V3.2',               'display': 'DeepSeek V3.2 (SambaNova)', 'gateway_model': 'sambanova-v3.2',         'naming': ('sambanova', 'deepseek-v3.2')},
    {'name': 'sambanova-llama-8b',     'provider': 'sambanova', 'model_id': 'Meta-Llama-3.1-8B-Instruct',  'display': 'Llama 3.1 8B (SambaNova)',  'gateway_model': 'sambanova-llama-8b',     'naming': ('sambanova', 'llama8b')},

    # ── Worker backends (grok/deepseek via kindness-worker Cloud Run) ──
    {'name': 'grok',      'provider': 'worker', 'model_id': 'grok-3',      'display': 'Grok 3',      'gateway_model': None, 'naming': ('xai', 'grok3'),      'worker_type': 'grok',      'assign': True},
    {'name': 'grok_fast', 'provider': 'worker', 'model_id': 'grok-3-fast', 'display': 'Grok 3 Fast', 'gateway_model': None, 'naming': ('xai', 'grok3-fast'), 'worker_type': 'grok_fast', 'assign': True},
    {'name': 'grok4',     'provider': 'worker', 'model_id': 'grok-4',      'display': 'Grok 4',      'gateway_model': None, 'naming': ('xai', 'grok4'),      'worker_type': 'grok4',     'assign': True},
    {'name': 'deepseek',  'provider': 'worker', 'model_id': 'deepseek-chat', 'display': 'DeepSeek Chat V3', 'gateway_model': None, 'naming': ('deepseek', 'chat-v3'), 'worker_type': 'deepseek'},
]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DB-backed MODELS loader. The catalog audit cron is the source of truth —
# this file's _FALLBACK_MODELS is only used when the DB is unreachable
# (cold-start, network blip). When the DB is reachable, MODELS is whatever
# kumori_llm_provider_limits says is currently active or probationary.
#
# Why import-time and not lazy: each app instance lives long enough that one
# fetch on boot is fine, and apps already snapshot derived constants
# (BACKEND_NAMING, AVAILABLE_BACKENDS) at import. Auto-retired backends
# disappear from the registry the next time an instance starts — App Engine
# cycles instances often enough for this to be effectively-realtime.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _load_models_from_db():
    """Pull the live registry from kumori_llm_provider_limits. Returns a list
    shaped like _FALLBACK_MODELS, or None if the DB is unreachable.

    Uses Secret Manager via the Python SDK — works on App Engine (metadata
    server auth) AND locally (gcloud-resolved ADC). Never shells out to the
    gcloud CLI at import time (it blocks for 8s+ when missing on App Engine
    and bricks instance startup)."""
    import os
    import logging
    logger = logging.getLogger(__name__)

    try:
        import psycopg2
        from google.cloud import secretmanager
    except ImportError as e:
        logger.warning(f"backend_registry: missing dep, using static fallback: {e}")
        return None

    _client = None
    _secret_cache = {}

    def _secret(name):
        nonlocal _client
        # Env var wins (local dev convenience)
        env_val = os.environ.get(name)
        if env_val:
            return env_val
        if name in _secret_cache:
            return _secret_cache[name]
        try:
            if _client is None:
                _client = secretmanager.SecretManagerServiceClient()
            resp = _client.access_secret_version(
                request={"name": f"projects/kumori-404602/secrets/{name}/versions/latest"}
            )
            val = resp.payload.data.decode('UTF-8')
            _secret_cache[name] = val
            return val
        except Exception:
            _secret_cache[name] = None
            return None

    # Prefer Cloud SQL unix socket if available (App Engine), else TCP via secret IP
    cloudsql_conn = os.environ.get('CLOUD_SQL_CONNECTION_NAME', '') or _secret('KUMORI_POSTGRES_CONNECTION_NAME') or ''
    socket_path = '/cloudsql/' + cloudsql_conn if cloudsql_conn else ''
    conn_args = {
        'dbname':   _secret('KUMORI_POSTGRES_DB_NAME'),
        'user':     _secret('KUMORI_POSTGRES_USERNAME'),
        'password': _secret('KUMORI_POSTGRES_PASSWORD'),
    }
    if not all(conn_args.values()):
        return None
    if socket_path and os.path.exists(socket_path):
        conn_args['host'] = socket_path
    else:
        conn_args['host'] = _secret('KUMORI_POSTGRES_IP')
        conn_args['port'] = 5432

    try:
        conn = psycopg2.connect(connect_timeout=8, **conn_args)
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT backend, provider, model_id, display_name, gateway_model,
                       naming_provider, naming_model, assign_new_agents,
                       gemini_model, worker_type, cf_path, overrides
                  FROM kumori_llm_provider_limits
                 WHERE status IN ('active', 'probationary')
                   AND model_id IS NOT NULL
                   AND provider IS NOT NULL
            """)
            rows = []
            for r in cur.fetchall():
                (backend, provider, model_id, display, gateway_model,
                 np_, nm_, assign, gemini_model, worker_type, cf_path, overrides) = r
                # Defensive: skip any row whose provider isn't in our static
                # PROVIDERS dict. Prevents DB drift (legacy rows, audit
                # discoveries from a brand-new provider) from KeyError-crashing
                # build_backends() at import time. The audit cron is the place
                # to resolve provider mismatches; this is the safety net.
                if provider not in PROVIDERS:
                    continue
                d = {
                    'name':          backend,
                    'provider':      provider,
                    'model_id':      model_id,
                    'display':       display,
                    'gateway_model': gateway_model,
                }
                if np_:
                    d['naming'] = (np_, nm_)
                if assign:
                    d['assign'] = True
                if gemini_model:
                    d['gemini_model'] = gemini_model
                if worker_type:
                    d['worker_type'] = worker_type
                if cf_path:
                    d['cf_path'] = cf_path
                if overrides:
                    d['overrides'] = overrides
                rows.append(d)
            return rows or None
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"backend_registry: DB load failed, using static fallback: {e}")
        return None


_db_models = _load_models_from_db()
MODELS = _db_models if _db_models else _FALLBACK_MODELS
MODELS_SOURCE = 'database' if _db_models else 'static_fallback'

# Count for display/marketing — live count, reflects current registry state
FREE_MODEL_COUNT = len(MODELS)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LiteLLM gateway + paid backends (not in MODELS — separate tier)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

LITELLM_BACKENDS = [
    {'name': 'litellm-gateway', 'type': 'litellm', 'litellm_model': 'groq-llama-70b'},
]

# Non-router entries (local-only). Paid backends (Anthropic / OpenAI) intentionally
# absent from the canonical — paid Anthropic surface is wrapped at the call site
# via utilities/killswitch.py + check_killswitch('anthropic') in each project.
KINDNESS_ONLY_MODELS = {
    'local':  {'model_id': 'lmstudio/auto',              'provider': 'LM Studio (local)', 'display': 'Local LLM (LM Studio)'},
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Derivation functions — auto-generate everything the router and apps need
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _build_backend_dict(m):
    """Convert a MODELS entry into the backend dict format the router expects."""
    prov = PROVIDERS[m['provider']]
    btype = prov.get('type')

    if btype == 'worker':
        return {'name': m['name'], 'type': m['worker_type']}

    if btype == 'gemini':
        d = {'name': m['name'], 'type': 'gemini', 'secret': prov['secret'],
             'gemini_model': m.get('gemini_model', m['model_id'])}
        if m.get('gateway_model'):
            d['gateway_model'] = m['gateway_model']
        return d

    if btype == 'cloudflare':
        url = prov['url'] + m.get('cf_path', m['model_id'])
        return {'name': m['name'], 'url': url, 'secret': prov['secret'],
                'type': 'cloudflare', 'gateway_model': m.get('gateway_model')}

    if btype == 'cohere':
        return {'name': m['name'], 'url': prov['url'], 'model': m['model_id'],
                'secret': prov['secret'], 'type': 'cohere',
                'gateway_model': m.get('gateway_model')}

    # Default: openai-compatible
    d = {'name': m['name'], 'url': prov['url'], 'model': m['model_id'],
         'secret': prov['secret']}
    if m.get('gateway_model'):
        d['gateway_model'] = m['gateway_model']
    return d


def build_backends():
    """Build the BACKENDS list for the router."""
    return [_build_backend_dict(m) for m in MODELS]


def build_fallback_limits():
    """Build the _FALLBACK_LIMITS dict for the router."""
    limits = {}
    for m in MODELS:
        prov = PROVIDERS[m['provider']]
        base = dict(prov['limits'])
        base.setdefault('enabled', True)
        if m.get('overrides'):
            base.update(m['overrides'])
        limits[m['name']] = base

    # Non-model backends (free only)
    limits['litellm-gateway'] = {'daily_limit': 500, 'rpm_spacing_sec': 2.0, 'backoff_sec': 120, 'enabled': True}
    return limits


def build_eval_pool():
    """Build the EVAL_POOL_FREE list — backends eligible for eval calls."""
    # Exclude worker backends (slow) and conservation-mode backends
    excluded_providers = {'worker'}
    pool = []
    for m in MODELS:
        if m['provider'] in excluded_providers:
            continue
        prov = PROVIDERS[m['provider']]
        overrides = m.get('overrides', {})
        if overrides.get('conservation') or prov['limits'].get('conservation'):
            continue
        if m.get('eval') is False:
            continue
        pool.append(m['name'])
    return pool


def build_available_backends():
    """Build the list of backends eligible for agent assignment."""
    return [m['name'] for m in MODELS if m.get('assign')]


def build_backend_naming():
    """Build BACKEND_NAMING dict: backend_name -> (provider_slug, model_short)."""
    naming = {m['name']: m['naming'] for m in MODELS if m.get('naming')}
    naming.update({
        'local': ('local', 'lmstudio'),
    })
    return naming


def build_model_registry():
    """Build the MODELS dict for kindness_social model_registry.py."""
    registry = {}
    for m in MODELS:
        prov = PROVIDERS[m['provider']]
        registry[m['name']] = {
            'model_id': m['model_id'],
            'provider': prov['display'],
            'display': m['display'],
        }
    # Add kindness-only entries
    registry.update(KINDNESS_ONLY_MODELS)
    return registry


def build_free_backends_set():
    """Build the set of free backend names."""
    return {m['name'] for m in MODELS}


# Pre-build for import convenience
BACKENDS = build_backends()
FALLBACK_LIMITS = build_fallback_limits()
EVAL_POOL_FREE = build_eval_pool()
AVAILABLE_BACKENDS = build_available_backends()
BACKEND_NAMING = build_backend_naming()

# Derived constants used by apps
FALLBACK_ORDER = [m['name'] for m in MODELS]
CLOUD_RUN_ONLY = {m['name'] for m in MODELS if m['provider'] == 'worker'}
CLOUD_RUN_WORKER_URL = 'https://kindness-worker-243380010344.us-central1.run.app'
