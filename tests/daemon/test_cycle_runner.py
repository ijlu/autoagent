"""Tests for bot/daemon/cycle_runner.py.

Verifies that CycleRunner:
- Returns a successful CycleReport when trade.main() returns a dict
- Catches exceptions from trade.main() and returns success=False
- Does not close the shared DB connection between cycles
- Increments cycle_count / error_count correctly
- Reports health
"""

from __future__ import annotations

import sqlite3
import sys
import types
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def stub_trade_module(monkeypatch):
    """CycleRunner does `import trade` inside run_once(). The real
    trade.py transitively imports the cryptography library, which has
    an arm64/x86_64 binary conflict in the test environment.

    Register a stub `trade` module in sys.modules so `import trade`
    finds our fake instead of the real one. Tests then `patch("trade.main", ...)`
    as normal."""
    if "trade" in sys.modules and not isinstance(sys.modules["trade"], types.ModuleType):
        yield
        return
    fake = types.ModuleType("trade")
    fake.main = MagicMock(name="trade.main", return_value={})
    orig = sys.modules.get("trade")
    sys.modules["trade"] = fake
    try:
        yield
    finally:
        if orig is None:
            sys.modules.pop("trade", None)
        else:
            sys.modules["trade"] = orig


from bot.daemon.cycle_runner import CycleReport, CycleRunner  # noqa: E402


@pytest.fixture
def fake_conn():
    """A dummy sqlite connection. CycleRunner only stores it and passes
    it to trade.main — we're mocking trade.main, so its contents don't
    matter."""
    return sqlite3.connect(":memory:")


# ═════════════════════════════════════════════════════════════════════════════
# Happy path
# ═════════════════════════════════════════════════════════════════════════════

def test_cycle_report_from_successful_main(fake_conn):
    """trade.main() returns a well-formed dict → CycleReport reflects it."""
    fake_result = {
        "markets_scanned": 2500,
        "opportunities": [{"ticker": "KXFOO"}, {"ticker": "KXBAR"}],
        "orders_placed": [{"order_id": "abc"}],
        "positions_managed": 47,
        "settlements_recorded": 3,
        "halted": False,
        "halt_reason": "",
    }

    runner = CycleRunner(fake_conn)
    with patch("trade.main", return_value=fake_result) as mock_main:
        report = runner.run_once()

    # trade.main was called with our connection and the daemon flags.
    mock_main.assert_called_once()
    kwargs = mock_main.call_args.kwargs
    assert kwargs["conn"] is fake_conn
    assert kwargs["close_conn"] is False
    assert kwargs["write_json_report"] is False

    assert report.success is True
    assert report.markets_scanned == 2500
    assert report.opportunities == 2
    assert report.orders_placed == 1
    assert report.positions_managed == 47
    assert report.settlements_recorded == 3
    assert report.halted is False
    assert runner.cycle_count == 1
    assert runner.error_count == 0


# ═════════════════════════════════════════════════════════════════════════════
# Halted cycle still counts as success
# ═════════════════════════════════════════════════════════════════════════════

def test_halted_cycle_is_still_success(fake_conn):
    """A halt is a business-logic outcome, not a code failure. The
    cycle ran to completion, so success=True."""
    fake_result = {
        "markets_scanned": 0,
        "opportunities": [],
        "orders_placed": [],
        "positions_managed": 0,
        "settlements_recorded": 0,
        "halted": True,
        "halt_reason": "daily_loss_limit",
    }

    runner = CycleRunner(fake_conn)
    with patch("trade.main", return_value=fake_result):
        report = runner.run_once()

    assert report.success is True
    assert report.halted is True
    assert report.halt_reason == "daily_loss_limit"


# ═════════════════════════════════════════════════════════════════════════════
# Exception handling
# ═════════════════════════════════════════════════════════════════════════════

def test_cycle_catches_exception_from_main(fake_conn):
    """trade.main() raising must not propagate. CycleRunner records
    error_count and returns a report with success=False."""
    runner = CycleRunner(fake_conn)
    with patch("trade.main", side_effect=RuntimeError("boom")):
        report = runner.run_once()  # must NOT raise

    assert report.success is False
    assert "RuntimeError" in (report.error or "")
    assert "boom" in (report.error or "")
    assert runner.cycle_count == 0
    assert runner.error_count == 1


def test_cycle_runner_recovers_across_cycles(fake_conn):
    """A failing cycle doesn't poison subsequent cycles. Runner alternates
    error → success → error → success cleanly."""
    runner = CycleRunner(fake_conn)

    good_result = {
        "markets_scanned": 10, "opportunities": [], "orders_placed": [],
        "positions_managed": 0, "settlements_recorded": 0,
        "halted": False, "halt_reason": "",
    }
    call_idx = [0]

    def flaky(*a, **kw):
        call_idx[0] += 1
        if call_idx[0] % 2 == 1:
            raise RuntimeError(f"fail on call {call_idx[0]}")
        return good_result

    with patch("trade.main", side_effect=flaky):
        r1 = runner.run_once()  # fail
        r2 = runner.run_once()  # ok
        r3 = runner.run_once()  # fail
        r4 = runner.run_once()  # ok

    assert r1.success is False
    assert r2.success is True
    assert r3.success is False
    assert r4.success is True
    assert runner.cycle_count == 2
    assert runner.error_count == 2


# ═════════════════════════════════════════════════════════════════════════════
# Connection is not closed
# ═════════════════════════════════════════════════════════════════════════════

def test_connection_stays_open_across_cycles(fake_conn):
    """Daemon depends on the connection surviving many cycles. Verify
    it's still usable after multiple run_once() calls."""
    fake_result = {
        "markets_scanned": 0, "opportunities": [], "orders_placed": [],
        "positions_managed": 0, "settlements_recorded": 0,
        "halted": False, "halt_reason": "",
    }
    runner = CycleRunner(fake_conn)
    with patch("trade.main", return_value=fake_result):
        for _ in range(5):
            runner.run_once()

    # If the connection were closed, this would raise ProgrammingError.
    fake_conn.execute("SELECT 1").fetchone()


# ═════════════════════════════════════════════════════════════════════════════
# Health snapshot
# ═════════════════════════════════════════════════════════════════════════════

def test_health_snapshot_after_mixed_cycles(fake_conn):
    runner = CycleRunner(fake_conn)
    good = {
        "markets_scanned": 5, "opportunities": [], "orders_placed": [],
        "positions_managed": 0, "settlements_recorded": 0,
        "halted": False, "halt_reason": "",
    }

    # One success
    with patch("trade.main", return_value=good):
        runner.run_once()
    # One failure
    with patch("trade.main", side_effect=ValueError("nope")):
        runner.run_once()

    h = runner.health()
    assert h["cycle_count"] == 1
    assert h["error_count"] == 1
    assert h["last_cycle_success"] is False
    assert "ValueError" in (h["last_error"] or "")
    assert h["last_cycle_age_s"] is not None
    assert h["last_cycle_age_s"] >= 0


# ═════════════════════════════════════════════════════════════════════════════
# None / non-dict return
# ═════════════════════════════════════════════════════════════════════════════

def test_non_dict_return_coerced_gracefully(fake_conn):
    """trade.main() can return None in edge cases. CycleRunner must
    still produce a valid report."""
    runner = CycleRunner(fake_conn)
    with patch("trade.main", return_value=None):
        report = runner.run_once()
    assert report.success is True
    assert report.markets_scanned == 0


def test_main_returning_error_dict(fake_conn):
    """trade.main() returns {'error': '...'} on portfolio fetch failure.
    CycleRunner still treats it as a successful cycle (ran to completion)."""
    runner = CycleRunner(fake_conn)
    with patch("trade.main", return_value={"error": "portfolio_fetch_failed"}):
        report = runner.run_once()
    assert report.success is True
    assert report.markets_scanned == 0
