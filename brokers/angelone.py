"""
AngelOne SmartAPI client adapter.

IMPORTANT: AngelOne SmartAPI connects to a REAL brokerage account —
there is no paper trading mode. Use --dry-run in india_orb_bot.py
to test without placing real orders.

Environment variables required (add to .env):
    ANGELONE_API_KEY      — from SmartAPI developer console
    ANGELONE_CLIENT_CODE  — your AngelOne login ID
    ANGELONE_PASSWORD     — your AngelOne trading password (PIN)
    ANGELONE_TOTP_SECRET  — Base32 TOTP secret from AngelOne app setup

Authentication uses TOTP (time-based OTP), so system clock must be accurate.
The JWT session token is valid until midnight — no need to re-auth mid-day.
"""
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pyotp
from SmartApi import SmartConnect

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

# SmartAPI candle interval codes
INTERVAL_5MIN = "FIVE_MINUTE"
INTERVAL_1MIN = "ONE_MINUTE"
INTERVAL_15MIN = "FIFTEEN_MINUTE"
INTERVAL_1HOUR = "ONE_HOUR"
INTERVAL_1DAY = "ONE_DAY"

# Nifty 50 index on SmartAPI (NSE)
# Token 99926000 is the well-known stable token for the Nifty 50 index.
# get_nifty_trend() tries searchScrip first and falls back to this constant.
_NIFTY50_FALLBACK_TOKEN = "99926000"
_NIFTY50_SEARCH_TERM    = "Nifty 50"


class AngelOneClient:
    """
    Thin wrapper around SmartConnect that handles auth, candles, and orders.
    Call connect() once at bot startup; the session is valid for the trading day.
    """

    def __init__(self):
        self._api_key     = os.environ["ANGELONE_API_KEY"]
        self._client_code = os.environ["ANGELONE_CLIENT_CODE"]
        self._password    = os.environ["ANGELONE_PASSWORD"]
        self._totp_secret = os.environ["ANGELONE_TOTP_SECRET"]
        self._obj: SmartConnect | None = None
        self._token_cache: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Authenticate and create a session. Returns True on success."""
        try:
            obj = SmartConnect(api_key=self._api_key)
            totp = pyotp.TOTP(self._totp_secret).now()
            resp = obj.generateSession(self._client_code, self._password, totp)
            if not resp.get("status"):
                log.error(f"AngelOne auth failed: {resp.get('message', 'unknown error')}")
                return False
            self._obj = obj
            log.info(f"AngelOne connected — client: {self._client_code}")
            return True
        except Exception as e:
            log.error(f"AngelOne connect error: {e}")
            return False

    def _ensure_connected(self) -> None:
        if self._obj is None:
            raise RuntimeError("AngelOneClient not connected — call connect() first")

    # ------------------------------------------------------------------
    # Symbol resolution: symbol → SmartAPI token
    # ------------------------------------------------------------------

    def resolve_token(self, symbol: str, exchange: str = "NSE") -> str | None:
        """
        Return the SmartAPI symboltoken for an NSE equity symbol.
        Results are cached so each symbol is resolved only once per session.
        Tries '<symbol>-EQ' first (NSE equity series), then bare symbol.
        """
        cache_key = f"{exchange}:{symbol}"
        if cache_key in self._token_cache:
            return self._token_cache[cache_key]

        self._ensure_connected()
        for query in (f"{symbol}-EQ", symbol):
            try:
                resp = self._obj.searchScrip(exchange, query)
                hits = resp.get("data") or []
                if hits:
                    token = str(hits[0]["symboltoken"])
                    self._token_cache[cache_key] = token
                    log.debug(f"Token resolved: {symbol} → {token} ({hits[0]['tradingsymbol']})")
                    return token
            except Exception:
                pass

        log.warning(f"Could not resolve SmartAPI token for {symbol}")
        return None

    def _eq_symbol(self, symbol: str) -> str:
        """Return 'SYMBOL-EQ' for NSE equity order placement."""
        if symbol.endswith("-EQ") or symbol.endswith("-BE") or symbol.endswith("-SM"):
            return symbol
        return f"{symbol}-EQ"

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    def get_ltp(self, symbol: str, token: str, exchange: str = "NSE") -> float | None:
        """Last Traded Price (real-time)."""
        self._ensure_connected()
        try:
            resp = self._obj.ltpData(exchange, self._eq_symbol(symbol), token)
            return float(resp["data"]["ltp"])
        except Exception as e:
            log.error(f"{symbol}: LTP fetch failed — {e}")
            return None

    def get_today_candles(
        self,
        symbol: str,
        token: str,
        exchange: str = "NSE",
        interval: str = INTERVAL_5MIN,
    ) -> pd.DataFrame | None:
        """
        Fetch all 5-min OHLCV bars for today's NSE session (9:15 AM → now).
        Returns a DataFrame indexed by IST datetime or None on failure.
        """
        self._ensure_connected()
        today = datetime.now(IST).date()
        from_str = f"{today} 09:15"
        to_str   = datetime.now(IST).strftime("%Y-%m-%d %H:%M")

        params = {
            "exchange": exchange,
            "symboltoken": token,
            "interval": interval,
            "fromdate": from_str,
            "todate": to_str,
        }
        try:
            resp = self._obj.getCandleData(params)
            rows = resp.get("data") or []
            if not rows:
                return None
            df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_convert(IST)
            df = df.set_index("timestamp")
            df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]].astype(float)
            df["volume"] = df["volume"].astype(int)
            return df if not df.empty else None
        except Exception as e:
            log.error(f"{symbol}: candle fetch failed — {e}")
            return None

    def get_nifty_trend(self) -> tuple[bool | None, float]:
        """
        Return (is_up, pct_change) for Nifty 50 today.
        Uses today's first bar open vs. latest bar close.
        Returns (None, 0.0) if data is unavailable.

        Resolves the Nifty 50 token dynamically via searchScrip so it stays
        correct even if AngelOne changes the well-known token (99926000).
        """
        # Try to resolve token via search; fall back to stable hardcoded token
        token = self.resolve_token(_NIFTY50_SEARCH_TERM, exchange="NSE")
        if token is None:
            log.debug(f"Nifty50 token search failed — using fallback {_NIFTY50_FALLBACK_TOKEN}")
            token = _NIFTY50_FALLBACK_TOKEN

        df = self.get_today_candles(_NIFTY50_SEARCH_TERM, token, exchange="NSE")
        if df is None or df.empty:
            return None, 0.0
        open_price = float(df.iloc[0]["open"])
        last_price = float(df.iloc[-1]["close"])
        pct = (last_price - open_price) / open_price if open_price > 0 else 0.0
        is_up = last_price >= open_price
        log.info(
            f"Nifty50 trend: open={open_price:.2f}  last={last_price:.2f}"
            f"  {'UP' if is_up else 'DOWN'}  ({pct:+.2%})"
        )
        return is_up, pct

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def place_market_order(
        self,
        symbol: str,
        token: str,
        side: str,
        qty: int,
        exchange: str = "NSE",
    ) -> str | None:
        """
        Place a MIS (intraday) market order. Returns order ID or None on failure.
        side: "BUY" or "SELL"
        """
        self._ensure_connected()
        params = {
            "variety": "NORMAL",
            "tradingsymbol": self._eq_symbol(symbol),
            "symboltoken": token,
            "transactiontype": side.upper(),
            "exchange": exchange,
            "ordertype": "MARKET",
            "producttype": "INTRADAY",  # SmartAPI term for intraday; auto-squared at 15:20 IST
            "duration": "DAY",
            "price": "0",
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(qty),
        }
        try:
            order_id = self._obj.placeOrder(params)
            log.info(f"{side} {qty} {symbol} — order_id: {order_id}")
            return str(order_id)
        except Exception as e:
            log.error(f"{symbol}: order placement failed ({side} × {qty}) — {e}")
            return None

    # ------------------------------------------------------------------
    # Positions & account
    # ------------------------------------------------------------------

    def get_positions(self) -> list[dict]:
        """Return list of open INTRADAY positions (netqty != 0)."""
        self._ensure_connected()
        try:
            resp = self._obj.position()
            all_pos = resp.get("data") or []
            return [
                p for p in all_pos
                if int(p.get("netqty", 0)) != 0
                and p.get("producttype", "").upper() in ("INTRADAY", "MIS")
            ]
        except Exception as e:
            log.error(f"get_positions failed: {e}")
            return []

    def get_position(self, symbol: str) -> dict | None:
        """Return the open position for a specific symbol, or None."""
        eq_sym = self._eq_symbol(symbol)
        for pos in self.get_positions():
            if pos.get("tradingsymbol") == eq_sym:
                return pos
        return None

    def get_available_funds_inr(self) -> float:
        """Return available cash balance in INR."""
        self._ensure_connected()
        try:
            resp = self._obj.rmsLimit()
            return float(resp["data"].get("net", 0))
        except Exception as e:
            log.error(f"get_funds failed: {e}")
            return 0.0

    def already_traded_today(self, symbol: str) -> bool:
        """True if a completed BUY order exists for this symbol today."""
        self._ensure_connected()
        eq_sym = self._eq_symbol(symbol)
        try:
            resp = self._obj.orderBook()
            orders = resp.get("data") or []
            for o in orders:
                if (
                    o.get("tradingsymbol") == eq_sym
                    and o.get("transactiontype", "").upper() == "BUY"
                    and o.get("status", "").lower() in ("complete", "filled")
                ):
                    return True
            return False
        except Exception as e:
            log.warning(f"{symbol}: order history check failed — {e}")
            return False

    def close_all_positions(self) -> None:
        """Market-sell all open MIS positions (EOD sweep)."""
        positions = self.get_positions()
        for pos in positions:
            qty = int(pos.get("netqty", 0))
            sym = pos.get("tradingsymbol", "")
            token = pos.get("symboltoken", "")
            if qty > 0 and sym and token:
                # Strip -EQ suffix for our place_market_order call
                base_sym = sym.replace("-EQ", "").replace("-BE", "")
                order_id = self.place_market_order(base_sym, token, "SELL", qty)
                if order_id:
                    log.info(f"EOD CLOSE — SELL {qty} {sym} (order {order_id})")
                else:
                    log.error(f"EOD CLOSE FAILED — {sym} × {qty}")
