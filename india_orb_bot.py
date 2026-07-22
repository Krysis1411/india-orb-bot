"""
India ORB (Opening Range Breakout) day trading bot — AngelOne SmartAPI.

⚠️  REAL MONEY: AngelOne SmartAPI connects to a live brokerage account.
    Use --dry-run to test without placing actual orders.

Strategy — ORB + VWAP + EMA Combined
--------------------------------------
Opening range   : first 6 × 5-min bars (9:15–9:45 IST)
Signal 1 (ORB)  : first close above OR high / below OR low → sets daily direction bias
                  NO entry on this bar — direction recorded, not acted on immediately
Signal 2 (VWAP) : entry only when price is within ±1σ of session VWAP  [REQUIRED gate]
Signal 3 (EMA)  : EMA 9 > EMA 21 confirms direction → position size ×1.5  [optional]
Stop            : OR low × (1 − INDIA_ORB_STOP_BUFFER_PCT)
Target          : OR high + (OR range × INDIA_ORB_PROFIT_MULTIPLIER)
EOD close       : force-close all MIS positions at INDIA_CLOSE_HOUR:INDIA_CLOSE_MINUTE IST
One trade/day   : skip entry if we already have a filled order for this symbol today
Nifty filter    : only enter when Nifty 50 session-to-date return > 0 (trending up)

Usage
-----
    python india_orb_bot.py              # loop every 5 min from 9:15 → 15:15 IST
    python india_orb_bot.py --once       # single check and exit (testing)
    python india_orb_bot.py --dry-run    # log all decisions, place NO real orders
"""
import json
import logging
import logging.handlers
import os
import time as _time
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from brokers.angelone import AngelOneClient
from brokers.angelone_ws import AngelOneWebSocket
from config import (
    INDIA_ALLOW_SHORTS,
    INDIA_BLOCKLIST,
    INDIA_CLOSE_HOUR,
    INDIA_CLOSE_MINUTE,
    INDIA_DAILY_LOSS_LIMIT_PCT,
    INDIA_MAX_ENTRY_HOUR,
    INDIA_MAX_ENTRY_MINUTE,
    INDIA_MAX_OPEN_POSITIONS,
    INDIA_MAX_TOTAL_INR,
    INDIA_ORB_BREAKOUT_STRENGTH_PCT,
    INDIA_ORB_MAX_OR_PCT,
    INDIA_ORB_MIN_OR_PCT,
    INDIA_ORB_PROFIT_MULTIPLIER,
    INDIA_ORB_RANGE_BARS,
    INDIA_ORB_STOP_BUFFER_PCT,
    INDIA_ORB_VOLUME_FACTOR,
    INDIA_POSITION_SIZE_INR,
    INDIA_SKIP_MONDAY_ENTRIES,
)
from india_screener import get_active_nse_symbols

load_dotenv()

DRY_RUN = False  # Overridden to True by --dry-run CLI flag

# Tracks live SL order IDs: symbol → order_id
# Populated after entry; cancelled before any exit (target, EOD).
# Exchange-level STOPLOSS_MARKET orders protect the position without polling.
_sl_orders: dict[str, str] = {}

# Trailing stop state: symbol → True once stop has been moved to breakeven.
# Reset each session (dict is empty at startup).
_trailing_activated: dict[str, bool] = {}

# ---------------------------------------------------------------------------
# Combined strategy state (ORB direction bias + VWAP + EMA)
# ---------------------------------------------------------------------------

# ORB direction set on FIRST breakout bar — sticky for the session, not traded on.
# "long" | "short"; absent until first breakout is detected.
_orb_direction: dict[str, str] = {}

# Session VWAP state per symbol — naturally reset when bot restarts each morning.
# Stores cumulative typical-price×volume, cumulative volume, and typical-price list
# for std-dev calculation.
_vwap_state: dict[str, dict] = {}

# EMA 9/21 state per symbol — persists for the bot process lifetime.
# Each entry: {ema9, ema21, bars_seen, processed}
# processed = count of bars already fed in (avoids re-processing on repeat fetches).
_ema_state: dict[str, dict] = {}

# Opening-range cache: computed once after 09:45 and reused all day.
# Keys: symbol. Values: {or_high, or_low, avg_or_volume}.
# Avoids repeated getCandleData calls for OR data that never changes after 09:45.
_or_cache: dict[str, dict] = {}

# Session-start equity — set once at startup for daily P&L tracking.
_session_start_equity = 0.0

# Our own entry records: symbol → {price, qty, direction}
# Stored when we place the entry order; popped when the position closes.
# Needed because AngelOne's `averageprice` field in positions often returns 0.
_entry_data = {}

# Accumulated realized P&L for the session (₹).
# Updated whenever a position closes (take profit, stop loss, EOD, or exchange SL detected).
_realized_pnl = 0.0

# Populated once at startup by the pre-market screener.
# Maps symbol → SmartAPI token for today's watchlist.
# Fixed for the whole session — screener does not re-run mid-day.
today_tokens: dict[str, str] = {}

# Live WebSocket feed — started after token resolution; None until then.
# Prices are updated in real-time by a background thread.
_ws = None

# Special key for the Nifty 50 index in the WebSocket price cache.
_NIFTY_WS_KEY = "__NIFTY50__"

# ---------------------------------------------------------------------------
# Logging — console + rotating daily file under logs/
# ---------------------------------------------------------------------------
_LOG_DIR = Path(__file__).parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
_today_label = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y%m%d")
_file_handler = logging.FileHandler(
    _LOG_DIR / f"india_orb_{_today_label}.log", encoding="utf-8"
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
logging.getLogger().addHandler(_file_handler)

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN_IST  = time(9, 15)
ORB_READY_IST    = time(9, 45)   # earliest possible entry (after 6 × 5-min bars)
ENTRY_CUTOFF_IST = time(INDIA_MAX_ENTRY_HOUR, INDIA_MAX_ENTRY_MINUTE)
CLOSE_TIME_IST   = time(INDIA_CLOSE_HOUR, INDIA_CLOSE_MINUTE)
MARKET_CLOSE_IST = time(15, 30)

# AngelOne client — authenticated once at startup
client = AngelOneClient()


# ---------------------------------------------------------------------------
# Combined strategy helpers — VWAP + EMA
# ---------------------------------------------------------------------------

def _update_vwap_from_bars(symbol: str, bars) -> None:
    """Recompute session VWAP and std from all available today's bars."""
    state = _vwap_state.setdefault(symbol, {})
    cum_tpv = 0.0
    cum_vol = 0.0
    tp_list: list[float] = []
    for _, row in bars.iterrows():
        tp  = (float(row["high"]) + float(row["low"]) + float(row["close"])) / 3.0
        vol = float(row["volume"])
        if vol > 0:
            cum_tpv += tp * vol
            cum_vol  += vol
        tp_list.append(tp)
    state["cum_tpv"] = cum_tpv
    state["cum_vol"]  = cum_vol
    state["tp_list"]  = tp_list


def _get_vwap_band(symbol: str) -> tuple[float, float] | None:
    """Return (vwap, std) or None if insufficient data."""
    state = _vwap_state.get(symbol)
    if not state or state.get("cum_vol", 0) == 0 or len(state.get("tp_list", [])) < 2:
        return None
    vwap    = state["cum_tpv"] / state["cum_vol"]
    tp_list = state["tp_list"]
    mean    = sum(tp_list) / len(tp_list)
    std     = (sum((x - mean) ** 2 for x in tp_list) / len(tp_list)) ** 0.5
    return vwap, max(std, vwap * 0.001)   # floor at 0.1% of price to avoid zero-band


def _in_vwap_zone(symbol: str, price: float, sigma: float = 1.0) -> bool:
    """True if price is within ±sigma·std of session VWAP. Defaults True if no data."""
    band = _get_vwap_band(symbol)
    if band is None:
        return True
    vwap, std = band
    return abs(price - vwap) <= sigma * std


def _update_ema_from_bars(symbol: str, bars) -> None:
    """Feed new (unprocessed) bars into EMA 9/21 — skips bars already seen."""
    state = _ema_state.setdefault(
        symbol, {"ema9": None, "ema21": None, "bars_seen": 0, "processed": 0}
    )
    already = state["processed"]
    if len(bars) <= already:
        return
    for _, row in bars.iloc[already:].iterrows():
        close = float(row["close"])
        state["bars_seen"] += 1
        if state["ema9"] is None:
            state["ema9"]  = close
            state["ema21"] = close
        else:
            state["ema9"]  = 0.2    * close + 0.8    * state["ema9"]
            state["ema21"] = (2/22) * close + (20/22) * state["ema21"]
    state["processed"] = len(bars)


def _ema_agrees(symbol: str, direction: str) -> bool:
    """True if EMA 9 confirms direction vs EMA 21 (requires 9-bar warmup)."""
    state = _ema_state.get(symbol)
    if not state or state["bars_seen"] < 9 or state["ema9"] is None:
        return False
    if direction == "long":
        return state["ema9"] > state["ema21"]
    return state["ema9"] < state["ema21"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_ist() -> datetime:
    return datetime.now(IST)


def is_market_open() -> bool:
    t = now_ist().time()
    weekday = now_ist().weekday()  # 0=Mon … 4=Fri, 5=Sat, 6=Sun
    return weekday < 5 and MARKET_OPEN_IST <= t < MARKET_CLOSE_IST


def cancel_sl(symbol: str) -> None:
    """Cancel the pending exchange-level SL order for this symbol (if any)."""
    order_id = _sl_orders.pop(symbol, None)
    if order_id and not DRY_RUN:
        client.cancel_order(order_id, variety="STOPLOSS")
    elif order_id and DRY_RUN:
        log.info(f"[DRY RUN] Cancel SL order {order_id} for {symbol}")


def compute_opening_range(bars) -> tuple[float, float, float] | None:
    """
    Return (or_high, or_low, avg_volume) from the first INDIA_ORB_RANGE_BARS bars.
    Returns None if not enough bars have formed yet.
    """
    if len(bars) < INDIA_ORB_RANGE_BARS:
        return None
    or_bars = bars.iloc[:INDIA_ORB_RANGE_BARS]
    return (
        float(or_bars["high"].max()),
        float(or_bars["low"].min()),
        float(or_bars["volume"].mean()),
    )


# ---------------------------------------------------------------------------
# Per-symbol strategy logic
# ---------------------------------------------------------------------------

def process_symbol(
    symbol: str,
    token: str,
    nifty_up: bool | None,
    open_positions_count: int,
    max_open_positions: int,
    positions: list[dict] | None = None,
    orders: list[dict] | None = None,
    ltp: float | None = None,
    or_data: dict | None = None,
) -> bool:
    global _realized_pnl
    """
    Evaluate one NSE stock for ORB entry / position management.
    Returns True if a new entry order was placed.

    ltp     — pre-fetched LTP from batch quote (skips getCandleData for price)
    or_data — pre-computed OR {or_high, or_low, avg_or_volume} from _or_cache
              (skips getCandleData for OR; getCandleData still called for volume
              confirmation if a breakout is detected and volume filter is active)
    """
    # ── OR levels ──────────────────────────────────────────────────────────
    if or_data is not None:
        or_high       = or_data["or_high"]
        or_low        = or_data["or_low"]
        avg_or_volume = or_data["avg_or_volume"]
    else:
        # OR not cached yet — fetch bars and compute it now
        bars = client.get_today_candles(symbol, token)
        if bars is None:
            log.info(f"{symbol}: no bars yet")
            return False
        or_result = compute_opening_range(bars)
        if or_result is None:
            log.info(f"{symbol}: opening range not ready ({len(bars)}/{INDIA_ORB_RANGE_BARS} bars)")
            return False
        or_high, or_low, avg_or_volume = or_result
        # Use last bar price if no LTP provided
        if ltp is None:
            ltp = float(bars.iloc[-1]["close"])

    or_range = or_high - or_low

    # Skip indecisive / narrow opening ranges
    if ORB_READY_IST > now_ist().time():
        log.info(f"{symbol}: ORB window not closed yet")
        return False

    or_pct = or_range / or_high
    if INDIA_ORB_MIN_OR_PCT > 0 and or_pct < INDIA_ORB_MIN_OR_PCT:
        log.info(f"{symbol}: OR too narrow ({or_pct:.2%} < {INDIA_ORB_MIN_OR_PCT:.1%}) — skipping")
        return False
    if INDIA_ORB_MAX_OR_PCT > 0 and or_pct > INDIA_ORB_MAX_OR_PCT:
        log.info(f"{symbol}: OR too wide ({or_pct:.2%} > {INDIA_ORB_MAX_OR_PCT:.1%}) — gap/spike day, skipping")
        return False

    # ── Current price ──────────────────────────────────────────────────────
    # Use real-time LTP from batch quote when available — more accurate than
    # the 5-min bar close and requires no extra API call.
    # For volume check on entry we fetch the current bar separately below.
    if ltp is not None:
        current_price = ltp
        current_volume: float | None = None   # fetched on-demand at entry
    else:
        # Fallback: fetch bars if LTP wasn't passed in
        bars = client.get_today_candles(symbol, token)
        if bars is None:
            return False
        current_price  = float(bars.iloc[-1]["close"])
        current_volume = float(bars.iloc[-1]["volume"])
        _update_vwap_from_bars(symbol, bars)
        _update_ema_from_bars(symbol, bars)
    now            = now_ist()
    trade_qty      = max(1, int(INDIA_POSITION_SIZE_INR / current_price))

    # Per-direction stop / target levels
    long_stop    = or_low   * (1 - INDIA_ORB_STOP_BUFFER_PCT)
    long_target  = or_high  + or_range * INDIA_ORB_PROFIT_MULTIPLIER
    short_stop   = or_high  * (1 + INDIA_ORB_STOP_BUFFER_PCT)
    short_target = or_low   - or_range * INDIA_ORB_PROFIT_MULTIPLIER

    log.info(
        f"{symbol} | ₹{current_price:.2f} | OR: ₹{or_low:.2f}–₹{or_high:.2f}"
        f" | Qty: {trade_qty} | Time: {now.strftime('%H:%M')} IST"
    )

    # --- EOD forced close ---
    if now.time() >= CLOSE_TIME_IST:
        pos = client.get_position(symbol, positions)
        if pos:
            qty     = int(pos["netqty"])
            is_long = qty > 0
            close_side = "SELL" if is_long else "BUY"
            abs_qty = abs(qty)
            if abs_qty > 0:
                cancel_sl(symbol)  # must cancel SL before placing exit order
                # Record estimated realized P&L using current price as exit
                data = _entry_data.pop(symbol, {})
                entry_px_rec = data.get("price", current_price)
                qty_rec = data.get("qty", abs_qty)
                if is_long:
                    eod_realized = (current_price - entry_px_rec) * qty_rec
                else:
                    eod_realized = (entry_px_rec - current_price) * qty_rec
                _realized_pnl += eod_realized
                eod_pct = eod_realized / (entry_px_rec * qty_rec) * 100 if entry_px_rec > 0 else 0.0
                if DRY_RUN:
                    log.info(
                        f"[DRY RUN] EOD CLOSE — {close_side} {abs_qty} {symbol}"
                        f" | Est. P&L: ₹{eod_realized:+.0f} ({eod_pct:+.2f}%)"
                    )
                else:
                    client.place_market_order(symbol, token, close_side, abs_qty)
                    log.info(
                        f"EOD CLOSE — {close_side} {abs_qty} {symbol} at ~₹{current_price:.2f}"
                        f" | P&L: ₹{eod_realized:+.0f} ({eod_pct:+.2f}%)"
                    )
        return False

    # --- Manage existing position ---
    pos = client.get_position(symbol, positions)

    # Detect positions that were closed externally (exchange SL fired, or broker EOD sweep).
    # If we have a recorded entry but the position is now flat, estimate and log realized P&L.
    if symbol in _entry_data and (pos is None or int(pos.get("netqty", 0)) == 0):
        data = _entry_data.pop(symbol)
        entry_px = data["price"]
        entry_qty_stored = data["qty"]
        direction = data["direction"]
        if direction == "long":
            realized = (current_price - entry_px) * entry_qty_stored
        else:
            realized = (entry_px - current_price) * entry_qty_stored
        _realized_pnl += realized
        pct = realized / (entry_px * entry_qty_stored) * 100 if entry_px > 0 else 0.0
        log.info(
            f"{symbol}: position closed externally (exchange SL or broker)"
            f" | P&L est. ₹{realized:+.0f} ({pct:+.2f}%)"
            f" | Entry ₹{entry_px:.2f} → ₹{current_price:.2f}"
        )

    if pos:
        qty = int(pos["netqty"])
        # Use our own recorded entry price; AngelOne's averageprice field often returns 0.
        stored = _entry_data.get(symbol, {})
        reported_avg = float(pos.get("averageprice") or 0)
        avg_price = stored.get("price") or (reported_avg if reported_avg > 0 else current_price)
        is_long   = qty > 0
        abs_qty   = abs(qty)

        if is_long:
            pnl_pct    = (current_price - avg_price) / avg_price * 100
            hit_stop   = current_price <= long_stop
            hit_target = current_price >= long_target
            close_side = "SELL"
        else:  # short position
            pnl_pct    = (avg_price - current_price) / avg_price * 100
            hit_stop   = current_price >= short_stop
            hit_target = current_price <= short_target
            close_side = "BUY"

        if hit_stop or hit_target:
            reason = "TAKE PROFIT" if hit_target else "STOP LOSS"
            # For TAKE PROFIT: cancel SL order then exit at market
            # For STOP LOSS:   the SL order already fired at exchange; just clean up
            cancel_sl(symbol)
            _trailing_activated.pop(symbol, None)

            # Record realized P&L: use actual exit price (current for TP; SL trigger for SL)
            exit_px = current_price if hit_target else (long_stop if is_long else short_stop)
            data = _entry_data.pop(symbol, {})
            entry_px_rec = data.get("price", avg_price)
            qty_rec = data.get("qty", abs_qty)
            if is_long:
                _realized_pnl += (exit_px - entry_px_rec) * qty_rec
            else:
                _realized_pnl += (entry_px_rec - exit_px) * qty_rec

            if DRY_RUN:
                log.info(f"[DRY RUN] {reason} — {close_side} {abs_qty} {symbol} at ~₹{current_price:.2f} | P&L: {pnl_pct:+.2f}%")
            elif hit_target:
                client.place_market_order(symbol, token, close_side, abs_qty)
                log.info(f"{reason} — {close_side} {abs_qty} {symbol} at ~₹{current_price:.2f} | P&L: {pnl_pct:+.2f}%")
            else:
                log.info(f"{reason} — exchange SL order fired for {symbol} | P&L: {pnl_pct:+.2f}%")
            return False

        # --- Trailing stop: move SL to breakeven once price clears 0.5× target ---
        if not _trailing_activated.get(symbol, False):
            if is_long:
                half_target = or_high + or_range * INDIA_ORB_PROFIT_MULTIPLIER * 0.5
                if current_price >= half_target:
                    _trailing_activated[symbol] = True
                    cancel_sl(symbol)  # cancel original stop
                    # Place SL at breakeven minus 0.1% buffer to avoid immediate trigger
                    be_price = avg_price * 0.999
                    if DRY_RUN:
                        log.info(f"[DRY RUN] {symbol}: trailing stop — SL moved to ₹{be_price:.2f} (breakeven ₹{avg_price:.2f} −0.1%)")
                    else:
                        new_sl = client.place_sl_order(symbol, token, "SELL", abs_qty, be_price)
                        if new_sl:
                            _sl_orders[symbol] = new_sl
                        log.info(f"{symbol}: trailing stop — SL moved to ₹{be_price:.2f} (breakeven ₹{avg_price:.2f} −0.1%)")
            else:  # short
                half_target = or_low - or_range * INDIA_ORB_PROFIT_MULTIPLIER * 0.5
                if current_price <= half_target:
                    _trailing_activated[symbol] = True
                    cancel_sl(symbol)
                    # Place SL at breakeven plus 0.1% buffer to avoid immediate trigger
                    be_price = avg_price * 1.001
                    if DRY_RUN:
                        log.info(f"[DRY RUN] {symbol}: trailing stop — SL moved to ₹{be_price:.2f} (breakeven ₹{avg_price:.2f} +0.1%)")
                    else:
                        new_sl = client.place_sl_order(symbol, token, "BUY", abs_qty, be_price)
                        if new_sl:
                            _sl_orders[symbol] = new_sl
                        log.info(f"{symbol}: trailing stop — SL moved to ₹{be_price:.2f} (breakeven ₹{avg_price:.2f} +0.1%)")

        trailing_label = " [trailing]" if _trailing_activated.get(symbol) else ""
        direction_label = "LONG" if is_long else "SHORT"
        pnl_inr = pnl_pct / 100 * avg_price * abs_qty
        log.info(
            f"{symbol} | {direction_label} {abs_qty}"
            f" | Entry ₹{avg_price:.2f} → ₹{current_price:.2f}"
            f" | P&L: {pnl_pct:+.2f}% (₹{pnl_inr:+.0f})"
            f" | Stop: ₹{long_stop if is_long else short_stop:.2f}{trailing_label}"
            f" | Target: ₹{long_target if is_long else short_target:.2f}"
        )
        return False

    # --- Entry gate checks (shared) ---
    if now.time() >= ENTRY_CUTOFF_IST:
        log.info(f"{symbol}: past entry cutoff ({INDIA_MAX_ENTRY_HOUR}:{INDIA_MAX_ENTRY_MINUTE:02d} IST)")
        return False

    if INDIA_SKIP_MONDAY_ENTRIES and now.weekday() == 0:
        log.info(f"{symbol}: Monday — skipping new entries")
        return False

    if client.already_traded_today(symbol, orders):
        log.info(f"{symbol}: already traded today — skipping")
        return False

    if nifty_up is None:
        log.warning(f"Nifty data unavailable — entries proceed without trend filter")

    # Volume confirmation + VWAP/EMA update — fetch current bars if not already available
    if current_volume is None:
        _bars = client.get_today_candles(symbol, token)
        if _bars is not None:
            current_volume = float(_bars.iloc[-1]["volume"])
            _update_vwap_from_bars(symbol, _bars)
            _update_ema_from_bars(symbol, _bars)
        else:
            current_volume = 0.0

    vol_ok = current_volume >= avg_or_volume * INDIA_ORB_VOLUME_FACTOR

    if open_positions_count >= max_open_positions:
        log.info(f"{symbol}: budget limit ({open_positions_count}/{max_open_positions}) — skipping")
        return False

    # --- Long breakout: above OR high, Nifty up ---
    long_strength = (current_price - or_high) / or_high if or_high > 0 else 0.0
    if (current_price > or_high and long_strength >= INDIA_ORB_BREAKOUT_STRENGTH_PCT
            and vol_ok and nifty_up is not False):
        if current_price >= long_target:
            log.info(f"{symbol}: stale long breakout — already past target")
            return False

        # ── Combined: record direction on first breakout bar, do NOT enter yet ──────
        vol_ratio = current_volume / avg_or_volume if avg_or_volume > 0 else 0.0
        if symbol not in _orb_direction:
            _orb_direction[symbol] = "long"
            log.info(
                f"{symbol}: ORB direction SET → LONG | Vol: {vol_ratio:.1f}× avg"
                f" — waiting for VWAP pullback"
            )
            return False
        if _orb_direction[symbol] != "long":
            return False  # daily bias is SHORT — skip this long signal

        # ── VWAP proximity gate (required) ─────────────────────────────────────────
        if not _in_vwap_zone(symbol, current_price):
            band = _get_vwap_band(symbol)
            if band:
                vwap, std = band
                log.info(
                    f"{symbol}: ₹{current_price:.2f} outside VWAP zone"
                    f" (VWAP ₹{vwap:.2f} ±₹{std:.2f}) | Vol: {vol_ratio:.1f}× avg"
                    f" — waiting for pullback"
                )
            return False

        # ── EMA confirmation (optional ×1.5 boost) ─────────────────────────────────
        if _ema_agrees(symbol, "long"):
            entry_qty  = max(1, int(INDIA_POSITION_SIZE_INR * 1.5 / current_price))
            entry_type = "vwap_ema_confluence"
        else:
            entry_qty  = trade_qty
            entry_type = "vwap_pullback"

        if DRY_RUN:
            log.info(
                f"[DRY RUN] BUY {entry_qty} {symbol} [{entry_type}] | ₹{current_price:.2f}"
                f" | Stop: ₹{long_stop:.2f} | Target: ₹{long_target:.2f}"
                f" | Vol: {current_volume/avg_or_volume:.1f}×"
            )
            log.info(
                f"[DRY RUN] SL ORDER — SELL {entry_qty} {symbol}"
                f" trigger ₹{long_stop:.2f} (exchange-level STOPLOSS_MARKET)"
            )
            return True
        order_id = client.place_market_order(symbol, token, "BUY", entry_qty)
        if order_id:
            _entry_data[symbol] = {"price": current_price, "qty": entry_qty, "direction": "long"}
            log.info(
                f"LONG BREAKOUT [{entry_type}] {entry_qty} {symbol}"
                f" | ₹{current_price:.2f} | Stop: ₹{long_stop:.2f} | Target: ₹{long_target:.2f}"
            )
            sl_id = client.place_sl_order(symbol, token, "SELL", entry_qty, long_stop)
            if sl_id:
                _sl_orders[symbol] = sl_id
            return True
        return False

    # --- Short breakout: below OR low, Nifty down ---
    short_strength = (or_low - current_price) / or_low if or_low > 0 else 0.0
    if (INDIA_ALLOW_SHORTS and current_price < or_low
            and short_strength >= INDIA_ORB_BREAKOUT_STRENGTH_PCT
            and vol_ok and nifty_up is not True):
        if short_target <= 0 or current_price <= short_target:
            log.info(f"{symbol}: stale short breakout — already past target")
            return False

        # ── Combined: record direction on first breakout bar, do NOT enter yet ──────
        vol_ratio = current_volume / avg_or_volume if avg_or_volume > 0 else 0.0
        if symbol not in _orb_direction:
            _orb_direction[symbol] = "short"
            log.info(
                f"{symbol}: ORB direction SET → SHORT | Vol: {vol_ratio:.1f}× avg"
                f" — waiting for VWAP pullback"
            )
            return False
        if _orb_direction[symbol] != "short":
            return False  # daily bias is LONG — skip this short signal

        # ── VWAP proximity gate (required) ─────────────────────────────────────────
        if not _in_vwap_zone(symbol, current_price):
            band = _get_vwap_band(symbol)
            if band:
                vwap, std = band
                log.info(
                    f"{symbol}: ₹{current_price:.2f} outside VWAP zone"
                    f" (VWAP ₹{vwap:.2f} ±₹{std:.2f}) | Vol: {vol_ratio:.1f}× avg"
                    f" — waiting for pullback"
                )
            return False

        # ── EMA confirmation (optional ×1.5 boost) ─────────────────────────────────
        if _ema_agrees(symbol, "short"):
            entry_qty  = max(1, int(INDIA_POSITION_SIZE_INR * 1.5 / current_price))
            entry_type = "vwap_ema_confluence"
        else:
            entry_qty  = trade_qty
            entry_type = "vwap_pullback"

        if DRY_RUN:
            log.info(
                f"[DRY RUN] SELL SHORT {entry_qty} {symbol} [{entry_type}] | ₹{current_price:.2f}"
                f" | Stop: ₹{short_stop:.2f} | Target: ₹{short_target:.2f}"
                f" | Vol: {current_volume/avg_or_volume:.1f}×"
            )
            log.info(
                f"[DRY RUN] SL ORDER — BUY {entry_qty} {symbol}"
                f" trigger ₹{short_stop:.2f} (exchange-level STOPLOSS_MARKET)"
            )
            return True
        order_id = client.place_market_order(symbol, token, "SELL", entry_qty)
        if order_id:
            _entry_data[symbol] = {"price": current_price, "qty": entry_qty, "direction": "short"}
            log.info(
                f"SHORT BREAKOUT [{entry_type}] {entry_qty} {symbol}"
                f" | ₹{current_price:.2f} | Stop: ₹{short_stop:.2f} | Target: ₹{short_target:.2f}"
            )
            sl_id = client.place_sl_order(symbol, token, "BUY", entry_qty, short_stop)
            if sl_id:
                _sl_orders[symbol] = sl_id
            return True
        return False

    if not vol_ok:
        log.info(f"{symbol}: volume too low ({current_volume/avg_or_volume:.2f}× < {INDIA_ORB_VOLUME_FACTOR}×)")
        return False


# ---------------------------------------------------------------------------
# Main loop body
# ---------------------------------------------------------------------------

def run_india_orb() -> None:
    global _realized_pnl
    now = now_ist()
    log.info(f"--- India ORB check at {now.strftime('%H:%M')} IST ---")

    _block_new_entries = False

    # --- EOD sweep: close everything if past close time ---
    if now.time() >= CLOSE_TIME_IST:
        log.info("EOD — cancelling SL orders and force-closing all open MIS positions")
        # Cancel all live SL orders first so they don't race with our market exits
        for sym in list(_sl_orders.keys()):
            cancel_sl(sym)
        if not DRY_RUN:
            client.close_all_positions()
        else:
            positions = client.get_positions()
            for pos in positions:
                log.info(f"[DRY RUN] EOD CLOSE — {pos.get('tradingsymbol')} × {pos.get('netqty')}")
        return

    # --- Before ORB window closes (9:15–9:45 IST) ---
    if now.time() < ORB_READY_IST:
        log.info(f"Building opening range — no entries before 09:45 IST")
        return

    # --- Nifty trend filter (WebSocket first; REST fallback) ---
    nifty_ws = _ws.prices.get(_NIFTY_WS_KEY) if _ws else None
    if nifty_ws and nifty_ws.get("ltp") and nifty_ws.get("open"):
        nifty_ltp   = nifty_ws["ltp"]
        nifty_open  = nifty_ws["open"]
        nifty_up    = nifty_ltp >= nifty_open
        nifty_pct   = (nifty_ltp - nifty_open) / nifty_open if nifty_open else 0.0
        log.info(f"Nifty50 (WS): ₹{nifty_ltp:.2f}  {'UP' if nifty_up else 'DOWN'}  ({nifty_pct:+.2%})")
    else:
        nifty_up, nifty_pct = client.get_nifty_trend()

    # --- Fetch positions and order book once per cycle (rate-limit budget) ---
    # position() limit: 1/s  |  orderBook() limit: 1/s
    # Passing these cached lists down avoids N redundant API calls for N symbols.
    try:
        open_positions = client.get_positions()
    except Exception as e:
        log.error(f"Failed to fetch positions: {e}")
        open_positions = []

    _time.sleep(1.1)  # respect 1/s limit before next call

    try:
        cached_orders = client.get_order_book()
    except Exception as e:
        log.error(f"Failed to fetch order book: {e}")
        cached_orders = []

    # Pre-resolve tokens for held positions
    held_symbols: dict[str, str] = {}
    for pos in open_positions:
        raw_sym = pos.get("tradingsymbol", "").replace("-EQ", "")
        token   = pos.get("symboltoken", "")
        if raw_sym and token:
            held_symbols[raw_sym] = token

    open_count    = len(open_positions)
    max_positions = INDIA_MAX_OPEN_POSITIONS

    # --- Live prices: WebSocket cache (zero API calls) ---
    # The background WebSocket thread updates _ws.prices on every tick.
    # Fall back to REST batch quote only if WebSocket has not received data yet
    # (e.g. first cycle right after startup, or WebSocket is reconnecting).
    batch_prices: dict[str, dict] = {}
    if _ws and len(_ws.prices) >= max(1, len(today_tokens) // 2):
        batch_prices = dict(_ws.prices)   # snapshot; thread may update concurrently
        log.debug(f"Using WebSocket prices ({len(batch_prices)} symbols)")
    elif today_tokens:
        batch_prices = client.get_batch_quote(today_tokens)
        if not batch_prices:
            log.warning("Batch quote failed — will fall back to getCandleData per symbol")
        else:
            log.info("WebSocket warming up — used REST batch quote this cycle")

    # --- Daily P&L circuit-breaker (position-aware) ---
    # Uses our own _entry_data records + realized P&L — not available_funds.
    # available_funds drops by margin blocked when positions open, which caused
    # false loss-limit triggers every session. This approach tracks actual P&L.
    if _session_start_equity > 0:
        unrealized = 0.0
        for pos in open_positions:
            sym = pos.get("tradingsymbol", "").replace("-EQ", "")
            qty = int(pos.get("netqty", 0))
            if qty == 0 or sym not in _entry_data:
                continue
            entry_px = _entry_data[sym]["price"]
            ltp = batch_prices.get(sym, {}).get("ltp") or 0.0
            if ltp > 0:
                if qty > 0:
                    unrealized += (ltp - entry_px) * qty
                else:
                    unrealized += (entry_px - ltp) * abs(qty)

        total_pnl = _realized_pnl + unrealized
        daily_loss = -total_pnl
        daily_loss_pct = daily_loss / _session_start_equity

        if open_positions or _realized_pnl != 0.0:
            log.info(
                f"Session P&L: ₹{total_pnl:+,.0f} ({total_pnl / _session_start_equity:+.1%})"
                + (f" | Realized ₹{_realized_pnl:+,.0f} | Unrealized ₹{unrealized:+,.0f}"
                   if open_positions else "")
            )

        if daily_loss_pct > INDIA_DAILY_LOSS_LIMIT_PCT:
            log.warning(
                f"Daily loss limit hit — down ₹{daily_loss:,.0f}"
                f" ({daily_loss_pct:.1%} > {INDIA_DAILY_LOSS_LIMIT_PCT:.0%})"
                f" — blocking new entries for rest of session"
            )
            _block_new_entries = True

    # --- OR cache: compute opening range for uncached symbols (once after 09:45) ---
    past_orb_window = now.time() >= ORB_READY_IST
    if past_orb_window and today_tokens:
        uncached = [s for s in today_tokens if s not in _or_cache]
        if uncached:
            log.info(f"Computing OR for {len(uncached)} uncached symbols...")
            for sym in uncached:
                _time.sleep(1.1)   # getCandleData: 3/s; 1.1s keeps us safe across symbols
                tok = today_tokens[sym]
                bars = client.get_today_candles(sym, tok)
                if bars is None:
                    continue
                or_result = compute_opening_range(bars)
                if or_result is None:
                    continue
                or_high, or_low, avg_or_vol = or_result
                or_range = or_high - or_low
                or_pct   = or_range / or_high if or_high > 0 else 0
                if or_pct < INDIA_ORB_MIN_OR_PCT or (INDIA_ORB_MAX_OR_PCT > 0 and or_pct > INDIA_ORB_MAX_OR_PCT):
                    # Mark as skip so we don't retry each cycle
                    _or_cache[sym] = {"or_high": 0, "or_low": 0, "avg_or_volume": 0, "skip": True}
                else:
                    _or_cache[sym] = {"or_high": or_high, "or_low": or_low, "avg_or_volume": avg_or_vol}
                    _update_vwap_from_bars(sym, bars)
                    _update_ema_from_bars(sym, bars)
                    log.info(f"  {sym}: OR ₹{or_low:.2f}–₹{or_high:.2f} ({or_pct:.2%})")

    # --- Manage held positions (use batch LTP — no getCandleData needed) ----------
    for sym, tok in held_symbols.items():
        ltp = batch_prices.get(sym, {}).get("ltp")
        try:
            process_symbol(sym, tok, nifty_up, len(open_positions), 0,
                           positions=open_positions, orders=cached_orders,
                           ltp=ltp, or_data=_or_cache.get(sym))
        except Exception as e:
            log.error(f"{sym}: unexpected error managing position — {e}")
        _time.sleep(0.5)   # small gap between held-symbol calls

    # --- Screen for new entry opportunities ---
    if _block_new_entries:
        log.warning("Blocking new entries — risk limit active")
        return

    if not today_tokens:
        log.warning("No symbols in today's watchlist — skipping new entries")
        return

    for symbol, token in today_tokens.items():
        if symbol in held_symbols:
            continue  # already managed above

        or_data = _or_cache.get(symbol)
        if or_data and or_data.get("skip"):
            continue  # OR was too narrow/wide — not tradeable today

        ltp = batch_prices.get(symbol, {}).get("ltp")

        # Quick pre-filter using batch LTP — only call process_symbol for symbols
        # near a breakout or with no OR cached yet (avoids getCandleData for quiet symbols)
        if or_data and ltp is not None:
            near_long  = ltp > or_data["or_high"]
            near_short = ltp < or_data["or_low"] and INDIA_ALLOW_SHORTS
            if not near_long and not near_short:
                log.debug(f"{symbol}: LTP ₹{ltp:.2f} inside OR ₹{or_data['or_low']:.2f}–{or_data['or_high']:.2f} — no signal")
                continue  # price inside OR range — skip, no getCandleData needed

        try:
            opened = process_symbol(symbol, token, nifty_up, open_count, max_positions,
                                    positions=open_positions, orders=cached_orders,
                                    ltp=ltp, or_data=or_data)
            if opened:
                open_count += 1
        except Exception as e:
            log.error(f"{symbol}: unexpected error — {e}")
        _time.sleep(1.1)   # getCandleData only called here for breakout candidates


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="India ORB Bot — AngelOne SmartAPI")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log all decisions without placing real orders (strongly recommended for testing)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single check and exit (default: loop every 5 min during market hours)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        metavar="SECONDS",
        help="Loop interval in seconds (default: 300 = 5 minutes)",
    )
    args = parser.parse_args()
    DRY_RUN = args.dry_run

    if DRY_RUN:
        log.info("*** DRY RUN MODE — no real orders will be placed ***")
    else:
        log.warning("⚠️  LIVE MODE — orders will be placed in your REAL AngelOne account!")

    # Authenticate once at startup
    if not client.connect():
        log.error("AngelOne authentication failed — check credentials in .env")
        raise SystemExit(1)

    # Snapshot starting equity for daily P&L circuit-breaker
    _session_start_equity = client.get_available_funds_inr()
    log.info(
        f"Session start equity: ₹{_session_start_equity:,.2f}"
        f"  |  Daily loss limit: ₹{_session_start_equity * INDIA_DAILY_LOSS_LIMIT_PCT:,.0f}"
        f" ({INDIA_DAILY_LOSS_LIMIT_PCT:.0%})"
    )

    # Run pre-market screener once — picks today's symbols by prev-day turnover
    log.info("Running pre-market screener (yfinance, ~10s)...")
    _screener_syms = get_active_nse_symbols()

    # Filter to NSE intraday-allowed stocks only — prevents trying to trade
    # stocks restricted from MIS/intraday orders on this session.
    _intraday_ok = client.get_nse_intraday_symbols()
    if _intraday_ok:
        _before = len(_screener_syms)
        _screener_syms = [s for s in _screener_syms if s in _intraday_ok]
        log.info(
            f"Intraday filter: {_before} → {len(_screener_syms)} symbols"
            f" ({_before - len(_screener_syms)} excluded)"
        )
    else:
        log.warning("NSE intraday list unavailable — no intraday filter applied")

    # Resolve SmartAPI tokens for all selected symbols upfront.
    # ScripMaster (downloaded in connect()) handles this locally — no API calls
    # or rate-limit sleeps needed for the vast majority of NSE equity symbols.
    # searchScrip is only reached for symbols absent from ScripMaster (rare).
    log.info(f"Resolving tokens for {len(_screener_syms)} symbols (ScripMaster)...")
    for _sym in _screener_syms:
        _tok = client.resolve_token(_sym)
        if _tok:
            today_tokens[_sym] = _tok
        else:
            log.warning(f"  {_sym}: token unresolvable — excluded from today's watchlist")
    log.info(f"Today's watchlist ({len(today_tokens)}): {', '.join(today_tokens)}")

    # --- Start WebSocket streaming -----------------------------------------------
    # Subscribe to Quote mode (LTP + OHLCV) for all watchlist symbols plus Nifty 50.
    # A background daemon thread keeps the connection alive and updates _ws.prices.
    # The main loop reads _ws.prices instead of calling REST batch quote each cycle.
    from brokers.angelone import _NIFTY50_FALLBACK_TOKEN
    _ws_token_map = dict(today_tokens)
    _ws_token_map[_NIFTY_WS_KEY] = _NIFTY50_FALLBACK_TOKEN   # Nifty 50 index
    _ws = AngelOneWebSocket(
        auth_token=client._access_token,
        feed_token=client.feed_token,
        client_code=os.environ["ANGELONE_CLIENT_CODE"],
        api_key=os.environ["ANGELONE_API_KEY"],
    )
    _ws.start(_ws_token_map)
    log.info("WebSocket streaming started — prices will update in real-time")

    log.info(
        f"India Combined Bot (ORB+VWAP+EMA) | OR: first {INDIA_ORB_RANGE_BARS}×5-min bars (09:15–09:45 IST)"
        f" | ₹{INDIA_POSITION_SIZE_INR:,}/trade (×1.5 with EMA confluence)"
        f" | Entry window: 09:45–{INDIA_MAX_ENTRY_HOUR}:{INDIA_MAX_ENTRY_MINUTE:02d} IST"
        f" | EOD close: {INDIA_CLOSE_HOUR}:{INDIA_CLOSE_MINUTE:02d} IST"
    )
    log.info(
        f"Signals: [1] ORB direction bias → [2] VWAP ±1σ gate (required) → [3] EMA 9/21 boost (optional)"
        f" | Nifty filter | min OR {INDIA_ORB_MIN_OR_PCT:.1%}"
        f" | vol {INDIA_ORB_VOLUME_FACTOR}× | target {INDIA_ORB_PROFIT_MULTIPLIER}×"
        f" | stop {INDIA_ORB_STOP_BUFFER_PCT:.1%} below OR low"
    )

    if args.once:
        run_india_orb()
        log.info("Single-run complete")
    else:
        log.info(f"Loop mode — checking every {args.interval // 60}m until {INDIA_CLOSE_HOUR}:{INDIA_CLOSE_MINUTE:02d} IST")
        while True:
            current = now_ist()
            current_t = current.time()

            if current_t < MARKET_OPEN_IST:
                wait_secs = int(
                    (datetime.combine(current.date(), MARKET_OPEN_IST).replace(tzinfo=IST) - current)
                    .total_seconds()
                )
                log.info(f"Pre-market ({current_t.strftime('%H:%M')} IST) — waiting {wait_secs // 60}m for 09:15 open")
                _time.sleep(min(wait_secs + 5, args.interval))
                continue

            if current_t >= MARKET_CLOSE_IST:
                log.info(f"Past 15:30 IST — session complete, exiting")
                if _ws:
                    _ws.stop()
                break

            run_india_orb()
            log.info(f"Cycle complete — next check in {args.interval // 60}m")
            _time.sleep(args.interval)
