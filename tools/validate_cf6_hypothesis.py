"""Validate the hypothesis that Kalshi settles weather markets on the
NWS Climatological Daily Report (CF6 form), not raw METAR tmpf max.

Fetches the CF6 product for each city's primary station from IEM's AFOS
archive, parses TMAX per day, prints a 3-way comparison:

    | DY |  CF6 TMAX  |  our tmpf max  |  Kalshi settled bracket  |

If CF6 TMAX consistently lands inside Kalshi's settled bracket while
our tmpf max sits 2-3°F below, the hypothesis is confirmed and we know
exactly what to fix in the data pipeline.

CF6 products are issued by WFO offices once per day, around 4-6h after
midnight local time, covering the prior local day. The text format is
fixed-width with a daily-rows table; we parse the MAX column per DY row.
"""

from __future__ import annotations

import argparse
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional

from bot.db import init_db
from bot.config import DB_PATH


_AFOS_URL = "https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py"
_USER_AGENT = "kalshi-bot-cf6-validate/1.0"
_PACE_S = 0.25


# CF6 PILs we care about — same naming as Kalshi rules (3-char site code).
# CF6 products are issued by WFOs but the PIL identifier is the airport's
# 3-letter ID, e.g., CF6MIA = Miami International (issued by NWS MFL).
_CF6_BY_SERIES: dict[str, str] = {
    "KXHIGHNY":  "CF6NYC",   # Central Park
    "KXHIGHCHI": "CF6MDW",   # Chicago Midway
    "KXHIGHMIA": "CF6MIA",   # Miami International
    "KXHIGHLAX": "CF6LAX",   # LAX
    "KXHIGHAUS": "CF6AUS",   # Austin Bergstrom
    "KXHIGHDEN": "CF6DEN",   # Denver International
}


def _fetch_cf6(pil: str, end_iso: str) -> Optional[str]:
    """Fetch the CF6 product for ``pil`` issued at-or-before ``end_iso``
    (UTC). Returns the raw text body or None on failure.

    IEM's AFOS retrieve endpoint serves WMO-tagged products by PIL.
    Default behavior returns the most-recent product before ``e``.
    """
    params = {"pil": pil, "limit": "1"}
    if end_iso:
        params["e"] = end_iso
    url = f"{_AFOS_URL}?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        print(f"  [cf6 fetch error for {pil}] {type(exc).__name__}: {exc}")
        return None


def _parse_cf6_daily_max(
    body: str, year: int, month: int,
) -> dict[int, int]:
    """Extract ``{day_of_month: tmax_int_F}`` from the CF6 text body.

    The CF6 form has a fixed-width daily table that begins after a
    header line containing ``DY`` ``MAX`` ``MIN`` etc. Each subsequent
    line until the SUMMARY/== separator is a day's data. The format:

        DY MAX MIN AVG DEP HDD CDD ...
         1  80  68  74   3   0   9 ...
         2  82  70  76   1   0  11 ...
         ...

    We parse the leading two integer columns per data row.
    """
    out: dict[int, int] = {}
    in_table = False
    for line in body.splitlines():
        stripped = line.strip()
        # Detect the start of the daily table by header keyword sequence.
        if not in_table:
            if stripped.startswith("DY ") and "MAX" in stripped and "MIN" in stripped:
                in_table = True
            continue

        # Inside the table: any line that matches the data-row pattern
        # (leading 1-2 digits = day-of-month, followed by MAX integer) is
        # a day's row. Non-matching lines (separators '====', blank lines,
        # SUMMARY rows starting 'SM'/'AV', PAGE 2 header, etc.) are
        # silently skipped — robust against the SUMMARY/blank-line
        # interleaving that earlier strict end-of-table detection broke on.
        m = re.match(r"^\s*(\d{1,2})\s+(\d{1,3}|M|-)\s+", line)
        if not m:
            # Heuristic stop: once we've collected data and we're now seeing
            # the second-page CF6 header (typically prefixed with letters
            # not numerals), we're done. Otherwise keep skipping.
            if out and stripped.startswith(("AVERAGE MONTHLY", "DPTR FM NORMAL",
                                            "HIGHEST", "LOWEST", "TOTAL FOR MONTH",
                                            "[TEMPERATURE", "[PRESSURE")):
                break
            continue
        try:
            day = int(m.group(1))
            if not (1 <= day <= 31):
                continue
            tmax_raw = m.group(2)
            if tmax_raw in ("M", "-"):
                continue
            tmax = int(tmax_raw)
            if -60 <= tmax <= 140:
                out[day] = tmax
        except ValueError:
            continue
    return out


# ── Validation: 4 catastrophic Miami cases ────────────────────────────

# Each case: (lst_date, kalshi_bracket_text, lo_inclusive, hi_inclusive)
_MIAMI_CASES = [
    ("2026-04-22", "B81.5 (high in [81, 82])", 81, 82),
    ("2026-04-23", "B79.5 (high in [79, 80])", 79, 80),
    ("2026-04-24", "B84.5 (high in [84, 85])", 84, 85),
    ("2026-04-25", "B85.5 (high in [85, 86])", 85, 86),
]


def _backfill_max(conn, station: str, lst_date: str) -> Optional[int]:
    row = conn.execute(
        """SELECT MAX(temp_f), MAX(daily_high_f)
             FROM weather_metar_hourly_backfill
            WHERE station = ? AND lst_date = ?""",
        (station, lst_date),
    ).fetchone()
    if row and row[0] is not None:
        return int(row[0])
    return None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=DB_PATH)
    args = p.parse_args()
    conn = init_db(args.db)

    # Single CF6 fetch with end_iso = a few days after the latest target
    # date covers everything in the same monthly product.
    end_iso = "2026-04-29T12:00Z"
    body = _fetch_cf6("CF6MIA", end_iso)
    if not body:
        print("Failed to fetch CF6MIA — abort")
        return 1

    print(f"=== CF6MIA product (length {len(body)} chars, end={end_iso}) ===")
    # Print the first few lines as a sanity dump
    for line in body.splitlines()[:10]:
        print(f"  | {line}")
    print("  | ...")

    daily_max = _parse_cf6_daily_max(body, year=2026, month=4)
    print(f"\nParsed {len(daily_max)} day-rows from CF6MIA: {sorted(daily_max.keys())}")

    print()
    print("=" * 92)
    print("CF6 TMAX vs our tmpf max vs Kalshi settled bracket — KMIA / Miami")
    print("=" * 92)
    print(f"  {'date':<12} {'CF6_TMAX':>9} {'our_tmpf_max':>14} "
          f"{'Kalshi':<28} {'verdict':<20}")
    print("  " + "-" * 88)

    for lst_date, kalshi_text, lo, hi in _MIAMI_CASES:
        day = int(lst_date.split("-")[2])
        cf6 = daily_max.get(day)
        ours = _backfill_max(conn, "KMIA", lst_date)
        cf6_str = f"{cf6}°F" if cf6 is not None else "-"
        ours_str = f"{ours}°F" if ours is not None else "-"

        if cf6 is None:
            verdict = "no CF6 data"
        elif lo <= cf6 <= hi:
            verdict = "✓ CF6 in Kalshi bracket"
        else:
            verdict = f"✗ CF6 outside [{lo},{hi}]"

        print(f"  {lst_date:<12} {cf6_str:>9} {ours_str:>14} "
              f"{kalshi_text:<28} {verdict:<20}")

    print()
    print("Reading: if every CF6 value sits inside the Kalshi bracket while")
    print("our tmpf max consistently undershoots, the hypothesis is confirmed:")
    print("Kalshi settles on CF6 / NWS Daily Climate Report TMAX, not METAR tmpf max.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
