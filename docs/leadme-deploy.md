# leadme-deploy

One command that replaces the manual WSL → GitHub → SSH → Droplet → systemd →
smoke-test deploy loop for LeadMeLeads.

- Repo implementation: `scripts/leadme_deploy.py` (stdlib only, no third-party
  dependencies).
- Repo wrapper: `scripts/leadme-deploy` (resolves the right Python, execs the
  module).
- Installed command: `~/.local/bin/leadme-deploy` — a symlink to
  `scripts/leadme-deploy`. Works as long as `~/.local/bin` is on `PATH`.

The Python tool resolves its local repository from the checked-in
`scripts/leadme_deploy.py` file, so invoking a worktree's own
`scripts/leadme-deploy` operates on that worktree. It does not trust an
arbitrary current directory or fall back to the protected primary checkout.
Before any mode runs, it verifies the resolved directory is the Git top-level
and contains the expected LeadMeLeads application, agent, deploy, and
documentation files. A mismatched repository stops before any production
contact or change.

Production target: `root@165.245.238.122:/var/www/leadmeleads`, systemd unit
`leadmeleads`, live site `https://leadmeleads.com`.

## Required production environment variable

The real production `.env` (not committed, not `.env.example`) must contain:

```
APP_COOKIE_SECURE=true
```

Without it, the login session cookie is sent without the `Secure` flag even
though the site is served over HTTPS. Confirm this is set on the droplet
before/after any deploy that touches auth.

## Modes

### `leadme-deploy`

The real deploy. Runs all 9 phases below in order and stops at the first
failure. Only this mode pushes, backs up, pulls, or restarts anything.

### `leadme-deploy --check`

Local-only pre-deploy validation: repo path, branch, tracked tree
cleanliness, untracked-file allowlist, origin divergence, local compile.
No push, no remote connection, no mutation.

### `leadme-deploy --status`

Read-only snapshot: local branch/HEAD, origin/main HEAD, ahead/behind/
diverged, production HEAD via SSH, production service state, and a single
GET to the live site. No mutations (fetches only update local
remote-tracking refs, never the working tree).

### `leadme-deploy --smoke`

GET-only requests against the routes in `SMOKE_ROUTES` (see below). No
deployment, no state-changing requests of any kind.

### `leadme-deploy --doctor`

Diagnoses the whole pipeline: repo/git presence, branch, tree state, SSH key
and connectivity, remote app path/git repo/venv/service, curl availability,
live-site reachability. No mutations.

### `leadme-deploy --dry-run`

Prints the exact plan — every command that phases 1–8 of a real deploy would
run — using live read-only data (current branch, HEAD, divergence) where
available. Never executes a mutating command.

### `leadme-deploy --rollback-info`

Reads the local state file (see below) and the current production commit/
backup listing over SSH, then prints a concrete rollback plan. **Never
executes** any of the printed commands — copy/paste them manually if you
need to roll back.

## The real deploy, phase by phase

1. **Local validation** — confirm repo path and branch `main`; confirm the
   tracked tree is clean (`git diff --quiet` + `git diff --cached --quiet`,
   not a slow full `git status`); classify untracked files (see allowlist
   below); record local HEAD; fetch `origin/main` and compute
   ahead/behind/diverged; compile `app/main.py`,
   `agents/auth_agent.py`, `agents/lead_dashboard_agent.py` with
   `~/.venvs/leadmeleads/bin/python` (falls back to the repo venv, then
   `sys.executable`, if that's missing).
2. **Push** — `git push origin main` only if local is ahead; otherwise
   skipped. Verifies `origin/main` now equals local HEAD
   (`EXPECTED_COMMIT`).
3. **Remote pre-flight** — SSH connectivity; `/var/www/leadmeleads` exists
   and is a git repo; production venv python exists; systemd unit is
   loaded; production tracked tree is clean (stops and reports the modified
   files if not — never overwrites them).
4. **Production backup** — `mkdir -p /var/www/leadmeleads-backups`; copy
   `data/app_auth.db` to a timestamped file; verify the copy's size and
   (when `sha256sum` is available) checksum match the source. If the DB
   exists but the backup can't be verified, deployment stops before
   touching anything else.
5. **Remote deploy** — `git fetch origin` then `git pull --ff-only origin
   main` (never `reset --hard`, never a forced checkout). Verifies the
   deployed HEAD exactly equals `EXPECTED_COMMIT` before continuing.
6. **Remote compile** — same three files, using
   `/var/www/leadmeleads/venv/bin/python`. A failure here stops before the
   service is ever restarted.
7. **Restart** — `systemctl restart leadmeleads`, wait 3s, require
   `systemctl is-active` to report `active`. Recent journal output is
   scanned for tracebacks/import errors/OperationalErrors/startup failures
   and surfaced as a warning (does not fail the deploy by itself — read the
   lines and judge).
8. **Live smoke test** — GET-only checks against:

   | Route | Accepted status codes |
   |---|---|
   | `/` | 200 |
   | `/login` | 200 |
   | `/create-account` | 200 |
   | `/forgot-password` | 200 |
   | `/reset-password?token=leadme-deploy-invalid-token` | 200, 302, 303, 400 |
   | `/compare` | 200, 302, 303 |
   | `/lead-bot` | 200, 302, 303 |
   | `/history` | 302, 303 (must redirect to `/login`) |
   | `/settings` | 200, 302, 303 |

   Auth-gated routes accept a redirect since production may send an
   unauthenticated request to `/login`. `/history` requires login, so an
   anonymous request must redirect to `/login` and a 200 is treated as a
   failure. No forms are submitted, no accounts
   created, no passwords reset, no history deleted, no scans started, no
   settings changed.
9. **Final report** — PASS/FAIL summary, previous/current commit, backup
   path, and a pointer to `--rollback-info`.

## What blocks deployment

- Current branch isn't `main` (never auto-switches).
- Tracked modifications in the local repo (staged or unstaged).
- Local main diverged from `origin/main` (both ahead and behind).
- `git push` fails (credentials/network).
- Local compile failure on any of the three target files.
- SSH unreachable, remote app path missing, remote path not a git repo,
  remote venv python missing, or the systemd unit not loaded.
- **Tracked** modifications on the production checkout (untracked files
  there are reported but do not block — a fast-forward `git pull` doesn't
  care about untracked files).
- Production auth DB exists but its backup can't be created or verified
  (size or checksum mismatch).
- Remote fetch/pull failure, or a fast-forward isn't possible.
- Deployed HEAD doesn't exactly match `EXPECTED_COMMIT`.
- Remote compile failure (service is **not** restarted in this case).
- `systemctl restart` fails, or the service doesn't report `active` after
  the restart wait.

Live smoke-test failures do **not** roll anything back automatically — the
service is left running (it already passed the compile + restart checks) and
the deploy is reported as FAIL so you can investigate before trusting it.

### Known untracked files (do not block)

- `.claude/`
- `CLAUDE.md.backup-*`
- `reset_local_password.py`

Anything else untracked triggers a `[WARN]` with the file list — it never
blocks by itself, and is never silently included in a commit/push.

## Backups

- Location: `/var/www/leadmeleads-backups/app_auth-YYYYMMDD-HHMMSS.db`.
- Verified by comparing file size to the source, and by SHA-256 when
  `sha256sum` is available on the box.
- If `data/app_auth.db` doesn't exist on production at all, backup is
  skipped with a warning (nothing to back up) rather than treated as a
  failure.

## Failure behavior

On any blocking condition the tool stops at that phase and prints:

- the reason it stopped,
- whether production was modified (`YES`/`NO`),
- whether the service was restarted (`YES`/`NO`),
- the backup path if one was created,
- the pre-deploy commit if known,
- a pointer to `leadme-deploy --rollback-info`.

## State file

Written only by a real deploy run, at `~/.local/state/leadme-deploy/state.json`:

```json
{
  "last_deploy_timestamp": "...",
  "previous_production_commit": "...",
  "deployed_commit": "...",
  "backup_path": "...",
  "result": "PASS"
}
```

Never stores passwords, tokens, SSH keys, SMTP secrets, or `.env` contents —
`write_state()` refuses to persist any key/value that looks secret-shaped.

## SSH connection notes

Production has been observed to throttle *new* SSH connections opened in
quick succession (a single `--doctor`/`--check`/deploy run makes several
short SSH calls). `leadme-deploy` uses OpenSSH's `ControlMaster`/
`ControlPersist` (control sockets under `~/.ssh/leadme-deploy-control/`) so
all the calls in one run reuse a single authenticated connection instead of
opening a new one each time. Host key checking uses
`StrictHostKeyChecking=accept-new` (verifies known hosts, auto-trusts on
first contact) — never disabled globally.

## Installing / verifying the command

```
ls -l ~/.local/bin/leadme-deploy   # should be a symlink to scripts/leadme-deploy
which leadme-deploy                # should resolve if ~/.local/bin is on PATH
```

If `~/.local/bin` isn't on `PATH`, the tool will still work when invoked by
full path; add it to `PATH` yourself if you want the bare command — this
tool does not modify `PATH`.
