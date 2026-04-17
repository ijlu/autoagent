"""Market categorization + series prefix extraction.

Extracted from bot/market_maker/selection.py and bot/market_maker/series_profitability.py
so that non-MM modules (arbitrage/, learning/) can use these utilities without
pulling in the deleted market_maker package.

Functions exposed:
  - categorize_market(ticker, title) -> str
  - _get_series_prefix(ticker) -> (prefix, is_bracket)
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Category classification
# ---------------------------------------------------------------------------

CATEGORY_KEYWORDS = {
    "economics": ["cpi", "inflation", "unemployment", "gdp", "nonfarm", "payroll",
                  "fed funds", "fomc", "interest rate", "jobs report",
                  "federal funds", "fed rate", "kxfed", "kxcpi", "kxgdp", "kxjob", "kxunrate"],
    "crypto":    ["btc", "bitcoin", "eth", "ether", "sol", "solana", "crypto", "coin"],
    "weather":   ["temperature", "temp", "weather", "degrees", "\u00b0f", "\u00b0c", "heat", "cold", "freeze",
                  "kxhigh", "kxhmonth", "kxhurr", "highest temperature", "nws"],
    "sports":    ["nba", "nfl", "mlb", "nhl", "ncaa", "mls", "epl", "nascar", "championship",
                  "playoff", "stanley cup", "finals", "world series"],
    "company":   ["deliveries", "production", "subscribers", "revenue", "earnings",
                  "daily active", "monthly active", "dau", "mau", "users",
                  "headcount", "total orders", "total rides", "total payers",
                  "total customers", "shipments", "bookings", "trips",
                  "gold sub", "semi truck", "ipo",
                  "tesla", "kxteslasemi", "boeing", "kxboeing",
                  "netflix", "kxearningsmentionnflx",
                  "meta", "kxmetaheadcount",
                  "spotify", "kxspotifymau", "uber", "kxubertrips",
                  "robinhood", "kxhood", "doordash", "kxdashorders",
                  "lyft", "kxlyft", "match group", "kxmtch",
                  "palantir", "kxpltr", "ferrari", "kxrace",
                  "philip morris", "zyn", "kxpm",
                  "airbnb", "kxabnb", "kxstripeipo", "kxismpmi",
                  "apple", "google", "alphabet", "amazon", "microsoft", "nvidia"],
}

_COMPANY_PREFIXES = [
    "kxboeing", "kxspotifymau", "kxubertrips", "kxmetaheadcount",
    "kxhood", "kxdashorders", "kxlyft", "kxmtch", "kxpltr",
    "kxrace", "kxpm", "kxabnb", "kxteslasemi", "kxismpmi",
    "kxearningsmention", "kxearningmention", "kxstripeipo",
]


def categorize_market(ticker, title):
    """Assign a market to a risk category based on ticker and title.

    Company tickers get priority -- e.g. KXEARNINGSMENTIONNFLX-26APR16-MLB
    should be 'company' not 'sports' despite containing 'mlb'.
    """
    text = (ticker + " " + title).lower()
    ticker_lower = ticker.lower()

    # Priority check: company ticker prefixes always win
    if any(ticker_lower.startswith(p) for p in _COMPANY_PREFIXES):
        return "company"

    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return category
    return "other"


# ---------------------------------------------------------------------------
# Series prefix extraction
# ---------------------------------------------------------------------------

def _get_series_prefix(ticker):
    """Extract the series prefix from a bracket/threshold ticker.

    KXHIGHDEN-26APR09-B69.5 -> ("KXHIGHDEN-26APR09", True)
    KXFED-27APR-T2.50       -> ("KXFED-27APR", False)

    Returns (prefix, is_bracket). Bracket markets (-B) have mutually exclusive
    outcomes; threshold markets (-T) do not.
    """
    parts = ticker.rsplit("-", 1)
    if len(parts) == 2 and parts[1] and parts[1][0] in ("B", "T"):
        is_bracket = parts[1][0] == "B"
        return parts[0], is_bracket
    # Not a bracket/threshold ticker
    return ticker, False
