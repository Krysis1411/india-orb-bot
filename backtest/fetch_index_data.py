"""
Fetch NIFTY / BANKNIFTY spot-index bars for the index options zone backtest.

Same multi-timeframe split as the equity pipeline (fetch_nse_data_smartapi.py
+ fetch_nse_multi_tf.py), applied to the index itself rather than a stock:
  - 5-min bars: AngelOne SmartAPI, using the direct INDEX_TOKENS constant
    (brokers/angelone.py) -- no searchScrip needed, and SmartAPI's practical
    lookback for 5-min bars is on the order of ~100 days, so this only ever
    covers a few months.
  - 1h bars: yfinance ("^NSEI" / "^NSEBANK"), same as equities' hourly
    confluence timeframe -- ~2 years of history, far beyond SmartAPI's cap.

Output uses the exact same parquet naming as the equity backtest
(f"{name}_NSE_5m.parquet", f"{name}_NSE_1h.parquet") so
backtest.zone_backtest.run_symbol("NIFTY") / ("BANKNIFTY") work unmodified --
zone_detector.py and entry_confirmation.py have no equity-specific
assumptions, they just consume OHLCV bars.

Usage
-----
    python -m backtest.fetch_index_data              # NIFTY + BANKNIFTY, 5m + 1h
    python -m backtest.fetch_index_data --5m-only
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

from brokers.angelone import AngelOneClient, INDEX_TOKENS, INTERVAL_5MIN

IST = ZoneInfo("Asia/Kolkata")
DATA_DIR = Path(__file__).parent / "data"

_NSE_START = "09:15"
_NSE_END = "15:30"
_CHUNK_DAYS = 28
_SLEEP_BETWEEN_CALLS = 1.0

# yfinance ticker for each index -- verified live 2026-07-31 (both return
# real intraday bars), not to be confused with the NFO underlying `name`
# used for option-chain resolution (INDEX_TOKENS / AngelOneClient keys).
_YF_TICKERS = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK"}


def _fetch_chunk(client: AngelOneClient, name: str, token: str, from_dt: datetime, to_dt: datetime, exchange: str = "NSE") -> pd.DataFrame | None:
    fmt = "%Y-%m-%d %H:%M"
    params = {
        "exchange": exchange,
        "symboltoken": token,
        "interval": INTERVAL_5MIN,
        "fromdate": from_dt.strftime(fmt),
        "todate": to_dt.strftime(fmt),
    }
    try:
        resp = client._obj.getCandleData(params)
    except Exception as e:
        print(f"  WARN  {name}: {from_dt.date()}→{to_dt.date()} — {e}")
        return None
    if not resp or not resp.get("status"):
        return None
    candles = resp.get("data") or []
    if not candles:
        return None
    rows = []
    for c in candles:
        try:
            ts = pd.to_datetime(c[0], utc=True)
            rows.append({"timestamp": ts, "open": c[1], "high": c[2], "low": c[3], "close": c[4], "volume": c[5]})
        except (IndexError, ValueError):
            continue
    if not rows:
        return None
    df = pd.DataFrame(rows).set_index("timestamp")
    df.index = pd.DatetimeIndex(df.index, tz="UTC")
    return df


def _fetch_futures_volume(client: AngelOneClient, name: str, months: int) -> pd.Series | None:
    """Real 5m traded volume from the near-month NFO future, keyed by UTC
    timestamp -- substituted into the index bars since the spot index itself
    always reports zero volume (see module docstring / get_near_month_future).
    Only the CURRENTLY listed contract's history is fetchable (expired
    contracts vanish from ScripMaster), so this can be thinner than the
    index's own price history if the near-month contract rolled recently."""
    fut = client.get_near_month_future(name)
    if fut is None:
        print(f"  WARN  {name}: no near-month future resolved -- volume proxy unavailable")
        return None
    now_ist = datetime.now(IST)
    to_ist = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
    if now_ist < to_ist:
        to_ist = now_ist
    from_ist = (to_ist - timedelta(days=months * 31)).replace(hour=9, minute=15, second=0, microsecond=0)

    chunks = []
    cursor = from_ist
    while cursor < to_ist:
        chunk_end = min(cursor + timedelta(days=_CHUNK_DAYS), to_ist)
        df = _fetch_chunk(client, f"{name}-FUT", fut["token"], cursor, chunk_end, exchange="NFO")
        if df is not None and not df.empty:
            chunks.append(df)
        cursor = chunk_end + timedelta(minutes=5)
        time.sleep(_SLEEP_BETWEEN_CALLS)
    if not chunks:
        print(f"  WARN  {name}: no future volume data fetched ({fut['symbol']})")
        return None
    raw = pd.concat(chunks)
    raw = raw[~raw.index.duplicated(keep="last")].sort_index()
    print(f"  {name}: futures volume proxy from {fut['symbol']} — {len(raw)} bars")
    return raw["volume"]


def fetch_index_5m(client: AngelOneClient, name: str, months: int = 3) -> Path:
    token = INDEX_TOKENS[name]
    now_ist = datetime.now(IST)
    to_ist = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
    if now_ist < to_ist:
        to_ist = now_ist
    from_ist = (to_ist - timedelta(days=months * 31)).replace(hour=9, minute=15, second=0, microsecond=0)

    chunks = []
    cursor = from_ist
    while cursor < to_ist:
        chunk_end = min(cursor + timedelta(days=_CHUNK_DAYS), to_ist)
        df = _fetch_chunk(client, name, token, cursor, chunk_end)
        if df is not None and not df.empty:
            chunks.append(df)
            print(f"  {name}: {cursor.date()} → {chunk_end.date()}  {len(df)} bars")
        else:
            print(f"  {name}: {cursor.date()} → {chunk_end.date()}  (no data -- likely past SmartAPI's lookback cap)")
        cursor = chunk_end + timedelta(minutes=5)
        time.sleep(_SLEEP_BETWEEN_CALLS)

    if not chunks:
        raise ValueError(f"No 5m data returned for {name}")

    raw = pd.concat(chunks)
    raw = raw[~raw.index.duplicated(keep="last")].sort_index()
    ist_df = raw.copy()
    ist_df.index = ist_df.index.tz_convert(IST)
    ist_df = ist_df.between_time(_NSE_START, _NSE_END)
    ist_df.index = ist_df.index.tz_convert("UTC")

    # Substitute real futures volume for the index's always-zero volume --
    # zone_detector.py's legin/breakout volume-ratio filters need genuine
    # traded volume to mean anything. Rows outside the future's own history
    # keep volume=0 and simply won't pass those filters (same as before),
    # rather than fabricating a number.
    fut_vol = _fetch_futures_volume(client, name, months)
    if fut_vol is not None:
        ist_df["volume"] = fut_vol.reindex(ist_df.index).fillna(0).astype(int)

    path = DATA_DIR / f"{name}_NSE_5m.parquet"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = pd.read_parquet(path)
        if existing.index.tz is None:
            existing.index = existing.index.tz_localize("UTC")
        # Each run only gets a partial, rate-limit-dependent slice of futures
        # volume -- a run with worse luck must NOT blank out volume a
        # previous run already captured. For overlapping timestamps, keep
        # whichever of (existing, new) volume is non-zero; prefer the new
        # value when both are non-zero (freshest fetch).
        overlap_idx = existing.index.intersection(ist_df.index)
        for ts in overlap_idx:
            if ist_df.at[ts, "volume"] == 0 and existing.at[ts, "volume"] != 0:
                ist_df.loc[ts, "volume"] = existing.at[ts, "volume"]
        ist_df = pd.concat([existing, ist_df])
        ist_df = ist_df[~ist_df.index.duplicated(keep="last")].sort_index()
    ist_df.to_parquet(path)
    return path


def fetch_index_1h(name: str) -> Path:
    yf_sym = _YF_TICKERS[name]
    raw = yf.download(yf_sym, period="730d", interval="1h", progress=False, auto_adjust=True)
    if raw.empty:
        raise ValueError(f"yfinance returned no hourly data for {yf_sym}")
    if isinstance(raw.columns, pd.MultiIndex):
        raw = raw.droplevel(1, axis=1)
    raw.columns = [c.lower() for c in raw.columns]
    raw = raw[["open", "high", "low", "close", "volume"]].copy()
    raw.index.name = "timestamp"
    if raw.index.tz is None:
        raw.index = raw.index.tz_localize("UTC")
    else:
        raw.index = raw.index.tz_convert("UTC")
    ist_df = raw.copy()
    ist_df.index = ist_df.index.tz_convert(IST)
    ist_df = ist_df.between_time(_NSE_START, _NSE_END)
    ist_df.index = ist_df.index.tz_convert("UTC")

    path = DATA_DIR / f"{name}_NSE_1h.parquet"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = pd.read_parquet(path)
        if existing.index.tz is None:
            existing.index = existing.index.tz_localize("UTC")
        ist_df = pd.concat([existing, ist_df])
        ist_df = ist_df[~ist_df.index.duplicated(keep="last")].sort_index()
    ist_df.to_parquet(path)
    return path


if __name__ == "__main__":
    only_5m = "--5m-only" in sys.argv
    only_1h = "--1h-only" in sys.argv

    if not only_1h:
        client = AngelOneClient()
        if not client.connect():
            print("AngelOne auth failed")
            sys.exit(1)
        for name in ("NIFTY", "BANKNIFTY"):
            try:
                path = fetch_index_5m(client, name)
                df = pd.read_parquet(path)
                days = pd.DatetimeIndex(df.index).tz_convert(IST).normalize().nunique()
                print(f"  OK   {name:<10} {len(df):>5} 5m bars  {days} trading days  → {path.name}\n")
            except Exception as e:
                print(f"  ERR  {name}: {e}\n")

    if not only_5m:
        for name in ("NIFTY", "BANKNIFTY"):
            try:
                path = fetch_index_1h(name)
                df = pd.read_parquet(path)
                print(f"  OK   {name:<10} {len(df):>5} 1h bars  → {path.name}\n")
            except Exception as e:
                print(f"  ERR  {name}: {e}\n")
