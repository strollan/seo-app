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
- Quick git/status commands go through leadme_deploy.run_local() (imported,
  not duplicated). Claude/Codex invocations go through run_monitored() below
  instead: they can run for minutes, so they need a visible heartbeat, a
  timeout that can actually terminate the process group, and a PID recorded
  in state.json for `status`/`abort` to act on — none of which a single
  blocking subprocess.run() call can provide.
- No automatic push/merge/commit/deploy anywhere in this module. Final
  state is always "hand it to a human", never "it's live".
- Codex CLI is installed and authenticated; discover_codex() proves this
  live (install + `codex login status`), not just `command -v codex`. If it
  isn't usable, the reviewer adapter falls back to file-handoff: a
  self-contained review-prompt.md is written and the task pauses for
  `resume` once a human/other reviewer drops a review.md verdict in place.

First-real-use repair note: the first real task run exposed that a fully
silent, blocking subprocess call is indistinguishable from a hang — when the
foreground terminal job got suspended (Ctrl-Z / focus loss), both the parent
process and its child ended up stopped with zero output, and the operator
(reasonably) launched duplicate attempts. Reporter now streams every line as
it happens instead of buffering until the end, run_monitored() prints a
`[RUNNING] <role> — Ns` heartbeat and records PID/timeout/last-heartbeat in
state.json, `start` detects an existing non-terminal task with a matching
description before creating a duplicate, and `abort` kills any live child
process it finds recorded for the task.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
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

HEARTBEAT_INTERVAL_SECONDS = 20  # how often [RUNNING] progress prints while waiting
POLL_INTERVAL_SECONDS = 1.0  # how often run_monitored() checks whether the child exited

# Non-terminal phases: a task in one of these is still "active" for the
# purposes of duplicate-start detection and stale-process checks.
NON_TERMINAL_PHASES = {"starting", "isolated", "implementing", "implemented", "awaiting_review", "repairing"}
TERMINAL_PHASES = {"done", "needs_human", "aborted", "failed"}

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
    """Prints each line as it's produced (stream=True, the default) so a
    long-running task never goes silent — the first-real-use failure was a
    fully-buffered report that printed nothing until the whole task
    finished, making a suspended process indistinguishable from progress.
    `render()` still returns the full accumulated text for callers/tests
    that want it; callers should NOT also print(r.render()) when streaming,
    since that would duplicate every line.
    """

    def __init__(self, stream=True):
        self.lines = []
        self.failed = False
        self.stream = stream

    def _emit(self, text):
        self.lines.append(text)
        if self.stream:
            print(text, flush=True)

    def title(self, text):
        self._emit(text)
        self._emit("─" * max(24, len(text)))

    def section(self, name):
        self._emit("")
        self._emit(name.upper())

    def step(self, label, ok, detail=""):
        tag = "PASS" if ok else "FAIL"
        if not ok:
            self.failed = True
        text = f"[{tag}] {label}"
        if detail:
            text += f" — {detail}"
        self._emit(text)
        return ok

    def warn(self, label, detail=""):
        text = f"[WARN] {label}"
        if detail:
            text += f" — {detail}"
        self._emit(text)

    def note(self, text=""):
        self._emit(text)

    def render(self):
        return "\n".join(self.lines)


# ---------------------------------------------------------------------------
# Task state (state.json) + events.log
# ---------------------------------------------------------------------------

def _scan_for_secrets(value):
    """Recursively refuse anything secret-shaped, including inside nested
    dicts/lists (e.g. the "active_process" sub-object)."""
    if isinstance(value, str):
        if SECRET_LIKE_KEY_PATTERN.search(value):
            raise ValueError("refusing to persist a value that looks secret-shaped")
    elif isinstance(value, dict):
        for key, sub_value in value.items():
            if SECRET_LIKE_KEY_PATTERN.search(str(key)):
                raise ValueError(f"refusing to persist unexpected/secret-shaped state key: {key}")
            _scan_for_secrets(sub_value)
    elif isinstance(value, list):
        for item in value:
            _scan_for_secrets(item)


def write_task_state(tid, data):
    for value in data.values():
        _scan_for_secrets(value)
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
# Monitored subprocess execution (heartbeat + hard timeout + PID tracking)
# ---------------------------------------------------------------------------
#
# Claude/Codex calls can legitimately run for minutes. A single blocking
# subprocess.run() call is exactly what caused the first-real-use failure:
# when the foreground terminal job got suspended (Ctrl-Z / focus loss), BOTH
# the parent leadme-collab process and its child ended up in Linux "T"
# (stopped) state simultaneously — and since subprocess.run()'s own timeout
# enforcement runs as code *inside* the (now-stopped) parent, it never got a
# chance to fire. A stopped process and a slow-but-running one look
# identical when nothing is printed, so the operator reasonably assumed a
# hang and launched duplicate attempts.
#
# run_monitored() addresses this by polling instead of blocking: it prints a
# heartbeat, records the PID and last-heartbeat time into state.json (so
# `status` can show it and detect staleness even from a fresh process), and
# enforces the timeout itself by killing the whole process group. This does
# NOT make a suspended process group resumable — nothing running inside that
# group can — but it makes the difference between "still working" and
# "something is wrong" observable in real time, and it guarantees a clean
# kill for the ordinary case (a child that's actually just slow/hung while
# its parent keeps running normally).

def _terminate_process_group(proc):
    """SIGTERM then SIGKILL the whole process group, not just the direct
    child — codex/claude may spawn their own subprocesses."""
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    time.sleep(2)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _set_active_process(tid, role, pid, timeout_seconds):
    if not tid:
        return
    state = read_task_state(tid)
    if state is None:
        return
    now = utc_now_iso()
    state["active_process"] = {
        "role": role,
        "pid": pid,
        "started_at": now,
        "last_heartbeat_at": now,
        "timeout_seconds": timeout_seconds,
    }
    state["updated_at"] = now
    write_task_state(tid, state)


def _touch_active_process_heartbeat(tid):
    if not tid:
        return
    state = read_task_state(tid)
    if state is None or "active_process" not in state:
        return
    state["active_process"]["last_heartbeat_at"] = utc_now_iso()
    write_task_state(tid, state)


def _clear_active_process(tid):
    if not tid:
        return
    state = read_task_state(tid)
    if state is None or "active_process" not in state:
        return
    del state["active_process"]
    write_task_state(tid, state)


def process_is_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, TypeError, OverflowError):
        return False
    except OSError:
        return False
    return True


def run_monitored(args, cwd, timeout, role_label, input_text=None, tid=None,
                   heartbeat_interval=HEARTBEAT_INTERVAL_SECONDS,
                   poll_interval=POLL_INTERVAL_SECONDS):
    """Run `args` in its own process group, polling rather than blocking.

    Prints "[RUNNING] <role_label> — Ns" roughly every heartbeat_interval
    seconds (never more often). If `tid` is given, records
    role/pid/started_at/last_heartbeat_at/timeout_seconds into that task's
    state.json under "active_process" while running, and clears it when
    done — this is what lets `status` show live progress and detect a
    stale/dead process later.

    stdout/stderr are captured to temp files (not PIPE) so polling doesn't
    risk a pipe-buffer deadlock. Returns a dict: returncode, stdout, stderr,
    elapsed_seconds, pid, timed_out.
    """
    with tempfile.TemporaryDirectory(prefix="leadme-collab-run-") as tmp:
        stdout_path = Path(tmp) / "stdout.txt"
        stderr_path = Path(tmp) / "stderr.txt"

        out_f = open(stdout_path, "wb")
        err_f = open(stderr_path, "wb")
        try:
            proc = subprocess.Popen(
                args,
                cwd=str(cwd) if cwd else None,
                stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                stdout=out_f,
                stderr=err_f,
                start_new_session=True,  # own process group -> killable as a unit
            )
        finally:
            out_f.close()
            err_f.close()

        if input_text is not None:
            try:
                proc.stdin.write(input_text.encode("utf-8"))
            except (BrokenPipeError, OSError):
                pass
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass

        _set_active_process(tid, role_label, proc.pid, timeout)

        start = time.monotonic()
        last_heartbeat = start
        timed_out = False

        while True:
            ret = proc.poll()
            now = time.monotonic()
            elapsed = now - start

            if ret is not None:
                break

            if elapsed >= timeout:
                timed_out = True
                _terminate_process_group(proc)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                break

            if now - last_heartbeat >= heartbeat_interval:
                print(f"[RUNNING] {role_label} — {int(elapsed)}s", flush=True)
                last_heartbeat = now
                _touch_active_process_heartbeat(tid)

            time.sleep(poll_interval)

        elapsed_total = time.monotonic() - start
        stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.exists() else ""
        stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.exists() else ""

    _clear_active_process(tid)

    return {
        "returncode": proc.returncode if proc.returncode is not None else -9,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "elapsed_seconds": round(elapsed_total, 1),
        "pid": proc.pid,
        "timed_out": timed_out,
    }


# ---------------------------------------------------------------------------
# Claude implementer adapter
# ---------------------------------------------------------------------------

def run_claude(prompt_text, cwd, timeout=CLAUDE_TIMEOUT_SECONDS, tid=None):
    """Invoke the real Claude CLI non-interactively in `cwd`.

    Returns a dict: stdout, stderr, returncode, elapsed_seconds, result_text,
    is_error, total_cost_usd (best-effort; parsed from --output-format
    json), timed_out, pid, reason (non-empty only when is_error).
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
    result = run_monitored(args, cwd=cwd, timeout=timeout, role_label="Claude implementer", tid=tid)

    parsed = None
    if not result["timed_out"] and result["returncode"] == 0:
        try:
            parsed = json.loads(result["stdout"])
        except (ValueError, TypeError):
            parsed = None

    if result["timed_out"]:
        reason = f"Claude implementer timed out after {int(result['elapsed_seconds'])}s (limit: {timeout}s)"
    elif parsed is None:
        reason = f"Claude implementer exited {result['returncode']} without a parseable JSON result"
    elif parsed.get("is_error"):
        reason = "Claude implementer reported is_error=true — see implementer-output.md"
    else:
        reason = ""

    return {
        "returncode": result["returncode"],
        "stdout": result["stdout"],
        "stderr": result["stderr"],
        "elapsed_seconds": result["elapsed_seconds"],
        "result_text": parsed.get("result") if parsed else None,
        "is_error": bool(reason),
        "total_cost_usd": parsed.get("total_cost_usd") if parsed else None,
        "parsed": parsed,
        "timed_out": result["timed_out"],
        "pid": result["pid"],
        "reason": reason,
    }


def run_codex_review(prompt_text, cwd, timeout=CODEX_TIMEOUT_SECONDS, tid=None):
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
        result = run_monitored(
            args, cwd=cwd, timeout=timeout, role_label="Codex reviewer",
            input_text=prompt_text, tid=tid,
        )

        result_text = None
        if output_path.exists():
            try:
                result_text = output_path.read_text(encoding="utf-8").strip()
            except OSError:
                result_text = None

    if result["timed_out"]:
        reason = f"Codex reviewer timed out after {int(result['elapsed_seconds'])}s (limit: {timeout}s)"
    elif result["returncode"] != 0:
        reason = f"Codex reviewer exited {result['returncode']} — see codex-raw-output.md"
    elif not result_text:
        reason = "Codex reviewer produced no output — see codex-raw-output.md"
    else:
        reason = ""

    return {
        "returncode": result["returncode"],
        "stdout": result["stdout"],
        "stderr": result["stderr"],
        "elapsed_seconds": result["elapsed_seconds"],
        "result_text": result_text,
        "is_error": bool(reason),
        "timed_out": result["timed_out"],
        "pid": result["pid"],
        "reason": reason,
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
    return 0 if not r.failed else 1


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------

def normalize_task_text(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def find_active_duplicate(description):
    """Return (tid, state) for an existing non-terminal task whose
    description matches (normalized) the given one, or (None, None)."""
    normalized = normalize_task_text(description)
    if not normalized:
        return None, None
    for tid in list_task_ids():
        candidate = read_task_state(tid)
        if not candidate:
            continue
        if candidate.get("phase") in TERMINAL_PHASES:
            continue
        if normalize_task_text(candidate.get("task_description", "")) == normalized:
            return tid, candidate
    return None, None


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

    if not getattr(args, "force_new", False):
        dup_tid, dup_state = find_active_duplicate(description)
        if dup_tid:
            print("[FAIL] an active task with a matching description already exists — not starting a duplicate")
            print(f"  existing task id: {dup_tid}")
            print(f"  phase:            {dup_state.get('phase')}")
            print(f"  created:          {dup_state.get('created_at')}")
            print("  next steps:")
            print(f"    leadme-collab status {dup_tid}")
            print(f"    leadme-collab resume {dup_tid}")
            print(f"    leadme-collab abort {dup_tid}")
            print("  (pass --force-new to start a new task anyway)")
            return 1

    r = Reporter()
    r.title("LeadMe Collab")
    r.section("task")
    r.note(description.strip().splitlines()[0][:200])

    # --- Phase 1: pre-flight -------------------------------------------------
    r.section("preflight")
    if not r.step("primary repo exists", ld.repo_exists(REPO_PATH), str(REPO_PATH)):
        return 1

    branch = ld.current_branch(REPO_PATH)
    if not r.step("primary repo on main", branch == PRIMARY_BRANCH, branch or "unknown"):
        r.note("  (collab tasks branch off main via a worktree; switch back to main first)")
        return 1

    clean = ld.tracked_tree_clean(REPO_PATH)
    if clean is None or not r.step("primary tracked tree clean", bool(clean)):
        return 1

    known, unexpected = ld.classify_untracked(REPO_PATH)
    if known:
        r.note(f"  known untracked (ignored): {', '.join(known)}")
    if unexpected:
        r.warn("unexpected untracked files present (not blocking)", ", ".join(unexpected))

    fetch_ok, fetch_err = ld.fetch_origin(REPO_PATH, PRIMARY_BRANCH)
    if not r.step("fetch origin/main", fetch_ok, fetch_err if not fetch_ok else ""):
        return 1

    divergence, ahead, behind = ld.divergence_state(REPO_PATH, PRIMARY_BRANCH)
    if divergence == "diverged":
        r.step(f"branch divergence ({divergence})", False, f"ahead={ahead} behind={behind}")
        r.note("  main has diverged from origin/main — resolve manually before starting a task")
        return 1
    r.step(f"branch divergence ({divergence})", True, f"ahead={ahead} behind={behind}")

    base_sha = ld.head_sha(REPO_PATH)
    r.note(f"  base commit: {base_sha}")

    before_snapshot = primary_repo_snapshot()

    # --- Phase 2: safety / isolation ------------------------------------------
    tid = new_task_id(description)
    r.section("safety")
    r.note(f"  task id: {tid}")

    # Write a minimal, early state record BEFORE the potentially slow git
    # operations below, so a stall here is still visible to `list`/`status`/
    # duplicate-detection — the first-real-use failure included a 4th
    # attempt that stalled inside `git worktree add` and left NO state.json
    # at all, making it invisible to every command that would normally have
    # helped (an incomplete, "locked" worktree with a real safety ref, but
    # nothing to `list` or `abort`).
    write_task_state(tid, {
        "task_id": tid,
        "task_description": description.strip()[:2000],
        "repo": str(REPO_PATH),
        "base_branch": PRIMARY_BRANCH,
        "base_commit": base_sha,
        "safety_ref": safety_ref_name(tid),
        "worktree": str(worktree_dir(tid)),
        "branch": branch_name(tid),
        "phase": "starting",
        "cycle": 1,
        "max_review_cycles": MAX_REVIEW_CYCLES,
        "reviewer_mode": None,
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
        "final": None,
    })
    set_current_task(tid)
    log_event(tid, "task_starting", f"base={base_sha}")

    ok, ref, err = create_safety_ref(base_sha, tid)
    if not r.step(f"safety ref {ref}", ok, err):
        _finalize(tid, "failed", "FAILED", f"could not create safety ref: {err}")
        return 1

    ok, wt_path, wt_branch, err = create_worktree(base_sha, tid)
    if not r.step(f"isolated worktree {wt_path}", ok, err):
        _finalize(tid, "failed", "FAILED", f"could not create isolated worktree: {err}")
        return 1

    wt_head = worktree_head(wt_path)
    r.step("worktree branch head matches base", wt_head == base_sha, f"{wt_head}")

    unchanged, before, after = primary_repo_unchanged(before_snapshot)
    if not r.step("primary repo untouched by isolation step", unchanged, f"before={before} after={after}"):
        _finalize(tid, "failed", "FAILED", f"primary repo changed during isolation: before={before} after={after}")
        return 1

    # --- Phase 3/4: state dir + task normalization -----------------------------
    task_md_text = build_task_md(description, base_sha)
    write_artifact(tid, "task.md", task_md_text)

    reviewer_mode, codex_info = choose_reviewer_mode()

    _write_state_update(tid, phase="isolated", reviewer_mode=reviewer_mode)
    log_event(tid, "task_isolated", f"worktree={wt_path} branch={wt_branch} reviewer_mode={reviewer_mode}")

    # --- Phase 5 onward: run the automated portion of the pipeline -------------
    result = _advance_task(tid, r)
    return 0 if result != "error" else 1


def _write_state_update(tid, **updates):
    state = read_task_state(tid) or {}
    state.update(updates)
    state["updated_at"] = utc_now_iso()
    write_task_state(tid, state)
    return state


def _print_explanation(r, reason, detail_path=None, next_action=None):
    """Always show a concrete reason for a non-PASS/error outcome — never
    just "[FAIL] verdict: NEEDS HUMAN" with nothing else."""
    r.note(f"  reason: {reason}")
    if detail_path:
        r.note(f"  details: {detail_path}")
    if next_action:
        r.note(f"  next action: {next_action}")


def _finalize(tid, phase, final, reason, detail_path=None):
    _write_state_update(tid, phase=phase, final=final, final_reason=reason)
    write_artifact(tid, "summary.md", _build_summary(tid))


def _check_no_concurrent_process(tid, state, r):
    """Refuse to proceed if a process already recorded for this task is
    still alive (a genuine concurrent run); if the recorded PID is dead,
    explain that it's a stale record from a previous stalled attempt and
    clear it so the caller can safely proceed. Returns True iff safe to
    continue."""
    active = state.get("active_process")
    if not active:
        return True
    pid = active.get("pid")
    role = active.get("role", "process")
    if process_is_alive(pid):
        r.step(
            f"no other {role} already running for this task",
            False,
            f"pid {pid} is still alive (started {active.get('started_at')}) — "
            f"wait for it to finish, or inspect it with: ps -p {pid}",
        )
        return False
    r.warn(
        f"previous {role} (pid {pid}) is no longer running",
        f"last heartbeat: {active.get('last_heartbeat_at')} — treating as failed, retrying",
    )
    log_event(tid, "stale_active_process_cleared", f"role={role} pid={pid}")
    _clear_active_process(tid)
    return True


def _advance_task(tid, r=None):
    """Run whatever automated phases are safe to run right now.

    Returns one of: "paused_review", "done", "needs_human", "failed", "error".
    Idempotent-ish: safe to call again from `resume` — it looks at
    state["phase"] and only does the next safe thing, explaining what (if
    anything) it's retrying rather than blindly repeating completed work.
    """
    r = r or Reporter()
    state = read_task_state(tid)
    if state is None:
        r.step("task state readable", False, f"no state.json for {tid}")
        return "error"

    wt = state["worktree"]

    if state["phase"] == "isolated":
        r.section("claude implementation")
        if not _check_no_concurrent_process(tid, state, r):
            return "needs_human"

        task_md_text = read_artifact(tid, "task.md") or ""
        prompt = build_implementer_prompt(task_md_text)
        write_artifact(tid, "implementer-prompt.md", prompt)
        log_event(tid, "implementer_start")

        claude_info = discover_claude()
        if not claude_info["available"]:
            r.step("claude CLI available", False, "not found on PATH")
            reason = "claude CLI not found on PATH — cannot run the implementer step"
            _print_explanation(r, reason, next_action="install/fix the claude CLI, then: leadme-collab resume")
            _finalize(tid, "needs_human", "NEEDS HUMAN", reason)
            log_event(tid, "implementer_unavailable")
            return "needs_human"

        result = run_claude(prompt, cwd=wt, tid=tid)
        write_artifact(
            tid, "implementer-output.md",
            f"exit/is_error: {result['is_error']}\n"
            f"elapsed_seconds: {result['elapsed_seconds']}\n"
            f"total_cost_usd: {result['total_cost_usd']}\n\n"
            f"## Result text\n\n{result['result_text'] or '(none)'}\n\n"
            f"## Raw stdout\n\n{result['stdout']}\n\n"
            f"## stderr\n\n{result['stderr']}\n",
        )
        ok = not result["is_error"]
        r.step("claude implementation complete", ok, "" if ok else result.get("reason", ""))
        log_event(tid, "implementer_done", f"is_error={result['is_error']} elapsed={result['elapsed_seconds']}s")

        # Capture diff/verification regardless of outcome — even a failed
        # implementer run may have made partial edits worth seeing.
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
            reason = result.get("reason") or "Claude implementer failed"
            _print_explanation(
                r, reason,
                detail_path=str(task_dir(tid) / "implementer-output.md"),
                next_action=f"leadme-collab inspect {tid}",
            )
            _finalize(tid, "needs_human", "NEEDS HUMAN", reason)
            return "needs_human"

        state = _write_state_update(tid, phase="implemented")

    if state["phase"] == "implemented":
        return _start_review_cycle(tid, r)

    if state["phase"] == "awaiting_review":
        return _try_resume_review(tid, r)

    if state["phase"] in TERMINAL_PHASES:
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
    if not _check_no_concurrent_process(tid, state, r):
        return "needs_human"

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
            reason = f"Codex reviewer became unavailable mid-task: {codex_info.get('detail', 'unknown reason')}"
            _print_explanation(r, reason, next_action=f"fix Codex, then: leadme-collab resume {tid}")
            _finalize(tid, "needs_human", "NEEDS HUMAN", reason)
            log_event(tid, "codex_unavailable_mid_task", codex_info.get("detail", ""))
            return "needs_human"

        log_event(tid, "codex_review_start", f"cycle={state['cycle']}")
        result = run_codex_review(prompt, cwd=wt, tid=tid)
        write_artifact(
            tid, "codex-raw-output.md",
            f"returncode: {result['returncode']}\n"
            f"elapsed_seconds: {result['elapsed_seconds']}\n"
            f"timed_out: {result['timed_out']}\n\n"
            f"## Result text\n\n{result['result_text'] or '(none captured)'}\n\n"
            f"## stdout\n\n{result['stdout']}\n\n"
            f"## stderr\n\n{result['stderr']}\n",
        )
        r.step(
            "codex review completed",
            not result["is_error"],
            f"{result['elapsed_seconds']}s" if not result["is_error"] else result.get("reason", ""),
        )
        log_event(
            tid, "codex_review_done",
            f"cycle={state['cycle']} returncode={result['returncode']} "
            f"elapsed={result['elapsed_seconds']}s timed_out={result['timed_out']}",
        )
        # Preserve full review text regardless of outcome — never silently
        # upgrade a missing/malformed result into anything resembling PASS.
        write_artifact(tid, "review.md", result["result_text"] or "(no output captured from Codex)")

        if result["is_error"]:
            reason = result.get("reason") or "Codex reviewer failed"
            _print_explanation(
                r, reason,
                detail_path=str(task_dir(tid) / "codex-raw-output.md"),
                next_action=f"leadme-collab inspect {tid}",
            )
            _finalize(tid, "needs_human", "NEEDS HUMAN", reason)
            return "needs_human"

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
        reason = "reviewer response had no 'VERDICT: ...' line — cannot be trusted as PASS or FAIL"
        excerpt = review_text.strip().splitlines()[0][:200] if review_text.strip() else "(empty response)"
        _print_explanation(
            r, reason,
            detail_path=str(review_path),
            next_action=f"read {review_path} and decide manually, or re-run the reviewer",
        )
        r.note(f"  reviewer said: {excerpt}")
        _finalize(tid, "needs_human", "NEEDS HUMAN", reason)
        return "needs_human"

    r.step(f"verdict: {verdict}", verdict == "PASS")
    log_event(tid, "review_verdict", f"cycle={state['cycle']} verdict={verdict}")

    # Archive this cycle's review so repeated cycles don't clobber history.
    (task_dir(tid) / f"review-cycle-{state['cycle']}.md").write_text(review_text, encoding="utf-8")

    excerpt = "\n".join(review_text.strip().splitlines()[1:4]).strip()

    if verdict == "PASS":
        _finalize(tid, "done", "READY FOR HUMAN REVIEW", "reviewer verdict: PASS")
        return "done"

    if verdict == "FAIL":
        reason = "reviewer verdict: FAIL (judged unsalvageable, not just needing a fix)"
        _print_explanation(r, reason, detail_path=str(review_path), next_action=f"leadme-collab inspect {tid}")
        if excerpt:
            r.note(f"  reviewer findings: {excerpt}")
        _finalize(tid, "needs_human", "FAILED", reason)
        return "needs_human"

    if verdict == "NEEDS HUMAN":
        reason = "reviewer verdict: NEEDS HUMAN"
        _print_explanation(r, reason, detail_path=str(review_path), next_action=f"leadme-collab inspect {tid}")
        if excerpt:
            r.note(f"  reviewer findings: {excerpt}")
        _finalize(tid, "needs_human", "NEEDS HUMAN", reason)
        return "needs_human"

    # NEEDS FIX
    if state["cycle"] >= state["max_review_cycles"]:
        reason = f"reviewer kept returning NEEDS FIX through all {state['max_review_cycles']} review cycles"
        r.warn("max review cycles reached without PASS", str(state["max_review_cycles"]))
        _print_explanation(r, reason, detail_path=str(review_path), next_action=f"leadme-collab inspect {tid}")
        _finalize(tid, "needs_human", "NEEDS HUMAN", reason)
        return "needs_human"

    return _run_repair(tid, r, review_text)


def _run_repair(tid, r, review_text):
    state = read_task_state(tid)
    wt = state["worktree"]
    diff_info = capture_diff(wt)
    task_md_text = read_artifact(tid, "task.md") or ""

    r.section("claude repair")
    if not _check_no_concurrent_process(tid, state, r):
        return "needs_human"

    prompt = build_repair_prompt(task_md_text, review_text, diff_info)
    write_artifact(tid, "repair-prompt.md", prompt)
    log_event(tid, "repair_start", f"cycle={state['cycle']}")

    claude_info = discover_claude()
    if not claude_info["available"]:
        r.step("claude CLI available", False, "not found on PATH")
        reason = "claude CLI not found on PATH — cannot run the repair step"
        _print_explanation(r, reason, next_action="install/fix the claude CLI, then: leadme-collab resume")
        _finalize(tid, "needs_human", "NEEDS HUMAN", reason)
        return "needs_human"

    result = run_claude(prompt, cwd=wt, tid=tid)
    write_artifact(
        tid, "repair-output.md",
        f"exit/is_error: {result['is_error']}\n"
        f"elapsed_seconds: {result['elapsed_seconds']}\n"
        f"total_cost_usd: {result['total_cost_usd']}\n\n"
        f"## Result text\n\n{result['result_text'] or '(none)'}\n\n"
        f"## Raw stdout\n\n{result['stdout']}\n\n"
        f"## stderr\n\n{result['stderr']}\n",
    )
    ok = not result["is_error"]
    r.step("targeted repair complete", ok, "" if ok else result.get("reason", ""))
    log_event(tid, "repair_done", f"cycle={state['cycle']} is_error={result['is_error']}")

    # Capture diff/verification regardless of outcome, same reasoning as the
    # initial implementer step.
    new_diff = capture_diff(wt)
    write_artifact(tid, "diff.patch", new_diff["patch"])
    verification_text, verify_ok = verify_changed_files(wt, new_diff["changed_files"])
    write_artifact(tid, "verification.md", verification_text)
    r.step("verification after repair", verify_ok)

    if not ok:
        reason = result.get("reason") or "Claude repair failed"
        _print_explanation(
            r, reason,
            detail_path=str(task_dir(tid) / "repair-output.md"),
            next_action=f"leadme-collab inspect {tid}",
        )
        _finalize(tid, "needs_human", "NEEDS HUMAN", reason)
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

def _format_duration(seconds):
    if seconds is None:
        return "unknown"
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _seconds_since(iso_timestamp):
    if not iso_timestamp:
        return None
    try:
        then = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - then).total_seconds()


def _report_active_process(r, state):
    """Shared by status/inspect: shows the live process (if any) and warns
    if state claims one is running but its PID is dead."""
    active = state.get("active_process")
    if not active:
        if state.get("phase") not in TERMINAL_PHASES:
            r.note("  process:      not running")
        return

    pid = active.get("pid")
    alive = process_is_alive(pid)
    elapsed = _seconds_since(active.get("started_at"))
    hb_ago = _seconds_since(active.get("last_heartbeat_at"))

    r.note(f"  process:      {active.get('role', 'unknown')}")
    r.note(f"  pid:          {pid}")
    r.note(f"  elapsed:      {_format_duration(elapsed)}")
    r.note(f"  last heartbeat: {_format_duration(hb_ago)} ago")
    r.note(f"  timeout:      {_format_duration(active.get('timeout_seconds'))}")

    if not alive:
        r.warn(
            "state says in progress but no child process is active",
            f"pid {pid} is not running — it may have crashed, been killed, or the whole "
            f"process group was suspended; run 'leadme-collab resume {state.get('task_id', '')}' "
            f"to retry cleanly",
        )


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
    if state.get("final_reason"):
        r.note(f"  final reason: {state.get('final_reason')}")
    r.note(f"  changed:      {', '.join(diff_info['changed_files']) or '(none)'}")

    _report_active_process(r, state)

    events_path = task_dir(tid) / EVENTS_LOG_NAME
    if events_path.exists():
        lines = events_path.read_text(encoding="utf-8").splitlines()
        if lines:
            r.note(f"  last event:   {lines[-1]}")

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

    if state.get("phase") in TERMINAL_PHASES:
        r.note("  task already in a terminal phase — nothing to resume")
        return 0

    active = state.get("active_process")
    if active and not process_is_alive(active.get("pid")):
        r.note(
            f"  previous {active.get('role', 'process')} (pid {active.get('pid')}) is no longer "
            f"running (last heartbeat {_format_duration(_seconds_since(active.get('last_heartbeat_at')))} "
            f"ago) — retrying that step rather than assuming it completed"
        )

    result = _advance_task(tid, r)
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
    if state.get("final_reason"):
        r.note(f"  final reason: {state.get('final_reason')}")
    _report_active_process(r, state)
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

    r = Reporter()
    r.title("LeadMe Collab Abort")
    r.note(f"  task id: {tid}")

    # Kill any live child process for this task FIRST, before touching
    # state — an aborted task must never leave a running Claude/Codex
    # process behind.
    active = state.get("active_process")
    if active and process_is_alive(active.get("pid")):
        r.section("stop live process")
        pid = active.get("pid")
        r.note(f"  killing {active.get('role', 'process')} (pid {pid})")
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
            time.sleep(1)
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        r.step(f"process {pid} stopped", not process_is_alive(pid))

    _write_state_update(tid, phase="aborted", final=state.get("final") or "ABORTED")
    _clear_active_process(tid)
    log_event(tid, "aborted", "cleanup=" + ("yes" if args.cleanup else "no"))

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

    return 0


def cmd_list(args):
    ids = list_task_ids()
    r = Reporter()
    r.title("LeadMe Collab Tasks")
    if not ids:
        r.note("  (no tasks yet)")
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
    p_start.add_argument(
        "--force-new", action="store_true",
        help="start a new task even if an active task with a matching description already exists",
    )

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
