"""
Regression test: the Lead Finder live-scan page still looked "in progress"
after a scan reached the terminal cancelled status.

app.main.leadbot_live_page (GET /lead-bot/live/{job_id}) renders inline JS
whose poll() function has a dedicated `job.status === "done"` branch that
finalizes the done-state visuals, but had no equivalent branch for
`job.status === "cancelled"`. The CSS that stops the pulse/progress-bar/
console-sweep animations is gated behind a `body.leadbot-live-final` class
(also used by the done path) plus `.status.leadbot-cancelled-state` --
neither poll() nor cancelScan()'s own response handler ever added them for
the cancelled path, so the progress bar kept animating, the Cancel button
stayed enabled, and the console lines kept "scanning"-flavored text
indefinitely even though #message and the Cancel button's own success
branch already correctly said "Scan cancelled."

The fix adds a shared finalizeLiveScanCancelled() JS helper, called from
both poll()'s new `job.status === "cancelled"` branch and cancelScan()'s
existing success handler, which adds the two classes, disables the Cancel
button, and swaps the live-console lines off scanning language.

Uses a real headless browser (Playwright) against a real uvicorn
subprocess, since this bug is purely client-side JS/CSS behavior invisible
to a route-only TestClient test (same pattern as
LeadBotCardsOwnershipBrowserRegressionTests in test_leadbot_csrf_routes.py).
Skips itself if Playwright/Chromium aren't installed. Uses a throwaway
account and a hand-written job file (real data/leadbot_live_jobs/, no real
crawling -- the job is written directly with status="cancelled" already
set, bypassing create_job()/run_job()), both removed in cleanup.
"""

import os
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

import requests

import agents.auth_agent as auth_agent
import agents.lead_live_job_agent as job_agent


class LeadFinderCancelUiStateTests(unittest.TestCase):
    PORT = 8792

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest("playwright is not installed")

        cls._sync_playwright_ctx = sync_playwright()
        cls._playwright = cls._sync_playwright_ctx.__enter__()

        try:
            cls._browser = cls._playwright.chromium.launch(args=["--no-sandbox"])
        except Exception as exc:
            cls._sync_playwright_ctx.__exit__(None, None, None)
            raise unittest.SkipTest(f"chromium is not available: {exc}")

        repo_root = Path(__file__).resolve().parent.parent
        cls._proc = subprocess.Popen(
            [
                str(repo_root / "venv" / "bin" / "python3"),
                "-m", "uvicorn", "app.main:app",
                "--host", "127.0.0.1", "--port", str(cls.PORT),
            ],
            cwd=str(repo_root),
            env={
                **os.environ,
                "USE_LIVE_SERP": "false",
                "DATAFORSEO_ENABLED": "0",
                "LEADBOT_DATAFORSEO_ENABLED": "0",
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        base_url = f"http://127.0.0.1:{cls.PORT}"
        deadline = time.time() + 20
        up = False
        while time.time() < deadline:
            try:
                resp = requests.get(f"{base_url}/login", timeout=1)
                if resp.status_code == 200:
                    up = True
                    break
            except Exception:
                pass
            time.sleep(0.3)

        if not up:
            cls._proc.terminate()
            cls._browser.close()
            cls._sync_playwright_ctx.__exit__(None, None, None)
            raise unittest.SkipTest("local dev server did not start in time")

        cls.base_url = base_url

    @classmethod
    def tearDownClass(cls):
        proc = getattr(cls, "_proc", None)
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()

        browser = getattr(cls, "_browser", None)
        if browser is not None:
            browser.close()

        ctx = getattr(cls, "_sync_playwright_ctx", None)
        if ctx is not None:
            ctx.__exit__(None, None, None)

    def setUp(self):
        suffix = uuid.uuid4().hex[:10]
        self.username = f"cancelui_{suffix}"
        self.password = "correct-horse-battery-staple"
        auth_agent.create_user(self.username, self.password, role="standard", email=f"{self.username}@example.com")
        self.addCleanup(self._delete_user, self.username)

        self.job_id = uuid.uuid4().hex[:16]
        job = {
            "job_id": self.job_id,
            "status": "cancelled",
            "message": "Scan cancelled.",
            "created_at": job_agent.now_iso(),
            "updated_at": job_agent.now_iso(),
            "params": {"limit": 5},
            "leads": [{
                "domain": "cancel-ui-test.example",
                "url": "https://cancel-ui-test.example",
                "title": "Cancel UI Test Plumbing",
                "outreach_status": "call_ready",
                "contact_confidence": 80,
                "final_lead_score": 90,
            }],
            "seen_domains": ["cancel-ui-test.example"],
            "errors": [],
            "cancel_requested": True,
            "cancelled_at": job_agent.now_iso(),
            "counts": {"found": 1, "cached": 0, "enriched": 0, "needs_research": 1},
            "export_file": "",
        }
        job_agent.write_job(job)
        self.addCleanup(self._delete_job, self.job_id)

    def _delete_user(self, username):
        import sqlite3
        try:
            conn = sqlite3.connect(auth_agent.AUTH_DB)
            conn.execute("DELETE FROM users WHERE username = ?", (username,))
            conn.commit()
            conn.close()
        except Exception:
            pass

    def _delete_job(self, job_id):
        try:
            job_agent.job_path(job_id).unlink()
        except FileNotFoundError:
            pass

    def _login_and_get_page(self):
        context = self._browser.new_context()
        self.addCleanup(context.close)
        page = context.new_page()
        page.goto(f"{self.base_url}/login")
        page.fill('input[name="username"]', self.username)
        page.fill('input[name="password"]', self.password)
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")
        return page

    def test_cancelled_scan_stops_looking_active(self):
        page = self._login_and_get_page()
        page.goto(f"{self.base_url}/lead-bot/live/{self.job_id}")
        page.wait_for_selector("#cancelScanBtn")

        # poll() runs on load and must finalize the cancelled state on its
        # own -- the fix's core assertion. Without it, this class is never
        # added and the wait times out.
        page.wait_for_function(
            "document.body.classList.contains('leadbot-live-final')", timeout=5000
        )

        self.assertTrue(
            page.evaluate(
                "document.querySelector('.status').classList.contains('leadbot-cancelled-state')"
            )
        )
        self.assertTrue(page.evaluate("document.getElementById('cancelScanBtn').disabled"))
        self.assertIn("Scan cancelled.", page.inner_text("#message"))

        progress_animation = page.evaluate(
            "getComputedStyle(document.querySelector('.live-progress-bar')).animationName"
        )
        self.assertEqual(progress_animation, "none")

        sweep_animation = page.evaluate(
            "getComputedStyle(document.querySelector('.live-console-body'), ':after').animationName"
        )
        self.assertEqual(sweep_animation, "none")

        self.assertEqual(page.eval_on_selector_all(".pulse", "els => els.length"), 0)

        # Partial leads found before cancellation must still be visible.
        self.assertIn("cancel-ui-test.example", page.content())

        line1 = page.inner_text("#liveConsoleLine1")
        line2 = page.inner_text("#liveConsoleLine2")
        for line in (line1, line2):
            self.assertNotIn("scanning", line.lower())
            self.assertNotIn("searching", line.lower())


if __name__ == "__main__":
    unittest.main()
