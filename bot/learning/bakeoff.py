"""T2 — Directional-vs-MM bakeoff report.

Reads settled rows from ``alpha_backtest`` and produces a head-to-head
comparison of the two strategies we run in shadow today:

  - ``mm_quote``           — WeatherQuoter's would-post decisions
  - ``directional_shadow`` — directional evaluator's buy/skip decisions

Answers the Phase-1 question "if we re-enabled live trading, which
strategy actually converts signal edge into realized P&L?" by computing,
per (strategy, family) slice:

  * **n_settled** — how many decisions closed with known outcomes
  * **brier_ensemble** — mean (ensemble_p_yes - literal_yes)^2, where
        ``literal_yes`` recovers the canonical YES outcome from the
        side-aware ``won_yes`` column (``CASE WHEN side='yes' THEN won_yes
        ELSE 1 - won_yes END``). See alpha_log.py convention note.
  * **brier_market**  — mean (market_prob_yes - literal_yes)^2 (market-mid slice)
  * **brier_beat**    — ``brier_market - brier_ensemble`` (positive is good)
  * **implied_pnl_cents_sum** — sum of decision-time expected P&L:
        for ``shadow_only`` / ``posted`` rows with price_cents filled, this
        is ``contracts * (market_prob * 100 - price_cents)`` on the chosen
        side — i.e., how much edge we thought we were buying.
  * **realized_pnl_cents_sum** — actual counterfactual P&L from the
        settlement backfill (already computed per-row in alpha_log.py).
  * **realization_ratio** — realized / implied. Under 0.5 means we're
        burning half the edge we thought we had; above 1.0 means positive
        surprise; at-or-near-1 means the edge estimate was honest.

Paired comparison
-----------------

When both strategies fire on the *same ticker* (weather MM quote + a
directional shadow decision on the same market), we can pair them and
ask "on the overlap set, which strategy made more money per decision?"
This is the stricter apples-to-apples gate for Phase-2 re-enable.

The module is pure-read. Zero writes. Safe to run from an audit script
or a cron'd summary report.
"""
from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Optional


# Restrict to the "clean" market-prob slice by default — mid and last are the
# resolutions we trust most (see alpha_log.resolve_market_prob).
CLEAN_MARKET_PROB_SOURCES = ("mid", "last")


@dataclass
class StrategyRollup:
    strategy: str
    family: Optional[str]
    n_settled: int
    brier_ensemble: Optional[float]
    brier_market: Optional[float]
    brier_beat: Optional[float]
    implied_pnl_cents_sum: int
    realized_pnl_cents_sum: int
    realization_ratio: Optional[float]
    # Raw counts for sanity-checking.
    n_with_market_prob: int = 0
    n_with_realized_pnl: int = 0


def _row_implied_pnl_cents(side, price_cents, contracts, market_prob) -> Optional[int]:
    """Edge we *thought* we'd capture, in cents.

    For a buy at price_cents on ``side`` with fair (market-implied) prob of
    winning = ``p_win_market``, the expected cents per contract is
    ``p_win_market * 100 - price_cents`` (ignores fees — we measure gross
    edge because realized_pnl is also gross-of-fee in alpha_backtest today).

    Returns None when any input is missing — caller filters those rows out
    of the implied_pnl_sum aggregate.
    """
    if (
        side is None
        or price_cents is None
        or contracts is None
        or market_prob is None
    ):
        return None
    p_win = market_prob if side == "yes" else (1.0 - market_prob)
    return int(round(contracts * (p_win * 100.0 - price_cents)))


def _brier(prob, outcome_1_or_0) -> float:
    return (prob - outcome_1_or_0) ** 2


def compute_bakeoff(
    conn: sqlite3.Connection,
    *,
    family: Optional[str] = None,
    strategies: tuple[str, ...] = ("mm_quote", "directional_shadow"),
    clean_market_slice: bool = True,
    min_n: int = 1,
) -> list[StrategyRollup]:
    """Return one rollup per (strategy, family) in ``strategies``.

    Parameters
    ----------
    family: restrict to a single family (e.g. ``"KXHIGHNY"``) or None for
        per-family breakouts.
    clean_market_slice: if True, only count rows with
        ``market_prob_source IN ('mid', 'last')`` — the "signal-bearing"
        slice where the market-mid comparison is apples-to-apples.
    min_n: drop rollups with fewer settled rows than this (noise floor).
    """
    cur = conn.cursor()

    where = ["ts_settle_unix IS NOT NULL", "won_yes IS NOT NULL",
             "decision_type IN (" + ",".join("?" * len(strategies)) + ")"]
    params: list = list(strategies)

    if family is not None:
        where.append("family = ?")
        params.append(family)
    if clean_market_slice:
        placeholders = ",".join("?" * len(CLEAN_MARKET_PROB_SOURCES))
        where.append(f"market_prob_source IN ({placeholders})")
        params.extend(CLEAN_MARKET_PROB_SOURCES)

    sql = f"""
        SELECT decision_type, family, side, price_cents, contracts,
               ensemble_p_yes, market_prob_yes, won_yes, realized_pnl_cents
        FROM alpha_backtest
        WHERE {' AND '.join(where)}
    """
    rows = cur.execute(sql, params).fetchall()

    # Group by (strategy, family).
    buckets: dict[tuple[str, Optional[str]], list] = {}
    for r in rows:
        key = (r[0], r[1])
        buckets.setdefault(key, []).append(r)

    results: list[StrategyRollup] = []
    for (strategy, fam), bucket in buckets.items():
        if len(bucket) < min_n:
            continue
        brier_ens, brier_mkt = [], []
        implied_sum, realized_sum = 0, 0
        n_with_market, n_with_realized = 0, 0
        for (_dt, _fam, side, price_c, contracts, ens_p, mkt_p, won, realized) in bucket:
            # Recover literal-YES outcome. ``won_yes`` in alpha_backtest is
            # "did our (side-aware) trade win", not literal YES — flip for
            # NO-side rows. See alpha_log.py convention note.
            if won is not None:
                literal_yes = bool(won) if side == "yes" else (not bool(won))
            else:
                literal_yes = None
            if ens_p is not None and literal_yes is not None:
                brier_ens.append(_brier(ens_p, literal_yes))
            if mkt_p is not None:
                n_with_market += 1
                if literal_yes is not None:
                    brier_mkt.append(_brier(mkt_p, literal_yes))
            ip = _row_implied_pnl_cents(side, price_c, contracts, mkt_p)
            if ip is not None:
                implied_sum += ip
            if realized is not None:
                realized_sum += realized
                n_with_realized += 1

        mean_ens = sum(brier_ens) / len(brier_ens) if brier_ens else None
        mean_mkt = sum(brier_mkt) / len(brier_mkt) if brier_mkt else None
        beat = (mean_mkt - mean_ens) if (mean_ens is not None and mean_mkt is not None) else None
        # Realization ratio is only meaningful when implied_sum != 0.
        ratio: Optional[float] = None
        if implied_sum != 0:
            ratio = realized_sum / implied_sum

        results.append(StrategyRollup(
            strategy=strategy,
            family=fam,
            n_settled=len(bucket),
            brier_ensemble=mean_ens,
            brier_market=mean_mkt,
            brier_beat=beat,
            implied_pnl_cents_sum=implied_sum,
            realized_pnl_cents_sum=realized_sum,
            realization_ratio=ratio,
            n_with_market_prob=n_with_market,
            n_with_realized_pnl=n_with_realized,
        ))

    results.sort(key=lambda r: (r.strategy, r.family or ""))
    return results


@dataclass
class PairedCell:
    ticker: str
    family: Optional[str]
    mm_realized_cents: int
    directional_realized_cents: int
    # Who won on this ticker?
    winner: str  # "mm" | "directional" | "tie"


def compute_paired_tickers(
    conn: sqlite3.Connection,
    *,
    clean_market_slice: bool = True,
) -> list[PairedCell]:
    """For every ticker where BOTH ``mm_quote`` and ``directional_shadow``
    have at least one settled row, aggregate realized_pnl_cents on each side
    and call a per-ticker winner.

    This is the stricter "same-ticker, same-outcome" comparison — eliminates
    confounders (family mix, market regime) that the per-strategy aggregate
    can't control for.
    """
    cur = conn.cursor()

    where = ["ts_settle_unix IS NOT NULL",
             "decision_type IN ('mm_quote', 'directional_shadow')",
             "realized_pnl_cents IS NOT NULL"]
    if clean_market_slice:
        where.append(
            "market_prob_source IN (" +
            ",".join("?" * len(CLEAN_MARKET_PROB_SOURCES)) + ")"
        )
        params: list = list(CLEAN_MARKET_PROB_SOURCES)
    else:
        params = []

    sql = f"""
        SELECT ticker, family, decision_type,
               SUM(realized_pnl_cents) AS pnl
        FROM alpha_backtest
        WHERE {' AND '.join(where)}
        GROUP BY ticker, family, decision_type
    """
    rows = cur.execute(sql, params).fetchall()

    # Pivot into per-ticker dicts.
    per_ticker: dict[str, dict[str, int]] = {}
    fams: dict[str, Optional[str]] = {}
    for (ticker, family, dt, pnl) in rows:
        per_ticker.setdefault(ticker, {})[dt] = int(pnl or 0)
        fams[ticker] = family

    pairs: list[PairedCell] = []
    for ticker, legs in per_ticker.items():
        if "mm_quote" not in legs or "directional_shadow" not in legs:
            continue
        mm = legs["mm_quote"]
        di = legs["directional_shadow"]
        if mm > di:
            winner = "mm"
        elif di > mm:
            winner = "directional"
        else:
            winner = "tie"
        pairs.append(PairedCell(
            ticker=ticker, family=fams.get(ticker),
            mm_realized_cents=mm,
            directional_realized_cents=di,
            winner=winner,
        ))

    pairs.sort(key=lambda p: (p.family or "", p.ticker))
    return pairs


def format_report(
    rollups: list[StrategyRollup],
    paired: list[PairedCell],
) -> str:
    """Render a markdown bakeoff summary suitable for dropping into a report."""
    lines: list[str] = []
    lines.append("# Strategy bakeoff — alpha_backtest settled rows")
    lines.append("")
    if not rollups:
        lines.append("_No settled rows matched the filter yet._")
        lines.append("")
    else:
        lines.append("## Per-strategy × family rollup")
        lines.append("")
        lines.append(
            "| strategy | family | n | Brier(ens) | Brier(mkt) | beat | "
            "implied¢ | realized¢ | ratio |"
        )
        lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
        for r in rollups:
            def fmt(x, p=3): return f"{x:.{p}f}" if isinstance(x, (int, float)) and x is not None else "—"
            lines.append(
                f"| {r.strategy} | {r.family or '—'} | {r.n_settled} | "
                f"{fmt(r.brier_ensemble)} | {fmt(r.brier_market)} | "
                f"{fmt(r.brier_beat)} | {r.implied_pnl_cents_sum} | "
                f"{r.realized_pnl_cents_sum} | {fmt(r.realization_ratio, 2)} |"
            )
        lines.append("")

    lines.append("## Paired-ticker head-to-head (both strategies fired)")
    lines.append("")
    if not paired:
        lines.append(
            "_No tickers yet where both mm_quote and directional_shadow "
            "fired and settled._"
        )
        return "\n".join(lines) + "\n"

    mm_wins = sum(1 for p in paired if p.winner == "mm")
    di_wins = sum(1 for p in paired if p.winner == "directional")
    ties = sum(1 for p in paired if p.winner == "tie")
    mm_total = sum(p.mm_realized_cents for p in paired)
    di_total = sum(p.directional_realized_cents for p in paired)

    lines.append(
        f"**{len(paired)} paired tickers.** MM wins {mm_wins}, "
        f"Directional wins {di_wins}, ties {ties}. "
        f"Total MM P&L: {mm_total}¢. Total Directional P&L: {di_total}¢."
    )
    lines.append("")
    lines.append("| ticker | family | MM¢ | Dir¢ | winner |")
    lines.append("|---|---|---:|---:|---|")
    for p in paired:
        lines.append(
            f"| {p.ticker} | {p.family or '—'} | "
            f"{p.mm_realized_cents} | {p.directional_realized_cents} | "
            f"{p.winner} |"
        )
    return "\n".join(lines) + "\n"


def render_bakeoff_report(
    conn: sqlite3.Connection,
    *,
    family: Optional[str] = None,
    clean_market_slice: bool = True,
    min_n: int = 1,
) -> str:
    """One-call convenience: run both aggregates and format the report."""
    rollups = compute_bakeoff(
        conn, family=family, clean_market_slice=clean_market_slice, min_n=min_n,
    )
    paired = compute_paired_tickers(
        conn, clean_market_slice=clean_market_slice,
    )
    return format_report(rollups, paired)
