"""
Regression tests for a bug where a completed Lead Finder live-scan export
was never tagged with an owner.

agents/lead_live_job_agent.py::run_job() writes the scan's export CSV in a
background thread with no HTTP request object, so the existing HTTP-path-only
helper (app.main.leadbot_mark_export_owner, which derives identity from a
request) was never reached for that path. Standard users were then denied
visibility into their own completed exports
(agents/lead_dashboard_agent.py::_leadbot_export_visible_to_user hides any
export with no recorded owner from non-admins), while admins -- who bypass
ownership checks entirely -- could see them fine.

The fix adds agents/lead_dashboard_agent.py::record_export_owner(), which
writes the same data/leadbot_export_owners.json format using owner identity
already present in the job's params (no request object required), and calls
it from run_job() right after a successful export.

agents.lead_finding_agent.find_leads is monkeypatched so these tests make no
real Serper/DataForSEO calls and trigger no paid scans. Real job files and
export CSVs are written under the real data/leadbot_live_jobs/ and exports/
directories (run_job() hardcodes these paths), and are removed in cleanup.
Any entry these tests add to the real data/leadbot_export_owners.json is
removed by key in cleanup, leaving pre-existing entries untouched.
"""

import json
import sys
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os
os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

import agents.lead_live_job_agent as job_agent
import agents.lead_dashboard_agent as dash_agent

OWNER_INDEX_PATH = Path("data/leadbot_export_owners.json")

CANNED_LEAD = {
    "domain": "leadbot-ownership-test.example",
    "url": "https://leadbot-ownership-test.example",
    "title": "Ownership Test Plumbing",
    "best_phone": "555-555-0100",
    "emails": "owner@leadbot-ownership-test.example",
    "outreach_status": "call_ready",
    "contact_confidence": 80,
    "final_lead_score": 90,
}


def fake_find_leads_with_lead(industry, market, service_keyword=None, own_domain=None, limit=10):
    return {"leads": [dict(CANNED_LEAD)]}


def fake_find_leads_empty(industry, market, service_keyword=None, own_domain=None, limit=10):
    return {"leads": []}


class LiveJobExportOwnershipTestCase(unittest.TestCase):
    """Runs a real create_job()/run_job() pass (background thread, no
    network) and tracks generated files for cleanup."""

    def setUp(self):
        self._created_job_ids = []
        self._created_export_names = []

    def tearDown(self):
        for job_id in self._created_job_ids:
            try:
                job_agent.job_path(job_id).unlink()
            except FileNotFoundError:
                pass

        for export_name in self._created_export_names:
            export_path = Path("exports") / export_name
            sidecar_path = Path("exports") / f"{export_name}.owner.json"
            for p in (export_path, sidecar_path):
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass

            if OWNER_INDEX_PATH.exists():
                try:
                    data = json.loads(OWNER_INDEX_PATH.read_text(encoding="utf-8") or "{}")
                except Exception:
                    data = {}
                if export_name in data:
                    data.pop(export_name, None)
                    OWNER_INDEX_PATH.write_text(
                        json.dumps(data, indent=2, sort_keys=True), encoding="utf-8"
                    )

    def run_job_and_wait(self, params, timeout_seconds=10):
        job_id = job_agent.create_job(params)
        self._created_job_ids.append(job_id)

        deadline = time.time() + timeout_seconds
        job = None
        while time.time() < deadline:
            job = job_agent.read_job(job_id)
            if job and job.get("status") in {"done", "error", "cancelled"}:
                break
            time.sleep(0.05)

        self.assertIsNotNone(job, "job file was never written")
        self.assertEqual(job.get("status"), "done", f"job did not finish cleanly: {job}")

        export_file = job.get("export_file") or ""
        if export_file:
            self._created_export_names.append(export_file)

        return job


class RecordsOwnershipOnCompletionTests(LiveJobExportOwnershipTestCase):
    def test_completed_scan_records_ownership(self):
        with mock.patch("agents.lead_finding_agent.find_leads", side_effect=fake_find_leads_with_lead):
            job = self.run_job_and_wait({
                "industry": "",
                "market": "Albany, NY",
                "keyword": "plumber",
                "own_domain": "",
                "limit": 1,
                "per_batch": 1,
                "per_query_limit": 1,
                "max_queries": 1,
                "owner_email": "standarduser@example.com",
                "owner_username": "standarduser",
                "owner_role": "standard",
            })

        self.assertTrue(job["export_file"])
        self.assertTrue((Path("exports") / job["export_file"]).exists())

        self.assertTrue(OWNER_INDEX_PATH.exists())
        index_data = json.loads(OWNER_INDEX_PATH.read_text(encoding="utf-8"))
        self.assertIn(job["export_file"], index_data)

    def test_ownership_record_contains_correct_identity(self):
        with mock.patch("agents.lead_finding_agent.find_leads", side_effect=fake_find_leads_with_lead):
            job = self.run_job_and_wait({
                "market": "10001",
                "keyword": "plumber",
                "owner_email": "standarduser@example.com",
                "owner_username": "standarduser",
                "owner_role": "standard",
            })

        owner_values = dash_agent._leadbot_export_owner_values(job["export_file"])
        self.assertEqual(owner_values, {"standarduser@example.com", "standarduser"})

    def test_owning_standard_user_can_view_the_export(self):
        with mock.patch("agents.lead_finding_agent.find_leads", side_effect=fake_find_leads_with_lead):
            job = self.run_job_and_wait({
                "market": "Hoboken, NJ 07030",
                "keyword": "plumber",
                "owner_email": "standarduser@example.com",
                "owner_username": "standarduser",
                "owner_role": "standard",
            })

        export_path = Path("exports") / job["export_file"]
        owner = {"username": "standarduser", "email": "standarduser@example.com", "role": "standard"}

        self.assertTrue(dash_agent._leadbot_export_visible_to_user(export_path, current_user=owner))

    def test_unrelated_standard_user_cannot_view_the_export(self):
        with mock.patch("agents.lead_finding_agent.find_leads", side_effect=fake_find_leads_with_lead):
            job = self.run_job_and_wait({
                "market": "Albany, NY",
                "keyword": "plumber",
                "owner_email": "standarduser@example.com",
                "owner_username": "standarduser",
                "owner_role": "standard",
            })

        export_path = Path("exports") / job["export_file"]
        other_user = {"username": "someoneelse", "email": "someoneelse@example.com", "role": "standard"}

        self.assertFalse(dash_agent._leadbot_export_visible_to_user(export_path, current_user=other_user))

    def test_admin_can_view_the_export(self):
        with mock.patch("agents.lead_finding_agent.find_leads", side_effect=fake_find_leads_with_lead):
            job = self.run_job_and_wait({
                "market": "Albany, NY",
                "keyword": "plumber",
                "owner_email": "standarduser@example.com",
                "owner_username": "standarduser",
                "owner_role": "standard",
            })

        export_path = Path("exports") / job["export_file"]
        admin = {"username": "adminuser", "email": "admin@example.com", "role": "admin"}

        self.assertTrue(dash_agent._leadbot_export_visible_to_user(export_path, current_user=admin))


class NoOwnerMetadataCurrentPolicyTests(LiveJobExportOwnershipTestCase):
    def test_scan_with_no_owner_metadata_keeps_current_policy(self):
        """A job with blank owner_email/owner_username (metadata absent)
        must not record an ownership entry, and the resulting export must
        keep today's policy: hidden from standard users, visible to admin."""
        with mock.patch("agents.lead_finding_agent.find_leads", side_effect=fake_find_leads_with_lead):
            job = self.run_job_and_wait({
                "market": "Albany, NY",
                "keyword": "plumber",
                "owner_email": "",
                "owner_username": "",
                "owner_role": "",
            })

        self.assertTrue(job["export_file"])

        index_data = {}
        if OWNER_INDEX_PATH.exists():
            index_data = json.loads(OWNER_INDEX_PATH.read_text(encoding="utf-8") or "{}")
        self.assertNotIn(job["export_file"], index_data)

        export_path = Path("exports") / job["export_file"]
        standard_user = {"username": "standarduser", "email": "standarduser@example.com", "role": "standard"}
        admin = {"username": "adminuser", "email": "admin@example.com", "role": "admin"}

        self.assertFalse(dash_agent._leadbot_export_visible_to_user(export_path, current_user=standard_user))
        self.assertTrue(dash_agent._leadbot_export_visible_to_user(export_path, current_user=admin))


if __name__ == "__main__":
    unittest.main()
