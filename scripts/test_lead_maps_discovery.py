"""Offline tests: Google Maps as the Lead Finder discovery source.

No network, no credentials, no paid calls. requests.post is patched to fail
loudly; the Maps request is served from the FULL real response captured on
2026-11-10 (66 items, $0.002). Every test runs in a temporary working
directory with a temporary spend ledger.

Run: venv/bin/python -m unittest scripts.test_lead_maps_discovery
"""

import csv
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents import dataforseo_maps_agent as maps  # noqa: E402
from agents import discovery_spend_guard as spend  # noqa: E402
from agents import lead_maps_discovery_agent as discovery  # noqa: E402

_REAL_RUN_MAPS_JOB = discovery.run_maps_job
FULL_FIXTURE = ROOT / "scripts" / "fixtures" / "dataforseo_maps_live_shirley_plumbers_20261110_full.json"


def real_envelope():
    data = json.loads(FULL_FIXTURE.read_text(encoding="utf-8"))
    data.pop("_note", None)
    return data


def real_items():
    return real_envelope()["tasks"][0]["result"][0]["items"]


class FakeResponse:
    def __init__(self, data, status_code=200):
        self._data, self.status_code = data, status_code

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class FakePost:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "payload": json})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakePage:
    def __init__(self, url, status_code=200, text="<html><body>ok</body></html>"):
        self.url, self.status_code, self.text = url, status_code, text


class Isolated(unittest.TestCase):
    """Temp cwd (data/, exports/ are relative), temp ledger, DataForSEO ON, Maps ON."""

    maps_enabled = "1"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        previous = Path.cwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, previous)
        (self.tmp / "data" / "leadbot_live_jobs").mkdir(parents=True)
        self.ledger = self.tmp / "data" / "ledger.json"
        spend.initialize_ledger(self.ledger)
        for p in (
            mock.patch.dict(os.environ, {
                "LEADBOT_MAPS_ENABLED": self.maps_enabled,
                "LEADBOT_DATAFORSEO_ENABLED": "1",
                "LEADBOT_DISCOVERY_LEDGER_PATH": str(self.ledger),
            }),
            mock.patch("requests.post", side_effect=AssertionError("real network call attempted")),
            mock.patch("agents.dataforseo_serp_agent._auth_header",
                       return_value={"Authorization": "Basic test", "Content-Type": "application/json"}),
        ):
            p.start()
            self.addCleanup(p.stop)

    def ledger_data(self):
        return json.loads(self.ledger.read_text(encoding="utf-8"))


# -- spend guard -------------------------------------------------------------------

class SpendGuardTests(Isolated):
    def guard(self, **kw):
        kw.setdefault("ledger_path", self.ledger)
        kw.setdefault("today", lambda: "2026-11-10")
        return spend.DiscoverySpendGuard(job_id="t", **kw)

    def test_defaults_match_policy(self):
        g = spend.DiscoverySpendGuard()
        self.assertEqual(g.max_scan_usd, 0.02)
        self.assertEqual(g.daily_max_usd, 0.25)
        self.assertEqual(g.max_calls["maps"], 1)

    def test_env_can_lower_but_never_raise_limits(self):
        with mock.patch.dict(os.environ, {"LEADBOT_DISCOVERY_MAX_SCAN_USD": "5",
                                          "LEADBOT_DISCOVERY_DAILY_MAX_USD": "99",
                                          "LEADBOT_MAPS_MAX_CALLS_PER_SCAN": "10"}):
            g = spend.DiscoverySpendGuard()
            self.assertEqual((g.max_scan_usd, g.daily_max_usd, g.max_calls["maps"]), (0.02, 0.25, 1))
        with mock.patch.dict(os.environ, {"LEADBOT_DISCOVERY_MAX_SCAN_USD": "0.005",
                                          "LEADBOT_DISCOVERY_DAILY_MAX_USD": "0.1",
                                          "LEADBOT_MAPS_MAX_CALLS_PER_SCAN": "0"}):
            g = spend.DiscoverySpendGuard()
            self.assertEqual((g.max_scan_usd, g.daily_max_usd, g.max_calls["maps"]), (0.005, 0.1, 0))

    def test_safe_mode_refuses_every_provider(self):
        with mock.patch.dict(os.environ, {"LEADBOT_DATAFORSEO_ENABLED": "0"}):
            for provider in ("maps", "organic"):
                with self.assertRaises(spend.SafeModeError):
                    self.guard().reserve(provider)
        self.assertEqual(self.ledger_data()["requests"], [])

    def test_missing_unreadable_and_corrupted_ledgers_fail_closed(self):
        self.ledger.unlink()
        with self.assertRaises(spend.LedgerUnavailable):
            self.guard().reserve("maps")
        self.assertFalse(self.ledger.exists())  # never silently recreated
        for bad in ("{not json", "[]", json.dumps({"schema": "other", "requests": []}),
                    json.dumps({"schema": spend.LEDGER_SCHEMA, "requests": [{"id": "x"}]}),
                    json.dumps({"schema": spend.LEDGER_SCHEMA, "requests": [
                        {"id": "x", "day": "2026-11-10", "status": "settled", "ceiling_usd": 0.01, "cost_usd": -1}]})):
            self.ledger.write_text(bad, encoding="utf-8")
            with self.assertRaises(spend.LedgerUnavailable):
                self.guard().reserve("maps")
            self.assertEqual(self.ledger.read_text(encoding="utf-8"), bad)

    def test_initialize_never_overwrites(self):
        g = self.guard()
        g.reserve("maps")
        before = self.ledger.read_text(encoding="utf-8")
        self.assertFalse(spend.initialize_ledger(self.ledger))
        self.assertEqual(self.ledger.read_text(encoding="utf-8"), before)

    def test_per_scan_ceiling_and_one_maps_request(self):
        g = self.guard()
        r = g.reserve("maps")
        g.record(0.002, res_id=r)
        with self.assertRaises(spend.SpendLimitExceeded):
            g.reserve("maps")  # 1 Maps request per scan
        g.reserve("organic")   # 0.002 + 0.01 <= 0.02
        with self.assertRaises(spend.SpendLimitExceeded):
            g.reserve("organic")  # 0.002 + 0.01 (open) + 0.01 > 0.02

    def test_daily_cap_shared_by_maps_and_organic_across_scans(self):
        for _ in range(2):
            g = self.guard(daily_max_usd=0.025, max_scan_usd=1.0)
            g.record(0.01, res_id=g.reserve("organic"))
        g = self.guard(daily_max_usd=0.025, max_scan_usd=1.0)
        with self.assertRaises(spend.SpendLimitExceeded):
            g.reserve("maps")  # 0.02 committed + 0.01 worst case > 0.025
        self.assertAlmostEqual(g.daily_spent(), 0.02)

    def test_restart_recovery_keeps_unsettled_reservation_at_ceiling(self):
        g = self.guard()
        g.reserve("maps")  # process "dies" here: never settled
        del g
        fresh = self.guard()
        self.assertAlmostEqual(fresh.daily_spent(), 0.01)
        reqs = self.ledger_data()["requests"]
        self.assertEqual([r["status"] for r in reqs], ["reserved"])

    def test_unknown_cost_charged_at_ceiling_and_records_details(self):
        g = self.guard()
        r = g.reserve("maps", keyword="plumbers")
        self.assertEqual(g.record(None, res_id=r, provider_status="request_failed"), 0.01)
        req = self.ledger_data()["requests"][0]
        self.assertEqual((req["status"], req["cost_usd"], req["cost_known"], req["provider_status"]),
                         ("settled", 0.01, False, "request_failed"))
        self.assertEqual(req["meta"]["keyword"], "plumbers")

    def test_release_unsent_frees_budget(self):
        g = self.guard()
        g.release_unsent(g.reserve("maps"))
        self.assertAlmostEqual(g.daily_spent(), 0.0)
        g.reserve("maps")  # the request cap was given back too

    def test_concurrent_processes_never_exceed_daily_cap(self):
        # 8 separate processes race for a $0.05/day cap at $0.01 worst case each.
        script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(ROOT)!r})
            from agents import discovery_spend_guard as s
            g = s.DiscoverySpendGuard(job_id="p", daily_max_usd=0.05, max_scan_usd=1.0,
                                      ledger_path={str(self.ledger)!r})
            try:
                g.reserve("organic"); print("OK")
            except s.SpendLimitExceeded:
                print("BLOCKED")
        """)
        env = dict(os.environ, LEADBOT_DATAFORSEO_ENABLED="1")
        procs = [subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True, env=env)
                 for _ in range(8)]
        outputs = [p.communicate(timeout=60)[0].strip() for p in procs]
        self.assertEqual(outputs.count("OK"), 5, outputs)
        self.assertEqual(outputs.count("BLOCKED"), 3, outputs)
        self.assertEqual(len(self.ledger_data()["requests"]), 5)
        self.assertAlmostEqual(spend.read_day_summary(path=self.ledger)["committed_usd"], 0.05)


# -- organic DataForSEO goes through the same guard -------------------------------------

class OrganicGuardTests(Isolated):
    maps_enabled = "0"

    def organic_ok(self, cost=0.0095):
        return mock.Mock(status_code=200, raise_for_status=mock.Mock(),
                         json=mock.Mock(return_value={"status_code": 20000, "cost": cost, "tasks": [
                             {"status_code": 20000, "cost": cost, "result": [{"items": []}]}]}))

    def test_organic_requests_are_reserved_and_capped_per_scan(self):
        from agents import dataforseo_serp_agent as serp
        post = mock.Mock(side_effect=[self.organic_ok(), self.organic_ok(), self.organic_ok()])
        with mock.patch.object(serp.requests, "post", post), \
                mock.patch.object(serp, "_circuit_breaker_gate", return_value="closed"):
            with spend.scan_guard(spend.DiscoverySpendGuard(job_id="scan")):
                serp.search_google_organic("plumber", "Shirley, NY")
                serp.search_google_organic("plumber", "Shirley, NY")
                with self.assertRaises(spend.SpendLimitExceeded):
                    serp.search_google_organic("plumber", "Shirley, NY")  # $0.019 + $0.01 > $0.02
        self.assertEqual(post.call_count, 2)
        reqs = self.ledger_data()["requests"]
        self.assertEqual([(r["provider"], r["cost_usd"]) for r in reqs], [("organic", 0.0095)] * 2)

    def test_organic_refuses_without_ledger(self):
        from agents import dataforseo_serp_agent as serp
        self.ledger.unlink()
        post = mock.Mock()
        with mock.patch.object(serp.requests, "post", post), \
                mock.patch.object(serp, "_circuit_breaker_gate", return_value="closed"):
            with self.assertRaises(spend.LedgerUnavailable):
                serp.search_google_organic("plumber", "Shirley, NY")
        post.assert_not_called()

    def test_maps_flag_off_keeps_the_organic_path(self):
        from agents import lead_live_job_agent as live
        with mock.patch.object(live, "_run_organic_job") as organic, \
                mock.patch.object(discovery, "run_maps_job") as maps_job:
            live.run_job("abc")
        organic.assert_called_once_with("abc")
        maps_job.assert_not_called()


# -- funnel against the real response ----------------------------------------------------

class RealResponseFunnelTests(Isolated):
    def funnel(self, limit=25, items=None, keyword="plumbers", blocked=()):
        return discovery.build_candidate_funnel(items if items is not None else real_items(),
                                                keyword, "Shirley, NY", limit, blocked)

    def test_counts_reconcile_exactly(self):
        f = self.funnel()
        c = f["counts"]
        self.assertEqual(c["provider_items"], 66)
        self.assertEqual((c["ads"], c["area_labels"], c["businesses"]), (2, 4, 60))
        self.assertEqual(c["ads"] + c["area_labels"] + c["businesses"], 66)
        self.assertEqual((c["duplicates_merged"], c["unique_businesses"]), (0, 60))
        self.assertEqual((c["off_category"], c["ineligible_no_contact"], c["blocked"], c["eligible"]), (10, 0, 0, 50))
        self.assertEqual((c["selected"], c["over_limit"]), (25, 25))
        self.assertEqual((c["eligible_with_website"], c["eligible_without_website"]), (24, 26))
        # The "64 raw" figure = the 64 maps_search items; the 2 ads are separate item types.
        items = real_items()
        self.assertEqual(sum(1 for i in items if i["type"] == "maps_search"), 64)
        self.assertEqual(sum(1 for i in items if i["type"] == "maps_paid_item"), 2)

    def test_off_trade_listings_rejected(self):
        rejected = set(self.funnel()["rejected"]["off_category"])
        self.assertEqual(rejected, {
            "Richard Supply Mastic Corporation", "Marrantino Plumbing & Heating",
            "Westhampton Plumbing & Heating Supply", "Ferguson Plumbing Supply",
            "Shirley Drywall Installation", "Alcus Fuel Oil", "Bailey Home Inspection",
            "BG Brothers Construction INC", "Harbor Freight", "A Level Above Carpentry",
        })

    def test_related_trades_named_for_plumbing_are_kept(self):
        f = self.funnel(limit=60)
        names = {r["title"]: r["relevance"] for r in f["selected"]}
        self.assertEqual(names["Frank Savins Plumbing & Heating"], "business_name")
        self.assertEqual(names["HiTech Sewer and Drain"], "primary_category")
        self.assertEqual(sum(1 for r in f["selected"] if r["category"] == "Plumber"), 46)

    def test_standard_scan_selects_top_25_in_maps_order(self):
        sel = self.funnel()["selected"]
        ranks = [r["rank_absolute"] for r in sel]
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual(sum(1 for r in sel if r["website"]), 15)
        self.assertEqual(sum(1 for r in sel if not r["website"]), 10)
        self.assertTrue(all(r["phone"] for r in sel))

    def test_duplicates_merge_but_distinct_place_ids_stay_separate(self):
        items = real_items()
        first = next(i for i in items if i.get("category") == "Plumber")
        dup = dict(first, rank_absolute=999)  # same place_id
        chain = dict(first, place_id="ChIJ_other_location", cid="999999", rank_absolute=1000,
                     address="1 Other St, Mastic, NY 11950")  # same brand, other location
        f = self.funnel(limit=60, items=items + [dup, chain])
        c = f["counts"]
        self.assertEqual(c["duplicates_merged"], 1)
        self.assertEqual(c["eligible"], 51)
        same_name = [r for r in f["selected"] if r["title"] == first["title"]]
        self.assertEqual(len(same_name), 2)
        if first.get("domain"):
            self.assertTrue(all(r["shared_domain_count"] == 2 for r in same_name))

    def test_closed_and_blocked_are_counted(self):
        items = real_items()
        target = next(i for i in items if i.get("category") == "Plumber" and i.get("url"))
        closed = next(i for i in items if i.get("category") == "Plumber" and i is not target and i.get("url"))
        closed = dict(closed, work_hours={"current_status": "closed_forever"})
        items = [closed if i.get("place_id") == closed.get("place_id") else i for i in items]
        domain = maps._host_of(maps.strip_tracking_params(target["url"]))
        f = self.funnel(limit=60, items=items, blocked={domain})
        c = f["counts"]
        self.assertEqual(c["closed_forever"], 1)
        self.assertEqual(c["blocked"], 1)
        self.assertEqual(c["eligible"], 48)

    def test_reconcile_detects_mismatch(self):
        c = dict(self.funnel()["counts"])
        c["eligible"] += 1
        with self.assertRaises(AssertionError):
            discovery.reconcile_funnel(c)


# -- end-to-end: Maps response -> live job -> cards -> CSV -> dashboard ----------------------

class EndToEndScanTests(Isolated):
    def make_job(self, limit=25, **params):
        from agents import lead_live_job_agent as live
        with mock.patch.object(live, "RUN_SCANS_IN_SUBPROCESS", False), \
                mock.patch("threading.Thread.start"):  # create only; we run it ourselves
            job_id = live.create_job({"industry": "plumbers", "keyword": "plumbers", "market": "Shirley, NY",
                                      "limit": limit, "owner_username": "owner", "owner_email": "owner@example.test",
                                      **params})
        return job_id

    def fake_fetch(self, url):
        host = maps._host_of(url)
        if host.startswith("hardy"):
            return FakePage("https://some-directory.example/listing")  # redirects off-site
        if host[:1] in "ab":
            return FakePage(url, status_code=503, text="")             # unreachable
        return FakePage(url)

    def fake_extract(self, url, market=""):
        host = maps._host_of(url)
        return {"emails": [f"office@{host}"], "best_phone": "", "phones": [],
                "contact_page_url": f"https://{host}/contact", "flags": []}

    def run_scan(self, job_id, post):
        from agents import lead_live_job_agent as live

        def maps_job(j):
            _REAL_RUN_MAPS_JOB(j, post=post, fetch=self.fake_fetch, extract_contact=self.fake_extract)

        with mock.patch("agents.lead_finding_agent.find_leads",
                        side_effect=AssertionError("organic search must not run")), \
                mock.patch.object(discovery, "run_maps_job", side_effect=maps_job):
            live.run_job(job_id)
        return live.read_job(job_id)

    def test_standard_scan_from_real_response(self):
        post = FakePost(FakeResponse(real_envelope()))
        job = self.run_scan(self.make_job(), post)

        self.assertEqual(len(post.calls), 1)
        self.assertEqual(post.calls[0]["url"], maps.ENDPOINTS["maps"])
        self.assertEqual(post.calls[0]["payload"][0]["depth"], 100)
        self.assertEqual(job["status"], "done", job.get("errors"))
        self.assertEqual(job["discovery_source"], "google_maps")
        self.assertEqual(len(job["leads"]), 25)
        self.assertEqual(job["result_note"], "Found 25 of 25 requested leads.")
        self.assertEqual(job["discovery"]["requests"], 1)
        self.assertEqual(job["discovery"]["funnel"]["eligible"], 50)

        leads = job["leads"]
        no_site = [l for l in leads if not l["url"]]
        with_site = [l for l in leads if l["url"]]
        self.assertEqual((len(with_site), len(no_site)), (15, 10))
        for lead in no_site:
            self.assertEqual(lead["website_flag"], "No Website")
            self.assertEqual(lead["website_status"], "missing")
            self.assertEqual((lead["domain"], lead["emails"], lead["contact_page_url"], lead["actions_domain"]),
                             ("", "", "", ""))
            self.assertTrue(lead["place_id"])
            self.assertTrue(lead["best_phone"])
            self.assertEqual(lead["meta_title"], "")
        verified = [l for l in with_site if l["website_verified"]]
        unverified = [l for l in with_site if not l["website_verified"]]
        self.assertTrue(verified and unverified)
        for lead in verified:
            self.assertEqual(lead["actions_domain"], lead["domain"])
            self.assertIn("@" + lead["domain"], lead["emails"])
        for lead in unverified:
            self.assertEqual(lead["website_status"], "unverified")
            self.assertEqual((lead["actions_domain"], lead["emails"], lead["contact_page_url"]), ("", "", ""))
        self.assertEqual(len({l["listing_key"] for l in leads}), 25)

        # Spend ledger: exactly one settled Maps request at the reported cost.
        reqs = self.ledger_data()["requests"]
        self.assertEqual(len(reqs), 1)
        self.assertEqual((reqs[0]["provider"], reqs[0]["status"], reqs[0]["cost_usd"], reqs[0]["results"]),
                         ("maps", "settled", 0.002, 66))
        self.assertEqual(reqs[0]["rejected"]["off_category"], 10)
        self.assertEqual(reqs[0]["rejected"]["area_labels"], 4)
        self.assertEqual(reqs[0]["rejected"]["ads"], 2)

        # CSV export with Maps columns; no guessed websites.
        rows = list(csv.DictReader((self.tmp / "exports" / job["export_file"]).open(encoding="utf-8")))
        self.assertEqual(len(rows), 25)
        for col in discovery.MAPS_EXPORT_FIELDS:
            self.assertIn(col, rows[0])
        csv_no_site = [r for r in rows if not r["url"]]
        self.assertEqual(len(csv_no_site), 10)
        self.assertTrue(all(r["website_flag"] == "No Website" and r["domain"] == "" and r["place_id"]
                            and r["website_verified"] == "false" for r in csv_no_site))
        owners = json.loads((self.tmp / "data" / "leadbot_export_owners.json").read_text())
        self.assertIn(job["export_file"], owners)

        # Saved-result cards: labels and actions follow verification.
        from agents.lead_dashboard_agent import lead_cards
        html = lead_cards(rows, selected_name=job["export_file"], csrf_token="t")
        self.assertEqual(html.count('class="lead-card"'), 25)
        self.assertEqual(html.count(">No Website</span>"), 10)
        # Add to CRM is offered for every listing (identity = place ID); Compare only with a verified website.
        self.assertEqual(html.count('class="lead-add-crm"'), 25)
        self.assertEqual(html.count('class="lead-top-competitor"'), len(verified))
        self.assertNotIn("leadbot-missing-pill", html)

    def test_fewer_eligible_than_requested_reports_reasons(self):
        job = self.run_scan(self.make_job(limit=60), FakePost(FakeResponse(real_envelope())))
        self.assertEqual(job["status"], "done")
        self.assertEqual(len(job["leads"]), 50)
        note = job["result_note"]
        self.assertTrue(note.startswith("Found 50 of 60 requested leads."), note)
        self.assertIn("Google Maps listed 60 businesses", note)
        self.assertIn("10 were a different type of business", note)

    def test_zero_results(self):
        data = real_envelope()
        data["tasks"][0]["status_code"] = 40102
        data["tasks"][0]["result"] = None
        job = self.run_scan(self.make_job(), FakePost(FakeResponse(data)))
        self.assertEqual((job["status"], job["leads"], job["export_file"]), ("done", [], ""))
        self.assertIn("Found 0 of 25 requested leads.", job["result_note"])
        self.assertIn("returned no businesses", job["result_note"])

    def test_safe_mode_sends_nothing_and_never_falls_back(self):
        post = FakePost()
        with mock.patch.dict(os.environ, {"LEADBOT_DATAFORSEO_ENABLED": "0"}):
            job = self.run_scan(self.make_job(), post)
        self.assertEqual((job["status"], job["error_code"]), ("error", "discovery_safe_mode"))
        self.assertEqual(post.calls, [])
        self.assertEqual(self.ledger_data()["requests"], [])

    def test_daily_budget_reached_sends_nothing(self):
        data = self.ledger_data()
        data["requests"].append({"id": "old", "day": spend.utc_day(), "provider": "organic", "status": "settled",
                                 "ceiling_usd": 0.01, "cost_usd": 0.245})
        self.ledger.write_text(json.dumps(data), encoding="utf-8")
        post = FakePost()
        job = self.run_scan(self.make_job(), post)
        self.assertEqual((job["status"], job["error_code"]), ("error", "discovery_budget_reached"))
        self.assertEqual(post.calls, [])

    def test_missing_ledger_sends_nothing(self):
        self.ledger.unlink()
        post = FakePost()
        job = self.run_scan(self.make_job(), post)
        self.assertEqual((job["status"], job["error_code"]), ("error", "discovery_ledger_unavailable"))
        self.assertEqual(post.calls, [])

    def test_provider_failure_no_retry_no_fallback(self):
        post = FakePost(ConnectionError("boom"), FakeResponse(real_envelope()))
        job = self.run_scan(self.make_job(), post)
        self.assertEqual((job["status"], job["error_code"]), ("error", "search_provider_unavailable"))
        self.assertEqual(len(post.calls), 1)
        req = self.ledger_data()["requests"][0]
        self.assertEqual((req["cost_usd"], req["cost_known"]), (0.01, False))

    def test_cancel_before_request_spends_nothing(self):
        from agents import lead_live_job_agent as live
        job_id = self.make_job()
        job = live.read_job(job_id)
        job["cancel_requested"] = True
        live.write_job(job)
        post = FakePost()
        job = self.run_scan(job_id, post)
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(post.calls, [])

    def test_preserves_existing_records(self):
        exports = self.tmp / "exports"
        exports.mkdir()
        old_csv = exports / "leads_old.csv"
        old_csv.write_text("title,domain\nOld,old.example\n", encoding="utf-8")
        owners = self.tmp / "data" / "leadbot_export_owners.json"
        owners.write_text(json.dumps({"leads_old.csv": {"owner_username": "x"}}), encoding="utf-8")
        cache = self.tmp / "data" / "leadbot_business_cache.json"
        cache.write_text('{"old.example": {"phone": "1"}}', encoding="utf-8")
        job = self.run_scan(self.make_job(), FakePost(FakeResponse(real_envelope())))
        self.assertEqual(job["status"], "done")
        self.assertEqual(old_csv.read_text(encoding="utf-8"), "title,domain\nOld,old.example\n")
        self.assertEqual(cache.read_text(encoding="utf-8"), '{"old.example": {"phone": "1"}}')
        self.assertIn("leads_old.csv", json.loads(owners.read_text()))


# -- live page + CRM ----------------------------------------------------------------------

class LivePageTests(unittest.TestCase):
    def test_live_page_has_maps_renderer_and_status_lines(self):
        source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        for needle in ("function renderMapsLead(lead, jobParams)", 'lead.discovery_source === "google_maps"',
                       "requested leads.", "discovery_safe_mode", "No Website",
                       "Listed on Google Maps · not verified", "div.dataset.crmDomain = String(lead.actions_domain"):
            self.assertIn(needle, source)


class MapsCrmTests(unittest.TestCase):
    """The existing CRM route harness, fed a Google Maps export."""

    def setUp(self):
        import scripts.test_leadbot_crm_import as crm_harness
        crm_harness.CrmImportTests.setUp(self)
        fields = ["title", "domain", "url", "best_phone", "keyword", "market", "website_status",
                  "website_verified", "website_flag", "place_id", "address"]
        rows = [
            dict(title="No Site Plumbing", domain="", url="", best_phone="631-555-0101", keyword="plumbers",
                 market="Shirley, NY", website_status="missing", website_verified="false",
                 website_flag="No Website", place_id="ChIJ_none", address="1 Main St"),
            dict(title="Unverified Plumbing", domain="unverified.example", url="https://unverified.example/",
                 best_phone="631-555-0102", keyword="plumbers", market="Shirley, NY", website_status="unverified",
                 website_verified="false", website_flag="", place_id="ChIJ_unv", address="2 Main St"),
            dict(title="Chain Plumbing Shirley", domain="chain.example", url="https://chain.example/",
                 best_phone="631-555-0103", keyword="plumbers", market="Shirley, NY", website_status="verified",
                 website_verified="true", website_flag="", place_id="ChIJ_c1", address="3 Main St"),
            dict(title="Chain Plumbing Mastic", domain="chain.example", url="https://chain.example/",
                 best_phone="631-555-0104", keyword="plumbers", market="Shirley, NY", website_status="verified",
                 website_verified="true", website_flag="", place_id="ChIJ_c2", address="4 Main St"),
            dict(title="Good Plumbing", domain="good.example", url="https://good.example/",
                 best_phone="631-555-0105", keyword="plumbers", market="Shirley, NY", website_status="verified",
                 website_verified="true", website_flag="", place_id="ChIJ_g", address="5 Main St"),
        ]
        with Path("exports", self.file).open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def post(self, domain, name, listing_key=""):
        self.payload.update(source_domain=domain, listing_key=listing_key, lead=dict(company_name=name))
        return self.client.post("/lead-bot/add-to-crm", json=self.payload)

    def test_maps_rows_are_never_selected_by_domain(self):
        for domain in ("", "!!!", "   ", "chain.example", "good.example"):
            self.assertIn(self.post(domain, "x").status_code, (403, 422))
        self.transport.assert_not_called()

    def test_every_listing_imports_by_place_id_with_only_verified_websites(self):
        # Approved 2026-11-10: businesses without (verified) websites can be added to the CRM.
        cases = [("place_id:ChIJ_none", ""), ("place_id:ChIJ_unv", ""), ("place_id:ChIJ_c1", "https://chain.example/"),
                 ("place_id:ChIJ_c2", "https://chain.example/"), ("place_id:ChIJ_g", "https://good.example/")]
        for key, website in cases:
            self.assertEqual(self.post("", "x", key).status_code, 200, key)
            self.assertEqual(json.loads(self.transport.call_args.kwargs["data"])["lead"]["website"], website, key)
        self.assertEqual(self.transport.call_count, len(cases))


class Python310CompatibilityTests(unittest.TestCase):
    CHANGED = ["agents/discovery_spend_guard.py", "agents/lead_maps_discovery_agent.py",
               "agents/dataforseo_maps_agent.py", "agents/dataforseo_serp_agent.py",
               "agents/lead_live_job_agent.py", "agents/lead_export_agent.py",
               "agents/lead_dashboard_agent.py", "app/main.py", "scripts/leadbot_discovery_ledger.py",
               "agents/lead_row_identity.py", "agents/lead_finding_agent.py", "business_competitor_finder.py",
               "scripts/test_maps_row_actions.py", "scripts/test_crm_maps_import.py",
               "agents/actii_queue_agent.py", "agents/actii_pull_connector.py", "agents/actii_integration_agent.py",
               "agents/actii_search_metadata_agent.py"]

    def test_changed_files_parse_as_python_310(self):
        import ast
        for rel in self.CHANGED:
            ast.parse((ROOT / rel).read_text(encoding="utf-8"), filename=rel, feature_version=(3, 10))

    def test_no_311_only_stdlib_names_in_new_code(self):
        for rel in self.CHANGED[:3] + ["scripts/leadbot_discovery_ledger.py"]:
            text = (ROOT / rel).read_text(encoding="utf-8")
            for needle in ("datetime.UTC", "from datetime import UTC", "import tomllib", "ExceptionGroup",
                           "typing import Self", "except*", "enterContext"):
                self.assertNotIn(needle, text, f"{needle} in {rel}")


if __name__ == "__main__":
    unittest.main()
