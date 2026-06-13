"""
Fetch NSE 5-min OHLCV bars via AngelOne SmartAPI for the India ORB backtest.

SmartAPI supports up to 1-year of historical 5-min data per request, well
beyond yfinance's 60-day cap.  Each API call is limited to a 30-day window
so this script makes multiple calls going backwards from today.

Results are saved to the same parquet format as fetch_nse_data.py so the
backtest runner can use either data source transparently.

Prerequisites
-------------
  pip install smartapi-python pyotp python-dotenv
  Set ANGELONE_API_KEY, ANGELONE_CLIENT_CODE, ANGELONE_PASSWORD,
      ANGELONE_TOTP_SECRET in .env (same as india_orb_bot.py)

Usage
-----
    # All 9 backtest symbols, 6 months back
    python -m backtest.fetch_nse_data_smartapi --all

    # Specific symbols
    python -m backtest.fetch_nse_data_smartapi RELIANCE HCLTECH

    # Custom lookback (months)
    python -m backtest.fetch_nse_data_smartapi --all --months 3
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import INDIA_SYMBOLS

load_dotenv()

IST      = ZoneInfo("Asia/Kolkata")
DATA_DIR = Path(__file__).parent / "data"

# NSE trading session in IST
_NSE_START = "09:15"
_NSE_END   = "15:30"

# Chunk size SmartAPI supports reliably (days)
_CHUNK_DAYS = 28

# Throttle between API calls to avoid rate-limiting
_SLEEP_BETWEEN_CALLS = 1.0  # seconds


def _make_smartapi_client():
    """Authenticate with AngelOne SmartAPI. Returns SmartConnect instance."""
    try:
        import pyotp
        from SmartApi import SmartConnect
    except ImportError:
        print("ERROR: Install smartapi-python and pyotp:\n  pip install smartapi-python pyotp")
        sys.exit(1)

    api_key     = os.environ.get("ANGELONE_API_KEY", "")
    client_code = os.environ.get("ANGELONE_CLIENT_CODE", "")
    password    = os.environ.get("ANGELONE_PASSWORD", "")
    totp_secret = os.environ.get("ANGELONE_TOTP_SECRET", "")

    if not all([api_key, client_code, password, totp_secret]):
        print(
            "ERROR: Set ANGELONE_API_KEY, ANGELONE_CLIENT_CODE, "
            "ANGELONE_PASSWORD, ANGELONE_TOTP_SECRET in .env"
        )
        sys.exit(1)

    obj = SmartConnect(api_key=api_key)
    totp = pyotp.TOTP(totp_secret).now()
    data = obj.generateSession(client_code, password, totp)
    if not data.get("status"):
        print(f"ERROR: SmartAPI auth failed — {data.get('message', 'unknown error')}")
        sys.exit(1)

    print(f"Authenticated as {client_code}")
    return obj


def _resolve_token(obj, symbol: str) -> str | None:
    """Look up the NSE EQ token for a symbol via SmartAPI searchScrip."""
    try:
        resp = obj.searchScrip("NSE", symbol)
        if resp and resp.get("status"):
            for item in resp.get("data", []):
                if item.get("symbol", "").upper() == symbol.upper():
                    return str(item["symboltoken"])
            # If exact match not found, return first result
            data = resp.get("data", [])
            if data:
                return str(data[0]["symboltoken"])
    except Exception as e:
        print(f"  WARN  {symbol}: searchScrip failed — {e}")
    return None


def _fetch_chunk(obj, token: str, symbol: str,
                 from_dt: datetime, to_dt: datetime) -> pd.DataFrame | None:
    """
    Fetch one 28-day chunk of 5-min bars from SmartAPI.
    Returns DataFrame with UTC index or None on failure.
    """
    fmt = "%Y-%m-%d %H:%M"
    params = {
        "exchange":    "NSE",
        "symboltoken": token,
        "interval":    "FIVE_MINUTE",
        "fromdate":    from_dt.strftime(fmt),
        "todate":      to_dt.strftime(fmt),
    }
    try:
        resp = obj.getCandleData(params)
    except Exception as e:
        print(f"  WARN  {symbol}: API error {from_dt.date()}→{to_dt.date()} — {e}")
        return None

    if not resp or not resp.get("status"):
        msg = resp.get("message", "no data") if resp else "no response"
        print(f"  WARN  {symbol}: {from_dt.date()}→{to_dt.date()} — {msg}")
        return None

    candles = resp.get("data", [])
    if not candles:
        return None

    rows = []
    for c in candles:
        # SmartAPI returns [[ts, open, high, low, close, volume], ...]
        try:
            ts_str, o, h, l, cl, v = c[0], c[1], c[2], c[3], c[4], c[5]
            # ts format: "2024-01-15T09:15:00+05:30"
            ts = pd.to_datetime(ts_str, utc=True)
            rows.append({"timestamp": ts, "open": o, "high": h,
                         "low": l, "close": cl, "volume": v})
        except (IndexError, ValueError):
            continue

    if not rows:
        return None

    df = pd.DataFrame(rows).set_index("timestamp")
    df.index = pd.DatetimeIndex(df.index, tz="UTC")
    return df


def fetch_nse_bars_smartapi(
    obj,
    symbol: str,
    months: int = 6,
    output_dir: Path = DATA_DIR,
) -> Path:
    """
    Download up to `months` months of NSE 5-min bars for `symbol` via SmartAPI
    and save/merge into the backtest data cache.

    Returns path to the saved parquet file.
    """
    token = _resolve_token(obj, symbol)
    if token is None:
        raise ValueError(f"Could not resolve SmartAPI token for {symbol}")

    now_ist = datetime.now(IST)
    # End: market close today (or now if during market hours)
    to_ist = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
    if now_ist < to_ist:
        to_ist = now_ist

    # Start: `months` months back from today at market open
    from_ist = to_ist - timedelta(days=months * 31)
    from_ist = from_ist.replace(hour=9, minute=15, second=0, microsecond=0)

    chunks = []
    cursor = from_ist

    while cursor < to_ist:
        chunk_end = min(cursor + timedelta(days=_CHUNK_DAYS), to_ist)
        df = _fetch_chunk(obj, token, symbol, cursor, chunk_end)
        if df is not None and not df.empty:
            chunks.append(df)
            print(f"  {symbol}: {cursor.date()} → {chunk_end.date()}  {len(df)} bars")
        else:
            print(f"  {symbol}: {cursor.date()} → {chunk_end.date()}  (no data)")
        cursor = chunk_end + timedelta(minutes=5)
        time.sleep(_SLEEP_BETWEEN_CALLS)

    if not chunks:
        raise ValueError(f"No data returned for {symbol}")

    raw = pd.concat(chunks)
    raw = raw[~raw.index.duplicated(keep="last")].sort_index()

    # Filter to NSE session hours
    ist_df = raw.copy()
    ist_df.index = ist_df.index.tz_convert(IST)
    ist_df = ist_df.between_time(_NSE_START, _NSE_END)
    if ist_df.empty:
        raise ValueError(f"No bars in NSE trading hours for {symbol}")
    ist_df.index = ist_df.index.tz_convert("UTC")
    raw = ist_df

    # Merge with any existing cached data
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{symbol}_NSE_5m.parquet"
    if path.exists():
        existing = pd.read_parquet(path)
        if existing.index.tz is None:
            existing.index = existing.index.tz_localize("UTC")
        combined = pd.concat([existing, raw])
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
        raw = combined

    raw.to_parquet(path)
    return path


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fetch NSE 5-min data via AngelOne SmartAPI")
    parser.add_argument("symbols", nargs="*", help="NSE symbols to fetch (e.g. RELIANCE HCLTECH)")
    parser.add_argument("--all", action="store_true", help="Fetch all INDIA_SYMBOLS from config.py")
    parser.add_argument("--months", type=int, default=6, help="Months of history to fetch (default: 6)")
    args = parser.parse_args()

    symbols = INDIA_SYMBOLS if args.all else (args.symbols or INDIA_SYMBOLS)

    print(f"Fetching {args.months} months of NSE 5-min data for {len(symbols)} symbols...\n")

    obj = _make_smartapi_client()

    ok, fail = [], []
    for sym in symbols:
        try:
            path = fetch_nse_bars_smartapi(obj, sym, months=args.months)
            from backtest.fetch_nse_data import load_nse_bars_df
            df   = load_nse_bars_df(path)
            days = df.index.normalize().nunique()
            print(f"  OK   {sym:<12} {len(df):>5} bars  {days} trading days  → {path.name}\n")
            ok.append(sym)
        except Exception as e:
            print(f"  ERR  {sym:<12} {e}\n")
            fail.append(sym)

    print(f"\n{len(ok)} succeeded  |  {len(fail)} failed")
    if fail:
        print(f"Failed: {', '.join(fail)}")
