"""Company KPI and SensorTower app intelligence signal sources.

Analyst estimates (Finnhub) for deliveries, revenue, subscribers, plus
SensorTower app download/usage data as a proxy for company KPIs.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone

import requests

from bot.config import FINNHUB_KEY, SENSORTOWER_TOKEN


# ══════════════════════════════════════════════════════════════════════════════
# Company KPI — analyst estimates for deliveries, revenue, subscribers
# ══════════════════════════════════════════════════════════════════════════════

_COMPANY_KPI_CACHE = {}  # {symbol: (data, timestamp)}

# Map Kalshi series prefixes to company symbols and KPI types
_KPI_TICKER_MAP = {
    # Verified active Kalshi series (2026-04-07)
    "KXBOEING":        {"symbol": "BA",   "kpi": "deliveries",   "unit": "aircraft",     "scale": 1},
    "KXSPOTIFYMAU":    {"symbol": "SPOT", "kpi": "mau",          "unit": "users",        "scale": 1_000_000},
    "KXUBERTRIPS":     {"symbol": "UBER", "kpi": "trips",        "unit": "trips",        "scale": 1_000_000_000},
    "KXMETAHEADCOUNT": {"symbol": "META", "kpi": "headcount",    "unit": "employees",    "scale": 1},
    "KXHOOD":          {"symbol": "HOOD", "kpi": "subscribers",  "unit": "subscribers",  "scale": 1_000_000},
    "KXDASHORDERS":    {"symbol": "DASH", "kpi": "orders",       "unit": "orders",       "scale": 1_000_000},
    "KXLYFT":          {"symbol": "LYFT", "kpi": "rides",        "unit": "rides",        "scale": 1_000_000},
    "KXMTCH":          {"symbol": "MTCH", "kpi": "payers",       "unit": "payers",       "scale": 1_000_000},
    "KXPLTR":          {"symbol": "PLTR", "kpi": "customers",    "unit": "customers",    "scale": 1},
    "KXRACE":          {"symbol": "RACE", "kpi": "shipments",    "unit": "vehicles",     "scale": 1},
    "KXPM":            {"symbol": "PM",   "kpi": "shipments",    "unit": "cans",         "scale": 1_000_000},
    "KXABNB":          {"symbol": "ABNB", "kpi": "bookings",     "unit": "nights",       "scale": 1_000_000},
    "KXTESLASEMI":     {"symbol": "TSLA", "kpi": "production",   "unit": "trucks",       "scale": 1},
    "KXISMPMI":        {"symbol": "ISM",  "kpi": "pmi",          "unit": "index",        "scale": 1},
}


def get_company_kpi_estimate(ticker, market_data):
    """Estimate probability for company KPI markets (deliveries, revenue, subscribers).

    Uses Finnhub analyst estimates + title parsing to extract threshold and direction,
    then estimates probability based on consensus vs. threshold distance.
    Falls back to news sentiment scoring for earnings-mention markets.
    """
    if not FINNHUB_KEY:
        return None, None

    title = (market_data.get("title") or market_data.get("subtitle") or "").lower()
    ticker_upper = ticker.upper()

    # Identify which company KPI this market is about
    kpi_info = None
    for prefix, info in _KPI_TICKER_MAP.items():
        if prefix in ticker_upper:
            kpi_info = info
            break

    # For earnings-mention markets, delegate to news sentiment (already handled)
    if "earningmention" in ticker_upper.replace("_", "").lower() or \
       "earningsMention" in ticker or "earnings mention" in title:
        return None, None  # Let finnhub/LLM handle these

    if not kpi_info:
        # Try to detect from title
        title_lower = title.lower()
        for company, sym in [("tesla", "TSLA"), ("netflix", "NFLX"), ("meta", "META"),
                              ("apple", "AAPL"), ("google", "GOOGL"), ("alphabet", "GOOGL"),
                              ("amazon", "AMZN"), ("microsoft", "MSFT"), ("nvidia", "NVDA")]:
            if company in title_lower:
                kpi = "revenue"  # default guess
                if "deliver" in title_lower: kpi = "deliveries"
                elif "produc" in title_lower: kpi = "production"
                elif "subscrib" in title_lower: kpi = "subscribers"
                elif "active" in title_lower or "dau" in title_lower or "mau" in title_lower: kpi = "dau"
                kpi_info = {"symbol": sym, "kpi": kpi, "unit": "units", "scale": 1}
                break

    if not kpi_info:
        return None, None

    symbol = kpi_info["symbol"]

    # Extract threshold from title: "above 500,000" / "at or above $90B" / etc.
    thresh_match = re.search(
        r'(at or above|at or below|above|below|over|under|at least|exceed|less than|more than|fewer than)'
        r'\s+\$?([\d,]+\.?\d*)\s*(k|m|b|billion|million|thousand)?',
        title
    )
    if not thresh_match:
        # Try ticker-based threshold: KXTESLA-26-Q1-T500000
        tick_match = re.search(r'-T([\d,]+\.?\d*)', ticker)
        if tick_match:
            threshold = float(tick_match.group(1).replace(",", ""))
            is_above = True  # default
        else:
            return None, None
    else:
        direction = thresh_match.group(1)
        raw_val = float(thresh_match.group(2).replace(",", ""))
        suffix = (thresh_match.group(3) or "").lower()
        multipliers = {"k": 1_000, "thousand": 1_000, "m": 1_000_000,
                       "million": 1_000_000, "b": 1_000_000_000, "billion": 1_000_000_000}
        threshold = raw_val * multipliers.get(suffix, 1)
        is_above = direction in ("above", "over", "at least", "exceed", "more than", "at or above")

    # Fetch analyst estimate from Finnhub
    now = time.time()
    cache_key = f"{symbol}_{kpi_info['kpi']}"
    if cache_key in _COMPANY_KPI_CACHE and now - _COMPANY_KPI_CACHE[cache_key][1] < 3600:
        estimate_data = _COMPANY_KPI_CACHE[cache_key][0]
    else:
        try:
            # Try Finnhub earnings estimates for revenue
            if kpi_info["kpi"] == "revenue":
                url = f"https://finnhub.io/api/v1/stock/revenue-estimate?symbol={symbol}&token={FINNHUB_KEY}"
            else:
                # For non-revenue KPIs, use EPS estimates as a proxy signal
                url = f"https://finnhub.io/api/v1/stock/eps-estimate?symbol={symbol}&token={FINNHUB_KEY}"
            resp = requests.get(url, timeout=5)
            estimate_data = resp.json() if resp.status_code == 200 else None
            _COMPANY_KPI_CACHE[cache_key] = (estimate_data, now)
        except Exception:
            return None, None

    if not estimate_data:
        return None, None

    # Extract consensus estimate
    consensus = None
    try:
        data_list = estimate_data.get("data", [])
        if data_list:
            latest = data_list[0]  # most recent quarter
            if kpi_info["kpi"] == "revenue":
                consensus = latest.get("revenueAvg") or latest.get("revenueHigh")
            else:
                consensus = latest.get("epsAvg") or latest.get("epsHigh")
    except Exception:
        pass

    if consensus is None:
        return None, None

    # For deliveries/subscribers, Finnhub doesn't have direct data.
    # Use revenue consensus as a directional signal (correlated).
    # For revenue markets, compare directly.
    if kpi_info["kpi"] in ("deliveries", "production", "subscribers", "dau"):
        # We have revenue estimate but need deliveries -- use as weak signal
        # Just return None and let LLM handle these for now
        # TODO: Add SensorTower/alternative data sources for app metrics
        return None, None

    # Compare consensus to threshold
    if consensus and threshold:
        # How far is consensus from threshold, as a fraction of threshold
        ratio = consensus / threshold if threshold != 0 else 1.0

        if is_above:
            # P(above threshold) -- higher ratio = more likely above
            if ratio > 1.15:
                prob = 0.85
            elif ratio > 1.05:
                prob = 0.70
            elif ratio > 1.0:
                prob = 0.58
            elif ratio > 0.95:
                prob = 0.42
            elif ratio > 0.85:
                prob = 0.30
            else:
                prob = 0.15
        else:
            # P(below threshold) -- lower ratio = more likely below
            if ratio < 0.85:
                prob = 0.85
            elif ratio < 0.95:
                prob = 0.70
            elif ratio < 1.0:
                prob = 0.58
            elif ratio < 1.05:
                prob = 0.42
            elif ratio < 1.15:
                prob = 0.30
            else:
                prob = 0.15

        src_desc = f"analyst:{symbol}={consensus:.1f} vs {threshold:.0f}"
        print(f"[info] Company KPI: {symbol} {kpi_info['kpi']} consensus={consensus:.1f} "
              f"threshold={threshold:.0f} {'above' if is_above else 'below'} -> {prob:.2f}")
        return prob, src_desc

    return None, None


# ══════════════════════════════════════════════════════════════════════════════
# SensorTower — app intelligence for subscriber/DAU/download markets
# ══════════════════════════════════════════════════════════════════════════════

_ST_RATE_LIMIT = {
    "calls_today": 0,
    "last_reset": None,
    "max_per_day": 40,       # 5000/month / 30 = ~166/day, but stay well under
    "cache": {},             # {app_id: (data, timestamp)}
    "cache_ttl": 3600,       # cache for 1h -- 24h was too stale for live trading
}

# Map companies to SensorTower unified app IDs (iOS App Store IDs)
# These are real App Store IDs for the primary iOS apps
_ST_APP_MAP = {
    # Map stock symbols to iOS App Store IDs for SensorTower queries
    "SPOT":  {"app_id": "324684580",  "name": "Spotify",     "platform": "ios"},
    "UBER":  {"app_id": "368677368",  "name": "Uber",        "platform": "ios"},
    "META":  {"app_id": "284882215",  "name": "Facebook",    "platform": "ios"},
    "HOOD":  {"app_id": "1326124521", "name": "Robinhood",   "platform": "ios"},
    "DASH":  {"app_id": "719972451",  "name": "DoorDash",    "platform": "ios"},
    "LYFT":  {"app_id": "529379082",  "name": "Lyft",        "platform": "ios"},
    "MTCH":  {"app_id": "547702041",  "name": "Tinder",      "platform": "ios"},
    "ABNB":  {"app_id": "401626263",  "name": "Airbnb",      "platform": "ios"},
    "TSLA":  {"app_id": "582007913",  "name": "Tesla",       "platform": "ios"},
    "PLTR":  {"app_id": "1546484855", "name": "Palantir AIP","platform": "ios"},
}


def get_sensortower_estimate(ticker, market_data):
    """Use SensorTower app download/usage data to estimate company KPI probabilities.

    Useful for:
    - Netflix subscriber markets (KXNFLX) -- app downloads correlate with subscriber growth
    - Meta DAU markets (KXMETADAP) -- app DAU directly measures this
    - Tesla delivery markets (KXTESLA) -- Tesla app downloads correlate with deliveries

    Rate limiting: max 40 API calls/day (5000/month budget, conservatively throttled).
    Results cached for 24h since app metrics don't change rapidly.
    """
    if not SENSORTOWER_TOKEN:
        return None, None

    title = (market_data.get("title") or "").lower()
    ticker_upper = ticker.upper()

    # Identify the company and relevant app
    target_symbol = None
    for prefix, info in _KPI_TICKER_MAP.items():
        if prefix in ticker_upper:
            target_symbol = info["symbol"]
            break

    if not target_symbol or target_symbol not in _ST_APP_MAP:
        return None, None

    app_info = _ST_APP_MAP[target_symbol]

    # Rate limiting -- reset daily counter
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _ST_RATE_LIMIT["last_reset"] != today:
        _ST_RATE_LIMIT["calls_today"] = 0
        _ST_RATE_LIMIT["last_reset"] = today

    # Check cache first
    cache_key = f"{app_info['app_id']}_{app_info['platform']}"
    now = time.time()
    if cache_key in _ST_RATE_LIMIT["cache"]:
        cached_data, cached_at = _ST_RATE_LIMIT["cache"][cache_key]
        if now - cached_at < _ST_RATE_LIMIT["cache_ttl"]:
            app_data = cached_data
        else:
            app_data = None
    else:
        app_data = None

    if app_data is None:
        # Check rate limit before making API call
        if _ST_RATE_LIMIT["calls_today"] >= _ST_RATE_LIMIT["max_per_day"]:
            print(f"[sensortower] Rate limit reached ({_ST_RATE_LIMIT['calls_today']}/{_ST_RATE_LIMIT['max_per_day']} today), skipping")
            return None, None

        try:
            # SensorTower sales report estimates endpoint
            # Fetches download & revenue estimates for the last 30 days
            end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            start_date = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")

            headers = {
                "Authorization": f"Bearer {SENSORTOWER_TOKEN}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }

            # Try the sales report estimates endpoint
            url = (f"https://api.sensortower.com/v1/{app_info['platform']}"
                   f"/sales_report_estimates"
                   f"?app_ids={app_info['app_id']}"
                   f"&start_date={start_date}&end_date={end_date}"
                   f"&countries=US&date_granularity=monthly")

            resp = requests.get(url, headers=headers, timeout=10)
            _ST_RATE_LIMIT["calls_today"] += 1

            if resp.status_code == 200:
                app_data = resp.json()
                _ST_RATE_LIMIT["cache"][cache_key] = (app_data, now)
                print(f"[sensortower] Fetched data for {app_info['name']} "
                      f"(call {_ST_RATE_LIMIT['calls_today']}/{_ST_RATE_LIMIT['max_per_day']} today)")
            elif resp.status_code == 401:
                print(f"[sensortower] Auth failed (401) -- check SENSORTOWER_API_TOKEN")
                return None, None
            elif resp.status_code == 429:
                print(f"[sensortower] Rate limited by API (429)")
                return None, None
            else:
                print(f"[sensortower] API returned {resp.status_code} for {app_info['name']}")
                # Try alternative endpoint format
                url_alt = (f"https://api.sensortower.com/v1/{app_info['platform']}"
                           f"/sales_report_estimates_comparison_attributes"
                           f"?app_ids={app_info['app_id']}"
                           f"&start_date={start_date}&end_date={end_date}"
                           f"&countries=US&date_granularity=monthly")
                resp_alt = requests.get(url_alt, headers=headers, timeout=10)
                _ST_RATE_LIMIT["calls_today"] += 1
                if resp_alt.status_code == 200:
                    app_data = resp_alt.json()
                    _ST_RATE_LIMIT["cache"][cache_key] = (app_data, now)
                else:
                    return None, None

        except Exception as e:
            print(f"[sensortower] Error fetching {app_info['name']}: {e}")
            return None, None

    if not app_data:
        return None, None

    # Parse the response -- extract download/revenue estimates
    try:
        # SensorTower returns list of date-bucketed estimates
        total_downloads = 0
        total_revenue = 0
        records = app_data if isinstance(app_data, list) else [app_data]
        for record in records:
            if isinstance(record, dict):
                total_downloads += record.get("units", 0) or record.get("downloads", 0) or 0
                total_revenue += record.get("revenue", 0) or 0

        if total_downloads == 0 and total_revenue == 0:
            return None, None

        # Extract threshold from title
        thresh_match = re.search(
            r'(at or above|at or below|above|below|over|under|at least|exceed|less than|more than|fewer than)'
            r'\s+\$?([\d,]+\.?\d*)\s*(k|m|b|billion|million|thousand)?',
            title
        )
        if not thresh_match:
            tick_match = re.search(r'-T([\d,]+\.?\d*)', ticker)
            if tick_match:
                threshold = float(tick_match.group(1).replace(",", ""))
                is_above = True
            else:
                # Can't determine threshold -- return download trend as signal
                src_desc = f"sensortower:{app_info['name']}=downloads:{total_downloads}"
                return None, src_desc  # No probability, just metadata
        else:
            direction = thresh_match.group(1)
            raw_val = float(thresh_match.group(2).replace(",", ""))
            suffix = (thresh_match.group(3) or "").lower()
            multipliers = {"k": 1_000, "thousand": 1_000, "m": 1_000_000,
                           "million": 1_000_000, "b": 1_000_000_000, "billion": 1_000_000_000}
            threshold = raw_val * multipliers.get(suffix, 1)
            is_above = direction in ("above", "over", "at least", "exceed", "more than", "at or above")

        # Use downloads as a proxy signal for company KPIs
        # For subscriber markets: downloads ~ new subscriber proxy
        # For delivery markets: app downloads correlate with vehicle orders
        kpi_info = None
        for prefix, info in _KPI_TICKER_MAP.items():
            if prefix in ticker_upper:
                kpi_info = info
                break

        # Compare relevant metric to threshold
        if kpi_info and kpi_info["kpi"] in ("subscribers", "dau"):
            metric = total_downloads  # downloads proxy for subscriber growth
            metric_name = "downloads_30d"
        elif kpi_info and kpi_info["kpi"] in ("deliveries", "production"):
            metric = total_downloads  # app downloads correlate with orders
            metric_name = "downloads_30d"
        elif kpi_info and kpi_info["kpi"] == "revenue":
            metric = total_revenue
            metric_name = "app_revenue_30d"
        else:
            metric = total_downloads
            metric_name = "downloads_30d"

        if metric > 0 and threshold > 0:
            ratio = metric / threshold
            # Conservative probability mapping -- app data is a proxy, not exact
            if is_above:
                if ratio > 1.3:   prob = 0.75
                elif ratio > 1.1: prob = 0.62
                elif ratio > 1.0: prob = 0.55
                elif ratio > 0.9: prob = 0.45
                elif ratio > 0.7: prob = 0.35
                else:             prob = 0.25
            else:
                if ratio < 0.7:   prob = 0.75
                elif ratio < 0.9: prob = 0.62
                elif ratio < 1.0: prob = 0.55
                elif ratio < 1.1: prob = 0.45
                elif ratio < 1.3: prob = 0.35
                else:             prob = 0.25

            src_desc = f"sensortower:{app_info['name']}={metric_name}:{metric:,.0f}"
            print(f"[info] SensorTower: {app_info['name']} {metric_name}={metric:,.0f} "
                  f"threshold={threshold:,.0f} {'above' if is_above else 'below'} -> {prob:.2f}")
            return prob, src_desc

    except Exception as e:
        print(f"[sensortower] Parse error for {app_info['name']}: {e}")

    return None, None
