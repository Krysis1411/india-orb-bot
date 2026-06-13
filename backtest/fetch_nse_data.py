"""
Fetch NSE 5-min OHLCV bars via yfinance for the India ORB backtest.

yfinance supports NSE stocks with a ".NS" suffix.  The 5-min lookback is
limited to the last 60 calendar days by Yahoo Finance policy — roughly
2.5 months of trading data.  Run this script periodically to build up a
local cache; each run appends the latest 60 days so the parquet file grows
over time without re-downloading data that already exists.

Usage
-----
    # Download specific symbols
    python -m backtest.fetch_nse_data RELIANCE TCS INFY HDFCBANK

    # Download all INDIA_SYMBOLS from config.py
    python -m backtest.fetch_nse_data --all
"""
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import INDIA_SYMBOLS

IST      = ZoneInfo("Asia/Kolkata")
DATA_DIR = Path(__file__).parent / "data"

# NSE trading session in IST — used to strip pre/post-market rows
_NSE_SESSION_START = "09:15"
_NSE_SESSION_END   = "15:30"


def fetch_nse_bars(symbol: str, output_dir: Path = DATA_DIR) -> Path:
    """
    Download up to 60 days of NSE 5-min bars for *symbol* and save as parquet.

    If a parquet file already exists for this symbol, new bars are merged in
    so old data is preserved.  Returns the path to the saved file.
    """
    yf_sym = f"{symbol}.NS"
    raw = yf.download(yf_sym, period="60d", interval="5m",
                      progress=False, auto_adjust=True)

    if raw.empty:
        raise ValueError(f"yfinance returned no data for {yf_sym}")

    # Flatten MultiIndex columns that yfinance sometimes returns
    if isinstance(raw.columns, pd.MultiIndex):
        raw = raw.droplevel(1, axis=1)

    raw.columns = [c.lower() for c in raw.columns]
    raw = raw[["open", "high", "low", "close", "volume"]].copy()
    raw.index.name = "timestamp"

    # Ensure UTC index
    if raw.index.tz is None:
        raw.index = raw.index.tz_localize("UTC")
    else:
        raw.index = raw.index.tz_convert("UTC")

    # Filter to NSE trading hours (09:15–15:30 IST)
    ist_df = raw.copy()
    ist_df.index = ist_df.index.tz_convert(IST)
    ist_df = ist_df.between_time(_NSE_SESSION_START, _NSE_SESSION_END)
    if ist_df.empty:
        raise ValueError(f"No bars in NSE trading hours for {yf_sym}")
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


def load_nse_bars_df(path: Path) -> pd.DataFrame:
    """Load a saved NSE bars parquet back into a DataFrame with UTC index."""
    df = pd.read_parquet(path)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    return df.sort_index()


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--all" in args:
        symbols = INDIA_SYMBOLS
    else:
        symbols = args or INDIA_SYMBOLS[:5]

    print(f"Fetching NSE 5-min data for {len(symbols)} symbols...\n")
    ok, fail = [], []
    for sym in symbols:
        try:
            path = fetch_nse_bars(sym)
            df   = load_nse_bars_df(path)
            days = df.index.normalize().nunique()
            print(f"  OK   {sym:<12} {len(df):>5} bars  {days} trading days  → {path.name}")
            ok.append(sym)
        except Exception as e:
            print(f"  ERR  {sym:<12} {e}")
            fail.append(sym)

    print(f"\n  {len(ok)} succeeded  |  {len(fail)} failed")
    if fail:
        print(f"  Failed: {', '.join(fail)}")
