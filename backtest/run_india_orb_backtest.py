"""
India ORB Backtest Runner — NautilusTrader + NSE 5-min bars.

Since real money is at stake, this runner goes beyond a simple P&L table.
It produces a full analysis: symbol ranking, day-of-week breakdown,
entry-time heatmap, and parameter optimization to find the safest
stop_buffer_pct and profit_multiplier before going live.

Usage
-----
    # Single symbol — detailed trade log + full statistics
    python -m backtest.run_india_orb_backtest RELIANCE

    # Full ranking across all INDIA_SYMBOLS
    python -m backtest.run_india_orb_backtest --rank

    # Portfolio simulation: ₹15k budget cap (max 3 concurrent positions)
    python -m backtest.run_india_orb_backtest --portfolio

    # Walk-forward validation: 67/33 train/test split + overfitting check
    python -m backtest.run_india_orb_backtest --walkforward

    # Parameter sweep to find best stop/target settings
    python -m backtest.run_india_orb_backtest --optimize

    # Fetch/refresh NSE data first, then run portfolio sim
    python -m backtest.run_india_orb_backtest --fetch --portfolio

    # Run on full NSE_UNIVERSE (45 symbols)
    python -m backtest.run_india_orb_backtest --universe --portfolio
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
    INDIA_ORB_BREAKOUT_STRENGTH_PCT,
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
    breakout_strength_pct: float = INDIA_ORB_BREAKOUT_STRENGTH_PCT,
    date_from: str | None = None,
    date_to: str | None = None,
) -> tuple[list[dict], str | None]:
    """Run one backtest engine pass for a single symbol. Returns (trades, error)."""
    try:
        instrument = TestInstrumentProvider.equity(symbol=symbol, venue="XNSE")
        path = DATA_DIR / f"{symbol}_NSE_5m.parquet"
        if not path.exists():
            path = fetch_nse_bars(symbol, DATA_DIR)
        df = load_nse_bars_df(path)
        if date_from:
            df = df[df.index >= pd.Timestamp(date_from, tz="UTC")]
        if date_to:
            df = df[df.index < pd.Timestamp(date_to, tz="UTC")]
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
                breakout_strength_pct=breakout_strength_pct,
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
# Full statistics
# ---------------------------------------------------------------------------

def _full_stats(trades: list[dict]) -> dict:
    """
    Comprehensive strategy statistics:
    Sharpe (annualised), profit factor, expectancy, Wilson 95% CI on win rate,
    max consecutive losses, max drawdown.
    """
    import math
    import statistics as _stats
    from collections import defaultdict

    pnls = [t["pnl"] for t in trades]
    n = len(pnls)
    if n == 0:
        return {}

    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    p_win  = len(wins) / n

    # Wilson score 95% confidence interval
    z      = 1.96
    denom  = 1 + z ** 2 / n
    center = p_win + z ** 2 / (2 * n)
    margin = z * math.sqrt(p_win * (1 - p_win) / n + z ** 2 / (4 * n ** 2))
    ci_lo  = max(0.0, (center - margin) / denom * 100)
    ci_hi  = min(100.0, (center + margin) / denom * 100)

    gross_wins   = sum(wins)
    gross_losses = abs(sum(losses))
    profit_factor = gross_wins / (gross_losses or 0.001)
    avg_win  = gross_wins  / len(wins)   if wins   else 0.0
    avg_loss = gross_losses / len(losses) if losses else 0.0
    expectancy = p_win * avg_win - (1 - p_win) * avg_loss

    # Annualised Sharpe via daily P&L grouping (pandas already imported)
    daily_pnl: dict = defaultdict(float)
    for t in trades:
        day = pd.Timestamp(t["entry_ts"], unit="ns", tz="UTC").tz_convert(IST).date()
        daily_pnl[day] += t["pnl"]
    daily_pnls = list(daily_pnl.values())
    if len(daily_pnls) > 1:
        mean_d = _stats.mean(daily_pnls)
        std_d  = _stats.stdev(daily_pnls)
        sharpe = (mean_d / std_d * math.sqrt(252)) if std_d > 0 else 0.0
    else:
        sharpe = 0.0

    # Max consecutive losses
    max_cl, cur_cl = 0, 0
    for p in pnls:
        if p <= 0:
            cur_cl += 1
            max_cl = max(max_cl, cur_cl)
        else:
            cur_cl = 0

    # Max drawdown
    cumsum, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        cumsum += p
        peak    = max(peak, cumsum)
        max_dd  = max(max_dd, peak - cumsum)

    return dict(
        n=n,
        wins=len(wins),
        losses=len(losses),
        win_rate=round(p_win * 100, 1),
        ci_lo=round(ci_lo, 1),
        ci_hi=round(ci_hi, 1),
        profit_factor=round(profit_factor, 2),
        avg_win=round(avg_win, 2),
        avg_loss=round(avg_loss, 2),
        expectancy=round(expectancy, 2),
        sharpe=round(sharpe, 2),
        total_pnl=round(sum(pnls), 2),
        avg_pnl=round(sum(pnls) / n, 2),
        max_dd=round(max_dd, 2),
        max_consec_losses=max_cl,
    )


def _print_full_stats(stats: dict, label: str = "Portfolio") -> None:
    """Print _full_stats() result as a readable table."""
    if not stats:
        print("  No trades.")
        return
    print(f"\n  ── {label} Statistics ─────────────────────────────────────────")
    print(f"  Trades              : {stats['n']}")
    print(f"  Win rate            : {stats['win_rate']:.1f}%"
          f"  (95% CI: {stats['ci_lo']:.1f}%–{stats['ci_hi']:.1f}%)")
    print(f"  Profit factor       : {stats['profit_factor']:.2f}"
          f"  (edge if > 1.0, strong if > 1.5)")
    print(f"  Expectancy / trade  : ₹{stats['expectancy']:+,.2f}")
    print(f"  Avg win / avg loss  : ₹{stats['avg_win']:+,.2f}  /  ₹{-stats['avg_loss']:+,.2f}")
    print(f"  Sharpe (annualised) : {stats['sharpe']:.2f}"
          f"  (>1.0 good, >2.0 excellent)")
    print(f"  Max consec losses   : {stats['max_consec_losses']}")
    print(f"  Max drawdown        : ₹{stats['max_dd']:,.2f}")
    print(f"  Total P&L           : ₹{stats['total_pnl']:+,.2f}")
    print(f"  ──────────────────────────────────────────────────────────────")


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

    _print_full_stats(_full_stats(trades), label=symbol)

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
# Portfolio simulation (₹15k budget cap = max 3 concurrent positions)
# ---------------------------------------------------------------------------

def run_portfolio(
    symbols: list[str],
    nifty_bars,
    max_positions: int = 3,
    label: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    stop_buffer_pct: float = INDIA_ORB_STOP_BUFFER_PCT,
    profit_multiplier: float = INDIA_ORB_PROFIT_MULTIPLIER,
    breakout_strength_pct: float = 0.0,
) -> list[dict]:
    """
    Simulate a ₹15k budget cap across all symbols.

    Collects every trade from every symbol, sorts them chronologically,
    and accepts a trade only if fewer than max_positions are currently open.
    This mirrors the real bot: ₹5k × 3 slots = ₹15k deployed at most.
    Returns the list of budget-constrained accepted trades.
    """
    RESULTS_DIR.mkdir(exist_ok=True)
    suffix = f" [{label}]" if label else ""
    print(f"\nPortfolio Simulation{suffix} — {len(symbols)} symbols, max {max_positions} concurrent")
    bud = INDIA_POSITION_SIZE_INR * max_positions
    print(f"  ₹{INDIA_POSITION_SIZE_INR:,}/trade × {max_positions} slots = ₹{bud:,} budget cap")
    print(f"  stop={stop_buffer_pct:.1%}  mult={profit_multiplier:.1f}×  breakout={breakout_strength_pct:.2%}\n")

    all_trades: list[dict] = []
    for i, sym in enumerate(symbols, 1):
        print(f"  [{i:>2}/{len(symbols)}] {sym:<12}", end="  ", flush=True)
        trades, err = _run_engine(
            sym, nifty_bars,
            stop_buffer_pct=stop_buffer_pct,
            profit_multiplier=profit_multiplier,
            breakout_strength_pct=breakout_strength_pct,
            date_from=date_from,
            date_to=date_to,
        )
        if err:
            print(f"ERROR: {err}")
            continue
        print(f"{len(trades)} trades")
        all_trades.extend(trades)

    if not all_trades:
        print("\n  No trades collected.")
        return []

    # Chronological budget simulation
    all_trades.sort(key=lambda t: t["entry_ts"])
    open_exits: list[int] = []   # exit_ts of currently open positions
    accepted: list[dict] = []
    rejected = 0

    for t in all_trades:
        open_exits = [ex for ex in open_exits if ex > t["entry_ts"]]
        if len(open_exits) < max_positions:
            accepted.append(t)
            open_exits.append(t["exit_ts"])
        else:
            rejected += 1

    print(f"\n  Accepted {len(accepted)} / {len(all_trades)} trades  "
          f"(rejected {rejected} — budget full)\n")

    # Per-symbol accepted breakdown
    sym_pnls: dict[str, list] = {}
    for t in accepted:
        sym_pnls.setdefault(t["symbol"], []).append(t["pnl"])

    print(f"  {'Symbol':<14} {'Taken':>5} {'Win%':>6} {'Total P&L':>12}")
    print(f"  {'-'*42}")
    for sym in sorted(sym_pnls, key=lambda s: sum(sym_pnls[s]), reverse=True):
        pnls = sym_pnls[sym]
        wins = sum(1 for p in pnls if p > 0)
        print(f"  {sym:<14} {len(pnls):>5} {wins/len(pnls)*100:>5.0f}%"
              f"  ₹{sum(pnls):>+10,.0f}")

    _print_full_stats(_full_stats(accepted),
                      label=f"Portfolio{' '+label if label else ''}")
    _print_dow_breakdown(accepted)
    _print_time_breakdown(accepted)
    return accepted


# ---------------------------------------------------------------------------
# Walk-forward validation (67% train / 33% test, chronological split)
# ---------------------------------------------------------------------------

def run_walkforward(
    symbols: list[str],
    nifty_bars,
    train_pct: float = 0.67,
) -> None:
    """
    Chronological walk-forward:
    1. Find date range across all available parquet files.
    2. Split train_pct / (1-train_pct) at the boundary date.
    3. Grid-search stop × mult × breakout on training period.
    4. Validate best params on out-of-sample test period (portfolio-constrained).
    5. Print IS vs OOS comparison and overfitting verdict.
    """
    # Find data date range
    date_min: pd.Timestamp | None = None
    date_max: pd.Timestamp | None = None
    for sym in symbols:
        path = DATA_DIR / f"{sym}_NSE_5m.parquet"
        if not path.exists():
            continue
        try:
            df = load_nse_bars_df(path)
            if df.empty:
                continue
            lo, hi = df.index.min(), df.index.max()
            date_min = lo if date_min is None else min(date_min, lo)
            date_max = hi if date_max is None else max(date_max, hi)
        except Exception:
            pass

    if date_min is None or date_max is None:
        print("\n  Walk-forward: no parquet data found.")
        return

    total_days = (date_max - date_min).days
    split_ts   = date_min + pd.Timedelta(days=int(total_days * train_pct))
    s0 = date_min.strftime("%Y-%m-%d")
    s1 = split_ts.strftime("%Y-%m-%d")
    s2 = (date_max + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    print(f"\nWalk-Forward Validation")
    print(f"  Data  : {s0} → {date_max.strftime('%Y-%m-%d')}")
    print(f"  Train : {s0} → {s1}  ({train_pct:.0%})")
    print(f"  Test  : {s1} → {s2}  ({1-train_pct:.0%})")
    print(f"  Syms  : {len(symbols)}")

    stop_grid   = [0.005, 0.010, 0.015, 0.020]
    target_grid = [1.0, 1.5, 2.0, 2.5]
    bkst_grid   = [0.0, 0.001, 0.002]
    total_runs  = len(stop_grid) * len(target_grid) * len(bkst_grid) * len(symbols)

    print(f"\n  ── Training phase ({len(stop_grid)}×{len(target_grid)}×{len(bkst_grid)} grid"
          f" × {len(symbols)} syms = {total_runs} runs) ──\n")

    best_pnl    = float("-inf")
    best_params: dict = {}

    for stop in stop_grid:
        for mult in target_grid:
            for bkst in bkst_grid:
                pnl_sum, n_sum, win_sum = 0.0, 0, 0
                for sym in symbols:
                    trades, _ = _run_engine(
                        sym, nifty_bars,
                        stop_buffer_pct=stop,
                        profit_multiplier=mult,
                        breakout_strength_pct=bkst,
                        date_from=s0,
                        date_to=s1,
                    )
                    if not trades:
                        continue
                    pnl_sum += sum(t["pnl"] for t in trades)
                    n_sum   += len(trades)
                    win_sum += sum(1 for t in trades if t["pnl"] > 0)

                if n_sum < 5:
                    continue
                marker = ""
                if pnl_sum > best_pnl:
                    best_pnl    = pnl_sum
                    best_params = dict(stop=stop, mult=mult, bkst=bkst)
                    marker = "  ◀ BEST"
                print(f"    stop={stop:.1%}  mult={mult:.1f}×  bkst={bkst:.2%}"
                      f"  n={n_sum:>4}  win%={win_sum/n_sum*100:>5.1f}%"
                      f"  pnl=₹{pnl_sum:>+10,.0f}{marker}")

    if not best_params:
        print("\n  No valid combinations in training period.")
        return

    print(f"\n  Best → stop={best_params['stop']:.1%}  mult={best_params['mult']:.1f}×"
          f"  bkst={best_params['bkst']:.2%}  IS P&L=₹{best_pnl:+,.0f}")

    # Full training stats with best params (all trades, no budget filter)
    train_all: list[dict] = []
    for sym in symbols:
        trades, _ = _run_engine(
            sym, nifty_bars,
            stop_buffer_pct=best_params["stop"],
            profit_multiplier=best_params["mult"],
            breakout_strength_pct=best_params["bkst"],
            date_from=s0,
            date_to=s1,
        )
        train_all.extend(trades)
    train_stats = _full_stats(train_all)

    # OOS test — portfolio-constrained (realistic)
    print(f"\n  ── Test phase (out-of-sample) ──")
    test_accepted = run_portfolio(
        symbols, nifty_bars,
        label="OOS test",
        date_from=s1,
        date_to=s2,
        stop_buffer_pct=best_params["stop"],
        profit_multiplier=best_params["mult"],
        breakout_strength_pct=best_params["bkst"],
    )
    test_stats = _full_stats(test_accepted)

    # IS vs OOS comparison table
    print(f"\n  ── In-sample vs Out-of-sample ────────────────────────────────")
    print(f"  {'Metric':<24} {'In-sample (train)':>18} {'Out-of-sample':>15}")
    print(f"  {'-'*60}")
    _CMP = [
        ("Trades",          "n",                  "d"),
        ("Win rate %",      "win_rate",            ".1f"),
        ("Profit factor",   "profit_factor",       ".2f"),
        ("Expectancy ₹",    "expectancy",          "+.2f"),
        ("Sharpe",          "sharpe",              ".2f"),
        ("Total P&L ₹",     "total_pnl",           "+,.0f"),
        ("Max DD ₹",        "max_dd",              ",.0f"),
        ("Max consec loss", "max_consec_losses",   "d"),
    ]
    for lbl, key, fmt in _CMP:
        tv = train_stats.get(key, 0)
        ov = test_stats.get(key, 0)
        try:
            print(f"  {lbl:<24} {format(tv, fmt):>18} {format(ov, fmt):>15}")
        except Exception:
            print(f"  {lbl:<24} {tv!s:>18} {ov!s:>15}")

    oos_pf  = test_stats.get("profit_factor", 0)
    oos_pnl = test_stats.get("total_pnl", 0)
    if oos_pf >= 1.2 and oos_pnl > 0:
        verdict = "PASS — strategy generalises well to unseen data"
    elif oos_pnl > 0:
        verdict = "CAUTION — OOS profitable but weaker than IS (mild overfit)"
    else:
        verdict = "FAIL — strategy does NOT generalise (overfit to training data)"

    print(f"\n  Verdict: {verdict}")
    print(f"\n  Suggested config.py (validated on {s1} → {s2}):")
    print(f"     INDIA_ORB_STOP_BUFFER_PCT        = {best_params['stop']}")
    print(f"     INDIA_ORB_PROFIT_MULTIPLIER      = {best_params['mult']}")
    print(f"     INDIA_ORB_BREAKOUT_STRENGTH_PCT  = {best_params['bkst']}")


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

    elif "--portfolio" in args:
        syms = [a for a in args if not a.startswith("--")] or default_syms
        run_portfolio(syms, nifty, breakout_strength_pct=INDIA_ORB_BREAKOUT_STRENGTH_PCT)

    elif "--walkforward" in args:
        syms = [a for a in args if not a.startswith("--")] or default_syms
        run_walkforward(syms, nifty)

    else:
        syms = [a for a in args if not a.startswith("--")] or ["RELIANCE"]
        for sym in syms:
            run_single(sym, nifty)
