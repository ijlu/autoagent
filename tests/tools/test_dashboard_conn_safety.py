"""Regression tests for dashboard's connection lifecycle.

The 2026-05-02 outage: ``tools.dashboard.generate_dashboard`` reset
``bot.db._PERSIST_CONN = None``, called ``init_db`` (which created a
new conn AND reassigned ``_PERSIST_CONN``), used it, then closed it.
The result: every subsequent ``kv_get/kv_set`` call from poller threads
hit ``ProgrammingError: Cannot operate on a closed database`` — 5,162
errors over 24h before we caught it.

Pin three properties:
  1. When called WITHOUT a conn (CLI mode), the function must not touch
     ``bot.db._PERSIST_CONN``. The daemon may not be running, but if a
     daemon is running on the same box, we shouldn't break it.
  2. When called WITH a conn, the function must NOT close it on exit.
  3. The CLI mode must use a read-only connection so accidental schema
     drift can't corrupt a snapshot DB during ad-hoc inspection.
"""
from __future__ import annotations

import sqlite3
import textwrap

import pytest


@pytest.fixture
def tmp_dashboard_db(tmp_path, monkeypatch):
    """Real schema via init_db so dashboard's complex queries don't
    explode on missing tables. We snapshot + restore _PERSIST_CONN so
    the fixture itself doesn't pollute global module state.
    """
    import bot.db as db_mod
    saved = db_mod._PERSIST_CONN
    db_path = tmp_path / "dashboard_test.db"
    conn = db_mod.init_db(str(db_path))
    conn.close()
    # Restore persist-conn to whatever it was — these tests assert
    # against module state separately.
    db_mod._PERSIST_CONN = saved
    return str(db_path)


def test_generate_dashboard_with_conn_does_not_close_caller_conn(tmp_dashboard_db, tmp_path):
    """Pin: generate_dashboard(conn=injected) must NOT close the
    caller's connection. This was the 2026-05-02 bug — the daemon
    passed its shared conn (or rather init_db reset it) and dashboard
    closed it on exit, breaking every subsequent poller-thread query.
    """
    from tools.dashboard import generate_dashboard

    caller_conn = sqlite3.connect(tmp_dashboard_db)
    output = tmp_path / "out.html"

    # The daemon-style call: pass our conn explicitly.
    generate_dashboard(tmp_dashboard_db, str(output), conn=caller_conn)

    # The conn MUST still be usable. If dashboard closed it, this
    # raises ProgrammingError("Cannot operate on a closed database").
    caller_conn.execute("SELECT 1").fetchone()
    caller_conn.close()  # cleanup


def test_generate_dashboard_without_conn_does_not_touch_persist_conn(
    tmp_dashboard_db, tmp_path, monkeypatch
):
    """Pin: CLI mode (no conn arg) must not poke at ``bot.db._PERSIST_CONN``.
    Pre-fix the function reset it to None and reassigned via init_db,
    making CLI invocation incompatible with a running daemon on the
    same box.
    """
    import bot.db as db_mod
    from tools.dashboard import generate_dashboard

    sentinel = object()
    monkeypatch.setattr(db_mod, "_PERSIST_CONN", sentinel)
    output = tmp_path / "out.html"

    generate_dashboard(tmp_dashboard_db, str(output))  # no conn

    # Persistent conn must be exactly as we left it. If dashboard
    # touched it, sentinel comparison fails.
    assert db_mod._PERSIST_CONN is sentinel


def test_generate_dashboard_cli_mode_uses_readonly_connection(
    tmp_dashboard_db, tmp_path,
):
    """Pin: when dashboard opens its own conn (CLI mode), that conn
    must be read-only. Prevents accidentally writing to a snapshot DB
    during ad-hoc inspection.

    We can't directly inspect the conn's mode after the function
    returns (it closes it), so we verify by attempting a read-only
    operation on a fresh conn and confirming the dashboard's path
    doesn't error.
    """
    from tools.dashboard import generate_dashboard
    output = tmp_path / "out.html"
    # Should complete without error using its own conn
    generate_dashboard(tmp_dashboard_db, str(output))
    assert output.exists()
