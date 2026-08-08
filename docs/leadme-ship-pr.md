# leadme-ship.sh

The one-command GitHub PR ship pipeline for LeadMeLeads. Run it from a
committed feature branch:

```
./scripts/leadme-ship.sh
```

It replaces the manual push → open PR → merge PR → delete branch → pull
main → ssh to production → back up DB → deploy → restart → smoke-test loop
with a single command.

- Wrapper: `scripts/leadme-ship.sh` (resolves the right Python, execs the
  module — does no git/gh/ssh work itself).
- Implementation: `scripts/leadme_ship_pr.py`. Reuses
  `leadme_deploy.py`'s low-level primitives (`run_local`/`run_remote`, the
  WSL git.exe/Linux-git routing, the `remote_*()` SSH helpers, curl-based
  smoke tests, `Reporter`) rather than re-implementing them — this module
  only adds GitHub PR orchestration and the production rollout sequence
  built on top of those primitives.

This is a **different workflow** from `scripts/leadme-ship` (no `.sh`
suffix): that older tool commits directly to `main` from a `leadme-collab`
task worktree (no PR). `leadme-ship.sh` assumes you already committed to a
feature branch yourself and ships it through a real GitHub pull request.

## What it does, phase by phase

1. **Preflight** — resolves the repo (shared with `leadme_deploy.py`, so it
   works from an alternate worktree exactly the way `leadme-deploy` does);
   refuses to run on `main`; requires the feature branch's tracked tree to
   be clean; confirms the branch has commits `origin/main` doesn't; runs
   `git diff --check`; confirms `git`, `ssh`, `curl`, and the GitHub CLI
   (`gh` on `PATH`, or `/mnt/c/Program Files/GitHub CLI/gh.exe`) are all
   available.
2. **GitHub auth** — confirms `gh auth status`, then runs
   `gh auth setup-git` (safe, idempotent credential-helper configuration;
   never asks for or prints a token).
3. **Push** — `git push -u origin <branch>`. Already-pushed is fine.
4. **Pull request** — reuses an existing open PR for the branch if one
   exists; otherwise creates one (title from the latest commit subject). A
   PR already `MERGED` for this branch is recognized and treated as "already
   done", not "no PR found".
5. **Merge** — `gh pr merge --merge`; an "already merged" error from GitHub
   is treated as success. Deletes the **remote** branch only (never the
   local one) — idempotent if it's already gone.
6. **Main SHA** — fetches and resolves the exact `origin/main` SHA after
   merge; this is the SHA every later step verifies against, not local
   HEAD (the local checkout may still be on the feature branch).
7. **Production safety check** — SSH reachable; app path exists; is a git
   repo; **on `main`**; tracked tree clean (refuses to overwrite a dirty
   production checkout); venv exists; systemd unit loaded.
8. **Database backup** — timestamped copy of `data/app_auth.db` under
   `/var/www/leadmeleads-backups`, verified by size and (when available)
   SHA-256, before anything else touches production. Skipped only if
   production is already at the target SHA (see rerun safety below).
9. **Deploy** — `git fetch origin main && git checkout main && git pull
   --ff-only origin main`, then verifies the resulting HEAD exactly equals
   the merged main SHA from step 6. Never `reset --hard`, never a forced
   checkout.
10. **Compile** — `python -m compileall -q app agents` on production (a
    directory sweep, not a fragile hand-maintained file list).
11. **Restart** — `systemctl restart leadmeleads`, then requires
    `systemctl is-active` to report `active`.
12. **Smoke test** — `curl` to `http://127.0.0.1:8000/` **from production**
    (bypasses nginx/DNS to confirm the app process itself is up), then the
    same GET-only public route checks `leadme_deploy.SMOKE_ROUTES` uses
    (`/`, `/login`, `/compare`, `/lead-bot`, etc.) against
    `https://leadmeleads.com`.
13. **Final report** — a compact PASS/FAIL summary of every stage.

## Safe to re-run at any stage

- Already pushed → continues.
- PR already open → reused, no duplicate created.
- PR already merged → treated as done, remote branch deletion still
  attempted (idempotent either way).
- Remote branch already deleted → treated as success.
- **Production already at the target SHA** → backup, pull, and restart are
  all skipped (nothing to back up or change); the tool still verifies the
  service is active and runs the smoke tests before reporting success.

## What stops it

Dirty production, a dirty feature branch, no new commits to ship, a failed
merge, a failed backup, a post-pull SHA mismatch, a compile failure, a
failed restart, or a failed smoke check all stop the run and print
`SHIP FAILED: <reason>` plus a concrete next action. Nothing here
force-pushes, resets, or touches the production DB except to make the
verified backup.
