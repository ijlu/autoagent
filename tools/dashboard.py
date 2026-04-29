"""Interactive HTML dashboard for the Kalshi bot.

Generates a single self-contained HTML file with Plotly visualizations.
Loads Plotly from CDN (no Python plotly dependency needed). Open the
file in any browser; charts are interactive (hover, zoom, pan).

Sections:
  1. Source health — current state of each weather source (state, σ,
     bias, recent MAE), plus state-transition history
  2. Combine quality — Brier per (city, TTE bucket) of combined_v2 vs
     market mid, sliced by recency
  3. Calibration — ensemble_p_yes vs actual win rate (reliability curve)
  4. Recent trading — decisions, skip reasons, settlements
  5. System health — scheduler / poller / cycle stats

Usage::

    python -m tools.dashboard --db /path/to/kalshi_trades.db \\
        --output reports/dashboard.html

Run on demand or wire as a daemon scheduler task. Output is plain HTML
that renders anywhere (file://, scp'd to laptop, served via simple
HTTP server).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# ── Section: source health ────────────────────────────────────────────
def _source_health_section(conn: sqlite3.Connection) -> dict:
    """Per-source state + metrics."""
    rows = conn.execute(
        """SELECT source, city, state, n_settled,
                  mae_7d, mae_30d, sigma_fitted, bias_fitted,
                  last_state_change_iso, last_evaluated_iso
             FROM weather_source_state
            ORDER BY source, city"""
    ).fetchall()

    pooled = []
    per_city = {}
    for r in rows:
        d = {
            "source": r[0], "city": r[1], "state": r[2],
            "n_settled": r[3] or 0,
            "mae_7d": r[4], "mae_30d": r[5],
            "sigma": r[6], "bias": r[7],
            "last_state_change": r[8],
            "last_evaluated": r[9],
        }
        if r[1] == "pooled":
            pooled.append(d)
        else:
            per_city.setdefault(r[0], []).append(d)

    return {"pooled": pooled, "per_city": per_city}


# ── Section: state transitions history ────────────────────────────────
def _state_transitions_section(conn: sqlite3.Connection) -> list[dict]:
    """Recent transitions — anything with a non-null last_state_change."""
    rows = conn.execute(
        """SELECT source, city, state, last_state_change_iso, notes
             FROM weather_source_state
            WHERE last_state_change_iso IS NOT NULL
            ORDER BY last_state_change_iso DESC LIMIT 50"""
    ).fetchall()
    return [
        {"source": r[0], "city": r[1], "to_state": r[2],
         "when": r[3], "notes": r[4]}
        for r in rows
    ]


# ── Section: combine Brier by (city, TTE bucket) ─────────────────────
def _combine_brier_section(conn: sqlite3.Connection) -> dict:
    """Per-(city, TTE bucket) Brier of combined_v2 vs market_mid.

    Joins weather_forecast_snapshots (combined_v2 source rows) with
    weather_metar_hourly_backfill for actual high, and weather_mm_shadow
    for market_mid. Uses last 30 days of data.
    """
    rows = conn.execute(
        """WITH combined AS (
              SELECT s.ticker, s.recorded_at, s.forecast_high_f AS combined_mu,
                     CAST(strftime('%s', s.recorded_at) AS INTEGER) AS rec_unix
                FROM weather_forecast_snapshots s
               WHERE s.source = 'combined_v2'
                 AND s.forecast_high_f IS NOT NULL
                 AND s.recorded_at > datetime('now', '-30 days')
           ),
           ticker_meta AS (
              SELECT DISTINCT ticker,
                     CASE WHEN ticker LIKE 'KXHIGHNY%' THEN 'KNYC'
                          WHEN ticker LIKE 'KXHIGHCHI%' THEN 'KMDW'
                          WHEN ticker LIKE 'KXHIGHMIA%' THEN 'KMIA'
                          WHEN ticker LIKE 'KXHIGHAUS%' THEN 'KAUS'
                          WHEN ticker LIKE 'KXHIGHLAX%' THEN 'KLAX'
                          WHEN ticker LIKE 'KXHIGHDEN%' THEN 'KDEN' END AS station,
                     '2026-' || CASE SUBSTR(ticker, INSTR(ticker, '-26')+3, 3)
                       WHEN 'JAN' THEN '01' WHEN 'FEB' THEN '02'
                       WHEN 'MAR' THEN '03' WHEN 'APR' THEN '04'
                       WHEN 'MAY' THEN '05' WHEN 'JUN' THEN '06' END
                       || '-' || SUBSTR(ticker, INSTR(ticker, '-26')+6, 2) AS lst_date,
                     CASE WHEN ticker LIKE '%-B%' THEN
                       CAST(SUBSTR(ticker, INSTR(ticker, '-B')+2) AS REAL) - 0.5 END AS bracket_lo
                FROM combined
           )
           SELECT tm.station,
                  CASE WHEN w.hours_left <= 6 THEN '01_<=6h'
                       WHEN w.hours_left <= 12 THEN '02_6-12h'
                       WHEN w.hours_left <= 24 THEN '03_12-24h'
                       ELSE '04_>24h' END AS tte_bucket,
                  w.market_mid,
                  c.combined_mu, gt.daily_high_f,
                  tm.bracket_lo
             FROM combined c
             JOIN weather_mm_shadow w ON w.ticker = c.ticker
              AND ABS(w.ts_unix - c.rec_unix) <= 30
             JOIN ticker_meta tm ON tm.ticker = c.ticker
             JOIN weather_metar_hourly_backfill gt
               ON gt.station = tm.station AND gt.lst_date = tm.lst_date
              AND gt.daily_high_f IS NOT NULL
            WHERE w.market_mid IS NOT NULL AND w.hours_left IS NOT NULL
              AND tm.bracket_lo IS NOT NULL"""
    ).fetchall()

    import math

    def _ncdf(x, mu, sigma):
        if sigma <= 0:
            return 1.0 if x >= mu else 0.0
        return 0.5 * (1 + math.erf((x - mu) / (sigma * math.sqrt(2))))

    # Bucket → (sum_brier_combined, sum_brier_market, n) per (station, tte)
    by_cell: dict = {}
    for station, tte, mid, mu, actual, lo in rows:
        if station is None or lo is None:
            continue
        key = (station, tte)
        d = by_cell.setdefault(key, {"n": 0, "combined_sse": 0.0, "market_sse": 0.0})
        # Project μ ± σ (assume σ=1.5 default since we don't have it here)
        # to bracket bounds
        outcome = 1.0 if (actual >= lo and actual < lo + 2) else 0.0
        # Combined p_yes from CDF (assume σ=1.5 — this is approximate)
        sigma = 1.5
        p_combined = _ncdf(lo + 2, mu, sigma) - _ncdf(lo, mu, sigma)
        p_combined = max(0.005, min(0.995, p_combined))
        p_market = mid / 100.0
        d["n"] += 1
        d["combined_sse"] += (p_combined - outcome) ** 2
        d["market_sse"] += (p_market - outcome) ** 2

    out = []
    for (station, tte), d in sorted(by_cell.items()):
        if d["n"] == 0:
            continue
        out.append({
            "station": station, "tte_bucket": tte, "n": d["n"],
            "combined_brier": round(d["combined_sse"] / d["n"], 4),
            "market_brier": round(d["market_sse"] / d["n"], 4),
            "we_beat_market_by": round(
                (d["market_sse"] - d["combined_sse"]) / d["n"], 4),
        })
    return out


# ── Section: calibration curve ────────────────────────────────────────
def _calibration_section(conn: sqlite3.Connection) -> list[dict]:
    """Bucket alpha_backtest by predicted P(YES); compute actual YES rate.

    A well-calibrated source has predicted ≈ actual within each bucket.
    """
    # Note: post-2026-04-29 migration, ensemble_p_yes is canonical P(YES).
    # We need to map won_yes (which is per-side our_side_won) back to
    # actual YES outcome.
    rows = conn.execute(
        """SELECT ensemble_p_yes, side, settlement_result
             FROM alpha_backtest
            WHERE ts_settle_unix IS NOT NULL
              AND ensemble_p_yes IS NOT NULL
              AND settlement_result IN ('yes', 'no')
              AND ts_decision_unix > strftime('%s', datetime('now', '-60 days'))"""
    ).fetchall()

    buckets: dict = {}
    for p, side, result in rows:
        # Bucket by predicted P(YES) — 10 buckets of 0.1 width
        b = min(int(p * 10), 9)
        bucket_label = f"{b/10:.1f}-{(b+1)/10:.1f}"
        actual_yes = 1 if result == "yes" else 0
        d = buckets.setdefault(bucket_label, {"n": 0, "sum_p": 0.0, "sum_actual": 0})
        d["n"] += 1
        d["sum_p"] += p
        d["sum_actual"] += actual_yes

    return [
        {
            "bucket": b,
            "n": d["n"],
            "predicted_pYES": round(d["sum_p"] / d["n"], 3),
            "actual_pYES": round(d["sum_actual"] / d["n"], 3),
        }
        for b, d in sorted(buckets.items())
        if d["n"] >= 5
    ]


# ── Section: recent trading activity ──────────────────────────────────
def _recent_trading_section(conn: sqlite3.Connection) -> dict:
    """Decisions in last 24h + skip-reason breakdown."""
    skip_reasons = conn.execute(
        """SELECT decision_outcome, skip_reason, COUNT(*) AS n
             FROM alpha_backtest
            WHERE ts_decision_unix > strftime('%s', datetime('now', '-24 hours'))
            GROUP BY decision_outcome, skip_reason
            ORDER BY n DESC LIMIT 20"""
    ).fetchall()

    by_family = conn.execute(
        """SELECT family, decision_outcome, COUNT(*) AS n
             FROM alpha_backtest
            WHERE ts_decision_unix > strftime('%s', datetime('now', '-24 hours'))
            GROUP BY family, decision_outcome
            ORDER BY family, n DESC LIMIT 30"""
    ).fetchall()

    return {
        "skip_reasons": [
            {"outcome": r[0], "skip_reason": r[1], "n": r[2]}
            for r in skip_reasons
        ],
        "by_family": [
            {"family": r[0], "outcome": r[1], "n": r[2]}
            for r in by_family
        ],
    }


# ── Section: system health ────────────────────────────────────────────
def _system_health_section(conn: sqlite3.Connection) -> dict:
    """Health-table-derived stats. Pulls last few hours of recordings."""
    cycles_24h = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE timestamp > datetime('now', '-24 hours')"
    ).fetchone()[0]

    settlements_24h = conn.execute(
        """SELECT COUNT(*) FROM alpha_backtest
            WHERE ts_settle_unix > strftime('%s', datetime('now', '-24 hours'))"""
    ).fetchone()[0]

    snapshots_1h_per_source = conn.execute(
        """SELECT source, COUNT(*) AS n
             FROM weather_forecast_snapshots
            WHERE recorded_at > datetime('now', '-1 hour')
            GROUP BY source ORDER BY n DESC"""
    ).fetchall()

    return {
        "cycles_24h": cycles_24h,
        "settlements_24h": settlements_24h,
        "snapshots_1h_per_source": [
            {"source": s, "n": n} for s, n in snapshots_1h_per_source
        ],
    }


# ── HTML rendering ────────────────────────────────────────────────────
_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Kalshi bot dashboard — {generated_at}</title>
<script src="https://cdn.plot.ly/plotly-2.35.0.min.js"></script>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
          max-width: 1200px; margin: 0 auto; padding: 16px; color: #1a1a1a;
          background: #fafafa; }}
  h1 {{ font-size: 22px; margin-bottom: 8px; }}
  h2 {{ font-size: 18px; margin-top: 32px; padding-bottom: 4px;
        border-bottom: 1px solid #ddd; }}
  h3 {{ font-size: 15px; color: #444; margin-top: 16px; }}
  table {{ border-collapse: collapse; margin: 8px 0; font-size: 13px; }}
  th, td {{ padding: 4px 10px; border: 1px solid #ddd; text-align: left; }}
  th {{ background: #f0f0f0; font-weight: 600; }}
  tr:nth-child(even) td {{ background: #f9f9f9; }}
  .plot {{ margin: 16px 0; }}
  .meta {{ color: #888; font-size: 12px; }}
  .state-active {{ color: #15803d; font-weight: 600; }}
  .state-probationary {{ color: #ca8a04; font-weight: 600; }}
  .state-shadow {{ color: #6b7280; }}
  .state-demoted {{ color: #b91c1c; font-weight: 600; }}
  .delta-positive {{ color: #15803d; }}
  .delta-negative {{ color: #b91c1c; }}
  .alert {{ background: #fef3c7; border-left: 3px solid #f59e0b;
           padding: 8px 12px; margin: 12px 0; }}
</style>
</head>
<body>

<h1>Kalshi bot dashboard</h1>
<p class="meta">Generated {generated_at} · Source: {db_path}</p>

{sections_html}

<script>
{plot_scripts}
</script>
</body>
</html>
"""


def _format_state_cell(state: str) -> str:
    return f'<span class="state-{state}">{state}</span>'


def _format_delta(value: Optional[float], higher_is_better: bool = False) -> str:
    if value is None:
        return "—"
    cls = ("delta-positive" if (value > 0) == higher_is_better
           else "delta-negative" if value != 0 else "")
    return f'<span class="{cls}">{value:+.3f}</span>'


def _render_source_health(data: dict) -> tuple[str, str]:
    pooled = data["pooled"]
    if not pooled:
        return ("<h2>1. Source health</h2><p>(no data)</p>", "")

    rows_html = ""
    for d in pooled:
        bias_str = f"{d['bias']:+.2f}" if d["bias"] is not None else "—"
        sigma_str = f"{d['sigma']:.2f}" if d["sigma"] is not None else "—"
        mae7 = f"{d['mae_7d']:.2f}" if d["mae_7d"] is not None else "—"
        mae30 = f"{d['mae_30d']:.2f}" if d["mae_30d"] is not None else "—"
        rows_html += (
            f"<tr><td>{d['source']}</td>"
            f"<td>{_format_state_cell(d['state'])}</td>"
            f"<td>{d['n_settled']}</td>"
            f"<td>{sigma_str}</td>"
            f"<td>{bias_str}</td>"
            f"<td>{mae7}</td>"
            f"<td>{mae30}</td>"
            f"<td class='meta'>{d['last_state_change'] or '—'}</td></tr>"
        )

    html = (
        "<h2>1. Source health</h2>"
        "<p class='meta'>Pooled-across-cities state. Per-city detail in the "
        "state machine table on disk; expand here in v2.</p>"
        "<table><tr><th>Source</th><th>State</th><th>n settled</th>"
        "<th>σ fitted</th><th>bias fitted</th><th>MAE 7d</th>"
        "<th>MAE 30d</th><th>Last state change</th></tr>"
        + rows_html + "</table>"
    )
    return (html, "")


def _render_state_transitions(transitions: list) -> str:
    if not transitions:
        return ("<h2>2. State transitions</h2>"
                "<p>No transitions recorded yet.</p>")
    rows_html = "".join(
        f"<tr><td>{t['when']}</td><td>{t['source']}/{t['city']}</td>"
        f"<td>{_format_state_cell(t['to_state'])}</td>"
        f"<td class='meta'>{t['notes'] or ''}</td></tr>"
        for t in transitions[:20]
    )
    return (
        "<h2>2. State transitions (last 20)</h2>"
        "<table><tr><th>When</th><th>Source / City</th>"
        "<th>New state</th><th>Reason</th></tr>"
        + rows_html + "</table>"
    )


def _render_combine_brier(rows: list) -> tuple[str, str]:
    if not rows:
        return ("<h2>3. Combine quality</h2><p>(no settled-cycle data yet)</p>", "")

    # Build a Plotly grouped bar chart: x=tte_bucket, bars per station,
    # color by we_beat_market_by (green = we win, red = we lose)
    stations = sorted(set(r["station"] for r in rows))
    tte_buckets = sorted(set(r["tte_bucket"] for r in rows))

    by_station: dict = {}
    for r in rows:
        by_station.setdefault(r["station"], {})[r["tte_bucket"]] = r

    traces = []
    for st in stations:
        cells = by_station.get(st, {})
        x_vals = []
        y_vals = []
        text = []
        for tb in tte_buckets:
            cell = cells.get(tb)
            if cell is None:
                continue
            x_vals.append(tb)
            y_vals.append(cell["we_beat_market_by"])
            text.append(
                f"n={cell['n']}<br>"
                f"combined Brier: {cell['combined_brier']:.3f}<br>"
                f"market Brier: {cell['market_brier']:.3f}<br>"
                f"beat: {cell['we_beat_market_by']:+.3f}"
            )
        traces.append({
            "x": x_vals, "y": y_vals, "name": st,
            "type": "bar", "text": text,
            "hovertemplate": "%{text}<extra>%{fullData.name}</extra>",
        })

    plot_div_id = "plot-combine-brier"
    layout = {
        "title": "Combine vs Market — Brier difference (positive = we win)",
        "xaxis": {"title": "TTE bucket"},
        "yaxis": {"title": "Market Brier - Combined Brier",
                  "zeroline": True, "zerolinecolor": "#888"},
        "barmode": "group", "height": 380,
        "showlegend": True,
    }

    plot_html = f'<div id="{plot_div_id}" class="plot"></div>'
    plot_script = (
        f"Plotly.newPlot('{plot_div_id}', "
        f"{json.dumps(traces)}, {json.dumps(layout)});"
    )

    # Also a table for read-at-a-glance
    rows_html = "".join(
        f"<tr><td>{r['station']}</td><td>{r['tte_bucket']}</td>"
        f"<td>{r['n']}</td><td>{r['combined_brier']:.3f}</td>"
        f"<td>{r['market_brier']:.3f}</td>"
        f"<td>{_format_delta(r['we_beat_market_by'], higher_is_better=True)}</td></tr>"
        for r in sorted(rows, key=lambda x: (x["station"], x["tte_bucket"]))
    )
    table_html = (
        "<h3>Detail table</h3>"
        "<table><tr><th>Station</th><th>TTE</th><th>n</th>"
        "<th>Combined Brier</th><th>Market Brier</th>"
        "<th>We beat market by</th></tr>"
        + rows_html + "</table>"
    )

    return (
        f"<h2>3. Combine quality</h2>"
        f"<p class='meta'>Last 30 days, all settled markets where market mid "
        f"was recorded. σ assumed 1.5°F (approximate — uses combined_v2 μ "
        f"and a fixed σ; live combine uses learned σ which varies).</p>"
        + plot_html + table_html
    ), plot_script


def _render_calibration(rows: list) -> tuple[str, str]:
    if not rows:
        return ("<h2>4. Calibration curve</h2><p>(insufficient data)</p>", "")

    pred_x = [r["predicted_pYES"] for r in rows]
    actual_y = [r["actual_pYES"] for r in rows]
    n_text = [f"n={r['n']}" for r in rows]

    plot_div_id = "plot-calibration"
    traces = [
        {
            "x": pred_x, "y": actual_y, "type": "scatter", "mode": "markers+lines",
            "name": "Observed", "text": n_text,
            "hovertemplate": "predicted: %{x:.2f}<br>actual: %{y:.2f}<br>%{text}<extra></extra>",
            "marker": {"size": 10},
        },
        {
            "x": [0, 1], "y": [0, 1], "type": "scatter", "mode": "lines",
            "name": "Perfect calibration", "line": {"dash": "dash", "color": "#999"},
        },
    ]
    layout = {
        "title": "Calibration: predicted vs actual P(YES)",
        "xaxis": {"title": "Predicted P(YES)", "range": [0, 1]},
        "yaxis": {"title": "Actual P(YES)", "range": [0, 1]},
        "height": 380,
        "showlegend": True,
    }

    plot_html = f'<div id="{plot_div_id}" class="plot"></div>'
    plot_script = (
        f"Plotly.newPlot('{plot_div_id}', "
        f"{json.dumps(traces)}, {json.dumps(layout)});"
    )

    return (
        f"<h2>4. Calibration curve</h2>"
        f"<p class='meta'>alpha_backtest predictions over last 60 days, bucketed "
        f"by predicted P(YES). A well-calibrated model lies on the dashed line.</p>"
        + plot_html
    ), plot_script


def _render_recent_trading(data: dict) -> str:
    skip_rows = "".join(
        f"<tr><td>{r['outcome']}</td><td>{r['skip_reason'] or '—'}</td>"
        f"<td>{r['n']}</td></tr>"
        for r in data["skip_reasons"]
    )
    fam_rows = "".join(
        f"<tr><td>{r['family']}</td><td>{r['outcome']}</td><td>{r['n']}</td></tr>"
        for r in data["by_family"]
    )
    return (
        "<h2>5. Recent trading (last 24h)</h2>"
        "<h3>Skip-reason breakdown</h3>"
        "<table><tr><th>Outcome</th><th>Skip reason</th><th>N</th></tr>"
        + (skip_rows or "<tr><td colspan='3'>(none)</td></tr>")
        + "</table>"
        + "<h3>By family</h3>"
        "<table><tr><th>Family</th><th>Outcome</th><th>N</th></tr>"
        + (fam_rows or "<tr><td colspan='3'>(none)</td></tr>")
        + "</table>"
    )


def _render_system_health(data: dict) -> str:
    snap_rows = "".join(
        f"<tr><td>{r['source']}</td><td>{r['n']}</td></tr>"
        for r in data["snapshots_1h_per_source"]
    )
    return (
        "<h2>6. System health</h2>"
        f"<p>Cycles in last 24h: <b>{data['cycles_24h']}</b></p>"
        f"<p>Settlements in last 24h: <b>{data['settlements_24h']}</b></p>"
        f"<h3>Snapshots written in last hour, by source</h3>"
        "<table><tr><th>Source</th><th>N rows</th></tr>"
        + (snap_rows or "<tr><td colspan='2'>(none)</td></tr>")
        + "</table>"
    )


# ── Main ──────────────────────────────────────────────────────────────
def generate_dashboard(db_path: str, output_path: str) -> None:
    # Ensure all expected tables exist — init_db is idempotent. This
    # makes the dashboard tolerant of older DB snapshots that pre-date
    # newer schema additions (weather_source_state, etc.).
    import bot.db as db_mod
    db_mod._PERSIST_CONN = None
    conn = db_mod.init_db(db_path)
    try:
        sections_html = []
        plot_scripts = []

        # 1. Source health
        sh_data = _source_health_section(conn)
        h, _ = _render_source_health(sh_data)
        sections_html.append(h)

        # 2. Transitions
        sections_html.append(
            _render_state_transitions(_state_transitions_section(conn))
        )

        # 3. Combine quality
        h, s = _render_combine_brier(_combine_brier_section(conn))
        sections_html.append(h)
        if s:
            plot_scripts.append(s)

        # 4. Calibration
        h, s = _render_calibration(_calibration_section(conn))
        sections_html.append(h)
        if s:
            plot_scripts.append(s)

        # 5. Recent trading
        sections_html.append(
            _render_recent_trading(_recent_trading_section(conn))
        )

        # 6. System health
        sections_html.append(
            _render_system_health(_system_health_section(conn))
        )

        html = _HTML_TEMPLATE.format(
            generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            db_path=str(db_path),
            sections_html="\n".join(sections_html),
            plot_scripts="\n".join(plot_scripts),
        )

        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html)
        print(f"[dashboard] wrote {len(html)} bytes to {output_path}")
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--output", default="reports/dashboard.html")
    args = ap.parse_args()
    generate_dashboard(args.db, args.output)


if __name__ == "__main__":
    main()
