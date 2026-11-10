"""Offline regression tests for the Google Maps Lead Finder pre-deploy repairs.

- Per-lead actions (delete, save details, save address) on a Maps export are
  keyed by the listing's stable Google ID, never by (empty or shared) domain.
- Chain locations with distinct place IDs stay separate.
- The discovery spend guard reserves atomically under thread and process
  concurrency and fails closed when its lock or ledger cannot be written.
- DataForSEO OFF (Safe Mode) also stops the paid Serper and Google Places
  discovery paths.

No network, no credentials, no paid calls. Runs on Python 3.10 (no
unittest.enterContext). Run: python -m unittest scripts.test_maps_row_actions
"""

import ast
import csv
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents import discovery_spend_guard as spend  # noqa: E402
from agents import lead_dashboard_agent as dashboard  # noqa: E402
from agents.lead_row_identity import clean_listing_key, is_maps_export, maps_rows_for_key, row_listing_key  # noqa: E402

MAPS_FIELDS = ["title", "domain", "url", "best_phone", "address", "keyword", "market", "website_status",
               "website_verified", "website_flag", "place_id", "cid", "category"]


def maps_rows():
    base = dict(keyword="plumbers", market="Shirley, NY", category="Plumber", cid="")
    nosite = [dict(base, title=f"No Site {n}", domain="", url="", best_phone=f"631-555-010{n}",
                   address=f"{n} Main St", website_status="missing", website_verified="false",
                   website_flag="No Website", place_id=f"ChIJ_none_{n}") for n in range(1, 4)]
    chain = [dict(base, title=f"Chain {town}", domain="chain.example", url="https://chain.example/",
                  best_phone=f"631-555-020{i}", address=f"{i} {town} Rd", website_status="verified",
                  website_verified="true", website_flag="", place_id=f"ChIJ_chain_{i}")
             for i, town in enumerate(("Shirley", "Mastic"), 1)]
    cid_only = [dict(base, title="Cid Only", domain="", url="", best_phone="631-555-0300", address="9 Elm St",
                     website_status="missing", website_verified="false", website_flag="No Website",
                     place_id="", cid="1234567890")]
    return nosite + chain + cid_only


def read_csv(path):
    with Path(path).open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


class RowIdentityUnitTests(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(row_listing_key({"place_id": "X", "cid": "1"}), "place_id:X")
        self.assertEqual(row_listing_key({"place_id": "", "cid": "1"}), "cid:1")
        self.assertEqual(row_listing_key({"domain": "a.example"}), "")
        self.assertEqual(clean_listing_key(" place_id:X "), "place_id:X")
        for bad in ("", "domain:a.example", "place_id:", "X", None):
            self.assertEqual(clean_listing_key(bad), "")

    def test_detects_maps_export_only(self):
        self.assertTrue(is_maps_export(MAPS_FIELDS))
        self.assertFalse(is_maps_export(["title", "domain", "url", "best_phone"]))

    def test_empty_key_matches_nothing(self):
        rows = maps_rows()
        self.assertEqual(maps_rows_for_key(rows, ""), [])
        self.assertEqual(maps_rows_for_key(rows, "place_id:ChIJ_none_2"), [1])
        self.assertEqual(maps_rows_for_key(rows, "cid:1234567890"), [5])


class RowRouteTests(unittest.TestCase):
    """The real route functions from app/main.py, on a throwaway FastAPI app.

    Auth, ownership and CSRF are stubbed to "allowed": these tests are about
    WHICH rows an allowed action touches, not who may act.
    """

    ROUTES = {"leadbot_delete_row_safe", "leadbot_delete_row_route", "leadbot_save_details_combined",
              "leadbot_update_address"}

    def setUp(self):
        from fastapi import FastAPI, Form, Request
        from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
        from fastapi.testclient import TestClient

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        previous = Path.cwd()
        os.chdir(tmp.name)
        self.addCleanup(os.chdir, previous)
        Path("exports").mkdir()
        patcher = mock.patch.object(dashboard, "EXPORT_DIR", Path("exports"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cache_writes = []
        for target in ("agents.lead_business_cache_agent.save_business_from_lead",
                       "agents.lead_detail_index_agent.upsert_lead"):
            p = mock.patch(target, side_effect=lambda *a, **k: self.cache_writes.append(a))
            p.start()
            self.addCleanup(p.stop)

        app = FastAPI()
        ns = dict(
            app=app, Request=Request, AuthRequest=Request, AuthForm=Form, Path=Path,
            JSONResponse=JSONResponse, LeadBotHTMLResponse=HTMLResponse,
            LeadBotRedirectResponse=RedirectResponse, safe_export_file=dashboard.safe_export_file,
            auth_current_user=lambda request: {"id": 1, "username": "owner"},
            leadbot_user_can_access_export=lambda name, request: True,
            _csrf_token_valid=lambda request, token: True,
            leadbot_find_editable_export_for_domain=lambda domain, request: (None, None),
        )
        tree = ast.parse((ROOT / "app/main.py").read_text(encoding="utf-8"))
        funcs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name in self.ROUTES]
        self.assertEqual({n.name for n in funcs}, self.ROUTES)
        exec(compile(ast.Module(body=funcs, type_ignores=[]), "row-route-harness", "exec"), ns)
        self.client = TestClient(app)

        self.file = "leads_plumbers_shirley_maps.csv"
        self.path = Path("exports") / self.file
        with self.path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=MAPS_FIELDS)
            writer.writeheader()
            writer.writerows(maps_rows())
        self.original = read_csv(self.path)

    def titles(self):
        return [r["title"] for r in read_csv(self.path)]

    def safe_delete(self, **params):
        return self.client.post("/lead-bot/delete-row-safe", params=dict(filename=self.file, csrf_token="t", **params))

    # -- delete ---------------------------------------------------------------------

    def test_safe_delete_removes_only_the_named_no_website_lead(self):
        res = self.safe_delete(listing_key="place_id:ChIJ_none_2")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["deleted"], 1)
        self.assertEqual(self.titles(), ["No Site 1", "No Site 3", "Chain Shirley", "Chain Mastic", "Cid Only"])

    def test_safe_delete_by_cid(self):
        self.assertEqual(self.safe_delete(listing_key="cid:1234567890").json()["deleted"], 1)
        self.assertNotIn("Cid Only", self.titles())
        self.assertEqual(len(self.titles()), 5)

    def test_safe_delete_one_chain_location_keeps_the_other(self):
        self.assertEqual(self.safe_delete(domain="chain.example", listing_key="place_id:ChIJ_chain_2").json()["deleted"], 1)
        self.assertIn("Chain Shirley", self.titles())
        self.assertNotIn("Chain Mastic", self.titles())

    def test_safe_delete_by_domain_alone_is_refused_on_maps_exports(self):
        for domain in ("chain.example", "", "!!!"):
            res = self.safe_delete(domain=domain)
            self.assertIn(res.status_code, (400,), res.text)
        self.assertEqual(read_csv(self.path), self.original)

    def test_safe_delete_unknown_key_deletes_nothing(self):
        self.assertEqual(self.safe_delete(listing_key="place_id:nope").json()["deleted"], 0)
        self.assertEqual(read_csv(self.path), self.original)

    def test_form_delete_by_listing_key_and_never_by_domain(self):
        res = self.client.post(f"/lead-bot/delete-row/{self.file}", data=dict(domain="", listing_key="place_id:ChIJ_none_1"),
                               follow_redirects=False)
        self.assertEqual(res.status_code, 303)
        self.assertNotIn("No Site 1", self.titles())
        before = read_csv(self.path)
        self.client.post(f"/lead-bot/delete-row/{self.file}", data=dict(domain="chain.example"), follow_redirects=False)
        self.assertEqual(read_csv(self.path), before)

    def test_organic_export_delete_by_domain_unchanged(self):
        organic = Path("exports") / "leads_organic.csv"
        with organic.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=["title", "domain", "url"])
            writer.writeheader()
            writer.writerow(dict(title="A", domain="a.example", url="https://a.example"))
            writer.writerow(dict(title="B", domain="b.example", url="https://b.example"))
        res = self.client.post("/lead-bot/delete-row-safe",
                               params=dict(filename=organic.name, domain="a.example", csrf_token="t"))
        self.assertEqual(res.json()["deleted"], 1)
        self.assertEqual([r["title"] for r in read_csv(organic)], ["B"])

    # -- edits ---------------------------------------------------------------------

    def test_save_details_edits_only_the_named_no_website_lead(self):
        res = self.client.post("/lead-bot/save-details", follow_redirects=False, data=dict(
            filename=self.file, domain="", listing_key="place_id:ChIJ_none_3", phone="631-555-9999",
            email="", website="", contact_page="", address="3 Main St"))
        self.assertEqual(res.status_code, 303)
        after = read_csv(self.path)
        self.assertEqual(after[2]["best_phone"], "631-555-9999")
        for i in (0, 1, 3, 4, 5):
            self.assertEqual(after[i]["best_phone"], self.original[i]["best_phone"], after[i]["title"])
        self.assertEqual(self.cache_writes, [], "Maps rows must not write the domain-keyed caches")

    def test_save_details_without_listing_key_changes_nothing(self):
        self.client.post("/lead-bot/save-details", follow_redirects=False, data=dict(
            filename=self.file, domain="", phone="000", email="", website="", contact_page="", address=""))
        self.client.post("/lead-bot/save-details", follow_redirects=False, data=dict(
            filename=self.file, domain="chain.example", phone="000", email="", website="", contact_page="", address=""))
        self.assertEqual([r["best_phone"] for r in read_csv(self.path)], [r["best_phone"] for r in self.original])

    def test_update_address_targets_one_chain_location(self):
        res = self.client.post("/lead-bot/update-address", follow_redirects=False, data=dict(
            filename=self.file, domain="chain.example", listing_key="place_id:ChIJ_chain_1", address="77 New Rd"))
        self.assertIn(res.status_code, (200, 303))
        after = read_csv(self.path)
        self.assertEqual(after[3]["address"], "77 New Rd")
        self.assertEqual(after[4]["address"], self.original[4]["address"])
        self.assertEqual(self.cache_writes, [])


class DashboardCardTests(unittest.TestCase):
    def test_maps_cards_carry_listing_keys_and_no_website_cards_can_be_deleted(self):
        html_out = dashboard.lead_cards(maps_rows(), selected_name="leads_maps.csv", csrf_token="tok")
        for row in maps_rows():
            key = row_listing_key(row)
            self.assertIn(f'data-listing-key="{key}"', html_out)
            self.assertEqual(html_out.count(f'name="listing_key" value="{key}"'), 3, key)  # delete, details, address
        # Chain rows share a website: no CRM / Compare for either location.
        self.assertNotIn("lead-add-crm", html_out)
        self.assertNotIn('class="lead-top-competitor"', html_out)
        self.assertEqual(html_out.count('class="lead-delete-one"'), len(maps_rows()))
        # ...but they are shown disabled, with the reason.
        self.assertEqual(html_out.count('class="lead-action-disabled" disabled'), 2 * len(maps_rows()))
        self.assertEqual(html_out.count("this business has no website"), 3 * 4)  # 2 titles + note, 4 no-site rows
        self.assertEqual(html_out.count("Several locations share this website"), 3 * 2)


class ChainLocationTests(unittest.TestCase):
    def test_distinct_place_ids_with_same_name_phone_and_site_stay_separate(self):
        from agents import dataforseo_maps_agent as maps
        rows = [
            dict(title="Roto Chain", phone="800-555-0000", place_id="P1", cid="", website="https://chain.example/",
                 domain="chain.example", address="1 A St"),
            dict(title="Roto Chain", phone="800-555-0000", place_id="P2", cid="", website="https://chain.example/",
                 domain="chain.example", address="2 B St"),
            dict(title="Roto Chain", phone="800-555-0000", place_id="P1", cid="", website="", domain="",
                 address="1 A St"),
        ]
        unique, stats = maps.dedupe_listings(rows)
        self.assertEqual([r["place_id"] for r in unique], ["P1", "P2"])
        self.assertEqual(stats["duplicates_merged"], 1)
        self.assertTrue(all(r["shared_domain_count"] == 2 for r in unique))


class GuardConcurrencyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.ledger = self.dir / "ledger.json"
        spend.initialize_ledger(self.ledger)
        p = mock.patch.dict(os.environ, {"LEADBOT_DATAFORSEO_ENABLED": "1",
                                         "LEADBOT_DISCOVERY_LEDGER_PATH": str(self.ledger)})
        p.start()
        self.addCleanup(p.stop)

    def _race(self, workers, target):
        barrier = threading.Barrier(workers)
        results = []

        def run():
            barrier.wait()
            try:
                results.append(target())
            except spend.SpendLimitExceeded as exc:
                results.append(exc)

        threads = [threading.Thread(target=run) for _ in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results

    def test_threads_sharing_one_scan_guard_never_exceed_the_scan_cap(self):
        guard = spend.DiscoverySpendGuard(job_id="race", ledger_path=self.ledger)  # $0.02 scan, $0.01 ceiling
        results = self._race(16, lambda: guard.reserve("organic"))
        ok = [r for r in results if isinstance(r, str)]
        self.assertEqual(len(ok), 2)
        self.assertEqual(guard.calls["organic"], 2)
        self.assertEqual(spend.read_day_summary(path=self.ledger)["requests"], 2)

    def test_threads_with_separate_guards_never_exceed_the_daily_cap(self):
        results = self._race(40, lambda: spend.DiscoverySpendGuard(job_id="d", ledger_path=self.ledger).reserve("maps"))
        self.assertEqual(len([r for r in results if isinstance(r, str)]), 25)  # $0.25 / $0.01
        self.assertAlmostEqual(spend.read_day_summary(path=self.ledger)["committed_usd"], 0.25)

    def test_unwritable_ledger_directory_fails_closed(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        os.chmod(self.dir, 0o500)
        self.addCleanup(os.chmod, self.dir, 0o700)
        guard = spend.DiscoverySpendGuard(job_id="ro", ledger_path=self.ledger)
        with self.assertRaises(spend.LedgerUnavailable):
            guard.reserve("maps")
        self.assertEqual(guard.calls["maps"], 0)


class SafeModeOtherPaidProvidersTests(unittest.TestCase):
    def test_serper_is_not_called_in_safe_mode(self):
        import business_competitor_finder as bcf
        with mock.patch.dict(os.environ, {"LEADBOT_DATAFORSEO_ENABLED": "0", "USE_LIVE_SERP": "true"}), \
                mock.patch.object(bcf, "google_search", side_effect=AssertionError("paid Serper call in Safe Mode")), \
                mock.patch("requests.post", side_effect=AssertionError("network")), \
                mock.patch("requests.get", side_effect=AssertionError("network")), \
                mock.patch("dotenv.load_dotenv", return_value=False):
            with self.assertRaises(bcf.SearchProviderUnavailableError):
                bcf._raw_find_business_competitors("plumber", location="Shirley, NY", pages=[1])

    def test_places_supplement_is_gated_on_paid_discovery(self):
        source = (ROOT / "agents/lead_finding_agent.py").read_text(encoding="utf-8")
        self.assertIn("if not quota_reached and not disable_places_main and _paid_discovery_enabled():", source)


if __name__ == "__main__":
    unittest.main()
