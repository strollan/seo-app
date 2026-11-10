"""Offline tests for agents/dataforseo_maps_agent.py (no network, no credentials).

Fixtures are synthetic, shaped after DataForSEO's documented Google Maps
("maps_search") and Local Finder ("local_pack") item fields. Every test blocks
requests.post so an accidental real call fails loudly.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents import dataforseo_maps_agent as maps  # noqa: E402


def item(title, place_id="", cid="", url=None, domain=None, phone="", address="", **extra):
    base = {
        "type": "maps_search", "rank_group": extra.pop("rank", 1), "rank_absolute": 1,
        "title": title, "place_id": place_id, "cid": cid, "url": url, "domain": domain,
        "phone": phone, "address": address, "category": "Plumber",
        "rating": {"rating_type": "Max5", "value": 4.7, "votes_count": 31},
        "is_claimed": True, "latitude": 40.8, "longitude": -72.8,
    }
    base.update(extra)
    return base


def envelope(items, cost=0.002, task_status=20000):
    return {
        "status_code": 20000, "status_message": "Ok.", "cost": cost,
        "tasks": [{
            "status_code": task_status, "status_message": "x", "cost": cost,
            "result": [{"items": items}] if items is not None else None,
        }],
    }


class FakeResponse:
    def __init__(self, data, http_error=None):
        self._data, self._err = data, http_error

    def raise_for_status(self):
        if self._err:
            raise self._err

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


SAMPLE = [
    item("Shirley Plumbing & Heating", place_id="ChIJ_A", cid="111",
         url="https://shirleyplumbing.com/?utm_source=gmb", domain="shirleyplumbing.com",
         phone="+1 631-555-0101", address="12 Main St, Shirley, NY 11967", rank=1),
    item("Joe's Drain Service", place_id="ChIJ_B", cid="222", url=None, domain=None,
         phone="(631) 555-0102", address="5 Oak Ave, Shirley, NY 11967", rank=2),
    item("Google-hosted Listing Plumbing", place_id="ChIJ_C", cid="333",
         url="https://www.google.com/maps/place/xyz", domain="google.com",
         phone="631-555-0103", address="9 Elm St, Mastic, NY", rank=3),
    item("Sponsored Plumber", place_id="ChIJ_AD", cid="444", url="https://ad.example",
         domain="ad.example", is_paid=True, rank=4),
    item("Gone Plumbing", place_id="ChIJ_D", cid="555", url="https://gone.example",
         domain="gone.example", work_hours={"current_status": "closed_forever"}, rank=5),
    # duplicate of ChIJ_A (same place_id) carrying nothing new
    item("Shirley Plumbing & Heating", place_id="ChIJ_A", cid="111", rank=6),
    {"type": "maps_paid_item", "title": "Ad"},
]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self.tmp.name) / "ledger.json"
        from agents.discovery_spend_guard import initialize_ledger
        initialize_ledger(self.ledger)
        self.empty_ledger = self.ledger.read_text(encoding="utf-8")
        patches = [
            mock.patch.dict(os.environ, {"LEADBOT_MAPS_ENABLED": "1", "LEADBOT_DATAFORSEO_ENABLED": "1",
                                         "LEADBOT_DISCOVERY_LEDGER_PATH": str(self.ledger)}),
            mock.patch("requests.post", side_effect=AssertionError("real network call attempted")),
            mock.patch("agents.dataforseo_serp_agent._auth_header",
                       return_value={"Authorization": "Basic test", "Content-Type": "application/json"}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def guard(self, **kw):
        defaults = dict(max_calls=1, max_scan_usd=0.02, per_call_ceiling_usd=0.01,
                        daily_max_usd=0.25, ledger_path=self.ledger, today=lambda: "2026-11-10")
        defaults.update(kw)
        return maps.MapsSpendGuard(**defaults)


class ParseTests(Base):
    def test_real_business_fields_and_honest_missing_websites(self):
        rows, skipped = maps.parse_maps_items(SAMPLE, keyword="plumber", market="Shirley, NY")
        rows, stats = maps.dedupe_listings(rows)
        by_name = {r["title"]: r for r in rows}
        self.assertEqual(set(by_name), {"Shirley Plumbing & Heating", "Joe's Drain Service",
                                         "Google-hosted Listing Plumbing"})
        a = by_name["Shirley Plumbing & Heating"]
        self.assertEqual(a["domain"], "shirleyplumbing.com")
        self.assertTrue(a["website"].startswith("https://shirleyplumbing.com/"))
        self.assertEqual(a["website_status"], "website")
        self.assertEqual(a["phone"], "+1 631-555-0101")
        self.assertEqual(a["address"], "12 Main St, Shirley, NY 11967")
        self.assertEqual(a["place_id"], "ChIJ_A")
        self.assertEqual(a["source"], "dataforseo_maps")
        self.assertEqual(a["lead_source_label"], "Google Maps")
        for name in ("Joe's Drain Service", "Google-hosted Listing Plumbing"):
            r = by_name[name]
            self.assertEqual(r["website"], "")
            self.assertEqual(r["link"], "")
            self.assertEqual(r["domain"], "")
            self.assertFalse(r["has_website"])
            self.assertEqual(r["website_status"], "missing")
            self.assertTrue(r["phone"] and r["address"])  # still a usable lead
        self.assertEqual(skipped, {"wrong_type": 0, "paid": 2, "closed_forever": 1, "no_name": 0, "not_a_business": 0})
        self.assertEqual(stats["duplicates_merged"], 1)
        self.assertEqual(stats["with_website"], 1)
        self.assertEqual(stats["without_website"], 2)

    def test_no_google_host_ever_becomes_a_website(self):
        bad = [
            item("A", place_id="p1", url="https://www.google.com/goto?url=CAESopaque", domain="google.com"),
            item("B", place_id="p2", url="https://maps.google.com/?cid=1", domain="maps.google.com"),
            item("C", place_id="p3", url="https://g.page/abc", domain="g.page"),
        ]
        rows, _ = maps.parse_maps_items(bad)
        self.assertEqual([r["website"] for r in rows], ["", "", ""])

    def test_google_redirect_with_real_target_is_unwrapped(self):
        rows, _ = maps.parse_maps_items([item(
            "D", place_id="p4", url="https://www.google.com/url?q=https://dplumb.com/", domain="dplumb.com")])
        self.assertEqual(rows[0]["website"], "https://dplumb.com/")
        self.assertEqual(rows[0]["website_status"], "unwrapped_google_redirect")

    def test_local_finder_mode_uses_local_pack_items(self):
        lp = {"type": "local_pack", "title": "LF Plumbing", "cid": "999", "url": "https://lf.example/",
              "domain": "lf.example", "phone": "631-555-0199", "rank_group": 1}
        rows, skipped = maps.parse_maps_items([lp, item("maps-only", place_id="x")], mode="local_finder")
        self.assertEqual([r["title"] for r in rows], ["LF Plumbing"])
        self.assertEqual(rows[0]["lead_source_label"], "Google Local Finder")
        self.assertEqual(skipped["wrong_type"], 1)


class DedupeTests(unittest.TestCase):
    def row(self, **kw):
        base = {"title": "", "place_id": "", "cid": "", "phone": "", "address": "", "website": "", "domain": ""}
        base.update(kw)
        return base

    def test_place_id_then_cid_then_name_phone(self):
        rows = [
            self.row(title="Acme Plumbing", place_id="P1", cid="C1", phone="631-555-0001"),
            self.row(title="Acme Plumbing", cid="C1", address="1 Main St", website="https://acme.com/", domain="acme.com"),
            self.row(title="Beta Drains", phone="+1 (631) 555-0002"),
            self.row(title="beta drains", phone="6315550002", address="2 Main St"),
        ]
        kept, stats = maps.dedupe_listings(rows)
        self.assertEqual(len(kept), 2)
        self.assertEqual(stats["duplicates_merged"], 2)
        acme = kept[0]
        self.assertEqual(acme["place_id"], "P1")
        self.assertEqual(acme["address"], "1 Main St")       # filled from duplicate
        self.assertEqual(acme["website"], "https://acme.com/")
        self.assertEqual(kept[1]["address"], "2 Main St")

    def test_different_place_ids_never_merge_even_with_same_name(self):
        rows = [self.row(title="Roto Pro", place_id="P1", phone="631-555-0003"),
                self.row(title="Roto Pro", place_id="P2", phone="631-555-0003")]
        kept, _ = maps.dedupe_listings(rows)
        self.assertEqual(len(kept), 2)

    def test_chain_locations_kept_and_flagged(self):
        rows = [self.row(title="Chain - Shirley", place_id="P1", domain="chain.com", website="https://chain.com/"),
                self.row(title="Chain - Mastic", place_id="P2", domain="chain.com", website="https://chain.com/")]
        kept, stats = maps.dedupe_listings(rows)
        self.assertEqual([r["shared_domain_count"] for r in kept], [2, 2])
        self.assertEqual(stats["shared_domain_rows"], 2)

    def test_unidentifiable_rows_are_never_merged(self):
        kept, _ = maps.dedupe_listings([self.row(title="X"), self.row(title="X")])
        self.assertEqual(len(kept), 2)


class SpendGuardTests(Base):
    def test_disabled_flag_sends_nothing(self):
        post = FakePost()
        with mock.patch.dict(os.environ, {"LEADBOT_MAPS_ENABLED": "0"}):
            with self.assertRaises(maps.MapsDisabledError):
                maps.search_google_maps("plumber", "Shirley, NY", self.guard(), post=post)
        self.assertEqual(post.calls, [])
        self.assertEqual(self.ledger.read_text(encoding="utf-8"), self.empty_ledger)

    def test_one_call_per_scan_by_default(self):
        post = FakePost(FakeResponse(envelope(SAMPLE)), FakeResponse(envelope(SAMPLE)))
        g = self.guard()
        out = maps.search_google_maps("plumber", "Shirley, NY", g, post=post)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["stats"]["unique"], 3)
        self.assertAlmostEqual(out["cost"], 0.002)
        with self.assertRaises(maps.SpendLimitExceeded):
            maps.search_google_maps("plumber", "Shirley, NY", g, post=post)
        self.assertEqual(len(post.calls), 1)

    def test_scan_budget_blocks_before_sending(self):
        post = FakePost()
        with self.assertRaises(maps.SpendLimitExceeded):
            maps.search_google_maps("plumber", "Shirley, NY",
                                    self.guard(max_scan_usd=0.005, per_call_ceiling_usd=0.01), post=post)
        self.assertEqual(post.calls, [])

    def test_daily_budget_persists_across_scans(self):
        post = FakePost(*[FakeResponse(envelope([], cost=0.01)) for _ in range(3)])
        for _ in range(2):
            maps.search_google_maps("plumber", "Shirley, NY",
                                    self.guard(daily_max_usd=0.02), post=post)
        with self.assertRaises(maps.SpendLimitExceeded):
            maps.search_google_maps("plumber", "Shirley, NY", self.guard(daily_max_usd=0.02), post=post)
        self.assertEqual(len(post.calls), 2)
        self.assertAlmostEqual(self.guard().daily_spent(), 0.02)

    def test_new_utc_day_resets_daily_budget(self):
        post = FakePost(FakeResponse(envelope([], cost=0.01)), FakeResponse(envelope([], cost=0.01)))
        maps.search_google_maps("plumber", "Shirley, NY", self.guard(daily_max_usd=0.01), post=post)
        maps.search_google_maps("plumber", "Shirley, NY",
                                self.guard(daily_max_usd=0.01, today=lambda: "2026-11-11"), post=post)
        self.assertEqual(len(post.calls), 2)

    def test_unknown_cost_is_charged_at_ceiling(self):
        data = envelope(SAMPLE)
        del data["tasks"][0]["cost"]
        del data["cost"]
        g = self.guard(max_calls=5)
        out = maps.search_google_maps("plumber", "Shirley, NY", g, post=FakePost(FakeResponse(data)))
        self.assertAlmostEqual(out["cost"], 0.01)
        self.assertAlmostEqual(g.daily_spent(), 0.01)

    def test_cost_above_ceiling_stops_further_spending(self):
        g = self.guard(max_calls=5, max_scan_usd=1.0)
        post = FakePost(FakeResponse(envelope([], cost=0.03)), FakeResponse(envelope([])))
        maps.search_google_maps("plumber", "Shirley, NY", g, post=post)
        with self.assertRaises(maps.SpendLimitExceeded):
            maps.search_google_maps("plumber", "Shirley, NY", g, post=post)
        self.assertEqual(len(post.calls), 1)
        self.assertIn("exceeded per-call ceiling", g.blocked_reason)

    def test_unreadable_ledger_refuses_to_spend(self):
        self.ledger.write_text("{not json", encoding="utf-8")
        post = FakePost()
        with self.assertRaises(maps.SpendLimitExceeded):
            maps.search_google_maps("plumber", "Shirley, NY", self.guard(), post=post)
        self.assertEqual(post.calls, [])

    def test_missing_ledger_refuses_to_spend(self):
        self.ledger.unlink()
        post = FakePost()
        with self.assertRaises(maps.LedgerUnavailable):
            maps.search_google_maps("plumber", "Shirley, NY", self.guard(), post=post)
        self.assertEqual(post.calls, [])
        self.assertFalse(self.ledger.exists())

    def test_safe_mode_blocks_maps(self):
        post = FakePost()
        with mock.patch.dict(os.environ, {"LEADBOT_DATAFORSEO_ENABLED": "0"}):
            with self.assertRaises(maps.SafeModeError):
                maps.search_google_maps("plumber", "Shirley, NY", self.guard(), post=post)
        self.assertEqual(post.calls, [])
        self.assertEqual(self.ledger.read_text(encoding="utf-8"), self.empty_ledger)

    def test_invalid_location_task_status_is_a_location_error(self):
        from agents.dataforseo_serp_agent import InvalidLocationValueError
        post = FakePost(FakeResponse(envelope(None, cost=0, task_status=40501)))
        with self.assertRaises(InvalidLocationValueError):
            maps.search_google_maps("plumber", "Shirley, NY", self.guard(), post=post)
        self.assertEqual(len(post.calls), 1)

    def test_network_error_no_retry_and_worst_case_booked(self):
        g = self.guard(max_calls=5, max_scan_usd=1.0)
        post = FakePost(ConnectionError("boom"))
        with self.assertRaises(maps.MapsProviderError):
            maps.search_google_maps("plumber", "Shirley, NY", g, post=post)
        self.assertEqual(len(post.calls), 1)
        self.assertAlmostEqual(g.spent_usd, 0.01)

    def test_task_error_raises_without_retry(self):
        post = FakePost(FakeResponse(envelope(None, cost=0, task_status=40101)))
        g = self.guard(max_calls=5, max_scan_usd=1.0)
        with self.assertRaises(maps.MapsProviderError):
            maps.search_google_maps("plumber", "Shirley, NY", g, post=post)
        self.assertEqual(len(post.calls), 1)
        self.assertEqual(g.spent_usd, 0.0)  # reported cost 0 is booked as 0

    def test_no_results_and_partial(self):
        out = maps.search_google_maps("plumber", "Shirley, NY", self.guard(),
                                      post=FakePost(FakeResponse(envelope(None, task_status=40102))))
        self.assertEqual((out["status"], out["rows"]), ("no_results", []))
        out = maps.search_google_maps("plumber", "Shirley, NY", self.guard(),
                                      post=FakePost(FakeResponse(envelope(SAMPLE, task_status=40106))))
        self.assertEqual(out["status"], "partial")
        self.assertEqual(out["stats"]["unique"], 3)

    def test_payload_depth_clamped_and_endpoint(self):
        post = FakePost(FakeResponse(envelope([])))
        maps.search_google_maps("plumber", "Shirley, NY", self.guard(), depth=5000, post=post)
        self.assertEqual(post.calls[0]["url"], maps.ENDPOINTS["maps"])
        payload = post.calls[0]["payload"][0]
        self.assertEqual(payload["depth"], maps.MAX_DEPTH)
        self.assertEqual(payload["keyword"], "plumber")
        self.assertTrue(payload["location_name"])

    def test_invalid_market_spends_nothing(self):
        from agents.dataforseo_serp_agent import InvalidMarketLocationError
        post = FakePost()
        with mock.patch("agents.dataforseo_serp_agent._location_name",
                        side_effect=InvalidMarketLocationError("bad")):
            with self.assertRaises(InvalidMarketLocationError):
                maps.search_google_maps("plumber", "zzz", self.guard(), post=post)
        self.assertEqual(post.calls, [])
        self.assertEqual(self.ledger.read_text(encoding="utf-8"), self.empty_ledger)


REAL_FIXTURE = ROOT / "scripts" / "fixtures" / "dataforseo_maps_live_shirley_plumbers_20261110.json"


class RealResponseFixtureTests(Base):
    """Regression against a trimmed REAL provider response (one live call, 2026-11-10)."""

    def load(self):
        import json
        return json.loads(REAL_FIXTURE.read_text(encoding="utf-8"))

    def rows(self):
        items = self.load()["tasks"][0]["result"][0]["items"]
        rows, skipped = maps.parse_maps_items(items, keyword="plumbers", market="Shirley, NY")
        return {r["title"]: r for r in rows}, skipped

    def test_skips_ads_and_area_features(self):
        by_name, skipped = self.rows()
        self.assertEqual(skipped["paid"], 1)              # maps_paid_item "Ray The Plumber"
        self.assertEqual(skipped["not_a_business"], 3)    # category "Area"
        self.assertNotIn("Plumbing", by_name)
        self.assertNotIn("Plumbing, Kitchen and Bath", by_name)
        self.assertNotIn("Ray The Plumber", by_name)
        self.assertEqual(len(by_name), 7)

    def test_websites_are_real_and_cleaned(self):
        by_name, _ = self.rows()
        hardy = by_name["Hardy Plumbing & Heating"]
        self.assertEqual(hardy["website"], "https://www.hardyplumbing.com/")
        self.assertEqual(hardy["domain"], "hardyplumbing.com")
        self.assertEqual(hardy["website_status"], "website")
        ferg = by_name["Ferguson Plumbing Supply"]
        self.assertEqual(ferg["website"], "https://www.ferguson.com/store/ny/medford/plumbingpvf-3518")
        for r in by_name.values():
            self.assertNotIn("google", r["website"])
            self.assertNotIn("utm_", r["website"])

    def test_social_profile_is_not_a_website(self):
        by_name, _ = self.rows()
        fb = by_name["A Level Above Carpentry"]
        self.assertEqual((fb["website"], fb["domain"], fb["link"]), ("", "", ""))
        self.assertEqual(fb["website_status"], "social_profile")
        self.assertEqual(fb["profile_url"], "https://www.facebook.com/alevelabovecarpentry2/")
        self.assertEqual(fb["website_flag"], "No Website")
        self.assertTrue(fb["lead_eligible"])  # has a verified phone

    def test_no_website_businesses_stay_eligible_with_real_contact_data(self):
        by_name, _ = self.rows()
        lj = by_name["L & J Cesspool Services Inc"]
        self.assertEqual((lj["website"], lj["website_flag"]), ("", "No Website"))
        self.assertEqual(lj["phone"], "+1631-399-3200")
        self.assertEqual(lj["address"], "Shirley, NY 11967")
        self.assertFalse(lj["has_street_address"])         # city-level only
        self.assertTrue(lj["lead_eligible"])
        warhawk = by_name["Warhawk Plumbing & Heating Inc"]  # no phone, street address
        self.assertEqual(warhawk["phone"], "")
        self.assertTrue(warhawk["has_street_address"])
        self.assertTrue(warhawk["lead_eligible"])
        plumbob = by_name["Plumbob plumbing & heating llc"]  # service-area: hidden address
        self.assertEqual(plumbob["address"], "")
        self.assertTrue(plumbob["lead_eligible"])
        for r in by_name.values():
            self.assertTrue(r["place_id"] and r["cid"])
            self.assertEqual(r["work_status"] in ("open", ""), True)

    def test_dedupe_with_real_identifiers(self):
        by_name, _ = self.rows()
        rows = list(by_name.values())
        kept, stats = maps.dedupe_listings(rows + [dict(r) for r in rows])
        self.assertEqual(stats["unique"], 7)
        self.assertEqual(stats["duplicates_merged"], 7)
        # same row seen without place_id still merges via cid
        no_pid = dict(rows[0], place_id="")
        kept, stats = maps.dedupe_listings(rows + [no_pid])
        self.assertEqual(stats["duplicates_merged"], 1)

    def test_end_to_end_with_real_envelope(self):
        out = maps.search_google_maps("plumbers", "Shirley, NY", self.guard(),
                                      post=FakePost(FakeResponse(self.load())))
        self.assertEqual(out["status"], "ok")
        self.assertAlmostEqual(out["cost"], 0.002)
        self.assertEqual(out["stats"]["unique"], 7)
        self.assertEqual(out["stats"]["lead_eligible"], 7)
        self.assertEqual(out["stats"]["social_profile_only"], 1)


if __name__ == "__main__":
    unittest.main()
