from agents.lead_confidence_agent import calculate_contact_confidence
import csv
from agents.lead_email_cleaner_agent import clean_lead_emails
from datetime import datetime
from pathlib import Path


# === LEADBOT EXPORT NAME CLEANUP START ===
def leadbot_clean_export_part(value, fallback=""):
    import re

    value = str(value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")

    bad = {"", "none", "null", "nan", "undefined", "manual"}
    if value in bad:
        return fallback

    return value


def leadbot_build_export_slug(industry="", market="", keyword=""):
    industry = leadbot_clean_export_part(industry)
    market = leadbot_clean_export_part(market)
    keyword = leadbot_clean_export_part(keyword)

    parts = [p for p in [industry, market] if p]

    if not parts and keyword:
        parts = [keyword]

    if not parts:
        parts = ["general", "search"]

    return "_".join(parts)
# === LEADBOT EXPORT NAME CLEANUP END ===



EXPORT_FIELDS = [
    "industry",
    "market",
    "keyword",
    "title",
    "domain",
    "url",
    "serp_page",
    "serp_position",
    "seo_opportunity_score",
    "best_phone",
    "emails",
    "contact_page_url",
    "outreach_status",
    "score",
    "final_lead_score",
    "contact_confidence",
    "contact_flags",
    "reason",
]


GOOD_OUTREACH_STATUSES = {
    "email_and_call_ready",
    "call_ready",
    "email_ready",
}



def leadbot_export_clean_emails(value, lead):
    """
    Compatibility wrapper for clean_lead_emails().
    Keeps CSV export from crashing and keeps list emails clean.
    """
    import re

    if isinstance(value, (list, tuple, set)):
        raw = ", ".join(str(v or "").strip() for v in value if str(v or "").strip())
    else:
        raw = str(value or "").strip()

    found = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", raw)
    if found:
        clean = []
        seen = set()
        for email in found:
            email = email.strip().lower()
            if email and email not in seen:
                seen.add(email)
                clean.append(email)
        return ", ".join(clean)

    try:
        return clean_lead_emails(value)
    except Exception:
        return raw


def safe_slug(value):
    value = str(value or "").strip().lower()
    value = value.replace(" ", "_")
    return "".join(ch for ch in value if ch.isalnum() or ch in {"_", "-"})


def export_leads_to_csv(result, industry="", market="", only_outreach_ready=True, keyword=""):
    leads = result.get("leads", []) if isinstance(result, dict) else []

    if only_outreach_ready:
        leads = [
            lead for lead in leads
            if lead.get("outreach_status") in GOOD_OUTREACH_STATUSES
        ]

    export_dir = Path("exports")
    export_dir.mkdir(exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    industry_slug = safe_slug(industry or result.get("industry", "leads"))
    market_slug = safe_slug(market or result.get("market", "market"))

    path = export_dir / f"leads_{industry_slug}_{market_slug}_{stamp}.csv"

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=EXPORT_FIELDS)
        writer.writeheader()

        for lead in leads:
            if isinstance(lead, tuple):
                lead = next((item for item in lead if isinstance(item, dict)), None)
            if not isinstance(lead, dict):
                continue

            writer.writerow({
                "industry": lead.get("industry", industry or result.get("industry", "")),
                  "market": lead.get("market", market or result.get("market", "")),
                  "keyword": lead.get("keyword", keyword or result.get("keyword", "") or result.get("query", "")),
                  "title": lead.get("title", ""),
                "domain": lead.get("domain", ""),
                "url": lead.get("url", ""),
                "serp_page": lead.get("serp_page", ""),
                "serp_position": lead.get("serp_position", ""),
                "seo_opportunity_score": lead.get("seo_opportunity_score", ""),
                "best_phone": lead.get("best_phone", ""),
                "emails": leadbot_export_clean_emails(lead.get("emails") or lead.get("email") or "", lead),
                "contact_page_url": lead.get("contact_page_url", ""),
                "outreach_status": lead.get("outreach_status", ""),
                "score": lead.get("score", ""),
                "final_lead_score": lead.get("final_lead_score", ""),
                "contact_confidence": calculate_contact_confidence(lead),
                "contact_flags": ", ".join(lead.get("contact_flags", []) or []),
                "reason": lead.get("reason", ""),
            })

    return {
        "path": str(path),
        "count": len(leads),
    }
