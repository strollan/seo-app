"""Captured search metadata; never derive ranks from scores, pages or addresses."""
from __future__ import annotations

import csv
import json
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

FIELDS = ("base_keyword", "search_location", "serp_position", "serp_page",
          "scan_date", "query_used", "lead_score", "ranking_type")
MISSING = {"", "none", "null", "nan", "undefined", "unknown", "not found",
           "not recorded", "manual", "?", "-", "—", "n/a"}


def clean(value):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    text = str(value).strip()
    return "" if text.lower() in MISSING else text


def first(row, *keys):
    return next((clean(row.get(key)) for key in keys if clean(row.get(key))), "")


def positive(value):
    text = clean(value)
    return int(text) if re.fullmatch(r"[1-9][0-9]{0,8}", text) else None


def scan_date(value):
    text = clean(value)
    try:
        if len(text) == 10:
            date.fromisoformat(text)
        else:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return text


def from_row(row):
    row = row if isinstance(row, dict) else {}
    source = " ".join(first(row, key) for key in ("source", "lead_source_label")).lower()
    kind = first(row, "ranking_type").lower()
    page = first(row, "serp_page").lower()
    nonorganic = (any(word in " ".join((source, kind, page))
                      for word in ("places", "maps", "local_pack", "local pack", "paid", "sponsored"))
                  or bool(kind and kind not in {"organic", "google organic"}))
    score = first(row, "lead_score", "final_lead_score", "seo_opportunity_score", "score")
    if not re.fullmatch(r"(?:[0-9]{1,2}(?:\.[0-9]+)?|100(?:\.0+)?)", score):
        score = ""
    return {
        "base_keyword": first(row, "base_keyword", "keyword"),
        # `location` can be a business address in old exports. Never use it.
        "search_location": first(row, "search_location", "market"),
        "serp_position": None if nonorganic else positive(row.get("serp_position")),
        "serp_page": None if nonorganic else positive(row.get("serp_page")),
        "scan_date": scan_date(first(row, "scan_date", "original_scan_date")),
        "query_used": first(row, "query_used"),
        "lead_score": score,
        "ranking_type": "nonorganic" if nonorganic else "organic",
    }


def domain(value):
    text = clean(value).lower()
    parsed = urlparse(text if "://" in text else "//" + text)
    return (parsed.hostname or "").removeprefix("www.").strip(".")


def safe_json(path):
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def from_export(path, source_domain, jobs_dir=None):
    """Read RAW rows: dashboard normalization may guess location from filenames.

    Require an exact domain match. Duplicate conflicting rows only contribute
    values that agree. Date comes from a captured field or an exactly linked
    scan job, never a filename, filesystem timestamp or import timestamp.
    """
    if not domain(source_domain):
        return None
    matched = []
    recorded_base = []
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            return None
        with path.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                if domain(first(row, "domain", "website", "url")) == domain(source_domain):
                    matched.append(from_row(row))
                    recorded_base.append(first(row, "base_keyword"))
    except (OSError, ValueError, csv.Error):
        return None
    if not matched:
        return None
    result = {key: matched[0][key] if all(m[key] == matched[0][key] for m in matched)
              else (None if key in {"serp_page", "serp_position"} else "") for key in FIELDS}
    if jobs_dir is not None and jobs_dir.is_dir():
        # A job's scan parameters retain the original search, including when
        # an older CSV omitted base_keyword. Only exact export links qualify.
        jobs = [job for file in jobs_dir.glob("*.json")
                if file.resolve().parent == jobs_dir.resolve()
                and (job := safe_json(file)).get("export_file") == path.name]
        if len(jobs) == 1:
            job = jobs[0]
            params = job.get("params") if isinstance(job.get("params"), dict) else {}
            original_keyword = first(params, "base_keyword", "keyword")
            if not any(recorded_base) and original_keyword:
                result["base_keyword"] = original_keyword
            result["search_location"] = result["search_location"] or first(params, "market")
            result["scan_date"] = result["scan_date"] or scan_date(job.get("created_at"))
    return result
