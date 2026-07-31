# Miner's "High Probability Trading Strategies" — notes for our zone strategy

Source: `docs/references/miner-high-probability-trading-strategies.{pdf,txt}` (Robert
C. Miner, Wiley 2009), kept permanently in this repo alongside the Kaufman book (see
[[reference_kaufman_book]] / `docs/kaufman-tsam-notes.md`) as a knowledge bank.

Read pass covered: Ch.2 "Multiple Time Frame Momentum Strategy" in full, Ch.3's
overlap guideline (trend-vs-correction pattern recognition), Ch.6 "Entry Strategies and
Position Size", Ch.7's exit-strategy concepts and multiple-unit trading. Skipped: Ch.4/5
(Miner's own Fibonacci price/time-projection system — a full Gann/Elliott framework,
philosophically a different system from ours, not a drop-in addition), Ch.8/9 (trader
case studies / business-of-trading, not technique).

## 1. Directly actionable — implemented this session

### 1a. Dual Time Frame Momentum filter (Ch.2)
Miner's core strategy, and the most concrete, testable idea in the book: trade direction
is set by a **higher timeframe's** momentum position, trade timing by a **lower
timeframe's** momentum reversal. Concretely (Table 2.2, stochastic-style OB/OS
indicator):

| Higher TF momentum | Lower TF signal |
|---|---|
| Bull, not OB | Long on a bullish reversal made *below* the OB zone |
| Bull, OB | No new long; a bearish reversal instead sets up a **short** |
| Bear, not OS | Short on a bearish reversal made *above* the OS zone |
| Bear, OS | No new short; a bullish reversal instead sets up a **long** |

The "Bull+OB → short" / "Bear+OS → long" rows are the important ones for us: Miner's
own rule set says a *countertrend/reversal* trade (which is what our zone strategy
always is — we only trade DBR/RBD reversal zones) is favored precisely when the higher
timeframe momentum is stretched into overbought/oversold, not just trending. That maps
directly onto our setup: a demand-zone long should be favored when the 1h momentum is
oversold (or turning up from oversold), a supply-zone short when 1h momentum is
overbought (or turning down). This is a different, complementary confluence factor to
the ones we already log (`confluent` zone overlap, `has_divergence`) — implemented as
`has_htf_momentum` in `backtest/zone_backtest.py` (1h RSI <=30 for longs / >=70 for
shorts as of the most recent completed 1h bar), logged and broken down exactly like the
others, **not required for entry**, per the project's standing rule of testing before
trusting a confluence factor.

**Empirical result (65-symbol run, 102 trades)**: HTF support present, n=9, PF 1.10,
+0.09% total; HTF support absent, n=93, PF 1.18, +2.93% total. Same pattern as MACD
divergence — a book/article-sourced confluence idea that does **not** show a clear
edge on our data (sample is small at n=9, so this isn't strong evidence it *hurts*
either, just that it gave no lift). Not promoted to a filter. Logged as a data point,
consistent with `kaufman-tsam-notes.md` §3's note that confluence ideas need the same
skeptical treatment regardless of source.

## 2. Validates decisions already made

- Miner's stop philosophy — "stops are always placed at the exact price that will void
  the setup... what can the market do that will void the very condition that prompted
  the trade? That is where the stop-loss is placed" (Ch.6) — is exactly our existing
  design: stop beyond the zone boundary (`Zone.is_invalidated_by`), because a decisive
  close through the zone is what invalidates the reversal thesis.
- "A market may run against an ideal setup... we will not be right all of the time. But
  we should be right most of the time, and when wrong, the cost is acceptable" (Ch.3) —
  same spirit as this project's insistence on honest, non-spun reporting of negative
  backtest results rather than cherry-picked examples.

## 3. New ideas — logged as candidates, not implemented (need their own empirical test)

- **No fixed price target — trail only** (Ch.7, Exit Strategy Concept 1): Miner argues
  against exiting at a predetermined price target at all ("a market will often blow
  right through a price target... let the market take you out... with a trailing
  stop"). Our current exit is target-OR-trailing-stop, whichever comes first. Dropping
  the fixed target entirely (pure trailing-stop exit) is a real candidate but a
  structural change to `run_symbol()`'s exit loop — needs its own before/after
  comparison, not a blind swap.
- **Multiple-unit exits** (Ch.7): split every trade into 2+ units — exit one quickly at
  a conservative target (protects against being wrong about the larger move), hold the
  rest with no fixed target, trailing-stop only. This is the more moderate version of
  the point above (partial profit-taking + let a runner ride) and is probably the
  better first thing to test if the "no fixed target" idea alone doesn't help, since it
  doesn't require betting the whole position on the trend continuing.
- **Swing-entry / trailing-one-bar breakout confirmation** (Ch.6): both of Miner's entry
  strategies require price to break out beyond a recent bar or swing point *after* the
  momentum reversal, before entering — not just touching the zone with pattern+RSI+
  volume confirmation on the same/recent bar (our current `check_entry` design). This
  adds a further "prove it" step that could cut down on reversals that touch, confirm,
  and then fail to actually follow through. Testable as an added condition in
  `strategies/entry_confirmation.py`, but changes what "entry price" means (breakout of
  a swing point vs. reaction-bar close) so it isn't a small tweak.
- **Overlap guideline for trend-vs-correction** (Ch.3): if price re-enters ("overlaps")
  the range of a prior swing section, that section was a correction, not a new trend —
  useful in principle for filtering out reversal-zone trades that are really just a
  correction inside a still-intact larger trend (which our zone detector, based purely
  on local base+breakout-leg geometry, currently has no way to distinguish). Not
  pursued: this needs real swing/wave-counting infrastructure (Elliott-style), which is
  a much bigger, more subjective lift than the other items here, and Miner's own
  approach relies on experienced discretionary judgment for wave counts more than the
  purely objective momentum/entry/exit rules elsewhere in the book.

## 4. Explicitly considered and rejected

- Miner's full Fibonacci price/time-projection system (Ch.4/5) — an entire proprietary
  Gann/Elliott framework for setting price and time targets. Adopting it wholesale would
  mean replacing our zone-based approach with a different system, not augmenting it.
  Not a fit for "add one confluence factor and test it."
- Risk/reward-ratio gatekeeping (Ch.7) — Miner explicitly argues *against* using a
  minimum reward:risk ratio as a trade filter, calling the reward side "always a best
  guess." We don't currently gate entries on R:R (target_rr is just where we place the
  exit, not a filter), so there's nothing to change here — noted for consistency, not
  actioned.
