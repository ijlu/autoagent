"""Phase 3c — would post-fix forecasts have avoided the 5 cross-bracket losses?

Reads post-fix (recorded_at >= 2026-05-04, after commit 8d043a8) combined_v2
forecasts from ``weather_forecast_snapshots`` for each of the 5 settled
losing tickers. For each cycle in the LST gate window, runs the
production scorer to see what the cross-bracket leg would have decided
given the corrected forecast.

Output: per-loss breakdown showing whether the strategy would have
(a) skipped, (b) bought the winning side, or (c) still bought the
losing side. Aggregate verdict on whether the lat/lon fix is sufficient.

This is the read-only counterfactual analysis Josh asked for in §6Q2 of
[reports/POSTFIX_REASSESSMENT_2026-05-05.md].

Usage::
    PYTHONPATH=. python3 tools/backtest_5_losses_postfix.py \\
        --db scratch/weather_analysis.db
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from typing import Optional

sys.path.insert(0, ".")

from bot.scoring.bracket_portfolio import _decide_leg  # production logic
from bot.learning.cross_bracket_lst_gate import DEFAULT_LST_GATE_BY_SERIES
from bot.daemon.stations import station_for_ticker
from tools.lst_align import lst_hour, lst_date


# The 5 settled positions from the morning audit (settlement_result column
# from alpha_backtest verified). All settled YES except NY-B61.5.
LOSSES = [
    {
        "ticker": "KXHIGHNY-26MAY03-B59.5",
        "actual_settled_yes": True,   # high WAS in [59, 61)
        "audit_action": "bought NO",
        "audit_pnl_per_contract": "loss",
        "note": "PRE-fix only — May 3 settle, fix shipped May 3 evening",
    },
    {
        "ticker": "KXHIGHNY-26MAY03-B61.5",
        "actual_settled_yes": False,  # high NOT in [61, 63)
        "audit_action": "bought NO",
        "audit_pnl_per_contract": "win",
        "note": "PRE-fix only",
    },
    {
        "ticker": "KXHIGHNY-26MAY04-B72.5",
        "actual_settled_yes": True,
        "audit_action": "bought NO",
        "audit_pnl_per_contract": "loss",
        "note": "MIXED — late post-fix data exists",
    },
    {
        "ticker": "KXHIGHAUS-26MAY04-B82.5",
        "actual_settled_yes": True,
        "audit_action": "bought NO",
        "audit_pnl_per_contract": "loss",
        "note": "MIXED — late post-fix data exists",
    },
    {
        "ticker": "KXHIGHLAX-26MAY04-B68.5",
        "actual_settled_yes": True,
        "audit_action": "bought NO",
        "audit_pnl_per_contract": "loss",
        "note": "MIXED — late post-fix data exists",
    },
]

# Post-fix cutoff
POSTFIX_CUTOFF_ISO = "2026-05-04T03:39:00+00:00"  # ~commit 8d043a8 + 24h cache stale

# Production scorer config
MIN_EDGE = 0.07
MIN_PRICE = 5
MAX_PRICE = 95


def _parse_bracket_bounds(ticker: str) -> tuple[float, float]:
    """KXHIGHNY-26MAY04-B72.5 → (72.0, 74.0)."""
    parts = ticker.rsplit("-B", 1)
    val = float(parts[1])
    return (val - 0.5, val + 1.5)


def _target_lst_date_from_ticker_simple(ticker: str) -> str:
    """KXHIGHNY-26MAY04-B72.5 → '2026-05-04'."""
    raw = ticker.split("-")[1]
    months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
              "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
    yr = 2000 + int(raw[:2])
    mon = months[raw[2:5]]
    day = int(raw[5:7])
    return f"{yr:04d}-{mon:02d}-{day:02d}"


def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _project_p_yes(mu: float, sigma: float, lo: float, hi: float) -> float:
    if sigma <= 0:
        return 1.0 if lo <= mu <= hi else 0.0
    z_hi = (hi - mu) / sigma
    z_lo = (lo - mu) / sigma
    return max(0.005, min(0.995, _ncdf(z_hi) - _ncdf(z_lo)))


def _shadow_quotes_for(conn: sqlite3.Connection, ticker: str) -> list[dict]:
    """Pull market yes_bid/yes_ask snapshots over time for ``ticker``.

    Prefer ``weather_mm_shadow`` if available (continuous quote stream).
    Fall back to ``alpha_backtest`` (per-decision quotes) which is
    sparser but always has both sides.
    """
    quotes: list[dict] = []
    try:
        rows = conn.execute(
            """SELECT ts_unix, market_yes_bid, market_yes_ask
               FROM weather_mm_shadow
               WHERE ticker = ? AND market_yes_bid IS NOT NULL
                     AND market_yes_ask IS NOT NULL
               ORDER BY ts_unix""",
            (ticker,),
        ).fetchall()
        quotes = [
            {"ts_unix": float(r[0]), "yes_bid": int(r[1]), "yes_ask": int(r[2])}
            for r in rows
        ]
    except sqlite3.OperationalError:
        pass

    if quotes:
        return quotes

    # Fallback: alpha_backtest. ts_decision_unix + yes_bid_cents / yes_ask_cents
    rows = conn.execute(
        """SELECT ts_decision_unix, yes_bid_cents, yes_ask_cents
           FROM alpha_backtest
           WHERE ticker = ?
             AND yes_bid_cents IS NOT NULL
             AND yes_ask_cents IS NOT NULL
           ORDER BY ts_decision_unix""",
        (ticker,),
    ).fetchall()
    return [
        {"ts_unix": float(r[0]), "yes_bid": int(r[1]), "yes_ask": int(r[2])}
        for r in rows
    ]


def _nearest_quote(quotes: list[dict], ts_unix: float, tol_s: int = 1800) -> Optional[dict]:
    if not quotes:
        return None
    closest = min(quotes, key=lambda q: abs(q["ts_unix"] - ts_unix))
    if abs(closest["ts_unix"] - ts_unix) > tol_s:
        return None
    return closest


def _parse_iso(s: str) -> Optional[float]:
    from datetime import datetime, timezone
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def analyze_loss(
    conn: sqlite3.Connection, loss: dict, *, fast_path: bool = False,
) -> dict:
    """Replay cross-bracket scorer against post-fix forecasts for one loss.

    When ``fast_path=True``, simulate the METAR post-peak override:
    if recording's LST hour ≥ peak_hour + 2 on the target settle day AND
    we have a post-fix METAR snapshot, replace combined (μ, σ) with
    (METAR.mean, 1.0°F) before projecting onto the bracket.
    """
    ticker = loss["ticker"]
    lo, hi = _parse_bracket_bounds(ticker)
    settled_yes = loss["actual_settled_yes"]

    station = station_for_ticker(ticker)
    if station is None:
        return {"ticker": ticker, "error": "no_station"}
    series = station.series
    lst_offset = station.lst_offset
    gate = DEFAULT_LST_GATE_BY_SERIES.get(series, (15, 23))

    # Per-city stability rule (Phase 3e — replaced fixed peak+2 buffer)
    from bot.learning.cross_bracket_lst_gate import is_post_peak_safe
    fast_path_sigma = 1.0  # _METAR_POST_PEAK_SIGMA_F

    # Build day-long METAR running max trace (LST hour → max raw temp_f).
    # MUST use raw hourly METAR (weather_metar_hourly_backfill.temp_f),
    # NOT metar source's forecast_high_f from snapshots — those are
    # diurnal-projected predictions which can be ABOVE the actual running
    # max (overshoots), or below it (re-projects from current temp).
    # Production's stability detector uses METAR poller's running_high_f,
    # which is raw max(temp_f). Match it here.
    metar_by_lst_hour: dict[int, float] = {}
    if fast_path:
        target_lst_date = _target_lst_date_from_ticker_simple(ticker)
        for hourly_temp_f, hourly_lst_hour in conn.execute(
            """SELECT temp_f, lst_hour
               FROM weather_metar_hourly_backfill
               WHERE station = ? AND lst_date = ?
                 AND temp_f IS NOT NULL
               ORDER BY lst_hour""",
            (station.icao, target_lst_date),
        ):
            h = int(hourly_lst_hour)
            t = float(hourly_temp_f)
            metar_by_lst_hour[h] = max(metar_by_lst_hour.get(h, t), t)

    # Pull post-fix combined_v2 snapshots for this ticker
    snapshots = conn.execute(
        """SELECT recorded_at, forecast_high_f, sigma_f
           FROM weather_forecast_snapshots
           WHERE ticker = ?
             AND source = 'combined_v2'
             AND recorded_at >= ?
             AND forecast_high_f IS NOT NULL
             AND sigma_f IS NOT NULL
           ORDER BY recorded_at""",
        (ticker, POSTFIX_CUTOFF_ISO),
    ).fetchall()
    if not snapshots:
        return {
            "ticker": ticker,
            "error": "no_postfix_combined_v2_snapshots",
            "audit_action": loss["audit_action"],
            "audit_pnl": loss["audit_pnl_per_contract"],
            "note": loss["note"],
        }

    quotes = _shadow_quotes_for(conn, ticker)

    decisions_in_gate = []
    for recorded_at, mu, sigma in snapshots:
        ts = _parse_iso(recorded_at)
        if ts is None:
            continue
        rec_lst_hour = lst_hour(ts, lst_offset=lst_offset)
        rec_lst_date = lst_date(ts, lst_offset=lst_offset)
        # LST gate: only count cycles in the per-series window AND on the
        # correct LST date (settlement-day).
        target_date = ticker.split("-")[1]  # e.g. "26MAY04"
        # Convert ticker date 26MAY04 → 2026-05-04 (cf. lst_align)
        months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
                  "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
        try:
            yr = 2000 + int(target_date[:2])
            mon = months[target_date[2:5]]
            day = int(target_date[5:7])
            target_lst_date = f"{yr:04d}-{mon:02d}-{day:02d}"
        except (ValueError, KeyError):
            continue
        if rec_lst_date != target_lst_date:
            continue
        if not (gate[0] <= rec_lst_hour <= gate[1]):
            continue

        # 2026-05-05 (Phase 3e): cross-bracket gate uses the SAME
        # is_post_peak_safe condition as the fast-path. If fast-path
        # can't arm, cross-bracket shouldn't fire (would use wide-σ
        # combined → wrong-side trades). Mirror cross_bracket_shadow's
        # _is_in_lst_gate logic here.
        if fast_path and metar_by_lst_hour:
            running_max_check = -1e9
            last_inc_check = -1
            for h in range(rec_lst_hour + 1):
                if h in metar_by_lst_hour:
                    if metar_by_lst_hour[h] > running_max_check + 0.1:
                        running_max_check = metar_by_lst_hour[h]
                        last_inc_check = h
                    elif metar_by_lst_hour[h] > running_max_check:
                        running_max_check = metar_by_lst_hour[h]
            if last_inc_check < 0:
                # No METAR data — skip
                continue
            stability_check = max(0, rec_lst_hour - last_inc_check)
            if not is_post_peak_safe(series, rec_lst_hour, stability_check):
                # Cross-bracket gate blocks this cycle
                continue

        # Fast-path override: if enabled, compute "stability" from the
        # day's METAR running max trace, then check is_post_peak_safe.
        # The "running max" must monotonically increase across the day
        # (matches production METAR poller's StationState.running_high_f).
        # The metar source's snapshot mean_f can re-project downward
        # late in the day if the diurnal fit decides current temp is
        # the basis — the poller's running_high never does that.
        eff_mu, eff_sigma = float(mu), float(sigma)
        fast_path_fired = False
        if fast_path and metar_by_lst_hour:
            # True running max (monotonic): max-so-far across all hours seen
            running_max = -1e9
            last_increase = -1
            for h in range(rec_lst_hour + 1):
                if h in metar_by_lst_hour:
                    candidate = metar_by_lst_hour[h]
                    if candidate > running_max + 0.1:
                        running_max = candidate
                        last_increase = h
                    elif candidate > running_max:
                        # Tiny increase — track new max but don't reset clock
                        running_max = candidate
            if last_increase >= 0:
                stability = max(0, rec_lst_hour - last_increase)
                if is_post_peak_safe(series, rec_lst_hour, stability):
                    eff_mu = running_max
                    eff_sigma = fast_path_sigma
                    fast_path_fired = True

        p_yes = _project_p_yes(eff_mu, eff_sigma, lo, hi)

        # Find a market quote near this snapshot
        q = _nearest_quote(quotes, ts)
        if q is None:
            continue

        action, side, price, skip_reason = _decide_leg(
            p_yes, q["yes_bid"], q["yes_ask"],
            min_edge=MIN_EDGE,
            min_price_cents=MIN_PRICE,
            max_price_cents=MAX_PRICE,
        )

        # What's the win/loss outcome of this hypothetical trade?
        if action == "skip":
            outcome = "skip"
            pnl = 0
        elif (action == "buy_yes" and settled_yes) or \
             (action == "buy_no" and not settled_yes):
            outcome = "win"
            pnl = (100 - price) if price else 0
        else:
            outcome = "loss"
            pnl = -price if price else 0

        decisions_in_gate.append({
            "ts": recorded_at,
            "lst_hour": rec_lst_hour,
            "mu": eff_mu,
            "sigma": eff_sigma,
            "fast_path_fired": fast_path_fired,
            "p_yes": p_yes,
            "yes_bid": q["yes_bid"],
            "yes_ask": q["yes_ask"],
            "action": action,
            "side": side,
            "price": price,
            "skip_reason": skip_reason,
            "outcome": outcome,
            "pnl_cents": pnl,
        })

    # Verdict: most frequent action in the LST window
    if not decisions_in_gate:
        verdict = "no_postfix_decisions_in_gate"
    else:
        actions = [d["action"] for d in decisions_in_gate]
        outcomes = [d["outcome"] for d in decisions_in_gate]
        n_skip = actions.count("skip")
        n_yes = actions.count("buy_yes")
        n_no = actions.count("buy_no")
        n_win = outcomes.count("win")
        n_loss = outcomes.count("loss")
        verdict = (
            f"{len(decisions_in_gate)} cycles in gate: "
            f"skip={n_skip} buy_yes={n_yes} buy_no={n_no} | "
            f"hypothetical: win={n_win} loss={n_loss}"
        )

    return {
        "ticker": ticker,
        "audit_action": loss["audit_action"],
        "audit_pnl": loss["audit_pnl_per_contract"],
        "note": loss["note"],
        "lst_gate": gate,
        "actual_settled_yes": settled_yes,
        "n_postfix_combined_snapshots": len(snapshots),
        "n_decisions_in_gate": len(decisions_in_gate),
        "verdict": verdict,
        "decisions": decisions_in_gate[:5],  # Sample for inspection
    }


def main(db_path: str, *, fast_path: bool = False) -> None:
    conn = sqlite3.connect(db_path)
    label = "WITH METAR fast-path" if fast_path else "WITHOUT fast-path (Phase 3c baseline)"
    print(f"Phase 3c counterfactual ({label})\n")
    print(f"Post-fix cutoff: {POSTFIX_CUTOFF_ISO}")
    print(f"Production scorer config: min_edge={MIN_EDGE}, "
          f"min_price={MIN_PRICE}, max_price={MAX_PRICE}\n")
    print("=" * 78)

    summary = []
    for loss in LOSSES:
        result = analyze_loss(conn, loss, fast_path=fast_path)
        print(f"\n## {loss['ticker']}")
        print(f"  Note: {result.get('note')}")
        print(f"  Audit: {result.get('audit_action')} → {result.get('audit_pnl')}")
        if "error" in result:
            print(f"  ERROR: {result['error']}")
            continue
        print(f"  Settled YES: {result['actual_settled_yes']}")
        print(f"  LST gate: {result['lst_gate']}")
        print(f"  Post-fix combined_v2 snapshots: {result['n_postfix_combined_snapshots']}")
        print(f"  Decisions in LST gate: {result['n_decisions_in_gate']}")
        print(f"  Verdict: {result['verdict']}")
        if result.get("decisions"):
            print(f"  Sample decisions in LST gate:")
            for d in result["decisions"]:
                fp_tag = " [FP]" if d.get("fast_path_fired") else ""
                print(f"    {d['ts'][:19]} LST={d['lst_hour']:02d}{fp_tag} "
                      f"mu={d['mu']:.1f} σ={d['sigma']:.2f} "
                      f"p_yes={d['p_yes']:.3f} mkt={d['yes_bid']}/{d['yes_ask']} "
                      f"→ {d['action']} [{d['outcome']}{(' '+str(d['pnl_cents'])+'¢') if d.get('pnl_cents') else ''}]")
        summary.append((loss["ticker"], result.get("verdict")))

    print("\n" + "=" * 78)
    print(f"SUMMARY ({label})")
    for ticker, verdict in summary:
        print(f"  {ticker}: {verdict}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="scratch/weather_analysis.db")
    p.add_argument("--fast-path", action="store_true",
                   help="Apply METAR post-peak override (Phase 3d)")
    args = p.parse_args()
    main(args.db, fast_path=args.fast_path)
