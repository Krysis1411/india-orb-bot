"""
NSE symbol selector for the India ORB bot.

Returns the fixed backtested 12-symbol watchlist from config.INDIA_SYMBOLS.

The previous turnover-based screener (yfinance batch download + ranking) was
retired after 60-day grid-search showed it consistently picks the wrong names:
high-turnover stocks like RELIANCE and HDFC have negligible ORB edges on NSE,
while the best ORB candidates (TORNTPHARM, BHARTIARTL, JSWSTEEL) rarely surface
in a top-15-by-turnover ranking.

Called ONCE at bot startup; result is fixed for the whole session.
"""
from __future__ import annotations

import logging

from config import INDIA_BLOCKLIST, INDIA_SYMBOLS

log = logging.getLogger(__name__)


def get_active_nse_symbols(
    n: int | None = None,
    universe: list[str] | None = None,
) -> list[str]:
    """
    Return the fixed backtested NSE watchlist, minus any symbols on INDIA_BLOCKLIST.

    Parameters `n` and `universe` are accepted for API compatibility but ignored —
    the watchlist is static and does not vary by session.
    """
    blocklist = set(INDIA_BLOCKLIST)
    selected = [s for s in INDIA_SYMBOLS if s not in blocklist]
    log.info(f"Watchlist ({len(selected)} symbols): {', '.join(selected)}")
    return selected


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
    syms = get_active_nse_symbols()
    print(f"\nSelected {len(syms)} symbols for today: {', '.join(syms)}")
