"""Tests for ``bot.learning.shadow_calibration_bridge``.

The bridge converts settled ``weather_mm_shadow`` rows into
``calibration`` training rows to unblock the Platt fitter without
waiting for fresh ``alpha_backtest`` accumulation. These tests cover:

  - empty table: no work
  - unsettled rows skipped (all three settlement columns required)
  - rows inserted with the expected column values
  - watermark advances to max(id) seen
  - second invocation is a no-op (watermark dedup)
  - new rows after watermark are picked up
  - invalid fv / settled_yes values are counted as skipped, not crashed
  - ``batch_limit`` caps work per call and advances watermark proportionally
  - Platt fitter runs end-to-end on bridged data (integration smoke)
"""
from __future__ import annotations

import time

import pytest

from bot.db import init_db
from bot.learning import shadow_calibration_bridge as bridge
from bot.learning.calibration import fit_calibration


# ── Fixtures ────────────────────────────────────────────────────────────
@pytest.fixture
def conn():
    c = init_db(":memory:")
    yield c
    c.close()


def _insert_shadow_row(
    conn,
    *,
    ticker: str,
    series: str = "KXHIGHNY",
    station: str = "KJFK",
    fv: int | None = 50,
    ts_unix: float | None = None,
    settled_yes: int | None = None,
    ts_settle_unix: float | None = None,
    live_mode: int = 0,
) -> int:
    if ts_unix is None:
        ts_unix = time.time() - 3600
    cur = conn.execute(
        "INSERT INTO weather_mm_shadow "
        "(ts_unix, ts_iso, ticker, series, station, "
        " fair_value_cents, market_yes_bid, market_yes_ask, market_mid, "
        " gate_should_quote, live_mode, "
        " ticker_settled_yes, ts_settle_unix) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (int(ts_unix), "t", ticker, series, station,
         fv, 45, 55, 50, 1, live_mode,
         settled_yes, ts_settle_unix),
    )
    conn.commit()
    return int(cur.lastrowid)


# ── Tests ───────────────────────────────────────────────────────────────

def test_empty_table_is_noop(conn):
    stats = bridge.bridge_shadow_to_calibration(conn)
    assert stats["rows_bridged"] == 0
    assert stats["watermark_before"] == 0
    assert stats["watermark_after"] == 0
    assert conn.execute("SELECT COUNT(*) FROM calibration").fetchone()[0] == 0


def test_unsettled_rows_are_skipped(conn):
    # Missing ts_settle_unix → unsettled, should not bridge
    _insert_shadow_row(
        conn, ticker="KXHIGHNY-26APR21-T75", fv=40,
        settled_yes=1, ts_settle_unix=None,
    )
    # Missing ticker_settled_yes → unsettled
    _insert_shadow_row(
        conn, ticker="KXHIGHNY-26APR21-T76", fv=40,
        settled_yes=None, ts_settle_unix=time.time(),
    )
    # Missing fv → skip (can't produce est_prob)
    _insert_shadow_row(
        conn, ticker="KXHIGHNY-26APR21-T77", fv=None,
        settled_yes=1, ts_settle_unix=time.time(),
    )
    stats = bridge.bridge_shadow_to_calibration(conn)
    assert stats["rows_bridged"] == 0
    assert conn.execute("SELECT COUNT(*) FROM calibration").fetchone()[0] == 0


def test_settled_row_creates_calibration_row(conn):
    _insert_shadow_row(
        conn, ticker="KXHIGHNY-26APR21-T75", fv=37,
        settled_yes=0, ts_settle_unix=time.time(),
    )
    stats = bridge.bridge_shadow_to_calibration(conn)
    assert stats["rows_bridged"] == 1
    assert stats["tickers_touched"] == 1
    row = conn.execute(
        "SELECT ticker, estimated_prob, actual_outcome, source_desc, "
        "       n_sources, bucket "
        "FROM calibration"
    ).fetchone()
    assert row[0] == "KXHIGHNY-26APR21-T75"
    assert row[1] == pytest.approx(0.37, abs=1e-6)
    assert row[2] == 0
    assert row[3] == bridge.SOURCE_DESC
    assert row[4] is None
    assert row[5] == "0.3-0.4"


def test_watermark_advances_to_max_id(conn):
    id1 = _insert_shadow_row(
        conn, ticker="KXHIGHNY-26APR21-T75", fv=30,
        settled_yes=0, ts_settle_unix=time.time(),
    )
    id2 = _insert_shadow_row(
        conn, ticker="KXHIGHNY-26APR21-T76", fv=60,
        settled_yes=1, ts_settle_unix=time.time(),
    )
    stats = bridge.bridge_shadow_to_calibration(conn)
    assert stats["watermark_after"] == id2
    assert stats["watermark_after"] > id1
    row = conn.execute(
        "SELECT value FROM kv_cache WHERE key=?", (bridge.WATERMARK_KEY,)
    ).fetchone()
    assert row is not None
    assert int(row[0]) == id2


def test_second_invocation_is_noop(conn):
    _insert_shadow_row(
        conn, ticker="KXHIGHNY-26APR21-T75", fv=37,
        settled_yes=0, ts_settle_unix=time.time(),
    )
    bridge.bridge_shadow_to_calibration(conn)
    n_before = conn.execute("SELECT COUNT(*) FROM calibration").fetchone()[0]
    stats = bridge.bridge_shadow_to_calibration(conn)
    n_after = conn.execute("SELECT COUNT(*) FROM calibration").fetchone()[0]
    assert stats["rows_bridged"] == 0
    assert n_after == n_before


def test_new_rows_after_watermark_are_picked_up(conn):
    _insert_shadow_row(
        conn, ticker="KXHIGHNY-26APR21-T75", fv=37,
        settled_yes=0, ts_settle_unix=time.time(),
    )
    bridge.bridge_shadow_to_calibration(conn)

    # New settled row lands after watermark
    _insert_shadow_row(
        conn, ticker="KXHIGHNY-26APR22-T80", fv=80,
        settled_yes=1, ts_settle_unix=time.time(),
    )
    stats = bridge.bridge_shadow_to_calibration(conn)
    assert stats["rows_bridged"] == 1
    assert conn.execute("SELECT COUNT(*) FROM calibration").fetchone()[0] == 2


def test_invalid_fv_counted_as_skip(conn):
    # fv > 100 is out of [0,1] after /100 → skipped
    _insert_shadow_row(
        conn, ticker="KXHIGHNY-26APR21-T75", fv=150,
        settled_yes=1, ts_settle_unix=time.time(),
    )
    # fv < 0 → skipped
    _insert_shadow_row(
        conn, ticker="KXHIGHNY-26APR21-T76", fv=-10,
        settled_yes=0, ts_settle_unix=time.time(),
    )
    # Valid row
    id3 = _insert_shadow_row(
        conn, ticker="KXHIGHNY-26APR21-T77", fv=55,
        settled_yes=1, ts_settle_unix=time.time(),
    )
    stats = bridge.bridge_shadow_to_calibration(conn)
    assert stats["rows_bridged"] == 1
    assert stats["skipped_invalid"] == 2
    # Watermark still advances past all rows (we've seen them).
    assert stats["watermark_after"] == id3


def test_batch_limit_caps_per_call(conn):
    ids = [
        _insert_shadow_row(
            conn, ticker=f"KXHIGHNY-26APR21-T{75 + i}", fv=40 + i,
            settled_yes=(i % 2), ts_settle_unix=time.time(),
        )
        for i in range(5)
    ]
    stats = bridge.bridge_shadow_to_calibration(conn, batch_limit=2)
    assert stats["rows_bridged"] == 2
    assert stats["watermark_after"] == ids[1]
    # Second batch picks up next slice
    stats2 = bridge.bridge_shadow_to_calibration(conn, batch_limit=2)
    assert stats2["rows_bridged"] == 2
    assert stats2["watermark_after"] == ids[3]
    # Third batch finishes
    stats3 = bridge.bridge_shadow_to_calibration(conn, batch_limit=2)
    assert stats3["rows_bridged"] == 1
    assert stats3["watermark_after"] == ids[4]


def test_fv_zero_and_hundred_are_valid(conn):
    """Boundary values must pass — fv=0 → est_prob=0.0, fv=100 → 1.0."""
    _insert_shadow_row(
        conn, ticker="KXHIGHNY-26APR21-T75", fv=0,
        settled_yes=0, ts_settle_unix=time.time(),
    )
    _insert_shadow_row(
        conn, ticker="KXHIGHNY-26APR21-T76", fv=100,
        settled_yes=1, ts_settle_unix=time.time(),
    )
    stats = bridge.bridge_shadow_to_calibration(conn)
    assert stats["rows_bridged"] == 2


def test_multiple_rows_per_ticker_all_bridged(conn):
    """A ticker typically has many shadow rows across its lifecycle — each
    should become an independent training sample. That preserves the
    temperature-trajectory calibration signal."""
    for fv in (20, 35, 50, 65, 80):
        _insert_shadow_row(
            conn, ticker="KXHIGHNY-26APR21-T75", fv=fv,
            settled_yes=1, ts_settle_unix=time.time(),
        )
    stats = bridge.bridge_shadow_to_calibration(conn)
    assert stats["rows_bridged"] == 5
    assert stats["tickers_touched"] == 1


def test_platt_fit_runs_on_bridged_data(conn):
    """End-to-end smoke: bridge N rows with a systematic bias (estimates
    consistently higher than outcomes), fit_calibration sees it and
    returns a non-identity curve."""
    # Generate 60 biased samples across two families. Estimates centered
    # at 0.7 but outcomes only 40% yes → classic over-confidence.
    import random
    random.seed(42)
    ts = time.time()
    for i in range(60):
        fam = "KXHIGHNY" if i % 2 == 0 else "KXHIGHMIA"
        fv = random.randint(60, 80)  # estimate 0.60-0.80
        actual = 1 if random.random() < 0.40 else 0
        _insert_shadow_row(
            conn,
            ticker=f"{fam}-26APR21-T{75 + i}",
            series=fam, station="KJFK",
            fv=fv, settled_yes=actual, ts_settle_unix=ts,
        )
    stats = bridge.bridge_shadow_to_calibration(conn)
    assert stats["rows_bridged"] == 60

    curve = fit_calibration(conn)
    # Enough samples for Platt; fit should be active (not identity).
    assert curve["method"] == "platt"
    assert curve["n_samples"] == 60
    # A systematic over-confidence sample should reduce brier after fit.
    assert curve["brier_after"] <= curve["brier_before"] + 1e-6


def test_watermark_survives_across_bridge_invocations(conn):
    """Integration: the watermark persisted in kv_cache is what blocks
    re-bridging on a second run. Corrupting it resets dedup."""
    _insert_shadow_row(
        conn, ticker="KXHIGHNY-26APR21-T75", fv=37,
        settled_yes=0, ts_settle_unix=time.time(),
    )
    bridge.bridge_shadow_to_calibration(conn)
    assert conn.execute("SELECT COUNT(*) FROM calibration").fetchone()[0] == 1

    # Corrupt the watermark — bridge should treat as 0 and reprocess.
    conn.execute(
        "UPDATE kv_cache SET value='not-an-int' WHERE key=?",
        (bridge.WATERMARK_KEY,),
    )
    conn.commit()
    stats = bridge.bridge_shadow_to_calibration(conn)
    # With watermark reset to 0, the existing row is re-bridged.
    assert stats["rows_bridged"] == 1
    assert conn.execute("SELECT COUNT(*) FROM calibration").fetchone()[0] == 2
