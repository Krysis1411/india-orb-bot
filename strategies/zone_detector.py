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
    confirmed_ts: pd.Timestamp    # last bar of the breakout leg -- the zone
    # isn't actually knowable until here, since breakout_move_atr/volume_ratio
    # are only measured once the leg is complete. Built live (paper trader),
    # a zone literally doesn't exist in memory before this bar closes -- so
    # any entry-scan starting from breakout_ts instead of confirmed_ts is
    # using hindsight a real-time system doesn't have. Found by building
    # zone_paper_trader.py and replaying real history through it causally:
    # one bar's difference invisibly moved a recorded backtest entry into a
    # window that would never have fired live.
    breakout_move_atr: float     # breakout leg's move, in multiples of ATR
    breakout_volume_ratio: float  # breakout leg's volume / recent average volume
    legin_move_atr: float = 0.0   # how many ATRs price moved INTO the base, signed
    legin_volume_ratio: float = 0.0  # avg volume / recent avg volume during the leg-in
    timeframe: str = ""     # set by caller: "1d" | "1h"
    # Whether this zone would survive the body-ratio / session-gap quality
    # filters even when they're not being enforced (find_zones(...,
    # enforce_body_filter=False, enforce_gap_filter=False)) -- lets a review
    # pass see the full candidate set with each one tagged, instead of only
    # ever seeing whichever config happened to run.
    passes_body_filter: bool = True
    passes_gap_filter: bool = True
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


def atr_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
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
    max_base_bars: int = 4,             # base is min_base_bars..N consecutive tight bars
    # Tested min_base_bars=2 (a published methodology's "2-5 candle base"
    # claim): collapses the whole 65-symbol universe to 5 trades (PF 3.99,
    # win 60%) -- directionally consistent with the idea but nowhere near
    # enough data to validate or reject it (one walk-forward fold had zero
    # trades). Kept as an opt-in toggle, not the default, until there's
    # enough history to judge it properly -- don't flip this default without
    # a lot more data behind it.
    min_base_bars: int = 1,
    base_min_body_ratio: float = 0.25,  # base candle(s) must be at least this much real body, not wick
    breakout_min_move_atr: float = 2.0,  # breakout leg must move >= this many ATRs away
    breakout_leg_bars: int = 3,          # breakout move can unfold over up to this many bars
    legin_bars: int = 8,                 # how many bars before the base define the leg-in direction
    legin_min_move_atr: float = 1.0,     # min move (in ATR) to call the leg-in a real trend, not noise
    legin_min_volume_ratio: float = 1.1,  # leg-in avg volume >= this x recent avg volume (real rally, not drift)
    volume_lookback: int = 20,
    breakout_min_volume_ratio: float = 1.5,  # breakout volume >= this x recent avg volume
    enforce_body_filter: bool = True,   # False: keep wick-dominated bases too, just tag them
    enforce_gap_filter: bool = True,    # False: keep session-gap-contaminated legs too, just tag them
) -> list[Zone]:
    """
    Scan df (columns: open, high, low, close, volume; any DatetimeIndex) for
    base+breakout-leg patterns and return the zones found, oldest first.
    """
    df = df.copy()
    df["atr"] = atr_series(df, atr_period)
    df["avg_vol"] = df["volume"].rolling(volume_lookback, min_periods=volume_lookback).mean()
    df["range"] = df["high"] - df["low"]
    df["body"] = (df["close"] - df["open"]).abs()

    # Chart-review finding (real trade forensics, not a guess): the breakout
    # leg window can span an overnight/weekend session boundary. The gap-open
    # + opening-auction volatility of the NEXT session then gets counted as
    # if it were a continuous intraday breakout move, wildly inflating
    # breakout_move_atr on pure gap noise -- 3 of 4 hand-reviewed bad zones
    # (WIPRO, both flagged TRENT zones) traced to exactly this. Detect session
    # boundaries from the timestamp gaps themselves (robust across timeframes)
    # rather than a price-based gap threshold.
    idx_series = df.index.to_series()
    typical_interval = idx_series.diff().median()
    is_new_session = (
        (idx_series.diff() > typical_interval * 3)
        if pd.notna(typical_interval) and typical_interval > pd.Timedelta(0)
        else pd.Series(False, index=df.index)
    )

    zones: list[Zone] = []
    n = len(df)
    warmup = max(atr_period, volume_lookback)

    i = warmup
    while i < n - 1:
        atr = df["atr"].iloc[i]
        if not atr or pd.isna(atr) or atr <= 0:
            i += 1
            continue

        # --- find a base: min_base_bars..max_base_bars consecutive tight-range bars ---
        # A bar can be "tight" in ATR terms while still being almost pure
        # wick (a doji/spinning-top spike) -- that's not a stable
        # consolidation shelf, it's a brief, low-conviction excursion, and a
        # zone boundary set by that wick tip is unreliable (chart-reviewed
        # example: UPL, base body was 22% of its range -- rejected here;
        # WIPRO's confirmed-good base was 31% -- kept). A published
        # institutional-zone methodology independently specifies a 2-5 candle
        # base (a single print isn't a real consolidation) -- min_base_bars
        # enforces the lower end of that.
        base_start = i
        j = i
        while j < n and (j - base_start) < max_base_bars:
            bar_range = df["range"].iloc[j]
            if bar_range <= 0 or bar_range > base_max_range_atr * atr:
                break
            body_ok = df["body"].iloc[j] / bar_range >= base_min_body_ratio
            if not body_ok and enforce_body_filter:
                break
            j += 1
        base_end = j - 1
        if base_end - base_start + 1 < min_base_bars:
            i += 1
            continue

        base_slice = df.iloc[base_start:base_end + 1]
        base_low = base_slice["low"].min()
        base_high = base_slice["high"].max()
        passes_body_filter = all(
            (df["body"].iloc[k] / df["range"].iloc[k]) >= base_min_body_ratio
            for k in range(base_start, base_end + 1)
            if df["range"].iloc[k] > 0
        )

        # A stale/flat print (open==high==low==close, no real trading) trivially
        # satisfies the tight-base range check and can single-handedly become a
        # zero-width "zone" -- not a real support/resistance level, just a data
        # artifact. Rare (~0.03% of bars) but every one it produces is a doomed
        # trade: any stop placed against a zero-width zone sits inside normal
        # noise. Require the base to have SOME real range.
        if base_high <= base_low:
            i = base_end + 1
            continue

        # --- determine the leg-IN direction: what was happening before the base ---
        # This is what separates a REVERSAL zone (trend flips here -- the powerful
        # kind, per DBR/RBD below) from a CONTINUATION zone (just extends a move
        # already in progress, RBR/DBD -- weaker). Compare price just before the
        # base to price a few bars further back. A real rally/drop needs BOTH a
        # real price move AND real volume behind it -- price drifting up on thin
        # volume isn't a rally, it's noise, and shouldn't count as a reversal.
        legin_start_idx = max(0, base_start - legin_bars)
        # Don't let the leg-in measurement bleed across a session boundary
        # either (same reasoning as the breakout leg below) -- if a new
        # session started partway through the lookback window, only the
        # current session's price action is a real "leg-in", not the gap.
        if enforce_gap_filter:
            for k in range(base_start - 1, legin_start_idx, -1):
                if is_new_session.iloc[k]:
                    legin_start_idx = k
                    break
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

        # If the very first leg bar already gapped in from a new session,
        # there's no same-session breakout here at all -- the base's last
        # bar and the "breakout" bar aren't even continuous price action.
        gapped_at_start = bool(is_new_session.iloc[breakout_idx])
        if gapped_at_start and enforce_gap_filter:
            i = breakout_idx
            continue

        leg_end = min(breakout_idx + breakout_leg_bars, n)
        gap_in_leg = False
        for k in range(breakout_idx + 1, leg_end):
            if is_new_session.iloc[k]:
                gap_in_leg = True
                if enforce_gap_filter:
                    leg_end = k
                break
        passes_gap_filter = not gapped_at_start and not gap_in_leg
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
                    confirmed_ts=leg.index[-1],
                    breakout_move_atr=round(float(move_up), 2),
                    breakout_volume_ratio=round(float(volume_ratio), 2),
                    legin_move_atr=round(float(legin_move_atr), 2),
                    legin_volume_ratio=round(float(legin_volume_ratio), 2),
                    timeframe=timeframe,
                    passes_body_filter=passes_body_filter,
                    passes_gap_filter=passes_gap_filter,
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
                    confirmed_ts=leg.index[-1],
                    breakout_move_atr=round(float(move_down), 2),
                    breakout_volume_ratio=round(float(volume_ratio), 2),
                    legin_move_atr=round(float(legin_move_atr), 2),
                    legin_volume_ratio=round(float(legin_volume_ratio), 2),
                    timeframe=timeframe,
                    passes_body_filter=passes_body_filter,
                    passes_gap_filter=passes_gap_filter,
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
