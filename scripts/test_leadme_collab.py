"""
Tests for scripts/leadme_collab.py.

Real (throwaway) git repos in tmp dirs are used for worktree/branch/safety-ref
tests — git itself is fast, safe, and deterministic against disposable dirs,
so mocking it out would just hide bugs. The Claude CLI is always mocked
(run_claude / leadme_deploy.run_local) so these tests never spend real API
budget or touch the network.

None of these tests push, merge, commit-and-push, or deploy anything, and
none touch the real primary repo.
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
import leadme_collab as lc  # noqa: E402


def make_temp_git_repo(tmp_path, name="primary"):
    repo = tmp_path / name
    repo.mkdir()
    for args in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
    ):
        res = ld.run_local(args, cwd=repo, timeout=15)
        assert res.ok, res.stderr
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    ld.run_local(["git", "add", "README.md"], cwd=repo, timeout=15)
    res = ld.run_local(["git", "commit", "-m", "init"], cwd=repo, timeout=15)
    assert res.ok, res.stderr
    return repo


class IsolatedDirsMixin:
    """Points STATE_ROOT/WORKTREE_ROOT/CURRENT_TASK_POINTER/REPO_PATH at a
    temp dir for the duration of a test."""

    def isolate(self, tmp_path, repo_path=None):
        patches = [
            mock.patch.object(lc, "STATE_ROOT", tmp_path / "state"),
            mock.patch.object(lc, "WORKTREE_ROOT", tmp_path / "worktrees"),
            mock.patch.object(lc, "CURRENT_TASK_POINTER", tmp_path / "state" / "current_task"),
        ]
        if repo_path is not None:
            patches.append(mock.patch.object(lc, "REPO_PATH", repo_path))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class TaskIdAndPathTests(unittest.TestCase):
    def test_slugify_produces_safe_lowercase_tokens(self):
        self.assertEqual(lc.slugify("Fix Mobile Report Layout!!"), "fix-mobile-report-layout")

    def test_new_task_id_has_timestamp_and_slug(self):
        tid = lc.new_task_id("Fix mobile report layout")
        self.assertRegex(tid, r"^\d{8}-\d{6}-fix-mobile-report-layout$")

    def test_worktree_path_generation(self):
        with mock.patch.object(lc, "WORKTREE_ROOT", Path("/tmp/wt-root")):
            path = lc.worktree_dir("20260709-000000-example")
        self.assertEqual(path, Path("/tmp/wt-root/20260709-000000-example"))

    def test_branch_and_safety_ref_naming(self):
        tid = "20260709-000000-example"
        self.assertEqual(lc.branch_name(tid), "collab/20260709-000000-example")
        self.assertEqual(lc.safety_ref_name(tid), "refs/heads/backup/collab-20260709-000000-example")


class StateFileTests(unittest.TestCase, IsolatedDirsMixin):
    def test_write_and_read_state_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.isolate(Path(tmp))
            lc.write_task_state("t1", {"task_id": "t1", "phase": "isolated"})
            state = lc.read_task_state("t1")
            self.assertEqual(state["phase"], "isolated")

    def test_read_missing_state_returns_none(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.isolate(Path(tmp))
            self.assertIsNone(lc.read_task_state("does-not-exist"))

    def test_write_state_refuses_secret_shaped_values(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.isolate(Path(tmp))
            with self.assertRaises(ValueError):
                lc.write_task_state("t1", {"note": "password=hunter2"})

    def test_current_task_pointer_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.isolate(Path(tmp))
            lc.set_current_task("t1")
            self.assertEqual(lc.get_current_task(), "t1")

    def test_list_task_ids_only_counts_dirs_with_state_json(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.isolate(Path(tmp))
            lc.write_task_state("t1", {"task_id": "t1"})
            (lc.STATE_ROOT / "not-a-task").mkdir(parents=True)
            self.assertEqual(lc.list_task_ids(), ["t1"])


class DiscoveryTests(unittest.TestCase):
    def test_discover_codex_reports_unavailable_when_not_on_path(self):
        with mock.patch("shutil.which", return_value=None):
            info = lc.discover_codex()
        self.assertFalse(info["available"])
        self.assertFalse(info["usable_noninteractive"])

    def test_choose_reviewer_mode_falls_back_to_file_handoff(self):
        with mock.patch("shutil.which", return_value=None):
            mode, info = lc.choose_reviewer_mode()
        self.assertEqual(mode, "file-handoff")

    def test_codex_review_stub_raises_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            lc.codex_review_stub()

    def test_discover_claude_reports_available_when_present(self):
        with mock.patch("shutil.which", return_value="/usr/bin/claude"), \
             mock.patch.object(ld, "run_local") as fake_run:
            fake_run.side_effect = [
                ld.CmdResult(0, "2.1.0", ""),
                ld.CmdResult(0, "-p, --print   Print response and exit", ""),
            ]
            info = lc.discover_claude()
        self.assertTrue(info["available"])
        self.assertTrue(info["print_mode"])


class ClaudeAdapterCommandTests(unittest.TestCase):
    def test_run_claude_builds_expected_argv(self):
        captured = {}

        def fake_run_local(args, cwd=None, timeout=None, input_text=None):
            captured["args"] = args
            captured["cwd"] = cwd
            captured["timeout"] = timeout
            return ld.CmdResult(0, json.dumps({"result": "ok", "is_error": False, "total_cost_usd": 0.01}), "")

        with mock.patch.object(ld, "run_local", fake_run_local):
            result = lc.run_claude("do the thing", cwd="/some/worktree", timeout=42)

        self.assertEqual(captured["args"][0], "claude")
        self.assertIn("-p", captured["args"])
        self.assertIn("do the thing", captured["args"])
        self.assertIn("--output-format", captured["args"])
        self.assertIn("json", captured["args"])
        self.assertIn("--permission-mode", captured["args"])
        self.assertIn("acceptEdits", captured["args"])
        self.assertIn("--disallowedTools", captured["args"])
        self.assertEqual(captured["cwd"], "/some/worktree")
        self.assertEqual(captured["timeout"], 42)
        self.assertFalse(result["is_error"])
        self.assertEqual(result["result_text"], "ok")

    def test_disallowed_tools_blocks_push_commit_merge_sudo_installs(self):
        for forbidden in ("git push", "git commit", "git merge", "sudo", "pip install", "npm install", "apt-get"):
            self.assertIn(forbidden, lc.DISALLOWED_TOOLS)

    def test_run_claude_surfaces_timeout_as_error_not_a_hang(self):
        def fake_run_local(args, cwd=None, timeout=None, input_text=None):
            return ld.CmdResult(124, "", f"TIMEOUT after {timeout}s")

        with mock.patch.object(ld, "run_local", fake_run_local):
            result = lc.run_claude("slow task", cwd="/x", timeout=5)
        self.assertEqual(result["returncode"], 124)
        self.assertTrue(result["is_error"])

    def test_run_claude_makes_exactly_one_subprocess_call(self):
        calls = []

        def fake_run_local(args, cwd=None, timeout=None, input_text=None):
            calls.append(args)
            return ld.CmdResult(0, json.dumps({"result": "ok", "is_error": False}), "")

        with mock.patch.object(ld, "run_local", fake_run_local):
            lc.run_claude("task", cwd="/x")
        self.assertEqual(len(calls), 1)


class VerdictParsingTests(unittest.TestCase):
    def test_parses_pass(self):
        self.assertEqual(lc.parse_verdict("VERDICT: PASS\nlooks good"), "PASS")

    def test_parses_needs_fix_case_insensitive(self):
        self.assertEqual(lc.parse_verdict("verdict: needs fix\nmissing null check"), "NEEDS FIX")

    def test_returns_none_when_unparseable(self):
        self.assertIsNone(lc.parse_verdict("looks fine to me, ship it"))


class PrimaryRepoGitTests(unittest.TestCase, IsolatedDirsMixin):
    def test_worktree_and_safety_ref_created_from_exact_base_commit(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_temp_git_repo(tmp_path)
            self.isolate(tmp_path, repo_path=repo)

            base_sha = ld.head_sha(repo)
            tid = "20260709-000000-test-task"

            ok, ref, err = lc.create_safety_ref(base_sha, tid)
            self.assertTrue(ok, err)

            ok, wt_path, branch, err = lc.create_worktree(base_sha, tid)
            self.assertTrue(ok, err)
            self.assertTrue(wt_path.exists())

            self.assertEqual(lc.worktree_head(wt_path), base_sha)

            # Safety ref points at the exact base commit.
            ref_res = ld.run_local(["git", "rev-parse", ref], cwd=repo, timeout=15)
            self.assertEqual(ref_res.stdout.strip(), base_sha)

    def test_primary_repo_untouched_by_worktree_creation(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_temp_git_repo(tmp_path)
            self.isolate(tmp_path, repo_path=repo)

            before = lc.primary_repo_snapshot()
            base_sha = ld.head_sha(repo)
            lc.create_safety_ref(base_sha, "20260709-000000-x")
            lc.create_worktree(base_sha, "20260709-000000-x")

            unchanged, b, a = lc.primary_repo_unchanged(before)
            self.assertTrue(unchanged, f"before={b} after={a}")

    def test_diff_capture_against_real_worktree_change(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_temp_git_repo(tmp_path)
            self.isolate(tmp_path, repo_path=repo)

            base_sha = ld.head_sha(repo)
            tid = "20260709-000000-diffcheck"
            lc.create_safety_ref(base_sha, tid)
            ok, wt_path, branch, err = lc.create_worktree(base_sha, tid)
            self.assertTrue(ok, err)

            (wt_path / "README.md").write_text("hello\nworld\n", encoding="utf-8")

            info = lc.capture_diff(wt_path)
            self.assertIn("README.md", info["changed_files"])
            self.assertIn("world", info["patch"])


class StartBlockerTests(unittest.TestCase, IsolatedDirsMixin):
    def _args(self, task="do something", task_file=None):
        import argparse
        return argparse.Namespace(task=task, task_file=task_file)

    def test_missing_repo_blocks_start(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self.isolate(tmp_path, repo_path=tmp_path / "does-not-exist")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lc.cmd_start(self._args())
        self.assertEqual(rc, 1)
        self.assertIn("primary repo exists", buf.getvalue())

    def test_wrong_branch_blocks_start(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_temp_git_repo(tmp_path)
            ld.run_local(["git", "checkout", "-b", "feature/x"], cwd=repo, timeout=15)
            self.isolate(tmp_path, repo_path=repo)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lc.cmd_start(self._args())
        self.assertEqual(rc, 1)
        self.assertIn("primary repo on main", buf.getvalue())

    def test_dirty_tree_blocks_start(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_temp_git_repo(tmp_path)
            (repo / "README.md").write_text("dirty change\n", encoding="utf-8")
            self.isolate(tmp_path, repo_path=repo)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lc.cmd_start(self._args())
        self.assertEqual(rc, 1)
        self.assertIn("primary tracked tree clean", buf.getvalue())

    def test_diverged_main_blocks_start(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_temp_git_repo(tmp_path)
            self.isolate(tmp_path, repo_path=repo)
            with mock.patch.object(ld, "fetch_origin", return_value=(True, "")), \
                 mock.patch.object(ld, "divergence_state", return_value=("diverged", 2, 3)):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = lc.cmd_start(self._args())
        self.assertEqual(rc, 1)
        self.assertIn("diverged", buf.getvalue())

    def test_empty_task_description_blocks_start(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_temp_git_repo(tmp_path)
            self.isolate(tmp_path, repo_path=repo)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lc.cmd_start(self._args(task="   "))
        self.assertEqual(rc, 1)


class ReviewLoopTests(unittest.TestCase, IsolatedDirsMixin):
    def _bootstrap_task(self, tmp_path):
        repo = make_temp_git_repo(tmp_path)
        self.isolate(tmp_path, repo_path=repo)
        base_sha = ld.head_sha(repo)
        tid = "20260709-000000-loop-test"
        lc.create_safety_ref(base_sha, tid)
        ok, wt_path, branch, err = lc.create_worktree(base_sha, tid)
        self.assertTrue(ok, err)
        lc.write_artifact(tid, "task.md", "# Task\n\nsome task\n")
        state = {
            "task_id": tid,
            "task_description": "some task",
            "repo": str(repo),
            "base_branch": "main",
            "base_commit": base_sha,
            "safety_ref": lc.safety_ref_name(tid),
            "worktree": str(wt_path),
            "branch": branch,
            "phase": "implemented",
            "cycle": 1,
            "max_review_cycles": 3,
            "reviewer_mode": "file-handoff",
            "created_at": lc.utc_now_iso(),
            "updated_at": lc.utc_now_iso(),
            "final": None,
        }
        lc.write_task_state(tid, state)
        lc.write_artifact(tid, "verification.md", "no changed files")
        return tid, wt_path

    def test_pass_verdict_stops_the_loop(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid, wt_path = self._bootstrap_task(tmp_path)

            r = lc.Reporter()
            result = lc._advance_task(tid, r)  # writes review-prompt.md, pauses
            self.assertEqual(result, "paused_review")

            (lc.task_dir(tid) / "review.md").write_text("VERDICT: PASS\nlooks good\n", encoding="utf-8")

            r2 = lc.Reporter()
            result2 = lc._advance_task(tid, r2)
            self.assertEqual(result2, "done")
            state = lc.read_task_state(tid)
            self.assertEqual(state["phase"], "done")
            self.assertEqual(state["final"], "READY FOR HUMAN REVIEW")
            self.assertTrue((lc.task_dir(tid) / "summary.md").exists())

    def test_needs_fix_triggers_repair_and_advances_cycle(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid, wt_path = self._bootstrap_task(tmp_path)

            lc._advance_task(tid, lc.Reporter())  # -> awaiting_review
            (lc.task_dir(tid) / "review.md").write_text("VERDICT: NEEDS FIX\nfix the thing\n", encoding="utf-8")

            def fake_run_claude(prompt, cwd, timeout=lc.CLAUDE_TIMEOUT_SECONDS):
                return {
                    "returncode": 0, "stdout": "", "stderr": "", "elapsed_seconds": 1.0,
                    "result_text": "fixed", "is_error": False, "total_cost_usd": 0.01, "parsed": {},
                }

            with mock.patch.object(lc, "discover_claude", return_value={"available": True, "path": "/x", "version": "1", "print_mode": True}), \
                 mock.patch.object(lc, "run_claude", fake_run_claude):
                r = lc.Reporter()
                result = lc._advance_task(tid, r)

            self.assertEqual(result, "paused_review")
            state = lc.read_task_state(tid)
            self.assertEqual(state["cycle"], 2)
            self.assertEqual(state["phase"], "awaiting_review")
            self.assertTrue((lc.task_dir(tid) / "repair-output.md").exists())

    def test_max_cycles_reaches_needs_human_without_further_repair(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid, wt_path = self._bootstrap_task(tmp_path)
            state = lc.read_task_state(tid)
            state["cycle"] = 3
            state["max_review_cycles"] = 3
            state["phase"] = "awaiting_review"
            lc.write_task_state(tid, state)
            (lc.task_dir(tid) / "review.md").write_text("VERDICT: NEEDS FIX\nstill broken\n", encoding="utf-8")

            claude_calls = []

            def fake_run_claude(prompt, cwd, timeout=lc.CLAUDE_TIMEOUT_SECONDS):
                claude_calls.append(prompt)
                return {"returncode": 0, "stdout": "", "stderr": "", "elapsed_seconds": 1.0,
                        "result_text": "x", "is_error": False, "total_cost_usd": 0.0, "parsed": {}}

            with mock.patch.object(lc, "run_claude", fake_run_claude):
                r = lc.Reporter()
                result = lc._advance_task(tid, r)

            self.assertEqual(result, "needs_human")
            self.assertEqual(claude_calls, [])  # no further repair attempted past max cycles
            state = lc.read_task_state(tid)
            self.assertEqual(state["final"], "NEEDS HUMAN")

    def test_fail_verdict_maps_to_failed_final(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid, wt_path = self._bootstrap_task(tmp_path)
            lc._advance_task(tid, lc.Reporter())
            (lc.task_dir(tid) / "review.md").write_text("VERDICT: FAIL\nunsalvageable\n", encoding="utf-8")
            result = lc._advance_task(tid, lc.Reporter())
            self.assertEqual(result, "needs_human")
            self.assertEqual(lc.read_task_state(tid)["final"], "FAILED")

    def test_unparseable_review_fails_closed_to_needs_human(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid, wt_path = self._bootstrap_task(tmp_path)
            lc._advance_task(tid, lc.Reporter())
            (lc.task_dir(tid) / "review.md").write_text("looks okay I guess\n", encoding="utf-8")
            result = lc._advance_task(tid, lc.Reporter())
            self.assertEqual(result, "needs_human")


class AbortTests(unittest.TestCase, IsolatedDirsMixin):
    def _args(self, task_id=None, cleanup=False):
        import argparse
        return argparse.Namespace(task_id=task_id, cleanup=cleanup)

    def test_abort_without_cleanup_preserves_worktree_and_state(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_temp_git_repo(tmp_path)
            self.isolate(tmp_path, repo_path=repo)
            base_sha = ld.head_sha(repo)
            tid = "20260709-000000-abort-test"
            lc.create_safety_ref(base_sha, tid)
            ok, wt_path, branch, err = lc.create_worktree(base_sha, tid)
            self.assertTrue(ok, err)
            lc.write_task_state(tid, {
                "task_id": tid, "phase": "isolated", "worktree": str(wt_path),
                "branch": branch, "safety_ref": lc.safety_ref_name(tid), "final": None,
            })

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lc.cmd_abort(self._args(task_id=tid, cleanup=False))

            self.assertEqual(rc, 0)
            self.assertTrue(wt_path.exists())  # preserved
            self.assertTrue(lc.task_dir(tid).exists())  # preserved
            self.assertEqual(lc.read_task_state(tid)["phase"], "aborted")

    def test_abort_with_cleanup_removes_worktree_but_keeps_state_and_safety_ref(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_temp_git_repo(tmp_path)
            self.isolate(tmp_path, repo_path=repo)
            base_sha = ld.head_sha(repo)
            tid = "20260709-000000-abort-cleanup"
            ok_ref, ref, _ = lc.create_safety_ref(base_sha, tid)
            ok, wt_path, branch, err = lc.create_worktree(base_sha, tid)
            self.assertTrue(ok, err)
            lc.write_task_state(tid, {
                "task_id": tid, "phase": "isolated", "worktree": str(wt_path),
                "branch": branch, "safety_ref": ref, "final": None,
            })

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lc.cmd_abort(self._args(task_id=tid, cleanup=True))

            self.assertEqual(rc, 0)
            self.assertFalse(wt_path.exists())
            self.assertTrue(lc.task_dir(tid).exists())  # state preserved

            ref_res = ld.run_local(["git", "rev-parse", ref], cwd=repo, timeout=15)
            self.assertTrue(ref_res.ok)  # safety ref preserved


class NoAutoGitFinalizationTests(unittest.TestCase):
    """Static checks that the module never contains a code path that would
    push, merge to main, commit on the user's behalf, or deploy."""

    def setUp(self):
        self.source = Path(lc.__file__).read_text(encoding="utf-8")

    def test_source_has_no_push_invocation(self):
        self.assertNotIn('"push"', self.source)

    def test_source_has_no_merge_invocation(self):
        self.assertNotIn('"merge"', self.source)

    def test_source_has_no_git_commit_invocation(self):
        # "git commit" only appears inside prompt text telling Claude NOT to
        # commit, and inside DISALLOWED_TOOLS blocking it — never as an
        # argv element the orchestrator itself would run.
        self.assertNotIn('["git", "commit"', self.source)

    def test_source_has_no_deploy_invocation(self):
        self.assertNotIn("systemctl", self.source)
        self.assertNotIn("leadme_deploy.cmd_deploy", self.source)


class SummaryGenerationTests(unittest.TestCase, IsolatedDirsMixin):
    def test_summary_contains_required_sections(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self.isolate(tmp_path)
            tid = "20260709-000000-summary-test"
            lc.write_task_state(tid, {
                "task_id": tid,
                "task_description": "Fix mobile report layout",
                "base_commit": "abc1234",
                "worktree": str(tmp_path / "nonexistent-worktree"),
                "branch": "collab/20260709-000000-summary-test",
                "final": "READY FOR HUMAN REVIEW",
            })
            summary = lc._build_summary(tid)
        for expected in ("TASK", "BASE", "WORKTREE", "BRANCH", "FINAL",
                          "READY FOR HUMAN REVIEW", "Commits: none", "Push: none", "Deploy: none"):
            self.assertIn(expected, summary)


class ArgumentParsingTests(unittest.TestCase):
    def test_requires_a_subcommand(self):
        parser = lc.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([])

    def test_each_subcommand_parses(self):
        parser = lc.build_parser()
        self.assertEqual(parser.parse_args(["doctor"]).command, "doctor")
        self.assertEqual(parser.parse_args(["start", "do the thing"]).command, "start")
        self.assertEqual(parser.parse_args(["status"]).command, "status")
        self.assertEqual(parser.parse_args(["resume"]).command, "resume")
        self.assertEqual(parser.parse_args(["inspect"]).command, "inspect")
        self.assertEqual(parser.parse_args(["abort"]).command, "abort")
        self.assertEqual(parser.parse_args(["list"]).command, "list")

    def test_start_accepts_task_file_flag(self):
        parser = lc.build_parser()
        args = parser.parse_args(["start", "--task-file", "task.md"])
        self.assertEqual(args.task_file, "task.md")

    def test_abort_accepts_cleanup_flag(self):
        parser = lc.build_parser()
        args = parser.parse_args(["abort", "some-id", "--cleanup"])
        self.assertTrue(args.cleanup)
        self.assertEqual(args.task_id, "some-id")


if __name__ == "__main__":
    unittest.main()
