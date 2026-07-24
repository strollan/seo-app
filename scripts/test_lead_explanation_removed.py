"""
Regression tests for the removal of the user-facing "Why this lead"
explanation feature from Lead Finder result cards, CSV exports, and the
underlying scoring / live-scan data path.

Reason for removal (per task spec): the generated explanation text was
redundant, often inaccurate, too long, and boring. While removing it, a
latent bug was found and confirmed in the old
agents/lead_finding_agent.py::score_lead(): it called
build_lead_reason(scored if "scored" in locals() else lead if "lead" in
locals() else item) -- neither "scored" nor "lead" was ever a local name in
that function, so the check always fell through to `item`, and the
resulting string was then misused as the *separator* argument to
str.join() on the `reasons` list, producing long, repetitive, sometimes
nonsensical text. This validates removing the feature outright rather than
trying to fix/rewrite it (which the task explicitly disallows).

These tests prove:
  - score_lead() no longer returns a "reason" key, and every other scoring
    value (score, seo_opportunity_score, status, flags) is unchanged for
    known inputs
  - lead_to_public() no longer emits "reason", and a legacy lead dict that
    still has a "reason" key (as if read from a job file written before
    this change) does not crash it
  - EXPORT_FIELDS / export_leads_to_csv() no longer produce a "reason"
    column, and a legacy source dict with a "reason" key does not break
    the CSV write
  - lead_cards() (dashboard renderer) no longer emits "Why this lead" text,
    an empty ".reason" wrapper, or the .reason CSS class at all, while
    normal card info (domain, phone, email, address, score) still renders
  - an old CSV export that still has a "reason" column loads through
    read_csv_rows()/lead_cards() without error and without showing the
    legacy explanation text, and remains downloadable via
    /lead-bot/export/{filename}
  - a real end-to-end run_job() scan (no network) produces a completed job
    whose public leads have no "reason" key and whose CSV export has no
    "reason" column
  - a completed live-scan job file that already has a legacy "reason" key
    on one of its stored leads (simulating a job written before this
    change) still polls successfully through
    /lead-bot/live-status/{job_id} with no error

agents.lead_finding_agent.find_leads is monkeypatched in the live-job test
so no real Serper/DataForSEO call is ever made. All other tests call the
scoring/export/render functions directly with hand-built dicts and make no
network calls. Real files written under exports/ and
data/leadbot_live_jobs/ are removed in cleanup.
"""

import csv
import json
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

from fastapi.testclient import TestClient

import app.main as appmain
import agents.auth_agent as auth_agent
import agents.lead_finding_agent as finding_agent
import agents.lead_live_job_agent as job_agent
import agents.lead_export_agent as export_agent
import agents.lead_dashboard_agent as dash_agent

VALID_PASSWORD = "correct-horse-battery-staple"


class ScoreLeadNoReasonFieldTests(unittest.TestCase):
    """score_lead() must no longer produce a "reason" key, and every other
    piece of scoring logic (score math, flags, status, seo_opportunity_score)
    must be unchanged."""

    def test_plain_result_has_no_reason_key_and_expected_score(self):
        item = {
            "title": "Acme Roofing Co",
            "url": "https://acmeroofing.example",
            "domain": "acmeroofing.example",
            "snippet": "Acme Roofing is a licensed roofing contractor.",
            "serp_page": 1,
            "serp_position": 5,
        }
        result = finding_agent.score_lead(item, industry="roofing", market="albany")

        self.assertNotIn("reason", result)
        # Base 50 + direct-domain (+10) + service-business term "contractor" (+5)
        # + industry good-term hits ("roof","roofing" both substring-match the
        # snippet/title text, min(30, 2*8)=16) = 81
        self.assertEqual(result["score"], 81)
        self.assertEqual(result["flags"], [])
        self.assertEqual(result["status"], "strong")

    def test_directory_hit_still_penalizes_score_with_no_reason_key(self):
        item = {
            "title": "Acme Roofing on Yelp",
            "url": "https://yelp.com/biz/acme-roofing",
            "domain": "yelp.com",
            "snippet": "Reviews and directory listing for Acme Roofing.",
        }
        result = finding_agent.score_lead(item, industry="roofing", market="")

        self.assertNotIn("reason", result)
        self.assertIn("directory_or_social", result["flags"])
        # Base 50 - directory hit (-50) + industry good-term hits ("roof",
        # "roofing" both substring-match, min(30, 2*8)=16) = 16
        # (yelp.com is excluded from the direct-domain bonus)
        self.assertEqual(result["score"], 16)
        self.assertEqual(result["status"], "weak")

    def test_serp_position_sweet_spot_still_boosts_opportunity_score(self):
        item = {
            "title": "Acme Roofing Co",
            "url": "https://acmeroofing.example",
            "domain": "acmeroofing.example",
            "snippet": "Full service roofing company.",
            "serp_page": 2,
            "serp_position": 20,
        }
        result = finding_agent.score_lead(item, industry="roofing", market="")

        self.assertNotIn("reason", result)
        # Base score: 50 + direct-domain(+10) + "company" business term(+5)
        # + industry good-term hits ("roof","roofing", min(30,2*8)=16) = 81
        # Sweet-spot serp_position 20 (11-40): seo_opportunity_score = 81 + 20
        # = 101, clamped to 100
        self.assertEqual(result["score"], 81)
        self.assertEqual(result["seo_opportunity_score"], 100)


class LeadToPublicNoReasonFieldTests(unittest.TestCase):
    """lead_to_public() must not emit "reason", and a legacy lead dict that
    still carries a "reason" key (as if loaded from an old job file) must
    not break it."""

    def test_new_style_lead_has_no_reason_key(self):
        lead = {
            "title": "Acme Roofing Co",
            "domain": "acmeroofing.example",
            "url": "https://acmeroofing.example",
            "best_phone": "555-555-0100",
            "emails": "owner@acmeroofing.example",
            "outreach_status": "call_ready",
            "contact_confidence": 80,
            "final_lead_score": 90,
        }
        public = job_agent.lead_to_public(lead)

        self.assertIsInstance(public, dict)
        self.assertNotIn("reason", public)
        self.assertEqual(public["domain"], "acmeroofing.example")

    def test_legacy_lead_with_reason_key_does_not_crash_and_drops_reason(self):
        legacy_lead = {
            "title": "Acme Roofing Co",
            "domain": "acmeroofing.example",
            "url": "https://acmeroofing.example",
            "best_phone": "555-555-0100",
            "emails": "owner@acmeroofing.example",
            "outreach_status": "call_ready",
            "contact_confidence": 80,
            "final_lead_score": 90,
            "reason": "Has a direct business domain. Matches industry signals: roofing.",
        }
        public = job_agent.lead_to_public(legacy_lead)

        self.assertIsInstance(public, dict)
        self.assertNotIn("reason", public)
        self.assertEqual(public["domain"], "acmeroofing.example")


class ExportCsvNoReasonColumnTests(unittest.TestCase):
    """New CSV exports must not contain a "reason" column, and a legacy
    source lead dict with a "reason" key must not break the export."""

    def setUp(self):
        self._written_paths = []

    def tearDown(self):
        for p in self._written_paths:
            try:
                Path(p).unlink()
            except FileNotFoundError:
                pass

    def test_export_fields_has_no_reason(self):
        self.assertNotIn("reason", export_agent.EXPORT_FIELDS)

    def test_csv_export_has_no_reason_column_and_survives_legacy_reason_key(self):
        result = {
            "leads": [
                {
                    "domain": "acmeroofing.example",
                    "title": "Acme Roofing Co",
                    "url": "https://acmeroofing.example",
                    "best_phone": "555-555-0100",
                    "emails": "owner@acmeroofing.example",
                    "outreach_status": "call_ready",
                    "score": 90,
                    "final_lead_score": 90,
                    "reason": "This legacy field should never reach the CSV.",
                }
            ]
        }

        export_result = export_agent.export_leads_to_csv(
            result, industry="roofing", market="albany", only_outreach_ready=True
        )
        path = Path(export_result["path"])
        self._written_paths.append(path)

        self.assertTrue(path.exists())

        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            self.assertNotIn("reason", reader.fieldnames)
            rows = list(reader)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["domain"], "acmeroofing.example")
        self.assertNotIn("reason", rows[0])


class LeadCardsDashboardRenderingTests(unittest.TestCase):
    """lead_cards() must not render "Why this lead" heading/text, the
    .reason CSS class, or any leftover empty wrapper -- while normal card
    fields still render correctly. Covers both a new-style row and a
    legacy row that still has a "reason" value (as if read from an old
    CSV export)."""

    def _rows(self, extra=None):
        row = {
            "domain": "acmeroofing.example",
            "title": "Acme Roofing Co",
            "url": "https://acmeroofing.example",
            "best_phone": "555-555-0100",
            "emails": "owner@acmeroofing.example",
            "outreach_status": "call_ready",
            "score": "90",
            "final_lead_score": "90",
            "address": "123 Main St, Albany, NY",
        }
        if extra:
            row.update(extra)
        return [row]

    def test_new_style_row_has_no_why_this_lead_or_reason_class(self):
        html_out = dash_agent.lead_cards(self._rows(), selected_name="test.csv", csrf_token="tok")

        self.assertNotIn("Why this lead", html_out)
        self.assertNotIn("Why This Lead", html_out)
        self.assertNotIn('class="reason"', html_out)
        # Normal card info must still be present.
        self.assertIn("acmeroofing.example", html_out)
        self.assertIn("Acme Roofing Co", html_out)
        self.assertIn("555-555-0100", html_out)

    def test_legacy_row_with_reason_value_is_ignored_not_rendered(self):
        legacy_text = "Legacy explanation text that must never render again."
        html_out = dash_agent.lead_cards(
            self._rows(extra={"reason": legacy_text}),
            selected_name="test.csv",
            csrf_token="tok",
        )

        self.assertNotIn("Why this lead", html_out)
        self.assertNotIn(legacy_text, html_out)
        self.assertNotIn('class="reason"', html_out)
        self.assertIn("acmeroofing.example", html_out)


class OldCsvBackwardCompatibilityTests(unittest.TestCase):
    """An old export CSV that still has a "reason" column (written before
    this change) must still load and render without error, without
    surfacing the legacy explanation text, and must remain downloadable
    byte-for-byte."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        self.auth_db_path = Path(self.tmpdir.name) / "test_auth.db"
        db_patch = mock.patch.object(auth_agent, "AUTH_DB", self.auth_db_path)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        auth_agent.init_auth_db()

        self.username = f"legacycsvtest_{uuid.uuid4().hex[:10]}"
        auth_agent.create_user(self.username, VALID_PASSWORD, role="standard", email=f"{self.username}@example.com")

        self.client = TestClient(appmain.app)
        user = auth_agent.get_user_by_username(self.username)
        token = auth_agent.create_session(user)
        self.client.cookies.set(appmain.AUTH_COOKIE_NAME, token)

        self.export_dir = Path("exports")
        self.export_dir.mkdir(exist_ok=True)
        self.filename = f"test_legacy_reason_{uuid.uuid4().hex}.csv"
        self.path = self.export_dir / self.filename
        self.owner_sidecar = self.export_dir / f"{self.filename}.owner.json"

        self.legacy_reason_text = "Legacy pre-removal explanation text."
        with self.path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["domain", "title", "best_phone", "reason"])
            writer.writeheader()
            writer.writerow({
                "domain": "legacy-lead.example",
                "title": "Legacy Lead Biz",
                "best_phone": "555-555-0199",
                "reason": self.legacy_reason_text,
            })
        self.owner_sidecar.write_text(json.dumps({"owner_username": self.username}), encoding="utf-8")

        self.addCleanup(self._cleanup_files)

    def _cleanup_files(self):
        for p in (self.path, self.owner_sidecar):
            try:
                p.unlink()
            except FileNotFoundError:
                pass

    def test_old_csv_loads_via_read_csv_rows_without_error(self):
        rows = dash_agent.read_csv_rows(self.path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["domain"], "legacy-lead.example")
        self.assertEqual(rows[0]["reason"], self.legacy_reason_text)

    def test_old_csv_renders_via_cards_route_without_legacy_text(self):
        resp = self.client.get(f"/lead-bot/cards/{self.filename}")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("Why this lead", resp.text)
        self.assertNotIn(self.legacy_reason_text, resp.text)
        self.assertNotIn('class="reason"', resp.text)
        self.assertIn("legacy-lead.example", resp.text)

    def test_old_csv_remains_downloadable(self):
        resp = self.client.get(f"/lead-bot/export/{self.filename}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("legacy-lead.example", resp.text)
        self.assertIn(self.legacy_reason_text, resp.text)


CANNED_LEAD = {
    "domain": "livejob-explanation-test.example",
    "url": "https://livejob-explanation-test.example",
    "title": "Explanation Test Plumbing",
    "best_phone": "555-555-0111",
    "emails": "owner@livejob-explanation-test.example",
    "outreach_status": "call_ready",
    "contact_confidence": 80,
    "final_lead_score": 90,
}


def fake_find_leads_with_lead(industry, market, service_keyword=None, own_domain=None, limit=10):
    return {"leads": [dict(CANNED_LEAD)]}


OWNER_INDEX_PATH = Path("data/leadbot_export_owners.json")


class LiveScanEndToEndNoReasonTests(unittest.TestCase):
    """A real create_job()/run_job() pass (background thread, no network)
    must produce a completed job whose public leads have no "reason" key
    and whose CSV export has no "reason" column.

    The market string includes a unique per-run suffix: run_job() names the
    export "leads_{industry-or-'leadbot'}_{market-slug}_{second-precision-
    timestamp}.csv", so a plain "Albany, NY" (used by other suites' live-job
    tests too) risks an exact filename collision -- and therefore a
    corrupted shared data/leadbot_export_owners.json entry -- if two such
    jobs finish within the same wall-clock second during a full-suite run.
    """

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

    def test_completed_scan_has_no_reason_anywhere(self):
        unique_market = f"Explanationtest {uuid.uuid4().hex[:8]}, NY"
        with mock.patch("agents.lead_finding_agent.find_leads", side_effect=fake_find_leads_with_lead):
            job_id = job_agent.create_job({
                "industry": "",
                "market": unique_market,
                "keyword": "plumber",
                "own_domain": "",
                "limit": 1,
                "per_batch": 1,
                "per_query_limit": 1,
                "max_queries": 1,
                "owner_email": "livejobtest@example.com",
                "owner_username": "livejobtest",
                "owner_role": "standard",
            })
            self._created_job_ids.append(job_id)

            deadline = time.time() + 10
            job = None
            while time.time() < deadline:
                job = job_agent.read_job(job_id)
                if job and job.get("status") in {"done", "error", "cancelled"}:
                    break
                time.sleep(0.05)

        self.assertIsNotNone(job, "job file was never written")
        self.assertEqual(job.get("status"), "done", f"job did not finish cleanly: {job}")

        leads = job.get("leads") or []
        self.assertTrue(leads, "expected at least one lead in the completed job")
        for lead in leads:
            self.assertNotIn("reason", lead)

        export_file = job.get("export_file") or ""
        self.assertTrue(export_file)
        self._created_export_names.append(export_file)

        export_path = Path("exports") / export_file
        with export_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            self.assertNotIn("reason", reader.fieldnames)


class LiveStatusLegacyJobCompatibilityTests(unittest.TestCase):
    """A job file written as if before this change (one stored lead still
    has a "reason" key) must still poll successfully through
    /lead-bot/live-status/{job_id} with no error."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        self.auth_db_path = Path(self.tmpdir.name) / "test_auth.db"
        db_patch = mock.patch.object(auth_agent, "AUTH_DB", self.auth_db_path)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        auth_agent.init_auth_db()

        self.username = f"legacyjobtest_{uuid.uuid4().hex[:10]}"
        auth_agent.create_user(self.username, VALID_PASSWORD, role="standard", email=f"{self.username}@example.com")

        self.client = TestClient(appmain.app)
        user = auth_agent.get_user_by_username(self.username)
        token = auth_agent.create_session(user)
        self.client.cookies.set(appmain.AUTH_COOKIE_NAME, token)

        self.job_id = f"legacy-{uuid.uuid4().hex}"
        self.addCleanup(self._cleanup_job)

        legacy_job = {
            "job_id": self.job_id,
            "status": "done",
            "message": "Scan complete.",
            "leads": [
                {
                    "domain": "legacy-job-lead.example",
                    "title": "Legacy Job Lead Biz",
                    "best_phone": "555-555-0177",
                    "reason": "Legacy reason text stored before this change.",
                }
            ],
            "counts": {"found": 1},
            "params": {"keyword": "plumber", "market": "Albany, NY"},
            "guest_id": "",
        }
        job_agent.write_job(legacy_job)

    def _cleanup_job(self):
        try:
            job_agent.job_path(self.job_id).unlink()
        except FileNotFoundError:
            pass

    def test_legacy_job_polls_successfully(self):
        resp = self.client.get(f"/lead-bot/live-status/{self.job_id}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data.get("status"), "done")
        self.assertEqual(len(data.get("leads") or []), 1)
        self.assertEqual(data["leads"][0]["domain"], "legacy-job-lead.example")


if __name__ == "__main__":
    unittest.main()
