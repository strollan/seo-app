"""Regression tests: Lead Finder must never treat Google redirect URLs as websites.

Production report (2026-11-09): a 25-lead Standard scan in Shirley, NY finished
with 1 Found, and its cards showed google.com/goto?... as the Website
("Water Line Repair", "Authentic NY Bagels & Deli").

Root cause, reproduced offline with scripts/fixtures/lead_finder_google_urls:
when search rows link through a Google redirect, every row's domain becomes
"google.com". find_leads() and run_job() dedupe leads by domain, so the first
Google-linked row claimed "google.com" and every later one was discarded as a
duplicate -- one lead per scan. The contact crawl then ran against Google
pages, and the domain-keyed business cache could mix businesses together.

Everything here is offline: search rows come from a fixture, contact crawls
are stubbed, and requests is blocked. No DataForSEO/OpenAI/Places call is made.
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # the finder reads data/ blocklists relative to the app root

import agents.lead_finding_agent as finding_agent  # noqa: E402
import agents.lead_live_job_agent as job_agent  # noqa: E402
import business_competitor_finder as competitor_finder  # noqa: E402
from agents import business_website as bw  # noqa: E402
from agents.actii_queue_agent import QueueError, verify_website  # noqa: E402
from agents.site_audit_agent import AuditLimiter, run_site_audit  # noqa: E402

FIXTURE = ROOT / "scripts" / "fixtures" / "lead_finder_google_urls" / "shirley_plumber_rows.json"

# What the fixture actually supports: 6 distinct businesses with a real website.
EXPECTED_DOMAINS = {
    "shirleywaterline-fixture.com",   # google.com/goto?url=... unwrapped
    "masticplumbing-fixture.com",     # google.com/url?q=... unwrapped
    "southshoreplumber-fixture.com",  # listed twice -> one lead
    "brookhavenpipe-fixture.com",
    "morichesleak-fixture.com",       # google.com/aclk?adurl=... unwrapped
    "coramsewer-fixture.com",
}


def _blocked(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


def _fixture_rows():
    rows = []
    for row in json.loads(FIXTURE.read_text(encoding="utf-8")):
        rank = row.pop("rank")
        row.update({
            "source": "dataforseo",
            "lead_source_label": "Google Organic",
            "serp_page": (rank - 1) // 10 + 1,
            "serp_position": rank,
            "rank_group": rank,
        })
        rows.append(row)
    return rows


class OfflineScan(unittest.TestCase):
    def setUp(self):
        self.crawled = []
        patches = [
            mock.patch("requests.get", _blocked),
            mock.patch("requests.post", _blocked),
            mock.patch("requests.Session.request", _blocked),
            mock.patch.object(competitor_finder, "_leadbot_use_live_serp", lambda: False),
            mock.patch.object(competitor_finder, "_leadbot_non_serper_search", self._search),
            mock.patch.object(competitor_finder, "_leadbot_fast_blocked_domains", lambda: set()),
            mock.patch.object(finding_agent, "extract_contact_from_url", self._contact),
            mock.patch.object(finding_agent, "_lead_bot_fast_mode", lambda: False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _search(self, keyword, location="", page=1, num=10, raise_on_failure=False):
        return _fixture_rows() if int(page or 1) == 1 else []

    def _contact(self, url, market=""):
        # Stand-in crawler: the contact page always lives on the crawled site.
        self.crawled.append(url)
        host = bw._host(url)
        return {
            "best_phone": "631-555-0100",
            "phones": ["631-555-0100"],
            "emails": [f"office@{host}"],
            "contact_page_url": f"https://{host}/contact",
            "confidence": 80,
            "flags": [],
        }

    def scan(self, limit=25):
        return finding_agent.find_leads("plumbing", "Shirley, NY", "plumber", limit=limit)["leads"]


class LeadFinderScanTests(OfflineScan):
    def test_multiple_leads_in_one_scan(self):
        leads = self.scan()
        self.assertGreater(len(leads), 1, "Google-linked rows collapsed into a single lead")
        self.assertEqual({lead["domain"] for lead in leads}, EXPECTED_DOMAINS)

    def test_google_redirects_are_never_a_website_or_domain(self):
        for lead in self.scan():
            for key in ("url", "domain", "contact_page_url"):
                self.assertFalse(bw.is_google_host(lead.get(key) or ""), f"{key}={lead.get(key)!r}")
        self.assertFalse([u for u in self.crawled if bw.is_google_host(u)], "contact crawl hit a Google URL")

    def test_redirect_target_is_extracted(self):
        by_domain = {lead["domain"]: lead for lead in self.scan()}
        self.assertEqual(by_domain["shirleywaterline-fixture.com"]["url"], "https://www.shirleywaterline-fixture.com/")
        self.assertEqual(by_domain["masticplumbing-fixture.com"]["url"], "https://masticplumbing-fixture.com/services/")
        self.assertEqual(by_domain["morichesleak-fixture.com"]["url"], "https://morichesleak-fixture.com/shirley")

    def test_businesses_with_no_website_are_not_saved_under_google(self):
        titles = " | ".join(lead["title"] for lead in self.scan())
        # Only a Google link, no destination: no verified website -> not a domain lead.
        self.assertNotIn("Authentic NY Bagels", titles)
        self.assertNotIn("Shirley Drain Pros", titles)

    def test_duplicate_business_is_one_lead(self):
        domains = [lead["domain"] for lead in self.scan()]
        self.assertEqual(domains.count("southshoreplumber-fixture.com"), 1)
        self.assertEqual(len(domains), len(set(domains)))

    def test_fewer_results_than_requested_limit(self):
        leads = self.scan(limit=25)
        self.assertEqual(len(leads), len(EXPECTED_DOMAINS))  # 6 < 25, no padding, no error

    def test_contact_and_crm_domain_association(self):
        for lead in self.scan():
            domain = lead["domain"]
            self.assertEqual(bw._host(lead["url"]), domain)
            self.assertEqual(bw._host(lead["contact_page_url"]), domain)
            self.assertTrue(all(e.endswith("@" + domain) for e in lead["emails"]))
            # The same website/domain pair is accepted by the ACT II queue check.
            verify_website({"website": lead["url"], "company_name": lead["title"]}, domain)


class BusinessWebsiteTests(unittest.TestCase):
    def test_unwraps_only_real_non_google_targets(self):
        cases = {
            "https://www.google.com/goto?url=https%3A%2F%2Fbiz-fixture.com%2F": ("https://biz-fixture.com/", bw.UNWRAPPED),
            "https://www.google.com/url?q=http://biz-fixture.com/a&sa=U": ("http://biz-fixture.com/a", bw.UNWRAPPED),
            "https://www.google.co.uk/url?url=https://www.google.com/url?q=https://biz-fixture.com/": ("https://biz-fixture.com/", bw.UNWRAPPED),
            "https://www.google.com/goto?sa=t&ved=abc": ("", bw.MISSING),
            "https://www.google.com/url?q=https://maps.google.com/x": ("", bw.MISSING),
            "https://www.google.com/url?q=javascript:alert(1)": ("", bw.MISSING),
            "https://maps.google.com/?cid=1": ("", bw.MISSING),
            "https://goo.gl/maps/abc": ("", bw.MISSING),
            "https://biz-fixture.com/": ("https://biz-fixture.com/", bw.WEBSITE),
            "https://sites.google.com/view/biz": ("https://sites.google.com/view/biz", bw.WEBSITE),
            "": ("", bw.MISSING),
        }
        for raw, (url, status) in cases.items():
            self.assertEqual(bw.resolve_business_website(raw), {"url": url, "status": status}, raw)

    def test_google_lookalikes_are_not_google(self):
        for host in ("google-plumbing.com", "mygoogle.com", "googleplumbers.net", "biz.com"):
            self.assertFalse(bw.is_google_host(host), host)
        for host in ("google.com", "www.google.com", "maps.google.com", "google.co.uk", "g.page"):
            self.assertTrue(bw.is_google_host(host), host)

    def test_no_website_rows_never_share_a_key(self):
        a = bw.no_website_dedupe_key({"title": "Authentic NY Bagels & Deli"})
        b = bw.no_website_dedupe_key({"title": "Shirley Drain Pros"})
        self.assertTrue(a and b)
        self.assertNotEqual(a, b)


class DisplayAndDownstreamGuardTests(unittest.TestCase):
    def test_saved_google_lead_displays_missing_website(self):
        public = job_agent.lead_to_public({
            "title": "Authentic NY Bagels & Deli",
            "domain": "google.com",
            "url": "https://www.google.com/goto?sa=t&ved=x",
            "contact_page_url": "https://www.google.com/contact",
            "best_phone": "631-555-0199",
            "address": "1 Main St, Shirley, NY",
        })
        self.assertEqual((public["url"], public["domain"], public["contact_page_url"]), ("", "", ""))
        self.assertEqual(public["website_status"], "missing")
        # Name, phone and location are preserved.
        self.assertEqual(public["title"], "Authentic NY Bagels & Deli")
        self.assertEqual(public["best_phone"], "631-555-0199")
        self.assertEqual(public["address"], "1 Main St, Shirley, NY")

    def test_saved_redirect_with_target_displays_real_website(self):
        public = job_agent.lead_to_public({"url": "https://www.google.com/goto?url=https://biz-fixture.com/"})
        self.assertEqual(public["url"], "https://biz-fixture.com/")

    def test_normal_lead_display_is_unchanged(self):
        public = job_agent.lead_to_public({"url": "biz-fixture.com", "domain": "biz-fixture.com"})
        self.assertEqual((public["url"], public["domain"]), ("biz-fixture.com", "biz-fixture.com"))

    def test_crm_rejects_google_website(self):
        with self.assertRaises(QueueError):
            verify_website({"website": "https://www.google.com/goto?sa=t", "company_name": "Biz"}, "google.com")

    def test_site_audit_rejects_google_url_without_fetching(self):
        fetched = []
        status, body = run_site_audit(
            {"connector_id": "c1"},
            {"request_id": "a" * 32, "url": "https://www.google.com/goto?url=https://biz-fixture.com/"},
            fetch=lambda u: fetched.append(u) or {},
            validate=lambda u: None,
            sanitize=lambda u: u,
            limiter=AuditLimiter(),
        )
        self.assertEqual(status, 422, body)
        self.assertEqual(fetched, [])



class ContactPageValidationTests(OfflineScan):
    def _contact(self, url, market=""):
        self.crawled.append(url)
        host = bw._host(url)
        if host == "masticplumbing-fixture.com":
            page = "https://www.google.com/contact"          # crawl was redirected to Google
        elif host == "coramsewer-fixture.com":
            page = "https://some-booking-platform-fixture.com/coram"  # another site entirely
        else:
            page = f"https://www.{host}/contact-us"         # same site (www is the same site)
        return {"best_phone": "631-555-0100", "phones": ["631-555-0100"], "emails": [],
                "contact_page_url": page, "confidence": 70, "flags": []}

    def test_contact_page_must_be_on_the_business_site(self):
        by_domain = {lead["domain"]: lead for lead in self.scan()}
        self.assertEqual(by_domain["masticplumbing-fixture.com"]["contact_page_url"], "")
        self.assertEqual(by_domain["coramsewer-fixture.com"]["contact_page_url"], "")
        self.assertIn("contact_page_offsite_dropped", by_domain["coramsewer-fixture.com"]["contact_flags"])
        self.assertEqual(by_domain["brookhavenpipe-fixture.com"]["contact_page_url"],
                         "https://www.brookhavenpipe-fixture.com/contact-us")
        # The phone found on the business's own page is kept.
        self.assertEqual(by_domain["coramsewer-fixture.com"]["best_phone"], "631-555-0100")


class DataForSEOSwitchTests(unittest.TestCase):
    """The real _leadbot_non_serper_search with the switch OFF and ON (provider mocked)."""

    def setUp(self):
        self.provider_calls = []
        for p in [
            mock.patch("requests.get", _blocked),
            mock.patch("requests.post", _blocked),
            mock.patch("requests.Session.request", _blocked),
            mock.patch.object(competitor_finder, "_leadbot_use_live_serp", lambda: False),
            mock.patch.object(competitor_finder, "_leadbot_fast_blocked_domains", lambda: set()),
            mock.patch("agents.dataforseo_serp_agent.search_google_organic", self._provider),
            mock.patch.object(finding_agent, "extract_contact_from_url", OfflineScan._contact.__get__(self)),
            mock.patch.object(finding_agent, "_lead_bot_fast_mode", lambda: False),
        ]:
            p.start()
            self.addCleanup(p.stop)
        self.crawled = []

    def _provider(self, query, location, depth=40):
        self.provider_calls.append((query, location, depth))
        return _fixture_rows()

    def test_switch_off_is_a_provider_failure_not_a_one_lead_scan(self):
        with mock.patch.dict(os.environ, {"LEADBOT_DATAFORSEO_ENABLED": "0"}):
            with self.assertRaises(competitor_finder.SearchProviderUnavailableError):
                finding_agent.find_leads("plumbing", "Shirley, NY", "plumber", limit=25)
        self.assertEqual(self.provider_calls, [])

    def test_switch_on_returns_every_verified_business(self):
        with mock.patch.dict(os.environ, {"LEADBOT_DATAFORSEO_ENABLED": "1"}):
            leads = finding_agent.find_leads("plumbing", "Shirley, NY", "plumber", limit=25)["leads"]
        self.assertEqual({lead["domain"] for lead in leads}, EXPECTED_DOMAINS)
        # One paid request per query: pages 2-4 are served by the same deep request.
        self.assertEqual(len(self.provider_calls), 1)


def _many_google_rows(count, with_target=True):
    rows = []
    for i in range(count):
        site = f"https://biz{i:02d}-plumber-fixture.com/"
        link = (f"https://www.google.com/goto?url={site}&sa=t" if with_target
                else f"https://www.google.com/goto?sa=t&ved=row{i}")
        rows.append({"title": f"Business {i:02d} Plumber Shirley NY", "link": link,
                     "snippet": "Plumber services in Shirley, NY.", "source": "dataforseo",
                     "serp_page": 2 + i // 10, "serp_position": 11 + i, "rank_group": 11 + i})
    return rows


class RunJobScanSizeTests(unittest.TestCase):
    """run_job() end to end: 25-lead scans, dedupe across query variants, status."""

    def setUp(self):
        import shutil
        import tempfile

        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        orig_dir = job_agent.JOB_DIR
        job_agent.JOB_DIR = Path(self.tmpdir)
        self.addCleanup(setattr, job_agent, "JOB_DIR", orig_dir)
        self.rows = []
        self.calls = 0
        for p in [
            mock.patch("requests.get", _blocked),
            mock.patch("requests.post", _blocked),
            mock.patch("requests.Session.request", _blocked),
            mock.patch.object(competitor_finder, "_leadbot_use_live_serp", lambda: False),
            mock.patch.object(competitor_finder, "_leadbot_non_serper_search", self._search),
            mock.patch.object(competitor_finder, "_leadbot_fast_blocked_domains", lambda: set()),
            mock.patch.object(finding_agent, "extract_contact_from_url", OfflineScan._contact.__get__(self)),
            mock.patch.object(finding_agent, "_lead_bot_fast_mode", lambda: False),
            mock.patch("agents.lead_business_cache_agent.apply_cached_business_to_lead", lambda lead: (lead, False)),
            mock.patch("agents.lead_business_cache_agent.save_business_from_lead", lambda lead, enriched=False: None),
            mock.patch("agents.lead_export_agent.export_leads_to_csv", lambda payload: {"filename": "test.csv"}),
            mock.patch.object(job_agent, "_leadbot_enrich_live_address", lambda row, market="": row),
            mock.patch.object(job_agent, "_leadbot_sync_live_addresses_to_export", lambda job: 0),
        ]:
            p.start()
            self.addCleanup(p.stop)
        self.crawled = []

    def _search(self, keyword, location="", page=1, num=10, raise_on_failure=False):
        if int(page or 1) != 1:
            return []
        # Each query variant surfaces the next slice of businesses, like real
        # variants ("plumber Shirley NY", "Shirley NY plumber", ...) do.
        start = self.calls * 8
        self.calls += 1
        return [dict(r) for r in self.rows[start:]]

    def run_scan(self, limit=25):
        job_id = "lfgoogle-" + os.urandom(6).hex()
        job_agent.write_job({
            "job_id": job_id, "status": "queued", "message": "", "leads": [], "errors": [],
            "counts": {"found": 0, "cached": 0, "enriched": 0, "needs_research": 0},
            "seen_domains": [],
            "params": {"industry": "plumbing", "market": "Shirley, NY", "keyword": "plumber",
                       "own_domain": "", "limit": limit, "per_batch": 8, "per_query_limit": 8,
                       "max_queries": 8, "guest_id": ""},
            "cancel_requested": False, "updated_at": job_agent.now_iso(),
        })
        job_agent.run_job(job_id)
        return job_agent.read_job(job_id)

    def test_25_lead_scan_of_google_linked_businesses_fills_to_25(self):
        self.rows = _many_google_rows(40)
        job = self.run_scan(limit=25)
        domains = [lead["domain"] for lead in job["leads"]]
        self.assertEqual(job["status"], "done", job.get("message"))
        self.assertEqual(len(domains), 25)              # was 1: every row shared "google.com"
        self.assertEqual(len(set(domains)), 25)
        self.assertFalse([d for d in domains if bw.is_google_host(d)])
        self.assertEqual(job["counts"]["found"], 25)
        self.assertEqual(job["requested"], 25)
        self.assertEqual(job["result_note"], "")

    def test_short_scan_reports_found_of_requested(self):
        self.rows = _many_google_rows(10)
        job = self.run_scan(limit=25)
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["counts"]["found"], len(job["leads"]))
        self.assertEqual(len(job["leads"]), 10)
        self.assertIn("Found 10 of 25 requested leads", job["result_note"])

    def test_google_links_without_a_target_give_no_fake_leads(self):
        self.rows = _many_google_rows(12, with_target=False)
        job = self.run_scan(limit=25)
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["leads"], [])
        self.assertEqual(self.crawled, [])  # never crawled a Google page
        self.assertIn("Found 0 of 25 requested leads", job["result_note"])


class QueryTextTests(unittest.TestCase):
    def test_market_is_not_appended_twice(self):
        self.assertEqual(finding_agent.make_query("plumbing", "Shirley, NY", "plumber Shirley, NY"), "plumber Shirley, NY")
        self.assertEqual(finding_agent.make_query("plumbing", "Shirley, NY", "Shirley, NY plumber"), "Shirley, NY plumber")
        self.assertEqual(finding_agent.make_query("plumbing", "Shirley, NY", "plumber"), "plumber Shirley, NY")
        self.assertEqual(finding_agent.make_query("plumbing", "Shirley, NY"), "plumbing company Shirley, NY")


# Production scan 192e1ac71767487d (2026-11-10): the saved lead's website was
#   https://google.com/goto?url=CAESeQHEIkXX...&gses
# -- the url= value is an opaque Google token, not an address. The SERP item's
# own "domain" field is the only readable website signal.
OPAQUE_GOTO = "https://google.com/goto?url=CAESeQHEIkXXl-Q8Ex3rCNww3iUjep2CUSoqQ-lCynhd_AZbUGqOp&gses"


def _dfs_item(rank, url, domain, title="Plumber in Shirley NY"):
    return {"type": "organic", "rank_group": rank, "rank_absolute": rank, "url": url,
            "domain": domain, "title": title, "description": "Plumbing services in Shirley, NY."}


def _dfs_response(task_status_code, items):
    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"status_code": 20000, "tasks": [{
                "status_code": task_status_code, "status_message": "x", "cost": 0.002,
                "result": [{"items": items}]}]}
    return _Response()


class DataForSEOSourceTests(unittest.TestCase):
    """The real search_google_organic with requests.post mocked (no paid call)."""

    def setUp(self):
        import tempfile
        import agents.dataforseo_serp_agent as dfs

        self.dfs = dfs
        state = dfs._circuit_breaker_state
        saved = {k: (list(v) if isinstance(v, list) else v) for k, v in state.items()}
        state.update(exhaustion_timestamps=[], opened_until=None, probe_in_progress=False)
        self.addCleanup(state.update, saved)
        diag = Path(tempfile.mkdtemp()) / "diag.log"
        for p in [
            mock.patch.dict(os.environ, {"LEADBOT_DATAFORSEO_ENABLED": "1",
                                         "DATAFORSEO_LOGIN": "test-placeholder-login",
                                         "DATAFORSEO_PASSWORD": "test-placeholder-password"}),
            mock.patch.object(dfs.time, "sleep"),
            mock.patch.object(dfs, "_LEADBOT_PROVIDER_DIAGNOSTICS_LOG", diag),
        ]:
            p.start()
            self.addCleanup(p.stop)
        self.diag = diag

    def search(self, *responses):
        with mock.patch.object(self.dfs.requests, "post", side_effect=list(responses)) as post:
            rows = self.dfs.search_google_organic("plumber Shirley, NY", "Shirley, NY", depth=40)
        return rows, post.call_count

    def test_40106_partial_results_are_kept_not_a_failure(self):
        items = [_dfs_item(31, "https://realplumber-fixture.com/", "realplumber-fixture.com")]
        rows, calls = self.search(_dfs_response(40106, items))
        self.assertEqual([r["link"] for r in rows], ["https://realplumber-fixture.com/"])
        self.assertEqual(calls, 1)  # not retried: the task completed (and was billed)
        self.assertIn('"outcome": "partial_results"', self.diag.read_text())

    def test_opaque_goto_link_uses_the_serp_domain(self):
        rows, _ = self.search(_dfs_response(20000, [
            _dfs_item(31, OPAQUE_GOTO, "www.emergencyplumber-fixture.com"),
            _dfs_item(32, OPAQUE_GOTO + "2", "www.otherplumber-fixture.com"),
            _dfs_item(33, OPAQUE_GOTO + "3", "google.com"),          # nothing readable -> dropped
            _dfs_item(34, "https://www.google.com/goto?url=https://unwrapped-fixture.com/x", ""),
        ]))
        self.assertEqual([(r["link"], r["website_status"]) for r in rows], [
            ("https://www.emergencyplumber-fixture.com/", "serp_domain"),
            ("https://www.otherplumber-fixture.com/", "serp_domain"),
            ("https://unwrapped-fixture.com/x", "unwrapped_google_redirect"),
        ])

    def test_serp_domain_is_never_guessed(self):
        for bad in ("", "not a host", "google.com", "maps.google.com", "javascript:alert(1)", "a..com"):
            self.assertEqual(bw.resolve_business_website(OPAQUE_GOTO, serp_domain=bad),
                             {"url": "", "status": bw.MISSING}, bad)
        # A normal website link is never replaced by the SERP domain.
        self.assertEqual(bw.resolve_business_website("https://biz-fixture.com/a", serp_domain="other-fixture.com")["url"],
                         "https://biz-fixture.com/a")


class ProductionScan192Tests(OfflineScan):
    """Replays the shape of scan 192e1ac71767487d end to end (offline): every
    organic result linked through an opaque google.com/goto token."""

    def _search(self, keyword, location="", page=1, num=10, raise_on_failure=False):
        if int(page or 1) != 1:
            return []
        import agents.dataforseo_serp_agent as dfs

        items = [_dfs_item(30 + i, f"{OPAQUE_GOTO}{i}", f"www.shirleyplumber{i:02d}-fixture.com",
                           f"Shirley Plumber {i:02d} | Plumbing Repair Shirley NY") for i in range(12)]
        # A directory hidden behind the same kind of link must still be rejected.
        items.append(_dfs_item(45, OPAQUE_GOTO + "yelp", "www.yelp.com", "Plumbers near Shirley NY - Yelp"))
        with mock.patch.object(dfs.requests, "post", return_value=_dfs_response(20000, items)), \
                mock.patch.dict(os.environ, {"LEADBOT_DATAFORSEO_ENABLED": "1", "DATAFORSEO_LOGIN": "x", "DATAFORSEO_PASSWORD": "y"}), \
                mock.patch.object(dfs, "_circuit_breaker_gate", lambda now: "closed"):
            return dfs.search_google_organic(keyword, location, depth=40)

    def test_goto_scan_returns_many_leads_not_one(self):
        leads = self.scan(limit=25)
        domains = [lead["domain"] for lead in leads]
        self.assertEqual(len(domains), 12)                    # was 1 ("google.com")
        self.assertEqual(len(set(domains)), 12)
        self.assertFalse([d for d in domains if bw.is_google_host(d)])
        self.assertTrue(all(lead.get("website_status") == "serp_domain" for lead in leads))
        self.assertFalse([d for d in domains if "yelp" in d])

    def test_tel_link_is_not_a_contact_page(self):
        def contact(url, market=""):
            return {"best_phone": "(934) 400-1351", "phones": ["(934) 400-1351"], "emails": [],
                    "contact_page_url": "tel:(934) 400 1351", "confidence": 60, "flags": []}

        with mock.patch.object(finding_agent, "extract_contact_from_url", contact):
            leads = self.scan(limit=25)
        self.assertTrue(leads)
        for lead in leads:
            self.assertEqual(lead["contact_page_url"], "")
            self.assertEqual(lead["best_phone"], "(934) 400-1351")  # phone kept


class BusinessCacheTests(unittest.TestCase):
    """Scan cb9290daa4104bae (2026-11-10): "Dead On Design" (web design,
    Southampton) was filled from the single cached "google.com" row -- the
    Shirley plumber's phone, email and tel: contact -- and counted as cached."""

    def setUp(self):
        import tempfile
        import agents.lead_business_cache_agent as cache

        self.cache = cache
        p = mock.patch.object(cache, "DB_PATH", Path(tempfile.mkdtemp()) / "businesses.db")
        p.start()
        self.addCleanup(p.stop)

    def _seed_poisoned_google_row(self):
        # What production holds: written directly, as the old code did.
        self.cache.init_db()
        with self.cache.connect() as conn:
            conn.execute(
                "INSERT INTO businesses (domain, title, url, best_phone, emails, contact_page_url, "
                "contact_confidence, outreach_status, first_seen_at, last_seen_at, last_enriched_at, times_seen) "
                "VALUES ('google.com', 'Emergency Plumber in Shirley, NY', 'https://google.com/goto?url=CAES', "
                "'(934) 400-1351', 'info@shirleyplumbingpro.com', 'tel:(934) 400 1351', 80, 'call_ready', "
                "'2026-11-10', '2026-11-10', '2026-11-10', 6)"
            )

    def test_google_row_is_never_applied_to_another_business(self):
        self._seed_poisoned_google_row()
        lead = {"title": "Dead On Design", "domain": "google.com",
                "url": "https://google.com/goto?url=CAESegHE", "best_phone": "", "emails": ""}
        out, cached = self.cache.apply_cached_business_to_lead(dict(lead))
        self.assertFalse(cached)
        self.assertEqual((out["best_phone"], out["emails"]), ("", ""))
        self.assertIsNone(self.cache.get_business("google.com"))
        # The historical row itself is preserved, not deleted.
        with self.cache.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM businesses WHERE domain='google.com'").fetchone()[0], 1)

    def test_google_domain_is_never_saved(self):
        self.cache.save_business_from_lead({"domain": "google.com", "url": "https://google.com/goto?url=x",
                                            "best_phone": "(631) 555-0100"}, enriched=True)
        with self.cache.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM businesses").fetchone()[0], 0)

    def test_valid_cached_prospect_still_fills_blanks(self):
        self.cache.save_business_from_lead({"domain": "realplumber-fixture.com", "url": "https://realplumber-fixture.com/",
                                            "title": "Real Plumber", "best_phone": "(631) 555-0100",
                                            "contact_page_url": "https://realplumber-fixture.com/contact"}, enriched=True)
        out, cached = self.cache.apply_cached_business_to_lead(
            {"domain": "realplumber-fixture.com", "url": "https://realplumber-fixture.com/", "best_phone": "", "emails": ""})
        self.assertTrue(cached)
        self.assertEqual(out["best_phone"], "(631) 555-0100")
        self.assertEqual(out["contact_page_url"], "https://realplumber-fixture.com/contact")

    def test_cached_tel_link_is_not_used_as_contact_page(self):
        self.cache.init_db()
        with self.cache.connect() as conn:
            conn.execute("INSERT INTO businesses (domain, title, url, best_phone, emails, contact_page_url, "
                         "first_seen_at, last_seen_at, times_seen) "
                         "VALUES ('telonly-fixture.com', 'T', 'https://telonly-fixture.com/', '(631) 452-5551', '', "
                         "'tel:631-452-5551', '2026-10-23', '2026-10-23', 1)")
        out, _ = self.cache.apply_cached_business_to_lead(
            {"domain": "telonly-fixture.com", "url": "https://telonly-fixture.com/", "best_phone": "", "emails": ""})
        self.assertEqual(out.get("contact_page_url", ""), "")
        self.assertEqual(out["best_phone"], "(631) 452-5551")  # the phone itself is still valid

class LiveStatusDisplayTests(unittest.TestCase):
    def test_old_saved_job_never_shows_google_as_website(self):
        os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")
        from fastapi.testclient import TestClient
        import app.main as appmain

        stored = {
            "job_id": "old-job", "status": "done", "message": "Done. Open Desktop is ready.",
            "counts": {"found": 1}, "params": {},
            "leads": [{"title": "Water Line Repair", "domain": "google.com",
                       "url": "https://www.google.com/goto?sa=t&ved=x",
                       "contact_page_url": "https://www.google.com/contact",
                       "best_phone": "631-555-0100", "address": "Shirley, NY"}],
        }
        snapshot = json.dumps(stored, sort_keys=True)
        with mock.patch.object(appmain, "auth_current_user", lambda request: {"id": 1, "username": "u", "role": "admin"}), \
                mock.patch("agents.lead_live_job_agent.read_job", lambda job_id: stored), \
                mock.patch("agents.lead_live_job_agent.job_belongs_to_authenticated_user", lambda job, user: True):
            body = TestClient(appmain.app).get("/lead-bot/live-status/old-job").json()
        lead = body["leads"][0]
        self.assertEqual((lead["url"], lead["domain"], lead["contact_page_url"]), ("", "", ""))
        self.assertEqual(lead["website_status"], "missing")
        self.assertEqual((lead["title"], lead["best_phone"], lead["address"]), ("Water Line Repair", "631-555-0100", "Shirley, NY"))
        self.assertEqual(json.dumps(stored, sort_keys=True), snapshot)  # stored job untouched


def setUpModule():
    # Paid discovery needs a spend ledger; give this module a throwaway one.
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parent))
    import discovery_ledger_testing
    discovery_ledger_testing.install()


def tearDownModule():
    import discovery_ledger_testing
    discovery_ledger_testing.uninstall()


if __name__ == "__main__":
    unittest.main()
