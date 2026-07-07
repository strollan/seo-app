# LeadMeLeads — Autonomous Working Rules

## Mission

Work autonomously on clearly requested tasks.

Do not stop for unnecessary confirmation when the requested action is already clear and within scope.

When a normal implementation decision is required:

1. Inspect the existing code and surrounding patterns.
2. Choose the smallest safe solution.
3. Implement it.
4. Verify it.
5. Inspect the diff.
6. Report exactly what changed.

Do not ask the user to choose between minor implementation details when one choice is clearly safer, simpler, or more consistent with the existing application.

---

## Autonomous Work Policy

Treat ordinary development work as pre-approved when it is necessary to complete the user's explicit request and permitted by Claude Code's configured permissions.

This includes:

- reading project files
- searching the repository
- inspecting existing implementations
- editing files directly related to the task
- creating narrowly scoped files required by the task
- creating backups before risky edits
- running syntax checks
- running existing tests
- inspecting git status
- inspecting git diff
- comparing before and after behavior
- correcting syntax errors introduced by the current task
- re-running verification after a failed check

Do not repeatedly ask permission to inspect, edit, test, or verify when those actions are clearly necessary to complete the requested task.

Questions should be exceptional, not routine.

---

## Project Location

Primary project:

`/mnt/c/Users/scott/ai-project/seo-app`

---

## Project Safety

Before meaningful edits:

1. Inspect the relevant implementation.
2. Understand nearby dependencies.
3. Prefer the smallest targeted change.
4. Create a backup when regression risk is meaningful.
5. Keep scope narrow.
6. Verify afterward.

Never silently broaden the task.

Never perform unrelated cleanup just because it looks desirable.

Never refactor unrelated code while fixing a narrow issue.

---

## Protected Areas

Do not change these areas unless the user explicitly requests work involving them:

- authentication behavior
- login behavior
- session behavior
- database/session behavior
- user ownership boundaries
- role logic
- admin authorization
- export ownership
- production deployment configuration
- secrets
- environment files
- LeadBot backend data flow

An explicit task involving one of these areas allows work only within the requested scope. It does not authorize broader redesign.

---

## Role Model

Preserve the existing three-role model:

1. Logged-out users
2. Standard users
3. Admin users

Standard users must only access their own authorized data and exports.

Admins may access admin-authorized functionality and data.

Never weaken authorization boundaries as a convenience.

---

## Secrets

Never expose, print, copy, move, commit, or modify secrets.

Do not read `.env` files unless the user explicitly authorizes that specific access for a specific task.

Never print API keys or credentials into:

- terminal output
- logs
- source code
- test fixtures
- documentation
- commit messages

---

## Destructive Operations

Do not perform destructive operations unless the user explicitly requests the exact operation.

Examples include:

- `rm -rf`
- `git reset --hard`
- `git clean`
- force push
- deleting databases
- deleting user data
- wiping exports
- deleting production data
- removing migrations
- overwriting production configuration

When uncertainty exists about irreversible data loss, stop.

---

## Git Rules

Normal inspection is allowed:

- `git status`
- `git diff`
- `git log`
- `git show`
- branch inspection

Do not automatically:

- commit
- push
- force push
- merge
- reset hard
- clean untracked files
- change branches when active work could be affected

unless explicitly authorized or permitted through the configured approval workflow.

Never claim Git is clean without checking.

---

## Backup Policy

Before a high-risk or structurally meaningful edit, create a targeted backup when practical.

Prefer:

`filename.bak_YYYYMMDD_HHMMSS`

Do not create unnecessary backup clutter for trivial changes.

Never treat a backup as a substitute for verification.

---

## LeadMeLeads Architecture

Preserve the existing FastAPI application architecture unless redesign is explicitly requested.

Do not casually replace existing patterns with:

- a new framework
- a new frontend architecture
- a new database
- a new auth system
- a new session system
- a new background-job architecture

Prefer compatibility with the existing codebase.

---

## Scope Discipline

Only modify files necessary for the requested task.

Before editing:

- identify the exact implementation
- inspect nearby code
- determine the narrowest safe change

After editing:

- inspect the diff
- confirm no unrelated files changed
- test the requested behavior

Do not use a small request as permission for broad cleanup.

---

## UI Work

For UI tasks:

1. Identify the exact page and element.
2. Inspect the rendered structure and relevant CSS.
3. Find the actual selector affecting the problem.
4. Make the narrowest targeted change.
5. Verify nearby elements were not unintentionally affected.
6. Check mobile when the task concerns responsive behavior.

Do not repeatedly adjust random spacing values without identifying the responsible layout rule.

Do not redesign unrelated elements.

---

## Python Verification

After Python changes, run the relevant syntax or compile checks.

Preferred project compile check:

`python -m py_compile app/main.py agents/*.py scripts/*.py business_competitor_finder.py`

When appropriate, also run:

- focused tests
- existing pytest tests
- route-level checks
- targeted application verification

If the full compile command is inappropriate because a glob does not match or a file is intentionally unavailable, use the narrowest valid equivalent and report that honestly.

---

## Error Recovery

When work causes an error:

1. Read the actual error.
2. Identify the likely cause.
3. Determine whether the current task introduced it.
4. Fix errors introduced by the current task.
5. Re-run verification.
6. Inspect the final diff.

Do not immediately ask the user what to do when the error is a direct consequence of the work just performed.

Do not hide failed verification.

---

## Decision Policy

Ask the user only when a decision is genuinely consequential.

Examples:

- the request is materially ambiguous
- choices produce different product behavior
- credentials are required
- secret access is required
- production deployment is involved
- irreversible data deletion is involved
- a database migration is involved
- a destructive Git operation is involved
- an external paid service will incur cost
- the requested work conflicts with an explicit project rule

Otherwise:

- inspect
- make the safest reasonable decision
- continue
- verify

---

## No Fake Success

Never claim:

- fixed
- complete
- verified
- working
- passed

without evidence.

Use actual evidence from:

- file inspection
- command output
- tests
- compile checks
- rendered behavior
- diffs

If verification is incomplete, say so plainly.

---

## Completion Report

At the end of a task, report:

- what changed
- exact files changed
- verification performed
- whether tests passed
- any remaining limitation or concern

Keep completion reports direct.

Do not bury failures.

---

## Core Principle

Move quickly on reversible work.

Be cautious with irreversible work.

Do not waste the user's time with unnecessary permission questions.

Do not confuse autonomy with recklessness.
