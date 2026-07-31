# Kaufman's "Trading Systems and Methods" — notes for our zone strategy

Source: `docs/references/kaufman-trading-systems-and-methods.{pdf,txt}` (5th ed.), kept
permanently in this repo as a knowledge bank per explicit instruction. This doc is the
distilled, actionable subset — cite the chapter and go back to the full text for detail.

Read pass covered: Ch.2 (basics), Ch.3 "Charting" in full, Ch.4 "Charting Systems and
Techniques" (Wyckoff, DeMark, action/reaction & retracement theory), Ch.12 "Volume,
Open Interest, and Breadth" (volume indicators), Ch.16 "Day Trading" (key elements),
Ch.18 "Price Distribution Systems" (Steidlmayer's Market Profile), Ch.19 "Multiple Time
Frames" (Elder's Triple-Screen), Ch.21 "System Testing", Ch.23 "Risk Control" (stops,
profit targets, entry timing). Not read cover-to-cover — regression/cycle/spread/
seasonality chapters (Ch.6,7,10,11,13) are not relevant to a discretionary-style zone
strategy and were skipped.

## 1. Directly actionable — queued for implementation

### 1a. Trailing-stop / breakeven management (Ch.23 "Risk Control")
Our current exit is fixed-stop + fixed 2:1 target only. Kaufman's "Percentage of Profits"
trailing stop, combined with Elder's Triple-Screen breakeven rule (Ch.19, already known),
gives a concrete 3-stage design:

1. **Initial stop**: structural/volatility-based (ours: beyond the zone boundary +
   buffer). Kept as-is — Kaufman notes trend/breakout stops should relate to the pattern,
   not an arbitrary $ or % figure.
2. **Breakeven trigger**: "move the stop to breakeven as soon as possible" once the trade
   has moved a set amount in its favor. A stop that captures 50% of profit "is a sensible
   technique except at the beginning of a trade... this method can only be used after some
   profits have already accumulated — a trigger value. Prior to that, an initial stop
   would be based on price volatility. **Once the profit stop is triggered, the closer of
   the two stops is used.**" (p.1056)
3. **Trailing stop only ever ratchets favorably** — "the trailing stop is only advanced
   when the new stop is higher for longs or lower for shorts" (p.1062, Bollinger-band
   trading rules — same principle stated generally in the trailing-stop section).

This is what got implemented in `backtest/zone_backtest.py` (see below): breakeven at
1R profit, then trail to protect 50% of peak profit above breakeven, stop only moves in
the favorable direction.

### 1b. In-sample/out-of-sample discipline (Ch.21 "System Testing")
Already the trigger for this whole reading pass — see prior session notes. Core rule:
in-sample data can be "tortured without mercy" while designing rules, but out-of-sample
data must be used *exactly once*; using feedback from an OOS test to re-tune contaminates
it and "the result is always overfitting." Not yet set up for the zone strategy — every
finding so far has come from re-running on the same (growing) dataset. Do this before any
further parameter tuning.

## 2. Validates decisions already made (no code change, but useful as citation)

- **Reaction/volume-confirmed entries over blind zone touches**: Ch.23's "Using Timing
  for a Better Price" study found that waiting for a pullback / RSI-timed entry after a
  trend signal improved results across bonds/S&P/gold, cut trade count ~40% (fewer, better
  trades) — the same trade-off we found empirically choosing `confirmation_window=2` over
  wider windows.
- **Volume-weighted zone strength**: Kaufman's Head-and-Shoulders section (Ch.3) notes
  "declining volume on the head or the right shoulder... must be seen as a strong
  confirmation of a failing upwards move" — same principle as our `strength = move_atr *
  volume_ratio` formula, applied to a different pattern.
- **Multi-timeframe key levels**: Ch.16 "Trading Key Levels" — floor traders track
  yesterday's high/low/close, today's open/high/low, and older significant highs/lows;
  "when the same price satisfies more than one condition, there is greater confidence."
  Directly the same logic as our `find_confluence()` (daily+hourly zone overlap = higher
  confidence), just phrased for discretionary floor trading.
- **Failed breakout confirms the range, doesn't invalidate it** (Ch.3, trendline
  section) — a wick beyond a level that closes back inside strengthens that level rather
  than breaking it. Our `Zone.is_invalidated_by()` already requires a *close* beyond the
  buffer, not just a touch, which is consistent with this.

## 3. New ideas — not implemented, candidates for later (empirically test, don't assume)

- **Support/resistance role reversal** (Ch.3): broken resistance becomes new support and
  vice versa. Our `Zone` class doesn't currently re-classify a zone's kind after it's
  invalidated — a broken supply zone above price could theoretically become a demand zone
  on a retest. Worth testing as a *new* zone-detection mode, not a patch to the existing
  one (Oster, FRBNY Economic Policy Review, July 2000, cited by Kaufman: support/
  resistance levels shown empirically to remain valid for about 5 days — gives a concrete
  "how stale is too stale" number if we ever add a zone-age decay).
- **Retracement-based targets instead of fixed 2:1 R:R** (Ch.4, "Action and Reaction"):
  Tubbs' Law of Proportion / Gann / Fibonacci retracement theory — targets set as a
  proportion (½, 2/3, ¾, or 0.618/1.618) of the *prior* measured move, rather than a fixed
  R:R off the stop distance. Different theoretical basis for where price "should" go.
  Not implemented — would need its own empirical comparison against the current fixed
  2:1, not just swapped in.
- **OBV / Force Index / Money Flow Index as confluence factors** (Ch.12): same spirit as
  the MACD divergence check we already added — but note that MACD divergence, when
  actually tested on our 18-symbol data, made results *worse* (PF 0.45 vs 1.28 without),
  the opposite of what the source article claimed. Any new indicator confluence factor
  should get the same skeptical empirical treatment before being trusted, not added on
  the assumption that "more confluence = better."
- **Market Profile / value-area concept** (Ch.18, Steidlmayer): reframes support/
  resistance as *time spent at a price* (value area, ~70% of volume/TPOs) rather than a
  price level touched. Conceptually close to our base/consolidation zones, but needs
  proper time/price/volume-at-price data (not just 5-min OHLCV) to construct — a bigger
  lift, not scheduled.
- **DeMark's Sequential** (Ch.4): a completely different, counting-based (not
  trendline/pattern-based) exhaustion-reversal system (9-bar setup + 13-bar countdown vs.
  the close 4/2 bars back). Independent of zones entirely — could be evaluated as its own
  standalone signal generator someday, not a zone-strategy modification.

## 4. Explicitly considered and rejected

- Point-and-figure charting for day trading (Ch.16) — built for tick-driven floor
  trading with continuous intraday box/reversal plotting; doesn't fit a 5-min-bar swing
  setup without a much bigger data/infra change, and no clear edge over what we have.
- DeMark Sequential as a *replacement* entry trigger — it's a fully separate system
  (own setup/countdown/stop rules), not a drop-in confluence factor; mixing it into the
  zone pipeline would muddy which signal is doing the work.
