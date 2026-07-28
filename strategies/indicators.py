"""
MACD + divergence detection — a confluence factor, not a requirement.

Per tradingsetupsreview.com's "Power of confluence" examples: the strongest
setups stack multiple INDEPENDENT signals (not just multi-timeframe zone
overlap) -- congestion zones, trendlines, and price/indicator divergence.
MACD bullish/bearish divergence is the first of those added here.

Divergence = price makes a new extreme (lower low / higher high) but the
indicator doesn't confirm it -- momentum is quietly weakening even though
price is still pushing in the same direction. Classic reversal tell.

Logged as a flag on each trade (has_divergence), same as `confluent` and
`touch_number` -- not required for entry. Whether it actually predicts
better trades is an empirical question for the backtest breakdown, not
something to assume because a trading blog says so.
"""
from __future__ import annotations

import pandas as pd


def macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Standard MACD. Returns (macd_line, signal_line, histogram)."""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def _local_lows(low: pd.Series, window: int = 3) -> pd.Series:
    """True where `low` is the minimum within a +/-window bar neighborhood."""
    return low == low.rolling(window * 2 + 1, center=True, min_periods=1).min()


def _local_highs(high: pd.Series, window: int = 3) -> pd.Series:
    return high == high.rolling(window * 2 + 1, center=True, min_periods=1).max()


def has_bullish_divergence(
    bars: pd.DataFrame, lookback: int = 40, swing_window: int = 3,
) -> bool:
    """
    True if, within the trailing `lookback` bars, price made a LOWER swing
    low than its prior swing low, while the MACD histogram made a HIGHER
    low at the same two points (momentum didn't confirm the new price low).
    """
    if len(bars) < lookback + 26:   # need MACD's slow EMA to have warmed up too
        return False
    window = bars.iloc[-lookback:]
    _, _, hist = macd(bars["close"])
    hist_window = hist.iloc[-lookback:]

    is_low = _local_lows(window["low"], swing_window)
    low_positions = [i for i, v in enumerate(is_low.values) if v]
    if len(low_positions) < 2:
        return False

    # last two distinct swing lows within the window
    p2 = low_positions[-1]
    p1 = low_positions[-2]
    if p1 == p2:
        return False

    price_lower_low = window["low"].iloc[p2] < window["low"].iloc[p1]
    macd_higher_low = hist_window.iloc[p2] > hist_window.iloc[p1]
    return bool(price_lower_low and macd_higher_low)


def has_bearish_divergence(
    bars: pd.DataFrame, lookback: int = 40, swing_window: int = 3,
) -> bool:
    """Mirror of has_bullish_divergence -- higher swing high in price, lower
    high in the MACD histogram."""
    if len(bars) < lookback + 26:
        return False
    window = bars.iloc[-lookback:]
    _, _, hist = macd(bars["close"])
    hist_window = hist.iloc[-lookback:]

    is_high = _local_highs(window["high"], swing_window)
    high_positions = [i for i, v in enumerate(is_high.values) if v]
    if len(high_positions) < 2:
        return False

    p2 = high_positions[-1]
    p1 = high_positions[-2]
    if p1 == p2:
        return False

    price_higher_high = window["high"].iloc[p2] > window["high"].iloc[p1]
    macd_lower_high = hist_window.iloc[p2] < hist_window.iloc[p1]
    return bool(price_higher_high and macd_lower_high)
