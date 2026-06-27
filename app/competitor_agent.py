import os
import re
from urllib.parse import urlparse

import requests


BLOCKED_DOMAINS = {
    "google.com",
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "yelp.com",
    "angi.com",
    "homeadvisor.com",
    "thumbtack.com",
    "yellowpages.com",
    "bbb.org",
    "mapquest.com",
    "wikipedia.org",
    "youtube.com",
}


def clean_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def is_valid_competitor(url: str, your_domain: str = "") -> bool:
    domain = clean_domain(url)

    if not domain:
        return False

    if your_domain and your_domain in domain:
        return False

    if any(blocked in domain for blocked in BLOCKED_DOMAINS):
        return False

    return True


def normalize_query(service: str, location: str = "") -> str:
    service = re.sub(r"\s+", " ", service or "").strip()
    location = re.sub(r"\s+", " ", location or "").strip()

    if location:
        return f"{service} {location}"
    return service


def find_competitors(
    your_site: str,
    service: str,
    location: str = "",
    limit: int = 5,
):
    enabled = os.getenv("GOOGLE_SEARCH_ENABLED", "0").strip()

    if enabled != "1":
        return {
            "enabled": False,
            "query": normalize_query(service, location),
            "competitors": [],
            "message": "Google competitor search is disabled.",
        }

    api_key = os.getenv("GOOGLE_API_KEY")
    cse_id = os.getenv("GOOGLE_CSE_ID")

    if not api_key or not cse_id:
        return {
            "enabled": False,
            "query": normalize_query(service, location),
            "competitors": [],
            "message": "Missing GOOGLE_API_KEY or GOOGLE_CSE_ID.",
        }

    query = normalize_query(service, location)
    your_domain = clean_domain(your_site)

    try:
        response = requests.get(
            "https://www.googleapis.com/customsearch/v1",
            params={
                "key": api_key,
                "cx": cse_id,
                "q": query,
                "num": 10,
            },
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()

    except Exception as e:
        return {
            "enabled": True,
            "query": query,
            "competitors": [],
            "message": f"Competitor search failed: {e}",
        }

    competitors = []
    seen_domains = set()

    for item in data.get("items", []):
        url = item.get("link", "")
        domain = clean_domain(url)

        if not is_valid_competitor(url, your_domain):
            continue

        if domain in seen_domains:
            continue

        seen_domains.add(domain)

        competitors.append({
            "title": item.get("title", ""),
            "url": url,
            "domain": domain,
            "snippet": item.get("snippet", ""),
        })

        if len(competitors) >= limit:
            break

    return {
        "enabled": True,
        "query": query,
        "competitors": competitors,
        "message": f"Found {len(competitors)} competitor candidates.",
    }


if __name__ == "__main__":
    result = find_competitors(
        your_site="example.com",
        service="plumber",
        location="nyc",
    )

    print(result)
