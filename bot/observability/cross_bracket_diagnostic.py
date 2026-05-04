"""Cross-bracket shadow-vs-realized diagnostic.

Answers a single question: did real-money cross-bracket P&L match the
shadow EV the strategy expected at decision time?

Per settled position:
  - Realized P&L: ``settlements.profit_cents`` (already net of fees per
    trade.py:3637 — ``profit = revenue - cost - fee``)
  - Shadow expected P&L: derived from the latest alpha_backtest row for
    the same (ticker, side) before the settlement, using the formula
    ``p_our_side * 100 - decision_price - estimated_fee`` per contract.
  - Delta: realized − shadow. Negative delta = shadow overstated.

Why this matters: if shadow EV consistently overstates realized P&L
(say by 50%+), the strategy is hitting adverse selection / stale
quotes that the simulator doesn't model. That's the "do not expand"
signal. If realized matches shadow within ±20%, the simulator is a
faithful predictor and we can ramp.

Caveats:
  - alpha_backtest doesn't carry the actual fill price (only the
    decision-time price), so slippage isn't measured here. For
    post-T3.1 fills, fills_ledger.yes_price_cents/no_price_cents has
    the actual fill price — a future v2 can join those.
  - Fees on legacy (pre-T3.1) settlements are estimated via
    kalshi_taker_fee since fills_ledger isn't populated for those.
    The actual settlements.profit_cents subtracts the real fee, so
    realized side is always honest; only the shadow side uses the
    estimate.
  - "Most recent decision before settle" is a heuristic — cross-bracket
    logs every 5 min, so multiple decisions per ticker exist. Latest
    decision is the most relevant because it's closest to fill time;
    earlier decisions for the same position would have been superseded.
"""

from __future__ import annotations

import sqlite3
import time
from collections import defaultdict
from typing import Optional

from bot.core.money import kalshi_taker_fee


CROSS_BRACKET_EPOCH_ISO = "2026-05-03"


def _family_from_ticker(ticker: str) -> str:
    return ticker.split("-")[0] if ticker else "unknown"


def _fmt_cents(c: Optional[float]) -> str:
    if c is None:
        return "-".rjust(8)
    return f"{c/100:+.2f}".rjust(8)


def _p_our_side(p_yes: float, side: str) -> float:
    """Convert canonical P(YES) to P(our_side)."""
    if side == "yes":
        return p_yes
    if side == "no":
        return 1.0 - p_yes
    return p_yes  # unknown side, assume yes


def gather_diagnostic_rows(conn: sqlite3.Connection, since_iso: str) -> list[dict]:
    """For each settled cross-bracket position, find the matching
    decision-time row and compute shadow-vs-realized comparison."""
    settlements = conn.execute(
        """SELECT recorded_at, ticker, side, contracts, price_cents,
                  profit_cents, won
           FROM settlements
           WHERE ticker LIKE 'KXHIGH%'
             AND strategy != 'mm:mm_v1'
             AND recorded_at >= ?
           ORDER BY recorded_at""",
        (since_iso,),
    ).fetchall()

    out = []
    for s_ts, s_ticker, s_side, s_contracts, s_price, s_profit, s_won in settlements:
        # Most recent cross-bracket decision for this (ticker, side) before settle.
        decision = conn.execute(
            """SELECT ts_decision, ts_decision_unix, ensemble_p_yes,
                      raw_ensemble_p_yes, market_prob_yes,
                      side, price_cents, decision_outcome
               FROM alpha_backtest
               WHERE ticker = ? AND side = ?
                 AND notes LIKE 'cross_bracket;%'
                 AND ts_decision <= ?
               ORDER BY ts_decision_unix DESC
               LIMIT 1""",
            (s_ticker, s_side, s_ts),
        ).fetchone()

        row: dict = {
            "ticker": s_ticker,
            "family": _family_from_ticker(s_ticker),
            "side": s_side,
            "contracts": s_contracts or 0,
            "realized_pnl_cents": s_profit or 0,
            "settle_recorded_at": s_ts,
            "won": bool(s_won),
        }

        if decision is None:
            row["decision_found"] = False
            row["shadow_pnl_cents"] = None
            row["delta_cents"] = None
            row["delta_pct"] = None
            out.append(row)
            continue

        d_ts, d_ts_unix, p_yes, raw_p_yes, mkt_p_yes, d_side, d_price, _outcome = decision

        # Shadow EV per contract = p_our_side * 100 - decision_price - est_fee
        # (Profit margin if we win minus cost minus fee, weighted by win prob.)
        p_our = _p_our_side(p_yes or 0.5, d_side or s_side)
        # Estimate fee per contract via taker formula (cross-bracket is often
        # taker — limit at best_ask + slip tolerance, frequently crosses).
        est_fee_per_contract = kalshi_taker_fee(1, int(d_price or 50))

        shadow_pnl_per_contract = (p_our * 100.0) - (d_price or 0) - est_fee_per_contract
        shadow_pnl_total = shadow_pnl_per_contract * (s_contracts or 0)

        # Slippage: requires both prices to be on the same side-convention.
        # alpha_backtest.price_cents is the price-of-our-side (the price we
        # paid for buying our side). settlements.price_cents has a different
        # convention (appears to be YES-equivalent on multi-leg positions),
        # so direct subtraction is unreliable for legacy data. For going-
        # forward fills, fills_ledger has yes_price_cents/no_price_cents in
        # an unambiguous convention — use that when available.
        fill_row = conn.execute(
            """SELECT yes_price_cents, no_price_cents
               FROM fills_ledger
               WHERE ticker = ? AND side = ?
                 AND fill_ts_iso <= ?
               ORDER BY fill_ts_unix DESC LIMIT 1""",
            (s_ticker, s_side, s_ts),
        ).fetchone()
        if fill_row:
            yp, np_ = fill_row
            fill_price_our_side = (yp if s_side == "yes" else np_) or 0
            slippage_cents = fill_price_our_side - (d_price or 0)
            slippage_source = "fills_ledger"
        else:
            slippage_cents = None
            slippage_source = "unavailable_legacy"

        row.update({
            "decision_found": True,
            "decision_ts": d_ts,
            "decision_p_yes": p_yes,
            "decision_raw_p_yes": raw_p_yes,
            "decision_market_p_yes": mkt_p_yes,
            "decision_p_our_side": p_our,
            "decision_price_cents": d_price,
            "fill_avg_price_cents": s_price,
            "slippage_cents": slippage_cents,
            "slippage_source": slippage_source,
            "est_fee_per_contract_cents": est_fee_per_contract,
            "shadow_pnl_cents": round(shadow_pnl_total),
            "delta_cents": round(s_profit - shadow_pnl_total),
            "delta_pct": (
                round((s_profit - shadow_pnl_total) / abs(shadow_pnl_total) * 100, 1)
                if shadow_pnl_total else None
            ),
            "decision_to_settle_seconds": (
                (time.mktime(time.strptime(s_ts[:19], "%Y-%m-%dT%H:%M:%S")) - d_ts_unix)
                if d_ts_unix else None
            ),
        })
        out.append(row)
    return out


def aggregate_diagnostic(rows: list[dict]) -> dict:
    """Aggregate row-level data into headline stats."""
    n = len(rows)
    n_with_decision = sum(1 for r in rows if r.get("decision_found"))
    realized_total = sum(r["realized_pnl_cents"] for r in rows)
    shadow_total = sum(
        r["shadow_pnl_cents"] for r in rows
        if r.get("shadow_pnl_cents") is not None
    )
    delta_total = realized_total - shadow_total

    by_family = defaultdict(lambda: {
        "n": 0, "contracts": 0, "realized": 0, "shadow": 0,
    })
    for r in rows:
        d = by_family[r["family"]]
        d["n"] += 1
        d["contracts"] += r["contracts"]
        d["realized"] += r["realized_pnl_cents"]
        if r.get("shadow_pnl_cents") is not None:
            d["shadow"] += r["shadow_pnl_cents"]

    # Slippage stats — only count rows where slippage is from fills_ledger
    # (legacy settlements have an unreliable price convention, see
    # gather_diagnostic_rows for why).
    slip = [
        r["slippage_cents"] for r in rows
        if r.get("slippage_cents") is not None
        and r.get("slippage_source") == "fills_ledger"
    ]
    avg_slip = (sum(slip) / len(slip)) if slip else None
    n_slip_legacy = sum(
        1 for r in rows if r.get("slippage_source") == "unavailable_legacy"
    )

    return {
        "n_positions": n,
        "n_with_decision": n_with_decision,
        "n_orphan_settles": n - n_with_decision,
        "realized_total_cents": realized_total,
        "shadow_total_cents": shadow_total,
        "delta_total_cents": delta_total,
        "delta_pct": (
            round(delta_total / abs(shadow_total) * 100, 1)
            if shadow_total else None
        ),
        "avg_slippage_cents": avg_slip,
        "n_slippage_legacy": n_slip_legacy,
        "by_family": dict(by_family),
    }


def render_diagnostic(rows: list[dict], summary: dict, header: Optional[str] = None) -> str:
    """Format diagnostic output as a multi-line printable block."""
    lines: list[str] = []
    if header:
        lines.append(header)
        lines.append("")

    # Per-position table
    if not rows:
        lines.append("No settled cross-bracket positions in this window.")
        return "\n".join(lines)

    lines.append(f"━━━ Per-position shadow vs realized ━━━")
    lines.append(
        f"{'ticker':<28}{'side':>4}{'cts':>5}"
        f"{'shadow':>10}{'realized':>10}{'Δ':>9}{'Δ%':>7}{'slip':>6}"
    )
    for r in rows:
        sh = (
            _fmt_cents(r["shadow_pnl_cents"])
            if r.get("shadow_pnl_cents") is not None else "  no_dec"
        )
        rl = _fmt_cents(r["realized_pnl_cents"])
        dl = (
            _fmt_cents(r["delta_cents"])
            if r.get("delta_cents") is not None else "    -"
        )
        dp = (
            f"{r['delta_pct']:+.0f}%" if r.get("delta_pct") is not None else "  -"
        )
        if r.get("slippage_source") == "fills_ledger":
            slip = f"{r['slippage_cents']:+d}¢"
        elif r.get("slippage_source") == "unavailable_legacy":
            slip = " legacy"  # legacy data, conventions don't reconcile
        else:
            slip = "  -"
        lines.append(
            f"{r['ticker']:<28}{r['side']:>4}{r['contracts']:>5}"
            f"{sh:>10}{rl:>10}{dl:>9}{dp:>7}{slip:>6}"
        )
    lines.append("")

    # Aggregate
    s = summary
    lines.append(f"━━━ Aggregate ━━━")
    lines.append(
        f"  positions: {s['n_positions']}  with_decision: {s['n_with_decision']}  "
        f"orphan_settles: {s['n_orphan_settles']}"
    )
    lines.append(
        f"  shadow_total:   {_fmt_cents(s['shadow_total_cents'])}"
    )
    lines.append(
        f"  realized_total: {_fmt_cents(s['realized_total_cents'])}"
    )
    delta_pct_str = (
        f"{s['delta_pct']:+.1f}%" if s.get("delta_pct") is not None else "  -"
    )
    lines.append(
        f"  delta:          {_fmt_cents(s['delta_total_cents'])}  ({delta_pct_str})"
    )
    if s.get("avg_slippage_cents") is not None:
        lines.append(
            f"  avg slippage (fills_ledger basis): "
            f"{s['avg_slippage_cents']:+.2f}¢ per contract"
        )
    if s.get("n_slippage_legacy", 0):
        lines.append(
            f"  ({s['n_slippage_legacy']} legacy positions: pre-T3.1, "
            f"slippage unavailable)"
        )

    if s["by_family"]:
        lines.append("")
        lines.append("  by family:")
        lines.append(
            f"    {'family':<12}{'n':>4}{'cts':>5}"
            f"{'shadow':>10}{'realized':>10}{'Δ':>9}"
        )
        for fam in sorted(s["by_family"]):
            d = s["by_family"][fam]
            lines.append(
                f"    {fam:<12}{d['n']:>4}{d['contracts']:>5}"
                f"{_fmt_cents(d['shadow'])}{_fmt_cents(d['realized'])}"
                f"{_fmt_cents(d['realized'] - d['shadow'])}"
            )

    # Decision-rule guidance
    lines.append("")
    lines.append("━━━ Read ━━━")
    if s["n_positions"] < 5:
        lines.append(
            f"  N={s['n_positions']} is too small for confident inference."
            " Pattern only — wait for ≥10 positions."
        )
    elif s.get("delta_pct") is None or s["shadow_total_cents"] == 0:
        lines.append("  No shadow signal to compare.")
    elif s["delta_pct"] > -20:
        lines.append(
            f"  delta={s['delta_pct']:+.0f}% — realized ≈ shadow. "
            "Faithful simulator. Continue accumulating data."
        )
    elif s["delta_pct"] > -50:
        lines.append(
            f"  delta={s['delta_pct']:+.0f}% — shadow overstating moderately. "
            "Likely some adverse selection or slippage. Monitor closely."
        )
    else:
        lines.append(
            f"  delta={s['delta_pct']:+.0f}% — shadow significantly overstating. "
            "Real fills are losing what shadow predicted as wins. "
            "Pause expansion; investigate root cause."
        )

    return "\n".join(lines)


def build_diagnostic(
    conn: sqlite3.Connection,
    since_iso: Optional[str] = None,
) -> tuple[list[dict], dict]:
    """Build diagnostic for a window. Returns (rows, summary)."""
    since_iso = since_iso or CROSS_BRACKET_EPOCH_ISO
    rows = gather_diagnostic_rows(conn, since_iso)
    summary = aggregate_diagnostic(rows)
    return rows, summary
