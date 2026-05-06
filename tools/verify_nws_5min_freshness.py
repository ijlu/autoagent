"""Live verification harness for `bot/signals/sources/nws_5min.py`.

Before adding `nws_5min` to the production combine, this tool needs to
demonstrate three things over a multi-hour window:

  1. NWS api.weather.gov genuinely serves sub-hourly observations for
     each of our 6 city stations (5/6 confirmed at probe time;
     KNYC is hourly-only and uses KLGA as proxy).
  2. NWS temperature values agree with aviationweather.gov at the
     hourly :53 METAR boundary (within 1°F — both sourced from the
     same physical ASOS sensor).
  3. NWS surfaces NEW readings between hourly METAR boundaries — i.e.
     `obs_age_s` averages well below 30 minutes, vs ~30min average for
     hourly METAR.

Run for at least 6 hours during the local LST 11–17 peak window of
ANY one city. Output CSV is appendable across runs.

Usage:

    python3 tools/verify_nws_5min_freshness.py \\
        --duration-hours 6 \\
        --poll-interval-s 300 \\
        --output /tmp/nws_5min_verify.csv

The output CSV columns:
    poll_iso, station, nws_temp_f, nws_obs_iso, nws_obs_age_s,
    metar_temp_f, metar_obs_iso, metar_obs_age_s,
    abs_diff_f, freshness_lead_s

Inspection rules:
  * Median(nws_obs_age_s) should be < 600 (10 min) — confirms 5-min cadence.
  * Median(metar_obs_age_s) should be ~1800 (30 min) — baseline.
  * Median(freshness_lead_s) > 600 means NWS is meaningfully fresher.
  * P95(abs_diff_f) should be < 1.5°F — confirms same-sensor agreement.
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot.signals.sources.nws_5min import (  # noqa: E402
    PRIMARY_5MIN_STATION_BY_CITY, fetch_recent_observations,
)


def _fetch_metar_aviationweather(icao: str) -> Optional[tuple[float, datetime]]:
    """Reference METAR fetch from aviationweather.gov — same data path
    the daemon uses today."""
    import requests

    url = (
        "https://aviationweather.gov/api/data/metar"
        f"?ids={icao}&format=json"
    )
    try:
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return None
        body = r.json()
    except Exception as e:
        print(f"[metar] {icao}: {type(e).__name__}: {e}", file=sys.stderr)
        return None
    if not body:
        return None
    obs = body[0]
    temp_c = obs.get("temp")
    rt = obs.get("reportTime") or obs.get("obsTime")
    if temp_c is None or rt is None:
        return None
    try:
        temp_f = float(temp_c) * 9.0 / 5.0 + 32.0
        obs_time = datetime.fromisoformat(str(rt).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return temp_f, obs_time


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--duration-hours", type=float, default=1.0)
    ap.add_argument("--poll-interval-s", type=int, default=300)
    ap.add_argument("--output", type=str, default="/tmp/nws_5min_verify.csv")
    args = ap.parse_args()

    out_path = Path(args.output)
    new_file = not out_path.exists()
    f = out_path.open("a", newline="")
    w = csv.writer(f)
    if new_file:
        w.writerow([
            "poll_iso", "station",
            "nws_temp_f", "nws_obs_iso", "nws_obs_age_s",
            "metar_temp_f", "metar_obs_iso", "metar_obs_age_s",
            "abs_diff_f", "freshness_lead_s",
        ])
        f.flush()

    end_time = time.time() + args.duration_hours * 3600.0
    poll_idx = 0
    print(f"[setup] writing to {out_path}")
    print(f"[setup] polling {len(PRIMARY_5MIN_STATION_BY_CITY)} stations every "
          f"{args.poll_interval_s}s for {args.duration_hours}h")

    while time.time() < end_time:
        poll_idx += 1
        now = datetime.now(timezone.utc)
        for city, stn in PRIMARY_5MIN_STATION_BY_CITY.items():
            obs_list = fetch_recent_observations(stn) or []
            latest = obs_list[0] if obs_list else None
            metar = _fetch_metar_aviationweather(stn)

            nws_temp = latest["temp_f"] if latest else None
            nws_t = latest["obs_time_utc"] if latest else None
            metar_temp, metar_t = metar if metar else (None, None)

            nws_age = (now - nws_t).total_seconds() if nws_t else None
            metar_age = (now - metar_t).total_seconds() if metar_t else None
            abs_diff = (
                abs(nws_temp - metar_temp)
                if nws_temp is not None and metar_temp is not None else None
            )
            lead = (
                metar_age - nws_age
                if nws_age is not None and metar_age is not None else None
            )

            w.writerow([
                now.isoformat(timespec="seconds"), stn,
                f"{nws_temp:.1f}" if nws_temp is not None else "",
                nws_t.isoformat(timespec="seconds") if nws_t else "",
                f"{nws_age:.0f}" if nws_age is not None else "",
                f"{metar_temp:.1f}" if metar_temp is not None else "",
                metar_t.isoformat(timespec="seconds") if metar_t else "",
                f"{metar_age:.0f}" if metar_age is not None else "",
                f"{abs_diff:.2f}" if abs_diff is not None else "",
                f"{lead:.0f}" if lead is not None else "",
            ])
            f.flush()

            print(
                f"[{now.strftime('%H:%M:%S')}] {city:13s}/{stn}: "
                f"nws={nws_temp}°F (age={nws_age}s)  "
                f"metar={metar_temp}°F (age={metar_age}s)  "
                f"diff={abs_diff if abs_diff is None else f'{abs_diff:.2f}'}°F  "
                f"lead={lead if lead is None else f'{lead:.0f}'}s"
            )

        time.sleep(max(1.0, float(args.poll_interval_s)))

    f.close()
    print()
    print(f"[done] {poll_idx} poll cycles → {out_path}")
    print("Inspect with:")
    print(f"  python3 -c \"import csv; rows=list(csv.DictReader(open('{out_path}'))); "
          "import statistics as s; ages=[float(r['nws_obs_age_s']) for r in rows "
          "if r['nws_obs_age_s']]; "
          "print(f'NWS obs_age median={s.median(ages):.0f}s  p95={sorted(ages)[int(len(ages)*0.95)]:.0f}s')\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
