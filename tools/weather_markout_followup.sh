#!/usr/bin/env bash
# 48h follow-up run for WEATHER_ENSEMBLE_V2 markout analysis.
#
# Run on or after 2026-04-26T18:21Z (48h after flip). Pulls the current
# VPS DB, runs the markout tool with hours_left>=1 filter to exclude
# near-settlement rows, and writes reports/WEATHER_MARKOUT_FOLLOWUP.md.
#
# Go-live gate (manual decision after reading report):
# - v2 overall > v1 overall at Δ=900s by ≥ 2σ
# - AND ≥ 4 of 6 families show positive v2 mean markout
# - AND no family shows v2 < -2σ significantly worse than v1
# If met: flip canary multiplier (`mm_promotion` graduation).

set -euo pipefail
cd "$(dirname "$0")/.."

VPS_HOST="${VPS_HOST:-root@45.55.79.193}"
LOCAL_DB="/tmp/markout_dev/vps_kalshi_followup.db"
FLIP="2026-04-24T18:21:00+00:00"
SINCE="${SINCE:-$FLIP}"
OUT="reports/WEATHER_MARKOUT_FOLLOWUP.md"

mkdir -p /tmp/markout_dev reports
echo "[followup] scp VPS DB → $LOCAL_DB"
scp -q "${VPS_HOST}:/home/kalshi/autoagent/kalshi_trades.db" "$LOCAL_DB"

echo "[followup] running markout tool (since=$SINCE, hours_left>=1.0)"
python3 tools/weather_markout_analysis.py \
    --db "$LOCAL_DB" \
    --since "$SINCE" \
    --flip "$FLIP" \
    --deltas 300,900,3600 \
    --max-spread-c 15 \
    --hours-left-min 1.0 \
    --out "$OUT"

echo "[followup] wrote $OUT"
