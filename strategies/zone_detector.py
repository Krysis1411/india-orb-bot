"""
Supply/demand zone detection — classic "base + breakout leg" definition.

A zone is a short consolidation (the base) immediately followed by a sharp,
high-volume move away from it (the breakout leg). The base's price range
becomes the zone:

  Demand zone: base -> strong move UP away from it (buyers overwhelmed sellers)
  Supply zone: base -> strong move DOWN away from it (sellers overwhelmed buyers)

Zone strength is NOT just the size of the move, and NOT just the volume --
it's both together. A big move on average volume, or average-sized move on
huge volume, is a weaker signal than a big move on high volume at the same
time. strength = move_strength_in_atr * volume_ratio (multiplicative, so a
zone needs both factors to score highly, not just one).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


@dataclass
class Zone:
    kind: str              # "demand" | "supply"
    price_low: float
    price_high: float
    base_start: pd.Timestamp
    base_end: pd.Timestamp
    breakout_ts: pd.Timestamp
    breakout_move_atr: float     # breakout leg's move, in multiples of ATR
    breakout_volume_ratio: float  # breakout leg's volume / recent average volume
    timeframe: str = ""     # set by caller: "1d" | "1h"
    strength: float = field(init=False)
    touches: int = field(default=0, init=False)   # updated during backtest (freshness)
    broken: bool = field(default=False, init=False)  # price closed decisively through it

    def __post_init__(self) -> None:
        self.strength = round(self.breakout_move_atr * self.breakout_volume_ratio, 2)

    @property
    def mid(self) -> float:
        return (self.price_low + self.price_high) / 2

    def contains(self, price: float) -> bool:
        return self.price_low <= price <= self.price_high


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=period).mean()


def find_zones(
    df: pd.DataFrame,
    timeframe: str = "",
    atr_period: int = 14,
    base_max_range_atr: float = 0.5,   # base candles must have range <= this x ATR (tight/indecisive)
    max_base_bars: int = 4,             # base is 1..N consecutive tight bars
    breakout_min_move_atr: float = 2.0,  # breakout leg must move >= this many ATRs away
    breakout_leg_bars: int = 3,          # breakout move can unfold over up to this many bars
    volume_lookback: int = 20,
    breakout_min_volume_ratio: float = 1.5,  # breakout volume >= this x recent avg volume
) -> list[Zone]:
    """
    Scan df (columns: open, high, low, close, volume; any DatetimeIndex) for
    base+breakout-leg patterns and return the zones found, oldest first.
    """
    df = df.copy()
    df["atr"] = _atr(df, atr_period)
    df["avg_vol"] = df["volume"].rolling(volume_lookback, min_periods=volume_lookback).mean()
    df["range"] = df["high"] - df["low"]

    zones: list[Zone] = []
    n = len(df)
    warmup = max(atr_period, volume_lookback)

    i = warmup
    while i < n - 1:
        atr = df["atr"].iloc[i]
        if not atr or pd.isna(atr) or atr <= 0:
            i += 1
            continue

        # --- find a base: 1..max_base_bars consecutive tight-range bars ---
        base_start = i
        j = i
        while (
            j < n
            and (j - base_start) < max_base_bars
            and df["range"].iloc[j] <= base_max_range_atr * atr
        ):
            j += 1
        base_end = j - 1
        if base_end < base_start:
            i += 1
            continue

        base_slice = df.iloc[base_start:base_end + 1]
        base_low = base_slice["low"].min()
        base_high = base_slice["high"].max()

        # --- check the next few bars (the "leg") for a breakout away from the base ---
        # Real breakout legs often unfold over 2-3 bars, not a single bar -- so we
        # look at a short window and take the cumulative move + the strongest
        # volume bar within it, rather than requiring everything in bar 1.
        breakout_idx = base_end + 1
        if breakout_idx >= n:
            break
        leg_end = min(breakout_idx + breakout_leg_bars, n)
        leg = df.iloc[breakout_idx:leg_end]
        if leg["avg_vol"].isna().all() or leg["avg_vol"].max() <= 0:
            i = breakout_idx
            continue

        leg_high = leg["high"].max()
        leg_low = leg["low"].min()
        volume_ratio = float((leg["volume"] / leg["avg_vol"]).max())
        move_up = (leg_high - base_high) / atr
        move_down = (base_low - leg_low) / atr

        if volume_ratio >= breakout_min_volume_ratio:
            if move_up >= breakout_min_move_atr and move_up >= move_down:
                zone = Zone(
                    kind="demand",
                    price_low=float(base_low),
                    price_high=float(base_high),
                    base_start=base_slice.index[0],
                    base_end=base_slice.index[-1],
                    breakout_ts=leg.index[0],
                    breakout_move_atr=round(float(move_up), 2),
                    breakout_volume_ratio=round(float(volume_ratio), 2),
                    timeframe=timeframe,
                )
                zones.append(zone)
                i = leg_end
                continue
            if move_down >= breakout_min_move_atr:
                zone = Zone(
                    kind="supply",
                    price_low=float(base_low),
                    price_high=float(base_high),
                    base_start=base_slice.index[0],
                    base_end=base_slice.index[-1],
                    breakout_ts=leg.index[0],
                    breakout_move_atr=round(float(move_down), 2),
                    breakout_volume_ratio=round(float(volume_ratio), 2),
                    timeframe=timeframe,
                )
                zones.append(zone)
                i = leg_end
                continue

        i += 1

    return zones


def find_confluence(daily_zones: list[Zone], hourly_zones: list[Zone]) -> list[tuple[Zone, Zone]]:
    """Pairs of (daily_zone, hourly_zone) whose price ranges overlap -- higher-confidence zones."""
    pairs = []
    for dz in daily_zones:
        for hz in hourly_zones:
            if dz.kind != hz.kind:
                continue
            if dz.price_low <= hz.price_high and hz.price_low <= dz.price_high:
                pairs.append((dz, hz))
    return pairs
