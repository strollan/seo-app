
# Suppress noisy SSL warnings during site audits.
# Some client sites have imperfect SSL configs, but audits should continue.
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from dotenv import load_dotenv
load_dotenv()


# === LEADBOT EMAIL CLEANER GLOBAL FALLBACK START ===
try:
    from agents.lead_email_cleaner_agent import clean_lead_emails
except Exception:
    def clean_lead_emails(value, lead_domain="", website=""):
        if isinstance(value, list):
            return ", ".join([str(v).strip() for v in value if str(v).strip()])
        return str(value or "").strip()
# === LEADBOT EMAIL CLEANER GLOBAL FALLBACK END ===

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from app.agent import build_agent_insight_html, build_agent_action_plan, enhance_analysis, enhance_quick_wins
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from collections import Counter
from datetime import datetime
from urllib.parse import urlencode,  urlparse
import os
import json
import base64
import re
import tempfile

import requests
from bs4 import BeautifulSoup
from agents.crawl_agent import crawl_get

from app.agent_service import run_agent_summary
from app.competitor_agent import find_competitors
from agents.seo_agent import run_seo_agent

app = FastAPI()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

static_dir = os.path.join(BASE_DIR, "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


reports_static_dir = os.path.join(os.path.dirname(BASE_DIR), "reports")
if os.path.isdir(reports_static_dir):
    app.mount("/reports", StaticFiles(directory=reports_static_dir), name="reports")


def location_terms_file_path() -> str:
    return os.path.join(BASE_DIR, "location_terms.txt")


def load_custom_location_terms():
    file_path = location_terms_file_path()
    if not os.path.exists(file_path):
        return set()

    with open(file_path, "r", encoding="utf-8") as f:
        return {line.strip().lower() for line in f if line.strip()}


def load_custom_location_text() -> str:
    file_path = location_terms_file_path()
    if not os.path.exists(file_path):
        return ""

    with open(file_path, "r", encoding="utf-8") as f:
        return f.read().strip()


def normalize_term_list(lines):
    return sorted(
        {
            line.strip().lower()
            for line in lines
            if line and line.strip()
        }
    )


BAD_LOCATION_TERMS = {
    "local",
    "near me",
    "nearby",
    "service area",
    "areas served",
    "scroll back up",
}

def clean_auto_geo_phrase(value):
    value = (value or "").strip()
    value = re.sub(r"\s+", " ", value)
    value = value.replace(",", ", ").replace("  ", " ")
    value = value.strip(" -|•·")
    return value.lower()


def auto_save_geo_phrases(phrases):
    """
    Automatically adds geo phrases found on analyzed pages
    into the saved custom location list used by /settings.
    """
    if not phrases:
        return

    try:
        location_sets = get_location_sets()
        existing_custom = location_sets.get("custom", [])
        existing_all = set(location_sets.get("combined", []))

        cleaned = []
        blocked = {
            "",
            *BAD_LOCATION_TERMS,
        }

        for phrase in phrases:
            item = clean_auto_geo_phrase(phrase)

            if not item or item in blocked:
                continue

            if len(item) < 3 or len(item) > 80:
                continue

            if item not in existing_all and item not in cleaned:
                cleaned.append(item)

        if not cleaned:
            return

        updated_custom = normalize_term_list(existing_custom + cleaned)

        # Uses the same custom settings file that /settings uses.
        CUSTOM_LOCATIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        CUSTOM_LOCATIONS_FILE.write_text("\n".join(updated_custom) + "\n", encoding="utf-8")

    except Exception as e:
        print(f"auto_save_geo_phrases error: {e}")


def get_location_sets():
    custom_terms = load_custom_location_terms()
    built_in_terms = set(BASE_LOCATION_TERMS)
    combined_terms = built_in_terms.union(custom_terms)

    return {
        "built_in": sorted(built_in_terms),
        "custom": sorted(custom_terms),
        "combined": sorted(combined_terms),
    }


STOP_WORDS = {
    "the", "and", "for", "with", "that", "this", "from", "your", "have", "are",
    "you", "was", "but", "not", "all", "can", "our", "out", "has", "get",
    "use", "how", "why", "what", "when", "where", "who", "about",
    "into", "than", "then", "them", "they", "their", "there",
    "will", "would", "could", "should", "here", "been", "also",
    "more", "most", "some", "any", "each", "page", "home", "contact",
    "services", "service", "click", "read", "learn", "best", "top",
    "today", "now", "login", "account", "sign", "video", "menu",
    "news", "live", "watch", "search", "cookies", "policy", "terms",
    "about", "welcome", "official", "website", "site", "page"
}

BASE_LOCATION_TERMS = {
    "long island", "new york", "nyc", "brooklyn", "queens", "bronx", "manhattan",
    "staten island", "suffolk", "nassau", "local", "near me", "nearby",
    "plano", "dallas", "houston", "miami", "chicago", "los angeles", "san diego",
    "boca raton", "florida", "texas", "california",
    "bellmore", "east meadow", "hicksville", "beverly hills",
    "southern california", "orange county", "san jose", "bay area"
}

LOCATION_TERMS = BASE_LOCATION_TERMS.union(load_custom_location_terms())

COMMERCIAL_TERMS = {
    "cost", "price", "pricing", "quote", "estimate", "affordable", "cheap",
    "company", "agency", "expert", "experts", "professional", "professionals",
    "hire", "hiring", "services", "service", "consultant", "consulting",
    "best", "top", "trusted", "leading", "rated"
}

SERVICE_TERMS = {
    "seo", "marketing", "digital marketing", "technical seo", "local seo",
    "web design", "ppc", "advertising", "content marketing", "link building",
    "google ads", "seo agency", "seo company", "search engine optimization",
    "website design", "web development",
    "plumber", "plumbing", "plumbing services", "water heater", "water heaters",
    "drain", "drains", "sewer", "sewers", "pipe", "pipes", "trenchless",
    "repiping", "repipe", "main line", "sewer main", "replacement", "repair",
    "tankless", "tankless water heater", "tankless water heaters"
}

BAD_PHRASE_WORDS = {
    "right", "doorstep", "find", "serving", "welcome", "official", "learn",
    "read", "click", "today", "now", "more", "watch", "news", "menu",
    "home", "contact", "account", "login", "sign", "handled", "cms"
}

BAD_START_WORDS = {
    "find", "serving", "welcome", "read", "click", "learn", "watch", "see"
}

BAD_END_WORDS = {
    "right", "doorstep", "today", "now", "more", "here", "there", "handled", "cms"
}


def sanitize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return url
    parsed = urlparse(url)
    if not parsed.scheme:
        return f"https://{url}"
    return url


def clean_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return url


def safe_logo_url():
    path = os.path.join(static_dir, "logo.png")
    return "/static/logo.png" if os.path.exists(path) else None


def length_status(length: int, min_len: int, max_len: int) -> str:
    if length == 0:
        return "Missing"
    if length < min_len:
        return "Too Short"
    if length > max_len:
        return "Too Long"
    return "Good"


def extract_visible_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "header", "footer"]):
        tag.decompose()
    return soup.get_text(" ", strip=True)


def tokenize(text: str):
    words = re.findall(r"\b[a-zA-Z]{3,}\b", text.lower())
    return [w for w in words if w not in STOP_WORDS]


def tokenize_phrase(text: str):
    return tokenize(text or "")


def build_top_terms(tokens, limit=30):
    counts = Counter(tokens)
    common = counts.most_common(limit)
    max_count = common[0][1] if common else 1

    terms = []
    for term, count in common:
        weight = max(1, int((count / max_count) * 5))
        terms.append({"term": term, "weight": weight, "count": count})
    return terms


def build_top_bigrams(tokens, limit=20):
    phrases = []
    for i in range(len(tokens) - 1):
        a, b = tokens[i], tokens[i + 1]
        if a in STOP_WORDS or b in STOP_WORDS:
            continue
        phrases.append(f"{a} {b}")

    counts = Counter(phrases)
    return [p for p, _ in counts.most_common(limit)]


def build_top_trigrams(tokens, limit=20):
    phrases = []
    for i in range(len(tokens) - 2):
        a, b, c = tokens[i], tokens[i + 1], tokens[i + 2]
        if a in STOP_WORDS or b in STOP_WORDS or c in STOP_WORDS:
            continue
        phrases.append(f"{a} {b} {c}")

    counts = Counter(phrases)
    return [p for p, _ in counts.most_common(limit)]


def make_ngrams_from_text(text: str, n: int):
    tokens = tokenize_phrase(text)
    if len(tokens) < n:
        return []
    return [" ".join(tokens[i:i + n]) for i in range(len(tokens) - n + 1)]


def normalize_phrase(phrase: str) -> str:
    return re.sub(r"\s+", " ", (phrase or "").strip().lower())


def phrase_has_service_term(phrase: str) -> bool:
    phrase = normalize_phrase(phrase)
    return any(term in phrase for term in SERVICE_TERMS)


def phrase_has_location_term(phrase: str) -> bool:
    phrase = normalize_phrase(phrase)
    return any(term in phrase for term in LOCATION_TERMS)


def phrase_has_commercial_term(phrase: str) -> bool:
    phrase = normalize_phrase(phrase)
    return any(term in phrase for term in COMMERCIAL_TERMS)


def contains_bad_word(phrase: str) -> bool:
    words = normalize_phrase(phrase).split()
    return any(w in BAD_PHRASE_WORDS for w in words)


def is_valid_phrase(phrase: str) -> bool:
    phrase = normalize_phrase(phrase)
    if not phrase:
        return False

    words = phrase.split()
    if len(words) < 2 or len(words) > 4:
        return False

    if all(w in STOP_WORDS for w in words):
        return False

    if words[0] in BAD_START_WORDS or words[-1] in BAD_END_WORDS:
        return False

    if contains_bad_word(phrase):
        allowed_exact = {
            "near me", "long island", "new york", "los angeles",
            "southern california", "orange county", "bay area", "san jose",
            "kansas city"
        }
        if phrase not in allowed_exact:
            return False

    if len("".join(words)) < 8:
        return False

    if sum(1 for w in words if w in STOP_WORDS) >= 2:
        return False

    return True


def is_human_sounding_keyword(phrase: str) -> bool:
    phrase = normalize_phrase(phrase)
    words = phrase.split()

    if not is_valid_phrase(phrase):
        return False

    has_service = phrase_has_service_term(phrase)
    has_location = phrase_has_location_term(phrase)
    has_commercial = phrase_has_commercial_term(phrase)

    if has_service and has_location:
        return True
    if has_service and has_commercial:
        return True
    if has_service and len(words) == 2:
        return True

    if has_location and not has_service and not has_commercial:
        return False

    if has_commercial and not has_service:
        return False

    return False


def phrase_intent_score(phrase: str) -> int:
    phrase = normalize_phrase(phrase)
    score = 0

    if phrase_has_location_term(phrase):
        score += 3
    if phrase_has_commercial_term(phrase):
        score += 3
    if phrase_has_service_term(phrase):
        score += 4

    words = phrase.split()
    if len(words) >= 2:
        score += 1
    if len(words) >= 3:
        score += 1

    return score


def get_priority_label(term: str) -> str:
    score = phrase_intent_score(term)
    if score >= 8:
        return "high"
    if score >= 5:
        return "medium"
    return "low"


def collect_weighted_keyword_candidates(
    title: str,
    meta: str,
    h1: str,
    h2_list,
    alt_texts,
    anchor_texts,
    body_bigrams,
    body_trigrams,
    top_single_terms
):
    weighted = Counter()

    def add_phrases(phrases, weight):
        for phrase in phrases:
            phrase = normalize_phrase(phrase)
            if not is_human_sounding_keyword(phrase):
                continue
            weighted[phrase] += weight + phrase_intent_score(phrase)

    add_phrases(make_ngrams_from_text(title, 2), 10)
    add_phrases(make_ngrams_from_text(title, 3), 12)

    add_phrases(make_ngrams_from_text(meta, 2), 8)
    add_phrases(make_ngrams_from_text(meta, 3), 10)

    add_phrases(make_ngrams_from_text(h1, 2), 11)
    add_phrases(make_ngrams_from_text(h1, 3), 13)

    for h2 in h2_list[:10]:
        add_phrases(make_ngrams_from_text(h2, 2), 5)
        add_phrases(make_ngrams_from_text(h2, 3), 7)

    for alt in alt_texts[:15]:
        add_phrases(make_ngrams_from_text(alt, 2), 2)
        add_phrases(make_ngrams_from_text(alt, 3), 3)

    for anchor in anchor_texts[:20]:
        add_phrases(make_ngrams_from_text(anchor, 2), 1)
        add_phrases(make_ngrams_from_text(anchor, 3), 2)

    add_phrases(body_bigrams[:12], 1)
    add_phrases(body_trigrams[:12], 2)

    for term in top_single_terms[:8]:
        term = normalize_phrase(term)
        if is_human_sounding_keyword(term) and phrase_intent_score(term) >= 5:
            weighted[term] += 1 + phrase_intent_score(term)

    return weighted


def classify_keyword(term: str) -> str:
    t = normalize_phrase(term)
    t_l = t.lower().strip()

    if not t_l:
        return "other"

    try:
        location_sets = get_location_sets()
        locations = location_sets.get("combined", [])
    except Exception:
        locations = []

    has_saved_location = any(
        loc and re.search(rf"\b{re.escape(str(loc).lower())}\b", t_l)
        for loc in locations
    )

    service_markers = [
        "roof", "roofing", "roofer", "roofers",
        "plumbing", "plumber", "plumbers",
        "hvac", "heating", "cooling",
        "repair", "replacement", "installation",
        "drain", "sewer", "cleaning", "restoration",
        "contractor", "contractors"
    ]

    commercial_markers = [
        "near me", "best", "top", "company", "companies",
        "cost", "price", "quote", "estimate",
        "emergency", "licensed", "insured"
    ]

    has_service = any(marker in t_l for marker in service_markers)
    has_commercial = any(marker in t_l for marker in commercial_markers)

    if has_saved_location and has_service:
        return "location"

    if phrase_has_location_term(t) and has_service:
        return "location"

    if has_commercial and has_service:
        return "commercial"

    if has_service:
        return "service"

    if has_saved_location:
        return "location"

    return "other"


def filter_by_volume(keywords, volume_data):
    """Keep only keywords with search volume"""
    valid = []

    for kw in keywords:
        data = volume_data.get(kw.lower()) if isinstance(volume_data, dict) else None
        vol = data.get("search_volume", 0) if data else 0

        if vol and vol > 0:
            valid.append(kw)

    return valid

def flatten_missing_gap_terms(gap, limit=30):
    terms = []

    if not isinstance(gap, dict):
        return terms

    grouped = gap.get("missing_grouped", {})
    for bucket in ("commercial", "location", "service"):
        for item in grouped.get(bucket, []):
            term = item.get("term") if isinstance(item, dict) else item
            if term and term not in terms:
                terms.append(term)

    return terms[:limit]


def enrich_keywords_with_dataforseo(keywords):
    if os.getenv("DATAFORSEO_ENABLED", "1").strip() != "1":
        print("DataForSEO disabled by DATAFORSEO_ENABLED.")
        return []

    login = os.getenv("DATAFORSEO_LOGIN")
    password = os.getenv("DATAFORSEO_PASSWORD")

    if not login or not password or not keywords:
        return []

    clean_keywords = []
    for kw in keywords:
        kw = normalize_phrase(kw)
        if kw and len(kw) <= 80 and len(kw.split()) <= 10 and kw not in clean_keywords:
            clean_keywords.append(kw)

    if not clean_keywords:
        return []

    auth = base64.b64encode(f"{login}:{password}".encode("utf-8")).decode("utf-8")

    payload = [{
        "location_code": int(os.getenv("DATAFORSEO_LOCATION_CODE", "2840")),
        "language_code": os.getenv("DATAFORSEO_LANGUAGE_CODE", "en"),
        "keywords": clean_keywords[:50],
    }]

    try:
        response = requests.post(
            "https://api.dataforseo.com/v3/keywords_data/google_ads/search_volume/live",
            json=payload,
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/json",
            },
            timeout=(5, 12),
        )
        data = response.json()
    except Exception as e:
        print("DataForSEO request failed:", e)
        return []

    items = []

    tasks = data.get("tasks") or []
    if not isinstance(tasks, list):
        print("DataForSEO unexpected tasks format:", data)
        return []

    for task in tasks:
        if not isinstance(task, dict):
            continue

        results = task.get("result") or []
        if not isinstance(results, list):
            continue

        for result in results:
            if not isinstance(result, dict):
                continue

            keyword = result.get("keyword")
            if not keyword:
                continue

            items.append({
                "keyword": keyword,
                "volume": result.get("search_volume") or 0,
                "cpc": result.get("cpc") or 0,
                "competition": result.get("competition") or "—",
                "competition_index": result.get("competition_index") or 0,
                "low_bid": result.get("low_top_of_page_bid") or 0,
                "high_bid": result.get("high_top_of_page_bid") or 0,
            })

    items.sort(
        key=lambda x: (
            x.get("volume") or 0,
            x.get("cpc") or 0,
            x.get("competition_index") or 0
        ),
        reverse=True
    )

    return items[:10]

def keyword_strategy_recommendation(keyword: str) -> dict:
    kw = normalize_phrase(keyword)

    page_terms = [
        "near me", "long island", "new york", "nyc", "brooklyn", "queens",
        "nassau", "suffolk", "local"
    ]

    emergency_terms = [
        "emergency", "24 hour", "same day", "urgent"
    ]

    repair_terms = [
        "repair", "replacement", "install", "installation", "service", "services"
    ]

    cost_terms = [
        "cost", "price", "pricing", "quote", "estimate", "affordable"
    ]

    if any(term in kw for term in emergency_terms):
        return {
            "type": "Emergency / urgent-intent keyword",
            "action": "Treat this as a high-priority service section or dedicated emergency page. These terms usually signal immediate buying intent, so the page should answer availability, service area, trust, and contact path quickly."
        }

    if any(term in kw for term in cost_terms):
        return {
            "type": "Cost / quote-intent keyword",
            "action": "Use this in an FAQ or pricing-explainer section. Do not promise exact pricing unless the business supports it; explain what affects cost and push users toward a quote."
        }

    if phrase_has_location_term(kw) and phrase_has_service_term(kw):
        return {
            "type": "Service + location keyword",
            "action": "This is usually a strong landing page or service-area section candidate. Build content around the service, the location, proof points, FAQs, and internal links from related pages."
        }

    if any(term in kw for term in page_terms):
        return {
            "type": "Local-intent keyword",
            "action": "Use this to strengthen local relevance. Add it naturally to headings, intro copy, service-area copy, internal links, and supporting location content."
        }

    if any(term in kw for term in repair_terms):
        return {
            "type": "Service-intent keyword",
            "action": "Use this to expand the service content. Add a clear section explaining the service, when users need it, common problems, and next-step CTAs."
        }

    if phrase_has_service_term(kw):
        return {
            "type": "Core service keyword",
            "action": "Use this as supporting topical coverage. It may not need its own page, but it should appear naturally in headings, body copy, internal links, or FAQs."
        }

    return {
        "type": "Supporting keyword",
        "action": "Use this carefully as supporting language only. It should not drive the page strategy unless it matches the actual service and search intent."
    }


def build_volume_opportunity_summary(volume_data):
    if not volume_data:
        return ""

    top = volume_data[0]
    top_keyword = top.get("keyword") or ""
    top_volume = top.get("volume") or 0
    top_cpc = top.get("cpc") or 0
    top_competition = top.get("competition") or "—"

    top_strategy = keyword_strategy_recommendation(top_keyword)

    rows = []
    for item in volume_data[:6]:
        keyword = item.get("keyword") or ""
        volume = item.get("volume") or 0
        cpc = item.get("cpc") or 0
        competition = item.get("competition") or "—"
        strategy = keyword_strategy_recommendation(keyword)

        rows.append(f"""
            <li>
                <strong>{keyword}</strong> — {volume:,} searches/month
                {f", CPC ${cpc:.2f}" if cpc else ""}
                {f", competition {competition}" if competition else ""}
                <br>
                <em>{strategy["type"]}:</em> {strategy["action"]}
            </li>
        """)

    return f"""
    <h4>Volume-Backed SEO Opportunity</h4>

    <li><strong>Top opportunity:</strong> <strong>{top_keyword}</strong> has about
    <strong>{top_volume:,}</strong> monthly searches{f" and an estimated CPC of <strong>${top_cpc:.2f}</strong>" if top_cpc else ""}.
    This makes it the strongest measurable keyword gap in this comparison.</li>

    <li><strong>Recommended use:</strong> {top_strategy["action"]}</li>

    <li><strong>Why this matters:</strong> This moves the report beyond keyword matching.
    The priority is no longer just “add missing keywords.” The priority is deciding which gaps deserve page-level attention,
    which belong in sections or FAQs, and which should support internal linking.</li>

    <h4>How to Use the Highest-Value Gaps</h4>
    <ul>
        {''.join(rows)}
    </ul>
    """

def fetch_page_data(url: str):
    url = sanitize_url(url)

    data = {
        "url": url,
        "domain": clean_domain(url),
        "clean_domain": clean_domain(url),
        "title": "",
        "meta_description": "",
        "h1": "",
        "keywords": [],
        "top_terms": [],
        "word_count": 0,
        "has_meta": False,
        "title_length": 0,
        "meta_length": 0,
        "title_status": "Missing",
        "meta_status": "Missing",
        "has_h1": False,
        "h1_count": 0,
        "h2_count": 0,
        "internal_link_count": 0,
        "canonical": "None",
        "image_count": 0,
        "alt_count": 0,
        "error": None,
        "score": 0,
    }

    try:
        r = crawl_get(
            url,
            timeout=(5, 12),
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            },
            allow_redirects=True,
            verify=False,
        )
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        title = soup.title.string.strip() if soup.title and soup.title.string else ""

        meta_tag = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
        meta = meta_tag["content"].strip() if meta_tag and meta_tag.get("content") else ""

        geo_meta = {}
        for tag in soup.find_all("meta"):
            name = (tag.get("name") or tag.get("property") or "").lower()
            content = (tag.get("content") or "").strip()

            if name in [
                "geo.region",
                "geo.placename",
                "geo.position",
                "icbm",
                "location",
                "geo.country",
            ]:
                geo_meta[name] = content

        h1_tags = soup.find_all("h1")
        h2_tags = soup.find_all("h2")
        h1 = h1_tags[0].get_text(strip=True) if h1_tags else ""
        h2_texts = [tag.get_text(" ", strip=True) for tag in h2_tags if tag.get_text(" ", strip=True)]

        canonical_tag = soup.find("link", attrs={"rel": lambda x: x and "canonical" in x})
        canonical = canonical_tag.get("href", "").strip() if canonical_tag else "None"

        parsed_url = urlparse(url)
        base_domain = parsed_url.netloc.replace("www.", "")

        internal_link_count = 0
        anchor_texts = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            anchor_text = a.get_text(" ", strip=True)
            if anchor_text:
                anchor_texts.append(anchor_text)
            if href.startswith("/") or base_domain in href:
                internal_link_count += 1

        images = soup.find_all("img")
        image_count = len(images)
        alt_texts = [img.get("alt", "").strip() for img in images if img.get("alt", "").strip()]
        alt_count = len(alt_texts)

        text = extract_visible_text(soup)
        tokens = tokenize(text)

        keyword_source_text = " ".join([
            title or "",
            meta or "",
            h1 or "",
            " ".join(h2_texts) if isinstance(h2_texts, list) else "",
            text or "",
        ])
        keyword_tokens = tokenize(keyword_source_text)
        geo_phrases = []
        geo_source_text = " ".join([
            title or "",
            meta or "",
            h1 or "",
            text[:3000] if text else "",
        ])

        for match in re.findall(r"\b[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){0,2},\s*[A-Z]{2}\b", geo_source_text):
            if match not in geo_phrases:
                geo_phrases.append(match)

        for match in re.findall(r"\b[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){0,2}\s+(?:County|City|Township|Borough)\b", geo_source_text):
            if match not in geo_phrases:
                geo_phrases.append(match)

        # Also detect saved /settings locations that appear in title, meta, H1, or body.
        try:
            saved_locations = get_location_sets().get("combined", [])
        except Exception:
            saved_locations = []

        geo_source_l = geo_source_text.lower()

        for loc in saved_locations:
            loc_l = str(loc).lower().strip()
            if not loc_l or len(loc_l) < 3:
                continue

            if loc_l in BAD_LOCATION_TERMS:
                continue

            if re.search(rf"\b{re.escape(loc_l)}\b", geo_source_l):
                if loc_l not in [g.lower() for g in geo_phrases]:
                    geo_phrases.append(loc_l)


        top_terms = build_top_terms(keyword_tokens, 30)
        top_bigrams = build_top_bigrams(keyword_tokens, 20)
        top_trigrams = build_top_trigrams(keyword_tokens, 20)
        single_terms = [t["term"] for t in top_terms[:10]]

        weighted_candidates = collect_weighted_keyword_candidates(
            title=title,
            meta=meta,
            h1=h1,
            h2_list=h2_texts,
            alt_texts=alt_texts,
            anchor_texts=anchor_texts,
            body_bigrams=top_bigrams,
            body_trigrams=top_trigrams,
            top_single_terms=single_terms
        )

        keywords = [term for term, _ in weighted_candidates.most_common(28)]

        # Fallback: if candidate logic returns empty, pull phrases directly from title/meta/H1/H2.
        if not keywords:
            fallback_phrases = []
            fallback_text = " ".join([
                title or "",
                meta or "",
                h1 or "",
                " ".join(h2_texts) if isinstance(h2_texts, list) else "",
            ])

            fallback_tokens = tokenize(fallback_text)
            fallback_bigrams = build_top_bigrams(fallback_tokens, 20)
            fallback_trigrams = build_top_trigrams(fallback_tokens, 20)

            for item in fallback_trigrams + fallback_bigrams:
                term = item.get("term") if isinstance(item, dict) else str(item)
                if term and term not in fallback_phrases:
                    fallback_phrases.append(term)

            bad_words = {
                "babe", "roof babe", "babe roof babe", "roof babe roof",
                "years experience", "professionals years", "professionals years experience"
            }

            cleaned = []
            for phrase in fallback_phrases:
                phrase_l = phrase.lower().strip()

                if any(bad in phrase_l for bad in bad_words):
                    continue

                if phrase_l.count("roof") > 1:
                    continue

                if phrase_l not in cleaned:
                    cleaned.append(phrase_l)

            keywords = cleaned[:28]

        data.update({
            "title": title,
            "meta_description": meta if meta else "No meta description",
            "geo_meta": geo_meta,
            "geo_phrases": geo_phrases,
            "visible_text": text,
            "page_text": text,
            "body_text": text,
            "all_text": text,
            "h1": h1,
            "keywords": keywords,
            "top_terms": top_terms[:20],
            "word_count": len(tokens),
            "has_meta": bool(meta),
            "title_length": len(title),
            "meta_length": len(meta),
            "title_status": length_status(len(title), 30, 65),
            "meta_status": length_status(len(meta), 70, 160),
            "has_h1": bool(h1),
            "h1_count": len(h1_tags),
            "h1_status": "Good" if len(h1_tags) == 1 else ("Needs Improvement" if len(h1_tags) > 1 else "Missing"),
            "h2_count": len(h2_tags),
            "internal_link_count": internal_link_count,
            "canonical": canonical,
            "image_count": image_count,
            "alt_count": alt_count,
        })
        return data

    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code if e.response is not None else None
        data["error"] = str(e)

        if status_code == 403:
            data["title"] = "Blocked by site"
            data["meta_description"] = "Blocked by site"
            data["title_status"] = "Blocked"
            data["meta_status"] = "Blocked"
        else:
            data["title"] = "Unable to fetch"
            data["meta_description"] = "Unable to fetch"
            data["title_status"] = "Unavailable"
            data["meta_status"] = "Unavailable"

        return data

    except Exception as e:
        data["error"] = str(e)
        data["title"] = "Unable to fetch"
        data["meta_description"] = "Unable to fetch"
        data["title_status"] = "Unavailable"
        data["meta_status"] = "Unavailable"
        return data


def score_page(d):
    breakdown = {
        "Title Tag": 0,
        "Meta Description": 0,
        "H1": 0,
        "Keyword Coverage": 0,
        "Content Depth": 0,
        "Internal Links": 0,
        "Image Alt Coverage": 0,
        "Total": 0,
    }

    if d["title"]:
        breakdown["Title Tag"] += 20
        if 30 <= len(d["title"]) <= 65:
            breakdown["Title Tag"] += 5

    if d["meta_description"] and d["meta_description"] not in {"ERROR", "No meta description"}:
        breakdown["Meta Description"] += 20
        if 70 <= d["meta_length"] <= 160:
            breakdown["Meta Description"] += 5

    if d["h1"]:
        breakdown["H1"] += 15

    if d["keywords"]:
        breakdown["Keyword Coverage"] += 15

    if d["word_count"] > 300:
        breakdown["Content Depth"] += 10
    if d["word_count"] > 800:
        breakdown["Content Depth"] += 10

    if d["internal_link_count"] >= 5:
        breakdown["Internal Links"] += 10

    if d["image_count"] > 0:
        alt_ratio = d["alt_count"] / max(d["image_count"], 1)
        if alt_ratio >= 0.8:
            breakdown["Image Alt Coverage"] += 10
        elif alt_ratio >= 0.4:
            breakdown["Image Alt Coverage"] += 5

    total = sum(v for k, v in breakdown.items() if k != "Total")
    breakdown["Total"] = min(total, 100)
    return min(total, 100), breakdown


def filter_relevant_keywords(keywords, site):
    """Remove irrelevant keywords based on site topic"""
    base_text = (
        (site.get("title") or "") + " " +
        (site.get("h1") or "")
    ).lower()

    base_words = set(base_text.split())

    filtered = []

    for kw in keywords:
        kw_words = set(kw.lower().split())

        # Keep if overlap OR contains core words
        if base_words & kw_words:
            filtered.append(kw)
        else:
            # allow partial match (e.g. "roof repair" vs "roofing")
            if any(word in base_text for word in kw_words):
                filtered.append(kw)

    return filtered

def keyword_gap(site, competitors):
    your_keywords = set(site.get("keywords", []))
    competitor_keywords = set()

    for comp in competitors:
        competitor_keywords.update(comp.get("keywords", []))

    missing = [kw for kw in competitor_keywords if kw not in your_keywords]
    shared = [kw for kw in your_keywords if kw in competitor_keywords]

    grouped = {
        "service": [],
        "location": [],
        "commercial": [],
    }

    for kw in missing:
        if not is_human_sounding_keyword(kw):
            continue
        bucket = classify_keyword(kw)
        grouped.setdefault(bucket, []).append({
            "term": kw,
            "priority": get_priority_label(kw)
        })

    for bucket in grouped:
        grouped[bucket].sort(
            key=lambda x: (phrase_intent_score(x["term"]), len(x["term"].split())),
            reverse=True
        )

    shared_clean = []
    for kw in shared:
        if is_human_sounding_keyword(kw):
            shared_clean.append({"term": kw, "priority": "shared"})

    return {
        "missing_grouped": {
            "service": grouped["service"][:10],
            "location": grouped["location"][:10],
            "commercial": grouped["commercial"][:10],
        },
        "shared": shared_clean[:12],
    }


def get_priority_missing_terms(site, competitor, limit=5):
    site_keywords = set(site.get("keywords", []))
    competitor_keywords = competitor.get("keywords", [])
    missing = [kw for kw in competitor_keywords if kw not in site_keywords and is_human_sounding_keyword(kw)]

    scored = []
    for kw in missing:
        scored.append((kw, phrase_intent_score(kw), len(kw.split())))

    scored.sort(key=lambda x: (x[1], x[2], len(x[0])), reverse=True)
    return [kw for kw, _, _ in scored[:limit]]


def pick_best_term(site, competitor):
    terms = get_priority_missing_terms(site, competitor, limit=5)
    return terms[0] if terms else None


def build_quick_wins(site, competitor):
    items = []
    best_term = pick_best_term(site, competitor)
    priority_terms = get_priority_missing_terms(site, competitor, limit=3)

    if not site.get("title"):
        if best_term:
            items.append(f"Add a 30–65 character title tag targeting '{best_term}'.")
        else:
            items.append("Add a 30–65 character title tag aligned with the main service query.")
    elif site.get("title_length", 0) < 30:
        if best_term:
            items.append(f"Expand the title tag to 30–65 characters and work in '{best_term}'.")
        else:
            items.append("Expand the title tag to 30–65 characters.")
    elif site.get("title_length", 0) > 65:
        if best_term:
            items.append(f"Tighten the title tag to 30–65 characters while keeping '{best_term}' prominent.")
        else:
            items.append("Tighten the title tag to 30–65 characters for cleaner SERP display.")

    if not site.get("has_meta"):
        if best_term:
            items.append(f"Add a 120–155 character meta description targeting '{best_term}'.")
        else:
            items.append("Add a 120–155 character meta description aligned with your primary keyword.")
    elif site.get("meta_length", 0) < 70:
        if best_term:
            items.append(f"Expand meta description to 120–155 characters and include '{best_term}'.")
        else:
            items.append("Expand meta description to 120–155 characters.")
    elif site.get("meta_length", 0) > 160:
        if best_term:
            items.append(f"Trim meta description to ~120–155 characters while keeping '{best_term}'.")
        else:
            items.append("Trim meta description to ~120–155 characters.")

    if not site.get("has_h1"):
        if best_term:
            items.append(f"Add an H1 that clearly targets '{best_term}'.")
        else:
            items.append("Add a clear H1 tied to the page’s main query.")
    elif site.get("h1_count", 0) > 1:
        items.append("Reduce H1 usage so the page has one primary heading and a clearer hierarchy.")
    else:
        site_h1 = normalize_phrase(site.get("h1") or "")
        if best_term and best_term not in site_h1:
            items.append(f"Strengthen H1 alignment by working '{best_term}' into the heading or opening section.")

    if site.get("word_count", 0) < competitor.get("word_count", 0):
        if priority_terms:
            items.append(
                f"Increase content depth and work in missing phrases such as: {', '.join(priority_terms)}."
            )
        else:
            items.append("Increase content depth to better match the competitor’s topical coverage.")

    if site.get("internal_link_count", 0) < 5:
        if best_term:
            items.append(f"Add more internal links using supporting anchor text related to '{best_term}'.")
        else:
            items.append("Add more internal links to strengthen page support and crawl paths.")

    if site.get("image_count", 0) > 0 and site.get("alt_count", 0) < site.get("image_count", 0):
        if best_term:
            items.append(f"Add missing alt text and, where relevant, reinforce phrases like '{best_term}'.")
        else:
            items.append("Add missing alt text to strengthen image optimization.")

    if priority_terms and len(items) < 6:
        items.append(f"Work missing phrases into copy, such as: {', '.join(priority_terms)}.")

    if not items:
        items.append("Page is fairly competitive. Focus on tighter phrasing and stronger intent match.")

    return items[:6]


def build_section_card(site):
    return {
        "domain": site.get("clean_domain", ""),
        "score": site.get("score", 0),
        "word_count": site.get("word_count", 0),
        "h1_count": site.get("h1_count", 0),
        "h2_count": site.get("h2_count", 0),
        "internal_link_count": site.get("internal_link_count", 0),
        "canonical": site.get("canonical", "None"),
        "image_count": site.get("image_count", 0),
        "alt_count": site.get("alt_count", 0),
        "title_length": site.get("title_length", 0),
        "meta_length": site.get("meta_length", 0),
        "title_status": site.get("title_status", "Missing"),
        "meta_status": site.get("meta_status", "Missing"),
    }


def generate_title(site):
    base = (site.get("title") or "").strip()
    h1 = (site.get("h1") or "").strip()

    keyword = h1 if h1 else base

    # simple cleanup
    keyword = keyword.replace("|", "").replace("-", "").strip()

    return f"{keyword} | Local Service & Fast Response"[:60]


def generate_meta(site):
    base = (site.get("meta") or site.get("description") or "").strip()
    h1 = (site.get("h1") or "").strip()

    keyword = h1 if h1 else base[:40]

    return f"Looking for {keyword}? Fast service, trusted experts, and reliable results. Call today."[:155]


def generate_section_ideas(site, missing_phrases=None):
    title = (site.get("title") or "").strip()
    h1 = (site.get("h1") or "").strip()
    base = h1 or title or "Main Service"

    phrases = missing_phrases or []
    ideas = []

    for phrase in phrases[:3]:
        ideas.append(f"{phrase.title()} Services")
        ideas.append(f"Why Choose Us for {phrase.title()}")

    ideas.extend([
        f"{base} FAQs",
        f"Why Choose Our Team",
        f"Service Areas and Local Expertise",
    ])

    clean = []
    seen = set()
    for idea in ideas:
        key = idea.lower()
        if key not in seen:
            seen.add(key)
            clean.append(idea)

    return clean[:5]


def build_final_strategy_summary(site, competitors, gap):
    competitor = competitors[0] if competitors else {}

    site_score = site.get("score", 0)
    comp_score = competitor.get("score", 0)
    score_gap = comp_score - site_score

    missing_phrases = []
    if isinstance(gap, dict):
        missing_phrases = filter_relevant_keywords(gap.get("missing_phrases", []), site) or gap.get("competitor_only") or gap.get("missing") or []
    elif isinstance(gap, list):
        missing_phrases = filter_relevant_keywords(gap, site)

    missing_phrases = missing_phrases[:8]

    title = site.get("title", "") or ""
    meta = site.get("meta", "") or site.get("description", "") or ""
    word_count = site.get("word_count", 0) or 0

    actions = []

    if competitor.get("weak_data"):
        actions.append("Use a stronger competitor URL before relying on keyword gaps or score comparisons.")
        actions.append("Pick a live service/location page with real content, headings, links, and indexable copy.")
    elif score_gap > 0:
        actions.append(f"Close the {score_gap}-point score gap by improving title targeting, content depth, and internal linking.")
    else:
        actions.append("Maintain the score advantage by tightening keyword targeting and adding stronger supporting sections.")

    if len(title) < 30 or len(title) > 65:
        actions.append(f"Rewrite the title to 50–60 characters with the main service and location. Current title length: {len(title)}.")

    if len(meta) < 120 or len(meta) > 155:
        actions.append(f"Rewrite the meta description to about 140–155 characters with service, benefit, and location. Current meta length: {len(meta)}.")

    if word_count and word_count < 700:
        actions.append(f"Expand the page to roughly 800–1200 words with service details, FAQs, trust proof, and local relevance. Current word count: {word_count}.")

    if missing_phrases and not competitor.get("weak_data"):
        actions.append(f"Add content sections or FAQ answers around: {', '.join(missing_phrases[:3])}.")

    if not actions:
        actions.append("No major structural issue was detected. Focus next on content quality, internal links, and conversion copy.")

    missing_html = (
        "<ul>" + "".join(f"<li>{phrase}</li>" for phrase in missing_phrases) + "</ul>"
        if missing_phrases and not competitor.get("weak_data")
        else "<p>Review the extracted keyword opportunities in the Keyword Opportunities section above and prioritize the most relevant service, location, and commercial terms.</p>"
    )

        # Generate actual fixes
    suggested_title = generate_title(site)
    suggested_meta = generate_meta(site)

    action_html = "".join(f"<li>{action}</li>" for action in actions)

    fixes_html = f"""
    <h4>Suggested Fixes</h4>
    <ul>
        <li><strong>Suggested title:</strong> {suggested_title}</li>
        <li><strong>Suggested meta:</strong> {suggested_meta}</li>
    </ul>
    <h4>Suggested Page Sections</h4>
    <ul>
        {"".join([f"<li>{section}</li>" for section in generate_section_ideas(site, missing_phrases)])}
    </ul>
    """


    return f"""
    <div class="final-summary-box">
        <h3>Final SEO Strategy Summary</h3>

        <p><strong>Summary:</strong> This report should be used to decide the next SEO actions, not just compare scores.</p>

        <h4>Recommended Next Steps</h4>
        <ol>
            {action_html}
        </ol>

        <h4>Keyword Opportunities</h4>
        {missing_html}
{fixes_html}

        <p><strong>Takeaway:</strong> Prioritize fixes that directly improve targeting, depth, and trust signals before chasing broader SEO work.</p>
    </div>
    """


def reports_dir_path() -> str:
    path = os.path.join(os.path.dirname(BASE_DIR), "reports")
    os.makedirs(path, exist_ok=True)
    os.makedirs(os.path.join(path, "saved"), exist_ok=True)
    return path


def history_file_path() -> str:
    return os.path.join(reports_dir_path(), "history.json")


def slugify_report_part(text: str) -> str:
    text = re.sub(r"^https?://", "", text or "")
    text = text.replace("www.", "")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:60] or "report"


def load_report_history():
    path = history_file_path()
    if not os.path.exists(path):
        return []

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_report_history_item(item: dict):
    history = load_report_history()
    history.insert(0, item)
    history = history[:100]

    with open(history_file_path(), "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)


def save_report_snapshot(html: str, site: dict, competitor: dict, gap: dict, volume_data: list):
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    site_slug = slugify_report_part(site.get("clean_domain", "site"))
    comp_slug = slugify_report_part(competitor.get("clean_domain", "competitor"))

    filename = f"{timestamp}_{site_slug}_vs_{comp_slug}.html"
    saved_dir = os.path.join(reports_dir_path(), "saved")
    file_path = os.path.join(saved_dir, filename)

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(html)

    missing_terms = []
    if isinstance(gap, dict):
        grouped = gap.get("missing_grouped", {})
        for bucket in ("commercial", "location", "service"):
            for item in grouped.get(bucket, []):
                term = item.get("term") if isinstance(item, dict) else item
                if term and term not in missing_terms:
                    missing_terms.append(term)

    history_item = {
        "date": datetime.now().strftime("%B %d, %Y %I:%M %p"),
        "timestamp": timestamp,
        "site_url": site.get("url", ""),
        "competitor_url": competitor.get("url", ""),
        "site_domain": site.get("clean_domain", ""),
        "competitor_domain": competitor.get("clean_domain", ""),
        "site_score": site.get("score", 0),
        "competitor_score": competitor.get("score", 0),
        "score_difference": site.get("score", 0) - competitor.get("score", 0),
        "top_gaps": missing_terms[:8],
        "volume_opportunities": volume_data[:8] if volume_data else [],
        "saved_report": f"/reports/saved/{filename}",
        "saved_path": file_path,
    }

    save_report_history_item(history_item)
    return history_item

def export_pdf(html: str) -> str:
    file_path = os.path.join(
        tempfile.gettempdir(),
        f"seo_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    )
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(html)
    return file_path


# === FINAL SCORE CAPS + GEO MISMATCH PATCH ===

BAD_LOCATION_TERMS = {
    "local", "nearby", "location", "locations", "service", "services",
    "image", "total", "scroll back up"
}

KNOWN_LOCAL_MARKETS = {
    "long island", "nassau", "suffolk", "bellmore", "east meadow", "hicksville",
    "kansas city", "lake of the ozarks", "ozarks", "missouri", "kansas",
    "los angeles", "southern california", "california", "new york"
}

def _clean_geo_term(term):
    term = str(term or "").strip().lower()
    term = term.replace("|", " ").replace(",", " ")
    term = " ".join(term.split())
    if not term or term in BAD_LOCATION_TERMS:
        return ""
    if len(term) < 3:
        return ""
    return term

def extract_geo_terms_for_report(page):
    found = set()

    if not isinstance(page, dict):
        return found

    for key in ("geo_phrases", "locations", "detected_locations", "location_keywords"):
        values = page.get(key)
        if isinstance(values, (list, tuple, set)):
            for item in values:
                cleaned = _clean_geo_term(item)
                if cleaned:
                    found.add(cleaned)

    combined = " ".join([
        str(page.get("title", "") or ""),
        str(page.get("meta", "") or ""),
        str(page.get("description", "") or ""),
        str(page.get("h1", "") or ""),
        str(page.get("clean_domain", "") or ""),
        " ".join(page.get("keywords", []) if isinstance(page.get("keywords"), list) else []),
    ]).lower()

    for market in KNOWN_LOCAL_MARKETS:
        if market in combined:
            found.add(market)

    return {x for x in found if _clean_geo_term(x)}

def apply_score_caps(page, score):
    try:
        score = int(score or 0)
    except Exception:
        score = 0

    if not isinstance(page, dict):
        return score

    title = str(page.get("title", "") or "")
    h1 = str(page.get("h1", "") or "")

    try:
        h1_count = int(page.get("h1_count", 0) or 0)
    except Exception:
        h1_count = 0

    try:
        word_count = int(page.get("word_count", 0) or 0)
    except Exception:
        word_count = 0

    if title and len(title) > 65:
        score = min(score, 95)

    if not h1 or h1_count == 0:
        score = min(score, 85)

    if word_count and word_count < 100:
        score = min(score, 75)

    if page.get("weak_data"):
        score = min(score, 85)

    if page.get("location_mismatch"):
        score = min(score, 85)

    return score

def apply_geo_mismatch_safety(site, competitor):
    if not isinstance(site, dict) or not isinstance(competitor, dict):
        return competitor

    site_geos = extract_geo_terms_for_report(site)
    comp_geos = extract_geo_terms_for_report(competitor)

    if site_geos and comp_geos and not (site_geos & comp_geos):
        competitor["location_mismatch"] = True
        competitor["weak_data"] = True
        competitor["weak_data_note"] = (
            "The selected competitor appears to target a different geographic market. "
            "Use this comparison for general page structure only, not final local keyword strategy."
        )
        competitor["site_detected_locations"] = sorted(site_geos)
        competitor["competitor_detected_locations"] = sorted(comp_geos)

    return competitor

def build_geo_mismatch_warning(site, competitor):
    if not isinstance(competitor, dict) or not competitor.get("location_mismatch"):
        return ""

    site_geos = competitor.get("site_detected_locations") or []
    comp_geos = competitor.get("competitor_detected_locations") or []

    site_text = ", ".join(site_geos[:8]) if site_geos else "Not enough location data detected"
    comp_text = ", ".join(comp_geos[:8]) if comp_geos else "Not enough location data detected"

    return f"""
    <div class="section">
        <div class="report-box" style="border-left:6px solid #f59e0b;background:#fffbeb;">
            <h3>Competitor Location Warning</h3>
            <p><strong>Important:</strong> The selected competitor appears to target a different geographic market.</p>
            <p><strong>Your detected locations:</strong> {site_text}</p>
            <p><strong>Competitor detected locations:</strong> {comp_text}</p>
            <p>Use this comparison for page structure, content depth, and technical SEO only. Do not use the competitor’s location keywords as final local SEO recommendations.</p>
        </div>
    </div>
    """

def remove_mismatched_geo_keywords_from_gap(gap, site, competitor):
    if not isinstance(gap, dict) or not isinstance(competitor, dict):
        return gap

    if not competitor.get("location_mismatch"):
        return gap

    site_geos = extract_geo_terms_for_report(site)
    comp_geos = extract_geo_terms_for_report(competitor)
    blocked_geos = {g for g in comp_geos if g not in site_geos}

    if not blocked_geos:
        return gap

    def bad_term(value):
        s = str(value or "").lower()
        return any(bg in s for bg in blocked_geos)

    def clean_list(items):
        cleaned = []
        for item in items:
            if isinstance(item, str):
                if not bad_term(item):
                    cleaned.append(item)
            elif isinstance(item, dict):
                joined = " ".join(str(v or "") for v in item.values()).lower()
                if not any(bg in joined for bg in blocked_geos):
                    cleaned.append(item)
            else:
                cleaned.append(item)
        return cleaned

    for key, value in list(gap.items()):
        if isinstance(value, list):
            gap[key] = clean_list(value)
        elif isinstance(value, dict):
            for subkey, subvalue in list(value.items()):
                if isinstance(subvalue, list):
                    value[subkey] = clean_list(subvalue)

    return gap


@app.get("/api/find-competitors")
def api_find_competitors(your_site: str, service: str, location: str = ""):
    return find_competitors(your_site=your_site, service=service, location=location)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request,
            "logo_url": safe_logo_url(),
            "user": auth_current_user(request),
        },
    )




# === LEADBOT DATAFORSEO SETTINGS SWITCH START ===

def leadbot_get_dataforseo_enabled():
    import os
    return os.getenv("LEADBOT_DATAFORSEO_ENABLED", "0").strip() == "1"


def leadbot_set_dataforseo_enabled(enabled: bool):
    import os
    from pathlib import Path

    env_path = Path(__file__).resolve().parents[1] / ".env"
    value = "1" if enabled else "0"

    env_text = env_path.read_text(encoding="utf-8", errors="ignore") if env_path.exists() else ""
    lines = env_text.splitlines()

    out = []
    found = False

    for line in lines:
        if line.strip().startswith("LEADBOT_DATAFORSEO_ENABLED="):
            out.append(f"LEADBOT_DATAFORSEO_ENABLED={value}")
            found = True
        else:
            out.append(line)

    if not found:
        if out and out[-1].strip():
            out.append("")
        out.append(f"LEADBOT_DATAFORSEO_ENABLED={value}")

    env_path.write_text(chr(10).join(out) + chr(10), encoding="utf-8")
    os.environ["LEADBOT_DATAFORSEO_ENABLED"] = value
    return value

# === LEADBOT DATAFORSEO SETTINGS SWITCH END ===

def _admin_role_from_user(user):
    if isinstance(user, dict):
        return str(user.get("role") or "").strip().lower()
    return str(getattr(user, "role", "") or "").strip().lower()


def _admin_only_response(request: Request):
    from fastapi.responses import RedirectResponse

    user = auth_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    if _admin_role_from_user(user) != "admin":
        return HTMLResponse(
            "<h1>Admin required</h1><p>This page is admin-only.</p>",
            status_code=403,
        )

    return None


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    admin_block = _admin_only_response(request)
    if admin_block:
        return admin_block

    location_sets = get_location_sets()

    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "request": request,
            "locations": "",
            "location_list": location_sets["combined"],
            "built_in_location_list": location_sets["built_in"],
            "custom_location_list": location_sets["custom"],
            "saved": False,
            "leadbot_dataforseo_enabled": leadbot_get_dataforseo_enabled(),
        },
    )

@app.post("/settings/leadbot-dataforseo", response_class=HTMLResponse)
async def leadbot_dataforseo_settings_switch(request: Request, enabled: str = Form("0")):
    from fastapi.responses import RedirectResponse

    admin_block = _admin_only_response(request)
    if admin_block:
        return admin_block

    value = leadbot_set_dataforseo_enabled(str(enabled).strip() == "1")

    print(
        f"LEADBOT DATAFORSEO SETTINGS SWITCH: LEADBOT_DATAFORSEO_ENABLED={value}",
        flush=True,
    )

    return RedirectResponse(url="/settings", status_code=303)


@app.post("/save-settings", response_class=HTMLResponse)
async def save_settings(request: Request, locations: str = Form(...)):
    admin_block = _admin_only_response(request)
    if admin_block:
        return admin_block

    file_path = location_terms_file_path()

    cleaned_list = normalize_term_list(locations.splitlines())
    cleaned_text = "\n".join(cleaned_list)

    with open(file_path, "w", encoding="utf-8") as f:
        if cleaned_text:
            f.write(cleaned_text + "\n")
        else:
            f.write("")

    global LOCATION_TERMS
    LOCATION_TERMS = BASE_LOCATION_TERMS.union(load_custom_location_terms())

    location_sets = get_location_sets()

    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "request": request,
            "locations": "",
            "location_list": location_sets["combined"],
            "built_in_location_list": location_sets["built_in"],
            "custom_location_list": location_sets["custom"],
            "saved": True,
            "leadbot_dataforseo_enabled": leadbot_get_dataforseo_enabled(),
        },
    )


@app.post("/history/delete")
async def delete_saved_report(index: int = Form(...)):
    history = load_report_history()

    if 0 <= index < len(history):
        item = history.pop(index)

        try:
            saved_report = item.get("saved_report", "")
            filename = os.path.basename(saved_report)
            saved_path = os.path.join(reports_dir_path(), "saved", filename)

            if os.path.isfile(saved_path):
                os.remove(saved_path)
        except Exception as e:
            print("Saved report delete failed:", e)

        with open(history_file_path(), "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)

    return RedirectResponse(url="/history", status_code=303)


@app.get("/history/rerun")
async def rerun_saved_report(request: Request, saved_report: str):
    history = load_report_history()

    for item in history:
        if item.get("saved_report") == saved_report:
            # Directly render analyze logic
            return await analyze(
                request,
                url_1=item.get("site_url", ""),
                url_2=item.get("competitor_url", "")
            )

    return RedirectResponse(url="/history", status_code=303)


@app.get("/history", response_class=HTMLResponse)
async def report_history(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="history.html",
        context={
            "request": request,
            "history": load_report_history(),
            "logo_url": safe_logo_url(),
        },
    )



@app.get("/compare")
async def compare_page(request: Request):
    return templates.TemplateResponse(
        request,
        "compare.html",
        {
            "request": request,
            "logo_url": safe_logo_url(),
            "user": auth_current_user(request),
        },
    )


@app.get("/analyze")
async def analyze_get_redirect():
    return RedirectResponse(url="/")


def strict_final_score_cap(page, score):
    """Final report-display score cap so flawed pages do not show unrealistic perfect scores."""
    try:
        score = int(score or 0)
    except Exception:
        score = 0

    if not isinstance(page, dict):
        return score

    title = str(page.get("title", "") or "").strip()
    meta = str(page.get("meta", "") or page.get("description", "") or "").strip()
    h1 = str(page.get("h1", "") or "").strip()

    try:
        h1_count = int(page.get("h1_count", 0) or 0)
    except Exception:
        h1_count = 0

    try:
        word_count = int(page.get("word_count", 0) or 0)
    except Exception:
        word_count = 0

    try:
        images = int(page.get("images", 0) or page.get("image_count", 0) or 0)
    except Exception:
        images = 0

    try:
        images_with_alt = int(page.get("images_with_alt", 0) or page.get("image_alt_count", 0) or 0)
    except Exception:
        images_with_alt = 0

    # Basic metadata caps
    if title and len(title) > 65:
        score = min(score, 95)

    if not title:
        score = min(score, 85)

    if not meta:
        score = min(score, 85)

    if not h1 or h1_count == 0:
        score = min(score, 85)

    # Thin-content caps
    if word_count and word_count < 100:
        score = min(score, 75)
    elif word_count and word_count < 300:
        score = min(score, 90)

    # Image alt coverage cap when images exist but many lack alt text
    if images > 0:
        alt_ratio = images_with_alt / images if images else 0
        if alt_ratio < 0.5:
            score = min(score, 95)

    # Weak or geographically mismatched competitor data should not look perfect
    if page.get("weak_data") or page.get("location_mismatch"):
        score = min(score, 85)

    return score


# === FINAL NO-PERFECT-SCORE + KEYWORD QUALITY CLEANUP ===

BAD_KEYWORD_EXACT = {
    "plumber free",
    "charges drains",
    "done plumbers",
    "plumbing experts serve",
    "raton trusted plumber",
    "raton plumbing experts",
    "location local plumber",
    "expert plumbing trenchless",
    "plumber southern",
}

BAD_KEYWORD_CONTAINS = {
    "charges drains",
    "done plumbers",
    "professionals years experience",
    "roof babe roof",
    "image",
    "scroll back up",
}

KEYWORD_REPLACEMENTS = {
    "raton trusted plumber": "Trusted Boca Raton Plumber",
    "raton plumbing experts": "Boca Raton Plumbing Experts",
    "plumbing experts serve": "Plumbing Experts",
    "plumber free": "Free Plumbing Estimates",
    "charges drains": "Drain Cleaning",
    "leaks repairs": "Leak Repair",
    "done plumbers": "Professional Plumbing Services",
    "expert plumbing trenchless": "Trenchless Plumbing Services",
    "plumber southern": "Southern California Plumber",
    "location local plumber": "Local Plumbing Services",
}

def no_perfect_display_score(page, score):
    """Avoid unrealistic perfect scores in client-facing reports."""
    try:
        score = int(score or 0)
    except Exception:
        score = 0

    # If a page is excellent, 98 still communicates strength without looking fake.
    if score >= 100:
        return 98

    return score

def polish_keyword_phrase_for_report(value):
    raw = str(value or "").strip()
    if not raw:
        return ""

    normalized = " ".join(raw.lower().split())

    if normalized in KEYWORD_REPLACEMENTS:
        return KEYWORD_REPLACEMENTS[normalized]

    if normalized in BAD_KEYWORD_EXACT:
        return ""

    if any(bad in normalized for bad in BAD_KEYWORD_CONTAINS):
        return ""

    # Remove chopped phrases where a city fragment starts the term.
    chopped_starts = (
        "raton trusted",
        "raton plumbing",
        "angeles plumbing",
        "southern plumber",
    )
    if normalized.startswith(chopped_starts):
        return ""

    words = normalized.split()

    # Avoid low-value two-word phrases that are just fragments.
    weak_pairs = {
        ("plumber", "free"),
        ("charges", "drains"),
        ("done", "plumbers"),
        ("total", "plumbing"),
        ("plumbing", "image"),
    }

    if len(words) == 2 and tuple(words) in weak_pairs:
        return ""

    return raw

def clean_keyword_gap_phrases_for_report(gap):
    if not isinstance(gap, dict):
        return gap

    def clean_item(item):
        if isinstance(item, str):
            return polish_keyword_phrase_for_report(item)

        if isinstance(item, dict):
            new_item = dict(item)
            for key in ("keyword", "query_variant_number", "query_group", "query_used", "base_keyword", "term", "phrase", "label", "text", "name"):
                if key in new_item:
                    cleaned = polish_keyword_phrase_for_report(new_item.get(key))
                    if not cleaned:
                        return None
                    new_item[key] = cleaned
            return new_item

        return item

    def clean_list(items):
        output = []
        seen = set()

        for item in items:
            cleaned = clean_item(item)

            if not cleaned:
                continue

            if isinstance(cleaned, str):
                dedupe_key = cleaned.lower()
            elif isinstance(cleaned, dict):
                dedupe_key = str(cleaned).lower()
            else:
                dedupe_key = repr(cleaned).lower()

            if dedupe_key in seen:
                continue

            seen.add(dedupe_key)
            output.append(cleaned)

        return output

    for key, value in list(gap.items()):
        if isinstance(value, list):
            gap[key] = clean_list(value)

        elif isinstance(value, dict):
            for subkey, subvalue in list(value.items()):
                if isinstance(subvalue, list):
                    value[subkey] = clean_list(subvalue)

    return gap


# === FINAL HTML REPORT POLISH ===

FINAL_BAD_HTML_PHRASES = {
    "Plumber Boca": "Boca Raton Plumber",
    "Raton Plumber": "Boca Raton Plumber",
    "Drains Leaks": "Drain and Leak Repair",
    "Plumbers Proudly": "Professional Plumbers",
    "Repairs Done": "Plumbing Repairs",
    "Plumbing Drains": "Drain Cleaning",
}

def final_polish_report_html(html):
    html = str(html or "")

    label_replacements = {
        ">Keyword Opportunities<": ">Keyword Opportunities<",
        ">Keywords Already Found": ">Keywords Already Found",
        ">Shared Topic Signals": ">Shared Topic Signals",
        ">Technical SEO Snapshot<": ">Technical SEO Snapshot<",
        ">Priority Growth Plan<": ">Priority Growth Plan<",
    }

    for old, new in label_replacements.items():
        html = html.replace(old, new)

    for old, new in FINAL_BAD_HTML_PHRASES.items():
        html = html.replace(old, new)
        html = html.replace(old.lower(), new)

    # Keep top card and table totals consistent once no-perfect scoring is active.
    # This only affects the client-facing rendered total cell, not factor rows.
    html = html.replace("<strong>100</strong>", "<strong>98</strong>", 1)

    return html






@app.post("/analyze", response_class=HTMLResponse)
async def analyze(
    request: Request,
    url_1: str = Form(...),
    url_2: str = Form(...),
):
    site = fetch_page_data(url_1)
    # Auto-select a real business competitor if no competitor URL was provided.
    # Live SERP stays protected by USE_LIVE_SERP in the Serper/client layer.
    selected_competitor_url = (url_2 or "").strip()

    if not selected_competitor_url:
        search_parts = [
            (service or "").strip() if "service" in locals() else "",
            (location or "").strip() if "location" in locals() else "",
        ]
        competitor_query = " ".join(part for part in search_parts if part).strip()

        if not competitor_query:
            competitor_query = (url_1 or "").strip()

        try:
            found_competitors = find_business_competitors(competitor_query)
        except Exception as exc:
            print(f"Business competitor finder failed: {exc}")
            found_competitors = []

        if found_competitors:
            selected_competitor_url = (
                found_competitors[0].get("url")
                or found_competitors[0].get("link")
                or found_competitors[0].get("website")
                or ""
            )

    if not selected_competitor_url:
        selected_competitor_url = url_2

    competitor = fetch_page_data(selected_competitor_url)
    competitors_sorted = [competitor]

    site_score, site_breakdown = score_page(site)
    site_score = apply_score_caps(site, site_score)
    site_score = no_perfect_display_score(site, site_score)
    site_score = no_perfect_display_score(site, site_score)
    comp_score, comp_breakdown = score_page(competitor)
    comp_score = strict_final_score_cap(competitor, comp_score)
    comp_score = no_perfect_display_score(competitor, comp_score)
    comp_score = no_perfect_display_score(competitor, comp_score)
    comp_score = strict_final_score_cap(competitor, comp_score)
    comp_score = apply_score_caps(competitor, comp_score)
    comp_score = no_perfect_display_score(competitor, comp_score)
    comp_score = no_perfect_display_score(competitor, comp_score)

    auto_save_geo_phrases(site.get("geo_phrases", []))
    auto_save_geo_phrases(competitor.get("geo_phrases", []))
    results = {
    "site_score": no_perfect_display_score(site, site_score),
    "competitor_score": strict_final_score_cap(competitor, comp_score),
    "site": site,
    "competitor": competitor
    }
    
    agent_insights = run_seo_agent(results)

    # Detect weak/blocked competitor crawls so the report does not overtrust bad scrape data.
    competitor["weak_data"] = (
        competitor.get("word_count", 0) < 100
        or competitor.get("h1_count", 0) == 0
        or not competitor.get("title")
    )

    site_score = no_perfect_display_score(site, site_score)
    site_score = no_perfect_display_score(site, site_score)
    site["score"] = site_score
    comp_score = apply_score_caps(competitor, comp_score)
    comp_score = strict_final_score_cap(competitor, comp_score)
    comp_score = strict_final_score_cap(competitor, comp_score)
    comp_score = no_perfect_display_score(competitor, comp_score)
    comp_score = no_perfect_display_score(competitor, comp_score)
    competitor["score"] = comp_score
    if isinstance(comp_breakdown, dict):
        if "total" in comp_breakdown:
            comp_breakdown["total"] = comp_score
        if "score" in comp_breakdown:
            comp_breakdown["score"] = comp_score

    gap = keyword_gap(site, competitors_sorted)
    gap = remove_mismatched_geo_keywords_from_gap(gap, site, competitor)
    gap = clean_keyword_gap_phrases_for_report_v2(gap)
    gap = clean_keyword_gap_phrases_for_report(gap)
    missing_terms_for_volume = flatten_missing_gap_terms(gap, limit=30)
    volume_data = enrich_keywords_with_dataforseo(missing_terms_for_volume)
    missing_terms_for_volume = filter_by_volume(missing_terms_for_volume, volume_data)

    competitor = apply_geo_mismatch_safety(site, competitor)
    comp_score = apply_score_caps(competitor, comp_score)

    if competitor.get("weak_data"):
        site_quick_wins = [
            "Use a stronger competitor page for accurate comparison.",
            "Choose a live service/location page, not a homepage, directory page, blocked page, or thin page.",
            "Review this report as a technical crawl check only; competitor-based recommendations are limited because the competitor data appears incomplete.",
        ]
    else:
        site_quick_wins = build_quick_wins(site, competitor)

    try:
        analysis = build_analysis_html(site, competitors_sorted, gap)
        analysis += build_geo_mismatch_warning(site, competitor)

        # Keep only the final clean Priority Growth Plan block if older analysis sections were also generated.
        if analysis and analysis.count('final-summary-box') > 1 and '<h3>Priority Growth Plan</h3>' in analysis:
            start = analysis.find('<div class="final-summary-box">', analysis.find('<h3>Priority Growth Plan</h3>') - 200)
            end = analysis.find('</div>', analysis.find('<h3>Priority Growth Plan</h3>'))
            if start != -1 and end != -1:
                analysis = analysis[start:end + len('</div>')]
        current_page_signals = extract_current_page_signals(site)
        analysis += build_final_strategy_summary(site, competitors_sorted, gap)
        analysis += build_volume_opportunity_summary(volume_data)
        try:
            agent_part = build_agent_action_plan(site, competitors_sorted, gap, site_quick_wins)
            agent_part += build_agent_insight_html(site, competitors_sorted, gap, site_quick_wins, volume_data)
        except Exception as e:
            print("Agent failed:", e)
            agent_part = ""

        analysis = agent_part + enhance_analysis(analysis or "")
    except Exception as e:
        print("Analysis generation failed:", e)
        analysis = f"<li><strong>Analysis generation failed:</strong> {e}</li><p>Core report data is still valid.</li>"

    competitor_quick_wins = [
        {
            "domain": competitor["clean_domain"],
            "items": build_quick_wins(competitor, site),
        }
    ]

    site_section_card = build_section_card(site)
    competitor_section_cards = [build_section_card(competitor)]

    context = {
            "request": request,
            "site": site,
            "competitors": competitors_sorted,
            "analysis_html": final_report_phrase_polish(polish_client_report_phrases(enforce_single_seo_action_plan(final_single_analysis_filter(clean_client_facing_report_text(clean_analysis_html_output(analysis)))))),
            "current_page_signals": current_page_signals,
            "volume_data": volume_data,
            "gap": gap,
            "site_score_breakdown": site_breakdown,
            "competitor_score_breakdowns": [
                {
                    "domain": competitor["clean_domain"],
                    "breakdown": comp_breakdown,
                    "score": strict_final_score_cap(competitor, competitor.get("score", 0)),
                }
            ],
            "site_quick_wins": site_quick_wins,
            "competitor_quick_wins": competitor_quick_wins,
            "site_section_card": site_section_card,
            "competitor_section_cards": competitor_section_cards,
            "generated_at": datetime.now().strftime("%B %d, %Y"),
            "logo_url": safe_logo_url(),
        }

    rendered = templates.TemplateResponse(
        request=request,
        name="report.html",
        context=context,
    )

    try:
        html = templates.env.get_template("report.html").render(context)
        html = final_polish_report_html(html)
        save_report_snapshot(html, site, competitor, gap, volume_data)
    except Exception as e:
        print("Report history save failed:", e)

    return rendered

@app.post("/export-pdf")
def export(html: str = Form(...)):
    try:
        path = export_pdf(html)
        return JSONResponse({"file": path})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# === CLEAN ANALYSIS OUTPUT OVERRIDE WITH SUGGESTION GUARDRAILS ===

# === CLEAN CLIENT-FACING ANALYSIS OUTPUT OVERRIDE ===
# Removes half-generated suggested titles/metas and uses cleaner client-ready language.
def build_analysis_html(site, competitors, gap):
    from html import escape

    def get(obj, key, default=None):
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    def safe_int(value, default=0):
        try:
            return int(value or 0)
        except Exception:
            return default

    def clean_text(value):
        return str(value or "").strip()

    def is_junk(value):
        text = clean_text(value).lower()
        if not text:
            return True

        junk_terms = [
            "breaking news",
            "latest news",
            "cnn",
            "msnbc",
            "undefined",
            "none",
            "null",
            "local service &",
            "looking for ?",
        ]

        if any(term in text for term in junk_terms):
            return True

        if text.endswith("&") or text.endswith("|") or text.endswith("-"):
            return True

        if len(text) < 8:
            return True

        return False

    site_score = safe_int(get(site, "score", 0))
    site_title_length = safe_int(get(site, "title_length", 0))
    site_meta_length = safe_int(get(site, "meta_length", 0))
    site_word_count = safe_int(get(site, "word_count", 0))

    site_h1 = clean_text(get(site, "h1", ""))
    site_title_status = clean_text(get(site, "title_status", ""))
    site_meta_status = clean_text(get(site, "meta_status", ""))

    competitors = competitors or []
    top_competitor = competitors[0] if competitors else None
    comp_score = safe_int(get(top_competitor, "score", 0)) if top_competitor else 0
    score_diff = site_score - comp_score if top_competitor else 0

    weak_competitor = any(bool(get(c, "weak_data", False)) for c in competitors)

    grouped = (gap or {}).get("missing_grouped", {}) if isinstance(gap, dict) else {}
    keyword_candidates = []

    for bucket in ["location", "service", "commercial"]:
        for item in grouped.get(bucket, []) or []:
            term = item.get("term") if isinstance(item, dict) else str(item)
            term = clean_text(term)
            if term and not is_junk(term):
                keyword_candidates.append(term)

    strongest_keyword = keyword_candidates[0] if keyword_candidates else ""

    if top_competitor:
        if score_diff > 0:
            score_sentence = f"Your page is currently ahead by {score_diff} points."
        elif score_diff < 0:
            score_sentence = f"Your page is currently behind by {abs(score_diff)} points."
        else:
            score_sentence = "Your page is currently tied with the competitor."
    else:
        score_sentence = "Use this report to review the page’s core SEO signals."

    if weak_competitor:
        summary = (
            f"{score_sentence} However, the competitor crawl appears limited, so the safest next step is to treat this as a page-quality review "
            "and rerun the report with a stronger direct competitor before making major keyword decisions."
        )
    else:
        summary = (
            f"{score_sentence} Use the recommendations below to prioritize the page updates most likely to improve targeting, depth, and search visibility."
        )

    steps = []

    if weak_competitor:
        steps.append("Rerun the report with a stronger direct competitor page that has clear service copy, headings, and indexable content.")

    if site_title_status.lower() != "good" or site_title_length < 30 or site_title_length > 65:
        steps.append(f"Review the title tag and keep it focused, readable, and close to 50–60 characters. Current title length: {site_title_length}.")

    if site_meta_status.lower() != "good" or site_meta_length < 70 or site_meta_length > 160:
        steps.append(f"Review the meta description and keep it around 140–155 characters with a clear service, benefit, and location. Current meta length: {site_meta_length}.")

    if not site_h1 or site_h1.lower() in ["none", "missing", "no h1"]:
        steps.append("Add one clear H1 that describes the page’s main service and location.")

    if site_word_count and site_word_count < 800:
        steps.append(f"Add useful supporting content such as service details, FAQs, trust signals, and local relevance. Current word count: {site_word_count}.")

    if strongest_keyword:
        steps.append(f"Work “{escape(strongest_keyword)}” into the page only if it accurately matches the client’s services and location targeting.")

    if not steps:
        steps.append("Keep the page focused and continue improving content depth, internal links, and trust signals where they support search intent.")

    steps_html = "".join(f"<li>{escape(step)}</li>" for step in steps)

    if strongest_keyword:
        keyword_html = (
            f"<p>Start by reviewing <strong>{escape(strongest_keyword)}</strong>. Use it only where it fits naturally, then review the Keyword Opportunities section above for other relevant service, location, and commercial terms.</p>"
        )
    else:
        keyword_html = (
            "<p>Review the Keyword Opportunities section above and prioritize only the terms that match the client’s actual services, locations, and search intent.</p>"
        )

    content_items = []

    if strongest_keyword:
        content_items.append(f"Add a focused FAQ or short supporting section around “{escape(strongest_keyword)}” if it fits the business.")
    if site_word_count and site_word_count < 800:
        content_items.append("Expand thin sections with specific service details, proof points, and local examples.")
    if not site_h1 or site_h1.lower() in ["none", "missing", "no h1"]:
        content_items.append("Create a stronger heading structure with one clear H1 and supporting H2s.")

    content_html = ""
    if content_items:
        content_html = "<h4>Content Improvements</h4><ul>" + "".join(f"<li>{item}</li>" for item in content_items) + "</ul>"

    return f"""
    <div class="final-summary-box action-plan-prioritized">
        <h3>Priority Growth Plan</h3>

        <p><strong>Summary:</strong> {escape(summary)}</p>

        <section class="priority-plan-grid">
            <section class="priority-plan-card priority-one">
                <span class="priority-plan-label">Priority 1</span>
                <h4>Top Priority</h4>
                <p>Fix the most important page issues first, including unclear targeting, missing service details, weak headings, or trust signals that affect conversions.</p>
                <ol>
                    {steps_html}
                </ol>
            </section>

            <section class="priority-plan-card priority-two">
                <span class="priority-plan-label">Priority 2</span>
                <h4>Next Priority</h4>
                <p>Use the competitor keyword gaps to improve page relevance and identify new sections, FAQs, or service/location content worth adding.</p>
                {keyword_html}
            </section>

            <section class="priority-plan-card priority-three">
                <span class="priority-plan-label">Priority 3</span>
                <h4>Supporting Improvements</h4>
                <p>Add supporting details that make the page more useful and trustworthy, such as FAQs, reviews, examples, service details, and clearer section headings.</p>
                {content_html if content_html else '<p>No major content structure issues were detected beyond the items above.</p>'}
            </section>
        </section>

        <p><strong>Takeaway:</strong> Focus on clear targeting, stronger content depth, and trust-building page elements before chasing broader SEO work.</p>
    </div>
    """

# === CURRENT PAGE KEYWORD SIGNALS ===
# Shows important phrases already found on the analyzed page.

# === FINAL ANALYSIS HTML CLEANER ===
# Cleans bad generated title/meta suggestions before rendering.
def clean_analysis_html_output(html):
    import re

    if not html:
        return html

    def clean_phrase(text):
        text = str(text or "").strip()

        # Remove SEO-waste words.
        text = re.sub(r"\bwelcome\s+to\b", "", text, flags=re.I)
        text = re.sub(r"\bwelcome\b", "", text, flags=re.I)

        # Fix smashed crawl text like TOPREMIER.
        text = re.sub(r"\bto([a-z])", r"\1", text, flags=re.I)

        # Remove unfinished endings.
        text = re.sub(r"\s*\|\s*local service\s*&.*$", "", text, flags=re.I)
        text = re.sub(r"\s*&\s*$", "", text)
        text = re.sub(r"\s*\|\s*$", "", text)
        text = re.sub(r"\s*-\s*$", "", text)

        # Normalize spacing.
        text = re.sub(r"\s+", " ", text).strip()

        # Convert ALL CAPS phrases into readable title case.
        if text and text.upper() == text:
            text = text.title()

        # Avoid title/meta starting empty after cleanup.
        text = text.strip(" -|&")

        return text

    # Clean suggested title line.
    def clean_title_match(match):
        original = match.group(1)
        cleaned = clean_phrase(original)

        if not cleaned or len(cleaned) < 10:
            return ""

        # Prevent chopped words by trimming to nearest word under 60 chars.
        if len(cleaned) > 60:
            cleaned = cleaned[:60].rsplit(" ", 1)[0].strip()

        return f"<li><strong>Suggested title:</strong> {cleaned}</li>"

    html = re.sub(
        r"<li><strong>Suggested title:</strong>\s*(.*?)</li>",
        clean_title_match,
        html,
        flags=re.I | re.S
    )

    # Clean suggested meta line.
    def clean_meta_match(match):
        original = match.group(1)
        cleaned = clean_phrase(original)

        cleaned = re.sub(r"Looking for\s*\?", "", cleaned, flags=re.I)
        cleaned = re.sub(r"Looking for\s+", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()

        if not cleaned or len(cleaned) < 25:
            return ""

        if len(cleaned) > 155:
            cleaned = cleaned[:155].rsplit(" ", 1)[0].strip()

        return f"<li><strong>Suggested meta:</strong> {cleaned}</li>"

    html = re.sub(
        r"<li><strong>Suggested meta:</strong>\s*(.*?)</li>",
        clean_meta_match,
        html,
        flags=re.I | re.S
    )

    # Remove empty Suggested Fixes block if both suggestions were removed.
    html = re.sub(
        r"<h4>Suggested Fixes</h4>\s*<ul>\s*</ul>",
        "",
        html,
        flags=re.I | re.S
    )

    return html

# === CLIENT-FACING TEXT QUALITY CLEANER ===
# Final safety pass for generated report language:
# - removes all-caps crawl junk
# - fixes words stuck together after "to"
# - removes unfinished title/meta endings
# - removes broken suggestion lines
def clean_client_facing_report_text(html):
    import re

    if not html:
        return html

    known_brand_terms = {
        "seo": "SEO",
        "hvac": "HVAC",
        "ny": "NY",
        "nyc": "NYC",
        "li": "LI",
    }

    bad_fragments = [
        "fast respo",
        "local service &",
        "looking for ?",
        "welcome topremier",
        "welcome to",
        "welcome",
        "undefined",
        "none",
        "null",
    ]

    def title_case_safely(text):
        words = []
        for word in text.split():
            clean = re.sub(r"[^A-Za-z0-9]", "", word).lower()
            if clean in known_brand_terms:
                words.append(word.replace(clean, known_brand_terms[clean]))
            elif word.isupper() and len(word) > 2:
                words.append(word.capitalize())
            else:
                words.append(word)
        return " ".join(words)

    def clean_line(text):
        text = str(text or "")

        # Basic entity/spacing cleanup.
        text = text.replace("&amp;", "&")
        text = re.sub(r"\s+", " ", text).strip()

        # Fix common crawl smash: "TOPREMIER" -> "Premier".
        text = re.sub(r"\bTO([A-Z][A-Z]+)", lambda m: m.group(1).capitalize(), text)
        text = re.sub(r"\bto([A-Z][a-z]+)", r"\1", text)

        # Remove low-value SEO/crawl words.
        text = re.sub(r"\bwelcome\s+to\b", "", text, flags=re.I)
        text = re.sub(r"\bwelcome\b", "", text, flags=re.I)

        # Remove unfinished template tails.
        text = re.sub(r"\s*\|\s*Local Service\s*&.*$", "", text, flags=re.I)
        text = re.sub(r"\s*\|\s*local service\s*&.*$", "", text, flags=re.I)
        text = re.sub(r"\s*&\s*$", "", text)
        text = re.sub(r"\s*\|\s*$", "", text)
        text = re.sub(r"\s*-\s*$", "", text)

        # Remove broken "Looking for ?" openings.
        text = re.sub(r"\bLooking for\s*\?\s*", "", text, flags=re.I)
        text = re.sub(r"\bLooking for\s+", "", text, flags=re.I)

        # Normalize spacing again.
        text = re.sub(r"\s+", " ", text).strip(" -|&")

        # Convert all-caps crawl strings into readable text.
        letters = re.sub(r"[^A-Za-z]", "", text)
        if letters and text.upper() == text and len(letters) > 6:
            text = text.title()

        text = title_case_safely(text)
        text = re.sub(r"\s+", " ", text).strip(" -|&")

        return text

    def looks_broken(text):
        low = str(text or "").lower().strip()

        if not low:
            return True

        if any(fragment in low for fragment in bad_fragments):
            return True

        if low.endswith((" respo", " fa", " ser", " loc", "&", "|", "-")):
            return True

        # Reject very short suggestions.
        if len(low) < 12:
            return True

        # Reject words with obvious smashed all-caps remnants.
        if re.search(r"\b[A-Z]{2,}[a-z]+", str(text or "")):
            return True

        return False

    def clean_suggested_title(match):
        raw = match.group(1)
        cleaned = clean_line(raw)

        if len(cleaned) > 60:
            cleaned = cleaned[:60].rsplit(" ", 1)[0].strip(" -|&")

        if looks_broken(cleaned):
            return ""

        return f"<li><strong>Suggested title:</strong> {cleaned}</li>"

    def clean_suggested_meta(match):
        raw = match.group(1)
        cleaned = clean_line(raw)

        if len(cleaned) > 155:
            cleaned = cleaned[:155].rsplit(" ", 1)[0].strip(" -|&")

        if looks_broken(cleaned):
            return ""

        return f"<li><strong>Suggested meta:</strong> {cleaned}</li>"

    html = re.sub(
        r"<li><strong>Suggested title:</strong>\s*(.*?)</li>",
        clean_suggested_title,
        html,
        flags=re.I | re.S
    )

    html = re.sub(
        r"<li><strong>Suggested meta:</strong>\s*(.*?)</li>",
        clean_suggested_meta,
        html,
        flags=re.I | re.S
    )

    # Clean Suggested Page Sections list items.
    def clean_section_item(match):
        raw = match.group(1)
        cleaned = clean_line(raw)

        if looks_broken(cleaned):
            return ""

        return f"<li>{cleaned}</li>"

    html = re.sub(
        r"<li>\s*([^<]*?(?:WELCOME|TOPREMIER|Local Service|Fast Respo|Looking for \?)[^<]*?)\s*</li>",
        clean_section_item,
        html,
        flags=re.I | re.S
    )

    # Remove empty Suggested blocks.
    html = re.sub(r"<h4>Suggested Fixes</h4>\s*<ul>\s*</ul>", "", html, flags=re.I | re.S)
    html = re.sub(r"<h4>Suggested Page Sections</h4>\s*<ul>\s*</ul>", "", html, flags=re.I | re.S)

    return html

# === FINAL SINGLE ANALYSIS BLOCK FILTER ===
# Removes older duplicate analysis sections and weak suggested title/meta blocks.

# === FINAL SINGLE ANALYSIS BLOCK FILTER ===
# Removes older duplicate analysis sections and weak suggested title/meta blocks.
def final_single_analysis_filter(html):
    import re

    if not html:
        return html

    # Remove weak generated suggestion sections entirely.
    html = re.sub(
        r"<h4>\s*Suggested Fixes\s*</h4>\s*<ul>.*?</ul>",
        "",
        html,
        flags=re.I | re.S
    )

    html = re.sub(
        r"<h4>\s*Suggested Page Sections\s*</h4>\s*<ul>.*?</ul>",
        "",
        html,
        flags=re.I | re.S
    )

    # If the clean Priority Growth Plan exists, keep only that card.
    seo_h3 = re.search(r"<h3>\s*Priority Growth Plan\s*</h3>", html, flags=re.I)
    if seo_h3:
        start = html.rfind('<div class="final-summary-box">', 0, seo_h3.start())
        if start != -1:
            end = html.find('</div>', seo_h3.end())
            if end != -1:
                return html[start:end + len('</div>')]

    return html

# === KEYWORD GAP ROOT WRAPPER ===
# Adds obvious shared service-root keyword bubbles such as painting / roofing / plumbing
# when both pages use related language, even if exact phrase matching misses it.

def _add_root_shared_keywords_to_gap(gap, site, competitors):
    import re

    if not isinstance(gap, dict):
        return gap

    def get(obj, key, default=""):
        if isinstance(obj, dict):
            return obj.get(key, default) or default
        return getattr(obj, key, default) or default

    def collect_text(obj):
        fields = [
            get(obj, "title", ""),
            get(obj, "meta_description", ""),
            get(obj, "meta", ""),
            get(obj, "h1", ""),
            get(obj, "domain", ""),
            get(obj, "clean_domain", ""),
            get(obj, "text", ""),
            get(obj, "page_text", ""),
            get(obj, "body_text", ""),
            get(obj, "visible_text", ""),
            get(obj, "content", ""),
            get(obj, "all_text", ""),
        ]
        return " ".join(str(x).lower() for x in fields if x)

    root_groups = {
        "painting": ["painting", "painter", "painters", "paint"],
        "interior painting": ["interior painting", "interior painter", "interior painters"],
        "exterior painting": ["exterior painting", "exterior painter", "exterior painters"],
        "residential painting": ["residential painting", "house painting", "house painters"],
        "commercial painting": ["commercial painting", "commercial painter", "commercial painters"],
        "wallpaper": ["wallpaper", "wallcovering", "wallcoverings"],
        "drywall repair": ["drywall repair", "drywall", "sheetrock"],
        "roofing": ["roofing", "roofer", "roofers", "roof"],
        "plumbing": ["plumbing", "plumber", "plumbers"],
        "hvac": ["hvac", "heating", "cooling", "air conditioning"],
    }

    site_text = collect_text(site)
    competitor_text = " ".join(collect_text(c) for c in (competitors or []))

    def has_variant(text, variants):
        for variant in variants:
            if re.search(r"\b" + re.escape(variant.lower()) + r"\b", text):
                return True
        return False

    existing_shared = gap.get("shared", []) or []
    existing_terms = {
        str(item.get("term", item)).strip().lower()
        for item in existing_shared
    }

    boosted = []

    for root, variants in root_groups.items():
        if root in existing_terms:
            continue

        if has_variant(site_text, variants) and has_variant(competitor_text, variants):
            boosted.append({
                "term": root,
                "priority": "shared"
            })

    if boosted:
        gap["shared"] = boosted + existing_shared

    return gap


# Wrap keyword_gap safely without editing the route indentation.
if "keyword_gap" in globals() and "_original_keyword_gap_before_root_wrapper" not in globals():
    _original_keyword_gap_before_root_wrapper = keyword_gap

    def keyword_gap(site, competitors):
        gap = _original_keyword_gap_before_root_wrapper(site, competitors)
        return _add_root_shared_keywords_to_gap(gap, site, competitors)

# === ENFORCE SINGLE SEO ACTION PLAN ===
# Final safety filter: keep only the Priority Growth Plan card from generated analysis_html.
def enforce_single_seo_action_plan(html):
    import re

    if not html:
        return html

    # Remove weak generated suggestion sections everywhere.
    html = re.sub(
        r"<h4>\s*Suggested Fixes\s*</h4>\s*<ul>.*?</ul>",
        "",
        html,
        flags=re.I | re.S,
    )
    html = re.sub(
        r"<h4>\s*Suggested Page Sections\s*</h4>\s*<ul>.*?</ul>",
        "",
        html,
        flags=re.I | re.S,
    )

    # Extract all final-summary-box cards.
    cards = re.findall(
        r'<div class="final-summary-box">.*?</div>',
        html,
        flags=re.I | re.S,
    )

    # Prefer the clean Priority Growth Plan card.
    for card in cards:
        if re.search(r"<h3>\s*Priority Growth Plan\s*</h3>", card, flags=re.I):
            return card

    # If no Priority Growth Plan exists, remove obvious legacy agent cards.
    html = re.sub(
        r'<div class="final-summary-box">\s*<h3>\s*Agent Action Plan\s*</h3>.*?</div>',
        "",
        html,
        flags=re.I | re.S,
    )
    html = re.sub(
        r'<div class="final-summary-box">\s*<h3>\s*Agent Recommendation\s*</h3>.*?</div>',
        "",
        html,
        flags=re.I | re.S,
    )

    return html

# === CLEAN CURRENT PAGE KEYWORD SIGNALS OVERRIDE ===
# Improves current page keyword bubbles:
# - removes junk terms like image / total / location
# - fixes smashed words like Locallong / Islandpainting
# - keeps useful service phrases like painting, painting company, professional painting

# === FINAL CLEAN CURRENT PAGE KEYWORD SIGNALS ===
# Replaces weird phrase fragments with clean SEO keyword bubbles.

# === FINAL CLEAN CURRENT PAGE KEYWORD SIGNALS ===
# Replaces weird phrase fragments with clean SEO keyword bubbles.

# === LOOSE CORE CURRENT PAGE KEYWORDS ===
# Shows clean useful keyword bubbles when related words appear anywhere on the page.

# === FINAL KEYWORD THEME BUBBLES ===
# Creates useful SEO keyword bubbles from the page's actual industry theme,
# instead of only exact-match fragments.


# Stronger shared keyword booster.
def add_shared_root_keywords(gap, site, competitors):
    import re

    if not isinstance(gap, dict):
        return gap

    def get(obj, key, default=""):
        if isinstance(obj, dict):
            return obj.get(key, default) or default
        return getattr(obj, key, default) or default

    def collect_text(obj):
        fields = [
            get(obj, "title", ""),
            get(obj, "meta_description", ""),
            get(obj, "meta", ""),
            get(obj, "h1", ""),
            get(obj, "domain", ""),
            get(obj, "clean_domain", ""),
            get(obj, "text", ""),
            get(obj, "page_text", ""),
            get(obj, "body_text", ""),
            get(obj, "visible_text", ""),
            get(obj, "content", ""),
            get(obj, "all_text", ""),
        ]
        text = " ".join(str(x).lower() for x in fields if x)
        text = text.replace("longisland", "long island")
        text = text.replace("islandpainting", "island painting")
        text = text.replace("propainting", "pro painting")
        text = text.replace("premierpainting", "premier painting")
        return re.sub(r"\s+", " ", text)

    site_text = collect_text(site)
    comp_text = " ".join(collect_text(c) for c in (competitors or []))

    def has_any(text, words):
        return any(re.search(r"\b" + re.escape(w) + r"\b", text) for w in words)

    shared_terms = []

    if has_any(site_text, ["painting", "painter", "painters", "paint"]) and has_any(comp_text, ["painting", "painter", "painters", "paint"]):
        shared_terms.append("painting")

    if has_any(site_text, ["roofing", "roofer", "roofers", "roof"]) and has_any(comp_text, ["roofing", "roofer", "roofers", "roof"]):
        shared_terms.append("roofing")

    if has_any(site_text, ["plumbing", "plumber", "plumbers"]) and has_any(comp_text, ["plumbing", "plumber", "plumbers"]):
        shared_terms.append("plumbing")

    if has_any(site_text, ["hvac", "heating", "cooling", "air conditioning"]) and has_any(comp_text, ["hvac", "heating", "cooling", "air conditioning"]):
        shared_terms.append("hvac")

    existing = gap.get("shared", []) or []
    existing_terms = {
        str(item.get("term", item)).strip().lower()
        for item in existing
    }

    boosted = []
    for term in shared_terms:
        if term not in existing_terms:
            boosted.append({
                "term": term,
                "priority": "shared",
            })

    gap["shared"] = boosted + existing

    return gap


# Wrap keyword_gap so shared roots are added every time.
if "keyword_gap" in globals() and "_keyword_gap_before_theme_bubbles" not in globals():
    _keyword_gap_before_theme_bubbles = keyword_gap

    def keyword_gap(site, competitors):
        gap = _keyword_gap_before_theme_bubbles(site, competitors)
        return add_shared_root_keywords(gap, site, competitors)

# === LOOSE CORE CURRENT PAGE KEYWORDS ===
# Shows clean useful keyword bubbles when related words appear anywhere on the page.

# === LOOSE CORE CURRENT PAGE KEYWORDS ===
# Shows clean useful keyword bubbles when related words appear anywhere on the page.
def extract_current_page_signals(site):
    import re

    def get(obj, key, default=""):
        if isinstance(obj, dict):
            return obj.get(key, default) or default
        return getattr(obj, key, default) or default

    fields = [
        get(site, "title", ""),
        get(site, "meta_description", ""),
        get(site, "meta", ""),
        get(site, "h1", ""),
        get(site, "clean_domain", ""),
        get(site, "domain", ""),
        get(site, "text", ""),
        get(site, "page_text", ""),
        get(site, "body_text", ""),
        get(site, "visible_text", ""),
        get(site, "content", ""),
        get(site, "all_text", ""),
    ]

    text = " ".join(str(x).lower() for x in fields if x)

    fixes = {
        "longisland": "long island",
        "islandpainting": "island painting",
        "propainting": "pro painting",
        "premierpainting": "premier painting",
        "topremier": "to premier",
    }

    for bad, good in fixes.items():
        text = text.replace(bad, good)

    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    text = re.sub(r"\s+", " ", text)

    def has(word):
        return re.search(r"\b" + re.escape(word) + r"\b", text) is not None

    def has_any(words):
        return any(has(w) for w in words)

    has_painting = has_any(["painting", "painter", "painters", "paint"])

    results = []

    def add(term):
        if term not in results:
            results.append(term)

    if has_painting:
        add("Painting")

        if has_any(["company", "contractor", "contractors", "business"]):
            add("Painting Company")

        if has_any(["professional", "expert", "quality"]):
            add("Professional Painting")

        if has_any(["interior", "inside", "indoor"]):
            add("Interior Painting")

        if has_any(["exterior", "outside", "outdoor"]):
            add("Exterior Painting")

        if has_any(["residential", "home", "house", "homes", "houses"]):
            add("Residential Painting")

        if has_any(["commercial", "business", "office", "retail"]):
            add("Commercial Painting")

        if has_any(["house", "home"]) and has_any(["painter", "painters"]):
            add("House Painters")

    if has_any(["drywall", "sheetrock"]):
        add("Drywall Repair")

    if has_any(["wallpaper", "wallcovering", "wallcoverings"]):
        add("Wallpaper Installation")

    if has_any(["roofing", "roofer", "roofers", "roof"]):
        add("Roofing")

    if has_any(["plumbing", "plumber", "plumbers"]):
        add("Plumbing")

    if has_any(["hvac", "heating", "cooling", "air conditioning"]):
        add("HVAC")

    blocked = {"Image", "Images", "Total", "Location", "Locations", "Local", "Service", "Services"}

    clean = []
    for term in results:
        if term not in blocked:
            clean.append({
                "term": term,
                "count": 1,
            })

    return clean[:10]

# === KEYWORD DISPLAY PHRASE CLEANUP ===
def clean_keyword_display_phrase(term):
    text = str(term or "").strip().lower()

    replacements = {
        "plumbing trenchless": "trenchless plumbing",
        "pipe trenchless": "trenchless pipe repair",
        "repair trenchless": "trenchless repair",
        "painting professional": "professional painting",
        "painting interior": "interior painting",
        "painting exterior": "exterior painting",
        "painting residential": "residential painting",
        "painting commercial": "commercial painting",
    }

    text = replacements.get(text, text)
    text = " ".join(text.split())

    keep_upper = {"seo", "hvac", "ny", "nyc"}
    return " ".join(w.upper() if w in keep_upper else w.capitalize() for w in text.split())


def clean_gap_keyword_display(gap):
    if not isinstance(gap, dict):
        return gap

    for bucket in ["service", "location", "commercial"]:
        for item in gap.get("missing_grouped", {}).get(bucket, []) or []:
            if isinstance(item, dict) and "term" in item:
                item["term"] = clean_keyword_display_phrase(item["term"])

    for item in gap.get("shared", []) or []:
        if isinstance(item, dict) and "term" in item:
            item["term"] = clean_keyword_display_phrase(item["term"])

    return gap

# === SAFE KEYWORD GAP DISPLAY CLEANER ===
# Cleans awkward keyword bubbles without editing route indentation.
def _clean_keyword_term_for_display(term):
    text = str(term or "").strip().lower()
    text = " ".join(text.split())

    replacements = {
        "plumbing trenchless": "trenchless plumbing",
        "trenchless total": "trenchless plumbing",
        "total plumbing": "plumbing services",
        "plumbing los": "plumbing los angeles",
        "plumbing image": "",
        "location local plumber": "local plumber",
        "plumber california": "california plumber",
        "plumber southern california": "southern california plumber",
        "pipe trenchless": "trenchless pipe repair",
        "repair trenchless": "trenchless repair",
    }

    text = replacements.get(text, text)

    blocked_exact = {
        "",
        "image",
        "images",
        "total",
        "location",
        "locations",
        "plumbing image",
    }

    blocked_contains = [
        " image",
        " total",
        "location location",
    ]

    if text in blocked_exact:
        return ""

    if any(bad in text for bad in blocked_contains):
        return ""

    # Remove ugly fragments.
    if text.endswith((" los", " total", " image", " location")):
        return ""

    keep_upper = {"seo", "hvac", "ny", "nyc", "la"}
    return " ".join(w.upper() if w in keep_upper else w.capitalize() for w in text.split())


def _clean_keyword_gap_display_terms(gap):
    if not isinstance(gap, dict):
        return gap

    grouped = gap.get("missing_grouped", {}) or {}

    for bucket in ["service", "location", "commercial"]:
        cleaned = []
        seen = set()

        for item in grouped.get(bucket, []) or []:
            if isinstance(item, dict):
                term = _clean_keyword_term_for_display(item.get("term", ""))
                if not term:
                    continue

                key = term.lower()
                if key in seen:
                    continue

                new_item = dict(item)
                new_item["term"] = term
                cleaned.append(new_item)
                seen.add(key)
            else:
                term = _clean_keyword_term_for_display(item)
                if term and term.lower() not in seen:
                    cleaned.append({"term": term, "priority": "medium"})
                    seen.add(term.lower())

        grouped[bucket] = cleaned

    gap["missing_grouped"] = grouped

    shared_cleaned = []
    shared_seen = set()

    for item in gap.get("shared", []) or []:
        if isinstance(item, dict):
            term = _clean_keyword_term_for_display(item.get("term", ""))
            if not term:
                continue

            key = term.lower()
            if key in shared_seen:
                continue

            new_item = dict(item)
            new_item["term"] = term
            shared_cleaned.append(new_item)
            shared_seen.add(key)
        else:
            term = _clean_keyword_term_for_display(item)
            if term and term.lower() not in shared_seen:
                shared_cleaned.append({"term": term, "priority": "shared"})
                shared_seen.add(term.lower())

    gap["shared"] = shared_cleaned

    return gap


if "keyword_gap" in globals() and "_keyword_gap_before_display_cleaner" not in globals():
    _keyword_gap_before_display_cleaner = keyword_gap

    def keyword_gap(site, competitors):
        gap = _keyword_gap_before_display_cleaner(site, competitors)
        return _clean_keyword_gap_display_terms(gap)

# === FINAL CLIENT PHRASE POLISH ===
def polish_client_report_phrases(html):
    if not html:
        return html

    replacements = {
        "Repair Replacement": "Roof Repair and Replacement",
        "repair replacement": "roof repair and replacement",
        "Your page is  ahead": "Your page is ahead",
        "Your page is  behind": "Your page is behind",
        "Your page is  tied": "Your page is tied",
    }

    for old, new in replacements.items():
        html = html.replace(old, new)

    # Fix duplicate phrases caused by broad HTML replacements.
    duplicate_fixes = {
        "Long Long Island": "Long Island",
        "Long Island Island": "Long Island",
        "Plumbing Long Island Island": "Plumbing Long Island",
        "Long Long Island Plumbing": "Long Island Plumbing",
        "Long Island Long Island": "Long Island",
        "Island Island": "Island",
    }

    for old, new in duplicate_fixes.items():
        html = html.replace(old, new)

    return html

# === FINAL REPORT HTML PHRASE POLISH ===
def final_report_phrase_polish(html):
    if not html:
        return html

    replacements = {
        "Repair Replacement": "Roof Repair and Replacement",
        "repair replacement": "roof repair and replacement",
        "Your page is  ahead": "Your page is ahead",
        "Your page is  behind": "Your page is behind",
        "Your page is  tied": "Your page is tied",
    }

    for old, new in replacements.items():
        html = html.replace(old, new)

    return html

# === ROOFING KEYWORD LANGUAGE CLEANUP ===
def clean_roofing_keyword_language_text(text):
    if not text:
        return text

    replacements = {
        "Repair Nassau Suffolk": "Roof Repair in Nassau and Suffolk",
        "repair nassau suffolk": "roof repair in Nassau and Suffolk",
        "Roofing Repair Nassau": "Roofing Repair in Nassau",
        "roofing repair nassau": "roofing repair in Nassau",
        "Repair Nassau": "Roof Repair in Nassau",
        "repair nassau": "roof repair in Nassau",
        "Repair Replacement": "Roof Repair and Replacement",
        "repair replacement": "roof repair and replacement",
        "Repair Corp": "",
        "repair corp": "",
        "Repair Maintenance": "Roof Maintenance",
        "repair maintenance": "roof maintenance",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    return text


def clean_roofing_gap_language(gap):
    if not isinstance(gap, dict):
        return gap

    grouped = gap.get("missing_grouped", {}) or {}

    for bucket in ["service", "location", "commercial"]:
        cleaned = []
        seen = set()

        for item in grouped.get(bucket, []) or []:
            if not isinstance(item, dict):
                continue

            term = clean_roofing_keyword_language_text(item.get("term", "")).strip()

            if not term:
                continue

            key = term.lower()
            if key in seen:
                continue

            new_item = dict(item)
            new_item["term"] = term
            cleaned.append(new_item)
            seen.add(key)

        grouped[bucket] = cleaned

    gap["missing_grouped"] = grouped
    return gap


# Wrap keyword_gap safely.
if "keyword_gap" in globals() and "_keyword_gap_before_roofing_language_cleaner" not in globals():
    _keyword_gap_before_roofing_language_cleaner = keyword_gap

    def keyword_gap(site, competitors):
        gap = _keyword_gap_before_roofing_language_cleaner(site, competitors)
        return clean_roofing_gap_language(gap)


# Wrap final phrase polish safely if present.
if "final_report_phrase_polish" in globals() and "_final_report_phrase_polish_before_roofing_language" not in globals():
    _final_report_phrase_polish_before_roofing_language = final_report_phrase_polish

    def final_report_phrase_polish(html):
        html = _final_report_phrase_polish_before_roofing_language(html)
        return clean_roofing_keyword_language_text(html)

# === FINAL KEYWORD LANGUAGE CLEANER ===
# Cleans awkward keyword phrases before they appear in bubbles or analysis.
def normalize_client_keyword_phrase(term):
    text = str(term or "").strip()
    if not text:
        return ""

    lower = " ".join(text.lower().split())

    remove_terms = {
        "repair corp",
        "corp repair",
    }

    if lower in remove_terms:
        return ""

    replacements = {
        "repair replacement": "Roof Repair and Replacement",
        "repair maintenance": "Roof Maintenance",
        "repair nassau suffolk": "Roof Repair in Nassau and Suffolk",
        "roofing repair nassau": "Roofing Repair in Nassau",
        "repair nassau": "Roof Repair in Nassau",
        "repair suffolk": "Roof Repair in Suffolk",
        "roofing repair suffolk": "Roofing Repair in Suffolk",
        "roofing repair": "Roofing Repair",
        "roof repair": "Roof Repair",
        "roof replacement": "Roof Replacement",
        "roof maintenance": "Roof Maintenance",
    }

    if lower in replacements:
        return replacements[lower]

    # Clean common reversed/awkward fragments.
    text = text.replace("Repair Nassau Suffolk", "Roof Repair in Nassau and Suffolk")
    text = text.replace("Roofing Repair Nassau", "Roofing Repair in Nassau")
    text = text.replace("Repair Nassau", "Roof Repair in Nassau")
    text = text.replace("Repair Suffolk", "Roof Repair in Suffolk")
    text = text.replace("Repair Replacement", "Roof Repair and Replacement")
    text = text.replace("Repair Maintenance", "Roof Maintenance")
    text = text.replace("Repair Corp", "")

    text = " ".join(text.split()).strip(" -|,")

    return text


def normalize_gap_keyword_phrases(gap):
    if not isinstance(gap, dict):
        return gap

    grouped = gap.get("missing_grouped", {}) or {}

    for bucket in ["service", "location", "commercial"]:
        new_items = []
        seen = set()

        for item in grouped.get(bucket, []) or []:
            if not isinstance(item, dict):
                continue

            term = normalize_client_keyword_phrase(item.get("term", ""))

            if not term:
                continue

            key = term.lower()
            if key in seen:
                continue

            new_item = dict(item)
            new_item["term"] = term
            new_items.append(new_item)
            seen.add(key)

        grouped[bucket] = new_items

    gap["missing_grouped"] = grouped

    shared = []
    seen = set()

    for item in gap.get("shared", []) or []:
        if not isinstance(item, dict):
            continue

        term = normalize_client_keyword_phrase(item.get("term", ""))

        if not term:
            continue

        key = term.lower()
        if key in seen:
            continue

        new_item = dict(item)
        new_item["term"] = term
        shared.append(new_item)
        seen.add(key)

    gap["shared"] = shared

    return gap


# Safely wrap keyword_gap after all earlier wrappers.
if "keyword_gap" in globals() and "_keyword_gap_before_final_language_clean" not in globals():
    _keyword_gap_before_final_language_clean = keyword_gap

    def keyword_gap(site, competitors):
        gap = _keyword_gap_before_final_language_clean(site, competitors)
        return normalize_gap_keyword_phrases(gap)


# Safely clean final analysis HTML too.
if "final_report_phrase_polish" in globals() and "_final_report_phrase_polish_before_keyword_language_clean" not in globals():
    _final_report_phrase_polish_before_keyword_language_clean = final_report_phrase_polish

    def final_report_phrase_polish(html):
        html = _final_report_phrase_polish_before_keyword_language_clean(html)
        if not html:
            return html

        html = html.replace("Repair Nassau Suffolk", "Roof Repair in Nassau and Suffolk")
        html = html.replace("repair nassau suffolk", "roof repair in Nassau and Suffolk")
        html = html.replace("Roofing Repair Nassau", "Roofing Repair in Nassau")
        html = html.replace("Repair Nassau", "Roof Repair in Nassau")
        html = html.replace("Repair Replacement", "Roof Repair and Replacement")
        html = html.replace("repair replacement", "roof repair and replacement")
        html = html.replace("Repair Maintenance", "Roof Maintenance")
        html = html.replace("Repair Corp", "")

        html = " ".join(html.split()) if "<" not in html else html
        return html

# === ROOF REPAIR WORDING POLISH ===
def polish_roof_repair_wording(html):
    if not html:
        return html

    html = html.replace("Roofing Repair in Nassau", "Roof Repair in Nassau")
    html = html.replace("roofing repair in Nassau", "roof repair in Nassau")
    html = html.replace("Roofing Repair", "Roof Repair")
    html = html.replace("roofing repair", "roof repair")

    return html


if "final_report_phrase_polish" in globals() and "_final_report_phrase_polish_before_roof_repair_wording" not in globals():
    _final_report_phrase_polish_before_roof_repair_wording = final_report_phrase_polish

    def final_report_phrase_polish(html):
        html = _final_report_phrase_polish_before_roof_repair_wording(html)
        return polish_roof_repair_wording(html)


if "keyword_gap" in globals() and "_keyword_gap_before_roof_repair_wording" not in globals():
    _keyword_gap_before_roof_repair_wording = keyword_gap

    def keyword_gap(site, competitors):
        gap = _keyword_gap_before_roof_repair_wording(site, competitors)

        if isinstance(gap, dict):
            grouped = gap.get("missing_grouped", {}) or {}
            for bucket in ["service", "location", "commercial"]:
                for item in grouped.get(bucket, []) or []:
                    if isinstance(item, dict) and "term" in item:
                        item["term"] = polish_roof_repair_wording(item["term"])

            for item in gap.get("shared", []) or []:
                if isinstance(item, dict) and "term" in item:
                    item["term"] = polish_roof_repair_wording(item["term"])

        return gap

# === CROSS-INDUSTRY CLIENT PHRASE POLISH ===
def polish_cross_industry_phrases(text):
    if not text:
        return text

    replacements = {
        "Repair Quick": "Emergency Repair",
        "repair quick": "emergency repair",

        "Repair Installation": "Repair and Installation",
        "repair installation": "repair and installation",

        "Roof Repair Installation": "Roof Repair and Installation",
        "roof repair installation": "roof repair and installation",

        "Renovations Plumbing": "Plumbing Renovations",
        "renovations plumbing": "plumbing renovations",

        "Presence Expert SEO": "Expert SEO Services",
        "presence expert seo": "expert SEO services",

        "Expert SEO Honolulu": "Expert SEO in Honolulu",
        "expert seo honolulu": "expert SEO in Honolulu",

        "SEO Honolulu": "Honolulu SEO",
        "seo honolulu": "Honolulu SEO",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    return text


if "final_report_phrase_polish" in globals() and "_final_report_phrase_polish_before_cross_industry" not in globals():
    _final_report_phrase_polish_before_cross_industry = final_report_phrase_polish

    def final_report_phrase_polish(html):
        html = _final_report_phrase_polish_before_cross_industry(html)
        return polish_cross_industry_phrases(html)


if "keyword_gap" in globals() and "_keyword_gap_before_cross_industry_phrase_polish" not in globals():
    _keyword_gap_before_cross_industry_phrase_polish = keyword_gap

    def keyword_gap(site, competitors):
        gap = _keyword_gap_before_cross_industry_phrase_polish(site, competitors)

        if isinstance(gap, dict):
            grouped = gap.get("missing_grouped", {}) or {}

            for bucket in ["service", "location", "commercial"]:
                cleaned = []
                seen = set()

                for item in grouped.get(bucket, []) or []:
                    if not isinstance(item, dict):
                        continue

                    term = polish_cross_industry_phrases(item.get("term", "")).strip()

                    if not term:
                        continue

                    key = term.lower()
                    if key in seen:
                        continue

                    new_item = dict(item)
                    new_item["term"] = term
                    cleaned.append(new_item)
                    seen.add(key)

                grouped[bucket] = cleaned

            gap["missing_grouped"] = grouped

        return gap

# === FINAL CROSS-INDUSTRY KEYWORD PHRASE CLEANUP ===
def final_cross_industry_phrase_cleanup(text):
    if not text:
        return text

    replacements = {
        # Plumbing
        "Repair Quick": "Emergency Plumbing Repair",
        "repair quick": "emergency plumbing repair",
        "Plumbing Inc": "",
        "plumbing inc": "",
        "Renovations Plumbing": "Plumbing Renovations",
        "renovations plumbing": "plumbing renovations",

        # Roofing
        "Repair Installation": "Roof Repair and Installation",
        "repair installation": "roof repair and installation",
        "Roofing Repair": "Roof Repair",
        "roofing repair": "roof repair",

        # SEO
        "Presence Expert SEO": "Expert SEO Services",
        "presence expert seo": "expert SEO services",
        "SEO Agency Actually": "SEO Agency",
        "seo agency actually": "SEO agency",
        "Expert SEO Honolulu": "Expert SEO in Honolulu",
        "expert seo honolulu": "expert SEO in Honolulu",
        "SEO Honolulu": "Honolulu SEO",
        "seo honolulu": "Honolulu SEO",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    return " ".join(text.split()).strip() if "<" not in text else text


def final_clean_gap_terms(gap):
    if not isinstance(gap, dict):
        return gap

    grouped = gap.get("missing_grouped", {}) or {}

    for bucket in ["service", "location", "commercial"]:
        cleaned = []
        seen = set()

        for item in grouped.get(bucket, []) or []:
            if not isinstance(item, dict):
                continue

            term = final_cross_industry_phrase_cleanup(item.get("term", ""))

            if not term:
                continue

            key = term.lower()
            if key in seen:
                continue

            new_item = dict(item)
            new_item["term"] = term
            cleaned.append(new_item)
            seen.add(key)

        grouped[bucket] = cleaned

    gap["missing_grouped"] = grouped

    shared_cleaned = []
    shared_seen = set()

    for item in gap.get("shared", []) or []:
        if not isinstance(item, dict):
            continue

        term = final_cross_industry_phrase_cleanup(item.get("term", ""))

        if not term:
            continue

        key = term.lower()
        if key in shared_seen:
            continue

        new_item = dict(item)
        new_item["term"] = term
        shared_cleaned.append(new_item)
        shared_seen.add(key)

    gap["shared"] = shared_cleaned

    return gap


if "keyword_gap" in globals() and "_keyword_gap_before_final_cross_industry_cleanup" not in globals():
    _keyword_gap_before_final_cross_industry_cleanup = keyword_gap

    def keyword_gap(site, competitors):
        gap = _keyword_gap_before_final_cross_industry_cleanup(site, competitors)
        return final_clean_gap_terms(gap)


if "final_report_phrase_polish" in globals() and "_final_report_phrase_polish_before_final_cross_industry_cleanup" not in globals():
    _final_report_phrase_polish_before_final_cross_industry_cleanup = final_report_phrase_polish

    def final_report_phrase_polish(html):
        html = _final_report_phrase_polish_before_final_cross_industry_cleanup(html)
        return final_cross_industry_phrase_cleanup(html)

# === FINAL CROSS-INDUSTRY KEYWORD PHRASE CLEANUP ===
def final_cross_industry_phrase_cleanup(text):
    if not text:
        return text

    replacements = {
        # Plumbing
        "Repair Quick": "Emergency Plumbing Repair",
        "repair quick": "emergency plumbing repair",
        "Plumbing Inc": "",
        "plumbing inc": "",
        "Renovations Plumbing": "Plumbing Renovations",
        "renovations plumbing": "plumbing renovations",

        # Roofing
        "Repair Installation": "Roof Repair and Installation",
        "repair installation": "roof repair and installation",
        "Roofing Repair": "Roof Repair",
        "roofing repair": "roof repair",

        # SEO
        "Presence Expert SEO": "Expert SEO Services",
        "presence expert seo": "expert SEO services",
        "SEO Agency Actually": "SEO Agency",
        "seo agency actually": "SEO agency",
        "Expert SEO Honolulu": "Expert SEO in Honolulu",
        "expert seo honolulu": "expert SEO in Honolulu",
        "SEO Honolulu": "Honolulu SEO",
        "seo honolulu": "Honolulu SEO",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    return " ".join(text.split()).strip() if "<" not in text else text


def final_clean_gap_terms(gap):
    if not isinstance(gap, dict):
        return gap

    grouped = gap.get("missing_grouped", {}) or {}

    for bucket in ["service", "location", "commercial"]:
        cleaned = []
        seen = set()

        for item in grouped.get(bucket, []) or []:
            if not isinstance(item, dict):
                continue

            term = final_cross_industry_phrase_cleanup(item.get("term", ""))

            if not term:
                continue

            key = term.lower()
            if key in seen:
                continue

            new_item = dict(item)
            new_item["term"] = term
            cleaned.append(new_item)
            seen.add(key)

        grouped[bucket] = cleaned

    gap["missing_grouped"] = grouped

    shared_cleaned = []
    shared_seen = set()

    for item in gap.get("shared", []) or []:
        if not isinstance(item, dict):
            continue

        term = final_cross_industry_phrase_cleanup(item.get("term", ""))

        if not term:
            continue

        key = term.lower()
        if key in shared_seen:
            continue

        new_item = dict(item)
        new_item["term"] = term
        shared_cleaned.append(new_item)
        shared_seen.add(key)

    gap["shared"] = shared_cleaned

    return gap


if "keyword_gap" in globals() and "_keyword_gap_before_final_cross_industry_cleanup" not in globals():
    _keyword_gap_before_final_cross_industry_cleanup = keyword_gap

    def keyword_gap(site, competitors):
        gap = _keyword_gap_before_final_cross_industry_cleanup(site, competitors)
        return final_clean_gap_terms(gap)


if "final_report_phrase_polish" in globals() and "_final_report_phrase_polish_before_final_cross_industry_cleanup" not in globals():
    _final_report_phrase_polish_before_final_cross_industry_cleanup = final_report_phrase_polish

    def final_report_phrase_polish(html):
        html = _final_report_phrase_polish_before_final_cross_industry_cleanup(html)
        return final_cross_industry_phrase_cleanup(html)

# === FINAL CROSS-INDUSTRY KEYWORD PHRASE CLEANUP ===
def final_cross_industry_phrase_cleanup(text):
    if not text:
        return text

    replacements = {
        # Plumbing
        "Repair Quick": "Emergency Plumbing Repair",
        "repair quick": "emergency plumbing repair",
        "Plumbing Inc": "",
        "plumbing inc": "",
        "Renovations Plumbing": "Plumbing Renovations",
        "renovations plumbing": "plumbing renovations",

        # Roofing
        "Repair Installation": "Roof Repair and Installation",
        "repair installation": "roof repair and installation",
        "Roofing Repair": "Roof Repair",
        "roofing repair": "roof repair",

        # SEO
        "Presence Expert SEO": "Expert SEO Services",
        "presence expert seo": "expert SEO services",
        "SEO Agency Actually": "SEO Agency",
        "seo agency actually": "SEO agency",
        "Expert SEO Honolulu": "Expert SEO in Honolulu",
        "expert seo honolulu": "expert SEO in Honolulu",
        "SEO Honolulu": "Honolulu SEO",
        "seo honolulu": "Honolulu SEO",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    return " ".join(text.split()).strip() if "<" not in text else text


def final_clean_gap_terms(gap):
    if not isinstance(gap, dict):
        return gap

    grouped = gap.get("missing_grouped", {}) or {}

    for bucket in ["service", "location", "commercial"]:
        cleaned = []
        seen = set()

        for item in grouped.get(bucket, []) or []:
            if not isinstance(item, dict):
                continue

            term = final_cross_industry_phrase_cleanup(item.get("term", ""))

            if not term:
                continue

            key = term.lower()
            if key in seen:
                continue

            new_item = dict(item)
            new_item["term"] = term
            cleaned.append(new_item)
            seen.add(key)

        grouped[bucket] = cleaned

    gap["missing_grouped"] = grouped

    shared_cleaned = []
    shared_seen = set()

    for item in gap.get("shared", []) or []:
        if not isinstance(item, dict):
            continue

        term = final_cross_industry_phrase_cleanup(item.get("term", ""))

        if not term:
            continue

        key = term.lower()
        if key in shared_seen:
            continue

        new_item = dict(item)
        new_item["term"] = term
        shared_cleaned.append(new_item)
        shared_seen.add(key)

    gap["shared"] = shared_cleaned

    return gap


if "keyword_gap" in globals() and "_keyword_gap_before_final_cross_industry_cleanup" not in globals():
    _keyword_gap_before_final_cross_industry_cleanup = keyword_gap

    def keyword_gap(site, competitors):
        gap = _keyword_gap_before_final_cross_industry_cleanup(site, competitors)
        return final_clean_gap_terms(gap)


if "final_report_phrase_polish" in globals() and "_final_report_phrase_polish_before_final_cross_industry_cleanup" not in globals():
    _final_report_phrase_polish_before_final_cross_industry_cleanup = final_report_phrase_polish

    def final_report_phrase_polish(html):
        html = _final_report_phrase_polish_before_final_cross_industry_cleanup(html)
        return final_cross_industry_phrase_cleanup(html)

# === SEO INDUSTRY PHRASE POLISH ===
def polish_seo_industry_phrases(text):
    if not text:
        return text

    replacements = {
        "Honululu": "Honolulu",
        "honululu": "Honolulu",

        "Local SEO Honululu": "Local SEO in Honolulu",
        "Local SEO Honolulu": "Local SEO in Honolulu",
        "local seo honululu": "local SEO in Honolulu",
        "local seo honolulu": "local SEO in Honolulu",

        "SEO Honululu": "Honolulu SEO",
        "SEO Honolulu": "Honolulu SEO",
        "seo honululu": "Honolulu SEO",
        "seo honolulu": "Honolulu SEO",

        "SEO Company Honolulu": "SEO Company in Honolulu",
        "seo company honolulu": "SEO company in Honolulu",

        "Marketing Agency Honolulu": "Marketing Agency in Honolulu",
        "marketing agency honolulu": "marketing agency in Honolulu",

        "Looking Local SEO": "Local SEO Services",
        "looking local seo": "local SEO services",

        "Web Design Local": "Local Web Design",
        "web design local": "local web design",

        "Offering SEO": "SEO Services",
        "offering seo": "SEO services",

        "Agency Offering SEO": "SEO Agency",
        "agency offering seo": "SEO agency",

        "Marketing Agency Offering": "Marketing Agency Services",
        "marketing agency offering": "marketing agency services",

        "Point SEO": "SEO Services",
        "point seo": "SEO services",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    return " ".join(text.split()).strip() if "<" not in text else text


if "keyword_gap" in globals() and "_keyword_gap_before_seo_phrase_polish" not in globals():
    _keyword_gap_before_seo_phrase_polish = keyword_gap

    def keyword_gap(site, competitors):
        gap = _keyword_gap_before_seo_phrase_polish(site, competitors)

        if isinstance(gap, dict):
            grouped = gap.get("missing_grouped", {}) or {}

            for bucket in ["service", "location", "commercial"]:
                cleaned = []
                seen = set()

                for item in grouped.get(bucket, []) or []:
                    if not isinstance(item, dict):
                        continue

                    term = polish_seo_industry_phrases(item.get("term", "")).strip()

                    if not term:
                        continue

                    key = term.lower()
                    if key in seen:
                        continue

                    new_item = dict(item)
                    new_item["term"] = term
                    cleaned.append(new_item)
                    seen.add(key)

                grouped[bucket] = cleaned

            gap["missing_grouped"] = grouped

        return gap


if "final_report_phrase_polish" in globals() and "_final_report_phrase_polish_before_seo_phrase_polish" not in globals():
    _final_report_phrase_polish_before_seo_phrase_polish = final_report_phrase_polish

    def final_report_phrase_polish(html):
        html = _final_report_phrase_polish_before_seo_phrase_polish(html)
        return polish_seo_industry_phrases(html)

# Allow HEAD checks on /analyze without triggering a 405.
from fastapi import Response as _FastAPIResponse
from business_competitor_finder import find_business_competitors

@app.head("/analyze", include_in_schema=False)
async def _analyze_head():
    return _FastAPIResponse(status_code=200)


# === CLIENT-READY KEYWORD CLEANUP + TARGET KEYWORDS V2 ===

CLIENT_READY_KEYWORD_CLEANUP_V2 = True

BAD_KEYWORD_EXACT_V2 = {
    "plumber free",
    "charges drains",
    "done plumbers",
    "plumbing experts serve",
    "raton trusted plumber",
    "raton plumbing experts",
    "location local plumber",
    "expert plumbing trenchless",
    "plumber southern",
    "repair quick",
    "plumbing inc",
    "total plumbing",
    "plumbing image",
    "image",
    "total",
    "location",
    "nearby",
    "local",
}

BAD_KEYWORD_CONTAINS_V2 = {
    "charges drains",
    "done plumbers",
    "professionals years experience",
    "roof babe roof",
    "scroll back up",
    "click here",
    "read more",
    "learn more",
}

KEYWORD_REPLACEMENTS_V2 = {
    "raton trusted plumber": "Trusted Boca Raton Plumber",
    "raton plumbing experts": "Boca Raton Plumbing Experts",
    "plumbing experts serve": "Plumbing Experts",
    "plumber free": "Free Plumbing Estimates",
    "charges drains": "Drain Cleaning",
    "leaks repairs": "Leak Repair",
    "done plumbers": "Professional Plumbing Services",
    "expert plumbing trenchless": "Trenchless Plumbing Services",
    "plumber southern": "Southern California Plumber",
    "location local plumber": "Local Plumbing Services",
    "plumbing los angeles": "Los Angeles Plumbing Services",
    "boca raton plumbing": "Boca Raton Plumbing Services",
}

SERVICE_TARGET_HINTS_V2 = [
    "Emergency Plumbing",
    "Drain Cleaning",
    "Leak Repair",
    "Water Heater Repair",
    "Trenchless Plumbing",
    "Plumbing Services",
    "Commercial Plumbing",
    "Residential Plumbing",
    "Pipe Repair",
    "Free Plumbing Estimates",
]

def _title_case_keyword_v2(text):
    small = {"and", "or", "of", "the", "in", "to", "for", "a", "an"}
    parts = str(text or "").split()
    output = []
    for i, part in enumerate(parts):
        if i > 0 and part.lower() in small:
            output.append(part.lower())
        else:
            output.append(part[:1].upper() + part[1:])
    return " ".join(output)

def client_ready_keyword_phrase_v2(value):
    raw = str(value or "").strip()
    if not raw:
        return ""

    normalized = " ".join(raw.lower().replace("_", " ").replace("|", " ").split())

    if not normalized:
        return ""

    if normalized in KEYWORD_REPLACEMENTS_V2:
        return KEYWORD_REPLACEMENTS_V2[normalized]

    if normalized in BAD_KEYWORD_EXACT_V2:
        return ""

    if any(bad in normalized for bad in BAD_KEYWORD_CONTAINS_V2):
        return ""

    words = normalized.split()

    # Remove fragments that are clearly chopped from a location phrase.
    chopped_starts = (
        "raton trusted",
        "raton plumbing",
        "angeles plumbing",
        "southern plumber",
    )
    if normalized.startswith(chopped_starts):
        return ""

    weak_pairs = {
        ("plumber", "free"),
        ("charges", "drains"),
        ("done", "plumbers"),
        ("total", "plumbing"),
        ("plumbing", "image"),
        ("repair", "quick"),
    }
    if len(words) == 2 and tuple(words) in weak_pairs:
        return ""

    # Avoid single generic words as SEO recommendations.
    if len(words) == 1 and normalized in {"image", "total", "location", "local", "nearby", "services"}:
        return ""

    return _title_case_keyword_v2(raw)

def clean_page_keywords_for_report_v2(page):
    if not isinstance(page, dict):
        return page

    page = dict(page)

    for key in ("keywords", "geo_phrases", "current_keywords", "shared_keywords"):
        values = page.get(key)
        if isinstance(values, list):
            cleaned = []
            seen = set()
            for item in values:
                phrase = client_ready_keyword_phrase_v2(item)
                if not phrase:
                    continue
                dedupe = phrase.lower()
                if dedupe in seen:
                    continue
                seen.add(dedupe)
                cleaned.append(phrase)
            page[key] = cleaned

    return page

def clean_keyword_gap_phrases_for_report_v2(gap):
    if not isinstance(gap, dict):
        return gap

    def clean_item(item):
        if isinstance(item, str):
            return client_ready_keyword_phrase_v2(item)

        if isinstance(item, dict):
            new_item = dict(item)
            text_keys = ("keyword", "term", "phrase", "label", "text", "name")

            found_text_key = False
            for key in text_keys:
                if key in new_item:
                    found_text_key = True
                    cleaned = client_ready_keyword_phrase_v2(new_item.get(key))
                    if not cleaned:
                        return None
                    new_item[key] = cleaned

            # Some bubble dicts use title/display/value style keys.
            for key in ("title", "display", "value"):
                if key in new_item and isinstance(new_item.get(key), str):
                    cleaned = client_ready_keyword_phrase_v2(new_item.get(key))
                    if not cleaned:
                        return None
                    new_item[key] = cleaned
                    found_text_key = True

            if not found_text_key:
                joined = " ".join(str(v or "") for v in new_item.values()).lower()
                if any(bad in joined for bad in BAD_KEYWORD_EXACT_V2 | BAD_KEYWORD_CONTAINS_V2):
                    return None

            return new_item

        return item

    def clean_list(items):
        output = []
        seen = set()

        for item in items:
            cleaned = clean_item(item)

            if not cleaned:
                continue

            if isinstance(cleaned, str):
                key = cleaned.lower()
            elif isinstance(cleaned, dict):
                key = str(sorted(cleaned.items())).lower()
            else:
                key = repr(cleaned).lower()

            if key in seen:
                continue

            seen.add(key)
            output.append(cleaned)

        return output

    for key, value in list(gap.items()):
        if isinstance(value, list):
            gap[key] = clean_list(value)
        elif isinstance(value, dict):
            for subkey, subvalue in list(value.items()):
                if isinstance(subvalue, list):
                    value[subkey] = clean_list(subvalue)

    return gap

def _collect_gap_terms_v2(gap):
    terms = []

    def add_value(value):
        if isinstance(value, str):
            cleaned = client_ready_keyword_phrase_v2(value)
            if cleaned:
                terms.append(cleaned)
        elif isinstance(value, dict):
            for key in ("keyword", "term", "phrase", "label", "text", "name", "title", "display", "value"):
                if key in value:
                    cleaned = client_ready_keyword_phrase_v2(value.get(key))
                    if cleaned:
                        terms.append(cleaned)
                        break
        elif isinstance(value, list):
            for item in value:
                add_value(item)
        elif isinstance(value, dict):
            for item in value.values():
                add_value(item)

    if isinstance(gap, dict):
        for value in gap.values():
            add_value(value)

    output = []
    seen = set()
    for term in terms:
        key = term.lower()
        if key not in seen:
            seen.add(key)
            output.append(term)

    return output

def build_recommended_target_keywords_html(site, gap):
    terms = _collect_gap_terms_v2(gap)

    if not terms:
        return ""

    primary = None
    secondary = []

    # Prefer a location/service phrase if available.
    for term in terms:
        low = term.lower()
        if any(x in low for x in ("plumbing", "plumber", "drain", "repair", "water heater", "trenchless")):
            primary = term
            break

    if not primary:
        primary = terms[0]

    for term in terms:
        if term.lower() == primary.lower():
            continue
        if len(secondary) >= 5:
            break
        secondary.append(term)

    # Add a few stable client-friendly service targets when relevant.
    for hint in SERVICE_TARGET_HINTS_V2:
        if len(secondary) >= 5:
            break
        if hint.lower() != primary.lower() and hint.lower() not in {s.lower() for s in secondary}:
            secondary.append(hint)

    secondary_items = "".join(f"<li>{term}</li>" for term in secondary[:5])

    return f"""
    <div class="section">
        <h2>Recommended Target Keywords</h2>
        <div class="keyword-box">
            <div class="keyword-group">
                <h3 class="keyword-group-title">Primary Target</h3>
                <div class="chip-wrap">
                    <span class="chip chip-high top-chip">🔥 {primary}</span>
                </div>
            </div>
            <div class="keyword-group">
                <h3 class="keyword-group-title">Secondary Targets</h3>
                <ul class="quick-wins-list">
                    {secondary_items}
                </ul>
            </div>
        </div>
    </div>
    """

# Wrap the final keyword_gap function after all previous cleanup wrappers.
try:
    _keyword_gap_before_client_ready_cleanup_v2
except NameError:
    _keyword_gap_before_client_ready_cleanup_v2 = keyword_gap

    def keyword_gap(site, competitors):
        cleaned_site = clean_page_keywords_for_report_v2(site)
        cleaned_competitors = [
            clean_page_keywords_for_report_v2(c) if isinstance(c, dict) else c
            for c in (competitors or [])
        ]
        gap = _keyword_gap_before_client_ready_cleanup_v2(cleaned_site, cleaned_competitors)
        return clean_keyword_gap_phrases_for_report_v2(gap)


# === FINAL HTML RESPONSE POLISH MIDDLEWARE V3 ===
# Final client-facing cleanup for rendered report HTML.

def final_polish_report_html_v3(html):
    import re

    html = str(html or "")

    replacements = {
        "Keyword Gap": "Keyword Opportunities",
        "Current Page Keywords": "Keywords Already Found",
        "Shared Keywords": "Shared Topic Signals",
        "Technical Details": "Technical SEO Snapshot",
        "SEO Action Plan": "Priority Growth Plan",

        "Detection Repairs": "Leak Detection and Repair",
        "Installation Replacements": "Installation and Replacement",
        "Island Plumbing": "Long Island Plumbing",
        "Plumbing Long": "Plumbing Long Island",
        "Plumbing Drains": "Drain Cleaning",
        "Drains Leaks": "Drain and Leak Repair",
        "Repairs Done": "Plumbing Repairs",
        "Plumbers Proudly": "Professional Plumbers",
        "Plumber Boca": "Boca Raton Plumber",
        "Raton Plumber": "Boca Raton Plumber",
    }

    for old, new in replacements.items():
        html = html.replace(old, new)

    # Match score table totals to the visible score cards.
    card_scores = re.findall(r'<div class="score">\s*(\d+)\s*</div>', html)

    total_row_match = re.search(
        r'(<tr>\s*<td><strong>Total</strong></td>)(.*?)(</tr>)',
        html,
        flags=re.DOTALL,
    )

    if total_row_match and card_scores:
        middle = total_row_match.group(2)
        scores = list(card_scores[:2])

        def replace_total_score(match):
            if scores:
                return "<strong>" + scores.pop(0) + "</strong>"
            try:
                value = int(match.group(1))
                if value >= 100:
                    value = 98
                return "<strong>" + str(value) + "</strong>"
            except Exception:
                return match.group(0)

        new_middle = re.sub(
            r"<strong>\s*(\d+)\s*</strong>",
            replace_total_score,
            middle,
        )

        new_total_row = total_row_match.group(1) + new_middle + total_row_match.group(3)
        html = html[:total_row_match.start()] + new_total_row + html[total_row_match.end():]

    # Add Recommended Target Keywords if missing.
    if "Recommended Target Keywords" not in html:
        chip_terms = re.findall(
            r'<span class="chip[^"]*">\s*(?:🔥|⭐|🔵)?\s*([^<]+?)\s*</span>',
            html,
            flags=re.DOTALL,
        )

        cleaned_terms = []
        seen = set()
        bad_terms = {"plumbing", "hvac", "team plumbing"}

        for term in chip_terms:
            term = " ".join(term.split())
            if not term:
                continue
            if term.lower() in bad_terms:
                continue
            if term.lower() in seen:
                continue
            seen.add(term.lower())
            cleaned_terms.append(term)

        if cleaned_terms:
            primary = cleaned_terms[0]
            secondary = cleaned_terms[1:6]

            secondary_html = "".join("<li>" + term + "</li>" for term in secondary)

            target_block = (
                '<div class="section">'
                '<h2>Recommended Target Keywords</h2>'
                '<div class="keyword-box">'
                '<div class="keyword-group">'
                '<h3 class="keyword-group-title">Primary Target</h3>'
                '<div class="chip-wrap">'
                '<span class="chip chip-high top-chip">🔥 ' + primary + '</span>'
                '</div>'
                '</div>'
                '<div class="keyword-group">'
                '<h3 class="keyword-group-title">Secondary Targets</h3>'
                '<ul class="quick-wins-list">' + secondary_html + '</ul>'
                '</div>'
                '</div>'
                '</div>'
            )

            marker = '<div class="section">\n            <h2>Quick Wins</h2>'
            if marker in html:
                html = html.replace(marker, target_block + "\n" + marker, 1)

    return html


@app.middleware("http")
async def final_html_report_polish_middleware_v3(request, call_next):
    from starlette.responses import Response

    response = await call_next(request)

    path = request.url.path or ""
    content_type = response.headers.get("content-type", "")

    if "text/html" not in content_type:
        return response

    if not (
        path == "/analyze"
        or path.startswith("/history")
        or path.startswith("/reports")
    ):
        return response

    body = b""
    async for chunk in response.body_iterator:
        body += chunk

    html = body.decode("utf-8", errors="replace")
    html = final_polish_report_html_v3(html)
    html = final_weak_competitor_keyword_safety_html(html)
    html = final_keyword_dedupe_and_industry_polish_html(html)
    html = final_fix_empty_recommended_keywords_html(html)
    html = final_dedupe_target_keywords_html(html)
    html = final_no_gap_keyword_message_polish_html(html)
    html = final_add_additional_keyword_ideas_html(html)
    html = final_replace_empty_gap_buckets_with_ideas_html(html)
    html = final_keyword_strategy_clarity_html(html)
    html = final_keyword_strategy_language_html(html)
    html = final_remove_keyword_strategy_overlap_html(html)
    html = final_industry_weighted_keyword_terms_html(html)
    html = final_current_report_cleanup_html(html)
    html = final_save_good_local_keywords_html(html)
    html = final_force_priority_recommendations_fillout_html(html)
    html = final_fluffy_keyword_guard_html(html)
    html = final_force_roofing_terms_override_html(html)
    html = final_cesspool_industry_override_html(html)
    html = final_blocked_client_crawl_mode_html(html)
    html = final_force_blocked_client_crawl_mode_html(html)
    html = final_roofing_keyword_contamination_guard_html(html)
    html = final_roofing_duplicate_phrase_cleanup_html(html)
    html = final_plumbing_keyword_quality_guard_html(html)
    html = final_plumbing_layout_action_cleanup_html(html)
    html = final_plumbing_geo_action_polish_html(html)
    html = final_force_plumbing_geo_polish_html(html)
    html = final_client_location_geo_guard_html(html)
    html = final_service_geo_split_cleanup_html(html)
    html = final_priority_plan_numbering_fix_html(html)
    html = final_target_keywords_quickwins_layout_html(html)
    html = final_fix_priority_recommendations_layout_retry_html(html)
    html = final_priority_recommendations_layout_v4_html(html)
    html = final_keyword_section_clarity_html(html)

    headers = dict(response.headers)
    headers.pop("content-length", None)

    return Response(
        content=html,
        status_code=response.status_code,
        headers=headers,
        media_type="text/html",
    )


# === WEAK COMPETITOR KEYWORD SAFETY ===

def final_weak_competitor_keyword_safety_html(html):
    import re

    html = str(html or "")

    # Remove / replace obvious competitor-brand and crawl-junk phrases.
    replacements = {
        "Zsl Plumbing Trusted": "Reliable Plumbing Services",
        "ZSL Plumbing Trusted": "Reliable Plumbing Services",
        "Plumbing Trusted Plumbers": "Trusted Plumbing Services",
        "Plumbing Trusted": "Trusted Plumbing Services",
        "Plumbers Zsl": "Professional Plumbers",
        "Plumbers ZSL": "Professional Plumbers",
        "Zsl Plumbing Suffolk": "Suffolk County Plumbing",
        "ZSL Plumbing Suffolk": "Suffolk County Plumbing",
        "Zsl Plumbing": "Plumbing Services",
        "ZSL Plumbing": "Plumbing Services",
        "Plumbing Logo": "Plumbing Services",
        "Hotels Plumbing": "Commercial Plumbing",
        "Plumbing Needs": "Plumbing Services",
        "Trusted Plumbers Suffolk": "Trusted Plumbers in Suffolk County",
        "Plumbing Solutions Suffolk": "Plumbing Solutions in Suffolk County",
        "Plumbing Suffolk": "Suffolk County Plumbing",
        "Plumbers Suffolk": "Suffolk County Plumbers",
    }

    for old, new in replacements.items():
        html = html.replace(old, new)

    # If report says competitor crawl is limited, prevent target keyword block
    # from looking like a final strategy.
    weak_markers = [
        "competitor crawl appears limited",
        "competitor-based recommendations are limited",
        "Use a stronger competitor page for accurate comparison",
        "technical crawl check only",
    ]

    is_weak = any(marker.lower() in html.lower() for marker in weak_markers)

    if is_weak:
        # Replace Recommended Target Keywords section title and intro behavior.
        html = html.replace(
            "<h2>Recommended Target Keywords</h2>",
            "<h2>Provisional Keyword Ideas</h2>"
        )

        warning = (
            '<p class="empty-note" style="margin-bottom:14px;">'
            '<strong>Note:</strong> These are provisional ideas only because the competitor crawl appears limited. '
            'Rerun the report with a stronger direct competitor before using these as final SEO targets.'
            '</p>'
        )

        marker = '<div class="keyword-box"><div class="keyword-group"><h3 class="keyword-group-title">Primary Target</h3>'
        if marker in html and "These are provisional ideas only" not in html:
            html = html.replace(
                '<div class="keyword-box"><div class="keyword-group"><h3 class="keyword-group-title">Primary Target</h3>',
                '<div class="keyword-box">' + warning + '<div class="keyword-group"><h3 class="keyword-group-title">Primary Target</h3>',
                1
            )

        # Make action plan avoid sounding like it is recommending the weak competitor's terms.
        html = re.sub(
            r'<li>Work “.*?” into the page only if it accurately matches the client’s services and location targeting\.</li>',
            '<li>Do not rely on competitor keyword gaps yet. Rerun the report with a stronger direct competitor before choosing final target keywords.</li>',
            html
        )

        html = re.sub(
            r'Start by reviewing <strong>.*?</strong>\. Use it only where it fits naturally, then review the Keyword Opportunities section above for other relevant service, location, and commercial terms\.',
            'Treat the Keyword Opportunities section as provisional. Use a stronger competitor page before turning these terms into final recommendations.',
            html
        )

    # Clean duplicate phrase accidents.
    duplicate_fixes = {
        "Reliable Plumbing Services Services": "Reliable Plumbing Services",
        "Trusted Plumbing Services Services": "Trusted Plumbing Services",
        "Plumbing Services Services": "Plumbing Services",
        "Suffolk County Plumbing County": "Suffolk County Plumbing",
    }

    for old, new in duplicate_fixes.items():
        html = html.replace(old, new)

    return html


# === FINAL KEYWORD DEDUPE + INDUSTRY POLISH ===

def final_keyword_dedupe_and_industry_polish_html(html):
    import re

    html = str(html or "")

    replacements = {
        "Plumbing Installs": "Plumbing Installation",
        "Suffolk County Plumbers County": "Suffolk County Plumbers",
        "Reliable Plumbing Services Services": "Reliable Plumbing Services",
        "Trusted Plumbing Services Services": "Trusted Plumbing Services",
        "Plumbing Services Services": "Plumbing Services",
        "Roof Maintenance": "Plumbing Maintenance",
        "Roof Repair": "Plumbing Repair",
        "Roof Replacement": "Plumbing Replacement",
    }

    for old, new in replacements.items():
        html = html.replace(old, new)

    # Remove duplicate chip spans by visible text.
    chip_pattern = re.compile(r'(<span class="chip[^"]*">\s*(?:🔥|⭐|🔵)?\s*([^<]+?)\s*</span>)', re.DOTALL)
    seen = set()

    def dedupe_chip(match):
        full = match.group(1)
        label = " ".join(match.group(2).split()).lower()

        # Keep generic shared/current terms, but dedupe repeated opportunity chips.
        if label in seen:
            return ""

        seen.add(label)
        return full

    html = chip_pattern.sub(dedupe_chip, html)

    # Clean duplicate li items in Recommended Target Keywords.
    def dedupe_ul(match):
        ul_content = match.group(1)
        items = re.findall(r"<li>(.*?)</li>", ul_content, flags=re.DOTALL)

        seen_items = set()
        cleaned_items = []

        for item in items:
            clean = re.sub(r"<.*?>", "", item)
            clean = " ".join(clean.split())
            key = clean.lower()

            if not clean or key in seen_items:
                continue

            if key in {
                "plumbing services",
                "trusted plumbing services",
                "reliable plumbing",
            } and len(cleaned_items) >= 2:
                continue

            seen_items.add(key)
            cleaned_items.append(clean)

        return "<ul class=\"quick-wins-list\">" + "".join(f"<li>{item}</li>" for item in cleaned_items[:5]) + "</ul>"

    html = re.sub(
        r'<ul class="quick-wins-list">(.*?)</ul>',
        dedupe_ul,
        html,
        flags=re.DOTALL,
        count=1
    )

    # Improve primary target when it is too generic.
    generic_primary_patterns = [
        "Reliable Plumbing Services",
        "Trusted Plumbing Services",
        "Plumbing Services",
    ]

    preferred = None
    for candidate in [
        "Suffolk County Plumbing",
        "Trusted Plumbers in Suffolk County",
        "Emergency Plumber",
        "Emergency Plumbers",
        "Plumbing Repair",
        "Drain Cleaning",
    ]:
        if candidate in html:
            preferred = candidate
            break

    if preferred:
        for generic in generic_primary_patterns:
            html = html.replace(
                f'<span class="chip chip-high top-chip">🔥 {generic}</span>',
                f'<span class="chip chip-high top-chip">🔥 {preferred}</span>',
                1
            )

    # Final duplicate phrase cleanup.
    duplicate_fixes = {
        "Suffolk County Plumbers County": "Suffolk County Plumbers",
        "Trusted Plumbing Services Trusted Plumbing Services": "Trusted Plumbing Services",
        "Plumbing Services Plumbing Services": "Plumbing Services",
    }

    for old, new in duplicate_fixes.items():
        html = html.replace(old, new)

    return html


# === FIX EMPTY RECOMMENDED TARGET KEYWORDS ===

def final_fix_empty_recommended_keywords_html(html):
    import re

    html = str(html or "")

    if "Recommended Target Keywords" not in html:
        return html

    # Detect empty primary target chip area.
    empty_primary_patterns = [
        '<h3 class="keyword-group-title">Primary Target</h3><div class="chip-wrap"></div>',
        '<h3 class="keyword-group-title">Primary Target</h3>\n                    <div class="chip-wrap"></div>',
    ]

    has_empty_primary = any(pattern in html for pattern in empty_primary_patterns)

    if not has_empty_primary:
        return html

    # Pull first useful secondary target.
    secondary_match = re.search(
        r'<h3 class="keyword-group-title">Secondary Targets</h3>\s*<ul class="quick-wins-list">(.*?)</ul>',
        html,
        flags=re.DOTALL,
    )

    primary = ""

    if secondary_match:
        items = re.findall(r"<li>(.*?)</li>", secondary_match.group(1), flags=re.DOTALL)
        for item in items:
            clean = re.sub(r"<.*?>", "", item)
            clean = " ".join(clean.split())
            if clean and clean.lower() not in {"painting", "plumbing", "hvac"}:
                primary = clean
                break

    # Fallbacks by industry if secondary list is empty.
    if not primary:
        lower_html = html.lower()
        if "painting" in lower_html:
            primary = "Professional Painting Services"
        elif "plumbing" in lower_html:
            primary = "Professional Plumbing Services"
        elif "roof" in lower_html:
            primary = "Roof Repair and Installation"
        else:
            primary = "Primary Service Keyword"

    primary_block = (
        '<h3 class="keyword-group-title">Primary Target</h3>'
        '<div class="chip-wrap">'
        '<span class="chip chip-high top-chip">🔥 ' + primary + '</span>'
        '</div>'
    )

    for pattern in empty_primary_patterns:
        html = html.replace(pattern, primary_block)

    return html


# === FINAL TARGET KEYWORD PRIMARY/SECONDARY DEDUPE ===

def final_dedupe_target_keywords_html(html):
    import re

    html = str(html or "")

    if "Recommended Target Keywords" not in html:
        return html

    primary_match = re.search(
        r'<h3 class="keyword-group-title">Primary Target</h3>.*?<span class="chip[^"]*">\s*(?:🔥|⭐|🔵)?\s*([^<]+?)\s*</span>',
        html,
        flags=re.DOTALL,
    )

    if not primary_match:
        return html

    primary = " ".join(primary_match.group(1).split()).strip().lower()

    def clean_secondary_ul(match):
        ul_content = match.group(1)
        items = re.findall(r"<li>(.*?)</li>", ul_content, flags=re.DOTALL)

        cleaned = []
        seen = set()

        for item in items:
            plain = re.sub(r"<.*?>", "", item)
            plain = " ".join(plain.split()).strip()
            key = plain.lower()

            if not plain:
                continue

            if key == primary:
                continue

            if key in seen:
                continue

            seen.add(key)
            cleaned.append(plain)

        return '<ul class="quick-wins-list">' + "".join(f"<li>{item}</li>" for item in cleaned[:5]) + "</ul>"

    html = re.sub(
        r'<ul class="quick-wins-list">(.*?)</ul>',
        clean_secondary_ul,
        html,
        flags=re.DOTALL,
        count=1,
    )

    return html


# === NO-GAP KEYWORD MESSAGE POLISH ===

def final_no_gap_keyword_message_polish_html(html):
    import re

    html = str(html or "")

    old_note = (
        "No major missing keyword gaps were found. "
        "The pages appear to overlap around the core topic, so use Keywords Already Found and Shared Topic Signals to confirm topical relevance."
    )

    new_note = (
        "No major missing keyword gaps were found. "
        "The page already covers the core service topic well. "
        "Use the Recommended Target Keywords section below to decide which existing terms should be prioritized in headings, service sections, and internal links."
    )

    html = html.replace(old_note, new_note)

    # If Shared Topic Signals has a positive count but no visible chips, make it client-clear.
    shared_sections = re.findall(
        r'(<div class="keyword-group">\s*<h3 class="keyword-group-title">Shared Topic Signals \((\d+)\)</h3>.*?</div>\s*</div>)',
        html,
        flags=re.DOTALL,
    )

    for full_section, count in shared_sections:
        visible_chips = re.findall(r'<span class="chip[^"]*">.*?</span>', full_section, flags=re.DOTALL)

        if int(count or 0) > 0 and not visible_chips:
            fixed_section = re.sub(
                r'Shared Topic Signals \(\d+\)',
                'Shared Topic Signals (0)',
                full_section,
                count=1
            )
            fixed_section = re.sub(
                r'<div class="chip-wrap">.*?</div>',
                '<p class="empty-note">No shared topic signals found.</p>',
                fixed_section,
                count=1,
                flags=re.DOTALL
            )
            html = html.replace(full_section, fixed_section)

    return html


# === ADDITIONAL KEYWORD IDEAS WHEN GAP IS EMPTY ===

def final_add_additional_keyword_ideas_html(html):
    import re

    html = str(html or "")

    if "Additional Keyword Ideas" in html:
        return html

    lower = html.lower()

    # Only add this when the report says there are no major keyword gaps.
    no_gap = (
        "No major missing keyword gaps were found" in html
        or "Service Keywords (0)" in html
    )

    if not no_gap:
        return html

    # Detect industry from visible report content.
    is_painting = any(x in lower for x in [
        "painting company",
        "professional painting",
        "interior painting",
        "exterior painting",
        "house painters",
        "paintings.com",
    ])

    is_roofing = any(x in lower for x in [
        "roofing",
        "roofer",
        "roof repair",
        "roof replacement",
    ])

    is_plumbing = (not vast_report_is_roofing_html(html)) and any(x in lower for x in [
        "plumbing",
        "plumber",
        "drain cleaning",
        "water heater",
    ])

    # Pull detected location-ish phrases from visible text.
    location_candidates = []

    known_locations = [
        "Long Island",
        "Suffolk County",
        "Nassau County",
        "Rocky Point",
        "Mastic Beach",
        "Commack",
        "Boca Raton",
        "Palm Beach County",
        "Broward County",
        "Kansas City",
        "Lake of the Ozarks",
    ]

    for loc in known_locations:
        if loc.lower() in lower and loc not in location_candidates:
            location_candidates.append(loc)

    if is_painting:
        base_terms = [
            "Professional Painting Services",
            "Interior Painting",
            "Exterior Painting",
            "Residential Painting",
            "Commercial Painting",
            "House Painters",
            "Cabinet Painting",
            "Deck Staining",
            "Fence Painting",
        ]
    elif is_roofing:
        base_terms = [
            "Roofing Contractor",
            "Roof Repair",
            "Roof Replacement",
            "Emergency Roof Repair",
            "Commercial Roofing",
            "Residential Roofing",
            "Roof Inspection",
            "Roof Maintenance",
            "Flat Roof Repair",
        ]
    elif is_plumbing:
        base_terms = [
            "Plumbing Services",
            "Emergency Plumber",
            "Drain Cleaning",
            "Leak Repair",
            "Water Heater Repair",
            "Commercial Plumbing",
            "Residential Plumbing",
            "Pipe Repair",
            "Same-Day Plumbing Service",
        ]
    else:
        base_terms = [
            "Professional Services",
            "Residential Services",
            "Commercial Services",
            "Emergency Services",
            "Local Service Provider",
        ]

    ideas = []
    seen = set()

    for term in base_terms:
        if term.lower() not in seen:
            seen.add(term.lower())
            ideas.append(term)

    # Add geo-modified ideas if locations were detected.
    for loc in location_candidates[:3]:
        for term in base_terms[:5]:
            idea = f"{term} {loc}"
            if idea.lower() not in seen:
                seen.add(idea.lower())
                ideas.append(idea)

    # Keep it readable.
    ideas = ideas[:14]

    if not ideas:
        return html

    chips = "".join(
        '<span class="chip chip-medium">⭐ ' + idea + '</span>'
        for idea in ideas
    )

    loc_text = ", ".join(location_candidates) if location_candidates else "No strong location signals detected"

    block = (
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Additional Keyword Ideas</h3>'
        '<p class="signals-intro">'
        'These are unranked keyword ideas based on the detected service category, page content, and location signals. '
        'Use them as content planning prompts, not confirmed keyword gaps.'
        '</p>'
        '<p class="empty-note"><strong>Detected location signals:</strong> ' + loc_text + '</p>'
        '<div class="chip-wrap">' + chips + '</div>'
        '</div>'
    )

    # Insert before Keywords Already Found.
    marker = '<div class="keyword-group">\n                    <h3 class="keyword-group-title">Keywords Already Found'
    if marker in html:
        html = html.replace(marker, block + "\n\n" + marker, 1)
    else:
        # Fallback: insert before Quick Wins.
        marker2 = '<div class="section">\n            <h2>Quick Wins</h2>'
        if marker2 in html:
            html = html.replace(marker2, '<div class="section"><h2>Additional Keyword Ideas</h2><div class="keyword-box">' + block + '</div></div>' + marker2, 1)

    return html


# === REPLACE EMPTY GAP BUCKETS WITH UNRANKED IDEAS ===

def final_replace_empty_gap_buckets_with_ideas_html(html):
    import re

    html = str(html or "")

    if "No major missing keyword gaps were found" not in html:
        return html

    lower = html.lower()

    is_painting = any(x in lower for x in [
        "painting company",
        "professional painting",
        "interior painting",
        "exterior painting",
        "house painters",
    ])

    is_roofing = any(x in lower for x in [
        "roofing",
        "roofer",
        "roof repair",
        "roof replacement",
    ])

    is_plumbing = (not vast_report_is_roofing_html(html)) and any(x in lower for x in [
        "plumbing",
        "plumber",
        "drain cleaning",
        "water heater",
    ])

    locations = []
    for loc in [
        "Long Island",
        "Suffolk County",
        "Nassau County",
        "Rocky Point",
        "Mastic Beach",
        "Commack",
        "Boca Raton",
        "Palm Beach County",
        "Broward County",
        "Kansas City",
        "Lake of the Ozarks",
    ]:
        if loc.lower() in lower and loc not in locations:
            locations.append(loc)

    primary_loc = locations[0] if locations else ""

    if is_painting:
        service_terms = [
            "Professional Painting Services",
            "Interior Painting",
            "Exterior Painting",
            "Residential Painting",
            "Commercial Painting",
            "House Painters",
            "Cabinet Painting",
            "Deck Staining",
        ]
        commercial_terms = [
            "Free Painting Estimate",
            "Painting Contractor",
            "Commercial Painting Contractor",
            "Residential Painting Company",
        ]
    elif is_roofing:
        service_terms = [
            "Roof Repair",
            "Roof Replacement",
            "Roofing Contractor",
            "Emergency Roof Repair",
            "Commercial Roofing",
            "Residential Roofing",
            "Roof Inspection",
            "Flat Roof Repair",
        ]
        commercial_terms = [
            "Free Roofing Estimate",
            "Roofing Contractor",
            "Emergency Roofing Service",
            "Commercial Roofing Contractor",
        ]
    elif is_plumbing:
        service_terms = [
            "Plumbing Services",
            "Emergency Plumber",
            "Drain Cleaning",
            "Leak Repair",
            "Water Heater Repair",
            "Commercial Plumbing",
            "Residential Plumbing",
            "Pipe Repair",
        ]
        commercial_terms = [
            "Free Plumbing Estimate",
            "Emergency Plumbing Service",
            "Same-Day Plumber",
            "Commercial Plumbing Contractor",
        ]
    else:
        service_terms = [
            "Professional Services",
            "Residential Services",
            "Commercial Services",
            "Emergency Services",
        ]
        commercial_terms = [
            "Free Estimate",
            "Local Contractor",
            "Emergency Service",
            "Service Quote",
        ]

    location_terms = []
    if primary_loc:
        for term in service_terms[:6]:
            location_terms.append(f"{term} {primary_loc}")

    if not location_terms and locations:
        location_terms = locations[:6]

    def chips(terms):
        return "".join(
            '<span class="chip chip-medium">⭐ ' + term + '</span>'
            for term in terms[:8]
        )

    service_block = (
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Suggested Service Ideas</h3>'
        '<p class="signals-intro">Unranked service keyword ideas based on the client page and same-topic competitor signals.</p>'
        '<div class="chip-wrap">' + chips(service_terms) + '</div>'
        '</div>'
    )

    location_block = (
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Suggested Location Ideas</h3>'
        '<p class="signals-intro">Unranked geo-modified ideas based on detected location signals.</p>'
        '<div class="chip-wrap">' + chips(location_terms) + '</div>'
        '</div>'
    )

    commercial_block = (
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Suggested Commercial Intent Ideas</h3>'
        '<p class="signals-intro">Unranked buyer-intent ideas that may help with service pages, CTAs, and ad landing pages.</p>'
        '<div class="chip-wrap">' + chips(commercial_terms) + '</div>'
        '</div>'
    )

    # Replace the empty zero-count bucket blocks.
    html = re.sub(
        r'<div class="keyword-group">\s*<h3 class="keyword-group-title">Service Keywords \(0\)</h3>\s*<p class="empty-note">No service keyword gaps found\.</p>\s*</div>',
        service_block,
        html,
        count=1,
        flags=re.DOTALL,
    )

    html = re.sub(
        r'<div class="keyword-group">\s*<h3 class="keyword-group-title">Location Keywords \(0\)</h3>\s*<p class="empty-note">No location keyword gaps found\.</p>\s*</div>',
        location_block,
        html,
        count=1,
        flags=re.DOTALL,
    )

    html = re.sub(
        r'<div class="keyword-group">\s*<h3 class="keyword-group-title">Commercial Keywords \(0\)</h3>\s*<p class="empty-note">No commercial keyword gaps found\.</p>\s*</div>',
        commercial_block,
        html,
        count=1,
        flags=re.DOTALL,
    )

    return html


# === KEYWORD SECTION CLARITY + REMOVE DUPLICATE ADDITIONAL IDEAS ===

def final_keyword_section_clarity_html(html):
    import re

    html = str(html or "")

    # Clarify the main note.
    html = html.replace(
        "No major missing keyword gaps were found. The page already covers the core service topic well. Use the Recommended Target Keywords section below to decide which existing terms should be prioritized in headings, service sections, and internal links.",
        "No major missing keyword gaps were found. The page already covers the core service topic well. The suggested ideas below are unranked content prompts, while Keywords Already Found shows terms already present on the client page."
    )

    # Rename suggested buckets so they do not sound like confirmed gaps.
    html = html.replace(
        "Suggested Service Ideas",
        "Unranked Service Ideas"
    )
    html = html.replace(
        "Suggested Location Ideas",
        "Unranked Location Ideas"
    )
    html = html.replace(
        "Suggested Commercial Intent Ideas",
        "Unranked Buyer-Intent Ideas"
    )

    html = html.replace(
        "Unranked service keyword ideas based on the client page and same-topic competitor signals.",
        "These are suggested service-topic ideas. They are not confirmed missing keywords."
    )
    html = html.replace(
        "Unranked geo-modified ideas based on detected location signals.",
        "These are suggested service-plus-location ideas based on detected geo signals. They are not confirmed missing keywords."
    )
    html = html.replace(
        "Unranked buyer-intent ideas that may help with service pages, CTAs, and ad landing pages.",
        "These are suggested buyer-intent ideas for service pages, CTAs, and ad landing pages. They are not confirmed missing keywords."
    )

    # Remove duplicate Additional Keyword Ideas block when the three suggested buckets already exist.
    if (
        "Unranked Service Ideas" in html
        and "Unranked Location Ideas" in html
        and "Unranked Buyer-Intent Ideas" in html
        and "Additional Keyword Ideas" in html
    ):
        html = re.sub(
            r'<div class="keyword-group"><h3 class="keyword-group-title">Additional Keyword Ideas</h3>.*?</div></div>',
            '',
            html,
            count=1,
            flags=re.DOTALL
        )

    # Clarify Keywords Already Found.
    html = html.replace(
        "These terms are already present on your page and help show current topical relevance.",
        "These terms were found on the client page. They are already being used and can be prioritized in headings, internal links, service sections, or supporting copy."
    )

    return html


# === FINAL KEYWORD STRATEGY CLARITY PATCH ===

def final_keyword_strategy_clarity_html(html):
    import re

    html = str(html or "")

    # Rename the section so it does not imply every chip is a confirmed gap.
    html = html.replace(
        "<h2>Keyword Opportunities</h2>",
        "<h2>Keyword Strategy</h2>"
    )

    # Clearer main note.
    html = html.replace(
        "No major missing keyword gaps were found. The page already covers the core service topic well. Use the Recommended Target Keywords section below to decide which existing terms should be prioritized in headings, service sections, and internal links.",
        "No confirmed competitor keyword gaps were found. The ideas below are planning prompts based on service, location, and buyer-intent patterns. They are not proof that the client page is missing those exact phrases."
    )

    html = html.replace(
        "No major missing keyword gaps were found. The pages appear to overlap around the core topic, so use Keywords Already Found and Shared Topic Signals to confirm topical relevance.",
        "No confirmed competitor keyword gaps were found. The ideas below are planning prompts based on service, location, and buyer-intent patterns. They are not proof that the client page is missing those exact phrases."
    )

    # Rename generated idea buckets.
    heading_replacements = {
        "Suggested Service Ideas": "Service Expansion Ideas",
        "Suggested Location Ideas": "Location Expansion Ideas",
        "Suggested Commercial Intent Ideas": "Buyer-Intent Expansion Ideas",
        "Unranked Service Ideas": "Service Expansion Ideas",
        "Unranked Location Ideas": "Location Expansion Ideas",
        "Unranked Buyer-Intent Ideas": "Buyer-Intent Expansion Ideas",
    }

    for old, new in heading_replacements.items():
        html = html.replace(old, new)

    # Clarify bucket descriptions.
    description_replacements = {
        "Unranked service keyword ideas based on the client page and same-topic competitor signals.":
            "Planning ideas based on the detected service category. These are not confirmed missing keywords.",
        "Unranked geo-modified ideas based on detected location signals.":
            "Planning ideas that combine services with detected locations. These are not confirmed missing keywords.",
        "Unranked buyer-intent ideas that may help with service pages, CTAs, and ad landing pages.":
            "Planning ideas for stronger service pages, CTAs, proposal pages, or ad landing pages.",
        "These are suggested service-topic ideas. They are not confirmed missing keywords.":
            "Planning ideas based on the detected service category. These are not confirmed missing keywords.",
        "These are suggested service-plus-location ideas based on detected geo signals. They are not confirmed missing keywords.":
            "Planning ideas that combine services with detected locations. These are not confirmed missing keywords.",
        "These are suggested buyer-intent ideas for service pages, CTAs, and ad landing pages. They are not confirmed missing keywords.":
            "Planning ideas for stronger service pages, CTAs, proposal pages, or ad landing pages.",
    }

    for old, new in description_replacements.items():
        html = html.replace(old, new)

    # Clarify already-found terms.
    html = html.replace(
        "These terms are already present on your page and help show current topical relevance.",
        "These terms were found on the client page. They are already being used."
    )

    html = html.replace(
        "These terms were found on the client page. They are already being used and can be prioritized in headings, internal links, service sections, or supporting copy.",
        "These terms were found on the client page. They are already being used."
    )

    # Remove duplicate Additional Keyword Ideas block if the cleaner expansion buckets exist.
    if (
        "Service Expansion Ideas" in html
        and "Location Expansion Ideas" in html
        and "Buyer-Intent Expansion Ideas" in html
        and "Additional Keyword Ideas" in html
    ):
        html = re.sub(
            r'<div class="keyword-group"><h3 class="keyword-group-title">Additional Keyword Ideas</h3>.*?</div></div>',
            '',
            html,
            count=1,
            flags=re.DOTALL
        )

    # Add a compact explanation after the legend if not already present.
    helper = (
        '<p class="signals-intro" style="margin-bottom:14px;">'
        '<strong>How to read this:</strong> Expansion Ideas are generated planning prompts. '
        'Keywords Already Found are confirmed terms already used on the client page. '
        'Shared Topic Signals are terms found on both pages.'
        '</p>'
    )

    if "How to read this:" not in html:
        legend_close = '</div>\n\n            \n\n            <div class="keyword-box">'
        if legend_close in html:
            html = html.replace(
                legend_close,
                '</div>\n' + helper + '\n\n            <div class="keyword-box">',
                1
            )

    return html


# === STRATEGIC KEYWORD LANGUAGE CLEANUP ===

def final_keyword_strategy_language_html(html):
    import re

    html = str(html or "")

    # Rename the section away from "opportunities" when some terms are planning ideas.
    html = html.replace("<h2>Keyword Opportunities</h2>", "<h2>Keyword Strategy</h2>")

    # Remove defensive/cop-out language.
    defensive_notes = [
        "No confirmed competitor keyword gaps were found. The ideas below are planning prompts based on service, location, and buyer-intent patterns. They are not proof that the client page is missing those exact phrases.",
        "No major missing keyword gaps were found. The page already covers the core service topic well. Use the Recommended Target Keywords section below to decide which existing terms should be prioritized in headings, service sections, and internal links.",
        "No major missing keyword gaps were found. The page already covers the core service topic well. The suggested ideas below are unranked content prompts, while Keywords Already Found shows terms already present on the client page.",
    ]

    strategic_note = (
        "The page already covers the core topic. The next step is to strengthen the most valuable "
        "service, location, and buyer-intent terms below across headings, service sections, internal links, "
        "image alt text, FAQs, and calls to action."
    )

    for note in defensive_notes:
        html = html.replace(note, strategic_note)

    # Rename idea buckets into stronger strategy buckets.
    heading_replacements = {
        "Suggested Service Ideas": "Service Terms to Strengthen",
        "Suggested Location Ideas": "Location Terms to Strengthen",
        "Suggested Commercial Intent Ideas": "Buyer-Intent Terms to Strengthen",
        "Unranked Service Ideas": "Service Terms to Strengthen",
        "Unranked Location Ideas": "Location Terms to Strengthen",
        "Unranked Buyer-Intent Ideas": "Buyer-Intent Terms to Strengthen",
        "Service Expansion Ideas": "Service Terms to Strengthen",
        "Location Expansion Ideas": "Location Terms to Strengthen",
        "Buyer-Intent Expansion Ideas": "Buyer-Intent Terms to Strengthen",
    }

    for old, new in heading_replacements.items():
        html = html.replace(old, new)

    # Replace weak explanations with client-ready strategy language.
    description_replacements = {
        "Unranked service keyword ideas based on the client page and same-topic competitor signals.":
            "Use these terms to clarify the page’s main services and support stronger topical relevance.",
        "Unranked geo-modified ideas based on detected location signals.":
            "Use these terms for location-focused sections, service-area copy, internal links, or dedicated location pages.",
        "Unranked buyer-intent ideas that may help with service pages, CTAs, and ad landing pages.":
            "Use these terms to improve conversion-focused copy, calls to action, proposal pages, and ad landing pages.",
        "These are suggested service-topic ideas. They are not confirmed missing keywords.":
            "Use these terms to clarify the page’s main services and support stronger topical relevance.",
        "These are suggested service-plus-location ideas based on detected geo signals. They are not confirmed missing keywords.":
            "Use these terms for location-focused sections, service-area copy, internal links, or dedicated location pages.",
        "These are suggested buyer-intent ideas for service pages, CTAs, and ad landing pages. They are not confirmed missing keywords.":
            "Use these terms to improve conversion-focused copy, calls to action, proposal pages, and ad landing pages.",
        "Planning ideas based on the detected service category. These are not confirmed missing keywords.":
            "Use these terms to clarify the page’s main services and support stronger topical relevance.",
        "Planning ideas that combine services with detected locations. These are not confirmed missing keywords.":
            "Use these terms for location-focused sections, service-area copy, internal links, or dedicated location pages.",
        "Planning ideas for stronger service pages, CTAs, proposal pages, or ad landing pages.":
            "Use these terms to improve conversion-focused copy, calls to action, proposal pages, and ad landing pages.",
    }

    for old, new in description_replacements.items():
        html = html.replace(old, new)

    # Remove duplicate Additional Keyword Ideas block if stronger buckets exist.
    if (
        "Service Terms to Strengthen" in html
        and "Location Terms to Strengthen" in html
        and "Buyer-Intent Terms to Strengthen" in html
        and "Additional Keyword Ideas" in html
    ):
        html = re.sub(
            r'<div class="keyword-group"><h3 class="keyword-group-title">Additional Keyword Ideas</h3>.*?</div></div>',
            '',
            html,
            count=1,
            flags=re.DOTALL
        )

    # Better already-found wording.
    html = html.replace(
        "These terms are already present on your page and help show current topical relevance.",
        "These terms were found on the client page and are already supporting topical relevance."
    )

    html = html.replace(
        "These terms were found on the client page. They are already being used.",
        "These terms were found on the client page and are already supporting topical relevance."
    )

    # Add a simple explainer after the legend.
    helper = (
        '<p class="signals-intro" style="margin-bottom:14px;">'
        '<strong>How to read this:</strong> Terms to Strengthen are strategic focus terms for page improvements. '
        'Keywords Already Found are terms already detected on the client page. '
        'Shared Topic Signals are terms found on both pages.'
        '</p>'
    )

    if "How to read this:" not in html:
        legend_close = '</div>\n\n            \n\n            <div class="keyword-box">'
        if legend_close in html:
            html = html.replace(
                legend_close,
                '</div>\n' + helper + '\n\n            <div class="keyword-box">',
                1
            )

    return html


# === REMOVE OVERLAP BETWEEN TERMS TO STRENGTHEN AND ALREADY FOUND ===

def final_remove_keyword_strategy_overlap_html(html):
    import re

    html = str(html or "")

    # Collect terms already found on the client page.
    found_section_match = re.search(
        r'<h3 class="keyword-group-title">Keywords Already Found \(\d+\)</h3>.*?<div class="chip-wrap">(.*?)</div>',
        html,
        flags=re.DOTALL,
    )

    if not found_section_match:
        return html

    found_terms = set()
    for term in re.findall(r'<span class="chip[^"]*">\s*(.*?)\s*</span>', found_section_match.group(1), flags=re.DOTALL):
        clean = re.sub(r'<.*?>', '', term)
        clean = " ".join(clean.split()).strip().lower()
        if clean:
            found_terms.add(clean)

    if not found_terms:
        return html

    # Remove exact duplicates from Service Terms to Strengthen only.
    # Keep Location Terms and Buyer-Intent Terms because those are modified uses.
    service_section_match = re.search(
        r'(<h3 class="keyword-group-title">Service Terms to Strengthen</h3>.*?<div class="chip-wrap">)(.*?)(</div>)',
        html,
        flags=re.DOTALL,
    )

    if service_section_match:
        prefix, chips_html, suffix = service_section_match.groups()

        chips = re.findall(
            r'<span class="chip[^"]*">\s*(?:🔥|⭐|🔵)?\s*([^<]+?)\s*</span>',
            chips_html,
            flags=re.DOTALL,
        )

        kept = []
        seen = set()

        for chip in chips:
            label = " ".join(chip.split()).strip()
            key = label.lower()

            # Remove exact terms that already appear in Keywords Already Found.
            if key in found_terms:
                continue

            if key in seen:
                continue

            seen.add(key)
            kept.append(label)

        # If removing overlap leaves too little, add stronger variants instead.
        fallback_terms = [
            "Professional Painting Services",
            "Local Painting Company",
            "Painting Contractor",
            "Cabinet Painting",
            "Deck Staining",
            "Fence Painting",
            "Drywall Repair",
        ]

        for term in fallback_terms:
            if len(kept) >= 6:
                break
            key = term.lower()
            if key not in seen and key not in found_terms:
                seen.add(key)
                kept.append(term)

        new_chips_html = "".join(
            '<span class="chip chip-medium">⭐ ' + term + '</span>'
            for term in kept[:8]
        )

        html = (
            html[:service_section_match.start()]
            + prefix + new_chips_html + suffix
            + html[service_section_match.end():]
        )

    # Rename already found section slightly so it is clear this is the source inventory.
    html = html.replace(
        "Keywords Already Found",
        "Terms Already Found on Page"
    )

    html = html.replace(
        "These terms were found on the client page and are already supporting topical relevance.",
        "These terms were detected on the client page. Use this as the source inventory, not a separate recommendation list."
    )

    return html


# === MOVE QUICK WINS NEXT TO RECOMMENDED TARGET KEYWORDS ===

def final_target_keywords_quickwins_layout_html(html):
    import re

    html = str(html or "")

    if "target-quickwins-grid" in html:
        return html

    # Find Recommended Target Keywords section.
    target_match = re.search(
        r'(<div class="section"><h2>Recommended Target Keywords</h2>.*?</div>\s*</div>\s*</div>)',
        html,
        flags=re.DOTALL,
    )

    # Find Quick Wins section.
    quick_match = re.search(
        r'(<div class="section">\s*<h2>Quick Wins</h2>.*?</div>\s*</div>\s*</div>)',
        html,
        flags=re.DOTALL,
    )

    if not target_match or not quick_match:
        return html

    target_section = target_match.group(1)
    quick_section = quick_match.group(1)

    # Extract inner keyword box from Recommended Target Keywords.
    target_inner = re.search(
        r'<h2>Recommended Target Keywords</h2>\s*(<div class="keyword-box">.*?</div>)\s*</div>',
        target_section,
        flags=re.DOTALL,
    )

    # Extract inner Quick Wins card/grid.
    quick_inner = re.search(
        r'<h2>Quick Wins</h2>\s*(<div class="compare-grid quick-wins-grid">.*?</div>\s*</div>)',
        quick_section,
        flags=re.DOTALL,
    )

    if not target_inner or not quick_inner:
        return html

    combined_section = (
        '<div class="section target-quickwins-section">'
        '<h2>Priority Recommendations</h2>'
        '<div class="target-quickwins-grid">'
        '<div class="target-panel">'
        '<h3 class="keyword-group-title">Recommended Target Keywords</h3>'
        + target_inner.group(1) +
        '</div>'
        '<div class="quickwins-panel">'
        '<h3 class="keyword-group-title">Quick Wins</h3>'
        + quick_inner.group(1) +
        '</div>'
        '</div>'
        '</div>'
    )

    # Replace target section with combined section.
    html = html[:target_match.start()] + combined_section + html[target_match.end():]

    # Remove original quick wins section after replacement.
    html = html.replace(quick_section, "", 1)

    # Add layout CSS.
    css = """
<style>
.target-quickwins-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 22px;
    align-items: stretch;
}

.target-panel,
.quickwins-panel {
    background: #ffffff;
    border: 1px solid #dbe4f0;
    border-radius: 14px;
    padding: 22px;
}

.target-panel .keyword-box {
    border: 0;
    padding: 0;
    background: transparent;
}

.quickwins-panel .compare-grid,
.quickwins-panel .quick-wins-grid {
    display: block !important;
}

.quickwins-panel .site-card {
    box-shadow: none !important;
    border: 0 !important;
    padding: 0 !important;
}

@media (max-width: 800px) {
    .target-quickwins-grid {
        grid-template-columns: 1fr;
    }
}

.lead-results-list {
    display: grid;
    gap: 16px;
}

.lead-result-card {
    background: #ffffff;
    border: 1px solid #dbe4f0;
    border-radius: 18px;
    padding: 20px;
    box-shadow: 0 8px 24px rgba(15,23,42,.06);
}

.lead-card-top {
    display: flex;
    justify-content: space-between;
    gap: 18px;
    align-items: flex-start;
}

.lead-card-title {
    font-size: 20px;
    font-weight: 900;
    color: #0f172a;
    line-height: 1.25;
}

.lead-card-domain {
    margin-top: 5px;
    color: #64748b;
    font-weight: 800;
}

.lead-card-score {
    min-width: 58px;
    height: 58px;
    border-radius: 999px;
    background: #dcfce7;
    color: #166534;
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 950;
    font-size: 19px;
}

.lead-card-badges {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin: 16px 0;
}

.lead-card-badges span {
    background: #eaf2ff;
    color: #1e3a8a;
    border: 1px solid #bfdbfe;
    border-radius: 999px;
    padding: 7px 10px;
    font-size: 12px;
    font-weight: 900;
}

.lead-card-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 12px;
}

.lead-card-grid div {
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 13px;
    padding: 12px;
    font-size: 14px;
    overflow-wrap: anywhere;
}

.lead-card-grid strong {
    color: #334155;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: .05em;
}

.lead-card-notes {
    margin-top: 14px;
    color: #334155;
    font-size: 14px;
    line-height: 1.55;
    background: #fff;
    border-left: 4px solid #1e3a8a;
    padding: 10px 12px;
}

@media (max-width: 1100px) {
    .lead-card-grid {
        grid-template-columns: 1fr 1fr;
    }
}

@media (max-width: 700px) {
    .lead-card-top {
        flex-direction: column;
    }

    .lead-card-grid {
        grid-template-columns: 1fr;
    }
}



/* === CENTER HOME ANALYZE BUTTON START === */
#analyzeForm > button,
#runBtn {
    display: block;
    margin: 28px auto 0 auto;
    min-width: 160px;
    text-align: center;
}
/* === CENTER HOME ANALYZE BUTTON END === */


/* === CENTER HOME ANALYZE BUTTON ROW START === */
.home-analyze-action-row {
    grid-column: 1 / -1;
    display: flex !important;
    justify-content: center !important;
    align-items: center !important;
    width: 100%;
    margin-top: 28px;
}

.home-analyze-action-row #runBtn,
#analyzeForm #runBtn.home-analyze-button {
    margin: 0 auto !important;
    display: inline-flex !important;
    align-items: center;
    justify-content: center;
    min-width: 170px;
    text-align: center;
}
/* === CENTER HOME ANALYZE BUTTON ROW END === */


/* === FORCE CENTER HOME BUTTON START === */
#analyzeForm #runBtn {
    display: block !important;
    margin-left: auto !important;
    margin-right: auto !important;
}

#analyzeForm .footer-note,
.card .footer-note {
    text-align: center !important;
}
/* === FORCE CENTER HOME BUTTON END === */


/* === FINAL HOME BUTTON CENTER LOCK START === */
#analyzeForm {
    text-align: center !important;
}

#analyzeForm .form-grid,
#analyzeForm .input-card,
#analyzeForm .extra-fields {
    text-align: left !important;
}

#analyzeForm #runBtn {
    display: inline-block !important;
    margin: 28px auto 0 auto !important;
}

#analyzeForm .loading,
.card .footer-note {
    text-align: center !important;
}
/* === FINAL HOME BUTTON CENTER LOCK END === */




/* === LEADBOT SIDEBAR SAVED LEADS BUTTON START === */
.leadbot-saved-leads-sidebar-wrap {
    margin-top: -4px !important;
    margin-bottom: 16px !important;
}

.leadbot-saved-leads-sidebar-btn {
    width: 100% !important;
    min-height: 40px !important;
    padding: 10px 12px !important;
    margin: 0 !important;

    display: flex !important;
    align-items: center !important;
    justify-content: center !important;

    background: #1e3a8a !important;
    color: #ffffff !important;
    border: 0 !important;
    border-radius: 11px !important;

    font-size: 13px !important;
    font-weight: 900 !important;
    line-height: 1.2 !important;
    text-align: center !important;
    text-decoration: none !important;
    white-space: nowrap !important;
}

.leadbot-saved-leads-sidebar-btn:hover {
    background: #172f70 !important;
    color: #ffffff !important;
    text-decoration: none !important;
}
/* === LEADBOT SIDEBAR SAVED LEADS BUTTON END === */


/* === LEADBOT REMOVE SAVED LEADS UI FINAL START === */
.leadbot-crm-save-btn,
.leadbot-crm-link,
.leadbot-search-summary-saved-leads,
.leadbot-sidebar-saved-leads-wrap,
.leadbot-sidebar-saved-leads-btn,
a[href="/lead-bot/my-leads"],
a[href*="/lead-bot/my-leads"] {
    display: none !important;
}
/* === LEADBOT REMOVE SAVED LEADS UI FINAL END === */


/* === LEADBOT LIVE CARD META SNAPSHOT START === */
.leadbot-live-seo-snapshot {
    margin-top: 20px;
    padding: 14px 16px;
    border-radius: 12px;
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    display: grid;
    gap: 10px;
}

.leadbot-live-seo-snapshot div {
    display: block;
    line-height: 1.4;
}

.leadbot-live-seo-snapshot b {
    display: none;
}

.leadbot-live-meta-title {
    font-weight: 850;
    color: #0f172a;
}

.leadbot-live-meta-description {
    color: #102033;
}

/* === LEADBOT LIVE CARD META SNAPSHOT END === */


/* === LEADBOT LIVE ZERO RESULTS EMPTY STATE START === */
.leadbot-zero-results-empty {
    margin: 18px 0 0;
    padding: 18px;
    border-radius: 18px;
    background: #f8fafc;
    border: 1px solid #dbe4f0;
    color: #0f172a;
    box-shadow: 0 10px 24px rgba(15, 23, 42, .06);
}

.leadbot-zero-results-empty h3 {
    margin: 0 0 8px;
    font-size: 18px;
    line-height: 1.2;
    color: #0f172a;
}

.leadbot-zero-results-empty p {
    margin: 0 0 10px;
    color: #475569;
    font-size: 13px;
    line-height: 1.45;
}

.leadbot-zero-results-empty ul {
    margin: 8px 0 0 18px;
    padding: 0;
    color: #334155;
    font-size: 13px;
    line-height: 1.5;
}

.leadbot-zero-results-empty li {
    margin: 4px 0;
}
/* === LEADBOT LIVE ZERO RESULTS EMPTY STATE END === */


.auth-brand-text {
    text-align: center;
    margin-bottom: 16px;
}
.auth-brand-name {
    display: inline-block;
    color: #0f172a;
    font-size: 24px;
    font-weight: 900;
    letter-spacing: -0.03em;
    text-decoration: none;
}
.auth-brand-subtitle {
    margin-top: 4px;
    color: #64748b;
    font-size: 13px;
    font-weight: 650;
}

</style>
"""

    if ".target-quickwins-grid" not in html:
        html = html.replace("</head>", extra_css + "\n</head>", 1)
def final_fix_priority_recommendations_layout_retry_html(html):
    import re

    html = str(html or "")

    if "Priority Recommendations" not in html:
        return html

    primary_match = re.search(
        r'<h3 class="keyword-group-title">Primary Target</h3>\s*<div class="chip-wrap">(.*?)</div>',
        html,
        flags=re.DOTALL,
    )

    secondary_match = re.search(
        r'<h3 class="keyword-group-title">Secondary Targets</h3>\s*<ul class="quick-wins-list">(.*?)</ul>',
        html,
        flags=re.DOTALL,
    )

    quickwins_match = re.search(
        r'<h3 class="keyword-group-title">Quick Wins</h3>.*?<ul class="quick-wins-list">(.*?)</ul>',
        html,
        flags=re.DOTALL,
    )

    if not primary_match or not quickwins_match:
        return html

    primary_html = primary_match.group(1).strip()
    secondary_html = secondary_match.group(1).strip() if secondary_match else ""
    quickwins_html = quickwins_match.group(1).strip()

    if not primary_html:
        primary_html = '<span class="chip chip-high top-chip">🔥 Primary Service Keyword</span>'

    new_section = (
        '<div class="section target-quickwins-section">'
        '<h2>Priority Recommendations</h2>'
        '<div class="target-quickwins-grid">'
        '<div class="target-panel">'
        '<h3 class="keyword-group-title">Recommended Target Keywords</h3>'
        '<div class="keyword-box clean-priority-box">'
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Primary Target</h3>'
        '<div class="chip-wrap">' + primary_html + '</div>'
        '</div>'
    )

    if secondary_html:
        new_section += (
            '<div class="keyword-group">'
            '<h3 class="keyword-group-title">Secondary Targets</h3>'
            '<ul class="quick-wins-list">' + secondary_html + '</ul>'
            '</div>'
        )

    new_section += (
        '</div>'
        '</div>'
        '<div class="quickwins-panel">'
        '<h3 class="keyword-group-title">Quick Wins</h3>'
        '<div class="site-card clean-priority-card">'
        '<ul class="quick-wins-list">' + quickwins_html + '</ul>'
        '</div>'
        '</div>'
        '</div>'
        '</div>'
    )

    html = re.sub(
        r'<div class="section target-quickwins-section">.*?(?=<div class="section">\s*<h2>Technical SEO Snapshot</h2>)',
        new_section,
        html,
        count=1,
        flags=re.DOTALL,
    )

    html = re.sub(
        r'<div class="section"><h2>Recommended Target Keywords</h2>.*?(?=<div class="section">)',
        '',
        html,
        count=1,
        flags=re.DOTALL,
    )

    html = re.sub(
        r'<div class="section">\s*<h2>Quick Wins</h2>.*?(?=<div class="section">)',
        '',
        html,
        count=1,
        flags=re.DOTALL,
    )

    css = """
<style>
.target-quickwins-grid {
    display: grid !important;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) !important;
    gap: 22px !important;
    align-items: stretch !important;
}

.target-panel,
.quickwins-panel {
    background: #ffffff !important;
    border: 1px solid #dbe4f0 !important;
    border-radius: 14px !important;
    padding: 22px !important;
    min-width: 0 !important;
}

.clean-priority-box {
    border: 0 !important;
    padding: 0 !important;
    background: transparent !important;
}

.clean-priority-card {
    border: 0 !important;
    box-shadow: none !important;
    padding: 0 !important;
}

@media (max-width: 800px) {
    .target-quickwins-grid {
        grid-template-columns: 1fr !important;
    }
}


</style>
"""

    if "clean-priority-box" not in html.split("</head>")[0]:
        html = html.replace("</head>", css + "\n</head>", 1)

    html = html.replace("</div></div></body></html>", "</body></html>")

    return html


# === PRIORITY RECOMMENDATIONS FINAL LAYOUT REPAIR V4 ===

def final_priority_recommendations_layout_v4_html(html):
    import re

    html = str(html or "")

    if "Priority Recommendations" not in html:
        return html

    primary_match = re.search(
        r'<h3 class="keyword-group-title">Primary Target</h3>\s*<div class="chip-wrap">(.*?)</div>',
        html,
        flags=re.DOTALL,
    )

    secondary_match = re.search(
        r'<h3 class="keyword-group-title">Secondary Targets</h3>\s*<ul class="quick-wins-list">(.*?)</ul>',
        html,
        flags=re.DOTALL,
    )

    quickwins_match = re.search(
        r'<h3 class="keyword-group-title">Quick Wins</h3>.*?<ul class="quick-wins-list">(.*?)</ul>',
        html,
        flags=re.DOTALL,
    )

    if not primary_match or not quickwins_match:
        return html

    primary_html = primary_match.group(1).strip()
    secondary_html = secondary_match.group(1).strip() if secondary_match else ""
    quickwins_html = quickwins_match.group(1).strip()

    if not primary_html:
        primary_html = '<span class="chip chip-high top-chip">🔥 Primary Service Keyword</span>'

    new_section = (
        '<div class="section target-quickwins-section">'
        '<h2>Priority Recommendations</h2>'
        '<div class="target-quickwins-grid">'
            '<div class="target-panel">'
                '<h3 class="keyword-group-title">Recommended Target Keywords</h3>'
                '<div class="keyword-box clean-priority-box">'
                    '<div class="keyword-group">'
                        '<h3 class="keyword-group-title">Primary Target</h3>'
                        '<div class="chip-wrap">' + primary_html + '</div>'
                    '</div>'
    )

    if secondary_html:
        new_section += (
                    '<div class="keyword-group">'
                        '<h3 class="keyword-group-title">Secondary Targets</h3>'
                        '<ul class="quick-wins-list">' + secondary_html + '</ul>'
                    '</div>'
        )

    new_section += (
                '</div>'
            '</div>'
            '<div class="quickwins-panel">'
                '<h3 class="keyword-group-title">Quick Wins</h3>'
                '<div class="site-card clean-priority-card">'
                    '<ul class="quick-wins-list">' + quickwins_html + '</ul>'
                '</div>'
            '</div>'
        '</div>'
        '</div>'
    )

    # Replace the broken Priority Recommendations section through the start of Technical SEO Snapshot.
    html = re.sub(
        r'<div class="section target-quickwins-section">.*?(?=<div class="section">\s*<h2>Technical SEO Snapshot</h2>)',
        new_section,
        html,
        count=1,
        flags=re.DOTALL,
    )

    css = """
<style>
.target-quickwins-grid {
    display: grid !important;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) !important;
    gap: 22px !important;
    align-items: stretch !important;
}

.target-panel,
.quickwins-panel {
    background: #ffffff !important;
    border: 1px solid #dbe4f0 !important;
    border-radius: 14px !important;
    padding: 22px !important;
    min-width: 0 !important;
}

.clean-priority-box {
    border: 0 !important;
    padding: 0 !important;
    background: transparent !important;
}

.clean-priority-card {
    border: 0 !important;
    box-shadow: none !important;
    padding: 0 !important;
}

.clean-priority-card .site-url {
    font-size: 20px !important;
    line-height: 1.2 !important;
    word-break: break-word !important;
}

@media (max-width: 800px) {
    .target-quickwins-grid {
        grid-template-columns: 1fr !important;
    }
}


</style>
"""

    if "PRIORITY_RECOMMENDATIONS_V4_CSS" not in html:
        css = css.replace("<style>", "<style>\n/* PRIORITY_RECOMMENDATIONS_V4_CSS */")
        html = html.replace("</head>", css + "\n</head>", 1)

    # Remove broken extra closing tags left by previous wrapper.
    html = html.replace("</div></div></body></html>", "</body></html>")

    return html


# === INDUSTRY-WEIGHTED KEYWORD TERMS ===

def final_industry_weighted_keyword_terms_html(html):
    import re

    html = str(html or "")
    lower = html.lower()

    # Detect the main client industry from the report content.
    industry = None
    industry_label = None

    industry_rules = [
        ("painting", "Painting", ["painting", "painters", "painter", "paint contractor"]),
        ("roofing", "Roofing", ["roofing", "roofer", "roof repair", "roof replacement"]),
        ("plumbing", "Plumbing", ["plumbing", "plumber", "drain cleaning", "water heater"]),
        ("hvac", "HVAC", ["hvac", "air conditioning", "heating", "cooling"]),
        ("electrician", "Electrical", ["electrician", "electrical", "electrical contractor"]),
        ("dentist", "Dental", ["dentist", "dental", "orthodontic", "cosmetic dentistry"]),
        ("law", "Legal", ["law firm", "attorney", "lawyer", "legal services"]),
        ("landscaping", "Landscaping", ["landscaping", "lawn care", "landscape"]),
        ("contractor", "Contractor", ["contractor", "remodeling", "renovation", "construction"]),
    ]

    for key, label, signals in industry_rules:
        if any(signal in lower for signal in signals):
            industry = key
            industry_label = label
            break

    if not industry_label:
        return html

    # Industry-specific replacements for weak generic terms.
    if industry == "painting":
        replacements = {
            "Professional Services": "Professional Painting Services",
            "Residential Services": "Residential Painting Services",
            "Commercial Services": "Commercial Painting Services",
            "Emergency Services": "Emergency Painting Services",
            "Local Service Provider": "Local Painting Company",
            "Local Contractor": "Painting Contractor",
            "Free Estimate": "Free Painting Estimate",
            "Service Quote": "Painting Quote",
        }
    elif industry == "roofing":
        replacements = {
            "Professional Services": "Professional Roofing Services",
            "Residential Services": "Residential Roofing Services",
            "Commercial Services": "Commercial Roofing Services",
            "Emergency Services": "Emergency Roof Repair",
            "Local Service Provider": "Local Roofing Contractor",
            "Local Contractor": "Roofing Contractor",
            "Free Estimate": "Free Roofing Estimate",
            "Service Quote": "Roofing Quote",
        }
    elif industry == "plumbing":
        replacements = {
            "Professional Services": "Professional Plumbing Services",
            "Residential Services": "Residential Plumbing Services",
            "Commercial Services": "Commercial Plumbing Services",
            "Emergency Services": "Emergency Plumbing Services",
            "Local Service Provider": "Local Plumber",
            "Local Contractor": "Plumbing Contractor",
            "Free Estimate": "Free Plumbing Estimate",
            "Service Quote": "Plumbing Quote",
        }
    elif industry == "hvac":
        replacements = {
            "Professional Services": "Professional HVAC Services",
            "Residential Services": "Residential HVAC Services",
            "Commercial Services": "Commercial HVAC Services",
            "Emergency Services": "Emergency HVAC Service",
            "Local Service Provider": "Local HVAC Contractor",
            "Local Contractor": "HVAC Contractor",
            "Free Estimate": "Free HVAC Estimate",
            "Service Quote": "HVAC Quote",
        }
    elif industry == "electrical":
        replacements = {
            "Professional Services": "Professional Electrical Services",
            "Residential Services": "Residential Electrical Services",
            "Commercial Services": "Commercial Electrical Services",
            "Emergency Services": "Emergency Electrical Services",
            "Local Service Provider": "Local Electrician",
            "Local Contractor": "Electrical Contractor",
            "Free Estimate": "Free Electrical Estimate",
            "Service Quote": "Electrical Quote",
        }
    else:
        replacements = {
            "Professional Services": "Professional " + industry_label + " Services",
            "Residential Services": "Residential " + industry_label + " Services",
            "Commercial Services": "Commercial " + industry_label + " Services",
            "Emergency Services": "Emergency " + industry_label + " Services",
            "Local Service Provider": "Local " + industry_label + " Provider",
            "Free Estimate": "Free " + industry_label + " Estimate",
            "Service Quote": industry_label + " Quote",
        }

    for old, new in replacements.items():
        html = html.replace("⭐ " + old, "⭐ " + new)
        html = html.replace("🔥 " + old, "🔥 " + new)
        html = html.replace("<li>" + old + "</li>", "<li>" + new + "</li>")

    # Make sure Service Terms to Strengthen never contains bare generic chips.
    service_section = re.search(
        r'(<h3 class="keyword-group-title">Service Terms to Strengthen</h3>.*?<div class="chip-wrap">)(.*?)(</div>)',
        html,
        flags=re.DOTALL,
    )

    if service_section:
        prefix, chips_html, suffix = service_section.groups()

        terms = re.findall(
            r'<span class="chip[^"]*">\s*(?:🔥|⭐|🔵)?\s*([^<]+?)\s*</span>',
            chips_html,
            flags=re.DOTALL,
        )

        cleaned_terms = []
        seen = set()

        for term in terms:
            term = " ".join(term.split()).strip()

            if term in replacements:
                term = replacements[term]

            # If a remaining generic phrase slipped through, attach industry.
            if term in {"Professional Services", "Residential Services", "Commercial Services", "Emergency Services"}:
                term = term.replace("Services", industry_label + " Services")

            key = term.lower()
            if key not in seen:
                seen.add(key)
                cleaned_terms.append(term)

        # If the section is too thin, add money-word fallbacks.
        if industry == "painting":
            fallbacks = [
                "Professional Painting Services",
                "Residential Painting Services",
                "Commercial Painting Services",
                "Emergency Painting Services",
                "Interior Painting Services",
                "Exterior Painting Services",
            ]
        elif industry == "roofing":
            fallbacks = [
                "Professional Roofing Services",
                "Residential Roofing Services",
                "Commercial Roofing Services",
                "Emergency Roof Repair",
                "Roof Repair Services",
                "Roof Replacement Services",
            ]
        elif industry == "plumbing":
            fallbacks = [
                "Professional Plumbing Services",
                "Residential Plumbing Services",
                "Commercial Plumbing Services",
                "Emergency Plumbing Services",
                "Drain Cleaning Services",
                "Water Heater Repair",
            ]
        else:
            fallbacks = [
                "Professional " + industry_label + " Services",
                "Residential " + industry_label + " Services",
                "Commercial " + industry_label + " Services",
                "Emergency " + industry_label + " Services",
            ]

        for term in fallbacks:
            if len(cleaned_terms) >= 8:
                break
            key = term.lower()
            if key not in seen:
                seen.add(key)
                cleaned_terms.append(term)

        new_chips = "".join(
            '<span class="chip chip-medium">⭐ ' + term + '</span>'
            for term in cleaned_terms[:8]
        )

        html = (
            html[:service_section.start()]
            + prefix + new_chips + suffix
            + html[service_section.end():]
        )

    return html


# === CURRENT REPORT CLEANUP: EMPTY IDEAS, BUYER TERMS, WEAK COMPETITOR COPY ===

def final_current_report_cleanup_html(html):
    import re

    html = str(html or "")
    lower = html.lower()

    # 1) Remove empty Additional Keyword Ideas section completely.
    html = re.sub(
        r'<div class="section">\s*<h2>Additional Keyword Ideas</h2>\s*<div class="keyword-box">\s*</div>\s*</div>',
        '',
        html,
        flags=re.DOTALL,
    )

    # Also remove empty Additional Keyword Ideas keyword-group blocks if they slip in.
    html = re.sub(
        r'<div class="keyword-group">\s*<h3 class="keyword-group-title">Additional Keyword Ideas</h3>\s*<div class="chip-wrap">\s*</div>\s*</div>',
        '',
        html,
        flags=re.DOTALL,
    )

    # 2) Detect industry from report content.
    industry = None
    if any(x in lower for x in ["electrical", "electrician", "electric ", "electric services"]):
        industry = "electrical"
    elif any(x in lower for x in ["painting", "painter", "painters"]):
        industry = "painting"
    elif any(x in lower for x in ["roofing", "roofer", "roof repair", "roof replacement"]):
        industry = "roofing"
    elif any(x in lower for x in ["plumbing", "plumber", "drain cleaning", "water heater"]):
        industry = "plumbing"
    elif any(x in lower for x in ["hvac", "heating", "cooling", "air conditioning"]):
        industry = "hvac"

    if industry == "electrical":
        replacements = {
            "Local Contractor": "Local Electrical Contractor",
            "Emergency Service": "Emergency Electrical Service",
            "Free Estimate": "Free Electrical Estimate",
            "Service Quote": "Electrical Quote",
            "Professional Services": "Professional Electrical Services",
            "Residential Services": "Residential Electrical Services",
            "Commercial Services": "Commercial Electrical Services",
            "Emergency Services": "Emergency Electrical Services",
        }
    elif industry == "painting":
        replacements = {
            "Local Contractor": "Painting Contractor",
            "Emergency Service": "Emergency Painting Service",
            "Free Estimate": "Free Painting Estimate",
            "Service Quote": "Painting Quote",
            "Professional Services": "Professional Painting Services",
            "Residential Services": "Residential Painting Services",
            "Commercial Services": "Commercial Painting Services",
            "Emergency Services": "Emergency Painting Services",
        }
    elif industry == "roofing":
        replacements = {
            "Local Contractor": "Local Roofing Contractor",
            "Emergency Service": "Emergency Roof Repair",
            "Free Estimate": "Free Roofing Estimate",
            "Service Quote": "Roofing Quote",
            "Professional Services": "Professional Roofing Services",
            "Residential Services": "Residential Roofing Services",
            "Commercial Services": "Commercial Roofing Services",
            "Emergency Services": "Emergency Roofing Services",
        }
    elif industry == "plumbing":
        replacements = {
            "Local Contractor": "Plumbing Contractor",
            "Emergency Service": "Emergency Plumbing Service",
            "Free Estimate": "Free Plumbing Estimate",
            "Service Quote": "Plumbing Quote",
            "Professional Services": "Professional Plumbing Services",
            "Residential Services": "Residential Plumbing Services",
            "Commercial Services": "Commercial Plumbing Services",
            "Emergency Services": "Emergency Plumbing Services",
        }
    elif industry == "hvac":
        replacements = {
            "Local Contractor": "HVAC Contractor",
            "Emergency Service": "Emergency HVAC Service",
            "Free Estimate": "Free HVAC Estimate",
            "Service Quote": "HVAC Quote",
            "Professional Services": "Professional HVAC Services",
            "Residential Services": "Residential HVAC Services",
            "Commercial Services": "Commercial HVAC Services",
            "Emergency Services": "Emergency HVAC Services",
        }
    else:
        replacements = {}

    for old, new in replacements.items():
        html = html.replace("⭐ " + old, "⭐ " + new)
        html = html.replace("🔥 " + old, "🔥 " + new)
        html = html.replace("<li>" + old + "</li>", "<li>" + new + "</li>")
        html = html.replace(">" + old + "<", ">" + new + "<")

    # 3) Weak competitor copy cleanup.
    weak_markers = [
        "competitor crawl appears limited",
        "competitor-based recommendations are limited",
        "competitor data appears incomplete",
        "Use a stronger competitor page for accurate comparison",
    ]
    is_weak_competitor = any(marker.lower() in html.lower() for marker in weak_markers)

    if is_weak_competitor:
        # Add a small keyword strategy warning if missing.
        warning = (
            '<p class="empty-note" style="margin: 12px 0 16px 0;">'
            '<strong>Note:</strong> Competitor keyword guidance is limited because the competitor crawl is weak. '
            'Use these terms as client-page improvement ideas, then rerun with a stronger direct competitor before making final gap-based recommendations.'
            '</p>'
        )

        keyword_note_end = '</div>\n                \n                <div class="keyword-group"><h3 class="keyword-group-title">Service Terms to Strengthen</h3>'
        if "Competitor keyword guidance is limited" not in html and keyword_note_end in html:
            html = html.replace(
                keyword_note_end,
                '</div>\n' + warning + '\n                <div class="keyword-group"><h3 class="keyword-group-title">Service Terms to Strengthen</h3>',
                1
            )

        html = html.replace(
            "Use the competitor keyword gaps to improve page relevance and identify new sections, FAQs, or service/location content worth adding.",
            "Use the keyword strategy section to strengthen service, location, and buyer-intent language on the client page."
        )

        html = html.replace(
            "Review the Keyword Opportunities section above and prioritize only the terms that match the client’s actual services, locations, and search intent.",
            "Rerun the report with a stronger same-industry competitor before making final gap-based keyword recommendations."
        )

        html = html.replace(
            "Review the Keyword Strategy section above and prioritize only the terms that match the client’s actual services, locations, and search intent.",
            "Rerun the report with a stronger same-industry competitor before making final gap-based keyword recommendations."
        )

    # 4) Keep old section names from leaking into the analysis.
    html = html.replace("Keyword Opportunities section", "Keyword Strategy section")

    # 5) Better shared signal empty wording.
    html = html.replace("No shared keywords found.", "No shared topic signals found.")

    # 6) Final typo cleanup.
    html = html.replace("Commerical", "Commercial")

    return html


# === CUSTOM LOCATIONS FILE FALLBACK ===
# Prevents auto_save_geo_phrases from failing when the settings/location file path is missing.

try:
    CUSTOM_LOCATIONS_FILE
except NameError:
    from pathlib import Path as _VastPath

    _VAST_APP_ROOT = _VastPath(__file__).resolve().parents[1]
    _VAST_SETTINGS_DIR = _VAST_APP_ROOT / "settings_data"
    _VAST_SETTINGS_DIR.mkdir(parents=True, exist_ok=True)

    CUSTOM_LOCATIONS_FILE = _VAST_SETTINGS_DIR / "custom_locations.json"


# === ROOFING KEYWORD CONTAMINATION GUARD ===
# If a roofing report gets polluted by plumbing/chopped repair terms, rebuild the client-facing keyword strategy.

def final_roofing_keyword_contamination_guard_html(html):
    import re

    html = str(html or "")
    lower = html.lower()

    is_roofing_report = any(x in lower for x in [
        "liprecisionroofing.com",
        "precision roofing",
        "long island precision roofing",
        "trusted roofer",
        "commercial roofing",
        "residential roofing",
    ])

    contaminated = any(x in lower for x in [
        "plumbing repairs",
        "plumbing repair",
        "repair long",
        "repairs long",
        "plumbing replacement",
    ])

    if not is_roofing_report or not contaminated:
        return html

    service_terms = [
        "Roof Repair",
        "Roof Replacement",
        "Emergency Roof Repair",
        "Residential Roofing",
        "Commercial Roofing",
        "Roof Inspection",
        "Roof Maintenance",
        "Flat Roof Repair",
    ]

    location_terms = [
        "Roof Repair Long Island",
        "Roof Replacement Long Island",
        "Emergency Roof Repair Long Island",
        "Residential Roofing Long Island",
        "Commercial Roofing Long Island",
        "Roof Inspection Long Island",
    ]

    buyer_terms = [
        "Free Roofing Estimate",
        "Roofing Contractor",
        "Local Roofing Contractor",
        "Emergency Roofing Service",
        "Licensed Roofing Contractor",
    ]

    def chips(terms, high=False):
        cls = "chip chip-high top-chip" if high else "chip chip-medium"
        icon = "🔥" if high else "⭐"
        return "".join(f'<span class="{cls}">{icon} {term}</span>' for term in terms)

    service_block = (
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Service Terms to Strengthen</h3>'
        '<p class="signals-intro">Use these roofing-specific terms to clarify the page’s main services and support stronger topical relevance.</p>'
        '<div class="chip-wrap">' + chips(service_terms) + '</div>'
        '</div>'
    )

    location_block = (
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Location Terms to Strengthen</h3>'
        '<p class="signals-intro">Use these roofing-plus-location terms for service-area copy, internal links, FAQs, and dedicated location sections.</p>'
        '<div class="chip-wrap">' + chips(location_terms) + '</div>'
        '</div>'
    )

    buyer_block = (
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Buyer-Intent Terms to Strengthen</h3>'
        '<p class="signals-intro">Use these buyer-intent terms to improve calls to action, quote pages, and lead-focused copy.</p>'
        '<div class="chip-wrap">' + chips(buyer_terms) + '</div>'
        '</div>'
    )

    # Replace the first three keyword buckets, whether they are old gap buckets or newer strategy buckets.
    html = re.sub(
        r'<div class="keyword-group">\s*<h3 class="keyword-group-title">(?:Service Keywords|Service Terms to Strengthen)(?: \(\d+\))?</h3>.*?</div>\s*</div>',
        service_block,
        html,
        count=1,
        flags=re.DOTALL,
    )

    html = re.sub(
        r'<div class="keyword-group">\s*<h3 class="keyword-group-title">(?:Location Keywords|Location Terms to Strengthen)(?: \(\d+\))?</h3>.*?</div>\s*</div>',
        location_block,
        html,
        count=1,
        flags=re.DOTALL,
    )

    html = re.sub(
        r'<div class="keyword-group">\s*<h3 class="keyword-group-title">(?:Commercial Keywords|Buyer-Intent Terms to Strengthen)(?: \(\d+\))?</h3>.*?</div>\s*</div>',
        buyer_block,
        html,
        count=1,
        flags=re.DOTALL,
    )

    # Force primary target to a real roofing money term.
    html = re.sub(
        r'(<h3 class="keyword-group-title">Primary Target</h3>\s*<div class="chip-wrap">).*?(</div>)',
        r'\1<span class="chip chip-high top-chip">🔥 Roof Repair Long Island</span>\2',
        html,
        count=1,
        flags=re.DOTALL,
    )

    # Remove bad contaminated phrases everywhere client-facing.
    replacements = {
        "Plumbing Repairs": "Roof Repair",
        "Plumbing Repair": "Roof Repair",
        "Plumbing Replacement": "Roof Replacement",
        "Repair Long Island": "Roof Repair Long Island",
        "Repairs Long Island": "Roof Repair Long Island",
        "Repair Long": "Roof Repair Long Island",
        "Repairs Long": "Roof Repair Long Island",
        "roofing repair": "roof repair",
        "Roofing Repair": "Roof Repair",
        "Roofing Repairs": "Roof Repair",
    }

    for old, new in replacements.items():
        html = html.replace(old, new)

    # Rewrite Quick Wins/action-plan phrases that were built from contaminated terms.
    html = re.sub(
        r"Tighten the title tag to 30–65 characters while keeping '.*?' prominent\.",
        "Tighten the title tag to 30–65 characters while keeping a clear roofing service and Long Island location focus.",
        html,
    )

    html = re.sub(
        r"Trim meta description to ~120–155 characters while keeping '.*?'\.",
        "Trim the meta description to ~120–155 characters while keeping the main roofing service, Long Island location, and quote-focused CTA.",
        html,
    )

    html = re.sub(
        r"Add missing alt text and, where relevant, reinforce phrases like '.*?'\.",
        "Add missing alt text and, where relevant, reinforce roofing-specific phrases such as roof repair, roof replacement, and Long Island roofing.",
        html,
    )

    html = re.sub(
        r"Work missing phrases into copy, such as: .*?\.</li>",
        "Work roofing-specific service phrases into copy, such as roof repair Long Island, roof replacement Long Island, emergency roof repair, and commercial roofing.</li>",
        html,
    )

    html = re.sub(
        r'Work “.*?” into the page only if it accurately matches the client’s services and location targeting\.',
        'Strengthen roofing-specific service language such as “Roof Repair Long Island” only where it naturally fits the page.',
        html,
    )

    html = re.sub(
        r'Start by reviewing <strong>.*?</strong>\. Use it only where it fits naturally, then review the Keyword Strategy section above for other relevant service, location, and commercial terms\.',
        'Start by strengthening roofing-specific terms such as <strong>Roof Repair Long Island</strong>, <strong>Roof Replacement Long Island</strong>, and <strong>Emergency Roof Repair</strong> where they fit naturally.',
        html,
    )

    html = re.sub(
        r'Add a focused FAQ or short supporting section around “.*?” if it fits the business\.',
        'Add focused FAQ or supporting sections around roof repair, roof replacement, emergency roofing, and Long Island service areas.',
        html,
    )

    # Add a warning that explains why competitor keyword gaps were sanitized.
    warning = (
        '<p class="empty-note" style="margin:12px 0 16px 0;">'
        '<strong>Keyword safety note:</strong> The competitor page contains mixed industry signals, so the keyword strategy below has been cleaned to keep recommendations roofing-specific.'
        '</p>'
    )

    if "Keyword safety note:" not in html:
        html = html.replace(
            '<div class="keyword-note">',
            warning + '<div class="keyword-note">',
            1
        )

    return html


# === ROOFING DUPLICATE PHRASE CLEANUP ===

def final_roofing_duplicate_phrase_cleanup_html(html):
    html = str(html or "")

    fixes = {
        "Roof Roof Roof Repair Long Island Island": "Roof Repair Long Island",
        "Emergency Roof Roof Roof Repair Long Island Island": "Emergency Roof Repair Long Island",
        "Roof Roof Repair Long Island Island": "Roof Repair Long Island",
        "Emergency Roof Roof Repair Long Island Island": "Emergency Roof Repair Long Island",
        "Roof Repair Long Island Island": "Roof Repair Long Island",
        "Emergency Roof Repair Long Island Island": "Emergency Roof Repair Long Island",
        "Roof Roof Repair": "Roof Repair",
        "Emergency Roof Roof Repair": "Emergency Roof Repair",
        "Long Island Island": "Long Island",
    }

    for old, new in fixes.items():
        html = html.replace(old, new)

    # Repeat once because several replacements can create a second cleanup opportunity.
    for old, new in fixes.items():
        html = html.replace(old, new)

    return html


# === PLUMBING KEYWORD QUALITY GUARD ===
# Removes awkward plumbing fragments, competitor brand terms, and empty target keyword blocks.

def final_plumbing_keyword_quality_guard_html(html):
    import re

    html = str(html or "")
    lower = html.lower()

    is_plumbing_report = any(x in lower for x in [
        "westernplumbing.net",
        "western plumbing",
        "plumbing",
        "plumber",
        "drain cleaning",
        "water heater",
        "sewer drain",
    ])

    if not is_plumbing_report:
        return html

    bad_terms = [
        "Plumbing Peace",
        "Matters Plumbing",
        "Plumbing Drilling",
        "Plumbing Modesto",
        "Teeples Plumbing",
        "Plumbing Count",
        "Plumbing Offers",
    ]

    # Remove bad keyword chips.
    for term in bad_terms:
        html = re.sub(
            r'<span class="chip[^"]*">\s*(?:🔥|⭐|🔵)?\s*' + re.escape(term) + r'\s*</span>',
            '',
            html,
            flags=re.IGNORECASE,
        )

    # Replace weak text mentions if they appear in lists or action plan.
    replacements = {
        "Plumbing Peace": "Professional Plumbing Services",
        "Matters Plumbing": "Emergency Plumbing Service",
        "Plumbing Drilling": "Plumbing Services",
        "Plumbing Modesto": "Local Plumbing Services",
        "Teeples Plumbing": "Plumbing Services",
        "Plumbing Count": "Reliable Plumbing Services",
        "Plumbing Offers": "Plumbing Services",
    }

    for old, new in replacements.items():
        html = html.replace(old, new)

    service_terms = [
        "Drain Cleaning",
        "Leak Repair",
        "Water Heater Repair",
        "Emergency Plumbing",
        "Residential Plumbing",
        "Commercial Plumbing",
        "Sewer Drain Cleaning",
        "Pipe Repair",
    ]

    location_terms = [
        "Plumbing Services",
        "Emergency Plumber",
        "Drain Cleaning",
        "Leak Repair",
        "Water Heater Repair",
    ]

    buyer_terms = [
        "Free Plumbing Estimate",
        "Emergency Plumbing Service",
        "Same-Day Plumber",
        "Licensed Plumbing Contractor",
        "Trusted Emergency Plumber",
    ]

    def chips(terms, high=False):
        cls = "chip chip-high top-chip" if high else "chip chip-medium"
        icon = "🔥" if high else "⭐"
        return "".join(f'<span class="{cls}">{icon} {term}</span>' for term in terms)

    def replace_bucket(title_patterns, new_title, description, terms):
        nonlocal html
        title_regex = "|".join(re.escape(t) for t in title_patterns)
        block = (
            '<div class="keyword-group">'
            f'<h3 class="keyword-group-title">{new_title}</h3>'
            f'<p class="signals-intro">{description}</p>'
            '<div class="chip-wrap">' + chips(terms) + '</div>'
            '</div>'
        )

        html = re.sub(
            r'<div class="keyword-group">\s*<h3 class="keyword-group-title">(?:' + title_regex + r')(?: \(\d+\))?</h3>.*?</div>\s*</div>',
            block,
            html,
            count=1,
            flags=re.DOTALL,
        )

    replace_bucket(
        ["Service Keywords", "Service Terms to Strengthen"],
        "Service Terms to Strengthen",
        "Use these plumbing-specific terms to clarify the page’s main services and support stronger topical relevance.",
        service_terms,
    )

    replace_bucket(
        ["Location Keywords", "Location Terms to Strengthen"],
        "Location Terms to Strengthen",
        "Use these service-plus-location terms for service-area copy, internal links, FAQs, and dedicated location sections.",
        location_terms,
    )

    replace_bucket(
        ["Commercial Keywords", "Buyer-Intent Terms to Strengthen"],
        "Buyer-Intent Terms to Strengthen",
        "Use these buyer-intent terms to improve calls to action, quote pages, and lead-focused copy.",
        buyer_terms,
    )

    # Fix empty Provisional/Recommended Target Keywords primary target.
    primary = "Emergency Plumbing Service"

    html = re.sub(
        r'(<h3 class="keyword-group-title">Primary Target</h3>\s*<div class="chip-wrap">)\s*(</div>)',
        r'\1<span class="chip chip-high top-chip">🔥 ' + primary + r'</span>\2',
        html,
        count=1,
        flags=re.DOTALL,
    )

    # Replace weak secondary targets inside Provisional Keyword Ideas.
    secondary_html = (
        "<li>Drain Cleaning</li>"
        "<li>Leak Repair</li>"
        "<li>Water Heater Repair</li>"
        "<li>Commercial Plumbing</li>"
        "<li>Trusted Emergency Plumber</li>"
    )

    html = re.sub(
        r'(<h3 class="keyword-group-title">Secondary Targets</h3>\s*<ul class="quick-wins-list">).*?(</ul>)',
        r'\1' + secondary_html + r'\2',
        html,
        count=1,
        flags=re.DOTALL,
    )

    # Clean action-plan mention if it picked an awkward term.
    html = html.replace(
        'Add a focused FAQ or short supporting section around “Professional Plumbing Services” if it fits the business.',
        'Add focused FAQ or supporting sections around drain cleaning, emergency plumbing, leak repair, and water heater repair.'
    )

    html = html.replace(
        'Add a focused FAQ or short supporting section around “Drain Cleaning” if it fits the business.',
        'Add focused FAQ or supporting sections around drain cleaning, emergency plumbing, leak repair, and water heater repair.'
    )

    # Remove empty chip wraps caused by cleanup.
    html = re.sub(
        r'<div class="chip-wrap">\s*</div>',
        '<p class="empty-note">No clean terms found for this bucket.</p>',
        html,
        flags=re.DOTALL,
    )

    return html


# === PLUMBING REPORT LAYOUT + ACTION COPY CLEANUP ===

def final_plumbing_layout_action_cleanup_html(html):
    import re

    html = str(html or "")
    lower = html.lower()

    is_plumbing_report = any(x in lower for x in [
        "plumbing",
        "plumber",
        "drain cleaning",
        "water heater",
        "sewer drain",
    ])

    if not is_plumbing_report:
        return html

    # Fix bad leftover action-plan phrase.
    html = html.replace(
        'Add a focused FAQ or short supporting section around “Repair Major” if it fits the business.',
        'Add focused FAQ or supporting sections around drain cleaning, leak repair, water heater repair, and emergency plumbing.'
    )

    html = html.replace(
        'Add a focused FAQ or short supporting section around “Repair Major”.',
        'Add focused FAQ or supporting sections around drain cleaning, leak repair, water heater repair, and emergency plumbing.'
    )

    # If Buyer-Intent bucket is missing from Keyword Strategy, add it before Shared Topic Signals.
    if "Buyer-Intent Terms to Strengthen" not in html:
        buyer_terms = [
            "Free Plumbing Estimate",
            "Emergency Plumbing Service",
            "Same-Day Plumber",
            "Licensed Plumbing Contractor",
            "Trusted Emergency Plumber",
        ]

        buyer_chips = "".join(
            '<span class="chip chip-medium">⭐ ' + term + '</span>'
            for term in buyer_terms
        )

        buyer_block = (
            '<div class="keyword-group">'
            '<h3 class="keyword-group-title">Buyer-Intent Terms to Strengthen</h3>'
            '<p class="signals-intro">Use these buyer-intent terms to improve calls to action, quote pages, and lead-focused copy.</p>'
            '<div class="chip-wrap">' + buyer_chips + '</div>'
            '</div>'
        )

        match = re.search(
            r'(?=<div class="keyword-group">\s*<h3 class="keyword-group-title">Shared Topic Signals)',
            html,
            flags=re.DOTALL,
        )

        if match:
            html = html[:match.start()] + buyer_block + "\n" + html[match.start():]

    # Convert Provisional Keyword Ideas + Quick Wins into the cleaner Priority Recommendations layout.
    if "Provisional Keyword Ideas" in html and "Quick Wins" in html:
        primary_match = re.search(
            r'<h3 class="keyword-group-title">Primary Target</h3>\s*<div class="chip-wrap">(.*?)</div>',
            html,
            flags=re.DOTALL,
        )

        secondary_match = re.search(
            r'<h3 class="keyword-group-title">Secondary Targets</h3>\s*<ul class="quick-wins-list">(.*?)</ul>',
            html,
            flags=re.DOTALL,
        )

        quickwins_match = re.search(
            r'<h2>Quick Wins</h2>.*?<ul class="quick-wins-list">(.*?)</ul>',
            html,
            flags=re.DOTALL,
        )

        if primary_match and quickwins_match:
            primary_html = primary_match.group(1).strip()
            secondary_html = secondary_match.group(1).strip() if secondary_match else ""
            quickwins_html = quickwins_match.group(1).strip()

            if not primary_html:
                primary_html = '<span class="chip chip-high top-chip">🔥 Emergency Plumbing Service</span>'

            combined = (
                '<div class="section target-quickwins-section">'
                '<h2>Priority Recommendations</h2>'
                '<div class="target-quickwins-grid">'
                '<div class="target-panel">'
                '<h3 class="keyword-group-title">Recommended Target Keywords</h3>'
                '<div class="keyword-box clean-priority-box">'
                '<p class="empty-note" style="margin-bottom:14px;">'
                '<strong>Note:</strong> Competitor keyword guidance is limited because the competitor crawl appears weak. '
                'Use these as client-page improvement targets and rerun with a stronger competitor before finalizing gap-based strategy.'
                '</p>'
                '<div class="keyword-group">'
                '<h3 class="keyword-group-title">Primary Target</h3>'
                '<div class="chip-wrap">' + primary_html + '</div>'
                '</div>'
            )

            if secondary_html:
                combined += (
                    '<div class="keyword-group">'
                    '<h3 class="keyword-group-title">Secondary Targets</h3>'
                    '<ul class="quick-wins-list">' + secondary_html + '</ul>'
                    '</div>'
                )

            combined += (
                '</div>'
                '</div>'
                '<div class="quickwins-panel">'
                '<h3 class="keyword-group-title">Quick Wins</h3>'
                '<div class="site-card clean-priority-card">'
                '<ul class="quick-wins-list">' + quickwins_html + '</ul>'
                '</div>'
                '</div>'
                '</div>'
                '</div>'
            )

            html = re.sub(
                r'<div class="section"><h2>Provisional Keyword Ideas</h2>.*?(?=<div class="section">\s*<h2>Technical SEO Snapshot</h2>)',
                combined,
                html,
                count=1,
                flags=re.DOTALL,
            )

    # Clean a few remaining weak plumbing phrases if they appear.
    replacements = {
        "Plumbing Peace": "Professional Plumbing Services",
        "Matters Plumbing": "Emergency Plumbing Service",
        "Plumbing Count": "Reliable Plumbing Services",
        "Plumbing Offers": "Plumbing Services",
        "Repair Major": "Plumbing Repair",
    }

    for old, new in replacements.items():
        html = html.replace(old, new)

    return html


# === PLUMBING GEO + ACTION PLAN POLISH ===

def final_plumbing_geo_action_polish_html(html):
    import re

    html = str(html or "")
    lower = html.lower()

    is_plumbing_report = any(x in lower for x in [
        "plumbing",
        "plumber",
        "drain cleaning",
        "water heater",
        "teeplesplumbing.com",
        "tonysplumbingandheating.com",
    ])

    if not is_plumbing_report:
        return html

    detected_location = ""

    if "modesto" in lower:
        detected_location = "Modesto"
    elif "long island" in lower:
        detected_location = "Long Island"
    elif "suffolk" in lower:
        detected_location = "Suffolk County"
    elif "nassau" in lower:
        detected_location = "Nassau County"

    if detected_location:
        location_terms = [
            f"Plumbing Services {detected_location}",
            f"Emergency Plumber {detected_location}",
            f"Drain Cleaning {detected_location}",
            f"Leak Repair {detected_location}",
            f"Water Heater Repair {detected_location}",
        ]

        chips = "".join(
            '<span class="chip chip-medium">⭐ ' + term + '</span>'
            for term in location_terms
        )

        location_block = (
            '<div class="keyword-group">'
            '<h3 class="keyword-group-title">Location Terms to Strengthen</h3>'
            '<p class="signals-intro">Use these service-plus-location terms for service-area copy, internal links, FAQs, and dedicated location sections.</p>'
            '<div class="chip-wrap">' + chips + '</div>'
            '</div>'
        )

        html = re.sub(
            r'<div class="keyword-group"><h3 class="keyword-group-title">Location Terms to Strengthen</h3>.*?</div></div>',
            location_block,
            html,
            count=1,
            flags=re.DOTALL,
        )

    weak_action_phrases = [
        'Add a focused FAQ or short supporting section around “Plumbing Fixtures” if it fits the business.',
        'Add a focused FAQ or short supporting section around “Repair Major” if it fits the business.',
        'Add a focused FAQ or short supporting section around “Plumbing Services” if it fits the business.',
    ]

    better_action = (
        'Add focused FAQ or supporting sections around drain cleaning, leak repair, '
        'water heater repair, emergency plumbing, and service areas.'
    )

    for phrase in weak_action_phrases:
        html = html.replace(phrase, better_action)

    return html


# === FORCE PLUMBING GEO + ACTION POLISH ===

def final_force_plumbing_geo_polish_html(html):
    import re

    html = str(html or "")
    lower = html.lower()

    is_plumbing = (not vast_report_is_roofing_html(html)) and any(x in lower for x in [
        "plumbing",
        "plumber",
        "drain cleaning",
        "water heater",
        "teeplesplumbing.com",
        "tonysplumbingandheating.com",
    ])

    if not is_plumbing:
        return html

    location = ""
    if "modesto" in lower:
        location = "Modesto"
    elif "long island" in lower:
        location = "Long Island"
    elif "suffolk" in lower:
        location = "Suffolk County"
    elif "nassau" in lower:
        location = "Nassau County"

    if location:
        location_terms = [
            f"Plumbing Services {location}",
            f"Emergency Plumber {location}",
            f"Drain Cleaning {location}",
            f"Leak Repair {location}",
            f"Water Heater Repair {location}",
        ]

        chips = "".join(
            '<span class="chip chip-medium">⭐ ' + term + '</span>'
            for term in location_terms
        )

        new_location_block = (
            '<div class="keyword-group">'
            '<h3 class="keyword-group-title">Location Terms to Strengthen</h3>'
            '<p class="signals-intro">Use these service-plus-location terms for service-area copy, internal links, FAQs, and dedicated location sections.</p>'
            '<div class="chip-wrap">' + chips + '</div>'
            '</div>'
        )

        html = re.sub(
            r'<div class="keyword-group"><h3 class="keyword-group-title">Location Terms to Strengthen</h3>.*?</div></div>',
            new_location_block,
            html,
            count=1,
            flags=re.DOTALL,
        )

        html = re.sub(
            r'<div class="keyword-group">\s*<h3 class="keyword-group-title">Location Terms to Strengthen</h3>.*?</div>\s*</div>',
            new_location_block,
            html,
            count=1,
            flags=re.DOTALL,
        )

    better_action = (
        'Add focused FAQ or supporting sections around drain cleaning, leak repair, '
        'water heater repair, emergency plumbing, and service areas.'
    )

    html = re.sub(
        r'Add a focused FAQ or short supporting section around “[^”]+” if it fits the business\.',
        better_action,
        html,
    )

    html = html.replace(
        "Treat the Keyword Strategy section as provisional. Use a stronger competitor page before turning these terms into final recommendations.",
        "Use the Keyword Strategy section as page-improvement guidance, and rerun with a stronger competitor before making final gap-based recommendations."
    )

    return html


# === CLIENT LOCATION GEO GUARD ===
# Uses client-side location terms first and warns when competitor market appears different.

def final_client_location_geo_guard_html(html):
    import re

    html = str(html or "")
    lower = html.lower()

    is_plumbing = (not vast_report_is_roofing_html(html)) and any(x in lower for x in [
        "plumbing",
        "plumber",
        "drain cleaning",
        "water heater",
        "mlpandservices.com",
        "teeplesplumbing.com",
        "tonysplumbingandheating.com",
    ])

    if not is_plumbing:
        return html

    client_location = ""
    competitor_location = ""

    # Client-side location detection from visible report text.
    if "north port, fl" in lower or "north port" in lower:
        client_location = "North Port"
    elif "modesto" in lower:
        client_location = "Modesto"
    elif "long island" in lower:
        client_location = "Long Island"
    elif "suffolk" in lower:
        client_location = "Suffolk County"
    elif "nassau" in lower:
        client_location = "Nassau County"

    # Competitor-side mismatch detection.
    if "northport, ny" in lower or "northport, new york" in lower:
        competitor_location = "Northport, NY"
    elif "modesto" in lower and client_location != "Modesto":
        competitor_location = "Modesto"
    elif "long island" in lower and client_location != "Long Island":
        competitor_location = "Long Island"

    if client_location:
        location_terms = [
            f"Plumbing Services {client_location}",
            f"Emergency Plumber {client_location}",
            f"Drain Cleaning {client_location}",
            f"Leak Repair {client_location}",
            f"Water Heater Repair {client_location}",
        ]

        chips = "".join(
            '<span class="chip chip-medium">⭐ ' + term + '</span>'
            for term in location_terms
        )

        location_block = (
            '<div class="keyword-group">'
            '<h3 class="keyword-group-title">Location Terms to Strengthen</h3>'
            '<p class="signals-intro">Use these client-market location terms for service-area copy, internal links, FAQs, and dedicated location sections.</p>'
            '<div class="chip-wrap">' + chips + '</div>'
            '</div>'
        )

        html = re.sub(
            r'<div class="keyword-group"><h3 class="keyword-group-title">Location Terms to Strengthen</h3>.*?</div></div>',
            location_block,
            html,
            count=1,
            flags=re.DOTALL,
        )

        html = re.sub(
            r'<div class="keyword-group">\s*<h3 class="keyword-group-title">Location Terms to Strengthen</h3>.*?</div>\s*</div>',
            location_block,
            html,
            count=1,
            flags=re.DOTALL,
        )

    # Add a location mismatch warning when client and competitor markets conflict.
    if client_location and competitor_location and "Competitor Location Warning" not in html:
        warning = (
            '<div class="report-box" style="border-left:6px solid #f59e0b;background:#fffbeb;margin-bottom:18px;">'
            '<h3>Competitor Location Warning</h3>'
            '<p><strong>Important:</strong> The client appears to target ' + client_location + ', while the competitor appears to target ' + competitor_location + '.</p>'
            '<p>Use this report for service-topic and technical comparison, but do not rely on competitor location signals for local SEO recommendations.</p>'
            '</div>'
        )

        html = html.replace(
            '<div class="keyword-box">',
            warning + '<div class="keyword-box">',
            1
        )

    # Clean old generic location chips if any survived.
    if client_location:
        generic_to_geo = {
            "⭐ Plumbing Services</span>": f"⭐ Plumbing Services {client_location}</span>",
            "⭐ Emergency Plumber</span>": f"⭐ Emergency Plumber {client_location}</span>",
            "⭐ Drain Cleaning</span>": f"⭐ Drain Cleaning {client_location}</span>",
            "⭐ Leak Repair</span>": f"⭐ Leak Repair {client_location}</span>",
            "⭐ Water Heater Repair</span>": f"⭐ Water Heater Repair {client_location}</span>",
        }

        # Only apply inside location-ish contexts by relying on the replacement above first.
        # These are final fallbacks if a previous regex missed.
        for old, new in generic_to_geo.items():
            if "Location Terms to Strengthen" in html:
                html = html.replace(old, new, 1)

    # Improve action-plan wording when location mismatch is detected.
    if client_location and competitor_location:
        html = html.replace(
            "Use the Keyword Strategy section as page-improvement guidance, and rerun with a stronger competitor before making final gap-based recommendations.",
            "Use the Keyword Strategy section as page-improvement guidance, and rerun with a same-market competitor before making final local keyword recommendations."
        )

        html = html.replace(
            "Rerun the report with a stronger direct competitor before choosing final target keywords.",
            "Rerun the report with a stronger same-market competitor before choosing final target keywords."
        )

    return html


# === SERVICE / GEO KEYWORD SPLIT CLEANUP ===
# Keeps pure service terms in Service Terms and geo-modified terms in Location Terms.

def final_service_geo_split_cleanup_html(html):
    import re

    html = str(html or "")

    service_terms = [
        "Drain Cleaning",
        "Leak Repair",
        "Water Heater Repair",
        "Emergency Plumbing",
        "Residential Plumbing",
        "Commercial Plumbing",
        "Sewer Drain Cleaning",
        "Pipe Repair",
    ]

    service_chips = "".join(
        '<span class="chip chip-medium">⭐ ' + term + '</span>'
        for term in service_terms
    )

    service_block = (
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Service Terms to Strengthen</h3>'
        '<p class="signals-intro">Use these plumbing-specific terms to clarify the page’s main services and support stronger topical relevance.</p>'
        '<div class="chip-wrap">' + service_chips + '</div>'
        '</div>'
    )

    if "Service Terms to Strengthen" in html and "plumbing-specific terms" in html.lower():
        html = re.sub(
            r'<div class="keyword-group"><h3 class="keyword-group-title">Service Terms to Strengthen</h3>.*?</div></div>',
            service_block,
            html,
            count=1,
            flags=re.DOTALL,
        )

    # Fix Shared Topic Signals count if blank chips were stripped.
    shared_match = re.search(
        r'(<h3 class="keyword-group-title">Shared Topic Signals \()(\d+)(\)</h3>\s*<div class="chip-wrap">(.*?)</div>)',
        html,
        flags=re.DOTALL,
    )

    if shared_match:
        chips = re.findall(r'<span class="chip[^"]*">.*?</span>', shared_match.group(4), flags=re.DOTALL)
        real_count = len(chips)
        old_count = shared_match.group(2)

        if str(real_count) != old_count:
            html = html[:shared_match.start()] + shared_match.group(1) + str(real_count) + shared_match.group(3) + html[shared_match.end():]

    return html


# === PRIORITY PLAN NUMBERING FIX ===
# Makes Priority 2 and Priority 3 use numbered action lists like Priority 1.

def final_priority_plan_numbering_fix_html(html):
    import re

    html = str(html or "")

    if "priority-plan-card priority-two" not in html or "priority-plan-card priority-three" not in html:
        return html

    # Priority 2: keep the intro paragraph, convert the second recommendation paragraph into an ordered list item.
    def fix_priority_two(match):
        block = match.group(0)

        if "<ol>" in block:
            return block

        paragraphs = re.findall(r'<p>(.*?)</p>', block, flags=re.DOTALL)

        if len(paragraphs) < 2:
            return block

        intro = paragraphs[0].strip()
        action = paragraphs[1].strip()

        new_block = re.sub(
            r'<p>.*?</p>\s*<p>.*?</p>',
            '<p>' + intro + '</p><ol><li>' + action + '</li></ol>',
            block,
            count=1,
            flags=re.DOTALL,
        )

        return new_block

    html = re.sub(
        r'<section class="priority-plan-card priority-two">.*?</section>',
        fix_priority_two,
        html,
        count=1,
        flags=re.DOTALL,
    )

    # Priority 3: convert the content improvement bullet list into a numbered list.
    def fix_priority_three(match):
        block = match.group(0)

        block = block.replace('<h4>Content Improvements</h4><ul>', '<h4>Content Improvements</h4><ol>')
        block = block.replace('</ul>', '</ol>')

        # If no list exists, turn the second paragraph into a numbered action.
        if "<ol>" not in block:
            paragraphs = re.findall(r'<p>(.*?)</p>', block, flags=re.DOTALL)
            if len(paragraphs) >= 2:
                intro = paragraphs[0].strip()
                action = paragraphs[1].strip()
                block = re.sub(
                    r'<p>.*?</p>\s*<p>.*?</p>',
                    '<p>' + intro + '</p><ol><li>' + action + '</li></ol>',
                    block,
                    count=1,
                    flags=re.DOTALL,
                )

        return block

    html = re.sub(
        r'<section class="priority-plan-card priority-three">.*?</section>',
        fix_priority_three,
        html,
        count=1,
        flags=re.DOTALL,
    )

    return html


# === LONG ISLAND MARKET CONFIG ===
# Central market grouping for launch market.
# Nassau, Suffolk, Northport, Hicksville, Montauk, etc. should roll up into Long Island.

LONG_ISLAND_MARKET_TERMS = {
    "long island",
    "nassau",
    "nassau county",
    "suffolk",
    "suffolk county",
    "northport",
    "hicksville",
    "montauk",
    "huntington",
    "smithtown",
    "mineola",
    "merrick",
    "bellmore",
    "east meadow",
    "levittown",
    "westbury",
    "garden city",
    "commack",
    "babylon",
    "islandia",
    "long beach",
    "ronkonkoma",
    "patchogue",
    "islip",
    "bay shore",
    "riverhead",
    "southampton",
    "hampton bays",
    "east hampton",
}

LONG_ISLAND_AREA_CODES = {
    "516",
    "631",
    "934",
}

def vast_is_long_island_market_text(value):
    import re

    blob = str(value or "").lower()

    if any(term in blob for term in LONG_ISLAND_MARKET_TERMS):
        return True

    area_codes = set(re.findall(r'(?:\(|\b)(516|631|934)(?:\)|[\s\-.])', blob))
    return bool(area_codes & LONG_ISLAND_AREA_CODES)


# === SAVE GOOD LOCAL KEYWORD PHRASES ===
# Protects useful phrases that sound natural and match local service intent.

GOOD_LOCAL_KEYWORD_PHRASES = {
    "expert plumbing patchogue",
    "expert plumber patchogue",
    "plumbing services patchogue",
    "emergency plumber patchogue",
    "drain cleaning patchogue",
}

def final_save_good_local_keywords_html(html):
    import re

    html = str(html or "")
    lower = html.lower()

    if "expert plumbing patchogue" not in lower:
        return html

    # Make sure the phrase appears as a secondary target if the card exists.
    if "Secondary Targets" in html and "Expert Plumbing Patchogue" not in html:
        html = html.replace(
            '<h3 class="keyword-group-title">Secondary Targets</h3><ul class="quick-wins-list">',
            '<h3 class="keyword-group-title">Secondary Targets</h3><ul class="quick-wins-list"><li>Expert Plumbing Patchogue</li>',
            1
        )

    # Make the action wording more professional while keeping the phrase.
    html = html.replace(
        "Strengthen H1 alignment by working 'expert plumbing patchogue' into the heading or opening section.",
        "Strengthen H1 alignment by naturally working “Expert Plumbing Patchogue” into the heading, opening section, or nearby service copy."
    )

    html = html.replace(
        "Add missing alt text and, where relevant, reinforce phrases like 'expert plumbing patchogue'.",
        "Add missing alt text and, where relevant, reinforce service/location phrases like “Expert Plumbing Patchogue.”"
    )

    html = html.replace(
        "expert plumbing patchogue, plumbing installation, installation repairs",
        "Expert Plumbing Patchogue, plumbing installation, and plumbing repairs"
    )

    html = html.replace(
        "Work missing phrases into copy, such as: Expert Plumbing Patchogue, plumbing installation, plumbing installation and repairs.",
        "Work useful service/location phrases into copy, such as Expert Plumbing Patchogue, plumbing installation, and plumbing repairs."
    )

    html = html.replace(
        "installation repairs",
        "plumbing repairs"
    )

    return html


# === FORCE PRIORITY RECOMMENDATIONS FILLOUT ===
# Rebuilds the Priority Recommendations section when the target card only has one chip.

def final_force_priority_recommendations_fillout_html(html):
    import re

    html = str(html or "")
    lower = html.lower()

    if "Priority Recommendations" not in html:
        return html

    is_plumbing = (not vast_report_is_roofing_html(html)) and any(x in lower for x in [
        "plumbing",
        "plumber",
        "drain cleaning",
        "water heater",
        "sewer drain",
    ])

    if not is_plumbing:
        return html

    if "patchogue" in lower:
        market = "Patchogue"
    elif "long island" in lower:
        market = "Long Island"
    elif "northport" in lower:
        market = "Northport"
    elif "hicksville" in lower:
        market = "Hicksville"
    elif "nassau" in lower:
        market = "Nassau County"
    elif "suffolk" in lower:
        market = "Suffolk County"
    else:
        market = "Long Island"

    primary = f"Expert Plumbing {market}" if market == "Patchogue" else f"Plumbing Services {market}"

    secondary_terms = [
        f"Expert Plumbing {market}",
        f"Emergency Plumber {market}",
        f"Drain Cleaning {market}",
        f"Water Heater Repair {market}",
        "Plumbing Installation",
        "Leak Repair",
        "Residential Plumbing",
        "Commercial Plumbing",
    ]

    # Deduplicate while preserving order.
    seen = set()
    secondary_terms = [
        term for term in secondary_terms
        if not (term.lower() in seen or seen.add(term.lower()))
    ]

    secondary_html = "".join(f"<li>{term}</li>" for term in secondary_terms[:6])

    buyer_html = (
        "<li>Free Plumbing Estimate</li>"
        "<li>Same-Day Plumbing Service</li>"
        "<li>Licensed Plumbing Contractor</li>"
    )

    quickwins_html = (
        f"<li>Strengthen H1 alignment by naturally working “{primary}” into the heading, opening section, or nearby service copy.</li>"
        "<li>Add supporting copy for high-intent services such as drain cleaning, plumbing installation, water heater repair, and leak repair.</li>"
        f"<li>Add image alt text that describes real services, locations, and trust signals, including service/location phrases like “{primary}.”</li>"
        "<li>Build a short FAQ section around emergency plumbing, service availability, estimates, and common repair needs.</li>"
    )

    new_section = (
        '<div class="section target-quickwins-section">'
        '<h2>Priority Recommendations</h2>'
        '<div class="target-quickwins-grid">'
        '<div class="target-panel">'
        '<h3 class="keyword-group-title">Recommended Target Keywords</h3>'
        '<div class="keyword-box clean-priority-box">'
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Primary Target</h3>'
        '<div class="chip-wrap"><span class="chip chip-high top-chip">🔥 ' + primary + '</span></div>'
        '</div>'
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Secondary Targets</h3>'
        '<ul class="quick-wins-list">' + secondary_html + '</ul>'
        '</div>'
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Buyer-Intent Targets</h3>'
        '<ul class="quick-wins-list">' + buyer_html + '</ul>'
        '</div>'
        '</div>'
        '</div>'
        '<div class="quickwins-panel">'
        '<h3 class="keyword-group-title">Quick Wins</h3>'
        '<div class="site-card clean-priority-card">'
        '<ul class="quick-wins-list">' + quickwins_html + '</ul>'
        '</div>'
        '</div>'
        '</div>'
        '</div>'
    )

    html = re.sub(
        r'<div class="section target-quickwins-section">.*?(?=<div class="section">\s*<h2>Technical SEO Snapshot</h2>)',
        new_section,
        html,
        count=1,
        flags=re.DOTALL,
    )

    # Clean awkward leftovers elsewhere.
    html = html.replace("installation repairs", "plumbing repairs")
    html = html.replace("Installation Repairs", "Plumbing Repairs")

    html = html.replace(
        "Use the competitor keyword gaps to improve page relevance and identify new sections, FAQs, or service/location content worth adding.",
        "Use the keyword strategy section to strengthen service, location, and buyer-intent language on the client page."
    )

    return html


# === BLOCKED CLIENT CRAWL MODE ===
# Makes reports safer and more professional when the client page could not be crawled.

def final_blocked_client_crawl_mode_html(html):
    import re

    html = str(html or "")
    lower = html.lower()

    blocked_client = any(x in lower for x in [
        "<strong>title:</strong> blocked by site",
        "<strong>meta:</strong> blocked by site",
        "<strong>word count:</strong> 0",
    ])

    # Require client-side blocked signal, not just any blocked mention.
    if "your site" not in lower or not blocked_client:
        return html

    warning = (
        '<div class="section blocked-client-warning-section">'
        '<div class="report-box" style="border-left:6px solid #dc2626;background:#fef2f2;">'
        '<h2 style="margin-top:0;">Client Crawl Warning</h2>'
        '<p><strong>The client page could not be fully crawled.</strong> Title, meta, H1, content, links, images, and keyword checks may be incomplete or unreliable.</p>'
        '<p>Use this report as a technical access check first. Fix crawl access or rerun with a readable page before using the competitor comparison for final SEO recommendations.</p>'
        '</div>'
        '</div>'
    )

    if "Client Crawl Warning" not in html:
        html = html.replace(
            '<div class="section">\n\n            <h2>Comparison Overview</h2>',
            warning + '\n<div class="section">\n\n            <h2>Comparison Overview</h2>',
            1
        )

    # Cap visible client score when blocked.
    html = re.sub(
        r'(<div class="site-label">Your Site</div>\s*<div class="score">)\d+(</div>)',
        r'\130\2',
        html,
        count=1,
        flags=re.DOTALL,
    )

    # Cap total score table value for Your Site.
    html = re.sub(
        r'(<tr>\s*<td><strong>Total</strong></td>\s*<td class="[^"]*">\s*<strong>)\d+(</strong>)',
        r'\130\2',
        html,
        count=1,
        flags=re.DOTALL,
    )

    # Add score disclaimer near score breakdown if missing.
    score_note = (
        '<p class="empty-note" style="margin-bottom:14px;">'
        '<strong>Score note:</strong> The client score is capped because the page could not be crawled. '
        'Resolve crawl access before treating the score as a normal SEO grade.'
        '</p>'
    )

    if "The client score is capped because the page could not be crawled" not in html:
        html = html.replace(
            '<div class="section">\n            <h2>Score Breakdown</h2>',
            '<div class="section">\n            <h2>Score Breakdown</h2>\n' + score_note,
            1
        )

    # Downgrade Keyword Strategy wording.
    html = html.replace(
        '<h2>Keyword Strategy</h2>',
        '<h2>Keyword Strategy Limited</h2>',
        1
    )

    limited_note = (
        '<p class="empty-note" style="margin-bottom:14px;">'
        '<strong>Limited data:</strong> The client page could not be crawled, so these are planning suggestions based on industry and market signals, not confirmed keyword gaps from the client page.'
        '</p>'
    )

    if "these are planning suggestions based on industry and market signals" not in html:
        html = html.replace(
            '<div class="keyword-box">',
            limited_note + '<div class="keyword-box">',
            1
        )

    # Rebuild Priority Recommendations for blocked client mode.
    is_plumbing = (not vast_report_is_roofing_html(html)) and any(x in lower for x in ["plumbing", "plumber", "drain cleaning", "water heater", "suffolkplumber"])
    is_roofing = any(x in lower for x in ["roofing", "roofer", "roof repair", "roof replacement"])

    if "suffolk" in lower:
        market = "Suffolk County"
    elif "long island" in lower:
        market = "Long Island"
    elif "nassau" in lower:
        market = "Nassau County"
    else:
        market = "Long Island"

    if is_plumbing:
        primary = f"Plumbing Services {market}"
        secondary_terms = [
            f"Emergency Plumber {market}",
            f"Drain Cleaning {market}",
            f"Water Heater Repair {market}",
            "Residential Plumbing",
            "Commercial Plumbing",
            "Leak Repair",
        ]
        buyer_terms = [
            "Free Plumbing Estimate",
            "Same-Day Plumbing Service",
            "Licensed Plumbing Contractor",
        ]
    elif is_roofing:
        primary = f"Roofing Contractor {market}"
        secondary_terms = [
            f"Roof Repair {market}",
            f"Roof Replacement {market}",
            f"Emergency Roof Repair {market}",
            "Residential Roofing",
            "Commercial Roofing",
            "Roof Inspection",
        ]
        buyer_terms = [
            "Free Roofing Estimate",
            "Licensed Roofing Contractor",
            "Emergency Roofing Service",
        ]
    else:
        primary = f"Local Service Provider {market}"
        secondary_terms = [
            f"Service Provider {market}",
            f"Emergency Service {market}",
            f"Residential Services {market}",
            "Commercial Services",
            "Free Estimate",
        ]
        buyer_terms = [
            "Free Estimate",
            "Local Contractor",
            "Service Quote",
        ]

    secondary_html = "".join(f"<li>{term}</li>" for term in secondary_terms)
    buyer_html = "".join(f"<li>{term}</li>" for term in buyer_terms)

    quickwins_html = (
        '<li>Fix client crawl access first so the app can read the title, meta description, headings, body content, links, and images.</li>'
        '<li>Confirm the page is not blocking the local crawler, bot user agents, or required page resources.</li>'
        '<li>After crawl access is fixed, rerun the report before making final keyword, score, or competitor-gap decisions.</li>'
        '<li>Use the planning keywords on the left only as starter targets until the client page can be fully analyzed.</li>'
    )

    new_section = (
        '<div class="section target-quickwins-section">'
        '<h2>Priority Recommendations</h2>'
        '<div class="target-quickwins-grid">'
        '<div class="target-panel">'
        '<h3 class="keyword-group-title">Planning Target Keywords</h3>'
        '<div class="keyword-box clean-priority-box">'
        '<p class="empty-note" style="margin-bottom:14px;"><strong>Note:</strong> These are planning targets only because the client page was blocked during crawl.</p>'
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Primary Planning Target</h3>'
        '<div class="chip-wrap"><span class="chip chip-high top-chip">🔥 ' + primary + '</span></div>'
        '</div>'
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Secondary Planning Targets</h3>'
        '<ul class="quick-wins-list">' + secondary_html + '</ul>'
        '</div>'
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Buyer-Intent Planning Targets</h3>'
        '<ul class="quick-wins-list">' + buyer_html + '</ul>'
        '</div>'
        '</div>'
        '</div>'
        '<div class="quickwins-panel">'
        '<h3 class="keyword-group-title">Quick Wins</h3>'
        '<div class="site-card clean-priority-card">'
        '<ul class="quick-wins-list">' + quickwins_html + '</ul>'
        '</div>'
        '</div>'
        '</div>'
        '</div>'
    )

    html = re.sub(
        r'<div class="section target-quickwins-section">.*?(?=<div class="section">\s*<h2>Technical SEO Snapshot</h2>)',
        new_section,
        html,
        count=1,
        flags=re.DOTALL,
    )

    # Rewrite analysis summary/action language.
    html = re.sub(
        r'<p><strong>Summary:</strong>.*?</p>',
        '<p><strong>Summary:</strong> The client page could not be fully crawled, so this report should be treated as a technical access check first. Fix crawl access and rerun before relying on score, keyword, or competitor-gap conclusions.</p>',
        html,
        count=1,
        flags=re.DOTALL,
    )

    html = html.replace(
        "Rerun the report with a stronger direct competitor page that has clear service copy, headings, and indexable content.",
        "Fix client crawl access first, then rerun the report with the same client URL."
    )

    html = html.replace(
        "Do not rely on competitor keyword gaps yet. Rerun the report with a stronger direct competitor before choosing final target keywords.",
        "Do not rely on keyword gaps yet. Rerun after the client page can be fully crawled."
    )

    html = html.replace(
        "Use the Keyword Strategy section as page-improvement guidance, and rerun with a stronger competitor before making final gap-based recommendations.",
        "Use the Keyword Strategy section as provisional planning guidance only. Rerun after client crawl access is fixed before making final recommendations."
    )

    html = html.replace(
        "Focus on clear targeting, stronger content depth, and trust-building page elements before chasing broader SEO work.",
        "Fix crawl access first, then use the next report to prioritize targeting, content depth, and trust-building page improvements."
    )

    return html


# === FORCE BLOCKED CLIENT CRAWL MODE V2 ===
# Final safety pass for reports where the client page is blocked/unreadable.

def final_force_blocked_client_crawl_mode_html(html):
    import re

    html = str(html or "")
    lower = html.lower()

    blocked_client = (
        '<div class="site-label">your site</div>' in lower
        and "blocked by site" in lower
        and "<strong>word count:</strong> 0" in lower
    )

    if not blocked_client:
        return html

    # Top warning before Comparison Overview.
    warning = (
        '<div class="section blocked-client-warning-section">'
        '<div class="report-box" style="border-left:6px solid #dc2626;background:#fef2f2;">'
        '<h2 style="margin-top:0;">Client Crawl Warning</h2>'
        '<p><strong>The client page could not be fully crawled.</strong> Title, meta, H1, content, links, images, and keyword checks may be incomplete or unreliable.</p>'
        '<p>Use this report as a technical access check first. Fix crawl access or rerun with a readable page before using competitor data for final SEO recommendations.</p>'
        '</div>'
        '</div>'
    )

    if "Client Crawl Warning" not in html:
        html = html.replace(
            '<div class="card">\n<div class="section">',
            '<div class="card">' + warning + '\n<div class="section">',
            1
        )

    # Cap visible client score card.
    html = re.sub(
        r'(<div class="site-label">Your Site</div>\s*<div class="score">)\d+(</div>)',
        r'\g<1>30\2',
        html,
        count=1,
        flags=re.DOTALL,
    )

    # Cap client total in score table.
    html = re.sub(
        r'(<tr>\s*<td><strong>Total</strong></td>\s*<td class="[^"]*">\s*<strong>)\d+(</strong>)',
        r'\g<1>30\2',
        html,
        count=1,
        flags=re.DOTALL,
    )

    # Rename keyword strategy.
    html = html.replace('<h2>Keyword Strategy</h2>', '<h2>Keyword Strategy Limited</h2>', 1)

    limited_note = (
        '<p class="empty-note" style="margin-bottom:14px;">'
        '<strong>Limited data:</strong> The client page could not be crawled, so these are planning suggestions based on industry and market signals, not confirmed keyword gaps from the client page.'
        '</p>'
    )

    if "these are planning suggestions based on industry and market signals" not in html:
        html = html.replace('<div class="keyword-box">', limited_note + '<div class="keyword-box">', 1)

    # Score note.
    score_note = (
        '<p class="empty-note" style="margin-bottom:14px;">'
        '<strong>Score note:</strong> The client score is capped because the page could not be crawled. Resolve crawl access before treating the score as a normal SEO grade.'
        '</p>'
    )

    if "The client score is capped because the page could not be crawled" not in html:
        html = html.replace(
            '<div class="section">\n            <h2>Score Breakdown</h2>',
            '<div class="section">\n            <h2>Score Breakdown</h2>\n' + score_note,
            1
        )

    # Market/industry planning terms.
    market = "Suffolk County" if "suffolk" in lower else "Long Island"

    primary = f"Plumbing Services {market}"
    secondary_terms = [
        f"Emergency Plumber {market}",
        f"Drain Cleaning {market}",
        f"Water Heater Repair {market}",
        "Residential Plumbing",
        "Commercial Plumbing",
        "Leak Repair",
    ]

    buyer_terms = [
        "Free Plumbing Estimate",
        "Same-Day Plumbing Service",
        "Licensed Plumbing Contractor",
    ]

    secondary_html = "".join(f"<li>{term}</li>" for term in secondary_terms)
    buyer_html = "".join(f"<li>{term}</li>" for term in buyer_terms)

    quickwins_html = (
        '<li>Fix client crawl access first so the app can read the title, meta description, headings, body content, links, and images.</li>'
        '<li>Confirm the site is not blocking the local crawler, bot user agents, or required page resources.</li>'
        '<li>After crawl access is fixed, rerun the report before making final keyword, score, or competitor-gap decisions.</li>'
        '<li>Use the planning keywords on the left only as starter targets until the client page can be fully analyzed.</li>'
    )

    new_priority = (
        '<div class="section target-quickwins-section">'
        '<h2>Priority Recommendations</h2>'
        '<div class="target-quickwins-grid">'
        '<div class="target-panel">'
        '<h3 class="keyword-group-title">Planning Target Keywords</h3>'
        '<div class="keyword-box clean-priority-box">'
        '<p class="empty-note" style="margin-bottom:14px;"><strong>Note:</strong> These are planning targets only because the client page was blocked during crawl.</p>'
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Primary Planning Target</h3>'
        '<div class="chip-wrap"><span class="chip chip-high top-chip">🔥 ' + primary + '</span></div>'
        '</div>'
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Secondary Planning Targets</h3>'
        '<ul class="quick-wins-list">' + secondary_html + '</ul>'
        '</div>'
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Buyer-Intent Planning Targets</h3>'
        '<ul class="quick-wins-list">' + buyer_html + '</ul>'
        '</div>'
        '</div>'
        '</div>'
        '<div class="quickwins-panel">'
        '<h3 class="keyword-group-title">Quick Wins</h3>'
        '<div class="site-card clean-priority-card">'
        '<ul class="quick-wins-list">' + quickwins_html + '</ul>'
        '</div>'
        '</div>'
        '</div>'
        '</div>'
    )

    html = re.sub(
        r'<div class="section target-quickwins-section">.*?(?=<div class="section">\s*<h2>Technical SEO Snapshot</h2>)',
        new_priority,
        html,
        count=1,
        flags=re.DOTALL,
    )

    # Rewrite analysis summary and stale competitor wording.
    html = re.sub(
        r'<p><strong>Summary:</strong>.*?</p>',
        '<p><strong>Summary:</strong> The client page could not be fully crawled, so this report should be treated as a technical access check first. Fix crawl access and rerun before relying on score, keyword, or competitor-gap conclusions.</p>',
        html,
        count=1,
        flags=re.DOTALL,
    )

    replacements = {
        "Rerun the report with a stronger direct competitor page that has clear service copy, headings, and indexable content.": "Fix client crawl access first, then rerun the report with the same client URL.",
        "Do not rely on competitor keyword gaps yet. Rerun the report with a stronger direct competitor before choosing final target keywords.": "Do not rely on keyword gaps yet. Rerun after the client page can be fully crawled.",
        "Use the Keyword Strategy section as page-improvement guidance, and rerun with a stronger competitor before making final gap-based recommendations.": "Use the Keyword Strategy section as provisional planning guidance only. Rerun after client crawl access is fixed before making final recommendations.",
        "Focus on clear targeting, stronger content depth, and trust-building page elements before chasing broader SEO work.": "Fix crawl access first, then use the next report to prioritize targeting, content depth, and trust-building page improvements.",
    }

    for old, new in replacements.items():
        html = html.replace(old, new)

    return html


# === BLOCKED CLIENT ACCESS PAGE MIDDLEWARE ===
# If the client page is blocked/unreadable, show a clean access-check page instead of a broken-looking report.

@app.middleware("http")
async def vast_blocked_client_access_page_middleware(request, call_next):
    import re
    from fastapi.responses import HTMLResponse, Response

    if request.query_params.get("show_raw") == "1":
        return await call_next(request)

    path = request.url.path or ""

    if path not in {"/analyze"} and not path.startswith("/history/rerun"):
        return await call_next(request)

    response = await call_next(request)

    content_type = response.headers.get("content-type", "")
    if "text/html" not in content_type:
        return response

    body_chunks = []
    async for chunk in response.body_iterator:
        body_chunks.append(chunk)

    body = b"".join(body_chunks)
    html = body.decode("utf-8", errors="replace")
    lower = html.lower()

    blocked_client = (
        '<div class="site-label">your site</div>' in lower
        and "blocked by site" in lower
        and (
            "<strong>word count:</strong> 0" in lower
            or "<strong>h1 count:</strong> 0" in lower
        )
    )

    if not blocked_client:
        headers = dict(response.headers)
        headers.pop("content-length", None)
        return Response(content=body, status_code=response.status_code, headers=headers, media_type="text/html")

    def extract(pattern, fallback="Not detected"):
        match = re.search(pattern, html, flags=re.DOTALL | re.IGNORECASE)
        if not match:
            return fallback
        value = re.sub(r"<.*?>", "", match.group(1))
        value = " ".join(value.split()).strip()
        return value or fallback

    client_domain = extract(
        r'<div class="site-label">\s*Your Site\s*</div>.*?<div class="site-url">(.+?)</div>',
        "Client site"
    )

    competitor_domain = extract(
        r'<div class="site-label">\s*Competitor 1\s*</div>.*?<div class="site-url">(.+?)</div>',
        "Competitor site"
    )

    if "suffolk" in lower:
        market = "Suffolk County / Long Island"
    elif "nassau" in lower:
        market = "Nassau County / Long Island"
    elif "long island" in lower:
        market = "Long Island"
    else:
        market = "Local market"

    if any(x in lower for x in ["plumbing", "plumber", "drain cleaning", "water heater"]):
        industry = "Plumbing"
        planning_terms = [
            f"Plumbing Services {market}",
            f"Emergency Plumber {market}",
            f"Drain Cleaning {market}",
            "Residential Plumbing",
            "Commercial Plumbing",
            "Water Heater Repair",
        ]
    elif any(x in lower for x in ["roofing", "roofer", "roof repair", "roof replacement"]):
        industry = "Roofing"
        planning_terms = [
            f"Roofing Contractor {market}",
            f"Roof Repair {market}",
            f"Roof Replacement {market}",
            "Residential Roofing",
            "Commercial Roofing",
            "Emergency Roof Repair",
        ]
    else:
        industry = "Local Service"
        planning_terms = [
            f"Local Service Provider {market}",
            f"Emergency Service {market}",
            "Residential Services",
            "Commercial Services",
            "Free Estimate",
        ]

    current_url = str(request.url)
    separator = "&" if "?" in current_url else "?"
    raw_url = current_url + separator + "show_raw=1"

    terms_html = "".join(f"<li>{term}</li>" for term in planning_terms)

    clean_page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LeadMeLeads Crawl Access Check</title>
<style>
body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
    background: linear-gradient(135deg, #0f172a, #1e3a8a);
    margin: 0;
    padding: 40px;
    color: #0f172a;
}}
.container {{
    max-width: 1050px;
    margin: 0 auto;
}}
.header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    color: white;
    margin-bottom: 26px;
}}
.header h1 {{
    margin: 0;
    font-size: 34px;
}}
.header p {{
    margin: 8px 0 0 0;
    opacity: .9;
}}
.actions {{
    display: flex;
    gap: 10px;
}}
.actions a {{
    background: white;
    color: #1e3a8a;
    padding: 11px 16px;
    border-radius: 9px;
    text-decoration: none;
    font-weight: 800;
}}
.card {{
    background: white;
    border-radius: 18px;
    box-shadow: 0 20px 50px rgba(15, 23, 42, 0.18);
    overflow: hidden;
}}
.mini-alert {{
    margin: 0;
    padding: 16px 34px;
    background: #fffbeb;
    border-left: 6px solid #f59e0b;
    font-size: 16px;
    line-height: 1.5;
}}
.mini-alert a {{
    color: #1e3a8a;
    font-weight: 800;
    text-decoration: none;
}}
.warning {{
    background: #fef2f2;
    border-left: 8px solid #dc2626;
    padding: 26px 28px;
    border-radius: 14px;
}}
.warning h2 {{
    margin: 0 0 12px 0;
    font-size: 26px;
}}
.warning p {{
    font-size: 16px;
    line-height: 1.65;
    margin: 0 0 10px 0;
}}
.grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 22px;
    padding: 34px;
}}
.box {{
    border: 1px solid #dbe4f0;
    border-radius: 14px;
    padding: 24px;
    background: #f9fbff;
}}
.box h3 {{
    margin: 0 0 14px 0;
    font-size: 22px;
}}
.label {{
    font-size: 12px;
    text-transform: uppercase;
    font-weight: 900;
    letter-spacing: .08em;
    color: #475569;
    margin-bottom: 8px;
}}
.big {{
    font-size: 25px;
    font-weight: 900;
    word-break: break-word;
}}
.status {{
    display: inline-block;
    margin-top: 14px;
    padding: 7px 12px;
    border-radius: 999px;
    font-weight: 800;
    font-size: 13px;
    background: #fee2e2;
    color: #991b1b;
}}
.section {{
    padding: 0 34px 34px 34px;
}}
.section h2 {{
    font-size: 27px;
    margin: 0 0 16px 0;
}}
.steps {{
    margin: 0;
    padding-left: 22px;
}}
.steps li {{
    margin-bottom: 12px;
    line-height: 1.6;
    font-size: 16px;
}}
.note {{
    background: #eff6ff;
    border-left: 6px solid #1e3a8a;
    padding: 20px;
    border-radius: 12px;
    margin-top: 22px;
}}
.footer {{
    padding: 22px 34px;
    color: #64748b;
    border-top: 1px solid #e5e7eb;
}}
@media (max-width: 800px) {{
    body {{ padding: 20px; }}
    .header {{ flex-direction: column; align-items: flex-start; gap: 16px; }}
    .grid {{ grid-template-columns: 1fr; padding: 24px; }}
    .section {{ padding: 0 24px 24px 24px; }}
}}


</style>
</head>
<body>
<div class="container">
    <div class="header">
        <div>
            <h1>Crawl Access Check</h1>
            <p>LeadMeLeads could not fully read the client page, so this is not a normal competitor report.</p>
        </div>
        <div class="actions">
            <a href="/">← Home</a>
            <a href="/history">History</a>
            <a href="{raw_url}">View Raw Report</a>
        </div>
    </div>

    <div class="card">
        <div class="mini-alert">
            <strong>Client crawl was limited.</strong>
            The app could not fully read {client_domain}. 
            <a href="#crawl-details">View details ↓</a>
        </div>

        <div class="grid">
            <div class="box">
                <div class="label">Client Site</div>
                <div class="big">{client_domain}</div>
                <span class="status">Crawl Blocked</span>
            </div>

            <div class="box">
                <div class="label">Compared Against</div>
                <div class="big">{competitor_domain}</div>
                <span class="status" style="background:#e0ecff;color:#1e3a8a;">Not Final Strategy</span>
            </div>
        </div>

        <div class="section">
            <h2>What to Fix First</h2>
            <ol class="steps">
                <li>Open the client URL in a browser and confirm the page loads normally.</li>
                <li>Check whether the site is blocking local crawlers, bot user agents, security tools, or required page resources.</li>
                <li>Try a more specific service page if the homepage blocks crawling.</li>
                <li>Rerun the report after the app can read the client page.</li>
            </ol>

            <div class="note">
                <strong>Planning keywords only:</strong>
                <ul>
                    {terms_html}
                </ul>
            </div>
        </div>

        <div class="section" id="crawl-details">
            <div class="warning">
                <h2>Client Page Blocked or Unreadable</h2>
                <p><strong>{client_domain}</strong> could not be fully crawled. The app could not reliably read the page title, meta description, headings, body content, links, images, or keyword coverage.</p>
                <p>Fix crawl access first, then rerun the report before using scores, keyword gaps, or competitor comparisons.</p>
            </div>
        </div>

        <div class="section">
            <h2>Why This Matters</h2>
            <p style="line-height:1.7;font-size:16px;">Without a readable client page, the app cannot fairly score SEO basics, compare keyword coverage, or produce reliable next-step recommendations. This access check prevents a broken crawl from looking like a finished client report.</p>
        </div>

        <div class="footer">Courtesy of LeadMeLeads</div>
    </div>
</div>
</body>
</html>"""

    return HTMLResponse(clean_page, status_code=response.status_code)


# === FLUFFY KEYWORD GUARD ===
# Prevents vague phrases like "Leading Plumbing Experts" from becoming primary targets.

def final_fluffy_keyword_guard_html(html):
    import re

    html = str(html or "")
    lower = html.lower()

    is_plumbing = (not vast_report_is_roofing_html(html)) and any(x in lower for x in [
        "plumbing",
        "plumber",
        "drain cleaning",
        "water heater",
        "sewer drain",
    ])

    if not is_plumbing:
        return html

    fluffy_phrases = [
        "Leading Plumbing Experts",
        "Plumbing Experts Shirley",
        "Plumbing Experts",
        "Trusted Plumbing Experts",
        "Expert Plumbing Experts",
        "Top Plumbing Experts",
        "Quality Plumbing Experts",
        "Leading Experts",
        "Trusted Professionals",
        "Quality Service",
        "Expert Services",
    ]

    # Detect client market first.
    if "yaphank" in lower:
        market = "Yaphank"
    elif "patchogue" in lower:
        market = "Patchogue"
    elif "suffolk" in lower:
        market = "Suffolk County"
    elif "nassau" in lower:
        market = "Nassau County"
    elif "long island" in lower:
        market = "Long Island"
    elif "northport" in lower:
        market = "Northport"
    elif "hicksville" in lower:
        market = "Hicksville"
    else:
        market = "Long Island"

    primary = f"Plumbing Services {market}"

    secondary_terms = [
        f"Emergency Plumber {market}",
        f"Drain Cleaning {market}",
        f"Water Heater Repair {market}",
        f"Plumbing Repair {market}",
        "Residential Plumbing",
        "Commercial Plumbing",
    ]

    buyer_terms = [
        "Free Plumbing Estimate",
        "Same-Day Plumbing Service",
        "Licensed Plumbing Contractor",
    ]

    # If a fluffy phrase appears in the priority section, rebuild the whole section.
    priority_match = re.search(
        r'<div class="section target-quickwins-section">.*?(?=<div class="section">\s*<h2>Technical SEO Snapshot</h2>)',
        html,
        flags=re.DOTALL,
    )

    priority_html = priority_match.group(0) if priority_match else ""
    has_fluffy_priority = any(phrase.lower() in priority_html.lower() for phrase in fluffy_phrases)

    if has_fluffy_priority:
        secondary_html = "".join(f"<li>{term}</li>" for term in secondary_terms)
        buyer_html = "".join(f"<li>{term}</li>" for term in buyer_terms)

        quickwins_html = (
            f"<li>Tighten the title tag while keeping a clear service and location phrase like “{primary}” prominent.</li>"
            f"<li>Strengthen the H1 and opening copy with a natural phrase such as “{primary}” or “Plumbing and Heating {market}.”</li>"
            "<li>Add supporting sections for drain cleaning, water heater repair, emergency plumbing, leak repair, and residential/commercial plumbing.</li>"
            "<li>Add missing image alt text using natural service descriptions instead of repeating vague keyword fragments.</li>"
        )

        new_priority = (
            '<div class="section target-quickwins-section">'
            '<h2>Priority Recommendations</h2>'
            '<div class="target-quickwins-grid">'
            '<div class="target-panel">'
            '<h3 class="keyword-group-title">Recommended Target Keywords</h3>'
            '<div class="keyword-box clean-priority-box">'
            '<div class="keyword-group">'
            '<h3 class="keyword-group-title">Primary Target</h3>'
            '<div class="chip-wrap"><span class="chip chip-high top-chip">🔥 ' + primary + '</span></div>'
            '</div>'
            '<div class="keyword-group">'
            '<h3 class="keyword-group-title">Secondary Targets</h3>'
            '<ul class="quick-wins-list">' + secondary_html + '</ul>'
            '</div>'
            '<div class="keyword-group">'
            '<h3 class="keyword-group-title">Buyer-Intent Targets</h3>'
            '<ul class="quick-wins-list">' + buyer_html + '</ul>'
            '</div>'
            '</div>'
            '</div>'
            '<div class="quickwins-panel">'
            '<h3 class="keyword-group-title">Quick Wins</h3>'
            '<div class="site-card clean-priority-card">'
            '<ul class="quick-wins-list">' + quickwins_html + '</ul>'
            '</div>'
            '</div>'
            '</div>'
            '</div>'
        )

        html = re.sub(
            r'<div class="section target-quickwins-section">.*?(?=<div class="section">\s*<h2>Technical SEO Snapshot</h2>)',
            new_priority,
            html,
            count=1,
            flags=re.DOTALL,
        )

    # Clean fluffy phrases from the action plan too.
    for phrase in fluffy_phrases:
        html = html.replace(phrase, primary)

    html = html.replace(
        "Use the competitor keyword gaps to improve page relevance and identify new sections, FAQs, or service/location content worth adding.",
        "Use the keyword strategy section to strengthen service, location, and buyer-intent language on the client page."
    )

    html = html.replace(
        f"Start by reviewing <strong>{primary}</strong>. Use it only where it fits naturally, then review the Keyword Strategy section above for other relevant service, location, and commercial terms.",
        f"Start by strengthening <strong>{primary}</strong>, then support it with related service terms like drain cleaning, water heater repair, emergency plumbing, and leak repair."
    )

    html = re.sub(
        r'Work “[^”]*Experts[^”]*” into the page only if it accurately matches the client’s services and location targeting\.',
        f'Strengthen the page with clear plumbing service and location language such as “{primary}” where it naturally fits.',
        html,
        flags=re.IGNORECASE,
    )

    html = html.replace("installation repairs", "plumbing repairs")
    html = html.replace("Installation Repairs", "Plumbing Repairs")

    return html


# === FORCE ROOFING TERMS OVERRIDE ===
# Final safety pass: if the report is roofing, never allow plumbing keyword buckets to survive.

def final_force_roofing_terms_override_html(html):
    import re

    html = str(html or "")
    lower = html.lower()

    is_roofing_report = any(x in lower for x in [
        "longislandroofing.com",
        "liroofing.com",
        "roofing",
        "roofer",
        "roofers",
        "roof repair",
        "roof replacement",
        "commercial roofing",
        "residential roofing",
    ])

    if not is_roofing_report:
        return html

    service_terms = [
        "Roof Repair",
        "Roof Replacement",
        "Emergency Roof Repair",
        "Residential Roofing",
        "Commercial Roofing",
        "Roof Inspection",
        "Roof Maintenance",
        "Flat Roof Repair",
    ]

    location_terms = [
        "Roofing Contractor Long Island",
        "Roof Repair Long Island",
        "Roof Replacement Long Island",
        "Emergency Roof Repair Long Island",
        "Residential Roofing Long Island",
        "Commercial Roofing Long Island",
    ]

    buyer_terms = [
        "Free Roofing Estimate",
        "Roofing Contractor",
        "Local Roofing Contractor",
        "Emergency Roofing Service",
        "Licensed Roofing Contractor",
    ]

    secondary_terms = [
        "Roof Repair Long Island",
        "Roof Replacement Long Island",
        "Emergency Roof Repair Long Island",
        "Residential Roofing",
        "Commercial Roofing",
        "Roof Inspection",
    ]

    buyer_target_terms = [
        "Free Roofing Estimate",
        "Licensed Roofing Contractor",
        "Emergency Roofing Service",
    ]

    def chips(terms):
        return "".join(
            '<span class="chip chip-medium">⭐ ' + term + '</span>'
            for term in terms
        )

    def list_items(terms):
        return "".join("<li>" + term + "</li>" for term in terms)

    service_block = (
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Service Terms to Strengthen</h3>'
        '<p class="signals-intro">Use these roofing-specific terms to clarify the page’s main services and support stronger topical relevance.</p>'
        '<div class="chip-wrap">' + chips(service_terms) + '</div>'
        '</div>'
    )

    location_block = (
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Location Terms to Strengthen</h3>'
        '<p class="signals-intro">Use these Long Island roofing terms for service-area copy, internal links, FAQs, and dedicated location sections.</p>'
        '<div class="chip-wrap">' + chips(location_terms) + '</div>'
        '</div>'
    )

    buyer_block = (
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Buyer-Intent Terms to Strengthen</h3>'
        '<p class="signals-intro">Use these buyer-intent terms to improve calls to action, quote pages, and lead-focused copy.</p>'
        '<div class="chip-wrap">' + chips(buyer_terms) + '</div>'
        '</div>'
    )

    html = re.sub(
        r'<div class="keyword-group">\s*<h3 class="keyword-group-title">Service Terms to Strengthen</h3>.*?</div>\s*</div>',
        service_block,
        html,
        count=1,
        flags=re.DOTALL,
    )

    html = re.sub(
        r'<div class="keyword-group">\s*<h3 class="keyword-group-title">Location Terms to Strengthen</h3>.*?</div>\s*</div>',
        location_block,
        html,
        count=1,
        flags=re.DOTALL,
    )

    if "Buyer-Intent Terms to Strengthen" in html:
        html = re.sub(
            r'<div class="keyword-group">\s*<h3 class="keyword-group-title">Buyer-Intent Terms to Strengthen</h3>.*?</div>\s*</div>',
            buyer_block,
            html,
            count=1,
            flags=re.DOTALL,
        )
    else:
        html = html.replace(
            '<div class="keyword-group">\n                    <h3 class="keyword-group-title">Shared Topic Signals',
            buyer_block + '\n<div class="keyword-group">\n                    <h3 class="keyword-group-title">Shared Topic Signals',
            1
        )

    quickwins_html = (
        '<li>Tighten the title tag while keeping a clear roofing service and Long Island location phrase prominent.</li>'
        '<li>Strengthen the H1 and opening copy with a natural phrase such as “Roofing Contractor Long Island” or “Roof Repair Long Island.”</li>'
        '<li>Add supporting sections for roof repair, roof replacement, emergency roofing, residential roofing, and commercial roofing.</li>'
        '<li>Add missing image alt text using natural roofing service descriptions instead of unrelated service terms.</li>'
    )

    new_priority = (
        '<div class="section target-quickwins-section">'
        '<h2>Priority Recommendations</h2>'
        '<div class="target-quickwins-grid">'
        '<div class="target-panel">'
        '<h3 class="keyword-group-title">Recommended Target Keywords</h3>'
        '<div class="keyword-box clean-priority-box">'
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Primary Target</h3>'
        '<div class="chip-wrap"><span class="chip chip-high top-chip">🔥 Roofing Contractor Long Island</span></div>'
        '</div>'
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Secondary Targets</h3>'
        '<ul class="quick-wins-list">' + list_items(secondary_terms) + '</ul>'
        '</div>'
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Buyer-Intent Targets</h3>'
        '<ul class="quick-wins-list">' + list_items(buyer_target_terms) + '</ul>'
        '</div>'
        '</div>'
        '</div>'
        '<div class="quickwins-panel">'
        '<h3 class="keyword-group-title">Quick Wins</h3>'
        '<div class="site-card clean-priority-card">'
        '<ul class="quick-wins-list">' + quickwins_html + '</ul>'
        '</div>'
        '</div>'
        '</div>'
        '</div>'
    )

    html = re.sub(
        r'<div class="section target-quickwins-section">.*?(?=<div class="section">\s*<h2>Technical SEO Snapshot</h2>)',
        new_priority,
        html,
        count=1,
        flags=re.DOTALL,
    )

    # Kill plumbing terms if they slipped into roofing report text.
    plumbing_to_roofing = {
        "Drain Cleaning": "Roof Repair",
        "Leak Repair": "Roof Leak Repair",
        "Water Heater Repair": "Roof Replacement",
        "Emergency Plumbing": "Emergency Roof Repair",
        "Residential Plumbing": "Residential Roofing",
        "Commercial Plumbing": "Commercial Roofing",
        "Sewer Drain Cleaning": "Roof Inspection",
        "Pipe Repair": "Roof Maintenance",
        "Plumbing Services Long Island": "Roofing Contractor Long Island",
        "Emergency Plumber Long Island": "Emergency Roof Repair Long Island",
        "Free Plumbing Estimate": "Free Roofing Estimate",
        "Same-Day Plumber": "Emergency Roofing Service",
        "Licensed Plumbing Contractor": "Licensed Roofing Contractor",
        "Trusted Emergency Plumber": "Emergency Roofing Service",
    }

    for old, new in plumbing_to_roofing.items():
        html = html.replace(old, new)

    html = html.replace(
        "Use these plumbing-specific terms",
        "Use these roofing-specific terms"
    )

    html = html.replace(
        "Use the keyword strategy section to strengthen service, location, and buyer-intent language on the client page.",
        "Use the keyword strategy section to strengthen roofing service, location, and buyer-intent language on the client page."
    )

    return html


# === ROOFING FINAL RENDER MIDDLEWARE ===
# Final HTML middleware guard: if rendered report is roofing, replace plumbing keyword leakage.

@app.middleware("http")
async def vast_roofing_final_render_middleware(request, call_next):
    import re
    from fastapi.responses import Response

    response = await call_next(request)

    path = request.url.path or ""
    if path != "/analyze" and not path.startswith("/history/rerun") and not path.startswith("/reports/saved"):
        return response

    content_type = response.headers.get("content-type", "")
    if "text/html" not in content_type:
        return response

    body_chunks = []
    async for chunk in response.body_iterator:
        body_chunks.append(chunk)

    body = b"".join(body_chunks)
    html = body.decode("utf-8", errors="replace")
    lower = html.lower()

    is_roofing = any(x in lower for x in [
        "longislandroofing.com",
        "liroofing.com",
        "roofing",
        "roofer",
        "roofers",
        "roof repair",
        "roof replacement",
        "commercial roofing",
        "residential roofing",
    ])

    if not is_roofing:
        headers = dict(response.headers)
        headers.pop("content-length", None)
        return Response(content=body, status_code=response.status_code, headers=headers, media_type="text/html")

    service_terms = [
        "Roof Repair",
        "Roof Replacement",
        "Emergency Roof Repair",
        "Residential Roofing",
        "Commercial Roofing",
        "Roof Inspection",
        "Roof Maintenance",
        "Flat Roof Repair",
    ]

    location_terms = [
        "Roofing Contractor Long Island",
        "Roof Repair Long Island",
        "Roof Replacement Long Island",
        "Emergency Roof Repair Long Island",
        "Residential Roofing Long Island",
        "Commercial Roofing Long Island",
    ]

    buyer_terms = [
        "Free Roofing Estimate",
        "Roofing Contractor",
        "Local Roofing Contractor",
        "Emergency Roofing Service",
        "Licensed Roofing Contractor",
    ]

    secondary_terms = [
        "Roof Repair Long Island",
        "Roof Replacement Long Island",
        "Emergency Roof Repair Long Island",
        "Residential Roofing",
        "Commercial Roofing",
        "Roof Inspection",
    ]

    buyer_target_terms = [
        "Free Roofing Estimate",
        "Licensed Roofing Contractor",
        "Emergency Roofing Service",
    ]

    def chips(terms):
        return "".join('<span class="chip chip-medium">⭐ ' + term + '</span>' for term in terms)

    def lis(terms):
        return "".join("<li>" + term + "</li>" for term in terms)

    service_block = (
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Service Terms to Strengthen</h3>'
        '<p class="signals-intro">Use these roofing-specific terms to clarify the page’s main services and support stronger topical relevance.</p>'
        '<div class="chip-wrap">' + chips(service_terms) + '</div>'
        '</div>'
    )

    location_block = (
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Location Terms to Strengthen</h3>'
        '<p class="signals-intro">Use these Long Island roofing terms for service-area copy, internal links, FAQs, and dedicated location sections.</p>'
        '<div class="chip-wrap">' + chips(location_terms) + '</div>'
        '</div>'
    )

    buyer_block = (
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Buyer-Intent Terms to Strengthen</h3>'
        '<p class="signals-intro">Use these buyer-intent terms to improve calls to action, quote pages, and lead-focused copy.</p>'
        '<div class="chip-wrap">' + chips(buyer_terms) + '</div>'
        '</div>'
    )

    html = re.sub(
        r'<div class="keyword-group">\s*<h3 class="keyword-group-title">Service Terms to Strengthen</h3>.*?</div>\s*</div>',
        service_block,
        html,
        count=1,
        flags=re.DOTALL,
    )

    html = re.sub(
        r'<div class="keyword-group">\s*<h3 class="keyword-group-title">Location Terms to Strengthen</h3>.*?</div>\s*</div>',
        location_block,
        html,
        count=1,
        flags=re.DOTALL,
    )

    html = re.sub(
        r'<div class="keyword-group">\s*<h3 class="keyword-group-title">Buyer-Intent Terms to Strengthen</h3>.*?</div>\s*</div>',
        buyer_block,
        html,
        count=1,
        flags=re.DOTALL,
    )

    quickwins_html = (
        '<li>Tighten the title tag while keeping a clear roofing service and Long Island location phrase prominent.</li>'
        '<li>Strengthen the H1 and opening copy with a natural phrase such as “Roofing Contractor Long Island” or “Roof Repair Long Island.”</li>'
        '<li>Add supporting sections for roof repair, roof replacement, emergency roofing, residential roofing, and commercial roofing.</li>'
        '<li>Add missing image alt text using natural roofing service descriptions instead of unrelated service terms.</li>'
    )

    new_priority = (
        '<div class="section target-quickwins-section">'
        '<h2>Priority Recommendations</h2>'
        '<div class="target-quickwins-grid">'
        '<div class="target-panel">'
        '<h3 class="keyword-group-title">Recommended Target Keywords</h3>'
        '<div class="keyword-box clean-priority-box">'
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Primary Target</h3>'
        '<div class="chip-wrap"><span class="chip chip-high top-chip">🔥 Roofing Contractor Long Island</span></div>'
        '</div>'
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Secondary Targets</h3>'
        '<ul class="quick-wins-list">' + lis(secondary_terms) + '</ul>'
        '</div>'
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Buyer-Intent Targets</h3>'
        '<ul class="quick-wins-list">' + lis(buyer_target_terms) + '</ul>'
        '</div>'
        '</div>'
        '</div>'
        '<div class="quickwins-panel">'
        '<h3 class="keyword-group-title">Quick Wins</h3>'
        '<div class="site-card clean-priority-card">'
        '<ul class="quick-wins-list">' + quickwins_html + '</ul>'
        '</div>'
        '</div>'
        '</div>'
        '</div>'
    )

    html = re.sub(
        r'<div class="section target-quickwins-section">.*?(?=<div class="section">\s*<h2>Technical SEO Snapshot</h2>)',
        new_priority,
        html,
        count=1,
        flags=re.DOTALL,
    )

    replacements = {
        "Use these plumbing-specific terms": "Use these roofing-specific terms",
        "Drain Cleaning": "Roof Repair",
        "Leak Repair": "Roof Leak Repair",
        "Water Heater Repair": "Roof Replacement",
        "Emergency Plumbing": "Emergency Roof Repair",
        "Residential Plumbing": "Residential Roofing",
        "Commercial Plumbing": "Commercial Roofing",
        "Sewer Drain Cleaning": "Roof Inspection",
        "Pipe Repair": "Roof Maintenance",
        "Plumbing Services Long Island": "Roofing Contractor Long Island",
        "Emergency Plumber Long Island": "Emergency Roof Repair Long Island",
        "Free Plumbing Estimate": "Free Roofing Estimate",
        "Same-Day Plumber": "Emergency Roofing Service",
        "Licensed Plumbing Contractor": "Licensed Roofing Contractor",
        "Trusted Emergency Plumber": "Emergency Roofing Service",
    }

    for old, new in replacements.items():
        html = html.replace(old, new)

    headers = dict(response.headers)
    headers.pop("content-length", None)
    return Response(content=html.encode("utf-8"), status_code=response.status_code, headers=headers, media_type="text/html")


# === INDUSTRY REPORT DETECTION HELPERS ===

def vast_report_is_roofing_html(html):
    blob = str(html or "").lower()
    return any(x in blob for x in [
        "roofing",
        "roofer",
        "roofers",
        "roof repair",
        "roof replacement",
        "residential roofing",
        "commercial roofing",
        "longislandroofing.com",
        "liroofing.com",
        "roofrepairslongisland.com",
    ])

def vast_report_is_plumbing_html(html):
    blob = str(html or "").lower()
    if vast_report_is_roofing_html(blob):
        return False
    return any(x in blob for x in [
        "plumbing",
        "plumber",
        "drain cleaning",
        "water heater",
        "sewer drain",
        "pipe repair",
    ])


# === CESSPOOL / SEPTIC INDUSTRY OVERRIDE ===
# Final safety pass: cesspool/septic reports should not use generic plumbing keyword buckets.

def final_cesspool_industry_override_html(html):
    import re

    html = str(html or "")
    lower = html.lower()

    is_cesspool = any(x in lower for x in [
        "cesspool",
        "septic",
        "jwcesspool.com",
        "qualitycesspool.com",
        "sewer and drain cleaning",
        "sewer & drain cleaning",
    ])

    if not is_cesspool:
        return html

    if "selden" in lower:
        local_market = "Suffolk County"
    elif "suffolk" in lower:
        local_market = "Suffolk County"
    elif "long island" in lower:
        local_market = "Long Island"
    else:
        local_market = "Long Island"

    service_terms = [
        "Cesspool Cleaning",
        "Cesspool Pumping",
        "Cesspool Repair",
        "Septic Services",
        "Septic Tank Cleaning",
        "Sewer and Drain Cleaning",
        "Emergency Cesspool Service",
        "Cesspool Installation",
    ]

    location_terms = [
        f"Cesspool Services {local_market}",
        f"Cesspool Cleaning {local_market}",
        f"Cesspool Pumping {local_market}",
        f"Septic Services {local_market}",
        f"Emergency Cesspool Service {local_market}",
        f"Sewer and Drain Cleaning {local_market}",
    ]

    buyer_terms = [
        "Free Cesspool Estimate",
        "Emergency Cesspool Service",
        "Licensed Cesspool Company",
        "Local Septic Contractor",
        "Same-Day Cesspool Service",
    ]

    secondary_terms = [
        f"Cesspool Cleaning {local_market}",
        f"Cesspool Pumping {local_market}",
        f"Septic Services {local_market}",
        f"Sewer and Drain Cleaning {local_market}",
        "Emergency Cesspool Service",
        "Cesspool Repair",
    ]

    buyer_target_terms = [
        "Free Cesspool Estimate",
        "Emergency Cesspool Service",
        "Licensed Cesspool Company",
    ]

    primary = f"Cesspool Services {local_market}"

    def chips(terms):
        return "".join(
            '<span class="chip chip-medium">⭐ ' + term + '</span>'
            for term in terms
        )

    def lis(terms):
        return "".join("<li>" + term + "</li>" for term in terms)

    service_block = (
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Service Terms to Strengthen</h3>'
        '<p class="signals-intro">Use these cesspool and septic-specific terms to clarify the page’s main services and support stronger topical relevance.</p>'
        '<div class="chip-wrap">' + chips(service_terms) + '</div>'
        '</div>'
    )

    location_block = (
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Location Terms to Strengthen</h3>'
        '<p class="signals-intro">Use these local cesspool and septic terms for service-area copy, internal links, FAQs, and dedicated location sections.</p>'
        '<div class="chip-wrap">' + chips(location_terms) + '</div>'
        '</div>'
    )

    buyer_block = (
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Buyer-Intent Terms to Strengthen</h3>'
        '<p class="signals-intro">Use these buyer-intent terms to improve calls to action, estimate pages, and lead-focused copy.</p>'
        '<div class="chip-wrap">' + chips(buyer_terms) + '</div>'
        '</div>'
    )

    html = re.sub(
        r'<div class="keyword-group">\s*<h3 class="keyword-group-title">Service Terms to Strengthen</h3>.*?</div>\s*</div>',
        service_block,
        html,
        count=1,
        flags=re.DOTALL,
    )

    html = re.sub(
        r'<div class="keyword-group">\s*<h3 class="keyword-group-title">Location Terms to Strengthen</h3>.*?</div>\s*</div>',
        location_block,
        html,
        count=1,
        flags=re.DOTALL,
    )

    html = re.sub(
        r'<div class="keyword-group">\s*<h3 class="keyword-group-title">Buyer-Intent Terms to Strengthen</h3>.*?</div>\s*</div>',
        buyer_block,
        html,
        count=1,
        flags=re.DOTALL,
    )

    quickwins_html = (
        f'<li>Strengthen the H1 and opening copy with a natural phrase such as “{primary}.”</li>'
        '<li>Add supporting sections for cesspool cleaning, cesspool pumping, septic services, sewer and drain cleaning, and emergency service.</li>'
        '<li>Add image alt text using real cesspool, septic, sewer, drain, truck, equipment, and service-area descriptions.</li>'
        '<li>Use licensed, emergency, local, and same-day language only where it accurately reflects the business.</li>'
    )

    new_priority = (
        '<div class="section target-quickwins-section">'
        '<h2>Priority Recommendations</h2>'
        '<div class="target-quickwins-grid">'
        '<div class="target-panel">'
        '<h3 class="keyword-group-title">Recommended Target Keywords</h3>'
        '<div class="keyword-box clean-priority-box">'
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Primary Target</h3>'
        '<div class="chip-wrap"><span class="chip chip-high top-chip">🔥 ' + primary + '</span></div>'
        '</div>'
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Secondary Targets</h3>'
        '<ul class="quick-wins-list">' + lis(secondary_terms) + '</ul>'
        '</div>'
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Buyer-Intent Targets</h3>'
        '<ul class="quick-wins-list">' + lis(buyer_target_terms) + '</ul>'
        '</div>'
        '</div>'
        '</div>'
        '<div class="quickwins-panel">'
        '<h3 class="keyword-group-title">Quick Wins</h3>'
        '<div class="site-card clean-priority-card">'
        '<ul class="quick-wins-list">' + quickwins_html + '</ul>'
        '</div>'
        '</div>'
        '</div>'
        '</div>'
    )

    html = re.sub(
        r'<div class="section target-quickwins-section">.*?(?=<div class="section">\s*<h2>Technical SEO Snapshot</h2>)',
        new_priority,
        html,
        count=1,
        flags=re.DOTALL,
    )

    bad_phrases = [
        "Repairs Licensed Local",
        "repairs licensed local",
        "Repairs Licensed",
        "repairs licensed",
    ]

    for phrase in bad_phrases:
        html = html.replace(phrase, primary)

    # Clean generic plumbing leftovers in cesspool reports.
    replacements = {
        "Use these plumbing-specific terms": "Use these cesspool and septic-specific terms",
        "Emergency Plumbing": "Emergency Cesspool Service",
        "Residential Plumbing": "Residential Cesspool Services",
        "Commercial Plumbing": "Commercial Cesspool Services",
        "Water Heater Repair": "Septic Tank Cleaning",
        "Leak Repair": "Cesspool Repair",
        "Pipe Repair": "Cesspool Repair",
        "Plumbing Services Long Island": "Cesspool Services Long Island",
        "Emergency Plumber Long Island": "Emergency Cesspool Service Long Island",
        "Free Plumbing Estimate": "Free Cesspool Estimate",
        "Emergency Plumbing Service": "Emergency Cesspool Service",
        "Same-Day Plumber": "Same-Day Cesspool Service",
        "Licensed Plumbing Contractor": "Licensed Cesspool Company",
        "Trusted Emergency Plumber": "Emergency Cesspool Service",
    }

    for old, new in replacements.items():
        html = html.replace(old, new)

    html = html.replace(
        "Add focused FAQ or supporting sections around drain cleaning, leak repair, water heater repair, emergency plumbing, and service areas.",
        "Add focused FAQ or supporting sections around cesspool cleaning, cesspool pumping, septic services, sewer and drain cleaning, emergency service, and service areas."
    )

    return html


# === CESSPOOL FINAL RENDER MIDDLEWARE ===
# Final HTML guard: if rendered report is cesspool/septic, replace generic plumbing leakage.

@app.middleware("http")
async def vast_cesspool_final_render_middleware(request, call_next):
    import re
    from fastapi.responses import Response

    response = await call_next(request)

    path = request.url.path or ""
    if path != "/analyze" and not path.startswith("/history/rerun") and not path.startswith("/reports/saved"):
        return response

    content_type = response.headers.get("content-type", "")
    if "text/html" not in content_type:
        return response

    body_chunks = []
    async for chunk in response.body_iterator:
        body_chunks.append(chunk)

    body = b"".join(body_chunks)
    html = body.decode("utf-8", errors="replace")
    lower = html.lower()

    is_cesspool = any(x in lower for x in [
        "cesspool",
        "septic",
        "jwcesspool.com",
        "qualitycesspool.com",
        "sewer and drain",
        "sewer & drain",
    ])

    if not is_cesspool:
        headers = dict(response.headers)
        headers.pop("content-length", None)
        return Response(content=body, status_code=response.status_code, headers=headers, media_type="text/html")

    if "selden" in lower or "suffolk" in lower:
        market = "Suffolk County"
    elif "long island" in lower:
        market = "Long Island"
    else:
        market = "Long Island"

    primary = f"Cesspool Services {market}"

    service_terms = [
        "Cesspool Cleaning",
        "Cesspool Pumping",
        "Cesspool Repair",
        "Septic Services",
        "Septic Tank Cleaning",
        "Sewer and Drain Cleaning",
        "Emergency Cesspool Service",
        "Cesspool Installation",
    ]

    location_terms = [
        f"Cesspool Services {market}",
        f"Cesspool Cleaning {market}",
        f"Cesspool Pumping {market}",
        f"Septic Services {market}",
        f"Emergency Cesspool Service {market}",
        f"Sewer and Drain Cleaning {market}",
    ]

    buyer_terms = [
        "Free Cesspool Estimate",
        "Emergency Cesspool Service",
        "Licensed Cesspool Company",
        "Local Septic Contractor",
        "Same-Day Cesspool Service",
    ]

    secondary_terms = [
        f"Cesspool Cleaning {market}",
        f"Cesspool Pumping {market}",
        f"Septic Services {market}",
        f"Sewer and Drain Cleaning {market}",
        "Emergency Cesspool Service",
        "Cesspool Repair",
    ]

    buyer_target_terms = [
        "Free Cesspool Estimate",
        "Emergency Cesspool Service",
        "Licensed Cesspool Company",
    ]

    def chips(terms):
        return "".join('<span class="chip chip-medium">⭐ ' + term + '</span>' for term in terms)

    def lis(terms):
        return "".join("<li>" + term + "</li>" for term in terms)

    service_block = (
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Service Terms to Strengthen</h3>'
        '<p class="signals-intro">Use these cesspool and septic-specific terms to clarify the page’s main services and support stronger topical relevance.</p>'
        '<div class="chip-wrap">' + chips(service_terms) + '</div>'
        '</div>'
    )

    location_block = (
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Location Terms to Strengthen</h3>'
        '<p class="signals-intro">Use these local cesspool and septic terms for service-area copy, internal links, FAQs, and dedicated location sections.</p>'
        '<div class="chip-wrap">' + chips(location_terms) + '</div>'
        '</div>'
    )

    buyer_block = (
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Buyer-Intent Terms to Strengthen</h3>'
        '<p class="signals-intro">Use these buyer-intent terms to improve calls to action, estimate pages, and lead-focused copy.</p>'
        '<div class="chip-wrap">' + chips(buyer_terms) + '</div>'
        '</div>'
    )

    html = re.sub(
        r'<div class="keyword-group">\s*<h3 class="keyword-group-title">Service Terms to Strengthen</h3>.*?</div>\s*</div>',
        service_block,
        html,
        count=1,
        flags=re.DOTALL,
    )

    html = re.sub(
        r'<div class="keyword-group">\s*<h3 class="keyword-group-title">Location Terms to Strengthen</h3>.*?</div>\s*</div>',
        location_block,
        html,
        count=1,
        flags=re.DOTALL,
    )

    html = re.sub(
        r'<div class="keyword-group">\s*<h3 class="keyword-group-title">Buyer-Intent Terms to Strengthen</h3>.*?</div>\s*</div>',
        buyer_block,
        html,
        count=1,
        flags=re.DOTALL,
    )

    quickwins_html = (
        f'<li>Strengthen the H1 and opening copy with a natural phrase such as “{primary}.”</li>'
        '<li>Add supporting sections for cesspool cleaning, cesspool pumping, septic services, sewer and drain cleaning, and emergency service.</li>'
        '<li>Add image alt text using real cesspool, septic, sewer, drain, truck, equipment, and service-area descriptions.</li>'
        '<li>Use licensed, emergency, local, and same-day language only where it accurately reflects the business.</li>'
    )

    new_priority = (
        '<div class="section target-quickwins-section">'
        '<h2>Priority Recommendations</h2>'
        '<div class="target-quickwins-grid">'
        '<div class="target-panel">'
        '<h3 class="keyword-group-title">Recommended Target Keywords</h3>'
        '<div class="keyword-box clean-priority-box">'
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Primary Target</h3>'
        '<div class="chip-wrap"><span class="chip chip-high top-chip">🔥 ' + primary + '</span></div>'
        '</div>'
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Secondary Targets</h3>'
        '<ul class="quick-wins-list">' + lis(secondary_terms) + '</ul>'
        '</div>'
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Buyer-Intent Targets</h3>'
        '<ul class="quick-wins-list">' + lis(buyer_target_terms) + '</ul>'
        '</div>'
        '</div>'
        '</div>'
        '<div class="quickwins-panel">'
        '<h3 class="keyword-group-title">Quick Wins</h3>'
        '<div class="site-card clean-priority-card">'
        '<ul class="quick-wins-list">' + quickwins_html + '</ul>'
        '</div>'
        '</div>'
        '</div>'
        '</div>'
    )

    html = re.sub(
        r'<div class="section target-quickwins-section">.*?(?=<div class="section">\s*<h2>Technical SEO Snapshot</h2>)',
        new_priority,
        html,
        count=1,
        flags=re.DOTALL,
    )

    replacements = {
        "Use these plumbing-specific terms": "Use these cesspool and septic-specific terms",
        "Repairs Licensed Local": primary,
        "repairs licensed local": primary,
        "Professional Sewer Drain": primary,
        "professional sewer drain": primary,
        "Cesspool Professional Sewer": primary,
        "cesspool professional sewer": primary,
        "Professional Sewer": "Sewer and Drain Cleaning",
        "professional sewer": "sewer and drain cleaning",
        "Emergency Plumbing": "Emergency Cesspool Service",
        "Residential Plumbing": "Residential Cesspool Services",
        "Commercial Plumbing": "Commercial Cesspool Services",
        "Water Heater Repair": "Septic Tank Cleaning",
        "Leak Repair": "Cesspool Repair",
        "Pipe Repair": "Cesspool Repair",
        "Plumbing Services Long Island": "Cesspool Services Long Island",
        "Emergency Plumber Long Island": "Emergency Cesspool Service Long Island",
        "Free Plumbing Estimate": "Free Cesspool Estimate",
        "Emergency Plumbing Service": "Emergency Cesspool Service",
        "Same-Day Plumber": "Same-Day Cesspool Service",
        "Licensed Plumbing Contractor": "Licensed Cesspool Company",
        "Trusted Emergency Plumber": "Emergency Cesspool Service",
    }

    for old, new in replacements.items():
        html = html.replace(old, new)

    html = html.replace(
        "Add focused FAQ or supporting sections around drain cleaning, Cesspool Repair, Septic Tank Cleaning, Emergency Cesspool Service, and service areas.",
        "Add focused FAQ or supporting sections around cesspool cleaning, cesspool pumping, septic services, sewer and drain cleaning, emergency service, and service areas."
    )

    html = html.replace(
        "Add focused FAQ or supporting sections around drain cleaning, leak repair, water heater repair, emergency plumbing, and service areas.",
        "Add focused FAQ or supporting sections around cesspool cleaning, cesspool pumping, septic services, sewer and drain cleaning, emergency service, and service areas."
    )

    html = html.replace(
        "Use the keyword strategy section to strengthen service, location, and buyer-intent language on the client page.",
        "Use the keyword strategy section to strengthen cesspool, septic, sewer/drain, location, and buyer-intent language on the client page."
    )

    headers = dict(response.headers)
    headers.pop("content-length", None)
    return Response(content=html.encode("utf-8"), status_code=response.status_code, headers=headers, media_type="text/html")


# === HISTORY PAGE GAP POLISH MIDDLEWARE ===
# Cleans raw/junky history Top Gaps chips for known industries without changing saved report files.

@app.middleware("http")
async def vast_history_gap_polish_middleware(request, call_next):
    from fastapi.responses import Response

    response = await call_next(request)

    path = request.url.path or ""
    if path != "/history":
        return response

    content_type = response.headers.get("content-type", "")
    if "text/html" not in content_type:
        return response

    body_chunks = []
    async for chunk in response.body_iterator:
        body_chunks.append(chunk)

    body = b"".join(body_chunks)
    html = body.decode("utf-8", errors="replace")
    lower = html.lower()

    if "qualitycesspool.com" in lower or "jwcesspool.com" in lower:
        replacements = {
            "Cesspool Professional Sewer": "Cesspool Services Long Island",
            "Professional Sewer Drain": "Sewer and Drain Cleaning",
            "Professional Sewer": "Cesspool Cleaning",
            "Trap Repair": "Cesspool Repair",
            "Drain Pump": "Cesspool Pumping",
            "Repair Sewer": "Sewer Repair",
            "Commercial Drainage": "Commercial Cesspool Services",
            "Main Sewer": "Sewer and Drain Cleaning",
            "Plumbing Services Long Island": "Cesspool Services Long Island",
            "Emergency Plumber Long Island": "Emergency Cesspool Service Long Island",
            "Free Plumbing Estimate": "Free Cesspool Estimate",
            "Emergency Plumbing Service": "Emergency Cesspool Service",
            "Licensed Plumbing Contractor": "Licensed Cesspool Company",
            "Water Heater Repair": "Septic Tank Cleaning",
            "Emergency Plumbing": "Emergency Cesspool Service",
            "Residential Plumbing": "Residential Cesspool Services",
            "Commercial Plumbing": "Commercial Cesspool Services",
        }

        for old, new in replacements.items():
            html = html.replace(old, new)

    if "longislandroofing.com" in lower or "liroofing.com" in lower or "roofing" in lower:
        replacements = {
            "Drain Cleaning": "Roof Repair",
            "Leak Repair": "Roof Leak Repair",
            "Water Heater Repair": "Roof Replacement",
            "Emergency Plumbing": "Emergency Roof Repair",
            "Residential Plumbing": "Residential Roofing",
            "Commercial Plumbing": "Commercial Roofing",
            "Plumbing Services Long Island": "Roofing Contractor Long Island",
            "Emergency Plumber Long Island": "Emergency Roof Repair Long Island",
            "Free Plumbing Estimate": "Free Roofing Estimate",
            "Licensed Plumbing Contractor": "Licensed Roofing Contractor",
        }

        for old, new in replacements.items():
            html = html.replace(old, new)

    cleanup_script = """
<script>
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll(".chip, span").forEach(function (chip) {
    if (!chip.textContent) return;
    chip.textContent = chip.textContent.replace(/\\s+/g, " ").trim();
  });

  document.querySelectorAll(".history-card, .report-card, .card, li, article, section").forEach(function (card) {
    const chips = Array.from(card.querySelectorAll(".chip, .gap-chip, span"));
    const seen = new Set();

    chips.forEach(function (chip) {
      const text = (chip.textContent || "").trim().toLowerCase();
      if (!text) return;

      const looksLikeGap =
        text.includes("cesspool") ||
        text.includes("septic") ||
        text.includes("sewer") ||
        text.includes("drain") ||
        text.includes("roof") ||
        text.includes("plumbing") ||
        text.includes("estimate") ||
        text.includes("repair");

      if (!looksLikeGap) return;

      if (seen.has(text)) {
        chip.style.display = "none";
      } else {
        seen.add(text);
      }
    });
  });
});


</script>
"""

    if "vast_history_gap_polish_marker" not in html:
        html = html.replace("</body>", "<!-- vast_history_gap_polish_marker -->" + cleanup_script + "\n</body>", 1)

    headers = dict(response.headers)
    headers.pop("content-length", None)
    return Response(content=html.encode("utf-8"), status_code=response.status_code, headers=headers, media_type="text/html")


# === AGENT KEYWORD PROFILE WIRING ===
# Final agent-powered keyword strategy pass.
# Uses Industry Agent + Keyword Agent to prevent cross-industry leakage before the browser sees the report.

@app.middleware("http")
async def vast_agent_keyword_profile_middleware(request, call_next):
    import re
    from fastapi.responses import Response

    try:
        from agents.industry_agent import detect_industry
        from agents.market_agent import detect_market
        from agents.keyword_agent import build_keyword_plan
        from agents.report_qa_agent import qa_report_text
    except Exception:
        return await call_next(request)

    response = await call_next(request)

    path = request.url.path or ""
    if path != "/analyze" and not path.startswith("/history/rerun") and not path.startswith("/reports/saved"):
        return response

    content_type = response.headers.get("content-type", "")
    if "text/html" not in content_type:
        return response

    body_chunks = []
    async for chunk in response.body_iterator:
        body_chunks.append(chunk)

    body = b"".join(body_chunks)
    html = body.decode("utf-8", errors="replace")
    lower = html.lower()

    # Only touch actual report pages.
    if "seo competitor report" not in lower and "keyword strategy" not in lower:
        headers = dict(response.headers)
        headers.pop("content-length", None)
        return Response(content=body, status_code=response.status_code, headers=headers, media_type="text/html")

    site_text = html

    industry_result = detect_industry(
        {"text": site_text, "title": site_text, "meta": site_text, "h1": site_text, "url": site_text},
        None,
    )

    industry = industry_result.industry

    # Do not rewrite generic reports yet.
    if industry in {"local_service", "", None}:
        headers = dict(response.headers)
        headers.pop("content-length", None)
        return Response(content=body, status_code=response.status_code, headers=headers, media_type="text/html")

    market_result = detect_market(
        {"text": site_text, "title": site_text, "meta": site_text, "h1": site_text, "url": site_text},
        None,
    )

    market = market_result.market
    plan = build_keyword_plan(industry, market)

    # QA catches leakage. For known industries, rebuild if QA finds issues OR if known old junk appears.
    joined_plan = " ".join(
        [plan.primary]
        + list(plan.service_terms)
        + list(plan.location_terms)
        + list(plan.buyer_terms)
        + list(plan.secondary_targets)
    )

    issues = qa_report_text(industry, html)

    known_junk = any(x in lower for x in [
        "professional sewer drain",
        "cesspool professional sewer",
        "repairs licensed local",
        "leading plumbing experts",
        "plumbing experts shirley",
    ])

    should_rebuild = bool(issues) or known_junk or industry in {"cesspool", "roofing", "plumbing", "painting"}

    if not should_rebuild:
        headers = dict(response.headers)
        headers.pop("content-length", None)
        return Response(content=body, status_code=response.status_code, headers=headers, media_type="text/html")

    def chips(terms):
        return "".join(
            '<span class="chip chip-medium">⭐ ' + str(term) + '</span>'
            for term in terms
        )

    def lis(terms):
        return "".join("<li>" + str(term) + "</li>" for term in terms)

    industry_label = {
        "cesspool": "cesspool and septic",
        "roofing": "roofing",
        "plumbing": "plumbing",
        "painting": "painting",
        "seo": "SEO",
    }.get(industry, "local service")

    service_block = (
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Service Terms to Strengthen</h3>'
        '<p class="signals-intro">Use these ' + industry_label + '-specific terms to clarify the page’s main services and support stronger topical relevance.</p>'
        '<div class="chip-wrap">' + chips(plan.service_terms) + '</div>'
        '</div>'
    )

    location_block = (
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Location Terms to Strengthen</h3>'
        '<p class="signals-intro">Use these market-specific terms for service-area copy, internal links, FAQs, and dedicated location sections.</p>'
        '<div class="chip-wrap">' + chips(plan.location_terms) + '</div>'
        '</div>'
    )

    buyer_block = (
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Buyer-Intent Terms to Strengthen</h3>'
        '<p class="signals-intro">Use these buyer-intent terms to improve calls to action, estimate pages, and lead-focused copy.</p>'
        '<div class="chip-wrap">' + chips(plan.buyer_terms) + '</div>'
        '</div>'
    )

    html = re.sub(
        r'<div class="keyword-group">\s*<h3 class="keyword-group-title">Service Terms to Strengthen</h3>.*?</div>\s*</div>',
        service_block,
        html,
        count=1,
        flags=re.DOTALL,
    )

    html = re.sub(
        r'<div class="keyword-group">\s*<h3 class="keyword-group-title">Location Terms to Strengthen</h3>.*?</div>\s*</div>',
        location_block,
        html,
        count=1,
        flags=re.DOTALL,
    )

    if "Buyer-Intent Terms to Strengthen" in html:
        html = re.sub(
            r'<div class="keyword-group">\s*<h3 class="keyword-group-title">Buyer-Intent Terms to Strengthen</h3>.*?</div>\s*</div>',
            buyer_block,
            html,
            count=1,
            flags=re.DOTALL,
        )
    else:
        html = html.replace(
            '<div class="keyword-group">\n                    <h3 class="keyword-group-title">Shared Topic Signals',
            buyer_block + '\n<div class="keyword-group">\n                    <h3 class="keyword-group-title">Shared Topic Signals',
            1,
        )

    quickwins_html = lis(plan.quick_wins)

    new_priority = (
        '<div class="section target-quickwins-section">'
        '<h2>Priority Recommendations</h2>'
        '<div class="target-quickwins-grid">'
        '<div class="target-panel">'
        '<h3 class="keyword-group-title">Recommended Target Keywords</h3>'
        '<div class="keyword-box clean-priority-box">'
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Primary Target</h3>'
        '<div class="chip-wrap"><span class="chip chip-high top-chip">🔥 ' + plan.primary + '</span></div>'
        '</div>'
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Secondary Targets</h3>'
        '<ul class="quick-wins-list">' + lis(plan.secondary_targets) + '</ul>'
        '</div>'
        '<div class="keyword-group">'
        '<h3 class="keyword-group-title">Buyer-Intent Targets</h3>'
        '<ul class="quick-wins-list">' + lis(plan.buyer_terms[:3]) + '</ul>'
        '</div>'
        '</div>'
        '</div>'
        '<div class="quickwins-panel">'
        '<h3 class="keyword-group-title">Quick Wins</h3>'
        '<div class="site-card clean-priority-card">'
        '<ul class="quick-wins-list">' + quickwins_html + '</ul>'
        '</div>'
        '</div>'
        '</div>'
        '</div>'
    )

    html = re.sub(
        r'<div class="section target-quickwins-section">.*?(?=<div class="section">\s*<h2>Technical SEO Snapshot</h2>)',
        new_priority,
        html,
        count=1,
        flags=re.DOTALL,
    )

    # Clean the analysis section language so it points to the profile target instead of old extracted junk.
    junk_replacements = {
        "Professional Sewer Drain": plan.primary,
        "professional sewer drain": plan.primary,
        "Cesspool Professional Sewer": plan.primary,
        "cesspool professional sewer": plan.primary,
        "Repairs Licensed Local": plan.primary,
        "repairs licensed local": plan.primary,
        "Leading Plumbing Experts": plan.primary,
        "leading plumbing experts": plan.primary,
        "Plumbing Experts Shirley": plan.primary,
        "plumbing experts shirley": plan.primary,
    }

    for old, new in junk_replacements.items():
        html = html.replace(old, new)

    # Industry-specific leftover cleanup.
    if industry == "cesspool":
        html = html.replace(
            "Add focused FAQ or supporting sections around drain cleaning, leak repair, water heater repair, emergency plumbing, and service areas.",
            "Add focused FAQ or supporting sections around cesspool cleaning, cesspool pumping, septic services, sewer and drain cleaning, emergency service, and service areas.",
        )
        html = html.replace(
            "Use the keyword strategy section to strengthen service, location, and buyer-intent language on the client page.",
            "Use the keyword strategy section to strengthen cesspool, septic, sewer/drain, location, and buyer-intent language on the client page.",
        )

    if industry == "roofing":
        html = html.replace(
            "Add focused FAQ or supporting sections around drain cleaning, leak repair, water heater repair, emergency plumbing, and service areas.",
            "Add focused FAQ or supporting sections around roof repair, roof replacement, emergency roofing, residential roofing, commercial roofing, and Long Island service areas.",
        )

    # Add a hidden marker so we know the agent pass ran.
    if "vast_agent_keyword_profile_marker" not in html:
        html = html.replace(
            "</body>",
            f"<!-- vast_agent_keyword_profile_marker industry={industry} market={market} industry_confidence={industry_result.confidence:.2f} market_confidence={market_result.confidence:.2f} market_reason={market_result.reason} -->\n</body>",
            1,
        )

    headers = dict(response.headers)
    headers.pop("content-length", None)
    return Response(content=html.encode("utf-8"), status_code=response.status_code, headers=headers, media_type="text/html")



# === LEADBOT TOOL ROUTE COMPAT IMPORTS START ===
from fastapi import Request, Form, Query
from urllib.parse import quote, unquote
# === LEADBOT TOOL ROUTE COMPAT IMPORTS END ===


# === DIRECT AUTH COMPAT IMPORTS START ===
from fastapi import Form as AuthForm, Query as AuthQuery, Request as AuthRequest
from fastapi.responses import HTMLResponse as AuthHTMLResponse
from fastapi.responses import RedirectResponse as AuthRedirectResponse
# === DIRECT AUTH COMPAT IMPORTS END ===


# === AUTH COOKIE COMPAT START ===
AUTH_COOKIE_NAME = "vast_session"
# === AUTH COOKIE COMPAT END ===





# === AUTH CURRENT USER COMPAT START ===
def auth_current_user(request):
    from agents.auth_agent import get_user_from_token

    token = request.cookies.get(AUTH_COOKIE_NAME)
    if not token:
        return None

    return get_user_from_token(token)
# === AUTH CURRENT USER COMPAT END ===


# === PASSWORD TOGGLE SHARED CSS/JS START ===
# Single reusable implementation, interpolated into each auth page below.
_PASSWORD_TOGGLE_STYLE = """
.password-row {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    margin-top: 7px;
}
.password-row input {
    flex: 1 1 auto;
    min-width: 0;
    margin-top: 0;
    height: 46px;
    min-height: 46px;
    padding-top: 0;
    padding-bottom: 0;
    box-sizing: border-box;
}
.password-toggle {
    flex: 0 0 auto;
    width: 72px;
    height: 46px;
    min-height: 46px;
    padding: 0 12px;
    border-radius: 12px;
    border: 1px solid #cbd5e1;
    background: #eff6ff;
    color: #1e3a8a;
    font-weight: 850;
    font-size: 13px;
    cursor: pointer;
    box-sizing: border-box;
    display: inline-flex;
    align-items: center;
    justify-content: center;
}
.password-toggle:hover {
    background: #dbeafe;
}
.password-toggle:focus-visible {
    outline: 2px solid #1e3a8a;
    outline-offset: 2px;
}
@media (max-width: 420px) {
    .password-toggle {
        width: 64px;
        padding: 0 8px;
        font-size: 12px;
    }
}
"""

_PASSWORD_TOGGLE_SCRIPT = """
<script>
document.addEventListener("click", function (event) {
    const button = event.target.closest("[data-password-toggle]");
    if (!button) return;

    const input = document.getElementById(button.dataset.passwordToggle);
    if (!input) return;

    const shouldShow = input.type === "password";
    input.type = shouldShow ? "text" : "password";
    button.textContent = shouldShow ? "Hide" : "Show";
    button.setAttribute("aria-label", shouldShow ? "Hide password" : "Show password");
});
</script>
"""
# === PASSWORD TOGGLE SHARED CSS/JS END ===


def auth_login_page(error="", message=""):
    error_html = f'<div class="auth-error">{error}</div>' if error else ""
    message_html = f'<div class="auth-success">{message}</div>' if message else ""

    return AuthHTMLResponse(f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Login | LeadMeLeads</title>
<style>
body {{
    margin: 0;
    min-height: 100vh;
    display: grid;
    place-items: center;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
    background: linear-gradient(135deg, #07152f, #0f172a, #1e3a8a, #4c1d95);
}}
.auth-card {{
    width: min(420px, calc(100vw - 32px));
    background: white;
    color: #0f172a;
    border-radius: 20px;
    padding: 28px;
    box-shadow: 0 24px 70px rgba(0,0,0,.32);
    box-sizing: border-box;
}}
h1 {{
    margin: 0 0 8px;
    font-size: 28px;
}}
p {{
    margin: 0 0 18px;
    color: #64748b;
}}
label {{
    display: block;
    margin-top: 14px;
    font-weight: 900;
    color: #334155;
}}
input {{
    width: 100%;
    margin-top: 7px;
    padding: 13px 14px;
    border-radius: 12px;
    border: 1px solid #cbd5e1;
    font-size: 16px;
    box-sizing: border-box;
}}
button {{
    width: 100%;
    margin-top: 20px;
    padding: 14px 16px;
    border: 0;
    border-radius: 12px;
    background: linear-gradient(135deg, #0f172a, #1e3a8a);
    color: white;
    font-weight: 950;
    cursor: pointer;
    box-sizing: border-box;
}}
.auth-error {{
    background: #fee2e2;
    color: #991b1b;
    border: 1px solid #fecaca;
    border-radius: 12px;
    padding: 11px 13px;
    margin-bottom: 14px;
    font-weight: 800;
}}
.auth-success {{
    background: #dcfce7;
    color: #166534;
    border: 1px solid #bbf7d0;
    border-radius: 12px;
    padding: 11px 13px;
    margin-bottom: 14px;
    font-weight: 800;
}}
.auth-links {{
    margin-top: 16px;
    text-align: center;
}}
.auth-links a {{
    color: #1e3a8a;
    font-weight: 800;
    text-decoration: none;
}}
{_PASSWORD_TOGGLE_STYLE}
</style>
</head>
<body>
    <form class="auth-card" method="post" action="/login">

<div style="text-align:center; margin:0 0 16px;">
    <a href="/" style="display:inline-block; text-decoration:none;">
        <img
            src="/static/leadmeleads-logo-auth-white.png?v=auth-white-1"
            alt="LeadMeLeads"
            style="display:block; width:min(280px, 100%); max-width:280px; height:auto; margin:0 auto;"
        >
    </a>
    <p style="margin:8px 0 0; color:#64748b; font-size:14px; line-height:1.4; font-weight:600;">
        Find local leads worth contacting.
    </p>
</div>

<h1>LeadMeLeads Login</h1>
        <p>Sign in to access protected tools.</p>
        {error_html}
        {message_html}
        <label>Username or Email</label>
        <input name="username" autocomplete="username" maxlength="254" required>
        <label>Password</label>
        <div class="password-row">
            <input id="login-password" name="password" type="password" autocomplete="current-password" maxlength="256" required>
            <button type="button" class="password-toggle" data-password-toggle="login-password" aria-label="Show password">Show</button>
        </div>
        <button type="submit">Log In</button>
        <div class="auth-links">
            <a href="/">Back to Home</a> &nbsp;|&nbsp;
            <a href="/forgot-password">Forgot password?</a> &nbsp;|&nbsp;
            <a href="/create-account">Create Account</a>
        </div>
    </form>
{_PASSWORD_TOGGLE_SCRIPT}
</body>
</html>
""")


@app.get("/login", response_class=AuthHTMLResponse)
def auth_login_get(reset: str = AuthQuery("")):
    if str(reset).strip() == "1":
        return auth_login_page(message="Password reset successfully. You can now log in.")
    return auth_login_page()


@app.post("/login")
def auth_login_post(request: AuthRequest, username: str = AuthForm(...), password: str = AuthForm(...)):
    from agents.auth_agent import (
        authenticate_user,
        create_session,
        cookie_secure_enabled,
        login_clear_failures,
        login_is_limited,
        login_record_failure,
    )

    username = str(username or "").strip()[:254]
    password = str(password or "")[:256]

    client_host = ""
    try:
        client_host = request.client.host if request.client else ""
    except Exception:
        client_host = ""

    if login_is_limited(client_host, username):
        return auth_login_page("Too many login attempts. Try again in a few minutes.")

    user = authenticate_user(username, password)

    if not user:
        login_record_failure(client_host, username)
        return auth_login_page("Invalid username or password.")

    login_clear_failures(client_host, username)

    token = create_session(user)

    response = AuthRedirectResponse(url="/", status_code=303)
    response.set_cookie(
        AUTH_COOKIE_NAME,
        token,
        httponly=True,
        secure=cookie_secure_enabled(),
        samesite="lax",
        max_age=60 * 60 * 24 * 7,
        path="/",
    )

    return response


@app.get("/logout")
def auth_logout(request: AuthRequest):
    from agents.auth_agent import delete_session

    delete_session(request.cookies.get(AUTH_COOKIE_NAME))

    response = AuthRedirectResponse(url="/login", status_code=303)
    response.delete_cookie(AUTH_COOKIE_NAME, path="/")
    return response



# === SIGNUP ROUTES START ===
def signup_page(error="", username="", email=""):
    import html as signup_html

    error_html = f'<div class="auth-error">{signup_html.escape(error)}</div>' if error else ""
    username_value = signup_html.escape(username or "")
    email_value = signup_html.escape(email or "")

    return AuthHTMLResponse(f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Create Account | LeadMeLeads</title>
<style>
body {{
    margin: 0;
    min-height: 100vh;
    display: grid;
    place-items: center;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
    background: linear-gradient(135deg, #07152f, #0f172a, #1e3a8a, #4c1d95);
}}
.auth-card {{
    width: min(460px, calc(100vw - 32px));
    background: white;
    color: #0f172a;
    border-radius: 20px;
    padding: 28px;
    box-shadow: 0 24px 70px rgba(0,0,0,.32);
    box-sizing: border-box;
}}
h1 {{
    margin: 0 0 8px;
    font-size: 28px;
}}
p {{
    margin: 0 0 18px;
    color: #64748b;
}}
label {{
    display: block;
    margin-top: 14px;
    font-weight: 900;
    color: #334155;
}}
input {{
    width: 100%;
    margin-top: 7px;
    padding: 13px 14px;
    border-radius: 12px;
    border: 1px solid #cbd5e1;
    font-size: 16px;
    box-sizing: border-box;
}}
button {{
    width: 100%;
    margin-top: 20px;
    padding: 14px 16px;
    border: 0;
    border-radius: 12px;
    background: linear-gradient(135deg, #0f172a, #1e3a8a);
    color: white;
    font-weight: 950;
    cursor: pointer;
    box-sizing: border-box;
}}
.auth-error {{
    background: #fee2e2;
    color: #991b1b;
    border: 1px solid #fecaca;
    border-radius: 12px;
    padding: 11px 13px;
    margin-bottom: 14px;
    font-weight: 800;
}}
.auth-links {{
    margin-top: 16px;
    text-align: center;
}}
.auth-links a {{
    color: #1e3a8a;
    font-weight: 800;
    text-decoration: none;
}}
.small-note {{
    margin-top: 10px;
    color: #64748b;
    font-size: 13px;
    line-height: 1.45;
}}
{_PASSWORD_TOGGLE_STYLE}
</style>
</head>
<body>
    <form class="auth-card" method="post" action="/signup">

<div style="text-align:center; margin:0 0 16px;">
    <a href="/" style="display:inline-block; text-decoration:none;">
        <img
            src="/static/leadmeleads-logo-auth-white.png?v=auth-white-1"
            alt="LeadMeLeads"
            style="display:block; width:min(280px, 100%); max-width:280px; height:auto; margin:0 auto;"
        >
    </a>
    <p style="margin:8px 0 0; color:#64748b; font-size:14px; line-height:1.4; font-weight:600;">
        Find local leads worth contacting.
    </p>
</div>

<h1>Create Account</h1>
        <p>Create a standard LeadMeLeads account.</p>
        {error_html}

        <label>Username</label>
        <input name="username" type="text" autocomplete="username" value="{username_value}" maxlength="150" required>

        <label>Email</label>
        <input name="email" type="email" autocomplete="email" value="{email_value}" maxlength="254" required>

        <label>Password</label>
        <div class="password-row">
            <input id="signup-password" name="password" type="password" autocomplete="new-password" required>
            <button type="button" class="password-toggle" data-password-toggle="signup-password" aria-label="Show password">Show</button>
        </div>

        <label>Confirm Password</label>
        <div class="password-row">
            <input id="signup-confirm-password" name="confirm_password" type="password" autocomplete="new-password" required>
            <button type="button" class="password-toggle" data-password-toggle="signup-confirm-password" aria-label="Show password">Show</button>
        </div>

        <div class="small-note">Password must be at least 12 characters.</div>

        <button type="submit">Create Account</button>

        <div class="auth-links">
            <a href="/login">Log In</a> &nbsp;|&nbsp; <a href="/">Back to Home</a>
        </div>
    </form>
{_PASSWORD_TOGGLE_SCRIPT}
</body>
</html>
""")


@app.get("/signup", response_class=AuthHTMLResponse)
def signup_get():
    return signup_page()


@app.get("/create-account", response_class=AuthHTMLResponse)
def create_account_get():
    return signup_page()


@app.post("/signup")
def signup_post(
    username: str = AuthForm(...),
    email: str = AuthForm(...),
    password: str = AuthForm(...),
    confirm_password: str = AuthForm(...),
):
    from agents.auth_agent import create_user, user_exists, email_exists

    clean_username = str(username or "").strip().lower()
    clean_email = str(email or "").strip().lower()

    if not clean_username:
        return signup_page("Username is required.", username=clean_username, email=clean_email)

    if "@" not in clean_email or "." not in clean_email:
        return signup_page("Use a valid email address.", username=clean_username, email=clean_email)

    if password != confirm_password:
        return signup_page("Passwords do not match.", username=clean_username, email=clean_email)

    if len(password or "") < 12:
        return signup_page("Password must be at least 12 characters.", username=clean_username, email=clean_email)

    if user_exists(clean_username):
        return signup_page("That username is already taken.", username=clean_username, email=clean_email)

    if email_exists(clean_email):
        return signup_page("An account with that email already exists. Log in instead.", username=clean_username, email=clean_email)

    try:
        create_user(clean_username, password, role="standard", email=clean_email)
    except Exception as exc:
        return signup_page(f"Could not create account: {str(exc)}", username=clean_username, email=clean_email)

    return AuthRedirectResponse(url="/login", status_code=303)


@app.post("/create-account")
def create_account_post(
    username: str = AuthForm(...),
    email: str = AuthForm(...),
    password: str = AuthForm(...),
    confirm_password: str = AuthForm(...),
):
    return signup_post(username=username, email=email, password=password, confirm_password=confirm_password)
# === SIGNUP ROUTES END ===


# === PASSWORD RESET ROUTES START ===
_SMTP_PLACEHOLDERS = frozenset({
    "smtp.yourmailprovider.com",
    "smtp.example.com",
    "your-smtp-host",
    "mail.example.com",
})

_BASE_URL_PLACEHOLDERS = frozenset({
    "https://yourdomain.com",
    "http://yourdomain.com",
    "https://example.com",
    "http://example.com",
})


def _is_dev_mode():
    """Dev mode when SMTP_HOST is blank or a known placeholder."""
    host = os.getenv("SMTP_HOST", "").strip().lower()
    return not host or host in _SMTP_PLACEHOLDERS


def _show_dev_reset_link():
    """Return True only when SHOW_DEV_RESET_LINK=1 is explicitly set.
    Must never be True in production. Controls both on-page link and console output."""
    return os.getenv("SHOW_DEV_RESET_LINK", "").strip() == "1"


def _get_base_url(request):
    """Return base URL for reset links, ignoring placeholder env values."""
    configured = os.getenv("APP_BASE_URL", "").rstrip("/")
    if configured and configured not in _BASE_URL_PLACEHOLDERS:
        return configured
    try:
        derived = str(request.base_url).rstrip("/")
        if derived:
            return derived
    except Exception:
        pass
    return "http://127.0.0.1:8000"


def _send_reset_email(to_email, reset_url):
    import smtplib
    from email.mime.text import MIMEText
    from datetime import timezone

    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "587").strip() or "587")
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
    from_email = os.getenv("SMTP_FROM", smtp_user).strip()

    requested_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    body = (
        f"You requested a password reset for your LeadMeLeads account.\n\n"
        f"Click the link below to set a new password:\n\n{reset_url}\n\n"
        f"This link expires in 60 minutes. If you did not request a reset, ignore this email.\n\n"
        f"Requested at: {requested_at}"
    )

    msg = MIMEText(body, "plain")
    msg["Subject"] = f"Reset your LeadMeLeads password — {requested_at}"
    msg["From"] = from_email
    msg["To"] = to_email

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        if smtp_user and smtp_password:
            server.login(smtp_user, smtp_password)
        server.sendmail(from_email, to_email, msg.as_string())


def forgot_password_page(message="", error="", dev_link=""):
    import html as _html

    error_html = f'<div class="auth-error">{_html.escape(error)}</div>' if error else ""
    message_html = f'<div class="auth-success">{_html.escape(message)}</div>' if message else ""
    dev_link_html = ""
    if dev_link:
        safe_link = _html.escape(dev_link)
        dev_link_html = (
            f'<div class="dev-link-box"><strong>Dev mode &mdash; reset link:</strong><br>'
            f'<a href="{safe_link}" style="word-break:break-all;">{safe_link}</a></div>'
        )

    return AuthHTMLResponse(f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Forgot Password | LeadMeLeads</title>
<style>
body {{
    margin: 0; min-height: 100vh; display: grid; place-items: center;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
    background: linear-gradient(135deg, #07152f, #0f172a, #1e3a8a, #4c1d95);
}}
.auth-card {{
    width: min(420px, calc(100vw - 32px)); background: white; color: #0f172a;
    border-radius: 20px; padding: 28px; box-shadow: 0 24px 70px rgba(0,0,0,.32);
    box-sizing: border-box;
}}
h1 {{ margin: 0 0 8px; font-size: 28px; }}
p {{ margin: 0 0 18px; color: #64748b; }}
label {{ display: block; margin-top: 14px; font-weight: 900; color: #334155; }}
input {{
    width: 100%; margin-top: 7px; padding: 13px 14px; border-radius: 12px;
    border: 1px solid #cbd5e1; font-size: 16px; box-sizing: border-box;
}}
button {{
    width: 100%; margin-top: 20px; padding: 14px 16px; border: 0;
    border-radius: 12px; background: linear-gradient(135deg, #0f172a, #1e3a8a);
    color: white; font-weight: 950; cursor: pointer;
    box-sizing: border-box;
}}
.auth-error {{
    background: #fee2e2; color: #991b1b; border: 1px solid #fecaca;
    border-radius: 12px; padding: 11px 13px; margin-bottom: 14px; font-weight: 800;
}}
.auth-success {{
    background: #dcfce7; color: #166534; border: 1px solid #bbf7d0;
    border-radius: 12px; padding: 11px 13px; margin-bottom: 14px; font-weight: 800;
}}
.auth-links {{ margin-top: 16px; text-align: center; }}
.auth-links a {{ color: #1e3a8a; font-weight: 800; text-decoration: none; }}
.dev-link-box {{
    margin-top: 18px; padding: 12px 14px; background: #fef9c3;
    border: 1px solid #fde047; border-radius: 12px; font-size: 13px; color: #713f12;
}}
</style>
</head>
<body>
    <form class="auth-card" method="post" action="/forgot-password">
        
<div style="text-align:center; margin:0 0 16px;">
    <a href="/" style="display:inline-block; text-decoration:none;">
        <img
            src="/static/leadmeleads-logo-auth-white.png?v=auth-white-1"
            alt="LeadMeLeads"
            style="display:block; width:min(280px, 100%); max-width:280px; height:auto; margin:0 auto;"
        >
    </a>
    <p style="margin:8px 0 0; color:#64748b; font-size:14px; line-height:1.4; font-weight:600;">
        Find local leads worth contacting.
    </p>
</div>

<h1>Forgot Password</h1>
        <p>Enter your username or email and we'll send you a reset link.</p>
        {error_html}
        {message_html}
        {dev_link_html}
        <label>Username or Email</label>
        <input name="identifier" type="text" autocomplete="username" maxlength="254" required>
        <button type="submit">Send Reset Link</button>
        <div class="auth-links"><a href="/login">Back to Login</a></div>
    </form>
</body>
</html>
""")


@app.get("/forgot-password", response_class=AuthHTMLResponse)
def forgot_password_get():
    return forgot_password_page()


@app.post("/forgot-password")
def forgot_password_post(request: AuthRequest, identifier: str = AuthForm(...)):
    from agents.auth_agent import create_reset_token

    clean_identifier = str(identifier or "").strip().lower()[:254]
    base_url = _get_base_url(request)

    GENERIC_MSG = "If an account exists for that username or email, a reset link has been sent."

    raw_token, user = create_reset_token(clean_identifier)

    if _show_dev_reset_link():
        if user:
            print(f"\n[DEV FORGOT] User found — user_id={user['id']} username={user['username']} email={user.get('email')!r}", flush=True)
            print(f"[DEV FORGOT] Reset token created: yes", flush=True)
        else:
            print(f"\n[DEV FORGOT] No user found for identifier: {clean_identifier!r}", flush=True)

    if not user:
        return forgot_password_page(message=GENERIC_MSG)

    reset_url = f"{base_url}/reset-password?token={raw_token}"

    if _show_dev_reset_link():
        print(f"[DEV FORGOT] Reset URL: {reset_url}\n", flush=True)
        return forgot_password_page(message=GENERIC_MSG, dev_link=reset_url)

    if _is_dev_mode():
        # No SMTP configured and dev link not enabled — return generic message, no output.
        return forgot_password_page(message=GENERIC_MSG)

    user_email = user.get("email")
    if not user_email:
        return forgot_password_page(message=GENERIC_MSG)

    try:
        _send_reset_email(user_email, reset_url)
    except Exception as exc:
        print(f"[ERROR] Failed to send reset email to {user_email}: {exc}", flush=True)

    return forgot_password_page(message=GENERIC_MSG)


def reset_password_page(token="", error=""):
    import html as _html

    error_html = f'<div class="auth-error">{_html.escape(error)}</div>' if error else ""
    safe_token = _html.escape(token or "")

    return AuthHTMLResponse(f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Reset Password | LeadMeLeads</title>
<style>
body {{
    margin: 0; min-height: 100vh; display: grid; place-items: center;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
    background: linear-gradient(135deg, #07152f, #0f172a, #1e3a8a, #4c1d95);
}}
.auth-card {{
    width: min(420px, calc(100vw - 32px)); background: white; color: #0f172a;
    border-radius: 20px; padding: 28px; box-shadow: 0 24px 70px rgba(0,0,0,.32);
    box-sizing: border-box;
}}
h1 {{ margin: 0 0 8px; font-size: 28px; }}
p {{ margin: 0 0 18px; color: #64748b; }}
label {{ display: block; margin-top: 14px; font-weight: 900; color: #334155; }}
input {{
    width: 100%; margin-top: 7px; padding: 13px 14px; border-radius: 12px;
    border: 1px solid #cbd5e1; font-size: 16px; box-sizing: border-box;
}}
button {{
    width: 100%; margin-top: 20px; padding: 14px 16px; border: 0;
    border-radius: 12px; background: linear-gradient(135deg, #0f172a, #1e3a8a);
    color: white; font-weight: 950; cursor: pointer;
    box-sizing: border-box;
}}
.auth-error {{
    background: #fee2e2; color: #991b1b; border: 1px solid #fecaca;
    border-radius: 12px; padding: 11px 13px; margin-bottom: 14px; font-weight: 800;
}}
.auth-links {{ margin-top: 16px; text-align: center; }}
.auth-links a {{ color: #1e3a8a; font-weight: 800; text-decoration: none; }}
.small-note {{ margin-top: 10px; color: #64748b; font-size: 13px; }}
{_PASSWORD_TOGGLE_STYLE}
</style>
</head>
<body>
    <form class="auth-card" method="post" action="/reset-password">
        
<div style="text-align:center; margin:0 0 16px;">
    <a href="/" style="display:inline-block; text-decoration:none;">
        <img
            src="/static/leadmeleads-logo-auth-white.png?v=auth-white-1"
            alt="LeadMeLeads"
            style="display:block; width:min(280px, 100%); max-width:280px; height:auto; margin:0 auto;"
        >
    </a>
    <p style="margin:8px 0 0; color:#64748b; font-size:14px; line-height:1.4; font-weight:600;">
        Find local leads worth contacting.
    </p>
</div>

<h1>Reset Password</h1>
        <p>Enter your new password below.</p>
        {error_html}
        <input type="hidden" name="token" value="{safe_token}">
        <label>New Password</label>
        <div class="password-row">
            <input id="reset-password-password" name="password" type="password" autocomplete="new-password" maxlength="256" required>
            <button type="button" class="password-toggle" data-password-toggle="reset-password-password" aria-label="Show password">Show</button>
        </div>
        <label>Confirm New Password</label>
        <div class="password-row">
            <input id="reset-password-confirm" name="confirm_password" type="password" autocomplete="new-password" maxlength="256" required>
            <button type="button" class="password-toggle" data-password-toggle="reset-password-confirm" aria-label="Show password">Show</button>
        </div>
        <div class="small-note">Password must be at least 12 characters.</div>
        <button type="submit">Set New Password</button>
        <div class="auth-links"><a href="/login">Back to Login</a></div>
    </form>
{_PASSWORD_TOGGLE_SCRIPT}
</body>
</html>
""")


def reset_password_invalid_page():
    return AuthHTMLResponse("""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Invalid Reset Link | LeadMeLeads</title>
<style>
body {
    margin: 0; min-height: 100vh; display: grid; place-items: center;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
    background: linear-gradient(135deg, #07152f, #0f172a, #1e3a8a, #4c1d95);
}
.auth-card {
    width: min(420px, calc(100vw - 32px)); background: white; color: #0f172a;
    border-radius: 20px; padding: 28px; box-shadow: 0 24px 70px rgba(0,0,0,.32);
    text-align: center;
    box-sizing: border-box;
}
h1 { margin: 0 0 12px; font-size: 26px; }
p { color: #64748b; margin: 0 0 14px; }
a { color: #1e3a8a; font-weight: 800; text-decoration: none; }
</style>
</head>
<body>
    <div class="auth-card">

<div style="text-align:center; margin:0 0 16px;">
    <a href="/" style="display:inline-block; text-decoration:none;">
        <img
            src="/static/leadmeleads-logo-auth-white.png?v=auth-white-1"
            alt="LeadMeLeads"
            style="display:block; width:min(280px, 100%); max-width:280px; height:auto; margin:0 auto;"
        >
    </a>
    <p style="margin:8px 0 0; color:#64748b; font-size:14px; line-height:1.4; font-weight:600;">
        Find local leads worth contacting.
    </p>
</div>

        <h1>Link Expired or Invalid</h1>
        <p>This reset link has expired or has already been used.</p>
        <p><a href="/forgot-password">Request a new reset link</a></p>
    </div>
</body>
</html>
""")


@app.get("/reset-password", response_class=AuthHTMLResponse)
def reset_password_get(token: str = AuthQuery("")):
    from agents.auth_agent import get_user_for_reset_token

    token = str(token or "").strip()
    if not get_user_for_reset_token(token):
        return reset_password_invalid_page()

    return reset_password_page(token=token)


@app.post("/reset-password")
def reset_password_post(
    token: str = AuthForm(...),
    password: str = AuthForm(...),
    confirm_password: str = AuthForm(...),
):
    from agents.auth_agent import (
        get_user_for_reset_token,
        consume_reset_token,
        set_user_password,
    )

    token = str(token or "").strip()
    password = str(password or "")[:256]
    confirm_password = str(confirm_password or "")[:256]

    user = get_user_for_reset_token(token)
    if not user:
        return reset_password_invalid_page()

    if _show_dev_reset_link():
        print(
            f"\n[DEV RESET] Token valid — user_id={user['id']} username={user['username']}",
            flush=True,
        )

    if password != confirm_password:
        return reset_password_page(token=token, error="Passwords do not match.")

    if len(password) < 12:
        return reset_password_page(token=token, error="Password must be at least 12 characters.")

    try:
        set_user_password(user["id"], password)
    except ValueError as exc:
        if _show_dev_reset_link():
            print(f"[DEV RESET] set_user_password FAILED: {exc}", flush=True)
        return reset_password_page(token=token, error=str(exc))

    if _show_dev_reset_link():
        print(
            f"[DEV RESET] Password updated — user_id={user['id']} username={user['username']}",
            flush=True,
        )

    consume_reset_token(token)

    return AuthRedirectResponse(url="/login?reset=1", status_code=303)
# === PASSWORD RESET ROUTES END ===




# === LEADBOT DATAFORSEO STATUS API START ===
@app.get("/lead-bot/dataforseo-status")
async def leadbot_dataforseo_status(request: AuthRequest):
    user = auth_current_user(request)

    if not user:
        return JSONResponse(
            {"ok": False, "error": "Login required."},
            status_code=401,
        )

    role = _admin_role_from_user(user)
    if role != "admin":
        return JSONResponse(
            {"ok": False, "error": "Admin required."},
            status_code=403,
        )

    return {
        "ok": True,
        "enabled": os.getenv("LEADBOT_DATAFORSEO_ENABLED", "0").strip() == "1",
        "value": os.getenv("LEADBOT_DATAFORSEO_ENABLED", "0").strip(),
    }
# === LEADBOT DATAFORSEO STATUS API END ===

# === LEADBOT DATAFORSEO SIDEBAR TOGGLE API START ===
@app.post("/lead-bot/dataforseo-toggle")
async def leadbot_dataforseo_toggle(request: AuthRequest):
    """
    Toggle LeadBot DataForSEO without redirecting to Settings.

    Security:
    - login required
    - admin role required because this changes runtime/.env behavior
    """
    user = auth_current_user(request)

    if not user:
        return JSONResponse(
            {"ok": False, "error": "Login required."},
            status_code=401,
        )

    role = ""
    if isinstance(user, dict):
        role = str(user.get("role") or "").strip().lower()
    else:
        role = str(getattr(user, "role", "") or "").strip().lower()

    if role != "admin":
        return JSONResponse(
            {"ok": False, "error": "Admin required."},
            status_code=403,
        )

    current = leadbot_get_dataforseo_enabled()
    value = leadbot_set_dataforseo_enabled(not current)
    enabled = str(value).strip() == "1"

    print(
        f"LEADBOT DATAFORSEO SIDEBAR TOGGLE: LEADBOT_DATAFORSEO_ENABLED={value}",
        flush=True,
    )

    return {
        "ok": True,
        "enabled": enabled,
        "value": str(value),
    }
# === LEADBOT DATAFORSEO SIDEBAR TOGGLE API END ===





# === LEADBOT SAFE DELETE ROW ROUTE START ===
@app.api_route("/lead-bot/delete-row-safe", methods=["GET", "POST"])
async def leadbot_delete_row_safe(request: Request):
    """
    Safe LeadBot row delete endpoint.

    Deletes one lead row from one export CSV by normalized domain.
    Returns JSON so the dashboard can remove the card without navigating
    into an Internal Server Error page.
    """
    import csv
    import re
    from pathlib import Path
    from fastapi.responses import JSONResponse

    def clean_domain(value: str) -> str:
        value = str(value or "").strip().lower()
        value = re.sub(r"^https?://", "", value)
        value = re.sub(r"^www\.", "", value)
        value = value.split("/")[0].split("?")[0].split("#")[0]
        value = value.strip(" .,:;()[]{}<>\"'")
        return value

    try:
        params = dict(request.query_params)

        if request.method.upper() == "POST":
            try:
                form = await request.form()
                params.update(dict(form))
            except Exception:
                pass

        filename = str(params.get("filename") or params.get("file") or "").strip()
        domain = clean_domain(params.get("domain") or "")

        if not filename:
            return JSONResponse(
                {"ok": False, "error": "Missing filename."},
                status_code=400,
            )

        if not domain:
            return JSONResponse(
                {"ok": False, "error": "Missing domain."},
                status_code=400,
            )

        # Lock delete to exports/*.csv only.
        safe_name = Path(filename).name
        if safe_name != filename or not safe_name.endswith(".csv"):
            return JSONResponse(
                {"ok": False, "error": "Invalid filename."},
                status_code=400,
            )

        export_path = Path("exports") / safe_name

        if not export_path.exists():
            return JSONResponse(
                {"ok": False, "error": f"Export not found: {safe_name}"},
                status_code=404,
            )

        with export_path.open("r", encoding="utf-8", errors="ignore", newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or []
            rows = list(reader)

        if not fieldnames:
            return JSONResponse(
                {"ok": False, "error": "Export has no header row."},
                status_code=400,
            )

        kept = []
        deleted = 0

        for row in rows:
            row_domain = clean_domain(
                row.get("domain")
                or row.get("Domain")
                or row.get("website")
                or row.get("Website")
                or row.get("url")
                or row.get("URL")
                or ""
            )

            if row_domain == domain:
                deleted += 1
                continue

            kept.append(row)

        if deleted:
            with export_path.open("w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(kept)

        return JSONResponse({
            "ok": True,
            "filename": safe_name,
            "domain": domain,
            "deleted": deleted,
            "remaining": len(kept),
        })

    except Exception as exc:
        print(f"LEADBOT SAFE DELETE ROW ERROR: {exc}", flush=True)
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=500,
        )
# === LEADBOT SAFE DELETE ROW ROUTE END ===


# === LEAD BOT ROUTES ===
from fastapi.responses import HTMLResponse as LeadBotHTMLResponse
from fastapi.responses import RedirectResponse as LeadBotRedirectResponse
from fastapi.responses import FileResponse as LeadBotFileResponse
from agents.lead_dashboard_agent import render_lead_dashboard, safe_export_file, _leadbot_export_visible_to_user

# === LEADBOT EXPORT ACCESS WRAPPER START ===
def leadbot_user_can_access_export(filename, request):
    """
    Main.py compatibility wrapper around the dashboard export visibility helper.
    Used by edit/save routes before touching an export CSV.
    """
    try:
        user = auth_current_user(request)
        if not user:
            return False

        path = safe_export_file(filename)
        if not path:
            return False

        return bool(_leadbot_export_visible_to_user(path, current_user=user))
    except Exception as exc:
        print(f"LEADBOT EXPORT ACCESS CHECK ERROR: {exc}", flush=True)
        return False
# === LEADBOT EXPORT ACCESS WRAPPER END ===

from pathlib import Path as LeadBotPath


@app.get("/lead-bot", response_class=LeadBotHTMLResponse)
def lead_bot_dashboard(request: AuthRequest, file: str = ""):
    user = auth_current_user(request)
    return render_lead_dashboard(file, current_user=user)


@app.get("/lead-bot/run")
def lead_bot_run(
    request: AuthRequest,
    industry: str = "cesspool",
    market: str = "Long Island",
    keyword: str = "cesspool service",
    own_domain: str = "qualitycesspool.com",
    limit: int = 10,
    per_query_limit: int = 4,
    max_queries: int = 4,
):
    import os as lead_bot_os

    # Keep browser-triggered runs small so the GUI does not feel frozen.
    limit = max(1, min(int(limit), 10))
    per_query_limit = max(1, min(int(per_query_limit), 4))
    max_queries = max(1, min(int(max_queries), 4))

    lead_bot_os.environ["LEAD_BOT_QUIET"] = "1"

    from agents.lead_finding_agent import find_leads
    from agents.lead_export_agent import export_leads_to_csv
    from agents.lead_query_agent import build_lead_queries
    from agents.seen_leads_agent import mark_seen

    queries = build_lead_queries(industry, market, keyword)[:max_queries]

    all_leads = []
    seen_domains = set()

    for q in queries:
        try:
            result = find_leads(
                industry=industry,
                market=market,
                service_keyword=q,
                own_domain=own_domain,
                limit=per_query_limit,
            )

            for lead in result.get("leads", []):
                domain = lead.get("domain")
                if domain and domain not in seen_domains:
                    seen_domains.add(domain)
                    all_leads.append(lead)

        except Exception as e:
            print("LEAD BOT GUI QUERY ERROR:", q, e, flush=True)

    usable = []

    for lead in all_leads:
        if lead.get("outreach_status") not in {"email_and_call_ready", "call_ready", "email_ready"}:
            continue
        if not lead.get("best_phone") and not lead.get("emails"):
            continue
        if int(lead.get("contact_confidence") or 0) < 40:
            continue
        usable.append(lead)

    usable = sorted(
        usable,
        key=lambda x: int(x.get("final_lead_score") or x.get("score") or 0),
        reverse=True,
    )[:limit]

    export = export_leads_to_csv(
        {
            "query": " | ".join(queries),
            "industry": industry,
            "market": market,
            "count": len(usable),
            "leads": usable,
        },
        industry=industry,
        market=market,
        only_outreach_ready=True,
    )

    mark_seen(usable)

    filename = LeadBotPath(export.get("path", "")).name

    try:
        leadbot_mark_export_owner(filename, request)
    except Exception as exc:
        print(f"LEADBOT RUN EXPORT OWNER WARN: {exc}", flush=True)

    return LeadBotRedirectResponse(url=f"/lead-bot?file={filename}#results", status_code=303)



# === LEADBOT LIVE ROUTE AUTH COMPAT ALIASES START ===
# These aliases let the transplanted newer LeadBot live routes run inside this cleaner older main.py.
from fastapi import Request as AuthRequest, Form as AuthForm, Query as AuthQuery, Form as AuthForm, Query as AuthQuery
AuthRedirectResponse = LeadBotRedirectResponse
AuthHTMLResponse = LeadBotHTMLResponse
# === LEADBOT LIVE ROUTE AUTH COMPAT ALIASES END ===

# === LEADBOT LIVE JOBS V1 START ===

@app.get("/lead-bot/cards/{filename}", response_class=AuthHTMLResponse)
def leadbot_cards_for_selected_export(filename: str, request: AuthRequest):
    """
    Direct card renderer for a selected LeadBot CSV export.

    Used as a safety fallback when /lead-bot?file=... lands on the dashboard
    but the selected export rows are not handed into the page correctly.
    """
    try:
        user = auth_current_user(request)
    except Exception:
        user = None

    if not user:
        return AuthRedirectResponse(url="/login?next=/lead-bot", status_code=303)

    try:
        from agents.lead_dashboard_agent import safe_export_file, read_csv_rows, lead_cards

        path = safe_export_file(filename)
        if not path:
            return AuthHTMLResponse('<div class="empty">Selected export not found.</div>')

        rows = read_csv_rows(path, limit=100)
        return AuthHTMLResponse(lead_cards(rows, selected_name=path.name))

    except Exception as exc:
        print(f"LEADBOT SELECTED CSV CARD FALLBACK ERROR: {exc}", flush=True)
        return AuthHTMLResponse('<div class="empty">Could not load selected export.</div>')


@app.get("/lead-bot/live-start")
def leadbot_live_start(
    request: AuthRequest,
    industry: str = "",
    market: str = "",
    keyword: str = "",
    own_domain: str = "",
    limit: int = 50,
    per_batch: int = 8,
    per_query_limit: int = 5,
    max_queries: int = 5,
):
    try:
        user = auth_current_user(request)
    except Exception:
        user = None

    if not user:
        return AuthRedirectResponse(url="/login?next=/lead-bot", status_code=303)

    from agents.lead_live_job_agent import create_job

    owner_email = ""
    owner_username = ""
    owner_role = ""

    if isinstance(user, dict):
        owner_email = str(user.get("email") or user.get("username") or "").strip().lower()
        owner_username = str(user.get("username") or user.get("email") or "").strip().lower()
        owner_role = str(user.get("role") or "").strip().lower()
    else:
        owner_email = str(getattr(user, "email", "") or getattr(user, "username", "") or "").strip().lower()
        owner_username = str(getattr(user, "username", "") or getattr(user, "email", "") or "").strip().lower()
        owner_role = str(getattr(user, "role", "") or "").strip().lower()

    job_id = create_job(
        {
            "industry": industry,
            "market": market,
            "keyword": keyword,
            "own_domain": own_domain,
            "limit": limit,
            "per_batch": per_batch,
            "per_query_limit": per_query_limit,
            "max_queries": max_queries,
            "owner_email": owner_email,
            "owner_username": owner_username,
            "owner_role": owner_role,
        }
    )

    return AuthRedirectResponse(url=f"/lead-bot/live/{job_id}", status_code=303)


@app.get("/lead-bot/live-status/{job_id}")
def leadbot_live_status(job_id: str, request: AuthRequest):
    try:
        user = auth_current_user(request)
    except Exception:
        user = None

    if not user:
        return {"status": "auth_required", "message": "Login required."}

    from agents.lead_live_job_agent import read_job

    job = read_job(job_id)

    if not job:
        return {"status": "missing", "message": "Job not found.", "leads": []}

    return job




@app.post("/lead-bot/live-cancel/{job_id}")
def leadbot_live_cancel(job_id: str, request: AuthRequest):
    try:
        user = auth_current_user(request)
    except Exception:
        user = None

    if not user:
        return {"status": "auth_required", "message": "Login required."}

    from agents.lead_live_job_agent import cancel_job

    job = cancel_job(job_id)

    if not job:
        return {"status": "missing", "message": "Job not found.", "job_id": job_id}

    return {
        "status": job.get("status") or "cancelled",
        "message": job.get("message") or "Scan cancelled.",
        "job_id": job_id,
        "counts": job.get("counts") or {},
        "export_file": job.get("export_file") or "",
    }



@app.get("/lead-bot/live-dashboard/{job_id}")
def leadbot_live_dashboard(job_id: str, request: AuthRequest):
    """
    Robust dashboard redirect for Live Scan.

    Normal path:
    - job has export_file
    - redirect to /lead-bot?file=<export>#results

    Recovery path:
    - export_file is missing/empty/bad
    - rebuild a small desktop CSV from job["leads"]
    - redirect to that recovered CSV
    """
    try:
        user = auth_current_user(request)
    except Exception:
        user = None

    if not user:
        return AuthRedirectResponse(url="/login?next=/lead-bot", status_code=303)

    try:
        import csv
        import re as _re
        from pathlib import Path
        from datetime import datetime
        from urllib.parse import quote
        from agents.lead_live_job_agent import read_job

        job = read_job(job_id)

        if not job:
            return AuthRedirectResponse(url="/lead-bot?live_dashboard=missing#exports", status_code=303)

        def _safe_name(value):
            value = str(value or "").strip()
            if not value:
                return ""
            return Path(value).name

        def _export_path(filename):
            filename = _safe_name(filename)
            if not filename or not filename.lower().endswith(".csv"):
                return None
            p = (Path("exports") / filename).resolve()
            root = Path("exports").resolve()
            if root not in p.parents:
                return None
            return p

        def _csv_has_rows(filename):
            p = _export_path(filename)
            if not p or not p.exists():
                return False
            try:
                with p.open(newline="", encoding="utf-8", errors="ignore") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        if any(str(v or "").strip() for v in row.values()):
                            return True
            except Exception:
                return False
            return False

        candidates = [
            job.get("export_file"),
            job.get("filename"),
            job.get("file"),
            job.get("output_file"),
            job.get("out_name"),
        ]

        export_obj = job.get("export") or {}
        if isinstance(export_obj, dict):
            candidates.extend([
                export_obj.get("filename"),
                export_obj.get("file"),
                export_obj.get("path"),
            ])

        for candidate in candidates:
            safe = _safe_name(candidate)
            if safe and _csv_has_rows(safe):
                return AuthRedirectResponse(
                    url=f"/lead-bot?file={quote(safe)}#results",
                    status_code=303,
                )

        # Recovery: build a desktop CSV from the live cards already shown on screen.
        leads = job.get("leads") or []
        if isinstance(leads, list) and leads:
            params = job.get("params") or {}

            keyword = str(params.get("keyword") or params.get("industry") or "scan").strip().lower()
            market = str(params.get("market") or params.get("location") or "market").strip().lower()

            slug_base = "_".join(x for x in [keyword, market] if x).strip("_") or "scan"
            slug_base = _re.sub(r"[^a-z0-9]+", "_", slug_base).strip("_") or "scan"

            out_name = f"leads_{slug_base}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_desktop.csv"
            out_path = Path("exports") / out_name
            out_path.parent.mkdir(parents=True, exist_ok=True)

            preferred = [
                "title", "business", "domain", "url", "website",
                "best_phone", "phone", "emails", "email",
                "contact_page_url", "contact_page",
                "address", "full_address", "business_address", "formatted_address",
                "serp_page", "serp_position", "page", "position",
                "contact_confidence", "outreach_status", "final_lead_score",
                "page_title", "meta_description", "h1", "reason",
            ]

            fields = []
            for field in preferred:
                if any(isinstance(row, dict) and field in row for row in leads):
                    fields.append(field)

            for row in leads:
                if isinstance(row, dict):
                    for key in row.keys():
                        if key not in fields:
                            fields.append(key)

            if not fields:
                fields = preferred

            with out_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                for row in leads:
                    if isinstance(row, dict):
                        writer.writerow(row)

            try:
                leadbot_mark_export_owner(out_name, request)
            except Exception as exc:
                print(f"LEADBOT LIVE DASHBOARD RECOVERY OWNER WARN: {exc}", flush=True)

            print(f"LEADBOT LIVE DASHBOARD RECOVERED CSV: {out_name}", flush=True)

            return AuthRedirectResponse(
                url=f"/lead-bot?file={quote(out_name)}#results",
                status_code=303,
            )

        return AuthRedirectResponse(url="/lead-bot?live_dashboard=no_export#exports", status_code=303)

    except Exception as exc:
        print(f"LEADBOT LIVE DASHBOARD REDIRECT ERROR: {exc}", flush=True)
        return AuthRedirectResponse(url="/lead-bot?live_dashboard=error#exports", status_code=303)



@app.get("/lead-bot/live/{job_id}", response_class=AuthHTMLResponse)
def leadbot_live_page(job_id: str, request: AuthRequest):
    try:
        user = auth_current_user(request)
    except Exception:
        user = None

    if not user:
        return AuthRedirectResponse(url="/login?next=/lead-bot", status_code=303)

    return AuthHTMLResponse(f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>LeadBot Live Scan</title>
<style>
* {{ box-sizing: border-box; }}
body {{
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
    background: #eef3fb;
    color: #0f172a;
}}
.container {{
    max-width: 1280px;
    margin: auto;
    padding: 28px;
}}
.hero {{
    background: linear-gradient(135deg, #07152f, #1e3a8a);
    color: white;
    border-radius: 24px;
    padding: 28px;
    margin-bottom: 20px;
}}
.hero-row {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 18px;
    flex-wrap: wrap;
}}
.hero h1 {{
    margin: 0 0 8px;
    font-size: 34px;
}}
.hero p {{
    margin: 0;
    color: rgba(255,255,255,.82);
}}
.nav {{
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
}}
.nav a {{
    background: white;
    color: #0f172a;
    text-decoration: none;
    font-weight: 900;
    border-radius: 12px;
    padding: 11px 14px;
}}

.cancel-scan-btn {{
    background: #dc2626;
    color: #fff;
    border: 0;
    text-decoration: none;
    font-weight: 950;
    border-radius: 12px;
    padding: 11px 14px;
    cursor: pointer;
    box-shadow: 0 10px 22px rgba(220, 38, 38, .22);
}}

.cancel-scan-btn:hover {{
    background: #b91c1c;
}}

.cancel-scan-btn:disabled {{
    opacity: .58;
    cursor: not-allowed;
}}

.cancel-note {{
    margin-top: 10px;
    color: #991b1b;
    font-size: 13px;
    font-weight: 900;
    display: none;
}}
.status {{
    background: white;
    border: 1px solid #dbe4f0;
    border-radius: 18px;
    padding: 18px;
    margin-bottom: 18px;
    box-shadow: 0 10px 26px rgba(15,23,42,.06);
}}
.status-grid {{
    display: grid;
    grid-template-columns: repeat(4, minmax(140px, 1fr));
    gap: 12px;
    margin-top: 14px;
}}
.stat {{
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 14px;
    padding: 14px;
}}
.stat b {{
    display: block;
    font-size: 24px;
}}
.stat span {{
    color: #64748b;
    font-size: 13px;
    font-weight: 800;
}}
.leads {{
    display: grid;
    gap: 16px;
}}
.card {{
    background: white;
    border: 1px solid #dbe4f0;
    border-radius: 18px;
    padding: 18px;
    box-shadow: 0 8px 22px rgba(15,23,42,.06);
}}
.card h3 {{
    margin: 0 0 5px;
    font-size: 21px;
}}
.domain {{
    color: #64748b;
    font-weight: 900;
    margin-bottom: 12px;
}}
.badges {{
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 13px;
}}
.badges span {{
    background: #eaf2ff;
    color: #1e3a8a;
    border: 1px solid #bfdbfe;
    padding: 7px 10px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 900;
}}
.info {{
    display: grid;
    grid-template-columns: 170px minmax(220px, 1fr) minmax(260px, 1fr) minmax(260px, 1fr);
    gap: 10px;
}}
.info div {{
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 14px;
    padding: 12px;
    overflow-wrap: anywhere;
}}
.info b {{
    display: block;
    margin-bottom: 5px;
    color: #334155;
    font-size: 12px;
    text-transform: uppercase;
}}
.reason {{
    margin-top: 13px;
    padding: 12px 14px;
    border-left: 5px solid #1e3a8a;
    background: #f8fafc;
    border-radius: 12px;
    color: #334155;
}}
.pulse {{
    display: inline-block;
    width: 7px;
    height: 7px;
    border-radius: 999px;
    background: #22c55e;
    box-shadow: 0 0 0 rgba(34,197,94,.38);
    animation: pulse 1.4s infinite;
    margin-right: 8px;
}}
@keyframes pulse {{
    0% {{ box-shadow: 0 0 0 0 rgba(34,197,94,.7); }}
    70% {{ box-shadow: 0 0 0 10px rgba(34,197,94,0); }}
    100% {{ box-shadow: 0 0 0 0 rgba(34,197,94,0); }}
}}
@media (max-width: 900px) {{
    .status-grid, .info {{ grid-template-columns: 1fr; }}
}}



















/* === DIRECT LIVE CONSOLE CSS START === */
.live-console {{
    margin-top: 16px;
    border-radius: 20px;
    overflow: hidden;
    border: 1px solid rgba(147,197,253,.35);
    background:
        radial-gradient(circle at 15% 18%, rgba(96,165,250,.28), transparent 28%),
        radial-gradient(circle at 85% 80%, rgba(167,139,250,.22), transparent 32%),
        linear-gradient(135deg, #07152f 0%, #0f172a 52%, #1e3a8a 100%);
    box-shadow: 0 18px 42px rgba(15,23,42,.20);
    color: white;
}}

.live-console-top {{
    display: flex;
    gap: 7px;
    padding: 12px 14px;
    background: rgba(255,255,255,.075);
    border-bottom: 1px solid rgba(255,255,255,.11);
}}

.live-dot {{
    width: 9px;
    height: 9px;
    border-radius: 999px;
    background: #60a5fa;
    box-shadow: 0 0 18px rgba(96,165,250,.75);
}}

.live-dot:nth-child(2) {{
    background: #a78bfa;
    box-shadow: 0 0 18px rgba(167,139,250,.75);
}}

.live-dot:nth-child(3) {{
    background: #22c55e;
    box-shadow: 0 0 18px rgba(34,197,94,.75);
}}

.live-console-body {{
    position: relative;
    padding: 16px;
    overflow: hidden;
}}

.live-console-body:before {{
    content: "";
    position: absolute;
    inset: 0;
    background-image:
        linear-gradient(rgba(255,255,255,.07) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,.07) 1px, transparent 1px);
    background-size: 24px 24px;
    opacity: .22;
    pointer-events: none;
}}

.live-console-body:after {{
    content: "";
    position: absolute;
    top: 0;
    left: -45%;
    width: 38%;
    height: 100%;
    background: linear-gradient(90deg, transparent, rgba(96,165,250,.22), rgba(255,255,255,.28), transparent);
    transform: skewX(-14deg);
    animation: leadbotLiveSweep 3.4s ease-in-out infinite;
    pointer-events: none;
}}

.live-line {{
    position: relative;
    z-index: 2;
    display: flex;
    gap: 9px;
    margin: 6px 0;
    color: rgba(219,234,254,.88);
    font-size: 13px;
    line-height: 1.55;
}}

.live-line strong {{
    min-width: 52px;
    color: #93c5fd;
    font-weight: 950;
    text-transform: uppercase;
}}

.live-progress-rail {{
    position: relative;
    z-index: 2;
    height: 10px;
    margin-top: 13px;
    border-radius: 999px;
    background: rgba(255,255,255,.10);
    overflow: hidden;
}}

.live-progress-bar {{
    position: absolute;
    inset: 0;
    width: 45%;
    border-radius: 999px;
    background: linear-gradient(90deg, #60a5fa, #a78bfa, #60a5fa);
    animation: leadbotProgress 4.8s ease-in-out infinite;
}}

@keyframes leadbotLiveSweep {{
    0% {{ left: -45%; opacity: .2; }}
    45% {{ opacity: 1; }}
    100% {{ left: 110%; opacity: .25; }}
}}

@keyframes leadbotProgress {{
    0% {{ transform: translateX(-80%); }}
    100% {{ transform: translateX(230%); }}
}}

.status.leadbot-done-state {{
    border-color: #86efac !important;
    box-shadow: 0 14px 34px rgba(22,163,74,.12) !important;
}}

.status.leadbot-done-state #message {{
    color: #166534 !important;
    font-weight: 950 !important;
}}

.status.leadbot-done-state #message::before {{
    content: "✓ ";
}}

.status.leadbot-done-state .live-progress-bar {{
    width: 100% !important;
    transform: none !important;
    animation: none !important;
    background: linear-gradient(90deg, #22c55e, #16a34a) !important;
}}

/* === LEADBOT CANCELLED FINAL STATE CSS START === */
.status.leadbot-cancelled-state {{
    border-color: #fca5a5 !important;
    box-shadow: 0 14px 34px rgba(220,38,38,.10) !important;
}}

.status.leadbot-cancelled-state #message {{
    color: #991b1b !important;
    font-weight: 950 !important;
}}

.status.leadbot-cancelled-state #message::before {{
    content: "× ";
}}

body.leadbot-live-final .pulse {{
    display: none !important;
}}

body.leadbot-live-final .live-console-body:after,
body.leadbot-live-final .live-progress-bar {{
    animation: none !important;
}}
/* === LEADBOT CANCELLED FINAL STATE CSS END === */

/* === DIRECT LIVE CONSOLE CSS END === */

</style>
</head>
<body>
<div class="container">
    <section class="hero">
        <div class="hero-row">
            <div>
                <h1>LeadBot Live Scan</h1>
                <p>Leads appear as they are found. Contact details fill in as cache/enrichment runs.</p>
            </div>
            <nav class="nav">
                <a href="/lead-bot">LeadBot</a>
                <a href="/">Home</a>
                <a href="/logout">Logout</a>
            </nav>
        </div>
    </section>

    <section class="status">
        <div id="message"><span class="pulse"></span>Starting...</div>
        <div class="status-grid">
            <div class="stat"><b id="found">0</b><span>Found</span></div>
            <div class="stat"><b id="cached">0</b><span>Loaded From Cache</span></div>
            <div class="stat"><b id="enriched">0</b><span>With Contact Info</span></div>
            <div class="stat"><b id="needs">0</b><span>Needs Research</span></div>
        </div>
<div class="live-console">
            <div class="live-console-top">
                <span class="live-dot"></span>
                <span class="live-dot"></span>
                <span class="live-dot"></span>
            </div>
            <div class="live-console-body">
                <div class="live-line"><strong>scan</strong><span id="liveConsoleLine1">Initializing LeadBot crawler...</span></div>
                <div class="live-line"><strong>serp</strong><span id="liveConsoleLine2">Finding page 1–4 opportunities...</span></div>
                <div class="live-line"><strong>data</strong><span id="liveConsoleLine3">Contacts will appear as they are enriched.</span></div>
                <div class="live-progress-rail"><div class="live-progress-bar"></div></div>
            </div>
        </div>
<div id="liveScanActions" style="margin-top:14px;display:flex;align-items:center;justify-content:center;gap:10px;flex-wrap:wrap;">
            <button id="cancelScanBtn" class="cancel-scan-btn" type="button">Cancel Scan</button>
            <div id="cancelNote" class="cancel-note">Cancel requested. The scan will stop at the next safe checkpoint.</div>
        </div>
        
        <p id="exportWrap" style="display:none; margin:18px 0 0; text-align:center;">
            <a id="exportLink" href="/lead-bot/live-dashboard/{job_id}" style="display:inline-flex;align-items:center;justify-content:center;padding:11px 16px;border-radius:12px;background:#1e3a8a;color:#fff!important;font-weight:900;text-decoration:none;box-shadow:0 8px 18px rgba(30,58,138,.22);">Open Desktop</a>
        </p>
    </section>

    <section class="leads" id="leads"></section>


</div>

<script>
const jobId = "{job_id}";

const cancelScanBtn = document.getElementById("cancelScanBtn");
const cancelNote = document.getElementById("cancelNote");

async function cancelScan() {{
    if (!cancelScanBtn) return;

    cancelScanBtn.disabled = true;
    cancelScanBtn.textContent = "Cancelling...";

    if (cancelNote) {{
        cancelNote.style.display = "block";
        cancelNote.textContent = "Cancel requested. The scan will stop at the next safe checkpoint.";
    }}

    try {{
        const res = await fetch(`/lead-bot/live-cancel/${{jobId}}`, {{
            method: "POST",
            cache: "no-store"
        }});

        const data = await res.json();

        if (data.status === "cancelled") {{
            cancelScanBtn.textContent = "Scan Cancelled";
            const msg = document.getElementById("message");
            if (msg) msg.textContent = data.message || "Scan cancelled.";
            if (cancelNote) cancelNote.textContent = "Scan cancelled.";
        }} else {{
            cancelScanBtn.textContent = "Cancel Requested";
            if (cancelNote) cancelNote.textContent = data.message || "Cancel requested.";
        }}
    }} catch (err) {{
        cancelScanBtn.disabled = false;
        cancelScanBtn.textContent = "Cancel Scan";
        if (cancelNote) {{
            cancelNote.style.display = "block";
            cancelNote.textContent = "Cancel failed. Try again.";
        }}
    }}
}}

if (cancelScanBtn) {{
    cancelScanBtn.addEventListener("click", cancelScan);
}}
const seen = new Set();

function esc(value) {{
    return String(value || "").replace(/[&<>"']/g, function (m) {{
        return {{
            "&": "&amp;",
            "<": "&lt;",
            ">": "&gt;",
            '"': "&quot;",
            "'": "&#039;"
        }}[m];
    }});
}}

function link(value) {{
    value = String(value || "");
    if (!value) return "Not found";
    return `<a href="${{esc(value)}}" target="_blank" rel="noopener">${{esc(value)}}</a>`;
}}

function renderLead(lead) {{
    const key = lead.domain || lead.url || lead.title;
    if (seen.has(key)) return;
    seen.add(key);

    const phone = lead.best_phone || "Not found";
    const email = lead.emails || "Not found";
    const contact = lead.contact_page_url ? link(lead.contact_page_url) : "Not found";
    const website = lead.url ? link(lead.url) : "Not found";
    const address = lead.address || lead.full_address || lead.business_address || lead.formatted_address || lead.location || "Not found";

    const metaTitle =
        lead.meta_title ||
        lead.title_tag ||
        lead.seo_title ||
        lead.page_title ||
        lead.site_title ||
        lead.html_title ||
        "";

    const metaDescription =
        lead.meta_description ||
        lead.meta_desc ||
        lead.seo_description ||
        lead.page_description ||
        lead.description ||
        "";
    function realSerpValue(value) {{
        value = String(value || "").trim();
        if (!value) return "";
        if (["manual", "?", "not found", "none", "null", "nan"].includes(value.toLowerCase())) return "";
        return value;
    }}

    const serpPage = realSerpValue(lead.serp_page);
    const serpPosition = realSerpValue(lead.serp_position);
    const serpBadge = (serpPage && serpPosition)
        ? `<span>Page ${{esc(serpPage)}} · Position ${{esc(serpPosition)}}</span>`
        : "";

    const div = document.createElement("article");
    div.className = "card";
    div.innerHTML = `
        <h3>${{esc(lead.title || "Untitled Lead")}}</h3>
        <div class="domain">${{esc(lead.domain || "")}}</div>
        <div class="badges">
            <span>${{esc(lead.outreach_status || "needs_manual_research")}}</span>
            ${{serpBadge}}
            <span>Contact Confidence ${{esc(lead.contact_confidence || "0")}}</span>
            <span>Lead Score ${{esc(lead.final_lead_score || "0")}}</span>
        </div>
        <div class="info">
            <div><b>Phone</b>${{esc(phone)}}</div>
            <div><b>Email</b>${{esc(email)}}</div>
            <div><b>Website</b>${{website}}</div>
            <div><b>Contact Page</b>${{contact}}</div>
        </div>

        <div class="reason" style="border-left-color:#16a34a;">
            <b>Address</b><br>${{esc(address)}}
        </div>

        <div class="leadbot-live-seo-snapshot" style="margin-top:14px !important; padding-top:14px !important;">
            <div class="leadbot-live-meta-title">${{esc(metaTitle || "Not found")}}</div>
            <div class="leadbot-live-meta-description">${{esc(metaDescription || "Not found")}}</div>
        </div>

        <div class="reason"><b>Why this lead</b><br>${{esc(lead.reason || "")}}</div>
    `;

    document.getElementById("leads").appendChild(div);
}}

async function poll() {{
    try {{
        const res = await fetch(`/lead-bot/live-status/${{jobId}}`, {{ cache: "no-store" }});
        const job = await res.json();

        const isFinalLiveStatus = (job.status === "done" || job.status === "cancelled" || job.status === "error");

        document.getElementById("message").innerHTML =
            (isFinalLiveStatus ? "" : '<span class="pulse"></span>') + esc(job.message || job.status || "");

        const counts = job.counts || {{}};
        document.getElementById("found").textContent = counts.found || 0;
        document.getElementById("cached").textContent = counts.cached || 0;
        document.getElementById("enriched").textContent = counts.enriched || 0;
        document.getElementById("needs").textContent = counts.needs_research || 0;

        const params = job.params || {{}};
        
        
        
        

        const liveLine1 = document.getElementById("liveConsoleLine1");
        const liveLine2 = document.getElementById("liveConsoleLine2");
        const liveLine3 = document.getElementById("liveConsoleLine3");

        if (liveLine1) liveLine1.textContent = job.message || "LeadBot is scanning...";
        if (liveLine2) liveLine2.textContent = "Found " + String(counts.found || 0) + " of " + String(params.limit || "—") + " target leads.";
        if (liveLine3) liveLine3.textContent = String(counts.cached || 0) + " cache hits · " + String(counts.enriched || 0) + " enriched · " + String(counts.needs_research || 0) + " need research.";

        if (job.status === "done") {{
            const statusBox = document.querySelector(".status");
            if (statusBox) statusBox.classList.add("leadbot-done-state");

            const leadCount = (job.leads || []).length;
            const leadsWrap = document.getElementById("leads");

            if (leadCount === 0 && leadsWrap && !document.getElementById("leadbotZeroResultsEmpty")) {{
                if (liveLine1) liveLine1.textContent = "No leads found for this scan.";
                if (liveLine2) liveLine2.textContent = "Try a broader search or a clearer location.";
                if (liveLine3) liveLine3.textContent = "State is required for best results.";

                const empty = document.createElement("div");
                empty.id = "leadbotZeroResultsEmpty";
                empty.className = "leadbot-zero-results-empty";
                empty.innerHTML = `
                    <h3>No leads found.</h3>
                    <p>LeadBot finished the scan, but no usable local business leads made it through search and filtering.</p>
                    <ul>
                        <li>Include city and state, like <b>Santa Barbara CA</b>.</li>
                        <li>Try a broader business type.</li>
                        <li>Try a close variation, like <b>bagel shop</b> instead of <b>bagel store</b>.</li>
                    </ul>
                `;
                leadsWrap.appendChild(empty);
            }} else {{
                if (liveLine1) liveLine1.textContent = "Scan complete. Your export is ready.";
                if (liveLine2) liveLine2.textContent = "Search complete.";
                if (liveLine3) liveLine3.textContent = "Dashboard is ready.";
            }}
        }}

        (job.leads || []).forEach(renderLead);

        const exportFileRaw =
            job.export_file ||
            job.filename ||
            job.file ||
            job.output_file ||
            job.out_name ||
            (job.export && (job.export.filename || job.export.file || job.export.path)) ||
            "";

        if (exportFileRaw) {{
            const exportFile = String(exportFileRaw).split(/[\\/]/).pop();
            const dashboardUrl = `/lead-bot?file=${{encodeURIComponent(exportFile)}}#results`;
            const exportWrap = document.getElementById("exportWrap");
            const exportLink = document.getElementById("exportLink");

            exportWrap.style.display = "block";
            exportLink.href = dashboardUrl;
            const exportWrapBottom = document.getElementById("exportWrapBottom");
            const exportLinkBottom = document.getElementById("exportLinkBottom");
            if (exportWrapBottom && exportLinkBottom) {{
                exportWrapBottom.style.display = "block";
                exportLinkBottom.href = dashboardUrl;
                exportLinkBottom.textContent = "Open Desktop";
            }}
            exportLink.onclick = function () {{
                window.location.href = dashboardUrl;
                return false;
            }};
        }} else if (job.status === "done") {{
            const exportWrap = document.getElementById("exportWrap");
            const exportLink = document.getElementById("exportLink");

            exportWrap.style.display = "block";
            exportLink.href = `/lead-bot/live-dashboard/${{jobId}}`;
            exportLink.onclick = function () {{
                window.location.href = `/lead-bot/live-dashboard/${{jobId}}`;
                return false;
            }};
        }}

        if (job.status !== "done" && job.status !== "error" && job.status !== "cancelled") {{
            setTimeout(poll, 1800);
        }}
    }} catch (e) {{
        console.warn("LeadBot live-status poll failed; keeping last visible scan message.", e);
        setTimeout(poll, 2500);
    }}
}}

poll();
</script>
</body>
</html>
""")
# === LEADBOT LIVE JOBS V1 END ===


@app.get("/lead-bot/add-domain")
def leadbot_real_manual_add_domain(
    request: AuthRequest,
    domain: str = "",
    industry: str = "",
    market: str = "",
    keyword: str = "",
    serp_page: str = "",
    serp_position: str = "",
):
    try:
        user = auth_current_user(request)
    except Exception:
        user = None

    if not user:
        return AuthRedirectResponse(url="/login?next=/lead-bot", status_code=303)

    if not str(domain or "").strip():
        return LeadBotRedirectResponse(url="/lead-bot?error=missing_domain", status_code=303)

    try:
        from agents.lead_manual_add_agent import manual_add_domain
        out_name, lead = manual_add_domain(
            domain=domain,
            industry=industry,
            market=market,
            keyword=keyword,
            serp_page=serp_page,
            serp_position=serp_position,
        )
    except Exception as e:
        return LeadBotHTMLResponse(
            f"<h1>Could not add domain</h1><p>{str(e)}</p><p><a href='/lead-bot'>Back to LeadBot</a></p>",
            status_code=400,
        )

    try:
        leadbot_mark_export_owner(out_name, request)
    except Exception as e:
        print("LEADBOT MANUAL ADD OWNER ERROR:", e, flush=True)

    print(f"LEADBOT MANUAL ADD DOMAIN: domain={lead.get('domain')} output={out_name}", flush=True)

    return LeadBotRedirectResponse(url=f"/lead-bot?file={out_name}#results", status_code=303)
# === LEADBOT REAL MANUAL ADD DOMAIN END ===



























# === LEADBOT LIVE SCAN VISUAL UPGRADE START ===


@app.get("/lead-bot/block-domains")
def leadbot_block_domains_route(request: AuthRequest, domains: str = ""):
    import csv
    import re
    from pathlib import Path
    from urllib.parse import urlparse

    try:
        user = auth_current_user(request)
    except Exception:
        user = None

    if not user:
        return AuthRedirectResponse(url="/login?next=/lead-bot", status_code=303)

    def clean_domain(value):
        value = str(value or "").strip().lower().strip(" ,;")
        if not value:
            return ""
        if "://" in value:
            host = urlparse(value).netloc.lower()
        else:
            host = value.split("/")[0].lower()
        host = host.replace("www.", "").strip()
        return host if "." in host else ""

    parts = re.split(r"[\s,;]+", domains or "")
    new_domains = sorted({clean_domain(x) for x in parts if clean_domain(x)})

    block_file = Path("data/leadbot_blocklist.txt")
    block_file.parent.mkdir(parents=True, exist_ok=True)

    existing = set()
    if block_file.exists():
        existing = {clean_domain(x) for x in block_file.read_text(encoding="utf-8").splitlines()}
        existing = {x for x in existing if x}

    added = [d for d in new_domains if d not in existing]

    if added:
        with block_file.open("a", encoding="utf-8") as f:
            for d in added:
                f.write(d + "\n")

    removed_total = 0

    try:
        from agents.lead_blacklist_agent import is_blocked_lead_domain

        for csv_path in Path("exports").glob("*.csv"):
            if csv_path.name == "leadbot_master.csv":
                continue

            with csv_path.open(newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                fieldnames = list(reader.fieldnames or [])

            if not fieldnames:
                continue

            kept = []
            removed = 0

            for row in rows:
                value = row.get("domain") or row.get("url") or row.get("website") or ""
                if is_blocked_lead_domain(value):
                    removed += 1
                else:
                    kept.append(row)

            if removed:
                with csv_path.open("w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(kept)

                removed_total += removed

    except Exception as e:
        print("LEADBOT BLOCK DOMAIN CLEANUP ERROR:", e, flush=True)

    print(f"LEADBOT BLOCK DOMAINS: added={added} removed_rows={removed_total}", flush=True)

    return LeadBotRedirectResponse(url=f"/lead-bot?blocked={len(added)}&removed={removed_total}#exports", status_code=303)
# === LEADBOT DOMAIN BLOCK ROUTE END ===



# === LEADBOT OPEN DASHBOARD LINK FIX START ===




# === LEADBOT EXPORT OWNER HELPER START ===
def leadbot_mark_export_owner(filename, request=None):
    """
    Best-effort export ownership marker.

    This prevents LeadBot enrichment from crashing after writing a CSV.
    It intentionally stays conservative:
    - does not change auth/session logic
    - does not block export creation
    - only calls existing ownership/index helpers if they exist
    """
    try:
        filename = str(filename or "").strip()
        if not filename:
            return False

        user = None
        email = ""

        if request is not None:
            try:
                user = auth_current_user(request)
            except Exception:
                user = None

        if isinstance(user, dict):
            email = str(user.get("email") or user.get("username") or "").strip()
        else:
            email = str(getattr(user, "email", "") or getattr(user, "username", "") or "").strip()

        # If a real ownership function exists elsewhere in this file later,
        # call it without making this helper a hard dependency.
        for fn_name in (
            "leadbot_save_export_owner",
            "leadbot_set_export_owner",
            "save_leadbot_export_owner",
            "set_leadbot_export_owner",
            "register_leadbot_export_owner",
        ):
            fn = globals().get(fn_name)
            if callable(fn):
                try:
                    fn(filename, request)
                    return True
                except TypeError:
                    try:
                        fn(filename, email)
                        return True
                    except Exception:
                        pass
                except Exception:
                    pass

        # Lightweight sidecar record. Safe if unused by the UI.
        try:
            from pathlib import Path
            import json
            from datetime import datetime

            owner_dir = Path("data")
            owner_dir.mkdir(parents=True, exist_ok=True)
            owner_file = owner_dir / "leadbot_export_owners.json"

            if owner_file.exists():
                try:
                    data = json.loads(owner_file.read_text(encoding="utf-8") or "{}")
                except Exception:
                    data = {}
            else:
                data = {}

            data[filename] = {
                "email": email,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }

            owner_file.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        except Exception as exc:
            print(f"LEADBOT EXPORT OWNER SIDEFILE WARNING: {exc}", flush=True)

        return True

    except Exception as exc:
        print(f"LEADBOT EXPORT OWNER MARK WARNING: {exc}", flush=True)
        return False
# === LEADBOT EXPORT OWNER HELPER END ===


@app.get("/lead-bot/enrich/{filename}")
def leadbot_enrich_this_scan(filename: str, request: AuthRequest):
    from agents.lead_email_cleaner_agent import clean_lead_emails
    import csv
    import html as html_lib
    import re
    from datetime import datetime
    from pathlib import Path
    from urllib.parse import urljoin, urlparse
    from urllib.request import Request, urlopen
    from bs4 import BeautifulSoup

    try:
        user = auth_current_user(request)
    except Exception:
        user = None

    if not user:
        return AuthRedirectResponse(url="/login?next=/lead-bot", status_code=303)

    source_path = safe_export_file(filename)

    if not source_path:
        return LeadBotHTMLResponse("Export file not found.", status_code=404)

    def clean_domain(value):
        value = str(value or "").strip()
        if not value:
            return ""

        if "://" not in value:
            value = "https://" + value

        parsed = urlparse(value)
        host = parsed.netloc or parsed.path
        host = host.strip().strip("/")

        if not host:
            return ""

        return host

    def base_urls(row):
        candidates = []

        for key in ["url", "website", "link", "domain"]:
            value = str(row.get(key) or "").strip()
            if value:
                candidates.append(value)

        urls = []
        seen = set()

        for value in candidates:
            if "://" not in value:
                value = "https://" + value

            parsed = urlparse(value)
            host = parsed.netloc or parsed.path
            if not host:
                continue

            base_https = "https://" + host.strip("/")
            base_http = "http://" + host.strip("/")

            for u in [value, base_https, base_http]:
                u = u.rstrip("/")
                if u and u not in seen:
                    seen.add(u)
                    urls.append(u)

        return urls[:5]

    def fetch(url):
        try:
            req = Request(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
            with urlopen(req, timeout=10) as res:
                raw = res.read(500000)
                return raw.decode("utf-8", errors="ignore"), res.geturl()
        except Exception:
            return "", url

    def clean_phone(value):
        value = html_lib.unescape(str(value or ""))
        digits = re.sub(r"\D", "", value)

        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]

        if len(digits) == 10:
            return f"({digits[0:3]}) {digits[3:6]}-{digits[6:10]}"

        return ""

    def strip_html(raw):
        raw = re.sub(r"<script.*?</script>", " ", raw, flags=re.I | re.S)
        raw = re.sub(r"<style.*?</style>", " ", raw, flags=re.I | re.S)
        raw = re.sub(r"<[^>]+>", " ", raw)
        raw = html_lib.unescape(raw)
        raw = re.sub(r"\s+", " ", raw)
        return raw

    def extract_seo_snapshot(html_text):
        snapshot = {
            "page_title": "",
            "meta_description": "",
            "h1": "",
        }

        if not html_text:
            return snapshot

        try:
            soup = BeautifulSoup(html_text, "html.parser")

            if soup.title and soup.title.string:
                snapshot["page_title"] = re.sub(r"\s+", " ", soup.title.string).strip()[:300]

            meta = (
                soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
                or soup.find("meta", attrs={"property": re.compile("^og:description$", re.I)})
                or soup.find("meta", attrs={"name": re.compile("^twitter:description$", re.I)})
            )
            if meta:
                meta_content = meta.get("content") or ""
                snapshot["meta_description"] = re.sub(r"\s+", " ", meta_content).strip()[:500]

            h1 = soup.find("h1")
            if h1:
                snapshot["h1"] = re.sub(r"\s+", " ", h1.get_text(" ", strip=True)).strip()[:300]

        except Exception:
            pass

        return snapshot

    def merge_seo_snapshot(row, html_text):
        snapshot = extract_seo_snapshot(html_text)

        changed = False

        for field in ["page_title", "meta_description", "h1"]:
            current = str(row.get(field) or "").strip()
            incoming = str(snapshot.get(field) or "").strip()

            if incoming and not current:
                row[field] = incoming
                changed = True

        return changed

    def extract_contact(html_text):
        if not html_text:
            return "", []

        decoded = html_lib.unescape(html_text)

        mailto_hits = re.findall(r'href=["\']mailto:([^"\'?#]+)', decoded, flags=re.I)
        normal_email_hits = re.findall(
            r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
            decoded,
        )

        emails = sorted(set(mailto_hits + normal_email_hits))
        emails = [
            e.strip()
            for e in emails
            if e.strip()
            and not e.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".css", ".js"))
            and "example.com" not in e.lower()
            and "domain.com" not in e.lower()
        ]

        phone = ""

        tel_hits = re.findall(r'href=["\']tel:([^"\']+)["\']', decoded, flags=re.I)
        for hit in tel_hits:
            phone = clean_phone(hit)
            if phone:
                break

        if not phone:
            text_only = strip_html(decoded)
            phone_candidates = re.findall(
                r"(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}",
                text_only,
            )

            for hit in phone_candidates:
                phone = clean_phone(hit)
                if phone:
                    break

        return phone, emails[:3]

    def discover_contact_links(html_text, current_url):
        if not html_text:
            return []

        links = []
        seen = set()

        for href in re.findall(r'href=["\']([^"\']+)["\']', html_text, flags=re.I):
            href = html_lib.unescape(href).strip()

            if not href or href.startswith("#"):
                continue

            low = href.lower()

            if any(word in low for word in [
                "contact",
                "about",
                "location",
                "locations",
                "office",
                "team",
                "staff",
                "appointment",
                "request",
            ]):
                full = urljoin(current_url, href).split("#")[0].rstrip("/")

                if full.startswith("http") and full not in seen:
                    seen.add(full)
                    links.append(full)

        return links[:12]

    try:
        from agents.lead_domain_filter_agent import is_bad_lead_domain, bad_lead_reason
    except Exception:
        def is_bad_lead_domain(domain="", title="", url=""):
            return False
        def bad_lead_reason(domain="", title="", url=""):
            return ""

    def enrich_row(row):
        already_phone = str(row.get("best_phone") or row.get("phone") or "").strip()
        already_email = str(row.get("emails") or row.get("email") or "").strip()
        already_page_title = str(row.get("page_title") or "").strip()
        already_meta_description = str(row.get("meta_description") or "").strip()
        already_h1 = str(row.get("h1") or "").strip()

        contact_complete = bool(already_phone and already_email)
        seo_complete = bool(already_page_title and already_meta_description and already_h1)

        if contact_complete and seo_complete:
            return row, False

        candidates = []
        seen = set()

        for base in base_urls(row):
            for url in [
                base,
                base + "/contact",
                base + "/contact-us",
                base + "/contactus",
                base + "/about",
                base + "/about-us",
                base + "/locations",
                base + "/location",
                base + "/appointment",
            ]:
                url = url.rstrip("/")
                if url not in seen:
                    seen.add(url)
                    candidates.append(url)

        found_home_links = []

        # Fetch first few pages, discover real contact links, then fetch those too.
        for candidate in list(candidates[:4]):
            html_text, final_url = fetch(candidate)

            seo_changed = False
            if html_text:
                found_home_links.extend(discover_contact_links(html_text, final_url))
                seo_changed = merge_seo_snapshot(row, html_text)

            phone, emails = extract_contact(html_text)

            if phone or emails:
                if phone and not already_phone:
                    row["best_phone"] = phone
                if emails and not already_email:
                    row["emails"] = ", ".join(emails)

                row["contact_page_url"] = final_url or candidate
                row["contact_confidence"] = "80"

                if (phone or already_phone) and (emails or already_email):
                    row["outreach_status"] = "email_and_call_ready"
                elif phone or already_phone:
                    row["outreach_status"] = "call_ready"
                elif emails or already_email:
                    row["outreach_status"] = "email_ready"

                row["contact_flags"] = "enriched_this_scan"
                return row, True

            if seo_changed and contact_complete:
                row["contact_flags"] = "seo_snapshot_enriched_this_scan"
                return row, True

        for link in found_home_links:
            if link not in seen:
                seen.add(link)
                candidates.append(link)

        for candidate in candidates[4:18]:
            html_text, final_url = fetch(candidate)

            seo_changed = False
            if html_text:
                seo_changed = merge_seo_snapshot(row, html_text)

            phone, emails = extract_contact(html_text)

            if phone or emails:
                if phone and not already_phone:
                    row["best_phone"] = phone
                if emails and not already_email:
                    row["emails"] = ", ".join(emails)

                row["contact_page_url"] = final_url or candidate
                row["contact_confidence"] = "80"

                if (phone or already_phone) and (emails or already_email):
                    row["outreach_status"] = "email_and_call_ready"
                elif phone or already_phone:
                    row["outreach_status"] = "call_ready"
                elif emails or already_email:
                    row["outreach_status"] = "email_ready"

                row["contact_flags"] = "enriched_this_scan"
                return row, True

            if seo_changed and contact_complete:
                row["contact_flags"] = "seo_snapshot_enriched_this_scan"
                return row, True

        seo_only_changed = any(str(row.get(field) or "").strip() for field in ["page_title", "meta_description", "h1"]) and not seo_complete
        if seo_only_changed:
            row["contact_flags"] = "seo_snapshot_enriched_this_scan"
            return row, True

        return row, False

    with open(source_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])

    needed_fields = [
        "best_phone",
        "emails",
        "contact_page_url",
        "contact_confidence",
        "outreach_status",
        "contact_flags",
        "page_title",
        "meta_description",
        "h1",
    ]

    for field in needed_fields:
        if field not in fieldnames:
            fieldnames.append(field)

    from agents.lead_business_cache_agent import apply_cached_business_to_lead, save_business_from_lead

    enriched_count = 0
    cached_count = 0

    for row in rows:
        _, cached_changed = apply_cached_business_to_lead(row)
        if cached_changed:
            cached_count += 1

        _, changed = enrich_row(row)
        if changed:
            enriched_count += 1

        save_business_from_lead(row, enriched=bool(changed or cached_changed))

    stem = Path(source_path).stem

    # Stable enriched output:
    # Avoid creating a new timestamped enriched CSV on every Enrich Website Details click.
    if "_enriched_" in stem:
        base_stem = stem.split("_enriched_")[0]
    elif stem.endswith("_enriched"):
        base_stem = stem[:-len("_enriched")]
    else:
        base_stem = stem

    out_name = f"{base_stem}_enriched.csv"
    out_path = Path("exports") / out_name
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    try:
        from agents.lead_detail_index_agent import sync_csv_to_index
        out_path_for_index = safe_export_file(out_name)
        sync_csv_to_index(out_path_for_index, source_file=out_name)
    except Exception as exc:
        print(f"LEADBOT DETAIL INDEX ENRICH SYNC ERROR: {exc}", flush=True)

    print(
        f"LEADBOT ENRICH THIS SCAN: source={filename} cached={cached_count} enriched={enriched_count} output={out_name}",
        flush=True,
    )

    leadbot_mark_export_owner(out_name, request)
    return LeadBotRedirectResponse(url=f"/lead-bot?file={out_name}&enriched={enriched_count}#results", status_code=303)
# === LEADBOT ENRICH THIS SCAN END ===



# === LEADBOT RAW FILE AUTO ENRICH REDIRECT START ===


@app.get("/lead-bot/delete-row/{filename}")
def leadbot_delete_row_route(filename: str, request: AuthRequest, domain: str = ""):
    import csv
    from pathlib import Path

    safe_name = Path(str(filename or "")).name
    target_domain = str(domain or "").strip().lower().replace("www.", "")

    if not safe_name or safe_name == "leadbot_master.csv":
        return LeadBotHTMLResponse(
            "<h1>Cannot delete from this export.</h1><p><a href='/lead-bot'>Back to LeadBot</a></p>",
            status_code=400,
        )

    if not leadbot_user_can_access_export(safe_name, request):
        return LeadBotHTMLResponse(
            "<h1>Export not available</h1><p>You can only edit your own LeadBot exports.</p><p><a href='/lead-bot'>Back to LeadBot</a></p>",
            status_code=403,
        )

    export_path = safe_export_file(safe_name)

    if not export_path or not export_path.exists():
        return LeadBotHTMLResponse(
            "<h1>Export not found</h1><p><a href='/lead-bot'>Back to LeadBot</a></p>",
            status_code=404,
        )

    with open(export_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])

    kept = []
    removed = 0

    for row in rows:
        row_domain = str(row.get("domain") or "").strip().lower().replace("www.", "")

        if target_domain and row_domain == target_domain:
            removed += 1
            continue

        kept.append(row)

    with open(export_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(kept)

    print(f"LEADBOT DELETE ROW: file={safe_name} domain={target_domain} removed={removed}", flush=True)

    return LeadBotRedirectResponse(url=f"/lead-bot?file={safe_name}#results", status_code=303)
# === LEADBOT DELETE ROW ROUTE END ===






# === LEADBOT CLEAN TOP ACTION BUTTONS START ===


@app.post("/lead-bot/update-address")
def leadbot_update_address(
    request: AuthRequest,
    filename: str = AuthForm(""),
    domain: str = AuthForm(""),
    address: str = AuthForm(""),
):
    import csv
    from pathlib import Path
    from urllib.parse import quote, urlparse, parse_qs

    raw_filename = str(filename or "").strip()

    if not raw_filename:
        try:
            ref = request.headers.get("referer", "")
            qs = parse_qs(urlparse(ref).query)
            raw_filename = (qs.get("file") or [""])[0]
        except Exception:
            raw_filename = ""

    safe_name = Path(raw_filename).name

    def clean_domain(value):
        value = str(value or "").strip().lower()
        value = value.replace("https://", "").replace("http://", "")
        value = value.replace("www.", "").split("/")[0]
        return value.strip()

    target_domain = clean_domain(domain)
    new_address = str(address or "").strip()

    if not safe_name or safe_name == "leadbot_master.csv":
        recovered_name, recovered_path = leadbot_find_editable_export_for_domain(target_domain, request)
        if recovered_name and recovered_path:
            safe_name = recovered_name
            export_path = recovered_path
        else:
            return LeadBotHTMLResponse(
                "<h1>Cannot update address</h1><p>No editable export was found for this lead. Open the specific export from the Exports list, then try again.</p><p><a href='/lead-bot'>Back to LeadBot</a></p>",
                status_code=400,
            )

    if not leadbot_user_can_access_export(safe_name, request):
        # Recovery path: some cards can post a stale/base/enriched filename.
        # Try finding an editable export that actually contains this domain.
        recovered_name, recovered_path = leadbot_find_editable_export_for_domain(target_domain, request)
        if recovered_name and recovered_path and leadbot_user_can_access_export(recovered_name, request):
            print(
                f"LEADBOT UPDATE ADDRESS RECOVERED EXPORT: posted={safe_name} recovered={recovered_name} domain={target_domain}",
                flush=True,
            )
            safe_name = recovered_name
            export_path = recovered_path
        else:
            print(
                f"LEADBOT UPDATE ADDRESS ACCESS DENIED: file={safe_name} domain={target_domain}",
                flush=True,
            )
            return LeadBotHTMLResponse(
                "<h1>Export not available</h1><p>You can only edit your own LeadBot exports.</p><p><a href='/lead-bot'>Back to LeadBot</a></p>",
                status_code=403,
            )

    if "export_path" not in locals():
        export_path = safe_export_file(safe_name)

    if not export_path or not Path(export_path).exists():
        return LeadBotHTMLResponse(
            "<h1>Export not found</h1><p><a href='/lead-bot'>Back to LeadBot</a></p>",
            status_code=404,
        )

    with open(export_path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])

    wanted_fields = [
        "address",
        "full_address",
        "business_address",
        "formatted_address",
        "street_address",
        "mailing_address",
        "address_status",
        "address_source",
    ]

    for field in wanted_fields:
        if field not in fieldnames:
            fieldnames.append(field)

    def row_domains(row):
        values = [
            row.get("domain"),
            row.get("website"),
            row.get("url"),
            row.get("link"),
            row.get("domain_checked_url"),
            row.get("domain_final_url"),
            row.get("final_url"),
        ]
        cleaned = []
        for value in values:
            d = clean_domain(value)
            if d:
                cleaned.append(d)
        return cleaned

    updated = 0

    for row in rows:
        domains = row_domains(row)

        matched = False
        if target_domain:
            for row_domain in domains:
                if row_domain == target_domain or row_domain.endswith("." + target_domain) or target_domain.endswith("." + row_domain):
                    matched = True
                    break

        if not matched:
            continue

        # Save to every address alias the app may display/export.
        row["address"] = new_address
        row["full_address"] = new_address
        row["business_address"] = new_address
        row["formatted_address"] = new_address
        row["street_address"] = new_address
        row["mailing_address"] = new_address
        row["address_status"] = "manual" if new_address else ""
        row["address_source"] = "manual" if new_address else ""

        updated += 1

        try:
            from agents.lead_business_cache_agent import save_business_from_lead
            save_business_from_lead(row, enriched=True)
        except Exception as exc:
            print(f"LEADBOT UPDATE ADDRESS BUSINESS CACHE ERROR: {exc}", flush=True)

        try:
            from agents.lead_detail_index_agent import upsert_lead
            upsert_lead(row, source_file=safe_name)
        except Exception as exc:
            print(f"LEADBOT UPDATE ADDRESS INDEX ERROR: {exc}", flush=True)

    if updated:
        with open(export_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        # Keep base/enriched export pair in sync so the UI does not show stale address data.
        try:
            export_path_obj = Path(export_path)
            twin_paths = []

            if export_path_obj.name.endswith("_enriched.csv"):
                twin_paths.append(export_path_obj.with_name(export_path_obj.name.replace("_enriched.csv", ".csv")))
            elif export_path_obj.name.endswith(".csv"):
                twin_paths.append(export_path_obj.with_name(export_path_obj.name.replace(".csv", "_enriched.csv")))

            for twin_path in twin_paths:
                if not twin_path.exists() or twin_path.resolve() == export_path_obj.resolve():
                    continue

                with open(twin_path, newline="", encoding="utf-8", errors="replace") as tf:
                    twin_reader = csv.DictReader(tf)
                    twin_rows = list(twin_reader)
                    twin_fieldnames = list(twin_reader.fieldnames or [])

                for field in wanted_fields:
                    if field not in twin_fieldnames:
                        twin_fieldnames.append(field)

                twin_updated = 0
                for twin_row in twin_rows:
                    twin_domains = row_domains(twin_row)
                    twin_matched = False

                    if target_domain:
                        for row_domain in twin_domains:
                            if row_domain == target_domain or row_domain.endswith("." + target_domain) or target_domain.endswith("." + row_domain):
                                twin_matched = True
                                break

                    if not twin_matched:
                        continue

                    twin_row["address"] = new_address
                    twin_row["full_address"] = new_address
                    twin_row["business_address"] = new_address
                    twin_row["formatted_address"] = new_address
                    twin_row["street_address"] = new_address
                    twin_row["mailing_address"] = new_address
                    twin_row["address_status"] = "manual" if new_address else ""
                    twin_row["address_source"] = "manual" if new_address else ""
                    twin_updated += 1

                if twin_updated:
                    with open(twin_path, "w", newline="", encoding="utf-8") as tf:
                        twin_writer = csv.DictWriter(tf, fieldnames=twin_fieldnames)
                        twin_writer.writeheader()
                        twin_writer.writerows(twin_rows)

                print(
                    f"LEADBOT UPDATE ADDRESS TWIN: file={twin_path.name} domain={target_domain} updated={twin_updated}",
                    flush=True,
                )
        except Exception as exc:
            print(f"LEADBOT UPDATE ADDRESS TWIN ERROR: {exc}", flush=True)

    print(
        f"LEADBOT UPDATE ADDRESS: file={safe_name} domain={target_domain} updated={updated} address={new_address}",
        flush=True,
    )

    if updated == 0:
        return LeadBotHTMLResponse(
            "<h1>Address not saved</h1><p>The app could not match this card to a row in the selected export. Open the export from the Exports list and try again.</p><p><a href='/lead-bot'>Back to LeadBot</a></p>",
            status_code=400,
        )

    # Old-fashioned browser form submit should return to the dashboard.
    # AJAX/fetch callers can still receive JSON.
    requested_with = ""
    try:
        requested_with = str(request.headers.get("x-requested-with") or "").lower()
    except Exception:
        requested_with = ""

    if requested_with == "fetch":
        try:
            from fastapi.responses import JSONResponse

            return JSONResponse(
                {
                    "ok": True,
                    "success": True,
                    "saved": True,
                    "updated": updated,
                    "filename": safe_name,
                    "domain": target_domain,
                    "address": new_address,
                }
            )
        except Exception:
            pass

    return LeadBotRedirectResponse(
        url=f"/lead-bot?file={quote(safe_name)}#results",
        status_code=303,
    )
# === LEADBOT EDIT ADDRESS END ===



# === REMOVE ROGUE USERS PREFIX ALL HTML START ===


@app.get("/lead-bot/complete-details/{filename}")
def leadbot_complete_details(filename: str, request: Request):
    """
    Tippy-toe background cleanup:
    - Keep the existing Enrich Website Details button.
    - Return to LeadBot immediately.
    - Run address fill + contact enrichment in a background thread.
    """
    from pathlib import Path
    from urllib.parse import quote
    import threading

    safe_name = Path(str(filename or "")).name

    if not safe_name:
        return LeadBotRedirectResponse(url="/lead-bot", status_code=303)

    try:
        if not leadbot_user_can_access_export(safe_name, request):
            return LeadBotHTMLResponse(
                "<h1>Export not available</h1><p>You can only edit your own LeadBot exports.</p><p><a href='/lead-bot'>Back to LeadBot</a></p>",
                status_code=403,
            )
    except Exception:
        pass
    # === LEADBOT SYNC ADDRESS BEFORE BACKGROUND START ===
    # Run address completion before returning, so the dashboard has addresses
    # when the browser reloads after Enrich Website Details.
    try:
        print(f"LEADBOT COMPLETE DETAILS SYNC ADDRESS START: {safe_name}", flush=True)
        leadbot_fill_missing_addresses(safe_name, request)
        print(f"LEADBOT COMPLETE DETAILS SYNC ADDRESS END: {safe_name}", flush=True)
    except Exception as exc:
        print(f"LEADBOT COMPLETE DETAILS SYNC ADDRESS ERROR: {safe_name} {exc}", flush=True)
    # === LEADBOT SYNC ADDRESS BEFORE BACKGROUND END ===



    def _leadbot_background_complete_details():
        print(f"LEADBOT BACKGROUND COMPLETE DETAILS START: {safe_name}", flush=True)

        try:
            leadbot_fill_missing_addresses(safe_name, request)
            leadbot_fill_missing_seo_snapshot(safe_name, request)
        except Exception as exc:
            print(f"LEADBOT BACKGROUND COMPLETE DETAILS ADDRESS/SEO ERROR: {safe_name} {exc}", flush=True)

        try:
            leadbot_enrich_this_scan(safe_name, request)
        except Exception as exc:
            print(f"LEADBOT BACKGROUND COMPLETE DETAILS ENRICH ERROR: {safe_name} {exc}", flush=True)

        print(f"LEADBOT BACKGROUND COMPLETE DETAILS END: {safe_name}", flush=True)

    threading.Thread(
        target=_leadbot_background_complete_details,
        daemon=True,
        name=f"leadbot-complete-details-{safe_name}",
    ).start()

    return LeadBotRedirectResponse(
        url=f"/lead-bot?file={quote(safe_name)}&details=running#results",
        status_code=303,
    )
# === LEADBOT COMPLETE DETAILS ADDRESS + CONTACT END ===











# === LEADBOT PLACES FALLBACK QUOTA GUARD START ===
_LEADBOT_PLACES_FALLBACK_QUOTA_STOPPED = False

def _leadbot_places_fallback_quota_text(value):
    haystack = str(value or "").lower()
    return (
        "429" in haystack
        or "too many requests" in haystack
        or "quota" in haystack
        or "resource_exhausted" in haystack
        or "rate limit" in haystack
    )

def _leadbot_places_fallback_stop_for_quota(reason=""):
    global _LEADBOT_PLACES_FALLBACK_QUOTA_STOPPED
    if not _LEADBOT_PLACES_FALLBACK_QUOTA_STOPPED:
        print(
            "LEADBOT PLACES FALLBACK QUOTA GUARD: stopping fallback address lookups for this process. "
            f"Reason: {str(reason)[:260]}",
            flush=True,
        )
    _LEADBOT_PLACES_FALLBACK_QUOTA_STOPPED = True
# === LEADBOT PLACES FALLBACK QUOTA GUARD END ===

# === LEADBOT GOOGLE PLACES ADDRESS FALLBACK START ===
def _leadbot_google_places_address_fallback(row, market=""):
    if _LEADBOT_PLACES_FALLBACK_QUOTA_STOPPED:
        return ""

    """
    Last-resort address lookup for LeadBot rows.

    Uses Google Places Text Search if a Places/Maps API key exists.
    This is only called when the normal address finder returns nothing.
    """
    import json
    import os
    from urllib.request import Request, urlopen

    try:
        title = str(
            row.get("title")
            or row.get("business_name")
            or row.get("name")
            or ""
        ).strip()

        domain = str(row.get("domain") or "").strip()
        website = str(row.get("website") or row.get("url") or row.get("link") or "").strip()

        if not title and not domain and not website:
            return ""

        query_bits = []
        if title:
            query_bits.append(title)
        if market:
            query_bits.append(str(market).strip())
        elif row.get("market"):
            query_bits.append(str(row.get("market")).strip())

        query = " ".join([bit for bit in query_bits if bit]).strip()

        if not query:
            query = f"{domain} {market}".strip()

        api_key = (
            os.getenv("GOOGLE_PLACES_API_KEY")
            or os.getenv("GOOGLE_MAPS_API_KEY")
            or os.getenv("GOOGLE_API_KEY")
            or ""
        ).strip()

        if not api_key:
            return ""

        payload = json.dumps({
            "textQuery": query,
            "maxResultCount": 3,
        }).encode("utf-8")

        req = Request(
            "https://places.googleapis.com/v1/places:searchText",
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": api_key,
                "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.websiteUri",
            },
        )

        with urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore") or "{}")

        places = data.get("places") or []

        for place in places:
            address = str(place.get("formattedAddress") or "").strip()
            if address:
                print(f"LEADBOT PLACES ADDRESS FALLBACK FOUND: {query} -> {address}", flush=True)
                return address

    except Exception as exc:
        if _leadbot_places_fallback_quota_text(exc):
            _leadbot_places_fallback_stop_for_quota(exc)
            return ""
        print(f"LEADBOT PLACES ADDRESS FALLBACK ERROR: {exc}", flush=True)

    return ""
# === LEADBOT GOOGLE PLACES ADDRESS FALLBACK END ===


# === RESTORED LEADBOT FILL MISSING ADDRESSES FUNCTION START ===
def leadbot_fill_missing_addresses(filename: str, request):
    """
    Restored LeadBot address completer.

    Reads an export CSV, finds missing addresses with agents.address_finding_agent,
    writes address/full_address/business_address/formatted_address columns back
    to the SAME file so the dashboard immediately has usable address data.
    """
    import csv
    from pathlib import Path

    print(f"LEADBOT ADDRESS COMPLETER START: {filename}", flush=True)

    source_path = safe_export_file(filename)

    if not source_path:
        print(f"LEADBOT ADDRESS COMPLETER ERROR: export not found: {filename}", flush=True)
        return None

    try:
        from agents.address_finding_agent import find_business_address
    except Exception as exc:
        print(f"LEADBOT ADDRESS COMPLETER ERROR: cannot import address finder: {exc}", flush=True)
        return None

    try:
        with open(source_path, "r", newline="", encoding="utf-8", errors="ignore") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            fieldnames = list(reader.fieldnames or [])
    except Exception as exc:
        print(f"LEADBOT ADDRESS COMPLETER ERROR: cannot read CSV: {source_path} {exc}", flush=True)
        return None

    if not rows:
        print(f"LEADBOT ADDRESS COMPLETER END: no rows: {filename}", flush=True)
        return source_path

    needed_fields = [
        "address",
        "full_address",
        "business_address",
        "formatted_address",
    ]

    for field in needed_fields:
        if field not in fieldnames:
            fieldnames.append(field)

    def first_value(row, *keys):
        for key in keys:
            value = str(row.get(key) or "").strip()
            if value:
                return value
        return ""

    def has_address(row):
        existing = first_value(
            row,
            "address",
            "full_address",
            "business_address",
            "formatted_address",
            "street_address",
            "place_address",
        )
        if not existing:
            return False

        bad = existing.strip().lower()
        return bad not in {"not found", "none", "null", "n/a", "na", "-"}

    filled = 0
    checked = 0

    for row in rows:
        checked += 1

        if has_address(row):
            continue

        try:
            market = first_value(row, "market", "city", "location")
            found = find_business_address(row, market=market)
        except TypeError:
            try:
                found = find_business_address(row)
            except Exception as exc:
                print(f"LEADBOT ADDRESS COMPLETER ROW ERROR: {first_value(row, 'title', 'domain')} {exc}", flush=True)
                found = ""
        except Exception as exc:
            print(f"LEADBOT ADDRESS COMPLETER ROW ERROR: {first_value(row, 'title', 'domain')} {exc}", flush=True)
            found = ""

        found = str(found or "").strip()

        if found:
            row["address"] = found
            row["full_address"] = found
            row["business_address"] = found
            row["formatted_address"] = found
            filled += 1
            print(f"LEADBOT ADDRESS FOUND: {first_value(row, 'title', 'domain')} -> {found}", flush=True)

    try:
        backup_path = Path(str(source_path) + f".before_address_complete_{Path(source_path).stat().st_mtime_ns}.bak")
        try:
            backup_path.write_bytes(Path(source_path).read_bytes())
        except Exception:
            pass

        with open(source_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    except Exception as exc:
        print(f"LEADBOT ADDRESS COMPLETER ERROR: cannot write CSV: {source_path} {exc}", flush=True)
        return None

    print(f"LEADBOT ADDRESS COMPLETER END: checked={checked} filled={filled} file={filename}", flush=True)
    return source_path
# === RESTORED LEADBOT FILL MISSING ADDRESSES FUNCTION END ===



# === LEADBOT FILL MISSING SEO SNAPSHOT START ===
def leadbot_fill_missing_seo_snapshot(filename: str, request):
    """
    Fill real SEO Snapshot fields in the SAME selected LeadBot CSV.

    Adds/fills:
    - page_title
    - meta_description
    - h1

    This does not fake descriptions from the reason/title.
    It fetches the lead website and parses real HTML meta tags.
    """
    import csv
    import re
    from pathlib import Path
    from urllib.request import Request, urlopen
    from bs4 import BeautifulSoup

    print(f"LEADBOT SEO SNAPSHOT COMPLETER START: {filename}", flush=True)

    try:
        path = safe_export_file(filename)
    except Exception as exc:
        print(f"LEADBOT SEO SNAPSHOT SAFE FILE ERROR: {exc}", flush=True)
        return {"ok": False, "error": str(exc)}

    if not path or not Path(path).exists():
        print(f"LEADBOT SEO SNAPSHOT FILE MISSING: {filename}", flush=True)
        return {"ok": False, "error": "file missing"}

    path = Path(path)

    try:
        with path.open("r", newline="", encoding="utf-8", errors="ignore") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            fieldnames = list(reader.fieldnames or [])
    except Exception as exc:
        print(f"LEADBOT SEO SNAPSHOT READ ERROR: {exc}", flush=True)
        return {"ok": False, "error": str(exc)}

    needed_fields = ["page_title", "meta_description", "h1"]
    for field in needed_fields:
        if field not in fieldnames:
            fieldnames.append(field)

    def clean(value, limit=500):
        value = str(value or "").strip()
        value = re.sub(r"\s+", " ", value)
        return value[:limit].strip()

    def fetch_html(url):
        url = str(url or "").strip()
        if not url:
            return ""

        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        req = Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )

        with urlopen(req, timeout=10) as res:
            content_type = str(res.headers.get("content-type") or "").lower()
            if "text/html" not in content_type and "application/xhtml" not in content_type:
                return ""
            raw = res.read(600000)
            return raw.decode("utf-8", errors="ignore")

    def extract_snapshot(html_text):
        snapshot = {
            "page_title": "",
            "meta_description": "",
            "h1": "",
        }

        soup = BeautifulSoup(html_text or "", "html.parser")

        if soup.title and soup.title.string:
            snapshot["page_title"] = clean(soup.title.string, 300)

        meta = (
            soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
            or soup.find("meta", attrs={"property": re.compile(r"^og:description$", re.I)})
            or soup.find("meta", attrs={"name": re.compile(r"^twitter:description$", re.I)})
        )

        if meta:
            snapshot["meta_description"] = clean(meta.get("content"), 500)

        h1 = soup.find("h1")
        if h1:
            snapshot["h1"] = clean(h1.get_text(" ", strip=True), 300)

        return snapshot

    changed = 0
    tried = 0

    for row in rows:
        url = (
            row.get("url")
            or row.get("website")
            or row.get("Website")
            or row.get("link")
            or ""
        )

        already_title = clean(row.get("page_title"), 300)
        already_desc = clean(row.get("meta_description"), 500)
        already_h1 = clean(row.get("h1"), 300)

        if already_title and already_desc and already_h1:
            continue

        if not url:
            continue

        tried += 1

        try:
            html_text = fetch_html(url)
            if not html_text:
                continue

            snapshot = extract_snapshot(html_text)

            row_changed = False

            if snapshot["page_title"] and not already_title:
                row["page_title"] = snapshot["page_title"]
                row_changed = True

            if snapshot["meta_description"] and not already_desc:
                row["meta_description"] = snapshot["meta_description"]
                row_changed = True

            if snapshot["h1"] and not already_h1:
                row["h1"] = snapshot["h1"]
                row_changed = True

            if row_changed:
                changed += 1
                print(
                    "LEADBOT SEO SNAPSHOT FOUND:",
                    row.get("domain") or row.get("url") or "",
                    "| title:",
                    bool(row.get("page_title")),
                    "| desc:",
                    bool(row.get("meta_description")),
                    "| h1:",
                    bool(row.get("h1")),
                    flush=True,
                )

        except Exception as exc:
            print(f"LEADBOT SEO SNAPSHOT SKIP {url}: {exc}", flush=True)
            continue

    if changed:
        backup_path = path.with_suffix(path.suffix + f".seo-snapshot-{__import__('time').strftime('%Y%m%d-%H%M%S')}.bak")
        backup_path.write_text(path.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8")

        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

    print(
        f"LEADBOT SEO SNAPSHOT COMPLETER END: tried={tried} changed={changed} file={path.name}",
        flush=True,
    )

    return {"ok": True, "tried": tried, "changed": changed, "file": path.name}
# === LEADBOT FILL MISSING SEO SNAPSHOT END ===


# === LEADBOT FILL MISSING ADDRESSES START ===


@app.post("/lead-bot/update-contact-fields")
def leadbot_update_contact_fields(
    request: AuthRequest,
    filename: str = AuthForm(""),
    domain: str = AuthForm(...),
    phone: str = AuthForm(""),
    email: str = AuthForm(""),
    website: str = AuthForm(""),
    contact_page: str = AuthForm(""),
):
    import csv
    from pathlib import Path
    from urllib.parse import quote, urlparse, parse_qs
    from fastapi.responses import JSONResponse
    from fastapi.responses import JSONResponse

    raw_filename = str(filename or "").strip()
    autosave_mode = str(request.query_params.get("autosave") or "").strip() == "1"

    if not raw_filename:
        try:
            ref = request.headers.get("referer", "")
            qs = parse_qs(urlparse(ref).query)
            raw_filename = (qs.get("file") or [""])[0]
        except Exception:
            raw_filename = ""

    safe_name = Path(raw_filename).name

    def clean_domain(value):
        value = str(value or "").strip().lower()
        value = value.replace("https://", "").replace("http://", "")
        value = value.replace("www.", "").split("/")[0]
        return value

    target_domain = clean_domain(domain)

    if not safe_name or safe_name == "leadbot_master.csv":
        recovered_name, recovered_path = leadbot_find_editable_export_for_domain(target_domain, request)
        if recovered_name and recovered_path:
            safe_name = recovered_name
            export_path = recovered_path
        else:
            return LeadBotHTMLResponse(
                "<h1>Cannot update contact fields</h1><p>No editable export was found for this lead. Open the specific export from the Exports list, then try again.</p><p><a href='/lead-bot'>Back to LeadBot</a></p>",
                status_code=400,
            )

    if not leadbot_user_can_access_export(safe_name, request):
        return LeadBotHTMLResponse(
            "<h1>Export not available</h1><p>You can only edit your own LeadBot exports.</p><p><a href='/lead-bot'>Back to LeadBot</a></p>",
            status_code=403,
        )

    if "export_path" not in locals():
        export_path = safe_export_file(safe_name)

    if not export_path or not export_path.exists():
        return LeadBotHTMLResponse(
            "<h1>Export not found</h1><p><a href='/lead-bot'>Back to LeadBot</a></p>",
            status_code=404,
        )

    with open(export_path, newline="", encoding="utf-8", errors="ignore") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])

    needed = [
        "phone", "phones", "phone_number", "phone_numbers", "primary_phone", "telephone", "tel",
        "email", "emails",
        "website", "url",
        "contact_page", "contact_page_url", "contact_url",
    ]

    for field in needed:
        if field not in fieldnames:
            fieldnames.append(field)

    phone_val = str(phone or "").strip()
    email_val = str(email or "").strip()
    website_val = str(website or "").strip()
    contact_val = str(contact_page or "").strip()

    updated = False

    for row in rows:
        row_domain = clean_domain(row.get("domain") or row.get("website") or row.get("url") or "")

        if row_domain == target_domain:
            row["phone"] = phone_val
            row["phones"] = phone_val
            row["phone_number"] = phone_val
            row["phone_numbers"] = phone_val
            row["primary_phone"] = phone_val
            row["telephone"] = phone_val
            row["tel"] = phone_val

            row["email"] = email_val
            row["emails"] = email_val

            row["website"] = website_val
            row["url"] = website_val

            row["contact_page"] = contact_val
            row["contact_page_url"] = contact_val
            row["contact_url"] = contact_val

            updated = True

    if not updated:
        return LeadBotHTMLResponse(
            "<h1>Lead not found</h1><p>Could not match that domain in this export.</p><p><a href='/lead-bot'>Back to LeadBot</a></p>",
            status_code=404,
        )

    with open(export_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    return LeadBotRedirectResponse(
        url=f"/lead-bot?file={quote(safe_name)}#results",
        status_code=303,
    )
# === LEADBOT EDIT CONTACT FIELDS END ===



# === ADMIN USERS SINGLE HEADER STYLE START ===


@app.post("/lead-bot/save-details")
def leadbot_save_details_combined(
    request: AuthRequest,
    filename: str = AuthForm(""),
    domain: str = AuthForm(""),
    phone: str = AuthForm(""),
    email: str = AuthForm(""),
    website: str = AuthForm(""),
    contact_page: str = AuthForm(""),
    address: str = AuthForm(""),
):
    import csv
    from pathlib import Path
    from urllib.parse import quote, urlparse, parse_qs

    raw_filename = str(filename or "").strip()

    if not raw_filename:
        try:
            ref = request.headers.get("referer", "")
            qs = parse_qs(urlparse(ref).query)
            raw_filename = (qs.get("file") or [""])[0]
        except Exception:
            raw_filename = ""

    safe_name = Path(raw_filename).name

    def clean_domain(value):
        value = str(value or "").strip().lower()
        value = value.replace("https://", "").replace("http://", "")
        value = value.replace("www.", "").split("/")[0]
        return value.strip()

    target_domain = clean_domain(domain)

    if not safe_name or safe_name == "leadbot_master.csv":
        recovered_name, recovered_path = leadbot_find_editable_export_for_domain(target_domain, request)
        if recovered_name and recovered_path:
            safe_name = recovered_name
            export_path = recovered_path
        else:
            return LeadBotHTMLResponse(
                "<h1>Cannot save details</h1><p>No editable export was found for this lead. Open the specific export from the Exports list, then try again.</p><p><a href='/lead-bot'>Back to LeadBot</a></p>",
                status_code=400,
            )

    if not leadbot_user_can_access_export(safe_name, request):
        return LeadBotHTMLResponse(
            "<h1>Export not available</h1><p>You can only edit your own LeadBot exports.</p><p><a href='/lead-bot'>Back to LeadBot</a></p>",
            status_code=403,
        )

    if "export_path" not in locals():
        export_path = safe_export_file(safe_name)

    if not export_path or not Path(export_path).exists():
        return LeadBotRedirectResponse(url="/lead-bot#results", status_code=303)

    with open(export_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])

    wanted_fields = [
        "phone",
        "email",
        "website",
        "contact_page",
        "best_phone",
        "emails",
        "url",
        "contact_page_url",
        "address",
        "full_address",
        "business_address",
        "formatted_address",
    ]

    for field in wanted_fields:
        if field not in fieldnames:
            fieldnames.append(field)

    updated = 0

    for row in rows:
        row_domain = clean_domain(
            row.get("domain")
            or row.get("website")
            or row.get("url")
            or row.get("link")
            or ""
        )

        if row_domain != target_domain:
            continue

        # Save submitted values exactly, including blanks.
        # This lets users clear bad phone/email/address/contact data.
        row["phone"] = phone
        row["best_phone"] = phone

        row["email"] = email
        row["emails"] = email

        row["website"] = website
        row["url"] = website

        row["contact_page"] = contact_page
        row["contact_page_url"] = contact_page

        row["address"] = address
        row["full_address"] = address
        row["business_address"] = address
        row["formatted_address"] = address

        updated += 1

        try:
            from agents.lead_business_cache_agent import save_business_from_lead
            save_business_from_lead(row, enriched=True)
        except Exception as exc:
            print(f"LEADBOT SAVE DETAILS BUSINESS CACHE ERROR: {exc}", flush=True)

        try:
            from agents.lead_detail_index_agent import upsert_lead
            upsert_lead(row, source_file=safe_name)
        except Exception as exc:
            print(f"LEADBOT SAVE DETAILS INDEX ERROR: {exc}", flush=True)

    if updated:
        with open(export_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    print(f"LEADBOT SAVE DETAILS: file={safe_name} domain={target_domain} updated={updated}", flush=True)

    return LeadBotRedirectResponse(
        url=f"/lead-bot?file={quote(safe_name)}#results",
        status_code=303,
    )
# === LEADBOT SAVE DETAILS COMBINED END ===



# === ADMIN USERS CREATE CARD POLISH START ===


@app.get("/lead-bot/open-desktop")
def leadbot_open_desktop_folder(request: Request):
    """
    Opens the LeadBot exports folder in Windows Explorer from WSL.
    Keeps LeadBot scan/card logic untouched.
    """
    import subprocess
    from pathlib import Path

    try:
        exports_dir = Path("exports").resolve()
        exports_dir.mkdir(parents=True, exist_ok=True)

        try:
            win_path = subprocess.check_output(
                ["wslpath", "-w", str(exports_dir)],
                text=True
            ).strip()
        except Exception:
            win_path = str(exports_dir)

        subprocess.Popen(
            ["explorer.exe", win_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        return AuthRedirectResponse(url="/lead-bot?desktop_opened=1", status_code=303)

    except Exception as exc:
        print(f"LEADBOT OPEN DESKTOP ERROR: {exc}", flush=True)
        return AuthRedirectResponse(url="/lead-bot?desktop_opened=0", status_code=303)
# === LEADBOT OPEN DESKTOP ROUTE END ===


# === SETTINGS BLOCKED DOMAINS LIST LINK START ===


@app.get("/lead-bot/export/{filename}")
def lead_bot_export(filename: str, request: AuthRequest):
    user = auth_current_user(request)
    if not user:
        return AuthRedirectResponse(url="/login?next=/lead-bot", status_code=303)

    path = safe_export_file(filename)

    if not path:
        return LeadBotHTMLResponse("File not found", status_code=404)

    if not _leadbot_export_visible_to_user(path, current_user=user):
        return LeadBotHTMLResponse(
            "<h1>Export not available</h1>"
            "<p>You can only download your own LeadBot exports.</p>"
            "<p><a href='/lead-bot'>Back to LeadBot</a></p>",
            status_code=403,
        )

    return LeadBotFileResponse(path, filename=path.name)


@app.get("/lead-bot/open-desktop")
def leadbot_open_desktop():
    """
    Open the LeadBot exports folder on the local desktop.

    Works for:
    - WSL on Windows: explorer.exe with converted Windows path
    - native Windows: os.startfile
    - macOS: open
    - Linux desktop: xdg-open
    """
    from pathlib import Path
    import os
    import shutil
    import subprocess
    import sys

    try:
        base_dir = BASE_DIR
    except NameError:
        base_dir = Path(__file__).resolve().parents[1]

    exports_dir = Path(base_dir) / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)

    try:
        # WSL -> Windows Explorer
        if shutil.which("wslpath") and shutil.which("explorer.exe"):
            win_path = subprocess.check_output(
                ["wslpath", "-w", str(exports_dir)],
                text=True,
            ).strip()
            subprocess.Popen(["explorer.exe", win_path])
            return RedirectResponse(url="/lead-bot", status_code=303)

        # Native Windows
        if sys.platform.startswith("win"):
            os.startfile(str(exports_dir))  # type: ignore[attr-defined]
            return RedirectResponse(url="/lead-bot", status_code=303)

        # macOS
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(exports_dir)])
            return RedirectResponse(url="/lead-bot", status_code=303)

        # Linux desktop
        subprocess.Popen(["xdg-open", str(exports_dir)])
        return RedirectResponse(url="/lead-bot", status_code=303)

    except Exception as exc:
        return PlainTextResponse(
            f"Could not open LeadBot exports folder.\n\nFolder: {exports_dir}\nError: {exc}",
            status_code=500,
        )


@app.get("/lead-bot/debug-complete-addresses/{filename}")
def leadbot_debug_complete_addresses(filename: str, request: AuthRequest):
    """
    Admin-only debug route for address completion.

    This calls the same restored address completion route if available.
    Use it to prove whether address filling is a backend issue or just auto-button timing.
    """
    user = auth_current_user(request)

    role = ""
    if isinstance(user, dict):
        role = str(user.get("role") or "").strip().lower()
    else:
        role = str(getattr(user, "role", "") or "").strip().lower()

    if not user or role != "admin":
        return LeadBotHTMLResponse(
            "<h1>Admin required</h1><p>This debug route is admin-only.</p>",
            status_code=403,
        )

    import csv
    from pathlib import Path

    safe_name = Path(filename).name

    try:
        before_path = EXPORT_DIR / safe_name
    except NameError:
        before_path = Path("exports") / safe_name

    before_filled = 0
    before_rows = 0

    if before_path.exists():
        try:
            with before_path.open("r", encoding="utf-8", errors="ignore", newline="") as f:
                before_data = list(csv.DictReader(f))
            before_rows = len(before_data)
            before_filled = sum(
                1 for row in before_data
                if str(row.get("address") or row.get("business_address") or row.get("formatted_address") or "").strip()
            )
        except Exception:
            pass

    # Call the existing route/function if it exists.
    try:
        result = leadbot_complete_addresses(filename)
    except NameError:
        try:
            result = complete_addresses(filename)
        except NameError as exc:
            return PlainTextResponse(
                "Website enrichmentr function was not found.\\n"
                "Search app/main.py for the restored address completer function name.\\n"
                f"Error: {exc}",
                status_code=500,
            )
    except Exception as exc:
        return PlainTextResponse(
            f"Website enrichmentr crashed for {safe_name}:\\n{exc}",
            status_code=500,
        )

    after_filled = 0
    after_rows = 0

    if before_path.exists():
        try:
            with before_path.open("r", encoding="utf-8", errors="ignore", newline="") as f:
                after_data = list(csv.DictReader(f))
            after_rows = len(after_data)
            after_filled = sum(
                1 for row in after_data
                if str(row.get("address") or row.get("business_address") or row.get("formatted_address") or "").strip()
            )
        except Exception:
            pass

    return PlainTextResponse(
        "LeadBot address debug complete\\n\\n"
        f"File: {safe_name}\\n"
        f"Rows before: {before_rows}\\n"
        f"Addresses before: {before_filled}\\n"
        f"Rows after: {after_rows}\\n"
        f"Addresses after: {after_filled}\\n"
        f"Result object: {result}\\n"
    )

# === LEADBOT DELETE EXPORT ROUTE START ===






# === LEADBOT SAFE OVERRIDE DELETE EXPORT ROUTE START ===
@app.get("/lead-bot/delete-export/{filename}")
def leadbot_delete_export(filename: str, request: AuthRequest):
    import json
    import re
    from pathlib import Path

    user = auth_current_user(request)
    if not user:
        return LeadBotHTMLResponse("Login required", status_code=401)

    target = safe_export_file(filename)
    if not target:
        return AuthRedirectResponse(url="/lead-bot?deleted=0", status_code=303)

    # Security check:
    # Admin may delete any export.
    # Standard user may delete only owned/visible export.
    if not _leadbot_export_visible_to_user(target, current_user=user):
        return LeadBotHTMLResponse("Forbidden", status_code=403)

    export_dir = Path("exports").resolve()
    names_to_delete = set()
    names_to_delete.add(target.name)

    # Delete matching base/enriched siblings too.
    # Example:
    # leads_x.csv
    # leads_x_enriched.csv
    # leads_x_enriched_YYYYMMDD_HHMMSS.csv
    try:
        name = target.name
        if "_enriched" in name:
            base_name = re.sub(r"_enriched(?:_\d{8}_\d{6})?\.csv$", ".csv", name)
            if base_name:
                names_to_delete.add(base_name)
        else:
            stem = target.stem
            for sibling in export_dir.glob(f"{stem}_enriched*.csv"):
                names_to_delete.add(sibling.name)
    except Exception:
        pass

    deleted = []

    for name in sorted(names_to_delete):
        path = safe_export_file(name)
        if not path:
            continue

        # Re-check every file path stays inside exports.
        try:
            resolved = path.resolve()
            if export_dir not in resolved.parents:
                continue
        except Exception:
            continue

        try:
            path.unlink()
            deleted.append(name)
        except FileNotFoundError:
            pass
        except Exception as exc:
            print(f"LEADBOT DELETE EXPORT ERROR {name}: {exc}", flush=True)

        # Delete sidecar owner file.
        try:
            sidecar = export_dir / f"{name}.owner.json"
            if sidecar.exists():
                sidecar.unlink()
        except Exception:
            pass

    # Remove ownership map entries for deleted files.
    try:
        owner_map_path = Path("data/leadbot_export_owners.json")
        if owner_map_path.exists():
            try:
                owners = json.loads(owner_map_path.read_text(encoding="utf-8") or "{}")
            except Exception:
                owners = {}

            if isinstance(owners, dict):
                changed = False
                for name in names_to_delete:
                    if name in owners:
                        owners.pop(name, None)
                        changed = True

                if changed:
                    owner_map_path.write_text(
                        json.dumps(owners, indent=2, sort_keys=True),
                        encoding="utf-8",
                    )
    except Exception as exc:
        print(f"LEADBOT DELETE OWNER MAP ERROR: {exc}", flush=True)

    print(f"LEADBOT DELETE EXPORT deleted={deleted}", flush=True)
    return AuthRedirectResponse(url="/lead-bot?deleted=1", status_code=303)

