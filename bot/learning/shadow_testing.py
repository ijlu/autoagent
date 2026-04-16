"""Hyperparameter shadow evaluation.

Runs shadow calculations with alternative parameter values alongside real trades.
Tracks what WOULD have happened with different settings to recommend tuning.
"""

from __future__ import annotations

from datetime import datetime, timezone

from bot.config import KELLY_FRACTION

SHADOW_PARAMS = {
    # param_name: [alternative_values_to_test]
    "kelly_fraction": [0.05, 0.15, 0.20],
    "min_edge": [0.03, 0.07, 0.10],
}


def record_shadow_evaluations(conn, result):
    """For each trade this run, compute what would have happened with alternative params."""
    now_str = datetime.now(timezone.utc).isoformat()

    opps = result.get("opportunities", [])
    if not opps:
        return

    for opp in opps:
        ticker = opp.get("ticker", "")
        contracts = opp.get("contracts", 0)
        price_cents = opp.get("price_cents", 50)
        indep_prob = opp.get("independent_prob")
        edge = opp.get("edge")

        if not indep_prob or not price_cents:
            continue

        # Shadow Kelly fractions
        for shadow_kelly in SHADOW_PARAMS.get("kelly_fraction", []):
            # Recompute Kelly with shadow value
            market_prob = price_cents / 100
            edge_val = indep_prob - market_prob
            if edge_val <= 0:
                continue
            b = (100 - price_cents) / price_cents
            q = 1 - indep_prob
            kelly_raw = (b * indep_prob - q) / b
            if kelly_raw <= 0:
                continue
            # Use the first session balance as reference
            bal_row = conn.execute(
                "SELECT balance_cents FROM sessions ORDER BY id DESC LIMIT 1"
            ).fetchone()
            balance = bal_row[0] if bal_row else 10000
            shadow_stake = kelly_raw * shadow_kelly * (balance / 100)
            shadow_contracts = max(1, int(shadow_stake / (price_cents / 100)))

            conn.execute("""INSERT INTO hyperparam_shadow
                (recorded_at, param_name, current_value, shadow_value,
                 ticker, actual_contracts, shadow_contracts, actual_profit, shadow_profit)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (now_str, "kelly_fraction", KELLY_FRACTION, shadow_kelly,
                 ticker, contracts, shadow_contracts, None, None))

    conn.commit()


def analyze_shadow_performance(conn):
    """After settlements, compare actual vs shadow performance.
    Recommends parameter changes when shadow consistently outperforms."""
    now_str = datetime.now(timezone.utc).isoformat()

    # Match shadow records to settlements via order_id (not just ticker, which is ambiguous)
    results = conn.execute("""
        SELECT h.param_name, h.current_value, h.shadow_value,
               h.actual_contracts, h.shadow_contracts,
               s.profit_cents, s.contracts, s.won
        FROM hyperparam_shadow h
        JOIN trades t ON h.ticker = t.ticker
            AND ABS(julianday(h.recorded_at) - julianday(t.timestamp)) < 0.01
        JOIN settlements s ON t.order_id = s.order_id
        WHERE h.actual_profit IS NULL
    """).fetchall()

    if len(results) < 10:
        return

    # Group by param + shadow value
    groups = {}
    for pname, current, shadow, actual_c, shadow_c, profit, settle_c, won in results:
        key = (pname, shadow)
        if key not in groups:
            groups[key] = {"current_val": current, "actual_profit": 0,
                          "shadow_profit": 0, "n": 0}
        # Scale profit proportionally to contract count
        per_contract_profit = profit / settle_c if settle_c > 0 else 0
        groups[key]["actual_profit"] += per_contract_profit * actual_c
        groups[key]["shadow_profit"] += per_contract_profit * shadow_c
        groups[key]["n"] += 1

    for (pname, shadow_val), stats in groups.items():
        if stats["n"] < 10:
            continue
        actual = stats["actual_profit"]
        shadow = stats["shadow_profit"]
        improvement = (shadow - actual) / abs(actual) if actual != 0 else 0

        if abs(improvement) > 0.10:  # >10% difference
            direction = "better" if improvement > 0 else "worse"
            print(f"[shadow] {pname}={shadow_val} would be {abs(improvement):.0%} {direction} "
                  f"than current {stats['current_val']} (n={stats['n']})")

            if improvement > 0.15 and stats["n"] >= 20:
                # Strong evidence for change — log recommendation
                conn.execute("""INSERT INTO strategy_journal
                    (timestamp, entry_type, category, title, detail, metric_value, metric_name)
                    VALUES (?,?,?,?,?,?,?)""",
                    (now_str, "hyperparam_recommendation", pname,
                     f"Consider changing {pname} from {stats['current_val']} to {shadow_val}",
                     f"Shadow testing over {stats['n']} trades shows {improvement:.0%} improvement. "
                     f"Actual profit: {actual:.0f}¢, shadow profit: {shadow:.0f}¢.",
                     improvement, f"shadow_{pname}"))

    conn.commit()
