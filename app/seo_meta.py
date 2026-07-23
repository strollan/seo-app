"""
Reusable SEO metadata for LeadMeLeads' public pages.

Single source of truth for title/description/canonical/robots/Open-Graph/
Twitter-card markup and the public-page allowlist, so the three indexable
routes (/, /lead-bot, /compare) render consistent tags instead of
duplicating <head> markup across two Jinja templates and a hand-built
HTML string (agents/lead_dashboard_agent.py). robots.txt, sitemap.xml,
and the noindex response middleware all read from the same
PUBLIC_INDEXABLE_PATHS set so they can't drift out of sync with each other.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass

SITE_NAME = "LeadMeLeads"
SITE_BASE_URL = "https://leadmeleads.com"

# The only routes meant to be indexed. robots.txt, sitemap.xml, and the
# noindex response middleware (see app/main.py) all key off this set --
# anything not listed here is noindexed by default, so a new dynamic
# route can't accidentally become indexable just by existing.
PUBLIC_INDEXABLE_PATHS = ("/", "/lead-bot", "/compare")

# Static assets are served from here and must never be noindexed or
# disallowed -- that would risk crawlers being told not to fetch CSS/JS.
STATIC_ASSET_PREFIX = "/static/"


@dataclass(frozen=True)
class SeoPage:
    title: str
    description: str
    canonical_path: str
    og_type: str = "website"


def canonical_url(canonical_path: str) -> str:
    path = canonical_path if canonical_path.startswith("/") else f"/{canonical_path}"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return f"{SITE_BASE_URL}{path}"


def render_seo_meta_html(page: SeoPage) -> str:
    """Render the <title> + description/canonical/robots/OG/Twitter block
    for a public, indexable page. Returns one HTML string meant to be
    inserted directly into that page's <head>."""
    title = html.escape(page.title)
    description = html.escape(page.description)
    canonical = html.escape(canonical_url(page.canonical_path))
    og_type = html.escape(page.og_type)
    site_name = html.escape(SITE_NAME)

    return (
        f'<title>{title}</title>\n'
        f'<meta name="description" content="{description}">\n'
        f'<link rel="canonical" href="{canonical}">\n'
        f'<meta name="robots" content="index, follow">\n'
        f'<meta property="og:title" content="{title}">\n'
        f'<meta property="og:description" content="{description}">\n'
        f'<meta property="og:url" content="{canonical}">\n'
        f'<meta property="og:type" content="{og_type}">\n'
        f'<meta property="og:site_name" content="{site_name}">\n'
        f'<meta name="twitter:card" content="summary_large_image">\n'
        f'<meta name="twitter:title" content="{title}">\n'
        f'<meta name="twitter:description" content="{description}">\n'
    )


NOINDEX_META_HTML = '<meta name="robots" content="noindex, nofollow">\n'


HOME_PAGE = SeoPage(
    title="LeadMeLeads — Find Local Leads Worth Contacting",
    description=(
        "Find local business leads by keyword and location. Review website "
        "gaps, contact details, and clear reasons each lead may be worth "
        "contacting."
    ),
    canonical_path="/",
)

LEAD_FINDER_PAGE = SeoPage(
    title="Local Lead Finder | LeadMeLeads",
    description=(
        "Find local business leads by keyword and location, review contact "
        "details, and prioritize businesses worth contacting."
    ),
    canonical_path="/lead-bot",
)

COMPARE_PAGE = SeoPage(
    title="Website SEO Comparison Tool | LeadMeLeads",
    description=(
        "Compare two websites and uncover practical SEO opportunities, "
        "missing page elements, and clear recommendations in minutes."
    ),
    canonical_path="/compare",
)


def _jsonld_script(data: dict) -> str:
    return f'<script type="application/ld+json">{json.dumps(data, separators=(",", ":"))}</script>'


def render_homepage_jsonld() -> str:
    """Organization + WebSite JSON-LD for the homepage only. Verified
    facts only: name, canonical site URL, and the logo asset this app
    actually serves at /static/logo.png. Deliberately excludes
    SearchAction (no real public search-results URL to point it at),
    ratings, pricing, customer counts, founding dates, social profiles,
    or geographic-coverage claims -- none of those are verifiable here."""
    organization = {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": SITE_NAME,
        "url": SITE_BASE_URL,
        "logo": f"{SITE_BASE_URL}/static/logo.png",
    }
    website = {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": SITE_NAME,
        "url": SITE_BASE_URL,
    }
    return _jsonld_script(organization) + "\n" + _jsonld_script(website)


def should_apply_noindex_header(path: str) -> bool:
    """True for any route that must never be indexed -- everything
    except the public allowlist and static assets. Deny-list-based
    exclusions (static assets only) rather than an allow-list of private
    routes, so a new private route added later is noindexed by default."""
    if path in PUBLIC_INDEXABLE_PATHS:
        return False
    if path.startswith(STATIC_ASSET_PREFIX):
        return False
    return True


def render_robots_txt() -> str:
    lines = [
        "User-agent: *",
        "Disallow: /login",
        "Disallow: /logout",
        "Disallow: /signup",
        "Disallow: /create-account",
        "Disallow: /forgot-password",
        "Disallow: /reset-password",
        "Disallow: /settings",
        "Disallow: /save-settings",
        "Disallow: /history",
        "Disallow: /analyze",
        "Disallow: /export-pdf",
        "Disallow: /reports/",
        "Disallow: /lead-bot/",
        "Disallow: /api/",
        "",
        f"Sitemap: {SITE_BASE_URL}/sitemap.xml",
    ]
    return "\n".join(lines) + "\n"


def render_sitemap_xml() -> str:
    urls = "\n".join(
        f"<url><loc>{html.escape(canonical_url(path))}</loc></url>"
        for path in PUBLIC_INDEXABLE_PATHS
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}\n"
        "</urlset>\n"
    )
