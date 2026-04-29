"""Pre-seed kv_cache + weather_source_state for the new sources.

Walked the data from the 2026-04-29 big eval on VPS (9 sources × 6
stations × 30 days). Embeds the per-(source, station) MAE and bias
values directly so this script is self-contained — no external file
dependency.

For each (source, station):
  * Writes ``weather_skill_<source>_<city>_<bucket>`` with σ = MAE × 1.25
    for every skill bucket (we don't have horizon-specific data from the
    backfill, so we use the same σ across buckets — the fitter will
    later refine per-bucket from settled outcomes).
  * Writes ``weather_mos_bias_<source>_<city>`` with the measured bias.
  * Writes a ``weather_source_state`` row in PROBATIONARY state, with
    fitted σ + bias + n_settled set so the daily evaluator has data to
    reason about.

Idempotent: re-running just overwrites with the same values.

Usage::

    python -m tools.seed_source_priors_from_eval --db /path/to/kalshi_trades.db --apply
    python -m tools.seed_source_priors_from_eval --db /path/to/kalshi_trades.db --dry-run

After running, query verification::

    SELECT source, city, state, n_settled, sigma_fitted, bias_fitted
      FROM weather_source_state
     WHERE source IN ('iem_1min', 'icon', 'ukmo');
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from typing import Optional


# ── Eval results (2026-04-29, n=30 days × 6 stations) ────────────────────
# Format: source → {ICAO → (mae_f, bias_f)}.
# bias is signed: positive = source predicts WARM vs actual; negative = COLD.
# σ derived as MAE × 1.25 (≈ σ for normal distributions).
EVAL_RESULTS: dict[str, dict[str, tuple[float, float]]] = {
    "iem_1min": {
        # Real-time observation: low σ, mostly low bias except KNYC.
        "KAUS": (1.00, 0.00),
        "KDEN": (1.08, 0.15),
        "KLAX": (0.78, 0.78),
        "KMDW": (0.37, 0.22),
        "KMIA": (1.11, 1.00),
        "KNYC": (2.32, -1.18),  # known data-gap issue; tracked separately
    },
    "icon": {
        # German DWD — independence 0.39 vs HRRR; some cold biases.
        "KAUS": (2.65, -2.47),
        "KDEN": (2.49, -0.26),
        "KLAX": (2.76, -2.76),
        "KMDW": (2.20, -0.46),
        "KMIA": (1.02, -0.29),
        "KNYC": (1.81, -1.42),
    },
    "ukmo": {
        # UK Met Office — independence 0.40 vs HRRR.
        "KAUS": (2.47, -2.16),
        "KDEN": (1.99, 0.11),
        "KLAX": (1.86, -1.60),
        "KMDW": (1.66, -1.13),
        "KMIA": (2.62, -2.62),
        "KNYC": (3.00, -2.49),
    },
}

# n_settled for the eval period (per station, per source).
EVAL_N_PER_STATION = 29  # 30 days minus avg ~1 missing per station

# ICAO → city_key (matches bot.signals.weather_ensemble_v2._city_for_ticker)
ICAO_TO_CITY = {
    "KNYC": "nyc",
    "KMDW": "chicago",
    "KMIA": "miami",
    "KAUS": "austin",
    "KLAX": "los_angeles",
    "KDEN": "denver",
}


# Skill buckets — must match _SKILL_BUCKET_EDGES in weather_ensemble_v2.
# We seed every bucket with the same σ since the eval didn't stratify by
# horizon. The materializer will refine per-bucket from settled outcomes.
SKILL_BUCKETS = ["0_6", "6_12", "12_24", "24_48", "48_96", "96_168"]


def _kv_set_dict(conn: sqlite3.Connection, key: str, value: dict, ttl_s: int) -> None:
    """Wrapper around bot.db.kv_set that handles JSON encoding."""
    import json, time
    expires = time.time() + ttl_s if ttl_s > 0 else 0
    conn.execute(
        "INSERT OR REPLACE INTO kv_cache (key, value, expires_at) VALUES (?, ?, ?)",
        (key, json.dumps(value), expires),
    )


def seed_one_source(
    conn: sqlite3.Connection, source: str, *, apply: bool = False,
) -> dict:
    """Seed σ priors, biases, and state-machine row for one source.

    Returns a dict with counts of writes (skill rows, bias rows, state rows).
    """
    if source not in EVAL_RESULTS:
        return {"err": f"unknown source {source}"}

    counts = {"skill_writes": 0, "bias_writes": 0, "state_writes": 0}

    # 5-year TTL for these long-lived priors. The fitter (daily) refreshes
    # the values; this is just the initial seed.
    LONG_TTL_S = 5 * 365 * 86400

    for icao, (mae_f, bias_f) in EVAL_RESULTS[source].items():
        city = ICAO_TO_CITY[icao]
        sigma_f = round(mae_f * 1.25, 3)

        # Skill σ — write one entry per bucket (no horizon-specific data
        # in eval, so all buckets get the same prior).
        for bucket in SKILL_BUCKETS:
            key = f"weather_skill_{source}_{city}_{bucket}"
            payload = {"sigma": sigma_f, "n": EVAL_N_PER_STATION,
                       "source": "eval_2026-04-29", "method": "MAE×1.25"}
            print(f"  [{source}] {key} = {sigma_f} (n={EVAL_N_PER_STATION})")
            if apply:
                _kv_set_dict(conn, key, payload, LONG_TTL_S)
            counts["skill_writes"] += 1

        # MOS bias — single key (no bucket).
        bias_key = f"weather_mos_bias_{source}_{city}"
        bias_payload = {"bias": bias_f, "n": EVAL_N_PER_STATION,
                        "source": "eval_2026-04-29"}
        print(f"  [{source}] {bias_key} = {bias_f}")
        if apply:
            _kv_set_dict(conn, bias_key, bias_payload, LONG_TTL_S)
        counts["bias_writes"] += 1

        # State row — PROBATIONARY for the new source on this city.
        # n_settled comes from the eval; mae_30d is the measured value.
        if apply:
            from bot.learning.source_state_machine import (
                upsert_state, SourceState,
            )
            upsert_state(
                conn,
                source=source, city=city, state=SourceState.PROBATIONARY,
                n_settled=EVAL_N_PER_STATION,
                mae_30d=mae_f, sigma_fitted=sigma_f, bias_fitted=bias_f,
                state_changed=True,
                notes=f"seeded from 2026-04-29 eval: MAE={mae_f}, bias={bias_f}",
            )
        counts["state_writes"] += 1

    # Also write a "pooled" state row for the source so cities without
    # explicit per-city rows fall back to a sensible default. Pooled
    # values = simple mean across the 6 stations.
    pooled_mae = sum(v[0] for v in EVAL_RESULTS[source].values()) / 6
    pooled_bias = sum(v[1] for v in EVAL_RESULTS[source].values()) / 6
    pooled_sigma = round(pooled_mae * 1.25, 3)
    print(f"  [{source}] pooled state row: σ={pooled_sigma} bias={pooled_bias:.2f}")
    if apply:
        from bot.learning.source_state_machine import (
            upsert_state, SourceState,
        )
        upsert_state(
            conn,
            source=source, city="pooled", state=SourceState.PROBATIONARY,
            n_settled=EVAL_N_PER_STATION * 6,
            mae_30d=round(pooled_mae, 3),
            sigma_fitted=pooled_sigma,
            bias_fitted=round(pooled_bias, 3),
            state_changed=True,
            notes=f"pooled prior from 2026-04-29 eval (6 stations)",
        )
    counts["state_writes"] += 1

    return counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--dry-run", action="store_true")
    grp.add_argument("--apply", action="store_true")
    # Default seeds only sources currently in GAUSSIAN_COMBINE_SOURCES.
    # IEM_1MIN was in the eval but pulled from live combine 2026-04-29
    # (24h publication latency). The eval data for it is preserved in
    # this file for potential future use.
    DEFAULT_SOURCES = ["icon", "ukmo"]
    ap.add_argument("--sources", nargs="+", default=DEFAULT_SOURCES,
                    help="which sources to seed (default: live combine sources)")
    args = ap.parse_args()

    # Run init_db to ensure all tables exist (the new weather_source_state
    # table won't be in DBs that pre-date 2026-04-29). init_db is
    # idempotent — `CREATE TABLE IF NOT EXISTS` everywhere.
    import bot.db as db_mod
    db_mod._PERSIST_CONN = None
    conn = db_mod.init_db(args.db)
    try:
        all_counts = {}
        for source in args.sources:
            print(f"\n=== seeding {source} ===")
            c = seed_one_source(conn, source, apply=args.apply)
            all_counts[source] = c
        if args.apply:
            conn.commit()

        print("\n=== summary ===")
        total = {"skill_writes": 0, "bias_writes": 0, "state_writes": 0}
        for src, c in all_counts.items():
            print(f"  {src}: {c}")
            for k, v in c.items():
                if isinstance(v, int):
                    total[k] = total.get(k, 0) + v
        print(f"  total: {total}")
        if not args.apply:
            print("  DRY RUN — pass --apply to commit")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
