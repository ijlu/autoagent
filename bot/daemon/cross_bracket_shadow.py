"""Cross-bracket portfolio runner — shadow + (gated) live trading.

Runs every cycle: pulls open weather markets, groups by settlement
event, scores via ``score_market_portfolio``, then either:
  - logs each per-bracket decision to ``alpha_backtest`` (shadow mode), or
  - posts each leg as a real Kalshi order AND logs it (live mode).

Live mode is OFF by default and gated by TWO independent switches:
  1. Global env ``CROSS_BRACKET_LIVE`` must be true
  2. Per-family kv key ``cross_bracket_live:<family>`` must be truthy
Both must pass; either being false keeps the family in shadow.

Additional safety rails (all configurable, see ``bot/config.py``):
  - LST + stability gate (PRIMARY entry filter): per-city
    ``is_post_peak_safe(lst_hour, stability_hours)`` from
    ``bot.learning.cross_bracket_lst_gate``. See Phase 3e in
    reports/POSTFIX_REASSESSMENT_2026-05-05.md.
  - TTE backstop: ``CROSS_BRACKET_MIN_TTE_HOURS`` (0.5h) prevents posting
    minutes before settle; ``CROSS_BRACKET_MAX_TTE_HOURS`` (24h)
    sanity ceiling. The LST gate is doing the actual entry-window
    work; TTE is now belt-and-suspenders.
  - Per-leg contract cap: ``CROSS_BRACKET_MAX_CONTRACTS_PER_LEG`` (default 1).
  - Per-portfolio leg cap: ``CROSS_BRACKET_MAX_LEGS_PER_PORTFOLIO`` (default 4).
  - Edge floor (live-only, separate from the shadow scorer's 0.07):
    ``CROSS_BRACKET_LIVE_MIN_EDGE`` (default 0.10).
  - Daily exposure cap: ``CROSS_BRACKET_DAILY_EXPOSURE_CAP_CENTS``
    tracked in ``kv_cache:cross_bracket_daily_exposure_<YYYY-MM-DD>``.

Why a separate runner instead of patching trade.py:
  * Cleanest separation. Existing trade flow keeps producing one
    decision per market visit; cross-bracket scoring runs in parallel.
  * Easier to roll back — flip env or kv key, no redeploy.
  * Shadow data is logged to the same alpha_backtest table, joinable
    by ticker for retro analysis.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from bot.config import (
    CROSS_BRACKET_LIVE,
    CROSS_BRACKET_MAX_CONTRACTS_PER_LEG,
    CROSS_BRACKET_MAX_LEGS_PER_PORTFOLIO,
    CROSS_BRACKET_DAILY_EXPOSURE_CAP_CENTS,
    CROSS_BRACKET_MIN_TTE_HOURS,
    CROSS_BRACKET_MAX_TTE_HOURS,
    CROSS_BRACKET_LIVE_MIN_EDGE,
    CROSS_BRACKET_SLIP_TOLERANCE_CENTS,
)
from bot.signals.weather_ensemble_v2 import (
    _city_for_ticker,
    _collect_gaussians,
    _apply_metar_post_peak_override,
    _weighted_inputs_with_group_discount,
    _COMBINED_SIGMA_FLOOR_F,
)
from bot.signals.weather_forecast import combine_gaussian


logger = logging.getLogger(__name__)


def _family_from_settlement_key(settlement_key: str) -> str:
    """KXHIGHNY-26APR30 → KXHIGHNY."""
    return settlement_key.split("-", 1)[0].upper()


def _is_family_live(conn, family: str) -> bool:
    """Per-family live trading kill switch. All three gates must pass:
    (1) global env CROSS_BRACKET_LIVE, (2) family NOT in
    CROSS_BRACKET_BLOCKLIST, (3) family-specific kv truthy.

    The three-layer design is belt-and-suspenders: the global env lets
    us instantly turn off ALL cross-bracket live trading via a deploy +
    restart; the blocklist hard-bans families with known structural
    problems even if their kv accidentally gets re-armed; the per-family
    kv lets us canary one family at a time without restarting.
    """
    if not CROSS_BRACKET_LIVE:
        return False
    # Hard block — survives an accidental kv re-arm. KXHIGHDEN is
    # currently the only entry (2026-05-12 audit: σ catastrophically
    # narrow vs actual day-to-day temperature variance).
    from bot.config import CROSS_BRACKET_BLOCKLIST
    if family.upper() in CROSS_BRACKET_BLOCKLIST:
        return False
    from bot.db import kv_get
    try:
        val = kv_get(conn, f"cross_bracket_live:{family}")
    except Exception:
        return False
    if val is True:
        return True
    if isinstance(val, dict) and val.get("enabled") is True:
        return True
    if isinstance(val, str) and val.lower() in ("true", "1", "yes"):
        return True
    return False


def _settlement_unix_from_key(settlement_key: str) -> Optional[int]:
    """KXHIGHNY-26APR30 → unix ts of 23:59:59 local clock time that day.

    Used by the TTE gate. Best-effort parse; returns None on
    unparseable format so the gate can fail-closed (skip live).

    DST-correct: we use IANA timezones via ``zoneinfo`` (Python 3.9+)
    rather than fixed LST offsets. Previously the function hardcoded
    LST offsets year-round, which during DST months (March-November)
    placed settle 1 hour later than the actual local-clock midnight —
    the cross-bracket TTE gate then thought TTE was 7.78h when reality
    was 6.78h, missing the first hour of every nightly live window.
    Confirmed via 2026-05-04 21:00 UTC observation (NY's actual EDT
    midnight is 04:00 UTC, code-via-LST said 05:00 UTC).
    """
    parts = settlement_key.split("-")
    if len(parts) < 2:
        return None
    suf = parts[1]
    if len(suf) < 7:
        return None
    months = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
              "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
    try:
        yy = int(suf[:2])
        mon = suf[2:5].upper()
        dd = int(suf[5:7])
        m_idx = months.index(mon) + 1
    except (ValueError, IndexError):
        return None
    family = _family_from_settlement_key(settlement_key)
    iana_tz = {
        "KXHIGHNY": "America/New_York",
        "KXHIGHMIA": "America/New_York",
        "KXHIGHCHI": "America/Chicago",
        "KXHIGHAUS": "America/Chicago",
        "KXHIGHDEN": "America/Denver",
        "KXHIGHLAX": "America/Los_Angeles",
    }.get(family, "America/New_York")
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        local_dt = datetime(2000 + yy, m_idx, dd, 23, 59, 59,
                            tzinfo=ZoneInfo(iana_tz))
        return int(local_dt.timestamp())
    except (ValueError, OverflowError, ImportError):
        return None


def _today_iso() -> str:
    """Date-bucket string for the daily exposure counter."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _get_daily_exposure_cents(conn) -> int:
    from bot.db import kv_get
    try:
        val = kv_get(conn, f"cross_bracket_daily_exposure_{_today_iso()}")
    except Exception:
        return 0
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, dict):
        return int(val.get("cents", 0) or 0)
    return 0


def _bump_daily_exposure_cents(conn, delta_cents: int) -> None:
    from bot.db import kv_set
    new_total = _get_daily_exposure_cents(conn) + delta_cents
    try:
        # 36-hour TTL so the counter rolls naturally to the next day.
        kv_set(conn,
               f"cross_bracket_daily_exposure_{_today_iso()}",
               {"cents": new_total, "updated": _today_iso()},
               ttl_seconds=129600)
    except Exception as exc:
        logger.warning("[cross_bracket_live] failed to bump exposure counter: %s", exc)


def _safe_client_order_id(settlement_key: str, leg_idx: int) -> str:
    """Build a valid Kalshi client_order_id.

    Rules (per CLAUDE.md regression watchlist):
      * Must start with "mm_"
      * No periods (Kalshi rejects)
      * Total length should stay under 64 chars
    Pattern: mm_xb_<settle_key>_<leg>_<unix_ms>
    """
    safe_key = settlement_key.replace(".", "_")
    return f"mm_xb_{safe_key}_{leg_idx}_{int(time.time() * 1000)}"


def _fetch_existing_positions() -> Optional[dict[str, int]]:
    """Snapshot current Kalshi positions as {ticker: signed_position_qty}.

    Positive = net YES holding, negative = net NO holding, zero = no
    position. Used by the live-post path to prevent re-posting on a
    bracket we already own (the strategy logs decisions every 5 min
    and would otherwise post repeatedly each cycle the edge persists).

    Returns None on fetch failure so the live path can fail closed instead
    of assuming unknown exposure is zero.
    """
    from bot.api import api_get
    try:
        resp = api_get("/portfolio/positions?limit=200")
    except Exception as exc:
        logger.warning(
            "[cross_bracket_live] positions fetch failed: %s — live posting "
            "will fail closed for this cycle", exc,
        )
        return None
    out: dict[str, int] = {}
    positions = resp.get("market_positions") or resp.get("positions") or []
    for pos in positions:
        ticker = pos.get("ticker") or ""
        if not ticker:
            continue
        raw = pos.get("position_fp") or pos.get("position", 0)
        try:
            qty = round(float(raw)) if raw is not None else 0
        except (TypeError, ValueError):
            qty = 0
        if qty != 0:
            out[ticker] = qty
    return out


def run_cross_bracket_shadow(conn) -> dict:
    """Score all currently-open weather markets via cross-bracket
    portfolio. Log each per-bracket decision to alpha_backtest with
    decision_type='cross_bracket_shadow'.

    Returns stats dict for telemetry.
    """
    stats = {
        "settlements_scored": 0,
        "total_brackets": 0,
        "decisions_buy_yes": 0,
        "decisions_buy_no": 0,
        "decisions_skip": 0,
        "errors": 0,
        "live_orders_posted": 0,
        "live_orders_failed": 0,
        "live_skipped_tte": 0,
        "live_skipped_edge": 0,
        "live_skipped_exposure_cap": 0,
        "live_skipped_leg_cap": 0,
        "live_skipped_family_off": 0,
        "live_skipped_already_holding": 0,
        "live_skipped_positions_unavailable": 0,
    }

    try:
        markets = _fetch_open_weather_markets()
    except Exception as exc:
        logger.warning("[cross_bracket_shadow] fetch failed: %s", exc)
        stats["errors"] += 1
        return stats

    if not markets:
        return stats

    from bot.scoring.bracket_portfolio import (
        group_markets_by_settlement, score_market_portfolio,
    )

    grouped = group_markets_by_settlement(markets)
    stats["settlements_scored"] = len(grouped)

    # Snapshot current positions ONCE at the top of the cycle. We pass
    # this through to the post path so the strategy can check "do we
    # already hold this bracket?" before posting. Without this guard,
    # the strategy posts a new order every 5 min on the same bracket
    # while edge persists, accumulating exposure beyond the intended
    # ≤4 legs/portfolio cap (which only counts within a single cycle).
    existing_positions = _fetch_existing_positions()

    for settlement_key, group in grouped.items():
        try:
            decisions = _score_one_settlement(group)
        except Exception as exc:
            logger.warning(
                "[cross_bracket_shadow] %s scoring failed: %s",
                settlement_key, exc,
            )
            stats["errors"] += 1
            continue

        if not decisions:
            continue

        stats["total_brackets"] += len(decisions)
        for d in decisions:
            if d.action == "buy_yes":
                stats["decisions_buy_yes"] += 1
            elif d.action == "buy_no":
                stats["decisions_buy_no"] += 1
            else:
                stats["decisions_skip"] += 1

        # Log + (conditionally) place orders. Each leg gets its own row,
        # all sharing the settlement_key as market_id.
        _process_decisions(conn, settlement_key, group, decisions, stats,
                           existing_positions=existing_positions)

    return stats


def _fetch_open_weather_markets() -> list[dict]:
    """Pull open KXHIGH* markets from Kalshi.

    Returns a flat list of market_data dicts (Kalshi's response
    format). Used by the cycle to score all weather brackets at once.
    """
    from bot.api import api_get

    out: list[dict] = []
    for series in ("KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA",
                   "KXHIGHAUS", "KXHIGHLAX", "KXHIGHDEN"):
        try:
            # api_get already prepends /trade-api/v2 — pass only the path tail.
            data = api_get(
                f"/markets?status=open&series_ticker={series}&limit=200"
            )
            if data:
                out.extend(data.get("markets", []))
        except Exception as exc:
            logger.warning("[cross_bracket_shadow] %s fetch failed: %s",
                          series, exc)
    return out


def _score_one_settlement(group: list[dict]) -> list:
    """Compute combined μ/σ from the first market in the group, then
    score every bracket against it.

    All markets in ``group`` share the same settlement event so they
    have the same predicted distribution.
    """
    from bot.scoring.bracket_portfolio import score_market_portfolio

    if not group:
        return []

    sample = group[0]
    ticker = sample.get("ticker", "")
    gaussians = _collect_gaussians(ticker, sample)
    if not gaussians:
        return []

    # 2026-05-05 (Phase 3d/3e): apply METAR post-peak fast-path BEFORE
    # combine, same as predict_v2's step 1b. Without this, cross-bracket
    # was scoring with wide-σ combined while WeatherQuoter (which goes
    # through predict_v2) was using the tight METAR-only override —
    # different μ/σ for the same market on the same cycle.
    gaussians = _apply_metar_post_peak_override(gaussians, ticker)

    weighted = _weighted_inputs_with_group_discount(gaussians)
    combined = combine_gaussian(weighted, combined_name="combined_v2")
    if combined is None:
        return []

    # Two-tier σ floor:
    #   1) Global physical floor ``_COMBINED_SIGMA_FLOOR_F`` (1.0°F) —
    #      matches predict_v2 step 4d.
    #   2) Per-family empirical RMSE floor — sourced from the
    #      2026-05-12 audit's residuals analysis. The post-peak fast-
    #      path tightens σ to ~1°F but actual day-to-day RMSE is
    #      1.3–2.4°F for most cities (6.5°F for Denver, hard-blocked).
    #      Inflating to the empirical floor tightens the conviction
    #      gate so cross_bracket fires only when the model genuinely
    #      disagrees with the market BEYOND the model's empirical
    #      precision. See tools/sigma_residuals.py.
    from bot.config import CROSS_BRACKET_FAMILY_SIGMA_FLOORS
    family = _family_from_settlement_key(ticker)
    family_floor = CROSS_BRACKET_FAMILY_SIGMA_FLOORS.get(
        family, _COMBINED_SIGMA_FLOOR_F,
    )
    effective_floor = max(_COMBINED_SIGMA_FLOOR_F, family_floor)
    sigma = max(combined.sigma_f, effective_floor)

    return score_market_portfolio(
        group,
        combined_mu=combined.mean_f,
        combined_sigma=sigma,
        sigma_floor=effective_floor,
    )


def _is_live_eligible_window(settlement_key: str) -> tuple[bool, float]:
    """Return (in_window, hours_to_settle).

    Hours-to-settle is computed from settlement_key. Returns (False, -1)
    on parse failure (fail-closed — won't trade live for unparseable
    keys).

    2026-05-06 (Phase 3e cleanup): this is now a belt-and-suspenders
    check, NOT the primary entry gate. The real entry gate is the
    per-city LST+stability check ``_is_in_lst_gate``. TTE thresholds
    are 0.5h (min — don't post at-settle) and 24h (max — skip
    next-day-or-later tickers, redundant with LST gate's date check).
    """
    settle_unix = _settlement_unix_from_key(settlement_key)
    if settle_unix is None:
        return False, -1.0
    now = time.time()
    hours_to_settle = (settle_unix - now) / 3600.0
    in_window = (
        CROSS_BRACKET_MIN_TTE_HOURS <= hours_to_settle <= CROSS_BRACKET_MAX_TTE_HOURS
    )
    return in_window, hours_to_settle


def _is_in_lst_gate(settlement_key: str) -> tuple[bool, str]:
    """Return (in_gate, reason) for the per-city LST + stability gate.

    Cross-bracket fires only when the METAR post-peak fast-path is
    safely armed for this city — i.e., either current LST is past the
    "always-arm" hour, or the running max has been stable enough hours
    that "peak surprise" risk is < 5% per the empirical city rules.

    Why same condition as fast-path: when fast-path can't arm, the
    combine falls back to wide-σ NWP-blended μ which correctly reflects
    forecast uncertainty for the strategy's purposes. But the strategy
    will then read that wide σ as "huge edge against market" and fire
    the wrong side. Phase 3c counterfactual showed this is the loss
    mechanism. So: gate cross-bracket on the same data the fast-path
    uses, ensuring the combine is METAR-tight whenever the strategy
    fires.

    Returns (False, reason) on parse failure, missing METAR state, or
    unsafe stability. Fails closed.
    """
    from bot.learning.cross_bracket_lst_gate import (
        get_running_high_state, is_post_peak_safe,
    )
    from bot.daemon.stations import station_for_ticker
    from tools.lst_align import lst_hour, lst_date

    family = _family_from_settlement_key(settlement_key)
    if family is None:
        return False, "lst_unparseable_family"
    station = station_for_ticker(f"{family}-stub-Bstub")
    if station is None:
        return False, "lst_unknown_station"

    target_lst = _target_lst_date_from_settlement_key(settlement_key)
    if target_lst is None:
        return False, "lst_unparseable_settle_date"

    now = time.time()
    cur_lst_hour = lst_hour(now, lst_offset=station.lst_offset)
    cur_lst_date = lst_date(now, lst_offset=station.lst_offset)

    if cur_lst_date != target_lst:
        return False, f"lst_date_{cur_lst_date}_vs_target_{target_lst}"

    # Read METAR poller's persisted running-high state. If absent (poller
    # didn't run today, kv_cache wiped), fall back to LST 18 as a
    # universal "definitely past peak" hour for all 6 currently-traded
    # cities — conservative.
    state = get_running_high_state(station.icao, target_lst)
    if state is None or state.get("last_increase_lst_hour", -1) < 0:
        if cur_lst_hour < 18:
            return False, f"lst_{cur_lst_hour}_no_metar_state_pre_18"
        stability_hours = 0  # conservative
    else:
        stability_hours = max(0, cur_lst_hour - state["last_increase_lst_hour"])

    if not is_post_peak_safe(station.series, cur_lst_hour, stability_hours):
        return False, (
            f"lst_{cur_lst_hour}_K{stability_hours}_unsafe_per_{station.series}_rule"
        )
    return True, f"lst_{cur_lst_hour}_K{stability_hours}_safe"


def _target_lst_date_from_settlement_key(settlement_key: str) -> Optional[str]:
    """Parse e.g. ``KXHIGHNY-26MAY04`` into ``2026-05-04``."""
    parts = settlement_key.split("-")
    if len(parts) < 2:
        return None
    raw = parts[1]
    if len(raw) != 7:
        return None
    months = {
        "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
        "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
    }
    try:
        yr = 2000 + int(raw[:2])
        mon = months[raw[2:5].upper()]
        day = int(raw[5:7])
        return f"{yr:04d}-{mon:02d}-{day:02d}"
    except (ValueError, KeyError):
        return None


def _best_ask_for_buy(
    ticker: str, side: str,
) -> tuple[Optional[int], Optional[int]]:
    """Return (best_ask_price_cents, size_at_best) for buying ``side``.

    Kalshi's orderbook returns BIDS for both yes and no. To buy YES we
    look at NO bids: a no_bid at price X is held by someone willing to
    sell YES at (100 - X) — that's our YES ask via reciprocity. The
    HIGHEST no_bid maps to the LOWEST yes_ask. Mirror for buying NO.

    Returns (None, None) when the orderbook is empty or unfetchable.
    The caller should treat None as "abort" — without depth visibility
    we can't bound slippage.
    """
    from bot.api import api_get
    try:
        resp = api_get(f"/markets/{ticker}/orderbook")
    except Exception as exc:
        logger.warning("[cross_bracket_live] orderbook fetch %s: %s", ticker, exc)
        return None, None

    book = resp.get("orderbook") or resp.get("orderbook_fp") or resp
    if side == "yes":
        # buying YES → look at NO bids → reciprocal yes_ask = 100 - no_bid
        levels = book.get("no") or book.get("no_dollars") or []
    elif side == "no":
        # buying NO → look at YES bids → reciprocal no_ask = 100 - yes_bid
        levels = book.get("yes") or book.get("yes_dollars") or []
    else:
        return None, None
    if not levels:
        return None, None

    # Parse and find the HIGHEST bid on the opposite side. Kalshi's API
    # returns prices either as integer cents [42, 50] or dollar strings
    # [["0.42", "50"]]. Normalize defensively.
    best_bid_cents: Optional[int] = None
    best_size: int = 0
    for level in levels:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            continue
        raw_p, raw_q = level[0], level[1]
        try:
            if isinstance(raw_p, str):
                p = round(float(raw_p) * 100)
            elif isinstance(raw_p, float) and raw_p < 1.0:
                p = round(raw_p * 100)
            else:
                p = int(raw_p)
            q = round(float(raw_q)) if isinstance(raw_q, str) else int(raw_q)
        except (TypeError, ValueError):
            continue
        if p < 0 or p > 100 or q <= 0:
            continue
        if best_bid_cents is None or p > best_bid_cents:
            best_bid_cents = p
            best_size = q
        elif p == best_bid_cents:
            # Multiple entries at same price — combine.
            best_size += q

    if best_bid_cents is None:
        return None, None
    # Reciprocal: implied ask price for the side we're buying.
    implied_ask = 100 - best_bid_cents
    return implied_ask, best_size


def _post_live_order(
    conn, settlement_key: str, leg_idx: int, decision,
    contracts: int,
) -> tuple[bool, Optional[str]]:
    """Place a live Kalshi order for one cross-bracket leg, with two
    layers of slippage protection.

    Layer 1: the limit price is capped at ``best_ask + slip_tolerance``
    so a thin top-of-book can be walked up by at most ``slip_tolerance``
    cents.

    Layer 2: the count is capped at the visible top-of-book size, so we
    never fill across multiple price levels in a single order. If the
    full target size needs more depth than visible, we simply post less
    — the higher-tier infra (portfolio sizing) is responsible for any
    fill-replenishment logic.

    Cross-bracket alpha comes from MISPRICING — we *want* to cross the
    spread when the market is wrong relative to our model. So
    ``post_only=False``. The two slippage layers above replace the
    safety post_only=True was meant to provide.

    Returns (success, order_id_or_error_str).
    """
    from bot.api import api_post

    if decision.action == "skip" or decision.side is None:
        return False, "skip_action"

    side = "yes" if decision.side == "yes" else "no"
    target_price = decision.price_cents

    # Layer 1+2 prep: check the book for slippage exposure.
    best_ask, best_ask_size = _best_ask_for_buy(decision.ticker, side)
    if best_ask is None or best_ask_size is None or best_ask_size <= 0:
        return False, "orderbook_unavailable_or_empty"

    # Layer 1 — cap the limit price at best_ask + slip_tolerance, but
    # never above our own model FV (target_price). If our FV is BELOW
    # best_ask, there's no edge here — abort.
    if target_price < best_ask:
        return False, f"no_edge:fv={target_price}<ask={best_ask}"
    limit_price = min(target_price, best_ask + CROSS_BRACKET_SLIP_TOLERANCE_CENTS)

    # Layer 2 — cap count to top-of-book size.
    safe_count = min(contracts, best_ask_size)
    if safe_count <= 0:
        return False, "empty_top_of_book"

    body = {
        "ticker": decision.ticker,
        "client_order_id": _safe_client_order_id(settlement_key, leg_idx),
        "side": side,
        "action": "buy",
        "type": "limit",
        "count": safe_count,
        "yes_price": limit_price if side == "yes" else None,
        "no_price": limit_price if side == "no" else None,
        # Cross-bracket needs to cross the spread to capture mispricing.
        # Slippage is bounded by Layers 1+2 above.
        "post_only": False,
    }
    # Strip None fields — Kalshi doesn't accept null prices.
    body = {k: v for k, v in body.items() if v is not None}

    try:
        resp = api_post("/portfolio/orders", body)
    except Exception as exc:
        return False, f"api_exception:{type(exc).__name__}:{exc}"

    if not isinstance(resp, dict):
        return False, f"unexpected_response_shape:{type(resp).__name__}"
    order = resp.get("order") or {}
    order_id = order.get("order_id")
    if not order_id:
        return False, f"no_order_id:{resp.get('error') or resp}"

    # Record (order_id, client_order_id) for fill-attribution recovery.
    # Kalshi's /portfolio/fills response no longer echoes client_order_id
    # (2026-05-10+ format drift) — without this row, the corresponding
    # fill would tag as ``manual`` and the strategy lose attribution.
    # See bot/daemon/fills_writer.record_posted_order docstring.
    from bot.daemon.fills_writer import record_posted_order
    record_posted_order(
        conn,
        order_id=order_id,
        client_order_id=body["client_order_id"],
        ticker=decision.ticker,
        side=side,
        action="buy",
        count=safe_count,
        price_cents=limit_price,
        source_hint="cross_bracket",
        live_mode=True,
    )
    return True, order_id


def _process_decisions(
    conn, settlement_key: str, group: list[dict],
    decisions: list, stats: dict,
    existing_positions: Optional[dict] = None,
) -> None:
    """For each decision, log to alpha_backtest. Additionally, if live
    mode is on AND the family/TTE/edge/exposure gates pass, place a
    real Kalshi order.

    Each leg gets its own alpha_backtest row tagged by the settlement
    key in ``market_id`` so portfolios are reconstructable later. Live
    rows use ``decision_type=CROSS_BRACKET_LIVE`` and
    ``decision_outcome=POSTED`` (or ``DISCARDED`` if a gate rejected).
    """
    from bot.learning.alpha_log import (
        DecisionOutcome, DecisionType, EnsembleSnapshot, MarketSnapshot,
        log_decision, market_snapshot_from_dict,
    )

    by_ticker = {m.get("ticker"): m for m in group}
    family = _family_from_settlement_key(settlement_key)
    family_live = _is_family_live(conn, family)

    # TTE check fires once per portfolio (all legs share a settlement).
    in_tte_window, hours_to_settle = _is_live_eligible_window(settlement_key)
    # LST gate (per-city, post-peak only) — also portfolio-level.
    in_lst_window, lst_reason = _is_in_lst_gate(settlement_key)

    # Cap legs per portfolio (live-mode only — shadow logs everything).
    non_skip = [d for d in decisions if d.action != "skip"]
    kept_live_leg_ids: Optional[set[int]] = None
    if family_live and in_tte_window and in_lst_window and \
            len(non_skip) > CROSS_BRACKET_MAX_LEGS_PER_PORTFOLIO:
        # Sort by edge desc and take top N. We compute a single edge
        # number per leg = max(edge_yes, edge_no) (whichever side fired).
        def _leg_edge(d):
            if d.action == "buy_yes" and d.edge_yes is not None:
                return d.edge_yes
            if d.action == "buy_no" and d.edge_no is not None:
                return d.edge_no
            return 0.0
        non_skip_sorted = sorted(non_skip, key=_leg_edge, reverse=True)
        kept = set(id(x) for x in non_skip_sorted[:CROSS_BRACKET_MAX_LEGS_PER_PORTFOLIO])
        kept_live_leg_ids = kept
        # Mark the rest as live-ineligible; they'll still log as shadow rows.
        for d in non_skip:
            if id(d) not in kept:
                stats["live_skipped_leg_cap"] += 1

    daily_exposure_cents = _get_daily_exposure_cents(conn)

    for leg_idx, d in enumerate(decisions):
        if d.action == "skip":
            continue
        market = by_ticker.get(d.ticker)
        if market is None:
            continue

        edge_yes_str = (
            f";edge_yes={d.edge_yes:+.3f}" if d.edge_yes is not None else ""
        )
        edge_no_str = (
            f";edge_no={d.edge_no:+.3f}" if d.edge_no is not None else ""
        )
        base_notes = (
            f"cross_bracket;leg={leg_idx};p_yes={d.p_yes:.3f}"
            f"{edge_yes_str}{edge_no_str}"
        )

        # Decide whether THIS leg can go live. All gates must pass.
        leg_edge = (d.edge_yes if d.action == "buy_yes" else d.edge_no) or 0.0
        leg_can_go_live = True
        live_skip_reason = None

        if not family_live:
            leg_can_go_live = False
            live_skip_reason = "family_not_live"
            stats["live_skipped_family_off"] += 1
        elif not in_lst_window:
            # Primary entry gate (Phase 3e): per-city LST + stability.
            # See _is_in_lst_gate / is_post_peak_safe.
            leg_can_go_live = False
            live_skip_reason = lst_reason
            stats["live_skipped_lst"] = stats.get("live_skipped_lst", 0) + 1
        elif not in_tte_window:
            # Belt-and-suspenders: 0.5h floor avoids posting at-settle;
            # 24h ceiling is redundant with LST gate's date check.
            leg_can_go_live = False
            live_skip_reason = f"tte_{hours_to_settle:.1f}h_outside_backstop"
            stats["live_skipped_tte"] += 1
        elif leg_edge < CROSS_BRACKET_LIVE_MIN_EDGE:
            leg_can_go_live = False
            live_skip_reason = (
                f"edge_{leg_edge:+.3f}_below_live_floor_"
                f"{CROSS_BRACKET_LIVE_MIN_EDGE:.3f}"
            )
            stats["live_skipped_edge"] += 1
        elif kept_live_leg_ids is not None and id(d) not in kept_live_leg_ids:
            leg_can_go_live = False
            live_skip_reason = (
                f"leg_cap_{CROSS_BRACKET_MAX_LEGS_PER_PORTFOLIO}"
            )
        elif existing_positions is None:
            leg_can_go_live = False
            live_skip_reason = "positions_unavailable"
            stats["live_skipped_positions_unavailable"] = (
                stats.get("live_skipped_positions_unavailable", 0) + 1
            )

        contracts = min(
            CROSS_BRACKET_MAX_CONTRACTS_PER_LEG,
            CROSS_BRACKET_MAX_CONTRACTS_PER_LEG,  # placeholder for Kelly later
        )
        leg_cost_cents = (d.price_cents or 0) * contracts
        if leg_can_go_live and \
                daily_exposure_cents + leg_cost_cents > CROSS_BRACKET_DAILY_EXPOSURE_CAP_CENTS:
            leg_can_go_live = False
            live_skip_reason = (
                f"exposure_cap_{daily_exposure_cents}c+{leg_cost_cents}c>"
                f"{CROSS_BRACKET_DAILY_EXPOSURE_CAP_CENTS}c"
            )
            stats["live_skipped_exposure_cap"] += 1

        # Existing-position gate: don't re-post on a bracket where we
        # already have a position on the side we're about to buy. Without
        # this, the strategy posts a new order every cycle while edge
        # persists. Position sign convention: + = net YES holding,
        # − = net NO holding. We're posting to BUY, so block if we already
        # hold the same side.
        if leg_can_go_live and existing_positions:
            existing_qty = existing_positions.get(d.ticker, 0)
            same_side_holding = (
                (d.side == "yes" and existing_qty > 0)
                or (d.side == "no" and existing_qty < 0)
            )
            if same_side_holding:
                leg_can_go_live = False
                live_skip_reason = f"already_holding:{d.side}={existing_qty}"
                stats["live_skipped_already_holding"] += 1

        # ── Live path ──
        order_id: Optional[str] = None
        if leg_can_go_live:
            success, result = _post_live_order(
                conn, settlement_key, leg_idx, d, contracts,
            )
            if success:
                order_id = result
                stats["live_orders_posted"] += 1
                daily_exposure_cents += leg_cost_cents
                _bump_daily_exposure_cents(conn, leg_cost_cents)
                logger.info(
                    "[cross_bracket_live] POSTED %s %s %d×%d¢ "
                    "edge=%+.3f tte=%.1fh order_id=%s",
                    d.ticker, d.side, contracts, d.price_cents or 0,
                    leg_edge, hours_to_settle, order_id,
                )
            else:
                stats["live_orders_failed"] += 1
                logger.warning(
                    "[cross_bracket_live] POST FAILED %s %s: %s",
                    d.ticker, d.side, result,
                )

        # ── Always log to alpha_backtest ──
        # When live posting succeeded → CROSS_BRACKET_LIVE + POSTED.
        # When live was attempted and skipped or failed → CROSS_BRACKET_SHADOW
        # + SHADOW_ONLY (preserves the historical shadow data shape).
        # `notes` always carries the cross_bracket marker so we can
        # filter retro queries.
        notes = base_notes
        if leg_can_go_live and order_id:
            decision_type = DecisionType.CROSS_BRACKET_LIVE
            decision_outcome = DecisionOutcome.POSTED
            notes += f";order_id={order_id};tte={hours_to_settle:.1f}h"
        else:
            decision_type = DecisionType.CROSS_BRACKET_SHADOW
            decision_outcome = DecisionOutcome.SHADOW_ONLY
            if live_skip_reason:
                notes += f";live_skip={live_skip_reason}"

        try:
            log_decision(
                conn,
                ticker=d.ticker,
                decision_type=decision_type,
                decision_outcome=decision_outcome,
                ensemble=EnsembleSnapshot(p_yes=float(d.p_yes)),
                market=market_snapshot_from_dict(market),
                side=d.side,
                price_cents=d.price_cents,
                contracts=contracts if order_id else 1,
                notes=notes,
                market_id=settlement_key,
                portfolio_leg_count=len(decisions),
            )
        except Exception as exc:
            logger.warning(
                "[cross_bracket_shadow] log_decision failed for %s: %r",
                d.ticker, exc,
            )
