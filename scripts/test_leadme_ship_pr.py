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

    def _router_ahead_zero(self, branch="feature/x", pulls_response='[]'):
        router = self._router_on_branch(branch)
        router.add_local(contains("fetch", "origin"), FakeResult(0))
        router.add_local(contains("rev-list", "--count"), FakeResult(0, "0\n"))
        router.add_local(contains("diff", "--check"), FakeResult(0))
        router.add_local(contains("auth", "status"), FakeResult(0, "Logged in to github.com as tester\n"))
        router.add_local(
            contains("remote", "get-url", "origin"),
            FakeResult(0, "https://github.com/strollan/seo-app.git\n"),
        )
        router.add_local(contains("api", "repos/strollan/seo-app/pulls"), FakeResult(0, pulls_response))
        router.add_local(contains("rev-parse", "origin/main"), FakeResult(0, "c" * 40 + "\n"))
        return router

    def test_ahead_zero_with_merged_pr_is_recognized_as_already_shipped(self):
        """The real-world scenario this repairs: a branch whose commits are
        all already merged into origin/main (ahead=0) must be recognized
        as already shipped, not treated as 'you forgot to commit'."""
        router = self._router_ahead_zero(
            pulls_response='[{"number": 11, "html_url": "https://github.com/strollan/seo-app/pull/11", '
                            '"state": "closed", "merged_at": "2026-08-09T18:32:44Z"}]',
        )
        # Already-deleted remote branch, as a real post-merge rerun would see.
        router.add_local(
            contains("push", "origin", "--delete"),
            FakeResult(1, "", "error: unable to delete 'feature/x': remote ref does not exist"),
        )
        with mock.patch.object(ld, "run_local", router.run_local), \
             mock.patch.object(lsp.shutil, "which", side_effect=lambda cmd: f"/usr/bin/{cmd}"), \
             mock.patch.object(lsp.Path, "is_dir", return_value=True), \
             mock.patch("pathlib.Path.exists", return_value=True):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lsp.cmd_ship(ns(no_deploy=True))
        out = buf.getvalue()

        self.assertEqual(rc, 0, out)
        self.assertIn("already shipped", out)
        self.assertIn("already merged", out)
        self.assertIn("LEADMELEADS SHIP COMPLETE", out)
        self.assertNotIn("has no commits ahead", out)
        self.assertNotIn("commit your work", out)

        # Push and PR-create must never have been attempted.
        joined = [" ".join(c) for c in router.local_calls]
        self.assertFalse(any("push" in c and "-u" in c and "origin" in c for c in joined),
                          "must not push when already merged")
        self.assertFalse(any("pulls" in c and "-X" not in c and "POST" in c for c in joined),
                          "must not create a duplicate PR when already merged")
        # No second lookup call either -- the probe's result is reused.
        pulls_get_calls = [c for c in joined if "api" in c and "repos/strollan/seo-app/pulls" in c and "merge" not in c]
        self.assertEqual(len(pulls_get_calls), 1, f"expected exactly one PR lookup, got: {pulls_get_calls}")

    def test_ahead_zero_without_any_pr_still_fails_safely(self):
        """A branch that is genuinely stale/empty (ahead=0, no PR ever
        existed for it) must still fail with the original message -- this
        is the case the original check protects against and must not be
        weakened."""
        router = self._router_ahead_zero(pulls_response="[]")
        with mock.patch.object(ld, "run_local", router.run_local), \
             mock.patch.object(lsp.shutil, "which", side_effect=lambda cmd: f"/usr/bin/{cmd}"), \
             mock.patch("pathlib.Path.exists", return_value=True):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lsp.cmd_ship(ns())
        out = buf.getvalue()
        self.assertEqual(rc, 1)
        self.assertIn("has no commits ahead of origin/main", out)
        self.assertNotIn("LEADMELEADS SHIP COMPLETE", out)

    def test_ahead_zero_with_open_unmerged_pr_still_fails_safely(self):
        """ahead=0 with only an OPEN (never-merged) PR for the branch must
        not be mistaken for 'already shipped' -- only a MERGED PR counts."""
        router = self._router_ahead_zero(
            pulls_response='[{"number": 12, "html_url": "https://github.com/strollan/seo-app/pull/12", '
                            '"state": "open", "merged_at": null}]',
        )
        with mock.patch.object(ld, "run_local", router.run_local), \
             mock.patch.object(lsp.shutil, "which", side_effect=lambda cmd: f"/usr/bin/{cmd}"), \
             mock.patch("pathlib.Path.exists", return_value=True):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lsp.cmd_ship(ns())
        out = buf.getvalue()
        self.assertEqual(rc, 1)
        self.assertIn("has no commits ahead of origin/main", out)

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
            contains("api", "repos/strollan/seo-app/pulls"),
            FakeResult(0, '[{"number": 7, "html_url": "https://github.com/strollan/seo-app/pull/7", "state": "open", "merged_at": null}]'),
        )
        with mock.patch.object(ld, "run_local", router.run_local):
            pr, res = lsp.find_pr_for_branch("gh", "feature/x", "strollan/seo-app")
        self.assertEqual(pr["number"], 7)
        self.assertEqual(pr["state"], "OPEN")

    def test_handles_already_merged_pr(self):
        router = FakeRouter()
        router.add_local(
            contains("api", "repos/strollan/seo-app/pulls"),
            FakeResult(0, '[{"number": 7, "html_url": "https://github.com/strollan/seo-app/pull/7", "state": "closed", "merged_at": "2026-08-09T00:00:00Z"}]'),
        )
        with mock.patch.object(ld, "run_local", router.run_local):
            pr, res = lsp.find_pr_for_branch("gh", "feature/x", "strollan/seo-app")
        self.assertEqual(pr["state"], "MERGED")

    def test_no_pr_found_returns_none(self):
        router = FakeRouter()
        router.add_local(contains("api", "repos/strollan/seo-app/pulls"), FakeResult(0, "[]"))
        with mock.patch.object(ld, "run_local", router.run_local):
            pr, res = lsp.find_pr_for_branch("gh", "feature/x", "strollan/seo-app")
        self.assertIsNone(pr)

    def test_merge_pr_treats_already_merged_error_as_success(self):
        router = FakeRouter()
        router.add_local(
            contains("api", "repos/strollan/seo-app/pulls/7/merge"),
            FakeResult(1, "", '{"message":"Pull Request is not mergeable"}'),
        )
        with mock.patch.object(ld, "run_local", router.run_local):
            ok, detail = lsp.merge_pr("gh", 7, "strollan/seo-app")
        self.assertTrue(ok)

    def test_merge_pr_real_failure_is_not_swallowed(self):
        router = FakeRouter()
        router.add_local(
            contains("api", "repos/strollan/seo-app/pulls/7/merge"),
            FakeResult(1, "", "required check has not passed"),
        )
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
# Regression: gh operations must be repository-explicit and independent of
# local git/worktree discovery.
#
# Real-world failure this locks in: with only Windows gh.exe available (no
# native `gh` on the WSL PATH) and REPO_PATH a Linux-created linked
# worktree under /tmp, `gh auth setup-git` / `gh pr list` / `gh pr create` /
# `gh pr merge` all failed with "fatal: not a git repository: .../.git/
# worktrees/wt-contact-form" -- those subcommands initialize a local git
# client internally even when --repo/--head/--base are given explicitly,
# and Windows git.exe cannot parse the POSIX `gitdir:` pointer files a
# Linux `git worktree add` writes. The fix: route every repository-scoped
# PR operation through `gh api` (pure REST, no local git at all) and drop
# `gh auth setup-git` from the critical path.
# ---------------------------------------------------------------------------

class GhRepositoryExplicitRegressionTests(unittest.TestCase):
    WORKTREE_GIT_ERROR = (
        "failed to run git: fatal: not a git repository: "
        "/mnt/c/Users/scott/ai-project/seo-app/.git/worktrees/wt-contact-form"
    )

    def _broken_worktree_router(self, branch="feature/contact-report-form"):
        """Simulates gh.exe invoked from a WSL /tmp linked worktree: any
        `gh pr <verb>` or `gh auth setup-git` call reproduces the real
        failure; only `gh api ...` calls succeed. If the implementation
        ever regresses to `gh pr list/create/merge` or re-adds `gh auth
        setup-git` to the critical path, that step fails exactly as it did
        in the real broken run and the pipeline reports SHIP FAILED."""
        router = FakeRouter()
        router.add_local(contains("branch", "--show-current"), FakeResult(0, f"{branch}\n"))
        router.add_local(contains("diff", "--check"), FakeResult(0))
        router.add_local(contains("diff", "--cached", "--quiet"), FakeResult(0))
        router.add_local(contains("diff", "--quiet"), FakeResult(0))
        router.add_local(contains("status", "--porcelain"), FakeResult(0, ""))
        router.add_local(contains("fetch", "origin"), FakeResult(0))
        # ahead=2 before the merge lands (preflight); ahead=0 afterwards
        # (post-merge verification) -- a real merge changes this between
        # the two rev-list calls, exactly like the actual `git fetch`
        # between them would.
        rev_list_calls = {"n": 0}

        def rev_list_result():
            rev_list_calls["n"] += 1
            return FakeResult(0, "2\n" if rev_list_calls["n"] == 1 else "0\n")

        router.add_local(contains("rev-list", "--count"), rev_list_result)
        router.add_local(contains("rev-parse", "origin/main"), FakeResult(0, "a" * 40 + "\n"))
        router.add_local(contains("auth", "status"), FakeResult(0, "Logged in to github.com as tester\n"))
        router.add_local(
            contains("remote", "get-url", "origin"),
            FakeResult(0, "https://github.com/strollan/seo-app.git\n"),
        )
        # Already-pushed branch: matches the real ship attempt this repairs
        # -- `git push` reports "Everything up-to-date", not a fresh push.
        router.add_local(contains("push", "-u", "origin"), FakeResult(1, "", "Everything up-to-date\n"))
        router.add_local(contains("push", "origin", "--delete"), FakeResult(0, "deleted"))

        # The old, broken invocation shapes: all fail exactly as observed.
        router.add_local(contains("auth", "setup-git"), FakeResult(1, "", self.WORKTREE_GIT_ERROR))
        router.add_local(contains("pr", "list"), FakeResult(1, "", self.WORKTREE_GIT_ERROR))
        router.add_local(contains("pr", "create"), FakeResult(1, "", self.WORKTREE_GIT_ERROR))
        router.add_local(contains("pr", "merge"), FakeResult(1, "", self.WORKTREE_GIT_ERROR))

        # The fixed shape: `gh api` never touches git, so it always
        # succeeds from this cwd. Merge rule registered first since its
        # path is a superstring of the list/create path.
        router.add_local(
            contains("api", "repos/strollan/seo-app/pulls/7/merge"),
            FakeResult(0, '{"merged": true}'),
        )
        router.add_local(
            contains("api", "repos/strollan/seo-app/pulls"),
            FakeResult(
                0,
                '[{"number": 7, "html_url": "https://github.com/strollan/seo-app/pull/7", '
                '"state": "open", "merged_at": null}]',
            ),
        )
        return router

    def test_ship_succeeds_from_windows_gh_exe_in_wsl_tmp_worktree_with_already_pushed_branch(self):
        """The exact real-world scenario: gh binary is Windows gh.exe,
        REPO_PATH is a WSL /tmp linked worktree, and the feature branch was
        already pushed in a prior run. Ship must reach PR-merged completion
        using only `gh api` calls -- reusing the already-pushed branch and
        the existing open PR (idempotent rerun), never a duplicate push or
        duplicate PR."""
        router = self._broken_worktree_router()
        win_gh = "/mnt/c/Program Files/GitHub CLI/gh.exe"
        tmp_worktree = Path("/tmp/claude-1000/-home-scot/fake-session/scratchpad/wt-contact-form")

        def which_no_native_gh(cmd):
            return None if cmd == "gh" else f"/usr/bin/{cmd}"

        with mock.patch.object(ld, "run_local", router.run_local), \
             mock.patch.object(lsp.shutil, "which", side_effect=which_no_native_gh), \
             mock.patch.object(lsp.Path, "is_dir", return_value=True), \
             mock.patch("pathlib.Path.exists", return_value=True), \
             mock.patch.object(lsp, "REPO_PATH", tmp_worktree):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lsp.cmd_ship(ns(no_deploy=True))
        out = buf.getvalue()

        self.assertEqual(rc, 0, out)
        self.assertIn("LEADMELEADS SHIP COMPLETE", out)
        self.assertIn("existing PR reused", out)

        gh_calls = [c for c in router.local_calls if c and c[0] == win_gh]
        self.assertTrue(gh_calls, "expected at least one gh.exe invocation")
        self.assertFalse(
            any(len(c) > 1 and c[1] == "pr" for c in gh_calls),
            "must not invoke `gh pr <verb>` from the Windows gh.exe binary",
        )
        self.assertTrue(
            any(len(c) > 1 and c[1] == "api" for c in gh_calls),
            "expected `gh api ...` calls instead of `gh pr ...`",
        )
        self.assertFalse(
            any(len(c) > 1 and c[1] == "auth" and "setup-git" in c for c in gh_calls),
            "`gh auth setup-git` must not be invoked",
        )

    def test_gh_auth_setup_git_is_never_invoked_on_the_happy_path(self):
        """Regression guard for requirement #5: `gh auth setup-git` must be
        fully removed from the critical path, not merely tolerated on
        failure."""
        router = self._broken_worktree_router()
        with mock.patch.object(ld, "run_local", router.run_local), \
             mock.patch.object(lsp.shutil, "which", side_effect=lambda cmd: f"/usr/bin/{cmd}"), \
             mock.patch.object(lsp.Path, "is_dir", return_value=True), \
             mock.patch("pathlib.Path.exists", return_value=True):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lsp.cmd_ship(ns(no_deploy=True))
        self.assertEqual(rc, 0)
        self.assertFalse(any("setup-git" in " ".join(c) for c in router.local_calls))

    def test_find_pr_for_branch_passes_explicit_repo_head_and_base(self):
        captured = {}

        def fake_run_local(args, cwd=None, timeout=None, input_text=None):
            captured["args"] = args
            return FakeResult(0, "[]")

        with mock.patch.object(ld, "run_local", fake_run_local):
            lsp.find_pr_for_branch("gh", "feature/x", "strollan/seo-app")

        args = captured["args"]
        self.assertEqual(args[0], "gh")
        self.assertIn("api", args)
        self.assertIn("repos/strollan/seo-app/pulls", args)
        self.assertIn("head=strollan:feature/x", args)
        self.assertIn("base=main", args)
        self.assertNotIn("list", args)  # never the git-repo-dependent `pr list` form

    def test_create_pr_passes_explicit_repo_head_and_base(self):
        captured = {}

        def fake_run_local(args, cwd=None, timeout=None, input_text=None):
            captured["args"] = args
            return FakeResult(0, '{"number": 8}')

        with mock.patch.object(ld, "run_local", fake_run_local):
            lsp.create_pr("gh", "feature/x", "strollan/seo-app", "Some title")

        args = captured["args"]
        self.assertIn("repos/strollan/seo-app/pulls", args)
        self.assertIn("head=feature/x", args)
        self.assertIn("base=main", args)
        self.assertNotIn("create", args)  # never the git-repo-dependent `pr create` form

    def test_merge_pr_uses_explicit_repo_and_pr_number_no_local_git(self):
        captured = {}

        def fake_run_local(args, cwd=None, timeout=None, input_text=None):
            captured["args"] = args
            return FakeResult(0, '{"merged": true}')

        with mock.patch.object(ld, "run_local", fake_run_local):
            ok, _ = lsp.merge_pr("gh", 42, "strollan/seo-app")

        self.assertTrue(ok)
        args = captured["args"]
        self.assertEqual(args[0], "gh")
        self.assertEqual(args[1], "api")  # never `gh pr merge ...`
        self.assertIn("repos/strollan/seo-app/pulls/42/merge", args)
        self.assertIn("merge_method=merge", args)

    def test_no_gh_pr_subcommand_anywhere_in_module(self):
        """Static guard: this module must never shell out to `gh pr
        list/create/merge` -- those subcommands are what triggered the
        local-git-discovery failure under WSL + Windows gh.exe. All
        repository-scoped PR operations must go through gh_api()."""
        src = Path(lsp.__file__).read_text(encoding="utf-8")
        self.assertNotIn('"pr", "list"', src)
        self.assertNotIn('"pr", "create"', src)
        self.assertNotIn('"pr", "merge"', src)
        self.assertNotIn('"auth", "setup-git"', src)


# ---------------------------------------------------------------------------
# Regression: a MERGED PR from a previous ship of this branch must never be
# reused once new commits have been added on top -- only an OPEN PR, or no
# PR at all, counts as "existing" once ahead > 0. And a ship must never
# report SHIP COMPLETE without confirming the exact current HEAD landed in
# origin/main, regardless of what the PR/merge steps claimed.
#
# Real-world failure this locks in: branch already merged once (PR #11),
# then a new commit was added (ahead=1). find_pr_for_branch()'s "fall back
# to the most recent MERGED PR" behavior -- added for the ahead=0 rerun
# case in fdadd73 -- was *also* reached from the normal ahead>0 path, so
# the ship reused PR #11, skipped creating/merging a PR for the new
# commit, deleted the remote branch, and reported SHIP COMPLETE even
# though origin/main never changed.
# ---------------------------------------------------------------------------

class StaleMergedPrRegressionTests(unittest.TestCase):
    def _router_ahead_one_with_stale_merged_pr(self, branch="feature/x"):
        router = FakeRouter()
        router.add_local(contains("branch", "--show-current"), FakeResult(0, f"{branch}\n"))
        router.add_local(contains("diff", "--quiet"), FakeResult(0))
        router.add_local(contains("diff", "--cached", "--quiet"), FakeResult(0))
        router.add_local(contains("status", "--porcelain"), FakeResult(0, ""))
        router.add_local(contains("diff", "--check"), FakeResult(0))
        router.add_local(contains("fetch", "origin"), FakeResult(0))
        router.add_local(contains("auth", "status"), FakeResult(0, "Logged in to github.com as tester\n"))
        router.add_local(
            contains("remote", "get-url", "origin"),
            FakeResult(0, "https://github.com/strollan/seo-app.git\n"),
        )
        router.add_local(contains("push", "-u", "origin"), FakeResult(0, "branch pushed"))
        router.add_local(contains("push", "origin", "--delete"), FakeResult(0, "deleted"))
        router.add_local(contains("rev-parse", "origin/main"), FakeResult(0, "d" * 40 + "\n"))

        # ahead=1 in preflight (one new commit past the old merge);
        # ahead=0 after the new PR is actually merged (post-merge check).
        rev_list_calls = {"n": 0}

        def rev_list_result():
            rev_list_calls["n"] += 1
            return FakeResult(0, "1\n" if rev_list_calls["n"] == 1 else "0\n")

        router.add_local(contains("rev-list", "--count"), rev_list_result)

        # First `pulls` GET (find_pr_for_branch): only the OLD merged PR
        # exists -- no open PR for the branch's new commit yet. Second GET
        # (after create_pr): the newly created OPEN PR is now visible.
        pulls_get_calls = {"n": 0}

        def pulls_get_result():
            pulls_get_calls["n"] += 1
            if pulls_get_calls["n"] == 1:
                return FakeResult(
                    0,
                    '[{"number": 11, "html_url": "https://github.com/strollan/seo-app/pull/11", '
                    '"state": "closed", "merged_at": "2026-08-09T18:32:44Z"}]',
                )
            return FakeResult(
                0,
                '[{"number": 20, "html_url": "https://github.com/strollan/seo-app/pull/20", '
                '"state": "open", "merged_at": null}]',
            )

        # Order matters: the merge endpoint's path is a superstring of the
        # bare pulls path, so it must be matched first.
        router.add_local(contains("api", "repos/strollan/seo-app/pulls/20/merge"), FakeResult(0, '{"merged": true}'))
        router.add_local(contains("api", "repos/strollan/seo-app/pulls", "POST"), FakeResult(0, '{"number": 20}'))
        router.add_local(contains("api", "repos/strollan/seo-app/pulls", "GET"), pulls_get_result)
        return router

    def test_stale_merged_pr_is_not_reused_new_pr_is_created_and_merged(self):
        router = self._router_ahead_one_with_stale_merged_pr()
        with mock.patch.object(ld, "run_local", router.run_local), \
             mock.patch.object(lsp.shutil, "which", side_effect=lambda cmd: f"/usr/bin/{cmd}"), \
             mock.patch.object(lsp.Path, "is_dir", return_value=True), \
             mock.patch("pathlib.Path.exists", return_value=True):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lsp.cmd_ship(ns(no_deploy=True))
        out = buf.getvalue()

        self.assertEqual(rc, 0, out)
        self.assertIn("LEADMELEADS SHIP COMPLETE", out)

        # The stale PR must be flagged and never treated as "reused".
        self.assertIn("treating as stale, opening a new PR", out)
        self.assertNotIn("existing PR reused", out)

        # A new PR must actually have been created and then merged.
        self.assertIn("[PASS] PR created", out)
        self.assertIn("pull/20", out)
        joined = [" ".join(c) for c in router.local_calls]
        self.assertTrue(
            any("pulls" in c and "POST" in c for c in joined),
            "expected a POST to create a new PR",
        )
        self.assertTrue(
            any("pulls/20/merge" in c for c in joined),
            "expected the new PR (#20), not the stale one (#11), to be merged",
        )
        self.assertFalse(
            any("pulls/11/merge" in c for c in joined),
            "must never attempt to merge the stale already-merged PR",
        )

        # Final verification step must have run and passed.
        self.assertIn("[PASS] current HEAD contained in origin/main", out)

    def test_no_ship_complete_without_confirming_head_is_in_origin_main(self):
        """Even if every push/PR/merge step reports PASS, SHIP COMPLETE
        must never print unless a fresh post-merge check confirms the
        current HEAD actually landed in origin/main."""
        router = self._router_ahead_one_with_stale_merged_pr()
        # Override: the post-merge rev-list check keeps reporting ahead=1
        # forever, simulating a merge that "succeeded" per gh but somehow
        # never actually incorporated the current HEAD into main. Inserted
        # at the front so it wins over the stateful rule already registered
        # by the fixture above.
        router.local_rules.insert(0, (contains("rev-list", "--count"), FakeResult(0, "1\n")))

        with mock.patch.object(ld, "run_local", router.run_local), \
             mock.patch.object(lsp.shutil, "which", side_effect=lambda cmd: f"/usr/bin/{cmd}"), \
             mock.patch.object(lsp.Path, "is_dir", return_value=True), \
             mock.patch("pathlib.Path.exists", return_value=True):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lsp.cmd_ship(ns(no_deploy=True))
        out = buf.getvalue()

        self.assertEqual(rc, 1, out)
        self.assertNotIn("LEADMELEADS SHIP COMPLETE", out)
        self.assertIn("current HEAD is not in origin/main", out)

    def test_ahead_zero_already_merged_behavior_from_fdadd73_is_preserved(self):
        """Sibling case must still work: ahead=0 (no new commits at all)
        with a genuinely covering MERGED PR is still recognized as already
        shipped -- this fix only changes the ahead>0 stale-PR path."""
        router = FakeRouter()
        router.add_local(contains("branch", "--show-current"), FakeResult(0, "feature/x\n"))
        router.add_local(contains("diff", "--quiet"), FakeResult(0))
        router.add_local(contains("diff", "--cached", "--quiet"), FakeResult(0))
        router.add_local(contains("status", "--porcelain"), FakeResult(0, ""))
        router.add_local(contains("diff", "--check"), FakeResult(0))
        router.add_local(contains("fetch", "origin"), FakeResult(0))
        router.add_local(contains("rev-list", "--count"), FakeResult(0, "0\n"))
        router.add_local(contains("rev-parse", "origin/main"), FakeResult(0, "c" * 40 + "\n"))
        router.add_local(contains("auth", "status"), FakeResult(0, "Logged in to github.com as tester\n"))
        router.add_local(
            contains("remote", "get-url", "origin"),
            FakeResult(0, "https://github.com/strollan/seo-app.git\n"),
        )
        router.add_local(
            contains("push", "origin", "--delete"),
            FakeResult(1, "", "error: unable to delete 'feature/x': remote ref does not exist"),
        )
        router.add_local(
            contains("api", "repos/strollan/seo-app/pulls"),
            FakeResult(
                0,
                '[{"number": 11, "html_url": "https://github.com/strollan/seo-app/pull/11", '
                '"state": "closed", "merged_at": "2026-08-09T18:32:44Z"}]',
            ),
        )
        with mock.patch.object(ld, "run_local", router.run_local), \
             mock.patch.object(lsp.shutil, "which", side_effect=lambda cmd: f"/usr/bin/{cmd}"), \
             mock.patch.object(lsp.Path, "is_dir", return_value=True), \
             mock.patch("pathlib.Path.exists", return_value=True):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lsp.cmd_ship(ns(no_deploy=True))
        out = buf.getvalue()
        self.assertEqual(rc, 0, out)
        self.assertIn("already shipped", out)
        self.assertIn("LEADMELEADS SHIP COMPLETE", out)


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



# ---------------------------------------------------------------------------
# Real-module contract test — no mocking of leadme_deploy's attribute
# surface. The suites above only ever exercise leadme_ship_pr with
# ld.run_local/ld.run_remote swapped for a FakeRouter; every ld.<name>
# reference is still resolved against the real, imported leadme_deploy
# module when that happens. But none of the earlier tests ever drove
# cmd_ship() far enough (past preflight + github auth) to actually execute
# the `ld.remote_origin_url(REPO_PATH)` line that shipped broken -- a
# module using a name leadme_deploy never defined would only surface as an
# AttributeError at call time, and nothing here called it. This test
# statically resolves every `ld.<name>` this module references against the
# real (unmocked) leadme_deploy module, so a future rename/removal on
# either side is caught without needing to hand-drive every code path.
# ---------------------------------------------------------------------------

import ast


class RealModuleContractTests(unittest.TestCase):
    def _ld_attribute_uses(self):
        source = Path(lsp.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=lsp.__file__)

        referenced = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "ld"
            ):
                referenced.add(node.attr)

        called = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "ld"
            ):
                called.add(node.func.attr)

        return referenced, called

    def test_every_ld_attribute_exists_on_the_real_leadme_deploy_module(self):
        """Imports the real leadme_deploy module (already done at file
        scope as `ld`, unmocked here) and confirms every `ld.<name>`
        leadme_ship_pr.py references actually resolves via getattr."""
        referenced, _called = self._ld_attribute_uses()
        self.assertTrue(referenced, "expected to find ld.<name> references in leadme_ship_pr.py")

        missing = sorted(name for name in referenced if not hasattr(ld, name))
        self.assertEqual(
            missing,
            [],
            "leadme_ship_pr.py references ld.<name> attributes that do not "
            f"exist on the real leadme_deploy module: {missing}",
        )

    def test_every_ld_function_call_target_is_callable_on_the_real_module(self):
        """Every `ld.<name>(...)` call site must resolve to something
        callable on the real module -- catches the case where a name
        exists but was repurposed into e.g. a constant."""
        _referenced, called = self._ld_attribute_uses()
        self.assertTrue(called, "expected to find ld.<name>(...) calls in leadme_ship_pr.py")

        not_callable = sorted(
            name for name in called if not callable(getattr(ld, name, None))
        )
        self.assertEqual(
            not_callable,
            [],
            "leadme_ship_pr.py calls ld.<name>(...) for attributes that "
            f"exist on the real module but are not callable: {not_callable}",
        )

    def test_remote_origin_url_regression_is_fixed(self):
        """Locks in the actual root cause and its fix: leadme_ship_pr.py
        must call the LOCAL git helper origin_remote_url() (git remote
        get-url origin, run in the repo working copy) — not a nonexistent
        SSH-to-production-style remote_origin_url(), which never existed
        in leadme_deploy.py and is not something that should exist there
        (all `remote_*()` names in that module are SSH-to-production
        helpers; resolving the local `origin` git remote's URL is not a
        production/SSH concern)."""
        self.assertTrue(hasattr(ld, "origin_remote_url"))
        self.assertFalse(hasattr(ld, "remote_origin_url"))

        src = Path(lsp.__file__).read_text(encoding="utf-8")
        self.assertIn("ld.origin_remote_url(", src)
        self.assertNotIn("ld.remote_origin_url(", src)


# ---------------------------------------------------------------------------
# Full pipeline dry run — drives cmd_ship() with --no-deploy through
# preflight, github auth, push, PR reuse, and merge, with every
# ld.run_local/ld.run_remote call intercepted by FakeRouter (the same
# hermetic seam every other test in this file uses -- nothing here touches
# a real git remote, gh, network, or SSH connection). --no-deploy is the
# tool's own documented dry-run flag (see build_parser): it returns before
# _deploy() ever runs, so no backup/pull/restart/production code executes
# either, real or faked.
#
# This is the test that would have caught the shipped AttributeError: the
# earlier PreflightTests all fail/return before the github-auth section,
# and DeployIdempotencyAndSafetyTests calls _deploy() directly, skipping
# cmd_ship()'s push/PR/merge phases entirely. This test is the first to
# drive cmd_ship() across the exact line that broke
# (ld.origin_remote_url(REPO_PATH), immediately after "gh auth setup-git").
# ---------------------------------------------------------------------------

class FullPipelineDryRunTests(unittest.TestCase):
    def _build_router(self, branch="feature/contact-report-form"):
        router = FakeRouter()
        router.add_local(contains("branch", "--show-current"), FakeResult(0, f"{branch}\n"))
        router.add_local(contains("diff", "--check"), FakeResult(0))
        router.add_local(contains("diff", "--cached", "--quiet"), FakeResult(0))
        router.add_local(contains("diff", "--quiet"), FakeResult(0))
        router.add_local(contains("status", "--porcelain"), FakeResult(0, ""))
        router.add_local(contains("fetch", "origin"), FakeResult(0))
        # ahead=2 before the merge lands (preflight); ahead=0 afterwards
        # (post-merge verification) -- a real merge changes this between
        # the two rev-list calls, exactly like the actual `git fetch`
        # between them would.
        rev_list_calls = {"n": 0}

        def rev_list_result():
            rev_list_calls["n"] += 1
            return FakeResult(0, "2\n" if rev_list_calls["n"] == 1 else "0\n")

        router.add_local(contains("rev-list", "--count"), rev_list_result)
        router.add_local(contains("rev-parse", "origin/main"), FakeResult(0, "a" * 40 + "\n"))
        router.add_local(contains("auth", "status"), FakeResult(0, "Logged in to github.com as tester\n"))
        router.add_local(
            contains("remote", "get-url", "origin"),
            FakeResult(0, "https://github.com/strollan/seo-app.git\n"),
        )
        router.add_local(contains("push", "-u", "origin"), FakeResult(0, "branch pushed"))
        router.add_local(contains("push", "origin", "--delete"), FakeResult(0, "deleted"))
        # Order matters: the merge endpoint's path is a superstring of the
        # list/create endpoint's path (".../pulls/7/merge" contains
        # ".../pulls"), and FakeRouter returns the first matching rule -- so
        # the more specific "/merge" matcher must be registered first.
        router.add_local(contains("api", "repos/strollan/seo-app/pulls/7/merge"), FakeResult(0, '{"merged": true}'))
        router.add_local(
            contains("api", "repos/strollan/seo-app/pulls"),
            FakeResult(
                0,
                '[{"number": 7, "html_url": "https://github.com/strollan/seo-app/pull/7", '
                '"state": "open", "merged_at": null}]',
            ),
        )
        return router

    def test_no_deploy_dry_run_completes_without_attributeerror_or_real_side_effects(self):
        router = self._build_router()
        with mock.patch.object(ld, "run_local", router.run_local), \
             mock.patch.object(lsp.shutil, "which", side_effect=lambda cmd: f"/usr/bin/{cmd}"), \
             mock.patch.object(lsp.Path, "is_dir", return_value=True), \
             mock.patch("pathlib.Path.exists", return_value=True):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lsp.cmd_ship(ns(no_deploy=True))

        out = buf.getvalue()

        # The whole point: this must not raise/report an AttributeError for
        # any ld.<name> reference, and must reach the documented dry-run
        # stopping point cleanly.
        self.assertEqual(rc, 0, out)
        self.assertNotIn("AttributeError", out)
        self.assertIn("origin remote resolves to owner/repo", out)
        self.assertIn("[PASS] origin remote resolves to owner/repo", out)
        self.assertIn("no-deploy given: PR merged, not deploying", out)
        self.assertIn("LEADMELEADS SHIP COMPLETE", out)

        # Proves the fixed call actually executed (not skipped/short-circuited).
        origin_url_calls = [c for c in router.local_calls if "remote" in c and "get-url" in c]
        self.assertEqual(len(origin_url_calls), 1)

        # --no-deploy must never reach production-safety/_deploy() code at
        # all, faked or otherwise -- zero SSH calls of any kind.
        self.assertEqual(router.remote_calls, [])

    def test_no_deploy_dry_run_reports_zero_attributeerror_when_run_via_main(self):
        """Same pipeline, entered through lsp.main() (the real ./scripts/
        leadme-ship.sh entry point) rather than calling cmd_ship directly,
        with --no-deploy passed on argv exactly as a caller would."""
        router = self._build_router()
        with mock.patch.object(ld, "run_local", router.run_local), \
             mock.patch.object(lsp.shutil, "which", side_effect=lambda cmd: f"/usr/bin/{cmd}"), \
             mock.patch.object(lsp.Path, "is_dir", return_value=True), \
             mock.patch("pathlib.Path.exists", return_value=True):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lsp.main(["--no-deploy"])

        self.assertEqual(rc, 0, buf.getvalue())
        self.assertEqual(router.remote_calls, [])


if __name__ == "__main__":
    unittest.main()
