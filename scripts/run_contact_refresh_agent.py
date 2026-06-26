#!/usr/bin/env python3
import argparse
import csv
import html as html_lib
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.lead_business_cache_agent import (
    get_refresh_candidates,
    mark_refresh_done,
    mark_refresh_error,
    mark_refresh_running,
    save_business_from_lead,
)


def fetch(url, timeout=10):
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
        with urlopen(req, timeout=timeout) as res:
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


def build_candidates(row):
    domain = str(row.get("domain") or "").strip()
    url = str(row.get("url") or "").strip()

    bases = []

    for value in [url, domain]:
        if not value:
            continue

        if "://" not in value:
            value = "https://" + value

        parsed = urlparse(value)
        host = parsed.netloc or parsed.path

        if not host:
            continue

        bases.append(value.rstrip("/"))
        bases.append(("https://" + host).rstrip("/"))
        bases.append(("http://" + host).rstrip("/"))

    clean = []
    seen = set()

    for base in bases:
        if base not in seen:
            seen.add(base)
            clean.append(base)

    candidates = []
    seen = set()

    for base in clean[:4]:
        for path in [
            "",
            "/contact",
            "/contact-us",
            "/contactus",
            "/about",
            "/about-us",
            "/locations",
            "/location",
            "/appointment",
        ]:
            candidate = (base + path).rstrip("/")
            if candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)

    return candidates


def refresh_business(row):
    domain = row.get("domain") or ""
    mark_refresh_running(domain)

    candidates = build_candidates(row)
    discovered = []
    seen = set(candidates)

    try:
        for candidate in candidates[:4]:
            html_text, final_url = fetch(candidate)

            if html_text:
                discovered.extend(discover_contact_links(html_text, final_url))

            phone, emails = extract_contact(html_text)

            if phone or emails:
                lead = dict(row)
                lead["best_phone"] = phone or row.get("best_phone") or ""
                lead["emails"] = ", ".join(emails) if emails else row.get("emails") or ""
                lead["contact_page_url"] = final_url or candidate
                lead["contact_confidence"] = 90 if phone and emails else 80
                lead["outreach_status"] = (
                    "email_and_call_ready" if phone and emails
                    else "call_ready" if phone
                    else "email_ready"
                )
                save_business_from_lead(lead, enriched=True)
                mark_refresh_done(domain)
                return True, lead

        for link in discovered:
            if link not in seen:
                seen.add(link)
                candidates.append(link)

        for candidate in candidates[4:20]:
            html_text, final_url = fetch(candidate)
            phone, emails = extract_contact(html_text)

            if phone or emails:
                lead = dict(row)
                lead["best_phone"] = phone or row.get("best_phone") or ""
                lead["emails"] = ", ".join(emails) if emails else row.get("emails") or ""
                lead["contact_page_url"] = final_url or candidate
                lead["contact_confidence"] = 90 if phone and emails else 80
                lead["outreach_status"] = (
                    "email_and_call_ready" if phone and emails
                    else "call_ready" if phone
                    else "email_ready"
                )
                save_business_from_lead(lead, enriched=True)
                mark_refresh_done(domain)
                return True, lead

        mark_refresh_done(domain)
        return False, row

    except Exception as e:
        mark_refresh_error(domain, e)
        return False, row


def main():
    parser = argparse.ArgumentParser(description="Refresh cached LeadBot business contact info.")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--stale-days", type=int, default=30)
    parser.add_argument("--csv", default="")
    args = parser.parse_args()

    rows = get_refresh_candidates(limit=args.limit, stale_days=args.stale_days)

    print(f"Contact Refresh Agent: candidates={len(rows)} limit={args.limit} stale_days={args.stale_days}")

    refreshed = 0
    still_missing = 0
    output_rows = []

    for i, row in enumerate(rows, start=1):
        domain = row.get("domain") or ""
        print(f"[{i}/{len(rows)}] Refreshing {domain}...")

        ok, updated = refresh_business(row)
        output_rows.append(updated)

        if ok:
            refreshed += 1
            print(f"  FOUND: {updated.get('best_phone') or ''} {updated.get('emails') or ''}")
        else:
            still_missing += 1
            print("  No contact update found.")

    print(f"Contact Refresh Agent done: refreshed={refreshed} still_missing={still_missing}")

    if args.csv:
        out = Path(args.csv)
        out.parent.mkdir(parents=True, exist_ok=True)

        fields = [
            "domain",
            "title",
            "url",
            "best_phone",
            "emails",
            "contact_page_url",
            "contact_confidence",
            "outreach_status",
            "last_enriched_at",
            "refresh_status",
            "last_refresh_error",
        ]

        with out.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()

            for row in output_rows:
                writer.writerow({field: row.get(field, "") for field in fields})

        print(f"Wrote refresh report: {out}")


if __name__ == "__main__":
    main()
