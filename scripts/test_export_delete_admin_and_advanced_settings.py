"""
Regression tests for two production UX fixes:

1. Admin export deletion returning HTTP 403 for some CSV files.

   Root cause: NOT a permission/ownership bug. leadbot_delete_export()
   (app.main) and latest_csvs()/render_lead_dashboard() (agents.
   lead_dashboard_agent) both gate on the exact same
   _leadbot_export_visible_to_user(), which already returns True
   unconditionally for admin -- an admin can always delete anything they
   can see, including legacy/unowned exports. The actual cause is CSRF
   token staleness: agents.auth_agent.issue_csrf_token() unconditionally
   overwrites the session's stored csrf_token_hash on every call, and
   _get_or_create_csrf_token() is called on every GET to /lead-bot,
   /history, and /settings. A token embedded in an already-rendered
   /lead-bot page goes stale the instant any other request re-mints one
   for that same session (a second tab, a visit to /history, even
   reloading /lead-bot) -- so whichever delete happens to fire after that
   fails CSRF verification, regardless of which file it targets. This
   file proves that exact sequence, and the fix: a new GET
   /lead-bot/csrf-token endpoint the delete button now calls immediately
   before each delete, instead of relying on the page-load-time token.

2. Advanced Settings (Limit / Per Query Limit / Max Queries) hidden from
   guests and standard users, kept for admins only under "Internal scan
   controls". These fields are cosmetic in the current UI -- the Scan
   Size preset dropdown always overwrites the real submitted values in
   buildLiveStartFormData() -- and the real ceiling is enforced
   server-side for everyone via agents.lead_live_job_agent.run_job()'s
   clean_int() clamp, plus agents.guest_session_agent.
   clamp_guest_scan_params() for guests specifically. This file also pins
   that clamp directly as a pure-function test.

agents.auth_agent.AUTH_DB is monkeypatched to a temp sqlite file, same
pattern as scripts/test_guest_beta_access.py and
scripts/test_leadbot_csrf_routes.py. Export fixtures are written under the
real exports/ directory (agents/lead_dashboard_agent.py hardcodes that
path) with randomized filenames, removed in cleanup.
"""

import json
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
import agents.lead_dashboard_agent as dashboard_agent
import agents.lead_live_job_agent as job_agent

VALID_PASSWORD = "correct-horse-battery-staple"

PERMISSION_DENIED_MESSAGE = "You do not have permission to delete this export."


class ExportFixture:
    """Throwaway CSV export under the real exports/ directory, optionally
    with an owner sidecar. Passing owner_username=None creates a genuine
    legacy/unowned export (no sidecar, no leadbot_export_owners.json
    entry) -- exactly the case the audit asked about."""

    def __init__(self, owner_username=None):
        self.dir = Path("exports")
        self.dir.mkdir(exist_ok=True)
        self.filename = f"test_export_delete_fix_{uuid.uuid4().hex}.csv"
        self.path = self.dir / self.filename
        self.owner_sidecar = self.dir / f"{self.filename}.owner.json"

        self.path.write_text("domain,title\nexample.com,Example Biz\n", encoding="utf-8")

        if owner_username is not None:
            self.owner_sidecar.write_text(
                json.dumps({"owner_username": owner_username}), encoding="utf-8"
            )

    def cleanup(self):
        for p in (self.path, self.owner_sidecar):
            try:
                p.unlink()
            except FileNotFoundError:
                pass


class ExportDeleteTestCase(unittest.TestCase):
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
        auth_agent.create_user("theadmin", VALID_PASSWORD, role="admin", email="admin@example.com")

        self.client = TestClient(appmain.app)

    def login(self, client, username):
        user = auth_agent.get_user_by_username(username)
        token = auth_agent.create_session(user)
        client.cookies.set(appmain.AUTH_COOKIE_NAME, token)
        csrf_token = auth_agent.issue_csrf_token(token)
        return token, csrf_token


class AdminAndOwnershipDeleteTests(ExportDeleteTestCase):
    """admin can delete an export owned by another user / normal user
    cannot delete another user's export / normal user can delete their
    own export / legacy-unowned handling is safe."""

    def test_admin_can_delete_export_owned_by_another_user(self):
        export = ExportFixture(owner_username="user1")
        self.addCleanup(export.cleanup)

        _, csrf_token = self.login(self.client, "theadmin")
        resp = self.client.post(
            f"/lead-bot/delete-export/{export.filename}",
            data={"csrf_token": csrf_token},
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 303)
        self.assertFalse(export.path.exists())

    def test_normal_user_cannot_delete_another_users_export(self):
        export = ExportFixture(owner_username="user1")
        self.addCleanup(export.cleanup)

        _, csrf_token = self.login(self.client, "user2")
        resp = self.client.post(
            f"/lead-bot/delete-export/{export.filename}",
            data={"csrf_token": csrf_token},
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 403)
        self.assertTrue(export.path.exists())

    def test_normal_user_can_delete_their_own_export(self):
        export = ExportFixture(owner_username="user1")
        self.addCleanup(export.cleanup)

        _, csrf_token = self.login(self.client, "user1")
        resp = self.client.post(
            f"/lead-bot/delete-export/{export.filename}",
            data={"csrf_token": csrf_token},
            follow_redirects=False,
        )

        self.assertEqual(resp.status_code, 303)
        self.assertFalse(export.path.exists())

    def test_legacy_unowned_export_is_invisible_and_undeletable_by_standard_user(self):
        export = ExportFixture(owner_username=None)  # genuine legacy/unowned file
        self.addCleanup(export.cleanup)

        self.assertFalse(
            dashboard_agent._leadbot_export_visible_to_user(export.path, current_user={"role": "standard", "username": "user1"})
        )

        _, csrf_token = self.login(self.client, "user1")
        resp = self.client.post(
            f"/lead-bot/delete-export/{export.filename}",
            data={"csrf_token": csrf_token},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(export.path.exists())

    def test_admin_can_safely_delete_legacy_unowned_export(self):
        export = ExportFixture(owner_username=None)  # genuine legacy/unowned file
        self.addCleanup(export.cleanup)

        self.assertTrue(
            dashboard_agent._leadbot_export_visible_to_user(export.path, current_user={"role": "admin", "username": "theadmin"})
        )

        _, csrf_token = self.login(self.client, "theadmin")
        resp = self.client.post(
            f"/lead-bot/delete-export/{export.filename}",
            data={"csrf_token": csrf_token},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertFalse(export.path.exists())

    def test_admin_visibility_and_delete_permission_are_never_out_of_sync(self):
        """Directly answers the audit question: can an admin see a file
        the delete endpoint won't let them delete? Both the listing
        (latest_csvs) and the delete route gate on the exact same
        function, so this asserts they can never diverge for any of the
        three export shapes in play (owned-by-self, owned-by-other,
        legacy/unowned)."""
        owned_by_admin = ExportFixture(owner_username="theadmin")
        owned_by_other = ExportFixture(owner_username="user1")
        legacy = ExportFixture(owner_username=None)
        self.addCleanup(owned_by_admin.cleanup)
        self.addCleanup(owned_by_other.cleanup)
        self.addCleanup(legacy.cleanup)

        admin_user = {"role": "admin", "username": "theadmin"}
        visible = set(dashboard_agent.latest_csvs(current_user=admin_user))

        for fixture in (owned_by_admin, owned_by_other, legacy):
            self.assertIn(fixture.path, visible)
            self.assertTrue(
                dashboard_agent._leadbot_export_visible_to_user(fixture.path, current_user=admin_user)
            )


class PathSafetyStillEnforcedTests(ExportDeleteTestCase):
    """Path traversal / filename safety protections remain intact."""

    def test_safe_export_file_strips_traversal_to_a_bare_basename(self):
        """Direct coverage of the actual defense: Path(filename).name
        strips any directory components before the path is ever joined
        with the exports/ dir, so a traversal attempt can only ever
        resolve to a (very likely nonexistent) basename inside exports/,
        never outside it."""
        self.assertIsNone(dashboard_agent.safe_export_file("../../../etc/passwd"))
        self.assertIsNone(dashboard_agent.safe_export_file("../../secret.csv"))
        self.assertIsNone(dashboard_agent.safe_export_file("/etc/passwd"))

    def test_traversal_attempt_over_http_is_rejected_without_a_server_error(self):
        _, csrf_token = self.login(self.client, "theadmin")
        resp = self.client.post(
            "/lead-bot/delete-export/..%2F..%2F..%2Fetc%2Fpasswd",
            data={"csrf_token": csrf_token},
            follow_redirects=False,
        )
        # Routing itself refuses an encoded-slash path segment (404) before
        # the handler ever runs; either way nothing is deleted and there's
        # no 500.
        self.assertIn(resp.status_code, (303, 404))

    def test_nonexistent_filename_is_rejected_safely(self):
        _, csrf_token = self.login(self.client, "theadmin")
        resp = self.client.post(
            "/lead-bot/delete-export/does-not-exist.csv",
            data={"csrf_token": csrf_token},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)

    def test_non_csv_extension_is_rejected(self):
        export = ExportFixture(owner_username="theadmin")
        self.addCleanup(export.cleanup)
        evil = Path("exports") / "not-a-real-export.txt"
        evil.write_text("not a csv", encoding="utf-8")
        self.addCleanup(lambda: evil.unlink(missing_ok=True))

        _, csrf_token = self.login(self.client, "theadmin")
        resp = self.client.post(
            "/lead-bot/delete-export/not-a-real-export.txt",
            data={"csrf_token": csrf_token},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertTrue(evil.exists())


class FreshCsrfTokenEndpointTests(ExportDeleteTestCase):
    """The new GET /lead-bot/csrf-token endpoint, and the exact stale-token
    scenario that produced the reported 403."""

    def test_logged_out_request_is_rejected(self):
        resp = self.client.get("/lead-bot/csrf-token")
        self.assertEqual(resp.status_code, 401)

    def test_successful_response_is_never_cacheable(self):
        """A freshly minted CSRF token must never be browser- or
        proxy-cached, or a stale value could be served back out."""
        self.login(self.client, "user1")
        resp = self.client.get("/lead-bot/csrf-token")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("cache-control"), "no-store, private")
        self.assertEqual(resp.headers.get("pragma"), "no-cache")

    def test_logged_in_user_gets_a_usable_token(self):
        export = ExportFixture(owner_username="user1")
        self.addCleanup(export.cleanup)

        self.login(self.client, "user1")
        token_resp = self.client.get("/lead-bot/csrf-token")
        self.assertEqual(token_resp.status_code, 200)
        fresh_token = token_resp.json()["csrf_token"]
        self.assertTrue(fresh_token)

        resp = self.client.post(
            f"/lead-bot/delete-export/{export.filename}",
            data={"csrf_token": fresh_token},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertFalse(export.path.exists())

    def test_reproduces_and_fixes_the_stale_token_403(self):
        """This is the exact production sequence: a token is minted (e.g.
        by an earlier /lead-bot page load), then something else re-mints
        one for the same session (another tab loading /lead-bot, or a
        visit to /history/-settings) before the first token is used --
        which invalidates it and 403s the delete. Fetching a fresh token
        from the new endpoint immediately before deleting recovers it,
        with no change to how tokens are issued or rotated."""
        export = ExportFixture(owner_username="user1")
        self.addCleanup(export.cleanup)

        token, first_csrf = self.login(self.client, "user1")

        # Simulate another page re-minting a token for this same session
        # (this is exactly what _get_or_create_csrf_token -> issue_csrf_token
        # does on every /lead-bot, /history, or /settings GET).
        auth_agent.issue_csrf_token(token)

        stale_resp = self.client.post(
            f"/lead-bot/delete-export/{export.filename}",
            data={"csrf_token": first_csrf},
            follow_redirects=False,
        )
        self.assertEqual(stale_resp.status_code, 403)
        self.assertTrue(export.path.exists(), "export must survive a rejected delete")

        fresh_token = self.client.get("/lead-bot/csrf-token").json()["csrf_token"]
        fixed_resp = self.client.post(
            f"/lead-bot/delete-export/{export.filename}",
            data={"csrf_token": fresh_token},
            follow_redirects=False,
        )
        self.assertEqual(fixed_resp.status_code, 303)
        self.assertFalse(export.path.exists())


class DeleteButtonFrontendSourceTests(unittest.TestCase):
    """
    Restore row/button state on failure; never remove the row unless
    deletion actually succeeds; the raw "HTTP 403" alert is replaced with
    a clear permission message.

    AGENT_NOTE: same limitation as scripts/test_guest_scan_rejection_messaging.py
    -- this repo has no browser/JS test runner outside the one Playwright
    class in test_leadbot_csrf_routes.py, so these are source-level
    regression checks on the server-rendered page, not executed-DOM
    assertions.
    """

    @classmethod
    def setUpClass(cls):
        cls.source = dashboard_agent.render_lead_dashboard(current_user=None, csrf_token="tok")

    def _delete_click_handler_block(self):
        match = re.search(
            r"<!--\s*LEADBOT MODERN EXPORT DELETE UI START\s*-->"
            r"(.*?)"
            r"<!--\s*LEADBOT MODERN EXPORT DELETE UI END\s*-->",
            self.source,
            re.S,
        )
        self.assertIsNotNone(match, "could not locate the export-delete click handler block")
        return match.group(1)

    def test_permission_denied_message_replaces_raw_http_403(self):
        block = self._delete_click_handler_block()
        self.assertIn(PERMISSION_DENIED_MESSAGE, block)
        self.assertNotIn('"Export delete failed: HTTP " + res.status', block)

    def test_row_is_only_removed_on_success_branch(self):
        block = self._delete_click_handler_block()
        # Exactly one row.remove() call, and it must not appear inside the
        # failure (.catch) branch.
        self.assertEqual(block.count("row.remove()"), 1)
        catch_match = re.search(r"\.catch\(function \(err\) \{(.*?)\}\);", block, re.S)
        self.assertIsNotNone(catch_match)
        self.assertNotIn("row.remove()", catch_match.group(1))

    def test_button_and_row_state_restored_on_failure(self):
        block = self._delete_click_handler_block()
        catch_match = re.search(r"\.catch\(function \(err\) \{(.*?)\}\);", block, re.S)
        self.assertIsNotNone(catch_match)
        catch_body = catch_match.group(1)
        self.assertIn("restoreDeleteButtonAndRow()", catch_body)

        restore_fn_match = re.search(
            r"function restoreDeleteButtonAndRow\(\) \{(.*?)\n        \}",
            block,
            re.S,
        )
        self.assertIsNotNone(restore_fn_match)
        restore_body = restore_fn_match.group(1)
        self.assertIn("__leadbotExportDeleteBusy = false", restore_body)
        self.assertIn('classList.remove("is-deleting", "is-deleted")', restore_body)

    def test_fresh_token_is_fetched_before_delete_post(self):
        block = self._delete_click_handler_block()
        self.assertIn('fetch("/lead-bot/csrf-token"', block)
        # The token fetch must happen before the delete POST to `url`.
        token_pos = block.index('fetch("/lead-bot/csrf-token"')
        delete_pos = block.index("method: \"POST\"")
        self.assertLess(token_pos, delete_pos)


class AdvancedSettingsVisibilityTests(unittest.TestCase):
    """Guest and standard-user HTML does not expose Advanced Settings;
    admin HTML retains the controls, renamed to "Internal scan controls"."""

    def test_guest_html_has_no_advanced_settings(self):
        # Bare id/class strings alone aren't safe markers to assert absent
        # -- the static CSS rule for .leadbot-advanced, and the (already
        # null-safe) preset-change JS that does
        # document.getElementById("leadbotLimitDisplay"), both stay in
        # every render regardless of role. Neither renders a control for a
        # nonexistent element; assert on the actual <input>/<details>
        # markup instead.
        source = dashboard_agent.render_lead_dashboard(current_user=None, csrf_token="tok")
        self.assertNotIn("Internal scan controls", source)
        self.assertNotIn("LEADBOT ADVANCED SCAN CONTROLS", source)
        self.assertNotIn('<details class="leadbot-advanced">', source)
        self.assertNotIn('id="leadbotLimitDisplay"', source)
        self.assertNotIn('id="leadbotPerQueryLimitDisplay"', source)
        self.assertNotIn('id="leadbotMaxQueriesDisplay"', source)

    def test_standard_user_html_has_no_advanced_settings(self):
        source = dashboard_agent.render_lead_dashboard(
            current_user={"role": "standard", "username": "user1"}, csrf_token="tok"
        )
        self.assertNotIn("Internal scan controls", source)
        self.assertNotIn("LEADBOT ADVANCED SCAN CONTROLS", source)
        self.assertNotIn('<details class="leadbot-advanced">', source)

    def test_admin_html_retains_advanced_settings_renamed(self):
        source = dashboard_agent.render_lead_dashboard(
            current_user={"role": "admin", "username": "theadmin"}, csrf_token="tok"
        )
        self.assertIn("Internal scan controls", source)
        self.assertIn("leadbot-advanced", source)
        self.assertIn("leadbotLimitDisplay", source)
        self.assertNotIn(">Advanced settings<", source)

    def test_keyword_market_and_start_button_remain_for_every_role(self):
        for current_user in (None, {"role": "standard", "username": "user1"}, {"role": "admin", "username": "theadmin"}):
            source = dashboard_agent.render_lead_dashboard(current_user=current_user, csrf_token="tok")
            self.assertIn("Keyword", source)
            self.assertIn("City, State, or ZIP Code", source)
            self.assertIn("Start Lead Finder Scan", source)
            self.assertIn('id="scanSizePreset"', source)


class ServerSideLimitClampTests(unittest.TestCase):
    """Server-side scan limits remain enforced regardless of submitted
    form values -- pure-function coverage of the clamp that actually
    governs a scan run, without starting a real Lead Finder scan."""

    def test_clean_int_clamps_absurdly_large_values(self):
        self.assertEqual(job_agent.clean_int(999999, 50, 1, 60), 60)
        self.assertEqual(job_agent.clean_int(999999, 8, 1, 12), 12)

    def test_clean_int_clamps_zero_and_negative_values(self):
        self.assertEqual(job_agent.clean_int(0, 50, 1, 60), 1)
        self.assertEqual(job_agent.clean_int(-5, 8, 1, 12), 1)

    def test_clean_int_falls_back_to_default_on_garbage_input(self):
        self.assertEqual(job_agent.clean_int("not-a-number", 50, 1, 60), 50)
        self.assertEqual(job_agent.clean_int(None, 8, 1, 12), 8)

    def test_clean_int_passes_through_in_range_values(self):
        self.assertEqual(job_agent.clean_int(30, 50, 1, 60), 30)


if __name__ == "__main__":
    unittest.main()
