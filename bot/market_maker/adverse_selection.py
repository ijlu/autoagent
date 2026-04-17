"""Adverse selection analysis for market-making fills.

Computes short-horizon markouts to detect informed-trader picking.
Instead of waiting for settlement (days/weeks), compares each fill's price
to the fair value estimated on a *subsequent* cycle for the same ticker.

Falls back to settlement-based analysis for tickers with insufficient
markout data.
"""

from __future__ import annotations


def mm_compute_adverse_selection(conn):
    """Analyze MM fills for adverse selection using short-horizon markouts.

    Instead of waiting for settlement (days/weeks), compares each fill's price to
    the fair value estimated on a *subsequent* cycle for the same ticker.
    This measures whether informed traders are picking us off in real-time.

    Method: For each filled order, find the next order's fair_value_cents for the
    same ticker (posted on a later cycle). The markout = fair_value_next - fill_price.
    Adverse = markout is negative for buys (price dropped after we bought) or
    positive for sells (price rose after we sold).

    Falls back to settlement-based analysis for tickers with no markout data.

    Returns dict of {ticker: adverse_selection_rate} for markets we should avoid or widen spreads on."""
    adverse = {}
    try:
        # --- Primary: short-horizon markouts ---
        # Get all filled orders with their fair values, ordered by time
        rows = conn.execute("""
            SELECT ticker, side, price_cents, fair_value_cents, timestamp
            FROM mm_orders
            WHERE status IN ('filled', 'partial') AND price_cents > 0
            ORDER BY ticker, timestamp
        """).fetchall()

        per_ticker_fills = {}
        for ticker, side, price, fair_val, ts in rows:
            if ticker not in per_ticker_fills:
                per_ticker_fills[ticker] = []
            per_ticker_fills[ticker].append((side, price, fair_val, ts))

        markout_tickers = set()
        for ticker, fills in per_ticker_fills.items():
            total = 0
            adverse_count = 0
            for i, (side, price, fair_val, ts) in enumerate(fills):
                # Find next entry for this ticker to get the "next cycle fair value"
                next_fv = None
                for j in range(i + 1, len(fills)):
                    if fills[j][2] and fills[j][2] > 0:  # fair_value_cents > 0
                        next_fv = fills[j][2]
                        break
                if next_fv is None:
                    continue  # no markout available yet

                total += 1
                # Markout: how did fair value move after our fill?
                if side == "yes":
                    # We bought YES at price. If next fair value < price, adverse.
                    if next_fv < price:
                        adverse_count += 1
                else:  # side == "no"
                    # We sold YES (bought NO) at price. If next fair value > (100-price), adverse.
                    # In NO terms: we paid (100-price) for NO. Adverse if NO value dropped.
                    no_price = 100 - price
                    next_no_value = 100 - next_fv
                    if next_no_value < no_price:
                        adverse_count += 1

            if total >= 3:  # Lower threshold since markouts are more informative
                rate = adverse_count / total
                adverse[ticker] = rate
                markout_tickers.add(ticker)
                if rate > 0.65:
                    print(f"[mm_learn] \u26a0\ufe0f  High adverse selection (markout) on {ticker}: "
                          f"{rate:.0%} ({adverse_count}/{total} fills)")

        # --- Fallback: settlement-based for tickers without enough markout data ---
        try:
            settle_rows = conn.execute("""
                SELECT mm.ticker, mm.side, mm.price_cents, s.won
                FROM mm_orders mm
                JOIN settlements s ON mm.ticker = s.ticker
                WHERE mm.status IN ('filled', 'partial')
                AND mm.ticker NOT IN (SELECT DISTINCT ticker FROM mm_orders
                    WHERE status IN ('filled', 'partial') AND price_cents > 0
                    GROUP BY ticker HAVING COUNT(*) >= 6)
            """).fetchall()

            per_ticker_settle = {}
            for ticker, side, price, won in settle_rows:
                if ticker in markout_tickers:
                    continue  # already have markout data
                if ticker not in per_ticker_settle:
                    per_ticker_settle[ticker] = {"total": 0, "adverse": 0}
                per_ticker_settle[ticker]["total"] += 1
                if (side == "yes" and not won) or (side == "no" and won):
                    per_ticker_settle[ticker]["adverse"] += 1

            for ticker, stats in per_ticker_settle.items():
                if stats["total"] >= 5:
                    rate = stats["adverse"] / stats["total"]
                    adverse[ticker] = rate
                    if rate > 0.65:
                        print(f"[mm_learn] \u26a0\ufe0f  High adverse selection (settlement) on {ticker}: "
                              f"{rate:.0%} ({stats['adverse']}/{stats['total']})")
        except Exception:
            pass  # settlements table may not have data yet

    except Exception as e:
        print(f"[mm_learn] Error computing adverse selection: {e}")
    return adverse


def mm_compute_postmortem_risk_scores(conn):
    """Defense 5: Compute per-family risk scores from MM loss postmortems.

    Looks at loss_postmortems classified by run_mm_postmortems() to find
    families with high adverse selection or consistent losses. Returns scores
    that feed back into spread widening and quoting blocks.

    Returns dict of {series_prefix: risk_score} where risk_score is 0-1.
    Higher scores → wider spreads or quoting blocks.
    """
    scores = {}
    try:
        rows = conn.execute("""
            SELECT ticker, loss_type, COUNT(*) as cnt
            FROM loss_postmortems
            WHERE source_combo LIKE 'mm:%'
              AND recorded_at > datetime('now', '-14 days')
            GROUP BY ticker, loss_type
        """).fetchall()

        if not rows:
            return scores

        # Aggregate per family prefix
        per_family = {}
        for ticker, loss_type, cnt in rows:
            family = ticker.split("-")[0] if "-" in ticker else ticker
            if family not in per_family:
                per_family[family] = {"total": 0, "adverse": 0, "fee_erosion": 0}
            per_family[family]["total"] += cnt
            if loss_type == "mm_adverse_selection":
                per_family[family]["adverse"] += cnt
            elif loss_type == "mm_fee_erosion":
                per_family[family]["fee_erosion"] += cnt

        for family, data in per_family.items():
            if data["total"] < 3:
                continue  # not enough data to judge
            # Risk score: adverse selection is weighted heavily, fee erosion moderately
            # Pure directional losses and inventory decay are not counted as "risk"
            # since those are normal MM outcomes (they indicate position management
            # issues, not toxic flow).
            adverse_rate = data["adverse"] / data["total"]
            fee_rate = data["fee_erosion"] / data["total"]
            # Composite: adverse selection is the real danger, fee erosion is fixable
            risk = adverse_rate * 1.0 + fee_rate * 0.3
            risk = min(1.0, risk)
            scores[family] = round(risk, 3)
            if risk > 0.40:
                print(f"[postmortem-risk] {family}: risk={risk:.0%} "
                      f"(adverse={data['adverse']}/{data['total']}, "
                      f"fee_erosion={data['fee_erosion']}/{data['total']})")

    except Exception as e:
        print(f"[postmortem-risk] Error computing scores: {e}")
    return scores
