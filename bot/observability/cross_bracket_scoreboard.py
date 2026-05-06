"""Cross-bracket performance scoreboard library.

Used by:
  - tools/cross_bracket_scoreboard.py — CLI for ad-hoc inspection
  - bot/daemon/main.py — daily scheduled task that emits a summary to the
    daemon log so we have a longitudinal record of strategy performance

Sources of truth:
  - settlements.profit_cents — net of fees (per trade.py:3637)
  - fills_ledger.fee_cents — explicit per-fill fees, populated since the
    T3.1 deploy (2026-04-21+)

We deliberately do NOT derive fees from settlements arithmetic
(``revenue - cost - profit``); the legacy multi-leg writer's
revenue/cost columns don't reconcile cleanly across cross-bracket
portfolios, but profit_cents is correct because the writer formula is
``profit = revenue - cost - fee`` and only profit is used downstream.
"""

from __future__ import annotations

import sqlite3
import time
from collections import defaultdict
from typing import Optional

# Cross-bracket went live for KXHIGHNY on 2026-05-03; the rest of the
# weather families today (2026-05-04 ~16:30 UTC). This is our reference
# epoch — anything before is pre-cross-bracket-live and not in scope.
CROSS_BRACKET_EPOCH_ISO = "2026-05-03"


def _family_from_ticker(ticker: str) -> str:
    return ticker.split("-")[0] if ticker else "unknown"


def _fmt_cents(c: Optional[float]) -> str:
    if c is None:
        return "-".rjust(8)
    return f"{c/100:+.2f}".rjust(8)


def gather_settlements(conn: sqlite3.Connection, since_iso: str) -> list[dict]:
    """Cross-bracket settlements with profit_cents already net of fees."""
    rows = conn.execute(
        """SELECT recorded_at, ticker, strategy, contracts, price_cents,
                  revenue_cents, profit_cents, won, side
           FROM settlements
           WHERE ticker LIKE 'KXHIGH%'
             AND recorded_at >= ?
             AND strategy != 'mm:mm_v1'
           ORDER BY recorded_at""",
        (since_iso,),
    ).fetchall()
    return [
        {
            "recorded_at": r[0], "ticker": r[1], "strategy": r[2],
            "contracts": r[3] or 0, "price_cents": r[4] or 0,
            "revenue_cents": r[5] or 0, "profit_cents": r[6] or 0,
            "won": r[7], "side": r[8],
            "family": _family_from_ticker(r[1]),
        }
        for r in rows
    ]


def gather_fills(conn: sqlite3.Connection, since_iso: str) -> list[dict]:
    """Fills from fills_ledger (post-T3.1) — has explicit fee_cents."""
    rows = conn.execute(
        """SELECT fill_ts_iso, ticker, family, side, action, contracts,
                  yes_price_cents, no_price_cents, is_taker, fee_cents,
                  live_mode, source
           FROM fills_ledger
           WHERE ticker LIKE 'KXHIGH%'
             AND fill_ts_iso >= ?
           ORDER BY fill_ts_iso""",
        (since_iso,),
    ).fetchall()
    cols = (
        "fill_ts_iso", "ticker", "family", "side", "action", "contracts",
        "yes_price_cents", "no_price_cents", "is_taker", "fee_cents",
        "live_mode", "source",
    )
    return [dict(zip(cols, r)) for r in rows]


def summarize_window(settlements: list[dict], fills: list[dict]) -> dict:
    """Aggregate one window's data into a summary dict."""
    s_count = len(settlements)
    s_net_pnl = sum(s["profit_cents"] for s in settlements)
    s_wins = sum(1 for s in settlements if s["won"])
    s_contracts = sum(s["contracts"] for s in settlements)

    by_family = defaultdict(lambda: {
        "settlements": 0, "contracts": 0, "net_pnl": 0, "wins": 0,
    })
    for s in settlements:
        d = by_family[s["family"]]
        d["settlements"] += 1
        d["contracts"] += s["contracts"]
        d["net_pnl"] += s["profit_cents"]
        d["wins"] += int(bool(s["won"]))

    live_fills = [f for f in fills if f.get("live_mode")]
    live_fees = sum(f.get("fee_cents") or 0 for f in live_fills)
    live_contracts = sum(f.get("contracts") or 0 for f in live_fills)
    live_cost = sum(
        ((f.get("yes_price_cents") or 0) if f.get("side") == "yes"
         else (f.get("no_price_cents") or 0))
        * (f.get("contracts") or 0)
        for f in live_fills
    )
    live_taker = sum(1 for f in live_fills if f.get("is_taker"))
    live_maker = len(live_fills) - live_taker

    fees_by_family = defaultdict(lambda: {
        "fills": 0, "fees_cents": 0, "contracts": 0,
    })
    for f in live_fills:
        fam = f.get("family") or _family_from_ticker(f.get("ticker") or "")
        d = fees_by_family[fam]
        d["fills"] += 1
        d["fees_cents"] += f.get("fee_cents") or 0
        d["contracts"] += f.get("contracts") or 0

    return {
        "settlements": {
            "count": s_count,
            "contracts": s_contracts,
            "net_pnl_cents": s_net_pnl,
            "win_rate": (s_wins / s_count) if s_count else None,
            "wins": s_wins,
            "by_family": dict(by_family),
        },
        "fills_ledger": {
            "count": len(live_fills),
            "contracts": live_contracts,
            "fees_paid_cents": live_fees,
            "cost_cents": live_cost,
            "taker_count": live_taker,
            "maker_count": live_maker,
            "by_family": dict(fees_by_family),
        },
    }


def render_window(label: str, summary: dict) -> str:
    """Format one window's summary as a printable block (multi-line string)."""
    lines = [f"━━━ {label} ━━━"]

    s = summary["settlements"]
    win_rate_str = f"{s['win_rate']*100:.0f}%" if s["win_rate"] is not None else "-"
    lines.append(
        f"  SETTLED (net of fees): n={s['count']:>3} contracts={s['contracts']:>4}  "
        f"net_pnl={_fmt_cents(s['net_pnl_cents'])}  win_rate={win_rate_str}"
    )
    if s["count"] and s["contracts"]:
        per_contract = s["net_pnl_cents"] / s["contracts"]
        lines.append(f"    → avg net P&L per contract: {_fmt_cents(per_contract)}")
    if s["by_family"]:
        lines.append(f"  net P&L by family:")
        lines.append(f"    {'family':<12}{'n':>4}{'contracts':>11}{'net_pnl':>10}{'win_rate':>10}")
        for fam in sorted(s["by_family"]):
            d = s["by_family"][fam]
            wr = f"{100*d['wins']/d['settlements']:.0f}%" if d['settlements'] else "-"
            lines.append(
                f"    {fam:<12}{d['settlements']:>4}{d['contracts']:>11}"
                f"{_fmt_cents(d['net_pnl'])}{wr:>10}"
            )

    f = summary["fills_ledger"]
    if f["count"]:
        lines.append(
            f"  FILLS_LEDGER (canonical fees, post-T3.1 deploy):"
        )
        lines.append(
            f"    n={f['count']} contracts={f['contracts']} "
            f"taker={f['taker_count']} maker={f['maker_count']}"
        )
        lines.append(
            f"    fees_paid={_fmt_cents(f['fees_paid_cents'])} "
            f"premium_paid={_fmt_cents(f['cost_cents'])}"
        )
        if f["count"]:
            avg_fee = f["fees_paid_cents"] / f["count"]
            lines.append(f"    → avg fee per fill: {_fmt_cents(avg_fee)}")
        if f["by_family"]:
            lines.append(f"    fees by family:")
            for fam in sorted(f["by_family"]):
                d = f["by_family"][fam]
                lines.append(
                    f"      {fam:<12}fills={d['fills']:>3} "
                    f"contracts={d['contracts']:>4} "
                    f"fees={_fmt_cents(d['fees_cents'])}"
                )
    else:
        lines.append("  FILLS_LEDGER: empty (no fills since T3.1 deploy or none in window)")
    return "\n".join(lines)


def build_scoreboard(
    conn: sqlite3.Connection,
    now_unix: Optional[float] = None,
) -> dict[str, dict]:
    """Build the standard 24h / 7d / all-time scoreboard. Returns a dict
    keyed by window label."""
    now_unix = now_unix if now_unix is not None else time.time()
    iso24 = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now_unix - 24 * 3600))
    iso7 = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now_unix - 7 * 86400))

    out: dict[str, dict] = {}
    for label, since in (
        ("Last 24h", iso24),
        ("Last 7d", iso7),
        (f"All since {CROSS_BRACKET_EPOCH_ISO}", CROSS_BRACKET_EPOCH_ISO),
    ):
        s = gather_settlements(conn, since)
        f = gather_fills(conn, since)
        out[label] = summarize_window(s, f)
    return out


def render_scoreboard(scoreboard: dict[str, dict], header: Optional[str] = None) -> str:
    """Format a full scoreboard as a single multi-line string."""
    parts = []
    if header:
        parts.append(header)
        parts.append("")
    for label, summary in scoreboard.items():
        parts.append(render_window(label, summary))
        parts.append("")
    return "\n".join(parts).rstrip()
