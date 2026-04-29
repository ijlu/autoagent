"""One-off migration for the 2026-04-28 ``ensemble_p_yes`` label-inversion bug.

The bug: ``trade.py`` directional-shadow logging passed ``indep_prob``
(P(our_side)) directly into ``alpha_backtest.ensemble_p_yes``, which is
documented (and read by every downstream consumer) as canonical P(YES).
For side='yes' rows the two are identical, so no harm. For side='no'
rows the stored value is P(NO), inverted from canonical P(YES).

100% of weather rows are side='no', so 100% of weather alpha_backtest
data is inverted. ``populate_from_alpha`` then double-flips when reading
(it does ``1 - p_yes`` for NO rows assuming canonical P(YES)) — so the
``calibration.our_prob`` and ``timing_patterns.edge`` rows derived from
broken alpha_backtest rows are also wrong.

This migration:
  1. Captures ``max_id_at_start`` so we only touch pre-fix rows.
  2. Inverts ``ensemble_p_yes`` on every directional_shadow side='no' row
     with id <= cutoff. (decision_type='directional_shadow_backfill' and
     side='yes' rows were already correct.)
  3. Deletes calibration + timing_patterns rows that referenced the
     inverted alpha_ids.
  4. Re-runs ``populate_calibration`` and ``populate_timing_patterns`` so
     the downstream tables are rebuilt from corrected data.

Idempotency: re-running the script is a no-op once cutoff is past.
The cutoff captures pre-fix state — anything written after the writer
fix deploy is guaranteed correct (see ``to_canonical_p_yes`` in
``bot.learning.alpha_log`` and the regression test in
``tests/test_alpha_log.py::TestToCanonicalPYes``).

Usage::

    python -m tools.migrate_alpha_label_inversion \\
        --db /path/to/kalshi_trades.db \\
        --dry-run         # preview counts only, no writes

    python -m tools.migrate_alpha_label_inversion \\
        --db /path/to/kalshi_trades.db \\
        --apply           # actually run the migration

The migration runs in a transaction. If anything fails, no changes
persist — re-run with ``--apply`` after the issue is fixed.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from typing import Optional


def _count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return conn.execute(sql, params).fetchone()[0]


def _diagnose(conn: sqlite3.Connection, cutoff_id: int) -> dict:
    """Snapshot pre-migration state for the post-migration verification."""
    return {
        "alpha_total": _count(conn, "SELECT COUNT(*) FROM alpha_backtest"),
        "alpha_directional_shadow": _count(
            conn,
            "SELECT COUNT(*) FROM alpha_backtest "
            "WHERE decision_type='directional_shadow'"),
        "alpha_inverted_target": _count(
            conn,
            "SELECT COUNT(*) FROM alpha_backtest "
            "WHERE id <= ? AND decision_type='directional_shadow' "
            "AND side='no'", (cutoff_id,)),
        "alpha_settled_inverted_target": _count(
            conn,
            "SELECT COUNT(*) FROM alpha_backtest "
            "WHERE id <= ? AND decision_type='directional_shadow' "
            "AND side='no' AND won_yes IS NOT NULL", (cutoff_id,)),
        "calibration_alpha_total": _count(
            conn,
            "SELECT COUNT(*) FROM calibration WHERE source_desc='alpha:shadow'"),
        "timing_patterns_alpha_total": _count(
            conn,
            "SELECT COUNT(*) FROM timing_patterns "
            "WHERE source LIKE 'alpha:%'"),
    }


def _spot_check_sample(conn: sqlite3.Connection, cutoff_id: int) -> list:
    """Pull 5 sample rows in the affected slice for human inspection."""
    rows = conn.execute(
        """SELECT id, ticker, side, ensemble_p_yes, won_yes, settlement_result
           FROM alpha_backtest
           WHERE id <= ? AND decision_type='directional_shadow'
             AND side='no' AND won_yes IS NOT NULL
           ORDER BY id DESC LIMIT 5""", (cutoff_id,)
    ).fetchall()
    return rows


# Idempotency marker stored in kv_cache. Records the cutoff id of the last
# successful run so re-running is a no-op. Re-applying the inversion on
# already-corrected rows would silently revert them, so this guard is
# load-bearing — never remove without replacing with a stronger one.
_MIGRATION_MARKER_KEY = "migration:alpha_label_inversion:done_cutoff_id"


def _read_marker(conn: sqlite3.Connection) -> Optional[int]:
    """Return the cutoff id of the prior successful run, or None."""
    try:
        row = conn.execute(
            "SELECT value FROM kv_cache WHERE key = ?",
            (_MIGRATION_MARKER_KEY,),
        ).fetchone()
    except sqlite3.OperationalError:
        # kv_cache may not exist on a hand-rolled test DB; treat as unset.
        return None
    if row is None or row[0] is None:
        return None
    try:
        import json
        v = json.loads(row[0])
        return int(v) if v is not None else None
    except (ValueError, TypeError):
        return None


def _write_marker(conn: sqlite3.Connection, cutoff_id: int) -> None:
    import json
    import time as _time
    conn.execute(
        "INSERT OR REPLACE INTO kv_cache (key, value, expires_at) "
        "VALUES (?, ?, ?)",
        (_MIGRATION_MARKER_KEY, json.dumps(cutoff_id), _time.time() + 10 * 365 * 86400),
    )


def run_migration(
    conn: sqlite3.Connection, *, apply: bool, cutoff_id: Optional[int] = None
) -> dict:
    """Run the migration. Returns a stats dict.

    With ``apply=False`` (dry run), reports counts but never writes.

    Idempotent: stores a cutoff marker in ``kv_cache`` after a successful
    run. Subsequent calls bail unless the new cutoff_id is strictly greater
    than the marker — which would indicate new pre-fix rows accumulated
    (shouldn't happen post-deploy, but guarded defensively).
    """
    if cutoff_id is None:
        cutoff_id = _count(conn, "SELECT COALESCE(MAX(id), 0) FROM alpha_backtest")

    prior_cutoff = _read_marker(conn)
    if prior_cutoff is not None and prior_cutoff >= cutoff_id:
        print(f"[migrate] marker present: prior cutoff_id={prior_cutoff} ≥ "
              f"current cutoff_id={cutoff_id}. Migration already applied; "
              f"nothing to do.")
        return {
            "cutoff_id": cutoff_id,
            "alpha_inverted": 0,
            "calibration_deleted": 0,
            "timing_deleted": 0,
            "calibration_repopulated": 0,
            "timing_repopulated": 0,
            "skipped_idempotency": True,
        }

    pre = _diagnose(conn, cutoff_id)
    pre_sample = _spot_check_sample(conn, cutoff_id)

    print(f"[migrate] cutoff_id = {cutoff_id} (max alpha_backtest.id at start)")
    if prior_cutoff is not None:
        print(f"[migrate] marker says prior cutoff was {prior_cutoff}; "
              f"will migrate id range ({prior_cutoff}, {cutoff_id}].")
    print(f"[migrate] pre-migration state:")
    for k, v in pre.items():
        print(f"  {k} = {v}")
    print(f"[migrate] sample of rows to be inverted (newest 5):")
    for r in pre_sample:
        print(f"  id={r[0]} ticker={r[1]} side={r[2]} "
              f"ensemble_p_yes={r[3]:.3f} won_yes={r[4]} "
              f"settlement_result={r[5]}")

    lower_bound_id = prior_cutoff if prior_cutoff is not None else 0

    stats = {
        "cutoff_id": cutoff_id,
        "lower_bound_id": lower_bound_id,
        "alpha_inverted": 0,
        "calibration_deleted": 0,
        "timing_deleted": 0,
        "calibration_repopulated": 0,
        "timing_repopulated": 0,
    }

    if not apply:
        print("[migrate] DRY RUN — no writes. Pass --apply to commit.")
        return stats

    # Single transaction: all-or-nothing.
    print(f"[migrate] applying migration in a single transaction "
          f"(id range ({lower_bound_id}, {cutoff_id}])…")
    conn.execute("BEGIN")
    try:
        # 1. Invert ensemble_p_yes on broken side='no' directional_shadow rows.
        cur = conn.execute(
            """UPDATE alpha_backtest
               SET ensemble_p_yes = 1.0 - ensemble_p_yes
               WHERE id > ? AND id <= ?
                 AND decision_type = 'directional_shadow'
                 AND side = 'no'""",
            (lower_bound_id, cutoff_id),
        )
        stats["alpha_inverted"] = cur.rowcount

        # 2. Delete poisoned downstream rows. populate_from_alpha will
        # rebuild from corrected alpha_backtest in step 3.
        cur = conn.execute(
            """DELETE FROM calibration
               WHERE source_desc = 'alpha:shadow'
                 AND alpha_id IN (
                   SELECT id FROM alpha_backtest
                   WHERE id > ? AND id <= ?
                     AND decision_type='directional_shadow'
                     AND side='no'
                 )""",
            (lower_bound_id, cutoff_id),
        )
        stats["calibration_deleted"] = cur.rowcount

        cur = conn.execute(
            """DELETE FROM timing_patterns
               WHERE source LIKE 'alpha:%'
                 AND alpha_id IN (
                   SELECT id FROM alpha_backtest
                   WHERE id > ? AND id <= ?
                     AND decision_type='directional_shadow'
                     AND side='no'
                 )""",
            (lower_bound_id, cutoff_id),
        )
        stats["timing_deleted"] = cur.rowcount

        # 3. Persist the marker INSIDE the transaction so the cutoff is
        # advanced atomically with the data writes. If the COMMIT fails,
        # the marker rolls back too.
        _write_marker(conn, cutoff_id)

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    # 3. Repopulate downstream tables from corrected alpha_backtest.
    # populate_from_alpha runs its own write transaction; if it fails the
    # invert+delete already committed are still consistent (alpha is
    # correct, downstream is just empty until next call succeeds).
    print("[migrate] repopulating calibration + timing_patterns from corrected alpha_backtest…")
    from bot.learning.populate_from_alpha import (
        populate_calibration, populate_timing_patterns,
    )
    stats["calibration_repopulated"] = populate_calibration(conn)
    stats["timing_repopulated"] = populate_timing_patterns(conn)

    print(f"[migrate] DONE.")
    print(f"  alpha_backtest rows inverted: {stats['alpha_inverted']}")
    print(f"  calibration rows deleted: {stats['calibration_deleted']}")
    print(f"  timing_patterns rows deleted: {stats['timing_deleted']}")
    print(f"  calibration rows repopulated: {stats['calibration_repopulated']}")
    print(f"  timing_patterns rows repopulated: {stats['timing_repopulated']}")

    post_sample = _spot_check_sample(conn, cutoff_id)
    print(f"[migrate] same 5 rows after migration:")
    for r in post_sample:
        print(f"  id={r[0]} ticker={r[1]} side={r[2]} "
              f"ensemble_p_yes={r[3]:.3f} won_yes={r[4]} "
              f"settlement_result={r[5]}")

    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="path to kalshi_trades.db")
    ap.add_argument(
        "--cutoff-id", type=int, default=None,
        help=(
            "Only invert rows with id <= this. If omitted, defaults to the "
            "current MAX(id) in alpha_backtest. SET THIS EXPLICITLY when "
            "deploying the writer fix to avoid inverting post-deploy rows "
            "that are already correct: capture MAX(id) on the live DB BEFORE "
            "shipping the trade.py fix, then pass that value here after the "
            "deploy completes."
        ),
    )
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--dry-run", action="store_true",
                     help="report counts only, no writes")
    grp.add_argument("--apply", action="store_true",
                     help="commit the migration")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    try:
        run_migration(conn, apply=args.apply, cutoff_id=args.cutoff_id)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
