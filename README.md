# Graviton — EMA20 Momentum Pullback Bot

<p align="center">
  <img src="GravitonLogo.png" alt="Graviton" width="200">
</p>

Graviton trades crypto perpetuals on Kraken by hunting momentum at
the NY session open. It scans for coins with strong intraday moves (3%+ / 24h),
determines directional bias from 4H/1H/15m EMA structure, then enters on a
5m EMA20 pullback confirmed by a closed rejection candle.

- **Sessions:** NY (13:30–16:00 UTC) and Asia (00:00–02:00 UTC)
- **Exchange:** Kraken Perpetuals via CCXT (308 USD linear perps)
- **Leverage:** 1x Isolated
- **Mode:** Dry-Run by default (`DRY_RUN = True`)
- **No LLM. Pure Python. Deterministic signals.**

## Strategy

```
Scan → Bias (4H/1H/15m, 2-of-3) → Entry (5m EMA20 pullback) → Exit
```

### 1. Scan — Momentum Filter (30 min before session)

- 24h Change: 3%+ (min 3%, no upper limit — Futures ticker differs from spot)
- 24h Volume: > $750,000 USD (Ticker-based; OHLCV validation after volume filter)
- Max 8 coins on watchlist
- Sorted by abs(change) descending

### 2. Bias — Multi-Timeframe EMA Structure (13:45 UTC)

The bias is calculated by the bias cron from closed candles on 4H, 1H and 15m.
At least two of the three timeframes must agree:

- **LONG:** at least 2× BULLISH
- **SHORT:** at least 2× BEARISH
- Otherwise: **NOISE** and no entry permission
- Session volume is evaluated in the bias stage; it is not re-filtered in the entry engine
- The session reads the bias snapshot and does not recalculate bias independently

### 3. Entry — 5m EMA20 Pullback (during the bounded bias window)

There is only one active entry mode: a confirmed 5m EMA20 pullback. The former
RSI-based Fast Entry was removed because it bypassed rejection confirmation.

Entry requires:

1. Current price is within the dynamic EMA20 distance band.
2. One of the last three fully closed 5m candles confirms rejection:
   - LONG: bullish candle with its low near EMA20
   - SHORT: bearish candle with its high near EMA20
3. The current price is still close enough to EMA20; the old rejection close is
   not used as the fill price. Execution uses the current market price.

Entry behavior:

- No new entry is allowed after **20 minutes from the bias file timestamp** or after session close.
- All active candidates are checked every 30 seconds in each cycle.
- The cycle checks every candidate first, then prioritizes the currently closest EMA20 signal.
- 3/3 vs. 2/3 bias strength is retained as the stable tie-breaker.
- S/R is checked before rotation and again at the actual entry price.
- BTC 1m counter-trend correlation can skip an otherwise valid candidate.
- Volume is handled by scan/bias, not duplicated as an entry filter.
- Initial SL: `max(1.0 × 1H ATR, 0.9%)`.
- One position per session, with the configured exposure limit.

### 4. Exit — 3 Levels

| Level | Trigger | Action | SL After |
|-------|---------|--------|----------|
| **1/3** | Candlestick reversal pattern (e.g., Shooting Star, Engulfing) or +1% profit lock | Close 50% | Move SL to entry (break-even) + Trailing |
| **2/3** | EMA overextended (>2.5%), S/R reached, RSI extreme, Trailing Stop hit, or original SL | Close 100% | — |
| **3/3** | Session end (16:00 NY / 02:00 Asia UTC) | Force close all | — |

### Telegram Updates

Every key event is delivered live via Telegram. DRY_RUN includes simulated exits with PnL at session end:

```
🧠 [NY] Bias: 🟢 AAVE: LONG | RSI 67.9
👁 [NY] Entry-Polling — 3 candidates (30s rotation)
🎯 [DRY RUN] ENTRY LONG AAVE @ 96.11 | SL 94.64 | Pullback: 5m Rejection an EMA
📤 [DRY RUN] EXIT 100% LONG AAVE @ session_end | PnL: 🟢 +4.85%
✅ [NY] Session Ende
```

## Project Structure

```
graviton/
├── session.py           # Full session runner (Bias → Entry → Watcher → Close)
├── config.py            # All parameters (sessions, filters, sizing, exit)
├── scanner.py           # Kraken Futures screener via CCXT
├── bias.py              # 4H/1H/15m multi-timeframe bias (2-of-3)
├── entry.py             # 5m EMA20 pullback + rejection entry
├── exit.py              # 3-level exit engine (Pattern / Structural / Session)
├── sr_levels.py         # Weekly/Daily support & resistance
├── patterns.py          # Candlestick pattern detection (ta-lib + pure fallback)
├── trader.py            # CCXT Kraken Futures order execution (open/SL/close)
├── watcher.py           # Position monitor + trailing stop
├── telegram_sender.py   # Direct Telegram Bot API for live updates
├── scripts/
│   ├── list_kraken_perps.py
│   ├── graviton_session_ny.sh  # nohup session runner
│   ├── graviton_bias_ny.sh     # standalone bias output
│   └── scan_cron.sh
├── data/                # Runtime data (watchlist, bias, entry state, debug logs)
├── logs/
├── requirements.txt
├── GravitonLogo.png
└── README.md
```

## Setup

```bash
git clone https://github.com/micimokus8/graviton.git
cd graviton
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env: KRAKEN_API_KEY, KRAKEN_API_SECRET, EQUITY_USD
python scripts/list_kraken_perps.py
python scanner.py
```

## Usage

```bash
python scanner.py                          # Scan only (no API key needed)
python scanner.py --list-perps             # List all Kraken perps
python session.py ny                       # Full NY session (dry run)
python session.py asia                     # Full Asia session (dry run)
```

### Live Mode

Set `DRY_RUN = False` in `config.py`. Start with dry-run for at least one week.

## Cron Jobs (Hermes Agent)

```
Job              UTC     CEST    Description
───────────────  ──────  ──────  ───────────────────────────────
NY Scan (pre)    13:30   15:30   Writes watchlist for bias
NY Bias          13:45   15:45   4H/1H/15m bias (2-of-3)
NY Session       13:50   15:50   Polling starts; entries limited to 20 min after bias
Asia Scan        23:30   01:30   Paused
Asia Session     00:00   02:00   Paused
```

- **Scan** saves watchlist to `data/watchlist.json`
- **Bias** runs at 13:45 UTC and writes the multi-timeframe snapshot
- **Entry** uses only the bias snapshot; no independent bias recalculation
- **Session** uses `nohup` to survive cron timeouts
- Empty watchlist → session skips automatically

### Debug Logs

`data/session_debug.jsonl` contains one entry per coin per polling cycle:

```json
{"cycle":1, "base":"FARTCOIN", "state":"no_entry", "reason":"Preis -0.06% unter EMA20 — kein LONG"}
{"cycle":2, "base":"AAVE", "state":"entered", "reason":"Pullback: 5m Rejection an EMA (0.17%, 0 Kerze(n) zurück)"}
```

If no entry occurs, a Telegram summary explains why for each candidate.

## Configuration

| Section | Key | Value | Description |
|---------|-----|-------|-------------|
| DRY_RUN | — | `True` | No live orders |
| SESSIONS | ny/asia | 13:30/00:00 | Session open/close (UTC) |
| SCAN | min/max_change_pct | 3.0 / 99.0 | 24h change filter (upper limit effectively disabled) |
| SCAN | min_volume_eur | 500_000 | Min 24h volume |
| BIAS | min_candles | 4 | Candles needed for bias |
| BIAS | min_session_vol_ratio | 0.7 | Minimum session-volume ratio |
| BIAS | rsi_long_max/short_min | 80 / 20 | RSI bias block |
| ENTRY | ema_distance_max | 0.50 | Base EMA distance (scaled by 24h change) |
| ENTRY | sl_offset_pct | 0.90 | Minimum initial SL; effective SL is max(1.0× 1H ATR, 0.9%) |
| ENTRY | ema_period | 20 | EMA length |
| SR | min_distance_pct | 0.50 | S/R entry block (fallback if all blocked) |
| POSITION | account_risk_pct_per_coin | 17.5 | ~$100 at $570 equity |
| EXIT | ema_overextended_pct | 2.50 | EMA structural exit |
| EXIT | trailing_pct | 0.30 | Trailing stop distance |
| EXIT | rsi_extreme_long/short | 78 / 22 | RSI extreme exit |

### Change Log (July 2026)

| Change | Before | After | Reason |
|--------|--------|-------|--------|
| **Bias timing** | Session-momentum snapshot | **4H/1H/15m, 2-of-3** | Multi-timeframe directional confirmation |
| **Entry rotation** | Single coin, 2h loop | **All candidates, 30s cycle** | Closest valid EMA20 signal first |
| **Entry mode** | Fast Entry + rejection modes | **5m closed-candle rejection only** | No blind RSI entry |
| **Entry window** | Until session close | **20 min after bias, capped by close** | Prevent stale-bias entries |
| **SL** | 0.30× 1H ATR, min 0.3% | **1.0× 1H ATR, min 0.9%** | Wider SL for pullback noise |
| **EMA distance** | Fixed 0.30% | **Dynamic 0.50-1.00%** | Adapt to volatility |
| **S/R blocking** | Hard block → session end | **Fallback to best candidate** | Price moves during session |
| **Debug logging** | trade_log.jsonl only | **session_debug.jsonl** per cycle | Post-mortem analysis |
| **DRY_RUN exit** | None (phantom positions) | **Simulated exit at session end** | See PnL of every trade |
| **Session timeout** | Cron kills after ~10 min | **nohup background process** | Full duration guaranteed |

## API Keys

Kraken Futures API keys at https://www.kraken.com/u/security/api  
Required: **Futures Trading** permission (not Spot).

## Requirements

```
ccxt>=4.4.0
numpy>=1.26.0
python-dotenv>=1.0.0
TA-Lib>=0.4.28
```

## Disclaimer

This bot executes real trades. Use at your own risk.  
Start with small capital (min. 100 USD). Always test with `DRY_RUN = True` first.