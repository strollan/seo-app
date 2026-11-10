"""Add to CRM for Google Maps leads (with or without a website) and stale form tokens.

Synthetic data only: temporary exports, auth DB, queue and (optionally) a
disposable ACT II database. No network, no paid search, no real CRM.

Run: python -m unittest scripts.test_crm_maps_import
Set ACTII_TEST_ROOT to an ACT II checkout to also run the full
LeadMeLeads -> queue -> connector -> ACT II import path.
"""
import csv
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import test_actii_outbound_connector as outbound
import test_leadbot_crm_import as legacy

MAPS_FIELDS = ["title", "domain", "url", "best_phone", "keyword", "market", "website_status", "website_verified",
               "website_flag", "place_id", "cid", "category", "profile_url", "address", "final_lead_score",
               "source", "lead_source_label"]


def maps_row(**values):
    row = dict(title="", domain="", url="", best_phone="", keyword="plumbers", market="Shirley, NY",
               website_status="missing", website_verified="false", website_flag="No Website", place_id="",
               cid="", category="Plumber", profile_url="", address="", final_lead_score="40",
               source="dataforseo_maps", lead_source_label="Google Maps")
    row.update(values)
    return row


ROWS = [
    maps_row(title="No Site Plumbing", best_phone="(631) 555-0101", place_id="ChIJ_none", cid="1001",
             address="1 Main St, Shirley, NY", profile_url="https://maps.google.com/?cid=1001"),
    maps_row(title="Second No Site Plumbing", best_phone="631-555-0109", place_id="ChIJ_none2", cid="1009",
             address="9 Main St, Shirley, NY"),
    maps_row(title="Unverified Plumbing", domain="unverified.example", url="https://unverified.example/",
             best_phone="631-555-0102", website_status="unverified", website_flag="", place_id="ChIJ_unv",
             address="2 Main St"),
    maps_row(title="Chain Plumbing Shirley", domain="chain.example", url="https://chain.example/",
             best_phone="631-555-0103", website_status="verified", website_verified="true", website_flag="",
             place_id="ChIJ_c1", address="3 Main St"),
    maps_row(title="Chain Plumbing Mastic", domain="chain.example", url="https://chain.example/",
             best_phone="631-555-0104", website_status="verified", website_verified="true", website_flag="",
             place_id="ChIJ_c2", address="4 Main St"),
    maps_row(title="Good Plumbing", domain="good.example", url="https://good.example/", best_phone="631-555-0105",
             website_status="verified", website_verified="true", website_flag="", place_id="ChIJ_g",
             address="5 Main St"),
    maps_row(title="CID Only Plumbing", best_phone="631-555-0106", cid="7777", address="6 Main St"),
]


def write_maps_export(name, rows=ROWS):
    with Path("exports", name).open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=MAPS_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    owners = json.loads(Path("data/leadbot_export_owners.json").read_text())
    owners[name] = {"owner_username": "owner"}
    Path("data/leadbot_export_owners.json").write_text(json.dumps(owners))


class MapsFixtureMixin:
    maps_file = "leads_plumbers_shirleyny_20261110_180813.csv"

    def setUp_maps(self):
        write_maps_export(self.maps_file)
        Path("data/leadbot_live_jobs").mkdir(parents=True, exist_ok=True)
        Path("data/leadbot_live_jobs/job1.json").write_text(json.dumps({
            "export_file": self.maps_file, "created_at": "2026-11-10 18:07:54",
            "params": {"keyword": "plumbers", "market": "Shirley, NY"}}))

    def post_maps(self, listing_key, name, file=None, **lead):
        body = dict(self.payload, source_file=file or self.maps_file, source_domain="", listing_key=listing_key,
                    lead=dict(company_name=name, **lead))
        return self.client.post("/lead-bot/add-to-crm", json=body)

    def sent(self, index=-1):
        return json.loads(self.transport.call_args_list[index].kwargs["data"])


class LocalModeMapsTests(MapsFixtureMixin, unittest.TestCase):
    """Route behaviour (explicit local bridge mode, transport mocked)."""

    def setUp(self):
        legacy.CrmImportTests.setUp(self)
        self.setUp_maps()

    def test_no_website_listing_is_imported_with_website_missing(self):
        response = self.post_maps("place_id:ChIJ_none", "No Site Plumbing")
        self.assertEqual(response.status_code, 200, response.text)
        body = self.sent()
        self.assertEqual(body["owner_id"], "owner")
        self.assertEqual(body["source_ref"], "leadmeleads-place:place_id:ChIJ_none")
        lead = body["lead"]
        self.assertEqual((lead["website"], lead["domain"]), ("", ""))
        self.assertEqual(lead["company_name"], "No Site Plumbing")
        self.assertEqual(lead["phone"], "(631) 555-0101")
        self.assertEqual(lead["address"], "1 Main St, Shirley, NY")
        self.assertEqual((lead["category"], lead["place_id"], lead["cid"]), ("Plumber", "ChIJ_none", "1001"))
        note = lead["source_note"]
        for text in ("Domain: none (no verified website).", "Google Maps category: Plumber.",
                     "Google place ID: ChIJ_none.", "Google CID: 1001."):
            self.assertIn(text, note)
        self.assertNotIn("Domain: .", note)
        self.assertEqual(lead["search_metadata"]["base_keyword"], "plumbers")
        self.assertEqual(lead["search_metadata"]["search_location"], "Shirley, NY")
        self.assertEqual(lead["search_metadata"]["ranking_type"], "nonorganic")

    def test_unverified_website_is_not_used_as_the_crm_website(self):
        self.assertEqual(self.post_maps("place_id:ChIJ_unv", "Unverified Plumbing").status_code, 200)
        lead = self.sent()["lead"]
        self.assertEqual((lead["website"], lead["domain"]), ("", ""))
        self.assertIn("Website listed on Google (not verified): https://unverified.example/.", lead["source_note"])

    def test_verified_website_kept_and_chain_locations_are_separate(self):
        for key, name in (("place_id:ChIJ_c1", "Chain Plumbing Shirley"), ("place_id:ChIJ_c2", "Chain Plumbing Mastic"),
                          ("place_id:ChIJ_g", "Good Plumbing")):
            self.assertEqual(self.post_maps(key, name).status_code, 200)
        refs = [self.sent(i)["source_ref"] for i in range(3)]
        self.assertEqual(len(set(refs)), 3)
        self.assertEqual([self.sent(i)["lead"]["website"] for i in range(3)],
                         ["https://chain.example/", "https://chain.example/", "https://good.example/"])
        self.assertEqual([self.sent(i)["lead"]["address"] for i in range(2)], ["3 Main St", "4 Main St"])

    def test_cid_only_listing_uses_cid_identity(self):
        self.assertEqual(self.post_maps("cid:7777", "CID Only Plumbing").status_code, 200)
        self.assertEqual(self.sent()["source_ref"], "leadmeleads-place:cid:7777")

    def test_never_invents_a_website_for_a_no_website_listing(self):
        response = self.post_maps("place_id:ChIJ_none", "No Site Plumbing", website="https://invented.example")
        self.assertEqual(response.status_code, 422)
        self.assertIn("no verified website", response.json()["error"])
        self.transport.assert_not_called()

    def test_unknown_malformed_or_missing_listing_key_never_imports(self):
        for key in ("place_id:ChIJ_missing", "place_id:", "domain:chain.example", "ChIJ_none", ""):
            response = self.post_maps(key, "Anything")
            self.assertIn(response.status_code, (403, 422), key)
        # A domain alone cannot select a Maps row (chain rows share it, No Website rows share "").
        body = dict(self.payload, source_file=self.maps_file, source_domain="chain.example", lead=dict(company_name="x"))
        self.assertEqual(self.client.post("/lead-bot/add-to-crm", json=body).status_code, 422)
        self.transport.assert_not_called()

    def test_other_users_export_is_refused(self):
        owners = json.loads(Path("data/leadbot_export_owners.json").read_text())
        owners[self.maps_file] = {"owner_username": "someone-else"}
        Path("data/leadbot_export_owners.json").write_text(json.dumps(owners))
        self.assertEqual(self.post_maps("place_id:ChIJ_none", "No Site Plumbing").status_code, 403)
        self.transport.assert_not_called()

    def test_stale_form_token_is_not_reported_as_an_ended_session(self):
        self.payload["csrf_token"] = "replaced-by-a-newer-page-render"
        response = self.post_maps("place_id:ChIJ_none", "No Site Plumbing")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error_code"], "csrf_stale")
        self.assertNotIn("expired", response.json()["error"].lower())
        # The page can fetch a fresh token for the SAME sign-in and succeed.
        fresh = self.client.get("/lead-bot/csrf-token")
        self.assertEqual(fresh.status_code, 200)
        self.assertEqual(fresh.headers["cache-control"], "no-store, private")
        self.payload["csrf_token"] = fresh.json()["csrf_token"]
        self.assertEqual(self.post_maps("place_id:ChIJ_none", "No Site Plumbing").status_code, 200)

    def test_signed_out_is_distinct_from_a_stale_token(self):
        self.client.cookies.clear()
        response = self.post_maps("place_id:ChIJ_none", "No Site Plumbing")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error_code"], "signed_out")
        self.assertEqual(self.client.get("/lead-bot/csrf-token").status_code, 401)
        self.transport.assert_not_called()

    def test_organic_import_unchanged(self):
        self.assertEqual(legacy.CrmImportTests.send(self).status_code, 200)
        self.assertEqual(self.sent()["source_ref"], "leadmeleads:synthetic.csv:business.example")


class QueueModeMapsTests(MapsFixtureMixin, outbound.QueueFixture):
    def setUp(self):
        super().setUp()
        self.setUp_maps()

    def test_queue_job_identity_dedupes_across_scans_and_keeps_locations_apart(self):
        first = self.post_maps("place_id:ChIJ_none", "No Site Plumbing")
        self.assertEqual(first.status_code, 202, first.text)
        item = first.json()
        self.assertEqual((item["state"], item["listing_key"], item["source_domain"]), ("queued", "place_id:ChIJ_none", ""))
        # Clicking again, or adding the same listing from a later scan, reuses the job.
        self.assertEqual(self.post_maps("place_id:ChIJ_none", "No Site Plumbing").json()["id"], item["id"])
        write_maps_export("leads_plumbers_shirleyny_later.csv")
        self.assertEqual(self.post_maps("place_id:ChIJ_none", "No Site Plumbing", file="leads_plumbers_shirleyny_later.csv").json()["id"], item["id"])
        # Other listings (another no-website business, chain locations) are separate jobs.
        others = {self.post_maps(key, "x").json()["id"]
                  for key in ("place_id:ChIJ_none2", "place_id:ChIJ_c1", "place_id:ChIJ_c2")}
        self.assertEqual(len(others | {item["id"]}), 4)
        jobs = self.status_client.get("/lead-bot/crm-jobs").json()["jobs"]
        self.assertEqual(sorted(job.get("listing_key") for job in jobs),
                         ["place_id:ChIJ_c1", "place_id:ChIJ_c2", "place_id:ChIJ_none", "place_id:ChIJ_none2"])
        self.transport.assert_not_called()

    def test_claimed_job_passes_connector_validation_and_tampering_fails(self):
        self.post_maps("place_id:ChIJ_none", "No Site Plumbing")
        job = self.claim()
        self.assertEqual(job["listing_key"], "place_id:ChIJ_none")
        self.assertEqual(job["payload"]["source_ref"], "leadmeleads-place:place_id:ChIJ_none")
        config = {key: self.binding[key] for key in ("connector_id", "binding_version", "user_id", "username", "expires_at")}
        worker = outbound.PullConnector.__new__(outbound.PullConnector)
        worker.config, worker.clock = config, lambda: self.now[0]
        worker._validate_job(job)
        for change in ({"listing_key": "place_id:ChIJ_other"}, {"source_domain": "invented.example"}):
            with self.assertRaises(outbound.ConnectorError):
                worker._validate_job({**job, **change})
        tampered = json.loads(json.dumps(job))
        tampered["payload"]["lead"]["website"] = "https://invented.example"
        tampered["payload_hash"] = outbound.queue_module.payload_hash(tampered["payload"])
        with self.assertRaises(outbound.ConnectorError):
            worker._validate_job(tampered)

    def test_listing_removed_from_export_revokes_pending_job(self):
        item = self.post_maps("place_id:ChIJ_none", "No Site Plumbing").json()
        write_maps_export(self.maps_file, [row for row in ROWS if row["place_id"] != "ChIJ_none"])
        self.assertEqual(self.state(item["id"]), "failed")


@unittest.skipUnless(os.getenv("ACTII_TEST_ROOT"), "ACT II checkout not supplied")
class FullPathMapsTests(MapsFixtureMixin, outbound.CompanionTests):
    """LeadMeLeads route -> durable queue -> workstation connector -> real ACT II import."""

    def setUp(self):
        super().setUp()
        self.setUp_maps()

    def add(self, key, name, file=None):
        response = self.post_maps(key, name, file=file)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def contacts(self):
        return self.crm.state.store.contacts("Workspace")

    def test_no_website_listing_reaches_actii_once(self):
        item = self.add("place_id:ChIJ_none", "No Site Plumbing")
        self.assertEqual(self.worker.run_once(), "Added.")
        self.assertEqual(self.state(item["id"]), "added")
        (contact,) = self.contacts()
        self.assertEqual(self.crm.state.store.contacts("owner"), [])  # mapped workspace owner, not the raw username
        self.assertEqual((contact["company_name"], contact["website"], contact["phone"], contact["address"]),
                         ("No Site Plumbing", "", "(631) 555-0101", "1 Main St, Shirley, NY"))
        self.assertEqual(contact["preferred_contact_method"], "Phone")
        record = self.crm.state.store.import_record("Workspace", contact["id"])
        self.assertEqual(record["source_ref"], "leadmeleads-place:place_id:ChIJ_none")
        for text in ("Google place ID: ChIJ_none", "Google CID: 1001", "Google Maps category: Plumber"):
            self.assertIn(text, record["source_payload"])
        # The same listing from a later scan: same job, nothing new sent to ACT II.
        write_maps_export("leads_plumbers_shirleyny_later.csv")
        self.assertEqual(self.add("place_id:ChIJ_none", "No Site Plumbing", "leads_plumbers_shirleyny_later.csv")["id"], item["id"])
        self.assertEqual(self.worker.run_once(), "No pending import.")
        self.assertEqual(len(self.contacts()), 1)

    def test_listing_already_in_actii_reports_already_present(self):
        # e.g. imported earlier, then the queue's job record was retired/lost.
        self.crm.state.store.import_lead("Workspace", {"company_name": "No Site Plumbing", "phone": "(631) 555-0101"},
                                         "leadmeleads-place:place_id:ChIJ_none", "earlier import")
        item = self.add("place_id:ChIJ_none", "No Site Plumbing")
        self.worker.run_once()
        self.assertEqual(self.state(item["id"]), "already_present")
        self.assertEqual(len(self.contacts()), 1)

    def test_chain_locations_and_other_no_website_businesses_stay_separate(self):
        for key, name in (("place_id:ChIJ_c1", "Chain Plumbing Shirley"), ("place_id:ChIJ_c2", "Chain Plumbing Mastic"),
                          ("place_id:ChIJ_none", "No Site Plumbing"), ("place_id:ChIJ_none2", "Second No Site Plumbing")):
            item = self.add(key, name)
            self.worker.run_once()
            self.assertEqual(self.state(item["id"]), "added", key)
        contacts = self.contacts()
        self.assertEqual(len(contacts), 4)
        self.assertEqual(sorted(c["address"] for c in contacts if c["website"] == "https://chain.example/"),
                         ["3 Main St", "4 Main St"])

    def review_case(self, existing, key, name):
        # needs_review is terminal and pauses the connector for the operator, by design.
        self.crm.state.store.import_lead("Workspace", existing, "manual:1", "manual")
        item = self.add(key, name)
        self.worker.run_once()
        self.assertEqual(self.state(item["id"]), "needs_review")
        self.assertEqual(len(self.contacts()), 1)  # nothing merged, nothing duplicated

    def test_existing_contact_with_same_phone_needs_review(self):
        self.review_case({"company_name": "No Site Plumbing Inc", "phone": "631.555.0101"}, "place_id:ChIJ_none", "No Site Plumbing")

    def test_existing_contact_with_same_website_needs_review(self):
        self.review_case({"company_name": "Good", "website": "https://www.good.example"}, "place_id:ChIJ_g", "Good Plumbing")

    def test_organic_import_path_unchanged(self):
        item = self.enqueue()
        self.assertEqual(self.worker.run_once(), "Added.")
        self.assertEqual(self.state(item["id"]), "added")


class CardsTokenTests(unittest.TestCase):
    """GET /lead-bot/cards must not invalidate the token of the page that asked for them."""

    def setUp(self):
        import test_leadbot_csrf_routes as routes
        self.case = routes.LeadBotCardsRouteOwnershipTests("test_owning_standard_user_can_view_cards")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_valid_page_token_is_reused_and_stays_valid(self):
        case = self.case
        session, page_token = case.login(case.client, "user1")
        response = case.client.get(f"/lead-bot/cards/{case.export.filename}", headers={"X-LeadBot-CSRF": page_token})
        self.assertEqual(response.status_code, 200)
        self.assertIn(page_token, response.text)
        from agents.auth_agent import verify_csrf_token
        self.assertTrue(verify_csrf_token(session, page_token))

    def test_without_a_valid_page_token_a_new_one_is_issued(self):
        case = self.case
        session, page_token = case.login(case.client, "user1")
        response = case.client.get(f"/lead-bot/cards/{case.export.filename}", headers={"X-LeadBot-CSRF": "forged"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("forged", response.text)
        from agents.auth_agent import verify_csrf_token
        self.assertFalse(verify_csrf_token(session, page_token))


if __name__ == "__main__":
    unittest.main()
