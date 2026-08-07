"""
Zone strategy paper trader — AngelOne SmartAPI, no real orders ever placed.

AngelOne has no native paper-trading mode, but that doesn't matter: paper
trading just means running the real decision logic against live data and
logging what it *would* do instead of calling the order API. This script
does exactly that, reusing the same zone-detection and entry-confirmation
code as the backtest (strategies/zone_detector.py, strategies/
entry_confirmation.py) so a paper trade and a backtested trade are produced
by identical logic — the only thing that changes is where the bars come from.

Why this exists: the zone strategy has only ever been validated against the
same historical dataset it was tuned on (see docs / project memory). Every
fix this project has made has been found by re-running against that same
data, which means no number produced so far is a genuine out-of-sample read.
This script is how that finally happens — running the LOCKED current rule
set forward against data it has never seen, with no further tuning allowed
while it runs.

Unlike the ORB bot, this is a SWING strategy (positions can span multiple
days), so there is no end-of-day square-off — positions carry over between
runs via the persisted state file.

Every equity signal ALSO gets a shadow options overlay: a real, listed
single-stock option (long CE for a demand/long signal, long PE for supply/
short) resolved and paper-traded against its own real live LTP, logged
separately to zone_options_paper_trades.csv. This exists specifically to
widen the strategy's trade-frequency bottleneck -- the equity bot alone was
producing very few real signals even pooled across 77 symbols, and options
give the exact same rare signals a second, leveraged outlet without touching
any of the zone-quality filters that produce them. Costs zero extra
candle-fetch API calls (reuses the bars already fetched for the equity
signal) -- only the incremental option-chain lookup + LTP fetch on an actual
signal, which is rare by construction.

Data flow per cycle:
  1. Pull today's 5-min and 1-hour bars via AngelOne REST (getCandleData),
     merged onto a warm-started historical base (backtest/data/*.parquet)
     so ATR/RSI/leg-in windows are valid from the first live bar, not empty.
  2. Re-run find_zones() on the updated bars -- deterministic, no hindsight
     issue, this naturally picks up newly formed zones.
  3. For each live (untouched-out, unbroken) zone, run check_entry() on the
     latest bar -- exactly the same function the backtest uses.
  4. For each open paper position, check the newest bar against stop /
     trailing-stop / target -- same logic as zone_backtest.py's exit loop,
     just evaluated one bar at a time as it actually arrives instead of in
     one historical sweep.

Usage
-----
    python zone_paper_trader.py              # loop every 5 min during market hours
    python zone_paper_trader.py --once       # single cycle and exit (testing)
    python zone_paper_trader.py --interval 900
"""
from __future__ import annotations

import json
import logging
import time as _time
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from dotenv import load_dotenv

from brokers.angelone import INTERVAL_1HOUR, INTERVAL_5MIN, AngelOneClient
from strategies.entry_confirmation import check_entry
from strategies.zone_detector import Zone, atr_series, find_zones
from zone_screener import get_zone_symbols

load_dotenv()

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN_IST = time(9, 15)
MARKET_CLOSE_IST = time(15, 30)

REPO_ROOT = Path(__file__).parent
DATA_DIR = REPO_ROOT / "backtest" / "data"
LOG_DIR = REPO_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
TRADES_CSV = LOG_DIR / "zone_paper_trades.csv"
OPTIONS_TRADES_CSV = LOG_DIR / "zone_options_paper_trades.csv"
STATE_FILE = LOG_DIR / "zone_paper_state.json"

# ---------------------------------------------------------------------------
# Logging -- console + rotating daily file, same pattern as india_orb_bot.py
#
# Python's logging module stamps %(asctime)s using the OS's LOCAL timezone
# (time.localtime()), not whatever timezone the code actually reasons in --
# on this machine that's US Eastern, while every market-hours decision here
# correctly uses IST via ZoneInfo. Left as the stdlib default, that produces
# a log file whose timestamps are silently ~9.5h off from the IST clock the
# bot is actually operating on, making a perfectly healthy process look
# stalled/hung when read back later. ISTFormatter fixes the display only --
# it was never a bug in the actual market-hours gating logic.
# ---------------------------------------------------------------------------
class ISTFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, IST)
        return dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S")


_LOG_FORMAT = "%(asctime)s IST | %(levelname)s | %(message)s"
logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)
logging.getLogger().handlers[0].setFormatter(ISTFormatter(_LOG_FORMAT))
_today_label = datetime.now(IST).strftime("%Y%m%d")
_file_handler = logging.FileHandler(LOG_DIR / f"zone_paper_{_today_label}.log", encoding="utf-8")
_file_handler.setFormatter(ISTFormatter(_LOG_FORMAT))
logging.getLogger().addHandler(_file_handler)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Strategy constants -- copied verbatim from backtest/zone_backtest.py so a
# paper trade and a backtested trade are produced by the same locked rules.
# DO NOT tune these here. If they change, they change in the backtest first,
# with a backtest run to justify it, and get copied here after.
# ---------------------------------------------------------------------------
MAX_TOUCHES = 3
INVALIDATION_BUFFER_PCT = 0.002
WARMUP_WINDOW = 300
BREAKEVEN_TRIGGER_R = 1.0
TRAIL_PROFIT_PCT = 0.5
MIN_RISK_ATR_MULT = 1.0
STOP_BUFFER_PCT = 0.002
TARGET_RR = 2.0
MAX_HOLD_BARS = 1500  # backstop only, same as backtest -- not the primary exit

# ---------------------------------------------------------------------------
# Options overlay -- on every equity zone signal, ALSO resolve and paper-trade
# a real single-stock option (long CE for a demand/long signal, long PE for a
# supply/short one). Reuses the exact same bars/zones/signal already computed
# above -- zero extra candle-fetch API calls, only the incremental option-
# chain lookup + LTP fetch on an actual signal (rare, not per-cycle). Verified
# live 2026-08-07: NSE single-stock options are monthly-only, last Tuesday of
# the month (same convention as BANKNIFTY), American exercise. The equity
# position's stop/target/trailing logic (unchanged) still decides WHEN to
# exit; only the option's own real LTP is used for the option's P&L.
# ---------------------------------------------------------------------------
MIN_DTE_DAYS = 2  # avoid resolving a same-day/next-day 0DTE-style contract


def _next_monthly_expiry(d: date) -> date:
    """Last Tuesday of d's month; if already past, last Tuesday of next month."""
    def last_tuesday_of(year: int, month: int) -> date:
        first_of_next = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        last_day = first_of_next - timedelta(days=1)
        offset = (last_day.weekday() - 1) % 7
        return last_day - timedelta(days=offset)

    candidate = last_tuesday_of(d.year, d.month)
    if candidate >= d:
        return candidate
    nm_year, nm_month = (d.year, d.month + 1) if d.month < 12 else (d.year + 1, 1)
    return last_tuesday_of(nm_year, nm_month)


def _pick_stock_expiry(entry_date: date) -> date:
    floor_date = entry_date + timedelta(days=MIN_DTE_DAYS)
    expiry = _next_monthly_expiry(entry_date)
    while expiry < floor_date:
        expiry = _next_monthly_expiry(expiry + timedelta(days=1))
    return expiry


@dataclass
class OptionPosition:
    symbol: str                # underlying stock symbol
    direction: str              # "long" | "short" -- of the underlying zone signal
    pattern: str
    zone_key: str                # ties back to the equity OpenPosition for the same zone
    entry_ts: str
    option_type: str            # "CE" | "PE"
    option_symbol: str
    option_token: str
    strike: float
    expiry: str                  # ISO date
    entry_option_price: float    # real LTP at entry
    touch_number: int


@dataclass
class OpenPosition:
    symbol: str
    direction: str          # "long" | "short"
    pattern: str
    zone_key: str            # zone.base_start.isoformat() -- clears zone_has_open_position on close
    zone_low: float
    zone_high: float
    entry_ts: str            # ISO -- JSON-serializable for state persistence
    entry_price: float
    stop: float
    cur_stop: float
    target: float
    peak: float
    risk: float
    touch_number: int


class SymbolState:
    """Per-symbol live state: bar history, detected zones, and which zones
    currently have an open position against them (so we don't re-trigger an
    entry on a touch we're already trading)."""

    def __init__(self, symbol: str, token: str):
        self.symbol = symbol
        self.token = token
        self.bars_5m = pd.DataFrame()
        self.bars_1h = pd.DataFrame()
        self.zones_5m: list[Zone] = []
        self.last_checked_bar_ts: pd.Timestamp | None = None
        # Seeded to "now" (not 0.0) -- warm-start already loaded 1h bars from
        # parquet, so the first live cycle doesn't need to re-fetch them too.
        # Starting at 0.0 made every symbol's very first cycle issue BOTH a
        # 5m and a 1h getCandleData call, doubling the initial call volume
        # right when rate-limit failures were already at ~100%.
        self.last_1h_fetch: float = _time.monotonic()
        # base_start ISO -> True while a position is open against that zone
        self.zone_has_open_position: dict[str, bool] = {}
        # base_start ISO -> (touches, broken) carried across cycles, since
        # find_zones() rebuilds fresh Zone objects every call
        self.zone_touch_state: dict[str, tuple[int, bool]] = {}


def _warm_start_bars(symbol: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load cached historical parquet as a warm start so ATR/RSI/leg-in
    windows are valid from the first live cycle instead of empty. Falls back
    to empty frames (cold start) for symbols never backtested before --
    those just won't produce signals until enough live bars accumulate."""
    from backtest.fetch_nse_data import load_nse_bars_df
    from backtest.fetch_nse_multi_tf import load_bars_df

    m_path = DATA_DIR / f"{symbol}_NSE_5m.parquet"
    h_path = DATA_DIR / f"{symbol}_NSE_1h.parquet"
    bars_5m = load_nse_bars_df(m_path) if m_path.exists() else pd.DataFrame()
    bars_1h = load_bars_df(h_path) if h_path.exists() else pd.DataFrame()
    return bars_5m, bars_1h


def _merge_new_bars(existing: pd.DataFrame, fresh: pd.DataFrame) -> pd.DataFrame:
    """Append genuinely new rows from `fresh` onto `existing`, deduped by
    timestamp. `fresh` (from get_today_candles) always covers the whole of
    today, so most rows are already present -- only the tail is new."""
    if fresh is None or fresh.empty:
        return existing
    if existing.empty:
        return fresh
    fresh = fresh[~fresh.index.isin(existing.index)]
    if fresh.empty:
        return existing
    return pd.concat([existing, fresh]).sort_index()


class ZonePaperTrader:
    def __init__(self, client: AngelOneClient):
        self.client = client
        self.states: dict[str, SymbolState] = {}
        self.open_positions: list[OpenPosition] = []
        self.open_option_positions: list[OptionPosition] = []

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def add_symbol(self, symbol: str, token: str) -> None:
        state = SymbolState(symbol, token)
        state.bars_5m, state.bars_1h = _warm_start_bars(symbol)
        # Pay the full-history find_zones() scan ONCE here, at startup --
        # measured at ~2.4s for a single 14k-bar symbol, so scanning the full
        # history every 5-min cycle across 65 symbols would take minutes and
        # get slower every day as live history accumulates. _refresh_zones()
        # only ever scans the recent tail after this.
        if len(state.bars_5m) >= 50:
            state.zones_5m = [z for z in find_zones(state.bars_5m, timeframe="5m") if z.is_reversal]
        self.states[symbol] = state

    def load_state(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            raw = json.loads(STATE_FILE.read_text())
        except Exception as e:
            log.warning(f"Could not load state file ({e}) -- starting fresh")
            return
        self.open_positions = [OpenPosition(**p) for p in raw.get("open_positions", [])]
        self.open_option_positions = [OptionPosition(**p) for p in raw.get("open_option_positions", [])]
        for sym, touch_state in raw.get("zone_touch_state", {}).items():
            if sym in self.states:
                self.states[sym].zone_touch_state = {
                    k: tuple(v) for k, v in touch_state.items()
                }
        for sym in raw.get("zone_has_open_position", {}):
            if sym in self.states:
                self.states[sym].zone_has_open_position = raw["zone_has_open_position"][sym]
        log.info(f"State loaded — {len(self.open_positions)} open position(s) restored")

    def save_state(self) -> None:
        payload = {
            "open_positions": [asdict(p) for p in self.open_positions],
            "open_option_positions": [asdict(p) for p in self.open_option_positions],
            "zone_touch_state": {
                sym: {k: list(v) for k, v in st.zone_touch_state.items()}
                for sym, st in self.states.items()
                if st.zone_touch_state
            },
            "zone_has_open_position": {
                sym: st.zone_has_open_position
                for sym, st in self.states.items()
                if st.zone_has_open_position
            },
        }
        STATE_FILE.write_text(json.dumps(payload, indent=2))

    # ------------------------------------------------------------------
    # Per-cycle bar refresh
    # ------------------------------------------------------------------

    def _update_bars(self, state: SymbolState) -> bool:
        """Fetch today's bars and merge onto history. Returns True if a new
        5-min bar has closed since we last checked this symbol."""
        fresh_5m = self.client.get_today_candles(state.symbol, state.token, interval=INTERVAL_5MIN)
        state.bars_5m = _merge_new_bars(state.bars_5m, fresh_5m)

        # 1h bars only change once an hour -- refetching every 5-min cycle is
        # pure API-quota waste across a 65-symbol watchlist (130 calls/cycle
        # instead of ~77). getCandleData's rate limit isn't documented in
        # this codebase, so err conservative rather than find out the hard way.
        now = _time.monotonic()
        if now - state.last_1h_fetch >= 3300:  # ~55 min, leaves slack before the hour rolls
            _time.sleep(1.0)  # space out the two calls for this same symbol
            fresh_1h = self.client.get_today_candles(state.symbol, state.token, interval=INTERVAL_1HOUR)
            state.bars_1h = _merge_new_bars(state.bars_1h, fresh_1h)
            state.last_1h_fetch = now

        if state.bars_5m.empty:
            return False
        latest_ts = state.bars_5m.index[-1]
        is_new = latest_ts != state.last_checked_bar_ts
        return is_new

    def _refresh_zones(self, state: SymbolState) -> None:
        """Scan only the recent tail for newly formed zones and merge them
        into the persistent list -- state.zones_5m is seeded once (full
        history) in add_symbol() and never fully rescanned again. A zone
        already known keeps its accumulated touches/broken state; anything
        new found in the tail window is added fresh. TAIL_SCAN_BARS is
        generous headroom above what a single base+breakout can span
        (legin_bars=8 + max_base_bars=4 + breakout_leg_bars=3) so a pattern
        straddling the window boundary is never missed."""
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

    def _check_new_entries(self, state: SymbolState) -> None:
        five = state.bars_5m
        five_atr = atr_series(five, 14)
        n = len(five)

        for zone in state.zones_5m:
            if zone.touches >= MAX_TOUCHES or zone.broken:
                continue
            key = zone.base_start.isoformat()
            if state.zone_has_open_position.get(key):
                continue  # already trading this zone's current touch

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
                continue  # stop too tight, matches backtest's noise-floor guard

            target = entry_price + risk * TARGET_RR if is_long else entry_price - risk * TARGET_RR

            zone.touches += 1
            state.zone_touch_state[key] = (zone.touches, zone.broken)
            state.zone_has_open_position[key] = True

            pos = OpenPosition(
                symbol=state.symbol, direction="long" if is_long else "short",
                pattern=zone.pattern, zone_key=key,
                zone_low=zone.price_low, zone_high=zone.price_high,
                entry_ts=five.index[-1].isoformat(), entry_price=round(entry_price, 4),
                stop=round(stop, 4), cur_stop=round(stop, 4), target=round(target, 4),
                peak=round(entry_price, 4), risk=round(risk, 4), touch_number=zone.touches,
            )
            self.open_positions.append(pos)
            log.info(
                f"PAPER ENTRY — {state.symbol} {pos.direction.upper()} {pos.pattern} "
                f"@ {entry_price:.2f} | stop {stop:.2f} | target {target:.2f} "
                f"| touch #{zone.touches} | zone {zone.price_low:.2f}-{zone.price_high:.2f}"
            )

            self._open_option_position(state, zone, key, is_long, entry_price, five.index[-1], zone.touches)

    def _open_option_position(
        self, state: SymbolState, zone: Zone, zone_key: str, is_long: bool,
        entry_price: float, entry_ts: pd.Timestamp, touch_number: int,
    ) -> None:
        """Resolve a real, listed single-stock option for this signal and
        record its real live LTP as the entry price. Best-effort: a symbol
        with no listed options, or a failed LTP fetch, just means no options
        overlay for this particular signal -- the equity paper trade above
        is entirely unaffected either way."""
        option_type = "CE" if is_long else "PE"
        entry_date = entry_ts.tz_convert(IST).date()
        expiry = _pick_stock_expiry(entry_date)
        interval = self.client.get_strike_interval(state.symbol, expiry)
        if interval is None:
            return  # no listed options for this stock/expiry
        strike = self.client.pick_strike(state.symbol, expiry, entry_price, offset_steps=0)
        if strike is None:
            return
        contract = self.client.resolve_option(state.symbol, expiry, strike, option_type)
        if contract is None:
            log.warning(f"{state.symbol}: no listed {option_type} at strike {strike} exp {expiry} -- options overlay skipped")
            return
        opt_ltp = self.client.get_ltp(contract["symbol"], contract["token"], exchange="NFO")
        if opt_ltp is None or opt_ltp <= 0:
            log.warning(f"{state.symbol}: LTP fetch failed for {contract['symbol']} -- options overlay skipped")
            return

        opt_pos = OptionPosition(
            symbol=state.symbol, direction="long" if is_long else "short",
            pattern=zone.pattern, zone_key=zone_key, entry_ts=entry_ts.isoformat(),
            option_type=option_type, option_symbol=contract["symbol"], option_token=contract["token"],
            strike=strike, expiry=expiry.isoformat(), entry_option_price=round(opt_ltp, 2),
            touch_number=touch_number,
        )
        self.open_option_positions.append(opt_pos)
        log.info(f"PAPER ENTRY (option) — {state.symbol} BUY {contract['symbol']} @ {opt_ltp:.2f}")

    def _close_option_position(self, opt_pos: OptionPosition, exit_reason: str, exit_ts: pd.Timestamp) -> None:
        opt_ltp = self.client.get_ltp(opt_pos.option_symbol, opt_pos.option_token, exchange="NFO")
        if opt_ltp is None:
            # Real quote unavailable (e.g. contract already expired/delisted) --
            # settle at intrinsic value against the current underlying price
            # rather than dropping the trade from the log.
            state = self.states.get(opt_pos.symbol)
            spot = float(state.bars_5m.iloc[-1]["close"]) if state is not None and not state.bars_5m.empty else opt_pos.strike
            log.warning(f"{opt_pos.option_symbol}: LTP unavailable at exit -- settling at intrinsic value")
            opt_ltp = max(spot - opt_pos.strike, 0.0) if opt_pos.option_type == "CE" else max(opt_pos.strike - spot, 0.0)

        pnl_pct = (opt_ltp - opt_pos.entry_option_price) / opt_pos.entry_option_price * 100
        log.info(
            f"PAPER EXIT (option) — {opt_pos.symbol} {opt_pos.option_symbol} @ {opt_ltp:.2f} "
            f"({exit_reason}) | pnl {pnl_pct:+.2f}%"
        )
        row = {
            "symbol": opt_pos.symbol, "direction": opt_pos.direction, "pattern": opt_pos.pattern,
            "option_symbol": opt_pos.option_symbol, "option_type": opt_pos.option_type,
            "strike": opt_pos.strike, "expiry": opt_pos.expiry, "touch_number": opt_pos.touch_number,
            "entry_ts": opt_pos.entry_ts, "entry_option_price": opt_pos.entry_option_price,
            "exit_ts": exit_ts.isoformat(), "exit_option_price": round(opt_ltp, 2),
            "exit_reason": exit_reason, "pnl_pct": round(pnl_pct, 2),
        }
        _append_option_trade_row(row)

    # ------------------------------------------------------------------
    # Exits -- same stop/trailing-stop/target/zero-volume logic as
    # zone_backtest.py's exit loop, evaluated one new bar at a time.
    # ------------------------------------------------------------------

    def _check_exits(self, state: SymbolState) -> None:
        if state.bars_5m.empty:
            return
        bar = state.bars_5m.iloc[-1]
        if bar["volume"] <= 0:
            return  # data artifact, same as backtest -- can't have genuinely traded here

        still_open: list[OpenPosition] = []
        for pos in self.open_positions:
            if pos.symbol != state.symbol:
                still_open.append(pos)
                continue

            is_long = pos.direction == "long"
            exit_price, exit_reason = None, None
            if is_long:
                if bar["low"] <= pos.cur_stop:
                    exit_price = pos.cur_stop
                    exit_reason = "trailing_stop" if pos.cur_stop > pos.stop else "stop_loss"
                elif bar["high"] >= pos.target:
                    exit_price, exit_reason = pos.target, "take_profit"
            else:
                if bar["high"] >= pos.cur_stop:
                    exit_price = pos.cur_stop
                    exit_reason = "trailing_stop" if pos.cur_stop < pos.stop else "stop_loss"
                elif bar["low"] <= pos.target:
                    exit_price, exit_reason = pos.target, "take_profit"

            # Backstop, same as the backtest's max_hold_bars -- force-close
            # rather than let a stuck position sit open indefinitely.
            if exit_price is None:
                entry_pos_idx = state.bars_5m.index.searchsorted(pd.Timestamp(pos.entry_ts), side="right")
                bars_held = len(state.bars_5m) - entry_pos_idx
                if bars_held >= MAX_HOLD_BARS:
                    exit_price, exit_reason = float(bar["close"]), "max_hold"

            if exit_price is not None:
                self._close_position(pos, exit_price, exit_reason, state.bars_5m.index[-1])
                matching_opt = next((o for o in self.open_option_positions if o.zone_key == pos.zone_key), None)
                if matching_opt is not None:
                    self._close_option_position(matching_opt, exit_reason, state.bars_5m.index[-1])
                    self.open_option_positions = [o for o in self.open_option_positions if o is not matching_opt]
                continue

            # Trailing-stop update from this bar's excursion -- takes effect
            # next bar, matching the backtest's look-ahead avoidance.
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

    def _close_position(self, pos: OpenPosition, exit_price: float, exit_reason: str, exit_ts: pd.Timestamp) -> None:
        is_long = pos.direction == "long"
        pnl_pct = (
            (exit_price - pos.entry_price) / pos.entry_price * 100 if is_long
            else (pos.entry_price - exit_price) / pos.entry_price * 100
        )
        log.info(
            f"PAPER EXIT  — {pos.symbol} {pos.direction.upper()} @ {exit_price:.2f} "
            f"({exit_reason}) | pnl {pnl_pct:+.2f}%"
        )
        row = {
            "symbol": pos.symbol, "direction": pos.direction, "pattern": pos.pattern,
            "zone_low": pos.zone_low, "zone_high": pos.zone_high,
            "touch_number": pos.touch_number,
            "entry_ts": pos.entry_ts, "entry_price": pos.entry_price,
            "exit_ts": exit_ts.isoformat(), "exit_price": round(exit_price, 4),
            "exit_reason": exit_reason, "pnl_pct": round(pnl_pct, 3),
        }
        _append_trade_row(row)
        state = self.states.get(pos.symbol)
        if state is not None:
            state.zone_has_open_position[pos.zone_key] = False

    def _check_option_expiries(self) -> None:
        """Force-close any option position whose own contract has expired --
        the equity swing position it's shadowing can run far longer than a
        single monthly options contract, so this is checked independently of
        the equity position's own stop/target/trailing status."""
        today = _now_ist().date()
        still_open = []
        for opt_pos in self.open_option_positions:
            if today >= date.fromisoformat(opt_pos.expiry):
                self._close_option_position(opt_pos, "contract_expiry", _now_ist())
            else:
                still_open.append(opt_pos)
        self.open_option_positions = still_open

    # ------------------------------------------------------------------
    # Main cycle
    # ------------------------------------------------------------------

    def run_cycle(self) -> None:
        for state in self.states.values():
            try:
                has_new_bar = self._update_bars(state)
            except Exception as e:
                log.error(f"{state.symbol}: bar update failed — {e}")
                continue
            finally:
                # 0.35s wasn't nearly enough -- a live run against all 77
                # zone-universe symbols got "Access denied because of
                # exceeding access rate" from getCandleData repeatedly.
                # 1/s matches the pacing already used elsewhere in this
                # codebase for AngelOne's other rate-limited endpoints
                # (searchScrip); still unverified against AngelOne's actual
                # documented limit for this specific endpoint.
                _time.sleep(1.0)
            if not has_new_bar:
                continue
            state.last_checked_bar_ts = state.bars_5m.index[-1]
            self._refresh_zones(state)
            self._check_new_entries(state)
            self._check_exits(state)
        self._check_option_expiries()
        self.save_state()
        positions_desc = ", ".join(f"{p.symbol} {p.direction}" for p in self.open_positions) or "none"
        log.info(
            f"Cycle complete — {len(self.open_positions)} open equity position(s): {positions_desc} "
            f"| {len(self.open_option_positions)} open option position(s)"
        )


def _append_trade_row(row: dict) -> None:
    df = pd.DataFrame([row])
    write_header = not TRADES_CSV.exists()
    df.to_csv(TRADES_CSV, mode="a", header=write_header, index=False)


def _append_option_trade_row(row: dict) -> None:
    df = pd.DataFrame([row])
    write_header = not OPTIONS_TRADES_CSV.exists()
    df.to_csv(OPTIONS_TRADES_CSV, mode="a", header=write_header, index=False)


def _now_ist() -> datetime:
    return datetime.now(IST)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Zone strategy paper trader — AngelOne SmartAPI, no real orders")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit (testing)")
    parser.add_argument("--interval", type=int, default=900, metavar="SECONDS", help="Loop interval (default 900 = 15 min -- 5 min was triggering AngelOne rate-limit errors on nearly every cycle across 62 symbols)")
    args = parser.parse_args()

    log.info("*** PAPER TRADING — no real orders will ever be placed by this script ***")

    client = AngelOneClient()
    if not client.connect():
        log.error("AngelOne authentication failed — check credentials in .env")
        raise SystemExit(1)

    # Restrict to symbols with cached historical parquet (meaningful warm
    # start) rather than the full ZONE_SYMBOLS universe -- fewer wasted
    # getCandleData calls on cold-start symbols while rate limits are this
    # tight, and every one of these has already been backtested.
    from backtest.zone_backtest import _cached_symbols
    all_symbols = get_zone_symbols()
    cached = set(_cached_symbols())
    symbols = [s for s in all_symbols if s in cached]
    log.info(f"Zone universe: {len(all_symbols)} symbols, {len(symbols)} with cached history -- using those")

    trader = ZonePaperTrader(client)
    log.info(f"Resolving tokens and warm-starting {len(symbols)} symbols...")
    ready = 0
    for sym in symbols:
        token = client.resolve_token(sym)
        if not token:
            log.warning(f"  {sym}: token unresolvable — skipped")
            continue
        trader.add_symbol(sym, token)
        ready += 1
    log.info(f"Watchlist ready — {ready}/{len(symbols)} symbols")
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
            # (open positions, zone touch history) persists to STATE_FILE,
            # so a clean daily exit is safe -- systemd's india-orb-bot-style
            # timer restarts the process fresh (new connect(), new JWT)
            # before the next session's market open.
            log.info("Market closed for the day — exiting cleanly (daily systemd timer restarts before next open)")
            raise SystemExit(0)
        if current_t < MARKET_OPEN_IST:
            log.info(f"Outside market hours ({current_t.strftime('%H:%M')} IST) — sleeping {args.interval // 60}m")
            _time.sleep(args.interval)
            continue
        trader.run_cycle()
        _time.sleep(args.interval)
