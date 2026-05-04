"""Tests for `bot.signals.sources._openmeteo` URL routing.

Pins the behavior that:
  * No `OPENMETEO_API_KEY` env → free endpoint, no apikey query param
  * Key set → commercial endpoint with apikey appended
  * Idempotency-ish: with_apikey called on a URL that already has
    a query string adds `&apikey=...`; on bare URL adds `?apikey=...`
"""
from __future__ import annotations

import importlib

import pytest


def _reload_helper(monkeypatch, key: str | None):
    """Helper to reload the module with a specific OPENMETEO_API_KEY env."""
    if key is None:
        monkeypatch.delenv("OPENMETEO_API_KEY", raising=False)
    else:
        monkeypatch.setenv("OPENMETEO_API_KEY", key)
    import bot.config
    importlib.reload(bot.config)
    import bot.signals.sources._openmeteo as om
    importlib.reload(om)
    return om


def test_free_tier_when_no_key(monkeypatch):
    """Default: no env → free api.open-meteo.com host, no apikey param."""
    om = _reload_helper(monkeypatch, None)
    assert om.is_commercial() is False
    assert om.base_url() == "https://api.open-meteo.com"
    url = om.forecast_url("latitude=25.79&longitude=-80.29")
    assert url == "https://api.open-meteo.com/v1/forecast?latitude=25.79&longitude=-80.29"
    assert "apikey=" not in url


def test_commercial_tier_when_key_set(monkeypatch):
    """With env: commercial host + apikey query param."""
    om = _reload_helper(monkeypatch, "abc123")
    assert om.is_commercial() is True
    assert om.base_url() == "https://customer-api.open-meteo.com"
    url = om.forecast_url("latitude=25.79&longitude=-80.29")
    assert url.startswith("https://customer-api.open-meteo.com/v1/forecast?")
    assert url.endswith("&apikey=abc123")


def test_with_apikey_on_url_with_existing_query(monkeypatch):
    """If URL already has a query string, apikey is appended with `&`."""
    om = _reload_helper(monkeypatch, "key123")
    url = om.with_apikey("https://example.com/path?foo=bar")
    assert url == "https://example.com/path?foo=bar&apikey=key123"


def test_with_apikey_on_url_without_query(monkeypatch):
    """If URL has no query string, apikey is appended with `?`."""
    om = _reload_helper(monkeypatch, "key123")
    url = om.with_apikey("https://example.com/path")
    assert url == "https://example.com/path?apikey=key123"


def test_with_apikey_no_op_when_no_key(monkeypatch):
    """No env → URL unchanged (no apikey appended)."""
    om = _reload_helper(monkeypatch, None)
    url = om.with_apikey("https://example.com/path?foo=bar")
    assert url == "https://example.com/path?foo=bar"


def test_empty_string_treated_as_no_key(monkeypatch):
    """OPENMETEO_API_KEY="" should be treated identically to unset.

    Important because deploy scripts often write empty values for
    optional keys, and we shouldn't accidentally route empty-key
    traffic to the commercial endpoint (which would 401)."""
    om = _reload_helper(monkeypatch, "")
    assert om.is_commercial() is False
    assert om.base_url() == "https://api.open-meteo.com"
    url = om.forecast_url("latitude=0&longitude=0")
    assert "apikey=" not in url


def test_callers_use_helper_not_hardcoded_host():
    """Regression guard: the eight Open-Meteo callers must not
    hard-code ``api.open-meteo.com`` directly.

    If they do, flipping OPENMETEO_API_KEY won't take effect for
    that source — silently leaving us on the throttled free tier
    while paying for commercial. This test scans the source files
    and asserts none of them contain the hardcoded host outside
    a comment context.
    """
    import re
    from pathlib import Path
    repo_root = Path(__file__).resolve().parent.parent.parent
    sources_to_check = [
        repo_root / "bot" / "signals" / "sources" / "hrrr.py",
        repo_root / "bot" / "signals" / "sources" / "icon.py",
        repo_root / "bot" / "signals" / "sources" / "ukmo.py",
        repo_root / "bot" / "signals" / "sources" / "ndfd_nbm.py",
        repo_root / "bot" / "signals" / "sources" / "gem.py",
        repo_root / "bot" / "signals" / "sources" / "metno.py",
        repo_root / "bot" / "signals" / "sources" / "ecmwf.py",
        repo_root / "bot" / "signals" / "sources" / "weather.py",
        repo_root / "bot" / "daemon" / "forecast_cache.py",
    ]
    for path in sources_to_check:
        text = path.read_text()
        # Strip out comment lines so legitimate explanatory mentions
        # of the host don't trigger the regression. Match lines
        # whose effective code part has a quoted hardcoded URL.
        for line_no, line in enumerate(text.splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            # Look for a quoted literal containing the host. Catches
            # both f-strings and plain strings.
            if re.search(r'["\']https?://api\.open-meteo\.com', line):
                pytest.fail(
                    f"{path.name}:{line_no} still hard-codes "
                    f"api.open-meteo.com. Use _openmeteo.forecast_url() "
                    f"so OPENMETEO_API_KEY routes to the commercial "
                    f"endpoint when set.\n  line: {line.strip()}"
                )
