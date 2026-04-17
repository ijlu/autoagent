#!/usr/bin/env python3
"""
Diagnose data source connectivity on VPS.
Run: sudo -u kalshi python3 /home/kalshi/autoagent/diagnose_sources.py
"""
import os, sys, time, json, socket

# Load .env
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k] = v
    print(f"[OK] Loaded .env from {env_path}")
else:
    print(f"[FAIL] No .env found at {env_path}")

print("=" * 70)
print("  DATA SOURCE DIAGNOSTIC")
print("=" * 70)

# 1. Check environment variables
print("\n═══ 1. ENVIRONMENT VARIABLES ═══")
keys_needed = {
    "FRED_API_KEY": "FRED (Federal Reserve economic data)",
    "ODDS_API_KEY": "The-Odds-API (sports betting odds)",
    "FINNHUB_API_KEY": "Finnhub (financial news & company data)",
    "SENSORTOWER_API_TOKEN": "SensorTower (app analytics)",
    "OPENAI_API_KEY": "OpenAI (LLM fallback estimates)",
    "KALSHI_API_KEY_ID": "Kalshi API authentication",
    "KALSHI_PRIVATE_KEY_PATH": "Kalshi RSA key file",
}
missing_keys = []
for key, desc in keys_needed.items():
    val = os.environ.get(key, "")
    if val:
        # Show first 8 chars only
        masked = val[:8] + "..." if len(val) > 8 else val
        print(f"  [OK]   {key} = {masked}  ({desc})")
    else:
        print(f"  [MISS] {key} — NOT SET  ({desc})")
        missing_keys.append(key)

if missing_keys:
    print(f"\n  ⚠️  Missing {len(missing_keys)} keys: {', '.join(missing_keys)}")
    print("  These sources will silently return None!")

# 2. Check SSL / certificates
print("\n═══ 2. SSL CERTIFICATES ═══")
import ssl
ctx = ssl.create_default_context()
print(f"  Default SSL context: {ctx.protocol}")
print(f"  CA certs loaded: {ctx.cert_store_stats()}")

cert_paths = ["/etc/ssl/certs/ca-certificates.crt", "/etc/ssl/cert.pem",
              "/usr/lib/ssl/cert.pem"]
for p in cert_paths:
    exists = os.path.exists(p)
    print(f"  {p}: {'EXISTS' if exists else 'not found'}")

# 3. Check DNS resolution
print("\n═══ 3. DNS RESOLUTION ═══")
domains = [
    "api.stlouisfed.org",       # FRED
    "api.coingecko.com",        # Crypto
    "api.open-meteo.com",       # Weather
    "api.weather.gov",          # NOAA
    "www.clevelandfed.org",     # Cleveland Fed
    "api.the-odds-api.com",     # Sports odds
    "www.metaculus.com",        # Metaculus
    "finnhub.io",               # Finnhub
    "api.sensortower.com",      # SensorTower
    "gamma-api.polymarket.com", # Polymarket
    "www.deribit.com",          # Deribit (crypto options)
    "api.elections.kalshi.com", # Kalshi
]
dns_failures = []
for domain in domains:
    try:
        ip = socket.gethostbyname(domain)
        print(f"  [OK]   {domain:35s} → {ip}")
    except socket.gaierror as e:
        print(f"  [FAIL] {domain:35s} → DNS FAILED: {e}")
        dns_failures.append(domain)

if dns_failures:
    print(f"\n  ⚠️  {len(dns_failures)} DNS failures! Check /etc/resolv.conf")

# 4. Test actual HTTP requests
print("\n═══ 4. HTTP CONNECTIVITY ═══")
import requests

tests = [
    ("FRED", f"https://api.stlouisfed.org/fred/series/observations?series_id=UNRATE&api_key={os.environ.get('FRED_API_KEY','demo')}&file_type=json&sort_order=desc&limit=1", 5),
    ("CoinGecko", "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd", 5),
    ("Open-Meteo", "https://api.open-meteo.com/v1/forecast?latitude=40.71&longitude=-74.01&daily=temperature_2m_max&forecast_days=1", 5),
    ("NOAA", "https://api.weather.gov/alerts/active?status=actual&message_type=alert&limit=1", 8),
    ("Cleveland Fed", "https://www.clevelandfed.org/api/InflationNowcasting/InflationNowcast", 8),
    ("Metaculus", "https://www.metaculus.com/api2/questions/?type=forecast&status=open&limit=1&order_by=-activity", 10),
    ("Polymarket", "https://gamma-api.polymarket.com/markets?closed=false&limit=1", 10),
    ("Kalshi", "https://api.elections.kalshi.com/trade-api/v2/markets?limit=1&status=open", 5),
]

# Add key-gated tests only if keys exist
if os.environ.get("FINNHUB_API_KEY"):
    tests.append(("Finnhub", f"https://finnhub.io/api/v1/quote?symbol=AAPL&token={os.environ['FINNHUB_API_KEY']}", 5))
if os.environ.get("ODDS_API_KEY"):
    tests.append(("Odds API", f"https://api.the-odds-api.com/v4/sports?apiKey={os.environ['ODDS_API_KEY']}", 5))
if os.environ.get("SENSORTOWER_API_TOKEN"):
    tests.append(("SensorTower", f"https://api.sensortower.com/v1/health?auth_token={os.environ['SENSORTOWER_API_TOKEN']}", 5))

http_failures = []
for name, url, timeout in tests:
    t0 = time.time()
    try:
        # Mask API keys in display URL
        display_url = url.split("?")[0]
        r = requests.get(url, timeout=timeout)
        latency = (time.time() - t0) * 1000
        if r.status_code == 200:
            try:
                data = r.json()
                data_preview = str(data)[:100]
            except:
                data_preview = r.text[:100]
            print(f"  [OK]   {name:15s} {r.status_code} in {latency:.0f}ms — {data_preview}")
        else:
            print(f"  [WARN] {name:15s} HTTP {r.status_code} in {latency:.0f}ms — {r.text[:100]}")
            http_failures.append((name, f"HTTP {r.status_code}"))
    except requests.exceptions.ConnectTimeout:
        latency = (time.time() - t0) * 1000
        print(f"  [FAIL] {name:15s} CONNECT TIMEOUT after {latency:.0f}ms")
        http_failures.append((name, "connect timeout"))
    except requests.exceptions.ReadTimeout:
        latency = (time.time() - t0) * 1000
        print(f"  [FAIL] {name:15s} READ TIMEOUT after {latency:.0f}ms")
        http_failures.append((name, "read timeout"))
    except requests.exceptions.SSLError as e:
        print(f"  [FAIL] {name:15s} SSL ERROR: {e}")
        http_failures.append((name, f"SSL: {e}"))
    except Exception as e:
        latency = (time.time() - t0) * 1000
        print(f"  [FAIL] {name:15s} ERROR: {type(e).__name__}: {e}")
        http_failures.append((name, str(e)))

    time.sleep(0.5)  # Be polite between requests

if http_failures:
    print(f"\n  ⚠️  {len(http_failures)} HTTP failures:")
    for name, reason in http_failures:
        print(f"    - {name}: {reason}")

# 5. Check pipeline_health table
print("\n═══ 5. PIPELINE HEALTH TABLE (last 5 per source) ═══")
import sqlite3
db_path = os.environ.get("DB_PATH", "kalshi_trades.db")
try:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    sources = conn.execute("SELECT DISTINCT source FROM pipeline_health ORDER BY source").fetchall()
    for (src,) in sources:
        rows = conn.execute("""
            SELECT status, markets_attempted, markets_returned, error_rate, avg_latency_ms, recorded_at
            FROM pipeline_health WHERE source = ? ORDER BY id DESC LIMIT 5
        """, (src,)).fetchall()
        statuses = [r['status'] for r in rows]
        print(f"\n  {src}:")
        for r in rows:
            print(f"    {r['recorded_at'][:19]}  {r['status']:10s}  "
                  f"attempted={r['markets_attempted']}  returned={r['markets_returned']}  "
                  f"err_rate={r['error_rate']:.2f}  latency={r['avg_latency_ms']:.0f}ms")

        # Check if this source would be disabled
        if len(statuses) >= 5 and all(s in ("broken", "degraded") for s in statuses):
            total_runs = conn.execute("SELECT COUNT(DISTINCT recorded_at) FROM pipeline_health").fetchone()[0]
            if total_runs % 10 == 0:
                print(f"    → Would be in RECOVERY CHECK mode (run #{total_runs})")
            else:
                print(f"    → ⚠️  DISABLED by feedback loop (5+ consecutive failures)")

    # Count total disabled sources
    disabled = []
    for (src,) in sources:
        rows = conn.execute(
            "SELECT status FROM pipeline_health WHERE source = ? ORDER BY id DESC LIMIT 5",
            (src,)
        ).fetchall()
        if len(rows) >= 5 and all(r[0] in ("broken", "degraded") for r in rows):
            disabled.append(src)

    if disabled:
        print(f"\n  ⚠️  CURRENTLY DISABLED SOURCES: {', '.join(disabled)}")
        print("  These sources are being SKIPPED entirely!")

    conn.close()
except Exception as e:
    print(f"  Error reading DB: {e}")

# 6. Check database writability
print("\n═══ 6. DATABASE WRITABILITY ═══")
try:
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS _diag_test (id INTEGER)")
    conn.execute("INSERT INTO _diag_test VALUES (1)")
    conn.execute("DROP TABLE _diag_test")
    conn.commit()
    conn.close()
    print(f"  [OK] Database is writable: {db_path}")
except sqlite3.OperationalError as e:
    print(f"  [FAIL] Database NOT writable: {e}")
    print(f"  Fix: chown kalshi:kalshi {db_path} && chmod 664 {db_path}")
except Exception as e:
    print(f"  [FAIL] Database error: {e}")

# 7. Check _cached_get behavior directly
print("\n═══ 7. SIMULATED _cached_get CALLS ═══")
print("  Testing if the actual bot code can fetch data...")
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import trade

    # Test FRED
    print(f"\n  FRED_API_KEY loaded by trade.py: {'YES' if trade.FRED_API_KEY else 'NO (empty)'}")
    if trade.FRED_API_KEY:
        result = trade.get_fred_latest("UNRATE")
        print(f"  FRED UNRATE latest: {result}")
    else:
        print("  ⚠️  FRED will NEVER work — API key not loaded into trade module")
        print(f"  os.environ has it: {'YES' if os.environ.get('FRED_API_KEY') else 'NO'}")
        print("  EXPLANATION: FRED_API_KEY is read at IMPORT TIME (line 1126).")
        print("  If .env wasn't loaded before 'import trade', the key is permanently empty.")

    # Test weather
    print(f"\n  Testing weather forecast (NYC)...")
    wx = trade.get_weather_forecast(40.71, -74.01)
    if wx:
        print(f"  Weather forecast: {str(wx)[:150]}")
    else:
        print("  ⚠️  Weather forecast returned None")

    # Test Polymarket
    print(f"\n  Testing Polymarket loader...")
    try:
        poly = trade._load_polymarket()
        print(f"  Polymarket markets loaded: {len(poly) if poly else 0}")
    except Exception as e:
        print(f"  Polymarket error: {e}")

except Exception as e:
    print(f"  Error importing trade: {e}")
    import traceback
    traceback.print_exc()

# Summary
print("\n" + "=" * 70)
print("  DIAGNOSIS SUMMARY")
print("=" * 70)
issues = []
if missing_keys:
    issues.append(f"Missing API keys: {', '.join(missing_keys)}")
if dns_failures:
    issues.append(f"DNS failures: {', '.join(dns_failures)}")
if http_failures:
    issues.append(f"HTTP failures: {', '.join(n for n,_ in http_failures)}")
if disabled:
    issues.append(f"Disabled by feedback loop: {', '.join(disabled)}")

if issues:
    print(f"\n  Found {len(issues)} issue(s):")
    for i, issue in enumerate(issues, 1):
        print(f"  {i}. {issue}")
    print("\n  RECOMMENDED FIXES:")
    if missing_keys:
        print("  - Add missing API keys to .env file")
    if dns_failures:
        print("  - Check /etc/resolv.conf — try adding: nameserver 8.8.8.8")
    if http_failures:
        print("  - Check firewall: ufw status, iptables -L")
        print("  - Install certs: apt install ca-certificates && update-ca-certificates")
    if disabled:
        print("  - Reset pipeline_health to clear death spiral:")
        sources_sql = "','".join(disabled)
        print(f"    sqlite3 {db_path} \"DELETE FROM pipeline_health WHERE source IN ('{sources_sql}');\"")
else:
    print("\n  All checks passed! Data sources should be working.")
