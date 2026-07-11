"""GA4 measurement-ID registry + tag-snippet helper.

Per-site GA4 measurement IDs live in MEASUREMENT_IDS (filled in by
setup_ga4_portfolio.py after the GA4 properties are created). Each site's
base.html template calls `{{ ga_snippet('<site_slug>') }}` to inject the
gtag block.

Wire in your Flask app:

    from utilities.gtag import snippet
    app.jinja_env.globals['ga_snippet'] = snippet

Then in base.html:

    {{ ga_snippet('inroads') | safe }}

Failure mode: if a slug isn't in the registry, snippet() returns an empty
string — silent no-op so a missing wiring doesn't break the page.
"""

from __future__ import annotations

# Filled in by setup_ga4_portfolio.py after GA4 property creation.
# Slug → measurement_id (G-XXXXXXXXXX format).
# Three of these existed before today's rebuild and stay alive:
#   digital_empire_tv, ooqio (=trustable.cc), 2manspades
MEASUREMENT_IDS: dict[str, str] = {
    # Pre-existing, still alive
    'digital_empire_tv': 'G-B876QNS855',
    'ooqio':             'G-ST971T1LGC',  # trustable.cc
    '2manspades':        'G-F5VP0CXERK',
    'inroads'               : 'G-4Z15YJP9WR',  # auto-added by setup_ga4_portfolio.py
    'kumori'                : 'G-07HHXYQTY7',  # auto-added by setup_ga4_portfolio.py
    'scatterbrain'          : 'G-HH3MW3C5WW',  # auto-added by setup_ga4_portfolio.py
    'kindness_social'       : 'G-DCQQQB46DJ',  # auto-added by setup_ga4_portfolio.py
    'kicksaw'               : 'G-D7YXVVLZB4',  # auto-added by setup_ga4_portfolio.py
    'wattson'               : 'G-MM9PVQWPRW',  # auto-added by setup_ga4_portfolio.py
    'galactica'             : 'G-EMM8WK9XV3',  # auto-added by setup_ga4_portfolio.py
    'crab_travel'           : 'G-Y88C7F3KH3',  # auto-added by setup_ga4_portfolio.py
    'briskr'                : 'G-C2MW0R7EMW',  # auto-added by setup_ga4_portfolio.py
    'dandy'                 : 'G-99G5F03H1R',  # auto-added by setup_ga4_portfolio.py
    'refinr'                : 'G-QFF3DRM48X',  # auto-added by setup_ga4_portfolio.py
    # Filled in by setup_ga4_portfolio.py for the 9 new properties:
    # 'inroads':         'G-...',
    # 'kumori':          'G-...',
    # 'scatterbrain':    'G-...',
    # 'kindness_social': 'G-...',
    # 'wattson':         'G-...',
    # 'galactica':       'G-...',
    # 'crab_travel':     'G-...',
    # 'briskr':          'G-...',
    # 'dandy':           'G-...',
    # 'refinr':          'G-...',
}


_TEMPLATE = """\
<!-- Google Analytics (GA4) -->
<script async src="https://www.googletagmanager.com/gtag/js?id={mid}"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){{ dataLayer.push(arguments); }}
  gtag('js', new Date());
  gtag('config', '{mid}');
</script>
"""


def snippet(slug: str) -> str:
    """Return the canonical gtag.js script block for a given site slug.
    Returns '' if the slug isn't in the registry — page still renders fine."""
    mid = MEASUREMENT_IDS.get(slug)
    if not mid:
        return ''
    return _TEMPLATE.format(mid=mid)
