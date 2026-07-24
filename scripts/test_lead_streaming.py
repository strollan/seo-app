"""
Permanent regression tests for true-incremental (streaming) lead publishing.

Covers agents/lead_finding_agent.py's find_leads() (page-by-page fetch +
first-N-that-qualify selection) and agents/lead_live_job_agent.py's
run_job()/call_find_leads_with_timeout() (thread-safe streaming publish,
deterministic heartbeat handoff, and the signature-based, non-exception-
driven find_leads call resolution).
"""

import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import agents.lead_finding_agent as lfa
import agents.lead_live_job_agent as ja


def fake_contact(url, market=""):
    return {
        "best_phone": "555-000-1111",
        "phones": ["555-000-1111"],
        "emails": ["owner@example.com"],
        "contact_page_url": url + "/contact",
        "confidence": 80,
        "flags": [],
    }


def make_item(domain, title="A Local Business", snippet="plumber services", serp_position=11):
    return {
        "title": title,
        "url": f"https://{domain}/",
        "domain": domain,
        "snippet": snippet,
        "serp_position": serp_position,
    }


class FindLeadsStreamingTests(unittest.TestCase):
    """find_leads(): per-page fetch, first-N-that-qualify selection,
    on_candidate emission timing."""

    def setUp(self):
        self.page_call_order = []
        self.page_call_times = []
        self.places_call_count = 0

        def fake_find_business_competitors(query, own_domain=None, location="", limit=20, pages=None):
            page = (pages or [1])[0]
            self.page_call_order.append(page)
            self.page_call_times.append((page, time.monotonic()))
            time.sleep(0.05)
            if page == 1:
                return [make_item("page1-a.com"), make_item("page1-b.com")]
            if page == 2:
                return [make_item("page2-a.com"), make_item("page2-b.com")]
            if page == 3:
                return [make_item("page3-a.com")]
            return []

        def fake_places_search(keyword, location, page, num):
            self.places_call_count += 1
            return [make_item(f"places-{page}-a.com")]

        self.fake_find_business_competitors = fake_find_business_competitors

        patches = [
            mock.patch.object(lfa, "find_business_competitors", fake_find_business_competitors),
            mock.patch.object(lfa, "extract_contact_from_url", fake_contact),
            mock.patch.object(lfa, "_lead_bot_fast_mode", lambda: False),
            mock.patch("business_competitor_finder._leadbot_google_places_search", fake_places_search),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_page1_lead_emitted_before_page2_fetch_starts(self):
        emitted = []

        def on_candidate(lead):
            emitted.append((time.monotonic(), lead.get("domain")))

        result = lfa.find_leads(
            industry="plumber", market="Test City", service_keyword="plumber",
            limit=10, on_candidate=on_candidate,
        )

        self.assertGreaterEqual(len(emitted), 1)
        first_emit_time = emitted[0][0]

        page2_calls = [t for (page, t) in self.page_call_times if page == 2]
        self.assertTrue(page2_calls, "page 2 was never fetched")
        self.assertLess(
            first_emit_time, page2_calls[0],
            "first lead was published after page 2's fetch already started -- not true streaming",
        )
        self.assertEqual(result["leads"][0]["domain"], "page1-a.com")

    def test_streamed_candidates_published_exactly_once(self):
        emitted_domains = []

        def on_candidate(lead):
            emitted_domains.append(lead["domain"])

        result = lfa.find_leads(
            industry="plumber", market="Test City", service_keyword="plumber",
            limit=10, on_candidate=on_candidate,
        )

        self.assertEqual(len(emitted_domains), len(set(emitted_domains)))
        self.assertEqual(
            emitted_domains, [l["domain"] for l in result["leads"]],
            "on_candidate emissions must match the final leads list one-for-one, in order",
        )

    def test_callback_and_non_callback_paths_match_exactly(self):
        result_with_cb = lfa.find_leads(
            industry="plumber", market="Test City", service_keyword="plumber",
            limit=10, on_candidate=lambda lead: None,
        )
        self.page_call_order.clear()
        self.page_call_times.clear()

        result_without_cb = lfa.find_leads(
            industry="plumber", market="Test City", service_keyword="plumber", limit=10,
        )

        domains_with = [l["domain"] for l in result_with_cb["leads"]]
        domains_without = [l["domain"] for l in result_without_cb["leads"]]
        self.assertEqual(domains_with, domains_without)
        self.assertEqual(result_with_cb["count"], result_without_cb["count"])

    def test_domain_dedupe_across_pages(self):
        def fake_with_dupe(query, own_domain=None, location="", limit=20, pages=None):
            page = (pages or [1])[0]
            if page == 1:
                return [make_item("dupe.com"), make_item("page1-b.com")]
            if page == 2:
                return [make_item("dupe.com"), make_item("page2-a.com")]
            return []

        with mock.patch.object(lfa, "find_business_competitors", fake_with_dupe):
            result = lfa.find_leads(
                industry="plumber", market="Test City", service_keyword="plumber", limit=10,
            )

        domains = [l["domain"] for l in result["leads"]]
        self.assertEqual(domains.count("dupe.com"), 1)
        self.assertEqual(len(domains), len(set(domains)))

    def test_quota_reached_stops_fetching_and_no_page_is_fetched_twice(self):
        result = lfa.find_leads(
            industry="plumber", market="Test City", service_keyword="plumber", limit=2,
        )
        self.assertEqual(len(result["leads"]), 2)
        self.assertNotIn(3, self.page_call_order)
        self.assertNotIn(4, self.page_call_order)
        # Every page that *was* fetched was fetched exactly once.
        self.assertEqual(len(self.page_call_order), len(set(self.page_call_order)))

    def test_no_page_ever_fetched_twice_even_without_early_stop(self):
        lfa.find_leads(
            industry="plumber", market="Test City", service_keyword="plumber", limit=100,
        )
        self.assertEqual(sorted(self.page_call_order), [1, 2, 3, 4])
        self.assertEqual(len(self.page_call_order), len(set(self.page_call_order)))

    def test_places_supplement_invoked_at_most_once(self):
        # "restaurant" triggers the Places supplement path.
        lfa.find_leads(
            industry="restaurant", market="Test City", service_keyword="restaurant", limit=100,
        )
        # The supplement itself fetches 2 Places *pages* in one supplement
        # invocation (existing, pre-streaming behavior) -- what must never
        # happen is the whole supplement running more than once.
        self.assertLessEqual(self.places_call_count, 2)

    def test_places_supplement_skipped_when_quota_already_filled(self):
        lfa.find_leads(
            industry="restaurant", market="Test City", service_keyword="restaurant", limit=2,
        )
        self.assertEqual(self.places_call_count, 0)

    def test_on_candidate_exception_does_not_break_scan(self):
        def bad_callback(lead):
            raise RuntimeError("UI publish failed")

        result = lfa.find_leads(
            industry="plumber", market="Test City", service_keyword="plumber",
            limit=10, on_candidate=bad_callback,
        )
        self.assertGreaterEqual(len(result["leads"]), 1)


class ResolveFindLeadsCallTests(unittest.TestCase):
    """Signature-based call resolution: exactly one find_leads invocation,
    no exception-driven retry that could double-run an expensive scan."""

    def test_full_signature_gets_on_candidate(self):
        calls = []

        def full(industry, market, service_keyword=None, own_domain=None, limit=10, on_candidate=None):
            calls.append(on_candidate)
            return {"leads": []}

        call = ja._resolve_find_leads_call(
            full, industry="i", market="m", query="q", own_domain="", limit=5, on_candidate=lambda x: None,
        )
        call()
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(calls[0])

    def test_legacy_signature_without_on_candidate_gets_keyword_call_no_callback(self):
        calls = []

        def legacy(industry, market, service_keyword=None, own_domain=None, limit=10):
            calls.append("called")
            return {"leads": []}

        call = ja._resolve_find_leads_call(
            legacy, industry="i", market="m", query="q", own_domain="", limit=5, on_candidate=lambda x: None,
        )
        call()
        self.assertEqual(len(calls), 1)

    def test_positional_only_signature_falls_back_to_positional_call(self):
        received = []

        def positional_only(a, b, c, d, e):
            received.append((a, b, c, d, e))
            return {"leads": []}

        call = ja._resolve_find_leads_call(
            positional_only, industry="i", market="m", query="q", own_domain="own", limit=5,
            on_candidate=lambda x: None,
        )
        call()
        self.assertEqual(received, [("i", "m", "q", "own", 5)])

    def test_internal_typeerror_does_not_trigger_a_second_call(self):
        """The exact scenario this hardening pass exists to prevent: an
        internal TypeError inside find_leads must propagate as a real
        error, not be mistaken for 'on_candidate unsupported' and cause a
        silent second (potentially expensive, external) call."""
        call_count = {"n": 0}

        def buggy_find_leads(industry, market, service_keyword=None, own_domain=None, limit=10, on_candidate=None):
            call_count["n"] += 1
            raise TypeError("unsupported operand type(s): simulated internal bug")

        leads, error = ja.call_find_leads_with_timeout(
            buggy_find_leads, industry="plumber", market="Test City", query="plumber", own_domain="", limit=5,
        )

        self.assertEqual(call_count["n"], 1, "find_leads was invoked more than once for a single request")
        self.assertIsNone(leads)
        self.assertIn("simulated internal bug", error)


class RunJobStreamingTests(unittest.TestCase):
    """run_job() end-to-end tests against real job files on disk."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

        self._orig_job_dir = ja.JOB_DIR
        ja.JOB_DIR = Path(self.tmpdir)

        self._orig_timeout = ja.LIVE_SCAN_BATCH_TIMEOUT_SECONDS
        self._orig_heartbeat_interval = ja.LEADBOT_HEARTBEAT_INTERVAL_SECONDS

        def restore():
            ja.JOB_DIR = self._orig_job_dir
            ja.LIVE_SCAN_BATCH_TIMEOUT_SECONDS = self._orig_timeout
            ja.LEADBOT_HEARTBEAT_INTERVAL_SECONDS = self._orig_heartbeat_interval

        self.addCleanup(restore)

        patches = [
            mock.patch("agents.lead_business_cache_agent.apply_cached_business_to_lead", lambda lead: (lead, False)),
            mock.patch("agents.lead_business_cache_agent.save_business_from_lead", lambda lead, enriched=False: None),
            mock.patch("agents.lead_export_agent.export_leads_to_csv", lambda payload: {"filename": "test.csv"}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _make_job(self, limit=10, guest_id=""):
        job_id = "testjob-" + str(time.monotonic()).replace(".", "")
        job = {
            "job_id": job_id,
            "status": "queued",
            "message": "",
            "leads": [],
            "errors": [],
            "counts": {"found": 0, "cached": 0, "enriched": 0, "needs_research": 0},
            "seen_domains": [],
            "params": {
                "industry": "plumber",
                "market": "Test City",
                "keyword": "plumber",
                "own_domain": "",
                "limit": limit,
                "per_batch": 8,
                "per_query_limit": 8,
                "max_queries": 1,
                "guest_id": guest_id,
            },
            "cancel_requested": False,
            "updated_at": ja.now_iso(),
        }
        ja.write_job(job)
        return job_id

    def test_leads_grow_incrementally_on_disk_during_run(self):
        def slow_find_leads(industry, market, service_keyword=None, own_domain=None, limit=10, on_candidate=None):
            lead1 = dict(make_item("live-page1.com"))
            lead1["final_lead_score"] = 90
            lead1["best_phone"] = "555-1"
            lead1["emails"] = []
            if on_candidate:
                on_candidate(lead1)

            time.sleep(0.3)

            lead2 = dict(make_item("live-page2.com"))
            lead2["final_lead_score"] = 85
            lead2["best_phone"] = "555-2"
            lead2["emails"] = []
            if on_candidate:
                on_candidate(lead2)

            return {"query": service_keyword, "leads": [lead1, lead2], "count": 2}

        job_id = self._make_job(limit=10)

        with mock.patch("agents.lead_finding_agent.find_leads", slow_find_leads):
            thread = threading.Thread(target=ja.run_job, args=(job_id,), daemon=True)
            thread.start()

            deadline = time.monotonic() + 5
            saw_first_lead_early = False
            while time.monotonic() < deadline:
                job = ja.read_job(job_id)
                if job and any(l.get("domain") == "live-page1.com" for l in job.get("leads", [])):
                    saw_first_lead_early = True
                    break
                time.sleep(0.02)

            thread.join(timeout=5)

        self.assertTrue(saw_first_lead_early, "live-page1.com never appeared in job['leads'] while the scan was still running")

        final_job = ja.read_job(job_id)
        domains = [l.get("domain") for l in final_job["leads"]]
        self.assertEqual(domains, ["live-page1.com", "live-page2.com"])
        self.assertEqual(final_job["status"], "done")

    def test_guest_and_total_limit_enforced_in_final_job(self):
        def generous_find_leads(industry, market, service_keyword=None, own_domain=None, limit=10, on_candidate=None):
            leads = []
            for i in range(5):
                lead = dict(make_item(f"guest-lead-{i}.com"))
                lead["final_lead_score"] = 90
                lead["best_phone"] = "555-0"
                lead["emails"] = []
                if on_candidate:
                    on_candidate(lead)
                leads.append(lead)
            return {"query": service_keyword, "leads": leads, "count": len(leads)}

        job_id = self._make_job(limit=2, guest_id="guest-abc")

        with mock.patch("agents.lead_finding_agent.find_leads", generous_find_leads):
            ja.run_job(job_id)

        final_job = ja.read_job(job_id)
        self.assertLessEqual(len(final_job["leads"]), 2)
        self.assertEqual(final_job["status"], "done")

    def test_timeout_path_ends_cleanly(self):
        ja.LIVE_SCAN_BATCH_TIMEOUT_SECONDS = 0.5

        def stalling_find_leads(industry, market, service_keyword=None, own_domain=None, limit=10, on_candidate=None):
            time.sleep(5)
            return {"query": service_keyword, "leads": [], "count": 0}

        job_id = self._make_job(limit=5)

        with mock.patch("agents.lead_finding_agent.find_leads", stalling_find_leads):
            start = time.monotonic()
            ja.run_job(job_id)
            elapsed = time.monotonic() - start

        self.assertLess(elapsed, 4, "run_job() did not respect the shortened timeout -- it hung")
        final_job = ja.read_job(job_id)
        self.assertIn(final_job["status"], {"done", "error", "cancelled"})

    def test_error_path_ends_cleanly(self):
        def raising_find_leads(industry, market, service_keyword=None, own_domain=None, limit=10, on_candidate=None):
            raise RuntimeError("simulated SERP provider outage")

        job_id = self._make_job(limit=5)

        with mock.patch("agents.lead_finding_agent.find_leads", raising_find_leads):
            ja.run_job(job_id)

        final_job = ja.read_job(job_id)
        self.assertEqual(final_job["status"], "done")
        self.assertTrue(any("simulated SERP provider outage" in e for e in final_job.get("errors", [])))

    def test_job_file_never_corrupted_during_concurrent_writes(self):
        def bursty_find_leads(industry, market, service_keyword=None, own_domain=None, limit=10, on_candidate=None):
            leads = []
            for i in range(6):
                lead = dict(make_item(f"burst-{i}.com"))
                lead["final_lead_score"] = 90
                lead["best_phone"] = "555-0"
                lead["emails"] = []
                if on_candidate:
                    on_candidate(lead)
                leads.append(lead)
                time.sleep(0.02)
            return {"query": service_keyword, "leads": leads, "count": len(leads)}

        job_id = self._make_job(limit=10)
        job_path = ja.job_path(job_id)

        corruption_seen = {"value": False}
        stop_reading = threading.Event()

        def reader():
            while not stop_reading.is_set():
                try:
                    text = job_path.read_text(encoding="utf-8")
                    if text.strip():
                        json.loads(text)
                except FileNotFoundError:
                    pass
                except json.JSONDecodeError:
                    corruption_seen["value"] = True
                    stop_reading.set()
                time.sleep(0.005)

        with mock.patch("agents.lead_finding_agent.find_leads", bursty_find_leads):
            reader_thread = threading.Thread(target=reader, daemon=True)
            reader_thread.start()
            ja.run_job(job_id)
            stop_reading.set()
            reader_thread.join(timeout=2)

        self.assertFalse(corruption_seen["value"], "job file was read as invalid/partial JSON at least once during concurrent writes")

    def test_heartbeat_write_never_lands_after_a_streamed_lead_write(self):
        """Forces a real in-flight heartbeat/streaming race: the heartbeat
        interval is shrunk so several real heartbeat writes land before the
        first candidate arrives, then asserts the final job state still has
        the correct, non-lost lead data -- proving the deterministic
        stop-and-join handoff (not just timing luck) protects the write."""
        ja.LEADBOT_HEARTBEAT_INTERVAL_SECONDS = 0.02

        def delayed_find_leads(industry, market, service_keyword=None, own_domain=None, limit=10, on_candidate=None):
            # Let several real heartbeat ticks (each ~0.02s) fire first.
            time.sleep(0.15)
            lead = dict(make_item("post-heartbeat-lead.com"))
            lead["final_lead_score"] = 90
            lead["best_phone"] = "555-9"
            lead["emails"] = []
            if on_candidate:
                on_candidate(lead)
            return {"query": service_keyword, "leads": [lead], "count": 1}

        job_id = self._make_job(limit=5)

        with mock.patch("agents.lead_finding_agent.find_leads", delayed_find_leads):
            ja.run_job(job_id)

        final_job = ja.read_job(job_id)
        domains = [l.get("domain") for l in final_job["leads"]]
        self.assertEqual(domains, ["post-heartbeat-lead.com"])
        self.assertEqual(final_job["status"], "done")
        self.assertEqual(final_job["counts"]["found"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
