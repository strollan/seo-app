"""Owner-bound outbound import queue. No database initialization or migrations."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from agents.actii_file_store import atomic_json, file_lock, private_directory, read_json

MAX_PAYLOAD = 64 * 1024
MAX_JOBS = 1000
MAX_ATTEMPTS = 10
LEASE_SECONDS = 120
JOB_SECONDS = 7 * 86400
TERMINAL = {"added", "already_present", "needs_review", "failed"}
ID = re.compile(r"[a-f0-9]{32}")
SAFE_NAME = re.compile(r"[A-Za-z0-9_-]{1,80}")
MESSAGES = {
    "queued": "Queued for ACT II.", "processing": "Processing in ACT II.",
    "added": "Added to CRM.", "already_present": "Already present in CRM.",
    "needs_review": "Needs review on the ACT II workstation. Do not resubmit.",
    "failed": "Import failed. Review the connection before trying again.",
}


class QueueError(Exception):
    def __init__(self, message="CRM queue is unavailable.", status=503):
        super().__init__(message)
        self.status = status


def enabled():
    return os.getenv("ACTII_CRM_MODE", "disabled") == "queue"


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError
    return moment.timestamp()


def payload_hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def verify_website(lead, source_domain):
    """Match ACT II's domain normalization; never permit an empty duplicate key."""
    from agents.actii_search_metadata_agent import domain
    website = lead.get("website")
    if not isinstance(website, str) or not website.strip() or any(c.isspace() for c in website):
        raise QueueError("Website must retain the original prospect's hostname.", 422)
    try:
        parsed = urlsplit(website if "://" in website else "https://" + website)
        if (parsed.scheme not in {"http", "https"} or parsed.username is not None
                or parsed.password is not None or not parsed.hostname or not parsed.port in {None, 80, 443}
                or domain(website) != source_domain or domain(source_domain) != source_domain):
            raise ValueError
    except ValueError:
        raise QueueError("Website must retain the original prospect's hostname.", 422) from None
    if not isinstance(lead.get("company_name"), str) or not lead["company_name"].strip():
        raise QueueError("Business name is required.", 422)


def current_source_authorized(user_id, username, filename, source_domain):
    """Read-only account lookup, exact export ownership, and current source check."""
    from agents.auth_agent import AUTH_DB
    from agents.lead_dashboard_agent import _leadbot_export_exact_owner_values, _leadbot_user_keys, safe_export_file
    from agents.actii_search_metadata_agent import from_export
    connection = sqlite3.connect(Path(AUTH_DB).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        row = connection.execute("SELECT id, username, role FROM users WHERE id=? AND username=? AND is_active=1",
                                 (user_id, username)).fetchone()
    finally:
        connection.close()
    if not row:
        return False
    user = dict(zip(("id", "username", "role"), row))
    path = safe_export_file(filename)
    return bool(path and _leadbot_user_keys(user) & _leadbot_export_exact_owner_values(filename)
                and from_export(path, source_domain) is not None)


class ImportQueue:
    def __init__(self, root=None, bindings=None, authorize=None, clock=time.time):
        self.root = private_directory(Path(root or "data/actii_import_queue"))
        self.bindings_path = Path(bindings or os.getenv("ACTII_CONNECTOR_BINDINGS_FILE", ""))
        self.authorize = authorize or current_source_authorized
        self.clock = clock

    def bindings(self):
        try:
            value = read_json(self.bindings_path, limit=64 * 1024)
            if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("connectors"), list):
                raise ValueError
            result = value["connectors"]
            seen = set()
            owners = set()
            digests = set()
            for item in result:
                if (not isinstance(item, dict) or not SAFE_NAME.fullmatch(item.get("connector_id", ""))
                        or type(item.get("user_id")) is not int or item["user_id"] <= 0
                        or not isinstance(item.get("username"), str) or not item["username"]
                        or item["username"] != item["username"].strip()
                        or not SAFE_NAME.fullmatch(item.get("binding_version", ""))
                        or type(item.get("enabled")) is not bool
                        or not re.fullmatch(r"[a-f0-9]{64}", item.get("token_sha256", ""))
                        or item["connector_id"] in seen or item["user_id"] in owners
                        or item["token_sha256"] in digests):
                    raise ValueError
                timestamp(item["expires_at"])
                seen.add(item["connector_id"]); owners.add(item["user_id"])
                digests.add(item["token_sha256"])
            return result
        except (OSError, ValueError, KeyError, TypeError):
            raise QueueError("CRM connector bindings are missing or invalid.") from None

    def binding(self, user):
        for item in self.bindings():
            if (item["user_id"] == user.get("id") and item["username"] == user.get("username")
                    and item["enabled"] and timestamp(item["expires_at"]) > self.clock()):
                return item
        raise QueueError("Your account has no authorized ACT II connector.", 403)

    def authenticate(self, authorization):
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            raise QueueError("Unauthorized connector.", 401)
        token = authorization[7:]
        if not 32 <= len(token) <= 256 or not token.isascii() or any(c.isspace() for c in token):
            raise QueueError("Unauthorized connector.", 401)
        digest = hashlib.sha256(token.encode()).hexdigest()
        for item in self.bindings():
            if (hmac.compare_digest(item["token_sha256"], digest) and item["enabled"]
                    and timestamp(item["expires_at"]) > self.clock()):
                return item
        raise QueueError("Unauthorized connector.", 401)

    def _jobs(self):
        paths = sorted(self.root.glob("*.json"))
        if len(paths) > MAX_JOBS:
            raise QueueError("CRM queue capacity requires operator review.")
        for path in paths:
            job = read_json(path)
            if (not ID.fullmatch(path.stem) or not isinstance(job, dict) or job.get("id") != path.stem
                    or job.get("state") not in TERMINAL | {"queued", "processing"}
                    or (job.get("payload") is None and not (job["state"] in TERMINAL and job.get("payload_retired")))
                    or (job.get("payload") is not None and job.get("payload_hash") != payload_hash(job["payload"]))):
                raise QueueError("CRM queue integrity requires operator review.")
            yield job

    def _save(self, job, event, **details):
        job["updated_at"] = self.clock()
        job["events"].append({"at": job["updated_at"], "event": event,
                              "attempt": job["attempts"], **details})
        atomic_json(self.root / (job["id"] + ".json"), job)

    def _reconcile(self, job, binding=None):
        if job["state"] in TERMINAL:
            if not job.get("payload_retired") and self.clock() - job["updated_at"] >= JOB_SECONDS:
                job.update(payload=None, payload_retired=True)
                self._save(job, "payload_retired")
            return
        reason = None
        if binding is not None and (not binding.get("enabled") or timestamp(binding["expires_at"]) <= self.clock()
                                    or any(job[key] != binding.get(key) for key in ("user_id", "username", "binding_version", "connector_id"))):
            reason = "binding_changed"
        elif self.clock() >= job["expires_at"]:
            reason = "expired"
        elif not self.authorize(job["user_id"], job["username"], job["source_file"], job["source_domain"]):
            reason = "authorization_revoked"
        if reason:
            # An outstanding dispatch may already have committed. Never make it retryable.
            job["state"] = "needs_review" if job["state"] == "processing" else "failed"
            job["reason"] = reason
            self._save(job, reason)
        elif job["state"] == "processing" and self.clock() >= job["lease_until"]:
            job["state"] = "needs_review"
            job["reason"] = "lease_expired_uncertain"
            self._save(job, job["reason"])

    @staticmethod
    def public(job):
        result = {key: job[key] for key in ("id", "state", "source_file", "source_domain", "updated_at")}
        result.update(ok=True, message=MESSAGES[job["state"]])
        if job.get("contact_id"):
            result["contact_id"] = job["contact_id"]
            result["open_url"] = f"http://127.0.0.1:8842/crm/contacts/{job['contact_id']}"
        return result

    def enqueue(self, user, filename, source_domain, lead):
        binding = self.authorize_owner(user, filename, source_domain)
        verify_website(lead, source_domain)
        source_ref = f"leadmeleads:{filename}:{source_domain}"
        payload = {"owner_id": user["username"], "source_ref": source_ref, "lead": lead}
        if len(json.dumps(payload, ensure_ascii=True).encode()) > MAX_PAYLOAD:
            raise QueueError("Prospect details exceed the CRM import limit.", 422)
        with file_lock(self.root):
            jobs = list(self._jobs())
            for job in jobs:
                if job["user_id"] == user["id"] and job["source_ref"] == source_ref:
                    self._reconcile(job, binding)
                    return self.public(job)
            if len(jobs) >= MAX_JOBS or sum(j["user_id"] == user["id"] and j["created_at"] > self.clock() - 60 for j in jobs) >= 20:
                raise QueueError("CRM queue limit reached. Try later.", 429)
            job = {"id": uuid.uuid4().hex, "state": "queued", "source_file": filename,
                   "source_domain": source_domain, "source_ref": source_ref,
                   **{key: binding[key] for key in ("connector_id", "binding_version", "user_id", "username")},
                   "payload": payload, "payload_hash": payload_hash(payload), "attempts": 0,
                   "created_at": self.clock(), "expires_at": self.clock() + JOB_SECONDS,
                   "next_attempt_at": self.clock(), "events": []}
            self._save(job, "enqueued")
            return self.public(job)

    def authorize_owner(self, user, filename, source_domain):
        binding = self.binding(user)
        if not self.authorize(user["id"], user["username"], filename, source_domain):
            raise QueueError("This prospect is not explicitly owned by your account.", 403)
        return binding

    def status(self, user, job_id=None):
        if job_id is not None and not ID.fullmatch(job_id):
            raise QueueError("CRM job not found.", 404)
        with file_lock(self.root):
            result = []
            bindings = self.bindings()
            for job in self._jobs():
                if job["user_id"] == user.get("id") and job["username"] == user.get("username"):
                    binding = next((item for item in bindings if item["connector_id"] == job["connector_id"]), {})
                    self._reconcile(job, binding)
                    if job_id is None or job_id == job["id"]:
                        result.append(self.public(job))
            if job_id is not None:
                if not result:
                    raise QueueError("CRM job not found.", 404)
                return result[0]
            return {"ok": True, "jobs": result}

    def claim(self, authorization):
        binding = self.authenticate(authorization)
        with file_lock(self.root):
            jobs = [job for job in self._jobs() if job["connector_id"] == binding["connector_id"]]
            for job in jobs:
                self._reconcile(job, binding)
            if any(j["state"] == "processing" for j in jobs):
                return {"ok": True, "job": None}
            for job in sorted(jobs, key=lambda j: j["created_at"]):
                if job["state"] != "queued" or job["next_attempt_at"] > self.clock():
                    continue
                job.pop("receipt", None)
                job.update(state="processing", lease_id=uuid.uuid4().hex, lease_until=self.clock() + LEASE_SECONDS,
                           attempts=job["attempts"] + 1)
                self._save(job, "claimed")
                return {"ok": True, "job": {key: job[key] for key in (
                    "id", "connector_id", "user_id", "username", "binding_version", "payload", "payload_hash",
                    "source_domain", "lease_id", "lease_until", "expires_at")}}
            return {"ok": True, "job": None}

    def acknowledge(self, authorization, body):
        binding = self.authenticate(authorization)
        if (not isinstance(body, dict) or not isinstance(body.get("id"), str) or not ID.fullmatch(body["id"])
                or not isinstance(body.get("lease_id"), str) or not ID.fullmatch(body["lease_id"])
                or not isinstance(body.get("outcome"), str)
                or body.get("outcome") not in {"created", "existing", "review", "failed", "not_sent", "uncertain"}):
            raise QueueError("Invalid CRM receipt.", 422)
        outcome = body["outcome"]
        contact_id = body.get("contact_id")
        if outcome in {"created", "existing"} and (type(contact_id) is not int or contact_id <= 0):
            raise QueueError("Invalid CRM receipt.", 422)
        receipt = {key: body[key] for key in ("id", "lease_id", "outcome")}
        if outcome in {"created", "existing"}:
            receipt["contact_id"] = contact_id
        with file_lock(self.root):
            job = next((j for j in self._jobs() if j["id"] == body["id"] and j["connector_id"] == binding["connector_id"]), None)
            if not job:
                raise QueueError("CRM job not found.", 404)
            if any(job[key] != binding[key] for key in ("user_id", "username", "binding_version")):
                raise QueueError("CRM binding changed; review required.", 409)
            if job.get("receipt") == receipt:
                return {"ok": True}
            if job.get("lease_id") != receipt["lease_id"] or job["state"] not in {"processing", "needs_review"} or job.get("receipt"):
                raise QueueError("CRM receipt conflicts with the recorded attempt.", 409)
            # A late confirmed receipt can resolve an uncertain expired lease, but never dispatch again.
            job["receipt"] = receipt
            if outcome in {"created", "existing"}:
                job.update(state="added" if outcome == "created" else "already_present", contact_id=contact_id)
            elif outcome == "not_sent" and job["attempts"] < MAX_ATTEMPTS and self.clock() < job["expires_at"]:
                jitter = int(job["id"][:2], 16) % 5
                job.update(state="queued", next_attempt_at=self.clock() + min(300, 5 * 2 ** (job["attempts"] - 1) + jitter))
            else:
                job["state"] = "needs_review" if outcome in {"review", "uncertain"} else "failed"
            self._save(job, "receipt", outcome=outcome)
            return {"ok": True}


def configured_queue():
    if not enabled():
        raise QueueError("CRM queue is disabled.")
    if not os.getenv("ACTII_CONNECTOR_BINDINGS_FILE"):
        raise QueueError("CRM connector bindings are not configured.")
    return ImportQueue()
