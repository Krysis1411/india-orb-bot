"""
Fetch NSE daily and hourly OHLCV bars via yfinance — for the supply/demand
zone research (zones are identified on daily/1h, entries executed on 5m).

Unlike the 5-min fetcher (backtest/fetch_nse_data.py, capped at 60 days by
Yahoo policy), daily and hourly data get much longer history: yfinance
allows ~2 years of hourly bars and effectively unlimited daily bars.

Usage
-----
    python -m backtest.fetch_nse_multi_tf --all              # daily + hourly, all INDIA_SYMBOLS
    python -m backtest.fetch_nse_multi_tf RELIANCE TCS        # specific symbols
    python -m backtest.fetch_nse_multi_tf --all --daily-only
"""
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import INDIA_SYMBOLS

IST = ZoneInfo("Asia/Kolkata")
DATA_DIR = Path(__file__).parent / "data"

_NSE_SESSION_START = "09:15"
_NSE_SESSION_END = "15:30"


def _clean_and_merge(raw: pd.DataFrame, path: Path, intraday: bool) -> pd.DataFrame:
    if isinstance(raw.columns, pd.MultiIndex):
        raw = raw.droplevel(1, axis=1)
    raw.columns = [c.lower() for c in raw.columns]
    raw = raw[["open", "high", "low", "close", "volume"]].copy()
    raw.index.name = "timestamp"

    if raw.index.tz is None:
        raw.index = raw.index.tz_localize("UTC")
    else:
        raw.index = raw.index.tz_convert("UTC")

    if intraday:
        ist_df = raw.copy()
        ist_df.index = ist_df.index.tz_convert(IST)
        ist_df = ist_df.between_time(_NSE_SESSION_START, _NSE_SESSION_END)
        ist_df.index = ist_df.index.tz_convert("UTC")
        raw = ist_df

    if raw.empty:
        raise ValueError("no bars after cleaning")

    if path.exists():
        existing = pd.read_parquet(path)
        if existing.index.tz is None:
            existing.index = existing.index.tz_localize("UTC")
        raw = pd.concat([existing, raw])
        raw = raw[~raw.index.duplicated(keep="last")].sort_index()

    return raw


def fetch_daily_bars(symbol: str, output_dir: Path = DATA_DIR) -> Path:
    """Download ~5 years of daily bars (yfinance has no meaningful cap on daily)."""
    yf_sym = f"{symbol}.NS"
    raw = yf.download(yf_sym, period="5y", interval="1d", progress=False, auto_adjust=True)
    if raw.empty:
        raise ValueError(f"yfinance returned no daily data for {yf_sym}")
    path = output_dir / f"{symbol}_NSE_1d.parquet"
    output_dir.mkdir(parents=True, exist_ok=True)
    merged = _clean_and_merge(raw, path, intraday=False)
    merged.to_parquet(path)
    return path


def fetch_hourly_bars(symbol: str, output_dir: Path = DATA_DIR) -> Path:
    """Download ~2 years of hourly bars (yfinance's practical cap for 60m/1h)."""
    yf_sym = f"{symbol}.NS"
    raw = yf.download(yf_sym, period="730d", interval="1h", progress=False, auto_adjust=True)
    if raw.empty:
        raise ValueError(f"yfinance returned no hourly data for {yf_sym}")
    path = output_dir / f"{symbol}_NSE_1h.parquet"
    output_dir.mkdir(parents=True, exist_ok=True)
    merged = _clean_and_merge(raw, path, intraday=True)
    merged.to_parquet(path)
    return path


def load_bars_df(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    return df.sort_index()


if __name__ == "__main__":
    args = sys.argv[1:]
    daily_only = "--daily-only" in args
    hourly_only = "--hourly-only" in args
    if "--all" in args:
        symbols = INDIA_SYMBOLS
    else:
        symbols = [a for a in args if not a.startswith("--")] or INDIA_SYMBOLS[:5]

    print(f"Fetching NSE multi-timeframe data for {len(symbols)} symbols...\n")
    ok, fail = [], []
    for sym in symbols:
        try:
            if not hourly_only:
                p = fetch_daily_bars(sym)
                df = load_bars_df(p)
                print(f"  OK   {sym:<12} daily   {len(df):>5} bars  ({df.index.min().date()} -> {df.index.max().date()})")
            if not daily_only:
                p = fetch_hourly_bars(sym)
                df = load_bars_df(p)
                print(f"  OK   {sym:<12} hourly  {len(df):>5} bars  ({df.index.min().date()} -> {df.index.max().date()})")
            ok.append(sym)
        except Exception as e:
            print(f"  ERR  {sym:<12} {e}")
            fail.append(sym)

    print(f"\n  {len(ok)} succeeded  |  {len(fail)} failed")
    if fail:
        print(f"  Failed: {', '.join(fail)}")
