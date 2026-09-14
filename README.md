# India ORB Bot

Automated intraday and swing trading systems for NSE equities and index options, via AngelOne
SmartAPI. Despite the repo's name (a holdover from its original scope), it now contains **three**
independent trading systems that share the same broker client and a common zone-detection engine:

| System | Style | Money | Entry point |
|---|---|---|---|
| **India ORB** | Intraday Opening Range Breakout, equities | ⚠️ **Real** (unless `--dry-run`) | [`india_orb_bot.py`](india_orb_bot.py) |
| **Zone strategy** | Swing supply/demand zones, equities + single-stock options overlay | Paper only | [`zone_paper_trader.py`](zone_paper_trader.py) |
| **Index options** | Swing supply/demand zones on NIFTY/BANKNIFTY, translated to real option contracts | Paper only | [`index_options_paper_trader.py`](index_options_paper_trader.py) |

Only `india_orb_bot.py` places real orders. The other two run the real decision logic against live
market data and log what they *would* do — AngelOne has no native paper-trading mode, so this is
how paper trading is done here.

## Quick start

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt

cp .env.example .env   # fill in your AngelOne credentials
```

### India ORB (real money)

```bash
venv/bin/python india_orb_bot.py --dry-run --once   # single check, no real orders
venv/bin/python india_orb_bot.py --dry-run           # full session loop, no real orders
venv/bin/python india_orb_bot.py                     # LIVE — places real orders
```

Strategy: ORB direction bias → VWAP proximity gate → EMA 9/21 confluence boost, with a Dual Thrust
gap-day filter and a Parabolic SAR trailing exit. Full details in
[docs/india-orb-strategy.md](docs/india-orb-strategy.md).

### Zone strategy (paper only)

```bash
venv/bin/python zone_paper_trader.py --once       # single cycle, testing
venv/bin/python zone_paper_trader.py               # loop every 15 min during market hours
```

Supply/demand reversal zones (1h + 5m) with a three-signal entry confirmation (reaction candle +
RSI exhaustion + volume), multi-touch trading, and a single-stock options overlay on every equity
signal. This is a swing strategy — positions can span multiple days. Full details in
[docs/zone-strategy.md](docs/zone-strategy.md).

### Index options (paper only)

```bash
venv/bin/python index_options_paper_trader.py --once
venv/bin/python index_options_paper_trader.py
```

Same zone-detection/entry-confirmation pipeline as the equity zone strategy, applied to
NIFTY/BANKNIFTY and translated into a real, ATM option contract (nearest weekly for NIFTY, nearest
monthly for BANKNIFTY — SEBI discontinued BANKNIFTY weeklies in Oct 2024), priced off the option's
real live LTP. Full details in [docs/index-options-strategy.md](docs/index-options-strategy.md).

## Backtesting

No extra dependencies needed — everything below runs on plain pandas, on top of `requirements.txt`.

```bash
# ORB equities
venv/bin/python -m backtest.fetch_nse_data_smartapi --all      # or fetch_nse_data.py (yfinance, 60-day cap)
venv/bin/python -m backtest.pandas_backtest --rank              # rank all INDIA_SYMBOLS
venv/bin/python -m backtest.pandas_backtest --optimize            # grid search stop/mult
venv/bin/python -m backtest.pandas_backtest RELIANCE               # single symbol, full trade log

# Zone strategy (equities)
venv/bin/python -m backtest.fetch_nse_multi_tf --all              # daily + hourly bars
venv/bin/python -m backtest.zone_backtest                          # all cached symbols + full stats
venv/bin/python -m backtest.zone_backtest TORNTPHARM                # single symbol, trade log

# Index options
venv/bin/python -m backtest.fetch_index_data
venv/bin/python -m backtest.index_options_backtest
```

`backtest/run_india_orb_backtest.py` is an older NautilusTrader-based ORB backtest runner, kept for
cross-checking `strategies/india_orb.py` against a real execution engine (symbol ranking,
day-of-week breakdown, entry-time heatmap). It needs the heavier, optional
`requirements-backtest.txt` (NautilusTrader + a Rust toolchain on first install) and is **not**
the day-to-day backtest path — `backtest/pandas_backtest.py` ports the identical signal logic
without that dependency and is what actually gets run to validate changes:

```bash
venv/bin/pip install -r requirements-backtest.txt
venv/bin/python -m backtest.run_india_orb_backtest --rank
```

**Read before trusting any backtest number**: `backtest/zone_backtest.py` nets out an estimated
round-trip cost + slippage (`pnl_pct` is net; `gross_pnl_pct` is kept for comparison) — the ORB and
index-options backtests do not model costs at all. Every strategy here has so far only been
validated in-sample (tuned and re-tested against the same cached dataset), and the paper traders
don't yet apply the same cost model to their logged P&L either. See the "Validation status"
sections of
[docs/zone-strategy.md](docs/zone-strategy.md#validation-status--read-before-trusting-any-number)
and [docs/index-options-strategy.md](docs/index-options-strategy.md#validation-status) — the paper
traders exist specifically to get a genuine out-of-sample read.

## Layout

```
india_orb_bot.py                 # live ORB bot (equities) — the thing that actually trades real money
india_screener.py                 # ORB watchlist — fixed INDIA_SYMBOLS list minus INDIA_BLOCKLIST
india_performance.py             # ORB session P&L logging + quantstats report generation
zone_paper_trader.py               # zone strategy paper trader (equities) + options overlay
zone_screener.py                    # zone strategy symbol universe (config.ZONE_SYMBOLS)
index_options_paper_trader.py        # NIFTY/BANKNIFTY index options paper trader
config.py                             # all strategy/risk parameters + symbol universes

brokers/
  angelone.py                          # AngelOne SmartAPI REST client — auth, candles, orders,
                                         # ScripMaster (equity tokens + NFO option/futures chains)
  angelone_ws.py                        # AngelOne WebSocket live price feed

strategies/
  india_orb.py                          # NautilusTrader ORB strategy (kept in parity with the live bot)
  zone_detector.py                       # supply/demand zone detection (shared by zone + index-options bots)
  entry_confirmation.py                   # reaction-candle + RSI + volume entry confirmation
  indicators.py                            # RSI/MACD-divergence helpers

ml/
  gbs.py                                    # Black-Scholes options pricing (index options backtest)

backtest/                                     # backtest runners + NSE/index data fetchers
  pandas_backtest.py / run_india_orb_backtest.py   # ORB backtest (pandas / NautilusTrader)
  zone_backtest.py                                  # zone strategy backtest
  index_options_backtest.py                          # index options backtest (uses ml/gbs.py)
  fetch_nse_data*.py / fetch_nse_multi_tf.py / fetch_index_data.py   # data fetchers
  data/                                                # cached parquet bars (gitignored — not in git)
  results/                                              # backtest output CSVs

deploy/                                        # systemd service/timer templates + VPS setup/update scripts
docs/                                           # strategy references + operational runbooks
logs/                                            # runtime logs, trade CSVs, paper-trader state (gitignored)
```

## Deployment

All three bots run continuously on a DigitalOcean VPS during NSE hours (09:00–15:30 IST), started
daily by a systemd timer (a fresh process each day, not just crash recovery — AngelOne's session
token expires at midnight). See:

- [docs/india-orb-runbook.md](docs/india-orb-runbook.md) — SSH access, deploy process, and known
  failure modes for the real-money ORB bot (also covers the shared VPS access/deploy conventions).
- [docs/paper-trading-runbook.md](docs/paper-trading-runbook.md) — setup, monitoring, and known
  quirks for the two paper-trading services.

## Docs index

| Doc | Covers |
|---|---|
| [docs/india-orb-strategy.md](docs/india-orb-strategy.md) | ORB signal pipeline, position management, config reference |
| [docs/india-orb-runbook.md](docs/india-orb-runbook.md) | ORB bot ops: access, deploy, monitoring, rate-limit failure mode |
| [docs/zone-strategy.md](docs/zone-strategy.md) | Zone detection, entry confirmation, confluence, options overlay, validation status |
| [docs/index-options-strategy.md](docs/index-options-strategy.md) | Index options translation layer, expiry/strike rules, pricing model |
| [docs/paper-trading-runbook.md](docs/paper-trading-runbook.md) | Paper-trader ops: setup, monitoring, known quirks |
| [docs/kaufman-tsam-notes.md](docs/kaufman-tsam-notes.md) | Distilled notes from Kaufman's *Trading Systems and Methods* backing the zone strategy's risk/exit design |
| [docs/miner-hpts-notes.md](docs/miner-hpts-notes.md) | Distilled notes from Miner's *High Probability Trading Strategies* backing the HTF-momentum confluence factor |
