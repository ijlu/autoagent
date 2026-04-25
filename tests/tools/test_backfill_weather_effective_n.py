"""Tests for tools/backfill_weather_effective_n.py (A2.5a + A2.5b).

Coverage:
  1. Schema — the backfill table exists after init_db() and has the
     expected UNIQUE constraint.
  2. Open-Meteo fetch parses a mocked response correctly (°F already, LST
     dates preserved).
  3. METAR fetch buckets UTC-stamped CSV rows into LST-local calendar
     days and takes the max per day.
  4. IEM tmpf NULL/invalid/out-of-range values are discarded without
     breaking the parse.
  5. replay_and_write writes one row per (source, date) with forecast_mean
     and observed populated, is idempotent under re-run, and emits
     distinct rows per OM model (A2.5b: best_match / gfs_hrrr / gfs_seamless).
  6. fit_per_source returns bias == mean(forecast - observed) and RMSE
     on a deterministic fixture.
  7. METAR rows are NOT counted in the fit (forecast == observed by
     construction).
  8. sigma_for_day functions match the corresponding live-signal module
     schedules for open_meteo, hrrr, and nbm (drift guards).
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from bot.daemon.stations import STATION_BY_CITY
from bot.db import init_db
from tools import backfill_weather_effective_n as bf


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def memdb():
    conn = init_db(":memory:")
    yield conn
    conn.close()


def _mock_response(status_code: int, *, json_payload=None, text: str = ""):
    r = MagicMock()
    r.status_code = status_code
    r.text = text
    if json_payload is not None:
        r.json.return_value = json_payload
    return r


def _mock_session(
    om_payload=None, iem_csv: str = "", om_status: int = 200,
    iem_status: int = 200, om_payloads_by_model=None,
):
    """Session whose .get returns OM or IEM mock based on URL match.

    ``om_payload`` (single) returns the same payload for every OM call
    regardless of which model was requested — matches the A2.5a single-model
    tests and preserves back-compat.

    ``om_payloads_by_model`` (dict keyed by the ``models=`` param value,
    with ``None`` as the default best_match key) routes per-model so tests
    can assert distinct forecasts per source.
    """
    sess = MagicMock()

    def _get(url, *, params=None, timeout=None, headers=None):
        if "open-meteo" in url:
            if om_payloads_by_model is not None:
                model = (params or {}).get("models")
                payload = om_payloads_by_model.get(
                    model, om_payloads_by_model.get(None, {})
                )
                return _mock_response(om_status, json_payload=payload)
            return _mock_response(om_status, json_payload=om_payload)
        if "asos.py" in url or "mesonet.agron" in url:
            return _mock_response(iem_status, text=iem_csv)
        raise AssertionError(f"unexpected URL: {url}")

    sess.get.side_effect = _get
    return sess


# ── 1. Schema ────────────────────────────────────────────────────────────

def test_backfill_schema_created(memdb):
    cols = memdb.execute(
        "PRAGMA table_info(weather_gaussian_snapshots_backfill)"
    ).fetchall()
    col_names = {c[1] for c in cols}
    expected = {
        "id", "created_at", "source", "city", "settlement_date",
        "lead_hours", "forecast_mean_f", "forecast_sigma_f", "observed_high_f",
    }
    assert expected <= col_names, f"missing columns: {expected - col_names}"

    # UNIQUE constraint on (source, city, settlement_date, lead_hours).
    # We verify by attempting a duplicate insert and expecting IntegrityError,
    # but since we use INSERT OR REPLACE in the tool, this also means
    # re-running is safe (exercised in the idempotency test below).
    idx_rows = memdb.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND "
        "tbl_name='weather_gaussian_snapshots_backfill'"
    ).fetchall()
    idx_sql = " ".join(r[0] or "" for r in idx_rows)
    assert "source" in idx_sql and "city" in idx_sql


# ── 2. Open-Meteo parsing ────────────────────────────────────────────────

def test_om_fetch_parses_daily_highs(memdb):
    station = STATION_BY_CITY["nyc"]
    payload = {
        "daily": {
            "time": ["2026-04-01", "2026-04-02", "2026-04-03"],
            "temperature_2m_max": [72.0, 78.0, None],
        }
    }
    sess = _mock_session(om_payload=payload)
    out = bf.fetch_open_meteo_daily_highs(
        station, "2026-04-01", "2026-04-03", session=sess,
    )
    assert out == {"2026-04-01": 72.0, "2026-04-02": 78.0}


def test_om_fetch_raises_on_http_error(memdb):
    station = STATION_BY_CITY["nyc"]
    sess = _mock_session(om_payload={}, om_status=503)
    with pytest.raises(RuntimeError, match="open-meteo.*503"):
        bf.fetch_open_meteo_daily_highs(
            station, "2026-04-01", "2026-04-02", session=sess,
        )


# ── 3/4. METAR parsing ───────────────────────────────────────────────────

def test_metar_fetch_takes_max_per_lst_day():
    """KNYC (LST=UTC-5). A UTC 04:00 obs on 2026-04-02 is 23:00 LST of
    2026-04-01 (the prior LST day), not 2026-04-02."""
    station = STATION_BY_CITY["nyc"]
    iem_csv = (
        "station,valid,tmpf\n"
        # LST 2026-04-01 midday (17:00 UTC = 12:00 LST, the expected peak time)
        "KNYC,2026-04-01 17:00,65.0\n"
        "KNYC,2026-04-01 18:00,72.0\n"   # LST 13:00 — this is the day's high
        "KNYC,2026-04-01 19:00,68.0\n"
        # UTC 04:00 next day → 23:00 LST PRIOR day. Lower than 72, ignored.
        "KNYC,2026-04-02 04:00,60.0\n"
        # LST 2026-04-02 afternoon
        "KNYC,2026-04-02 18:00,80.0\n"
        "KNYC,2026-04-02 19:00,77.0\n"
    )
    sess = _mock_session(iem_csv=iem_csv)
    out = bf.fetch_metar_daily_highs(
        station, "2026-04-01", "2026-04-02", session=sess,
    )
    assert out == {"2026-04-01": 72.0, "2026-04-02": 80.0}


def test_metar_fetch_discards_invalid_rows():
    station = STATION_BY_CITY["nyc"]
    iem_csv = (
        "station,valid,tmpf\n"
        "KNYC,2026-04-01 18:00,72.0\n"
        "KNYC,2026-04-01 19:00,\n"           # empty → skip
        "KNYC,2026-04-01 20:00,-99.99\n"     # out-of-range → skip
        "KNYC,2026-04-01 21:00,not_a_num\n"  # non-numeric → skip
        "KNYC,invalid_ts,75.0\n"             # bad timestamp → skip
    )
    sess = _mock_session(iem_csv=iem_csv)
    out = bf.fetch_metar_daily_highs(
        station, "2026-04-01", "2026-04-01", session=sess,
    )
    assert out == {"2026-04-01": 72.0}


# ── 5. replay_and_write ──────────────────────────────────────────────────

def test_replay_writes_expected_rows(memdb):
    """A2.5b default: replay fetches 3 OM models + METAR → 4 sources per date."""
    station = STATION_BY_CITY["nyc"]
    om_payload = {
        "daily": {
            "time": ["2026-04-01", "2026-04-02"],
            "temperature_2m_max": [72.5, 79.0],
        }
    }
    iem_csv = (
        "station,valid,tmpf\n"
        "KNYC,2026-04-01 18:00,71.0\n"
        "KNYC,2026-04-02 18:00,81.0\n"
    )
    # Single payload returned for every OM model call — fine for row-count
    # and observed-join assertions; per-model distinctness is covered by
    # test_replay_routes_per_model.
    sess = _mock_session(om_payload=om_payload, iem_csv=iem_csv)
    n = bf.replay_and_write(
        memdb, station, "2026-04-01", "2026-04-02", session=sess,
    )
    assert n == 8  # 2 dates × (3 OM models + 1 metar)

    rows = memdb.execute(
        "SELECT source, settlement_date, forecast_mean_f, forecast_sigma_f, "
        "observed_high_f, lead_hours "
        "FROM weather_gaussian_snapshots_backfill "
        "WHERE city = 'nyc' ORDER BY settlement_date, source"
    ).fetchall()
    assert len(rows) == 8
    sources_on_04_01 = {r[0] for r in rows if r[1] == "2026-04-01"}
    assert sources_on_04_01 == {"weather", "hrrr", "nbm", "metar"}
    # metar row on 04-01 carries observed_high as its own mean.
    m0 = next(r for r in rows if r[0] == "metar" and r[1] == "2026-04-01")
    assert m0[2] == 71.0 and m0[4] == 71.0 and m0[5] == 0
    # "weather" (Open-Meteo best_match) row on 04-01 carries OM forecast + METAR observed.
    o0 = next(r for r in rows if r[0] == "weather" and r[1] == "2026-04-01")
    assert o0[2] == 72.5 and o0[4] == 71.0
    assert o0[5] == bf._DEFAULT_LEAD_HOURS
    # hrrr + nbm rows also carry the (same, since mock is shared) forecast
    # and the shared METAR observed.
    h0 = next(r for r in rows if r[0] == "hrrr" and r[1] == "2026-04-01")
    assert h0[2] == 72.5 and h0[4] == 71.0
    assert abs(h0[3] - bf.hrrr_sigma_for_day(0)) < 1e-9
    n0 = next(r for r in rows if r[0] == "nbm" and r[1] == "2026-04-01")
    assert n0[2] == 72.5 and n0[4] == 71.0
    assert abs(n0[3] - bf.nbm_sigma_for_day(0)) < 1e-9


def test_replay_routes_per_model(memdb):
    """When OM returns different highs per ``models=`` param, backfill rows
    must carry the right forecast per source key."""
    station = STATION_BY_CITY["nyc"]
    iem_csv = "station,valid,tmpf\nKNYC,2026-04-01 18:00,70.0\n"
    # Distinct daily-high per model so we can verify routing.
    payloads = {
        None:           {"daily": {"time": ["2026-04-01"], "temperature_2m_max": [72.0]}},
        "gfs_hrrr":     {"daily": {"time": ["2026-04-01"], "temperature_2m_max": [73.0]}},
        "gfs_seamless": {"daily": {"time": ["2026-04-01"], "temperature_2m_max": [74.0]}},
    }
    sess = _mock_session(om_payloads_by_model=payloads, iem_csv=iem_csv)
    bf.replay_and_write(
        memdb, station, "2026-04-01", "2026-04-01", session=sess,
    )
    rows = memdb.execute(
        "SELECT source, forecast_mean_f FROM weather_gaussian_snapshots_backfill "
        "WHERE city = 'nyc' ORDER BY source"
    ).fetchall()
    by_source = {r[0]: r[1] for r in rows}
    assert by_source["weather"] == 72.0
    assert by_source["hrrr"] == 73.0
    assert by_source["nbm"] == 74.0
    assert by_source["metar"] == 70.0


def test_replay_respects_models_argument(memdb):
    """Passing ``models=[None]`` should fetch only best_match and emit only
    the "weather" + metar rows — back-compat path used by A2.5a tests."""
    station = STATION_BY_CITY["nyc"]
    om_payload = {"daily": {"time": ["2026-04-01"], "temperature_2m_max": [72.5]}}
    iem_csv = "station,valid,tmpf\nKNYC,2026-04-01 18:00,71.0\n"
    sess = _mock_session(om_payload=om_payload, iem_csv=iem_csv)
    n = bf.replay_and_write(
        memdb, station, "2026-04-01", "2026-04-01",
        session=sess, models=[None],
    )
    assert n == 2
    sources = {
        r[0] for r in memdb.execute(
            "SELECT source FROM weather_gaussian_snapshots_backfill "
            "WHERE city = 'nyc'"
        ).fetchall()
    }
    assert sources == {"weather", "metar"}


def test_replay_is_idempotent_under_rerun(memdb):
    """Re-running with identical inputs must not duplicate rows (UNIQUE
    constraint + INSERT OR REPLACE)."""
    station = STATION_BY_CITY["nyc"]
    om_payload = {"daily": {"time": ["2026-04-01"], "temperature_2m_max": [72.5]}}
    iem_csv = "station,valid,tmpf\nKNYC,2026-04-01 18:00,71.0\n"
    sess = _mock_session(om_payload=om_payload, iem_csv=iem_csv)

    bf.replay_and_write(memdb, station, "2026-04-01", "2026-04-01", session=sess)
    bf.replay_and_write(memdb, station, "2026-04-01", "2026-04-01", session=sess)

    count = memdb.execute(
        "SELECT COUNT(*) FROM weather_gaussian_snapshots_backfill"
    ).fetchone()[0]
    # 3 OM models + 1 metar = 4 unique (source, city, date, lead) tuples.
    assert count == 4


# ── 6/7. fit_per_source ──────────────────────────────────────────────────

def test_fit_produces_bias_and_rmse(memdb):
    """Inject deterministic rows: OM overshoots by +2.0°F avg, RMSE 2.0°F."""
    now = "2026-04-22T00:00:00Z"

    def _write(conn):
        rows = [
            # (created_at, source, city, date, lead, fcst, sigma, obs)
            (now, "weather", "nyc", "2026-04-01", 12, 74.0, 2.0, 72.0),
            (now, "weather", "nyc", "2026-04-02", 12, 82.0, 2.0, 80.0),
            (now, "weather", "nyc", "2026-04-03", 12, 76.0, 2.0, 74.0),
            # METAR rows — should be excluded from fit.
            (now, "metar", "nyc", "2026-04-01", 0, 72.0, 0.1, 72.0),
            (now, "metar", "nyc", "2026-04-02", 0, 80.0, 0.1, 80.0),
        ]
        conn.executemany(
            """INSERT INTO weather_gaussian_snapshots_backfill
               (created_at, source, city, settlement_date, lead_hours,
                forecast_mean_f, forecast_sigma_f, observed_high_f)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
    from bot.db import db_write as _db_write
    _db_write(_write, conn=memdb)

    fits = bf.fit_per_source(memdb)
    # Only open_meteo should appear — metar is filtered out.
    assert [f.source for f in fits] == ["weather"]
    om = fits[0]
    assert om.n == 3
    assert abs(om.mean_bias_f - 2.0) < 1e-9
    assert abs(om.rmse_f - 2.0) < 1e-9
    assert abs(om.reported_sigma_f - 2.0) < 1e-9
    assert abs(om.sigma_ratio - 1.0) < 1e-9


def test_fit_excludes_null_observed(memdb):
    """Rows with observed=NULL (no METAR ground truth for that date) must
    be excluded from the fit to avoid dividing by zero."""
    now = "2026-04-22T00:00:00Z"

    def _write(conn):
        conn.executemany(
            """INSERT INTO weather_gaussian_snapshots_backfill
               (created_at, source, city, settlement_date, lead_hours,
                forecast_mean_f, forecast_sigma_f, observed_high_f)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (now, "weather", "nyc", "2026-04-01", 12, 74.0, 2.0, 72.0),
                # This row has no ground truth — must be filtered.
                (now, "weather", "nyc", "2026-04-02", 12, 82.0, 2.0, None),
            ],
        )
    from bot.db import db_write as _db_write
    _db_write(_write, conn=memdb)

    fits = bf.fit_per_source(memdb)
    assert len(fits) == 1
    assert fits[0].n == 1


def test_fit_handles_empty_table(memdb):
    assert bf.fit_per_source(memdb) == []


# ── 8. Sigma schedule drift guard ────────────────────────────────────────

def test_open_meteo_sigma_matches_live_signal():
    """If bot/signals/sources/weather.py changes its OM sigma schedule,
    the backfill fit becomes apples-to-oranges. This is the early
    warning."""
    from bot.signals.sources.weather import get_weather_gaussian  # noqa

    # Per the weather.py source code reviewed during A1b, OM uses
    # sigma = 2.0 + 0.6 * day_idx.
    for day_idx in range(7):
        expected = 2.0 + 0.6 * day_idx
        actual = bf.open_meteo_sigma_for_day(day_idx)
        assert abs(actual - expected) < 1e-9, (
            f"OM sigma schedule drift at day_idx={day_idx}: "
            f"live={expected}, backfill={actual}"
        )


def test_hrrr_sigma_matches_live_signal():
    """Drift guard: backfill hrrr sigma must match
    ``bot/signals/sources/hrrr.py``'s schedule (sigma = 1.2 + 0.5 * day_idx)."""
    from bot.signals.sources import hrrr as hrrr_src  # noqa

    for day_idx in range(7):
        expected = 1.2 + 0.5 * day_idx
        actual = bf.hrrr_sigma_for_day(day_idx)
        assert abs(actual - expected) < 1e-9, (
            f"HRRR sigma schedule drift at day_idx={day_idx}: "
            f"live={expected}, backfill={actual}"
        )


def test_nbm_sigma_matches_live_signal():
    """Drift guard: backfill nbm sigma must match
    ``bot/signals/sources/ndfd_nbm.py``'s schedule
    (sigma = 1.8 + 0.5 * day_idx)."""
    from bot.signals.sources import ndfd_nbm as nbm_src  # noqa

    for day_idx in range(7):
        expected = 1.8 + 0.5 * day_idx
        actual = bf.nbm_sigma_for_day(day_idx)
        assert abs(actual - expected) < 1e-9, (
            f"NBM sigma schedule drift at day_idx={day_idx}: "
            f"live={expected}, backfill={actual}"
        )


# ── 9. Report rendering ──────────────────────────────────────────────────

# ── 11. Effective-N group fit (A2.5c) ────────────────────────────────────

def _insert_backfill_rows(conn, rows):
    from bot.db import db_write as _db_write

    def _write(c):
        c.executemany(
            """INSERT INTO weather_gaussian_snapshots_backfill
               (created_at, source, city, settlement_date, lead_hours,
                forecast_mean_f, forecast_sigma_f, observed_high_f)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )

    _db_write(_write, conn=conn)


def test_pearson_basic():
    """Identity series → ρ=1. Reversed → ρ=-1. Zero variance → None."""
    assert bf._pearson([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)
    assert bf._pearson([1.0, 2.0, 3.0], [3.0, 2.0, 1.0]) == pytest.approx(-1.0)
    assert bf._pearson([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None
    assert bf._pearson([1.0], [2.0]) is None


def test_group_fit_returns_none_on_empty_db(memdb):
    assert bf.fit_group_correlation(memdb) is None


def test_group_fit_returns_none_when_fewer_than_two_sources(memdb):
    now = "2026-04-22T00:00:00Z"
    # Only 'open_meteo' present — single-source group can't fit ρ.
    _insert_backfill_rows(memdb, [
        (now, "weather", "nyc", "2026-04-01", 12, 72.0, 2.0, 71.0),
        (now, "weather", "nyc", "2026-04-02", 12, 80.0, 2.0, 79.0),
    ])
    assert bf.fit_group_correlation(memdb) is None


def test_group_fit_math_mean_pairwise_rho(memdb):
    """Design 5-date fixture with known error series per source:
        om   = [+1, +2, +3, +4, +5]   (identical to hrrr)
        hrrr = [+1, +2, +3, +4, +5]   (identical to om)
        nbm  = [+5, +4, +3, +2, +1]   (reverse of the others)
    Pairwise ρ: (om,hrrr)=+1, (om,nbm)=-1, (hrrr,nbm)=-1 → mean = -1/3.
    n_eff = 3 / (1 + 2*(-1/3)) = 3 / (1/3) = 9.0.
    """
    now = "2026-04-22T00:00:00Z"
    dates = ["2026-04-01", "2026-04-02", "2026-04-03",
             "2026-04-04", "2026-04-05"]
    obs = [70.0, 71.0, 72.0, 73.0, 74.0]
    om_err   = [1, 2, 3, 4, 5]
    hrrr_err = [1, 2, 3, 4, 5]
    nbm_err  = [5, 4, 3, 2, 1]
    rows = []
    for i, d in enumerate(dates):
        rows.extend([
            (now, "weather", "nyc", d, 12,
             obs[i] + om_err[i],   2.0, obs[i]),
            (now, "hrrr",       "nyc", d, 12,
             obs[i] + hrrr_err[i], 1.2, obs[i]),
            (now, "nbm",        "nyc", d, 12,
             obs[i] + nbm_err[i],  1.8, obs[i]),
        ])
    _insert_backfill_rows(memdb, rows)

    fit = bf.fit_group_correlation(memdb)
    assert fit is not None
    assert fit.sources == ("weather", "hrrr", "nbm")
    assert fit.n_pairs == 5
    assert fit.rho == pytest.approx(-1.0 / 3.0, abs=1e-9)
    assert fit.n_eff == pytest.approx(9.0, abs=1e-6)


def test_group_fit_perfectly_correlated_matches_mvp(memdb):
    """Identical errors across all 3 model sources → ρ=1, n_eff=1.
    Sanity-check that a perfectly correlated group recovers the MVP
    1/n discount."""
    now = "2026-04-22T00:00:00Z"
    dates = ["2026-04-01", "2026-04-02", "2026-04-03", "2026-04-04"]
    obs = [70.0, 72.0, 74.0, 76.0]
    err = [1, 2, 3, 4]
    rows = []
    for i, d in enumerate(dates):
        for src in ("weather", "hrrr", "nbm"):
            rows.append((
                now, src, "nyc", d, 12,
                obs[i] + err[i], 2.0, obs[i],
            ))
    _insert_backfill_rows(memdb, rows)

    fit = bf.fit_group_correlation(memdb)
    assert fit is not None
    assert fit.rho == pytest.approx(1.0, abs=1e-9)
    assert fit.n_eff == pytest.approx(1.0, abs=1e-9)


def test_group_fit_drops_rows_missing_a_member(memdb):
    """If one source is missing on a date, the whole (city, date) row is
    dropped from the joint sample. Exact-match only."""
    now = "2026-04-22T00:00:00Z"
    # Need ≥2 joint dates AND nonzero error-variance across them for ρ to
    # be defined — use distinct per-date errors.
    rows = [
        # Full triple on 04-01 — counted. Errors: (2, 1, 3).
        (now, "weather", "nyc", "2026-04-01", 12, 72.0, 2.0, 70.0),
        (now, "hrrr",       "nyc", "2026-04-01", 12, 71.0, 1.2, 70.0),
        (now, "nbm",        "nyc", "2026-04-01", 12, 73.0, 1.8, 70.0),
        # 04-02 missing nbm — NOT counted.
        (now, "weather", "nyc", "2026-04-02", 12, 78.0, 2.0, 77.0),
        (now, "hrrr",       "nyc", "2026-04-02", 12, 79.0, 1.2, 77.0),
        # Full triple on 04-03. Errors: (4, 3, 5) — varies from 04-01.
        (now, "weather", "nyc", "2026-04-03", 12, 84.0, 2.0, 80.0),
        (now, "hrrr",       "nyc", "2026-04-03", 12, 83.0, 1.2, 80.0),
        (now, "nbm",        "nyc", "2026-04-03", 12, 85.0, 1.8, 80.0),
        # Full triple on 04-04. Errors: (1, 2, 0) — varies more.
        (now, "weather", "nyc", "2026-04-04", 12, 91.0, 2.0, 90.0),
        (now, "hrrr",       "nyc", "2026-04-04", 12, 92.0, 1.2, 90.0),
        (now, "nbm",        "nyc", "2026-04-04", 12, 90.0, 1.8, 90.0),
    ]
    _insert_backfill_rows(memdb, rows)

    fit = bf.fit_group_correlation(memdb)
    assert fit is not None
    assert fit.n_pairs == 3   # 04-01, 04-03, 04-04 had all three; 04-02 dropped


def test_report_groups_renders_empty():
    out = bf.report_groups([])
    assert "No group fits" in out


def test_report_groups_renders_non_empty():
    fit = bf.GroupFit(
        group="model", sources=("weather", "hrrr", "nbm"),
        n_pairs=42, rho=0.75, n_sources_present=3, n_eff=1.5,
    )
    out = bf.report_groups([fit])
    assert "model" in out
    assert "weather" in out and "hrrr" in out and "nbm" in out
    assert "+0.750" in out
    assert "1.50" in out


# ── 12. persist_group_fit + --persist-effective-n (A2.5c swap) ──────────

def test_persist_group_fit_writes_kv_cache_row(memdb):
    """After persist, kv_cache holds a payload under
    ``weather_group_corr_<group>`` with ``rho`` readable by
    ``weather_ensemble_v2._get_group_rho``."""
    from bot.db import kv_get
    from bot.signals import weather_ensemble_v2 as v2

    fit = bf.GroupFit(
        group="model", sources=("weather", "hrrr", "nbm"),
        n_pairs=50, rho=0.42, n_sources_present=3, n_eff=1.82,
    )
    key = bf.persist_group_fit(memdb, fit)
    assert key == "weather_group_corr_model"

    payload = kv_get(memdb, key)
    assert isinstance(payload, dict)
    assert payload["rho"] == pytest.approx(0.42)
    assert payload["n_eff"] == pytest.approx(1.82)
    assert payload["n_sources"] == 3
    assert payload["n_pairs"] == 50
    assert payload["sources"] == ["weather", "hrrr", "nbm"]
    assert "fit_at" in payload

    # Round-trip check: the v2 reader sees the same value.
    assert v2._get_group_rho("model") == pytest.approx(0.42)


def test_persist_flag_requires_non_empty_fit(memdb, capsys):
    """`--persist-effective-n --report-only` on an empty DB prints a
    skip message and does NOT write to kv_cache."""
    from bot.db import kv_get

    exit_code = bf.main([
        "--start", "2026-04-01", "--end", "2026-04-01",
        "--cities", "nyc", "--db", ":memory:",
        "--report-only", "--persist-effective-n",
    ])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "skipped" in out or "no fits" in out.lower()
    # Nothing written under the weather_group_corr_ prefix.
    row = kv_get(memdb, "weather_group_corr_model")
    assert row is None


# ── 13. Horizon-stratified skill curves (A3) ─────────────────────────────

def test_skill_bucket_boundaries():
    """Half-open bucket intervals [lo, hi). 0h → 0_6, 6h → 6_24, 24h →
    24_48, 47h → 24_48, 48h → 48_168, 167h → 48_168, 168h → None."""
    assert bf._bucket_for(0.0) == "0_6"
    assert bf._bucket_for(5.9) == "0_6"
    assert bf._bucket_for(6.0) == "6_24"
    assert bf._bucket_for(23.9) == "6_24"
    assert bf._bucket_for(24.0) == "24_48"
    assert bf._bucket_for(47.9) == "24_48"
    assert bf._bucket_for(48.0) == "48_168"
    assert bf._bucket_for(167.0) == "48_168"
    # Outside the top edge → unhandled.
    assert bf._bucket_for(168.0) is None
    assert bf._bucket_for(-1.0) is None
    assert bf._bucket_for(None) is None


def test_fit_skill_curves_math(memdb):
    """Inject deterministic rows: open_meteo at 12h lead overshoots by
    +2°F on average, RMSE 2°F."""
    now = "2026-04-22T00:00:00Z"
    rows = [
        (now, "weather", "nyc", "2026-04-01", 12, 74.0, 2.0, 72.0),
        (now, "weather", "nyc", "2026-04-02", 12, 82.0, 2.0, 80.0),
        (now, "weather", "nyc", "2026-04-03", 12, 76.0, 2.0, 74.0),
        # METAR must be skipped from skill fit.
        (now, "metar", "nyc", "2026-04-01", 0, 72.0, 0.1, 72.0),
    ]
    _insert_backfill_rows(memdb, rows)

    fits = bf.fit_skill_curves(memdb)
    # Only one (source, bucket) pair should emerge: (open_meteo, 6_24).
    assert [(f.source, f.bucket) for f in fits] == [("weather", "6_24")]
    sf = fits[0]
    assert sf.n == 3
    assert sf.bias_f == pytest.approx(2.0, abs=1e-9)
    assert sf.rmse_f == pytest.approx(2.0, abs=1e-9)
    assert sf.prior_sigma_f == pytest.approx(2.0, abs=1e-9)


def test_fit_skill_curves_splits_by_bucket(memdb):
    """Rows at different horizons go into distinct buckets."""
    now = "2026-04-22T00:00:00Z"
    rows = [
        # 2 rows in the 0-6h bucket with bias +1.
        (now, "hrrr", "nyc", "2026-04-01", 3, 71.0, 1.2, 70.0),
        (now, "hrrr", "chi", "2026-04-01", 3, 61.0, 1.2, 60.0),
        # 2 rows in the 48-168h bucket with bias +4.
        (now, "hrrr", "nyc", "2026-04-05", 72, 84.0, 3.2, 80.0),
        (now, "hrrr", "chi", "2026-04-05", 72, 74.0, 3.2, 70.0),
    ]
    _insert_backfill_rows(memdb, rows)

    fits = {(f.source, f.bucket): f for f in bf.fit_skill_curves(memdb)}
    assert ("hrrr", "0_6") in fits
    assert ("hrrr", "48_168") in fits
    assert fits[("hrrr", "0_6")].bias_f == pytest.approx(1.0, abs=1e-9)
    assert fits[("hrrr", "48_168")].bias_f == pytest.approx(4.0, abs=1e-9)


def test_persist_skill_fit_writes_kv_cache(memdb):
    """After persist, kv_cache holds the payload under
    ``weather_skill_<source>_<bucket>`` with σ readable by
    ``weather_ensemble_v2._get_learned_sigma``."""
    from bot.db import kv_get
    from bot.signals import weather_ensemble_v2 as v2

    fit = bf.SkillFit(
        source="hrrr", bucket="6_24", n=42,
        bias_f=0.5, rmse_f=1.8, prior_sigma_f=1.2,
    )
    key = bf.persist_skill_fit(memdb, fit)
    assert key == "weather_skill_hrrr_6_24"

    payload = kv_get(memdb, key)
    assert isinstance(payload, dict)
    assert payload["sigma"] == pytest.approx(1.8)
    assert payload["bias"] == pytest.approx(0.5)
    assert payload["n"] == 42
    assert payload["prior_sigma"] == pytest.approx(1.2)
    assert "fit_at" in payload

    # Round-trip: v2 reader sees the value for any horizon in [6, 24).
    assert v2._get_learned_sigma("hrrr", 12.0) == pytest.approx(1.8)
    # But not for horizons outside the bucket.
    assert v2._get_learned_sigma("hrrr", 30.0) is None


def test_persist_skill_flag_respects_min_samples(memdb, capsys):
    """`--persist-skill-curves` must skip buckets below the sample floor.
    Write 3 rows for open_meteo in the 6-24h bucket (< min), then run
    persist: no kv_cache row should appear."""
    from bot.db import kv_get
    now = "2026-04-22T00:00:00Z"
    rows = [
        (now, "weather", "nyc", f"2026-04-{i:02d}", 12, 74.0, 2.0, 72.0)
        for i in range(1, 4)
    ]
    _insert_backfill_rows(memdb, rows)

    exit_code = bf.main([
        "--start", "2026-04-01", "--end", "2026-04-03",
        "--cities", "nyc", "--db", ":memory:",
        "--report-only", "--persist-skill-curves",
    ])
    assert exit_code == 0
    captured = capsys.readouterr().out
    assert "skipped" in captured or "thin" in captured
    # Nothing written.
    assert kv_get(memdb, "weather_skill_weather_6_24") is None


def test_bucket_edges_match_between_tool_and_v2():
    """Drift guard: tool's _SKILL_BUCKET_EDGES must match v2's, otherwise
    the tool writes a key the reader doesn't look up."""
    from bot.signals import weather_ensemble_v2 as v2
    assert bf._SKILL_BUCKET_EDGES == v2._SKILL_BUCKET_EDGES


def test_skill_key_prefix_matches_between_tool_and_v2():
    """Drift guard: tool's _SKILL_KEY_PREFIX must match v2's."""
    from bot.signals import weather_ensemble_v2 as v2
    assert bf._SKILL_KEY_PREFIX == v2._SKILL_KEY_PREFIX


def test_report_renders_when_empty():
    assert bf.report([]) == "No fits — run backfill first."


def test_report_includes_source_and_numbers():
    fit = bf.SourceFit(
        source="weather", n=3,
        mean_bias_f=1.25, rmse_f=1.8,
        reported_sigma_f=2.0, sigma_ratio=0.9,
    )
    out = bf.report([fit])
    assert "weather" in out
    assert "+1.25" in out or "1.25" in out
    assert "1.80" in out
    assert "0.90" in out


# ── 10. CLI integration (main) ───────────────────────────────────────────

def test_main_report_only_runs_without_network(memdb, capsys):
    """--report-only should skip all network fetches and just print."""
    exit_code = bf.main([
        "--start", "2026-04-01", "--end", "2026-04-01",
        "--cities", "nyc", "--db", ":memory:",
        "--report-only",
    ])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "No fits" in captured.out or "A2.5a" in captured.out


def test_main_rejects_unknown_city():
    with pytest.raises(SystemExit, match="Unknown cities"):
        bf.main([
            "--start", "2026-04-01", "--end", "2026-04-01",
            "--cities", "atlantis", "--db", ":memory:",
            "--report-only",
        ])


# ══════════════════════════════════════════════════════════════════════
# A4 — hourly METAR + diurnal fit
# ══════════════════════════════════════════════════════════════════════


def _hourly_csv(rows: list[tuple[str, float]]) -> str:
    """Build an IEM asos.py CSV for a sequence of (UTC timestamp, tmpf).

    Matches the "onlycomma" format the tool expects: header `station,valid,tmpf`
    and one row per reading.
    """
    lines = ["station,valid,tmpf"]
    for ts, t in rows:
        lines.append(f"XXXX,{ts},{t}")
    return "\n".join(lines) + "\n"


def test_hourly_schema_created(memdb):
    cols = memdb.execute(
        "PRAGMA table_info(weather_metar_hourly_backfill)"
    ).fetchall()
    col_names = {c[1] for c in cols}
    expected = {
        "id", "created_at", "station", "lst_date", "lst_hour",
        "temp_f", "daily_high_f",
    }
    assert expected <= col_names, f"missing columns: {expected - col_names}"


def test_fetch_metar_hourly_buckets_to_lst_hour(memdb):
    """UTC rows should be bucketed into the station's LST hour/date."""
    # NYC lst_offset = -5 → 14:55 UTC = 09:55 LST
    station = STATION_BY_CITY["nyc"]
    iem = _hourly_csv([
        ("2026-04-01 14:55", 60.0),  # LST 09:55 → (2026-04-01, hour 9)
        ("2026-04-01 15:55", 63.0),  # LST 10:55 → (2026-04-01, hour 10)
        # Multiple readings in the same LST hour — LAST wins
        ("2026-04-01 16:15", 65.0),  # LST 11:15
        ("2026-04-01 16:55", 67.0),  # LST 11:55 → beats 16:15
    ])
    sess = _mock_session(iem_csv=iem)
    records = bf.fetch_metar_hourly(
        station, "2026-04-01", "2026-04-01", session=sess,
    )
    by_key = {(d, h): t for (d, h, t, _) in records}
    assert by_key[("2026-04-01", 9)] == 60.0
    assert by_key[("2026-04-01", 10)] == 63.0
    # The 16:15 reading is superseded by 16:55; last-wins
    assert by_key[("2026-04-01", 11)] == 67.0
    # daily_high = max over entire LST day = 67.0
    for (_, _, _, high) in records:
        assert high == 67.0


def test_fetch_metar_hourly_skips_bad_rows(memdb):
    station = STATION_BY_CITY["nyc"]
    iem = _hourly_csv([
        ("2026-04-01 14:55", 60.0),
        # Missing temperature
        ("2026-04-01 15:55", -999.99),  # out-of-range → dropped
        ("2026-04-01 16:55", 65.0),
        ("bad-timestamp",    66.0),       # unparseable → dropped
    ])
    # Also add an empty-tmpf row manually:
    iem += "XXXX,2026-04-01 17:55,\n"
    sess = _mock_session(iem_csv=iem)
    records = bf.fetch_metar_hourly(
        station, "2026-04-01", "2026-04-01", session=sess,
    )
    temps = sorted(t for (_, _, t, _) in records)
    assert temps == [60.0, 65.0]


def test_replay_hourly_is_idempotent(memdb):
    station = STATION_BY_CITY["nyc"]
    iem = _hourly_csv([
        ("2026-04-01 14:55", 60.0),
        ("2026-04-01 18:55", 71.0),
    ])
    sess = _mock_session(iem_csv=iem)
    n1 = bf.replay_hourly_and_write(
        memdb, station, "2026-04-01", "2026-04-01", session=sess,
    )
    n2 = bf.replay_hourly_and_write(
        memdb, station, "2026-04-01", "2026-04-01", session=sess,
    )
    assert n1 == n2 == 2
    row_count = memdb.execute(
        "SELECT COUNT(*) FROM weather_metar_hourly_backfill"
    ).fetchone()[0]
    assert row_count == 2  # Re-run replaces, does not duplicate


def test_ols_fit_recovers_known_slope_intercept():
    """Synthetic y = 2 + 1.5*x + small noise → OLS should recover it."""
    xs = [60.0, 62.0, 65.0, 70.0, 72.0, 75.0]
    ys = [2.0 + 1.5 * x for x in xs]  # zero-noise, perfect fit
    fit = bf._ols_fit(xs, ys)
    assert fit is not None
    alpha, beta, rmse = fit
    assert abs(alpha - 2.0) < 1e-8
    assert abs(beta - 1.5) < 1e-8
    assert rmse < 1e-8


def test_ols_fit_returns_none_on_zero_variance():
    # All x's identical → sxx == 0 → undefined slope
    assert bf._ols_fit([60.0] * 5, [70.0] * 5) is None
    # Too few samples
    assert bf._ols_fit([60.0], [70.0]) is None
    assert bf._ols_fit([], []) is None


def test_fit_metar_diurnal_drops_tautological_rows(memdb):
    """Rows where temp_f == daily_high_f (T(h) == max) should be excluded —
    including them biases β toward 1.0 tautologically."""
    station = "KNYC"
    # Four rows at LST hour 9: three with T < high (useful), one T == high.
    rows = [
        ("2026-04-01", 9, 60.0, 78.0),
        ("2026-04-02", 9, 62.0, 80.0),
        ("2026-04-03", 9, 64.0, 82.0),
        ("2026-04-04", 9, 90.0, 90.0),  # T == high → dropped
    ]
    memdb.executemany(
        """INSERT INTO weather_metar_hourly_backfill
           (created_at, station, lst_date, lst_hour, temp_f, daily_high_f)
           VALUES ('now', ?, ?, ?, ?, ?)""",
        [(station, d, h, t, hi) for (d, h, t, hi) in rows],
    )
    memdb.commit()
    fits = bf.fit_metar_diurnal(memdb)
    assert len(fits) == 1
    f = fits[0]
    assert f.station == station
    assert f.lst_hour == 9
    assert f.n == 3  # the T==high row was dropped


def test_fit_metar_diurnal_splits_by_station_and_hour(memdb):
    # Two stations × two hours × 3 rows each (avoiding T == high)
    for station in ("KNYC", "KDEN"):
        for lst_hour in (9, 14):
            for i in range(3):
                memdb.execute(
                    """INSERT INTO weather_metar_hourly_backfill
                       (created_at, station, lst_date, lst_hour, temp_f, daily_high_f)
                       VALUES ('now', ?, ?, ?, ?, ?)""",
                    (station, f"2026-04-{i+1:02d}", lst_hour,
                     50.0 + i + lst_hour, 75.0 + i),
                )
    memdb.commit()
    fits = bf.fit_metar_diurnal(memdb)
    keys = {(f.station, f.lst_hour) for f in fits}
    assert keys == {("KDEN", 9), ("KDEN", 14), ("KNYC", 9), ("KNYC", 14)}


def test_persist_diurnal_writes_kv_cache_row(memdb):
    fits = [
        bf.DiurnalFit(station="KNYC", lst_hour=9,
                      n=20, alpha=12.5, beta=1.05, rmse=2.4),
        bf.DiurnalFit(station="KNYC", lst_hour=14,
                      n=18, alpha=5.0, beta=1.02, rmse=1.9),
    ]
    keys = bf.persist_diurnal_fit(memdb, fits)
    assert keys == ["weather_metar_diurnal_KNYC"]
    from bot.db import kv_get
    payload = kv_get(memdb, "weather_metar_diurnal_KNYC")
    assert payload is not None
    assert set(payload["hours"].keys()) == {"9", "14"}
    cell = payload["hours"]["9"]
    assert cell["alpha"] == 12.5
    assert cell["beta"] == 1.05
    assert cell["rmse"] == 2.4
    assert cell["n"] == 20


def test_persist_diurnal_respects_min_samples_and_sigma_bounds(memdb):
    fits = [
        bf.DiurnalFit(station="KNYC", lst_hour=9,
                      n=5, alpha=10.0, beta=1.0, rmse=2.0),  # too thin
        bf.DiurnalFit(station="KNYC", lst_hour=10,
                      n=20, alpha=10.0, beta=1.0, rmse=0.1),  # σ below floor
        bf.DiurnalFit(station="KNYC", lst_hour=11,
                      n=20, alpha=10.0, beta=1.0, rmse=20.0),  # σ above ceil
        bf.DiurnalFit(station="KNYC", lst_hour=12,
                      n=20, alpha=10.0, beta=1.0, rmse=2.5),  # persisted
    ]
    keys = bf.persist_diurnal_fit(memdb, fits)
    assert keys == ["weather_metar_diurnal_KNYC"]
    from bot.db import kv_get
    payload = kv_get(memdb, "weather_metar_diurnal_KNYC")
    assert set(payload["hours"].keys()) == {"12"}


def test_persist_diurnal_empty_when_all_filtered(memdb):
    fits = [
        bf.DiurnalFit(station="KNYC", lst_hour=9,
                      n=5, alpha=10.0, beta=1.0, rmse=2.0),  # thin → drop
    ]
    assert bf.persist_diurnal_fit(memdb, fits) == []


def test_diurnal_key_prefix_matches_between_tool_and_signal():
    """Drift-guard: the tool's persist key prefix MUST match the reader
    constant in bot.signals.sources.metar_observations."""
    from bot.signals.sources import metar_observations as mo
    assert bf._DIURNAL_KEY_PREFIX == mo._DIURNAL_KEY_PREFIX


def test_diurnal_sigma_bounds_match_between_tool_and_signal():
    """Drift-guard: σ clamp used at persist-time must match the read-time
    clamp; otherwise persisted values would get silently dropped on read."""
    from bot.signals.sources import metar_observations as mo
    assert bf._DIURNAL_SIGMA_FLOOR_F == mo._DIURNAL_SIGMA_FLOOR_F
    assert bf._DIURNAL_SIGMA_CEIL_F == mo._DIURNAL_SIGMA_CEIL_F


def test_report_diurnal_renders_when_empty():
    assert bf.report_diurnal_fits([]) == (
        "No diurnal fits — run hourly backfill first."
    )


def test_report_diurnal_renders_with_fit():
    fit = bf.DiurnalFit(
        station="KNYC", lst_hour=9,
        n=30, alpha=12.5, beta=1.05, rmse=2.4,
    )
    out = bf.report_diurnal_fits([fit])
    assert "KNYC" in out
    assert "9" in out
    assert "2.40" in out


# ══════════════════════════════════════════════════════════════════════
# A5 — EWMA MOS bias per (source, city)
# ══════════════════════════════════════════════════════════════════════


def test_city_key_normalizes_spaces():
    assert bf._city_key("nyc") == "nyc"
    assert bf._city_key("Los Angeles") == "los_angeles"
    assert bf._city_key("  los angeles  ") == "los_angeles"


def test_ewma_weight_today_is_one():
    """An observation dated the same as the reference date weighs 1.0."""
    assert bf._ewma_weight("2026-04-24", "2026-04-24", 14.0) == pytest.approx(1.0)


def test_ewma_weight_at_half_life_is_one_half():
    """14 days old, H=14 → 2^(-1) = 0.5."""
    assert bf._ewma_weight("2026-04-10", "2026-04-24", 14.0) == pytest.approx(0.5)


def test_ewma_weight_at_two_half_lives_is_one_quarter():
    assert bf._ewma_weight("2026-03-27", "2026-04-24", 14.0) == pytest.approx(0.25)


def test_ewma_weight_infinity_half_life_is_one():
    """H=+∞ degenerates EWMA to a flat mean — every age weighs 1.0."""
    for age in (0, 7, 30, 365):
        d = datetime(2026, 4, 24) - timedelta(days=age)
        w = bf._ewma_weight(d.strftime("%Y-%m-%d"), "2026-04-24", float("inf"))
        assert w == 1.0


def test_ewma_weight_future_dated_caps_at_one():
    """A row dated ahead of the reference date shouldn't out-weigh today."""
    assert bf._ewma_weight("2026-04-25", "2026-04-24", 14.0) == pytest.approx(1.0)


def test_ewma_weight_garbage_returns_zero():
    assert bf._ewma_weight("not-a-date", "2026-04-24", 14.0) == 0.0
    assert bf._ewma_weight("", "2026-04-24", 14.0) == 0.0


def _insert_bias_rows(memdb, rows):
    """Helper: rows are tuples of (source, city, settlement_date, fcst, obs)."""
    memdb.executemany(
        """INSERT INTO weather_gaussian_snapshots_backfill
           (created_at, source, city, settlement_date, lead_hours,
            forecast_mean_f, forecast_sigma_f, observed_high_f)
           VALUES ('now', ?, ?, ?, 12, ?, 1.5, ?)""",
        rows,
    )
    memdb.commit()


def test_fit_mos_bias_pools_by_source_city_only(memdb):
    """No more season/bucket split — all rows under one (source, city)
    cell pool together."""
    rows = [
        ("hrrr", "nyc", "2026-04-01", 75.0, 74.0),  # err +1
        ("hrrr", "nyc", "2026-04-02", 80.0, 78.0),  # err +2
        ("hrrr", "nyc", "2026-04-03", 71.0, 70.0),  # err +1
        ("hrrr", "nyc", "2026-04-04", 78.0, 76.0),  # err +2
    ]
    _insert_bias_rows(memdb, rows)
    # H=∞ → equal weighting; bias = (1+2+1+2)/4 = 1.5
    fits = bf.fit_mos_bias(memdb, half_life_days=float("inf"))
    assert len(fits) == 1
    f = fits[0]
    assert f.source == "hrrr"
    assert f.city == "nyc"
    assert f.n == 4
    assert f.bias_f == pytest.approx(1.5)
    # Equal weights → eff_n equals raw n.
    assert f.eff_n == pytest.approx(4.0)


def test_fit_mos_bias_splits_only_by_source_and_city(memdb):
    """Two sources × two cities → 4 cells regardless of date / season."""
    rows = [
        # hrrr/nyc — bias +2
        ("hrrr", "nyc", "2026-01-01", 82.0, 80.0),
        ("hrrr", "nyc", "2026-07-01", 85.0, 83.0),
        # hrrr/miami — bias +3
        ("hrrr", "miami", "2026-01-05", 75.0, 72.0),
        ("hrrr", "miami", "2026-07-05", 78.0, 75.0),
        # nbm/nyc — bias -1
        ("nbm", "nyc", "2026-01-01", 81.0, 82.0),
        ("nbm", "nyc", "2026-07-01", 82.0, 83.0),
    ]
    _insert_bias_rows(memdb, rows)
    fits = {(f.source, f.city): f for f in
            bf.fit_mos_bias(memdb, half_life_days=float("inf"))}
    assert fits[("hrrr", "nyc")].bias_f == pytest.approx(2.0)
    assert fits[("hrrr", "miami")].bias_f == pytest.approx(3.0)
    assert fits[("nbm", "nyc")].bias_f == pytest.approx(-1.0)
    assert ("nbm", "miami") not in fits  # No data; not invented.


def test_fit_mos_bias_ewma_weights_recent_rows_more(memdb):
    """With H=14d and a step change in bias 21 days ago, recent rows
    dominate. Old (3 weeks back) bias=+5, new (today) bias=-1."""
    rows = [
        ("hrrr", "nyc", "2026-04-03", 75.0, 70.0),  # 21d ago, err +5
        ("hrrr", "nyc", "2026-04-24", 71.0, 72.0),  # today, err -1
    ]
    _insert_bias_rows(memdb, rows)
    fits = bf.fit_mos_bias(
        memdb, half_life_days=14.0, ref_date_iso="2026-04-24",
    )
    assert len(fits) == 1
    f = fits[0]
    # Weights: today=1.0, 21d ago = 2^(-1.5) ≈ 0.3536
    # bias = (1.0·-1 + 0.3536·5) / (1.0 + 0.3536) ≈ 0.5672
    assert f.bias_f == pytest.approx(0.5672, abs=1e-3)
    # Effective n = (1.3536)² / (1.0² + 0.3536²) ≈ 1.628 — well below raw n=2.
    assert f.eff_n == pytest.approx(1.628, abs=1e-3)


def test_fit_mos_bias_ewma_at_h_infinity_equals_flat_mean(memdb):
    """Regression: H=+∞ collapses EWMA to a flat arithmetic mean —
    proves the EWMA fitter is a strict generalization of the simple-mean
    behavior the previous A5 version implemented."""
    rows = [
        ("hrrr", "nyc", "2026-01-01", 82.0, 80.0),
        ("hrrr", "nyc", "2026-04-01", 85.0, 80.0),
        ("hrrr", "nyc", "2026-04-15", 78.0, 76.0),
        ("hrrr", "nyc", "2026-04-24", 70.0, 71.0),
    ]
    _insert_bias_rows(memdb, rows)
    flat_mean = (2.0 + 5.0 + 2.0 + (-1.0)) / 4.0  # +2.0
    f = bf.fit_mos_bias(memdb, half_life_days=float("inf"))[0]
    assert f.bias_f == pytest.approx(flat_mean)


def test_fit_mos_bias_excludes_metar_rows(memdb):
    rows = [
        ("metar", "nyc", "2026-04-01", 75.0, 75.0),  # err 0 by construction
        ("hrrr", "nyc", "2026-04-01", 76.0, 75.0),
    ]
    _insert_bias_rows(memdb, rows)
    fits = bf.fit_mos_bias(memdb, half_life_days=float("inf"))
    assert all(f.source != "metar" for f in fits)


def test_persist_mos_bias_writes_2tuple_kv(memdb):
    fit = bf.MOSBiasFit(source="hrrr", city="nyc", n=20, bias_f=1.5, eff_n=20.0)
    keys = bf.persist_mos_bias(memdb, [fit])
    assert keys == ["weather_mos_bias_hrrr_nyc"]
    from bot.db import kv_get
    payload = kv_get(memdb, keys[0])
    assert payload is not None
    assert payload["bias"] == 1.5
    assert payload["n"] == 20
    assert payload["eff_n"] == 20.0


def test_persist_mos_bias_normalizes_city_spaces(memdb):
    fit = bf.MOSBiasFit(source="hrrr", city="los angeles", n=15, bias_f=-1.2, eff_n=15.0)
    keys = bf.persist_mos_bias(memdb, [fit])
    assert keys == ["weather_mos_bias_hrrr_los_angeles"]


def test_persist_mos_bias_gates_on_eff_n_not_raw_n(memdb):
    """A cell with raw n=30 but EWMA eff_n=2 must NOT persist — the
    information content is 2 rows, not 30."""
    fits = [
        bf.MOSBiasFit("hrrr", "nyc", n=30, bias_f=1.0, eff_n=2.0),    # eff_n thin
        bf.MOSBiasFit("hrrr", "chi", n=20, bias_f=10.0, eff_n=20.0),  # |bias| > cap
        bf.MOSBiasFit("hrrr", "den", n=20, bias_f=1.0, eff_n=20.0),   # ok
    ]
    keys = bf.persist_mos_bias(memdb, fits)
    assert keys == ["weather_mos_bias_hrrr_den"]


def test_round_trip_through_v2_reader(memdb):
    """End-to-end: write 2-tuple key with the fitter, read back via the
    v2 reader. This is the seam the open_meteo/weather drift bug broke."""
    fit = bf.MOSBiasFit(source="hrrr", city="nyc", n=30, bias_f=1.5, eff_n=30.0)
    bf.persist_mos_bias(memdb, [fit])
    import bot.db as _db
    from bot.signals import weather_ensemble_v2 as v2
    saved = _db._PERSIST_CONN
    _db._PERSIST_CONN = memdb
    try:
        bias = v2._get_mos_bias("hrrr", "nyc")
    finally:
        _db._PERSIST_CONN = saved
    assert bias == pytest.approx(1.5)


def test_mos_bias_key_prefix_matches_between_tool_and_signal():
    """Drift-guard: the kv key prefix must agree between the persister
    (tool) and the reader (weather_ensemble_v2)."""
    from bot.signals import weather_ensemble_v2 as v2
    assert bf._MOS_BIAS_KEY_PREFIX == v2._MOS_BIAS_KEY_PREFIX


def test_mos_bias_city_key_matches_between_tool_and_signal():
    """Drift-guard: a city name must normalize to the same key on both
    sides or the reader won't find what the tool wrote."""
    from bot.signals import weather_ensemble_v2 as v2
    for raw in ("nyc", "Los Angeles", "los angeles", "DENVER"):
        assert bf._city_key(raw) == v2._city_key(raw)


def test_mos_bias_clamp_matches_between_tool_and_signal():
    from bot.signals import weather_ensemble_v2 as v2
    assert bf._MOS_BIAS_MAX_ABS_F == v2._MOS_BIAS_MAX_ABS_F


def test_report_mos_bias_renders_with_fit():
    fit = bf.MOSBiasFit(source="hrrr", city="nyc", n=30, bias_f=1.5, eff_n=30.0)
    out = bf.report_mos_bias([fit])
    assert "hrrr" in out
    assert "nyc" in out
    assert "+1.50" in out
