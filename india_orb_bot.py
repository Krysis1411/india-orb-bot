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
import logging
import os
import time as _time
from datetime import datetime, time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from brokers.angelone import AngelOneClient
from config import (
    INDIA_CLOSE_HOUR,
    INDIA_CLOSE_MINUTE,
    INDIA_DAILY_LOSS_LIMIT_PCT,
    INDIA_MAX_ENTRY_HOUR,
    INDIA_MAX_ENTRY_MINUTE,
    INDIA_MAX_TOTAL_INR,
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
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

    if INDIA_ORB_MIN_OR_PCT > 0 and or_range / or_high < INDIA_ORB_MIN_OR_PCT:
        log.info(
            f"{symbol}: OR too narrow"
            f" ({or_range / or_high:.3%} < {INDIA_ORB_MIN_OR_PCT:.1%}) — skipping"
        )
        return False

    current_bar    = bars.iloc[-1]
    current_price  = float(current_bar["close"])
    current_volume = float(current_bar["volume"])
    now            = now_ist()

    stop_price   = or_low * (1 - INDIA_ORB_STOP_BUFFER_PCT)
    target_price = or_high + or_range * INDIA_ORB_PROFIT_MULTIPLIER
    trade_qty    = max(1, int(INDIA_POSITION_SIZE_INR / current_price))

    log.info(
        f"{symbol} | ₹{current_price:.2f} | OR: ₹{or_low:.2f}–₹{or_high:.2f}"
        f" | Qty: {trade_qty} | Time: {now.strftime('%H:%M')} IST"
    )

    # --- EOD forced close ---
    if now.time() >= CLOSE_TIME_IST:
        pos = client.get_position(symbol)
        if pos:
            qty = int(pos["netqty"])
            if qty > 0:
                if DRY_RUN:
                    log.info(f"[DRY RUN] EOD CLOSE — SELL {qty} {symbol} at ~₹{current_price:.2f}")
                else:
                    client.place_market_order(symbol, token, "SELL", qty)
                    log.info(f"EOD CLOSE — SELL {qty} {symbol} at ~₹{current_price:.2f}")
        return False

    # --- Manage existing position ---
    pos = client.get_position(symbol)
    if pos:
        qty       = int(pos["netqty"])
        avg_price = float(pos.get("averageprice", current_price))
        pnl_pct   = (current_price - avg_price) / avg_price * 100

        if current_price <= stop_price:
            if DRY_RUN:
                log.info(f"[DRY RUN] STOP LOSS — SELL {qty} {symbol} at ~₹{current_price:.2f} | P&L: {pnl_pct:+.2f}%")
            else:
                client.place_market_order(symbol, token, "SELL", qty)
                log.info(f"STOP LOSS — SELL {qty} {symbol} at ~₹{current_price:.2f} | P&L: {pnl_pct:+.2f}%")
            return False

        if current_price >= target_price:
            if DRY_RUN:
                log.info(f"[DRY RUN] TAKE PROFIT — SELL {qty} {symbol} at ~₹{current_price:.2f} | P&L: {pnl_pct:+.2f}%")
            else:
                client.place_market_order(symbol, token, "SELL", qty)
                log.info(f"TAKE PROFIT — SELL {qty} {symbol} at ~₹{current_price:.2f} | P&L: {pnl_pct:+.2f}%")
            return False

        log.info(
            f"{symbol} | Holding {qty} shares @ ₹{avg_price:.2f}"
            f" | P&L: {pnl_pct:+.2f}%"
            f" | Stop: ₹{stop_price:.2f} | Target: ₹{target_price:.2f}"
        )
        return False

    # --- Entry gate checks ---

    # No entries after cutoff time
    if now.time() >= ENTRY_CUTOFF_IST:
        log.info(f"{symbol}: past entry cutoff ({INDIA_MAX_ENTRY_HOUR}:{INDIA_MAX_ENTRY_MINUTE:02d} IST) — skipping")
        return False

    # Monday skip
    if INDIA_SKIP_MONDAY_ENTRIES and now.weekday() == 0:
        log.info(f"{symbol}: Monday — skipping new entries")
        return False

    if client.already_traded_today(symbol):
        log.info(f"{symbol}: already traded today — skipping")
        return False

    # Nifty trend filter
    if nifty_up is False:
        log.info(f"{symbol}: Nifty trending DOWN — skipping long entry")
        return False
    if nifty_up is None:
        log.info(f"{symbol}: Nifty data not ready — skipping entry")
        return False

    vol_ok = current_volume >= avg_or_volume * INDIA_ORB_VOLUME_FACTOR

    # Already shot past target — skip stale breakout
    if current_price >= target_price:
        log.info(f"{symbol}: price already past target ₹{target_price:.2f} — skipping stale breakout")
        return False

    # --- Breakout entry ---
    if current_price > or_high and vol_ok:
        if open_positions_count >= max_open_positions:
            log.info(
                f"{symbol}: budget limit reached"
                f" ({open_positions_count}/{max_open_positions} positions open) — skipping"
            )
            return False

        if DRY_RUN:
            log.info(
                f"[DRY RUN] BUY {trade_qty} {symbol}"
                f" | ₹{current_price:.2f} | Cost: ~₹{trade_qty * current_price:,.0f}"
                f" | Stop: ₹{stop_price:.2f} | Target: ₹{target_price:.2f}"
                f" | Vol: {current_volume / avg_or_volume:.1f}×"
            )
            return True

        order_id = client.place_market_order(symbol, token, "BUY", trade_qty)
        if order_id:
            log.info(
                f"BUY BREAKOUT {trade_qty} {symbol}"
                f" | ₹{current_price:.2f} | Cost: ~₹{trade_qty * current_price:,.0f}"
                f" | Stop: ₹{stop_price:.2f} | Target: ₹{target_price:.2f}"
                f" | Vol: {current_volume / avg_or_volume:.1f}×"
            )
            return True
        return False

    elif current_price > or_high and not vol_ok:
        log.info(
            f"{symbol}: price above OR high but volume too low"
            f" ({current_volume / avg_or_volume:.2f}× < {INDIA_ORB_VOLUME_FACTOR}×) — skipping"
        )
        return False
    else:
        log.info(f"{symbol}: no breakout (₹{current_price:.2f} vs OR high ₹{or_high:.2f})")
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
        # AngelOne doesn't expose yesterday's equity directly; use a simple absolute cap check.
        # For a proper daily-loss check, compare against a session-start snapshot stored externally.
        if funds <= 0:
            log.warning("Zero available funds — blocking new entries")
            _block_new_entries = True
    except Exception:
        pass

    # --- EOD sweep: close everything if past close time ---
    if now.time() >= CLOSE_TIME_IST:
        log.info(f"EOD — force-closing all open MIS positions")
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

    # --- Manage all currently held positions first ---
    try:
        open_positions = client.get_positions()
    except Exception as e:
        log.error(f"Failed to fetch positions: {e}")
        open_positions = []

    # Pre-resolve tokens for held positions
    held_symbols: dict[str, str] = {}
    for pos in open_positions:
        raw_sym = pos.get("tradingsymbol", "").replace("-EQ", "")
        token   = pos.get("symboltoken", "")
        if raw_sym and token:
            held_symbols[raw_sym] = token

    for sym, tok in held_symbols.items():
        try:
            process_symbol(sym, tok, nifty_up, len(open_positions), 0)
        except Exception as e:
            log.error(f"{sym}: unexpected error managing position — {e}")

    open_count     = len(open_positions)
    max_positions  = max(1, int(INDIA_MAX_TOTAL_INR / INDIA_POSITION_SIZE_INR))

    # --- Screen for new entry opportunities ---
    if _block_new_entries:
        log.warning("Blocking new entries — risk limit active")
        return

    active_symbols = get_active_nse_symbols()
    if not active_symbols:
        log.warning("NSE screener returned no symbols — skipping new entries")
        return

    for symbol in active_symbols:
        if symbol in held_symbols:
            continue  # already managed above
        token = client.resolve_token(symbol)
        if token is None:
            log.warning(f"{symbol}: skipping — could not resolve SmartAPI token")
            continue
        try:
            opened = process_symbol(symbol, token, nifty_up, open_count, max_positions)
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
