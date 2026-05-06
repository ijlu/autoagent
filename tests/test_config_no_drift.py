"""T0.1 regression test — config must not drift across daemon cycles.

Before the T0.1 fix, trade.apply_phase_limits() multiplied KELLY_FRACTION,
MIN_EDGE, and SINGLE_SOURCE_EDGE by phase multipliers *in place* — so running
it N times in a long-lived daemon compounded the multiplication. The
active-feedback block in trade.main() had the same bug with edge_multiplier.

This test pins the invariant: calling apply_phase_limits() any number of
times with the same inputs must produce the same output, and the module
globals must match what the first call produced.

If this test fails, the ratcheting bug is back.
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest


def _stub_cryptography():
    """Stub the cryptography imports trade.py pulls in.

    trade.py has `from cryptography.hazmat.primitives import hashes, serialization`
    and `from cryptography.hazmat.primitives.asymmetric import padding`. On dev
    machines with an arch-mismatched venv, the real import fails with a dlopen
    error. We only exercise apply_phase_limits() — no crypto is used — so
    injecting bare stub modules into sys.modules is sufficient.
    """
    for mod_path in (
        "cryptography",
        "cryptography.hazmat",
        "cryptography.hazmat.primitives",
        "cryptography.hazmat.primitives.asymmetric",
    ):
        if mod_path not in sys.modules:
            sys.modules[mod_path] = types.ModuleType(mod_path)
    # Attach the names trade.py imports directly.
    primitives = sys.modules["cryptography.hazmat.primitives"]
    if not hasattr(primitives, "hashes"):
        primitives.hashes = types.ModuleType("hashes")
    if not hasattr(primitives, "serialization"):
        primitives.serialization = types.ModuleType("serialization")
    asym = sys.modules["cryptography.hazmat.primitives.asymmetric"]
    if not hasattr(asym, "padding"):
        asym.padding = types.ModuleType("padding")


@pytest.fixture(scope="module")
def trade_module():
    """Fresh import of trade.py with cryptography stubbed."""
    _stub_cryptography()
    try:
        if "trade" in sys.modules:
            # Re-importing is safer than reload — reload re-runs module-level
            # side effects (DB writes, print statements) which we don't want.
            return sys.modules["trade"]
        return importlib.import_module("trade")
    except Exception as e:  # pragma: no cover
        pytest.skip(f"trade.py import failed: {e}")


def test_apply_phase_limits_is_idempotent(trade_module):
    """Calling apply_phase_limits N times with same inputs = same output."""
    trade = trade_module
    # Phase 3 has kelly_mult=0.50 and edge_mult=1.25 — the original bug would
    # shrink KELLY and grow MIN_EDGE geometrically on repeat calls.
    phase_cfg = trade.PHASE_CONFIG[3]

    first = trade.apply_phase_limits(3, phase_cfg)
    for _ in range(100):
        later = trade.apply_phase_limits(3, phase_cfg)
        assert later == first, (
            f"apply_phase_limits drifted after repeat calls: "
            f"first={first} later={later}"
        )


def test_globals_do_not_ratchet_across_100_cycles(trade_module):
    """Simulate 100 daemon cycles; globals must equal phase*initial every time."""
    trade = trade_module
    phase_cfg = trade.PHASE_CONFIG[3]
    _, _, _, _, _, kelly_mult, edge_mult, _ = phase_cfg

    expected_kelly = trade._INITIAL_KELLY_FRACTION * kelly_mult
    expected_min_edge = trade._INITIAL_MIN_EDGE * edge_mult
    expected_sse = trade._INITIAL_SINGLE_SOURCE_EDGE * edge_mult

    for cycle in range(100):
        trade.apply_phase_limits(3, phase_cfg)
        assert trade.KELLY_FRACTION == expected_kelly, (
            f"cycle {cycle}: KELLY drifted to {trade.KELLY_FRACTION}, "
            f"expected {expected_kelly}"
        )
        assert trade.MIN_EDGE == expected_min_edge, (
            f"cycle {cycle}: MIN_EDGE drifted to {trade.MIN_EDGE}, "
            f"expected {expected_min_edge}"
        )
        assert trade.SINGLE_SOURCE_EDGE == expected_sse, (
            f"cycle {cycle}: SINGLE_SOURCE_EDGE drifted to "
            f"{trade.SINGLE_SOURCE_EDGE}, expected {expected_sse}"
        )


def test_phase_switch_recovers_initial_scale(trade_module):
    """Switching phases must pick up from the initial values, not the current."""
    trade = trade_module
    # Run phase 2 (edge_mult=1.5) a bunch, then switch to phase 5 (edge_mult=1.0).
    # Under the old bug, MIN_EDGE would still be ~1.5× initial even after the
    # phase 5 call because it read the current mutated value.
    for _ in range(10):
        trade.apply_phase_limits(2, trade.PHASE_CONFIG[2])

    trade.apply_phase_limits(5, trade.PHASE_CONFIG[5])
    assert trade.MIN_EDGE == trade._INITIAL_MIN_EDGE, (
        f"Phase 5 did not restore MIN_EDGE to initial: "
        f"got {trade.MIN_EDGE}, expected {trade._INITIAL_MIN_EDGE}"
    )
    assert trade.KELLY_FRACTION == trade._INITIAL_KELLY_FRACTION, (
        f"Phase 5 did not restore KELLY_FRACTION to initial: "
        f"got {trade.KELLY_FRACTION}, expected {trade._INITIAL_KELLY_FRACTION}"
    )


def test_initial_snapshot_is_never_mutated(trade_module):
    """The _INITIAL_* values must be treated as immutable reference."""
    trade = trade_module
    snapshot = {
        "kelly": trade._INITIAL_KELLY_FRACTION,
        "min_edge": trade._INITIAL_MIN_EDGE,
        "sse": trade._INITIAL_SINGLE_SOURCE_EDGE,
        "max_pos_pct": trade._INITIAL_MAX_POSITION_PCT,
        "max_port_pct": trade._INITIAL_MAX_PORTFOLIO_PCT,
        "max_contracts": trade._INITIAL_MAX_CONTRACTS,
    }
    for phase in (1, 2, 3, 4, 5):
        trade.apply_phase_limits(phase, trade.PHASE_CONFIG[phase])
    assert trade._INITIAL_KELLY_FRACTION == snapshot["kelly"]
    assert trade._INITIAL_MIN_EDGE == snapshot["min_edge"]
    assert trade._INITIAL_SINGLE_SOURCE_EDGE == snapshot["sse"]
    assert trade._INITIAL_MAX_POSITION_PCT == snapshot["max_pos_pct"]
    assert trade._INITIAL_MAX_PORTFOLIO_PCT == snapshot["max_port_pct"]
    assert trade._INITIAL_MAX_CONTRACTS == snapshot["max_contracts"]
