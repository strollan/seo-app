"""Focused export-management UX and security regression coverage.

All files are created under a temporary working directory. Tests call route
functions directly and never contact production or start a Lead Finder scan.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

import app.main as appmain
import agents.lead_dashboard_agent as dashboard


class ExportManagementTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.old_cwd = Path.cwd()
        os.chdir(self.tempdir.name)
        self.addCleanup(os.chdir, self.old_cwd)
        Path("exports").mkdir()
        Path("data").mkdir()
        self.user1 = {
            "role": "standard",
            "username": "user1",
            "email": "user1@example.test",
        }
        self.user2 = {
            "role": "standard",
            "username": "user2",
            "email": "user2@example.test",
        }

    def make_export(self, name, owner, *, partial=False, keyword="plumber", market="Albany, NY"):
        path = Path("exports") / name
        path.write_text(
            f'keyword,market,title,domain\n{keyword},"{market}",Example,example.test\n',
            encoding="utf-8",
        )
        (Path("exports") / f"{name}.owner.json").write_text(
            json.dumps({"owner_username": owner}),
            encoding="utf-8",
        )
        owner_map_path = Path("data/leadbot_export_owners.json")
        owner_map = (
            json.loads(owner_map_path.read_text(encoding="utf-8"))
            if owner_map_path.exists()
            else {}
        )
        owner_map[name] = {"owner_username": owner, "partial": partial}
        owner_map_path.write_text(json.dumps(owner_map), encoding="utf-8")
        return path

    def call_delete(self, filename, user=None, csrf_valid=True):
        with (
            mock.patch.object(appmain, "auth_current_user", return_value=user or self.user1),
            mock.patch.object(appmain, "_csrf_token_valid", return_value=csrf_valid),
        ):
            return appmain.leadbot_delete_export(filename, object(), csrf_token="token")

    def test_user_sees_only_own_exports_with_metadata_and_partial_state(self):
        own = self.make_export("own.csv", "user1", partial=True)
        other = self.make_export("other.csv", "user2")

        visible = dashboard.latest_csvs(current_user=self.user1)
        self.assertIn(own, visible)
        self.assertNotIn(other, visible)

        page = dashboard.render_lead_dashboard(current_user=self.user1, csrf_token="token")
        self.assertEqual(dashboard.export_display_label(own), "Plumber · Albany, NY")
        self.assertIn("Plumber", page)
        self.assertIn("export-file-date", page)
        self.assertIn(">Partial</span>", page)
        self.assertIn("/lead-bot/export/own.csv", page)
        self.assertNotIn("other.csv", page)

    def test_download_ownership_is_enforced(self):
        own = self.make_export("own.csv", "user1")
        other = self.make_export("other.csv", "user2")

        with mock.patch.object(appmain, "auth_current_user", return_value=self.user1):
            allowed = appmain.lead_bot_export(own.name, object())
            denied = appmain.lead_bot_export(other.name, object())

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(denied.status_code, 403)

    def test_delete_requires_csrf_and_ownership(self):
        own = self.make_export("own.csv", "user1")
        other = self.make_export("other.csv", "user2")

        self.assertEqual(self.call_delete(own.name, csrf_valid=False).status_code, 403)
        self.assertTrue(own.exists())
        self.assertEqual(self.call_delete(other.name).status_code, 403)
        self.assertTrue(other.exists())

    def test_delete_removes_only_owned_export_and_metadata(self):
        own = self.make_export("own.csv", "user1")
        own_sidecar = Path("exports/own.csv.owner.json")
        other = self.make_export("other.csv", "user2")

        response = self.call_delete(own.name)

        self.assertEqual(response.status_code, 303)
        self.assertFalse(own.exists())
        self.assertFalse(own_sidecar.exists())
        self.assertTrue(other.exists())
        owner_map = json.loads(Path("data/leadbot_export_owners.json").read_text())
        self.assertNotIn("own.csv", owner_map)
        self.assertIn("other.csv", owner_map)

    def test_related_file_is_rechecked_for_ownership(self):
        base = self.make_export("shared.csv", "user1")
        enriched = self.make_export("shared_enriched.csv", "user2")

        self.assertEqual(self.call_delete(base.name).status_code, 303)
        self.assertFalse(base.exists())
        self.assertTrue(enriched.exists())

    def test_traversal_and_non_csv_names_are_rejected(self):
        own = self.make_export("own.csv", "user1")

        self.assertEqual(self.call_delete("../../own.csv").status_code, 400)
        self.assertEqual(self.call_delete("own.txt").status_code, 400)
        self.assertTrue(own.exists())

    def test_missing_export_fails_gracefully(self):
        response = self.call_delete("missing.csv")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/lead-bot?deleted=0")

    def test_confirmation_and_mobile_safeguards_are_present(self):
        page = dashboard.render_lead_dashboard(current_user=self.user1, csrf_token="token")
        self.assertIn(
            'window.confirm("Delete this export and its saved metadata? This cannot be undone.")',
            page,
        )
        self.assertIn("overflow-wrap: anywhere !important", page)
        self.assertIn("@media (max-width: 700px)", page)
        self.assertIn("grid-template-columns: 1fr !important", page)
        self.assertIn("min-height: 44px !important", page)


if __name__ == "__main__":
    unittest.main()
