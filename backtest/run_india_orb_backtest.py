"""
India ORB Backtest Runner — NautilusTrader + NSE 5-min bars.

Since real money is at stake, this runner goes beyond a simple P&L table.
It produces a full analysis: symbol ranking, day-of-week breakdown,
entry-time heatmap, and parameter optimization to find the safest
stop_buffer_pct and profit_multiplier before going live.

Usage
-----
    # Single symbol — detailed trade log
    python -m backtest.run_india_orb_backtest RELIANCE

    # Full ranking across all INDIA_SYMBOLS
    python -m backtest.run_india_orb_backtest --rank

    # Parameter sweep to find best stop/target settings
    python -m backtest.run_india_orb_backtest --optimize

    # Fetch/refresh NSE data first, then rank
    python -m backtest.run_india_orb_backtest --fetch --rank
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import LoggingConfig, RiskEngineConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import TraderId, Venue
from nautilus_trader.model.objects import Money
from nautilus_trader.persistence.wranglers import BarDataWrangler
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from zoneinfo import ZoneInfo

from backtest.fetch_nse_data import fetch_nse_bars, load_nse_bars_df
from config import (
    INDIA_ORB_PROFIT_MULTIPLIER,
    INDIA_ORB_RANGE_BARS,
    INDIA_ORB_STOP_BUFFER_PCT,
    INDIA_ORB_MIN_OR_PCT,
    INDIA_ORB_VOLUME_FACTOR,
    INDIA_POSITION_SIZE_INR,
    INDIA_SYMBOLS,
)
from strategies.india_orb import IndiaORBConfig, IndiaORBStrategy

IST      = ZoneInfo("Asia/Kolkata")
DATA_DIR = Path(__file__).parent / "data"
RESULTS_DIR = Path(__file__).parent / "results"
XNSE = Venue("XNSE")

# Nifty 50 index instrument — used as the market trend filter
_NIFTY_SYMBOL   = "^NSEI"
_NIFTY_YF_ALIAS = "NIFTY50"
NIFTY_INSTRUMENT = TestInstrumentProvider.equity(symbol="NIFTY50", venue="XNSE")
NIFTY_BAR_TYPE   = BarType.from_str("NIFTY50.XNSE-5-MINUTE-LAST-EXTERNAL")


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _load_nifty_bars():
    """Load Nifty 50 bars for the trend filter. Returns None if unavailable."""
    import yfinance as yf
    try:
        raw = yf.download("^NSEI", period="60d", interval="5m",
                          progress=False, auto_adjust=True)
        if raw.empty:
            return None
        if isinstance(raw.columns, pd.MultiIndex):
            raw = raw.droplevel(1, axis=1)
        raw.columns = [c.lower() for c in raw.columns]
        raw.index.name = "timestamp"
        if raw.index.tz is None:
            raw.index = raw.index.tz_localize("UTC")
        else:
            raw.index = raw.index.tz_convert("UTC")
        # Filter to NSE hours
        ist_df = raw.copy()
        ist_df.index = ist_df.index.tz_convert(IST)
        ist_df = ist_df.between_time("09:15", "15:30")
        ist_df.index = ist_df.index.tz_convert("UTC")
        if ist_df.empty:
            return None
        return BarDataWrangler(NIFTY_BAR_TYPE, NIFTY_INSTRUMENT).process(ist_df)
    except Exception as e:
        print(f"  [Nifty] failed ({e}) — trend filter disabled")
        return None


def _run_engine(
    symbol: str,
    nifty_bars,
    profit_multiplier: float = INDIA_ORB_PROFIT_MULTIPLIER,
    stop_buffer_pct: float = INDIA_ORB_STOP_BUFFER_PCT,
    min_or_pct: float = INDIA_ORB_MIN_OR_PCT,
    volume_factor: float = INDIA_ORB_VOLUME_FACTOR,
    trailing_stop: bool = True,
) -> tuple[list[dict], str | None]:
    """Run one backtest engine pass for a single symbol. Returns (trades, error)."""
    try:
        instrument = TestInstrumentProvider.equity(symbol=symbol, venue="XNSE")
        path = DATA_DIR / f"{symbol}_NSE_5m.parquet"
        if not path.exists():
            path = fetch_nse_bars(symbol, DATA_DIR)
        df = load_nse_bars_df(path)
        df = df.between_time("03:45", "10:00")   # NSE hours in UTC
        if len(df) < 50:
            return [], f"only {len(df)} bars after filtering"

        bar_type = BarType.from_str(f"{instrument.id}-5-MINUTE-LAST-EXTERNAL")
        bars     = BarDataWrangler(bar_type, instrument).process(df)

        engine = BacktestEngine(
            config=BacktestEngineConfig(
                trader_id=TraderId("BACKTESTER-001"),
                logging=LoggingConfig(log_level="ERROR"),
                risk_engine=RiskEngineConfig(bypass=True),
            )
        )
        engine.add_venue(
            venue=XNSE,
            oms_type=OmsType.NETTING,
            account_type=AccountType.CASH,
            base_currency=USD,
            starting_balances=[Money(500_000, USD)],  # dummy — P&L tracked manually
        )
        engine.add_instrument(instrument)
        engine.add_data(bars)

        nifty_bar_type_arg = None
        if nifty_bars is not None:
            engine.add_instrument(NIFTY_INSTRUMENT)
            engine.add_data(nifty_bars)
            nifty_bar_type_arg = NIFTY_BAR_TYPE

        strategy = IndiaORBStrategy(
            config=IndiaORBConfig(
                instrument_id=instrument.id,
                bar_type=bar_type,
                position_size_inr=float(INDIA_POSITION_SIZE_INR),
                orb_range_bars=INDIA_ORB_RANGE_BARS,
                profit_multiplier=profit_multiplier,
                volume_factor=volume_factor,
                stop_buffer_pct=stop_buffer_pct,
                min_or_pct=min_or_pct,
                nifty_bar_type=nifty_bar_type_arg,
                trailing_stop=trailing_stop,
            )
        )
        engine.add_strategy(strategy)
        engine.run()
        trades = list(strategy.trades)
        engine.reset()
        engine.dispose()
        return trades, None

    except Exception as e:
        return [], str(e)


# ---------------------------------------------------------------------------
# Summary helpers
# ---------------------------------------------------------------------------

def _summarize(symbol: str, trades: list[dict]) -> dict:
    if not trades:
        return dict(symbol=symbol, trades=0, wins=0, losses=0, win_rate=0.0,
                    total_pnl=0.0, avg_pnl=0.0, best=0.0, worst=0.0, max_dd=0.0)
    pnls   = [t["pnl"] for t in trades]
    n      = len(pnls)
    wins   = sum(1 for p in pnls if p > 0)
    cumsum = 0.0
    peak   = 0.0
    max_dd = 0.0
    for p in pnls:
        cumsum += p
        peak   = max(peak, cumsum)
        max_dd = max(max_dd, peak - cumsum)
    return dict(
        symbol=symbol,
        trades=n,
        wins=wins,
        losses=n - wins,
        win_rate=round(wins / n * 100, 1),
        total_pnl=round(sum(pnls), 2),
        avg_pnl=round(sum(pnls) / n, 2),
        best=round(max(pnls), 2),
        worst=round(min(pnls), 2),
        max_dd=round(max_dd, 2),
    )


# ---------------------------------------------------------------------------
# Single-symbol detailed output
# ---------------------------------------------------------------------------

def run_single(symbol: str, nifty_bars) -> None:
    w = 80
    print(f"\n{'='*w}")
    print(f"  India ORB Backtest — {symbol} (NSE)")
    print(f"  ₹{INDIA_POSITION_SIZE_INR:,}/trade | profit_mult={INDIA_ORB_PROFIT_MULTIPLIER}×"
          f" | stop={INDIA_ORB_STOP_BUFFER_PCT:.1%} | min_OR={INDIA_ORB_MIN_OR_PCT:.1%}"
          f" | trailing_stop=ON")
    print(f"{'='*w}\n")

    trades, err = _run_engine(symbol, nifty_bars)
    if err:
        print(f"  ERROR: {err}")
        return
    if not trades:
        print("  No trades triggered.")
        return

    # --- Trade log ---
    print(f"  {'Entry ₹':>9} {'Exit ₹':>9} {'Qty':>5} {'P&L ₹':>10} {'P&L%':>7}  "
          f"{'Day':<10} {'Entry':>6}  Reason")
    print(f"  {'-'*75}")
    for t in trades:
        print(
            f"  {t['entry_price']:>9.2f}"
            f"  {t['exit_price']:>9.2f}"
            f"  {t['qty']:>5}"
            f"  {t['pnl']:>+10.2f}"
            f"  {t['pnl_pct']:>+6.2f}%"
            f"  {t['entry_weekday']:<10}"
            f"  {t['entry_time_ist']:>6}"
            f"  {t['exit_reason']}"
        )

    s = _summarize(symbol, trades)
    print(f"\n  Trades: {s['trades']}  |  Wins: {s['wins']}  |  Win%: {s['win_rate']:.0f}%  |"
          f"  Total P&L: ₹{s['total_pnl']:+,.2f}  |  Avg: ₹{s['avg_pnl']:+,.2f}"
          f"  |  Max DD: ₹{s['max_dd']:,.2f}")

    # --- Day-of-week breakdown ---
    _print_dow_breakdown(trades)

    # --- Entry time breakdown ---
    _print_time_breakdown(trades)

    # --- OR range breakdown ---
    _print_or_range_breakdown(trades)


def _print_dow_breakdown(trades: list[dict]) -> None:
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    print(f"\n  Day-of-week breakdown:")
    print(f"  {'Day':<12} {'Trades':>7} {'Wins':>6} {'Win%':>6} {'Total P&L':>12} {'Avg P&L':>10}")
    print(f"  {'-'*58}")
    for day in days:
        day_trades = [t for t in trades if t["entry_weekday"] == day]
        if not day_trades:
            continue
        pnls = [t["pnl"] for t in day_trades]
        n    = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        print(
            f"  {day:<12} {n:>7} {wins:>6} {wins/n*100:>5.0f}%"
            f"  ₹{sum(pnls):>+10,.0f}  ₹{sum(pnls)/n:>+8,.0f}"
        )


def _print_time_breakdown(trades: list[dict]) -> None:
    slots = [
        ("09:45–10:30", "09:45", "10:30"),
        ("10:30–11:30", "10:30", "11:30"),
        ("11:30–12:30", "11:30", "12:30"),
        ("12:30–13:00", "12:30", "13:00"),
    ]
    print(f"\n  Entry time breakdown (IST):")
    print(f"  {'Window':<16} {'Trades':>7} {'Wins':>6} {'Win%':>6} {'Total P&L':>12} {'Avg P&L':>10}")
    print(f"  {'-'*62}")
    from datetime import time as _t
    for label, start, end in slots:
        s_h, s_m = map(int, start.split(":"))
        e_h, e_m = map(int, end.split(":"))
        bucket = [
            t for t in trades
            if _t(s_h, s_m) <= _t(*map(int, t["entry_time_ist"].split(":"))) < _t(e_h, e_m)
        ]
        if not bucket:
            continue
        pnls = [t["pnl"] for t in bucket]
        n    = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        print(
            f"  {label:<16} {n:>7} {wins:>6} {wins/n*100:>5.0f}%"
            f"  ₹{sum(pnls):>+10,.0f}  ₹{sum(pnls)/n:>+8,.0f}"
        )


def _print_or_range_breakdown(trades: list[dict]) -> None:
    """Show win rate grouped by opening range size (% of price)."""
    bins = [(0, 0.3), (0.3, 0.5), (0.5, 0.8), (0.8, 1.2), (1.2, 99)]
    labels = ["<0.3%", "0.3–0.5%", "0.5–0.8%", "0.8–1.2%", ">1.2%"]
    print(f"\n  Opening range size breakdown:")
    print(f"  {'OR range':>10} {'Trades':>7} {'Wins':>6} {'Win%':>6} {'Avg P&L':>10}")
    print(f"  {'-'*46}")
    for (lo, hi), label in zip(bins, labels):
        bucket = [t for t in trades if lo <= t["or_range_pct"] < hi]
        if not bucket:
            continue
        pnls = [t["pnl"] for t in bucket]
        n    = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        print(f"  {label:>10} {n:>7} {wins:>6} {wins/n*100:>5.0f}%  ₹{sum(pnls)/n:>+8,.0f}")


# ---------------------------------------------------------------------------
# Full ranking
# ---------------------------------------------------------------------------

def run_ranking(symbols: list[str], nifty_bars, label: str | None = None) -> None:
    RESULTS_DIR.mkdir(exist_ok=True)
    print(f"\nIndia ORB Backtest Ranking — {len(symbols)} symbols")
    print(f"₹{INDIA_POSITION_SIZE_INR:,}/trade | mult={INDIA_ORB_PROFIT_MULTIPLIER}×"
          f" | stop={INDIA_ORB_STOP_BUFFER_PCT:.1%} | Nifty filter={'ON' if nifty_bars else 'OFF'}\n")

    results = []
    all_trades = []
    for i, sym in enumerate(symbols, 1):
        print(f"[{i:>2}/{len(symbols)}] {sym:<12}", end="  ", flush=True)
        trades, err = _run_engine(sym, nifty_bars)
        if err:
            print(f"ERROR: {err}")
            continue
        r = _summarize(sym, trades)
        results.append(r)
        all_trades.extend(trades)
        if r["trades"] == 0:
            print("no trades")
        else:
            flag = "  ★" if r["total_pnl"] > 0 and r["trades"] >= 5 else ""
            print(
                f"trades={r['trades']:>3}  win%={r['win_rate']:>5.1f}%"
                f"  pnl=₹{r['total_pnl']:>+8,.0f}"
                f"  avg=₹{r['avg_pnl']:>+7,.0f}"
                f"  maxDD=₹{r['max_dd']:>6,.0f}{flag}"
            )

    if not results:
        print("\nNo results.")
        return

    df = (pd.DataFrame(results)
          .sort_values("total_pnl", ascending=False)
          .reset_index(drop=True))
    df.index += 1

    suffix   = f"_{label}" if label else ""
    csv_path = RESULTS_DIR / f"india_orb{suffix}_ranking.csv"
    df.to_csv(csv_path)

    w = 95
    print(f"\n{'='*w}")
    print(f"  INDIA ORB RANKING  |  ₹{INDIA_POSITION_SIZE_INR:,}/trade"
          f"  |  mult={INDIA_ORB_PROFIT_MULTIPLIER}×"
          f"  |  stop={INDIA_ORB_STOP_BUFFER_PCT:.1%}")
    print(f"{'='*w}")
    print(f"  {'#':<4} {'Symbol':<12} {'Trades':>7} {'Win%':>6} {'Total P&L':>12}"
          f" {'Avg P&L':>10} {'Best':>10} {'Worst':>10} {'Max DD':>10}")
    print(f"  {'-'*w}")
    for rank, row in df.iterrows():
        flag = "  ★" if row["total_pnl"] > 0 and row["trades"] >= 5 else ""
        print(
            f"  {rank:<4} {row['symbol']:<12}"
            f" {int(row['trades']):>7}"
            f" {row['win_rate']:>5.0f}%"
            f"  ₹{row['total_pnl']:>+10,.0f}"
            f"  ₹{row['avg_pnl']:>+8,.0f}"
            f"  ₹{row['best']:>+8,.0f}"
            f"  ₹{row['worst']:>+8,.0f}"
            f"  ₹{row['max_dd']:>8,.0f}{flag}"
        )
    print(f"{'='*w}")

    winners = df[df["total_pnl"] > 0]
    losers  = df[df["total_pnl"] < 0]
    print(f"\n  Profitable symbols: {len(winners)}  |  Losing: {len(losers)}")
    if len(df) > 0:
        print(f"  Best:  {df.iloc[0]['symbol']}  ₹{df.iloc[0]['total_pnl']:+,.0f}")
        print(f"  Worst: {df.iloc[-1]['symbol']}  ₹{df.iloc[-1]['total_pnl']:+,.0f}")

    if all_trades:
        print(f"\n  Portfolio (all {len(all_trades)} trades across {len(results)} symbols):")
        _print_dow_breakdown(all_trades)
        _print_time_breakdown(all_trades)

    print(f"\n  Results saved → {csv_path}\n")


# ---------------------------------------------------------------------------
# Parameter optimization
# ---------------------------------------------------------------------------

def run_optimize(symbols: list[str], nifty_bars) -> None:
    """
    Grid search over stop_buffer_pct × profit_multiplier.
    Finds the combination with the best risk-adjusted return across
    all INDIA_SYMBOLS.  Run this before going live to validate config.
    """
    RESULTS_DIR.mkdir(exist_ok=True)
    stop_grid   = [0.001, 0.002, 0.003, 0.005, 0.010]
    target_grid = [1.0, 1.5, 2.0, 2.5, 3.0]

    total = len(stop_grid) * len(target_grid) * len(symbols)
    print(f"\nParameter optimization — {len(stop_grid)}×{len(target_grid)} grid"
          f" × {len(symbols)} symbols = {total} runs\n")

    rows = []
    run_n = 0
    for stop in stop_grid:
        for mult in target_grid:
            label = f"stop={stop:.1%}  mult={mult:.1f}×"
            all_pnls = []
            all_wins = 0
            all_n    = 0
            for sym in symbols:
                trades, err = _run_engine(
                    sym, nifty_bars,
                    profit_multiplier=mult,
                    stop_buffer_pct=stop,
                )
                run_n += len(symbols)
                if not trades:
                    continue
                pnls = [t["pnl"] for t in trades]
                all_pnls.extend(pnls)
                all_wins += sum(1 for p in pnls if p > 0)
                all_n    += len(pnls)

            if all_n == 0:
                continue

            total_pnl = sum(all_pnls)
            win_rate  = all_wins / all_n * 100
            avg_pnl   = total_pnl / all_n

            # Max drawdown across combined equity curve
            cumsum, peak, max_dd = 0.0, 0.0, 0.0
            for p in all_pnls:
                cumsum += p
                peak   = max(peak, cumsum)
                max_dd = max(max_dd, peak - cumsum)

            rows.append(dict(
                stop_pct=stop, mult=mult,
                trades=all_n, win_rate=round(win_rate, 1),
                total_pnl=round(total_pnl, 0),
                avg_pnl=round(avg_pnl, 0),
                max_dd=round(max_dd, 0),
            ))
            marker = "  ◀ best?" if total_pnl > 0 and win_rate >= 50 else ""
            print(
                f"  {label}  |  trades={all_n:>4}  win%={win_rate:>5.1f}%"
                f"  pnl=₹{total_pnl:>+10,.0f}  avg=₹{avg_pnl:>+7,.0f}"
                f"  maxDD=₹{max_dd:>8,.0f}{marker}"
            )

    if not rows:
        print("No results.")
        return

    opt_df = pd.DataFrame(rows).sort_values("total_pnl", ascending=False)
    best   = opt_df.iloc[0]
    csv_path = RESULTS_DIR / "india_orb_optimize.csv"
    opt_df.to_csv(csv_path, index=False)

    print(f"\n  ── Best parameters ──────────────────────────────────────")
    print(f"     stop_buffer_pct  = {best['stop_pct']:.1%}")
    print(f"     profit_multiplier = {best['mult']:.1f}×")
    print(f"     Win rate          = {best['win_rate']:.1f}%")
    print(f"     Total P&L         = ₹{best['total_pnl']:+,.0f}")
    print(f"     Avg P&L/trade     = ₹{best['avg_pnl']:+,.0f}")
    print(f"     Max drawdown      = ₹{best['max_dd']:,.0f}")
    print(f"\n  Results saved → {csv_path}\n")
    print(f"  ⚡ Update config.py:")
    print(f"     INDIA_ORB_STOP_BUFFER_PCT   = {best['stop_pct']}")
    print(f"     INDIA_ORB_PROFIT_MULTIPLIER = {best['mult']}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    args = sys.argv[1:]

    # --universe: use the full NSE_UNIVERSE screener pool instead of INDIA_SYMBOLS
    use_universe = "--universe" in args
    if use_universe:
        from india_screener import NSE_UNIVERSE
        default_syms = NSE_UNIVERSE
        print(f"Universe mode — {len(NSE_UNIVERSE)} symbols from NSE_UNIVERSE")
    else:
        default_syms = INDIA_SYMBOLS

    if "--fetch" in args:
        print("Fetching NSE data (yfinance, last 60 days)...")
        syms_to_fetch = [a for a in args if not a.startswith("--")] or default_syms
        ok, fail = [], []
        for sym in syms_to_fetch:
            try:
                path = fetch_nse_bars(sym, DATA_DIR)
                df   = load_nse_bars_df(path)
                days = df.index.normalize().nunique()
                print(f"  OK  {sym:<14} {len(df):>5} bars  {days} trading days")
                ok.append(sym)
            except Exception as e:
                print(f"  ERR {sym}: {e}")
                fail.append(sym)
        print(f"\n  {len(ok)} OK  |  {len(fail)} failed")
        if fail:
            print(f"  Failed: {', '.join(fail)}")
        args = [a for a in args if a != "--fetch"]
        print()

    print("Loading Nifty 50 bars for trend filter...", end="  ", flush=True)
    nifty = _load_nifty_bars()
    print("OK" if nifty else "FAILED (trend filter disabled)")

    if "--optimize" in args:
        syms = [a for a in args if not a.startswith("--")] or default_syms
        run_optimize(syms, nifty)

    elif "--rank" in args:
        syms = [a for a in args if not a.startswith("--")] or default_syms
        label = "universe" if use_universe else None
        run_ranking(syms, nifty, label=label)

    else:
        syms = [a for a in args if not a.startswith("--")] or ["RELIANCE"]
        for sym in syms:
            run_single(sym, nifty)
