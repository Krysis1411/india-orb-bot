"""
NSE symbol universe for the supply/demand zone strategy.

Separate from india_screener.py (the ORB bot's watchlist) on purpose — the
zone strategy has no reason to inherit ORB-specific curation. See the
ZONE_SYMBOLS comment in config.py for why.

Currently static (config.ZONE_SYMBOLS, no blocklist yet — nothing's been
backtested long enough on this strategy to justify excluding a symbol).
get_zone_symbols() exists as the single entry point so a future dynamic
filter (liquidity, volatility, whatever the zone backtest ends up needing)
has one place to plug in without touching every caller.
"""
from __future__ import annotations

import logging

from config import ZONE_SYMBOLS

log = logging.getLogger(__name__)


def get_zone_symbols() -> list[str]:
    """Return the NSE symbol universe to scan for supply/demand zones."""
    log.info(f"Zone universe ({len(ZONE_SYMBOLS)} symbols): {', '.join(ZONE_SYMBOLS)}")
    return list(ZONE_SYMBOLS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
    syms = get_zone_symbols()
    print(f"\n{len(syms)} symbols in the zone universe:\n{', '.join(syms)}")
