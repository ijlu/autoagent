"""Series profitability gate for bracket market making.

Before posting a new MM order in a bracket series, simulates every possible
outcome (each bracket winning, plus none winning) and rejects if:
  - No profitable outcome exists
  - Worst-case loss exceeds the per-event budget
  - Expected value (model-weighted) is negative after fees

Uses mm_inventory as primary position source, merges directional trades
from the trades table (fail-closed on error), and uses apply_trade() for
position math.
"""

from __future__ import annotations

from bot.core.money import apply_trade
from bot.market_maker.inventory import mm_get_inventory
from bot.config import MM_MAX_INVENTORY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_series_prefix(ticker):
    """Extract the series prefix from a bracket/threshold ticker.
    KXHIGHDEN-26APR09-B69.5 -> KXHIGHDEN-26APR09
    KXFED-27APR-T2.50 -> KXFED-27APR
    Returns (prefix, is_bracket). Bracket markets (-B) have mutually exclusive outcomes."""
    parts = ticker.rsplit("-", 1)
    if len(parts) == 2 and parts[1] and parts[1][0] in ("B", "T"):
        is_bracket = parts[1][0] == "B"
        return parts[0], is_bracket
    # Not a bracket/threshold ticker
    return ticker, False


# ---------------------------------------------------------------------------
# Main gate
# ---------------------------------------------------------------------------

def mm_check_series_profitability(conn, ticker, proposed_side, proposed_qty, proposed_price):
    """Check if adding a proposed position to a bracket series would make the
    portfolio structurally unprofitable.

    For bracket markets (only ONE bracket can be true):
    - Simulates every possible outcome (each bracket winning, plus none winning)
    - For each outcome, computes total portfolio P&L across ALL brackets in the series
    - Rejects if: no profitable outcome, or worst-case loss exceeds budget, or EV < 0

    Uses mm_inventory as primary source (tracks actual fills).
    Merges directional trades from trades table (fail-closed on error).
    Uses apply_trade() for proposed position math (shared with mm_update_inventory).

    Returns (ok, reason) -- ok=True if the new position is safe to add."""
    series_prefix, is_bracket = _get_series_prefix(ticker)

    if not is_bracket:
        return True, "not a bracket market"  # threshold markets (-T) aren't mutually exclusive

    # --- Get ALL positions in this series from mm_inventory (ground truth for MM) ---
    positions = {}
    rows = conn.execute(
        "SELECT ticker, net_position, avg_entry_cents FROM mm_inventory WHERE ticker LIKE ? AND abs(net_position) > 0",
        (series_prefix + "%",)
    ).fetchall()
    for t, net, avg_e in rows:
        _, pos_is_bracket = _get_series_prefix(t)
        if not pos_is_bracket:
            continue
        positions[t] = (net, float(avg_e))

    # Also merge directional trades -- FAIL CLOSED on error (audit fix: no silent pass)
    try:
        dir_rows = conn.execute(
            """SELECT ticker, side, SUM(contracts), AVG(price_cents) FROM trades
               WHERE ticker LIKE ? AND action='buy' AND fill_status IN ('executed','filled')
               GROUP BY ticker, side""",
            (series_prefix + "%",)
        ).fetchall()
        for t, side, qty, avg_p in dir_rows:
            _, dir_is_bracket = _get_series_prefix(t)
            if not dir_is_bracket:
                continue
            dir_net = int(qty) if side == "yes" else -int(qty)
            dir_avg = float(avg_p)
            if t in positions:
                # Combine with existing MM position using apply_trade()
                existing_net, existing_avg = positions[t]
                combined_net, combined_avg, _ = apply_trade(
                    existing_net, existing_avg, side, int(qty), dir_avg)
                positions[t] = (combined_net, combined_avg)
            else:
                positions[t] = (dir_net, dir_avg)
    except Exception as e:
        # FAIL CLOSED: if we can't see directional trades, block the order
        print(f"    [series-check] BLOCKED: cannot read directional trades: {e}")
        return False, f"cannot read directional trades for risk check: {e}"

    # Apply proposed trade using shared apply_trade() math
    existing_net, existing_avg = positions.get(ticker, (0, 0.0))
    new_net, new_avg, _ = apply_trade(existing_net, existing_avg, proposed_side, proposed_qty, proposed_price)
    positions[ticker] = (new_net, new_avg)

    # Get all bracket tickers in this series (even those without inventory)
    all_tickers = set(positions.keys())
    all_tickers.add(ticker)

    # Simulate each possible outcome: one bracket wins, all others lose
    # CONVENTION: avg_e is always YES-side cost basis
    #   - For long YES (net > 0): avg_e = what we paid for YES
    #   - For short YES / long NO (net < 0): avg_e = 100 - what_we_paid_for_NO (YES-equivalent)
    scenario_pnls = []

    for winning_ticker in all_tickers:
        total_pnl = 0.0
        for t, (net, avg_e) in positions.items():
            if net == 0:
                continue
            if net > 0:
                if t == winning_ticker:
                    pnl = net * (100.0 - avg_e)
                else:
                    pnl = net * (0.0 - avg_e)
            else:
                no_cost = 100.0 - avg_e
                if t == winning_ticker:
                    pnl = -abs(net) * no_cost
                else:
                    pnl = abs(net) * (100.0 - no_cost)
            total_pnl += pnl
        scenario_pnls.append(total_pnl)

    # Also check: outcome where NONE of our held brackets win (uncovered bracket)
    uncovered_pnl = 0.0
    for t, (net, avg_e) in positions.items():
        if net == 0:
            continue
        if net > 0:
            uncovered_pnl += net * (0.0 - avg_e)
        else:
            no_cost = 100.0 - avg_e
            uncovered_pnl += abs(net) * (100.0 - no_cost)
    scenario_pnls.append(uncovered_pnl)

    worst_pnl = min(scenario_pnls) if scenario_pnls else 0.0
    best_pnl = max(scenario_pnls) if scenario_pnls else 0.0
    n_scenarios = len(scenario_pnls)

    # --- EV estimate using model probabilities (not uniform prior) ---
    # Look up most recent fair_value_cents for each bracket ticker from mm_orders.
    # scenario_pnls[i] corresponds to all_tickers[i] winning (last entry = none winning).
    ticker_list = list(all_tickers)  # matches scenario_pnls order from for loop above
    scenario_weights = []
    total_model_prob = 0.0
    for t in ticker_list:
        fv = None
        try:
            row = conn.execute(
                "SELECT fair_value_cents FROM mm_orders WHERE ticker=? AND fair_value_cents > 0 "
                "ORDER BY timestamp DESC LIMIT 1", (t,)
            ).fetchone()
            if row and row[0]:
                fv = float(row[0]) / 100.0  # convert cents to probability
        except Exception:
            pass
        if fv is not None and 0.01 <= fv <= 0.99:
            scenario_weights.append(fv)
            total_model_prob += fv
        else:
            scenario_weights.append(None)  # unknown -- will fall back

    # Weight for "none wins" scenario = 1 - sum(all bracket probs), clamped to [0.01, 0.99]
    none_wins_prob = max(0.01, min(0.99, 1.0 - total_model_prob))
    # Fill in missing weights with uniform share of remaining probability
    n_missing = sum(1 for w in scenario_weights if w is None)
    if n_missing > 0 and n_missing < len(scenario_weights):
        # Distribute remaining probability uniformly among unknown brackets
        leftover = max(0.01, 1.0 - total_model_prob - none_wins_prob)
        fill_val = leftover / n_missing
        scenario_weights = [w if w is not None else fill_val for w in scenario_weights]
    elif n_missing == len(scenario_weights):
        # No model data at all -- fall back to uniform
        uniform_p = 1.0 / max(1, n_scenarios)
        scenario_weights = [uniform_p] * len(scenario_weights)
        none_wins_prob = uniform_p

    scenario_weights.append(none_wins_prob)

    # Normalize weights to sum to 1.0
    w_total = sum(scenario_weights)
    if w_total > 0:
        scenario_weights = [w / w_total for w in scenario_weights]
    else:
        scenario_weights = [1.0 / n_scenarios] * n_scenarios

    # Weighted EV: sum(weight_i * pnl_i) instead of simple average
    avg_pnl = sum(w * p for w, p in zip(scenario_weights, scenario_pnls))

    # --- Fee accounting: use actual fees paid + estimate for proposed order ---
    # Actual fees from fill records for tickers in this series
    actual_fees_cents = 0.0
    try:
        fee_row = conn.execute(
            "SELECT COALESCE(SUM(fee_cents), 0) FROM mm_processed_fills WHERE ticker LIKE ?",
            (series_prefix + "%",)
        ).fetchone()
        if fee_row and fee_row[0]:
            actual_fees_cents = float(fee_row[0])
    except Exception:
        pass  # fee_cents/ticker columns may not exist yet

    # Estimate fee for proposed order using Kalshi formula: roundup(0.07 * C * P * (1-P))
    # For maker: roundup(0.0175 * C * P * (1-P))
    # We use maker rate since we use post_only
    p_dollar = proposed_price / 100.0
    proposed_fee_cents = max(1, int(0.0175 * proposed_qty * p_dollar * (1 - p_dollar) * 100 + 0.99))
    # Also estimate exit fee (assume we'll close at similar price)
    exit_fee_cents = proposed_fee_cents
    total_fees = actual_fees_cents + proposed_fee_cents + exit_fee_cents

    best_pnl_net = best_pnl - total_fees
    worst_pnl_net = worst_pnl - total_fees
    avg_pnl_net = avg_pnl - total_fees

    # --- Acceptance criteria (tightened from V2) ---
    # 1. Must have at least one profitable outcome
    has_profitable_outcome = any(p > total_fees for p in scenario_pnls)
    # 2. Best case must be positive after fees
    # 3. Average (EV) must be positive after fees -- not just "one good outcome"
    # 4. Worst case must not exceed per-event loss budget (50 contracts * 100c = $50)
    EVENT_LOSS_BUDGET_CENTS = MM_MAX_INVENTORY * 100  # worst-case budget per event

    if not has_profitable_outcome:
        return False, (f"no profitable outcome (net of fees): best={best_pnl_net/100:.2f} "
                       f"worst={worst_pnl_net/100:.2f} fees~{total_fees/100:.2f} "
                       f"across {len(all_tickers)} brackets in {series_prefix}")

    if best_pnl_net <= 0:
        return False, (f"best case negative after fees: best={best_pnl_net/100:.2f} "
                       f"fees~{total_fees/100:.2f}")

    if avg_pnl_net <= 0:
        return False, (f"negative EV after fees: EV={avg_pnl_net/100:.2f} "
                       f"best={best_pnl_net/100:+.2f} worst={worst_pnl_net/100:+.2f} "
                       f"fees~{total_fees/100:.2f}")

    if worst_pnl_net < -EVENT_LOSS_BUDGET_CENTS:
        return False, (f"worst case exceeds loss budget: worst={worst_pnl_net/100:.2f} "
                       f"budget=-{EVENT_LOSS_BUDGET_CENTS/100:.2f}")

    return True, (f"ok: EV={avg_pnl_net/100:+.2f} best={best_pnl_net/100:+.2f} "
                  f"worst={worst_pnl_net/100:+.2f} (fees~{total_fees/100:.2f})")
