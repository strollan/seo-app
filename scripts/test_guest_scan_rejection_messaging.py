"""
Regression tests for clear guest-scan rejection messaging (vs. a completed
zero-lead scan) on POST /lead-bot/live-start and the /lead-bot dashboard
page's client-side handling of that endpoint's response.

Background: a production incident traced a "zero leads" report from a
guest visitor to a rejected submission (guest scan rate limit or an
expired/missing guest CSRF cookie pair) that never reached create_job() at
all -- no job file was ever written. The old frontend code showed a single
generic alert() ("your session may have expired") for *any* non-ok
response and then called window.location.reload(), which reloads the
current /lead-bot page and can land on the same empty/stale results view a
genuine zero-lead scan would show. That made a blocked submission visually
indistinguishable from a completed scan that found nothing.

This file covers, at the backend/TestClient level:
  - the 4th guest scan attempt in the rate-limit window gets a distinct,
    specific 429 body plus a Retry-After header (not the old generic text)
  - a missing/mismatched guest CSRF cookie gets a distinct 403 body
  - neither rejection ever calls create_job() (no job file is written)
  - the completed-zero-lead copy on /lead-bot/live/{job_id} is present and
    textually distinct from both rejection messages
  - a blank market (400) still uses its own separate, pre-existing message

...and, at the rendered-page-source level (this repo has no browser/JS
test runner -- see AGENT_NOTES in this file's class docstrings for what
that does and doesn't prove):
  - the old alert()/reload() bug pattern is gone from the dashboard's
    fetch handler
  - the button-reset helper is invoked on every rejection branch
  - the manual "Refresh page" button is gone entirely (see "Simplify
    failed scan recovery"): expired-session recovery is now automatic --
    a logged-in user's 403 gets one silent CSRF-token refetch + resubmit,
    with a single automatic reload as the bounded fallback (guests, or a
    still-failing retry)

agents.auth_agent.AUTH_DB is monkeypatched to a temp sqlite file, and the
in-process guest scan rate limiter dict is isolated per test, same pattern
as scripts/test_guest_beta_access.py. agents.lead_live_job_agent.create_job
is mocked everywhere a scan might otherwise start (no real scan or
external API call ever happens).
"""

import re
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os
os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

from fastapi.testclient import TestClient

import app.main as appmain
import agents.auth_agent as auth_agent
import agents.guest_session_agent as guest_agent
import agents.lead_live_job_agent as job_agent
import agents.lead_dashboard_agent as dashboard_agent

VALID_PASSWORD = "correct-horse-battery-staple"

RATE_LIMIT_MESSAGE = (
    "Guest scan limit reached. You can run up to 3 guest scans per hour. "
    "Create an account for full access or try again later."
)
GUEST_SESSION_EXPIRED_MESSAGE = "Your guest session expired. Refresh this page and try again."
ZERO_LEAD_MESSAGE = "The scan completed, but no qualifying leads were found for this search."


class RejectionMessagingTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        self.auth_db_path = Path(self.tmpdir.name) / "test_auth.db"
        db_patch = mock.patch.object(auth_agent, "AUTH_DB", self.auth_db_path)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        auth_agent.init_auth_db()

        # Isolate the in-process guest rate limiter between tests.
        rl_patch = mock.patch.object(guest_agent, "_guest_scan_rate_limit_attempts", {})
        rl_patch.start()
        self.addCleanup(rl_patch.stop)

        self._job_files_to_remove = []

    def tearDown(self):
        for path in self._job_files_to_remove:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def guest_client(self):
        return TestClient(appmain.app)

    def get_guest_cookies(self, client):
        resp = client.get("/lead-bot")
        self.assertEqual(resp.status_code, 200)
        return (
            client.cookies.get(guest_agent.GUEST_ID_COOKIE),
            client.cookies.get(guest_agent.GUEST_CSRF_COOKIE),
        )

    def write_fake_job(self, guest_id="", status="done", leads=None):
        job_id = uuid.uuid4().hex[:16]
        job = {
            "job_id": job_id,
            "status": status,
            "message": "Done" if status == "done" else "Working",
            "params": {"guest_id": guest_id, "owner_email": "", "owner_username": ""},
            "leads": leads if leads is not None else [],
            "counts": {"found": 0, "cached": 0, "enriched": 0, "needs_research": 0},
            "export_file": "",
        }
        job_agent.write_job(job)
        self._job_files_to_remove.append(job_agent.job_path(job_id))
        return job_id


class GuestRateLimitRejectionTests(RejectionMessagingTestCase):
    """1. HTTP 429 from /lead-bot/live-start."""

    def test_fourth_guest_submission_gets_distinct_429_message(self):
        client = self.guest_client()
        _guest_id, guest_csrf = self.get_guest_cookies(client)

        with mock.patch.object(job_agent, "create_job", return_value="job-rl") as mock_create:
            for _ in range(guest_agent.GUEST_SCAN_RATE_LIMIT_MAX_ATTEMPTS):
                resp = client.post(
                    "/lead-bot/live-start",
                    data={"keyword": "plumber", "market": "Long Island, NY", "csrf_token": guest_csrf},
                    follow_redirects=False,
                )
                self.assertEqual(resp.status_code, 303)

            fourth_resp = client.post(
                "/lead-bot/live-start",
                data={"keyword": "plumber", "market": "Long Island, NY", "csrf_token": guest_csrf},
                follow_redirects=False,
            )

        self.assertEqual(fourth_resp.status_code, 429)
        self.assertIn(RATE_LIMIT_MESSAGE, fourth_resp.text)
        # Distinct from the guest-CSRF-failure and zero-lead copy.
        self.assertNotIn(GUEST_SESSION_EXPIRED_MESSAGE, fourth_resp.text)
        self.assertNotIn(ZERO_LEAD_MESSAGE, fourth_resp.text)
        # Only 3 create_job calls total -- the 4th (rejected) attempt never
        # reaches job creation.
        self.assertEqual(mock_create.call_count, guest_agent.GUEST_SCAN_RATE_LIMIT_MAX_ATTEMPTS)

    def test_fourth_guest_submission_includes_retry_after_header(self):
        client = self.guest_client()
        _guest_id, guest_csrf = self.get_guest_cookies(client)

        with mock.patch.object(job_agent, "create_job", return_value="job-rl"):
            for _ in range(guest_agent.GUEST_SCAN_RATE_LIMIT_MAX_ATTEMPTS):
                client.post(
                    "/lead-bot/live-start",
                    data={"keyword": "plumber", "market": "Long Island, NY", "csrf_token": guest_csrf},
                    follow_redirects=False,
                )

            fourth_resp = client.post(
                "/lead-bot/live-start",
                data={"keyword": "plumber", "market": "Long Island, NY", "csrf_token": guest_csrf},
                follow_redirects=False,
            )

        self.assertEqual(fourth_resp.status_code, 429)
        retry_after = fourth_resp.headers.get("Retry-After")
        self.assertIsNotNone(retry_after)
        self.assertGreater(int(retry_after), 0)
        self.assertLessEqual(int(retry_after), guest_agent.GUEST_SCAN_RATE_LIMIT_WINDOW_SECONDS)

    def test_rejected_rate_limited_attempt_creates_no_job_file(self):
        client = self.guest_client()
        _guest_id, guest_csrf = self.get_guest_cookies(client)

        before = set(Path("data/leadbot_live_jobs").glob("*.json"))

        with mock.patch.object(job_agent, "create_job", return_value="job-rl"):
            for _ in range(guest_agent.GUEST_SCAN_RATE_LIMIT_MAX_ATTEMPTS):
                client.post(
                    "/lead-bot/live-start",
                    data={"keyword": "plumber", "market": "Long Island, NY", "csrf_token": guest_csrf},
                    follow_redirects=False,
                )
            resp = client.post(
                "/lead-bot/live-start",
                data={"keyword": "plumber", "market": "Long Island, NY", "csrf_token": guest_csrf},
                follow_redirects=False,
            )

        after = set(Path("data/leadbot_live_jobs").glob("*.json"))
        self.assertEqual(resp.status_code, 429)
        # create_job is mocked (returns a string, writes nothing), so any
        # real job file appearing here would mean the rejection path fell
        # through to the real create_job import despite the mock.
        self.assertEqual(before, after)


class GuestCsrfRejectionTests(RejectionMessagingTestCase):
    """2. HTTP 403 caused by guest CSRF/session failure."""

    def test_missing_guest_csrf_cookie_gets_distinct_403_message(self):
        client = self.guest_client()  # never visited /lead-bot: no cookies at all

        with mock.patch.object(job_agent, "create_job") as mock_create:
            resp = client.post(
                "/lead-bot/live-start",
                data={"keyword": "plumber", "market": "Long Island, NY", "csrf_token": ""},
                follow_redirects=False,
            )

        self.assertEqual(resp.status_code, 403)
        self.assertIn(GUEST_SESSION_EXPIRED_MESSAGE, resp.text)
        self.assertNotIn(RATE_LIMIT_MESSAGE, resp.text)
        self.assertNotIn(ZERO_LEAD_MESSAGE, resp.text)
        mock_create.assert_not_called()

    def test_mismatched_guest_csrf_token_gets_distinct_403_message(self):
        client = self.guest_client()
        self.get_guest_cookies(client)  # mints a real guest CSRF cookie

        with mock.patch.object(job_agent, "create_job") as mock_create:
            resp = client.post(
                "/lead-bot/live-start",
                data={
                    "keyword": "plumber",
                    "market": "Long Island, NY",
                    "csrf_token": "not-the-real-token",
                },
                follow_redirects=False,
            )

        self.assertEqual(resp.status_code, 403)
        self.assertIn(GUEST_SESSION_EXPIRED_MESSAGE, resp.text)
        mock_create.assert_not_called()

    def test_logged_in_user_csrf_failure_message_unchanged_in_spirit(self):
        """A logged-in user's own (DB-backed) CSRF failure is a separate
        code path from the guest one above -- confirms this task did not
        touch it (still 403, still no job created), without pinning its
        exact wording (not part of this task's required copy)."""
        auth_agent.create_user("user1", VALID_PASSWORD, role="standard", email="user1@example.com")
        client = self.guest_client()
        user = auth_agent.get_user_by_username("user1")
        token = auth_agent.create_session(user)
        client.cookies.set(appmain.AUTH_COOKIE_NAME, token)

        with mock.patch.object(job_agent, "create_job") as mock_create:
            resp = client.post(
                "/lead-bot/live-start",
                data={"keyword": "plumber", "market": "Long Island, NY", "csrf_token": "bad-token"},
                follow_redirects=False,
            )

        self.assertEqual(resp.status_code, 403)
        mock_create.assert_not_called()


class ValidationVsZeroLeadDistinctionTests(RejectionMessagingTestCase):
    """3 & 5. A completed zero-lead job is textually and visually distinct
    from every rejection message; a blank-market validation error keeps
    its own separate message."""

    def test_blank_market_uses_its_own_message_not_session_or_rate_limit_copy(self):
        client = self.guest_client()
        _guest_id, guest_csrf = self.get_guest_cookies(client)

        with mock.patch.object(job_agent, "create_job") as mock_create:
            resp = client.post(
                "/lead-bot/live-start",
                data={"keyword": "plumber", "market": "", "csrf_token": guest_csrf},
                follow_redirects=False,
            )

        self.assertEqual(resp.status_code, 400)
        self.assertIn(dashboard_agent.MARKET_REQUIRED_MESSAGE, resp.text)
        self.assertNotIn(GUEST_SESSION_EXPIRED_MESSAGE, resp.text)
        self.assertNotIn(RATE_LIMIT_MESSAGE, resp.text)
        mock_create.assert_not_called()

    def test_completed_zero_lead_job_uses_the_required_distinct_copy(self):
        client = self.guest_client()
        client.get("/lead-bot")
        guest_id = client.cookies.get(guest_agent.GUEST_ID_COOKIE)
        job_id = self.write_fake_job(guest_id=guest_id, status="done", leads=[])

        resp = client.get(f"/lead-bot/live/{job_id}")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(ZERO_LEAD_MESSAGE, resp.text)
        # The zero-lead copy must never be confusable with a rejection.
        self.assertNotIn(RATE_LIMIT_MESSAGE, resp.text)
        self.assertNotIn(GUEST_SESSION_EXPIRED_MESSAGE, resp.text)

    def test_all_three_messages_are_pairwise_distinct_strings(self):
        messages = [RATE_LIMIT_MESSAGE, GUEST_SESSION_EXPIRED_MESSAGE, ZERO_LEAD_MESSAGE]
        for i, a in enumerate(messages):
            for b in messages[i + 1:]:
                self.assertNotEqual(a, b)
                self.assertNotIn(a, b)
                self.assertNotIn(b, a)


class DashboardFetchHandlerSourceTests(unittest.TestCase):
    """
    6 (partial) & button-reset / no-auto-resubmit coverage.

    AGENT_NOTE: this repo has no browser/JS test runner (no Playwright,
    no package.json, nothing under scripts/ that drives a real browser
    against this page), so these are source-level regression checks on the
    server-rendered page, not executed-DOM/browser assertions. They prove
    the *code* that must run client-side is present and the old buggy
    pattern is gone -- they cannot prove the browser actually runs it as
    intended. See the completion report for what this does and doesn't
    cover.
    """

    @classmethod
    def setUpClass(cls):
        cls.source = dashboard_agent.render_lead_dashboard(current_user=None, csrf_token="tok")

    def test_old_generic_alert_and_reload_pattern_is_gone(self):
        self.assertNotIn(
            "your session may have expired",
            self.source,
            "the old catch-all message must not remain",
        )
        # The old bug: any non-ok response called window.location.reload(),
        # which could land a rejected guest on a stale/empty results view.
        # Assert no reload() call sits inside the live-start .then() handler
        # specifically (a reload() elsewhere in the file, e.g. a manual
        # "Refresh page" button click handler, is fine and expected).
        then_block_match = re.search(
            r"fetch\(\"/lead-bot/live-start\".*?\}\)\.catch\(function \(\) \{.*?\}\);",
            self.source,
            re.S,
        )
        self.assertIsNotNone(then_block_match, "could not locate the live-start fetch handler")
        self.assertNotIn("alert(", then_block_match.group(0))
        self.assertNotIn("window.location.reload()", then_block_match.group(0))

    def test_reset_button_helper_invoked_on_every_rejection_branch(self):
        then_block_match = re.search(
            r"fetch\(\"/lead-bot/live-start\".*?\}\)\.catch\(function \(\) \{.*?\}\);",
            self.source,
            re.S,
        )
        self.assertIsNotNone(then_block_match)
        block = then_block_match.group(0)
        # resetStartScanButton() must run before the status-code branches,
        # and again in the network-failure .catch().
        self.assertGreaterEqual(block.count("resetStartScanButton()"), 2)

    def test_no_manual_refresh_page_button_remains(self):
        # The old manual "Refresh page" control (button markup, its id,
        # and its click-only listener) is gone entirely -- expired-session
        # recovery is now automatic, so there is nothing left for a
        # visitor to click.
        self.assertNotIn("leadbotRefreshPageBtn", self.source)
        self.assertNotIn(">Refresh page<", self.source)
        self.assertNotIn("refreshPageBtnEl", self.source)

    def test_expired_session_recovery_is_automatic_and_bounded(self):
        # A logged-in user's 403 triggers exactly one silent CSRF-token
        # refetch + resubmit (via submitLiveStart(form, true)), never a
        # button click. Guests -- who use a separate cookie-based CSRF
        # pair that can only be re-minted by a real page load -- and a
        # logged-in user's still-failing retry both fall through to one
        # automatic reload, with no user action required either way.
        self.assertIn("function submitLiveStart(form, isRetry)", self.source)
        self.assertIn("/lead-bot/csrf-token", self.source)
        self.assertIn("submitLiveStart(form, true)", self.source)
        self.assertIn("function recoverExpiredSessionAndReload()", self.source)

        submit_fn_match = re.search(
            r"function submitLiveStart\(form, isRetry\) \{(.*?)\n    \}\n\n    function launchLiveStart",
            self.source,
            re.S,
        )
        self.assertIsNotNone(submit_fn_match, "could not locate submitLiveStart()")
        body = submit_fn_match.group(1)

        # The retry must be gated on !isRetry, so it can only ever fire
        # once per original submission -- never a second time on the
        # retry's own response.
        self.assertIn("if (!isRetry && !window.LEADBOT_IS_GUEST)", body)
        # A bare, unconditional reload() must not exist inside this
        # function -- every path to recovery goes through the named,
        # single-purpose recoverExpiredSessionAndReload() helper instead.
        self.assertNotIn("window.location.reload()", body)

    def test_no_recursive_or_delayed_auto_resubmit_in_fetch_handler(self):
        then_block_match = re.search(
            r"fetch\(\"/lead-bot/live-start\".*?\}\)\.catch\(function \(\) \{.*?\}\);",
            self.source,
            re.S,
        )
        block = then_block_match.group(0)
        self.assertNotIn("launchLiveStart(", block)
        self.assertNotIn("setTimeout(", block)

    def test_guest_disclosure_does_not_imply_full_logged_in_scan_size(self):
        self.assertIn("Guest preview: up to 3 scans per hour with limited results.", self.source)
        note_match = re.search(r'<p class="leadbot-guest-note"[^>]*>(.*?)</p>', self.source, re.S)
        self.assertIsNotNone(note_match, "guest note paragraph not found")
        note_text = note_match.group(1)
        self.assertNotIn("25", note_text)

    def test_is_guest_js_flag_present_for_guest_render(self):
        self.assertIn("window.LEADBOT_IS_GUEST = true;", self.source)

    def test_is_guest_js_flag_false_for_logged_in_render(self):
        logged_in_source = dashboard_agent.render_lead_dashboard(
            current_user={"id": 1, "email": "user1@example.com", "username": "user1", "role": "standard"},
            csrf_token="tok",
        )
        self.assertIn("window.LEADBOT_IS_GUEST = false;", logged_in_source)


if __name__ == "__main__":
    unittest.main()
