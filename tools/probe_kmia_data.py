"""Direct IEM probe to settle the Miami data-discrepancy question.

For each catastrophe date (4/22, 4/23, 4/24, 4/25), fetches:
  * Raw hourly tmpf from IEM ASOS for KMIA — confirms whether late-evening
    peak hours (18-23 LST) carry higher temps than our backfill captured.
  * Same query for KFLL, KMFL, KOPF — alternate Miami-area stations
    Kalshi might use for settlement.

Reports daily max LST per station, side-by-side with what
weather_metar_hourly_backfill stored and what Kalshi settled.

Run on the VPS:
    python -m tools.probe_kmia_data --db /home/kalshi/autoagent/kalshi_trades.db
"""

from __future__ import annotations

import argparse
import csv
import io
from datetime import datetime, timedelta, timezone

import requests

from bot.db import init_db
from bot.config import DB_PATH


_IEM_ASOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
_USER_AGENT = "kalshi-bot-probe/1.0"
_LST_OFFSET_HOURS = -4  # EDT (Miami's TZ in late April 2026)


# Catastrophe dates + Kalshi settlement evidence for KXHIGHMIA on those days.
_CASES = [
    # (lst_date,   kalshi_signal)
    ("2026-04-22", "settled YES on B81.5 → high in [81, 82]°F"),
    ("2026-04-23", "settled YES on B79.5 → high in [79, 80]°F"),
    ("2026-04-24", "settled YES on B84.5 → high in [84, 85]°F"),
    ("2026-04-25", "settled YES on B85.5 → high in [85, 86]°F"),
]

_STATIONS = ["KMIA", "KFLL", "KMFL", "KOPF"]


def fetch_hourly(station: str, lst_date: str) -> tuple[list, float]:
    """Return ((lst_hour, tmpf_f, utc_iso) list, max_tmpf_6hr_max).

    Fetches both ``tmpf`` (instantaneous reading) and ``max_tmpf_6hr``
    (ASOS-reported max in the prior 6 hours). The latter captures peaks
    that happened *between* hourly observations — exactly the case we
    suspected for the Miami discrepancy. Returns the max of all
    max_tmpf_6hr values across the LST day.
    """
    d = datetime.strptime(lst_date, "%Y-%m-%d")
    start = d - timedelta(days=1)
    end = d + timedelta(days=1)
    params = {
        "station": station,
        "data": "tmpf,max_tmpf_6hr",
        "year1": start.year, "month1": start.month, "day1": start.day,
        "year2": end.year, "month2": end.month, "day2": end.day,
        "tz": "Etc/UTC",
        "format": "onlycomma",
        "missing": "empty",
        "latlon": "no",
    }
    r = requests.get(
        _IEM_ASOS_URL, params=params, timeout=60,
        headers={"User-Agent": _USER_AGENT},
    )
    if r.status_code != 200:
        raise RuntimeError(f"IEM HTTP {r.status_code}: {r.text[:200]}")

    lst_tz = timezone(timedelta(hours=_LST_OFFSET_HOURS))
    per_cell: dict[int, tuple[float, datetime]] = {}
    max_tmpf_6hr_running = float("-inf")
    for row in csv.DictReader(io.StringIO(r.text)):
        ts_raw = (row.get("valid") or "").strip()
        temp_raw = (row.get("tmpf") or "").strip()
        max6_raw = (row.get("max_tmpf_6hr") or "").strip()
        if not ts_raw:
            continue
        try:
            dt_utc = datetime.strptime(ts_raw, "%Y-%m-%d %H:%M").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        dt_lst = dt_utc.astimezone(lst_tz)
        if dt_lst.date().isoformat() != lst_date:
            continue
        if temp_raw:
            try:
                temp = float(temp_raw)
                if -60 <= temp <= 140:
                    prior = per_cell.get(dt_lst.hour)
                    if prior is None or dt_utc > prior[1]:
                        per_cell[dt_lst.hour] = (temp, dt_utc)
            except ValueError:
                pass
        if max6_raw:
            try:
                m6 = float(max6_raw)
                if -60 <= m6 <= 140 and m6 > max_tmpf_6hr_running:
                    max_tmpf_6hr_running = m6
            except ValueError:
                pass
    hourly = sorted(
        (h, t, dt.isoformat()) for h, (t, dt) in per_cell.items()
    )
    max6 = max_tmpf_6hr_running if max_tmpf_6hr_running > -1e9 else None
    return hourly, max6


def backfill_summary(conn, station: str, lst_date: str) -> dict:
    """What our weather_metar_hourly_backfill table has for this case."""
    rows = conn.execute(
        """SELECT lst_hour, temp_f, daily_high_f
             FROM weather_metar_hourly_backfill
            WHERE station = ? AND lst_date = ?
         ORDER BY lst_hour""",
        (station, lst_date),
    ).fetchall()
    if not rows:
        return {"n": 0, "min_h": None, "max_h": None, "max_temp": None,
                "stored_dh": None}
    temps = [t for _, t, _ in rows if t is not None]
    return {
        "n": len(rows),
        "min_h": rows[0][0],
        "max_h": rows[-1][0],
        "max_temp": max(temps) if temps else None,
        "stored_dh": rows[0][2],
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=DB_PATH)
    args = p.parse_args()
    conn = init_db(args.db)
    print("=" * 92)
    print("KMIA + Miami-area alternate stations — IEM raw hourly probe")
    print("=" * 92)

    for lst_date, kalshi in _CASES:
        print()
        print(f"── {lst_date} — Kalshi: {kalshi} ──")
        for station in _STATIONS:
            try:
                hourly, max6 = fetch_hourly(station, lst_date)
            except Exception as exc:
                print(f"  {station}: fetch error: {exc}")
                continue
            if not hourly:
                print(f"  {station}: no IEM data for this LST day")
                continue
            iem_max = max(t for _, t, _ in hourly)
            iem_max_hour = max(hourly, key=lambda r: r[1])[0]

            bf = backfill_summary(conn, station, lst_date)
            bf_str = (
                f"backfill: dh={bf['stored_dh']}°F max_temp={bf['max_temp']}°F "
                f"hours={bf['min_h']}-{bf['max_h']} ({bf['n']} rows)"
                if bf["n"] else "backfill: (no rows)"
            )
            max6_str = (
                f"max_tmpf_6hr={max6:.1f}°F"
                if max6 is not None else "max_tmpf_6hr=NA"
            )
            print(f"  {station}: tmpf_max={iem_max:.1f}°F @ hour {iem_max_hour} "
                  f"({len(hourly)} cells)  {max6_str}  |  {bf_str}")
            late = [(h, t) for h, t, _ in hourly if 18 <= h <= 23]
            if late:
                late_max = max(t for _, t in late)
                late_max_hour = max(late, key=lambda x: x[1])[0]
                print(f"      late hours (18-23): max {late_max:.1f}°F "
                      f"@ {late_max_hour}, n={len(late)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
