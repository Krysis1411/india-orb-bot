"""
NIFTY / BANKNIFTY index options backtest.

Does NOT re-implement zone detection: reuses backtest.zone_backtest.run_symbol
UNCHANGED, pointed at the index's own 5m/1h price bars (fetch_index_data.py)
instead of a stock's. zone_detector.py / entry_confirmation.py have no
equity-specific assumptions -- they consume generic OHLCV bars, so the exact
same signal pipeline validated on 77 NSE stocks this session (confirmed_ts
hindsight-bias fix, RSI-at-entry fix, wick/gap/outlier-range filters, the
MIN_RISK_ATR_MULT stop floor, breakeven+trailing-stop management) runs
identically on the index. What's new here is ONLY the translation layer:
underlying zone signal -> simulated long-option trade.

Why simulated (Black-Scholes) pricing instead of real historical option
quotes: AngelOne's ScripMaster only lists currently-live NFO contracts --
once a weekly/monthly contract expires, its token disappears, so there is no
way to pull real historical OHLC for an option that has already expired
(confirmed via SmartAPI docs/forum research, 2026-07-31). Every other
AngelOne integration in this repo (equity zone backtest, zone_paper_trader.py)
runs on real historical or real live bars; this is the one place synthetic
pricing is unavoidable for backtesting. ml/gbs.py's merton() is the right
model (European exercise + continuous dividend yield, matching NIFTY/
BANKNIFTY cash-settled index options) -- but IV, r, and q below are FLAT
ASSUMPTIONS, not fetched market data (no historical India VIX / IV surface
source is wired up). Treat results as a directional P&L-shape approximation,
not a fill-accurate simulation: real bid/ask spread, strike-specific IV skew,
and slippage on NFO contracts are not modeled. Paper trading against LIVE
option quotes (not simulated) is the next step specifically to validate or
invalidate this approximation against reality.

Usage
-----
    python -m backtest.index_options_backtest NIFTY
    python -m backtest.index_options_backtest BANKNIFTY
    python -m backtest.index_options_backtest              # both
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.zone_backtest import _full_stats, _print_full_stats, _print_walkforward, run_symbol
from ml.gbs import merton

# --- Flat pricing assumptions (see module docstring) ------------------------
# India ~6.5% short-term risk-free rate (RBI repo-adjacent), 2026.
RISK_FREE_RATE = 0.065
# Approximate continuous dividend yield of each index (NIFTY 50 ~1.2%/yr,
# NIFTY BANK ~0.8%/yr -- banks pay less of their earnings as dividends).
DIVIDEND_YIELD = {"NIFTY": 0.012, "BANKNIFTY": 0.008}
# Flat assumed IV per underlying (rough recent India VIX / index-options IV
# levels). Does NOT model IV changing over the trade's hold or skew by
# strike/expiry -- theta decay and delta are still captured correctly since
# spot and time-to-expiry move bar by bar, only IV itself is held constant.
IMPLIED_VOL = {"NIFTY": 0.13, "BANKNIFTY": 0.15}

STRIKE_INTERVAL = {"NIFTY": 50.0, "BANKNIFTY": 100.0}
# Require at least this many calendar days of runway from entry to the chosen
# expiry -- avoids picking a same-day/next-day 0DTE contract where gamma risk
# dominates and the Black-Scholes theoretical price is least reliable.
MIN_DTE_DAYS = 2


def _next_weekly_tuesday(d: date) -> date:
    days_ahead = (1 - d.weekday()) % 7  # Monday=0 ... Tuesday=1
    if days_ahead == 0:
        days_ahead = 7
    return d + timedelta(days=days_ahead)


def _next_monthly_expiry(d: date) -> date:
    """Last Tuesday of d's month; if already past, last Tuesday of next month."""
    def last_tuesday_of(year: int, month: int) -> date:
        if month == 12:
            first_of_next = date(year + 1, 1, 1)
        else:
            first_of_next = date(year, month + 1, 1)
        last_day = first_of_next - timedelta(days=1)
        offset = (last_day.weekday() - 1) % 7
        return last_day - timedelta(days=offset)

    candidate = last_tuesday_of(d.year, d.month)
    if candidate >= d:
        return candidate
    nm_year, nm_month = (d.year, d.month + 1) if d.month < 12 else (d.year + 1, 1)
    return last_tuesday_of(nm_year, nm_month)


def _pick_expiry(underlying: str, entry_date: date) -> date:
    floor_date = entry_date + timedelta(days=MIN_DTE_DAYS)
    if underlying == "NIFTY":
        expiry = _next_weekly_tuesday(entry_date)
        while expiry < floor_date:
            expiry = _next_weekly_tuesday(expiry)
        return expiry
    # BANKNIFTY: weekly options were discontinued (SEBI Oct 2024 single-weekly-
    # expiry-per-exchange rule) -- only the last-Tuesday monthly contract trades.
    expiry = _next_monthly_expiry(entry_date)
    while expiry < floor_date:
        expiry = _next_monthly_expiry(expiry + timedelta(days=1))
    return expiry


def _price(option_type: str, spot: float, strike: float, as_of: date, expiry: date, underlying: str) -> float | None:
    t_years = (expiry - as_of).days / 365.0
    if t_years <= 0:
        # Expired at/after this point -- settle at intrinsic value.
        intrinsic = max(spot - strike, 0.0) if option_type == "c" else max(strike - spot, 0.0)
        return intrinsic
    try:
        value, *_greeks = merton(
            option_type, spot, strike, t_years,
            RISK_FREE_RATE, DIVIDEND_YIELD[underlying], IMPLIED_VOL[underlying],
        )
        return float(value)
    except Exception:
        return None


def translate_to_options(underlying: str, equity_trades: list[dict]) -> list[dict]:
    """Convert each underlying zone trade into a simulated long-CE/long-PE trade.
    Long zone (demand reversal) -> buy CE. Short zone (supply reversal) -> buy PE."""
    interval = STRIKE_INTERVAL[underlying]
    out: list[dict] = []
    for t in equity_trades:
        option_type = "c" if t["direction"] == "long" else "p"
        entry_date = t["entry_ts"].tz_convert("Asia/Kolkata").date()
        expiry = _pick_expiry(underlying, entry_date)
        strike = round(t["entry_price"] / interval) * interval

        entry_opt = _price(option_type, t["entry_price"], strike, entry_date, expiry, underlying)
        if entry_opt is None or entry_opt <= 0.01:
            continue  # too far OTM / degenerate -- not a realistically tradeable premium

        exit_date = t["exit_ts"].tz_convert("Asia/Kolkata").date()
        # A contract can't be held past its own expiry -- clip the exit to
        # expiry (settled at intrinsic value there) if the underlying trade's
        # exit would otherwise run past it. We only have the underlying's
        # recorded exit price, not a full path to expiry, so that price is
        # used as the spot either way -- an approximation when clipped.
        effective_exit_date = min(exit_date, expiry)
        exit_opt = _price(option_type, t["exit_price"], strike, effective_exit_date, expiry, underlying)
        if exit_opt is None:
            continue

        pnl_pct = (exit_opt - entry_opt) / entry_opt * 100

        out.append({
            **t,
            "option_type": option_type.upper(),
            "expiry": expiry,
            "strike": strike,
            "dte_at_entry": (expiry - entry_date).days,
            "entry_option_price": round(entry_opt, 2),
            "exit_option_price": round(exit_opt, 2),
            "pnl_pct": round(pnl_pct, 1),   # overwrite underlying-% with option-%
        })
    return out


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    underlyings = args or ["NIFTY", "BANKNIFTY"]

    all_trades: list[dict] = []
    for name in underlyings:
        equity_trades = run_symbol(name)
        print(f"\n{name}: {len(equity_trades)} underlying zone signals")
        opt_trades = translate_to_options(name, equity_trades)
        print(f"{name}: {len(opt_trades)} translated into tradeable option premiums")
        for t in opt_trades:
            t["underlying"] = name
        all_trades.extend(opt_trades)

        stats = _full_stats(opt_trades)
        _print_full_stats(stats, f"{name} OPTIONS (simulated Black-Scholes)")

    if len(underlyings) > 1:
        _print_full_stats(_full_stats(all_trades), "COMBINED (NIFTY + BANKNIFTY options)")
    _print_walkforward(all_trades, folds=4)

    if all_trades:
        out_path = Path(__file__).parent / "results" / "index_options_backtest_trades.csv"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(all_trades).to_csv(out_path, index=False)
        print(f"\nTrade log -> {out_path}")
