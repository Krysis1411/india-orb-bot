"""
Supply/demand zone backtest — 5-min zones (tradeable on their own) + optional
higher-timeframe confluence (a strength boost, not a requirement) + multi-touch
+ entry confirmation.

Full trade pipeline:
  1. Find REVERSAL zones (DBR/RBD) independently on both 1h and 5m.
  2. 5-min zones are the tradeable unit -- their tighter boundaries are what
     price actually interacts with. An overlapping 1h zone is recorded as
     `confluent=True` and logged, not required -- per hand-verified real
     chart examples (RECLTD/SBIN), a clean 5-min-only reversal is a real,
     tradeable setup; a matching 1h zone just makes it a stronger one.
  3. Zones are no longer one-shot: a still-valid zone can produce MULTIPLE
     trades as price returns to it (per the SHREECEM example -- the same
     supply zone rejected price twice). Capped at MAX_TOUCHES per zone, and
     retired early if price closes decisively through it (`is_invalidated_by`).
     touch_number is recorded on every trade specifically so win-rate-by-touch
     can be checked empirically -- theory says later touches should be weaker
     (resting orders get used up), but that's a claim to verify against real
     results, not something to bake into the entry logic blind.
  4. Within a live zone, wait for entry CONFIRMATION (strategies/entry_confirmation.py).
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
from strategies.indicators import has_bearish_divergence, has_bullish_divergence
from strategies.zone_detector import Zone, find_zones

DATA_DIR = Path(__file__).parent / "data"
RESULTS_DIR = Path(__file__).parent / "results"

MAX_TOUCHES = 3
INVALIDATION_BUFFER_PCT = 0.002


def _confluent_hourly_zone(m_zone: Zone, h_zones: list[Zone]) -> Zone | None:
    """The overlapping hourly reversal zone for this 5-min zone, if any (first match)."""
    for hz in h_zones:
        if hz.kind == m_zone.kind and m_zone.price_low <= hz.price_high and hz.price_low <= m_zone.price_high:
            return hz
    return None


def run_symbol(
    symbol: str,
    stop_buffer_pct: float = 0.002,
    target_rr: float = 2.0,
    max_hold_bars: int = 1500,   # ~ a few weeks of 5-min bars; backstop, not the primary exit
    max_touches: int = MAX_TOUCHES,
) -> list[dict]:
    h_path = DATA_DIR / f"{symbol}_NSE_1h.parquet"
    m_path = DATA_DIR / f"{symbol}_NSE_5m.parquet"
    if not m_path.exists():
        return []

    five = load_nse_bars_df(m_path)
    m_zones = [z for z in find_zones(five, timeframe="5m") if z.is_reversal]
    if not m_zones:
        return []

    h_zones: list[Zone] = []
    if h_path.exists():
        hourly = load_bars_df(h_path)
        h_zones = [z for z in find_zones(hourly, timeframe="1h") if z.is_reversal]

    trades: list[dict] = []
    five_idx = five.index
    n = len(five)

    for zone in m_zones:
        confluent_hz = _confluent_hourly_zone(zone, h_zones)
        is_confluent = confluent_hz is not None

        start_pos = five_idx.searchsorted(zone.breakout_ts, side="right")
        start_pos = max(start_pos, 25)
        if start_pos >= n:
            continue

        pos = start_pos
        while pos < n and zone.touches < max_touches and not zone.broken:
            close = float(five.iloc[pos]["close"])
            if zone.is_invalidated_by(close, INVALIDATION_BUFFER_PCT):
                zone.broken = True
                break

            sub = five.iloc[: pos + 1]
            sig = check_entry(sub, zone)
            if sig is None:
                pos += 1
                continue

            zone.touches += 1
            entry_pos = pos
            entry_price = sig.price
            is_long = zone.kind == "demand"
            has_divergence = has_bullish_divergence(sub) if is_long else has_bearish_divergence(sub)
            if is_long:
                stop = zone.price_low * (1 - stop_buffer_pct)
                risk = entry_price - stop
                target = entry_price + risk * target_rr if risk > 0 else None
            else:
                stop = zone.price_high * (1 + stop_buffer_pct)
                risk = stop - entry_price
                target = entry_price - risk * target_rr if risk > 0 else None

            if target is None:
                pos += 1
                continue

            exit_price, exit_reason, exit_pos = None, None, None
            hold_end = min(entry_pos + 1 + max_hold_bars, n)
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
                "zone_strength": zone.strength,
                "confluent": is_confluent,
                "confluent_hourly_pattern": confluent_hz.pattern if confluent_hz else None,
                "has_divergence": has_divergence,
                "touch_number": zone.touches,
                "entry_ts": five_idx[entry_pos],
                "entry_price": round(entry_price, 2),
                "reaction_pattern": sig.pattern,
                "reaction_rsi": round(sig.rsi, 1),
                "reaction_vol_ratio": round(sig.volume_ratio, 2),
                "exit_ts": five_idx[exit_pos],
                "exit_price": round(exit_price, 2),
                "exit_reason": exit_reason,
                "pnl_pct": round(pnl_pct, 3),
                "hold_bars": exit_pos - entry_pos,
            })

            # resume scanning for a FURTHER touch only after this trade has closed
            pos = exit_pos + 1

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


def _print_breakdown(trades: list[dict]) -> None:
    """Checks the two hypotheses from the reversal-zone chart review empirically,
    instead of assuming them: do later touches perform worse, does confluence help."""
    if not trades:
        return

    print(f"\n  -- By touch number (does zone strength decay with re-tests?) ------")
    print(f"  {'Touch #':<8} {'Trades':>7} {'Win%':>7} {'PF':>6} {'Total%':>8}")
    by_touch: dict[int, list[dict]] = {}
    for t in trades:
        by_touch.setdefault(t["touch_number"], []).append(t)
    for touch_num in sorted(by_touch):
        s = _full_stats(by_touch[touch_num])
        print(f"  {touch_num:<8} {s['n']:>7} {s['win_rate']:>6.1f}% {s['profit_factor']:>6.2f} {s['total_pnl_pct']:>+7.2f}%")

    print(f"\n  -- Confluent (1h+5m overlap) vs 5-min-only ------------------------")
    print(f"  {'Group':<14} {'Trades':>7} {'Win%':>7} {'PF':>6} {'Total%':>8}")
    for label, group in [("Confluent", [t for t in trades if t["confluent"]]),
                          ("5-min only", [t for t in trades if not t["confluent"]])]:
        s = _full_stats(group)
        if not s:
            print(f"  {label:<14} {'0':>7}")
            continue
        print(f"  {label:<14} {s['n']:>7} {s['win_rate']:>6.1f}% {s['profit_factor']:>6.2f} {s['total_pnl_pct']:>+7.2f}%")

    print(f"\n  -- MACD divergence vs none (per tradingsetupsreview.com confluence) -")
    print(f"  {'Group':<14} {'Trades':>7} {'Win%':>7} {'PF':>6} {'Total%':>8}")
    for label, group in [("Divergence", [t for t in trades if t["has_divergence"]]),
                          ("No divergence", [t for t in trades if not t["has_divergence"]])]:
        s = _full_stats(group)
        if not s:
            print(f"  {label:<14} {'0':>7}")
            continue
        print(f"  {label:<14} {s['n']:>7} {s['win_rate']:>6.1f}% {s['profit_factor']:>6.2f} {s['total_pnl_pct']:>+7.2f}%")


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
        _print_breakdown(all_trades)
        print(f"\n  Saved -> {RESULTS_DIR / 'zone_backtest_trades.csv'}")
    else:
        print("\nNo trades across any symbol.")
