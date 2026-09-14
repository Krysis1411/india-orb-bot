# Supply/Demand Zone Strategy — Reference

Detection: [`strategies/zone_detector.py`](../strategies/zone_detector.py).
Entry confirmation: [`strategies/entry_confirmation.py`](../strategies/entry_confirmation.py).
Backtest: [`backtest/zone_backtest.py`](../backtest/zone_backtest.py).
Live (paper): [`zone_paper_trader.py`](../zone_paper_trader.py) — no real orders, ever.

Unlike the ORB bot (intraday-only, one shot per symbol per day), this is a **swing** strategy:
zones are found on 1h/5m bars, positions can be held for hours to weeks, and a single zone can
produce multiple trades over its life.

## What a zone is

A zone is a short consolidation (**the base**) immediately followed by a sharp, high-volume move
away from it (**the breakout leg**). The base's price range becomes the zone's boundaries. Named
by what happened before and after the base:

| Pattern | Into the base | Out of the base | Kind | Class |
|---|---|---|---|---|
| RBR | up | up | demand | continuation |
| DBR | down | up | demand | **reversal** |
| DBD | down | down | supply | continuation |
| RBD | up | down | supply | **reversal** |

**Only reversal zones (DBR/RBD) are traded.** A reversal means the trend actually changed
direction at that level — a much stronger signal than a continuation zone just extending a move
already in progress. The "into the base" leg only counts as a real rally/drop (not drift) if it
has both a real price move (≥ `legin_min_move_atr`, default 1.0 ATR) **and** real volume behind it
(≥ `legin_min_volume_ratio`, default 1.1×) — price drifting on thin volume isn't a rally.

**Zone strength** = `breakout_move_atr × breakout_volume_ratio`, multiplied again by
`legin_volume_ratio` for reversal zones. A big move on average volume, or an average move on huge
volume, scores lower than a big move on high volume at the same time — strength needs both
factors together, not just one.

### Quality filters (on by default, can be disabled for review exports)

- **Base body filter** (`base_min_body_ratio`, 0.25): each base candle must be at least 25% real
  body, not wick — rejects a doji/spinning-top spike masquerading as a stable consolidation shelf.
- **Session-gap filter**: detects session boundaries from timestamp gaps (not a price threshold,
  so it works across timeframes) and refuses to let a breakout leg or leg-in measurement span an
  overnight/weekend gap — gap-open volatility from the next session was found to wildly inflate
  `breakout_move_atr` on pure noise (traced to 3 of 4 hand-reviewed bad zones).
- **Zero-width guard**: a stale/flat print (`open==high==low==close`) can trivially pass the tight
  base-range check and produce a zero-width "zone" — rare (~0.03% of bars) but every one is a
  doomed trade (any stop against it sits inside normal noise). Rejected outright.

### `Zone.confirmed_ts` — why entries scan from here, not `breakout_ts`

A zone's `breakout_move_atr`/`breakout_volume_ratio` aren't knowable until the whole breakout leg
has been observed. Scanning for touches starting at `breakout_ts` (the leg's *start*) instead of
`confirmed_ts` (its *end*) lets a backtest "see" an entry in the 1–2 bars before a live system
could have known the zone existed at all — a real hindsight-bias bug, found by replaying history
through `zone_paper_trader.py` causally. Always scan from `confirmed_ts`.

## Entry confirmation (`strategies/entry_confirmation.py`)

Touching a zone is not enough to enter. Within a trailing `confirmation_window` (2 bars), **all
three** must be present:

1. **Touch** — price traded into the zone's `[price_low, price_high]` range at some point in the
   window.
2. **Reaction candle + volume, coupled** — some bar in the window shows a rejection pattern
   (`pin_bar` or `engulfing`, in the expected direction) **and** ≥ `reaction_min_volume_ratio`
   (1.3×) volume on that *same* bar. A candle shape without volume behind it isn't treated as a
   real reaction. A candidate reaction bar whose range blows past `MAX_REACTION_RANGE_MULT` (3.0×)
   the recent average range is rejected too — an unusually wide bar is an unreliable, effectively
   unfillable print, not an orderly rejection (found via a UPL trade that filled 0.4% away from the
   zone off exactly this kind of spike).
3. **RSI exhaustion, still true at entry** — RSI must be oversold (≤ 35, for a long) or overbought
   (≥ 65, for a short) on the **entry bar itself**, not merely somewhere in the window. Trades
   where the pattern+volume fired earlier but RSI had already unwound by entry are a coin flip
   (PF 0.42, n=8) vs. trades where RSI is still exhausted right at entry (PF 3.51, n=17) — momentum
   has to still be stretched when the trade is actually taken.

`confirmation_window=2` was chosen empirically (window=1: n=5, PF 0.91; window=2: n=16, PF 1.05;
window=3: n=16, PF 0.26 — window=3 gets the same trade-count lift by admitting stale signals, and
quality collapses). The three signals do **not** need to land on the same candle — requiring that
was tested and found to be a structural bottleneck (0.11 conversion rate regardless of universe
size), not a data-availability one.

## Multi-timeframe confluence (logged, not required)

1h zones are detected the same way as 5m zones. A 5-min zone whose price range overlaps a same-kind
1h reversal zone is tagged `confluent=True` — a strength signal, not a gate. Per hand-verified real
chart examples (RECLTD, SBIN), a clean 5-min-only reversal is a real, tradeable setup on its own;
a matching 1h zone just makes it a stronger one.

**Standalone 1h zones** — an hourly reversal zone with *no* overlapping 5m zone — are also scanned
for trades in their own right (`zone_source="1h"` in the trade log), using the same
`check_entry()` against 5m bars for tight, low-slippage entry timing. This exists because these
zones were previously detected and simply discarded (only ever used to set the `confluent` flag on
a matching 5m zone).

**Fixed 2026-09-14**: both confluent and standalone-1h trade counts were zero across the entire
universe for as long as this feature existed — traced (by instrumenting `find_zones()` with
rejection counters) to `run_symbol()`'s hourly call still enforcing the session-gap filter. That
filter's `gapped_at_start` rejection was hand-tuned against 5m bars (~75/session, chart-reviewed on
WIPRO/TRENT) where a base ending on the session's last bar is rare. At 1h density (~6-7
bars/session) it's routine: instrumented on RELIANCE, 263 of ~444 valid base candidates (59%) were
rejected for exactly this reason before any move/volume check even ran. `run_symbol()` now calls
`find_zones(hourly, timeframe="1h", enforce_gap_filter=False)` — the concern the filter protects
against (overnight gap-open volatility miscounted as a continuous intraday move) is specifically an
intraday-noise problem; a real overnight gap continuing into the next session is legitimate price
action at swing/1h scale, not contamination. See the comment at that call site in
`backtest/zone_backtest.py` for the full per-symbol numbers.

With the fix, a full-universe run (132 symbols, 2026-09-14) produced 40 standalone-1h trades and 1
confluent trade — both previously always zero. **This did not make the strategy look better.**
Combined with cost modeling (below), the same run's total P&L went from a misleadingly decent-looking
+2.31% gross to **-14.95% net** over 59 trades (30.5% win rate, PF 0.50) — the standalone-1h trades
specifically ran 35% win / PF 0.58 / -9.36%, dragging the total down rather than adding a hidden
edge. Read as: the bug was hiding data, not hiding an edge. Don't re-enable the gap filter for 1h
to make the numbers look better — that would be re-introducing the bug because the fixed number is
inconvenient, exactly the kind of after-the-fact re-tuning this project's own validation rules
warn against.

## Other logged (not yet filtering) confluence factors

- **MACD divergence** (`has_divergence`, per tradingsetupsreview.com) — bullish/bearish divergence
  detected on the same 5m window as the entry.
- **HTF momentum support** (`has_htf_momentum`, per Miner's Dual Time-Frame Momentum filter, see
  [docs/miner-hpts-notes.md](miner-hpts-notes.md)) — true if the most recently completed 1h bar's
  RSI is on the trade's side of overbought/oversold (≤30 / ≥70).

Both are logged on every trade specifically so their real effect can be checked empirically before
either is promoted to a hard filter or dropped. **As of the last full run, both undercut the
baseline** (divergence: 0-for-3; HTF support: 0-for-2) — too small a sample to trust, but a reason
not to add either as a filter yet.

## Multi-touch trading

A zone is no longer one-shot: a still-valid zone can produce multiple trades as price returns to
it (up to `MAX_TOUCHES`, default 3), retired early if price closes decisively through it
(`Zone.is_invalidated_by`, 0.2% buffer past the boundary). `touch_number` is recorded on every
trade specifically to let win-rate-by-touch be checked empirically rather than assumed.

## Position management

| Mechanism | Behavior |
|---|---|
| **Minimum risk floor** | Stop distance must be ≥ `MIN_RISK_ATR_MULT` (1.0×) the 5-min ATR at entry, or the touch is skipped and scanning continues. Forensics on an earlier run found 33/102 trades entered with a stop distance smaller than one typical bar's range — those stopped out within 1 bar 26/33 times (30% win rate vs. 55% for properly-spaced trades), i.e. normal noise was taking the stop regardless of thesis. |
| **Stop** | `zone.price_low × (1 − stop_buffer_pct)` for longs (mirror for shorts), 0.2% default buffer |
| **Target** | `entry_price ± risk × target_rr` (2.0R default) |
| **Breakeven** | Once favorable excursion reaches `BREAKEVEN_TRIGGER_R` (1.0R), stop moves to entry price |
| **Trailing** | Past breakeven, stop trails to protect `TRAIL_PROFIT_PCT` (50%) of peak favorable excursion — evaluated one bar *after* the excursion that set it, never same-bar (no assumed intrabar high/low ordering) |
| **Max hold** | `max_hold_bars` (1500, ≈ a few weeks of 5-min bars) — a backstop, not the primary exit |
| **Zero-volume bar guard** | A bar with `volume == 0` can't have genuinely triggered a stop/target even if its printed range looks real (found via a DRREDDY trade) — skipped, not treated as an exit |

## Transaction costs and slippage (modeled since 2026-08-13)

Every trade's `pnl_pct` in the output is **net** of a cost model — the raw price-to-price move is
kept separately as `gross_pnl_pct` for comparison. Two components:

- **`ROUND_TRIP_COST_PCT`** (0.22%) — a flat estimate of STT (0.1% each side for delivery/CNC,
  since this is a swing strategy, not intraday), NSE exchange transaction charge, SEBI turnover
  fee, stamp duty, and GST, summed both legs. AngelOne's brokerage on delivery is currently ~₹0 so
  it's excluded. This isn't paisa-precise tax accounting — the point is realistic drag exists at
  all, since the strategy's per-trade P&L (~0.2–1.2%) is the same order of magnitude as the costs.
- **Slippage**, applied asymmetrically by exit type: `SLIPPAGE_ENTRY_PCT` (0.03%) on every entry
  (a market-order-like fill once `check_entry` confirms), `SLIPPAGE_STOP_PCT` (0.05%) on
  `stop_loss`/`trailing_stop`/`max_hold` exits (all effectively market orders once triggered, which
  can gap through the intended level), and **zero** slippage on `take_profit` (a resting limit
  order fills at or better than its price by construction).

Run the backtest and check the "Gross vs net of costs" table in the printed breakdown — it directly
shows how much of the strategy's edge (if any) survives realistic costs. See
`ROUND_TRIP_COST_PCT`/`SLIPPAGE_*_PCT` in `backtest/zone_backtest.py` to adjust the assumptions.

Stop/target/breakeven/trailing constants (Kaufman *Trading Systems and Methods* Ch.23 + Elder's
Triple-Screen, see [docs/kaufman-tsam-notes.md](kaufman-tsam-notes.md)) have all been tuned by
repeated in-sample re-runs against the full cached dataset — fine during design (Kaufman Ch.21),
but it means nothing measured against that full range is a validated result. See below.

## Options overlay (live paper trading only, not in the backtest)

Every equity zone signal in `zone_paper_trader.py` **also** resolves and paper-trades a real,
listed single-stock option: long CE for a demand/long signal, long PE for a supply/short one.
Logged separately to `logs/zone_options_paper_trades.csv`. Added specifically to widen the
strategy's trade-frequency bottleneck — the equity signals alone are rare by construction (real
reversal zones don't form often), and options give the same rare signal a second, leveraged outlet
without touching any zone-quality filter.

- Expiry: nearest **monthly** (last Tuesday), at least `MIN_DTE_DAYS` (2) days out — NSE
  single-stock options are monthly-only (verified live 2026-08-07), American exercise.
  Strike: ATM (`offset_steps=0`) via `AngelOneClient.pick_strike`.
- The option's entry/exit price is its own **real live LTP** (`resolve_option` +
  `get_option_lot_size`/`get_strike_interval`, both read live from ScripMaster — lot sizes and
  strike spacing are NSE-revised periodically, never hardcoded) — not a theoretical/Black-Scholes
  price.
- The equity position's stop/target/trailing logic is unchanged and still decides **when** to
  exit; only the option's own LTP is used for the option's recorded P&L.
- Costs zero extra candle-fetch API calls (reuses the bars already fetched for the equity signal)
  — only an incremental option-chain lookup + LTP fetch on an actual signal.

## Symbol universe (`config.ZONE_SYMBOLS`)

Deliberately **separate** from `INDIA_SYMBOLS`/`INDIA_BLOCKLIST` — those were curated for the ORB
strategy specifically (grid-search PF, ORB backtest losses) and there's no reason a different
strategy should inherit that curation. E.g. ICICIBANK is ORB-blocklisted but produces a clean RBD
reversal zone matching a hand-verified real chart example. No blocklist exists yet for the zone
strategy — nothing has been backtested long enough to justify excluding a symbol this way.

As of 2026-08-07, expanded to the full **208-symbol** live NFO F&O-eligible universe (per
ScripMaster — itself NSE/SEBI's own liquidity screen), added because more instruments scanned in
parallel means more chances for a genuinely rare reversal zone to form; loosening the zone-quality
filters themselves was tested and made results worse, so trade frequency was a *surface-area*
problem, not a filter problem. Some additions are recent IPOs with thin history (SWIGGY,
PREMIERENE, WAAREEENER, …) — left in rather than pre-filtered, since the fetchers already tolerate
individual symbol failures.

**Coverage gap**: only symbols with cached 1h/5m parquet data in `backtest/data/` actually produce
trades. Check `ls backtest/data/*_NSE_5m.parquet | wc -l` against `len(config.ZONE_SYMBOLS)` before
trusting a "0 trades" result for a newly-added symbol — it may just mean the data hasn't been
fetched yet (`backtest/fetch_nse_multi_tf.py` / `fetch_nse_data_smartapi.py`), not that the symbol
has no zones.

## Validation status — read before trusting any number

- **Transaction costs are modeled in `zone_backtest.py`** (see "Transaction costs and slippage"
  above) as of 2026-08-13 — `pnl_pct` in its output is net of an estimated round-trip cost +
  slippage. **The paper traders (`zone_paper_trader.py`, `index_options_paper_trader.py`) do
  NOT yet apply this same cost model** to their logged P&L — they use real live prices for fills,
  but don't subtract STT/brokerage/slippage from the recorded result. Treat paper-trader P&L as
  gross, and the backtest's net-vs-gross comparison as the more honest read of what real costs
  would do to it.
- **In-sample/out-of-sample split**: `OOS_START` (2026-05-01) marks a holdout that must be used
  **exactly once**. If the OOS numbers look bad, the correct conclusion is "this rule set doesn't
  hold up," not "adjust one more threshold and re-check" — that exact re-tuning pattern already
  produced one result (`confirmation_window=2`'s early promise) that inverted on more data.
- **`zone_paper_trader.py` exists specifically because everything above is in-sample-tuned.** It
  runs the current, locked rule set forward against data it has never seen, with no further tuning
  permitted while it runs — the only genuinely out-of-sample read this strategy has ever had.
- Run `python -m backtest.zone_backtest` for current combined/IS/OOS/walk-forward stats — don't
  rely on a stale `backtest/results/zone_backtest_trades.csv` in the working tree; regenerate it.
