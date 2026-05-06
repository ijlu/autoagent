"""Tests for the per-city LST gate (Phase 3a.2).

LST-of-settlement-day gate replaces the global TTE gate as the primary
post-peak entry filter for cross-bracket. See
``reports/POSTFIX_REASSESSMENT_2026-05-05.md``.
"""

from __future__ import annotations

import pytest

from bot.learning.cross_bracket_lst_gate import (
    DEFAULT_LST_GATE_BY_SERIES,
    LstGate,
    get_lst_gate,
    set_lst_gate,
)
from bot.db import init_db
from bot.daemon.cross_bracket_shadow import (
    _is_in_lst_gate,
    _target_lst_date_from_settlement_key,
)


# ─── parser helpers ───────────────────────────────────────────────────


class TestSettlementKeyParse:
    def test_ny_settle_date(self):
        assert _target_lst_date_from_settlement_key("KXHIGHNY-26MAY04") == "2026-05-04"

    def test_lax_settle_date(self):
        assert _target_lst_date_from_settlement_key("KXHIGHLAX-26MAY15") == "2026-05-15"

    def test_invalid_key_returns_none(self):
        assert _target_lst_date_from_settlement_key("KXHIGHNY") is None
        assert _target_lst_date_from_settlement_key("KXHIGHNY-FOO") is None
        assert _target_lst_date_from_settlement_key("KXHIGHNY-26ZZZ04") is None


# ─── LstGate dataclass + defaults ─────────────────────────────────────


class TestDefaultGates:
    def test_all_six_cities_have_default(self):
        # All currently-traded weather series must have a default gate
        # (otherwise a deploy with a wiped kv_cache would gate everything to
        # the conservative LST 18-23 fallback).
        for series in (
            "KXHIGHNY", "KXHIGHLAX", "KXHIGHCHI",
            "KXHIGHAUS", "KXHIGHMIA", "KXHIGHDEN",
        ):
            assert series in DEFAULT_LST_GATE_BY_SERIES, f"{series} missing default LST gate"

    def test_default_gates_are_valid_ranges(self):
        for series, (lo, hi) in DEFAULT_LST_GATE_BY_SERIES.items():
            assert 0 <= lo <= 23, f"{series}: bad lo {lo}"
            assert 0 <= hi <= 23, f"{series}: bad hi {hi}"
            assert lo <= hi, f"{series}: lo ({lo}) > hi ({hi})"

    def test_lst_gate_includes(self):
        gate = LstGate(series="X", min_lst_hour=15, max_lst_hour=22, source="test")
        assert gate.includes(15) is True
        assert gate.includes(22) is True
        assert gate.includes(18) is True
        assert gate.includes(14) is False
        assert gate.includes(23) is False


# ─── kv_cache round-trip ──────────────────────────────────────────────


class TestKvCacheRoundtrip:
    def test_set_then_get_returns_cached(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "test.db")
        conn = init_db(db_path=db_path)

        set_lst_gate("KXHIGHTEST", min_lst_hour=14, max_lst_hour=22, conn=conn)
        gate = get_lst_gate("KXHIGHTEST", conn=conn)
        assert gate.min_lst_hour == 14
        assert gate.max_lst_hour == 22
        assert gate.source == "kv_cache"

    def test_get_with_no_cache_falls_back_to_default(self, tmp_path):
        conn = init_db(db_path=str(tmp_path / "test.db"))
        gate = get_lst_gate("KXHIGHNY", conn=conn)
        assert gate.source == "default"
        assert (gate.min_lst_hour, gate.max_lst_hour) == DEFAULT_LST_GATE_BY_SERIES["KXHIGHNY"]

    def test_get_unknown_series_uses_conservative_fallback(self, tmp_path):
        conn = init_db(db_path=str(tmp_path / "test.db"))
        gate = get_lst_gate("KXMADEUP", conn=conn)
        assert gate.source == "default_unknown_series"
        assert gate.min_lst_hour == 18

    def test_set_rejects_invalid_hours(self, tmp_path):
        conn = init_db(db_path=str(tmp_path / "test.db"))
        with pytest.raises(ValueError):
            set_lst_gate("KXHIGHX", min_lst_hour=-1, max_lst_hour=10, conn=conn)
        with pytest.raises(ValueError):
            set_lst_gate("KXHIGHX", min_lst_hour=20, max_lst_hour=10, conn=conn)
