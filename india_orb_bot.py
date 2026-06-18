"""
India ORB (Opening Range Breakout) day trading bot — AngelOne SmartAPI.

⚠️  REAL MONEY: AngelOne SmartAPI connects to a live brokerage account.
    Use --dry-run to test without placing actual orders.

Strategy
--------
Opening range : first 6 × 5-min bars (9:15–9:45 IST)
Entry         : close breaks above OR high AND bar volume >= avg OR vol × factor
Stop          : OR low × (1 - INDIA_ORB_STOP_BUFFER_PCT)
Target        : OR high + (OR range × INDIA_ORB_PROFIT_MULTIPLIER)
EOD close     : force-close all MIS positions at INDIA_CLOSE_HOUR:INDIA_CLOSE_MINUTE IST
One trade/day : skip entry if we already have a filled BUY order for this symbol today
Nifty filter  : only enter when Nifty 50 session-to-date return > 0 (trending up)

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
from config import (
    INDIA_ALLOW_SHORTS,
    INDIA_BLOCKLIST,
    INDIA_CLOSE_HOUR,
    INDIA_CLOSE_MINUTE,
    INDIA_DAILY_LOSS_LIMIT_PCT,
    INDIA_MAX_ENTRY_HOUR,
    INDIA_MAX_ENTRY_MINUTE,
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

# Session-start equity — set once at startup for daily P&L tracking.
_session_start_equity: float = 0.0

# Populated once at startup by the pre-market screener.
# Maps symbol → SmartAPI token for today's watchlist.
# Fixed for the whole session — screener does not re-run mid-day.
today_tokens: dict[str, str] = {}

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
) -> bool:
    """
    Evaluate one NSE stock for ORB entry / position management.
    Returns True if a new BUY was placed.
    """
    bars = client.get_today_candles(symbol, token)
    if bars is None:
        log.info(f"{symbol}: no bars yet")
        return False

    or_result = compute_opening_range(bars)
    if or_result is None:
        log.info(f"{symbol}: opening range not ready ({len(bars)}/{INDIA_ORB_RANGE_BARS} bars)")
        return False

    or_high, or_low, avg_or_volume = or_result
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

    current_bar    = bars.iloc[-1]
    current_price  = float(current_bar["close"])
    current_volume = float(current_bar["volume"])
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
                if DRY_RUN:
                    log.info(f"[DRY RUN] EOD CLOSE — {close_side} {abs_qty} {symbol} at ~₹{current_price:.2f}")
                else:
                    client.place_market_order(symbol, token, close_side, abs_qty)
                    log.info(f"EOD CLOSE — {close_side} {abs_qty} {symbol} at ~₹{current_price:.2f}")
        return False

    # --- Manage existing position ---
    pos = client.get_position(symbol, positions)
    if pos:
        qty       = int(pos["netqty"])
        avg_price = float(pos.get("averageprice", current_price))
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
                    be_price = avg_price
                    if DRY_RUN:
                        log.info(f"[DRY RUN] {symbol}: trailing stop — SL moved to breakeven ₹{be_price:.2f}")
                    else:
                        new_sl = client.place_sl_order(symbol, token, "SELL", abs_qty, be_price)
                        if new_sl:
                            _sl_orders[symbol] = new_sl
                        log.info(f"{symbol}: trailing stop — SL moved to breakeven ₹{be_price:.2f}")
            else:  # short
                half_target = or_low - or_range * INDIA_ORB_PROFIT_MULTIPLIER * 0.5
                if current_price <= half_target:
                    _trailing_activated[symbol] = True
                    cancel_sl(symbol)
                    be_price = avg_price
                    if DRY_RUN:
                        log.info(f"[DRY RUN] {symbol}: trailing stop — SL moved to breakeven ₹{be_price:.2f}")
                    else:
                        new_sl = client.place_sl_order(symbol, token, "BUY", abs_qty, be_price)
                        if new_sl:
                            _sl_orders[symbol] = new_sl
                        log.info(f"{symbol}: trailing stop — SL moved to breakeven ₹{be_price:.2f}")

        trailing_label = " [trailing]" if _trailing_activated.get(symbol) else ""
        direction_label = "LONG" if is_long else "SHORT"
        log.info(
            f"{symbol} | {direction_label} {abs_qty} @ ₹{avg_price:.2f}"
            f" | P&L: {pnl_pct:+.2f}%"
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
        if DRY_RUN:
            log.info(
                f"[DRY RUN] BUY {trade_qty} {symbol} | ₹{current_price:.2f}"
                f" | Stop: ₹{long_stop:.2f} | Target: ₹{long_target:.2f}"
                f" | Vol: {current_volume/avg_or_volume:.1f}×"
            )
            log.info(
                f"[DRY RUN] SL ORDER — SELL {trade_qty} {symbol}"
                f" trigger ₹{long_stop:.2f} (exchange-level STOPLOSS_MARKET)"
            )
            return True
        order_id = client.place_market_order(symbol, token, "BUY", trade_qty)
        if order_id:
            log.info(f"LONG BREAKOUT {trade_qty} {symbol} | ₹{current_price:.2f} | Stop: ₹{long_stop:.2f} | Target: ₹{long_target:.2f}")
            # Place exchange-level SL immediately (SELL trigger at long_stop)
            sl_id = client.place_sl_order(symbol, token, "SELL", trade_qty, long_stop)
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
        if DRY_RUN:
            log.info(
                f"[DRY RUN] SELL SHORT {trade_qty} {symbol} | ₹{current_price:.2f}"
                f" | Stop: ₹{short_stop:.2f} | Target: ₹{short_target:.2f}"
                f" | Vol: {current_volume/avg_or_volume:.1f}×"
            )
            log.info(
                f"[DRY RUN] SL ORDER — BUY {trade_qty} {symbol}"
                f" trigger ₹{short_stop:.2f} (exchange-level STOPLOSS_MARKET)"
            )
            return True
        order_id = client.place_market_order(symbol, token, "SELL", trade_qty)
        if order_id:
            log.info(f"SHORT BREAKOUT {trade_qty} {symbol} | ₹{current_price:.2f} | Stop: ₹{short_stop:.2f} | Target: ₹{short_target:.2f}")
            # Place exchange-level SL immediately (BUY trigger at short_stop)
            sl_id = client.place_sl_order(symbol, token, "BUY", trade_qty, short_stop)
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
    now = now_ist()
    log.info(f"--- India ORB check at {now.strftime('%H:%M')} IST ---")

    # --- Daily P&L circuit-breaker ---
    _block_new_entries = False
    try:
        funds = client.get_available_funds_inr()
        if funds <= 0:
            log.warning("Zero available funds — blocking new entries")
            _block_new_entries = True
        elif _session_start_equity > 0:
            daily_loss = _session_start_equity - funds
            daily_loss_pct = daily_loss / _session_start_equity
            if daily_loss_pct > INDIA_DAILY_LOSS_LIMIT_PCT:
                log.warning(
                    f"Daily loss limit hit — down ₹{daily_loss:,.0f}"
                    f" ({daily_loss_pct:.1%} > {INDIA_DAILY_LOSS_LIMIT_PCT:.0%})"
                    f" — blocking new entries for rest of session"
                )
                _block_new_entries = True
    except Exception:
        pass

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

    # --- Nifty trend filter (shared across all symbols) ---
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

    for sym, tok in held_symbols.items():
        _time.sleep(1.1)  # getCandleData limit: 3/s — held loop has no other throttle
        try:
            process_symbol(sym, tok, nifty_up, len(open_positions), 0,
                           positions=open_positions, orders=cached_orders)
        except Exception as e:
            log.error(f"{sym}: unexpected error managing position — {e}")

    open_count     = len(open_positions)
    max_positions  = max(1, int(INDIA_MAX_TOTAL_INR / INDIA_POSITION_SIZE_INR))

    # --- Screen for new entry opportunities ---
    if _block_new_entries:
        log.warning("Blocking new entries — risk limit active")
        return

    if not today_tokens:
        log.warning("No symbols in today's watchlist — skipping new entries")
        return

    for symbol, token in today_tokens.items():
        _time.sleep(1.5)  # getCandleData limit: 3/s, 180/min — 1.5s keeps us well under
        if symbol in held_symbols:
            continue  # already managed above
        try:
            opened = process_symbol(symbol, token, nifty_up, open_count, max_positions,
                                    positions=open_positions, orders=cached_orders)
            if opened:
                open_count += 1
        except Exception as e:
            log.error(f"{symbol}: unexpected error — {e}")


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
    global _session_start_equity
    _session_start_equity = client.get_available_funds_inr()
    log.info(
        f"Session start equity: ₹{_session_start_equity:,.2f}"
        f"  |  Daily loss limit: ₹{_session_start_equity * INDIA_DAILY_LOSS_LIMIT_PCT:,.0f}"
        f" ({INDIA_DAILY_LOSS_LIMIT_PCT:.0%})"
    )

    # Run pre-market screener once — picks today's symbols by prev-day turnover
    log.info("Running pre-market screener (yfinance, ~10s)...")
    _screener_syms = get_active_nse_symbols()

    # Resolve SmartAPI tokens for all selected symbols upfront.
    # INDIA_TOKEN_MAP handles known symbols instantly; new symbols call searchScrip.
    # Done here (~08:50 IST) so tokens are ready before the 09:45 ORB window.
    log.info(f"Resolving tokens for {len(_screener_syms)} symbols...")
    for _sym in _screener_syms:
        _tok = client.resolve_token(_sym)
        if _tok:
            today_tokens[_sym] = _tok
        else:
            log.warning(f"  {_sym}: token unresolvable — excluded from today's watchlist")
        _time.sleep(1.2)  # searchScrip rate limit: 1/s — 1.2s gives safe headroom
    log.info(f"Today's watchlist ({len(today_tokens)}): {', '.join(today_tokens)}")

    log.info(
        f"India ORB Bot | OR: first {INDIA_ORB_RANGE_BARS}×5-min bars (09:15–09:45 IST)"
        f" | ₹{INDIA_POSITION_SIZE_INR:,}/trade"
        f" | Entry window: 09:45–{INDIA_MAX_ENTRY_HOUR}:{INDIA_MAX_ENTRY_MINUTE:02d} IST"
        f" | EOD close: {INDIA_CLOSE_HOUR}:{INDIA_CLOSE_MINUTE:02d} IST"
    )
    log.info(
        f"Filters: Nifty trend | min OR {INDIA_ORB_MIN_OR_PCT:.1%}"
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
                break

            run_india_orb()
            log.info(f"Cycle complete — next check in {args.interval // 60}m")
            _time.sleep(args.interval)
