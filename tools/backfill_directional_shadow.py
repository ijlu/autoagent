"""Backfill historical directional-shadow rows from Kalshi trades + persisted forecasts.

Why: ``alpha_backtest`` only logs decisions made by the live daemon. The
fresh table holds a few hours of natural accumulation — far short of the
N≥30 settled rows per family we need to claim "ensemble beats market mid"
on directional. Rather than wait three weeks of forward accumulation, we
synthesize historical rows by pairing:

  1. Kalshi public ``/markets/trades`` tape (no-auth) — gives us the
     market mid at any past timestamp.
  2. ``weather_gaussian_snapshots_backfill`` — gives us the historical
     per-source forecast Gaussians for each (city, settlement_date) on
     91 days of data.
  3. The live ``predict_v2`` flow — applies the just-persisted MOS bias
     + skill-σ overrides to those Gaussians and projects them onto the
     bracket. The combined number is exactly what the live ensemble
     would have said on that day.
  4. Observed daily high from ``weather_metar_hourly_backfill`` (or
     METAR daily column) — gives us the ground truth ``won_yes``.

Output: rows in ``alpha_backtest`` with
``decision_type='directional_shadow_backfill'``. The same Brier-comparison
queries we use on live shadow rows then work over the synthetic ones.

Re-runnable: rows are uniquely keyed on (ticker, ts_decision_unix); a
re-run on the same date range INSERT OR IGNOREs.

Limitations:
  * No HRRR/NBM grib pulls — we rely on what's already in the backfill
    table. Sources missing from a given (city, date) are simply absent
    from the combine, same as a flaky live source would be.
  * Decision_ts is fixed at close_time - 4h. We could stratify by
    horizon (12h, 24h) later; one decision per market is enough for an
    initial Brier comparison.
  * AFD bias is skipped — backfill has no AFD data and AFD is a small
    secondary signal anyway.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional

from bot.config import DB_PATH
from bot.daemon.stations import STATION_BY_CITY
from bot.db import init_db
from bot.signals import weather_ensemble_v2 as v2
from bot.signals.weather_forecast import GaussianForecast


KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
# Backfill forecasts are stored at lead_hours≈12 (snapshot taken ~12h before
# settlement). Daily-high markets close at ~04:59Z the day AFTER LST settlement
# date, so close_time - 18h ≈ noon UTC on the settlement day, which is roughly
# when the 12h-lead forecast was made and BEFORE intra-day METAR observations
# narrow market mid to certainty. Comparing ensemble to market mid at that
# point is the apples-to-apples directional alpha test.
DECISION_LEAD_HOURS = 18.0
TRADES_WINDOW_MINUTES = 90.0
HTTP_PACE_S = 0.3  # ~3 req/s, well under Kalshi's public rate limit


# Family → city map (lowercase city key matching weather_gaussian_snapshots_backfill).
# Kept short — extending coverage is trivial once weather_sources land NCO/HOU/etc.
FAMILY_TO_CITY = {
    "KXHIGHNY":  "nyc",
    "KXHIGHCHI": "chicago",
    "KXHIGHMIA": "miami",
    "KXHIGHLAX": "los angeles",
    "KXHIGHAUS": "austin",
    "KXHIGHDEN": "denver",
}


def _http_get(url: str, retries: int = 3) -> dict:
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "kalshi-bot-backfill/1.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(0.5 * (2 ** i))
    return {}


def _list_markets_for_event(event_ticker: str) -> list[dict]:
    out: list[dict] = []
    cursor = ""
    while True:
        q = {"event_ticker": event_ticker, "limit": "100"}
        if cursor:
            q["cursor"] = cursor
        url = f"{KALSHI_BASE}/markets?" + urllib.parse.urlencode(q)
        data = _http_get(url)
        page = data.get("markets", []) or []
        out.extend(page)
        cursor = data.get("cursor", "") or ""
        if not cursor or not page:
            break
        time.sleep(HTTP_PACE_S)
    return out


def _list_trades(ticker: str, limit: int = 1000) -> list[dict]:
    """All trades for a ticker (newest-first, paginated)."""
    out: list[dict] = []
    cursor = ""
    for _ in range(20):
        q = {"ticker": ticker, "limit": str(limit)}
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
    return out


def _market_prob_at(trades: list[dict], target_ts: datetime,
                    window_minutes: float = TRADES_WINDOW_MINUTES) -> Optional[float]:
    """VWAP of YES price across trades within ±window_minutes of target_ts.

    Falls back to the single nearest trade if the window is empty (so we
    don't drop sparse-volume markets entirely)."""
    if not trades:
        return None
    target = target_ts.timestamp()
    win_s = window_minutes * 60.0
    in_win: list[tuple[float, float]] = []  # (yes_price, count)
    nearest: Optional[tuple[float, float, float]] = None  # (dist, yes_price, count)
    for tr in trades:
        ct = tr.get("created_time")
        if not ct:
            continue
        try:
            tts = datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        try:
            yp = float(tr.get("yes_price_dollars", "0") or 0)  # already in dollars
            ct_fp = float(tr.get("count_fp", "0") or 0)
        except Exception:
            continue
        if yp <= 0 or yp >= 1:
            continue
        dist = abs(tts - target)
        if dist <= win_s:
            in_win.append((yp, ct_fp))
        if nearest is None or dist < nearest[0]:
            nearest = (dist, yp, ct_fp)
    if in_win:
        num = sum(p * c for p, c in in_win)
        den = sum(c for _, c in in_win)
        if den > 0:
            return num / den
    if nearest is not None:
        return nearest[1]
    return None


def _load_backfill_gaussians(conn: sqlite3.Connection, city: str,
                             settlement_date: str) -> list[GaussianForecast]:
    """Pull forecast Gaussians for (city, settlement_date) from backfill.

    Excludes:
      * METAR — backfill rows store the settlement-day observed high as
        ``forecast_mean_f`` with σ≈0.1, which is pure data leakage.
        Live METAR is real-time intra-day obs, but we don't have that
        timestream historically.
      * MADIS / NWS_POINT / TOMORROW — backfill rows are tiny (n≈12 each)
        and would spike the combine with non-canonical σ.
      * ``open_meteo`` source rows when a ``weather`` row exists for the
        same (city, date) — they're the same provider under two tags;
        keeping both double-counts Open-Meteo."""
    rows = conn.execute(
        """SELECT source, forecast_mean_f, forecast_sigma_f, lead_hours
           FROM weather_gaussian_snapshots_backfill
           WHERE city = ? AND settlement_date = ?
             AND source IN ('hrrr','nbm','open_meteo','weather')
             AND forecast_mean_f IS NOT NULL AND forecast_sigma_f IS NOT NULL""",
        (city, settlement_date),
    ).fetchall()
    sources_present = {r[0] for r in rows}
    out: list[GaussianForecast] = []
    for source, mean_f, sigma_f, lead_h in rows:
        # Drop the duplicate Open-Meteo row when the canonical 'weather' tag exists.
        if source == "open_meteo" and "weather" in sources_present:
            continue
        name = "weather" if source == "open_meteo" else source
        try:
            g = GaussianForecast(
                mean_f=float(mean_f),
                sigma_f=float(sigma_f) if sigma_f and sigma_f > 0 else 2.0,
                horizon_hours=float(lead_h or 12.0),
                source_name=name,
                source_tag=f"{name}:{city}_{settlement_date}_backfill",
            )
        except ValueError:
            continue
        out.append(g)
    return out


def _observed_high(conn: sqlite3.Connection, city: str,
                   settlement_date: str) -> Optional[float]:
    """Observed daily high (°F) from METAR backfill row."""
    row = conn.execute(
        """SELECT observed_high_f
           FROM weather_gaussian_snapshots_backfill
           WHERE city = ? AND settlement_date = ? AND source = 'metar'
           LIMIT 1""",
        (city, settlement_date),
    ).fetchone()
    if row and row[0] is not None:
        return float(row[0])
    return None


def _won_yes_for_market(market: dict, observed_high_f: Optional[float]) -> Optional[int]:
    """Resolve YES outcome from market.result if available, else from
    observed_high_f vs the bracket bounds."""
    result = (market.get("result") or "").lower()
    if result in ("yes",):
        return 1
    if result in ("no",):
        return 0
    if observed_high_f is None:
        return None
    floor_strike = market.get("floor_strike")
    cap_strike = market.get("cap_strike")
    if floor_strike is not None and cap_strike is not None:
        try:
            lo = float(floor_strike)
            hi = float(cap_strike)
        except (TypeError, ValueError):
            return None
        return 1 if (lo <= observed_high_f <= hi) else 0
    # Threshold market — title parsing is fragile; rely on Kalshi result.
    return None


def _ensemble_predict(ticker: str, market: dict,
                      gaussians: list[GaussianForecast]) -> tuple[Optional[float], int]:
    """Run predict_v2 with the supplied Gaussians.

    Returns (ensemble_p_yes, source_count). Achieved by monkey-patching
    ``_collect_gaussians`` for the duration of the call so all the v2
    plumbing (MOS bias from kv, skill σ from kv, group discount, AFD
    shift skip, σ inflation no-op, projection) runs unchanged."""
    if not gaussians:
        return None, 0

    saved = v2._collect_gaussians
    v2._collect_gaussians = lambda *_a, **_kw: gaussians
    try:
        prob, _tag = v2.predict_v2(ticker, market)
    except Exception as e:
        print(f"[backfill] {ticker} predict_v2 raised: {type(e).__name__}: {e}")
        prob = None
    finally:
        v2._collect_gaussians = saved
    return prob, len(gaussians)


def _settlement_date_from_event(event_ticker: str) -> Optional[str]:
    """Parse 'KXHIGHNY-26APR23' → '2026-04-23' (LST)."""
    parts = event_ticker.split("-")
    if len(parts) < 2:
        return None
    suffix = parts[-1]  # '26APR23'
    if len(suffix) < 7:
        return None
    try:
        yy = int(suffix[:2])
        mon = suffix[2:5]
        dd = int(suffix[5:7])
        m_idx = MONTHS.index(mon.upper()) + 1
        return f"20{yy:02d}-{m_idx:02d}-{dd:02d}"
    except (ValueError, IndexError):
        return None


def _enumerate_event_tickers(family: str, days_back: int) -> list[str]:
    out: list[str] = []
    today = datetime.now(timezone.utc).date()
    for d in range(1, days_back + 1):
        dt = today - timedelta(days=d)
        suffix = f"{dt.strftime('%y').upper()}{MONTHS[dt.month-1]}{dt.day:02d}"
        out.append(f"{family}-{suffix}")
    return out


def _insert_row(conn: sqlite3.Connection, row: dict) -> int:
    """INSERT OR IGNORE into alpha_backtest. Returns 1 if inserted."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO alpha_backtest
           (ts_decision, ts_decision_unix, ticker, family, decision_type,
            decision_outcome, side, price_cents, contracts, skip_reason,
            ensemble_p_yes, ensemble_confidence, source_count, sources_json,
            yes_bid_cents, yes_ask_cents, market_prob_yes, market_prob_source,
            ts_settle, ts_settle_unix, settlement_result, won_yes,
            cycle_id, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                   ?, ?, ?, ?, ?, ?)""",
        (
            row["ts_decision"], row["ts_decision_unix"], row["ticker"],
            row["family"], row["decision_type"], row["decision_outcome"],
            None, None, None, None,
            row["ensemble_p_yes"], None, row["source_count"], None,
            None, None, row["market_prob_yes"], "kalshi_trades_vwap",
            row["ts_settle"], row["ts_settle_unix"],
            row["settlement_result"], row["won_yes"],
            "backfill", "synthesized from /trades + backfill forecast",
        ),
    )
    return cur.rowcount


def backfill(conn: sqlite3.Connection, families: list[str],
             days_back: int = 30, dry_run: bool = False) -> dict:
    stats = {"events": 0, "events_with_markets": 0, "markets": 0,
             "rows_inserted": 0, "rows_skipped": 0,
             "no_forecast": 0, "no_market_price": 0, "no_outcome": 0}
    for family in families:
        if family not in FAMILY_TO_CITY:
            print(f"[backfill] unknown family {family} — skipping")
            continue
        city = FAMILY_TO_CITY[family]
        for event_ticker in _enumerate_event_tickers(family, days_back):
            stats["events"] += 1
            settlement_date = _settlement_date_from_event(event_ticker)
            if settlement_date is None:
                continue
            print(f"[backfill] {event_ticker} → city={city} settle={settlement_date}")
            try:
                markets = _list_markets_for_event(event_ticker)
            except Exception as e:
                print(f"[backfill]   list_markets failed: {type(e).__name__}: {e}")
                continue
            if not markets:
                continue
            stats["events_with_markets"] += 1

            gaussians = _load_backfill_gaussians(conn, city, settlement_date)
            if not gaussians:
                stats["no_forecast"] += 1
                print(f"[backfill]   no backfill forecast for {city}/{settlement_date}")
                continue
            observed_high = _observed_high(conn, city, settlement_date)

            for m in markets:
                stats["markets"] += 1
                ticker = m.get("ticker")
                close_time_str = m.get("close_time")
                if not ticker or not close_time_str:
                    continue
                try:
                    close_dt = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
                except Exception:
                    continue
                decision_dt = close_dt - timedelta(hours=DECISION_LEAD_HOURS)

                try:
                    trades = _list_trades(ticker)
                except Exception as e:
                    print(f"[backfill]   list_trades({ticker}) failed: {e}")
                    continue
                market_prob = _market_prob_at(trades, decision_dt)
                if market_prob is None:
                    stats["no_market_price"] += 1
                    continue

                ensemble_p, n_sources = _ensemble_predict(ticker, m, gaussians)
                if ensemble_p is None:
                    continue

                won_yes = _won_yes_for_market(m, observed_high)
                if won_yes is None:
                    stats["no_outcome"] += 1
                    continue

                row = {
                    "ts_decision": decision_dt.isoformat(),
                    "ts_decision_unix": decision_dt.timestamp(),
                    "ticker": ticker,
                    "family": family,
                    "decision_type": "directional_shadow_backfill",
                    "decision_outcome": "synthesized",
                    "ensemble_p_yes": float(ensemble_p),
                    "source_count": int(n_sources),
                    "market_prob_yes": float(market_prob),
                    "ts_settle": close_time_str,
                    "ts_settle_unix": close_dt.timestamp(),
                    "settlement_result": "yes" if won_yes == 1 else "no",
                    "won_yes": int(won_yes),
                }
                if dry_run:
                    print(f"[backfill]   DRY {ticker} ens={ensemble_p:.3f} "
                          f"mkt={market_prob:.3f} won={won_yes}")
                    continue
                from bot.db import db_write_ctx
                with db_write_ctx(conn):
                    stats["rows_inserted"] += _insert_row(conn, row)
    return stats


def report(conn: sqlite3.Connection) -> None:
    print("\nDirectional shadow backfill — Brier comparison vs market mid:")
    print("=" * 76)
    print(f"{'family':<12} {'n':>4} {'mkt_brier':>10} {'ens_brier':>10} "
          f"{'edge':>8} {'win%':>6}")
    print("-" * 76)
    rows = conn.execute(
        """SELECT family,
                  COUNT(*) AS n,
                  AVG((market_prob_yes - won_yes)*(market_prob_yes - won_yes))
                    AS mkt_brier,
                  AVG((ensemble_p_yes - won_yes)*(ensemble_p_yes - won_yes))
                    AS ens_brier,
                  AVG(won_yes) AS win_rate
           FROM alpha_backtest
           WHERE decision_type = 'directional_shadow_backfill'
             AND won_yes IS NOT NULL
           GROUP BY family
           HAVING COUNT(*) >= 5
           ORDER BY n DESC"""
    ).fetchall()
    for fam, n, mb, eb, wr in rows:
        edge = (mb or 0) - (eb or 0)
        print(f"{fam:<12} {n:>4} {mb:>10.4f} {eb:>10.4f} {edge:>+8.4f} "
              f"{wr*100:>5.1f}%")
    if not rows:
        print("(no rows yet — run without --report-only first)")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=DB_PATH)
    p.add_argument("--families", default=",".join(FAMILY_TO_CITY.keys()),
                   help="Comma-separated families to backfill")
    p.add_argument("--days-back", type=int, default=30)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--report-only", action="store_true")
    args = p.parse_args()

    conn = init_db(args.db)
    if args.report_only:
        report(conn)
        return 0

    families = [f.strip().upper() for f in args.families.split(",") if f.strip()]
    stats = backfill(conn, families, days_back=args.days_back, dry_run=args.dry_run)
    print(f"\n[backfill] stats: {stats}")
    report(conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
