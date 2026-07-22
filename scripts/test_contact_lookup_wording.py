"""
Regression tests for the "Enrich Website Details" -> "Find Missing Contact
Details" user-facing wording cleanup.

Scope: visible copy only. The underlying route (/lead-bot/enrich/{filename},
/lead-bot/complete-details/{filename}), function names (leadbot_enrich_this_scan,
enrich_row, enrich_keywords_with_dataforseo), CSV column values
("enriched_this_scan", "seo_snapshot_enriched_this_scan"), and filename
convention (leads_x_enriched.csv) are all internal/backend and are
deliberately left unchanged -- this file asserts they're still there
alongside the new visible copy, not that they're gone.

agents.auth_agent.AUTH_DB is monkeypatched to a temp sqlite file, same
pattern as the other scripts/test_*.py files in this repo.
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
import agents.lead_dashboard_agent as dashboard_agent
import agents.lead_live_job_agent as job_agent

VALID_PASSWORD = "correct-horse-battery-staple"

NEW_BUTTON_LABEL = "Find Missing Contact Details"
NEW_HELPER_TEXT = "Check business websites for missing email, phone, and address information."

# Old phrases that must not appear as *visible* copy anywhere in either
# rendered page. These are deliberately scoped to actual markup/labels,
# not bare substrings -- "Enrich Website Details" alone would also match
# the intentionally-untouched console.log() debug strings (e.g. "LeadBot
# auto Enrich Website Details failed:"), which are never shown to a user
# and are covered separately by test_internal_identifiers_still_use_enrich_and_are_unaffected.
RETIRED_VISIBLE_PHRASES = [
    ">Enrich Website Details<",
    'complete.textContent = "Enrich Website Details"',
    "enrich contact details",
    "contacts are enriched",
    "cache/enrichment",
    "as they are enriched",
    "Enriching contact details and updating this scan",
]


class DashboardWordingTests(unittest.TestCase):
    """Rendered /lead-bot (agents.lead_dashboard_agent.render_lead_dashboard)."""

    @classmethod
    def setUpClass(cls):
        cls.guest_source = dashboard_agent.render_lead_dashboard(current_user=None, csrf_token="tok")
        cls.admin_source = dashboard_agent.render_lead_dashboard(
            current_user={"role": "admin", "username": "theadmin"}, csrf_token="tok"
        )

    def test_new_button_label_present(self):
        self.assertIn(NEW_BUTTON_LABEL, self.guest_source)
        # The button's JS reset also uses the new label, not just the
        # initial static markup.
        self.assertIn(f'complete.textContent = "{NEW_BUTTON_LABEL}"', self.guest_source)

    def test_new_helper_text_present(self):
        self.assertIn(NEW_HELPER_TEXT, self.guest_source)

    def test_new_subtitle_and_progress_copy_present(self):
        self.assertIn("add missing contact details", self.guest_source)
        self.assertIn("contact details are filled in", self.guest_source)
        self.assertIn("checking for missing contact details", self.guest_source)
        self.assertIn("Checking websites for contact details and updating this scan", self.guest_source)

    def test_no_retired_visible_phrases_remain(self):
        for phrase in RETIRED_VISIBLE_PHRASES:
            self.assertNotIn(phrase, self.guest_source, f"retired phrase still present: {phrase!r}")
            self.assertNotIn(phrase, self.admin_source, f"retired phrase still present: {phrase!r}")

    def test_wording_change_present_for_every_role(self):
        """The copy change applies regardless of role -- it's not gated
        behind the admin-only Advanced Settings filter from the other
        pending fix."""
        for current_user in (None, {"role": "standard", "username": "user1"}, {"role": "admin", "username": "theadmin"}):
            source = dashboard_agent.render_lead_dashboard(current_user=current_user, csrf_token="tok")
            self.assertIn(NEW_BUTTON_LABEL, source)
            self.assertIn(NEW_HELPER_TEXT, source)

    def test_internal_identifiers_still_use_enrich_and_are_unaffected(self):
        """Confirms the required identifiers/routes/CSV convention are
        untouched -- this is deliberate, not a regression."""
        source = self.admin_source
        self.assertIn('href*="/lead-bot/enrich/"', source)  # route selector, unchanged
        self.assertIn('name.replace(/_enriched$/i, "")', source)  # filename convention, unchanged

    def test_route_and_function_names_unchanged(self):
        import inspect
        self.assertTrue(hasattr(appmain, "leadbot_enrich_this_scan"))
        source = inspect.getsource(appmain.leadbot_enrich_this_scan)
        self.assertIn("def leadbot_enrich_this_scan", source)

        route_paths = {getattr(r, "path", "") for r in appmain.app.routes}
        self.assertIn("/lead-bot/enrich/{filename}", route_paths)
        self.assertIn("/lead-bot/complete-details/{filename}", route_paths)


class LiveScanPageWordingTests(unittest.TestCase):
    """Rendered /lead-bot/live/{job_id} (app.main.leadbot_live_page)."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        self.auth_db_path = Path(self.tmpdir.name) / "test_auth.db"
        db_patch = mock.patch.object(auth_agent, "AUTH_DB", self.auth_db_path)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        auth_agent.init_auth_db()

        auth_agent.create_user("user1", VALID_PASSWORD, role="standard", email="user1@example.com")
        self.client = TestClient(appmain.app)

        self._job_files_to_remove = []

    def tearDown(self):
        for path in self._job_files_to_remove:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def login(self, client, username):
        user = auth_agent.get_user_by_username(username)
        token = auth_agent.create_session(user)
        client.cookies.set(appmain.AUTH_COOKIE_NAME, token)
        return token

    def write_fake_job(self, owner_email="", owner_username=""):
        job_id = uuid.uuid4().hex[:16]
        job = {
            "job_id": job_id,
            "status": "running",
            "message": "Working",
            "params": {"guest_id": "", "owner_email": owner_email, "owner_username": owner_username},
            "leads": [],
            "counts": {"found": 0, "cached": 0, "enriched": 0, "needs_research": 0},
            "export_file": "",
        }
        job_agent.write_job(job)
        self._job_files_to_remove.append(job_agent.job_path(job_id))
        return job_id

    def test_live_page_has_new_copy_and_no_retired_phrases(self):
        job_id = self.write_fake_job(owner_email="user1@example.com", owner_username="user1")
        self.login(self.client, "user1")

        resp = self.client.get(f"/lead-bot/live/{job_id}")
        self.assertEqual(resp.status_code, 200)

        self.assertIn("contact details are checked", resp.text)
        self.assertIn("checked", resp.text)
        self.assertIn("with contact info", resp.text)

        for phrase in RETIRED_VISIBLE_PHRASES:
            self.assertNotIn(phrase, resp.text, f"retired phrase still present: {phrase!r}")

    def test_live_page_internal_enriched_id_and_field_unaffected(self):
        job_id = self.write_fake_job(owner_email="user1@example.com", owner_username="user1")
        self.login(self.client, "user1")

        resp = self.client.get(f"/lead-bot/live/{job_id}")
        self.assertIn('id="enriched"', resp.text)
        self.assertIn("counts.enriched", resp.text)


if __name__ == "__main__":
    unittest.main()
