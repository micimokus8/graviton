#!/bin/bash
set -o pipefail
# Graviton Bias Cron — NY (16:01 DE) / Asia (00:01 DE)
# Läuft bias.py im Hintergrund (kein Warten auf Session nötig — bias.py ist instant)
cd /root/.hermes/workspace/graviton || exit 1
LOG="logs/bias_${1:-ny}.log"
mkdir -p logs
echo "[$(date -u '+%H:%M UTC')] Bias $1" >> "$LOG"
.venv/bin/python3 -c "
import sys, json
sys.path.insert(0, '.')
from config import CFG, SESSIONS, LLM_BIAS_ENABLED
from bias import analyze_watchlist
from atomic_json import atomic_write_json
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

results = analyze_watchlist(symbols, open_ts, include_features=LLM_BIAS_ENABLED)
candidates = [r for r in results if r.bias in ('LONG', 'SHORT')]
print(f'{len(candidates)} Kandidaten von {len(results)}')
for r in results:
    print(f'  {r.bias} {r.symbol} | {r.reason}')
# Save FULL bias result (alle Coins, inkl. NOISE) für Session
bias_out = [{'symbol': r.symbol, 'bias': r.bias, 'price': r.session_open_price,
             'reason': r.reason, 'green': r.green_candles, 'red': r.red_candles,
             'signal_count': r.signal_count,
             'session_vol_ratio': r.session_vol_ratio,
             'regime': r.regime,
             'deterministic_bias': r.bias,
             'llm_enabled': LLM_BIAS_ENABLED,
             'llm_bias': 'DISABLED', 'llm_confidence': 0.0,
             'llm_fallback': False, 'effective_bias': r.bias,
             'features': r.features}
            for r in results]
atomic_write_json(data_dir / 'bias_result.json', bias_out)
" 2>&1 | tee -a "$LOG"

# Optional LLM shadow enrichment is isolated from deterministic bias creation.
# Read the already parsed .env-backed value from config.py; do not require the
# caller/Cron to export LLM_BIAS_ENABLED separately.
LLM_ENABLED_FROM_CONFIG="$(.venv/bin/python3 -c 'from config import LLM_BIAS_ENABLED; print(str(LLM_BIAS_ENABLED).lower())' 2>/dev/null || printf 'false')"
if [[ "$LLM_ENABLED_FROM_CONFIG" == "true" ]]; then
  timeout --signal=TERM --kill-after=5s 40s .venv/bin/python3 shadow_runner.py data/bias_result.json >> "$LOG" 2>&1
  status=$?
  if [ "$status" -ne 0 ]; then
    echo "[$(date -u '+%H:%M UTC')] LLM shadow skipped/failed (status=$status); deterministic bias bleibt aktiv" >> "$LOG"
  fi
fi

# Market Breadth Sentiment (log-only Beobachtung, kein Filter).
# Schreibt data/market_sentiment.json als Kontext für Trade-Logs.
# Fail-soft: Fehler blockiert weder Bias noch Session.
timeout --signal=TERM --kill-after=5s 90s .venv/bin/python3 breadth.py >> "$LOG" 2>&1
status=$?
if [ "$status" -ne 0 ]; then
  echo "[$(date -u '+%H:%M UTC')] Breadth-Scan skipped/failed (status=$status); Trade-Logs ohne Sentiment-Kontext" >> "$LOG"
fi