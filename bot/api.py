"""Kalshi API authentication, HTTP helpers, and rate limiting.

Extracted from trade.py. All API communication goes through this module.
"""

from __future__ import annotations

import base64
import time
from urllib.parse import urlparse

import requests

from bot.config import KEY_ID, KEY_PATH, HOST, RATE_LIMITS

# Lazy import cryptography — only needed for Kalshi API auth, not for data source HTTP
_hashes = _serialization = _padding = None

def _ensure_crypto():
    global _hashes, _serialization, _padding
    if _hashes is None:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        _hashes, _serialization, _padding = hashes, serialization, padding


# ══════════════════════════════════════════════════════════════════════════════
# RSA-PSS AUTH
# ══════════════════════════════════════════════════════════════════════════════

_PRIVATE_KEY = None


def _get_private_key():
    global _PRIVATE_KEY
    if _PRIVATE_KEY is None:
        _ensure_crypto()
        with open(KEY_PATH, "rb") as f:
            _PRIVATE_KEY = _serialization.load_pem_private_key(f.read(), password=None)
    return _PRIVATE_KEY


def _sign(method: str, path: str) -> dict:
    _ensure_crypto()
    ts_ms = str(int(time.time() * 1000))
    msg = (ts_ms + method.upper() + path).encode()
    pk = _get_private_key()
    sig = pk.sign(
        msg,
        _padding.PSS(mgf=_padding.MGF1(_hashes.SHA256()), salt_length=_padding.PSS.MAX_LENGTH),
        _hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts_ms,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "Content-Type": "application/json",
    }


def api_get(path: str) -> dict:
    full = "/trade-api/v2" + path
    sign_path = full.split("?")[0]
    rate_limit_wait(HOST + full)
    r = requests.get(HOST + full, headers=_sign("GET", sign_path), timeout=15)
    r.raise_for_status()
    return r.json()


def api_post(path: str, body: dict) -> dict:
    full = "/trade-api/v2" + path
    rate_limit_wait(HOST + full)
    r = requests.post(HOST + full, headers=_sign("POST", full), json=body, timeout=15)
    if r.status_code >= 400:
        try:
            detail = r.json()
        except Exception:
            detail = r.text[:300]
        print(f"[api_post] {path} → HTTP {r.status_code}: {detail}")
        print(f"[api_post] request body: {body}")
    r.raise_for_status()
    return r.json()


def api_delete(path: str):
    full = "/trade-api/v2" + path
    rate_limit_wait(HOST + full)
    r = requests.delete(HOST + full, headers=_sign("DELETE", full), timeout=15)
    if r.status_code not in (200, 204):
        print(f"[api_delete] {path} → HTTP {r.status_code}")
    return r


def get_portfolio() -> tuple[int, int]:
    resp = api_get("/portfolio/balance")
    return resp.get("balance", 0), resp.get("portfolio_value", 0)


# ══════════════════════════════════════════════════════════════════════════════
# Rate limiter
# ══════════════════════════════════════════════════════════════════════════════

_RATE_HISTORY: dict[str, list[float]] = {}


def rate_limit_wait(url: str) -> None:
    """Enforce per-domain rate limiting. Blocks until safe to request."""
    domain = urlparse(url).hostname or ""

    matched_key = None
    for key in RATE_LIMITS:
        if key in domain:
            matched_key = key
            break

    if not matched_key:
        return

    min_interval, max_burst = RATE_LIMITS[matched_key]
    now = time.time()

    if matched_key not in _RATE_HISTORY:
        _RATE_HISTORY[matched_key] = []

    history = _RATE_HISTORY[matched_key]
    window = max_burst * min_interval
    history[:] = [t for t in history if now - t < window]

    if len(history) >= max_burst:
        wait_until = history[0] + window
        sleep_time = wait_until - now
        if sleep_time > 0:
            time.sleep(sleep_time)
    elif history:
        time_since_last = now - history[-1]
        if time_since_last < min_interval:
            time.sleep(min_interval - time_since_last)

    _RATE_HISTORY[matched_key].append(time.time())


# ══════════════════════════════════════════════════════════════════════════════
# HTTP helpers for external data sources
# ══════════════════════════════════════════════════════════════════════════════

_CACHE: dict[str, tuple] = {}
CACHE_TTL = 60  # seconds

_DEFAULT_HEADERS = {
    "User-Agent": "KalshiTradingBot/1.0 (contact: bot@example.com)",
    "Accept": "application/json",
}


def cached_get(key: str, url: str, timeout: int = 5, headers: dict = None):
    """GET with in-memory cache, per-domain rate limiting, and retry on transient errors."""
    now = time.time()
    if key in _CACHE and now - _CACHE[key][1] < CACHE_TTL:
        return _CACHE[key][0]
    if not url:
        return None
    max_retries = 2
    for attempt in range(max_retries + 1):
        try:
            rate_limit_wait(url)
            hdrs = {**_DEFAULT_HEADERS, **(headers or {})}
            r = requests.get(url, timeout=timeout, headers=hdrs)
            if r.status_code in (500, 502, 503) and attempt < max_retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            if r.status_code != 200:
                print(f"[http] {key} → HTTP {r.status_code} from {url.split('?')[0]}")
                return None
            data = r.json()
            _CACHE[key] = (data, now)
            return data
        except Exception as e:
            if attempt < max_retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            print(f"[http] {key} → {type(e).__name__}: {e}")
            return None
    return None
