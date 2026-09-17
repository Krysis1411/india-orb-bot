"""
Grid search target_rr against NET (cost-adjusted) P&L, reversal-only.

Why this exists: target_rr=2.0 (and BREAKEVEN_TRIGGER_R=1.0, TRAIL_PROFIT_PCT=0.5,
both module constants in zone_backtest.py) have never been re-validated since the
transaction-cost model was added (2026-09-14). They were tuned when P&L was
measured gross -- a fixed ~0.22% round-trip cost changes the math on how much
reward-to-risk is actually needed to clear costs, so the old default is worth
re-checking, not assumed correct.

Scoped to reversal-only (continuation zones are a settled, rejected question as
of 2026-09-17 -- see docs/zone-strategy.md) and run once per target_rr value
across every cached symbol. target_rr only affects the EXIT target level, not
entry acceptance -- so this changes exit outcomes only, comparable apples-to-
apples across the grid.

Usage
-----
    python -m backtest.grid_search_rr
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.zone_backtest import _cached_symbols, _full_stats, run_symbol

TARGET_RR_GRID = [1.5, 2.0, 2.5, 3.0, 4.0]


def main() -> None:
    symbols = _cached_symbols()
    print(f"Grid search over {len(symbols)} cached symbols, reversal-only, target_rr in {TARGET_RR_GRID}\n")

    for rr in TARGET_RR_GRID:
        all_trades = []
        for sym in symbols:
            trades = run_symbol(sym, target_rr=rr, reversal_only=True)
            all_trades.extend(trades)
        s = _full_stats(all_trades)
        if not s:
            print(f"target_rr={rr:<4}  no trades")
            continue
        print(
            f"target_rr={rr:<4}  n={s['n']:>4}  win={s['win_rate']:>5.1f}%  "
            f"PF={s['profit_factor']:>5.2f}  expectancy={s['expectancy_pct']:>+6.3f}%  "
            f"total={s['total_pnl_pct']:>+8.2f}%  max_consec_losses={s['max_consec_losses']}"
        )


if __name__ == "__main__":
    main()
