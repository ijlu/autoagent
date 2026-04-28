"""Backtest AFD multi-signal extraction against observed daily highs.

Validates whether richer LLM extraction (point forecast + confidence +
model agreement, etc.) produces calibrated predictions over the past
~90 days, before we ship Option 3 to production.

Pipeline:
  1. For each (city, settle_date) in the metar backfill window, fetch
     the AFD that was issued ~12-18h before LST midnight. We pull from
     IEM's archive via the ``wx/afos/p.php?pil=...&e=...`` endpoint,
     extract the product ID, then download the raw text.
  2. For each AFD, call gpt-4o-mini with two prompts:
       * OLD (current production): single bias number in °F.
       * NEW (multi-signal): JSON with expected_high_f, shift_vs_model_f,
         confidence, model_agreement, key_driver.
  3. Persist responses to ``afd_backtest`` so analysis can re-read.
  4. Join to ``weather_metar_hourly_backfill`` for observed daily highs.
  5. Compute calibration:
       * Old bias signal: does sign(bias) match sign(observed - baseline)?
       * New explicit point: is expected_high_f closer to observed
         than the NBM baseline?
       * New confidence: does it correlate with |residual|?

Cost: ~$2 in OpenAI charges, ~30 min runtime.

Usage::

    OPENAI_API_KEY=... python -m tools.backtest_afd_signal \\
        --db /home/kalshi/autoagent/kalshi_trades.db \\
        --start 2026-01-23 --end 2026-04-22

    # then re-analyze without re-fetching:
    python -m tools.backtest_afd_signal --db ... --analyze-only
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import statistics
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional

from bot.config import DB_PATH
from bot.db import db_write_ctx, init_db


_CITY_WFO: dict[str, tuple[str, str]] = {
    # city → (WFO code, METAR station)
    "nyc":         ("OKX", "KNYC"),
    "chicago":     ("LOT", "KMDW"),
    "miami":       ("MFL", "KMIA"),
    "los_angeles": ("LOX", "KLAX"),
    "austin":      ("EWX", "KAUS"),
    "denver":      ("BOU", "KDEN"),
}

_CITY_LST_OFFSET: dict[str, int] = {
    "nyc": -4, "chicago": -5, "miami": -4,
    "los_angeles": -7, "austin": -5, "denver": -6,
}

_IEM_BASE = "https://mesonet.agron.iastate.edu"
_HTTP_PACE_S = 0.4  # IEM is generous; be polite

_USER_AGENT = "KalshiTradingBot/1.0 (afd backtest; joshlu@a16z.com)"


# ── Data layer ──────────────────────────────────────────────────────────────

def _ensure_schema(conn: sqlite3.Connection) -> None:
    """One-shot table for backtest results so we can re-analyze without
    re-running the LLM (which costs money + time)."""
    with db_write_ctx(conn):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS afd_backtest (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                city TEXT NOT NULL,
                settle_date TEXT NOT NULL,
                wfo TEXT NOT NULL,
                product_id TEXT,
                afd_text TEXT,
                old_bias_f REAL,
                new_expected_high_f REAL,
                new_shift_vs_model_f REAL,
                new_confidence REAL,
                new_model_agreement TEXT,
                new_key_driver TEXT,
                fetched_at TEXT NOT NULL,
                UNIQUE(city, settle_date)
            )
        """)


# ── IEM fetch ───────────────────────────────────────────────────────────────

def _http_get(url: str, retries: int = 3, timeout: int = 30) -> Optional[bytes]:
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception:
            if i == retries - 1:
                return None
            time.sleep(0.5 * (2 ** i))
    return None


_PRODUCT_ID_RE = re.compile(r"/api/1/nwstext/(\d{12}-[A-Z0-9]+-[A-Z0-9]+-[A-Z0-9]+)")


def fetch_afd_for(wfo: str, lst_target_date: str, lst_offset_h: int) -> Optional[tuple[str, str]]:
    """Return (product_id, raw_text) of the AFD nearest to noon LST on
    ``lst_target_date`` for the given WFO, or None.

    We aim for the morning AFD on the LST settlement date (it's the one
    a trader would consult for that day's high). Convert noon LST to UTC
    using ``lst_offset_h`` and ask IEM for "latest before that UTC time".
    """
    try:
        y, m, d = (int(x) for x in lst_target_date.split("-"))
    except (ValueError, AttributeError):
        return None
    # Noon LST = 12 - lst_offset_h UTC (offset is negative for US zones)
    target_utc_hour = 12 - lst_offset_h  # e.g. -4 → 16 (4 PM UTC = noon EDT)
    # Cap into [0, 47] so dates roll over cleanly
    target_dt = datetime(y, m, d, 0, 0, tzinfo=timezone.utc) + timedelta(hours=target_utc_hour)
    e_param = target_dt.strftime("%Y%m%d%H%M")

    list_url = f"{_IEM_BASE}/wx/afos/p.php?pil=AFD{wfo}&e={e_param}"
    body = _http_get(list_url)
    if not body:
        return None
    text = body.decode("utf-8", errors="replace")
    m = _PRODUCT_ID_RE.search(text)
    if not m:
        return None
    product_id = m.group(1)
    raw = _http_get(f"{_IEM_BASE}/api/1/nwstext/{product_id}")
    if not raw:
        return None
    afd_text = raw.decode("utf-8", errors="replace")
    return product_id, afd_text


# ── LLM extraction ──────────────────────────────────────────────────────────

def _openai_call(prompt: str, max_tokens: int = 8) -> Optional[str]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    body = json.dumps({
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        print(f"[afd-backtest] OpenAI error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None


def llm_old_prompt(text: str, city: str) -> Optional[float]:
    """Production prompt — single bias number in °F."""
    snippet = text[:4000]
    prompt = (
        f"You are a forecaster reading an NWS Area Forecast Discussion for "
        f"{city}. Based on the text, output a single number: how much "
        f"(in °F) you expect today's daily high to deviate from generic "
        f"model guidance. Positive = warmer than model, negative = cooler "
        f"than model. Cap at ±5°F. If no opinion, output 0.\n\n"
        f"AFD text:\n{snippet}\n\n"
        f"Output a single number only, e.g. '-1.5' or '2' or '0'."
    )
    out = _openai_call(prompt, max_tokens=8)
    if not out:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", out)
    if not m:
        return None
    try:
        return max(-5.0, min(5.0, float(m.group(0))))
    except ValueError:
        return None


def llm_new_prompt(text: str, city: str) -> Optional[dict]:
    """Multi-signal extraction. Returns a dict with the structured fields
    or None on failure. Forces JSON via response_format and a strict prompt."""
    snippet = text[:4500]
    prompt = (
        f"You are reading an NWS Area Forecast Discussion (AFD) for {city}. "
        f"Extract the forecaster's view on today's daily high temperature.\n\n"
        f"Output strict JSON ONLY, no prose, with these fields:\n"
        f"  expected_high_f: number | null  "
        f"(forecaster's stated daily high in °F if explicit; else null)\n"
        f"  shift_vs_model_f: number  "
        f"(°F deviation forecaster expects vs model guidance; +warmer, -cooler; "
        f"cap ±5)\n"
        f"  confidence: number  "
        f"(0-1, how confident the forecaster sounds based on language used)\n"
        f"  model_agreement: \"good\" | \"spread\" | \"outliers\" | \"unstated\"\n"
        f"  key_driver: string  (≤6 words; physical mechanism cited)\n\n"
        f"If the AFD doesn't address today's high, return shift=0, confidence=0.\n\n"
        f"AFD text:\n{snippet}\n\n"
        f"Output JSON only."
    )
    out = _openai_call(prompt, max_tokens=200)
    if not out:
        return None
    # Strip ```json fences if present
    out = re.sub(r"^```(?:json)?\s*", "", out).strip()
    out = re.sub(r"\s*```$", "", out).strip()
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError:
        # Try to extract a JSON object inside the response
        m = re.search(r"\{.*\}", out, flags=re.DOTALL)
        if not m:
            return None
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return parsed


# ── Backtest driver ─────────────────────────────────────────────────────────

def fetch_and_extract(
    conn: sqlite3.Connection, start: str, end: str, *,
    cities: tuple[str, ...] = tuple(_CITY_WFO.keys()),
) -> dict:
    _ensure_schema(conn)
    # Iterate (city, date) combinations
    start_d = datetime.strptime(start, "%Y-%m-%d").date()
    end_d = datetime.strptime(end, "%Y-%m-%d").date()
    n_skipped = 0
    n_fetched = 0
    n_llm_old = 0
    n_llm_new = 0
    cur = start_d
    while cur <= end_d:
        for city in cities:
            wfo, _station = _CITY_WFO[city]
            offset_h = _CITY_LST_OFFSET[city]
            # Skip if already processed
            existing = conn.execute(
                "SELECT id, old_bias_f, new_expected_high_f FROM afd_backtest "
                "WHERE city = ? AND settle_date = ?",
                (city, cur.isoformat()),
            ).fetchone()
            if existing and existing[1] is not None and existing[2] is not None:
                n_skipped += 1
                continue
            time.sleep(_HTTP_PACE_S)
            fetched = fetch_afd_for(wfo, cur.isoformat(), offset_h)
            if not fetched:
                n_skipped += 1
                continue
            product_id, afd_text = fetched
            # OLD prompt
            old_bias = llm_old_prompt(afd_text, city)
            if old_bias is not None:
                n_llm_old += 1
            # NEW prompt
            new_signals = llm_new_prompt(afd_text, city)
            if new_signals:
                n_llm_new += 1
            # Persist
            with db_write_ctx(conn):
                conn.execute(
                    """INSERT INTO afd_backtest
                          (city, settle_date, wfo, product_id, afd_text,
                           old_bias_f, new_expected_high_f, new_shift_vs_model_f,
                           new_confidence, new_model_agreement, new_key_driver,
                           fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(city, settle_date) DO UPDATE SET
                           wfo=excluded.wfo,
                           product_id=excluded.product_id,
                           afd_text=excluded.afd_text,
                           old_bias_f=excluded.old_bias_f,
                           new_expected_high_f=excluded.new_expected_high_f,
                           new_shift_vs_model_f=excluded.new_shift_vs_model_f,
                           new_confidence=excluded.new_confidence,
                           new_model_agreement=excluded.new_model_agreement,
                           new_key_driver=excluded.new_key_driver,
                           fetched_at=excluded.fetched_at""",
                    (
                        city, cur.isoformat(), wfo, product_id, afd_text,
                        old_bias,
                        (new_signals or {}).get("expected_high_f"),
                        (new_signals or {}).get("shift_vs_model_f"),
                        (new_signals or {}).get("confidence"),
                        (new_signals or {}).get("model_agreement"),
                        (new_signals or {}).get("key_driver"),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            n_fetched += 1
            if n_fetched % 10 == 0:
                print(f"[afd-backtest] processed {n_fetched} (city={city} date={cur})")
        cur += timedelta(days=1)
    return {"fetched": n_fetched, "skipped": n_skipped,
            "llm_old": n_llm_old, "llm_new": n_llm_new}


def analyze(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """SELECT a.city, a.settle_date, a.old_bias_f,
                  a.new_expected_high_f, a.new_shift_vs_model_f,
                  a.new_confidence, a.new_model_agreement
             FROM afd_backtest a
            WHERE a.afd_text IS NOT NULL""",
    ).fetchall()
    if not rows:
        print("No AFD backtest rows yet — run without --analyze-only first.")
        return

    # Map city → station → fetch observed daily highs
    obs = {}
    for s, d, h in conn.execute(
        "SELECT station, lst_date, daily_high_f FROM weather_metar_hourly_backfill "
        "WHERE daily_high_f IS NOT NULL"
    ).fetchall():
        obs[(str(s), str(d))] = float(h)

    # We need a baseline to compute residuals (forecast - observed). Use
    # the snapshot's NBM forecast for that ticker if available.
    # Simpler: pull NBM's forecast for the same (city, date) from snapshots.
    nbm_per_city_date = {}
    for series, ticker, fcst in conn.execute(
        """SELECT s.series, s.ticker, AVG(s.forecast_high_f)
             FROM weather_forecast_snapshots s
            WHERE s.source = 'nbm' AND s.forecast_high_f IS NOT NULL
            GROUP BY s.ticker"""
    ).fetchall():
        # parse city, date
        city_map = {"KXHIGHNY": "nyc", "KXHIGHCHI": "chicago", "KXHIGHMIA": "miami",
                    "KXHIGHLAX": "los_angeles", "KXHIGHAUS": "austin", "KXHIGHDEN": "denver"}
        city = city_map.get(series)
        if not city or not ticker:
            continue
        parts = ticker.split("-")
        if len(parts) < 2 or len(parts[1]) < 7:
            continue
        suf = parts[1]
        months = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]
        try:
            sd = f"20{int(suf[:2]):02d}-{months.index(suf[2:5].upper())+1:02d}-{int(suf[5:7]):02d}"
        except (ValueError, IndexError):
            continue
        key = (city, sd)
        # Take any one ticker's NBM as the baseline
        if key not in nbm_per_city_date:
            nbm_per_city_date[key] = float(fcst)

    print(f"[analyze] {len(rows)} AFD records; "
          f"{len(obs)} observed daily highs; "
          f"{len(nbm_per_city_date)} NBM baseline cells")

    # Compute residuals
    sample = []
    for (city, settle_date, old_bias,
         new_high, new_shift, new_conf, new_agree) in rows:
        station = _CITY_WFO[city][1]
        observed = obs.get((station, settle_date))
        if observed is None:
            continue
        baseline = nbm_per_city_date.get((city, settle_date))
        # Use observed as truth; baseline gives us "residual that AFD should predict"
        sample.append({
            "city": city, "date": settle_date,
            "observed": observed,
            "baseline": baseline,
            "old_bias": old_bias,
            "new_high": new_high,
            "new_shift": new_shift,
            "new_conf": new_conf,
            "new_agree": new_agree,
        })
    print(f"[analyze] {len(sample)} rows with both AFD + observed")

    if not sample:
        return

    # 1. Old bias: when AFD said warmer, was the day actually warmer than baseline?
    print()
    print("=" * 96)
    print("OLD prompt: does sign(bias) match sign(observed - baseline)?")
    print("=" * 96)
    n_aligned = n_total = 0
    n_with_baseline = 0
    abs_err_with = 0.0
    abs_err_without = 0.0
    for s in sample:
        if s["baseline"] is None or s["old_bias"] is None:
            continue
        n_with_baseline += 1
        residual = s["observed"] - s["baseline"]
        # Apply old confidence (0.7 LLM)
        applied = max(-3.0, min(3.0, s["old_bias"] * 0.7))
        err_with = abs((s["baseline"] + applied) - s["observed"])
        err_without = abs(s["baseline"] - s["observed"])
        abs_err_with += err_with
        abs_err_without += err_without
        if abs(s["old_bias"]) > 0.1:
            n_total += 1
            if (s["old_bias"] > 0) == (residual > 0):
                n_aligned += 1
    print(f"  rows with both AFD bias + NBM baseline: {n_with_baseline}")
    if n_with_baseline:
        print(f"  Mean |error| WITH AFD shift:    {abs_err_with / n_with_baseline:.2f}°F")
        print(f"  Mean |error| WITHOUT AFD shift: {abs_err_without / n_with_baseline:.2f}°F")
    if n_total:
        print(f"  Sign alignment (excluding zeros): {n_aligned}/{n_total} = "
              f"{100*n_aligned/n_total:.1f}%  (50% = chance)")

    # 2. New explicit point forecast
    print()
    print("=" * 96)
    print("NEW prompt: does explicit expected_high_f beat NBM baseline?")
    print("=" * 96)
    n_explicit = 0
    abs_err_afd = 0.0
    abs_err_nbm = 0.0
    for s in sample:
        if s["new_high"] is None or s["baseline"] is None:
            continue
        n_explicit += 1
        abs_err_afd += abs(s["new_high"] - s["observed"])
        abs_err_nbm += abs(s["baseline"] - s["observed"])
    print(f"  Rows where AFD gave an explicit expected_high_f: {n_explicit}")
    if n_explicit:
        print(f"  Mean |AFD high - observed|: {abs_err_afd / n_explicit:.2f}°F")
        print(f"  Mean |NBM     - observed|:  {abs_err_nbm / n_explicit:.2f}°F")

    # 3. Confidence calibration
    print()
    print("=" * 96)
    print("NEW prompt: does confidence correlate with prediction error?")
    print("=" * 96)
    bands = [(0.0, 0.3, "low"), (0.3, 0.6, "mid"), (0.6, 1.01, "high")]
    for lo, hi, label in bands:
        errors = []
        for s in sample:
            if s["new_conf"] is None or s["new_shift"] is None or s["baseline"] is None:
                continue
            if not (lo <= s["new_conf"] < hi):
                continue
            applied = max(-5.0, min(5.0, s["new_shift"]))
            err = abs((s["baseline"] + applied) - s["observed"])
            errors.append(err)
        if errors:
            print(f"  conf {label:<5} ({lo}-{hi}):  n={len(errors):>3}  "
                  f"mean |err|={statistics.mean(errors):.2f}°F  "
                  f"std={statistics.stdev(errors) if len(errors)>=2 else 0:.2f}")

    # 4. Per-city distribution
    print()
    print("=" * 96)
    print("Per-city: how varied is AFD's signal across days?")
    print("=" * 96)
    from collections import defaultdict
    by_city = defaultdict(list)
    for s in sample:
        if s["new_shift"] is not None:
            by_city[s["city"]].append(s["new_shift"])
    for city in sorted(by_city.keys()):
        vals = by_city[city]
        if len(vals) < 2:
            continue
        print(f"  {city:<13} n={len(vals):>3}  "
              f"mean={statistics.mean(vals):+.2f}  "
              f"std={statistics.stdev(vals):.2f}  "
              f"range=[{min(vals):+.2f}, {max(vals):+.2f}]")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=DB_PATH)
    p.add_argument("--start", default="2026-01-23")
    p.add_argument("--end", default="2026-04-22")
    p.add_argument("--analyze-only", action="store_true",
                   help="Skip fetch + LLM, only re-run the analysis.")
    p.add_argument("--limit-cities", default=None,
                   help="Comma-separated subset (e.g. nyc,miami) for quick tests")
    args = p.parse_args()

    conn = init_db(args.db)
    if not args.analyze_only:
        cities = (tuple(_CITY_WFO.keys()) if not args.limit_cities
                  else tuple(c.strip() for c in args.limit_cities.split(",")))
        stats = fetch_and_extract(conn, args.start, args.end, cities=cities)
        print(f"[afd-backtest] {stats}")
    analyze(conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
