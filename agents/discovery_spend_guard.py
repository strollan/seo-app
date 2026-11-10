"""Persistent, cross-process spending guard for ALL paid Lead Finder discovery.

Covers every DataForSEO discovery request (Google Maps and organic SERP).

Rules (owner policy 2026-11-10):
- DataForSEO OFF ("Safe Mode", LEADBOT_DATAFORSEO_ENABLED != 1) => no paid
  discovery request of any kind, Maps included.
- Every request reserves its worst-case cost BEFORE it is sent, against:
    * a per-scan ceiling (default $0.02),
    * a per-UTC-day ceiling shared by all scans and processes (default $0.25),
    * a per-scan request cap per provider (Maps: 1 request per scan).
- The reservation is later settled to the provider-reported cost. An unknown
  cost (network error, unreadable body, missing "cost") stays charged at the
  worst-case ceiling. A process that dies after reserving leaves its
  reservation charged at the ceiling (restart-safe, conservative).
- The ledger must exist and be valid. Missing, unreadable or corrupted =>
  refuse to spend (fail closed). It is created only by an explicit
  initialize_ledger() call (scripts/leadbot_discovery_ledger.py init).
- Environment overrides may only LOWER the defaults. Raising a limit needs a
  code change and review.
- No retries or paid fallbacks are performed here; callers that retry must
  reserve again for each attempt.

Concurrency: a process-wide threading lock plus an exclusive fcntl lock on a
sidecar lock file serialize read-check-write across scan subprocesses. The
ledger is replaced atomically (unique temp file + fsync + os.replace).
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional

try:  # POSIX only. Production and WSL are Linux; elsewhere we fail closed.
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore

LEDGER_SCHEMA = "leadbot-discovery-spend/1"
_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER_PATH = _REPO_ROOT / "data" / "leadbot_discovery_spend_ledger.json"

# Hard defaults. Env vars may lower these, never raise them.
DEFAULT_MAX_SCAN_USD = 0.02
DEFAULT_DAILY_MAX_USD = 0.25
DEFAULT_MAX_CALLS = {"maps": 1}  # per scan; organic is bounded by the USD caps
# Worst-case price assumed for a request until the provider reports the real
# one. Maps observed $0.002 (2026-11-10); organic observed $0.0095.
DEFAULT_PER_CALL_CEILING_USD = {"maps": 0.01, "organic": 0.01}
PROVIDERS = ("maps", "organic")
KEEP_DAYS = 40

_THREAD_LOCK = threading.RLock()
_EPS = 1e-9


class SpendLimitExceeded(RuntimeError):
    """Raised BEFORE a request is sent when it may not be sent."""


class SafeModeError(SpendLimitExceeded):
    """DataForSEO is OFF (Safe Mode): no paid discovery at all."""


class LedgerUnavailable(SpendLimitExceeded):
    """The spend ledger is missing, unreadable or corrupted: fail closed."""


class _Refused(Exception):
    """Internal: a limit refused the reservation (re-raised as SpendLimitExceeded)."""


def paid_discovery_enabled() -> bool:
    """The existing DataForSEO ON/OFF (Safe Mode) switch, shared with organic."""
    return os.getenv("LEADBOT_DATAFORSEO_ENABLED", "0").strip() == "1"


def _env_lowered(name: str, default: float) -> float:
    raw = str(os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value != value or value < 0:  # NaN / negative: ignore
        return default
    return min(value, default)


def ledger_path() -> Path:
    raw = str(os.getenv("LEADBOT_DISCOVERY_LEDGER_PATH") or "").strip()
    return Path(raw) if raw else DEFAULT_LEDGER_PATH


def utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- ledger file ----------------------------------------------------------------

def _empty_ledger() -> Dict[str, Any]:
    return {"schema": LEDGER_SCHEMA, "created_at": _utc_now_iso(), "requests": []}


def initialize_ledger(path: Optional[Path] = None) -> bool:
    """Create an empty ledger if none exists. Never overwrites. True if created."""
    path = Path(path) if path else ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(_empty_ledger(), fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    return True


def _num(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("not a number")
    value = float(value)
    if value != value or value < 0:
        raise ValueError("negative or NaN")
    return value


def _validate(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict) or data.get("schema") != LEDGER_SCHEMA:
        raise ValueError("wrong schema")
    requests = data.get("requests")
    if not isinstance(requests, list):
        raise ValueError("requests not a list")
    for req in requests:
        if not isinstance(req, dict):
            raise ValueError("request not an object")
        if req.get("status") not in {"reserved", "settled", "released"}:
            raise ValueError("bad request status")
        if not isinstance(req.get("day"), str) or not isinstance(req.get("id"), str):
            raise ValueError("bad request id/day")
        _num(req.get("ceiling_usd"))
        if req["status"] == "settled":
            _num(req.get("cost_usd"))
    return data


def _load(path: Path) -> Dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise LedgerUnavailable("discovery spend ledger is missing; refusing to spend") from None
    except OSError:
        raise LedgerUnavailable("discovery spend ledger is unreadable; refusing to spend") from None
    try:
        return _validate(json.loads(text))
    except ValueError:
        raise LedgerUnavailable("discovery spend ledger is corrupted; refusing to spend") from None


def _save(path: Path, data: Dict[str, Any], today: str) -> None:
    try:
        cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    except ValueError:
        cutoff = ""
    if cutoff:
        # Old settled/released rows only; an unsettled reservation is evidence.
        data["requests"] = [r for r in data["requests"] if r["day"] >= cutoff or r["status"] == "reserved"]
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    if fcntl is None:
        raise LedgerUnavailable("no cross-process file locking on this platform; refusing to spend")
    with _THREAD_LOCK:
        if not path.parent.is_dir():
            raise LedgerUnavailable("discovery spend ledger is missing; refusing to spend")
        lock_path = path.with_name(path.name + ".lock")
        try:
            fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            raise LedgerUnavailable("discovery spend ledger lock is unavailable; refusing to spend") from None
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError:
                raise LedgerUnavailable("discovery spend ledger lock is unavailable; refusing to spend") from None
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def committed_for_day(data: Dict[str, Any], day: str) -> float:
    """Worst-case committed spend: settled cost + open reservations at ceiling."""
    total = 0.0
    for req in data.get("requests") or []:
        if req.get("day") != day:
            continue
        if req["status"] == "reserved":
            total += float(req["ceiling_usd"])
        elif req["status"] == "settled":
            total += float(req["cost_usd"])
    return round(total, 6)


def read_day_summary(day: Optional[str] = None, path: Optional[Path] = None) -> Dict[str, Any]:
    """Read-only summary for operators (raises LedgerUnavailable like spending would)."""
    path = Path(path) if path else ledger_path()
    day = day or utc_day()
    with _locked(path):
        data = _load(path)
    reqs = [r for r in data["requests"] if r["day"] == day]
    return {
        "day": day,
        "committed_usd": committed_for_day(data, day),
        "requests": len([r for r in reqs if r["status"] != "released"]),
        "open_reservations": len([r for r in reqs if r["status"] == "reserved"]),
        "by_provider": {
            p: len([r for r in reqs if r.get("provider") == p and r["status"] != "released"]) for p in PROVIDERS
        },
    }


# -- guard ------------------------------------------------------------------------

class DiscoverySpendGuard:
    """Spending limits for ONE scan, backed by the shared persistent ledger."""

    def __init__(
        self,
        job_id: str = "",
        max_calls: Optional[int] = None,
        max_scan_usd: Optional[float] = None,
        per_call_ceiling_usd: Optional[float] = None,
        daily_max_usd: Optional[float] = None,
        ledger_path: Optional[Path] = None,
        today: Optional[Callable[[], str]] = None,
        organic_ceiling_usd: Optional[float] = None,
    ):
        self.job_id = str(job_id or "")
        self.max_calls = {
            "maps": max_calls if max_calls is not None else int(
                _env_lowered("LEADBOT_MAPS_MAX_CALLS_PER_SCAN", DEFAULT_MAX_CALLS["maps"])
            ),
        }
        self.max_scan_usd = (
            max_scan_usd if max_scan_usd is not None
            else _env_lowered("LEADBOT_DISCOVERY_MAX_SCAN_USD", DEFAULT_MAX_SCAN_USD)
        )
        self.daily_max_usd = (
            daily_max_usd if daily_max_usd is not None
            else _env_lowered("LEADBOT_DISCOVERY_DAILY_MAX_USD", DEFAULT_DAILY_MAX_USD)
        )
        maps_ceiling = (
            per_call_ceiling_usd if per_call_ceiling_usd is not None
            else DEFAULT_PER_CALL_CEILING_USD["maps"]
        )
        organic_ceiling = (
            organic_ceiling_usd if organic_ceiling_usd is not None
            else (per_call_ceiling_usd if per_call_ceiling_usd is not None else DEFAULT_PER_CALL_CEILING_USD["organic"])
        )
        self.ceilings = {"maps": float(maps_ceiling), "organic": float(organic_ceiling)}
        self.ledger_path = Path(ledger_path) if ledger_path else globals()["ledger_path"]()
        self._today = today or utc_day
        self.calls: Dict[str, int] = {p: 0 for p in PROVIDERS}
        self.spent_usd = 0.0  # settled cost for this scan (unknown => ceiling)
        self._open: Dict[str, Dict[str, Any]] = {}  # reservation id -> {provider, ceiling, day}
        self._last_id = ""
        self.blocked_reason = ""
        self.events: list = []  # per-request records for this scan (no secrets)

    # Back-compat with the staged MapsSpendGuard API.
    @property
    def per_call_ceiling_usd(self) -> float:
        return self.ceilings["maps"]

    @property
    def total_calls(self) -> int:
        return sum(self.calls.values())

    def _open_ceilings(self) -> float:
        return sum(r["ceiling"] for r in self._open.values())

    def daily_spent(self) -> float:
        with _locked(self.ledger_path):
            data = _load(self.ledger_path)
        return committed_for_day(data, self._today())

    def reserve(self, provider: str = "maps", **meta: Any) -> str:
        """Reserve the worst case for one request. Raises before anything is sent."""
        if provider not in PROVIDERS:
            raise ValueError(f"unknown provider {provider!r}")
        if not paid_discovery_enabled():
            raise SafeModeError("DataForSEO is OFF (Safe Mode); no paid discovery request sent")
        ceiling = self.ceilings[provider]
        # The whole check-and-reserve runs under the process lock and the
        # cross-process file lock, so concurrent threads sharing this scan's
        # guard (e.g. a timed-out search batch still running) and other scan
        # processes can never both pass a limit that only one of them fits.
        try:
            with _locked(self.ledger_path):
                return self._reserve_locked(provider, ceiling, meta)
        except _Refused as refused:
            raise SpendLimitExceeded(str(refused)) from None

    def _reserve_locked(self, provider: str, ceiling: float, meta: Dict[str, Any]) -> str:
        reason = ""
        limit = self.max_calls.get(provider)
        if self.blocked_reason:
            reason = self.blocked_reason
        elif limit is not None and self.calls[provider] + 1 > limit:
            reason = f"{provider} request limit reached ({self.calls[provider]}/{limit} per scan)"
        elif self.spent_usd + self._open_ceilings() + ceiling > self.max_scan_usd + _EPS:
            reason = (
                f"scan budget ${self.max_scan_usd:.4f} would be exceeded "
                f"(committed ${self.spent_usd + self._open_ceilings():.4f} + worst case ${ceiling:.4f})"
            )
        if reason:
            self._block(provider, reason, meta, sticky=False)
            raise _Refused(reason)

        day = self._today()
        res_id = uuid.uuid4().hex[:16]
        data = _load(self.ledger_path)  # LedgerUnavailable propagates: nothing sent
        spent_today = committed_for_day(data, day)
        if spent_today + ceiling > self.daily_max_usd + _EPS:
            reason = (
                f"daily budget ${self.daily_max_usd:.4f} would be exceeded "
                f"(committed today ${spent_today:.4f})"
            )
            self._block(provider, reason, meta, sticky=True)
            raise _Refused(reason)
        data["requests"].append({
            "id": res_id,
            "day": day,
            "provider": provider,
            "job_id": self.job_id,
            "status": "reserved",
            "ceiling_usd": ceiling,
            "reserved_at": _utc_now_iso(),
            "meta": {k: v for k, v in meta.items() if isinstance(v, (str, int, float, bool))},
        })
        try:
            _save(self.ledger_path, data, day)
        except OSError:
            raise LedgerUnavailable("discovery spend ledger could not be written; refusing to spend") from None
        self.calls[provider] += 1
        self._open[res_id] = {"provider": provider, "ceiling": ceiling, "day": day}
        self._last_id = res_id
        return res_id

    def _block(self, provider: str, reason: str, meta: Dict[str, Any], sticky: bool) -> None:
        if sticky and not self.blocked_reason:
            self.blocked_reason = reason
        self.events.append({"provider": provider, "status": "blocked", "reason": reason})

    def _resolve_id(self, res_id: Optional[str]) -> str:
        res_id = res_id or self._last_id
        if res_id not in self._open:
            raise KeyError("no open reservation to settle")
        return res_id

    def record(
        self,
        reported_cost: Any = None,
        res_id: Optional[str] = None,
        provider_status: Any = None,
        results: Optional[int] = None,
        **details: Any,
    ) -> float:
        """Settle a sent request at the reported cost (unknown => ceiling)."""
        with _THREAD_LOCK:
            res_id = self._resolve_id(res_id)
            info = self._open.pop(res_id)
            ceiling = info["ceiling"]
            known = True
            try:
                cost = _num(reported_cost)
            except ValueError:
                cost, known = ceiling, False
            self.spent_usd = round(self.spent_usd + cost, 6)
            if cost > ceiling + _EPS:
                # Pricing is higher than our worst-case assumption: stop spending.
                self.blocked_reason = f"reported cost ${cost:.4f} exceeded per-call ceiling ${ceiling:.4f}"
        clean = {k: v for k, v in details.items() if isinstance(v, (str, int, float, bool, dict))}
        with _locked(self.ledger_path):
            data = _load(self.ledger_path)
            for req in data["requests"]:
                if req["id"] == res_id:
                    req.update({
                        "status": "settled",
                        "cost_usd": cost,
                        "cost_known": known,
                        "settled_at": _utc_now_iso(),
                        "provider_status": None if provider_status is None else str(provider_status),
                        "results": results,
                        **clean,
                    })
                    break
            else:
                # Our reservation vanished from the ledger: someone edited it.
                # Re-add it settled so the day total stays conservative.
                data["requests"].append({
                    "id": res_id, "day": info["day"], "provider": info["provider"],
                    "job_id": self.job_id, "status": "settled", "ceiling_usd": ceiling,
                    "cost_usd": cost, "cost_known": known, "settled_at": _utc_now_iso(),
                    "provider_status": None if provider_status is None else str(provider_status),
                    "results": results, "note": "reservation missing at settle time",
                })
            _save(self.ledger_path, data, self._today())
        self.events.append({
            "provider": info["provider"], "status": "settled", "cost_usd": cost, "cost_known": known,
            "provider_status": None if provider_status is None else str(provider_status), "results": results,
        })
        return cost

    def annotate(self, res_id: Optional[str] = None, **details: Any) -> None:
        """Attach post-processing facts (e.g. rejected listing counts) to a settled request."""
        res_id = res_id or self._last_id
        if not res_id:
            return
        clean = {k: v for k, v in details.items() if isinstance(v, (str, int, float, bool, dict))}
        with _locked(self.ledger_path):
            data = _load(self.ledger_path)
            for req in data["requests"]:
                if req["id"] == res_id:
                    req.update(clean)
                    _save(self.ledger_path, data, self._today())
                    return

    def release_unsent(self, res_id: Optional[str] = None) -> None:
        """Undo a reservation for a request that was definitely never sent."""
        with _THREAD_LOCK:
            res_id = self._resolve_id(res_id)
            info = self._open.pop(res_id)
            self.calls[info["provider"]] = max(0, self.calls[info["provider"]] - 1)
        with _locked(self.ledger_path):
            data = _load(self.ledger_path)
            for req in data["requests"]:
                if req["id"] == res_id and req["status"] == "reserved":
                    req["status"] = "released"
                    req["released_at"] = _utc_now_iso()
            _save(self.ledger_path, data, self._today())

    def summary(self) -> Dict[str, Any]:
        return {
            "calls": dict(self.calls),
            "max_calls": dict(self.max_calls),
            "spent_usd": self.spent_usd,
            "max_scan_usd": self.max_scan_usd,
            "per_call_ceiling_usd": dict(self.ceilings),
            "daily_max_usd": self.daily_max_usd,
            "blocked_reason": self.blocked_reason,
        }


# -- active guard for the running scan ---------------------------------------------
# Each live scan runs in its own subprocess, so one module-level slot is one
# scan. Paid calls made while no scan guard is active (scripts, ad-hoc tools)
# still get a fresh guard that enforces the shared daily ledger.

_ACTIVE: Optional[DiscoverySpendGuard] = None


def active_guard() -> Optional[DiscoverySpendGuard]:
    return _ACTIVE


def guard_for_request(job_id: str = "adhoc") -> DiscoverySpendGuard:
    return _ACTIVE if _ACTIVE is not None else DiscoverySpendGuard(job_id=job_id)


@contextlib.contextmanager
def scan_guard(guard: DiscoverySpendGuard) -> Iterator[DiscoverySpendGuard]:
    global _ACTIVE
    previous = _ACTIVE
    _ACTIVE = guard
    try:
        yield guard
    finally:
        _ACTIVE = previous
