"""
Regression tests for the Lead Finder "Block domain" flow: the
".lead-block-one-js" button rendered per lead card (agents/lead_dashboard_agent.py,
"LEADBOT JS ONE DELETE BUTTON" / "LEADBOT DASHBOARD NO CONFIRM BLOCK"), which
POSTs to app.main.leadbot_user_block_add (POST /lead-bot/blocklist/user/add).

Production bug: clicking Block on a lead card (e.g. instacart.com) asked for
confirmation ("Don't show instacart.com in your future Lead Finder scans?"),
then showed "Block failed. Try again." -- yet the card still flipped its
button to "Blocked" and disappeared, as if the block had succeeded.

Two independent root causes, both fixed here:

1. Backend: the CSRF token embedded in the page at load time
   (window.LEADBOT_CSRF_TOKEN) goes stale the instant *any* other request
   for the same session re-mints a token -- agents.auth_agent.issue_csrf_token
   unconditionally overwrites the session's single stored csrf_token_hash on
   every call, and app.main._get_or_create_csrf_token() is invoked on every
   /lead-bot, /history, and /settings render. A block submitted with a
   now-stale token correctly 403s at _leadbot_blocklist_write_guard. This is
   the exact bug class GET /lead-bot/csrf-token was already built to fix for
   the sibling export-delete flow (see its docstring in app.main) -- the
   Block button just never adopted that pattern. It now does.

2. Frontend: the click handler set btn.textContent = "Blocked" immediately
   after the user confirmed, *before* the fetch even started, rather than
   after the backend confirmed success. A failed block (403, network error,
   anything) briefly -- or, per the bug report, apparently permanently --
   left the button reading "Blocked". The fix defers that text change to
   inside the success branch, after `response.ok` is confirmed true.

Route-level tests use a real FastAPI TestClient with real auth/session/CSRF
plumbing (agents.auth_agent.AUTH_DB monkeypatched to a temp sqlite file) and
a real per-user blocklist store (agents.lead_blocked_domain_db_agent.DB_PATH
monkeypatched to a temp sqlite file), so the CSRF staleness bug and its fix
are exercised for real, not mocked away. A second test class does static
shape assertions on the rendered click-handler script, proving the fix's
ordering (fresh-token-fetch before POST, "Blocked" only after response.ok)
without needing a browser.
"""

import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os
os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

from fastapi.testclient import TestClient

import app.main as appmain
import agents.auth_agent as auth_agent
import agents.lead_blocked_domain_db_agent as db_agent
from agents.leadbot_block_gate import blocklist_owner_key, load_effective_blocked_domains

VALID_PASSWORD = "correct-horse-battery-staple"


class LeadFinderBlockRouteTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        self.auth_db_path = Path(self.tmpdir.name) / "test_auth.db"
        auth_db_patch = mock.patch.object(auth_agent, "AUTH_DB", self.auth_db_path)
        auth_db_patch.start()
        self.addCleanup(auth_db_patch.stop)
        auth_agent.init_auth_db()

        self.block_db_path = Path(self.tmpdir.name) / "test_blocklist.sqlite"
        block_db_patch = mock.patch.object(db_agent, "DB_PATH", self.block_db_path)
        block_db_patch.start()
        self.addCleanup(block_db_patch.stop)

        auth_agent.create_user("user1", VALID_PASSWORD, role="standard", email="user1@example.com")

        self.client = TestClient(appmain.app)

    def login(self, username="user1"):
        user = auth_agent.get_user_by_username(username)
        token = auth_agent.create_session(user)
        self.client.cookies.set(appmain.AUTH_COOKIE_NAME, token)
        csrf_token = auth_agent.issue_csrf_token(token)
        return token, csrf_token

    def owner_key(self, username="user1"):
        # Mirrors app.main.auth_current_user's return shape exactly (id/
        # username/role only, no email) -- that's the dict the real route
        # passes to blocklist_owner_key(), not auth_agent.get_user_by_username's
        # full row (which also has "email", a key auth_current_user's dict
        # never has and blocklist_owner_key() checks first).
        return blocklist_owner_key({"username": username})


class SuccessfulBlockTests(LeadFinderBlockRouteTestCase):
    def test_valid_block_persists_the_normalized_domain(self):
        _, csrf_token = self.login()

        resp = self.client.post(
            "/lead-bot/blocklist/user/add",
            data={"domain": "instacart.com", "csrf_token": csrf_token},
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/lead-bot#exports")
        self.assertIn("instacart.com", load_effective_blocked_domains(self.owner_key()))

    def test_www_prefixed_submission_persists_as_bare_domain(self):
        """www.instacart.com and instacart.com must collapse to one block."""
        _, csrf_token = self.login()

        resp = self.client.post(
            "/lead-bot/blocklist/user/add",
            data={"domain": "www.instacart.com", "csrf_token": csrf_token},
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 303)
        blocked = load_effective_blocked_domains(self.owner_key())
        self.assertIn("instacart.com", blocked)
        self.assertNotIn("www.instacart.com", blocked)

    def test_full_url_submission_normalizes_to_bare_domain(self):
        _, csrf_token = self.login()

        resp = self.client.post(
            "/lead-bot/blocklist/user/add",
            data={
                "domain": "https://www.instacart.com/cake-delivery/ny/near-me-in-mastic-beach-ny",
                "csrf_token": csrf_token,
            },
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 303)
        self.assertIn("instacart.com", load_effective_blocked_domains(self.owner_key()))


class FailureDoesNotFalselyPersistTests(LeadFinderBlockRouteTestCase):
    def test_missing_csrf_token_is_rejected_and_nothing_persists(self):
        self.login()

        resp = self.client.post(
            "/lead-bot/blocklist/user/add",
            data={"domain": "instacart.com", "csrf_token": ""},
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 403)
        self.assertNotIn("instacart.com", load_effective_blocked_domains(self.owner_key()))

    def test_wrong_csrf_token_is_rejected_and_nothing_persists(self):
        self.login()

        resp = self.client.post(
            "/lead-bot/blocklist/user/add",
            data={"domain": "instacart.com", "csrf_token": "not-a-real-token"},
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 403)
        self.assertNotIn("instacart.com", load_effective_blocked_domains(self.owner_key()))

    def test_stale_csrf_token_reproduces_the_production_bug(self):
        """The token embedded at page-load time (T1) goes stale the instant
        another request for the same session re-mints one (T2) -- e.g. a
        second /lead-bot render, or a visit to /history or /settings. This
        reproduces exactly that: mint T1, then mint T2 for the same
        session (simulating the other page load), then submit the block
        using the now-stale T1. It must 403, and nothing must persist."""
        token, stale_csrf = self.login()
        auth_agent.issue_csrf_token(token)  # simulates another page re-minting

        resp = self.client.post(
            "/lead-bot/blocklist/user/add",
            data={"domain": "instacart.com", "csrf_token": stale_csrf},
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 403)
        self.assertNotIn("instacart.com", load_effective_blocked_domains(self.owner_key()))

    def test_fresh_token_from_csrf_endpoint_succeeds_after_staleness(self):
        """This is the actual fix's mechanism: fetch a fresh token from
        GET /lead-bot/csrf-token right before submitting, instead of
        relying on the one baked into the page at load time."""
        token, stale_csrf = self.login()
        auth_agent.issue_csrf_token(token)  # stales the page-load token

        fresh = self.client.get("/lead-bot/csrf-token")
        self.assertEqual(fresh.status_code, 200)
        fresh_csrf = fresh.json()["csrf_token"]
        self.assertNotEqual(fresh_csrf, stale_csrf)

        resp = self.client.post(
            "/lead-bot/blocklist/user/add",
            data={"domain": "instacart.com", "csrf_token": fresh_csrf},
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 303)
        self.assertIn("instacart.com", load_effective_blocked_domains(self.owner_key()))

    def test_unauthenticated_request_is_rejected_and_nothing_persists(self):
        resp = self.client.post(
            "/lead-bot/blocklist/user/add",
            data={"domain": "instacart.com", "csrf_token": ""},
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 401)

    def test_invalid_domain_is_rejected_and_nothing_persists(self):
        _, csrf_token = self.login()

        resp = self.client.post(
            "/lead-bot/blocklist/user/add",
            data={"domain": "not a domain", "csrf_token": csrf_token},
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 400)
        self.assertNotIn("not a domain", load_effective_blocked_domains(self.owner_key()))


class CsrfTokenEndpointTests(LeadFinderBlockRouteTestCase):
    def test_returns_a_token_that_validates_for_the_caller_session(self):
        token, _ = self.login()

        resp = self.client.get("/lead-bot/csrf-token")

        self.assertEqual(resp.status_code, 200)
        csrf_token = resp.json()["csrf_token"]
        self.assertTrue(auth_agent.verify_csrf_token(token, csrf_token))

    def test_requires_login(self):
        resp = self.client.get("/lead-bot/csrf-token")
        self.assertEqual(resp.status_code, 401)


class ExistingBlockBehaviorCompatibilityTests(unittest.TestCase):
    """Confirms this fix doesn't disturb the pre-existing default/global
    blocklist behavior (agents.leadbot_block_gate.DEFAULT_HARD_BLOCKS),
    independent of any per-user block added above."""

    def test_default_hard_block_is_still_blocked(self):
        from agents.leadbot_block_gate import is_main_blocked_domain

        self.assertTrue(is_main_blocked_domain("opentable.com"))
        self.assertTrue(is_main_blocked_domain("www.opentable.com"))
        self.assertTrue(is_main_blocked_domain("reservations.opentable.com"))

    def test_unrelated_domain_is_not_blocked(self):
        from agents.leadbot_block_gate import is_main_blocked_domain

        self.assertFalse(is_main_blocked_domain("instacart.com"))


class BlockButtonScriptShapeTests(unittest.TestCase):
    """Static assertions on the rendered "LEADBOT DASHBOARD NO CONFIRM
    BLOCK" script proving the fix's shape: confirm-before-any-mutation,
    fresh-CSRF-token-fetch-before-POST, and "Blocked" only set after
    response.ok is confirmed true."""

    @classmethod
    def setUpClass(cls):
        import agents.lead_dashboard_agent as dashboard_agent

        page = dashboard_agent.render_lead_dashboard(current_user={"role": "standard", "username": "user1"}, csrf_token="tok")
        match = re.search(
            r"<!-- LEADBOT DASHBOARD NO CONFIRM BLOCK START -->(.*?)<!-- LEADBOT DASHBOARD NO CONFIRM BLOCK END -->",
            page,
            re.DOTALL,
        )
        assert match, "block-handler script section not found in rendered page"
        cls.script = match.group(1)

    def index_of(self, needle):
        idx = self.script.find(needle)
        self.assertGreaterEqual(idx, 0, f"expected to find {needle!r} in the block-handler script")
        return idx

    def test_confirm_dialog_uses_expected_copy(self):
        self.assertIn(
            'window.confirm("Don\'t show " + domain + " in your future Lead Finder scans?")',
            self.script,
        )

    def test_cancel_returns_before_any_state_mutation(self):
        confirm_idx = self.index_of("window.confirm(")
        busy_idx = self.index_of('btn.dataset.busy = "1"')
        self.assertLess(
            confirm_idx, busy_idx,
            "confirm() must gate every mutation -- Cancel must leave busy/text/fetch untouched",
        )

    def test_optimistic_text_is_not_the_success_label(self):
        # Regression: this used to be set to "Blocked" here, before the
        # fetch was even sent.
        self.assertIn('btn.textContent = "Blocking...";', self.script)

    def test_fresh_csrf_token_is_fetched_before_the_block_post(self):
        csrf_fetch_idx = self.index_of('fetch("/lead-bot/csrf-token"')
        post_idx = self.index_of("method: \"POST\"")
        self.assertLess(
            csrf_fetch_idx, post_idx,
            "a fresh CSRF token must be fetched before the block POST is sent",
        )

    def test_blocked_text_is_set_only_after_response_ok_check(self):
        ok_check_idx = self.index_of('if (!response.ok) throw new Error("Block request failed");')
        blocked_idx = self.script.find('btn.textContent = "Blocked";')
        self.assertGreaterEqual(
            blocked_idx, 0,
            'expected btn.textContent = "Blocked"; to appear in the script',
        )
        self.assertLess(
            ok_check_idx, blocked_idx,
            "the UI must only flip to \"Blocked\" after response.ok is confirmed true",
        )

    def test_failure_path_resets_button_and_alerts(self):
        self.assertIn('btn.textContent = oldText || "Block";', self.script)
        self.assertIn('alert("Block failed. Try again.");', self.script)

    def test_old_text_is_captured_before_any_mutation(self):
        old_text_idx = self.index_of("const oldText = btn.textContent;")
        blocking_idx = self.index_of('btn.textContent = "Blocking...";')
        self.assertLess(
            old_text_idx, blocking_idx,
            "oldText must be captured before the button label is ever changed",
        )


if __name__ == "__main__":
    unittest.main()
