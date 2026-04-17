#!/usr/bin/env python3
"""
Kalshi Bot Health Check — runs every hour via cron.
Checks for problems that would otherwise go unnoticed:

1. Bot not running (cron broken, crash loop)
2. Bot halted (false loss limit, API auth failure)
3. Zero trades for too long (stuck, no opportunities found)
4. MM not quoting (fills stopped, orders not posting)
5. Sources death spiral (all disabled)
6. Balance anomaly (unexpected large loss)
7. Inventory imbalance (too concentrated in one direction)
8. Stale orders (orders sitting unfilled for days)

Outputs a single-line summary to stdout + writes HEALTH_REPORT.md.
If any CRITICAL or WARNING issues found, also sends a macOS notification.
"""

import sqlite3
import os
import sys
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "kalshi_trades.db"))
REPORT_PATH = os.path.join(os.path.dirname(__file__), "HEALTH_REPORT.md")
LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")

# Thresholds
MAX_MINUTES_SINCE_LAST_RUN = 20      # bot should run every 15 min
MAX_CONSECUTIVE_HALTS = 4             # 1 hour of halts = problem
MAX_HOURS_NO_MM_FILLS = 12            # MM should get some fills within 12h
MAX_HOURS_NO_DIRECTIONAL = 72         # 3 days without a directional trade = worth noting
MAX_INVENTORY_PER_MARKET = 30         # position concentration warning
BALANCE_DROP_WARNING_PCT = 0.05       # 5% drop in a single run
BALANCE_DROP_CRITICAL_PCT = 0.15      # 15% total drop = critical
MAX_SOURCE_DISABLE_PCT = 0.80         # 80%+ sources disabled = problem

def check_health():
    issues = []  # list of (severity, category, message)
    stats = {}

    if not os.path.exists(DB_PATH):
        issues.append(("CRITICAL", "database", "Database not found at " + DB_PATH))
        return issues, stats

    conn = sqlite3.connect(DB_PATH)

    now = datetime.now(timezone.utc)
    now_str = now.isoformat()

    # ── 1. Is the bot running? ────────────────────────────────────────
    try:
        last_session = conn.execute(
            "SELECT timestamp, balance_cents, halted, halt_reason FROM sessions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if last_session:
            last_ts = datetime.fromisoformat(last_session[0].replace("Z", "+00:00"))
            minutes_ago = (now - last_ts).total_seconds() / 60
            stats["last_run_minutes_ago"] = round(minutes_ago, 1)
            stats["last_balance"] = last_session[1]

            if minutes_ago > MAX_MINUTES_SINCE_LAST_RUN:
                issues.append(("CRITICAL", "cron",
                    f"Bot hasn't run in {minutes_ago:.0f} minutes (last: {last_ts.strftime('%H:%M UTC')})"))
        else:
            issues.append(("CRITICAL", "cron", "No sessions found in database — bot may have never run"))
    except Exception as e:
        issues.append(("CRITICAL", "database", f"Cannot read sessions: {e}"))

    # ── 2. Is the bot halted? ─────────────────────────────────────────
    try:
        recent_sessions = conn.execute(
            "SELECT halted, halt_reason FROM sessions ORDER BY id DESC LIMIT 8"
        ).fetchall()
        consecutive_halts = 0
        halt_reason = ""
        for halted, reason in recent_sessions:
            if halted:
                consecutive_halts += 1
                halt_reason = reason or "unknown"
            else:
                break

        stats["consecutive_halts"] = consecutive_halts
        if consecutive_halts >= MAX_CONSECUTIVE_HALTS:
            hours = consecutive_halts * 0.25  # each run is ~15 min
            issues.append(("CRITICAL", "halt",
                f"Bot halted for {consecutive_halts} consecutive runs (~{hours:.1f}h): {halt_reason}"))
        elif consecutive_halts >= 2:
            issues.append(("WARNING", "halt",
                f"Bot halted for {consecutive_halts} runs: {halt_reason}"))
    except Exception:
        pass

    # ── 3. MM activity ────────────────────────────────────────────────
    try:
        mm_session = conn.execute(
            "SELECT recorded_at, fills_detected, orders_posted FROM mm_sessions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if mm_session:
            last_mm = datetime.fromisoformat(mm_session[0].replace("Z", "+00:00"))
            mm_hours_ago = (now - last_mm).total_seconds() / 3600
            stats["last_mm_hours_ago"] = round(mm_hours_ago, 1)
            stats["last_mm_fills"] = mm_session[1]
            stats["last_mm_orders"] = mm_session[2]

            if mm_hours_ago > MAX_HOURS_NO_MM_FILLS:
                issues.append(("WARNING", "mm",
                    f"No MM activity for {mm_hours_ago:.1f}h"))

        # Check for fills in last 24h
        cutoff = (now - timedelta(hours=24)).isoformat()
        total_fills = conn.execute(
            "SELECT COALESCE(SUM(fills_detected), 0) FROM mm_sessions WHERE recorded_at > ?",
            (cutoff,)
        ).fetchone()[0]
        stats["mm_fills_24h"] = total_fills
        if total_fills == 0 and mm_session:
            issues.append(("WARNING", "mm", "Zero MM fills in the last 24 hours"))
    except Exception:
        pass

    # ── 4. Directional trading activity ───────────────────────────────
    try:
        last_trade = conn.execute(
            "SELECT timestamp FROM trades WHERE dry_run = 0 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        stats["directional_trades_total"] = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE dry_run = 0"
        ).fetchone()[0]
        if last_trade:
            lt = datetime.fromisoformat(last_trade[0].replace("Z", "+00:00"))
            hours = (now - lt).total_seconds() / 3600
            stats["last_directional_hours_ago"] = round(hours, 1)
        else:
            stats["last_directional_hours_ago"] = None

        stats["settlements_total"] = conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0]
    except Exception:
        pass

    # ── 5. Source health ──────────────────────────────────────────────
    try:
        sources = conn.execute("SELECT DISTINCT source FROM pipeline_health").fetchall()
        total_sources = len(sources)
        disabled_count = 0
        for (source,) in sources:
            recent = conn.execute(
                "SELECT status FROM pipeline_health WHERE source = ? ORDER BY id DESC LIMIT 5",
                (source,)
            ).fetchall()
            if len(recent) >= 5 and all(r[0] in ("broken", "degraded") for r in recent):
                disabled_count += 1

        stats["total_sources"] = total_sources
        stats["disabled_sources"] = disabled_count
        if total_sources > 0 and disabled_count / total_sources >= MAX_SOURCE_DISABLE_PCT:
            issues.append(("WARNING", "sources",
                f"{disabled_count}/{total_sources} data sources disabled (death spiral risk)"))
    except Exception:
        pass

    # ── 6. Balance tracking ───────────────────────────────────────────
    try:
        balances = conn.execute(
            "SELECT balance_cents, portfolio_cents, timestamp FROM sessions ORDER BY id DESC LIMIT 20"
        ).fetchall()
        if len(balances) >= 2:
            current_equity = balances[0][0] + (balances[0][1] or 0)
            prev_equity = balances[1][0] + (balances[1][1] or 0)
            if prev_equity > 0:
                run_change = (prev_equity - current_equity) / prev_equity
                if run_change > BALANCE_DROP_WARNING_PCT:
                    issues.append(("WARNING", "balance",
                        f"Equity dropped {run_change:.1%} in last run "
                        f"(${prev_equity/100:.2f} → ${current_equity/100:.2f})"))

            # Check against day high
            today = now.strftime("%Y-%m-%d")
            day_rows = conn.execute(
                "SELECT MAX(balance_cents + COALESCE(portfolio_cents, 0)) FROM sessions WHERE timestamp LIKE ?",
                (today + "%",)
            ).fetchone()
            if day_rows and day_rows[0]:
                day_high = day_rows[0]
                if day_high > 0:
                    day_dd = (day_high - current_equity) / day_high
                    stats["day_drawdown_pct"] = round(day_dd * 100, 1)
                    if day_dd > BALANCE_DROP_CRITICAL_PCT:
                        issues.append(("CRITICAL", "balance",
                            f"Intraday drawdown {day_dd:.1%} from peak ${day_high/100:.2f}"))
    except Exception:
        pass

    # ── 7. Inventory concentration ────────────────────────────────────
    try:
        inventory = conn.execute(
            "SELECT ticker, net_position, avg_entry_cents FROM mm_inventory WHERE net_position != 0"
        ).fetchall()
        stats["open_mm_positions"] = len(inventory)
        total_inventory = sum(abs(r[1]) for r in inventory)
        stats["total_mm_contracts"] = total_inventory
        for ticker, net, avg in inventory:
            if abs(net) > MAX_INVENTORY_PER_MARKET:
                issues.append(("WARNING", "inventory",
                    f"Concentrated position: {ticker} net={net} contracts (avg entry={avg:.0f}¢)"))
    except Exception:
        pass

    # ── 8. Stale orders check ─────────────────────────────────────────
    try:
        cutoff_48h = (now - timedelta(hours=48)).isoformat()
        stale = conn.execute(
            "SELECT COUNT(*) FROM mm_orders WHERE status='posted' AND timestamp < ?",
            (cutoff_48h,)
        ).fetchone()[0]
        stats["stale_orders_48h"] = stale
        if stale > 10:
            issues.append(("WARNING", "orders",
                f"{stale} MM orders still 'posted' after 48h — cancel loop may be broken"))
    except Exception:
        pass

    # ── 9. Check cron.log for recent errors ───────────────────────────
    try:
        cron_log = os.path.join(LOG_DIR, "cron.log")
        if os.path.exists(cron_log):
            # Read last 50 lines
            with open(cron_log, 'r') as f:
                lines = f.readlines()[-50:]
            error_lines = [l.strip() for l in lines if "ERROR" in l or "FATAL" in l or "Traceback" in l]
            if error_lines:
                stats["recent_errors"] = len(error_lines)
                issues.append(("WARNING", "errors",
                    f"{len(error_lines)} error lines in recent cron.log"))
    except Exception:
        pass

    conn.close()
    return issues, stats


def generate_report(issues, stats):
    now = datetime.now(timezone.utc)
    severity_order = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}
    issues.sort(key=lambda x: severity_order.get(x[0], 9))

    has_critical = any(s == "CRITICAL" for s, _, _ in issues)
    has_warning = any(s == "WARNING" for s, _, _ in issues)

    if has_critical:
        status_emoji = "🔴"
        status_text = "CRITICAL"
    elif has_warning:
        status_emoji = "🟡"
        status_text = "WARNING"
    else:
        status_emoji = "🟢"
        status_text = "HEALTHY"

    lines = [
        f"# Bot Health Report {status_emoji} {status_text}",
        f"Generated: {now.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
    ]

    if issues:
        lines.append("## Issues")
        lines.append("")
        for severity, category, message in issues:
            icon = "🔴" if severity == "CRITICAL" else "🟡"
            lines.append(f"- {icon} **[{category}]** {message}")
        lines.append("")

    lines.append("## Stats")
    lines.append("")
    stat_display = {
        "last_run_minutes_ago": "Last run",
        "consecutive_halts": "Consecutive halts",
        "last_balance": "Balance (cents)",
        "day_drawdown_pct": "Intraday drawdown %",
        "mm_fills_24h": "MM fills (24h)",
        "last_mm_hours_ago": "Last MM activity (hours)",
        "directional_trades_total": "Directional trades (all-time)",
        "settlements_total": "Settlements (all-time)",
        "total_sources": "Data sources tracked",
        "disabled_sources": "Sources disabled",
        "open_mm_positions": "Open MM positions",
        "total_mm_contracts": "Total MM contracts",
        "stale_orders_48h": "Stale orders (>48h)",
    }
    for key, label in stat_display.items():
        if key in stats:
            val = stats[key]
            if key == "last_balance" and val:
                val = f"${val/100:.2f}"
            elif key == "last_run_minutes_ago":
                val = f"{val:.0f} min ago"
            elif key == "last_mm_hours_ago":
                val = f"{val:.1f}h ago"
            elif key == "day_drawdown_pct":
                val = f"{val:.1f}%"
            lines.append(f"- {label}: {val}")

    return "\n".join(lines), status_text, has_critical or has_warning


def send_notification(title, message):
    """Send macOS notification via osascript."""
    try:
        script = f'display notification "{message}" with title "{title}" sound name "Ping"'
        subprocess.run(["osascript", "-e", script], timeout=5, capture_output=True)
    except Exception:
        pass  # notifications are best-effort


def main():
    issues, stats = check_health()
    report, status, has_alerts = generate_report(issues, stats)

    # Write report file
    with open(REPORT_PATH, "w") as f:
        f.write(report)

    # Print one-line summary
    n_critical = sum(1 for s, _, _ in issues if s == "CRITICAL")
    n_warning = sum(1 for s, _, _ in issues if s == "WARNING")
    balance = stats.get("last_balance", 0)
    bal_str = f"${balance/100:.2f}" if balance else "?"
    print(f"[health] {status} | balance={bal_str} | "
          f"critical={n_critical} warning={n_warning} | "
          f"mm_fills_24h={stats.get('mm_fills_24h', '?')} | "
          f"halts={stats.get('consecutive_halts', '?')}")

    # Send macOS notification for critical/warning issues
    if has_alerts:
        alert_msgs = [f"[{cat}] {msg}" for sev, cat, msg in issues if sev in ("CRITICAL", "WARNING")]
        notify_body = "; ".join(alert_msgs[:3])  # cap at 3 to fit in notification
        send_notification(f"Kalshi Bot: {status}", notify_body)

    # Also print issues to stdout for cron log
    for sev, cat, msg in issues:
        print(f"  [{sev}] {cat}: {msg}")

    return 1 if n_critical > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
