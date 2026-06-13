"""
NSE top movers screener for India ORB bot.

Primary: NSE live most-active endpoint (by volume, session-based request).
Fallback: static INDIA_SYMBOLS list from config.py.
"""
import logging
import requests

from config import INDIA_SCREENER_LIMIT, INDIA_SYMBOLS

log = logging.getLogger(__name__)

# NSE blocks direct XHR — a session cookie from the homepage is required first.
_NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}


def _fetch_nse_most_active() -> list[str]:
    """
    Fetch the most actively traded NSE stocks by value from the NSE live API.
    Requires a browser-like session cookie obtained from the NSE homepage.
    """
    session = requests.Session()
    # Seed the session with a cookie from the main site
    session.get("https://www.nseindia.com", headers=_NSE_HEADERS, timeout=15)
    resp = session.get(
        "https://www.nseindia.com/api/live-analysis-stocksTraded",
        params={"index": "val", "limit": INDIA_SCREENER_LIMIT},
        headers=_NSE_HEADERS,
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    symbols = [d["symbol"] for d in (data.get("data") or [])[:INDIA_SCREENER_LIMIT]]
    return symbols


def get_active_nse_symbols() -> list[str]:
    """
    Return up to INDIA_SCREENER_LIMIT NSE stock symbols ranked by trading activity.
    Falls back to the static INDIA_SYMBOLS list from config.py if live fetch fails.
    """
    try:
        symbols = _fetch_nse_most_active()
        if symbols:
            log.info(f"NSE screener: {len(symbols)} symbols — {', '.join(symbols)}")
            return symbols
        log.warning("NSE screener returned empty list — using fallback")
    except Exception as e:
        log.warning(f"NSE screener failed ({e}) — using INDIA_SYMBOLS fallback")

    fallback = list(INDIA_SYMBOLS[:INDIA_SCREENER_LIMIT])
    log.info(f"Fallback symbols ({len(fallback)}): {', '.join(fallback)}")
    return fallback


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("NSE active symbols:", get_active_nse_symbols())
