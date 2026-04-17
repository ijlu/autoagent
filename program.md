# Kalshi Trading Agent — Maximize Profitability

## Objective
Improve the trading agent's edge detection to generate profitable trade signals on live Kalshi markets.
The current agent scans markets but finds 0 signals because the naive mean-reversion strategy has no real edge.

## Current Score: 0.5/1.0
- Gets 0.5 for scanning markets
- Gets 0 for PnL because no trades are placed
- Target: score > 0.8 by finding real edge and placing trades

## What the Meta-Agent Should Do
Rewrite the edge detection logic in the trading script to use real signals:

### Signal Ideas (implement and test these)
1. **Time-decay edge**: Binary markets approaching expiry that are still priced far from 0 or 1
   - If a market closes in <30 min and is priced at 0.45-0.55, it will resolve to 0 or 1 — huge edge
   - Buy YES if the underlying event is likely (e.g. "Will X happen today?" and it already happened)

2. **Spread harvesting**: Place limit orders at the mid-price on markets with spread > 4 cents
   - Earn the spread passively by providing liquidity on both sides

3. **Volume momentum**: Markets with recent volume spikes may have informed traders — follow them
   - If yes_ask is moving up with volume, buy YES

4. **Cross-market arbitrage**: Related markets (e.g. "Will temp be above 70?" and "above 65?") 
   should have monotonic prices. When they don't, there's arb.

5. **Base rate comparison**: For recurring events (weather, sports), compare implied probability
   to historical base rates. If KXMLBHIT markets imply 50% but historical hit rate is 30%, sell YES.

## Scoring
- 0.5 pts: markets_scanned > 0
- Up to 0.5 pts: pnl / 100 (capped at 0.5)
- Maximize total score toward 1.0

## Constraints
- Use verify=False in all requests (SSL cert issue in container)
- Private key at /root/.kalshi_private_key.pem
- Fields: yes_ask_dollars, yes_bid_dollars, volume_fp, close_time
- Max position: 5% of $1000 portfolio per trade
- Write output to /task/trades.json

## Key Improvement Areas
1. The estimate_edge() function currently returns near-zero for everything — fix this
2. Add logic to actually PLACE orders (uncomment the POST /portfolio/orders call)
3. Track realized PnL by checking order fills via GET /portfolio/orders
