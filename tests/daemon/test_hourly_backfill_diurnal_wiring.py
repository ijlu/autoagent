"""Pin: the daemon's hourly_backfill task MUST call fit_metar_diurnal +
persist_diurnal_fit after CF6 stage 2.

Pre-2026-05-02 the diurnal fitter was only invoked from the CLI tool's
``--persist-diurnal`` flag. Nothing in the daemon called it, so the
``weather_metar_diurnal_*`` kv_cache keys were never written on prod —
and ``metar_observations.get_metar_gaussian`` (plus the new
``nws_5min_diurnal`` source) silently fell back to None on every cycle.

If anyone removes this wiring or moves it before CF6 stage 2, the
fitter trains on raw tmpf max instead of CF6-corrected daily_high_f
(systematically 1-3°F cold) — which is the same regression that
contributed the largest weather Brier gap in early April.
"""
from __future__ import annotations

import inspect
import re

from bot.daemon import main as daemon_main


def test_hourly_backfill_imports_diurnal_fitter():
    """Import sanity — both fit + persist must be pulled into the
    function's local namespace."""
    src = inspect.getsource(daemon_main._run_hourly_backfill)
    assert "fit_metar_diurnal" in src, (
        "fit_metar_diurnal not imported into _run_hourly_backfill. "
        "Diurnal fits will never be written to kv_cache, which means "
        "metar_observations and nws_5min_diurnal silently return None."
    )
    assert "persist_diurnal_fit" in src, (
        "persist_diurnal_fit not imported into _run_hourly_backfill."
    )


def test_diurnal_fit_runs_after_cf6():
    """Strict ordering: CF6 must overwrite daily_high_f BEFORE the
    diurnal fitter reads from weather_metar_hourly_backfill, otherwise
    the regression trains on tmpf max (1-3°F cold relative to
    settlement) instead of the CF6 TMAX Kalshi actually settles on."""
    src = inspect.getsource(daemon_main._run_hourly_backfill)
    cf6_pos = src.find("update_daily_high_from_cf6")
    fit_pos = src.find("fit_metar_diurnal(")
    persist_pos = src.find("persist_diurnal_fit(")

    assert cf6_pos != -1, "CF6 stage missing from hourly_backfill"
    assert fit_pos != -1, "fit_metar_diurnal call missing"
    assert persist_pos != -1, "persist_diurnal_fit call missing"
    assert cf6_pos < fit_pos < persist_pos, (
        f"Order broken: cf6@{cf6_pos} fit@{fit_pos} persist@{persist_pos}. "
        f"Fitter must read CF6-corrected daily_high_f."
    )


def test_diurnal_stage_logs_outcome():
    """Operational visibility — the stage must log either how many
    fits/keys it wrote or, on exception, a warning. Silent failure
    is exactly how this stage went undetected for the entire window
    nws_5min_diurnal was live."""
    src = inspect.getsource(daemon_main._run_hourly_backfill)
    # The diurnal block must contain at least one logger call
    diurnal_block = src[src.find("fit_metar_diurnal("):]
    assert re.search(r"logger\.(info|warning)", diurnal_block), (
        "Diurnal stage must emit a logger.info/warning so we can see "
        "fits being persisted (and notice when they aren't)."
    )


def test_diurnal_stage_wrapped_in_try_except():
    """A diurnal-fit error must NOT take down the whole hourly_backfill
    task. The mos_materializer follows the opposite convention (loud
    failure) but that's a separate task; this one is shared with CF6
    + regime fetches that we want to keep running independently."""
    src = inspect.getsource(daemon_main._run_hourly_backfill)
    # Look for try-block whose body mentions fit_metar_diurnal
    # Simple heuristic: there must be `try:` before fit_metar_diurnal
    # and `except` after, both within the diurnal stage section.
    diurnal_section = src[src.find("Stage 3"):]
    assert "try:" in diurnal_section, (
        "Diurnal stage must be wrapped in try/except — a fit failure "
        "shouldn't take down the whole backfill task."
    )
    assert "except" in diurnal_section, (
        "Missing except clause in diurnal stage."
    )
