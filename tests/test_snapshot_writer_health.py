"""Regression tests for the weather_forecast_snapshots writer health counters.

Pins the fix shipped after the 26APR26 outage: a ~22h silent failure of
weather_ensemble_v2._write_snapshots was invisible because errors were
swallowed by ``print(...)``. The fix adds module-level counters
(_SNAPSHOT_WRITE_OK / _SNAPSHOT_WRITE_FAIL / _SNAPSHOT_BUILD_FAIL) that
the daemon health log reads and logs at WARNING level when fail>0, so
any future recurrence surfaces within HEALTH_LOG_INTERVAL_S.

If you change the snapshot writer's error-handling semantics, update
this test in the same PR.
"""
from __future__ import annotations

from unittest import mock

import pytest

from bot.signals import weather_ensemble_v2 as v2


@pytest.fixture(autouse=True)
def _reset_counters():
    """Each test starts from zero counters and leaves them at zero."""
    v2.get_and_reset_snapshot_health_stats()
    yield
    v2.get_and_reset_snapshot_health_stats()


def test_get_and_reset_returns_zeros_on_first_call():
    stats = v2.get_and_reset_snapshot_health_stats()
    assert stats == {"write_ok": 0, "write_fail": 0, "build_fail": 0}


def test_get_and_reset_resets_after_read():
    v2._SNAPSHOT_WRITE_OK = 5
    v2._SNAPSHOT_WRITE_FAIL = 2
    v2._SNAPSHOT_BUILD_FAIL = 1
    first = v2.get_and_reset_snapshot_health_stats()
    second = v2.get_and_reset_snapshot_health_stats()
    assert first == {"write_ok": 5, "write_fail": 2, "build_fail": 1}
    assert second == {"write_ok": 0, "write_fail": 0, "build_fail": 0}


def test_write_snapshots_increments_ok_on_success():
    rows = [(
        "2026-04-28T12:00:00+00:00", "KXHIGHMIA", "KXHIGHMIA-26APR28-T85",
        "hrrr", None, 84.5, 1.2, 6,
    )]
    with mock.patch.object(v2, "db_write") as mocked:
        mocked.return_value = None
        v2._write_snapshots(rows)
    stats = v2.get_and_reset_snapshot_health_stats()
    assert stats["write_ok"] == 1
    assert stats["write_fail"] == 0


def test_write_snapshots_increments_fail_on_db_error():
    rows = [(
        "2026-04-28T12:00:00+00:00", "KXHIGHMIA", "KXHIGHMIA-26APR28-T85",
        "hrrr", None, 84.5, 1.2, 6,
    )]
    with mock.patch.object(v2, "db_write") as mocked:
        mocked.side_effect = RuntimeError("simulated db failure")
        # Must not re-raise — caller relies on best-effort semantics.
        v2._write_snapshots(rows)
    stats = v2.get_and_reset_snapshot_health_stats()
    assert stats["write_ok"] == 0
    assert stats["write_fail"] == 1


def test_write_snapshots_skips_empty_rows_without_counter_change():
    v2._write_snapshots([])
    stats = v2.get_and_reset_snapshot_health_stats()
    # Empty input is not a real write attempt — neither ok nor fail.
    assert stats == {"write_ok": 0, "write_fail": 0, "build_fail": 0}


def test_health_log_emits_warning_when_failures_present(caplog):
    """The daemon health line must escalate to WARNING when fail>0,
    so log scans for ``[health].*WARNING`` catch the regression we hit
    on 26APR26 (silent swallow → 22h of dropped writes)."""
    import logging
    from bot.daemon import main as daemon_main

    v2._SNAPSHOT_WRITE_OK = 10
    v2._SNAPSHOT_WRITE_FAIL = 3
    v2._SNAPSHOT_BUILD_FAIL = 0

    # _log_health expects pollers/cycle_runner/scheduler. Provide minimal
    # stand-ins — the snapshot block is the only thing under test.
    cycle_runner = mock.MagicMock()
    cycle_runner.health.return_value = {
        "cycle_count": 0, "error_count": 0,
        "last_cycle_success": None, "last_cycle_duration_s": None,
    }
    scheduler = mock.MagicMock()
    scheduler.health.return_value = {"tasks": {}}

    with caplog.at_level(logging.INFO, logger="bot.daemon.main"):
        daemon_main._log_health([], cycle_runner, scheduler)

    snap_records = [r for r in caplog.records if "wx_snapshots" in r.message]
    assert len(snap_records) == 1, (
        f"Expected exactly one wx_snapshots health line, got "
        f"{len(snap_records)}"
    )
    rec = snap_records[0]
    assert rec.levelno == logging.WARNING, (
        f"Expected WARNING when write_fail>0, got level "
        f"{logging.getLevelName(rec.levelno)}"
    )
    assert "write_ok=10" in rec.message
    assert "write_fail=3" in rec.message
    assert "build_fail=0" in rec.message


def test_health_log_emits_info_when_no_failures(caplog):
    import logging
    from bot.daemon import main as daemon_main

    v2._SNAPSHOT_WRITE_OK = 50
    v2._SNAPSHOT_WRITE_FAIL = 0
    v2._SNAPSHOT_BUILD_FAIL = 0

    cycle_runner = mock.MagicMock()
    cycle_runner.health.return_value = {
        "cycle_count": 0, "error_count": 0,
        "last_cycle_success": None, "last_cycle_duration_s": None,
    }
    scheduler = mock.MagicMock()
    scheduler.health.return_value = {"tasks": {}}

    with caplog.at_level(logging.INFO, logger="bot.daemon.main"):
        daemon_main._log_health([], cycle_runner, scheduler)

    snap_records = [r for r in caplog.records if "wx_snapshots" in r.message]
    assert len(snap_records) == 1
    assert snap_records[0].levelno == logging.INFO
