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
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pyotp
import requests
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
        self._access_token: str = ""
        self._feed_token: str = ""
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
            data = resp.get("data", {})
            # Store JWT and feed tokens — JWT for REST calls, feed token for WebSocket
            self._access_token = data.get("jwtToken", "")
            self._feed_token   = data.get("feedToken", "")
            log.info(f"AngelOne connected — client: {self._client_code}")
            return True
        except Exception as e:
            log.error(f"AngelOne connect error: {e}")
            return False

    @property
    def feed_token(self) -> str:
        return self._feed_token

    def _ensure_connected(self) -> None:
        if self._obj is None:
            raise RuntimeError("AngelOneClient not connected — call connect() first")

    # ------------------------------------------------------------------
    # Symbol resolution: symbol → SmartAPI token
    # ------------------------------------------------------------------

    def resolve_token(self, symbol: str, exchange: str = "NSE") -> str | None:
        """
        Return the SmartAPI symboltoken for an NSE equity symbol.
        Checks INDIA_TOKEN_MAP first (zero API calls for known symbols),
        then falls back to searchScrip with EQ-preference and caching.
        """
        from config import INDIA_TOKEN_MAP
        if symbol in INDIA_TOKEN_MAP:
            return INDIA_TOKEN_MAP[symbol]

        cache_key = f"{exchange}:{symbol}"
        if cache_key in self._token_cache:
            return self._token_cache[cache_key]

        self._ensure_connected()
        for query in (f"{symbol}-EQ", symbol):
            try:
                resp = self._obj.searchScrip(exchange, query)
                hits = resp.get("data") or []
                if hits:
                    # Prefer exact SYMBOL-EQ equity match; fall back to first result
                    eq_name = f"{symbol}-EQ"
                    hit = next((h for h in hits if h.get("tradingsymbol") == eq_name), hits[0])
                    token = str(hit["symboltoken"])
                    self._token_cache[cache_key] = token
                    log.debug(f"Token resolved: {symbol} → {token} ({hit['tradingsymbol']})")
                    return token
            except Exception:
                pass
            time.sleep(0.3)

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
        """
        # Use hardcoded stable token directly — searchScrip always fails for "Nifty 50"
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
            "producttype": "INTRADAY",
            "duration": "DAY",
            "price": "0",
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(qty),
            "scripconsent": "yes",   # required for scrips under ASM/GSM surveillance
        }
        try:
            order_id = self._obj.placeOrder(params)
            log.info(f"{side} {qty} {symbol} — order_id: {order_id}")
            return str(order_id)
        except Exception as e:
            log.error(f"{symbol}: order placement failed ({side} × {qty}) — {e}")
            return None

    def place_sl_order(
        self,
        symbol: str,
        token: str,
        side: str,
        qty: int,
        trigger_price: float,
        exchange: str = "NSE",
    ) -> str | None:
        """
        Place an exchange-level STOPLOSS_MARKET order for an INTRADAY position.
        Fires at the exchange the instant trigger_price is touched — no polling lag.

        For a LONG position  → side="SELL", trigger_price = OR low * (1 - buffer)
        For a SHORT position → side="BUY",  trigger_price = OR high * (1 + buffer)

        Returns the SL order ID (store it to cancel later when target is hit or EOD).
        GTT is NOT used because AngelOne GTT only supports DELIVERY/MARGIN, not INTRADAY.
        """
        self._ensure_connected()
        params = {
            "variety": "STOPLOSS",           # AngelOne docs: STOPLOSS variety = stop loss order
            "tradingsymbol": self._eq_symbol(symbol),
            "symboltoken": token,
            "transactiontype": side.upper(),
            "exchange": exchange,
            "ordertype": "STOPLOSS_MARKET",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "price": "0",
            "triggerprice": str(round(trigger_price, 2)),
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(qty),
            "scripconsent": "yes",           # required for scrips under ASM/GSM surveillance
        }
        try:
            order_id = self._obj.placeOrder(params)
            log.info(
                f"SL order placed — {side} {qty} {symbol}"
                f" trigger ₹{trigger_price:.2f} → order_id: {order_id}"
            )
            return str(order_id)
        except Exception as e:
            log.error(f"{symbol}: SL order placement failed ({side} × {qty} @ ₹{trigger_price:.2f}) — {e}")
            return None

    def cancel_order(self, order_id: str, variety: str = "STOPLOSS") -> bool:
        """
        Cancel a pending order (typically the SL order when target is hit or EOD).
        Returns True if the cancel was accepted.
        """
        self._ensure_connected()
        try:
            self._obj.cancelOrder(order_id, variety)
            log.info(f"Order cancelled — id: {order_id}")
            return True
        except Exception as e:
            log.warning(f"Cancel order {order_id} failed — {e}")
            return False

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

    def get_position(self, symbol: str, positions: list[dict] | None = None) -> dict | None:
        """
        Return the open position for a specific symbol, or None.
        Pass a pre-fetched positions list to avoid an extra API call (1/s rate limit).
        """
        eq_sym = self._eq_symbol(symbol)
        pool = positions if positions is not None else self.get_positions()
        for pos in pool:
            if pos.get("tradingsymbol") == eq_sym:
                return pos
        return None

    def get_order_book(self) -> list[dict]:
        """Fetch today's full order book. Call once per cycle and pass the result
        to already_traded_today() to avoid repeated orderBook API calls (1/s limit)."""
        self._ensure_connected()
        try:
            resp = self._obj.orderBook()
            return resp.get("data") or []
        except Exception as e:
            log.error(f"get_order_book failed: {e}")
            return []

    def get_available_funds_inr(self) -> float:
        """Return available cash balance in INR."""
        self._ensure_connected()
        try:
            resp = self._obj.rmsLimit()
            return float(resp["data"].get("net", 0))
        except Exception as e:
            log.error(f"get_funds failed: {e}")
            return 0.0

    def get_batch_quote(
        self,
        token_map: dict[str, str],
        exchange: str = "NSE",
        mode: str = "FULL",
    ) -> dict[str, dict]:
        """
        Fetch LTP + OHLC + volume for up to 50 symbols in ONE API call.
        Returns {symbol: {ltp, open, high, low, close, volume}} or {} on failure.

        Rate limit: 1 req/s per docs. 50 tokens per request.
        Use this instead of per-symbol getCandleData for current-price scanning.
        """
        self._ensure_connected()
        if not token_map:
            return {}

        token_to_sym = {tok: sym for sym, tok in token_map.items()}
        tokens = list(token_map.values())

        headers = {
            "Authorization":    f"Bearer {self._access_token}",
            "Content-Type":     "application/json",
            "Accept":           "application/json",
            "X-UserType":       "USER",
            "X-SourceID":       "WEB",
            "X-PrivateKey":     self._api_key,
            "X-ClientLocalIP":  "127.0.0.1",
            "X-ClientPublicIP": "127.0.0.1",
            "X-MACAddress":     "00:00:00:00:00:00",
        }
        payload = {"mode": mode, "exchangeTokens": {exchange: tokens}}
        try:
            r = requests.post(
                "https://apiconnect.angelone.in/rest/secure/angelbroking/market/v1/quote/",
                headers=headers,
                json=payload,
                timeout=10,
            )
            result = r.json()
            if not result.get("status"):
                log.warning(f"get_batch_quote: {result.get('message', 'unknown error')}")
                return {}

            out: dict[str, dict] = {}
            for item in result.get("data", {}).get("fetched", []):
                tok = str(item.get("symbolToken", ""))
                sym = token_to_sym.get(tok)
                if sym:
                    out[sym] = {
                        "ltp":    float(item.get("ltp",         0)),
                        "open":   float(item.get("open",        0)),
                        "high":   float(item.get("high",        0)),
                        "low":    float(item.get("low",         0)),
                        "close":  float(item.get("close",       0)),
                        "volume": int(  item.get("tradeVolume", 0)),
                    }
            unfetched = result.get("data", {}).get("unfetched", [])
            if unfetched:
                log.warning(f"get_batch_quote: {len(unfetched)} symbols unfetched")
            return out
        except Exception as e:
            log.error(f"get_batch_quote failed: {e}")
            return {}

    def already_traded_today(self, symbol: str, orders: list[dict] | None = None) -> bool:
        """
        True if any completed entry order exists today for this symbol.
        Pass pre-fetched orders list (from get_order_book()) to avoid repeated
        orderBook API calls — the limit is 1/s and we check up to 15 symbols per cycle.
        """
        self._ensure_connected()
        eq_sym = self._eq_symbol(symbol)
        if orders is None:
            orders = self.get_order_book()
        for o in orders:
            if (
                o.get("tradingsymbol") == eq_sym
                and o.get("status", "").lower() in ("complete", "filled")
                and o.get("variety", "").upper() == "NORMAL"   # entry orders only, not SL
            ):
                return True
        return False

    def close_all_positions(self) -> None:
        """Market-close all open MIS positions at EOD — handles both longs and shorts."""
        positions = self.get_positions()
        for pos in positions:
            qty = int(pos.get("netqty", 0))
            sym = pos.get("tradingsymbol", "")
            token = pos.get("symboltoken", "")
            if qty == 0 or not sym or not token:
                continue
            base_sym = sym.replace("-EQ", "").replace("-BE", "")
            close_side = "SELL" if qty > 0 else "BUY"
            abs_qty = abs(qty)
            order_id = self.place_market_order(base_sym, token, close_side, abs_qty)
            if order_id:
                log.info(f"EOD CLOSE — {close_side} {abs_qty} {sym} (order {order_id})")
            else:
                log.error(f"EOD CLOSE FAILED — {sym} × {abs_qty}")
