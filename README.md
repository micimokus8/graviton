# Graviton — EMA20 Momentum Pullback Bot

<p align="center">
  <img src="GravitonLogo.png" alt="Graviton" width="200">
</p>

Graviton trades **Kraken USD perpetuals** by hunting momentum around the US
session open. It scans for instruments with strong intraday moves, determines
the direction from the 1H structure (4H acts as a veto, 15m confirms), then
enters on an EMA20 pullback confirmed by a closed rejection candle with volume.
Exits use an R-multiple target derived from the real stop plus a forced close
at session end.

The bot runs in **one of two modes** — same code, same cron, one switch
(`GRAVITON_MODE` / the `MODE` file):

| | **crypto** | **stocks** |
|---|---|---|
| Universe | coins (Layer 1, DeFi, …) | tokenized equities: 14 `xStocks` + 13 `Equities` |
| Entry timeframe | 5m | **15m** |
| Initial stop | floor 1.5 % (ATR-based, cap 5 %) | **2.5 %** (cap 6 %) |
| Target | **3R** of the stop | **2R** of the stop |
| Session | 13:30–17:00 UTC | **NYSE 9:30–16:00 ET** (auto-DST) |
| Days | all | **Mon–Fri** |
| State | `data/` | `data/stocks/` |

- **Exchange:** Kraken Perpetuals via CCXT (USD linear perps)
- **Leverage:** 1x Isolated · **size:** fixed 100 USDC per trade
- **Mode:** **DRY_RUN** since 25.10.2026 (`DRY_RUN = True`, live pause)
- **LLM:** shadow bias **disabled** (never controlled trades)
- **Sentiment context:** 1H+4H market-breadth scan, log-only (no filter yet)

## Strategy

```
MODE → Scan (universe per mode) → Bias (4H veto / 1H direction / 15m confirmation)
     → Entry (EMA20 pullback on the mode's timeframe) → Exit (R-target + session end)
                        ↓
              ADX(+DI/-DI) regime label · Market Breadth 1H+4H (both log-only)
```

### 1. Scan — Momentum Filter (~10 min before session)

| Filter | crypto | stocks |
|---|---|---|
| 24h change | ≥ 3 % (no upper limit — futures ticker differs from spot) | **≥ 0.8 %** (equities move 1–3 %/day) |
| 24h volume | > $500 000 | **> $20 000** (only 27 equities exist — do not pre-filter them away) |
| Watchlist | top 8 | top 8 |

The universe comes from the Kraken market category, not from symbol names:
`stock_categories = ["xStocks", "Equities"]`. Pre-IPO tokens (`OPENAIX`,
`ANTHROPICX`), commodities, forex and indices stay out (different asset class).
Equities such as `AMD`, `INTC`, `PLTR`, `MU`, `SOXL`, `SAMSUNG` carry **no X
suffix** — a name-based filter missed 13 tradeable instruments.

### 2. Bias — Roles instead of votes (13:25 UTC)

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

**LLM shadow (disabled):** `shadow_runner.py` asked an LLM for a second opinion
per coin and wrote it to `bias_shadow_history.jsonl`. The shadow never changed a
trade (`effective_bias` was always the deterministic value), the prompt was
crypto-specific and the symbol parser failed on xStocks (8/8 runs
"LLM-Rangfolge ungültig"). Switched off completely (`LLM_BIAS_ACTIVE = False`) —
it cost ~30–40 s per bias run for data that never influenced anything.

**Market breadth (log-only):** `breadth.py` scans BTC/ETH/SOL + top-20 perps
on **1H + 4H** at bias time and writes `data/market_sentiment.json`
(LONG / SHORT / CHOPPY). Every trade-log entry carries this sentiment for
later evaluation. **It does not filter trades yet** — the historical backtest
(`backtest_breadth.py`) decides whether it will become a filter.

### 3. Entry — EMA20 Pullback (5m crypto / 15m stocks)

The timeframe is chosen from the market category: **equities 15m, coins 5m**.
Backtest over 4 000 bars per symbol (`scripts/tf_backtest.py`):

| | trades | total | Ø/trade | win rate |
|---|---|---|---|---|
| **15m equities** | 177 | **+12.4 %** | +0.07 % | 51 % |
| 5m equities | 82 | +1.7 % | +0.02 % | 39 % |
| 15m crypto | 155 | **−21.0 %** | −0.14 % | 39 % |
| 5m crypto | 56 | +3.4 % | +0.06 % | 46 % |

Entry requires:

1. **Distance to EMA20 in ATR units**, not percent: `≤ 0.70 × ATR(14)` of the
   entry timeframe (`ema_distance_max_atr`). Measured on 41 live trades:

   | distance (ATR, 5m) | n | win rate | Ø | total |
   |---|---|---|---|---|
   | < 0.3 | 10 | 70 % | +1.09 % | +10.9 % |
   | 0.3–0.7 | 13 | 62 % | +0.66 % | +8.6 % |
   | **≥ 0.7** | 18 | **39 %** | **−0.72 %** | **−13.0 %** |

   Percent was the wrong unit: 0.5 % is 1.5 ATR on a quiet coin and 0.3 ATR on
   a volatile one — which let 18 entries chase moves that had already run.
2. One of the last three fully closed candles confirms rejection (on the same
   timeframe):
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
- S/R is checked **only at the actual entry price**, per cycle. The former
  one-shot pre-filter used the price from the bias snapshot and removed a coin
  for the whole session: DASH 28.10. was blocked at 53.30 (support 53.28,
  0.04 % away), while at 13:50 the live price was 53.02 — the same check
  passes there. A blocked entry attempt only skips that cycle; the coin stays
  in the rotation and is re-evaluated with a fresh price 30 s later.
- BTC 1m counter-trend correlation can skip an otherwise valid candidate.
- Volume is checked twice by design: session activity in scan/bias **and**
  participation on the rejection candle in the entry.
- Initial SL: `min(max(1.0 × 1H ATR, sl_offset_pct), sl_cap_pct)` —
  crypto floor 1.5 % / cap 5 %, stocks 2.5 % / 6 %. The old 0.9 % floor stopped
  trades out inside normal noise (BCH: 1.14 % stop, −1.14 % after <30 min).
- One position per session (100 USDC fixed).

### 4. Exit — LIVE uses the "dry-run parity" engine

In LIVE, only the conservative exit mechanisms are active
(`structural_exits = False`, `pattern_exit_50 = False` — same as the DRY_RUN sim):

| Level | Trigger | Action |
|-------|---------|--------|
| **1/3** | Profit lock at **target_rr × the actual stop** | Close 50 %, rest → trailing stop |
| **2/3** | Trailing stop hit (0.5 %) or initial SL | Close 100 % |
| **3/3** | Session end | Force close all |

`target_rr` = **3.0 for crypto** (min 2 %, cap 10 %), **2.0 for stocks**
(3R = 7.5 % on a 2.5 % stop practically never triggers — 4 TP hits in 177
backtest trades). Early targets destroy the edge: with a 1.5 % stop the same
41 trades returned 1.5R → +1.6 %, 2R → +5.4 %, **3R → +19.4 %**, no TP → +19.9 %.

Session end is **17:00 UTC for crypto** and **16:00 ET (= 20:00 UTC, 21:00 CET)
for stocks**, computed from `America/New_York` so the US DST switch needs no
manual change. A wide "safety" stop (10 %) was measured and rejected: it returns
+3.5 % where a 1.5 % floor returns +19.9 % — it only cuts the recoveries.

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
   Ziel:   4.50%  (3.0R) → 50% sichern
   Grund:  Pullback: 5m Rejection an EMA (0.15%, 1 Kerze zurück, Vol 1.42x)
📤 [DRY RUN] EXIT 50% LONG NEAR
   Ziel:   4.50% (3.0R) erreicht
   PnL:    🟢 +4.50%
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
├── entry.py             # EMA20 pullback + rejection (5m crypto / 15m stocks)
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
│   ├── filter_stats.py          # bucket analysis of the trade log
│   ├── exit_sim.py              # SL/TP counterfactual on real candles
│   └── tf_backtest.py           # 5m vs 15m vs 1h entry backtest per universe
├── data/                # crypto runtime state (watchlist, bias, entry state, logs)
├── data/stocks/         # stocks runtime state (same files, separate directory)
├── MODE                 # "crypto" or "stocks" — single source of truth
├── logs/
├── tests/               # unittest suite (86 tests)
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

# Mode switch (one file, both crons read it)
echo stocks > MODE && python scanner.py    # trade tokenized equities
echo crypto > MODE && python scanner.py    # trade coins

# Research
python scripts/exit_sim.py                # SL/TP counterfactual (real candles)
python scripts/tf_backtest.py --tf 15m --group stocks
```

### Live Mode

`DRY_RUN = True` in `config.py` since 25.10.2026 (deliberate live pause after a
losing streak; the signal rework is validated in simulation first). In LIVE,
SL is a hard exchange stop-order (`reduceOnly`, mark-triggered); take-profit is
watcher-side only (if the watcher dies, the SL still protects, but there is no
exchange-side TP).

## Cron Jobs (Hermes Agent)

```
Job              UTC     CET*    Beschreibung
───────────────  ──────  ──────  ───────────────────────────────
NY Scan (pre)    13:20   14:20   Writes watchlist for bias (Mon–Fri)
NY Bias          13:25   14:25   4H/1H/15m bias + regime → breadth (Mon–Fri)
NY Session       13:30   14:30   Polling; entries limited to 20 min after bias
Asia Scan/Session                Paused
```

*CET during winter time. The cron expressions run in **server-local time**.
All three jobs run **Mon–Fri** and read the mode from `graviton/MODE`:
the shell scripts export `GRAVITON_MODE` and point `$GV_DATA` at `data/` or
`data/stocks/`. On 01.11. (US DST) the NYSE opens at 15:30 CET — the cron start
times move to 15:20 / 15:25 / 15:30, the session window itself follows
automatically.

- **Scan** saves watchlist to `data/watchlist.json` (stocks: `data/stocks/`)
- **Bias** runs at 13:45 UTC, writes the multi-timeframe snapshot, then runs
  LLM shadow enrichment and the market-breadth scan (both fail-soft)
- **Entry** uses only the bias snapshot; no independent bias recalculation
- **Session** runs as a background process so it survives cron timeouts
- Empty watchlist → session skips automatically

### Trade Log

`data/trade_log.jsonl` (stocks: `data/stocks/trade_log.jsonl`) records every
entry and exit with context:

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
| MODE | `GRAVITON_MODE` / `MODE` file | `crypto` \| `stocks` | env → file → crypto |
| DRY_RUN | — | `True` | Simulated trading since 25.10.2026 (live pause) |
| SESSIONS | ny | crypto 13:30–17:00 UTC · stocks 9:30–16:00 ET | stocks window computed from America/New_York |
| SCAN | min_change_pct | 3.0 (crypto) / **0.8** (stocks) | 24h change filter |
| SCAN | min_volume_eur | 500 000 (crypto) / **20 000** (stocks) | Min 24h volume |
| SCAN | stock_categories | `["xStocks","Equities"]` | 27 tokenized equities |
| BIAS | min_candles | 4 | Candles needed for bias |
| BIAS | min_session_vol_ratio | 0.7 | Minimum session-volume ratio |
| ENTRY | entry_tf / entry_tf_stock | `5m` / `15m` | Chosen by market category, not by symbol name |
| ENTRY | ema_distance_max_atr | 0.70 | Max EMA distance in ATR units (0 = off) |
| ENTRY | ema_distance_max | 2.00 | Hard percent cap as a safety net |
| ENTRY | sl_offset_pct | 1.50 (crypto) / 2.50 (stocks) | Minimum initial SL |
| ENTRY | sl_cap_pct | 5.00 (crypto) / 6.00 (stocks) | Max SL distance (protects against ATR spikes) |
| ENTRY | rejection_min_vol_ratio | 1.00 | Volume on the rejection candle vs 20-bar median |
| ENTRY | entry_bar_min_close_pos | 0.35 | Entry bar must not close at the extreme |
| ENTRY | max_stair_steps | 1 | Only the first pullback per session |
| ENTRY | max_parallel_coins | 1 | One position per session |
| REGIME | enabled | `True` | Regime gate active |
| REGIME | role_mode | `True` | 4H veto / 1H bias / 15m confirmation (False = 2-of-3) |
| REGIME | four_h_veto / four_h_veto_adx | `True` / 25.0 | 4H counter-trend blocks only above ADX 25 |
| REGIME | chop_gate_enabled | `False` | ADX<20 blocks entries (default: log only) |
| SR | min_distance_pct | 0.50 | S/R entry block |
| POSITION | fixed_usd | 100.00 | Fixed 100 USDC per trade (0 = use risk percent) |
| POSITION | account_risk_pct_per_coin | 8.75 | Fallback sizing when `fixed_usd = 0` |
| EXIT | target_rr | 3.00 (crypto) / 2.00 (stocks) | Target = R × actual stop distance |
| EXIT | profit_lock_pct | 2.00 (crypto) / 2.50 (stocks) | Minimum 50 %-close level |
| EXIT | target_max_pct | 10.00 (crypto) / 12.00 (stocks) | Cap for wide stops |
| EXIT | trailing_pct | 0.50 | Trailing stop for remaining half |
| EXIT | structural_exits | `False` | Pattern/EMA/RSI/S-R exits OFF in LIVE |
| EXIT | rsi_extreme_long/short | 78 / 22 | Reserved (inactive with structural_exits) |
| BREADTH | TIMEFRAMES | `("1h", "4h")` | Sentiment timeframes (1D removed: too laggy) |
| LLM | LLM_BIAS_ACTIVE | `False` | Shadow bias disabled (never influenced trades) |

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
| **S/R block** | one-shot with bias-snapshot price | **per entry, at the live price** | A stale-price block removed DASH from the whole session although the level was already broken |
| **Mode** | LIVE | **DRY_RUN** (25.10.2026) | Live pause during the signal rework |
| **Two modes** | crypto only | **crypto \| stocks** (`MODE` file) | One bot, one cron, switchable |
| **Universe** | coins only | **+27 tokenized equities** (`xStocks` + `Equities`) | Kraken lists 13 equities without an X suffix (AMD, INTC, PLTR, …) |
| **Entry timeframe** | 5m everywhere | **15m equities / 5m crypto** | 15m crypto: −21 %; 15m equities: +12.4 % |
| **EMA distance** | percent (0.5 %, up to 1.5 %) | **0.70 × ATR(14)** | Percent is the wrong unit — 18 trades chased moves ≥0.7 ATR (WR 39 %, −13.0 %) |
| **SL floor** | 0.9 % | **1.5 % crypto / 2.5 % stocks** | 1.14 % stops died inside normal noise |
| **Target** | 2R | **3R crypto / 2R stocks** | 2R = +5.4 %, 3R = +19.4 % on the same trades |
| **Position size** | 8.75 % of equity (≈48 USD) | **fixed 100 USDC** | Explicit size, no equity drift |
| **Safety** | DRY_RUN only checked in `session.py` | **guards inside `trader.py`** | A manual test fired a real Kraken order |
| **Close matching** | compared close side to position side | **mapped buy/sell → long/short** | `close_position` never found a position |
| **LLM bias** | shadow, enabled | **off** | Crypto-specific prompt, xStocks parser failures, never influenced trades |
| **Bias/breadth paths** | hardcoded `data/` | **`CFG.data_dir`** | Stocks mode would have read the crypto watchlist |

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