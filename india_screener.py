"""
Pre-market NSE screener for the India ORB bot.

Ranks a pool of ~50 liquid NSE stocks by previous-day rupee turnover
(close price × volume) using yfinance — no AngelOne auth required.

Called ONCE at bot startup (~08:50 IST), before the ORB window opens.
The result is fixed for the whole session; the bot does not re-rank
mid-day. Fallback: INDIA_SYMBOLS from config.py (the backtested shortlist).
"""
from __future__ import annotations

import logging

import pandas as pd
import yfinance as yf

from config import INDIA_BLOCKLIST, INDIA_SCREENER_LIMIT, INDIA_SYMBOLS

log = logging.getLogger(__name__)

# Curated pool of ~50 liquid NSE stocks suitable for ORB.
# Excludes INDIA_BLOCKLIST losers. Covers Nifty 100/200 across key sectors.
# The screener picks the top INDIA_SCREENER_LIMIT by yesterday's turnover daily.
NSE_UNIVERSE = [
    # Confirmed ORB performers (backtested shortlist)
    "SUNPHARMA", "ADANIENT", "JSWSTEEL", "POWERGRID", "HCLTECH",
    "BAJFINANCE", "ONGC", "RELIANCE", "BHARTIARTL",
    # Banks & Financials
    "AXISBANK", "INDUSINDBK", "BAJAJFINSV", "BANKBARODA",
    "IDFCFIRSTB", "FEDERALBNK", "CHOLAFIN", "MUTHOOTFIN",
    # Auto
    "HEROMOTOCO", "EICHERMOT", "TVSMOTOR",
    # Metals & Materials
    "TATASTEEL", "HINDALCO", "VEDL", "GRASIM", "ULTRACEMCO",
    # Pharma & Healthcare
    "DRREDDY", "CIPLA", "DIVISLAB", "APOLLOHOSP", "TORNTPHARM",
    # IT (non-blocklisted)
    "TECHM",
    # Energy
    "BPCL", "COALINDIA",
    # Consumer / FMCG
    "BRITANNIA", "DABUR", "MARICO", "TATACONSUM", "GODREJCP",
    # Capital Goods / Defence
    "HAL", "BEL", "HAVELLS", "SIEMENS", "ABB",
    # Others
    "TRENT", "PIDILITIND",
]


def _fetch_prev_day_data(symbols: list[str]) -> dict[str, dict]:
    """
    Batch-fetch previous trading day's Close and Volume for NSE symbols
    via yfinance (SYMBOL.NS suffix).  Returns {symbol: {close, volume, turnover}}.
    Empty dict on failure.
    """
    yf_syms = [f"{s}.NS" for s in symbols]

    try:
        raw = yf.download(
            " ".join(yf_syms),
            period="5d",
            interval="1d",
            progress=False,
            auto_adjust=True,
        )
    except Exception as e:
        log.warning(f"Screener: yfinance download failed — {e}")
        return {}

    if raw is None or (hasattr(raw, "empty") and raw.empty):
        return {}

    result: dict[str, dict] = {}

    for sym, yf_sym in zip(symbols, yf_syms):
        try:
            if isinstance(raw.columns, pd.MultiIndex):
                # Multi-ticker download: try (metric, ticker) layout first,
                # then fall back to (ticker, metric) layout.
                try:
                    close_s  = raw["Close"][yf_sym].dropna()
                    volume_s = raw["Volume"][yf_sym].dropna()
                except KeyError:
                    close_s  = raw[yf_sym]["Close"].dropna()
                    volume_s = raw[yf_sym]["Volume"].dropna()
            else:
                # Single-ticker layout (shouldn't happen in batch, but safe fallback)
                close_s  = raw["Close"].dropna()
                volume_s = raw["Volume"].dropna()

            if close_s.empty or volume_s.empty:
                continue

            close  = float(close_s.iloc[-1])
            volume = float(volume_s.iloc[-1])

            if close > 0 and volume > 0:
                result[sym] = {
                    "close":    close,
                    "volume":   volume,
                    "turnover": close * volume,
                }
        except Exception:
            pass

    return result


def get_active_nse_symbols(
    n: int | None = None,
    universe: list[str] | None = None,
) -> list[str]:
    """
    Return the top `n` NSE symbols ranked by previous-day rupee turnover
    from NSE_UNIVERSE (minus INDIA_BLOCKLIST).

    Falls back to INDIA_SYMBOLS if yfinance fails.
    Call this ONCE at bot startup — not inside the 5-min trading loop.
    """
    n = n or INDIA_SCREENER_LIMIT
    blocklist = set(INDIA_BLOCKLIST)
    candidates = [s for s in (universe or NSE_UNIVERSE) if s not in blocklist]

    log.info(f"Screener: ranking {len(candidates)} symbols by prev-day turnover (yfinance)...")

    data = _fetch_prev_day_data(candidates)

    if not data:
        log.warning("Screener: yfinance returned no data — falling back to INDIA_SYMBOLS")
        fallback = [s for s in INDIA_SYMBOLS if s not in blocklist][:n]
        log.info(f"Fallback ({len(fallback)}): {', '.join(fallback)}")
        return fallback

    ranked = sorted(data.items(), key=lambda x: x[1]["turnover"], reverse=True)
    top    = ranked[:n]

    log.info(f"Today's top {n} by prev-day rupee turnover:")
    for sym, d in top:
        log.info(
            f"  {sym:<14} ₹{d['close']:.0f}  "
            f"vol {d['volume'] / 1e6:.1f}M  "
            f"turnover ₹{d['turnover'] / 1e7:.1f}Cr"
        )

    selected = [sym for sym, _ in top]

    # Surface any backtested favourites that didn't make today's cut
    known_good = [s for s in INDIA_SYMBOLS if s not in selected and s not in blocklist]
    if known_good:
        log.info(f"  (backtested symbols outside today's top {n}: {', '.join(known_good)})")

    return selected


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
    syms = get_active_nse_symbols()
    print(f"\nSelected {len(syms)} symbols for today: {', '.join(syms)}")
