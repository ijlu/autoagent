"""Counterfactual: what would today's v2 ensemble have said on past markets?

Most rows in ``weather_mm_shadow`` were written when ``WEATHER_ENSEMBLE_V2``
was still false — the ``fair_value_cents`` values are v1 outputs (with the
broken Platt curve). Comparing them to settled outcomes measures v1, not
the v2 we just shipped. This tool fills that gap.

For each settled historical ticker:
  1. Pull each source's latest pre-settlement snapshot (μ, hours_out)
     from ``weather_forecast_snapshots``.
  2. Build a fresh ``GaussianForecast`` per source with a sane default σ
     (the raw source σ, before any learned override).
  3. Call **today's** ``predict_v2`` on that synthetic input list, with
     the live kv state — i.e. with all the per-city skill σ, MOS bias,
     staleness inflation, σ ceiling, and truncated projection in place.
  4. Compare the resulting prob to the settled outcome and to the market
     mid (recorded in the shadow row at decision time).

What we're answering:
  * Would today's v2 have beaten v1 on past data? (replay vs live shadow)
  * Would today's v2 have beaten market mid on past data? (alpha test)

The replay uses snapshot μ values *that were observed historically*. It
does not re-fetch sources or change the data — only the post-source
processing (σ, bias, staleness, projection) reflects today's code +
current kv values.

Requires Kalshi market data for bracket bounds — refetched once per unique
ticker and cached (~1,500 calls × 0.3s ≈ 8 min).

Run on the VPS::

    python -m tools.backtest_v2_replay --db /home/kalshi/autoagent/kalshi_trades.db
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from typing import Optional

from bot.config import DB_PATH
from bot.db import init_db
from bot.signals import weather_ensemble_v2 as v2
from bot.signals.weather_forecast import GaussianForecast


KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
HTTP_PACE_S = 0.20

# Each source's "raw" σ before skill curve override. The replay uses
# these as the starting point so today's v2 can re-apply learned
# corrections — exactly what would happen on a live quote.
_RAW_SOURCE_SIGMA: dict[str, float] = {
    "hrrr": 1.2,
    "nbm": 1.8,
    "weather": 2.0,
    "open_meteo": 2.0,
    "nws_point": 2.0,
    "metar": 1.5,
    "madis": 2.5,
}

_MARKET_CACHE: dict[str, Optional[dict]] = {}

# (city_key, settle_date) → ECMWF (mean_f, sigma_f) — fetched live from
# Open-Meteo's historical-forecast endpoint. Cached because the same
# event date is asked about across many tickers (one per bracket).
_ECMWF_CACHE: dict[tuple[str, str], Optional[tuple[float, float]]] = {}

_OPEN_METEO_HISTORICAL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
_ECMWF_MODEL = "ecmwf_ifs025"

# Lat/lon per city — must match the production source modules.
_CITY_COORDS: dict[str, tuple[float, float, str]] = {
    "nyc":         (40.78, -73.97, "America/New_York"),
    "chicago":     (41.79, -87.75, "America/Chicago"),
    "miami":       (25.79, -80.32, "America/New_York"),
    "los_angeles": (33.94, -118.41, "America/Los_Angeles"),
    "austin":      (30.18, -97.68, "America/Chicago"),
    "denver":      (39.84, -104.66, "America/Denver"),
}


def _fetch_ecmwf_historical(city: str, settle_date: str) -> Optional[tuple[float, float]]:
    """Fetch the ECMWF forecast for (city, settle_date) issued ~12-24h
    before settlement. Returns (mean_high, sigma) in °F or None on failure.

    We pull a 2-day window centered on settle_date so the daily-high
    aggregation has enough hourly samples even at edges of the ECMWF
    schedule. Sigma is hardcoded to 1.4°F (a reasonable mid-range prior;
    today's predict_v2 will replace it with the learned skill σ when it
    runs the combine).
    """
    key = (city, settle_date)
    if key in _ECMWF_CACHE:
        return _ECMWF_CACHE[key]
    coords = _CITY_COORDS.get(city)
    if coords is None:
        _ECMWF_CACHE[key] = None
        return None
    lat, lon, tz = coords
    url = (
        f"{_OPEN_METEO_HISTORICAL}?latitude={lat}&longitude={lon}"
        f"&hourly=temperature_2m"
        f"&temperature_unit=fahrenheit&timezone={tz}"
        f"&start_date={settle_date}&end_date={settle_date}"
        f"&models={_ECMWF_MODEL}"
    )
    data = _http_get(url)
    time.sleep(HTTP_PACE_S)
    hourly = (data or {}).get("hourly", {}) or {}
    times = hourly.get("time", []) or []
    temps = hourly.get("temperature_2m", []) or []
    if not times or not temps:
        _ECMWF_CACHE[key] = None
        return None
    # Filter to hourly observations falling on settle_date, then take the max.
    daily_temps = [
        float(v) for t, v in zip(times, temps)
        if v is not None and isinstance(t, str) and t.startswith(settle_date)
    ]
    if not daily_temps:
        _ECMWF_CACHE[key] = None
        return None
    daily_high = max(daily_temps)
    # Use 1.4°F as starting sigma — predict_v2 will override via skill σ
    # when the cell exists (currently only HRRR/NBM/weather have per-city
    # fits, so ECMWF will fall back to pooled σ → σ ceiling at 2.0°F).
    result = (daily_high, 1.4)
    _ECMWF_CACHE[key] = result
    return result


def _http_get(url: str, retries: int = 3) -> dict:
    for i in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "kalshi-bot-replay/1.0"}
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except Exception:
            if i == retries - 1:
                return {}
            time.sleep(0.5 * (2 ** i))
    return {}


def _fetch_market(ticker: str) -> Optional[dict]:
    if ticker in _MARKET_CACHE:
        return _MARKET_CACHE[ticker]
    url = f"{KALSHI_BASE}/markets/{urllib.parse.quote(ticker, safe='')}"
    data = _http_get(url)
    market = data.get("market")
    _MARKET_CACHE[ticker] = market
    time.sleep(HTTP_PACE_S)
    return market


def _replay_predict_v2(
    ticker: str, market: dict, conn: sqlite3.Connection,
    *, include_ecmwf: bool = False, disable_afd: bool = False,
) -> Optional[tuple[float, str]]:
    """Run today's ``predict_v2`` against historical per-source snapshots.

    Returns (prob, tag) or None if we can't reconstruct the input list."""
    rows = conn.execute(
        """SELECT s.source, s.forecast_high_f, s.hours_out
             FROM weather_forecast_snapshots s
             JOIN (SELECT source, MAX(id) AS mid
                     FROM weather_forecast_snapshots
                    WHERE ticker = ?
                      AND source NOT IN ('combined_v2', 'afd_bias')
                      AND forecast_high_f IS NOT NULL
                      AND hours_out IS NOT NULL
                    GROUP BY source) latest ON latest.mid = s.id""",
        (ticker,),
    ).fetchall()
    if not rows:
        return None

    gaussians: list[GaussianForecast] = []
    for src, mu, hours_out in rows:
        sigma = _RAW_SOURCE_SIGMA.get(src, 2.0)
        try:
            g = GaussianForecast(
                mean_f=float(mu),
                sigma_f=sigma,
                horizon_hours=float(hours_out),
                source_name=src,
                source_tag=f"{src}:replay",
            )
            gaussians.append(g)
        except (TypeError, ValueError):
            continue
    if not gaussians:
        return None

    # Optional: fetch historical ECMWF for this (city, settle_date) and
    # add it as an additional source. ECMWF is genuinely independent from
    # Open-Meteo's gfs_seamless / gfs_hrrr (which we discovered are
    # serving the same data), so adding it gives the combine a real
    # second model voice.
    if include_ecmwf:
        from bot.signals.weather_ensemble_v2 import _city_for_ticker
        city = _city_for_ticker(ticker)
        if city:
            # Parse settle_date from ticker (KXHIGH<CITY>-26APR27-... → 2026-04-27)
            parts = ticker.split("-")
            if len(parts) >= 2 and len(parts[1]) >= 7:
                suf = parts[1]
                months = ["JAN","FEB","MAR","APR","MAY","JUN","JUL",
                          "AUG","SEP","OCT","NOV","DEC"]
                try:
                    yy = int(suf[:2])
                    mon = suf[2:5].upper()
                    dd = int(suf[5:7])
                    m_idx = months.index(mon) + 1
                    settle_date = f"20{yy:02d}-{m_idx:02d}-{dd:02d}"
                    ecmwf_data = _fetch_ecmwf_historical(city, settle_date)
                    if ecmwf_data is not None:
                        ec_mu, ec_sigma = ecmwf_data
                        try:
                            # ECMWF gets its own source name — won't get the
                            # GFS-family ρ=1.0 group discount because it's
                            # not in _MODEL_GROUP. Full weight in the combine.
                            horizon = max(g.horizon_hours for g in gaussians)
                            gaussians.append(GaussianForecast(
                                mean_f=ec_mu, sigma_f=ec_sigma,
                                horizon_hours=horizon,
                                source_name="ecmwf",
                                source_tag=f"ecmwf:replay_{city}_{settle_date}",
                            ))
                        except (TypeError, ValueError):
                            pass
                except (ValueError, IndexError):
                    pass

    # Monkey-patch _collect_gaussians so today's predict_v2 sees our
    # historically-reconstructed input list. All other layers (skill σ,
    # MOS bias, staleness inflation, σ ceiling, truncated projection)
    # run exactly as they would on a live quote.
    saved = v2._collect_gaussians
    v2._collect_gaussians = lambda *a, **kw: gaussians
    saved_afd = None
    if disable_afd:
        # Force the AFD source's bias getter to return (None, None, None)
        # so predict_v2's AFD-shift step skips. Restored after the call.
        import bot.signals.sources.afd as afd_mod
        saved_afd = afd_mod.get_afd_bias
        afd_mod.get_afd_bias = lambda *a, **kw: (None, None, None)
    try:
        return v2.predict_v2(ticker, market)
    except Exception as exc:
        sys.stderr.write(f"[replay] {ticker} raised {type(exc).__name__}: {exc}\n")
        return None
    finally:
        v2._collect_gaussians = saved
        if saved_afd is not None:
            import bot.signals.sources.afd as afd_mod
            afd_mod.get_afd_bias = saved_afd


def run(conn: sqlite3.Connection, limit: Optional[int] = None,
        include_ecmwf: bool = False, disable_afd: bool = False) -> None:
    # Pull one row per (ticker, recorded_at LATEST) so each ticker
    # contributes one settled data point. Use shadow.fair_value_cents +
    # market_mid + ticker_settled_yes from the shadow row.
    rows = conn.execute(
        """SELECT
              ticker, series,
              MAX(ts_unix) AS ts,
              MAX(fair_value_cents) AS live_fair,
              MAX(market_mid) AS market_mid,
              MAX(ticker_settled_yes) AS settled
            FROM weather_mm_shadow
           WHERE ticker_settled_yes IS NOT NULL
             AND fair_value_cents IS NOT NULL
             AND market_mid IS NOT NULL
        GROUP BY ticker
        ORDER BY ts ASC""",
    ).fetchall()
    if limit:
        rows = rows[:limit]

    print(f"[replay] {len(rows)} settled tickers; refetching markets + replaying v2...")
    t0 = time.time()
    last_print = t0

    # Per-series: list of (live_b, mkt_b, replay_b)
    series_brier: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    skipped = {"no_market": 0, "no_replay": 0, "no_projection": 0}
    n_processed = 0

    for ticker, series, _ts, live_fair, market_mid, settled in rows:
        market = _fetch_market(ticker)
        if not market:
            skipped["no_market"] += 1
            continue
        try:
            replay = _replay_predict_v2(
                ticker, market, conn,
                include_ecmwf=include_ecmwf,
                disable_afd=disable_afd,
            )
        except Exception:
            replay = None
        if replay is None or replay[0] is None:
            skipped["no_replay"] += 1
            continue

        prob, _tag = replay
        won_yes = float(settled)
        live_b   = (float(live_fair) / 100.0 - won_yes) ** 2
        mkt_b    = (float(market_mid) / 100.0 - won_yes) ** 2
        replay_b = (float(prob) - won_yes) ** 2
        series_brier[series].append((live_b, mkt_b, replay_b))
        n_processed += 1

        if time.time() - last_print > 10:
            elapsed = time.time() - t0
            rate = n_processed / max(elapsed, 1e-6)
            remaining = (len(rows) - n_processed) / max(rate, 1e-6)
            print(f"  ... {n_processed}/{len(rows)} "
                  f"({rate:.1f}/s, ~{remaining:.0f}s left)")
            last_print = time.time()

    elapsed = time.time() - t0
    print(f"[replay] processed={n_processed} skipped={dict(skipped)}  ({elapsed:.0f}s)")
    print()
    print("=" * 92)
    print("v2 counterfactual replay — Brier vs settled outcomes")
    print("=" * 92)
    print(f"  {'series':<11} {'n':>5} {'v1_live':>9} {'mkt_mid':>9} "
          f"{'v2_replay':>10} {'v2-mkt_edge':>12}")
    print("  " + "-" * 70)
    pooled_live = []
    pooled_mkt = []
    pooled_replay = []
    for series in sorted(series_brier.keys()):
        rows_b = series_brier[series]
        if not rows_b:
            continue
        live_b = statistics.mean(r[0] for r in rows_b)
        mkt_b = statistics.mean(r[1] for r in rows_b)
        replay_b = statistics.mean(r[2] for r in rows_b)
        edge = mkt_b - replay_b
        print(f"  {series:<11} {len(rows_b):>5} "
              f"{live_b:>9.4f} {mkt_b:>9.4f} {replay_b:>10.4f} "
              f"{edge:>+11.4f}")
        pooled_live.extend(r[0] for r in rows_b)
        pooled_mkt.extend(r[1] for r in rows_b)
        pooled_replay.extend(r[2] for r in rows_b)

    if pooled_live:
        print()
        print(f"  POOLED  n={len(pooled_live):>5}  "
              f"live={statistics.mean(pooled_live):.4f}  "
              f"mkt={statistics.mean(pooled_mkt):.4f}  "
              f"v2_replay={statistics.mean(pooled_replay):.4f}  "
              f"edge_vs_mkt={statistics.mean(pooled_mkt)-statistics.mean(pooled_replay):+.4f}  "
              f"edge_vs_v1={statistics.mean(pooled_live)-statistics.mean(pooled_replay):+.4f}")

    print()
    print("Reading guide:")
    print("  v1_live   = Brier of fair_value_cents recorded in weather_mm_shadow at decision time")
    print("              (mostly v1 outputs — the broken Platt-curve era).")
    print("  mkt_mid   = Brier of the market consensus at the same decision time.")
    print("  v2_replay = Brier of today's predict_v2 evaluated against historical snapshot data.")
    print("  edge_vs_mkt > 0 means v2 beats market consensus on past data — alpha.")
    print("  edge_vs_v1  > 0 means v2 is an improvement over what was actually quoted.")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=DB_PATH)
    p.add_argument("--limit", type=int, default=None,
                   help="Limit to first N settled tickers (for debugging)")
    p.add_argument("--include-ecmwf", action="store_true",
                   help="Add ECMWF as a 7th source via Open-Meteo's "
                        "historical-forecast endpoint. Slower (extra API "
                        "call per (city, settle_date)).")
    p.add_argument("--disable-afd", action="store_true",
                   help="Force get_afd_bias to return None so predict_v2 "
                        "skips the AFD shift step. Use to A/B-test whether "
                        "AFD helps the ensemble at the bracket-projection "
                        "level (different question from |error| on point "
                        "predictions).")
    args = p.parse_args()
    conn = init_db(args.db)
    run(conn, limit=args.limit, include_ecmwf=args.include_ecmwf,
        disable_afd=args.disable_afd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
