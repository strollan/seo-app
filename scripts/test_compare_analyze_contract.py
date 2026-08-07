"""
Regression tests for the Compare form -> POST /analyze field contract.

Production bug: /analyze returned 422 {"type": "missing", "loc": ["body",
"url_2"]} for real visitors. Root cause: compare.html marked url_1 `required`
but not url_2, so the browser happily submitted url_2="" — and FastAPI treats
an empty string for a required Form(...) field as missing.

Covers: the template exposes url_1/url_2 inputs (matching the /analyze
schema) and marks both required with client-side trimming; a valid two-URL
submission passes validation and reaches the analysis pipeline; empty or
absent url_2 is rejected with 422 (backend stays strict); submitted values
reach fetch_page_data unchanged (url_2 minus normal trimming).

Route-level tests patch app.main.validate_public_url to a no-op and
app.main.fetch_page_data to a recorder, isolating the request/validation
contract from network access and the report-generation pipeline.
"""

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# agents/seo_agent.py raises RuntimeError at import time if OPENAI_API_KEY
# isn't set, and app.main imports it transitively. This suite never talks to
# OpenAI, so a placeholder key is enough to let `import app.main` succeed.
os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

from fastapi.testclient import TestClient

import app.main as appmain
import agents.auth_agent as auth_agent


COMPARE_TEMPLATE = (
    Path(__file__).resolve().parent.parent / "app" / "templates" / "compare.html"
)


class SecondFetchSentinel(Exception):
    """Raised on the second fetch_page_data call, proving the request passed
    validation and both URLs reached the analysis pipeline."""


class CompareTemplateContractTests(unittest.TestCase):
    """The Compare form's field names and constraints must match /analyze."""

    @classmethod
    def setUpClass(cls):
        cls.html = COMPARE_TEMPLATE.read_text(encoding="utf-8")

    def input_tag(self, name):
        match = re.search(rf'<input[^>]*\bname="{name}"[^>]*>', self.html)
        self.assertIsNotNone(match, f'no <input name="{name}"> in compare.html')
        return match.group(0)

    def test_form_posts_to_analyze(self):
        self.assertRegex(
            self.html, r'<form[^>]*method="post"[^>]*action="/analyze"'
        )

    def test_both_url_fields_exist_with_backend_schema_names(self):
        self.input_tag("url_1")
        self.input_tag("url_2")

    def test_both_url_fields_are_required(self):
        # url_2 without `required` is the exact bug: browsers submit
        # url_2="" and FastAPI 422s it as a missing field.
        self.assertIn(" required", self.input_tag("url_1"))
        self.assertIn(" required", self.input_tag("url_2"))

    def test_submit_script_trims_both_fields(self):
        self.assertIn("url1.value = url1.value.trim()", self.html)
        self.assertIn("url2.value = url2.value.trim()", self.html)


class AnalyzeRouteContractTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        self.auth_db_path = Path(self.tmpdir.name) / "test_auth.db"
        db_patch = mock.patch.object(auth_agent, "AUTH_DB", self.auth_db_path)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        auth_agent.init_auth_db()

        appmain._analyze_rate_limit_attempts.clear()
        self.addCleanup(appmain._analyze_rate_limit_attempts.clear)

        validate_patch = mock.patch.object(
            appmain, "validate_public_url", lambda url: None
        )
        validate_patch.start()
        self.addCleanup(validate_patch.stop)

        self.fetched_urls = []

        def _recording_fetch(url):
            self.fetched_urls.append(url)
            if len(self.fetched_urls) >= 2:
                raise SecondFetchSentinel("both URLs reached the pipeline")
            return {}

        fetch_patch = mock.patch.object(appmain, "fetch_page_data", _recording_fetch)
        fetch_patch.start()
        self.addCleanup(fetch_patch.stop)

        self.client = TestClient(appmain.app)


class AnalyzeRouteContractTests(AnalyzeRouteContractTestCase):
    def test_valid_two_url_submission_passes_validation(self):
        with self.assertRaises(SecondFetchSentinel):
            self.client.post(
                "/analyze",
                data={"url_1": "https://example.com", "url_2": "https://example.org"},
            )
        self.assertEqual(
            self.fetched_urls, ["https://example.com", "https://example.org"]
        )

    def test_submitted_values_survive_unchanged_except_url2_trim(self):
        with self.assertRaises(SecondFetchSentinel):
            self.client.post(
                "/analyze",
                data={"url_1": "https://example.com", "url_2": "  https://example.org  "},
            )
        self.assertEqual(
            self.fetched_urls, ["https://example.com", "https://example.org"]
        )

    def test_empty_url_2_is_rejected_with_422(self):
        response = self.client.post(
            "/analyze",
            data={"url_1": "https://example.com", "url_2": ""},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.fetched_urls, [])

    def test_absent_url_2_is_rejected_with_422(self):
        response = self.client.post(
            "/analyze",
            data={"url_1": "https://example.com"},
        )
        self.assertEqual(response.status_code, 422)
        detail = response.json()["detail"]
        self.assertTrue(
            any(err.get("loc") == ["body", "url_2"] for err in detail),
            f"expected a url_2 error, got: {detail}",
        )
        self.assertEqual(self.fetched_urls, [])

    def test_absent_url_1_is_rejected_with_422(self):
        response = self.client.post(
            "/analyze",
            data={"url_2": "https://example.org"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.fetched_urls, [])


if __name__ == "__main__":
    unittest.main()
