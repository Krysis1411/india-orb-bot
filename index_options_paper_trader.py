"""
NIFTY / BANKNIFTY index options paper trader — AngelOne SmartAPI, no real orders.

Sibling to zone_paper_trader.py, same rationale (real decision logic against
live data, logged instead of ordered), same signal pipeline (strategies/
zone_detector.py + entry_confirmation.py, unchanged). Two differences:

  1. The index itself always reports zero traded volume (it's a computed
     value, not a traded instrument) -- so the same NIFTY/BANKNIFTY futures
     contract's real 5m volume is fetched each cycle and substituted in,
     same technique as backtest/fetch_index_data.py.
  2. A zone signal on the index doesn't get traded directly -- it's
     translated into a real option contract (nearest weekly for NIFTY,
     nearest monthly for BANKNIFTY -- BANKNIFTY weekly was discontinued by
     the Oct 2024 SEBI single-weekly-expiry rule -- ATM strike from live
     spot), and the position's entry/exit price is that option's REAL live
     LTP, not a Black-Scholes estimate (unlike backtest/index_options_
     backtest.py, which has no choice but to simulate since expired option
     contracts vanish from AngelOne's instrument list -- live paper trading
     has no such gap, so it should always use the real quote).

The underlying's zone (stop/target/trailing) still drives WHEN to exit --
same causal logic as the equity bot -- only the P&L actually recorded is the
option's.

Usage
-----
    python index_options_paper_trader.py              # loop every 5 min during market hours
    python index_options_paper_trader.py --once        # single cycle and exit (testing)
"""
from __future__ import annotations

import json
import logging
import time as _time
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from dotenv import load_dotenv

from backtest.index_options_backtest import STRIKE_INTERVAL, _pick_expiry
from brokers.angelone import INTERVAL_1HOUR, INTERVAL_5MIN, INDEX_TOKENS, AngelOneClient
from strategies.entry_confirmation import check_entry
from strategies.zone_detector import Zone, atr_series, find_zones

load_dotenv()

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN_IST = time(9, 15)
MARKET_CLOSE_IST = time(15, 30)

REPO_ROOT = Path(__file__).parent
DATA_DIR = REPO_ROOT / "backtest" / "data"
LOG_DIR = REPO_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
TRADES_CSV = LOG_DIR / "index_options_paper_trades.csv"
STATE_FILE = LOG_DIR / "index_options_paper_state.json"


# Python's logging module stamps %(asctime)s using the OS's LOCAL timezone,
# not IST -- see zone_paper_trader.py's identical fix/comment. Duplicated
# here (rather than imported) because zone_paper_trader.py's module-level
# code sets up ITS OWN root-logger handlers and log file as an import side
# effect -- importing it would attach a second file handler writing into
# THAT script's log file from this separate process.
class ISTFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, IST)
        return dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S")


def _merge_new_bars(existing: pd.DataFrame, fresh: pd.DataFrame) -> pd.DataFrame:
    """Append genuinely new rows from `fresh` onto `existing`, deduped by timestamp."""
    if fresh is None or fresh.empty:
        return existing
    if existing.empty:
        return fresh
    fresh = fresh[~fresh.index.isin(existing.index)]
    if fresh.empty:
        return existing
    return pd.concat([existing, fresh]).sort_index()


def _warm_start_bars(name: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load cached historical parquet (backtest/fetch_index_data.py's output)
    as a warm start so ATR/RSI/leg-in windows are valid from the first live
    cycle instead of empty."""
    from backtest.fetch_nse_data import load_nse_bars_df
    from backtest.fetch_nse_multi_tf import load_bars_df

    m_path = DATA_DIR / f"{name}_NSE_5m.parquet"
    h_path = DATA_DIR / f"{name}_NSE_1h.parquet"
    bars_5m = load_nse_bars_df(m_path) if m_path.exists() else pd.DataFrame()
    bars_1h = load_bars_df(h_path) if h_path.exists() else pd.DataFrame()
    return bars_5m, bars_1h

_LOG_FORMAT = "%(asctime)s IST | %(levelname)s | %(message)s"
logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)
logging.getLogger().handlers[0].setFormatter(ISTFormatter(_LOG_FORMAT))
_today_label = datetime.now(IST).strftime("%Y%m%d")
_file_handler = logging.FileHandler(LOG_DIR / f"index_options_paper_{_today_label}.log", encoding="utf-8")
_file_handler.setFormatter(ISTFormatter(_LOG_FORMAT))
logging.getLogger().addHandler(_file_handler)
log = logging.getLogger(__name__)

# Strategy constants -- copied verbatim from backtest/zone_backtest.py /
# zone_paper_trader.py, same "don't tune here" rule.
MAX_TOUCHES = 3
INVALIDATION_BUFFER_PCT = 0.002
WARMUP_WINDOW = 300
BREAKEVEN_TRIGGER_R = 1.0
TRAIL_PROFIT_PCT = 0.5
MIN_RISK_ATR_MULT = 1.0
STOP_BUFFER_PCT = 0.002
TARGET_RR = 2.0
MAX_HOLD_BARS = 1500

UNDERLYINGS = ["NIFTY", "BANKNIFTY"]


@dataclass
class OpenPosition:
    underlying: str
    direction: str            # "long" | "short" -- of the UNDERLYING zone signal
    pattern: str
    zone_key: str
    zone_low: float
    zone_high: float
    entry_ts: str
    entry_price: float        # underlying price at entry -- drives stop/target/trailing
    stop: float
    cur_stop: float
    target: float
    peak: float
    risk: float
    touch_number: int
    option_type: str          # "CE" | "PE"
    option_symbol: str
    option_token: str
    strike: float
    expiry: str                # ISO date
    entry_option_price: float  # REAL live LTP at entry


class UnderlyingState:
    def __init__(self, name: str, index_token: str, futures_token: str, futures_symbol: str):
        self.name = name
        self.index_token = index_token
        self.futures_token = futures_token
        self.futures_symbol = futures_symbol
        self.bars_5m = pd.DataFrame()
        self.bars_1h = pd.DataFrame()
        self.zones_5m: list[Zone] = []
        self.last_checked_bar_ts: pd.Timestamp | None = None
        self.last_1h_fetch: float = _time.monotonic()
        self.zone_has_open_position: dict[str, bool] = {}
        self.zone_touch_state: dict[str, tuple[int, bool]] = {}


class IndexOptionsPaperTrader:
    def __init__(self, client: AngelOneClient):
        self.client = client
        self.states: dict[str, UnderlyingState] = {}
        self.open_positions: list[OpenPosition] = []

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def add_underlying(self, name: str) -> bool:
        fut = self.client.get_near_month_future(name)
        if fut is None:
            log.error(f"{name}: no near-month future resolved -- skipping (no volume proxy available)")
            return False
        state = UnderlyingState(name, INDEX_TOKENS[name], fut["token"], fut["symbol"])
        state.bars_5m, state.bars_1h = _warm_start_bars(name)
        if len(state.bars_5m) >= 50:
            state.zones_5m = [z for z in find_zones(state.bars_5m, timeframe="5m") if z.is_reversal]
        self.states[name] = state
        log.info(f"{name}: warm-started {len(state.bars_5m)} 5m bars, {len(state.zones_5m)} live reversal zone(s) — volume proxy: {fut['symbol']}")
        return True

    def load_state(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            raw = json.loads(STATE_FILE.read_text())
        except Exception as e:
            log.warning(f"Could not load state file ({e}) -- starting fresh")
            return
        self.open_positions = [OpenPosition(**p) for p in raw.get("open_positions", [])]
        for name, touch_state in raw.get("zone_touch_state", {}).items():
            if name in self.states:
                self.states[name].zone_touch_state = {k: tuple(v) for k, v in touch_state.items()}
        for name in raw.get("zone_has_open_position", {}):
            if name in self.states:
                self.states[name].zone_has_open_position = raw["zone_has_open_position"][name]
        log.info(f"State loaded — {len(self.open_positions)} open position(s) restored")

    def save_state(self) -> None:
        payload = {
            "open_positions": [asdict(p) for p in self.open_positions],
            "zone_touch_state": {
                name: {k: list(v) for k, v in st.zone_touch_state.items()}
                for name, st in self.states.items() if st.zone_touch_state
            },
            "zone_has_open_position": {
                name: st.zone_has_open_position
                for name, st in self.states.items() if st.zone_has_open_position
            },
        }
        STATE_FILE.write_text(json.dumps(payload, indent=2))

    # ------------------------------------------------------------------
    # Per-cycle bar refresh
    # ------------------------------------------------------------------

    def _update_bars(self, state: UnderlyingState) -> bool:
        fresh_5m = self.client.get_today_candles(state.name, state.index_token, exchange="NSE", interval=INTERVAL_5MIN)
        _time.sleep(1.0)
        fresh_fut = self.client.get_today_candles(f"{state.name}-FUT", state.futures_token, exchange="NFO", interval=INTERVAL_5MIN)
        if fresh_5m is not None and fresh_fut is not None:
            fresh_5m = fresh_5m.copy()
            fresh_5m["volume"] = fresh_fut["volume"].reindex(fresh_5m.index).fillna(0).astype(int)
        state.bars_5m = _merge_new_bars(state.bars_5m, fresh_5m)

        now = _time.monotonic()
        if now - state.last_1h_fetch >= 3300:
            _time.sleep(1.0)
            fresh_1h = self.client.get_today_candles(state.name, state.index_token, exchange="NSE", interval=INTERVAL_1HOUR)
            state.bars_1h = _merge_new_bars(state.bars_1h, fresh_1h)
            state.last_1h_fetch = now

        if state.bars_5m.empty:
            return False
        latest_ts = state.bars_5m.index[-1]
        is_new = latest_ts != state.last_checked_bar_ts
        return is_new

    def _refresh_zones(self, state: UnderlyingState) -> None:
        if len(state.bars_5m) < 50:
            return
        TAIL_SCAN_BARS = 500
        tail = state.bars_5m.iloc[-min(len(state.bars_5m), TAIL_SCAN_BARS):]
        known_keys = {z.base_start for z in state.zones_5m}
        new_zones = [
            z for z in find_zones(tail, timeframe="5m")
            if z.is_reversal and z.base_start not in known_keys
        ]
        state.zones_5m.extend(new_zones)
        for z in state.zones_5m:
            key = z.base_start.isoformat()
            if key in state.zone_touch_state:
                z.touches, z.broken = state.zone_touch_state[key]

    # ------------------------------------------------------------------
    # Entries
    # ------------------------------------------------------------------

    def _check_new_entries(self, state: UnderlyingState) -> None:
        five = state.bars_5m
        five_atr = atr_series(five, 14)
        n = len(five)

        for zone in state.zones_5m:
            if zone.touches >= MAX_TOUCHES or zone.broken:
                continue
            key = zone.base_start.isoformat()
            if state.zone_has_open_position.get(key):
                continue

            close = float(five.iloc[-1]["close"])
            if zone.is_invalidated_by(close, INVALIDATION_BUFFER_PCT):
                zone.broken = True
                state.zone_touch_state[key] = (zone.touches, zone.broken)
                continue

            sub = five.iloc[max(0, n - WARMUP_WINDOW):n]
            sig = check_entry(sub, zone)
            if sig is None:
                continue

            is_long = zone.kind == "demand"
            entry_price = sig.price
            if is_long:
                stop = zone.price_low * (1 - STOP_BUFFER_PCT)
                risk = entry_price - stop
            else:
                stop = zone.price_high * (1 + STOP_BUFFER_PCT)
                risk = stop - entry_price

            atr_here = five_atr.iloc[-1]
            min_risk = MIN_RISK_ATR_MULT * atr_here if pd.notna(atr_here) and atr_here > 0 else 0.0
            if risk <= 0 or risk < min_risk:
                continue

            target = entry_price + risk * TARGET_RR if is_long else entry_price - risk * TARGET_RR

            # Translate to a real option contract -- long zone -> buy CE,
            # short zone -> buy PE. Only commit the touch/zone-lock if this
            # actually resolves to a live, quotable contract.
            option_type = "CE" if is_long else "PE"
            entry_date = five.index[-1].tz_convert(IST).date()
            expiry = _pick_expiry(state.name, entry_date)
            strike = round(entry_price / STRIKE_INTERVAL[state.name]) * STRIKE_INTERVAL[state.name]
            contract = self.client.resolve_option(state.name, expiry, strike, option_type)
            if contract is None:
                log.warning(f"{state.name}: no listed {option_type} at strike {strike} exp {expiry} -- signal skipped")
                continue
            opt_ltp = self.client.get_ltp(contract["symbol"], contract["token"], exchange="NFO")
            if opt_ltp is None or opt_ltp <= 0:
                log.warning(f"{state.name}: LTP fetch failed for {contract['symbol']} -- signal skipped")
                continue

            zone.touches += 1
            state.zone_touch_state[key] = (zone.touches, zone.broken)
            state.zone_has_open_position[key] = True

            pos = OpenPosition(
                underlying=state.name, direction="long" if is_long else "short",
                pattern=zone.pattern, zone_key=key,
                zone_low=zone.price_low, zone_high=zone.price_high,
                entry_ts=five.index[-1].isoformat(), entry_price=round(entry_price, 2),
                stop=round(stop, 2), cur_stop=round(stop, 2), target=round(target, 2),
                peak=round(entry_price, 2), risk=round(risk, 2), touch_number=zone.touches,
                option_type=option_type, option_symbol=contract["symbol"], option_token=contract["token"],
                strike=strike, expiry=expiry.isoformat(), entry_option_price=round(opt_ltp, 2),
            )
            self.open_positions.append(pos)
            log.info(
                f"PAPER ENTRY — {state.name} {pos.direction.upper()} {pos.pattern} zone "
                f"| BUY {contract['symbol']} @ {opt_ltp:.2f} "
                f"| underlying stop {stop:.2f} target {target:.2f} | touch #{zone.touches}"
            )

    # ------------------------------------------------------------------
    # Exits -- underlying stop/target/trailing decides WHEN; the option's
    # real live LTP at that moment is what gets recorded as P&L.
    # ------------------------------------------------------------------

    def _check_exits(self, state: UnderlyingState) -> None:
        if state.bars_5m.empty:
            return
        bar = state.bars_5m.iloc[-1]
        if bar["volume"] <= 0:
            return

        still_open: list[OpenPosition] = []
        for pos in self.open_positions:
            if pos.underlying != state.name:
                still_open.append(pos)
                continue

            is_long = pos.direction == "long"
            exit_reason = None
            if is_long:
                if bar["low"] <= pos.cur_stop:
                    exit_reason = "trailing_stop" if pos.cur_stop > pos.stop else "stop_loss"
                elif bar["high"] >= pos.target:
                    exit_reason = "take_profit"
            else:
                if bar["high"] >= pos.cur_stop:
                    exit_reason = "trailing_stop" if pos.cur_stop < pos.stop else "stop_loss"
                elif bar["low"] <= pos.target:
                    exit_reason = "take_profit"

            if exit_reason is None:
                entry_pos_idx = state.bars_5m.index.searchsorted(pd.Timestamp(pos.entry_ts), side="right")
                bars_held = len(state.bars_5m) - entry_pos_idx
                if bars_held >= MAX_HOLD_BARS:
                    exit_reason = "max_hold"
                elif date.today() >= date.fromisoformat(pos.expiry):
                    # Contract expires today/has expired -- can't hold past
                    # its own expiry, force-close at whatever it's worth now.
                    exit_reason = "contract_expiry"

            if exit_reason is not None:
                self._close_position(pos, exit_reason, state.bars_5m.index[-1])
                continue

            if is_long:
                pos.peak = max(pos.peak, float(bar["high"]))
                profit_r = (pos.peak - pos.entry_price) / pos.risk
                if profit_r >= BREAKEVEN_TRIGGER_R:
                    trail = pos.entry_price + TRAIL_PROFIT_PCT * (pos.peak - pos.entry_price)
                    pos.cur_stop = max(pos.cur_stop, pos.entry_price, trail)
            else:
                pos.peak = min(pos.peak, float(bar["low"]))
                profit_r = (pos.entry_price - pos.peak) / pos.risk
                if profit_r >= BREAKEVEN_TRIGGER_R:
                    trail = pos.entry_price - TRAIL_PROFIT_PCT * (pos.entry_price - pos.peak)
                    pos.cur_stop = min(pos.cur_stop, pos.entry_price, trail)
            still_open.append(pos)

        self.open_positions = still_open

    def _close_position(self, pos: OpenPosition, exit_reason: str, exit_ts: pd.Timestamp) -> None:
        opt_ltp = self.client.get_ltp(pos.option_symbol, pos.option_token, exchange="NFO")
        if opt_ltp is None:
            # Real quote unavailable (e.g. contract already expired/delisted) --
            # settle at intrinsic value against the current underlying price
            # rather than dropping the trade from the log.
            log.warning(f"{pos.option_symbol}: LTP unavailable at exit -- settling at intrinsic value")
            spot = float(self.states[pos.underlying].bars_5m.iloc[-1]["close"])
            opt_ltp = max(spot - pos.strike, 0.0) if pos.option_type == "CE" else max(pos.strike - spot, 0.0)

        pnl_pct = (opt_ltp - pos.entry_option_price) / pos.entry_option_price * 100
        log.info(
            f"PAPER EXIT  — {pos.underlying} {pos.option_symbol} @ {opt_ltp:.2f} "
            f"({exit_reason}) | pnl {pnl_pct:+.2f}%"
        )
        row = {
            "underlying": pos.underlying, "direction": pos.direction, "pattern": pos.pattern,
            "option_symbol": pos.option_symbol, "option_type": pos.option_type,
            "strike": pos.strike, "expiry": pos.expiry, "touch_number": pos.touch_number,
            "entry_ts": pos.entry_ts, "entry_underlying_price": pos.entry_price,
            "entry_option_price": pos.entry_option_price,
            "exit_ts": exit_ts.isoformat(), "exit_option_price": round(opt_ltp, 2),
            "exit_reason": exit_reason, "pnl_pct": round(pnl_pct, 2),
        }
        _append_trade_row(row)
        state = self.states.get(pos.underlying)
        if state is not None:
            state.zone_has_open_position[pos.zone_key] = False

    # ------------------------------------------------------------------
    # Main cycle
    # ------------------------------------------------------------------

    def run_cycle(self) -> None:
        for state in self.states.values():
            try:
                has_new_bar = self._update_bars(state)
            except Exception as e:
                log.error(f"{state.name}: bar update failed — {e}")
                continue
            finally:
                _time.sleep(1.0)
            if not has_new_bar:
                continue
            state.last_checked_bar_ts = state.bars_5m.index[-1]
            self._refresh_zones(state)
            self._check_new_entries(state)
            self._check_exits(state)
        self.save_state()
        positions_desc = ", ".join(f"{p.underlying} {p.option_symbol}" for p in self.open_positions) or "none"
        log.info(f"Cycle complete — {len(self.open_positions)} open position(s): {positions_desc}")


def _append_trade_row(row: dict) -> None:
    df = pd.DataFrame([row])
    write_header = not TRADES_CSV.exists()
    df.to_csv(TRADES_CSV, mode="a", header=write_header, index=False)


def _now_ist() -> datetime:
    return datetime.now(IST)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="NIFTY/BANKNIFTY index options paper trader — AngelOne SmartAPI, no real orders")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit (testing)")
    parser.add_argument("--interval", type=int, default=900, metavar="SECONDS", help="Loop interval (default 900 = 15 min -- 5 min was triggering AngelOne rate-limit errors most cycles)")
    args = parser.parse_args()

    log.info("*** INDEX OPTIONS PAPER TRADING — no real orders will ever be placed by this script ***")

    client = AngelOneClient()
    if not client.connect():
        log.error("AngelOne authentication failed — check credentials in .env")
        raise SystemExit(1)

    trader = IndexOptionsPaperTrader(client)
    ready = 0
    for name in UNDERLYINGS:
        if trader.add_underlying(name):
            ready += 1
    log.info(f"Watchlist ready — {ready}/{len(UNDERLYINGS)} underlyings")
    trader.load_state()

    if args.once:
        trader.run_cycle()
        log.info("Single-run complete")
        raise SystemExit(0)

    log.info(f"Loop mode — checking every {args.interval // 60}m during market hours (09:15-15:30 IST)")
    while True:
        current_t = _now_ist().time()
        if current_t >= MARKET_CLOSE_IST:
            # Exit cleanly at close rather than sleep through the night --
            # AngelOne's JWT session token expires at midnight and this
            # process never re-authenticates, so surviving past midnight
            # just means every call fails until someone notices. State
            # persists to STATE_FILE, so a clean daily exit is safe --
            # systemd's daily timer restarts the process fresh (new
            # connect(), new JWT) before the next session's market open.
            log.info("Market closed for the day — exiting cleanly (daily systemd timer restarts before next open)")
            raise SystemExit(0)
        if current_t < MARKET_OPEN_IST:
            log.info(f"Outside market hours ({current_t.strftime('%H:%M')} IST) — sleeping {args.interval // 60}m")
            _time.sleep(args.interval)
            continue
        trader.run_cycle()
        _time.sleep(args.interval)
