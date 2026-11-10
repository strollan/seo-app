"""Test support: a temporary discovery spend ledger for tests that exercise the
organic DataForSEO request path with a mocked transport.

Paid discovery fails closed without a ledger (agents/discovery_spend_guard.py),
so these modules get a throwaway ledger plus a roomy scan guard: they test
retry / circuit-breaker / diagnostics behavior, not spending caps (those are
covered in scripts/test_lead_maps_discovery.py). Nothing here touches the real
ledger or sends anything.
"""
import os
import tempfile
from pathlib import Path
from unittest import mock

_STATE = {}


def install():
    from agents import discovery_spend_guard as spend

    tmp = tempfile.TemporaryDirectory()
    ledger = Path(tmp.name) / "ledger.json"
    spend.initialize_ledger(ledger)
    env = mock.patch.dict(os.environ, {"LEADBOT_DISCOVERY_LEDGER_PATH": str(ledger)})
    env.start()
    guard = spend.DiscoverySpendGuard(job_id="unit-test", max_scan_usd=1000.0, daily_max_usd=1000.0,
                                      ledger_path=ledger)
    ctx = spend.scan_guard(guard)
    ctx.__enter__()
    _STATE.update(tmp=tmp, env=env, ctx=ctx)


def uninstall():
    ctx, env, tmp = _STATE.pop("ctx", None), _STATE.pop("env", None), _STATE.pop("tmp", None)
    if ctx:
        ctx.__exit__(None, None, None)
    if env:
        env.stop()
    if tmp:
        tmp.cleanup()
