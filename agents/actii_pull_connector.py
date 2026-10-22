"""Explicitly invoked, single-pass HTTPS companion. Never imports ACT II code."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

from agents.actii_file_store import atomic_json, file_lock, private_directory, read_json
from agents.actii_queue_agent import ID, MAX_PAYLOAD, QueueError, payload_hash, timestamp, verify_website

LOCAL_ORIGIN = "http://127.0.0.1:8842"


class ConnectorError(Exception):
    """Messages are safe for the console; never include request/response text."""


def load_config(path):
    try:
        config = read_json(Path(path), limit=16 * 1024)
        parsed = urlsplit(config["production_origin"])
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path or parsed.query or parsed.fragment
                or parsed.port not in {None, 443}):
            raise ValueError
        for key in ("connector_id", "username", "binding_version", "workspace", "state_dir"):
            if not isinstance(config[key], str) or not config[key] or config[key] != config[key].strip():
                raise ValueError
        if type(config["user_id"]) is not int or config["user_id"] <= 0:
            raise ValueError
        token = config["token"]
        if not isinstance(token, str) or not 32 <= len(token) <= 256 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError
        timestamp(config["expires_at"])
        if not isinstance(config["actii_secret_file"], str) or not Path(config["actii_secret_file"]).is_absolute():
            raise ValueError
        if not Path(config["state_dir"]).is_absolute():
            raise ValueError
        return config
    except (OSError, ValueError, KeyError, TypeError):
        raise ConnectorError("Connector configuration is missing or invalid.") from None


class PullConnector:
    def __init__(self, config, *, clock=time.time, session=None):
        self.config = config
        self.clock = clock
        self.root = private_directory(Path(config["state_dir"]))
        self.session = session or requests.Session()
        self.session.trust_env = False

    def _remote(self, path, body):
        try:
            response = self.session.post(
                self.config["production_origin"] + path, json=body,
                headers={"Authorization": "Bearer " + self.config["token"]},
                timeout=(5, 15), allow_redirects=False, verify=True, stream=True)
            with response:
                if response.status_code != 200:
                    raise ConnectorError("Production rejected the connector request; review configuration or retry later.")
                result = self._body(response, MAX_PAYLOAD * 2)
                if not isinstance(result, dict) or result.get("ok") is not True:
                    raise ConnectorError("Invalid production response; review required.")
                return result
        except requests.RequestException:
            raise ConnectorError("Production connection unavailable; run once again later.") from None

    @staticmethod
    def _body(response, limit):
        raw = bytearray()
        for chunk in response.iter_content(8192):
            raw.extend(chunk)
            if len(raw) > limit:
                raise ConnectorError("Oversized response; review required.")
        try:
            return json.loads(raw)
        except ValueError:
            raise ConnectorError("Invalid response; review required.") from None

    def _validate_job(self, job):
        try:
            if not isinstance(job, dict) or not ID.fullmatch(job["id"]) or not ID.fullmatch(job["lease_id"]):
                raise ValueError
            if any(job[key] != self.config[key] for key in ("connector_id", "binding_version", "user_id", "username")):
                raise ValueError
            if not self.clock() < job["lease_until"] <= self.clock() + 125 or not self.clock() < job["expires_at"]:
                raise ValueError
            payload = job["payload"]
            if set(payload) != {"owner_id", "source_ref", "lead"} or payload["owner_id"] != self.config["username"]:
                raise ValueError
            reference = payload["source_ref"].split(":")
            if (len(reference) != 3 or reference[0] != "leadmeleads" or not reference[1].endswith(".csv")
                    or any(c in reference[1] for c in ("/", "\\")) or reference[2] != job["source_domain"]
                    or payload_hash(payload) != job["payload_hash"]
                    or len(json.dumps(payload, ensure_ascii=True).encode()) > MAX_PAYLOAD):
                raise ValueError
            verify_website(payload["lead"], job["source_domain"])
        except (KeyError, TypeError, ValueError, QueueError):
            raise ConnectorError("Job identity or payload failed validation; no import submitted.") from None

    def _local_secret(self):
        # This separate private file contains an operator-verified copy of EXISTING
        # ACT II settings. It never changes ACT II's files or environment.
        try:
            local = read_json(Path(self.config["actii_secret_file"]), limit=16 * 1024)
            secret = local["integration_secret"]
            mapping = local["owner_map"]
            if not isinstance(secret, str) or not secret or secret != secret.strip() or not isinstance(mapping, dict):
                raise ValueError
            if any(not isinstance(k, str) or not k or k != k.strip() or not isinstance(v, str) or not v or v != v.strip()
                   for k, v in mapping.items()):
                raise ValueError
            if mapping.get(self.config["username"], self.config["username"]) != self.config["workspace"]:
                raise ValueError
            return secret
        except (OSError, ValueError, KeyError, TypeError):
            raise ConnectorError("Local signing configuration or verified workspace mapping is invalid.") from None

    def _submit(self, payload, secret):
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        stamp = str(int(self.clock()))
        signature = hmac.new(secret.encode(), stamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
        try:
            response = self.session.post(
                LOCAL_ORIGIN + "/internal/leadme/import", data=raw,
                headers={"Content-Type": "application/json", "X-ACTII-Timestamp": stamp, "X-ACTII-Signature": signature},
                timeout=(3, 5), allow_redirects=False, stream=True)
            with response:
                body = self._body(response, 64 * 1024)
                if not isinstance(body, dict):
                    return {"outcome": "uncertain"}
                if response.status_code == 200 and body.get("ok") is True:
                    contact = body.get("contact_id")
                    if (body.get("status") in {"created", "existing"} and type(contact) is int and contact > 0
                            and body.get("open_url") == f"{LOCAL_ORIGIN}/crm/contacts/{contact}"):
                        return {"outcome": body["status"], "contact_id": contact}
                if response.status_code == 409 and body.get("ok") is False:
                    return {"outcome": "review"}
                if response.status_code in {401, 403, 422, 503} and body.get("ok") is False:
                    return {"outcome": "failed"}
                return {"outcome": "uncertain"}
        except requests.ConnectTimeout:
            # Requests documents ConnectTimeout as safe to retry. Other connection
            # errors may mean the request was sent and the response was lost.
            return {"outcome": "not_sent"}
        except (requests.RequestException, ConnectorError):
            return {"outcome": "uncertain"}

    def _ack(self, path, record):
        self._remote("/integrations/actii/result", record["receipt"])
        record["phase"] = "acknowledged"
        atomic_json(path, record)

    def _check_expiry(self):
        if timestamp(self.config["expires_at"]) <= self.clock():
            raise ConnectorError("Connector credential has expired; operator rotation is required.")

    def _recover_receipts(self):
        """Called under the singleton lock. Only sends saved results to production."""
        recovered = False
        for path in sorted(self.root.glob("*.json")):
            record = read_json(path)
            if (not isinstance(record, dict) or not ID.fullmatch(path.stem) or record.get("id") != path.stem
                    or any(record.get(key) != self.config[key] for key in ("connector_id", "binding_version", "user_id", "username"))
                    or not isinstance(record.get("lease_id"), str) or not ID.fullmatch(record["lease_id"])):
                raise ConnectorError("Local journal binding requires operator review.")
            if record.get("phase") == "dispatching":
                # Persist uncertainty before attempting an acknowledgment. Never retry a dispatch.
                record.update(phase="receipt", receipt={"id": record["id"], "lease_id": record["lease_id"], "outcome": "uncertain"})
                atomic_json(path, record)
            receipt = record.get("receipt")
            if (record.get("phase") not in {"receipt", "acknowledged"} or not isinstance(receipt, dict)
                    or receipt.get("id") != record["id"] or receipt.get("lease_id") != record["lease_id"]
                    or receipt.get("outcome") not in {"created", "existing", "review", "failed", "not_sent", "uncertain"}):
                raise ConnectorError("Local journal state requires operator review.")
            if record["phase"] == "receipt":
                self._ack(path, record)  # A failed acknowledgment leaves the durable receipt intact.
                recovered = True
            if receipt["outcome"] in {"uncertain", "review"}:
                # Keep this durable stop even after acknowledgment and across later invocations.
                raise ConnectorError("Local journal outcome requires operator review; no job claimed.")
        return recovered

    def recover_only(self):
        """Reconcile saved receipts only. No claim, ACT II access, dispatch or polling."""
        with file_lock(self.root, blocking=False):
            self._check_expiry()
            recovered = self._recover_receipts()
            return "Receipts recovered; no jobs claimed." if recovered else "No receipts pending; no jobs claimed."

    def run_once(self):
        """Explicit normal operation: recover, then claim at most one new job."""
        with file_lock(self.root, blocking=False):
            self._check_expiry()
            secret = self._local_secret()  # Validate before claiming any work.
            recovered = self._recover_receipts()
            result = self._remote("/integrations/actii/claim", {})
            job = result.get("job")
            if job is None:
                return "Receipts recovered; no pending import." if recovered else "No pending import."
            self._validate_job(job)
            path = self.root / (job["id"] + ".json")
            if path.exists():
                previous = read_json(path)
                # Only a positively not-sent attempt can ever dispatch again.
                if (previous.get("payload_hash") != job["payload_hash"]
                        or previous.get("receipt", {}).get("outcome") != "not_sent"):
                    record = {"receipt": {"id": job["id"], "lease_id": job["lease_id"], "outcome": "uncertain"}}
                    self._remote("/integrations/actii/result", record["receipt"])
                    return "Needs review; previously dispatched job was not imported again."
            record = {key: job[key] for key in ("id", "connector_id", "binding_version", "user_id", "username", "lease_id", "payload_hash")}
            record.update(phase="dispatching", dispatched_at=self.clock())
            atomic_json(path, record)
            # No retry after any exception from this point without a saved not_sent result.
            receipt = {"id": job["id"], "lease_id": job["lease_id"], **self._submit(job["payload"], secret)}
            record.update(phase="receipt", receipt=receipt)
            atomic_json(path, record)
            self._ack(path, record)
            return {"created": "Added.", "existing": "Already present.", "review": "Needs review.",
                    "uncertain": "Needs review; import outcome is uncertain.", "not_sent": "Not submitted; retry queued.",
                    "failed": "Failed; review configuration."}[receipt["outcome"]]
