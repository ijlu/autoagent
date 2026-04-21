"""Automatic parameter promotion from shadow testing.

Checks shadow evaluation results and promotes parameters that prove better,
with stress-gated safety controls and full version tracking.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from bot.db import db_write_ctx

# ---------------------------------------------------------------------------
# Local guardrails (canonical copy; mirrors bot.config.GUARDRAILS but kept
# self-contained so this module never silently inherits unrelated changes).
# ---------------------------------------------------------------------------

GUARDRAILS = {
    "max_config_change_pct": 0.50,
    "require_min_samples": 20,
    "cooldown_hours": 24,
    "max_daily_modifications": 3,
    "stress_gate_threshold": 0.8,
    "auto_revert_after_n_losses": 10,
    "min_improvement_pct": 0.15,
    "stressed_improvement_pct": 0.20,
    "protected_params": {
        "DAILY_LOSS_LIMIT",
        "MAX_DRAWDOWN",
        "MM_MAX_INVENTORY",
        "MAX_CONTRACTS",
        "PHASE_CONFIG",
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _table_exists(conn, table_name: str) -> bool:
    """Return True if *table_name* exists in the database."""
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _ensure_config_version_table(conn) -> bool:
    """Create the config_versions audit table if missing.  Returns True on success."""
    try:
        with db_write_ctx(conn):
            conn.execute("""CREATE TABLE IF NOT EXISTS config_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                param_name TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                reason TEXT,
                evidence_n INTEGER DEFAULT 0,
                version INTEGER DEFAULT 1,
                created_at TEXT NOT NULL
            )""")
        return True
    except Exception:
        return False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Core: auto-promote shadow params
# ---------------------------------------------------------------------------

def auto_promote_shadow_params(conn, stress_level: float) -> list[dict]:
    """Check shadow testing results and promote parameters that prove better.

    Safety gates:
    - Minimum 20 samples before any promotion.
    - Shadow must outperform by >15 % (>20 % when stressed).
    - Stressed + loosening change = BLOCKED.
    - Same param cooldown: once per 24 h.
    - Daily cap: max 3 promotions total.
    - Protected params can never be auto-modified.

    Returns a list of dicts describing each promotion (empty if none).
    """
    promotions: list[dict] = []

    # ---- prerequisite tables ------------------------------------------------
    if not _table_exists(conn, "hyperparam_shadow"):
        return promotions
    if not _table_exists(conn, "learned_config"):
        return promotions
    _ensure_config_version_table(conn)

    # ---- daily limit check --------------------------------------------------
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()

    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM config_versions WHERE created_at >= ?",
            (today_start,),
        ).fetchone()
        daily_count = row[0] if row else 0
    except Exception:
        daily_count = 0

    if daily_count >= GUARDRAILS["max_daily_modifications"]:
        return promotions

    # ---- gather shadow evaluation groups ------------------------------------
    #
    # We join shadow records to settlements (via trades) to compute actual P&L
    # for both the real and shadow contract sizes.  Only groups with enough
    # settled samples are considered.
    #
    min_samples = GUARDRAILS["require_min_samples"]

    try:
        rows = conn.execute("""
            SELECT h.param_name,
                   h.current_value,
                   h.shadow_value,
                   COUNT(*)                              AS n,
                   SUM(CASE WHEN s.profit_cents IS NOT NULL
                       THEN (s.profit_cents * 1.0 / CASE WHEN s.contracts > 0
                             THEN s.contracts ELSE 1 END) * h.actual_contracts
                       ELSE 0 END)                       AS actual_profit,
                   SUM(CASE WHEN s.profit_cents IS NOT NULL
                       THEN (s.profit_cents * 1.0 / CASE WHEN s.contracts > 0
                             THEN s.contracts ELSE 1 END) * h.shadow_contracts
                       ELSE 0 END)                       AS shadow_profit
            FROM hyperparam_shadow h
            JOIN trades t ON h.ticker = t.ticker
                AND ABS(julianday(h.recorded_at) - julianday(t.timestamp)) < 0.01
            JOIN settlements s ON t.order_id = s.order_id
            GROUP BY h.param_name, h.shadow_value
            HAVING COUNT(*) >= ?
        """, (min_samples,)).fetchall()
    except Exception:
        return promotions

    if not rows:
        return promotions

    # ---- evaluate each candidate --------------------------------------------
    stressed = stress_level > GUARDRAILS["stress_gate_threshold"]
    improvement_bar = (
        GUARDRAILS["stressed_improvement_pct"] if stressed
        else GUARDRAILS["min_improvement_pct"]
    )

    for param_name, current_val, shadow_val, n, actual_profit, shadow_profit in rows:
        # --- protected params ------------------------------------------------
        if param_name.upper() in GUARDRAILS["protected_params"]:
            continue

        # --- check improvement -----------------------------------------------
        if actual_profit == 0:
            # Avoid division by zero; require positive shadow profit as minimum.
            if shadow_profit <= 0:
                continue
            improvement = 1.0  # infinite improvement, cap at 100 %
        else:
            improvement = (shadow_profit - actual_profit) / abs(actual_profit)

        if improvement < improvement_bar:
            continue

        # --- stressed + loosening = BLOCK ------------------------------------
        if stressed:
            try:
                current_num = float(current_val)
                shadow_num = float(shadow_val)
                # "Loosening" means the new value allows MORE risk.
                # For most params a larger value is looser (bigger kelly, bigger
                # position size).  For edge thresholds, a *smaller* value is
                # looser (lower bar to trade).
                is_edge_like = "edge" in param_name.lower()
                if is_edge_like:
                    loosening = shadow_num < current_num
                else:
                    loosening = shadow_num > current_num
                if loosening:
                    continue
            except (ValueError, TypeError):
                # Non-numeric; can't determine direction — block to be safe.
                continue

        # --- max change magnitude --------------------------------------------
        try:
            c = float(current_val)
            s = float(shadow_val)
            if c != 0 and abs(s - c) / abs(c) > GUARDRAILS["max_config_change_pct"]:
                continue
        except (ValueError, TypeError):
            pass  # non-numeric; skip magnitude check

        # --- cooldown --------------------------------------------------------
        cooldown_seconds = GUARDRAILS["cooldown_hours"] * 3600
        cutoff_iso = datetime.fromtimestamp(
            time.time() - cooldown_seconds, tz=timezone.utc
        ).isoformat()

        try:
            recent = conn.execute(
                "SELECT 1 FROM config_versions WHERE param_name = ? AND created_at >= ? LIMIT 1",
                (param_name, cutoff_iso),
            ).fetchone()
            if recent:
                continue
        except Exception:
            pass  # table missing — no cooldown history, allow

        # --- daily cap re-check (may have promoted another param above) ------
        if daily_count + len(promotions) >= GUARDRAILS["max_daily_modifications"]:
            break

        # ---- all gates passed — promote! ------------------------------------
        reason = (
            f"Shadow value {shadow_val} outperformed current {current_val} "
            f"by {improvement:.0%} over {n} settled trades"
        )

        try:
            _write_learned_config(conn, param_name, current_val, shadow_val, reason)
            record_config_version(conn, param_name, current_val, shadow_val, reason, n)
        except Exception:
            continue

        promotion = {
            "param": param_name,
            "old_value": current_val,
            "new_value": shadow_val,
            "improvement_pct": round(improvement, 4),
            "evidence_n": n,
            "reason": reason,
            "timestamp": _now_iso(),
        }
        promotions.append(promotion)
        print(f"[self-mod] PROMOTED {param_name}: {current_val} -> {shadow_val} "
              f"({improvement:.0%} better, n={n})")

    return promotions


# ---------------------------------------------------------------------------
# Version tracking
# ---------------------------------------------------------------------------

def record_config_version(
    conn,
    param_name: str,
    old_value,
    new_value,
    reason: str,
    evidence_count: int,
) -> None:
    """Record a config change with full provenance in config_versions.

    Auto-increments a per-param version counter.
    """
    _ensure_config_version_table(conn)

    # Determine next version for this param
    try:
        row = conn.execute(
            "SELECT MAX(version) FROM config_versions WHERE param_name = ?",
            (param_name,),
        ).fetchone()
        next_version = (row[0] or 0) + 1 if row else 1
    except Exception:
        next_version = 1

    try:
        with db_write_ctx(conn):
            conn.execute(
                """INSERT INTO config_versions
                   (param_name, old_value, new_value, reason, evidence_n, version, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    param_name,
                    str(old_value),
                    str(new_value),
                    reason,
                    evidence_count,
                    next_version,
                    _now_iso(),
                ),
            )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Auto-revert
# ---------------------------------------------------------------------------

def auto_revert_if_underperforming(conn, stress_level: float) -> list[dict]:
    """Check if recently promoted params are causing losses and revert them.

    Looks at trades executed since the most recent promotion of each param.
    If there are N+ consecutive underperforming trades (negative profit),
    the param is reverted to its previous value.

    Returns a list of revert dicts (empty if nothing reverted).
    """
    reverts: list[dict] = []

    if not _table_exists(conn, "config_versions"):
        return reverts
    if not _table_exists(conn, "learned_config"):
        return reverts

    loss_threshold = GUARDRAILS["auto_revert_after_n_losses"]

    # Get the most recent promotion for each param
    try:
        recent_promotions = conn.execute("""
            SELECT param_name, old_value, new_value, created_at
            FROM config_versions
            WHERE reason NOT LIKE '%AUTO-REVERT%'
            GROUP BY param_name
            HAVING created_at = MAX(created_at)
        """).fetchall()
    except Exception:
        return reverts

    for param_name, old_value, new_value, promoted_at in recent_promotions:
        # Count consecutive losing trades since the promotion
        try:
            trades = conn.execute(
                """SELECT profit_cents FROM settlements
                   WHERE recorded_at >= ?
                   ORDER BY recorded_at ASC""",
                (promoted_at,),
            ).fetchall()
        except Exception:
            continue

        if len(trades) < loss_threshold:
            continue

        # Check for N consecutive losses (profit_cents <= 0)
        consecutive_losses = 0
        max_consecutive = 0
        for (profit,) in trades:
            if profit is not None and profit <= 0:
                consecutive_losses += 1
                max_consecutive = max(max_consecutive, consecutive_losses)
            else:
                consecutive_losses = 0

        if max_consecutive < loss_threshold:
            continue

        # ---- Revert ----------------------------------------------------------
        reason = (
            f"AUTO-REVERT: {max_consecutive} consecutive losing trades after "
            f"promoting {param_name} from {old_value} to {new_value}"
        )

        try:
            _write_learned_config(conn, param_name, new_value, old_value, reason)
            record_config_version(conn, param_name, new_value, old_value, reason, len(trades))
        except Exception:
            continue

        revert = {
            "param": param_name,
            "reverted_from": new_value,
            "reverted_to": old_value,
            "consecutive_losses": max_consecutive,
            "trades_since_promotion": len(trades),
            "reason": reason,
            "timestamp": _now_iso(),
        }
        reverts.append(revert)
        print(f"[self-mod] REVERTED {param_name}: {new_value} -> {old_value} "
              f"({max_consecutive} consecutive losses)")

    return reverts


# ---------------------------------------------------------------------------
# Internal: write to learned_config
# ---------------------------------------------------------------------------

def _write_learned_config(conn, param_name: str, old_value, new_value, reason: str) -> None:
    """Upsert the learned_config table with a new parameter value."""
    now = _now_iso()
    evidence = json.dumps({"reason": reason, "timestamp": now})

    # Get current version
    try:
        row = conn.execute(
            "SELECT version FROM learned_config WHERE param_name = ?",
            (param_name,),
        ).fetchone()
        next_version = (row[0] + 1) if row else 1
    except Exception:
        next_version = 1

    with db_write_ctx(conn):
        conn.execute(
            """INSERT INTO learned_config (param_name, value, updated_at, evidence, previous_value, version)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(param_name) DO UPDATE SET
                   value = excluded.value,
                   updated_at = excluded.updated_at,
                   evidence = excluded.evidence,
                   previous_value = excluded.previous_value,
                   version = excluded.version""",
            (
                param_name,
                str(new_value),
                now,
                evidence,
                str(old_value),
                next_version,
            ),
        )
