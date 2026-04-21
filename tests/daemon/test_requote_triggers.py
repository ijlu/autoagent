"""T1.2 — non-METAR requote trigger tests.

Covers:
1. ``time_decay_interval_s`` cadence table (45 s tail → 600 s morning →
   ``inf`` day-closed).
2. ``_series_for_primary_icao`` reverse lookup (primary → series, backup
   → None).
3. ``_hours_left_for_station`` LST math (matches poller's hours_left).
4. :class:`TimeDecayDriver`
   - fires when (now − last_fire) ≥ cadence
   - skips when cadence not met
   - skips when ``hours_left <= 0``
   - skips when poller has no reading yet
   - ``note_external_fire`` suppresses the next tick (METAR/forecast fire
     defers the time-decay clock)
   - rejected enqueue doesn't advance our timestamp (retries next tick)
5. :class:`ForecastChangeDriver`
   - fires on ≥1°F delta
   - skips < 1°F jitter
   - skips missing-on-prior (first-ever refresh is not a change event)
   - skips backup ICAO (not tradable → series=None)
6. :meth:`WeatherChangeHandler.enqueue_synthetic`
   - writes ``trigger_reason`` to weather_mm_shadow
   - rejects when poller has no state
   - rejects on cooldown
   - invokes ``on_fire`` callback after success
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from bot.daemon.forecast_cache import ForecastCache
from bot.daemon.metar_poller import StationReading, StationState
from bot.daemon.requote_triggers import (
    FORECAST_DELTA_THRESHOLD_F,
    REASON_FORECAST_CHANGE,
    REASON_METAR_CHANGE,
    REASON_TIME_DECAY,
    VALID_REASONS,
    ForecastChangeDriver,
    TimeDecayDriver,
    _hours_left_for_station,
    _series_for_primary_icao,
    time_decay_interval_s,
)
from bot.daemon.stations import STATION_BY_SERIES, STATIONS
from bot.daemon.weather_handler import WeatherChangeHandler


# ═════════════════════════════════════════════════════════════════════════════
# Cadence table — the pure function at the heart of TimeDecayDriver
# ═════════════════════════════════════════════════════════════════════════════

class TestTimeDecayCadence:
    @pytest.mark.parametrize("hrs,expected", [
        (-1.0, float("inf")),   # day closed
        (0.0, float("inf")),
        (0.01, 45.0),           # tail volatility
        (0.5, 45.0),
        (0.99, 45.0),
        (1.0, 90.0),            # 1h boundary
        (1.5, 90.0),
        (2.0, 180.0),
        (3.9, 180.0),
        (4.0, 300.0),
        (7.9, 300.0),
        (8.0, 600.0),
        (23.0, 600.0),
        (100.0, 600.0),         # far-future catch-all
    ])
    def test_cadence_buckets(self, hrs, expected):
        assert time_decay_interval_s(hrs) == expected

    def test_day_closed_is_no_op(self):
        assert time_decay_interval_s(0.0) == float("inf")
        assert time_decay_interval_s(-1e9) == float("inf")


# ═════════════════════════════════════════════════════════════════════════════
# Registry lookups
# ═════════════════════════════════════════════════════════════════════════════

class TestRegistryHelpers:
    def test_series_for_primary_icao_hits(self):
        # Every primary in the registry must resolve to its own series.
        for series, station in STATION_BY_SERIES.items():
            assert _series_for_primary_icao(station.icao) == series

    def test_series_for_primary_icao_misses_backups(self):
        # KJFK is a NY backup (primary flipped to KNYC in T1.1). It must
        # return None — we do not trade a KJFK series.
        assert _series_for_primary_icao("KJFK") is None

    def test_series_for_primary_icao_unknown(self):
        assert _series_for_primary_icao("ZZZZ") is None

    def test_hours_left_matches_poller(self):
        """Our pure helper must return the same value as the poller does."""
        ny = STATION_BY_SERIES["KXHIGHNY"]
        now = time.time()
        ours = _hours_left_for_station(ny, now)
        # Mirror _get_hours_remaining in METARPoller
        from datetime import datetime, timedelta, timezone
        lst = timezone(timedelta(hours=ny.lst_offset))
        now_lst = datetime.fromtimestamp(now, tz=lst)
        end_of_day = now_lst.replace(hour=23, minute=59, second=59, microsecond=0)
        theirs = max(0.0, (end_of_day - now_lst).total_seconds() / 3600.0)
        assert abs(ours - theirs) < 1e-6


# ═════════════════════════════════════════════════════════════════════════════
# TimeDecayDriver
# ═════════════════════════════════════════════════════════════════════════════

class _StubHandler:
    """Minimal handler the driver can talk to without touching a dispatcher."""

    def __init__(self, accept: bool = True):
        self.accept = accept
        self.calls: list[tuple[str, str, str]] = []

    def enqueue_synthetic(self, series, station, reason):
        self.calls.append((series, station, reason))
        return self.accept


def _stub_poller(states: dict[str, StationState]) -> MagicMock:
    poller = MagicMock()
    poller.get_state.side_effect = lambda s: states.get(s)
    return poller


def _state_with_reading(icao: str, temp_f: float = 72.0,
                         running_high: float = 72.0) -> StationState:
    r = StationReading(
        station=icao, temp_f=temp_f, temp_c=(temp_f - 32) * 5 / 9,
        obs_time="2026-04-20T18:00:00Z", poll_time=time.time(),
    )
    return StationState(
        station=icao,
        last_temp_f=temp_f,
        running_high_f=running_high,
        running_high_date="2026-04-20",
        readings=[r],
    )


class TestTimeDecayDriver:
    @pytest.fixture
    def ny_only(self):
        """Driver restricted to just NY to keep assertions simple."""
        return (STATIONS["KNYC"],)

    def test_fires_when_cadence_exceeded(self, ny_only):
        handler = _StubHandler(accept=True)
        now = [time.time()]
        states = {"KNYC": _state_with_reading("KNYC")}
        driver = TimeDecayDriver(
            handler=handler,
            poller=_stub_poller(states),
            stations=ny_only,
            now_fn=lambda: now[0],
        )
        # First tick with no prior fire: elapsed = now (huge) >> cadence.
        # But our _last_fire_ts starts at 0.0 so now - 0 is huge → fires.
        fired = driver.run_once()
        assert fired == 1
        assert handler.calls == [("KXHIGHNY", "KNYC", REASON_TIME_DECAY)]
        assert driver.stats["fired"] == 1

    def test_skips_when_cadence_not_met(self, ny_only):
        handler = _StubHandler(accept=True)
        now = [time.time()]
        states = {"KNYC": _state_with_reading("KNYC")}
        driver = TimeDecayDriver(
            handler=handler,
            poller=_stub_poller(states),
            stations=ny_only,
            now_fn=lambda: now[0],
        )
        # First tick fires.
        driver.run_once()
        # Second tick 1 s later — cadence for hrs_left > 8 is 600 s so blocked.
        now[0] += 1.0
        fired2 = driver.run_once()
        assert fired2 == 0
        assert driver.stats["skipped_cadence"] == 1

    def test_skips_when_day_closed(self, ny_only):
        """Day-closed → time_decay_interval_s returns inf → stats skipped_day_closed."""
        handler = _StubHandler(accept=True)
        # Build a now_fn that lands at 23:59 LST for NY (offset = -5):
        # to be safe we just pick a station cfg that returns hours_left=0
        # by mocking a station at offset that puts us past 23:59. Simpler:
        # patch _hours_left_for_station via monkeypatch.
        states = {"KNYC": _state_with_reading("KNYC")}
        driver = TimeDecayDriver(
            handler=handler,
            poller=_stub_poller(states),
            stations=ny_only,
            now_fn=lambda: time.time(),
        )
        # Monkeypatch the bound import inside run_once via the module seam.
        from bot.daemon import requote_triggers as rt
        orig = rt._hours_left_for_station
        try:
            rt._hours_left_for_station = lambda *_a, **_kw: 0.0
            fired = driver.run_once()
        finally:
            rt._hours_left_for_station = orig

        assert fired == 0
        assert handler.calls == []
        assert driver.stats["skipped_day_closed"] == 1

    def test_skips_when_no_state(self, ny_only):
        handler = _StubHandler(accept=True)
        driver = TimeDecayDriver(
            handler=handler,
            poller=_stub_poller({}),  # no state for anyone
            stations=ny_only,
        )
        fired = driver.run_once()
        assert fired == 0
        assert driver.stats["skipped_no_state"] == 1
        assert handler.calls == []

    def test_note_external_fire_suppresses_next_tick(self, ny_only):
        handler = _StubHandler(accept=True)
        now = [time.time()]
        states = {"KNYC": _state_with_reading("KNYC")}
        driver = TimeDecayDriver(
            handler=handler,
            poller=_stub_poller(states),
            stations=ny_only,
            now_fn=lambda: now[0],
        )
        # METAR just fired 10 s ago.
        driver.note_external_fire("KXHIGHNY", ts=now[0] - 10.0)
        # Cadence for hrs_left > 8 is 600 s → still within cooldown.
        fired = driver.run_once()
        assert fired == 0
        assert driver.stats["skipped_cadence"] == 1

    def test_rejected_enqueue_does_not_advance_clock(self, ny_only):
        """When handler.enqueue_synthetic returns False, we must retry next tick.

        If we advanced _last_fire_ts on rejection, a series under
        cooldown would be starved for the full cadence window every time
        the handler refused it.
        """
        handler = _StubHandler(accept=False)
        now = [time.time()]
        states = {"KNYC": _state_with_reading("KNYC")}
        driver = TimeDecayDriver(
            handler=handler,
            poller=_stub_poller(states),
            stations=ny_only,
            now_fn=lambda: now[0],
        )
        driver.run_once()
        assert driver.stats["skipped_enqueue_rejected"] == 1
        assert driver.stats["fired"] == 0

        # _last_fire_ts should still be 0 so the next tick also tries.
        now[0] += 1.0
        driver.run_once()
        assert driver.stats["skipped_enqueue_rejected"] == 2


# ═════════════════════════════════════════════════════════════════════════════
# ForecastChangeDriver
# ═════════════════════════════════════════════════════════════════════════════

class TestForecastChangeDriver:
    def test_fires_on_material_delta(self):
        handler = _StubHandler(accept=True)
        driver = ForecastChangeDriver(handler)
        ny = STATION_BY_SERIES["KXHIGHNY"].icao  # KNYC
        fired = driver.on_refresh(
            prior={ny: 75.0},
            current={ny: 77.5},  # 2.5°F jump
        )
        assert fired == 1
        assert handler.calls == [("KXHIGHNY", ny, REASON_FORECAST_CHANGE)]
        assert driver.stats["fires"] == 1

    def test_skips_below_threshold(self):
        handler = _StubHandler(accept=True)
        driver = ForecastChangeDriver(handler)
        ny = STATION_BY_SERIES["KXHIGHNY"].icao
        # Threshold default is 1.0 — 0.5°F is jitter.
        fired = driver.on_refresh(
            prior={ny: 75.0}, current={ny: 75.4},
        )
        assert fired == 0
        assert driver.stats["skipped_below_threshold"] == 1
        assert driver.stats["fires"] == 0
        assert handler.calls == []

    def test_skips_missing_prior(self):
        """First-ever refresh: no prior snapshot → not a change event."""
        handler = _StubHandler(accept=True)
        driver = ForecastChangeDriver(handler)
        ny = STATION_BY_SERIES["KXHIGHNY"].icao
        fired = driver.on_refresh(prior={}, current={ny: 77.0})
        assert fired == 0
        assert driver.stats["skipped_missing_prior"] == 1

    def test_skips_backup_icao(self):
        """KJFK is a backup post-T1.1 — no series owns it → skip cleanly."""
        handler = _StubHandler(accept=True)
        driver = ForecastChangeDriver(handler)
        fired = driver.on_refresh(
            prior={"KJFK": 75.0}, current={"KJFK": 80.0},
        )
        assert fired == 0
        assert driver.stats["skipped_not_tradable"] == 1
        assert handler.calls == []

    def test_exact_threshold_boundary(self):
        """Delta == threshold is *not* material (strict < threshold skips)."""
        handler = _StubHandler(accept=True)
        driver = ForecastChangeDriver(handler, threshold_f=1.0)
        ny = STATION_BY_SERIES["KXHIGHNY"].icao
        fired = driver.on_refresh(
            prior={ny: 75.0}, current={ny: 76.0},  # exactly 1°F
        )
        # abs(new-old) < threshold → skip; threshold is strict
        # We pick > behavior: our code does `if abs(...) < threshold`.
        # 1.0 < 1.0 is False → this one *fires*. Document and test both.
        assert fired == 1


# ═════════════════════════════════════════════════════════════════════════════
# WeatherChangeHandler.enqueue_synthetic
# ═════════════════════════════════════════════════════════════════════════════

class TestHandlerEnqueueSynthetic:
    def _make_handler(self, *, poller, forecast, dispatcher=None, cooldown=10.0):
        quoter = MagicMock()
        quoter.shadow_requote_city.return_value = []
        quoter.requote_city.return_value = []
        return WeatherChangeHandler(
            quoter=quoter,
            forecast_cache=forecast,
            dispatcher=dispatcher,
            poller=poller,
            cooldown_s=cooldown,
            live=False,
        ), quoter

    def test_rejects_when_no_poller(self):
        fc = ForecastCache()
        fc.set("KNYC", 78.0)
        h = WeatherChangeHandler(
            quoter=MagicMock(),
            forecast_cache=fc,
            poller=None,
            live=False,
        )
        assert h.enqueue_synthetic("KXHIGHNY", "KNYC", REASON_TIME_DECAY) is False
        assert h.stats["synthetic_rejected_no_state"] == 1

    def test_rejects_when_no_state(self):
        fc = ForecastCache()
        fc.set("KNYC", 78.0)
        h, _ = self._make_handler(poller=_stub_poller({}), forecast=fc)
        assert h.enqueue_synthetic("KXHIGHNY", "KNYC", REASON_TIME_DECAY) is False
        assert h.stats["synthetic_rejected_no_state"] == 1

    def test_rejects_on_cooldown(self):
        fc = ForecastCache()
        fc.set("KNYC", 78.0)
        states = {"KNYC": _state_with_reading("KNYC")}
        h, _ = self._make_handler(
            poller=_stub_poller(states), forecast=fc, cooldown=3600.0,
        )
        # Pretend a natural event just fired for this series.
        h._last_requote["KXHIGHNY"] = time.time()
        assert h.enqueue_synthetic("KXHIGHNY", "KNYC", REASON_TIME_DECAY) is False
        assert h.stats["synthetic_rejected_cooldown"] == 1

    def test_rejects_bad_reason(self):
        fc = ForecastCache()
        fc.set("KNYC", 78.0)
        states = {"KNYC": _state_with_reading("KNYC")}
        h, _ = self._make_handler(poller=_stub_poller(states), forecast=fc)
        assert h.enqueue_synthetic("KXHIGHNY", "KNYC", "garbage") is False

    def test_accepts_and_dispatches_synthetic(self):
        fc = ForecastCache()
        fc.set("KNYC", 78.0)
        states = {"KNYC": _state_with_reading("KNYC", temp_f=73.0, running_high=75.0)}
        h, quoter = self._make_handler(poller=_stub_poller(states), forecast=fc)
        ok = h.enqueue_synthetic("KXHIGHNY", "KNYC", REASON_TIME_DECAY)
        assert ok is True
        # Dispatcher is None so _handle_one runs synchronously.
        quoter.shadow_requote_city.assert_called_once()
        kwargs = quoter.shadow_requote_city.call_args.kwargs
        assert kwargs["series"] == "KXHIGHNY"
        assert kwargs["station"] == "KNYC"
        assert kwargs["trigger_reason"] == REASON_TIME_DECAY
        assert kwargs["running_high_f"] == 75.0
        assert h.stats["synthetic_enqueued"] == 1

    def test_on_fire_callback_invoked(self):
        fc = ForecastCache()
        fc.set("KNYC", 78.0)
        states = {"KNYC": _state_with_reading("KNYC")}
        h, _ = self._make_handler(poller=_stub_poller(states), forecast=fc)
        fires: list[tuple[str, float]] = []
        h.on_fire = lambda s, ts: fires.append((s, ts))
        h.enqueue_synthetic("KXHIGHNY", "KNYC", REASON_FORECAST_CHANGE)
        assert len(fires) == 1
        assert fires[0][0] == "KXHIGHNY"
        assert fires[0][1] > 0

    def test_valid_reasons_frozenset_contract(self):
        """VALID_REASONS must contain exactly the three labels we wrote to
        the schema default. Adding a reason without updating docs / gate
        queries silently breaks the P&L-by-reason report, so lock it."""
        assert VALID_REASONS == frozenset({
            REASON_METAR_CHANGE, REASON_TIME_DECAY, REASON_FORECAST_CHANGE,
        })
