# India ORB Bot

Automated intraday Opening Range Breakout trading bot for NSE equities, via AngelOne SmartAPI.

Strategy: ORB direction bias → VWAP proximity gate → EMA 9/21 confluence boost, with a Dual
Thrust gap-day filter and a Parabolic SAR trailing exit. Full details in
[docs/india-orb-strategy.md](docs/india-orb-strategy.md).

⚠️ **Real money.** `india_orb_bot.py` places live orders on a real AngelOne account unless run
with `--dry-run`.

## Quick start

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt

cp .env.example .env   # fill in your AngelOne credentials

venv/bin/python india_orb_bot.py --dry-run --once   # single check, no real orders
venv/bin/python india_orb_bot.py --dry-run           # full session loop, no real orders
venv/bin/python india_orb_bot.py                     # LIVE — places real orders
```

## Backtesting

Requires `nautilus_trader`, a heavier/optional dependency kept out of the main
`requirements.txt`:

```bash
venv/bin/pip install -r requirements-backtest.txt
venv/bin/python -m backtest.fetch_nse_data --all
venv/bin/python -m backtest.run_india_orb_backtest --rank
```

See [docs/india-orb-strategy.md](docs/india-orb-strategy.md) for the full config reference and
[docs/india-orb-runbook.md](docs/india-orb-runbook.md) for deployment, monitoring, and
known operational issues.

## Layout

```
india_orb_bot.py        # live bot — the thing that actually trades
india_screener.py        # daily watchlist selection (top-turnover NSE symbols)
india_performance.py    # session P&L logging + quantstats report generation
config.py                 # all strategy/risk parameters

brokers/
  angelone.py              # AngelOne SmartAPI REST client
  angelone_ws.py            # AngelOne WebSocket live price feed

strategies/india_orb.py    # NautilusTrader backtest strategy (kept in parity with the live bot)
backtest/                   # backtest runner + NSE data fetchers

deploy/                     # systemd service/timer + VPS setup/update scripts
docs/                       # strategy reference + operational runbook
```

## Deployment

Runs continuously on a DigitalOcean VPS during NSE hours (09:00–15:15 IST), started daily by a
systemd timer. See [docs/india-orb-runbook.md](docs/india-orb-runbook.md) for SSH access, the
deploy process, and known failure modes (AngelOne rate limiting and how it's handled).
