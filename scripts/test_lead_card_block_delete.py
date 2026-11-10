"""Offline tests for per-lead Block / Delete on Lead Finder cards.

Covers: verified-website lead, No Website Google Maps lead, chain locations
sharing one website, delete + refresh, block + future-results filtering,
Undo (restore / unblock), and a lead that is already in the CRM.

No network, no credentials, no paid calls. Every test runs in a temp cwd so
the relative data/ and exports/ paths never touch real files.
Run: python -m unittest scripts.test_lead_card_block_delete
"""

import ast
import csv
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents import lead_card_actions_agent as lca  # noqa: E402
from agents import lead_dashboard_agent as dashboard  # noqa: E402
from agents.lead_row_identity import row_listing_key  # noqa: E402

MAPS_FIELDS = ["title", "domain", "url", "best_phone", "address", "keyword", "market", "website_status",
               "website_verified", "website_flag", "place_id", "cid", "category"]
ORGANIC_FIELDS = ["title", "domain", "url", "best_phone", "emails", "keyword", "market", "final_lead_score"]
OWNER = {"id": 1, "username": "owner"}


def maps_rows():
    base = dict(keyword="bagels", market="Shirley, NY", category="Bagel shop", cid="")
    return [
        dict(base, title="No Site Bagels", domain="", url="", best_phone="631-555-0101", address="1 Main St",
             website_status="missing", website_verified="false", website_flag="No Website",
             place_id="ChIJ_nosite"),
        dict(base, title="Chain Shirley", domain="chain.example", url="https://chain.example/",
             best_phone="631-555-0201", address="2 Shirley Rd", website_status="verified",
             website_verified="true", website_flag="", place_id="ChIJ_chain_1"),
        dict(base, title="Chain Mastic", domain="chain.example", url="https://chain.example/",
             best_phone="631-555-0202", address="3 Mastic Rd", website_status="verified",
             website_verified="true", website_flag="", place_id="ChIJ_chain_2"),
        dict(base, title="Solo Verified", domain="solo.example", url="https://solo.example/",
             best_phone="631-555-0301", address="4 Elm St", website_status="verified",
             website_verified="true", website_flag="", place_id="ChIJ_solo"),
    ]


def organic_rows():
    base = dict(keyword="plumbers", market="Shirley, NY", final_lead_score="52", emails="")
    return [
        dict(base, title="Alpha Plumbing", domain="alpha.example", url="https://alpha.example/", best_phone="1"),
        dict(base, title="Beta Plumbing", domain="beta.example", url="https://beta.example/", best_phone="2"),
    ]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class TempCwd(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        previous = Path.cwd()
        os.chdir(tmp.name)
        self.addCleanup(os.chdir, previous)
        Path("exports").mkdir()
        Path("data").mkdir()
        p = mock.patch.object(dashboard, "EXPORT_DIR", Path("exports"))
        p.start()
        self.addCleanup(p.stop)

    def write(self, name, fields, rows):
        path = Path("exports") / name
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        return path


class LeadKeyTests(unittest.TestCase):
    def test_keys(self):
        rows = maps_rows()
        self.assertEqual(lca.row_lead_key(rows[0]), "place_id:ChIJ_nosite")  # No Website
        self.assertNotEqual(lca.row_lead_key(rows[1]), lca.row_lead_key(rows[2]))  # chain locations
        self.assertEqual(lca.row_lead_key(organic_rows()[0]), "domain:alpha.example")
        self.assertEqual(lca.row_lead_key({"domain": "https://www.Alpha.example/x"}), "domain:alpha.example")
        self.assertEqual(lca.row_lead_key({"domain": ""}), "")
        # A Maps row without a Google ID has no identity, even with a website.
        self.assertEqual(lca.row_lead_key(dict(maps_rows()[3], place_id="")), "")

    def test_clean_lead_key_rejects_junk(self):
        for bad in ("", "foo", "domain:", "domain:not a domain", "place_id:", "name_phone:x|1"):
            self.assertEqual(lca.clean_lead_key(bad), "", bad)
        self.assertEqual(lca.clean_lead_key("cid:123"), "cid:123")


class CardRenderTests(TempCwd):
    def test_every_eligible_card_gets_block_and_delete(self):
        out = dashboard.lead_cards(maps_rows(), selected_name="m.csv", csrf_token="t", owner_key="owner")
        self.assertEqual(out.count('class="lead-card-manage"'), 4)
        self.assertEqual(out.count('class="lead-card-block"'), 4)
        self.assertEqual(out.count('class="lead-card-delete"'), 4)
        for row in maps_rows():
            self.assertIn(f'data-lead-key="{row_listing_key(row)}"', out)
        # Add to CRM untouched (every Maps listing, keyed by its Google ID).
        self.assertEqual(out.count('class="lead-add-crm"'), 4)
        # Compare still offered for the single verified website only.
        self.assertEqual(out.count('class="lead-top-competitor"'), 1)

        out = dashboard.lead_cards(organic_rows(), selected_name="o.csv", csrf_token="t", owner_key="owner")
        self.assertEqual(out.count('class="lead-card-manage"'), 2)
        self.assertIn('data-lead-key="domain:alpha.example"', out)
        self.assertEqual(out.count('class="lead-add-crm"'), 2)

    def test_no_controls_for_master_guest_or_unselected(self):
        for kwargs in (dict(selected_name="leadbot_master.csv", owner_key="owner"),
                       dict(selected_name="m.csv", owner_key=""),
                       dict(selected_name="", owner_key="owner")):
            out = dashboard.lead_cards(maps_rows(), csrf_token="t", **kwargs)
            self.assertNotIn("lead-card-manage", out, kwargs)

    def test_old_hard_delete_form_is_gone_but_route_untouched(self):
        out = dashboard.lead_cards(maps_rows(), selected_name="m.csv", csrf_token="t", owner_key="owner")
        self.assertNotIn("lead-delete-one-form", out)
        src = (ROOT / "app/main.py").read_text(encoding="utf-8")
        self.assertIn('@app.post("/lead-bot/delete-row/{filename}")', src)

    def test_live_page_merges_controls_and_hidden_keys(self):
        src = (ROOT / "app/main.py").read_text(encoding="utf-8")
        start = src.index("async function addCompletedLiveCrmActions")
        body = src[start:src.index("function formatScanQuery", start)]
        self.assertIn(".lead-hidden-keys", body)
        self.assertIn(".lead-card-manage", body)
        self.assertIn("item.dataset.listingKey", body)
        dash = (ROOT / "agents/lead_dashboard_agent.py").read_text(encoding="utf-8")
        self.assertIn('/static/lead-card-actions.js?v=', dash)
        self.assertTrue((ROOT / "app/static/lead-card-actions.js").exists())


class RouteTests(TempCwd):
    """The real /lead-bot/lead-action route from app/main.py on a throwaway app."""

    def setUp(self):
        super().setUp()
        from fastapi import FastAPI, Form, Request
        from fastapi.responses import JSONResponse
        from fastapi.testclient import TestClient

        self.csrf_ok = True
        self.can_access = True
        app = FastAPI()
        ns = dict(
            app=app, AuthRequest=Request, AuthForm=Form, Path=Path, JSONResponse=JSONResponse,
            auth_current_user=lambda request: OWNER,
            leadbot_user_can_access_export=lambda name, request: self.can_access,
            _csrf_token_valid=lambda request, token: self.csrf_ok,
        )
        tree = ast.parse((ROOT / "app/main.py").read_text(encoding="utf-8"))
        funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "leadbot_lead_card_action"]
        self.assertEqual(len(funcs), 1)
        exec(compile(ast.Module(body=funcs, type_ignores=[]), "lead-action-harness", "exec"), ns)
        self.client = TestClient(app)

        self.maps_file = "leads_bagels_maps.csv"
        self.maps_path = self.write(self.maps_file, MAPS_FIELDS, maps_rows())
        self.org_file = "leads_plumbers.csv"
        self.org_path = self.write(self.org_file, ORGANIC_FIELDS, organic_rows())
        self.hashes = {p: sha(p) for p in (self.maps_path, self.org_path)}

    def act(self, action, lead_key, filename=None):
        return self.client.post("/lead-bot/lead-action", data=dict(
            action=action, lead_key=lead_key, filename=filename or self.maps_file, csrf_token="t"))

    def render(self, filename=None, rows=None):
        filename = filename or self.maps_file
        rows = rows if rows is not None else dashboard.read_csv_rows(Path("exports") / filename)
        return dashboard.lead_cards(rows, selected_name=filename, csrf_token="t", owner_key="owner")

    def tearDown(self):
        # No action ever rewrites an export CSV.
        for p, h in self.hashes.items():
            self.assertEqual(sha(p), h, p)

    def test_delete_no_website_lead_survives_refresh_and_undo_restores(self):
        r = self.act("delete", "place_id:ChIJ_nosite")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["scope"], "listing")
        out = self.render()  # "refresh"
        self.assertNotIn("No Site Bagels", out)
        self.assertIn("Chain Shirley", out)
        self.assertIn("Chain Mastic", out)
        self.assertIn('class="lead-hidden-keys"', out)
        self.assertIn("place_id:ChIJ_nosite", json.loads(
            out.split('data-keys="')[1].split('"')[0].replace("&quot;", '"')))
        # Delete is per export: the same lead in another export is unaffected.
        self.assertIn("No Site Bagels", self.render(filename=self.maps_file.replace(".csv", "") + "_x.csv",
                                                    rows=maps_rows()))
        # Not a block: future discovery is unaffected.
        self.assertEqual(lca.blocked_listing_keys("owner"), set())
        r = self.act("restore", "place_id:ChIJ_nosite")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("No Site Bagels", self.render())

    def test_block_one_chain_location_only(self):
        from agents.leadbot_block_gate import load_user_blocked_domains
        r = self.act("block", "place_id:ChIJ_chain_1")
        self.assertEqual(r.status_code, 200, r.text)
        out = self.render()
        self.assertNotIn("Chain Shirley", out)
        self.assertIn("Chain Mastic", out)          # other location of the chain survives
        self.assertIn("Solo Verified", out)
        self.assertIn("No Site Bagels", out)
        self.assertEqual(load_user_blocked_domains("owner"), set())  # chain.example NOT blocked
        self.assertEqual(lca.blocked_listing_keys("owner"), {"place_id:ChIJ_chain_1"})
        # Persisted across "refresh" in any export that holds the same listing.
        self.assertNotIn("Chain Shirley", self.render(filename="other.csv", rows=maps_rows()))
        # Another user is unaffected.
        self.assertEqual(lca.blocked_listing_keys("someone-else"), set())
        r = self.act("unblock", "place_id:ChIJ_chain_1")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("Chain Shirley", self.render())

    def test_block_verified_website_organic_lead_uses_personal_domain_block(self):
        from agents.leadbot_block_gate import load_effective_blocked_domains, load_user_blocked_domains
        r = self.act("block", "domain:alpha.example", filename=self.org_file)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["scope"], "domain")
        self.assertEqual(load_user_blocked_domains("owner"), {"alpha.example"})
        self.assertIn("alpha.example", load_effective_blocked_domains("owner"))
        out = self.render(filename=self.org_file)
        self.assertNotIn("Alpha Plumbing", out)
        self.assertIn("Beta Plumbing", out)
        self.act("unblock", "domain:alpha.example", filename=self.org_file)
        self.assertEqual(load_user_blocked_domains("owner"), set())
        self.assertIn("Alpha Plumbing", self.render(filename=self.org_file))

    def test_rejections(self):
        self.assertEqual(self.act("delete", "place_id:ChIJ_invented").status_code, 404)
        self.assertEqual(self.act("block", "domain:alpha.example").status_code, 404)  # not in maps file
        self.assertEqual(self.act("explode", "place_id:ChIJ_nosite").status_code, 400)
        self.assertEqual(self.act("delete", "garbage").status_code, 400)
        self.assertEqual(self.act("delete", "place_id:ChIJ_nosite", filename="leadbot_master.csv").status_code, 400)
        self.assertEqual(self.act("delete", "place_id:ChIJ_nosite", filename="../x.csv").status_code, 400)
        self.assertEqual(self.act("delete", "place_id:ChIJ_nosite", filename="missing.csv").status_code, 403)
        self.can_access = False
        self.assertEqual(self.act("delete", "place_id:ChIJ_nosite").status_code, 403)
        self.can_access = True
        self.csrf_ok = False
        r = self.act("delete", "place_id:ChIJ_nosite")
        self.assertEqual((r.status_code, r.json()["error_code"]), (403, "csrf_stale"))
        self.csrf_ok = True
        self.assertEqual(lca.deleted_lead_keys("owner", self.maps_file), set())
        self.assertEqual(lca.blocked_listing_keys("owner"), set())

    def test_crm_imported_lead_delete_leaves_crm_state_alone(self):
        # Stand-ins for the CRM queue/receipt files under data/.
        queue = Path("data/actii_crm_jobs.json")
        queue.write_text(json.dumps({"jobs": [{"source_ref": "leadmeleads-place:place_id:ChIJ_solo",
                                               "state": "added"}]}))
        before = sha(queue)
        r = self.act("delete", "place_id:ChIJ_solo")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.act("block", "place_id:ChIJ_solo").status_code, 200)
        self.assertEqual(sha(queue), before)
        self.assertEqual(sorted(p.name for p in Path("data").iterdir()),
                         ["actii_crm_jobs.json", "leadbot_lead_actions.sqlite"])
        src = (ROOT / "agents/lead_card_actions_agent.py").read_text(encoding="utf-8")
        self.assertNotIn("actii", src.split('"""', 2)[2].lower())


class FutureDiscoveryTests(TempCwd):
    def test_blocked_listing_is_rejected_by_maps_funnel_and_only_it(self):
        from agents import lead_maps_discovery_agent as discovery
        sys.path.insert(0, str(ROOT / "scripts"))
        from test_lead_maps_discovery import real_items
        items = real_items()
        base = discovery.build_candidate_funnel(items, "plumbers", "Shirley, NY", 25, ())
        target = base["selected"][0]
        key = row_listing_key(target)
        self.assertTrue(key)
        f = discovery.build_candidate_funnel(items, "plumbers", "Shirley, NY", 25, (), {key})
        self.assertEqual(f["counts"]["blocked"], base["counts"]["blocked"] + 1)
        self.assertEqual(f["counts"]["eligible"], base["counts"]["eligible"] - 1)
        self.assertNotIn(key, {row_listing_key(r) for r in f["selected"]})
        others = {row_listing_key(r) for r in base["selected"][1:]}
        self.assertTrue(others <= {row_listing_key(r) for r in f["selected"]})

    def test_run_path_loads_the_users_blocked_listings(self):
        src = (ROOT / "agents/lead_maps_discovery_agent.py").read_text(encoding="utf-8")
        self.assertIn("blocked_listing_keys(blocklist_owner_key(params))", src)
        self.assertIn("total_limit, blocked_domains,\n                                        blocked_listings)", src)


if __name__ == "__main__":
    unittest.main()
