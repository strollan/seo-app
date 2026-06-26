def clean_value(value):
    value = str(value or "").strip()
    if not value:
        return ""
    if value.lower() in {"not found", "none", "null", "nan", "n/a", "unknown"}:
        return ""
    return value


def build_lead_reason(row):
    serp_page = clean_value(row.get("serp_page"))
    serp_position = clean_value(row.get("serp_position"))
    phone = clean_value(row.get("best_phone") or row.get("phone"))
    emails = clean_value(row.get("emails") or row.get("email") or row.get("email_1"))
    contact_page = clean_value(row.get("contact_page_url") or row.get("contact_page"))
    confidence = clean_value(row.get("contact_confidence"))

    parts = []

    if serp_page and serp_position:
        parts.append(f"Ranks on page {serp_page} at position {serp_position}, so there is clear SEO upside.")
    elif serp_position:
        parts.append(f"Ranks around position {serp_position}, suggesting room to improve organic visibility.")

    if phone and emails:
        parts.append("Phone and email were found, giving you both call and email options.")
    elif phone:
        parts.append("Phone number found; this is a call-first opportunity.")
    elif emails:
        parts.append("Email found; outreach can start there.")
    else:
        parts.append("No direct phone or email was found, so contact details need manual review.")

    if contact_page:
        parts.append("Contact source found.")
    elif not phone and not emails:
        parts.append("No contact page was found during enrichment.")

    if confidence:
        parts.append(f"Contact confidence: {confidence}.")

    return " ".join(parts)
