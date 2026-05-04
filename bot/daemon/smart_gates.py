"""Smart gating logic for weather market making.

Decides which weather markets to quote and when, based on:
1. Time-of-day: only quote during active temperature hours
2. Bracket proximity: only quote brackets near expected outcome
3. METAR trajectory: don't fight the temperature trend
4. Forecast confidence: wider spreads when uncertain
5. Settlement certainty: skip markets already decided

Each gate returns (should_quote: bool, reason: str, spread_multiplier: float).
spread_multiplier > 1.0 means widen the spread for safety.
"""

from __future__ import annotations

import logging

from bot.daemon.stations import STATIONS, lst_offset_for_station

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Station LST offset lookup — legacy alias kept for any external importer.
# New code should call ``lst_offset_for_station()`` directly.
# ---------------------------------------------------------------------------

_STATION_LST_OFFSET: dict[str, int] = {
    sid: station.lst_offset for sid, station in STATIONS.items()
}


# ═══════════════════════════════════════════════════════════════════════════════
# Gate 1: Time-of-Day
# ═══════════════════════════════════════════════════════════════════════════════

def time_of_day_gate(station: str, hours_left: float) -> tuple[bool, str, float]:
    """Gate based on local standard time of day.

    Quote only during the configured LST window. Within that window,
    adjust spreads based on how much temperature movement is still likely.

    The upper cutoff is overridable via env ``WEATHER_QUOTE_MAX_LST_HOUR``
    (default 19, current behavior). Lower it (e.g. 17 or 15) when the
    LST-hour vs market-Brier analysis shows the late-day window is
    structurally negative-EV. As of 2026-04-30 the data showed v2 trails
    market by Brier 0.27+ at LST ≥15 — see reports/CROSS_BRACKET_BACKTEST_2026-04-30.md.

    Parameters
    ----------
    station : str
        ICAO station ID (e.g. "KJFK").  Used to derive LST offset.
    hours_left : float
        Hours remaining in the settlement day (until 23:59 LST).

    Returns
    -------
    (should_quote, reason, spread_multiplier)
    """
    import os as _os
    try:
        max_lst_hour = float(_os.environ.get("WEATHER_QUOTE_MAX_LST_HOUR", "19"))
    except ValueError:
        max_lst_hour = 19.0
    # Clamp to a sane range so a typo in env can't accidentally disable
    # the gate or block all hours. 5 = "5am LST" floor (no real reason
    # to quote earlier); 23 = "11pm LST" ceiling (settlement boundary).
    max_lst_hour = max(5.0, min(23.0, max_lst_hour))

    # Settlement day ends at 23:59 LST.
    # hours_left == 17 means it's ~7am;  hours_left == 5 means it's ~7pm.
    # So: lst_hour_approx ≈ 24 - hours_left  (not perfect, but good enough).
    lst_hour = 24.0 - hours_left

    if lst_hour < 7.0:
        return (False, f"pre-7am LST ({lst_hour:.1f}h): temp hasn't started rising", 1.0)
    if lst_hour >= max_lst_hour:
        return (False,
                f"past-{max_lst_hour:.0f}-LST ({lst_hour:.1f}h): "
                f"high locked in / market beats us late-day",
                1.0)

    # Inside the 7am-7pm window: adjust spread by sub-period
    if lst_hour < 10.0:
        # 7am-10am: full speed, tightest spreads -- temp just starting to rise,
        # high uncertainty means METAR edge is large
        return (True, f"morning ramp ({lst_hour:.1f}h LST): tight spreads", 1.0)
    elif lst_hour < 14.0:
        # 10am-2pm: peak heating -- most METAR changes, keep tight
        return (True, f"peak heating ({lst_hour:.1f}h LST): tight spreads", 1.0)
    elif lst_hour < 17.0:
        # 2pm-5pm: high likely reached, widen slightly
        return (True, f"afternoon ({lst_hour:.1f}h LST): widen slightly", 1.2)
    else:
        # 5pm-7pm: marginal window, widen more -- only quoting because
        # there's still a theoretical chance of a late-day reading
        return (True, f"late afternoon ({lst_hour:.1f}h LST): wide spreads", 1.5)


# ═══════════════════════════════════════════════════════════════════════════════
# Gate 2: Bracket Proximity
# ═══════════════════════════════════════════════════════════════════════════════

def bracket_proximity_gate(
    bracket_floor: float,
    bracket_cap: float,
    running_high: float,
    forecast_high: float,
    hours_left: float,
) -> tuple[bool, str, float]:
    """Gate based on how close this bracket is to the expected daily high.

    Only quote brackets within +-6 deg F of max(running_high, forecast_high).
    Brackets far from the expected outcome have tiny probability and huge
    relative error -- not worth quoting.

    Parameters
    ----------
    bracket_floor : float
        Lower bound of the bracket in deg F.
    bracket_cap : float
        Upper bound of the bracket in deg F.
    running_high : float
        Highest observed temperature today so far (deg F).
    forecast_high : float
        Forecast daily high (deg F).
    hours_left : float
        Hours remaining in the settlement day.

    Returns
    -------
    (should_quote, reason, spread_multiplier)
    """
    expected = max(running_high, forecast_high)

    # Already passed through this bracket -- running high already above cap.
    # P(landing in bracket) ~ 0 since daily high can only increase.
    if running_high > bracket_cap:
        if hours_left < 8:
            # Late in the day, running_high already blew past this bracket.
            # Still quote but with wide spread -- counterparties know it's ~0.
            return (
                True,
                f"bracket [{bracket_floor}-{bracket_cap}] already passed "
                f"(running={running_high:.1f}F): wide spread",
                2.0,
            )
        else:
            # Early enough that we might see a data correction or reset,
            # but still likely done -- skip.
            return (
                False,
                f"bracket [{bracket_floor}-{bracket_cap}] already passed "
                f"(running={running_high:.1f}F, {hours_left:.1f}h left): skip",
                1.0,
            )

    # Bracket is far above what we expect (unreachable)
    if bracket_floor > expected + 8:
        return (
            False,
            f"bracket floor {bracket_floor}F is >{8}F above expected "
            f"{expected:.1f}F: unreachable",
            1.0,
        )

    # Bracket is far below running high (already passed through it)
    if bracket_cap < running_high - 8:
        return (
            False,
            f"bracket cap {bracket_cap}F is >{8}F below running "
            f"{running_high:.1f}F: already passed",
            1.0,
        )

    # Within the +-6F sweet spot
    bracket_mid = (bracket_floor + bracket_cap) / 2.0
    distance = abs(bracket_mid - expected)

    if distance <= 3:
        return (
            True,
            f"bracket [{bracket_floor}-{bracket_cap}] near expected "
            f"{expected:.1f}F (dist={distance:.1f}): tight spread",
            1.0,
        )
    elif distance <= 6:
        return (
            True,
            f"bracket [{bracket_floor}-{bracket_cap}] moderate distance "
            f"from expected {expected:.1f}F (dist={distance:.1f}): slight widen",
            1.2,
        )
    else:
        # 6-8F away -- still quotable but wide
        return (
            True,
            f"bracket [{bracket_floor}-{bracket_cap}] far from expected "
            f"{expected:.1f}F (dist={distance:.1f}): wide spread",
            1.5,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Gate 3: METAR Trajectory
# ═══════════════════════════════════════════════════════════════════════════════

def trajectory_gate(
    trajectory_f_per_hr: float,
    bracket_floor: float,
    bracket_cap: float,
    running_high: float,
    side: str,
) -> tuple[bool, str, float]:
    """Gate based on the current temperature trajectory.

    Don't fight the temperature trend.  If temp is rising fast into a bracket,
    don't sell YES -- you'll get adversely selected.

    Parameters
    ----------
    trajectory_f_per_hr : float
        Current temperature change rate in deg F per hour.  Positive = warming.
    bracket_floor : float
        Lower bound of the bracket in deg F.
    bracket_cap : float
        Upper bound of the bracket in deg F.
    running_high : float
        Highest observed temperature today so far (deg F).
    side : str
        "yes" or "no" -- which side we would be *selling* (posting as maker).

    Returns
    -------
    (should_quote, reason, spread_multiplier)
    """
    side = side.lower()
    bracket_above = bracket_floor > running_high  # bracket is above current temp

    # Extreme trajectory -- genuinely anomalous, skip entirely.
    # Normal morning warming peaks ~10F/hr in spring/summer, so threshold
    # is set above that to avoid rejecting normal diurnal heating.
    if abs(trajectory_f_per_hr) > 12.0:
        return (
            False,
            f"extreme trajectory ({trajectory_f_per_hr:+.1f}F/hr): "
            f"skip -- model may be wrong",
            1.0,
        )

    # Very fast warming (6-12 F/hr): rare but plausible mid-morning peak
    if trajectory_f_per_hr > 6.0:
        if bracket_above and side == "yes":
            return (
                False,
                f"very fast warming ({trajectory_f_per_hr:+.1f}F/hr) toward bracket "
                f"[{bracket_floor}-{bracket_cap}]: don't sell YES",
                1.0,
            )
        return (
            True,
            f"very fast warming ({trajectory_f_per_hr:+.1f}F/hr): widen spread, "
            f"sell NO only",
            2.0,
        )

    # Fast warming (2-6 F/hr)
    if trajectory_f_per_hr > 2.0:
        if bracket_above and side == "yes":
            # Bracket is above running high and temp is rising fast toward it.
            # Selling YES is dangerous -- counterparty knows temp is heading
            # into the bracket.
            return (
                False,
                f"fast warming ({trajectory_f_per_hr:+.1f}F/hr) toward bracket "
                f"[{bracket_floor}-{bracket_cap}]: don't sell YES",
                1.0,
            )
        # For NO side or brackets below running high, still quote but wider
        return (
            True,
            f"fast warming ({trajectory_f_per_hr:+.1f}F/hr): widen spread, "
            f"sell NO only",
            1.5,
        )

    # Moderate warming (1-2 F/hr)
    if trajectory_f_per_hr > 1.0:
        return (
            True,
            f"moderate warming ({trajectory_f_per_hr:+.1f}F/hr): slight widen",
            1.2,
        )

    # Mild warming (0-1 F/hr) or cooling/flat -- normal
    if trajectory_f_per_hr >= -1.0:
        return (
            True,
            f"mild/flat trajectory ({trajectory_f_per_hr:+.1f}F/hr): normal",
            1.0,
        )

    # Moderate cooling (-2 to -1 F/hr) -- fine for trading
    if trajectory_f_per_hr >= -2.0:
        return (
            True,
            f"moderate cooling ({trajectory_f_per_hr:+.1f}F/hr): normal",
            1.0,
        )

    # Fast cooling (-4 to -2 F/hr) -- unusual but doesn't affect daily high
    # (daily high is running max, so cooling doesn't reduce it)
    return (
        True,
        f"fast cooling ({trajectory_f_per_hr:+.1f}F/hr): normal "
        f"(doesn't affect daily high)",
        1.0,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Gate 4: Settlement Certainty
# ═══════════════════════════════════════════════════════════════════════════════

def settlement_certainty_gate(
    running_high: float,
    bracket_floor: float,
    bracket_cap: float,
    hours_left: float,
) -> tuple[bool, str, float]:
    """Gate for markets that are already effectively decided.

    When the outcome is near-certain, market makers get picked off by
    informed traders who know the "right" price better than our CDF model.

    Parameters
    ----------
    running_high : float
        Highest observed temperature today so far (deg F).
    bracket_floor : float
        Lower bound of the bracket in deg F.
    bracket_cap : float
        Upper bound of the bracket in deg F.
    hours_left : float
        Hours remaining in the settlement day.

    Returns
    -------
    (should_quote, reason, spread_multiplier)
    """
    # Bracket is blown: running high already exceeded the bracket cap.
    # P(in bracket) ~ 0 because daily high can only increase.
    if running_high >= bracket_cap and hours_left < 4:
        return (
            False,
            f"bracket [{bracket_floor}-{bracket_cap}] blown "
            f"(running={running_high:.1f}F, {hours_left:.1f}h left): "
            f"P(in bracket)~0, skip",
            1.0,
        )

    # Inside the bracket with very little time remaining.
    # P(in bracket) is high but our CDF model handles these edge cases poorly.
    if (running_high >= bracket_floor
            and running_high < bracket_cap
            and hours_left < 2):
        return (
            False,
            f"inside bracket [{bracket_floor}-{bracket_cap}] "
            f"(running={running_high:.1f}F) with {hours_left:.1f}h left: "
            f"too close to call, skip",
            1.0,
        )

    # Too close to call with too little time
    if hours_left < 1 and abs(running_high - bracket_floor) < 2:
        return (
            False,
            f"running={running_high:.1f}F within 2F of bracket floor "
            f"{bracket_floor}F with {hours_left:.1f}h left: "
            f"coin flip, skip",
            1.0,
        )

    # Not decided yet -- OK to quote
    return (True, "settlement not yet certain", 1.0)


# ═══════════════════════════════════════════════════════════════════════════════
# Gate 5: Forecast Confidence
# ═══════════════════════════════════════════════════════════════════════════════

def forecast_confidence_gate(
    running_high: float,
    forecast_high: float,
    hours_left: float,
) -> tuple[bool, str, float]:
    """Gate based on agreement between forecast and observations.

    Large disagreements between forecast and running observations suggest
    our model inputs are unreliable -- widen spread or skip.

    Parameters
    ----------
    running_high : float
        Highest observed temperature today so far (deg F).
    forecast_high : float
        Forecast daily high (deg F).
    hours_left : float
        Hours remaining in the settlement day.

    Returns
    -------
    (should_quote, reason, spread_multiplier)
    """
    gap = abs(forecast_high - running_high)

    # Early in the day (>8h left): forecast might still be right even if
    # there's a big gap -- temp hasn't peaked yet.
    if hours_left > 8:
        if gap > 5:
            # Big gap but it's early, forecast could still be right.
            return (
                True,
                f"forecast gap {gap:.1f}F but {hours_left:.1f}h left: "
                f"early, trust forecast",
                1.0,
            )
        return (
            True,
            f"forecast gap {gap:.1f}F, early ({hours_left:.1f}h left): normal",
            1.0,
        )

    # Mid-day (6-8h left): moderate caution
    if hours_left > 6:
        if gap > 5:
            return (
                True,
                f"forecast gap {gap:.1f}F at mid-day: widen",
                1.5,
            )
        return (
            True,
            f"forecast gap {gap:.1f}F at mid-day: normal",
            1.0,
        )

    # Afternoon / late day (<=6h left): observations dominate
    if gap <= 3:
        return (
            True,
            f"forecast agrees with obs (gap={gap:.1f}F, "
            f"{hours_left:.1f}h left): high confidence",
            1.0,
        )
    elif gap <= 5:
        return (
            True,
            f"moderate forecast gap ({gap:.1f}F, {hours_left:.1f}h left): "
            f"slight widen",
            1.3,
        )
    else:
        # Forecast says 85F but running high is 72F at 3pm -- something is off.
        return (
            True,
            f"large forecast gap ({gap:.1f}F, {hours_left:.1f}h left): "
            f"something is off, wide spread",
            2.0,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Composite Gate
# ═══════════════════════════════════════════════════════════════════════════════

_MAX_COMPOSITE_MULTIPLIER = 3.0


def evaluate_all_gates(
    station: str,
    bracket_floor: float,
    bracket_cap: float,
    running_high: float,
    forecast_high: float,
    hours_left: float,
    trajectory_f_per_hr: float,
    side: str = "yes",
) -> tuple[bool, str, float]:
    """Run ALL gates in sequence and combine results.

    If ANY gate says don't quote, return immediately with that rejection.
    Otherwise, the spread multiplier is the product of all gates' multipliers,
    capped at ``_MAX_COMPOSITE_MULTIPLIER``.

    Parameters
    ----------
    station : str
        ICAO station ID.
    bracket_floor : float
        Lower bound of the bracket in deg F.
    bracket_cap : float
        Upper bound of the bracket in deg F.
    running_high : float
        Highest observed temperature today so far (deg F).
    forecast_high : float
        Forecast daily high (deg F).
    hours_left : float
        Hours remaining in the settlement day.
    trajectory_f_per_hr : float
        Temperature change rate (deg F / hr).
    side : str
        "yes" or "no" -- which side we'd be selling.

    Returns
    -------
    (should_quote, combined_reason, combined_spread_multiplier)
    """
    gates = [
        ("time_of_day", time_of_day_gate(station, hours_left)),
        ("bracket_proximity", bracket_proximity_gate(
            bracket_floor, bracket_cap, running_high, forecast_high, hours_left,
        )),
        ("trajectory", trajectory_gate(
            trajectory_f_per_hr, bracket_floor, bracket_cap, running_high, side,
        )),
        ("settlement_certainty", settlement_certainty_gate(
            running_high, bracket_floor, bracket_cap, hours_left,
        )),
        ("forecast_confidence", forecast_confidence_gate(
            running_high, forecast_high, hours_left,
        )),
    ]

    combined_mult = 1.0
    reasons: list[str] = []

    for gate_name, (should_quote, reason, mult) in gates:
        if not should_quote:
            log.debug("gate REJECT [%s]: %s", gate_name, reason)
            return (False, f"[{gate_name}] {reason}", mult)
        combined_mult *= mult
        reasons.append(f"[{gate_name}] {reason} (x{mult:.1f})")

    # Cap the composite multiplier
    if combined_mult > _MAX_COMPOSITE_MULTIPLIER:
        log.debug(
            "composite multiplier %.2f exceeds cap %.1f, rejecting",
            combined_mult, _MAX_COMPOSITE_MULTIPLIER,
        )
        return (
            False,
            f"composite spread multiplier {combined_mult:.2f}x exceeds "
            f"cap {_MAX_COMPOSITE_MULTIPLIER:.1f}x: too risky. "
            f"Gates: {'; '.join(reasons)}",
            combined_mult,
        )

    combined_reason = "; ".join(reasons)
    log.debug("all gates PASS (x%.2f): %s", combined_mult, combined_reason)
    return (True, combined_reason, combined_mult)


# ═══════════════════════════════════════════════════════════════════════════════
# Spread helper
# ═══════════════════════════════════════════════════════════════════════════════

def compute_smart_spread(base_half_spread_cents: int, spread_multiplier: float) -> int:
    """Apply spread multiplier to base half-spread.

    Returns adjusted half-spread in cents, floored at ``base_half_spread_cents``
    and capped at 15c.

    Parameters
    ----------
    base_half_spread_cents : int
        The base half-spread in cents (e.g. 3 for a 6c total spread).
    spread_multiplier : float
        Multiplier from the gate evaluation (>=1.0).

    Returns
    -------
    int
        Adjusted half-spread in cents, in [base, 15].
    """
    adjusted = int(round(base_half_spread_cents * spread_multiplier))
    return max(base_half_spread_cents, min(adjusted, 15))
