"""Regression guard for CLAUDE.md Known Bug Pattern #9.

Correlated sources must NOT count as fully independent in the ensemble's
effective-source-count. ``n_effective`` feeds the directional edge-threshold
scaling (3+ → 5%, 2 → 7%, 1 → 10–12%). Overcounting means we trade on edges
that aren't really there.

Two failure modes the previous implementation suffered from:

  (1) Two weather sources (e.g. ``hrrr`` + ``nbm``) — historically this branch
      worked, but pinning it makes regressions visible.

  (2) A single source that sits in MULTIPLE correlated groups — e.g. ``fred``
      is a member of ``cpi``, ``fed``, ``nfp``, and ``gdp`` simultaneously.
      The old loop iterated each group and added 1.0 every time, so a single
      ``fred`` source was counted as 4. This was a real, latent bug.

The fixed implementation lives at ``bot.signals.ensemble._compute_n_effective``
and uses "claim by first matching group" semantics: once a source is claimed
by any group, subsequent groups can't double-count it.
"""

from __future__ import annotations

from bot.config import CORRELATED_GROUPS
from bot.signals.ensemble import _compute_n_effective


def test_single_uncorrelated_source_counts_as_one() -> None:
    assert _compute_n_effective({"odds_api"}, CORRELATED_GROUPS) == 1.0


def test_three_correlated_weather_sources_collapse_to_one() -> None:
    n = _compute_n_effective({"hrrr", "nbm", "metar"}, CORRELATED_GROUPS)
    assert n == 1.0, f"3 weather sources should collapse to 1 effective, got {n}"


def test_weather_plus_uncorrelated_counts_as_two() -> None:
    n = _compute_n_effective({"hrrr", "nbm", "odds_api"}, CORRELATED_GROUPS)
    assert n == 2.0, f"2 weather + 1 uncorrelated should be 2 effective, got {n}"


def test_source_in_multiple_groups_counts_once() -> None:
    """``fred`` sits in cpi/fed/nfp/gdp — must not be counted 4×."""
    n = _compute_n_effective({"fred"}, CORRELATED_GROUPS)
    assert n == 1.0, (
        f"`fred` is in 4 correlated groups; previous implementation counted it "
        f"as {n} effective sources, inflating edge-threshold scaling"
    )


def test_two_sources_each_in_multiple_groups() -> None:
    """``fred`` (cpi/fed/nfp/gdp) + ``bls`` (cpi/nfp) — they share groups, so
    n_effective should be 1 (one combined economics group), not 6."""
    n = _compute_n_effective({"fred", "bls"}, CORRELATED_GROUPS)
    assert n == 1.0, f"fred+bls share correlated groups; expected 1, got {n}"


def test_n_effective_never_exceeds_raw_source_count() -> None:
    """Sanity: under any input, ``n_effective <= len(source_names)``."""
    candidates = {
        "hrrr", "nbm", "metar", "weather_ensemble",
        "fred", "bls", "fedwatch", "zq_futures", "adp_nfp",
        "polymarket", "metaculus",
        "odds_api", "finnhub",
    }
    for k in range(1, len(candidates) + 1):
        # Build subsets of progressively larger size to stress the dedup path.
        subset = set(list(candidates)[:k])
        n = _compute_n_effective(subset, CORRELATED_GROUPS)
        assert n <= len(subset), (
            f"n_effective={n} > raw count {len(subset)} for {subset}"
        )
