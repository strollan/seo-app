#!/usr/bin/env python3
"""
leadme-deploy — deployment automation for LeadMeLeads.

Replaces the manual WSL -> GitHub -> SSH -> Droplet -> systemd -> smoke-test
workflow with a single command. See docs/leadme-deploy.md for usage.

Design notes:
- Standard library only (subprocess, argparse, json, urllib not required —
  curl is used for HTTP checks per project convention). No third-party deps.
- Every external command runs through run_local()/run_remote() so tests can
  monkeypatch a single choke point instead of mocking subprocess directly.
- Nothing in this module pushes, pulls, restarts, or backs up anything
  unless cmd_deploy() (the real, default, no-flag mode) is executed.
"""

from __future__ import annotations

import argparse
import functools
import json
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Resolve from this checked-in script, not the caller's CWD. The wrapper may
# be invoked through a user-local symlink, but it ultimately executes this
# real file inside whichever linked worktree contains it.
REPO_PATH = Path(__file__).resolve().parent.parent
PRIMARY_BRANCH = "main"

REPO_IDENTITY_FILES = (
    "app/main.py",
    "agents/lead_dashboard_agent.py",
    "scripts/leadme-deploy",
    "scripts/leadme_deploy.py",
    "docs/leadme-deploy.md",
)

# WSL drvfs mount prefix for the Windows C: drive. git operations whose cwd
# lives under here are eligible to run through git.exe instead of Linux git
# (see _select_git_binary below).
WINDOWS_MOUNT_PREFIX = "/mnt/c"
WINDOWS_GIT_EXE = "git.exe"
LINUX_GIT = "git"

PROD_HOST = "165.245.238.122"
PROD_USER = "root"
SSH_KEY = str(Path.home() / ".ssh" / "id_ed25519_leadmeleads")

PROD_APP_PATH = "/var/www/leadmeleads"
PROD_BACKUPS_DIR = "/var/www/leadmeleads-backups"
PROD_DB_PATH = f"{PROD_APP_PATH}/data/app_auth.db"
PROD_VENV_PYTHON = f"{PROD_APP_PATH}/venv/bin/python"
PROD_SERVICE = "leadmeleads"

LIVE_SITE = "https://leadmeleads.com"

# Preferred local compile interpreter, falling back if unavailable.
COMPILE_TARGETS = [
    "app/main.py",
    "agents/auth_agent.py",
    "agents/lead_dashboard_agent.py",
]

# Untracked paths that are known/expected in the working tree and must not
# block deployment on their own.
KNOWN_UNTRACKED_EXACT = {
    ".claude",
    "reset_local_password.py",
}
KNOWN_UNTRACKED_PREFIXES = (
    "CLAUDE.md.backup-",
)

SSH_CONNECT_TIMEOUT = 10
SSH_COMMAND_TIMEOUT = 30
GIT_TIMEOUT = 30
CURL_TIMEOUT = 15
COMPILE_TIMEOUT = 60
RESTART_WAIT_SECONDS = 3

SMOKE_ROUTES = [
    ("/", {200}),
    ("/login", {200}),
    ("/create-account", {200}),
    ("/forgot-password", {200}),
    ("/reset-password?token=leadme-deploy-invalid-token", {200, 302, 303, 400}),
    ("/compare", {200, 302, 303}),
    ("/lead-bot", {200, 302, 303}),
    # /history requires login; an anonymous request must redirect to /login,
    # never render 200. Accepting 200 here previously masked a stale deploy
    # that predated the login-required gate on this route.
    ("/history", {302, 303}),
    ("/settings", {200, 302, 303}),
]

# Routes whose redirect target must be validated, not just its status code.
REQUIRED_REDIRECT_LOCATIONS = {
    "/history": "/login",
}

JOURNAL_ERROR_PATTERNS = [
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r"sqlite3\.OperationalError"),
    re.compile(r"ModuleNotFoundError"),
    re.compile(r"ImportError"),
    re.compile(r"SyntaxError"),
    re.compile(r"Failed to start"),
    re.compile(r"Main process exited"),
    re.compile(r"\b50[0-9]\b.*(error|Error)"),
]

STATE_DIR_DEFAULT = Path.home() / ".local" / "state" / "leadme-deploy"
STATE_FILE_NAME = "state.json"


# ---------------------------------------------------------------------------
# Command execution — the one choke point tests monkeypatch.
# ---------------------------------------------------------------------------

class CmdResult:
    __slots__ = ("returncode", "stdout", "stderr")

    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    @property
    def ok(self):
        return self.returncode == 0


def _is_windows_mount_path(path_str):
    normalized = str(path_str).rstrip("/")
    return normalized == WINDOWS_MOUNT_PREFIX or normalized.startswith(WINDOWS_MOUNT_PREFIX + "/")


@functools.lru_cache(maxsize=1)
def _git_exe_available():
    return shutil.which(WINDOWS_GIT_EXE) is not None


def _select_git_binary(cwd, args):
    """Choose git.exe for /mnt/c repo operations, /usr/bin/git otherwise.

    WSL's Linux git talks to a /mnt/c path through the 9p/drvfs translation
    layer, paying a cross-boundary cost on every stat()-like call; git.exe
    (reached via WSL interop, already on PATH when Git for Windows is
    installed) talks to the same NTFS volume natively and is markedly
    faster/more reliable there. Native /home paths get no such benefit and
    keep using Linux git.

    cwd alone is not sufficient: leadme-collab creates linked worktrees
    under /home/scot from the /mnt/c primary repo (`git worktree add
    <home-path> ...`), and git.exe cannot see native Linux filesystem paths
    at all (WSL2's ext4 is not exposed to Windows). So any argument naming
    a /home path forces Linux git even when cwd is under /mnt/c.

    Falls back to Linux git whenever git.exe isn't discoverable (e.g. WSL
    interop disabled) rather than failing the command outright — this is a
    performance choice, not a correctness one, so failing closed here would
    only make things worse.
    """
    if cwd is None or not _is_windows_mount_path(cwd):
        return LINUX_GIT
    for arg in args:
        if isinstance(arg, str) and arg.startswith("/home/"):
            return LINUX_GIT
    return WINDOWS_GIT_EXE if _git_exe_available() else LINUX_GIT


def run_local(args, cwd=None, timeout=GIT_TIMEOUT, input_text=None):
    """Run a local command as an argv list. Never uses shell=True.

    If args[0] == "git", the actual binary invoked is chosen by cwd (and,
    for worktree paths, by the other arguments too) via
    _select_git_binary() — see that function for the WSL git.exe/Linux git
    routing rationale. Every git call in this project's automation goes
    through run_local (directly or via a thin git() wrapper), so this is
    the single place the choice is made; no PATH shims or env var
    overrides are needed anywhere else.
    """
    if args and args[0] == "git":
        args = [_select_git_binary(cwd, args[1:])] + list(args[1:])
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
        )
        return CmdResult(proc.returncode, proc.stdout, proc.stderr)
    except subprocess.TimeoutExpired:
        return CmdResult(124, "", f"TIMEOUT after {timeout}s: {' '.join(args)}")
    except FileNotFoundError as exc:
        return CmdResult(127, "", f"command not found: {exc}")


SSH_CONTROL_DIR = Path.home() / ".ssh" / "leadme-deploy-control"


def ssh_base_args():
    # A single leadme-deploy run makes many short SSH calls (doctor/check/
    # deploy each fire off several remote checks). The production host has
    # been observed to rate-limit *new* TCP connections and refuse them for
    # a short window after a handful in quick succession. ControlMaster
    # reuses one already-authenticated connection for the whole run instead
    # of opening a new one per call, which avoids tripping that limiter.
    SSH_CONTROL_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    control_path = str(SSH_CONTROL_DIR / "%r@%h:%p")
    return [
        "ssh",
        "-i", SSH_KEY,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
        "-o", "ControlMaster=auto",
        "-o", "ControlPersist=60s",
        "-o", f"ControlPath={control_path}",
        f"{PROD_USER}@{PROD_HOST}",
    ]


def run_remote(remote_cmd, timeout=SSH_COMMAND_TIMEOUT):
    """Run remote_cmd (a single shell string executed on the remote host).

    remote_cmd must only ever be built from static, developer-controlled
    fragments plus values validated with q()/is_valid_sha(); never raw
    user input.
    """
    args = ssh_base_args() + [remote_cmd]
    return run_local(args, timeout=timeout)


def q(value):
    """Shell-quote a value for embedding in a remote command string."""
    return shlex.quote(str(value))


def is_valid_sha(value):
    return bool(re.fullmatch(r"[0-9a-f]{7,40}", str(value or "")))


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

class Reporter:
    def __init__(self):
        self.lines = []
        self.failed = False
        self.blocked_reason = None

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

    def block(self, reason):
        self.failed = True
        self.blocked_reason = reason


# ---------------------------------------------------------------------------
# Local git helpers
# ---------------------------------------------------------------------------

def _repo_path(repo=None):
    return Path(repo) if repo is not None else REPO_PATH


def repo_exists(repo=None):
    repo = _repo_path(repo)
    return repo.is_dir() and (repo / ".git").exists()


def validate_repo_identity(repo=None):
    """Fail closed unless repo is this script's LeadMeLeads Git worktree."""
    repo = _repo_path(repo).resolve()
    if not repo_exists(repo):
        return False, f"{repo} is not a Git worktree"

    root_result = run_local(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=repo,
        timeout=10,
    )
    if not root_result.ok or not root_result.stdout.strip():
        return False, "Git top-level could not be resolved"

    resolved_root = Path(root_result.stdout.strip()).resolve()
    if resolved_root != repo:
        return False, f"resolved Git root is {resolved_root}, expected {repo}"

    missing = [relative for relative in REPO_IDENTITY_FILES if not (repo / relative).is_file()]
    if missing:
        return False, "LeadMeLeads identity files missing: " + ", ".join(missing)

    return True, str(repo)


def git_available():
    res = run_local(["git", "--version"], timeout=10)
    return res.ok


def current_branch(repo=None):
    repo = _repo_path(repo)
    res = run_local(["git", "branch", "--show-current"], cwd=repo, timeout=10)
    if not res.ok:
        return None
    return res.stdout.strip()


def head_sha(repo=None):
    repo = _repo_path(repo)
    res = run_local(["git", "rev-parse", "HEAD"], cwd=repo, timeout=10)
    if not res.ok:
        return None
    return res.stdout.strip()


def tracked_tree_clean(repo=None):
    """True only if there are no staged or unstaged changes to tracked files.

    Uses `git diff --quiet` / `git diff --cached --quiet` per project
    guidance rather than parsing full `git status` output.
    """
    repo = _repo_path(repo)
    unstaged = run_local(["git", "diff", "--quiet"], cwd=repo, timeout=15)
    staged = run_local(["git", "diff", "--cached", "--quiet"], cwd=repo, timeout=15)
    if unstaged.returncode not in (0, 1) or staged.returncode not in (0, 1):
        return None  # error (e.g. not a repo) — caller should treat as unknown/fail
    return unstaged.returncode == 0 and staged.returncode == 0


def is_known_untracked(path):
    norm = path.rstrip("/")
    if norm in KNOWN_UNTRACKED_EXACT:
        return True
    return any(norm.startswith(p) for p in KNOWN_UNTRACKED_PREFIXES)


def classify_untracked(repo=None):
    """Return (known, unexpected) lists of untracked paths."""
    repo = _repo_path(repo)
    res = run_local(
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        cwd=repo,
        timeout=15,
    )
    if not res.ok:
        return [], []
    known, unexpected = [], []
    for line in res.stdout.splitlines():
        if not line.startswith("?? "):
            continue
        path = line[3:].strip()
        (known if is_known_untracked(path) else unexpected).append(path)
    return known, unexpected


def fetch_origin(repo=None, branch=PRIMARY_BRANCH, timeout=30):
    repo = _repo_path(repo)
    res = run_local(["git", "fetch", "origin", branch], cwd=repo, timeout=timeout)
    return res.ok, res.stderr.strip()


def origin_branch_sha(repo=None, branch=PRIMARY_BRANCH):
    repo = _repo_path(repo)
    res = run_local(["git", "rev-parse", f"origin/{branch}"], cwd=repo, timeout=10)
    if not res.ok:
        return None
    return res.stdout.strip()


def divergence_state(repo=None, branch=PRIMARY_BRANCH):
    """Return (state, ahead, behind) comparing HEAD to origin/<branch>.

    state is one of: "in-sync", "ahead", "behind", "diverged", "unknown".
    """
    repo = _repo_path(repo)
    res = run_local(
        ["git", "rev-list", "--left-right", "--count", f"origin/{branch}...HEAD"],
        cwd=repo,
        timeout=15,
    )
    if not res.ok:
        return "unknown", None, None
    parts = res.stdout.split()
    if len(parts) != 2:
        return "unknown", None, None
    behind, ahead = int(parts[0]), int(parts[1])
    if ahead and behind:
        return "diverged", ahead, behind
    if ahead:
        return "ahead", ahead, behind
    if behind:
        return "behind", ahead, behind
    return "in-sync", ahead, behind


def push_origin(repo=None, branch=PRIMARY_BRANCH, timeout=60):
    repo = _repo_path(repo)
    res = run_local(["git", "push", "origin", branch], cwd=repo, timeout=timeout)
    return res.ok, (res.stdout + res.stderr).strip()


def local_python_for_compile():
    candidates = [
        Path.home() / ".venvs" / "leadmeleads" / "bin" / "python",
        REPO_PATH / "venv" / "bin" / "python",
        Path(sys.executable),
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return str(candidate)
    return sys.executable


def compile_targets(python_bin, repo=None, targets=None, timeout=COMPILE_TIMEOUT):
    """Compile each target individually so one bad file doesn't hide others."""
    repo = _repo_path(repo)
    targets = targets if targets is not None else COMPILE_TARGETS
    results = []
    for target in targets:
        res = run_local([python_bin, "-m", "py_compile", target], cwd=repo, timeout=timeout)
        results.append((target, res.ok, (res.stdout + res.stderr).strip()))
    return results


# ---------------------------------------------------------------------------
# Remote (production) helpers — all read-only unless noted.
# ---------------------------------------------------------------------------

def remote_hostname():
    res = run_remote("hostname")
    return (res.stdout.strip() if res.ok else None), res


def remote_path_exists(path=PROD_APP_PATH):
    res = run_remote(f"test -d {q(path)} && echo EXISTS || echo MISSING")
    return res.ok and "EXISTS" in res.stdout


def remote_is_git_repo(path=PROD_APP_PATH):
    res = run_remote(f"cd {q(path)} && git rev-parse --is-inside-work-tree 2>&1")
    return res.ok and "true" in res.stdout


def remote_head_sha(path=PROD_APP_PATH):
    res = run_remote(f"cd {q(path)} && git rev-parse HEAD 2>&1")
    if not res.ok:
        return None
    value = res.stdout.strip()
    return value if is_valid_sha(value) else None


def remote_branch(path=PROD_APP_PATH):
    res = run_remote(f"cd {q(path)} && git branch --show-current 2>&1")
    return res.stdout.strip() if res.ok else None


def remote_tracked_clean(path=PROD_APP_PATH):
    res = run_remote(
        f"cd {q(path)} && git diff --quiet; a=$?; git diff --cached --quiet; b=$?; echo \"$a $b\""
    )
    if not res.ok:
        return None
    try:
        a, b = res.stdout.strip().split()
        return int(a) == 0 and int(b) == 0
    except ValueError:
        return None


def remote_modified_tracked_files(path=PROD_APP_PATH):
    res = run_remote(f"cd {q(path)} && git diff --name-only HEAD 2>&1")
    if not res.ok:
        return []
    return [line for line in res.stdout.splitlines() if line.strip()]


def remote_venv_python_exists(python_path=PROD_VENV_PYTHON):
    res = run_remote(f"test -x {q(python_path)} && echo EXISTS || echo MISSING")
    return res.ok and "EXISTS" in res.stdout


def remote_service_state(service=PROD_SERVICE):
    res = run_remote(
        f"systemctl show {q(service)} --property=LoadState,ActiveState,UnitFileState --no-pager 2>&1"
    )
    state = {}
    if res.ok:
        for line in res.stdout.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                state[key.strip()] = value.strip()
    return state


def remote_db_size(path=PROD_DB_PATH):
    res = run_remote(f"stat -c %s {q(path)} 2>/dev/null || echo MISSING")
    out = res.stdout.strip()
    if not res.ok or out == "MISSING" or not out.isdigit():
        return None
    return int(out)


def remote_backups_listing(backups_dir=PROD_BACKUPS_DIR, limit=5):
    res = run_remote(f"ls -1t {q(backups_dir)} 2>/dev/null | head -{int(limit)}")
    if not res.ok:
        return []
    return [line for line in res.stdout.splitlines() if line.strip()]


def remote_sha256(path):
    res = run_remote(f"sha256sum {q(path)} 2>/dev/null | awk '{{print $1}}'")
    if not res.ok:
        return None
    value = res.stdout.strip()
    return value or None


def curl_available():
    res = run_local(["curl", "--version"], timeout=10)
    return res.ok


# ---------------------------------------------------------------------------
# Smoke tests (GET-only)
# ---------------------------------------------------------------------------

def curl_status_code(url, timeout=CURL_TIMEOUT):
    """Return (status_code_int_or_None, error_detail)."""
    res = run_local(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", str(timeout), url],
        timeout=timeout + 5,
    )
    if not res.ok:
        return None, (res.stderr or "curl failed").strip()
    raw = res.stdout.strip()
    if raw.isdigit():
        return int(raw), ""
    return None, f"unexpected curl output: {raw!r}"


def curl_status_and_location(url, timeout=CURL_TIMEOUT):
    """Single request: return (status_code_int_or_None, location_or_None, error_detail).

    Fetches headers and the status code from the same curl invocation so a
    redirect check can never mix the status from one response with the
    Location header from another.
    """
    res = run_local(
        ["curl", "-s", "-o", "/dev/null", "-D", "-", "-w", "\n%{http_code}", "--max-time", str(timeout), url],
        timeout=timeout + 5,
    )
    if not res.ok:
        return None, None, (res.stderr or "curl failed").strip()
    lines = res.stdout.splitlines()
    if not lines:
        return None, None, "empty curl output"
    status_raw = lines[-1].strip()
    location = None
    for line in lines[:-1]:
        if line.lower().startswith("location:"):
            location = line.split(":", 1)[1].strip()
    if not status_raw.isdigit():
        return None, None, f"unexpected curl output: {status_raw!r}"
    return int(status_raw), location, ""


def _redirect_location_matches(location, expected_path, request_url):
    """True only if location's path is exactly expected_path (same-site if absolute)."""
    if not location:
        return False
    parsed = urlparse(location)
    if parsed.path != expected_path:
        return False
    if parsed.netloc and parsed.netloc != urlparse(request_url).netloc:
        return False
    return True


def run_smoke_tests(base_url=LIVE_SITE, routes=None, reporter=None):
    routes = routes if routes is not None else SMOKE_ROUTES
    results = []
    for path, acceptable in routes:
        url = base_url.rstrip("/") + path
        required_location = REQUIRED_REDIRECT_LOCATIONS.get(path)

        if required_location is not None:
            status, location, detail = curl_status_and_location(url)
            ok = status is not None and status in acceptable
            if ok and not _redirect_location_matches(location, required_location, url):
                ok = False
                detail = (
                    f"redirected but Location {location!r} did not match "
                    f"expected path {required_location!r}"
                ).strip()
        else:
            status, detail = curl_status_code(url)
            ok = status is not None and status in acceptable

        results.append((path, status, ok, detail))
        if reporter is not None:
            label = path if path != "/" else "/"
            if ok:
                reporter.step(label, True, f"{status}")
            else:
                shown = status if status is not None else "no response"
                reporter.step(label, False, f"{shown} (expected one of {sorted(acceptable)}) {detail}".strip())
    return results


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------

def state_dir(override=None):
    return Path(override) if override else STATE_DIR_DEFAULT


def state_path(override=None):
    return state_dir(override) / STATE_FILE_NAME


def read_state(override=None):
    path = state_path(override)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


SAFE_STATE_KEYS = {
    "last_deploy_timestamp",
    "previous_production_commit",
    "deployed_commit",
    "backup_path",
    "result",
}

SECRET_LIKE_KEY_PATTERN = re.compile(
    r"(password|token|secret|key|smtp|\.env)", re.IGNORECASE
)


def write_state(data, override=None):
    """Persist only the known-safe fields; refuse anything secret-shaped."""
    for key in data:
        if key not in SAFE_STATE_KEYS or SECRET_LIKE_KEY_PATTERN.search(key):
            raise ValueError(f"refusing to persist unexpected/secret-shaped state key: {key}")
    for value in data.values():
        if isinstance(value, str) and SECRET_LIKE_KEY_PATTERN.search(value):
            raise ValueError("refusing to persist a value that looks secret-shaped")

    target_dir = state_dir(override)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / STATE_FILE_NAME
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


# ---------------------------------------------------------------------------
# Mode: --doctor
# ---------------------------------------------------------------------------

def cmd_doctor(args):
    r = Reporter()
    r.title("LeadMeLeads Doctor")

    r.section("local")
    r.step("repo exists", repo_exists(), str(REPO_PATH))
    r.step("git available", git_available())

    branch = current_branch()
    r.step("current branch resolvable", branch is not None, branch or "unknown")

    clean = tracked_tree_clean()
    if clean is None:
        r.step("tracked working tree state", False, "could not determine (git error)")
    else:
        known, unexpected = classify_untracked()
        r.step("tracked working tree clean", clean)
        if known:
            r.note(f"  known untracked (ignored): {', '.join(known)}")
        if unexpected:
            r.warn("unexpected untracked files present", ", ".join(unexpected))

    python_bin = local_python_for_compile()
    r.note(f"  compile interpreter: {python_bin}")

    r.section("ssh")
    key_path = Path(SSH_KEY)
    r.step("ssh key file exists", key_path.exists(), str(key_path))

    hostname, hostname_res = remote_hostname()
    r.step("ssh connectivity", hostname is not None, hostname or hostname_res.stderr.strip())

    r.section("remote")
    if hostname is not None:
        r.step("remote app path exists", remote_path_exists(), PROD_APP_PATH)
        r.step("remote repo valid", remote_is_git_repo())
        rsha = remote_head_sha()
        r.step("remote HEAD readable", rsha is not None, rsha or "unknown")
        r.step("production venv python exists", remote_venv_python_exists(), PROD_VENV_PYTHON)

        state = remote_service_state()
        service_ok = state.get("LoadState") == "loaded"
        r.step(
            "service exists",
            service_ok,
            f"LoadState={state.get('LoadState', '?')} ActiveState={state.get('ActiveState', '?')}",
        )
    else:
        r.warn("remote checks skipped", "no SSH connectivity")

    r.section("live site")
    r.step("curl available", curl_available())
    status, detail = curl_status_code(LIVE_SITE + "/")
    reachable = status is not None and 200 <= status < 400
    r.step("live site reachable", reachable, f"{status}" if status else detail)

    r.note()
    r.note("DOCTOR PASS" if not r.failed else "DOCTOR FAIL")
    print(r.render())
    return 0 if not r.failed else 1


# ---------------------------------------------------------------------------
# Mode: --status
# ---------------------------------------------------------------------------

def gather_local_status():
    branch = current_branch()
    local_sha = head_sha()
    fetch_ok, fetch_err = fetch_origin()
    origin_sha = origin_branch_sha() if fetch_ok else None
    if origin_sha:
        state, ahead, behind = divergence_state()
    else:
        state, ahead, behind = "unknown", None, None
    return {
        "branch": branch,
        "local_sha": local_sha,
        "fetch_ok": fetch_ok,
        "fetch_error": fetch_err,
        "origin_sha": origin_sha,
        "divergence": state,
        "ahead": ahead,
        "behind": behind,
    }


def cmd_status(args):
    r = Reporter()
    r.title("LeadMeLeads Status")

    r.section("local")
    local = gather_local_status()
    r.note(f"  branch:       {local['branch'] or 'unknown'}")
    r.note(f"  local HEAD:   {local['local_sha'] or 'unknown'}")
    if local["fetch_ok"]:
        r.note(f"  origin HEAD:  {local['origin_sha'] or 'unknown'}")
        r.note(f"  divergence:   {local['divergence']} (ahead={local['ahead']}, behind={local['behind']})")
    else:
        r.warn("could not fetch origin/main", local["fetch_error"])

    r.section("remote")
    hostname, hostname_res = remote_hostname()
    if hostname is None:
        r.warn("ssh unreachable", hostname_res.stderr.strip())
    else:
        rsha = remote_head_sha()
        r.note(f"  production HEAD: {rsha or 'unknown'}")
        state = remote_service_state()
        r.note(f"  service state:   {state.get('ActiveState', 'unknown')}")

    r.section("live")
    status, detail = curl_status_code(LIVE_SITE + "/")
    r.note(f"  {LIVE_SITE}/ -> {status if status is not None else 'unreachable (' + detail + ')'}")

    print(r.render())
    return 0


# ---------------------------------------------------------------------------
# Mode: --check
# ---------------------------------------------------------------------------

def cmd_check(args):
    r = Reporter()
    r.title("LeadMeLeads Pre-Deploy Check (local only)")

    r.section("local")
    r.step("repo exists", repo_exists(), str(REPO_PATH))

    branch = current_branch()
    on_main = branch == PRIMARY_BRANCH
    r.step("branch is main", on_main, branch or "unknown")

    clean = tracked_tree_clean()
    if clean is None:
        r.step("tracked tree clean", False, "could not determine (git error)")
    else:
        r.step("tracked tree clean", clean)
        if not clean:
            diff = run_local(["git", "diff", "--stat"], cwd=REPO_PATH, timeout=15)
            staged = run_local(["git", "diff", "--cached", "--stat"], cwd=REPO_PATH, timeout=15)
            if diff.stdout.strip():
                r.note("  unstaged:\n    " + diff.stdout.strip().replace("\n", "\n    "))
            if staged.stdout.strip():
                r.note("  staged:\n    " + staged.stdout.strip().replace("\n", "\n    "))

    known, unexpected = classify_untracked()
    if known:
        r.note(f"  known untracked (ignored): {', '.join(known)}")
    if unexpected:
        r.warn("unexpected untracked files present (not auto-included)", ", ".join(unexpected))

    fetch_ok, fetch_err = fetch_origin()
    if not fetch_ok:
        r.step("fetch origin/main", False, fetch_err)
    else:
        r.step("fetch origin/main", True)
        state, ahead, behind = divergence_state()
        divergence_ok = state in ("in-sync", "ahead")
        r.step(f"branch divergence ({state})", divergence_ok, f"ahead={ahead} behind={behind}")

    python_bin = local_python_for_compile()
    r.note(f"  compiling with: {python_bin}")
    for target, ok, detail in compile_targets(python_bin):
        r.step(f"compile {target}", ok, detail if not ok else "")

    r.note()
    r.note("CHECK PASS" if not r.failed else "CHECK FAIL")
    print(r.render())
    return 0 if not r.failed else 1


# ---------------------------------------------------------------------------
# Mode: --smoke
# ---------------------------------------------------------------------------

def cmd_smoke(args):
    r = Reporter()
    r.title("LeadMeLeads Live Smoke Test")
    r.section("live (GET only)")
    run_smoke_tests(reporter=r)
    r.note()
    r.note("SMOKE PASS" if not r.failed else "SMOKE FAIL")
    print(r.render())
    return 0 if not r.failed else 1


# ---------------------------------------------------------------------------
# Mode: --dry-run
# ---------------------------------------------------------------------------

def cmd_dry_run(args):
    r = Reporter()
    r.title("LeadMeLeads Deploy Plan (dry run — nothing will be executed)")

    local = gather_local_status()
    branch = local["branch"]
    local_sha = local["local_sha"] or "<unknown>"

    r.section("phase 1 — local validation")
    r.note(f"  would confirm repo at {REPO_PATH}")
    r.note(f"  would confirm branch == '{PRIMARY_BRANCH}' (currently: {branch or 'unknown'})")
    r.note("  would run: git diff --quiet && git diff --cached --quiet")
    r.note("  would classify untracked files against the known-safe allowlist")
    python_bin = local_python_for_compile()
    for target in COMPILE_TARGETS:
        r.note(f"  would run: {python_bin} -m py_compile {target}")

    r.section("phase 2 — push")
    if local["divergence"] == "ahead":
        r.note(f"  would run: git push origin {PRIMARY_BRANCH}")
    elif local["divergence"] == "in-sync":
        r.note("  already in sync with origin/main — push would be skipped")
    elif local["divergence"] == "diverged":
        r.note("  DIVERGED from origin/main — real run would STOP here")
    else:
        r.note(f"  divergence state: {local['divergence']} — would re-evaluate at run time")
    r.note(f"  EXPECTED_COMMIT would be set to: {local_sha}")

    r.section("phase 3 — remote pre-flight")
    r.note(f"  would run: ssh -i {SSH_KEY} {PROD_USER}@{PROD_HOST} ...")
    r.note(f"  would verify {PROD_APP_PATH} exists, is a git repo, tracked tree is clean")
    r.note(f"  would verify {PROD_VENV_PYTHON} exists")
    r.note(f"  would verify systemd unit '{PROD_SERVICE}' is loaded")

    r.section("phase 4 — production backup")
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    r.note(f"  would run: mkdir -p {PROD_BACKUPS_DIR}")
    r.note(f"  would run: cp {PROD_DB_PATH} {PROD_BACKUPS_DIR}/app_auth-{ts}.db")
    r.note("  would verify backup size and sha256 match the source")

    r.section("phase 5 — remote deploy")
    r.note(f"  would run: cd {PROD_APP_PATH} && git fetch origin")
    r.note(f"  would run: cd {PROD_APP_PATH} && git pull --ff-only origin {PRIMARY_BRANCH}")
    r.note(f"  would verify: cd {PROD_APP_PATH} && git rev-parse HEAD == {local_sha}")

    r.section("phase 6 — remote compile")
    for target in COMPILE_TARGETS:
        r.note(f"  would run: {PROD_VENV_PYTHON} -m py_compile {target}")

    r.section("phase 7 — restart")
    r.note(f"  would run: systemctl restart {PROD_SERVICE}")
    r.note(f"  would run: systemctl is-active {PROD_SERVICE} (require: active)")
    r.note(f"  would inspect: journalctl -u {PROD_SERVICE} -n 100 --no-pager")

    r.section("phase 8 — live smoke test")
    for path, acceptable in SMOKE_ROUTES:
        r.note(f"  would GET {LIVE_SITE}{path} (accept {sorted(acceptable)})")

    r.note()
    r.note("DRY RUN — no commands above were executed.")
    print(r.render())
    return 0


# ---------------------------------------------------------------------------
# Mode: --rollback-info
# ---------------------------------------------------------------------------

def cmd_rollback_info(args):
    r = Reporter()
    r.title("LeadMeLeads Rollback Info (informational only — nothing executed)")

    state = read_state()
    prev_commit = state.get("previous_production_commit")
    deployed_commit = state.get("deployed_commit")
    recorded_backup = state.get("backup_path")
    last_deploy = state.get("last_deploy_timestamp")

    r.section("recorded state")
    r.note(f"  last deploy timestamp:        {last_deploy or 'unknown (no recorded deploy)'}")
    r.note(f"  previous production commit:  {prev_commit or 'unknown'}")
    r.note(f"  last deployed commit:        {deployed_commit or 'unknown'}")
    r.note(f"  backup recorded at deploy:   {recorded_backup or 'unknown'}")

    r.section("live production")
    hostname, hostname_res = remote_hostname()
    current_commit = None
    if hostname is None:
        r.warn("ssh unreachable", hostname_res.stderr.strip())
    else:
        current_commit = remote_head_sha()
        r.note(f"  current production commit:  {current_commit or 'unknown'}")
        backups = remote_backups_listing()
        r.note(f"  latest known DB backups:    {', '.join(backups) if backups else 'none found'}")

    target_commit = prev_commit or "<previous commit — not recorded, inspect journal/backups>"

    r.section("rollback plan (not executed)")
    r.note("  1. ssh -i " + SSH_KEY + f" {PROD_USER}@{PROD_HOST}")
    r.note(f"  2. cd {PROD_APP_PATH} && git fetch origin")
    r.note(f"  3. git checkout {target_commit} -- .   # or: git reset --hard {target_commit}")
    if recorded_backup:
        r.note(f"  4. cp {recorded_backup} {PROD_DB_PATH}")
    else:
        r.note(f"  4. cp {PROD_BACKUPS_DIR}/<chosen-backup>.db {PROD_DB_PATH}")
    r.note(f"  5. systemctl restart {PROD_SERVICE}")
    r.note(f"  6. systemctl is-active {PROD_SERVICE}")
    r.note(f"  7. curl -I {LIVE_SITE}/")

    r.note()
    r.note("This mode never executes the rollback. Run the commands above manually if needed.")
    print(r.render())
    return 0


# ---------------------------------------------------------------------------
# Failure handling for the real deploy flow
# ---------------------------------------------------------------------------

class DeployHalt(Exception):
    def __init__(self, reason, ctx):
        super().__init__(reason)
        self.reason = reason
        self.ctx = ctx


def _print_failure_summary(r, ctx):
    r.note()
    r.note(f"STOPPED: {ctx.get('reason', 'unknown reason')}")
    r.note(f"  production modified:   {'YES' if ctx.get('production_modified') else 'NO'}")
    r.note(f"  service restarted:     {'YES' if ctx.get('service_restarted') else 'NO'}")
    r.note(f"  backup path:           {ctx.get('backup_path') or 'none created'}")
    r.note(f"  pre-deploy commit:     {ctx.get('pre_deploy_commit') or 'unknown'}")
    r.note("  recovery: run 'leadme-deploy --rollback-info' for the rollback plan")


# ---------------------------------------------------------------------------
# Mode: real deploy (default, no flags)
# ---------------------------------------------------------------------------

def cmd_deploy(args):
    r = Reporter()
    r.title("LeadMeLeads Deploy")

    ctx = {
        "production_modified": False,
        "service_restarted": False,
        "backup_path": None,
        "pre_deploy_commit": None,
        "reason": None,
    }

    try:
        # --- Phase 1: local validation -------------------------------------------------
        r.section("local")

        if not r.step("repo exists", repo_exists(), str(REPO_PATH)):
            raise DeployHalt("local repo path missing or not a git repo", ctx)

        identity_ok, identity_detail = validate_repo_identity()
        if not r.step("LeadMeLeads repo identity", identity_ok, identity_detail):
            raise DeployHalt("local repository identity check failed", ctx)

        branch = current_branch()
        if not r.step("branch main", branch == PRIMARY_BRANCH, branch or "unknown"):
            raise DeployHalt(
                f"current branch is '{branch}', not '{PRIMARY_BRANCH}' — refusing to auto-switch",
                ctx,
            )

        clean = tracked_tree_clean()
        if clean is None:
            r.step("tracked tree clean", False, "could not determine (git error)")
            raise DeployHalt("could not determine tracked working-tree state", ctx)
        if not r.step("tracked tree clean", clean):
            diff = run_local(["git", "diff", "--stat"], cwd=REPO_PATH, timeout=15).stdout.strip()
            staged = run_local(["git", "diff", "--cached", "--stat"], cwd=REPO_PATH, timeout=15).stdout.strip()
            if diff:
                r.note("  unstaged:\n    " + diff.replace("\n", "\n    "))
            if staged:
                r.note("  staged:\n    " + staged.replace("\n", "\n    "))
            raise DeployHalt("unexpected tracked modifications in local repo", ctx)

        known, unexpected = classify_untracked()
        if known:
            r.note(f"  known untracked (ignored): {', '.join(known)}")
        if unexpected:
            r.warn("unexpected untracked files present (not blocking, review recommended)", ", ".join(unexpected))

        local_sha = head_sha()
        ctx["pre_deploy_commit"] = local_sha
        r.note(f"  local HEAD: {local_sha}")

        fetch_ok, fetch_err = fetch_origin()
        if not r.step("fetch origin/main", fetch_ok, fetch_err if not fetch_ok else ""):
            raise DeployHalt("could not fetch origin/main", ctx)

        state, ahead, behind = divergence_state()
        if state == "diverged":
            r.step(f"branch divergence ({state})", False, f"ahead={ahead} behind={behind}")
            raise DeployHalt("local main has diverged from origin/main — resolve manually", ctx)
        r.step(f"branch divergence ({state})", True, f"ahead={ahead} behind={behind}")

        python_bin = local_python_for_compile()
        r.note(f"  compiling with: {python_bin}")
        for target, ok, detail in compile_targets(python_bin):
            if not r.step(f"compile {target}", ok, detail if not ok else ""):
                raise DeployHalt(f"local compile failed: {target}", ctx)

        # --- Phase 2: push --------------------------------------------------------------
        r.section("push")
        if state == "ahead":
            push_ok, push_detail = push_origin()
            if not r.step(f"pushed {local_sha[:7]}", push_ok, push_detail if not push_ok else ""):
                raise DeployHalt("git push origin main failed (credentials/network?)", ctx)
        else:
            r.step("already synchronized with origin/main", True)

        fetch_ok, fetch_err = fetch_origin()
        origin_sha = origin_branch_sha() if fetch_ok else None
        if not r.step("origin/main contains local HEAD", origin_sha == local_sha,
                       f"origin={origin_sha} local={local_sha}"):
            raise DeployHalt("origin/main does not match local HEAD after push", ctx)

        expected_commit = local_sha
        r.note(f"  EXPECTED_COMMIT={expected_commit}")

        # --- Phase 3: remote pre-flight ---------------------------------------------------
        r.section("remote")
        hostname, hostname_res = remote_hostname()
        if not r.step("ssh connectivity", hostname is not None, hostname or hostname_res.stderr.strip()):
            raise DeployHalt("SSH connectivity to production failed", ctx)

        if not r.step("production app path exists", remote_path_exists(), PROD_APP_PATH):
            raise DeployHalt(f"{PROD_APP_PATH} missing on production", ctx)

        if not r.step("production repo valid", remote_is_git_repo()):
            raise DeployHalt(f"{PROD_APP_PATH} is not a git repository", ctx)

        pre_deploy_remote_sha = remote_head_sha()
        ctx["pre_deploy_commit"] = pre_deploy_remote_sha or ctx["pre_deploy_commit"]
        r.note(f"  production HEAD before deploy: {pre_deploy_remote_sha}")

        if not r.step("production venv python exists", remote_venv_python_exists(), PROD_VENV_PYTHON):
            raise DeployHalt("production venv python missing", ctx)

        svc_state = remote_service_state()
        if not r.step("production service loaded", svc_state.get("LoadState") == "loaded",
                      f"LoadState={svc_state.get('LoadState', '?')}"):
            raise DeployHalt(f"systemd service '{PROD_SERVICE}' not found", ctx)

        remote_clean = remote_tracked_clean()
        if remote_clean is None:
            r.step("production tracked tree clean", False, "could not determine (git error)")
            raise DeployHalt("could not determine production tracked working-tree state", ctx)
        if not r.step("production tracked tree clean", remote_clean):
            modified = remote_modified_tracked_files()
            r.note(f"  modified tracked files on production: {', '.join(modified) if modified else 'unknown'}")
            raise DeployHalt("unexpected tracked modifications on production — refusing to overwrite", ctx)

        # --- Phase 4: production backup --------------------------------------------------
        r.section("backup")
        db_size = remote_db_size()
        if db_size is not None:
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_path = f"{PROD_BACKUPS_DIR}/app_auth-{ts}.db"
            mkdir_res = run_remote(f"mkdir -p {q(PROD_BACKUPS_DIR)}")
            if not r.step("backup directory ready", mkdir_res.ok, mkdir_res.stderr.strip()):
                raise DeployHalt("could not create production backup directory", ctx)

            cp_res = run_remote(f"cp -p {q(PROD_DB_PATH)} {q(backup_path)}")
            if not r.step("db backup copied", cp_res.ok, cp_res.stderr.strip()):
                raise DeployHalt("production DB backup failed", ctx)

            backup_size = remote_db_size(backup_path)
            size_match = backup_size == db_size
            if not r.step("backup size matches source", size_match, f"source={db_size} backup={backup_size}"):
                raise DeployHalt("backup size does not match source DB — stopping before deploy", ctx)

            src_hash = remote_sha256(PROD_DB_PATH)
            backup_hash = remote_sha256(backup_path)
            if src_hash and backup_hash:
                if not r.step("backup sha256 matches source", src_hash == backup_hash):
                    raise DeployHalt("backup checksum mismatch — stopping before deploy", ctx)
            else:
                r.warn("sha256 verification skipped", "sha256sum unavailable on remote")

            ctx["backup_path"] = backup_path
            r.note(f"  backup: {backup_path}")
        else:
            r.warn("no production auth DB found to back up", PROD_DB_PATH)

        # --- Phase 5: remote deploy --------------------------------------------------------
        r.section("deploy")
        fetch_res = run_remote(f"cd {q(PROD_APP_PATH)} && git fetch origin 2>&1")
        if not r.step("remote fetch origin", fetch_res.ok, fetch_res.stdout.strip() if not fetch_res.ok else ""):
            raise DeployHalt("remote git fetch failed", ctx)

        ctx["production_modified"] = True  # about to pull; treat as modified from here on
        pull_res = run_remote(f"cd {q(PROD_APP_PATH)} && git pull --ff-only origin {q(PRIMARY_BRANCH)} 2>&1")
        if not r.step("remote pull --ff-only", pull_res.ok, pull_res.stdout.strip() if not pull_res.ok else ""):
            raise DeployHalt("remote fast-forward pull failed (non-ff or conflict)", ctx)

        deployed_sha = remote_head_sha()
        if not r.step("deployed HEAD matches EXPECTED_COMMIT", deployed_sha == expected_commit,
                      f"deployed={deployed_sha} expected={expected_commit}"):
            raise DeployHalt("deployed commit does not match expected commit — not restarting", ctx)

        # --- Phase 6: remote compile ---------------------------------------------------------
        r.section("remote compile")
        compile_failed = False
        for target in COMPILE_TARGETS:
            comp_res = run_remote(
                f"cd {q(PROD_APP_PATH)} && {q(PROD_VENV_PYTHON)} -m py_compile {q(target)} 2>&1"
            )
            if not r.step(f"compile {target}", comp_res.ok, comp_res.stdout.strip() if not comp_res.ok else ""):
                compile_failed = True
        if compile_failed:
            raise DeployHalt("remote compile failed after pull — service NOT restarted", ctx)

        # --- Phase 7: restart -----------------------------------------------------------------
        r.section("restart")
        restart_res = run_remote(f"systemctl restart {q(PROD_SERVICE)} 2>&1")
        if not r.step("service restart command", restart_res.ok, restart_res.stdout.strip() if not restart_res.ok else ""):
            raise DeployHalt("systemctl restart failed", ctx)
        ctx["service_restarted"] = True

        time.sleep(RESTART_WAIT_SECONDS)
        active_res = run_remote(f"systemctl is-active {q(PROD_SERVICE)} 2>&1")
        is_active = active_res.stdout.strip() == "active"
        if not r.step("service active", is_active, active_res.stdout.strip()):
            raise DeployHalt("service failed to become active after restart", ctx)

        journal_res = run_remote(f"journalctl -u {q(PROD_SERVICE)} -n 100 --no-pager 2>&1")
        journal_hits = []
        if journal_res.ok:
            for line in journal_res.stdout.splitlines():
                if any(pat.search(line) for pat in JOURNAL_ERROR_PATTERNS):
                    journal_hits.append(line)
        if journal_hits:
            r.warn("journal shows possible errors since restart", f"{len(journal_hits)} matching line(s)")
            for line in journal_hits[:5]:
                r.note(f"    {line[:200]}")
        else:
            r.step("journal clean of known error patterns", True)

        # --- Phase 8: live smoke test -----------------------------------------------------------
        r.section("live")
        smoke_results = run_smoke_tests(reporter=r)
        smoke_failed = any(not ok for _, _, ok, _ in smoke_results)

        # --- Phase 9: final report ----------------------------------------------------------------
        r.note()
        r.note("DEPLOY FAIL (smoke tests failed — service is running, review before relying on it)"
               if smoke_failed else "DEPLOY PASS")
        r.note()
        r.note(f"Previous commit: {pre_deploy_remote_sha}")
        r.note(f"Current commit:  {deployed_sha}")
        if ctx["backup_path"]:
            r.note(f"DB backup: {ctx['backup_path']}")
        r.note()
        r.note("Rollback info:")
        r.note("leadme-deploy --rollback-info")

        write_state({
            "last_deploy_timestamp": utc_now_iso(),
            "previous_production_commit": pre_deploy_remote_sha or "",
            "deployed_commit": deployed_sha or "",
            "backup_path": ctx["backup_path"] or "",
            "result": "FAIL" if smoke_failed else "PASS",
        })

        print(r.render())
        return 1 if smoke_failed else 0

    except DeployHalt as halt:
        ctx = halt.ctx
        ctx["reason"] = halt.reason
        _print_failure_summary(r, ctx)
        try:
            write_state({
                "last_deploy_timestamp": utc_now_iso(),
                "previous_production_commit": ctx.get("pre_deploy_commit") or "",
                "deployed_commit": "",
                "backup_path": ctx.get("backup_path") or "",
                "result": "FAIL",
            })
        except ValueError:
            pass
        print(r.render())
        return 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="leadme-deploy",
        description="Deployment automation for LeadMeLeads.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="local pre-deploy validation only")
    mode.add_argument("--status", action="store_true", help="show local/remote/live status, no mutations")
    mode.add_argument("--smoke", action="store_true", help="run GET-only live smoke tests")
    mode.add_argument("--doctor", action="store_true", help="diagnose the local/remote/live environment")
    mode.add_argument("--dry-run", action="store_true", help="print the deploy plan without executing it")
    mode.add_argument("--rollback-info", action="store_true", help="show rollback plan, nothing executed")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    identity_ok, identity_detail = validate_repo_identity()
    if not identity_ok:
        print("LeadMeLeads Repository Check")
        print("────────────────────────────")
        print(f"[FAIL] repository identity — {identity_detail}")
        print("STOPPED: refusing to contact or modify production from the wrong repository")
        return 1

    if args.check:
        return cmd_check(args)
    if args.status:
        return cmd_status(args)
    if args.smoke:
        return cmd_smoke(args)
    if args.doctor:
        return cmd_doctor(args)
    if args.dry_run:
        return cmd_dry_run(args)
    if args.rollback_info:
        return cmd_rollback_info(args)

    return cmd_deploy(args)


if __name__ == "__main__":
    sys.exit(main())
