"""Saved-card regressions; all exports and ownership metadata stay in a temp directory.

Run: venv/bin/python scripts/check_leadbot_crm_buttons.py
"""
import csv
import json
import os
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import lead_dashboard_agent as dashboard


class CrmButtons(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.buttons = []
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "button" and "lead-add-crm" in attrs.get("class", "").split():
            self.buttons.append(attrs)


class SavedCrmButtonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        previous = Path.cwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, previous)
        exports = Path("exports")
        exports.mkdir()
        Path("data").mkdir()
        export_patch = patch.object(dashboard, "EXPORT_DIR", exports)
        export_patch.start()
        self.addCleanup(export_patch.stop)
        self.filename = "leads_plumber_albany_20261021_120000.csv"
        with (exports / self.filename).open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["business", "domain", "website"])
            writer.writeheader()
            writer.writerows([
                {"business": "Saved business", "domain": "saved.example", "website": "https://saved.example"},
                {"business": "No domain", "domain": "", "website": ""},
            ])
        Path("data/leadbot_export_owners.json").write_text(json.dumps({
            self.filename: {"owner_username": "owner"},
        }))
        self.admin = {"username": "admin", "role": "admin"}

    def assert_source(self, page):
        buttons = CrmButtons(page).buttons
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0]["data-source-file"], self.filename)
        self.assertEqual(buttons[0]["data-source-domain"], "saved.example")
        self.assertIn(self.filename, json.loads(buttons[0]["data-lead"])["source_note"])
        self.assertIn('id="actiiCrmDialog"', page)

    def test_admin_default_dashboard_uses_resolved_export(self):
        self.assert_source(dashboard.render_lead_dashboard(current_user=self.admin, csrf_token="test"))

    def test_admin_explicit_export(self):
        self.assert_source(dashboard.render_lead_dashboard(self.filename, self.admin, "test"))

    def test_invalid_requested_file_uses_real_fallback_export(self):
        self.assert_source(dashboard.render_lead_dashboard("missing.csv", self.admin, "test"))

    def test_standard_owner_gets_button(self):
        self.assert_source(dashboard.render_lead_dashboard(current_user={"username": "owner", "role": "standard"}))

    def test_guest_and_unrelated_user_get_no_buttons(self):
        for user in (None, {"username": "other", "role": "standard"}):
            with self.subTest(user=user):
                page = dashboard.render_lead_dashboard(self.filename, user)
                self.assertEqual(CrmButtons(page).buttons, [])

    def test_no_export_gets_no_button(self):
        (Path("exports") / self.filename).unlink()
        page = dashboard.render_lead_dashboard(current_user=self.admin)
        self.assertEqual(CrmButtons(page).buttons, [])


if __name__ == "__main__":
    unittest.main()
