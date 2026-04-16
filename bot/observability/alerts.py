"""Telegram alerting for critical bot events.

Sends alerts via Telegram Bot API for:
- Circuit breaker fires
- Unusual losses (> threshold)
- Source outages
- Self-modifier promotions
- Daily performance summary
"""

from __future__ import annotations

import os
import requests

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def send_alert(message: str, level: str = "info") -> bool:
    """Send a Telegram notification. Returns True if sent successfully.
    Silently no-ops if credentials not configured."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    prefix = {"critical": "\U0001f6a8", "warning": "\u26a0\ufe0f", "info": "\u2139\ufe0f"}.get(level, "\U0001f4ca")
    text = f"{prefix} Kalshi Bot: {message}"
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=5,
        )
        return resp.status_code == 200
    except Exception:
        return False


def alert_circuit_breaker(reason: str):
    send_alert(f"CIRCUIT BREAKER: {reason}", "critical")

def alert_large_loss(amount_cents: int, ticker: str):
    send_alert(f"Large loss: ${amount_cents/100:.2f} on {ticker}", "warning")

def alert_self_modification(param: str, old_val, new_val, reason: str):
    send_alert(f"Self-mod: {param} {old_val}\u2192{new_val} ({reason})", "info")

def alert_daily_summary(stats: dict):
    pnl = stats.get("daily_pnl_cents", 0)
    trades = stats.get("trades", 0)
    win_rate = stats.get("win_rate", 0)
    send_alert(f"Daily: P&L=${pnl/100:.2f} | {trades} trades | {win_rate:.0%} WR", "info")
