"""Tests for ``bot.learning.weather_mos_materializer``.

Covers:
  * ticker → (city, settlement_date) parsing for the canonical regression
    cases (NY T67, MIA B84.5)
  * settlement-date eligibility window (today/future skipped, beyond
    max_back_days skipped)
  * morning-of snapshot picker (closest-to-12h, recency tiebreak)
  * per-(source, city, date, lead) idempotency under repeat runs
  * IEM miss → soft skip, no rows written, retry-clean on next pass
  * METAR exclusion (observation, not forecast — circular bias fit)
  * combined_v2 / afd_bias snapshots ignored
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

import bot.learning.weather_mos_materializer as wmm
from bot.db import init_db
from bot.learning.weather_mos_materializer import (
    _MATERIALIZED_LEAD_HOURS,
    _MATERIALIZED_SOURCES,
    _morning_of_per_source,
    _settlement_date_from_ticker,
    materialize_due,
)


# ── Stub out IEM with deterministic per-(station, date) returns ───────────


class _FakeIEM:
    """Replaces the IEM ASOS fetcher; tests register
    ``self.responses[(icao, date)] = high_f`` for each call expected."""

    def __init__(self):
        self.responses: dict[tuple[str, str], Optional[float]] = {}
        self.calls: list[tuple[str, str, str]] = []  # (icao, start, end)

    def __call__(self, station, start_date, end_date, *, session=None):
        self.calls.append((station.icao, start_date, end_date))
        # Build per_day across the widened window. Materializer asks ±1
        # day around the target date, so we synthesize all three keys when
        # the registered key falls inside the window.
        out = {}
        d = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
        while d <= end:
            iso = d.strftime("%Y-%m-%d")
            v = self.responses.get((station.icao, iso))
            if v is not None:
                out[iso] = v
            d += timedelta(days=1)
        return out


@pytest.fixture()
def fake_iem(monkeypatch):
    fake = _FakeIEM()

    # The materializer imports inside _fetch_iem_high. Monkey-patch the
    # source module so the in-function import resolves to our fake.
    import tools.backfill_weather_effective_n as bf

    monkeypatch.setattr(bf, "fetch_metar_daily_highs", fake)
    return fake


@pytest.fixture()
def conn():
    return init_db(":memory:")


def _insert_snapshot(conn, *, recorded_at, ticker, source,
                     forecast_high_f, sigma_f, hours_out,
                     series=None):
    if series is None:
        series = ticker.split("-", 1)[0]
    conn.execute(
        """INSERT INTO weather_forecast_snapshots
              (recorded_at, series, ticker, source, forecast_prob,
               forecast_high_f, sigma_f, hours_out)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (recorded_at, series, ticker, source, None,
         forecast_high_f, sigma_f, hours_out),
    )


# ── 1. Ticker → settlement_date parsing ────────────────────────────────────


def test_ticker_date_parser_handles_canonical_weather_tickers():
    assert _settlement_date_from_ticker("KXHIGHNY-26APR24-T67") == "2026-04-24"
    assert _settlement_date_from_ticker("KXHIGHMIA-26APR18-B84.5") == "2026-04-18"
    assert _settlement_date_from_ticker("KXHIGHCHI-26JAN05-T32") == "2026-01-05"
    assert _settlement_date_from_ticker("KXHIGHLAX-26DEC31-B70.5") == "2026-12-31"


def test_ticker_date_parser_returns_none_for_unparseable_shapes():
    # KXFED uses YY+MMM format (no DD), so weather-shaped regex must miss.
    assert _settlement_date_from_ticker("KXFED-26MAY-T425") is None
    assert _settlement_date_from_ticker("") is None
    assert _settlement_date_from_ticker("garbage") is None
    # Note: parser is shape-only; tickers like 'KXNBA-26APR20-LAL' will
    # parse fine. The *materializer* filters non-weather tickers by
    # city resolution failing in _city_for_ticker (covered separately).


def test_ticker_date_parser_rejects_invalid_calendar_dates():
    # Feb 30 doesn't exist; parser must reject without raising.
    assert _settlement_date_from_ticker("KXHIGHNY-26FEB30-T50") is None
    # Bogus month abbreviation.
    assert _settlement_date_from_ticker("KXHIGHNY-26ZZZ15-T50") is None


# ── 2. Snapshot picker: morning-of, ties broken by recency ─────────────────


def test_morning_of_picks_closest_to_12h(conn):
    ticker = "KXHIGHNY-26APR24-T67"
    # Three NWS snapshots at hours_out = 24, 11, 6. 11 is closest to 12.
    _insert_snapshot(
        conn, recorded_at="2026-04-23T14:00:00Z", ticker=ticker,
        source="nws_point", forecast_high_f=70.0, sigma_f=2.5, hours_out=24,
    )
    _insert_snapshot(
        conn, recorded_at="2026-04-24T08:00:00Z", ticker=ticker,
        source="nws_point", forecast_high_f=68.0, sigma_f=1.8, hours_out=11,
    )
    _insert_snapshot(
        conn, recorded_at="2026-04-24T13:00:00Z", ticker=ticker,
        source="nws_point", forecast_high_f=69.0, sigma_f=1.5, hours_out=6,
    )

    out = _morning_of_per_source(conn, ticker)
    assert out == {"nws_point": (68.0, 1.8)}


def test_morning_of_breaks_ties_by_latest_recorded_at(conn):
    ticker = "KXHIGHMIA-26APR18-B84.5"
    # Two snapshots at hours_out = 12 (perfect tie). Later recorded_at wins.
    _insert_snapshot(
        conn, recorded_at="2026-04-17T22:00:00Z", ticker=ticker,
        source="hrrr", forecast_high_f=85.0, sigma_f=2.0, hours_out=12,
    )
    _insert_snapshot(
        conn, recorded_at="2026-04-17T23:00:00Z", ticker=ticker,
        source="hrrr", forecast_high_f=85.5, sigma_f=2.0, hours_out=12,
    )

    out = _morning_of_per_source(conn, ticker)
    assert out == {"hrrr": (85.5, 2.0)}


def test_morning_of_excludes_combined_and_afd(conn):
    ticker = "KXHIGHNY-26APR24-T67"
    _insert_snapshot(
        conn, recorded_at="2026-04-24T08:00:00Z", ticker=ticker,
        source="combined_v2", forecast_high_f=68.5, sigma_f=1.5, hours_out=11,
    )
    _insert_snapshot(
        conn, recorded_at="2026-04-24T08:00:00Z", ticker=ticker,
        source="afd_bias", forecast_high_f=0.2, sigma_f=None, hours_out=None,
    )
    _insert_snapshot(
        conn, recorded_at="2026-04-24T08:00:00Z", ticker=ticker,
        source="metar", forecast_high_f=66.0, sigma_f=2.5, hours_out=11,
    )
    # Only canonical Gaussian forecast sources should appear.
    out = _morning_of_per_source(conn, ticker)
    assert out == {}, "combined_v2 / afd_bias / metar must be excluded"


# ── 3. End-to-end materialize_due ─────────────────────────────────────────


def _populate_full_ticker(conn, ticker, *, recorded_at, source_means):
    """Insert one snapshot per source for a ticker at the morning-of time."""
    for src, mean_f in source_means.items():
        _insert_snapshot(
            conn, recorded_at=recorded_at, ticker=ticker,
            source=src, forecast_high_f=mean_f, sigma_f=2.0, hours_out=12,
        )


def test_materialize_due_writes_one_row_per_canonical_source(conn, fake_iem):
    # NY ticker; source set covers exactly the canonical Gaussian sources
    # (minus metar, which the materializer excludes).
    ticker = "KXHIGHNY-26APR23-T67"
    means = {
        "hrrr": 68.0, "nbm": 67.5, "nws_point": 68.5,
        "tomorrow": 67.0, "weather": 68.2, "madis": 66.8,
    }
    _populate_full_ticker(
        conn, ticker, recorded_at="2026-04-23T08:00:00Z", source_means=means,
    )
    fake_iem.responses[("KNYC", "2026-04-23")] = 67.0  # observed

    stats = materialize_due(conn, today_iso="2026-04-25")

    assert stats["tickers_eligible"] == 1
    assert stats["city_dates_eligible"] == 1
    assert stats["iem_calls"] == 1
    assert stats["iem_misses"] == 0
    assert stats["rows_written"] == len(_MATERIALIZED_SOURCES)

    rows = conn.execute(
        "SELECT source, city, settlement_date, lead_hours, "
        "forecast_mean_f, observed_high_f "
        "FROM weather_gaussian_snapshots_backfill ORDER BY source"
    ).fetchall()
    assert len(rows) == len(_MATERIALIZED_SOURCES)
    for source, city, date, lead, mean, obs in rows:
        assert source in _MATERIALIZED_SOURCES
        assert city == "nyc"
        assert date == "2026-04-23"
        assert lead == _MATERIALIZED_LEAD_HOURS
        assert mean == pytest.approx(means[source], abs=1e-6)
        assert obs == pytest.approx(67.0, abs=1e-6)


def test_materialize_due_excludes_today_and_future(conn, fake_iem):
    today_iso = "2026-04-25"
    today_ticker = "KXHIGHNY-26APR25-T70"
    future_ticker = "KXHIGHNY-26APR26-T70"
    yesterday_ticker = "KXHIGHNY-26APR24-T70"

    for tk in (today_ticker, future_ticker, yesterday_ticker):
        _populate_full_ticker(
            conn, tk, recorded_at="2026-04-24T08:00:00Z",
            source_means={"hrrr": 70.0},
        )
    fake_iem.responses[("KNYC", "2026-04-24")] = 71.0

    stats = materialize_due(conn, today_iso=today_iso)
    assert stats["tickers_eligible"] == 1, "only yesterday is eligible"
    assert stats["city_dates_eligible"] == 1
    assert stats["rows_written"] == 1


def test_materialize_due_excludes_beyond_max_back_days(conn, fake_iem):
    today_iso = "2026-04-25"
    too_old = "KXHIGHNY-26APR05-T70"  # 20 days back
    just_in = "KXHIGHNY-26APR12-T70"  # 13 days back, within 14-day window

    for tk in (too_old, just_in):
        _populate_full_ticker(
            conn, tk, recorded_at="2026-04-12T08:00:00Z",
            source_means={"hrrr": 60.0},
        )
    fake_iem.responses[("KNYC", "2026-04-12")] = 62.0

    stats = materialize_due(conn, today_iso=today_iso, max_back_days=14)
    assert stats["tickers_eligible"] == 1
    assert stats["rows_written"] == 1


def test_materialize_due_dedups_iem_per_city_date(conn, fake_iem):
    """Two NY tickers (T66 + T67) settling on the same date trigger one IEM
    fetch, not two — and write one row per source, not two."""
    means = {"hrrr": 68.0}
    for tk in ("KXHIGHNY-26APR23-T66", "KXHIGHNY-26APR23-T67"):
        _populate_full_ticker(
            conn, tk, recorded_at="2026-04-23T08:00:00Z", source_means=means,
        )
    fake_iem.responses[("KNYC", "2026-04-23")] = 67.0

    stats = materialize_due(conn, today_iso="2026-04-25")
    assert stats["tickers_eligible"] == 2
    assert stats["city_dates_eligible"] == 1, "siblings collapse to one (city,date)"
    assert stats["iem_calls"] == 1
    assert stats["rows_written"] == 1, "one row per (source, city, date, lead)"


def test_materialize_due_idempotent_under_repeat_runs(conn, fake_iem):
    ticker = "KXHIGHMIA-26APR18-B84.5"
    _populate_full_ticker(
        conn, ticker, recorded_at="2026-04-18T08:00:00Z",
        source_means={"hrrr": 85.0, "nbm": 84.5},
    )
    fake_iem.responses[("KMIA", "2026-04-18")] = 86.0

    s1 = materialize_due(conn, today_iso="2026-04-25")
    s2 = materialize_due(conn, today_iso="2026-04-25")

    assert s1["rows_written"] == 2
    assert s2["rows_written"] == 0, "second pass is a no-op"
    rows = conn.execute(
        "SELECT COUNT(*) FROM weather_gaussian_snapshots_backfill "
        "WHERE city = 'miami' AND settlement_date = '2026-04-18'"
    ).fetchone()[0]
    assert rows == 2


def test_materialize_due_iem_miss_soft_skips_and_retries(conn, fake_iem):
    ticker = "KXHIGHCHI-26APR20-T55"
    _populate_full_ticker(
        conn, ticker, recorded_at="2026-04-20T08:00:00Z",
        source_means={"hrrr": 56.0, "nbm": 55.5},
    )
    # Pass 1: IEM has no response → soft skip
    s1 = materialize_due(conn, today_iso="2026-04-25")
    assert s1["iem_calls"] == 1
    assert s1["iem_misses"] == 1
    assert s1["rows_written"] == 0
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM weather_gaussian_snapshots_backfill"
        ).fetchone()[0] == 0
    )

    # Pass 2: IEM now returns the high — re-runs and writes
    fake_iem.responses[("KMDW", "2026-04-20")] = 57.5
    s2 = materialize_due(conn, today_iso="2026-04-25")
    assert s2["iem_misses"] == 0
    assert s2["rows_written"] == 2


def test_materialize_due_skips_unresolved_city(conn, fake_iem):
    """A weather-shaped ticker for a series we don't have a station for
    must skip cleanly without an IEM call."""
    ticker = "KXHIGHZZZ-26APR20-T50"  # bogus city series
    _populate_full_ticker(
        conn, ticker, recorded_at="2026-04-20T08:00:00Z",
        source_means={"hrrr": 50.0},
    )
    stats = materialize_due(conn, today_iso="2026-04-25")
    assert stats["tickers_eligible"] == 1
    assert stats["tickers_unresolved_city"] == 1
    assert stats["iem_calls"] == 0
    assert stats["rows_written"] == 0


def test_materialized_city_matches_v2_reader_key(conn, fake_iem):
    """End-to-end: materialize for Los Angeles, persist via the bias
    fitter, verify the v2 reader's _city_key("los angeles") =
    "los_angeles" reads back the same kv key the fitter wrote."""
    from tools.backfill_weather_effective_n import (
        fit_mos_bias, persist_mos_bias,
    )
    from bot.signals.weather_ensemble_v2 import _city_key as v2_city_key

    # Sufficient rows for the fitter's eff_n gate (default min_samples=10
    # in persist_mos_bias). Spread over recent dates so EWMA weights stay high.
    base = datetime(2026, 4, 5, tzinfo=timezone.utc)
    for i in range(15):
        date_iso = (base + timedelta(days=i)).strftime("%Y-%m-%d")
        ticker = f"KXHIGHLAX-{(base + timedelta(days=i)).strftime('%y%b%d').upper()}-T70"
        _populate_full_ticker(
            conn, ticker, recorded_at=f"{date_iso}T08:00:00Z",
            source_means={"hrrr": 75.0},
        )
        fake_iem.responses[("KLAX", date_iso)] = 73.0  # +2°F warm bias

    materialize_due(conn, today_iso="2026-04-25", max_back_days=30)

    # City stored as 'los angeles' (raw station.city) — fitter normalizes.
    rows = conn.execute(
        "SELECT DISTINCT city FROM weather_gaussian_snapshots_backfill"
    ).fetchall()
    assert ("los angeles",) in rows

    fits = fit_mos_bias(conn, ref_date_iso="2026-04-25")
    hrrr_lax = next(
        (f for f in fits if f.source == "hrrr" and f.city == "los angeles"),
        None,
    )
    assert hrrr_lax is not None
    assert hrrr_lax.bias_f == pytest.approx(2.0, abs=1e-3)

    # Persist + read back via v2 reader's key shape.
    import bot.db as bot_db
    saved_persist = bot_db._PERSIST_CONN
    bot_db._PERSIST_CONN = conn
    try:
        persist_mos_bias(conn, fits)
        from bot.signals.weather_ensemble_v2 import _get_mos_bias
        bias = _get_mos_bias("hrrr", v2_city_key("los angeles"))
    finally:
        bot_db._PERSIST_CONN = saved_persist

    assert bias is not None
    assert bias == pytest.approx(2.0, abs=1e-3)
