"""NWS 5-min temperature → daily-high forecast via curve fit + analog days.

Two predictors combine into a single Gaussian:

  1. **Today's curve fit**: quadratic regression on today's 5-min readings
     so far. Vertex of the fitted parabola = predicted peak time +
     temperature. σ from residual std + horizon penalty.

  2. **Historical analog**: K-nearest-neighbors over a feature vector
     (forecast μ from HRRR/weather, AFD bias parsed today,
     day-of-year, last-3-days' residuals). Top-k similar days'
     actual peaks → inverse-distance-weighted prediction. σ from
     spread across neighbors.

Combined via precision weighting (1/σ² weights). The component with
tighter σ dominates. Early in the day, today's curve has few points
→ wide σ → analog dominates. Late afternoon, today's curve is well-
constrained → curve dominates. Exactly the dynamic we want.

This is one source. The combine sees a single Gaussian; internal blend
is not exposed to the upstream ensemble. ``nws_5min`` (running max) and
``nws_5min_diurnal`` remain separate sources — each contributes
independently, and the obs-group correlation discount handles the
overlap.
"""
from __future__ import annotations

import math
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from bot.daemon.locks import DB_WRITE_LOCK
from bot.daemon.stations import (
    lst_offset_for_station, station_for_ticker,
)
from bot.db import get_connection
from bot.signals.sources.nws_5min import (
    PRIMARY_5MIN_STATION_BY_CITY, _MAX_OBS_AGE_S,
    _MIN_LST_HOUR_TO_FIRE, fetch_recent_observations,
)
from bot.signals.weather_forecast import GaussianForecast, hours_until_settlement_end


_SOURCE_NAME = "nws_5min_analog"

# Curve fit parameters
_MIN_POINTS_FOR_CURVE_FIT = 12  # ~1 hour of 5-min readings
_CURVE_SIGMA_FLOOR_F = 1.0
_CURVE_SIGMA_CEIL_F = 8.0
# Sanity bounds on the fitted vertex temperature
_PEAK_BAND_BELOW_F = 5.0   # vertex can't be more than 5°F below current
_PEAK_BAND_ABOVE_F = 30.0  # or more than 30°F above (extreme spring CONUS)

# Analog matcher parameters
_ANALOG_K = 10
_ANALOG_MIN_HISTORICAL_DAYS = 20  # if backfill has fewer days, skip analog
_ANALOG_LOOKBACK_DAYS = 120
# Distance threshold beyond which a historical day is rejected as too dissimilar
_ANALOG_MAX_DISTANCE = 5.0


# ── Curve fit ─────────────────────────────────────────────────────────


def _fit_today_curve(
    observations: list[dict], lst_offset: int,
) -> Optional[tuple[float, float, int]]:
    """Fit T(h) = a + b·h + c·h² to today's 5-min readings.

    Returns ``(predicted_peak_f, sigma_f, n_points_used)`` or None
    when the fit is unusable (too few points, concave-up shape,
    extreme prediction).

    ``observations`` is the list returned by
    ``fetch_recent_observations`` — newest-first dicts with
    ``temp_f`` and ``obs_time_utc``.
    """
    if len(observations) < _MIN_POINTS_FOR_CURVE_FIT:
        return None

    # Convert obs times to LST decimal hours (e.g. 14.25 = 14:15 LST)
    pts: list[tuple[float, float]] = []
    for o in observations:
        utc = o["obs_time_utc"]
        lst = utc + timedelta(hours=lst_offset)
        # Restrict to today's LST date so we don't fit yesterday's curve
        if lst.date() != (datetime.now(timezone.utc) + timedelta(hours=lst_offset)).date():
            continue
        h = lst.hour + lst.minute / 60.0 + lst.second / 3600.0
        pts.append((h, o["temp_f"]))
    if len(pts) < _MIN_POINTS_FOR_CURVE_FIT:
        return None

    # Pure-Python least-squares for y = a + b·x + c·x²
    n = len(pts)
    sum_x = sum(p[0] for p in pts)
    sum_y = sum(p[1] for p in pts)
    sum_x2 = sum(p[0] ** 2 for p in pts)
    sum_x3 = sum(p[0] ** 3 for p in pts)
    sum_x4 = sum(p[0] ** 4 for p in pts)
    sum_xy = sum(p[0] * p[1] for p in pts)
    sum_x2y = sum(p[0] ** 2 * p[1] for p in pts)

    # Normal equations: [[n, sum_x, sum_x2], [sum_x, sum_x2, sum_x3],
    #                    [sum_x2, sum_x3, sum_x4]] @ [a, b, c] =
    #                   [sum_y, sum_xy, sum_x2y]
    M = [
        [n, sum_x, sum_x2],
        [sum_x, sum_x2, sum_x3],
        [sum_x2, sum_x3, sum_x4],
    ]
    v = [sum_y, sum_xy, sum_x2y]
    sol = _solve_3x3(M, v)
    if sol is None:
        return None
    a, b, c = sol

    # Vertex (peak): h* = -b / (2c). c must be NEGATIVE for a peak.
    if c >= -1e-6:
        # Concave-up or flat: not a peaking curve. Reject the fit.
        return None
    h_peak = -b / (2.0 * c)

    # Sanity-clamp vertex hour: peak should land in 11:00-19:00 LST,
    # not extrapolate to midnight or beyond.
    if not (10.0 <= h_peak <= 20.0):
        return None

    predicted_peak_f = a + b * h_peak + c * h_peak * h_peak

    # Compute residual std on the fit
    residuals = [p[1] - (a + b * p[0] + c * p[0] * p[0]) for p in pts]
    mean_r = sum(residuals) / n
    var_r = sum((r - mean_r) ** 2 for r in residuals) / max(1, n - 3)  # 3 params
    sigma = math.sqrt(max(0.0, var_r))

    # Add horizon penalty: σ widens with hours-to-peak. The fit is
    # tightest when the peak has already passed (h_peak <= now);
    # widens when we're projecting forward.
    now_lst = datetime.now(timezone.utc) + timedelta(hours=lst_offset)
    now_h = now_lst.hour + now_lst.minute / 60.0
    hours_to_peak = max(0.0, h_peak - now_h)
    horizon_penalty = math.sqrt(1.0 + hours_to_peak / 4.0)
    sigma = sigma * horizon_penalty

    # Clamp σ to safe band
    sigma = max(_CURVE_SIGMA_FLOOR_F, min(_CURVE_SIGMA_CEIL_F, sigma))

    # Sanity-clamp the predicted peak relative to the latest reading
    latest_temp = pts[-1][1]  # newest-first ordering, but rely on LST hour
    # Re-compute latest by max LST hour
    latest_temp = max(pts, key=lambda p: p[0])[1]
    predicted_peak_f = max(
        latest_temp - _PEAK_BAND_BELOW_F,
        min(latest_temp + _PEAK_BAND_ABOVE_F, predicted_peak_f),
    )

    if not math.isfinite(predicted_peak_f):
        return None
    return predicted_peak_f, sigma, n


def _solve_3x3(M: list[list[float]], v: list[float]) -> Optional[list[float]]:
    """Solve 3x3 linear system M·x = v via Cramer's rule. Returns None
    if singular. Pure-Python (no numpy dep)."""
    def det3(m):
        return (
            m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
            - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
            + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
        )

    D = det3(M)
    if abs(D) < 1e-12:
        return None
    out = []
    for col in range(3):
        Mi = [row[:] for row in M]
        for r in range(3):
            Mi[r][col] = v[r]
        out.append(det3(Mi) / D)
    return out


# ── Historical analog matcher ─────────────────────────────────────────


def _kalshi_ticker_date_pattern(lst_date: str) -> str:
    """Convert YYYY-MM-DD → Kalshi ticker date pattern (e.g. 26MAY02).

    Kalshi tickers embed the LST settlement date as ``YY{MMM}DD`` with
    the 3-letter month abbreviation. Pre-fix this helper used
    ``lst_date.replace("-", "")[2:]`` which produces ``260502`` —
    digit-month format that doesn't appear in any Kalshi ticker. The
    LIKE query consequently matched zero rows and the analog matcher
    silently fell back to None for every prediction.
    """
    months = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
              "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
    try:
        d = datetime.strptime(lst_date, "%Y-%m-%d")
    except ValueError:
        return lst_date  # caller will get no matches → falls back gracefully
    return f"{d.year % 100:02d}{months[d.month - 1]}{d.day:02d}"


def _build_today_features(
    conn: sqlite3.Connection, station: str, lst_date: str,
) -> Optional[dict]:
    """Build the feature vector for today, matching the same shape
    we'll extract for historical days. Returns dict with keys
    `forecast_hrrr`, `forecast_weather`, `afd_bias`, `day_of_year`,
    `lag1_residual`, `lag2_residual`, `lag3_residual`. All values
    are floats; None if any required field is missing.
    """
    feats: dict[str, float] = {}

    # Forecasts targeting today's market: pull every snapshot for
    # this ticker and average. Originally we constrained to a 06-18
    # UTC "morning of lst_date" window, but probing prod showed the
    # daemon's snapshot cadence varies — older markets only have
    # snapshots from the afternoon onward. The mean across the
    # market's lifetime is a stable, vintage-consistent feature for
    # the analog matcher (each historical day is compared on the
    # same statistic).
    try:
        d = datetime.strptime(lst_date, "%Y-%m-%d")
    except ValueError:
        return None
    ticker_pattern = f"%-{_kalshi_ticker_date_pattern(lst_date)}-%"
    rows = conn.execute(
        """SELECT source, AVG(forecast_high_f)
             FROM weather_forecast_snapshots
            WHERE source IN ('hrrr', 'weather', 'afd_bias')
              AND series LIKE 'KXHIGH%'
              AND ticker LIKE ?
            GROUP BY source""",
        (ticker_pattern,),
    ).fetchall()
    src_means = {r[0]: r[1] for r in rows if r[1] is not None}
    if "hrrr" not in src_means or "weather" not in src_means:
        return None
    feats["forecast_hrrr"] = float(src_means["hrrr"])
    feats["forecast_weather"] = float(src_means["weather"])
    # AFD: optional — if not present, use 0 (no shift)
    feats["afd_bias"] = float(src_means.get("afd_bias", 0.0))

    # Day-of-year (d already parsed above)
    feats["day_of_year"] = float(d.timetuple().tm_yday)

    # Lag residuals: yesterday/2-days-ago/3-days-ago
    # residual_d = forecast_hrrr_d - actual_d
    for lag in (1, 2, 3):
        d_lag = d - timedelta(days=lag)
        d_lag_iso = d_lag.strftime("%Y-%m-%d")
        # Lag forecast: average all hrrr snapshots for the lag-day's
        # ticker (same vintage-consistent strategy as today's pull).
        f_row = conn.execute(
            """SELECT AVG(forecast_high_f) FROM weather_forecast_snapshots
                WHERE source = 'hrrr' AND ticker LIKE ?""",
            (f"%-{_kalshi_ticker_date_pattern(d_lag_iso)}-%",),
        ).fetchone()
        a_row = conn.execute(
            """SELECT daily_high_f FROM weather_metar_hourly_backfill
                WHERE station = ? AND lst_date = ?
                  AND daily_high_f IS NOT NULL
                ORDER BY lst_hour DESC LIMIT 1""",
            (station, d_lag_iso),
        ).fetchone()
        if f_row is None or f_row[0] is None or a_row is None:
            # Missing residual for this lag → use 0 (neutral)
            feats[f"lag{lag}_residual"] = 0.0
        else:
            feats[f"lag{lag}_residual"] = float(f_row[0]) - float(a_row[0])
    return feats


def _build_historical_features(
    conn: sqlite3.Connection, station: str, lookback_days: int,
) -> list[tuple[str, dict, float]]:
    """Walk historical days and build (date, feature_dict, actual_peak)
    tuples for the analog matcher. ``actual_peak`` is the
    settled daily high (CF6 if available, else METAR running high).
    """
    today = datetime.now(timezone.utc).date()
    out: list[tuple[str, dict, float]] = []
    for days_back in range(1, lookback_days + 1):
        d = today - timedelta(days=days_back)
        d_iso = d.strftime("%Y-%m-%d")
        feats = _build_today_features(conn, station, d_iso)
        if feats is None:
            continue
        a_row = conn.execute(
            """SELECT daily_high_f FROM weather_metar_hourly_backfill
                WHERE station = ? AND lst_date = ?
                  AND daily_high_f IS NOT NULL
                ORDER BY lst_hour DESC LIMIT 1""",
            (station, d_iso),
        ).fetchone()
        if a_row is None:
            continue
        out.append((d_iso, feats, float(a_row[0])))
    return out


def _zscore_normalize(
    feat_vectors: list[dict], today: dict,
) -> tuple[list[list[float]], list[float], list[str]]:
    """Standardize features so euclidean distance treats each axis
    equivalently. Returns (historical_matrix, today_vector, axis_names).
    """
    axes = ["forecast_hrrr", "forecast_weather", "afd_bias",
            "day_of_year", "lag1_residual", "lag2_residual", "lag3_residual"]
    # Compute mean + std per axis from historical only (not today)
    means = {}
    stds = {}
    for a in axes:
        vals = [f[a] for f in feat_vectors]
        if not vals:
            return [], [], axes
        m = sum(vals) / len(vals)
        v = sum((x - m) ** 2 for x in vals) / max(1, len(vals) - 1)
        s = math.sqrt(v) if v > 1e-9 else 1.0
        means[a] = m
        stds[a] = s
    hist = [[(f[a] - means[a]) / stds[a] for a in axes] for f in feat_vectors]
    today_v = [(today[a] - means[a]) / stds[a] for a in axes]
    return hist, today_v, axes


def _find_analog_days(
    conn: sqlite3.Connection, station: str, today_features: dict,
    k: int = _ANALOG_K, lookback_days: int = _ANALOG_LOOKBACK_DAYS,
) -> Optional[tuple[float, float, int]]:
    """KNN over historical days. Returns
    ``(predicted_peak_f, sigma_f, n_neighbors_used)`` or None when
    there isn't enough historical data.
    """
    historical = _build_historical_features(conn, station, lookback_days)
    if len(historical) < _ANALOG_MIN_HISTORICAL_DAYS:
        return None

    feat_vectors = [h[1] for h in historical]
    hist_matrix, today_v, _ = _zscore_normalize(feat_vectors, today_features)
    if not hist_matrix:
        return None

    # Compute distances + take top-k
    distances = []
    for i, hv in enumerate(hist_matrix):
        d = math.sqrt(sum((a - b) ** 2 for a, b in zip(hv, today_v)))
        distances.append((d, i))
    distances.sort()
    nearest = [
        (d, historical[i]) for d, i in distances[:k]
        if d <= _ANALOG_MAX_DISTANCE
    ]
    if len(nearest) < 3:
        return None  # too few similar days to trust

    # Inverse-distance-weighted mean of actual peaks. Add ε to avoid
    # divide-by-zero on identical neighbors.
    weights = [1.0 / (d + 0.1) for d, _ in nearest]
    peaks = [n[2] for _, n in nearest]
    total_w = sum(weights)
    predicted = sum(w * p for w, p in zip(weights, peaks)) / total_w

    # σ from spread of neighbors' peaks (weighted)
    var_w = sum(w * (p - predicted) ** 2 for w, p in zip(weights, peaks)) / total_w
    sigma = math.sqrt(max(0.0, var_w))
    sigma = max(0.5, min(8.0, sigma))

    return predicted, sigma, len(nearest)


# ── Combine ───────────────────────────────────────────────────────────


def _precision_combine(
    a: Optional[tuple[float, float]],
    b: Optional[tuple[float, float]],
) -> Optional[tuple[float, float]]:
    """Precision-weighted combine of two (μ, σ) tuples. Either may be
    None — returns the other. Returns None when both None.
    """
    if a is None and b is None:
        return None
    if a is None:
        return b
    if b is None:
        return a
    mu_a, sigma_a = a
    mu_b, sigma_b = b
    if sigma_a <= 0 or sigma_b <= 0:
        return None
    p_a = 1.0 / (sigma_a * sigma_a)
    p_b = 1.0 / (sigma_b * sigma_b)
    p_total = p_a + p_b
    mu = (p_a * mu_a + p_b * mu_b) / p_total
    sigma = math.sqrt(1.0 / p_total)
    return mu, sigma


def get_nws_5min_analog_gaussian(
    ticker: str, market_data: dict,
) -> Optional[GaussianForecast]:
    """Build a Gaussian from today's 5-min curve fit + historical
    analogs. See module docstring for design rationale.
    """
    ws = station_for_ticker((ticker or "").upper())
    if ws is None:
        return None

    city = ws.city.lower().replace(" ", "_")
    poll_station = PRIMARY_5MIN_STATION_BY_CITY.get(city, ws.icao)
    lst_offset = lst_offset_for_station(ws.icao)

    lst_now = datetime.now(timezone.utc) + timedelta(hours=lst_offset)
    if lst_now.hour < _MIN_LST_HOUR_TO_FIRE:
        return None

    # ── Today's curve fit ──
    obs = fetch_recent_observations(poll_station)
    curve_pred = None
    if obs:
        curve_result = _fit_today_curve(obs, lst_offset)
        if curve_result is not None:
            curve_mu, curve_sigma, _n = curve_result
            curve_pred = (curve_mu, curve_sigma)

    # ── Historical analog ──
    # Hold DB_WRITE_LOCK around the entire SQL traversal: this source
    # runs ~480 queries against the daemon's shared connection from a
    # poller thread. Python's sqlite3 module is not safe for concurrent
    # cursor use on a single connection — without the lock we get
    # InterfaceError + IndexError under load. Reads themselves are
    # lock-free under WAL, but cursor lifecycle on the shared conn is
    # what we're serializing here.
    analog_pred = None
    try:
        conn = get_connection()
    except RuntimeError:
        conn = None
    if conn is not None:
        today_lst_date = lst_now.strftime("%Y-%m-%d")
        with DB_WRITE_LOCK:
            today_features = _build_today_features(
                conn, ws.icao, today_lst_date,
            )
            if today_features is not None:
                analog_result = _find_analog_days(
                    conn, ws.icao, today_features,
                )
            else:
                analog_result = None
        if analog_result is not None:
            analog_mu, analog_sigma, _k = analog_result
            analog_pred = (analog_mu, analog_sigma)

    # ── Combine ──
    combined = _precision_combine(curve_pred, analog_pred)
    if combined is None:
        return None
    mu, sigma = combined

    # Provenance tag carries which sub-predictors fired
    parts = []
    if curve_pred is not None:
        parts.append("curve")
    if analog_pred is not None:
        parts.append("analog")
    tag = f"{_SOURCE_NAME}:{poll_station}_{'+'.join(parts)}"

    horizon_hours = hours_until_settlement_end(lst_offset, day_idx=0)
    issued_at = obs[0]["obs_time_utc"].timestamp() if obs else time.time()

    return GaussianForecast(
        mean_f=float(mu),
        sigma_f=float(sigma),
        horizon_hours=horizon_hours,
        source_name=_SOURCE_NAME,
        source_tag=tag,
        issued_at=issued_at,
    )
