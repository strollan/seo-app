"""
Regression tests for the V1 security fix converting state-changing LeadBot
GET routes to POST + CSRF:

  - POST /lead-bot/live-start        (app.main.leadbot_live_start)
  - POST /lead-bot/delete-row/{f}    (app.main.leadbot_delete_row_route)
  - POST /lead-bot/delete-export/{f} (app.main.leadbot_delete_export)
  - POST /lead-bot/delete-row-safe   (app.main.leadbot_delete_row_safe)

Before the fix these were plain GET (or GET+POST, for delete-row-safe)
routes reachable by a simple link click or top-level navigation even with a
SameSite=Lax session cookie (Lax cookies still ride along on top-level GET
navigation), so a crafted link could start a paid scan or delete a lead/
export for a logged-in victim with no confirmation and no token check.

agents.auth_agent.AUTH_DB is monkeypatched to a temp sqlite file so real
session/user data is never touched. Real sessions and real CSRF tokens are
issued through the actual auth_agent flow (not mocked) so these tests
exercise the real CSRF check, not a stand-in for it. Export CSVs used for
the delete-row/delete-export/delete-row-safe tests are written under the
real exports/ directory with randomized filenames (agents/lead_dashboard_agent.py
hardcodes "exports" and "data/leadbot_export_owners.json" as literal paths
in a few places, so redirecting them via monkeypatch isn't practical) and
removed in addCleanup.

Also covers the V1 Lead Finder form simplification (visible Industry field
removed, Market relabeled to "City, State, or ZIP Code" and made required):
IsValidMarketFunctionTests, LiveStartMarketValidationTests, and
LeadBotFormMarkupTests. agents.lead_live_job_agent.create_job is mocked in
these tests too, so no real scan or external API call ever happens.
"""

import json
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os
os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

import requests
from fastapi.testclient import TestClient

import app.main as appmain
import agents.auth_agent as auth_agent

VALID_PASSWORD = "correct-horse-battery-staple"


class LeadBotCsrfRouteTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        self.auth_db_path = Path(self.tmpdir.name) / "test_auth.db"
        db_patch = mock.patch.object(auth_agent, "AUTH_DB", self.auth_db_path)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        auth_agent.init_auth_db()

        auth_agent.create_user("user1", VALID_PASSWORD, role="standard", email="user1@example.com")
        auth_agent.create_user("user2", VALID_PASSWORD, role="standard", email="user2@example.com")

        self.client = TestClient(appmain.app)

    def login(self, client, username):
        user = auth_agent.get_user_by_username(username)
        token = auth_agent.create_session(user)
        client.cookies.set(appmain.AUTH_COOKIE_NAME, token)
        csrf_token = auth_agent.issue_csrf_token(token)
        return token, csrf_token


class LiveStartCsrfTests(LeadBotCsrfRouteTestCase):
    def test_get_no_longer_starts_a_scan(self):
        self.login(self.client, "user1")

        with mock.patch("agents.lead_live_job_agent.create_job") as mock_create_job:
            resp = self.client.get(
                "/lead-bot/live-start",
                params={"industry": "roofing", "market": "long island"},
                follow_redirects=False,
            )

        self.assertEqual(resp.status_code, 405)
        mock_create_job.assert_not_called()

    def test_post_without_valid_csrf_is_rejected(self):
        self.login(self.client, "user1")

        with mock.patch("agents.lead_live_job_agent.create_job") as mock_create_job:
            resp = self.client.post(
                "/lead-bot/live-start",
                data={"industry": "roofing", "market": "long island", "csrf_token": "not-a-real-token"},
                follow_redirects=False,
            )

        self.assertEqual(resp.status_code, 403)
        mock_create_job.assert_not_called()

    def test_post_with_valid_csrf_starts_a_scan(self):
        _, csrf_token = self.login(self.client, "user1")

        with mock.patch("agents.lead_live_job_agent.create_job", return_value="job-123") as mock_create_job:
            resp = self.client.post(
                "/lead-bot/live-start",
                data={
                    "industry": "roofing",
                    "market": "long island",
                    "csrf_token": csrf_token,
                },
                follow_redirects=False,
            )

        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/lead-bot/live/job-123")
        mock_create_job.assert_called_once()

    def test_logged_out_post_without_guest_cookie_is_rejected_without_starting_a_scan(self):
        """
        Pre-guest-mode this asserted an unconditional redirect to /login for
        any logged-out POST. Limited guest beta testing (see
        agents.guest_session_agent / scripts/test_guest_beta_access.py) now
        intentionally allows a logged-out scan when a valid guest CSRF
        cookie pair is present. A request with no guest cookie at all (as
        here) still cannot start a scan -- it now fails CSRF verification
        (403) instead of being redirected to login, since anonymous access
        is no longer categorically blocked.
        """
        with mock.patch("agents.lead_live_job_agent.create_job") as mock_create_job:
            resp = self.client.post(
                "/lead-bot/live-start",
                data={"industry": "roofing", "market": "long island", "csrf_token": ""},
                follow_redirects=False,
            )

        self.assertEqual(resp.status_code, 403)
        mock_create_job.assert_not_called()


class IsValidMarketFunctionTests(unittest.TestCase):
    """Pure-function coverage for agents.lead_dashboard_agent.is_valid_market,
    the presence-only check backing the required City/State/ZIP field."""

    def test_blank_is_rejected(self):
        from agents.lead_dashboard_agent import is_valid_market
        self.assertFalse(is_valid_market(""))

    def test_none_is_rejected(self):
        from agents.lead_dashboard_agent import is_valid_market
        self.assertFalse(is_valid_market(None))

    def test_whitespace_only_is_rejected(self):
        from agents.lead_dashboard_agent import is_valid_market
        self.assertFalse(is_valid_market("   "))
        self.assertFalse(is_valid_market("\t\n"))

    def test_city_state_is_accepted(self):
        from agents.lead_dashboard_agent import is_valid_market
        self.assertTrue(is_valid_market("Albany, NY"))

    def test_zip_code_is_accepted(self):
        from agents.lead_dashboard_agent import is_valid_market
        self.assertTrue(is_valid_market("10001"))

    def test_city_state_zip_is_accepted(self):
        from agents.lead_dashboard_agent import is_valid_market
        self.assertTrue(is_valid_market("Hoboken, NJ 07030"))


class LiveStartMarketValidationTests(LeadBotCsrfRouteTestCase):
    """Server-side enforcement of the required market field on
    POST /lead-bot/live-start, covering the Industry-field removal and the
    "City, State, or ZIP Code" requirement."""

    def test_blank_market_is_rejected(self):
        _, csrf_token = self.login(self.client, "user1")

        with mock.patch("agents.lead_live_job_agent.create_job") as mock_create_job:
            resp = self.client.post(
                "/lead-bot/live-start",
                data={"keyword": "plumber", "market": "", "csrf_token": csrf_token},
                follow_redirects=False,
            )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("Enter a city, state, or ZIP code.", resp.text)
        mock_create_job.assert_not_called()

    def test_whitespace_only_market_is_rejected(self):
        _, csrf_token = self.login(self.client, "user1")

        with mock.patch("agents.lead_live_job_agent.create_job") as mock_create_job:
            resp = self.client.post(
                "/lead-bot/live-start",
                data={"keyword": "plumber", "market": "   ", "csrf_token": csrf_token},
                follow_redirects=False,
            )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("Enter a city, state, or ZIP code.", resp.text)
        mock_create_job.assert_not_called()

    def test_city_state_market_starts_a_scan(self):
        _, csrf_token = self.login(self.client, "user1")

        with mock.patch("agents.lead_live_job_agent.create_job", return_value="job-albany") as mock_create_job:
            resp = self.client.post(
                "/lead-bot/live-start",
                data={"keyword": "plumber", "market": "Albany, NY", "csrf_token": csrf_token},
                follow_redirects=False,
            )

        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/lead-bot/live/job-albany")
        mock_create_job.assert_called_once()

    def test_zip_code_market_starts_a_scan(self):
        _, csrf_token = self.login(self.client, "user1")

        with mock.patch("agents.lead_live_job_agent.create_job", return_value="job-zip") as mock_create_job:
            resp = self.client.post(
                "/lead-bot/live-start",
                data={"keyword": "plumber", "market": "10001", "csrf_token": csrf_token},
                follow_redirects=False,
            )

        self.assertEqual(resp.status_code, 303)
        mock_create_job.assert_called_once()

    def test_city_state_zip_market_starts_a_scan(self):
        _, csrf_token = self.login(self.client, "user1")

        with mock.patch("agents.lead_live_job_agent.create_job", return_value="job-hoboken") as mock_create_job:
            resp = self.client.post(
                "/lead-bot/live-start",
                data={"keyword": "plumber", "market": "Hoboken, NJ 07030", "csrf_token": csrf_token},
                follow_redirects=False,
            )

        self.assertEqual(resp.status_code, 303)
        mock_create_job.assert_called_once()

    def test_valid_standard_user_scan_with_keyword_and_market_only(self):
        """No industry field is sent at all -- matching the real V1 form,
        which no longer has an Industry input. industry should still be
        derived from keyword internally without requiring the caller to
        send it."""
        _, csrf_token = self.login(self.client, "user1")

        with mock.patch("agents.lead_live_job_agent.create_job", return_value="job-noindustry") as mock_create_job:
            resp = self.client.post(
                "/lead-bot/live-start",
                data={"keyword": "plumber", "market": "Albany, NY", "csrf_token": csrf_token},
                follow_redirects=False,
            )

        self.assertEqual(resp.status_code, 303)
        mock_create_job.assert_called_once()
        job_params = mock_create_job.call_args[0][0]
        self.assertEqual(job_params["industry"], "plumber")
        self.assertEqual(job_params["keyword"], "plumber")
        self.assertEqual(job_params["market"], "Albany, NY")


class LeadBotFormMarkupTests(LeadBotCsrfRouteTestCase):
    """Asserts the rendered V1 Lead Finder form matches the simplified
    Keyword + City/State/ZIP flow with no visible Industry input."""

    def test_rendered_form_has_no_industry_input(self):
        self.login(self.client, "user1")

        resp = self.client.get("/lead-bot")

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('name="industry"', resp.text)
        self.assertNotIn(">Industry<", resp.text)

    def test_rendered_form_has_city_state_zip_market_field(self):
        self.login(self.client, "user1")

        resp = self.client.get("/lead-bot")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("City, State, or ZIP Code", resp.text)
        self.assertIn('name="market"', resp.text)
        self.assertIn('placeholder="Albany, NY or 10001"', resp.text)
        self.assertIn("Enter a city, state, or ZIP code.", resp.text)

    def test_rendered_form_still_requires_keyword(self):
        self.login(self.client, "user1")

        resp = self.client.get("/lead-bot")

        self.assertEqual(resp.status_code, 200)
        self.assertIn('name="keyword"', resp.text)

    def test_own_domain_field_has_optional_placeholder_for_every_role(self):
        """Own Domain must keep its label and stay optional, with the new
        placeholder, regardless of who is viewing the form (guest, standard
        user, or admin)."""
        import agents.lead_dashboard_agent as dashboard_agent

        expected_input = '<input name="own_domain" value="" placeholder="yourdomain.com (optional)">'

        for current_user in (
            None,
            {"role": "standard", "username": "user1"},
            {"role": "admin", "username": "theadmin"},
        ):
            source = dashboard_agent.render_lead_dashboard(current_user=current_user, csrf_token="tok")
            self.assertIn(">Own Domain<", source)
            # The exact tag (no `required`, no other attributes added) proves
            # the field stayed optional and untouched beyond the placeholder.
            self.assertIn(expected_input, source)

    def test_no_stale_state_required_text_remains(self):
        """Regression: the removed "LEADBOT MARKET STATE HELPER" /
        "LEADBOT MARKET STATE REQUIRED VALIDATION" scripts used to clobber
        the placeholder with this text at runtime and reject ZIP-only input
        on submit. Neither string should appear anywhere in the page source
        now that those blocks are gone."""
        self.login(self.client, "user1")

        resp = self.client.get("/lead-bot")

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("State is required for results", resp.text)
        self.assertNotIn("leadbotMarketStateRequiredError", resp.text)
        self.assertNotIn("marketStateValidationInstalled", resp.text)


class ExportFixture:
    """Creates a throwaway export CSV (+ owner sidecar) under the real
    exports/ directory and guarantees cleanup, since the ownership-lookup
    helpers in agents/lead_dashboard_agent.py hardcode the "exports" path."""

    def __init__(self, owner_username):
        self.dir = Path("exports")
        self.dir.mkdir(exist_ok=True)
        self.filename = f"test_csrf_fix_{uuid.uuid4().hex}.csv"
        self.path = self.dir / self.filename
        self.owner_sidecar = self.dir / f"{self.filename}.owner.json"

        self.path.write_text("domain,title\nexample.com,Example Biz\n", encoding="utf-8")
        self.owner_sidecar.write_text(json.dumps({"owner_username": owner_username}), encoding="utf-8")

    def cleanup(self):
        for p in (self.path, self.owner_sidecar):
            try:
                p.unlink()
            except FileNotFoundError:
                pass

    def rows(self):
        if not self.path.exists():
            return []
        return [
            line.strip() for line in self.path.read_text(encoding="utf-8").splitlines()[1:] if line.strip()
        ]


class DeleteRowCsrfAndOwnershipTests(LeadBotCsrfRouteTestCase):
    def setUp(self):
        super().setUp()
        self.export = ExportFixture(owner_username="user1")
        self.addCleanup(self.export.cleanup)

    def test_get_no_longer_deletes_the_row(self):
        self.login(self.client, "user1")

        resp = self.client.get(
            f"/lead-bot/delete-row/{self.export.filename}",
            params={"domain": "example.com"},
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 405)
        self.assertEqual(self.export.rows(), ["example.com,Example Biz"])

    def test_post_without_valid_csrf_does_not_delete(self):
        self.login(self.client, "user1")

        resp = self.client.post(
            f"/lead-bot/delete-row/{self.export.filename}",
            data={"domain": "example.com", "csrf_token": "not-a-real-token"},
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.export.rows(), ["example.com,Example Biz"])

    def test_owner_post_with_valid_csrf_deletes_the_row(self):
        _, csrf_token = self.login(self.client, "user1")

        resp = self.client.post(
            f"/lead-bot/delete-row/{self.export.filename}",
            data={"domain": "example.com", "csrf_token": csrf_token},
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 303)
        self.assertEqual(self.export.rows(), [])

    def test_other_user_cannot_delete_row_from_someone_elses_export(self):
        _, csrf_token = self.login(self.client, "user2")

        resp = self.client.post(
            f"/lead-bot/delete-row/{self.export.filename}",
            data={"domain": "example.com", "csrf_token": csrf_token},
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.export.rows(), ["example.com,Example Biz"])


class DeleteExportCsrfAndOwnershipTests(LeadBotCsrfRouteTestCase):
    def setUp(self):
        super().setUp()
        self.export = ExportFixture(owner_username="user1")
        self.addCleanup(self.export.cleanup)

    def test_get_no_longer_deletes_the_export(self):
        self.login(self.client, "user1")

        resp = self.client.get(
            f"/lead-bot/delete-export/{self.export.filename}",
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 405)
        self.assertTrue(self.export.path.exists())

    def test_post_without_valid_csrf_does_not_delete(self):
        self.login(self.client, "user1")

        resp = self.client.post(
            f"/lead-bot/delete-export/{self.export.filename}",
            data={"csrf_token": "not-a-real-token"},
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 403)
        self.assertTrue(self.export.path.exists())

    def test_owner_post_with_valid_csrf_deletes_the_export(self):
        _, csrf_token = self.login(self.client, "user1")

        resp = self.client.post(
            f"/lead-bot/delete-export/{self.export.filename}",
            data={"csrf_token": csrf_token},
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 303)
        self.assertFalse(self.export.path.exists())

    def test_other_user_cannot_delete_someone_elses_export(self):
        _, csrf_token = self.login(self.client, "user2")

        resp = self.client.post(
            f"/lead-bot/delete-export/{self.export.filename}",
            data={"csrf_token": csrf_token},
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 403)
        self.assertTrue(self.export.path.exists())


class DeleteRowSafeCsrfAndOwnershipTests(LeadBotCsrfRouteTestCase):
    def setUp(self):
        super().setUp()
        self.export = ExportFixture(owner_username="user1")
        self.addCleanup(self.export.cleanup)

    def test_get_is_rejected(self):
        self.login(self.client, "user1")

        resp = self.client.get(
            "/lead-bot/delete-row-safe",
            params={"filename": self.export.filename, "domain": "example.com"},
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 405)
        self.assertEqual(self.export.rows(), ["example.com,Example Biz"])

    def test_logged_out_post_is_rejected(self):
        resp = self.client.post(
            "/lead-bot/delete-row-safe",
            data={"filename": self.export.filename, "domain": "example.com", "csrf_token": ""},
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 401)
        self.assertFalse(resp.json()["ok"])
        self.assertEqual(self.export.rows(), ["example.com,Example Biz"])

    def test_post_without_valid_csrf_does_not_delete(self):
        self.login(self.client, "user1")

        resp = self.client.post(
            "/lead-bot/delete-row-safe",
            data={
                "filename": self.export.filename,
                "domain": "example.com",
                "csrf_token": "not-a-real-token",
            },
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 403)
        self.assertFalse(resp.json()["ok"])
        self.assertEqual(self.export.rows(), ["example.com,Example Biz"])

    def test_owner_post_with_valid_csrf_deletes_the_row(self):
        _, csrf_token = self.login(self.client, "user1")

        resp = self.client.post(
            "/lead-bot/delete-row-safe",
            data={
                "filename": self.export.filename,
                "domain": "example.com",
                "csrf_token": csrf_token,
            },
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["deleted"], 1)
        self.assertEqual(self.export.rows(), [])

    def test_other_user_cannot_delete_row_from_someone_elses_export(self):
        _, csrf_token = self.login(self.client, "user2")

        resp = self.client.post(
            "/lead-bot/delete-row-safe",
            data={
                "filename": self.export.filename,
                "domain": "example.com",
                "csrf_token": csrf_token,
            },
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 403)
        self.assertFalse(resp.json()["ok"])
        self.assertEqual(self.export.rows(), ["example.com,Example Biz"])


class LeadBotCardsRouteOwnershipTests(LeadBotCsrfRouteTestCase):
    """Regression tests for a bug where GET /lead-bot/cards/{filename}
    (app.main.leadbot_cards_for_selected_export) returned lead cards to any
    logged-in user with no ownership check at all. The client-side "LEADBOT
    SELECTED EXPORT FALLBACK LOADER" script calls this exact route whenever
    the main results area renders empty -- which is exactly what happens
    when the server-side ownership check on GET /lead-bot?file=... correctly
    blocks an unrelated user -- so the missing check let any logged-in user
    view another user's completed scan just by knowing/copying the export
    filename. Only visible via a real browser executing that JS, not via
    route-only tests, which is why LeadBotCardsOwnershipBrowserRegressionTests
    below also exists."""

    def setUp(self):
        super().setUp()
        auth_agent.create_user("admin1", VALID_PASSWORD, role="admin", email="admin1@example.com")
        self.export = ExportFixture(owner_username="user1")
        self.addCleanup(self.export.cleanup)

    def test_owning_standard_user_can_view_cards(self):
        self.login(self.client, "user1")

        resp = self.client.get(f"/lead-bot/cards/{self.export.filename}")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("example.com", resp.text)

    def test_unrelated_standard_user_is_blocked(self):
        self.login(self.client, "user2")

        resp = self.client.get(f"/lead-bot/cards/{self.export.filename}")

        self.assertEqual(resp.status_code, 403)
        self.assertNotIn("example.com", resp.text)

    def test_admin_can_view_any_export_cards(self):
        self.login(self.client, "admin1")

        resp = self.client.get(f"/lead-bot/cards/{self.export.filename}")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("example.com", resp.text)

    def test_logged_out_is_redirected_to_login(self):
        resp = self.client.get(
            f"/lead-bot/cards/{self.export.filename}",
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/login?next=/lead-bot")

    def test_missing_filename_is_blocked_safely(self):
        self.login(self.client, "user1")

        resp = self.client.get("/lead-bot/cards/does-not-exist-12345.csv")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("not found", resp.text.lower())

    def test_non_csv_filename_is_blocked_safely(self):
        self.login(self.client, "user1")

        resp = self.client.get("/lead-bot/cards/not-a-real-export")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("not found", resp.text.lower())


class LeadBotCardsOwnershipBrowserRegressionTests(unittest.TestCase):
    """Drives a real headless browser against a real uvicorn subprocess so
    the client-side "LEADBOT SELECTED EXPORT FALLBACK LOADER" script in
    agents/lead_dashboard_agent.py actually executes -- this is the only way
    to catch the ownership bypass this class regression-tests, since the
    bug was a client-side fetch to an unprotected route, invisible to any
    route-only TestClient test. Skips itself if Playwright/Chromium aren't
    installed rather than failing the suite.

    Uses real local files (this repo's real exports/ and data/app_auth.db,
    same constraint as the rest of this module -- see the module docstring)
    with uniquely-named throwaway accounts/exports, all removed in cleanup.
    No network calls: lead discovery is never invoked, since this test only
    exercises already-completed, hand-written export data.
    """

    PORT = 8791

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest("playwright is not installed")

        cls._sync_playwright_ctx = sync_playwright()
        cls._playwright = cls._sync_playwright_ctx.__enter__()

        try:
            cls._browser = cls._playwright.chromium.launch(args=["--no-sandbox"])
        except Exception as exc:
            cls._sync_playwright_ctx.__exit__(None, None, None)
            raise unittest.SkipTest(f"chromium is not available: {exc}")

        repo_root = Path(__file__).resolve().parent.parent
        cls._proc = subprocess.Popen(
            [
                str(repo_root / "venv" / "bin" / "python3"),
                "-m", "uvicorn", "app.main:app",
                "--host", "127.0.0.1", "--port", str(cls.PORT),
            ],
            cwd=str(repo_root),
            env={
                **os.environ,
                "USE_LIVE_SERP": "false",
                "DATAFORSEO_ENABLED": "0",
                "LEADBOT_DATAFORSEO_ENABLED": "0",
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        base_url = f"http://127.0.0.1:{cls.PORT}"
        deadline = time.time() + 20
        up = False
        while time.time() < deadline:
            try:
                resp = requests.get(f"{base_url}/login", timeout=1)
                if resp.status_code == 200:
                    up = True
                    break
            except Exception:
                pass
            time.sleep(0.3)

        if not up:
            cls._proc.terminate()
            cls._browser.close()
            cls._sync_playwright_ctx.__exit__(None, None, None)
            raise unittest.SkipTest("local dev server did not start in time")

        cls.base_url = base_url

    @classmethod
    def tearDownClass(cls):
        proc = getattr(cls, "_proc", None)
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()

        browser = getattr(cls, "_browser", None)
        if browser is not None:
            browser.close()

        ctx = getattr(cls, "_sync_playwright_ctx", None)
        if ctx is not None:
            ctx.__exit__(None, None, None)

    def setUp(self):
        suffix = uuid.uuid4().hex[:10]
        self.owner_username = f"browsertest_owner_{suffix}"
        self.other_username = f"browsertest_other_{suffix}"
        self.password = "correct-horse-battery-staple"

        auth_agent.create_user(self.owner_username, self.password, role="standard", email=f"{self.owner_username}@example.com")
        auth_agent.create_user(self.other_username, self.password, role="standard", email=f"{self.other_username}@example.com")
        self.addCleanup(self._delete_user, self.owner_username)
        self.addCleanup(self._delete_user, self.other_username)

        self.export = ExportFixture(owner_username=self.owner_username)
        self.addCleanup(self.export.cleanup)

    def _delete_user(self, username):
        import sqlite3
        try:
            conn = sqlite3.connect(auth_agent.AUTH_DB)
            conn.execute("DELETE FROM users WHERE username = ?", (username,))
            conn.commit()
            conn.close()
        except Exception:
            pass

    def _login_and_get_page(self, username):
        context = self._browser.new_context()
        self.addCleanup(context.close)
        page = context.new_page()
        page.goto(f"{self.base_url}/login")
        page.fill('input[name="username"]', username)
        page.fill('input[name="password"]', self.password)
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")
        return page

    def test_owner_sees_their_export_via_fallback_loader(self):
        page = self._login_and_get_page(self.owner_username)
        page.goto(f"{self.base_url}/lead-bot?file={self.export.filename}#results")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(500)

        self.assertIn("example.com", page.content())

    def test_unrelated_user_does_not_see_the_export_via_fallback_loader(self):
        page = self._login_and_get_page(self.other_username)
        page.goto(f"{self.base_url}/lead-bot?file={self.export.filename}#results")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(500)

        self.assertNotIn("example.com", page.content())

    def test_rendered_placeholder_has_no_stale_text(self):
        page = self._login_and_get_page(self.owner_username)
        page.goto(f"{self.base_url}/lead-bot")
        page.wait_for_selector("#leadbotRunForm")
        page.wait_for_timeout(1200)  # legacy scripts used setTimeout(..., 1000) to re-clobber

        html = page.content()
        self.assertIn('placeholder="Albany, NY or 10001"', html)
        self.assertNotIn("State is required for results", html)

    def test_zip_only_input_still_starts_a_scan(self):
        page = self._login_and_get_page(self.owner_username)
        page.goto(f"{self.base_url}/lead-bot")
        page.wait_for_selector("#leadbotRunForm")

        page.fill("#leadbotKeywordInput", "plumber")
        page.fill("#leadbotMarketInput", "10001")
        page.click("#leadbotStartScanButton")
        page.wait_for_timeout(800)

        self.assertIn("/lead-bot/live/", page.url)


if __name__ == "__main__":
    unittest.main()
