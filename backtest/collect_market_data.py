"""
Collect every piece of NIFTY / BANKNIFTY market data AngelOne will serve, and
keep it growing with a daily after-close run.

Why this exists: option contracts vanish from AngelOne's ScripMaster the day
they expire, and their candle history goes with them (see
index_options_backtest.py's docstring -- it prices options with Black-Scholes
for exactly this reason). The only way to ever backtest against REAL option
prices is to record each contract's history while it is still listed. Every
day this doesn't run is option data lost for good.

What it stores, all under backtest/data/market/ (gitignored):
  index/{NAME}_{iv}.parquet            spot index + INDIAVIX; 1m, 5m, 15m, 1h, 1d
  futures/{SYMBOL}_{iv}.parquet        every listed index future; 1m, 5m, 1d, 5m open interest
  options/{UNDERLYING}/{EXPIRY}/{STRIKE}{CE|PE}_{iv}.parquet
                                       strikes within +/-STRIKE_RANGE_PCT of spot,
                                       every listed expiry; 5m, 5m open interest
                                       (`_oi_5m`), 1m

Measured 2026-10-05 against the live API: index history reaches back much
further than fetch_index_data.py's "~100 days" comment assumed -- 5m to at
least 2024-04, 15m to 2022-06, 1h to 2018, 1d to 2002. Those were the probe's
own stopping points, not the API's; this script walks back until the API
returns nothing.

Every file is incremental: a run fetches only what is newer than the last
stored bar, so the first run is the long one (hours) and daily runs are short.
Work is ordered soonest-expiry first, and --deadline makes a run stop cleanly
(e.g. before the live bot starts and needs the rate limit).

This deliberately does NOT write to backtest/data/{NAME}_NSE_5m.parquet -- the
live bot's warm-start cache keeps its own, separately validated, pipeline.

Usage
-----
    python -m backtest.collect_market_data                    # everything
    python -m backtest.collect_market_data --deadline 08:40   # stop by 08:40 IST
    python -m backtest.collect_market_data --only index futures
    python -m backtest.collect_market_data --summary          # what's on disk
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

from brokers.angelone import AngelOneClient, INDEX_TOKENS

IST = ZoneInfo("Asia/Kolkata")
MARKET_DIR = Path(__file__).parent / "data" / "market"

# AngelOne's documented max span per getCandleData request, minus a margin.
_INTERVALS = {
    "1m":  ("ONE_MINUTE", 28, timedelta(minutes=1)),
    "5m":  ("FIVE_MINUTE", 90, timedelta(minutes=5)),
    "15m": ("FIFTEEN_MINUTE", 180, timedelta(minutes=15)),
    "1h":  ("ONE_HOUR", 365, timedelta(hours=1)),
    "1d":  ("ONE_DAY", 1800, timedelta(days=1)),
}
# Documented limit is 180 req/min and 5000 req/hour; the hourly cap is the
# binding one for a multi-hour backfill. 0.8s/call = 4500/hour.
_MIN_CALL_SPACING_S = 0.8
_MAX_RETRIES = 3

INDEX_SERIES = {**INDEX_TOKENS, "INDIAVIX": "99926017"}
UNDERLYINGS = ("NIFTY", "BANKNIFTY")
STRIKE_RANGE_PCT = 0.10

log = logging.getLogger("collect_market_data")


class DeadlineReached(Exception):
    pass


class Collector:
    def __init__(self, client: AngelOneClient, deadline: datetime | None):
        self.client = client
        self.deadline = deadline
        self._last_call = 0.0
        self.calls = 0
        self.failed = 0
        self.files_updated = 0
        self.rows_added = 0

    # ------------------------------------------------------------------
    # One API call, paced and retried
    # ------------------------------------------------------------------

    def _request(self, kind: str, exchange: str, token: str, interval: str, start: datetime, end: datetime) -> list | None:
        """Returns the raw row list ([] = genuinely no data), or None if every
        attempt failed -- callers must not treat None as 'no data'."""
        if self.deadline and datetime.now(IST) >= self.deadline:
            raise DeadlineReached
        params = {
            "exchange": exchange,
            "symboltoken": token,
            "interval": interval,
            "fromdate": start.strftime("%Y-%m-%d %H:%M"),
            "todate": end.strftime("%Y-%m-%d %H:%M"),
        }
        fn = self.client._obj.getOIData if kind == "oi" else self.client._obj.getCandleData
        for attempt in range(_MAX_RETRIES):
            wait = _MIN_CALL_SPACING_S - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            self.calls += 1
            try:
                resp = fn(dict(params))
            except Exception:
                time.sleep(5.0 * (attempt + 1))
                continue
            if resp and resp.get("status"):
                return resp.get("data") or []
            # getOIData answers a window with no data at all (e.g. entirely
            # before the contract was listed) with AB1012 "Invalid Bad Request"
            # rather than an empty list -- that is the end of its history, not
            # a failure worth three retries per contract.
            if resp and (not resp.get("errorcode") or (kind == "oi" and resp.get("errorcode") == "AB1012")):
                return []
            time.sleep(5.0 * (attempt + 1))
        self.failed += 1
        return None

    @staticmethod
    def _to_frame(kind: str, rows: list) -> pd.DataFrame:
        if kind == "oi":
            df = pd.DataFrame([{"timestamp": r["time"], "oi": r["oi"]} for r in rows])
        else:
            df = pd.DataFrame([r[:6] for r in rows], columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        return df.set_index("timestamp")

    # ------------------------------------------------------------------
    # One series -> one parquet, incremental
    # ------------------------------------------------------------------

    def update(self, path: Path, exchange: str, token: str, iv: str, kind: str = "candle") -> None:
        interval, chunk_days, bar = _INTERVALS[iv]
        existing = pd.read_parquet(path) if path.exists() else None
        floor = existing.index[-1].tz_convert(IST).to_pydatetime() if existing is not None and len(existing) else None

        # Walk backward from now until we reach what's already stored, or the
        # API has nothing older.
        now = datetime.now(IST)
        end = now
        chunks: list[pd.DataFrame] = []
        while True:
            start = end - timedelta(days=chunk_days)
            reached_floor = floor is not None and start <= floor
            if reached_floor:
                start = floor
            rows = self._request(kind, exchange, token, interval, start, end)
            if rows is None:
                # Don't write a partial backfill: the next run resumes from the
                # last stored bar, so a gap written now would never be filled.
                log.warning(f"FAILED {path.relative_to(MARKET_DIR)} ({start.date()} -> {end.date()}) -- left for the next run")
                return
            if rows:
                chunks.append(self._to_frame(kind, rows))
            if not rows or reached_floor:
                break
            end = start

        if not chunks:
            return
        fresh = pd.concat(chunks)
        # Never store a bar that is still forming.
        if iv == "1d":
            # Today's daily bar is only final after the close.
            if now.hour < 16:
                fresh = fresh[fresh.index.tz_convert(IST).date < now.date()]
        else:
            fresh = fresh[fresh.index + bar <= pd.Timestamp.now(tz="UTC")]
        merged = fresh if existing is None else pd.concat([existing, fresh])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        added = len(merged) - (0 if existing is None else len(existing))
        if added <= 0:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        merged.to_parquet(tmp)
        tmp.replace(path)
        self.files_updated += 1
        self.rows_added += added

    # ------------------------------------------------------------------
    # What to collect
    # ------------------------------------------------------------------

    def collect_index(self) -> None:
        for name, token in INDEX_SERIES.items():
            for iv in ("1d", "1h", "15m", "5m", "1m"):
                self.update(MARKET_DIR / "index" / f"{name}_{iv}.parquet", "NSE", token, iv)
                log.info(f"index {name} {iv} done ({self.calls} calls so far)")

    def collect_futures(self) -> None:
        for u in UNDERLYINGS:
            for fut in self.client._futures_chain.get(u, []):
                for iv in ("1d", "5m", "1m"):
                    self.update(MARKET_DIR / "futures" / f"{fut['symbol']}_{iv}.parquet", "NFO", fut["token"], iv)
                self.update(MARKET_DIR / "futures" / f"{fut['symbol']}_oi_5m.parquet", "NFO", fut["token"], "5m", kind="oi")
            log.info(f"futures {u} done ({self.calls} calls so far)")

    def _option_contracts(self) -> list[tuple[str, dict]]:
        """In-range contracts for both underlyings, soonest expiry first --
        the nearest expiry is the data closest to being lost."""
        out: list[tuple[str, dict]] = []
        today = datetime.now(IST).date()
        for u in UNDERLYINGS:
            spot = self.client.get_ltp(u, INDEX_TOKENS[u])
            if not spot:
                log.warning(f"{u}: no spot price -- skipping its options this run")
                continue
            lo, hi = spot * (1 - STRIKE_RANGE_PCT), spot * (1 + STRIKE_RANGE_PCT)
            rows = [r for r in self.client._option_chain.get(u, []) if lo <= r["strike"] <= hi and r["expiry"] >= today]
            log.info(f"{u}: spot {spot:.0f}, {len(rows)} option contracts within +/-{STRIKE_RANGE_PCT:.0%} across {len({r['expiry'] for r in rows})} expiries")
            out.extend((u, r) for r in rows)
        out.sort(key=lambda x: (x[1]["expiry"], x[0], x[1]["strike"], x[1]["opt_type"]))
        return out

    def collect_options(self) -> None:
        contracts = self._option_contracts()
        # Three passes rather than per-contract, so a deadline-cut run has
        # complete 5m coverage before it spends anything on OI or 1m.
        for iv, kind, suffix in (("5m", "candle", "5m"), ("5m", "oi", "oi_5m"), ("1m", "candle", "1m")):
            for n, (u, r) in enumerate(contracts, 1):
                strike = f"{r['strike']:g}"
                path = MARKET_DIR / "options" / u / r["expiry"].isoformat() / f"{strike}{r['opt_type']}_{suffix}.parquet"
                self.update(path, "NFO", r["token"], iv, kind)
                if n % 200 == 0:
                    log.info(f"options {suffix}: {n}/{len(contracts)} contracts ({self.calls} calls, {self.failed} failed)")
            log.info(f"options {suffix} pass complete")


def summary() -> None:
    if not MARKET_DIR.exists():
        print("nothing collected yet")
        return
    for sub in ("index", "futures"):
        for p in sorted((MARKET_DIR / sub).glob("*.parquet")):
            df = pd.read_parquet(p)
            print(f"{sub}/{p.name:<34} {len(df):>8} rows  {df.index[0].date()} -> {df.index[-1].date()}")
    for u_dir in sorted((MARKET_DIR / "options").glob("*")):
        for e_dir in sorted(u_dir.glob("*")):
            files = list(e_dir.glob("*.parquet"))
            by_kind = {k: sum(1 for f in files if f.stem.endswith("_" + k) and (k != "5m" or "_oi_" not in f.stem)) for k in ("5m", "oi_5m", "1m")}
            print(f"options/{u_dir.name}/{e_dir.name}  contracts: 5m={by_kind['5m']} oi={by_kind['oi_5m']} 1m={by_kind['1m']}")
    size_mb = sum(f.stat().st_size for f in MARKET_DIR.rglob("*.parquet")) / 1e6
    print(f"total on disk: {size_mb:.0f} MB")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Collect NIFTY/BANKNIFTY index, futures and option history from AngelOne")
    parser.add_argument("--only", nargs="+", choices=["index", "futures", "options"], default=["index", "futures", "options"])
    parser.add_argument("--deadline", metavar="HH:MM", help="stop cleanly at this IST time today")
    parser.add_argument("--summary", action="store_true", help="print what is on disk and exit")
    args = parser.parse_args()

    if args.summary:
        summary()
        raise SystemExit(0)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    deadline = None
    if args.deadline:
        hh, mm = map(int, args.deadline.split(":"))
        deadline = datetime.now(IST).replace(hour=hh, minute=mm, second=0, microsecond=0)

    client = AngelOneClient()
    if not client.connect():
        log.error("AngelOne authentication failed")
        raise SystemExit(1)
    client._ensure_connected()
    # SmartApi logs every failed/empty request at ERROR; an illiquid strike
    # with no candles is routine here, not an error worth a journal line each.
    import logzero
    logzero.loglevel(logging.CRITICAL)

    collector = Collector(client, deadline)
    started = time.monotonic()
    try:
        if "index" in args.only:
            collector.collect_index()
        if "futures" in args.only:
            collector.collect_futures()
        if "options" in args.only:
            collector.collect_options()
        log.info("Run complete")
    except DeadlineReached:
        log.info(f"Deadline {args.deadline} IST reached -- stopping; the next run resumes where this left off")
    finally:
        log.info(
            f"{collector.calls} API calls, {collector.failed} failed series, {collector.files_updated} files updated, "
            f"{collector.rows_added} rows added in {(time.monotonic() - started) / 60:.1f} min"
        )
