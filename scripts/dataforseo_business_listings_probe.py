"""
DataForSEO Business Listings Probe

Purpose:
- Test DataForSEO Business Listings live endpoint
- Print names, addresses, phones, websites
- Does NOT modify exports
- Does NOT touch dashboard
- Does NOT use Google Cloud
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


ENDPOINT = "https://api.dataforseo.com/v3/business_data/business_listings/search/live"


def env_first(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value.strip()
    return ""


def auth_header() -> str:
    login = env_first("DATAFORSEO_LOGIN", "DATAFORSEO_USERNAME", "DATAFORSEO_USER")
    password = env_first("DATAFORSEO_PASSWORD", "DATAFORSEO_API_PASSWORD", "DATAFORSEO_PASS")

    if not login or not password:
        raise SystemExit(
            "Missing DataForSEO credentials. Expected DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD in .env/environment."
        )

    token = base64.b64encode(f"{login}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def post_business_listings(
    category: str,
    location_coordinate: str,
    title: str = "",
    description: str = "",
    limit: int = 10,
    is_claimed: str = "",
) -> dict:
    task: dict = {
        "categories": [category],
        "location_coordinate": location_coordinate,
        "limit": limit,
    }

    if title:
        task["title"] = title

    if description:
        task["description"] = description

    if is_claimed.lower() in {"true", "1", "yes"}:
        task["is_claimed"] = True
    elif is_claimed.lower() in {"false", "0", "no"}:
        task["is_claimed"] = False

    payload = json.dumps([task]).encode("utf-8")

    req = Request(
        ENDPOINT,
        data=payload,
        method="POST",
        headers={
            "Authorization": auth_header(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )

    try:
        with urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw)
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP ERROR {exc.code}\n{body}") from exc
    except URLError as exc:
        raise SystemExit(f"URL ERROR: {exc}") from exc


def dig(obj, *keys):
    cur = obj
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def pick_address(item: dict) -> str:
    # DataForSEO may return address as string or structured object depending on endpoint/version.
    for key in ["address", "full_address", "formatted_address"]:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    address_info = item.get("address_info")
    if isinstance(address_info, dict):
        parts = []
        for key in [
            "address",
            "street",
            "city",
            "region",
            "zip",
            "postal_code",
            "country_code",
        ]:
            value = address_info.get(key)
            if value:
                parts.append(str(value).strip())
        if parts:
            return ", ".join(parts)

    return ""


def pick_phone(item: dict) -> str:
    for key in ["phone", "phone_number", "main_phone"]:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    contacts = item.get("contacts")
    if isinstance(contacts, dict):
        for key in ["phone", "phone_numbers", "main_phone"]:
            value = contacts.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, list) and value:
                return str(value[0]).strip()

    return ""


def pick_website(item: dict) -> str:
    for key in ["url", "website", "domain", "site"]:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return ""


def extract_items(data: dict) -> list[dict]:
    tasks = data.get("tasks") or []
    items: list[dict] = []

    for task in tasks:
        results = task.get("result") or []
        for result in results:
            result_items = result.get("items") or []
            for item in result_items:
                if isinstance(item, dict):
                    items.append(item)

    return items


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--category", default="pizza_restaurant")
    parser.add_argument("--coord", default="34.052235,-118.243683,10")
    parser.add_argument("--title", default="")
    parser.add_argument("--description", default="")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--is-claimed", default="")
    parser.add_argument("--raw", action="store_true")
    args = parser.parse_args()

    print("===== REQUEST =====")
    print("category:", args.category)
    print("coord:", args.coord)
    print("title:", args.title or "(blank)")
    print("limit:", args.limit)

    data = post_business_listings(
        category=args.category,
        location_coordinate=args.coord,
        title=args.title,
        description=args.description,
        limit=args.limit,
        is_claimed=args.is_claimed,
    )

    if args.raw:
        print(json.dumps(data, indent=2)[:12000])

    print("\n===== API STATUS =====")
    print("status_code:", data.get("status_code"))
    print("status_message:", data.get("status_message"))
    print("cost:", data.get("cost"))

    items = extract_items(data)

    print("\n===== RESULTS =====")
    print("items:", len(items))

    found_address = 0

    for idx, item in enumerate(items[: args.limit], start=1):
        title = item.get("title") or item.get("name") or ""
        address = pick_address(item)
        phone = pick_phone(item)
        website = pick_website(item)
        rating = item.get("rating") or dig(item, "rating", "value") or ""

        if address:
            found_address += 1

        print("\n---", idx)
        print("TITLE:", title)
        print("ADDRESS:", address or "NOT FOUND")
        print("PHONE:", phone or "NOT FOUND")
        print("WEBSITE:", website or "NOT FOUND")
        print("RATING:", rating or "NOT FOUND")

        # Show useful keys so we can map exact response fields later.
        print("KEYS:", ", ".join(sorted(item.keys())[:40]))

    print("\n===== SUMMARY =====")
    print("items_checked:", min(len(items), args.limit))
    print("addresses_found:", found_address)


if __name__ == "__main__":
    main()
