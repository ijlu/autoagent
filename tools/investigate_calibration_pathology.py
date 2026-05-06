#!/usr/bin/env python3
"""Investigate the 0.9-1.0 bucket pathology in weather_mm_shadow.

Research questions:
  1. Is the pathology concentrated in bracket (-B) vs threshold (-T) markets?
  2. Does `running_high_f >= threshold` correlate correctly with settlement?
  3. Is the bucket dominated by a few high-activity tickers (confound)?
  4. Does time-in-lifecycle (hours_left) correlate with bucket membership?
  5. Per-family breakdown — is the pathology in one family or all?
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict

sys.path.insert(0, ".")

from bot.db import init_db


def _suffix(ticker: str) -> str:
    parts = ticker.split("-")
    if not parts:
        return "other"
    s = parts[-1]
    if s.startswith("T"):
        return "T"
    if s.startswith("B"):
        return "B"
    return "other"


def _family(ticker: str) -> str:
    return ticker.split("-")[0] if ticker else "?"


def _bucket(fv: int) -> str:
    p = fv / 100.0
    lo = int(p * 10) / 10
    if lo >= 1.0:
        lo = 0.9
    return f"{lo:.1f}-{lo + 0.1:.1f}"


def main(db: str = "kalshi_trades.db") -> int:
    conn = init_db(db)

    rows = conn.execute("""
        SELECT
            ticker, series, station,
            fair_value_cents,
            ticker_settled_yes,
            running_high_f, forecast_high_f, hours_left,
            old_temp_f, new_temp_f
        FROM weather_mm_shadow
        WHERE ts_settle_unix IS NOT NULL
          AND ticker_settled_yes IS NOT NULL
          AND fair_value_cents IS NOT NULL
    """).fetchall()

    print(f"Total settled shadow rows: {len(rows):,}\n")

    # ── 1. Suffix × bucket breakdown ─────────────────────────────────────
    print("=" * 80)
    print(" 1. Calibration by (suffix, bucket)")
    print("=" * 80)
    print(f"  {'suffix':<8} {'bucket':<12} {'n':>6} {'avg_est':>8} "
          f"{'yes_rate':>9} {'bias':>8}")
    by_sb: dict[tuple[str, str], list[tuple[float, int]]] = defaultdict(list)
    for r in rows:
        sfx = _suffix(r[0])
        b = _bucket(r[3])
        by_sb[(sfx, b)].append((r[3] / 100.0, r[4]))
    for (sfx, b) in sorted(by_sb):
        samples = by_sb[(sfx, b)]
        n = len(samples)
        if n < 10:
            continue
        avg_est = sum(x[0] for x in samples) / n
        yes_rate = sum(x[1] for x in samples) / n
        bias = avg_est - yes_rate
        print(f"  {sfx:<8} {b:<12} {n:>6} {avg_est:>8.4f} "
              f"{yes_rate:>9.4f} {bias:>+8.4f}")

    # ── 2. Per-family bucket breakdown ───────────────────────────────────
    print()
    print("=" * 80)
    print(" 2. Calibration by (family, bucket)")
    print("=" * 80)
    print(f"  {'family':<12} {'bucket':<12} {'n':>6} {'avg_est':>8} "
          f"{'yes_rate':>9} {'bias':>8}")
    by_fb: dict[tuple[str, str], list[tuple[float, int]]] = defaultdict(list)
    for r in rows:
        fam = _family(r[0])
        b = _bucket(r[3])
        by_fb[(fam, b)].append((r[3] / 100.0, r[4]))
    for (fam, b) in sorted(by_fb):
        samples = by_fb[(fam, b)]
        n = len(samples)
        if n < 20:
            continue
        avg_est = sum(x[0] for x in samples) / n
        yes_rate = sum(x[1] for x in samples) / n
        bias = avg_est - yes_rate
        print(f"  {fam:<12} {b:<12} {n:>6} {avg_est:>8.4f} "
              f"{yes_rate:>9.4f} {bias:>+8.4f}")

    # ── 3. 0.9-1.0 bucket: unique tickers, distribution ──────────────────
    print()
    print("=" * 80)
    print(" 3. 0.9-1.0 bucket forensics — what tickers dominate?")
    print("=" * 80)
    hi = [r for r in rows if r[3] >= 90]
    print(f"  Total rows in bucket: {len(hi):,}")
    tickers = Counter(r[0] for r in hi)
    print(f"  Unique tickers:       {len(tickers):,}")
    print(f"  Top 15 tickers by shadow-row count in bucket:")
    print(f"    {'ticker':<30} {'rows':>5} {'yes':>4} "
          f"{'avg_fv':>7} {'avg_rh':>7} {'avg_fh':>7}")
    by_ticker: dict[str, list] = defaultdict(list)
    for r in hi:
        by_ticker[r[0]].append(r)
    for tk, _n in tickers.most_common(15):
        tk_rows = by_ticker[tk]
        n = len(tk_rows)
        yes = sum(r[4] for r in tk_rows) / n
        avg_fv = sum(r[3] for r in tk_rows) / n
        avg_rh = sum(r[5] for r in tk_rows if r[5] is not None) / max(
            1, sum(1 for r in tk_rows if r[5] is not None)
        )
        avg_fh = sum(r[6] for r in tk_rows if r[6] is not None) / max(
            1, sum(1 for r in tk_rows if r[6] is not None)
        )
        print(f"    {tk:<30} {n:>5} {yes:>4.2f} "
              f"{avg_fv:>7.1f} {avg_rh:>7.1f} {avg_fh:>7.1f}")

    # ── 4. 0.9-1.0 bucket: random 20 rows with full context ──────────────
    print()
    print("=" * 80)
    print(" 4. 0.9-1.0 bucket — random 20-row deep dump")
    print("=" * 80)
    import random
    random.seed(42)
    sample = random.sample(hi, min(20, len(hi)))
    print(f"  {'ticker':<30} {'fv':>3} {'sett':>4} {'rh':>6} "
          f"{'fh':>6} {'hrs':>5} {'new_t':>6}")
    for r in sample:
        tk = r[0]
        fv = r[3]
        settled = r[4]
        rh = r[5] if r[5] is not None else -999
        fh = r[6] if r[6] is not None else -999
        hrs = r[7] if r[7] is not None else -999
        nt = r[9] if r[9] is not None else -999
        print(f"  {tk:<30} {fv:>3} {settled:>4} "
              f"{rh:>6.1f} {fh:>6.1f} {hrs:>5.1f} {nt:>6.1f}")

    # ── 5. 0.9-1.0 bucket: aggregated outcome by ticker-level ────────────
    print()
    print("=" * 80)
    print(" 5. 0.9-1.0 bucket — DEDUPED by ticker (one row per market)")
    print("=" * 80)
    # Most shadow rows are duplicates of the same ticker across its lifecycle.
    # Let's see the per-ticker YES rate: does the same market resolve
    # YES once? If the bucket is 90% NO tickers, then the FV was just
    # wrong on those markets' outcomes.
    by_tk_outcome: dict[str, int] = {}
    for r in hi:
        tk = r[0]
        by_tk_outcome[tk] = r[4]  # settled_yes is constant per ticker
    n_tk = len(by_tk_outcome)
    yes_tk = sum(by_tk_outcome.values())
    print(f"  Unique tickers:       {n_tk}")
    print(f"  Resolved YES:         {yes_tk} ({100*yes_tk/max(1,n_tk):.1f}%)")
    print(f"  Resolved NO:          {n_tk - yes_tk} ({100*(n_tk-yes_tk)/max(1,n_tk):.1f}%)")

    # ── 6. 0.9-1.0 bucket — is it mostly "running_high past threshold" cases? ──
    print()
    print("=" * 80)
    print(" 6. 0.9-1.0 bucket — decompose by WeatherQuoter code path")
    print("=" * 80)
    import re
    paths = Counter()
    path_yes = defaultdict(list)
    for r in hi:
        tk, series, station, fv, settled, rh, fh, hrs = r[:8]
        sfx = _suffix(tk)
        if rh is None:
            paths["missing_rh"] += 1
            continue
        if sfx == "T":
            m = re.search(r"-[TtBb](-?\d+\.?\d*)", tk)
            if not m:
                paths["T_unparsed"] += 1
                continue
            thresh = float(m.group(1))
            if rh >= thresh + 3:
                key = "T: rh>=thresh+3 (quoter → 0.98)"
            elif rh >= thresh + 1:
                key = "T: rh>=thresh+1 (quoter → 0.96)"
            elif rh >= thresh:
                key = "T: rh>=thresh (quoter → 0.95)"
            else:
                key = "T: below thresh (forecast-driven)"
            paths[key] += 1
            path_yes[key].append(settled)
        elif sfx == "B":
            m = re.search(r"-[Bb](-?\d+\.?\d*)", tk)
            if not m:
                paths["B_unparsed"] += 1
                continue
            floor = float(m.group(1))
            cap = floor + 2.0  # the fallback assumption in _parse_market
            if rh >= cap:
                key = "B: rh>=cap (quoter → 0.02)  [CONTRADICTION]"
            elif rh >= floor:
                key = "B: rh in bracket (CDF-based)"
            else:
                key = "B: below floor (forecast-driven)"
            paths[key] += 1
            path_yes[key].append(settled)
    print(f"  {'code path':<50} {'n':>5} {'yes_rate':>9}")
    for p, n in paths.most_common():
        if n < 10:
            continue
        ys = path_yes.get(p, [])
        yes = sum(ys) / max(1, len(ys)) if ys else 0
        print(f"  {p:<50} {n:>5} {yes:>9.3f}")

    # ── 7. 0.0-0.1 bucket under-estimate — diagnostic ────────────────────
    print()
    print("=" * 80)
    print(" 7. 0.0-0.1 bucket — what's there and why under-estimated?")
    print("=" * 80)
    lo = [r for r in rows if r[3] < 10]
    n_lo = len(lo)
    yes_lo = sum(r[4] for r in lo)
    print(f"  Total rows:     {n_lo:,}")
    print(f"  Resolved YES:   {yes_lo:,} ({100*yes_lo/max(1,n_lo):.1f}%)")
    # Per-suffix
    by_sfx: dict[str, list] = defaultdict(list)
    for r in lo:
        by_sfx[_suffix(r[0])].append(r)
    for s in sorted(by_sfx):
        srows = by_sfx[s]
        n = len(srows)
        yes = sum(r[4] for r in srows)
        print(f"    {s}: {n:,} rows, {yes:,} yes ({100*yes/max(1,n):.1f}%)")

    return 0


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else "kalshi_trades.db"
    sys.exit(main(db))
