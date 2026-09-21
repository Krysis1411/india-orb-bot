# Paper Trading Bots — Operational Runbook

Covers `zone_paper_trader.py` (equity zone strategy + options overlay) and
`index_options_paper_trader.py` (NIFTY/BANKNIFTY index options) — the two strategies that are
**paper-only**, no real orders ever placed via AngelOne. For `india_orb_bot.py` (real money), see
[docs/india-orb-runbook.md](india-orb-runbook.md); shared access/deploy conventions below assume
you've read that doc first.

## Process model

- **Timers**: `zone-paper-bot.timer` and `index-options-paper-bot.timer` both fire
  `Mon-Fri 08:50:00` (server TZ = Asia/Kolkata) — same rationale as `india-orb-bot.timer`.
- **Services**: run `zone_paper_trader.py` / `index_options_paper_trader.py` with no `--dry-run`
  flag (there isn't one — these scripts never place real orders in the first place).
  `Restart=on-failure`, `RestartSec=60`.
- **Both exit cleanly at market close (15:30 IST) rather than sleeping through the night.**
  AngelOne's JWT session token expires at midnight and neither script re-authenticates
  mid-process — a process that stays alive past midnight silently fails every subsequent call
  until someone notices (this actually happened: both processes sat idle for ~4 days with no
  crash trace before being caught, fixed in `6835ce7`). Open positions and zone-touch history are
  persisted to their state files before exit, so the daily restart is safe.
- **State files** (`logs/zone_paper_state.json`, `logs/index_options_paper_state.json`) carry open
  positions and per-zone touch counts across the daily restart — this is what makes the clean-exit
  design safe. Don't delete these while positions are open unless you intend to lose track of them
  (they're paper positions, so the only real cost is a gap in the trade log, not real money).
- **Swing positions carry over between days** — unlike `india-orb-bot`, there is no end-of-day
  square-off. A position opened Tuesday can still be open Thursday.
- **Default poll interval is 15 minutes** (`--interval 900`), not 5 — a 5-minute interval was
  triggering AngelOne rate-limit errors on nearly every cycle once scanning the full ~130+ symbol
  zone universe (or even both index underlyings at higher call volume). Don't lower this without
  re-checking rate-limit error counts in the logs first.

## Setup (manual — not yet wired into `setup_vps.sh`)

`deploy/setup_vps.sh` only installs `india-orb-bot.service`/`.timer`. The zone and index-options
paper-trading services exist as templates
(`deploy/zone-paper-bot.{service,timer}`, `deploy/index-options-paper-bot.{service,timer}`) but
must be installed by hand:

```bash
# From the repo root on the VPS, for each of zone-paper-bot / index-options-paper-bot:
sudo sed \
    -e "s|__REPO_DIR__|$(pwd)|g" \
    -e "s|__USER__|$(whoami)|g" \
    deploy/zone-paper-bot.service | sudo tee /etc/systemd/system/zone-paper-bot.service > /dev/null
sudo cp deploy/zone-paper-bot.timer /etc/systemd/system/zone-paper-bot.timer

sudo systemctl daemon-reload
sudo systemctl enable --now zone-paper-bot.timer
```

Repeat with `index-options-paper-bot` in place of `zone-paper-bot`. Both need the same `.env`
AngelOne credentials as `india-orb-bot` — no separate secrets required, since these are read-only
against the account (no order placement).

## Deploying a code change

Same `deploy/update_vps.sh` restarts `india-orb-bot` only — it does **not** restart these two
services. After a `git pull`, restart them explicitly:

```bash
ssh india-vps "cd ~/india-orb-bot && git pull origin main && venv/bin/pip install -r requirements.txt -q && sudo systemctl restart zone-paper-bot index-options-paper-bot"
```

> Note: `requirements.txt` alone is correct here — see the `requirements-india.txt` bug flagged in
> [docs/india-orb-runbook.md](india-orb-runbook.md#deploying-a-code-change). Also confirm
> `pyarrow`, `scipy`, and `numpy` are present in whatever venv you're deploying to — both are real
> runtime dependencies now (`ml/gbs.py` for options pricing, `pyarrow` for reading the cached
> `backtest/data/*.parquet` warm-start files), not backtest-only extras.

## Monitoring

```bash
ssh india-vps "systemctl status zone-paper-bot --no-pager"
ssh india-vps "systemctl status index-options-paper-bot --no-pager"
ssh india-vps "journalctl -u zone-paper-bot -f"                # live tail
ssh india-vps "journalctl -u index-options-paper-bot -f"

ssh india-vps "tail -f ~/india-orb-bot/logs/zone_paper_\$(date +%Y%m%d).log"
ssh india-vps "tail -f ~/india-orb-bot/logs/index_options_paper_\$(date +%Y%m%d).log"
```

Trade logs (CSV, one row per closed trade):

```bash
ssh india-vps "cat ~/india-orb-bot/logs/zone_paper_trades.csv"
ssh india-vps "cat ~/india-orb-bot/logs/zone_options_paper_trades.csv"   # options overlay, separate file
ssh india-vps "cat ~/india-orb-bot/logs/index_options_paper_trades.csv"
```

Open positions right now (state file, JSON):

```bash
ssh india-vps "cat ~/india-orb-bot/logs/zone_paper_state.json | python3 -m json.tool"
ssh india-vps "cat ~/india-orb-bot/logs/index_options_paper_state.json | python3 -m json.tool"
```

Rate-limit error count (same failure mode as `india-orb-bot`, see
[docs/india-orb-runbook.md](india-orb-runbook.md#known-failure-mode-angelone-getcandledata-rate-limiting)):

```bash
ssh india-vps "grep -c 'exceeding access rate' ~/india-orb-bot/logs/zone_paper_\$(date +%Y%m%d).log"
```

## Known quirks

- **Connection resets from AngelOne, distinct from rate-limit rejections** — showed up 2026-09-15
  right after the zone universe grew to 153 cached symbols, and got worse the next day (240
  `ConnectionResetError`/"Connection aborted" occurrences on 2026-09-16, outnumbering the classic
  "exceeding access rate" rejection at 76). Check with:
  ```bash
  ssh india-vps "journalctl -u zone-paper-bot --no-pager --since '<date> 00:00' --until '<date+1> 00:00' | grep -c 'Connection reset\|Connection aborted'"
  ```
  Fixed in `brokers/angelone.py::get_today_candles` (2026-09-17): the retry-with-backoff logic
  previously only covered "exceeding access rate" — a connection reset fell straight through to
  failure with zero retries. Now retries both. If this count keeps climbing even with retries in
  place, the next lever is trimming the scanned universe (fewer symbols = fewer requests/cycle),
  not more retries — retrying harder against a server that's actively throttling the account risks
  making it worse, not better.
- **The retry fix moved failures rather than removing them (2026-09-21).** Per-day counts from the
  session logs: connection resets fell 120 -> 3 -> 2 (Sep 16/17/18) but "exceeding access rate" rose
  76 -> 213 -> 219, and total failed candle fetches stayed ~200-236/day. The zone bot also completes only
  ~11-13 cycles/day (index bot ~23-24) because retries with backoff on ~220 failures stretch each cycle
  well past the 15-minute interval. Since the per-bar replay fix (2026-09-21) a slow cycle no longer
  skips bars, but data is still fetched late. Next lever if this persists: trim the scanned universe.
  Quick check: `grep -c 'Cycle complete' logs/zone_paper_<date>.log` (~25 expected).
- **Secrets in the journal (fixed 2026-09-21).** SmartApi's own logger printed the full request headers
  -- bearer JWT and API key -- on every failed request. `brokers/angelone.py` now redacts them, but
  journal entries written before that date still contain them (JWTs expire at midnight; the API key does
  not). Don't paste raw journal output anywhere external; consider `journalctl --vacuum-time` on the VPS
  and rotating the SmartAPI key if the old journal or a transcript could have been exposed.
- Neither service is covered by `setup_vps.sh`'s automated install — a fresh VPS needs the manual
  steps above run once.
- Neither is restarted by `update_vps.sh` — a code change needs the manual restart command above,
  or `update_vps.sh` needs to be extended to cover all three services.
- `index_options_paper_trader.py` fetches an extra NFO **futures** contract per cycle (as a volume
  proxy for the index, which itself always reports zero traded volume) on top of the index spot
  candles — factor this into rate-limit budgeting if tightening the poll interval.
- Both scripts read/write `backtest/data/*.parquet` for their warm-start historical base merged
  with live bars. **This directory is gitignored** (`backtest/data/` in `.gitignore`) — a fresh
  VPS clone won't have it. Populate it manually (e.g. `rsync` from a machine that has it, or run
  `backtest/fetch_nse_multi_tf.py` / `fetch_nse_data_smartapi.py` for the needed symbols) before
  first starting either paper trader, or ATR/RSI/leg-in windows will be empty for a long warm-up
  period after every fresh deploy.
