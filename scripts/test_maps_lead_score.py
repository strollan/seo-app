"""Offline tests for the evidence-based Maps lead score (agents/lead_maps_discovery_agent.maps_lead_score).

No network, no paid calls. Run: python -m unittest scripts.test_maps_lead_score
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents import lead_maps_discovery_agent as maps  # noqa: E402


def lead(**kw):
    row = dict(best_phone="(631) 555-0100", outreach_status="call_ready", contact_page_url="",
               website="https://bagels.example/", url="https://bagels.example/", website_status="verified",
               website_verified=True, relevance="primary_category", has_street_address=True,
               rating_votes="120", maps_position="5", shared_domain_count=1)
    row.update(kw)
    return row


class MapsLeadScoreTests(unittest.TestCase):
    def score(self, **kw):
        return maps.maps_lead_score(lead(**kw))[0]

    def test_typical_verified_call_ready(self):
        score, basis = maps.maps_lead_score(lead())
        self.assertEqual(score, 15 + 20 + 20 + 7 + 8 + 6)  # 76
        self.assertIn("Phone +15", basis)
        self.assertIn("Verified website +20", basis)
        self.assertIn("Maps #5 +6", basis)

    def test_website_states_rank_in_order(self):
        verified = self.score()
        unverified = self.score(website_verified=False, website_status="unverified")
        social = self.score(website="", url="", website_verified=False, website_status="social_profile")
        missing = self.score(website="", url="", website_verified=False, website_status="missing")
        self.assertEqual(verified - unverified, 12)
        self.assertEqual(unverified - social, 5)
        self.assertEqual(social - missing, 3)

    def test_verified_vs_unverified_string_flags(self):
        # Saved CSV rows carry "True"/"False" strings.
        self.assertEqual(self.score(website_verified="True"), self.score(website_verified=True))
        self.assertEqual(self.score(website_verified="False", website_status="unverified"),
                         self.score(website_verified=False, website_status="unverified"))

    def test_contact_evidence(self):
        base = self.score(best_phone="", outreach_status="needs_review")
        self.assertEqual(self.score(outreach_status="call_ready") - base, 15)
        self.assertEqual(self.score(outreach_status="email_and_call_ready", contact_page_url="https://bagels.example/contact")
                         - base, 35)

    def test_reviews_relevance_and_position(self):
        self.assertGreater(self.score(rating_votes="40"), self.score(rating_votes="10"))
        self.assertGreater(self.score(rating_votes="10"), self.score(rating_votes="2"))
        self.assertGreater(self.score(rating_votes="2"), self.score(rating_votes=""))
        self.assertGreater(self.score(relevance="primary_category"), self.score(relevance="additional_category"))
        self.assertGreater(self.score(relevance="additional_category"), self.score(relevance="business_name"))
        self.assertGreater(self.score(maps_position="15"), self.score(maps_position="6"))
        self.assertGreater(self.score(maps_position="6"), self.score(maps_position="2"))

    def test_shared_website_penalty_and_bounds(self):
        self.assertEqual(self.score() - self.score(shared_domain_count=3), 10)
        top = self.score(outreach_status="email_and_call_ready", contact_page_url="x", maps_position="30")
        self.assertEqual(top, 100)
        self.assertEqual(maps.maps_lead_score({})[0], 0)
        self.assertEqual(maps.maps_lead_score({})[1], "No recorded evidence")

    def test_distinct_realistic_leads_spread(self):
        rows = [
            lead(),
            lead(rating_votes="8", maps_position="2"),
            lead(website="", url="", website_verified=False, website_status="missing", rating_votes="3", maps_position="17"),
            lead(website_verified=False, website_status="unverified", relevance="business_name", rating_votes="0"),
            lead(outreach_status="email_and_call_ready", contact_page_url="c", maps_position="12"),
            lead(best_phone="", outreach_status="needs_review", website="", url="", website_verified=False,
                 website_status="missing", has_street_address=False, rating_votes="", maps_position="40"),
        ]
        scores = [maps.maps_lead_score(r)[0] for r in rows]
        self.assertEqual(len(set(scores)), len(scores), scores)
        self.assertGreaterEqual(max(scores) - min(scores), 50, scores)


if __name__ == "__main__":
    unittest.main()
