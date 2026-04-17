"""Opportunity logging -- records every candidate for audit and learning.

Logs all markets that were evaluated, whether traded or rejected,
with source estimates, four-factor scores, regime, rank, and skip reason.
Post-settlement, outcomes are backfilled for rejected candidates too.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone


def log_opportunity(conn, ticker: str, action: str, details: dict):
    """Log a candidate evaluation to the opportunity_log table.

    action: 'trade', 'skip', 'filter', 'shadow'
    details: dict with source_estimates, four_factor, regime, rank, skip_reason, etc.
    """
    if conn is None:
        return
    try:
        conn.execute(
            """INSERT INTO opportunity_log
            (timestamp, ticker, action, details_json)
            VALUES (?, ?, ?, ?)""",
            (datetime.now(timezone.utc).isoformat(), ticker, action, json.dumps(details)),
        )
        conn.commit()
    except Exception:
        pass  # table might not exist yet
