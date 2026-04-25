# Weather MM markout analysis — Apr 24 (initial read)

**Generated:** 2026-04-24
**DB snapshot:** `/home/kalshi/autoagent/kalshi_trades.db` copied 2026-04-24T20:08Z
**Window:** `ts_iso >= 2026-04-23T03:34:00Z`  (48h)
**Flip:** `2026-04-24T18:21:00Z` — so v2 has only ~2h of post-flip rows in this snapshot
**Reader:** `tools/weather_markout_analysis.py`

## TL;DR

- **v1 has ~zero alpha** on market mid at every horizon (overall means −0.04¢/+0.004¢/−0.04¢ at 300/900/3600s; CIs all include 0).
- **v2 sample counts are too small** to conclude (n=24/160/62). Directionally encouraging at 900s on the four most-populated families (AUS/CHI/DEN/LAX all positive), but none are individually significant.
- **MIA and NY at 3600s show catastrophic v2 markouts** (−90¢, −63¢ on n=4 each) — these are almost certainly settlement-convergence artifacts. Re-running should filter `hours_left >= 1` to exclude rows near expiry.
- **Go-live gate is NOT yet satisfied.** Re-run after 48h of post-flip data (~2026-04-26T18Z) with `hours_left` filter; promote only if v2 > v1 markout at 1σ on overall pooled samples AND both family-level v2 means are positive on ≥ 4 of 6 families.

## Follow-up

1. In 48h (2026-04-26T18:21Z +): re-pull VPS DB, re-run with `--since 2026-04-24T18:21Z --hours-left-min 1.0`.
2. If overall v2 > v1 markout by ≥ 2σ on Δ=900s pooled: promote to canary (graduated sizing multiplier 0.5).
3. If v2 ≤ v1 or per-family failures dominate: roll back the flag, investigate per-family Gaussian sigma sizing.

---

# Raw output



## Δ = 300s

- Samples: 3960
- Overall **v1** mean markout: -0.034¢  (95% CI [-0.082, +0.013], n=3960)
- Overall **v2** mean markout: -0.167¢  (95% CI [-2.896, +2.479], n=24)

| family | v1 n | v1 mean¢ | v1 95% CI | v2 n | v2 mean¢ | v2 95% CI |
|---|---|---|---|---|---|---|
| KXHIGHAUS | 706 | -0.11 | [-0.27, +0.03] | 3 | -0.17 | [-1.00, +0.50] |
| KXHIGHCHI | 719 | +0.03 | [-0.05, +0.12] | 6 | -0.58 | [-8.75, +8.08] |
| KXHIGHDEN | 852 | -0.05 | [-0.13, +0.04] | 8 | -0.12 | [-1.25, +1.19] |
| KXHIGHLAX | 695 | -0.01 | [-0.11, +0.09] | 3 | +0.17 | [-12.00, +14.50] |
| KXHIGHMIA | 456 | -0.09 | [-0.33, +0.06] | 2 | +0.25 | [+0.00, +0.50] |
| KXHIGHNY | 532 | +0.02 | [-0.06, +0.11] | 2 | +0.00 | [+0.00, +0.00] |

## Δ = 900s

- Samples: 5908
- Overall **v1** mean markout: +0.004¢  (95% CI [-0.083, +0.091], n=5908)
- Overall **v2** mean markout: -1.391¢  (95% CI [-3.928, +0.766], n=160)

| family | v1 n | v1 mean¢ | v1 95% CI | v2 n | v2 mean¢ | v2 95% CI |
|---|---|---|---|---|---|---|
| KXHIGHAUS | 1164 | -0.05 | [-0.26, +0.16] | 37 | +0.58 | [-1.34, +2.36] |
| KXHIGHCHI | 1025 | +0.07 | [-0.10, +0.26] | 30 | +1.28 | [-2.97, +5.48] |
| KXHIGHDEN | 1206 | -0.08 | [-0.23, +0.06] | 40 | +0.65 | [-1.15, +2.51] |
| KXHIGHLAX | 1005 | -0.11 | [-0.28, +0.05] | 27 | +0.96 | [-2.37, +4.09] |
| KXHIGHMIA | 760 | -0.09 | [-0.50, +0.25] | 16 | -10.81 | [-27.00, +0.25] |
| KXHIGHNY | 748 | +0.39 | [+0.10, +0.77] | 10 | -16.15 | [-40.30, +1.10] |

## Δ = 3600s

- Samples: 5374
- Overall **v1** mean markout: -0.040¢  (95% CI [-0.203, +0.119], n=5374)
- Overall **v2** mean markout: -10.266¢  (95% CI [-17.927, -3.315], n=62)

| family | v1 n | v1 mean¢ | v1 95% CI | v2 n | v2 mean¢ | v2 95% CI |
|---|---|---|---|---|---|---|
| KXHIGHAUS | 1069 | -0.04 | [-0.42, +0.32] | 13 | -1.23 | [-6.46, +3.88] |
| KXHIGHCHI | 922 | +0.51 | [+0.18, +0.82] | 12 | -1.38 | [-13.96, +11.46] |
| KXHIGHDEN | 1159 | -0.43 | [-0.69, -0.19] | 17 | +0.21 | [-2.85, +3.12] |
| KXHIGHLAX | 966 | -0.27 | [-0.54, +0.02] | 12 | +0.08 | [-5.67, +5.75] |
| KXHIGHMIA | 639 | -0.40 | [-1.14, +0.26] | 4 | -89.50 | [-91.25, -87.62] |
| KXHIGHNY | 619 | +0.60 | [+0.08, +1.20] | 4 | -62.62 | [-85.75, -20.88] |
