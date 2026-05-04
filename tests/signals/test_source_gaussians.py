"""Per-source tests for the v2 ``get_<source>_gaussian()`` entry points.

These are the contract tests for A1b. v1 probability functions continue to
use the logistic CDF (see each source module's docstring); the v2 Gaussian
sibling must:

  * return ``None`` for non-weather tickers
  * return ``None`` when the upstream fetch fails
  * respect per-source horizon gates (HRRR <= day 1, MADIS day 0 only)
  * on happy path, emit a ``GaussianForecast`` with finite mean, positive
    sigma, non-negative horizon, and the expected ``source_name``
  * have sigma that grows with ``day_idx`` where applicable (quantifies
    forecast skill decay with lead time)

Network is fully mocked — no real HTTP. The ``close_time`` in the
market payload is anchored to 11:00 PM EST (UTC-5) today so that
``_determine_day_index`` resolves to ``0`` (today) regardless of when
the test runs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.signals.sources import hrrr, ndfd_nbm, madis, metar_observations, nws_point, weather
from bot.signals.weather_forecast import GaussianForecast


# ══════════════════════════════════════════════════════════════════════
# Shared fixtures
# ══════════════════════════════════════════════════════════════════════

def _today_close_time_utc(offset_hours: int = -5) -> str:
    """Return an ISO-UTC close_time string that is "today" in LST.

    Anchored to 11:00 PM in the station's LST so that it is safely inside
    the settlement day regardless of the clock. Using UTC-today would
    flake at night UTC (LST lags by ``offset_hours`` — date can differ).
    """
    lst_tz = timezone(timedelta(hours=offset_hours))
    today_lst = datetime.now(lst_tz).date()
    dt_lst = datetime(
        today_lst.year, today_lst.month, today_lst.day,
        23, 0, 0, tzinfo=lst_tz,
    )
    return dt_lst.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _market(title: str = "Will NYC high exceed 75°F today?") -> dict:
    return {"title": title, "close_time": _today_close_time_utc()}


def _assert_gaussian_shape(
    g: GaussianForecast | None,
    *,
    expected_source: str,
    expected_mean: float,
    mean_tol: float = 0.01,
) -> None:
    assert g is not None, "expected a GaussianForecast, got None"
    assert isinstance(g, GaussianForecast)
    assert g.source_name == expected_source
    assert abs(g.mean_f - expected_mean) <= mean_tol, (
        f"mean mismatch: got {g.mean_f}, expected {expected_mean} ±{mean_tol}"
    )
    assert g.sigma_f > 0
    assert g.horizon_hours >= 0


# ══════════════════════════════════════════════════════════════════════
# HRRR
# ══════════════════════════════════════════════════════════════════════

class TestHRRRGaussian:
    def test_non_weather_ticker_returns_none(self):
        assert hrrr.get_hrrr_gaussian(
            "KXETH-26", {"title": "Eth price"},
        ) is None

    def test_missing_market_returns_none(self):
        assert hrrr.get_hrrr_gaussian("KXHIGHNY-26APR20-T75", None) is None

    def test_day_beyond_hrrr_horizon_returns_none(self, monkeypatch):
        """HRRR only covers day 0-1. Asking for day 2 → None (not a failed fetch)."""
        lst_tz = timezone(timedelta(hours=-5))
        three_days_out = (datetime.now(lst_tz) + timedelta(days=3)).date()
        close_utc = datetime(
            three_days_out.year, three_days_out.month, three_days_out.day,
            23, 0, 0, tzinfo=lst_tz,
        ).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        market = {"title": "Will NYC high exceed 75°F", "close_time": close_utc}
        # Even if the fetch succeeded, day_idx gate fires first.
        monkeypatch.setattr(hrrr, "_fetch_hrrr_forecast", lambda c: {"hourly": {}})
        assert hrrr.get_hrrr_gaussian("KXHIGHNY-26APR25-T75", market) is None

    def test_happy_path(self, monkeypatch):
        monkeypatch.setattr(hrrr, "_fetch_hrrr_forecast", lambda c: {"hourly": {"time": [], "temperature_2m": []}})
        monkeypatch.setattr(hrrr, "_daily_high_from_hourly_hrrr", lambda f, d: 82.0)
        g = hrrr.get_hrrr_gaussian("KXHIGHNY-26APR20-T75", _market())
        _assert_gaussian_shape(g, expected_source="hrrr", expected_mean=82.0)
        # sigma schedule: 2.0 + day_idx*0.5 → day 0 = 2.0.
        # 2026-05-04: bumped 1.2 → 2.0 after KXHIGHNY canary postmortem.
        # Measured RMSE was ~3.5°F post truth-fix. The old 1.2°F prior
        # (barely inflated to 1.31°F by staleness) gave HRRR ~27% weight
        # in the precision-weighted combine when honest sources had ~12%.
        # See bot/signals/sources/hrrr.py docstring for the rationale.
        assert abs(g.sigma_f - 2.0) < 1e-6

    def test_sigma_monotonic_in_day_idx(self):
        # Day-0 sigma should be strictly less than day-1 sigma
        assert hrrr._hrrr_sigma_for_day(0) < hrrr._hrrr_sigma_for_day(1)

    def test_per_city_sigma_priors_keep_well_calibrated_cities_at_old_value(self):
        """2026-05-04: per-city carve-out so the postmortem 1.2 → 2.0 σ
        bump doesn't break cities where HRRR was already well-calibrated.

        Backtest evidence:
          - KDEN: HRRR RMSE 2.47 at settlement (n=1426), HRRR's σ bump
            caused the global combine to regress -12% on KXHIGHDEN
            because weight shifted away from the most-accurate source.
          - KMIA: HRRR RMSE 1.19 — the best of any city.
          - KAUS: HRRR RMSE 1.27 — well-calibrated.
        These cities keep the original 1.2°F prior. NYC, Chicago, LAX
        get the 2.0°F bump (where measured HRRR RMSE was 1.7-6.1°F)."""
        # Cities that keep the old 1.2°F prior (well-calibrated)
        for city in ("denver", "miami", "austin"):
            assert hrrr._hrrr_sigma_for_day(0, city) == 1.2, (
                f"{city} HRRR σ should remain at 1.2°F (well-calibrated)"
            )

        # Cities that get the postmortem bump
        for city in ("nyc", "chicago", "los_angeles"):
            assert hrrr._hrrr_sigma_for_day(0, city) == 2.0, (
                f"{city} HRRR σ should be at the postmortem 2.0°F prior"
            )

        # Day-1 decay applies to both
        assert hrrr._hrrr_sigma_for_day(1, "denver") == 1.7
        assert hrrr._hrrr_sigma_for_day(1, "nyc") == 2.5

    def test_per_city_sigma_unknown_city_uses_default(self):
        """A city not in the map (e.g. typo'd 'sand_diego') uses the
        default 2.0°F prior — fail-safe."""
        assert hrrr._hrrr_sigma_for_day(0, "atlantis") == 2.0
        assert hrrr._hrrr_sigma_for_day(0, None) == 2.0
        assert hrrr._hrrr_sigma_for_day(0) == 2.0


# ══════════════════════════════════════════════════════════════════════
# NBM
# ══════════════════════════════════════════════════════════════════════

class TestNBMGaussian:
    def test_non_weather_ticker_returns_none(self):
        assert ndfd_nbm.get_nbm_gaussian("KXBTC-26", {"title": "BTC"}) is None

    def test_fetch_failure_returns_none(self, monkeypatch):
        monkeypatch.setattr(ndfd_nbm, "_fetch_nbm_forecast", lambda c: None)
        assert ndfd_nbm.get_nbm_gaussian(
            "KXHIGHNY-26APR20-T75", _market(),
        ) is None

    def test_happy_path(self, monkeypatch):
        fake = {
            "daily": {
                "temperature_2m_max": [79.0, 81.0, 83.0],
                "time": ["2026-04-20", "2026-04-21", "2026-04-22"],
            }
        }
        monkeypatch.setattr(ndfd_nbm, "_fetch_nbm_forecast", lambda c: fake)
        g = ndfd_nbm.get_nbm_gaussian("KXHIGHNY-26APR20-T75", _market())
        _assert_gaussian_shape(g, expected_source="nbm", expected_mean=79.0)
        # sigma schedule: 1.8 + day_idx*0.5
        assert abs(g.sigma_f - 1.8) < 1e-6

    def test_sigma_monotonic_in_day_idx(self):
        assert ndfd_nbm._nbm_sigma_for_day(0) < ndfd_nbm._nbm_sigma_for_day(2)


# ══════════════════════════════════════════════════════════════════════
# Open-Meteo
# ══════════════════════════════════════════════════════════════════════

class TestOpenMeteoGaussian:
    def test_non_weather_ticker_returns_none(self):
        assert weather.get_weather_gaussian(
            "KXBTC-26", {"title": "BTC"},
        ) is None

    def test_fetch_failure_returns_none(self, monkeypatch):
        monkeypatch.setattr(weather, "get_weather_forecast", lambda ck: None)
        assert weather.get_weather_gaussian(
            "KXHIGHNY-26APR20-T75", _market(),
        ) is None

    def test_happy_path(self, monkeypatch):
        fake = {
            "daily": {
                "temperature_2m_max": [77.0, 79.0, 81.0],
                "time": ["2026-04-20", "2026-04-21", "2026-04-22"],
            }
        }
        monkeypatch.setattr(weather, "get_weather_forecast", lambda ck: fake)
        g = weather.get_weather_gaussian(
            "KXHIGHNY-26APR20-T75", _market(),
        )
        _assert_gaussian_shape(g, expected_source="weather", expected_mean=77.0)
        assert abs(g.sigma_f - 2.0) < 1e-6

    def test_sigma_monotonic_in_day_idx(self):
        assert weather._open_meteo_sigma_for_day(0) < weather._open_meteo_sigma_for_day(3)


# ══════════════════════════════════════════════════════════════════════
# Tomorrow.io
# ══════════════════════════════════════════════════════════════════════

class TestTomorrowGaussian:
    def test_non_weather_ticker_returns_none(self):
        assert weather.get_tomorrow_gaussian(
            "KXETH-26", {"title": "ETH"},
        ) is None

    def test_fetch_failure_returns_none(self, monkeypatch):
        monkeypatch.setattr(weather, "get_tomorrow_forecast", lambda ck: None)
        assert weather.get_tomorrow_gaussian(
            "KXHIGHNY-26APR20-T75", _market(),
        ) is None

    def test_happy_path(self, monkeypatch):
        fake = {
            "daily": {
                "temperature_2m_max": [76.0, 78.0],
                "time": ["2026-04-20", "2026-04-21"],
            }
        }
        monkeypatch.setattr(weather, "get_tomorrow_forecast", lambda ck: fake)
        g = weather.get_tomorrow_gaussian(
            "KXHIGHNY-26APR20-T75", _market(),
        )
        _assert_gaussian_shape(g, expected_source="tomorrow", expected_mean=76.0)


# ══════════════════════════════════════════════════════════════════════
# NWS Point
# ══════════════════════════════════════════════════════════════════════

class TestNWSPointGaussian:
    def test_non_weather_ticker_returns_none(self):
        assert nws_point.get_nws_point_gaussian(
            "KXBTC-26", {"title": "BTC"},
        ) is None

    def test_grid_resolution_failure_returns_none(self, monkeypatch):
        monkeypatch.setattr(nws_point, "_resolve_grid_url", lambda lat, lon: None)
        assert nws_point.get_nws_point_gaussian(
            "KXHIGHNY-26APR20-T75", _market(),
        ) is None

    def test_happy_path(self, monkeypatch):
        monkeypatch.setattr(
            nws_point, "_resolve_grid_url",
            lambda lat, lon: "https://fake/forecast",
        )
        monkeypatch.setattr(
            nws_point, "_fetch_hourly_forecast",
            lambda url: [{"startTime": "2026-04-20T15:00:00Z", "temperature": 80, "temperatureUnit": "F"}],
        )
        monkeypatch.setattr(
            nws_point, "_daily_high_from_hourly",
            lambda periods, date, tz: 80.0,
        )
        g = nws_point.get_nws_point_gaussian(
            "KXHIGHNY-26APR20-T75", _market(),
        )
        _assert_gaussian_shape(g, expected_source="nws_point", expected_mean=80.0)


# ══════════════════════════════════════════════════════════════════════
# MADIS
# ══════════════════════════════════════════════════════════════════════

class TestMADISGaussian:
    def test_non_weather_ticker_returns_none(self):
        assert madis.get_madis_gaussian(
            "KXBTC-26", {"title": "BTC"},
        ) is None

    def test_next_day_returns_none(self, monkeypatch):
        """MADIS is obs-only — only fires for today (day_idx == 0)."""
        lst_tz = timezone(timedelta(hours=-5))
        tomorrow = (datetime.now(lst_tz) + timedelta(days=1)).date()
        close_utc = datetime(
            tomorrow.year, tomorrow.month, tomorrow.day,
            23, 0, 0, tzinfo=lst_tz,
        ).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        market = {"title": "Will NYC high exceed 75°F tomorrow?", "close_time": close_utc}
        assert madis.get_madis_gaussian("KXHIGHNY-26APR21-T75", market) is None

    def test_basket_too_small_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            madis, "_fetch_madis_basket",
            lambda ids: [{"temp": 20.0}],  # exactly 1 station — need ≥2
        )
        assert madis.get_madis_gaussian(
            "KXHIGHNY-26APR20-T75", _market(),
        ) is None

    def test_happy_path(self, monkeypatch):
        # Three stations around 22°C (~71.6°F); spread ~2°F.
        monkeypatch.setattr(
            madis, "_fetch_madis_basket",
            lambda ids: [{"temp": 21.0}, {"temp": 22.0}, {"temp": 23.0}],
        )
        g = madis.get_madis_gaussian(
            "KXHIGHNY-26APR20-T75", _market(),
        )
        assert g is not None
        assert g.source_name == "madis"
        # Median 22°C = 71.6°F; expected_high adds warming headroom
        # (>= median). Just bound sanity-check; exact value depends on
        # hour-of-day which is test-machine-dependent.
        median_f = 22.0 * 9.0 / 5.0 + 32.0
        assert g.mean_f >= median_f - 0.01
        assert g.sigma_f > 0


# ══════════════════════════════════════════════════════════════════════
# METAR
# ══════════════════════════════════════════════════════════════════════

class TestMETARGaussian:
    def test_unknown_station_returns_none(self):
        assert metar_observations.get_metar_gaussian(
            "KXBTC-26", {"title": "BTC"},
        ) is None

    def test_no_metar_data_returns_none(self, monkeypatch):
        monkeypatch.setattr(metar_observations, "_fetch_metar_data", lambda: None)
        assert metar_observations.get_metar_gaussian(
            "KXHIGHNY-26APR20-T75", _market(),
        ) is None

    def test_happy_path(self, monkeypatch):
        """METAR Gaussian uses obs + forecast + hours_left blending."""
        obs = {"temp": 22.0, "reportTime": "2026-04-20T18:00:00Z"}
        monkeypatch.setattr(
            metar_observations, "_fetch_metar_data", lambda: [obs],
        )
        monkeypatch.setattr(
            metar_observations, "_extract_station_obs",
            lambda data, station: obs,
        )
        monkeypatch.setattr(
            metar_observations, "_update_running_daily_high",
            lambda station, temp_f, obs_time: {"high_f": 74.0, "date": "2026-04-20"},
        )
        monkeypatch.setattr(
            metar_observations, "_get_forecast_high",
            lambda station, running_high: 78.0,
        )
        # Decouple from any persisted diurnal fit — this test pins the v1 path
        monkeypatch.setattr(
            metar_observations, "_get_diurnal_fit",
            lambda station, lst_hour: None,
        )
        g = metar_observations.get_metar_gaussian(
            "KXHIGHNY-26APR20-T75", _market(),
        )
        assert g is not None
        assert g.source_name == "metar"
        # Mean is the blended expected eventual high, which must be
        # between running_high (74) and forecast (78) inclusive.
        assert 74.0 <= g.mean_f <= 78.0 + 1e-6
        assert g.sigma_f > 0
        assert g.horizon_hours >= 0

    def test_uses_learned_diurnal_fit_when_present(self, monkeypatch):
        """A4: when a diurnal fit exists for (station, lst_hour), the
        Gaussian's μ and σ should come from α + β·T and RMSE, not from
        the naive blend or the hours_left σ schedule."""
        obs = {"temp": 10.0, "reportTime": "2026-04-20T15:00:00Z"}
        monkeypatch.setattr(
            metar_observations, "_fetch_metar_data", lambda: [obs],
        )
        monkeypatch.setattr(
            metar_observations, "_extract_station_obs",
            lambda data, station: obs,
        )
        # Running-high low enough that α + β·T will exceed it
        monkeypatch.setattr(
            metar_observations, "_update_running_daily_high",
            lambda s, t, o: {"high_f": 55.0, "date": "2026-04-20"},
        )
        monkeypatch.setattr(
            metar_observations, "_get_forecast_high",
            lambda s, r: 70.0,
        )
        # Freeze LST hour to 9 so the fit lookup resolves deterministically
        import datetime as _dt
        class _FrozenNow:
            @staticmethod
            def replace_hour(hour: int):
                pass
        monkeypatch.setattr(
            metar_observations, "_get_lst_now",
            lambda station: _dt.datetime(
                2026, 4, 20, 9, 0, tzinfo=_dt.timezone(_dt.timedelta(hours=-5))
            ),
        )
        # Stub the diurnal lookup: α=20, β=1.1, RMSE=3.0.
        # temp_f = 10°C = 50°F → predicted = 20 + 1.1*50 = 75°F.
        monkeypatch.setattr(
            metar_observations, "_get_diurnal_fit",
            lambda station, lst_hour: (20.0, 1.1, 3.0),
        )
        g = metar_observations.get_metar_gaussian(
            "KXHIGHNY-26APR20-T75", _market(),
        )
        assert g is not None
        # μ = max(α + β·T, running_high) = max(75.0, 55.0) = 75.0
        assert abs(g.mean_f - 75.0) < 1e-6
        assert abs(g.sigma_f - 3.0) < 1e-6

    def test_past_peak_clamp_pins_mu_to_running_high_in_late_day(self, monkeypatch):
        """Regression for the 2026-05-01 evening warm-bias finding:
        when LST ≥ 14 AND running_high − current ≥ 2°F, the day's peak
        has happened and METAR.μ must equal running_high (not the
        diurnal fit's prediction).

        Live evidence: KXHIGHLAX at LST 18 had running_high=69.08°F,
        current temp = ~63°F, but METAR.μ = 71.85°F because the
        diurnal fit predicted continued warming. That dragged combined
        ensemble +5°F above the actual peak.
        """
        # Current temp 18°C ≈ 64°F, running_high 70°F. Cooling state.
        obs = {"temp": 18.0, "reportTime": "2026-05-01T22:00:00Z"}
        monkeypatch.setattr(
            metar_observations, "_fetch_metar_data", lambda: [obs],
        )
        monkeypatch.setattr(
            metar_observations, "_extract_station_obs",
            lambda data, station: obs,
        )
        monkeypatch.setattr(
            metar_observations, "_update_running_daily_high",
            lambda s, t, o: {"high_f": 70.0, "date": "2026-05-01"},
        )
        monkeypatch.setattr(
            metar_observations, "_get_forecast_high",
            lambda s, r: 78.0,  # forecast was warm — should NOT win
        )
        # LST hour 18 (past 2pm gate)
        import datetime as _dt
        monkeypatch.setattr(
            metar_observations, "_get_lst_now",
            lambda station: _dt.datetime(
                2026, 5, 1, 18, 0,
                tzinfo=_dt.timezone(_dt.timedelta(hours=-5)),
            ),
        )
        # A diurnal fit exists that would predict 75°F — but past-peak
        # clamp must short-circuit and pin μ to running_high.
        monkeypatch.setattr(
            metar_observations, "_get_diurnal_fit",
            lambda station, lst_hour: (20.0, 1.1, 3.0),
        )
        g = metar_observations.get_metar_gaussian(
            "KXHIGHNY-26MAY01-T70", _market(),
        )
        assert g is not None
        assert g.mean_f == 70.0, f"expected μ pinned to running_high, got {g.mean_f}"
        # σ should be tight (we know the day's high) — much tighter than
        # the diurnal fit's RMSE=3.0 or the schedule σ at hours_left=2.
        assert g.sigma_f < 1.0, (
            f"past-peak σ should be tight; got {g.sigma_f:.2f}°F"
        )
        assert "past_peak" in g.source_tag


    def test_past_peak_clamp_does_not_fire_in_morning(self, monkeypatch):
        """Morning false-positive guard: even with running_high above
        current temp by 2°F+, pre-LST-14 we should NOT clamp — the
        pattern could be a brief cold front gust or cloud passage,
        not the day's peak.
        """
        obs = {"temp": 18.0, "reportTime": "2026-05-01T13:00:00Z"}  # 8 AM LST
        monkeypatch.setattr(
            metar_observations, "_fetch_metar_data", lambda: [obs],
        )
        monkeypatch.setattr(
            metar_observations, "_extract_station_obs",
            lambda data, station: obs,
        )
        monkeypatch.setattr(
            metar_observations, "_update_running_daily_high",
            lambda s, t, o: {"high_f": 70.0, "date": "2026-05-01"},
        )
        monkeypatch.setattr(
            metar_observations, "_get_forecast_high",
            lambda s, r: 78.0,
        )
        import datetime as _dt
        monkeypatch.setattr(
            metar_observations, "_get_lst_now",
            lambda station: _dt.datetime(
                2026, 5, 1, 8, 0,  # LST 8 AM, well before peak
                tzinfo=_dt.timezone(_dt.timedelta(hours=-5)),
            ),
        )
        # Diurnal fit present: should be used (NOT clamped).
        monkeypatch.setattr(
            metar_observations, "_get_diurnal_fit",
            lambda station, lst_hour: (20.0, 1.1, 3.0),
        )
        g = metar_observations.get_metar_gaussian(
            "KXHIGHNY-26MAY01-T70", _market(),
        )
        assert g is not None
        # μ should follow diurnal fit: 20 + 1.1 × 64.4 = 90.84, but
        # capped at max(predicted, running_high) = max(90.84, 70) = 90.84
        assert g.mean_f > 80.0, (
            f"morning state should follow diurnal fit, not clamp; got μ={g.mean_f}"
        )


    def test_diurnal_fit_respects_running_high_floor(self, monkeypatch):
        """Daily high can only rise — when α + β·T < running_high, μ
        clamps to running_high (predicted morning implies low but the
        afternoon already beat it)."""
        obs = {"temp": 25.0, "reportTime": "2026-04-20T20:00:00Z"}
        monkeypatch.setattr(
            metar_observations, "_fetch_metar_data", lambda: [obs],
        )
        monkeypatch.setattr(
            metar_observations, "_extract_station_obs",
            lambda data, station: obs,
        )
        monkeypatch.setattr(
            metar_observations, "_update_running_daily_high",
            lambda s, t, o: {"high_f": 88.0, "date": "2026-04-20"},
        )
        monkeypatch.setattr(
            metar_observations, "_get_forecast_high",
            lambda s, r: 85.0,
        )
        import datetime as _dt
        monkeypatch.setattr(
            metar_observations, "_get_lst_now",
            lambda station: _dt.datetime(
                2026, 4, 20, 14, 0, tzinfo=_dt.timezone(_dt.timedelta(hours=-5))
            ),
        )
        # α+β·T = 0 + 0.5*77 = 38.5°F — nowhere near 88°F running high
        monkeypatch.setattr(
            metar_observations, "_get_diurnal_fit",
            lambda station, lst_hour: (0.0, 0.5, 2.5),
        )
        g = metar_observations.get_metar_gaussian(
            "KXHIGHNY-26APR20-T75", _market(),
        )
        assert g is not None
        # μ clamped to running_high = 88.0
        assert abs(g.mean_f - 88.0) < 1e-6

    def test_falls_back_to_naive_blend_without_fit(self, monkeypatch):
        """Cold-cache path — no diurnal fit persisted → Gaussian uses v1
        naive blend + hours_left σ schedule."""
        obs = {"temp": 22.0, "reportTime": "2026-04-20T18:00:00Z"}
        monkeypatch.setattr(
            metar_observations, "_fetch_metar_data", lambda: [obs],
        )
        monkeypatch.setattr(
            metar_observations, "_extract_station_obs",
            lambda data, station: obs,
        )
        monkeypatch.setattr(
            metar_observations, "_update_running_daily_high",
            lambda s, t, o: {"high_f": 74.0, "date": "2026-04-20"},
        )
        monkeypatch.setattr(
            metar_observations, "_get_forecast_high",
            lambda s, r: 78.0,
        )
        # Explicit None — no fit available
        monkeypatch.setattr(
            metar_observations, "_get_diurnal_fit",
            lambda station, lst_hour: None,
        )
        g = metar_observations.get_metar_gaussian(
            "KXHIGHNY-26APR20-T75", _market(),
        )
        assert g is not None
        # Back to the v1 blended bound: between running_high and forecast
        assert 74.0 <= g.mean_f <= 78.0 + 1e-6

    def test_get_diurnal_fit_rejects_out_of_band_sigma(self, memdb=None):
        """Read-side σ clamp: persisted σ outside [floor, ceil] → None so
        the signal path falls back to the prior instead of using garbage."""
        from bot.db import init_db, kv_set
        conn = init_db(":memory:")
        try:
            # σ below floor
            kv_set(conn, "weather_metar_diurnal_KXXX", {
                "hours": {"9": {"alpha": 10.0, "beta": 1.0, "rmse": 0.01, "n": 20}},
            }, ttl_seconds=86400)
            assert metar_observations._get_diurnal_fit("KXXX", 9) is None
            # σ above ceil
            kv_set(conn, "weather_metar_diurnal_KXXX", {
                "hours": {"9": {"alpha": 10.0, "beta": 1.0, "rmse": 50.0, "n": 20}},
            }, ttl_seconds=86400)
            assert metar_observations._get_diurnal_fit("KXXX", 9) is None
            # In-band — accepted
            kv_set(conn, "weather_metar_diurnal_KXXX", {
                "hours": {"9": {"alpha": 10.0, "beta": 1.0, "rmse": 2.0, "n": 20}},
            }, ttl_seconds=86400)
            fit = metar_observations._get_diurnal_fit("KXXX", 9)
            assert fit is not None
            assert fit == (10.0, 1.0, 2.0)
        finally:
            conn.close()

    def test_get_diurnal_fit_rejects_malformed_payload(self):
        from bot.db import init_db, kv_set
        conn = init_db(":memory:")
        try:
            # Not a dict
            kv_set(conn, "weather_metar_diurnal_KXXX",
                   "not a dict", ttl_seconds=86400)
            assert metar_observations._get_diurnal_fit("KXXX", 9) is None
            # Missing "hours" key
            kv_set(conn, "weather_metar_diurnal_KXXX",
                   {"fit_at": "now"}, ttl_seconds=86400)
            assert metar_observations._get_diurnal_fit("KXXX", 9) is None
            # Hour not in map
            kv_set(conn, "weather_metar_diurnal_KXXX", {
                "hours": {"14": {"alpha": 1.0, "beta": 1.0, "rmse": 2.0}},
            }, ttl_seconds=86400)
            assert metar_observations._get_diurnal_fit("KXXX", 9) is None
            # Missing required key in cell
            kv_set(conn, "weather_metar_diurnal_KXXX", {
                "hours": {"9": {"alpha": 1.0}},  # no beta/rmse
            }, ttl_seconds=86400)
            assert metar_observations._get_diurnal_fit("KXXX", 9) is None
        finally:
            conn.close()

    def test_does_not_short_circuit_when_running_high_over_threshold(self, monkeypatch):
        """V1 `get_metar_observation_estimate` returns 0.95/0.96/0.98 when
        running_high >= threshold. The v2 Gaussian path must NOT do that —
        it emits the blended distribution and leaves projection to the
        ensemble. This prevents extreme-FV over-confidence on hot days.
        """
        obs = {"temp": 28.0, "reportTime": "2026-04-20T18:00:00Z"}
        monkeypatch.setattr(
            metar_observations, "_fetch_metar_data", lambda: [obs],
        )
        monkeypatch.setattr(
            metar_observations, "_extract_station_obs",
            lambda data, station: obs,
        )
        # Running high 85°F already clear of the 75°F threshold
        monkeypatch.setattr(
            metar_observations, "_update_running_daily_high",
            lambda s, t, o: {"high_f": 85.0, "date": "2026-04-20"},
        )
        monkeypatch.setattr(
            metar_observations, "_get_forecast_high",
            lambda s, r: 86.0,
        )
        # Pin to v1 path — this test's invariants are about the naive blend
        monkeypatch.setattr(
            metar_observations, "_get_diurnal_fit",
            lambda s, h: None,
        )
        g = metar_observations.get_metar_gaussian(
            "KXHIGHNY-26APR20-T75", _market(),
        )
        assert g is not None
        # mean is a real blended temperature, not a probability scalar
        assert g.mean_f >= 85.0
        # sigma is never degenerate (would make prob exactly 1.0)
        assert g.sigma_f >= 0.1


# ══════════════════════════════════════════════════════════════════════
# Cross-source invariants
# ══════════════════════════════════════════════════════════════════════

class TestCrossSourceInvariants:
    """Pin invariants shared across every Gaussian source."""

    def test_all_sources_stamp_horizon_hours(self, monkeypatch):
        """Every source must populate ``horizon_hours`` so A3 can stratify
        skill curves by lead time. Zero horizons are suspect — they usually
        mean the source forgot to call ``hours_until_settlement_end``.
        """
        # Use HRRR as the representative; contract applies to all.
        monkeypatch.setattr(hrrr, "_fetch_hrrr_forecast", lambda c: {"hourly": {}})
        monkeypatch.setattr(hrrr, "_daily_high_from_hourly_hrrr", lambda f, d: 80.0)
        g = hrrr.get_hrrr_gaussian("KXHIGHNY-26APR20-T75", _market())
        # End-of-day LST is in the future relative to now, so horizon > 0
        assert g is not None
        assert g.horizon_hours > 0

    def test_all_sources_emit_distinct_source_names(self, monkeypatch):
        """Each source's output ``source_name`` must be unique — it's the
        key for ``weather_source_weights`` lookups and skill-curve
        binning. Accidentally reusing a name would silently collapse two
        sources into one weight bucket.
        """
        monkeypatch.setattr(hrrr, "_fetch_hrrr_forecast", lambda c: {"hourly": {}})
        monkeypatch.setattr(hrrr, "_daily_high_from_hourly_hrrr", lambda f, d: 80.0)
        monkeypatch.setattr(
            ndfd_nbm, "_fetch_nbm_forecast",
            lambda c: {"daily": {"temperature_2m_max": [80.0], "time": ["2026-04-20"]}},
        )
        monkeypatch.setattr(
            weather, "get_weather_forecast",
            lambda ck: {"daily": {"temperature_2m_max": [80.0], "time": ["2026-04-20"]}},
        )
        monkeypatch.setattr(
            weather, "get_tomorrow_forecast",
            lambda ck: {"daily": {"temperature_2m_max": [80.0], "time": ["2026-04-20"]}},
        )
        market = _market()
        names = set()
        for g in (
            hrrr.get_hrrr_gaussian("KXHIGHNY-26APR20-T75", market),
            ndfd_nbm.get_nbm_gaussian("KXHIGHNY-26APR20-T75", market),
            weather.get_weather_gaussian("KXHIGHNY-26APR20-T75", market),
            weather.get_tomorrow_gaussian("KXHIGHNY-26APR20-T75", market),
        ):
            assert g is not None
            names.add(g.source_name)
        assert names == {"hrrr", "nbm", "weather", "tomorrow"}
