# India ORB Bot — Strategy Reference

Live implementation: [`india_orb_bot.py`](../india_orb_bot.py) (AngelOne SmartAPI, NSE equities).
Backtest implementation: [`strategies/india_orb.py`](../strategies/india_orb.py) (NautilusTrader), kept
in parity with the live bot — port any live strategy change here too.

## Signal pipeline

Entries go through three gates in order. All three must pass for a trade to open.

### 1. Opening Range Breakout (ORB) — sets direction bias, does not enter

- Opening range = first `INDIA_ORB_RANGE_BARS` (6) × 5-min bars, i.e. **09:15–09:45 IST**.
- OR is rejected for the day if:
  - `OR range % < INDIA_ORB_MIN_OR_PCT` (0.3%) — flat/indecisive open, or
  - `OR range % > INDIA_ORB_MAX_OR_PCT` (2.0%) — gap/spike day, stops would blow out, or
  - **Dual Thrust gate**: `OR range > INDIA_DUAL_THRUST_MAX_MULTIPLE (2.0) × expected range`,
    where expected range = `max(HH−LC, HC−LL)` over the prior `INDIA_DUAL_THRUST_DAYS` (5) days
    of daily OHLC. Catches gap/news days the static % bands miss.
- First bar to close above OR high (or below OR low, if shorts allowed) sets `_orb_direction`
  for the session ("long"/"short") but **does not trade** — it just marks intent. A breakout in
  the opposite direction after bias is set is ignored for the rest of the day.
- **Short-only pullback-in-uptrend filter**: a short signal is skipped if the 5-min EMA9 > EMA21
  (bullish local trend, ≥9 bars warmup) — a breakdown below OR low during an EMA uptrend reads as
  a retracement, not a reversal.

### 2. VWAP proximity — required gate

- Entry only when price is within `± vwap_sigma` (1σ) of session VWAP (cumulative typical-price
  × volume from 09:15, reset daily).
- This is why entries lag the initial breakout bar: price must break out, then pull back toward
  VWAP before the bot will actually enter.

### 3. EMA 9/21 confluence — optional size boost

- If EMA9 agrees with the trade direction (9-bar warmup), position size is **1.5×** normal.
- Otherwise, normal size (`INDIA_POSITION_SIZE_INR` per trade).

## Position management

| Mechanism | Behavior |
|---|---|
| **Stop** | `OR low × (1 − INDIA_ORB_STOP_BUFFER_PCT)` for longs (mirror for shorts) — 0.5% beyond OR boundary |
| **Target** | `OR high + OR range × INDIA_ORB_PROFIT_MULTIPLIER` (2.5×) beyond breakout |
| **Breakeven trail** | Once price reaches 0.5× target, stop moves to entry price (±0.1% buffer) |
| **Parabolic SAR trail** | Independent trailing exit — AF starts at `INDIA_SAR_AF_STEP` (0.02), steps up 0.02 per new price extreme, caps at `INDIA_SAR_AF_MAX` (0.20). Exits on reversal through the SAR level even if stop/target aren't hit. |
| **EOD close** | Force-close all positions at `INDIA_CLOSE_HOUR:INDIA_CLOSE_MINUTE` (15:10 IST) |
| **One trade/day/symbol** | No re-entry once a symbol has traded, win or lose |

Exit priority when multiple conditions fire in the same check: **take_profit > stop_loss/trailing_stop > sar_trail**.

## Entry gates (shared, checked every cycle)

- No new entries after `INDIA_MAX_ENTRY_HOUR:INDIA_MAX_ENTRY_MINUTE` (13:00 IST)
- `INDIA_SKIP_MONDAY_ENTRIES` — currently `False` (60-day backtest showed no meaningful Monday effect)
- Breakout volume ≥ `INDIA_ORB_VOLUME_FACTOR` (0.5×) the average OR-window bar volume
- Nifty 50 trend filter: longs blocked when Nifty is down on the day, shorts blocked when Nifty is up
- Max `INDIA_MAX_OPEN_POSITIONS` (3) concurrent positions, `INDIA_MAX_TOTAL_INR` (₹15,000) deployed
- Daily loss circuit-breaker: blocks new entries for the rest of the session once realized+unrealized
  P&L drops below `−INDIA_DAILY_LOSS_LIMIT_PCT` (5%) of session-start equity

## Watchlist selection

- `india_screener.py` picks the top `INDIA_SCREENER_LIMIT` (15) symbols by **previous day's turnover**
  from the `NSE_UNIVERSE` pool, filtered against `INDIA_BLOCKLIST` and the NSE intraday-eligibility list.
- Re-run once per session at bot startup (~08:50 IST); does not change intraday.
- `INDIA_SYMBOLS` (12 names) is the curated fixed list used for backtesting/optimization, not
  necessarily what's live on a given day — check the actual day's log line
  (`Today's watchlist (N): ...`) for what's really being traded.
- `INDIA_BLOCKLIST` (30 symbols) — proven ORB losers from backtests, permanently excluded regardless
  of screener ranking. See the comments above the list in `config.py` for the backtest PF/win-rate
  evidence behind each exclusion.

## Config reference (`config.py`)

| Constant | Value | Meaning |
|---|---|---|
| `INDIA_ORB_RANGE_BARS` | 6 | OR window length in 5-min bars |
| `INDIA_POSITION_SIZE_INR` | 5,000 | ₹ per trade (base, before EMA boost) |
| `INDIA_MAX_TOTAL_INR` | 15,000 | Max ₹ deployed at once |
| `INDIA_MAX_OPEN_POSITIONS` | 3 | Max concurrent positions |
| `INDIA_ORB_MIN_OR_PCT` / `MAX_OR_PCT` | 0.3% / 2.0% | OR sanity band |
| `INDIA_ORB_PROFIT_MULTIPLIER` | 2.5× | Target distance beyond breakout (grid-search optimised) |
| `INDIA_ORB_VOLUME_FACTOR` | 0.5× | Min breakout volume vs. OR average |
| `INDIA_ORB_STOP_BUFFER_PCT` | 0.5% | Stop distance beyond OR boundary |
| `INDIA_ORB_BREAKOUT_STRENGTH_PCT` | 0.0% | Min clearance past OR boundary (0% wins per walk-forward) |
| `INDIA_DUAL_THRUST_DAYS` / `MAX_MULTIPLE` | 5 / 2.0× | Gap-day gate lookback / threshold |
| `INDIA_SAR_AF_STEP` / `AF_MAX` | 0.02 / 0.20 | Parabolic SAR acceleration factor |
| `INDIA_ALLOW_SHORTS` | True | Enable short breakouts |
| `INDIA_MAX_ENTRY_HOUR:MINUTE` | 13:00 | Entry cutoff |
| `INDIA_CLOSE_HOUR:MINUTE` | 15:10 | EOD forced close |
| `INDIA_DAILY_LOSS_LIMIT_PCT` | 5% | Circuit breaker |
| `INDIA_SCREENER_LIMIT` | 15 | Watchlist size |

## Backtest/live parity

`strategies/india_orb.py` mirrors the live bot's signal logic for use with NautilusTrader
(`backtest/run_india_orb_backtest.py`). As of 2026-07-23 both implement: ORB direction bias, VWAP
zone gate, EMA confluence boost, Dual Thrust gap filter, and Parabolic SAR trail. **Any future change
to the live entry/exit logic should be ported here too**, or backtest results will silently stop
reflecting what's actually trading. There's no automated parity checker for this bot (unlike
`orb_options_bot.py`, which has `check_backtest_parity.py`) — parity is manual, check by diffing the
two files' signal logic when either changes.
