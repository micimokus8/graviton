# Graviton — EMA20 Momentum Pullback Bot

<p align="center">
  <img src="GravitonLogo.png" alt="Graviton" width="200">
</p>

Graviton trades crypto perpetuals on Kraken by hunting momentum pullbacks
to the EMA20. It scans for coins with strong intraday moves (4–18% / 24h),
confirms directional bias from the first 15m candle after session open,
then enters on 1m timeframe when price gravitates back to the EMA20 line.

- **Sessions:** NY (13:30–16:00 UTC) and Asia (00:00–02:00 UTC)
- **Exchange:** Kraken Perpetuals via CCXT (308 USD linear perps)
- **Leverage:** 1x Isolated
- **Mode:** Dry-Run by default (`DRY_RUN = True`)
- **No LLM. Pure Python. Deterministic signals.**

## Strategy

```
Scan (30m before) → Bias (15m candle) → Entry (1m EMA20) → Exit (3 Levels)
```

### 1. Scan — Momentum Filter (30 min before session)

- 24h Change: 4% – 18%
- 24h Volume: > $750,000 USD
- Max 8 coins on watchlist
- Sorted by abs(change) descending

### 2. Bias — Directional Analysis (16 min after session open)

- **1 closed 15m candle** → LONG / SHORT / NOISE
- Daily trend context: strong daily trend + small session retrace = trend continuation bias
- RSI guard: RSI > 80 blocks LONG, RSI < 20 blocks SHORT
- Earlier bias (16 min vs 31 min) gives 15 min more entry time

### 3. Entry — 1m EMA20 Touch (during session)

Two entry modes, selected by RSI:

**Fast Entry (RSI neutral):** Price touches EMA20 + RSI 30-65 (LONG) / 35-70 (SHORT) → **immediate entry.** No rejection candle required. The neutral RSI confirms the trend has room to run.

**Rejection Entry (RSI extreme):** Price touches EMA20 + RSI outside neutral range → waits for a rejection candle (green candle with low at EMA20 for LONG, red candle with high at EMA20 for SHORT) plus volume confirmation (>1.2x average).

Entry filters:
- **EMA side check:** LONG only if price > EMA20, SHORT only if price < EMA20. Prevents trading against the micro-trend.
- **Dynamic EMA distance:** Based on 24h coin change (<5% → 0.50%, 5-10% → 0.60%, >10% → 1.00%)
- **Candidate rotation:** All non-S/R-blocked candidates are polled in a 30s cycle — first with a valid entry wins.
- **S/R proximity:** If nearest S/R is <0.5% away → fallback to best candidate if all blocked.
- **SL: 0.75× 1H ATR** (min 0.6%) — dynamic per coin volatility
- 1 position per session, ~$100 (17.5% of equity)

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
🎯 [DRY RUN] ENTRY LONG AAVE @ 96.11 | SL 94.64 | Fast Entry (RSI 55)
📤 [DRY RUN] EXIT 100% LONG AAVE @ session_end | PnL: 🟢 +4.85%
✅ [NY] Session Ende
```

## Project Structure

```
graviton/
├── session.py           # Full session runner (Bias → Entry → Watcher → Close)
├── config.py            # All parameters (sessions, filters, sizing, exit)
├── scanner.py           # Kraken Futures screener via CCXT
├── bias.py              # 15m directional bias (Wilder's RSI)
├── entry.py             # 1m EMA20 entry + Fast/Rejection modes
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
├── data/                # Runtime data (watchlist, entry state, debug logs)
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
NY Bias          14:01   16:01   16 min after open (1 × 15m candle)
NY Session       14:02   16:02   Polling starts immediately after bias
NY Scan (pre)    13:00   15:00   Writes watchlist for bias
Asia Scan        23:30   01:30   Paused
Asia Session     00:00   02:00   Paused
```

- **Scan** saves watchlist to `data/watchlist.json`
- **Bias** runs 16 min after session open
- **Session** uses `nohup` to survive cron timeouts
- Empty watchlist → session skips automatically

### Debug Logs

`data/session_debug.jsonl` contains one entry per coin per polling cycle:

```json
{"cycle":1, "base":"FARTCOIN", "state":"no_entry", "reason":"Preis -0.06% unter EMA20 — kein LONG"}
{"cycle":2, "base":"AAVE", "state":"entered", "reason":"Fast Entry: an EMA20 (RSI 55, Dist 0.17%)"}
```

If no entry occurs, a Telegram summary explains why for each candidate.

## Configuration

| Section | Key | Value | Description |
|---------|-----|-------|-------------|
| DRY_RUN | — | `True` | No live orders |
| SESSIONS | ny/asia | 13:30/00:00 | Session open/close (UTC) |
| SCAN | min/max_change_pct | 4.0 / 18.0 | 24h change filter |
| SCAN | min_volume_eur | 750_000 | Min 24h volume |
| BIAS | min_candles | 2 | Candles needed for bias |
| BIAS | rsi_long_max/short_min | 80 / 20 | RSI bias block |
| ENTRY | ema_distance_max | 0.50 | Base EMA distance (scaled by 24h change) |
| ENTRY | ema_period | 20 | EMA length |
| SR | min_distance_pct | 0.50 | S/R entry block (fallback if all blocked) |
| POSITION | account_risk_pct_per_coin | 17.5 | ~$100 at $570 equity |
| EXIT | ema_overextended_pct | 2.50 | EMA structural exit |
| EXIT | trailing_pct | 0.30 | Trailing stop distance |
| EXIT | rsi_extreme_long/short | 78 / 22 | RSI extreme exit |

### Change Log (July 2026)

| Change | Before | After | Reason |
|--------|--------|-------|--------|
| **Bias timing** | 31 min (2 candles) | **16 min** (1 candle) | +15 min entry time |
| **Entry rotation** | Single coin, 2h loop | **All candidates, 30s cycle** | Fair chance for all |
| **EMA side check** | None | **LONG > EMA20, SHORT < EMA20** | Prevent wrong-side entries |
| **Fast Entry** | Always wait for rejection | **RSI neutral → immediate** | Catch momentum moves |
| **SL** | 0.30× 1H ATR, min 0.3% | **0.75× 1H ATR, min 0.6%** | Wider SL for noise |
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