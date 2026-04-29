"""Phase A.1 — pull extended ASOS regime features for the 6 cities.

Goal: gather hourly observations enriched with wind direction, sky cover,
dewpoint, and wind speed for ~30 days × 6 stations, joined with CF6 daily
TMAX (the corrected ground truth from commit 8eac0cf). Writes one CSV per
station to ``reports/regime_features/<station>.csv`` for offline analysis.

This is exploratory — no production schema change. We're testing whether
residual peak σ stratifies cleanly by regime features (wind direction →
sea breeze, sky cover → radiative regime, etc.) before deciding whether
the production fitter should grow a regime dimension.

Per-row schema (CSV header):
    station, lst_date, lst_hour, utc_ts,
    tmpf, dwpf, drct, sknt, skyc1,
    dewpoint_depression_f,
    daily_high_cf6_f,
    running_max_tmpf_at_hour_f,
    residual_peak_f

Where:
    dewpoint_depression_f = tmpf - dwpf (humidity proxy; smaller = more humid)
    running_max_tmpf_at_hour_f = max tmpf in raw obs at-or-before this LST hour
    residual_peak_f = daily_high_cf6_f - running_max_tmpf_at_hour_f
        (= "how much hotter does it still get after this hour" — what the
         METAR residual σ fitter is supposed to capture)

Usage::

    python -m tools.regime_features_pull --days 30
    python -m tools.regime_features_pull --days 30 --stations KMIA,KLAX

Defaults to all 6 cities in bot.daemon.stations._REGISTRY and a 30-day
window ending today. Total rows ~= 6 × 30 × 24 = 4,320 cells.
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

# Reuse station catalog + CF6 fetch from existing modules.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot.daemon.stations import _REGISTRY, WeatherStation  # noqa: E402
from tools.backfill_weather_effective_n import fetch_cf6_tmax  # noqa: E402


_IEM_ASOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
_USER_AGENT = "kalshi-research/regime-features 1.0 (joshlu@a16z.com)"
# IEM is polite — we respect it. ~1s between station fetches is plenty.
_INTER_STATION_SLEEP_S = 1.0


@dataclass
class _RawRow:
    utc_ts: datetime
    lst_date: str
    lst_hour: int
    tmpf: float
    dwpf: Optional[float]
    drct: Optional[float]
    sknt: Optional[float]
    skyc1: Optional[str]


def _fetch_extended_asos(
    station: WeatherStation, start_date: str, end_date: str,
    *, session: Optional[requests.Session] = None,
) -> list[_RawRow]:
    """Fetch ~5-min-cadence ASOS observations with regime features.

    Returns a list of _RawRow (one per non-empty CSV line). Caller is
    responsible for downsampling to per-LST-hour aggregates — we keep the
    raw cadence here because computing ``running_max_tmpf`` accurately
    needs every reading, not just one per hour.
    """
    sess = session or requests
    sd = datetime.strptime(start_date, "%Y-%m-%d")
    ed = datetime.strptime(end_date, "%Y-%m-%d")
    # Back up 1 UTC day so the first requested LST date gets full
    # 24-hour coverage. Stations west of UTC have their LST day starting
    # before the matching UTC day, so a same-day UTC start truncates the
    # morning hours of the first LST date — running_max would be wrong
    # for that day. The downstream filter still keys off the requested
    # window via lst_date >= start_date, so the extra UTC day is purely
    # for accurate running-max computation, not for inclusion in output.
    sd_fetch = sd - timedelta(days=1)
    params = {
        "station": station.icao,
        # Wind dir + speed feed sea-breeze classifier; sky cover feeds
        # radiative regime; dewpoint feeds humidity bucket. tmpf stays
        # primary because residual_peak is computed against it.
        "data": "tmpf,dwpf,drct,sknt,skyc1",
        "year1": sd_fetch.year, "month1": sd_fetch.month, "day1": sd_fetch.day,
        "year2": ed.year, "month2": ed.month, "day2": ed.day,
        "tz": "Etc/UTC",
        "format": "onlycomma",
        "missing": "empty",
        "latlon": "no",
    }
    r = sess.get(
        _IEM_ASOS_URL, params=params, timeout=120,
        headers={"User-Agent": _USER_AGENT},
    )
    if r.status_code != 200:
        raise RuntimeError(
            f"IEM asos {station.icao} HTTP {r.status_code}: {r.text[:200]}"
        )

    lst_tz = timezone(timedelta(hours=station.lst_offset))
    out: list[_RawRow] = []
    reader = csv.DictReader(io.StringIO(r.text))
    for row in reader:
        ts_raw = (row.get("valid") or "").strip()
        temp_raw = (row.get("tmpf") or "").strip()
        if not ts_raw or not temp_raw:
            continue
        try:
            tmpf = float(temp_raw)
        except ValueError:
            continue
        # Same physical-plausibility gate as the existing backfill — keep
        # behavior consistent. Anything outside ±60..140°F is a sensor
        # glitch or unit confusion, not real weather.
        if tmpf < -60.0 or tmpf > 140.0:
            continue
        try:
            utc_ts = datetime.strptime(ts_raw, "%Y-%m-%d %H:%M").replace(
                tzinfo=timezone.utc,
            )
        except ValueError:
            continue

        def _f(key: str) -> Optional[float]:
            raw = (row.get(key) or "").strip()
            if not raw:
                return None
            try:
                return float(raw)
            except ValueError:
                return None

        dt_lst = utc_ts.astimezone(lst_tz)
        out.append(_RawRow(
            utc_ts=utc_ts,
            lst_date=dt_lst.date().isoformat(),
            lst_hour=dt_lst.hour,
            tmpf=tmpf,
            dwpf=_f("dwpf"),
            drct=_f("drct"),
            sknt=_f("sknt"),
            skyc1=(row.get("skyc1") or "").strip() or None,
        ))
    return out


def _last_per_cell(
    raw: list[_RawRow],
) -> dict[tuple[str, int], _RawRow]:
    """Pick the latest-UTC row in each (lst_date, lst_hour) cell.

    Matches existing backfill convention: "the most recent observation at
    hour h that a runtime call would have had available." Different from
    "max within hour" — for residual-peak we use the running max separately.
    """
    by_cell: dict[tuple[str, int], _RawRow] = {}
    for r in raw:
        key = (r.lst_date, r.lst_hour)
        prev = by_cell.get(key)
        if prev is None or r.utc_ts > prev.utc_ts:
            by_cell[key] = r
    return by_cell


def _running_max_at_hour(
    raw: list[_RawRow],
) -> dict[tuple[str, int], float]:
    """For each (lst_date, lst_hour), the max tmpf observed at-or-before
    the END of that hour on that date. So the value at hour 14 reflects
    the max from midnight LST through 14:59:59 LST.

    This is what "running high so far" means — what a runtime quoter
    would see at that LST hour during the day.
    """
    # First, group by date and sort by utc_ts ascending.
    by_date: dict[str, list[_RawRow]] = {}
    for r in raw:
        by_date.setdefault(r.lst_date, []).append(r)
    out: dict[tuple[str, int], float] = {}
    for lst_date, rows in by_date.items():
        rows.sort(key=lambda r: r.utc_ts)
        # For each hour 0..23, take the max tmpf among rows whose
        # lst_hour <= hour AND lst_date == lst_date. Because rows are
        # already filtered to the date, the constraint is just lst_hour.
        running = -1e9
        # Walk hours in order and update running max as we encounter rows
        # with that lst_hour. Then for any hour without a row we still
        # carry running forward.
        rows_by_hour: dict[int, list[_RawRow]] = {}
        for r in rows:
            rows_by_hour.setdefault(r.lst_hour, []).append(r)
        for hour in range(24):
            for r in rows_by_hour.get(hour, []):
                if r.tmpf > running:
                    running = r.tmpf
            if running > -1e8:
                out[(lst_date, hour)] = running
    return out


def _fetch_cf6_for_window(
    station_icao: str, start_date: str, end_date: str,
    *, session: Optional[requests.Session] = None,
) -> dict[str, int]:
    """Pull CF6 TMAX for every calendar month touched by [start, end].

    Returns ``{lst_date_iso: tmax_F}``.
    """
    sd = datetime.strptime(start_date, "%Y-%m-%d").date()
    ed = datetime.strptime(end_date, "%Y-%m-%d").date()
    months: set[tuple[int, int]] = set()
    cur = sd.replace(day=1)
    while cur <= ed:
        months.add((cur.year, cur.month))
        # Move to the first of next month.
        if cur.month == 12:
            cur = cur.replace(year=cur.year + 1, month=1)
        else:
            cur = cur.replace(month=cur.month + 1)
    out: dict[str, int] = {}
    for (yr, mo) in sorted(months):
        per_day = fetch_cf6_tmax(station_icao, yr, mo, session=session)
        for day, tmax in per_day.items():
            out[f"{yr:04d}-{mo:02d}-{day:02d}"] = tmax
    return out


def pull_station(
    station: WeatherStation, start_date: str, end_date: str,
    out_dir: Path,
    *, session: Optional[requests.Session] = None,
) -> tuple[int, int]:
    """Pull, join, and write one station's regime CSV.

    Returns ``(rows_written, days_with_cf6)``.
    """
    sess = session or requests.Session()
    print(f"[{station.icao}] fetching extended ASOS {start_date} → {end_date}")
    raw = _fetch_extended_asos(station, start_date, end_date, session=sess)
    print(f"[{station.icao}] {len(raw)} raw obs rows")
    by_cell = _last_per_cell(raw)
    running = _running_max_at_hour(raw)
    print(f"[{station.icao}] {len(by_cell)} (lst_date, lst_hour) cells")

    cf6 = _fetch_cf6_for_window(station.icao, start_date, end_date, session=sess)
    print(f"[{station.icao}] CF6 TMAX days = {len(cf6)}")

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{station.icao}.csv"
    rows_written = 0
    with out_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "station", "lst_date", "lst_hour", "utc_ts",
            "tmpf", "dwpf", "drct", "sknt", "skyc1",
            "dewpoint_depression_f",
            "daily_high_cf6_f",
            "running_max_tmpf_at_hour_f",
            "residual_peak_f",
        ])
        for (lst_date, lst_hour), row in sorted(by_cell.items()):
            # Drop the extra LST day we pulled for accurate running-max —
            # any cells before the requested window are diagnostic-only.
            if lst_date < start_date:
                continue
            run = running.get((lst_date, lst_hour))
            if run is None:
                continue
            cf6_high = cf6.get(lst_date)
            # Keep the row even without CF6 — regime lookups (wind / sky /
            # dewpoint at a given lst_hour) don't need the eventual daily
            # high. Fitter consumers that need residual_peak_f filter on
            # the empty field. Predicates needing only the regime
            # axes work fine. Today's day is the canonical no-CF6 case
            # (CF6 publishes ~5am LST the next day).
            ddep = (row.tmpf - row.dwpf) if row.dwpf is not None else None
            w.writerow([
                station.icao, lst_date, lst_hour,
                row.utc_ts.isoformat(),
                f"{row.tmpf:.1f}",
                f"{row.dwpf:.1f}" if row.dwpf is not None else "",
                f"{row.drct:.0f}" if row.drct is not None else "",
                f"{row.sknt:.1f}" if row.sknt is not None else "",
                row.skyc1 or "",
                f"{ddep:.1f}" if ddep is not None else "",
                cf6_high if cf6_high is not None else "",
                f"{run:.1f}",
                f"{cf6_high - run:.1f}" if cf6_high is not None else "",
            ])
            rows_written += 1
    print(
        f"[{station.icao}] wrote {rows_written} rows → {out_path}"
    )
    return rows_written, len(cf6)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--days", type=int, default=30,
        help="Days of history to pull, ending today UTC (default 30)",
    )
    ap.add_argument(
        "--stations", default="",
        help="Comma-separated ICAO list. Empty = all 6 cities.",
    )
    ap.add_argument(
        "--out-dir", default="reports/regime_features",
        help="Output directory for per-station CSVs.",
    )
    args = ap.parse_args(argv)

    today = datetime.now(timezone.utc).date()
    # IEM treats day2 as exclusive (data ends at start-of-day on day2),
    # so to include today's hours-so-far we set end = today + 1. The
    # downstream output filter still keys off the requested window.
    end = today + timedelta(days=1)
    start = today - timedelta(days=args.days - 1)
    start_iso, end_iso = start.isoformat(), end.isoformat()

    if args.stations:
        wanted = {s.strip().upper() for s in args.stations.split(",") if s.strip()}
        stations = [s for s in _REGISTRY if s.icao in wanted]
        missing = wanted - {s.icao for s in stations}
        if missing:
            print(f"[warn] unknown stations skipped: {sorted(missing)}")
    else:
        stations = list(_REGISTRY)

    out_dir = Path(args.out_dir)
    sess = requests.Session()

    total_rows = 0
    for s in stations:
        try:
            n, _ = pull_station(s, start_iso, end_iso, out_dir, session=sess)
            total_rows += n
        except Exception as exc:
            print(f"[{s.icao}] FAILED: {type(exc).__name__}: {exc}")
        time.sleep(_INTER_STATION_SLEEP_S)

    print(f"[done] total rows written: {total_rows}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
