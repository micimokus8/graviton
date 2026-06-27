# Graviton — EMA20 Momentum Pullback Bot

Graviton trades crypto perpetuals on Kraken by hunting momentum pullbacks
to the EMA20. It scans for coins with strong intraday moves (4–18% / 24h),
confirms directional bias from the first 15m candles after session open,
then enters on 1m timeframe when price gravitates back to the EMA20 line
and shows a rejection signal.

- **Sessions:** NY (13:30–16:00 UTC) and Asia (00:00–02:00 UTC)
- **Exchange:** Kraken Perpetuals (CCXT)
- **Leverage:** 1x Isolated
- **No LLM. Pure Python. Deterministic signals.**

## Strategy

```
Scan (30m before) → Bias (15m) → Entry (1m EMA20 Pullback) → Exit (3 ways)
```

1. **Scan:** Find coins with 4–18% 24h change, >750K EUR volume
2. **Bias:** First 2–3 15m candles after open → LONG/SHORT/NOISE
3. **Entry:** Wait for pullback to EMA20 (1m), confirm rejection candle
4. **Exit:** Pattern (50%), Structural (100%), Session End (100%)

## Project Structure

```
graviton/
├── main.py          # Session Orchestrator
├── config.py        # All Parameters
├── scanner.py       # Kraken Screener (CCXT)
├── bias.py          # 15m Directional Bias
├── entry.py         # EMA20 Pullback Entry
├── sr_levels.py     # Support/Resistance
├── patterns.py      # Candlestick Detection
├── exit.py          # 3 Exit Paths
├── trader.py        # Order Execution
├── watcher.py       # Position Monitor
├── scripts/
│   └── list_kraken_perps.py
├── logs/
├── requirements.txt
└── README.md
```

## Setup

```bash
# 1. Clone
git clone https://github.com/micimokus8/graviton.git
cd graviton

# 2. Install dependencies
pip install -r requirements.txt

# Optional: TA-Lib system library (for candlestick patterns)
# Ubuntu: sudo apt-get install ta-lib
# macOS:  brew install ta-lib

# 3. Configure API keys
cp .env.example .env
# Edit .env with your Kraken Futures API keys
# KRAKEN_API_KEY=your_key_here
# KRAKEN_API_SECRET=your_secret_here
# EQUITY_USD=200

# 4. List available perpetuals
python scripts/list_kraken_perps.py

# 5. Run scan (dry run, no API keys needed)
python scanner.py
```

## Usage

```bash
# Run scanner only (no API keys needed)
python scanner.py

# List all Kraken perpetuals
python scanner.py --list-perps
python scripts/list_kraken_perps.py

# Run one session (dry run)
python main.py --once --dry-run

# Run continuously (waits for next session)
python main.py

# Run with specific session
python main.py --once --ny
python main.py --once --asia
```

## Configuration

All parameters in `config.py`:

- `SCAN`: Change filters, volume threshold, max watchlist
- `BIAS`: 15m candle count, RSI limits
- `ENTRY`: EMA period, smoothing, distance max, stair steps
- `SR`: Lookback weeks, min distance for entry
- `POSITION`: Risk per coin, step distribution
- `EXIT`: EMA overextended, trailing %, RSI extremes
- `SESSIONS`: NY/Asia times

## API Keys

Create Kraken Futures API keys at https://www.kraken.com/u/security/api

Required permissions: **Futures Trading** (not Spot)

## Disclaimer

This bot executes real trades. Use at your own risk.
Start with small capital (min. 100 USD, recommended 200 USD).
Always test with `--dry-run` first.