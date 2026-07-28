"""
Supply/demand zone detection — classic "base + breakout leg" definition,
with reversal-vs-continuation classification.

A zone is a short consolidation (the base) immediately followed by a sharp,
high-volume move away from it (the breakout leg). The base's price range
becomes the zone. There are four patterns, named by what happens before and
after the base:

  Rally-Base-Rally (RBR): up into the base, up out of it   -> demand, CONTINUATION
  Drop-Base-Rally  (DBR): down into the base, up out of it  -> demand, REVERSAL
  Drop-Base-Drop   (DBD): down into the base, down out      -> supply, CONTINUATION
  Rally-Base-Drop  (RBD): up into the base, down out of it  -> supply, REVERSAL

Reversal zones (DBR/RBD) are the powerful ones — the trend actually changes
direction there, which is a much stronger signal than a continuation zone
just extending a move that was already happening. But a "reversal" only
counts if the move INTO the base was a real rally/drop, not just drift: it
needs both a real price move AND real volume behind it (same principle as
the breakout leg below) — a price move on thin volume isn't a rally, it's
noise.

Zone strength is NOT just the size of the breakout move, and NOT just the
volume -- it's both together. A big move on average volume, or an
average-sized move on huge volume, is a weaker signal than a big move on
high volume at the same time. base strength = move_strength_in_atr *
volume_ratio (multiplicative, so a zone needs both factors to score highly,
not just one). Reversal zones then get their leg-in's own volume ratio
folded in too, so a reversal backed by a heavier-volume rally/drop scores
higher than one backed by a thinner one.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


@dataclass
class Zone:
    kind: str              # "demand" | "supply"
    pattern: str            # "RBR" | "DBR" | "DBD" | "RBD"
    price_low: float
    price_high: float
    base_start: pd.Timestamp
    base_end: pd.Timestamp
    breakout_ts: pd.Timestamp
    breakout_move_atr: float     # breakout leg's move, in multiples of ATR
    breakout_volume_ratio: float  # breakout leg's volume / recent average volume
    legin_move_atr: float = 0.0   # how many ATRs price moved INTO the base, signed
    legin_volume_ratio: float = 0.0  # avg volume / recent avg volume during the leg-in
    timeframe: str = ""     # set by caller: "1d" | "1h"
    strength: float = field(init=False)
    touches: int = field(default=0, init=False)   # updated during backtest (freshness)
    broken: bool = field(default=False, init=False)  # price closed decisively through it

    def __post_init__(self) -> None:
        base = self.breakout_move_atr * self.breakout_volume_ratio
        self.strength = round(base * self.legin_volume_ratio, 2) if self.is_reversal else round(base, 2)

    @property
    def is_reversal(self) -> bool:
        return self.pattern in ("DBR", "RBD")

    @property
    def mid(self) -> float:
        return (self.price_low + self.price_high) / 2

    def contains(self, price: float) -> bool:
        return self.price_low <= price <= self.price_high

    def is_invalidated_by(self, close: float, buffer_pct: float = 0.002) -> bool:
        """
        True if `close` breaks decisively through the zone, invalidating it as
        support/resistance going forward -- a demand zone whose low gets
        closed below, or a supply zone whose high gets closed above (with a
        small buffer so a wick/noise close doesn't falsely invalidate it).
        """
        if self.kind == "demand":
            return close < self.price_low * (1 - buffer_pct)
        return close > self.price_high * (1 + buffer_pct)


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
    legin_bars: int = 8,                 # how many bars before the base define the leg-in direction
    legin_min_move_atr: float = 1.0,     # min move (in ATR) to call the leg-in a real trend, not noise
    legin_min_volume_ratio: float = 1.1,  # leg-in avg volume >= this x recent avg volume (real rally, not drift)
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

        # --- determine the leg-IN direction: what was happening before the base ---
        # This is what separates a REVERSAL zone (trend flips here -- the powerful
        # kind, per DBR/RBD below) from a CONTINUATION zone (just extends a move
        # already in progress, RBR/DBD -- weaker). Compare price just before the
        # base to price a few bars further back. A real rally/drop needs BOTH a
        # real price move AND real volume behind it -- price drifting up on thin
        # volume isn't a rally, it's noise, and shouldn't count as a reversal.
        legin_start_idx = max(0, base_start - legin_bars)
        legin_slice = df.iloc[legin_start_idx:base_start]
        price_before_legin = df["close"].iloc[legin_start_idx]
        price_at_base = base_slice["close"].iloc[0]
        legin_move_atr = (price_at_base - price_before_legin) / atr
        if len(legin_slice) > 0 and legin_slice["avg_vol"].notna().any() and legin_slice["avg_vol"].max() > 0:
            legin_volume_ratio = float((legin_slice["volume"] / legin_slice["avg_vol"]).mean())
        else:
            legin_volume_ratio = 0.0
        legin_has_volume = legin_volume_ratio >= legin_min_volume_ratio
        legin_up = legin_move_atr >= legin_min_move_atr and legin_has_volume        # real rally into the base
        legin_down = legin_move_atr <= -legin_min_move_atr and legin_has_volume      # real drop into the base

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
                    pattern="DBR" if legin_down else "RBR",
                    price_low=float(base_low),
                    price_high=float(base_high),
                    base_start=base_slice.index[0],
                    base_end=base_slice.index[-1],
                    breakout_ts=leg.index[0],
                    breakout_move_atr=round(float(move_up), 2),
                    breakout_volume_ratio=round(float(volume_ratio), 2),
                    legin_move_atr=round(float(legin_move_atr), 2),
                    legin_volume_ratio=round(float(legin_volume_ratio), 2),
                    timeframe=timeframe,
                )
                zones.append(zone)
                i = leg_end
                continue
            if move_down >= breakout_min_move_atr:
                zone = Zone(
                    kind="supply",
                    pattern="RBD" if legin_up else "DBD",
                    price_low=float(base_low),
                    price_high=float(base_high),
                    base_start=base_slice.index[0],
                    base_end=base_slice.index[-1],
                    breakout_ts=leg.index[0],
                    breakout_move_atr=round(float(move_down), 2),
                    breakout_volume_ratio=round(float(volume_ratio), 2),
                    legin_move_atr=round(float(legin_move_atr), 2),
                    legin_volume_ratio=round(float(legin_volume_ratio), 2),
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
