"""Private server-to-server bridge for the local ACT II CRM.

No browser code gets the shared secret. ACT II is expected to be listening
only on loopback; configuration deliberately defaults to disabled.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from urllib.parse import urlparse

import requests


def configured() -> bool:
    return bool(os.getenv("ACTII_INTEGRATION_SECRET", "").strip())


def import_lead(owner_id: str, source_ref: str, lead: dict) -> dict:
    secret = os.getenv("ACTII_INTEGRATION_SECRET", "").strip()
    if not secret:
        return {"ok": False, "status_code": 503, "error": "CRM import is unavailable: a secure ACT II connection has not been configured for this server. No contact was imported."}
    base_url = os.getenv("ACTII_INTEGRATION_URL", "http://127.0.0.1:8842").rstrip("/")
    parsed = urlparse(base_url)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        return {"ok": False, "status_code": 503, "error": "ACT II must be configured with a local loopback URL."}
    payload = json.dumps({"owner_id": owner_id, "source_ref": source_ref, "lead": lead}, separators=(",", ":")).encode("utf-8")
    timestamp = str(int(time.time()))
    signature = hmac.new(secret.encode("utf-8"), timestamp.encode("utf-8") + b"." + payload, hashlib.sha256).hexdigest()
    try:
        # Do not forward signed prospect data through environment proxies or redirects.
        with requests.Session() as session:
            session.trust_env = False
            response = session.post(
                f"{base_url}/internal/leadme/import",
                data=payload,
                headers={"Content-Type": "application/json", "X-ACTII-Timestamp": timestamp, "X-ACTII-Signature": signature},
                timeout=5,
                allow_redirects=False,
            )
            body = response.json()
    except requests.RequestException:
        return {"ok": False, "status_code": 503, "error": "The secure ACT II connection is unavailable. Please try again later."}
    except ValueError:
        return {"ok": False, "status_code": 502, "error": "ACT II returned an invalid response."}
    if not isinstance(body, dict):
        return {"ok": False, "status_code": 502, "error": "ACT II returned an invalid response."}
    if 200 <= response.status_code < 300 or body.get("ok"):
        contact_id = body.get("contact_id")
        expected_url = f"{base_url}/crm/contacts/{contact_id}"
        if (body.get("ok") is not True or not 200 <= response.status_code < 300
                or type(contact_id) is not int or contact_id <= 0
                or body.get("status") not in {"created", "existing"}
                or body.get("open_url") != expected_url):
            return {"ok": False, "status_code": 502, "error": "ACT II returned an invalid contact link."}
    body["status_code"] = response.status_code
    return body


def lead_payload(row: dict, source_file: str) -> dict:
    """One mapping for the preview and import; provenance comes from the owned CSV."""
    from agents.actii_search_metadata_agent import first, from_row, domain
    metadata = from_row(row)
    website = first(row, "website", "url", "domain")
    source_domain = domain(first(row, "domain", "website", "url"))
    notes = ["Imported from LeadMeLeads.", f"Original scan: {source_file}.",
             f"Domain: {source_domain}."]
    for label, value in (
        ("Lead score", metadata["lead_score"]),
        ("Industry/search", metadata["base_keyword"]),
        ("Search location", metadata["search_location"]),
        ("Source", first(row, "source", "lead_source_label")),
        ("SEO finding — page title", first(row, "page_title", "meta_title", "title_tag")),
        ("SEO finding — meta description", first(row, "meta_description", "page_description", "description") or "Missing"),
        ("SEO finding — H1", first(row, "h1", "h1_text", "page_h1")),
        ("SEO findings", first(row, "seo_findings", "seo_issues", "issues", "reason")),
    ):
        if value:
            notes.append(f"{label}: {value}.")
    return {
        "company_name": first(row, "business", "company_name", "business_name", "title", "domain"),
        "person_name": first(row, "contact_name", "person_name", "owner_name"),
        "domain": source_domain, "website": website,
        "phone": first(row, "best_phone", "phone", "phones", "phone_number", "primary_phone"),
        "email": first(row, "emails", "email") or ", ".join(filter(None, (first(row, "email_1"), first(row, "email_2")))),
        "address": first(row, "address", "business_address", "street_address", "full_address", "formatted_address", "place_address"),
        "industry": metadata["base_keyword"], "lead_score": metadata["lead_score"],
        "source_note": "\n".join(notes),
    }
