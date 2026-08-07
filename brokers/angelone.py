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
from collections import defaultdict
from datetime import date, datetime
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

# Index spot tokens for NFO underlyings (NSE segment, AMXIDX instrument type).
# These are the AngelOne-migrated 99926xxx tokens -- the older 26000/26009
# tokens are deprecated. Verified against SmartAPI forum docs, 2026-07-31.
INDEX_TOKENS = {
    "NIFTY":     "99926000",
    "BANKNIFTY": "99926009",
}


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
        # Populated by _load_scrip_master() inside connect()
        # Maps base symbol (e.g. "RELIANCE") → token for NSE equity (-EQ) instruments
        self._scrip_master: dict[str, str] = {}
        # Populated by _load_scrip_master() inside connect()
        # Maps underlying name (e.g. "NIFTY") → list of NFO OPTIDX contract dicts:
        # {token, symbol, expiry (date), strike (float, rupees), opt_type ("CE"/"PE"), lotsize}
        self._option_chain: dict[str, list[dict]] = {}
        # Populated by _load_scrip_master() inside connect()
        # Maps underlying name → list of NFO FUTIDX contract dicts: {token, symbol, expiry, lotsize}
        self._futures_chain: dict[str, list[dict]] = {}

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
            # Download full instrument list — eliminates searchScrip calls at startup
            self._scrip_master = self._load_scrip_master()
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
    # Instrument master (downloaded once at connect)
    # ------------------------------------------------------------------

    _SCRIP_MASTER_URL = (
        "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
    )

    # Index underlyings we resolve NFO futures for (used only as a volume
    # proxy -- see get_near_month_future). Kept narrow on purpose: FUTIDX
    # only exists for indices, stocks already have real traded volume on
    # their own equity bars so they never need this.
    _OPTION_UNDERLYINGS = ("NIFTY", "BANKNIFTY")

    def _load_scrip_master(self) -> dict[str, str]:
        """
        Download the full instrument list ONCE and build:
          1. {base_symbol: token} for NSE equity instruments (exch_seg="NSE",
             symbol ending in -EQ) -- returned, stored as self._scrip_master.
          2. self._option_chain: {underlying: [contract dicts]} for NFO
             option rows -- OPTIDX (index options, NIFTY/BANKNIFTY only) AND
             OPTSTK (single-stock options, ANY name with listed contracts --
             ~180 F&O-enabled stocks, verified live 2026-08-07: same
             monthly/last-Tuesday expiry convention as BANKNIFTY, American
             exercise). Unlike the index case, no name whitelist is applied
             for OPTSTK -- the file only has entries for names that actually
             have listed options, so there's nothing to filter.
          3. self._futures_chain: FUTIDX only (index volume-proxy futures).

        exch_seg is "NSE" (verified against the live file 2026-07-30, 2434
        matching rows) -- NOT "nse_cm", which never matches any row and
        silently produced an empty map, forcing every symbol through the
        1/s-limited searchScrip fallback instead of this fast local lookup.
        """
        try:
            resp = requests.get(self._SCRIP_MASTER_URL, timeout=30)
            resp.raise_for_status()
            instruments = resp.json()
            out: dict[str, str] = {}
            options: dict[str, list[dict]] = defaultdict(list)
            futures: dict[str, list[dict]] = {u: [] for u in self._OPTION_UNDERLYINGS}
            for item in instruments:
                exch = item.get("exch_seg")
                if exch == "NSE":
                    sym = item.get("symbol", "")
                    if sym.endswith("-EQ"):
                        out[sym[:-3]] = str(item["token"])
                    continue
                if exch != "NFO":
                    continue
                itype = item.get("instrumenttype")
                if itype in ("OPTIDX", "OPTSTK"):
                    name = item.get("name", "")
                    if not name:
                        continue
                    try:
                        expiry = datetime.strptime(item["expiry"], "%d%b%Y").date()
                        strike = float(item["strike"]) / 100.0
                    except (KeyError, ValueError):
                        continue
                    sym = item.get("symbol", "")
                    opt_type = "CE" if sym.endswith("CE") else "PE" if sym.endswith("PE") else None
                    if opt_type is None:
                        continue
                    options[name].append({
                        "token": str(item["token"]),
                        "symbol": sym,
                        "expiry": expiry,
                        "strike": strike,
                        "opt_type": opt_type,
                        "lotsize": int(float(item.get("lotsize", 0))),
                    })
                elif itype == "FUTIDX":
                    name = item.get("name", "")
                    if name not in futures:
                        continue
                    try:
                        expiry = datetime.strptime(item["expiry"], "%d%b%Y").date()
                    except (KeyError, ValueError):
                        continue
                    futures[name].append({
                        "token": str(item["token"]),
                        "symbol": item.get("symbol", ""),
                        "expiry": expiry,
                        "lotsize": int(float(item.get("lotsize", 0))),
                    })
            self._option_chain = dict(options)
            self._futures_chain = {u: sorted(v, key=lambda r: r["expiry"]) for u, v in futures.items()}
            n_opts = sum(len(v) for v in options.values())
            n_futs = sum(len(v) for v in futures.values())
            n_stocks_with_options = sum(1 for k in options if k not in self._OPTION_UNDERLYINGS)
            log.info(
                f"ScripMaster loaded — {len(out)} NSE equity tokens, "
                f"{n_opts} NFO option contracts across {n_stocks_with_options} stocks + "
                f"{', '.join(self._OPTION_UNDERLYINGS)}, {n_futs} NFO index-future contracts"
            )
            return out
        except Exception as e:
            log.warning(f"ScripMaster load failed ({e}) — will fall back to searchScrip")
            return {}

    # ------------------------------------------------------------------
    # Index & option-chain resolution -- get_index_candles/get_near_month_future
    # are index-only (NIFTY/BANKNIFTY); get_option_expiries/get_option_lot_size/
    # get_strike_interval/pick_strike/resolve_option work for ANY underlying
    # with listed NFO options, index or single-stock alike.
    # ------------------------------------------------------------------

    def get_index_candles(
        self,
        underlying: str,
        interval: str = INTERVAL_5MIN,
    ) -> pd.DataFrame | None:
        """Today's OHLCV bars for the NIFTY/BANKNIFTY spot index (NSE segment)."""
        token = INDEX_TOKENS.get(underlying)
        if token is None:
            log.error(f"No index token configured for {underlying}")
            return None
        return self.get_today_candles(underlying, token, exchange="NSE", interval=interval)

    def get_option_expiries(self, underlying: str) -> list[date]:
        """Sorted list of distinct expiry dates currently listed for underlying."""
        rows = self._option_chain.get(underlying, [])
        return sorted({r["expiry"] for r in rows})

    def get_option_lot_size(self, underlying: str) -> int | None:
        """Current NFO lot size for underlying, read live from ScripMaster (not hardcoded --
        NSE revises lot sizes every few months by circular)."""
        rows = self._option_chain.get(underlying, [])
        return rows[0]["lotsize"] if rows else None

    def get_strike_interval(self, underlying: str, expiry: date) -> float | None:
        """Infer the strike spacing for a given expiry from the actual listed strikes
        (not hardcoded -- NSE has changed strike schemes multiple times, e.g. the
        Nov 2025 monthly-strike-interval revision)."""
        strikes = sorted({r["strike"] for r in self._option_chain.get(underlying, []) if r["expiry"] == expiry})
        if len(strikes) < 2:
            return None
        diffs = [round(b - a, 2) for a, b in zip(strikes, strikes[1:])]
        return min(diffs)

    def pick_strike(
        self,
        underlying: str,
        expiry: date,
        spot: float,
        offset_steps: int = 0,
    ) -> float | None:
        """
        Return the strike nearest to `spot`, offset by `offset_steps` strike
        intervals (positive = further OTM for calls / further ITM for puts,
        i.e. higher strikes; negative = lower strikes).
        offset_steps=0 → ATM.
        """
        strikes = sorted({r["strike"] for r in self._option_chain.get(underlying, []) if r["expiry"] == expiry})
        if not strikes:
            return None
        atm = min(strikes, key=lambda s: abs(s - spot))
        idx = strikes.index(atm) + offset_steps
        idx = max(0, min(idx, len(strikes) - 1))
        return strikes[idx]

    def get_near_month_future(self, underlying: str) -> dict | None:
        """Return the nearest-expiry NFO futures contract dict for underlying,
        or None. Used as a real-volume proxy for the spot index, which itself
        always reports zero traded volume (it's a computed value, not a
        traded instrument -- only its futures/options are)."""
        rows = self._futures_chain.get(underlying, [])
        return rows[0] if rows else None

    def resolve_option(
        self,
        underlying: str,
        expiry: date,
        strike: float,
        opt_type: str,
    ) -> dict | None:
        """Return the contract dict (token, symbol, lotsize, ...) for an exact
        underlying/expiry/strike/CE-or-PE combination, or None if not listed."""
        for r in self._option_chain.get(underlying, []):
            if r["expiry"] == expiry and abs(r["strike"] - strike) < 0.01 and r["opt_type"] == opt_type.upper():
                return r
        return None

    def get_nse_intraday_symbols(self) -> set[str]:
        """
        Return the set of NSE symbol names approved for intraday (MIS) trading.
        Use this to pre-filter the watchlist before token resolution.
        Returns an empty set on failure (caller should allow all symbols through).
        """
        self._ensure_connected()
        headers = self._rest_headers()
        try:
            resp = requests.get(
                "https://apiconnect.angelone.in/rest/secure/angelbroking/marketData/v1/nseIntraday",
                headers=headers,
                timeout=10,
            )
            data = resp.json()
            if not data.get("status"):
                log.warning(f"nseIntraday: {data.get('message', 'unknown error')}")
                return set()
            symbols = {item["SymbolName"] for item in data.get("data", [])}
            log.info(f"NSE intraday allowed: {len(symbols)} symbols")
            return symbols
        except Exception as e:
            log.warning(f"nseIntraday fetch failed ({e}) — no intraday filter applied")
            return set()

    def _rest_headers(self) -> dict:
        """Standard headers required for direct REST calls to AngelOne APIs."""
        return {
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

    # ------------------------------------------------------------------
    # Symbol resolution: symbol → SmartAPI token
    # ------------------------------------------------------------------

    def resolve_token(self, symbol: str, exchange: str = "NSE") -> str | None:
        """
        Return the SmartAPI symboltoken for an NSE equity symbol.
        Priority:
          1. ScripMaster (local, downloaded at connect — zero API calls)
          2. INDIA_TOKEN_MAP in config (hardcoded fallback)
          3. searchScrip API (last resort, 1/s rate limit applies)
        """
        # ScripMaster covers all NSE equities — the fast path for 99% of symbols
        if symbol in self._scrip_master:
            return self._scrip_master[symbol]

        from config import INDIA_TOKEN_MAP
        if symbol in INDIA_TOKEN_MAP:
            return INDIA_TOKEN_MAP[symbol]

        cache_key = f"{exchange}:{symbol}"
        if cache_key in self._token_cache:
            return self._token_cache[cache_key]

        # searchScrip fallback — only reached for symbols missing from ScripMaster
        self._ensure_connected()
        for query in (f"{symbol}-EQ", symbol):
            try:
                resp = self._obj.searchScrip(exchange, query)
                hits = resp.get("data") or []
                if hits:
                    eq_name = f"{symbol}-EQ"
                    hit = next((h for h in hits if h.get("tradingsymbol") == eq_name), hits[0])
                    token = str(hit["symboltoken"])
                    self._token_cache[cache_key] = token
                    log.debug(f"Token resolved via searchScrip: {symbol} → {token}")
                    return token
            except Exception:
                pass
            time.sleep(1.1)   # searchScrip: 1/s rate limit

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
        """Last Traded Price (real-time). `symbol` should be the exact tradingsymbol
        for non-NSE exchanges (e.g. an NFO option's "NIFTY30JUL26240000CE") --
        only NSE equity symbols get the "-EQ" suffix auto-appended."""
        self._ensure_connected()
        tradingsymbol = self._eq_symbol(symbol) if exchange == "NSE" else symbol
        try:
            resp = self._obj.ltpData(exchange, tradingsymbol, token)
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
        max_retries: int = 2,
    ) -> pd.DataFrame | None:
        """
        Fetch all 5-min OHLCV bars for today's NSE session (9:15 AM → now).
        Returns a DataFrame indexed by IST datetime or None on failure.

        Retries on "exceeding access rate" errors: AngelOne's documented
        limit for this endpoint is 3 req/s, 180/min, 5000/hour (per their
        SmartAPI forum, "Changes in API Rate Limit"), and our callers pace
        well under that (~1/s) -- yet a live run against 62 symbols got
        rejected on every single call. That matches a separately-documented,
        AngelOne-acknowledged issue ("API Rate Limit checks are not perfect"
        on the SmartAPI forum): users get blocked well below the documented
        limits, i.e. a real flakiness/false-positive in AngelOne's own
        enforcement, not something pacing alone fixes. A short retry
        recovers a meaningful fraction of these transient false rejections.
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
        last_err: Exception | None = None
        for attempt in range(max_retries + 1):
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
                last_err = e
                if attempt < max_retries and "exceeding access rate" in str(e).lower():
                    time.sleep(2.0 * (attempt + 1))   # 2s, then 4s
                    continue
                break
        log.error(f"{symbol}: candle fetch failed — {last_err}")
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
        payload = {"mode": mode, "exchangeTokens": {exchange: tokens}}
        try:
            r = requests.post(
                "https://apiconnect.angelone.in/rest/secure/angelbroking/market/v1/quote/",
                headers=self._rest_headers(),
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
