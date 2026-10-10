# Graviton — EMA20 Momentum Pullback Bot

<p align="center">
  <img src="GravitonLogo.png" alt="Graviton" width="200">
</p>

Graviton trades crypto perpetuals on Kraken by hunting momentum at
the NY session open. It scans for coins with strong intraday moves (3%+ / 24h),
determines directional bias from 4H/1H/15m EMA structure, then enters on a
5m EMA20 pullback confirmed by a closed rejection candle.

- **Sessions:** NY (12:50–16:00 UTC polling; Asia paused)
- **Exchange:** Kraken Perpetuals via CCXT (USD linear perps)
- **Leverage:** 1x Isolated
- **Mode:** **LIVE** since 27.09.2026 (`DRY_RUN = False`)
- **LLM:** optional shadow-only bias (observation; never controls trades)
- **Sentiment context:** 1H+4H market-breadth scan, log-only (no filter yet)

## Strategy

```
Scan → Bias (4H/1H/15m, 2-of-3) → Entry (5m EMA20 pullback) → Exit
                        ↓
              Market Breadth 1H+4H (log-only, Trade-Log-Kontext)
```

### 1. Scan — Momentum Filter (~30 min before session)

- 24h Change: 3%+ (no upper limit — Futures ticker differs from spot)
- 24h Volume: > $500,000 USD (Ticker-based; OHLCV validation after volume filter)
- Max 8 coins on watchlist, sorted by abs(change) descending

### 2. Bias — Multi-Timeframe EMA Structure (12:45 UTC)

The bias is calculated by the bias cron from closed candles on 4H, 1H and 15m.
At least two of the three timeframes must agree:

- **LONG:** at least 2× BULLISH
- **SHORT:** at least 2× BEARISH
- Otherwise: **NOISE** and no entry permission
- 4H exhaustion check demotes stale trends to NEUTRAL
- Session volume is evaluated in the bias stage; it is not re-filtered in the entry engine
- The session reads the bias snapshot and does not recalculate bias independently

**LLM shadow (optional):** `shadow_runner.py` asks the LLM for a second opinion
per coin and writes it to `bias_shadow_history.jsonl` for weekly evaluation.
`effective_bias` always stays deterministic — the LLM never authorizes, blocks
or changes a trade.

**Market breadth (log-only):** `breadth.py` scans BTC/ETH/SOL + top-20 perps
on **1H + 4H** at bias time and writes `data/market_sentiment.json`
(LONG / SHORT / CHOPPY). Every trade-log entry carries this sentiment for
later evaluation. **It does not filter trades yet** — the historical backtest
(`backtest_breadth.py`) decides whether it will become a filter.

### 3. Entry — 5m EMA20 Pullback (during the bounded bias window)

There is only one active entry mode: a confirmed 5m EMA20 pullback.

Entry requires:

1. Current price is within the dynamic EMA20 distance band
   (0.50 % base, scaled by 24h change up to 1.50 %).
2. One of the last three fully closed 5m candles confirms rejection:
   - LONG: bullish candle with its low near EMA20
   - SHORT: bearish candle with its high near EMA20
3. The current price is still close enough to EMA20; the old rejection close is
   not used as the fill price. Execution uses the current market price.

Entry behavior:

- No new entry after **30 minutes from the bias file timestamp** or after session close.
- All active candidates are checked every 30 seconds in each cycle.
- **Candidate sorting (since 10.10.):** signals are sorted by **bias strength first**
  (3/3 before 2/3), EMA distance only as tie-breaker — a weak 2/3 pullback can
  no longer beat a strong 3/3 signal (ZRO 10.10. regression case).
- S/R is checked before rotation and again at the actual entry price.
- BTC 1m counter-trend correlation can skip an otherwise valid candidate.
- Volume is handled by scan/bias, not duplicated as an entry filter.
- Initial SL: `min(max(1.0 × 1H ATR, 0.9%), 2.5%)`.
- One position per session, with the configured exposure limit.

### 4. Exit — LIVE uses the "dry-run parity" engine

In LIVE, only the conservative exit mechanisms are active
(`structural_exits = False`, `pattern_exit_50 = False` — same as the DRY_RUN sim):

| Level | Trigger | Action |
|-------|---------|--------|
| **1/3** | Profit lock at +1.05% | Close 50%, rest → trailing stop |
| **2/3** | Trailing stop hit (0.5%) or initial SL | Close 100% |
| **3/3** | Session end (16:00 UTC) | Force close all |

Every exit reports the **real Kraken fill price** and the account balance —
never the signal price (phantom-exit bug fixed, Oct 2026).

### Telegram Updates

Every key event is delivered live via Telegram. Exits include real fill price,
PnL and account balance:

```
🧠 [NY] Bias: 🟢 NEAR: LONG (3/3) 4H:BULLISH 1H:BULLISH 15m:BULLISH
🎯 [LIVE] ENTRY LONG NEAR @ 0.493 | SL 0.487 | Pullback: 5m Rejection an EMA
📤 [LIVE] EXIT 100% SHORT ZRO @ 1.989100 | PnL: 🔴 -1.55% | Konto: 564.67 USD
✅ [NY] Session Ende — 16:00 UTC
```

## Project Structure

```
graviton/
├── session.py           # Full session runner (Bias → Entry → Watcher → Close)
├── config.py            # All parameters (sessions, filters, sizing, exit)
├── scanner.py           # Kraken Futures screener via CCXT
├── bias.py              # 4H/1H/15m multi-timeframe bias (2-of-3)
├── entry.py             # 5m EMA20 pullback + rejection entry
├── exit.py              # 3-level exit engine (SL / Profit-Lock / Trailing / Session)
├── sr_levels.py         # Weekly/Daily support & resistance
├── patterns.py          # Candlestick pattern detection (ta-lib + pure fallback)
├── trader.py            # CCXT Kraken Futures order execution (open/SL/close)
├── watcher.py           # Position monitor + trailing stop
├── telegram_sender.py   # Direct Telegram Bot API for live updates
├── breadth.py           # Market breadth sentiment 1H+4H (log-only)
├── backtest_breadth.py  # Historical breadth evaluation of LIVE trades
├── llm_bias.py          # Optional LLM shadow bias (observation only)
├── shadow_runner.py     # Out-of-process LLM shadow enrichment + history
├── candidate_ranking.py # Deterministic ranking + LLM shadow ranking
├── feature_snapshot.py  # Deterministic technical features for LLM shadow
├── volume_metrics.py    # Session-volume ratio helpers
├── candle_utils.py      # Closed-candle OHLCV helpers (shared)
├── atomic_json.py       # Atomic JSON writes (state files)
├── scripts/
│   ├── list_kraken_perps.py
│   ├── graviton_scan_ny.sh      # scan cron
│   ├── graviton_bias_ny.sh      # bias cron + shadow + breadth
│   ├── graviton_session_ny.sh   # session cron (nohup)
│   └── scan_cron.sh
├── data/                # Runtime data (watchlist, bias, entry state, logs)
├── logs/
├── tests/               # pytest suite (112 tests)
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
python session.py ny                       # Full NY session (LIVE)
python session.py asia                     # Full Asia session (currently paused)
python breadth.py                          # Market sentiment scan (log-only)
python backtest_breadth.py                 # Breadth backtest over LIVE trades
python backtest_breadth.py --tfs=1h,4h,1d  # Optionally include 1D (default: 1h,4h)
python shadow_runner.py data/bias_result.json  # LLM shadow enrichment
python -m pytest tests/ -q                 # Run the test suite
```

### Live Mode

`DRY_RUN = False` in `config.py` (current). The bot places real Kraken Futures
orders from the NY session. SL is a hard exchange stop-order (`reduceOnly`,
mark-triggered); take-profit is watcher-side only (if the watcher dies, the SL
still protects, but there is no exchange-side TP).

## Cron Jobs (Hermes Agent)

```
Job              UTC     CEST*   Description
───────────────  ──────  ──────  ───────────────────────────────
NY Scan (pre)    12:30   14:30   Writes watchlist for bias
NY Bias          12:45   14:45   4H/1H/15m bias → shadow → breadth
NY Session       12:50   14:50   Polling; entries limited to 30 min after bias
Asia Scan        23:30   01:30   Paused
Asia Session     00:00   02:00   Paused
```

*CEST during summer time; schedules are UTC-internally consistent and shift
±1h in local time on DST boundaries (winter-time reminder cron on 24.10.).

- **Scan** saves watchlist to `data/watchlist.json`
- **Bias** runs at 12:45 UTC, writes the multi-timeframe snapshot, then runs
  LLM shadow enrichment and the market-breadth scan (both fail-soft)
- **Entry** uses only the bias snapshot; no independent bias recalculation
- **Session** runs as a background process so it survives cron timeouts
- Empty watchlist → session skips automatically

### Trade Log

`data/trade_log.jsonl` records every entry and exit with context:

```json
{"event":"entry","symbol":"NEAR/USD:USD","bias":"LONG","signal_count":3,
 "dist_pct":0.493,"market_sentiment":"LONG","market_breadth":"1H:15B/0S 4H:16B/1S"}
{"event":"exit","base":"ZRO","reason":"stop_loss","close_pct":100,
 "pnl_pct":-1.55,"market_sentiment":"CHOPPY", ...}
```

`data/session_debug.jsonl` contains one entry per coin per polling cycle:

```json
{"cycle":1, "base":"ZRO", "bias":"SHORT", "state":"entered", "dist":0.114}
```

If no entry occurs, a Telegram summary explains why for each candidate.

## Configuration

| Section | Key | Value | Description |
|---------|-----|-------|-------------|
| DRY_RUN | — | `False` | **LIVE** trading |
| SESSIONS | ny/asia | 12:30/00:00 open, 16:00/02:00 close | Session times (UTC); cron starts polling ~12:50 |
| SCAN | min_change_pct | 3.0 | 24h change filter |
| SCAN | min_volume_eur | 500_000 | Min 24h volume |
| BIAS | min_candles | 4 | Candles needed for bias |
| BIAS | min_session_vol_ratio | 0.7 | Minimum session-volume ratio |
| ENTRY | ema_distance_max | 0.50 | Base EMA distance (dynamically scaled to 1.50) |
| ENTRY | sl_offset_pct | 0.90 | Minimum initial SL (of the effective SL formula) |
| ENTRY | sl_cap_pct | 2.50 | Max SL distance (protects against ATR spikes) |
| ENTRY | max_stair_steps | 1 | Only the first pullback per session |
| ENTRY | max_parallel_coins | 1 | One position per session |
| SR | min_distance_pct | 0.50 | S/R entry block |
| POSITION | account_risk_pct_per_coin | 8.75 | Position size (~$50 at ~$570) |
| EXIT | profit_lock_pct | 1.05 | 50% close level |
| EXIT | trailing_pct | 0.50 | Trailing stop for remaining half |
| EXIT | structural_exits | `False` | Pattern/EMA/RSI/S-R exits OFF in LIVE |
| EXIT | rsi_extreme_long/short | 78 / 22 | Reserved (inactive with structural_exits) |
| BREADTH | TIMEFRAMES | `("1h", "4h")` | Sentiment timeframes (1D removed: too laggy) |
| LLM | LLM_BIAS_ENABLED | `.env` (`true`) | Shadow-only second opinion |

## Change Log

| Change | Before | After | Reason |
|--------|--------|-------|--------|
| **Mode** | DRY_RUN | **LIVE** (27.09.2026) | Go-live after dry-run validation |
| **SL cap** | 1.5% | **2.5%** | 1.5% sat below coin ATR → routine stop-outs |
| **Position size** | 17.5% | **8.75%** | Halved for conservative LIVE start |
| **Candidate sorting** | EMA distance only | **3/3 before 2/3, distance as tie-breaker** | Weak 2/3 pullbacks beat strong 3/3 signals (ZRO 10.10.) |
| **Entry window** | 20 min | **30 min** | High-momentum days need more pullback time |
| **Market breadth** | — | **1H+4H scan, log-only** | Day-trading sentiment context for trade logs |
| **Breadth evaluation** | — | `backtest_breadth.py` | Decides on filter activation from data |
| **LLM shadow** | — | optional, observation only | Weekly deterministic-vs-LLM comparison |
| **Exit reporting** | signal price | **real Kraken fill + account balance** | No phantom PnL messages |
| **Bias volume** | re-filtered in entry | **scan/bias only** | Single responsibility per stage |

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