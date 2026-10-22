"""Synthetic queue/HTTP/companion tests. No listeners, real imports or credentials.

Set ACTII_TEST_ROOT to the read-only ACT II checkout for disposable CRM tests.
"""
import ast
import hashlib
import io
import json
import os
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import requests
import test_leadbot_crm_import as legacy
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from agents import actii_queue_agent as queue_module, actii_queue_routes as routes
from agents.actii_file_store import StorageError, atomic_json, file_lock, read_json
from agents.actii_pull_connector import ConnectorError, PullConnector, load_config

TOKEN = "synthetic-connector-token-for-tests-only-0000"


class QueueFixture(unittest.TestCase):
    send = legacy.CrmImportTests.send

    def setUp(self):
        legacy.CrmImportTests.setUp(self)
        self.enterContext(patch.dict(os.environ, {"ACTII_CRM_MODE": "queue"}))
        self.now = [time.time()]
        self.user = self.auth.get_user_by_username("owner")
        self.binding = {"connector_id": "synthetic-workstation", "user_id": self.user["id"],
                        "username": "owner", "binding_version": "v1", "enabled": True,
                        "token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
                        "expires_at": datetime.fromtimestamp(self.now[0] + 86400 * 30, timezone.utc).isoformat()}
        self.bindings = Path.cwd() / "bindings.json"
        self.write_binding()
        self.enterContext(patch.dict(os.environ, {"ACTII_CONNECTOR_BINDINGS_FILE": str(self.bindings)}))
        self.queue = queue_module.ImportQueue(bindings=self.bindings, clock=lambda: self.now[0])
        self.enterContext(patch.object(routes, "configured_queue", return_value=self.queue))
        app = FastAPI()
        ns = dict(app=app, AuthRequest=Request, Request=Request, JSONResponse=JSONResponse,
                  AUTH_COOKIE_NAME="vast_session", Path=Path)
        names = {"auth_current_user", "leadbot_crm_job_status", "actii_connector_claim", "actii_connector_result"}
        tree = ast.parse((ROOT / "app/main.py").read_text())
        functions = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
        self.assertEqual({n.name for n in functions}, names)
        exec(compile(ast.Module(body=functions, type_ignores=[]), "queue-route-harness", "exec"), ns)
        self.status_client = TestClient(app)
        self.status_client.cookies.update(self.client.cookies)
        self.worker_headers = {"Authorization": "Bearer " + TOKEN}

    def write_binding(self):
        atomic_json(self.bindings, {"version": 1, "connectors": [self.binding]})

    def enqueue(self):
        response = self.send()
        self.assertEqual(response.status_code, 202, response.text)
        self.transport.assert_not_called()
        return response.json()

    def claim(self):
        response = self.status_client.post("/integrations/actii/claim", json={}, headers=self.worker_headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["job"]

    def ack(self, job, outcome="created", **kwargs):
        return self.status_client.post("/integrations/actii/result", headers=self.worker_headers,
                                       json={"id": job["id"], "lease_id": job["lease_id"], "outcome": outcome, **kwargs})

    def state(self, job_id):
        return self.status_client.get("/lead-bot/crm-jobs/" + job_id).json()["state"]



class QueueTests(QueueFixture):
    def test_default_disabled_and_no_bindings_fail_closed(self):
        for mode in (None, "", "disabled", "unknown"):
            with self.subTest(mode=mode), patch.dict(os.environ):
                if mode is None:
                    os.environ.pop("ACTII_CRM_MODE", None)
                else:
                    os.environ["ACTII_CRM_MODE"] = mode
                self.assertEqual(self.send().status_code, 503)
                with self.assertRaises(queue_module.QueueError):
                    queue_module.configured_queue()
        self.binding["enabled"] = False; self.write_binding()
        self.assertEqual(self.send().status_code, 403)
        self.binding["enabled"] = True; self.binding["user_id"] += 1; self.write_binding()
        self.assertEqual(self.send().status_code, 403)
        self.transport.assert_not_called()

    def test_auth_csrf_cross_origin_and_request_limits(self):
        token = self.payload["csrf_token"]
        self.payload["csrf_token"] = "invalid"
        self.assertEqual(self.send().status_code, 403)
        self.payload["csrf_token"] = token
        self.assertEqual(self.client.post("/lead-bot/add-to-crm", json=self.payload, headers={"Origin": "https://attacker.example"}).status_code, 403)
        self.assertEqual(self.client.post("/lead-bot/add-to-crm", content="x" * 65537).status_code, 413)
        self.client.cookies.clear()
        self.assertEqual(self.send().status_code, 401)

    def test_explicit_ownership_even_for_admin(self):
        with patch.object(self.auth, "get_user_from_token", return_value={**self.user, "role": "admin"}):
            Path("data/leadbot_export_owners.json").write_text(json.dumps({self.file: {"owner_username": "someone-else"}}))
            self.assertEqual(self.send().status_code, 403)

    def test_website_conflicts_missing_and_non_http_rejected(self):
        for website in ("", "https://attacker.example", "https://user@business.example", "file://business.example", "https://business.example:8842", "https://business.example bad"):
            with self.subTest(website=website):
                self.payload["lead"]["website"] = website
                self.assertEqual(self.send().status_code, 422)
        self.payload["lead"]["website"] = "https://www.business.example/contact"
        self.enqueue()

    def test_snapshot_provenance_immutable_and_repeat_click(self):
        self.payload["lead"].update(owner_id="attacker", source_note="forged", lead_score="100", search_metadata={"serp_position": 1})
        first = self.enqueue()
        self.payload["lead"]["phone"] = "changed later"
        self.assertEqual(self.enqueue()["id"], first["id"])
        job = self.claim()
        payload = job["payload"]
        self.assertEqual(payload["owner_id"], "owner")
        self.assertEqual(payload["source_ref"], "leadmeleads:synthetic.csv:business.example")
        self.assertEqual(payload["lead"]["lead_score"], "82")
        self.assertIn("Original scan: synthetic.csv", payload["lead"]["source_note"])
        self.assertNotIn("forged", json.dumps(payload))
        self.assertNotEqual(payload["lead"]["phone"], "changed later")
        public = self.status_client.get("/lead-bot/crm-jobs").json()
        self.assertNotIn("source_note", json.dumps(public))
        self.assertEqual(self.state(first["id"]), "processing")

    def test_concurrent_enqueue_and_claim(self):
        lead = {"company_name": "Synthetic", "website": "https://business.example"}
        def enqueue(_):
            return self.queue.enqueue(self.user, self.file, "business.example", lead)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(enqueue, range(12)))
        self.assertEqual(len({r["id"] for r in results}), 1)
        with ThreadPoolExecutor(max_workers=6) as pool:
            claims = list(pool.map(lambda _: self.queue.claim("Bearer " + TOKEN)["job"], range(8)))
        self.assertEqual(sum(j is not None for j in claims), 1)

    def test_bearer_scope_and_other_owner_status(self):
        job = self.enqueue()
        for header in ({}, {"Authorization": "Bearer wrong"}):
            self.assertEqual(self.status_client.post("/integrations/actii/claim", json={}, headers=header).status_code, 401)
        self.status_client.cookies.clear()
        self.assertEqual(self.status_client.get("/lead-bot/crm-jobs", headers=self.worker_headers).status_code, 401)
        self.assertEqual(self.client.post("/lead-bot/add-to-crm", json={**self.payload, "csrf_token": ""}, headers=self.worker_headers).status_code, 403)
        self.assertEqual(self.queue.status({"id": self.user["id"] + 1, "username": "other"})["jobs"], [])
        with self.assertRaises(queue_module.QueueError) as error:
            self.queue.status({"id": self.user["id"] + 1}, job["id"])
        self.assertEqual(error.exception.status, 404)

    def test_receipts_are_idempotent_conflicts_rejected_and_audited(self):
        item = self.enqueue(); job = self.claim()
        self.assertIsNone(self.claim())
        self.assertEqual(self.ack(job, contact_id=7).status_code, 200)
        self.assertEqual(self.ack(job, contact_id=7).status_code, 200)
        self.assertEqual(self.ack(job, contact_id=8).status_code, 409)
        self.assertEqual(self.state(item["id"]), "added")
        saved = read_json(self.queue.root / (job["id"] + ".json"))
        self.assertEqual([e["event"] for e in saved["events"]], ["enqueued", "claimed", "receipt"])
        self.assertNotIn(TOKEN, json.dumps(saved))

    def test_lease_expiry_never_reclaims_and_late_receipt_resolves(self):
        item = self.enqueue(); job = self.claim()
        self.now[0] += 121
        self.assertIsNone(self.claim())
        self.assertEqual(self.state(item["id"]), "needs_review")
        self.assertEqual(self.ack(job, contact_id=7).status_code, 200)
        self.assertEqual(self.state(item["id"]), "added")

    def test_only_not_sent_retries_and_attempts_are_bounded(self):
        item = self.enqueue()
        for attempt in range(queue_module.MAX_ATTEMPTS):
            job = self.claim(); self.assertIsNotNone(job)
            self.assertEqual(self.ack(job, "not_sent").status_code, 200)
            if attempt < queue_module.MAX_ATTEMPTS - 1:
                self.assertIsNone(self.claim())
                self.now[0] += 301
        self.assertEqual(self.state(item["id"]), "failed")
        self.assertIsNone(self.claim())

    def test_pending_expiry_and_revoked_account_or_export(self):
        item = self.enqueue()
        self.now[0] += queue_module.JOB_SECONDS + 1
        self.assertIsNone(self.claim())
        self.assertEqual(self.state(item["id"]), "failed")

    def test_source_ownership_and_domain_rechecked_on_claim(self):
        item = self.enqueue()
        Path("data/leadbot_export_owners.json").write_text("{}")
        self.assertIsNone(self.claim())
        self.assertEqual(self.state(item["id"]), "failed")

    def test_account_disable_rechecked_without_schema_changes(self):
        item = self.enqueue()
        self.auth.set_user_active(self.user["id"], False)
        self.assertIsNone(self.claim())
        self.assertEqual(read_json(self.queue.root / (item["id"] + ".json"))["state"], "failed")

    def test_binding_changes_and_token_expiry(self):
        item = self.enqueue()
        self.binding["binding_version"] = "v2"; self.write_binding()
        self.assertIsNone(self.claim())
        self.assertEqual(self.state(item["id"]), "failed")
        self.binding["expires_at"] = datetime.fromtimestamp(self.now[0] - 1, timezone.utc).isoformat(); self.write_binding()
        self.assertEqual(self.status_client.post("/integrations/actii/claim", json={}, headers=self.worker_headers).status_code, 401)

    def test_integrity_bad_permissions_symlinks_and_disk_failure(self):
        self.bindings.chmod(0o644)
        self.assertEqual(self.send().status_code, 503)
        self.bindings.chmod(0o600)
        with patch("agents.actii_queue_agent.atomic_json", side_effect=OSError("synthetic disk full")):
            self.assertEqual(self.send().status_code, 503)
        self.assertEqual(list(self.queue.root.glob("*.json")), [])
        item = self.enqueue(); path = self.queue.root / (item["id"] + ".json")
        raw = read_json(path); raw["payload"]["owner_id"] = "attacker"; atomic_json(path, raw)
        self.assertEqual(self.status_client.get("/lead-bot/crm-jobs").status_code, 503)
        path.unlink(); path.symlink_to(self.bindings)
        self.assertEqual(self.status_client.get("/lead-bot/crm-jobs").status_code, 503)

    def test_existing_review_and_uncertain_are_terminal(self):
        item = self.enqueue(); job = self.claim()
        self.assertEqual(self.ack(job, "review").status_code, 200)
        self.assertEqual(self.state(item["id"]), "needs_review")
        self.assertEqual(self.enqueue()["id"], item["id"])
        self.assertIsNone(self.claim())

    def test_revocation_status_and_payload_retirement_keep_tombstone(self):
        item = self.enqueue()
        self.binding["enabled"] = False; self.write_binding()
        self.assertEqual(self.state(item["id"]), "failed")
        self.now[0] += queue_module.JOB_SECONDS + 1
        self.assertEqual(self.state(item["id"]), "failed")
        job = read_json(self.queue.root / (item["id"] + ".json"))
        self.assertIsNone(job["payload"])
        self.assertTrue(job["payload_retired"])
        self.assertEqual(job["events"][-1]["event"], "payload_retired")
        self.binding["enabled"] = True; self.write_binding()
        self.assertEqual(self.enqueue()["id"], item["id"])

    def test_unknown_domain_traversal_duplicate_binding_and_invalid_receipt(self):
        self.payload["source_domain"] = "other.example"
        self.assertEqual(self.send().status_code, 422)
        self.payload["source_domain"] = "business.example"
        self.payload["source_file"] = "../synthetic.csv"
        self.assertEqual(self.send().status_code, 403)
        self.payload["source_file"] = self.file
        atomic_json(self.bindings, {"version": 1, "connectors": [self.binding, self.binding]})
        self.assertEqual(self.send().status_code, 503)
        self.write_binding()
        self.enqueue(); job = self.claim()
        self.assertEqual(self.ack(job, contact_id=True).status_code, 422)
        self.assertEqual(self.ack({**job, "lease_id": "b" * 32}, contact_id=1).status_code, 409)

    def test_new_account_without_binding_cannot_queue(self):
        self.auth.create_user("new-public-signup", "synthetic-password")
        user = self.auth.get_user_by_username("new-public-signup")
        session = self.auth.create_session(user)
        csrf = self.auth.issue_csrf_token(session)
        self.client.cookies.set("vast_session", session)
        Path("data/leadbot_export_owners.json").write_text(json.dumps({self.file: {"owner_username": user["username"]}}))
        self.payload["csrf_token"] = csrf
        self.assertEqual(self.send().status_code, 403)
        self.assertEqual(list(self.queue.root.glob("*.json")), [])

    def test_backup_manifest_includes_durable_spool(self):
        import leadme_deploy
        item = self.enqueue()
        entry = next(item for item in leadme_deploy.BACKUP_ITEMS if item["path"] == "data/actii_import_queue")
        self.assertTrue(entry["reject_symlinks"])
        destination = Path.cwd() / "spool-backup"
        leadme_deploy.backup_directory(self.queue.root, destination)
        self.assertEqual(read_json(destination / (item["id"] + ".json"))["state"], "queued")
        self.assertEqual(destination.stat().st_mode & 0o777, 0o700)

    def test_queue_does_not_change_auth_schema(self):
        import sqlite3
        connection = sqlite3.connect(self.auth.AUTH_DB)
        try:
            before = connection.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
            self.enqueue(); self.claim()
            after = connection.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
            self.assertEqual(before, after)
        finally:
            connection.close()


@unittest.skipUnless(os.getenv("ACTII_TEST_ROOT"), "ACT II checkout not supplied")
class CompanionTests(QueueFixture):
    def setUp(self):
        super().setUp()
        sys.path.insert(0, os.environ["ACTII_TEST_ROOT"])
        self.enterContext(patch.dict(os.environ, {"ACTII_DATABASE": str(Path.cwd() / "bootstrap.sqlite"),
                                                "ACTII_INTEGRATION_SECRET": "synthetic-integration-secret",
                                                "ACTII_LEADME_OWNER_MAP": json.dumps({"owner": "Workspace"})}))
        from actii.main import create_app
        self.crm = create_app(Path("crm.sqlite"), password="synthetic-password", session_secret="synthetic-session")
        self.crm_client = TestClient(self.crm, base_url="http://127.0.0.1:8842")
        self.calls = []
        self.session = Mock()
        self.session.post.side_effect = self.transport_request
        self.secret_file = Path.cwd() / "local-signing.json"
        atomic_json(self.secret_file, {"integration_secret": "synthetic-integration-secret", "owner_map": {"owner": "Workspace"}})
        self.config = {**{k: self.binding[k] for k in ("connector_id", "binding_version", "user_id", "username", "expires_at")},
                       "production_origin": "https://production.example", "token": TOKEN, "workspace": "Workspace",
                       "actii_secret_file": str(self.secret_file), "state_dir": str(Path.cwd() / "companion-state")}
        self.worker = PullConnector(self.config, clock=lambda: self.now[0], session=self.session)

    def transport_request(self, url, **kwargs):
        self.calls.append((url, kwargs))
        self.assertFalse(kwargs["allow_redirects"])
        if url.startswith("https://production.example/"):
            self.assertTrue(kwargs["verify"])
            self.assertNotIn("X-ACTII-Signature", kwargs["headers"])
            response = self.status_client.post(url.replace("https://production.example", ""), json=kwargs["json"], headers=kwargs["headers"])
        elif url == "http://127.0.0.1:8842/internal/leadme/import":
            self.assertNotIn("Authorization", kwargs["headers"])
            response = self.crm_client.post(url, content=kwargs["data"], headers=kwargs["headers"])
        else:
            self.fail("Unexpected network target")
        result = requests.Response()
        result.status_code = response.status_code
        result.raw = io.BytesIO(response.content)
        return result

    def test_companion_real_contract_workspace_metadata_and_repeats(self):
        item = self.enqueue()
        self.assertEqual(self.worker.run_once(), "Added.")
        self.assertFalse(self.session.trust_env)
        self.assertEqual(self.state(item["id"]), "added")
        contacts = self.crm.state.store.contacts("Workspace")
        self.assertEqual(len(contacts), 1)
        self.assertEqual(self.crm.state.store.contacts("owner"), [])
        contact = contacts[0]
        self.assertEqual(contact["website"], self.row["website"])
        record = self.crm.state.store.import_record("Workspace", contact["id"])
        self.assertEqual(record["source_ref"], "leadmeleads:synthetic.csv:business.example")
        self.assertEqual(record["lead_score"], "82")
        self.assertIn("Example heading", record["source_payload"])
        self.assertEqual(self.enqueue()["id"], item["id"])
        self.assertEqual(self.worker.run_once(), "No pending import.")
        self.assertEqual(sum(url.startswith("http:") for url, _ in self.calls), 1)
        self.crm_client.post("/login", data={"username": "Other", "password_attempt": "synthetic-password"})
        self.assertEqual(self.crm_client.get(f"/crm/contacts/{contact['id']}").status_code, 404)

    def test_lost_ack_recovers_receipt_without_resubmission_even_after_delete(self):
        item = self.enqueue()
        original = self.session.post.side_effect
        def fail_ack(url, **kwargs):
            if url.endswith("/result"):
                raise requests.ReadTimeout()
            return original(url, **kwargs)
        self.session.post.side_effect = fail_ack
        with self.assertRaises(ConnectorError):
            self.worker.run_once()
        contact = self.crm.state.store.contacts("Workspace")[0]
        self.assertEqual(self.crm.state.store.delete_contact("Workspace", contact["id"], contact["updated_at"]), "deleted")
        self.now[0] += 121
        self.session.post.side_effect = original
        self.worker.run_once()
        self.assertEqual(self.crm.state.store.contacts("Workspace"), [])
        self.assertEqual(self.state(item["id"]), "added")
        self.assertEqual(sum(url.startswith("http:") for url, _ in self.calls), 1)

    def test_crash_after_local_commit_never_reposts(self):
        item = self.enqueue()
        from agents import actii_pull_connector as companion
        original = companion.atomic_json
        def crash(path, value):
            if value.get("phase") == "receipt":
                raise OSError("synthetic crash")
            return original(path, value)
        with patch.object(companion, "atomic_json", side_effect=crash):
            with self.assertRaises(OSError):
                self.worker.run_once()
        self.assertEqual(len(self.crm.state.store.contacts("Workspace")), 1)
        self.worker.run_once()
        self.assertEqual(self.state(item["id"]), "needs_review")
        self.assertEqual(sum(url.startswith("http:") for url, _ in self.calls), 1)

    def test_local_timeout_goes_to_review_and_connect_timeout_can_retry(self):
        item = self.enqueue()
        original = self.session.post.side_effect
        def timeout(url, **kwargs):
            if url.startswith("http:"):
                raise requests.ReadTimeout()
            return original(url, **kwargs)
        self.session.post.side_effect = timeout
        self.worker.run_once()
        self.assertEqual(self.state(item["id"]), "needs_review")
        self.worker.run_once()
        self.assertEqual(self.crm.state.store.contacts("Workspace"), [])

    def test_token_config_mapping_and_singleton_fail_before_network(self):
        config_file = Path.cwd() / "companion.json"; atomic_json(config_file, self.config)
        self.assertEqual(load_config(config_file), self.config)
        for origin in ("http://production.example", "https://production.example/path", "https://u:p@production.example", "https://production.example?token=bad"):
            atomic_json(config_file, {**self.config, "production_origin": origin})
            with self.assertRaises(ConnectorError): load_config(config_file)
        atomic_json(self.secret_file, {"integration_secret": "synthetic", "owner_map": {"owner": "Other Workspace"}})
        with self.assertRaises(ConnectorError): self.worker.run_once()
        self.assertEqual(self.calls, [])
        with file_lock(self.worker.root):
            with self.assertRaises(BlockingIOError): self.worker.run_once()

    def test_real_domain_conflict_is_review_without_merge(self):
        self.crm.state.store.import_lead("Workspace", {"company_name": "Existing", "website": "https://business.example"}, "synthetic:prior", "Synthetic prior note")
        item = self.enqueue()
        self.assertEqual(self.worker.run_once(), "Needs review.")
        self.assertEqual(self.state(item["id"]), "needs_review")
        self.assertEqual(len(self.crm.state.store.contacts("Workspace")), 1)
        self.assertEqual(self.crm.state.store.contacts("Workspace")[0]["company_name"], "Existing")

    def test_real_source_repeat_is_already_present(self):
        self.crm.state.store.import_lead("Workspace", {"company_name": "Existing", "website": "https://business.example"}, "leadmeleads:synthetic.csv:business.example", "Synthetic prior note")
        item = self.enqueue()
        self.assertEqual(self.worker.run_once(), "Already present.")
        self.assertEqual(self.state(item["id"]), "already_present")
        self.assertEqual(len(self.crm.state.store.contacts("Workspace")), 1)

    def test_positive_connect_timeout_retries_same_reference(self):
        item = self.enqueue()
        original = self.session.post.side_effect
        def timeout(url, **kwargs):
            if url.startswith("http:"):
                raise requests.ConnectTimeout()
            return original(url, **kwargs)
        self.session.post.side_effect = timeout
        self.assertEqual(self.worker.run_once(), "Not submitted; retry queued.")
        self.assertEqual(self.state(item["id"]), "queued")
        self.assertEqual(self.crm.state.store.contacts("Workspace"), [])
        self.now[0] += 10
        self.session.post.side_effect = original
        self.assertEqual(self.worker.run_once(), "Added.")
        self.assertEqual(self.state(item["id"]), "added")

    def test_binding_and_payload_tampering_never_reaches_actii(self):
        self.enqueue(); job = self.claim()
        for value in ({**job, "username": "attacker"}, {**job, "payload_hash": "0" * 64}, {**job, "lease_until": self.now[0] - 1}):
            with self.assertRaises(ConnectorError): self.worker._validate_job(value)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
