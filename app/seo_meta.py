"""
Reusable SEO metadata for LeadMeLeads' public pages.

Single source of truth for title/description/canonical/robots/Open-Graph/
Twitter-card markup and the public-page allowlist, so the three indexable
routes (/, /lead-bot, /compare) render consistent tags instead of
duplicating <head> markup across Jinja templates and a hand-built
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
PUBLIC_INDEXABLE_PATHS = (
    "/",
    "/lead-bot",
    "/compare",
    "/what-makes-a-good-lead",
    "/how-to-find-local-leads",
    "/how-to-find-local-business-leads-without-buying-a-lead-list",
    "/how-to-verify-local-business-leads-before-outreach",
    "/local-lead-generation",
    "/lead-list-vs-lead-finder",
    "/how-to-find-website-seo-opportunities-in-a-lead-list",
    "/resources",
)

# Static assets are served from here and must never be noindexed or
# disallowed -- that would risk crawlers being told not to fetch CSS/JS.
STATIC_ASSET_PREFIX = "/static/"

# Crawler-control files. These aren't indexable pages -- they're the
# directives crawlers read to decide what to index -- so they must never
# carry X-Robots-Tag: noindex themselves, regardless of the public page
# allowlist above.
CRAWLER_CONTROL_PATHS = ("/robots.txt", "/sitemap.xml")


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

GOOD_LEAD_PAGE = SeoPage(
    title="What Makes a Good Lead? A Practical Guide | LeadMeLeads",
    description=(
        "Learn what makes a good lead, how to identify businesses worth "
        "contacting, and why quality matters more than collecting more names."
    ),
    canonical_path="/what-makes-a-good-lead",
)

FIND_LOCAL_LEADS_PAGE = SeoPage(
    title="How to Find Local Leads: A Step-by-Step Process | LeadMeLeads",
    description=(
        "Learn how to find local businesses worth reviewing, check contact "
        "details and website signals, and organize prospects before outreach."
    ),
    canonical_path="/how-to-find-local-leads",
)

FIND_LOCAL_BUSINESS_LEADS_WITHOUT_LIST_PAGE = SeoPage(
    title="Find Local Business Leads Without Buying a List | LeadMeLeads",
    description=(
        "Learn how to find local business leads without buying a list. Search "
        "by keyword and location, review public details, and verify prospects "
        "before outreach."
    ),
    canonical_path="/how-to-find-local-business-leads-without-buying-a-lead-list",
)

VERIFY_LOCAL_BUSINESS_LEADS_PAGE = SeoPage(
    title="Verify Local Business Leads Before Outreach | LeadMeLeads",
    description=(
        "Learn how to verify local business leads before outreach by checking "
        "business status, websites, contact details, locations, and visible "
        "opportunities."
    ),
    canonical_path="/how-to-verify-local-business-leads-before-outreach",
)

LOCAL_LEAD_GENERATION_PAGE = SeoPage(
    title="Local Lead Generation: A Practical Guide for Finding Prospects",
    description=(
        "Compare practical local lead generation methods, from referrals and "
        "directories to search-based prospect research for outbound outreach."
    ),
    canonical_path="/local-lead-generation",
)

LEAD_LIST_VS_FINDER_PAGE = SeoPage(
    title="Lead Lists vs Lead Finders: Which Is Better for Prospecting?",
    description=(
        "Compare purchased lead lists with active lead finders across freshness, "
        "targeting, contact data, research time, and prospect relevance."
    ),
    canonical_path="/lead-list-vs-lead-finder",
)

FIND_WEBSITE_SEO_OPPORTUNITIES_IN_LEAD_LIST_PAGE = SeoPage(
    title=(
        "How to Find Website and SEO Opportunities in a Local Lead List | "
        "LeadMeLeads"
    ),
    description=(
        "A practical framework for auditing a local lead list to spot "
        "website and SEO gaps worth prioritizing before outreach."
    ),
    canonical_path="/how-to-find-website-seo-opportunities-in-a-lead-list",
)

RESOURCES_PAGE = SeoPage(
    title="Lead Generation Resources | LeadMeLeads",
    description=(
        "Explore practical guides to finding, reviewing, and organizing local "
        "business prospects for thoughtful outreach."
    ),
    canonical_path="/resources",
)

# Single source of truth for the /what-makes-a-good-lead FAQ: the same
# list drives both the visible FAQ section (rendered by the Jinja
# template) and the FAQPage JSON-LD below, so the schema can never drift
# from what a visitor actually sees on the page.
GOOD_LEAD_FAQ = [
    {
        "question": "What makes a good lead?",
        "answer": (
            "A good lead is a business that fits your target market, is "
            "reachable, and gives you a clear reason to reach out. It is "
            "not about how many contacts you have -- it is about how many "
            "of them are actually worth pursuing."
        ),
    },
    {
        "question": "How do I find good leads?",
        "answer": (
            "Start with a specific search instead of a purchased list -- "
            "a keyword and a location narrows results to businesses that "
            "plausibly fit what you sell. Then review each business "
            "individually: is it reachable, does it match your target "
            "market, and is there a real, specific reason to contact it."
        ),
    },
    {
        "question": "What is the difference between a good lead and a qualified lead?",
        "answer": (
            "A good lead is a business worth a first outreach attempt; a "
            "qualified lead is one that has been confirmed, usually "
            "through a conversation, to have real need, budget, and "
            "timing. LeadMeLeads helps you find good leads worth "
            "contacting -- qualifying them still happens through your own "
            "outreach and discovery process."
        ),
    },
    {
        "question": "Are local leads better?",
        "answer": (
            "Local leads are not automatically better, but local context "
            "usually makes outreach easier. Businesses that share your "
            "market, region, or local search competition are simpler to "
            "research and reference specifically, which tends to make a "
            "first message land better than something generic."
        ),
    },
    {
        "question": "Is buying a large lead list worth it?",
        "answer": (
            "Usually not, unless the list is genuinely targeted and "
            "current. Most purchased lead lists mix outdated contact "
            "information with businesses that do not match your target "
            "market, so a large list often means more time spent "
            "filtering rather than more real opportunities."
        ),
    },
]

FIND_LOCAL_LEADS_FAQ = [
    {
        "question": "What is a local lead?",
        "answer": (
            "A local lead is a business in a defined geographic market that "
            "plausibly fits what you sell and is worth reviewing for outreach. "
            "It is not automatically a qualified buyer."
        ),
    },
    {
        "question": "How do I find local business leads?",
        "answer": (
            "Choose a specific service or business type and a real market, find "
            "businesses through search or a lead finder, then check each "
            "business for fit, reachable contact details, and useful context."
        ),
    },
    {
        "question": "What should I check before contacting a local business?",
        "answer": (
            "Check that the business serves the market you target, its website "
            "and contact details appear current, and you have a specific reason "
            "for reaching out. Verify important details yourself before sending."
        ),
    },
    {
        "question": "Does a low Google position make a business a qualified lead?",
        "answer": (
            "No. Search position can provide context for your research, but it "
            "does not prove need, budget, authority, timing, or willingness to "
            "buy. Qualification happens through direct conversation."
        ),
    },
]

LOCAL_LEAD_GENERATION_FAQ = [
    {
        "question": "What is local lead generation?",
        "answer": (
            "Local lead generation is the process of creating or identifying "
            "potential business opportunities within a defined geographic "
            "market. It can include inbound marketing, referrals, networking, "
            "directories, and outbound prospect research."
        ),
    },
    {
        "question": "What is the difference between inbound and outbound local lead generation?",
        "answer": (
            "Inbound methods encourage potential customers to contact you, while "
            "outbound methods involve finding relevant businesses and starting "
            "the conversation yourself. They solve different parts of growth."
        ),
    },
    {
        "question": "Are directories useful for local lead generation?",
        "answer": (
            "Directories can be useful for discovering businesses in a category "
            "or area, but entries may be incomplete or outdated. Treat them as "
            "a starting point and verify each business before outreach."
        ),
    },
    {
        "question": "Does LeadMeLeads generate inbound customer inquiries?",
        "answer": (
            "No. LeadMeLeads helps users find and review local businesses for "
            "outbound outreach. It does not run inbound campaigns or claim that "
            "the businesses it finds are ready to buy."
        ),
    },
]

LEAD_LIST_VS_FINDER_FAQ = [
    {
        "question": "What is the main difference between a lead list and a lead finder?",
        "answer": (
            "A lead list is a prepared dataset you receive at once, while a lead "
            "finder lets you search for prospects using current targeting choices "
            "and review the results before outreach."
        ),
    },
    {
        "question": "Are purchased lead lists always bad?",
        "answer": (
            "No. A reputable, recent, well-targeted list can make sense when you "
            "need broad coverage and have a process for verification, filtering, "
            "and compliant outreach."
        ),
    },
    {
        "question": "Does a lead finder guarantee accurate contact information?",
        "answer": (
            "No. Public business information can change, so contact data from any "
            "source should be checked before use. A lead finder can make review "
            "easier, but it cannot eliminate stale or incomplete data."
        ),
    },
    {
        "question": "Which option is better for local prospecting?",
        "answer": (
            "A lead finder is often better when geographic precision and "
            "business-by-business review matter. A purchased list may be more "
            "efficient when scale and standardized coverage matter more."
        ),
    },
]


def _jsonld_script(data: dict) -> str:
    return f'<script type="application/ld+json">{json.dumps(data, separators=(",", ":"))}</script>'


def render_faq_jsonld(faq_items: list) -> str:
    """FAQPage JSON-LD built directly from the same faq_items list the
    calling template renders as visible copy, so schema and visible
    content can't drift out of sync with each other."""
    return _jsonld_script({
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": item["question"],
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": item["answer"],
                },
            }
            for item in faq_items
        ],
    })


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


def render_webapplication_jsonld(name: str, description: str, canonical_path: str) -> str:
    """WebApplication JSON-LD for a single tool page (Lead Finder,
    Compare). Verified facts only -- name, description, and canonical
    URL taken from the page's own SeoPage entry, no ratings, pricing,
    or user counts, none of which are verifiable here."""
    return _jsonld_script({
        "@context": "https://schema.org",
        "@type": "WebApplication",
        "name": name,
        "description": description,
        "url": canonical_url(canonical_path),
        "applicationCategory": "BusinessApplication",
        "operatingSystem": "Web",
    })


def render_article_jsonld(page: SeoPage) -> str:
    """Minimal Article JSON-LD for a guide/resource page. Only fields
    directly backed by visible on-page content (headline, description,
    canonical URL, publisher) -- deliberately omits datePublished /
    dateModified since no publish date is shown anywhere on the page,
    and fabricating one would violate the "accurate JSON-LD only" rule."""
    return _jsonld_script({
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": page.title,
        "description": page.description,
        "mainEntityOfPage": canonical_url(page.canonical_path),
        "publisher": {
            "@type": "Organization",
            "name": SITE_NAME,
            "logo": {
                "@type": "ImageObject",
                "url": f"{SITE_BASE_URL}/static/logo.png",
            },
        },
    })


def render_not_found_html() -> str:
    """Branded 404 page for unmatched routes. Deliberately static and
    self-contained -- no request data (path, query string, referrer) is
    echoed back into the page, so there is nothing here for a crafted
    URL to inject into the response."""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Page Not Found | {SITE_NAME}</title>
<meta name="robots" content="noindex, nofollow">
<style>
body {{
    margin: 0;
    min-height: 100vh;
    display: grid;
    place-items: center;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
    background: #0f172a;
    color: #0f172a;
}}
.notfound-card {{
    width: min(480px, calc(100vw - 32px));
    background: white;
    border-radius: 20px;
    padding: 36px;
    box-shadow: 0 24px 70px rgba(0,0,0,.32);
    box-sizing: border-box;
    text-align: center;
}}
h1 {{ margin: 0 0 8px; font-size: 26px; }}
p {{ margin: 0 0 22px; color: #64748b; }}
.notfound-links {{ display: flex; flex-wrap: wrap; gap: 10px; justify-content: center; }}
.notfound-links a {{
    padding: 10px 16px;
    border-radius: 10px;
    background: #eff6ff;
    color: #1e3a8a;
    font-weight: 700;
    text-decoration: none;
}}
</style>
</head>
<body>
<div class="notfound-card">
<h1>Page Not Found</h1>
<p>The page you're looking for doesn't exist or may have moved.</p>
<div class="notfound-links">
<a href="/">Home</a>
<a href="/lead-bot">Lead Finder</a>
<a href="/compare">Compare</a>
<a href="/resources">Resources</a>
</div>
</div>
</body>
</html>
"""


def should_apply_noindex_header(path: str) -> bool:
    """True for any route that must never be indexed -- everything
    except the public allowlist, static assets, and the crawler-control
    files themselves. Deny-list-based exclusions rather than an
    allow-list of private routes, so a new private route added later is
    noindexed by default."""
    if path in PUBLIC_INDEXABLE_PATHS:
        return False
    if path in CRAWLER_CONTROL_PATHS:
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
        "Disallow: /admin",
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
