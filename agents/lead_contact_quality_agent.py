"""
LeadBot contact-quality assessment (single source of truth).

Why this exists
---------------
Before this module, a lead could be published as
`outreach_status: email_and_call_ready` with `contact_confidence: 80` purely
because *some* string containing an "@" was scraped off the page, even when
that string was a placeholder (`example@gmail.com`) or belonged to a domain
with no relationship to the business being scanned. Two independent code
paths produced that 80:

  * agents/contact_extraction_agent.extract_contact_from_url()
      45 (any phone) + 35 (any email) = 80
  * agents/lead_confidence_agent.calculate_contact_confidence()
      `elif phone_ok or email_ok: score = max(score, 80)`

Neither one compared the email's domain against the business's own domain,
and neither one recognised placeholder addresses whose *local part* is the
giveaway (`example@gmail.com` passes a naive "is example.com blocked?" test).

Design rules
------------
1. Raw discovered contact data is NEVER deleted here. This module only
   *interprets* it. Callers keep their original `emails` value for export;
   what changes is the confidence/status derived from it, plus an explicit
   flag saying why.
2. Every downgrade emits a named flag so the reasoning is auditable in the
   existing `contact_flags` export column.
3. Generic business inboxes (info@/contact@/office@/...) stay fully trusted
   as long as the domain matches the business.
4. Phone-only leads keep their existing usefulness -- a missing/weak email
   never reduces the phone signal, it only stops the *email* claim.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Sequence
from urllib.parse import urlparse


# --- Flags -----------------------------------------------------------------
# Stable, greppable names. These land in the existing `contact_flags` field.

FLAG_PLACEHOLDER_EMAIL = "email_placeholder_rejected"
FLAG_MALFORMED_EMAIL = "email_malformed_rejected"
FLAG_DOMAIN_MISMATCH = "email_domain_mismatch"
FLAG_FREE_MAIL_UNVERIFIED = "email_free_mail_unverified"
FLAG_DOMAIN_MATCH = "email_domain_verified"
FLAG_NO_BUSINESS_DOMAIN = "email_business_domain_unknown"
FLAG_DOWNGRADED = "outreach_downgraded_low_email_confidence"
FLAG_NO_EMAIL = "no_email_found"
FLAG_NO_PHONE = "no_phone_found"


# --- Vocabularies ----------------------------------------------------------

# Domains that are documentation/test placeholders by definition
# (RFC 2606 reserves example.com/.net/.org and .test/.invalid/.example).
PLACEHOLDER_EMAIL_DOMAINS = frozenset({
    "example.com",
    "example.org",
    "example.net",
    "example.edu",
    "test.com",
    "test.org",
    "testing.com",
    "domain.com",
    "yourdomain.com",
    "your-domain.com",
    "mydomain.com",
    "email.com",
    "yoursite.com",
    "website.com",
    "sample.com",
    "acme.com",
    "company.com",
    "localhost.com",
    "localhost.localdomain",
})

# Reserved / non-routable TLDs -- never a real business inbox.
PLACEHOLDER_EMAIL_TLDS = frozenset({
    "test",
    "invalid",
    "example",
    "localhost",
    "local",
})

# Local parts that mark the address as a template/demo value regardless of
# which domain follows the "@". This is what catches `example@gmail.com`.
PLACEHOLDER_LOCAL_PARTS = frozenset({
    "example",
    "example1",
    "examples",
    "sample",
    "test",
    "test1",
    "testing",
    "testuser",
    "placeholder",
    "youremail",
    "your-email",
    "your_email",
    "yourname",
    "your-name",
    "yourmail",
    "emailaddress",
    "email",
    "e-mail",
    "mail",
    "someone",
    "somebody",
    "anybody",
    "username",
    "user",
    "firstname",
    "lastname",
    "firstnamelastname",
    "name",
    "johndoe",
    "john.doe",
    "janedoe",
    "jane.doe",
    "foo",
    "bar",
    "foobar",
    "asdf",
    "abc",
    "xxx",
    "none",
    "null",
    "undefined",
    "nobody",
    "noreply",
    "no-reply",
    "donotreply",
    "do-not-reply",
})

# Consumer mailbox providers. A business CAN legitimately use one of these,
# so they are not rejected -- but on their own they are not evidence that the
# address belongs to *this* business, so they only count as weak evidence.
FREE_MAIL_DOMAINS = frozenset({
    "gmail.com",
    "googlemail.com",
    "yahoo.com",
    "yahoo.co.uk",
    "ymail.com",
    "hotmail.com",
    "hotmail.co.uk",
    "outlook.com",
    "live.com",
    "msn.com",
    "aol.com",
    "icloud.com",
    "me.com",
    "mac.com",
    "protonmail.com",
    "proton.me",
    "gmx.com",
    "gmx.net",
    "mail.com",
    "mail.ru",
    "yandex.com",
    "yandex.ru",
    "zoho.com",
    "comcast.net",
    "verizon.net",
    "att.net",
    "sbcglobal.net",
    "bellsouth.net",
    "optonline.net",
    "cox.net",
    "charter.net",
    "earthlink.net",
    "rocketmail.com",
    "inbox.com",
    "fastmail.com",
})

# Generic-but-legitimate business inboxes. Explicitly preserved (rule 3).
GENERIC_BUSINESS_LOCALS = frozenset({
    "info",
    "contact",
    "contactus",
    "office",
    "sales",
    "hello",
    "hi",
    "admin",
    "support",
    "service",
    "services",
    "team",
    "help",
    "enquiries",
    "enquiry",
    "inquiries",
    "inquiry",
    "booking",
    "bookings",
    "reservations",
    "orders",
    "frontdesk",
    "reception",
    "mail",
    "email",
})

# Multi-part public suffixes we care about, so "co.uk" is never mistaken for
# the registrable domain. Deliberately short -- this is a heuristic, not a
# full PSL, and a miss only costs us a slightly stricter match.
#
# NOTE: no Public Suffix List library (tldextract / publicsuffix2) is available
# in this project's environment, and adding one is an environment change that
# needs approval, so the handling below is deliberately conservative instead:
# when a suffix looks like one *unrelated third parties* can sit under, the
# label in front of it must match exactly before we will call it a domain
# match. Being too strict only costs a weaker evidence class; being too loose
# produced the actual defect (evil.co.ke "matching" good.co.ke).
_MULTI_PART_SUFFIXES = frozenset({
    "co.uk", "org.uk", "me.uk", "ac.uk", "gov.uk", "net.uk", "sch.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au",
    "co.nz", "net.nz", "org.nz",
    "co.za", "org.za",
    "com.br", "com.mx", "com.ar", "com.sg", "com.my", "com.hk", "com.tr",
    "co.jp", "or.jp", "ne.jp",
    "co.in", "net.in", "org.in",
})

# Hosting/SaaS suffixes where each customer gets a subdomain. Two sites sharing
# one of these share nothing at all: evil.github.io and good.github.io are
# unrelated parties, so collapsing both to "github.io" invented a domain match.
_WILDCARD_STYLE_SUFFIXES = frozenset({
    "github.io", "gitlab.io", "bitbucket.io", "readthedocs.io",
    "pages.dev", "workers.dev", "web.app", "firebaseapp.com", "appspot.com",
    "netlify.app", "netlify.com", "vercel.app", "onrender.com", "fly.dev",
    "railway.app", "herokuapp.com", "azurewebsites.net", "cloudfront.net",
    "s3.amazonaws.com", "elasticbeanstalk.com",
    "blogspot.com", "wordpress.com", "tumblr.com", "substack.com",
    "wixsite.com", "weebly.com", "squarespace.com", "myshopify.com",
    "godaddysites.com", "business.site", "square.site", "webflow.io",
    "strikingly.com", "mystrikingly.com", "jimdosite.com", "site123.me",
    "yolasite.com", "000webhostapp.com", "carrd.co", "glitch.me", "repl.co",
    "surge.sh", "neocities.org", "notion.site", "sites.google.com",
})

# Second-level labels that registries hand out under a two-letter ccTLD
# ("co.ke", "com.gh", "org.ng", ...). Anyone can register under these, so they
# are a public suffix, not a shared owner.
_CC_SECOND_LEVEL_LABELS = frozenset({
    "co", "com", "net", "org", "edu", "ac", "gov", "gob", "govt", "mil",
    "sch", "or", "ne", "ltd", "plc", "in", "id", "gen", "nom", "res", "firm",
    "asn", "k12", "lg", "go", "re", "priv", "sc",
})


def _is_public_suffix(candidate: str) -> bool:
    """True when `candidate` is a suffix unrelated parties can register under."""
    if not candidate:
        return False
    if candidate in _MULTI_PART_SUFFIXES or candidate in _WILDCARD_STYLE_SUFFIXES:
        return True

    parts = candidate.split(".")
    if (
        len(parts) == 2
        and len(parts[1]) == 2
        and parts[1].isalpha()
        and parts[0] in _CC_SECOND_LEVEL_LABELS
    ):
        return True

    return False

# Deliberately permissive-but-structural. Rejects the malformed shapes the
# scrapers actually produce (missing TLD, double @, trailing dot, spaces).
_EMAIL_SHAPE_RE = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.\-]+@[A-Za-z0-9]([A-Za-z0-9\-]*[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9\-]*[A-Za-z0-9])?)+$"
)

# Email classification results.
CLASS_MALFORMED = "malformed"
CLASS_PLACEHOLDER = "placeholder"
CLASS_DOMAIN_MATCH = "domain_match"
CLASS_FREE_MAIL = "free_mail"
CLASS_DOMAIN_MISMATCH = "domain_mismatch"

# Evidence levels.
EVIDENCE_NONE = "none"
EVIDENCE_WEAK = "weak"
EVIDENCE_STRONG = "strong"

STATUS_EMAIL_AND_CALL_READY = "email_and_call_ready"
STATUS_CALL_READY = "call_ready"
STATUS_EMAIL_READY = "email_ready"
STATUS_NEEDS_MANUAL = "needs_manual_research"

_BAD_TEXT_VALUES = frozenset({
    "", "not found", "none", "null", "nan", "n/a", "unknown", "manual", "?", "-",
})


# --- Normalisation helpers -------------------------------------------------

def _text(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    if text.lower() in _BAD_TEXT_VALUES:
        return ""
    return text


def normalize_host(value: Any) -> str:
    """Return a bare lowercase hostname from a URL, domain, or email domain."""
    text = _text(value).lower()
    if not text:
        return ""

    if "://" in text:
        host = urlparse(text).netloc
    elif "/" in text or "?" in text:
        host = urlparse("https://" + text).netloc
    else:
        host = text

    host = host.split("@")[-1]          # strip any user:pass@
    host = host.split(":")[0]            # strip port
    host = host.strip().strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def registrable_domain(host: Any) -> str:
    """Best-effort eTLD+1 ("shop.acme.co.uk" -> "acme.co.uk")."""
    host = normalize_host(host)
    if not host:
        return ""

    parts = [p for p in host.split(".") if p]
    if len(parts) <= 2:
        return ".".join(parts)

    last_two = ".".join(parts[-2:])
    if _is_public_suffix(last_two) and len(parts) >= 3:
        return ".".join(parts[-3:])
    return last_two


def business_domain_for(website: Any = "", domain: Any = "") -> str:
    """The business's own domain, preferring an explicit `domain` field."""
    return normalize_host(domain) or normalize_host(website)


def split_emails(value: Any) -> List[str]:
    """Split a raw emails value into candidate addresses.

    Accepts a string, a list/tuple/set, or a comma/semicolon/whitespace
    separated blob. Order and duplicates-after-lowercasing are preserved as
    first-seen. Nothing is filtered here -- filtering is classification's job.
    """
    if value is None:
        return []

    if isinstance(value, (list, tuple, set)):
        pieces: List[str] = []
        for item in value:
            pieces.extend(split_emails(item))
        seen: set = set()
        ordered: List[str] = []
        for piece in pieces:
            if piece not in seen:
                seen.add(piece)
                ordered.append(piece)
        return ordered

    raw = str(value or "")
    candidates = [p.strip().strip("<>()[]{}\"'").strip(".,;:") for p in re.split(r"[,;\s|]+", raw)]

    out: List[str] = []
    seen = set()
    for candidate in candidates:
        candidate = candidate.lower()
        if not candidate or candidate in _BAD_TEXT_VALUES:
            continue
        if candidate.startswith("mailto:"):
            candidate = candidate[len("mailto:"):]
        if "@" not in candidate:
            continue
        if candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


def has_real_phone(value: Any) -> bool:
    digits = re.sub(r"\D+", "", _text(value))
    return len(digits) >= 7


# --- Classification --------------------------------------------------------

def classify_email(email: Any, business_domain: Any = "") -> str:
    """Classify one address relative to the business's own domain.

    Returns one of CLASS_MALFORMED / CLASS_PLACEHOLDER / CLASS_DOMAIN_MATCH /
    CLASS_FREE_MAIL / CLASS_DOMAIN_MISMATCH.
    """
    address = _text(email).lower()
    if address.startswith("mailto:"):
        address = address[len("mailto:"):]

    if not address or address.count("@") != 1:
        return CLASS_MALFORMED

    if not _EMAIL_SHAPE_RE.match(address):
        return CLASS_MALFORMED

    local, _, domain = address.partition("@")
    local = local.strip()
    domain = domain.strip().strip(".")

    if not local or not domain or "." not in domain:
        return CLASS_MALFORMED

    if local.startswith(".") or local.endswith(".") or ".." in local:
        return CLASS_MALFORMED

    tld = domain.rsplit(".", 1)[-1]
    if not re.fullmatch(r"[a-z]{2,24}", tld):
        return CLASS_MALFORMED

    if tld in PLACEHOLDER_EMAIL_TLDS:
        return CLASS_PLACEHOLDER

    if domain in PLACEHOLDER_EMAIL_DOMAINS or registrable_domain(domain) in PLACEHOLDER_EMAIL_DOMAINS:
        return CLASS_PLACEHOLDER

    # `example@gmail.com` -- the local part is the placeholder, not the domain.
    local_key = local.replace("_", "").replace("-", "")
    if local in PLACEHOLDER_LOCAL_PARTS or local_key in PLACEHOLDER_LOCAL_PARTS:
        # A generic business inbox on the business's own domain is real, not a
        # placeholder ("mail@thebusiness.com"), so only treat overlapping names
        # as placeholders when the domain does not belong to the business.
        biz = registrable_domain(business_domain)
        if not (biz and registrable_domain(domain) == biz and local in GENERIC_BUSINESS_LOCALS):
            return CLASS_PLACEHOLDER

    biz = registrable_domain(business_domain)
    email_registrable = registrable_domain(domain)

    if biz and email_registrable == biz:
        return CLASS_DOMAIN_MATCH

    if domain in FREE_MAIL_DOMAINS or email_registrable in FREE_MAIL_DOMAINS:
        return CLASS_FREE_MAIL

    if not biz:
        # We do not know the business domain, so we cannot claim a match.
        # Treat it as unverified rather than as a mismatch.
        return CLASS_FREE_MAIL if email_registrable in FREE_MAIL_DOMAINS else CLASS_DOMAIN_MISMATCH

    return CLASS_DOMAIN_MISMATCH


def assess_contact_quality(
    emails: Any = "",
    phone: Any = "",
    website: Any = "",
    domain: Any = "",
    contact_page_url: Any = "",
) -> Dict[str, Any]:
    """Interpret raw contact data. Never mutates or drops the raw input.

    Returns a dict with:
      emails_all        every candidate address, first-seen order (unchanged)
      trusted_emails    addresses that verify against the business domain
      weak_emails       real-looking but unverified (free-mail / mismatch)
      rejected_emails   placeholder / malformed
      classifications   {address: class}
      email_evidence    EVIDENCE_NONE | EVIDENCE_WEAK | EVIDENCE_STRONG
      phone_ok          bool
      outreach_status   the status this lead is actually entitled to
      contact_confidence  0-93 score consistent with the evidence
      flags             ordered, de-duplicated audit flags
    """
    business_domain = business_domain_for(website=website, domain=domain)

    all_emails = split_emails(emails)
    classifications: Dict[str, str] = {}
    trusted: List[str] = []
    weak: List[str] = []
    rejected: List[str] = []

    for address in all_emails:
        kind = classify_email(address, business_domain)
        classifications[address] = kind
        if kind == CLASS_DOMAIN_MATCH:
            trusted.append(address)
        elif kind in (CLASS_FREE_MAIL, CLASS_DOMAIN_MISMATCH):
            weak.append(address)
        else:
            rejected.append(address)

    phone_ok = has_real_phone(phone)
    contact_page_ok = bool(_text(contact_page_url))

    if trusted:
        evidence = EVIDENCE_STRONG
    elif weak:
        evidence = EVIDENCE_WEAK
    else:
        evidence = EVIDENCE_NONE

    flags: List[str] = []

    if any(k == CLASS_PLACEHOLDER for k in classifications.values()):
        flags.append(FLAG_PLACEHOLDER_EMAIL)
    if any(k == CLASS_MALFORMED for k in classifications.values()):
        flags.append(FLAG_MALFORMED_EMAIL)
    if any(k == CLASS_DOMAIN_MISMATCH for k in classifications.values()):
        flags.append(FLAG_DOMAIN_MISMATCH)
    if any(k == CLASS_FREE_MAIL for k in classifications.values()):
        flags.append(FLAG_FREE_MAIL_UNVERIFIED)
    if trusted:
        flags.append(FLAG_DOMAIN_MATCH)
    if all_emails and not business_domain:
        flags.append(FLAG_NO_BUSINESS_DOMAIN)
    if not all_emails:
        flags.append(FLAG_NO_EMAIL)
    if not phone_ok:
        flags.append(FLAG_NO_PHONE)

    # --- Outreach status ---------------------------------------------------
    # The core rule: `email_and_call_ready` requires a phone AND at least one
    # email that actually verifies against the business's own domain.
    if phone_ok and evidence == EVIDENCE_STRONG:
        status = STATUS_EMAIL_AND_CALL_READY
    elif phone_ok:
        status = STATUS_CALL_READY
        if evidence == EVIDENCE_WEAK:
            flags.append(FLAG_DOWNGRADED)
    elif evidence == EVIDENCE_STRONG:
        status = STATUS_EMAIL_READY
    elif evidence == EVIDENCE_WEAK:
        status = STATUS_EMAIL_READY
        flags.append(FLAG_DOWNGRADED)
    else:
        status = STATUS_NEEDS_MANUAL

    # --- Confidence --------------------------------------------------------
    score = 0
    if business_domain:
        score += 5
    if phone_ok:
        score += 45
    if evidence == EVIDENCE_STRONG:
        score += 35
    elif evidence == EVIDENCE_WEAK:
        score += 10
    if contact_page_ok:
        score += 10

    # Ceilings that make the number consistent with the claim.
    if evidence == EVIDENCE_WEAK:
        # Never let unverified email evidence reach the "ready" band (>=80).
        score = min(score, 70 if phone_ok else 45)
    elif evidence == EVIDENCE_NONE:
        score = min(score, 75 if phone_ok else 40)

    if not phone_ok and evidence == EVIDENCE_NONE and not contact_page_ok:
        score = min(score, 20)

    score = max(0, min(int(score), 93))

    ordered_flags: List[str] = []
    for flag in flags:
        if flag not in ordered_flags:
            ordered_flags.append(flag)

    return {
        "emails_all": all_emails,
        "trusted_emails": trusted,
        "weak_emails": weak,
        "rejected_emails": rejected,
        "classifications": classifications,
        "business_domain": business_domain,
        "email_evidence": evidence,
        "phone_ok": phone_ok,
        "contact_page_ok": contact_page_ok,
        "outreach_status": status,
        "contact_confidence": score,
        "flags": ordered_flags,
    }


def resolve_outreach_status(
    emails: Any = "",
    phone: Any = "",
    website: Any = "",
    domain: Any = "",
    contact_page_url: Any = "",
):
    """Convenience wrapper: -> (outreach_status, flags, contact_confidence)."""
    assessment = assess_contact_quality(
        emails=emails,
        phone=phone,
        website=website,
        domain=domain,
        contact_page_url=contact_page_url,
    )
    return (
        assessment["outreach_status"],
        assessment["flags"],
        assessment["contact_confidence"],
    )


def merge_flags(existing: Any, new_flags: Iterable[str]) -> List[str]:
    """Merge flags from mixed shapes (list / comma string / None) safely."""
    out: List[str] = []

    def _add(value):
        value = str(value or "").strip()
        if value and value not in out:
            out.append(value)

    if isinstance(existing, (list, tuple, set)):
        for item in existing:
            _add(item)
    elif existing:
        for item in str(existing).split(","):
            _add(item)

    for item in new_flags or []:
        _add(item)

    return out


def assess_lead_row(row: Any) -> Dict[str, Any]:
    """Assess a lead dict/row using this project's usual field aliases."""
    def pick(*keys: str) -> str:
        for key in keys:
            try:
                value = row.get(key)
            except Exception:
                value = ""
            value = _text(value)
            if value:
                return value
        return ""

    def pick_raw(*keys: str):
        for key in keys:
            try:
                value = row.get(key)
            except Exception:
                value = None
            if value:
                return value
        return ""

    website = pick("website", "url", "link", "homepage")
    domain = pick("domain") or normalize_host(website)

    return assess_contact_quality(
        emails=pick_raw("emails", "email", "email_1", "best_email", "contact_email"),
        phone=pick("best_phone", "phone", "phone_number", "telephone", "tel"),
        website=website,
        domain=domain,
        contact_page_url=pick("contact_page_url", "contact_page"),
    )
