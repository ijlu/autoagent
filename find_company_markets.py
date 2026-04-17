#!/usr/bin/env python3
"""Find all company KPI markets on Kalshi based on known market names."""
import os, sys
sys.path.insert(0, '.')
os.chdir(os.path.expanduser('~/autoagent'))
for line in open('.env'):
    line = line.strip()
    if '=' in line and not line.startswith('#'):
        k, v = line.split('=', 1)
        os.environ[k.strip()] = v.strip()

from trade import api_get

# Based on screenshots, try every plausible series ticker format
# Pattern variations: KXCOMPANY, KXTICKERMETRIC, company name, etc.
candidates = []

# KPI markets seen in screenshots
kpi_companies = {
    # (name_variants, metric_variants)
    'boeing':    (['KXBOEING', 'KXBA', 'KXBOEINGDEL', 'KXBOEINGDELIVERIES'], 'deliveries'),
    'spotify':   (['KXSPOTIFY', 'KXSPOT', 'KXSPOTIFYMAU', 'KXSPOTIFYUSERS'], 'mau'),
    'uber':      (['KXUBER', 'KXUBERTRIPS', 'KXUBERRIDES'], 'trips'),
    'meta':      (['KXMETA', 'KXMETAHEADCOUNT', 'KXMETAHC', 'KXMETADAP', 'KXMETADAU'], 'headcount'),
    'robinhood': (['KXHOOD', 'KXROBINHOOD', 'KXHOODGOLD', 'KXROBINHOODGOLD'], 'subscribers'),
    'doordash':  (['KXDASH', 'KXDOORDASH', 'KXDOORDASHORDERS', 'KXDASHORDERS'], 'orders'),
    'lyft':      (['KXLYFT', 'KXLYFTRIDES', 'KXLYFTTOTAL'], 'rides'),
    'match':     (['KXMTCH', 'KXMATCH', 'KXMATCHPAYERS', 'KXMATCHGROUP'], 'payers'),
    'palantir':  (['KXPLTR', 'KXPALANTIR', 'KXPLTRCUST', 'KXPALANTIRCUSTOMERS'], 'customers'),
    'ferrari':   (['KXFERRARI', 'KXRACE', 'KXFERRARISHP', 'KXFERRARISHIPMENTS'], 'shipments'),
    'zyn':       (['KXZYN', 'KXZYNSHIP', 'KXZYNVOLUME', 'KXPM', 'KXPHILIPMORRIS'], 'shipments'),
    'airbnb':    (['KXABNB', 'KXAIRBNB', 'KXAIRBNBBOOKINGS', 'KXABNBBOOKINGS'], 'bookings'),
    'tesla':     (['KXTESLA', 'KXTSLA', 'KXTESLADEL', 'KXTESLAPROD', 'KXTESLASEMI',
                   'KXTESLADELIVERIES', 'KXTESLAGROW', 'KXTESLAENERGY'], 'deliveries'),
    'spacex':    (['KXSPACEX', 'KXSPACEXLAUNCH', 'KXSPACEXIPO'], 'launches'),
    'netflix':   (['KXNFLX', 'KXNETFLIX', 'KXNFLXSUB'], 'subscribers'),
    'apple':     (['KXAAPL', 'KXAPPLE', 'KXIPHONE', 'KXAPPLEREV', 'KXAPPLECEO'], 'revenue'),
    'nvidia':    (['KXNVDA', 'KXNVIDIA'], 'revenue'),
    'amazon':    (['KXAMZN', 'KXAMAZON'], 'revenue'),
    'microsoft': (['KXMSFT', 'KXMICROSOFT'], 'revenue'),
    'google':    (['KXGOOG', 'KXGOOGLE', 'KXGOOGL', 'KXALPHABET'], 'revenue'),
    'openai':    (['KXOPENAI', 'KXOPENAIIPO', 'KXAGI'], 'ipo'),
    'discord':   (['KXDISCORD', 'KXDISCORDIPO'], 'ipo'),
    'stripe':    (['KXSTRIPE', 'KXSTRIPEIPO'], 'ipo'),
    'anthropic': (['KXANTHROPIC', 'KXANTHROPICIPO'], 'ipo'),
    'spacexipo': (['KXSPACEXIPO', 'KXSPACEXPUBLIC'], 'ipo'),
    'starlink':  (['KXSTARLINK', 'KXSTARLINKIPO'], 'ipo'),
    'anduril':   (['KXANDURIL', 'KXANDURILIPO'], 'ipo'),
    'costco':    (['KXCOSTCO', 'KXCOST'], 'price'),
    'ism':       (['KXISM', 'KXISMPMI', 'KXPMI'], 'pmi'),
}

# Also try earnings mention patterns
earnings_tickers = [
    'TSLA', 'NFLX', 'META', 'AAPL', 'NVDA', 'AMZN', 'MSFT', 'GOOG', 'GOOGL',
    'BA', 'DIS', 'JPM', 'GS', 'UBER', 'LYFT', 'DASH', 'SPOT', 'PLTR',
    'HOOD', 'ABNB', 'COIN', 'CRM', 'SNAP', 'PINS', 'RBLX',
]

hits = []

print("=== Checking KPI series tickers ===")
for company, (tickers, metric) in kpi_companies.items():
    for t in tickers:
        try:
            resp = api_get(f'/markets?limit=3&status=open&series_ticker={t}')
            mkts = resp.get('markets', [])
            if mkts:
                m = mkts[0]
                vol = m.get('volume_fp') or m.get('volume') or 0
                print(f"  HIT: {t} ({company}/{metric}) -> {len(mkts)} mkts, vol={vol}")
                print(f"       title={m.get('title','')[:80]}")
                hits.append(t)
                break  # found this company, skip other variants
        except:
            pass

print(f"\n=== Checking earnings mention series ===")
for sym in earnings_tickers:
    for prefix in [f'KXEARNINGSMENTIO{sym}', f'KXEARNINGSMENTIONN{sym}',
                   f'KXEARNINGMENTION{sym}', f'KXEARNINGS{sym}']:
        try:
            resp = api_get(f'/markets?limit=3&status=open&series_ticker={prefix}')
            mkts = resp.get('markets', [])
            if mkts:
                m = mkts[0]
                vol = m.get('volume_fp') or m.get('volume') or 0
                print(f"  HIT: {prefix} -> {len(mkts)} mkts, vol={vol}")
                print(f"       title={m.get('title','')[:80]}")
                hits.append(prefix)
                break
        except:
            pass

# Also try event_ticker format for some known markets
print(f"\n=== Checking event tickers ===")
event_guesses = [
    'KXTESLA-26-Q2', 'KXTESLA-26', 'KXBOEINGDEL-26-Q1',
    'KXSPOTIFYMAU-26-Q1', 'KXUBERTRIPS-26-Q1',
    'KXMETAHC-26-Q1', 'KXMETAHEADCOUNT-26-Q1',
    'KXHOODGOLD-26-Q1', 'KXDASHTOTAL-26-Q1',
    'KXLYFTRIDES-26-Q1', 'KXPLTRCUST-26-Q1',
    'KXFERRARI-26-Q1', 'KXZYN-26-Q1', 'KXABNB-26-Q1',
    'KXAIRBNB-26-Q1', 'KXAIRBNBBOOKINGS-26-Q1',
]
for e in event_guesses:
    try:
        resp = api_get(f'/markets?limit=3&status=open&event_ticker={e}')
        mkts = resp.get('markets', [])
        if mkts:
            m = mkts[0]
            print(f"  EVENT HIT: {e} -> series={m.get('series_ticker','')} title={m.get('title','')[:70]}")
            if m.get('series_ticker') and m['series_ticker'] not in hits:
                hits.append(m['series_ticker'])
    except:
        pass

print(f"\n{'='*60}")
print(f"TOTAL: {len(hits)} active series found")
for h in hits:
    print(f"  '{h}',")
