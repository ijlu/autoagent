"""Weather MM markout analysis — v1 vs v2 alpha on market mid.

Primary go-live gate for ``WEATHER_ENSEMBLE_V2``. Rather than wait 2 weeks
for enough settled Kalshi weather markets to do settlement-Brier stats,
this tool uses the 20k+ rows/day of ``weather_mm_shadow`` and asks: for
each shadow row at time t, did the market mid move TOWARD our fair value
by time t+Δt?

  markout(Δt) = (mid_{t+Δt} − mid_t) × sign(fv − mid_t)

Positive → our FV had alpha over market consensus (market moved toward
us). Negative → adverse move (market went the other way; we'd have been
adversely selected).

v1 FV is re-derived from stored inputs (pure function of running_high,
forecast_high, hours_left, bracket/threshold). v2 FV is the stored
``fair_value_cents`` on rows written after WEATHER_ENSEMBLE_V2 was
enabled. For post-flip rows we get both, for pre-flip rows only v1.

Usage::

    python3 tools/weather_markout_analysis.py \\
        --db /home/kalshi/autoagent/kalshi_trades.db \\
        --since 2026-04-24T18:21:00 \\
        --deltas 300,900,3600 \\
        --max-spread-c 15
"""

from __future__ import annotations

import argparse
import math
import random
import re
import sqlite3
import statistics
import sys
from dataclasses import dataclass
from typing import Iterable, Optional

# Port v1 math rather than import — analysis tool should run even if the
# live quoter's FV math evolves. Pin the algorithm that wrote historical
# rows. These three functions are the verbatim v1 math from
# ``bot/daemon/weather_quoter.py`` as of 2026-04-24.


def _logistic_cdf(x: float, mu: float, sigma: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-(x - mu) / sigma))
    except OverflowError:
        return 0.0 if x < mu else 1.0


def _sigma_for_hours(hours_left: float) -> float:
    if hours_left <= 0:
        return 0.5
    elif hours_left < 1:
        return 1.0
    elif hours_left < 2:
        return 2.0
    elif hours_left < 4:
        return 3.5
    elif hours_left < 6:
        return 5.0
    elif hours_left < 12:
        return 6.5
    else:
        return 8.0


def _blended_mu(
    running_high_f: float, forecast_high_f: float, hours_left: float,
) -> float:
    if hours_left > 0:
        day_fraction_elapsed = max(0.0, min(1.0, 1.0 - hours_left / 24.0))
        forecast_weight = max(0.1, 1.0 - day_fraction_elapsed)
        obs_weight = 1.0 - forecast_weight
        return (
            forecast_weight * max(forecast_high_f, running_high_f)
            + obs_weight * running_high_f
        )
    return running_high_f


# ---------------------------------------------------------------------------
# Ticker parsing (weather-series conventions)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketSpec:
    """Parsed market bounds from the ticker alone."""
    is_bracket: bool
    is_above: Optional[bool]       # threshold markets only
    threshold: Optional[float]
    bracket_floor: Optional[float]
    bracket_cap: Optional[float]


_THRESHOLD_RE = re.compile(r"-T(-?\d+(?:\.\d+)?)$")
_BRACKET_RE = re.compile(r"-B(-?\d+(?:\.\d+)?)$")


def parse_ticker(ticker: str) -> Optional[MarketSpec]:
    """Parse weather-series ticker into bounds.

    Weather-series `-T<N>` markets are "daily high > N" by convention on
    the families we trade (KXHIGH*). `-B<N>` is a 2°F bracket starting
    at N. Direction for threshold markets is confirmed downstream by
    regressing stored v1 FV against the `is_above=True` hypothesis; any
    ticker where the residual is consistently large gets flagged.
    """
    m = _BRACKET_RE.search(ticker)
    if m:
        floor_val = float(m.group(1))
        return MarketSpec(
            is_bracket=True,
            is_above=None,
            threshold=None,
            bracket_floor=floor_val,
            bracket_cap=floor_val + 2.0,
        )
    m = _THRESHOLD_RE.search(ticker)
    if m:
        return MarketSpec(
            is_bracket=False,
            is_above=True,
            threshold=float(m.group(1)),
            bracket_floor=None,
            bracket_cap=None,
        )
    return None


# ---------------------------------------------------------------------------
# v1 fair-value reconstruction
# ---------------------------------------------------------------------------


def v1_fair_value_cents(
    spec: MarketSpec,
    running_high_f: float,
    forecast_high_f: float,
    hours_left: float,
) -> Optional[int]:
    """Re-derive the v1 quoter FV in cents from stored inputs.

    Mirrors ``WeatherQuoter._compute_fair_value`` for the v1-only branch.
    Returns None if the spec can't be projected (shouldn't happen for
    well-formed tickers).
    """
    mu = _blended_mu(running_high_f, forecast_high_f, hours_left)
    sigma = _sigma_for_hours(hours_left)

    if spec.is_bracket:
        assert spec.bracket_floor is not None and spec.bracket_cap is not None
        floor_val = spec.bracket_floor
        cap_val = spec.bracket_cap
        if running_high_f >= cap_val:
            prob = 0.02
        elif running_high_f >= floor_val:
            prob_below_cap = _logistic_cdf(cap_val, mu, sigma)
            prob = max(0.02, min(0.98, prob_below_cap))
        else:
            cdf_upper = _logistic_cdf(cap_val, mu, sigma)
            cdf_lower = _logistic_cdf(floor_val, mu, sigma)
            prob = max(0.02, min(0.98, cdf_upper - cdf_lower))
    else:
        assert spec.threshold is not None and spec.is_above is not None
        threshold = spec.threshold
        if running_high_f >= threshold:
            margin = running_high_f - threshold
            if margin >= 3.0:
                prob_above = 0.98
            elif margin >= 1.0:
                prob_above = 0.96
            else:
                prob_above = 0.95
        else:
            prob_above = 1.0 - _logistic_cdf(threshold, mu, sigma)
            prob_above = max(0.02, min(0.98, prob_above))
        prob = prob_above if spec.is_above else max(0.02, min(0.98, 1.0 - prob_above))

    return max(2, min(98, int(round(prob * 100))))


# ---------------------------------------------------------------------------
# Direction audit (pre-flip v1 FV residual)
# ---------------------------------------------------------------------------


def audit_threshold_directions(
    conn: sqlite3.Connection, pre_flip_iso: str,
) -> dict[str, bool]:
    """For every threshold ticker seen in the pre-flip shadow data, pick
    the direction (is_above True/False) whose v1 reconstruction matches
    the stored ``fair_value_cents`` more closely. Returns a
    {ticker: is_above} map. Callers use this to override the default
    `-T<N>` → is_above=True convention where needed.

    Tickers where both directions fit equally badly (residual > 10¢ on
    both) are dropped — likely a data issue or edge case we should
    skip rather than score wrong.
    """
    out: dict[str, bool] = {}
    rows = conn.execute(
        """SELECT ticker, running_high_f, forecast_high_f, hours_left,
                  fair_value_cents
             FROM weather_mm_shadow
            WHERE ts_iso < ?
              AND ticker LIKE '%-T%'
              AND running_high_f IS NOT NULL
              AND forecast_high_f IS NOT NULL
              AND hours_left IS NOT NULL
              AND fair_value_cents IS NOT NULL""",
        (pre_flip_iso,),
    ).fetchall()

    by_ticker: dict[str, list[tuple[float, float, float, int]]] = {}
    for tk, rh, fh, hl, fv in rows:
        by_ticker.setdefault(tk, []).append((rh, fh, hl, fv))

    for tk, samples in by_ticker.items():
        spec_true = parse_ticker(tk)
        if spec_true is None or spec_true.is_bracket:
            continue
        spec_false = MarketSpec(
            is_bracket=False, is_above=False,
            threshold=spec_true.threshold,
            bracket_floor=None, bracket_cap=None,
        )
        err_true = 0.0
        err_false = 0.0
        for rh, fh, hl, fv in samples:
            tv = v1_fair_value_cents(spec_true, rh, fh, hl)
            fv_false = v1_fair_value_cents(spec_false, rh, fh, hl)
            if tv is not None:
                err_true += abs(tv - fv)
            if fv_false is not None:
                err_false += abs(fv_false - fv)
        err_true /= len(samples)
        err_false /= len(samples)
        if min(err_true, err_false) > 10.0:
            continue  # data gremlin — skip
        out[tk] = err_true <= err_false
    return out


# ---------------------------------------------------------------------------
# Markout computation
# ---------------------------------------------------------------------------


@dataclass
class MarkoutSample:
    ticker: str
    series: str
    ts_unix: int
    hours_left: float
    market_spread_c: int
    mid_t: float
    mid_t_plus: float
    v1_fv: Optional[int]
    v2_fv: Optional[int]          # None on pre-flip rows
    v1_markout: Optional[float]
    v2_markout: Optional[float]


def _mid(bid: Optional[int], ask: Optional[int]) -> Optional[float]:
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2.0


def _signed_markout(fv: int, mid_t: float, mid_tp: float) -> float:
    """Cents the market moved in our direction. Positive = alpha."""
    return (mid_tp - mid_t) * (1.0 if fv > mid_t else (-1.0 if fv < mid_t else 0.0))


def compute_markouts(
    conn: sqlite3.Connection,
    since_iso: str,
    delta_seconds: int,
    max_spread_c: int,
    flip_iso: str,
    direction_overrides: dict[str, bool],
    hours_left_min: float = 0.0,
) -> list[MarkoutSample]:
    """For each shadow row at time t with a valid market mid and a
    same-ticker partner row at ts ≥ t+Δ (within 2Δ), emit a MarkoutSample
    with v1 + (where available) v2 markouts.

    ``flip_iso`` marks the WEATHER_ENSEMBLE_V2 cutover — rows before it
    stored v1 FV in ``fair_value_cents``; rows at/after it stored v2 FV.
    """
    # Pull everything we need in one pass. Order by (ticker, ts) so the
    # inner join-next can walk a sorted stream.
    rows = conn.execute(
        """SELECT id, ts_unix, ticker, series, running_high_f,
                  forecast_high_f, hours_left, fair_value_cents,
                  market_yes_bid, market_yes_ask
             FROM weather_mm_shadow
            WHERE ts_iso >= ?
              AND market_yes_bid IS NOT NULL
              AND market_yes_ask IS NOT NULL
              AND running_high_f IS NOT NULL
              AND forecast_high_f IS NOT NULL
              AND hours_left IS NOT NULL
              AND fair_value_cents IS NOT NULL
              AND hours_left >= ?
            ORDER BY ticker, ts_unix""",
        (since_iso, hours_left_min),
    ).fetchall()

    # Group consecutively by ticker for the next-row lookup.
    samples: list[MarkoutSample] = []
    i = 0
    n = len(rows)
    flip_unix = _iso_to_unix(flip_iso)
    window_max = 2 * delta_seconds

    while i < n:
        j = i
        while j < n and rows[j][2] == rows[i][2]:
            j += 1
        # rows[i:j] are same-ticker, sorted by ts_unix ascending.
        ticker_rows = rows[i:j]
        _emit_markouts_for_ticker(
            ticker_rows, delta_seconds, window_max, max_spread_c,
            flip_unix, direction_overrides, samples,
        )
        i = j
    return samples


def _emit_markouts_for_ticker(
    ticker_rows: list[tuple],
    delta_seconds: int,
    window_max: int,
    max_spread_c: int,
    flip_unix: int,
    direction_overrides: dict[str, bool],
    out: list[MarkoutSample],
) -> None:
    # Pre-parse market spec once per ticker; apply direction override.
    ticker = ticker_rows[0][2]
    spec = parse_ticker(ticker)
    if spec is None:
        return
    if not spec.is_bracket and ticker in direction_overrides:
        spec = MarketSpec(
            is_bracket=False,
            is_above=direction_overrides[ticker],
            threshold=spec.threshold,
            bracket_floor=None, bracket_cap=None,
        )

    # Two-pointer walk: for each row at index a, advance b to first row
    # at ts_a + Δ or later (and ≤ ts_a + 2Δ).
    b = 0
    for a in range(len(ticker_rows)):
        rid_a, ts_a, tk, series, rh, fh, hl, fv_stored, bid_a, ask_a = ticker_rows[a]
        spread = ask_a - bid_a
        if spread > max_spread_c:
            continue
        mid_a = _mid(bid_a, ask_a)
        if mid_a is None:
            continue

        while b < len(ticker_rows) and ticker_rows[b][1] < ts_a + delta_seconds:
            b += 1
        if b >= len(ticker_rows):
            break
        ts_b = ticker_rows[b][1]
        if ts_b - ts_a > window_max:
            continue
        bid_b, ask_b = ticker_rows[b][8], ticker_rows[b][9]
        if bid_b is None or ask_b is None:
            continue
        if (ask_b - bid_b) > max_spread_c:
            continue
        mid_b = _mid(bid_b, ask_b)
        if mid_b is None:
            continue

        v1 = v1_fair_value_cents(spec, rh, fh, hl)
        v2 = fv_stored if ts_a >= flip_unix else None
        if ts_a < flip_unix:
            stored_v1 = fv_stored  # cross-check
            if v1 is not None and stored_v1 is not None and abs(v1 - stored_v1) > 2:
                # Reconstruction is off by more than a cent — likely a
                # direction or bracket-bound parse issue. Skip rather than
                # score wrong.
                continue
        mk_v1 = _signed_markout(v1, mid_a, mid_b) if v1 is not None else None
        mk_v2 = _signed_markout(v2, mid_a, mid_b) if v2 is not None else None

        out.append(MarkoutSample(
            ticker=tk, series=series, ts_unix=ts_a, hours_left=hl,
            market_spread_c=spread, mid_t=mid_a, mid_t_plus=mid_b,
            v1_fv=v1, v2_fv=v2, v1_markout=mk_v1, v2_markout=mk_v2,
        ))


def _iso_to_unix(iso: str) -> int:
    from datetime import datetime, timezone
    s = iso.replace("Z", "+00:00")
    if "+" not in s and "T" in s:
        s += "+00:00"
    return int(datetime.fromisoformat(s).astimezone(timezone.utc).timestamp())


# ---------------------------------------------------------------------------
# Aggregation + bootstrap
# ---------------------------------------------------------------------------


def bootstrap_mean_ci(
    values: list[float], n_boot: int = 2000, alpha: float = 0.05,
    rng: Optional[random.Random] = None,
) -> tuple[float, float, float]:
    """Return (mean, lo, hi) for `alpha`-level CI of the mean. Standard
    percentile bootstrap — adequate for our sample sizes (hundreds to
    thousands), no heavy-tailed concerns on cents-per-row markouts."""
    if not values:
        return float("nan"), float("nan"), float("nan")
    rng = rng or random.Random(0xBACF)
    n = len(values)
    means = []
    for _ in range(n_boot):
        resample_sum = 0.0
        for _ in range(n):
            resample_sum += values[rng.randrange(n)]
        means.append(resample_sum / n)
    means.sort()
    lo = means[int(n_boot * alpha / 2)]
    hi = means[int(n_boot * (1 - alpha / 2))]
    return statistics.fmean(values), lo, hi


def aggregate(
    samples: list[MarkoutSample], field: str,
) -> dict[str, dict[str, list[float]]]:
    """Group markouts by the given row attr. Returns
    ``{group: {"v1": [...], "v2": [...]}}``."""
    out: dict[str, dict[str, list[float]]] = {}
    for s in samples:
        grp = getattr(s, field)
        b = out.setdefault(str(grp), {"v1": [], "v2": []})
        if s.v1_markout is not None:
            b["v1"].append(s.v1_markout)
        if s.v2_markout is not None:
            b["v2"].append(s.v2_markout)
    return out


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def render_report(
    samples_by_delta: dict[int, list[MarkoutSample]],
) -> str:
    """Render markdown report across the configured Δ lags."""
    lines = ["# Weather MM markout analysis", ""]
    for delta, samples in sorted(samples_by_delta.items()):
        lines.append(f"## Δ = {delta}s")
        lines.append("")
        lines.append(f"- Samples: {len(samples)}")
        v1 = [s.v1_markout for s in samples if s.v1_markout is not None]
        v2 = [s.v2_markout for s in samples if s.v2_markout is not None]
        if v1:
            m, lo, hi = bootstrap_mean_ci(v1)
            lines.append(f"- Overall **v1** mean markout: "
                         f"{m:+.3f}¢  (95% CI [{lo:+.3f}, {hi:+.3f}], n={len(v1)})")
        if v2:
            m, lo, hi = bootstrap_mean_ci(v2)
            lines.append(f"- Overall **v2** mean markout: "
                         f"{m:+.3f}¢  (95% CI [{lo:+.3f}, {hi:+.3f}], n={len(v2)})")
        lines.append("")
        # Per-family table
        by_fam = aggregate(samples, "series")
        if by_fam:
            lines.append("| family | v1 n | v1 mean¢ | v1 95% CI | v2 n | v2 mean¢ | v2 95% CI |")
            lines.append("|---|---|---|---|---|---|---|")
            for fam in sorted(by_fam):
                b = by_fam[fam]
                v1m, v1lo, v1hi = bootstrap_mean_ci(b["v1"]) if b["v1"] else (float("nan"),) * 3
                v2m, v2lo, v2hi = bootstrap_mean_ci(b["v2"]) if b["v2"] else (float("nan"),) * 3
                lines.append(
                    f"| {fam} | {len(b['v1'])} | {v1m:+.2f} | [{v1lo:+.2f}, {v1hi:+.2f}] | "
                    f"{len(b['v2'])} | {v2m:+.2f} | [{v2lo:+.2f}, {v2hi:+.2f}] |"
                )
            lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True, help="SQLite DB path")
    p.add_argument(
        "--since", default=None,
        help="ISO-8601 lower bound on ts_iso (defaults to 48h before now)",
    )
    p.add_argument(
        "--flip", default="2026-04-24T18:21:00+00:00",
        help="ISO-8601 cutover when WEATHER_ENSEMBLE_V2 was enabled",
    )
    p.add_argument(
        "--deltas", default="300,900,3600",
        help="Comma-separated markout lags in seconds",
    )
    p.add_argument(
        "--max-spread-c", type=int, default=15,
        help="Drop rows where market bid-ask spread > this (cents)",
    )
    p.add_argument(
        "--hours-left-min", type=float, default=0.0,
        help="Drop rows with hours_left below this (filters settlement pinning)",
    )
    p.add_argument(
        "--out", default=None,
        help="Markdown report output path (stdout if omitted)",
    )
    args = p.parse_args(argv)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    if args.since:
        since = args.since
    else:
        from datetime import datetime, timedelta, timezone
        since = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()

    print(f"[markout] audit threshold directions pre-flip={args.flip}", file=sys.stderr)
    dir_overrides = audit_threshold_directions(conn, args.flip)
    n_overridden = sum(1 for v in dir_overrides.values() if not v)
    print(f"[markout]   {len(dir_overrides)} tickers scanned, "
          f"{n_overridden} overridden to is_above=False", file=sys.stderr)

    deltas = [int(s) for s in args.deltas.split(",") if s.strip()]
    samples_by_delta: dict[int, list[MarkoutSample]] = {}
    for d in deltas:
        print(f"[markout] Δ={d}s since={since}", file=sys.stderr)
        s = compute_markouts(
            conn, since, d, args.max_spread_c, args.flip, dir_overrides,
            hours_left_min=args.hours_left_min,
        )
        samples_by_delta[d] = s
        print(f"[markout]   {len(s)} markout samples (Δ={d}s)", file=sys.stderr)

    report = render_report(samples_by_delta)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(report)
        print(f"[markout] wrote {args.out}", file=sys.stderr)
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
