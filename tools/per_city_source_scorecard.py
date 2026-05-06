"""Per-city per-source ensemble scorecard.

Generates ``reports/SCORECARD_<CITY>_<DATE>.md`` from ``weather_forecast_snapshots``
joined to ground-truth daily highs in ``weather_metar_hourly_backfill``,
and per-source longer-history backfill in ``weather_gaussian_snapshots_backfill``.

Sections (per [reports/ENSEMBLE_DEEP_DIVE_2026-05-05.md]):
    1. TL;DR
    2. Per-source bias by LST hour (and day-offset)
    3. Per-source RMSE / claimed-σ ratio by LST hour
    4. Per-source skill curve by lead time
    5. Within-group correlation matrix at peak window
    6. METAR signal value by LST hour
    7. Empirical diurnal-phase boundaries for this city
    8. Recommended per-LST-phase config (concrete dicts)
    9. Backtest comparison (filled in after Phase 4)

Usage::

    PYTHONPATH=. python3 tools/per_city_source_scorecard.py \\
        --city nyc \\
        --db scratch/weather_analysis.db \\
        --out reports/SCORECARD_NY_2026-05-05.md

Read-only against the DB.
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, ".")

from bot.daemon.stations import station_for_city
from tools.lst_align import (
    DEFAULT_PHASE_BOUNDARIES,
    diurnal_phase,
    lst_date,
    lst_hour,
)


# Buckets for the bias / RMSE tables. LST hours are grouped into 3-hour
# bins so per-cell n is large enough to be meaningful with ~10 days of
# live data.
LST_HOUR_BINS: tuple[tuple[str, int, int], ...] = (
    ("00-02", 0, 3),
    ("03-05", 3, 6),
    ("06-08", 6, 9),
    ("09-11", 9, 12),
    ("12-14", 12, 15),
    ("15-17", 15, 18),
    ("18-20", 18, 21),
    ("21-23", 21, 24),
)

# Lead-time buckets (hours_out from snapshot row).
LEAD_HOUR_BINS: tuple[tuple[str, int, int], ...] = (
    ("0-3",   0, 4),
    ("4-7",   4, 8),
    ("8-12",  8, 13),
    ("13-18", 13, 19),
    ("19-24", 19, 25),
    ("25-36", 25, 37),
    ("37+",   37, 999),
)


def lst_hour_bin(h: int) -> str:
    for label, lo, hi in LST_HOUR_BINS:
        if lo <= h < hi:
            return label
    return "?"


def lead_hour_bin(h: Optional[int]) -> str:
    if h is None:
        return "?"
    for label, lo, hi in LEAD_HOUR_BINS:
        if lo <= h < hi:
            return label
    return "?"


# ─────────────────────────────────────────────────────────────────────────────
# Data loading
# ─────────────────────────────────────────────────────────────────────────────

_NON_TEMPERATURE_SOURCES = frozenset({"afd_bias"})


def load_snapshots_with_truth(
    conn: sqlite3.Connection,
    series: str,
    icao: str,
    lst_offset: int,
    since_iso: Optional[str] = None,
) -> list[dict]:
    """Return every snapshot row joined to its target-day daily high.

    The target LST date for a ticker like ``KXHIGHNY-26MAY04-B72.5`` is
    extracted from the ticker (``2026-05-04``). The observed high comes
    from ``weather_metar_hourly_backfill.daily_high_f`` for the city's
    primary station on that LST date.

    Skips ``afd_bias`` and other non-temperature sources whose
    ``forecast_high_f`` is a delta or score, not a predicted high (would
    pollute bias/RMSE analyses with -64°F nonsense).
    """
    skip_list = ",".join(f"'{s}'" for s in _NON_TEMPERATURE_SOURCES)
    since_clause = " AND s.recorded_at >= ?" if since_iso else ""
    sql = f"""
        SELECT
            s.recorded_at,
            s.ticker,
            s.source,
            s.forecast_high_f,
            s.sigma_f,
            s.hours_out
        FROM weather_forecast_snapshots s
        WHERE s.ticker LIKE ?
          AND s.forecast_high_f IS NOT NULL
          AND s.source NOT IN ({skip_list})
          {since_clause}
    """
    params: tuple = (f"{series}-%",) if not since_iso else (f"{series}-%", since_iso)
    rows = conn.execute(sql, params).fetchall()

    # Pull all daily highs for this station, into a dict
    truth: dict[str, float] = {}
    for r in conn.execute(
        "SELECT lst_date, MAX(daily_high_f) FROM weather_metar_hourly_backfill "
        "WHERE station=? GROUP BY lst_date",
        (icao,),
    ):
        if r[1] is not None:
            truth[r[0]] = float(r[1])

    out: list[dict] = []
    for recorded_at, ticker, source, fc_high, sigma, hours_out in rows:
        target_lst = _target_lst_date_from_ticker(ticker)
        if target_lst is None or target_lst not in truth:
            continue
        ts = _parse_iso(recorded_at)
        if ts is None:
            continue
        rec_hour = lst_hour(ts, lst_offset=lst_offset)
        rec_date = lst_date(ts, lst_offset=lst_offset)
        day_off = _date_diff_days(target_lst, rec_date)
        out.append({
            "recorded_at": recorded_at,
            "ts_unix": ts,
            "ticker": ticker,
            "source": source,
            "forecast_high_f": float(fc_high),
            "sigma_f": float(sigma) if sigma is not None else None,
            "hours_out": int(hours_out) if hours_out is not None else None,
            "observed_high_f": truth[target_lst],
            "lst_hour": rec_hour,
            "lst_date_recording": rec_date,
            "lst_date_target": target_lst,
            "day_offset": day_off,
            "residual": float(fc_high) - truth[target_lst],
        })
    return out


def load_metar_observations(
    conn: sqlite3.Connection,
    icao: str,
) -> list[dict]:
    """Hourly METAR rows with daily_high tagged. Used for METAR signal-value
    analysis and empirical diurnal-peak detection.
    """
    rows = conn.execute(
        "SELECT lst_date, lst_hour, temp_f, daily_high_f "
        "FROM weather_metar_hourly_backfill WHERE station=? "
        "ORDER BY lst_date, lst_hour",
        (icao,),
    ).fetchall()
    out = []
    for lst_date_str, lst_hr, temp_f, daily_hi in rows:
        if temp_f is None or daily_hi is None:
            continue
        out.append({
            "lst_date": lst_date_str,
            "lst_hour": int(lst_hr),
            "temp_f": float(temp_f),
            "daily_high_f": float(daily_hi),
        })
    return out


def _target_lst_date_from_ticker(ticker: str) -> Optional[str]:
    """Parse ``KXHIGHNY-26MAY04-B72.5`` → ``2026-05-04``."""
    parts = ticker.split("-")
    if len(parts) < 3:
        return None
    raw = parts[1]  # e.g. 26MAY04
    if len(raw) != 7:
        return None
    try:
        yr = 2000 + int(raw[:2])
        mon_str = raw[2:5]
        day = int(raw[5:7])
        months = {
            "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
            "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
        }
        mon = months[mon_str.upper()]
        return f"{yr:04d}-{mon:02d}-{day:02d}"
    except (ValueError, KeyError):
        return None


def _parse_iso(s: str) -> Optional[float]:
    """Parse ISO timestamp to unix seconds. Tolerates trailing 'Z' or
    fractional seconds."""
    if not s:
        return None
    try:
        # Python 3.11+ fromisoformat handles Z and microseconds directly.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _date_diff_days(later: str, earlier: str) -> int:
    a = datetime.strptime(later, "%Y-%m-%d")
    b = datetime.strptime(earlier, "%Y-%m-%d")
    return (a - b).days


# ─────────────────────────────────────────────────────────────────────────────
# Aggregations
# ─────────────────────────────────────────────────────────────────────────────

def per_source_lst_table(rows: list[dict], day_offset: int = 0) -> dict[str, dict[str, dict]]:
    """For a given day_offset (default same-day), return
    ``{source: {lst_bin: {n, bias, rmse, claimed_sigma_avg, ratio}}}``.

    bias = mean(forecast - observed)
    rmse = sqrt(mean(residual^2))
    claimed_sigma_avg = mean(sigma_f) if reported
    ratio = rmse / claimed_sigma_avg (the σ inflation factor needed)
    """
    grouped: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r["day_offset"] != day_offset:
            continue
        bin_label = lst_hour_bin(r["lst_hour"])
        grouped[r["source"]][bin_label].append(r)

    out: dict[str, dict[str, dict]] = {}
    for source, by_bin in grouped.items():
        out[source] = {}
        for bin_label, items in by_bin.items():
            residuals = [r["residual"] for r in items]
            sigmas = [r["sigma_f"] for r in items if r["sigma_f"] is not None]
            n = len(items)
            bias = sum(residuals) / n
            rmse = math.sqrt(sum(x * x for x in residuals) / n)
            avg_sigma = (sum(sigmas) / len(sigmas)) if sigmas else None
            ratio = (rmse / avg_sigma) if avg_sigma and avg_sigma > 0 else None
            out[source][bin_label] = {
                "n": n,
                "bias": bias,
                "rmse": rmse,
                "avg_sigma": avg_sigma,
                "ratio": ratio,
            }
    return out


def per_source_lead_table(rows: list[dict]) -> dict[str, dict[str, dict]]:
    """Per-source RMSE/bias bucketed by hours_out (lead time)."""
    grouped: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        bin_label = lead_hour_bin(r["hours_out"])
        grouped[r["source"]][bin_label].append(r)

    out: dict[str, dict[str, dict]] = {}
    for source, by_bin in grouped.items():
        out[source] = {}
        for bin_label, items in by_bin.items():
            residuals = [r["residual"] for r in items]
            n = len(items)
            bias = sum(residuals) / n
            rmse = math.sqrt(sum(x * x for x in residuals) / n)
            out[source][bin_label] = {
                "n": n,
                "bias": bias,
                "rmse": rmse,
            }
    return out


def within_group_correlation(rows: list[dict], lst_phase: str = "peak_window") -> dict:
    """Pairwise residual correlation between sources within ``lst_phase``.

    Returns ``{(src_a, src_b): {n, corr}}`` for src_a < src_b alphabetically.

    Two snapshots are paired if they have the same (target_lst_date,
    rounded recorded_at hour). Coarse alignment to maximize overlap.
    """
    in_phase = [
        r for r in rows
        if diurnal_phase(r["lst_hour"]) == lst_phase
        and r["day_offset"] == 0  # same-day forecasts only
    ]

    # Index by (target_lst, hour-of-day-bucket, source) → list of residuals
    by_key: dict[tuple[str, int], dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for r in in_phase:
        # Round recorded_at to the hour for pairing
        hour_bucket = r["lst_hour"]
        key = (r["lst_date_target"], hour_bucket)
        by_key[key][r["source"]].append(r["residual"])

    # For each pair (a, b), compute correlation across keys where both exist
    sources = sorted({r["source"] for r in in_phase})
    out: dict[tuple[str, str], dict] = {}
    for i, a in enumerate(sources):
        for b in sources[i + 1:]:
            xs, ys = [], []
            for key, srcs in by_key.items():
                if a in srcs and b in srcs:
                    xs.append(statistics.mean(srcs[a]))
                    ys.append(statistics.mean(srcs[b]))
            if len(xs) < 5:
                continue
            corr = _pearson(xs, ys)
            out[(a, b)] = {"n": len(xs), "corr": corr}
    return out


def _pearson(xs: list[float], ys: list[float]) -> float:
    if len(xs) != len(ys) or len(xs) < 2:
        return 0.0
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return num / (dx * dy) if dx > 0 and dy > 0 else 0.0


def metar_signal_by_hour(metar_rows: list[dict]) -> dict[int, dict]:
    """For each LST hour: distribution of (running_metar_max_at_hour - daily_high_field).

    NOTE: ``daily_high_f`` in ``weather_metar_hourly_backfill`` is the
    NWS "X-high" daily field (the official observed high). The hourly
    ``temp_f`` rows are spot METAR readings — the actual peak can fall
    between hourly observations and be missed. So a gap of -1°F at 23:00
    LST does NOT necessarily mean the model is wrong; it can mean the
    METAR reading-cycle missed the peak. The 80%-locked threshold for
    ``first_post_peak_hour`` should be interpreted as "by this hour, the
    METAR-observed running max equals or exceeds the official daily high
    on 80% of days" — useful but not a perfect proxy.
    """
    # Compute running max per (lst_date, lst_hour) for each day
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in metar_rows:
        by_day[r["lst_date"]].append(r)

    # Aggregate: per LST hour, list of (running_max - daily_high) values
    by_hour: dict[int, list[float]] = defaultdict(list)
    for day, recs in by_day.items():
        recs_sorted = sorted(recs, key=lambda x: x["lst_hour"])
        running_max = -1e9
        daily_hi = recs_sorted[0]["daily_high_f"]
        for r in recs_sorted:
            running_max = max(running_max, r["temp_f"])
            by_hour[r["lst_hour"]].append(running_max - daily_hi)

    out: dict[int, dict] = {}
    for hour, gaps in sorted(by_hour.items()):
        n = len(gaps)
        if n < 5:
            continue
        gaps_sorted = sorted(gaps)
        median = gaps_sorted[n // 2]
        # Frac of days where running_max == daily_high (gap == 0) at this hour
        frac_locked = sum(1 for g in gaps if g >= -0.5) / n
        # Also: frac where running_max within 1°F of daily_high (more lenient,
        # better captures "high is essentially set" given METAR sample-rate issues).
        frac_within_1f = sum(1 for g in gaps if g >= -1.0) / n
        out[hour] = {
            "n": n,
            "median_gap": median,
            "p10_gap": gaps_sorted[int(n * 0.1)],
            "p90_gap": gaps_sorted[int(n * 0.9)],
            "frac_at_high": frac_locked,
            "frac_within_1f": frac_within_1f,
        }
    return out


def empirical_phase_boundaries(metar_rows: list[dict]) -> dict[str, int]:
    """Find per-city diurnal phase boundaries from METAR.

    Returns dict with:
      - peak_hour_median: LST hour at which the daily high typically lands
      - first_post_peak_hour: smallest LST hour where ≥80% of days have
        running_max == daily_high (high is "locked")
    """
    metar_signal = metar_signal_by_hour(metar_rows)
    if not metar_signal:
        return {}

    # peak_hour_median = mode of the LST hour where the daily high occurs
    hour_high_count: dict[int, int] = defaultdict(int)
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in metar_rows:
        by_day[r["lst_date"]].append(r)
    for day, recs in by_day.items():
        if not recs:
            continue
        max_temp = max(r["temp_f"] for r in recs)
        # Could be hit at multiple hours; count first occurrence
        for r in sorted(recs, key=lambda x: x["lst_hour"]):
            if r["temp_f"] >= max_temp - 0.1:
                hour_high_count[r["lst_hour"]] += 1
                break

    if not hour_high_count:
        return {}
    peak_hour = max(hour_high_count.items(), key=lambda kv: kv[1])[0]

    # Use the more-lenient "within 1°F of daily high" since METAR hourly
    # sample-rate can miss the actual peak by 0.5-2°F.
    first_post_peak = None
    for hour in sorted(metar_signal):
        if metar_signal[hour].get("frac_within_1f", 0) >= 0.80:
            first_post_peak = hour
            break

    return {
        "peak_hour_median": peak_hour,
        "first_post_peak_hour": first_post_peak if first_post_peak is not None else -1,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Markdown rendering
# ─────────────────────────────────────────────────────────────────────────────

def render_table(headers: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |"]
    out.append("|" + "|".join("---" for _ in headers) + "|")
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def fmt_num(x: Optional[float], digits: int = 2) -> str:
    if x is None:
        return "—"
    if isinstance(x, int):
        return str(x)
    return f"{x:.{digits}f}"


def render_per_source_lst_section(
    table: dict[str, dict[str, dict]],
    metric: str,
    title: str,
    digits: int = 2,
) -> str:
    """Render a source × lst_bin table for one metric ('bias', 'rmse', 'ratio')."""
    bin_labels = [b[0] for b in LST_HOUR_BINS]
    sources = sorted(table.keys())

    out = [f"### {title}\n"]
    headers = ["source"] + bin_labels + ["n_total"]
    rows = []
    for src in sources:
        row = [src]
        n_total = 0
        for label in bin_labels:
            cell = table.get(src, {}).get(label)
            if cell is None:
                row.append("—")
            else:
                row.append(fmt_num(cell.get(metric), digits))
                n_total += cell["n"]
        row.append(n_total)
        rows.append(row)
    out.append(render_table(headers, rows))
    return "\n".join(out)


def render_per_source_lead_section(table: dict[str, dict[str, dict]]) -> str:
    bin_labels = [b[0] for b in LEAD_HOUR_BINS]
    sources = sorted(table.keys())

    out = ["### 4. Per-source RMSE by lead time (hours_out)\n"]
    headers = ["source"] + bin_labels + ["n_total"]
    rows = []
    for src in sources:
        row = [src]
        n_total = 0
        for label in bin_labels:
            cell = table.get(src, {}).get(label)
            if cell is None:
                row.append("—")
            else:
                row.append(fmt_num(cell.get("rmse")))
                n_total += cell["n"]
        row.append(n_total)
        rows.append(row)
    out.append(render_table(headers, rows))
    return "\n".join(out)


def render_correlation_section(corr: dict) -> str:
    out = ["### 5. Within-group residual correlation (peak_window, same-day)\n"]
    if not corr:
        out.append("_Insufficient overlap to compute correlations (try wider window)._")
        return "\n".join(out)
    rows = sorted(corr.items(), key=lambda kv: -abs(kv[1]["corr"]))
    out.append(render_table(
        ["source A", "source B", "n", "corr"],
        [[a, b, c["n"], fmt_num(c["corr"], 3)] for (a, b), c in rows],
    ))
    out.append("")
    out.append("_corr ≥ 0.7 = effectively redundant; treat as one source._")
    return "\n".join(out)


def render_metar_signal_section(sig: dict[int, dict]) -> str:
    out = ["### 6. METAR signal value by LST hour\n"]
    out.append(
        "Distribution of (running_max_at_hour − daily_high). Negative = "
        "running_max still climbing. ~0 = high reached. `frac_at_high` = "
        "fraction of days where running_max ≥ daily_high − 0.5°F at that "
        "hour."
    )
    rows = []
    for hour in sorted(sig):
        d = sig[hour]
        rows.append([
            hour,
            d["n"],
            fmt_num(d["median_gap"]),
            fmt_num(d["p10_gap"]),
            fmt_num(d["p90_gap"]),
            fmt_num(d.get("frac_at_high"), 2),
            fmt_num(d.get("frac_within_1f"), 2),
        ])
    out.append(render_table(
        ["lst_hour", "n", "median_gap_F", "p10", "p90", "frac_at_high", "frac_within_1F"],
        rows,
    ))
    out.append("")
    out.append(
        "_Note: `daily_high_f` is the official NWS daily field. Hourly METAR can"
        " miss the actual peak by 0.5-2°F due to reporting cadence; `frac_within_1F`"
        " is the more reliable 'high-is-set' indicator._"
    )
    return "\n".join(out)


def render_phase_section(phase: dict) -> str:
    out = ["### 7. Empirical diurnal-phase boundaries\n"]
    if not phase:
        out.append("_No phase data available._")
        return "\n".join(out)
    out.append(
        f"- **Peak hour (LST, mode):** {phase.get('peak_hour_median', '?')}\n"
        f"- **First post-peak hour (LST, ≥80% days locked):** "
        f"{phase.get('first_post_peak_hour', '?')}"
    )
    return "\n".join(out)


def render_recommendation_section(
    bias_table: dict, ratio_table: dict, corr: dict, phase: dict
) -> str:
    """Concrete heuristics to seed Phase 3 redesign. Hand-tunable later."""
    out = ["### 8. Recommended config (auto-derived heuristics)\n"]
    out.append(
        "_These are starting points. Phase 3 should review, not blindly accept._\n"
    )
    # Identify biased sources (|bias| > 1.5°F at peak window)
    biased = []
    for src, by_bin in bias_table.items():
        peak_cells = [by_bin.get(lab) for lab in ("12-14", "15-17") if by_bin.get(lab)]
        if not peak_cells:
            continue
        avg_bias = sum(c["bias"] for c in peak_cells) / len(peak_cells)
        if abs(avg_bias) > 1.5:
            biased.append((src, avg_bias))

    if biased:
        out.append("**Sources biased >1.5°F at peak window (consider exclusion or correction):**")
        for src, b in sorted(biased, key=lambda x: -abs(x[1])):
            out.append(f"- `{src}`: bias = {b:+.2f}°F")
        out.append("")

    # Sources with under-calibrated σ (ratio > 1.5)
    under_sigma = []
    for src, by_bin in ratio_table.items():
        for label, c in by_bin.items():
            if c.get("ratio") is not None and c["ratio"] > 1.5:
                under_sigma.append((src, label, c["ratio"]))
    if under_sigma:
        out.append(f"**Source × LST-bin pairs with σ-ratio > 1.5 (need σ inflation):**")
        for src, label, r in sorted(under_sigma, key=lambda x: -x[2])[:20]:
            out.append(f"- `{src}` LST {label}: realized RMSE / claimed σ = {r:.2f}")
        out.append("")

    # Highly correlated source pairs
    redundant = [(a, b, c["corr"]) for (a, b), c in corr.items() if c["corr"] > 0.7]
    if redundant:
        out.append("**Highly-correlated source pairs (n_eff < n):**")
        for a, b, r in sorted(redundant, key=lambda x: -x[2]):
            out.append(f"- `{a}` ↔ `{b}`: corr = {r:.3f}")
        out.append("")

    # LST gate suggestion
    if phase.get("first_post_peak_hour", -1) > 0:
        out.append(
            f"**Cross-bracket LST gate suggestion:** fire only when "
            f"recording_lst_hour ≥ {phase['first_post_peak_hour']} AND "
            f"day_offset == 0."
        )
    return "\n".join(out)


def render_tldr(
    n_snapshots: int,
    n_settled_days: int,
    n_sources: int,
    bias_table: dict,
    ratio_table: dict,
    phase: dict,
) -> str:
    out = ["## 1. TL;DR\n"]
    out.append(f"- **Sample:** {n_snapshots:,} snapshots, {n_settled_days} settled "
               f"days, {n_sources} sources.")
    out.append(f"- **Empirical peak hour:** LST {phase.get('peak_hour_median', '?')}; "
               f"running-high locks (≥80% days) by LST {phase.get('first_post_peak_hour', '?')}.")

    # Top bias offenders
    biased = []
    for src, by_bin in bias_table.items():
        peak_cells = [by_bin.get(lab) for lab in ("12-14", "15-17") if by_bin.get(lab)]
        if peak_cells:
            avg = sum(c["bias"] for c in peak_cells) / len(peak_cells)
            biased.append((src, avg))
    biased.sort(key=lambda x: -abs(x[1]))
    if biased[:3]:
        offenders = ", ".join(f"{s} ({b:+.1f}°F)" for s, b in biased[:3])
        out.append(f"- **Biggest peak-window bias offenders:** {offenders}.")

    # Worst σ underestimation
    worst_ratio = []
    for src, by_bin in ratio_table.items():
        for label, c in by_bin.items():
            if c.get("ratio") is not None:
                worst_ratio.append((src, label, c["ratio"]))
    worst_ratio.sort(key=lambda x: -x[2])
    if worst_ratio[:3]:
        out.append("- **Worst σ underestimation (RMSE / claimed σ):**")
        for src, label, r in worst_ratio[:3]:
            out.append(f"  - `{src}` LST {label}: ratio = {r:.2f}")

    return "\n".join(out)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def generate_scorecard(city: str, db_path: str, out_path: str, since_iso: Optional[str] = None) -> None:
    station = station_for_city(city)
    if station is None:
        sys.exit(f"unknown city: {city}")

    conn = sqlite3.connect(db_path)
    rows = load_snapshots_with_truth(
        conn, station.series, station.icao, station.lst_offset,
        since_iso=since_iso,
    )
    metar_rows = load_metar_observations(conn, station.icao)

    if not rows:
        sys.exit(f"no snapshots found for {station.series}")

    # Aggregations
    bias_table_d0 = per_source_lst_table(rows, day_offset=0)
    bias_table_d1 = per_source_lst_table(rows, day_offset=1)
    lead_table = per_source_lead_table(rows)
    corr = within_group_correlation(rows, lst_phase="peak_window")
    metar_sig = metar_signal_by_hour(metar_rows)
    phase = empirical_phase_boundaries(metar_rows)

    # Render
    sources = sorted({r["source"] for r in rows})
    n_settled_days = len({r["lst_date_target"] for r in rows})

    md_parts = [
        f"# Per-source ensemble scorecard — {station.city.upper()} ({station.icao})\n",
        f"**Series:** `{station.series}`  ",
        f"**LST offset:** {station.lst_offset:+d}h  ",
        f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}  ",
        f"**DB:** `{db_path}`\n",
        render_tldr(
            len(rows), n_settled_days, len(sources),
            bias_table_d0, {}, phase,
        ),
        "\n## 2. Per-source bias (forecast − observed) by LST hour, same-day\n",
        render_per_source_lst_section(
            bias_table_d0, "bias",
            "2a. Same-day (day_offset = 0)",
            digits=2,
        ),
        "\n",
        render_per_source_lst_section(
            bias_table_d1, "bias",
            "2b. Day-before (day_offset = 1)",
            digits=2,
        ),
        "\n## 3. Per-source RMSE / claimed-σ ratio by LST hour, same-day\n",
        "_Ratio > 1 means model's σ is too tight (under-calibrated)._\n",
        render_per_source_lst_section(
            bias_table_d0, "ratio",
            "3a. Realized RMSE / claimed σ — same-day",
            digits=2,
        ),
        "\n",
        render_per_source_lst_section(
            bias_table_d0, "rmse",
            "3b. Realized RMSE (°F) — same-day, for context",
            digits=2,
        ),
        "\n",
        render_per_source_lead_section(lead_table),
        "\n",
        render_correlation_section(corr),
        "\n",
        render_metar_signal_section(metar_sig),
        "\n",
        render_phase_section(phase),
        "\n",
        render_recommendation_section(bias_table_d0, bias_table_d0, corr, phase),
        "\n## 9. Backtest comparison (Phase 4)\n",
        "_Filled in after Phase 3 redesign + Phase 4 backtest._\n",
    ]

    Path(out_path).write_text("\n".join(md_parts))
    print(f"Wrote {out_path}")
    print(f"  {len(rows):,} snapshots, {n_settled_days} settled days, {len(sources)} sources")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--city", required=True)
    p.add_argument("--db", default="scratch/weather_analysis.db")
    p.add_argument("--out", required=True)
    p.add_argument("--since", default=None,
                   help="ISO timestamp; only snapshots with recorded_at >= this are included")
    args = p.parse_args()
    generate_scorecard(args.city, args.db, args.out, since_iso=args.since)
