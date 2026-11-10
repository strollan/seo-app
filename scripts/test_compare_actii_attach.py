"""URL Compare -> ACT II report attachment: route, queue and connector.

Synthetic only: temporary auth DB, bindings and history; no listeners, no real
ACT II, no production. Uses the same route harness as the Lead Finder queue tests.
"""
import ast
import hashlib
import json
import os
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import test_leadbot_crm_import as legacy
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from agents import actii_queue_agent as queue_module, actii_queue_routes as routes
from agents.actii_file_store import atomic_json
from agents.actii_pull_connector import ConnectorError, PullConnector

TOKEN = "synthetic-connector-token-for-tests-only-0000"
REPORT = "2026-11-03_12-00-00-000001_alpha-plumbing-com_vs_beta-plumbing-com.html"
OTHER = "2026-11-03_12-05-00-000002_gamma-com_vs_delta-com.html"


def history_entry(owner_id, filename=REPORT, site="https://www.alpha-plumbing.com/", **extra):
    entry = {"date": "November 03, 2026 12:00:00.000 PM · ID 001", "timestamp": filename.split("_alpha")[0].split("_gamma")[0],
             "site_url": site, "competitor_url": "https://beta-plumbing.com/", "site_final_url": site,
             "competitor_final_url": "https://www.beta-plumbing.com/home/", "site_domain": "alpha-plumbing.com",
             "competitor_domain": "beta-plumbing.com", "site_score": 85, "competitor_score": 98, "score_difference": -13,
             "top_gaps": ["drain cleaning", "shirley plumber"], "volume_opportunities": [{"keyword": "drain cleaning", "volume": 320}],
             "recommendations": ["Add a 120–155 character meta description targeting 'drain cleaning'."],
             "saved_report": f"/reports/saved/{filename}", "saved_path": "/var/x", "owner_id": owner_id}
    entry.update(extra)
    return entry


class AttachFixture(unittest.TestCase):
    def setUp(self):
        legacy.CrmImportTests.setUp(self)
        self.now = [time.time()]
        self.user = self.auth.get_user_by_username("owner")
        self.auth.create_user("someone-else", "synthetic-password", role="standard")
        other = self.auth.get_user_by_username("someone-else")
        self.history = Path.cwd() / "history.json"
        self.history.write_text(json.dumps([history_entry(self.user["id"]), history_entry(other["id"], OTHER, "https://gamma.com/")]))
        self.enterContext(patch.object(queue_module, "REPORT_HISTORY", self.history))
        self.binding = {"connector_id": "synthetic-workstation", "user_id": self.user["id"], "username": "owner",
                        "binding_version": "v1", "enabled": True, "token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
                        "expires_at": datetime.fromtimestamp(self.now[0] + 86400 * 30, timezone.utc).isoformat()}
        self.bindings = Path.cwd() / "bindings.json"
        atomic_json(self.bindings, {"version": 1, "connectors": [self.binding]})
        self.enterContext(patch.dict(os.environ, {"ACTII_CRM_MODE": "queue", "ACTII_COMPARE_ATTACH_ENABLED": "true",
                                                  "ACTII_CONNECTOR_BINDINGS_FILE": str(self.bindings)}))
        self.queue = queue_module.ImportQueue(bindings=self.bindings, clock=lambda: self.now[0])
        self.enterContext(patch.object(routes, "configured_queue", return_value=self.queue))
        app = FastAPI()
        ns = dict(app=app, AuthRequest=Request, Request=Request, JSONResponse=JSONResponse, AUTH_COOKIE_NAME="vast_session",
                  Path=Path, os=os)
        names = {"auth_current_user", "_csrf_token_valid", "compare_attach_enabled", "history_attach_to_crm",
                 "leadbot_crm_job_status", "actii_connector_claim", "actii_connector_result"}
        tree = ast.parse((ROOT / "app/main.py").read_text())
        functions = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
        self.assertEqual({n.name for n in functions}, names)
        exec(compile(ast.Module(body=functions, type_ignores=[]), "attach-route-harness", "exec"), ns)
        self.app_client = TestClient(app)
        self.app_client.cookies.update(self.client.cookies)
        self.csrf = self.payload["csrf_token"]
        self.worker = {"Authorization": "Bearer " + TOKEN}

    def attach(self, saved_report=f"/reports/saved/{REPORT}", **extra):
        return self.app_client.post("/history/attach-to-crm", json={"csrf_token": self.csrf, "saved_report": saved_report, **extra})

    def claim(self):
        response = self.app_client.post("/integrations/actii/claim", json={"created_after": 978307200}, headers=self.worker)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["job"]

    def ack(self, job, outcome, **extra):
        return self.app_client.post("/integrations/actii/result", headers=self.worker,
                                    json={"id": job["id"], "lease_id": job["lease_id"], "outcome": outcome, **extra})

    def status(self, job_id):
        return self.app_client.get("/lead-bot/crm-jobs/" + job_id).json()


class RouteTests(AttachFixture):
    def test_switch_off_by_default_and_queue_mode_required(self):
        for env in ({"ACTII_COMPARE_ATTACH_ENABLED": "false"}, {"ACTII_CRM_MODE": "local"}):
            with self.subTest(env=env), patch.dict(os.environ, env):
                response = self.attach()
                self.assertEqual(response.status_code, 503)
                self.assertIn("not enabled", response.json()["error"])
        with patch.dict(os.environ):
            os.environ.pop("ACTII_COMPARE_ATTACH_ENABLED")
            self.assertEqual(self.attach().status_code, 503)
        self.assertEqual(list(Path(self.queue.root).glob("*.json")), [])

    def test_sign_in_csrf_and_origin_are_enforced(self):
        self.assertEqual(self.app_client.post("/history/attach-to-crm", json={"csrf_token": "bad", "saved_report": f"/reports/saved/{REPORT}"}).status_code, 403)
        self.assertEqual(self.app_client.post("/history/attach-to-crm", json={"csrf_token": self.csrf, "saved_report": f"/reports/saved/{REPORT}"},
                                              headers={"Origin": "https://attacker.example"}).status_code, 403)
        anonymous = TestClient(self.app_client.app)
        self.assertEqual(anonymous.post("/history/attach-to-crm", json={"saved_report": REPORT}).status_code, 401)

    def test_only_own_existing_reports_can_be_queued(self):
        for saved in (f"/reports/saved/{OTHER}", "/reports/saved/missing.html", f"/reports/saved/../{REPORT}", REPORT, ""):
            with self.subTest(saved=saved):
                self.assertEqual(self.attach(saved).status_code, 403)
        self.assertEqual(list(Path(self.queue.root).glob("*.json")), [])

    def test_queued_payload_carries_the_report_and_never_a_contact(self):
        response = self.attach()
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["state"], "queued")
        job = self.claim()
        self.assertEqual(job["kind"], "compare_report")
        self.assertEqual(job["source_domain"], "alpha-plumbing.com")
        payload = job["payload"]
        self.assertEqual(set(payload), {"owner_id", "source_ref", "report"})
        self.assertEqual(payload["owner_id"], "owner")
        self.assertTrue(payload["source_ref"].startswith("leadmeleads-compare:2026-11-03_12-00-00"))
        report = payload["report"]
        self.assertEqual((report["site_url"], report["competitor_url"]), ("https://www.alpha-plumbing.com/", "https://beta-plumbing.com/"))
        self.assertEqual((report["site_score"], report["competitor_score"], report["score_difference"]), (85, 98, -13))
        self.assertEqual(report["top_gaps"], ["drain cleaning", "shirley plumber"])
        self.assertEqual(report["recommendations"][0][:20], "Add a 120–155 charac")
        self.assertEqual(report["report_url"], f"/reports/saved/{REPORT}")
        self.assertNotIn("lead", payload)

    def test_repeat_attach_reuses_the_same_job(self):
        first, second = self.attach().json(), self.attach().json()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(list(Path(self.queue.root).glob("*.json"))), 1)


class OutcomeTests(AttachFixture):
    def run_outcome(self, outcome, **extra):
        job_id = self.attach().json()["id"]
        job = self.claim()
        self.assertEqual(self.ack(job, outcome, **extra).status_code, 200)
        return self.status(job_id)

    def test_each_outcome_maps_to_its_message(self):
        cases = [("attached", {"contact_id": 7}, "attached", "Report attached to the ACT II contact."),
                 ("already_attached", {"contact_id": 7}, "already_attached", "Already added."),
                 ("no_contact", {}, "no_match", "No matching contact in ACT II for this website."),
                 ("ambiguous", {}, "needs_review", "Needs review on the ACT II workstation. Do not resubmit.")]
        for outcome, extra, state, message in cases:
            with self.subTest(outcome=outcome):
                for path in Path(self.queue.root).glob("*.json"):
                    path.unlink()
                result = self.run_outcome(outcome, **extra)
                self.assertEqual((result["state"], result["message"]), (state, message))

    def test_lead_outcomes_are_refused_for_report_jobs(self):
        self.attach()
        job = self.claim()
        for outcome in ("created", "existing", "review"):
            with self.subTest(outcome=outcome):
                self.assertEqual(self.ack(job, outcome, contact_id=3).status_code, 422)
        self.assertEqual(self.ack(job, "attached").status_code, 422)  # contact id required

    def test_deleting_the_saved_report_revokes_a_queued_job(self):
        job_id = self.attach().json()["id"]
        self.history.write_text("[]")
        self.assertEqual(self.status(job_id)["state"], "failed")


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._raw = status, json.dumps(body).encode()
    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def iter_content(self, size): yield self._raw


class ConnectorTests(AttachFixture):
    def connector(self, local_response):
        secret_file = Path.cwd() / "local-signing.json"
        atomic_json(secret_file, {"integration_secret": "synthetic-secret", "owner_map": {"owner": "Synthetic Workspace"}})
        config = {"state_dir": str(Path.cwd() / "connector-state"), "claim_created_after": "2001-01-01T00:00:00+00:00", "production_origin": "https://leadmeleads.example",
                  "token": TOKEN, "actii_secret_file": str(secret_file), "workspace": "Synthetic Workspace",
                  "expires_at": self.binding["expires_at"], **{k: self.binding[k] for k in ("connector_id", "binding_version", "user_id", "username")}}
        sent = []
        client = self.app_client
        class Session:
            trust_env = True
            def post(inner, url, **kwargs):
                if url.startswith("https://leadmeleads.example"):
                    response = client.post(url.removeprefix("https://leadmeleads.example"), json=kwargs["json"], headers=self.worker)
                    return FakeResponse(response.status_code, response.json())
                sent.append((url, json.loads(kwargs["data"])))
                return FakeResponse(*local_response)
        return PullConnector(config, clock=lambda: self.now[0], session=Session()), sent

    def test_end_to_end_attach_posts_to_the_report_endpoint_only(self):
        job_id = self.attach().json()["id"]
        connector, sent = self.connector((200, {"ok": True, "status": "attached", "contact_id": 14,
                                                "open_url": "http://127.0.0.1:8842/crm/contacts/14"}))
        self.assertEqual(connector.run_once(), "Report attached.")
        self.assertEqual([url for url, _ in sent], ["http://127.0.0.1:8842/internal/leadme/attach-report"])
        self.assertEqual(set(sent[0][1]), {"owner_id", "source_ref", "report"})
        self.assertEqual(self.status(job_id)["state"], "attached")
        self.assertEqual(connector.run_once(), "No pending import.")  # nothing dispatched twice

    def test_no_match_and_needs_review_do_not_stop_later_jobs(self):
        for body, expected, state in ((( 404, {"ok": False, "status": "no_contact"})), "No matching contact.", "no_match"), \
                                     (((409, {"ok": False, "status": "ambiguous"})), "Needs review: more than one matching contact.", "needs_review"):
            with self.subTest(expected=expected):
                for path in Path(self.queue.root).glob("*.json"):
                    path.unlink()
                job_id = self.attach().json()["id"]
                connector, _ = self.connector(body)
                self.assertEqual(connector.run_once(), expected)
                self.assertEqual(self.status(job_id)["state"], state)
                self.assertEqual(connector.run_once(), "No pending import.")  # no durable stop

    def test_unexpected_local_response_is_uncertain_and_stops(self):
        self.attach()
        connector, _ = self.connector((500, {"ok": False}))
        self.assertEqual(connector.run_once(), "Needs review; import outcome is uncertain.")
        with self.assertRaises(ConnectorError):
            connector.run_once()

    def test_report_job_with_a_mismatched_domain_is_not_dispatched(self):
        self.attach()
        connector, sent = self.connector((200, {}))
        original = self.queue.claim
        def tampered(authorization):
            result = original(authorization)
            result["job"]["source_domain"] = "someone-else.com"
            return result
        with patch.object(self.queue, "claim", side_effect=tampered):
            with self.assertRaises(ConnectorError):
                connector.run_once()
        self.assertEqual(sent, [])


class HistoryTemplateTests(unittest.TestCase):
    def render(self, enabled, user_id=1):
        from jinja2 import Environment, FileSystemLoader, select_autoescape
        env = Environment(loader=FileSystemLoader(str(ROOT / "app/templates")), autoescape=select_autoescape(["html"]))
        history = [history_entry(1), history_entry(2, OTHER, "https://gamma.com/")]
        return env.get_template("history.html").render(request=None, history=history, logo_url=None, csrf_token="tok",
                                                        actii_attach_enabled=enabled, current_user_id=user_id)

    def test_button_only_for_own_reports_when_enabled(self):
        html = self.render(True)
        self.assertEqual(html.count('class="btn actii-attach"'), 1)
        self.assertIn(f'data-saved-report="/reports/saved/{REPORT}"', html)
        self.assertNotIn(f'data-saved-report="/reports/saved/{OTHER}"', html)
        self.assertIn("/history/attach-to-crm", html)

    def test_no_button_or_script_when_disabled(self):
        html = self.render(False)
        self.assertNotIn("actii-attach", html)
        self.assertNotIn("/history/attach-to-crm", html)


if __name__ == "__main__":
    unittest.main()
