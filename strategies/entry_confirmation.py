"""
Entry confirmation for supply/demand zone trades.

Touching a zone isn't enough to enter (per the "reaction confirmation"
design decision) -- price has to actually react there, and that reaction
needs to be real, not a coin flip. Three independent checks, all required:

  1. Reaction candle  -- a rejection pattern (pin bar / engulfing) at the
                          zone, in the expected direction.
  2. RSI exhaustion    -- momentum should be stretched the way that makes a
                          reversal plausible (oversold at a demand zone,
                          overbought at a supply zone), not neutral.
  3. Reaction volume   -- the rejection candle itself needs above-average
                          volume, confirming real buying/selling showed up,
                          not just price drifting through on a quiet bar.

This runs on the 5-min execution timeframe against zones found on daily/1h
data (see zone_detector.py).
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from strategies.zone_detector import Zone

# Chart-review finding: UPL's touch-2 entry filled at 608.75 against a
# 605.70-606.15 zone -- 0.4% away -- because the "reaction" bar was a single
# 4.35-point-range spike (open 606.15, wicked down to 605.10 to touch the
# zone, then closed at 608.75 in the same 5-min bar). A bar with a range this
# far outside normal isn't a trustworthy rejection candle -- it's an
# unreliable, effectively unfillable print, not a real orderly reaction.
# Reject candidate reaction bars whose range blows past recent normal range.
MAX_REACTION_RANGE_MULT = 3.0


@dataclass
class ReactionSignal:
    ts: pd.Timestamp
    direction: str          # "long" | "short"
    pattern: str             # "pin_bar" | "engulfing"
    price: float             # confirmed entry reference price (the reaction bar's close)
    rsi: float
    volume_ratio: float


# ---------------------------------------------------------------------------
# RSI (Wilder's, standard 14-period)
# ---------------------------------------------------------------------------

def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    out = 100 - (100 / (1 + rs))
    return out.fillna(100)  # avg_loss == 0 -> maximally overbought, RSI 100


# ---------------------------------------------------------------------------
# Candlestick reaction patterns
# ---------------------------------------------------------------------------

def is_bullish_pin_bar(o: float, h: float, l: float, c: float, wick_ratio: float = 2.0) -> bool:
    """Small body, long lower wick (rejection of the downside), close in upper half."""
    body = abs(c - o)
    full_range = h - l
    if full_range <= 0:
        return False
    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)
    return (
        lower_wick >= wick_ratio * max(body, full_range * 0.01)
        and lower_wick > upper_wick
        and c >= l + full_range * 0.5   # close in upper half of the bar
    )


def is_bearish_pin_bar(o: float, h: float, l: float, c: float, wick_ratio: float = 2.0) -> bool:
    """Small body, long upper wick (rejection of the upside), close in lower half."""
    body = abs(c - o)
    full_range = h - l
    if full_range <= 0:
        return False
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    return (
        upper_wick >= wick_ratio * max(body, full_range * 0.01)
        and upper_wick > lower_wick
        and c <= h - full_range * 0.5   # close in lower half of the bar
    )


def is_bullish_engulfing(prev: tuple[float, float, float, float], cur: tuple[float, float, float, float]) -> bool:
    po, ph, pl, pc = prev
    co, ch, cl, cc = cur
    return pc < po and cc > co and cc >= po and co <= pc


def is_bearish_engulfing(prev: tuple[float, float, float, float], cur: tuple[float, float, float, float]) -> bool:
    po, ph, pl, pc = prev
    co, ch, cl, cc = cur
    return pc > po and cc < co and cc <= po and co >= pc


# ---------------------------------------------------------------------------
# Entry checker
# ---------------------------------------------------------------------------

def check_entry(
    bars_5m: pd.DataFrame,   # columns: open, high, low, close, volume; needs >= 20 bars of history before "now"
    zone: Zone,
    rsi_period: int = 14,
    rsi_oversold: float = 35.0,
    rsi_overbought: float = 65.0,
    volume_lookback: int = 20,
    reaction_min_volume_ratio: float = 1.3,
    confirmation_window: int = 2,
) -> ReactionSignal | None:
    """
    Check whether the reaction at this zone is confirmed as of the LAST bar
    in bars_5m. The three signals no longer need to land on the exact same
    candle -- requiring that turned out to be too strict (0.11 conversion
    rate from confluence pair to actual trade, unchanged across 13 vs 18
    symbols, i.e. a structural bottleneck, not a data one).

    Within the trailing `confirmation_window` bars:
      - the zone must have been touched at some point
      - some bar must show a reaction candle (pattern) AND above-average
        volume TOGETHER on that same bar (this pairing stays coupled --
        a candle shape without volume behind it still isn't a real reaction)
      - RSI must STILL be in exhaustion territory on the entry bar itself
        (the last bar in the window, where the trade actually fills) --
        chart-review forensics found trades where the pattern+volume+touch
        fired on an earlier bar but RSI had already unwound back toward
        neutral by the time entry executed: those trades are a coin flip
        (PF 0.42, n=8) vs trades where RSI is still exhausted right at entry
        (PF 3.51, n=17) on the same 25-trade active set. Momentum needs to
        still be stretched when you actually take the trade, not merely have
        been stretched a bar or two ago.

    Entry executes at the CURRENT (last) bar's close once all three are
    satisfied -- not retroactively at whichever bar supplied the pattern.

    confirmation_window=2 was chosen by comparing against the alternatives
    on the 18-symbol backtest, not guessed:
        window=1 (original, same-bar): n=5,  win 40.0%, PF 0.91, total -0.08%
        window=2:                      n=16, win 37.5%, PF 1.05, total +0.17%
        window=3:                      n=16, win 18.8%, PF 0.26, total -5.51%
    window=3 gets the same trade-count lift as window=2 but by admitting
    stale, disconnected signals -- quality collapses. window=2 gets the
    same lift while the profit factor actually crosses above 1.0.
    """
    if len(bars_5m) < max(rsi_period, volume_lookback) + confirmation_window + 1:
        return None

    direction = "long" if zone.kind == "demand" else "short"
    n = len(bars_5m)
    window = bars_5m.iloc[-confirmation_window:]

    touched = ((window["low"] <= zone.price_high) & (window["high"] >= zone.price_low)).any()
    if not touched:
        return None

    rsi_series = rsi(bars_5m["close"], rsi_period)
    entry_rsi = rsi_series.iloc[-1]
    if direction == "long":
        if not entry_rsi <= rsi_oversold:
            return None
    else:
        if not entry_rsi >= rsi_overbought:
            return None

    avg_vol = bars_5m["volume"].iloc[-(volume_lookback + confirmation_window):-confirmation_window].mean()
    if not avg_vol or avg_vol <= 0:
        return None
    avg_range = (bars_5m["high"] - bars_5m["low"]).iloc[-(volume_lookback + confirmation_window):-confirmation_window].mean()

    # Scan the window most-recent-first for a bar with BOTH pattern and volume.
    pattern, reaction_vol_ratio = None, 0.0
    for offset in range(confirmation_window - 1, -1, -1):   # most recent bar in the window first
        i = n - confirmation_window + offset
        cur, prev = bars_5m.iloc[i], bars_5m.iloc[i - 1]
        vol_ratio = float(cur["volume"] / avg_vol)
        if vol_ratio < reaction_min_volume_ratio:
            continue
        if avg_range and avg_range > 0 and (cur["high"] - cur["low"]) > MAX_REACTION_RANGE_MULT * avg_range:
            continue
        if direction == "long":
            if is_bullish_pin_bar(cur["open"], cur["high"], cur["low"], cur["close"]):
                pattern = "pin_bar"
            elif is_bullish_engulfing(
                (prev["open"], prev["high"], prev["low"], prev["close"]),
                (cur["open"], cur["high"], cur["low"], cur["close"]),
            ):
                pattern = "engulfing"
        else:
            if is_bearish_pin_bar(cur["open"], cur["high"], cur["low"], cur["close"]):
                pattern = "pin_bar"
            elif is_bearish_engulfing(
                (prev["open"], prev["high"], prev["low"], prev["close"]),
                (cur["open"], cur["high"], cur["low"], cur["close"]),
            ):
                pattern = "engulfing"
        if pattern is not None:
            reaction_vol_ratio = vol_ratio
            break

    if pattern is None:
        return None

    last = bars_5m.iloc[-1]
    return ReactionSignal(
        bars_5m.index[-1], direction, pattern, float(last["close"]),
        float(entry_rsi), reaction_vol_ratio,
    )
