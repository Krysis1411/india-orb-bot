"""
Supply/demand zone backtest — multi-timeframe confluence + entry confirmation.

Full trade pipeline:
  1. Find REVERSAL zones (DBR/RBD) on the higher timeframe (1h) -- the "bigger
     picture" zones, per strategies/zone_detector.py.
  2. Find zones independently on the lower/execution timeframe (5m).
  3. Require CONFLUENCE: a trade is only considered where an hourly zone's
     price range overlaps a 5-min zone's price range. The 5-min zone's
     (tighter) boundaries are used for the actual entry/stop, since it's the
     more precise of the two.
  4. Within a confluent zone, wait for entry CONFIRMATION on 5-min bars --
     reaction candle + RSI exhaustion + volume (strategies/entry_confirmation.py).
  5. Stop beyond the zone; target at a fixed reward:risk multiple.

This is a swing-style setup (hourly/daily zones), not an intraday-only system
like the ORB bot -- trades are allowed to run for multiple days, capped by
max_hold_bars as a backstop against indefinite holds.

Usage
-----
    python -m backtest.zone_backtest                # all symbols with cached 5m+1h data
    python -m backtest.zone_backtest TORNTPHARM      # single symbol, trade log
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.fetch_nse_data import load_nse_bars_df
from backtest.fetch_nse_multi_tf import load_bars_df
from strategies.entry_confirmation import check_entry
from strategies.zone_detector import Zone, find_confluence, find_zones

DATA_DIR = Path(__file__).parent / "data"
RESULTS_DIR = Path(__file__).parent / "results"


def _confluent_execution_zone(h_zone: Zone, m_zone: Zone) -> Zone:
    """Build the tradeable zone from a confluence pair -- 5-min boundaries (tighter/more
    precise), hourly's kind/pattern (the validated "bigger picture" reversal)."""
    return Zone(
        kind=h_zone.kind,
        pattern=h_zone.pattern,
        price_low=m_zone.price_low,
        price_high=m_zone.price_high,
        base_start=m_zone.base_start,
        base_end=m_zone.base_end,
        breakout_ts=max(h_zone.breakout_ts, m_zone.breakout_ts),
        breakout_move_atr=h_zone.breakout_move_atr,
        breakout_volume_ratio=h_zone.breakout_volume_ratio,
        legin_move_atr=h_zone.legin_move_atr,
        legin_volume_ratio=h_zone.legin_volume_ratio,
        timeframe="confluence",
    )


def run_symbol(
    symbol: str,
    stop_buffer_pct: float = 0.002,
    target_rr: float = 2.0,
    max_hold_bars: int = 1500,   # ~ a few weeks of 5-min bars; backstop, not the primary exit
) -> list[dict]:
    h_path = DATA_DIR / f"{symbol}_NSE_1h.parquet"
    m_path = DATA_DIR / f"{symbol}_NSE_5m.parquet"
    if not h_path.exists() or not m_path.exists():
        return []

    hourly = load_bars_df(h_path)
    five = load_nse_bars_df(m_path)

    h_zones = [z for z in find_zones(hourly, timeframe="1h") if z.is_reversal]
    m_zones = find_zones(five, timeframe="5m")
    pairs = find_confluence(h_zones, m_zones)
    if not pairs:
        return []

    trades: list[dict] = []
    five_idx = five.index

    for h_zone, m_zone in pairs:
        zone = _confluent_execution_zone(h_zone, m_zone)
        start_pos = five_idx.searchsorted(zone.breakout_ts, side="right")
        if start_pos < 25:
            start_pos = 25
        if start_pos >= len(five):
            continue

        # walk forward from when the zone is confirmed, looking for a confirmed reaction
        entry_pos = None
        signal = None
        for i in range(start_pos, len(five)):
            sub = five.iloc[: i + 1]
            sig = check_entry(sub, zone)
            if sig:
                entry_pos = i
                signal = sig
                break

        if entry_pos is None:
            continue

        entry_price = signal.price
        is_long = zone.kind == "demand"
        if is_long:
            stop = zone.price_low * (1 - stop_buffer_pct)
            risk = entry_price - stop
            if risk <= 0:
                continue
            target = entry_price + risk * target_rr
        else:
            stop = zone.price_high * (1 + stop_buffer_pct)
            risk = stop - entry_price
            if risk <= 0:
                continue
            target = entry_price - risk * target_rr

        exit_price, exit_reason, exit_pos = None, None, None
        hold_end = min(entry_pos + 1 + max_hold_bars, len(five))
        for j in range(entry_pos + 1, hold_end):
            bar = five.iloc[j]
            if is_long:
                if bar["low"] <= stop:
                    exit_price, exit_reason = stop, "stop_loss"
                elif bar["high"] >= target:
                    exit_price, exit_reason = target, "take_profit"
            else:
                if bar["high"] >= stop:
                    exit_price, exit_reason = stop, "stop_loss"
                elif bar["low"] <= target:
                    exit_price, exit_reason = target, "take_profit"
            if exit_price is not None:
                exit_pos = j
                break

        if exit_price is None:
            exit_pos = hold_end - 1
            exit_price = float(five.iloc[exit_pos]["close"])
            exit_reason = "max_hold"

        pnl_pct = (
            (exit_price - entry_price) / entry_price * 100 if is_long
            else (entry_price - exit_price) / entry_price * 100
        )
        trades.append({
            "symbol": symbol,
            "direction": "long" if is_long else "short",
            "pattern": zone.pattern,
            "zone_low": zone.price_low,
            "zone_high": zone.price_high,
            "zone_strength": h_zone.strength,
            "entry_ts": five_idx[entry_pos],
            "entry_price": round(entry_price, 2),
            "reaction_pattern": signal.pattern,
            "reaction_rsi": round(signal.rsi, 1),
            "reaction_vol_ratio": round(signal.volume_ratio, 2),
            "exit_ts": five_idx[exit_pos],
            "exit_price": round(exit_price, 2),
            "exit_reason": exit_reason,
            "pnl_pct": round(pnl_pct, 3),
            "hold_bars": exit_pos - entry_pos,
        })

    return trades


# ---------------------------------------------------------------------------
# Stats (same shape as backtest/pandas_backtest.py, pnl_pct-based since this
# runner doesn't model position sizing -- just the trade's % return)
# ---------------------------------------------------------------------------

def _full_stats(trades: list[dict]) -> dict:
    import math
    import statistics as _stats

    pnls = [t["pnl_pct"] for t in trades]
    n = len(pnls)
    if n == 0:
        return {}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    p_win = len(wins) / n
    gross_wins = sum(wins)
    gross_losses = abs(sum(losses))
    profit_factor = gross_wins / (gross_losses or 0.001)
    avg_win = gross_wins / len(wins) if wins else 0.0
    avg_loss = gross_losses / len(losses) if losses else 0.0
    expectancy = p_win * avg_win - (1 - p_win) * avg_loss
    max_cl, cur_cl = 0, 0
    for p in pnls:
        if p <= 0:
            cur_cl += 1
            max_cl = max(max_cl, cur_cl)
        else:
            cur_cl = 0
    return dict(
        n=n, wins=len(wins), losses=len(losses), win_rate=round(p_win * 100, 1),
        profit_factor=round(profit_factor, 2), avg_win_pct=round(avg_win, 2),
        avg_loss_pct=round(avg_loss, 2), expectancy_pct=round(expectancy, 3),
        total_pnl_pct=round(sum(pnls), 2), max_consec_losses=max_cl,
    )


def _print_full_stats(stats: dict, label: str) -> None:
    if not stats:
        print(f"  {label}: no trades.")
        return
    print(f"\n  -- {label} --------------------------------------------")
    print(f"  Trades              : {stats['n']}")
    print(f"  Win rate            : {stats['win_rate']:.1f}%")
    print(f"  Profit factor       : {stats['profit_factor']:.2f}")
    print(f"  Expectancy / trade  : {stats['expectancy_pct']:+.3f}%")
    print(f"  Avg win / avg loss  : {stats['avg_win_pct']:+.2f}% / {-stats['avg_loss_pct']:+.2f}%")
    print(f"  Max consec losses   : {stats['max_consec_losses']}")
    print(f"  Total return (sum)  : {stats['total_pnl_pct']:+.2f}%")


if __name__ == "__main__":
    args = sys.argv[1:]
    default_syms = ["TORNTPHARM", "ADANIENT", "TRENT", "PIDILITIND", "EICHERMOT", "HAL",
                     "BAJFINANCE", "APOLLOHOSP", "ICICIBANK", "FEDERALBNK", "BRITANNIA",
                     "TECHM", "BHARTIARTL"]
    syms = args or default_syms

    all_trades: list[dict] = []
    for sym in syms:
        trades = run_symbol(sym)
        all_trades.extend(trades)
        if trades:
            wins = sum(1 for t in trades if t["pnl_pct"] > 0)
            print(f"{sym:<12} {len(trades)} trade(s)  {wins}W/{len(trades)-wins}L  "
                  f"total={sum(t['pnl_pct'] for t in trades):+.2f}%")
        else:
            print(f"{sym:<12} 0 trades")

    if len(args) == 1:
        print(f"\nTrade log for {args[0]}:")
        for t in all_trades:
            print(f"  {t['entry_ts']} {t['direction']:<5} {t['pattern']} @ {t['entry_price']:>9.2f} "
                  f"[{t['reaction_pattern']}, RSI={t['reaction_rsi']}, vol={t['reaction_vol_ratio']}x]"
                  f"  -> {t['exit_ts']} @ {t['exit_price']:>9.2f}  {t['exit_reason']:<12} "
                  f"{t['pnl_pct']:+.2f}%  ({t['hold_bars']} bars)")

    RESULTS_DIR.mkdir(exist_ok=True)
    if all_trades:
        pd.DataFrame(all_trades).to_csv(RESULTS_DIR / "zone_backtest_trades.csv", index=False)
        _print_full_stats(_full_stats(all_trades), "Combined (all symbols)")
        print(f"\n  Saved -> {RESULTS_DIR / 'zone_backtest_trades.csv'}")
    else:
        print("\nNo trades across any symbol.")
