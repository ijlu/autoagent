"""T.7 golden-set CI regression test for the MM promotion gate.

Any PR that touches ``bot/learning/mm_promotion.py`` logic must either:

  (a) leave the decision surface identical on the fixtures, or
  (b) explicitly update ``tests/golden/mm_promotion_fixtures.json`` in the
      same PR and call out why in the commit message.

Fixtures are deliberately simple and span the decision edges: strong-pass,
just-below-N, just-below-per-fill, zero-fills, and the catastrophic-loss
kill-switch trip. If the gate changes behavior here silently, that's a
regression in threshold semantics — catch it in CI, not in production.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from bot.db import init_db
from bot.learning.mm_promotion import (
    evaluate_mm_kill_switch,
    evaluate_mm_promotion,
)

FIXTURES_PATH = Path(__file__).parent / "golden" / "mm_promotion_fixtures.json"


@pytest.fixture(scope="module")
def fixtures():
    with FIXTURES_PATH.open() as f:
        return json.load(f)


@pytest.fixture()
def conn():
    c = init_db(":memory:")
    yield c
    c.close()


def _seed(conn, count, pnl_per_row, bid_filled, ask_filled, *, live_mode=0):
    start = time.time() - 14 * 86400
    for i in range(count):
        cur = conn.execute(
            "INSERT INTO weather_mm_shadow "
            "(ts_unix, ts_iso, ticker, series, station, "
            " fair_value_cents, proposed_bid_cents, proposed_ask_cents, "
            " half_spread_cents, gate_should_quote, live_mode) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (int(start + i * 60), "t", f"GOLD-{i}", "GOLDEN", "KJFK",
             50, 40, 60, 10, 1, live_mode),
        )
        conn.execute(
            "UPDATE weather_mm_shadow SET shadow_bid_filled=?, "
            "shadow_ask_filled=?, shadow_pnl_cents=?, ts_settle_unix=? "
            "WHERE id=?",
            (bid_filled, ask_filled, pnl_per_row,
             start + (i + 1) * 60, cur.lastrowid),
        )
    conn.commit()


class TestPromotionGoldenFixtures:
    def test_all_promotion_fixtures(self, fixtures, conn):
        failures: list[str] = []
        for fx in fixtures["fixtures"]:
            # Fresh DB per fixture to keep cases isolated
            c = init_db(":memory:")
            try:
                rows = fx["rows"]
                _seed(
                    c,
                    count=rows["count"],
                    pnl_per_row=rows["pnl_per_row"],
                    bid_filled=rows["bid_filled"],
                    ask_filled=rows["ask_filled"],
                )
                ok, reason, metrics = evaluate_mm_promotion(c, "GOLDEN")
                exp = fx["expected"]
                if exp["evaluate_mm_promotion_ok"] != ok:
                    failures.append(
                        f"{fx['name']}: expected ok={exp['evaluate_mm_promotion_ok']}, "
                        f"got {ok} (reason={reason!r})"
                    )
                substr = exp.get("evaluate_mm_promotion_reason_contains")
                if substr and substr not in reason:
                    failures.append(
                        f"{fx['name']}: reason={reason!r} missing {substr!r}"
                    )
                if "n_fills" in exp and metrics.get("n_fills") != exp["n_fills"]:
                    failures.append(
                        f"{fx['name']}: n_fills={metrics.get('n_fills')} "
                        f"expected {exp['n_fills']}"
                    )
                if "pnl_per_fill_cents" in exp:
                    got = metrics.get("pnl_per_fill_cents")
                    if got != exp["pnl_per_fill_cents"]:
                        failures.append(
                            f"{fx['name']}: pnl_per_fill={got} "
                            f"expected {exp['pnl_per_fill_cents']}"
                        )
            finally:
                c.close()

        assert not failures, (
            "Golden fixture regressions:\n  - "
            + "\n  - ".join(failures)
            + "\n\nIf intentional, update tests/golden/mm_promotion_fixtures.json"
            " in this PR and explain why in the commit message."
        )


class TestKillSwitchGoldenFixtures:
    def test_kill_switch_fixtures(self, fixtures, conn):
        failures: list[str] = []
        for fx in fixtures["kill_switch_fixtures"]:
            c = init_db(":memory:")
            try:
                start = time.time() - 3600
                for i, lr in enumerate(fx["live_rows"]):
                    cur = c.execute(
                        "INSERT INTO weather_mm_shadow "
                        "(ts_unix, ts_iso, ticker, series, station, "
                        " fair_value_cents, proposed_bid_cents, "
                        " proposed_ask_cents, half_spread_cents, "
                        " gate_should_quote, live_mode) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (int(start + i * 60), "t", f"GOLD-K{i}",
                         "GOLDEN", "KJFK", 50, 40, 60, 10, 1, 1),
                    )
                    c.execute(
                        "UPDATE weather_mm_shadow SET shadow_bid_filled=?, "
                        "shadow_ask_filled=?, shadow_pnl_cents=?, "
                        "ts_settle_unix=? WHERE id=?",
                        (lr["bid_filled"], lr["ask_filled"], lr["pnl_cents"],
                         start + (i + 1) * 60, cur.lastrowid),
                    )
                c.commit()
                tripped, reason, _ = evaluate_mm_kill_switch(
                    c, "GOLDEN", equity_dollars=fx["equity_dollars"],
                )
                exp = fx["expected"]
                if exp["tripped"] != tripped:
                    failures.append(
                        f"{fx['name']}: expected tripped={exp['tripped']}, "
                        f"got {tripped} (reason={reason!r})"
                    )
                substr = exp.get("reason_contains")
                if substr and substr not in reason:
                    failures.append(
                        f"{fx['name']}: reason={reason!r} missing {substr!r}"
                    )
            finally:
                c.close()

        assert not failures, (
            "Kill-switch golden regressions:\n  - "
            + "\n  - ".join(failures)
        )
