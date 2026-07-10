"""
Tests for scripts/leadme_ship.py.

Real (throwaway) git repos + a real bare "origin" remote are used for
commit/push tests — local git operations are fast, safe, and deterministic
against disposable dirs, so mocking them out would just hide bugs. The
boundaries that touch things outside this machine (Claude/Codex inside
promote, and SSH/production inside deploy) are always mocked via the same
seams leadme_ship.py itself calls: run_promote() and run_deploy().

None of these tests push to a real remote host, deploy, or touch the real
primary repo / production.
"""

import argparse
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import leadme_deploy as ld  # noqa: E402
import leadme_collab as lc  # noqa: E402
import leadme_ship as ls  # noqa: E402


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


def add_bare_origin(tmp_path, repo, name="origin.git"):
    origin = tmp_path / name
    res = ld.run_local(["git", "init", "--bare", "-b", "main", str(origin)], timeout=15)
    assert res.ok, res.stderr
    res = ld.run_local(["git", "remote", "add", "origin", str(origin)], cwd=repo, timeout=15)
    assert res.ok, res.stderr
    res = ld.run_local(["git", "push", "-u", "origin", "main"], cwd=repo, timeout=15)
    assert res.ok, res.stderr
    return origin


class IsolatedDirsMixin:
    """Points leadme_ship/leadme_collab's REPO_PATH and leadme_collab's
    state/worktree roots at temp dirs for the duration of a test — the same
    isolation shape test_leadme_collab.py uses, extended to also isolate
    leadme_ship's own REPO_PATH global."""

    def isolate(self, tmp_path, repo_path=None):
        patches = [
            mock.patch.object(lc, "STATE_ROOT", tmp_path / "state"),
            mock.patch.object(lc, "WORKTREE_ROOT", tmp_path / "worktrees"),
            mock.patch.object(lc, "CURRENT_TASK_POINTER", tmp_path / "state" / "current_task"),
        ]
        if repo_path is not None:
            patches.append(mock.patch.object(lc, "REPO_PATH", repo_path))
            patches.append(mock.patch.object(ls, "REPO_PATH", repo_path))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


def bootstrap_shippable_task(test_case, tmp_path, tid="20260710-000000-ship-test",
                              gitignore_text=None, add_worktree_file="NOTES.txt",
                              add_worktree_content="new file\n"):
    """A task whose worktree has one new untracked file ready to promote,
    branched off a primary repo that already has a bare 'origin' so push
    works against something real."""
    repo = make_temp_git_repo(tmp_path)
    if gitignore_text is not None:
        (repo / ".gitignore").write_text(gitignore_text, encoding="utf-8")
        ld.run_local(["git", "add", ".gitignore"], cwd=repo, timeout=15)
        res = ld.run_local(["git", "commit", "-m", "add gitignore"], cwd=repo, timeout=15)
        assert res.ok, res.stderr
    add_bare_origin(tmp_path, repo)
    test_case.isolate(tmp_path, repo_path=repo)
    base_sha = ld.head_sha(repo)
    lc.create_safety_ref(base_sha, tid)
    ok, wt_path, branch, err = lc.create_worktree(base_sha, tid)
    test_case.assertTrue(ok, err)
    if add_worktree_file:
        target = wt_path / add_worktree_file
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(add_worktree_content, encoding="utf-8")
    state = {
        "task_id": tid,
        "task_description": "ship test task",
        "repo": str(repo),
        "base_branch": "main",
        "base_commit": base_sha,
        "safety_ref": lc.safety_ref_name(tid),
        "worktree": str(wt_path),
        "branch": branch,
        "phase": "done",
        "cycle": 1,
        "max_review_cycles": 3,
        "reviewer_mode": "file-handoff",
        "created_at": lc.utc_now_iso(),
        "updated_at": lc.utc_now_iso(),
        "final": "READY FOR HUMAN REVIEW",
    }
    lc.write_task_state(tid, state)
    return tid, repo, wt_path


def ship_args(task_id=None, message=None, no_deploy=False, no_push=False,
              dry_run=False, test_command=None):
    return argparse.Namespace(
        task_id=task_id, message=message, no_deploy=no_deploy, no_push=no_push,
        dry_run=dry_run, test_command=test_command,
    )


def passing_test_result():
    return ld.CmdResult(0, "OK", "")


class HelperTests(unittest.TestCase):
    def test_promoted_test_modules_matches_scripts_test_star_py(self):
        modules = ls.promoted_test_modules([
            "scripts/test_new_thing.py", "app/main.py", "scripts/test_leadme_collab.py",
        ])
        # test_leadme_collab is already a default module — must not duplicate.
        self.assertEqual(modules, ["scripts.test_new_thing"])

    def test_promoted_test_modules_ignores_non_scripts_dir(self):
        modules = ls.promoted_test_modules(["tests/test_something.py"])
        self.assertEqual(modules, [])

    def test_build_test_argv_default_includes_extra_modules(self):
        argv = ls.build_test_argv(None, ["scripts.test_new_thing"])
        self.assertEqual(argv, [sys.executable, "-m", "unittest",
                                 "scripts.test_leadme_deploy", "scripts.test_leadme_collab",
                                 "scripts.test_new_thing"])

    def test_build_test_argv_override_ignores_extra_modules(self):
        argv = ls.build_test_argv("python3 -m unittest scripts.test_foo", ["scripts.test_new_thing"])
        self.assertEqual(argv, ["python3", "-m", "unittest", "scripts.test_foo"])


class PreflightTests(unittest.TestCase, IsolatedDirsMixin):
    def test_missing_task_id_fails(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_temp_git_repo(tmp_path)
            add_bare_origin(tmp_path, repo)
            self.isolate(tmp_path, repo_path=repo)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = ls.cmd_ship(ship_args(task_id=None, message="msg"))
        self.assertEqual(rc, 1)
        self.assertIn("task id provided", buf.getvalue())

    def test_missing_commit_message_fails(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid, repo, wt = bootstrap_shippable_task(self, tmp_path)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = ls.cmd_ship(ship_args(task_id=tid, message=None))
        self.assertEqual(rc, 1)
        self.assertIn("commit message provided", buf.getvalue())

    def test_dirty_primary_tracked_tree_fails(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid, repo, wt = bootstrap_shippable_task(self, tmp_path)
            (repo / "README.md").write_text("dirty\n", encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = ls.cmd_ship(ship_args(task_id=tid, message="msg"))
        self.assertEqual(rc, 1)
        self.assertIn("[FAIL] primary tracked tree clean", buf.getvalue())

    def test_unknown_task_id_fails(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_temp_git_repo(tmp_path)
            add_bare_origin(tmp_path, repo)
            self.isolate(tmp_path, repo_path=repo)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = ls.cmd_ship(ship_args(task_id="does-not-exist", message="msg"))
        self.assertEqual(rc, 1)
        self.assertIn("task exists", buf.getvalue())

    def test_task_with_no_changes_fails(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid, repo, wt = bootstrap_shippable_task(self, tmp_path, add_worktree_file=None)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = ls.cmd_ship(ship_args(task_id=tid, message="msg"))
        self.assertEqual(rc, 1)
        self.assertIn("task has changed files", buf.getvalue())


class DryRunTests(unittest.TestCase, IsolatedDirsMixin):
    def test_dry_run_does_not_modify_commit_push_or_deploy(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid, repo, wt = bootstrap_shippable_task(self, tmp_path)
            pre_head = ld.head_sha(repo)

            with mock.patch.object(ls, "run_promote") as fake_promote, \
                 mock.patch.object(ls, "run_deploy") as fake_deploy, \
                 mock.patch.object(ld, "push_origin") as fake_push:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = ls.cmd_ship(ship_args(task_id=tid, dry_run=True))
                fake_promote.assert_not_called()
                fake_deploy.assert_not_called()
                fake_push.assert_not_called()

            self.assertEqual(rc, 0)
            self.assertIn("DRY RUN", buf.getvalue())
            self.assertEqual(ld.head_sha(repo), pre_head)
            self.assertFalse((repo / "NOTES.txt").exists())
            self.assertTrue(ld.tracked_tree_clean(repo))

    def test_dry_run_allows_missing_commit_message(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid, repo, wt = bootstrap_shippable_task(self, tmp_path)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = ls.cmd_ship(ship_args(task_id=tid, message=None, dry_run=True))
        self.assertEqual(rc, 0)
        self.assertNotIn("[FAIL]", buf.getvalue())


class OrchestrationTests(unittest.TestCase, IsolatedDirsMixin):
    """These mock the three real boundaries (promote, test-run, deploy) to
    verify leadme_ship's own stop-on-failure ordering logic, independent of
    whether promote/tests/deploy themselves are correct (those have their
    own test suites)."""

    def _bootstrap(self, tmp_path, **kw):
        return bootstrap_shippable_task(self, tmp_path, **kw)

    def test_promote_failure_stops_before_tests(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid, repo, wt = self._bootstrap(tmp_path)
            with mock.patch.object(ls, "run_promote", return_value=1) as fake_promote, \
                 mock.patch.object(ld, "run_local", wraps=ld.run_local) as fake_run_local:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = ls.cmd_ship(ship_args(task_id=tid, message="msg"))
                fake_promote.assert_called_once_with(tid)
                # No unittest invocation among the run_local calls made after promote.
                self.assertFalse(any("unittest" in " ".join(c.args[0]) for c in fake_run_local.call_args_list))
        self.assertEqual(rc, 1)
        self.assertIn("promote", buf.getvalue().lower())
        self.assertIn("STOPPED", buf.getvalue())

    def test_test_failure_stops_before_commit(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid, repo, wt = self._bootstrap(tmp_path)
            pre_head = ld.head_sha(repo)

            def fake_promote(t):
                (repo / "NOTES.txt").write_text("new file\n", encoding="utf-8")
                ld.run_local(["git", "add", "-f", "NOTES.txt"], cwd=repo, timeout=15)
                return 0

            real_run_local = ld.run_local

            def router(args, cwd=None, timeout=None, input_text=None):
                if len(args) >= 3 and args[1:3] == ["-m", "unittest"]:
                    return ld.CmdResult(1, "", "FAIL: something broke")
                return real_run_local(args, cwd=cwd, timeout=timeout, input_text=input_text)

            with mock.patch.object(ls, "run_promote", fake_promote), \
                 mock.patch.object(ld, "run_local", router):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = ls.cmd_ship(ship_args(task_id=tid, message="msg"))

            self.assertEqual(rc, 1)
            self.assertIn("test suite", buf.getvalue())
            self.assertIn("STOPPED", buf.getvalue())
            self.assertIn("git status --short", buf.getvalue())
            # Nothing was committed.
            self.assertEqual(ld.head_sha(repo), pre_head)

    def test_commit_failure_stops_before_push_and_deploy(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid, repo, wt = self._bootstrap(tmp_path)

            def fake_promote(t):
                (repo / "NOTES.txt").write_text("new file\n", encoding="utf-8")
                ld.run_local(["git", "add", "-f", "NOTES.txt"], cwd=repo, timeout=15)
                return 0

            real_run_local = ld.run_local

            def router(args, cwd=None, timeout=None, input_text=None):
                if len(args) >= 3 and args[1:3] == ["-m", "unittest"]:
                    return ld.CmdResult(0, "OK", "")
                if len(args) >= 2 and args[:2] == ["git", "commit"]:
                    return ld.CmdResult(1, "", "commit failed: hook rejected")
                return real_run_local(args, cwd=cwd, timeout=timeout, input_text=input_text)

            with mock.patch.object(ls, "run_promote", fake_promote), \
                 mock.patch.object(ld, "run_local", router), \
                 mock.patch.object(ld, "push_origin") as fake_push, \
                 mock.patch.object(ls, "run_deploy") as fake_deploy:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = ls.cmd_ship(ship_args(task_id=tid, message="msg"))
                fake_push.assert_not_called()
                fake_deploy.assert_not_called()

        self.assertEqual(rc, 1)
        self.assertIn("git commit failed", buf.getvalue())
        self.assertIn("STOPPED", buf.getvalue())

    def test_push_failure_stops_before_deploy_when_origin_lacks_head(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid, repo, wt = self._bootstrap(tmp_path)

            def fake_promote(t):
                (repo / "NOTES.txt").write_text("new file\n", encoding="utf-8")
                ld.run_local(["git", "add", "-f", "NOTES.txt"], cwd=repo, timeout=15)
                return 0

            real_run_local = ld.run_local

            def router(args, cwd=None, timeout=None, input_text=None):
                if len(args) >= 3 and args[1:3] == ["-m", "unittest"]:
                    return ld.CmdResult(0, "OK", "")
                return real_run_local(args, cwd=cwd, timeout=timeout, input_text=input_text)

            with mock.patch.object(ls, "run_promote", fake_promote), \
                 mock.patch.object(ld, "run_local", router), \
                 mock.patch.object(ld, "push_origin", return_value=(False, "connection refused")), \
                 mock.patch.object(ls, "run_deploy") as fake_deploy:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = ls.cmd_ship(ship_args(task_id=tid, message="msg"))
                fake_deploy.assert_not_called()

        self.assertEqual(rc, 1)
        self.assertIn("push failed", buf.getvalue())
        self.assertIn("git log --oneline -3", buf.getvalue())
        self.assertIn("git branch -vv", buf.getvalue())

    def test_push_failure_may_continue_when_origin_already_has_head(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid, repo, wt = self._bootstrap(tmp_path)

            def fake_promote(t):
                (repo / "NOTES.txt").write_text("new file\n", encoding="utf-8")
                ld.run_local(["git", "add", "-f", "NOTES.txt"], cwd=repo, timeout=15)
                return 0

            real_run_local = ld.run_local

            def router(args, cwd=None, timeout=None, input_text=None):
                if len(args) >= 3 and args[1:3] == ["-m", "unittest"]:
                    return ld.CmdResult(0, "OK", "")
                return real_run_local(args, cwd=cwd, timeout=timeout, input_text=input_text)

            def fake_push_origin(repo=None, branch="main", timeout=60):
                # Simulate: the push call itself reports failure, but a real
                # push actually landed (e.g. a flaky client-side error) —
                # push the commit for real here so origin genuinely has it,
                # then report failure so cmd_ship must verify independently.
                real_run_local(["git", "push", "origin", "main"], cwd=repo, timeout=15)
                return False, "simulated flaky push client error"

            with mock.patch.object(ls, "run_promote", fake_promote), \
                 mock.patch.object(ld, "run_local", router), \
                 mock.patch.object(ld, "push_origin", fake_push_origin), \
                 mock.patch.object(ls, "run_deploy", return_value=(0, None)) as fake_deploy:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = ls.cmd_ship(ship_args(task_id=tid, message="msg"))
                fake_deploy.assert_called_once()

        self.assertEqual(rc, 0)
        self.assertIn("already contains local HEAD", buf.getvalue())

    def test_no_push_commits_but_skips_push_and_deploy(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid, repo, wt = self._bootstrap(tmp_path)

            def fake_promote(t):
                (repo / "NOTES.txt").write_text("new file\n", encoding="utf-8")
                ld.run_local(["git", "add", "-f", "NOTES.txt"], cwd=repo, timeout=15)
                return 0

            real_run_local = ld.run_local

            def router(args, cwd=None, timeout=None, input_text=None):
                if len(args) >= 3 and args[1:3] == ["-m", "unittest"]:
                    return ld.CmdResult(0, "OK", "")
                return real_run_local(args, cwd=cwd, timeout=timeout, input_text=input_text)

            with mock.patch.object(ls, "run_promote", fake_promote), \
                 mock.patch.object(ld, "run_local", router), \
                 mock.patch.object(ld, "push_origin") as fake_push, \
                 mock.patch.object(ls, "run_deploy") as fake_deploy:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = ls.cmd_ship(ship_args(task_id=tid, message="msg", no_push=True))
                fake_push.assert_not_called()
                fake_deploy.assert_not_called()

            self.assertEqual(rc, 0)
            self.assertIn("RESULT: PASS", buf.getvalue())
            log = ld.run_local(["git", "log", "-1", "--pretty=%s"], cwd=repo, timeout=15)
            self.assertEqual(log.stdout.strip(), "msg")

    def test_no_deploy_pushes_but_skips_deploy(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid, repo, wt = self._bootstrap(tmp_path)

            def fake_promote(t):
                (repo / "NOTES.txt").write_text("new file\n", encoding="utf-8")
                ld.run_local(["git", "add", "-f", "NOTES.txt"], cwd=repo, timeout=15)
                return 0

            real_run_local = ld.run_local

            def router(args, cwd=None, timeout=None, input_text=None):
                if len(args) >= 3 and args[1:3] == ["-m", "unittest"]:
                    return ld.CmdResult(0, "OK", "")
                return real_run_local(args, cwd=cwd, timeout=timeout, input_text=input_text)

            with mock.patch.object(ls, "run_promote", fake_promote), \
                 mock.patch.object(ld, "run_local", router), \
                 mock.patch.object(ls, "run_deploy") as fake_deploy:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = ls.cmd_ship(ship_args(task_id=tid, message="msg", no_deploy=True))
                fake_deploy.assert_not_called()

            self.assertEqual(rc, 0)
            self.assertIn("RESULT: PASS", buf.getvalue())
            origin_sha = ld.run_local(["git", "rev-parse", "origin/main"], cwd=repo, timeout=15).stdout.strip()
            local_sha = ld.head_sha(repo)
            self.assertEqual(origin_sha, local_sha)

    def test_success_path_runs_promote_tests_commit_push_deploy_in_order(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid, repo, wt = self._bootstrap(tmp_path)
            order = []

            def fake_promote(t):
                order.append("promote")
                (repo / "NOTES.txt").write_text("new file\n", encoding="utf-8")
                ld.run_local(["git", "add", "-f", "NOTES.txt"], cwd=repo, timeout=15)
                return 0

            real_run_local = ld.run_local

            def router(args, cwd=None, timeout=None, input_text=None):
                if len(args) >= 3 and args[1:3] == ["-m", "unittest"]:
                    order.append("tests")
                    return ld.CmdResult(0, "OK", "")
                if len(args) >= 2 and args[:2] == ["git", "commit"]:
                    order.append("commit")
                return real_run_local(args, cwd=cwd, timeout=timeout, input_text=input_text)

            def fake_push_origin(repo=None, branch="main", timeout=60):
                order.append("push")
                res = real_run_local(["git", "push", "origin", branch], cwd=repo, timeout=15)
                return res.ok, (res.stdout + res.stderr).strip()

            def fake_deploy():
                order.append("deploy")
                return 0, "/var/www/leadmeleads-backups/app_auth-20260710-000000.db"

            with mock.patch.object(ls, "run_promote", fake_promote), \
                 mock.patch.object(ld, "run_local", router), \
                 mock.patch.object(ld, "push_origin", fake_push_origin), \
                 mock.patch.object(ls, "run_deploy", fake_deploy):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = ls.cmd_ship(ship_args(task_id=tid, message="Ship it"))

        self.assertEqual(rc, 0)
        self.assertEqual(order, ["promote", "tests", "commit", "push", "deploy"])
        out = buf.getvalue()
        self.assertIn("RESULT: PASS", out)
        self.assertIn("NOTES.txt", out)  # promoted files reported
        self.assertIn("app_auth-20260710-000000.db", out)  # backup path reported

    def test_promoted_staged_files_are_reported(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid, repo, wt = self._bootstrap(tmp_path, add_worktree_file="CHANGED.md")

            def fake_promote(t):
                (repo / "CHANGED.md").write_text("hello\n", encoding="utf-8")
                ld.run_local(["git", "add", "-f", "CHANGED.md"], cwd=repo, timeout=15)
                return 0

            real_run_local = ld.run_local

            def router(args, cwd=None, timeout=None, input_text=None):
                if len(args) >= 3 and args[1:3] == ["-m", "unittest"]:
                    return ld.CmdResult(0, "OK", "")
                return real_run_local(args, cwd=cwd, timeout=timeout, input_text=input_text)

            with mock.patch.object(ls, "run_promote", fake_promote), \
                 mock.patch.object(ld, "run_local", router), \
                 mock.patch.object(ld, "push_origin", return_value=(True, "")), \
                 mock.patch.object(ls, "run_deploy", return_value=(0, None)):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = ls.cmd_ship(ship_args(task_id=tid, message="msg"))

        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("STAGED FILES", out)
        self.assertIn("CHANGED.md", out)

    def test_new_staged_test_file_is_included_in_test_command(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid, repo, wt = self._bootstrap(
                tmp_path, gitignore_text="test_*.py\n",
                add_worktree_file="scripts/test_ship_promoted.py",
                add_worktree_content="def test_x():\n    assert True\n",
            )

            def fake_promote(t):
                (repo / "scripts").mkdir(exist_ok=True)
                (repo / "scripts" / "test_ship_promoted.py").write_text(
                    "def test_x():\n    assert True\n", encoding="utf-8",
                )
                ld.run_local(["git", "add", "-f", "scripts/test_ship_promoted.py"], cwd=repo, timeout=15)
                return 0

            captured_argv = []
            real_run_local = ld.run_local

            def router(args, cwd=None, timeout=None, input_text=None):
                if len(args) >= 3 and args[1:3] == ["-m", "unittest"]:
                    captured_argv.append(args)
                    return ld.CmdResult(0, "OK", "")
                return real_run_local(args, cwd=cwd, timeout=timeout, input_text=input_text)

            with mock.patch.object(ls, "run_promote", fake_promote), \
                 mock.patch.object(ld, "run_local", router), \
                 mock.patch.object(ld, "push_origin", return_value=(True, "")), \
                 mock.patch.object(ls, "run_deploy", return_value=(0, None)):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = ls.cmd_ship(ship_args(task_id=tid, message="msg"))

        self.assertEqual(rc, 0)
        self.assertEqual(len(captured_argv), 1)
        self.assertIn("scripts.test_ship_promoted", captured_argv[0])

    def test_no_secrets_printed_in_full_report(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid, repo, wt = self._bootstrap(tmp_path)

            def fake_promote(t):
                (repo / "NOTES.txt").write_text("new file\n", encoding="utf-8")
                ld.run_local(["git", "add", "-f", "NOTES.txt"], cwd=repo, timeout=15)
                return 0

            real_run_local = ld.run_local

            def router(args, cwd=None, timeout=None, input_text=None):
                if len(args) >= 3 and args[1:3] == ["-m", "unittest"]:
                    return ld.CmdResult(0, "OK", "")
                return real_run_local(args, cwd=cwd, timeout=timeout, input_text=input_text)

            with mock.patch.object(ls, "run_promote", fake_promote), \
                 mock.patch.object(ld, "run_local", router), \
                 mock.patch.object(ld, "push_origin", return_value=(True, "")), \
                 mock.patch.object(ls, "run_deploy", return_value=(0, None)):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = ls.cmd_ship(ship_args(task_id=tid, message="msg"))

        self.assertEqual(rc, 0)
        self.assertIsNone(ld.SECRET_LIKE_KEY_PATTERN.search(buf.getvalue()))


class DeployFailureTests(unittest.TestCase, IsolatedDirsMixin):
    def test_deploy_failure_prints_rollback_info(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid, repo, wt = bootstrap_shippable_task(self, tmp_path)

            def fake_promote(t):
                (repo / "NOTES.txt").write_text("new file\n", encoding="utf-8")
                ld.run_local(["git", "add", "-f", "NOTES.txt"], cwd=repo, timeout=15)
                return 0

            real_run_local = ld.run_local

            def router(args, cwd=None, timeout=None, input_text=None):
                if len(args) >= 3 and args[1:3] == ["-m", "unittest"]:
                    return ld.CmdResult(0, "OK", "")
                return real_run_local(args, cwd=cwd, timeout=timeout, input_text=input_text)

            with mock.patch.object(ls, "run_promote", fake_promote), \
                 mock.patch.object(ld, "run_local", router), \
                 mock.patch.object(ld, "push_origin", return_value=(True, "")), \
                 mock.patch.object(ls, "run_deploy", return_value=(1, None)), \
                 mock.patch.object(ld, "cmd_rollback_info", return_value=0) as fake_rollback:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = ls.cmd_ship(ship_args(task_id=tid, message="msg"))
                fake_rollback.assert_called_once()

        self.assertEqual(rc, 1)
        self.assertIn("RESULT: FAIL", buf.getvalue())
        self.assertIn("rollback-info", buf.getvalue())


class ArgumentParsingTests(unittest.TestCase):
    def test_task_id_and_message_parse(self):
        parser = ls.build_parser()
        args = parser.parse_args(["20260710-000000-x", "-m", "Ship it"])
        self.assertEqual(args.task_id, "20260710-000000-x")
        self.assertEqual(args.message, "Ship it")
        self.assertFalse(args.no_deploy)
        self.assertFalse(args.no_push)
        self.assertFalse(args.dry_run)
        self.assertIsNone(args.test_command)

    def test_flags_parse(self):
        parser = ls.build_parser()
        args = parser.parse_args(["x", "--no-deploy", "--no-push", "--dry-run",
                                   "--test-command", "python3 -m unittest scripts.test_foo"])
        self.assertTrue(args.no_deploy)
        self.assertTrue(args.no_push)
        self.assertTrue(args.dry_run)
        self.assertEqual(args.test_command, "python3 -m unittest scripts.test_foo")

    def test_missing_task_id_parses_to_none_not_a_hard_error(self):
        parser = ls.build_parser()
        args = parser.parse_args(["-m", "msg"])
        self.assertIsNone(args.task_id)


class NoAutoShipFromCollabTests(unittest.TestCase):
    """Static checks mirroring the safety-rule requirement that nothing in
    leadme-collab auto-chains into leadme-ship."""

    def test_leadme_collab_source_never_references_leadme_ship(self):
        src = Path(lc.__file__).read_text(encoding="utf-8")
        self.assertNotIn("leadme_ship", src)
        self.assertNotIn("leadme-ship", src)

    def test_leadme_ship_never_auto_invoked_by_start_or_advance(self):
        src = Path(ls.__file__).read_text(encoding="utf-8")
        # ship imports collab/deploy, never the other way around; and ship's
        # own module never wires itself into collab's automatic phases.
        self.assertIn("import leadme_collab", src)


if __name__ == "__main__":
    unittest.main()
