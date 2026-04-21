from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from collections import Counter
from datetime import datetime
from urllib.parse import urlparse
import os
import re
import tempfile

import requests
from bs4 import BeautifulSoup

from app.agent_service import run_agent_summary

app = FastAPI()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

static_dir = os.path.join(BASE_DIR, "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


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

    if phrase_has_location_term(t) and phrase_has_service_term(t):
        return "location"
    if phrase_has_commercial_term(t) and phrase_has_service_term(t):
        return "commercial"
    return "service"


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
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        title = soup.title.string.strip() if soup.title and soup.title.string else ""

        meta_tag = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
        meta = meta_tag["content"].strip() if meta_tag and meta_tag.get("content") else ""

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

        top_terms = build_top_terms(tokens, 30)
        top_bigrams = build_top_bigrams(tokens, 20)
        top_trigrams = build_top_trigrams(tokens, 20)
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

        data.update({
            "title": title,
            "meta_description": meta if meta else "No meta description",
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
            "h2_count": len(h2_tags),
            "internal_link_count": internal_link_count,
            "canonical": canonical,
            "image_count": image_count,
            "alt_count": alt_count,
        })
        return data

    except Exception as e:
        data["error"] = str(e)
        data["meta_description"] = "ERROR"
        data["meta_status"] = "Missing"
        data["title_status"] = "Missing"
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
        grouped[bucket].append({
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


def build_analysis_html(site, competitors, gap):
    comp = competitors[0]

    winner_name = site["clean_domain"] if site["score"] > comp["score"] else comp["clean_domain"]
    if site["score"] == comp["score"]:
        winner_name = "Both sites"

    your_top = ", ".join(site.get("keywords", [])[:6]) or "limited keyword coverage"
    comp_top = ", ".join(comp.get("keywords", [])[:6]) or "limited keyword coverage"

    missing_terms = []
    for bucket in ("service", "location", "commercial"):
        missing_terms.extend([x["term"] for x in gap["missing_grouped"][bucket]])
    missing_preview = ", ".join(missing_terms[:6]) if missing_terms else "no major gaps"

    return f"""
    <h3>Executive Summary</h3>
    <p><strong>{winner_name}</strong> currently shows the stronger on-page SEO profile based on metadata, content depth, heading usage, keyword coverage, and supporting page elements.</p>

    <h3>Metadata</h3>
    <p>Your site title length is <strong>{site.get('title_length', 0)}</strong> characters and meta length is <strong>{site.get('meta_length', 0)}</strong>. Competitor title length is <strong>{comp.get('title_length', 0)}</strong> and meta length is <strong>{comp.get('meta_length', 0)}</strong>. Recommended targets are roughly <strong>30–65</strong> for titles and <strong>70–160</strong> for meta descriptions.</p>

    <h3>Content Depth</h3>
    <p>Your site has roughly <strong>{site.get('word_count', 0)}</strong> meaningful words, while the competitor has roughly <strong>{comp.get('word_count', 0)}</strong>. More copy alone does not win rankings, but stronger depth usually gives the page more room to support topical relevance and internal linking.</p>

    <h3>Keyword Coverage</h3>
    <p>Your page leans on phrases like <strong>{your_top}</strong>. The competitor leans on phrases like <strong>{comp_top}</strong>. The clearest opportunity is to close gaps around <strong>{missing_preview}</strong> rather than just repeating existing single-word terms.</p>

    <h3>Wrap-Up</h3>
    <p>The strongest next move is to tighten title and H1 alignment, strengthen topical phrase coverage, and improve weak page elements before chasing bigger off-page wins. This report is most useful as a quick on-page benchmark, not a final technical audit.</p>
    """


def export_pdf(html: str) -> str:
    file_path = os.path.join(
        tempfile.gettempdir(),
        f"seo_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    )
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(html)
    return file_path


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request,
            "logo_url": safe_logo_url(),
        },
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    location_sets = get_location_sets()
    custom_text = load_custom_location_text()

    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "request": request,
            "locations": custom_text,
            "location_list": location_sets["combined"],
            "built_in_location_list": location_sets["built_in"],
            "custom_location_list": location_sets["custom"],
            "saved": False,
        },
    )


@app.post("/save-settings", response_class=HTMLResponse)
async def save_settings(request: Request, locations: str = Form(...)):
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
            "locations": cleaned_text,
            "location_list": location_sets["combined"],
            "built_in_location_list": location_sets["built_in"],
            "custom_location_list": location_sets["custom"],
            "saved": True,
        },
    )


@app.post("/analyze", response_class=HTMLResponse)
async def analyze(
    request: Request,
    url_1: str = Form(...),
    url_2: str = Form(...),
):
    site = fetch_page_data(url_1)
    competitor = fetch_page_data(url_2)
    competitors_sorted = [competitor]

    site_score, site_breakdown = score_page(site)
    comp_score, comp_breakdown = score_page(competitor)

    site["score"] = site_score
    competitor["score"] = comp_score

    gap = keyword_gap(site, competitors_sorted)

    site_quick_wins = build_quick_wins(site, competitor)

    analysis = build_analysis_html(site, competitors_sorted, gap)
    try:
        agent_analysis = run_agent_summary(
            site=site,
            competitors=competitors_sorted,
            gap=gap,
            site_quick_wins=site_quick_wins,
        )
        if agent_analysis:
            analysis = agent_analysis
    except Exception:
        pass

    competitor_quick_wins = [
        {
            "domain": competitor["clean_domain"],
            "items": build_quick_wins(competitor, site),
        }
    ]

    site_section_card = build_section_card(site)
    competitor_section_cards = [build_section_card(competitor)]

    return templates.TemplateResponse(
        request=request,
        name="report.html",
        context={
            "request": request,
            "site": site,
            "competitors": competitors_sorted,
            "analysis_html": analysis,
            "gap": gap,
            "site_score_breakdown": site_breakdown,
            "competitor_score_breakdowns": [
                {
                    "domain": competitor["clean_domain"],
                    "breakdown": comp_breakdown,
                    "score": competitor["score"],
                }
            ],
            "site_quick_wins": site_quick_wins,
            "competitor_quick_wins": competitor_quick_wins,
            "site_section_card": site_section_card,
            "competitor_section_cards": competitor_section_cards,
            "generated_at": datetime.now().strftime("%B %d, %Y"),
            "logo_url": safe_logo_url(),
        },
    )


@app.post("/export-pdf")
def export(html: str = Form(...)):
    try:
        path = export_pdf(html)
        return JSONResponse({"file": path})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)