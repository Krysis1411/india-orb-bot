# India ORB Bot — Operational Runbook

Live bot runs continuously on a DigitalOcean VPS during NSE hours (09:00–15:15 IST), started daily
by a systemd timer. This doc covers access, deployment, monitoring, and known failure modes.

## Access

SSH alias configured in `~/.ssh/config` (Mac):

```
Host india-vps
    HostName 168.144.90.57
    User root
    IdentityFile ~/.ssh/id_ed25519
```

Connect: `ssh india-vps`. Repo lives at `~/trading-bot` on the VPS.

## Process model

- **Timer**: `india-orb-bot.timer` fires `Mon-Fri 08:50:00` (server TZ = Asia/Kolkata), ~25 min
  before NSE open — gives time for auth + the bot's own pre-market wait loop.
- **Service**: `india-orb-bot.service` runs `venv/bin/python india_orb_bot.py` (no `--dry-run`,
  no `--once` → live money, continuous loop). `Restart=on-failure`, `RestartSec=60`.
- The process **exits itself** after 15:30 IST each day (session complete) — the timer starts a
  fresh process the next morning. All in-memory state (VWAP/EMA/OR cache/session P&L counters) is
  therefore naturally reset once per trading day.
- **Restarting the service mid-session also resets this state** (fresh screener run, fresh OR
  cache, fresh `_session_start_equity` snapshot) even though nothing crashed. Safe with no open
  positions; if a position is open, the exchange-side stop-loss order stays live regardless, but
  the bot's local P&L tracking (`_entry_data`, `_realized_pnl`) resets to zero. Prefer deploying
  outside market hours, or right after confirming no open positions, when possible.

## Deploying a code change

```bash
# Local: commit + push as normal, then:
ssh india-vps "bash ~/trading-bot/deploy/update_vps.sh"
```

This pulls `origin/main`, reinstalls `requirements.txt` + `requirements-india.txt`, and restarts
the service. **The script uses `set -euo pipefail`** — if `pip install` fails for any reason (e.g.
an unsatisfiable version pin), the script aborts *before* reaching the restart step, and the
running process silently keeps executing the old code with no error surfaced beyond the deploy
log. Always verify after deploying:

```bash
ssh india-vps "cd ~/trading-bot && git log -1 --format='%h %s' && systemctl show india-orb-bot --property=ActiveEnterTimestamp"
```

Confirm the commit hash matches what you pushed, and the timestamp is recent.

### If `git pull` fails with local-changes conflicts

Check `git status`/`git diff` on the VPS before doing anything destructive — don't assume it's
safe to discard. This has happened before (2026-07-23): the VPS had uncommitted hand-edits that
turned out to be functionally identical to work already merged into `main` via separate commits.
Resolution used: `git stash push -u -m "<reason>" -- <files>` (keeps a recoverable backup, doesn't
delete), then pull, then leave the stash in place rather than dropping it.

## Monitoring

```bash
ssh india-vps "systemctl status india-orb-bot --no-pager"       # is it running, since when, recent tail
ssh india-vps "systemctl status india-orb-bot.timer --no-pager" # daily auto-start enabled?
ssh india-vps "journalctl -u india-orb-bot -f"                  # live tail
ssh india-vps "tail -f ~/trading-bot/logs/india_orb_\$(date +%Y%m%d).log"   # live tail (file)
ssh india-vps "tail -100 ~/trading-bot/logs/india_orb_\$(date +%Y%m%d).log" # recent snapshot
```

Trades only (filters out per-symbol status noise):
```bash
ssh india-vps "grep -iE 'BREAKOUT|TAKE PROFIT|STOP LOSS|SAR TRAIL|EOD CLOSE' ~/trading-bot/logs/india_orb_\$(date +%Y%m%d).log"
```

P&L history:
```bash
ssh india-vps "cat ~/trading-bot/logs/india_pnl_history.csv"
ssh india-vps "cd ~/trading-bot && venv/bin/python india_performance.py"   # formatted + regenerates quantstats HTML report
```

Today's watchlist:
```bash
ssh india-vps "grep 'Today.s watchlist' ~/trading-bot/logs/india_orb_\$(date +%Y%m%d).log | tail -1"
```

Rate-limit error count (see below):
```bash
ssh india-vps "grep -c 'exceeding access rate' ~/trading-bot/logs/india_orb_\$(date +%Y%m%d).log"
```

## Known failure mode: AngelOne `getCandleData` rate limiting

**Symptom**: `ERROR | <SYMBOL>: candle fetch failed — ... 'Access denied because of exceeding access rate'`

Three distinct contributing causes have been found and fixed (2026-07-23), plus one residual cause
that's accepted as normal cost:

1. **Fixed** — the OR-cache-building loop and the entry-screening loop could both independently
   hit `getCandleData` for the same symbol within one cycle when the first fetch failed, with no
   cooldown between them. Now a failed OR fetch marks the symbol as already-failed-this-cycle, so
   the entry loop skips it instead of retrying immediately.
2. **Fixed** — failed OR fetches were retried every single cycle indefinitely. Now capped at
   `_OR_FETCH_MAX_RETRIES` (3) consecutive cycle failures before giving up on that symbol for the
   day, with an extra 1.5s cooldown sleep after each failure.
3. **Fixed** — `process_symbol()` would force a fallback `getCandleData` call purely for price
   discovery whenever no live price (`ltp`) was available from the WebSocket/batch-quote cache,
   even when the symbol's OR was already cached. Now the entry loop skips a symbol for the cycle
   entirely when its OR is known but no live price is available, deferring to the WebSocket's next
   tick instead.
4. **Residual, by design** — volume confirmation for a symbol sitting near/through its OR boundary
   still requires a fresh `getCandleData` call every cycle it stays there (the WebSocket price feed
   carries cumulative session volume, not per-bar volume, so it can't directly satisfy the
   `vol_ok` check without extra bookkeeping). This occasionally collides with AngelOne's rate limit
   for symbols that hover near breakout for many consecutive cycles (e.g. BHARTIARTL sitting just
   below OR low for 30+ minutes). Not yet fixed — see "Possible future fix" below.

**Impact history**: 186 rate-limit errors / 0 trades on 2026-07-22 (worst day, before fixes) → 11
errors in the first 85 min of 2026-07-24 (after fixes) — meaningful improvement, not full elimination.

**Possible future fix for #4**: track each symbol's cumulative WS volume from the previous cycle
and diff it (`current_cum_vol − previous_cum_vol` ≈ volume in the last ~5 min) as an approximation
of per-bar volume, avoiding the REST call entirely for symbols with a live WS feed. Not implemented
yet — would need new per-symbol state and changes the volume signal's precision slightly (interval
delta vs. bar-aligned volume).

## Other known quirks

- `nseIntraday: Invalid Token` / `get_batch_quote: Invalid Token` warnings can appear, especially
  right after a mid-session restart before the WebSocket has warmed up — these are handled
  fallbacks (no intraday filter applied / falls back to per-symbol fetch), not fatal, but worth
  checking if they persist across multiple cycles.
- `Unknown key name 'StartLimitIntervalSec' in section 'Service'` — harmless systemd version
  mismatch warning from `deploy/india-orb-bot.service`, directive is just ignored.
- `requirements-india.txt` pins should be sanity-checked against actual PyPI version history when
  edited — `jugaad-data>=2.6` was an unsatisfiable constraint (package never went past 0.33.x) that
  silently broke every deploy for a period until caught (fixed to `>=0.26`).
