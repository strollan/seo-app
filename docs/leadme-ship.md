# leadme-ship

The explicit "I approve this result, do the rest" button.

```
leadme-ship <task-id> -m "Commit message"
```

Chains the three already-safe, already-tested steps that used to require
Scot as the button-pusher between them:

```
leadme-collab promote <task-id>   (stage)
   -> run tests
      -> git commit -m "..."
         -> git push origin main
            -> leadme-deploy
```

- Repo implementation: `scripts/leadme_ship.py` (imports `leadme_collab.py`
  and `leadme_deploy.py` directly — it does not shell out to them, and does
  not re-implement their behavior).
- Repo wrapper: `scripts/leadme-ship` (resolves the right Python, same
  pattern as `scripts/leadme-collab` and `scripts/leadme-deploy`).
- Installed command: `~/.local/bin/leadme-ship` — a symlink to
  `scripts/leadme-ship`, same as the other two tools.

## The explicit approval model

`leadme-ship` is the **only** command in this toolset allowed to commit,
push, and deploy — and it only ever does so because a human typed
`leadme-ship <task-id> -m "..."` themselves, with a task id and a commit
message. Specifically, by design:

- `leadme-collab start` never auto-ships. A `PASS` verdict from Codex ends a
  task at `READY FOR HUMAN REVIEW` — it does not trigger promote, commit,
  push, or deploy on its own.
- `leadme-collab promote` (used internally by `leadme-ship`, and still
  usable standalone) never commits, pushes, or deploys by itself either.
- Nothing in `leadme_collab.py` references `leadme_ship` at all — there is
  no code path by which starting or advancing a collab task can reach a
  ship. The dependency only ever runs the other direction: `leadme_ship.py`
  imports `leadme_collab.py` and `leadme_deploy.py`, never the reverse.

If you want promote-only, or promote+push-but-not-deploy, say so explicitly
with `--no-push` / `--no-deploy` (below) — the default is the full chain,
but every step past promote is something you're asking for by running this
specific command.

## Relationship to `leadme-collab promote`

`leadme-ship` calls the exact same `cmd_promote()` that `leadme-collab
promote <task-id>` calls on its own — same preflight, same tracked/
untracked/`.gitignore`-hidden file detection, same force-add-and-warn
behavior for ignored files (e.g. a `test_*.py` file matching a `.gitignore`
rule, as happened with `scripts/test_history_rerun_ownership.py` during the
`/history` Run Again ownership fix — see `docs/leadme-collab.md`). Promotion
always stages by default; `leadme-ship` never passes `--no-stage` to it,
since it needs the staged files to commit.

If you'd rather promote and inspect manually before deciding whether to
ship, just run `leadme-collab promote <task-id>` directly — `leadme-ship` is
for the case where you've already decided to ship and don't want to
re-invoke each step by hand.

## Default flow, phase by phase

1. **Preflight** — primary repo exists, is on `main`, tracked tree is clean
   (known untracked files still non-blocking), `origin/main` is fetchable,
   the task id exists and has a worktree with changes to promote, the
   `leadme-collab`/`leadme-deploy` wrappers exist, and a commit message was
   given (not required for `--dry-run`).
2. **Promote** — `leadme-collab promote <task-id>` (staged by default).
3. **Staged files** — `git diff --cached --name-only` / `--stat` are shown.
   Refuses if nothing ended up staged.
4. **Tests** — by default:
   ```
   python -m unittest scripts.test_leadme_deploy scripts.test_leadme_collab
   ```
   Any staged file matching `scripts/test_*.py` that isn't already one of
   the two defaults (e.g. a new focused test the promoted task added, even
   if it was `.gitignore`-hidden and had to be force-added) is **discovered
   automatically and appended** to the test command — this is the fix for
   the exact gap that made the `/history` promotion manual. Then
   `git diff --check` and `git diff --cached --check` run as a final
   safety net. A `--test-command` override replaces the whole command
   (the auto-discovered files are reported but not force-injected into an
   explicit override, since that would silently change what you asked for).
5. **Commit** — `git commit -m "<message>"`.
6. **Push** — `git push origin main`, unless `--no-push`. If the push call
   itself reports failure, `leadme-ship` independently re-fetches and
   compares `origin/main` to local HEAD before deciding what to do next:
   if they already match, it explains why and continues (a flaky client-side
   push error where the ref actually landed); otherwise it stops before
   deploy and prints `git log --oneline -3`, `git status --short`, and
   `git branch -vv`.
7. **Deploy** — `leadme-deploy` (the real, default deploy mode), unless
   `--no-deploy` or `--no-push`. Uses `leadme-deploy` as the sole source of
   truth for backup, remote compile, restart, logs, and live smoke tests —
   `leadme-ship` does not duplicate any of that logic.
8. **Final report** — task id, promoted files, commit hash, push result,
   deploy result, DB backup path (if `leadme-deploy` recorded one), overall
   `RESULT: PASS`/`FAIL`, and a pointer to `leadme-deploy --rollback-info`.

## Flags

```
leadme-ship 20260710-153458-fix-the-history-run-again-ownership-isol -m "Fix /history Run Again ownership isolation"
leadme-ship 20260710-153458-fix-the-history-run-again-ownership-isol -m "..." --dry-run
leadme-ship 20260710-153458-fix-the-history-run-again-ownership-isol -m "..." --no-push
leadme-ship 20260710-153458-fix-the-history-run-again-ownership-isol -m "..." --no-deploy
leadme-ship 20260710-153458-fix-the-history-run-again-ownership-isol -m "..." --test-command "python3 -m unittest scripts.test_leadme_deploy"
```

- **`--dry-run`** — runs the read-only preflight checks (so you still find
  out if the task/repo state is wrong) and prints exactly what promote,
  tests, commit, push, and deploy *would* do, including the predicted test
  module list. Modifies nothing, stages nothing, commits nothing, pushes
  nothing, deploys nothing. Commit message is optional in this mode.
- **`--no-push`** — promote, test, commit; stop there. Push and deploy are
  both reported as `skipped (--no-push)`.
- **`--no-deploy`** — promote, test, commit, push; stop there. Deploy is
  reported as `skipped (--no-deploy)`.
- **`--test-command "..."`** — override the test command entirely. Runs
  exactly what you pass (via `shlex.split`, no shell interpolation).

## Failure behavior

Every phase stops the pipeline immediately on failure and prints the
reason, and a next safe action:

- **Promote fails** → stop before tests. Nothing staged/committed.
  `leadme-collab inspect <task-id>`.
- **Tests fail** (or either `git diff --check` fails) → stop before commit.
  The promoted files are **left in place** (staged, uncommitted) for
  inspection:
  ```
  git status --short
  git diff --stat
  git diff
  ```
- **Commit fails** → stop before push/deploy:
  ```
  git log --oneline -3
  git status --short
  git branch -vv
  ```
- **Push fails and `origin/main` does not contain local HEAD** → stop
  before deploy (same three commands as above).
- **Push fails but `origin/main` already contains local HEAD** (verified by
  an independent fetch + SHA comparison, not assumed) → explained and the
  pipeline continues to deploy (unless `--no-deploy`).
- **Deploy fails** → `leadme-deploy`'s own failure report is shown, plus
  `leadme-deploy --rollback-info` is run inline so the rollback plan is
  visible in the same report, not a separate manual step.

At no point does a test failure, commit failure, or push failure allow the
pipeline to continue to the next step "just in case" — every continuation
past a failure requires the specific verified condition above (origin
already has HEAD), never a guess.

## What `leadme-ship` never does

- Never runs unless a human explicitly invokes it with a task id.
- Never commits without a message.
- Never pushes if `--no-push` was given, or if commit failed.
- Never deploys if `--no-deploy`/`--no-push` was given, if push failed and
  origin doesn't already have HEAD, or if any earlier phase failed.
- Never force-pushes, never `git reset --hard`s, never skips the primary
  repo's own preflight (same checks `leadme-collab start`/`promote` use).
