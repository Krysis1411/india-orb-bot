"""
India ORB Strategy — NautilusTrader backtesting implementation.

Replays NSE equity 5-min bars through the same ORB signal logic as
india_orb_bot.py, tracking P&L in INR without options pricing.

Strategy (mirrors india_orb_bot.py exactly)
-------------------------------------------
  Opening range : first INDIA_ORB_RANGE_BARS × 5-min bars (09:15–09:45 IST)
  Entry         : close breaks above OR high AND volume ≥ avg_OR_vol × factor
  Stop          : OR low × (1 − stop_buffer_pct)
  Target        : OR high + (OR range × profit_multiplier)
  Trailing stop : after 0.5× target reached, stop moves to breakeven
  Nifty filter  : optional — skip entry when Nifty 50 is down on the day
  EOD close     : force-close at close_hour:close_minute IST

All filters from config.py are imported at module level so the parity
checker (check_backtest_parity.py) can verify live ↔ backtest alignment.

NSE session times in UTC (IST = UTC + 5:30)
-------------------------------------------
  09:15 IST = 03:45 UTC  (market open)
  09:45 IST = 04:15 UTC  (ORB window closes, earliest entry)
  13:00 IST = 07:30 UTC  (entry cutoff, INDIA_MAX_ENTRY_HOUR/MINUTE)
  15:15 IST = 09:45 UTC  (EOD close, INDIA_CLOSE_HOUR/MINUTE)
"""
from __future__ import annotations

from datetime import date, datetime, time as _time, timezone
from zoneinfo import ZoneInfo

from nautilus_trader.config import PositiveFloat, PositiveInt, StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy

from config import (
    INDIA_ALLOW_SHORTS,
    INDIA_CLOSE_HOUR,
    INDIA_CLOSE_MINUTE,
    INDIA_MAX_ENTRY_HOUR,
    INDIA_MAX_ENTRY_MINUTE,
    INDIA_ORB_MAX_OR_PCT,
    INDIA_ORB_MIN_OR_PCT,
    INDIA_ORB_PROFIT_MULTIPLIER,
    INDIA_ORB_RANGE_BARS,
    INDIA_ORB_STOP_BUFFER_PCT,
    INDIA_ORB_VOLUME_FACTOR,
    INDIA_POSITION_SIZE_INR,
    INDIA_SKIP_MONDAY_ENTRIES,
)

IST = ZoneInfo("Asia/Kolkata")

# ---------------------------------------------------------------------------
# UTC session boundary helpers
# IST = UTC + 5:30 (330 minutes ahead)
# ---------------------------------------------------------------------------

def _ist_to_utc_time(hour: int, minute: int) -> _time:
    total = hour * 60 + minute - 330
    return _time(total // 60, total % 60)


# NSE open: 09:15 IST → 03:45 UTC
_NSE_OPEN_UTC = _ist_to_utc_time(9, 15)

# ORB window end: INDIA_ORB_RANGE_BARS × 5 min after open
_ORB_END_UTC = _ist_to_utc_time(9, 15 + INDIA_ORB_RANGE_BARS * 5)

# Entry cutoff: INDIA_MAX_ENTRY_HOUR:INDIA_MAX_ENTRY_MINUTE IST
_ENTRY_CUTOFF_UTC = _ist_to_utc_time(INDIA_MAX_ENTRY_HOUR, INDIA_MAX_ENTRY_MINUTE)

# EOD close: INDIA_CLOSE_HOUR:INDIA_CLOSE_MINUTE IST
_EOD_CLOSE_UTC = _ist_to_utc_time(INDIA_CLOSE_HOUR, INDIA_CLOSE_MINUTE)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class IndiaORBConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    position_size_inr: float = float(INDIA_POSITION_SIZE_INR)
    orb_range_bars: PositiveInt = INDIA_ORB_RANGE_BARS
    profit_multiplier: PositiveFloat = INDIA_ORB_PROFIT_MULTIPLIER
    volume_factor: PositiveFloat = INDIA_ORB_VOLUME_FACTOR
    stop_buffer_pct: float = INDIA_ORB_STOP_BUFFER_PCT
    min_or_pct: float = INDIA_ORB_MIN_OR_PCT
    max_or_pct: float = INDIA_ORB_MAX_OR_PCT
    nifty_bar_type: BarType | None = None  # supply to enable Nifty trend filter
    trailing_stop: bool = True             # move stop to breakeven at 0.5× target
    allow_shorts: bool = INDIA_ALLOW_SHORTS  # trade breakouts below OR low as short sells
    breakout_strength_pct: float = 0.0    # min % price must clear OR boundary before entry


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

class IndiaORBStrategy(Strategy):
    """
    NautilusTrader strategy for India ORB equity intraday trading.
    No real orders are placed — trade results are tracked in self.trades.
    """

    def __init__(self, config: IndiaORBConfig) -> None:
        super().__init__(config)
        self.trades: list[dict] = []

        # Daily state (reset on each new trading date)
        self._current_date: date | None = None
        self._or_bars_seen: int = 0
        self._or_high: float | None = None
        self._or_low: float | None = None
        self._or_vol_sum: float = 0.0
        self._range_ready: bool = False
        self._range_skip: bool = False
        self._traded: bool = False

        # Open trade state
        self._open_trade: dict | None = None

        # Nifty trend (updated from nifty_bar_type bars)
        self._nifty_open: float | None = None
        self._nifty_last: float | None = None
        self._nifty_date: date | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_start(self) -> None:
        self.subscribe_bars(self.config.bar_type)
        if self.config.nifty_bar_type is not None:
            self.subscribe_bars(self.config.nifty_bar_type)

    def on_stop(self) -> None:
        pass

    # ------------------------------------------------------------------
    # Daily state reset
    # ------------------------------------------------------------------

    def _reset_day(self) -> None:
        self._or_bars_seen = 0
        self._or_high = None
        self._or_low = None
        self._or_vol_sum = 0.0
        self._range_ready = False
        self._range_skip = False
        self._traded = False
        # Force-close any open trade at EOD (exit recorded by _exit_trade)
        if self._open_trade is not None:
            # This should have been closed by EOD logic; if not, record a loss
            pass

    # ------------------------------------------------------------------
    # Time helpers
    # ------------------------------------------------------------------

    def _bar_utc(self, bar: Bar) -> datetime:
        return datetime.fromtimestamp(bar.ts_event / 1_000_000_000, tz=timezone.utc)

    def _bar_ist(self, bar: Bar) -> datetime:
        return self._bar_utc(bar).astimezone(IST)

    # ------------------------------------------------------------------
    # Nifty trend
    # ------------------------------------------------------------------

    def _handle_nifty(self, bar: Bar) -> None:
        dt = self._bar_ist(bar)
        if self._nifty_date != dt.date():
            self._nifty_date = dt.date()
            self._nifty_open = float(bar.open)
        self._nifty_last = float(bar.close)

    def _nifty_up(self) -> bool | None:
        """True if Nifty is positive on the day; None if no data."""
        if self.config.nifty_bar_type is None:
            return True   # no filter when Nifty feed not provided
        if self._nifty_open is None or self._nifty_last is None:
            return None
        return self._nifty_last >= self._nifty_open

    # ------------------------------------------------------------------
    # Trade recording
    # ------------------------------------------------------------------

    def _open_position(self, bar: Bar, price: float, stop: float,
                       target: float, qty: int, direction: str = "long") -> None:
        self._open_trade = dict(
            direction=direction,       # "long" or "short"
            entry_price=price,
            stop=stop,
            target=target,
            qty=qty,
            trailing_activated=False,
            entry_ts=bar.ts_event,
            entry_ist=self._bar_ist(bar).strftime("%H:%M"),
            entry_weekday=self._bar_ist(bar).strftime("%A"),
            or_high=self._or_high,
            or_low=self._or_low,
            or_range=self._or_high - self._or_low,
        )
        self._traded = True

    def _exit_trade(self, bar: Bar, exit_price: float, reason: str) -> None:
        if self._open_trade is None:
            return
        t = self._open_trade
        # P&L direction differs for long vs short
        if t["direction"] == "short":
            pnl = round((t["entry_price"] - exit_price) * t["qty"], 2)
            pnl_pct = round((t["entry_price"] - exit_price) / t["entry_price"] * 100, 3)
        else:
            pnl = round((exit_price - t["entry_price"]) * t["qty"], 2)
            pnl_pct = round((exit_price - t["entry_price"]) / t["entry_price"] * 100, 3)
        self.trades.append({
            "symbol":         str(self.config.instrument_id.symbol),
            "direction":      t["direction"],
            "entry_price":    t["entry_price"],
            "exit_price":     exit_price,
            "qty":            t["qty"],
            "pnl":            pnl,
            "pnl_pct":        pnl_pct,
            "exit_reason":    reason,
            "entry_time_ist": t["entry_ist"],
            "entry_weekday":  t["entry_weekday"],
            "or_range":       t["or_range"],
            "or_range_pct":   round(t["or_range"] / t["or_high"] * 100, 3),
            "entry_ts":       t["entry_ts"],
            "exit_ts":        bar.ts_event,
        })
        self._open_trade = None

    # ------------------------------------------------------------------
    # Main bar handler
    # ------------------------------------------------------------------

    def on_bar(self, bar: Bar) -> None:
        # Route Nifty bars to their own handler
        if self.config.nifty_bar_type and bar.bar_type == self.config.nifty_bar_type:
            self._handle_nifty(bar)
            return

        dt_utc  = self._bar_utc(bar)
        t_utc   = dt_utc.time().replace(second=0, microsecond=0)
        dt_ist  = dt_utc.astimezone(IST)
        today   = dt_ist.date()
        close   = float(bar.close)
        volume  = float(bar.volume)

        # ── New trading day ────────────────────────────────────────────
        if today != self._current_date:
            self._current_date = today
            self._reset_day()

        # ── Skip pre-market bars ───────────────────────────────────────
        if t_utc < _NSE_OPEN_UTC:
            return

        # ── EOD forced close ──────────────────────────────────────────
        if t_utc >= _EOD_CLOSE_UTC:
            if self._open_trade is not None:
                self._exit_trade(bar, close, "eod")
            return

        # ── Phase 1: accumulate ORB bars (09:15–09:45 IST) ───────────
        if t_utc < _ORB_END_UTC:
            if self._or_bars_seen == 0:
                # First bar of the day
                self._or_high = float(bar.high)
                self._or_low  = float(bar.low)
            else:
                self._or_high = max(self._or_high, float(bar.high))
                self._or_low  = min(self._or_low,  float(bar.low))
            self._or_vol_sum += volume
            self._or_bars_seen += 1
            return

        # ── Phase 2: finalise ORB on first post-window bar ────────────
        if not self._range_ready and not self._range_skip:
            if self._or_bars_seen < self.config.orb_range_bars:
                self._range_skip = True    # not enough bars — skip today
                return

            or_range = self._or_high - self._or_low
            or_pct   = or_range / self._or_high if self._or_high > 0 else 0
            if or_pct < self.config.min_or_pct:
                self._range_skip = True    # OR too narrow — flat/indecisive open
                return
            if self.config.max_or_pct > 0 and or_pct > self.config.max_or_pct:
                self._range_skip = True    # OR too wide — gap/spike day, stops blow out
                return

            self._range_ready = True
            self._avg_or_vol = self._or_vol_sum / self._or_bars_seen

        if self._range_skip:
            return

        or_range = self._or_high - self._or_low
        stop     = self._or_low  * (1 - self.config.stop_buffer_pct)
        target   = self._or_high + or_range * self.config.profit_multiplier

        # ── Phase 3: manage open position ─────────────────────────────
        if self._open_trade is not None:
            t   = self._open_trade
            is_long  = t["direction"] == "long"
            is_short = t["direction"] == "short"

            # Trailing stop: move stop to breakeven after 0.5× target reached
            if self.config.trailing_stop and not t["trailing_activated"]:
                if is_long:
                    half_target = t["or_high"] + or_range * self.config.profit_multiplier * 0.5
                    if close >= half_target:
                        t["stop"] = max(t["stop"], t["entry_price"])
                        t["trailing_activated"] = True
                else:
                    half_target = t["or_low"] - or_range * self.config.profit_multiplier * 0.5
                    if close <= half_target:
                        t["stop"] = min(t["stop"], t["entry_price"])
                        t["trailing_activated"] = True

            if is_long:
                if close <= t["stop"]:
                    self._exit_trade(bar, close, "trailing_stop" if t["trailing_activated"] else "stop_loss")
                elif close >= t["target"]:
                    self._exit_trade(bar, close, "take_profit")
            else:  # short
                if close >= t["stop"]:
                    self._exit_trade(bar, close, "trailing_stop" if t["trailing_activated"] else "stop_loss")
                elif close <= t["target"]:
                    self._exit_trade(bar, close, "take_profit")
            return

        # ── Phase 4: entry checks (shared gates) ──────────────────────
        if self._traded:
            return

        if INDIA_SKIP_MONDAY_ENTRIES and dt_ist.weekday() == 0:
            return

        if t_utc >= _ENTRY_CUTOFF_UTC:
            return

        vol_ok = volume >= self._avg_or_vol * self.config.volume_factor
        if not vol_ok:
            return

        nifty = self._nifty_up()

        min_strength = self.config.breakout_strength_pct

        # ── Long breakout: price above OR high, Nifty up ──────────────
        long_strength = (close - self._or_high) / self._or_high if self._or_high > 0 else 0
        if close > self._or_high and long_strength >= min_strength and nifty is not False:
            long_stop   = self._or_low  * (1 - self.config.stop_buffer_pct)
            long_target = self._or_high + or_range * self.config.profit_multiplier
            if close < long_target:   # not a stale breakout
                qty = max(1, int(self.config.position_size_inr / close))
                self._open_position(bar, close, long_stop, long_target, qty, "long")

        # ── Short breakout: price below OR low, Nifty down ────────────
        elif close < self._or_low and self.config.allow_shorts and nifty is not True:
            short_strength = (self._or_low - close) / self._or_low if self._or_low > 0 else 0
            if short_strength >= min_strength:
                short_stop   = self._or_high * (1 + self.config.stop_buffer_pct)
                short_target = self._or_low  - or_range * self.config.profit_multiplier
                if close > short_target and short_target > 0:   # not a stale breakout
                    qty = max(1, int(self.config.position_size_inr / close))
                    self._open_position(bar, close, short_stop, short_target, qty, "short")
