"""Multi-horizon nowcast Brier diagnostic.

The methodology diagnostic showed our 12h-lead forecast loses to market mid
at decision_ts = close-18h. That's the wrong horizon to evaluate
``WeatherQuoter`` though — the live quoter requotes on every material
METAR change, so by the time we'd actually post we've absorbed the
running max-so-far. The right test is: at the horizon we'd quote, can a
nowcast (forecast + running METAR max) beat market mid?

We compute, for each backfill row and each horizon ``h ∈ {12, 8, 4}``:

  * ``running_max_f`` — max temp observed in the LST day from
    ``weather_metar_hourly_backfill`` up to (close − h).
  * ``nowcast_mean = max(forecast_mean, running_max + 0.5)`` — the daily
    high cannot be below the running max (with a half-degree ceiling
    cushion for residual peak).
  * ``nowcast_sigma = forecast_sigma × clamp(h / 12, 0.1, 1.0)`` —
    linear σ decay as the day progresses; at ``h=12`` keep the full
    forecast spread, at ``h=4`` shrink to ⅓.
  * Project nowcast onto bracket via ``probability_for_market``.
  * Market mid at the same horizon = VWAP across trades within ±90min.

Cities with no hourly METAR backfill (``KXHIGHCHI`` in the current data
window) are skipped automatically — the diagnostic just reports their
``no_metar`` count.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from bot.config import DB_PATH
from bot.db import init_db
from bot.signals import weather_ensemble_v2 as v2
from bot.signals.weather_forecast import (
    GaussianForecast,
    probability_for_market,
)


KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
HTTP_PACE_S = 0.20

FAMILY_TO_CITY = {
    "KXHIGHNY":  "nyc",
    "KXHIGHCHI": "chicago",      # no hourly METAR — will be skipped
    "KXHIGHMIA": "miami",
    "KXHIGHLAX": "los angeles",
    "KXHIGHAUS": "austin",
    "KXHIGHDEN": "denver",
}
CITY_TO_STATION = {
    "nyc": "KNYC",
    "miami": "KMIA",
    "los angeles": "KLAX",
    "austin": "KAUS",
    "denver": "KDEN",
    # chicago intentionally absent — no rows in weather_metar_hourly_backfill
}
# LST offset (UTC + offset = LST hour). Mirrors bot/daemon/stations.py.
CITY_TO_LST_OFFSET = {
    "nyc": -5, "miami": -5,
    "los angeles": -8,
    "austin": -6, "denver": -7,
}

HORIZONS = (12, 8, 4)            # hours-to-close decision points
TRADES_WINDOW_MIN = 90.0


# ── HTTP helpers (same shape as backfill_directional_shadow) ────────────

_MARKET_CACHE: dict[str, Optional[dict]] = {}
_TRADES_CACHE: dict[str, list[dict]] = {}


def _http_get(url: str, retries: int = 3) -> dict:
    for i in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "kalshi-bot-nowcast/1.0"}
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


def _fetch_trades(ticker: str, max_pages: int = 20) -> list[dict]:
    if ticker in _TRADES_CACHE:
        return _TRADES_CACHE[ticker]
    out: list[dict] = []
    cursor = ""
    for _ in range(max_pages):
        q = {"ticker": ticker, "limit": "1000"}
        if cursor:
            q["cursor"] = cursor
        url = f"{KALSHI_BASE}/markets/trades?" + urllib.parse.urlencode(q)
        data = _http_get(url)
        page = data.get("trades", []) or []
        out.extend(page)
        cursor = data.get("cursor", "") or ""
        if not cursor or not page:
            break
        time.sleep(HTTP_PACE_S)
    _TRADES_CACHE[ticker] = out
    return out


def _market_mid_at(trades: list[dict], target: datetime,
                   window_minutes: float = TRADES_WINDOW_MIN) -> Optional[float]:
    """VWAP yes_price across trades within ±window_minutes; nearest-trade
    fallback when window is empty."""
    if not trades:
        return None
    target_ts = target.timestamp()
    win_s = window_minutes * 60.0
    in_win: list[tuple[float, float]] = []
    nearest: Optional[tuple[float, float]] = None  # (dist, yes_price)
    for tr in trades:
        ct = tr.get("created_time")
        if not ct:
            continue
        try:
            tts = datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp()
            yp = float(tr.get("yes_price_dollars", "0") or 0)
            ct_fp = float(tr.get("count_fp", "0") or 0)
        except Exception:
            continue
        if yp <= 0 or yp >= 1:
            continue
        dist = abs(tts - target_ts)
        if dist <= win_s:
            in_win.append((yp, ct_fp))
        if nearest is None or dist < nearest[0]:
            nearest = (dist, yp)
    if in_win:
        num = sum(p * c for p, c in in_win)
        den = sum(c for _, c in in_win)
        if den > 0:
            return num / den
    return nearest[1] if nearest else None


# ── METAR running max ───────────────────────────────────────────────────

def _running_max_at(
    conn: sqlite3.Connection, station: str, lst_date: str, lst_hour: int,
) -> Optional[float]:
    """Max temp observed at this station on this LST date for hours
    ``[0, lst_hour]`` inclusive. None if no rows."""
    row = conn.execute(
        """SELECT MAX(temp_f) FROM weather_metar_hourly_backfill
           WHERE station = ? AND lst_date = ? AND lst_hour <= ?""",
        (station, lst_date, lst_hour),
    ).fetchone()
    if row and row[0] is not None:
        return float(row[0])
    return None


def _decision_to_lst(decision_dt: datetime, lst_offset: int) -> tuple[str, int]:
    """UTC datetime → (LST date string, LST hour)."""
    lst = decision_dt + timedelta(hours=lst_offset)
    return lst.strftime("%Y-%m-%d"), lst.hour


# ── Forecast loader (same source filtering as backfill_directional_shadow) ─

def _load_backfill_gaussians(
    conn: sqlite3.Connection, city: str, settle_date: str,
) -> list[GaussianForecast]:
    rows = conn.execute(
        """SELECT source, forecast_mean_f, forecast_sigma_f, lead_hours
           FROM weather_gaussian_snapshots_backfill
           WHERE city = ? AND settlement_date = ?
             AND source IN ('hrrr','nbm','open_meteo','weather')
             AND forecast_mean_f IS NOT NULL AND forecast_sigma_f IS NOT NULL""",
        (city, settle_date),
    ).fetchall()
    sources_present = {r[0] for r in rows}
    out: list[GaussianForecast] = []
    for source, mean_f, sigma_f, lead_h in rows:
        if source == "open_meteo" and "weather" in sources_present:
            continue
        name = "weather" if source == "open_meteo" else source
        try:
            g = GaussianForecast(
                mean_f=float(mean_f),
                sigma_f=float(sigma_f) if sigma_f and sigma_f > 0 else 2.0,
                horizon_hours=float(lead_h or 12.0),
                source_name=name,
                source_tag=f"{name}:{city}_{settle_date}_diag",
            )
        except ValueError:
            continue
        out.append(g)
    return out


# ── Combine + nowcast projection ────────────────────────────────────────

def _combine_for_city(
    gaussians: list[GaussianForecast], city_key: str,
) -> Optional[GaussianForecast]:
    """Apply per-city skill σ + MOS bias + group-discounted combine →
    single combined Gaussian."""
    corrected: list[GaussianForecast] = []
    for g in gaussians:
        try:
            g2 = v2._apply_learned_sigma(g, city_key=city_key)
        except Exception:
            g2 = g
        try:
            g2 = v2._apply_mos_bias(g2, city_key)
        except Exception:
            pass
        corrected.append(g2)
    weighted = v2._weighted_inputs_with_group_discount(corrected)
    from bot.signals.weather_forecast import combine_gaussian
    return combine_gaussian(weighted, combined_name="combined_v2")


def _nowcast(
    forecast_g: GaussianForecast, running_max: Optional[float],
    hours_to_close: float,
) -> GaussianForecast:
    """Build a nowcast Gaussian: shift mean up to running_max if the
    forecast is below it (running max is observed; daily high cannot be
    below it), and shrink σ linearly with hours-to-close."""
    new_mean = forecast_g.mean_f
    if running_max is not None and running_max > forecast_g.mean_f:
        # Forecast was already too low; reset mean to the observed floor
        # plus a small cushion for residual peak between now and close.
        new_mean = running_max + 0.5
    decay = max(0.1, min(1.0, hours_to_close / 12.0))
    new_sigma = forecast_g.sigma_f * decay
    return GaussianForecast(
        mean_f=new_mean,
        sigma_f=max(0.3, new_sigma),  # don't go below 0.3°F (METAR noise floor)
        horizon_hours=hours_to_close,
        source_name=forecast_g.source_name,
        source_tag=f"nowcast_{int(hours_to_close)}h",
    )


# ── Main loop ───────────────────────────────────────────────────────────

def run(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """SELECT family, ticker, won_yes, ts_settle_unix
           FROM alpha_backtest
           WHERE decision_type = 'directional_shadow_backfill'
             AND won_yes IS NOT NULL""",
    ).fetchall()
    print(f"[nowcast] loaded {len(rows)} settled backfill rows")

    # (family, horizon, "combined"|"market") → list of brier samples
    brier: dict[tuple[str, int, str], list[float]] = defaultdict(list)
    skipped = {"no_metar": 0, "no_forecast": 0, "no_market": 0,
               "no_combined": 0, "city_unknown": 0}
    n_processed = 0
    t0 = time.time()
    last_print = t0

    # Cache backfill gaussians per (city, settle_date)
    gauss_cache: dict[tuple[str, str], list[GaussianForecast]] = {}

    for family, ticker, won_yes, ts_settle in rows:
        if family not in FAMILY_TO_CITY:
            skipped["city_unknown"] += 1
            continue
        city = FAMILY_TO_CITY[family]
        if city not in CITY_TO_STATION:
            skipped["no_metar"] += 1
            continue
        station = CITY_TO_STATION[city]
        lst_offset = CITY_TO_LST_OFFSET[city]

        parts = ticker.split("-")
        if len(parts) < 2:
            continue
        date_suf = parts[1]
        try:
            yy = int(date_suf[:2])
            mon = date_suf[2:5]
            dd = int(date_suf[5:7])
            months = ["JAN","FEB","MAR","APR","MAY","JUN","JUL",
                      "AUG","SEP","OCT","NOV","DEC"]
            m_idx = months.index(mon.upper()) + 1
            settle_date = f"20{yy:02d}-{m_idx:02d}-{dd:02d}"
        except (ValueError, IndexError):
            continue

        gkey = (city, settle_date)
        if gkey not in gauss_cache:
            gauss_cache[gkey] = _load_backfill_gaussians(conn, city, settle_date)
        gaussians = gauss_cache[gkey]
        if not gaussians:
            skipped["no_forecast"] += 1
            continue

        market = _fetch_market(ticker)
        if not market:
            skipped["no_market"] += 1
            continue

        proj = v2._parse_market_for_projection(ticker, market)
        if proj is None:
            continue
        is_bracket, threshold_f, is_above, lo_f, hi_f = proj

        city_key = v2._city_key(city)
        combined = _combine_for_city(gaussians, city_key)
        if combined is None:
            skipped["no_combined"] += 1
            continue

        close_dt = datetime.fromtimestamp(ts_settle, tz=timezone.utc)
        trades = _fetch_trades(ticker)

        for h in HORIZONS:
            decision_dt = close_dt - timedelta(hours=h)
            lst_date, lst_hour = _decision_to_lst(decision_dt, lst_offset)
            running_max = _running_max_at(conn, station, lst_date, lst_hour)

            nc = _nowcast(combined, running_max, hours_to_close=h)
            try:
                p = probability_for_market(
                    nc, is_bracket=is_bracket, threshold_f=threshold_f,
                    is_above=is_above, bracket_lo_f=lo_f, bracket_hi_f=hi_f,
                )
            except Exception:
                continue
            brier[(family, h, "combined")].append((p - won_yes) ** 2)

            mkt = _market_mid_at(trades, decision_dt)
            if mkt is not None:
                brier[(family, h, "market")].append((mkt - won_yes) ** 2)

        n_processed += 1
        if time.time() - last_print > 10:
            elapsed = time.time() - t0
            rate = n_processed / max(elapsed, 1e-6)
            remaining = (len(rows) - n_processed) / max(rate, 1e-6)
            print(f"  ... {n_processed}/{len(rows)} "
                  f"({rate:.1f}/s, ~{remaining:.0f}s left)")
            last_print = time.time()

    print(f"[nowcast] processed={n_processed}  skipped={dict(skipped)}  "
          f"({time.time()-t0:.0f}s)")
    print()
    print("=" * 88)
    print("Multi-horizon nowcast Brier (vs market mid at the same horizon)")
    print("=" * 88)
    families = sorted({k[0] for k in brier.keys()})
    print(f"  {'family':<12}", end="")
    for h in HORIZONS:
        print(f"{f'  h={h}h_combined':>15}{f'  h={h}h_market':>14}{f'  Δ':>8}",
              end="")
    print()
    print("  " + "-" * (12 + len(HORIZONS) * (15 + 14 + 8)))
    for fam in families:
        print(f"  {fam:<12}", end="")
        for h in HORIZONS:
            cv = brier.get((fam, h, "combined"), [])
            mv = brier.get((fam, h, "market"), [])
            if not cv or not mv:
                print(f"{'-':>15}{'-':>14}{'-':>8}", end="")
                continue
            cb = statistics.mean(cv)
            mb = statistics.mean(mv)
            edge = mb - cb     # +ve = combined beats market
            print(f"{cb:>14.4f} {mb:>13.4f} {edge:>+7.4f}", end="")
        print()

    print()
    print("  Pooled across families:")
    for h in HORIZONS:
        cv = [v for (_f, hh, c), lst in brier.items()
              if hh == h and c == "combined" for v in lst]
        mv = [v for (_f, hh, c), lst in brier.items()
              if hh == h and c == "market" for v in lst]
        if cv and mv:
            edge = statistics.mean(mv) - statistics.mean(cv)
            print(f"    h={h:>2}h  combined={statistics.mean(cv):.4f}  "
                  f"market={statistics.mean(mv):.4f}  Δ={edge:+.4f}  "
                  f"n={len(cv)}")
    print()
    print("  Reading: Δ > 0 means combined nowcast beats market mid (alpha).")
    print("  At h=12h forecast carries most weight; at h=4h running METAR")
    print("  dominates and combined ≈ deterministic max — Brier ≈ 0 expected")
    print("  if our running-max math is right and the market converges similarly.")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=DB_PATH)
    args = p.parse_args()
    conn = init_db(args.db)
    run(conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
