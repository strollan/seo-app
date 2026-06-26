from urllib.parse import urlparse


LEADBOT_BLOCKED_DOMAINS = {
    "theodysseyonline.com",
    "bringfido.com",
    "menupix.com",
    "allmenus.com",
    "sirved.com",
    "restaurantguru.com",
    "restaurantji.com",
    "thrillist.com",
    "eater.com",
    "onlyinyourstate.com",
    "timeout.com",
    "longislandrestaurants.com",
    "longislandrestaurantnews.com",
    "myglobalviewpoint.com",
    "mommypoppins.com",
    "discoverlongisland.com",
    "abc7ny.com",
    "weedmaps.com",
    "nypost.com",
    "theknot.com",
    "weddingwire.com",
    "zola.com",
    "eventective.com",
    "tripadvisor.com",
    "opentable.com",
    "resy.com",
    "toasttab.com",
    "clover.com",
    "square.site",
    "godaddy.com",
    "wixsite.com",
    "weebly.com",
    "business.site",
    "chamberofcommerce.com",
    "manta.com",
    "alignable.com",
    "nextdoor.com",
    "patch.com",
    "newsday.com",
    "nytimes.com",
    "reddit.com",
    "youtube.com",
    "tiktok.com",

    # News / content / blogs
    "dailyvoice.com",
    "substack.com",

    # Marketplaces / directories / review sites
    "houzz.com",
    "yelp.com",
    "angi.com",
    "angieslist.com",
    "thumbtack.com",
    "bbb.org",
    "yellowpages.com",
    "mapquest.com",

    # Social / profiles
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "x.com",
    "twitter.com",

    # Jobs
    "indeed.com",
    "ziprecruiter.com",
    "glassdoor.com",
    "monster.com",

    # Food ordering / delivery
    "doordash.com",
    "ubereats.com",
    "grubhub.com",
    "postmates.com",
    "seamless.com",

    # Business-for-sale junk
    "bizbuysell.com",
    "businessesforsale.com",
    "us.businessesforsale.com",

    # Government / public pages
    "suffolkcountyny.gov",
    "scnylegislature.us",
}


def normalize_domain(value):
    value = str(value or "").strip().lower()

    if not value:
        return ""

    if "://" in value:
        host = urlparse(value).netloc.lower()
    else:
        host = value.split("/")[0].lower()

    host = host.strip().replace("www.", "")

    return host


def is_blocked_lead_domain(domain_or_url):
    host = normalize_domain(domain_or_url)

    if not host:
        return False

    for blocked in all_blocked_domains():
        blocked = blocked.lower().replace("www.", "")
        if host == blocked or host.endswith("." + blocked):
            return True

    return False



# === LEADBOT DYNAMIC BLOCKLIST FILE START ===
from pathlib import Path as _LeadBotPath

LEADBOT_BLOCKLIST_FILE = _LeadBotPath("data/leadbot_blocklist.txt")


def load_dynamic_blocked_domains():
    try:
        if not LEADBOT_BLOCKLIST_FILE.exists():
            return set()

        domains = set()

        for line in LEADBOT_BLOCKLIST_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip().lower()
            if not line or line.startswith("#"):
                continue
            domains.add(normalize_domain(line))

        return {d for d in domains if d}
    except Exception:
        return set()


def all_blocked_domains():
    return set(LEADBOT_BLOCKED_DOMAINS) | load_dynamic_blocked_domains()
# === LEADBOT DYNAMIC BLOCKLIST FILE END ===


# === LEADBOT CONSOLIDATED MAIN BLOCK GATE START ===
from agents.leadbot_block_gate import (
    load_main_blocked_domains,
    add_main_blocked_domain,
    remove_main_blocked_domain,
    is_main_blocked_domain,
    lead_is_main_blocked,
)

def all_blocked_domains():
    return load_main_blocked_domains()

def load_dynamic_blocked_domains():
    return load_main_blocked_domains()

def is_blocked_lead_domain(domain_or_url):
    return is_main_blocked_domain(domain_or_url)

# === LEADBOT CONSOLIDATED MAIN BLOCK GATE END ===

