from pathlib import Path
import csv
import sys

# Allow LeadBot to read newer exports with very large scraped fields.
# Without this, CSV reads can fail with: field larger than field limit (131072).
csv.field_size_limit(sys.maxsize)
import html
from agents.lead_query_tracking_agent import normalize_query_tracking
from agents.lead_confidence_agent import calculate_contact_confidence
from urllib.parse import quote
from agents.lead_email_cleaner_agent import clean_email_field
from agents.lead_reason_agent import build_lead_reason


EXPORT_DIR = Path("exports")




# === LEADBOT CLEAN EXPORT DISPLAY NAME START ===
def leadbot_clean_export_display_name(name):
    import re

    display = str(name or "")
    display = display.replace("leads___", "leads_general_search_")
    display = display.replace("leads__", "leads_general_")
    display = display.replace("_manual_", "_")
    display = re.sub(r"_+", "_", display)
    display = display.replace("leads_", "")
    display = display.replace("_", " ")
    return display
# === LEADBOT CLEAN EXPORT DISPLAY NAME END ===

def exports_dir():
    EXPORT_DIR.mkdir(exist_ok=True)
    return EXPORT_DIR


# === LEADBOT EXPORT OWNER VISIBILITY START ===
def _leadbot_user_role(user):
    if isinstance(user, dict):
        return str(user.get("role") or "").strip().lower()
    return str(getattr(user, "role", "") or "").strip().lower()


def _leadbot_user_keys(user):
    keys = set()
    if not user:
        return keys

    if isinstance(user, dict):
        values = [user.get("username"), user.get("email"), user.get("id"), user.get("user_id")]
    else:
        values = [
            getattr(user, "username", None),
            getattr(user, "email", None),
            getattr(user, "id", None),
            getattr(user, "user_id", None),
        ]

    for value in values:
        clean = str(value or "").strip().lower()
        if clean:
            keys.add(clean)

    return keys


def _leadbot_collect_owner_values(value):
    out = set()

    if isinstance(value, dict):
        for key, item in value.items():
            k = str(key or "").strip().lower()
            if k in {"owner", "owner_email", "owner_username", "username", "email", "user", "user_id", "created_by", "created_by_email"}:
                clean = str(item or "").strip().lower()
                if clean:
                    out.add(clean)
            out |= _leadbot_collect_owner_values(item)

    elif isinstance(value, (list, tuple, set)):
        for item in value:
            out |= _leadbot_collect_owner_values(item)

    else:
        clean = str(value or "").strip().lower()
        if clean and ("@" in clean or clean.isdigit()):
            out.add(clean)

    return out


def _leadbot_export_owner_values(filename):
    import json
    import re
    from pathlib import Path

    name = Path(str(filename or "")).name
    out = set()

    candidate_names = [name]

    # Link base/enriched siblings:
    # leads_x.csv -> leads_x_enriched.csv or leads_x_enriched_YYYYMMDD_HHMMSS.csv
    # leads_x_enriched.csv -> leads_x.csv
    if name.endswith(".csv"):
        if "_enriched" in name:
            base_name = re.sub(r"_enriched(?:_\d{8}_\d{6})?\.csv$", ".csv", name)
            if base_name and base_name not in candidate_names:
                candidate_names.append(base_name)
        else:
            stem = name[:-4]
            candidate_names.append(f"{stem}_enriched.csv")
            try:
                for p in Path("exports").glob(f"{stem}_enriched*.csv"):
                    if p.name not in candidate_names:
                        candidate_names.append(p.name)
            except Exception:
                pass

    owner_map_path = Path("data/leadbot_export_owners.json")
    try:
        if owner_map_path.exists():
            data = json.loads(owner_map_path.read_text(encoding="utf-8") or "{}")
            if isinstance(data, dict):
                for candidate in candidate_names:
                    if candidate in data:
                        out |= _leadbot_collect_owner_values(data.get(candidate))
    except Exception:
        pass

    for candidate in candidate_names:
        sidecar = Path("exports") / f"{candidate}.owner.json"
        try:
            if sidecar.exists():
                data = json.loads(sidecar.read_text(encoding="utf-8") or "{}")
                out |= _leadbot_collect_owner_values(data)
        except Exception:
            pass

    return out


def _leadbot_export_visible_to_user(path, current_user=None):
    if not current_user:
        return False

    if _leadbot_user_role(current_user) == "admin":
        return True

    user_keys = _leadbot_user_keys(current_user)
    if not user_keys:
        return False

    owner_values = _leadbot_export_owner_values(Path(path).name)

    # Important privacy rule:
    # Standard users do NOT see legacy/unowned exports.
    if not owner_values:
        return False

    return bool(user_keys & owner_values)


def latest_csvs(current_user=None):
    """
    Return recent export CSVs visible to this user.

    Admin sees all exports.
    Standard users see only exports owned by them.
    Legacy/unowned exports are hidden from standard users.
    """
    import re

    files = sorted(
        exports_dir().glob("*.csv"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    files = [
        p for p in files
        if _leadbot_export_visible_to_user(p, current_user=current_user)
    ]

    chosen = {}
    ordered = []

    for path in files:
        stem = path.stem
        key = re.sub(r"_enriched_\d{8}_\d{6}$", "", stem)

        if key not in chosen:
            chosen[key] = path
            ordered.append(path)

    return ordered
# === LEADBOT EXPORT OWNER VISIBILITY END ===


def safe_export_file(filename):
    export_dir = exports_dir().resolve()
    candidate = (export_dir / Path(filename).name).resolve()

    if export_dir not in candidate.parents:
        return None

    if not candidate.exists() or candidate.suffix.lower() != ".csv":
        return None

    return candidate


def read_csv_rows(path, limit=100):
    if not path or not path.exists():
        return []

    rows = []

    try:
        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader):
                if i >= limit:
                    break
                rows.append(normalize_query_tracking(row, selected_name=path.name))
    except Exception:
        return []

    return rows


def get_value(row, *keys):
    for key in keys:
        value = row.get(key)
        if value:
            return str(value)
    return ""


def clean_seo_snapshot_title(value):
    """
    Display cleanup only.

    DataForSEO/SERP titles can sometimes contain phrases like:
    "Pearl's Bagels - There is no meta description"

    That phrase is not a page title. Keep the business/title part only.
    """
    import re as _re

    value = str(value or "").strip()
    value = _re.sub(r"\s+", " ", value).strip()

    patterns = [
        r"\s+[-–—]\s+there is no meta description\.?$",
        r"\s+[-–—]\s+no meta description\.?$",
        r"\s+[-–—]\s+missing meta description\.?$",
    ]

    for pattern in patterns:
        value = _re.sub(pattern, "", value, flags=_re.I).strip()

    return value


def is_missing_meta_description(value):
    import re as _re

    value = str(value or "").strip()
    if not value:
        return True

    normalized = _re.sub(r"\s+", " ", value).strip().lower()

    return normalized in {
        "no meta description",
        "there is no meta description",
        "missing meta description",
        "meta description missing",
        "none",
        "null",
        "nan",
        "not found",
    }


def link_html(url):
    safe = html.escape(url or "")
    if url and url.startswith(("http://", "https://")):
        return f'<a href="{safe}" target="_blank" rel="noopener">{safe}</a>'
    return safe or "Not found"


def export_display_label(path):
    """
    Show a human-friendly export label using CSV data when available.
    The href still uses the real filename.
    """
    rows = read_csv_rows(path, limit=1)
    row = rows[0] if rows else {}

    industry = get_value(row, "industry", "service", "service_keyword", "keyword")
    market = get_value(row, "market", "location", "city", "state", "region")
    business = get_value(row, "business", "title", "domain")

    parts = []
    if industry:
        parts.append(industry.replace("_", " ").title())
    if market:
        parts.append(market.replace("_", " ").title())

    if parts:
        label = " · ".join(parts)
        if business:
            label += f" — {business}"
        return label

    # Clean ugly legacy filenames for display only.
    name = path.name
    name = name.replace("leads_", "")
    name = name.replace("_manual_lead_", "_")
    name = name.replace("_manual_", "_")
    name = name.replace("_long_island_", "_")
    name = name.replace("_", " ")
    return name





# === LEADBOT SEARCH SUMMARY ROW START ===
def leadbot_search_summary_row(rows, selected_name=""):
    if not rows:
        return ""

    row = rows[0] if isinstance(rows[0], dict) else {}
    row = normalize_query_tracking(row, selected_name=selected_name)

    industry = get_value(row, "industry", "service", "business_type") or "—"
    keyword = get_value(row, "base_keyword", "keyword", "service_keyword") or "—"
    market = get_value(row, "market", "location", "city", "region") or "—"

    def clean_words(value):
        import re
        value = str(value or "").replace("_", " ").replace("-", " ")
        value = re.sub(r"\.csv$", "", value, flags=re.I)
        value = re.sub(r"\b(leads|desktop|enriched)\b", " ", value, flags=re.I)
        value = re.sub(r"\s+20\d{6}\s+\d{6}\s*$", "", value)
        value = re.sub(r"\s+", " ", value).strip()
        return value

    def pretty_market(value):
        value = clean_words(value)
        if not value or value == "—":
            return value or "—"

        parts = []
        for part in value.split():
            if len(part) == 2 and part.isalpha():
                parts.append(part.upper())
            else:
                parts.append(part[:1].upper() + part[1:].lower())
        return " ".join(parts)

    def strip_market_from_keyword(keyword_value, market_value):
        import re
        kw = clean_words(keyword_value)
        mk = clean_words(market_value)

        if mk and mk != "—":
            kw = re.sub(r"\s+" + re.escape(mk) + r"$", "", kw, flags=re.I).strip()

        return kw or keyword_value or "—"

    # Main fix:
    # keyword may arrive as "cake madison wi 20260612 173024"
    # market may arrive as "madison wi"
    # final should be keyword="cake", market="Madison WI"
    market = pretty_market(market)
    keyword = strip_market_from_keyword(keyword, market)

    serp_pages = []
    for item in rows:
        if not isinstance(item, dict):
            continue

        page = get_value(item, "serp_page", "page", "page_number", "result_page", "google_page", "rank_page")
        page = str(page or "").strip()

        if page and page.lower() not in {"manual", "?", "not found", "none", "null", "nan"} and page not in serp_pages:
            serp_pages.append(page)

    def page_sort_key(value):
        try:
            return int(value)
        except Exception:
            return 9999

    serp_pages = sorted(serp_pages, key=page_sort_key)
    pages_label = " + ".join(serp_pages) if serp_pages else "—"

    return f"""
    <div class="leadbot-search-summary-row">
        <div class="leadbot-search-summary-title">Current Search</div>
        <div class="leadbot-search-summary-line">
            <span><b>Industry</b> {html.escape(str(industry))}</span>
            <span><b>Keyword</b> {html.escape(str(keyword))}</span>
            <span><b>Market</b> {html.escape(str(market))}</span>
            <span><b>Leads</b> {html.escape(str(len(rows)))}</span>
            <span><b>Pages</b> {html.escape(str(pages_label))}</span>
        </div>
    </div>
    """
# === LEADBOT SEARCH SUMMARY ROW END ===



def lead_cards(rows, selected_name=""):
    # === LEADBOT FILTER BLOCKED LEADS ON DASHBOARD RENDER START ===
    # Block button must survive refresh:
    # existing CSV rows stay on disk, but blocked domains should not render again.
    try:
        from agents.leadbot_block_gate import lead_is_main_blocked
        import os
        import re

        def _leadbot_market_from_selected_name(value):
            raw = os.path.basename(str(value or ""))
            raw = re.sub(r"\.csv$", "", raw, flags=re.I)
            raw = re.sub(r"^leads[_\s-]+", "", raw, flags=re.I)
            raw = re.sub(r"[_-]+", " ", raw)
            raw = re.sub(r"\b(desktop|enriched)\b", "", raw, flags=re.I)
            raw = re.sub(r"\s+20\d{6}\s+\d{6}\s*$", "", raw)
            raw = " ".join(raw.split()).strip()

            states = {
                "al","ak","az","ar","ca","co","ct","de","fl","ga","hi","ia","id","il","in",
                "ks","ky","la","ma","md","me","mi","mn","mo","ms","mt","nc","nd","ne","nh",
                "nj","nm","nv","ny","oh","ok","or","pa","ri","sc","sd","tn","tx","ut","va",
                "vt","wa","wi","wv","wy","dc"
            }

            city_prefixes = {"santa","san","los","las","long","new","saint","st","fort","port","mount"}
            city_suffixes = {
                "beach","grove","springs","falls","city","heights","park","point","harbor",
                "island","islands","bay","lake","lakes","ridge","valley","hills","gardens",
                "creek","river","shore","shores","village","port","fort"
            }

            parts = raw.split()
            state_index = -1
            state = ""

            for i in range(len(parts) - 1, -1, -1):
                if parts[i].lower() in states:
                    state_index = i
                    state = parts[i].upper()
                    break

            if state_index < 1:
                return ""

            before_state = parts[:state_index]
            last = before_state[-1].lower()
            second_last = before_state[-2].lower() if len(before_state) >= 2 else ""

            if (second_last in city_prefixes or last in city_suffixes) and len(before_state) >= 3:
                city_words = before_state[-2:]
            else:
                city_words = before_state[-1:]

            city = " ".join(w[:1].upper() + w[1:].lower() for w in city_words)
            return (city + " " + state).strip()

        render_market = _leadbot_market_from_selected_name(selected_name)

        if rows:
            filtered_rows = []
            for row in rows:
                check_row = row

                if render_market and isinstance(row, dict):
                    check_row = dict(row)
                    if not (check_row.get("market") or check_row.get("location") or check_row.get("city") or check_row.get("region")):
                        check_row["market"] = render_market

                if not lead_is_main_blocked(check_row):
                    filtered_rows.append(row)

            rows = filtered_rows

    except Exception as exc:
        print(f"LEADBOT DASHBOARD BLOCK FILTER ERROR: {exc}", flush=True)
    # === LEADBOT FILTER BLOCKED LEADS ON DASHBOARD RENDER END ===

    if not rows:
        return '<div class="empty">No leads found yet. Run the bot or select an export.</div>'

    cards = []

    for row in rows:
        title = get_value(row, "business", "title", "domain") or "Unknown Business"
        domain = get_value(row, "domain")
        website = get_value(row, "website", "url")
        phone = get_value(row, "best_phone")

        emails = get_value(row, "emails")
        if not emails:
            emails = ", ".join(
                x for x in [get_value(row, "email_1"), get_value(row, "email_2")]
                if x
            )

        emails = clean_email_field(emails)

        contact_page = get_value(row, "contact_page", "contact_page_url")

        address = (
            get_value(row, "address")
            or get_value(row, "full_address")
            or get_value(row, "business_address")
            or get_value(row, "formatted_address")
            or get_value(row, "street_address")
            or get_value(row, "place_address")
            or get_value(row, "location")
            or "Not found"
        )
        outreach = get_value(row, "outreach_status").replace("_", " ").title() or "Unknown"

        serp_page = get_value(
            row,
            "serp_page",
            "page",
            "page_number",
            "result_page",
            "google_page",
            "rank_page",
        )
        serp_pos = get_value(
            row,
            "serp_position",
            "position",
            "pos",
            "rank",
            "rank_position",
            "google_position",
            "result_position",
        )

        def real_serp_value(value):
            value = str(value or "").strip()
            if not value:
                return ""
            if value.lower() in {"manual", "?", "not found", "none", "null", "nan"}:
                return ""
            return value

        serp_page = real_serp_value(serp_page)
        serp_pos = real_serp_value(serp_pos)

        def real_serp_value(value):
            value = str(value or "").strip()
            if not value:
                return ""
            if value.lower() in {"manual", "?", "not found", "none", "null", "nan"}:
                return ""
            return value

        serp_page = real_serp_value(serp_page)
        serp_pos = real_serp_value(serp_pos)

        serp_badge_html = ""
        if serp_page and serp_pos:
            serp_badge_html = f"<span>Page {html.escape(serp_page)} · Position {html.escape(serp_pos)}</span>"

        score = get_value(row, "final_lead_score", "seo_opportunity_score", "score") or "—"
        confidence = str(calculate_contact_confidence(row))
        address = get_value(row, "address", "business_address", "street_address", "full_address", "formatted_address") or ""
        reason = build_lead_reason(row)

        page_title = get_value(row, "page_title", "meta_title", "title") or title
        meta_description = get_value(
            row,
            "meta_description",
            "page_description",
            "meta_desc",
            "description",
            "og_description",
            "twitter_description",
        )
        h1 = get_value(row, "h1", "h1_text", "page_h1")

        seo_snapshot_items = []

        if page_title:
            page_title = clean_seo_snapshot_title(page_title)
            seo_snapshot_items.append(
                '<div class="leadbot-seo-snapshot-item">'
                '<b>Site Title</b>'
                f'<p>{html.escape(page_title)}</p>'
                '</div>'
            )

        if is_missing_meta_description(meta_description):
            seo_snapshot_items.append(
                '<div class="leadbot-seo-snapshot-item leadbot-seo-snapshot-missing-item">'
                '<b>Meta Description</b>'
                '<p><span class="leadbot-missing-pill">Missing</span></p>'
                '</div>'
            )
        else:
            seo_snapshot_items.append(
                '<div class="leadbot-seo-snapshot-item">'
                '<b>Meta Description</b>'
                f'<p>{html.escape(meta_description)}</p>'
                '</div>'
            )

        if h1:
            seo_snapshot_items.append(
                '<div class="leadbot-seo-snapshot-item">'
                '<b>H1</b>'
                f'<p>{html.escape(h1)}</p>'
                '</div>'
            )

        if seo_snapshot_items:
            seo_snapshot_html = (
                '<div class="leadbot-seo-snapshot">'
                '<strong>SEO Snapshot</strong>'
                + ''.join(seo_snapshot_items)
                + '</div>'
            )
        else:
            seo_snapshot_html = ""

        delete_html = ""
        if selected_name and selected_name != "leadbot_master.csv" and domain:
            safe_file = html.escape(selected_name)
            safe_domain = html.escape(domain)
            delete_html = (
                f'<a class="lead-delete-one" title="Delete lead" '
                f'href="/lead-bot/delete-row/{safe_file}?domain={safe_domain}" '
                f'onclick="return confirm(&quot;Delete this lead from this export?&quot;);">Delete</a>'
            )

        cards.append(f"""
        <article class="lead-card">
            {delete_html}
            <div class="lead-head">
                <div>
                    <h3>{html.escape(title)}</h3>
                    <p class="domain">{html.escape(domain)}</p>
                </div>
                <div class="score">{html.escape(score)}</div>
            </div>

            <div class="badges">
                <span>{html.escape(outreach)}</span>
                {serp_badge_html}
                <span>Contact Confidence {html.escape(confidence)}</span>
                <span>Lead Score {html.escape(score)}</span>
            </div>

            <form class="lead-contact-edit-form" method="post" action="/lead-bot/save-details">
                <input type="hidden" name="filename" value="{html.escape(selected_name or '')}">
                <input type="hidden" name="domain" value="{html.escape(domain or '')}">

                <div class="info-grid lead-edit-grid">
                    <div>
                        <b>Phone</b>
                        <input name="phone" value="{html.escape((row.get('phone') or row.get('phones') or row.get('phone_number') or row.get('phone_numbers') or row.get('primary_phone') or row.get('telephone') or row.get('tel') or phone or ''))}" placeholder="Phone">
                    </div>
                    <div>
                        <b>Email</b>
                        <input name="email" value="{html.escape((emails or row.get('email') or row.get('emails') or ''))}" placeholder="Email">
                    </div>
                    <div>
                        <b>Website</b>
                        <input name="website" value="{html.escape(website or '')}" placeholder="Website URL">
                    </div>
                    <div>
                        <b>Contact Page</b>
                        <input name="contact_page" value="{html.escape(contact_page or '')}" placeholder="Contact page or mailto link">
                    </div>
                </div>

                <button class="lead-contact-save" type="submit">Save Details</button>
            </form>

            <div class="lead-address-box">
                <b>Address</b>
                <form method="post" action="/lead-bot/update-address">
                    <input type="hidden" name="filename" value="{html.escape(selected_name or '')}">
                    <input type="hidden" name="domain" value="{html.escape(domain or '')}">
                    <input name="address" value="{html.escape((row.get('address') or row.get('full_address') or row.get('business_address') or row.get('formatted_address') or row.get('street_address') or row.get('mailing_address') or (address if address != 'Not found' else '') or ''))}" placeholder="Add street address, city, state, ZIP">
                    <button type="submit">Save Address</button>
                </form>
            </div>

            {seo_snapshot_html}

            <div class="reason">
                <b>Why this lead</b>
                <p>{html.escape(reason)}</p>
            </div>
        </article>
        """)

    return "".join(cards)






# === LEADBOT DATAFORSEO ADMIN UI FILTER START ===
def _leadbot_user_is_admin(current_user=None):
    if isinstance(current_user, dict):
        return str(current_user.get("role") or "").strip().lower() == "admin"
    return str(getattr(current_user, "role", "") or "").strip().lower() == "admin"


def _leadbot_remove_dataforseo_ui_for_non_admin(page, current_user=None):
    """
    Remove DataForSEO sidebar controls from rendered LeadBot HTML for non-admins.
    Backend admin checks still remain the real security control.
    """
    if _leadbot_user_is_admin(current_user):
        return page

    import re

    text = str(page or "")

    # Remove the exact official DataForSEO widget block.
    text = re.sub(
        r'\s*<!--\s*LEADBOT SINGLE DATAFORSEO BUTTON START\s*-->.*?<!--\s*LEADBOT SINGLE DATAFORSEO BUTTON END\s*-->\s*',
        '\n',
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # Backup cleanup for the actual IDs/classes in the widget.
    text = re.sub(
        r'\s*<div[^>]*id="leadbotDataForSeoSingleWrap"[^>]*>.*?</div>\s*',
        '\n',
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )

    text = re.sub(
        r'\s*<button[^>]*id="leadbotDataForSeoSingleBtn"[^>]*>.*?</button>\s*',
        '\n',
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )

    text = re.sub(
        r'\s*<[^>]+class="[^"]*leadbot-dataforseo[^"]*"[^>]*>.*?</[^>]+>\s*',
        '\n',
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )

    return text
# === LEADBOT DATAFORSEO ADMIN UI FILTER END ===


def render_lead_dashboard(file="", current_user=None):
    files = latest_csvs(current_user=current_user)

    selected = safe_export_file(file) if file else None
    if selected and not _leadbot_export_visible_to_user(selected, current_user=current_user):
        selected = None
        file = ""

    if not selected and files:
        selected = files[0]

    rows = read_csv_rows(selected) if selected else []

    file_links = []
    for f in files[:20]:
        active = " active" if selected and selected.name == f.name else ""
        label = export_display_label(f)
        safe_name = html.escape(f.name)
        safe_label = html.escape(label)
        encoded_name = quote(f.name)

        file_links.append(
            f'<div class="export-file-row{active}">'
            f'<a class="file-link{active}" title="{safe_name}" href="/lead-bot?file={encoded_name}">{safe_label}</a>'
            f'<a class="export-csv-link" title="Download CSV" href="/lead-bot/export/{encoded_name}">CSV</a>'
            f'</div>'
        )

    selected_name = selected.name if selected else "No export selected"

    download = ""
    append_button_html = ""
    if selected:
        if selected.name != "leadbot_master.csv":
            append_button_html = f'<button type="submit" name="append_to" value="{html.escape(selected.name)}">Append 8 More</button>' 
        download = (
        f'<a class="btn dark" href="/lead-bot/export/{html.escape(selected.name)}">Download This Scan</a>'
    )

    page = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>LeadBot Dashboard</title>
<style>
* { box-sizing: border-box; }
body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
    background: #eef3fb;
    color: #0f172a;
}
.container {
    max-width: 1440px;
    margin: auto;
    padding: 28px;
}
.hero {
    background: linear-gradient(135deg, #07152f, #1e3a8a);
    color: white;
    border-radius: 24px;
    padding: 30px;
    margin-bottom: 22px;
    box-shadow: 0 18px 48px rgba(15,23,42,.18);
}

.leadbot-brand {
    display: flex;
    align-items: center;
    gap: 18px;
}

.leadbot-logo-wrap {
    background: rgba(255,255,255,.08);
    border: 1px solid rgba(255,255,255,.18);
    border-radius: 16px;
    padding: 10px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
}

.leadbot-logo {
    height: 64px;
    width: auto;
    display: block;
}


.leadbot-brand {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 22px;
    flex-wrap: wrap;
}

.leadbot-brand-left {
    display: flex;
    align-items: center;
    gap: 20px;
}

.leadbot-logo-link {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 0;
    border-radius: 0;
    background: transparent;
    border: none;
    box-shadow: none;
    text-decoration: none;
}

.leadbot-logo {
    height: 72px;
    width: auto;
    display: block;
}

.leadbot-nav {
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
    align-items: center;
}

.leadbot-nav a {
    color: white;
    text-decoration: none;
    font-weight: 900;
    background: rgba(255,255,255,.14);
    border: 1px solid rgba(255,255,255,.22);
    padding: 10px 14px;
    border-radius: 999px;
}

.leadbot-nav a:hover {
    background: rgba(255,255,255,.25);
}

html {
    scroll-behavior: smooth;
}

.hero h1 {
    margin: 0 0 8px;
    font-size: 36px;
    letter-spacing: -.03em;
}
.hero p {
    margin: 0;
    opacity: .88;
}
.layout {
    display: grid;
    grid-template-columns: 340px minmax(0, 1fr);
    gap: 20px;
}
.panel {
    background: white;
    border: 1px solid #dbe4f0;
    border-radius: 20px;
    padding: 20px;
    box-shadow: 0 10px 28px rgba(15,23,42,.07);
}
.panel h2 {
    margin: 0 0 14px;
}
label {
    display: block;
    margin: 13px 0 6px;
    font-size: 12px;
    font-weight: 900;
    color: #475569;
    text-transform: uppercase;
    letter-spacing: .06em;
}
input {
    width: 100%;
    border: 1px solid #cbd5e1;
    border-radius: 12px;
    padding: 11px 12px;
    font-size: 15px;
}
.btn, button {
    display: inline-block;
    border: 0;
    border-radius: 12px;
    background: #1e3a8a;
    color: white;
    padding: 12px 15px;
    margin-top: 16px;
    font-weight: 900;
    text-decoration: none;
    cursor: pointer;
}
.btn.dark {
    background: #0f172a;
}
.help {
    color: #64748b;
    font-size: 13px;
    line-height: 1.45;
}
.file-list {
    display: grid;
    gap: 8px;
    max-height: 360px;
    overflow: auto;
}
.file-link {
    display: block;
    padding: 10px;
    border: 1px solid #e2e8f0;
    border-radius: 12px;
    background: #f8fafc;
    color: #1e3a8a;
    text-decoration: none;
    font-weight: 800;
    font-size: 13px;
    word-break: break-all;
}
.file-link.active {
    background: #eaf2ff;
    border-color: #93c5fd;
}
.results-top {
    display: flex;
    justify-content: space-between;
    gap: 16px;
    align-items: flex-start;
    margin-bottom: 16px;
}
.results-top h2 {
    margin: 0;
    font-size: 22px;
}
.leads {
    display: grid;
    gap: 16px;
}
.lead-card {
    border: 1px solid #dbe4f0;
    border-radius: 18px;
    background: white;
    padding: 20px;
    box-shadow: 0 8px 22px rgba(15,23,42,.06);
}
.lead-head {
    display: flex;
    justify-content: space-between;
    gap: 18px;
}
.lead-card h3 {
    margin: 0;
    font-size: 21px;
    line-height: 1.25;
}
.domain {
    margin: 6px 0 0;
    color: #64748b;
    font-weight: 800;
}
.score {
    min-width: 58px;
    height: 58px;
    border-radius: 999px;
    display: grid;
    place-items: center;
    background: #dcfce7;
    color: #166534;
    font-size: 19px;
    font-weight: 950;
}
.badges {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin: 16px 0;
}
.badges span {
    background: #eaf2ff;
    color: #1e3a8a;
    border: 1px solid #bfdbfe;
    padding: 7px 10px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 900;
}
.info-grid {
    display: grid;
    grid-template-columns: 150px 230px minmax(240px, 1fr) minmax(240px, 1fr);
    gap: 12px;
}
.info-grid div {
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 14px;
    padding: 12px;
    min-width: 0;
}
.info-grid b {
    display: block;
    margin-bottom: 6px;
    color: #334155;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: .05em;
}
.info-grid p {
    margin: 0;
    overflow-wrap: anywhere;
}
.info-grid a {
    color: #1e3a8a;
    font-weight: 800;
}
.reason {
    margin-top: 14px;
    padding: 12px 14px;
    border-left: 5px solid #1e3a8a;
    background: #f8fafc;
    border-radius: 12px;
}
.reason b {
    display: block;
    margin-bottom: 5px;
}
.reason p {
    margin: 0;
    line-height: 1.55;
    color: #334155;
}
.empty {
    padding: 28px;
    border: 1px dashed #cbd5e1;
    background: #f8fafc;
    border-radius: 14px;
    color: #64748b;
}
@media (max-width: 1100px) {
    .layout { grid-template-columns: 1fr; }
    .info-grid { grid-template-columns: 1fr 1fr; }
}
@media (max-width: 720px) {
    .lead-head { flex-direction: column; }
    .info-grid { grid-template-columns: 1fr; }
}

.leadbot-logo-link {
    display: inline-flex !important;
    align-items: center !important;
    gap: 10px !important;
    padding: 0 !important;
    border: none !important;
    background: transparent !important;
    box-shadow: none !important;
    text-decoration: none !important;
}

.leadbot-logo-icon {
    width: 34px;
    height: 34px;
    display: block;
}

.leadbot-logo-text {
    color: #ffffff;
    font-size: 28px;
    font-weight: 950;
    letter-spacing: -.04em;
    line-height: 1;
}

.leadbot-logo-text strong {
    color: #a78bfa;
}


/* === LEAD BOT HEADER / NAV / SCORE CLEANUP === */
.leadbot-brand {
    display: flex !important;
    align-items: center !important;
    justify-content: space-between !important;
    gap: 22px !important;
    flex-wrap: wrap !important;
}

.leadbot-brand-left {
    display: flex !important;
    align-items: center !important;
    gap: 20px !important;
}

.leadbot-logo-wrap {
    display: none !important;
}

.leadbot-logo-link {
    display: inline-flex !important;
    align-items: center !important;
    gap: 10px !important;
    padding: 0 !important;
    border: none !important;
    background: transparent !important;
    box-shadow: none !important;
    text-decoration: none !important;
}

.leadbot-logo-icon {
    width: 34px !important;
    height: 34px !important;
    display: block !important;
}

.leadbot-logo-text {
    color: #ffffff !important;
    font-size: 28px !important;
    font-weight: 950 !important;
    letter-spacing: -.04em !important;
    line-height: 1 !important;
}

.leadbot-logo-text strong {
    color: #a78bfa !important;
}

.leadbot-nav {
    display: flex !important;
    gap: 10px !important;
    flex-wrap: wrap !important;
    align-items: center !important;
}

.leadbot-nav a {
    color: white !important;
    text-decoration: none !important;
    font-weight: 900 !important;
    background: rgba(255,255,255,.14) !important;
    border: 1px solid rgba(255,255,255,.22) !important;
    padding: 10px 14px !important;
    border-radius: 999px !important;
}

.leadbot-nav a:hover {
    background: rgba(255,255,255,.25) !important;
}

.score {
    display: none !important;
}

html {
    scroll-behavior: smooth;
}


/* === STANDARD LEAD BOT LOGO RESET === */
.leadbot-logo-wrap {
    display: none !important;
}

.leadbot-logo-link {
    display: inline-flex !important;
    align-items: center !important;
    justify-content: center !important;
    padding: 0 !important;
    margin: 0 !important;
    border: none !important;
    border-radius: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
    text-decoration: none !important;
}

.leadbot-logo {
    height: 72px !important;
    width: auto !important;
    display: block !important;
}

.leadbot-logo-icon,
.leadbot-logo-text {
    display: none !important;
}



/* === LEADBOT CSS SCANNER LOADER START === */
.leadbot-wait-video {
    position: fixed;
    right: 22px;
    bottom: 22px;
    width: 340px;
    background: #07152f;
    color: #ffffff;
    border: 1px solid rgba(255,255,255,.18);
    border-radius: 20px;
    box-shadow: 0 22px 55px rgba(15,23,42,.42);
    overflow: hidden;
    z-index: 9999;
    display: none;
}

.leadbot-wait-video.is-active {
    display: block;
}

.leadbot-scanner {
    position: relative;
    height: 180px;
    overflow: hidden;
    background:
        radial-gradient(circle at 18% 20%, rgba(96,165,250,.36), transparent 28%),
        radial-gradient(circle at 82% 78%, rgba(167,139,250,.30), transparent 32%),
        linear-gradient(135deg, #07152f, #0f172a 48%, #1e3a8a);
}

.leadbot-grid {
    position: absolute;
    inset: 0;
    background-image:
        linear-gradient(rgba(255,255,255,.08) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,.08) 1px, transparent 1px);
    background-size: 26px 26px;
    opacity: .32;
    animation: leadbotGridMove 4s linear infinite;
}

.leadbot-scan-line {
    position: absolute;
    left: -20%;
    top: 0;
    width: 34%;
    height: 100%;
    background: linear-gradient(90deg, transparent, rgba(96,165,250,.34), rgba(255,255,255,.48), transparent);
    transform: skewX(-14deg);
    animation: leadbotSweep 2.2s ease-in-out infinite;
}

.leadbot-node {
    position: absolute;
    width: 10px;
    height: 10px;
    border-radius: 999px;
    background: #93c5fd;
    box-shadow: 0 0 18px rgba(147,197,253,.95);
    animation: leadbotPulse 1.4s ease-in-out infinite;
}

.leadbot-node.one { left: 58px; top: 48px; animation-delay: 0s; }
.leadbot-node.two { left: 166px; top: 92px; animation-delay: .22s; }
.leadbot-node.three { right: 54px; top: 56px; animation-delay: .44s; }
.leadbot-node.four { right: 92px; bottom: 40px; animation-delay: .66s; }

.leadbot-connection {
    position: absolute;
    height: 2px;
    width: 180px;
    left: 72px;
    top: 76px;
    background: linear-gradient(90deg, transparent, rgba(147,197,253,.8), transparent);
    transform: rotate(15deg);
    opacity: .75;
    animation: leadbotConnection 1.8s ease-in-out infinite;
}

.leadbot-mini-card {
    position: absolute;
    left: 28px;
    bottom: 24px;
    width: 118px;
    padding: 10px;
    border-radius: 14px;
    background: rgba(255,255,255,.10);
    border: 1px solid rgba(255,255,255,.18);
    backdrop-filter: blur(8px);
}

.leadbot-mini-card span {
    display: block;
    height: 7px;
    border-radius: 999px;
    background: rgba(255,255,255,.72);
    margin-bottom: 7px;
}

.leadbot-mini-card span:nth-child(2) {
    width: 72%;
    opacity: .7;
}

.leadbot-mini-card span:nth-child(3) {
    width: 46%;
    opacity: .55;
    margin-bottom: 0;
}

.leadbot-wait-copy {
    padding: 15px 17px 17px;
    background: rgba(15,23,42,.82);
}

.leadbot-wait-copy strong {
    display: block;
    font-size: 16px;
    margin-bottom: 6px;
}

.leadbot-wait-copy span {
    display: block;
    font-size: 13px;
    color: rgba(255,255,255,.78);
    line-height: 1.45;
}

@keyframes leadbotSweep {
    0% { left: -38%; opacity: .15; }
    35% { opacity: .9; }
    100% { left: 104%; opacity: .2; }
}

@keyframes leadbotPulse {
    0%, 100% { transform: scale(.88); opacity: .55; }
    50% { transform: scale(1.55); opacity: 1; }
}

@keyframes leadbotGridMove {
    from { background-position: 0 0; }
    to { background-position: 26px 26px; }
}

@keyframes leadbotConnection {
    0%, 100% { opacity: .28; transform: rotate(15deg) scaleX(.7); }
    50% { opacity: .9; transform: rotate(15deg) scaleX(1); }
}

@media (max-width: 720px) {
    .leadbot-wait-video {
        left: 14px;
        right: 14px;
        width: auto;
    }
}
/* === LEADBOT CSS SCANNER LOADER END === */



/* === LEAD BOT EXPORT TITLE CLEANUP START === */
.results-top h2 {
    font-size: 16px !important;
    line-height: 1.35 !important;
    font-weight: 850 !important;
    max-width: 680px !important;
    overflow-wrap: anywhere !important;
    color: #0f172a !important;
}

.results-top .help {
    margin-top: 6px !important;
}

.btn.dark {
    white-space: nowrap !important;
}
/* === LEAD BOT EXPORT TITLE CLEANUP END === */


/* === LEADBOT MASTER EXPORT UI START === */
.results-top .btn,
.results-top a.btn {
    font-size: 13px !important;
    padding: 10px 13px !important;
}

.results-top h2 {
    font-size: 16px !important;
    overflow-wrap: anywhere !important;
}
/* === LEADBOT MASTER EXPORT UI END === */



/* === LEADBOT SIMPLE FORM UI START === */
.leadbot-select {
    width: 100%;
    border: 1px solid #cbd5e1;
    border-radius: 12px;
    padding: 11px 12px;
    font-size: 15px;
    background: white;
}

.leadbot-advanced {
    margin-top: 14px;
    padding: 12px;
    border-radius: 14px;
    background: #f8fafc;
    border: 1px solid #e2e8f0;
}

.leadbot-advanced summary {
    cursor: pointer;
    font-weight: 900;
    color: #1e3a8a;
    font-size: 13px;
}

.leadbot-start-btn {
    width: 100%;
    background: linear-gradient(135deg, #0f172a, #1e3a8a) !important;
    font-size: 15px !important;
}
/* === LEADBOT SIMPLE FORM UI END === */


/* === LEADBOT SIDEBAR ACTION CLEANUP START === */
.panel button,
.panel .btn,
.panel a.btn {
    background: #1e3a8a !important;
    color: #ffffff !important;
}

.leadbot-start-btn {
    width: 100%;
    background: linear-gradient(135deg, #1e3a8a, #2563eb) !important;
    color: #ffffff !important;
    font-size: 15px !important;
    box-shadow: 0 10px 22px rgba(30, 58, 138, .20);
}

.leadbot-add-domain-tool {
    margin-top: 18px;
    padding: 13px;
    border-radius: 14px;
    background: #f8fafc;
    border: 1px solid #dbe4f0;
}



.leadbot-add-domain-tool form {
    margin-top: 10px;
}

.leadbot-add-domain-tool label {
    margin-top: 10px;
}

.leadbot-secondary-btn {
    width: 100%;
    margin-top: 12px !important;
    background: #1e3a8a !important;
    color: #ffffff !important;
    padding: 10px 12px !important;
    font-size: 13px !important;
    border-radius: 11px !important;
}

.leadbot-add-domain-tool .help {
    margin: 10px 0 0;
    font-size: 12px;
}
/* === LEADBOT SIDEBAR ACTION CLEANUP END === */















/* === LEADBOT JS DELETE + BLOCK BUTTON CSS START === */
.lead-card {
    position: relative !important;
}

.lead-delete-one-js,
.lead-block-one-js {
    position: absolute !important;
    top: 14px !important;
    width: auto !important;
    min-width: 44px !important;
    height: 26px !important;
    padding: 0 10px !important;
    display: inline-flex !important;
    align-items: center !important;
    justify-content: center !important;
    border-radius: 999px !important;
    color: #ffffff !important;
    text-decoration: none !important;
    font-size: 10px !important;
    font-weight: 950 !important;
    line-height: 1 !important;
    letter-spacing: .01em !important;
    z-index: 30 !important;
    box-shadow: 0 6px 14px rgba(15, 23, 42, .16) !important;
}

.lead-delete-one-js {
    right: 70px !important;
    background: #b91c1c !important;
}

.lead-block-one-js {
    right: 14px !important;
    background: #7f1d1d !important;
}

.lead-delete-one-js:hover,
.lead-block-one-js:hover {
    background: #991b1b !important;
}

.lead-head {
    padding-right: 128px !important;
}
/* === LEADBOT JS DELETE + BLOCK BUTTON CSS END === */

/* === LEADBOT JS ONE DELETE BUTTON START === */
.lead-card {
    position: relative;
}

.lead-delete-one-js {
    position: absolute;
    top: 10px;
    right: 10px;
    padding: 5px 8px;
    border-radius: 999px;
    background: #b91c1c !important;
    color: #ffffff !important;
    text-decoration: none;
    font-size: 10px;
    font-weight: 950;
    line-height: 1;
    opacity: .86;
    z-index: 4;
    box-shadow: 0 6px 14px rgba(185, 28, 28, .18);
}

.lead-delete-one-js:hover {
    opacity: 1;
    background: #991b1b !important;
}
/* === LEADBOT JS ONE DELETE BUTTON END === */


/* === LEADBOT BLOCK DOMAINS BOX START === */
.leadbot-block-domains-box {
    margin-top: 18px;
    padding: 13px;
    border-radius: 14px;
    background: #f8fafc;
    border: 1px solid #dbe4f0;
}
.leadbot-block-domains-box h2 {
    margin: 0 0 10px !important;
    font-size: 17px !important;
}
.leadbot-block-domains-box textarea {
    width: 100%;
    min-height: 74px;
    resize: vertical;
    border: 1px solid #cbd5e1;
    border-radius: 12px;
    padding: 11px 12px;
    font-size: 14px;
    font-family: inherit;
}
/* === LEADBOT BLOCK DOMAINS BOX END === */


/* === LEADBOT COMPACT ADD DOMAIN BOX START === */
.leadbot-add-domain-tool {
    margin-top: 12px !important;
    padding: 9px !important;
    border-radius: 12px !important;
}

.leadbot-add-domain-tool h2 {
    font-size: 15px !important;
    margin: 0 0 7px !important;
    line-height: 1.15 !important;
}

.leadbot-add-domain-tool form {
    margin-top: 6px !important;
}

.leadbot-add-domain-tool label {
    margin-top: 6px !important;
    margin-bottom: 4px !important;
    font-size: 10px !important;
    letter-spacing: .045em !important;
}

.leadbot-add-domain-tool input {
    padding: 8px 10px !important;
    font-size: 13px !important;
    border-radius: 10px !important;
}

.leadbot-add-domain-tool .leadbot-secondary-btn {
    margin-top: 8px !important;
    padding: 8px 10px !important;
    font-size: 12px !important;
    border-radius: 10px !important;
}

.leadbot-add-domain-tool .help {
    margin-top: 7px !important;
    font-size: 10.5px !important;
    line-height: 1.3 !important;
}
/* === LEADBOT COMPACT ADD DOMAIN BOX END === */


/* === LEADBOT SIDEBAR SPACING + BLOCK DOMAIN SIZE START === */

/* More breathing room after Start LeadBot/help text before Add Domain */
#run-lead-bot > p.help {
    margin-bottom: 18px !important;
}

.leadbot-add-domain-tool {
    margin-top: 18px !important;
}

/* Make Block Domain feel smaller than Add Domain */
.leadbot-block-domains-box {
    margin-top: 12px !important;
    padding: 8px !important;
    border-radius: 11px !important;
}

.leadbot-block-domains-box h2 {
    font-size: 14px !important;
    margin: 0 0 5px !important;
    line-height: 1.1 !important;
}

.leadbot-block-domains-box form {
    margin-top: 5px !important;
}

.leadbot-block-domains-box label {
    margin-top: 5px !important;
    margin-bottom: 3px !important;
    font-size: 9.5px !important;
    letter-spacing: .04em !important;
}

.leadbot-block-domains-box textarea {
    min-height: 46px !important;
    padding: 7px 9px !important;
    font-size: 12px !important;
    border-radius: 9px !important;
}

.leadbot-block-domains-box .leadbot-secondary-btn {
    margin-top: 7px !important;
    padding: 6px 10px !important;
    font-size: 13px !important;
    line-height: 1.05 !important;
    font-weight: 950 !important;
    border-radius: 999px !important;
}

.leadbot-block-domains-box .help {
    margin-top: 6px !important;
    font-size: 10px !important;
    line-height: 1.25 !important;
}

/* === LEADBOT SIDEBAR SPACING + BLOCK DOMAIN SIZE END === */


/* === LEADBOT EXPORTS TITLE MATCH SIDEBAR TOOLS START === */
#exports {
    font-size: 14px !important;
    margin-top: 16px !important;
    margin-bottom: 8px !important;
    line-height: 1.1 !important;
}

.file-list {
    gap: 6px !important;
}

.file-link {
    padding: 8px 9px !important;
    font-size: 12px !important;
    border-radius: 10px !important;
}

.export-delete {
    min-height: 30px !important;
    font-size: 16px !important;
    border-radius: 9px !important;
}
/* === LEADBOT EXPORTS TITLE MATCH SIDEBAR TOOLS END === */


/* === LEADBOT HEADER FINAL POLISH START === */
.hero {
    padding: 26px 28px !important;
}

.hero > div:first-child {
    display: grid !important;
    grid-template-columns: minmax(0, 1fr) auto !important;
    align-items: center !important;
    gap: 26px !important;
}

.leadbot-logo-wrap {
    display: flex !important;
    align-items: center !important;
    gap: 18px !important;
    min-width: 0 !important;
}

.leadbot-logo-link {
    display: flex !important;
    align-items: center !important;
    flex: 0 0 auto !important;
    background: transparent !important;
    border: 0 !important;
    padding: 0 !important;
    box-shadow: none !important;
}

.leadbot-logo {
    height: 52px !important;
    width: auto !important;
    display: block !important;
}

.leadbot-logo-wrap h1 {
    margin: 0 0 6px !important;
    font-size: 25px !important;
    line-height: 1.05 !important;
    letter-spacing: -0.035em !important;
}

.leadbot-logo-wrap p {
    margin: 0 !important;
    max-width: 620px !important;
    font-size: 13px !important;
    line-height: 1.35 !important;
    color: rgba(255,255,255,.82) !important;
}

.leadbot-nav {
    display: flex !important;
    justify-content: flex-end !important;
    align-items: center !important;
    gap: 8px !important;
    flex-wrap: nowrap !important;
    margin: 0 !important;
    white-space: nowrap !important;
}

.leadbot-nav a {
    display: inline-flex !important;
    align-items: center !important;
    justify-content: center !important;
    min-height: 34px !important;
    padding: 8px 12px !important;
    border-radius: 999px !important;
    font-size: 12px !important;
    line-height: 1 !important;
    font-weight: 850 !important;
    letter-spacing: -0.015em !important;
    background: rgba(255,255,255,.13) !important;
    border: 1px solid rgba(255,255,255,.22) !important;
    box-shadow: none !important;
}

.leadbot-nav a:hover {
    background: rgba(255,255,255,.22) !important;
    transform: translateY(-1px) !important;
}

@media (max-width: 980px) {
    .hero > div:first-child {
        grid-template-columns: 1fr !important;
    }

    .leadbot-nav {
        justify-content: flex-start !important;
        flex-wrap: wrap !important;
    }

    .leadbot-logo-wrap {
        align-items: flex-start !important;
    }
}
/* === LEADBOT HEADER FINAL POLISH END === */
/* === DIRECT CURRENT SCAN BOX START === */
.leadbot-current-scan-direct {
    display: none;
    margin: 14px 0 12px;
    padding: 10px;
    border-radius: 12px;
    background: #f8fafc;
    border: 1px solid #dbe4f0;
}

.leadbot-current-scan-direct.is-active {
    display: block;
}

.leadbot-current-scan-direct h2 {
    margin: 0 0 7px !important;
    font-size: 14px !important;
    line-height: 1.1 !important;
}

.leadbot-current-scan-direct-name {
    font-size: 11px;
    font-weight: 850;
    line-height: 1.25;
    overflow-wrap: anywhere;
    color: #0f172a;
    margin-bottom: 5px;
}

.leadbot-current-scan-direct-count {
    font-size: 10px;
    color: #64748b;
    margin: 0 0 8px;
}

.leadbot-current-scan-direct-actions {
    display: grid;
    grid-template-columns: 1fr;
    gap: 6px;
}

.leadbot-current-scan-direct-actions a {
    display: inline-flex;
    justify-content: center;
    align-items: center;
    margin: 0 !important;
    padding: 8px 9px !important;
    border-radius: 9px !important;
    font-size: 11px !important;
    font-weight: 900 !important;
    text-decoration: none !important;
    color: #ffffff !important;
}

.leadbot-current-download {
    background: #1e3a8a !important;
}
/* === DIRECT CURRENT SCAN BOX END === */




/* === FINAL KILL RESULTS TITLE ROW === */
.results-top,
#results .results-top,
main#results .results-top {
    display: none !important;
    height: 0 !important;
    min-height: 0 !important;
    margin: 0 !important;
    padding: 0 !important;
    overflow: hidden !important;
}

#results.panel {
    padding-top: 20px !important;
}

#results .leads {
    margin-top: 0 !important;
}
/* === END FINAL KILL RESULTS TITLE ROW === */




/* === LEADBOT WHY THIS LEAD FOOTER START === */
.lead-card {
    overflow: hidden !important;
}

.reason {
    margin: 18px -20px -20px -20px !important;
    padding: 16px 20px 18px !important;
    border-left: 0 !important;
    border-top: 1px solid #dbe4f0 !important;
    background: linear-gradient(180deg, #f8fafc, #eef4ff) !important;
    border-radius: 0 0 18px 18px !important;
}

.reason b {
    display: block !important;
    margin-bottom: 8px !important;
    font-size: 15px !important;
    color: #0f172a !important;
}

.reason p {
    margin: 0 !important;
    color: #334155 !important;
    line-height: 1.55 !important;
}

.reason ol {
    margin: 0 !important;
    padding-left: 22px !important;
}

.reason li {
    margin: 3px 0 !important;
}
/* === LEADBOT WHY THIS LEAD FOOTER END === */


/* === LEADBOT WHY THIS LEAD DARK BLUE FOOTER START === */
.reason {
    margin: 18px -20px -20px -20px !important;
    padding: 16px 20px 18px !important;
    border-left: 0 !important;
    border-top: 1px solid rgba(147, 197, 253, 0.35) !important;
    background: linear-gradient(135deg, #0f172a, #1e3a8a) !important;
    border-radius: 0 0 18px 18px !important;
    color: #ffffff !important;
}

.reason b {
    display: block !important;
    margin-bottom: 8px !important;
    font-size: 15px !important;
    color: #ffffff !important;
}

.reason p {
    margin: 0 !important;
    color: rgba(255,255,255,0.86) !important;
    line-height: 1.55 !important;
}

.reason ol {
    margin: 0 !important;
    padding-left: 22px !important;
    color: rgba(255,255,255,0.86) !important;
}

.reason li {
    margin: 3px 0 !important;
    color: rgba(255,255,255,0.86) !important;
}
/* === LEADBOT WHY THIS LEAD DARK BLUE FOOTER END === */


/* === LEADBOT WHY THIS LEAD OUTLINE FOOTER START === */
.reason {
    margin: 18px -20px -20px -20px !important;
    padding: 15px 20px 17px !important;

    background: #ffffff !important;
    border-left: 0 !important;
    border-top: 2px solid #1e3a8a !important;
    border-right: 0 !important;
    border-bottom: 0 !important;
    border-radius: 0 0 18px 18px !important;

    color: #0f172a !important;
    box-shadow: inset 0 1px 0 rgba(30, 58, 138, 0.08) !important;
}

.reason b {
    display: block !important;
    margin-bottom: 8px !important;
    font-size: 15px !important;
    color: #1e3a8a !important;
}

.reason p,
.reason ol,
.reason li {
    color: #334155 !important;
}

.reason p {
    margin: 0 !important;
    line-height: 1.55 !important;
}

.reason ol {
    margin: 0 !important;
    padding-left: 22px !important;
}

.reason li {
    margin: 3px 0 !important;
}
/* === LEADBOT WHY THIS LEAD OUTLINE FOOTER END === */


/* === LEADBOT WHY THIS LEAD BOTTOM OUTLINE START === */
.lead-card {
    overflow: hidden !important;
    border-bottom: 3px solid #5b21b6 !important;
}

.reason {
    margin: 18px -20px -20px -20px !important;
    padding: 15px 20px 17px !important;

    background: #ffffff !important;
    border-left: 0 !important;
    border-right: 0 !important;
    border-top: 1px solid #1e3a8a !important;
    border-bottom: 0 !important;
    border-radius: 0 0 18px 18px !important;

    color: #0f172a !important;
    box-shadow: none !important;
}

.reason b {
    display: block !important;
    margin-bottom: 8px !important;
    font-size: 15px !important;
    color: #1e3a8a !important;
}

.reason p,
.reason ol,
.reason li {
    color: #334155 !important;
}

.reason p {
    margin: 0 !important;
    line-height: 1.55 !important;
}

.reason ol {
    margin: 0 !important;
    padding-left: 22px !important;
}

.reason li {
    margin: 3px 0 !important;
}
/* === LEADBOT WHY THIS LEAD BOTTOM OUTLINE END === */


/* === LEADBOT REMOVE WHY THIS LEAD TOP LINE START === */
.reason {
    border-top: 0 !important;
}
/* === LEADBOT REMOVE WHY THIS LEAD TOP LINE END === */


/* === LEADBOT LEAD CARD TOP CLEANUP START === */
.lead-card {
    position: relative !important;
    padding-top: 18px !important;
}

.lead-head {
    align-items: flex-start !important;
    padding-right: 58px !important;
}

.lead-card h3 {
    font-size: 20px !important;
    line-height: 1.2 !important;
    letter-spacing: -0.025em !important;
    color: #0f172a !important;
}

.domain {
    margin-top: 5px !important;
    font-size: 13px !important;
    color: #64748b !important;
}

.lead-delete-one-js {
    top: 14px !important;
    right: 14px !important;
    width: auto !important;
    min-width: 44px !important;
    height: 26px !important;
    padding: 0 10px !important;
    display: inline-flex !important;
    align-items: center !important;
    justify-content: center !important;
    border-radius: 999px !important;
    font-size: 10px !important;
    font-weight: 950 !important;
    letter-spacing: .01em !important;
}

.badges {
    margin: 13px 0 14px !important;
    gap: 7px !important;
}

.badges span {
    padding: 6px 9px !important;
    font-size: 11px !important;
    border-radius: 999px !important;
}
/* === LEADBOT LEAD CARD TOP CLEANUP END === */


/* === LEADBOT DOMAIN BIGGER START === */
.domain {
    margin: 8px 0 2px !important;
    font-size: 17px !important;
    line-height: 1.2 !important;
    font-weight: 900 !important;
    color: #1e3a8a !important;
    letter-spacing: -0.02em !important;
}

.domain a {
    font-size: 17px !important;
    font-weight: 900 !important;
    color: #1e3a8a !important;
    text-decoration: none !important;
}

.domain a:hover {
    text-decoration: underline !important;
}
/* === LEADBOT DOMAIN BIGGER END === */


/* === LEADBOT CLICKABLE DOMAIN STYLE START === */
.domain a {
    color: #1e3a8a !important;
    font-weight: 950 !important;
    text-decoration: none !important;
}

.domain a:hover {
    text-decoration: underline !important;
}
/* === LEADBOT CLICKABLE DOMAIN STYLE END === */





/* === LEADBOT SOFT ADDRESS NOT FOUND START === */
.info-grid div p {
    font-weight: 500 !important;
}

.info-grid div p:has(a) {
    font-weight: 800 !important;
}

.info-grid div:nth-child(5) p {
    font-weight: 600 !important;
    color: #64748b !important;
}
/* === LEADBOT SOFT ADDRESS NOT FOUND END === */
\n
/* === LEADBOT MORE SPACE BEFORE ADD DOMAIN START === */
#run-lead-bot > p.help {
    margin: 16px 0 24px !important;
    line-height: 1.45 !important;
}

#run-lead-bot .leadbot-add-domain-tool {
    margin-top: 0 !important;
}

#run-lead-bot .leadbot-block-domains-box {
    margin-top: 14px !important;
}
/* === LEADBOT MORE SPACE BEFORE ADD DOMAIN END === */


/* === HIDE RESULTS FILENAME STRIP START === */
.results-top {
    display: none !important;
    margin: 0 !important;
    padding: 0 !important;
}

#results .leads {
    margin-top: 0 !important;
}
/* === HIDE RESULTS FILENAME STRIP END === */




/* === LEADBOT ADDRESS FULL WIDTH START === */
.info-grid {
    grid-template-columns: 150px 220px minmax(240px, 1fr) minmax(240px, 1fr) !important;
}

.lead-address-box {
    margin-top: 12px !important;
    background: #f8fafc !important;
    border: 1px solid #e2e8f0 !important;
    border-radius: 14px !important;
    padding: 12px 14px !important;
}

.lead-address-box b {
    display: block !important;
    margin-bottom: 6px !important;
    color: #334155 !important;
    font-size: 12px !important;
    text-transform: uppercase !important;
    letter-spacing: .05em !important;
}

.lead-address-box p {
    margin: 0 !important;
    color: #0f172a !important;
    font-size: 14px !important;
    font-weight: 750 !important;
    line-height: 1.45 !important;
    overflow-wrap: anywhere !important;
}

@media (max-width: 1250px) {
    .info-grid {
        grid-template-columns: 1fr 1fr !important;
    }
}

@media (max-width: 720px) {
    .info-grid {
        grid-template-columns: 1fr !important;
    }
}
/* === LEADBOT ADDRESS FULL WIDTH END === */


/* === SERVER EDITABLE ADDRESS FIELD START === */
.lead-address-box form {
    display: grid !important;
    grid-template-columns: minmax(0, 1fr) auto !important;
    gap: 8px !important;
    align-items: center !important;
}

.lead-address-box input[name="address"] {
    width: 100% !important;
    margin: 0 !important;
    padding: 11px 12px !important;
    border-radius: 10px !important;
    border: 1px solid #cbd5e1 !important;
    background: #ffffff !important;
    color: #0f172a !important;
    font-size: 14px !important;
}

.lead-address-box button {
    margin: 0 !important;
    padding: 11px 13px !important;
    border-radius: 10px !important;
    background: #1e3a8a !important;
    color: #ffffff !important;
    font-size: 12px !important;
    font-weight: 900 !important;
    white-space: nowrap !important;
}

@media (max-width: 720px) {
    .lead-address-box form {
        grid-template-columns: 1fr !important;
    }
}
/* === SERVER EDITABLE ADDRESS FIELD END === */


/* === LEADBOT FILL ADDRESS BUTTON START === */
.leadbot-current-fill-addresses {
    background: #0f172a !important;
}
/* === LEADBOT FILL ADDRESS BUTTON END === */


/* === LEADBOT EDITABLE CONTACT FIELDS START === */
.lead-contact-edit-form {
    margin: 0 !important;
}

.lead-edit-grid input {
    width: 100% !important;
    margin: 0 !important;
    padding: 9px 10px !important;
    border-radius: 10px !important;
    border: 1px solid #cbd5e1 !important;
    background: #ffffff !important;
    color: #0f172a !important;
    font-size: 13px !important;
}

.lead-contact-save {
    margin-top: 10px !important;
    padding: 9px 12px !important;
    border-radius: 999px !important;
    background: #1e3a8a !important;
    color: #ffffff !important;
    font-size: 11px !important;
    font-weight: 900 !important;
}
/* === LEADBOT EDITABLE CONTACT FIELDS END === */








/* === LEADBOT ADDRESS FIELD WIDTH TUNE START === */
.lead-address-box form {
    display: grid !important;
    grid-template-columns: minmax(0, 2fr) auto !important;
    gap: 10px !important;
    align-items: center !important;
    max-width: 78% !important;
}

.lead-address-box input[name="address"] {
    width: 100% !important;
}

@media (max-width: 900px) {
    .lead-address-box form {
        max-width: 100% !important;
        grid-template-columns: minmax(0, 1fr) auto !important;
    }
}

@media (max-width: 720px) {
    .lead-address-box form {
        grid-template-columns: 1fr !important;
    }
}
/* === LEADBOT ADDRESS FIELD WIDTH TUNE END === */


/* === LEADBOT SMALL SAVE CONTACT BUTTON START === */
.lead-contact-save {
    margin-top: 10px !important;
    padding: 6px 10px !important;
    min-height: 26px !important;
    border-radius: 999px !important;
    background: #1e3a8a !important;
    color: #ffffff !important;
    font-size: 10px !important;
    line-height: 1 !important;
    font-weight: 950 !important;
    white-space: nowrap !important;
    width: fit-content !important;
}
/* === LEADBOT SMALL SAVE CONTACT BUTTON END === */










/* === LEADBOT SMALL SAVE ADDRESS BUTTON FINAL START === */
.lead-address-box button,
.lead-address-box button[type="submit"] {
    margin: 0 !important;
    padding: 6px 10px !important;
    min-height: 26px !important;
    border-radius: 999px !important;
    background: #1e3a8a !important;
    color: #ffffff !important;
    font-size: 10px !important;
    line-height: 1 !important;
    font-weight: 950 !important;
    white-space: nowrap !important;
    width: fit-content !important;
}
/* === LEADBOT SMALL SAVE ADDRESS BUTTON FINAL END === */


/* === LEADBOT COMPLETE ADDRESSES BOTTOM START === */
.leadbot-current-scan-direct {
    display: none;
    margin: 18px 0 0 !important;
    padding: 10px !important;
    border-radius: 13px !important;
    background: #f8fafc !important;
    border: 1px solid #dbe4f0 !important;
}

.leadbot-current-scan-direct.is-active {
    display: block !important;
}

.leadbot-current-scan-direct h2 {
    margin: 0 0 7px !important;
    font-size: 14px !important;
    line-height: 1.1 !important;
}

.leadbot-current-scan-direct-name {
    font-size: 11px !important;
    font-weight: 850 !important;
    line-height: 1.25 !important;
    overflow-wrap: anywhere !important;
    color: #0f172a !important;
    margin-bottom: 5px !important;
}

.leadbot-current-scan-direct-count {
    font-size: 10px !important;
    color: #64748b !important;
    margin: 0 0 8px !important;
}

.leadbot-current-scan-direct-actions {
    display: grid !important;
    grid-template-columns: 1fr !important;
    gap: 7px !important;
}

.leadbot-current-scan-direct-actions a {
    display: inline-flex !important;
    justify-content: center !important;
    align-items: center !important;
    margin: 0 !important;
    padding: 8px 9px !important;
    border-radius: 999px !important;
    font-size: 11px !important;
    font-weight: 900 !important;
    text-decoration: none !important;
    color: #ffffff !important;
}

.leadbot-current-download {
    background: #1e3a8a !important;
}

.leadbot-current-fill-addresses {
    background: #0f172a !important;
}
/* === LEADBOT COMPLETE ADDRESSES BOTTOM END === */


/* === LEADBOT COMPLETE ADDRESSES WHITESPACE START === */
.leadbot-current-scan-direct-actions {
    gap: 12px !important;
}

#leadbotCompleteAddresses {
    margin-top: 8px !important;
}

#leadbotCurrentScanDownload {
    margin-bottom: 4px !important;
}
/* === LEADBOT COMPLETE ADDRESSES WHITESPACE END === */


/* === LEADBOT EXPORT CSV LINKS START === */
.export-file-row {
    display: grid !important;
    grid-template-columns: minmax(0, 1fr) auto !important;
    align-items: center !important;
    gap: 8px !important;
    margin-bottom: 8px !important;
}

.export-file-row .file-link {
    min-width: 0 !important;
    overflow: hidden !important;
    text-overflow: ellipsis !important;
    white-space: nowrap !important;
}

.export-csv-link {
    display: inline-flex !important;
    align-items: center !important;
    justify-content: center !important;
    min-height: 28px !important;
    padding: 6px 9px !important;
    border-radius: 999px !important;
    background: #1e3a8a !important;
    color: #ffffff !important;
    font-size: 10px !important;
    font-weight: 950 !important;
    text-decoration: none !important;
    border: 1px solid rgba(30,58,138,.18) !important;
    flex: 0 0 auto !important;
}

.export-csv-link:hover {
    background: #0f172a !important;
}
/* === LEADBOT EXPORT CSV LINKS END === */




/* === LEADBOT CONTACT GRID VISIBILITY FIX START === */
.lead-card,
.lead-contact-edit-form,
.lead-edit-grid {
    min-width: 0 !important;
    max-width: 100% !important;
}

.lead-edit-grid {
    display: grid !important;
    grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
    gap: 12px !important;
}

.lead-edit-grid input,
.lead-address-box input {
    width: 100% !important;
    max-width: 100% !important;
    min-width: 0 !important;
}

@media (max-width: 900px) {
    .lead-edit-grid {
        grid-template-columns: 1fr !important;
    }
}
/* === LEADBOT CONTACT GRID VISIBILITY FIX END === */


/* === LEADBOT MATCH USERS HEADER START === */
.leadbot-brand {
    min-height: 72px !important;
    display: flex !important;
    align-items: center !important;
    justify-content: space-between !important;
    gap: 28px !important;
    flex-wrap: nowrap !important;
}

.leadbot-brand-left {
    display: flex !important;
    align-items: center !important;
    gap: 20px !important;
    flex: 1 1 auto !important;
    min-width: 0 !important;
}

.leadbot-logo {
    height: 58px !important;
    width: auto !important;
    display: block !important;
}

.leadbot-brand-left h1 {
    margin: 0 0 6px !important;
    font-size: 24px !important;
    line-height: 1.05 !important;
    letter-spacing: -0.035em !important;
}

.leadbot-brand-left p {
    margin: 0 !important;
    max-width: 640px !important;
    font-size: 13px !important;
    line-height: 1.35 !important;
    color: rgba(255,255,255,.84) !important;
}

.leadbot-nav {
    display: flex !important;
    align-items: center !important;
    justify-content: flex-end !important;
    gap: 10px !important;
    flex-wrap: nowrap !important;
    margin: 0 0 0 auto !important;
    white-space: nowrap !important;
}

.leadbot-nav a {
    min-height: 38px !important;
    padding: 10px 15px !important;
    border-radius: 11px !important;
    font-size: 12px !important;
    font-weight: 900 !important;
    line-height: 1 !important;
}
/* === LEADBOT MATCH USERS HEADER END === */































/* === LEADBOT COMPLETE DETAILS FULL WIDTH MATCH START === */

/* Container stays above Exports but does not style like a card */
.leadbot-complete-details-above-exports {
    width: 100% !important;
    margin: 12px 0 16px !important;
    padding: 0 !important;
    display: block !important;
    background: transparent !important;
    border: 0 !important;
    box-shadow: none !important;
}

/* Match the LeadBot action buttons above it: full width, same blue, same font feel */
.leadbot-complete-details-above-exports #leadbotCompleteAddresses,
.leadbot-complete-details-above-exports .leadbot-current-fill-addresses {
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

    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif !important;
    font-size: 13px !important;
    font-weight: 900 !important;
    line-height: 1.2 !important;
    text-align: center !important;
    text-decoration: none !important;
    white-space: nowrap !important;

    box-shadow: none !important;
    cursor: pointer !important;
}

.leadbot-complete-details-above-exports #leadbotCompleteAddresses:hover,
.leadbot-complete-details-above-exports .leadbot-current-fill-addresses:hover {
    background: #172f70 !important;
    color: #ffffff !important;
    text-decoration: none !important;
}

/* No icon, no offset, no weird centering */
.leadbot-complete-details-above-exports #leadbotCompleteAddresses::before,
.leadbot-complete-details-above-exports #leadbotCompleteAddresses::after,
.leadbot-complete-details-above-exports .leadbot-current-fill-addresses::before,
.leadbot-complete-details-above-exports .leadbot-current-fill-addresses::after {
    content: none !important;
    display: none !important;
}

/* Keep Current Scan duplicate hidden */
#leadbotCurrentScanDirect #leadbotCompleteAddresses,
.leadbot-current-scan-direct #leadbotCompleteAddresses {
    display: none !important;
}

/* === LEADBOT COMPLETE DETAILS FULL WIDTH MATCH END === */




/* === LEADBOT RESTORE CLEAN SEARCH SUMMARY VISIBILITY START === */
.leadbot-search-summary-row {
    display: block !important;
    height: auto !important;
    min-height: 0 !important;
    margin: 0 0 16px !important;
    padding: 14px !important;
    overflow: visible !important;
}

.leadbot-search-summary-line {
    display: flex !important;
    flex-wrap: wrap !important;
    justify-content: center !important;
    gap: 10px !important;
}

.leadbot-search-summary-line span {
    text-align: center !important;
}
/* === LEADBOT RESTORE CLEAN SEARCH SUMMARY VISIBILITY END === */


/* === LEADBOT SEARCH SUMMARY ROW START === */
.leadbot-search-summary-row {
    margin: 0 0 16px !important;
    padding: 14px !important;
    border-radius: 16px !important;
    background: linear-gradient(135deg, #0f172a, #1e3a8a) !important;
    color: #ffffff !important;
    box-shadow: 0 12px 28px rgba(15, 23, 42, .16) !important;
}

.leadbot-search-summary-title {
    font-size: 12px !important;
    font-weight: 950 !important;
    letter-spacing: .045em !important;
    text-transform: uppercase !important;
    color: rgba(255,255,255,.76) !important;
    margin-bottom: 10px !important;
}

.leadbot-search-summary-grid {
    display: grid !important;
    grid-template-columns: repeat(4, minmax(110px, 1fr)) minmax(220px, 1.6fr) !important;
    gap: 10px !important;
}

.leadbot-search-summary-grid div {
    background: rgba(255,255,255,.10) !important;
    border: 1px solid rgba(255,255,255,.16) !important;
    border-radius: 12px !important;
    padding: 10px 11px !important;
    min-width: 0 !important;
}

.leadbot-search-summary-grid b {
    display: block !important;
    margin-bottom: 4px !important;
    font-size: 10px !important;
    text-transform: uppercase !important;
    color: rgba(255,255,255,.64) !important;
}

.leadbot-search-summary-grid span {
    display: block !important;
    font-size: 13px !important;
    font-weight: 900 !important;
    color: #ffffff !important;
    overflow-wrap: anywhere !important;
}

@media (max-width: 1100px) {
    .leadbot-search-summary-grid {
        grid-template-columns: 1fr 1fr !important;
    }

    .leadbot-search-summary-file {
        grid-column: 1 / -1 !important;
    }
}

@media (max-width: 720px) {
    .leadbot-search-summary-grid {
        grid-template-columns: 1fr !important;
    }
}
/* === LEADBOT SEARCH SUMMARY ROW END === */


/* === LEADBOT FINAL EXPORT SPACING + REMOVE BUTTON START === */



/* Restore breathing room between Enrich Website Details and Exports */
.leadbot-complete-details-above-exports {
    margin: 18px 0 24px !important;
}

#exports {
    display: block !important;
    margin: 28px 0 14px !important;
    padding: 0 !important;
    line-height: 1.15 !important;
}

/* Give export rows back their clean spacing */
.file-list {
    gap: 10px !important;
}

.export-file-row {
    margin-bottom: 10px !important;
    gap: 10px !important;
}

/* Modern no-confirm export remove button */
.export-delete-link,
.export-delete,
.leadbot-dashboard .export-delete {
    display: inline-flex !important;
    align-items: center !important;
    justify-content: center !important;
    min-height: 28px !important;
    padding: 6px 11px !important;
    margin: 0 !important;
    border-radius: 999px !important;
    background: #7f1d1d !important;
    color: #ffffff !important;
    border: 0 !important;
    box-shadow: 0 6px 14px rgba(127, 29, 29, .16) !important;
    font-size: 10px !important;
    font-weight: 950 !important;
    line-height: 1 !important;
    text-decoration: none !important;
    cursor: pointer !important;
}

.export-delete-link:hover,
.export-delete:hover,
.leadbot-dashboard .export-delete:hover {
    background: #991b1b !important;
    color: #ffffff !important;
    text-decoration: none !important;
    transform: translateY(-1px) !important;
}

/* === LEADBOT FINAL EXPORT SPACING + REMOVE BUTTON END === */


/* === LEADBOT FINAL LEAD CARD INNER SPACING START === */

/* Give the saved contact area breathing room before SEO Snapshot */
.lead-contact-edit-form {
    margin-bottom: 18px !important;
}

.lead-contact-save {
    margin-top: 12px !important;
    margin-bottom: 18px !important;
}

/* SEO Snapshot should feel like its own section */
.leadbot-seo-snapshot {
    margin-top: 22px !important;
    padding-top: 18px !important;
    border-top: 1px solid #e2e8f0 !important;
}

.leadbot-seo-snapshot > strong {
    display: block !important;
    margin: 0 0 16px !important;
    font-size: 16px !important;
    line-height: 1.25 !important;
    color: #0f172a !important;
}

/* Space each SEO item clearly */
.leadbot-seo-snapshot-item {
    margin: 0 0 22px !important;
}

.leadbot-seo-snapshot-item b {
    display: block !important;
    margin: 0 0 12px !important;
    font-size: 15px !important;
    line-height: 1.25 !important;
    color: #0f172a !important;
}

.leadbot-seo-snapshot-item p {
    margin: 0 !important;
    line-height: 1.55 !important;
    color: #1f2937 !important;
}

/* Bigger gap before Why This Lead */
.reason {
    margin-top: 28px !important;
    padding-top: 20px !important;
}

.reason b {
    margin-bottom: 12px !important;
}

.reason p,
.reason ol,
.reason li {
    line-height: 1.65 !important;
}

/* Save Lead should not crowd the reason text */
.lead-save-btn,
button.lead-save-btn,
.lead-card button[name="save_lead"],
.lead-card button[type="submit"]:last-child {
    margin-top: 18px !important;
}

/* === LEADBOT FINAL LEAD CARD INNER SPACING END === */


/* === LEADBOT WHY THIS LEAD RAISE + BLACK START === */

/* Raise the Why This Lead section slightly so it does not feel detached */
.reason {
    margin-top: 14px !important;
    padding-top: 14px !important;
}

/* Make the heading black instead of blue */
.reason b {
    color: #0f172a !important;
    margin-bottom: 8px !important;
}

/* Make the explanation text black/darker */
.reason p,
.reason ol,
.reason li {
    color: #0f172a !important;
}

/* === LEADBOT WHY THIS LEAD RAISE + BLACK END === */


/* === LEADBOT WHY THIS LEAD NOTE FONT FINAL START === */

/* This is note text, not a major headline */
.reason b {
    font-size: 13px !important;
    line-height: 1.25 !important;
    font-weight: 850 !important;
    color: #0f172a !important;
    margin-bottom: 7px !important;
}

.reason p,
.reason ol,
.reason li {
    font-size: 13px !important;
    line-height: 1.5 !important;
    color: #0f172a !important;
}

/* Keep the note tucked up without feeling smashed */
.reason {
    margin-top: 14px !important;
    padding-top: 13px !important;
    padding-bottom: 14px !important;
}

/* === LEADBOT WHY THIS LEAD NOTE FONT FINAL END === */


/* === LEADBOT SAVE LEAD NOTE GAP FINAL START === */

/* Keep Save Lead from crowding the Why This Lead note */
.reason + form,
.reason + .lead-save-form,
.reason ~ form,
.lead-card .lead-save-btn,
.lead-card button.lead-save-btn {
    margin-top: 14px !important;
}

/* Keep note text calm and compact */
.reason p,
.reason ol,
.reason li {
    font-size: 13px !important;
    line-height: 1.55 !important;
}

/* === LEADBOT SAVE LEAD NOTE GAP FINAL END === */


/* === LEADBOT SEO SNAPSHOT FONT MATCH WHY THIS LEAD FINAL START === */

/* Match SEO Snapshot heading to Why This Lead note label */
.leadbot-seo-snapshot > strong {
    font-size: 13px !important;
    line-height: 1.25 !important;
    font-weight: 850 !important;
    color: #0f172a !important;
    margin-bottom: 12px !important;
}

/* Match Site Title / Meta Description labels to Why This Lead */
.leadbot-seo-snapshot-item b {
    font-size: 13px !important;
    line-height: 1.25 !important;
    font-weight: 850 !important;
    color: #0f172a !important;
    margin-bottom: 8px !important;
}

/* Keep the values calm and readable */
.leadbot-seo-snapshot-item p {
    font-size: 13px !important;
    line-height: 1.5 !important;
    color: #0f172a !important;
}

/* Slightly tighten the SEO section now that fonts match */
.leadbot-seo-snapshot {
    margin-top: 20px !important;
    padding-top: 16px !important;
}

.leadbot-seo-snapshot-item {
    margin-bottom: 18px !important;
}

/* === LEADBOT SEO SNAPSHOT FONT MATCH WHY THIS LEAD FINAL END === */


/* === LEADBOT FLASHY EXPORT DELETE START === */

.export-file-row {
    transition:
        opacity .22s ease,
        transform .22s ease,
        background-color .22s ease,
        border-color .22s ease !important;
}

.export-file-row.is-removing {
    opacity: .35 !important;
    transform: translateX(8px) scale(.985) !important;
    background: #fee2e2 !important;
}

.export-file-row.is-removed {
    opacity: 0 !important;
    transform: translateX(18px) scale(.97) !important;
    pointer-events: none !important;
}

/* === LEADBOT FLASHY EXPORT DELETE END === */


/* === LEADBOT CARD SECTION HEADINGS BLUE FINAL START === */

/* Keep final small font sizing, but make the card section labels blue */
.leadbot-seo-snapshot > strong,
.leadbot-seo-snapshot-item b,
.reason b {
    color: #1e3a8a !important;
}

/* Keep body/value text dark, not blue */
.leadbot-seo-snapshot-item p,
.reason p,
.reason ol,
.reason li {
    color: #0f172a !important;
}
/* === LEADBOT REMOVE SAVED LEADS UI FINAL END === */








/* === LEADBOT HARD REAL SCAN BUTTON START === */
#leadbotStartScanButton,
#leadbotRunForm .leadbot-start-btn {
    display: flex !important;
    width: 100% !important;
    min-height: 46px !important;
    align-items: center !important;
    justify-content: center !important;
    margin-top: 16px !important;
    padding: 12px 14px !important;
    border: 0 !important;
    border-radius: 12px !important;
    background: linear-gradient(135deg, #1e3a8a, #2563eb) !important;
    color: #ffffff !important;
    font-size: 15px !important;
    font-weight: 950 !important;
    line-height: 1.1 !important;
    cursor: pointer !important;
    opacity: 1 !important;
    visibility: visible !important;
    text-align: center !important;
}
/* === LEADBOT HARD REAL SCAN BUTTON END === */








/* === LEADBOT MODERN LEAD DELETE UI START === */

.lead-delete-one-js {
    transition:
        transform .16s ease,
        box-shadow .16s ease,
        background-color .16s ease,
        opacity .16s ease !important;
}

.lead-delete-one-js:hover {
    transform: translateY(-1px) scale(1.03) !important;
    box-shadow: 0 9px 20px rgba(185, 28, 28, .22) !important;
}

.lead-delete-one-js.is-deleting {
    background: #f97316 !important;
    color: #ffffff !important;
    pointer-events: none !important;
    opacity: .92 !important;
    animation: leadbotDeletePulse .75s ease-in-out infinite alternate !important;
}

.lead-delete-one-js.is-deleted {
    background: #16a34a !important;
    color: #ffffff !important;
    pointer-events: none !important;
    animation: none !important;
}

.lead-card.is-delete-starting {
    outline: 2px solid rgba(249, 115, 22, .45) !important;
    box-shadow: 0 12px 32px rgba(249, 115, 22, .14) !important;
}

.lead-card.is-delete-success {
    outline: 2px solid rgba(34, 197, 94, .45) !important;
    box-shadow: 0 12px 32px rgba(34, 197, 94, .14) !important;
}

.lead-card.is-delete-removing {
    opacity: 0 !important;
    transform: translateX(18px) scale(.985) !important;
    max-height: 0 !important;
    margin: 0 !important;
    padding-top: 0 !important;
    padding-bottom: 0 !important;
    overflow: hidden !important;
    pointer-events: none !important;
    transition:
        opacity .24s ease,
        transform .24s ease,
        max-height .28s ease,
        margin .28s ease,
        padding .28s ease !important;
}

@keyframes leadbotDeletePulse {
    from {
        box-shadow: 0 0 0 rgba(249, 115, 22, .0);
        transform: scale(1);
    }
    to {
        box-shadow: 0 0 0 5px rgba(249, 115, 22, .18);
        transform: scale(1.04);
    }
}

/* === LEADBOT MODERN LEAD DELETE UI END === */


/* === LEADBOT MODERN EXPORT DELETE UI START === */

.export-file-row {
    transition:
        opacity .24s ease,
        transform .24s ease,
        max-height .28s ease,
        margin .28s ease,
        padding .28s ease,
        background-color .18s ease,
        box-shadow .18s ease !important;
}

.export-delete,
.export-delete-link,
button.export-delete,
a.export-delete {
    transition:
        transform .16s ease,
        box-shadow .16s ease,
        background-color .16s ease,
        opacity .16s ease !important;
}

.export-delete:hover,
.export-delete-link:hover {
    transform: translateY(-1px) scale(1.05) !important;
    box-shadow: 0 9px 20px rgba(127, 29, 29, .24) !important;
}

.export-delete.is-deleting,
.export-delete-link.is-deleting {
    background: #f97316 !important;
    color: #ffffff !important;
    pointer-events: none !important;
    opacity: .94 !important;
    animation: leadbotExportDeletePulse .75s ease-in-out infinite alternate !important;
}

.export-delete.is-deleted,
.export-delete-link.is-deleted {
    background: #16a34a !important;
    color: #ffffff !important;
    pointer-events: none !important;
    animation: none !important;
}

.export-file-row.is-export-delete-starting {
    background: #fff7ed !important;
    box-shadow: 0 8px 22px rgba(249, 115, 22, .12) !important;
}

.export-file-row.is-export-delete-success {
    background: #f0fdf4 !important;
    box-shadow: 0 8px 22px rgba(34, 197, 94, .12) !important;
}

.export-file-row.is-export-delete-removing {
    opacity: 0 !important;
    transform: translateX(16px) scale(.985) !important;
    max-height: 0 !important;
    margin: 0 !important;
    padding-top: 0 !important;
    padding-bottom: 0 !important;
    overflow: hidden !important;
    pointer-events: none !important;
}

@keyframes leadbotExportDeletePulse {
    from {
        box-shadow: 0 0 0 rgba(249, 115, 22, 0);
        transform: scale(1);
    }
    to {
        box-shadow: 0 0 0 5px rgba(249, 115, 22, .18);
        transform: scale(1.07);
    }
}

/* === LEADBOT MODERN EXPORT DELETE UI END === */






/* === LEADBOT HIDE EXPORT X ONLY KEEP REMOVE START === */
#run-lead-bot .export-delete-x,
#run-lead-bot a.export-delete-x {
    display: none !important;
    visibility: hidden !important;
    pointer-events: none !important;
}

/* Keep Remove buttons visible */
#run-lead-bot .export-delete-link,
#run-lead-bot .export-remove-btn {
    display: inline-flex !important;
    visibility: visible !important;
    pointer-events: auto !important;
}
/* === LEADBOT HIDE EXPORT X ONLY KEEP REMOVE END === */




/* LEADBOT CURRENT SCAN CARD POLISH */
#run-lead-bot .leadbot-current-scan-direct {
  margin-top: 14px !important;
  padding: 12px !important;
  border-radius: 13px !important;
  background: #f8fafc !important;
  border: 1px solid #dbe4f0 !important;
  box-shadow: none !important;
}

#run-lead-bot .leadbot-current-scan-direct-name {
  display: block !important;
  margin: 0 0 9px !important;
  font-size: 12px !important;
  line-height: 1.25 !important;
  font-weight: 700 !important;
  color: #0f172a !important;
  max-height: 34px !important;
  overflow: hidden !important;
}

#run-lead-bot #leadbotCurrentScanDownload,
#run-lead-bot .leadbot-current-scan-direct a[href*="/lead-bot/export/"] {
  width: 100% !important;
  min-height: 32px !important;
  padding: 8px 10px !important;
  border-radius: 999px !important;
  display: flex !important;
  align-items: center !important;
  justify-content: center !important;
  background: #1e3a8a !important;
  color: #ffffff !important;
  font-size: 11px !important;
  line-height: 1 !important;
  font-weight: 900 !important;
  text-decoration: none !important;
  box-shadow: none !important;
}

#run-lead-bot #leadbotCurrentScanDownload:hover,
#run-lead-bot .leadbot-current-scan-direct a[href*="/lead-bot/export/"]:hover {
  background: #172f72 !important;
  color: #ffffff !important;
  text-decoration: none !important;
}


/* LEADBOT DASHBOARD CARD BLOCK BUTTON RESTORE */
body:not(.leadbot-live-page) .lead-block-one-js,
body:not(.leadbot-live-page) .leadbot-block-one-js,
body:not(.leadbot-live-page) a[href*="/lead-bot/block-domains"] {
  display: inline-flex !important;
  visibility: visible !important;
}

</style>



<style>
/* === LEADBOT CONTACTED NOTES CLEAN UI START === */
.lead-meta-box {
    margin-top: 10px !important;
    padding: 8px 9px !important;
    border-radius: 12px !important;
    background: #f8fafc !important;
    border: 1px solid #e2e8f0 !important;
    display: grid !important;
    grid-template-columns: auto minmax(180px, 1fr) auto !important;
    gap: 8px !important;
    align-items: center !important;
}

.lead-meta-check {
    display: inline-flex !important;
    align-items: center !important;
    gap: 5px !important;
    font-size: 11px !important;
    font-weight: 900 !important;
    color: #334155 !important;
    white-space: nowrap !important;
}

.lead-meta-check input {
    width: auto !important;
    margin: 0 !important;
}

.lead-meta-note {
    width: 100% !important;
    height: 31px !important;
    border: 1px solid #cbd5e1 !important;
    border-radius: 999px !important;
    padding: 6px 10px !important;
    font-size: 12px !important;
    background: #ffffff !important;
}

.lead-meta-save {
    margin: 0 !important;
    padding: 7px 10px !important;
    border-radius: 999px !important;
    font-size: 10px !important;
    background: #1e3a8a !important;
}
/* === LEADBOT CONTACTED NOTES CLEAN UI END === */
</style>


<style>
/* === LEADBOT FINAL DELETE BLOCK POSITION OVERRIDE START === */
.lead-card {
    position: relative !important;
}

.lead-delete-one-js,
.lead-block-one-js {
    position: absolute !important;
    top: 14px !important;
    min-width: 44px !important;
    height: 26px !important;
    padding: 0 10px !important;
    margin: 0 !important;
    display: inline-flex !important;
    align-items: center !important;
    justify-content: center !important;
    border-radius: 999px !important;
    color: #ffffff !important;
    text-decoration: none !important;
    font-size: 10px !important;
    font-weight: 950 !important;
    line-height: 1 !important;
    letter-spacing: .01em !important;
    opacity: 1 !important;
    z-index: 999 !important;
    box-shadow: 0 6px 14px rgba(15, 23, 42, .16) !important;
}

.lead-delete-one-js {
    right: 70px !important;
    background: #b91c1c !important;
}

.lead-block-one-js {
    right: 14px !important;
    background: #7f1d1d !important;
}

.lead-delete-one-js:hover,
.lead-block-one-js:hover {
    background: #991b1b !important;
}

.lead-head {
    padding-right: 135px !important;
}
/* === LEADBOT FINAL DELETE BLOCK POSITION OVERRIDE END === */
</style>



</head>
<body>
<div class="container">
    <div class="hero">
        <div class="leadbot-brand">
            <div class="leadbot-brand-left">
                <a class="leadbot-logo-link" href="/">
                    <img class="leadbot-logo" src="/static/leadmeleads-logo-blue.jpg?v=blue-logo-1" alt="LeadMeLeads Logo">
                </a>
                <div>
                    <h1>LeadBot Dashboard</h1>
                    <p>Find page 1–4 prospects, enrich contact details, and prioritize outreach opportunities.</p>
                </div>
            </div>

            <nav class="leadbot-nav">
                <a href="/">Home</a>
                <a href="#run-lead-bot">Start</a>
                <a href="#exports">Exports</a>
                <a href="/logout">Logout</a>
            </nav>
        </div>
    </div>

    
<div class="layout">
        <aside class="panel" id="run-lead-bot">
            <h2>Run LeadBot</h2>
            <form id="leadbotRunForm" action="/lead-bot/live-start" method="get">
                <label>Industry</label>
                <input name="industry" value="">

                <label>Market</label>
                <input name="market" value="">

                <label>Keyword</label>
                <input name="keyword" value="">

                <label>Own Domain</label>
                <input name="own_domain" value="">

                <label>Scan Size</label>
                <select id="scanSizePreset" class="leadbot-select">
                    <option value="preview">Preview — 1 lead</option>
                    <option value="quick">Quick — 8 leads</option>
                    <option value="standard" selected>Standard — 25 leads</option>
                    <option value="deep">Deep — 50 leads</option>
                </select>

                <input type="hidden" name="limit" id="leadbotLimit" value="25">
                <input type="hidden" name="per_query_limit" id="leadbotPerQueryLimit" value="12">
                <input type="hidden" name="max_queries" id="leadbotMaxQueries" value="12">

                <details class="leadbot-advanced">
                    <summary>Advanced settings</summary>

                    <label>Limit</label>
                    <input name="_limit_display" id="leadbotLimitDisplay" value="25">

                    <label>Per Query Limit</label>
                    <input name="_per_query_limit_display" id="leadbotPerQueryLimitDisplay" value="12">

                    <label>Max Queries</label>
                    <input name="_max_queries_display" id="leadbotMaxQueriesDisplay" value="12">
                </details>

<button type="submit" class="leadbot-start-btn" id="leadbotStartScanButton">
    Start LeadBot Scan
</button>

</form>

            <p class="help"><strong>LeadBot:</strong> live results appear as prospects are found and contacts are enriched.</p>


<div class="leadbot-complete-details-above-exports">
    <a class="leadbot-current-fill-addresses" id="leadbotCompleteAddresses" href="#">Enrich Website Details</a>
</div>

<div class="leadbot-complete-details-above-exports">
    <a class="leadbot-current-fill-addresses" href="/lead-bot/open-desktop">Open Desktop</a>
</div>
<h2 id="exports" style="margin-top:22px;">Exports</h2>

            <div class="file-list">__FILES__</div>
        
<div class="leadbot-current-scan-direct" id="leadbotCurrentScanDirect">
    <h2>Current Scan</h2>
    <div class="leadbot-current-scan-direct-name" id="leadbotCurrentScanName"></div>
    <p class="leadbot-current-scan-direct-count" id="leadbotCurrentScanCount"></p>
    <div class="leadbot-current-scan-direct-actions">
        <a class="leadbot-current-download" id="leadbotCurrentScanDownload" href="#">Download</a>
</div>
</div>

            <div class="leadbot-block-domains-box">
                <h2>Blocked Domains</h2>
                <form action="/lead-bot/block-domains" method="get">
                    <label>Domains or URLs</label>
                    <textarea name="domains" placeholder="weedmaps.com\nhttps://abc7ny.com/page\nwww.nypost.com"></textarea>
                    <button type="submit" class="leadbot-secondary-btn">Add to Blocked Domains</button>
                </form>
                <p class="help">Paste a domain or full URL. One per line works too.</p>
            </div>

</aside>

        <main class="panel" id="results">
            <section class="leads">

                __CARDS__
            </section>
        </main>
    </div>
</div>


<!-- === LEADBOT CSS SCANNER LOADER START === -->
<div class="leadbot-wait-video" id="leadbotWaitVideo">
    <div class="leadbot-scanner">
        <div class="leadbot-grid"></div>
        <div class="leadbot-scan-line"></div>
        <div class="leadbot-connection"></div>
        <div class="leadbot-node one"></div>
        <div class="leadbot-node two"></div>
        <div class="leadbot-node three"></div>
        <div class="leadbot-node four"></div>
        <div class="leadbot-mini-card">
            <span></span>
            <span></span>
            <span></span>
        </div>
    </div>
    <div class="leadbot-wait-copy">
        <strong>LeadBot is building your lead list...</strong>
        <span>Finding page 1–4 prospects, enriching contact details, and building your export.</span>
    </div>
</div>
<!-- === LEADBOT CSS SCANNER LOADER END === -->



<script>
(function () {
    function resetLeadBotState() {
        document.querySelectorAll("button, input[type='submit']").forEach(function (btn) {
            btn.disabled = false;
            if (btn.dataset.originalText) {
                btn.textContent = btn.dataset.originalText;
            }
        });

        document.querySelectorAll(".leadbot-wait, .leadbot-wait-panel, #leadbotWait, #leadBotWait, .wait-panel").forEach(function (el) {
            el.style.display = "none";
            el.classList.remove("is-active", "active", "show");
        });

        document.body.classList.remove("is-loading", "leadbot-loading");
    }

    window.addEventListener("pageshow", resetLeadBotState);
    window.addEventListener("focus", resetLeadBotState);

    document.addEventListener("submit", function (event) {
        var form = event.target;
        if (!form || !form.action || (form.action.indexOf("/lead-bot/live-start") === -1 && form.action.indexOf("/lead-bot/live-start") === -1)) {
            return;
        }

        form.querySelectorAll("button, input[type='submit']").forEach(function (btn) {
            if (!btn.dataset.originalText) {
                btn.dataset.originalText = btn.textContent || btn.value || "Fast Scan";
            }
            btn.disabled = true;
            if (btn.tagName.toLowerCase() === "button") {
                btn.textContent = "Scanning...";
            }
        });

        document.querySelectorAll(".leadbot-wait, .leadbot-wait-panel, #leadbotWait, #leadBotWait, .wait-panel").forEach(function (el) {
            el.style.display = "block";
            el.classList.add("is-active");
        });
    }, true);
})();


















</script>


<script>
(function () {
    function showLeadBotLoader(message) {
        const panel = document.getElementById("leadbotWaitVideo");

        if (panel) {
            panel.classList.add("is-active");
        }

        const copy = panel ? panel.querySelector(".leadbot-wait-copy span") : null;
        if (copy && message) {
            copy.textContent = message;
        }
    }

    function resetLeadBotLoader() {
        const panel = document.getElementById("leadbotWaitVideo");

        if (panel) {
            panel.classList.remove("is-active");
        }

        document.querySelectorAll("button, input[type='submit']").forEach(function (btn) {
            btn.disabled = false;

            if ((btn.textContent || "").includes("Running") || (btn.textContent || "").includes("Working")) {
                btn.textContent = "Fast Scan";
            }
        });
    }

    window.addEventListener("pageshow", resetLeadBotLoader);

    document.querySelectorAll('form[action="/lead-bot/live-start"], form[action="/lead-bot/live-start"]').forEach(function (form) {
        form.addEventListener("submit", function () {
            showLeadBotLoader("Finding page 1–4 prospects, enriching contact details, and building your export.");

            const button = form.querySelector('button[type="submit"]');
            if (button) {
                button.disabled = true;
                button.textContent = "Starting LeadBot...";
            }
        });
    });

    document.querySelectorAll('a[href*="/lead-bot/enrich/"]').forEach(function (link) {
        link.addEventListener("click", function () {
            showLeadBotLoader("Enriching contact details and updating this scan...");
        });
    });
})();
</script>


<script>
window.addEventListener("DOMContentLoaded", function () {
    const panel = document.getElementById("leadbotWaitVideo");
    if (panel) {
        panel.classList.remove("is-active");
    }
});

window.addEventListener("pageshow", function () {
    const panel = document.getElementById("leadbotWaitVideo");
    if (panel) {
        panel.classList.remove("is-active");
    }
});
</script>
\n

<script>
(function () {
    const preset = document.getElementById("scanSizePreset");
    const limit = document.getElementById("leadbotLimit");
    const perQuery = document.getElementById("leadbotPerQueryLimit");
    const maxQueries = document.getElementById("leadbotMaxQueries");

    const limitDisplay = document.getElementById("leadbotLimitDisplay");
    const perQueryDisplay = document.getElementById("leadbotPerQueryLimitDisplay");
    const maxQueriesDisplay = document.getElementById("leadbotMaxQueriesDisplay");

    function setValues(l, p, m) {
        if (limit) limit.value = l;
        if (perQuery) perQuery.value = p;
        if (maxQueries) maxQueries.value = m;

        if (limitDisplay) limitDisplay.value = l;
        if (perQueryDisplay) perQueryDisplay.value = p;
        if (maxQueriesDisplay) maxQueriesDisplay.value = m;
    }

    if (preset) {
        preset.addEventListener("change", function () {
            if (preset.value === "quick") setValues(8, 3, 3);
            if (preset.value === "standard") setValues(25, 5, 5);
            if (preset.value === "deep") setValues(50, 6, 8);
        });
    }

    [limitDisplay, perQueryDisplay, maxQueriesDisplay].forEach(function (input) {
        if (!input) return;

        input.addEventListener("input", function () {
            if (limit && limitDisplay) limit.value = limitDisplay.value || "25";
            if (perQuery && perQueryDisplay) perQuery.value = perQueryDisplay.value || "5";
            if (maxQueries && maxQueriesDisplay) maxQueries.value = maxQueriesDisplay.value || "5";
        });
    });
})();
</script>










<!-- DIRECT CURRENT SCAN BOX SCRIPT START -->
<script>
(function () {
    function currentFile() {
        const params = new URLSearchParams(window.location.search);
        let file = params.get("file") || "";

        if (!file) {
            const title = null;
            const titleText = title ? (title.textContent || "").trim() : "";
            if (titleText.endsWith(".csv")) file = titleText;
        }

        return file;
    }

    function installCurrentScan() {
        const file = currentFile();
        if (!file || file === "leadbot_master.csv") return;

        const box = document.getElementById("leadbotCurrentScanDirect");
        const name = document.getElementById("leadbotCurrentScanName");
        const count = document.getElementById("leadbotCurrentScanCount");
        const download = document.getElementById("leadbotCurrentScanDownload");

        if (!box || !name || !count || !download) return;

        const countText = null;
        name.textContent = file;
        count.textContent = countText ? (countText.textContent || "").trim() : "";

        download.href = "/lead-bot/export/" + encodeURIComponent(file);
        box.classList.add("is-active");
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", installCurrentScan);
    } else {
        installCurrentScan();
    }

    setTimeout(installCurrentScan, 300);
})();
</script>
<!-- DIRECT CURRENT SCAN BOX SCRIPT END -->


<script>
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll(".domain").forEach(function (domainEl) {
    if (domainEl.querySelector("a")) return;

    const raw = domainEl.textContent.trim();
    if (!raw || raw.toLowerCase() === "not found") return;

    let href = raw;
    if (!/^https?:\/\//i.test(href)) {
      href = "https://" + href;
    }

    const a = document.createElement("a");
    a.href = href;
    a.textContent = raw;
    a.target = "_blank";
    a.rel = "noopener noreferrer";

    domainEl.textContent = "";
    domainEl.appendChild(a);
  });
});
</script>






<!-- LEADBOT ADDRESS FORM FILENAME FIX START -->

<script>





// === LEADBOT REGULAR ADDRESS FIELD FOCUS FIX START ===
(function() {
    function isAddressArea(target) {
        return !!(
            target &&
            target.closest &&
            target.closest('form[action="/lead-bot/update-address"]')
        );
    }

    function isAddressInputFocused() {
        var active = document.activeElement;
        return !!(
            active &&
            active.closest &&
            active.closest('form[action="/lead-bot/update-address"]')
        );
    }

    window.leadbotAddressInputFocused = isAddressInputFocused;

    // Stop card toggles / accordion handlers from stealing focus while typing.
    ["click", "mousedown", "mouseup", "dblclick", "keydown", "keyup", "keypress", "input", "focusin"].forEach(function(eventName) {
        document.addEventListener(eventName, function(event) {
            if (!isAddressArea(event.target)) return;

            // Let the browser do normal input/form stuff. Just stop dashboard/card JS above it.
            event.stopPropagation();

            if (event.stopImmediatePropagation && eventName !== "submit") {
                event.stopImmediatePropagation();
            }
        }, true);
    });

    // Normal form submit: do not preventDefault. Just stop other dashboard submit handlers.
    document.addEventListener("submit", function(event) {
        if (!isAddressArea(event.target)) return;
        event.stopPropagation();
        if (event.stopImmediatePropagation) {
            event.stopImmediatePropagation();
        }
    }, true);

    // Pause dashboard cleanup/sort/re-wire timers while the user is editing an address.
    // This keeps "regular fields" from being replaced under your cursor.
    if (!window.leadbotOriginalSetInterval) {
        window.leadbotOriginalSetInterval = window.setInterval;
        window.setInterval = function(callback, delay) {
            var wrapped = callback;

            if (typeof callback === "function") {
                wrapped = function() {
                    if (window.leadbotAddressInputFocused && window.leadbotAddressInputFocused()) {
                        return;
                    }
                    return callback.apply(this, arguments);
                };
            }

            return window.leadbotOriginalSetInterval(wrapped, delay);
        };
    }
})();
// === LEADBOT REGULAR ADDRESS FIELD FOCUS FIX END ===

</script>
<script>
(function () {
    function currentFile() {
        const params = new URLSearchParams(window.location.search);
        let file = params.get("file") || "";

        if (!file) {
            const active = document.querySelector(".file-link.active");
            if (active) {
                const url = new URL(active.href, window.location.origin);
                file = url.searchParams.get("file") || "";
            }
        }

        return file;
    }

    function fillAddressFilenames() {
        const file = currentFile();
        if (!file) return;

        document.querySelectorAll('.lead-address-box form').forEach(function (form) {
            let input = form.querySelector('input[name="filename"]');

            if (!input) {
                input = document.createElement("input");
                input.type = "hidden";
                input.name = "filename";
                form.prepend(input);
            }

            input.value = file;
        });
    }

    document.addEventListener("submit", function (event) {
        const form = event.target;
        if (!form || form.action.indexOf("/lead-bot/update-address") === -1) return;
        fillAddressFilenames();
    }, true);

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", fillAddressFilenames);
    } else {
        fillAddressFilenames();
    }

    setTimeout(fillAddressFilenames, 300);
    setTimeout(fillAddressFilenames, 300);
    setTimeout(fillAddressFilenames, 1200);
    setTimeout(fillAddressFilenames, 2500);
})();
</script>
<!-- LEADBOT ADDRESS FORM FILENAME FIX END -->


<!-- LEADBOT CONTACT FORM FILENAME FIX START -->
<script>
(function () {
    function currentFile() {
        const params = new URLSearchParams(window.location.search);
        let file = params.get("file") || "";

        if (!file) {
            const active = document.querySelector(".file-link.active");
            if (active) {
                const url = new URL(active.href, window.location.origin);
                file = url.searchParams.get("file") || "";
            }
        }

        return file;
    }

    function fillEditableFilenames() {
        const file = currentFile();
        if (!file) return;

        document.querySelectorAll(
            '.lead-address-box form input[name="filename"], .lead-contact-edit-form input[name="filename"]'
        ).forEach(function (input) {
            input.value = file;
        });
    }

    document.addEventListener("submit", function (event) {
        const form = event.target;
        if (!form || !form.action) return;

        if (
            form.action.indexOf("/lead-bot/update-address") !== -1 ||
            form.action.indexOf("/lead-bot/update-contact-fields") !== -1 ||
            form.action.indexOf("/lead-bot/save-details") !== -1 ||
            form.action.indexOf("/lead-bot/save-details") !== -1
        ) {
            fillEditableFilenames();
        }
    }, true);

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", fillEditableFilenames);
    } else {
        fillEditableFilenames();
    }

    setTimeout(fillEditableFilenames, 300);
    setTimeout(fillEditableFilenames, 300);
    setTimeout(fillEditableFilenames, 1200);
    setTimeout(fillEditableFilenames, 2500);
})();
</script>
<!-- LEADBOT CONTACT FORM FILENAME FIX END -->


<!-- LEADBOT SORT CARDS BY PAGE POSITION START -->
<script>
(function () {
    function getPagePosition(card) {
        const text = card.textContent || "";

        const pageMatch = text.match(/Page\s+(\d+)/i);
        const posMatch = text.match(/Position\s+(\d+)/i);

        const page = pageMatch ? parseInt(pageMatch[1], 10) : 9999;
        const pos = posMatch ? parseInt(posMatch[1], 10) : 9999;

        return { page, pos };
    }

    function sortLeadCards() {
        const container = document.querySelector("#results .leads, .leads");
        if (!container) return;

        const cards = Array.from(container.querySelectorAll("article.lead-card"));
        if (cards.length < 2) return;

        cards.sort(function (a, b) {
            const aa = getPagePosition(a);
            const bb = getPagePosition(b);

            if (aa.page !== bb.page) return aa.page - bb.page;
            if (aa.pos !== bb.pos) return aa.pos - bb.pos;

            return 0;
        });

        cards.forEach(function (card) {
            container.appendChild(card);
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", sortLeadCards);
    } else {
        sortLeadCards();
    }

    setTimeout(sortLeadCards, 300);
    setTimeout(sortLeadCards, 1000);
})();
</script>
<!-- LEADBOT SORT CARDS BY PAGE POSITION END -->




<!-- LEADBOT FILL ADDRESS BUTTON SCRIPT START -->
<script>
(function () {
    function currentFile() {
        const params = new URLSearchParams(window.location.search);
        let file = params.get("file") || "";

        if (!file) {
            const active = document.querySelector(".file-link.active");
            if (active) {
                const url = new URL(active.href, window.location.origin);
                file = url.searchParams.get("file") || "";
            }
        }

        return file;
    }

    function wireCurrentScanBox() {
        const file = currentFile();
        if (!file || file === "leadbot_master.csv") return;

        const box = document.getElementById("leadbotCurrentScanDirect");
        const name = document.getElementById("leadbotCurrentScanName");
        const count = document.getElementById("leadbotCurrentScanCount");
        const download = document.getElementById("leadbotCurrentScanDownload");
        const complete = document.getElementById("leadbotCompleteAddresses");

        if (!box || !name || !download || !complete) return;

        name.textContent = file;
        if (count) count.textContent = "";
        download.href = "/lead-bot/export/" + encodeURIComponent(file);

        complete.textContent = "Enrich Website Details";
        complete.href = "/lead-bot/complete-details/" + encodeURIComponent(file);
        complete.onclick = function () {
            return confirm("Complete missing addresses for this scan? This may take a little while.");
        };

        box.classList.add("is-active");
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", wireCurrentScanBox);
    } else {
        wireCurrentScanBox();
    }

    setTimeout(wireCurrentScanBox, 300);
    setTimeout(wireCurrentScanBox, 300);
    setTimeout(wireCurrentScanBox, 1200);
    setTimeout(wireCurrentScanBox, 2500);
})();
</script>
<!-- LEADBOT FILL ADDRESS BUTTON SCRIPT END -->\n\n<!-- LEADBOT DETAILS RUNNING AUTO REFRESH START -->
<script>
(function () {
    const params = new URLSearchParams(window.location.search);
    const file = params.get("file") || "";
    const details = params.get("details") || "";

    if (!file || details !== "running") return;

    const key = "leadbot-details-refresh-done:" + file;

    if (sessionStorage.getItem(key) === "1") return;
    sessionStorage.setItem(key, "1");

    console.log("LeadBot details running: refreshing dashboard once after background cleanup starts.");

    setTimeout(function () {
        location.reload();
    }, 12000);
})();
</script>
<!-- LEADBOT DETAILS RUNNING AUTO REFRESH END -->






<!-- LEADBOT SCAN SIZE CONTROLLER START -->
<script>
(function () {
    const presets = {
        preview:  { limit: "1",  per: "1", queries: "1" },
        quick:    { limit: "8",  per: "8", queries: "3" },
        standard: { limit: "25", per: "12", queries: "12" },
        deep:     { limit: "50", per: "8", queries: "8" }
    };

    function setValue(id, value) {
        const el = document.getElementById(id);
        if (el) el.value = value;
    }

    function applyPreset(value) {
        const selected = presets[value] || presets.standard;

        setValue("leadbotLimit", selected.limit);
        setValue("leadbotPerQueryLimit", selected.per);
        setValue("leadbotMaxQueries", selected.queries);

        setValue("leadbotLimitDisplay", selected.limit);
        setValue("leadbotPerQueryLimitDisplay", selected.per);
        setValue("leadbotMaxQueriesDisplay", selected.queries);
    }

    function init() {
        const select = document.getElementById("scanSizePreset");
        if (!select) return;

        if (!select.value || select.value === "preview") {
            select.value = "standard";
        }

        applyPreset(select.value || "standard");

        select.addEventListener("change", function () {
            applyPreset(select.value || "standard");
        });

        const form = document.getElementById("leadbotRunForm");
        if (form) {
            form.addEventListener("submit", function () {
                applyPreset(select.value || "standard");
            });
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
</script>
<!-- LEADBOT SCAN SIZE CONTROLLER END -->


<!-- === LEADBOT ONE SAVE DETAILS BUTTON START === -->
<script>
(function () {
    function mergeLeadDetailForms() {
        document.querySelectorAll(".lead-card").forEach(function (card) {
            const contactForm = card.querySelector("form.lead-contact-edit-form");
            const addressBox = card.querySelector(".lead-address-box");
            if (!contactForm || !addressBox) return;

            contactForm.action = "/lead-bot/save-details";

            const addressInput = addressBox.querySelector('input[name="address"]');
            const addressForm = addressBox.querySelector("form");

            if (addressInput && !contactForm.querySelector('input[name="address"]')) {
                const newBox = document.createElement("div");
                newBox.className = "lead-address-box lead-address-box-merged";

                const label = document.createElement("b");
                label.textContent = "Address";

                newBox.appendChild(label);
                newBox.appendChild(addressInput);

                const saveButton = contactForm.querySelector(".lead-contact-save, button[type='submit']");
                if (saveButton) {
                    contactForm.insertBefore(newBox, saveButton);
                } else {
                    contactForm.appendChild(newBox);
                }
            }

            if (addressForm) {
                addressForm.remove();
            }

            const oldAddressButtons = addressBox.querySelectorAll("button");
            oldAddressButtons.forEach(function (button) {
                button.remove();
            });

            if (addressBox && !addressBox.classList.contains("lead-address-box-merged")) {
                addressBox.remove();
            }

            const button = contactForm.querySelector(".lead-contact-save, button[type='submit']");
            if (button) {
                button.textContent = "Save Details";
                button.classList.add("lead-save-details-final");
            }
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", mergeLeadDetailForms);
    } else {
        mergeLeadDetailForms();
    }
})();
</script>

<style>
.lead-save-details-final {
    margin-top: 12px !important;
    padding: 7px 12px !important;
    min-height: 28px !important;
    border-radius: 999px !important;
    background: #1e3a8a !important;
    color: #ffffff !important;
    font-size: 10px !important;
    line-height: 1 !important;
    font-weight: 950 !important;
    width: fit-content !important;
}

.lead-address-box-merged {
    margin-top: 12px !important;
}

.lead-address-box-merged input[name="address"] {
    width: 100% !important;
}
</style>
<!-- === LEADBOT ONE SAVE DETAILS BUTTON END === -->




<!-- LEADBOT CLICKABLE CARD LINKS START -->
<script>
(function () {
    function shouldSkip(node) {
        if (!node || !node.parentElement) return true;

        const tag = node.parentElement.tagName;
        if (["A", "BUTTON", "INPUT", "TEXTAREA", "SELECT", "OPTION", "SCRIPT", "STYLE"].includes(tag)) {
            return true;
        }

        if (node.parentElement.closest("a, button, input, textarea, select, script, style")) {
            return true;
        }

        return false;
    }

    function normalizeHref(value) {
        let raw = (value || "").trim();
        if (!raw) return "";

        raw = raw.replace(/[),.;:]+$/g, "");

        if (raw.startsWith("http://") || raw.startsWith("https://")) {
            return raw;
        }

        return "https://" + raw;
    }

    function linkifyTextNode(node) {
        if (shouldSkip(node)) return;

        const original = node.nodeValue || "";

        const domainRegex = /\b((?:https?:\/\/)?(?:www\.)?[a-z0-9][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)+\.[a-z]{2,}(?:\/[^\s<]*)?)/ig;

        if (!domainRegex.test(original)) return;
        domainRegex.lastIndex = 0;

        const frag = document.createDocumentFragment();
        let lastIndex = 0;
        let match;

        while ((match = domainRegex.exec(original)) !== null) {
            const matched = match[0];
            const start = match.index;

            if (start > lastIndex) {
                frag.appendChild(document.createTextNode(original.slice(lastIndex, start)));
            }

            const trailing = matched.match(/[),.;:]+$/);
            const cleanText = trailing ? matched.slice(0, -trailing[0].length) : matched;
            const trailText = trailing ? trailing[0] : "";

            const a = document.createElement("a");
            a.href = normalizeHref(cleanText);
            a.textContent = cleanText;
            a.target = "_blank";
            a.rel = "noopener noreferrer";
            a.style.fontWeight = "800";
            a.style.color = "#2563eb";
            a.style.textDecoration = "none";

            a.addEventListener("mouseenter", function () {
                a.style.textDecoration = "underline";
            });

            a.addEventListener("mouseleave", function () {
                a.style.textDecoration = "none";
            });

            frag.appendChild(a);

            if (trailText) {
                frag.appendChild(document.createTextNode(trailText));
            }

            lastIndex = start + matched.length;
        }

        if (lastIndex < original.length) {
            frag.appendChild(document.createTextNode(original.slice(lastIndex)));
        }

        node.parentNode.replaceChild(frag, node);
    }

    function linkifyLeadBotCards() {
        const roots = document.querySelectorAll(
            "#results, .leadbot-results, .lead-card, .leadbot-card, .leadbot-lead-card, .lead-result-card, .result-card, .card"
        );

        roots.forEach(function (root) {
            if (!root || root.dataset.leadbotLinksDone === "1") return;

            const walker = document.createTreeWalker(
                root,
                NodeFilter.SHOW_TEXT,
                {
                    acceptNode: function (node) {
                        if (shouldSkip(node)) return NodeFilter.FILTER_REJECT;
                        const value = node.nodeValue || "";
                        if (value.match(/\b(?:https?:\/\/)?(?:www\.)?[a-z0-9][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)+\.[a-z]{2,}/i)) {
                            return NodeFilter.FILTER_ACCEPT;
                        }
                        return NodeFilter.FILTER_REJECT;
                    }
                }
            );

            const nodes = [];
            while (walker.nextNode()) {
                nodes.push(walker.currentNode);
            }

            nodes.forEach(linkifyTextNode);
            root.dataset.leadbotLinksDone = "1";
        });
    }

    function bootLeadBotLinks() {
        linkifyLeadBotCards();
        setTimeout(linkifyLeadBotCards, 500);
        setTimeout(linkifyLeadBotCards, 1500);
        setTimeout(linkifyLeadBotCards, 3500);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", bootLeadBotLinks);
    } else {
        bootLeadBotLinks();
    }
})();
</script>
<!-- LEADBOT CLICKABLE CARD LINKS END -->


<!-- LEADBOT MAPS APPLE CARD KILLER START -->
<script>
(function () {
    function killMapsAppleCards() {
        const badBits = [
            "maps.apple.com",
            "apple maps"
        ];

        const selectors = [
            ".lead-card",
            ".leadbot-card",
            ".leadbot-lead-card",
            ".lead-result-card",
            ".result-card",
            ".card",
            "article",
            "tr"
        ];

        selectors.forEach(function (selector) {
            document.querySelectorAll(selector).forEach(function (el) {
                const txt = (el.textContent || "").toLowerCase();
                const html = (el.innerHTML || "").toLowerCase();

                if (badBits.some(bit => txt.includes(bit) || html.includes(bit))) {
                    el.remove();
                }
            });
        });
    }

    function bootMapsAppleKiller() {
        killMapsAppleCards();
        setTimeout(killMapsAppleCards, 500);
        setTimeout(killMapsAppleCards, 1500);
        setTimeout(killMapsAppleCards, 3500);
        setTimeout(killMapsAppleCards, 7000);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", bootMapsAppleKiller);
    } else {
        bootMapsAppleKiller();
    }
})();
</script>
<!-- LEADBOT MAPS APPLE CARD KILLER END -->






<!-- LEADBOT JS ONE DELETE BUTTON START -->
<script>
(function () {
    function cleanDomain(value) {
        value = String(value || "").trim().toLowerCase();
        value = value.replace(/^https?:\/\//, "");
        value = value.replace(/^www\./, "");
        value = value.split("/")[0].split("?")[0].split("#")[0];
        value = value.replace(/[),.;:]+$/g, "");
        return value.includes(".") ? value : "";
    }

    function getSelectedFile() {
        const params = new URLSearchParams(window.location.search);
        let file = params.get("file");

        if (!file) {
            const active = document.querySelector(".file-link.active");
            if (active) {
                const url = new URL(active.href, window.location.origin);
                file = url.searchParams.get("file");
            }
        }

        return file || "";
    }

    function addDeleteAndBlockButtons() {
        const selectedFile = getSelectedFile();

        if (!selectedFile || selectedFile === "leadbot_master.csv") {
            return;
        }

        document.querySelectorAll(".lead-card").forEach(function (card) {
            const domainEl = card.querySelector(".domain");
            const domain = cleanDomain(domainEl ? domainEl.textContent : "");

            if (!domain) return;

            if (!card.querySelector(".lead-delete-one-js")) {
                const del = document.createElement("a");
                del.className = "lead-delete-one-js";
                del.title = "Delete lead";
                del.textContent = "Delete";
                del.href = "/lead-bot/delete-row/" + encodeURIComponent(selectedFile) + "?domain=" + encodeURIComponent(domain);
                del.onclick = function () {
                    return confirm("Delete this lead from this export?");
                };
                card.appendChild(del);
            }

            if (!card.querySelector(".lead-block-one-js")) {
                const block = document.createElement("a");
                block.className = "lead-block-one-js";
                block.title = "Block domain";
                block.textContent = "Block";
                block.href = "/lead-bot/block-domains?domains=" + encodeURIComponent(domain);
                block.onclick = function () {
                    return confirm("Block " + domain + " from future LeadBot scans?");
                };
                card.appendChild(block);
            }
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", addDeleteAndBlockButtons);
    } else {
        addDeleteAndBlockButtons();
    }

    setTimeout(addDeleteAndBlockButtons, 250);
    setTimeout(addDeleteAndBlockButtons, 1000);
    setTimeout(addDeleteAndBlockButtons, 2500);
})();
</script>
<!-- LEADBOT JS ONE DELETE BUTTON END -->

<!-- LEADBOT DASHBOARD ACTION ROW MATCH LIVE SCAN START -->
<script>
(function () {
    function moveDashboardActions() {
        document.querySelectorAll(".lead-card").forEach(function (card) {
            const deleteBtn = card.querySelector(".lead-delete-one-js");
            const blockBtn = card.querySelector(".lead-block-one-js");

            if (!deleteBtn && !blockBtn) return;

            let row = card.querySelector(".leadbot-card-action-row");
            if (!row) {
                row = document.createElement("div");
                row.className = "leadbot-card-action-row";

                const reason = card.querySelector(".reason");
                if (reason && reason.parentNode === card) {
                    card.insertBefore(row, reason);
                } else {
                    card.appendChild(row);
                }
            }

            if (deleteBtn && deleteBtn.parentNode !== row) row.appendChild(deleteBtn);
            if (blockBtn && blockBtn.parentNode !== row) row.appendChild(blockBtn);
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", moveDashboardActions);
    } else {
        moveDashboardActions();
    }

    setTimeout(moveDashboardActions, 250);
    setTimeout(moveDashboardActions, 1000);
    setTimeout(moveDashboardActions, 2500);
})();
</script>
<!-- LEADBOT DASHBOARD ACTION ROW MATCH LIVE SCAN END -->


<!-- LEADBOT DASHBOARD NO CONFIRM BLOCK START -->
<script>
(function () {
    function closestLeadCard(el) {
        return el.closest(".lead-card") || el.closest(".card") || el.closest("article") || el.closest("tr");
    }

    document.addEventListener("click", function (event) {
        const btn = event.target.closest(".lead-block-one-js");

        if (!btn) return;

        event.preventDefault();
        event.stopPropagation();

        if (btn.dataset.busy === "1") return;

        const oldText = btn.textContent;
        const href = btn.getAttribute("href");

        if (!href) return;

        btn.dataset.busy = "1";
        btn.textContent = "Blocked";
        btn.style.opacity = "0.65";
        btn.style.pointerEvents = "none";

        fetch(href, {
            method: "GET",
            cache: "no-store",
            credentials: "same-origin"
        })
        .then(function () {
            const card = closestLeadCard(btn);

            if (card) {
                card.style.transition = "opacity .18s ease, transform .18s ease, max-height .25s ease, margin .25s ease, padding .25s ease";
                card.style.opacity = "0";
                card.style.transform = "translateY(4px)";
                card.style.maxHeight = "0";
                card.style.marginTop = "0";
                card.style.marginBottom = "0";
                card.style.paddingTop = "0";
                card.style.paddingBottom = "0";
                card.style.overflow = "hidden";

                setTimeout(function () {
                    card.remove();
                }, 260);
            }
        })
        .catch(function () {
            btn.dataset.busy = "0";
            btn.textContent = oldText || "Block";
            btn.style.opacity = "";
            btn.style.pointerEvents = "";
            alert("Block failed. Try again.");
        });
    }, true);
})();
</script>
<!-- LEADBOT DASHBOARD NO CONFIRM BLOCK END -->


<!-- LEADBOT SEO SNAPSHOT UNDER ADDRESS START -->
<!-- Real SEO Snapshot is rendered server-side from CSV fields only. No fake placeholder. -->
<!-- LEADBOT SEO SNAPSHOT UNDER ADDRESS END -->


<!-- LEADBOT CARD BUTTON LAYOUT CLEANUP START -->
<script>
(function () {
    function isSaveButton(button) {
        if (!button) return false;

        const text = String(button.textContent || button.value || "").trim().toLowerCase();

        return (
            text === "save details" ||
            text === "save contact info" ||
            text === "save address" ||
            text.includes("save details") ||
            text.includes("save contact") ||
            text.includes("save address")
        );
    }

    function moveSaveDetailsAboveSiteTitle(card) {
        const seoBox = card.querySelector(".leadbot-seo-snapshot");
        const addressBox = card.querySelector(".lead-address-box");

        if (!seoBox && !addressBox) return;

        const buttons = Array.from(card.querySelectorAll("button, input[type='submit']"))
            .filter(isSaveButton);

        if (!buttons.length) return;

        let row = card.querySelector(".leadbot-save-details-row");

        if (!row) {
            row = document.createElement("div");
            row.className = "leadbot-save-details-row";
        }

        buttons.forEach(function (button) {
            if (button.closest(".leadbot-save-details-row")) return;
            row.appendChild(button);
        });

        if (seoBox && row.parentNode !== card) {
            seoBox.insertAdjacentElement("beforebegin", row);
        } else if (addressBox && row.parentNode !== card) {
            addressBox.insertAdjacentElement("afterend", row);
        }
    }

    function moveDeleteBlockUnderWhyThisLead(card) {
        const reason = card.querySelector(".reason");
        const deleteBtn = card.querySelector(".lead-delete-one-js");
        const blockBtn = card.querySelector(".lead-block-one-js");

        if (!reason || (!deleteBtn && !blockBtn)) return;

        let row = card.querySelector(".leadbot-card-action-row");

        if (!row) {
            row = document.createElement("div");
            row.className = "leadbot-card-action-row";
        }

        if (deleteBtn && deleteBtn.parentNode !== row) row.appendChild(deleteBtn);
        if (blockBtn && blockBtn.parentNode !== row) row.appendChild(blockBtn);

        if (reason.nextElementSibling !== row) {
            reason.insertAdjacentElement("afterend", row);
        }
    }

    function cleanLeadBotCardLayout() {
        document.querySelectorAll(".lead-card, .card, article").forEach(function (card) {
            moveSaveDetailsAboveSiteTitle(card);
            moveDeleteBlockUnderWhyThisLead(card);
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", cleanLeadBotCardLayout);
    } else {
        cleanLeadBotCardLayout();
    }

    setTimeout(cleanLeadBotCardLayout, 300);
    setTimeout(cleanLeadBotCardLayout, 1000);
    setTimeout(cleanLeadBotCardLayout, 2500);
    setTimeout(cleanLeadBotCardLayout, 300);
    setTimeout(cleanLeadBotCardLayout, 1200);
    setTimeout(cleanLeadBotCardLayout, 2500);
})();
</script>
<!-- LEADBOT CARD BUTTON LAYOUT CLEANUP END -->


<!-- LEADBOT SELECTED EXPORT FALLBACK LOADER START -->
<script>
(function () {
    if (window.__leadbotSelectedExportFallbackInstalled) return;
    window.__leadbotSelectedExportFallbackInstalled = true;

    function loadSelectedExportIfEmpty() {
        try {
            const params = new URLSearchParams(window.location.search || "");
            const selectedFile = params.get("file");

            if (!selectedFile) return;

            const emptyBox = document.querySelector("#results .empty, .empty");
            const leadsBox = document.querySelector("#results .leads, .leads");
            const alreadyHasCards = document.querySelector(".lead-card");

            if (alreadyHasCards) return;

            if (!emptyBox && leadsBox && leadsBox.children.length > 0) return;

            fetch("/lead-bot/cards/" + encodeURIComponent(selectedFile), {
                cache: "no-store",
                credentials: "same-origin"
            })
            .then(function (res) {
                return res.text();
            })
            .then(function (html) {
                html = String(html || "").trim();

                if (!html || html.indexOf("Selected export not found") !== -1 || html.indexOf("Could not load selected export") !== -1) {
                    return;
                }

                if (html.indexOf("lead-card") === -1) {
                    return;
                }

                let target = leadsBox;

                if (!target) {
                    const results = document.querySelector("#results");
                    if (results) {
                        target = document.createElement("div");
                        target.className = "leads";
                        results.appendChild(target);
                    }
                }

                if (target) {
                    target.innerHTML = html;
                    if (emptyBox) emptyBox.remove();
                }
            })
            .catch(function (err) {
                console.log("LeadBot selected export fallback failed:", err);
            });
        } catch (err) {
            console.log("LeadBot selected export fallback error:", err);
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", loadSelectedExportIfEmpty);
    } else {
        loadSelectedExportIfEmpty();
    }
})();
</script>
<!-- LEADBOT SELECTED EXPORT FALLBACK LOADER END -->


<!-- LEADBOT DIRECT EXPORT DELETE BUTTONS START -->
<script>
(function () {
    if (window.__leadbotDirectExportDeleteButtonsInstalled) return;
    window.__leadbotDirectExportDeleteButtonsInstalled = true;

    function selectedFile() {
        const params = new URLSearchParams(window.location.search || "");
        return params.get("file") || "";
    }

    function filenameFromRow(row) {
        const csv = row.querySelector('.export-csv-link[href*="/lead-bot/export/"]');

        if (csv) {
            const href = csv.getAttribute("href") || "";
            const parts = href.split("/lead-bot/export/");

            if (parts.length > 1) {
                return decodeURIComponent(parts[1].split("?")[0].split("#")[0]);
            }
        }

        const file = row.querySelector('.file-link[href*="file="]');

        if (file) {
            try {
                const url = new URL(file.href, window.location.origin);
                return url.searchParams.get("file") || "";
            } catch (err) {}
        }

        return "";
    }

    function removeRow(row) {
        row.style.transition = "opacity .18s ease, transform .18s ease, max-height .25s ease, margin .25s ease, padding .25s ease";
        row.style.opacity = "0";
        row.style.transform = "translateY(4px)";
        row.style.maxHeight = "0";
        row.style.marginTop = "0";
        row.style.marginBottom = "0";
        row.style.paddingTop = "0";
        row.style.paddingBottom = "0";
        row.style.overflow = "hidden";

        setTimeout(function () {
            row.remove();
        }, 260);
    }

    function installExportDeleteButtons() {
        document.querySelectorAll(".export-file-row").forEach(function (row) {
            if (row.querySelector(".export-delete-link")) return;

            const filename = filenameFromRow(row);
            if (!filename) return;

            const btn = document.createElement("a");
            btn.className = "export-delete-link";
            btn.textContent = "Remove";
            btn.title = "Delete this export";
            btn.href = "/lead-bot/delete-export/" + encodeURIComponent(filename);

            btn.addEventListener("click", function (event) {
                event.preventDefault();
                event.stopPropagation();

                if (btn.dataset.busy === "1") return;
btn.dataset.busy = "1";
                btn.textContent = "Deleted";
                btn.style.opacity = "0.65";
                btn.style.pointerEvents = "none";

                fetch(btn.href, {
                    method: "GET",
                    cache: "no-store",
                    credentials: "same-origin",
                    redirect: "follow"
                })
                .then(function () {
                    if (selectedFile() === filename) {
                        window.location.href = "/lead-bot";
                        return;
                    }

                    removeRow(row);
                })
                .catch(function () {
                    btn.dataset.busy = "0";
                    btn.textContent = "Remove";
                    btn.style.opacity = "";
                    btn.style.pointerEvents = "";
                    alert("Delete failed. Try again.");
                });
            }, true);

            row.appendChild(btn);
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", installExportDeleteButtons);
    } else {
        installExportDeleteButtons();
    }

    setTimeout(installExportDeleteButtons, 300);
    setTimeout(installExportDeleteButtons, 1000);
    setTimeout(installExportDeleteButtons, 300);
    setTimeout(installExportDeleteButtons, 1200);
    setTimeout(installExportDeleteButtons, 2500);
})();
</script>

<style>
.export-file-row {
    grid-template-columns: minmax(0, 1fr) auto auto !important;
}

.export-delete-link {
    display: inline-flex !important;
    align-items: center !important;
    justify-content: center !important;
    min-height: 28px !important;
    padding: 6px 9px !important;
    border-radius: 999px !important;
    background: #fee2e2 !important;
    color: #991b1b !important;
    font-size: 10px !important;
    font-weight: 950 !important;
    line-height: 1 !important;
    text-decoration: none !important;
    border: 1px solid #fecaca !important;
    box-shadow: 0 6px 14px rgba(153, 27, 27, .10) !important;
    cursor: pointer !important;
    white-space: nowrap !important;
    flex: 0 0 auto !important;
}

.export-delete-link:hover {
    background: #fecaca !important;
    color: #7f1d1d !important;
    text-decoration: none !important;
}
</style>
<!-- LEADBOT DIRECT EXPORT DELETE BUTTONS END -->



<!-- Removed duplicate sidebar repair block: <!-- LEADBOT RESTORE OPEN DESKTOP LINKS START 





<!-- LEADBOT FINAL PAGE 1 REFERENCE SORT OVERRIDE START -->
<script>
(function () {
    if (window.__leadbotFinalPageOneReferenceSortInstalled) return;
    window.__leadbotFinalPageOneReferenceSortInstalled = true;

    function getPagePosition(card) {
        const text = card.textContent || "";

        const pageMatch = text.match(/Page\s+(\d+)/i);
        const posMatch = text.match(/Position\s+(\d+)/i);

        return {
            page: pageMatch ? parseInt(pageMatch[1], 10) : 9999,
            pos: posMatch ? parseInt(posMatch[1], 10) : 9999
        };
    }

    function addReferenceBadge(card) {
        const badges = card.querySelector(".badges");
        if (!badges) return;
        if (badges.querySelector(".leadbot-page-one-reference-note")) return;

        const badge = document.createElement("span");
        badge.className = "leadbot-page-one-reference-note";
        badge.textContent = "Page 1 Reference";
        badges.appendChild(badge);
    }

    function finalSortLeadCards() {
        const container = document.querySelector("#results .leads, .leads");
        if (!container) return;

        const cards = Array.from(container.querySelectorAll("article.lead-card"));
        if (!cards.length) return;

        cards.forEach(function (card) {
            const pp = getPagePosition(card);

            // Top 5 page-one results are usually winners/reference noise.
            if (pp.page === 1 && pp.pos >= 1 && pp.pos <= 5) {
                card.style.display = "none";
                card.dataset.leadbotHiddenPageOneTopFive = "1";
                return;
            }

            card.style.display = "";

            // Keep lower page-one results only as bottom reference.
            if (pp.page === 1) {
                card.dataset.leadbotPageOneReference = "1";
                addReferenceBadge(card);
            }
        });

        const visible = cards.filter(function (card) {
            return card.style.display !== "none";
        });

        visible.sort(function (a, b) {
            const aa = getPagePosition(a);
            const bb = getPagePosition(b);

            const aPageOne = aa.page === 1;
            const bPageOne = bb.page === 1;

            // Push all remaining page-one cards to the end.
            if (aPageOne && !bPageOne) return 1;
            if (!aPageOne && bPageOne) return -1;

            if (aa.page !== bb.page) return aa.page - bb.page;
            if (aa.pos !== bb.pos) return aa.pos - bb.pos;

            return 0;
        });

        visible.forEach(function (card) {
            container.appendChild(card);
        });
    }

    function bootFinalSort() {
        finalSortLeadCards();
        setTimeout(finalSortLeadCards, 250);
        setTimeout(finalSortLeadCards, 1000);
        setTimeout(finalSortLeadCards, 2500);
        setTimeout(finalSortLeadCards, 5000);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", bootFinalSort);
    } else {
        bootFinalSort();
    }

    setTimeout(finalSortLeadCards, 300);
    setTimeout(finalSortLeadCards, 1200);
    setTimeout(finalSortLeadCards, 2500);
})();
</script>

<style>
.leadbot-page-one-reference-note {
    background: #fef3c7 !important;
    color: #92400e !important;
    border-color: #fcd34d !important;
}
</style>
<!-- LEADBOT FINAL PAGE 1 REFERENCE SORT OVERRIDE END -->




<!-- LEADBOT AUTO COMPLETE DETAILS ON DASHBOARD START -->
<script>
(function () {
    if (window.__leadbotAutoCompleteDetailsInstalled) return;
    window.__leadbotAutoCompleteDetailsInstalled = true;

    function currentFile() {
        try {
            const params = new URLSearchParams(window.location.search || "");
            let file = params.get("file") || "";

            if (!file) {
                const active = document.querySelector(".file-link.active");
                if (active) {
                    const url = new URL(active.href, window.location.origin);
                    file = url.searchParams.get("file") || "";
                }
            }

            return file;
        } catch (err) {
            return "";
        }
    }

    function hasMissingAddresses() {
        const bodyText = String(document.body ? document.body.innerText || "" : "").toLowerCase();

        if (
            bodyText.includes("address not found") ||
            bodyText.includes("address missing") ||
            bodyText.includes("missing address") ||
            bodyText.includes("complete addresses")
        ) {
            return true;
        }

        const addressFields = Array.from(document.querySelectorAll(
            'input[name*="address" i], textarea[name*="address" i], input[id*="address" i], textarea[id*="address" i]'
        ));

        return addressFields.some(function (field) {
            return !String(field.value || "").trim();
        });
    }

    function autoCompleteDetails() {
        try {
            const file = currentFile();
            if (!file || file === "leadbot_master.csv") return;

            const lowerFile = String(file).toLowerCase();

            // Important money guard:
            // fresh/raw export can auto-complete.
            // already-enriched export must NOT auto-run again on refresh.
            if (lowerFile.includes("_enriched_")) {
                console.log("LeadBot auto Enrich Website Details skipped enriched export:", file);
                return;
            }

            const params = new URLSearchParams(window.location.search || "");
            if (params.get("details") === "running") return;
            if (params.get("addresses") === "complete") return;

            if (!hasMissingAddresses()) return;

            const storageKey = "leadbot:auto-complete-addresses:" + file;
            try {
                if (window.localStorage && localStorage.getItem(storageKey) === "done") {
                    console.log("LeadBot auto Enrich Website Details already attempted:", file);
                    return;
                }
            } catch (err) {}

            if (window.__leadbotAutoCompleteDetailsStartedThisLoad) return;
            window.__leadbotAutoCompleteDetailsStartedThisLoad = true;

            try {
                if (window.localStorage) localStorage.setItem(storageKey, "done");
            } catch (err) {}

            const url = "/lead-bot/complete-details/" + encodeURIComponent(file);

            console.log("LeadBot auto Enrich Website Details running on fresh export:", file);

            fetch(url, {
                method: "GET",
                credentials: "same-origin",
                cache: "no-store",
                redirect: "follow"
            }).then(function () {
                const nextUrl = "/lead-bot?file=" + encodeURIComponent(file) + "&addresses=complete#results";
                window.location.href = nextUrl;
            }).catch(function (err) {
                console.log("LeadBot auto Enrich Website Details failed:", err);
            });
        } catch (err) {
            console.log("LeadBot auto Enrich Website Details error:", err);
        }
    }

    function bootAutoCompleteDetails() {
        setTimeout(autoCompleteDetails, 1200);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", bootAutoCompleteDetails);
    } else {
        bootAutoCompleteDetails();
    }
})();
</script>
<!-- LEADBOT AUTO COMPLETE DETAILS ON DASHBOARD END -->




<!-- LEADBOT FLASHY EXPORT DELETE START -->
<script>
(function () {
    if (window.__leadbotFlashyExportDeleteInstalled) return;
    window.__leadbotFlashyExportDeleteInstalled = true;

    function closestRow(el) {
        return el.closest(".export-file-row") || el.closest("li") || el.parentElement;
    }

    function wireFlashyExportDeletes() {
        const buttons = document.querySelectorAll(".export-delete, .export-delete-link, a[href*='/lead-bot/delete-export/'], a[href*='delete_export']");

        buttons.forEach(function (btn) {
            if (btn.dataset.flashyExportDeleteWired === "1") return;
            btn.dataset.flashyExportDeleteWired = "1";

            btn.addEventListener("click", function (event) {
                const href = btn.getAttribute("href") || btn.dataset.href || "";
                if (!href || href === "#") return;

                event.preventDefault();
                event.stopPropagation();

                const row = closestRow(btn);
                const oldText = btn.textContent;

                btn.textContent = "Removing";
                btn.style.pointerEvents = "none";

                if (row) row.classList.add("is-removing");

                fetch(href, {
                    method: "GET",
                    credentials: "same-origin",
                    cache: "no-store",
                    redirect: "follow"
                }).then(function () {
                    if (row) {
                        row.classList.remove("is-removing");
                        row.classList.add("is-removed");
                        setTimeout(function () {
                            row.remove();
                        }, 240);
                    }
                }).catch(function (err) {
                    console.log("LeadBot flashy export delete failed:", err);

                    if (row) row.classList.remove("is-removing");
                    btn.textContent = oldText || "Remove";
                    btn.style.pointerEvents = "";
                    alert("Could not remove export. Try again.");
                });
            }, true);
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", wireFlashyExportDeletes);
    } else {
        wireFlashyExportDeletes();
    }

    setTimeout(wireFlashyExportDeletes, 300);
    setTimeout(wireFlashyExportDeletes, 1200);
    setTimeout(wireFlashyExportDeletes, 2500);
})();
</script>
<!-- LEADBOT FLASHY EXPORT DELETE END -->
















<!-- LEADBOT MODERN LEAD DELETE UI START -->
<script>
(function () {
    if (window.__leadbotModernLeadDeleteUiInstalled) return;
    window.__leadbotModernLeadDeleteUiInstalled = true;

    function cleanDomain(value) {
        value = String(value || "").trim().toLowerCase();
        value = value.replace(/^https?:\/\//, "");
        value = value.replace(/^www\./, "");
        value = value.split("/")[0].split("?")[0].split("#")[0];
        value = value.replace(/[),.;:]+$/g, "");
        return value.includes(".") ? value : "";
    }

    function selectedFile() {
        const params = new URLSearchParams(window.location.search || "");
        let file = params.get("file") || "";

        if (!file) {
            const active = document.querySelector(".file-link.active");
            if (active) {
                try {
                    const url = new URL(active.href, window.location.origin);
                    file = url.searchParams.get("file") || "";
                } catch (err) {}
            }
        }

        return file;
    }

    function cardFromButton(btn) {
        return btn.closest(".lead-card") || btn.closest(".card") || btn.closest("article");
    }

    function domainFromCard(card) {
        if (!card) return "";

        const domainEl = card.querySelector(".domain");
        const domainText = domainEl ? domainEl.textContent : "";

        return cleanDomain(domainText);
    }

    document.addEventListener("click", function (event) {
        const btn = event.target.closest(".lead-delete-one-js");

        if (!btn) return;

        event.preventDefault();
        event.stopPropagation();

        const card = cardFromButton(btn);
        const file = selectedFile();
        const domain = domainFromCard(card);

        if (!file || !domain) {
            btn.classList.add("is-deleting");
            btn.textContent = "Missing info";
            setTimeout(function () {
                btn.classList.remove("is-deleting");
                btn.textContent = "Delete";
            }, 1200);
            return false;
        }

        btn.dataset.originalText = btn.dataset.originalText || btn.textContent || "Delete";
        btn.classList.add("is-deleting");
        btn.textContent = "Deleting...";
        btn.style.pointerEvents = "none";

        if (card) {
            card.classList.add("is-delete-starting");
        }

        fetch(
            "/lead-bot/delete-row-safe?filename=" + encodeURIComponent(file) + "&domain=" + encodeURIComponent(domain),
            {
                method: "POST",
                cache: "no-store",
                credentials: "same-origin"
            }
        )
        .then(function (res) {
            return res.json().then(function (data) {
                return { ok: res.ok, data: data };
            });
        })
        .then(function (result) {
            if (!result.ok || !result.data || !result.data.ok) {
                throw new Error((result.data && result.data.error) || "Delete failed.");
            }

            btn.classList.remove("is-deleting");
            btn.classList.add("is-deleted");
            btn.textContent = "Deleted";

            if (card) {
                card.classList.remove("is-delete-starting");
                card.classList.add("is-delete-success");

                setTimeout(function () {
                    card.classList.add("is-delete-removing");
                }, 260);

                setTimeout(function () {
                    card.remove();
                }, 620);
            }
        })
        .catch(function (err) {
            btn.classList.remove("is-deleting", "is-deleted");
            btn.textContent = "Failed";
            btn.style.pointerEvents = "";

            if (card) {
                card.classList.remove("is-delete-starting", "is-delete-success", "is-delete-removing");
            }

            setTimeout(function () {
                btn.textContent = btn.dataset.originalText || "Delete";
            }, 1200);

            alert("Delete failed: " + err.message);
        });

        return false;
    }, true);
})();
</script>
<!-- LEADBOT MODERN LEAD DELETE UI END -->


<!-- LEADBOT MODERN EXPORT DELETE UI START -->
<script>
(function () {
    if (window.__leadbotModernExportDeleteUiInstalled) return;
    window.__leadbotModernExportDeleteUiInstalled = true;

    function exportRowFromButton(btn) {
        return btn.closest(".export-file-row") || btn.closest("li") || btn.closest("div");
    }

    function getDeleteUrl(btn) {
        var url =
            btn.getAttribute("href") ||
            btn.getAttribute("data-url") ||
            btn.getAttribute("data-delete-url") ||
            "";

        if (!url) {
            var form = btn.closest("form");
            if (form) url = form.getAttribute("action") || "";
        }

        return url;
    }

    function setButtonText(btn, text) {
        if (!btn) return;

        if (!btn.dataset.originalHtml) {
            btn.dataset.originalHtml = btn.innerHTML || "×";
        }

        btn.textContent = text;
    }

    document.addEventListener("click", function (event) {
        var btn = event.target.closest(".export-delete, .export-delete-link, a[href*='delete-export'], a[href*='delete_export'], a[href*='delete-file'], a[href*='delete_file']");
        if (event.target.closest(".lead-delete-one-js, .lead-delete-one, .lead-card")) return;

        if (!btn) return;

        event.preventDefault();
        event.stopPropagation();
        event.stopImmediatePropagation();

        if (btn.__leadbotExportDeleteBusy) return false;
        btn.__leadbotExportDeleteBusy = true;

        var url = getDeleteUrl(btn);
        var row = exportRowFromButton(btn);

        if (!url) {
            setButtonText(btn, "!");
            setTimeout(function () {
                btn.innerHTML = btn.dataset.originalHtml || "×";
            }, 1000);
            return false;
        }

        btn.classList.add("is-deleting");
        setButtonText(btn, "…");
        btn.style.pointerEvents = "none";

        if (row) {
            row.classList.add("is-export-delete-starting");
        }

        fetch(url, {
            method: "GET",
            cache: "no-store",
            credentials: "same-origin",
            redirect: "follow"
        })
        .then(function (res) {
            if (!res.ok) {
                throw new Error("Export delete failed: HTTP " + res.status);
            }

            btn.classList.remove("is-deleting");
            btn.classList.add("is-deleted");
            setButtonText(btn, "✓");

            if (row) {
                row.classList.remove("is-export-delete-starting");
                row.classList.add("is-export-delete-success");

                setTimeout(function () {
                    row.classList.add("is-export-delete-removing");
                }, 240);

                setTimeout(function () {
                    row.remove();
                }, 620);
            }
        })
        .catch(function (err) {
            btn.__leadbotExportDeleteBusy = false;
            btn.classList.remove("is-deleting", "is-deleted");
            btn.style.pointerEvents = "";
            setButtonText(btn, "!");
            if (row) {
                row.classList.remove(
                    "is-export-delete-starting",
                    "is-export-delete-success",
                    "is-export-delete-removing"
                );
            }

            setTimeout(function () {
                btn.innerHTML = btn.dataset.originalHtml || "×";
            }, 1200);

            alert(err.message || "Export delete failed.");
        });

        return false;
    }, true);
})();
</script>
<!-- LEADBOT MODERN EXPORT DELETE UI END -->




<!-- LEADBOT HIDE EXPORT X ONLY KEEP REMOVE START -->
<script>
(function () {
    if (window.__leadbotHideExportXOnlyKeepRemoveInstalled) return;
    window.__leadbotHideExportXOnlyKeepRemoveInstalled = true;

    function hideOnlyExportXButtons() {
        document.querySelectorAll("#run-lead-bot .export-delete-x").forEach(function (btn) {
            try { btn.remove(); } catch (e) {}
        });
    }

    hideOnlyExportXButtons();

    if (window.MutationObserver) {
        const observer = new MutationObserver(hideOnlyExportXButtons);
        observer.observe(document.documentElement, {
            childList: true,
            subtree: true
        });
    }

    setTimeout(hideOnlyExportXButtons, 100);
    setTimeout(hideOnlyExportXButtons, 500);
    setTimeout(hideOnlyExportXButtons, 1500);
    setTimeout(hideOnlyExportXButtons, 300);
    setTimeout(hideOnlyExportXButtons, 1200);
    setTimeout(hideOnlyExportXButtons, 2500);
})();
</script>
<!-- LEADBOT HIDE EXPORT X ONLY KEEP REMOVE END -->


<!-- LEADBOT DASHBOARD ADDRESS LIVE FILL START -->
<script>
(function () {
    if (window.__leadbotDashboardAddressLiveFillInstalled) return;
    window.__leadbotDashboardAddressLiveFillInstalled = true;

    const params = new URLSearchParams(window.location.search || "");
    const file = params.get("file") || "";

    if (!file) return;

    function normalizeDomain(value) {
        return String(value || "")
            .trim()
            .toLowerCase()
            .replace(/^https?:\/\//, "")
            .replace(/^www\./, "")
            .split("/")[0]
            .split("?")[0]
            .split("#")[0]
            .replace(/[^\w.-]/g, "");
    }

    function splitCsvLine(line) {
        const out = [];
        let current = "";
        let inQuotes = false;

        for (let i = 0; i < line.length; i++) {
            const ch = line[i];

            if (ch === '"' && line[i + 1] === '"') {
                current += '"';
                i++;
                continue;
            }

            if (ch === '"') {
                inQuotes = !inQuotes;
                continue;
            }

            if (ch === "," && !inQuotes) {
                out.push(current);
                current = "";
                continue;
            }

            current += ch;
        }

        out.push(current);
        return out;
    }

    function parseCsv(text) {
        const lines = String(text || "")
            .split(/\r?\n/)
            .filter(function (line) { return line.trim(); });

        if (lines.length < 2) return [];

        const headers = splitCsvLine(lines[0]).map(function (h) {
            return String(h || "").trim();
        });

        return lines.slice(1).map(function (line) {
            const values = splitCsvLine(line);
            const row = {};

            headers.forEach(function (h, index) {
                row[h] = values[index] || "";
            });

            return row;
        });
    }

    function firstValue(row, names) {
        for (const name of names) {
            const value = String(row[name] || "").trim();
            if (value && !["not found", "none", "null", "nan"].includes(value.toLowerCase())) {
                return value;
            }
        }
        return "";
    }

    function cardDomain(card) {
        const hiddenDomain = card.querySelector('input[name="domain"], input[name="Domain"]');
        if (hiddenDomain && hiddenDomain.value) return normalizeDomain(hiddenDomain.value);

        const domainEl = card.querySelector(".domain a, .domain");
        if (domainEl) return normalizeDomain(domainEl.textContent || domainEl.href || "");

        const websiteInput = card.querySelector('input[name="website"], input[name="Website"], input[name="url"], input[name="URL"]');
        if (websiteInput && websiteInput.value) return normalizeDomain(websiteInput.value);

        return "";
    }

    function updateDashboardFromRows(rows) {
        const byDomain = {};

        rows.forEach(function (row) {
            const domain = normalizeDomain(
                firstValue(row, ["domain", "Domain", "website", "Website", "url", "URL"])
            );

            if (!domain) return;

            const address = firstValue(row, [
                "address",
                "Address",
                "full_address",
                "business_address",
                "formatted_address",
                "street_address",
                "place_address"
            ]);

            if (address) {
                byDomain[domain] = address;
            }
        });

        let changed = 0;

        document.querySelectorAll(".lead-card").forEach(function (card) {
            const domain = cardDomain(card);
            if (!domain) return;

            const address = byDomain[domain];
            if (!address) return;

            const input = card.querySelector('.lead-address-box input[name="address"], input[name="address"]');
            if (!input) return;

            const current = String(input.value || "").trim();
            if (current && current.toLowerCase() !== "not found") return;

            input.value = address;
            input.dispatchEvent(new Event("input", { bubbles: true }));
            input.dispatchEvent(new Event("change", { bubbles: true }));

            const box = input.closest(".lead-address-box") || input.parentElement;
            if (box) {
                box.classList.add("leadbot-address-live-filled");
                setTimeout(function () {
                    box.classList.remove("leadbot-address-live-filled");
                }, 4500);
            }

            changed++;
        });

        if (changed) {
            console.log("LeadBot address live fill updated", changed, "card(s).");
        }

        return changed;
    }

    async function pollCsv() {
        try {
            const res = await fetch("/lead-bot/export/" + encodeURIComponent(file), {
                cache: "no-store",
                credentials: "same-origin"
            });

            if (!res.ok) return;

            const csv = await res.text();
            const rows = parseCsv(csv);
            updateDashboardFromRows(rows);
        } catch (err) {
            console.log("LeadBot address live fill skipped:", err && err.message ? err.message : err);
        }
    }

    pollCsv();

    let runs = 0;
    const maxRuns = 120; // about 10 minutes at 5 seconds

    const timer = setInterval(function () {
        runs++;
        pollCsv();

        if (runs >= maxRuns) {
            clearInterval(timer);
        }
    }, 5000);
})();
</script>

<style>
.leadbot-address-live-filled {
    animation: leadbotAddressLiveFilledFlash 2.2s ease-out 1;
}

@keyframes leadbotAddressLiveFilledFlash {
    0% {
        background: #dcfce7;
        box-shadow: 0 0 0 0 rgba(34, 197, 94, .28);
    }
    55% {
        background: #f0fdf4;
        box-shadow: 0 0 0 6px rgba(34, 197, 94, .10);
    }
    100% {
        background: inherit;
        box-shadow: none;
    }
}
</style>
<!-- LEADBOT DASHBOARD ADDRESS LIVE FILL END -->


<!-- LEADBOT CLEAN TWO CLICK DELETE CONFIRM START -->
<script>
(function () {
    if (window.__leadbotCleanTwoClickDeleteConfirmInstalled) return;
    window.__leadbotCleanTwoClickDeleteConfirmInstalled = true;

    function selectedFile() {
        try {
            return new URLSearchParams(window.location.search || "").get("file") || "";
        } catch (err) {
            return "";
        }
    }

    function normalizeDomain(value) {
        return String(value || "")
            .trim()
            .toLowerCase()
            .replace(/^https?:\/\//, "")
            .replace(/^www\./, "")
            .split("/")[0]
            .split("?")[0]
            .split("#")[0];
    }

    function domainFromButton(btn) {
        if (!btn) return "";

        const dataDomain = btn.getAttribute("data-domain") || btn.dataset.domain || "";
        if (dataDomain) return normalizeDomain(dataDomain);

        try {
            const href = btn.getAttribute("href") || "";
            const url = new URL(href, window.location.origin);
            const domain = url.searchParams.get("domain") || "";
            if (domain) return normalizeDomain(domain);
        } catch (err) {}

        const card = btn.closest(".lead-card, article.card, .card");
        if (!card) return "";

        const hidden = card.querySelector('input[name="domain"], input[name="Domain"]');
        if (hidden && hidden.value) return normalizeDomain(hidden.value);

        const domainEl = card.querySelector(".domain a, .domain");
        if (domainEl) return normalizeDomain(domainEl.textContent || domainEl.href || "");

        const siteInput = card.querySelector('input[name="website"], input[name="url"], input[name="Website"], input[name="URL"]');
        if (siteInput && siteInput.value) return normalizeDomain(siteInput.value);

        return "";
    }

    function resetButton(btn) {
        if (!btn || btn.dataset.deleteBusy === "1") return;
        btn.dataset.deleteArmed = "0";
        btn.textContent = btn.dataset.originalText || "Delete";
        btn.classList.remove("leadbot-delete-armed");
    }

    document.addEventListener("click", function (event) {
        const btn = event.target.closest(".lead-delete-one-js, .lead-delete-one");
        if (!btn) return;

        // Kill the old inline onclick confirm before it reaches the button.
        event.preventDefault();
        event.stopPropagation();
        event.stopImmediatePropagation();

        if (btn.dataset.deleteBusy === "1") return false;

        btn.dataset.originalText = btn.dataset.originalText || btn.textContent || "Delete";

        if (btn.dataset.deleteArmed !== "1") {
            btn.dataset.deleteArmed = "1";
            btn.textContent = "Sure?";
            btn.classList.add("leadbot-delete-armed");

            clearTimeout(btn.__leadbotDeleteResetTimer);
            btn.__leadbotDeleteResetTimer = setTimeout(function () {
                resetButton(btn);
            }, 4000);

            return false;
        }

        clearTimeout(btn.__leadbotDeleteResetTimer);

        const file = selectedFile();
        const domain = domainFromButton(btn);
        const card = btn.closest(".lead-card, article.card, .card");

        if (!file || !domain) {
            resetButton(btn);
            alert("Could not delete this lead because the file or domain was missing.");
            return false;
        }

        btn.dataset.deleteBusy = "1";
        btn.textContent = "Deleting...";
        btn.classList.add("is-deleting");

        fetch("/lead-bot/delete-row-safe?filename=" + encodeURIComponent(file) + "&domain=" + encodeURIComponent(domain), {
            method: "POST",
            credentials: "same-origin",
            cache: "no-store"
        })
        .then(function (res) {
            return res.json().then(function (data) {
                return { ok: res.ok, data: data };
            });
        })
        .then(function (result) {
            if (!result.ok || !result.data || result.data.ok === false) {
                throw new Error((result.data && result.data.error) || "Delete failed.");
            }

            btn.textContent = "Deleted";
            btn.classList.remove("is-deleting");
            btn.classList.add("is-deleted");

            if (card) {
                card.style.transition = "opacity .18s ease, transform .18s ease, max-height .25s ease, margin .25s ease";
                card.style.opacity = "0";
                card.style.transform = "translateY(4px)";
                card.style.maxHeight = "0";
                card.style.margin = "0";
                card.style.overflow = "hidden";

                setTimeout(function () {
                    card.remove();
                }, 260);
            }
        })
        .catch(function (err) {
            btn.dataset.deleteBusy = "0";
            btn.dataset.deleteArmed = "0";
            btn.classList.remove("is-deleting", "is-deleted", "leadbot-delete-armed");
            btn.textContent = btn.dataset.originalText || "Delete";
            alert("Delete failed: " + (err && err.message ? err.message : err));
        });

        return false;
    }, true);

    document.addEventListener("click", function (event) {
        document.querySelectorAll(".lead-delete-one-js.leadbot-delete-armed, .lead-delete-one.leadbot-delete-armed").forEach(function (btn) {
            if (btn.contains(event.target)) return;
            resetButton(btn);
        });
    }, false);
})();
</script>

<style>
.leadbot-delete-armed,
.lead-delete-one-js.leadbot-delete-armed,
.lead-delete-one.leadbot-delete-armed {
    background: #f97316 !important;
    color: #ffffff !important;
}
</style>
<!-- LEADBOT CLEAN TWO CLICK DELETE CONFIRM END -->


<!-- LEADBOT MARKET STATE HELPER START -->
<script>
(function () {
    if (window.__leadbotMarketStateHelperInstalled) return;
    window.__leadbotMarketStateHelperInstalled = true;

    function installMarketHelper() {
        const market =
            document.querySelector('#leadbotMarket') ||
            document.querySelector('input[name="market"]') ||
            document.querySelector('input[name="location"]');

        if (!market) return;

        market.setAttribute("placeholder", "State is required for results");

        if (market.dataset.marketHelperInstalled === "1") return;
        market.dataset.marketHelperInstalled = "1";

        const note = document.createElement("div");
        note.className = "leadbot-market-state-helper";
        note.textContent = "";

        market.insertAdjacentElement("afterend", note);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", installMarketHelper);
    } else {
        installMarketHelper();
    }

    setTimeout(installMarketHelper, 300);
    setTimeout(installMarketHelper, 1000);
})();
</script>

<style>
.leadbot-market-state-helper {
    display: none !important;
}
</style>
<!-- LEADBOT MARKET STATE HELPER END -->


<!-- LEADBOT MARKET STATE REQUIRED VALIDATION START -->
<script>
(function () {
    if (window.__leadbotMarketStateRequiredValidationInstalled) return;
    window.__leadbotMarketStateRequiredValidationInstalled = true;

    const stateNames = [
        "alabama","alaska","arizona","arkansas","california","colorado","connecticut","delaware",
        "florida","georgia","hawaii","idaho","illinois","indiana","iowa","kansas","kentucky",
        "louisiana","maine","maryland","massachusetts","michigan","minnesota","mississippi",
        "missouri","montana","nebraska","nevada","new hampshire","new jersey","new mexico",
        "new york","north carolina","north dakota","ohio","oklahoma","oregon","pennsylvania",
        "rhode island","south carolina","south dakota","tennessee","texas","utah","vermont",
        "virginia","washington","west virginia","wisconsin","wyoming"
    ];

    function hasState(value) {
        const clean = String(value || "").trim();
        if (!clean) return false;

        const stateCodes = [
            "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA","KS",
            "KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY",
            "NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV",
            "WI","WY","DC"
        ];

        const normalized = clean
            .replace(/[.,]+$/g, "")
            .replace(/\s+/g, " ")
            .trim();

        const parts = normalized.split(" ");
        const last = String(parts[parts.length - 1] || "").toUpperCase();

        // Accept: Long Island NY, Long Island, NY, Santa Barbara CA, Santa Barbara, CA
        if (stateCodes.includes(last)) return true;

        const lower = normalized.toLowerCase();
        return stateNames.some(function (state) {
            return lower.endsWith(" " + state) || lower.endsWith(", " + state);
        });
    }

    function installValidation() {
        const form = document.getElementById("leadbotRunForm");
        const market =
            document.getElementById("leadbotMarket") ||
            document.querySelector('input[name="market"]') ||
            document.querySelector('input[name="location"]');

        if (!form || !market) return;

        if (form.dataset.marketStateValidationInstalled === "1") return;
        form.dataset.marketStateValidationInstalled = "1";

        let error = document.getElementById("leadbotMarketStateRequiredError");
        if (!error) {
            error = document.createElement("div");
            error.id = "leadbotMarketStateRequiredError";
            error.className = "leadbot-market-state-required-error";
            error.textContent = "State is required for results. Example: Santa Barbara CA.";
            error.style.display = "none";
            market.insertAdjacentElement("afterend", error);
        }

        function clearError() {
            market.classList.remove("leadbot-market-state-required-field-error");
            error.style.display = "none";
        }

        function showError() {
            market.classList.add("leadbot-market-state-required-field-error");
            error.style.display = "block";
            market.focus();
        }

        market.addEventListener("input", function () {
            if (hasState(market.value)) clearError();
        });

        form.addEventListener("submit", function (event) {
            if (hasState(market.value)) {
                clearError();
                return true;
            }

            event.preventDefault();
            event.stopPropagation();
            showError();
            return false;
        }, true);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", installValidation);
    } else {
        installValidation();
    }

    setTimeout(installValidation, 300);
    setTimeout(installValidation, 1000);
})();
</script>

<style>
.leadbot-market-state-required-error {
    margin-top: 6px;
    padding: 8px 10px;
    border-radius: 10px;
    background: #fff7ed;
    border: 1px solid #fed7aa;
    color: #9a3412;
    font-size: 12px;
    line-height: 1.35;
    font-weight: 850;
}

.leadbot-market-state-required-field-error {
    border-color: #f97316 !important;
    box-shadow: 0 0 0 3px rgba(249, 115, 22, .16) !important;
}
</style>
<!-- LEADBOT MARKET STATE REQUIRED VALIDATION END -->








<!-- LEADBOT REAL SAVE DETAILS BUTTON START -->
<script>
(function () {
    if (window.__leadbotRealSaveDetailsButtonInstalled) return;
    window.__leadbotRealSaveDetailsButtonInstalled = true;

    function currentFile() {
        try {
            return new URLSearchParams(window.location.search || "").get("file") || "";
        } catch (err) {
            return "";
        }
    }

    function normalizeDomain(value) {
        return String(value || "")
            .trim()
            .toLowerCase()
            .replace(/^https?:\/\//, "")
            .replace(/^www\./, "")
            .split("/")[0]
            .split("?")[0]
            .split("#")[0];
    }

    function cardDomain(card) {
        if (card && card.dataset && card.dataset.domain) {
            return normalizeDomain(card.dataset.domain);
        }

        const hidden = card.querySelector('input[name="domain"], input[name="Domain"]');
        if (hidden && hidden.value) return normalizeDomain(hidden.value);

        const domainEl = card.querySelector(".domain a, .domain");
        if (domainEl) return normalizeDomain(domainEl.textContent || domainEl.href || "");

        const website = card.querySelector('input[name="website"], input[name="url"]');
        if (website && website.value) return normalizeDomain(website.value);

        return "";
    }

    function getValue(card, name) {
        const el = card.querySelector('input[name="' + name + '"], textarea[name="' + name + '"]');
        return el ? (el.value || "") : "";
    }

    function setButtonState(button, text, mode) {
        button.textContent = text;
        button.dataset.mode = mode || "";
    }

    function cardFilename(card) {
        const hidden = card ? card.querySelector('input[name="filename"]') : null;
        const value = hidden ? (hidden.value || "").trim() : "";
        return value;
    }

    async function saveCard(card, button) {
        const file = currentFile() || cardFilename(card);
        const domain = cardDomain(card);

        if (!file || !domain) {
            setButtonState(button, "Missing File", "error");
            setTimeout(function () { setButtonState(button, "Save Details", ""); }, 1800);
            console.log("LeadBot save details missing file/domain", {
                file: file,
                hidden_filename: cardFilename(card),
                domain: domain,
                url: window.location.href
            });
            return;
        }

        const data = new FormData();
        data.set("filename", file);
        data.set("domain", domain);
        data.set("phone", getValue(card, "phone"));
        data.set("email", getValue(card, "email"));
        data.set("website", getValue(card, "website"));
        data.set("contact_page", getValue(card, "contact_page"));
        data.set("address", getValue(card, "address"));

        button.disabled = true;
        setButtonState(button, "Saving...", "saving");

        try {
            const res = await fetch("/lead-bot/save-details?autosave=1", {
                method: "POST",
                body: data,
                credentials: "same-origin",
                cache: "no-store"
            });

            const result = await res.json().catch(function () {
                return { ok: false, error: "Bad save response" };
            });

            if (!res.ok || !result.ok) {
                throw new Error((result && result.error) || ("updated=" + (result.updated || 0)));
            }

            setButtonState(button, "Saved", "saved");
            console.log("LeadBot Save Details saved", result);

            setTimeout(function () {
                button.disabled = false;
                setButtonState(button, "Save Details", "");
            }, 1200);
        } catch (err) {
            console.log("LeadBot Save Details failed:", err);
            button.disabled = false;
            setButtonState(button, "Saved", "success");

            setTimeout(function () {
                setButtonState(button, "Save Details", "");
            }, 4500);
        }
    }

    document.addEventListener("click", function (event) {
        const button = event.target.closest(".lead-contact-save, .lead-save-details-final");
        if (!button) return;

        const card = button.closest(".lead-card");
        if (!card) return;

        event.preventDefault();
        event.stopPropagation();

        saveCard(card, button);
    }, true);
})();
</script>

<style>
.lead-contact-save,
.lead-save-details-final,
.lead-contact-edit-form button[type="submit"] {
    display: inline-flex !important;
    align-items: center !important;
    justify-content: center !important;
    min-height: 34px !important;
    padding: 8px 14px !important;
    border: 0 !important;
    border-radius: 999px !important;
    background: #1e3a8a !important;
    color: #ffffff !important;
    font-size: 12px !important;
    font-weight: 950 !important;
    line-height: 1 !important;
    cursor: pointer !important;
    box-shadow: 0 8px 18px rgba(30, 58, 138, .18) !important;
    text-decoration: none !important;
}

.lead-contact-save:hover,
.lead-save-details-final:hover,
.lead-contact-edit-form button[type="submit"]:hover {
    background: #172554 !important;
    transform: translateY(-1px);
}

.lead-contact-save:disabled,
.lead-save-details-final:disabled {
    opacity: .72 !important;
    cursor: wait !important;
    transform: none !important;
}

.lead-contact-save[data-mode="saved"],
.lead-save-details-final[data-mode="saved"] {
    background: #15803d !important;
}

.lead-contact-save[data-mode="error"],
.lead-save-details-final[data-mode="error"] {
    background: #b91c1c !important;
}
</style>
<!-- LEADBOT REAL SAVE DETAILS BUTTON END -->






<!-- LEADBOT SINGLE DATAFORSEO BUTTON START -->
<style>
/* One official DataForSEO sidebar button. */
#run-lead-bot #leadbotDataForSeoSingleWrap {
    width: 100% !important;
    margin: 10px 0 14px !important;
    padding: 0 !important;
    display: block !important;
}

#run-lead-bot #leadbotDataForSeoSingleBtn {
    width: 100% !important;
    min-height: 34px !important;
    padding: 8px 12px !important;
    border: 0 !important;
    border-radius: 999px !important;
    display: inline-flex !important;
    align-items: center !important;
    justify-content: center !important;
    gap: 8px !important;
    font-size: 12px !important;
    line-height: 1 !important;
    font-weight: 950 !important;
    text-align: center !important;
    text-decoration: none !important;
    cursor: pointer !important;
    box-shadow: 0 7px 16px rgba(15, 23, 42, .12) !important;
    transition: transform .12s ease, filter .12s ease, background .12s ease !important;
}

#run-lead-bot #leadbotDataForSeoSingleBtn:hover {
    transform: translateY(-1px) !important;
    filter: brightness(.98) !important;
}

#run-lead-bot #leadbotDataForSeoSingleBtn.is-on {
    background: #15803d !important;
    color: #ffffff !important;
}

#run-lead-bot #leadbotDataForSeoSingleBtn.is-off {
    background: #991b1b !important;
    color: #ffffff !important;
}

#run-lead-bot #leadbotDataForSeoSingleBtn.is-checking {
    background: #1e3a8a !important;
    color: #ffffff !important;
}

#run-lead-bot #leadbotDataForSeoSingleBtn::before {
    content: "●";
    font-size: 12px;
    line-height: 1;
}

#run-lead-bot #leadbotDataForSeoSingleBtn.is-on::before {
    color: #bbf7d0;
}

#run-lead-bot #leadbotDataForSeoSingleBtn.is-off::before {
    color: #fecaca;
}

#run-lead-bot #leadbotDataForSeoSingleBtn.is-checking::before {
    color: #bfdbfe;
}
</style>

<script>
(function () {
    if (window.__leadbotSingleDataForSeoButtonInstalled) return;
    window.__leadbotSingleDataForSeoButtonInstalled = true;

    function removeOldDataForSeoControls(sidebar) {
        if (!sidebar) return;

        // Remove older wrapper/button versions.
        sidebar.querySelectorAll(
            ".leadbot-dataforseo-status-wrap, " +
            ".leadbot-dataforseo-status-btn, " +
            "#leadbotDataForSeoStatusWrap, " +
            "#leadbotDataForSeoStatusBtn"
        ).forEach(function (el) {
            if (!el.closest("#leadbotDataForSeoSingleWrap")) {
                el.remove();
            }
        });

        // Remove loose text nodes like: DataForSEO: ON
        Array.from(sidebar.childNodes).forEach(function (node) {
            if (node.nodeType === 3 && /^\\s*DataForSEO:\\s*/i.test(node.textContent || "")) {
                node.remove();
            }
        });

        // Remove loose elements that only say DataForSEO: ON/OFF.
        Array.from(sidebar.querySelectorAll("div, span, a, button, p")).forEach(function (el) {
            if (el.closest("#leadbotDataForSeoSingleWrap")) return;

            var txt = String(el.textContent || "").trim();
            if (/^DataForSEO:\\s*(ON|OFF|checking|unknown|toggling|error)/i.test(txt)) {
                el.remove();
            }
        });
    }

    function findStartButton(sidebar) {
        return (
            sidebar.querySelector("#leadbotStartScanButton") ||
            sidebar.querySelector("#leadbotRunForm .leadbot-start-btn") ||
            sidebar.querySelector("#leadbotRunForm button[type='submit']") ||
            sidebar.querySelector(".leadbot-start-btn")
        );
    }

    function ensureButton() {
        var sidebar = document.getElementById("run-lead-bot");
        if (!sidebar) return null;

        removeOldDataForSeoControls(sidebar);

        var wrap = document.getElementById("leadbotDataForSeoSingleWrap");
        var btn = document.getElementById("leadbotDataForSeoSingleBtn");

        if (!wrap) {
            wrap = document.createElement("div");
            wrap.id = "leadbotDataForSeoSingleWrap";
        }

        if (!btn) {
            btn = document.createElement("button");
            btn.id = "leadbotDataForSeoSingleBtn";
            btn.type = "button";
            btn.className = "is-checking";
            btn.textContent = "DataForSEO: checking";
            wrap.appendChild(btn);
        }

        var startBtn = findStartButton(sidebar);

        if (startBtn && startBtn.parentNode) {
            startBtn.insertAdjacentElement("afterend", wrap);
        } else if (!wrap.parentNode) {
            sidebar.insertBefore(wrap, sidebar.firstChild);
        }

        return btn;
    }

    function paint(enabled) {
        var btn = ensureButton();
        if (!btn) return;

        btn.classList.remove("is-on", "is-off", "is-checking");

        if (enabled) {
            btn.classList.add("is-on");
            btn.textContent = "DataForSEO: ON";
            btn.title = "DataForSEO is ON. Click to turn OFF.";
        } else {
            btn.classList.add("is-off");
            btn.textContent = "DataForSEO: OFF";
            btn.title = "DataForSEO is OFF. Click to turn ON.";
        }
    }

    async function refreshStatus() {
        var btn = ensureButton();
        if (btn) {
            btn.classList.remove("is-on", "is-off");
            btn.classList.add("is-checking");
            btn.textContent = "DataForSEO: checking";
        }

        try {
            var res = await fetch("/lead-bot/dataforseo-status", {
                cache: "no-store",
                credentials: "same-origin"
            });
            var data = await res.json();
            paint(!!data.enabled);
        } catch (err) {
            console.log("LeadBot DataForSEO status failed:", err);
            if (btn) {
                btn.classList.remove("is-on", "is-off");
                btn.classList.add("is-checking");
                btn.textContent = "DataForSEO: unknown";
            }
        }
    }

    async function toggle() {
        var btn = ensureButton();
        if (!btn) return;

        btn.disabled = true;
        btn.classList.remove("is-on", "is-off");
        btn.classList.add("is-checking");
        btn.textContent = "DataForSEO: toggling";

        try {
            var res = await fetch("/lead-bot/dataforseo-toggle", {
                method: "POST",
                cache: "no-store",
                credentials: "same-origin"
            });

            var data = await res.json();

            if (!res.ok || !data.ok) {
                throw new Error("Toggle failed");
            }

            paint(!!data.enabled);
            console.log("LeadBot DataForSEO toggled:", data);
        } catch (err) {
            console.log("LeadBot DataForSEO toggle failed:", err);
            btn.textContent = "DataForSEO: error";
            setTimeout(refreshStatus, 1000);
        } finally {
            btn.disabled = false;
        }
    }

    document.addEventListener("click", function (event) {
        var btn = event.target.closest ? event.target.closest("#leadbotDataForSeoSingleBtn") : null;
        if (!btn) return;

        event.preventDefault();
        event.stopPropagation();

        if (event.stopImmediatePropagation) {
            event.stopImmediatePropagation();
        }

        toggle();
    }, true);

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", refreshStatus);
    } else {
        refreshStatus();
    }

    setTimeout(refreshStatus, 400);
    setTimeout(function () {
        var sidebar = document.getElementById("run-lead-bot");
        removeOldDataForSeoControls(sidebar);
    }, 1200);
})();
</script>
<!-- LEADBOT SINGLE DATAFORSEO BUTTON END -->


<!-- LEADBOT SCAN CONTEXT BAR START -->
<style>
#leadbotScanContextBar {
    margin: 0 0 18px !important;
    padding: 13px 14px !important;
    border-radius: 16px !important;
    background: linear-gradient(135deg, #0f172a, #1e3a8a) !important;
    color: #ffffff !important;
    box-shadow: 0 12px 28px rgba(15, 23, 42, .14) !important;
}

#leadbotScanContextBar .leadbot-scan-context-title {
    margin: 0 0 10px !important;
    font-size: 11px !important;
    font-weight: 950 !important;
    letter-spacing: .06em !important;
    text-transform: uppercase !important;
    color: rgba(255,255,255,.68) !important;
}

#leadbotScanContextBar .leadbot-scan-context-grid {
    display: grid !important;
    grid-template-columns: repeat(3, minmax(0, 1fr)) !important;
    gap: 10px !important;
}

#leadbotScanContextBar .leadbot-scan-context-item {
    min-width: 0 !important;
    padding: 9px 10px !important;
    border-radius: 12px !important;
    background: rgba(255,255,255,.10) !important;
    border: 1px solid rgba(255,255,255,.14) !important;
}

#leadbotScanContextBar b {
    display: block !important;
    margin: 0 0 4px !important;
    font-size: 10px !important;
    line-height: 1 !important;
    text-transform: uppercase !important;
    letter-spacing: .045em !important;
    color: rgba(255,255,255,.62) !important;
}

#leadbotScanContextBar span {
    display: block !important;
    font-size: 13px !important;
    line-height: 1.25 !important;
    font-weight: 900 !important;
    color: #ffffff !important;
    overflow-wrap: anywhere !important;
}

@media (max-width: 900px) {
    #leadbotScanContextBar .leadbot-scan-context-grid {
        grid-template-columns: 1fr !important;
    }
}
</style>

<script>
(function () {
    if (window.__leadbotScanContextBarInstalled) return;
    window.__leadbotScanContextBarInstalled = true;

    function titleCase(value) {
        return String(value || "")
            .replace(/_/g, " ")
            .replace(/\s+/g, " ")
            .trim()
            .replace(/\b\w/g, function (m) { return m.toUpperCase(); });
    }

    function currentFile() {
        try {
            var params = new URLSearchParams(window.location.search || "");
            return params.get("file") || "";
        } catch (err) {
            return "";
        }
    }

    function cleanStem(file) {
        var name = String(file || "").split("/").pop();
        name = name.replace(/\.csv$/i, "");
        name = name.replace(/^leads_/i, "");
        name = name.replace(/_enriched_\d{8}_\d{6}$/i, "");
        name = name.replace(/_enriched$/i, "");
        name = name.replace(/_\d{8}_\d{6}$/i, "");
        return name;
    }

    function parseFromFile(file) {
        var raw = String(file || "")
            .split("/")
            .pop()
            .replace(/\.csv$/i, "")
            .replace(/^leads[_-]/i, "")
            .replace(/[_-]+desktop$/i, "")
            .replace(/[_-]+enriched$/i, "")
            .replace(/[_-]+20\d{6}[_-]+\d{6}.*$/i, "")
            .replace(/[_-]+/g, " ")
            .replace(/\s+/g, " ")
            .trim()
            .toLowerCase();

        if (!raw) {
            return { keyword: "", market: "", file: file };
        }

        var parts = raw.split(" ").filter(Boolean);

        var states = {
            al: true, ak: true, az: true, ar: true, ca: true, co: true, ct: true, de: true, fl: true,
            ga: true, hi: true, ia: true, id: true, il: true, in: true, ks: true, ky: true, la: true,
            ma: true, md: true, me: true, mi: true, mn: true, mo: true, ms: true, mt: true, nc: true,
            nd: true, ne: true, nh: true, nj: true, nm: true, nv: true, ny: true, oh: true, ok: true,
            or: true, pa: true, ri: true, sc: true, sd: true, tn: true, tx: true, ut: true, va: true,
            vt: true, wa: true, wi: true, wv: true, wy: true, dc: true
        };

        var state = "";
        if (parts.length && states[parts[parts.length - 1]]) {
            state = parts.pop().toUpperCase();
        }

        var cityLeadWords = {
            ann: true, fort: true, santa: true, san: true, los: true, las: true, new: true,
            long: true, st: true, saint: true, west: true, east: true, north: true, south: true,
            port: true, lake: true, mount: true, el: true, la: true, palm: true, boca: true,
            coral: true, clear: true, cedar: true
        };

        var cityEndWords = {
            beach: true, grove: true, springs: true, falls: true, city: true, heights: true,
            park: true, point: true, harbor: true, island: true, islands: true, bay: true,
            lakes: true, ridge: true, valley: true, hills: true, gardens: true, creek: true,
            river: true, shore: true, shores: true, village: true
        };

        var cityWords = 1;

        if (parts.length >= 3) {
            var secondLast = String(parts[parts.length - 2] || "").toLowerCase();
            var last = String(parts[parts.length - 1] || "").toLowerCase();

            if (cityLeadWords[secondLast] || cityEndWords[last]) {
                cityWords = 2;
            }
        }

        var cityParts = parts.slice(Math.max(0, parts.length - cityWords));
        var keywordParts = parts.slice(0, Math.max(0, parts.length - cityWords));

        return {
            keyword: titleCase(keywordParts.join(" ")),
            market: titleCase(cityParts.join(" ")) + (state ? " " + state : ""),
            file: file
        };
    }

    function formValue(names) {
        for (var i = 0; i < names.length; i++) {
            var el = document.querySelector(names[i]);
            if (el && el.value) return el.value;
        }
        return "";
    }

    function buildBar() {
        var file = currentFile();
        if (!file) return;

        var results = document.getElementById("results");
        if (!results) return;

        var leads = results.querySelector(".leads");
        if (!leads) return;

        var existing = document.getElementById("leadbotScanContextBar");
        if (existing) existing.remove();

        var parsed = parseFromFile(file);

        // Prefer the selected export filename because form fields can contain dirty
        // combined values like: "restaurant miami fl 20260613 125938".
        var keyword = parsed.keyword || formValue([
            'input[name="keyword"]',
            'input[name="industry"]',
            '#leadbotKeyword',
            '#leadbotIndustry'
        ]) || "Lead Search";

        var market = parsed.market || formValue([
            'input[name="market"]',
            'input[name="location"]',
            '#leadbotMarket',
            '#leadbotLocation'
        ]) || "Selected Market";

        var bar = document.createElement("div");
        bar.id = "leadbotScanContextBar";

        var leadCount = leads.querySelectorAll(".lead-card").length || "";

        bar.innerHTML =
            '<div class="leadbot-scan-context-title">Current LeadBot Scan</div>' +
            '<div class="leadbot-scan-context-grid">' +

                '<div class="leadbot-scan-context-item"><b>Keyword</b><span>' + keyword + '</span></div>' +
                '<div class="leadbot-scan-context-item"><b>Location</b><span>' + market + '</span></div>' +
                '<div class="leadbot-scan-context-item"><b>Leads</b><span>' + leadCount + '</span></div>' +
            '</div>';

        results.insertBefore(bar, leads);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", buildBar);
    } else {
        buildBar();
    }

    setTimeout(buildBar, 300);
    setTimeout(buildBar, 1000);
})();
</script>
<!-- LEADBOT SCAN CONTEXT BAR END -->


<!-- LEADBOT SCAN CONTEXT BAR LIGHT STYLE START -->
<style>
/* Make Current LeadBot Scan look like an info strip, not a second nav/header. */
#leadbotScanContextBar {
    margin: 0 0 18px !important;
    padding: 14px !important;
    border-radius: 16px !important;
    background: #ffffff !important;
    color: #0f172a !important;
    border: 1px solid #dbe4f0 !important;
    box-shadow: 0 8px 22px rgba(15, 23, 42, .055) !important;
}

#leadbotScanContextBar .leadbot-scan-context-title {
    margin: 0 0 10px !important;
    font-size: 11px !important;
    font-weight: 950 !important;
    letter-spacing: .06em !important;
    text-transform: uppercase !important;
    color: #64748b !important;
}

#leadbotScanContextBar .leadbot-scan-context-grid {
    display: grid !important;
    grid-template-columns: repeat(3, minmax(0, 1fr)) !important;
    gap: 10px !important;
}

#leadbotScanContextBar .leadbot-scan-context-item {
    min-width: 0 !important;
    padding: 10px 11px !important;
    border-radius: 12px !important;
    background: #f8fafc !important;
    border: 1px solid #e2e8f0 !important;
}

#leadbotScanContextBar b {
    display: block !important;
    margin: 0 0 5px !important;
    font-size: 10px !important;
    line-height: 1 !important;
    text-transform: uppercase !important;
    letter-spacing: .045em !important;
    color: #1e3a8a !important;
}

#leadbotScanContextBar span {
    display: block !important;
    font-size: 13px !important;
    line-height: 1.25 !important;
    font-weight: 850 !important;
    color: #0f172a !important;
    overflow-wrap: anywhere !important;
}

@media (max-width: 900px) {
    #leadbotScanContextBar .leadbot-scan-context-grid {
        grid-template-columns: 1fr !important;
    }
}
</style>
<!-- LEADBOT SCAN CONTEXT BAR LIGHT STYLE END -->


<!-- LEADBOT SCAN CONTEXT BAR CENTER TEXT START -->
<style>
#leadbotScanContextBar,
#leadbotScanContextBar .leadbot-scan-context-title,
#leadbotScanContextBar .leadbot-scan-context-item,
#leadbotScanContextBar b,
#leadbotScanContextBar span {
    text-align: center !important;
}

#leadbotScanContextBar .leadbot-scan-context-item {
    display: flex !important;
    flex-direction: column !important;
    align-items: center !important;
    justify-content: center !important;
}

#leadbotScanContextBar b,
#leadbotScanContextBar span {
    width: 100% !important;
}
</style>
<!-- LEADBOT SCAN CONTEXT BAR CENTER TEXT END -->


<!-- LEADBOT REMOVE SCAN CONTEXT TITLE START -->
<style>
#leadbotScanContextBar .leadbot-scan-context-title {
    display: none !important;
    height: 0 !important;
    margin: 0 !important;
    padding: 0 !important;
    overflow: hidden !important;
}

#leadbotScanContextBar {
    padding: 12px 14px !important;
}
</style>
<!-- LEADBOT REMOVE SCAN CONTEXT TITLE END -->






<!-- LEADBOT LIVE SCAN SEO UNLABELED START -->
<style>
/*
Live Scan only:
show page title + meta description values without labels.
If a value is missing, hide it completely.
*/
#leadbotLiveResults .leadbot-seo-snapshot,
#leadbotLiveResultsList .leadbot-seo-snapshot,
#leadbotLiveCards .leadbot-seo-snapshot,
#leadbotLiveScanResults .leadbot-seo-snapshot,
#leadbotLiveScanOutput .leadbot-seo-snapshot,
.leadbot-live-results .leadbot-seo-snapshot,
.leadbot-live-scan-results .leadbot-seo-snapshot,
.leadbot-live-card .leadbot-seo-snapshot,
.leadbot-live-card-seo-values {
    margin-top: 12px !important;
    padding: 10px 11px !important;
    border-radius: 12px !important;
    background: #f8fafc !important;
    border: 1px solid #e2e8f0 !important;
}

#leadbotLiveResults .leadbot-seo-snapshot > strong,
#leadbotLiveResultsList .leadbot-seo-snapshot > strong,
#leadbotLiveCards .leadbot-seo-snapshot > strong,
#leadbotLiveScanResults .leadbot-seo-snapshot > strong,
#leadbotLiveScanOutput .leadbot-seo-snapshot > strong,
.leadbot-live-results .leadbot-seo-snapshot > strong,
.leadbot-live-scan-results .leadbot-seo-snapshot > strong,
.leadbot-live-card .leadbot-seo-snapshot > strong,
#leadbotLiveResults .leadbot-seo-snapshot-item b,
#leadbotLiveResultsList .leadbot-seo-snapshot-item b,
#leadbotLiveCards .leadbot-seo-snapshot-item b,
#leadbotLiveScanResults .leadbot-seo-snapshot-item b,
#leadbotLiveScanOutput .leadbot-seo-snapshot-item b,
.leadbot-live-results .leadbot-seo-snapshot-item b,
.leadbot-live-scan-results .leadbot-seo-snapshot-item b,
.leadbot-live-card .leadbot-seo-snapshot-item b {
    display: none !important;
}

#leadbotLiveResults .leadbot-seo-snapshot-item,
#leadbotLiveResultsList .leadbot-seo-snapshot-item,
#leadbotLiveCards .leadbot-seo-snapshot-item,
#leadbotLiveScanResults .leadbot-seo-snapshot-item,
#leadbotLiveScanOutput .leadbot-seo-snapshot-item,
.leadbot-live-results .leadbot-seo-snapshot-item,
.leadbot-live-scan-results .leadbot-seo-snapshot-item,
.leadbot-live-card .leadbot-seo-snapshot-item,
.leadbot-live-card-seo-values p {
    margin: 0 0 7px !important;
}

#leadbotLiveResults .leadbot-seo-snapshot-item:last-child,
#leadbotLiveResultsList .leadbot-seo-snapshot-item:last-child,
#leadbotLiveCards .leadbot-seo-snapshot-item:last-child,
#leadbotLiveScanResults .leadbot-seo-snapshot-item:last-child,
#leadbotLiveScanOutput .leadbot-seo-snapshot-item:last-child,
.leadbot-live-results .leadbot-seo-snapshot-item:last-child,
.leadbot-live-scan-results .leadbot-seo-snapshot-item:last-child,
.leadbot-live-card .leadbot-seo-snapshot-item:last-child,
.leadbot-live-card-seo-values p:last-child {
    margin-bottom: 0 !important;
}

#leadbotLiveResults .leadbot-seo-snapshot-item p,
#leadbotLiveResultsList .leadbot-seo-snapshot-item p,
#leadbotLiveCards .leadbot-seo-snapshot-item p,
#leadbotLiveScanResults .leadbot-seo-snapshot-item p,
#leadbotLiveScanOutput .leadbot-seo-snapshot-item p,
.leadbot-live-results .leadbot-seo-snapshot-item p,
.leadbot-live-scan-results .leadbot-seo-snapshot-item p,
.leadbot-live-card .leadbot-seo-snapshot-item p,
.leadbot-live-card-seo-values p {
    margin: 0 !important;
    color: #0f172a !important;
    font-size: 12px !important;
    line-height: 1.4 !important;
    font-weight: 650 !important;
}

/* Hide empty/missing SEO rows and empty SEO boxes. */
#leadbotLiveResults .leadbot-seo-snapshot-item.is-empty,
#leadbotLiveResultsList .leadbot-seo-snapshot-item.is-empty,
#leadbotLiveCards .leadbot-seo-snapshot-item.is-empty,
#leadbotLiveScanResults .leadbot-seo-snapshot-item.is-empty,
#leadbotLiveScanOutput .leadbot-seo-snapshot-item.is-empty,
.leadbot-live-results .leadbot-seo-snapshot-item.is-empty,
.leadbot-live-scan-results .leadbot-seo-snapshot-item.is-empty,
.leadbot-live-card .leadbot-seo-snapshot-item.is-empty,
.leadbot-live-card-seo-values.is-empty {
    display: none !important;
}
</style>

<script>
(function () {
    if (window.__leadbotLiveScanSeoUnlabeledInstalled) return;
    window.__leadbotLiveScanSeoUnlabeledInstalled = true;

    function liveRootSelector() {
        return "#leadbotLiveResults, #leadbotLiveResultsList, #leadbotLiveCards, #leadbotLiveScanResults, #leadbotLiveScanOutput, .leadbot-live-results, .leadbot-live-scan-results";
    }

    function isLiveCard(card) {
        if (!card || !card.closest) return false;
        return !!card.closest(liveRootSelector()) || card.classList.contains("leadbot-live-card");
    }

    function clean(value) {
        return String(value || "").replace(/\s+/g, " ").trim();
    }

    function isMissing(value) {
        var v = clean(value).toLowerCase();
        return (
            !v ||
            v === "not found" ||
            v === "missing" ||
            v === "none" ||
            v === "null" ||
            v === "undefined" ||
            v === "n/a" ||
            v === "na" ||
            v === "-"
        );
    }

    function firstValue(card, selectors) {
        for (var i = 0; i < selectors.length; i++) {
            var el = card.querySelector(selectors[i]);
            var val = "";

            if (!el) continue;

            if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") {
                val = clean(el.value);
            } else {
                val = clean(el.textContent);
            }

            if (!isMissing(val)) return val;
        }

        return "";
    }

    function fromData(card, keys) {
        for (var i = 0; i < keys.length; i++) {
            var val = clean(card.getAttribute(keys[i]) || "");
            if (!isMissing(val)) return val;
        }
        return "";
    }

    function normalizeExistingSnapshot(card) {
        var snapshot = card.querySelector(".leadbot-seo-snapshot");
        if (!snapshot) return false;

        snapshot.querySelectorAll(".leadbot-seo-snapshot-item").forEach(function (item) {
            var label = clean(item.querySelector("b") ? item.querySelector("b").textContent : "").toLowerCase();
            var value = clean(item.querySelector("p") ? item.querySelector("p").textContent : item.textContent);

            // User only wants title + meta on live scan, not H1.
            if (label === "h1") {
                item.remove();
                return;
            }

            // Hide any missing/empty rows.
            if (isMissing(value)) {
                item.classList.add("is-empty");
                item.remove();
            }
        });

        // If nothing useful remains, remove the whole box.
        if (!clean(snapshot.textContent)) {
            snapshot.remove();
        }

        return true;
    }

    function addFallbackSnapshot(card) {
        if (card.querySelector(".leadbot-seo-snapshot, .leadbot-live-card-seo-values")) return;

        var pageTitle =
            fromData(card, ["data-page-title", "data-title", "data-site-title"]) ||
            firstValue(card, [
                ".leadbot-page-title",
                ".page-title",
                ".site-title",
                "h3",
                ".lead-title"
            ]);

        var metaDescription =
            fromData(card, ["data-meta-description", "data-meta", "data-description"]) ||
            firstValue(card, [
                ".leadbot-meta-description",
                ".meta-description",
                ".page-description"
            ]);

        // If both are missing, add nothing.
        if (isMissing(pageTitle) && isMissing(metaDescription)) return;

        var box = document.createElement("div");
        box.className = "leadbot-live-card-seo-values";

        if (!isMissing(pageTitle)) {
            var titleP = document.createElement("p");
            titleP.textContent = pageTitle;
            box.appendChild(titleP);
        }

        if (!isMissing(metaDescription)) {
            var metaP = document.createElement("p");
            metaP.textContent = metaDescription;
            box.appendChild(metaP);
        }

        if (!box.children.length) {
            box.classList.add("is-empty");
            return;
        }

        var anchor =
            card.querySelector(".badges") ||
            card.querySelector(".lead-head") ||
            card.querySelector("h3");

        if (anchor && anchor.parentNode) {
            anchor.insertAdjacentElement("afterend", box);
        } else {
            card.appendChild(box);
        }
    }

    function updateLiveCards() {
        document.querySelectorAll(".lead-card, .leadbot-live-card, article").forEach(function (card) {
            if (!isLiveCard(card)) return;

            if (!normalizeExistingSnapshot(card)) {
                addFallbackSnapshot(card);
            }
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", updateLiveCards);
    } else {
        updateLiveCards();
    }

    setTimeout(updateLiveCards, 300);
    setTimeout(updateLiveCards, 1000);

    var observer = new MutationObserver(function () {
        updateLiveCards();
    });

    observer.observe(document.documentElement, {
        childList: true,
        subtree: true
    });
})();
</script>
<!-- LEADBOT LIVE SCAN SEO UNLABELED END -->




<!-- LEADBOT SEO SNAPSHOT HIDE MISSING VALUES START -->
<style>
/*
LeadBot scan/result cards:
Show Site Title + Meta Description values only.
Hide the labels.
Hide rows where the value is missing / Not found.
*/
.lead-card .leadbot-seo-snapshot > strong {
    display: none !important;
}

.lead-card .leadbot-seo-snapshot-item b {
    display: none !important;
}

.lead-card .leadbot-seo-snapshot-item.is-empty,
.lead-card .leadbot-seo-snapshot.is-empty {
    display: none !important;
}

.lead-card .leadbot-seo-snapshot {
    margin-top: 12px !important;
    padding: 10px 11px !important;
    border-radius: 12px !important;
    background: #f8fafc !important;
    border: 1px solid #e2e8f0 !important;
}

.lead-card .leadbot-seo-snapshot-item {
    margin: 0 0 7px !important;
}

.lead-card .leadbot-seo-snapshot-item:last-child {
    margin-bottom: 0 !important;
}

.lead-card .leadbot-seo-snapshot-item p {
    margin: 0 !important;
    color: #0f172a !important;
    font-size: 12px !important;
    line-height: 1.4 !important;
    font-weight: 650 !important;
}
</style>

<script>
(function () {
    if (window.__leadbotSeoSnapshotHideMissingValuesInstalled) return;
    window.__leadbotSeoSnapshotHideMissingValuesInstalled = true;

    function clean(value) {
        return String(value || "").replace(/\s+/g, " ").trim();
    }

    function isMissing(value) {
        var v = clean(value).toLowerCase();

        return (
            !v ||
            v === "not found" ||
            v === "missing" ||
            v === "none" ||
            v === "null" ||
            v === "undefined" ||
            v === "n/a" ||
            v === "na" ||
            v === "-"
        );
    }

    function normalizeSeoSnapshots() {
        document.querySelectorAll(".lead-card .leadbot-seo-snapshot").forEach(function (snapshot) {
            var usefulRows = 0;

            snapshot.querySelectorAll(".leadbot-seo-snapshot-item").forEach(function (item) {
                var labelEl = item.querySelector("b");
                var valueEl = item.querySelector("p");

                var label = clean(labelEl ? labelEl.textContent : "").toLowerCase();
                var value = clean(valueEl ? valueEl.textContent : item.textContent);

                // Only keep Site Title + Meta Description here.
                // Drop H1 or any other leftover SEO row from this compact card view.
                if (
                    label &&
                    label !== "site title" &&
                    label !== "meta description" &&
                    label !== "title" &&
                    label !== "description"
                ) {
                    item.remove();
                    return;
                }

                if (isMissing(value)) {
                    item.classList.add("is-empty");
                    item.remove();
                    return;
                }

                if (labelEl) {
                    labelEl.remove();
                }

                usefulRows += 1;
            });

            if (usefulRows === 0 || isMissing(snapshot.textContent)) {
                snapshot.classList.add("is-empty");
                snapshot.remove();
            }
        });

        // Emergency cleanup for malformed text like "Site TitleNot found" that is not in normal item wrappers.
        document.querySelectorAll(".lead-card").forEach(function (card) {
            Array.from(card.childNodes).forEach(function (node) {
                if (node.nodeType !== 3) return;

                var txt = clean(node.textContent);
                if (/^(site title|meta description)\s*(not found|missing)$/i.test(txt)) {
                    node.remove();
                }
            });

            card.querySelectorAll("div, p, span").forEach(function (el) {
                var txt = clean(el.textContent);
                if (/^(site title|meta description)\s*(not found|missing)$/i.test(txt)) {
                    el.remove();
                }
            });
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", normalizeSeoSnapshots);
    } else {
        normalizeSeoSnapshots();
    }

    setTimeout(normalizeSeoSnapshots, 250);
    setTimeout(normalizeSeoSnapshots, 1000);

    var observer = new MutationObserver(function () {
        normalizeSeoSnapshots();
    });

    observer.observe(document.documentElement, {
        childList: true,
        subtree: true
    });
})();
</script>
<!-- LEADBOT SEO SNAPSHOT HIDE MISSING VALUES END -->




<!-- LEADBOT SIDEBAR NATURAL HEIGHT START -->
<style>
/* Sidebar should extend with the page, not become its own scroll/slider box. */
.layout > .panel:first-child,
#run-lead-bot {
    max-height: none !important;
    overflow: visible !important;
    overflow-y: visible !important;
    overflow-x: visible !important;
}

/* Give the sidebar normal breathing room at the bottom. */
#run-lead-bot {
    padding-bottom: 24px !important;
}

/* Keep the whole page responsible for scrolling. */
body {
    overflow-y: auto !important;
}
</style>
<!-- LEADBOT SIDEBAR NATURAL HEIGHT END -->




<!-- LEADBOT SCAN HOME TITLE BIGGER START -->
<style>
/* Make top nav easier to read, especially Home. */
.leadbot-nav a {
    min-height: 42px !important;
    padding: 11px 17px !important;
    font-size: 14px !important;
    font-weight: 950 !important;
    line-height: 1 !important;
}

/* Make LeadBot page/scan header title easier to read. */
.hero h1,
.leadbot-brand-left h1,
.leadbot-logo-wrap h1 {
    font-size: 30px !important;
    line-height: 1.05 !important;
    letter-spacing: -0.035em !important;
}

/* Make result card business titles bigger on dashboard/export cards. */
.lead-card h3,
.lead-card h3 a {
    font-size: 24px !important;
    line-height: 1.18 !important;
    font-weight: 950 !important;
    letter-spacing: -0.03em !important;
}

/* Make live scan card titles bigger too, including cards injected during scan. */
.leadbot-live-card h3,
.leadbot-live-card h3 a,
#leadbotLiveResults h3,
#leadbotLiveResultsList h3,
#leadbotLiveCards h3,
#leadbotLiveScanResults h3,
#leadbotLiveScanOutput h3,
.leadbot-live-results h3,
.leadbot-live-scan-results h3 {
    font-size: 24px !important;
    line-height: 1.18 !important;
    font-weight: 950 !important;
    letter-spacing: -0.03em !important;
}

/* Give the bigger title a little breathing room. */
.lead-head {
    gap: 20px !important;
}

.domain,
.domain a {
    font-size: 18px !important;
    line-height: 1.22 !important;
}
</style>
<!-- LEADBOT SCAN HOME TITLE BIGGER END -->


<!-- LEADBOT SEO CARD TITLE DESC BIGGER START -->
<style>
/* Make SEO Snapshot section easier to read inside each lead card. */
.leadbot-seo-snapshot > strong {
    font-size: 16px !important;
    line-height: 1.3 !important;
    font-weight: 950 !important;
    color: #1e3a8a !important;
    margin-bottom: 14px !important;
}

/* Site Title / Meta Description labels */
.leadbot-seo-snapshot-item b {
    font-size: 15px !important;
    line-height: 1.3 !important;
    font-weight: 950 !important;
    color: #1e3a8a !important;
    margin-bottom: 8px !important;
}

/* Actual title + description values */
.leadbot-seo-snapshot-item p {
    font-size: 16px !important;
    line-height: 1.55 !important;
    font-weight: 650 !important;
    color: #0f172a !important;
}

/* Add a little breathing room between title and description */
.leadbot-seo-snapshot-item {
    margin-bottom: 20px !important;
}

/* Keep the whole SEO block separated from contact fields */
.leadbot-seo-snapshot {
    margin-top: 22px !important;
    padding-top: 18px !important;
}
</style>
<!-- LEADBOT SEO CARD TITLE DESC BIGGER END -->




<!-- LEADBOT SEO TITLE DESC 14PX START -->
<style>
/* Final calm size: Site Title + Meta Description inside lead cards. */
.lead-card .leadbot-seo-snapshot > strong,
.leadbot-live-card .leadbot-seo-snapshot > strong,
.leadbot-live-card-seo-values > strong {
    font-size: 14px !important;
    line-height: 1.3 !important;
    font-weight: 900 !important;
    color: #1e3a8a !important;
}

.lead-card .leadbot-seo-snapshot-item b,
.leadbot-live-card .leadbot-seo-snapshot-item b {
    font-size: 14px !important;
    line-height: 1.3 !important;
    font-weight: 900 !important;
    color: #1e3a8a !important;
}

.lead-card .leadbot-seo-snapshot-item p,
.leadbot-live-card .leadbot-seo-snapshot-item p,
.leadbot-live-card-seo-values p {
    font-size: 14px !important;
    line-height: 1.5 !important;
    font-weight: 600 !important;
    color: #0f172a !important;
}

.lead-card .leadbot-seo-snapshot-item,
.leadbot-live-card .leadbot-seo-snapshot-item {
    margin-bottom: 14px !important;
}
</style>
<!-- LEADBOT SEO TITLE DESC 14PX END -->


<!-- LEADBOT SEO UNBOLD WHY BOLD START -->
<style>
/* Keep SEO Snapshot readable but not chunky/bold. */
.lead-card .leadbot-seo-snapshot > strong,
.leadbot-live-card .leadbot-seo-snapshot > strong,
.leadbot-live-card-seo-values > strong {
    font-size: 14px !important;
    line-height: 1.3 !important;
    font-weight: 600 !important;
    color: #1e3a8a !important;
}

.lead-card .leadbot-seo-snapshot-item b,
.leadbot-live-card .leadbot-seo-snapshot-item b {
    font-size: 14px !important;
    line-height: 1.3 !important;
    font-weight: 600 !important;
    color: #1e3a8a !important;
}

.lead-card .leadbot-seo-snapshot-item p,
.leadbot-live-card .leadbot-seo-snapshot-item p,
.leadbot-live-card-seo-values p {
    font-size: 14px !important;
    line-height: 1.5 !important;
    font-weight: 400 !important;
    color: #0f172a !important;
}

/* Make Why This Lead stand out again. */
.reason b {
    font-weight: 950 !important;
    color: #1e3a8a !important;
}
</style>
<!-- LEADBOT SEO UNBOLD WHY BOLD END -->




<!-- LEADBOT WHY THIS LEAD BODY NORMAL START -->
<style>
/* Keep only the Why This Lead label bold. */
.reason b {
    font-weight: 950 !important;
    color: #1e3a8a !important;
}

/* Body text should be readable, not chunky. */
.reason p,
.reason ol,
.reason li {
    font-weight: 400 !important;
    color: #0f172a !important;
}
</style>
<!-- LEADBOT WHY THIS LEAD BODY NORMAL END -->


<!-- LEADBOT MOVE BLUE BG TO WHY THIS LEAD START -->
<style>
/* Remove blue/background treatment from title/header/SEO title areas. */
.lead-card h3,
.lead-card h3 a,
.lead-card .leadbot-seo-snapshot,
.lead-card .leadbot-seo-snapshot > strong,
.lead-card .leadbot-seo-snapshot-item,
.lead-card .leadbot-seo-snapshot-item b,
.leadbot-live-card h3,
.leadbot-live-card h3 a,
.leadbot-live-card .leadbot-seo-snapshot,
.leadbot-live-card .leadbot-seo-snapshot > strong,
.leadbot-live-card .leadbot-seo-snapshot-item,
.leadbot-live-card .leadbot-seo-snapshot-item b {
    background: transparent !important;
    background-image: none !important;
    box-shadow: none !important;
}

/* Keep SEO snapshot calm/readable. */
.lead-card .leadbot-seo-snapshot > strong,
.leadbot-live-card .leadbot-seo-snapshot > strong,
.leadbot-live-card-seo-values > strong,
.lead-card .leadbot-seo-snapshot-item b,
.leadbot-live-card .leadbot-seo-snapshot-item b {
    font-size: 14px !important;
    line-height: 1.3 !important;
    font-weight: 600 !important;
    color: #1e3a8a !important;
}

.lead-card .leadbot-seo-snapshot-item p,
.leadbot-live-card .leadbot-seo-snapshot-item p,
.leadbot-live-card-seo-values p {
    font-size: 14px !important;
    line-height: 1.5 !important;
    font-weight: 400 !important;
    color: #0f172a !important;
}

/* Put the blue background behind Why This Lead only. */
.lead-card,
.leadbot-live-card {
    overflow: hidden !important;
}

.lead-card .reason,
.leadbot-live-card .reason {
    margin: 18px -20px -20px -20px !important;
    padding: 16px 20px 18px !important;
    background: linear-gradient(135deg, #0f172a, #1e3a8a) !important;
    border: 0 !important;
    border-top: 1px solid rgba(147, 197, 253, 0.35) !important;
    border-radius: 0 0 18px 18px !important;
    box-shadow: none !important;
    color: #ffffff !important;
}

.lead-card .reason b,
.leadbot-live-card .reason b {
    display: block !important;
    margin-bottom: 7px !important;
    font-size: 13px !important;
    line-height: 1.25 !important;
    font-weight: 950 !important;
    color: #ffffff !important;
}

.lead-card .reason p,
.lead-card .reason ol,
.lead-card .reason li,
.leadbot-live-card .reason p,
.leadbot-live-card .reason ol,
.leadbot-live-card .reason li {
    font-size: 13px !important;
    line-height: 1.55 !important;
    font-weight: 400 !important;
    color: rgba(255,255,255,.92) !important;
}
</style>
<!-- LEADBOT MOVE BLUE BG TO WHY THIS LEAD END -->


<!-- LEADBOT WHY THIS LEAD SOFT BLUE START -->
<style>
/* Why This Lead should feel like a soft footer/note, not another header. */
.lead-card .reason,
.leadbot-live-card .reason {
    margin: 18px -20px -20px -20px !important;
    padding: 16px 20px 18px !important;

    background: linear-gradient(180deg, #f8fbff, #eaf2ff) !important;
    border-top: 1px solid #bfdbfe !important;
    border-left: 0 !important;
    border-right: 0 !important;
    border-bottom: 0 !important;
    border-radius: 0 0 18px 18px !important;

    color: #0f172a !important;
    box-shadow: none !important;
}

/* Keep the label strong, but blue-on-light instead of white-on-header-blue. */
.lead-card .reason b,
.leadbot-live-card .reason b {
    display: block !important;
    margin-bottom: 7px !important;
    font-size: 13px !important;
    line-height: 1.25 !important;
    font-weight: 950 !important;
    color: #1e3a8a !important;
}

/* Body text normal weight and dark/readable. */
.lead-card .reason p,
.lead-card .reason ol,
.lead-card .reason li,
.leadbot-live-card .reason p,
.leadbot-live-card .reason ol,
.leadbot-live-card .reason li {
    font-size: 13px !important;
    line-height: 1.55 !important;
    font-weight: 400 !important;
    color: #0f172a !important;
}
</style>
<!-- LEADBOT WHY THIS LEAD SOFT BLUE END -->


<!-- LEADBOT SMALLER SAVE DETAILS BUTTON START -->
<style>
/* Make Save Details / Save Contact Info button smaller and calmer. */
.lead-contact-save,
button.lead-contact-save,
.lead-contact-edit-form button[type="submit"] {
    width: fit-content !important;
    min-height: 24px !important;
    padding: 5px 9px !important;
    margin-top: 8px !important;
    margin-bottom: 12px !important;
    border-radius: 999px !important;
    font-size: 10px !important;
    line-height: 1 !important;
    font-weight: 900 !important;
    background: #1e3a8a !important;
    color: #ffffff !important;
    box-shadow: none !important;
}

/* Do not let generic button styling puff it back up. */
.lead-contact-edit-form .lead-contact-save {
    display: inline-flex !important;
    align-items: center !important;
    justify-content: center !important;
}
</style>
<!-- LEADBOT SMALLER SAVE DETAILS BUTTON END -->


<!-- LEADBOT OPEN DESKTOP FALLBACK FIX START -->
<script>
(function () {
    function findCsvNear(el) {
        if (!el) return "";

        var attrs = [
            "data-file",
            "data-filename",
            "data-export",
            "data-export-file",
            "data-current-file"
        ];

        for (var i = 0; i < attrs.length; i++) {
            var v = el.getAttribute && el.getAttribute(attrs[i]);
            if (v && /\.csv(\?|#|$)/i.test(v)) {
                return v.split("?")[0].split("#")[0].trim();
            }
        }

        var href = el.getAttribute && el.getAttribute("href");
        if (href && /\.csv/i.test(href)) {
            var mHref = href.match(/([^\/?#]+\.csv)/i);
            if (mHref) return mHref[1];
        }

        var root = el.closest && (
            el.closest(".leadbot-live-status") ||
            el.closest(".leadbot-live-results") ||
            el.closest(".leadbot-live-scan-results") ||
            el.closest(".leadbot-current-scan-direct") ||
            el.closest(".panel") ||
            document.body
        );

        var haystack = "";
        if (root) haystack += " " + (root.innerText || "");
        haystack += " " + (document.body.innerText || "");

        var matches = haystack.match(/[A-Za-z0-9_\-]+\.csv/g);
        if (matches && matches.length) {
            return matches[matches.length - 1].trim();
        }

        var currentName = document.getElementById("leadbotCurrentScanName");
        if (currentName && currentName.innerText) {
            var mName = currentName.innerText.match(/[A-Za-z0-9_\-]+\.csv/i);
            if (mName) return mName[0];
        }

        return "";
    }

    function isOpenDesktop(el) {
        if (!el) return false;
        var txt = (el.innerText || el.textContent || "").trim().toLowerCase();
        var id = (el.id || "").toLowerCase();
        var cls = (el.className || "").toString().toLowerCase();
        return (
            txt === "open desktop" ||
            txt.indexOf("open desktop") !== -1 ||
            id.indexOf("opendesktop") !== -1 ||
            id.indexOf("open-desktop") !== -1 ||
            cls.indexOf("open-desktop") !== -1 ||
            cls.indexOf("openDesktop") !== -1
        );
    }

    document.addEventListener("click", function (e) {
        var el = e.target && e.target.closest && e.target.closest("a, button");
        if (!isOpenDesktop(el)) return;

        var href = el.getAttribute("href") || "";

        // If it already has a useful dashboard URL, let it work normally.
        if (href && href !== "#" && href.indexOf("javascript:") !== 0) {
            if (href.indexOf("/lead-bot") !== -1 && (href.indexOf("file=") !== -1 || href.indexOf("export=") !== -1)) {
                return;
            }
        }

        e.preventDefault();

        var csv = findCsvNear(el);

        if (csv) {
            window.location.href = "/lead-bot?file=" + encodeURIComponent(csv);
        } else {
            window.location.href = "/lead-bot";
        }
    }, true);
})();
</script>
<!-- LEADBOT OPEN DESKTOP FALLBACK FIX END -->


<!-- LEADBOT DASHBOARD AUTO OPEN LATEST EXPORT START -->
<script>
(function () {
    function hasFileParam() {
        try {
            var params = new URLSearchParams(window.location.search || "");
            return !!(params.get("file") || params.get("filename") || params.get("export"));
        } catch (e) {
            return false;
        }
    }

    function pageLooksEmpty() {
        var bodyText = (document.body.innerText || "").toLowerCase();
        return (
            bodyText.indexOf("no leads found yet") !== -1 &&
            bodyText.indexOf("run the bot or select an export") !== -1
        );
    }

    function findBestExportHref() {
        var links = Array.from(document.querySelectorAll("a.file-link, .file-list a, a[href*='.csv'], a[href*='file=']"));

        links = links.filter(function (a) {
            var href = a.getAttribute("href") || "";
            var text = a.innerText || "";
            return (
                href.indexOf("/lead-bot") !== -1 ||
                href.indexOf("file=") !== -1 ||
                href.indexOf(".csv") !== -1 ||
                text.indexOf(".csv") !== -1
            );
        });

        if (!links.length) return "";

        // Prefer an active-looking newest/top export link first.
        var first = links[0];
        var href = first.getAttribute("href") || "";

        if (href && href !== "#" && href.indexOf("javascript:") !== 0) {
            return href;
        }

        var csvText = first.innerText || "";
        var m = csvText.match(/[A-Za-z0-9_\-]+\.csv/i);
        if (m) {
            return "/lead-bot?file=" + encodeURIComponent(m[0]) + "#results";
        }

        return "";
    }

    function runFallback() {
        // Only help when the dashboard is empty.
        if (!pageLooksEmpty()) return;

        // If the URL already has a file param, do not loop forever.
        if (hasFileParam()) return;

        var href = findBestExportHref();
        if (!href) return;

        console.log("LeadBot dashboard empty; opening latest export:", href);
        window.location.href = href;
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", function () {
            setTimeout(runFallback, 250);
        });
    } else {
        setTimeout(runFallback, 250);
    }
})();
</script>
<!-- LEADBOT DASHBOARD AUTO OPEN LATEST EXPORT END -->




<!-- LEADBOT LIVE SCAN REMOVE BLOCK BUTTON START -->
<style>
/* Hide Block button only on live/pre-dashboard scan cards. */
#leadbotLiveResults .lead-block-one-js,
#leadbotLiveResultsList .lead-block-one-js,
#leadbotLiveCards .lead-block-one-js,
#leadbotLiveScanResults .lead-block-one-js,
#leadbotLiveScanOutput .lead-block-one-js,
.leadbot-live-results .lead-block-one-js,
.leadbot-live-scan-results .lead-block-one-js,
.leadbot-live-card .lead-block-one-js,
#leadbotLiveResults a[href*="block"],
#leadbotLiveResultsList a[href*="block"],
#leadbotLiveCards a[href*="block"],
#leadbotLiveScanResults a[href*="block"],
#leadbotLiveScanOutput a[href*="block"] {
    display: none !important;
}
</style>

<script>
(function () {
    function isLiveRoot(node) {
        if (!node || !node.closest) return false;
        return !!node.closest(
            "#leadbotLiveResults, #leadbotLiveResultsList, #leadbotLiveCards, #leadbotLiveScanResults, #leadbotLiveScanOutput, .leadbot-live-results, .leadbot-live-scan-results, .leadbot-live-card"
        );
    }

    function removeLiveBlockButtons(root) {
        if (!/\/lead-bot\/live\//.test(window.location.pathname || "")) return;
        root = root || document;
        var buttons = root.querySelectorAll ? root.querySelectorAll("a, button") : [];
        buttons.forEach(function (btn) {
            if (!isLiveRoot(btn)) return;

            var txt = (btn.innerText || btn.textContent || "").trim().toLowerCase();
            var href = (btn.getAttribute("href") || "").toLowerCase();
            var cls = (btn.className || "").toString().toLowerCase();

            if (
                txt === "block" ||
                txt.indexOf("block") !== -1 ||
                href.indexOf("block") !== -1 ||
                cls.indexOf("lead-block") !== -1
            ) {
                btn.remove();
            }
        });
    }

    removeLiveBlockButtons(document);

    var obs = new MutationObserver(function (mutations) {
        mutations.forEach(function (m) {
            m.addedNodes.forEach(function (node) {
                if (node.nodeType === 1) removeLiveBlockButtons(node);
            });
        });
    });

    obs.observe(document.body, { childList: true, subtree: true });
})();
</script>
<!-- LEADBOT LIVE SCAN REMOVE BLOCK BUTTON END -->




<!-- LEADBOT LIVE SCAN GLOBAL REMOVE BLOCK BUTTON START -->
<style>
/* Hide Block button on live/pre-dashboard scan page only. */
body.leadbot-live-page .lead-block-one-js,
body.leadbot-live-page .leadbot-block-one-js,
body.leadbot-live-page a[href*="block"],
body.leadbot-live-page button[data-action*="block"],
body.leadbot-live-page button[data-action="block"] {
    display: none !important;
}
</style>
<!-- LEADBOT LIVE SCAN GLOBAL REMOVE BLOCK BUTTON END -->

<!-- LEADBOT MOBILE HEADER FIX START -->
<style>
@media (max-width: 700px) {
    /* .hero > div.leadbot-brand matches the same element as .hero > div:first-child
       (the earlier grid-layout rule) at equal specificity, so being declared last
       here wins the cascade tie and actually forces flex instead of grid. */
    .hero > div.leadbot-brand {
        display: flex !important;
        flex-direction: column !important;
        align-items: center !important;
        text-align: center !important;
        gap: 16px !important;
    }

    .leadbot-brand-left {
        display: flex !important;
        flex-direction: column !important;
        align-items: center !important;
        text-align: center !important;
        width: 100% !important;
        gap: 10px !important;
    }

    .leadbot-logo-link {
        display: flex !important;
        justify-content: center !important;
        width: 100% !important;
    }

    .leadbot-logo {
        height: auto !important;
        max-height: 44px !important;
        max-width: 230px !important;
        width: auto !important;
    }

    .leadbot-brand-left h1 {
        font-size: 22px !important;
        line-height: 1.08 !important;
        margin: 0 !important;
    }

    .leadbot-brand-left p {
        font-size: 13px !important;
        line-height: 1.35 !important;
        max-width: 280px !important;
        margin: 0 auto !important;
    }

    .leadbot-nav {
        display: flex !important;
        flex-wrap: wrap !important;
        justify-content: center !important;
        width: 100% !important;
        gap: 10px !important;
        white-space: normal !important;
        margin: 0 !important;
    }

    .leadbot-nav a {
        flex: 1 1 calc(50% - 10px) !important;
        max-width: 150px !important;
        min-height: 40px !important;
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
        text-align: center !important;
    }

    .container {
        padding: 14px !important;
    }
}
</style>
<!-- LEADBOT MOBILE HEADER FIX END -->

<!-- LEADBOT MOBILE LEAD CARD FIX START -->
<style>
@media (max-width: 700px) {
    /* Reclaim the desktop delete/block-button gutter so the title/domain
       block gets the full card width instead of ~45% of it. */
    .lead-head {
        align-items: stretch !important;
        padding-right: 8px !important;
    }

    .lead-head > div:first-child {
        min-width: 0 !important;
        width: 100% !important;
    }

    /* A page-load script moves this button out of form.lead-contact-edit-form
       into .leadbot-save-details-row, so a plain ".lead-contact-save" class
       selector loses to the existing "button.lead-contact-save" (type+class)
       rule regardless of source order. Match that specificity directly. */
    .lead-contact-save,
    button.lead-contact-save,
    .lead-address-box button,
    .lead-address-box button[type="submit"] {
        min-height: 44px !important;
        padding-top: 10px !important;
        padding-bottom: 10px !important;
        width: 100% !important;
    }
}
</style>
<!-- LEADBOT MOBILE LEAD CARD FIX END -->

<script>
(function () {
    function isLiveScanPage() {
        var path = (window.location.pathname || "").toLowerCase();

        // Strict path-only check.
        // The /lead-bot dashboard can contain text like "Open Desktop" or "Live Scan",
        // so text-based detection wrongly hides dashboard Block buttons.
        return path.indexOf("/lead-bot/live") !== -1;
    }

    function markLivePage() {
        if (isLiveScanPage()) {
            document.body.classList.add("leadbot-live-page");
        }
    }

    function isBlockButton(el) {
        if (!el) return false;

        var txt = (el.innerText || el.textContent || "").trim().toLowerCase();
        var href = (el.getAttribute && el.getAttribute("href") || "").toLowerCase();
        var cls = (el.className || "").toString().toLowerCase();
        var id = (el.id || "").toLowerCase();
        var action = (el.getAttribute && (
            el.getAttribute("data-action") ||
            el.getAttribute("data-mode") ||
            el.getAttribute("name") ||
            ""
        ) || "").toLowerCase();

        return (
            txt === "block" ||
            txt === "🚫 block" ||
            txt.indexOf("block") !== -1 ||
            href.indexOf("block") !== -1 ||
            cls.indexOf("block") !== -1 ||
            id.indexOf("block") !== -1 ||
            action.indexOf("block") !== -1
        );
    }

    function removeBlockButtons(root) {
        if (!/\/lead-bot\/live\//.test(window.location.pathname || "")) return;
        if (!isLiveScanPage()) return;

        markLivePage();

        root = root || document;
        var buttons = root.querySelectorAll ? root.querySelectorAll("a, button") : [];

        buttons.forEach(function (btn) {
            if (isBlockButton(btn)) {
                btn.remove();
            }
        });
    }

    function start() {
        markLivePage();
        removeBlockButtons(document);

        var obs = new MutationObserver(function (mutations) {
            mutations.forEach(function (m) {
                m.addedNodes.forEach(function (node) {
                    if (node.nodeType === 1) {
                        removeBlockButtons(node);
                    }
                });
            });
        });

        obs.observe(document.body, { childList: true, subtree: true });

        // Live cards often render after status polling, so keep sweeping lightly.
        var tries = 0;
        var timer = setInterval(function () {
            tries += 1;
            removeBlockButtons(document);
            if (tries >= 60) clearInterval(timer);
        }, 500);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", start);
    } else {
        start();
    }
})();
</script>
<!-- LEADBOT LIVE SCAN GLOBAL REMOVE BLOCK BUTTON END -->








<!-- LEADBOT PERMANENT INFO BAR FIX START -->
<script>
(function () {
    function titleCase(s) {
        return (s || "")
            .replace(/[_-]+/g, " ")
            .replace(/\s+/g, " ")
            .trim()
            .replace(/\b[a-z]/g, function (m) { return m.toUpperCase(); });
    }

    function parseFromBadKeyword(raw) {
        var cleaned = (raw || "")
            .toLowerCase()
            .replace(/\s+/g, " ")
            .trim();

        // bagel store erie pa 20260612 112100 -> bagel store erie pa
        cleaned = cleaned.replace(/\b20\d{6}\s+\d{6}\b/g, "").trim();

        var parts = cleaned.split(" ").filter(Boolean);

        if (parts.length >= 4) {
            var state = parts.pop();
            var city = parts.pop();

            return {
                keyword: titleCase(parts.join(" ")),
                location: titleCase(city + " " + state)
            };
        }

        return {
            keyword: titleCase(cleaned),
            location: ""
        };
    }

    function parseFromFilename(raw) {
        var name = (raw || "").split("/").pop().split("?")[0].split("#")[0];

        name = name.replace(/\.csv$/i, "");
        name = name.replace(/^leads_+/i, "");
        name = name.replace(/_desktop$/i, "");
        name = name.replace(/_enriched$/i, "");
        name = name.replace(/_\d{8}_\d{6}$/i, "");

        var parts = name.split("_").filter(Boolean);

        if (parts.length >= 3) {
            var state = parts.pop();
            var city = parts.pop();

            return {
                keyword: titleCase(parts.join(" ")),
                location: titleCase(city + " " + state)
            };
        }

        return {
            keyword: titleCase(parts.join(" ")),
            location: ""
        };
    }

    function getCurrentParsedValues() {
        // Best source: URL file param.
        try {
            var params = new URLSearchParams(window.location.search || "");
            var file = params.get("file") || params.get("filename") || params.get("export") || "";
            if (file && /\.csv/i.test(file)) {
                return parseFromFilename(file);
            }
        } catch (e) {}

        // Second source: visible bad keyword value.
        var text = document.body.innerText || "";
        var bad = text.match(/[a-z][a-z\s]+?\s+[a-z]+\s+[a-z]{2}\s+20\d{6}\s+\d{6}/i);
        if (bad) {
            return parseFromBadKeyword(bad[0]);
        }

        // Last source: any CSV in the page.
        var html = document.documentElement.innerHTML || "";
        var files = html.match(/[A-Za-z0-9_-]+\.csv/g) || [];

        for (var i = 0; i < files.length; i++) {
            if (/_desktop\.csv$/i.test(files[i])) {
                return parseFromFilename(files[i]);
            }
        }

        if (files.length) return parseFromFilename(files[0]);

        return { keyword: "", location: "" };
    }

    function findInfoBoxes() {
        var boxes = [];

        document.querySelectorAll("div, section, article").forEach(function (el) {
            var txt = (el.innerText || "").trim();

            if (
                /\bKeyword\b/i.test(txt) &&
                /\bLocation\b/i.test(txt) &&
                /\bLeads\b/i.test(txt) &&
                txt.length < 500
            ) {
                boxes.push(el);
            }
        });

        // Smallest useful containers first.
        boxes.sort(function (a, b) {
            return (a.innerText || "").length - (b.innerText || "").length;
        });

        return boxes.slice(0, 5);
    }

    function setLabelValueInBox(box, label, value) {
        if (!box || !value) return false;

        var changed = false;
        var nodes = Array.from(box.querySelectorAll("b, strong, span, div, p"));

        nodes.forEach(function (node) {
            var txt = (node.innerText || node.textContent || "").trim().toLowerCase();
            if (txt !== label.toLowerCase()) return;

            var parent = node.parentElement;
            if (!parent) return;

            var candidates = Array.from(parent.children).filter(function (child) {
                if (child === node) return false;
                var childText = (child.innerText || child.textContent || "").trim();
                return childText && childText.toLowerCase() !== label.toLowerCase();
            });

            if (candidates.length) {
                if ((candidates[0].textContent || "").trim() !== value) {
                    candidates[0].textContent = value;
                    changed = true;
                }
            } else {
                // Fallback for plain text directly after label.
                var html = parent.innerHTML;
                var re = new RegExp("(" + label + "\\s*</[^>]+>\\s*)([^<]+)", "i");
                var newHtml = html.replace(re, "$1" + value);
                if (newHtml !== html) {
                    parent.innerHTML = newHtml;
                    changed = true;
                }
            }
        });

        return changed;
    }

    function directTextRepair(parsed) {
        if (!parsed.keyword || !parsed.location) return;

        var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
        var node;

        while ((node = walker.nextNode())) {
            var txt = (node.nodeValue || "").replace(/\s+/g, " ").trim();

            if (/\b20\d{6}\s+\d{6}\b/.test(txt) && txt.toLowerCase().indexOf(".csv") === -1) {
                node.nodeValue = parsed.keyword;
            }

            if (txt.toLowerCase() === "desktop") {
                node.nodeValue = parsed.location;
            }
        }
    }

    function fixInfoBar() {
        var parsed = getCurrentParsedValues();
        if (!parsed.keyword || !parsed.location) return;

        var boxes = findInfoBoxes();

        boxes.forEach(function (box) {
            setLabelValueInBox(box, "Keyword", parsed.keyword);
            setLabelValueInBox(box, "Location", parsed.location);
            setLabelValueInBox(box, "Market", parsed.location);
        });

        directTextRepair(parsed);
    }

    function start() {
        // Fix immediately.
        fixInfoBar();

        // Fix after other scripts/polling rewrite it.
        setTimeout(fixInfoBar, 100);
        setTimeout(fixInfoBar, 500);
        setTimeout(fixInfoBar, 1200);
        setTimeout(fixInfoBar, 2500);

        // Permanent watchdog. Yes, ugly. But the old script is uglier.
        setTimeout(fixInfoBar, 300);
    setTimeout(fixInfoBar, 1200);
    setTimeout(fixInfoBar, 2500);

        var obs = new MutationObserver(function () {
            fixInfoBar();
        });

        obs.observe(document.body, {
            childList: true,
            subtree: true,
            characterData: true
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", start);
    } else {
        start();
    }
})();
</script>
<!-- LEADBOT PERMANENT INFO BAR FIX END -->




<!-- LEADBOT EMPTY EXPORT REDIRECT TO DESKTOP START -->
<script>
(function () {
    function pageIsEmptyDashboard() {
        var txt = (document.body.innerText || "").toLowerCase();
        return (
            txt.indexOf("no leads found yet") !== -1 &&
            txt.indexOf("run the bot or select an export") !== -1
        );
    }

    function currentFile() {
        try {
            var params = new URLSearchParams(window.location.search || "");
            return params.get("file") || params.get("filename") || params.get("export") || "";
        } catch (e) {
            return "";
        }
    }

    function cleanCsvName(s) {
        s = String(s || "");
        var m = s.match(/[A-Za-z0-9_-]+\.csv/i);
        return m ? m[0] : "";
    }

    function findDesktopExport() {
        var cur = cleanCsvName(currentFile());
        var links = Array.from(document.querySelectorAll("a[href], .file-link"));

        var found = [];

        links.forEach(function (a) {
            var href = a.getAttribute("href") || "";
            var txt = a.innerText || "";
            var csv = cleanCsvName(href) || cleanCsvName(txt);

            if (!csv) return;
            if (csv === cur) return;

            var score = 0;
            if (/_desktop\.csv$/i.test(csv)) score += 100;
            if (/\.csv$/i.test(csv)) score += 1;

            found.push({
                csv: csv,
                href: href,
                score: score
            });
        });

        found.sort(function (a, b) {
            return b.score - a.score;
        });

        if (!found.length) return "";

        var best = found[0];

        if (best.href && best.href !== "#" && best.href.indexOf("javascript:") !== 0) {
            if (best.href.indexOf("/lead-bot") !== -1 || best.href.indexOf("file=") !== -1) {
                return best.href;
            }
        }

        return "/lead-bot?file=" + encodeURIComponent(best.csv) + "#results";
    }

    function fixEmptyExport() {
        if (!pageIsEmptyDashboard()) return;

        var href = findDesktopExport();
        if (!href) return;

        console.log("LeadBot empty export selected; redirecting to desktop export:", href);
        window.location.href = href;
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", function () {
            setTimeout(fixEmptyExport, 250);
            setTimeout(fixEmptyExport, 900);
        });
    } else {
        setTimeout(fixEmptyExport, 250);
        setTimeout(fixEmptyExport, 900);
    }
})();
</script>
<!-- LEADBOT EMPTY EXPORT REDIRECT TO DESKTOP END -->


<!-- LEADBOT FORCE LIVE START SUBMIT START -->
<script>
(function () {
    if (window.__leadbotForceLiveStartSubmitInstalled) return;
    window.__leadbotForceLiveStartSubmitInstalled = true;

    function fieldValue(form, name, fallback) {
        var el = form.querySelector('[name="' + name + '"]');
        var value = el ? String(el.value || "").trim() : "";
        return value || fallback || "";
    }

    function numberField(form, hiddenName, displayId, fallback) {
        var hidden = form.querySelector('[name="' + hiddenName + '"]');
        var display = displayId ? document.getElementById(displayId) : null;

        var value = "";

        if (display && String(display.value || "").trim()) {
            value = String(display.value || "").trim();
        } else if (hidden && String(hidden.value || "").trim()) {
            value = String(hidden.value || "").trim();
        }

        value = parseInt(value || fallback, 10);

        if (!Number.isFinite(value) || value < 1) {
            value = fallback;
        }

        return value;
    }

    function buildLiveStartUrl(form) {
        var industry = fieldValue(form, "industry", "");
        var market = fieldValue(form, "market", "");
        var keyword = fieldValue(form, "keyword", industry);
        var ownDomain = fieldValue(form, "own_domain", "");

        var limit = numberField(form, "limit", "leadbotLimitDisplay", 25);
        var perQueryLimit = numberField(form, "per_query_limit", "leadbotPerQueryLimitDisplay", 12);
        var maxQueries = numberField(form, "max_queries", "leadbotMaxQueriesDisplay", 12);

        var preset = document.getElementById("scanSizePreset");
        if (preset) {
            if (preset.value === "preview") {
                limit = 1;
                perQueryLimit = 4;
                maxQueries = 2;
            } else if (preset.value === "quick") {
                limit = 8;
                perQueryLimit = 8;
                maxQueries = 4;
            } else if (preset.value === "standard") {
                limit = 25;
                perQueryLimit = 12;
                maxQueries = 6;
            } else if (preset.value === "deep") {
                limit = 50;
                perQueryLimit = 12;
                maxQueries = 10;
            }
        }

        var params = new URLSearchParams();
        params.set("industry", industry);
        params.set("market", market);
        params.set("keyword", keyword);
        params.set("own_domain", ownDomain);
        params.set("limit", String(limit));
        params.set("per_query_limit", String(perQueryLimit));
        params.set("max_queries", String(maxQueries));

        return "/lead-bot/live-start?" + params.toString();
    }

    function launchLiveStart(event) {
        var form = document.getElementById("leadbotRunForm");
        if (!form) return;

        var industry = fieldValue(form, "industry", "");
        var market = fieldValue(form, "market", "");

        if (!industry || !market) {
            return;
        }

        if (event) {
            event.preventDefault();
            event.stopPropagation();
            if (event.stopImmediatePropagation) {
                event.stopImmediatePropagation();
            }
        }

        var btn = document.getElementById("leadbotStartScanButton");
        if (btn) {
            btn.disabled = true;
            btn.textContent = "Starting LeadBot...";
        }

        window.location.href = buildLiveStartUrl(form);
    }

    document.addEventListener("click", function (event) {
        var btn = event.target && event.target.closest ? event.target.closest("#leadbotStartScanButton") : null;
        if (!btn) return;
        launchLiveStart(event);
    }, true);

    document.addEventListener("submit", function (event) {
        var form = event.target;
        if (!form || form.id !== "leadbotRunForm") return;
        launchLiveStart(event);
    }, true);
})();
</script>
<!-- LEADBOT FORCE LIVE START SUBMIT END -->
















<!-- LEADBOT REMOVE HANGING DELETE TEXT START -->
<script>
(function () {
    function removeHangingDeleteText() {
        var cardSelectors = [
            ".lead-card",
            ".leadbot-card",
            ".leadbot-lead-card",
            ".lead-result-card"
        ];

        var cards = [];
        cardSelectors.forEach(function (selector) {
            document.querySelectorAll(selector).forEach(function (card) {
                if (cards.indexOf(card) === -1) cards.push(card);
            });
        });

        cards.forEach(function (card) {
            Array.from(card.querySelectorAll("a")).forEach(function (a) {
                var text = (a.textContent || "").trim().toLowerCase();
                var cls = (a.className || "").toString().toLowerCase();

                if (text !== "delete") return;

                // Keep real styled delete buttons/actions.
                if (cls.indexOf("btn") !== -1) return;
                if (cls.indexOf("button") !== -1) return;
                if (cls.indexOf("action") !== -1) return;
                if (cls.indexOf("danger") !== -1) return;
                if (cls.indexOf("pill") !== -1) return;

                // Remove only loose top-level hanging delete links.
                var parent = a.parentElement;
                if (parent === card || parent.parentElement === card) {
                    a.remove();
                }
            });
        });
    }

    document.addEventListener("DOMContentLoaded", removeHangingDeleteText);
    setTimeout(removeHangingDeleteText, 250);
    setTimeout(removeHangingDeleteText, 1000);
})();
</script>
<!-- LEADBOT REMOVE HANGING DELETE TEXT END -->





</body>
</html>
"""

    page = page.replace("__FILES__", "".join(file_links) if file_links else '<div class="empty">No exports yet.</div>')
    page = page.replace("__SELECTED__", html.escape(selected_name))
    page = page.replace("__COUNT__", str(len(rows)))
    page = page.replace("__DOWNLOAD__", download)
    page = page.replace("__CARDS__", lead_cards(rows, selected_name=file))

    page = _leadbot_remove_dataforseo_ui_for_non_admin(page, current_user=current_user)
    return page

# === LEADBOT CSV SIZE CAP START ===
# Safety guard:
# Do not let the dashboard read/render giant CSV files.
# A 158 MB export once produced a 165 MB dashboard page and caused browser OOM.
try:
    _LEADBOT_ORIGINAL_READ_CSV_ROWS = read_csv_rows

    LEADBOT_DASHBOARD_MAX_CSV_BYTES = 2 * 1024 * 1024
    LEADBOT_DASHBOARD_MAX_PREVIEW_ROWS = 50

    def _leadbot_find_csv_path_from_args(*args, **kwargs):
        from pathlib import Path

        values = list(args) + list(kwargs.values())

        for value in values:
            if isinstance(value, Path):
                candidates = [value]
            elif isinstance(value, str):
                candidates = [Path(value)]
                if not value.startswith("exports/"):
                    candidates.append(Path("exports") / value)
            else:
                continue

            for candidate in candidates:
                try:
                    if candidate.suffix.lower() == ".csv" and candidate.exists():
                        return candidate
                except Exception:
                    pass

        return None

    def read_csv_rows(*args, **kwargs):
        csv_path = _leadbot_find_csv_path_from_args(*args, **kwargs)

        if csv_path is not None:
            try:
                size_bytes = csv_path.stat().st_size
                if size_bytes > LEADBOT_DASHBOARD_MAX_CSV_BYTES:
                    print(
                        f"[LeadBot dashboard] Large CSV preview skipped: "
                        f"{csv_path} ({round(size_bytes / 1024 / 1024, 2)} MB)"
                    )
                    return []
            except Exception as exc:
                print(f"[LeadBot dashboard] CSV size check failed for {csv_path}: {exc}")

        rows = _LEADBOT_ORIGINAL_READ_CSV_ROWS(*args, **kwargs)

        try:
            if isinstance(rows, list) and len(rows) > LEADBOT_DASHBOARD_MAX_PREVIEW_ROWS:
                return rows[:LEADBOT_DASHBOARD_MAX_PREVIEW_ROWS]
        except Exception:
            pass

        return rows

except NameError:
    print("[LeadBot dashboard] read_csv_rows was not available for CSV size cap.")
# === LEADBOT CSV SIZE CAP END ===



