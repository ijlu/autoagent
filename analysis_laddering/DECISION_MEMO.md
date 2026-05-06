# Laddering on Kalshi Weather — Go/Kill Decision

**Author:** Claude (for Josh Lu)
**Date:** 2026-04-23
**Question:** Should we build a Polymarket-style temperature-laddering directional strategy on Kalshi weather brackets, as described in [polydao's Apr 6 article](https://x.com/polydao/status/2041113454023475370)?

**Verdict: KILL laddering as a strategy on Kalshi weather.** Three independent legs all point the same way:
1. Fill-rate data from our own history: 725 resting orders at 1–2¢, zero fills (not a lifetime issue — 521 expired).
2. Tail calibration from Kalshi's public trade tape across 1,074 settled weather buckets (30 days × 6 families): at market open, 2–10¢ tails hit at ~3% vs ~5% implied — slightly *overpriced*, not underpriced. No free option.
3. Structural: polydao's 142× return-on-tail math only works if you can buy at ask; on Kalshi, the ask is either absent at 1–2¢ or you're the one quoting it, and the hit rate doesn't cover the posted price.

Two side-findings inform Phase 1:
- **Tail calibration is healthy enough** that our `edge_after_costs` gate is usable down to ~5¢, but the 10-pp underpricing at 40–60¢ is where our directional alpha should live. Suggest restricting the Phase 1 directional MIN_EDGE gate to `15¢ ≤ price ≤ 85¢` until per-bucket depth is characterized.
- **First-2-minute-of-hour** tail fill rate (13.1% vs 0.22% rest of hour) is a real, separable signal — tagged for post-Phase-1 ideation, not built now.

---

## Why this question came up

polydao's quant-trading guide claimed a small cohort of Polymarket weather traders is generating 5-figure P&L by buying cheap tail buckets (0.7–5¢) on daily temperature brackets and letting the multi-bucket ladder catch the winner. This was quote-tweeted into our feed, so we asked: does the same playbook work on Kalshi, where we already have signal alpha on weather (Phase 0 Brier 0.09–0.21 vs 0.24 baseline across 5 families)?

This memo is the result of Track 1 analysis under the constraint "no Phase 1 delay, zero new production code on the critical path."

## Evidence base

### 1. Fees (polydao's playbook math works on Kalshi structurally)

Kalshi fee schedule validated against public docs ([kalshi.com/fee-schedule](https://kalshi.com/fee-schedule)): `taker = 7¢ × P × (1-P)`, `maker = 1.75¢ × P × (1-P)`, ceil on total. Matches [bot/core/money.py](../bot/core/money.py) exactly. Because fees are quadratic in price, tail buys pay near-zero fees — a 100-contract buy at 1¢ costs 7¢ taker / 2¢ maker. Fees are NOT a tail-killer. The math behind polydao's "142× on a 0.7¢ buy" is essentially honest.

### 2. Fill-rate curve from our own history (DECISIVE)

Query: `mm_orders` weather rows (3,486 orders across KXHIGHAUS/CHI/DEN/LAX/MIA/NY), fill indicator = `fill_qty > 0`.

| Price bin | n_orders | n_filled | fill_pct |
|-----------|---------:|---------:|---------:|
| 01–02¢ | 725 | **0** | **0.0%** |
| 03–05¢ | 831 | 22 | 2.6% |
| 06–10¢ | 753 | 7 | 0.9% |
| 11–20¢ | 124 | 14 | 11.3% |
| 21–40¢ | 281 | 62 | 22.1% |
| 41–60¢ | 274 | 98 | 35.8% |
| 61–80¢ | 211 | 86 | 40.8% |
| 81–95¢ | 196 | 103 | 52.6% |

**725 resting orders posted at 1–2¢ on weather brackets. Zero fills.** Of those, 521 expired (sat full 110-second lifetime) — so it's not a lifetime issue. Tail liquidity at 1–2¢ on Kalshi weather is structurally absent.

### 3. Market-open window (HIGHLY DIAGNOSTIC)

Of all tail fills (price 3–10¢, n=29 across weather):

| Minute past UTC hour | n_fills |
|---|---|
| 0–2 | 23 |
| 3–10 | 2 |
| 11–59 | 4 |

23 of 29 tail fills happened within the first 2 minutes of each hour — the market-open window when Kalshi daily weather brackets open (local midnight for each city).

| Window | n_orders | fill_pct |
|---|---:|---:|
| first_2min | 175 | **13.1%** |
| first_10min | 275 | 0.73% |
| rest_of_hour | 1,859 | **0.22%** |

The tail liquidity that polydao describes is a ~2-minute daily burst on Kalshi, not a continuous phenomenon. Outside that window, tail fill rate is 1-in-465.

### 4. Tail calibration — does 2¢ mean 2% actual probability?

`pull_tail_calibration.py` pulls the Kalshi public trade tape for every settled weather bracket in the last 30 days across all 6 live families (KXHIGHAUS/CHI/DEN/LAX/MIA/NY), walks the pagination cursor to capture the full lifetime tape, and computes three volume-weighted YES prices per bucket:
- **OPEN:** first 20 trades (market-open activity, before any resolution signal)
- **MID:** middle 20% of the tape (uncertainty window)
- **CLOSE:** last 20 trades (after the bucket's fate is effectively settled)

1,074 settled buckets, 6 families, 30 days. Calibration @ OPEN (the leg that matters for entry-time pricing):

| Implied prob bin | n | n_yes | actual_yes_rate | avg_implied | Gap |
|---|---:|---:|---:|---:|---:|
| 0–2¢ | 28 | 1 | **3.6%** | 1.6% | +2.0pp under |
| 2–5¢ | 160 | 2 | **1.2%** | 3.4% | −2.2pp over |
| 5–10¢ | 162 | 8 | **4.9%** | 7.5% | −2.6pp over |
| 10–20¢ | 295 | 30 | **10.2%** | 14.7% | −4.5pp over |
| 20–40¢ | 391 | 117 | **29.9%** | 27.3% | +2.6pp under |
| 40–60¢ | 35 | 20 | **57.1%** | 46.6% | **+10.5pp under** |
| 60–80¢ | 2 | 0 | 0.0% | 60.2% | (n too small) |
| 80–95¢ | 1 | 1 | 100.0% | 82.8% | (n too small) |

**Tail aggregate (0–10¢, n=350):** 11 hits = **3.1% realized vs ~5.0% avg implied.** Slightly *overpriced*, not underpriced. At MID, tails tighten further (0–5¢ → 0% hit across n=331).

Sanity check — at CLOSE the tape is fully resolved: 0–2¢ n=869 all settle NO, 0.95–1.01 n=177 all settle YES. Confirms the pipeline is picking up real settlement outcomes.

## Interpretation

The data landed squarely in the "tails slightly overpriced" bucket, which is the cleanest possible kill. Three legs of evidence all lean the same way — no tension to resolve:

1. **Even if laddering were priced right, we can't buy at the tail.** The 0-fill / 725-order result at 1–2¢ settles the execution question before calibration matters. Laddering requires cheap tail entry, and Kalshi doesn't offer it on weather.

2. **And the tails aren't priced right for laddering anyway.** 0–10¢ tails hit at 3.1% vs ~5.0% implied. Even with zero fees and instant fills, buying tails would bleed ~2pp per contract. Polydao's 142× example assumes you got in at 0.7¢ on a bucket that hit. What's missing from his framing is the survivor bias on *other* tail buys that didn't hit — on Kalshi, those dominate.

3. **The sliver of edge that does exist is not at the tail.** The clear gap is at 40–60¢ (57% actual vs 47% implied, +10.5pp) and 20–40¢ (+2.6pp). That's the zone where our Phase 0 per-family Brier advantage (0.09–0.21 vs 0.24 baseline) actually shows up in the price-vs-truth decomposition. Phase 1 directional should live here, not at the tails.

## Implications

### For the laddering question

**Kill, don't revise.** There is no version of laddering that works on Kalshi weather given the current book. Any future revisit should be event-driven: a live fill-rate sentinel on the first-2-min-of-hour window, not a scheduled cycle strategy.

### For Phase 1 directional (independent)

Three concrete deltas worth shipping alongside the Phase 1 weather-MM-live gate:
1. **Restrict the directional MIN_EDGE gate to `15¢ ≤ price ≤ 85¢`** on weather until we instrument per-bucket depth. The 10–20¢ band is the one actually-worth-trading-but-overpriced-by-market zone, and that's where a miscalibrated `edge_after_costs` would bleed the most.
2. **Feed the calibration findings into the weather sub-ensemble's Platt fit.** The observed 40–60¢ underpricing is a free update signal — `bot/learning/calibration.py` already persists a Platt curve to `kv_cache`; this data belongs in that pipeline (gated by the normal `_alpha_populate_all` path, not a one-off override).
3. **Block laddering-adjacent posts from `WeatherQuoter` quoting tails explicitly.** The quoter already passes through `smart_gates`; add a min-price floor (e.g. 8¢) for weather until we have evidence the tail MM is a survivor rather than adverse-selected.

### Opening-auction window (adjacent but distinct)

The 13.1% first-2-minutes fill rate is real signal. It's a different strategy (active-sniping, not passive-laddering) and I'm not proposing it here — just flagging that the data suggests a competitor's offloading flow exists at market open that an event-driven system could tap. Noted for Phase 2+ ideation.

## What we did NOT do, and why

- **No shadow evaluator.** Josh explicit constraint: no code changes to Phase 1 critical path. The fill-rate data answered the core question without needing a live experiment.
- **No VPS DB pull.** Local DB (Apr 13) was sufficient for fill-rate and bank of 76 settlements. A fresh VPS snapshot would expand sample sizes but not change the qualitative conclusions given how lopsided the fill-rate numbers are.
- **No Level-2 orderbook poller.** Would have been Track 2 had Track 1 been ambiguous; Track 1 wasn't.

## Go-forward

1. **Do not build laddering.** Do not add it to the Phase 2+ queue as a "maybe later" either — the fill-rate data is a structural kill, not a quality-of-signal kill, and structural killers don't get better with more signal.
2. **Ship the three Phase-1 directional deltas above** (price-band gate, Platt calibration feed, WeatherQuoter tail floor) — small, self-contained, none block the Phase-1 weather-MM-live gate.
3. **Park the 13.1% first-2-min fill-rate observation for post-Phase-1 Q3.** It's a separate strategy (active-sniping of the market-open auction window) and cleanly distinct from both laddering and our current MM/directional paths. Revisit only after Phase 1 weather MM is live and shadow-to-live P&L is stable.
4. **Keep `pull_tail_calibration.py` as a recurring diagnostic.** It runs in <10 min, uses only the public trade tape (no auth), and gives us an independent sanity-check on per-bucket calibration separate from our own alpha_backtest. Suggest running monthly; output CSV already at `analysis_laddering/tail_calibration.csv`.

**Verdict stands: KILL laddering. GO Phase 1 directional (with the band-gate delta). GO Phase 1 weather MM (unchanged — this memo does not affect the shadow-to-live gate).**
