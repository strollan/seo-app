#!/usr/bin/env python3
"""
leadme-collab — local implement/review/repair loop automation for LeadMeLeads.

Removes Scot as the copy/paste message bus between an implementer (Claude
Code) and an independent reviewer (Codex CLI if installed, otherwise a
file-handoff to whatever external reviewer he's using — ChatGPT, Codex web,
another Claude session, a human).

Design notes:
- Every task runs in its own throwaway git worktree + branch, off a safety
  ref pinned to the exact base commit. The primary repo/worktree is never
  edited by this tool or by the Claude subprocess it spawns.
- All external commands go through leadme_deploy.run_local() (imported, not
  duplicated) so tests can monkeypatch one choke point.
- No automatic push/merge/commit/deploy anywhere in this module. Final
  state is always "hand it to a human", never "it's live".
- Codex CLI is not installed in this environment (verified via `command -v
  codex`). The reviewer adapter therefore runs as file-handoff: a
  self-contained review-prompt.md is written and the task pauses for
  `resume` once a human/other reviewer drops a review.md verdict in place.
  The Codex adapter function is a clearly-labeled stub — it is not wired to
  invented flags, per instructions not to fake CLI behavior.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import leadme_deploy as ld  # noqa: E402  (reused, read-only: run_local, git helpers)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REPO_PATH = ld.REPO_PATH
PRIMARY_BRANCH = ld.PRIMARY_BRANCH

STATE_ROOT = Path.home() / ".local" / "state" / "leadme-collab"
WORKTREE_ROOT = Path.home() / ".local" / "share" / "leadme-collab" / "worktrees"
CURRENT_TASK_POINTER = STATE_ROOT / "current_task"

MAX_REVIEW_CYCLES = 3
CLAUDE_TIMEOUT_SECONDS = 900  # 15 minutes per Claude invocation
CLAUDE_MAX_BUDGET_USD = "2.00"
CODEX_TIMEOUT_SECONDS = 600  # 10 minutes per Codex review invocation

DISALLOWED_TOOLS = (
    "Bash(git push:*) "
    "Bash(git commit:*) "
    "Bash(git merge:*) "
    "Bash(git reset --hard:*) "
    "Bash(sudo:*) "
    "Bash(pip install:*) "
    "Bash(npm install:*) "
    "Bash(apt-get:*) "
    "Bash(apt install:*)"
)

PROTECTED_AREAS = [
    "authentication behavior",
    "login behavior",
    "session behavior",
    "database/session behavior",
    "user ownership boundaries",
    "role logic",
    "admin authorization",
    "export ownership",
    "production deployment configuration",
    "secrets",
    "environment files",
    "LeadBot backend data flow",
]

DEFAULT_CONSTRAINTS = [
    "no deploy",
    "no push",
    "no merge to main",
    "no production mutation",
    "no auth DB writes unless task explicitly authorizes",
    "no environment installation without approval",
    "preserve unrelated files",
    "minimal targeted changes",
]

VERDICT_PATTERN = re.compile(
    r"^\s*VERDICT:\s*(PASS|NEEDS FIX|FAIL|NEEDS HUMAN)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

SECRET_LIKE_KEY_PATTERN = ld.SECRET_LIKE_KEY_PATTERN

STATE_JSON_NAME = "state.json"
EVENTS_LOG_NAME = "events.log"

ARTIFACT_NAMES = [
    "task.md",
    "plan.md",
    "implementer-prompt.md",
    "implementer-output.md",
    "diff.patch",
    "review-prompt.md",
    "review.md",
    "repair-prompt.md",
    "repair-output.md",
    "verification.md",
    "summary.md",
]


# ---------------------------------------------------------------------------
# Small shared utilities
# ---------------------------------------------------------------------------

def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(text, max_len=40):
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text[:max_len].rstrip("-") or "task"


def new_task_id(description):
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{ts}-{slugify(description)}"


def task_dir(task_id):
    return STATE_ROOT / task_id


def worktree_dir(task_id):
    return WORKTREE_ROOT / task_id


def branch_name(task_id):
    return f"collab/{task_id}"


def safety_ref_name(task_id):
    return f"refs/heads/backup/collab-{task_id}"


def q(value):
    import shlex
    return shlex.quote(str(value))


class Reporter:
    def __init__(self):
        self.lines = []
        self.failed = False

    def title(self, text):
        self.lines.append(text)
        self.lines.append("─" * max(24, len(text)))

    def section(self, name):
        self.lines.append("")
        self.lines.append(name.upper())

    def step(self, label, ok, detail=""):
        tag = "PASS" if ok else "FAIL"
        if not ok:
            self.failed = True
        text = f"[{tag}] {label}"
        if detail:
            text += f" — {detail}"
        self.lines.append(text)
        return ok

    def warn(self, label, detail=""):
        text = f"[WARN] {label}"
        if detail:
            text += f" — {detail}"
        self.lines.append(text)

    def note(self, text=""):
        self.lines.append(text)

    def render(self):
        return "\n".join(self.lines)


# ---------------------------------------------------------------------------
# Task state (state.json) + events.log
# ---------------------------------------------------------------------------

def write_task_state(tid, data):
    for value in data.values():
        if isinstance(value, str) and SECRET_LIKE_KEY_PATTERN.search(value):
            raise ValueError("refusing to persist a value that looks secret-shaped")
    d = task_dir(tid)
    d.mkdir(parents=True, exist_ok=True)
    path = d / STATE_JSON_NAME
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def read_task_state(tid):
    path = task_dir(tid) / STATE_JSON_NAME
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def log_event(tid, name, detail=""):
    d = task_dir(tid)
    d.mkdir(parents=True, exist_ok=True)
    line = f"{utc_now_iso()}  {name}"
    if detail:
        line += f"  {detail}"
    with open(d / EVENTS_LOG_NAME, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def write_artifact(tid, name, content):
    d = task_dir(tid)
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    path.write_text(content, encoding="utf-8")
    return path


def read_artifact(tid, name):
    path = task_dir(tid) / name
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def set_current_task(tid):
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    CURRENT_TASK_POINTER.write_text(tid, encoding="utf-8")


def get_current_task():
    if not CURRENT_TASK_POINTER.exists():
        return None
    tid = CURRENT_TASK_POINTER.read_text(encoding="utf-8").strip()
    return tid or None


def list_task_ids():
    if not STATE_ROOT.is_dir():
        return []
    ids = []
    for entry in STATE_ROOT.iterdir():
        if entry.is_dir() and (entry / STATE_JSON_NAME).exists():
            ids.append(entry.name)
    return sorted(ids)


def resolve_task_id(explicit_tid):
    if explicit_tid:
        return explicit_tid
    tid = get_current_task()
    if tid:
        return tid
    ids = list_task_ids()
    return ids[-1] if ids else None


# ---------------------------------------------------------------------------
# Discovery: Claude CLI / Codex CLI
# ---------------------------------------------------------------------------

def discover_claude():
    path = shutil.which("claude")
    if not path:
        return {"available": False, "path": None, "version": None, "print_mode": False}
    version_res = ld.run_local(["claude", "--version"], timeout=15)
    help_res = ld.run_local(["claude", "--help"], timeout=15)
    print_mode = help_res.ok and ("-p, --print" in help_res.stdout or "--print" in help_res.stdout)
    return {
        "available": True,
        "path": path,
        "version": version_res.stdout.strip() if version_res.ok else None,
        "print_mode": print_mode,
    }


def discover_codex():
    """Prove Codex is actually usable, not just present on PATH.

    "Usable" here means: the binary runs, and it's authenticated (`codex
    login status` succeeds). This does NOT spend real API budget on every
    doctor/start call by running a live review probe each time — that
    tradeoff was deliberate (see docs/leadme-collab.md) — but it never
    reports usable_noninteractive=True on `command -v codex` alone.
    """
    path = shutil.which("codex")
    if not path:
        return {
            "available": False, "path": None, "version": None,
            "authenticated": False, "usable_noninteractive": False,
            "detail": "codex not found on PATH",
        }

    version_res = ld.run_local([path, "--version"], timeout=15)
    version = version_res.stdout.strip() if version_res.ok else None
    if not version:
        return {
            "available": True, "path": path, "version": None,
            "authenticated": False, "usable_noninteractive": False,
            "detail": "codex --version failed",
        }

    status_res = ld.run_local([path, "login", "status"], timeout=15)
    authenticated = status_res.ok and "not logged in" not in status_res.stdout.lower()

    return {
        "available": True,
        "path": path,
        "version": version,
        "authenticated": authenticated,
        "usable_noninteractive": authenticated,
        "detail": "" if authenticated else "codex not authenticated (run: codex login --device-auth)",
    }


def choose_reviewer_mode():
    codex = discover_codex()
    if codex["available"] and codex["usable_noninteractive"]:
        return "codex", codex
    return "file-handoff", codex


# ---------------------------------------------------------------------------
# Claude implementer adapter
# ---------------------------------------------------------------------------

def run_claude(prompt_text, cwd, timeout=CLAUDE_TIMEOUT_SECONDS):
    """Invoke the real Claude CLI non-interactively in `cwd`.

    Returns a dict: stdout, stderr, returncode, elapsed_seconds, result_text,
    is_error, total_cost_usd (best-effort; parsed from --output-format json).
    """
    args = [
        "claude",
        "-p", prompt_text,
        "--output-format", "json",
        "--no-session-persistence",
        "--permission-mode", "acceptEdits",
        "--disallowedTools", DISALLOWED_TOOLS,
        "--max-budget-usd", CLAUDE_MAX_BUDGET_USD,
    ]
    start = time.monotonic()
    res = ld.run_local(args, cwd=cwd, timeout=timeout)
    elapsed = time.monotonic() - start

    parsed = None
    if res.ok:
        try:
            parsed = json.loads(res.stdout)
        except (ValueError, TypeError):
            parsed = None

    return {
        "returncode": res.returncode,
        "stdout": res.stdout,
        "stderr": res.stderr,
        "elapsed_seconds": round(elapsed, 1),
        "result_text": parsed.get("result") if parsed else None,
        "is_error": (parsed.get("is_error") if parsed else None) if parsed else (res.returncode != 0),
        "total_cost_usd": parsed.get("total_cost_usd") if parsed else None,
        "parsed": parsed,
    }


def run_codex_review(prompt_text, cwd, timeout=CODEX_TIMEOUT_SECONDS):
    """Invoke the real Codex CLI as a read-only, non-interactive reviewer.

    Uses plain `codex exec --sandbox read-only` — deliberately NOT the
    specialized `codex exec review` subcommand, which has its own built-in
    PR-comment-style formatting (`[P1] ... — file:line`) that ignores a
    custom "first line must be VERDICT: ..." instruction entirely (verified
    live: it produced zero VERDICT line across repeated probes). Plain
    `codex exec` with a custom prompt honored the verdict-line contract
    reliably in the same probes.

    `--sandbox read-only` is Codex's own enforcement layer (not just a
    prompt instruction): verified live that a review under this flag makes
    zero file changes and needs no approval (`approval: never`, no hang),
    even though the prompt never disables its ability to try. cwd is the
    task's worktree, so Codex can read surrounding source for context; it
    cannot write there.

    The prompt is piped via stdin (`-`) rather than passed as an argv
    string, to avoid ARG_MAX limits on large diffs. The final message is
    captured via `-o <file>` into a private temp file rather than parsed
    out of `--json` event-stream noise.
    """
    with tempfile.TemporaryDirectory(prefix="leadme-collab-codex-out-") as tmp:
        output_path = Path(tmp) / "codex-output.txt"
        args = [
            "codex", "exec",
            "--sandbox", "read-only",
            "--ephemeral",
            "-o", str(output_path),
            "-",
        ]
        start = time.monotonic()
        res = ld.run_local(args, cwd=cwd, timeout=timeout, input_text=prompt_text)
        elapsed = time.monotonic() - start

        result_text = None
        if output_path.exists():
            try:
                result_text = output_path.read_text(encoding="utf-8").strip()
            except OSError:
                result_text = None

    is_error = res.returncode != 0 or not result_text
    return {
        "returncode": res.returncode,
        "stdout": res.stdout,
        "stderr": res.stderr,
        "elapsed_seconds": round(elapsed, 1),
        "result_text": result_text,
        "is_error": is_error,
    }


# ---------------------------------------------------------------------------
# Git / worktree helpers (local repo only — never touches production)
# ---------------------------------------------------------------------------

def git(args, cwd=None, timeout=30):
    return ld.run_local(["git"] + args, cwd=cwd, timeout=timeout)


def worktree_support_ok():
    res = git(["worktree", "list"], cwd=REPO_PATH, timeout=15)
    return res.ok


def primary_repo_snapshot():
    """A cheap fingerprint of primary-repo state, to prove it wasn't touched."""
    return {
        "branch": ld.current_branch(REPO_PATH),
        "head": ld.head_sha(REPO_PATH),
        "clean": ld.tracked_tree_clean(REPO_PATH),
    }


def primary_repo_unchanged(before):
    after = primary_repo_snapshot()
    return before == after, before, after


def create_safety_ref(base_sha, tid):
    ref = safety_ref_name(tid)
    res = git(["update-ref", ref, base_sha], cwd=REPO_PATH, timeout=15)
    return res.ok, ref, (res.stderr or "").strip()


def create_worktree(base_sha, tid):
    path = worktree_dir(tid)
    branch = branch_name(tid)
    WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)
    res = git(
        ["worktree", "add", "-q", str(path), "-b", branch, base_sha],
        cwd=REPO_PATH,
        timeout=120,
    )
    return res.ok, path, branch, (res.stdout + res.stderr).strip()


def worktree_head(path):
    res = git(["rev-parse", "HEAD"], cwd=path, timeout=15)
    return res.stdout.strip() if res.ok else None


def capture_diff(worktree_path):
    diff_res = git(["diff", "HEAD"], cwd=worktree_path, timeout=30)
    stat_res = git(["diff", "HEAD", "--stat"], cwd=worktree_path, timeout=30)
    files_res = git(["diff", "HEAD", "--name-only"], cwd=worktree_path, timeout=30)
    changed = [line for line in files_res.stdout.splitlines() if line.strip()]
    return {
        "patch": diff_res.stdout,
        "stat": stat_res.stdout.strip(),
        "changed_files": changed,
    }


def remove_worktree(path, force=True):
    args = ["worktree", "remove", str(path)]
    if force:
        args.append("--force")
    return git(args, cwd=REPO_PATH, timeout=30)


def delete_branch(branch, force=True):
    args = ["branch", "-D" if force else "-d", branch]
    return git(args, cwd=REPO_PATH, timeout=15)


# ---------------------------------------------------------------------------
# Verification (compile changed .py files with the same interpreter leadme-deploy uses)
# ---------------------------------------------------------------------------

def verify_changed_files(worktree_path, changed_files):
    py_files = [f for f in changed_files if f.endswith(".py")]
    python_bin = ld.local_python_for_compile()
    results = []
    if py_files:
        results = ld.compile_targets(python_bin, repo=worktree_path, targets=py_files)
    lines = [f"Compile interpreter: {python_bin}", ""]
    if not py_files:
        lines.append("No changed .py files — nothing to compile.")
    for target, ok, detail in results:
        lines.append(f"[{'PASS' if ok else 'FAIL'}] compile {target}" + (f" — {detail}" if detail else ""))
    lines.append("")
    lines.append(
        "Automatic test discovery is out of scope for V1 — run any relevant "
        "existing tests manually before treating this as verified."
    )
    all_ok = all(ok for _, ok, _ in results)
    return "\n".join(lines), all_ok


# ---------------------------------------------------------------------------
# Task artifact templates
# ---------------------------------------------------------------------------

def build_task_md(description, base_commit):
    constraints = "\n".join(f"- {c}" for c in DEFAULT_CONSTRAINTS)
    protected = "\n".join(f"- {p}" for p in PROTECTED_AREAS)
    return f"""# Task

## Requested outcome

{description.strip()}

## Repo

{REPO_PATH}

## Base branch

{PRIMARY_BRANCH}

## Base commit

{base_commit}

## Constraints

{constraints}

## Protected areas

{protected}

## Success criteria

The requested outcome is implemented with the smallest safe change; unrelated
behavior, files, and tests are preserved; the change compiles cleanly.

## Verification expectations

- `python -m py_compile` on every changed `.py` file
- Existing relevant tests reviewed/run where practical
- Diff reviewed for scope creep before it is considered done

## No-go actions

- `git commit`, `git push`, `git merge` (leave changes as uncommitted edits)
- Any deploy or production command
- Installing packages, OS packages, or browser runtimes
- Editing files outside this worktree
- Reading or printing `.env` contents or secrets
"""


def build_implementer_prompt(task_md_text):
    return f"""You are the implementer step of an automated local pipeline
(leadme-collab). You are running non-interactively inside an isolated git
worktree created solely for this task — nobody will approve individual tool
calls, so file edits in this worktree are pre-authorized. Do not touch
anything outside this worktree.

{task_md_text}

Additional instructions:
- Make the smallest safe change that satisfies the requested outcome above.
- Do NOT run git commit, git push, git merge, or any deployment command —
  leave your changes as uncommitted edits in this worktree's working tree.
- Do NOT install packages, OS packages, or browser runtimes.
- Do NOT modify the protected areas listed above unless the requested
  outcome explicitly requires it.
- When finished, briefly summarize what you changed and why in your final
  response.
"""


def build_review_prompt(task_md_text, diff_info, verification_text, reviewer_mode="file-handoff"):
    changed = "\n".join(f"- {f}" for f in diff_info["changed_files"]) or "(no files changed)"
    patch = diff_info["patch"]
    if len(patch) > 20000:
        patch = patch[:20000] + "\n...[diff truncated at 20000 chars]...\n"
    dims = "\n".join(f"- {d}" for d in [
        "correctness",
        "task fulfillment (does the diff actually do what the task asked?)",
        "regressions",
        "scope creep",
        "security",
        "auth/session impact",
        "database/schema impact",
        "route behavior",
        "mobile/desktop impact when relevant",
        "missing tests",
        "accidental broad changes",
        "secrets/debug leftovers",
        "deployment risk",
    ])

    if reviewer_mode == "codex":
        delivery = (
            "Your entire response is captured programmatically as the review "
            "— do not attempt to write or edit any file yourself."
        )
    else:
        delivery = (
            "Save your full response as review.md in this task's state "
            "directory, then run: leadme-collab resume"
        )

    return f"""You are an INDEPENDENT, READ-ONLY reviewer in an automated
implement/review/repair pipeline (leadme-collab). Review only — do not edit
any files, and do not propose broad refactors unless the task explicitly
requires one. Distinguish real blockers from non-blocking follow-up
suggestions, and cite exact files/functions/lines where possible.

{task_md_text}

## Changed files

{changed}

## Diff

```diff
{patch}
```

## Verification output so far

{verification_text}

## What to review

{dims}

## Required response format

The FIRST LINE of your response must be exactly one of:

VERDICT: PASS
VERDICT: NEEDS FIX
VERDICT: FAIL
VERDICT: NEEDS HUMAN

After that line, list concrete findings (or state there are none). For each
finding, mark it as a BLOCKER (must fix before this can pass) or FOLLOW-UP
(non-blocking, worth noting but not disqualifying), and cite the file (and
function/line, if applicable). {delivery}
"""


def build_repair_prompt(task_md_text, review_text, diff_info):
    changed = "\n".join(f"- {f}" for f in diff_info["changed_files"]) or "(no files changed)"
    patch = diff_info["patch"]
    if len(patch) > 20000:
        patch = patch[:20000] + "\n...[diff truncated at 20000 chars]...\n"
    return f"""You are the repair step of an automated pipeline
(leadme-collab), in the same isolated worktree as before. An independent
reviewer found issues with your previous change. Apply the SMALLEST fix that
addresses the findings below — do not restructure unrelated code, and do not
re-scope the task.

{task_md_text}

## Reviewer findings

{review_text}

## Current changed files

{changed}

## Current diff

```diff
{patch}
```

Additional instructions:
- Do NOT run git commit, git push, git merge, or any deployment command.
- Do NOT install packages, OS packages, or browser runtimes.
- Make the minimal targeted correction the review calls for, nothing more.
"""


def parse_verdict(review_text):
    match = VERDICT_PATTERN.search(review_text or "")
    if not match:
        return None
    return match.group(1).strip().upper()


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

def cmd_doctor(args):
    r = Reporter()
    r.title("LeadMe Collab Doctor")

    r.section("local repo")
    r.step("primary repo exists", ld.repo_exists(REPO_PATH), str(REPO_PATH))
    r.step("git available", ld.git_available())
    r.step("git worktree support", worktree_support_ok())
    branch = ld.current_branch(REPO_PATH)
    r.step("current branch resolvable", branch is not None, branch or "unknown")

    fetch_ok, fetch_err = ld.fetch_origin(REPO_PATH, PRIMARY_BRANCH)
    if fetch_ok:
        state, ahead, behind = ld.divergence_state(REPO_PATH, PRIMARY_BRANCH)
        r.step(f"origin divergence ({state})", state in ("in-sync", "ahead", "behind"),
               f"ahead={ahead} behind={behind}")
    else:
        r.warn("could not fetch origin/main", fetch_err)

    r.section("state / worktree directories")
    try:
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        probe = STATE_ROOT / ".doctor-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        r.step("state directory writable", True, str(STATE_ROOT))
    except OSError as exc:
        r.step("state directory writable", False, str(exc))

    try:
        WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)
        probe = WORKTREE_ROOT / ".doctor-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        r.step("worktree directory writable", True, str(WORKTREE_ROOT))
    except OSError as exc:
        r.step("worktree directory writable", False, str(exc))

    r.section("python runtime")
    python_bin = ld.local_python_for_compile()
    r.note(f"  compile interpreter: {python_bin}")
    r.step("python interpreter resolves", Path(python_bin).exists() or python_bin == sys.executable)

    r.section("implementer (claude)")
    claude = discover_claude()
    claude_ok = r.step("claude implementer", claude["available"] and claude["print_mode"],
                        claude["path"] or "not found")
    if claude["available"]:
        r.note(f"  version: {claude['version']}")

    r.section("reviewer")
    mode, codex = choose_reviewer_mode()
    if codex["available"]:
        r.note(f"  codex path: {codex['path']}")
        r.note(f"  codex version: {codex.get('version')}")
        r.note(f"  codex authenticated: {codex.get('authenticated')}")
    else:
        r.note("  codex CLI: not found on PATH")

    if mode == "codex":
        r.step("codex reviewer", True)
    else:
        r.warn(
            "Codex unavailable — file-handoff fallback",
            codex.get("detail", "") + " (review will pause for a human/external reviewer to write review.md)",
        )

    r.note()
    r.note("DOCTOR PASS" if not r.failed else "DOCTOR FAIL")
    print(r.render())
    return 0 if not r.failed else 1


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------

def cmd_start(args):
    description = args.task
    if args.task_file:
        path = Path(args.task_file)
        if not path.exists():
            print(f"[FAIL] task file not found: {path}")
            return 1
        description = path.read_text(encoding="utf-8")

    if not description or not description.strip():
        print("[FAIL] no task description provided")
        return 1

    r = Reporter()
    r.title("LeadMe Collab")
    r.section("task")
    r.note(description.strip().splitlines()[0][:200])

    # --- Phase 1: pre-flight -------------------------------------------------
    r.section("preflight")
    if not r.step("primary repo exists", ld.repo_exists(REPO_PATH), str(REPO_PATH)):
        print(r.render())
        return 1

    branch = ld.current_branch(REPO_PATH)
    if not r.step("primary repo on main", branch == PRIMARY_BRANCH, branch or "unknown"):
        r.note("  (collab tasks branch off main via a worktree; switch back to main first)")
        print(r.render())
        return 1

    clean = ld.tracked_tree_clean(REPO_PATH)
    if clean is None or not r.step("primary tracked tree clean", bool(clean)):
        print(r.render())
        return 1

    known, unexpected = ld.classify_untracked(REPO_PATH)
    if known:
        r.note(f"  known untracked (ignored): {', '.join(known)}")
    if unexpected:
        r.warn("unexpected untracked files present (not blocking)", ", ".join(unexpected))

    fetch_ok, fetch_err = ld.fetch_origin(REPO_PATH, PRIMARY_BRANCH)
    if not r.step("fetch origin/main", fetch_ok, fetch_err if not fetch_ok else ""):
        print(r.render())
        return 1

    state, ahead, behind = ld.divergence_state(REPO_PATH, PRIMARY_BRANCH)
    if state == "diverged":
        r.step(f"branch divergence ({state})", False, f"ahead={ahead} behind={behind}")
        r.note("  main has diverged from origin/main — resolve manually before starting a task")
        print(r.render())
        return 1
    r.step(f"branch divergence ({state})", True, f"ahead={ahead} behind={behind}")

    base_sha = ld.head_sha(REPO_PATH)
    r.note(f"  base commit: {base_sha}")

    before_snapshot = primary_repo_snapshot()

    # --- Phase 2: safety / isolation ------------------------------------------
    tid = new_task_id(description)
    r.section("safety")

    ok, ref, err = create_safety_ref(base_sha, tid)
    if not r.step(f"safety ref {ref}", ok, err):
        print(r.render())
        return 1

    ok, wt_path, wt_branch, err = create_worktree(base_sha, tid)
    if not r.step(f"isolated worktree {wt_path}", ok, err):
        print(r.render())
        return 1

    wt_head = worktree_head(wt_path)
    r.step("worktree branch head matches base", wt_head == base_sha, f"{wt_head}")

    unchanged, before, after = primary_repo_unchanged(before_snapshot)
    if not r.step("primary repo untouched by isolation step", unchanged, f"before={before} after={after}"):
        print(r.render())
        return 1

    # --- Phase 3/4: state dir + task normalization -----------------------------
    task_md_text = build_task_md(description, base_sha)
    write_artifact(tid, "task.md", task_md_text)

    reviewer_mode, codex_info = choose_reviewer_mode()

    task_state = {
        "task_id": tid,
        "task_description": description.strip()[:2000],
        "repo": str(REPO_PATH),
        "base_branch": PRIMARY_BRANCH,
        "base_commit": base_sha,
        "safety_ref": ref,
        "worktree": str(wt_path),
        "branch": wt_branch,
        "phase": "isolated",
        "cycle": 1,
        "max_review_cycles": MAX_REVIEW_CYCLES,
        "reviewer_mode": reviewer_mode,
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
        "final": None,
    }
    write_task_state(tid, task_state)
    set_current_task(tid)
    log_event(tid, "task_created", f"base={base_sha} worktree={wt_path} branch={wt_branch}")

    r.note()
    r.note(f"  task id: {tid}")

    # --- Phase 5 onward: run the automated portion of the pipeline -------------
    result = _advance_task(tid, r)
    print(r.render())
    return 0 if result != "error" else 1


def _write_state_update(tid, **updates):
    state = read_task_state(tid) or {}
    state.update(updates)
    state["updated_at"] = utc_now_iso()
    write_task_state(tid, state)
    return state


def _advance_task(tid, r=None):
    """Run whatever automated phases are safe to run right now.

    Returns one of: "paused_review", "done", "needs_human", "error".
    Idempotent-ish: safe to call again from `resume` — it looks at
    state["phase"] and only does the next safe thing.
    """
    r = r or Reporter()
    state = read_task_state(tid)
    if state is None:
        r.step("task state readable", False, f"no state.json for {tid}")
        return "error"

    wt = state["worktree"]

    if state["phase"] == "isolated":
        r.section("claude implementation")
        task_md_text = read_artifact(tid, "task.md") or ""
        prompt = build_implementer_prompt(task_md_text)
        write_artifact(tid, "implementer-prompt.md", prompt)
        log_event(tid, "implementer_start")

        claude_info = discover_claude()
        if not claude_info["available"]:
            r.step("claude CLI available", False, "not found on PATH")
            _write_state_update(tid, phase="needs_human", final="NEEDS HUMAN")
            write_artifact(tid, "summary.md", _build_summary(tid))
            log_event(tid, "implementer_unavailable")
            return "needs_human"

        result = run_claude(prompt, cwd=wt)
        write_artifact(
            tid, "implementer-output.md",
            f"exit/is_error: {result['is_error']}\n"
            f"elapsed_seconds: {result['elapsed_seconds']}\n"
            f"total_cost_usd: {result['total_cost_usd']}\n\n"
            f"## Result text\n\n{result['result_text'] or '(none)'}\n\n"
            f"## Raw stdout\n\n{result['stdout']}\n\n"
            f"## stderr\n\n{result['stderr']}\n",
        )
        ok = result["is_error"] is False
        r.step("claude implementation complete", ok, "" if ok else "see implementer-output.md")
        log_event(tid, "implementer_done", f"is_error={result['is_error']} elapsed={result['elapsed_seconds']}s")

        diff_info = capture_diff(wt)
        write_artifact(tid, "diff.patch", diff_info["patch"])
        for f in diff_info["changed_files"]:
            r.note(f"  changed: {f}")
        if not diff_info["changed_files"]:
            r.warn("no files changed by implementer")

        verification_text, verify_ok = verify_changed_files(wt, diff_info["changed_files"])
        write_artifact(tid, "verification.md", verification_text)
        r.step("verification", verify_ok)

        if not ok:
            _write_state_update(tid, phase="needs_human", final="NEEDS HUMAN")
            write_artifact(tid, "summary.md", _build_summary(tid))
            return "needs_human"

        state = _write_state_update(tid, phase="implemented")

    if state["phase"] == "implemented":
        return _start_review_cycle(tid, r)

    if state["phase"] == "awaiting_review":
        return _try_resume_review(tid, r)

    if state["phase"] in ("done", "needs_human", "aborted", "failed"):
        r.note(f"  task already in terminal phase: {state['phase']}")
        return state["phase"]

    r.note(f"  unrecognized phase '{state['phase']}' — inspect state.json manually")
    return "error"


def _start_review_cycle(tid, r):
    state = read_task_state(tid)
    wt = state["worktree"]
    diff_info = capture_diff(wt)
    verification_text = read_artifact(tid, "verification.md") or ""
    task_md_text = read_artifact(tid, "task.md") or ""

    r.section(f"review cycle {state['cycle']}")

    prompt = build_review_prompt(task_md_text, diff_info, verification_text, reviewer_mode=state["reviewer_mode"])
    write_artifact(tid, "review-prompt.md", prompt)
    review_path = task_dir(tid) / "review.md"
    if review_path.exists():
        review_path.unlink()

    if state["reviewer_mode"] == "codex":
        codex_info = discover_codex()
        if not codex_info["usable_noninteractive"]:
            # Was usable when the task started; auth/availability changed
            # mid-task. Fail closed rather than silently downgrading.
            r.step("codex reviewer available", False, codex_info.get("detail", "unavailable"))
            _write_state_update(tid, phase="needs_human", final="NEEDS HUMAN")
            write_artifact(tid, "summary.md", _build_summary(tid))
            log_event(tid, "codex_unavailable_mid_task", codex_info.get("detail", ""))
            return "needs_human"

        log_event(tid, "codex_review_start", f"cycle={state['cycle']}")
        result = run_codex_review(prompt, cwd=wt)
        write_artifact(
            tid, "codex-raw-output.md",
            f"returncode: {result['returncode']}\n"
            f"elapsed_seconds: {result['elapsed_seconds']}\n\n"
            f"## Result text\n\n{result['result_text'] or '(none captured)'}\n\n"
            f"## stdout\n\n{result['stdout']}\n\n"
            f"## stderr\n\n{result['stderr']}\n",
        )
        r.step(
            "codex review completed",
            not result["is_error"],
            f"{result['elapsed_seconds']}s" if not result["is_error"] else "see codex-raw-output.md",
        )
        log_event(
            tid, "codex_review_done",
            f"cycle={state['cycle']} returncode={result['returncode']} elapsed={result['elapsed_seconds']}s",
        )
        # Preserve full review text regardless of outcome — never silently
        # upgrade a missing/malformed result into anything resembling PASS.
        write_artifact(tid, "review.md", result["result_text"] or "(no output captured from Codex)")

        _write_state_update(tid, phase="awaiting_review")
        return _try_resume_review(tid, r)

    # file-handoff fallback
    _write_state_update(tid, phase="awaiting_review")
    log_event(tid, "awaiting_review", f"cycle={state['cycle']}")

    r.warn(
        "no automated reviewer available — pausing for file-handoff",
        f"paste {task_dir(tid) / 'review-prompt.md'} into your reviewer, "
        f"save its answer to {review_path}, then run: leadme-collab resume",
    )
    return "paused_review"


def _try_resume_review(tid, r):
    state = read_task_state(tid)
    review_path = task_dir(tid) / "review.md"

    if not review_path.exists():
        r.section(f"review cycle {state['cycle']}")
        r.warn("still waiting for review.md", str(review_path))
        return "paused_review"

    review_text = review_path.read_text(encoding="utf-8")
    verdict = parse_verdict(review_text)

    r.section(f"review cycle {state['cycle']}")
    if verdict is None:
        r.step("review verdict parseable", False, "no 'VERDICT: ...' line found — failing closed")
        _write_state_update(tid, phase="needs_human", final="NEEDS HUMAN")
        write_artifact(tid, "summary.md", _build_summary(tid))
        return "needs_human"

    r.step(f"verdict: {verdict}", verdict == "PASS")
    log_event(tid, "review_verdict", f"cycle={state['cycle']} verdict={verdict}")

    # Archive this cycle's review so repeated cycles don't clobber history.
    (task_dir(tid) / f"review-cycle-{state['cycle']}.md").write_text(review_text, encoding="utf-8")

    if verdict == "PASS":
        _write_state_update(tid, phase="done", final="READY FOR HUMAN REVIEW")
        write_artifact(tid, "summary.md", _build_summary(tid))
        return "done"

    if verdict == "FAIL":
        _write_state_update(tid, phase="needs_human", final="FAILED")
        write_artifact(tid, "summary.md", _build_summary(tid))
        return "needs_human"

    if verdict == "NEEDS HUMAN":
        _write_state_update(tid, phase="needs_human", final="NEEDS HUMAN")
        write_artifact(tid, "summary.md", _build_summary(tid))
        return "needs_human"

    # NEEDS FIX
    if state["cycle"] >= state["max_review_cycles"]:
        r.warn("max review cycles reached without PASS", str(state["max_review_cycles"]))
        _write_state_update(tid, phase="needs_human", final="NEEDS HUMAN")
        write_artifact(tid, "summary.md", _build_summary(tid))
        return "needs_human"

    return _run_repair(tid, r, review_text)


def _run_repair(tid, r, review_text):
    state = read_task_state(tid)
    wt = state["worktree"]
    diff_info = capture_diff(wt)
    task_md_text = read_artifact(tid, "task.md") or ""

    r.section("claude repair")
    prompt = build_repair_prompt(task_md_text, review_text, diff_info)
    write_artifact(tid, "repair-prompt.md", prompt)
    log_event(tid, "repair_start", f"cycle={state['cycle']}")

    claude_info = discover_claude()
    if not claude_info["available"]:
        r.step("claude CLI available", False, "not found on PATH")
        _write_state_update(tid, phase="needs_human", final="NEEDS HUMAN")
        write_artifact(tid, "summary.md", _build_summary(tid))
        return "needs_human"

    result = run_claude(prompt, cwd=wt)
    write_artifact(
        tid, "repair-output.md",
        f"exit/is_error: {result['is_error']}\n"
        f"elapsed_seconds: {result['elapsed_seconds']}\n"
        f"total_cost_usd: {result['total_cost_usd']}\n\n"
        f"## Result text\n\n{result['result_text'] or '(none)'}\n\n"
        f"## Raw stdout\n\n{result['stdout']}\n\n"
        f"## stderr\n\n{result['stderr']}\n",
    )
    ok = result["is_error"] is False
    r.step("targeted repair complete", ok, "" if ok else "see repair-output.md")
    log_event(tid, "repair_done", f"cycle={state['cycle']} is_error={result['is_error']}")

    new_diff = capture_diff(wt)
    write_artifact(tid, "diff.patch", new_diff["patch"])
    verification_text, verify_ok = verify_changed_files(wt, new_diff["changed_files"])
    write_artifact(tid, "verification.md", verification_text)
    r.step("verification after repair", verify_ok)

    if not ok:
        _write_state_update(tid, phase="needs_human", final="NEEDS HUMAN")
        write_artifact(tid, "summary.md", _build_summary(tid))
        return "needs_human"

    next_cycle = state["cycle"] + 1
    _write_state_update(tid, phase="implemented", cycle=next_cycle)
    return _start_review_cycle(tid, r)


def _build_summary(tid):
    state = read_task_state(tid) or {}
    diff_stat = ""
    changed_files = []
    wt = state.get("worktree")
    if wt and Path(wt).exists():
        info = capture_diff(wt)
        diff_stat = info["stat"]
        changed_files = info["changed_files"]

    final = state.get("final") or "IN PROGRESS"
    lines = [
        "LeadMe Collab",
        "─" * 24,
        "",
        "TASK",
        state.get("task_description", ""),
        "",
        "BASE",
        state.get("base_commit", ""),
        "",
        "WORKTREE",
        state.get("worktree", ""),
        "",
        "BRANCH",
        state.get("branch", ""),
        "",
        f"FINAL",
        final,
        "",
        "Files changed:",
        "\n".join(changed_files) if changed_files else "(none)",
        "",
        "Diff stat:",
        diff_stat or "(none)",
        "",
        "Commits: none",
        "Push: none",
        "Deploy: none",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# status / resume / inspect / abort / list
# ---------------------------------------------------------------------------

def cmd_status(args):
    tid = resolve_task_id(args.task_id)
    if not tid:
        print("[FAIL] no tasks found")
        return 1
    state = read_task_state(tid)
    if state is None:
        print(f"[FAIL] no state found for task {tid}")
        return 1

    diff_info = {"changed_files": [], "stat": ""}
    wt = state.get("worktree")
    if wt and Path(wt).exists():
        diff_info = capture_diff(wt)

    r = Reporter()
    r.title("LeadMe Collab Status")
    r.note(f"  task id:      {tid}")
    r.note(f"  phase:        {state.get('phase')}")
    r.note(f"  cycle:        {state.get('cycle')}/{state.get('max_review_cycles')}")
    r.note(f"  worktree:     {state.get('worktree')}")
    r.note(f"  branch:       {state.get('branch')}")
    r.note(f"  reviewer:     {state.get('reviewer_mode')}")
    r.note(f"  final:        {state.get('final') or '(in progress)'}")
    r.note(f"  changed:      {', '.join(diff_info['changed_files']) or '(none)'}")

    events_path = task_dir(tid) / EVENTS_LOG_NAME
    if events_path.exists():
        lines = events_path.read_text(encoding="utf-8").splitlines()
        if lines:
            r.note(f"  last event:   {lines[-1]}")

    print(r.render())
    return 0


def cmd_resume(args):
    tid = resolve_task_id(args.task_id)
    if not tid:
        print("[FAIL] no tasks found")
        return 1
    state = read_task_state(tid)
    if state is None:
        print(f"[FAIL] no state found for task {tid}")
        return 1

    set_current_task(tid)
    r = Reporter()
    r.title("LeadMe Collab Resume")
    r.note(f"  task id: {tid}")
    r.note(f"  resuming from phase: {state.get('phase')}")

    if state.get("phase") in ("done", "needs_human", "aborted", "failed"):
        r.note("  task already in a terminal phase — nothing to resume")
        print(r.render())
        return 0

    result = _advance_task(tid, r)
    print(r.render())
    return 0 if result not in ("error",) else 1


def cmd_inspect(args):
    tid = resolve_task_id(args.task_id)
    if not tid:
        print("[FAIL] no tasks found")
        return 1
    state = read_task_state(tid)
    if state is None:
        print(f"[FAIL] no state found for task {tid}")
        return 1

    wt = state.get("worktree")
    diff_info = {"changed_files": [], "stat": ""}
    if wt and Path(wt).exists():
        diff_info = capture_diff(wt)

    r = Reporter()
    r.title("LeadMe Collab Inspect")
    r.note(f"  task:      {state.get('task_description')}")
    r.note(f"  worktree:  {wt}")
    r.note(f"  branch:    {state.get('branch')}")
    r.note("")
    r.note("  diff stat:")
    r.note(f"    {diff_info['stat'] or '(none)'}")
    r.note("")
    r.note(f"  changed files: {', '.join(diff_info['changed_files']) or '(none)'}")

    latest_review = None
    cycle = state.get("cycle", 1)
    for c in range(cycle, 0, -1):
        candidate = task_dir(tid) / f"review-cycle-{c}.md"
        if candidate.exists():
            latest_review = candidate
            break
    r.note(f"  latest review: {latest_review or '(none yet)'}")

    summary_path = task_dir(tid) / "summary.md"
    r.note(f"  summary path:  {summary_path if summary_path.exists() else '(not written yet)'}")

    print(r.render())
    return 0


def cmd_abort(args):
    tid = resolve_task_id(args.task_id)
    if not tid:
        print("[FAIL] no tasks found")
        return 1
    state = read_task_state(tid)
    if state is None:
        print(f"[FAIL] no state found for task {tid}")
        return 1

    _write_state_update(tid, phase="aborted", final=state.get("final") or "ABORTED")
    log_event(tid, "aborted", "cleanup=" + ("yes" if args.cleanup else "no"))

    r = Reporter()
    r.title("LeadMe Collab Abort")
    r.note(f"  task id: {tid}")
    if args.cleanup:
        r.note("  marked aborted — state preserved, worktree/branch will be removed below")
    else:
        r.note("  marked aborted — worktree and state preserved")

    wt = Path(state["worktree"])
    branch = state["branch"]

    if args.cleanup:
        r.section("cleanup")
        res = remove_worktree(wt, force=True)
        r.step("worktree removed", res.ok, (res.stdout + res.stderr).strip() if not res.ok else "")
        res2 = delete_branch(branch, force=True)
        r.step("task branch deleted", res2.ok, (res2.stdout + res2.stderr).strip() if not res2.ok else "")
        r.note(f"  safety ref preserved: {state['safety_ref']}")
        r.note(f"  state directory preserved: {task_dir(tid)}")
    else:
        r.section("manual cleanup (not run automatically)")
        r.note(f"  git -C {REPO_PATH} worktree remove {wt} --force")
        r.note(f"  git -C {REPO_PATH} branch -D {branch}")
        r.note(f"  (safety ref {state['safety_ref']} is left in place either way)")
        r.note("  re-run with --cleanup to perform the worktree/branch removal now")

    print(r.render())
    return 0


def cmd_list(args):
    ids = list_task_ids()
    r = Reporter()
    r.title("LeadMe Collab Tasks")
    if not ids:
        r.note("  (no tasks yet)")
        print(r.render())
        return 0

    for tid in ids:
        state = read_task_state(tid) or {}
        summary = (state.get("task_description") or "").splitlines()[0][:60] if state.get("task_description") else ""
        r.note(
            f"  {tid}  [{state.get('phase', '?')}]  cycle={state.get('cycle', '?')}  "
            f"created={state.get('created_at', '?')}"
        )
        r.note(f"    {summary}")
        r.note(f"    worktree: {state.get('worktree', '?')}")

    print(r.render())
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(prog="leadme-collab", description="Implement/review/repair loop automation.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="diagnose the collab pipeline")

    p_start = sub.add_parser("start", help="start a new task")
    p_start.add_argument("task", nargs="?", default=None, help="task description")
    p_start.add_argument("--task-file", default=None, help="read task description from a file")

    p_status = sub.add_parser("status", help="show current task status")
    p_status.add_argument("task_id", nargs="?", default=None)

    p_resume = sub.add_parser("resume", help="resume an interrupted task")
    p_resume.add_argument("task_id", nargs="?", default=None)

    p_inspect = sub.add_parser("inspect", help="inspect diff/review/summary for a task")
    p_inspect.add_argument("task_id", nargs="?", default=None)

    p_abort = sub.add_parser("abort", help="mark a task aborted")
    p_abort.add_argument("task_id", nargs="?", default=None)
    p_abort.add_argument("--cleanup", action="store_true", help="also remove the worktree and task branch")

    sub.add_parser("list", help="list recent tasks")

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        return cmd_doctor(args)
    if args.command == "start":
        return cmd_start(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "resume":
        return cmd_resume(args)
    if args.command == "inspect":
        return cmd_inspect(args)
    if args.command == "abort":
        return cmd_abort(args)
    if args.command == "list":
        return cmd_list(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
