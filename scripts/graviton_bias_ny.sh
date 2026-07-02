#!/bin/bash
# Graviton Bias Cron — NY (16:01 DE) / Asia (00:01 DE)
# Läuft bias.py im Hintergrund (kein Warten auf Session nötig — bias.py ist instant)
cd /root/.hermes/workspace/graviton || exit 1
LOG="logs/bias_${1:-ny}.log"
mkdir -p logs
echo "[$(date -u '+%H:%M UTC')] Bias $1" >> "$LOG"
.venv/bin/python3 -c "
import sys, json
sys.path.insert(0, '.')
from config import CFG, SESSIONS
from bias import analyze_watchlist
from datetime import datetime, timezone

data_dir = __import__('pathlib').Path('data')
wl_file = data_dir / 'watchlist.json'
if not wl_file.exists():
    print('Keine Watchlist')
    sys.exit(0)

with open(wl_file) as f:
    watchlist = json.load(f)

symbols = [w['symbol'] for w in watchlist]
session = SESSIONS['${1:-ny}']
h, m = map(int, session['open'].split(':'))
open_dt = datetime.now(timezone.utc).replace(hour=h, minute=m, second=0)
open_ts = int(open_dt.timestamp() * 1000)

results = analyze_watchlist(symbols, open_ts)
candidates = [r for r in results if r.bias in ('LONG', 'SHORT')]
print(f'{len(candidates)} Kandidaten von {len(results)}')
for r in candidates:
    print(f'  {r.bias} {r.symbol} | RSI {r.rsi_15m} | {r.reason}')
# Save bias result
bias_out = [{'symbol': r.symbol, 'bias': r.bias, 'price': r.session_open_price,
             'rsi': r.rsi_15m, 'reason': r.reason} for r in candidates]
with open(data_dir / 'bias_result.json', 'w') as f:
    json.dump(bias_out, f, indent=2)
" 2>&1 | tee -a "$LOG"