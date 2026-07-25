import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from starlette.requests import Request

import app.main as main
import agents.lead_live_job_agent as live


def request(path="/", cookies=None):
    headers = []
    if cookies:
        value = "; ".join(f"{key}={value}" for key, value in cookies.items())
        headers.append((b"cookie", value.encode()))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": headers,
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 1),
            "scheme": "http",
        }
    )


def route_methods(path):
    return {
        method
        for route in main.app.routes
        if getattr(route, "path", None) == path
        for method in getattr(route, "methods", set())
    }


class ImmediateThread:
    def __init__(self, target, **_kwargs):
        self.target = target

    def start(self):
        self.target()


class HeldThread(ImmediateThread):
    targets = []

    def start(self):
        self.targets.append(self.target)


class LiveOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.job_dir = Path(self.temp.name)
        self.job_patch = patch.object(live, "JOB_DIR", self.job_dir)
        self.job_patch.start()
        self.owner = {"email": "owner@example.com", "username": "owner"}
        self.other = {"email": "other@example.com", "username": "other"}
        self.job = {
            "job_id": "0123456789abcdef",
            "status": "running",
            "params": {
                "owner_email": "OWNER@example.com",
                "owner_username": "owner",
            },
            "leads": [],
        }
        (self.job_dir / f"{self.job['job_id']}.json").write_text(
            json.dumps(self.job), encoding="utf-8"
        )

    def tearDown(self):
        self.job_patch.stop()
        self.temp.cleanup()

    def test_normalized_owner_helper_has_no_admin_bypass(self):
        self.assertTrue(live.job_belongs_to_authenticated_user(self.job, self.owner))
        self.assertFalse(
            live.job_belongs_to_authenticated_user(
                self.job, {"email": "admin@example.com", "role": "admin"}
            )
        )

    def test_owner_can_view_poll_and_cancel(self):
        with patch.object(main, "auth_current_user", return_value=self.owner), patch.object(
            main, "_get_or_create_csrf_token", return_value="token"
        ), patch.object(main, "_csrf_token_valid", return_value=True):
            self.assertEqual(main.leadbot_live_page(self.job["job_id"], request()).status_code, 200)
            self.assertEqual(main.leadbot_live_status(self.job["job_id"], request())["status"], "running")
            cancelled = main.leadbot_live_cancel(
                self.job["job_id"], request(), csrf_token="token"
            )
            self.assertEqual(cancelled["status"], "cancelled")

    def test_other_user_gets_same_missing_response_as_unknown_job(self):
        with patch.object(main, "auth_current_user", return_value=self.other):
            view = main.leadbot_live_page(self.job["job_id"], request())
            self.assertEqual(view.status_code, 404)
            other = main.leadbot_live_status(self.job["job_id"], request())
            missing = main.leadbot_live_status("ffffffffffffffff", request())
            self.assertEqual(other.status_code, 404)
            self.assertEqual(other.body, missing.body)
            cancel = main.leadbot_live_cancel(
                self.job["job_id"], request(), csrf_token="anything"
            )
            self.assertEqual(cancel.status_code, 404)
            self.assertEqual(live.read_job(self.job["job_id"])["status"], "running")

    def test_cancel_requires_csrf(self):
        with patch.object(main, "auth_current_user", return_value=self.owner), patch.object(
            main, "_csrf_token_valid", return_value=False
        ):
            response = main.leadbot_live_cancel(self.job["job_id"], request())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(live.read_job(self.job["job_id"])["status"], "running")

    def test_guest_isolation_and_guest_csrf_remain(self):
        guest_job = dict(self.job)
        guest_job["params"] = {"guest_id": "guest-a"}
        (self.job_dir / f"{self.job['job_id']}.json").write_text(
            json.dumps(guest_job), encoding="utf-8"
        )
        cookies = {
            main.GUEST_ID_COOKIE: "guest-a",
            main.GUEST_CSRF_COOKIE: "guest-token",
        }
        with patch.object(main, "auth_current_user", return_value=None):
            self.assertEqual(
                main.leadbot_live_status(
                    self.job["job_id"], request(cookies=cookies)
                )["status"],
                "running",
            )
            denied = main.leadbot_live_status(
                self.job["job_id"],
                request(cookies={main.GUEST_ID_COOKIE: "guest-b"}),
            )
            self.assertEqual(denied["status"], "auth_required")
            no_csrf = main.leadbot_live_cancel(
                self.job["job_id"], request(cookies=cookies)
            )
            self.assertEqual(no_csrf.status_code, 403)


class RouteSecurityTests(unittest.TestCase):
    def setUp(self):
        main._LEADBOT_COMPLETE_DETAILS_RUNNING.clear()
        main._LEADBOT_COMPLETE_DETAILS_COMPLETED.clear()
        HeldThread.targets.clear()

    def tearDown(self):
        main._LEADBOT_COMPLETE_DETAILS_RUNNING.clear()
        main._LEADBOT_COMPLETE_DETAILS_COMPLETED.clear()
        HeldThread.targets.clear()

    def test_mutating_routes_are_post_only(self):
        self.assertEqual(route_methods("/lead-bot/add-domain"), {"POST"})
        self.assertEqual(route_methods("/lead-bot/enrich/{filename}"), {"POST"})
        self.assertEqual(route_methods("/lead-bot/complete-details/{filename}"), {"POST"})
        self.assertEqual(route_methods("/lead-bot/live-cancel/{job_id}"), {"POST"})

    def test_manual_add_requires_csrf_and_escapes_errors(self):
        with patch.object(main, "auth_current_user", return_value={"email": "u@example.com"}), patch.object(
            main, "_csrf_token_valid", return_value=False
        ):
            denied = main.leadbot_real_manual_add_domain(
                request(), domain="example.com"
            )
        self.assertEqual(denied.status_code, 403)

        with patch.object(main, "auth_current_user", return_value={"email": "u@example.com"}), patch.object(
            main, "_csrf_token_valid", return_value=True
        ), patch(
            "agents.lead_manual_add_agent.manual_add_domain",
            side_effect=ValueError("<script>alert(1)</script>"),
        ):
            response = main.leadbot_real_manual_add_domain(
                request(), domain="example.com", csrf_token="token"
            )
        body = response.body.decode()
        self.assertNotIn("<script>", body)
        self.assertIn("&lt;script&gt;", body)

    def test_enrich_requires_csrf_and_export_ownership(self):
        with patch.object(main, "auth_current_user", return_value={"email": "u@example.com"}), patch.object(
            main, "_csrf_token_valid", return_value=False
        ):
            denied = main.leadbot_enrich_this_scan("scan.csv", request())
        self.assertEqual(denied.status_code, 403)

        fake_path = Mock()
        fake_path.name = "scan.csv"
        with patch.object(main, "auth_current_user", return_value={"email": "u@example.com"}), patch.object(
            main, "_csrf_token_valid", return_value=True
        ), patch.object(main, "safe_export_file", return_value=fake_path), patch.object(
            main, "leadbot_user_can_access_export", return_value=False
        ):
            denied = main.leadbot_enrich_this_scan(
                "scan.csv", request(), csrf_token="token"
            )
        self.assertEqual(denied.status_code, 403)

    def _complete_patches(self, thread=ImmediateThread, allowed=True):
        return (
            patch.object(main, "auth_current_user", return_value={"email": "u@example.com"}),
            patch.object(main, "_csrf_token_valid", return_value=True),
            patch.object(main, "leadbot_user_can_access_export", return_value=allowed),
            patch.object(main, "leadbot_fill_missing_addresses"),
            patch.object(main, "leadbot_fill_missing_seo_snapshot"),
            patch.object(main, "leadbot_enrich_this_scan", return_value=Mock(status_code=303)),
            patch.object(main.threading, "Thread", thread),
        )

    def test_complete_details_fails_closed_on_ownership_exception(self):
        with patch.object(main, "auth_current_user", return_value={"email": "u@example.com"}), patch.object(
            main, "_csrf_token_valid", return_value=True
        ), patch.object(
            main, "leadbot_user_can_access_export", side_effect=RuntimeError("lookup failed")
        ):
            response = main.leadbot_complete_details(
                "scan.csv", request(), csrf_token="token"
            )
        self.assertEqual(response.status_code, 403)

    def test_complete_details_requires_csrf(self):
        with patch.object(main, "auth_current_user", return_value={"email": "u@example.com"}), patch.object(
            main, "_csrf_token_valid", return_value=False
        ), patch.object(main, "leadbot_fill_missing_addresses") as fill:
            response = main.leadbot_complete_details("scan.csv", request())
        self.assertEqual(response.status_code, 403)
        fill.assert_not_called()

    def test_duplicate_same_file_is_bounded_and_different_files_are_independent(self):
        patches = self._complete_patches(thread=HeldThread)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            first = main.leadbot_complete_details(
                "one.csv", request(), csrf_token="token"
            )
            duplicate = main.leadbot_complete_details(
                "one.csv", request(), csrf_token="token"
            )
            other = main.leadbot_complete_details(
                "two.csv", request(), csrf_token="token"
            )
        self.assertEqual(first.status_code, 303)
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(other.status_code, 303)
        self.assertEqual(len(HeldThread.targets), 2)

    def test_address_fill_runs_once_and_marker_clears_on_success_or_failure(self):
        patches = self._complete_patches()
        with patches[0], patches[1], patches[2], patches[3] as fill, patches[4], patches[5], patches[6]:
            response = main.leadbot_complete_details(
                "success.csv", request(), csrf_token="token"
            )
        self.assertEqual(response.status_code, 303)
        fill.assert_called_once()
        self.assertNotIn("success.csv", main._LEADBOT_COMPLETE_DETAILS_RUNNING)

        with patch.object(main, "auth_current_user", return_value={"email": "u@example.com"}), patch.object(
            main, "_csrf_token_valid", return_value=True
        ), patch.object(main, "leadbot_user_can_access_export", return_value=True), patch.object(
            main, "leadbot_fill_missing_addresses"
        ), patch.object(main, "leadbot_fill_missing_seo_snapshot"), patch.object(
            main, "leadbot_enrich_this_scan", side_effect=RuntimeError("fail")
        ), patch.object(main.threading, "Thread", ImmediateThread):
            main.leadbot_complete_details("failure.csv", request(), csrf_token="token")
        self.assertNotIn("failure.csv", main._LEADBOT_COMPLETE_DETAILS_RUNNING)

    def test_admin_toggle_enforces_role_and_csrf_before_mutation(self):
        setter = Mock()
        with patch.object(main, "auth_current_user", return_value={"role": "user"}), patch.object(
            main, "leadbot_set_dataforseo_enabled", setter
        ):
            response = asyncio.run(main.leadbot_dataforseo_toggle(request(), "token"))
        self.assertEqual(response.status_code, 403)
        setter.assert_not_called()

        with patch.object(main, "auth_current_user", return_value={"role": "admin"}), patch.object(
            main, "_csrf_token_valid", return_value=False
        ), patch.object(main, "leadbot_set_dataforseo_enabled", setter):
            response = asyncio.run(main.leadbot_dataforseo_toggle(request()))
        self.assertEqual(response.status_code, 403)
        setter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
