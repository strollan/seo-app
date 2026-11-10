"""Browser status regressions with intercepted HTTP; starts no application server."""
import html
import json
import sys
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agents.lead_dashboard_agent import _actii_crm_modal


class CrmUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page(viewport={"width": 1280, "height": 900})
        self.addCleanup(self.page.close)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.jobs = []
        self.posts = 0
        self.status_checks = 0
        self.status = 202
        self.reply = {"ok": True, "id": "a" * 32, "state": "queued", "message": "Queued for ACT II.",
                      "source_file": "synthetic.csv", "source_domain": "business.example"}
        lead = html.escape(json.dumps({"company_name": "Synthetic Business", "website": "https://business.example"}), quote=True)
        self.card = f'<div class="lead-card"><div class="lead-crm-action"><button class="lead-add-crm" data-source-file="synthetic.csv" data-source-domain="business.example" data-lead="{lead}">Add to CRM</button></div></div>'
        self.document = '<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body>' + self.card + _actii_crm_modal("synthetic-csrf") + '</body></html>'
        self.page.route("**/*", self.route)

    def route(self, route):
        path = route.request.url.split("ui.example", 1)[-1]
        if path.split("?", 1)[0] == "/static/actii-crm.js":
            route.fulfill(content_type="text/javascript", body=(ROOT / "app/static/actii-crm.js").read_text())
        elif path == "/lead-bot/crm-jobs":
            self.status_checks += 1
            route.fulfill(json={"ok": True, "jobs": self.jobs})
        elif path == "/lead-bot/add-to-crm":
            self.posts += 1
            self.assertEqual(route.request.post_data_json["csrf_token"], "synthetic-csrf")
            if self.status == 202:
                self.jobs = [self.reply.copy()]
            route.fulfill(status=self.status, json=self.reply)
        elif path == "/":
            route.fulfill(content_type="text/html", body=self.document)
        else:
            route.abort()  # No unhandled request reaches a real host or loopback.

    def open_form(self):
        self.page.goto("https://ui.example/")
        self.page.get_by_role("button", name="Add to CRM", exact=True).click()

    def test_desktop_mobile_queue_and_reload_no_resubmit(self):
        for width in (1280, 390):
            with self.subTest(width=width):
                self.jobs = []; self.posts = 0
                self.page.set_viewport_size({"width": width, "height": 900})
                self.open_form()
                self.assertEqual(self.page.locator('input[name="website"]').input_value(), "https://business.example")
                self.page.screenshot(path=f"/tmp/actii-crm-{width}.png")
                self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
                self.page.get_by_role("button", name="Add Contact", exact=True).click()
                self.page.wait_for_function("document.querySelector('.lead-add-crm').textContent === 'Queued for ACT II'")
                self.assertNotIn("couple of minutes", self.page.locator('#actiiCrmMessage').text_content())
                self.assertIn("Queued for ACT II", self.page.locator('#actiiCrmMessage').text_content())
                self.assertTrue(self.page.locator('.lead-add-crm').is_disabled())
                self.assertTrue(self.page.locator('.crm-save').is_disabled())
                self.page.locator('.lead-add-crm').click(force=True)  # a second click must not resubmit
                self.page.locator('.crm-save').click(force=True)
                self.assertEqual(self.posts, 1)
                self.assertEqual(self.page.locator('.lead-crm-result a').count(), 0)
                self.page.reload()
                self.page.wait_for_function("document.querySelector('.lead-add-crm').textContent === 'Pending import'")
                self.assertNotIn("couple of minutes", self.page.locator('.lead-crm-result').text_content())
                self.assertTrue(self.page.locator('.lead-add-crm').is_disabled())
                self.assertEqual(self.posts, 1)
                self.jobs[0].update(state="processing", message="Processing in ACT II.")
                self.page.reload()
                self.page.wait_for_function("document.querySelector('.lead-add-crm').textContent === 'Importing…'")
                self.assertIn("couple of minutes", self.page.locator('.lead-crm-result').text_content())
                self.jobs[0].update(state="added", message="Added to CRM.", open_url="http://127.0.0.1:8842/crm/contacts/7")
                self.page.reload()
                self.page.wait_for_function("document.querySelector('.lead-add-crm').textContent === 'Added to ACT II'")
                links = self.page.locator('.lead-crm-result a')
                self.assertEqual([links.nth(i).get_attribute("href") for i in range(links.count())],
                                 ["http://127.0.0.1:8842/crm/contacts/7", "http://127.0.0.1:8842/crm"])
                self.assertEqual(self.posts, 1)
        self.assertEqual(self.errors, [])

    def test_pending_status_polls_until_added_then_stops(self):
        self.jobs = [self.reply.copy()]
        self.page.clock.install()
        self.page.goto("https://ui.example/")
        self.page.wait_for_function("document.querySelector('.lead-add-crm').textContent === 'Pending import'")
        self.assertEqual(self.status_checks, 1)
        self.page.clock.fast_forward(15000)
        self.page.wait_for_function("() => true")
        self.assertEqual(self.status_checks, 2)
        self.jobs[0].update(state="added", message="Added to CRM.", open_url="http://127.0.0.1:8842/crm/contacts/7")
        self.page.clock.fast_forward(15000)
        self.page.wait_for_function("document.querySelector('.lead-add-crm').textContent === 'Added to ACT II'")
        self.assertEqual(self.status_checks, 3)
        self.page.clock.fast_forward(120000)  # completed: no further status checks
        self.assertEqual(self.status_checks, 3)
        self.assertEqual(self.posts, 0)
        self.assertEqual(self.errors, [])

    def test_review_failed_and_existing_states(self):
        cases = (("needs_review", "Needs review", "Do not resubmit"), ("failed", "Import failed", "Nothing was added"),
                 ("already_present", "Already in ACT II", "No duplicate was created"))
        for state, label, note in cases:
            self.jobs = [{**self.reply, "state": state, "open_url": "http://127.0.0.1:8842/crm/contacts/7",
                          "message": "Needs review on the ACT II workstation. Do not resubmit." if state == "needs_review" else ""}]
            self.page.goto("https://ui.example/")
            self.page.wait_for_function("label => document.querySelector('.lead-add-crm').textContent === label", arg=label)
            self.assertTrue(self.page.locator('.lead-add-crm').is_disabled())
            self.assertIn(note, self.page.locator('.lead-crm-result').text_content())
            self.assertNotIn("couple of minutes", self.page.locator('.lead-crm-result').text_content())
        self.assertEqual(self.posts, 0)
        self.assertEqual(self.errors, [])

    def test_existing_contact_reply_to_submission(self):
        self.status = 202; self.reply = {**self.reply, "state": "already_present", "message": "Already present in CRM.",
                                         "open_url": "http://127.0.0.1:8842/crm/contacts/7"}
        self.open_form()
        self.page.get_by_role("button", name="Add Contact", exact=True).click()
        self.page.wait_for_function("document.querySelector('.lead-add-crm').textContent === 'Already in ACT II'")
        self.assertIn("No duplicate was created", self.page.locator('#actiiCrmMessage').text_content())
        links = self.page.locator('#actiiCrmMessage a')
        self.assertEqual([links.nth(i).get_attribute("href") for i in range(links.count())],
                         ["http://127.0.0.1:8842/crm/contacts/7", "http://127.0.0.1:8842/crm"])
        self.assertEqual(self.posts, 1)
        self.assertEqual(self.errors, [])

    def test_queued_with_connector_stopped_settles_without_spinning(self):
        self.jobs = [self.reply.copy()]
        self.page.clock.install()
        self.page.goto("https://ui.example/")
        self.page.wait_for_function("document.querySelector('.lead-add-crm').textContent === 'Pending import'")
        for expected in range(2, 82):  # each check finishes before the next one is scheduled
            self.page.clock.fast_forward(15000)
            for _ in range(100):
                if self.status_checks >= expected:
                    break
                self.page.wait_for_timeout(20)
            self.assertEqual(self.status_checks, expected)
        self.page.wait_for_timeout(200)
        self.page.clock.fast_forward(15000)
        self.page.wait_for_function("document.querySelector('.lead-crm-result').textContent.includes('Automatic status checks have stopped')")
        checks = self.status_checks
        self.page.clock.fast_forward(600000)
        self.assertEqual(self.status_checks, checks)  # bounded: no endless polling
        self.assertEqual(self.page.locator('.lead-add-crm').text_content(), 'Pending import')
        self.assertEqual(self.posts, 0)
        self.assertEqual(self.errors, [])

    def test_server_error_offers_retry(self):
        self.status = 503; self.reply = {"ok": False}
        self.open_form()
        self.page.get_by_role("button", name="Add Contact", exact=True).click()
        self.page.wait_for_function("document.getElementById('actiiCrmMessage').textContent.includes('Nothing was queued')")
        self.assertFalse(self.page.get_by_role("button", name="Retry request", exact=True).is_disabled())
        self.assertEqual(self.page.locator('.lead-add-crm').text_content(), 'Add to CRM')
        self.assertEqual(self.errors, [])

    def test_validation_failure_preserves_edits_and_keyboard_close(self):
        self.status = 422; self.reply = {"ok": False, "error": "Website must retain the original prospect's hostname."}
        self.open_form()
        self.page.locator('input[name="website"]').fill("https://wrong.example")
        self.page.get_by_role("button", name="Add Contact", exact=True).click()
        self.page.wait_for_function("document.getElementById('actiiCrmMessage').textContent.includes('original prospect')")
        self.assertEqual(self.page.locator('input[name="website"]').input_value(), "https://wrong.example")
        self.assertFalse(self.page.get_by_role("button", name="Retry request", exact=True).is_disabled())
        self.page.keyboard.press("Escape")
        self.assertFalse(self.page.locator('dialog').is_visible())
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
