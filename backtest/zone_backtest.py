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
     A signal is only ACCEPTED if the resulting stop distance clears
     MIN_RISK_ATR_MULT x the 5-min ATR -- trade-log forensics on an earlier
     run found ~1/3 of trades entered with a stop distance smaller than one
     typical bar's noise, and those stopped out within 1 bar most of the time
     regardless of whether the reversal thesis was right (25-38% win rate vs
     55-58% for properly-spaced trades). If a touch doesn't leave the stop
     room to breathe, it's skipped and the scan keeps looking for a better
     one -- see the comment on MIN_RISK_ATR_MULT below for the numbers.
  5. Stop beyond the zone; target at a fixed reward:risk multiple, with
     breakeven + trailing-stop management on top (Kaufman "Trading Systems and
     Methods" Ch.23 + Elder's Triple-Screen, see docs/kaufman-tsam-notes.md):
     once profit reaches BREAKEVEN_TRIGGER_R, the stop moves to entry price;
     past that, it trails to protect TRAIL_PROFIT_PCT of the peak favorable
     excursion. The stop only ever moves in the favorable direction (never
     retreats), and is evaluated one bar AFTER the excursion that set it, to
     avoid same-bar look-ahead (we don't have intrabar high/low ordering).

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
from strategies.entry_confirmation import check_entry, rsi
from strategies.indicators import has_bearish_divergence, has_bullish_divergence
from strategies.zone_detector import Zone, atr_series, find_zones

DATA_DIR = Path(__file__).parent / "data"
RESULTS_DIR = Path(__file__).parent / "results"

MAX_TOUCHES = 3
INVALIDATION_BUFFER_PCT = 0.002
# check_entry/has_*_divergence only ever look at trailing windows <= ~66 bars
# (rsi/volume lookback, confirmation_window, MACD's lookback+26 warmup). Slicing
# `five.iloc[:pos+1]` from bar 0 every scan bar was O(pos) per bar -> O(n^2) per
# zone; a fixed trailing window gives identical signals at O(1) per bar instead.
WARMUP_WINDOW = 300
BREAKEVEN_TRIGGER_R = 1.0    # move stop to entry once profit reaches this many R
TRAIL_PROFIT_PCT = 0.5       # once past breakeven, protect this fraction of peak profit

# Trade-log forensics (backtest/results/zone_backtest_trades.csv, 102-trade run):
# 33/102 trades entered with the stop-to-entry distance (risk) smaller than a
# typical single 5-min bar's high-low range for that symbol (median stop
# distance 0.17% vs a typical bar range of 0.15-0.28%). Those trades stopped
# out within 1 bar 26/33 times, win rate 30% vs 55% for properly-spaced
# trades -- normal noise was taking the stop out regardless of whether the
# reversal thesis was right. Root cause: the stop buffer is a fixed % beyond
# the zone edge with no floor, so a confirmation that fires late (entry
# already close to or past the zone boundary) produces a stop sitting inside
# ordinary chop. Fix: require the stop distance to be at least this many
# multiples of the 5-min ATR -- if a touch doesn't leave enough room, skip it
# and keep scanning for a better one, rather than taking a trade that's
# already lost before the first bar closes.
MIN_RISK_ATR_MULT = 1.0

# Every rule/parameter in this file (legin thresholds, confirmation_window,
# MAX_TOUCHES, the trailing-stop constants above, ...) has so far been tuned by
# repeatedly re-running against the full cached dataset -- exactly the
# in-sample "torture" Kaufman's Ch.21 says is fine during design, but it means
# NOTHING run against that full range can be reported as a validated result.
# OOS_START marks a holdout suffix that has NOT been used to justify any of the
# above choices. Per Kaufman: out-of-sample data gets used *exactly once* --
# do not re-tune parameters based on the OOS numbers this produces. If the OOS
# result is bad, the honest conclusion is "this rule set doesn't hold up out of
# sample," not "let's adjust one more threshold and check again" (that's
# exactly the contamination that produced the confirmation_window=2 result
# which later inverted on more data).
OOS_START = pd.Timestamp("2026-05-01", tz="UTC")

# Miner's "High Probability Trading Strategies" (see docs/miner-hpts-notes.md) Dual
# Time Frame Momentum filter: for a COUNTERtrend/reversal trade -- which is all we
# ever take, since we only trade DBR/RBD reversal zones -- his own rule table favors
# it precisely when the HIGHER timeframe momentum is stretched into overbought (for a
# short) or oversold (for a long), not just trending. Logged like `confluent` and
# `has_divergence`: a candidate confluence factor to check empirically, not a filter.
HTF_RSI_OVERSOLD = 30.0
HTF_RSI_OVERBOUGHT = 70.0


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
    enforce_body_filter: bool = True,   # False: for review exports -- keep+tag wick-dominated bases
    enforce_gap_filter: bool = True,    # False: for review exports -- keep+tag gap-contaminated legs
) -> list[dict]:
    h_path = DATA_DIR / f"{symbol}_NSE_1h.parquet"
    m_path = DATA_DIR / f"{symbol}_NSE_5m.parquet"
    if not m_path.exists():
        return []

    five = load_nse_bars_df(m_path)
    m_zones = [
        z for z in find_zones(
            five, timeframe="5m",
            enforce_body_filter=enforce_body_filter,
            enforce_gap_filter=enforce_gap_filter,
        ) if z.is_reversal
    ]
    if not m_zones:
        return []
    five_atr = atr_series(five, 14)

    h_zones: list[Zone] = []
    hourly_rsi: pd.Series | None = None
    hourly_idx: pd.DatetimeIndex | None = None
    if h_path.exists():
        hourly = load_bars_df(h_path)
        h_zones = [z for z in find_zones(hourly, timeframe="1h") if z.is_reversal]
        hourly_rsi = rsi(hourly["close"])
        hourly_idx = hourly.index

    def _htf_momentum_supports(direction: str, entry_ts: pd.Timestamp) -> bool:
        """True if the most recently completed 1h bar's RSI is on our side of the
        OB/OS extreme -- oversold for a long, overbought for a short (see
        docs/miner-hpts-notes.md, Dual Time Frame Momentum filter)."""
        if hourly_rsi is None:
            return False
        i = hourly_idx.searchsorted(entry_ts, side="right") - 1
        if i < 0:
            return False
        val = hourly_rsi.iloc[i]
        if pd.isna(val):
            return False
        return val <= HTF_RSI_OVERSOLD if direction == "long" else val >= HTF_RSI_OVERBOUGHT

    trades: list[dict] = []
    five_idx = five.index
    n = len(five)

    for zone in m_zones:
        confluent_hz = _confluent_hourly_zone(zone, h_zones)
        is_confluent = confluent_hz is not None

        # Scan from confirmed_ts (end of the breakout leg), not breakout_ts
        # (its start) -- the zone's breakout_move_atr/volume_ratio aren't
        # knowable until the whole leg has been observed, so starting from
        # breakout_ts lets the backtest "see" a touch/entry in the 1-2 bars
        # before a live system could have known this zone existed at all.
        # Found by building zone_paper_trader.py and replaying real history
        # through it causally (strategies/zone_detector.py Zone.confirmed_ts).
        start_pos = five_idx.searchsorted(zone.confirmed_ts, side="right")
        start_pos = max(start_pos, 25)
        if start_pos >= n:
            continue

        pos = start_pos
        while pos < n and zone.touches < max_touches and not zone.broken:
            close = float(five.iloc[pos]["close"])
            if zone.is_invalidated_by(close, INVALIDATION_BUFFER_PCT):
                zone.broken = True
                break

            sub = five.iloc[max(0, pos + 1 - WARMUP_WINDOW): pos + 1]
            sig = check_entry(sub, zone)
            if sig is None:
                pos += 1
                continue

            entry_pos = pos
            entry_price = sig.price
            is_long = zone.kind == "demand"
            if is_long:
                stop = zone.price_low * (1 - stop_buffer_pct)
                risk = entry_price - stop
            else:
                stop = zone.price_high * (1 + stop_buffer_pct)
                risk = stop - entry_price

            atr_here = five_atr.iloc[entry_pos]
            min_risk = MIN_RISK_ATR_MULT * atr_here if pd.notna(atr_here) and atr_here > 0 else 0.0
            if risk <= 0 or risk < min_risk:
                # No room left for the stop to breathe -- confirmation fired
                # too close to (or past) the zone edge. Not a trade; keep
                # scanning for a touch that leaves the stop outside normal
                # noise instead of taking one that's already lost.
                pos += 1
                continue

            target = entry_price + risk * target_rr if is_long else entry_price - risk * target_rr

            zone.touches += 1
            has_divergence = has_bullish_divergence(sub) if is_long else has_bearish_divergence(sub)
            has_htf_momentum = _htf_momentum_supports("long" if is_long else "short", five_idx[entry_pos])

            exit_price, exit_reason, exit_pos = None, None, None
            hold_end = min(entry_pos + 1 + max_hold_bars, n)
            cur_stop = stop
            peak = entry_price   # best favorable excursion seen so far
            for j in range(entry_pos + 1, hold_end):
                bar = five.iloc[j]
                if bar["volume"] <= 0:
                    # Chart-review finding: a DRREDDY trade's trailing-stop
                    # exit landed on a bar with volume=0 but a real-looking
                    # 12.9-point range (~4.3x normal) -- not a flat print
                    # (which the zero-width zone fix already catches), but a
                    # bar where nothing actually traded despite a nonzero
                    # printed range, most often right at a session boundary.
                    # No real trading happened here, so it can't have
                    # genuinely triggered a stop or target -- skip it and
                    # keep watching for the next real bar. Not rare either:
                    # 0.3-1.3% of bars per symbol had volume=0 in a spot check.
                    continue
                if is_long:
                    if bar["low"] <= cur_stop:
                        exit_price = cur_stop
                        exit_reason = "trailing_stop" if cur_stop > stop else "stop_loss"
                    elif bar["high"] >= target:
                        exit_price, exit_reason = target, "take_profit"
                else:
                    if bar["high"] >= cur_stop:
                        exit_price = cur_stop
                        exit_reason = "trailing_stop" if cur_stop < stop else "stop_loss"
                    elif bar["low"] <= target:
                        exit_price, exit_reason = target, "take_profit"
                if exit_price is not None:
                    exit_pos = j
                    break

                # Update the trailing stop from THIS bar's excursion -- takes
                # effect starting next bar, never this one (avoids assuming
                # intrabar high/low ordering we don't actually have).
                if is_long:
                    peak = max(peak, float(bar["high"]))
                    profit_r = (peak - entry_price) / risk
                    if profit_r >= BREAKEVEN_TRIGGER_R:
                        trail = entry_price + TRAIL_PROFIT_PCT * (peak - entry_price)
                        cur_stop = max(cur_stop, entry_price, trail)
                else:
                    peak = min(peak, float(bar["low"]))
                    profit_r = (entry_price - peak) / risk
                    if profit_r >= BREAKEVEN_TRIGGER_R:
                        trail = entry_price - TRAIL_PROFIT_PCT * (entry_price - peak)
                        cur_stop = min(cur_stop, entry_price, trail)

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
                "has_htf_momentum": has_htf_momentum,
                "passes_body_filter": zone.passes_body_filter,
                "passes_gap_filter": zone.passes_gap_filter,
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

    print(f"\n  -- 1h momentum OB/OS support vs none (Miner's Dual TF filter, see docs/miner-hpts-notes.md) -")
    print(f"  {'Group':<14} {'Trades':>7} {'Win%':>7} {'PF':>6} {'Total%':>8}")
    for label, group in [("HTF support", [t for t in trades if t["has_htf_momentum"]]),
                          ("No HTF support", [t for t in trades if not t["has_htf_momentum"]])]:
        s = _full_stats(group)
        if not s:
            print(f"  {label:<14} {'0':>7}")
            continue
        print(f"  {label:<14} {s['n']:>7} {s['win_rate']:>6.1f}% {s['profit_factor']:>6.2f} {s['total_pnl_pct']:>+7.2f}%")


def _print_walkforward(trades: list[dict], folds: int = 4) -> None:
    """
    Sequential time-ordered folds over the FIXED rule set already in this file
    -- no re-tuning between folds, that would just be slower-motion overfitting.
    This checks something the single in-sample/out-of-sample split can't: is
    performance reasonably consistent across different stretches of time, or
    is any edge concentrated in one lucky period? Per Kaufman Ch.21, this is
    diagnostic only -- if a fold looks bad, the answer is "note it," not
    "adjust a threshold and rerun."
    """
    if not trades:
        return
    ordered = sorted(trades, key=lambda t: t["entry_ts"])
    t_min, t_max = ordered[0]["entry_ts"], ordered[-1]["entry_ts"]
    span = (t_max - t_min) / folds

    print(f"\n  ==================================================================")
    print(f"  WALK-FORWARD  ({folds} sequential time folds, fixed rule set)")
    print(f"  ==================================================================")
    print(f"  {'Fold':<24} {'Trades':>7} {'Win%':>7} {'PF':>6} {'Total%':>8}")
    for i in range(folds):
        fold_start = t_min + span * i
        fold_end = t_min + span * (i + 1) if i < folds - 1 else t_max + pd.Timedelta(seconds=1)
        group = [t for t in ordered if fold_start <= t["entry_ts"] < fold_end]
        label = f"{fold_start.date()} -> {(fold_end - pd.Timedelta(seconds=1)).date()}"
        s = _full_stats(group)
        if not s:
            print(f"  {label:<24} {'0':>7}")
            continue
        print(f"  {label:<24} {s['n']:>7} {s['win_rate']:>6.1f}% {s['profit_factor']:>6.2f} {s['total_pnl_pct']:>+7.2f}%")


def _cached_symbols() -> list[str]:
    """Every symbol with cached 5-min data -- grows automatically as more gets
    fetched, instead of a hand-maintained list that silently goes stale."""
    return sorted(p.name[: -len("_NSE_5m.parquet")] for p in DATA_DIR.glob("*_NSE_5m.parquet"))


if __name__ == "__main__":
    args = sys.argv[1:]
    syms = args or _cached_symbols()

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
        _print_full_stats(_full_stats(all_trades), "Combined, ALL data (in-sample, already tuned against -- not a validation number)")
        _print_breakdown(all_trades)

        is_trades = [t for t in all_trades if t["entry_ts"] < OOS_START]
        oos_trades = [t for t in all_trades if t["entry_ts"] >= OOS_START]
        print(f"\n  ==================================================================")
        print(f"  IN-SAMPLE / OUT-OF-SAMPLE SPLIT  (cutoff {OOS_START.date()})")
        print(f"  ==================================================================")
        _print_full_stats(_full_stats(is_trades), f"In-sample (entry < {OOS_START.date()})")
        _print_full_stats(_full_stats(oos_trades), f"Out-of-sample (entry >= {OOS_START.date()}) -- ONE-SHOT, do not re-tune off this")

        _print_walkforward(all_trades, folds=4)

        print(f"\n  Saved -> {RESULTS_DIR / 'zone_backtest_trades.csv'}")
    else:
        print("\nNo trades across any symbol.")
