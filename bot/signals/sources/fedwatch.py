"""CME FedWatch-style implied probability source for Fed funds rate markets.

Derives forward-looking market-implied probabilities for FOMC rate decisions,
similar to the CME FedWatch Tool. Uses CME Group's published probability data
as the primary source, falling back to FRED-derived estimates.

Provides a significant upgrade over the spot-rate-only FRED source for KXFED
markets by incorporating market expectations of future rate changes.
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime, timezone, timedelta

import requests

from bot.api import cached_get, _CACHE, rate_limit_wait
from bot.config import FRED_API_KEY
from bot.signals.sources._fomc_calendar import (
    FOMC_MEETING_DATES as _FOMC_MEETING_DATES,
    FOMC_CALENDAR_CUTOFF as _FOMC_CALENDAR_CUTOFF,
    MONTH_ABBR as _MONTH_ABBR,
    RATE_RANGES as _RATE_RANGES,
    parse_fomc_dates as _parse_fomc_dates,
    next_meeting_after as _next_meeting_after,
    closest_meeting_to as _closest_meeting_to,
    last_meeting_on_or_before as _last_meeting_on_or_before,
    meetings_between as _meetings_between,
    is_beyond_calendar as _is_beyond_calendar,
)


# ══════════════════════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════════════════════

_FEDWATCH_CACHE_TTL = 14400  # 4 hours — rates don't move fast
_CACHE_KEY = "fedwatch_probabilities"

# Browser-like headers for CME endpoint (they block plain bot User-Agents)
_CME_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html",
}

# Ticker / title patterns that indicate a Fed rate market
_FED_TICKER_PREFIX = "KXFED"
_FED_TITLE_KEYWORDS = [
    "federal funds", "fed funds", "fed rate", "interest rate",
    "fomc", "fed cut", "fed hike", "federal reserve",
    "fed hold", "rate decision", "rate cut", "rate hike",
]

# _MONTH_ABBR imported from _fomc_calendar


# FOMC meeting helpers imported from _fomc_calendar:
# _parse_fomc_dates, _next_meeting_after, _closest_meeting_to,
# _last_meeting_on_or_before, _meetings_between, _is_beyond_calendar


# ══════════════════════════════════════════════════════════════════════════════
# Data fetching — CME FedWatch probabilities
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_cme_probabilities() -> dict | None:
    """Try to fetch FedWatch probabilities from CME Group.

    The CME publishes meeting-level probability distributions at a public
    JSON endpoint.  Returns parsed dict or None on failure.
    """
    urls = [
        "https://www.cmegroup.com/services/fed-fund-probability/probability.json",
        "https://www.cmegroup.com/CmeWS/mvc/FedWatch/FedFundProbability.json",
    ]

    for url in urls:
        try:
            rate_limit_wait(url)
            resp = requests.get(url, timeout=10, headers=_CME_HEADERS)
            if resp.status_code != 200:
                print(f"[fedwatch] CME HTTP {resp.status_code} from {url.split('?')[0]}")
                continue
            data = resp.json()
            if not data:
                continue
            return data
        except Exception as exc:
            print(f"[fedwatch] CME fetch error: {type(exc).__name__}: {exc}")
            continue

    return None


def _parse_cme_data(raw: dict) -> list[dict] | None:
    """Parse raw CME JSON into a list of meeting dicts with probability maps.

    CME returns a variety of formats depending on the endpoint version.
    We try several known schemas.
    """
    meetings = []

    # Schema A: list of meeting objects directly
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        # Schema B: nested under a key
        items = (raw.get("meetings") or raw.get("data")
                 or raw.get("fedFundProbabilities") or [])
        if not isinstance(items, list):
            return None
    else:
        return None

    for item in items:
        if not isinstance(item, dict):
            continue

        # Extract meeting date
        date_str = (item.get("meetingDate") or item.get("date")
                    or item.get("meeting_date") or "")
        if not date_str:
            continue

        # Parse date — try multiple formats
        meeting_dt = None
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d %b %Y", "%b %d, %Y"):
            try:
                meeting_dt = datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        if meeting_dt is None:
            continue

        # Extract probabilities per rate range
        probabilities: dict[str, float] = {}

        # Try: item has "probabilities" dict directly
        prob_data = item.get("probabilities") or item.get("probs") or {}
        if isinstance(prob_data, dict):
            for range_key, pval in prob_data.items():
                try:
                    probabilities[range_key] = float(pval) / 100.0 if float(pval) > 1.0 else float(pval)
                except (ValueError, TypeError):
                    continue

        # Try: item has individual range fields like "range_425_450"
        if not probabilities:
            for key, val in item.items():
                range_match = re.match(r'range_(\d+)_(\d+)', key)
                if range_match:
                    lo = int(range_match.group(1)) / 100.0
                    hi = int(range_match.group(2)) / 100.0
                    try:
                        p = float(val)
                        p = p / 100.0 if p > 1.0 else p
                        probabilities[f"{lo:.2f}-{hi:.2f}"] = p
                    except (ValueError, TypeError):
                        continue

        if probabilities:
            meetings.append({
                "date": meeting_dt.strftime("%Y-%m-%d"),
                "datetime": meeting_dt,
                "probabilities": probabilities,
            })

    return meetings if meetings else None


# ══════════════════════════════════════════════════════════════════════════════
# Data fetching — FRED fallback
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_fred_rate_data() -> dict | None:
    """Fetch current rate data from FRED and build synthetic probability distributions.

    Uses:
      - DFEDTARU / DFEDTARL: target range upper/lower
      - DFF: daily effective federal funds rate

    Returns dict with current_rate, target bounds, and synthetic meeting probabilities.
    """
    if not FRED_API_KEY:
        print("[fedwatch] No FRED_API_KEY — cannot build fallback estimates")
        return None

    def _fred_latest(series_id: str) -> float | None:
        url = (
            f"https://api.stlouisfed.org/fred/series/observations?"
            f"series_id={series_id}&api_key={FRED_API_KEY}&file_type=json"
            f"&sort_order=desc&limit=5"
        )
        data = cached_get(f"fedwatch_fred_{series_id}", url, timeout=8)
        if not data:
            return None
        for obs in data.get("observations", []):
            val = obs.get("value", ".")
            if val != ".":
                return float(val)
        return None

    upper = _fred_latest("DFEDTARU")
    lower = _fred_latest("DFEDTARL")
    eff = _fred_latest("DFF")

    if upper is None and lower is None and eff is None:
        print("[fedwatch] FRED returned no rate data")
        return None

    # Determine current rate
    if upper is not None and lower is not None:
        current_rate = (upper + lower) / 2.0
        target_upper = upper
        target_lower = lower
    elif eff is not None:
        current_rate = eff
        # Infer target range (effective rate is usually within the 25bp range)
        target_lower = math.floor(eff * 4) / 4.0
        target_upper = target_lower + 0.25
    else:
        return None

    return {
        "current_rate": current_rate,
        "target_upper": target_upper,
        "target_lower": target_lower,
    }


def _build_synthetic_meeting_probs(
    current_rate: float,
    target_lower: float,
    target_upper: float,
) -> list[dict]:
    """Build synthetic probability distributions for upcoming FOMC meetings.

    Uses a simple model: the further out the meeting, the wider the distribution
    of possible rate outcomes.  The model is centered on the current rate with
    a slight dovish bias (markets historically price in more cuts than hikes).

    Each meeting that passes adds one opportunity to change rates, so
    uncertainty grows with the square root of meetings remaining.
    """
    now = datetime.now(timezone.utc)
    meetings_out = []
    fomc_dates = _parse_fomc_dates()
    future_meetings = [m for m in fomc_dates if m > now]

    if not future_meetings:
        return []

    # Find the current target range
    current_range_key = f"{target_lower:.2f}-{target_upper:.2f}"

    for idx, meeting_dt in enumerate(future_meetings[:8]):  # limit to 8 meetings ahead
        # Uncertainty scales with sqrt of meetings ahead
        n_meetings_ahead = idx + 1
        # Standard deviation in 25bp increments: ~0.5 steps per meeting, sqrt scaling
        sigma_steps = 0.6 * math.sqrt(n_meetings_ahead)
        # Slight dovish bias: market typically prices ~0.3 cuts per meeting
        mean_shift_steps = -0.3 * n_meetings_ahead

        # Build probability distribution over rate ranges
        probabilities: dict[str, float] = {}
        total = 0.0

        for lo, hi in _RATE_RANGES:
            midpoint = (lo + hi) / 2.0
            current_mid = (target_lower + target_upper) / 2.0
            steps_from_current = (midpoint - current_mid) / 0.25

            # Gaussian probability centered on expected rate
            z = (steps_from_current - mean_shift_steps) / max(sigma_steps, 0.3)
            p = math.exp(-0.5 * z * z)
            range_key = f"{lo:.2f}-{hi:.2f}"
            probabilities[range_key] = p
            total += p

        # Normalize to sum to 1.0
        if total > 0:
            for k in probabilities:
                probabilities[k] /= total

        # Drop negligible probabilities (< 0.5%)
        probabilities = {k: v for k, v in probabilities.items() if v >= 0.005}

        # Re-normalize after dropping
        total2 = sum(probabilities.values())
        if total2 > 0:
            probabilities = {k: v / total2 for k, v in probabilities.items()}

        meetings_out.append({
            "date": meeting_dt.strftime("%Y-%m-%d"),
            "datetime": meeting_dt,
            "probabilities": probabilities,
        })

    return meetings_out


# ══════════════════════════════════════════════════════════════════════════════
# Main fetch function
# ══════════════════════════════════════════════════════════════════════════════

def fetch_fedwatch_probabilities() -> dict | None:
    """Fetch FedWatch-style implied probabilities for upcoming FOMC meetings.

    Attempts CME Group data first, then falls back to FRED-derived estimates.
    Results are cached for 4 hours.

    Returns:
        {
            "current_rate": float,
            "target_upper": float,
            "target_lower": float,
            "source": str,          # "cme" or "fred_synthetic"
            "meetings": [
                {
                    "date": "YYYY-MM-DD",
                    "probabilities": {"4.25-4.50": 0.65, ...}
                },
                ...
            ]
        }
        or None on total failure.
    """
    # Check in-memory cache
    now = time.time()
    if _CACHE_KEY in _CACHE:
        cached_val, cached_ts = _CACHE[_CACHE_KEY]
        if isinstance(cached_val, dict) and now - cached_ts < _FEDWATCH_CACHE_TTL:
            return cached_val

    result = None

    # ── Attempt 1: Yahoo Finance ZQ futures (market-implied, replaces broken CME) ──
    try:
        from bot.signals.sources.zq_futures import fetch_zq_fedwatch_probabilities
        zq_result = fetch_zq_fedwatch_probabilities()
        if zq_result and zq_result.get("meetings"):
            result = zq_result
            print(f"[fedwatch] ZQ futures data loaded: {len(result['meetings'])} meetings, "
                  f"current rate={result['current_rate']:.2f}%")
    except Exception as e:
        print(f"[fedwatch] ZQ futures failed: {e}")

    # ── Attempt 2 (legacy): CME Group — currently IP-blocked (HTTP 403) ──
    if result is None:
        cme_raw = _fetch_cme_probabilities()
        if cme_raw is not None:
            meetings = _parse_cme_data(cme_raw)
            if meetings:
                fred_data = _fetch_fred_rate_data()
                current_rate = fred_data["current_rate"] if fred_data else 4.33
                target_upper = fred_data["target_upper"] if fred_data else 4.50
                target_lower = fred_data["target_lower"] if fred_data else 4.25

                result = {
                    "current_rate": current_rate,
                    "target_upper": target_upper,
                    "target_lower": target_lower,
                    "source": "cme",
                    "meetings": meetings,
                }
                print(f"[fedwatch] CME data loaded: {len(meetings)} meetings, "
                      f"current rate={current_rate:.2f}%")

    # ── Attempt 3: FRED fallback with synthetic probabilities ──
    if result is None:
        fred_data = _fetch_fred_rate_data()
        if fred_data:
            meetings = _build_synthetic_meeting_probs(
                fred_data["current_rate"],
                fred_data["target_lower"],
                fred_data["target_upper"],
            )
            if meetings:
                result = {
                    "current_rate": fred_data["current_rate"],
                    "target_upper": fred_data["target_upper"],
                    "target_lower": fred_data["target_lower"],
                    "source": "fred_synthetic",
                    "meetings": meetings,
                }
                print(f"[fedwatch] FRED fallback: {len(meetings)} meetings, "
                      f"current rate={fred_data['current_rate']:.2f}%")

    if result is None:
        print("[fedwatch] All data sources failed")
        return None

    # Cache for 4 hours
    _CACHE[_CACHE_KEY] = (result, now)
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Ticker / title parsing
# ══════════════════════════════════════════════════════════════════════════════

def _is_fed_market(ticker: str, market_data: dict) -> bool:
    """Check whether this market is about Fed rate decisions."""
    ticker_upper = (ticker or "").upper()
    if _FED_TICKER_PREFIX in ticker_upper:
        return True
    title = (market_data.get("title") or market_data.get("subtitle") or "").lower()
    return any(kw in title for kw in _FED_TITLE_KEYWORDS)


def _parse_ticker_date(ticker: str) -> datetime | None:
    """Parse an expiry date from a Kalshi KXFED ticker.

    Supports two formats:
        Old:  KXFED-27APR25-T4.625  -> 2025-04-27  (DD + MMM + YY)
        Live: KXFED-26JUL-T3.25     -> resolves to closest FOMC in Jul 2026
              KXFED-27APR-T2.50     -> resolves to closest FOMC in Apr 2027

    The live format encodes year + month only (no day).  We resolve to the
    FOMC meeting date in that month via _closest_meeting_to().
    """
    upper = ticker.upper()

    # ── Old format: DD + MMM + YY (e.g. "27APR25") ──
    m = re.search(r'(\d{1,2})([A-Z]{3})(\d{2})', upper)
    if m:
        day = int(m.group(1))
        month_abbr = m.group(2)
        year_short = int(m.group(3))
        month = _MONTH_ABBR.get(month_abbr)
        if month is not None:
            year = 2000 + year_short
            try:
                return datetime(year, month, day, tzinfo=timezone.utc)
            except ValueError:
                pass  # fall through to new-format attempt

    # ── Live format: KXFED-{YY}{MMM}-T... (e.g. "KXFED-26JUL-T3.25") ──
    # The segment after "KXFED-" is {2-digit year}{3-letter month}.
    m2 = re.search(r'KXFED-(\d{2})([A-Z]{3})', upper)
    if m2:
        year = 2000 + int(m2.group(1))
        month = _MONTH_ABBR.get(m2.group(2))
        if month is not None:
            # Use mid-month as anchor, then resolve to nearest FOMC meeting
            mid_month = datetime(year, month, 15, tzinfo=timezone.utc)
            meeting = _closest_meeting_to(mid_month)
            if meeting is not None and meeting.month == month and meeting.year == year:
                return meeting
            # If no exact month match, still return the mid-month anchor
            # so downstream _last_meeting_on_or_before() can find the right meeting
            return mid_month

    return None


def _parse_ticker_threshold(ticker: str) -> float | None:
    """Parse threshold rate from Kalshi ticker suffix.

    Examples:
        KXFED-27APR25-T4.625  -> 4.625
        KXFED-07MAY26-T4.375  -> 4.375
        KXFED-28JAN26-B4.25-4.50  -> None (bracket, not threshold)
    """
    m = re.search(r'-T(\d+\.?\d*)', ticker.upper())
    if m:
        return float(m.group(1))
    return None


def _parse_ticker_bracket(ticker: str) -> tuple[float, float] | None:
    """Parse bracket range from Kalshi ticker suffix.

    Examples:
        KXFED-07MAY26-B4.25-4.50  -> (4.25, 4.50)
    """
    m = re.search(r'-B(\d+\.?\d*)-(\d+\.?\d*)', ticker.upper())
    if m:
        return float(m.group(1)), float(m.group(2))
    return None


def _parse_expiry_from_market(market_data: dict) -> datetime | None:
    """Parse the market expiry/close date from market_data fields."""
    for field in ("close_time", "expiration_time", "expected_expiration_time"):
        val = market_data.get(field)
        if not val or not isinstance(val, str):
            continue
        try:
            return datetime.fromisoformat(val.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
    return None


def _parse_title_direction(title: str) -> str | None:
    """Parse the direction from a market title.

    Returns "cut", "hike", "hold", "above", or "below".
    """
    title_lower = title.lower()

    if any(w in title_lower for w in ["cut", "lower", "decrease", "reduce"]):
        return "cut"
    if any(w in title_lower for w in ["hike", "raise", "increase"]):
        return "hike"
    if any(w in title_lower for w in ["hold", "unchanged", "maintain", "no change"]):
        return "hold"
    if any(w in title_lower for w in ["above", "over", "at or above", "at least", "exceed"]):
        return "above"
    if any(w in title_lower for w in ["below", "under", "at or below", "less than"]):
        return "below"

    return None


def _parse_title_threshold(title: str) -> float | None:
    """Extract a rate threshold from the market title.

    Matches patterns like "above 4.375%", "target rate 4.50%", etc.
    """
    m = re.search(r'(\d+\.\d+)\s*%', title)
    if m:
        return float(m.group(1))
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Probability computation
# ══════════════════════════════════════════════════════════════════════════════

def _sum_probs_above_threshold(probabilities: dict[str, float], threshold: float) -> float:
    """Sum probabilities of all rate ranges whose midpoint >= threshold.

    The fed funds rate target is a 25bp RANGE (e.g., 4.25-4.50).
    When a market asks "above 4.375%", that means midpoint of 4.25-4.50 = 4.375.
    We sum all ranges whose midpoint is >= the threshold.
    """
    total = 0.0
    for range_key, prob in probabilities.items():
        m = re.match(r'(\d+\.?\d*)\s*-\s*(\d+\.?\d*)', range_key)
        if not m:
            continue
        lo = float(m.group(1))
        hi = float(m.group(2))
        midpoint = (lo + hi) / 2.0
        if midpoint >= threshold - 0.001:  # small epsilon for float comparison
            total += prob
    return total


def _sum_probs_below_threshold(probabilities: dict[str, float], threshold: float) -> float:
    """Sum probabilities of all rate ranges whose midpoint < threshold."""
    total = 0.0
    for range_key, prob in probabilities.items():
        m = re.match(r'(\d+\.?\d*)\s*-\s*(\d+\.?\d*)', range_key)
        if not m:
            continue
        lo = float(m.group(1))
        hi = float(m.group(2))
        midpoint = (lo + hi) / 2.0
        if midpoint < threshold + 0.001:
            total += prob
    return total


def _prob_in_range(probabilities: dict[str, float], target_lo: float, target_hi: float) -> float:
    """Sum probabilities of ranges that fall within [target_lo, target_hi]."""
    total = 0.0
    for range_key, prob in probabilities.items():
        m = re.match(r'(\d+\.?\d*)\s*-\s*(\d+\.?\d*)', range_key)
        if not m:
            continue
        lo = float(m.group(1))
        hi = float(m.group(2))
        # Range overlaps if it's within the target bracket
        if lo >= target_lo - 0.001 and hi <= target_hi + 0.001:
            total += prob
    return total


def _classify_meeting_probs(
    probabilities: dict[str, float],
    current_lower: float,
    current_upper: float,
) -> tuple[float, float, float]:
    """Classify meeting probabilities into cut/hold/hike.

    Returns (cut_prob, hold_prob, hike_prob).
    """
    current_range = f"{current_lower:.2f}-{current_upper:.2f}"
    hold_prob = probabilities.get(current_range, 0.0)

    cut_prob = 0.0
    hike_prob = 0.0
    current_mid = (current_lower + current_upper) / 2.0

    for range_key, prob in probabilities.items():
        if range_key == current_range:
            continue
        m = re.match(r'(\d+\.?\d*)\s*-\s*(\d+\.?\d*)', range_key)
        if not m:
            continue
        lo = float(m.group(1))
        hi = float(m.group(2))
        midpoint = (lo + hi) / 2.0
        if midpoint < current_mid - 0.001:
            cut_prob += prob
        elif midpoint > current_mid + 0.001:
            hike_prob += prob

    return cut_prob, hold_prob, hike_prob


# ══════════════════════════════════════════════════════════════════════════════
# Main source entry point
# ══════════════════════════════════════════════════════════════════════════════

def get_fedwatch_estimate(ticker: str, market_data: dict) -> tuple[float | None, str | None]:
    """FedWatch-style estimate for Fed rate markets.

    Called by the ensemble router.  Returns (probability, source_tag) or
    (None, None) if this market is not a Fed rate market or data is unavailable.
    """
    try:
        if not _is_fed_market(ticker, market_data):
            return None, None

        fw = fetch_fedwatch_probabilities()
        if fw is None:
            return None, None

        current_rate = fw["current_rate"]
        target_upper = fw["target_upper"]
        target_lower = fw["target_lower"]
        meetings = fw["meetings"]
        data_source = fw["source"]

        if not meetings:
            return None, None

        title = (market_data.get("title") or market_data.get("subtitle") or "")

        # ── Parse what the market is asking ──

        # 1. Try to get date and threshold from ticker
        market_date = _parse_ticker_date(ticker)
        threshold = _parse_ticker_threshold(ticker)
        bracket = _parse_ticker_bracket(ticker)

        # 2. Fall back to title parsing
        direction = _parse_title_direction(title)
        if threshold is None:
            threshold = _parse_title_threshold(title)

        # 3. If we can't figure out what's being asked, bail
        if threshold is None and bracket is None and direction is None:
            print(f"[fedwatch] Cannot parse market question from ticker={ticker} "
                  f"title={title[:80]}")
            return None, None

        # ── Determine market expiry date ──
        # Prefer close_time / expiration_time from market_data over ticker date
        expiry_date = _parse_expiry_from_market(market_data) or market_date

        # ── Horizon guard ──
        # Refuse to estimate if expiry is beyond our FOMC calendar coverage.
        # Returning a wrong probability is worse than returning None.
        if expiry_date is not None and _is_beyond_calendar(expiry_date):
            print(f"[fedwatch] {ticker} expiry {expiry_date.strftime('%Y-%m-%d')} "
                  f"beyond calendar cutoff — skipping")
            return None, None

        # ── Find the relevant meeting ──
        # The effective rate on the expiry date is determined by the last
        # FOMC meeting on or before that date.  For near-term markets where
        # that meeting has already happened, the current rate is already set.
        now = datetime.now(timezone.utc)
        if expiry_date is not None:
            target_meeting = _last_meeting_on_or_before(expiry_date)
        else:
            # No expiry info — fall back to next upcoming meeting
            target_meeting = _next_meeting_after(now)

        if target_meeting is None:
            print("[fedwatch] No matching FOMC meeting found")
            return None, None

        # If the relevant meeting is already in the past, the rate is known —
        # it is the current rate with certainty.
        if target_meeting <= now:
            current_range_key = f"{target_lower:.2f}-{target_upper:.2f}"
            best_meeting = {
                "date": target_meeting.strftime("%Y-%m-%d"),
                "datetime": target_meeting,
                "probabilities": {current_range_key: 1.0},
            }
        else:
            # Future meeting — find it in the fetched meeting data
            target_str = target_meeting.strftime("%Y-%m-%d")
            best_meeting = None
            best_delta = float("inf")
            for m in meetings:
                try:
                    m_dt = m.get("datetime") or datetime.strptime(
                        m["date"], "%Y-%m-%d"
                    ).replace(tzinfo=timezone.utc)
                    delta = abs((m_dt - target_meeting).total_seconds())
                    if delta < best_delta:
                        best_delta = delta
                        best_meeting = m
                except (ValueError, KeyError):
                    continue

            if best_meeting is None:
                print(f"[fedwatch] No meeting data near {target_str}")
                return None, None

        probs = best_meeting["probabilities"]

        # ── Compute probability ──

        prob: float | None = None

        # Case 1: Bracket market — probability of a specific range
        if bracket is not None:
            bracket_lo, bracket_hi = bracket
            prob = _prob_in_range(probs, bracket_lo, bracket_hi)
            source_tag = (f"fedwatch:{data_source}:bracket={bracket_lo:.2f}-{bracket_hi:.2f}"
                          f":meeting={best_meeting['date']}")

        # Case 2: Threshold market — "above X%"
        elif threshold is not None:
            # Determine direction: default to "above" for -T suffix tickers
            is_above = True
            if direction == "below":
                is_above = False
            elif direction is not None and direction not in ("above", "hike"):
                # "cut" / "hold" are handled separately below
                pass

            if direction == "cut":
                # "Will the Fed cut?" = probability of rates below current
                prob = _sum_probs_below_threshold(probs, (target_lower + target_upper) / 2.0)
            elif direction == "hold":
                current_range_key = f"{target_lower:.2f}-{target_upper:.2f}"
                prob = probs.get(current_range_key, 0.0)
            elif direction == "hike":
                prob = _sum_probs_above_threshold(probs, (target_lower + target_upper) / 2.0 + 0.25)
            elif is_above:
                prob = _sum_probs_above_threshold(probs, threshold)
            else:
                prob = _sum_probs_below_threshold(probs, threshold)

            source_tag = (f"fedwatch:{data_source}:thresh={threshold}"
                          f":{'above' if is_above else 'below'}"
                          f":meeting={best_meeting['date']}")

        # Case 3: Direction-only (no threshold) — "Will the Fed cut/hike/hold?"
        elif direction is not None:
            cut_prob, hold_prob, hike_prob = _classify_meeting_probs(
                probs, target_lower, target_upper
            )
            if direction == "cut":
                prob = cut_prob
            elif direction == "hike":
                prob = hike_prob
            elif direction == "hold":
                prob = hold_prob
            else:
                return None, None

            source_tag = (f"fedwatch:{data_source}:{direction}"
                          f":meeting={best_meeting['date']}")

        else:
            return None, None

        if prob is None:
            return None, None

        # Safety clamp to [0.02, 0.98] — consistent with all other sources
        prob = max(0.02, min(0.98, prob))

        # ── Logging ──
        cut_p, hold_p, hike_p = _classify_meeting_probs(
            probs, target_lower, target_upper
        )
        print(f"[fedwatch] Current rate: {current_rate:.2f}%, "
              f"target meeting: {best_meeting['date']}, "
              f"hold={hold_p:.0%} cut={cut_p:.0%} hike={hike_p:.0%} "
              f"-> prob={prob:.3f} (source={data_source})")

        return prob, source_tag

    except Exception as exc:
        print(f"[fedwatch] Error: {type(exc).__name__}: {exc}")
        return None, None
