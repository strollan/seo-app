#!/usr/bin/env python3
"""
leadme-ship — the explicit "I approve this result, do the rest" button.

Chains leadme-collab's promote step and leadme-deploy's real deploy step
into one command, with tests and a commit in between:

    promote -> stage -> test -> commit -> push -> deploy

Design notes:
- This is the ONLY tool in the LeadMeLeads automation that is allowed to
  commit, push, and deploy — and it only ever does so because a human typed
  `leadme-ship <task-id> -m "..."` themselves. Nothing in leadme-collab
  auto-chains into this; `leadme-collab start` and Codex PASS never trigger
  a ship on their own. See docs/leadme-ship.md.
- Reuses leadme_collab.cmd_promote() and leadme_deploy.cmd_deploy()
  (imported, not duplicated or re-implemented) so promote/deploy behave
  identically whether run standalone or via ship.
- Every phase that can fail stops the pipeline before the next phase runs.
  A test failure never reaches commit; a commit failure never reaches push;
  a push failure never reaches deploy unless origin/main is independently
  verified to already contain local HEAD.
"""

from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import leadme_deploy as ld  # noqa: E402  (reused: run_local, git/push helpers, cmd_deploy)
import leadme_collab as lc  # noqa: E402  (reused: cmd_promote, task state helpers)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REPO_PATH = ld.REPO_PATH
PRIMARY_BRANCH = ld.PRIMARY_BRANCH

SCRIPTS_DIR = Path(__file__).resolve().parent
COLLAB_WRAPPER = SCRIPTS_DIR / "leadme-collab"
DEPLOY_WRAPPER = SCRIPTS_DIR / "leadme-deploy"

DEFAULT_TEST_MODULES = ["scripts.test_leadme_deploy", "scripts.test_leadme_collab"]
TEST_TIMEOUT_SECONDS = 300

Reporter = lc.Reporter


# ---------------------------------------------------------------------------
# Small helpers — every one takes/uses REPO_PATH by name (not as a bound
# default argument) so tests can redirect it with mock.patch.object(ls,
# "REPO_PATH", tmp_repo), same pattern leadme_collab.py uses against
# leadme_deploy's helpers.
# ---------------------------------------------------------------------------

def git(args, timeout=30):
    return ld.run_local(["git"] + args, cwd=REPO_PATH, timeout=timeout)


def staged_files():
    res = git(["diff", "--cached", "--name-only"], timeout=15)
    if not res.ok:
        return []
    return [line for line in res.stdout.splitlines() if line.strip()]


def promoted_test_modules(paths):
    """scripts/test_foo.py -> scripts.test_foo — restricted to scripts/test_*.py
    so a newly promoted focused test file (even one force-added past a
    .gitignore rule) gets exercised, not silently skipped."""
    modules = []
    for path in paths:
        p = Path(path)
        if p.parent.as_posix() == "scripts" and p.name.startswith("test_") and p.suffix == ".py":
            module = "scripts." + p.stem
            if module not in DEFAULT_TEST_MODULES and module not in modules:
                modules.append(module)
    return modules


def build_test_argv(test_command, extra_modules):
    if test_command:
        return shlex.split(test_command)
    modules = list(DEFAULT_TEST_MODULES)
    for m in extra_modules:
        if m not in modules:
            modules.append(m)
    return [sys.executable, "-m", "unittest"] + modules


def run_promote(tid):
    """Delegates to the real leadme-collab promote command (stages by
    default, never commits) so ship's promote behavior is exactly
    leadme-collab promote's behavior — no parallel re-implementation."""
    return lc.cmd_promote(argparse.Namespace(task_id=tid, no_stage=False, commit=False, message=None))


def run_deploy():
    """Delegates to the real leadme-deploy default mode. Returns
    (returncode, backup_path) — bundling the state-file read here means
    tests only need to mock this one seam, not a separate ld.read_state()
    call against the real on-disk state file."""
    rc = ld.cmd_deploy(argparse.Namespace())
    backup_path = ld.read_state().get("backup_path") or None
    return rc, backup_path


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------

def _stop(r, reason, next_action=None):
    r.note()
    r.note(f"STOPPED: {reason}")
    if next_action:
        r.note(f"  next action: {next_action}")
    return 1


def _final_report(r, ctx):
    r.section("final report")
    r.note(f"  task id:        {ctx['task_id']}")
    r.note(f"  promoted files: {len(ctx['promoted_files'])}")
    for f in ctx["promoted_files"]:
        r.note(f"    {f}")
    r.note(f"  commit:         {ctx['commit_hash'] or '(none)'}")
    r.note(f"  push:           {ctx['push_result']}")
    r.note(f"  deploy:         {ctx['deploy_result']}")
    r.note(f"  db backup:      {ctx['backup_path'] or '(none)'}")
    r.note()
    r.note(f"RESULT: {ctx['final']}")
    r.note()
    r.note("Rollback info: leadme-deploy --rollback-info")


# ---------------------------------------------------------------------------
# ship
# ---------------------------------------------------------------------------

def cmd_ship(args):
    r = Reporter()
    r.title("LeadMe Ship")

    ctx = {
        "task_id": args.task_id,
        "promoted_files": [],
        "commit_hash": None,
        "push_result": "not attempted",
        "deploy_result": "not attempted",
        "backup_path": None,
        "final": "FAIL",
    }

    # --- Preflight ------------------------------------------------------------
    r.section("preflight")

    if not r.step("repo exists", ld.repo_exists(REPO_PATH), str(REPO_PATH)):
        return _stop(r, "primary repo missing", "check REPO_PATH configuration")

    branch = ld.current_branch(REPO_PATH)
    if not r.step("primary repo on main", branch == PRIMARY_BRANCH, branch or "unknown"):
        return _stop(r, f"current branch is '{branch}', not '{PRIMARY_BRANCH}'", "git checkout main")

    clean = ld.tracked_tree_clean(REPO_PATH)
    if clean is None:
        r.step("primary tracked tree clean", False, "could not determine (git error)")
        return _stop(r, "could not determine tracked working-tree state", "run `git status` manually")
    if not r.step("primary tracked tree clean", clean):
        return _stop(r, "unexpected tracked modifications in local repo",
                     "git status --short ; git diff --stat")

    known, unexpected = ld.classify_untracked(REPO_PATH)
    if known:
        r.note(f"  known untracked (ignored): {', '.join(known)}")
    if unexpected:
        r.warn("unexpected untracked files present (not blocking)", ", ".join(unexpected))

    fetch_ok, fetch_err = ld.fetch_origin(REPO_PATH, PRIMARY_BRANCH)
    if not r.step("fetch origin/main", fetch_ok, fetch_err if not fetch_ok else ""):
        return _stop(r, "could not fetch origin/main", "check network/credentials, then retry")

    tid = args.task_id
    if not r.step("task id provided", bool(tid), 'usage: leadme-ship <task-id> -m "message"'):
        return _stop(r, "no task id given", 'leadme-ship <task-id> -m "message"')

    state = lc.read_task_state(tid)
    if not r.step("task exists", state is not None, f"no state.json for {tid}"):
        return _stop(r, f"unknown task id: {tid}", "leadme-collab list")

    wt = Path(state["worktree"])
    if not r.step("task worktree exists", wt.is_dir(), str(wt)):
        return _stop(r, f"task worktree missing: {wt}", f"leadme-collab inspect {tid}")

    tracked, untracked, ignored = lc.worktree_change_inventory(wt)
    has_changes = bool(tracked or untracked or ignored)
    if not r.step("task has changed files", has_changes, "" if has_changes else "nothing to promote"):
        return _stop(r, f"task {tid} has no changes to ship", f"leadme-collab inspect {tid}")

    r.step("leadme-collab promote command available", COLLAB_WRAPPER.exists(), str(COLLAB_WRAPPER))
    if not args.no_deploy:
        r.step("leadme-deploy command available", DEPLOY_WRAPPER.exists(), str(DEPLOY_WRAPPER))

    if not args.dry_run:
        if not r.step("commit message provided", bool(args.message), 'pass -m "message"'):
            return _stop(r, "no commit message given", 'leadme-ship <task-id> -m "message"')

    if r.failed:
        return _stop(r, "preflight failed", "see [FAIL] lines above")

    # --- Dry run: print the plan, touch nothing ------------------------------
    if args.dry_run:
        r.section("plan (dry run — nothing below was executed)")
        r.note(f"  would run: leadme-collab promote {tid}")
        for status, path in tracked:
            r.note(f"    tracked   [{status}] {path}")
        for path in untracked:
            r.note(f"    untracked     {path}")
        for path in ignored:
            r.note(f"    ignored       {path} (force-add)")
        predicted_modules = promoted_test_modules(
            [p for _, p in tracked] + untracked + ignored
        )
        argv = build_test_argv(args.test_command, predicted_modules)
        r.note(f"  would run: {' '.join(argv)}")
        r.note("  would run: git diff --check")
        r.note("  would run: git diff --cached --check")
        r.note(f"  would run: git commit -m {args.message!r}" if args.message else
               "  would run: git commit -m <message>  (no -m given yet)")
        if args.no_push:
            r.note("  --no-push given: would stop after commit")
        else:
            r.note(f"  would run: git push origin {PRIMARY_BRANCH}")
            if args.no_deploy:
                r.note("  --no-deploy given: would stop after push")
            else:
                r.note("  would run: leadme-deploy")
        r.note()
        r.note("DRY RUN — no files modified, nothing committed, pushed, or deployed.")
        return 0

    # --- Promote ----------------------------------------------------------------
    r.section("promote")
    promote_rc = run_promote(tid)
    if promote_rc != 0:
        return _stop(
            r, f"leadme-collab promote {tid} failed",
            f"leadme-collab inspect {tid}  (fix the reported issue, then re-run leadme-ship)",
        )

    # --- Staged files -------------------------------------------------------------
    r.section("staged files")
    staged = staged_files()
    if not staged:
        return _stop(r, "nothing staged after promote", f"leadme-collab inspect {tid}")
    ctx["promoted_files"] = staged
    for f in staged:
        r.note(f"  {f}")
    stat_res = git(["diff", "--cached", "--stat"], timeout=30)
    r.note(stat_res.stdout.rstrip())

    # --- Tests ----------------------------------------------------------------------
    r.section("tests")
    extra_modules = promoted_test_modules(staged)
    if extra_modules and args.test_command:
        r.note(f"  note: promoted test file(s) not auto-included in --test-command override: "
               f"{', '.join(extra_modules)}")
    argv = build_test_argv(args.test_command, extra_modules)
    r.note(f"  running: {' '.join(argv)}")
    test_res = ld.run_local(argv, cwd=REPO_PATH, timeout=TEST_TIMEOUT_SECONDS)
    tests_ok = r.step("test suite", test_res.ok, "" if test_res.ok else (test_res.stdout + test_res.stderr).strip()[-4000:])
    if not tests_ok:
        return _stop(
            r, "test suite failed — promoted changes left in place for inspection",
            "git status --short ; git diff --stat ; git diff",
        )

    diff_check = git(["diff", "--check"], timeout=30)
    if not r.step("git diff --check", diff_check.ok, (diff_check.stdout + diff_check.stderr).strip() if not diff_check.ok else ""):
        return _stop(r, "git diff --check found whitespace/conflict-marker issues", "git diff --check ; fix and re-run")

    cached_check = git(["diff", "--cached", "--check"], timeout=30)
    if not r.step("git diff --cached --check", cached_check.ok, (cached_check.stdout + cached_check.stderr).strip() if not cached_check.ok else ""):
        return _stop(r, "git diff --cached --check found whitespace/conflict-marker issues", "git diff --cached --check ; fix and re-run")

    # --- Commit -----------------------------------------------------------------------
    r.section("commit")
    commit_res = git(["commit", "-m", args.message], timeout=30)
    if not r.step("committed", commit_res.ok, (commit_res.stdout + commit_res.stderr).strip() if not commit_res.ok else ""):
        return _stop(
            r, "git commit failed",
            "git log --oneline -3 ; git status --short ; git branch -vv",
        )
    ctx["commit_hash"] = ld.head_sha(REPO_PATH)
    r.note(f"  commit: {ctx['commit_hash']}")

    # --- Push -------------------------------------------------------------------------
    if args.no_push:
        ctx["push_result"] = "skipped (--no-push)"
        ctx["deploy_result"] = "skipped (--no-push)"
        r.section("push")
        r.note("  --no-push given: commit only, not pushing")
        ctx["final"] = "PASS"
        _final_report(r, ctx)
        return 0

    r.section("push")
    push_ok, push_detail = ld.push_origin(repo=REPO_PATH, branch=PRIMARY_BRANCH)
    if push_ok:
        ctx["push_result"] = "pushed"
        r.step(f"pushed {ctx['commit_hash'][:7]}", True)
    else:
        r.step(f"push origin {PRIMARY_BRANCH}", False, push_detail)
        fetch_ok2, _ = ld.fetch_origin(REPO_PATH, PRIMARY_BRANCH)
        origin_sha = ld.origin_branch_sha(repo=REPO_PATH, branch=PRIMARY_BRANCH) if fetch_ok2 else None
        if origin_sha and origin_sha == ctx["commit_hash"]:
            r.warn(
                "push reported failure but origin/main already contains local HEAD",
                f"origin={origin_sha} local={ctx['commit_hash']} — continuing",
            )
            ctx["push_result"] = "already on origin (push call failed but verified up to date)"
        else:
            ctx["push_result"] = f"failed: {push_detail}"
            ctx["deploy_result"] = "skipped (push failed, origin does not contain HEAD)"
            r.note("git log --oneline -3:")
            r.note("  " + git(["log", "--oneline", "-3"]).stdout.strip().replace("\n", "\n  "))
            r.note("git status --short:")
            r.note("  " + git(["status", "--short"]).stdout.strip().replace("\n", "\n  "))
            r.note("git branch -vv:")
            r.note("  " + git(["branch", "-vv"]).stdout.strip().replace("\n", "\n  "))
            _final_report(r, ctx)
            return _stop(r, "push failed and origin/main does not contain local HEAD",
                         "resolve the push failure, then re-run: leadme-ship " + tid + f' -m "{args.message}" --no-deploy')

    # --- Deploy -----------------------------------------------------------------------
    if args.no_deploy:
        ctx["deploy_result"] = "skipped (--no-deploy)"
        r.section("deploy")
        r.note("  --no-deploy given: pushed, not deploying")
        ctx["final"] = "PASS"
        _final_report(r, ctx)
        return 0

    r.section("deploy")
    deploy_rc, backup_path = run_deploy()
    ctx["backup_path"] = backup_path
    if deploy_rc == 0:
        ctx["deploy_result"] = "PASS"
        ctx["final"] = "PASS"
        _final_report(r, ctx)
        return 0

    ctx["deploy_result"] = "FAIL"
    ctx["final"] = "FAIL"
    r.note()
    r.note("leadme-deploy --rollback-info:")
    ld.cmd_rollback_info(argparse.Namespace())
    _final_report(r, ctx)
    return 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="leadme-ship",
        description="Promote a reviewed leadme-collab task, test, commit, push, and deploy — the explicit approval button.",
    )
    parser.add_argument("task_id", nargs="?", default=None, help="leadme-collab task id to ship")
    parser.add_argument("-m", "--message", default=None, help="commit message (required unless --dry-run)")
    parser.add_argument("--no-deploy", action="store_true", help="promote, test, commit, push — do not deploy")
    parser.add_argument("--no-push", action="store_true", help="promote, test, commit — do not push or deploy")
    parser.add_argument("--dry-run", action="store_true", help="print what would happen; modify/commit/push/deploy nothing")
    parser.add_argument("--test-command", default=None, help='override test command (default: python -m unittest scripts.test_leadme_deploy scripts.test_leadme_collab)')
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return cmd_ship(args)


if __name__ == "__main__":
    sys.exit(main())
