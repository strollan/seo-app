"""SEO Audit v1 (ACT II single-page audit). Synthetic only: no network, no real accounts, no paid calls.

Covers: findings from the real Compare page scan (app.main.fetch_page_data with crawl_get stubbed to synthetic
HTML), no numeric score, incomplete audits for blocked/failed fetches, the connector-authenticated route
(switch off by default, auth, SSRF guard, rate limit, retry cache, no paid calls), and the connector audit pass.
"""
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

from fastapi.testclient import TestClient

import app.main as appmain
import agents.actii_queue_agent as queue_module
import agents.site_audit_agent as audit_module
from agents.actii_file_store import atomic_json
from agents.actii_pull_connector import PullConnector

TOKEN = "synthetic-connector-token-" + "x" * 24
GOOD_HTML = """<html><head><title>Alpha Plumbing | Shirley NY Emergency Plumber</title>
<meta name="description" content="Licensed Shirley NY plumbers for emergency repairs, drain cleaning and water heaters. Call for same-day service.">
<link rel="canonical" href="https://alpha-plumbing.example/"></head><body>
<h1>Emergency Plumber in Shirley, NY</h1><h2>Services</h2><h2>Areas we serve</h2>
<p>{words}</p>
<a href="/drain-cleaning">Drain</a><a href="/water-heaters">Heaters</a><a href="/areas">Areas</a><a href="/about">About</a><a href="/contact">Contact</a>
<img src="a.jpg" alt="Truck"><img src="b.jpg" alt="Team"></body></html>""".replace("{words}", " ".join(["plumbing repair service shirley"] * 100))
POOR_HTML = "<html><head><title>Home</title></head><body><p>Welcome</p><img src='x.jpg'><img src='y.jpg'></body></html>"


class FakeResponse:
    def __init__(self, text, url, status=200):
        self.text, self.url, self.status_code = text, url, status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            error = requests.exceptions.HTTPError(f"{self.status_code} Error")
            error.response = self
            raise error


def crawl(pages):
    def fake(url, **kwargs):
        text, final, status = pages[url]
        return FakeResponse(text, final, status)
    return fake


class BuildAuditTests(unittest.TestCase):
    def audit(self, html, url="https://alpha-plumbing.example/", final=None, status=200):
        with mock.patch.object(appmain, "crawl_get", crawl({url: (html, final or url, status)})):
            return audit_module.build_audit(appmain.fetch_page_data(url), url)

    def test_good_page_passes_with_evidence_and_no_score(self):
        a = self.audit(GOOD_HTML)
        self.assertEqual(a["status"], "complete")
        results = {f["id"]: f["result"] for f in a["findings"]}
        self.assertEqual(results, {"title": "pass", "meta_description": "pass", "h1": "pass", "h2": "pass", "content_depth": "pass",
                                   "internal_links": "pass", "image_alt": "pass", "canonical": "pass"})
        title = next(f for f in a["findings"] if f["id"] == "title")
        self.assertIn("Alpha Plumbing | Shirley NY Emergency Plumber", title["evidence"])
        self.assertNotIn("score", json.dumps(a).lower())

    def test_poor_page_lists_fixes_with_observed_values(self):
        a = self.audit(POOR_HTML)
        by = {f["id"]: f for f in a["findings"]}
        self.assertEqual(by["title"]["result"], "warn"); self.assertIn("(4 characters)", by["title"]["evidence"])
        self.assertEqual(by["meta_description"]["result"], "fail")
        self.assertEqual(by["h1"]["result"], "fail")
        self.assertEqual(by["content_depth"]["result"], "fail")
        self.assertEqual(by["image_alt"]["result"], "fail"); self.assertIn("0 of 2 images", by["image_alt"]["evidence"])
        self.assertEqual(by["canonical"]["result"], "warn")
        self.assertTrue(all(f["recommendation"] for f in a["findings"] if f["result"] in {"warn", "fail"}))
        self.assertEqual(a["summary"]["fail"] + a["summary"]["warn"] + a["summary"]["pass"] + a["summary"]["info"], len(a["findings"]))

    def test_blocked_and_failed_fetches_are_incomplete_without_findings(self):
        blocked = self.audit("Forbidden", status=403)
        self.assertEqual((blocked["status"], blocked["findings"]), ("incomplete", []))
        self.assertIn("blocked", blocked["incomplete_reason"])
        failed = self.audit("oops", status=500)
        self.assertEqual((failed["status"], failed["findings"]), ("incomplete", []))

    def test_offsite_redirect_is_flagged(self):
        a = self.audit(GOOD_HTML, final="https://other-company.example/")
        redirect = next(f for f in a["findings"] if f["id"] == "redirect")
        self.assertEqual(redirect["result"], "warn")
        same = self.audit(GOOD_HTML, final="https://www.alpha-plumbing.example/home")
        self.assertEqual(next(f for f in same["findings"] if f["id"] == "redirect")["result"], "info")


class StrictFetchTests(unittest.TestCase):
    """Audit fetches verify TLS, never use the shared crawl cache, and fail closed; Compare's fetch is unchanged."""

    def capture(self, **fetch_kwargs):
        seen = {}
        def fake(url, **kwargs):
            seen.update(kwargs)
            return FakeResponse(GOOD_HTML, url, 200)
        with mock.patch.object(appmain, "crawl_get", fake):
            appmain.fetch_page_data("https://alpha-plumbing.example/", **fetch_kwargs)
        return seen

    def test_strict_fetch_verifies_tls_and_skips_cache(self):
        seen = self.capture(strict=True)
        self.assertIs(seen["verify"], True)
        self.assertIs(seen["use_cache"], False)

    def test_compare_fetch_is_unchanged(self):
        seen = self.capture()
        self.assertIs(seen["verify"], False)
        self.assertIs(seen["use_cache"], True)

    def test_certificate_error_is_an_incomplete_audit(self):
        import requests
        import agents.crawl_agent as crawl_module
        url = "https://alpha-plumbing.example/"
        with mock.patch.object(crawl_module, "validate_public_url", lambda u: "alpha-plumbing.example"), \
                mock.patch.object(crawl_module.requests, "get", side_effect=requests.exceptions.SSLError("certificate verify failed")) as get:
            audit = audit_module.build_audit(appmain.fetch_page_data(url, strict=True), url)
        self.assertIs(get.call_args.kwargs["verify"], True)
        self.assertEqual((audit["status"], audit["findings"]), ("incomplete", []))

    def test_strict_fetch_ignores_a_cached_unverified_page(self):
        import agents.crawl_agent as crawl_module
        url = "https://alpha-plumbing.example/"
        stale = {"url": url, "status_code": 200, "text": GOOD_HTML, "headers": {}}
        with mock.patch.dict(crawl_module._CRAWL_CACHE, {url: (time.time(), stale)}, clear=True), \
                mock.patch.object(crawl_module, "validate_public_url", lambda u: "alpha-plumbing.example"), \
                mock.patch.object(crawl_module.requests, "get", side_effect=crawl_module.requests.exceptions.ConnectionError("down")):
            audit = audit_module.build_audit(appmain.fetch_page_data(url, strict=True), url)
            self.assertEqual(audit["status"], "incomplete")
            self.assertEqual(crawl_module._CRAWL_CACHE[url][1], stale)   # cache neither used nor overwritten

    def test_non_2xx_final_response_is_incomplete_in_strict_mode(self):
        url = "https://alpha-plumbing.example/"
        for status in (0, 204, 301, 302):
            with mock.patch.object(appmain, "crawl_get", crawl({url: (GOOD_HTML, url, status)})):
                audit = audit_module.build_audit(appmain.fetch_page_data(url, strict=True), url)
            if status == 204:
                self.assertEqual(audit["status"], "complete")
            else:
                self.assertEqual((audit["status"], audit["findings"]), ("incomplete", []), status)


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.cwd = os.getcwd(); os.chdir(self.tmp.name); self.addCleanup(os.chdir, self.cwd)
        os.chmod(self.tmp.name, 0o700)
        binding = {"connector_id": "synthetic-workstation", "user_id": 1, "username": "owner", "binding_version": "v1", "enabled": True,
                   "token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
                   "expires_at": datetime.fromtimestamp(time.time() + 86400 * 30, timezone.utc).isoformat()}
        bindings = Path(self.tmp.name) / "bindings.json"; atomic_json(bindings, {"version": 1, "connectors": [binding]})
        self.env = {"ACTII_CRM_MODE": "queue", "ACTII_CONNECTOR_BINDINGS_FILE": str(bindings), "ACTII_SITE_AUDIT_ENABLED": "true"}
        self.limiter = audit_module.AuditLimiter(per_hour=3, per_day=5)
        self.enterContext(mock.patch.object(audit_module, "LIMITER", self.limiter))
        self.enterContext(mock.patch.object(audit_module.run_site_audit, "__kwdefaults__", {"limiter": self.limiter}))
        self.fetches = []
        def fetch(url, **kwargs):
            self.assertEqual(kwargs, {"strict": True})   # audits always use the TLS-verified, uncached fetch
            self.fetches.append(url)
            return {"error": None, "final_url": url, "title": "Alpha Plumbing | Shirley NY Plumber", "meta_description": "x" * 90,
                    "has_meta": True, "h1": "Plumber", "h1_count": 1, "h2_count": 2, "word_count": 650, "internal_link_count": 8,
                    "image_count": 0, "alt_count": 0, "canonical": url}
        self.enterContext(mock.patch.object(appmain, "fetch_page_data", fetch))
        self.enterContext(mock.patch.object(appmain, "validate_public_url", self.validate))
        # Paid paths must never run: any call fails the test.
        for name in ("run_seo_agent", "enrich_keywords_with_dataforseo", "keyword_gap"):
            self.enterContext(mock.patch.object(appmain, name, side_effect=AssertionError(name + " called")))
        self.client = TestClient(appmain.app)

    @staticmethod
    def validate(url):
        from agents.url_safety import UnsafeURLError
        if "localhost" in url or "127.0.0.1" in url:
            raise UnsafeURLError("That URL cannot be scanned.")

    def post(self, body, token=TOKEN, env=None):
        with mock.patch.dict(os.environ, env if env is not None else self.env):
            return self.client.post("/integrations/actii/site-audit", json=body, headers={"Authorization": "Bearer " + token} if token else {})

    def test_disabled_by_default(self):
        env = dict(self.env); env.pop("ACTII_SITE_AUDIT_ENABLED")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ACTII_SITE_AUDIT_ENABLED", None)
            r = self.post({"request_id": "a" * 32, "url": "https://alpha.example/"}, env=env)
        self.assertEqual(r.status_code, 503); self.assertEqual(self.fetches, [])

    def test_auth_validation_ssrf(self):
        self.assertEqual(self.post({"request_id": "a" * 32, "url": "https://alpha.example/"}, token="").status_code, 401)
        self.assertEqual(self.post({"request_id": "a" * 32, "url": "https://alpha.example/"}, token="wrong-" + "y" * 40).status_code, 401)
        self.assertEqual(self.post({"request_id": "not-an-id", "url": "https://alpha.example/"}).status_code, 422)
        self.assertEqual(self.post({"request_id": "a" * 32, "url": "https://alpha.example/", "extra": 1}).status_code, 422)
        self.assertEqual(self.post({"request_id": "a" * 32, "url": "http://localhost:8842/"}).status_code, 422)
        self.assertEqual(self.post({"request_id": "a" * 32, "url": "ftp://alpha.example/"}).status_code, 422)
        self.assertEqual(self.fetches, [])

    def test_audit_returned_free_cached_and_rate_limited(self):
        r = self.post({"request_id": "b" * 32, "url": "alpha.example"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json(); self.assertEqual(body["audit"]["requested_url"], "https://alpha.example")
        self.assertEqual(body["audit"]["status"], "complete"); self.assertNotIn("score", json.dumps(body).lower())
        self.assertEqual(self.post({"request_id": "b" * 32, "url": "alpha.example"}).json()["cached"], True)
        self.assertEqual(len(self.fetches), 1)                                   # retry never rescans
        codes = [self.post({"request_id": c * 32, "url": "alpha.example"}).status_code for c in "cdef"]
        self.assertEqual(codes, [200, 200, 429, 429])                            # per_hour=3 in this test
        self.assertEqual(len(self.fetches), 3)


class ConnectorPassTests(unittest.TestCase):
    """The connector leases from ACT II, audits on production and returns the result for the same request id."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name); os.chmod(root, 0o700)
        secret = root / "local.json"; atomic_json(secret, {"integration_secret": "synthetic-secret", "owner_map": {"owner": "Workspace"}})
        self.config = {"production_origin": "https://leadmeleads.example", "connector_id": "synthetic-workstation", "username": "owner",
                       "binding_version": "v1", "workspace": "Workspace", "state_dir": str(root / "state"), "user_id": 1, "token": TOKEN,
                       "expires_at": datetime.fromtimestamp(time.time() + 86400, timezone.utc).isoformat(), "actii_secret_file": str(secret)}
        self.calls = []

    def session(self, production):
        test = self

        class Resp:
            def __init__(self, status, body): self.status_code, self.body = status, json.dumps(body).encode()
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def iter_content(self, n): yield self.body

        class Session:
            trust_env = True
            def post(self, url, json=None, data=None, headers=None, **kw):
                body = json if json is not None else __import__("json").loads(data)
                test.calls.append((url, body))
                if url.endswith("/internal/leadme/audit-requests/claim"):
                    return Resp(200, {"ok": True, "requests": [{"request_id": "c" * 32, "url": "https://alpha.example/"}]})
                if url.endswith("/integrations/actii/site-audit"):
                    return Resp(*production(body))
                if url.endswith("/internal/leadme/audit-result"):
                    return Resp(200, {"ok": True, "status": "recorded" if body["outcome"] == "completed" else "requeued", "contact_id": 7})
                raise AssertionError(url)
        return Session()

    def test_completed_audit_is_returned_for_the_same_request(self):
        audit = audit_module.build_audit({"error": None, "final_url": "https://alpha.example/", "title": "T" * 40, "has_meta": True,
                                          "meta_description": "m" * 100, "h1": "H", "h1_count": 1, "h2_count": 1, "word_count": 500,
                                          "internal_link_count": 6, "image_count": 0, "alt_count": 0, "canonical": "https://alpha.example/"},
                                         "https://alpha.example/")
        c = PullConnector(self.config, session=self.session(lambda b: (200, {"ok": True, "request_id": b["request_id"], "audit": audit})),
                          local_origin="http://127.0.0.1:18842")
        self.assertEqual(c.run_site_audits_once(), "Site audits: recorded.")
        urls = [u for u, _ in self.calls]
        self.assertEqual(urls, ["http://127.0.0.1:18842/internal/leadme/audit-requests/claim", "https://leadmeleads.example/integrations/actii/site-audit",
                                "http://127.0.0.1:18842/internal/leadme/audit-result"])
        self.assertEqual(self.calls[2][1]["request_id"], "c" * 32); self.assertEqual(self.calls[2][1]["audit"], audit)

    def test_production_failure_requeues_instead_of_saving(self):
        c = PullConnector(self.config, session=self.session(lambda b: (429, {"ok": False})), local_origin="http://127.0.0.1:18842")
        self.assertEqual(c.run_site_audits_once(), "Site audits: requeued.")
        self.assertEqual(self.calls[-1][1], {"owner_id": "owner", "request_id": "c" * 32, "outcome": "retry"})

    def test_default_local_origin_is_unchanged(self):
        from agents import actii_pull_connector
        self.assertEqual(actii_pull_connector.LOCAL_ORIGIN, "http://127.0.0.1:8842")


if __name__ == "__main__":
    unittest.main()
