"""Focused Lead Finder personal/global blocklist tests.

Every database, job, and export fixture lives in a temporary directory.
No HTTP TestClient, provider call, production scan, or production write occurs.
"""

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

import app.main as appmain
import agents.lead_blocked_domain_db_agent as block_db
import agents.lead_dashboard_agent as dashboard
import agents.lead_finding_agent as finding
import agents.lead_live_job_agent as live
import agents.leadbot_block_gate as gate


class BlocklistTempCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.old_cwd = Path.cwd()
        os.chdir(self.tempdir.name)
        self.addCleanup(os.chdir, self.old_cwd)
        Path("data").mkdir()
        Path("exports").mkdir()
        self.db_patch = mock.patch.object(
            block_db, "DB_PATH", Path("data/leadbot_blocked_domains.sqlite")
        )
        self.text_patch = mock.patch.object(
            block_db, "TEXT_FILE", Path("data/leadbot_blocked_domains.txt")
        )
        self.job_patch = mock.patch.object(live, "JOB_DIR", Path("data/jobs"))
        self.db_patch.start()
        self.text_patch.start()
        self.job_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.text_patch.stop)
        self.addCleanup(self.job_patch.stop)
        live.JOB_DIR.mkdir()
        self.user1 = {
            "role": "standard", "username": "user1", "email": "user1@example.test"
        }
        self.user2 = {
            "role": "standard", "username": "user2", "email": "user2@example.test"
        }
        self.admin = {
            "role": "admin", "username": "admin", "email": "admin@example.test"
        }

    def route(self, function, user, domain, *, csrf=True):
        with (
            mock.patch.object(appmain, "auth_current_user", return_value=user),
            mock.patch.object(appmain, "_csrf_token_valid", return_value=csrf),
        ):
            return function(object(), domain=domain, csrf_token="token")


class DomainNormalizationTests(BlocklistTempCase):
    def test_www_url_and_path_normalize_consistently(self):
        expected = "example.com"
        for value in (
            "example.com",
            "www.example.com",
            "https://example.com/",
            "https://www.example.com/contact",
        ):
            self.assertEqual(gate.normalize_domain(value), expected)

    def test_matching_is_domain_boundary_safe(self):
        self.assertTrue(gate.domain_matches_blocked("www.example.com", "example.com"))
        self.assertTrue(gate.domain_matches_blocked("shop.example.com", "example.com"))
        self.assertFalse(gate.domain_matches_blocked("badexample.com", "example.com"))
        self.assertFalse(gate.domain_matches_blocked("example.com.evil.test", "example.com"))

    def test_invalid_values_are_rejected(self):
        for value in ("", "not a domain", "/etc/passwd", "localhost"):
            self.assertFalse(gate.is_valid_block_domain(value))


class ScopeAndRouteSecurityTests(BlocklistTempCase):
    def test_personal_scope_isolated_and_unblock_restores_eligibility(self):
        key1 = gate.blocklist_owner_key(self.user1)
        key2 = gate.blocklist_owner_key(self.user2)
        self.assertTrue(block_db.add_user_blocked_domain(key1, "example.com"))
        self.assertIn("example.com", gate.load_effective_blocked_domains(key1))
        self.assertNotIn("example.com", gate.load_effective_blocked_domains(key2))
        self.assertTrue(block_db.remove_user_blocked_domain(key1, "example.com"))
        self.assertNotIn("example.com", gate.load_effective_blocked_domains(key1))

    def test_global_scope_affects_every_user(self):
        gate.add_main_blocked_domain("global.example", source="test")
        for user in (self.user1, self.user2, self.admin):
            self.assertIn(
                "global.example",
                gate.load_effective_blocked_domains(gate.blocklist_owner_key(user)),
            )

    def test_user_routes_never_accept_another_owner_identity(self):
        self.route(appmain.leadbot_user_block_add, self.user2, "other.example")
        response = self.route(
            appmain.leadbot_user_block_remove, self.user1, "other.example"
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            block_db.list_user_blocked_domains(gate.blocklist_owner_key(self.user2)),
            ["other.example"],
        )

    def test_normal_user_cannot_modify_global_blocklist(self):
        response = self.route(
            appmain.leadbot_global_block_add, self.user1, "global.example"
        )
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("global.example", gate.load_main_blocked_domains())

    def test_csrf_and_authentication_are_enforced(self):
        denied = self.route(
            appmain.leadbot_user_block_add, self.user1, "example.com", csrf=False
        )
        self.assertEqual(denied.status_code, 403)
        unauthenticated = self.route(
            appmain.leadbot_user_block_add, None, "example.com"
        )
        self.assertEqual(unauthenticated.status_code, 401)

    def test_admin_can_add_view_and_remove_global_entry(self):
        added = self.route(
            appmain.leadbot_global_block_add, self.admin, "https://www.global.example/a"
        )
        self.assertEqual(added.status_code, 303)
        self.assertIn("global.example", gate.load_main_blocked_domains())
        admin_page = dashboard.render_blocklist_panel(self.admin, "token")
        self.assertIn("Master Blocklist", admin_page)
        self.assertIn("global.example", admin_page)
        removed = self.route(
            appmain.leadbot_global_block_remove, self.admin, "global.example"
        )
        self.assertEqual(removed.status_code, 303)
        self.assertNotIn("global.example", gate.load_main_blocked_domains())

    def test_standard_user_sees_only_personal_list(self):
        block_db.add_user_blocked_domain(
            gate.blocklist_owner_key(self.user1), "mine.example"
        )
        block_db.add_user_blocked_domain(
            gate.blocklist_owner_key(self.user2), "theirs.example"
        )
        page = dashboard.render_blocklist_panel(self.user1, "token")
        self.assertIn("mine.example", page)
        self.assertNotIn("theirs.example", page)
        self.assertNotIn("Master Blocklist", page)

    def test_card_block_action_is_deliberate_csrf_post(self):
        page = dashboard.render_lead_dashboard(
            current_user=self.user1, csrf_token="token"
        )
        self.assertIn('block.href = "/lead-bot/blocklist/user/add"', page)
        self.assertIn('method: "POST"', page)
        self.assertIn("csrf_token: window.LEADBOT_CSRF_TOKEN", page)
        self.assertIn("window.confirm(", page)
        self.assertNotIn('method: "GET"', page[
            page.index("LEADBOT DASHBOARD NO CONFIRM BLOCK START"):
            page.index("LEADBOT DASHBOARD NO CONFIRM BLOCK END")
        ])


class PipelineTests(BlocklistTempCase):
    def test_blocked_candidate_is_rejected_before_scoring_and_contact_crawl(self):
        rows = [
            {
                "title": "Blocked",
                "domain": "www.example.com",
                "url": "https://www.example.com/contact",
                "serp_position": 11,
                "serp_page": 2,
            },
            {
                "title": "Allowed",
                "domain": "allowed.example",
                "url": "https://allowed.example",
                "serp_position": 12,
                "serp_page": 2,
            },
        ]
        scored = []
        crawled = []
        original_score = finding.score_lead

        def score(item, industry, market):
            scored.append(item["domain"])
            return original_score(item, industry, market)

        def contact(url, market=""):
            crawled.append(url)
            return {
                "best_phone": "555-0100", "phones": ["555-0100"],
                "emails": [], "contact_page_url": "", "confidence": 75, "flags": [],
            }

        with (
            mock.patch.object(finding, "find_business_competitors", return_value=rows),
            mock.patch.object(finding, "score_lead", side_effect=score),
            mock.patch.object(finding, "extract_contact_from_url", side_effect=contact),
        ):
            result = finding.find_leads(
                "plumber", "Albany, NY", limit=1, blocked_domains={"example.com"}
            )

        self.assertEqual([lead["domain"] for lead in result["leads"]], ["allowed.example"])
        self.assertEqual(scored, ["allowed.example"])
        self.assertEqual(crawled, ["https://allowed.example"])

    def test_central_persistence_guard_filters_partial_jobs(self):
        block_db.add_user_blocked_domain(
            gate.blocklist_owner_key(self.user1), "blocked.example"
        )
        job = {
            "job_id": "partial-test",
            "status": "running",
            "params": {
                "owner_email": self.user1["email"],
                "owner_username": self.user1["username"],
            },
            "leads": [
                {"domain": "blocked.example", "url": "https://blocked.example"},
                {"domain": "allowed.example", "url": "https://allowed.example"},
            ],
            "counts": {"found": 2, "enriched": 0, "needs_research": 2},
        }
        live.write_job(job)
        saved = live.read_job("partial-test")
        self.assertEqual([row["domain"] for row in saved["leads"]], ["allowed.example"])
        self.assertEqual(saved["counts"]["found"], 1)

    def test_blocked_rows_cannot_enter_new_export_payload(self):
        rows = [
            {"domain": "blocked.example", "url": "https://blocked.example"},
            {"domain": "allowed.example", "url": "https://allowed.example"},
        ]
        filtered = live._leadbot_filter_new_export_rows(rows, {"blocked.example"})
        self.assertEqual([row["domain"] for row in filtered], ["allowed.example"])

    def test_legacy_export_rows_remain_readable(self):
        gate.add_main_blocked_domain("blocked.example", source="test")
        cards = dashboard.lead_cards(
            [{
                "title": "Historical Result",
                "domain": "blocked.example",
                "url": "https://blocked.example",
                "serp_position": "11",
                "serp_page": "2",
            }],
            selected_name="historical.csv",
            csrf_token="token",
        )
        self.assertIn("Historical Result", cards)
        why = re.search(
            r'<div class="leadbot-why-note">(.*?)</div>', cards, re.DOTALL
        ).group(1)
        self.assertEqual(len(re.findall(r"<p>.*?</p>", why, re.DOTALL)), 4)


if __name__ == "__main__":
    unittest.main()
