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

import argparse
import io
import json
import subprocess
import sys
import time
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

    def test_discover_codex_installed_but_not_authenticated_is_unusable(self):
        """Codex present on PATH but `codex login status` says not logged
        in — command -v alone must never be treated as usable."""
        def fake_run_local(args, cwd=None, timeout=None, input_text=None):
            if args[1:] == ["--version"]:
                return ld.CmdResult(0, "codex-cli 0.144.1", "")
            if args[1:] == ["login", "status"]:
                return ld.CmdResult(1, "Not logged in", "")
            raise AssertionError(f"unexpected call: {args}")

        with mock.patch("shutil.which", return_value="/home/x/.local/bin/codex"), \
             mock.patch.object(ld, "run_local", fake_run_local):
            info = lc.discover_codex()

        self.assertTrue(info["available"])
        self.assertFalse(info["authenticated"])
        self.assertFalse(info["usable_noninteractive"])

    def test_discover_codex_installed_and_authenticated_is_usable(self):
        def fake_run_local(args, cwd=None, timeout=None, input_text=None):
            if args[1:] == ["--version"]:
                return ld.CmdResult(0, "codex-cli 0.144.1", "")
            if args[1:] == ["login", "status"]:
                return ld.CmdResult(0, "Logged in using ChatGPT", "")
            raise AssertionError(f"unexpected call: {args}")

        with mock.patch("shutil.which", return_value="/home/x/.local/bin/codex"), \
             mock.patch.object(ld, "run_local", fake_run_local):
            info = lc.discover_codex()

        self.assertTrue(info["available"])
        self.assertTrue(info["authenticated"])
        self.assertTrue(info["usable_noninteractive"])

    def test_choose_reviewer_mode_falls_back_to_file_handoff_when_unavailable(self):
        with mock.patch("shutil.which", return_value=None):
            mode, info = lc.choose_reviewer_mode()
        self.assertEqual(mode, "file-handoff")

    def test_choose_reviewer_mode_falls_back_to_file_handoff_when_unauthenticated(self):
        def fake_run_local(args, cwd=None, timeout=None, input_text=None):
            if args[1:] == ["--version"]:
                return ld.CmdResult(0, "codex-cli 0.144.1", "")
            return ld.CmdResult(1, "Not logged in", "")

        with mock.patch("shutil.which", return_value="/x/codex"), \
             mock.patch.object(ld, "run_local", fake_run_local):
            mode, info = lc.choose_reviewer_mode()
        self.assertEqual(mode, "file-handoff")

    def test_choose_reviewer_mode_uses_codex_when_usable(self):
        def fake_run_local(args, cwd=None, timeout=None, input_text=None):
            if args[1:] == ["--version"]:
                return ld.CmdResult(0, "codex-cli 0.144.1", "")
            return ld.CmdResult(0, "Logged in using ChatGPT", "")

        with mock.patch("shutil.which", return_value="/x/codex"), \
             mock.patch.object(ld, "run_local", fake_run_local):
            mode, info = lc.choose_reviewer_mode()
        self.assertEqual(mode, "codex")

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


def fake_monitored_result(stdout="", stderr="", returncode=0, elapsed_seconds=1.0, pid=999, timed_out=False):
    return {
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
        "elapsed_seconds": elapsed_seconds,
        "pid": pid,
        "timed_out": timed_out,
    }


class ClaudeAdapterCommandTests(unittest.TestCase):
    """run_claude()/run_codex_review() delegate to run_monitored() for the
    actual subprocess lifecycle (heartbeat/timeout/PID tracking), so
    command-construction tests mock run_monitored, not a lower-level
    primitive — see RunMonitoredTests below for real-subprocess coverage of
    run_monitored itself."""

    def test_run_claude_builds_expected_argv(self):
        captured = {}

        def fake_run_monitored(args, cwd, timeout, role_label, input_text=None, tid=None, **kw):
            captured.update(args=args, cwd=cwd, timeout=timeout, role_label=role_label, tid=tid)
            return fake_monitored_result(stdout=json.dumps({"result": "ok", "is_error": False, "total_cost_usd": 0.01}))

        with mock.patch.object(lc, "run_monitored", fake_run_monitored):
            result = lc.run_claude("do the thing", cwd="/some/worktree", timeout=42, tid="t1")

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
        self.assertEqual(captured["tid"], "t1")
        self.assertEqual(captured["role_label"], "Claude implementer")
        self.assertFalse(result["is_error"])
        self.assertEqual(result["result_text"], "ok")

    def test_disallowed_tools_blocks_push_commit_merge_sudo_installs(self):
        for forbidden in ("git push", "git commit", "git merge", "sudo", "pip install", "npm install", "apt-get"):
            self.assertIn(forbidden, lc.DISALLOWED_TOOLS)

    def test_run_claude_surfaces_timeout_as_error_with_reason(self):
        def fake_run_monitored(args, cwd, timeout, role_label, input_text=None, tid=None, **kw):
            return fake_monitored_result(returncode=-9, timed_out=True, elapsed_seconds=5.0)

        with mock.patch.object(lc, "run_monitored", fake_run_monitored):
            result = lc.run_claude("slow task", cwd="/x", timeout=5)
        self.assertTrue(result["timed_out"])
        self.assertTrue(result["is_error"])
        self.assertIn("timed out", result["reason"])
        self.assertIn("5s", result["reason"])

    def test_run_claude_makes_exactly_one_monitored_call(self):
        calls = []

        def fake_run_monitored(args, cwd, timeout, role_label, input_text=None, tid=None, **kw):
            calls.append(args)
            return fake_monitored_result(stdout=json.dumps({"result": "ok", "is_error": False}))

        with mock.patch.object(lc, "run_monitored", fake_run_monitored):
            lc.run_claude("task", cwd="/x")
        self.assertEqual(len(calls), 1)

    def test_run_claude_nonzero_exit_without_json_has_reason(self):
        def fake_run_monitored(args, cwd, timeout, role_label, input_text=None, tid=None, **kw):
            return fake_monitored_result(returncode=1, stdout="not json")

        with mock.patch.object(lc, "run_monitored", fake_run_monitored):
            result = lc.run_claude("task", cwd="/x")
        self.assertTrue(result["is_error"])
        self.assertTrue(result["reason"])


class CodexAdapterCommandTests(unittest.TestCase):
    """run_codex_review writes its result via `-o <file>`, so the fake
    run_monitored must simulate that side effect the same way the real
    `codex` binary would, or output_path.exists() never becomes true."""

    def _fake_run_monitored_writing_output(self, output_text, returncode=0, stderr="", stdout="",
                                            elapsed_seconds=1.0, timed_out=False):
        captured = {}

        def fake(args, cwd, timeout, role_label, input_text=None, tid=None, **kw):
            captured.update(args=args, cwd=cwd, timeout=timeout, role_label=role_label,
                             input_text=input_text, tid=tid)
            if output_text is not None and "-o" in args:
                idx = args.index("-o")
                Path(args[idx + 1]).write_text(output_text, encoding="utf-8")
            return fake_monitored_result(
                stdout=stdout, stderr=stderr, returncode=returncode,
                elapsed_seconds=elapsed_seconds, timed_out=timed_out,
            )

        return fake, captured

    def test_run_codex_review_builds_expected_argv(self):
        fake, captured = self._fake_run_monitored_writing_output("VERDICT: PASS\nlooks good")
        with mock.patch.object(lc, "run_monitored", fake):
            result = lc.run_codex_review("review this diff", cwd="/some/worktree", timeout=42, tid="t1")

        self.assertEqual(captured["args"][0], "codex")
        self.assertIn("exec", captured["args"])
        self.assertIn("--sandbox", captured["args"])
        self.assertIn("read-only", captured["args"])
        self.assertIn("--ephemeral", captured["args"])
        self.assertIn("-o", captured["args"])
        self.assertIn("-", captured["args"])  # prompt delivered via stdin, not argv
        self.assertEqual(captured["cwd"], "/some/worktree")
        self.assertEqual(captured["timeout"], 42)
        self.assertEqual(captured["tid"], "t1")
        self.assertEqual(captured["role_label"], "Codex reviewer")
        self.assertEqual(captured["input_text"], "review this diff")
        self.assertEqual(result["result_text"], "VERDICT: PASS\nlooks good")
        self.assertFalse(result["is_error"])

    def test_run_codex_review_never_requests_a_write_capable_sandbox(self):
        source = Path(lc.__file__).read_text(encoding="utf-8")
        start = source.index("def run_codex_review")
        end = source.index("\ndef ", start + 1)
        body = source[start:end]
        for forbidden in ("workspace-write", "danger-full-access", "acceptEdits", "bypass-approvals-and-sandbox"):
            self.assertNotIn(forbidden, body)

    def test_run_codex_review_captures_stdout_and_stderr(self):
        fake, captured = self._fake_run_monitored_writing_output(
            "VERDICT: PASS\nfine", stderr="progress noise", stdout="raw stdout"
        )
        with mock.patch.object(lc, "run_monitored", fake):
            result = lc.run_codex_review("prompt", cwd="/x")
        self.assertEqual(result["stderr"], "progress noise")
        self.assertEqual(result["stdout"], "raw stdout")

    def test_run_codex_review_nonzero_exit_is_error_with_reason(self):
        fake, _ = self._fake_run_monitored_writing_output(None, returncode=2, stderr="error: bad args")
        with mock.patch.object(lc, "run_monitored", fake):
            result = lc.run_codex_review("prompt", cwd="/x")
        self.assertTrue(result["is_error"])
        self.assertIsNone(result["result_text"])
        self.assertIn("exited 2", result["reason"])

    def test_run_codex_review_timeout_is_error_with_reason(self):
        fake, _ = self._fake_run_monitored_writing_output(
            None, returncode=-9, timed_out=True, elapsed_seconds=5.0,
        )
        with mock.patch.object(lc, "run_monitored", fake):
            result = lc.run_codex_review("slow review", cwd="/x", timeout=5)
        self.assertTrue(result["timed_out"])
        self.assertTrue(result["is_error"])
        self.assertIn("timed out", result["reason"])

    def test_run_codex_review_missing_output_file_is_error_with_reason(self):
        fake, _ = self._fake_run_monitored_writing_output(None, returncode=0)  # "succeeds" but writes nothing
        with mock.patch.object(lc, "run_monitored", fake):
            result = lc.run_codex_review("prompt", cwd="/x")
        self.assertTrue(result["is_error"])
        self.assertIsNone(result["result_text"])
        self.assertIn("no output", result["reason"])


class RunMonitoredTests(unittest.TestCase, IsolatedDirsMixin):
    """run_monitored() is the fix for the first-real-use failure (suspended
    process group + zero output looked identical to a hang). These use real
    short-lived subprocesses — sleep/exit behavior can't be proven by
    mocking subprocess.Popen away."""

    def test_captures_stdout_stderr_and_returncode(self):
        result = lc.run_monitored(
            [sys.executable, "-c", "import sys; print('hello'); print('oops', file=sys.stderr); sys.exit(3)"],
            cwd=None, timeout=10, role_label="test-role",
        )
        self.assertEqual(result["returncode"], 3)
        self.assertIn("hello", result["stdout"])
        self.assertIn("oops", result["stderr"])
        self.assertFalse(result["timed_out"])
        self.assertIsNotNone(result["pid"])

    def test_heartbeat_prints_while_waiting(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = lc.run_monitored(
                [sys.executable, "-c", "import time; time.sleep(0.6)"],
                cwd=None, timeout=10, role_label="sleepy-role",
                heartbeat_interval=0.15, poll_interval=0.05,
            )
        self.assertFalse(result["timed_out"])
        self.assertIn("[RUNNING] sleepy-role", buf.getvalue())

    def test_heartbeat_not_spammed_faster_than_interval(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            lc.run_monitored(
                [sys.executable, "-c", "import time; time.sleep(0.6)"],
                cwd=None, timeout=10, role_label="r",
                heartbeat_interval=0.3, poll_interval=0.05,
            )
        count = buf.getvalue().count("[RUNNING]")
        # ~0.6s / 0.3s interval -> a couple of heartbeats, never one per poll
        # tick (which would be ~12 at a 0.05s poll interval).
        self.assertGreaterEqual(count, 1)
        self.assertLessEqual(count, 4)

    def test_kills_process_and_leaves_no_orphan_on_timeout(self):
        result = lc.run_monitored(
            [sys.executable, "-c", "import time; time.sleep(20)"],
            cwd=None, timeout=0.5, role_label="slow-role",
            poll_interval=0.05,
        )
        self.assertTrue(result["timed_out"])
        time.sleep(0.5)
        self.assertFalse(lc.process_is_alive(result["pid"]))

    def test_process_is_alive_reflects_real_process_state(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)"])
        try:
            self.assertTrue(lc.process_is_alive(proc.pid))
        finally:
            proc.kill()
            proc.wait(timeout=5)
        self.assertFalse(lc.process_is_alive(proc.pid))

    def test_active_process_recorded_during_run_and_cleared_after(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.isolate(Path(tmp))
            tid = "t-heartbeat"
            lc.write_task_state(tid, {"task_id": tid, "phase": "isolated"})

            real_set = lc._set_active_process
            real_heartbeat = lc._touch_active_process_heartbeat
            real_clear = lc._clear_active_process
            calls = {"set": 0, "heartbeat": 0, "clear": 0}

            def spy_set(*a, **kw):
                calls["set"] += 1
                return real_set(*a, **kw)

            def spy_heartbeat(*a, **kw):
                calls["heartbeat"] += 1
                return real_heartbeat(*a, **kw)

            def spy_clear(*a, **kw):
                calls["clear"] += 1
                return real_clear(*a, **kw)

            with mock.patch.object(lc, "_set_active_process", spy_set), \
                 mock.patch.object(lc, "_touch_active_process_heartbeat", spy_heartbeat), \
                 mock.patch.object(lc, "_clear_active_process", spy_clear):
                lc.run_monitored(
                    [sys.executable, "-c", "import time; time.sleep(0.6)"],
                    cwd=None, timeout=10, role_label="tracked-role",
                    tid=tid, heartbeat_interval=0.15, poll_interval=0.05,
                )

            self.assertEqual(calls["set"], 1)
            self.assertGreaterEqual(calls["heartbeat"], 1)
            self.assertEqual(calls["clear"], 1)
            self.assertNotIn("active_process", lc.read_task_state(tid))


class DuplicateActiveTaskTests(unittest.TestCase, IsolatedDirsMixin):
    def test_normalize_task_text_collapses_whitespace_and_case(self):
        self.assertEqual(
            lc.normalize_task_text("  Fix   THE\nThing  "),
            "fix the thing",
        )

    def test_finds_active_duplicate_by_normalized_description(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.isolate(Path(tmp))
            lc.write_task_state("t1", {
                "task_id": "t1", "phase": "isolated",
                "task_description": "Fix the thing",
            })
            tid, state = lc.find_active_duplicate("  fix   THE thing  ")
            self.assertEqual(tid, "t1")

    def test_terminal_phase_task_is_not_treated_as_duplicate(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.isolate(Path(tmp))
            lc.write_task_state("t1", {
                "task_id": "t1", "phase": "done",
                "task_description": "Fix the thing",
            })
            tid, state = lc.find_active_duplicate("Fix the thing")
            self.assertIsNone(tid)

    def test_cmd_start_blocks_on_active_duplicate(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_temp_git_repo(tmp_path)
            self.isolate(tmp_path, repo_path=repo)
            lc.write_task_state("t1", {
                "task_id": "t1", "phase": "isolated",
                "task_description": "fix the history route",
                "created_at": "2026-01-01T00:00:00+00:00",
            })

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lc.cmd_start(argparse.Namespace(
                    task="Fix the history route", task_file=None, force_new=False,
                ))
            self.assertEqual(rc, 1)
            out = buf.getvalue()
            self.assertIn("already exists", out)
            self.assertIn("t1", out)
            # Confirms it never even reached preflight for a new task.
            self.assertNotIn("PREFLIGHT", out)

    def test_force_new_bypasses_duplicate_block(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_temp_git_repo(tmp_path)
            self.isolate(tmp_path, repo_path=repo)
            lc.write_task_state("t1", {
                "task_id": "t1", "phase": "isolated",
                "task_description": "fix the history route",
                "created_at": "2026-01-01T00:00:00+00:00",
            })

            with mock.patch.object(lc, "discover_claude", return_value={"available": False, "path": None, "version": None, "print_mode": False}):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = lc.cmd_start(argparse.Namespace(
                        task="Fix the history route", task_file=None, force_new=True,
                    ))
            out = buf.getvalue()
            # It should have proceeded past duplicate-detection into real
            # preflight/isolation (and then hit the (mocked) missing-claude
            # needs_human path) rather than being blocked outright.
            self.assertIn("PREFLIGHT", out)
            self.assertNotIn("already exists", out)


class StaleProcessAndResumeTests(unittest.TestCase, IsolatedDirsMixin):
    def test_check_no_concurrent_process_blocks_when_pid_alive(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)"])
        try:
            state = {"active_process": {"role": "Claude implementer", "pid": proc.pid,
                                         "started_at": lc.utc_now_iso(), "last_heartbeat_at": lc.utc_now_iso()}}
            r = lc.Reporter(stream=False)
            ok = lc._check_no_concurrent_process("t1", state, r)
            self.assertFalse(ok)
            self.assertTrue(r.failed)
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_check_no_concurrent_process_clears_stale_dead_pid(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.isolate(Path(tmp))
            tid = "t-stale"
            # A PID essentially guaranteed not to be alive: spawn and reap one.
            proc = subprocess.Popen([sys.executable, "-c", "pass"])
            proc.wait(timeout=5)
            dead_pid = proc.pid

            lc.write_task_state(tid, {
                "task_id": tid, "phase": "isolated",
                "active_process": {"role": "Claude implementer", "pid": dead_pid,
                                    "started_at": lc.utc_now_iso(), "last_heartbeat_at": lc.utc_now_iso()},
            })
            state = lc.read_task_state(tid)
            r = lc.Reporter(stream=False)
            ok = lc._check_no_concurrent_process(tid, state, r)
            self.assertTrue(ok)
            self.assertNotIn("active_process", lc.read_task_state(tid))

    def test_resume_explains_stale_implementer_start(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_temp_git_repo(tmp_path)
            self.isolate(tmp_path, repo_path=repo)
            base_sha = ld.head_sha(repo)
            tid = "t-resume-stale"
            lc.create_safety_ref(base_sha, tid)
            ok, wt_path, branch, err = lc.create_worktree(base_sha, tid)
            self.assertTrue(ok, err)

            proc = subprocess.Popen([sys.executable, "-c", "pass"])
            proc.wait(timeout=5)
            dead_pid = proc.pid

            lc.write_task_state(tid, {
                "task_id": tid, "phase": "isolated", "worktree": str(wt_path), "branch": branch,
                "cycle": 1, "max_review_cycles": 3, "reviewer_mode": "file-handoff",
                "active_process": {"role": "Claude implementer", "pid": dead_pid,
                                    "started_at": lc.utc_now_iso(), "last_heartbeat_at": lc.utc_now_iso()},
            })
            lc.write_artifact(tid, "task.md", "# Task\n\nsomething\n")

            with mock.patch.object(lc, "discover_claude", return_value={"available": False, "path": None, "version": None, "print_mode": False}):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    lc.cmd_resume(argparse.Namespace(task_id=tid))
            out = buf.getvalue()
            self.assertIn("no longer running", out)
            self.assertIn("retrying", out)


class StatusDisplayTests(unittest.TestCase, IsolatedDirsMixin):
    def test_status_shows_active_process_elapsed_and_heartbeat(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.isolate(Path(tmp))
            tid = "t-status-active"
            lc.write_task_state(tid, {
                "task_id": tid, "phase": "implementing", "cycle": 1, "max_review_cycles": 3,
                "worktree": "/nonexistent", "branch": "collab/x", "reviewer_mode": "codex", "final": None,
                "active_process": {
                    "role": "Claude implementer", "pid": 999999,
                    "started_at": lc.utc_now_iso(), "last_heartbeat_at": lc.utc_now_iso(),
                    "timeout_seconds": 600,
                },
            })
            buf = io.StringIO()
            with redirect_stdout(buf):
                lc.cmd_status(argparse.Namespace(task_id=tid))
            out = buf.getvalue()
            self.assertIn("process:", out)
            self.assertIn("Claude implementer", out)
            self.assertIn("elapsed:", out)
            self.assertIn("last heartbeat:", out)
            self.assertIn("timeout:", out)
            self.assertIn("pid:", out)

    def test_status_warns_when_recorded_process_is_not_actually_running(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.isolate(Path(tmp))
            tid = "t-status-stale"
            lc.write_task_state(tid, {
                "task_id": tid, "phase": "implementing", "cycle": 1, "max_review_cycles": 3,
                "worktree": "/nonexistent", "branch": "collab/x", "reviewer_mode": "codex", "final": None,
                "active_process": {
                    "role": "Claude implementer", "pid": 999999,  # not a real running pid
                    "started_at": lc.utc_now_iso(), "last_heartbeat_at": lc.utc_now_iso(),
                    "timeout_seconds": 600,
                },
            })
            buf = io.StringIO()
            with redirect_stdout(buf):
                lc.cmd_status(argparse.Namespace(task_id=tid))
            out = buf.getvalue()
            self.assertIn("no child process is active", out)


class AbortKillsLiveProcessTests(unittest.TestCase, IsolatedDirsMixin):
    def test_abort_kills_recorded_live_process_leaving_no_orphan(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_temp_git_repo(tmp_path)
            self.isolate(tmp_path, repo_path=repo)
            base_sha = ld.head_sha(repo)
            tid = "t-abort-live"
            ok_ref, ref, _ = lc.create_safety_ref(base_sha, tid)
            ok, wt_path, branch, err = lc.create_worktree(base_sha, tid)
            self.assertTrue(ok, err)

            proc = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                start_new_session=True,
            )
            try:
                lc.write_task_state(tid, {
                    "task_id": tid, "phase": "isolated", "worktree": str(wt_path), "branch": branch,
                    "safety_ref": ref, "final": None,
                    "active_process": {"role": "Claude implementer", "pid": proc.pid,
                                        "started_at": lc.utc_now_iso(), "last_heartbeat_at": lc.utc_now_iso()},
                })
                self.assertTrue(lc.process_is_alive(proc.pid))

                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = lc.cmd_abort(argparse.Namespace(task_id=tid, cleanup=False))
                self.assertEqual(rc, 0)

                # cmd_abort kills by PID (not via a Popen handle, since it only
                # ever has a PID from state.json), so the killed process is a
                # zombie until its actual parent (this test) reaps it — exactly
                # like a real orchestrator process would eventually reap its
                # own child. Reap, then confirm nothing is left running.
                proc.wait(timeout=5)
                self.assertFalse(lc.process_is_alive(proc.pid))
                self.assertNotIn("active_process", lc.read_task_state(tid))
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)


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
        """Dirty UNSTAGED tracked file blocks start."""
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
        self.assertIn("[FAIL] primary tracked tree clean", buf.getvalue())

    def test_staged_dirty_tracked_file_blocks_start(self):
        """Dirty STAGED (but uncommitted) tracked file also blocks start."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_temp_git_repo(tmp_path)
            (repo / "README.md").write_text("staged change\n", encoding="utf-8")
            res = ld.run_local(["git", "add", "README.md"], cwd=repo, timeout=15)
            self.assertTrue(res.ok, res.stderr)
            self.isolate(tmp_path, repo_path=repo)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lc.cmd_start(self._args())
        self.assertEqual(rc, 1)
        self.assertIn("[FAIL] primary tracked tree clean", buf.getvalue())

    def test_clean_tracked_tree_passes_preflight(self):
        """A genuinely clean tree gets past the tracked-tree-clean check
        (proceeds to later phases rather than stopping here)."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_temp_git_repo(tmp_path)
            self.isolate(tmp_path, repo_path=repo)
            with mock.patch.object(lc, "discover_claude",
                                    return_value={"available": False, "path": None, "version": None, "print_mode": False}):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    lc.cmd_start(self._args())
        out = buf.getvalue()
        self.assertIn("[PASS] primary tracked tree clean", out)

    def test_known_untracked_files_remain_non_blocking_at_start(self):
        """.claude/, CLAUDE.md.backup-*, reset_local_password.py must not
        block start even though the tracked tree check runs regardless."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_temp_git_repo(tmp_path)
            (repo / ".claude").mkdir()
            (repo / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
            (repo / "reset_local_password.py").write_text("# scratch\n", encoding="utf-8")
            self.isolate(tmp_path, repo_path=repo)
            with mock.patch.object(lc, "discover_claude",
                                    return_value={"available": False, "path": None, "version": None, "print_mode": False}):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = lc.cmd_start(self._args())
        out = buf.getvalue()
        self.assertIn("[PASS] primary tracked tree clean", out)
        self.assertIn("known untracked (ignored)", out)
        self.assertNotIn("[FAIL] primary tracked tree clean", out)
        # It proceeded well past the tree-clean check (blocked later only on
        # the mocked-unavailable claude CLI), proving untracked files never
        # blocked preflight itself.
        self.assertEqual(rc, 1)
        self.assertIn("claude", out.lower())

    def test_tracked_tree_clean_none_prints_visible_reason_not_silent_exit(self):
        """This is the exact bug report: tracked_tree_clean() returning None
        must never short-circuit past r.step() and exit silently — it must
        print a visible [FAIL] with a reason."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_temp_git_repo(tmp_path)
            self.isolate(tmp_path, repo_path=repo)
            with mock.patch.object(ld, "tracked_tree_clean", return_value=None):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = lc.cmd_start(self._args())
        out = buf.getvalue()
        self.assertEqual(rc, 1)
        # The previous bug: `clean is None or not r.step(...)` short-circuits
        # on `or`, so r.step() is never called and nothing prints past the
        # prior two [PASS] lines. Assert the failure is now visible with a
        # concrete reason, not just silence after "primary repo on main".
        self.assertIn("[FAIL] primary tracked tree clean", out)
        self.assertIn("could not determine", out)

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


def bootstrap_review_task(test_case, tmp_path, reviewer_mode="file-handoff", tid="20260709-000000-loop-test"):
    """Shared by ReviewLoopTests and CodexReviewLoopTests: sets up a real
    worktree + safety ref already at phase="implemented", ready for a
    review cycle to run against it."""
    repo = make_temp_git_repo(tmp_path)
    test_case.isolate(tmp_path, repo_path=repo)
    base_sha = ld.head_sha(repo)
    lc.create_safety_ref(base_sha, tid)
    ok, wt_path, branch, err = lc.create_worktree(base_sha, tid)
    test_case.assertTrue(ok, err)
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
        "reviewer_mode": reviewer_mode,
        "created_at": lc.utc_now_iso(),
        "updated_at": lc.utc_now_iso(),
        "final": None,
    }
    lc.write_task_state(tid, state)
    lc.write_artifact(tid, "verification.md", "no changed files")
    return tid, wt_path


class ReviewLoopTests(unittest.TestCase, IsolatedDirsMixin):
    def _bootstrap_task(self, tmp_path, reviewer_mode="file-handoff", tid="20260709-000000-loop-test"):
        return bootstrap_review_task(self, tmp_path, reviewer_mode=reviewer_mode, tid=tid)

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

            def fake_run_claude(prompt, cwd, timeout=lc.CLAUDE_TIMEOUT_SECONDS, tid=None, **kw):
                return {
                    "returncode": 0, "stdout": "", "stderr": "", "elapsed_seconds": 1.0,
                    "result_text": "fixed", "is_error": False, "total_cost_usd": 0.01, "parsed": {},
                    "timed_out": False, "pid": 1234, "reason": "",
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

            def fake_run_claude(prompt, cwd, timeout=lc.CLAUDE_TIMEOUT_SECONDS, tid=None, **kw):
                claude_calls.append(prompt)
                return {"returncode": 0, "stdout": "", "stderr": "", "elapsed_seconds": 1.0,
                        "result_text": "x", "is_error": False, "total_cost_usd": 0.0, "parsed": {},
                        "timed_out": False, "pid": 1234, "reason": ""}

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


def fake_codex_result(verdict_text, returncode=0):
    return {
        "returncode": returncode,
        "stdout": "",
        "stderr": "model: gpt-5.6-sol\nsandbox: read-only\napproval: never\n",
        "elapsed_seconds": 3.2,
        "result_text": verdict_text,
        "is_error": returncode != 0 or not verdict_text,
        "timed_out": False,
        "pid": 4321,
        "reason": "" if (returncode == 0 and verdict_text) else "codex failed",
    }


class CodexReviewLoopTests(unittest.TestCase, IsolatedDirsMixin):
    """The whole point of the Codex bridge: these run entirely within a
    single _advance_task() call, with no file-handoff pause in between —
    proving Claude -> Codex -> (repair) -> Codex re-review runs
    automatically."""

    def _bootstrap_codex_task(self, tmp_path, tid="20260709-000000-codex-loop"):
        return bootstrap_review_task(self, tmp_path, reviewer_mode="codex", tid=tid)

    def _usable_codex_info(self):
        return {"available": True, "path": "/x/codex", "version": "0.144.1",
                "authenticated": True, "usable_noninteractive": True, "detail": ""}

    def test_pass_verdict_from_codex_stops_the_loop_automatically(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tid, wt_path = self._bootstrap_codex_task(Path(tmp))

            with mock.patch.object(lc, "discover_codex", return_value=self._usable_codex_info()), \
                 mock.patch.object(lc, "run_codex_review", return_value=fake_codex_result("VERDICT: PASS\nclean change")):
                result = lc._advance_task(tid, lc.Reporter())

            self.assertEqual(result, "done")
            state = lc.read_task_state(tid)
            self.assertEqual(state["final"], "READY FOR HUMAN REVIEW")
            self.assertEqual((lc.task_dir(tid) / "review.md").read_text(encoding="utf-8"), "VERDICT: PASS\nclean change")

    def test_needs_fix_triggers_claude_repair_then_a_second_codex_review_automatically(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tid, wt_path = self._bootstrap_codex_task(Path(tmp))

            codex_calls = []

            def fake_codex(prompt, cwd, timeout=lc.CODEX_TIMEOUT_SECONDS, tid=None, **kw):
                codex_calls.append(prompt)
                if len(codex_calls) == 1:
                    return fake_codex_result("VERDICT: NEEDS FIX\nmissing null check")
                return fake_codex_result("VERDICT: PASS\nfixed")

            def fake_claude(prompt, cwd, timeout=lc.CLAUDE_TIMEOUT_SECONDS, tid=None, **kw):
                return {"returncode": 0, "stdout": "", "stderr": "", "elapsed_seconds": 1.0,
                        "result_text": "repaired", "is_error": False, "total_cost_usd": 0.01, "parsed": {},
                        "timed_out": False, "pid": 1234, "reason": ""}

            with mock.patch.object(lc, "discover_codex", return_value=self._usable_codex_info()), \
                 mock.patch.object(lc, "discover_claude", return_value={"available": True, "path": "/x", "version": "1", "print_mode": True}), \
                 mock.patch.object(lc, "run_codex_review", fake_codex), \
                 mock.patch.object(lc, "run_claude", fake_claude):
                result = lc._advance_task(tid, lc.Reporter())

            self.assertEqual(result, "done")
            self.assertEqual(len(codex_calls), 2)  # first review + automatic re-review after repair
            state = lc.read_task_state(tid)
            self.assertEqual(state["final"], "READY FOR HUMAN REVIEW")
            self.assertEqual(state["cycle"], 2)
            self.assertTrue((lc.task_dir(tid) / "repair-output.md").exists())

    def test_max_three_cycles_then_needs_human_all_within_one_advance_call(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tid, wt_path = self._bootstrap_codex_task(Path(tmp))

            codex_calls = []

            def fake_codex(prompt, cwd, timeout=lc.CODEX_TIMEOUT_SECONDS, tid=None, **kw):
                codex_calls.append(prompt)
                return fake_codex_result("VERDICT: NEEDS FIX\nstill broken")

            def fake_claude(prompt, cwd, timeout=lc.CLAUDE_TIMEOUT_SECONDS, tid=None, **kw):
                return {"returncode": 0, "stdout": "", "stderr": "", "elapsed_seconds": 1.0,
                        "result_text": "attempted fix", "is_error": False, "total_cost_usd": 0.0, "parsed": {},
                        "timed_out": False, "pid": 1234, "reason": ""}

            with mock.patch.object(lc, "discover_codex", return_value=self._usable_codex_info()), \
                 mock.patch.object(lc, "discover_claude", return_value={"available": True, "path": "/x", "version": "1", "print_mode": True}), \
                 mock.patch.object(lc, "run_codex_review", fake_codex), \
                 mock.patch.object(lc, "run_claude", fake_claude):
                result = lc._advance_task(tid, lc.Reporter())

            self.assertEqual(result, "needs_human")
            self.assertEqual(len(codex_calls), 3)  # exactly max_review_cycles, never a 4th
            state = lc.read_task_state(tid)
            self.assertEqual(state["final"], "NEEDS HUMAN")
            self.assertEqual(state["cycle"], 3)

    def test_codex_becoming_unavailable_mid_task_fails_closed_not_silently_downgraded(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tid, wt_path = self._bootstrap_codex_task(Path(tmp))
            unusable = {"available": True, "path": "/x/codex", "version": "0.144.1",
                        "authenticated": False, "usable_noninteractive": False,
                        "detail": "codex not authenticated"}
            with mock.patch.object(lc, "discover_codex", return_value=unusable):
                result = lc._advance_task(tid, lc.Reporter())
            self.assertEqual(result, "needs_human")
            self.assertEqual(lc.read_task_state(tid)["final"], "NEEDS HUMAN")


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


class DoctorReviewerModeTests(unittest.TestCase, IsolatedDirsMixin):
    def _run_doctor(self, tmp_path, repo, codex_info, claude_info=None):
        self.isolate(tmp_path, repo_path=repo)
        claude_info = claude_info or {"available": True, "path": "/x/claude", "version": "2.1.0", "print_mode": True}
        with mock.patch.object(lc, "discover_claude", return_value=claude_info), \
             mock.patch.object(lc, "discover_codex", return_value=codex_info), \
             mock.patch.object(ld, "fetch_origin", return_value=(True, "")), \
             mock.patch.object(ld, "divergence_state", return_value=("in-sync", 0, 0)):
            buf = io.StringIO()
            with redirect_stdout(buf):
                lc.cmd_doctor(argparse.Namespace())
        return buf.getvalue()

    def test_doctor_reports_codex_reviewer_pass_when_usable(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_temp_git_repo(tmp_path)
            codex_info = {"available": True, "path": "/x/codex", "version": "0.144.1",
                          "authenticated": True, "usable_noninteractive": True, "detail": ""}
            out = self._run_doctor(tmp_path, repo, codex_info)
        self.assertIn("[PASS] codex reviewer", out)

    def test_doctor_reports_file_handoff_warn_when_codex_unavailable(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_temp_git_repo(tmp_path)
            codex_info = {"available": False, "path": None, "version": None,
                          "authenticated": False, "usable_noninteractive": False,
                          "detail": "codex not found on PATH"}
            out = self._run_doctor(tmp_path, repo, codex_info)
        self.assertIn("[WARN] Codex unavailable", out)
        self.assertNotIn("[PASS] codex reviewer", out)

    def test_doctor_reports_file_handoff_warn_when_codex_installed_but_unauthenticated(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_temp_git_repo(tmp_path)
            codex_info = {"available": True, "path": "/x/codex", "version": "0.144.1",
                          "authenticated": False, "usable_noninteractive": False,
                          "detail": "codex not authenticated (run: codex login --device-auth)"}
            out = self._run_doctor(tmp_path, repo, codex_info)
        self.assertIn("[WARN] Codex unavailable", out)
        self.assertIn("not authenticated", out)


def bootstrap_promotable_task(test_case, tmp_path, tid="20260709-000000-promote-test",
                               gitignore_text=None, phase="done", final="READY FOR HUMAN REVIEW"):
    """Like bootstrap_review_task, but lets the caller commit a .gitignore
    into the base repo before the task worktree branches off it (needed to
    reproduce the test_*.py-hidden-file scenario), and defaults to a
    terminal, ready-to-promote state."""
    repo = make_temp_git_repo(tmp_path)
    if gitignore_text is not None:
        (repo / ".gitignore").write_text(gitignore_text, encoding="utf-8")
        ld.run_local(["git", "add", ".gitignore"], cwd=repo, timeout=15)
        res = ld.run_local(["git", "commit", "-m", "add gitignore"], cwd=repo, timeout=15)
        assert res.ok, res.stderr
    test_case.isolate(tmp_path, repo_path=repo)
    base_sha = ld.head_sha(repo)
    lc.create_safety_ref(base_sha, tid)
    ok, wt_path, branch, err = lc.create_worktree(base_sha, tid)
    test_case.assertTrue(ok, err)
    state = {
        "task_id": tid,
        "task_description": "promote test task",
        "repo": str(repo),
        "base_branch": "main",
        "base_commit": base_sha,
        "safety_ref": lc.safety_ref_name(tid),
        "worktree": str(wt_path),
        "branch": branch,
        "phase": phase,
        "cycle": 1,
        "max_review_cycles": 3,
        "reviewer_mode": "file-handoff",
        "created_at": lc.utc_now_iso(),
        "updated_at": lc.utc_now_iso(),
        "final": final,
    }
    lc.write_task_state(tid, state)
    return tid, repo, wt_path


def promote_args(task_id=None, no_stage=False, commit=False, message=None):
    return argparse.Namespace(task_id=task_id, no_stage=no_stage, commit=commit, message=message)


class PromoteTests(unittest.TestCase, IsolatedDirsMixin):
    def test_promote_refuses_dirty_primary_tracked_tree(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid, repo, wt_path = bootstrap_promotable_task(self, tmp_path)
            (repo / "README.md").write_text("dirty\n", encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lc.cmd_promote(promote_args(task_id=tid))
        self.assertEqual(rc, 1)
        self.assertIn("[FAIL] primary tracked tree clean", buf.getvalue())

    def test_promote_refuses_missing_task(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_temp_git_repo(tmp_path)
            self.isolate(tmp_path, repo_path=repo)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lc.cmd_promote(promote_args(task_id="does-not-exist"))
        self.assertEqual(rc, 1)
        self.assertIn("task state readable", buf.getvalue())

    def test_promote_refuses_when_no_tasks_exist_at_all(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = make_temp_git_repo(tmp_path)
            self.isolate(tmp_path, repo_path=repo)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lc.cmd_promote(promote_args())
        self.assertEqual(rc, 1)
        self.assertIn("no tasks found", buf.getvalue())

    def test_promote_refuses_task_with_no_changes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid, repo, wt_path = bootstrap_promotable_task(self, tmp_path)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lc.cmd_promote(promote_args(task_id=tid))
        self.assertEqual(rc, 1)
        self.assertIn("task has changes to promote", buf.getvalue())

    def test_promote_applies_tracked_modified_file_and_stages_by_default(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid, repo, wt_path = bootstrap_promotable_task(self, tmp_path)
            (wt_path / "README.md").write_text("hello\nworld\n", encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lc.cmd_promote(promote_args(task_id=tid))
            out = buf.getvalue()
            self.assertEqual(rc, 0, out)
            self.assertEqual((repo / "README.md").read_text(encoding="utf-8"), "hello\nworld\n")
            status = ld.run_local(["git", "status", "--short"], cwd=repo, timeout=15)
            self.assertIn("M  README.md", status.stdout)

    def test_promote_includes_untracked_task_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid, repo, wt_path = bootstrap_promotable_task(self, tmp_path)
            (wt_path / "NOTES.txt").write_text("new file\n", encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lc.cmd_promote(promote_args(task_id=tid))
            out = buf.getvalue()
            self.assertEqual(rc, 0, out)
            self.assertIn("untracked", out)
            self.assertTrue((repo / "NOTES.txt").exists())
            status = ld.run_local(["git", "status", "--short"], cwd=repo, timeout=15)
            self.assertIn("A  NOTES.txt", status.stdout)

    def test_promote_includes_ignored_task_file_and_force_adds_it(self):
        """Reproduces the /history Run Again ownership scenario: a new
        test_*.py file is hidden by .gitignore, promote must still copy it,
        warn that it needs force-add, and actually force-add it by default."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid, repo, wt_path = bootstrap_promotable_task(self, tmp_path, gitignore_text="test_*.py\n")
            (wt_path / "test_new_thing.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lc.cmd_promote(promote_args(task_id=tid))
            out = buf.getvalue()
            self.assertEqual(rc, 0, out)
            self.assertIn("ignored file is part of the task result", out)
            self.assertIn("force-add", out)
            self.assertTrue((repo / "test_new_thing.py").exists())
            status = ld.run_local(["git", "status", "--short"], cwd=repo, timeout=15)
            self.assertIn("A  test_new_thing.py", status.stdout)

    def test_inventory_excludes_runtime_junk_but_keeps_ignored_task_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid, repo, wt_path = bootstrap_promotable_task(
                self,
                tmp_path,
                gitignore_text=(
                    "test_*.py\n"
                    "__pycache__/\n"
                    "settings_data/\n"
                    "*.pyc\n"
                ),
            )

            (wt_path / "test_new_thing.py").write_text(
                "def test_x():\n    assert True\n",
                encoding="utf-8",
            )

            (wt_path / "__pycache__").mkdir()
            (wt_path / "__pycache__" / "module.cpython-314.pyc").write_bytes(b"cache")

            (wt_path / "settings_data").mkdir()
            (wt_path / "settings_data" / "runtime.json").write_text(
                "{}",
                encoding="utf-8",
            )

            tracked, untracked, ignored = lc.worktree_change_inventory(wt_path)

        self.assertEqual(tracked, [])
        self.assertEqual(untracked, [])
        self.assertIn("test_new_thing.py", ignored)
        self.assertFalse(any("__pycache__" in path for path in ignored))
        self.assertFalse(any("settings_data" in path for path in ignored))
        self.assertFalse(any(path.endswith(".pyc") for path in ignored))

    def test_promote_no_stage_leaves_ignored_file_unstaged_with_warning(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid, repo, wt_path = bootstrap_promotable_task(self, tmp_path, gitignore_text="test_*.py\n")
            (wt_path / "test_new_thing.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lc.cmd_promote(promote_args(task_id=tid, no_stage=True))
            out = buf.getvalue()
            self.assertEqual(rc, 0, out)
            self.assertIn("were copied but not staged", out)
            self.assertTrue((repo / "test_new_thing.py").exists())
            status = ld.run_local(
                ["git", "status", "--short", "--ignored=matching"], cwd=repo, timeout=15,
            )
            self.assertIn("!! test_new_thing.py", status.stdout)

    def test_promote_leaves_understandable_status_output(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid, repo, wt_path = bootstrap_promotable_task(self, tmp_path)
            (wt_path / "README.md").write_text("hello\nworld\n", encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lc.cmd_promote(promote_args(task_id=tid))
            out = buf.getvalue()
            self.assertEqual(rc, 0, out)
            self.assertIn("PRIMARY REPO STATUS", out)
            self.assertIn("DIFF STAT", out)
            self.assertIn("README.md", out)

    def test_promote_commit_flag_commits_staged_changes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid, repo, wt_path = bootstrap_promotable_task(self, tmp_path)
            (wt_path / "README.md").write_text("hello\nworld\n", encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lc.cmd_promote(promote_args(task_id=tid, commit=True, message="Promote task"))
            out = buf.getvalue()
            self.assertEqual(rc, 0, out)
            self.assertIn("[PASS] committed", out)
            log = ld.run_local(["git", "log", "-1", "--pretty=%s"], cwd=repo, timeout=15)
            self.assertEqual(log.stdout.strip(), "Promote task")
            self.assertTrue(ld.tracked_tree_clean(repo))

    def test_promote_commit_without_message_refuses(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid, repo, wt_path = bootstrap_promotable_task(self, tmp_path)
            (wt_path / "README.md").write_text("hello\nworld\n", encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = lc.cmd_promote(promote_args(task_id=tid, commit=True, message=None))
            self.assertEqual(rc, 1)
            self.assertIn("commit message provided", buf.getvalue())

    def test_promote_does_not_push(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tid, repo, wt_path = bootstrap_promotable_task(self, tmp_path)
            (wt_path / "README.md").write_text("hello\nworld\n", encoding="utf-8")
            with mock.patch.object(ld, "push_origin", side_effect=AssertionError("must not push")):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = lc.cmd_promote(promote_args(task_id=tid, commit=True, message="Promote task"))
                self.assertEqual(rc, 0, buf.getvalue())
                ld.push_origin.assert_not_called()

    def test_promote_source_never_pushes_or_deploys(self):
        """Static check mirroring NoAutoGitFinalizationTests: cmd_promote
        itself must never contain a push or deploy invocation — only the
        "Push: none" / "Deploy: none" report lines."""
        import inspect
        src = inspect.getsource(lc.cmd_promote)
        self.assertNotIn("push_origin", src)
        self.assertNotIn("systemctl", src)
        self.assertNotIn("leadme_deploy.cmd_deploy", src)
        self.assertIn("Push: none", src)
        self.assertIn("Deploy: none", src)


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
        self.assertEqual(parser.parse_args(["promote"]).command, "promote")

    def test_start_accepts_task_file_flag(self):
        parser = lc.build_parser()
        args = parser.parse_args(["start", "--task-file", "task.md"])
        self.assertEqual(args.task_file, "task.md")

    def test_abort_accepts_cleanup_flag(self):
        parser = lc.build_parser()
        args = parser.parse_args(["abort", "some-id", "--cleanup"])
        self.assertTrue(args.cleanup)
        self.assertEqual(args.task_id, "some-id")

    def test_promote_accepts_flags(self):
        parser = lc.build_parser()
        args = parser.parse_args(["promote", "some-id", "--no-stage"])
        self.assertEqual(args.task_id, "some-id")
        self.assertTrue(args.no_stage)
        self.assertFalse(args.commit)

        args2 = parser.parse_args(["promote", "some-id", "--commit", "-m", "msg"])
        self.assertTrue(args2.commit)
        self.assertEqual(args2.message, "msg")



class ClaudeBudgetRecoveryTests(unittest.TestCase):
    def test_run_claude_parses_budget_json_despite_nonzero_exit(self):
        raw = json.dumps({
            "type": "result",
            "subtype": "error_max_budget_usd",
            "is_error": True,
            "total_cost_usd": 1.25,
        })

        monitored = {
            "returncode": 1,
            "stdout": raw,
            "stderr": "",
            "elapsed_seconds": 120.0,
            "timed_out": False,
            "pid": 12345,
        }

        with mock.patch.object(lc, "run_monitored", return_value=monitored):
            result = lc.run_claude(
                "perform a focused task",
                cwd="/tmp",
                timeout=240,
            )

        self.assertTrue(result["is_error"])
        self.assertTrue(result["budget_exhausted"])
        self.assertEqual(result["total_cost_usd"], 1.25)
        self.assertEqual(
            result["parsed"]["subtype"],
            "error_max_budget_usd",
        )
        self.assertIn("budget", result["reason"].lower())

    def test_verified_budget_result_can_continue(self):
        result = {
            "is_error": True,
            "budget_exhausted": True,
        }

        self.assertTrue(
            lc._verified_budget_result_can_continue(
                result,
                ["app/templates/report.html"],
                True,
            )
        )

    def test_budget_result_with_no_changes_fails_closed(self):
        result = {
            "is_error": True,
            "budget_exhausted": True,
        }

        self.assertFalse(
            lc._verified_budget_result_can_continue(
                result,
                [],
                True,
            )
        )

    def test_budget_result_with_failed_verification_fails_closed(self):
        result = {
            "is_error": True,
            "budget_exhausted": True,
        }

        self.assertFalse(
            lc._verified_budget_result_can_continue(
                result,
                ["app/main.py"],
                False,
            )
        )

    def test_non_budget_failure_never_continues(self):
        result = {
            "is_error": True,
            "budget_exhausted": False,
        }

        self.assertFalse(
            lc._verified_budget_result_can_continue(
                result,
                ["app/main.py"],
                True,
            )
        )

if __name__ == "__main__":
    unittest.main()
