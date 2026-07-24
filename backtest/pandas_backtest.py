"""
Lightweight NautilusTrader-free backtest for the India ORB strategy.

Ports strategies/india_orb.py's exact signal logic (ORB direction bias, VWAP
proximity gate, EMA confluence boost, Dual Thrust gap-day filter, Parabolic
SAR trail) onto plain pandas DataFrames instead of NautilusTrader's engine.

Why this exists: getting NautilusTrader building locally required fighting a
broken PyPI sdist, a Rust toolchain version mismatch, then a full-workspace
compile that pulls in a dozen exchange adapters (Binance, Polymarket, etc.)
this bot doesn't use — more time and RAM than it's worth for validating a
straightforward bar-replay ORB strategy. NautilusTrader is worth revisiting
once there's an options bot here that needs its order-matching/execution
modeling; this strategy doesn't.

Usage
-----
    python -m backtest.pandas_backtest RELIANCE        # single symbol, full trade log
    python -m backtest.pandas_backtest --rank            # rank all INDIA_SYMBOLS
    python -m backtest.pandas_backtest --optimize         # grid search stop/mult
    python -m backtest.pandas_backtest --ab-test           # VWAP/EMA gate ON vs OFF
"""
from __future__ import annotations

import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.fetch_nse_data import fetch_nse_bars, load_nse_bars_df
from config import (
    INDIA_DUAL_THRUST_DAYS,
    INDIA_DUAL_THRUST_MAX_MULTIPLE,
    INDIA_ORB_BREAKOUT_STRENGTH_PCT,
    INDIA_ORB_MAX_OR_PCT,
    INDIA_ORB_MIN_OR_PCT,
    INDIA_ORB_PROFIT_MULTIPLIER,
    INDIA_ORB_RANGE_BARS,
    INDIA_ORB_STOP_BUFFER_PCT,
    INDIA_ORB_VOLUME_FACTOR,
    INDIA_POSITION_SIZE_INR,
    INDIA_SAR_AF_MAX,
    INDIA_SAR_AF_STEP,
    INDIA_SKIP_MONDAY_ENTRIES,
    INDIA_SYMBOLS,
)

IST = ZoneInfo("Asia/Kolkata")
DATA_DIR = Path(__file__).parent / "data"
RESULTS_DIR = Path(__file__).parent / "results"

_NSE_OPEN = pd.Timestamp("09:15").time()


# ---------------------------------------------------------------------------
# Strategy — plain-Python port of strategies/india_orb.py's IndiaORBStrategy
# ---------------------------------------------------------------------------

class IndiaORBPandasStrategy:
    """Bar-by-bar replica of the NautilusTrader strategy, no engine required."""

    def __init__(
        self,
        symbol: str,
        position_size_inr: float = INDIA_POSITION_SIZE_INR,
        orb_range_bars: int = INDIA_ORB_RANGE_BARS,
        profit_multiplier: float = INDIA_ORB_PROFIT_MULTIPLIER,
        volume_factor: float = INDIA_ORB_VOLUME_FACTOR,
        stop_buffer_pct: float = INDIA_ORB_STOP_BUFFER_PCT,
        min_or_pct: float = INDIA_ORB_MIN_OR_PCT,
        max_or_pct: float = INDIA_ORB_MAX_OR_PCT,
        trailing_stop: bool = True,
        allow_shorts: bool = True,
        breakout_strength_pct: float = INDIA_ORB_BREAKOUT_STRENGTH_PCT,
        use_vwap_ema: bool = True,
        vwap_sigma: float = 1.0,
        dual_thrust_days: int = INDIA_DUAL_THRUST_DAYS,
        dual_thrust_max_multiple: float = INDIA_DUAL_THRUST_MAX_MULTIPLE,
        use_sar: bool = True,
        sar_af_step: float = INDIA_SAR_AF_STEP,
        sar_af_max: float = INDIA_SAR_AF_MAX,
        skip_monday: bool = INDIA_SKIP_MONDAY_ENTRIES,
        max_entry_hour: int = 13,
        max_entry_minute: int = 0,
        close_hour: int = 15,
        close_minute: int = 10,
    ) -> None:
        self.symbol = symbol
        self.position_size_inr = position_size_inr
        self.orb_range_bars = orb_range_bars
        self.profit_multiplier = profit_multiplier
        self.volume_factor = volume_factor
        self.stop_buffer_pct = stop_buffer_pct
        self.min_or_pct = min_or_pct
        self.max_or_pct = max_or_pct
        self.trailing_stop = trailing_stop
        self.allow_shorts = allow_shorts
        self.breakout_strength_pct = breakout_strength_pct
        self.use_vwap_ema = use_vwap_ema
        self.vwap_sigma = vwap_sigma
        self.dual_thrust_days = dual_thrust_days
        self.dual_thrust_max_multiple = dual_thrust_max_multiple
        self.use_sar = use_sar
        self.sar_af_step = sar_af_step
        self.sar_af_max = sar_af_max
        self.skip_monday = skip_monday
        self.entry_cutoff = pd.Timestamp(f"{max_entry_hour:02d}:{max_entry_minute:02d}").time()
        self.eod_close = pd.Timestamp(f"{close_hour:02d}:{close_minute:02d}").time()

        self.trades: list[dict] = []

        self._current_date = None
        self._or_bars_seen = 0
        self._or_high = None
        self._or_low = None
        self._or_vol_sum = 0.0
        self._range_ready = False
        self._range_skip = False
        self._traded = False
        self._avg_or_vol = 0.0

        self._open_trade: dict | None = None

        self._orb_direction: str | None = None

        self._vwap_cum_tpv = 0.0
        self._vwap_cum_vol = 0.0
        self._vwap_tp_sum = 0.0
        self._vwap_tp_sumsq = 0.0
        self._vwap_tp_count = 0

        self._ema9 = None
        self._ema21 = None
        self._ema_bars_seen = 0

        self._daily_history: list[tuple[float, float, float]] = []
        self._day_high = None
        self._day_low = None
        self._day_close = None

    # ------------------------------------------------------------------
    def _reset_day(self) -> None:
        self._or_bars_seen = 0
        self._or_high = None
        self._or_low = None
        self._or_vol_sum = 0.0
        self._range_ready = False
        self._range_skip = False
        self._traded = False
        self._orb_direction = None
        self._vwap_cum_tpv = 0.0
        self._vwap_cum_vol = 0.0
        self._vwap_tp_sum = 0.0
        self._vwap_tp_sumsq = 0.0
        self._vwap_tp_count = 0
        self._ema9 = None
        self._ema21 = None
        self._ema_bars_seen = 0

    def _roll_daily_history(self) -> None:
        if self._day_high is not None:
            self._daily_history.append((self._day_high, self._day_low, self._day_close))
            if len(self._daily_history) > self.dual_thrust_days:
                self._daily_history.pop(0)
        self._day_high = None
        self._day_low = None
        self._day_close = None

    # ------------------------------------------------------------------
    def _update_vwap(self, high, low, close, volume) -> None:
        tp = (high + low + close) / 3.0
        if volume > 0:
            self._vwap_cum_tpv += tp * volume
            self._vwap_cum_vol += volume
        self._vwap_tp_sum += tp
        self._vwap_tp_sumsq += tp * tp
        self._vwap_tp_count += 1

    def _vwap_band(self):
        if self._vwap_cum_vol == 0 or self._vwap_tp_count < 2:
            return None
        vwap = self._vwap_cum_tpv / self._vwap_cum_vol
        mean = self._vwap_tp_sum / self._vwap_tp_count
        var = max(0.0, self._vwap_tp_sumsq / self._vwap_tp_count - mean * mean)
        std = var ** 0.5
        return vwap, max(std, vwap * 0.001)

    def _in_vwap_zone(self, price) -> bool:
        band = self._vwap_band()
        if band is None:
            return True
        vwap, std = band
        return abs(price - vwap) <= self.vwap_sigma * std

    def _update_ema(self, close) -> None:
        self._ema_bars_seen += 1
        if self._ema9 is None:
            self._ema9 = close
            self._ema21 = close
        else:
            self._ema9 = 0.2 * close + 0.8 * self._ema9
            self._ema21 = (2 / 22) * close + (20 / 22) * self._ema21

    def _ema_agrees(self, direction: str) -> bool:
        if self._ema_bars_seen < 9 or self._ema9 is None:
            return False
        if direction == "long":
            return self._ema9 > self._ema21
        return self._ema9 < self._ema21

    def _pullback_in_uptrend(self) -> bool:
        return (
            self._ema_bars_seen >= 9
            and self._ema9 is not None
            and self._ema21 is not None
            and self._ema9 > self._ema21
        )

    def _dual_thrust_range(self):
        n = self.dual_thrust_days
        if len(self._daily_history) < n:
            return None
        window = self._daily_history[-n:]
        highs = [h for h, _, _ in window]
        lows = [l for _, l, _ in window]
        closes = [c for _, _, c in window]
        hh, ll = max(highs), min(lows)
        hc, lc = max(closes), min(closes)
        return max(hh - lc, hc - ll)

    # ------------------------------------------------------------------
    def _sar_init(self, trade, initial_stop, entry_price) -> None:
        trade["sar"] = initial_stop
        trade["sar_extreme"] = entry_price
        trade["sar_af"] = self.sar_af_step

    def _sar_update(self, trade, current_price, is_long):
        sar = trade["sar"]
        extreme = trade["sar_extreme"]
        af = trade["sar_af"]

        if is_long:
            if current_price > extreme:
                extreme = current_price
                af = min(af + self.sar_af_step, self.sar_af_max)
        else:
            if current_price < extreme:
                extreme = current_price
                af = min(af + self.sar_af_step, self.sar_af_max)

        new_sar = sar + af * (extreme - sar)
        new_sar = min(new_sar, current_price) if is_long else max(new_sar, current_price)

        trade["sar"] = new_sar
        trade["sar_extreme"] = extreme
        trade["sar_af"] = af

        fired = (current_price <= new_sar) if is_long else (current_price >= new_sar)
        return new_sar, fired

    # ------------------------------------------------------------------
    def _open_position(self, ts_ist, price, stop, target, qty, direction) -> None:
        self._open_trade = dict(
            direction=direction,
            entry_price=price,
            stop=stop,
            target=target,
            qty=qty,
            trailing_activated=False,
            entry_ts=ts_ist,
            entry_ist=ts_ist.strftime("%H:%M"),
            entry_weekday=ts_ist.strftime("%A"),
            or_high=self._or_high,
            or_low=self._or_low,
            or_range=self._or_high - self._or_low,
        )
        if self.use_sar:
            self._sar_init(self._open_trade, stop, price)
        self._traded = True

    def _exit_trade(self, ts_ist, exit_price, reason) -> None:
        if self._open_trade is None:
            return
        t = self._open_trade
        if t["direction"] == "short":
            pnl = round((t["entry_price"] - exit_price) * t["qty"], 2)
            pnl_pct = round((t["entry_price"] - exit_price) / t["entry_price"] * 100, 3)
        else:
            pnl = round((exit_price - t["entry_price"]) * t["qty"], 2)
            pnl_pct = round((exit_price - t["entry_price"]) / t["entry_price"] * 100, 3)
        self.trades.append({
            "symbol": self.symbol,
            "direction": t["direction"],
            "entry_price": t["entry_price"],
            "exit_price": exit_price,
            "qty": t["qty"],
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "exit_reason": reason,
            "entry_time_ist": t["entry_ist"],
            "entry_weekday": t["entry_weekday"],
            "or_range": t["or_range"],
            "or_range_pct": round(t["or_range"] / t["or_high"] * 100, 3),
            "entry_ts": t["entry_ts"],
            "exit_ts": ts_ist,
        })
        self._open_trade = None

    # ------------------------------------------------------------------
    def on_bar(self, ts_ist, high, low, close, volume, orb_end_time, nifty_up) -> None:
        today = ts_ist.date()
        t_ist = ts_ist.time()

        if today != self._current_date:
            self._roll_daily_history()
            self._current_date = today
            self._reset_day()

        self._day_high = high if self._day_high is None else max(self._day_high, high)
        self._day_low = low if self._day_low is None else min(self._day_low, low)
        self._day_close = close
        self._update_vwap(high, low, close, volume)
        self._update_ema(close)

        if t_ist >= self.eod_close:
            if self._open_trade is not None:
                self._exit_trade(ts_ist, close, "eod")
            return

        if t_ist < orb_end_time:
            if self._or_bars_seen == 0:
                self._or_high = high
                self._or_low = low
            else:
                self._or_high = max(self._or_high, high)
                self._or_low = min(self._or_low, low)
            self._or_vol_sum += volume
            self._or_bars_seen += 1
            return

        if not self._range_ready and not self._range_skip:
            if self._or_bars_seen < self.orb_range_bars:
                self._range_skip = True
                return
            or_range = self._or_high - self._or_low
            or_pct = or_range / self._or_high if self._or_high > 0 else 0
            if or_pct < self.min_or_pct:
                self._range_skip = True
                return
            if self.max_or_pct > 0 and or_pct > self.max_or_pct:
                self._range_skip = True
                return
            dt_range = self._dual_thrust_range()
            if dt_range is not None and or_range > self.dual_thrust_max_multiple * dt_range:
                self._range_skip = True
                return
            self._range_ready = True
            self._avg_or_vol = self._or_vol_sum / self._or_bars_seen

        if self._range_skip:
            return

        or_range = self._or_high - self._or_low

        if self._open_trade is not None:
            t = self._open_trade
            is_long = t["direction"] == "long"

            if self.trailing_stop and not t["trailing_activated"]:
                if is_long:
                    half_target = t["or_high"] + or_range * self.profit_multiplier * 0.5
                    if close >= half_target:
                        t["stop"] = max(t["stop"], t["entry_price"])
                        t["trailing_activated"] = True
                else:
                    half_target = t["or_low"] - or_range * self.profit_multiplier * 0.5
                    if close <= half_target:
                        t["stop"] = min(t["stop"], t["entry_price"])
                        t["trailing_activated"] = True

            sar_fired = False
            if self.use_sar and "sar" in t:
                _, sar_fired = self._sar_update(t, close, is_long)

            if is_long:
                hit_stop = close <= t["stop"]
                hit_target = close >= t["target"]
            else:
                hit_stop = close >= t["stop"]
                hit_target = close <= t["target"]

            if hit_stop or hit_target or sar_fired:
                if hit_target:
                    reason = "take_profit"
                elif hit_stop:
                    reason = "trailing_stop" if t["trailing_activated"] else "stop_loss"
                else:
                    reason = "sar_trail"
                self._exit_trade(ts_ist, close, reason)
            return

        if self._traded:
            return
        if self.skip_monday and ts_ist.weekday() == 0:
            return
        if t_ist >= self.entry_cutoff:
            return

        vol_ok = volume >= self._avg_or_vol * self.volume_factor
        if not vol_ok:
            return

        min_strength = self.breakout_strength_pct

        long_strength = (close - self._or_high) / self._or_high if self._or_high > 0 else 0
        if close > self._or_high and long_strength >= min_strength and nifty_up is not False:
            long_stop = self._or_low * (1 - self.stop_buffer_pct)
            long_target = self._or_high + or_range * self.profit_multiplier
            if close >= long_target:
                return
            if not self.use_vwap_ema:
                qty = max(1, int(self.position_size_inr / close))
                self._open_position(ts_ist, close, long_stop, long_target, qty, "long")
                return
            if self._orb_direction is None:
                self._orb_direction = "long"
                return
            if self._orb_direction != "long":
                return
            if not self._in_vwap_zone(close):
                return
            qty = max(1, int(self.position_size_inr * (1.5 if self._ema_agrees("long") else 1.0) / close))
            self._open_position(ts_ist, close, long_stop, long_target, qty, "long")
            return

        elif close < self._or_low and self.allow_shorts and nifty_up is not True:
            short_strength = (self._or_low - close) / self._or_low if self._or_low > 0 else 0
            if short_strength < min_strength:
                return
            short_stop = self._or_high * (1 + self.stop_buffer_pct)
            short_target = self._or_low - or_range * self.profit_multiplier
            if short_target <= 0 or close <= short_target:
                return
            if not self.use_vwap_ema:
                qty = max(1, int(self.position_size_inr / close))
                self._open_position(ts_ist, close, short_stop, short_target, qty, "short")
                return
            if self._pullback_in_uptrend():
                return
            if self._orb_direction is None:
                self._orb_direction = "short"
                return
            if self._orb_direction != "short":
                return
            if not self._in_vwap_zone(close):
                return
            qty = max(1, int(self.position_size_inr * (1.5 if self._ema_agrees("short") else 1.0) / close))
            self._open_position(ts_ist, close, short_stop, short_target, qty, "short")


# ---------------------------------------------------------------------------
# Data prep
# ---------------------------------------------------------------------------

def _orb_end_time(orb_range_bars: int) -> "pd.Timestamp.time":
    total_min = 9 * 60 + 15 + orb_range_bars * 5
    return pd.Timestamp(f"{total_min // 60:02d}:{total_min % 60:02d}").time()


def _load_nifty_trend(date_from=None, date_to=None) -> pd.Series | None:
    """Returns a Series indexed by UTC timestamp: True/False/None (nifty up on the day)."""
    path = DATA_DIR / "NIFTY50_NSE_5m.parquet"
    if not path.exists():
        return None
    df = load_nse_bars_df(path)
    if date_from:
        df = df[df.index >= pd.Timestamp(date_from, tz="UTC")]
    if date_to:
        df = df[df.index < pd.Timestamp(date_to, tz="UTC")]
    ist_idx = df.index.tz_convert(IST)
    dates = ist_idx.normalize()
    day_open = df.groupby(dates)["open"].transform("first")
    nifty_up = df["close"] >= day_open
    nifty_up.index = df.index
    return nifty_up


def run_symbol(symbol: str, nifty_up: pd.Series | None = None, date_from=None, date_to=None,
                **strategy_kwargs) -> list[dict]:
    path = DATA_DIR / f"{symbol}_NSE_5m.parquet"
    if not path.exists():
        path = fetch_nse_bars(symbol, DATA_DIR)
    df = load_nse_bars_df(path)
    if date_from:
        df = df[df.index >= pd.Timestamp(date_from, tz="UTC")]
    if date_to:
        df = df[df.index < pd.Timestamp(date_to, tz="UTC")]
    df = df.between_time("03:45", "10:00")  # NSE hours in UTC
    if len(df) < 50:
        return []

    orb_range_bars = strategy_kwargs.get("orb_range_bars", INDIA_ORB_RANGE_BARS)
    orb_end = _orb_end_time(orb_range_bars)

    strat = IndiaORBPandasStrategy(symbol, **strategy_kwargs)
    ist_index = df.index.tz_convert(IST)

    # nifty_up is indexed by UTC timestamp (see _load_nifty_trend) — look up
    # against the original (UTC) df.index, not the IST-converted one.
    for ts_utc, ts_ist, row in zip(df.index, ist_index, df.itertuples(index=False)):
        if nifty_up is not None:
            nu = nifty_up.get(ts_utc)
            nu = True if nu is None else bool(nu)
        else:
            nu = True  # no Nifty filter configured -> no restriction
        strat.on_bar(ts_ist, row.high, row.low, row.close, row.volume, orb_end, nu)

    return strat.trades


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def _summarize(symbol: str, trades: list[dict]) -> dict:
    if not trades:
        return dict(symbol=symbol, trades=0, wins=0, losses=0, win_rate=0.0,
                    total_pnl=0.0, avg_pnl=0.0, best=0.0, worst=0.0, max_dd=0.0)
    pnls = [t["pnl"] for t in trades]
    n = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    cumsum, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        cumsum += p
        peak = max(peak, cumsum)
        max_dd = max(max_dd, peak - cumsum)
    return dict(
        symbol=symbol, trades=n, wins=wins, losses=n - wins,
        win_rate=round(wins / n * 100, 1), total_pnl=round(sum(pnls), 2),
        avg_pnl=round(sum(pnls) / n, 2), best=round(max(pnls), 2),
        worst=round(min(pnls), 2), max_dd=round(max_dd, 2),
    )


def _full_stats(trades: list[dict]) -> dict:
    import math
    import statistics as _stats
    from collections import defaultdict

    pnls = [t["pnl"] for t in trades]
    n = len(pnls)
    if n == 0:
        return {}

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    p_win = len(wins) / n

    z = 1.96
    denom = 1 + z ** 2 / n
    center = p_win + z ** 2 / (2 * n)
    margin = z * math.sqrt(p_win * (1 - p_win) / n + z ** 2 / (4 * n ** 2))
    ci_lo = max(0.0, (center - margin) / denom * 100)
    ci_hi = min(100.0, (center + margin) / denom * 100)

    gross_wins = sum(wins)
    gross_losses = abs(sum(losses))
    profit_factor = gross_wins / (gross_losses or 0.001)
    avg_win = gross_wins / len(wins) if wins else 0.0
    avg_loss = gross_losses / len(losses) if losses else 0.0
    expectancy = p_win * avg_win - (1 - p_win) * avg_loss

    daily_pnl: dict = defaultdict(float)
    for t in trades:
        day = t["entry_ts"].date()
        daily_pnl[day] += t["pnl"]
    daily_pnls = list(daily_pnl.values())
    if len(daily_pnls) > 1:
        mean_d = _stats.mean(daily_pnls)
        std_d = _stats.stdev(daily_pnls)
        sharpe = (mean_d / std_d * math.sqrt(252)) if std_d > 0 else 0.0
    else:
        sharpe = 0.0

    max_cl, cur_cl = 0, 0
    for p in pnls:
        if p <= 0:
            cur_cl += 1
            max_cl = max(max_cl, cur_cl)
        else:
            cur_cl = 0

    cumsum, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        cumsum += p
        peak = max(peak, cumsum)
        max_dd = max(max_dd, peak - cumsum)

    return dict(
        n=n, wins=len(wins), losses=len(losses), win_rate=round(p_win * 100, 1),
        ci_lo=round(ci_lo, 1), ci_hi=round(ci_hi, 1),
        profit_factor=round(profit_factor, 2), avg_win=round(avg_win, 2),
        avg_loss=round(avg_loss, 2), expectancy=round(expectancy, 2),
        sharpe=round(sharpe, 2), total_pnl=round(sum(pnls), 2),
        avg_pnl=round(sum(pnls) / n, 2), max_dd=round(max_dd, 2),
        max_consec_losses=max_cl,
    )


def _print_full_stats(stats: dict, label: str = "Portfolio") -> None:
    if not stats:
        print("  No trades.")
        return
    print(f"\n  -- {label} Statistics --------------------------------------")
    print(f"  Trades              : {stats['n']}")
    print(f"  Win rate            : {stats['win_rate']:.1f}%  (95% CI: {stats['ci_lo']:.1f}%-{stats['ci_hi']:.1f}%)")
    print(f"  Profit factor       : {stats['profit_factor']:.2f}  (edge if > 1.0, strong if > 1.5)")
    print(f"  Expectancy / trade  : Rs{stats['expectancy']:+,.2f}")
    print(f"  Avg win / avg loss  : Rs{stats['avg_win']:+,.2f}  /  Rs{-stats['avg_loss']:+,.2f}")
    print(f"  Sharpe (annualised) : {stats['sharpe']:.2f}  (>1.0 good, >2.0 excellent)")
    print(f"  Max consec losses   : {stats['max_consec_losses']}")
    print(f"  Max drawdown        : Rs{stats['max_dd']:,.2f}")
    print(f"  Total P&L           : Rs{stats['total_pnl']:+,.2f}")
    print(f"  --------------------------------------------------------------")


# ---------------------------------------------------------------------------
# Single-symbol detail
# ---------------------------------------------------------------------------

def run_single(symbol: str, nifty_up, **kwargs) -> None:
    trades = run_symbol(symbol, nifty_up, **kwargs)
    if not trades:
        print(f"{symbol}: no trades triggered.")
        return
    print(f"\n{'='*80}\n  India ORB Backtest (pandas engine) - {symbol}\n{'='*80}\n")
    print(f"  {'Entry':>9} {'Exit':>9} {'Qty':>5} {'P&L':>10} {'P&L%':>7}  {'Day':<10} {'Time':>6}  Reason")
    print(f"  {'-'*75}")
    for t in trades:
        print(f"  {t['entry_price']:>9.2f}  {t['exit_price']:>9.2f}  {t['qty']:>5}"
              f"  {t['pnl']:>+10.2f}  {t['pnl_pct']:>+6.2f}%  {t['entry_weekday']:<10}"
              f"  {t['entry_time_ist']:>6}  {t['exit_reason']}")
    s = _summarize(symbol, trades)
    print(f"\n  Trades: {s['trades']}  Wins: {s['wins']}  Win%: {s['win_rate']:.0f}%  "
          f"Total: Rs{s['total_pnl']:+,.2f}  Avg: Rs{s['avg_pnl']:+,.2f}  MaxDD: Rs{s['max_dd']:,.2f}")
    _print_full_stats(_full_stats(trades), label=symbol)


# ---------------------------------------------------------------------------
# Ranking across symbols
# ---------------------------------------------------------------------------

def run_ranking(symbols: list[str], nifty_up, label: str | None = None, **kwargs) -> None:
    RESULTS_DIR.mkdir(exist_ok=True)
    print(f"\nIndia ORB Ranking (pandas engine) - {len(symbols)} symbols\n")

    results, all_trades = [], []
    for i, sym in enumerate(symbols, 1):
        print(f"[{i:>2}/{len(symbols)}] {sym:<12}", end="  ", flush=True)
        trades = run_symbol(sym, nifty_up, **kwargs)
        r = _summarize(sym, trades)
        results.append(r)
        all_trades.extend(trades)
        if r["trades"] == 0:
            print("no trades")
        else:
            print(f"trades={r['trades']:>3}  win%={r['win_rate']:>5.1f}%  "
                  f"pnl=Rs{r['total_pnl']:>+8,.0f}  avg=Rs{r['avg_pnl']:>+7,.0f}  maxDD=Rs{r['max_dd']:>6,.0f}")

    if not results:
        print("\nNo results.")
        return

    df = pd.DataFrame(results).sort_values("total_pnl", ascending=False).reset_index(drop=True)
    df.index += 1
    suffix = f"_{label}" if label else ""
    csv_path = RESULTS_DIR / f"india_orb_pandas{suffix}_ranking.csv"
    df.to_csv(csv_path)

    print(f"\n{'='*95}\n  RANKING (pandas engine)\n{'='*95}")
    for rank, row in df.iterrows():
        print(f"  {rank:<4} {row['symbol']:<12} {int(row['trades']):>7} {row['win_rate']:>5.0f}%"
              f"  Rs{row['total_pnl']:>+10,.0f}  Rs{row['avg_pnl']:>+8,.0f}  Rs{row['max_dd']:>8,.0f}")

    winners = df[df["total_pnl"] > 0]
    print(f"\n  Profitable: {len(winners)}/{len(df)}")
    if all_trades:
        _print_full_stats(_full_stats(all_trades), label="Combined (all symbols, unconstrained)")
    print(f"\n  Saved -> {csv_path}\n")


# ---------------------------------------------------------------------------
# A/B test: VWAP/EMA gate on vs off
# ---------------------------------------------------------------------------

def run_ab_test(symbols: list[str], nifty_up) -> None:
    print(f"\nA/B test: ORB+VWAP+EMA gate vs plain ORB - {len(symbols)} symbols\n")
    for label, kwargs in [("VWAP/EMA ON  (current live logic)", dict(use_vwap_ema=True)),
                           ("VWAP/EMA OFF (plain ORB + SAR)   ", dict(use_vwap_ema=False))]:
        all_trades = []
        for sym in symbols:
            all_trades.extend(run_symbol(sym, nifty_up, **kwargs))
        stats = _full_stats(all_trades)
        if not stats:
            print(f"  {label}: no trades")
            continue
        print(f"  {label}  |  n={stats['n']:>4}  win%={stats['win_rate']:>5.1f}%  "
              f"PF={stats['profit_factor']:>5.2f}  Rs{stats['total_pnl']:>+9,.0f}  "
              f"Sharpe={stats['sharpe']:>5.2f}  maxDD=Rs{stats['max_dd']:>7,.0f}")


# ---------------------------------------------------------------------------
# Parameter optimization
# ---------------------------------------------------------------------------

def run_optimize(symbols: list[str], nifty_up) -> None:
    RESULTS_DIR.mkdir(exist_ok=True)
    stop_grid = [0.001, 0.002, 0.003, 0.005, 0.010]
    target_grid = [1.0, 1.5, 2.0, 2.5, 3.0]

    print(f"\nParameter optimization (pandas engine) - {len(stop_grid)}x{len(target_grid)} grid "
          f"x {len(symbols)} symbols\n")

    rows = []
    for stop in stop_grid:
        for mult in target_grid:
            all_pnls, all_wins, all_n = [], 0, 0
            for sym in symbols:
                trades = run_symbol(sym, nifty_up, stop_buffer_pct=stop, profit_multiplier=mult)
                if not trades:
                    continue
                pnls = [t["pnl"] for t in trades]
                all_pnls.extend(pnls)
                all_wins += sum(1 for p in pnls if p > 0)
                all_n += len(pnls)
            if all_n == 0:
                continue
            total_pnl = sum(all_pnls)
            win_rate = all_wins / all_n * 100
            cumsum, peak, max_dd = 0.0, 0.0, 0.0
            for p in all_pnls:
                cumsum += p
                peak = max(peak, cumsum)
                max_dd = max(max_dd, peak - cumsum)
            rows.append(dict(stop_pct=stop, mult=mult, trades=all_n,
                              win_rate=round(win_rate, 1), total_pnl=round(total_pnl, 0),
                              avg_pnl=round(total_pnl / all_n, 0), max_dd=round(max_dd, 0)))
            print(f"  stop={stop:.1%}  mult={mult:.1f}x  |  trades={all_n:>4}  win%={win_rate:>5.1f}%"
                  f"  pnl=Rs{total_pnl:>+10,.0f}  maxDD=Rs{max_dd:>8,.0f}")

    if not rows:
        print("No results.")
        return
    opt_df = pd.DataFrame(rows).sort_values("total_pnl", ascending=False)
    best = opt_df.iloc[0]
    csv_path = RESULTS_DIR / "india_orb_pandas_optimize.csv"
    opt_df.to_csv(csv_path, index=False)
    print(f"\n  Best -> stop={best['stop_pct']:.1%}  mult={best['mult']:.1f}x"
          f"  win%={best['win_rate']:.1f}%  pnl=Rs{best['total_pnl']:+,.0f}\n")
    print(f"  Saved -> {csv_path}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    args = sys.argv[1:]

    print("Loading Nifty 50 bars for trend filter...", end="  ", flush=True)
    nifty = _load_nifty_trend()
    print("OK" if nifty is not None else "unavailable (no NIFTY50_NSE_5m.parquet -- filter disabled)")

    if "--optimize" in args:
        syms = [a for a in args if not a.startswith("--")] or INDIA_SYMBOLS
        run_optimize(syms, nifty)
    elif "--rank" in args:
        syms = [a for a in args if not a.startswith("--")] or INDIA_SYMBOLS
        run_ranking(syms, nifty)
    elif "--ab-test" in args:
        syms = [a for a in args if not a.startswith("--")] or INDIA_SYMBOLS
        run_ab_test(syms, nifty)
    else:
        syms = [a for a in args if not a.startswith("--")] or ["RELIANCE"]
        for sym in syms:
            run_single(sym, nifty)
