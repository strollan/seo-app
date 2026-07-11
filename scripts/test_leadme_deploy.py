"""
Tests for scripts/leadme_deploy.py.

These tests never touch the real repo state, real network, or real SSH —
run_local()/run_remote() are monkeypatched with a small fake command router
so every command's stdout/stderr/returncode is fully controlled.

None of these tests deploy, push, commit, or restart anything.
"""

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import leadme_deploy as ld  # noqa: E402


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
        self.local_rules = []  # list of (matcher(args) -> bool, FakeResult)
        self.remote_rules = []  # list of (matcher(cmd_str) -> bool, FakeResult)
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
                return result
        return FakeResult(1, "", f"no fake rule for: {args}")

    def run_remote(self, remote_cmd, timeout=None):
        self.remote_calls.append(remote_cmd)
        for matcher, result in self.remote_rules:
            if matcher(remote_cmd):
                return result
        return FakeResult(1, "", f"no fake rule for remote cmd: {remote_cmd}")


def contains(*tokens):
    def matcher(argv_or_str):
        haystack = " ".join(argv_or_str) if isinstance(argv_or_str, list) else argv_or_str
        return all(tok in haystack for tok in tokens)
    return matcher


class UntrackedAllowlistTests(unittest.TestCase):
    def test_known_untracked_paths_are_allowed(self):
        self.assertTrue(ld.is_known_untracked(".claude/"))
        self.assertTrue(ld.is_known_untracked(".claude"))
        self.assertTrue(ld.is_known_untracked("reset_local_password.py"))
        self.assertTrue(ld.is_known_untracked("CLAUDE.md.backup-20260709-165036"))

    def test_unrelated_untracked_paths_are_not_allowed(self):
        self.assertFalse(ld.is_known_untracked("some_new_file.py"))
        self.assertFalse(ld.is_known_untracked("stray_dir/"))
        self.assertFalse(ld.is_known_untracked("CLAUDE.md"))  # not a .backup- file

    def test_classify_untracked_splits_known_and_unexpected(self):
        router = FakeRouter()
        porcelain = "\n".join([
            "?? .claude/",
            "?? CLAUDE.md.backup-20260709-165036",
            "?? reset_local_password.py",
            "?? surprise_file.txt",
        ])
        router.add_local(contains("status", "--porcelain"), FakeResult(0, porcelain, ""))
        with mock.patch.object(ld, "run_local", router.run_local):
            known, unexpected = ld.classify_untracked()
        self.assertEqual(
            sorted(known),
            sorted([".claude/", "CLAUDE.md.backup-20260709-165036", "reset_local_password.py"]),
        )
        self.assertEqual(unexpected, ["surprise_file.txt"])


class TrackedCleanTests(unittest.TestCase):
    def test_clean_tree_true_when_both_diffs_empty(self):
        router = FakeRouter()
        router.add_local(contains("diff", "--quiet"), FakeResult(0))
        router.add_local(contains("diff", "--cached", "--quiet"), FakeResult(0))
        with mock.patch.object(ld, "run_local", router.run_local):
            self.assertTrue(ld.tracked_tree_clean())

    def test_dirty_tree_blocks(self):
        router = FakeRouter()
        router.add_local(contains("diff", "--cached", "--quiet"), FakeResult(0))
        router.add_local(contains("diff", "--quiet"), FakeResult(1))
        with mock.patch.object(ld, "run_local", router.run_local):
            self.assertFalse(ld.tracked_tree_clean())

    def test_git_error_returns_none_not_a_silent_pass(self):
        router = FakeRouter()
        router.add_local(contains("diff", "--quiet"), FakeResult(128, "", "fatal: not a git repo"))
        router.add_local(contains("diff", "--cached", "--quiet"), FakeResult(128))
        with mock.patch.object(ld, "run_local", router.run_local):
            self.assertIsNone(ld.tracked_tree_clean())


class BranchBlockerTests(unittest.TestCase):
    def test_check_fails_when_not_on_main(self):
        router = FakeRouter()
        router.add_local(contains("branch", "--show-current"), FakeResult(0, "feature/x\n"))
        router.add_local(contains("diff", "--quiet"), FakeResult(0))
        router.add_local(contains("diff", "--cached", "--quiet"), FakeResult(0))
        router.add_local(contains("status", "--porcelain"), FakeResult(0, ""))
        router.add_local(contains("fetch", "origin"), FakeResult(0))
        router.add_local(contains("rev-list", "--left-right"), FakeResult(0, "0\t0"))
        router.add_local(contains("py_compile"), FakeResult(0))

        buf = io.StringIO()
        with mock.patch.object(ld, "run_local", router.run_local), redirect_stdout(buf):
            rc = ld.cmd_check(argparse_namespace())
        self.assertEqual(rc, 1)
        self.assertIn("CHECK FAIL", buf.getvalue())
        self.assertIn("branch is main", buf.getvalue())


class DivergenceTests(unittest.TestCase):
    def test_in_sync(self):
        router = FakeRouter()
        router.add_local(contains("rev-list", "--left-right"), FakeResult(0, "0\t0"))
        with mock.patch.object(ld, "run_local", router.run_local):
            state, ahead, behind = ld.divergence_state()
        self.assertEqual((state, ahead, behind), ("in-sync", 0, 0))

    def test_ahead(self):
        router = FakeRouter()
        router.add_local(contains("rev-list", "--left-right"), FakeResult(0, "0\t3"))
        with mock.patch.object(ld, "run_local", router.run_local):
            state, ahead, behind = ld.divergence_state()
        self.assertEqual((state, ahead, behind), ("ahead", 3, 0))

    def test_behind(self):
        router = FakeRouter()
        router.add_local(contains("rev-list", "--left-right"), FakeResult(0, "2\t0"))
        with mock.patch.object(ld, "run_local", router.run_local):
            state, ahead, behind = ld.divergence_state()
        self.assertEqual((state, ahead, behind), ("behind", 0, 2))

    def test_diverged_fails_closed(self):
        router = FakeRouter()
        router.add_local(contains("rev-list", "--left-right"), FakeResult(0, "2\t3"))
        with mock.patch.object(ld, "run_local", router.run_local):
            state, ahead, behind = ld.divergence_state()
        self.assertEqual(state, "diverged")

    def test_unknown_on_git_error(self):
        router = FakeRouter()
        router.add_local(contains("rev-list", "--left-right"), FakeResult(128, "", "fatal"))
        with mock.patch.object(ld, "run_local", router.run_local):
            state, ahead, behind = ld.divergence_state()
        self.assertEqual(state, "unknown")


class SmokeAcceptanceTests(unittest.TestCase):
    def test_status_within_acceptable_set_passes(self):
        def fake_curl(url, timeout=ld.CURL_TIMEOUT):
            if "reset-password" in url:
                return 303, ""
            if url.endswith("/lead-bot"):
                return 303, ""
            return 200, ""

        def fake_curl_and_location(url, timeout=ld.CURL_TIMEOUT):
            return 303, "/login", ""

        with mock.patch.object(ld, "curl_status_code", fake_curl), \
             mock.patch.object(ld, "curl_status_and_location", fake_curl_and_location):
            results = ld.run_smoke_tests()
        for path, status, ok, detail in results:
            self.assertTrue(ok, f"{path} unexpectedly failed with status {status}: {detail}")

    def test_unexpected_status_fails(self):
        with mock.patch.object(ld, "curl_status_code", lambda url, timeout=ld.CURL_TIMEOUT: (500, "")), \
             mock.patch.object(ld, "curl_status_and_location", lambda url, timeout=ld.CURL_TIMEOUT: (500, None, "")):
            results = ld.run_smoke_tests()
        self.assertTrue(all(not ok for _, _, ok, _ in results))

    def test_unreachable_counts_as_failure_not_silent_pass(self):
        with mock.patch.object(ld, "curl_status_code", lambda url, timeout=ld.CURL_TIMEOUT: (None, "timeout")), \
             mock.patch.object(ld, "curl_status_and_location", lambda url, timeout=ld.CURL_TIMEOUT: (None, None, "timeout")):
            results = ld.run_smoke_tests()
        self.assertTrue(all(not ok for _, _, ok, _ in results))

    def test_history_redirect_to_wrong_location_fails(self):
        def fake_curl_and_location(url, timeout=ld.CURL_TIMEOUT):
            return 303, "/some-other-page", ""

        with mock.patch.object(ld, "curl_status_and_location", fake_curl_and_location):
            results = ld.run_smoke_tests(routes=[("/history", {302, 303})])
        [(path, status, ok, detail)] = results
        self.assertFalse(ok)
        self.assertIn("Location", detail)

    def test_history_redirect_to_suffix_matching_path_fails(self):
        """A Location like /foo/login must not be accepted just because it ends in /login."""

        def fake_curl_and_location(url, timeout=ld.CURL_TIMEOUT):
            return 303, "/foo/login", ""

        with mock.patch.object(ld, "curl_status_and_location", fake_curl_and_location):
            results = ld.run_smoke_tests(routes=[("/history", {302, 303})])
        [(path, status, ok, detail)] = results
        self.assertFalse(ok)
        self.assertIn("Location", detail)

    def test_history_redirect_to_exact_login_path_passes(self):
        def fake_curl_and_location(url, timeout=ld.CURL_TIMEOUT):
            return 303, "/login", ""

        with mock.patch.object(ld, "curl_status_and_location", fake_curl_and_location):
            results = ld.run_smoke_tests(routes=[("/history", {302, 303})])
        [(path, status, ok, detail)] = results
        self.assertTrue(ok, detail)

    def test_history_200_still_fails(self):
        with mock.patch.object(ld, "curl_status_and_location", lambda url, timeout=ld.CURL_TIMEOUT: (200, None, "")):
            results = ld.run_smoke_tests(routes=[("/history", {302, 303})])
        [(path, status, ok, detail)] = results
        self.assertFalse(ok)


class DoctorCheckDryRunTests(unittest.TestCase):
    def _base_router(self):
        router = FakeRouter()
        router.add_local(contains("--version"), FakeResult(0, "git version 2.40.0"))
        router.add_local(contains("branch", "--show-current"), FakeResult(0, "main\n"))
        router.add_local(contains("diff", "--quiet"), FakeResult(0))
        router.add_local(contains("diff", "--cached", "--quiet"), FakeResult(0))
        router.add_local(contains("status", "--porcelain"), FakeResult(0, ""))
        router.add_local(contains("fetch", "origin"), FakeResult(0))
        router.add_local(contains("rev-list", "--left-right"), FakeResult(0, "0\t0"))
        router.add_local(contains("rev-parse", "origin/main"), FakeResult(0, "abc1234\n"))
        router.add_local(contains("rev-parse", "HEAD"), FakeResult(0, "abc1234\n"))
        router.add_local(contains("py_compile"), FakeResult(0))
        router.add_remote(contains("hostname"), FakeResult(0, "prod-host\n"))
        router.add_remote(contains("test -d"), FakeResult(0, "EXISTS\n"))
        router.add_remote(contains("is-inside-work-tree"), FakeResult(0, "true\n"))
        router.add_remote(contains("rev-parse HEAD"), FakeResult(0, "abc1234\n"))
        router.add_remote(contains("venv/bin/python"), FakeResult(0, "EXISTS\n"))
        router.add_remote(contains("systemctl show"), FakeResult(0, "LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n"))
        return router

    def test_doctor_all_green(self):
        router = self._base_router()
        with mock.patch.object(ld, "run_local", router.run_local), \
             mock.patch.object(ld, "run_remote", router.run_remote), \
             mock.patch.object(ld, "curl_status_code", lambda *a, **k: (200, "")), \
             mock.patch("pathlib.Path.exists", return_value=True):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = ld.cmd_doctor(argparse_namespace())
        self.assertEqual(rc, 0)
        self.assertIn("DOCTOR PASS", buf.getvalue())

    def test_check_passes_when_everything_clean(self):
        router = self._base_router()
        with mock.patch.object(ld, "run_local", router.run_local):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = ld.cmd_check(argparse_namespace())
        self.assertEqual(rc, 0)
        self.assertIn("CHECK PASS", buf.getvalue())

    def test_dry_run_never_calls_mutating_commands(self):
        router = self._base_router()
        with mock.patch.object(ld, "run_local", router.run_local):
            buf = io.StringIO()
            with redirect_stdout(buf):
                ld.cmd_dry_run(argparse_namespace())
        joined_calls = [" ".join(c) if isinstance(c, list) else c for c in router.local_calls]
        for call in joined_calls:
            self.assertNotIn("push", call)
            self.assertNotIn("pull", call)
            self.assertNotIn("reset --hard", call)
        self.assertIn("DRY RUN", buf.getvalue())
        self.assertIn("already in sync with origin/main", buf.getvalue())

    def test_dry_run_shows_push_plan_when_ahead(self):
        router = self._base_router()
        # Prepend an "ahead" divergence rule so it matches before the
        # in-sync default rule added by _base_router().
        router.local_rules.insert(
            0, (contains("rev-list", "--left-right"), FakeResult(0, "0\t2"))
        )
        with mock.patch.object(ld, "run_local", router.run_local):
            buf = io.StringIO()
            with redirect_stdout(buf):
                ld.cmd_dry_run(argparse_namespace())
        out = buf.getvalue()
        self.assertIn("would run: git push origin main", out)
        # Still must never actually call push.
        joined_calls = [" ".join(c) if isinstance(c, list) else c for c in router.local_calls]
        self.assertTrue(all("push" not in call for call in joined_calls))

class StatusModeTests(unittest.TestCase):
    def test_status_reports_without_mutating_and_handles_ssh_down(self):
        router = FakeRouter()
        router.add_local(contains("branch", "--show-current"), FakeResult(0, "main\n"))
        router.add_local(contains("rev-parse", "HEAD"), FakeResult(0, "deadbeef\n"))
        router.add_local(contains("fetch", "origin"), FakeResult(0))
        router.add_local(contains("rev-parse", "origin/main"), FakeResult(0, "deadbeef\n"))
        router.add_local(contains("rev-list", "--left-right"), FakeResult(0, "0\t0"))
        router.add_remote(contains("hostname"), FakeResult(255, "", "ssh: connect timed out"))

        with mock.patch.object(ld, "run_local", router.run_local), \
             mock.patch.object(ld, "run_remote", router.run_remote), \
             mock.patch.object(ld, "curl_status_code", lambda *a, **k: (200, "")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = ld.cmd_status(argparse_namespace())
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("ssh unreachable", out)
        self.assertNotIn("push", " ".join(str(c) for c in router.local_calls))


class CommandFailureReportingTests(unittest.TestCase):
    def test_ssh_failure_is_reported_not_silently_swallowed(self):
        router = FakeRouter()
        router.add_remote(contains("hostname"), FakeResult(255, "", "Permission denied (publickey)"))
        with mock.patch.object(ld, "run_remote", router.run_remote):
            hostname, res = ld.remote_hostname()
        self.assertIsNone(hostname)
        self.assertIn("Permission denied", res.stderr)

    def test_compile_failure_detail_surfaced(self):
        router = FakeRouter()
        router.add_local(contains("py_compile"), FakeResult(1, "", "SyntaxError: invalid syntax"))
        with mock.patch.object(ld, "run_local", router.run_local):
            results = ld.compile_targets("python3", targets=["app/main.py"])
        target, ok, detail = results[0]
        self.assertFalse(ok)
        self.assertIn("SyntaxError", detail)


class StateFileTests(unittest.TestCase):
    def test_write_and_read_roundtrip_with_temp_dir(self, tmp_override=None):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            override = Path(tmp) / "leadme-deploy-state"
            path = ld.write_state(
                {
                    "last_deploy_timestamp": "2026-07-09T00:00:00+00:00",
                    "previous_production_commit": "aaa1111",
                    "deployed_commit": "bbb2222",
                    "backup_path": "/var/www/leadmeleads-backups/app_auth-20260709.db",
                    "result": "PASS",
                },
                override=override,
            )
            self.assertTrue(path.exists())
            data = ld.read_state(override=override)
            self.assertEqual(data["deployed_commit"], "bbb2222")
            self.assertEqual(data["result"], "PASS")

    def test_read_missing_state_returns_empty_dict(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            override = Path(tmp) / "does-not-exist-yet"
            self.assertEqual(ld.read_state(override=override), {})

    def test_write_state_refuses_secret_shaped_keys(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            override = Path(tmp) / "state"
            with self.assertRaises(ValueError):
                ld.write_state({"github_token": "ghp_xxx"}, override=override)

    def test_write_state_refuses_secret_shaped_values(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            override = Path(tmp) / "state"
            with self.assertRaises(ValueError):
                ld.write_state({"result": "password=hunter2"}, override=override)


class NoSecretsPrintedTests(unittest.TestCase):
    def test_doctor_output_never_contains_ssh_key_material(self):
        router = FakeRouter()
        router.add_local(contains("--version"), FakeResult(0, "git version 2.40.0"))
        router.add_local(contains("branch", "--show-current"), FakeResult(0, "main\n"))
        router.add_local(contains("diff", "--quiet"), FakeResult(0))
        router.add_local(contains("diff", "--cached", "--quiet"), FakeResult(0))
        router.add_local(contains("status", "--porcelain"), FakeResult(0, ""))
        router.add_remote(contains("hostname"), FakeResult(0, "prod-host\n"))
        router.add_remote(contains("test -d"), FakeResult(0, "EXISTS\n"))
        router.add_remote(contains("is-inside-work-tree"), FakeResult(0, "true\n"))
        router.add_remote(contains("rev-parse HEAD"), FakeResult(0, "abc1234\n"))
        router.add_remote(contains("venv/bin/python"), FakeResult(0, "EXISTS\n"))
        router.add_remote(contains("systemctl show"), FakeResult(0, "LoadState=loaded\nActiveState=active\n"))

        with mock.patch.object(ld, "run_local", router.run_local), \
             mock.patch.object(ld, "run_remote", router.run_remote), \
             mock.patch.object(ld, "curl_status_code", lambda *a, **k: (200, "")), \
             mock.patch("pathlib.Path.exists", return_value=True):
            buf = io.StringIO()
            with redirect_stdout(buf):
                ld.cmd_doctor(argparse_namespace())
        out = buf.getvalue()
        self.assertNotIn("PRIVATE KEY", out)
        self.assertNotIn("BEGIN OPENSSH", out)

    def test_state_file_never_stores_env_or_password_shaped_data(self):
        # SAFE_STATE_KEYS is the sole write surface for state — assert it
        # contains nothing secret-shaped by construction.
        for key in ld.SAFE_STATE_KEYS:
            self.assertIsNone(ld.SECRET_LIKE_KEY_PATTERN.search(key))


class ArgumentParsingTests(unittest.TestCase):
    def test_default_mode_has_no_flags(self):
        parser = ld.build_parser()
        args = parser.parse_args([])
        self.assertFalse(any([args.check, args.status, args.smoke, args.doctor, args.dry_run, args.rollback_info]))

    def test_flags_are_mutually_exclusive(self):
        parser = ld.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["--check", "--status"])

    def test_each_mode_flag_parses(self):
        parser = ld.build_parser()
        for flag, attr in [
            ("--check", "check"),
            ("--status", "status"),
            ("--smoke", "smoke"),
            ("--doctor", "doctor"),
            ("--dry-run", "dry_run"),
            ("--rollback-info", "rollback_info"),
        ]:
            args = parser.parse_args([flag])
            self.assertTrue(getattr(args, attr))


def argparse_namespace():
    import argparse
    return argparse.Namespace()


class FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class GitBinaryRoutingTests(unittest.TestCase):
    """run_local() must route git.exe vs. Linux git purely from cwd/args —
    no PATH shims, env vars, or other runtime overrides required. These
    tests mock subprocess.run() itself (not run_local) so the real
    _select_git_binary() logic inside run_local runs and is verified."""

    def setUp(self):
        ld._git_exe_available.cache_clear()

    def _mock_subprocess(self, returncode=0, stdout="", stderr=""):
        patcher = mock.patch.object(
            ld.subprocess, "run",
            return_value=FakeCompletedProcess(returncode, stdout, stderr),
        )
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def test_mnt_c_repo_uses_git_exe_when_available(self):
        run_mock = self._mock_subprocess()
        with mock.patch.object(ld, "_git_exe_available", return_value=True):
            ld.run_local(["git", "status", "--porcelain"], cwd="/mnt/c/Users/scott/ai-project/seo-app")
        called_args = run_mock.call_args.args[0]
        self.assertEqual(called_args[0], "git.exe")
        self.assertEqual(called_args[1:], ["status", "--porcelain"])

    def test_mnt_c_repo_falls_back_to_linux_git_when_git_exe_missing(self):
        run_mock = self._mock_subprocess()
        with mock.patch.object(ld, "_git_exe_available", return_value=False):
            ld.run_local(["git", "status", "--porcelain"], cwd="/mnt/c/Users/scott/ai-project/seo-app")
        called_args = run_mock.call_args.args[0]
        self.assertEqual(called_args[0], "git")

    def test_home_worktree_uses_linux_git_even_when_git_exe_available(self):
        run_mock = self._mock_subprocess()
        with mock.patch.object(ld, "_git_exe_available", return_value=True):
            ld.run_local(
                ["git", "diff", "HEAD", "--stat"],
                cwd="/home/scot/.local/share/leadme-collab/worktrees/20260711-000000-example",
            )
        called_args = run_mock.call_args.args[0]
        self.assertEqual(called_args[0], "git")

    def test_worktree_add_targeting_home_path_forces_linux_git_despite_mnt_c_cwd(self):
        """`git worktree add <path>` runs with cwd on the /mnt/c primary
        repo but creates the new worktree under /home/scot — git.exe
        cannot see native Linux ext4 paths at all, so this must never be
        routed to git.exe even though cwd alone would qualify."""
        run_mock = self._mock_subprocess()
        home_worktree = "/home/scot/.local/share/leadme-collab/worktrees/20260711-000000-example"
        with mock.patch.object(ld, "_git_exe_available", return_value=True):
            ld.run_local(
                ["git", "worktree", "add", "-q", home_worktree, "-b", "collab/example", "deadbeef"],
                cwd="/mnt/c/Users/scott/ai-project/seo-app",
            )
        called_args = run_mock.call_args.args[0]
        self.assertEqual(called_args[0], "git")
        self.assertEqual(called_args[1:], ["worktree", "add", "-q", home_worktree, "-b", "collab/example", "deadbeef"])

    def test_non_git_commands_are_never_rewritten(self):
        run_mock = self._mock_subprocess()
        with mock.patch.object(ld, "_git_exe_available", return_value=True):
            ld.run_local(["curl", "--version"], cwd="/mnt/c/Users/scott/ai-project/seo-app")
        called_args = run_mock.call_args.args[0]
        self.assertEqual(called_args, ["curl", "--version"])

    def test_cwd_forwarded_unchanged_to_subprocess(self):
        run_mock = self._mock_subprocess()
        with mock.patch.object(ld, "_git_exe_available", return_value=True):
            ld.run_local(["git", "rev-parse", "HEAD"], cwd="/mnt/c/Users/scott/ai-project/seo-app")
        self.assertEqual(run_mock.call_args.kwargs["cwd"], "/mnt/c/Users/scott/ai-project/seo-app")

    def test_no_cwd_defaults_to_linux_git(self):
        run_mock = self._mock_subprocess()
        with mock.patch.object(ld, "_git_exe_available", return_value=True):
            ld.run_local(["git", "--version"])
        called_args = run_mock.call_args.args[0]
        self.assertEqual(called_args[0], "git")

    def test_command_failure_still_surfaces_as_failed_result_not_swallowed(self):
        """Fail-closed regression: a real git.exe failure (non-zero exit)
        must still come back as a failed CmdResult, not get masked by the
        binary-selection rewrite."""
        self._mock_subprocess(returncode=128, stdout="", stderr="fatal: not a git repository")
        with mock.patch.object(ld, "_git_exe_available", return_value=True):
            result = ld.run_local(["git", "status"], cwd="/mnt/c/Users/scott/ai-project/seo-app")
        self.assertFalse(result.ok)
        self.assertEqual(result.returncode, 128)
        self.assertIn("not a git repository", result.stderr)

    def test_missing_binary_fails_closed_with_127(self):
        with mock.patch.object(ld.subprocess, "run", side_effect=FileNotFoundError("git.exe")):
            with mock.patch.object(ld, "_git_exe_available", return_value=True):
                result = ld.run_local(["git", "status"], cwd="/mnt/c/Users/scott/ai-project/seo-app")
        self.assertFalse(result.ok)
        self.assertEqual(result.returncode, 127)

    def test_timeout_fails_closed_with_124(self):
        with mock.patch.object(
            ld.subprocess, "run",
            side_effect=ld.subprocess.TimeoutExpired(cmd=["git", "status"], timeout=5),
        ):
            with mock.patch.object(ld, "_git_exe_available", return_value=True):
                result = ld.run_local(["git", "status"], cwd="/mnt/c/Users/scott/ai-project/seo-app", timeout=5)
        self.assertFalse(result.ok)
        self.assertEqual(result.returncode, 124)


if __name__ == "__main__":
    unittest.main()
