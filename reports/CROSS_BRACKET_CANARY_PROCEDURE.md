# Cross-Bracket Live Canary Procedure

**Last verified:** 2026-05-01 (pre-canary). All gates default OFF.

This doc captures the exact steps to flip cross-bracket from shadow
mode to canary live trading on a single family. **Read it end-to-end
before running anything.** The whole procedure is reversible in <1
minute via the rollback at the bottom.

## Pre-flight checklist

Before running any of the canary commands, confirm all of these:

```bash
# 1. Open-Meteo commercial endpoint healthy (zero 429s in last hour)
ssh root@45.55.79.193 'awk -v cutoff="$(date -u -d "1 hour ago" +%Y-%m-%dT%H:%M:%S)" \
  "\$1\"T\"\$2 > cutoff && /HTTP 429/" /home/kalshi/autoagent/daemon.log | wc -l'
# Expected: 0

# 2. All 10 ensemble sources contributing
ssh root@45.55.79.193 'tail -200 /home/kalshi/autoagent/daemon.log | \
  grep -oE "tag=weather_ensemble_v2:[a-z_+]+" | sort | uniq -c | sort -rn | head -3'
# Expected: top tag includes hrrr+nws_point+weather+icon+ukmo+gem+metno+ecmwf+metar
# (nws_5min may drop in/out depending on observation freshness — ok)

# 3. ICON/UKMO not demoted (sigma_fitted ≤ 5 or NULL)
ssh root@45.55.79.193 'sqlite3 /home/kalshi/autoagent/kalshi_trades.db \
  "SELECT source, city, state, sigma_fitted FROM weather_source_state \
   WHERE source IN (\"icon\", \"ukmo\") AND state = \"demoted\";"'
# Expected: empty (no demoted rows)

# 4. Daemon healthy
ssh root@45.55.79.193 'systemctl is-active kalshi-daemon.service'
# Expected: active

# 5. Default-off state confirmed
ssh root@45.55.79.193 'grep "^CROSS_BRACKET_LIVE=" /home/kalshi/autoagent/.env || echo "(not set; default false)"'
# Expected: not set (or set to false)
```

If ANY of these fails, **don't proceed** — fix the failing item first.

## Canary launch — single family (KXHIGHNY)

KXHIGHNY is the recommended canary because:
- Has the most settled-decision data in `alpha_backtest`
- Settles at 04:59 UTC (early in the night) so we get fast feedback
- Single station (KNYC) with relatively predictable weather

The two switches that must both be ON:

```bash
# Switch 1: Global env. Add to prod .env, then restart daemon to pick it up.
ssh root@45.55.79.193 '
  sed -i "/^CROSS_BRACKET_LIVE=/d" /home/kalshi/autoagent/.env
  echo "CROSS_BRACKET_LIVE=true" >> /home/kalshi/autoagent/.env
  systemctl restart kalshi-daemon.service
'

# Wait for daemon to come up
sleep 10
ssh root@45.55.79.193 'systemctl is-active kalshi-daemon.service'
# Expected: active

# Switch 2: Per-family kv. Enables ONLY this family — other families
# stay shadow even though the global env is on.
ssh root@45.55.79.193 '
  sqlite3 /home/kalshi/autoagent/kalshi_trades.db "
    INSERT OR REPLACE INTO kv_cache (key, value, expires_at)
    VALUES (
      '\''cross_bracket_live:KXHIGHNY'\'',
      '\''true'\'',
      strftime('\''%s'\'', '\''now'\'', '\''+24 hours'\'')
    );
  "
'
```

After both switches are on, the next cross-bracket cycle (within ~5
minutes) will fire live for KXHIGHNY portfolios that pass all 5 gates:

| Gate | Threshold |
|---|---|
| Global env | `CROSS_BRACKET_LIVE=true` ✓ |
| Per-family kv | `cross_bracket_live:KXHIGHNY=true` ✓ |
| TTE window | 3.0–7.0 hours pre-settle |
| Per-leg edge | ≥ 0.10 (separate from shadow scorer's 0.07) |
| Daily exposure cap | ≤ 500¢ ($5/day) total committed |

Plus per-leg sizing: **1 contract × max 4 legs per portfolio**. Worst
case daily exposure: $5.

## Watching it work

In one terminal, watch for live order activity:

```bash
ssh root@45.55.79.193 'tail -f /home/kalshi/autoagent/daemon.log | \
  grep --line-buffered -E "cross_bracket_live|cross_bracket_shadow"'
```

Look for lines like:

```
[cross_bracket_live] POSTED KXHIGHNY-26MAY02-B62.5 yes 1×38¢ edge=+0.156 tte=4.2h order_id=...
```

That = live order successfully placed. The first batch of fills will
appear in `fills_ledger` within a minute or two (Kalshi accepts the
order; whether it fills depends on someone matching the limit price
within the 110-second post_only window).

To check fills directly:

```bash
ssh root@45.55.79.193 'sqlite3 /home/kalshi/autoagent/kalshi_trades.db "
  SELECT trade_id, ticker, side, count, price_cents, fill_ts_iso
  FROM fills_ledger
  WHERE client_order_id LIKE '\''mm_xb_%'\''
    AND fill_ts_iso > datetime('\''now'\'', '\''-2 hours'\'')
  ORDER BY fill_ts_iso DESC LIMIT 20;
"'
```

To see what shadow vs live decisions the daemon recorded:

```bash
ssh root@45.55.79.193 'sqlite3 /home/kalshi/autoagent/kalshi_trades.db "
  SELECT
    decision_type,
    decision_outcome,
    family,
    COUNT(*) AS n,
    AVG(contracts) AS avg_contracts
  FROM alpha_backtest
  WHERE notes LIKE '\''cross_bracket%'\''
    AND ts_decision >= datetime('\''now'\'', '\''-2 hours'\'')
  GROUP BY decision_type, decision_outcome, family
  ORDER BY n DESC;
"'
```

`cross_bracket_live + posted` rows = live orders. `cross_bracket_shadow + shadow_only` rows = decisions that didn't fire live (gate skipped them). The `notes` field on shadow rows includes `live_skip=<reason>` when applicable — useful for debugging which gate fired.

## Daily exposure cap status

The kv counter `cross_bracket_daily_exposure_<YYYY-MM-DD>` tracks
total cents committed today across all live cross-bracket fills. To
inspect:

```bash
ssh root@45.55.79.193 'sqlite3 /home/kalshi/autoagent/kalshi_trades.db "
  SELECT key, value FROM kv_cache
  WHERE key LIKE '\''cross_bracket_daily_exposure_%'\''
  ORDER BY key DESC LIMIT 3;
"'
```

When the counter reaches 500¢, the next leg attempting to fire live
gets logged as `live_skip=exposure_cap_...` and stays shadow until
midnight UTC rolls over (the kv key is dated, so a new day starts a
new counter).

## Expanding the canary

After ~24 hours of clean canary on KXHIGHNY, you can expand to more
families. Same pattern, one at a time. Recommended order based on
data depth + microclimate predictability:

1. ✅ KXHIGHNY (canary first — most data)
2. KXHIGHMIA (consistent peak, low variance)
3. KXHIGHAUS (small market but predictable)
4. KXHIGHLAX (microclimate-tricky, do this 4th)
5. KXHIGHCHI (continental, more variance)
6. KXHIGHDEN (highest σ, last)

```bash
# After KXHIGHNY clean for 24h, add KXHIGHMIA:
ssh root@45.55.79.193 'sqlite3 /home/kalshi/autoagent/kalshi_trades.db "
  INSERT OR REPLACE INTO kv_cache (key, value, expires_at)
  VALUES (
    '\''cross_bracket_live:KXHIGHMIA'\'',
    '\''true'\'',
    strftime('\''%s'\'', '\''now'\'', '\''+24 hours'\'')
  );
"'
```

Repeat for the others as confidence builds.

## Rollback (instant, no redeploy)

If anything looks wrong, **flip a single kv key** and the canary
stops within the next cycle (~5 min). No daemon restart needed.

```bash
# Stop ONE family (e.g. KXHIGHNY had a bad run):
ssh root@45.55.79.193 'sqlite3 /home/kalshi/autoagent/kalshi_trades.db "
  DELETE FROM kv_cache WHERE key = '\''cross_bracket_live:KXHIGHNY'\'';
"'

# Stop ALL cross-bracket live (nuclear option, requires daemon restart):
ssh root@45.55.79.193 '
  sed -i "/^CROSS_BRACKET_LIVE=/d" /home/kalshi/autoagent/.env
  echo "CROSS_BRACKET_LIVE=false" >> /home/kalshi/autoagent/.env
  systemctl restart kalshi-daemon.service
'
```

After rollback, **existing positions remain open** until they settle
naturally or `manage_positions` (in `trade.py`) triggers an exit
(edge_flipped, edge_decayed, or near-expiry). The kill switch only
prevents NEW orders.

## Tuning knobs (env vars on prod, restart to apply)

If the canary works and you want to ramp:

```
# Bigger position size (default 1)
CROSS_BRACKET_MAX_CONTRACTS_PER_LEG=2

# Bigger daily exposure cap (default 500¢ = $5)
CROSS_BRACKET_DAILY_EXPOSURE_CAP_CENTS=2000  # $20/day

# Wider TTE window (default 3-7h, narrowest band where backtest showed alpha)
CROSS_BRACKET_MIN_TTE_HOURS=2.0
CROSS_BRACKET_MAX_TTE_HOURS=8.0

# Lower edge floor (default 0.10, conservative)
CROSS_BRACKET_LIVE_MIN_EDGE=0.07  # match the shadow scorer's gate
```

**Don't tune more than one knob at a time** — you'll lose the ability
to attribute changes to specific tweaks.

## Things that should make you ABORT

- Any line in daemon.log matching `[cross_bracket_live] POST FAILED`
  (>5 in 10 minutes — Kalshi rejecting orders means our format is
  wrong or auth is bad)
- Daily exposure counter ramps to cap in <2 hours (more aggressive
  than expected; the gates may not be tight enough)
- More than 2 of 4 legs filling in a portfolio (we expected mostly
  partial fills; full fills mean we're moving the market and adverse
  selection will likely hurt)
- σ blowing up for any source (`weather_source_state` rows showing
  state='demoted' suddenly)
- HRRR / Open-Meteo 429s returning (>10 in 10 min — commercial tier
  may have lapsed or hit its monthly quota)

In any of those cases, hit the rollback. The whole pipeline is
designed for fast revert because the cost of being wrong on real
fills is real money.

## After the first 24 hours

Check fills + realized PnL:

```bash
# Realized PnL on cross-bracket fills (back-filled by record_settlements)
ssh root@45.55.79.193 'sqlite3 /home/kalshi/autoagent/kalshi_trades.db "
  SELECT
    family,
    COUNT(*) AS legs,
    SUM(realized_pnl_cents) AS total_pnl_c,
    ROUND(AVG(realized_pnl_cents)*1.0, 1) AS avg_pnl_per_leg
  FROM alpha_backtest
  WHERE decision_type = '\''cross_bracket_live'\''
    AND decision_outcome = '\''posted'\''
    AND realized_pnl_cents IS NOT NULL
  GROUP BY family;
"'
```

Compare to the backtest expectation: ~+89¢/leg net at TTE 4-6h with
96% WR. If realized matches, expand. If realized is ≤25¢/leg or WR
< 50%, freeze and re-investigate before any expansion.
