"""
Tests for scripts/leadme_ship_pr.py (the ./scripts/leadme-ship.sh pipeline).

These tests never touch the real repo state, real network, real SSH, or
real GitHub — every git/gh call goes through leadme_deploy.run_local() and
every SSH call through leadme_deploy.run_remote(), both of which
leadme_ship_pr.py calls as `ld.run_local(...)` / `ld.run_remote(...)`
(never through a locally-aliased copy), so patching `ld.run_local` /
`ld.run_remote` here controls every call this module makes, including the
ones it makes indirectly through leadme_deploy's own remote_*() helpers.

None of these tests push, merge a PR, delete a branch, back up a database,
or deploy anything for real.
"""

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import leadme_deploy as ld  # noqa: E402
import leadme_ship_pr as lsp  # noqa: E402


class FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    @property
    def ok(self):
        return self.returncode == 0


class FakeRouter:
    """Maps a predicate over the argv/command string to a canned FakeResult."""

    def __init__(self):
        self.local_rules = []
        self.remote_rules = []
        self.local_calls = []
        self.remote_calls = []

    def add_local(self, matcher, result):
        self.local_rules.append((matcher, result))

    def add_remote(self, matcher, result):
        self.remote_rules.append((matcher, result))

    def run_local(self, args, cwd=None, timeout=None, input_text=None):
        self.local_calls.append(args)
        for matcher, result in self.local_rules:
            if matcher(args):
                return result() if callable(result) else result
        return FakeResult(1, "", f"no fake rule for: {args}")

    def run_remote(self, remote_cmd, timeout=None):
        self.remote_calls.append(remote_cmd)
        for matcher, result in self.remote_rules:
            if matcher(remote_cmd):
                return result() if callable(result) else result
        return FakeResult(1, "", f"no fake rule for remote cmd: {remote_cmd}")


def contains(*tokens):
    def matcher(argv_or_str):
        haystack = " ".join(argv_or_str) if isinstance(argv_or_str, list) else argv_or_str
        return all(tok in haystack for tok in tokens)
    return matcher


def ns(**kwargs):
    import argparse
    defaults = {"no_deploy": False}
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

class PreflightTests(unittest.TestCase):
    def _router_on_branch(self, branch, clean=True):
        router = FakeRouter()
        router.add_local(contains("branch", "--show-current"), FakeResult(0, f"{branch}\n"))
        router.add_local(contains("diff", "--quiet"), FakeResult(0 if clean else 1))
        router.add_local(contains("diff", "--cached", "--quiet"), FakeResult(0))
        router.add_local(contains("status", "--porcelain"), FakeResult(0, ""))
        return router

    def test_refuses_main(self):
        router = self._router_on_branch("main")
        with mock.patch.object(ld, "run_local", router.run_local), \
             mock.patch.object(lsp.Path, "is_dir", return_value=True), \
             mock.patch("pathlib.Path.exists", return_value=True):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lsp.cmd_ship(ns())
        self.assertEqual(rc, 1)
        out = buf.getvalue()
        self.assertIn("SHIP FAILED", out)
        self.assertIn("refusing to ship directly from main", out)
        # Never pushes when refused at preflight.
        self.assertTrue(all("push" not in " ".join(c) for c in router.local_calls))

    def test_refuses_dirty_tracked_feature_branch(self):
        router = self._router_on_branch("feature/x", clean=False)
        with mock.patch.object(ld, "run_local", router.run_local), \
             mock.patch("pathlib.Path.exists", return_value=True):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lsp.cmd_ship(ns())
        self.assertEqual(rc, 1)
        out = buf.getvalue()
        self.assertIn("unexpected tracked modifications on feature branch", out)
        self.assertTrue(all("push" not in " ".join(c) for c in router.local_calls))

    def test_refuses_when_no_commits_ahead_of_origin_main(self):
        router = self._router_on_branch("feature/x")
        router.add_local(contains("fetch", "origin"), FakeResult(0))
        router.add_local(contains("rev-list", "--count"), FakeResult(0, "0\n"))
        with mock.patch.object(ld, "run_local", router.run_local), \
             mock.patch("pathlib.Path.exists", return_value=True):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lsp.cmd_ship(ns())
        self.assertEqual(rc, 1)
        self.assertIn("has no commits ahead of origin/main", buf.getvalue())

    def test_stops_when_gh_not_found(self):
        router = self._router_on_branch("feature/x")
        router.add_local(contains("fetch", "origin"), FakeResult(0))
        router.add_local(contains("rev-list", "--count"), FakeResult(0, "2\n"))
        router.add_local(contains("diff", "--check"), FakeResult(0))
        with mock.patch.object(ld, "run_local", router.run_local), \
             mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch.object(lsp, "find_gh_binary", return_value=None):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lsp.cmd_ship(ns())
        self.assertEqual(rc, 1)
        self.assertIn("GitHub CLI not found", buf.getvalue())


# ---------------------------------------------------------------------------
# gh binary discovery
# ---------------------------------------------------------------------------

class GhDiscoveryTests(unittest.TestCase):
    def test_prefers_gh_on_path(self):
        with mock.patch.object(lsp.shutil, "which", return_value="/usr/bin/gh"):
            self.assertEqual(lsp.find_gh_binary(), "/usr/bin/gh")

    def test_falls_back_to_windows_gh_exe(self):
        win_path = "/mnt/c/Program Files/GitHub CLI/gh.exe"
        with mock.patch.object(lsp.shutil, "which", return_value=None), \
             mock.patch.object(Path, "exists", lambda self: str(self) == win_path):
            self.assertEqual(lsp.find_gh_binary(), win_path)

    def test_returns_none_when_neither_found(self):
        with mock.patch.object(lsp.shutil, "which", return_value=None), \
             mock.patch.object(Path, "exists", return_value=False):
            self.assertIsNone(lsp.find_gh_binary())


# ---------------------------------------------------------------------------
# Repository identity / WSL reuse — this module must not reimplement any of
# leadme_deploy's git.exe/Linux-git routing or repo-path resolution; it must
# only ever call through ld.run_local()/ld.REPO_PATH.
# ---------------------------------------------------------------------------

class RepoIdentityReuseTests(unittest.TestCase):
    def test_repo_path_is_shared_with_leadme_deploy_not_recomputed(self):
        """No separate Windows-path-parsing logic exists in this module —
        it inherits whatever leadme_deploy already resolved."""
        self.assertEqual(lsp.REPO_PATH, ld.REPO_PATH)

    def test_git_helper_routes_through_shared_run_local(self):
        """git() must call ld.run_local (which owns the git.exe/Linux-git
        selection) rather than invoking subprocess or git.exe directly."""
        router = FakeRouter()
        router.add_local(contains("status"), FakeResult(0, "clean"))
        with mock.patch.object(ld, "run_local", router.run_local):
            lsp.git(["status"])
        self.assertEqual(len(router.local_calls), 1)
        self.assertEqual(router.local_calls[0][0], "git")

    def test_alternate_worktree_repo_path_is_honored(self):
        """Overriding REPO_PATH (as happens when this script is copied into
        another worktree) must be the only thing that changes which path
        commands run against — no hardcoded primary-checkout path anywhere."""
        alt = Path("/home/scot/some/other/worktree")
        captured = {}

        def fake_run_local(args, cwd=None, timeout=None, input_text=None):
            captured["cwd"] = cwd
            return FakeResult(0, "ok")

        with mock.patch.object(ld, "run_local", fake_run_local), \
             mock.patch.object(lsp, "REPO_PATH", alt):
            lsp.git(["status"])
        self.assertEqual(captured["cwd"], alt)

    def test_no_direct_subprocess_or_git_exe_usage_in_module(self):
        """Static guard: this module must not import subprocess or define
        its own git.exe/Linux-git selection logic — that lives solely in
        leadme_deploy.py and must not be duplicated here (a comment merely
        mentioning "git.exe" while describing reused behavior is fine)."""
        src = Path(lsp.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import subprocess", src)
        self.assertNotIn("_select_git_binary", src)
        self.assertNotIn("WINDOWS_GIT_EXE", src)
        self.assertNotIn('"git.exe"', src)


# ---------------------------------------------------------------------------
# PR reuse / already-merged handling
# ---------------------------------------------------------------------------

class PullRequestTests(unittest.TestCase):
    def test_reuses_existing_open_pr_without_creating_a_duplicate(self):
        router = FakeRouter()
        router.add_local(
            contains("pr", "list"),
            FakeResult(0, '[{"number": 7, "url": "https://github.com/strollan/seo-app/pull/7", "state": "OPEN"}]'),
        )
        with mock.patch.object(ld, "run_local", router.run_local):
            pr, res = lsp.find_pr_for_branch("gh", "feature/x", "strollan/seo-app")
        self.assertEqual(pr["number"], 7)
        self.assertEqual(pr["state"], "OPEN")

    def test_handles_already_merged_pr(self):
        router = FakeRouter()
        router.add_local(
            contains("pr", "list"),
            FakeResult(0, '[{"number": 7, "url": "https://github.com/strollan/seo-app/pull/7", "state": "MERGED"}]'),
        )
        with mock.patch.object(ld, "run_local", router.run_local):
            pr, res = lsp.find_pr_for_branch("gh", "feature/x", "strollan/seo-app")
        self.assertEqual(pr["state"], "MERGED")

    def test_no_pr_found_returns_none(self):
        router = FakeRouter()
        router.add_local(contains("pr", "list"), FakeResult(0, "[]"))
        with mock.patch.object(ld, "run_local", router.run_local):
            pr, res = lsp.find_pr_for_branch("gh", "feature/x", "strollan/seo-app")
        self.assertIsNone(pr)

    def test_merge_pr_treats_already_merged_error_as_success(self):
        router = FakeRouter()
        router.add_local(
            contains("pr", "merge"),
            FakeResult(1, "", "GraphQL: Pull request is not open (already merged)"),
        )
        with mock.patch.object(ld, "run_local", router.run_local):
            ok, detail = lsp.merge_pr("gh", 7, "strollan/seo-app")
        self.assertTrue(ok)

    def test_merge_pr_real_failure_is_not_swallowed(self):
        router = FakeRouter()
        router.add_local(contains("pr", "merge"), FakeResult(1, "", "required check has not passed"))
        with mock.patch.object(ld, "run_local", router.run_local):
            ok, detail = lsp.merge_pr("gh", 7, "strollan/seo-app")
        self.assertFalse(ok)
        self.assertIn("required check", detail)

    def test_delete_remote_branch_missing_ref_is_idempotent_success(self):
        router = FakeRouter()
        router.add_local(
            contains("push", "origin", "--delete"),
            FakeResult(1, "", "error: unable to delete 'feature/x': remote ref does not exist"),
        )
        with mock.patch.object(ld, "run_local", router.run_local):
            ok, detail = lsp.delete_remote_branch("feature/x")
        self.assertTrue(ok)

    def test_delete_remote_branch_real_failure_is_not_swallowed(self):
        router = FakeRouter()
        router.add_local(contains("push", "origin", "--delete"), FakeResult(1, "", "Permission denied"))
        with mock.patch.object(ld, "run_local", router.run_local):
            ok, detail = lsp.delete_remote_branch("feature/x")
        self.assertFalse(ok)

    def test_push_already_up_to_date_is_treated_as_success(self):
        router = FakeRouter()
        router.add_local(contains("push", "-u", "origin"), FakeResult(1, "", "Everything up-to-date"))
        with mock.patch.object(ld, "run_local", router.run_local):
            ok, detail = lsp.push_feature_branch("feature/x")
        # push_feature_branch itself reports raw ok; cmd_ship is what treats
        # "up-to-date" text as success — covered by the full pipeline test.
        self.assertFalse(ok)
        self.assertIn("up-to-date", detail)


# ---------------------------------------------------------------------------
# owner/repo parsing
# ---------------------------------------------------------------------------

class OwnerRepoParsingTests(unittest.TestCase):
    def test_https_url(self):
        self.assertEqual(lsp.owner_repo_from_url("https://github.com/strollan/seo-app.git"), "strollan/seo-app")

    def test_ssh_url(self):
        self.assertEqual(lsp.owner_repo_from_url("git@github.com:strollan/seo-app.git"), "strollan/seo-app")

    def test_no_git_suffix(self):
        self.assertEqual(lsp.owner_repo_from_url("https://github.com/strollan/seo-app"), "strollan/seo-app")

    def test_garbage_url_returns_none(self):
        self.assertIsNone(lsp.owner_repo_from_url("not a url"))

    def test_none_url_returns_none(self):
        self.assertIsNone(lsp.owner_repo_from_url(None))


# ---------------------------------------------------------------------------
# Production deploy phase — fails safely, idempotent reruns
# ---------------------------------------------------------------------------

class ProductionSafetyBaseRouter:
    """Shared remote-call fixture for the _deploy()-phase tests."""

    @staticmethod
    def base(expected_sha="deadbeef" * 5, prod_sha=None, prod_branch="main", prod_clean=True):
        prod_sha = prod_sha or expected_sha
        router = FakeRouter()
        router.add_remote(contains("hostname"), FakeResult(0, "prod-host\n"))
        router.add_remote(contains("test -d"), FakeResult(0, "EXISTS\n"))
        router.add_remote(contains("is-inside-work-tree"), FakeResult(0, "true\n"))
        router.add_remote(contains("git", "branch", "--show-current"), FakeResult(0, f"{prod_branch}\n"))
        # The remote command's own exit status is always that of its final
        # `echo "$a $b"`, i.e. 0 — dirty/clean is decided by parsing that
        # echoed "$a $b" string, never by this call's returncode.
        router.add_remote(
            contains("git diff --quiet"),
            FakeResult(0, "0 0" if prod_clean else "1 0"),
        )
        router.add_remote(contains("venv/bin/python"), FakeResult(0, "EXISTS\n"))
        router.add_remote(
            contains("systemctl show"),
            FakeResult(0, "LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n"),
        )
        router.add_remote(contains("git rev-parse HEAD"), FakeResult(0, f"{prod_sha}\n"))
        return router


class DeployIdempotencyAndSafetyTests(unittest.TestCase):
    def _mk_ctx(self):
        return {
            "pushed": True, "pr_merged": True, "pr_url": "u", "branch_deleted": True,
            "backup_path": None, "production_sha": None, "compile_ok": False,
            "service_state": None, "local_smoke": None, "public_smoke": None, "final": "FAIL",
        }

    def test_fails_safely_on_dirty_production(self):
        expected = "a" * 40
        router = ProductionSafetyBaseRouter.base(expected_sha=expected, prod_sha="b" * 40, prod_clean=False)
        r = lsp.Reporter()
        with mock.patch.object(ld, "run_remote", router.run_remote):
            rc = lsp._deploy(r, self._mk_ctx(), expected)
        self.assertEqual(rc, 1)
        out = r.render()
        self.assertIn("unexpected tracked modifications on production", out)
        # Must never attempt backup or deploy once dirty production is found.
        joined = " ".join(router.remote_calls)
        self.assertNotIn("cp -p", joined)
        self.assertNotIn("git pull", joined)

    def test_fails_safely_on_sha_mismatch_after_pull(self):
        expected = "a" * 40
        router = ProductionSafetyBaseRouter.base(expected_sha=expected, prod_sha="b" * 40)
        # After pull, rev-parse HEAD still returns the wrong sha (mismatch).
        router.add_remote(contains("git fetch origin"), FakeResult(0))
        router.add_remote(contains("git checkout"), FakeResult(0))
        router.add_remote(contains("git pull"), FakeResult(0))
        router.add_remote(contains("stat -c %s"), FakeResult(0, "1024"))
        router.add_remote(contains("mkdir -p"), FakeResult(0))
        router.add_remote(contains("cp -p"), FakeResult(0))
        router.add_remote(contains("sha256sum"), FakeResult(0, "abc123  x\n"))
        r = lsp.Reporter()
        with mock.patch.object(ld, "run_remote", router.run_remote):
            rc = lsp._deploy(r, self._mk_ctx(), expected)
        self.assertEqual(rc, 1)
        out = r.render()
        self.assertIn("does not match the merged main SHA", out)
        # Restart must never be attempted after a SHA mismatch.
        self.assertNotIn("systemctl restart", " ".join(router.remote_calls))

    def test_backup_failure_stops_before_deploy(self):
        expected = "a" * 40
        router = ProductionSafetyBaseRouter.base(expected_sha=expected, prod_sha="b" * 40)
        router.add_remote(contains("stat -c %s"), FakeResult(0, "1024"))
        router.add_remote(contains("mkdir -p"), FakeResult(0))
        router.add_remote(contains("cp -p"), FakeResult(1, "", "No space left on device"))
        r = lsp.Reporter()
        with mock.patch.object(ld, "run_remote", router.run_remote):
            rc = lsp._deploy(r, self._mk_ctx(), expected)
        self.assertEqual(rc, 1)
        out = r.render()
        self.assertIn("production DB backup failed", out)
        self.assertNotIn("git pull", " ".join(router.remote_calls))
        self.assertNotIn("systemctl restart", " ".join(router.remote_calls))

    def test_health_check_failure_after_restart_reports_failure(self):
        expected = "a" * 40
        router = ProductionSafetyBaseRouter.base(expected_sha=expected, prod_sha="b" * 40)
        router.add_remote(contains("git fetch origin"), FakeResult(0))
        router.add_remote(contains("git checkout"), FakeResult(0))
        router.add_remote(contains("git pull"), FakeResult(0))
        # First rev-parse HEAD call (pre-deploy check) sees the stale sha;
        # the second (post-pull verification) sees the newly-pulled sha —
        # a real `git pull` would change this between the two calls.
        call_count = {"n": 0}

        def rev_parse_result():
            call_count["n"] += 1
            sha = "b" * 40 if call_count["n"] == 1 else expected
            return FakeResult(0, f"{sha}\n")

        router.remote_rules.insert(0, (contains("git rev-parse HEAD"), rev_parse_result))
        router.add_remote(contains("stat -c %s"), FakeResult(0, "1024"))
        router.add_remote(contains("mkdir -p"), FakeResult(0))
        router.add_remote(contains("cp -p"), FakeResult(0))
        router.add_remote(contains("sha256sum"), FakeResult(0, "abc123  x\n"))
        router.add_remote(contains("compileall"), FakeResult(0, ""))
        router.add_remote(contains("systemctl restart"), FakeResult(0))
        router.add_remote(contains("systemctl is-active"), FakeResult(0, "active\n"))
        router.add_remote(contains("127.0.0.1:8000"), FakeResult(0, "500"))
        r = lsp.Reporter()
        with mock.patch.object(ld, "run_remote", router.run_remote):
            rc = lsp._deploy(r, self._mk_ctx(), expected)
        self.assertEqual(rc, 1)
        self.assertIn("production loopback smoke check failed", r.render())

    def test_rerun_when_production_already_at_target_sha_is_safe(self):
        """Re-running ship after a successful deploy must not re-backup,
        re-pull, or restart — only verify and report."""
        expected = "a" * 40
        router = ProductionSafetyBaseRouter.base(expected_sha=expected, prod_sha=expected)
        router.add_remote(contains("compileall"), FakeResult(0, ""))
        router.add_remote(contains("systemctl is-active"), FakeResult(0, "active\n"))
        router.add_remote(contains("127.0.0.1:8000"), FakeResult(0, "200"))
        with mock.patch.object(ld, "run_remote", router.run_remote), \
             mock.patch.object(ld, "run_smoke_tests", return_value=[("/", 200, True, "")]):
            r = lsp.Reporter()
            rc = lsp._deploy(r, self._mk_ctx(), expected)
        self.assertEqual(rc, 0)
        out = r.render()
        self.assertIn("already at target SHA", out)
        joined = " ".join(router.remote_calls)
        self.assertNotIn("cp -p", joined)
        self.assertNotIn("git pull", joined)
        self.assertNotIn("systemctl restart", joined)


if __name__ == "__main__":
    unittest.main()
