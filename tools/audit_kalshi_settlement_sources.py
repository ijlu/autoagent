"""Audit what station/source Kalshi uses to settle each weather series.

Discrepancy: KMIA's IEM hourly tmpf max is consistently 2-3°F below what
Kalshi settled on (4/22, 4/23, 4/24 all confirmed). Hypothesis: Kalshi
uses a different data source — different station, different ASOS field,
or the official NWS climate report — than the one our system trains
against. If true, our skill σ + MOS bias fits are trained on the wrong
ground truth across every weather city, and the catastrophic Miami
losses are just the most visible case.

This tool fetches one current-or-recent market per weather series, prints:
  * The market title and rules_primary / rules_secondary text
  * Any explicit settlement_sources field
  * Our STATION_BY_SERIES assumption for that series
  * Whether they agree

Then we read by eye and decide whether to:
  (a) Switch our backfill ground-truth source to whatever Kalshi names.
  (b) Add the right ASOS field (TMAX) to our existing IEM fetcher.
  (c) Both.

Run anywhere — pure HTTP against the public Kalshi market API.
"""

from __future__ import annotations

import argparse
import json
import textwrap
import time
import urllib.parse
import urllib.request
from typing import Optional

from bot.daemon.stations import STATION_BY_SERIES


_KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
_USER_AGENT = "kalshi-bot-audit/1.0"
_PACE_S = 0.3


def _http_get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def _find_recent_market(series_ticker: str) -> Optional[dict]:
    """Find a recent or upcoming market in this series and return its
    full payload. Tries the events endpoint first (usually has open
    events), falls back to the markets list scoped to the series.
    """
    # Markets endpoint with series_ticker filter is the most direct.
    url = (
        f"{_KALSHI_BASE}/markets?"
        f"{urllib.parse.urlencode({'series_ticker': series_ticker, 'limit': 5})}"
    )
    try:
        data = _http_get(url)
    except Exception as exc:
        return {"error": f"markets fetch failed: {exc}"}
    time.sleep(_PACE_S)

    markets = data.get("markets", []) or []
    if not markets:
        return {"error": "no markets returned"}

    # Pick the one with the longest rules text — the open ones tend to
    # have full settlement rules; expired ones can have shortened text.
    markets.sort(
        key=lambda m: -len(
            (m.get("rules_primary") or "") + (m.get("rules_secondary") or "")
        )
    )
    sample_ticker = markets[0]["ticker"]

    # Re-fetch the single market for the most-detailed payload (the list
    # response is sometimes truncated).
    detail_url = f"{_KALSHI_BASE}/markets/{urllib.parse.quote(sample_ticker, safe='')}"
    try:
        detail = _http_get(detail_url)
    except Exception as exc:
        return {"error": f"market detail fetch failed: {exc}"}
    time.sleep(_PACE_S)
    return detail.get("market") or markets[0]


def _print_section(market: dict, series_ticker: str) -> None:
    print()
    print("=" * 92)
    print(f"  {series_ticker}")
    print("=" * 92)

    if "error" in market:
        print(f"  ERROR: {market['error']}")
        return

    ticker = market.get("ticker", "?")
    title = market.get("title", "(no title)")
    subtitle = market.get("subtitle") or market.get("yes_sub_title") or ""
    print(f"  sample ticker: {ticker}")
    print(f"  title:    {title}")
    if subtitle:
        print(f"  subtitle: {subtitle}")

    # The two free-text rule fields. These almost always contain the
    # settlement source / station name.
    for key in ("rules_primary", "rules_secondary"):
        val = market.get(key)
        if val:
            print(f"  {key}:")
            for line in textwrap.wrap(val, width=88):
                print(f"    {line}")

    # Less commonly populated but worth showing if present.
    for key in ("settlement_sources", "settlement_source",
                "expiration_value_source", "underlying"):
        val = market.get(key)
        if val:
            print(f"  {key}: {val}")

    # Our assumption
    ws = STATION_BY_SERIES.get(series_ticker)
    if ws is None:
        print(f"  bot assumption: NO STATION_BY_SERIES entry — series not wired")
    else:
        print(f"  bot assumption: city={ws.city}  primary_icao={ws.icao}  "
              f"backups={list(ws.backups) if hasattr(ws, 'backups') else []}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--series", default=None,
        help="Comma-list of series tickers to audit (default: all weather "
             "in STATION_BY_SERIES)",
    )
    args = p.parse_args()

    if args.series:
        targets = [s.strip() for s in args.series.split(",") if s.strip()]
    else:
        targets = sorted(
            s for s in STATION_BY_SERIES.keys() if s.startswith("KXHIGH")
        )

    print(f"Auditing {len(targets)} weather series: {targets}")
    for series_ticker in targets:
        market = _find_recent_market(series_ticker)
        _print_section(market or {"error": "no market"}, series_ticker)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
