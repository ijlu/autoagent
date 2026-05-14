from __future__ import annotations

import logging
from unittest import mock

from bot.daemon import main as daemon_main


def _cycle_runner():
    cycle_runner = mock.MagicMock()
    cycle_runner.health.return_value = {
        "cycle_count": 0,
        "error_count": 0,
        "last_cycle_success": None,
        "last_cycle_duration_s": None,
    }
    return cycle_runner


def _scheduler():
    scheduler = mock.MagicMock()
    scheduler.health.return_value = {"tasks": {}}
    return scheduler


def _weather_handler(*, forecast_skips: int = 0, v2_fail_closed: int = 0):
    handler = mock.MagicMock()
    handler.live = True
    handler.stats = {
        "changes_seen": 0,
        "changes_throttled": 0,
        "requotes_dispatched": 0,
        "markets_shadowed": 0,
        "markets_quoted": 0,
        "markets_skipped": 0,
        "synthetic_enqueued": 0,
        "synthetic_rejected_no_state": 0,
        "synthetic_rejected_cooldown": 0,
        "live_forecast_missing_skips": forecast_skips,
        "errors": 0,
    }
    handler.quoter = mock.MagicMock()
    handler.quoter._v2_fail_closed_count = v2_fail_closed
    return handler


def test_health_log_includes_live_forecast_missing_skips(caplog):
    handler = _weather_handler(forecast_skips=2)

    with caplog.at_level(logging.INFO, logger="bot.daemon.main"):
        daemon_main._log_health([], _cycle_runner(), _scheduler(), handler)

    records = [r for r in caplog.records if "[health] wx_handler" in r.message]
    assert len(records) == 1
    assert "live_fc_missing_skips=2" in records[0].message


def test_health_log_warns_and_resets_v2_fail_closed_count(caplog):
    handler = _weather_handler(v2_fail_closed=3)

    with caplog.at_level(logging.INFO, logger="bot.daemon.main"):
        daemon_main._log_health([], _cycle_runner(), _scheduler(), handler)
        daemon_main._log_health([], _cycle_runner(), _scheduler(), handler)

    records = [
        r for r in caplog.records if "[health] wx_live_fail_closed" in r.message
    ]
    assert len(records) == 2
    assert records[0].levelno == logging.WARNING
    assert "v2_fv_unavailable=3" in records[0].message
    assert records[1].levelno == logging.INFO
    assert "v2_fv_unavailable=0" in records[1].message
    assert handler.quoter._v2_fail_closed_count == 0
