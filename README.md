# Graviton — EMA20 Momentum Pullback Bot

<p align="center">
  <img src="GravitonLogo.png" alt="Graviton" width="200">
</p>

Graviton trades crypto perpetuals on Kraken by hunting momentum pullbacks
to the EMA20. It scans for coins with strong intraday moves (4–18% / 24h),
confirms directional bias from the first 15m candle after session open,
then enters on 1m timeframe when price gravitates back to the EMA20 line
and shows a rejection signal.

- **Sessions:** NY (13:30–16:00 UTC) and Asia (00:00–02:00 UTC)
- **Exchange:** Kraken Perpetuals via CCXT (308 USD linear perps)
- **Leverage:** 1x Isolated
- **Mode:** Dry-Run by default (`DRY_RUN = True`)
- **No LLM. Pure Python. Deterministic signals.**

## Strategy

```
Scan (30m before) → Bias (15m candle) → Entry (1m EMA20 Pullback) → Exit (3 Levels)
```

### 1. Scan — Momentum Filter (30 min before session)

- 24h Change: 4% – 18%
- 24h Volume: > $750,000 USD
- Max 8 coins on watchlist
- Sorted by abs(change) descending

### 2. Bias — Directional Analysis (16 min after session open)

- **1 closed 15m candle** → LONG / SHORT / NOISE (previously: 2 candles / 31 min)
- Daily trend context: +6% on 24h + small red session candle = LONG pullback continuation
- RSI guard: RSI > 80 blocks LONG, RSI < 20 blocks SHORT
- **Earlier bias (16 min vs 31 min)** gives 15 min more entry time

### 3. Entry — EMA20 Pullback (1m chart, during session)

- **Candidate rotation:** all non-S/R-blocked candidates are polled in a 30s cycle — first with a valid pullback wins
- **Dynamic EMA distance:** based on 24h coin change
  - `< 5% daily move` → 0.50% distance
  - `5–10% daily move` → 0.60% distance
  - `> 10% daily move` → 1.00% distance
- Wait for price to pull back near EMA20 + confirm rejection candle
- **SL: 1H-ATR-based** (30% of 1H ATR, min 0.3%) — dynamic per coin volatility
  - Previously: fixed 0.20% from candle low → too tight for volatile coins
  - Now: `last_close * (1 - max(ATR_1h * 0.3, 0.3%))`
- 1 position per session
- Position size: ~$100 (17.5% of equity)

### 4. Exit — 3 Levels

| Level | Trigger | Action | SL After |
|-------|---------|--------|----------|
| **1/3** | Candlestick reversal pattern (e.g., Shooting Star, Engulfing) or +1% profit lock | Close 50% | Move SL to entry (break-even) + Trailing |
| **2/3** | EMA overextended (>2.5%), S/R reached, RSI extreme, Trailing Stop hit, or original SL | Close 100% | — |
| **3/3** | Session end (16:00 NY / 02:00 Asia UTC) | Force close all | — |

### Telegram Updates

Every key event is delivered live via Telegram:
```
🧠 [NY] Bias: 🟢 NEAR: LONG | RSI 58.3
👁 [NY] Entry-Polling NEAR (LONG) — every 30s
🎯 [DRY RUN] ENTRY LONG NEAR @ 1.923 | SL 1.919
📤 [DRY RUN] EXIT 50% LONG NEAR — Pattern: Shooting Star → SL to break-even
✅ [NY] Session Ende
```

## Project Structure

```
graviton/
├── session.py           # Full session runner (Bias → Entry → Watcher → Close)
├── config.py            # All parameters (sessions, filters, sizing, exit)
├── scanner.py           # Kraken Futures screener via CCXT
├── bias.py              # 15m directional bias (Wilder's RSI)
├── entry.py             # 1m EMA20 pullback entry + rejection detection
├── exit.py              # 3-level exit engine (Pattern / Structural / Session)
├── sr_levels.py         # Weekly/Daily support & resistance
├── patterns.py          # Candlestick pattern detection (ta-lib + pure fallback)
├── trader.py            # CCXT Kraken Futures order execution (open/SL/close)
├── watcher.py           # Position monitor + trailing stop
├── telegram_sender.py   # Direct Telegram Bot API for live updates
├── scripts/
│   ├── list_kraken_perps.py
│   ├── graviton_session_ny.sh  # nohup session runner (survives cron timeout)
│   ├── graviton_bias_ny.sh     # standalone bias analysis
│   └── scan_cron.sh
├── data/                # Runtime data (watchlist.json, entry state, debug logs)
├── logs/
├── requirements.txt
├── GravitonLogo.png
└── README.md
```

## Setup

```bash
# 1. Clone
git clone https://github.com/micimokus8/graviton.git
cd graviton

# 2. Create venv + install
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Configure API keys
cp .env.example .env
# Edit .env:
#   KRAKEN_API_KEY=***   KRAKEN_API_SECRET=***   EQUITY_USD=200

# 4. List perpetuals
python scripts/list_kraken_perps.py
# → 308 USD linear perps on Kraken Futures

# 5. Test scan
python scanner.py
```

## Usage

```bash
# Scanner only (no API keys needed)
python scanner.py

# List all Kraken perpetuals
python scanner.py --list-perps

# Full session (dry run — no real money)
python session.py ny

# Full session (Asia)
python session.py asia

# Standalone bias analysis
python -c "from bias import analyze_watchlist; ..."
```

### Live Mode

Set `DRY_RUN = False` in `config.py` to enable live trading.  
Start with dry-run first for at least one week.

## Cron Jobs (Hermes Agent)

```
Job              UTC     CEST    Schedule
───────────────  ──────  ──────  ────────
NY Bias          14:01   16:01   daily  → 16 min after open (1 × 15m candle)
NY Session       14:02   16:02   daily  → starts polling immediately after bias
NY Scan (pre)    13:00   15:00   daily  → writes watchlist for bias
Asia Scan        23:30   01:30   daily (paused)
Asia Session     00:00   02:00   daily (paused)
```

- **Scan** saves watchlist to `data/watchlist.json`
- **Bias** runs 16 min after session open (1 closed 15m candle)
- **Session** uses `nohup` to survive cron timeouts — Telegram messages are sent live
- Empty watchlist → session skips automatically

### Debug Logs

After each session, `data/session_debug.jsonl` contains one entry per coin per polling cycle:

```json
{"cycle":1, "base":"ZEC", "state":"approaching", "reason":"EMA distance 0.8% > dynamic max", "price":449.3, "dist":0.8}
{"cycle":5, "base":"ENA", "state":"at_ema", "reason":"At EMA (0.3%) — waiting for rejection", "price":0.0776, "dist":0.3}
{"cycle":240, "base":"SESSION", "state":"END", "reason":"NO_ENTRY — 5 coins, 240 cycles"}
```

If no entry occurs, a Telegram summary explains why for each candidate.

## Configuration

All parameters in `config.py`:

| Section | Key | Value | Description |
|---------|-----|-------|-------------|
| DRY_RUN | — | `True` | No live orders |
| SESSIONS | ny/asia | 13:30/00:00 | Session open/close times UTC |
| SCAN | min/max_change_pct | 4.0 / 18.0 | 24h change filter |
| SCAN | min_volume_eur | 750_000 | Min volume filter |
| BIAS | min_candles | 2 | Candles needed for bias |
| BIAS | rsi_long_max/short_min | 80 / 20 | RSI block thresholds |
| ENTRY | ema_period | 20 | EMA length |
| ENTRY | ema_distance_max | 0.50 | Base EMA distance (scaled dynamically by 24h change) |
| ENTRY | sl_offset_pct | 0.20 | Fallback SL offset (overridden by 1H ATR) |
| ENTRY | max_parallel_coins | 1 | Coins per session |
| SR | min_distance_pct | 0.50 | Min % to nearest S/R — fallback to best candidate if all blocked |
| POSITION | account_risk_pct_per_coin | 17.5 | ~$100 at $570 equity |
| EXIT | ema_overextended_pct | 2.50 | Structural exit trigger |
| EXIT | trailing_pct | 0.30 | Trailing stop distance |
| EXIT | rsi_extreme_long/short | 78 / 22 | RSI extreme exit |

### Recent Changes (July 2026)

| Change | Before | After | Reason |
|--------|--------|-------|--------|
| **Bias timing** | 31 min after open (2 candles) | **16 min** (1 candle) | 15 min more entry time |
| **Entry distance** | fixed 0.30% | **dynamic** 0.50/0.60/1.00% via 24h change | Strong trend days need wider distance |
| **SL calculation** | fixed 0.20% from candle low | **1H ATR × 30%** (min 0.3%) | Dynamic per coin volatility |
| **Polling** | single candidate, 2h loop | **candidate rotation**, 30s cycle | All candidates get equal chance |
| **S/R blocking** | hard block → session end | **fallback** to best candidate | Price moves during session |
| **Watcher (DRY_RUN)** | tracked phantom positions | simple PnL wait loop | No false exit Telegram messages |
| **Debug logging** | trade_log.jsonl only | **session_debug.jsonl** per cycle | Post-mortem: why no entry |
| **Session timeout** | cron kills after ~10 min | **nohup** background process | Session runs full duration |

## API Keys

Create Kraken Futures API keys at https://www.kraken.com/u/security/api

Required permission: **Futures Trading** (not Spot)

## Requirements

```
ccxt>=4.4.0
numpy>=1.26.0
python-dotenv>=1.0.0
TA-Lib>=0.4.28
```

## Disclaimer

This bot executes real trades. Use at your own risk.
Start with small capital (min. 100 USD, recommended 200 USD).
Always test with `DRY_RUN = True` first.