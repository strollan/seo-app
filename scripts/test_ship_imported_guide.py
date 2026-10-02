"""Focused unit checks for the Slayer imported-guide shipping boundary."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("ship-imported-guide")
SPEC = importlib.util.spec_from_loader("ship_imported_guide", importlib.machinery.SourceFileLoader("ship_imported_guide", str(SCRIPT)))
shipper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(shipper)


class ShipImportedGuideTests(unittest.TestCase):
    slug = "safe-guide"

    def _call(self, *, status="", staged="", deploy_code=0, state=None, validation_error=None):
        calls = []
        def fake_git(*args):
            calls.append(args)
            out = ""
            if args == ("rev-parse", "--is-inside-work-tree"): out = "true\n"
            elif args == ("branch", "--show-current"): out = "main\n"
            elif args == ("status", "--porcelain=v1", "--untracked-files=all"): out = status
            elif args == ("diff", "--cached", "--name-only"): out = staged
            elif args == ("rev-parse", "HEAD"): out = "a" * 40 + "\n"
            return subprocess.CompletedProcess(args, 0, out, "")
        output = StringIO()
        validator = mock.patch.object(shipper, "validate_imported_guide", side_effect=validation_error) if validation_error else mock.patch.object(shipper, "validate_imported_guide")
        with mock.patch.object(shipper, "git", side_effect=fake_git), validator, \
             mock.patch.object(shipper, "deploy_state", return_value=state or {"result": "PASS", "deployed_commit": "a" * 40}), \
             redirect_stdout(output):
            code = shipper.ship(self.slug, deploy_runner=lambda: subprocess.CompletedProcess([], deploy_code, "human deploy log", ""))
        return code, json.loads(output.getvalue()), calls

    def test_invalid_slug_has_json_failure_and_nonzero_exit(self):
        output = StringIO()
        with redirect_stdout(output): code = shipper.ship("BAD slug")
        self.assertEqual(code, 1); self.assertFalse(json.loads(output.getvalue())["ok"])

    def test_missing_or_invalid_manifest_refuses_before_git_changes(self):
        code, result, calls = self._call(validation_error=ValueError("missing manifest"))
        self.assertEqual(code, 1); self.assertIn("missing manifest", result["reason"])
        self.assertNotIn(("add", "-f", "--", "app/content/imported_guides/safe-guide.json", "app/static/images/resources/imported/safe-guide"), calls)

    def test_unrelated_tracked_or_staged_changes_refuse(self):
        code, result, calls = self._call(status=" M app/main.py\n")
        self.assertEqual(code, 1); self.assertIn("unrelated", result["reason"]); self.assertFalse(any(c[0] == "add" for c in calls))
        code, result, calls = self._call(staged="app/main.py\n")
        self.assertEqual(code, 1); self.assertIn("unrelated staged", result["reason"]); self.assertFalse(any(c[0] == "add" for c in calls))

    def test_unrelated_untracked_is_excluded_and_exact_paths_are_staged(self):
        code, result, calls = self._call(status="?? scratch.txt\n?? app/content/imported_guides/safe-guide.json\n?? app/static/images/resources/imported/safe-guide/hero.png\n")
        self.assertEqual(code, 0); self.assertTrue(result["ok"])
        self.assertIn(("add", "-f", "--", "app/content/imported_guides/safe-guide.json", "app/static/images/resources/imported/safe-guide"), calls)
        self.assertFalse(any("scratch.txt" in part for call in calls for part in call))

    def test_no_empty_commit_for_already_committed_article_and_deploy_failure_preserves_sha(self):
        code, result, calls = self._call(deploy_code=1)
        self.assertEqual(code, 1); self.assertEqual(result["commit_sha"], "a" * 40)
        self.assertFalse(any(c[0] == "commit" for c in calls))

    def _validate_assets(self, hero_url, inline_images=(), *, create_assets=()):
        """Run validation with an isolated repository and registry response."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        repo = Path(directory.name)
        manifest = repo / "app/content/imported_guides" / f"{self.slug}.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("{}", encoding="utf-8")
        for asset in create_assets:
            file = repo / "app" / asset.lstrip("/")
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b"image")

        imported_guides = types.ModuleType("app.imported_guides")
        imported_guides.load_imported_guides = mock.Mock(return_value={self.slug: {
            "hero_image": {"url": hero_url},
            "inline_images": [{"url": url} for url in inline_images],
        }})
        app = types.ModuleType("app")
        app.imported_guides = imported_guides
        with mock.patch.object(shipper, "REPO_PATH", repo), \
             mock.patch.dict(sys.modules, {"app": app, "app.imported_guides": imported_guides}):
            shipper.validate_imported_guide(self.slug)

    def test_valid_hero_url_resolves_below_app_static(self):
        asset = f"/static/images/resources/imported/{self.slug}/hero.png"
        self._validate_assets(asset, create_assets=(asset,))

    def test_valid_inline_image_resolves_below_app_static(self):
        hero = f"/static/images/resources/imported/{self.slug}/hero.png"
        inline = f"/static/images/resources/imported/{self.slug}/inline-chart.png"
        self._validate_assets(hero, (inline,), create_assets=(hero, inline))

    def test_genuinely_missing_asset_still_fails(self):
        asset = f"/static/images/resources/imported/{self.slug}/missing.png"
        with self.assertRaisesRegex(ValueError, "referenced imported article asset is missing"):
            self._validate_assets(asset)

    def test_asset_outside_imported_guide_path_still_fails(self):
        with self.assertRaisesRegex(ValueError, "invalid article asset URL"):
            self._validate_assets("/static/images/resources/not-imported/hero.png")


if __name__ == "__main__":
    unittest.main()
