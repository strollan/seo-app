"""Validated, source-controlled local imports for Slayer guide packages.

This module intentionally has no HTTP client, Git, SSH, or deployment code.
An import only writes a JSON guide record and copied static assets into this
LeadMeLeads checkout, where they can be reviewed and deployed separately.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


APP_DIR = Path(__file__).resolve().parent
CONTENT_DIR = APP_DIR / "content" / "imported_guides"
ASSET_ROOT = APP_DIR / "static" / "images" / "resources" / "imported"
MANIFEST_NAME = "slayer-guide.json"
LOCAL_BASE_URL = "http://127.0.0.1:8800"
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
RESOURCE_TOPICS = frozenset({"finding-leads", "verification", "website-seo", "outreach-comparison"})
# Article paragraphs can legitimately be much longer than short metadata and
# labels. Keep them bounded while allowing approved long-form copy through.
PARAGRAPH_MAXIMUM = 4_000
# Structured body format. Records without ``blocks`` keep rendering from the
# legacy ``paragraphs``/``bullets`` fields, so published guides do not change.
BODY_FORMAT = 2
SITE_HOSTS = frozenset({"leadmeleads.com", "www.leadmeleads.com"})
SAFE_PATH_RE = re.compile(r"^/[A-Za-z0-9._~%!$'()*+,;=:@/-]*(?:\?[^\s#]*)?(?:#\S*)?$")
SPAN_FLAGS = ("bold", "italic", "code")
MAX_TABLE_COLUMNS = 8

# Root routes owned by the app that are not part of the public SEO list. This
# keeps an import from shadowing an application route once the generic route
# is installed at the end of app.main.
RESERVED_ROOT_SLUGS = frozenset({
    "analyze", "compare", "contact", "create-account", "export-pdf",
    "forgot-password", "history", "lead-bot", "login", "logout",
    "privacy-policy", "reports", "reset-password", "robots.txt", "settings",
    "signup", "sitemap.xml", "static",
})


class GuideImportError(ValueError):
    """A package cannot be imported without changing the local checkout."""


def _text(value: Any, name: str, *, maximum: int = 500) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GuideImportError(f"{name} is required.")
    value = value.strip()
    if len(value) > maximum:
        raise GuideImportError(f"{name} exceeds {maximum} characters.")
    return value


def _safe_slug(value: Any) -> str:
    slug = _text(value, "slug", maximum=100).strip("/")
    if slug != value.strip() or not SLUG_RE.fullmatch(slug):
        raise GuideImportError("slug must be one lowercase, hyphenated URL segment.")
    if slug in RESERVED_ROOT_SLUGS:
        raise GuideImportError(f"slug '{slug}' is reserved by LeadMeLeads.")
    # Import here avoids making this module depend on FastAPI/app.main.
    from app import seo_meta
    if f"/{slug}" in seo_meta.PUBLIC_INDEXABLE_PATHS:
        raise GuideImportError(f"slug '{slug}' already belongs to a hand-built guide.")
    return slug


def _relative_source(package_dir: Path, value: Any, name: str) -> Path:
    relative = _text(value, name, maximum=300)
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise GuideImportError(f"{name} must be a safe relative package path.")
    source = (package_dir / candidate).resolve()
    try:
        source.relative_to(package_dir)
    except ValueError as exc:
        raise GuideImportError(f"{name} escapes the package directory.") from exc
    if not source.is_file():
        raise GuideImportError(f"{name} does not name an existing file.")
    if source.suffix.lower() not in IMAGE_EXTENSIONS or not _image_bytes_are_valid(source):
        raise GuideImportError(f"{name} is not a supported PNG, JPEG, GIF, or WebP image.")
    return source


def _image_bytes_are_valid(path: Path) -> bool:
    try:
        prefix = path.read_bytes()[:16]
    except OSError:
        return False
    return (
        prefix.startswith(b"\x89PNG\r\n\x1a\n")
        or prefix.startswith(b"\xff\xd8\xff")
        or prefix.startswith((b"GIF87a", b"GIF89a"))
        or (prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP")
    )


def _image(value: Any, package_dir: Path, name: str) -> tuple[Path, str]:
    if not isinstance(value, dict):
        raise GuideImportError(f"{name} must be an object with path and alt.")
    return _relative_source(package_dir, value.get("path"), f"{name}.path"), _text(
        value.get("alt"), f"{name}.alt", maximum=300
    )


def _safe_href(value: Any, name: str) -> str:
    """Return a site-relative path or absolute http(s) URL, or refuse it.

    Links to our own site are stored site-relative so that a guide never
    points at the wrong host. Nothing else (javascript:, data:, //host,
    backslash tricks, embedded credentials) can reach the template.
    """
    href = _text(value, name, maximum=500)
    if any(ord(char) <= 32 or ord(char) == 127 or char == "\\" for char in href):
        raise GuideImportError(f"{name} contains characters that are not allowed in a link.")
    try:
        parts = urlsplit(href)
        hostname = parts.hostname
    except ValueError as exc:
        raise GuideImportError(f"{name} is not a valid link.") from exc
    if parts.scheme in {"http", "https"} and hostname:
        if parts.username or parts.password:
            raise GuideImportError(f"{name} must not contain credentials.")
        if hostname.lower() not in SITE_HOSTS:
            return href
        href = parts.path or "/"
        href += f"?{parts.query}" if parts.query else ""
        href += f"#{parts.fragment}" if parts.fragment else ""
    elif parts.scheme or parts.netloc:
        raise GuideImportError(f"{name} must be a site path or an http(s) URL.")
    if not SAFE_PATH_RE.fullmatch(href) or href.startswith("//") or ".." in href.split("/"):
        raise GuideImportError(f"{name} is not a safe site path.")
    return href


def _spans(value: Any, name: str) -> list[dict[str, Any]]:
    """Validate inline text runs. Stored as data; the template escapes them."""
    if not isinstance(value, list) or not value:
        raise GuideImportError(f"{name} must be a non-empty list of text runs.")
    spans = []
    for index, item in enumerate(value):
        label = f"{name}[{index}]"
        if not isinstance(item, dict) or set(item) - {"text", "href", *SPAN_FLAGS}:
            raise GuideImportError(f"{label} is not a supported text run.")
        text = item.get("text")
        if not isinstance(text, str) or not text or len(text) > PARAGRAPH_MAXIMUM:
            raise GuideImportError(f"{label}.text is required.")
        span: dict[str, Any] = {"text": text}
        if "href" in item:
            span["href"] = _safe_href(item["href"], f"{label}.href")
        for flag in SPAN_FLAGS:
            if flag in item:
                if item[flag] is not True:
                    raise GuideImportError(f"{label}.{flag} must be true when present.")
                span[flag] = True
        spans.append(span)
    if not "".join(span["text"] for span in spans).strip():
        raise GuideImportError(f"{name} has no visible text.")
    return spans


def _table(value: dict[str, Any], name: str) -> dict[str, Any]:
    header, rows = value.get("header"), value.get("rows")
    if not isinstance(header, list) or not 2 <= len(header) <= MAX_TABLE_COLUMNS:
        raise GuideImportError(f"{name}.header must have 2 to {MAX_TABLE_COLUMNS} columns.")
    if not isinstance(rows, list) or not rows:
        raise GuideImportError(f"{name}.rows must contain at least one row.")
    cleaned = []
    for row_index, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != len(header):
            raise GuideImportError(f"{name}.rows[{row_index}] must match the header's column count.")
        # An empty cell is legitimate table content; every other run is checked.
        cleaned.append([
            _spans(cell, f"{name}.rows[{row_index}][{cell_index}]") if cell else []
            for cell_index, cell in enumerate(row)
        ])
    return {
        "type": "table",
        "header": [_spans(cell, f"{name}.header[{index}]") for index, cell in enumerate(header)],
        "rows": cleaned,
    }


def _blocks(value: Any, name: str) -> list[dict[str, Any]]:
    """Validate an ordered body: paragraphs, lists and tables, in source order."""
    if not isinstance(value, list) or not value:
        raise GuideImportError(f"{name} must be a non-empty list.")
    blocks: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        label = f"{name}[{index}]"
        kind = item.get("type") if isinstance(item, dict) else None
        if kind == "paragraph":
            blocks.append({"type": "paragraph", "spans": _spans(item.get("spans"), f"{label}.spans")})
        elif kind == "list":
            items = item.get("items")
            if not isinstance(items, list) or not items:
                raise GuideImportError(f"{label}.items must be a non-empty list.")
            block: dict[str, Any] = {
                "type": "list",
                "items": [_spans(entry, f"{label}.items[{i}]") for i, entry in enumerate(items)],
            }
            if item.get("ordered") is True:
                block["ordered"] = True
                start = item.get("start", 1)
                if not isinstance(start, int) or isinstance(start, bool) or not 0 <= start <= 9999:
                    raise GuideImportError(f"{label}.start must be a list number.")
                block["start"] = start
            blocks.append(block)
        elif kind == "table":
            blocks.append(_table(item, label))
        else:
            raise GuideImportError(f"{label}.type is not a supported body block.")
    return blocks


def _iter_spans(blocks: list[dict[str, Any]]):
    for block in blocks:
        if block["type"] == "paragraph":
            yield block["spans"]
        elif block["type"] == "list":
            yield from block["items"]
        else:
            yield from block["header"]
            for row in block["rows"]:
                yield from row


def _resolve_internal_links(record: dict[str, Any], public_paths: set[str]) -> list[dict[str, str]]:
    """Unlink site links whose target is not a public page in this checkout.

    An approved article may be scheduled ahead of a guide it cites. Publishing
    the sentence as plain text is the safe result; a 404 in the body is not,
    and neither is failing an approved article on its slot. Each one is
    reported in the record so the gap stays visible and auditable.
    """
    unlinked: list[dict[str, str]] = []
    groups = [record.get("intro_blocks") or [],
              *(section.get("blocks") or [] for section in record["body_sections"])]
    for blocks in groups:
        for spans in _iter_spans(blocks):
            for span in spans:
                href = span.get("href")
                if not href or not href.startswith("/"):
                    continue
                path = re.split(r"[?#]", href, maxsplit=1)[0].rstrip("/") or "/"
                if path != f"/{record['slug']}" and path not in public_paths:
                    unlinked.append({"text": span["text"], "href": href})
                    del span["href"]
    return unlinked


def _sections(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise GuideImportError("body_sections must contain at least one section.")
    sections = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise GuideImportError(f"body_sections[{index}] must be an object.")
        paragraphs = item.get("paragraphs")
        bullets = item.get("bullets", [])
        if not isinstance(paragraphs, list):
            raise GuideImportError(f"body_sections[{index}].paragraphs must be a list.")
        if not isinstance(bullets, list):
            raise GuideImportError(f"body_sections[{index}].bullets must be a list.")
        # Slayer can faithfully serialize an H2/H3 section that contains only
        # a Markdown list. A non-empty list is real section content, so accept
        # it without inventing or duplicating a paragraph. Empty sections are
        # still rejected after both structured content fields are validated.
        if not paragraphs and not bullets:
            raise GuideImportError(f"body_sections[{index}] needs paragraphs or bullets.")
        section: dict[str, Any] = {
            "heading": _text(item.get("heading"), f"body_sections[{index}].heading"),
            "paragraphs": [
                _text(p, f"body_sections[{index}].paragraphs", maximum=PARAGRAPH_MAXIMUM)
                for p in paragraphs
            ],
            "bullets": [_text(b, f"body_sections[{index}].bullets") for b in bullets],
        }
        if item.get("level") == 3:
            section["level"] = 3
        if "blocks" in item:
            section["blocks"] = _blocks(item["blocks"], f"body_sections[{index}].blocks")
        sections.append(section)
    return sections


def _intro_paragraphs(value: Any) -> list[str]:
    """Validate optional plain-text copy that precedes the first H2."""
    if not isinstance(value, list):
        raise GuideImportError("intro_paragraphs must be a list.")
    return [
        _text(paragraph, f"intro_paragraphs[{index}]", maximum=PARAGRAPH_MAXIMUM)
        for index, paragraph in enumerate(value)
    ]


def validate_package(package_dir: str | Path) -> tuple[dict[str, Any], list[tuple[Path, str]]]:
    """Validate every input before writing any target file."""
    base = Path(package_dir).resolve()
    manifest_path = base / MANIFEST_NAME
    if not base.is_dir() or not manifest_path.is_file():
        raise GuideImportError(f"Package must contain {MANIFEST_NAME}.")
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GuideImportError(f"{MANIFEST_NAME} must be valid UTF-8 JSON.") from exc
    if not isinstance(raw, dict):
        raise GuideImportError(f"{MANIFEST_NAME} must contain an object.")

    slug = _safe_slug(raw.get("slug"))
    intro_paragraphs = _intro_paragraphs(raw.get("intro_paragraphs", []))
    sections = _sections(raw.get("body_sections"))
    intro_blocks = _blocks(raw["intro_blocks"], "intro_blocks") if raw.get("intro_blocks") else []
    hero_source, hero_alt = _image(raw.get("hero_image"), base, "hero_image")
    inline_raw = raw.get("inline_images", [])
    if not isinstance(inline_raw, list):
        raise GuideImportError("inline_images must be a list.")
    copied = [(hero_source, hero_alt)]
    inline = []
    for index, image in enumerate(inline_raw):
        source, alt = _image(image, base, f"inline_images[{index}]")
        placement = image.get("placement_after_section")
        if not isinstance(placement, int) or not 0 <= placement < len(sections):
            raise GuideImportError(
                f"inline_images[{index}].placement_after_section must identify a body section."
            )
        copied.append((source, alt))
        inline.append({"source": source, "alt": alt, "placement_after_section": placement})

    card = raw.get("resource_card")
    if not isinstance(card, dict):
        raise GuideImportError("resource_card is required.")
    topic = _text(card.get("topic"), "resource_card.topic", maximum=80)
    if topic not in RESOURCE_TOPICS:
        raise GuideImportError("resource_card.topic is not a supported Resources category.")
    record = {
        "version": 1,
        "slug": slug,
        "title": _text(raw.get("title"), "title"),
        "dek": _text(raw.get("dek"), "dek"),
        "intro_paragraphs": intro_paragraphs,
        "body_sections": sections,
        "seo_title": _text(raw.get("seo_title"), "seo_title"),
        "meta_description": _text(raw.get("meta_description"), "meta_description"),
        "hero_image": {"source": hero_source, "alt": hero_alt},
        "inline_images": inline,
        "resource_card": {
            "topic": topic,
            "summary": _text(card.get("summary"), "resource_card.summary", maximum=350),
        },
    }
    if intro_blocks:
        record["intro_blocks"] = intro_blocks
    if intro_blocks or any("blocks" in section for section in sections):
        record["body_format"] = BODY_FORMAT
        from app import seo_meta
        record["unlinked_links"] = _resolve_internal_links(record, set(seo_meta.public_indexable_paths()))
    return record, copied


def import_guide_package(
    package_dir: str | Path,
    *,
    content_dir: Path = CONTENT_DIR,
    asset_root: Path = ASSET_ROOT,
    local_base_url: str = LOCAL_BASE_URL,
) -> dict[str, str]:
    """Import a validated package locally, with no deployment side effects."""
    record, _copied = validate_package(package_dir)
    # This is the local record's modification/import timestamp, not a claim
    # that the guide has been published publicly.
    record["modified_at"] = datetime.now(timezone.utc).isoformat()
    slug = record["slug"]
    content_dir = Path(content_dir)
    asset_root = Path(asset_root)
    record_path = content_dir / f"{slug}.json"
    asset_dir = asset_root / slug
    if record_path.exists() or asset_dir.exists():
        raise GuideImportError(f"slug '{slug}' already exists; nothing was written.")

    # Stage both artifacts on the same filesystem. The destination names are
    # checked before any writes; cleanup keeps a later I/O error from leaving
    # a half-imported guide behind.
    content_dir.mkdir(parents=True, exist_ok=True)
    asset_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="guide-import-", dir=content_dir.parent) as staging_name:
        staging = Path(staging_name)
        staged_assets = staging / "assets"
        staged_assets.mkdir()
        hero = record["hero_image"]
        hero_name = f"hero{hero['source'].suffix.lower()}"
        shutil.copyfile(hero["source"], staged_assets / hero_name)
        hero["url"] = f"/static/images/resources/imported/{slug}/{hero_name}"
        hero.pop("source")
        for index, inline in enumerate(record["inline_images"], start=1):
            name = f"inline-{index}{inline['source'].suffix.lower()}"
            shutil.copyfile(inline["source"], staged_assets / name)
            inline["url"] = f"/static/images/resources/imported/{slug}/{name}"
            inline.pop("source")
        staged_record = staging / f"{slug}.json"
        staged_record.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        if record_path.exists() or asset_dir.exists():
            raise GuideImportError(f"slug '{slug}' already exists; nothing was written.")
        try:
            os.replace(staged_assets, asset_dir)
            os.replace(staged_record, record_path)
        except OSError:
            if asset_dir.exists() and not record_path.exists():
                shutil.rmtree(asset_dir)
            raise
    result = {"slug": slug, "local_url": f"{local_base_url.rstrip('/')}/{slug}", "record": str(record_path)}
    if record.get("unlinked_links"):
        result["unlinked_links"] = record["unlinked_links"]
    return result


def load_imported_guides(content_dir: Path = CONTENT_DIR) -> dict[str, dict[str, Any]]:
    """Read only well-formed records written by this importer."""
    guides: dict[str, dict[str, Any]] = {}
    if not content_dir.is_dir():
        return guides
    for path in sorted(content_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            slug = _safe_slug(data.get("slug"))
            if data.get("version") != 1 or path.name != f"{slug}.json":
                continue
            _text(data.get("title"), "title")
            _text(data.get("dek"), "dek")
            _text(data.get("seo_title"), "seo_title")
            _text(data.get("meta_description"), "meta_description")
            _intro_paragraphs(data.get("intro_paragraphs", []))
            _sections(data.get("body_sections"))
            if data.get("intro_blocks"):
                _blocks(data["intro_blocks"], "intro_blocks")
            card = data.get("resource_card")
            if not isinstance(card, dict):
                continue
            if _text(card.get("topic"), "resource_card.topic", maximum=80) not in RESOURCE_TOPICS:
                continue
            _text(card.get("summary"), "resource_card.summary", maximum=350)
            if not isinstance(data.get("inline_images", []), list):
                continue
            images = [data.get("hero_image"), *data["inline_images"]]
            prefix = f"/static/images/resources/imported/{slug}/"
            for image in images:
                if not isinstance(image, dict) or not str(image.get("url", "")).startswith(prefix):
                    raise GuideImportError("Imported image URL is invalid.")
                _text(image.get("alt"), "image.alt", maximum=300)
            for image in data["inline_images"]:
                placement = image.get("placement_after_section")
                if not isinstance(placement, int) or not 0 <= placement < len(data["body_sections"]):
                    raise GuideImportError("Imported inline image placement is invalid.")
            # Older local imports predate the explicit field. Their on-disk
            # record mtime is the truthful modification timestamp and avoids
            # a migration that might imply public publication.
            modified_at = data.get("modified_at")
            if not isinstance(modified_at, str) or not modified_at.strip():
                modified_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
            data["modified_at"] = modified_at
            guides[slug] = data
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return guides


def public_paths() -> tuple[str, ...]:
    return tuple(f"/{slug}" for slug in load_imported_guides())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import one validated Slayer guide package locally.")
    parser.add_argument("--package-dir", required=True, help=f"directory containing {MANIFEST_NAME}")
    args = parser.parse_args(argv)
    try:
        result = import_guide_package(args.package_dir)
    except GuideImportError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
