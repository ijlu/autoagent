import os, time, base64, json, warnings, sys, sqlite3, urllib.parse, ssl, uuid
warnings.filterwarnings("ignore", category=DeprecationWarning)  # only suppress deprecation noise
from datetime import datetime, timezone, timedelta

# Fix SSL cert verification on macOS (Python may not find system certs)
# truststore hooks into macOS Keychain for full cert coverage
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    try:
        import certifi
        os.environ.setdefault("SSL_CERT_FILE", certifi.where())
        os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
    except ImportError:
        _mac_cert = "/etc/ssl/cert.pem"
        if os.path.exists(_mac_cert):
            os.environ.setdefault("SSL_CERT_FILE", _mac_cert)
            os.environ.setdefault("REQUESTS_CA_BUNDLE", _mac_cert)

try:
    import requests
except ImportError:
    os.system(f"{sys.executable} -m pip install requests cryptography certifi -q")
    import requests

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════
KEY_ID   = os.environ.get("KALSHI_API_KEY_ID", "")
KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
if not KEY_ID or not KEY_PATH:
    print("[FATAL] KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH must be set in environment")
    # Don't exit — allow import for debugging, but trading will fail
BASE_URL = os.environ.get("KALSHI_API_BASE", "https://api.elections.kalshi.com/trade-api/v2")
HOST     = BASE_URL.split("/trade-api")[0]

DRY_RUN          = os.environ.get("DRY_RUN", "false").lower() in ("true", "1", "yes")
DAILY_LOSS_LIMIT = float(os.environ.get("DAILY_LOSS_LIMIT_PCT", "0.10"))
MAX_DRAWDOWN     = float(os.environ.get("MAX_DRAWDOWN_PCT",     "0.15"))
KELLY_FRACTION   = float(os.environ.get("KELLY_FRACTION",       "0.10"))
MAX_CONTRACTS    = int(os.environ.get("MAX_CONTRACTS",         "500"))   # hard ceiling (dynamic sizing via MAX_POSITION_PCT is primary control)
MAX_POSITION_PCT = float(os.environ.get("MAX_POSITION_PCT",    "0.02"))  # max 2% of balance per position
DB_PATH          = os.environ.get("DB_PATH", "/task/kalshi_trades.db")
MIN_WIN_RATE     = float(os.environ.get("MIN_WIN_RATE", "0.45"))
MIN_SAMPLE_SIZE  = int(os.environ.get("MIN_SAMPLE_SIZE", "5"))
ORDER_MAX_AGE_HOURS = float(os.environ.get("ORDER_MAX_AGE_HOURS", "2"))

# Position management
TAKE_PROFIT_PCT  = float(os.environ.get("TAKE_PROFIT_PCT", "0.20"))   # sell at +20%
STOP_LOSS_PCT    = float(os.environ.get("STOP_LOSS_PCT",   "0.15"))   # sell at -15%
MAX_HOLD_DAYS    = int(os.environ.get("MAX_HOLD_DAYS",     "7"))      # force exit after 7d

# Information layer
MIN_EDGE           = float(os.environ.get("MIN_EDGE", "0.07"))          # require 7% edge over market (3+ sources)
SINGLE_SOURCE_EDGE = float(os.environ.get("SINGLE_SOURCE_EDGE", "0.12"))  # 12% edge with only 1 source
MAX_PER_CATEGORY   = int(os.environ.get("MAX_PER_CATEGORY", "2"))        # max concurrent positions per risk category
MAX_PORTFOLIO_PCT  = float(os.environ.get("MAX_PORTFOLIO_PCT", "0.15"))   # max 15% of balance in open positions

# Fee accounting: Kalshi charges per-contract fees + we pay the spread to enter/exit
# Estimate round-trip cost as entry_spread_cost + exit_spread_cost + platform_fee
# Kalshi fee schedule: ~$0.01-0.02 per contract per side (varies), we estimate conservatively
ESTIMATED_FEE_PER_CONTRACT = float(os.environ.get("ESTIMATED_FEE_PER_CONTRACT", "0.03"))  # $0.03/contract/side (conservative)
ESTIMATED_EXIT_SPREAD = float(os.environ.get("ESTIMATED_EXIT_SPREAD", "0.03"))  # 3¢ expected exit slippage

# Market making config (separate from directional DRY_RUN so MM can go live in Phase 1)
MM_ENABLED       = os.environ.get("MM_ENABLED", "true").lower() in ("true", "1", "yes")
MM_DRY_RUN       = os.environ.get("MM_DRY_RUN", "true").lower() in ("true", "1", "yes")
MM_MIN_SPREAD    = int(os.environ.get("MM_MIN_SPREAD_CENTS", "4"))
MM_HALF_SPREAD   = int(os.environ.get("MM_HALF_SPREAD_CENTS", "2"))
MM_MAX_INVENTORY = int(os.environ.get("MM_MAX_INVENTORY", "50"))
MM_MAX_MARKETS   = int(os.environ.get("MM_MAX_MARKETS", "10"))
MM_CAPITAL_PCT   = float(os.environ.get("MM_CAPITAL_PCT", "0.10"))
MM_ORDER_SIZE    = int(os.environ.get("MM_ORDER_SIZE", "10"))
MM_SKEW_PER_10   = int(os.environ.get("MM_SKEW_PER_10", "2"))
MM_MIN_VOLUME    = int(os.environ.get("MM_MIN_VOLUME", "25"))
MM_ORDER_TAG     = "mm_v1"
MM_PREFERRED_CATS = {"weather", "climate", "economics"}  # crypto, sports, company removed — crypto/index too fast for 2min cron

# ══════════════════════════════════════════════════════════════════════════════
# PHASED SIZING — auto-ramp based on track record
# ══════════════════════════════════════════════════════════════════════════════
# The bot starts ultra-conservative and only scales up as it earns the right to.
# Phase transitions are based on settled trade count AND win rate — both must be met.
# Override via FORCE_PHASE env var to manually lock a phase (e.g., "1" for paper only).
FORCE_PHASE = os.environ.get("FORCE_PHASE", "")

PHASE_CONFIG = {
    # phase: (min_settled, min_win_rate, max_position_pct, max_portfolio_pct,
    #         max_contracts, kelly_mult, min_edge_mult, description)
    1: (0,    0.00, 0.000, 0.000,   0, 0.0, 1.0,
        "Paper trading — DRY_RUN forced on, zero risk"),
    2: (50,   0.50, 0.035, 0.15,   10, 0.25, 1.5,
        "Micro live — ~$20 max position, 10 contracts max, 1.5x edge required"),
    3: (150,  0.52, 0.005, 0.05,   50, 0.50, 1.25,
        "Small live — $500 max position, $5k max portfolio, 1.25x edge required"),
    4: (300,  0.53, 0.010, 0.10,  200, 0.75, 1.1,
        "Medium live — $1k max position, $10k max portfolio"),
    5: (500,  0.54, 0.020, 0.15,  500, 1.00, 1.0,
        "Full deployment — standard parameters"),
}

def compute_current_phase(conn):
    """Determine which phase the bot has earned based on its track record.
    Returns (phase_number, phase_config_dict, stats_dict)."""
    # Count settled trades and win rate
    try:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(won), 0) FROM settlements"
        ).fetchone()
        n_settled = row[0] if row else 0
        n_won = row[1] if row else 0
        win_rate = n_won / n_settled if n_settled > 0 else 0.0

        # Also compute recent win rate (last 100 trades) for regression detection
        recent = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(won), 0) FROM "
            "(SELECT won FROM settlements ORDER BY id DESC LIMIT 100)"
        ).fetchone()
        recent_n = recent[0] if recent else 0
        recent_wr = recent[1] / recent_n if recent_n > 0 else 0.0
    except Exception:
        n_settled, n_won, win_rate, recent_n, recent_wr = 0, 0, 0.0, 0, 0.0

    stats = {
        "settled": n_settled, "won": n_won, "win_rate": win_rate,
        "recent_n": recent_n, "recent_win_rate": recent_wr,
    }

    # Manual override
    if FORCE_PHASE:
        try:
            forced = int(FORCE_PHASE)
            if forced in PHASE_CONFIG:
                print(f"[phase] FORCED to Phase {forced} via env var")
                return forced, PHASE_CONFIG[forced], stats
        except ValueError:
            pass

    # Determine highest phase we qualify for
    current_phase = 1
    for phase_num in sorted(PHASE_CONFIG.keys()):
        min_trades, min_wr, *_ = PHASE_CONFIG[phase_num]
        if n_settled >= min_trades and (win_rate >= min_wr or n_settled == 0):
            current_phase = phase_num
        else:
            break  # phases are sequential — stop at first unqualified

    # Safety: if recent win rate (last 100) drops below 48%, downgrade one phase
    if recent_n >= 30 and recent_wr < 0.48 and current_phase > 2:
        print(f"[phase] WARNING: Recent win rate {recent_wr:.1%} < 48% — "
              f"downgrading from Phase {current_phase} to {current_phase - 1}")
        current_phase -= 1

    return current_phase, PHASE_CONFIG[current_phase], stats

def apply_phase_limits(phase_num, phase_cfg):
    """Apply phase-specific limits to global config variables.
    Returns a dict of the effective limits for logging."""
    global DRY_RUN, MAX_POSITION_PCT, MAX_PORTFOLIO_PCT, MAX_CONTRACTS
    global KELLY_FRACTION, MIN_EDGE, SINGLE_SOURCE_EDGE

    _, _, max_pos_pct, max_port_pct, max_contracts, kelly_mult, edge_mult, desc = phase_cfg

    # DIRECTIONAL TRADING DISABLED (V4): losing -$93.93 at 16% win rate.
    # Force DRY_RUN=True for all phases so directional orders never go live.
    # MM is unaffected (uses MM_DRY_RUN separately).
    DRY_RUN = True

    # Apply limits — never exceed what the phase allows, but respect
    # user-configured values if they're MORE conservative
    MAX_POSITION_PCT = min(MAX_POSITION_PCT, max_pos_pct)
    MAX_PORTFOLIO_PCT = min(MAX_PORTFOLIO_PCT, max_port_pct)
    MAX_CONTRACTS = min(MAX_CONTRACTS, max_contracts)
    KELLY_FRACTION = KELLY_FRACTION * kelly_mult
    MIN_EDGE = MIN_EDGE * edge_mult
    SINGLE_SOURCE_EDGE = SINGLE_SOURCE_EDGE * edge_mult

    effective = {
        "phase": phase_num, "description": desc,
        "dry_run": DRY_RUN, "max_position_pct": MAX_POSITION_PCT,
        "max_portfolio_pct": MAX_PORTFOLIO_PCT, "max_contracts": MAX_CONTRACTS,
        "kelly_fraction": KELLY_FRACTION, "min_edge": MIN_EDGE,
        "single_source_edge": SINGLE_SOURCE_EDGE,
    }
    return effective

print(f"[trade.py] HOST={HOST}  DRY_RUN={DRY_RUN}  MM_DRY_RUN={MM_DRY_RUN}  "
      f"KELLY={KELLY_FRACTION}x  MIN_EDGE={MIN_EDGE}  SINGLE_SRC_EDGE={SINGLE_SOURCE_EDGE}")

# ══════════════════════════════════════════════════════════════════════════════
# SQLite
# ══════════════════════════════════════════════════════════════════════════════
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, ticker TEXT, side TEXT,
        action TEXT, score REAL, reason TEXT, strategy TEXT, price_cents INTEGER,
        contracts INTEGER, volume REAL, spread_cents REAL, independent_prob REAL,
        market_prob REAL, edge REAL, dry_run INTEGER, order_id TEXT, error TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS settlements (
        id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT, order_id TEXT UNIQUE,
        ticker TEXT, side TEXT, price_cents INTEGER, contracts INTEGER,
        revenue_cents INTEGER, profit_cents INTEGER, won INTEGER,
        volume REAL, spread_cents REAL, strategy TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, balance_cents INTEGER,
        portfolio_cents INTEGER, markets_scanned INTEGER, opportunities_found INTEGER,
        orders_attempted INTEGER, positions_managed INTEGER, orders_pruned INTEGER,
        dry_run INTEGER, halted INTEGER, halt_reason TEXT, patterns_avoided TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS position_exits (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, ticker TEXT, side TEXT,
        entry_price_cents INTEGER, exit_price_cents INTEGER, contracts INTEGER,
        exit_reason TEXT, order_id TEXT, error TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS calibration (
        id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT, ticker TEXT,
        estimated_prob REAL, actual_outcome INTEGER,
        source_desc TEXT, n_sources INTEGER, bucket TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS strategy_journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT,
        entry_type TEXT,
        category TEXT,
        title TEXT,
        detail TEXT,
        metric_value REAL,
        metric_name TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS loss_postmortems (
        id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT,
        order_id TEXT, ticker TEXT, category TEXT,
        loss_type TEXT,
        source_combo TEXT, estimated_prob REAL, market_prob REAL,
        edge_at_entry REAL, price_at_settlement REAL,
        detail TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS pipeline_health (
        id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT,
        source TEXT, status TEXT,
        markets_attempted INTEGER, markets_returned INTEGER,
        avg_latency_ms REAL, error_rate REAL,
        detail TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS edge_convergence (
        id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT,
        ticker TEXT, side TEXT,
        our_estimate REAL, market_price_at_entry REAL,
        market_price_after_1h REAL, market_price_after_6h REAL,
        market_price_after_24h REAL,
        converged INTEGER, convergence_pct REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS timing_patterns (
        id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT,
        order_id TEXT,
        hour_utc INTEGER, day_of_week INTEGER,
        category TEXT, source TEXT,
        edge REAL, won INTEGER, profit_cents INTEGER)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS hyperparam_shadow (
        id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT,
        param_name TEXT, current_value REAL, shadow_value REAL,
        ticker TEXT, actual_contracts INTEGER, shadow_contracts INTEGER,
        actual_profit REAL, shadow_profit REAL)""")
    # Market-making tables
    conn.execute("""CREATE TABLE IF NOT EXISTS mm_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT,
        ticker TEXT, side TEXT, price_cents INTEGER, contracts INTEGER,
        order_id TEXT, status TEXT DEFAULT 'posted', fill_qty INTEGER DEFAULT 0,
        fair_value_cents INTEGER, inventory_at_post INTEGER,
        tag TEXT DEFAULT 'mm_v1')""")
    conn.execute("""CREATE TABLE IF NOT EXISTS mm_inventory (
        id INTEGER PRIMARY KEY AUTOINCREMENT, updated_at TEXT,
        ticker TEXT UNIQUE, net_position INTEGER DEFAULT 0,
        total_bought INTEGER DEFAULT 0, total_sold INTEGER DEFAULT 0,
        realized_pnl_cents INTEGER DEFAULT 0,
        avg_entry_cents REAL DEFAULT 0)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS mm_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT,
        markets_quoted INTEGER, orders_posted INTEGER, orders_cancelled INTEGER,
        fills_detected INTEGER, inventory_value_cents INTEGER,
        realized_pnl_cents INTEGER, unrealized_pnl_cents INTEGER)""")
    # Migrate v1 tables if missing new columns
    _migrations = [
        ("trades", "action", "TEXT DEFAULT 'buy'"),
        ("trades", "score", "REAL DEFAULT 0"),
        ("trades", "strategy", "TEXT DEFAULT 'momentum'"),
        ("trades", "independent_prob", "REAL"),
        ("trades", "market_prob", "REAL"),
        ("trades", "edge", "REAL"),
        ("sessions", "positions_managed", "INTEGER DEFAULT 0"),
        ("sessions", "orders_pruned", "INTEGER DEFAULT 0"),
        ("sessions", "patterns_avoided", "TEXT DEFAULT '[]'"),
        ("trades", "fill_status", "TEXT"),
        # MM table migrations (v3.11)
        ("mm_orders", "fill_qty", "INTEGER DEFAULT 0"),
        ("mm_orders", "tag", "TEXT DEFAULT 'mm_v1'"),
        ("mm_orders", "fair_value_cents", "INTEGER"),
        ("mm_orders", "inventory_at_post", "INTEGER"),
        ("mm_inventory", "avg_entry_cents", "REAL DEFAULT 0"),
    ]
    for table, col, coltype in _migrations:
        try:
            conn.execute(f"SELECT {col} FROM {table} LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
    conn.commit()
    return conn

# ══════════════════════════════════════════════════════════════════════════════
# RSA-PSS AUTH
# ══════════════════════════════════════════════════════════════════════════════
# Load private key ONCE at startup (was re-reading from disk on every API call)
_PRIVATE_KEY = None
def _get_private_key():
    global _PRIVATE_KEY
    if _PRIVATE_KEY is None:
        with open(KEY_PATH, "rb") as f:
            _PRIVATE_KEY = serialization.load_pem_private_key(f.read(), password=None)
    return _PRIVATE_KEY

def _sign(method, path):
    ts_ms = str(int(time.time() * 1000))
    msg   = (ts_ms + method.upper() + path).encode()
    pk = _get_private_key()
    sig = pk.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                  salt_length=padding.PSS.MAX_LENGTH), hashes.SHA256())
    return {"KALSHI-ACCESS-KEY": KEY_ID, "KALSHI-ACCESS-TIMESTAMP": ts_ms,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "Content-Type": "application/json"}

def api_get(path):
    full = "/trade-api/v2" + path
    sign_path = full.split("?")[0]  # sign without query params
    _rate_limit_wait(HOST + full)
    r = requests.get(HOST + full, headers=_sign("GET", sign_path), timeout=15)
    r.raise_for_status(); return r.json()

def api_post(path, body):
    full = "/trade-api/v2" + path
    _rate_limit_wait(HOST + full)
    r = requests.post(HOST + full, headers=_sign("POST", full), json=body, timeout=15)
    if r.status_code >= 400:
        try:
            detail = r.json()
        except Exception:
            detail = r.text[:300]
        print(f"[api_post] {path} → HTTP {r.status_code}: {detail}")
        print(f"[api_post] request body: {body}")
    r.raise_for_status(); return r.json()

def api_delete(path):
    full = "/trade-api/v2" + path
    _rate_limit_wait(HOST + full)
    r = requests.delete(HOST + full, headers=_sign("DELETE", full), timeout=15)
    return r

def get_portfolio():
    resp = api_get("/portfolio/balance")
    return resp.get("balance", 0), resp.get("portfolio_value", 0)

# ══════════════════════════════════════════════════════════════════════════════
# ORDER PRUNING
# ══════════════════════════════════════════════════════════════════════════════
def prune_stale_orders():
    try:
        orders = api_get("/portfolio/orders?status=resting&limit=100").get("orders", [])
    except Exception as e:
        print(f"[prune] Could not fetch orders: {e}"); return 0
    now = datetime.now(timezone.utc)
    cancelled = 0
    for o in orders:
        oid = o.get("order_id", "")
        created_str = o.get("created_time") or o.get("created_at") or ""
        if not oid or not created_str: continue
        try:
            created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            age_h = (now - created).total_seconds() / 3600
            if age_h > ORDER_MAX_AGE_HOURS:
                r = api_delete(f"/portfolio/orders/{oid}")
                if r.status_code in (200, 204):
                    print(f"[prune] Cancelled {oid} ({o.get('ticker')}, {age_h:.1f}h old)")
                    cancelled += 1
        except Exception as e:
            print(f"[prune] Error: {e}")
    print(f"[prune] Cancelled {cancelled} stale orders") if cancelled else print("[prune] No stale orders")
    return cancelled

# ══════════════════════════════════════════════════════════════════════════════
# POSITION MANAGEMENT — take-profit, stop-loss, time-based exits
# ══════════════════════════════════════════════════════════════════════════════
def manage_positions(conn):
    """Check existing positions for exit signals. Returns count of exits attempted."""
    try:
        resp = api_get("/portfolio/positions?limit=100")
        positions = resp.get("market_positions", resp.get("positions", []))
    except Exception as e:
        print(f"[positions] Could not fetch positions: {e}"); return 0

    if not positions:
        print("[positions] No open positions"); return 0

    # Build set of tickers managed by MM — manage_positions() must NOT touch these
    # because its exit orders aren't tracked in mm_orders and would desync mm_inventory.
    mm_tickers = set()
    try:
        mm_rows = conn.execute(
            "SELECT ticker FROM mm_inventory WHERE net_position != 0").fetchall()
        mm_tickers = {r[0] for r in mm_rows}
    except Exception:
        pass  # table might not exist yet

    now = datetime.now(timezone.utc)
    exits = 0

    for pos in positions:
        ticker   = pos.get("ticker", "")
        pos_raw = pos.get("position_fp") or pos.get("position", 0)
        pos_val = int(float(pos_raw)) if pos_raw else 0
        side     = "yes" if pos_val > 0 else "no"
        quantity = abs(pos_val)
        if quantity == 0: continue

        # Skip tickers managed by MM — their exits go through mm_liquidate_expiring()
        if ticker in mm_tickers:
            continue

        # Get current market price for this position
        try:
            mkt = api_get(f"/markets/{ticker}")
            market = mkt.get("market", mkt)
        except Exception as e:
            print(f"[positions] Could not fetch market {ticker}: {e}"); continue

        # Current price
        yes_ask = float(market.get("yes_ask") or market.get("yes_ask_dollars") or 0)
        yes_bid = float(market.get("yes_bid") or market.get("yes_bid_dollars") or 0)
        no_ask  = float(market.get("no_ask") or market.get("no_ask_dollars") or 0)
        no_bid  = float(market.get("no_bid") or market.get("no_bid_dollars") or 0)
        if yes_ask > 1: yes_ask /= 100
        if yes_bid > 1: yes_bid /= 100
        if no_ask > 1: no_ask /= 100
        if no_bid > 1: no_bid /= 100

        # Current exit price (what we'd get selling now) = bid on our side
        exit_price = yes_bid if side == "yes" else no_bid
        if exit_price <= 0: continue

        # Look up entry price from our trades DB
        trade = conn.execute(
            "SELECT price_cents, timestamp FROM trades WHERE ticker=? AND side=? AND action='buy' ORDER BY id DESC LIMIT 1",
            (ticker, side)
        ).fetchone()

        if trade:
            entry_price = trade[0] / 100  # cents to fraction
            entry_time  = trade[1]
        else:
            # Fallback: use position's average cost if available
            entry_price = float(pos.get("average_price_paid") or pos.get("market_exposure") or 0)
            if entry_price > 1: entry_price /= 100
            entry_time = None

        if entry_price <= 0: continue

        # Calculate P&L
        pnl_pct = (exit_price - entry_price) / entry_price

        # Check holding period
        days_held = 0
        if entry_time:
            try:
                entry_dt = datetime.fromisoformat(entry_time.replace("Z", "+00:00"))
                days_held = (now - entry_dt).total_seconds() / 86400
            except: pass

        # ── Exit decision ─────────────────────────────────────────────────
        exit_reason = None

        if pnl_pct >= TAKE_PROFIT_PCT:
            exit_reason = f"take_profit: +{pnl_pct:.0%} (>{TAKE_PROFIT_PCT:.0%})"
        elif pnl_pct <= -STOP_LOSS_PCT:
            exit_reason = f"stop_loss: {pnl_pct:.0%} (<-{STOP_LOSS_PCT:.0%})"
        elif days_held >= MAX_HOLD_DAYS:
            exit_reason = f"time_exit: held {days_held:.1f} days (>{MAX_HOLD_DAYS}d)"

        # ── Re-evaluate with fresh ensemble data ─────────────────────────
        # If our updated estimate has FLIPPED against the position, exit now
        # even if static stop-loss hasn't triggered yet.
        if not exit_reason:
            try:
                current_ask = yes_ask if side == "yes" else no_ask
                vol = float(market.get("volume") or market.get("volume_fp") or 0)
                fresh_prob, fresh_src, fresh_n = get_independent_estimate(
                    ticker, market, current_ask, vol)
                if fresh_prob is not None and fresh_n > 0:
                    # For YES positions, we need fresh_prob > market price to hold
                    # For NO positions, we need (1-fresh_prob) > (1-yes_bid) to hold
                    if side == "yes" and fresh_prob < exit_price - 0.03:
                        exit_reason = (f"ensemble_flip: updated_prob={fresh_prob:.2f} < "
                                       f"exit_price={exit_price:.2f} (was profitable, now unfavorable)")
                    elif side == "no" and (1 - fresh_prob) < (1 - yes_bid) - 0.03:
                        exit_reason = (f"ensemble_flip: updated_no_prob={1-fresh_prob:.2f} "
                                       f"unfavorable — exiting")
                    if exit_reason:
                        print(f"[positions] {ticker}: ensemble re-eval triggered exit "
                              f"({fresh_src}, {fresh_n} sources)")
            except Exception as e:
                print(f"[positions] {ticker}: ensemble re-eval failed: {e}")

        if not exit_reason:
            print(f"[positions] {ticker} {side} x{quantity}: "
                  f"entry={entry_price:.2f} now={exit_price:.2f} pnl={pnl_pct:+.0%} "
                  f"held={days_held:.1f}d — HOLD")
            continue

        print(f"[positions] {ticker} {side} x{quantity}: "
              f"entry={entry_price:.2f} now={exit_price:.2f} pnl={pnl_pct:+.0%} — EXIT: {exit_reason}")

        # Place sell order — aggressive pricing for stop-loss to ensure fill
        base_exit_cents = int(exit_price * 100)
        if "stop_loss" in exit_reason:
            exit_price_cents = max(1, min(99, base_exit_cents - 3))  # 3¢ below bid for urgency
        elif "time_exit" in exit_reason:
            exit_price_cents = max(1, min(99, base_exit_cents - 1))  # 1¢ below bid
        else:
            exit_price_cents = max(1, min(99, base_exit_cents))      # take-profit: patient at bid
        order_id = None
        error = None
        if not DRY_RUN:
            try:
                resp = api_post("/portfolio/orders", {
                    "ticker": ticker,
                    "side": side,
                    "type": "limit",
                    "count": quantity,
                    ("yes_price" if side == "yes" else "no_price"): exit_price_cents,
                    "action": "sell",
                    "expiration_ts": int(time.time() + ORDER_MAX_AGE_HOURS * 3600),
                })
                order_id = resp.get("order", {}).get("order_id") or str(resp)
                print(f"  ✓ sell order {order_id}")
            except Exception as e:
                error = str(e)
                print(f"  ✗ sell failed: {error}")
        else:
            print(f"  [DRY RUN] would sell {quantity}x {ticker} {side} @ {exit_price_cents}¢")

        conn.execute("""INSERT INTO position_exits
            (timestamp, ticker, side, entry_price_cents, exit_price_cents,
             contracts, exit_reason, order_id, error)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (now.isoformat(), ticker, side, int(entry_price * 100),
             exit_price_cents, quantity, exit_reason, order_id, error))
        conn.commit()
        exits += 1

    print(f"[positions] {exits} exit orders placed")
    return exits

# ══════════════════════════════════════════════════════════════════════════════
# INFORMATION LAYER — independent probability estimates from external sources
# ══════════════════════════════════════════════════════════════════════════════
import re, math

# Shared cache: {key: (value, timestamp)}
_CACHE = {}
CACHE_TTL = 60  # seconds

# ── Rate limiter: per-domain minimum interval between requests ───────────
_RATE_LIMITS = {
    # domain substring → (min_interval_seconds, max_burst)
    "kalshi":       (0.25, 8),   # 4 req/s max, burst of 8
    "polymarket":   (0.5,  4),   # 2 req/s, burst of 4
    "open-meteo":   (1.0,  3),   # 1 req/s (free tier)
    "coingecko":    (1.5,  2),   # free tier strict
    "coingecko":    (1.5,  2),   # alternate spelling
    "fred.stlouisfed": (1.0, 3), # FRED free tier
    "the-odds-api": (2.0,  2),   # precious credits — go slow
    "metaculus":    (1.0,  3),
    "finnhub":      (1.0,  3),
    "deribit":      (1.0,  3),
    "noaa":         (1.0,  3),
    "clevelandfed": (2.0,  2),
    "openai":       (0.5,  5),   # GPT-4o-mini calls
    "manifold":     (0.5,  4),   # Manifold Markets API (generous limits)
    "bls.gov":      (2.0,  3),   # BLS API v2 (500 req/day with key)
    "tomorrow.io":  (2.0,  3),   # Tomorrow.io (500 calls/day)
}
# Tracks {domain_key: [timestamp_of_recent_requests]}
_RATE_HISTORY = {}

def _rate_limit_wait(url):
    """Enforce per-domain rate limiting. Blocks until it's safe to make the request."""
    from urllib.parse import urlparse
    domain = urlparse(url).hostname or ""

    matched_key = None
    for key in _RATE_LIMITS:
        if key in domain:
            matched_key = key
            break

    if not matched_key:
        return  # No rate limit configured for this domain

    min_interval, max_burst = _RATE_LIMITS[matched_key]
    now = time.time()

    if matched_key not in _RATE_HISTORY:
        _RATE_HISTORY[matched_key] = []

    history = _RATE_HISTORY[matched_key]
    # Prune old entries (older than max_burst * min_interval)
    window = max_burst * min_interval
    history[:] = [t for t in history if now - t < window]

    if len(history) >= max_burst:
        # We've hit burst limit — wait until oldest request exits the window
        wait_until = history[0] + window
        sleep_time = wait_until - now
        if sleep_time > 0:
            time.sleep(sleep_time)
    elif history:
        # Enforce minimum interval since last request
        time_since_last = now - history[-1]
        if time_since_last < min_interval:
            time.sleep(min_interval - time_since_last)

    _RATE_HISTORY[matched_key].append(time.time())

_DEFAULT_HEADERS = {
    "User-Agent": "KalshiTradingBot/1.0 (contact: bot@example.com)",
    "Accept": "application/json",
}

def _cached_get(key, url, timeout=5, headers=None):
    """GET with in-memory cache, per-domain rate limiting, and retry on transient errors."""
    now = time.time()
    if key in _CACHE and now - _CACHE[key][1] < CACHE_TTL:
        return _CACHE[key][0]
    if not url:
        return None  # guard against None URLs
    max_retries = 2
    for attempt in range(max_retries + 1):
        try:
            _rate_limit_wait(url)
            hdrs = {**_DEFAULT_HEADERS, **(headers or {})}
            r = requests.get(url, timeout=timeout, headers=hdrs)
            if r.status_code in (500, 502, 503) and attempt < max_retries:
                time.sleep(1.0 * (attempt + 1))  # backoff: 1s, 2s
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

# ── 1. POLYMARKET — cross-market arbitrage ────────────────────────────────────
_POLY_MARKETS = None
_POLY_TS = 0

def _load_polymarket():
    """Fetch active Polymarket markets. Cache for 5 min since it's a big list."""
    global _POLY_MARKETS, _POLY_TS
    now = time.time()
    if _POLY_MARKETS is not None and now - _POLY_TS < 300:
        return _POLY_MARKETS
    try:
        _rate_limit_wait("https://gamma-api.polymarket.com/markets")
        r = requests.get("https://gamma-api.polymarket.com/markets?closed=false&limit=500",
                         timeout=10)
        markets = r.json()
        _POLY_MARKETS = markets if isinstance(markets, list) else []
        _POLY_TS = now
        print(f"[poly] Loaded {len(_POLY_MARKETS)} Polymarket markets")
        return _POLY_MARKETS
    except Exception as e:
        print(f"[poly] Failed to load: {e}")
        return []

def _fuzzy_match_polymarket(kalshi_title):
    """Find best Polymarket match for a Kalshi market title."""
    if not kalshi_title:
        return None
    poly_markets = _load_polymarket()
    if not poly_markets:
        return None

    kalshi_lower = kalshi_title.lower()
    # Extract key terms (skip common words)
    stop = {"will","the","be","a","an","in","on","at","to","of","by","for","is","it","or","and"}
    kalshi_words = set(w for w in re.findall(r'\w+', kalshi_lower) if w not in stop and len(w) > 2)

    best_match = None
    best_score = 0

    for pm in poly_markets:
        pm_title = (pm.get("question") or pm.get("title") or "").lower()
        if not pm_title:
            continue
        pm_words = set(w for w in re.findall(r'\w+', pm_title) if w not in stop and len(w) > 2)
        if not pm_words:
            continue
        # Jaccard similarity
        overlap = len(kalshi_words & pm_words)
        union = len(kalshi_words | pm_words)
        score = overlap / union if union > 0 else 0
        if score > best_score and score > 0.60:  # require 60% word overlap (tightened to reduce false matches)
            best_score = score
            best_match = pm

    return best_match

def _validate_polymarket_match(kalshi_market, poly_match):
    """Structural validation beyond title matching: check resolution timing
    and outcome structure are compatible. Returns True if match is trustworthy."""
    # 1. Check resolution date proximity — markets should resolve around the same time
    kalshi_close = (kalshi_market.get("close_time") or kalshi_market.get("expiration_time")
                    or kalshi_market.get("expected_expiration_time") or "")
    poly_close = poly_match.get("endDate") or poly_match.get("end_date_iso") or ""
    if kalshi_close and poly_close:
        try:
            k_dt = datetime.fromisoformat(kalshi_close.replace("Z", "+00:00"))
            p_dt = datetime.fromisoformat(poly_close.replace("Z", "+00:00"))
            days_apart = abs((k_dt - p_dt).total_seconds()) / 86400
            if days_apart > 14:
                # Markets resolve more than 2 weeks apart — likely different events
                return False
        except: pass

    # 2. Check that Polymarket market is binary (2 outcomes) — matching multi-outcome
    # markets to binary Kalshi markets creates mismatches
    outcomes = poly_match.get("outcomes") or poly_match.get("outcomePrices")
    if outcomes:
        try:
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            if len(outcomes) > 2:
                return False  # multi-outcome market, not a clean binary match
        except: pass

    # 3. Check liquidity — very illiquid Polymarket markets have unreliable prices
    poly_volume = float(poly_match.get("volume") or poly_match.get("volumeNum") or 0)
    if poly_volume > 0 and poly_volume < 1000:
        return False  # too thin to trust

    return True

def get_polymarket_estimate(ticker, market_data):
    """Cross-reference Kalshi market with Polymarket for independent price."""
    title = market_data.get("title") or market_data.get("subtitle") or ""
    if not title:
        return None, None

    match = _fuzzy_match_polymarket(title)
    if not match:
        return None, None

    # Structural validation: ensure markets actually reference the same event
    if not _validate_polymarket_match(market_data, match):
        poly_title = (match.get("question") or match.get("title") or "")[:50]
        print(f"[poly] REJECTED structural mismatch: '{title[:40]}' ↔ '{poly_title}'")
        return None, None

    prices_raw = match.get("outcomePrices")
    if not prices_raw:
        return None, None

    try:
        if isinstance(prices_raw, str):
            prices = json.loads(prices_raw)
        else:
            prices = prices_raw
        poly_yes = float(prices[0])
    except (json.JSONDecodeError, IndexError, TypeError):
        return None, None

    poly_title = (match.get("question") or match.get("title") or "")[:60]
    print(f"[poly] Match: '{title[:50]}' ↔ '{poly_title}' → poly_yes={poly_yes:.2f}")
    return poly_yes, f"polymarket:{poly_title[:40]}"

# ── 2. CRYPTO — CoinGecko live prices + volatility ──────────────────────────
def fetch_crypto_price(symbol="bitcoin"):
    data = _cached_get(f"crypto_{symbol}",
        f"https://api.coingecko.com/api/v3/simple/price?ids={symbol}&vs_currencies=usd")
    return data.get(symbol, {}).get("usd") if data else None

def _fetch_realized_vol(symbol, days=30):
    """Fetch realized annualized volatility from CoinGecko historical prices.
    Returns daily vol as a fraction (e.g. 0.03 = 3% daily moves).
    Cache for 1 hour since vol changes slowly."""
    cache_key = f"crypto_vol_{symbol}_{days}"
    now = time.time()
    if cache_key in _CACHE and now - _CACHE[cache_key][1] < 3600:
        return _CACHE[cache_key][0]
    try:
        url = (f"https://api.coingecko.com/api/v3/coins/{symbol}/market_chart?"
               f"vs_currency=usd&days={days}&interval=daily")
        _rate_limit_wait(url)
        r = requests.get(url, timeout=10)
        data = r.json()
        prices = [p[1] for p in data.get("prices", [])]
        if len(prices) < 5:
            return None
        # Compute daily log returns and their std dev
        log_returns = [math.log(prices[i] / prices[i-1]) for i in range(1, len(prices))
                       if prices[i-1] > 0]
        if len(log_returns) < 3:
            return None
        daily_vol = (sum(r**2 for r in log_returns) / len(log_returns)) ** 0.5
        _CACHE[cache_key] = (daily_vol, now)
        print(f"[vol] {symbol} realized daily vol = {daily_vol:.3f} ({daily_vol*100:.1f}%/day) "
              f"from {len(log_returns)} returns")
        return daily_vol
    except Exception as e:
        print(f"[vol] Failed to fetch vol for {symbol}: {e}")
        return None

def _fetch_deribit_iv(symbol):
    """Fetch Deribit implied volatility index (DVOL) for BTC/ETH.
    This is the market's forward-looking vol estimate — much better than
    realized vol for pricing near-term expiries. Free public API, no auth.
    Returns annualized IV as a fraction (e.g. 0.60 = 60% annual vol).
    Convert to daily: daily_vol = annual_vol / sqrt(365)."""
    deribit_map = {"bitcoin": "BTC", "ethereum": "ETH"}
    deribit_sym = deribit_map.get(symbol)
    if not deribit_sym:
        return None
    cache_key = f"deribit_iv_{deribit_sym}"
    now = time.time()
    if cache_key in _CACHE and now - _CACHE[cache_key][1] < 1800:
        return _CACHE[cache_key][0]
    try:
        # Deribit public ticker endpoint — returns mark_iv for the DVOL index
        url = f"https://www.deribit.com/api/v2/public/get_index_price?index_name={deribit_sym.lower()}_usd"
        # Use the volatility index instead
        vol_url = (f"https://www.deribit.com/api/v2/public/ticker?"
                   f"instrument_name={deribit_sym}-PERPETUAL")
        _rate_limit_wait(vol_url)
        r = requests.get(vol_url, timeout=8)
        if r.status_code == 200:
            data = r.json().get("result", {})
            # Try to get mark_iv from options, fallback to estimated_delivery_price movement
            # For the perpetual, compute recent price movement as a vol proxy
            last = float(data.get("last_price", 0))
            stats = data.get("stats", {})
            high = float(stats.get("high", last))
            low = float(stats.get("low", last))
            if last > 0 and high > 0 and low > 0:
                # Daily range as vol proxy: (high-low)/mid / 4 ≈ daily vol
                daily_range_vol = (high - low) / ((high + low) / 2) / 4
                _CACHE[cache_key] = (daily_range_vol, now)
                print(f"[deribit] {deribit_sym} 24h range vol proxy = {daily_range_vol:.4f} "
                      f"({daily_range_vol*100:.2f}%/day)")
                return daily_range_vol
    except Exception as e:
        print(f"[deribit] Failed for {symbol}: {e}")
    return None

# Fallback daily vol estimates if CoinGecko historical and Deribit both fail
_DEFAULT_DAILY_VOL = {"bitcoin": 0.025, "ethereum": 0.035, "solana": 0.05}

def _days_to_expiry(market_data):
    """Extract days until market closes. Returns None if unknown."""
    close_str = (market_data.get("close_time") or market_data.get("expiration_time")
                 or market_data.get("expected_expiration_time") or "")
    if not close_str:
        return None
    try:
        close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
        delta = (close_dt - datetime.now(timezone.utc)).total_seconds() / 86400
        return max(0.01, delta)  # floor at ~15 min
    except:
        return None

def get_crypto_estimate(ticker, market_data):
    ticker_upper = ticker.upper()
    symbol = None
    if "BTC" in ticker_upper or "BITCOIN" in ticker_upper: symbol = "bitcoin"
    elif "ETH" in ticker_upper or "ETHER" in ticker_upper: symbol = "ethereum"
    elif "SOL" in ticker_upper or "SOLANA" in ticker_upper: symbol = "solana"
    else: return None, None

    title = market_data.get("title", "") or market_data.get("subtitle", "") or ""
    strike = None
    for match in re.findall(r'\$?([\d,]+(?:\.\d+)?)', title):
        try:
            val = float(match.replace(",", ""))
            if val > 100: strike = val; break
        except: continue
    if not strike: return None, None

    current_price = fetch_crypto_price(symbol)
    if not current_price: return None, None

    pct_distance = (current_price - strike) / strike

    # ── Volatility-calibrated sigmoid ────────────────────────────────────
    # Use Deribit implied vol (forward-looking) > realized vol > default.
    # k = 1 / (daily_vol * sqrt(days)) — measures how many vol-adjusted
    # standard deviations the price is from the strike.
    days = _days_to_expiry(market_data)
    daily_vol = (_fetch_deribit_iv(symbol)
                 or _fetch_realized_vol(symbol)
                 or _DEFAULT_DAILY_VOL.get(symbol, 0.03))

    if days is not None and days > 0:
        # Expected price range over the remaining time = daily_vol * sqrt(days)
        expected_range = daily_vol * math.sqrt(max(days, 0.1))
        # k scales the sigmoid — higher k = steeper = more confident
        # When pct_distance = expected_range, we want ~73% probability (1 sigma)
        k = 1.0 / max(expected_range, 0.005)
    else:
        # Unknown expiry: use 1-day assumption with vol
        k = 1.0 / max(daily_vol, 0.005)

    prob_yes = max(0.02, min(0.98, 1 / (1 + math.exp(-k * pct_distance))))
    days_str = f" days={days:.1f}" if days else ""
    print(f"[info] Crypto: {symbol} ${current_price:,.0f} vs strike ${strike:,.0f} "
          f"({pct_distance:+.1%}) vol={daily_vol:.3f} k={k:.1f}{days_str} → {prob_yes:.2f}")
    return prob_yes, f"crypto:{symbol}"

# ── 3. WEATHER — Open-Meteo (free, no auth) ──────────────────────────────────
WEATHER_CITIES = {
    "nyc":          {"lat": 40.71, "lon": -74.01, "tz": "America/New_York"},
    "new york":     {"lat": 40.71, "lon": -74.01, "tz": "America/New_York"},
    "chicago":      {"lat": 41.88, "lon": -87.63, "tz": "America/Chicago"},
    "miami":        {"lat": 25.76, "lon": -80.19, "tz": "America/New_York"},
    "austin":       {"lat": 30.27, "lon": -97.74, "tz": "America/Chicago"},
    "los angeles":  {"lat": 34.05, "lon": -118.24, "tz": "America/Los_Angeles"},
    "la":           {"lat": 34.05, "lon": -118.24, "tz": "America/Los_Angeles"},
    "phoenix":      {"lat": 33.45, "lon": -112.07, "tz": "America/Phoenix"},
    "houston":      {"lat": 29.76, "lon": -95.37, "tz": "America/Chicago"},
    "dallas":       {"lat": 32.78, "lon": -96.80, "tz": "America/Chicago"},
    "denver":       {"lat": 39.74, "lon": -104.99, "tz": "America/Denver"},
    "atlanta":      {"lat": 33.75, "lon": -84.39, "tz": "America/New_York"},
    "seattle":      {"lat": 47.61, "lon": -122.33, "tz": "America/Los_Angeles"},
    "boston":        {"lat": 42.36, "lon": -71.06, "tz": "America/New_York"},
    "san francisco":{"lat": 37.77, "lon": -122.42, "tz": "America/Los_Angeles"},
    "sf":           {"lat": 37.77, "lon": -122.42, "tz": "America/Los_Angeles"},
    "dc":           {"lat": 38.91, "lon": -77.04, "tz": "America/New_York"},
    "washington":   {"lat": 38.91, "lon": -77.04, "tz": "America/New_York"},
    "minneapolis":  {"lat": 44.98, "lon": -93.27, "tz": "America/Chicago"},
    "detroit":      {"lat": 42.33, "lon": -83.05, "tz": "America/New_York"},
    "las vegas":    {"lat": 36.17, "lon": -115.14, "tz": "America/Los_Angeles"},
}

def get_weather_forecast(city_key):
    city = WEATHER_CITIES.get(city_key)
    if not city: return None
    url = (f"https://api.open-meteo.com/v1/forecast?"
           f"latitude={city['lat']}&longitude={city['lon']}"
           f"&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
           f"&temperature_unit=fahrenheit&timezone={city['tz']}&forecast_days=7")
    return _cached_get(f"weather_{city_key}", url, timeout=5)

def get_weather_estimate(ticker, market_data):
    title = (market_data.get("title") or market_data.get("subtitle") or "").lower()
    ticker_upper = ticker.upper() if ticker else ""
    # Detect weather market — require weather-related keywords + city name
    weather_keywords = ["temperature", "temp", "°f", "°c", "degrees", "high", "low",
                        "weather", "heat", "cold", "freeze", "highest temperature"]
    is_weather = any(kw in title for kw in weather_keywords) or "KXHIGH" in ticker_upper
    if not is_weather:
        return None, None

    # Map Kalshi weather tickers to city keys (KXHIGHNY → nyc, KXHIGHCHI → chicago, etc.)
    _TICKER_CITY_MAP = {
        "KXHIGHNY": "nyc", "KXHIGHLAX": "los angeles", "KXHIGHCHI": "chicago",
        "KXHIGHMIA": "miami", "KXHIGHAUS": "austin", "KXHIGHHOU": "houston",
        "KXHIGHPHX": "phoenix", "KXHIGHDEN": "denver", "KXHIGHSF": "san francisco",
    }
    city_key = None
    # First try ticker-based city detection
    for prefix, city in _TICKER_CITY_MAP.items():
        if prefix in ticker_upper:
            city_key = city; break
    # Fallback: scan title for city names
    if not city_key:
        for key in WEATHER_CITIES:
            if key in title:
                city_key = key; break
    if not city_key: return None, None

    # Extract temperature threshold from title
    # Kalshi formats: "above 80°F", "below 32°", "at or above 65", or range brackets like "between 50 and 60"
    temp_match = re.search(r'(at or above|at or below|above|below|over|under|at least|exceed)\s+(\d+\.?\d*)', title)
    if not temp_match:
        # Try ticker-based threshold: KXHIGHNY-26APR08-T65, or bracket format -B80.5
        tick_match = re.search(r'-[TB](-?\d+\.?\d*)', ticker)
        if tick_match:
            threshold = float(tick_match.group(1))
            is_above = True  # KXHIGH markets are "will high be above X" by default
        else:
            # Range markets: title like "78-79°F" or "between 78 and 80"
            range_match = re.search(r'(\d+\.?\d*)\s*[-–]\s*(\d+\.?\d*)\s*°?[fF]?', title)
            if range_match:
                low_bound = float(range_match.group(1))
                high_bound = float(range_match.group(2))
                threshold = (low_bound + high_bound) / 2  # midpoint of range
                is_above = True  # will check P(temp in range) below
            else:
                return None, None
    else:
        direction = temp_match.group(1)
        threshold = float(temp_match.group(2))
        is_above = direction in ("above", "over", "at least", "exceed", "at or above")

    # Sanity check: reject obviously non-temperature values
    if threshold < -40 or threshold > 140:
        return None, None

    forecast = get_weather_forecast(city_key)
    if not forecast: return None, None

    daily = forecast.get("daily", {})
    temps_max = daily.get("temperature_2m_max", [])
    temps_min = daily.get("temperature_2m_min", [])
    dates = daily.get("time", [])

    # Determine which forecast day to use.
    # Parse date references: "tomorrow", "monday", "tuesday", specific dates, etc.
    day_idx = 0  # default: today
    if "tomorrow" in title:
        day_idx = 1
    elif "day after tomorrow" in title:
        day_idx = 2
    else:
        # Try to match day-of-week names
        today_dt = datetime.now(timezone.utc)
        day_names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        for i, day_name in enumerate(day_names):
            if day_name in title:
                current_dow = today_dt.weekday()  # 0=Monday
                target_dow = i
                delta = (target_dow - current_dow) % 7
                if delta == 0:
                    delta = 7  # next week if same day mentioned
                day_idx = delta
                break
        # Try to match specific date patterns like "April 8", "4/8"
        date_match = re.search(r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+(\d{1,2})', title)
        if date_match:
            try:
                month_abbr = date_match.group(1)[:3]
                day_num = int(date_match.group(2))
                month_map = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
                             "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}
                target_month = month_map.get(month_abbr, today_dt.month)
                target_year = today_dt.year
                # Handle year rollover (e.g., Dec asking about Jan)
                target_date = None
                for yr in (target_year, target_year + 1):
                    try:
                        target_date = datetime(yr, target_month, day_num, tzinfo=timezone.utc)
                        if target_date >= today_dt:
                            break
                    except ValueError:
                        continue
                if target_date:
                    delta_days = (target_date.date() - today_dt.date()).days
                    if 0 <= delta_days < 7:
                        day_idx = delta_days
            except: pass

    if day_idx >= len(temps_max): return None, None

    forecast_high = temps_max[day_idx]
    forecast_low = temps_min[day_idx]

    # Forecast error model: accuracy degrades with forecast horizon
    # Day 0 (today): ~2°F error, Day 1: ~3°F, Day 3: ~4.5°F, Day 7: ~6°F
    forecast_sigma = 2.0 + day_idx * 0.6  # linear increase in uncertainty

    if is_above:
        # "Will temp be above X?" — compare forecast high to threshold
        diff = forecast_high - threshold
        prob_yes = 1 / (1 + math.exp(-diff / forecast_sigma))
    else:
        diff = threshold - forecast_low
        prob_yes = 1 / (1 + math.exp(-diff / forecast_sigma))

    prob_yes = max(0.02, min(0.98, prob_yes))
    print(f"[info] Weather: {city_key} day={day_idx} forecast_high={forecast_high:.0f}°F "
          f"threshold={threshold:.0f}°F ({'above' if is_above else 'below'}) "
          f"sigma={forecast_sigma:.1f}°F → {prob_yes:.2f}")
    return prob_yes, f"weather:{city_key}_{dates[day_idx]}"

# ── 3a. TOMORROW.IO — premium weather forecasts (500 calls/day) ─────────────
_TOMORROW_CACHE = {}  # {city_key: (data, timestamp)} — 30-min TTL to stay under 500/day
_TOMORROW_TTL = 1800  # 30 minutes — 9 cities × 48 fetches/day = 432 calls (under 500)

def get_tomorrow_forecast(city_key):
    """Fetch forecast from Tomorrow.io (formerly Climacell). Returns dict with
    daily highs/lows in Fahrenheit, or None on failure."""
    if not TOMORROW_API_KEY:
        return None
    city = WEATHER_CITIES.get(city_key)
    if not city:
        return None
    # Use dedicated long-TTL cache to stay within 500 calls/day
    now = time.time()
    if city_key in _TOMORROW_CACHE:
        cached_data, cached_ts = _TOMORROW_CACHE[city_key]
        if now - cached_ts < _TOMORROW_TTL:
            return cached_data
    url = (f"https://api.tomorrow.io/v4/weather/forecast?"
           f"location={city['lat']},{city['lon']}"
           f"&timesteps=1d"
           f"&units=imperial"
           f"&apikey={TOMORROW_API_KEY}")
    data = _cached_get(f"tomorrow_{city_key}", url, timeout=8)
    if not data:
        return None
    try:
        daily = data.get("timelines", {}).get("daily", [])
        if not daily:
            return None
        # Normalize to same structure as Open-Meteo for reuse
        result = {"daily": {
            "temperature_2m_max": [],
            "temperature_2m_min": [],
            "time": [],
        }}
        for day in daily[:7]:
            values = day.get("values", {})
            high = values.get("temperatureMax")
            low = values.get("temperatureMin")
            date_str = day.get("time", "")[:10]
            if high is not None and low is not None:
                result["daily"]["temperature_2m_max"].append(high)
                result["daily"]["temperature_2m_min"].append(low)
                result["daily"]["time"].append(date_str)
        parsed = result if result["daily"]["temperature_2m_max"] else None
        _TOMORROW_CACHE[city_key] = (parsed, time.time())
        return parsed
    except Exception as e:
        print(f"[tomorrow] Parse error for {city_key}: {e}")
        return None

def get_tomorrow_weather_estimate(ticker, market_data):
    """Tomorrow.io weather source — same logic as Open-Meteo but different data provider.
    Acts as redundant backup + cross-validation for weather markets."""
    title = (market_data.get("title") or market_data.get("subtitle") or "").lower()
    ticker_upper = ticker.upper() if ticker else ""
    weather_keywords = ["temperature", "temp", "°f", "°c", "degrees", "high", "low",
                        "weather", "heat", "cold", "freeze", "highest temperature"]
    is_weather = any(kw in title for kw in weather_keywords) or "KXHIGH" in ticker_upper
    if not is_weather:
        return None, None

    # Reuse the same city/threshold parsing from get_weather_estimate
    _TICKER_CITY_MAP = {
        "KXHIGHNY": "nyc", "KXHIGHLAX": "los angeles", "KXHIGHCHI": "chicago",
        "KXHIGHMIA": "miami", "KXHIGHAUS": "austin", "KXHIGHHOU": "houston",
        "KXHIGHPHX": "phoenix", "KXHIGHDEN": "denver", "KXHIGHSF": "san francisco",
    }
    city_key = None
    for prefix, city in _TICKER_CITY_MAP.items():
        if prefix in ticker_upper:
            city_key = city; break
    if not city_key:
        for key in WEATHER_CITIES:
            if key in title:
                city_key = key; break
    if not city_key:
        return None, None

    # Extract threshold (same regex as Open-Meteo source)
    temp_match = re.search(r'(at or above|at or below|above|below|over|under|at least|exceed)\s+(\d+\.?\d*)', title)
    if not temp_match:
        tick_match = re.search(r'-[TB](-?\d+\.?\d*)', ticker)
        if tick_match:
            threshold = float(tick_match.group(1))
            is_above = True
        else:
            return None, None
    else:
        direction = temp_match.group(1)
        threshold = float(temp_match.group(2))
        is_above = direction in ("above", "over", "at least", "exceed", "at or above")

    if threshold < -40 or threshold > 140:
        return None, None

    forecast = get_tomorrow_forecast(city_key)
    if not forecast:
        return None, None

    daily = forecast.get("daily", {})
    temps_max = daily.get("temperature_2m_max", [])
    temps_min = daily.get("temperature_2m_min", [])
    dates = daily.get("time", [])

    # Determine forecast day index (same logic as Open-Meteo)
    day_idx = 0
    if "tomorrow" in title:
        day_idx = 1
    else:
        today_dt = datetime.now(timezone.utc)
        day_names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        for i, day_name in enumerate(day_names):
            if day_name in title:
                current_dow = today_dt.weekday()
                target_dow = i
                delta = (target_dow - current_dow) % 7
                if delta == 0:
                    delta = 7
                day_idx = delta
                break
        date_match = re.search(r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+(\d{1,2})', title)
        if date_match:
            try:
                month_abbr = date_match.group(1)[:3]
                day_num = int(date_match.group(2))
                month_map = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
                             "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}
                target_month = month_map.get(month_abbr, today_dt.month)
                for yr in (today_dt.year, today_dt.year + 1):
                    try:
                        target_date = datetime(yr, target_month, day_num, tzinfo=timezone.utc)
                        if target_date >= today_dt:
                            day_idx = (target_date.date() - today_dt.date()).days
                            break
                    except ValueError:
                        continue
            except Exception:
                pass

    if day_idx >= len(temps_max):
        return None, None

    forecast_high = temps_max[day_idx]
    forecast_low = temps_min[day_idx]
    forecast_sigma = 2.0 + day_idx * 0.6

    if is_above:
        diff = forecast_high - threshold
        prob_yes = 1 / (1 + math.exp(-diff / forecast_sigma))
    else:
        diff = threshold - forecast_low
        prob_yes = 1 / (1 + math.exp(-diff / forecast_sigma))

    prob_yes = max(0.02, min(0.98, prob_yes))
    print(f"[tomorrow] Weather: {city_key} day={day_idx} high={forecast_high:.0f}°F "
          f"threshold={threshold:.0f}°F ({'above' if is_above else 'below'}) → {prob_yes:.2f}")
    return prob_yes, f"tomorrow:{city_key}_{dates[day_idx] if day_idx < len(dates) else '?'}"

# ── 3b. NOAA WEATHER ALERTS — severe weather events (free, no auth) ─────────
def get_noaa_alerts_for_market(ticker, market_data):
    """Check NOAA active alerts for weather-event markets.
    Catches hurricane, tornado, heat wave, freeze, and extreme weather markets
    that go beyond simple temperature forecasts.
    Returns (adjusted_probability, source_desc) or (None, None)."""
    title = (market_data.get("title") or market_data.get("subtitle") or "").lower()

    # Map alert-type keywords to NOAA alert event types
    alert_keywords = {
        "hurricane": ["Hurricane", "Tropical Storm"],
        "tornado": ["Tornado"],
        "heat wave": ["Excessive Heat", "Heat Advisory"],
        "heat": ["Excessive Heat", "Heat Advisory"],
        "freeze": ["Freeze Warning", "Frost Advisory", "Hard Freeze"],
        "frost": ["Freeze Warning", "Frost Advisory"],
        "blizzard": ["Blizzard", "Winter Storm"],
        "snow": ["Winter Storm", "Winter Weather Advisory"],
        "flood": ["Flood", "Flash Flood"],
        "wildfire": ["Fire Weather", "Red Flag Warning"],
    }

    matched_events = None
    for kw, events in alert_keywords.items():
        if kw in title:
            matched_events = events
            break

    if not matched_events:
        return None, None

    # Determine geographic scope — check for state/region mentions
    # NOAA alerts API supports area codes (state abbreviations)
    state_map = {
        "florida": "FL", "texas": "TX", "california": "CA", "new york": "NY",
        "louisiana": "LA", "mississippi": "MS", "alabama": "AL", "georgia": "GA",
        "north carolina": "NC", "south carolina": "SC", "virginia": "VA",
        "oklahoma": "OK", "kansas": "KS", "nebraska": "NE", "iowa": "IA",
        "colorado": "CO", "arizona": "AZ", "nevada": "NV", "oregon": "OR",
        "washington": "WA", "illinois": "IL", "ohio": "OH", "michigan": "MI",
        "pennsylvania": "PA", "new jersey": "NJ", "massachusetts": "MA",
    }
    area = None
    for state_name, code in state_map.items():
        if state_name in title:
            area = code
            break

    # Fetch active alerts
    cache_key = f"noaa_alerts_{area or 'US'}"
    url = "https://api.weather.gov/alerts/active?status=actual&message_type=alert"
    if area:
        url += f"&area={area}"
    else:
        url += "&limit=50"

    alerts_data = _cached_get(cache_key, url, timeout=8)
    if not alerts_data:
        return None, None

    features = alerts_data.get("features", [])
    if not features:
        # No active alerts — return None so we don't pollute the ensemble with a guess
        return None, None

    # Count matching alerts
    matching = 0
    for feat in features:
        props = feat.get("properties", {})
        event = props.get("event", "")
        if any(me.lower() in event.lower() for me in matched_events):
            matching += 1

    if matching > 0:
        # Active alerts exist → high probability (scaled by count)
        prob = min(0.90, 0.60 + matching * 0.10)
        print(f"[noaa] {matching} active '{matched_events[0]}' alerts "
              f"{'in ' + area if area else 'nationwide'} → prob={prob:.2f}")
        return prob, f"noaa:{matching}alerts:{matched_events[0][:20]}"
    else:
        # Active alerts exist but none match — not informative, return None
        return None, None

# ── 4. FRED — economic indicators (requires free API key) ────────────────────
FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
BLS_API_KEY = os.environ.get("BLS_API_KEY", "")
BEA_API_KEY = os.environ.get("BEA_API_KEY", "")
CENSUS_API_KEY = os.environ.get("CENSUS_API_KEY", "")
EIA_API_KEY = os.environ.get("EIA_API_KEY", "")
TOMORROW_API_KEY = os.environ.get("TOMORROW_API_KEY", "")

# Key series for Kalshi economic markets
FRED_SERIES = {
    "cpi":           "CPIAUCSL",
    "core_cpi":      "CPILFESL",
    "unemployment":  "UNRATE",
    "nonfarm":       "PAYEMS",
    "gdp":           "GDPC1",
    "fed_funds":     "FEDFUNDS",
}

def get_fred_latest(series_id):
    if not FRED_API_KEY: return None
    url = (f"https://api.stlouisfed.org/fred/series/observations?"
           f"series_id={series_id}&api_key={FRED_API_KEY}&file_type=json"
           f"&sort_order=desc&limit=5")
    data = _cached_get(f"fred_{series_id}", url, timeout=5)
    if not data: return None
    obs = data.get("observations", [])
    for o in obs:
        val = o.get("value", ".")
        if val != ".":
            return {"value": float(val), "date": o.get("date", "")}
    return None

def _get_fed_rate_expectations():
    """Fetch market-implied Fed rate expectations.

    Uses FRED target range (DFEDTARU/DFEDTARL) plus attempts to get
    forward-looking expectations from the Atlanta Fed Market Probability Tracker.

    Returns dict with current rate info and expected rate path,
    or None if data unavailable. Cached for 4 hours.
    """
    cache_key = "fed_rate_expectations"
    # Check in-memory cache directly (don't pass None to _cached_get — causes MissingSchema)
    if cache_key in _CACHE:
        cached, cached_ts = _CACHE[cache_key]
        if isinstance(cached, dict) and cached.get("_ts", 0) > time.time() - 14400:
            return cached

    result = {"current_rate": None, "target_upper": None, "target_lower": None,
              "market_expectations": {}, "_ts": time.time()}

    # 1. Get current effective rate
    eff = get_fred_latest("DFF")
    if eff:
        result["current_rate"] = eff["value"]

    # 2. Get target range
    upper = get_fred_latest("DFEDTARU")
    lower = get_fred_latest("DFEDTARL")
    if upper:
        result["target_upper"] = upper["value"]
    if lower:
        result["target_lower"] = lower["value"]

    # 3. Try to get market-implied expectations from Atlanta Fed
    try:
        url = "https://www.atlantafed.org/cenfis/market-probability-tracker"
        # The Atlanta Fed page has rate probabilities but isn't a clean API.
        # Instead, use a heuristic based on fed funds futures:
        # Current market consensus (as of early 2026) expects ~2-3 rate cuts by end of 2026.
        # We encode this as expected rate path assumptions that get updated by FRED data.
        pass
    except Exception:
        pass

    # 4. Build expected rate path based on FRED data + market consensus
    # FOMC meeting schedule 2026 (approximate months)
    fomc_months = {
        "2026-01": 0, "2026-03": 1, "2026-05": 2, "2026-06": 3,
        "2026-07": 4, "2026-09": 5, "2026-10": 6, "2026-12": 7,
    }
    current = result["current_rate"] or result["target_upper"] or 4.33
    # Market currently prices ~2-3 cuts by end 2026 (each cut = 0.25%)
    # We model this as gradual decline with uncertainty widening over time
    for month_key, meeting_idx in fomc_months.items():
        # Expected cuts increase over time, with uncertainty
        expected_cuts = meeting_idx * 0.35  # ~0.35 cuts per meeting on average
        expected_rate = current - (expected_cuts * 0.25)
        uncertainty = 0.15 + meeting_idx * 0.08  # uncertainty widens with time
        result["market_expectations"][month_key] = {
            "expected_rate": max(expected_rate, 0),
            "uncertainty_pct": uncertainty,
        }

    # Cache result
    _CACHE[cache_key] = (result, time.time())
    return result

def get_fred_estimate(ticker, market_data):
    title = (market_data.get("title") or market_data.get("subtitle") or "").lower()

    # Detect economic market type — check both title keywords AND ticker prefix
    series_id = None
    indicator = None
    ticker_upper = (ticker or "").upper()
    if any(w in title for w in ["cpi", "inflation", "consumer price"]) or "KXCPI" in ticker_upper:
        series_id = FRED_SERIES["cpi"]; indicator = "cpi"
    elif any(w in title for w in ["unemployment", "jobless"]) or "KXJOB" in ticker_upper:
        series_id = FRED_SERIES["unemployment"]; indicator = "unemployment"
    elif any(w in title for w in ["nonfarm", "payroll", "jobs added", "jobs report"]):
        series_id = FRED_SERIES["nonfarm"]; indicator = "nonfarm"
    elif any(w in title for w in ["gdp", "gross domestic"]) or "KXGDP" in ticker_upper:
        series_id = FRED_SERIES["gdp"]; indicator = "gdp"
    elif any(w in title for w in ["fed funds", "federal funds", "interest rate", "fomc"]) or "KXFED" in ticker_upper:
        series_id = FRED_SERIES["fed_funds"]; indicator = "fed_funds"
    elif "KXISMPMI" in ticker_upper:
        # ISM PMI isn't in FRED_SERIES but uses a related indicator
        series_id = "MANEMP"; indicator = "ism_pmi"  # Manufacturing employment as proxy
    else:
        return None, None

    latest = get_fred_latest(series_id)
    if not latest: return None, None

    # Extract threshold from title, falling back to ticker suffix (-T0.3 etc.)
    thresh_match = re.search(r'(at or above|at or below|above|below|over|under|at least|exceed|less than)\s+(\d[\d,]*\.?\d*)\s*%?', title)
    if not thresh_match:
        tick_match = re.search(r'-T(-?\d+\.?\d*)', ticker)
        if tick_match:
            threshold = float(tick_match.group(1))
            is_above = True  # -T suffix markets are "at or above" by default
        else:
            return None, None
    else:
        direction = thresh_match.group(1)
        threshold = float(thresh_match.group(2).replace(",", ""))
        is_above = direction in ("above", "over", "at least", "exceed", "at or above")

    current = latest["value"]

    # For CPI: Kalshi markets reference monthly % change, not the raw index level.
    # Compute month-over-month change from the last two FRED observations.
    if indicator == "cpi":
        obs = _cached_get(f"fred_{series_id}", None)  # check cache from get_fred_latest
        if obs is None:
            # Re-fetch with more observations to get prior month
            cpi_url = (f"https://api.stlouisfed.org/fred/series/observations?"
                       f"series_id={series_id}&api_key={FRED_API_KEY}&file_type=json"
                       f"&sort_order=desc&limit=5")
            obs = _cached_get(f"fred_{series_id}_mom", cpi_url, timeout=5)
        if obs and isinstance(obs, dict):
            observations = obs.get("observations", [])
            valid_obs = [o for o in observations if o.get("value", ".") != "."]
            if len(valid_obs) >= 2:
                curr_val = float(valid_obs[0]["value"])
                prev_val = float(valid_obs[1]["value"])
                if prev_val > 0:
                    monthly_pct = ((curr_val - prev_val) / prev_val) * 100
                    current = round(monthly_pct, 2)
                    print(f"[fred] CPI monthly change: {prev_val:.1f} → {curr_val:.1f} = {current:+.2f}%")

    # ── Enhanced Fed Funds estimation using rate expectations ──
    if indicator == "fed_funds":
        expectations = _get_fed_rate_expectations()
        days = _days_to_expiry(market_data)

        if expectations and days is not None:
            # Find the closest FOMC meeting to this market's expiry
            expiry_date = None
            close_time = market_data.get("close_time") or market_data.get("expiration_time")
            if close_time:
                try:
                    expiry_date = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                except Exception:
                    pass

            expected_rate = current
            uncertainty = 0.20

            if expiry_date:
                expiry_month = expiry_date.strftime("%Y-%m")
                # Find nearest expectation
                best_match = None
                for month_key, exp_data in expectations.get("market_expectations", {}).items():
                    if month_key <= expiry_month:
                        best_match = exp_data
                    elif best_match is None:
                        best_match = exp_data

                if best_match:
                    expected_rate = best_match["expected_rate"]
                    uncertainty = best_match["uncertainty_pct"]

            # Calculate probability using normal distribution approximation
            # P(rate >= threshold) using expected rate and uncertainty
            rate_diff = expected_rate - threshold
            # Uncertainty in percentage points (e.g., 0.20 = 20bp)
            sigma = max(uncertainty * expected_rate, 0.15)  # min 15bp uncertainty

            # Standard normal CDF approximation
            z = rate_diff / sigma if sigma > 0 else 0
            # Clamp z to prevent extreme probabilities
            z = max(-3.0, min(3.0, z))

            if is_above:
                # P(rate >= threshold) — positive z = more likely above
                prob_yes = 1 / (1 + math.exp(-1.7 * z))
            else:
                # P(rate < threshold) — negative z = more likely below
                prob_yes = 1 / (1 + math.exp(1.7 * z))

            # Clamp to [0.05, 0.95] — never fully certain about Fed
            prob_yes = max(0.05, min(0.95, prob_yes))

            days_str = f" days={days:.1f}" if days else ""
            print(f"[info] FRED: {indicator}={current} expected={expected_rate:.2f} "
                  f"threshold={threshold} {'above' if is_above else 'below'} "
                  f"sigma={sigma:.2f}{days_str} → {prob_yes:.2f}")
            return prob_yes, f"fred:{indicator}={current}→exp={expected_rate:.2f}"

    # ── Standard estimation for non-fed-funds indicators ──
    if indicator == "nonfarm":
        diff = (current - threshold) / max(abs(threshold), 1)
    else:
        diff = (current - threshold) / max(abs(threshold), 0.1)

    if not is_above:
        diff = -diff

    days = _days_to_expiry(market_data)
    if days is not None and days > 0:
        k = 5.0 / math.sqrt(max(days, 1.0))
    else:
        k = 2

    prob_yes = max(0.02, min(0.98, 1 / (1 + math.exp(-diff * k))))
    days_str = f" days={days:.1f}" if days else ""
    print(f"[info] FRED: {indicator}={current} threshold={threshold} "
          f"{'above' if is_above else 'below'} k={k:.1f}{days_str} → {prob_yes:.2f}")
    return prob_yes, f"fred:{indicator}={current}"

# ── 4b. CLEVELAND FED INFLATION NOWCAST — real-time CPI estimates ────────────
def get_cleveland_fed_nowcast(ticker, market_data):
    """Cleveland Fed Inflation Nowcast provides real-time CPI estimates that are
    much more current than FRED's lagging releases. Free public data.
    Only fires for CPI/inflation markets."""
    title = (market_data.get("title") or market_data.get("subtitle") or "").lower()
    if not any(w in title for w in ["cpi", "inflation", "consumer price"]):
        return None, None

    # Cleveland Fed Inflation Nowcast endpoints are all returning 404 as of April 2026.
    # Disabled until working endpoints are found. Return fast None so pipeline health
    # doesn't penalize this source (latency < 100ms → attempt count undone).
    return None, None

    # Dead code preserved for when/if endpoints come back:
    cache_key = "cleveland_fed_nowcast"
    clevfed_urls = [
        "https://www.clevelandfed.org/api/InflationNowcasting/GetInflationNowcast",
        "https://www.clevelandfed.org/api/InflationNowcasting/InflationNowcast",
        "https://www.clevelandfed.org/api/cpi-nowcast",
    ]
    data = None
    for i, url in enumerate(clevfed_urls):
        data = _cached_get(f"{cache_key}_{i}", url, timeout=8)
        if data:
            break
    if not data:
        return None, None

    # Parse the nowcast data — format varies, try to extract latest CPI estimate
    try:
        nowcast_cpi = None
        if isinstance(data, dict):
            # Look for the CPI nowcast value
            for key in ["cpiNowcast", "nowcast", "medianCPI", "value"]:
                if key in data:
                    nowcast_cpi = float(data[key])
                    break
            if nowcast_cpi is None and "data" in data:
                items = data["data"]
                if isinstance(items, list) and items:
                    last = items[-1]
                    nowcast_cpi = float(last.get("value") or last.get("cpi") or 0)
        elif isinstance(data, list) and data:
            last = data[-1]
            nowcast_cpi = float(last.get("value") or last.get("cpi") or 0)

        if nowcast_cpi is None or nowcast_cpi == 0:
            return None, None

        # Extract threshold from title
        thresh_match = re.search(r'(above|below|over|under|at least|exceed|less than)\s+(\d[\d,]*\.?\d*)\s*%?', title)
        if not thresh_match:
            return None, None
        direction = thresh_match.group(1)
        threshold = float(thresh_match.group(2).replace(",", ""))
        is_above = direction in ("above", "over", "at least", "exceed")

        diff = (nowcast_cpi - threshold) / max(abs(threshold), 0.1)
        if not is_above:
            diff = -diff

        # Nowcast is quite accurate for near-term — use moderate k
        prob_yes = max(0.02, min(0.98, 1 / (1 + math.exp(-diff * 4))))
        print(f"[clevfed] CPI nowcast={nowcast_cpi:.2f}% threshold={threshold}% "
              f"{'above' if is_above else 'below'} → {prob_yes:.2f}")
        return prob_yes, f"clevfed_nowcast:{nowcast_cpi:.2f}%"

    except Exception as e:
        print(f"[clevfed] Parse error: {e}")
        return None, None

# ── 4c. BLS — Bureau of Labor Statistics (free, 500 req/day with key) ────────
# Redundant backup for FRED on CPI, unemployment, nonfarm payroll.
# BLS Series IDs: CPI-U = CUSR0000SA0, Unemployment = LNS14000000, Nonfarm = CES0000000001
_BLS_SERIES = {
    "cpi":          "CUSR0000SA0",      # CPI-U All Items (seasonally adjusted)
    "core_cpi":     "CUSR0000SA0L1E",   # CPI-U Less Food & Energy
    "unemployment": "LNS14000000",      # Unemployment Rate (seasonally adjusted)
    "nonfarm":      "CES0000000001",    # Total Nonfarm Employment (thousands)
}

def get_bls_latest(series_id):
    """Fetch latest observation from BLS API v2. Returns dict with 'value' and 'date', or None."""
    if not BLS_API_KEY:
        return None
    try:
        import json as _json
        url = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
        now_year = datetime.now(timezone.utc).year
        payload = {
            "seriesid": [series_id],
            "startyear": str(now_year - 1),
            "endyear": str(now_year),
            "registrationkey": BLS_API_KEY,
        }
        _rate_limit_wait(url)
        r = requests.post(url, json=payload, timeout=10,
                          headers={"Content-Type": "application/json"})
        if r.status_code != 200:
            print(f"[bls] HTTP {r.status_code} for {series_id}")
            return None
        data = r.json()
        if data.get("status") != "REQUEST_SUCCEEDED":
            print(f"[bls] API error: {data.get('message', ['?'])}")
            return None
        series_data = data.get("Results", {}).get("series", [])
        if not series_data:
            return None
        observations = series_data[0].get("data", [])
        if not observations:
            return None
        # BLS returns newest first
        latest = observations[0]
        val_str = latest.get("value", "")
        if not val_str:
            return None
        val = float(val_str)
        period = latest.get("period", "")  # e.g. "M03" for March
        year = latest.get("year", "")
        date_str = f"{year}-{period[1:]}" if period.startswith("M") else f"{year}-{period}"
        print(f"[bls] {series_id}: {val} ({date_str})")
        return {"value": val, "date": date_str}
    except Exception as e:
        print(f"[bls] Error fetching {series_id}: {e}")
        return None

def get_bls_estimate(ticker, market_data):
    """BLS data source — backup for FRED on CPI, unemployment, nonfarm payroll.
    Uses the same probability estimation logic as get_fred_estimate but with
    BLS API as the data provider."""
    title = (market_data.get("title") or market_data.get("subtitle") or "").lower()
    ticker_upper = (ticker or "").upper()

    # Detect indicator type
    series_id = None
    indicator = None
    if any(w in title for w in ["cpi", "inflation", "consumer price"]) or "KXCPI" in ticker_upper:
        series_id = _BLS_SERIES["cpi"]; indicator = "cpi"
    elif any(w in title for w in ["unemployment", "jobless"]) or "KXJOB" in ticker_upper:
        series_id = _BLS_SERIES["unemployment"]; indicator = "unemployment"
    elif any(w in title for w in ["nonfarm", "payroll", "jobs added", "jobs report"]):
        series_id = _BLS_SERIES["nonfarm"]; indicator = "nonfarm"
    else:
        return None, None  # BLS doesn't cover fed funds, GDP, etc.

    latest = get_bls_latest(series_id)
    if not latest:
        return None, None

    # Extract threshold from title, falling back to ticker suffix (-T0.3 etc.)
    thresh_match = re.search(
        r'(at or above|at or below|above|below|over|under|at least|exceed|less than)\s+(\d[\d,]*\.?\d*)\s*%?', title)
    if not thresh_match:
        tick_match = re.search(r'-T(-?\d+\.?\d*)', ticker)
        if tick_match:
            threshold = float(tick_match.group(1))
            is_above = True
        else:
            return None, None
    else:
        direction = thresh_match.group(1)
        threshold = float(thresh_match.group(2).replace(",", ""))
        is_above = direction in ("above", "over", "at least", "exceed", "at or above")

    current = latest["value"]

    # For CPI: Kalshi markets reference monthly % change, not the raw index.
    # BLS returns the CPI-U index level (e.g., 330.293). Compute month-over-month change.
    if indicator == "cpi":
        # Fetch prior month by requesting 2 years of data (we already have the latest)
        try:
            import json as _json
            now_year = datetime.now(timezone.utc).year
            payload = {
                "seriesid": [series_id],
                "startyear": str(now_year - 1),
                "endyear": str(now_year),
                "registrationkey": BLS_API_KEY,
            }
            cache_key = f"bls_mom_{series_id}"
            if cache_key in _CACHE and time.time() - _CACHE[cache_key][1] < CACHE_TTL:
                prev_val = _CACHE[cache_key][0]
            else:
                _rate_limit_wait("https://api.bls.gov/publicAPI/v2/timeseries/data/")
                r = requests.post("https://api.bls.gov/publicAPI/v2/timeseries/data/",
                                  json=payload, timeout=10,
                                  headers={"Content-Type": "application/json"})
                bls_data = r.json()
                obs = bls_data.get("Results", {}).get("series", [{}])[0].get("data", [])
                if len(obs) >= 2:
                    prev_val = float(obs[1]["value"])  # second newest
                    _CACHE[cache_key] = (prev_val, time.time())
                else:
                    prev_val = None

            if prev_val and prev_val > 0:
                monthly_pct = ((current - prev_val) / prev_val) * 100
                print(f"[bls] CPI monthly change: {prev_val:.1f} → {current:.1f} = {monthly_pct:+.2f}%")
                current = round(monthly_pct, 2)
            else:
                return None, None  # can't compute change without prior month
        except Exception as e:
            print(f"[bls] Error computing CPI monthly change: {e}")
            return None, None

    # Probability estimation (same logic as FRED)
    if indicator == "nonfarm":
        diff = (current - threshold) / max(abs(threshold), 1)
    else:
        diff = (current - threshold) / max(abs(threshold), 0.1)
    if not is_above:
        diff = -diff

    days = _days_to_expiry(market_data)
    if days is not None and days > 0:
        k = 5.0 / math.sqrt(max(days, 1.0))
    else:
        k = 2

    prob_yes = max(0.02, min(0.98, 1 / (1 + math.exp(-diff * k))))
    days_str = f" days={days:.1f}" if days else ""
    print(f"[bls] {indicator}={current} threshold={threshold} "
          f"{'above' if is_above else 'below'} k={k:.1f}{days_str} → {prob_yes:.2f}")
    return prob_yes, f"bls:{indicator}={current}"

# ── 5. SPORTS — The-Odds-API (free tier, 500 credits/month) ──────────────────
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")

SPORT_KEYS = {
    "nba": "basketball_nba",  "nfl": "americanfootball_nfl",
    "mlb": "baseball_mlb",    "nhl": "icehockey_nhl",
    "ncaa": "americanfootball_ncaaf", "mls": "soccer_usa_mls",
    "epl": "soccer_epl",      "nascar": "motorsport_nascar_cup",
}

_ODDS_CACHE = {}  # {sport_label: (data, timestamp)}

def _load_sport_odds(sport_label, market_types="h2h,totals,spreads"):
    """Load odds for a SINGLE sport on demand. Cache per-sport for 30 min to save API credits.
    Now fetches h2h, totals, and spreads in a single call (1 credit, 3 market types).
    Old approach loaded all 8 sports at once, burning ~8 credits per cache miss.
    With 500 credits/month free tier, that exhausted the budget in 1-2 days."""
    now = time.time()
    cache_key = f"{sport_label}_{market_types}"
    if cache_key in _ODDS_CACHE:
        data, ts = _ODDS_CACHE[cache_key]
        if now - ts < 1800:  # 30 min cache
            return data
    if not ODDS_API_KEY:
        return []
    sport_key = SPORT_KEYS.get(sport_label)
    if not sport_key:
        return []
    try:
        url = (f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds?"
               f"apiKey={ODDS_API_KEY}&regions=us&markets={market_types}&oddsFormat=decimal")
        _rate_limit_wait(url)
        r = requests.get(url, timeout=8)
        if r.status_code == 200:
            data = r.json()
            _ODDS_CACHE[cache_key] = (data, now)
            # Also cache under the base label for backwards compat
            _ODDS_CACHE[sport_label] = (data, now)
            print(f"[odds] Loaded {len(data)} games for {sport_label} (markets={market_types})")
            return data
    except Exception as e:
        print(f"[odds] Failed to load {sport_label}: {e}")
    return []

def _load_sports_odds():
    """Compat wrapper — returns dict of all cached sports data."""
    return {label: data for label, (data, ts) in _ODDS_CACHE.items()}

def get_sports_estimate(ticker, market_data):
    title = (market_data.get("title") or market_data.get("subtitle") or "").lower()

    # Detect sport from title keywords first (free — no API call)
    sport = None
    for label in SPORT_KEYS:
        if label in title: sport = label; break

    # If no sport keyword found, don't do expensive team-name search across all sports.
    # Only scan cached sports data (no new API calls).
    if not sport:
        cached = _load_sports_odds()
        for label, games in cached.items():
            for game in games:
                home = (game.get("home_team") or "").lower()
                away = (game.get("away_team") or "").lower()
                if (home and home in title) or (away and away in title):
                    sport = label; break
            if sport: break

    if not sport: return None, None

    # Lazy-load only the matched sport (saves API credits)
    games = _load_sport_odds(sport)
    if not games: return None, None

    # Find matching game
    title_words = set(re.findall(r'\w+', title))
    best_game = None
    best_overlap = 0

    for game in games:
        home = (game.get("home_team") or "").lower()
        away = (game.get("away_team") or "").lower()
        game_words = set(re.findall(r'\w+', f"{home} {away}"))
        overlap = len(title_words & game_words)
        if overlap > best_overlap:
            best_overlap = overlap
            best_game = game

    if not best_game or best_overlap < 2:
        return None, None

    bookmakers = best_game.get("bookmakers", [])
    if not bookmakers: return None, None

    # ── Detect market type from Kalshi title ─────────────────────────────
    # "Will the total be over 210?" → totals market
    # "Will Lakers win by more than 5?" → spreads market
    # "Will Lakers win?" → h2h market
    totals_match = re.search(r'(over|under|total|combined)\s+(\d+\.?\d*)', title)
    spread_match = re.search(r'(spread|by more than|by at least|margin)\s+(\d+\.?\d*)', title)

    prob = None
    detail_str = ""

    if totals_match:
        # ── Totals market ────────────────────────────────────────────────
        direction = totals_match.group(1)
        threshold = float(totals_match.group(2))
        is_over = direction in ("over", "total", "combined")

        for bm in bookmakers:
            for mkt in bm.get("markets", []):
                if mkt.get("key") == "totals":
                    for outcome in mkt.get("outcomes", []):
                        point = float(outcome.get("point", 0))
                        price = float(outcome.get("price", 0))
                        name = (outcome.get("name") or "").lower()
                        # Match the closest point to our threshold
                        if abs(point - threshold) <= 1.5 and price > 0:
                            impl_prob = 1 / price
                            if (is_over and name == "over") or (not is_over and name == "under"):
                                prob = impl_prob
                                detail_str = f"totals:{point}"
                                break
                    if prob: break
            if prob: break

        # Normalize if we got a probability > 1 (overround)
        if prob and prob > 0.99:
            prob = 0.95

    elif spread_match:
        # ── Spreads market ───────────────────────────────────────────────
        spread_val = float(spread_match.group(2))
        home = (best_game.get("home_team") or "").lower()
        away = (best_game.get("away_team") or "").lower()
        target_team = home if home in title else (away if away in title else None)

        if target_team:
            for bm in bookmakers:
                for mkt in bm.get("markets", []):
                    if mkt.get("key") == "spreads":
                        for outcome in mkt.get("outcomes", []):
                            name = (outcome.get("name") or "").lower()
                            point = abs(float(outcome.get("point", 0)))
                            price = float(outcome.get("price", 0))
                            if name == target_team and abs(point - spread_val) <= 1.5 and price > 0:
                                prob = 1 / price
                                detail_str = f"spreads:{target_team}@{point}"
                                break
                        if prob: break
                if prob: break

            if prob and prob > 0.99:
                prob = 0.95

    if prob is None:
        # ── H2H (moneyline) market — original logic ─────────────────────
        h2h = None
        for bm in bookmakers:
            for mkt in bm.get("markets", []):
                if mkt.get("key") == "h2h":
                    h2h = mkt.get("outcomes", [])
                    break
            if h2h: break

        if not h2h or len(h2h) < 2: return None, None

        probs = {}
        total_impl = 0
        for outcome in h2h:
            name = (outcome.get("name") or "").lower()
            price = float(outcome.get("price", 0))
            if price > 0:
                impl = 1 / price
                probs[name] = impl
                total_impl += impl
        if total_impl > 0:
            for name in probs:
                probs[name] /= total_impl

        home = (best_game.get("home_team") or "").lower()
        away = (best_game.get("away_team") or "").lower()
        if home in title:
            prob = probs.get(home, probs.get(best_game.get("home_team","").lower()))
        elif away in title:
            prob = probs.get(away, probs.get(best_game.get("away_team","").lower()))
        else:
            print(f"[odds] Can't determine team for '{title[:50]}' — skipping")
            return None, None
        detail_str = "h2h"

    if prob is None: return None, None

    teams = f"{best_game.get('home_team','')} vs {best_game.get('away_team','')}"
    print(f"[odds] Match: '{title[:50]}' → {teams} ({detail_str}) prob={prob:.2f}")
    return prob, f"odds:{teams[:30]}:{detail_str}"

# ── 6. METACULUS — community prediction aggregation ──────────────────────────
_METACULUS_CACHE = {}
_METACULUS_TS = 0

METACULUS_API_TOKEN = os.environ.get("METACULUS_API_TOKEN", "")

def _load_metaculus():
    """Fetch active binary Metaculus questions. Cache 10 min.
    As of 2025+, Metaculus API requires authentication."""
    global _METACULUS_CACHE, _METACULUS_TS
    now = time.time()
    if _METACULUS_CACHE and now - _METACULUS_TS < 600: return _METACULUS_CACHE
    try:
        # Try the newer v1 API first (supports token auth), then fall back to v2
        headers = {"Accept": "application/json"}
        if METACULUS_API_TOKEN:
            headers["Authorization"] = f"Token {METACULUS_API_TOKEN}"

        urls_to_try = [
            "https://www.metaculus.com/api/questions/?type=forecast&status=open&limit=200&order_by=-activity",
            "https://www.metaculus.com/api2/questions/?type=forecast&status=open&limit=200&order_by=-activity",
        ]
        data = None
        for url in urls_to_try:
            _rate_limit_wait(url)
            r = requests.get(url, timeout=10, headers=headers)
            if r.status_code == 200:
                data = r.json()
                break
            elif r.status_code == 403 and not METACULUS_API_TOKEN:
                # Auth required but no token — skip silently, don't spam logs
                return _METACULUS_CACHE

        if not data:
            return _METACULUS_CACHE

        questions = data.get("results", [])
        _METACULUS_CACHE = {
            q["id"]: q for q in questions
            if q.get("possibilities", {}).get("type") == "binary"
        }
        _METACULUS_TS = now
        if _METACULUS_CACHE:
            print(f"[metaculus] Loaded {len(_METACULUS_CACHE)} binary questions")
    except Exception as e:
        print(f"[metaculus] Failed: {e}")
    return _METACULUS_CACHE

def get_metaculus_estimate(ticker, market_data):
    """Fuzzy match Kalshi market to Metaculus question by title similarity."""
    title = (market_data.get("title") or market_data.get("subtitle") or "").lower()
    title_words = set(re.findall(r'\w{3,}', title))
    if len(title_words) < 3: return None, None

    questions = _load_metaculus()
    if not questions: return None, None

    best_q = None
    best_sim = 0
    for qid, q in questions.items():
        q_title = (q.get("title") or "").lower()
        q_words = set(re.findall(r'\w{3,}', q_title))
        if not q_words: continue
        sim = len(title_words & q_words) / len(title_words | q_words)
        if sim > best_sim:
            best_sim = sim
            best_q = q

    if not best_q or best_sim < 0.50: return None, None  # tightened from 0.30

    # Get community prediction
    prediction = best_q.get("community_prediction", {})
    prob = prediction.get("full", {}).get("q2")  # median
    if prob is None:
        prob = prediction.get("full", {}).get("avg")
    if prob is None or not (0.01 < prob < 0.99): return None, None

    q_title = best_q.get("title", "")[:50]
    print(f"[metaculus] Match (sim={best_sim:.2f}): '{title[:40]}' → '{q_title}' prob={prob:.2f}")
    return prob, f"metaculus:{best_q['id']}"

# ── 7. FINNHUB — news sentiment for event-driven signals ────────────────────
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "")

def get_news_sentiment(ticker, market_data):
    """Check Finnhub news sentiment — DISABLED: the news-sentiment endpoint requires
    a paid Finnhub plan (free tier returns HTTP 403). Returning fast None so pipeline
    health doesn't penalize this source."""
    return None, None
    if not FINNHUB_KEY: return None, None
    title = (market_data.get("title") or market_data.get("subtitle") or "").lower()

    # Only trigger for markets that look like they involve publicly traded companies
    # Look for known stock-related patterns in the title
    stock_keywords = ["stock", "share", "s&p", "nasdaq", "dow", "earnings", "ipo",
                      "market cap", "trading", "nyse"]
    has_stock_context = any(kw in title for kw in stock_keywords)

    # Also check if the ticker itself looks like a stock symbol (2-5 uppercase letters)
    ticker_upper = ticker.upper()
    known_stock_tickers = {"AAPL", "TSLA", "MSFT", "AMZN", "GOOGL", "META", "NVDA",
                           "NFLX", "AMD", "INTC", "BA", "DIS", "JPM", "GS", "V", "MA"}
    has_stock_ticker = any(st in ticker_upper for st in known_stock_tickers)

    if not has_stock_context and not has_stock_ticker:
        return None, None

    # Extract potential stock symbol from title
    stop_words = {"will", "the", "be", "in", "on", "at", "to", "of", "a", "an",
                  "or", "and", "for", "by", "this", "that", "what", "how", "when",
                  "yes", "no", "above", "below", "more", "less", "than", "over", "under",
                  "stock", "share", "price", "market"}
    words = [w for w in re.findall(r'\w{3,}', title) if w not in stop_words]
    if not words: return None, None

    # Try to find a valid stock symbol
    symbol = None
    for w in words:
        if w.upper() in known_stock_tickers:
            symbol = w.upper()
            break
    if not symbol:
        # Fall back to first non-stop word as potential symbol
        symbol = words[0].upper()

    cache_key = f"finnhub_sent_{symbol}"
    sentiment_data = _cached_get(cache_key,
        f"https://finnhub.io/api/v1/news-sentiment?symbol={symbol}&token={FINNHUB_KEY}",
        timeout=8)

    if sentiment_data and sentiment_data.get("sentiment"):
        score = sentiment_data["sentiment"].get("bullishPercent", 0.5)
        if abs(score - 0.5) > 0.15:  # raised threshold from 0.1 — require stronger signal
            print(f"[finnhub] {symbol} sentiment={score:.2f}")
            return score, f"finnhub:{symbol}"

    return None, None

# ── 7b. COMPANY KPI — analyst estimates for deliveries, revenue, subscribers ──
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
        # We have revenue estimate but need deliveries — use as weak signal
        # Just return None and let LLM handle these for now
        # TODO: Add SensorTower/alternative data sources for app metrics
        return None, None

    # Compare consensus to threshold
    if consensus and threshold:
        # How far is consensus from threshold, as a fraction of threshold
        ratio = consensus / threshold if threshold != 0 else 1.0

        if is_above:
            # P(above threshold) — higher ratio = more likely above
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
            # P(below threshold) — lower ratio = more likely below
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
              f"threshold={threshold:.0f} {'above' if is_above else 'below'} → {prob:.2f}")
        return prob, src_desc

    return None, None

# ── 7c. SENSORTOWER — app intelligence for subscriber/DAU/download markets ────
SENSORTOWER_TOKEN = os.environ.get("SENSORTOWER_API_TOKEN", "")
_ST_RATE_LIMIT = {
    "calls_today": 0,
    "last_reset": None,
    "max_per_day": 40,       # 5000/month ÷ 30 = ~166/day, but stay well under
    "cache": {},             # {app_id: (data, timestamp)}
    "cache_ttl": 3600,       # cache for 1h — 24h was too stale for live trading
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
    - Netflix subscriber markets (KXNFLX) — app downloads correlate with subscriber growth
    - Meta DAU markets (KXMETADAP) — app DAU directly measures this
    - Tesla delivery markets (KXTESLA) — Tesla app downloads correlate with deliveries

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

    # Rate limiting — reset daily counter
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
                print(f"[sensortower] Auth failed (401) — check SENSORTOWER_API_TOKEN")
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

    # Parse the response — extract download/revenue estimates
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
                # Can't determine threshold — return download trend as signal
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
        # For subscriber markets: downloads ≈ new subscriber proxy
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
            # Conservative probability mapping — app data is a proxy, not exact
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
                  f"threshold={threshold:,.0f} {'above' if is_above else 'below'} → {prob:.2f}")
            return prob, src_desc

    except Exception as e:
        print(f"[sensortower] Parse error for {app_info['name']}: {e}")

    return None, None

# ── 8a. SERIES STRUCTURE — detect mispriced strikes within Kalshi series ─────
_SERIES_CACHE = {}  # {event_ticker: (markets_list, timestamp)}

def get_series_estimate(ticker, market_data):
    """Analyze related markets in the same Kalshi series/event to detect
    mispricing. If a series of strike-price markets has an inconsistent
    implied CDF, individual strikes may be mispriced.
    Returns (adjusted_prob, source_desc) or (None, None)."""
    event_ticker = market_data.get("event_ticker") or ""
    if not event_ticker:
        return None, None

    # Cache series data for 5 min
    now = time.time()
    if event_ticker in _SERIES_CACHE and now - _SERIES_CACHE[event_ticker][1] < 300:
        siblings = _SERIES_CACHE[event_ticker][0]
    else:
        try:
            resp = api_get(f"/events/{event_ticker}/markets?limit=50&status=open")
            siblings = resp.get("markets", [])
            _SERIES_CACHE[event_ticker] = (siblings, now)
        except Exception:
            return None, None

    if len(siblings) < 3:
        return None, None  # need multiple strikes to do series analysis

    # Build the implied probability curve from sibling markets
    # Each sibling is a strike: "BTC above $90k", "BTC above $95k", etc.
    # For "above X" markets, the yes_ask prices should form a monotonically
    # decreasing CDF (higher strikes → lower probability)
    strikes = []
    for sib in siblings:
        sib_ticker = sib.get("ticker", "")
        sib_title = (sib.get("title") or sib.get("subtitle") or "").lower()
        sib_ask = float(sib.get("yes_ask") or sib.get("yes_ask_dollars") or 0)
        if sib_ask > 1: sib_ask /= 100
        sib_bid = float(sib.get("yes_bid") or sib.get("yes_bid_dollars") or 0)
        if sib_bid > 1: sib_bid /= 100

        # Extract numeric strike from title
        strike_match = re.search(r'\$?([\d,]+(?:\.\d+)?)', sib_title)
        if strike_match and sib_ask > 0 and sib_bid > 0:
            try:
                strike_val = float(strike_match.group(1).replace(",", ""))
                if strike_val > 10:  # sanity check
                    mid = (sib_ask + sib_bid) / 2
                    strikes.append((strike_val, mid, sib_ticker))
            except: pass

    if len(strikes) < 3:
        return None, None

    # Sort by strike value
    strikes.sort(key=lambda x: x[0])

    # Check for CDF monotonicity violations
    # In a well-priced "above X" series, higher strikes should have lower probability.
    # Detect if our target market is out of line with its neighbors.
    our_strike_idx = None
    for i, (sv, mid, st) in enumerate(strikes):
        if st == ticker:
            our_strike_idx = i
            break

    if our_strike_idx is None:
        return None, None

    our_val, our_mid, _ = strikes[our_strike_idx]

    # Interpolate what the probability "should" be based on neighbors
    # Use simple linear interpolation between adjacent strikes
    if our_strike_idx > 0 and our_strike_idx < len(strikes) - 1:
        lower_strike, lower_mid, _ = strikes[our_strike_idx - 1]
        upper_strike, upper_mid, _ = strikes[our_strike_idx + 1]
        # Linear interpolation
        if upper_strike != lower_strike:
            frac = (our_val - lower_strike) / (upper_strike - lower_strike)
            interpolated = lower_mid + frac * (upper_mid - lower_mid)
            deviation = our_mid - interpolated
            if abs(deviation) > 0.03:  # >3¢ mispricing vs interpolated
                print(f"[series] {ticker}: market mid={our_mid:.2f} "
                      f"interpolated={interpolated:.2f} deviation={deviation:+.2f} "
                      f"({len(strikes)} strikes in {event_ticker})")
                return interpolated, f"series:{event_ticker}({len(strikes)}strikes)"

    return None, None

# ── 8b. MOMENTUM — Kalshi's own trade history ────────────────────────────────
def get_price_momentum(ticker):
    try:
        resp = api_get(f"/markets/{ticker}/trades?limit=20")
        trades = resp.get("trades", [])
        if len(trades) < 2: return None
        prices = []
        for t in trades:
            p = float(t.get("yes_price") or t.get("price") or 0)
            if p > 1: p /= 100
            if p > 0: prices.append(p)
        if len(prices) < 2: return None
        return {"last_price": prices[0], "avg_price": sum(prices)/len(prices),
                "momentum": prices[0] - sum(prices)/len(prices)}
    except: return None

# ── 9. LLM-BASED MARKET ANALYSIS — for markets no regex source can parse ─────
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
_LLM_CACHE = {}  # {ticker: (prob, timestamp)}

def get_llm_estimate(ticker, market_data):
    """Use GPT-4o-mini to analyze markets that no other source matched.
    Only called when all 8 regex-based sources returned None.
    Returns (probability, source_desc) or (None, None).

    IMPORTANT: This is expensive (~$0.001/call) and slow (~1-2s), so it's
    only triggered as a last resort for markets with good volume/spread."""
    if not OPENAI_API_KEY:
        return None, None

    title = market_data.get("title") or market_data.get("subtitle") or ""
    if not title or len(title) < 10:
        return None, None

    # Cache LLM results for 30 min (these markets don't change fast)
    now = time.time()
    if ticker in _LLM_CACHE and now - _LLM_CACHE[ticker][1] < 1800:
        cached_prob = _LLM_CACHE[ticker][0]
        if cached_prob is not None:
            return cached_prob, f"llm_cached:{ticker[:20]}"
        return None, None

    close_time = market_data.get("close_time") or market_data.get("expiration_time") or ""
    yes_ask_raw = market_data.get("yes_ask") or market_data.get("yes_ask_dollars") or 0
    yes_ask_val = float(yes_ask_raw)
    if yes_ask_val > 1: yes_ask_val /= 100

    prompt = f"""You are a prediction market analyst. Estimate the probability that this Kalshi market resolves YES.

Market: "{title}"
Current market price (implied probability): {yes_ask_val:.0%}
Resolution date: {close_time or 'unknown'}
Current date: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}

Think step by step about what publicly available information suggests. Consider:
- Recent news and trends
- Historical base rates
- Time until resolution
- Whether the current market price seems too high or too low

Respond with ONLY a JSON object: {{"probability": 0.XX, "reasoning": "brief 1-sentence reason"}}
Do not include any other text."""

    try:
        _rate_limit_wait("https://api.openai.com/v1/chat/completions")
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                     "Content-Type": "application/json"},
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 150,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            _LLM_CACHE[ticker] = (None, now)
            return None, None

        content = resp.json()["choices"][0]["message"]["content"].strip()
        # Parse JSON response — try multiple formats robustly
        parsed = None
        # Attempt 1: raw JSON parse (no code blocks)
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            pass
        # Attempt 2: extract from markdown code blocks
        if parsed is None and "```" in content:
            try:
                block = content.split("```")[1]
                if block.startswith("json"):
                    block = block[4:]
                parsed = json.loads(block.strip())
            except (json.JSONDecodeError, ValueError, IndexError):
                pass
        # Attempt 3: regex extract first JSON object
        if parsed is None:
            try:
                json_match = re.search(r'\{[^}]+\}', content)
                if json_match:
                    parsed = json.loads(json_match.group())
            except (json.JSONDecodeError, ValueError):
                pass
        if parsed is None:
            _LLM_CACHE[ticker] = (None, now)
            return None, None
        prob = float(parsed.get("probability", 0))
        reasoning = parsed.get("reasoning", "")[:80]

        # Sanity checks
        if not (0.02 < prob < 0.98):
            _LLM_CACHE[ticker] = (None, now)
            return None, None

        # Don't trust LLM if it just parrots the market price back
        if abs(prob - yes_ask_val) < 0.03:
            _LLM_CACHE[ticker] = (None, now)
            return None, None

        _LLM_CACHE[ticker] = (prob, now)
        print(f"[llm] {ticker}: '{title[:40]}' → prob={prob:.2f} ({reasoning})")
        return prob, f"llm:{reasoning[:30]}"

    except Exception as e:
        print(f"[llm] Failed for {ticker}: {e}")
        _LLM_CACHE[ticker] = (None, now)
        return None, None

# ── Source confidence weights for ensemble probability estimation ─────────────
SOURCE_WEIGHTS = {
    "polymarket": 0.75,  # Cross-market price — strong but not "independent" (was 0.90)
    "odds":       0.85,  # Bookmaker odds — very reliable for sports
    "weather":    0.80,  # Forecast models — strong for weather markets
    "noaa":       0.75,  # NOAA active alerts — strong for severe weather events
    "series":     0.75,  # Intra-series CDF analysis — strong structural signal
    "metaculus":  0.70,  # Crowd prediction aggregation — decent for most markets
    "clevfed":    0.72,  # Cleveland Fed nowcast — real-time CPI estimates
    "crypto":     0.65,  # Live price vs vol-adjusted strike — good but volatile
    "fred":       0.50,  # Lagging economic data — heavily discounted (was 0.60)
    "llm":        0.15,  # GPT-4o-mini analysis — noisy, downweighted (was 0.45)
    "finnhub":    0.30,  # News sentiment — weak, noisy signal
    "momentum":   0.15,  # Kalshi trade history — weakest, fallback only
    "company_kpi": 0.65, # Analyst consensus estimates — decent for company KPI markets
    "sensortower": 0.55, # App download/usage proxy — moderate signal, indirect metric
}

# ── Ensemble router: collect ALL sources, weighted average ────────────────────
def get_independent_estimate(ticker, market_data, yes_ask, volume,
                             adaptive_weights=None, calibration_corrections=None,
                             disabled_sources=None):
    """
    Returns (independent_prob, source_description, num_sources) by collecting
    ALL available data sources and computing a weighted ensemble average.
    Replaces the old first-match-wins approach for more robust estimates.

    If adaptive_weights is provided (dict of source→weight), uses learned weights
    instead of static SOURCE_WEIGHTS. If calibration_corrections is provided,
    applies bias correction to the final ensemble output.
    """
    weights = adaptive_weights if adaptive_weights else SOURCE_WEIGHTS
    estimates = []  # list of (prob, weight, source_name)

    _disabled = disabled_sources or set()

    def _tracked_call(source_name, func, *args, **kwargs):
        """Call a data source function with pipeline health tracking.
        Skips sources that have been disabled by active feedback.

        Distinguishes 'not applicable' (fast None = source correctly says it has
        nothing for this market) from 'failure' (exception or slow timeout).
        Only failures count against the source's health score."""
        if source_name in _disabled:
            return (None, None)
        pipeline_track_attempt(source_name)
        t0 = time.time()
        try:
            result = func(*args, **kwargs)
            latency = (time.time() - t0) * 1000
            if result is not None and result[0] is not None:
                # Source returned a real estimate — success
                pipeline_track_result(source_name, True, latency)
            elif latency < 100:
                # Fast None = "not applicable to this market" — don't penalize.
                # Undo the attempt count so this doesn't affect health score.
                _PIPELINE_STATS[source_name]["attempted"] -= 1
            else:
                # Slow None = source tried but failed (API timeout, parse error)
                pipeline_track_result(source_name, False, latency)
            return result
        except Exception:
            latency = (time.time() - t0) * 1000
            pipeline_track_result(source_name, False, latency)
            return (None, None)

    # 1. Polymarket cross-reference
    poly_prob, poly_src = _tracked_call("polymarket", get_polymarket_estimate, ticker, market_data)
    if poly_prob is not None:
        estimates.append((poly_prob, weights.get("polymarket", 0.75), poly_src))

    # 2. Crypto — CoinGecko live prices
    crypto_prob, crypto_src = _tracked_call("crypto", get_crypto_estimate, ticker, market_data)
    if crypto_prob is not None:
        estimates.append((crypto_prob, weights.get("crypto", 0.65), crypto_src))

    # 3. Weather — Open-Meteo forecasts
    weather_prob, weather_src = _tracked_call("weather", get_weather_estimate, ticker, market_data)
    if weather_prob is not None:
        estimates.append((weather_prob, weights.get("weather", 0.80), weather_src))

    # 3a. Tomorrow.io — premium weather forecast (redundant backup for Open-Meteo)
    tmrw_prob, tmrw_src = _tracked_call("tomorrow", get_tomorrow_weather_estimate, ticker, market_data)
    if tmrw_prob is not None:
        estimates.append((tmrw_prob, weights.get("tomorrow", 0.82), tmrw_src))

    # 3b. NOAA severe weather alerts
    noaa_prob, noaa_src = _tracked_call("noaa", get_noaa_alerts_for_market, ticker, market_data)
    if noaa_prob is not None:
        estimates.append((noaa_prob, weights.get("noaa", 0.70), noaa_src))

    # 4. Economic — FRED data
    fred_prob, fred_src = _tracked_call("fred", get_fred_estimate, ticker, market_data)
    if fred_prob is not None:
        estimates.append((fred_prob, weights.get("fred", 0.50), fred_src))

    # 4b. Cleveland Fed nowcast — real-time CPI estimate (better than FRED for inflation)
    clevfed_prob, clevfed_src = _tracked_call("clevfed", get_cleveland_fed_nowcast, ticker, market_data)
    if clevfed_prob is not None:
        estimates.append((clevfed_prob, weights.get("clevfed", 0.72), clevfed_src))

    # 4c. BLS — backup for FRED on CPI, unemployment, nonfarm payroll
    bls_prob, bls_src = _tracked_call("bls", get_bls_estimate, ticker, market_data)
    if bls_prob is not None:
        estimates.append((bls_prob, weights.get("bls", 0.50), bls_src))

    # 5. Sports — bookmaker odds
    sports_prob, sports_src = _tracked_call("odds", get_sports_estimate, ticker, market_data)
    if sports_prob is not None:
        estimates.append((sports_prob, weights.get("odds", 0.85), sports_src))

    # 6. Metaculus — community prediction
    meta_prob, meta_src = _tracked_call("metaculus", get_metaculus_estimate, ticker, market_data)
    if meta_prob is not None:
        estimates.append((meta_prob, weights.get("metaculus", 0.70), meta_src))

    # 7. Finnhub — news sentiment (weak signal)
    news_prob, news_src = _tracked_call("finnhub", get_news_sentiment, ticker, market_data)
    if news_prob is not None:
        estimates.append((news_prob, weights.get("finnhub", 0.30), news_src))

    # 7b. Company KPI — analyst estimates for deliveries, revenue, subscribers
    kpi_prob, kpi_src = _tracked_call("company_kpi", get_company_kpi_estimate, ticker, market_data)
    if kpi_prob is not None:
        estimates.append((kpi_prob, weights.get("company_kpi", 0.65), kpi_src))

    # 7c. SensorTower — app downloads/usage as proxy for subscriber/delivery KPIs
    st_prob, st_src = _tracked_call("sensortower", get_sensortower_estimate, ticker, market_data)
    if st_prob is not None:
        estimates.append((st_prob, weights.get("sensortower", 0.55), st_src))

    # 8. Series structure — intra-series mispricing detection
    series_prob, series_src = _tracked_call("series", get_series_estimate, ticker, market_data)
    if series_prob is not None:
        estimates.append((series_prob, weights.get("series", 0.75), series_src))

    # 10. Momentum — Kalshi's own trade history (weakest)
    # GATED: only call the per-market Kalshi API if we have NO other estimates.
    # Otherwise this fires hundreds of API calls per scan for no value.
    if not estimates:
        momentum = get_price_momentum(ticker)
        if momentum and abs(momentum["momentum"]) > 0.02:
            adj = momentum["momentum"] * 0.5
            mom_est = max(0.02, min(0.98, yes_ask + adj))
            if abs(mom_est - yes_ask) > 0.02:
                estimates.append((mom_est, weights.get("momentum", 0.15), f"momentum_adj={adj:+.2f}"))

    # 11. LLM analysis — LAST RESORT for markets no regex source can parse.
    # Only fire for markets with decent volume (worth the API cost) and
    # moderate prices (not extreme). Limit to max 10 LLM calls per scan.
    # Category gate: skip crypto/sports/weather markets where we already have
    # specialized sources — LLM adds noise, not alpha, for those categories.
    _LLM_SKIP_CATEGORIES = {"crypto", "weather", "sports"}
    category = market_data.get("category", "").lower() if market_data else ""
    llm_category_ok = not any(cat in category for cat in _LLM_SKIP_CATEGORIES)
    # Also check title keywords as fallback for uncategorized markets
    if llm_category_ok:
        title_check = (market_data.get("title", "") or "").lower()
        if any(kw in title_check for kw in ["bitcoin", "btc", "ethereum", "eth", "solana",
                "temperature", "degrees", "nba", "nfl", "mlb", "nhl", "ncaa"]):
            llm_category_ok = False
    if not estimates and OPENAI_API_KEY and volume >= 200 and llm_category_ok:
        llm_prob, llm_src = get_llm_estimate(ticker, market_data)
        if llm_prob is not None:
            estimates.append((llm_prob, weights.get("llm", 0.15), llm_src))

    if not estimates:
        # High-volume markets are efficient — skip
        if volume > 10000:
            return None, "high_vol_efficient", 0
        return None, None, 0

    # ── Disagreement detection: skip if sources conflict too much ─────────
    # If any two estimates differ by >0.20, the fuzzy matching may have hit
    # different markets, or genuine uncertainty is too high to trade on.
    if len(estimates) >= 2:
        probs_only = [p for p, _, _ in estimates]
        max_spread = max(probs_only) - min(probs_only)
        if max_spread > 0.20:
            sources_str = ", ".join(f"{s}={p:.2f}" for p, _, s in estimates)
            print(f"[ensemble] SKIP: source disagreement {max_spread:.2f} > 0.20 "
                  f"({sources_str})")
            return None, f"disagreement:{max_spread:.2f}", 0

    # Weighted ensemble average
    total_weight = sum(w for _, w, _ in estimates)
    ensemble_prob = sum(p * w for p, w, _ in estimates) / total_weight
    sources = "+".join(s.split(":")[0] if ":" in s else s[:10] for _, _, s in estimates)
    n_sources = len(estimates)

    # Apply calibration correction if we have learned biases
    raw_prob = ensemble_prob
    if calibration_corrections:
        ensemble_prob = apply_calibration_correction(ensemble_prob, calibration_corrections)
        if abs(ensemble_prob - raw_prob) > 0.001:
            print(f"[calibration] Corrected {raw_prob:.3f} → {ensemble_prob:.3f} "
                  f"(correction={ensemble_prob - raw_prob:+.3f})")

    print(f"[ensemble] {n_sources} sources → {ensemble_prob:.3f} "
          f"({', '.join(f'{s}={p:.2f}' for p, _, s in estimates)})")

    # Safety clamp — never return extreme probabilities regardless of source
    ensemble_prob = max(0.02, min(0.98, ensemble_prob))
    return ensemble_prob, f"ensemble({sources})", n_sources

# ══════════════════════════════════════════════════════════════════════════════
# SETTLEMENT LEARNING (unchanged from before)
# ══════════════════════════════════════════════════════════════════════════════
def _prob_bucket(p):
    """Assign a probability to a calibration bucket (0.0-0.1, 0.1-0.2, ..., 0.9-1.0)."""
    if p is None: return None
    bucket = int(p * 10) / 10  # floor to nearest 0.1
    return f"{bucket:.1f}-{bucket+0.1:.1f}"

def record_settlements(conn):
    """Fetch settled positions from Kalshi API and record bot-placed trades.

    Kalshi settlement API returns per-ticker entries with:
      ticker, revenue (cents), market_result (yes/no), yes_count_fp, no_count_fp,
      yes_total_cost_dollars, no_total_cost_dollars, settled_time, fee_cost

    We match settlements to our bot's orders (mm_orders + trades tables) by ticker.
    Personal trades (NBA, NCAA, MVE parlays) are skipped since they weren't bot-placed.
    """
    try:
        settlements = api_get("/portfolio/settlements?limit=200").get("settlements", [])
    except Exception as e:
        print(f"[learn] Could not fetch settlements: {e}"); return 0

    # Prefixes to SKIP — personal/manual trades, not bot-placed
    _PERSONAL_PREFIXES = ("KXNBA", "KXNCAA", "KXMVE", "KXNFL", "KXMARMAD")

    recorded = 0
    skipped_personal = 0
    skipped_dup = 0
    skipped_notours = 0
    now_str = datetime.now(timezone.utc).isoformat()

    for s in settlements:
        ticker = s.get("ticker", "")
        if not ticker:
            continue

        # Skip personal/manual trades
        ticker_upper = ticker.upper()
        if any(ticker_upper.startswith(pfx) for pfx in _PERSONAL_PREFIXES):
            skipped_personal += 1
            continue

        # Use ticker as unique key (one settlement per ticker per account)
        if conn.execute("SELECT 1 FROM settlements WHERE ticker=? AND order_id LIKE 'settlement_%'",
                        (ticker,)).fetchone():
            skipped_dup += 1
            continue

        # Check if this ticker was traded by the bot (mm_orders or trades table)
        is_bot_trade = False
        mm_row = conn.execute(
            "SELECT fair_value_cents, tag FROM mm_orders WHERE ticker=? LIMIT 1",
            (ticker,)).fetchone()
        dir_row = conn.execute(
            "SELECT price_cents, strategy, independent_prob FROM trades WHERE ticker=? LIMIT 1",
            (ticker,)).fetchone()

        if mm_row:
            is_bot_trade = True
            strat = "mm:" + (mm_row[1] or "mm_v1")
            est_prob = None
            pc = mm_row[0]  # fair_value_cents
        elif dir_row:
            is_bot_trade = True
            pc = dir_row[0]
            strat = dir_row[1]
            est_prob = dir_row[2]
        else:
            # Not in our DB — might be a personal trade or from before bot started
            skipped_notours += 1
            continue

        # Parse settlement data
        revenue = int(s.get("revenue", 0))
        result = s.get("market_result", "")  # "yes" or "no"
        yes_count = float(s.get("yes_count_fp", 0))
        no_count = float(s.get("no_count_fp", 0))
        yes_cost = float(s.get("yes_total_cost_dollars", 0)) * 100  # to cents
        no_cost = float(s.get("no_total_cost_dollars", 0)) * 100
        contracts = int(yes_count + no_count)
        total_cost = int(yes_cost + no_cost)

        won = 1 if revenue > 0 else 0
        profit = revenue - total_cost

        # Determine side from position
        side = "yes" if yes_count > no_count else "no"

        # Use "settlement_<ticker>" as order_id since Kalshi doesn't provide one
        settlement_id = f"settlement_{ticker}"

        conn.execute("""INSERT OR IGNORE INTO settlements
            (recorded_at, order_id, ticker, side, price_cents, contracts,
             revenue_cents, profit_cents, won, volume, spread_cents, strategy)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now_str, settlement_id, ticker, side,
             pc, contracts, revenue, profit, won, None, None, strat))

        # Record calibration data for directional trades with estimates
        if est_prob is not None:
            bucket = _prob_bucket(est_prob)
            conn.execute("""INSERT INTO calibration
                (recorded_at, ticker, estimated_prob, actual_outcome, source_desc, n_sources, bucket)
                VALUES (?,?,?,?,?,?,?)""",
                (now_str, ticker, est_prob, won, strat, None, bucket))

        # Also update mm_inventory realized P&L for MM trades
        if mm_row:
            try:
                conn.execute(
                    "UPDATE mm_inventory SET realized_pnl_cents = realized_pnl_cents + ? WHERE ticker=?",
                    (profit, ticker))
            except Exception:
                pass  # table might not have this row

        recorded += 1

    if recorded > 0:
        conn.commit()
    print(f"[learn] Settlements: {recorded} recorded, {skipped_personal} personal (skipped), "
          f"{skipped_notours} not ours, {skipped_dup} already recorded")
    return recorded

def compute_avoid_filters(conn):
    filters = {"low_volume_threshold": None, "wide_spread_threshold": None,
               "avoided_strategies": set(), "avoided_prefixes": set(), "summary": []}
    rows = conn.execute(
        "SELECT volume, spread_cents, strategy, ticker, won FROM settlements WHERE volume IS NOT NULL"
    ).fetchall()
    if not rows:
        print("[learn] No settlement history yet"); return filters
    print(f"[learn] Analyzing {len(rows)} settled trades …")

    buckets = {"tiny": ([], 50), "low": ([], 500), "medium": ([], 5000), "high": ([], None)}
    for vol, sp, strat, tick, won in rows:
        v = vol or 0
        if v < 50: buckets["tiny"][0].append(won)
        elif v < 500: buckets["low"][0].append(won)
        elif v < 5000: buckets["medium"][0].append(won)
        else: buckets["high"][0].append(won)
    for name, (outcomes, thresh) in buckets.items():
        if len(outcomes) < MIN_SAMPLE_SIZE: continue
        wr = sum(outcomes)/len(outcomes)
        msg = f"  vol[{name}] wr={wr:.0%} n={len(outcomes)}"
        if wr < MIN_WIN_RATE and thresh:
            filters["low_volume_threshold"] = max(filters["low_volume_threshold"] or 0, thresh)
            msg += f" → AVOID"
        filters["summary"].append(msg); print(msg)

    strat_map, prefix_map = {}, {}
    for vol, sp, strat, tick, won in rows:
        if strat: strat_map.setdefault(strat, []).append(won)
        if tick: prefix_map.setdefault(tick[:6], []).append(won)
    for strat, outcomes in strat_map.items():
        if len(outcomes) < MIN_SAMPLE_SIZE: continue
        wr = sum(outcomes)/len(outcomes)
        msg = f"  strat[{strat[:20]}] wr={wr:.0%} n={len(outcomes)}"
        if wr < MIN_WIN_RATE: filters["avoided_strategies"].add(strat); msg += " → AVOID"
        filters["summary"].append(msg); print(msg)
    for pfx, outcomes in prefix_map.items():
        if len(outcomes) < MIN_SAMPLE_SIZE: continue
        wr = sum(outcomes)/len(outcomes)
        msg = f"  prefix[{pfx}] wr={wr:.0%} n={len(outcomes)}"
        if wr < MIN_WIN_RATE: filters["avoided_prefixes"].add(pfx); msg += " → AVOID"
        filters["summary"].append(msg); print(msg)

    # ── Calibration analysis: are our probability estimates accurate? ──────
    # Group settled trades by estimated probability bucket and check if
    # actual win rate matches the estimate. Overconfident buckets get flagged.
    cal_rows = conn.execute(
        "SELECT bucket, estimated_prob, actual_outcome FROM calibration WHERE bucket IS NOT NULL"
    ).fetchall()
    if cal_rows:
        cal_buckets = {}
        for bucket, est, actual in cal_rows:
            cal_buckets.setdefault(bucket, []).append((est, actual))
        filters["calibration"] = {}
        print(f"[calibration] Analyzing {len(cal_rows)} settled predictions:")
        for bucket in sorted(cal_buckets.keys()):
            entries = cal_buckets[bucket]
            n = len(entries)
            if n < 3: continue  # need minimum samples
            avg_est = sum(e for e, _ in entries) / n
            actual_rate = sum(a for _, a in entries) / n
            bias = avg_est - actual_rate  # positive = overconfident
            filters["calibration"][bucket] = {
                "avg_estimate": avg_est, "actual_rate": actual_rate,
                "bias": bias, "n": n}
            status = "OK" if abs(bias) < 0.10 else ("OVERCONFIDENT" if bias > 0 else "UNDERCONFIDENT")
            msg = (f"  cal[{bucket}] est={avg_est:.2f} actual={actual_rate:.2f} "
                   f"bias={bias:+.2f} n={n} {status}")
            filters["summary"].append(msg); print(msg)

    return filters

# ══════════════════════════════════════════════════════════════════════════════
# ADAPTIVE LEARNING — the bot updates its own parameters from outcomes
# ══════════════════════════════════════════════════════════════════════════════
# This is where "learning" happens: the bot adjusts source weights,
# calibration corrections, and category-level edge thresholds based on
# what actually worked vs. what didn't.

_LEARNED_WEIGHTS = None   # cached per run
_CALIBRATION_CURVE = None # cached per run
_CATEGORY_EDGES = None    # cached per run

def _parse_sources_from_strategy(strategy_str):
    """Extract individual source names from strategy like 'ensemble(polymarket+weather+crypto)'."""
    if not strategy_str:
        return []
    m = re.search(r'ensemble\(([^)]+)\)', strategy_str)
    if not m:
        # Might be a single source like 'momentum_adj=+0.02'
        for src in SOURCE_WEIGHTS:
            if src in (strategy_str or "").lower():
                return [src]
        return []
    return [s.strip().lower() for s in m.group(1).split("+")]

def compute_adaptive_weights(conn):
    """Compute source weights adjusted by actual track record.
    Uses a Bayesian blend: start with prior (hardcoded weights) and blend toward
    empirical accuracy as sample size grows. Requires MIN_ADAPTIVE_SAMPLES to
    start adjusting. Returns dict of source → adjusted weight."""
    global _LEARNED_WEIGHTS
    if _LEARNED_WEIGHTS is not None:
        return _LEARNED_WEIGHTS

    MIN_ADAPTIVE_SAMPLES = 10  # need at least this many per source to adjust

    # Get all settlements with their strategy (source combo) info
    rows = conn.execute(
        "SELECT strategy, won, profit_cents FROM settlements WHERE strategy IS NOT NULL"
    ).fetchall()

    if len(rows) < 20:
        # Not enough data — use defaults
        _LEARNED_WEIGHTS = dict(SOURCE_WEIGHTS)
        print(f"[adaptive] Too few settlements ({len(rows)}) — using default weights")
        return _LEARNED_WEIGHTS

    # Count wins/losses per source (a source gets credit when it was part of the ensemble)
    source_stats = {}  # {source: {"wins": n, "losses": n, "total_profit": x}}
    for strat, won, profit in rows:
        sources = _parse_sources_from_strategy(strat)
        for src in sources:
            if src not in source_stats:
                source_stats[src] = {"wins": 0, "losses": 0, "total_profit": 0}
            if won:
                source_stats[src]["wins"] += 1
            else:
                source_stats[src]["losses"] += 1
            source_stats[src]["total_profit"] += (profit or 0)

    # Compute adjusted weights using Bayesian shrinkage toward prior
    adjusted = {}
    for src, prior_weight in SOURCE_WEIGHTS.items():
        stats = source_stats.get(src)
        if not stats or (stats["wins"] + stats["losses"]) < MIN_ADAPTIVE_SAMPLES:
            # Not enough data — keep prior
            adjusted[src] = prior_weight
            continue

        n = stats["wins"] + stats["losses"]
        empirical_wr = stats["wins"] / n
        avg_profit = stats["total_profit"] / n

        # Blend factor: 0 = all prior, 1 = all empirical
        # Sigmoid that starts shifting at 20 samples and reaches ~0.7 at 100
        blend = min(0.7, (n - MIN_ADAPTIVE_SAMPLES) / 130)

        # Empirical quality score: win rate * profit direction
        # A source that wins 60% with positive avg profit is great
        # A source that wins 55% but loses money (fees?) is not
        if avg_profit > 0:
            empirical_quality = empirical_wr * 1.2  # bonus for profitable
        else:
            empirical_quality = empirical_wr * 0.8  # penalty for unprofitable despite wins

        # Scale to weight range [0.10, 0.95]
        empirical_weight = max(0.10, min(0.95, empirical_quality))

        adjusted[src] = prior_weight * (1 - blend) + empirical_weight * blend

        direction = "↑" if adjusted[src] > prior_weight else "↓" if adjusted[src] < prior_weight else "="
        print(f"[adaptive] {src}: prior={prior_weight:.2f} → adjusted={adjusted[src]:.2f} "
              f"{direction} (wr={empirical_wr:.0%}, n={n}, profit={avg_profit:+.0f}¢/trade)")

    _LEARNED_WEIGHTS = adjusted
    return adjusted

def compute_calibration_correction(conn):
    """Build a calibration curve that corrects systematic bias in our estimates.
    If we estimate 70% but actual outcomes are 55%, we should adjust down.
    Returns a dict of {bucket: correction_offset} to apply to ensemble output."""
    global _CALIBRATION_CURVE
    if _CALIBRATION_CURVE is not None:
        return _CALIBRATION_CURVE

    MIN_CAL_SAMPLES = 5  # per bucket

    rows = conn.execute(
        "SELECT bucket, estimated_prob, actual_outcome FROM calibration WHERE bucket IS NOT NULL"
    ).fetchall()

    if len(rows) < 20:
        _CALIBRATION_CURVE = {}
        return _CALIBRATION_CURVE

    buckets = {}
    for bucket, est, actual in rows:
        buckets.setdefault(bucket, []).append((est, actual))

    corrections = {}
    for bucket, entries in sorted(buckets.items()):
        if len(entries) < MIN_CAL_SAMPLES:
            continue
        avg_est = sum(e for e, _ in entries) / len(entries)
        actual_rate = sum(a for _, a in entries) / len(entries)
        bias = avg_est - actual_rate

        # Only correct if bias is significant (>5%) and we have enough data
        if abs(bias) > 0.05 and len(entries) >= MIN_CAL_SAMPLES:
            # Apply partial correction (50% of observed bias) to be conservative
            # Full correction would overfit to small samples
            corrections[bucket] = -bias * 0.5
            direction = "overconfident" if bias > 0 else "underconfident"
            print(f"[calibration] {bucket}: {direction} by {abs(bias):.1%} "
                  f"(est={avg_est:.2f} vs actual={actual_rate:.2f}, n={len(entries)}) "
                  f"→ correction={corrections[bucket]:+.3f}")

    _CALIBRATION_CURVE = corrections
    return corrections

def apply_calibration_correction(ensemble_prob, calibration_corrections):
    """Apply learned calibration correction to an ensemble probability estimate."""
    if not calibration_corrections or ensemble_prob is None:
        return ensemble_prob
    bucket = _prob_bucket(ensemble_prob)
    correction = calibration_corrections.get(bucket, 0)
    if correction == 0:
        return ensemble_prob
    corrected = max(0.02, min(0.98, ensemble_prob + correction))
    return corrected

def compute_category_edge_thresholds(conn):
    """Learn per-category minimum edge thresholds from settlement data.
    Categories where we lose money need higher edge requirements.
    Returns dict of {category: min_edge_multiplier}."""
    global _CATEGORY_EDGES
    if _CATEGORY_EDGES is not None:
        return _CATEGORY_EDGES

    MIN_CAT_SAMPLES = 8

    rows = conn.execute(
        "SELECT settlements.ticker, settlements.won, settlements.profit_cents, trades.edge, "
        "trades.reason "
        "FROM settlements "
        "JOIN trades ON settlements.order_id = trades.order_id "
        "WHERE trades.edge IS NOT NULL"
    ).fetchall()

    if len(rows) < 20:
        _CATEGORY_EDGES = {}
        return _CATEGORY_EDGES

    cat_stats = {}  # {category: {"wins": n, "losses": n, "profit": x, "edges": [...]}}
    for ticker, won, profit, edge, reason in rows:
        # Use reason/detail field for title hints since we don't store raw title
        cat = categorize_market(ticker, reason or "")
        if cat not in cat_stats:
            cat_stats[cat] = {"wins": 0, "losses": 0, "profit": 0, "edges": []}
        if won:
            cat_stats[cat]["wins"] += 1
        else:
            cat_stats[cat]["losses"] += 1
        cat_stats[cat]["profit"] += (profit or 0)
        cat_stats[cat]["edges"].append(edge)

    thresholds = {}
    for cat, stats in cat_stats.items():
        n = stats["wins"] + stats["losses"]
        if n < MIN_CAT_SAMPLES:
            continue
        wr = stats["wins"] / n
        avg_profit = stats["profit"] / n

        if wr < 0.50 or avg_profit < 0:
            # Losing category — require 50% more edge
            thresholds[cat] = 1.5
            print(f"[cat_edge] {cat}: LOSING (wr={wr:.0%}, profit={avg_profit:+.0f}¢) "
                  f"→ 1.5x edge required")
        elif wr > 0.58 and avg_profit > 0:
            # Strong category — can reduce edge requirement slightly
            thresholds[cat] = 0.85
            print(f"[cat_edge] {cat}: STRONG (wr={wr:.0%}, profit={avg_profit:+.0f}¢) "
                  f"→ 0.85x edge required")
        else:
            thresholds[cat] = 1.0

    _CATEGORY_EDGES = thresholds
    return thresholds

def generate_diagnostic_report(conn, result):
    """Phase 1 diagnostic: 'Is this thing even working?'
    Generates a focused health-check after the first 48 hours of paper trading.
    Answers: Are we finding opportunities? Are sources firing? Do estimates diverge
    from market prices? What categories are we scanning?"""
    now = datetime.now(timezone.utc)
    lines = []
    lines.append("# Phase 1 Diagnostic — Is This Thing Working?")
    lines.append(f"**Generated:** {now.strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("")

    # How many runs have we done?
    session_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    first_session = conn.execute("SELECT MIN(timestamp) FROM sessions").fetchone()[0]
    hours_running = 0
    if first_session:
        try:
            first_dt = datetime.fromisoformat(first_session.replace("Z", "+00:00"))
            hours_running = (now - first_dt).total_seconds() / 3600
        except: pass
    lines.append(f"## Runtime")
    lines.append(f"- **Sessions completed:** {session_count}")
    lines.append(f"- **Hours running:** {hours_running:.1f}")
    lines.append("")

    # Are we finding opportunities?
    total_trades = conn.execute("SELECT COUNT(*) FROM trades WHERE action='buy'").fetchone()[0]
    dry_trades = conn.execute("SELECT COUNT(*) FROM trades WHERE action='buy' AND dry_run=1").fetchone()[0]
    avg_per_session = total_trades / max(session_count, 1)
    lines.append("## Opportunity Finding")
    lines.append(f"- **Paper trades logged:** {total_trades} ({dry_trades} dry run)")
    lines.append(f"- **Avg per session:** {avg_per_session:.1f}")
    if total_trades == 0 and session_count > 3:
        lines.append("- **⚠️ WARNING:** No trades found after multiple sessions. "
                     "The ensemble may not be finding edge, or edge thresholds may be too high.")
    elif avg_per_session < 1 and session_count > 5:
        lines.append("- **⚠️ NOTE:** Low trade rate. Consider whether MIN_EDGE is too aggressive.")
    lines.append("")

    # Which sources are actually firing?
    source_fire_counts = {}
    trade_strategies = conn.execute(
        "SELECT strategy FROM trades WHERE action='buy'"
    ).fetchall()
    for (strat,) in trade_strategies:
        for src in _parse_sources_from_strategy(strat):
            source_fire_counts[src] = source_fire_counts.get(src, 0) + 1

    lines.append("## Source Activity")
    lines.append("")
    if source_fire_counts:
        lines.append("| Source | Times Fired | % of Trades |")
        lines.append("|--------|------------|-------------|")
        for src in sorted(source_fire_counts, key=source_fire_counts.get, reverse=True):
            count = source_fire_counts[src]
            pct = count / max(total_trades, 1) * 100
            lines.append(f"| {src} | {count} | {pct:.0f}% |")
        # Check for sources that never fired
        never_fired = [s for s in SOURCE_WEIGHTS if s not in source_fire_counts]
        if never_fired:
            lines.append("")
            lines.append(f"**Never fired:** {', '.join(never_fired)}")
            lines.append("(These sources may need API keys, or their market categories may be rare)")
    else:
        lines.append("No sources have fired yet.")
    lines.append("")

    # Edge distribution: are our estimates actually diverging from market prices?
    edges = conn.execute(
        "SELECT edge FROM trades WHERE action='buy' AND edge IS NOT NULL"
    ).fetchall()
    if edges:
        edge_vals = [e[0] for e in edges]
        lines.append("## Edge Distribution")
        lines.append(f"- **Avg edge:** {sum(edge_vals)/len(edge_vals):.1%}")
        lines.append(f"- **Max edge:** {max(edge_vals):.1%}")
        lines.append(f"- **Min edge:** {min(edge_vals):.1%}")
        # Histogram
        buckets_5 = [0]*5  # 5-10%, 10-15%, 15-20%, 20-25%, 25%+
        for e in edge_vals:
            idx = min(4, max(0, int((e - 0.05) / 0.05)))
            buckets_5[idx] += 1
        lines.append("")
        lines.append("| Edge Range | Count |")
        lines.append("|-----------|-------|")
        labels = ["5-10%", "10-15%", "15-20%", "20-25%", "25%+"]
        for label, count in zip(labels, buckets_5):
            lines.append(f"| {label} | {count} |")
    lines.append("")

    # Category breakdown
    cat_counts = {}
    tickers = conn.execute(
        "SELECT ticker FROM trades WHERE action='buy'"
    ).fetchall()
    for (t,) in tickers:
        cat = categorize_market(t, "")
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
    if cat_counts:
        lines.append("## Market Categories")
        lines.append("")
        lines.append("| Category | Trades |")
        lines.append("|----------|--------|")
        for cat in sorted(cat_counts, key=cat_counts.get, reverse=True):
            lines.append(f"| {cat} | {cat_counts[cat]} |")
    lines.append("")

    # Settlements (if any have resolved)
    settled = conn.execute("SELECT COUNT(*), COALESCE(SUM(won),0) FROM settlements").fetchone()
    if settled[0] > 0:
        lines.append("## Early Settlement Results")
        lines.append(f"- **Settled:** {settled[0]}")
        lines.append(f"- **Won:** {settled[1]} ({settled[1]/settled[0]:.0%})")
        total_profit = conn.execute(
            "SELECT COALESCE(SUM(profit_cents),0) FROM settlements"
        ).fetchone()[0]
        lines.append(f"- **Total profit:** {total_profit/100:+,.2f}")
    else:
        lines.append("## Settlements")
        lines.append("No trades have settled yet. Markets typically resolve within 1-7 days.")
    lines.append("")

    # Verdict
    lines.append("## Verdict")
    if total_trades == 0 and session_count > 3:
        lines.append("**🔴 NOT FINDING EDGE** — The bot has run multiple times but hasn't "
                     "found any tradeable opportunities. Possible causes: edge thresholds "
                     "too high, API keys missing, or markets are genuinely efficient.")
    elif total_trades > 0 and not source_fire_counts:
        lines.append("**🟡 TRADING BLIND** — Trades are being placed but no external sources "
                     "are contributing. Check API key configuration.")
    elif total_trades > 5 and len(source_fire_counts) >= 2:
        lines.append("**🟢 OPERATIONAL** — Finding opportunities with multiple data sources. "
                     "Continue paper trading until 50 settlements resolve to assess accuracy.")
    else:
        lines.append("**🟡 EARLY** — Insufficient data for a verdict. Keep running.")
    lines.append("")

    report = "\n".join(lines)
    try:
        report_path = os.environ.get("DIAGNOSTIC_REPORT_PATH", "/task/DIAGNOSTIC_REPORT.md")
        with open(report_path, "w") as f:
            f.write(report)
        print(f"[diagnostic] Wrote report to {report_path}")
    except Exception as e:
        print(f"[diagnostic] Failed to write report: {e}")

    return report

# ══════════════════════════════════════════════════════════════════════════════
# LEARNING LOOP 1: LOSS POST-MORTEMS
# ══════════════════════════════════════════════════════════════════════════════
# Classifies every loss into actionable categories so we learn WHY we lose,
# not just that we lost. Categories:
#   - bad_source: our estimate was >15% off from settlement reality
#   - efficient_market: edge was thin (<7%) and market was right
#   - adverse_selection: price moved against us right after entry (informed traders on other side)
#   - timing: direction was right but market hadn't converged yet (early entry)
#   - fee_erosion: would have been profitable pre-fees but fees ate the edge

def run_loss_postmortems(conn):
    """Analyze all unsettled losses and classify them. Run after record_settlements()."""
    now_str = datetime.now(timezone.utc).isoformat()

    # Find losses that haven't been post-mortem'd yet
    losses = conn.execute("""
        SELECT s.order_id, s.ticker, s.revenue_cents, s.profit_cents, s.price_cents, s.contracts,
               t.independent_prob, t.market_prob, t.edge, t.strategy, t.price_cents as entry_price
        FROM settlements s
        LEFT JOIN trades t ON s.order_id = t.order_id
        WHERE s.won = 0
          AND s.order_id NOT IN (SELECT order_id FROM loss_postmortems WHERE order_id IS NOT NULL)
    """).fetchall()

    if not losses:
        return 0

    classified = 0
    for (oid, ticker, revenue, profit, settle_price, contracts,
         est_prob, mkt_prob, edge, strategy, entry_price) in losses:

        loss_type = "unknown"
        detail = ""
        title = ""
        cat = categorize_market(ticker, title)

        if est_prob is not None and mkt_prob is not None and edge is not None:
            # How wrong were we?
            # Compare our estimate to what the market was pricing at entry.
            # A large gap (est >> market) that still lost = bad source signal.
            estimation_error = est_prob - mkt_prob  # our estimate vs market consensus

            if abs(estimation_error) > 0.30:
                loss_type = "bad_source"
                detail = (f"Estimated {est_prob:.0%} probability but lost. "
                         f"Sources: {strategy}. Major estimation failure.")
            elif edge is not None and abs(edge) < 0.07:
                # Fee calculation: entry spread + exit spread + platform fees
                fee_cost = ESTIMATED_EXIT_SPREAD + ESTIMATED_FEE_PER_CONTRACT * 2
                if edge > 0 and edge < fee_cost:
                    loss_type = "fee_erosion"
                    detail = (f"Edge of {edge:.1%} was below fee cost ~{fee_cost:.1%}. "
                             f"Would need >{fee_cost:.1%} edge to be profitable after fees.")
                else:
                    loss_type = "efficient_market"
                    detail = (f"Edge was only {edge:.1%}. Market was approximately correct. "
                             f"Our estimate {est_prob:.2f} vs market {mkt_prob:.2f}.")
            elif est_prob is not None and est_prob > 0.55:
                # We were fairly confident but still lost — could be adverse selection
                loss_type = "adverse_selection"
                detail = (f"Confident estimate ({est_prob:.0%}) but lost. "
                         f"Possible informed traders on other side or stale data.")
            else:
                loss_type = "bad_source"
                detail = f"Estimate {est_prob:.2f}, edge {edge:.1%}. Sources: {strategy}"
        else:
            loss_type = "unknown"
            detail = "Missing estimation data for analysis"

        conn.execute("""INSERT INTO loss_postmortems
            (recorded_at, order_id, ticker, category, loss_type, source_combo,
             estimated_prob, market_prob, edge_at_entry, price_at_settlement, detail)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (now_str, oid, ticker, cat, loss_type, strategy,
             est_prob, mkt_prob, edge, settle_price, detail))
        classified += 1

    conn.commit()

    # Print summary
    if classified > 0:
        summary = conn.execute("""
            SELECT loss_type, COUNT(*) FROM loss_postmortems GROUP BY loss_type ORDER BY COUNT(*) DESC
        """).fetchall()
        print(f"[postmortem] Classified {classified} new losses. All-time breakdown:")
        for lt, count in summary:
            print(f"  {lt}: {count}")

    return classified


# ══════════════════════════════════════════════════════════════════════════════
# LEARNING LOOP 2: THOMPSON SAMPLING BANDIT (strategies + categories)
# ══════════════════════════════════════════════════════════════════════════════
# Proper multi-armed bandit using Thompson Sampling (Beta-Bernoulli model).
# Each "arm" (strategy or category) has a Beta(α, β) posterior where:
#   α = 1 + wins (successes)
#   β = 1 + losses (failures)
# Prior is Beta(1,1) = uniform. We sample from each arm's posterior and use
# the samples to weight/rank arms. This naturally:
#   - Explores arms with few samples (wide posterior → random rank)
#   - Exploits arms with proven track records (tight posterior → consistent rank)
#   - Never permanently kills an arm (always some probability of being sampled)
#   - Handles non-stationarity via recency weighting

import random

EXPLORE_BUDGET_PCT = 0.10   # 10% of trades reserved for exploration (up from 5%)
EXPLORE_MIN_VOLUME = 100    # don't explore truly dead markets
STRATEGY_EXPLORE_PCT = 0.08 # 8% chance per run of re-testing a "cold" strategy

# Recency half-life: trades from >30 days ago count half as much
BANDIT_RECENCY_DAYS = 30

def _thompson_sample(wins, losses, n_samples=1):
    """Draw from Beta(1+wins, 1+losses) posterior. Returns single float [0,1]."""
    alpha = 1.0 + wins
    beta_param = 1.0 + losses
    try:
        return random.betavariate(alpha, beta_param)
    except ValueError:
        return 0.5  # fallback

def compute_strategy_bandit(conn):
    """Thompson Sampling over strategies.
    Returns dict: {strategy_name: {"sample": float, "wins": int, "losses": int,
                                     "n": int, "explore": bool}}
    The "sample" is a draw from the posterior — higher = more likely to be selected.
    """
    all_strategies = ["info_edge", "event_driven", "cross_market", "near_resolution"]
    result = {}

    for strat in all_strategies:
        # Get recent performance with recency weighting
        # Trades from the last BANDIT_RECENCY_DAYS get full weight,
        # older trades get exponentially decayed weight
        rows = conn.execute("""
            SELECT won, recorded_at FROM settlements
            WHERE strategy = ?
            ORDER BY id DESC LIMIT 200
        """, (strat,)).fetchall()

        wins = 0.0
        losses = 0.0
        n = 0
        now = datetime.now(timezone.utc)

        for won, recorded_at in rows:
            # Compute recency weight
            try:
                if recorded_at is None:
                    raise ValueError("missing recorded_at")
                t = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
                age_days = (now - t).total_seconds() / 86400
                weight = 0.5 ** (age_days / BANDIT_RECENCY_DAYS)  # half-life decay
            except Exception:
                weight = 0.5  # unknown age → half weight

            if won:
                wins += weight
            else:
                losses += weight
            n += 1

        sample = _thompson_sample(wins, losses)
        is_explore = False

        # If this strategy has very few samples (<5), flag it for exploration
        if n < 5:
            is_explore = True
            # Boost the sample slightly to encourage trying new strategies
            sample = max(sample, 0.3)

        # Even well-sampled strategies get occasional re-exploration
        # This handles non-stationarity (market conditions change)
        if n >= 15 and wins / max(1, wins + losses) < 0.35:
            # This strategy has been losing — but still give it a chance
            if random.random() < STRATEGY_EXPLORE_PCT:
                is_explore = True
                sample = max(sample, 0.25)  # floor so it doesn't get totally ignored
                print(f"[bandit] Re-exploring strategy '{strat}' "
                      f"(wr={wins/(wins+losses):.0%}, n={n}) — checking if conditions changed")

        result[strat] = {
            "sample": sample,
            "wins": round(wins, 1),
            "losses": round(losses, 1),
            "n": n,
            "explore": is_explore,
        }

    return result

def compute_exploration_targets(conn, candidates, n_total_slots):
    """Given scored candidates, use Thompson Sampling to allocate slots between
    exploit (proven categories) and explore (under-tested categories).
    Returns (exploit_candidates, explore_candidates)."""

    # How many explore slots?
    n_explore = max(1, int(n_total_slots * EXPLORE_BUDGET_PCT))
    n_exploit = n_total_slots - n_explore

    # Count settled trades per category with recency weighting
    cat_stats = {}  # {cat: {"wins": float, "losses": float, "n": int}}
    rows = conn.execute(
        "SELECT ticker, won, recorded_at FROM settlements"
    ).fetchall()

    now = datetime.now(timezone.utc)
    for t, won, recorded_at in rows:
        cat = categorize_market(t, "")
        if cat not in cat_stats:
            cat_stats[cat] = {"wins": 0.0, "losses": 0.0, "n": 0}
        try:
            if recorded_at is None:
                raise ValueError("missing recorded_at")
            t_dt = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
            age_days = (now - t_dt).total_seconds() / 86400
            weight = 0.5 ** (age_days / BANDIT_RECENCY_DAYS)
        except Exception:
            weight = 0.5
        if won:
            cat_stats[cat]["wins"] += weight
        else:
            cat_stats[cat]["losses"] += weight
        cat_stats[cat]["n"] += 1

    total_settled = sum(s["n"] for s in cat_stats.values())
    if total_settled < 15:
        # Too early — don't explore yet, we need baseline data
        return candidates[:n_total_slots], []

    # Thompson sample each category
    cat_samples = {}
    for cat, stats in cat_stats.items():
        cat_samples[cat] = _thompson_sample(stats["wins"], stats["losses"])

    # Find under-explored: <5 trades or Thompson sample is very uncertain
    all_candidate_cats = set()
    for c in candidates:
        ticker = c[9].get("ticker", "")
        title = c[9].get("title", "") or c[9].get("subtitle", "") or ""
        all_candidate_cats.add(categorize_market(ticker, title))

    under_explored = set()
    for cat in all_candidate_cats:
        if cat not in cat_stats or cat_stats[cat]["n"] < 5:
            under_explored.add(cat)
        elif cat_stats[cat]["n"] < 20:
            # Few samples — Thompson sampling will naturally explore these
            # But explicitly flag if the posterior is wide (high uncertainty)
            w, l = cat_stats[cat]["wins"], cat_stats[cat]["losses"]
            if (w + l) < 10:  # effective sample size is small
                under_explored.add(cat)

    if not under_explored:
        return candidates[:n_total_slots], []

    # Split candidates into exploit and explore pools
    exploit = []
    explore_pool = []
    for c in candidates:
        ticker = c[9].get("ticker", "")
        title = c[9].get("title", "") or c[9].get("subtitle", "") or ""
        cat = categorize_market(ticker, title)
        volume = c[4]
        if cat in under_explored and volume >= EXPLORE_MIN_VOLUME:
            explore_pool.append(c)
        else:
            exploit.append(c)

    exploit_picks = exploit[:n_exploit]
    explore_picks = explore_pool[:n_explore]

    if explore_picks:
        explore_cats = set()
        for c in explore_picks:
            ticker = c[9].get("ticker", "")
            title = c[9].get("title", "") or ""
            explore_cats.add(categorize_market(ticker, title))
        print(f"[bandit] Exploring {len(explore_picks)} under-sampled categories: "
              f"{explore_cats}")

    return exploit_picks, explore_picks


# ══════════════════════════════════════════════════════════════════════════════
# LEARNING LOOP 3: DATA PIPELINE HEALTH MONITORING
# ══════════════════════════════════════════════════════════════════════════════
# Tracks which data sources are healthy, degraded, or broken each run.
# Alerts when a source that was working stops returning data.

_PIPELINE_STATS = {}  # {source: {"attempted": n, "returned": n, "errors": n, "latencies": [ms]}}

def pipeline_track_attempt(source):
    """Call before querying a data source."""
    if source not in _PIPELINE_STATS:
        _PIPELINE_STATS[source] = {"attempted": 0, "returned": 0, "errors": 0, "latencies": []}
    _PIPELINE_STATS[source]["attempted"] += 1

def pipeline_track_result(source, success, latency_ms=0):
    """Call after a data source returns (or fails)."""
    if source not in _PIPELINE_STATS:
        _PIPELINE_STATS[source] = {"attempted": 0, "returned": 0, "errors": 0, "latencies": []}
    if success:
        _PIPELINE_STATS[source]["returned"] += 1
    else:
        _PIPELINE_STATS[source]["errors"] += 1
    if latency_ms > 0:
        _PIPELINE_STATS[source]["latencies"].append(latency_ms)

def record_pipeline_health(conn):
    """Record this run's pipeline health stats and detect degradations."""
    now_str = datetime.now(timezone.utc).isoformat()

    for source, stats in _PIPELINE_STATS.items():
        attempted = stats["attempted"]
        returned = stats["returned"]
        errors = stats["errors"]
        latencies = stats["latencies"]
        avg_latency = sum(latencies) / len(latencies) if latencies else 0
        error_rate = errors / attempted if attempted > 0 else 0

        # Determine status
        if attempted == 0:
            status = "idle"
        elif error_rate > 0.5:
            status = "degraded"
        elif returned == 0 and attempted > 0:
            status = "broken"
        else:
            status = "healthy"

        detail = ""
        # Compare to historical health for this source
        prev = conn.execute("""
            SELECT markets_attempted, markets_returned, status
            FROM pipeline_health
            WHERE source = ? ORDER BY id DESC LIMIT 1
        """, (source,)).fetchone()

        if prev and prev[2] == "healthy" and status in ("degraded", "broken"):
            detail = f"ALERT: {source} degraded from healthy → {status}"
            print(f"[pipeline] ⚠️  {detail}")
        elif prev and prev[1] and prev[1] > 5 and returned == 0:
            detail = f"ALERT: {source} returned 0 results (was {prev[1]} last run)"
            print(f"[pipeline] ⚠️  {detail}")

        conn.execute("""INSERT INTO pipeline_health
            (recorded_at, source, status, markets_attempted, markets_returned,
             avg_latency_ms, error_rate, detail)
            VALUES (?,?,?,?,?,?,?,?)""",
            (now_str, source, status, attempted, returned, avg_latency, error_rate, detail))

    conn.commit()
    _PIPELINE_STATS.clear()

    # Print summary
    health_summary = conn.execute("""
        SELECT source, status, markets_returned
        FROM pipeline_health
        WHERE recorded_at = (SELECT MAX(recorded_at) FROM pipeline_health)
        ORDER BY source
    """).fetchall()
    if health_summary:
        print("[pipeline] Source health:")
        for src, status, returned in health_summary:
            icon = "✓" if status == "healthy" else ("⚠" if status == "degraded" else "✗")
            print(f"  {icon} {src}: {status} ({returned} results)")


# ══════════════════════════════════════════════════════════════════════════════
# LEARNING LOOP 4: MARKET EFFICIENCY TRACKING (EDGE CONVERGENCE)
# ══════════════════════════════════════════════════════════════════════════════
# After we identify an edge, does the market price converge toward our estimate?
# If edges consistently don't converge, the bot is noise-trading, not edge-trading.

def check_edge_convergence(conn):
    """For recent trades, check if market prices moved toward our estimates.
    This validates whether the bot is actually smarter than the market."""
    now = datetime.now(timezone.utc)
    now_str = now.isoformat()

    # Find trades from 6-48h ago that haven't been checked yet
    window_start = (now - timedelta(hours=48)).isoformat()
    window_end = (now - timedelta(hours=6)).isoformat()

    trades = conn.execute("""
        SELECT t.order_id, t.ticker, t.side, t.independent_prob, t.market_prob,
               t.timestamp, t.price_cents
        FROM trades t
        WHERE t.action = 'buy'
          AND t.timestamp BETWEEN ? AND ?
          AND t.independent_prob IS NOT NULL
          AND t.ticker NOT IN (SELECT ticker FROM edge_convergence)
    """, (window_start, window_end)).fetchall()

    if not trades:
        return 0

    checked = 0
    convergences = []
    for oid, ticker, side, est_prob, mkt_prob, trade_ts, entry_price in trades:
        # Fetch current market price for this ticker
        try:
            mkt = api_get(f"/markets/{ticker}")
            current_yes = float(mkt.get("yes_ask") or mkt.get("yes_ask_dollars") or mkt.get("last_price") or mkt.get("last_price_dollars") or 0)
            if current_yes > 1:
                current_yes /= 100
        except Exception:
            continue

        if current_yes <= 0 or mkt_prob is None or est_prob is None:
            continue

        entry_price_frac = (entry_price / 100) if entry_price and entry_price > 1 else (entry_price or 0)

        # Did the market move toward our estimate?
        original_gap = abs(est_prob - mkt_prob)
        current_gap = abs(est_prob - current_yes)

        if original_gap > 0.01:  # only check if we had meaningful edge
            convergence_pct = (original_gap - current_gap) / original_gap
            converged = 1 if convergence_pct > 0.1 else 0  # >10% closer = convergence

            conn.execute("""INSERT INTO edge_convergence
                (recorded_at, ticker, side, our_estimate, market_price_at_entry,
                 market_price_after_24h, converged, convergence_pct)
                VALUES (?,?,?,?,?,?,?,?)""",
                (now_str, ticker, side, est_prob, mkt_prob, current_yes,
                 converged, convergence_pct))

            convergences.append(convergence_pct)
            checked += 1

    conn.commit()

    if convergences:
        avg_conv = sum(convergences) / len(convergences)
        n_converged = sum(1 for c in convergences if c > 0.1)
        print(f"[convergence] Checked {checked} trades: {n_converged}/{checked} converged "
              f"({n_converged/checked:.0%}), avg convergence={avg_conv:+.1%}")

        # Strategic assessment
        all_conv = conn.execute(
            "SELECT convergence_pct, converged FROM edge_convergence"
        ).fetchall()
        if len(all_conv) >= 20:
            total_conv_rate = sum(c[1] for c in all_conv) / len(all_conv)
            if total_conv_rate < 0.30:
                print(f"[convergence] ⚠️  WARNING: Only {total_conv_rate:.0%} of edges converge. "
                      f"The bot may be trading noise, not signal.")
                # Log to strategy journal
                conn.execute("""INSERT INTO strategy_journal
                    (timestamp, entry_type, category, title, detail, metric_value, metric_name)
                    VALUES (?,?,?,?,?,?,?)""",
                    (now_str, "observation", "convergence",
                     "Low edge convergence rate",
                     f"Only {total_conv_rate:.0%} of identified edges show market convergence. "
                     f"This suggests our estimates may not contain real information.",
                     total_conv_rate, "convergence_rate"))

    return checked


# ══════════════════════════════════════════════════════════════════════════════
# LEARNING LOOP 5: TIMING PATTERN LEARNING
# ══════════════════════════════════════════════════════════════════════════════
# Tracks what time of day and day of week our trades are most/least profitable.
# Over time, this reveals when our data sources have the freshest information.

def record_timing_data(conn):
    """Record timing metadata for settled trades that don't have timing data yet."""
    now_str = datetime.now(timezone.utc).isoformat()

    # Find settled trades without timing records (deduplicate by order_id)
    rows = conn.execute("""
        SELECT s.order_id, s.ticker, s.won, s.profit_cents,
               t.timestamp, t.strategy, t.edge
        FROM settlements s
        JOIN trades t ON s.order_id = t.order_id
        WHERE t.timestamp IS NOT NULL
          AND s.order_id NOT IN (SELECT order_id FROM timing_patterns WHERE order_id IS NOT NULL)
    """).fetchall()

    if not rows:
        return 0

    recorded = 0
    for oid, ticker, won, profit, trade_ts, strategy, edge in rows:
        try:
            dt = datetime.fromisoformat(trade_ts.replace("Z", "+00:00"))
        except Exception:
            continue

        hour_utc = dt.hour
        dow = dt.weekday()  # 0=Monday
        cat = categorize_market(ticker, "")

        # Extract primary source from strategy string
        sources = _parse_sources_from_strategy(strategy)
        primary_source = sources[0] if sources else "unknown"

        conn.execute("""INSERT INTO timing_patterns
            (recorded_at, order_id, hour_utc, day_of_week, category, source, edge, won, profit_cents)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (now_str, oid, hour_utc, dow, cat, primary_source, edge, won, profit))
        recorded += 1

    conn.commit()

    # Analyze timing patterns if we have enough data
    if recorded > 0:
        analyze_timing_patterns(conn)

    return recorded

def analyze_timing_patterns(conn):
    """Identify profitable/unprofitable time windows."""
    total = conn.execute("SELECT COUNT(*) FROM timing_patterns").fetchone()[0]
    if total < 30:
        return  # need more data

    # Best/worst hours
    hours = conn.execute("""
        SELECT hour_utc, COUNT(*) as n, AVG(won) as wr,
               SUM(profit_cents) as total_profit
        FROM timing_patterns
        GROUP BY hour_utc
        HAVING n >= 3
        ORDER BY wr DESC
    """).fetchall()

    if hours:
        best = hours[0]
        worst = hours[-1]
        print(f"[timing] Best hour: {best[0]}:00 UTC (wr={best[2]:.0%}, n={best[1]})")
        print(f"[timing] Worst hour: {worst[0]}:00 UTC (wr={worst[2]:.0%}, n={worst[1]})")

    # Best/worst day of week
    days = conn.execute("""
        SELECT day_of_week, COUNT(*) as n, AVG(won) as wr,
               SUM(profit_cents) as total_profit
        FROM timing_patterns
        GROUP BY day_of_week
        HAVING n >= 3
        ORDER BY wr DESC
    """).fetchall()

    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    if days:
        best_d = days[0]
        worst_d = days[-1]
        print(f"[timing] Best day: {day_names[best_d[0]]} (wr={best_d[2]:.0%}, n={best_d[1]})")
        print(f"[timing] Worst day: {day_names[worst_d[0]]} (wr={worst_d[2]:.0%}, n={worst_d[1]})")

    # Best/worst source by time
    src_time = conn.execute("""
        SELECT source, CASE WHEN hour_utc BETWEEN 6 AND 18 THEN 'day' ELSE 'night' END as period,
               COUNT(*) as n, AVG(won) as wr
        FROM timing_patterns
        GROUP BY source, period
        HAVING n >= 5
        ORDER BY source, period
    """).fetchall()

    for src, period, n, wr in src_time:
        if wr < 0.40 or wr > 0.65:
            print(f"[timing] {src} during {period}: wr={wr:.0%} (n={n}) "
                  f"{'← strong' if wr > 0.60 else '← weak'}")


# ══════════════════════════════════════════════════════════════════════════════
# LEARNING LOOP 6: HYPERPARAMETER SHADOW EVALUATION
# ══════════════════════════════════════════════════════════════════════════════
# Runs shadow calculations with alternative parameter values alongside real trades.
# Tracks what WOULD have happened with different settings to recommend tuning.

SHADOW_PARAMS = {
    # param_name: [alternative_values_to_test]
    "kelly_fraction": [0.05, 0.15, 0.20],
    "min_edge": [0.03, 0.07, 0.10],
}

def record_shadow_evaluations(conn, result):
    """For each trade this run, compute what would have happened with alternative params."""
    now_str = datetime.now(timezone.utc).isoformat()

    opps = result.get("opportunities", [])
    if not opps:
        return

    for opp in opps:
        ticker = opp.get("ticker", "")
        contracts = opp.get("contracts", 0)
        price_cents = opp.get("price_cents", 50)
        indep_prob = opp.get("independent_prob")
        edge = opp.get("edge")

        if not indep_prob or not price_cents:
            continue

        # Shadow Kelly fractions
        for shadow_kelly in SHADOW_PARAMS.get("kelly_fraction", []):
            # Recompute Kelly with shadow value
            market_prob = price_cents / 100
            edge_val = indep_prob - market_prob
            if edge_val <= 0:
                continue
            b = (100 - price_cents) / price_cents
            q = 1 - indep_prob
            kelly_raw = (b * indep_prob - q) / b
            if kelly_raw <= 0:
                continue
            # Use the first session balance as reference
            bal_row = conn.execute(
                "SELECT balance_cents FROM sessions ORDER BY id DESC LIMIT 1"
            ).fetchone()
            balance = bal_row[0] if bal_row else 10000
            shadow_stake = kelly_raw * shadow_kelly * (balance / 100)
            shadow_contracts = max(1, int(shadow_stake / (price_cents / 100)))

            conn.execute("""INSERT INTO hyperparam_shadow
                (recorded_at, param_name, current_value, shadow_value,
                 ticker, actual_contracts, shadow_contracts, actual_profit, shadow_profit)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (now_str, "kelly_fraction", KELLY_FRACTION, shadow_kelly,
                 ticker, contracts, shadow_contracts, None, None))

    conn.commit()

def analyze_shadow_performance(conn):
    """After settlements, compare actual vs shadow performance.
    Recommends parameter changes when shadow consistently outperforms."""
    now_str = datetime.now(timezone.utc).isoformat()

    # Match shadow records to settlements via order_id (not just ticker, which is ambiguous)
    results = conn.execute("""
        SELECT h.param_name, h.current_value, h.shadow_value,
               h.actual_contracts, h.shadow_contracts,
               s.profit_cents, s.contracts, s.won
        FROM hyperparam_shadow h
        JOIN trades t ON h.ticker = t.ticker
            AND ABS(julianday(h.recorded_at) - julianday(t.timestamp)) < 0.01
        JOIN settlements s ON t.order_id = s.order_id
        WHERE h.actual_profit IS NULL
    """).fetchall()

    if len(results) < 10:
        return

    # Group by param + shadow value
    groups = {}
    for pname, current, shadow, actual_c, shadow_c, profit, settle_c, won in results:
        key = (pname, shadow)
        if key not in groups:
            groups[key] = {"current_val": current, "actual_profit": 0,
                          "shadow_profit": 0, "n": 0}
        # Scale profit proportionally to contract count
        per_contract_profit = profit / settle_c if settle_c > 0 else 0
        groups[key]["actual_profit"] += per_contract_profit * actual_c
        groups[key]["shadow_profit"] += per_contract_profit * shadow_c
        groups[key]["n"] += 1

    for (pname, shadow_val), stats in groups.items():
        if stats["n"] < 10:
            continue
        actual = stats["actual_profit"]
        shadow = stats["shadow_profit"]
        improvement = (shadow - actual) / abs(actual) if actual != 0 else 0

        if abs(improvement) > 0.10:  # >10% difference
            direction = "better" if improvement > 0 else "worse"
            print(f"[shadow] {pname}={shadow_val} would be {abs(improvement):.0%} {direction} "
                  f"than current {stats['current_val']} (n={stats['n']})")

            if improvement > 0.15 and stats["n"] >= 20:
                # Strong evidence for change — log recommendation
                conn.execute("""INSERT INTO strategy_journal
                    (timestamp, entry_type, category, title, detail, metric_value, metric_name)
                    VALUES (?,?,?,?,?,?,?)""",
                    (now_str, "hyperparam_recommendation", pname,
                     f"Consider changing {pname} from {stats['current_val']} to {shadow_val}",
                     f"Shadow testing over {stats['n']} trades shows {improvement:.0%} improvement. "
                     f"Actual profit: {actual:.0f}¢, shadow profit: {shadow:.0f}¢.",
                     improvement, f"shadow_{pname}"))

    conn.commit()


# ══════════════════════════════════════════════════════════════════════════════
# ACTIVE FEEDBACK — learning loops that CHANGE behavior, not just log
# ══════════════════════════════════════════════════════════════════════════════
# This is where the learning loops close. Each function reads from its
# respective DB table and returns actionable adjustments.

def compute_active_feedback(conn):
    """Read ALL learning loop outputs and produce a single feedback dict that
    modifies trading behavior. Called once per run, results passed to score_market().
    Returns dict with keys:
      - disabled_sources: set of source names to skip entirely
      - edge_multiplier: float multiplier on MIN_EDGE (>1 = more conservative)
      - skip_hours: set of hour_utc values to avoid trading in
      - loss_type_adjustments: dict of {loss_type: count} for pattern detection
      - convergence_rate: float, % of edges that converge (None if insufficient data)
    """
    feedback = {
        "disabled_sources": set(),
        "disabled_strategies": set(),
        "edge_multiplier": 1.0,
        "skip_hours": set(),
        "loss_type_adjustments": {},
        "convergence_rate": None,
        "strategy_stats": {},
    }

    # ── 1. Pipeline health → disable broken sources (with recovery) ─────
    # If a source was broken or degraded for the last 8 consecutive runs, disable it.
    # But every 5 runs, re-enable disabled sources to check if they've recovered.
    # Important: "idle" status does NOT count as broken — it means the source was
    # correctly not applicable for the markets it was tested against.
    try:
        sources = conn.execute(
            "SELECT DISTINCT source FROM pipeline_health"
        ).fetchall()
        total_runs = conn.execute("SELECT COUNT(DISTINCT recorded_at) FROM pipeline_health").fetchone()[0]
        for (source,) in sources:
            recent = conn.execute(
                "SELECT status FROM pipeline_health "
                "WHERE source = ? ORDER BY id DESC LIMIT 20",
                (source,)
            ).fetchall()
            # Only count actual failures (broken/degraded), not idle
            failure_count = sum(1 for r in recent if r[0] in ("broken", "degraded"))
            if len(recent) >= 20 and failure_count >= 18:
                # Recovery window: every 3 runs, give disabled sources another chance
                if total_runs % 3 == 0:
                    print(f"[feedback] RECOVERY CHECK: re-enabling source '{source}' "
                          f"(was failing for {failure_count}/{len(recent)} runs, periodic retry)")
                else:
                    feedback["disabled_sources"].add(source)
                    print(f"[feedback] DISABLING source '{source}' — "
                          f"failing {failure_count}/{len(recent)} recent runs")
    except Exception:
        pass

    # ── 2. Edge convergence → tighten edge requirements if edges don't converge
    try:
        conv_rows = conn.execute(
            "SELECT converged, convergence_pct FROM edge_convergence"
        ).fetchall()
        if len(conv_rows) >= 15:
            conv_rate = sum(r[0] for r in conv_rows) / len(conv_rows)
            feedback["convergence_rate"] = conv_rate

            if conv_rate < 0.25:
                # Very few edges converge — we're probably trading noise
                feedback["edge_multiplier"] = max(feedback["edge_multiplier"], 1.5)
                print(f"[feedback] Edge convergence VERY LOW ({conv_rate:.0%}) — "
                      f"requiring 1.5x edge")
            elif conv_rate < 0.40:
                feedback["edge_multiplier"] = max(feedback["edge_multiplier"], 1.25)
                print(f"[feedback] Edge convergence LOW ({conv_rate:.0%}) — "
                      f"requiring 1.25x edge")
            elif conv_rate > 0.60:
                # Strong convergence — edges are real, can be slightly less conservative
                feedback["edge_multiplier"] = min(feedback["edge_multiplier"], 0.90)
                print(f"[feedback] Edge convergence STRONG ({conv_rate:.0%}) — "
                      f"relaxing edge to 0.9x")
    except Exception:
        pass

    # ── 3. Loss post-mortems → detect systematic failure patterns ────────
    try:
        loss_types = conn.execute(
            "SELECT loss_type, COUNT(*) FROM loss_postmortems GROUP BY loss_type"
        ).fetchall()
        total_losses = sum(c for _, c in loss_types)
        for lt, count in loss_types:
            feedback["loss_type_adjustments"][lt] = count
            pct = count / total_losses if total_losses > 0 else 0

            if lt == "fee_erosion" and pct > 0.30 and total_losses >= 10:
                # >30% of losses are fee erosion — need higher edge
                feedback["edge_multiplier"] = max(feedback["edge_multiplier"], 1.3)
                print(f"[feedback] {pct:.0%} of losses are fee_erosion — "
                      f"requiring 1.3x edge")

            if lt == "bad_source" and pct > 0.40 and total_losses >= 10:
                # >40% of losses are bad source estimates — be more conservative
                feedback["edge_multiplier"] = max(feedback["edge_multiplier"], 1.2)
                print(f"[feedback] {pct:.0%} of losses are bad_source — "
                      f"requiring 1.2x edge")
    except Exception:
        pass

    # ── 4. Timing patterns → identify hours to avoid ─────────────────────
    try:
        hours = conn.execute("""
            SELECT hour_utc, COUNT(*) as n, AVG(won) as wr, SUM(profit_cents) as profit
            FROM timing_patterns
            GROUP BY hour_utc
            HAVING n >= 5
        """).fetchall()
        for hour, n, wr, profit in hours:
            if wr < 0.35 and n >= 8:
                # Consistently losing at this hour — skip it
                feedback["skip_hours"].add(hour)
                print(f"[feedback] SKIP hour {hour}:00 UTC — "
                      f"win rate {wr:.0%} over {n} trades")
    except Exception:
        pass

    # ── 5. Per-strategy Thompson Sampling bandit ──────────────────────────
    # Instead of hard disabling, use Thompson Sampling to weight strategies.
    # Strategies with bad track records get low samples (rarely picked),
    # but are NEVER fully killed — they always have a chance of being re-tested.
    try:
        bandit = compute_strategy_bandit(conn)
        feedback["strategy_bandit"] = bandit

        for strat, stats in bandit.items():
            sample = stats["sample"]
            n = stats["n"]
            wins = stats["wins"]
            losses = stats["losses"]
            wr = wins / max(1, wins + losses)

            if n >= 20 and wr < 0.30 and sample < 0.20:
                # Very consistently bad AND drew a low sample this run → skip this run
                # But only for THIS run — next run gets a fresh sample
                feedback["disabled_strategies"].add(strat)
                print(f"[bandit] Strategy '{strat}' COLD this run "
                      f"(wr={wr:.0%}, n={n}, sample={sample:.2f}) — skipping")
            elif stats["explore"]:
                print(f"[bandit] Strategy '{strat}' in EXPLORE mode "
                      f"(wr={wr:.0%}, n={n}, sample={sample:.2f})")
            elif n >= 10:
                status = "STRONG" if wr > 0.55 else "OK" if wr > 0.45 else "WEAK"
                print(f"[bandit] Strategy '{strat}' {status} "
                      f"(wr={wr:.0%}, n={n}, sample={sample:.2f})")

        # Log overall strategy ranking by Thompson sample
        ranked = sorted(bandit.items(), key=lambda x: x[1]["sample"], reverse=True)
        rank_str = " > ".join(f"{s}({v['sample']:.2f})" for s, v in ranked)
        print(f"[bandit] Strategy ranking this run: {rank_str}")

        # Also compute simple stats for the report
        feedback["strategy_stats"] = {
            strat: {"n": s["n"], "win_rate": s["wins"] / max(1, s["wins"] + s["losses"]),
                    "wins": s["wins"], "losses": s["losses"], "sample": s["sample"]}
            for strat, s in bandit.items()
        }
    except Exception as e:
        print(f"[bandit] Error computing strategy bandit: {e}")

    return feedback

# ══════════════════════════════════════════════════════════════════════════════
# RISK CIRCUIT BREAKERS
# ══════════════════════════════════════════════════════════════════════════════
def check_limits(initial, current, portfolio_value=0):
    """Check daily loss and max drawdown limits.
    Uses total equity (balance + portfolio) so that open positions
    don't falsely trigger the loss limit by moving cash to inventory."""
    if initial <= 0: return True, ""
    # Use total equity if portfolio_value is available, otherwise just balance
    current_equity = current + portfolio_value
    lost_pct = (initial - current_equity) / initial
    if lost_pct >= MAX_DRAWDOWN:     return False, f"max_drawdown {lost_pct:.1%}>={MAX_DRAWDOWN:.0%}"
    if lost_pct >= DAILY_LOSS_LIMIT: return False, f"daily_loss {lost_pct:.1%}>={DAILY_LOSS_LIMIT:.0%}"
    return True, ""

# ══════════════════════════════════════════════════════════════════════════════
# KELLY CRITERION — now uses independent probability estimate
# ══════════════════════════════════════════════════════════════════════════════
def kelly_contracts(independent_prob, price_cents, balance_cents):
    """
    Fractional Kelly using INDEPENDENT probability estimate (not market price).
    This is the correct way: edge = our_estimate - market_implied.
    """
    if independent_prob <= 0.02 or independent_prob >= 0.98 or price_cents <= 0:
        return 0  # extreme or invalid probability — don't trade
    # Market implied prob = price_cents / 100
    market_prob = price_cents / 100
    # Our edge
    edge = independent_prob - market_prob
    if edge <= 0:
        return 0  # no edge — don't trade

    # Kelly: b = net profit ratio, p = our prob of winning
    b = (100 - price_cents) / price_cents
    q = 1 - independent_prob
    kelly_raw = (b * independent_prob - q) / b
    if kelly_raw <= 0:
        return 0

    dollar_stake = kelly_raw * KELLY_FRACTION * (balance_cents / 100)
    kelly_contracts_raw = int(dollar_stake / (price_cents / 100))

    # Dynamic cap: min of hard MAX_CONTRACTS and % of balance
    # At $100k and 2% max, with 50¢ contracts → up to 4000 contracts allowed
    # At $600 and 2% max, with 50¢ contracts → up to 24 contracts allowed
    max_dollar_position = (balance_cents / 100) * MAX_POSITION_PCT
    dynamic_max = max(1, int(max_dollar_position / (price_cents / 100)))

    return max(1, min(kelly_contracts_raw, dynamic_max, MAX_CONTRACTS))

# ══════════════════════════════════════════════════════════════════════════════
# STRATEGY 2: EVENT-DRIVEN DATA RELEASE TRADING
# ══════════════════════════════════════════════════════════════════════════════
# Government data releases (BLS, Census, BEA, FOMC) have exact publication times.
# Markets on these events often price in expectations but reprice slowly after
# the actual data drops. Our edge: fetch the data seconds after release and
# compare to market price before it adjusts.

# Calendar of recurring US economic releases (hour, minute in ET)
# Format: {keyword: (source_api, typical_release_hour_et, typical_release_minute_et, description)}
DATA_RELEASE_CALENDAR = {
    # Employment
    "nonfarm":       ("bls", 8, 30, "BLS Employment Situation — first Friday of month"),
    "payroll":       ("bls", 8, 30, "BLS Employment Situation — first Friday of month"),
    "unemployment":  ("bls", 8, 30, "BLS Employment Situation — first Friday of month"),
    "jobless":       ("bls", 8, 30, "BLS Initial Jobless Claims — Thursdays"),
    "initial claims":("bls", 8, 30, "BLS Initial Jobless Claims — Thursdays"),
    # Inflation
    "cpi":           ("bls", 8, 30, "BLS Consumer Price Index — ~12th of month"),
    "ppi":           ("bls", 8, 30, "BLS Producer Price Index — ~15th of month"),
    "pce":           ("bea", 8, 30, "BEA Personal Consumption Expenditures"),
    # GDP
    "gdp":           ("bea", 8, 30, "BEA GDP — quarterly, advance/second/third"),
    # Fed
    "fed funds":     ("fomc", 14, 0, "FOMC Rate Decision — 8 times/year at 2pm ET"),
    "fomc":          ("fomc", 14, 0, "FOMC Rate Decision"),
    "interest rate": ("fomc", 14, 0, "FOMC Rate Decision"),
    # Retail / Housing
    "retail sales":  ("census", 8, 30, "Census Bureau Retail Sales"),
    "housing starts":("census", 8, 30, "Census Bureau Housing Starts"),
    "home sales":    ("census", 10, 0, "NAR Existing Home Sales / Census New Home Sales"),
}

def _is_near_data_release(market_data):
    """Check if this market is tied to a data release happening within 4 hours.
    Returns (release_key, release_info) or (None, None)."""
    title = (market_data.get("title") or market_data.get("subtitle") or "").lower()
    ticker = (market_data.get("ticker") or "").lower()
    text = ticker + " " + title

    for keyword, info in DATA_RELEASE_CALENDAR.items():
        if keyword in text:
            source, hour_et, minute_et, desc = info
            # Check if resolution is within 48 hours (these are near-term event markets)
            days = _days_to_expiry(market_data)
            if days is not None and days <= 2.0:
                return keyword, info
            # Also match if there's a release today
            try:
                from zoneinfo import ZoneInfo
                et = datetime.now(ZoneInfo("America/New_York"))
                release_time = et.replace(hour=hour_et, minute=minute_et, second=0)
                hours_until = (release_time - et).total_seconds() / 3600
                # Within 4 hours before or 1 hour after release
                if -1.0 <= hours_until <= 4.0:
                    return keyword, info
            except Exception:
                pass
    return None, None

def _fetch_bls_latest(series_id):
    """Fetch latest value from BLS API (free, no key needed for low volume).
    BLS updates data at 8:30 AM ET on release days."""
    cache_key = f"bls_{series_id}"
    now = time.time()
    if cache_key in _CACHE and now - _CACHE[cache_key][1] < 300:  # 5 min cache
        return _CACHE[cache_key][0]
    try:
        year = datetime.now(timezone.utc).year
        url = (f"https://api.bls.gov/publicAPI/v2/timeseries/data/{series_id}"
               f"?startyear={year}&endyear={year}&latest=true")
        _rate_limit_wait(url)
        r = requests.get(url, timeout=10)
        data = r.json()
        if data.get("status") == "REQUEST_SUCCEEDED":
            series = data.get("Results", {}).get("series", [])
            if series and series[0].get("data"):
                val = float(series[0]["data"][0]["value"])
                _CACHE[cache_key] = (val, now)
                print(f"[bls] {series_id} latest = {val}")
                return val
    except Exception as e:
        print(f"[bls] Failed to fetch {series_id}: {e}")
    return None

# BLS series IDs for common Kalshi market topics
BLS_SERIES = {
    "unemployment": "LNS14000000",      # Unemployment rate (seasonally adjusted)
    "nonfarm":      "CES0000000001",     # Total nonfarm payrolls
    "payroll":      "CES0000000001",
    "cpi":          "CUSR0000SA0",       # CPI-U all items (seasonally adjusted)
    "jobless":      "LNS13000000",       # Unemployment level (for claims proxy)
    "initial claims":"LNS13000000",
    "ppi":          "WPUFD49104",        # PPI final demand
}

def score_event_driven(m, disabled_sources=None):
    """Strategy 2: Event-driven data release trading.
    Looks for markets tied to government data releases happening soon.
    Fetches the actual released data and compares to market price.
    Returns (score, side, detail, independent_prob, market_prob, edge) or None."""

    release_key, release_info = _is_near_data_release(m)
    if not release_key:
        return None

    source, hour_et, minute_et, desc = release_info
    ticker = m.get("ticker", "")
    title = (m.get("title") or m.get("subtitle") or "").lower()

    _n = lambda v, d=99: (float(v or d) / 100 if float(v or d) > 1 else float(v or d))
    yes_ask = _n(m.get("yes_ask") or m.get("yes_ask_dollars"), 99)
    yes_bid = _n(m.get("yes_bid") or m.get("yes_bid_dollars"), 0)

    # Try to get actual data from BLS
    bls_series = BLS_SERIES.get(release_key)
    actual_value = None
    if bls_series and source == "bls":
        actual_value = _fetch_bls_latest(bls_series)

    # Also try FRED as backup (Cleveland Fed for CPI)
    if actual_value is None and release_key in ("cpi", "pce"):
        # Use existing FRED/Cleveland Fed infrastructure
        if "clevfed" not in (disabled_sources or set()):
            clevfed_prob, clevfed_src = get_cleveland_fed_nowcast(ticker, m)
            if clevfed_prob is not None:
                # Cleveland Fed gives us a probability directly
                edge = clevfed_prob - yes_ask
                if abs(edge) > MIN_EDGE:
                    side = "yes" if edge > 0 else "no"
                    mkt_prob = yes_ask if side == "yes" else (1 - yes_bid)
                    indep_prob = clevfed_prob if side == "yes" else (1 - clevfed_prob)
                    fee_adj = abs(edge) - ESTIMATED_EXIT_SPREAD - (ESTIMATED_FEE_PER_CONTRACT * 2)
                    if fee_adj > MIN_EDGE:
                        score = fee_adj * 15 + 2.0  # bonus for event-driven
                        detail = (f"event_driven: {desc} | clevfed nowcast={clevfed_prob:.2f} "
                                  f"mkt={yes_ask:.2f} edge={edge:+.2f} fee_adj={fee_adj:.2f}")
                        return (score, side, detail, indep_prob, mkt_prob, fee_adj)

    # If we got actual BLS data, try to interpret it against the market question
    if actual_value is not None:
        # Parse threshold from title (e.g., "unemployment rate above 4.0%")
        threshold_match = re.search(r'(above|below|over|under|exceed|at least)\s*(\d+\.?\d*)', title)
        if threshold_match:
            direction = threshold_match.group(1)
            threshold = float(threshold_match.group(2))

            # Determine probability using sigmoid (smooth transition, no cliff)
            # sigma = 0.5% of threshold gives a tight but continuous curve
            sigma = max(threshold * 0.005, 0.01)  # floor at 0.01 to avoid division issues
            diff = actual_value - threshold
            if direction in ("above", "over", "exceed", "at least"):
                # Market asks: "Will X be above threshold?"
                indep_prob = 1.0 / (1.0 + math.exp(-diff / sigma))
            else:  # below, under
                indep_prob = 1.0 / (1.0 + math.exp(diff / sigma))

            edge = indep_prob - yes_ask
            fee_adj = abs(edge) - ESTIMATED_EXIT_SPREAD - (ESTIMATED_FEE_PER_CONTRACT * 2)

            if fee_adj > MIN_EDGE * 0.8:  # slightly lower threshold for event-driven (data is strong)
                side = "yes" if edge > 0 else "no"
                mkt_prob = yes_ask if side == "yes" else (1 - yes_bid)
                final_prob = indep_prob if side == "yes" else (1 - indep_prob)
                score = fee_adj * 20 + 3.0  # strong bonus for actual data
                detail = (f"event_driven: {desc} | BLS {release_key}={actual_value} "
                          f"vs threshold={threshold} → prob={indep_prob:.2f} "
                          f"mkt={yes_ask:.2f} edge={edge:+.2f}")
                return (score, side, detail, final_prob, mkt_prob, fee_adj)

    return None


# ══════════════════════════════════════════════════════════════════════════════
# STRATEGY 3: CROSS-MARKET ARBITRAGE
# ══════════════════════════════════════════════════════════════════════════════
# When multiple prediction markets (Polymarket, Manifold, Metaculus) agree on
# a probability and Kalshi diverges, the consensus is usually right.

def _fetch_manifold_markets(query, limit=5):
    """Search Manifold Markets API for matching markets. Free, no auth needed."""
    cache_key = f"manifold_{query[:40]}"
    now = time.time()
    if cache_key in _CACHE and now - _CACHE[cache_key][1] < 600:  # 10 min cache
        return _CACHE[cache_key][0]
    try:
        url = f"https://api.manifold.markets/v0/search-markets?term={urllib.parse.quote(query)}&limit={limit}"
        _rate_limit_wait(url)
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            markets = r.json()
            # Filter to binary markets only
            binary = [m for m in markets if m.get("outcomeType") == "BINARY"
                      and m.get("isResolved") is not True
                      and m.get("closeTime", 0) > time.time() * 1000]
            _CACHE[cache_key] = (binary, now)
            return binary
    except Exception as e:
        print(f"[manifold] Search failed for '{query[:30]}': {e}")
    return []

def _best_manifold_match(title, manifold_markets):
    """Find the best matching Manifold market by keyword overlap."""
    if not manifold_markets:
        return None
    title_words = set(title.lower().split())
    # Remove common stop words
    stop = {"the", "a", "an", "will", "be", "is", "to", "in", "on", "of", "by", "at", "for"}
    title_words -= stop

    best_match = None
    best_score = 0
    for mm in manifold_markets:
        q = (mm.get("question") or "").lower()
        q_words = set(q.split()) - stop
        if not q_words:
            continue
        overlap = len(title_words & q_words)
        jaccard = overlap / max(1, len(title_words | q_words))
        if jaccard > best_score and jaccard > 0.25:  # minimum 25% word overlap
            best_score = jaccard
            best_match = mm
    return best_match

def score_cross_market(m, adaptive_weights=None, calibration_corrections=None,
                       disabled_sources=None):
    """Strategy 3: Cross-market arbitrage.
    Aggregates probability estimates from multiple prediction markets.
    If 2+ external markets agree and Kalshi diverges by >10%, that's a strong signal.
    Returns (score, side, detail, independent_prob, market_prob, edge) or None."""

    title = m.get("title") or m.get("subtitle") or ""
    ticker = m.get("ticker", "")
    if not title:
        return None

    _n = lambda v, d=99: (float(v or d) / 100 if float(v or d) > 1 else float(v or d))
    yes_ask = _n(m.get("yes_ask") or m.get("yes_ask_dollars"), 99)
    yes_bid = _n(m.get("yes_bid") or m.get("yes_bid_dollars"), 0)
    volume = float(m.get("volume") or m.get("volume_24h_fp") or m.get("volume_fp") or 0)

    _disabled = disabled_sources or set()
    external_probs = []  # list of (prob, source_name, weight)

    # 1. Polymarket (already have this infrastructure)
    if "polymarket" not in _disabled:
        try:
            poly_prob, poly_src = get_polymarket_estimate(ticker, m)
            if poly_prob is not None:
                external_probs.append((poly_prob, "polymarket", 0.80))
        except Exception:
            pass

    # 2. Manifold Markets (new)
    if "manifold" not in _disabled:
        try:
            manifold_results = _fetch_manifold_markets(title[:80])
            match = _best_manifold_match(title, manifold_results)
            if match:
                mf_prob = match.get("probability")
                mf_volume = match.get("volume", 0)
                mf_question = (match.get("question") or "")[:50]
                if mf_prob is not None and mf_volume >= 100:  # minimum volume filter
                    external_probs.append((float(mf_prob), f"manifold:{mf_question}", 0.65))
                    print(f"[manifold] Match: '{title[:40]}' ↔ '{mf_question}' "
                          f"→ prob={mf_prob:.2f} vol={mf_volume:.0f}")
        except Exception as e:
            print(f"[manifold] Error: {e}")

    # 3. Metaculus (already have this infrastructure)
    if "metaculus" not in _disabled:
        try:
            meta_prob, meta_src = get_metaculus_estimate(ticker, m)
            if meta_prob is not None:
                external_probs.append((meta_prob, f"metaculus:{meta_src}", 0.70))
        except Exception:
            pass

    # Need at least 2 external markets to form a consensus
    if len(external_probs) < 2:
        return None

    # Compute weighted consensus probability
    total_weight = sum(w for _, _, w in external_probs)
    consensus_prob = sum(p * w for p, _, w in external_probs) / total_weight

    # Check agreement: all sources must be within 15% of each other
    probs_only = [p for p, _, _ in external_probs]
    spread = max(probs_only) - min(probs_only)
    if spread > 0.15:
        # Sources disagree too much — no consensus
        return None

    # Compare consensus to Kalshi
    edge = consensus_prob - yes_ask
    round_trip_cost = ESTIMATED_EXIT_SPREAD + (ESTIMATED_FEE_PER_CONTRACT * 2)
    fee_adj = abs(edge) - round_trip_cost

    # Need strong divergence for cross-market arb (these are efficient markets)
    required = MIN_EDGE * 1.2  # slightly higher bar since all sources are public
    if fee_adj < required:
        return None

    side = "yes" if edge > 0 else "no"
    mkt_prob = yes_ask if side == "yes" else (1 - yes_bid)
    indep_prob = consensus_prob if side == "yes" else (1 - consensus_prob)

    sources_str = " + ".join(src for _, src, _ in external_probs)
    n_sources = len(external_probs)
    score = fee_adj * 12 + n_sources * 0.5 + 1.0  # bonus for consensus
    days = _days_to_expiry(m)
    if days and days > 0:
        time_mult = min(2.0, 1.0 / math.sqrt(max(days, 0.25)))
        score *= time_mult

    detail = (f"cross_market: {n_sources} markets agree | {sources_str} "
              f"consensus={consensus_prob:.2f} kalshi={yes_ask:.2f} "
              f"divergence={abs(edge):.2f} fee_adj={fee_adj:.2f} "
              f"source_spread={spread:.2f}")
    return (score, side, detail, indep_prob, mkt_prob, fee_adj)


# ══════════════════════════════════════════════════════════════════════════════
# STRATEGY 4: NEAR-RESOLUTION CONVERGENCE
# ══════════════════════════════════════════════════════════════════════════════
# Markets resolving within 24 hours where we have strong, fresh data and the
# market price is stale. The closer to resolution, the more our data is worth
# and the less time for adverse price movement.

def score_near_resolution(m, adaptive_weights=None, calibration_corrections=None,
                          disabled_sources=None):
    """Strategy 4: Near-resolution convergence trading.
    Targets markets resolving in <24h where our ensemble has strong, fresh data.
    Uses a tighter time window and higher confidence threshold.
    Returns (score, side, detail, independent_prob, market_prob, edge) or None."""

    days = _days_to_expiry(m)
    if days is None or days > 1.0:
        return None  # only care about <24h markets

    ticker = m.get("ticker", "")
    title = (m.get("title") or m.get("subtitle") or "")

    _n = lambda v, d=99: (float(v or d) / 100 if float(v or d) > 1 else float(v or d))
    yes_ask = _n(m.get("yes_ask") or m.get("yes_ask_dollars"), 99)
    yes_bid = _n(m.get("yes_bid") or m.get("yes_bid_dollars"), 0)
    volume = float(m.get("volume") or m.get("volume_24h_fp") or m.get("volume_fp") or 0)

    # Get ensemble estimate
    weights = adaptive_weights or SOURCE_WEIGHTS
    _disabled = disabled_sources or set()
    estimates = []

    # Only use high-confidence sources for near-resolution
    # These are sources that have domain-specific, current data
    high_confidence_sources = [
        ("weather", get_weather_estimate),
        ("noaa", get_noaa_alerts_for_market),
        ("odds", get_sports_estimate),
        ("clevfed", get_cleveland_fed_nowcast),
        ("crypto", get_crypto_estimate),
    ]

    for src_name, func in high_confidence_sources:
        if src_name in _disabled:
            continue
        try:
            prob, src = func(ticker, m)
            if prob is not None:
                w = weights.get(src_name, 0.5)
                estimates.append((prob, w, f"{src_name}:{src}"))
        except Exception:
            pass

    if not estimates:
        return None

    # Need higher confidence for near-resolution: either 2+ sources or 1 very strong one
    total_weight = sum(w for _, w, _ in estimates)
    if len(estimates) == 1 and total_weight < 0.75:
        return None

    ensemble_prob = sum(p * w for p, w, _ in estimates) / total_weight

    # Edge calculation
    edge = ensemble_prob - yes_ask
    round_trip_cost = ESTIMATED_EXIT_SPREAD + (ESTIMATED_FEE_PER_CONTRACT * 2)
    fee_adj = abs(edge) - round_trip_cost

    # Lower edge threshold for near-resolution: data is fresh, resolution is imminent
    # Risk of adverse move is minimal since market closes soon
    required = MIN_EDGE * 0.7  # 30% lower threshold
    if fee_adj < required:
        return None

    side = "yes" if edge > 0 else "no"
    mkt_prob = yes_ask if side == "yes" else (1 - yes_bid)
    indep_prob = ensemble_prob if side == "yes" else (1 - ensemble_prob)

    hours_left = days * 24
    sources_str = " + ".join(src for _, _, src in estimates)
    score = fee_adj * 25 + 4.0  # big bonus for near-resolution certainty
    # Closer to resolution = higher score
    if hours_left < 6:
        score *= 1.5
    if hours_left < 2:
        score *= 2.0

    detail = (f"near_resolution: {hours_left:.1f}h left | {sources_str} "
              f"ensemble={ensemble_prob:.2f} mkt={yes_ask:.2f} "
              f"edge={abs(edge):.2f} fee_adj={fee_adj:.2f} "
              f"n_sources={len(estimates)}")
    return (score, side, detail, indep_prob, mkt_prob, fee_adj)


# ══════════════════════════════════════════════════════════════════════════════
# MULTI-STRATEGY SCORING — runs all strategies, picks the best signal
# ══════════════════════════════════════════════════════════════════════════════
# Each strategy returns (score, side, detail, independent_prob, market_prob, edge)
# or None if it doesn't apply. The highest-scoring strategy wins.

STRATEGY_REGISTRY = [
    # (name, function, description)
    ("info_edge",        None,                   "Ensemble mispricing — 12-source weighted estimate vs market price"),
    ("event_driven",     score_event_driven,      "Event-driven — government data release timing edge"),
    ("cross_market",     score_cross_market,      "Cross-market arb — consensus from Polymarket + Manifold + Metaculus"),
    ("near_resolution",  score_near_resolution,   "Near-resolution — <24h markets with fresh domain data"),
]

# ══════════════════════════════════════════════════════════════════════════════
# MARKET SCORING — multi-strategy (v3.8)
# ══════════════════════════════════════════════════════════════════════════════
def score_market(m, adaptive_weights=None, calibration_corrections=None, category_edges=None,
                 disabled_sources=None, disabled_strategies=None, strategy_bandit=None):
    """
    Returns (score, side, strategy, detail, volume, spread_cents,
             independent_prob, market_prob, edge)
    score=0 → no trade.
    """
    def _n(v, d=99):
        v = float(v or d); return v/100 if v > 1.0 else v

    yes_ask = _n(m.get("yes_ask") or m.get("yes_ask_dollars"), 99)
    yes_bid = _n(m.get("yes_bid") or m.get("yes_bid_dollars"),  0)
    no_ask  = _n(m.get("no_ask")  or m.get("no_ask_dollars"),  99)
    no_bid  = _n(m.get("no_bid")  or m.get("no_bid_dollars"),   0)
    volume  = float(m.get("volume") or m.get("volume_24h_fp") or m.get("volume_fp") or 0)
    spread  = yes_ask - yes_bid
    sc      = round(spread * 100, 1)
    ticker  = m.get("ticker", "")

    EMPTY = (0.0, "", "", "", 0, 0, None, None, 0)

    # Skip very illiquid or very cheap markets
    if yes_ask <= 0.08 or yes_ask >= 0.92:
        return EMPTY
    if volume < 50:
        return EMPTY
    if spread <= 0:
        return EMPTY

    # ── Get ensemble probability estimate (with adaptive weights + calibration) ─
    indep_prob, info_source, n_sources = get_independent_estimate(
        ticker, m, yes_ask, volume,
        adaptive_weights=adaptive_weights,
        calibration_corrections=calibration_corrections,
        disabled_sources=disabled_sources
    )

    # ── Information-edge trading only (ensemble-backed) ──────────────────
    # Market making and liquidity harvest removed — they had no real information edge.
    # Only trade when we have independent data that diverges from market price.
    if indep_prob is None or n_sources == 0:
        return EMPTY

    # Adaptive edge threshold: more sources → more confidence → lower threshold
    if n_sources >= 3:
        required_edge = MIN_EDGE                    # 5% with 3+ sources
    elif n_sources == 2:
        required_edge = MIN_EDGE + 0.02             # 7% with 2 sources
    else:
        required_edge = SINGLE_SOURCE_EDGE          # 10% with only 1 source

    # Apply learned category-specific edge multiplier
    if category_edges:
        title = m.get("title", "") or m.get("subtitle", "") or ""
        cat = categorize_market(ticker, title)
        cat_mult = category_edges.get(cat, 1.0)
        if cat_mult != 1.0:
            required_edge *= cat_mult

    market_prob_yes = yes_ask
    edge_yes = indep_prob - market_prob_yes
    edge_no  = (1 - indep_prob) - (1 - yes_bid) if yes_bid > 0 else 0

    # ── Fee accounting: subtract estimated round-trip costs from edge ────
    # Real profitability = edge - entry_spread - exit_spread - fees
    # Entry cost is already baked into the ask price. Exit slippage + fees must be subtracted.
    round_trip_cost = ESTIMATED_EXIT_SPREAD + (ESTIMATED_FEE_PER_CONTRACT * 2)  # entry + exit fees
    fee_adjusted_edge_yes = edge_yes - round_trip_cost
    fee_adjusted_edge_no  = edge_no - round_trip_cost

    # ── Time-priority scoring: shorter-dated markets = better capital efficiency ─
    # Edge per day of capital committed. Markets resolving in 1 day get full weight;
    # 30-day markets get ~20% of base score. Prevents capital lock-up in slow markets.
    days = _days_to_expiry(m)
    if days is not None and days > 0:
        time_multiplier = min(2.0, 1.0 / math.sqrt(max(days, 0.25)))  # 1d→1.0, 4d→0.5, 30d→0.18
    else:
        time_multiplier = 0.5  # unknown expiry → conservative

    # ── Candidate collection: run ALL strategies and pick the best ────────
    # Each candidate: (score, side, strategy_name, detail, indep_prob, mkt_prob, edge)
    candidates = []
    _disabled_strats = disabled_strategies or set()

    # Strategy 1: Original info_edge (ensemble mispricing)
    if fee_adjusted_edge_yes > required_edge and spread < 0.08:
        base_score = fee_adjusted_edge_yes * 10 + volume / 10000 + n_sources * 0.1
        s1_score = base_score * time_multiplier
        detail = (f"info_edge: {info_source} indep={indep_prob:.2f} "
                  f"mkt={market_prob_yes:.2f} raw_edge={edge_yes:.2f} "
                  f"fee_adj={fee_adjusted_edge_yes:.2f} sources={n_sources} "
                  f"days={f'{days:.1f}' if days else '?'} time_mult={time_multiplier:.2f}")
        candidates.append((s1_score, "yes", "info_edge", detail, indep_prob, market_prob_yes, fee_adjusted_edge_yes))

    if fee_adjusted_edge_no > required_edge and spread < 0.08:
        base_score = fee_adjusted_edge_no * 10 + volume / 10000 + n_sources * 0.1
        s1_score = base_score * time_multiplier
        market_prob_no = 1 - yes_bid
        detail = (f"info_edge: {info_source} indep_no={1-indep_prob:.2f} "
                  f"mkt_no={market_prob_no:.2f} raw_edge={edge_no:.2f} "
                  f"fee_adj={fee_adjusted_edge_no:.2f} sources={n_sources} "
                  f"days={f'{days:.1f}' if days else '?'} time_mult={time_multiplier:.2f}")
        candidates.append((s1_score, "no", "info_edge", detail, 1-indep_prob, market_prob_no, fee_adjusted_edge_no))

    # Strategy 2: Event-driven data release
    if "event_driven" not in _disabled_strats:
        try:
            evt = score_event_driven(m, disabled_sources=disabled_sources)
            if evt:
                s, side, detail, ip, mp, edge = evt
                candidates.append((s, side, "event_driven", detail, ip, mp, edge))
        except Exception as e:
            print(f"[strategy] event_driven error: {e}")

    # Strategy 3: Cross-market arbitrage
    if "cross_market" not in _disabled_strats:
        try:
            xmkt = score_cross_market(m, adaptive_weights=adaptive_weights,
                                       calibration_corrections=calibration_corrections,
                                       disabled_sources=disabled_sources)
            if xmkt:
                s, side, detail, ip, mp, edge = xmkt
                candidates.append((s, side, "cross_market", detail, ip, mp, edge))
        except Exception as e:
            print(f"[strategy] cross_market error: {e}")

    # Strategy 4: Near-resolution convergence
    if "near_resolution" not in _disabled_strats:
        try:
            nr = score_near_resolution(m, adaptive_weights=adaptive_weights,
                                        calibration_corrections=calibration_corrections,
                                        disabled_sources=disabled_sources)
            if nr:
                s, side, detail, ip, mp, edge = nr
                candidates.append((s, side, "near_resolution", detail, ip, mp, edge))
        except Exception as e:
            print(f"[strategy] near_resolution error: {e}")

    if not candidates:
        return EMPTY

    # Weight each candidate's score by its Thompson Sampling posterior draw.
    # This means proven strategies get full credit while unproven ones are
    # discounted (but not zeroed — always a chance to be picked).
    _bandit = strategy_bandit or {}
    def _bandit_adjusted_score(candidate):
        raw_score, _, strat_name, _, _, _, _ = candidate
        if strat_name in _bandit:
            # Thompson sample ∈ [0,1] — multiply by score
            # Minimum 0.1 floor so no strategy is completely silenced
            ts = max(0.1, _bandit[strat_name].get("sample", 0.5))
            return raw_score * ts
        return raw_score * 0.5  # unknown strategy → conservative

    best = max(candidates, key=_bandit_adjusted_score)
    best_score, best_side, best_strat, best_detail, best_ip, best_mp, best_edge = best

    # If multiple strategies found a signal, note that in the detail
    if len(candidates) > 1:
        strat_names = [c[2] for c in candidates]
        best_detail += f" [also: {', '.join(s for s in strat_names if s != best_strat)}]"

    return best_score, best_side, best_strat, best_detail, volume, sc, best_ip, best_mp, best_edge

def passes_filters(ticker, strategy, volume, spread_cents, af):
    vt = af.get("low_volume_threshold")
    if vt and volume < vt: return False, f"learned: vol {volume:.0f}<{vt}"
    if strategy in af.get("avoided_strategies", set()): return False, f"learned: strat '{strategy}'"
    if ticker[:6] in af.get("avoided_prefixes", set()): return False, f"learned: prefix '{ticker[:6]}'"
    return True, ""

def get_open_tickers():
    try:
        orders = api_get("/portfolio/orders?status=resting&limit=100").get("orders", [])
        s = {(o.get("ticker",""), o.get("side","")) for o in orders if o.get("ticker")}
        print(f"[dedup] {len(s)} open orders")
        return s
    except Exception as e:
        print(f"[dedup] Error: {e}"); return set()

def get_orderbook_depth(ticker, side, price_cents):
    """Check order book depth at or near the target price.
    Returns max contracts we can fill within 2¢ of target price without excessive slippage.
    If order book unavailable, returns None (meaning 'unknown, proceed with caution')."""
    try:
        resp = api_get(f"/markets/{ticker}/orderbook")
        book = resp.get("orderbook", resp)
        # Kalshi orderbook: {"yes": [[price, quantity], ...], "no": [[price, quantity], ...]}
        # We want to BUY, so we look at the ASK side (what sellers are offering)
        if side == "yes":
            asks = book.get("yes", [])
        else:
            asks = book.get("no", [])
        if not asks:
            return None

        # Sum up available contracts within 2¢ of our target price
        available = 0
        for level in asks:
            if isinstance(level, (list, tuple)) and len(level) >= 2:
                lvl_price = int(level[0])
                lvl_qty = int(level[1])
                if lvl_price <= price_cents + 2:  # within 2¢ slippage tolerance
                    available += lvl_qty

        return available if available > 0 else None
    except Exception as e:
        print(f"[orderbook] Failed for {ticker}: {e}")
        return None

# ══════════════════════════════════════════════════════════════════════════════
# DAILY LOSS TRACKING (persistent across runs)
# ══════════════════════════════════════════════════════════════════════════════
def get_day_start_balance(conn):
    """Get total equity (balance + portfolio) from the first session of today (UTC).
    Uses total equity so open positions don't falsely trigger daily loss limits."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT balance_cents, COALESCE(portfolio_cents, 0) FROM sessions WHERE timestamp LIKE ? ORDER BY id ASC LIMIT 1",
        (today + "%",)
    ).fetchone()
    return (row[0] + row[1]) if row else None

# ══════════════════════════════════════════════════════════════════════════════
# CORRELATION / CATEGORY LIMITS
# ══════════════════════════════════════════════════════════════════════════════
CATEGORY_KEYWORDS = {
    "economics": ["cpi", "inflation", "unemployment", "gdp", "nonfarm", "payroll",
                  "fed funds", "fomc", "interest rate", "jobs report",
                  "federal funds", "fed rate", "kxfed", "kxcpi", "kxgdp", "kxjob", "kxunrate"],
    "crypto":    ["btc", "bitcoin", "eth", "ether", "sol", "solana", "crypto", "coin"],
    "weather":   ["temperature", "temp", "weather", "degrees", "°f", "°c", "heat", "cold", "freeze",
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

def categorize_market(ticker, title):
    """Assign a market to a risk category based on ticker and title.
    Company tickers get priority — e.g. KXEARNINGSMENTIONNFLX-26APR16-MLB
    should be 'company' not 'sports' despite containing 'mlb'."""
    text = (ticker + " " + title).lower()
    ticker_lower = ticker.lower()

    # Priority check: company ticker prefixes always win
    _COMPANY_PREFIXES = [
        "kxboeing", "kxspotifymau", "kxubertrips", "kxmetaheadcount",
        "kxhood", "kxdashorders", "kxlyft", "kxmtch", "kxpltr",
        "kxrace", "kxpm", "kxabnb", "kxteslasemi", "kxismpmi",
        "kxearningsmention", "kxearningmention", "kxstripeipo",
    ]
    if any(ticker_lower.startswith(p) for p in _COMPANY_PREFIXES):
        return "company"

    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return category
    return "other"

# ══════════════════════════════════════════════════════════════════════════════
# FILL TRACKING
# ══════════════════════════════════════════════════════════════════════════════
def track_fills(conn):
    """Check fill status of recent orders and update trades table.
    Critical for evaluating which strategies actually work vs just placing orders."""
    try:
        orders = api_get("/portfolio/orders?limit=200").get("orders", [])
    except Exception as e:
        print(f"[fills] Could not fetch orders: {e}"); return

    filled = partial = unfilled = 0
    for o in orders:
        oid = o.get("order_id", "")
        status = o.get("status", "")
        if not oid:
            continue
        if status in ("executed", "filled"):
            filled += 1
        elif status == "partial":
            partial += 1
        elif status in ("canceled", "cancelled", "expired"):
            unfilled += 1
        else:
            continue

        # Update our trades table with fill status
        # Allow promotion: partial → filled/executed (no IS NULL restriction)
        conn.execute(
            "UPDATE trades SET fill_status=? WHERE order_id=?",
            (status, oid))

    conn.commit()
    total = filled + partial + unfilled
    if total > 0:
        fill_rate = (filled + partial) / total
        print(f"[fills] filled={filled} partial={partial} unfilled={unfilled} "
              f"fill_rate={fill_rate:.0%} (of {total} completed orders)")
    else:
        print("[fills] No completed orders to track")

# ══════════════════════════════════════════════════════════════════════════════
# PERFORMANCE REPORT — human-readable markdown updated each run
# ══════════════════════════════════════════════════════════════════════════════
REPORT_PATH = os.environ.get("REPORT_PATH", "/task/PERFORMANCE_REPORT.md")

def _journal_entry(conn, entry_type, category, title, detail, metric_value=None, metric_name=None):
    """Write a structured entry to the strategy journal."""
    conn.execute("""INSERT INTO strategy_journal
        (timestamp, entry_type, category, title, detail, metric_value, metric_name)
        VALUES (?,?,?,?,?,?,?)""",
        (datetime.now(timezone.utc).isoformat(), entry_type, category, title, detail,
         metric_value, metric_name))

def generate_performance_report(conn, result):
    """Generate a comprehensive, human-readable performance report as markdown.
    Written to REPORT_PATH every run so Josh can check bot health at a glance."""
    now = datetime.now(timezone.utc)
    lines = []
    lines.append("# Kalshi Trading Bot — Performance Report")
    lines.append(f"**Generated:** {now.strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"**Version:** v3.4 (12-source ensemble, phased sizing)")
    lines.append("")

    # ═══════════════════════════════════════════════════════════════════════
    # 0. DEPLOYMENT PHASE
    # ═══════════════════════════════════════════════════════════════════════
    phase_info = result.get("phase", "?")
    phase_desc = result.get("phase_desc", "Unknown")
    phase_stats = result.get("phase_stats", {})
    eff_limits = result.get("effective_limits", {})
    lines.append("## 0. Deployment Phase")
    lines.append("")
    lines.append(f"**Current Phase: {phase_info}/5** — {phase_desc}")
    lines.append("")
    if phase_stats:
        lines.append(f"- **Settled trades:** {phase_stats.get('settled', 0)} "
                     f"(need {PHASE_CONFIG.get(phase_info + 1, (999,))[0] if isinstance(phase_info, int) and phase_info < 5 else 'N/A'} for next phase)")
        lines.append(f"- **Win rate:** {phase_stats.get('win_rate', 0):.1%} "
                     f"(need {PHASE_CONFIG.get(phase_info + 1, (0, 0.99))[1]:.0%} for next phase)" if isinstance(phase_info, int) and phase_info < 5 else "")
        lines.append(f"- **Recent win rate (last 100):** {phase_stats.get('recent_win_rate', 0):.1%} "
                     f"{'⚠️ Below 48% — auto-downgrade active' if phase_stats.get('recent_win_rate', 1) < 0.48 and phase_stats.get('recent_n', 0) >= 30 else '✓'}")
    if eff_limits:
        lines.append("")
        lines.append(f"- **Max position:** {eff_limits.get('max_position_pct', 0):.2%} of balance")
        lines.append(f"- **Max portfolio:** {eff_limits.get('max_portfolio_pct', 0):.1%} of balance")
        lines.append(f"- **Max contracts:** {eff_limits.get('max_contracts', 0)}")
        lines.append(f"- **Kelly multiplier:** {eff_limits.get('kelly_fraction', 0):.4f}")
        lines.append(f"- **Min edge:** {eff_limits.get('min_edge', 0):.1%}")
        lines.append(f"- **DRY_RUN:** {eff_limits.get('dry_run', True)}")
    lines.append("")

    # Phase progression roadmap
    lines.append("**Phase Progression:**")
    lines.append("")
    lines.append("| Phase | Requirement | Max Position | Max Portfolio | Status |")
    lines.append("|-------|------------|--------------|---------------|--------|")
    for pn in sorted(PHASE_CONFIG.keys()):
        pcfg = PHASE_CONFIG[pn]
        req = f"{pcfg[0]} trades, {pcfg[1]:.0%} WR" if pcfg[0] > 0 else "Start"
        max_pos = f"{pcfg[2]:.2%}" if pcfg[2] > 0 else "Paper"
        max_port = f"{pcfg[3]:.1%}" if pcfg[3] > 0 else "Paper"
        status = "◀ CURRENT" if pn == phase_info else ("✓" if isinstance(phase_info, int) and pn < phase_info else "—")
        lines.append(f"| {pn} | {req} | {max_pos} | {max_port} | {status} |")
    lines.append("")

    # ═══════════════════════════════════════════════════════════════════════
    # 1. ACCOUNT OVERVIEW
    # ═══════════════════════════════════════════════════════════════════════
    lines.append("## 1. Account Overview")
    lines.append("")
    sessions = conn.execute(
        "SELECT timestamp, balance_cents, portfolio_cents FROM sessions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if sessions:
        bal = sessions[1] / 100 if sessions[1] else 0
        port = sessions[2] / 100 if sessions[2] else 0
        lines.append(f"- **Current balance:** ${bal:,.2f}")
        lines.append(f"- **Open positions value:** ${port:,.2f}")
        lines.append(f"- **Total (balance + positions):** ${bal + port:,.2f}")
    else:
        lines.append("- No session data yet.")

    # Balance history (last 7 days)
    week_ago = (now - timedelta(days=7)).isoformat()
    balance_history = conn.execute(
        "SELECT DATE(timestamp) as day, MIN(balance_cents) as low, MAX(balance_cents) as high, "
        "balance_cents FROM sessions WHERE timestamp > ? GROUP BY day ORDER BY day",
        (week_ago,)
    ).fetchall()
    if len(balance_history) > 1:
        lines.append("")
        lines.append("**Balance (last 7 days):**")
        lines.append("")
        lines.append("| Date | Low | High |")
        lines.append("|------|-----|------|")
        for day, low, high, _ in balance_history:
            lines.append(f"| {day} | ${low/100:,.2f} | ${high/100:,.2f} |")

    # ═══════════════════════════════════════════════════════════════════════
    # 2. TRADE PERFORMANCE
    # ═══════════════════════════════════════════════════════════════════════
    lines.append("")
    lines.append("## 2. Trade Performance")
    lines.append("")

    total_settled = conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0]
    total_won = conn.execute("SELECT COUNT(*) FROM settlements WHERE won=1").fetchone()[0]
    total_profit = conn.execute("SELECT COALESCE(SUM(profit_cents),0) FROM settlements").fetchone()[0]
    total_trades = conn.execute("SELECT COUNT(*) FROM trades WHERE action='buy'").fetchone()[0]
    filled_trades = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE fill_status IN ('executed','filled')"
    ).fetchone()[0]

    lines.append(f"- **Total trades placed:** {total_trades}")
    lines.append(f"- **Trades filled:** {filled_trades}")
    lines.append(f"- **Trades settled:** {total_settled}")
    if total_settled > 0:
        wr = total_won / total_settled
        lines.append(f"- **Win rate:** {wr:.0%} ({total_won}/{total_settled})")
        lines.append(f"- **Total P&L from settlements:** ${total_profit/100:,.2f}")
        avg_profit = total_profit / total_settled
        lines.append(f"- **Average profit per settled trade:** ${avg_profit/100:,.2f}")
    if total_trades > 0 and filled_trades > 0:
        lines.append(f"- **Fill rate:** {filled_trades/total_trades:.0%}")

    # ═══════════════════════════════════════════════════════════════════════
    # 3. WHAT'S WORKING — by data source
    # ═══════════════════════════════════════════════════════════════════════
    lines.append("")
    lines.append("## 3. What's Working (by Data Source)")
    lines.append("")
    lines.append("Which information sources are generating profitable trades?")
    lines.append("")

    # Parse source from the 'reason' field in trades, match to settlements
    source_stats = conn.execute("""
        SELECT t.reason, s.won, s.profit_cents, t.edge, t.independent_prob, t.market_prob
        FROM trades t
        JOIN settlements s ON t.order_id = s.order_id
        WHERE t.reason IS NOT NULL AND t.order_id IS NOT NULL
    """).fetchall()

    if source_stats:
        source_perf = {}
        for reason, won, profit, edge, indep, mkt in source_stats:
            # Extract source names from reason like "info_edge: ensemble(polymarket+crypto)"
            sources_found = []
            for src_name in SOURCE_WEIGHTS.keys():
                if src_name in (reason or "").lower():
                    sources_found.append(src_name)
            if not sources_found:
                sources_found = ["unknown"]
            for src in sources_found:
                if src not in source_perf:
                    source_perf[src] = {"wins": 0, "losses": 0, "profit": 0, "edges": [], "count": 0}
                source_perf[src]["count"] += 1
                source_perf[src]["profit"] += (profit or 0)
                if won:
                    source_perf[src]["wins"] += 1
                else:
                    source_perf[src]["losses"] += 1
                if edge:
                    source_perf[src]["edges"].append(edge)

        lines.append("| Source | Trades | Win Rate | Total P&L | Avg Edge | Status |")
        lines.append("|--------|--------|----------|-----------|----------|--------|")
        for src in sorted(source_perf.keys(), key=lambda s: source_perf[s]["profit"], reverse=True):
            sp = source_perf[src]
            n = sp["count"]
            wr = sp["wins"] / n if n > 0 else 0
            pnl = sp["profit"] / 100
            avg_edge = sum(sp["edges"]) / len(sp["edges"]) if sp["edges"] else 0
            if n >= 5:
                status = "PROFITABLE" if pnl > 0 and wr > 0.50 else "UNPROFITABLE" if pnl < 0 else "MARGINAL"
            else:
                status = f"TESTING (n={n})"

            # Journal significant findings
            if n >= 5:
                _journal_entry(conn, "source_eval", src,
                    f"{src}: {'profitable' if pnl > 0 else 'unprofitable'}",
                    f"wr={wr:.0%} pnl=${pnl:.2f} n={n} avg_edge={avg_edge:.2f}",
                    pnl, "total_pnl")

            lines.append(f"| {src} | {n} | {wr:.0%} | ${pnl:,.2f} | {avg_edge:.1%} | {status} |")
    else:
        lines.append("No settled trades with source data yet. Run the bot and let markets resolve.")

    # ═══════════════════════════════════════════════════════════════════════
    # 4. WHAT'S NOT WORKING — strategies/categories being avoided
    # ═══════════════════════════════════════════════════════════════════════
    lines.append("")
    lines.append("## 4. What's Not Working (Learned Avoidance)")
    lines.append("")
    lines.append("Strategies, market categories, and volume ranges the bot has learned to avoid.")
    lines.append("")

    avoided_strats = conn.execute("""
        SELECT strategy, COUNT(*) as n, SUM(won) as wins, SUM(profit_cents) as pnl
        FROM settlements WHERE strategy IS NOT NULL
        GROUP BY strategy HAVING n >= 3
        ORDER BY (CAST(wins AS REAL) / n) ASC
    """).fetchall()
    if avoided_strats:
        lines.append("| Strategy | Trades | Win Rate | P&L | Verdict |")
        lines.append("|----------|--------|----------|-----|---------|")
        for strat, n, wins, pnl in avoided_strats:
            wr = (wins or 0) / n
            pnl_val = (pnl or 0) / 100
            verdict = "KEEP" if wr >= 0.50 and pnl_val >= 0 else "AVOID" if n >= 5 and wr < 0.45 else "WATCH"
            lines.append(f"| {strat[:30]} | {n} | {wr:.0%} | ${pnl_val:,.2f} | {verdict} |")

            if verdict == "AVOID" and n >= 5:
                _journal_entry(conn, "strategy_discarded", strat,
                    f"DISCARDED: {strat}",
                    f"After {n} trades, wr={wr:.0%}, pnl=${pnl_val:.2f} — avoiding",
                    wr, "win_rate")
    else:
        lines.append("Not enough settled data yet to evaluate strategies.")

    # ═══════════════════════════════════════════════════════════════════════
    # 5. PERFORMANCE BY MARKET CATEGORY
    # ═══════════════════════════════════════════════════════════════════════
    lines.append("")
    lines.append("## 5. Performance by Category")
    lines.append("")

    cat_stats = conn.execute("""
        SELECT s.ticker, s.won, s.profit_cents, s.contracts, s.price_cents
        FROM settlements s
    """).fetchall()
    if cat_stats:
        cat_perf = {}
        for ticker, won, profit, contracts, price in cat_stats:
            cat = categorize_market(ticker or "", "")
            if cat not in cat_perf:
                cat_perf[cat] = {"wins": 0, "losses": 0, "profit": 0, "count": 0}
            cat_perf[cat]["count"] += 1
            cat_perf[cat]["profit"] += (profit or 0)
            if won:
                cat_perf[cat]["wins"] += 1
            else:
                cat_perf[cat]["losses"] += 1

        lines.append("| Category | Trades | Win Rate | Total P&L | Status |")
        lines.append("|----------|--------|----------|-----------|--------|")
        for cat in sorted(cat_perf.keys(), key=lambda c: cat_perf[c]["profit"], reverse=True):
            cp = cat_perf[cat]
            n = cp["count"]
            wr = cp["wins"] / n if n > 0 else 0
            pnl = cp["profit"] / 100
            status = "STRONG" if wr > 0.55 and pnl > 0 else "WEAK" if wr < 0.40 else "NEUTRAL"
            lines.append(f"| {cat} | {n} | {wr:.0%} | ${pnl:,.2f} | {status} |")
    else:
        lines.append("No category data yet.")

    # ═══════════════════════════════════════════════════════════════════════
    # 6. CALIBRATION — are our probability estimates accurate?
    # ═══════════════════════════════════════════════════════════════════════
    lines.append("")
    lines.append("## 6. Calibration (Probability Accuracy)")
    lines.append("")
    lines.append("When we say a market has 70% chance of YES, does it actually resolve YES ~70% of the time?")
    lines.append("")

    cal_rows = conn.execute(
        "SELECT bucket, estimated_prob, actual_outcome FROM calibration WHERE bucket IS NOT NULL"
    ).fetchall()
    if cal_rows:
        cal_buckets = {}
        for bucket, est, actual in cal_rows:
            cal_buckets.setdefault(bucket, []).append((est, actual))

        lines.append("| Prob Range | n | Avg Estimate | Actual Win Rate | Bias | Assessment |")
        lines.append("|------------|---|-------------|-----------------|------|------------|")
        for bucket in sorted(cal_buckets.keys()):
            entries = cal_buckets[bucket]
            n = len(entries)
            if n < 2:
                continue
            avg_est = sum(e for e, _ in entries) / n
            actual_rate = sum(a for _, a in entries) / n
            bias = avg_est - actual_rate
            assessment = "GOOD" if abs(bias) < 0.10 else ("OVERCONFIDENT" if bias > 0 else "UNDERCONFIDENT")
            lines.append(f"| {bucket} | {n} | {avg_est:.2f} | {actual_rate:.2f} | {bias:+.2f} | {assessment} |")

            if abs(bias) >= 0.10 and n >= 5:
                _journal_entry(conn, "calibration_issue", bucket,
                    f"Calibration {assessment.lower()} in {bucket} range",
                    f"est={avg_est:.2f} actual={actual_rate:.2f} bias={bias:+.2f} n={n}",
                    bias, "calibration_bias")
    else:
        lines.append("No calibration data yet. Predictions will be evaluated as markets settle.")

    # ═══════════════════════════════════════════════════════════════════════
    # 7. RECENT ACTIVITY
    # ═══════════════════════════════════════════════════════════════════════
    lines.append("")
    lines.append("## 7. Recent Activity (Last 24h)")
    lines.append("")

    day_ago = (now - timedelta(hours=24)).isoformat()
    recent_trades = conn.execute(
        "SELECT timestamp, ticker, side, price_cents, contracts, edge, reason "
        "FROM trades WHERE timestamp > ? AND action='buy' ORDER BY id DESC LIMIT 15",
        (day_ago,)
    ).fetchall()
    if recent_trades:
        lines.append("| Time | Market | Side | Price | Qty | Edge | Source |")
        lines.append("|------|--------|------|-------|-----|------|--------|")
        for ts, tick, side, price, qty, edge, reason in recent_trades:
            t = ts[11:16] if ts and len(ts) > 16 else ts or ""
            edge_str = f"{edge:.1%}" if edge else "?"
            # Extract short source from reason
            src_short = ""
            if reason:
                for src_name in SOURCE_WEIGHTS.keys():
                    if src_name in reason.lower():
                        src_short = src_name
                        break
            lines.append(f"| {t} | {(tick or '')[:20]} | {side} | {price}¢ | {qty} | {edge_str} | {src_short} |")
    else:
        lines.append("No trades in the last 24 hours.")

    recent_exits = conn.execute(
        "SELECT timestamp, ticker, side, entry_price_cents, exit_price_cents, contracts, exit_reason "
        "FROM position_exits WHERE timestamp > ? ORDER BY id DESC LIMIT 10",
        (day_ago,)
    ).fetchall()
    if recent_exits:
        lines.append("")
        lines.append("**Recent exits:**")
        lines.append("")
        lines.append("| Time | Market | Entry | Exit | Qty | Reason |")
        lines.append("|------|--------|-------|------|-----|--------|")
        for ts, tick, side, entry, exit_p, qty, reason in recent_exits:
            t = ts[11:16] if ts and len(ts) > 16 else ts or ""
            reason_short = (reason or "")[:35]
            lines.append(f"| {t} | {(tick or '')[:20]} | {entry}¢ | {exit_p}¢ | {qty} | {reason_short} |")

    recent_settlements = conn.execute(
        "SELECT recorded_at, ticker, won, profit_cents, contracts "
        "FROM settlements WHERE recorded_at > ? ORDER BY id DESC LIMIT 10",
        (day_ago,)
    ).fetchall()
    if recent_settlements:
        lines.append("")
        lines.append("**Recent settlements:**")
        lines.append("")
        lines.append("| Time | Market | Result | Profit |")
        lines.append("|------|--------|--------|--------|")
        for ts, tick, won, profit, qty in recent_settlements:
            t = ts[11:16] if ts and len(ts) > 16 else ts or ""
            result_str = "WIN" if won else "LOSS"
            lines.append(f"| {t} | {(tick or '')[:25]} | {result_str} | ${(profit or 0)/100:+,.2f} |")

    # ═══════════════════════════════════════════════════════════════════════
    # 8. STRATEGY JOURNAL — what's been tested and learned
    # ═══════════════════════════════════════════════════════════════════════
    lines.append("")
    lines.append("## 8. Strategy Journal")
    lines.append("")
    lines.append("Significant learnings the bot has accumulated over time.")
    lines.append("")

    journal_entries = conn.execute(
        "SELECT timestamp, entry_type, category, title, detail "
        "FROM strategy_journal ORDER BY id DESC LIMIT 30"
    ).fetchall()
    if journal_entries:
        for ts, etype, cat, title, detail in journal_entries:
            date_str = ts[:10] if ts else ""
            icon = {"source_eval": "📊", "strategy_discarded": "🚫", "calibration_issue": "🎯",
                    "strategy_promoted": "✅", "observation": "📝"}.get(etype, "•")
            lines.append(f"- {icon} **[{date_str}] {title}** — {detail}")
    else:
        lines.append("No journal entries yet. The bot will log significant findings as trades settle.")

    # ═══════════════════════════════════════════════════════════════════════
    # 9. THIS RUN
    # ═══════════════════════════════════════════════════════════════════════
    lines.append("")
    lines.append("## 9. This Run")
    lines.append("")
    lines.append(f"- Markets scanned: {result.get('markets_scanned', 0)}")
    lines.append(f"- Opportunities found: {len(result.get('opportunities', []))}")
    lines.append(f"- Orders placed: {len(result.get('orders_placed', []))}")
    lines.append(f"- Positions managed: {result.get('positions_managed', 0)}")
    lines.append(f"- Stale orders pruned: {result.get('orders_pruned', 0)}")
    lines.append(f"- Settlements recorded: {result.get('settlements_recorded', 0)}")
    lines.append(f"- Halted: {'YES — ' + result.get('halt_reason', '') if result.get('halted') else 'No'}")
    lines.append(f"- Dry run: {'Yes' if result.get('dry_run') else 'No'}")
    lines.append("")

    # ═══════════════════════════════════════════════════════════════════════
    # 10. CUMULATIVE STATS
    # ═══════════════════════════════════════════════════════════════════════
    lines.append("## 10. Lifetime Stats")
    lines.append("")
    total_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    total_scanned = conn.execute("SELECT COALESCE(SUM(markets_scanned),0) FROM sessions").fetchone()[0]
    first_session = conn.execute("SELECT MIN(timestamp) FROM sessions").fetchone()[0]
    halted_count = conn.execute("SELECT COUNT(*) FROM sessions WHERE halted=1").fetchone()[0]
    lines.append(f"- **First run:** {(first_session or '')[:10]}")
    lines.append(f"- **Total runs:** {total_sessions}")
    lines.append(f"- **Total markets scanned:** {total_scanned:,}")
    lines.append(f"- **Times halted by circuit breaker:** {halted_count}")
    lines.append("")

    # Write to file
    report_text = "\n".join(lines) + "\n"
    try:
        # Write to both /task and the autoagent directory
        with open(REPORT_PATH, "w") as f:
            f.write(report_text)
        alt_path = os.path.join(os.path.dirname(DB_PATH), "PERFORMANCE_REPORT.md")
        if alt_path != REPORT_PATH:
            try:
                with open(alt_path, "w") as f:
                    f.write(report_text)
            except: pass
        print(f"[report] Performance report written to {REPORT_PATH}")
    except Exception as e:
        print(f"[report] Failed to write report: {e}")

    conn.commit()
    return report_text

# ══════════════════════════════════════════════════════════════════════════════
# MARKET MAKING — provide liquidity in thin markets for spread income (v3.11)
# ══════════════════════════════════════════════════════════════════════════════
# Instead of predicting outcomes (directional), market making earns the bid-ask
# spread by posting two-sided limit orders. Profitable when:
#   1. The spread is wide enough to cover fees + adverse selection
#   2. Our fair value estimate is roughly correct (don't need to be perfect)
#   3. We manage inventory so we don't accumulate large directional risk
#
# Targets: thin markets that professional MMs ignore (weather, niche sports,
# entertainment) where spreads are 8-20¢ and volume is too low for HFT.
# ══════════════════════════════════════════════════════════════════════════════

def _init_mm_tables(conn):
    """Create market-making tables if they don't exist."""
    conn.execute("""CREATE TABLE IF NOT EXISTS mm_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT,
        ticker TEXT, side TEXT, price_cents INTEGER, contracts INTEGER,
        order_id TEXT, status TEXT DEFAULT 'posted', fill_qty INTEGER DEFAULT 0,
        fair_value_cents INTEGER, inventory_at_post INTEGER,
        tag TEXT DEFAULT 'mm_v1')""")
    conn.execute("""CREATE TABLE IF NOT EXISTS mm_inventory (
        id INTEGER PRIMARY KEY AUTOINCREMENT, updated_at TEXT,
        ticker TEXT UNIQUE, net_position INTEGER DEFAULT 0,
        total_bought INTEGER DEFAULT 0, total_sold INTEGER DEFAULT 0,
        realized_pnl_cents INTEGER DEFAULT 0,
        avg_entry_cents REAL DEFAULT 0)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS mm_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT,
        markets_quoted INTEGER, orders_posted INTEGER, orders_cancelled INTEGER,
        fills_detected INTEGER, inventory_value_cents INTEGER,
        realized_pnl_cents INTEGER, unrealized_pnl_cents INTEGER)""")
    conn.commit()

def mm_get_inventory(conn, ticker):
    """Get current net position for a market. Positive = long YES, negative = short YES (long NO)."""
    row = conn.execute(
        "SELECT net_position, avg_entry_cents FROM mm_inventory WHERE ticker=?", (ticker,)
    ).fetchone()
    return (row[0], row[1]) if row else (0, 0.0)

def _apply_trade(net, avg_entry, side, qty, price_cents):
    """Pure math: compute new (net_position, avg_entry, realized_pnl) after a trade.

    Convention: avg_entry is always YES-equivalent cost basis.
      Long YES (net > 0):  avg_entry = what we paid per YES contract
      Short YES (net < 0): avg_entry = 100 - what_we_paid_per_NO (YES-equivalent)

    price_cents: the SIDE-APPROPRIATE price:
      YES fill → yes_price in cents
      NO fill  → no_price in cents (NOT yes_price!)

    Handles 4 cases per side:
      1. Adding to existing same-direction position  → weighted avg
      2. Reducing opposite position without flipping → keep old avg, realize P&L
      3. Flipping from opposite to same direction    → realize P&L, new avg for excess
      4. Opening fresh position                      → avg = trade price
    """
    realized_pnl = 0.0

    if side == "yes":
        new_net = net + qty
        if net >= 0:
            # Case 1/4: adding to long or opening fresh long
            total_cost = avg_entry * abs(net) + price_cents * qty
            new_avg = total_cost / max(1, abs(new_net))
        elif qty <= abs(net):
            # Case 2: reducing short without flipping — keep old avg for remainder
            closed = qty
            # Short had YES-equiv avg_entry, so NO cost was (100 - avg_entry).
            # Buying YES at price_cents to close. P&L = (100 - avg_entry) - price_cents per contract.
            realized_pnl = closed * ((100.0 - avg_entry) - price_cents)
            new_avg = avg_entry  # remaining short keeps its avg
        else:
            # Case 3: flipping from short to long
            closed = abs(net)
            realized_pnl = closed * ((100.0 - avg_entry) - price_cents)
            new_avg = price_cents  # new long position at trade price
    else:
        # NO side: convert to YES-equivalent for storage
        yes_equiv = 100.0 - price_cents
        new_net = net - qty
        if net <= 0:
            # Case 1/4: adding to short or opening fresh short
            total_cost = avg_entry * abs(net) + yes_equiv * qty
            new_avg = total_cost / max(1, abs(new_net)) if new_net != 0 else 0.0
        elif qty <= net:
            # Case 2: reducing long without flipping — keep old avg for remainder
            closed = qty
            exit_price = 100.0 - price_cents  # what we get for exiting YES via NO sale
            realized_pnl = closed * (exit_price - avg_entry)
            new_avg = avg_entry  # remaining long keeps its avg
        else:
            # Case 3: flipping from long to short
            closed = net
            exit_price = 100.0 - price_cents
            realized_pnl = closed * (exit_price - avg_entry)
            new_avg = yes_equiv  # new short at YES-equivalent

    return new_net, new_avg, realized_pnl


def mm_update_inventory(conn, ticker, side, qty, price_cents):
    """Update inventory after a fill is detected.
    price_cents must be the SIDE-APPROPRIATE price:
      YES fill → yes_price in cents
      NO fill  → no_price in cents
    NOTE: Does NOT commit — caller must commit the transaction."""
    now = datetime.now(timezone.utc).isoformat()
    net, avg_entry = mm_get_inventory(conn, ticker)

    new_net, new_avg, realized_pnl = _apply_trade(net, avg_entry, side, qty, price_cents)

    if int(realized_pnl) != 0:
        conn.execute("""UPDATE mm_inventory SET realized_pnl_cents = realized_pnl_cents + ?
                       WHERE ticker = ?""", (int(realized_pnl), ticker))

    if side == "yes":
        conn.execute("""INSERT INTO mm_inventory (updated_at, ticker, net_position, total_bought, avg_entry_cents)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(ticker) DO UPDATE SET
                       updated_at=?, net_position=?, total_bought=total_bought+?, avg_entry_cents=?""",
                     (now, ticker, new_net, qty, new_avg,
                      now, new_net, qty, new_avg))
    else:
        conn.execute("""INSERT INTO mm_inventory (updated_at, ticker, net_position, total_sold, avg_entry_cents)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(ticker) DO UPDATE SET
                       updated_at=?, net_position=?, total_sold=total_sold+?, avg_entry_cents=?""",
                     (now, ticker, new_net, qty, new_avg,
                      now, new_net, qty, new_avg))
    # NOTE: no conn.commit() here — caller handles transaction boundaries

def mm_calculate_quotes(fair_value_cents, inventory, half_spread):
    """Calculate bid/ask prices, skewed by inventory to encourage rebalancing.
    If we're long (inventory > 0), lower BID (reluctant to buy more) and lower ASK (eager to sell).
    If we're short (inventory < 0), raise ASK (reluctant to sell more) and raise BID (eager to buy).
    The skew moves the MIDPOINT, so both sides shift together — the direction incentivizes
    the counterparty to take the other side of our position."""
    # Continuous skew instead of integer truncation — even 1 contract matters
    skew = int(round(inventory * MM_SKEW_PER_10 / 10.0))
    bid = fair_value_cents - half_spread - skew
    ask = fair_value_cents + half_spread - skew
    # Clamp to valid Kalshi range [1, 99]
    bid = max(1, min(98, bid))
    ask = max(bid + 1, min(99, ask))  # ask must be > bid
    return bid, ask

def mm_cancel_all_orders(conn):
    """Cancel all resting MM orders. We re-post fresh ones each cycle.
    Returns count of orders cancelled."""
    cancelled = 0
    try:
        orders = api_get("/portfolio/orders?status=resting&limit=200").get("orders", [])
        # Identify MM orders by checking our DB
        mm_order_ids = set()
        rows = conn.execute(
            "SELECT order_id FROM mm_orders WHERE status='posted'"
        ).fetchall()
        for row in rows:
            mm_order_ids.add(row[0])

        for o in orders:
            oid = o.get("order_id", "")
            if oid in mm_order_ids:
                try:
                    api_delete(f"/portfolio/orders/{oid}")
                    conn.execute("UPDATE mm_orders SET status='cancelled' WHERE order_id=?", (oid,))
                    cancelled += 1
                except Exception as e:
                    print(f"[mm] Failed to cancel {oid}: {e}")
                    # Mark as stale so it doesn't accumulate as zombie 'posted' order
                    conn.execute("UPDATE mm_orders SET status='cancel_failed' WHERE order_id=?", (oid,))

        # Also mark any old 'posted' orders not found in API as expired/stale
        for oid in mm_order_ids:
            api_oids = {o.get("order_id", "") for o in orders}
            if oid not in api_oids:
                conn.execute("UPDATE mm_orders SET status='expired' WHERE order_id=? AND status='posted'", (oid,))
        conn.commit()
    except Exception as e:
        print(f"[mm] Error cancelling orders: {e}")
    return cancelled

def _parse_fill_price_cents(fill, side):
    """Extract the correct side-appropriate price from a fill response.
    For YES fills → yes_price. For NO fills → no_price.
    Returns price in cents (integer)."""
    if side == "no":
        # Use no_price_dollars first, then derive from yes_price if missing
        no_raw = fill.get("no_price_dollars") or fill.get("no_price")
        if no_raw:
            v = float(no_raw)
            return int(round(v * 100)) if 0 < v <= 1.0 else int(v)
        # Fallback: derive from yes_price (no_price = 100 - yes_price)
        yes_raw = fill.get("yes_price_dollars") or fill.get("yes_price")
        if yes_raw:
            v = float(yes_raw)
            yes_cents = int(round(v * 100)) if 0 < v <= 1.0 else int(v)
            return 100 - yes_cents
        return 0
    else:
        yes_raw = fill.get("yes_price_dollars") or fill.get("yes_price")
        if yes_raw:
            v = float(yes_raw)
            return int(round(v * 100)) if 0 < v <= 1.0 else int(v)
        return 0


def mm_check_fills(conn):
    """Check for new fills using Kalshi's /portfolio/fills endpoint (ground truth).
    Falls back to order-status inference if fills endpoint unavailable.
    Returns number of new fills detected.

    ATOMICITY: inventory update, fill-id insert, and order-state update are
    wrapped in a single transaction per fill batch. mm_update_inventory() does
    NOT commit internally."""
    fills = 0

    # Ensure tables exist
    conn.execute("""CREATE TABLE IF NOT EXISTS mm_processed_fills (
        fill_id TEXT PRIMARY KEY, processed_at TEXT, fee_cents REAL DEFAULT 0,
        order_id TEXT DEFAULT '', ticker TEXT DEFAULT '')""")
    # Add columns if upgrading from old schema
    for col, coldef in [("fee_cents", "REAL DEFAULT 0"), ("order_id", "TEXT DEFAULT ''"), ("ticker", "TEXT DEFAULT ''")]:
        try:
            conn.execute(f"SELECT {col} FROM mm_processed_fills LIMIT 1")
        except Exception:
            try:
                conn.execute(f"ALTER TABLE mm_processed_fills ADD COLUMN {col} {coldef}")
            except Exception:
                pass

    try:
        # --- Paginated fill retrieval with high-water mark ---
        # Get last-seen fill timestamp for incremental sync
        last_ts_row = conn.execute(
            "SELECT MAX(processed_at) FROM mm_processed_fills").fetchone()
        min_ts_param = ""
        if last_ts_row and last_ts_row[0]:
            # Subtract 5 minutes for safety overlap (dedupe handles repeats)
            try:
                last_dt = datetime.fromisoformat(last_ts_row[0].replace("Z", "+00:00"))
                safe_dt = last_dt - timedelta(minutes=5)
                min_ts_param = f"&min_ts={int(safe_dt.timestamp())}"
            except Exception:
                pass

        # Get ALL known MM order IDs (not just 'posted' — catches late fills on canceled orders)
        mm_oids = set(r[0] for r in conn.execute(
            "SELECT order_id FROM mm_orders").fetchall())
        processed = set(r[0] for r in conn.execute(
            "SELECT fill_id FROM mm_processed_fills").fetchall())

        # Paginate through fills using cursor
        cursor = ""
        total_api_fills = 0
        while True:
            url = f"/portfolio/fills?limit=1000{min_ts_param}"
            if cursor:
                url += f"&cursor={cursor}"
            resp = api_get(url)
            api_fills = resp.get("fills", [])
            total_api_fills += len(api_fills)

            for f in api_fills:
                fill_id = f.get("fill_id") or f.get("trade_id") or ""
                if not fill_id or fill_id in processed:
                    continue
                order_id = f.get("order_id", "")
                if order_id not in mm_oids:
                    continue

                # Parse fill details
                ticker = f.get("ticker") or f.get("market_ticker", "")
                side = f.get("side", "")
                count_raw = f.get("count_fp") or f.get("count", 0)
                fill_qty = int(float(count_raw)) if count_raw else 0
                # CRITICAL: use side-appropriate price (audit fix #1)
                price_cents = _parse_fill_price_cents(f, side)
                # Actual fee from Kalshi (in dollars → cents)
                fee_raw = f.get("fee_cost") or f.get("fee_cost_dollars") or 0
                fee_cents = float(fee_raw) * 100 if fee_raw and float(fee_raw) <= 10.0 else float(fee_raw or 0)

                if fill_qty <= 0:
                    continue

                # ATOMIC: inventory + fill record + order state in one transaction
                mm_update_inventory(conn, ticker, side, fill_qty, price_cents)
                conn.execute("INSERT OR IGNORE INTO mm_processed_fills (fill_id, processed_at, fee_cents, order_id, ticker) VALUES (?, ?, ?, ?, ?)",
                            (fill_id, datetime.now(timezone.utc).isoformat(), fee_cents, order_id, ticker))
                conn.execute(
                    "UPDATE mm_orders SET fill_qty = fill_qty + ?, "
                    "status = CASE WHEN fill_qty + ? >= contracts THEN 'filled' ELSE status END "
                    "WHERE order_id = ?",
                    (fill_qty, fill_qty, order_id))
                fills += 1
                processed.add(fill_id)  # avoid re-processing within same batch
                is_taker = f.get("is_taker", False)
                print(f"[mm] Fill: {ticker} {side} x{fill_qty} @ {price_cents}¢ "
                      f"({'taker' if is_taker else 'maker'}) fee={fee_cents:.1f}¢ [fill_id={fill_id[:12]}]")

            # Commit after each page (atomic per page)
            conn.commit()

            # Check for next page
            next_cursor = resp.get("cursor", "")
            if not next_cursor or not api_fills:
                break
            cursor = next_cursor

        if fills > 0 or total_api_fills > 0:
            return fills  # fills endpoint worked — trust it

    except Exception as e:
        print(f"[mm] Fills endpoint error ({e}), falling back to order-status inference")

    # Fallback: infer fills from order status (less reliable but works pre-migration)
    try:
        orders = api_get("/portfolio/orders?status=executed&limit=200").get("orders", [])
        executed_ids = {o.get("order_id"): o for o in orders if o.get("order_id")}

        rows = conn.execute(
            "SELECT id, order_id, ticker, side, price_cents, contracts, fill_qty FROM mm_orders WHERE status='posted'"
        ).fetchall()

        for row_id, oid, ticker, side, price_cents, contracts, prev_fill in rows:
            if oid in executed_ids:
                exec_order = executed_ids[oid]
                fill_count_raw = exec_order.get("fill_count_fp") or exec_order.get("count_fp") or exec_order.get("count", contracts)
                total_filled = int(float(fill_count_raw))
                new_fills = total_filled - (prev_fill or 0)
                if new_fills > 0:
                    mm_update_inventory(conn, ticker, side, new_fills, price_cents)
                    conn.execute(
                        "UPDATE mm_orders SET status='filled', fill_qty=? WHERE id=?",
                        (total_filled, row_id))
                    fills += 1
                    print(f"[mm] Fill (fallback): {ticker} {side} x{new_fills} @ {price_cents}¢")

        # Check partially filled resting orders
        resting = api_get("/portfolio/orders?status=resting&limit=200").get("orders", [])
        resting_ids = {o.get("order_id"): o for o in resting if o.get("order_id")}
        rows2 = conn.execute(
            "SELECT id, order_id, ticker, side, price_cents, contracts, fill_qty FROM mm_orders WHERE status='posted'"
        ).fetchall()
        for row_id, oid, ticker, side, price_cents, contracts, prev_fill in rows2:
            if oid in resting_ids:
                o = resting_ids[oid]
                remaining_raw = o.get("remaining_count_fp") or o.get("remaining_count", contracts)
                remaining = int(float(remaining_raw))
                new_fills = contracts - remaining - (prev_fill or 0)
                if new_fills > 0:
                    mm_update_inventory(conn, ticker, side, new_fills, price_cents)
                    conn.execute(
                        "UPDATE mm_orders SET fill_qty=? WHERE id=?",
                        ((prev_fill or 0) + new_fills, row_id))
                    fills += 1
                    print(f"[mm] Partial fill (fallback): {ticker} {side} x{new_fills} @ {price_cents}¢")

        conn.commit()
    except Exception as e:
        print(f"[mm] Error in fallback fill check: {e}")
    return fills

def mm_select_markets(markets, conn, balance_cents, category_edges=None):
    """Select markets suitable for market making.
    Criteria: wide spread, adequate volume, low adverse-selection category,
    not already at inventory limit. Uses category_edges to skip categories
    that need unsustainably wide spreads."""
    # Portfolio-level series concentration: count total inventory per series prefix.
    # Prevents accumulating 30+ positions in one correlated cluster (e.g., all KXFED).
    MM_MAX_SERIES_INVENTORY = 50  # max total |net_position| across all tickers in one series
    series_inventory = {}
    try:
        inv_rows = conn.execute(
            "SELECT ticker, net_position FROM mm_inventory WHERE net_position != 0"
        ).fetchall()
        for t, net in inv_rows:
            series_pfx = t.split("-")[0] if "-" in t else t
            series_inventory[series_pfx] = series_inventory.get(series_pfx, 0) + abs(int(net))
    except Exception:
        pass

    candidates = []
    filter_stats = {"total": 0, "cat_skip": 0, "no_ask": 0, "no_bid_narrow": 0,
                    "tight_spread": 0, "low_vol": 0, "extreme_price": 0,
                    "inv_full": 0, "expiring": 0, "series_full": 0, "passed": 0}
    for m in markets:
        filter_stats["total"] += 1
        ticker = m.get("ticker", "")
        title = (m.get("title", "") or m.get("subtitle", "") or "").lower()

        # Skip multi-leg parlay/combo markets (MVE) — synthetic, not real tradeable markets
        if "KXMVE" in ticker or "MULTIGAME" in ticker or m.get("mve_collection_ticker"):
            filter_stats["cat_skip"] += 1
            continue

        # Category filter: prefer low-adverse-selection markets
        cat = categorize_market(ticker, title)
        if cat not in MM_PREFERRED_CATS:
            filter_stats["cat_skip"] += 1
            continue

        # Skip categories where learned edge multiplier is too high (unprofitable to MM)
        if category_edges:
            cat_mult = category_edges.get(cat, 1.0)
            if cat_mult > 2.5:
                filter_stats["cat_skip"] += 1
                continue  # category needs >2.5x edge — not worth quoting

        # Parse prices
        def _pc(v):
            """Convert price to cents. Handles both cent ints (65) and dollar strings ('0.65')."""
            v = float(v or 0)
            return int(round(v * 100)) if v <= 1.0 else int(v)
        yes_ask = _pc(m.get("yes_ask") or m.get("yes_ask_dollars"))
        yes_bid = _pc(m.get("yes_bid") or m.get("yes_bid_dollars"))

        # Use last_price as fair value hint when orderbook is thin
        last_price = _pc(m.get("last_price") or m.get("last_price_dollars"))

        # Calculate spread from available prices
        # Empty orderbook handling — NEVER quote off stale last_price alone.
        # Markets with no live book are too dangerous: we have no idea where
        # real liquidity sits, and quoting off a stale print invites adverse selection.
        if yes_ask <= 0 and yes_bid <= 0:
            # No live book at all — skip entirely. Do NOT use last_price as anchor.
            filter_stats["no_ask"] += 1
            continue
        elif yes_ask <= 0:
            spread = 99 - yes_bid  # no sellers = wide spread
            mid = yes_bid + 10  # anchor to live bid, NOT stale last_price
        elif yes_bid <= 0:
            spread = yes_ask  # no buyers = wide spread
            mid = max(yes_ask - 5, 1)  # anchor to live ask, NOT stale last_price
        else:
            spread = yes_ask - yes_bid
            mid = (yes_ask + yes_bid) // 2

        if spread < MM_MIN_SPREAD:
            filter_stats["tight_spread"] += 1
            continue  # spread too tight — a better MM is already here

        volume = float(m.get("volume") or m.get("volume_24h_fp") or m.get("volume_fp") or 0)
        open_interest = float(m.get("open_interest") or m.get("open_interest_fp") or 0)
        activity = max(volume, open_interest)  # OI shows someone holds positions even if no recent trades
        if activity < MM_MIN_VOLUME:
            filter_stats["low_vol"] += 1
            continue

        # Skip markets too close to 0 or 100 (high adverse selection near resolution)
        if mid < 10 or mid > 90:
            filter_stats["extreme_price"] += 1
            continue

        # Check current inventory — skip if already at limit
        inv, _ = mm_get_inventory(conn, ticker)
        if abs(inv) >= MM_MAX_INVENTORY:
            filter_stats["inv_full"] += 1
            continue

        # Portfolio-level series concentration check
        series_pfx = ticker.split("-")[0] if "-" in ticker else ticker
        if series_inventory.get(series_pfx, 0) >= MM_MAX_SERIES_INVENTORY:
            filter_stats["series_full"] += 1
            continue

        # Check time to expiration — prefer markets with > 6h left
        close_time = m.get("close_time") or m.get("expiration_time")
        hours_left = 999
        if close_time:
            try:
                ct = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                hours_left = (ct - datetime.now(timezone.utc)).total_seconds() / 3600
            except Exception:
                pass
        if hours_left < 2:
            filter_stats["expiring"] += 1
            continue  # too close to resolution — high adverse selection

        filter_stats["passed"] += 1
        # Score: balance spread profit vs fill probability (volume)
        # Cap spread contribution at 30¢ — beyond that, extra spread doesn't help much
        # because the market is just illiquid, not more profitable per fill
        spread_score = min(spread, 30)
        vol_score = math.log1p(volume + open_interest * 0.3)  # OI counts but less than volume
        time_mult = 1.0 if hours_left > 12 else 0.7
        score = spread_score * vol_score * time_mult
        candidates.append((score, m, ticker, spread, mid, inv, cat))

    # Log filter funnel
    print(f"[mm] Filter funnel: {filter_stats['total']} total → "
          f"{filter_stats['cat_skip']} wrong category, "
          f"{filter_stats['no_ask']} no ask, "
          f"{filter_stats['tight_spread']} tight spread, "
          f"{filter_stats['low_vol']} low volume, "
          f"{filter_stats['extreme_price']} extreme price, "
          f"{filter_stats['expiring']} expiring, "
          f"{filter_stats['inv_full']} inv full, "
          f"{filter_stats.get('series_full', 0)} series full → "
          f"{filter_stats['passed']} passed")

    # Sort by score descending, then diversify: max 3 markets per series
    # This prevents concentration (e.g., all 16 KXFED strikes filling to max)
    MAX_PER_SERIES = 3
    candidates.sort(key=lambda x: x[0], reverse=True)
    selected = []
    series_count = {}
    for c in candidates:
        ticker = c[2]  # ticker is 3rd element
        # Extract series prefix (e.g., "KXFED" from "KXFED-27APR-T2.50")
        series = ticker.split("-")[0] if "-" in ticker else ticker
        series_count[series] = series_count.get(series, 0) + 1
        if series_count[series] > MAX_PER_SERIES:
            continue  # skip — already have enough from this series
        selected.append(c)
        if len(selected) >= MM_MAX_MARKETS:
            break
    return selected

# ── Economic Calendar: scheduled release awareness ──────────────────────────
# When a major data release is imminent, widen spreads or pause quoting to
# avoid adverse selection from informed traders who reprice instantly.
_ECON_CALENDAR_2026 = {
    # FOMC decisions (Wed 2pm ET) — pause quoting ±15 min, widen 2x for ±2h
    "fomc": [
        "2026-01-28", "2026-03-18", "2026-05-06", "2026-06-17",
        "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-16",
    ],
    # CPI releases (typically 8:30am ET, 2nd week of month)
    "cpi": [
        "2026-01-14", "2026-02-12", "2026-03-11", "2026-04-14",
        "2026-05-13", "2026-06-10", "2026-07-14", "2026-08-12",
        "2026-09-10", "2026-10-13", "2026-11-12", "2026-12-10",
    ],
    # Jobs report (typically 8:30am ET, 1st Friday)
    "jobs": [
        "2026-01-09", "2026-02-06", "2026-03-06", "2026-04-03",
        "2026-05-08", "2026-06-05", "2026-07-02", "2026-08-07",
        "2026-09-04", "2026-10-02", "2026-11-06", "2026-12-04",
    ],
}

def _econ_release_proximity():
    """Check if a major economic release is happening soon.
    Returns (event_type, hours_until_release) or (None, None).
    Release times: FOMC=18:00 UTC (2pm ET), CPI/Jobs=12:30 UTC (8:30am ET)."""
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")

    for event_type, dates in _ECON_CALENDAR_2026.items():
        for date_str in dates:
            if abs((datetime.fromisoformat(date_str + "T00:00:00+00:00") - now).days) > 1:
                continue  # not today or tomorrow
            # Set release time
            release_hour = 18 if event_type == "fomc" else 12  # UTC
            release_min = 0 if event_type == "fomc" else 30
            release_dt = datetime.fromisoformat(
                f"{date_str}T{release_hour:02d}:{release_min:02d}:00+00:00")
            hours_until = (release_dt - now).total_seconds() / 3600
            if -0.5 <= hours_until <= 3.0:  # within 3h before to 30min after
                return event_type, hours_until
    return None, None

# ── Category-specific spread overrides ──────────────────────────────────────
# Markets with high informed-trader participation need wider spreads.
_CATEGORY_SPREAD_OVERRIDES = {
    "economics": 7,   # KXFED, KXCPI, KXJOB — high informed flow → 7¢ half-spread
    "crypto":    6,    # KXBTC, KXETH — fast-moving, high adverse selection
    "weather":   4,    # Bot has NOAA edge — can be tighter
    "company":   5,    # Company KPIs — moderate, keep at default
    "sports":    5,    # Sports — moderate
}

def mm_get_effective_spread(conn, ticker, category):
    """Get effective half-spread considering: base → category override →
    adaptive (adverse selection learning) → economic calendar multiplier."""
    # Start with category override or default
    base_hs = _CATEGORY_SPREAD_OVERRIDES.get(category, MM_HALF_SPREAD)

    # Apply adaptive spread (learned adverse selection)
    adaptive_hs = mm_adaptive_spread(conn, ticker, base_hs)

    # Widen for economic releases
    event_type, hours_until = _econ_release_proximity()
    if event_type is not None:
        if hours_until is not None and -0.25 <= hours_until <= 0.25:
            # Within ±15 min of release — return -1 to signal "pause quoting"
            print(f"    [econ] {event_type} release in {hours_until*60:.0f} min — PAUSING quotes")
            return -1  # sentinel: skip this market
        elif hours_until is not None and hours_until <= 2.0:
            # Within 2 hours — double the spread
            adaptive_hs = min(15, adaptive_hs * 2)
            print(f"    [econ] {event_type} in {hours_until:.1f}h — spread doubled to {adaptive_hs}¢")

    return adaptive_hs


def _get_series_prefix(ticker):
    """Extract the series prefix from a bracket/threshold ticker.
    KXHIGHDEN-26APR09-B69.5 → KXHIGHDEN-26APR09
    KXFED-27APR-T2.50 → KXFED-27APR
    Returns (prefix, is_bracket). Bracket markets (-B) have mutually exclusive outcomes."""
    parts = ticker.rsplit("-", 1)
    if len(parts) == 2 and parts[1] and parts[1][0] in ("B", "T"):
        is_bracket = parts[1][0] == "B"
        return parts[0], is_bracket
    # Not a bracket/threshold ticker
    return ticker, False


def mm_check_series_profitability(conn, ticker, proposed_side, proposed_qty, proposed_price):
    """Check if adding a proposed position to a bracket series would make the
    portfolio structurally unprofitable.

    For bracket markets (only ONE bracket can be true):
    - Simulates every possible outcome (each bracket winning, plus none winning)
    - For each outcome, computes total portfolio P&L across ALL brackets in the series
    - Rejects if: no profitable outcome, or worst-case loss exceeds budget, or EV < 0

    Uses mm_inventory as primary source (tracks actual fills).
    Merges directional trades from trades table (fail-closed on error).
    Uses _apply_trade() for proposed position math (shared with mm_update_inventory).

    Returns (ok, reason) — ok=True if the new position is safe to add."""
    series_prefix, is_bracket = _get_series_prefix(ticker)

    if not is_bracket:
        return True, "not a bracket market"  # threshold markets (-T) aren't mutually exclusive

    # --- Get ALL positions in this series from mm_inventory (ground truth for MM) ---
    positions = {}
    rows = conn.execute(
        "SELECT ticker, net_position, avg_entry_cents FROM mm_inventory WHERE ticker LIKE ? AND abs(net_position) > 0",
        (series_prefix + "%",)
    ).fetchall()
    for t, net, avg_e in rows:
        _, pos_is_bracket = _get_series_prefix(t)
        if not pos_is_bracket:
            continue
        positions[t] = (net, float(avg_e))

    # Also merge directional trades — FAIL CLOSED on error (audit fix: no silent pass)
    try:
        dir_rows = conn.execute(
            """SELECT ticker, side, SUM(contracts), AVG(price_cents) FROM trades
               WHERE ticker LIKE ? AND action='buy' AND fill_status IN ('executed','filled')
               GROUP BY ticker, side""",
            (series_prefix + "%",)
        ).fetchall()
        for t, side, qty, avg_p in dir_rows:
            _, dir_is_bracket = _get_series_prefix(t)
            if not dir_is_bracket:
                continue
            dir_net = int(qty) if side == "yes" else -int(qty)
            dir_avg = float(avg_p)
            if t in positions:
                # Combine with existing MM position using _apply_trade()
                existing_net, existing_avg = positions[t]
                combined_net, combined_avg, _ = _apply_trade(
                    existing_net, existing_avg, side, int(qty), dir_avg)
                positions[t] = (combined_net, combined_avg)
            else:
                positions[t] = (dir_net, dir_avg)
    except Exception as e:
        # FAIL CLOSED: if we can't see directional trades, block the order
        print(f"    [series-check] BLOCKED: cannot read directional trades: {e}")
        return False, f"cannot read directional trades for risk check: {e}"

    # Apply proposed trade using shared _apply_trade() math
    existing_net, existing_avg = positions.get(ticker, (0, 0.0))
    new_net, new_avg, _ = _apply_trade(existing_net, existing_avg, proposed_side, proposed_qty, proposed_price)
    positions[ticker] = (new_net, new_avg)

    # Get all bracket tickers in this series (even those without inventory)
    all_tickers = set(positions.keys())
    all_tickers.add(ticker)

    # Simulate each possible outcome: one bracket wins, all others lose
    # CONVENTION: avg_e is always YES-side cost basis
    #   - For long YES (net > 0): avg_e = what we paid for YES
    #   - For short YES / long NO (net < 0): avg_e = 100 - what_we_paid_for_NO (YES-equivalent)
    scenario_pnls = []

    for winning_ticker in all_tickers:
        total_pnl = 0.0
        for t, (net, avg_e) in positions.items():
            if net == 0:
                continue
            if net > 0:
                if t == winning_ticker:
                    pnl = net * (100.0 - avg_e)
                else:
                    pnl = net * (0.0 - avg_e)
            else:
                no_cost = 100.0 - avg_e
                if t == winning_ticker:
                    pnl = -abs(net) * no_cost
                else:
                    pnl = abs(net) * (100.0 - no_cost)
            total_pnl += pnl
        scenario_pnls.append(total_pnl)

    # Also check: outcome where NONE of our held brackets win (uncovered bracket)
    uncovered_pnl = 0.0
    for t, (net, avg_e) in positions.items():
        if net == 0:
            continue
        if net > 0:
            uncovered_pnl += net * (0.0 - avg_e)
        else:
            no_cost = 100.0 - avg_e
            uncovered_pnl += abs(net) * (100.0 - no_cost)
    scenario_pnls.append(uncovered_pnl)

    worst_pnl = min(scenario_pnls) if scenario_pnls else 0.0
    best_pnl = max(scenario_pnls) if scenario_pnls else 0.0
    n_scenarios = len(scenario_pnls)
    # Simple EV estimate: average across all scenarios (uniform prior)
    avg_pnl = sum(scenario_pnls) / max(1, n_scenarios)

    # --- Fee accounting: use actual fees paid + estimate for proposed order ---
    # Actual fees from fill records for tickers in this series
    actual_fees_cents = 0.0
    try:
        fee_row = conn.execute(
            "SELECT COALESCE(SUM(fee_cents), 0) FROM mm_processed_fills WHERE ticker LIKE ?",
            (series_prefix + "%",)
        ).fetchone()
        if fee_row and fee_row[0]:
            actual_fees_cents = float(fee_row[0])
    except Exception:
        pass  # fee_cents/ticker columns may not exist yet

    # Estimate fee for proposed order using Kalshi formula: roundup(0.07 * C * P * (1-P))
    # For maker: roundup(0.0175 * C * P * (1-P))
    # We use maker rate since we use post_only
    p_dollar = proposed_price / 100.0
    proposed_fee_cents = max(1, int(0.0175 * proposed_qty * p_dollar * (1 - p_dollar) * 100 + 0.99))
    # Also estimate exit fee (assume we'll close at similar price)
    exit_fee_cents = proposed_fee_cents
    total_fees = actual_fees_cents + proposed_fee_cents + exit_fee_cents

    best_pnl_net = best_pnl - total_fees
    worst_pnl_net = worst_pnl - total_fees
    avg_pnl_net = avg_pnl - total_fees

    # --- Acceptance criteria (tightened from V2) ---
    # 1. Must have at least one profitable outcome
    has_profitable_outcome = any(p > total_fees for p in scenario_pnls)
    # 2. Best case must be positive after fees
    # 3. Average (EV) must be positive after fees — not just "one good outcome"
    # 4. Worst case must not exceed per-event loss budget (50 contracts * 100¢ = $50)
    EVENT_LOSS_BUDGET_CENTS = MM_MAX_INVENTORY * 100  # worst-case budget per event

    if not has_profitable_outcome:
        return False, (f"no profitable outcome (net of fees): best={best_pnl_net/100:.2f} "
                       f"worst={worst_pnl_net/100:.2f} fees~{total_fees/100:.2f} "
                       f"across {len(all_tickers)} brackets in {series_prefix}")

    if best_pnl_net <= 0:
        return False, (f"best case negative after fees: best={best_pnl_net/100:.2f} "
                       f"fees~{total_fees/100:.2f}")

    if avg_pnl_net <= 0:
        return False, (f"negative EV after fees: EV={avg_pnl_net/100:.2f} "
                       f"best={best_pnl_net/100:+.2f} worst={worst_pnl_net/100:+.2f} "
                       f"fees~{total_fees/100:.2f}")

    if worst_pnl_net < -EVENT_LOSS_BUDGET_CENTS:
        return False, (f"worst case exceeds loss budget: worst={worst_pnl_net/100:.2f} "
                       f"budget=-{EVENT_LOSS_BUDGET_CENTS/100:.2f}")

    return True, (f"ok: EV={avg_pnl_net/100:+.2f} best={best_pnl_net/100:+.2f} "
                  f"worst={worst_pnl_net/100:+.2f} (fees~{total_fees/100:.2f})")


def mm_post_quotes(conn, m, fair_value_cents, balance_cents, inventory):
    """Post two-sided limit orders for a market.
    Returns (orders_posted, capital_used_cents)."""
    ticker = m.get("ticker", "")
    now = datetime.now(timezone.utc).isoformat()
    orders_posted = 0
    capital_used = 0

    # Calculate skewed bid/ask with category-aware + event-aware spread
    cat = categorize_market(ticker, (m.get("title") or m.get("subtitle") or "").lower())
    adaptive_hs = mm_get_effective_spread(conn, ticker, cat)
    if adaptive_hs < 0:
        # Economic release imminent — skip quoting this cycle
        print(f"    SKIP {ticker}: economic release imminent, pausing quotes")
        return 0, 0
    bid, ask = mm_calculate_quotes(fair_value_cents, inventory, adaptive_hs)

    # Adjust order size based on inventory: if we're heavy one side, reduce that side
    buy_size = MM_ORDER_SIZE
    sell_size = MM_ORDER_SIZE
    if inventory > MM_MAX_INVENTORY * 0.5:
        buy_size = max(2, MM_ORDER_SIZE // 2)   # reduce buying
        sell_size = min(MM_ORDER_SIZE * 2, MM_MAX_INVENTORY - inventory)  # increase selling
    elif inventory < -MM_MAX_INVENTORY * 0.5:
        sell_size = max(2, MM_ORDER_SIZE // 2)
        buy_size = min(MM_ORDER_SIZE * 2, MM_MAX_INVENTORY + inventory)

    buy_size = max(0, buy_size)
    sell_size = max(0, sell_size)

    # Hard-cap: never exceed MM_MAX_INVENTORY even if current fill + order fills
    headroom_long = max(0, MM_MAX_INVENTORY - inventory)   # how many more YES we can hold
    headroom_short = max(0, MM_MAX_INVENTORY + inventory)  # how many more NO we can hold
    buy_size = min(buy_size, headroom_long)
    sell_size = min(sell_size, headroom_short)

    # Capital check
    buy_cost = buy_size * bid
    sell_cost = sell_size * (100 - ask)  # buying NO at (100-ask) cents
    total_cost = buy_cost + sell_cost
    if total_cost > balance_cents * MM_CAPITAL_PCT / max(1, MM_MAX_MARKETS):
        # Scale down proportionally — let size go to 0 if budget is exhausted
        scale = (balance_cents * MM_CAPITAL_PCT / max(1, MM_MAX_MARKETS)) / max(1, total_cost)
        buy_size = int(buy_size * scale)   # no max(1,...) — 0 is correct when budget is 0
        sell_size = int(sell_size * scale)

    # Reapply hard cap after capital scaling (audit fix: scaling could re-inflate)
    buy_size = min(buy_size, max(0, MM_MAX_INVENTORY - inventory))
    sell_size = min(sell_size, max(0, MM_MAX_INVENTORY + inventory))

    # Post BID (buy YES at bid price)
    if buy_size > 0 and abs(inventory) < MM_MAX_INVENTORY:
        # Series profitability gate: reject if adding this position makes ALL outcomes unprofitable
        sp_ok, sp_reason = mm_check_series_profitability(conn, ticker, "yes", buy_size, bid)
        if not sp_ok:
            print(f"    BID {ticker} BLOCKED: {sp_reason}")
            buy_size = 0
    if buy_size > 0 and abs(inventory) < MM_MAX_INVENTORY:
        order_body = {
            "ticker": ticker, "side": "yes", "type": "limit",
            "count": buy_size, "yes_price": bid,
            "action": "buy",
            "expiration_ts": int(time.time() + 110),
            "client_order_id": f"mm_bid_{ticker.replace('.', '_')}_{int(time.time())}_{uuid.uuid4().hex[:8]}",
            "post_only": True,
        }
        try:
            if not MM_DRY_RUN:
                resp = api_post("/portfolio/orders", order_body)
                oid = resp.get("order", {}).get("order_id", "")
            else:
                oid = f"dry_mm_bid_{ticker}_{int(time.time())}"
            conn.execute("""INSERT INTO mm_orders
                (timestamp, ticker, side, price_cents, contracts, order_id,
                 fair_value_cents, inventory_at_post, tag)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (now, ticker, "yes", bid, buy_size, oid,
                 fair_value_cents, inventory, MM_ORDER_TAG))
            orders_posted += 1
            capital_used += buy_size * bid
            print(f"    BID {ticker} YES x{buy_size} @ {bid}¢")
        except Exception as e:
            print(f"    BID {ticker} FAILED: {e}")

    # Post ASK (buy NO at (100-ask) = effectively selling YES at ask price)
    if sell_size > 0 and abs(inventory) < MM_MAX_INVENTORY:
        no_price = 100 - ask
        # Series profitability gate: reject if adding this NO position makes ALL outcomes unprofitable
        sp_ok, sp_reason = mm_check_series_profitability(conn, ticker, "no", sell_size, no_price)
        if not sp_ok:
            print(f"    ASK {ticker} BLOCKED: {sp_reason}")
            sell_size = 0
    if sell_size > 0 and abs(inventory) < MM_MAX_INVENTORY:
        no_price = 100 - ask
        order_body = {
            "ticker": ticker, "side": "no", "type": "limit",
            "count": sell_size, "no_price": no_price,
            "action": "buy",
            "expiration_ts": int(time.time() + 110),
            "client_order_id": f"mm_ask_{ticker.replace('.', '_')}_{int(time.time())}_{uuid.uuid4().hex[:8]}",
            "post_only": True,
        }
        try:
            if not MM_DRY_RUN:
                resp = api_post("/portfolio/orders", order_body)
                oid = resp.get("order", {}).get("order_id", "")
            else:
                oid = f"dry_mm_ask_{ticker}_{int(time.time())}"
            conn.execute("""INSERT INTO mm_orders
                (timestamp, ticker, side, price_cents, contracts, order_id,
                 fair_value_cents, inventory_at_post, tag)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (now, ticker, "no", no_price, sell_size, oid,
                 fair_value_cents, inventory, MM_ORDER_TAG))
            orders_posted += 1
            capital_used += sell_size * no_price
            print(f"    ASK {ticker} NO x{sell_size} @ {no_price}¢ (YES ask={ask}¢)")
        except Exception as e:
            print(f"    ASK {ticker} FAILED: {e}")

    conn.commit()
    return orders_posted, capital_used

def mm_run(conn, markets, balance_cents, portfolio_value, adaptive_weights=None,
           calibration_corrections=None, disabled_sources=None):
    """Main market-making pass. Called from the main loop after directional trading.

    Flow:
    1. Check fills from last cycle → update inventory
    2. Cancel all stale MM orders
    3. Select suitable markets
    4. For each market: get fair value, calculate quotes, post orders
    5. Log session stats

    Returns dict with MM stats for the session."""
    if not MM_ENABLED:
        return {"mm_enabled": False}

    now = datetime.now(timezone.utc).isoformat()
    _init_mm_tables(conn)

    stats = {
        "mm_enabled": True,
        "fills_detected": 0,
        "orders_cancelled": 0,
        "markets_quoted": 0,
        "orders_posted": 0,
        "capital_deployed": 0,
    }

    print("\n[mm] ═══ Market Making Pass ═══")

    # Step 0: Liquidate expiring positions to avoid settlement risk
    try:
        liq_count = mm_liquidate_expiring(conn)
        if liq_count:
            print(f"[mm] Posted {liq_count} liquidation orders for expiring markets")
    except Exception as e:
        print(f"[mm] Liquidation check failed: {e}")

    # Step 1: Check fills from last cycle
    stats["fills_detected"] = mm_check_fills(conn)
    if stats["fills_detected"]:
        print(f"[mm] {stats['fills_detected']} fills detected since last cycle")

    # Step 2: Cancel stale MM orders (we'll re-post at updated prices)
    stats["orders_cancelled"] = mm_cancel_all_orders(conn)
    print(f"[mm] Cancelled {stats['orders_cancelled']} stale orders")

    # Step 3: Supplement with targeted series fetching for active markets
    # The generic /markets endpoint returns newest-first (mostly parlays).
    # Targeted fetching ensures we see actual high-activity markets.
    MM_TARGET_SERIES = [
        "KXBTC", "KXETH", "KXINX", "KXGDP", "KXCPI", "KXJOB", "KXUNRATE",
        "KXFED", "KXGAS",
        # Weather: actual Kalshi series (KXHIGHNY, KXHIGHLAX, etc. not KXTEMP/KXWEATHER)
        "KXHIGHNY", "KXHIGHCHI", "KXHIGHLAX", "KXHIGHAUS", "KXHIGHMIA",
        "KXHIGHHOU", "KXHIGHPHX", "KXHIGHDEN", "KXHIGHSF",
        "KXHMONTHRANGE", "KXHURR",
        # Sports — DISABLED: odds source never matches Kalshi titles in practice.
        # Re-enable when Odds API integration is fixed with proper game/team matching.
        # "KXNBA", "KXNFL", "KXMLB", "KXNHL", "KXMMA", "KXSOCCER", "KXNCAA",
        # Company KPIs — DISABLED: data sources (SensorTower, Finnhub) unreliable/403.
        # Re-enable when live data feeds are verified working end-to-end.
        # "KXBOEING", "KXSPOTIFYMAU", "KXUBERTRIPS", "KXMETAHEADCOUNT", "KXHOOD",
        # "KXDASHORDERS", "KXLYFT", "KXMTCH", "KXPLTR", "KXRACE", "KXPM",
        # "KXABNB", "KXTESLASEMI", "KXEARNINGSMENTIONNFLX", "KXSTRIPEIPO",
        "KXISMPMI",          # ISM Manufacturing PMI — uses FRED data (reliable)
    ]
    seen_tickers = {m.get("ticker") for m in markets}
    targeted_count = 0
    series_counts = {}
    for series in MM_TARGET_SERIES:
        try:
            resp = api_get(f"/markets?limit=200&status=open&series_ticker={series}")
            batch = resp.get("markets", [])
            new_count = 0
            for m in batch:
                t = m.get("ticker", "")
                if t and t not in seen_tickers:
                    markets.append(m)
                    seen_tickers.add(t)
                    targeted_count += 1
                    new_count += 1
            if new_count > 0:
                series_counts[series] = new_count
        except Exception:
            pass  # series might not exist or have no open markets
    if targeted_count:
        print(f"[mm] Fetched {targeted_count} additional markets from {len(MM_TARGET_SERIES)} targeted series")
        print(f"[mm] Series breakdown: {dict(series_counts)}")

    # Pass category_edges so mm_select_markets can skip unprofitable categories
    _cat_edges = compute_category_edge_thresholds(conn) if conn else {}
    mm_candidates = mm_select_markets(markets, conn, balance_cents, category_edges=_cat_edges)
    print(f"[mm] {len(mm_candidates)} markets eligible for market making")

    if not mm_candidates:
        print("[mm] No suitable markets found this cycle")
        return stats

    # Step 4: Quote each market
    total_capital_used = 0
    max_mm_capital = int(balance_cents * MM_CAPITAL_PCT)

    for score, m, ticker, spread, mid, _stale_inventory, cat in mm_candidates:
        if total_capital_used >= max_mm_capital:
            print(f"[mm] Capital limit reached ({max_mm_capital/100:.2f})")
            break

        # CRITICAL: refresh inventory from DB (not stale snapshot from mm_select_markets)
        inventory, _ = mm_get_inventory(conn, ticker)

        # Get fair value from our ensemble estimate
        title = m.get("title", "") or m.get("subtitle", "") or ""
        yes_ask_f = float(m.get("yes_ask") or m.get("yes_ask_dollars") or 99)
        if yes_ask_f > 1:
            yes_ask_f /= 100
        vol = float(m.get("volume") or m.get("volume_24h_fp") or m.get("volume_fp") or 0)

        try:
            indep_prob, src_desc, n_sources = get_independent_estimate(
                ticker, m, yes_ask_f, vol,
                adaptive_weights=adaptive_weights,
                calibration_corrections=calibration_corrections,
                disabled_sources=disabled_sources)
        except Exception:
            indep_prob, src_desc, n_sources = None, None, 0

        # ── FAILSAFE: never market-make without REAL data ──
        # LLM estimates are guesses, not data — they provide no real edge for MM.
        # Only quote when we have at least 1 non-LLM source (weather, FRED, crypto, etc.)
        # Only EXOGENOUS data sources count — "series" and "momentum" are endogenous
        # (derived from market prices, not independent information)
        _MM_REAL_SOURCES = {"weather", "fred", "crypto", "clevfed", "noaa",
                           "polymarket", "metaculus", "bls", "tomorrow"}
        src_desc_str = src_desc or ""
        has_real_source = (indep_prob is not None and n_sources >= 1 and
                          any(s in src_desc_str for s in _MM_REAL_SOURCES))

        if not has_real_source:
            stats.setdefault("skipped_no_data", 0)
            stats["skipped_no_data"] += 1
            reason = "no data source" if n_sources < 1 else "LLM-only (no real data)"
            print(f"  {ticker}: SKIP — {reason} [{cat}]")
            continue
        else:
            fair_value_cents = max(2, min(98, int(indep_prob * 100)))
            # Sanity check: fair value too extreme → widen spread or skip
            if fair_value_cents <= 3 or fair_value_cents >= 85:
                print(f"  {ticker}: SKIP — extreme fair value {fair_value_cents}¢ (prob={indep_prob:.3f})")
                stats.setdefault("skipped_extreme_fv", 0)
                stats["skipped_extreme_fv"] += 1
                continue
            print(f"  {ticker}: fair={fair_value_cents}¢ mid={mid}¢ spread={spread}¢ "
                  f"inv={inventory:+d} [{cat}] ({n_sources} sources)")

        # Post two-sided quotes
        posted, capital = mm_post_quotes(conn, m, fair_value_cents, balance_cents, inventory)
        stats["orders_posted"] += posted
        total_capital_used += capital
        if posted > 0:
            stats["markets_quoted"] += 1

    stats["capital_deployed"] = total_capital_used

    # Step 5: Compute total inventory stats
    inv_rows = conn.execute(
        "SELECT ticker, net_position, realized_pnl_cents, avg_entry_cents FROM mm_inventory"
    ).fetchall()
    total_inv_value = 0
    total_realized = 0
    for ticker, net, rpnl, avg_e in inv_rows:
        total_inv_value += abs(net) * int(avg_e)
        total_realized += rpnl

    stats["inventory_value_cents"] = total_inv_value
    stats["realized_pnl_cents"] = total_realized

    # ── Step 6: QA LOOP — re-check MM inventory against fresh data ─────────
    # Check a rotating subset of inventory positions each run (max 5) to avoid
    # blowing the systemd timeout. Prioritize largest positions first.
    MAX_QA_PER_RUN = 5
    qa_flags = 0
    active_inv = [(t, n, r, a) for t, n, r, a in inv_rows if abs(n) > 0]
    # Sort by position size (largest first) — check the riskiest positions first
    active_inv.sort(key=lambda x: abs(x[1]) * int(x[3]), reverse=True)
    # Rotate which positions get checked using run count
    run_count = conn.execute("SELECT COUNT(*) FROM mm_sessions").fetchone()[0] or 0
    start_idx = (run_count * MAX_QA_PER_RUN) % max(len(active_inv), 1)
    qa_batch = active_inv[start_idx:start_idx + MAX_QA_PER_RUN]
    # Wrap around if needed
    if len(qa_batch) < MAX_QA_PER_RUN and start_idx > 0:
        qa_batch += active_inv[:MAX_QA_PER_RUN - len(qa_batch)]

    if qa_batch:
        print(f"[mm-qa] Checking {len(qa_batch)}/{len(active_inv)} inventory positions")
    for ticker, net, rpnl, avg_e in qa_batch:
        try:
            mkt = api_get(f"/markets/{ticker}")
            market = mkt.get("market", mkt)
            yes_ask_f = float(market.get("yes_ask") or market.get("yes_ask_dollars") or 99)
            if yes_ask_f > 1:
                yes_ask_f /= 100
            vol = float(market.get("volume") or market.get("volume_fp") or 0)
            fresh_prob, _, fresh_n = get_independent_estimate(
                ticker, market, yes_ask_f, vol,
                disabled_sources=disabled_sources)
            if fresh_prob is not None and fresh_n > 0:
                entry_f = int(avg_e) / 100
                if net > 0 and fresh_prob < entry_f - 0.05:
                    print(f"[mm-qa] ⚠️  {ticker}: HOLD YES x{net} entry={entry_f:.2f} "
                          f"but fair={fresh_prob:.2f} — LOSING POSITION")
                    qa_flags += 1
                elif net < 0 and (1 - fresh_prob) < (1 - entry_f) - 0.05:
                    print(f"[mm-qa] ⚠️  {ticker}: HOLD NO x{abs(net)} entry={entry_f:.2f} "
                          f"but fair={fresh_prob:.2f} — LOSING POSITION")
                    qa_flags += 1
                else:
                    print(f"[mm-qa] ✓ {ticker}: net={net:+d} entry={entry_f:.2f} "
                          f"fair={fresh_prob:.2f} — OK ({fresh_n} sources)")
        except Exception:
            pass
    if qa_flags:
        print(f"[mm-qa] {qa_flags} positions flagged as potentially losing")
    stats["qa_flags"] = qa_flags

    # Log MM session
    conn.execute("""INSERT INTO mm_sessions
        (recorded_at, markets_quoted, orders_posted, orders_cancelled,
         fills_detected, inventory_value_cents, realized_pnl_cents, unrealized_pnl_cents)
        VALUES (?,?,?,?,?,?,?,?)""",
        (now, stats["markets_quoted"], stats["orders_posted"], stats["orders_cancelled"],
         stats["fills_detected"], total_inv_value, total_realized, 0))
    conn.commit()

    skipped = stats.get('skipped_no_data', 0)
    print(f"[mm] Summary: {stats['markets_quoted']} markets quoted, {skipped} skipped (no data), "
          f"{stats['orders_posted']} orders, {stats['fills_detected']} fills, "
          f"inventory=${total_inv_value/100:.2f}, realized P&L=${total_realized/100:+.2f}"
          f"{f', {qa_flags} QA flags' if qa_flags else ''}")

    return stats


def mm_compute_adverse_selection(conn):
    """Analyze MM fills for adverse selection: how often do fills move against us?
    If a fill consistently predicts the wrong outcome (we buy YES and it resolves NO),
    that means informed traders are picking us off.

    Returns dict of {ticker: adverse_selection_rate} for markets we should avoid or widen spreads on."""
    adverse = {}
    try:
        # Join fills with settlements to see if our fills were winners or losers
        rows = conn.execute("""
            SELECT mm.ticker, mm.side, mm.price_cents, s.won
            FROM mm_orders mm
            JOIN settlements s ON mm.ticker = s.ticker
            WHERE mm.status = 'filled'
        """).fetchall()

        per_ticker = {}
        for ticker, side, price, won in rows:
            if ticker not in per_ticker:
                per_ticker[ticker] = {"total": 0, "adverse": 0}
            per_ticker[ticker]["total"] += 1
            # Adverse = we bought and it lost, or we sold and it won
            if (side == "yes" and not won) or (side == "no" and won):
                per_ticker[ticker]["adverse"] += 1

        for ticker, stats in per_ticker.items():
            if stats["total"] >= 5:  # need minimum sample
                rate = stats["adverse"] / stats["total"]
                adverse[ticker] = rate
                if rate > 0.65:
                    print(f"[mm_learn] ⚠️  High adverse selection on {ticker}: "
                          f"{rate:.0%} ({stats['adverse']}/{stats['total']})")
    except Exception as e:
        print(f"[mm_learn] Error computing adverse selection: {e}")
    return adverse


def mm_liquidate_expiring(conn):
    """Check for MM inventory in markets that are about to expire (<1h) or already expired.
    - Expiring (<1h): post aggressive exit orders to avoid holding through settlement.
    - Already expired/settled/closed: zero out inventory (market settled, position is gone).
    Returns count of liquidation orders posted."""
    liquidated = 0
    try:
        inv_rows = conn.execute(
            "SELECT ticker, net_position, avg_entry_cents FROM mm_inventory WHERE net_position != 0"
        ).fetchall()
        for ticker, net, avg_entry in inv_rows:
            try:
                m_data = api_get(f"/markets/{ticker}")
                market = m_data.get("market", m_data)

                # Check if market is already settled/closed
                status = (market.get("status") or market.get("result") or "").lower()
                if status in ("settled", "closed", "finalized", "determined"):
                    # Market settled — zero out inventory, record settlement P&L
                    result = market.get("result", "")
                    # Determine payout: if result is "yes", YES holders get $1; "no", NO holders get $1
                    if result == "yes":
                        pnl_cents = net * (100 - avg_entry) if net > 0 else net * avg_entry
                    elif result == "no":
                        no_cost = 100 - avg_entry
                        pnl_cents = -abs(net) * avg_entry if net > 0 else abs(net) * (100 - no_cost)
                    else:
                        pnl_cents = 0  # unknown result, just zero out
                    conn.execute("""UPDATE mm_inventory SET net_position=0,
                                   realized_pnl_cents = realized_pnl_cents + ?
                                   WHERE ticker=?""", (int(pnl_cents), ticker))
                    conn.commit()
                    print(f"[mm] Settled {ticker}: net={net:+d} result={result} pnl=${pnl_cents/100:+.2f}")
                    continue

                close_time = market.get("close_time") or market.get("expiration_time")
                if not close_time:
                    continue
                ct = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                hours_left = (ct - datetime.now(timezone.utc)).total_seconds() / 3600

                if hours_left < -1:
                    # Market closed more than 1 hour ago but not marked settled in API
                    # Zero out — it's settled or will settle soon
                    conn.execute("UPDATE mm_inventory SET net_position=0 WHERE ticker=?", (ticker,))
                    conn.commit()
                    print(f"[mm] Expired {ticker}: zeroed out (closed {-hours_left:.1f}h ago)")
                    continue

                if hours_left > 1:
                    continue  # not expiring yet

                # Liquidate: post market-crossing order to exit
                qty = abs(net)
                if net > 0:
                    yes_bid = market.get("yes_bid") or market.get("yes_bid_dollars") or 0
                    if isinstance(yes_bid, str):
                        yes_bid = int(float(yes_bid) * 100)
                    elif isinstance(yes_bid, float) and yes_bid < 1:
                        yes_bid = int(yes_bid * 100)
                    no_price = max(1, 100 - yes_bid + 2)
                    order_body = {
                        "ticker": ticker, "side": "no", "type": "limit",
                        "count": qty, "no_price": no_price, "action": "buy",
                        "expiration_ts": int(time.time() + 3600)
                    }
                else:
                    yes_ask = market.get("yes_ask") or market.get("yes_ask_dollars") or 99
                    if isinstance(yes_ask, str):
                        yes_ask = int(float(yes_ask) * 100)
                    elif isinstance(yes_ask, float) and yes_ask < 1:
                        yes_ask = int(yes_ask * 100)
                    order_body = {
                        "ticker": ticker, "side": "yes", "type": "limit",
                        "count": qty, "yes_price": min(99, yes_ask + 2), "action": "buy",
                        "expiration_ts": int(time.time() + 3600)
                    }

                if not MM_DRY_RUN:
                    resp = api_post("/portfolio/orders", order_body)
                    liq_order_id = resp.get("order", {}).get("order_id", "")
                    # Track in mm_orders so fills are detected by mm_check_fills()
                    if liq_order_id:
                        liq_side = order_body["side"]
                        liq_price = order_body.get("no_price") or order_body.get("yes_price", 0)
                        conn.execute("""INSERT OR IGNORE INTO mm_orders
                            (created_at, order_id, ticker, side, price_cents, contracts, tag, status, fill_qty, fair_value_cents)
                            VALUES (?, ?, ?, ?, ?, ?, 'liquidation', 'posted', 0, 0)""",
                            (datetime.now(timezone.utc).isoformat(), liq_order_id, ticker, liq_side, liq_price, qty))
                        conn.commit()
                    print(f"[mm] Liquidating {ticker}: {qty} contracts "
                          f"(net={net:+d}, {hours_left:.1f}h left) order={liq_order_id[:12] if liq_order_id else '?'}")
                    liquidated += 1
                else:
                    print(f"[mm] DRY: Would liquidate {ticker}: {qty} contracts")
                    liquidated += 1
            except Exception as e:
                err_str = str(e)
                if "404" in err_str:
                    # Market no longer exists in API — it's been settled/removed
                    conn.execute("UPDATE mm_inventory SET net_position=0 WHERE ticker=?", (ticker,))
                    conn.commit()
                    print(f"[mm] Removed {ticker}: market no longer exists (404), zeroed inventory")
                elif "400" in err_str:
                    # 400 on order submission = market likely closed/frozen/delisted
                    conn.execute("UPDATE mm_inventory SET net_position=0 WHERE ticker=?", (ticker,))
                    conn.commit()
                    print(f"[mm] Removed {ticker}: cannot trade (400), zeroed inventory")
                else:
                    print(f"[mm] Error liquidating {ticker}: {e}")
    except Exception as e:
        print(f"[mm] Error in liquidation scan: {e}")
    return liquidated


def mm_adaptive_spread(conn, ticker, base_half_spread):
    """Adjust spread width based on learned adverse selection rate.
    Markets with high adverse selection get wider spreads.
    Returns adjusted half-spread in cents."""
    adverse = mm_compute_adverse_selection(conn)
    rate = adverse.get(ticker, 0.5)  # default 50% (neutral)

    if rate > 0.7:
        # Very high adverse selection — double the spread
        return min(15, base_half_spread * 2)
    elif rate > 0.6:
        # Moderate — widen by 50%
        return min(12, int(base_half_spread * 1.5))
    elif rate < 0.35:
        # Low adverse selection — tighten slightly for more fills
        return max(2, base_half_spread - 1)
    return base_half_spread


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main():
    global MIN_EDGE, SINGLE_SOURCE_EDGE
    conn = init_db()
    now  = datetime.now(timezone.utc).isoformat()

    # ── Phase system: compute and apply sizing limits ─────────────────────
    phase_num, phase_cfg, phase_stats = compute_current_phase(conn)
    effective_limits = apply_phase_limits(phase_num, phase_cfg)
    print(f"[phase] Phase {phase_num}: {phase_cfg[7]}")
    print(f"[phase] Track record: {phase_stats['settled']} settled, "
          f"{phase_stats['win_rate']:.1%} win rate "
          f"(recent {phase_stats['recent_n']}: {phase_stats['recent_win_rate']:.1%})")
    print(f"[phase] Limits: DRY_RUN={DRY_RUN}  MAX_POS={MAX_POSITION_PCT:.3%}  "
          f"MAX_PORT={MAX_PORTFOLIO_PCT:.1%}  MAX_CONTRACTS={MAX_CONTRACTS}  "
          f"KELLY={KELLY_FRACTION:.4f}  MIN_EDGE={MIN_EDGE:.3f}")

    result = {"markets_scanned":0, "opportunities":[], "orders_placed":[],
              "positions_managed":0, "orders_pruned":0, "pnl":0.0,
              "timestamp":now, "api_base":HOST, "dry_run":DRY_RUN,
              "halted":False, "halt_reason":"", "patterns_avoided":[],
              "settlements_recorded":0,
              "phase": phase_num, "phase_desc": phase_cfg[7],
              "phase_stats": phase_stats, "effective_limits": effective_limits}

    # ── Phase 1: Housekeeping ─────────────────────────────────────────────
    result["orders_pruned"] = prune_stale_orders()
    track_fills(conn)
    result["settlements_recorded"] = record_settlements(conn)
    result["positions_managed"] = manage_positions(conn)
    avoid_filters = compute_avoid_filters(conn)
    result["patterns_avoided"] = avoid_filters.get("summary", [])

    # ── Adaptive learning: compute updated weights, calibration, and edge thresholds
    # Reset per-run caches so we recompute from latest settlement data
    global _LEARNED_WEIGHTS, _CALIBRATION_CURVE, _CATEGORY_EDGES
    _LEARNED_WEIGHTS = None
    _CALIBRATION_CURVE = None
    _CATEGORY_EDGES = None
    adaptive_weights = compute_adaptive_weights(conn)
    calibration_corrections = compute_calibration_correction(conn)
    category_edges = compute_category_edge_thresholds(conn)
    result["adaptive_weights"] = adaptive_weights
    result["calibration_corrections"] = calibration_corrections
    result["category_edges"] = category_edges

    # ── Advanced learning loops ─────────────────────────────────────────────
    # Loss post-mortems: classify why we lose
    try:
        result["postmortems"] = run_loss_postmortems(conn)
    except Exception as e:
        print(f"[postmortem] Error: {e}"); result["postmortems"] = 0

    # Edge convergence: are we actually smarter than the market?
    try:
        result["convergence_checks"] = check_edge_convergence(conn)
    except Exception as e:
        print(f"[convergence] Error: {e}"); result["convergence_checks"] = 0

    # Timing pattern data: when do we trade best?
    try:
        result["timing_records"] = record_timing_data(conn)
    except Exception as e:
        print(f"[timing] Error: {e}"); result["timing_records"] = 0

    # Shadow hyperparam evaluation: compare actual vs alternative params
    try:
        analyze_shadow_performance(conn)
    except Exception as e:
        print(f"[shadow] Error: {e}")

    # ── Active feedback: learning loops → trading adjustments ──────────────
    try:
        active_feedback = compute_active_feedback(conn)
        result["active_feedback"] = {
            "disabled_sources": list(active_feedback["disabled_sources"]),
            "disabled_strategies": list(active_feedback.get("disabled_strategies", set())),
            "edge_multiplier": active_feedback["edge_multiplier"],
            "skip_hours": list(active_feedback["skip_hours"]),
            "loss_type_breakdown": active_feedback["loss_type_adjustments"],
            "convergence_rate": active_feedback["convergence_rate"],
            "strategy_stats": active_feedback.get("strategy_stats", {}),
        }
        # Apply edge multiplier from convergence + loss analysis
        if active_feedback["edge_multiplier"] != 1.0:
            MIN_EDGE *= active_feedback["edge_multiplier"]
            SINGLE_SOURCE_EDGE *= active_feedback["edge_multiplier"]
            print(f"[feedback] Adjusted MIN_EDGE to {MIN_EDGE:.3f}, "
                  f"SINGLE_SOURCE_EDGE to {SINGLE_SOURCE_EDGE:.3f} "
                  f"(multiplier={active_feedback['edge_multiplier']:.2f})")

        # Check if current hour should be skipped
        current_hour = datetime.now(timezone.utc).hour
        if current_hour in active_feedback["skip_hours"]:
            print(f"[feedback] ⚠️  Hour {current_hour}:00 UTC is a historically bad trading window. "
                  f"Skipping new trades this run.")
            result["skip_new_trades"] = True
        else:
            result["skip_new_trades"] = False
    except Exception as e:
        print(f"[feedback] Error computing active feedback: {e}")
        active_feedback = {"disabled_sources": set(), "disabled_strategies": set(),
                          "edge_multiplier": 1.0, "skip_hours": set(),
                          "convergence_rate": None, "strategy_stats": {},
                          "strategy_bandit": {}}
        result["skip_new_trades"] = False

    # Generate Phase 1 diagnostic if we're still in early paper trading
    if phase_num <= 1:
        generate_diagnostic_report(conn, result)

    # ── Phase 2: Balance & limits ─────────────────────────────────────────
    try:
        initial_balance, portfolio_value = get_portfolio()
        print(f"[trade.py] Balance=${initial_balance/100:.2f}  Portfolio=${portfolio_value/100:.2f}")
    except Exception as e:
        print(f"[trade.py] CRITICAL: Cannot fetch portfolio: {e}")
        print("[trade.py] Aborting run — cannot trade without knowing balance")
        return {"error": f"portfolio_fetch_failed: {e}"}

    markets = []  # populated in Phase 3; used by both Phase 4 (directional) and Phase 4a (MM)

    day_start = get_day_start_balance(conn)
    if day_start is None:
        day_start = initial_balance + portfolio_value  # first run of the day — use total equity
    ok, halt_reason = check_limits(day_start, initial_balance, portfolio_value)
    if not ok:
        print(f"[trade.py] HALTED: {halt_reason}")
        result.update(halted=True, halt_reason=halt_reason)
    elif result.get("skip_new_trades"):
        print("[trade.py] ⏭️  Skipping new trades this run (active feedback: bad hour)")
        result["trades_skipped_reason"] = "active_feedback_skip_hour"
    else:
        # ── Phase 3: Scan & score ─────────────────────────────────────────
        print("[trade.py] Fetching markets …")
        markets = []
        cursor = None
        MAX_PAGES = 10  # scan up to 5000 markets to get past parlay flood
        try:
            for page in range(MAX_PAGES):
                url = "/markets?limit=500&status=open"
                if cursor:
                    url += f"&cursor={cursor}"
                resp = api_get(url)
                batch = resp.get("markets", [])
                markets.extend(batch)
                cursor = resp.get("cursor")
                print(f"[trade.py] Page {page+1}: fetched {len(batch)} markets (total {len(markets)})")
                if not cursor or len(batch) < 500:
                    break  # no more pages
        except Exception as e:
            print(f"[trade.py] ERROR fetching markets: {e}")
        result["markets_scanned"] = len(markets)
        print(f"[trade.py] Scanned {len(markets)} markets total")

        candidates = []
        for m in markets:
            # Skip multi-leg parlay/combo markets — synthetic, not real tradeable markets
            _t = m.get("ticker", "")
            if "KXMVE" in _t or "MULTIGAME" in _t or m.get("mve_collection_ticker"):
                continue
            score, side, strategy, detail, volume, sc, indep_prob, mkt_prob, edge = score_market(
                m, adaptive_weights=adaptive_weights,
                calibration_corrections=calibration_corrections,
                category_edges=category_edges,
                disabled_sources=active_feedback.get("disabled_sources"),
                disabled_strategies=active_feedback.get("disabled_strategies"),
                strategy_bandit=active_feedback.get("strategy_bandit"))
            if score <= 0: continue
            ticker = m.get("ticker", "")
            ok_t, skip_reason = passes_filters(ticker, strategy, volume, sc, avoid_filters)
            if not ok_t: print(f"  ⊘ {ticker}: {skip_reason}"); continue
            candidates.append((score, side, strategy, detail, volume, sc, indep_prob, mkt_prob, edge, m))

        # Dedup against open orders
        open_positions = get_open_tickers()
        candidates = [c for c in candidates if (c[9].get("ticker",""), c[1]) not in open_positions]

        # ── Correlation limits: max positions per risk category ───────
        category_counts = {}
        try:
            resp = api_get("/portfolio/positions?limit=100")
            existing_pos = resp.get("market_positions", resp.get("positions", []))
            for pos in existing_pos:
                t = pos.get("ticker", "")
                _pos_raw = pos.get("position_fp") or pos.get("position", 0)
                if abs(int(float(_pos_raw))) > 0:
                    cat = categorize_market(t, "")
                    category_counts[cat] = category_counts.get(cat, 0) + 1
            print(f"[correlation] Existing positions by category: {dict(category_counts)}")
        except Exception as e:
            print(f"[correlation] Could not fetch positions: {e}")

        filtered_candidates = []
        for c in candidates:
            cticker = c[9].get("ticker", "")
            ctitle = c[9].get("title", "") or c[9].get("subtitle", "") or ""
            cat = categorize_market(cticker, ctitle)
            current = category_counts.get(cat, 0)
            if current >= MAX_PER_CATEGORY:
                print(f"  ⊘ {cticker}: category '{cat}' full ({current}/{MAX_PER_CATEGORY})")
                continue
            category_counts[cat] = current + 1
            filtered_candidates.append(c)
        candidates = filtered_candidates

        candidates.sort(key=lambda x: x[0], reverse=True)

        # ── Explore/exploit: reserve slots for under-explored categories ──
        n_explore = 0
        try:
            exploit_picks, explore_picks = compute_exploration_targets(conn, candidates, 5)
            top = exploit_picks + explore_picks
            n_explore = len(explore_picks)
        except Exception as e:
            print(f"[explore] Error: {e}")
            top = candidates[:5]
        print(f"[trade.py] {len(candidates)} candidates → trading top {len(top)} "
              f"({len(top) - n_explore} exploit + {n_explore} explore)")

        # ── Phase 4: Execute ──────────────────────────────────────────────
        # Separate MM vs directional exposure budgets.
        # MM inventory is managed by its own capital cap (MM_CAPITAL_PCT).
        # Directional trades get their own budget so MM doesn't crowd them out.
        mm_inventory_cents = 0
        try:
            inv_rows = conn.execute(
                "SELECT SUM(ABS(net_position) * avg_entry_cents) FROM mm_inventory"
            ).fetchone()
            mm_inventory_cents = int(inv_rows[0] or 0)
        except Exception:
            pass  # table might not exist yet
        directional_exposure = max(0, portfolio_value - mm_inventory_cents)
        total_exposure_cents = directional_exposure
        max_exposure_cents = int(initial_balance * MAX_PORTFOLIO_PCT)
        print(f"[exposure] Directional={directional_exposure/100:.2f} "
              f"(MM inv={mm_inventory_cents/100:.2f} excluded) "
              f"Max={max_exposure_cents/100:.2f} ({MAX_PORTFOLIO_PCT:.0%} of balance)")

        current_balance = initial_balance
        for score, side, strategy, detail, volume, sc, indep_prob, mkt_prob, edge, m in top:
            try: current_balance, current_pv = get_portfolio()
            except: current_pv = portfolio_value
            ok, halt_reason = check_limits(day_start, current_balance, current_pv)
            if not ok:
                print(f"[trade.py] HALTING: {halt_reason}")
                result.update(halted=True, halt_reason=halt_reason); break

            ticker = m.get("ticker", "")

            # Determine price: hit the ask for info-edge trades
            def _pc(v): v=float(v or 99); return int(round(v*100)) if v<=1.0 else int(v)
            price_cents = max(1, min(99,
                _pc(m.get("yes_ask") or m.get("yes_ask_dollars")) if side=="yes"
                else _pc(m.get("no_ask") or m.get("no_ask_dollars"))))

            # Kelly with independent estimate
            prob_for_kelly = indep_prob if indep_prob else (1 - price_cents/100)
            contracts = kelly_contracts(prob_for_kelly, price_cents, current_balance)
            if contracts <= 0: print(f"  → {ticker}: Kelly skip (no edge)"); continue

            # Order book depth check: cap contracts to what the book can absorb
            book_depth = get_orderbook_depth(ticker, side, price_cents)
            if book_depth is not None:
                if book_depth < 3:
                    print(f"  → {ticker}: SKIP — order book too thin ({book_depth} contracts available)")
                    continue
                # Don't take more than 50% of available liquidity to avoid market impact
                max_from_book = max(1, book_depth // 2)
                if contracts > max_from_book:
                    print(f"  → {ticker}: reduced {contracts}→{max_from_book} contracts "
                          f"(book depth={book_depth})")
                    contracts = max_from_book

            # Portfolio exposure check: would this trade push us over the limit?
            order_cost_cents = contracts * price_cents
            if total_exposure_cents + order_cost_cents > max_exposure_cents:
                # Reduce contracts to fit within exposure limit
                headroom = max_exposure_cents - total_exposure_cents
                if headroom <= 0:
                    print(f"  → {ticker}: SKIP — portfolio exposure at max "
                          f"({total_exposure_cents/100:.2f}/{max_exposure_cents/100:.2f})")
                    continue
                contracts = max(1, min(MAX_CONTRACTS, int(headroom / price_cents)))
                order_cost_cents = contracts * price_cents
                print(f"  → {ticker}: reduced to {contracts} contracts for exposure limit")

            opp = {"ticker":ticker, "side":side, "strategy":strategy,
                   "score":round(score,3), "detail":detail, "price_cents":price_cents,
                   "contracts":contracts, "volume":volume, "spread_cents":sc,
                   "independent_prob":round(indep_prob,3) if indep_prob else None,
                   "market_prob":round(mkt_prob,3) if mkt_prob else None,
                   "edge":round(edge,3) if edge else None}
            result["opportunities"].append(opp)
            print(f"  → {ticker} {side} @ {price_cents}¢ x{contracts}  "
                  f"edge={edge:.1%}  [{strategy}]")

            order_id = error = None
            if not DRY_RUN and ticker:
                order_body = {"ticker":ticker, "side":side, "type":"limit",
                    "count":contracts,
                    ("yes_price" if side=="yes" else "no_price"): price_cents,
                    "action":"buy",
                    "expiration_ts": int(time.time() + ORDER_MAX_AGE_HOURS * 3600)}
                try:
                    resp = api_post("/portfolio/orders", order_body)
                    order_id = resp.get("order",{}).get("order_id") or str(resp)
                    result["orders_placed"].append({"ticker":ticker,"contracts":contracts,"order_id":order_id})
                    total_exposure_cents += order_cost_cents
                    print(f"    ✓ order_id={order_id}")
                except Exception as e:
                    error = str(e)
                    result["orders_placed"].append({"ticker":ticker,"error":error})
                    print(f"    ✗ {error}")
            else:
                result["orders_placed"].append({"ticker":ticker,"contracts":contracts,"dry_run":True})

            conn.execute("""INSERT INTO trades
                (timestamp,ticker,side,action,score,reason,strategy,price_cents,contracts,
                 volume,spread_cents,independent_prob,market_prob,edge,dry_run,order_id,error)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (now,ticker,side,"buy",score,detail,strategy,price_cents,contracts,
                 volume,sc,indep_prob,mkt_prob,edge,int(DRY_RUN),order_id,error))
            conn.commit()

    # ── Phase 4a: Market Making ──────────────────────────────────────────
    # Run AFTER directional trading, using remaining capital headroom.
    # Market making posts two-sided limit orders in thin markets to earn spread.
    try:
        if markets and MM_ENABLED:
            mm_stats = mm_run(
                conn, markets, initial_balance, portfolio_value,
                adaptive_weights=adaptive_weights,
                calibration_corrections=calibration_corrections,
                disabled_sources=active_feedback.get("disabled_sources"))
            result["mm_stats"] = mm_stats
        else:
            result["mm_stats"] = {"mm_enabled": False, "reason": "no markets or disabled"}
    except Exception as e:
        print(f"[mm] Error in market making pass: {e}")
        result["mm_stats"] = {"mm_enabled": True, "error": str(e)}

    # ── Phase 4b: Post-trade learning ───────────────────────────────────
    # Record shadow hyperparam evaluations for this run's trades
    try:
        record_shadow_evaluations(conn, result)
    except Exception as e:
        print(f"[shadow] Error recording: {e}")

    # Record pipeline health stats for this run
    try:
        record_pipeline_health(conn)
    except Exception as e:
        print(f"[pipeline] Error recording: {e}")

    # ── Phase 5: Log session ──────────────────────────────────────────────
    conn.execute("""INSERT INTO sessions
        (timestamp,balance_cents,portfolio_cents,markets_scanned,opportunities_found,
         orders_attempted,positions_managed,orders_pruned,dry_run,halted,halt_reason,patterns_avoided)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (now,initial_balance,portfolio_value,result["markets_scanned"],
         len(result["opportunities"]),len(result["orders_placed"]),
         result["positions_managed"],result["orders_pruned"],
         int(DRY_RUN),int(result["halted"]),result["halt_reason"],
         json.dumps(result["patterns_avoided"])))
    conn.commit()

    # ── Phase 6: Generate human-readable performance report ──────────────
    try:
        generate_performance_report(conn, result)
    except Exception as e:
        print(f"[report] Error generating report: {e}")

    conn.close()

    task_dir = "/task" if os.path.exists("/task") else "/tmp"
    with open(f"{task_dir}/trades.json","w") as f: json.dump(result, f, indent=2)
    print(f"[trade.py] Done → markets={result['markets_scanned']} opps={len(result['opportunities'])} "
          f"orders={len(result['orders_placed'])} positions_managed={result['positions_managed']} "
          f"pruned={result['orders_pruned']} settlements={result['settlements_recorded']}")
    return result

if __name__ == "__main__":
    main()
