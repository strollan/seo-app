"""Regression coverage for excluding organic SERP positions 1-10 from new leads."""

import shutil
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import agents.lead_finding_agent as finding_agent
import agents.lead_live_job_agent as job_agent
import business_competitor_finder as competitor_finder


def _raw_lead(position, domain=None):
    domain = domain or f"position-{position}.example"
    return {
        "title": f"Business at {position}",
        "url": f"https://{domain}/",
        "domain": domain,
        "snippet": "local plumber services",
        "serp_page": ((position - 1) // 10) + 1,
        "serp_position": position,
    }


def _final_lead(position, domain=None):
    lead = _raw_lead(position, domain)
    lead.update({
        "score": 85,
        "final_lead_score": 80,
        "best_phone": "555-0100",
        "emails": ["owner@example.com"],
    })
    return lead


class FindingAgentPageOneTests(unittest.TestCase):
    def test_positions_one_through_ten_are_skipped_before_downstream_work(self):
        rows = [_raw_lead(position) for position in range(1, 12)]
        scored_positions = []
        contacted_positions = []
        streamed_positions = []

        original_score = finding_agent.score_lead

        def track_score(item, industry, market):
            scored_positions.append(item["serp_position"])
            return original_score(item, industry, market)

        def track_contact(url, market=""):
            position = int(url.split("position-")[1].split(".")[0])
            contacted_positions.append(position)
            return {
                "best_phone": "555-0100",
                "phones": ["555-0100"],
                "emails": ["owner@example.com"],
                "contact_page_url": f"{url}contact",
                "confidence": 80,
                "flags": [],
            }

        def fake_search(query, own_domain=None, location="", limit=20, pages=None):
            return rows if (pages or [1])[0] == 1 else []

        with (
            mock.patch.object(finding_agent, "find_business_competitors", fake_search),
            mock.patch.object(finding_agent, "score_lead", track_score),
            mock.patch.object(finding_agent, "extract_contact_from_url", track_contact),
            mock.patch.object(finding_agent, "_lead_bot_fast_mode", lambda: False),
        ):
            result = finding_agent.find_leads(
                "plumber", "Albany, NY", "plumber", limit=20,
                on_candidate=lambda lead: streamed_positions.append(int(lead["serp_position"])),
            )

        self.assertEqual(scored_positions, [11])
        self.assertEqual(contacted_positions, [11])
        self.assertEqual(streamed_positions, [11])
        self.assertEqual([lead["serp_position"] for lead in result["leads"]], [11])
        self.assertEqual(result["count"], 1)

    def test_positions_eleven_twenty_thirty_and_forty_keep_real_positions(self):
        rows = [_raw_lead(position) for position in (11, 20, 30, 40)]

        def fake_search(query, own_domain=None, location="", limit=20, pages=None):
            return rows if (pages or [1])[0] == 1 else []

        contact = {
            "best_phone": "555-0100", "phones": ["555-0100"],
            "emails": ["owner@example.com"], "contact_page_url": "",
            "confidence": 80, "flags": [],
        }
        with (
            mock.patch.object(finding_agent, "find_business_competitors", fake_search),
            mock.patch.object(finding_agent, "extract_contact_from_url", lambda url, market="": contact),
            mock.patch.object(finding_agent, "_lead_bot_fast_mode", lambda: False),
        ):
            result = finding_agent.find_leads("plumber", "Albany, NY", "plumber", limit=10)

        self.assertEqual(
            [lead["serp_position"] for lead in result["leads"]],
            [11, 20, 30, 40],
        )

    def test_google_places_positions_are_not_treated_as_organic_page_one(self):
        place = _raw_lead(1, "maps-business.example")
        place.update({"source": "google_places", "places_position": 1})
        self.assertFalse(finding_agent._leadbot_is_page_one_organic_result(place))


class LiveJobPageOneSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        original_job_dir = job_agent.JOB_DIR
        job_agent.JOB_DIR = Path(self.tmpdir)
        self.addCleanup(lambda: setattr(job_agent, "JOB_DIR", original_job_dir))

        self.export_calls = []
        patches = [
            mock.patch(
                "agents.lead_business_cache_agent.apply_cached_business_to_lead",
                lambda lead: (lead, False),
            ),
            mock.patch(
                "agents.lead_business_cache_agent.save_business_from_lead",
                lambda lead, enriched=False: None,
            ),
            mock.patch(
                "agents.lead_export_agent.export_leads_to_csv",
                self._capture_export,
            ),
            mock.patch.object(job_agent, "_leadbot_enrich_live_address", lambda lead, market="": lead),
            mock.patch.object(job_agent.time, "sleep", lambda seconds: None),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _capture_export(self, payload, **kwargs):
        self.export_calls.append(payload)
        return {"path": "/tmp/page-one-filter.csv"}

    def _make_job(self, max_queries=1):
        job_id = "page-one-" + uuid.uuid4().hex[:12]
        job_agent.write_job({
            "job_id": job_id,
            "status": "queued",
            "message": "",
            "leads": [],
            "errors": [],
            "counts": {"found": 0, "cached": 0, "enriched": 0, "needs_research": 0},
            "seen_domains": [],
            "params": {
                "industry": "plumber", "market": "Albany, NY", "keyword": "plumber",
                "own_domain": "", "limit": 10, "per_batch": 8,
                "per_query_limit": 8, "max_queries": max_queries, "guest_id": "",
            },
            "cancel_requested": False,
            "updated_at": job_agent.now_iso(),
            "export_file": "",
        })
        return job_id

    def test_stream_persistence_counts_and_export_exclude_page_one(self):
        rows = [_final_lead(position) for position in (1, 10, 11, 20, 30, 40)]

        def fake_find_leads(*args, on_candidate=None, **kwargs):
            for lead in rows:
                if on_candidate:
                    on_candidate(lead)
            return {"leads": rows, "count": len(rows)}

        job_id = self._make_job()
        with mock.patch("agents.lead_finding_agent.find_leads", fake_find_leads):
            job_agent.run_job(job_id)

        final_job = job_agent.read_job(job_id)
        expected = ["11", "20", "30", "40"]
        self.assertEqual([lead["serp_position"] for lead in final_job["leads"]], expected)
        self.assertEqual(final_job["counts"]["found"], 4)
        self.assertEqual(len(self.export_calls), 1)
        self.assertEqual(
            [str(lead["serp_position"]) for lead in self.export_calls[0]["leads"]],
            expected,
        )

    def test_partial_job_excludes_page_one_but_keeps_valid_results(self):
        calls = {"count": 0}

        def partial_find_leads(*args, on_candidate=None, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                rows = [_final_lead(5), _final_lead(11)]
                for lead in rows:
                    if on_candidate:
                        on_candidate(lead)
                return {"leads": rows, "count": len(rows)}
            raise competitor_finder.SearchProviderUnavailableError("simulated")

        job_id = self._make_job(max_queries=2)
        with mock.patch("agents.lead_finding_agent.find_leads", partial_find_leads):
            job_agent.run_job(job_id)

        final_job = job_agent.read_job(job_id)
        self.assertTrue(final_job["partial"])
        self.assertEqual([lead["serp_position"] for lead in final_job["leads"]], ["11"])
        self.assertEqual(final_job["counts"]["found"], 1)
        self.assertEqual(
            [str(lead["serp_position"]) for lead in self.export_calls[0]["leads"]],
            ["11"],
        )

    def test_legacy_job_and_export_rows_remain_readable(self):
        legacy_job = {
            "job_id": "legacy-job",
            "status": "done",
            "leads": [_final_lead(5, "legacy.example")],
            "export_file": "legacy.csv",
        }
        job_agent.write_job(legacy_job)

        loaded = job_agent.read_job("legacy-job")
        self.assertEqual(loaded["leads"][0]["serp_position"], 5)
        self.assertEqual(
            job_agent.normalize_lead_results({"leads": [_final_lead(5)]})[0]["serp_position"],
            5,
        )


if __name__ == "__main__":
    unittest.main()
