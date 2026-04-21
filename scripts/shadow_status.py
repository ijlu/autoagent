#!/usr/bin/env python3
"""Daily (or on-demand) shadow-MM readiness dashboard.

Replaces the ad-hoc `ssh + sqlite3` ritual for checking whether any
weather series has accumulated enough positive shadow P&L to flip into
LIVE_CANARY. Reads the local DB — run on the VPS, or copy the DB down.

Usage:
    # On VPS:
    sudo -u kalshi python3 scripts/shadow_status.py

    # One-liner from laptop:
    ssh root@45.55.79.193 "sudo -u kalshi python3 /home/kalshi/autoagent/scripts/shadow_status.py"

    # Custom window (default 7 days):
    python3 scripts/shadow_status.py --window-days 3

    # Alternate DB:
    python3 scripts/shadow_status.py --db /path/to/kalshi_trades.db

Sections:
  1. Per-series state + shadow P&L table.
  2. Data-integrity findings (reuses the daemon monitor's rules).
  3. Next-action hint per series, matching the two-gate CANARY rules.

Every piece of data here also lives in the daemon log, but digging
through 300-line health dumps is painful. This is one screen, once.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from typing import Optional

# Make bot.* imports work when invoked as a script.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, _REPO)


DEFAULT_DB_PATHS = [
    "/home/kalshi/autoagent/kalshi_trades.db",     # VPS
    os.path.join(_REPO, "kalshi_trades.db"),        # Local dev
    "kalshi_trades.db",                             # CWD fallback
]


def _find_db(path: Optional[str]) -> str:
    if path:
        if not os.path.isfile(path):
            sys.exit(f"DB not found: {path}")
        return path
    for p in DEFAULT_DB_PATHS:
        if os.path.isfile(p):
            return p
    sys.exit(
        "kalshi_trades.db not found. Pass --db explicitly or run from a "
        "directory that has it."
    )


# ── Section renderers ───────────────────────────────────────────────────


def _fmt_cents(n: Optional[int]) -> str:
    if n is None:
        return "   -  "
    sign = "-" if n < 0 else " "
    return f"{sign}{abs(n)/100:>5.2f}"


def _fmt_int(n: Optional[int]) -> str:
    if n is None:
        return "   -"
    return f"{n:>4d}"


def _per_series_table(conn: sqlite3.Connection, window_s: int) -> list[dict]:
    """Aggregate all the numbers that feed the next-action logic.

    We do this in one big SELECT per series rather than multiple small
    queries because (a) it's the shape the user actually wants to see
    and (b) it minimises round-trips against a locked WAL DB.
    """
    since = int(time.time() - window_s)
    # Early-return if the schema isn't set up — prevents a confusing
    # traceback on first-boot / out-of-tree DBs.
    has_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='weather_mm_shadow'"
    ).fetchone()
    if not has_table:
        return []
    rows = conn.execute(
        """
        SELECT
          series,
          COUNT(*) AS n_quotes,
          SUM(CASE WHEN gate_should_quote=1 THEN 1 ELSE 0 END) AS n_post,
          SUM(CASE WHEN shadow_bid_filled=1 THEN 1 ELSE 0 END) AS n_bid,
          SUM(CASE WHEN shadow_ask_filled=1 THEN 1 ELSE 0 END) AS n_ask,
          SUM(CASE WHEN shadow_bid_filled=1 OR shadow_ask_filled=1
                   THEN 1 ELSE 0 END) AS n_filled_rows,
          SUM(CASE WHEN ticker_settled_yes IS NOT NULL THEN 1 ELSE 0 END)
              AS n_settled,
          SUM(CASE WHEN shadow_pnl_cents IS NOT NULL THEN 1 ELSE 0 END)
              AS n_pnl_rows,
          SUM(shadow_pnl_cents) AS pnl_sum,
          SUM(CASE WHEN live_pnl_cents IS NOT NULL THEN 1 ELSE 0 END)
              AS n_live_pnl,
          SUM(live_pnl_cents) AS live_pnl_sum,
          SUM(CASE WHEN market_yes_bid = 0 OR market_yes_ask = 0
                   THEN 1 ELSE 0 END) AS n_zero_book
        FROM weather_mm_shadow
        WHERE ts_unix >= ?
        GROUP BY series
        ORDER BY series
        """,
        (since,),
    ).fetchall()
    return [
        dict(
            series=r[0], n_quotes=r[1], n_post=r[2], n_bid=r[3], n_ask=r[4],
            n_filled_rows=r[5], n_settled=r[6], n_pnl_rows=r[7],
            pnl_sum=r[8], n_live_pnl=r[9], live_pnl_sum=r[10],
            n_zero_book=r[11],
        ) for r in rows
    ]


def _fetch_live_states(
    conn: sqlite3.Connection, series_list: list[str],
) -> dict[str, str]:
    """One kv_cache lookup per series, cached for both table + action render.

    Previous shape called `get_mm_live_state` twice per series (once in
    `_render_table`, once in `_render_next_actions`) — 2N round-trips on
    the read-only operator connection. One pass, dict-cached, is plenty.
    """
    from bot.learning.mm_promotion import get_mm_live_state
    return {s: get_mm_live_state(conn, s).state for s in series_list}


def _render_table(
    per_series: list[dict], live_states: dict[str, str],
) -> None:
    from bot.config import (
        MM_SIZING_MIN_N,
        MM_CANARY_MIN_PNL_PER_FILL_CENTS,
        MM_GRADUATION_MIN_PAIRED_N,
        MM_GRADUATION_MIN_PNL_RATIO,
    )

    # Column widths tuned to stay inside 120 chars.
    cols = ("series", "state", "n_quotes", "n_post", "fills", "settled",
            "n_pnl", "shadow$", "pnl/fill", "live_n", "live$")
    print(
        f"{cols[0]:<10} {cols[1]:<11} {cols[2]:>8} {cols[3]:>6} "
        f"{cols[4]:>5} {cols[5]:>7} {cols[6]:>5} {cols[7]:>7} "
        f"{cols[8]:>8} {cols[9]:>6} {cols[10]:>6}"
    )
    print("-" * 96)
    for row in per_series:
        series = row["series"]
        state = live_states[series].lower()
        fills = (row["n_bid"] or 0) + (row["n_ask"] or 0)
        n_filled_rows = row["n_filled_rows"] or 0
        pnl_sum = row["pnl_sum"]
        pnl_per_fill = (
            f"{pnl_sum / n_filled_rows:>5.2f}¢"
            if pnl_sum is not None and n_filled_rows > 0 else "   -  "
        )
        print(
            f"{series:<10} {state:<11} "
            f"{_fmt_int(row['n_quotes'])} "
            f"{_fmt_int(row['n_post']):>6} "
            f"{fills:>5} "
            f"{_fmt_int(row['n_settled']):>7} "
            f"{_fmt_int(row['n_pnl_rows']):>5} "
            f"{_fmt_cents(pnl_sum):>7} "
            f"{pnl_per_fill:>8} "
            f"{_fmt_int(row['n_live_pnl']):>6} "
            f"{_fmt_cents(row['live_pnl_sum']):>6}"
        )

    print()
    print(
        f"Gates: CANARY flip requires n_filled_rows >= {MM_SIZING_MIN_N} "
        f"AND pnl/fill >= {MM_CANARY_MIN_PNL_PER_FILL_CENTS:.1f}¢."
    )
    print(
        f"       LIVE_FULL graduation requires "
        f"live_n >= {MM_GRADUATION_MIN_PAIRED_N} AND shadow$>0 AND "
        f"live$/shadow$ >= {MM_GRADUATION_MIN_PNL_RATIO:.2f}."
    )


def _render_integrity(conn: sqlite3.Connection, window_s: int) -> None:
    from bot.daemon.shadow_integrity import check_shadow_data_integrity

    # Reuses the daemon's own check so the dashboard can never disagree
    # with what the monitor would alert on — single source of truth.
    findings = check_shadow_data_integrity(conn, window_s=window_s)
    if not findings:
        print("Data integrity: OK — no findings in window.")
        return
    print("Data integrity findings:")
    for f in findings:
        marker = {"critical": "!!!", "warning": " ! ", "info": " . "}.get(
            f.level, "   "
        )
        print(f"  {marker} [{f.level:<8}] {f.kind:<12} {f.series or '*':<10} "
              f"metric={f.metric:.1f}")
        print(f"       {f.message}")


def _render_next_actions(
    per_series: list[dict], live_states: dict[str, str],
) -> None:
    """Tell the operator exactly what to do about each series.

    The suggested action is deliberately explicit — a "flip with X"
    message means the pre-gate numbers check out. The operator still
    owns the call (human-in-the-loop on the first LIVE flip is a
    deliberate safety gate; see T4 §5).
    """
    from bot.config import (
        MM_SIZING_MIN_N,
        MM_CANARY_MIN_PNL_PER_FILL_CENTS,
        MM_GRADUATION_MIN_PAIRED_N,
        MM_GRADUATION_MIN_PNL_RATIO,
    )
    from bot.learning.directional_shadow import LiveState

    print("Suggested next action per series:")
    for row in per_series:
        series = row["series"]
        state = live_states[series]
        n_filled_rows = row["n_filled_rows"] or 0
        pnl_sum = row["pnl_sum"] or 0
        pnl_per_fill = (pnl_sum / n_filled_rows) if n_filled_rows else 0.0
        n_post = row["n_post"] or 0
        fills = (row["n_bid"] or 0) + (row["n_ask"] or 0)
        n_settled = row["n_settled"] or 0
        n_pnl_rows = row["n_pnl_rows"] or 0
        n_zero = row["n_zero_book"] or 0

        if n_zero > 0:
            print(f"  {series}: CRITICAL — {n_zero} zero-book row(s). "
                  f"Resolve data-integrity finding before any promotion.")
            continue

        if state == LiveState.LIVE_FULL:
            print(f"  {series}: LIVE_FULL — monitor the kill-switch trigger.")
            continue

        if state == LiveState.LIVE_CANARY:
            live_n = row["n_live_pnl"] or 0
            live_sum = row["live_pnl_sum"] or 0
            if live_n < MM_GRADUATION_MIN_PAIRED_N:
                print(
                    f"  {series}: CANARY — {live_n}/{MM_GRADUATION_MIN_PAIRED_N} "
                    f"paired live rows. Hold and accumulate."
                )
                continue
            if pnl_sum <= 0:
                print(
                    f"  {series}: CANARY — live_n={live_n} but shadow$ "
                    f"non-positive. Graduation gate will reject; consider "
                    f"demoting."
                )
                continue
            ratio = live_sum / pnl_sum if pnl_sum else 0.0
            if ratio < MM_GRADUATION_MIN_PNL_RATIO:
                print(
                    f"  {series}: CANARY — live/shadow={ratio:.2f} < "
                    f"{MM_GRADUATION_MIN_PNL_RATIO:.2f}. Shadow is "
                    f"over-predicting; hold."
                )
                continue
            print(
                f"  {series}: CANARY — READY for LIVE_FULL graduation "
                f"(n={live_n}, live/shadow={ratio:.2f})."
            )
            continue

        # SHADOW
        if n_post == 0:
            print(f"  {series}: SHADOW — no posts in window. Series "
                  f"may be blocked or inactive.")
            continue
        if fills == 0:
            print(f"  {series}: SHADOW — {n_post} posts, 0 fills. "
                  f"Structural — diagnose spread / gate / market liquidity.")
            continue
        if n_settled == 0:
            print(f"  {series}: SHADOW — {fills} fills, 0 settled yet. "
                  f"Wait for next settlement batch.")
            continue
        if n_filled_rows < MM_SIZING_MIN_N:
            print(
                f"  {series}: SHADOW — n_filled_rows={n_filled_rows} < "
                f"{MM_SIZING_MIN_N}. Wait for more fills."
            )
            continue
        if n_pnl_rows == 0:
            # Fills exist and markets settled, but annotator hasn't
            # stamped shadow_pnl_cents yet. Distinct from the "edge too
            # low" case: here we simply don't know yet.
            print(
                f"  {series}: SHADOW — {n_filled_rows} fills over "
                f"{n_settled} settled market(s), but no shadow_pnl_cents "
                f"annotated yet. Annotator runs at record_settlements "
                f"time; if stuck, investigate."
            )
            continue
        if pnl_per_fill < MM_CANARY_MIN_PNL_PER_FILL_CENTS:
            print(
                f"  {series}: SHADOW — pnl/fill={pnl_per_fill:.2f}¢ < "
                f"{MM_CANARY_MIN_PNL_PER_FILL_CENTS:.1f}¢. Hold — shadow "
                f"signal isn't edged enough yet."
            )
            continue
        print(
            f"  {series}: SHADOW — READY for LIVE_CANARY flip "
            f"(n={n_filled_rows}, pnl/fill={pnl_per_fill:.2f}¢). "
            f"Review, then set_mm_live_state(conn, '{series}', "
            f"'LIVE_CANARY', manual=True)."
        )


# ── Entry point ─────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", help="Path to kalshi_trades.db", default=None)
    ap.add_argument(
        "--window-days", type=float, default=7.0,
        help="Look-back window in days (default 7). Gates evaluate the "
             "subset of rows within this window.",
    )
    args = ap.parse_args(argv)

    db_path = _find_db(args.db)
    window_s = int(args.window_days * 86400)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA query_only = ON")  # read-only by contract

    print(f"# Shadow-MM status — db={db_path}")
    print(f"# Window: last {args.window_days:.1f} days "
          f"({window_s}s) · now={time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print()

    per_series = _per_series_table(conn, window_s)
    if not per_series:
        print("No weather_mm_shadow rows in window. "
              "(Daemon may not have started yet, or window is too short.)")
        return 0

    live_states = _fetch_live_states(conn, [r["series"] for r in per_series])
    _render_table(per_series, live_states)
    print()
    _render_integrity(conn, window_s)
    print()
    _render_next_actions(per_series, live_states)
    return 0


if __name__ == "__main__":
    sys.exit(main())
