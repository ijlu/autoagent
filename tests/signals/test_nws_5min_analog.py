"""Tests for `bot.signals.sources.nws_5min_analog`.

Three layers:
  1. Pure-math helpers (curve fit, 3x3 solver, precision combine)
  2. Curve-fit happy path with synthetic peaking data
  3. Analog matcher with seeded historical data
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from bot.api import _CACHE
from bot.db import init_db
from bot.signals.sources import nws_5min_analog as src


@pytest.fixture(autouse=True)
def _clear_cache():
    _CACHE.clear()
    yield
    _CACHE.clear()


# ── 1. Pure math ──────────────────────────────────────────────────────


def test_solve_3x3_identity():
    M = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    sol = src._solve_3x3(M, [4.0, 5.0, 6.0])
    assert sol == [4.0, 5.0, 6.0]


def test_solve_3x3_singular_returns_none():
    M = [[1.0, 2.0, 3.0], [2.0, 4.0, 6.0], [3.0, 6.0, 9.0]]
    assert src._solve_3x3(M, [1.0, 2.0, 3.0]) is None


def test_solve_3x3_recovers_quadratic_coeffs():
    """Build y = 1 + 2x + 3x² evaluated at x=0,1,2,3 and confirm
    the solver recovers (a=1, b=2, c=3)."""
    xs = [0.0, 1.0, 2.0, 3.0]
    ys = [1 + 2 * x + 3 * x * x for x in xs]
    n = len(xs)
    sx = sum(xs)
    sx2 = sum(x * x for x in xs)
    sx3 = sum(x ** 3 for x in xs)
    sx4 = sum(x ** 4 for x in xs)
    sy = sum(ys)
    sxy = sum(x * y for x, y in zip(xs, ys))
    sx2y = sum(x * x * y for x, y in zip(xs, ys))
    M = [[n, sx, sx2], [sx, sx2, sx3], [sx2, sx3, sx4]]
    v = [sy, sxy, sx2y]
    a, b, c = src._solve_3x3(M, v)
    assert a == pytest.approx(1.0, abs=1e-9)
    assert b == pytest.approx(2.0, abs=1e-9)
    assert c == pytest.approx(3.0, abs=1e-9)


def test_precision_combine_basic():
    out = src._precision_combine((75.0, 2.0), (77.0, 1.0))
    assert out is not None
    mu, sigma = out
    # tighter source (σ=1) gets 4× the weight of σ=2 source.
    # μ = (1/4 * 75 + 1 * 77) / (1/4 + 1) = (18.75 + 77) / 1.25 = 76.6
    assert mu == pytest.approx(76.6, abs=0.05)
    # combined σ = 1/√(1/4 + 1) = 0.894
    assert sigma == pytest.approx(0.894, abs=0.01)


def test_precision_combine_handles_none():
    assert src._precision_combine(None, None) is None
    assert src._precision_combine((75.0, 2.0), None) == (75.0, 2.0)
    assert src._precision_combine(None, (77.0, 1.0)) == (77.0, 1.0)


def test_precision_combine_rejects_non_positive_sigma():
    """σ=0 or negative is pathological — combine must reject."""
    assert src._precision_combine((75.0, 0.0), (77.0, 1.0)) is None
    assert src._precision_combine((75.0, -1.0), (77.0, 1.0)) is None


def test_kalshi_ticker_date_pattern_uses_letter_month():
    """Regression for 2026-05-02: pre-fix the analog matcher used
    ``lst_date.replace("-", "")[2:]`` which produces digit-month
    format (260502) but Kalshi tickers use letter-month (26MAY02).
    The LIKE query matched zero rows and the matcher silently fell
    back to None for every prediction.
    """
    assert src._kalshi_ticker_date_pattern("2026-05-02") == "26MAY02"
    assert src._kalshi_ticker_date_pattern("2026-01-15") == "26JAN15"
    assert src._kalshi_ticker_date_pattern("2026-12-31") == "26DEC31"


def test_kalshi_ticker_date_pattern_handles_malformed_input():
    """Malformed date string falls back to the input unchanged
    rather than crashing — caller will get zero matches and
    gracefully produce None."""
    # Should not raise
    src._kalshi_ticker_date_pattern("not-a-date")
    src._kalshi_ticker_date_pattern("")


# ── 2. Curve fit ──────────────────────────────────────────────────────


def _synth_obs(temps_at_lst_hours: list[tuple[float, float]],
                lst_offset: int) -> list[dict]:
    """Build observations list for _fit_today_curve.

    ``temps_at_lst_hours`` = [(lst_hour_decimal, temp_f), ...]
    """
    today_lst = datetime.now(timezone.utc) + timedelta(hours=lst_offset)
    today_lst_midnight = today_lst.replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    out = []
    for lst_h, temp_f in temps_at_lst_hours:
        h_int = int(lst_h)
        m_int = int((lst_h - h_int) * 60)
        utc_ts = today_lst_midnight.replace(
            hour=h_int, minute=m_int,
        ) - timedelta(hours=lst_offset)
        out.append({
            "temp_f": temp_f,
            "obs_time_utc": utc_ts,
            "is_metar": False,
            "raw": {},
        })
    return out


def test_curve_fit_recovers_synthetic_peak():
    """Build a synthetic concave-down curve peaking at LST 14 with
    peak temp 80°F. The fit should recover those values."""
    # T(h) = 80 - 0.5 * (h - 14)² → peaks at h=14, T=80
    pts = []
    for h_x10 in range(60, 200, 5):  # 6.0 to 20.0 LST in 0.5 steps
        h = h_x10 / 10.0
        t = 80.0 - 0.5 * (h - 14.0) ** 2
        pts.append((h, t))
    obs = _synth_obs(pts, lst_offset=-5)

    # Pin "now" so today's-LST-date filter passes
    fixed_now = datetime.now(timezone.utc)

    class _FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz is None else fixed_now.astimezone(tz)

    with patch.object(src, "datetime", _FixedDT):
        result = src._fit_today_curve(obs, lst_offset=-5)
    assert result is not None
    pred, sigma, n = result
    assert pred == pytest.approx(80.0, abs=0.5)
    assert sigma > 0
    assert n >= 12


def test_curve_fit_rejects_concave_up():
    """A curve that's still RISING (concave-up) should be rejected
    — we don't want to extrapolate forever upward."""
    # T(h) = (h - 6)² → minimum at h=6, rising afterward
    pts = []
    for h_x10 in range(110, 160, 5):  # LST 11 to 16 — still rising
        h = h_x10 / 10.0
        t = (h - 6.0) ** 2
        pts.append((h, t))
    obs = _synth_obs(pts, lst_offset=-5)
    fixed_now = datetime.now(timezone.utc)

    class _FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz is None else fixed_now.astimezone(tz)

    with patch.object(src, "datetime", _FixedDT):
        result = src._fit_today_curve(obs, lst_offset=-5)
    assert result is None  # concave-up rejected


def test_curve_fit_rejects_few_points():
    """Below _MIN_POINTS_FOR_CURVE_FIT (=12) the fit is too unreliable."""
    obs = _synth_obs([(12.0, 70.0), (13.0, 72.0), (14.0, 73.0)], lst_offset=-5)
    fixed_now = datetime.now(timezone.utc)

    class _FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz is None else fixed_now.astimezone(tz)

    with patch.object(src, "datetime", _FixedDT):
        assert src._fit_today_curve(obs, lst_offset=-5) is None


# ── 3. Analog matcher with seeded DB ───────────────────────────────────


@pytest.fixture
def memdb():
    conn = init_db(":memory:")
    yield conn
    conn.close()


def _seed_historical_day(
    conn, station: str, lst_date: str,
    forecast_hrrr: float, forecast_weather: float,
    actual_high: float, afd_bias: float = 0.0,
):
    """Seed both the snapshot table (forecasts) and the
    weather_metar_hourly_backfill table (truth) for one historical day.
    """
    # Snapshots: morning predictions. Use the SAME ticker pattern
    # the production matcher looks for (Kalshi letter-month format).
    ticker = (
        f"KXHIGH{station[1:]}-"
        f"{src._kalshi_ticker_date_pattern(lst_date)}-T75"
    )
    for src_name, val in [
        ("hrrr", forecast_hrrr),
        ("weather", forecast_weather),
        ("afd_bias", afd_bias),
    ]:
        conn.execute(
            """INSERT INTO weather_forecast_snapshots
                  (recorded_at, series, ticker, source, forecast_high_f,
                   sigma_f, hours_out)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (f"{lst_date}T10:00:00Z", f"KXHIGH{station[1:]}", ticker,
             src_name, val, 2.0, 12),
        )
    # Truth row
    conn.execute(
        """INSERT OR REPLACE INTO weather_metar_hourly_backfill
              (created_at, station, lst_date, lst_hour, temp_f, daily_high_f)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (f"{lst_date}T20:00:00Z", station, lst_date, 14,
         actual_high - 1.0, actual_high),
    )
    conn.commit()


def test_analog_matcher_finds_similar_days(memdb):
    """Seed 25 historical days. 5 of them have features matching today.
    The matcher should pull those 5 near the top and produce a
    prediction close to their average actual peak.
    """
    station = "KMIA"
    today_iso = "2026-05-02"
    today_d = datetime.strptime(today_iso, "%Y-%m-%d").date()

    # 5 historical days that look like today: forecast 88, actual 87
    for i in range(5):
        d = (today_d - timedelta(days=i + 1)).strftime("%Y-%m-%d")
        _seed_historical_day(memdb, station, d, 88.0, 87.5, 87.0)

    # 20 dissimilar days: forecast 60, actual 62 (cool regime)
    for i in range(20):
        d = (today_d - timedelta(days=i + 6)).strftime("%Y-%m-%d")
        _seed_historical_day(memdb, station, d, 60.0, 60.0, 62.0)

    # Today's features look like the 5 warm days
    today_features = {
        "forecast_hrrr": 88.0,
        "forecast_weather": 87.5,
        "afd_bias": 0.0,
        "day_of_year": float(today_d.timetuple().tm_yday),
        "lag1_residual": 1.0,
        "lag2_residual": 0.5,
        "lag3_residual": 1.5,
    }

    result = src._find_analog_days(memdb, station, today_features)
    assert result is not None
    mu, sigma, k = result
    # Should pick the 5 warm-day analogs (actual_high=87.0) over the
    # 20 cool-day analogs (actual_high=62.0). Top-k=10 will likely
    # include all 5 warm days + 5 cool days; the inverse-distance
    # weighting heavily favors the warm ones.
    assert mu > 80.0, f"expected μ near 87 (warm analogs), got {mu:.1f}"
    assert k >= 5


def test_analog_matcher_returns_none_when_thin(memdb):
    """Below _ANALOG_MIN_HISTORICAL_DAYS (=20) historical days, the
    matcher refuses to produce a prediction."""
    station = "KMIA"
    today_iso = "2026-05-02"
    today_d = datetime.strptime(today_iso, "%Y-%m-%d").date()
    # Only 5 historical days — way below threshold
    for i in range(5):
        d = (today_d - timedelta(days=i + 1)).strftime("%Y-%m-%d")
        _seed_historical_day(memdb, station, d, 80.0, 80.0, 80.0)

    today_features = {
        "forecast_hrrr": 80.0, "forecast_weather": 80.0, "afd_bias": 0.0,
        "day_of_year": 122.0,
        "lag1_residual": 0.0, "lag2_residual": 0.0, "lag3_residual": 0.0,
    }
    assert src._find_analog_days(memdb, station, today_features) is None
