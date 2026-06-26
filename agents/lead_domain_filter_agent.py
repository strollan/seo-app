"""
LeadBot shared domain filter.

Purpose:
- Keep junk SERP results out of cards, exports, and paid/background cleanup.
- One shared filter used across LeadBot instead of random duct tape.

This should block:
- government / city / county / public works pages
- directories / social / review sites
- national chains that are poor SEO outreach targets
- news/listicle/info pages
"""

from __future__ import annotations
from agents.leadbot_block_gate import load_main_blocked_domains, is_main_blocked_domain, is_geo_only_domain_for_market

import re
from pathlib import Path
from urllib.parse import urlparse


BLOCKED_EXACT_DOMAINS = {
    "www.joe.coffee",

    "joe.coffee",

    "www.postmates.com",

    "tiktok.com",
    "www.tiktok.com",
    "facebook.com",
    "m.facebook.com",
    "instagram.com",
    "linkedin.com",
    "x.com",
    "twitter.com",
    "youtube.com",
    "youtu.be",
    "pinterest.com",
    "reddit.com",
    "threads.net",
    "snapchat.com",

    "klcc.org",
    "wttw.com",
    "inquirer.com",
    "chicagobusiness.com",
    "ny.eater.com",

    "chownow.com",
    "inquirer.com",
    "maps.apple.com",
    "grubhub.com",
    "nextdoor.com",
    "chicagobusiness.com",
    "coffeeshop-usa.nears.me",
    "wttw.com",
    "oprfchamber.org",
    "panerabread.com",
    "local.albertsons.com",
    "doordash.com",
    "food4less.com",
    "wonder.com",
    "ny.eater.com",

    "chownow.com",
    "inquirer.com",
    "maps.apple.com",
    "grubhub.com",
    "nextdoor.com",
    "chicagobusiness.com",
    "coffeeshop-usa.nears.me",
    "wttw.com",
    "oprfchamber.org",
    "panerabread.com",
    "local.albertsons.com",
    "doordash.com",
    "food4less.com",
    "wonder.com",
    "ny.eater.com",

    "maps.apple.com",
    "locations.baskinrobbins.com",
    "locations.dunkindonuts.com",
    "ny.eater.com",
    "wonder.com",

    "www.seamless.com",

    "seamless.com",

    "substack.com",

    "order.online",

    "locations.tacobell.com",

    "tacobell.com",

    "cityplace.com",

    "miaminewtimes.com",

    "streetfoodfinder.com",

    "apple.com",

    "maps.apple.com",

    "www.ubereats.com",

    "travelyourself.ca",

    "seattlemet.com",

    "bozemanonline.com",

    "bozemandailychronicle.com",

    "bozemanmagazine.com",

    "goldbelly.com",

    "findmeglutenfree.com",

    "toasttab.com",

    "postmates.com",

    "grubhub.com",

    "doordash.com",

    "ubereats.com",

    "tiktok.com",

    # Directories / review / social
    "yelp.com",
    "groupon.com",
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "nextdoor.com",
    "bbb.org",
    "angi.com",
    "homeadvisor.com",
    "thumbtack.com",
    "yellowpages.com",
    "mapquest.com",
    "tripadvisor.com",
    "foursquare.com",
    "manta.com",
    "chamberofcommerce.com",
    "alignable.com",

    # Big marketplaces / ecommerce / generic
    "amazon.com",
    "walmart.com",
    "target.com",
    "chewy.com",

    # Common chains / poor local SEO outreach targets
    "petco.com",
    "petsmart.com",
    "tractorsupply.com",
    "llbean.com",
    "servpro.com",
    "republicservices.com",
    "recology.com",
    "1800gotjunk.com",
    "oxifresh.com",

    # News / listicles
    "forbes.com",
    "patch.com",
    "i95rock.com",
    "wtnh.com",
    "visitflorida.com",
}


BLOCKED_DOMAIN_PARTS = (
    "postmates",

    "chamber.org",
    "nears.me",
    "eater.com",
    "maps.apple",
    "grubhub",
    "doordash",
    "chownow",
    "albertsons",
    "panerabread",
    "food4less",
    "chamber.org",
    "nears.me",
    "eater.com",
    "maps.apple",
    "grubhub",
    "doordash",
    "chownow",
    "albertsons",
    "panerabread",
    "food4less",
    "visit",

    "substack",

    "tacobell",

    "cityplace",

    "newtimes",

    "times.com",

    "magazine",

    "cityof",
    "countyof",
    "townof",
    "villageof",
    "municipal",
    "publicworks",
    "public-works",
    "sanitation",
    "recycling",
    "solidwaste",
    "solid-waste",
    "chamber",
    "yellowpage",
    "superpages",
    "directory",
)



TV_STATION_IDS = {
    "wwrd",
    "know",
    "wabc",
    "wcbs",
    "wnbc",
}

BLOCKED_TITLE_PATTERNS = (
    r"\bcityplace\b",

    r"\bnew times\b",

    r"\btimes\b",

    r"\bgluten-free\b.*\bin\b",

    r"\btravel\b",

    r"\bfield guide\b",

    r"\btiktok\b",

    r"\buber eats\b",

    r"\bmenu\b.*\bubereats\b",

    r"\bnewspaper\b",

    r"\bchronicle\b",

    r"\bdaily chronicle\b",

    r"\bmagazine\b",

    r"\bcity of\b",
    r"\bcounty of\b",
    r"\btown of\b",
    r"\bvillage of\b",
    r"\bstate of\b",
    r"\bdepartment of\b",
    r"\bpublic works\b",
    r"\bsanitation department\b",
    r"\bwaste management authority\b",
    r"\bmunicipal\b",
    r"\bgovernment\b",
    r"\bchamber of commerce\b",
    r"\bofficial website\b",
    r"\bnews\b",
    r"\barticle\b",
    r"\bbest\b.*\bnear\b",
    r"\btop\b.*\bnear\b",
)


def normalize_domain(value: str) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""

    if raw.startswith("mailto:") or raw.startswith("tel:"):
        return raw.split(":", 1)[0]

    if "://" not in raw:
        raw_for_parse = "https://" + raw
    else:
        raw_for_parse = raw

    try:
        parsed = urlparse(raw_for_parse)
        host = parsed.netloc or parsed.path.split("/")[0]
    except Exception:
        host = raw

    host = host.lower().strip()
    host = host.split("@")[-1]
    host = host.split(":")[0]

    if host.startswith("www."):
        host = host[4:]

    return host.strip(".")


def domain_matches(domain: str, blocked: str) -> bool:
    domain = normalize_domain(domain)
    blocked = normalize_domain(blocked)

    if not domain or not blocked:
        return False

    return domain == blocked or domain.endswith("." + blocked)



CUSTOM_BLOCKED_DOMAINS_FILES = (
    Path("data/leadbot_blocked_domains.txt"),
    Path("exports/leadbot_blocked_domains.txt"),
)


def load_custom_blocked_domains() -> set[str]:
    blocked: set[str] = set()

    for path in CUSTOM_BLOCKED_DOMAINS_FILES:
        try:
            if not path.exists():
                continue

            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                value = normalize_domain(line.strip())
                if value and not value.startswith("#"):
                    blocked.add(value)
        except Exception:
            continue

    return blocked


def bad_lead_reason(domain: str = "", title: str = "", url: str = "") -> str:
    domain = normalize_domain(domain or url)
    title_l = str(title or "").strip().lower()
    url_l = str(url or "").strip().lower()

    if not domain:
        return "missing_domain"

    if domain in {"mailto", "tel"}:
        return "non_web_link"

    if domain.endswith(".gov"):
        return "government_domain"

    if domain.endswith(".edu"):
        return "education_domain"

    # Safer TV/radio/media rule:
    # Do NOT block only because a domain is 4 letters or starts with K/W.
    # Only block call-sign-looking domains when the title/url confirms media.
    if re.match(r"^[kw][a-z0-9]{3}\.(org|com|net|fm)$", domain):
        media_blob = " ".join([domain, title_l, url_l])
        if re.search(
            r"\b(radio|tv|television|npr|pbs|station|broadcast|public media|listen live|local news)\b",
            media_blob,
            flags=re.I,
        ):
            return "confirmed_media_call_sign_domain"


    # TV/radio station call-sign domains:
    # Examples: klcc.org, wttw.com, wabc.com, knbc.com
    # Most US broadcast call signs start with K or W and are 4 characters.
    call_sign_match = re.match(r"^[kw][a-z0-9]{3}\.(org|com|net|fm)$", domain)
    if call_sign_match:
        combined_media_check = " ".join([domain, title_l, url_l])
        if (
            domain.endswith(".org")
            or re.search(
                r"\b(radio|tv|television|news|npr|pbs|station|listen|broadcast|public media)\b",
                combined_media_check,
                flags=re.I,
            )
        ):
            return "media_call_sign_domain"

    if domain.startswith("visit") and domain.endswith(".com"):
        return "visit_site"


    for blocked in BLOCKED_EXACT_DOMAINS:
        if domain_matches(domain, blocked):
            return f"blocked_domain:{blocked}"

    combined = " ".join([domain, title_l, url_l])

    for station_id in TV_STATION_IDS:
        if re.search(r"\\b" + re.escape(station_id) + r"\\b", combined, flags=re.I):
            return f"tv_station:{station_id}"


    for part in BLOCKED_DOMAIN_PARTS:
        if part in combined:
            return f"blocked_pattern:{part}"

    for pattern in BLOCKED_TITLE_PATTERNS:
        if re.search(pattern, combined, flags=re.I):
            return f"blocked_title:{pattern}"

    return ""



# === LEADBOT BARE GEO DOMAIN FILTER START ===
def _leadbot_compact_geo_text(value: str = "") -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _leadbot_domain_root(domain_or_url: str = "") -> str:
    from urllib.parse import urlparse
    import re

    value = str(domain_or_url or "").strip().lower()
    if not value:
        return ""

    if "://" not in value:
        value = "http://" + value

    host = urlparse(value).netloc or urlparse(value).path
    host = host.split("@")[-1].split(":")[0]
    host = re.sub(r"^www\d*\.", "", host)
    host = host.strip(".")

    if not host or "." not in host:
        return ""

    parts = [p for p in host.split(".") if p]
    if len(parts) < 2:
        return ""

    # Basic registrable root: santabarbara.com -> santabarbara
    return parts[-2]


def _leadbot_market_geo_roots(market: str = "") -> set[str]:
    import re

    raw = str(market or "").strip().lower()
    raw = raw.replace("_", " ").replace("-", " ")
    raw = re.sub(r"[^a-z0-9\s]+", " ", raw)
    raw = " ".join(raw.split())

    if not raw:
        return set()

    state_words = {
        "alabama","alaska","arizona","arkansas","california","colorado","connecticut",
        "delaware","florida","georgia","hawaii","idaho","illinois","indiana","iowa",
        "kansas","kentucky","louisiana","maine","maryland","massachusetts","michigan",
        "minnesota","mississippi","missouri","montana","nebraska","nevada",
        "new hampshire","new jersey","new mexico","new york","north carolina",
        "north dakota","ohio","oklahoma","oregon","pennsylvania","rhode island",
        "south carolina","south dakota","tennessee","texas","utah","vermont",
        "virginia","washington","west virginia","wisconsin","wyoming"
    }

    state_abbrs = {
        "al","ak","az","ar","ca","co","ct","de","fl","ga","hi","id","il","in","ia",
        "ks","ky","la","me","md","ma","mi","mn","ms","mo","mt","ne","nv","nh","nj",
        "nm","ny","nc","nd","oh","ok","or","pa","ri","sc","sd","tn","tx","ut","vt",
        "va","wa","wv","wi","wy","dc"
    }

    cleaned = raw

    for state in sorted(state_words, key=len, reverse=True):
        cleaned = re.sub(r"\b" + re.escape(state) + r"\b", " ", cleaned)

    tokens = [t for t in cleaned.split() if t not in state_abbrs]
    city_region = " ".join(tokens).strip()

    roots = set()
    if city_region:
        roots.add(_leadbot_compact_geo_text(city_region))

    return {r for r in roots if len(r) >= 5}


def is_bare_geo_domain(domain: str = "", market: str = "") -> bool:
    """
    Reject domains that are only the geographic market root.

    Example:
      market='Santa Barbara CA'
      domain='santabarbara.com'
      => True

    But:
      santabarbarabakery.com
      santabarbaracakes.com
      cakesofsantabarbara.com
      => False
    """
    root = _leadbot_domain_root(domain)
    if not root:
        return False

    root_compact = _leadbot_compact_geo_text(root)
    geo_roots = _leadbot_market_geo_roots(market)

    if not geo_roots:
        return False

    return root_compact in geo_roots
# === LEADBOT BARE GEO DOMAIN FILTER END ===




# === LEADBOT OPENTABLE HARD BLOCK START ===
def _leadbot_is_opentable_domain(value: str = "") -> bool:
    from urllib.parse import urlparse
    import re

    raw = str(value or "").strip().lower()
    if not raw:
        return False

    if "://" not in raw:
        raw = "http://" + raw

    host = urlparse(raw).netloc or urlparse(raw).path
    host = host.split("@")[-1].split(":")[0]
    host = re.sub(r"^www\d*\.", "", host)
    host = host.strip(".")

    return host == "opentable.com" or host.endswith(".opentable.com")
# === LEADBOT OPENTABLE HARD BLOCK END ===

def is_bad_lead_domain(domain: str = "", title: str = "", url: str = "", market: str = "") -> bool:
    if market and (is_geo_only_domain_for_market(domain, market) or is_geo_only_domain_for_market(url, market)):
        return True
    if is_main_blocked_domain(domain) or is_main_blocked_domain(url):
        return True
    if _leadbot_is_opentable_domain(domain) or _leadbot_is_opentable_domain(url):
        return True
    if _leadbot_is_opentable_domain(domain) or _leadbot_is_opentable_domain(url):
        return True
    return bool(bad_lead_reason(domain=domain, title=title, url=url))
