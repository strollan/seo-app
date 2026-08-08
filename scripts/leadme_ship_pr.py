#!/usr/bin/env python3
"""
leadme-ship-pr — the one-command GitHub PR ship pipeline for LeadMeLeads.

Replaces the manual push -> open PR -> merge PR -> delete branch -> pull main
-> ssh to production -> back up DB -> deploy -> restart -> smoke-test loop
with a single command run from a committed feature branch:

    ./scripts/leadme-ship.sh

Design notes:
- Never commits on the user's behalf. The feature branch must already be
  clean and committed before this tool runs (see cmd_ship phase "preflight").
- Reuses leadme_deploy.py's low-level primitives (run_local/run_remote, the
  WSL git.exe/Linux git routing, remote_*() SSH helpers, curl-based smoke
  tests, Reporter) instead of re-implementing them — this module only adds
  the GitHub PR orchestration (push/PR/merge/branch-delete) and the
  production rollout sequence built on top of those primitives, targeting
  the merged origin/main SHA rather than a locally-pushed HEAD.
- Safe to run more than once at any stage: already-pushed, already-open-PR,
  already-merged-PR, already-deleted-branch, and already-deployed-SHA are
  all treated as "continue", never as duplicate actions or hard failures.
- Never force-pushes, never resets, never deletes the local branch, never
  touches the production DB except to make the verified backup.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import leadme_deploy as ld  # noqa: E402 (reused: run_local/run_remote, remote_* SSH helpers, smoke tests, Reporter)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REPO_PATH = ld.REPO_PATH
PRIMARY_BRANCH = ld.PRIMARY_BRANCH

PROD_APP_PATH = ld.PROD_APP_PATH
PROD_BACKUPS_DIR = ld.PROD_BACKUPS_DIR
PROD_DB_PATH = ld.PROD_DB_PATH
PROD_VENV_PYTHON = ld.PROD_VENV_PYTHON
PROD_SERVICE = ld.PROD_SERVICE
LIVE_SITE = ld.LIVE_SITE

LOCAL_APP_URL = "http://127.0.0.1:8000/"
RESTART_WAIT_SECONDS = ld.RESTART_WAIT_SECONDS

# Local Python code compiled remotely with `compileall` (a directory listing,
# not a fragile hand-maintained file list — see leadme_deploy.COMPILE_TARGETS
# for the older per-file approach this deliberately avoids).
COMPILE_DIRS = ["app", "agents"]

GH_CANDIDATES = [
    "gh",
    "/mnt/c/Program Files/GitHub CLI/gh.exe",
]

Reporter = ld.Reporter


# ---------------------------------------------------------------------------
# gh CLI discovery
# ---------------------------------------------------------------------------

def find_gh_binary():
    """Return a usable `gh` binary path, or None if not found anywhere."""
    on_path = shutil.which("gh")
    if on_path:
        return on_path
    for candidate in GH_CANDIDATES:
        if candidate == "gh":
            continue
        if Path(candidate).exists():
            return candidate
    return None


def gh(gh_bin, args, timeout=30):
    return ld.run_local([gh_bin] + args, cwd=REPO_PATH, timeout=timeout)


def gh_json(gh_bin, args, timeout=30):
    """Run a gh command expected to print JSON; return (data_or_None, res)."""
    res = gh(gh_bin, args, timeout=timeout)
    if not res.ok:
        return None, res
    try:
        return json.loads(res.stdout), res
    except (ValueError, TypeError):
        return None, res


# ---------------------------------------------------------------------------
# git helpers local to this module (thin wrappers, no logic duplication)
# ---------------------------------------------------------------------------

def git(args, timeout=30):
    return ld.run_local(["git"] + args, cwd=REPO_PATH, timeout=timeout)


def commits_ahead_of_origin_main(branch):
    res = git(["rev-list", "--count", f"origin/{PRIMARY_BRANCH}..{branch}"], timeout=15)
    if not res.ok:
        return None
    out = res.stdout.strip()
    return int(out) if out.isdigit() else None


def push_feature_branch(branch, timeout=60):
    res = git(["push", "-u", "origin", branch], timeout=timeout)
    return res.ok, (res.stdout + res.stderr).strip()


def delete_remote_branch(branch, timeout=30):
    """Idempotent: a branch that's already gone is treated as success."""
    res = git(["push", "origin", "--delete", branch], timeout=timeout)
    if res.ok:
        return True, "deleted"
    detail = (res.stdout + res.stderr).strip()
    if "remote ref does not exist" in detail or "not found" in detail.lower():
        return True, "already deleted"
    return False, detail


def latest_commit_subject():
    res = git(["log", "-1", "--pretty=%s"], timeout=15)
    return res.stdout.strip() if res.ok else None


# ---------------------------------------------------------------------------
# GitHub PR helpers
# ---------------------------------------------------------------------------

def find_pr_for_branch(gh_bin, branch, owner_repo):
    """Return the most relevant PR dict for `branch` -> main, or None.

    Prefers an OPEN PR; falls back to the most recently updated MERGED PR so
    a re-run after a successful merge is recognized rather than treated as
    "no PR found".
    """
    data, res = gh_json(
        gh_bin,
        [
            "pr", "list",
            "--repo", owner_repo,
            "--head", branch,
            "--base", PRIMARY_BRANCH,
            "--state", "all",
            "--json", "number,url,state,mergedAt,mergeCommit",
            "--limit", "10",
        ],
    )
    if data is None:
        return None, res
    if not data:
        return None, res
    open_prs = [p for p in data if p.get("state") == "OPEN"]
    if open_prs:
        return open_prs[0], res
    merged_prs = [p for p in data if p.get("state") == "MERGED"]
    if merged_prs:
        return merged_prs[0], res
    return data[0], res


def create_pr(gh_bin, branch, owner_repo, title):
    res = gh(
        gh_bin,
        [
            "pr", "create",
            "--repo", owner_repo,
            "--head", branch,
            "--base", PRIMARY_BRANCH,
            "--title", title,
            "--body", f"Automated PR from leadme-ship.sh for `{branch}`.",
        ],
        timeout=60,
    )
    return res.ok, (res.stdout + res.stderr).strip()


def merge_pr(gh_bin, pr_number, owner_repo):
    res = gh(gh_bin, ["pr", "merge", str(pr_number), "--repo", owner_repo, "--merge"], timeout=90)
    detail = (res.stdout + res.stderr).strip()
    if res.ok:
        return True, detail
    if "already merged" in detail.lower() or "pull request is not open" in detail.lower():
        return True, "already merged: " + detail
    return False, detail


def owner_repo_from_url(url):
    m = re.search(r"github\.com[:/]([^/]+)/([^/.]+?)(?:\.git)?/?$", url or "")
    if not m:
        return None
    return f"{m.group(1)}/{m.group(2)}"


# ---------------------------------------------------------------------------
# Production DB backup (mirrors leadme_deploy.cmd_deploy phase 4, calling
# the same run_remote/q primitives rather than a new implementation)
# ---------------------------------------------------------------------------

def backup_production_db(r):
    """Returns (ok, backup_path_or_None)."""
    from datetime import datetime

    db_size = ld.remote_db_size(PROD_DB_PATH)
    if db_size is None:
        r.warn("no production auth DB found to back up", PROD_DB_PATH)
        return True, None

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = f"{PROD_BACKUPS_DIR}/app_auth-{ts}.db"

    mkdir_res = ld.run_remote(f"mkdir -p {ld.q(PROD_BACKUPS_DIR)}")
    if not r.step("backup directory ready", mkdir_res.ok, mkdir_res.stderr.strip()):
        return False, None

    cp_res = ld.run_remote(f"cp -p {ld.q(PROD_DB_PATH)} {ld.q(backup_path)}")
    if not r.step("db backup copied", cp_res.ok, cp_res.stderr.strip()):
        return False, None

    backup_size = ld.remote_db_size(backup_path)
    if not r.step("backup size matches source", backup_size == db_size,
                  f"source={db_size} backup={backup_size}"):
        return False, None

    src_hash = ld.remote_sha256(PROD_DB_PATH)
    backup_hash = ld.remote_sha256(backup_path)
    if src_hash and backup_hash:
        if not r.step("backup sha256 matches source", src_hash == backup_hash):
            return False, None
    else:
        r.warn("sha256 verification skipped", "sha256sum unavailable on remote")

    r.note(f"  backup: {backup_path}")
    return True, backup_path


# ---------------------------------------------------------------------------
# Production compile / restart / smoke
# ---------------------------------------------------------------------------

def remote_compileall(dirs=None):
    dirs = dirs if dirs is not None else COMPILE_DIRS
    targets = " ".join(ld.q(d) for d in dirs)
    res = ld.run_remote(f"cd {ld.q(PROD_APP_PATH)} && {ld.q(PROD_VENV_PYTHON)} -m compileall -q {targets} 2>&1")
    return res.ok, res.stdout.strip()


def local_smoke_on_production():
    """curl 127.0.0.1:8000 FROM the production host — bypasses nginx/DNS to
    confirm the app process itself (not just the proxy) is responding."""
    res = ld.run_remote(
        f"curl -s -o /dev/null -w '%{{http_code}}' --max-time {ld.CURL_TIMEOUT} {ld.q(LOCAL_APP_URL)}"
    )
    if not res.ok:
        return None, (res.stderr or "curl failed").strip()
    raw = res.stdout.strip()
    return (int(raw), "") if raw.isdigit() else (None, f"unexpected output: {raw!r}")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _stop(r, ctx, reason, next_action=None):
    r.note()
    r.note(f"SHIP FAILED: {reason}")
    if next_action:
        r.note(f"  next action: {next_action}")
    _final_report(r, ctx)
    print(r.render())
    return 1


def _final_report(r, ctx):
    r.section("final report")
    r.note("=" * 32)
    r.note("LEADMELEADS SHIP" + (" COMPLETE" if ctx["final"] == "PASS" else " FAILED"))
    r.note("=" * 32)
    r.note(f"Branch pushed:          {_mark(ctx['pushed'])}")
    r.note(f"PR merged:              {_mark(ctx['pr_merged'])} {ctx['pr_url'] or ''}".rstrip())
    r.note(f"Remote branch deleted:  {_mark(ctx['branch_deleted'])}")
    r.note(f"DB backup:              {_mark(ctx['backup_path'] is not None)} {ctx['backup_path'] or ''}".rstrip())
    r.note(f"Production SHA:         {ctx['production_sha'] or '(unknown)'}")
    r.note(f"Compile:                {_mark(ctx['compile_ok'])}")
    r.note(f"Service:                {ctx['service_state'] or 'UNKNOWN'}")
    r.note(f"Local smoke:            {ctx['local_smoke'] if ctx['local_smoke'] is not None else '(not run)'}")
    r.note(f"Public smoke:           {ctx['public_smoke'] if ctx['public_smoke'] is not None else '(not run)'}")
    r.note("=" * 32)


def _mark(ok):
    return "PASS" if ok else "FAIL"


# ---------------------------------------------------------------------------
# ship
# ---------------------------------------------------------------------------

def cmd_ship(args):
    r = Reporter()
    r.title("LeadMeLeads Ship (PR flow)")

    ctx = {
        "pushed": False,
        "pr_merged": False,
        "pr_url": None,
        "branch_deleted": False,
        "backup_path": None,
        "production_sha": None,
        "compile_ok": False,
        "service_state": None,
        "local_smoke": None,
        "public_smoke": None,
        "final": "FAIL",
    }

    # --- Preflight ------------------------------------------------------------
    r.section("preflight")

    if not r.step("repo exists", ld.repo_exists(REPO_PATH), str(REPO_PATH)):
        return _stop(r, ctx, "primary repo missing", "check repo path")

    branch = ld.current_branch(REPO_PATH)
    if not r.step("not on main", branch is not None and branch != PRIMARY_BRANCH, branch or "unknown"):
        return _stop(r, ctx, "refusing to ship directly from main", "checkout a feature branch")

    clean = ld.tracked_tree_clean(REPO_PATH)
    if clean is None:
        r.step("feature branch tracked tree clean", False, "could not determine (git error)")
        return _stop(r, ctx, "could not determine tracked working-tree state", "run `git status` manually")
    if not r.step("feature branch tracked tree clean", clean):
        return _stop(r, ctx, "unexpected tracked modifications on feature branch",
                     "git status --short ; git diff --stat")

    known, unexpected = ld.classify_untracked(REPO_PATH)
    if known:
        r.note(f"  known untracked (ignored): {', '.join(known)}")
    if unexpected:
        r.warn("unexpected untracked files present (not blocking)", ", ".join(unexpected))

    fetch_ok, fetch_err = ld.fetch_origin(REPO_PATH, PRIMARY_BRANCH)
    if not r.step(f"fetch origin/{PRIMARY_BRANCH}", fetch_ok, fetch_err if not fetch_ok else ""):
        return _stop(r, ctx, "could not fetch origin/main", "check network/credentials, then retry")

    ahead = commits_ahead_of_origin_main(branch)
    if not r.step("branch contains commits not on origin/main", ahead is not None and ahead > 0,
                  f"ahead={ahead}" if ahead is not None else "could not determine"):
        return _stop(r, ctx, f"'{branch}' has no commits ahead of origin/{PRIMARY_BRANCH}",
                     "commit your work, then re-run")

    diff_check = git(["diff", "--check"], timeout=30)
    if not r.step("git diff --check", diff_check.ok, (diff_check.stdout + diff_check.stderr).strip() if not diff_check.ok else ""):
        return _stop(r, ctx, "git diff --check found whitespace/conflict-marker issues", "fix and re-run")

    for cmd_name in ("git", "ssh", "curl"):
        available = shutil.which(cmd_name) is not None
        if not r.step(f"{cmd_name} available", available):
            return _stop(r, ctx, f"required command not found: {cmd_name}", f"install {cmd_name}")

    gh_bin = find_gh_binary()
    if not r.step("GitHub CLI available", gh_bin is not None,
                  gh_bin or "checked: gh on PATH, /mnt/c/Program Files/GitHub CLI/gh.exe"):
        return _stop(r, ctx, "GitHub CLI not found", "install gh, or ensure gh.exe is reachable")
    r.note(f"  gh: {gh_bin}")

    if r.failed:
        return _stop(r, ctx, "preflight failed", "see [FAIL] lines above")

    # --- GitHub auth ------------------------------------------------------------
    r.section("github auth")
    auth_res = gh(gh_bin, ["auth", "status"], timeout=20)
    if not r.step("gh authenticated", auth_res.ok, "run: gh auth login" if not auth_res.ok else ""):
        return _stop(r, ctx, "GitHub CLI is not authenticated", "gh auth login")

    setup_res = gh(gh_bin, ["auth", "setup-git"], timeout=20)
    r.step("gh auth setup-git", setup_res.ok, (setup_res.stdout + setup_res.stderr).strip() if not setup_res.ok else "")

    remote_url = ld.remote_origin_url(REPO_PATH)
    owner_repo = owner_repo_from_url(remote_url)
    if not r.step("origin remote resolves to owner/repo", owner_repo is not None, remote_url or "(none)"):
        return _stop(r, ctx, "could not determine owner/repo from origin remote", "check `git remote -v`")

    # --- Push ---------------------------------------------------------------
    r.section("push")
    push_ok, push_detail = push_feature_branch(branch)
    if push_ok or "up-to-date" in push_detail.lower() or "up to date" in push_detail.lower():
        ctx["pushed"] = True
        r.step(f"pushed {branch}", True)
    else:
        r.step(f"push origin {branch}", False, push_detail)
        return _stop(r, ctx, "git push failed", "resolve the push failure, then re-run")

    # --- Pull request ---------------------------------------------------------
    r.section("pull request")
    pr, pr_res = find_pr_for_branch(gh_bin, branch, owner_repo)
    if pr is None and pr_res is not None and not pr_res.ok:
        r.step("look up existing PR", False, (pr_res.stdout + pr_res.stderr).strip())
        return _stop(r, ctx, "could not query GitHub for an existing PR", "check gh auth / network")

    if pr is None:
        title = latest_commit_subject() or branch
        create_ok, create_detail = create_pr(gh_bin, branch, owner_repo, title)
        if not r.step("PR created", create_ok, create_detail):
            return _stop(r, ctx, "gh pr create failed", "inspect the error above")
        pr, pr_res = find_pr_for_branch(gh_bin, branch, owner_repo)
        if pr is None:
            return _stop(r, ctx, "PR created but could not be looked up afterward", "gh pr list --head " + branch)
    else:
        r.step("existing PR reused", True, pr.get("url", ""))

    ctx["pr_url"] = pr.get("url")
    pr_number = pr.get("number")
    already_merged = pr.get("state") == "MERGED"

    # --- Merge ------------------------------------------------------------------
    r.section("merge")
    if already_merged:
        r.step("PR already merged", True, ctx["pr_url"] or "")
        ctx["pr_merged"] = True
    else:
        merge_ok, merge_detail = merge_pr(gh_bin, pr_number, owner_repo)
        if not r.step("PR merged", merge_ok, merge_detail if not merge_ok else ""):
            return _stop(r, ctx, "gh pr merge failed", "resolve conflicts/checks on the PR, then re-run")
        ctx["pr_merged"] = True

    del_ok, del_detail = delete_remote_branch(branch)
    r.step("remote branch deleted", del_ok, del_detail)
    ctx["branch_deleted"] = del_ok

    # --- Exact main SHA ---------------------------------------------------------
    r.section("main sha")
    fetch_ok, fetch_err = ld.fetch_origin(REPO_PATH, PRIMARY_BRANCH)
    if not r.step(f"fetch origin/{PRIMARY_BRANCH}", fetch_ok, fetch_err if not fetch_ok else ""):
        return _stop(r, ctx, "could not fetch origin/main after merge", "check network/credentials")
    main_sha = ld.origin_branch_sha(REPO_PATH, PRIMARY_BRANCH)
    if not r.step("origin/main SHA resolved", ld.is_valid_sha(main_sha), main_sha or "unknown"):
        return _stop(r, ctx, "could not resolve origin/main SHA after merge", "git fetch origin main")
    r.note(f"  origin/main -> {main_sha}")

    if args.no_deploy:
        ctx["final"] = "PASS"
        r.note()
        r.note("--no-deploy given: PR merged, not deploying")
        _final_report(r, ctx)
        print(r.render())
        return 0

    return _deploy(r, ctx, main_sha)


def _deploy(r, ctx, expected_sha):
    """Phases 7-13: production safety, backup, deploy, restart, smoke."""

    # --- Production safety check --------------------------------------------------
    r.section("production safety check")
    hostname, hostname_res = ld.remote_hostname()
    if not r.step("ssh connectivity", hostname is not None, hostname or hostname_res.stderr.strip()):
        return _stop(r, ctx, "SSH connectivity to production failed", "check SSH key/network")

    if not r.step("production app path exists", ld.remote_path_exists(PROD_APP_PATH), PROD_APP_PATH):
        return _stop(r, ctx, f"{PROD_APP_PATH} missing on production", "inspect production manually")

    if not r.step("production repo valid", ld.remote_is_git_repo(PROD_APP_PATH)):
        return _stop(r, ctx, f"{PROD_APP_PATH} is not a git repository", "inspect production manually")

    prod_branch = ld.remote_branch(PROD_APP_PATH)
    if not r.step("production on main", prod_branch == PRIMARY_BRANCH, prod_branch or "unknown"):
        return _stop(r, ctx, f"production is on branch '{prod_branch}', not '{PRIMARY_BRANCH}'",
                     "checkout main on production manually")

    remote_clean = ld.remote_tracked_clean(PROD_APP_PATH)
    if remote_clean is None:
        r.step("production tracked tree clean", False, "could not determine (ssh/git error)")
        return _stop(r, ctx, "could not determine production tracked working-tree state", "inspect production manually")
    if not r.step("production tracked tree clean", remote_clean):
        modified = ld.remote_modified_tracked_files(PROD_APP_PATH)
        r.note(f"  modified tracked files on production: {', '.join(modified) if modified else 'unknown'}")
        return _stop(r, ctx, "unexpected tracked modifications on production — refusing to overwrite", "inspect/clean production manually")

    if not r.step("production venv exists", ld.remote_venv_python_exists(PROD_VENV_PYTHON), PROD_VENV_PYTHON):
        return _stop(r, ctx, "production venv missing", "inspect production manually")

    svc_state = ld.remote_service_state(PROD_SERVICE)
    if not r.step("production service exists", svc_state.get("LoadState") == "loaded",
                  f"LoadState={svc_state.get('LoadState', '?')}"):
        return _stop(r, ctx, f"systemd service '{PROD_SERVICE}' not found", "inspect production manually")

    pre_deploy_sha = ld.remote_head_sha(PROD_APP_PATH)
    r.note(f"  production HEAD before deploy: {pre_deploy_sha}")

    already_deployed = pre_deploy_sha == expected_sha
    if already_deployed:
        r.note(f"  production is already at target SHA ({expected_sha}) — skipping backup/pull, verifying only")

    # --- Database backup ----------------------------------------------------------
    if not already_deployed:
        r.section("database backup")
        backup_ok, backup_path = backup_production_db(r)
        ctx["backup_path"] = backup_path
        if not backup_ok:
            return _stop(r, ctx, "production DB backup failed — stopping before deploy", "inspect production manually")

    # --- Deploy ---------------------------------------------------------------------
    if not already_deployed:
        r.section("deploy")
        fetch_res = ld.run_remote(f"cd {ld.q(PROD_APP_PATH)} && git fetch origin {ld.q(PRIMARY_BRANCH)} 2>&1")
        if not r.step("remote fetch origin main", fetch_res.ok, fetch_res.stdout.strip() if not fetch_res.ok else ""):
            return _stop(r, ctx, "remote git fetch failed", "inspect production manually")

        checkout_res = ld.run_remote(f"cd {ld.q(PROD_APP_PATH)} && git checkout {ld.q(PRIMARY_BRANCH)} 2>&1")
        if not r.step("remote checkout main", checkout_res.ok, checkout_res.stdout.strip() if not checkout_res.ok else ""):
            return _stop(r, ctx, "remote git checkout main failed", "inspect production manually")

        pull_res = ld.run_remote(f"cd {ld.q(PROD_APP_PATH)} && git pull --ff-only origin {ld.q(PRIMARY_BRANCH)} 2>&1")
        if not r.step("remote pull --ff-only", pull_res.ok, pull_res.stdout.strip() if not pull_res.ok else ""):
            return _stop(r, ctx, "remote fast-forward pull failed (non-ff or conflict)", "inspect production manually")

        deployed_sha = ld.remote_head_sha(PROD_APP_PATH)
        ctx["production_sha"] = deployed_sha
        if not r.step("production HEAD matches merged main SHA", deployed_sha == expected_sha,
                      f"deployed={deployed_sha} expected={expected_sha}"):
            return _stop(r, ctx, "production HEAD does not match the merged main SHA — not restarting", "inspect production manually")
    else:
        ctx["production_sha"] = pre_deploy_sha

    # --- Validation (compile) --------------------------------------------------------
    r.section("compile")
    compile_ok, compile_detail = remote_compileall()
    ctx["compile_ok"] = compile_ok
    if not r.step("compileall app agents", compile_ok, compile_detail if not compile_ok else ""):
        return _stop(r, ctx, "remote compile failed — service NOT restarted", "inspect production manually")

    # --- Restart ----------------------------------------------------------------------
    if not already_deployed:
        r.section("restart")
        restart_res = ld.run_remote(f"systemctl restart {ld.q(PROD_SERVICE)} 2>&1")
        if not r.step("service restart command", restart_res.ok, restart_res.stdout.strip() if not restart_res.ok else ""):
            return _stop(r, ctx, "systemctl restart failed", "inspect production manually")

        time.sleep(RESTART_WAIT_SECONDS)

    active_res = ld.run_remote(f"systemctl is-active {ld.q(PROD_SERVICE)} 2>&1")
    is_active = active_res.stdout.strip() == "active"
    ctx["service_state"] = active_res.stdout.strip().upper() if active_res.stdout.strip() else "UNKNOWN"
    if not r.step("service active", is_active, active_res.stdout.strip()):
        return _stop(r, ctx, "service is not active", "inspect production manually: journalctl -u " + PROD_SERVICE)

    # --- Smoke test -----------------------------------------------------------------------
    r.section("smoke test")
    local_status, local_detail = local_smoke_on_production()
    ctx["local_smoke"] = local_status
    if not r.step(f"production loopback {LOCAL_APP_URL}", local_status == 200, str(local_status) if local_status else local_detail):
        return _stop(r, ctx, "production loopback smoke check failed", "inspect production manually: journalctl -u " + PROD_SERVICE)

    smoke_results = ld.run_smoke_tests(base_url=LIVE_SITE, reporter=r)
    homepage_status = next((status for path, status, ok, detail in smoke_results if path == "/"), None)
    ctx["public_smoke"] = homepage_status
    smoke_failed = any(not ok for _, _, ok, _ in smoke_results)
    if smoke_failed:
        return _stop(r, ctx, "public smoke test failed", "service is running — inspect before trusting it")

    # --- Final report ----------------------------------------------------------------------
    ctx["final"] = "PASS"
    _final_report(r, ctx)
    print(r.render())
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="leadme-ship-pr",
        description="Push, PR, merge, and deploy the current feature branch to production — one command.",
    )
    parser.add_argument("--no-deploy", action="store_true", help="push, PR, merge — do not deploy to production")
    return parser


def main(argv=None):
    global REPO_PATH
    parser = build_parser()
    args = parser.parse_args(argv)
    rc = cmd_ship(args)
    return rc


if __name__ == "__main__":
    sys.exit(main())
