"""
Regression tests for the Nassau County / Suffolk County precise-location
improvement, and permanent coverage for the pre-existing Long Island local
SERP guardrail.

Background (from this session's read-only audit of production commit
b8ab1215adcb6a607fabfc512d9bd7d0a61e1ec3): DataForSEO has no canonical
location representing the whole Long Island region (it spans Nassau and
Suffolk counties), so bare "Long Island" markets deliberately keep using
the broad "New York,New York,United States" location_name -- this predates
the recent location-formatting fixes and is left unchanged here. A
separate, already-live guardrail (_leadbot_row_allowed_for_local_market,
wired into search_google_organic() via a wrapper reassignment) filters out
NYC/borough/nearby-non-LI leakage from those broad results, and cross-
rejects Nassau-vs-Suffolk leakage for county-scoped searches. Before this
fix, that guardrail had no dedicated regression coverage.

Unlike Long Island as a whole, DataForSEO DOES have precise canonical
locations for each county (confirmed via a live locations lookup in the
audit): "Nassau County,New York,United States" (location_code 9058760) and
"Suffolk County,New York,United States" (location_code 1023413). This fix
updates only the "nassau county"/"nassau county ny" and "suffolk
county"/"suffolk county ny" alias values to use those precise locations
instead of the broad NYC value. Bare Long Island aliases and all other
existing aliases (Brooklyn, Queens, Bronx, Staten Island, NYC, Manhattan)
are unchanged.

Uses mocked provider behavior only -- no live provider request is made by
this file.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

import agents.dataforseo_serp_agent as dfs

BROAD_NY = "New York,New York,United States"
NASSAU_CANONICAL = "Nassau County,New York,United States"
SUFFOLK_CANONICAL = "Suffolk County,New York,United States"


class LongIslandAliasUnchangedTests(unittest.TestCase):
    """Every bare "Long Island" phrasing must keep normalizing identically
    to the existing broad canonical location -- this fix must not touch
    that behavior at all."""

    LONG_ISLAND_VARIANTS = [
        "Long Island",
        "long island",
        "LONG ISLAND",
        "Long Island NY",
        "Long Island, NY",
        "long island ny",
        "Long Island , NY",
        "Long Island New York",
        "Long Island, New York",
    ]

    def test_all_long_island_variants_normalize_identically_to_broad_ny(self):
        for market in self.LONG_ISLAND_VARIANTS:
            with self.subTest(market=market):
                self.assertEqual(dfs._location_name(market), BROAD_NY)

    def test_long_island_does_not_use_a_county_specific_location(self):
        for market in self.LONG_ISLAND_VARIANTS:
            with self.subTest(market=market):
                result = dfs._location_name(market)
                self.assertNotEqual(result, NASSAU_CANONICAL)
                self.assertNotEqual(result, SUFFOLK_CANONICAL)


class NassauCountyAliasTests(unittest.TestCase):
    """Nassau County aliases must now resolve to the precise canonical
    county location rather than the broad NYC value."""

    def test_nassau_county_bare(self):
        self.assertEqual(dfs._location_name("Nassau County"), NASSAU_CANONICAL)

    def test_nassau_county_ny_no_comma(self):
        self.assertEqual(dfs._location_name("Nassau County NY"), NASSAU_CANONICAL)

    def test_nassau_county_ny_with_comma(self):
        self.assertEqual(dfs._location_name("Nassau County, NY"), NASSAU_CANONICAL)

    def test_nassau_county_lowercase(self):
        self.assertEqual(dfs._location_name("nassau county ny"), NASSAU_CANONICAL)

    def test_nassau_county_uppercase(self):
        self.assertEqual(dfs._location_name("NASSAU COUNTY, NY"), NASSAU_CANONICAL)

    def test_nassau_county_extra_whitespace(self):
        self.assertEqual(dfs._location_name("Nassau   County ,  NY"), NASSAU_CANONICAL)


class SuffolkCountyAliasTests(unittest.TestCase):
    """Suffolk County aliases must now resolve to the precise canonical
    county location rather than the broad NYC value."""

    def test_suffolk_county_bare(self):
        self.assertEqual(dfs._location_name("Suffolk County"), SUFFOLK_CANONICAL)

    def test_suffolk_county_ny_no_comma(self):
        self.assertEqual(dfs._location_name("Suffolk County NY"), SUFFOLK_CANONICAL)

    def test_suffolk_county_ny_with_comma(self):
        self.assertEqual(dfs._location_name("Suffolk County, NY"), SUFFOLK_CANONICAL)

    def test_suffolk_county_lowercase(self):
        self.assertEqual(dfs._location_name("suffolk county ny"), SUFFOLK_CANONICAL)

    def test_suffolk_county_uppercase(self):
        self.assertEqual(dfs._location_name("SUFFOLK COUNTY, NY"), SUFFOLK_CANONICAL)

    def test_suffolk_county_extra_whitespace(self):
        self.assertEqual(dfs._location_name("Suffolk   County ,  NY"), SUFFOLK_CANONICAL)


class ExistingBoroughAndCityAliasesUnchangedTests(unittest.TestCase):
    """Confirms this fix touched only the Nassau/Suffolk county alias
    values -- every other existing alias must be byte-for-byte unchanged."""

    UNCHANGED_ALIASES = {
        "nyc": BROAD_NY,
        "new york city": BROAD_NY,
        "brooklyn": "Brooklyn,New York,United States",
        "brooklyn ny": "Brooklyn,New York,United States",
        "queens": "Queens,New York,United States",
        "queens ny": "Queens,New York,United States",
        "bronx": "Bronx,New York,United States",
        "bronx ny": "Bronx,New York,United States",
        "manhattan": BROAD_NY,
        "manhattan ny": BROAD_NY,
        "staten island": "Staten Island,New York,United States",
        "staten island ny": "Staten Island,New York,United States",
    }

    def test_unrelated_aliases_are_unchanged(self):
        for market, expected in self.UNCHANGED_ALIASES.items():
            with self.subTest(market=market):
                self.assertEqual(dfs._location_name(market), expected)


class GuardrailScopeDetectionTests(unittest.TestCase):
    """_leadbot_market_scope_for_guardrail() reads the raw market string
    directly (not the alias-resolved location_name), so it must still
    detect the right scope regardless of the alias value change."""

    def test_long_island_scope(self):
        self.assertEqual(dfs._leadbot_market_scope_for_guardrail("Long Island, NY"), "long_island")

    def test_nassau_scope(self):
        self.assertEqual(dfs._leadbot_market_scope_for_guardrail("Nassau County, NY"), "nassau")

    def test_suffolk_scope(self):
        self.assertEqual(dfs._leadbot_market_scope_for_guardrail("Suffolk County, NY"), "suffolk")

    def test_unrelated_market_has_no_scope(self):
        self.assertEqual(dfs._leadbot_market_scope_for_guardrail("Albany, NY"), "")


class GuardrailRowFilteringTests(unittest.TestCase):
    """Direct unit coverage of _leadbot_row_allowed_for_local_market() --
    this guardrail existed before this fix but had no dedicated test
    coverage; this locks in its documented behavior."""

    def _row(self, text):
        return {"title": text, "snippet": "", "address": ""}

    def test_manhattan_row_rejected_for_long_island_market(self):
        row = self._row("Joe's Plumbing, Manhattan NY")
        self.assertFalse(dfs._leadbot_row_allowed_for_local_market(row, "Long Island, NY"))

    def test_brooklyn_row_rejected_for_long_island_market(self):
        row = self._row("ABC Roofing, Brooklyn NY")
        self.assertFalse(dfs._leadbot_row_allowed_for_local_market(row, "Long Island, NY"))

    def test_queens_row_rejected_for_long_island_market(self):
        row = self._row("Best Electric, Queens NY")
        self.assertFalse(dfs._leadbot_row_allowed_for_local_market(row, "Long Island, NY"))

    def test_bronx_row_rejected_for_long_island_market(self):
        row = self._row("Bronx Auto Repair")
        self.assertFalse(dfs._leadbot_row_allowed_for_local_market(row, "Long Island, NY"))

    def test_staten_island_row_rejected_for_long_island_market(self):
        row = self._row("Staten Island Dental")
        self.assertFalse(dfs._leadbot_row_allowed_for_local_market(row, "Long Island, NY"))

    def test_jersey_city_row_rejected_for_long_island_market(self):
        row = self._row("Jersey City Locksmith")
        self.assertFalse(dfs._leadbot_row_allowed_for_local_market(row, "Long Island, NY"))

    def test_genuine_nassau_row_retained_for_long_island_market(self):
        row = self._row("Hempstead Hardware Store")
        self.assertTrue(dfs._leadbot_row_allowed_for_local_market(row, "Long Island, NY"))

    def test_genuine_suffolk_row_retained_for_long_island_market(self):
        row = self._row("Huntington Family Dentistry")
        self.assertTrue(dfs._leadbot_row_allowed_for_local_market(row, "Long Island, NY"))

    def test_nassau_only_search_rejects_suffolk_only_row(self):
        row = self._row("Huntington Family Dentistry")
        self.assertFalse(dfs._leadbot_row_allowed_for_local_market(row, "Nassau County, NY"))

    def test_suffolk_only_search_rejects_nassau_only_row(self):
        row = self._row("Hempstead Hardware Store")
        self.assertFalse(dfs._leadbot_row_allowed_for_local_market(row, "Suffolk County, NY"))

    def test_nassau_only_search_retains_genuine_nassau_row(self):
        row = self._row("Hempstead Hardware Store")
        self.assertTrue(dfs._leadbot_row_allowed_for_local_market(row, "Nassau County, NY"))

    def test_suffolk_only_search_retains_genuine_suffolk_row(self):
        row = self._row("Huntington Family Dentistry")
        self.assertTrue(dfs._leadbot_row_allowed_for_local_market(row, "Suffolk County, NY"))

    def test_unrelated_market_never_filters_rows(self):
        row = self._row("Manhattan Plumbing Co")
        self.assertTrue(dfs._leadbot_row_allowed_for_local_market(row, "Albany, NY"))


class SearchGoogleOrganicGuardrailIntegrationTests(unittest.TestCase):
    """End-to-end: search_google_organic() is itself the guardrail-wrapped
    function (reassigned at module scope). Confirms the wrapper actually
    filters rows returned by the underlying (mocked) DataForSEO call."""

    def test_manhattan_and_nassau_rows_mixed_only_nassau_survives_for_long_island(self):
        rows = [
            {"title": "Midtown Plumbing", "snippet": "", "link": "https://a.example"},
            {"title": "Hempstead Hardware", "snippet": "", "link": "https://b.example"},
        ]
        with mock.patch.object(dfs, "_leadbot_original_search_google_organic", return_value=rows):
            result = dfs.search_google_organic("plumber", "Long Island, NY", depth=10)

        titles = [row["title"] for row in result]
        self.assertEqual(titles, ["Hempstead Hardware"])

    def test_nassau_search_drops_suffolk_only_row(self):
        rows = [
            {"title": "Huntington Family Dentistry", "snippet": "", "link": "https://c.example"},
            {"title": "Hempstead Hardware", "snippet": "", "link": "https://d.example"},
        ]
        with mock.patch.object(dfs, "_leadbot_original_search_google_organic", return_value=rows):
            result = dfs.search_google_organic("dentist", "Nassau County, NY", depth=10)

        titles = [row["title"] for row in result]
        self.assertEqual(titles, ["Hempstead Hardware"])


class InvalidLocationNoProviderCallTests(unittest.TestCase):
    """Confirms an unresolvable market still never reaches the provider,
    unaffected by this fix."""

    def setUp(self):
        self._env_backup = os.environ.get("LEADBOT_DATAFORSEO_ENABLED")
        os.environ["LEADBOT_DATAFORSEO_ENABLED"] = "1"
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._env_backup is None:
            os.environ.pop("LEADBOT_DATAFORSEO_ENABLED", None)
        else:
            os.environ["LEADBOT_DATAFORSEO_ENABLED"] = self._env_backup

    def test_invalid_market_raises_before_any_request(self):
        with mock.patch.object(dfs, "requests") as mock_requests:
            with self.assertRaises(dfs.InvalidMarketLocationError):
                dfs._leadbot_original_search_google_organic("plumber", "Southampton", depth=10)
        mock_requests.post.assert_not_called()


class NoSerperFilesOrConfigurationTouchedTests(unittest.TestCase):
    """Permanent guard: this module must never reference Serper or
    USE_LIVE_SERP, and calling the new/changed aliases must never mutate
    process environment (e.g. Serper's enable flag)."""

    def test_module_source_has_no_serper_references(self):
        source = Path(dfs.__file__).read_text().lower()
        self.assertNotIn("serper", source)

    def test_resolving_new_aliases_does_not_touch_use_live_serp_env(self):
        before = os.environ.get("USE_LIVE_SERP")
        dfs._location_name("Nassau County")
        dfs._location_name("Suffolk County")
        dfs._location_name("Long Island, NY")
        after = os.environ.get("USE_LIVE_SERP")
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
