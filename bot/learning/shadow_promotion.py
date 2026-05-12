"""Shadow-to-live promotion + auto-demote (Phase 1 step 9).

Decides when a per-family `directional_live:<FAMILY>` kv flag flips between
SHADOW ↔ LIVE_CANARY ↔ LIVE_FULL. Reads settled `alpha_backtest` rows, writes
transitions to `promotion_events`, and mutates kv_cache via
`bot.learning.directional_shadow.set_live_state`.

Promotion = option D + H from the 2026-04-17 design debate:

    shadow → canary : Brier beat baseline ≥ 0.005, realized P&L ≥ $0 after
                      fees, N ≥ 50 settled shadow decisions, *and the
                      out-of-sample (second-half) slice also clears the gate*.
    canary → full   : 30 live canary settlements have elapsed AND the kill
                      switch has not tripped during that window.

Demotion = option F (trifecta kill switch):

    1. Realized P&L on the last 30 settled LIVE rows < max($30, 3% equity)
    2. Live rolling-30 Brier > shadow baseline Brier + 0.03 (regime shift)
    3. Single-trade loss > 5% equity (hard stop; catches config/code bugs)

Graduated demotion: full → canary → shadow. A demote to shadow is terminal
for that family until an operator manually re-enables via
`set_live_state(..., manual=True)` using the ratcheted gate
(`DEFAULT_RATCHETED_EDGE_BEAT = 0.010`, 2× first-promotion bar).

Design notes:

* All family-level stats come from `alpha_backtest`. No joins against
  `settlements` — step 6's `fill_settlement_for_ticker` already back-fills
  `ts_settle_unix` + `won_yes` + `realized_pnl_cents` on each alpha row.
* `ensemble_p_yes` in alpha_backtest is canonical P(YES). Brier uses the
  YES-side outcome regardless of our `side`, so there's no per-side
  normalization to get wrong.
* Baseline Brier = always predicting the market-implied P(YES) at decision
  time. We store `market_prob_yes` per alpha row; when null, baseline falls
  back to 0.5.
* N is "settled rows" not "all rows" — pending decisions don't count toward
  either direction of the gate.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from bot.config import DIRECTIONAL_BLOCKED_FAMILIES
from bot.daemon.locks import DB_WRITE_LOCK
from bot.db import db_write_ctx
from bot.learning.directional_shadow import (
    DEFAULT_MIN_CANARY_SETTLED,
    DEFAULT_MIN_EDGE_BEAT,
    DEFAULT_MIN_SETTLED,
    DEFAULT_RATCHETED_EDGE_BEAT,
    LiveFlag,
    LiveState,
    get_live_state,
    set_live_state,
)

logger = logging.getLogger(__name__)


# ── Tunable thresholds ──────────────────────────────────────────────────
# Auto-demote kill switch (option F + trifecta C from 2026-04-17 debate).
# Bundled into a dataclass so the promotion-sweep job can be re-run with
# different thresholds during dry-run tuning without surgery on constants.
@dataclass(frozen=True)
class KillSwitchConfig:
    # Trigger 1: realized P&L floor on last-N live settled
    pnl_window_n: int = 30
    pnl_floor_dollars: float = 30.0         # min absolute $ loss before trip
    pnl_floor_equity_pct: float = 0.03      # or 3% of equity, whichever larger
    # Trigger 2: Brier regime-shift
    brier_window_n: int = 30
    brier_regime_delta: float = 0.03        # live Brier > shadow_brier + 0.03
    # Trigger 3: single-trade hard stop
    single_loss_equity_pct: float = 0.05    # any one loss > 5% equity


DEFAULT_KILL_SWITCH = KillSwitchConfig()


# Minimum number of live settled decisions before ANY kill-switch trigger
# is eligible to fire. Below this, noise dominates and we'd demote good
# families on a 4-trade losing streak. Matches the N≥20 floor from the
# 2026-04-17 kill-switch debate.
MIN_LIVE_SETTLED_FOR_DEMOTION = 20

# Canary state minimum dwell time (settled live decisions) before a
# canary → full auto-promotion is considered. Distinct from
# MIN_LIVE_SETTLED_FOR_DEMOTION because canary dwell is a promotion
# criterion, not a demotion one.
MIN_CANARY_SETTLED = DEFAULT_MIN_CANARY_SETTLED


# ── Family-level stats ──────────────────────────────────────────────────
@dataclass(frozen=True)
class FamilyStats:
    """Aggregates computed from alpha_backtest for one family / decision subset.

    `brier` is the mean Brier score under our model; `baseline_brier` is the
    mean Brier if we'd predicted `market_prob_yes` each time (or 0.5 when
    market_prob_yes is null). `edge_beat` = baseline_brier - brier (positive
    = our model better).

    All P&L fields are in dollars (cents / 100), sign-correct: positive is
    profit.
    """
    n: int
    brier: float
    baseline_brier: float
    edge_beat: float
    realized_pnl_dollars: float
    max_single_loss_dollars: float


def _brier(our_p_yes: float, literal_yes: bool) -> float:
    """Brier score against the canonical literal-YES outcome.

    Callers MUST pass literal_yes (did the YES outcome settle), NOT the
    raw ``alpha_backtest.won_yes`` column which stores "did our (side-aware)
    trade win" and is flipped for NO-side rows. See alpha_log.py convention
    note at fill_settlement and ``tools/validate_cross_bracket.py``.
    """
    return (float(our_p_yes) - (1.0 if literal_yes else 0.0)) ** 2


def _market_baseline_p_yes(market_prob_yes: Optional[float]) -> float:
    """Baseline prediction = market-implied P(YES) at decision time.

    Falls back to 0.5 when the market snapshot didn't capture a price (rare
    — only possible when yes_bid / yes_ask / yes_last were all None).
    """
    if market_prob_yes is None:
        return 0.5
    return max(0.0, min(1.0, float(market_prob_yes)))


def _rows_to_stats(rows: list[sqlite3.Row]) -> FamilyStats:
    """Collapse a list of settled alpha_backtest rows into FamilyStats."""
    if not rows:
        return FamilyStats(0, 0.0, 0.0, 0.0, 0.0, 0.0)
    n = len(rows)
    brier_sum = 0.0
    base_sum = 0.0
    pnl_cents = 0
    max_loss_cents = 0
    for r in rows:
        p = float(r["ensemble_p_yes"])
        # Recover literal-YES outcome from the side-aware won_yes column.
        # See alpha_log.py convention note: won_yes==1 means "our trade won",
        # which equals literal-YES only for side='yes' rows. ~99% of weather
        # rows are side='no', so failing to flip here would invert every
        # Brier on weather (the original bug).
        side = r["side"]
        won_yes_stored = bool(r["won_yes"])
        literal_yes = won_yes_stored if side == "yes" else (not won_yes_stored)
        brier_sum += _brier(p, literal_yes)
        base_sum += _brier(_market_baseline_p_yes(r["market_prob_yes"]), literal_yes)
        pnl = int(r["realized_pnl_cents"] or 0)
        pnl_cents += pnl
        if pnl < max_loss_cents:  # track most-negative single-trade P&L
            max_loss_cents = pnl
    brier = brier_sum / n
    baseline = base_sum / n
    return FamilyStats(
        n=n,
        brier=brier,
        baseline_brier=baseline,
        edge_beat=baseline - brier,
        realized_pnl_dollars=pnl_cents / 100.0,
        max_single_loss_dollars=max_loss_cents / 100.0,
    )


def _fetch_settled_rows(
    conn: sqlite3.Connection,
    family: str,
    *,
    outcomes: tuple[str, ...],
    since_unix: Optional[float] = None,
    limit: Optional[int] = None,
) -> list[sqlite3.Row]:
    """Pull settled alpha_backtest rows for one family, ordered oldest → newest.

    `outcomes` selects which decision_outcome values to include (e.g.
    ("shadow_only",) for the promotion gate, ("posted","filled") for the
    kill-switch).

    `since_unix` filters rows by ts_decision_unix — used to bound canary-era
    settlements to "since this family entered canary state".

    `limit` clips to the most-recent N rows (SQL orders DESC then we reverse).
    """
    # Use a local cursor with its own row_factory — never mutate the shared
    # daemon connection's row_factory, which would race with other threads
    # (see T0.2 and regression 15 in CLAUDE.md).
    cursor = conn.cursor()
    cursor.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in outcomes)
    sql = (
        "SELECT ensemble_p_yes, market_prob_yes, won_yes, side, "
        "       realized_pnl_cents, ts_decision_unix "
        "FROM alpha_backtest "
        "WHERE family = ? "
        f"  AND decision_outcome IN ({placeholders}) "
        "  AND ts_settle_unix IS NOT NULL "
    )
    params: list[Any] = [family.upper(), *outcomes]
    if since_unix is not None:
        sql += "  AND ts_decision_unix >= ? "
        params.append(since_unix)
    sql += "ORDER BY ts_decision_unix DESC"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    rows = cursor.execute(sql, params).fetchall()
    return list(reversed(rows))  # oldest → newest


# ── Promotion gate (shadow → canary) ────────────────────────────────────
def evaluate_promotion(
    conn: sqlite3.Connection,
    family: str,
    *,
    min_settled: int = DEFAULT_MIN_SETTLED,
    min_edge_beat: float = DEFAULT_MIN_EDGE_BEAT,
) -> tuple[bool, str, dict[str, Any]]:
    """Check whether a shadow family should promote to canary.

    Returns (promote?, reason, metrics_dict). Reason is human-readable;
    metrics_dict is serialized verbatim into `promotion_events.metrics_json`.

    Applies option H (out-of-sample split): the second-half slice of settled
    shadow rows must ALSO clear edge_beat ≥ min_edge_beat. Prevents
    graduation on a hot tail-streak.
    """
    rows = _fetch_settled_rows(conn, family, outcomes=("shadow_only",))
    n = len(rows)
    if n < min_settled:
        return (False, f"insufficient_shadow_n={n}<{min_settled}",
                {"n_shadow_settled": n})

    full = _rows_to_stats(rows)
    half = n // 2
    oos = _rows_to_stats(rows[half:])  # second (OOS) half
    metrics = {
        "n_shadow_settled": n,
        "brier": full.brier,
        "baseline_brier": full.baseline_brier,
        "edge_beat": full.edge_beat,
        "realized_pnl_dollars": full.realized_pnl_dollars,
        "oos_n": oos.n,
        "oos_brier": oos.brier,
        "oos_baseline_brier": oos.baseline_brier,
        "oos_edge_beat": oos.edge_beat,
        "oos_realized_pnl_dollars": oos.realized_pnl_dollars,
    }
    if full.edge_beat < min_edge_beat:
        return (False,
                f"edge_beat={full.edge_beat:.4f}<{min_edge_beat:.4f}",
                metrics)
    if oos.edge_beat < min_edge_beat:
        return (False,
                f"oos_edge_beat={oos.edge_beat:.4f}<{min_edge_beat:.4f}",
                metrics)
    if full.realized_pnl_dollars < 0.0:
        return (False,
                f"realized_pnl=${full.realized_pnl_dollars:.2f}<0",
                metrics)
    return (True,
            f"passed:n={n},edge_beat={full.edge_beat:.4f},"
            f"pnl=${full.realized_pnl_dollars:.2f}",
            metrics)


# ── Canary → full auto-promotion ────────────────────────────────────────
def evaluate_canary_graduation(
    conn: sqlite3.Connection,
    family: str,
    flag: LiveFlag,
    *,
    min_canary_settled: int = MIN_CANARY_SETTLED,
) -> tuple[bool, str, dict[str, Any]]:
    """Check whether a canary family should auto-promote to full.

    Criterion: at least `min_canary_settled` settled live decisions occurred
    AFTER `flag.since_ts_unix` AND kill-switch does not fire on the window.

    Returns (promote?, reason, metrics).
    """
    rows = _fetch_settled_rows(
        conn, family,
        outcomes=("posted", "filled"),
        since_unix=flag.since_ts_unix,
    )
    stats = _rows_to_stats(rows)
    metrics = {
        "n_canary_settled": stats.n,
        "canary_brier": stats.brier,
        "canary_pnl_dollars": stats.realized_pnl_dollars,
        "canary_max_loss_dollars": stats.max_single_loss_dollars,
        "canary_since_ts_unix": flag.since_ts_unix,
    }
    if stats.n < min_canary_settled:
        return (False,
                f"canary_n={stats.n}<{min_canary_settled}",
                metrics)
    if stats.realized_pnl_dollars < 0.0:
        return (False,
                f"canary_pnl=${stats.realized_pnl_dollars:.2f}<0",
                metrics)
    return (True,
            f"canary_graduated:n={stats.n},pnl=${stats.realized_pnl_dollars:.2f}",
            metrics)


# ── Kill switch (demotion trifecta) ─────────────────────────────────────
def evaluate_kill_switch(
    conn: sqlite3.Connection,
    family: str,
    equity_dollars: float,
    *,
    cfg: KillSwitchConfig = DEFAULT_KILL_SWITCH,
) -> tuple[bool, str, dict[str, Any]]:
    """Check the trifecta kill switch on live settled rows.

    Returns (trip?, reason, metrics). Does not mutate state — caller decides
    whether tripping demotes to canary or shadow based on current state.

    The three triggers (from the 2026-04-17 debate):

      1. Realized P&L on last N live settled < −max($floor, equity_pct*equity)
      2. Rolling-N live Brier > shadow_baseline_brier + regime_delta
      3. Any one live trade realized P&L < −equity_pct * equity (hard stop)

    All three are OR'd — any one trips the switch. The N≥20 floor applies
    to triggers 1 and 2 (hard stop fires at N=1).
    """
    # Pull LIVE settlements (posted or filled outcomes)
    live_rows = _fetch_settled_rows(
        conn, family,
        outcomes=("posted", "filled"),
        limit=max(cfg.pnl_window_n, cfg.brier_window_n),
    )
    n_live = len(live_rows)
    metrics: dict[str, Any] = {
        "equity_dollars": equity_dollars,
        "n_live_settled": n_live,
    }

    # Trigger 3 (hard stop) fires immediately on any single large loss,
    # even at N=1 — this is the code-bug tripwire.
    single_loss_floor = -abs(cfg.single_loss_equity_pct * equity_dollars)
    for r in live_rows:
        pnl_d = int(r["realized_pnl_cents"] or 0) / 100.0
        if pnl_d <= single_loss_floor:
            metrics["tripped_single_loss_dollars"] = pnl_d
            metrics["single_loss_floor_dollars"] = single_loss_floor
            return (True,
                    f"single_trade_loss=${pnl_d:.2f}<=${single_loss_floor:.2f}",
                    metrics)

    if n_live < MIN_LIVE_SETTLED_FOR_DEMOTION:
        return (False,
                f"insufficient_live_n={n_live}<{MIN_LIVE_SETTLED_FOR_DEMOTION}",
                metrics)

    # Trigger 1: P&L floor on last pnl_window_n live settled
    pnl_window = live_rows[-cfg.pnl_window_n:]
    pnl_stats = _rows_to_stats(pnl_window)
    floor_dollars = -max(cfg.pnl_floor_dollars,
                         cfg.pnl_floor_equity_pct * equity_dollars)
    metrics["live_pnl_window_dollars"] = pnl_stats.realized_pnl_dollars
    metrics["live_pnl_floor_dollars"] = floor_dollars
    if pnl_stats.realized_pnl_dollars <= floor_dollars:
        return (True,
                f"live_pnl=${pnl_stats.realized_pnl_dollars:.2f}"
                f"<=floor=${floor_dollars:.2f}",
                metrics)

    # Trigger 2: Brier regime-shift vs shadow baseline
    brier_window = live_rows[-cfg.brier_window_n:]
    live_stats = _rows_to_stats(brier_window)
    shadow_rows = _fetch_settled_rows(conn, family, outcomes=("shadow_only",))
    shadow_stats = _rows_to_stats(shadow_rows)
    metrics["live_brier"] = live_stats.brier
    metrics["shadow_baseline_brier"] = shadow_stats.brier
    metrics["brier_regime_delta"] = live_stats.brier - shadow_stats.brier
    if (shadow_stats.n > 0
            and live_stats.brier > shadow_stats.brier + cfg.brier_regime_delta):
        return (True,
                f"live_brier={live_stats.brier:.4f}>shadow_brier="
                f"{shadow_stats.brier:.4f}+{cfg.brier_regime_delta:.2f}",
                metrics)

    return (False, "kill_switch_clear", metrics)


# ── Orchestration ───────────────────────────────────────────────────────
def _log_event(
    conn: sqlite3.Connection,
    family: str,
    *,
    old_state: str,
    new_state: str,
    reason: str,
    trigger: str,
    metrics: dict[str, Any],
    manual: bool,
) -> None:
    """Insert a promotion_events row. Takes DB_WRITE_LOCK."""
    now = time.time()
    iso = datetime.fromtimestamp(now, tz=timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    try:
        with db_write_ctx(conn):
            conn.execute(
                "INSERT INTO promotion_events "
                "(ts_unix,ts_iso,family,old_state,new_state,reason,"
                " trigger,metrics_json,manual) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (now, iso, family.upper(), old_state, new_state, reason,
                 trigger, json.dumps(metrics), int(bool(manual))),
            )
    except Exception as exc:  # pragma: no cover — logging is best-effort
        logger.warning("[shadow-promotion] event log failed: %s", exc)


def _transition(
    conn: sqlite3.Connection,
    family: str,
    *,
    from_state: str,
    to_state: str,
    reason: str,
    trigger: str,
    metrics: dict[str, Any],
) -> None:
    """Apply a state change and record the event."""
    set_live_state(conn, family, to_state, manual=False)
    _log_event(
        conn, family,
        old_state=from_state, new_state=to_state,
        reason=reason, trigger=trigger, metrics=metrics, manual=False,
    )
    logger.info(
        "[shadow-promotion] %s: %s → %s (%s / %s)",
        family, from_state, to_state, trigger, reason,
    )


def _candidate_families(conn: sqlite3.Connection) -> list[str]:
    """All distinct families currently present in alpha_backtest, minus blocks."""
    rows = conn.execute(
        "SELECT DISTINCT family FROM alpha_backtest "
        "WHERE family IS NOT NULL AND family != ''"
    ).fetchall()
    return [r[0] for r in rows if r[0] not in DIRECTIONAL_BLOCKED_FAMILIES]


def run_promotion_sweep(
    conn: sqlite3.Connection,
    *,
    equity_dollars: float,
    families: Optional[list[str]] = None,
    kill_switch: KillSwitchConfig = DEFAULT_KILL_SWITCH,
) -> dict[str, Any]:
    """One pass over all families: promote, graduate, or demote as warranted.

    Intended to run daily from the daemon scheduler. `equity_dollars` comes
    from the cycle's equity snapshot (balance + portfolio value).

    Order of operations per family matters:
      1. Kill switch first — if live and bleeding, demote before considering
         any graduation.
      2. Canary → full — promotes a surviving canary to full size.
      3. Shadow → canary — first-time promotion from shadow.

    Returns a summary dict used by the scheduler health logger.
    """
    fams = families if families is not None else _candidate_families(conn)
    summary: dict[str, Any] = {
        "checked": len(fams),
        "promoted": [],
        "graduated": [],
        "demoted": [],
        "unchanged": [],
    }
    for fam in fams:
        flag = get_live_state(conn, fam)

        # 1. Kill switch — only meaningful in a live state
        if flag.state != LiveState.SHADOW:
            tripped, reason, metrics = evaluate_kill_switch(
                conn, fam, equity_dollars, cfg=kill_switch,
            )
            if tripped:
                # Graduated demotion: full → canary; canary → shadow.
                new_state = (
                    LiveState.LIVE_CANARY
                    if flag.state == LiveState.LIVE_FULL
                    else LiveState.SHADOW
                )
                _transition(
                    conn, fam,
                    from_state=flag.state, to_state=new_state,
                    reason=reason, trigger="kill_switch", metrics=metrics,
                )
                summary["demoted"].append(
                    {"family": fam, "from": flag.state, "to": new_state,
                     "reason": reason}
                )
                continue

        # 2. Canary → full auto-graduation
        if flag.state == LiveState.LIVE_CANARY:
            graduate, reason, metrics = evaluate_canary_graduation(
                conn, fam, flag,
            )
            if graduate:
                _transition(
                    conn, fam,
                    from_state=LiveState.LIVE_CANARY,
                    to_state=LiveState.LIVE_FULL,
                    reason=reason, trigger="canary_graduation",
                    metrics=metrics,
                )
                summary["graduated"].append(
                    {"family": fam, "reason": reason}
                )
                continue

        # 3. Shadow → canary first promotion
        if flag.state == LiveState.SHADOW:
            promote, reason, metrics = evaluate_promotion(conn, fam)
            if promote:
                _transition(
                    conn, fam,
                    from_state=LiveState.SHADOW,
                    to_state=LiveState.LIVE_CANARY,
                    reason=reason, trigger="shadow_promotion",
                    metrics=metrics,
                )
                summary["promoted"].append(
                    {"family": fam, "reason": reason}
                )
                continue

        summary["unchanged"].append({"family": fam, "state": flag.state})

    return summary


def manual_re_enable(
    conn: sqlite3.Connection,
    family: str,
    *,
    target_state: str = LiveState.LIVE_CANARY,
    min_edge_beat: float = DEFAULT_RATCHETED_EDGE_BEAT,
) -> tuple[bool, str]:
    """Operator-only re-enable after an auto-demote to shadow.

    Applies the ratcheted gate (default 2× first-promotion edge_beat) to
    prevent flip-flop between shadow and live on the same underlying signal.
    Writes the kv row with `manual=True` so future auto-demotes to shadow
    leave a clear audit trail that this family was human-reinstated.

    Returns (enabled?, reason).
    """
    if target_state not in (LiveState.LIVE_CANARY, LiveState.LIVE_FULL):
        raise ValueError(f"manual re-enable target must be live, got {target_state!r}")
    promote, reason, metrics = evaluate_promotion(
        conn, family, min_edge_beat=min_edge_beat,
    )
    if not promote:
        return (False, f"ratcheted_gate_failed:{reason}")
    set_live_state(conn, family, target_state, manual=True)
    _log_event(
        conn, family,
        old_state=LiveState.SHADOW, new_state=target_state,
        reason=f"manual_re_enable:{reason}",
        trigger="manual", metrics=metrics, manual=True,
    )
    return (True, reason)
