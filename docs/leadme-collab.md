# leadme-collab

Removes Scot as the human copy/paste bus between an implementer (Claude Code)
and an independent reviewer, for scoped local coding tasks on LeadMeLeads.

## Concept

```
start "task description"
  -> pre-flight checks on the primary repo
  -> isolated worktree + safety ref + dedicated branch (primary repo never touched)
  -> Claude implements the task in that worktree, non-interactively
  -> diff + compile verification captured
  -> Codex reviews the diff (read-only sandbox) and returns PASS / NEEDS FIX / FAIL / NEEDS HUMAN
  -> NEEDS FIX -> Claude repairs (up to 3 cycles) -> Codex re-reviews, automatically
  -> PASS -> "READY FOR HUMAN REVIEW" — you decide what happens next
```

When Codex is installed and authenticated (the common case now — see below),
the entire Claude → Codex → Claude repair → Codex re-review loop runs inside
a single `leadme-collab start` invocation, with no pause for a human message
bus in between. If Codex isn't available, the same loop still runs, just
with a pause at each review step for a file-handoff reviewer.

V1 never pushes, merges, commits on your behalf, or deploys. It hands you a
branch in a worktree and a summary; you review it and decide — `promote`
(below) applies that decision into the primary repo without manual patch
juggling, but still never commits unless you pass `--commit` explicitly, and
never pushes or deploys.

## Architecture

- `scripts/leadme_collab.py` — implementation (stdlib + `git` CLI + `claude`
  CLI only; imports `leadme_deploy.py`'s generic git/subprocess helpers
  read-only rather than duplicating them).
- `scripts/leadme-collab` — exec wrapper (resolves the right Python).
- `~/.local/bin/leadme-collab` — installed symlink to the wrapper.
- State: `~/.local/state/leadme-collab/<task-id>/` (human-readable files).
- Worktrees: `~/.local/share/leadme-collab/worktrees/<task-id>/`.

### Adapters actually available in this environment

| Adapter | Status | Notes |
|---|---|---|
| Claude implementer | **Real, wired up** | `claude` is installed, supports `-p/--print` non-interactive mode with `--output-format json`, `--permission-mode acceptEdits`, `--disallowedTools`. Verified with a live throwaway probe before wiring it in. |
| Codex reviewer | **Real, wired up** | Official Codex CLI (`curl -fsSL https://chatgpt.com/codex/install.sh \| sh`), installed to `~/.local/bin/codex`, authenticated via `codex login --device-auth` (ChatGPT sign-in). Verified with live probes against a disposable throwaway repo before wiring it in — see "Why plain `codex exec`, not `codex exec review`" below. |
| File-handoff reviewer | **Real, wired up** | Automatic fallback whenever `doctor`'s live usability check (install + auth, not just `command -v`) says Codex isn't usable right now. Writes a self-contained `review-prompt.md` (task + diff + verification + required response format) and pauses. Paste it into whatever reviewer you're using (ChatGPT, Codex web, another Claude session, yourself) and save the verdict as `review.md`; then run `leadme-collab resume`. |

Claude Code's own cloud `ultrareview` was **not** used as the reviewer here —
it's a separate, billed, user-triggered feature that this session is not
permitted to invoke on your behalf.

### Why plain `codex exec`, not `codex exec review`

Codex ships a purpose-built `codex exec review` subcommand (`--uncommitted`,
`--base`, `--commit`) that sounded like the obvious fit. Two things ruled
it out after live testing against a disposable throwaway repo:

1. `--uncommitted`/`--base`/`--commit` cannot be combined with a custom
   `[PROMPT]` — the CLI rejects it outright (`error: the argument
   '--uncommitted' cannot be used with '[PROMPT]'`), so there's no way to
   attach our task context or verdict-format contract to it.
2. Without a custom prompt, `codex exec review`'s built-in review mode
   produces its own PR-comment-style output (`- [P1] ... — file.py:12`)
   with **no `VERDICT:` line at all**, in every probe run. It's a fine
   format for a human, but not machine-parseable against our contract.

Plain `codex exec --sandbox read-only` with our own custom prompt (piped via
stdin, cwd set to the task worktree) reliably produced the exact
`VERDICT: ...` first line we need, in every probe — including a PASS
verdict for an innocuous change and a NEEDS FIX verdict (citing the correct
line) for a deliberately broken one. That's the adapter that got wired in.

### Read-only safety

`--sandbox read-only` is Codex's own enforcement layer, not just a prompt
request — confirmed live: `stderr` reports `sandbox: read-only` and
`approval: never` for every invocation (no interactive hang is possible),
and repeated probes against a throwaway repo showed zero file changes even
though the model was never told it *couldn't* try. `run_codex_review()`
never passes `workspace-write`, `danger-full-access`,
`--dangerously-bypass-approvals-and-sandbox`, or any other write-capable
flag — enforced by a static test
(`test_run_codex_review_never_requests_a_write_capable_sandbox`).

Codex runs with the task's **worktree** as its working directory (not an
isolated "reviewer package" outside it) — deliberately, per the stronger
architecture note in the original design: since the CLI *can* guarantee
read-only behavior (verified above), giving it read access to the real
worktree lets it inspect surrounding source for context, while remaining
provably unable to write there.

### Install / auth

Installed with the official installer:

```
curl -fsSL https://chatgpt.com/codex/install.sh | sh
```

which places `codex` in `~/.local/bin` and its config/session state under
`~/.codex`. Authenticated with:

```
codex login --device-auth
```

— the documented path for headless/remote machines (plain `codex login`
opens a local OAuth callback server on `localhost:1455`, which doesn't work
without port access to a browser on the same host). Device-auth prints a
one-time code and a `https://auth.openai.com/codex/device` URL; sign in on
any device and it completes in the background. Check status any time with
`codex login status` (prints only `Logged in using ChatGPT` or
`Not logged in` — never a token). `codex logout` clears it.

## Commands

### `leadme-collab doctor`

Diagnoses the whole pipeline: primary repo, git, worktree support, state/
worktree directory writability, Python runtime, Claude CLI presence/version/
print-mode, and Codex reviewer usability. Reports one of:

```
[PASS] codex reviewer
```
or
```
[WARN] Codex unavailable — file-handoff fallback
```

The Codex check proves usability (installed **and** authenticated via
`codex login status`), not just `command -v codex`. It does not spend real
API budget running a live review probe on every `doctor` call — that
tradeoff is deliberate (see Limitations). No mutation beyond harmless
state-directory creation if it doesn't exist yet.

### `leadme-collab start "task description"` (or `--task-file path.md`)

Runs the whole pipeline in one call when Codex is usable: pre-flight,
isolation (safety ref + worktree + branch), state directory + `task.md`,
Claude implementation, verification, Codex review, and — automatically, with
no separate command needed — repair + re-review cycles up to
`max_review_cycles` (3). Ends at `READY FOR HUMAN REVIEW`, `NEEDS HUMAN`, or
`FAILED`.

If Codex isn't usable, the same phases run up through the first review step,
which instead writes `review-prompt.md` and **pauses**, printing exactly
what to do next (file-handoff).

### `leadme-collab status [task-id]`

Task ID, phase, review cycle, worktree, branch, reviewer mode, changed
files, last event, and final status if the task is complete. Defaults to
the most recently started/resumed task if no ID is given.

### `leadme-collab resume [task-id]`

Picks up from `state.json`'s recorded phase — never restarts from phase 1.
If waiting on `review.md` and it still doesn't exist, reports that and does
nothing else. If it exists, parses the verdict and continues the loop
(repair, re-review, or finish).

### `leadme-collab inspect [task-id]`

Task description, worktree, branch, diff stat, changed files, latest review
cycle file, and the summary path (once written).

### `leadme-collab abort [task-id] [--cleanup]`

Marks the task aborted. By default nothing is deleted — worktree and state
are preserved, and the exact manual cleanup commands are printed. Pass
`--cleanup` to actually remove the worktree and delete the task branch (the
safety ref and state directory are still kept either way).

### `leadme-collab list`

Every task under the state root: ID, one-line task summary, phase, cycle,
created time, worktree path.

### `leadme-collab promote [task-id] [--no-stage] [--commit -m "message"]`

Applies a completed/reviewed task worktree's result into the primary repo
without the manual copy/`git add -f` dance that a new, `.gitignore`-hidden
test file used to require (this is exactly what happened during the
`/history` Run Again ownership fix: `scripts/test_history_rerun_ownership.py`
matched the `test_*.py` rule, so it existed in the worktree but wasn't part
of any diff Codex could see, and Codex correctly blocked on it).

Preflight (same shape as `start`): refuses unless the primary repo exists,
is on `main`, and has a clean tracked tree — known untracked files (`.claude/`,
`CLAUDE.md.backup-*`, `reset_local_password.py`) are still non-blocking.
Then it locates the task by ID (defaults to the most recently
started/resumed task, like `status`/`resume`/`inspect`), and refuses if the
task has no state, no worktree, or no changes to promote.

It inspects the worktree three ways:

- **tracked** modifications — `git diff HEAD --name-status` (added/modified/
  deleted files already known to git)
- **untracked** — new files git sees but was never told to track
- **ignored** — new files hidden by a `.gitignore` rule; each one gets a
  `[WARN] ignored file is part of the task result` line explaining it needs
  `git add -f`

Every changed path is copied into the primary repo at the exact same
relative path (deletions are applied as deletions). By default the promoted
paths are then staged with `git add -f` — which also force-adds any ignored
files, so you don't have to do it by hand — leaving them ready for a human
`git diff --cached` before committing. `git diff --check` (and
`git diff --cached --check` when staged) run automatically, and the final
report shows `git status --short` plus a diff stat.

```
leadme-collab promote 20260710-153458-fix-the-history-run-again-ownership-isol
leadme-collab promote 20260710-153458-fix-the-history-run-again-ownership-isol --no-stage
leadme-collab promote 20260710-153458-fix-the-history-run-again-ownership-isol --commit -m "Fix /history Run Again ownership isolation"
```

- **`--no-stage`** — applies the files but leaves them unstaged. Ignored
  files are still copied to disk, but a `[WARN]` explains they won't show up
  in plain `git status` (only `git status --ignored`) until you `git add -f`
  them yourself.
- **`--commit -m "..."`** — optional; commits the staged promotion in one
  step. Requires staging to have happened (i.e. not combined with
  `--no-stage`) and a message. Still never pushes and never deploys —
  identical guarantee to every other command in this tool.

Promote never touches the task's worktree, safety ref, or branch — those are
still cleaned up (or not) via `abort --cleanup`, same as always.

If you've already decided a task is ready to ship — not just to promote and
inspect — see **`docs/leadme-ship.md`**: `leadme-ship <task-id> -m "..."`
runs this exact promote step and then tests, commit, push, and deploy in
one explicit, human-invoked command. `leadme-collab` itself never chains
into it; a `PASS` verdict here only ever ends a task at
`READY FOR HUMAN REVIEW`.

## Reviewer verdicts and the repair loop

The reviewer's response must start with exactly one of:

```
VERDICT: PASS
VERDICT: NEEDS FIX
VERDICT: FAIL
VERDICT: NEEDS HUMAN
```

- **PASS** → task finishes as `READY FOR HUMAN REVIEW`.
- **NEEDS FIX** → Claude repairs (smallest fix only) in the same worktree,
  diff/verification are recaptured, and a new review cycle starts — up to
  `max_review_cycles` (3). After 3 unresolved cycles: `NEEDS HUMAN`.
- **FAIL** → `FAILED` (reviewer judged it unsalvageable, not just needing a
  tweak).
- **NEEDS HUMAN** → stops immediately, no more automated cycles.
- Unparseable review text (no `VERDICT:` line) → fails closed to
  `NEEDS HUMAN` rather than guessing.

Each cycle's review is archived as `review-cycle-N.md` so history isn't lost
across cycles.

## Worktrees and safety refs

For a task started at commit `<base>`:

- Safety ref: `refs/heads/backup/collab-<task-id>` → pinned to `<base>`
  forever (until you delete it yourself).
- Worktree: `~/.local/share/leadme-collab/worktrees/<task-id>/`, checked out
  on a new branch `collab/<task-id>` starting at `<base>`.
- The primary repo/worktree is verified untouched immediately after
  isolation is created, and the tool fails loudly if that check doesn't
  hold.

Claude and Codex both only ever run with that worktree directory as their
working directory — neither ever touches the primary checkout. Claude runs
with edit permissions scoped to the worktree; Codex runs `--sandbox
read-only` and cannot write there at all.

## State files

Per task, under `~/.local/state/leadme-collab/<task-id>/`:

```
task.md               — structured task definition (outcome, constraints, protected areas, ...)
implementer-prompt.md  — exact prompt sent to Claude for implementation
implementer-output.md  — Claude's result text + raw stdout/stderr + cost/timing
diff.patch             — current diff of the worktree against its base
verification.md        — compile results for every changed .py file
review-prompt.md       — self-contained prompt for the independent reviewer
review.md              — the reviewer's verdict + findings (Codex writes this automatically; a human/external reviewer does in file-handoff mode)
codex-raw-output.md    — Codex's returncode/elapsed time/stdout/stderr for the cycle (only written in codex mode)
review-cycle-N.md       — archived copy of each cycle's review
repair-prompt.md        — prompt sent to Claude for a repair pass
repair-output.md        — Claude's repair result
summary.md              — final human-readable report
state.json              — machine state (phase, cycle, commit, paths — no secrets)
events.log              — append-only timestamped event log
```

`state.json` refuses to persist any value that looks secret-shaped (same
guard pattern as `leadme-deploy`'s state file) — no passwords, tokens, SSH
keys, SMTP secrets, or `.env` contents are ever written there.

## Limitations (V1)

- **`doctor`'s Codex check doesn't run a live review probe.** It verifies
  install + auth state (`codex --version`, `codex login status`), not a full
  paid API round-trip, on every call. If Codex is installed+authenticated
  but somehow broken in a way that only shows up mid-review (rate limit,
  model outage), `_start_review_cycle()` still fails closed to
  `NEEDS HUMAN` rather than hanging or guessing — just not until the first
  real review attempt.
- **Real reviews cost real API budget.** Every automated Codex review cycle
  is a genuine paid call (same for Claude implementation/repair). There's no
  dry-run/preview mode for the loop itself — use `doctor` to check
  reachability without spending anything.
- **No automatic test discovery.** Verification compiles every changed
  `.py` file; it does not guess which existing test file to run. Run
  relevant tests yourself before trusting a `PASS`.
- **No merge/push/deploy**, by design — V1 always stops at "hand it to a
  human."
- A repair cycle re-sends the **whole current diff** to Claude (truncated
  at 20,000 chars) rather than a minimal patch context — fine for small/
  medium changes, not ideal for huge diffs. The same 20,000-char truncation
  applies to what Codex sees in the review prompt.
- Codex is invoked once per review step; a Codex-side failure (timeout,
  nonzero exit, empty output) is not retried automatically — it's preserved
  verbatim in `review.md`/`codex-raw-output.md` and fails closed to
  `NEEDS HUMAN` on the next `parse_verdict()` pass, consistent with "never
  silently convert malformed output into PASS."
- `leadme-collab start` currently requires the primary repo to already be on
  `main`; it does not create tasks from other starting branches.

## Cleanup

- `leadme-collab abort <task-id>` — marks aborted, changes nothing on disk.
- `leadme-collab abort <task-id> --cleanup` — also removes the worktree
  directory and deletes the `collab/<task-id>` branch. The safety ref and
  the state directory are kept.
- To fully remove a task's history once you're done with it:
  ```
  git -C /mnt/c/Users/scott/ai-project/seo-app worktree remove <path> --force   # if not already removed
  git -C /mnt/c/Users/scott/ai-project/seo-app branch -D collab/<task-id>       # if not already removed
  git -C /mnt/c/Users/scott/ai-project/seo-app update-ref -d refs/heads/backup/collab-<task-id>
  rm -rf ~/.local/state/leadme-collab/<task-id>
  ```
  None of this is done automatically — it's your call.
