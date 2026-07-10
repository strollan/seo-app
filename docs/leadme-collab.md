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
  -> an independent reviewer reads the diff and returns PASS / NEEDS FIX / FAIL / NEEDS HUMAN
  -> NEEDS FIX -> Claude repairs (up to 3 cycles) -> re-review
  -> PASS -> "READY FOR HUMAN REVIEW" — you decide what happens next
```

V1 never pushes, merges, commits on your behalf, or deploys. It hands you a
branch in a worktree and a summary; you review it and decide.

## Architecture

- `scripts/leadme_collab.py` — implementation (stdlib + `git` CLI + `claude`
  CLI only; imports `leadme_deploy.py`'s generic git/subprocess helpers
  read-only rather than duplicating them).
- `scripts/leadme-collab` — exec wrapper (resolves the right Python).
- `~/.local/bin/leadme-collab` — installed symlink to the wrapper.
- State: `~/.local/state/leadme-collab/<task-id>/` (human-readable files).
- Worktrees: `~/.local/share/leadme-collab/worktrees/<task-id>/`.

### Adapters actually available in this environment

Checked by read-only discovery (`command -v claude`, `claude --version`,
`command -v codex`) before anything was built:

| Adapter | Status | Notes |
|---|---|---|
| Claude implementer | **Real, wired up** | `claude` is installed (`2.1.206`), supports `-p/--print` non-interactive mode with `--output-format json`, `--permission-mode acceptEdits`, `--disallowedTools`. Verified with a live throwaway probe before wiring it in. |
| Codex reviewer | **Not available** | `codex` is not installed anywhere on `PATH` in this environment. `discover_codex()` reports this honestly; `codex_review_stub()` raises `NotImplementedError` rather than guessing invented CLI flags. If a real Codex CLI is installed later, inspect its `--help` and wire up a genuine adapter — do not assume the stub's shape is right. |
| File-handoff reviewer | **Real, wired up** | The fallback when Codex isn't usable. Writes a self-contained `review-prompt.md` (task + diff + verification + required response format) and pauses. Paste it into whatever reviewer you're using (ChatGPT, Codex web, another Claude session, yourself) and save the verdict as `review.md`; then run `leadme-collab resume`. |

Claude Code's own cloud `ultrareview` was **not** used as the reviewer here —
it's a separate, billed, user-triggered feature that this session is not
permitted to invoke on your behalf.

## Commands

### `leadme-collab doctor`

Diagnoses the whole pipeline: primary repo, git, worktree support, state/
worktree directory writability, Python runtime, Claude CLI presence/version/
print-mode, Codex presence, and which reviewer adapter is actually in play.
No mutation beyond harmless state-directory creation if it doesn't exist yet.

### `leadme-collab start "task description"` (or `--task-file path.md`)

Runs phases 1–5 automatically: pre-flight, isolation (safety ref + worktree +
branch), state directory + `task.md`, and the Claude implementation step.
Then it either runs an automated review (if a real Codex adapter is ever
wired up) or — today — writes `review-prompt.md` and **pauses**, printing
exactly what to do next.

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

Claude only ever runs with that worktree directory as its working
directory — it never edits the primary checkout.

## State files

Per task, under `~/.local/state/leadme-collab/<task-id>/`:

```
task.md               — structured task definition (outcome, constraints, protected areas, ...)
implementer-prompt.md  — exact prompt sent to Claude for implementation
implementer-output.md  — Claude's result text + raw stdout/stderr + cost/timing
diff.patch             — current diff of the worktree against its base
verification.md        — compile results for every changed .py file
review-prompt.md       — self-contained prompt for the independent reviewer
review.md              — the reviewer's verdict + findings (you/they create this)
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

- **No real automated reviewer today.** Codex isn't installed, so every
  review cycle currently pauses for a human/external reviewer. This is
  reported honestly by `doctor` rather than faked.
- **No automatic test discovery.** Verification compiles every changed
  `.py` file; it does not guess which existing test file to run. Run
  relevant tests yourself before trusting a `PASS`.
- **No merge/push/deploy**, by design — V1 always stops at "hand it to a
  human."
- A repair cycle re-sends the **whole current diff** to Claude (truncated
  at 20,000 chars) rather than a minimal patch context — fine for small/
  medium changes, not ideal for huge diffs.
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
