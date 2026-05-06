"""A1b + A3a: Pull Kalshi public trade tape for settled weather brackets.

Purpose: test tail-bucket calibration on Kalshi weather.
For each settled bucket, determine its "reference price" (volume-weighted yes
price during the first active trading window) and match to settlement outcome.

Then compute actual YES-hit rate stratified by reference price bin.
If 2c buckets actually hit at ~2% rate, tails are calibrated.
If 2c buckets hit at 6% rate, tails are systematically underpriced (good for buyers).
If 2c buckets hit at 0% rate, tails are systematically overpriced / illiquid.

Outputs a CSV + prints a calibration table.
"""
from __future__ import annotations

import csv
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
FAMILIES = ["KXHIGHAUS", "KXHIGHCHI", "KXHIGHDEN", "KXHIGHLAX", "KXHIGHMIA", "KXHIGHNY"]
OUT = Path(__file__).parent / "tail_calibration.csv"

MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def http_get(url: str, retries: int = 3) -> dict:
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "kalshi-bot-research/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except Exception as e:
            if i == retries - 1:
                raise
            time.sleep(0.5 * (2 ** i))
    return {}


def list_markets_for_event(event_ticker: str) -> list[dict]:
    markets: list[dict] = []
    cursor = ""
    while True:
        q = {"event_ticker": event_ticker, "limit": "100"}
        if cursor:
            q["cursor"] = cursor
        url = f"{KALSHI_BASE}/markets?" + urllib.parse.urlencode(q)
        data = http_get(url)
        got = data.get("markets", []) or []
        markets.extend(got)
        cursor = data.get("cursor", "") or ""
        if not cursor or not got:
            break
    return markets


def list_trades_all(ticker: str, page_size: int = 1000, max_pages: int = 20) -> list[dict]:
    """Fetch ALL trades for a ticker by walking the cursor.

    API returns newest-first. We paginate until cursor is empty or we've
    pulled max_pages * page_size trades. For weather brackets the lifetime
    tape is usually 100-3000 trades, so this captures the full history.
    """
    trades: list[dict] = []
    cursor = ""
    for _ in range(max_pages):
        q = {"ticker": ticker, "limit": str(page_size)}
        if cursor:
            q["cursor"] = cursor
        url = f"{KALSHI_BASE}/markets/trades?" + urllib.parse.urlencode(q)
        data = http_get(url)
        page = data.get("trades", []) or []
        trades.extend(page)
        cursor = data.get("cursor", "") or ""
        if not cursor or not page:
            break
    return trades


def volume_weighted_yes_price(trades: list[dict], max_trades: int = 20) -> float | None:
    """Volume-weighted YES price across the earliest `max_trades` trades
    (the earliest trades in the tape, ie market-open activity).

    API returns newest-first, so earliest are at the end of the list.
    """
    if not trades:
        return None
    # Sort chronologically, take earliest max_trades
    trades_sorted = sorted(trades, key=lambda t: t.get("created_time", ""))
    subset = trades_sorted[:max_trades]
    num = 0.0
    den = 0.0
    for tr in subset:
        try:
            y = float(tr.get("yes_price_dollars", "0") or 0)
            c = float(tr.get("count_fp", "0") or 0)
            num += y * c
            den += c
        except Exception:
            continue
    return num / den if den > 0 else None


def enumerate_recent_settled_expiries(family: str, days_back: int = 30) -> list[str]:
    """Build event tickers like KXHIGHNY-26APR10 for past N days."""
    tickers = []
    now = datetime.now(timezone.utc)
    for d in range(1, days_back + 1):
        dt = now - timedelta(days=d)
        et = f"{family}-{dt.strftime('%y').upper()}{MONTHS[dt.month-1]}{dt.day:02d}"
        tickers.append(et)
    return tickers


def main() -> None:
    rows = []
    for family in FAMILIES:
        events = enumerate_recent_settled_expiries(family, days_back=30)
        print(f"\n=== {family}: checking {len(events)} candidate expiries ===")
        settled_count = 0
        for ev in events:
            markets = list_markets_for_event(ev)
            if not markets:
                continue
            # Only consider fully-settled events
            results = [m.get("result") for m in markets]
            if not all(r in ("yes", "no") for r in results):
                continue
            settled_count += 1
            for m in markets:
                tkr = m.get("ticker", "")
                result = m.get("result", "")
                trades = list_trades_all(tkr, page_size=1000, max_pages=20)
                if not trades:
                    continue
                # Compute multiple reference prices along the lifetime:
                #   - first 20 trades (true market-open)
                #   - middle 20% of tape
                #   - last 20 trades (near-settlement)
                # This lets us distinguish "forecast-uncertainty pricing" from
                # "already-resolved pricing."
                trades_sorted = sorted(trades, key=lambda t: t.get("created_time", ""))
                n = len(trades_sorted)
                open_slice = trades_sorted[: min(20, n)]
                mid_lo = int(n * 0.4)
                mid_hi = int(n * 0.6)
                mid_slice = trades_sorted[mid_lo:mid_hi] if mid_hi > mid_lo else trades_sorted
                close_slice = trades_sorted[max(0, n - 20):]
                open_price = volume_weighted_yes_price(open_slice, max_trades=1000)
                mid_price = volume_weighted_yes_price(mid_slice, max_trades=1000)
                close_price = volume_weighted_yes_price(close_slice, max_trades=1000)
                if open_price is None:
                    continue
                first_t = trades_sorted[0].get("created_time", "")
                last_t = trades_sorted[-1].get("created_time", "")
                rows.append({
                    "family": family,
                    "event": ev,
                    "ticker": tkr,
                    "yes_sub_title": m.get("yes_sub_title", ""),
                    "open_yes_price": round(open_price, 4) if open_price is not None else None,
                    "mid_yes_price": round(mid_price, 4) if mid_price is not None else None,
                    "close_yes_price": round(close_price, 4) if close_price is not None else None,
                    "settled_yes": 1 if result == "yes" else 0,
                    "n_trades_in_tape": n,
                    "first_trade_utc": first_t,
                    "last_trade_utc": last_t,
                })
                time.sleep(0.05)  # rate limit gentle
        print(f"    settled expiries found: {settled_count}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    print(f"\nWrote {len(rows)} rows to {OUT}")

    # Calibration table — one per reference point
    bins = [(0.00, 0.02), (0.02, 0.05), (0.05, 0.10), (0.10, 0.20),
            (0.20, 0.40), (0.40, 0.60), (0.60, 0.80), (0.80, 0.95), (0.95, 1.01)]
    for label, field in [("OPEN (first 20 trades)", "open_yes_price"),
                         ("MID (middle 20% of tape)", "mid_yes_price"),
                         ("CLOSE (last 20 trades)", "close_yes_price")]:
        print(f"\n=== Calibration @ {label}: implied YES prob vs actual YES rate ===")
        print(f"{'bin':<15} {'n':>5} {'n_yes':>6} {'actual_yes_rate':>16} {'avg_implied':>12}")
        for lo, hi in bins:
            subset = [r for r in rows if r.get(field) is not None and lo <= r[field] < hi]
            n = len(subset)
            n_yes = sum(r["settled_yes"] for r in subset)
            avg_implied = sum(r[field] for r in subset) / n if n else 0.0
            actual = n_yes / n if n else 0.0
            print(f"{lo:.2f}-{hi:.2f}      {n:>5} {n_yes:>6} {actual:>15.1%} {avg_implied:>11.1%}")


if __name__ == "__main__":
    main()
