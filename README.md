# Graviton — EMA20 Momentum Pullback Bot

<p align="center">
  <img src="GravitonLogo.png" alt="Graviton" width="200">
</p>

Graviton trades crypto perpetuals on Kraken by hunting momentum at
the NY session open. It scans for coins with strong intraday moves (3%+ / 24h),
determines the direction from the 1H structure (4H acts as a veto, 15m
confirms), then enters on a 5m EMA20 pullback confirmed by a closed rejection
candle with volume. Exits run a 2R profit lock derived from the real stop.

- **Sessions:** NY (13:50–17:00 UTC polling; Asia paused)
- **Exchange:** Kraken Perpetuals via CCXT (USD linear perps)
- **Leverage:** 1x Isolated
- **Mode:** **DRY_RUN** since 25.10.2026 (`DRY_RUN = True`, live pause)
- **LLM:** optional shadow-only bias (observation; never controls trades)
- **Sentiment context:** 1H+4H market-breadth scan, log-only (no filter yet)

## Strategy

```
Scan → Bias (4H veto / 1H direction / 15m confirmation) → Entry (5m EMA20 pullback) → Exit (2R lock)
                        ↓
              ADX(+DI/-DI) regime label · Market Breadth 1H+4H (both log-only)
```

### 1. Scan — Momentum Filter (20 min before session)

- 24h Change: 3%+ (no upper limit — Futures ticker differs from spot)
- 24h Volume: > $500,000 USD (Ticker-based; OHLCV validation after volume filter)
- Max 8 coins on watchlist, sorted by abs(change) descending

### 2. Bias — Roles instead of votes (13:45 UTC)

The bias is calculated by the bias cron from closed candles on 4H, 1H and 15m.
Timeframes no longer vote democratically (2-of-3); they have **roles**
(`REGIME.role_mode = True`):

| Frame | Role | Rule |
|-------|------|------|
| 4H | **Veto** (no vote) | Blocks the trade only on a real counter-trend: opposite 4H direction **and** ADX(4H) ≥ 25 |
| 1H | **Bias** (direction) | Must be directional. `1H = NEUTRAL` → no trade, regardless of the other frames |
| 15m | **Confirmation** | Aligns → `signal_count = 3`; neutral/opposite → `2`. The actual trigger is the 5m pullback |

Fallback: `REGIME.role_mode = False` restores the old 2-of-3 democracy
(useful for A/B comparison). `role_mode` is on by default.

**Regime / ADX** (`regime.py`, Wilder-smoothed):

- ADX has **no direction** — `+DI/-DI` always travel with it, and the veto
  compares them against the trade bias.
- `ADX < 20` (chop) is **logged only** by default (`chop_gate_enabled = False`):
  n=4 in the historical sample and the edge mechanically lives in shallow
  pullbacks / contracted volatility, i.e. rather low ADX.
- ADX is a lagging confirmer — a label, not a timing tool.

Other bias rules (unchanged):

- 4H exhaustion check demotes stale trends to NEUTRAL
- Session volume is evaluated in the bias stage
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
   - LONG: bullish candle with its low near EMA20, close in the upper part
   - SHORT: bearish candle with its high near EMA20, close in the lower part
   - **Volume confirmation on that rejection candle**: volume ≥
     `rejection_min_vol_ratio` (1.0× the 20-bar median). Without participation
     it is not a rejection (DASH 27.10.: signal bar 1.50×, entry bar 0.44×).
3. **Entry-bar geometry:** the candle at whose close we enter must not be a
   crash bar. LONG requires close position ≥ 35% of its range, SHORT ≤ 65%
   (`entry_bar_min_close_pos`). DASH closed at 0% of its range → blocked.
4. The current price is still close enough to EMA20; the old rejection close is
   not used as the fill price. Execution uses the current market price.

Entry behavior:

- No new entry after **20 minutes from the bias file timestamp** or after session close.
  The window is a staleness cap, not the expected latency (measured 27.10.:
  bias 12:46 → entry 12:50:23 = 4 min 13 s). Two effects eat into it: the
  5-minute cron offset (bias :45, session :50) and the loop stopping 30 s
  before the deadline, so the last 5m bar must close before deadline−30 s.
  With 15 min only ~9.5 min (2 bars) remained — the single valid PEPE
  rejection on 28.10. closed 30 s after the window. History (41 trades,
  bias :45): 36× at :50/:51, 3× at :55, 1× at :00, 1× at :05 → 20 min covers
  all of them. Revisit if the cron cadence drops to 1 minute.
- All active candidates are checked every 30 seconds in each cycle.
- **Candidate sorting (since 10.10.):** signals are sorted by **bias strength first**
  (3/3 before 2/3), EMA distance only as tie-breaker — a weak 2/3 pullback can
  no longer beat a strong 3/3 signal (ZRO 10.10. regression case).
- S/R is checked before rotation and again at the actual entry price.
- BTC 1m counter-trend correlation can skip an otherwise valid candidate.
- Volume is checked twice by design: session activity in scan/bias **and**
  participation on the rejection candle in the entry.
- Initial SL: `min(max(1.0 × 1H ATR, 0.9%), 5.0%)`.
- One position per session, with the configured exposure limit.

### 4. Exit — LIVE uses the "dry-run parity" engine

In LIVE, only the conservative exit mechanisms are active
(`structural_exits = False`, `pattern_exit_50 = False` — same as the DRY_RUN sim):

| Level | Trigger | Action |
|-------|---------|--------|
| **1/3** | Profit lock at **2R** of the actual stop (min 2%, cap 10%) | Close 50%, rest → trailing stop |
| **2/3** | Trailing stop hit (0.5%) or initial SL | Close 100% |
| **3/3** | Session end (17:00 UTC) | Force close all |

The target is derived per trade from the real stop distance
(`config.profit_lock_pct_for`): a fixed 2% lock against a 1.41% stop was only
1.4R. Example DASH: SL 1.41% → target 2.83% = 2.00R. DRY_RUN and LIVE use the
same per-trade target (LIVE: `TrackedPosition.profit_lock_pct`).

Every exit reports the **real Kraken fill price** and the account balance —
never the signal price (phantom-exit bug fixed, Oct 2026).

### Telegram Updates

Every key event is delivered live via Telegram. Exits include real fill price,
PnL and account balance:

```
🧠 [NY] Bias: 🟢 NEAR: LONG (Role: 1H BULLISH, 15m BULLISH) 4H:NEUTRAL ...
🎯 [DRY RUN] ENTRY LONG NEAR
   Price: 0.49300000
   SL:    0.48562000  (1.50%)
   Ziel:   3.00%  (2.0R) → 50% sichern
   Grund:  Pullback: 5m Rejection an EMA (0.15%, 1 Kerze zurück, Vol 1.42x)
📤 [DRY RUN] EXIT 50% LONG NEAR
   Ziel:   3.00% (2.0R) erreicht
   PnL:    🟢 +3.00%
✅ [NY] Session Ende — 17:00 UTC
```

## Project Structure

```
graviton/
├── session.py           # Full session runner (Bias → Entry → Watcher → Close)
├── config.py            # All parameters (sessions, filters, sizing, exit)
├── scanner.py           # Kraken Futures screener via CCXT
├── bias.py              # 4H veto / 1H bias / 15m confirmation (2-of-3 fallback)
├── regime.py            # Wilder ADX(+DI/-DI), ATR, regime classification
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
│   ├── scan_cron.sh
│   ├── entry_forensics.py       # per-trade feature reconstruction (live OHLCV)
│   └── filter_stats.py          # bucket analysis of the trade log
├── data/                # Runtime data (watchlist, bias, entry state, logs)
├── logs/
├── tests/               # pytest suite (124 tests)
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

`DRY_RUN = True` in `config.py` since 25.10.2026 (deliberate live pause after a
losing streak; the signal rework is validated in simulation first). In LIVE,
SL is a hard exchange stop-order (`reduceOnly`, mark-triggered); take-profit is
watcher-side only (if the watcher dies, the SL still protects, but there is no
exchange-side TP).

## Cron Jobs (Hermes Agent)

```
Job              UTC     CET*    Description
───────────────  ──────  ──────  ───────────────────────────────
NY Scan (pre)    13:30   14:30   Writes watchlist for bias
NY Bias          13:45   14:45   4H/1H/15m bias + regime → shadow → breadth
NY Session       13:50   14:50   Polling; entries limited to 20 min after bias
Asia Scan        00:30   01:30   Paused
Asia Session     01:00   02:00   Paused
```

*CET during winter time. The cron expressions run in **server-local time**;
they were adjusted on the DST boundary (25.10.) and shift again on 01.11.

- **Scan** saves watchlist to `data/watchlist.json`
- **Bias** runs at 13:45 UTC, writes the multi-timeframe snapshot, then runs
  LLM shadow enrichment and the market-breadth scan (both fail-soft)
- **Entry** uses only the bias snapshot; no independent bias recalculation
- **Session** runs as a background process so it survives cron timeouts
- Empty watchlist → session skips automatically

### Trade Log

`data/trade_log.jsonl` records every entry and exit with context:

```json
{"event":"entry","symbol":"NEAR/USD:USD","bias":"LONG","signal_count":3,
 "dist_pct":0.493,"vol_ratio":1.42,"bar_close_pos":0.62,"atr_pct":0.67,
 "atr_ratio":0.93,"sl_pct":1.41,"target_pct":2.83,"rr":2.0,
 "bias_age_sec":253,"regime":{"regime":"TREND","adx":40.1,"veto":false},
 "market_sentiment":"LONG","market_breadth":"1H:15B/0S 4H:16B/1S"}
{"event":"exit","base":"ZRO","reason":"stop_loss","close_pct":100,
 "pnl_pct":-1.55,"mfe_pct":0.42,"market_sentiment":"CHOPPY", ...}
```

These fields exist so the filters are **measured, not guessed**: volume ratio,
close position, ATR ratio, ADX label, bias age, MFE and the realised R:R are
continuous variables for later bucket analysis.

Rejected candidates are logged too: `session_debug.jsonl` carries the exact
gate (`reason`), the EMA distance and the last bar's EMA penetration
(`wick`), e.g. `keine Rejection: Wick 0.72% > 0.50%`.

`data/session_debug.jsonl` contains one entry per coin per polling cycle:

```json
{"cycle":1, "base":"ZRO", "bias":"SHORT", "state":"entered", "dist":0.114}
```

If no entry occurs, a Telegram summary explains why for each candidate.

## Configuration

| Section | Key | Value | Description |
|---------|-----|-------|-------------|
| DRY_RUN | — | `True` | Simulated trading since 25.10.2026 (live pause) |
| SESSIONS | ny/asia | 13:30/00:00 open, 17:00/02:00 close | Session times (UTC); cron starts polling ~13:50 |
| SCAN | min_change_pct | 3.0 | 24h change filter |
| SCAN | min_volume_eur | 500_000 | Min 24h volume |
| BIAS | min_candles | 4 | Candles needed for bias |
| BIAS | min_session_vol_ratio | 0.7 | Minimum session-volume ratio |
| ENTRY | ema_distance_max | 0.50 | Base EMA distance (dynamically scaled to 1.50) |
| ENTRY | sl_offset_pct | 0.90 | Minimum initial SL (of the effective SL formula) |
| ENTRY | sl_cap_pct | 5.00 | Max SL distance (protects against ATR spikes) |
| ENTRY | rejection_min_vol_ratio | 1.00 | Volume on the rejection candle vs 20-bar median |
| ENTRY | entry_bar_min_close_pos | 0.35 | Entry bar must not close at the extreme |
| ENTRY | max_stair_steps | 1 | Only the first pullback per session |
| ENTRY | max_parallel_coins | 1 | One position per session |
| REGIME | enabled | `True` | Regime gate active |
| REGIME | role_mode | `True` | 4H veto / 1H bias / 15m confirmation (False = 2-of-3) |
| REGIME | four_h_veto / four_h_veto_adx | `True` / 25.0 | 4H counter-trend blocks only above ADX 25 |
| REGIME | chop_gate_enabled | `False` | ADX<20 blocks entries (default: log only) |
| SR | min_distance_pct | 0.50 | S/R entry block |
| POSITION | account_risk_pct_per_coin | 8.75 | Position size (~$50 at ~$570) |
| EXIT | target_rr | 2.00 | Target = 2R of the actual stop distance |
| EXIT | profit_lock_pct | 2.00 | Minimum 50%-close level (floor for `target_rr`) |
| EXIT | target_max_pct | 10.00 | Cap for wide stops |
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
| **SL cap** | 2.5% | **5.0%** | 2.5% still cut ATR-wide stops on volatile days |
| **Position size** | 17.5% | **8.75%** | Halved for conservative LIVE start |
| **Candidate sorting** | EMA distance only | **3/3 before 2/3, distance as tie-breaker** | Weak 2/3 pullbacks beat strong 3/3 signals (ZRO 10.10.) |
| **Entry window** | 20 min | **30 min** | High-momentum days need more pullback time |
| **Market breadth** | — | **1H+4H scan, log-only** | Day-trading sentiment context for trade logs |
| **Breadth evaluation** | — | `backtest_breadth.py` | Decides on filter activation from data |
| **LLM shadow** | — | optional, observation only | Weekly deterministic-vs-LLM comparison |
| **Exit reporting** | signal price | **real Kraken fill + account balance** | No phantom PnL messages |
| **Bias volume** | re-filtered in entry | **scan/bias only** | Single responsibility per stage |
| **Bias roles** | 2-of-3 democracy | **4H veto / 1H bias / 15m confirmation** | Equal votes let 15m noise outvote the 1H (28.10.); 2-of-3 kept as fallback |
| **Regime** | — | **Wilder ADX + +DI/-DI** | ADX gives trend strength, never direction; labels every trade |
| **ADX chop gate** | — | **log-only** (`chop_gate_enabled=False`) | n=4 and the edge lives in shallow pullbacks (low ADX) |
| **Rejection volume** | not checked at entry | **≥1.0× 20-bar median** | DASH 27.10.: entry bar 0.44×, signal bar 1.50× — wrong bar |
| **Entry-bar geometry** | not checked | **close ≥35% of range (LONG)** | DASH closed at 0% of its range — long into a falling knife |
| **Exit target** | fixed 2.0% (1.4R) | **2R of actual SL** (min 2%, cap 10%) | Fixed lock ignored stop width; DASH now 2.83% |
| **Entry window** | 30 min | **15 min** | Staleness cap tied to the 5-min cron cadence |
| **Entry window** | 15 min | **20 min** | 15 min left only ~9.5 min of polling (cron offset + 30 s guard); 2/41 historical entries came at 15–20 min |
| **Rejection reasons** | "keine Rejection" | **exact gate + wick depth** | PEPE 28.10.: wick 0.72 % > 0.5 % was invisible in the log |
| **Deploy** | edits in `graviton/scripts/` | **active cron scripts symlinked to the repo** | Cron ran a 10.10. copy → `regime` never reached `bias_result.json` |
| **Mode** | LIVE | **DRY_RUN** (25.10.2026) | Live pause during the signal rework |

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