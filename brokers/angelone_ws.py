"""
AngelOne SmartAPI WebSocket Streaming 2.0 client.

Maintains a background thread subscribed to Quote-mode ticks for registered
NSE tokens. Prices update in real-time; read via .prices[symbol].

Binary packet layout (Quote mode, 123 bytes, little-endian):
    Offset  Size  Type    Field
        0     1  uint8   mode
        1     1  uint8   exchange_type
        2    25  bytes   token (null-padded ASCII)
       27     8  int64   sequence_number
       35     8  int64   exchange_timestamp
       43     8  int64   LTP  (paise → divide by 100)
       51     8  int64   last_traded_qty
       59     8  int64   avg_traded_price (paise)
       67     8  int64   volume
       75     8  double  total_buy_qty
       83     8  double  total_sell_qty
       91     8  int64   open  (paise)
       99     8  int64   high  (paise)
      107     8  int64   low   (paise)
      115     8  int64   close (paise)  ← Quote mode ends here (123 bytes)
"""
import json
import logging
import struct
import threading
import time as _time

import websocket

log = logging.getLogger(__name__)

_WS_URL        = "wss://smartapisocket.angelone.in/smart-stream"
_QUOTE_MODE    = 2    # Quote: LTP + OHLCV
_NSE_CM        = 1    # exchangeType for NSE cash market
_PING_INTERVAL = 30   # seconds between heartbeat pings


class AngelOneWebSocket:
    """
    Real-time tick feed for AngelOne SmartAPI Streaming 2.0.

    Runs a daemon thread that keeps a WebSocket connection open and updates
    self.prices on every incoming tick. The main trading thread reads prices
    without any extra API calls or rate-limit budget.

    Usage:
        ws = AngelOneWebSocket(auth_token, feed_token, client_code, api_key)
        ws.start(token_map)           # {symbol: "token_str"} — starts background thread
        ltp = ws.prices.get("RELIANCE", {}).get("ltp")
        ws.stop()

    prices: dict[symbol, dict]  where inner dict has:
        ltp, open, high, low, close (float, ₹), volume (int)
    """

    def __init__(
        self,
        auth_token: str,
        feed_token: str,
        client_code: str,
        api_key: str,
    ):
        self._auth_token  = auth_token
        self._feed_token  = feed_token
        self._client_code = client_code
        self._api_key     = api_key

        self.prices: dict[str, dict] = {}
        self.connected = False

        self._token_map:    dict[str, str] = {}
        self._token_to_sym: dict[str, str] = {}
        self._ws:    websocket.WebSocketApp | None = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def start(self, token_map: dict[str, str]) -> None:
        """Connect in a daemon thread and begin streaming prices."""
        self._token_map    = token_map
        self._token_to_sym = {v: k for k, v in token_map.items()}

        headers = [
            f"Authorization: {self._auth_token}",
            f"x-api-key: {self._api_key}",
            f"x-client-code: {self._client_code}",
            f"x-feed-token: {self._feed_token}",
        ]
        self._stop_event.clear()
        self._ws = websocket.WebSocketApp(
            _WS_URL,
            header=headers,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        t = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="AngelOneWS",
        )
        t.start()
        log.info(f"WebSocket thread started — {len(token_map)} symbols queued for subscription")

    def stop(self) -> None:
        """Close the WebSocket and stop the background thread."""
        self._stop_event.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        self.connected = False
        log.info("WebSocket stopped")

    # ------------------------------------------------------------------ #
    # Internal loop with auto-reconnect
    # ------------------------------------------------------------------ #

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._ws.run_forever(ping_interval=0)
            except Exception as e:
                log.error(f"WebSocket run_forever error: {e}")
            if self._stop_event.is_set():
                break
            log.warning("WebSocket disconnected — reconnecting in 5s")
            _time.sleep(5)
            # Rebuild the WebSocketApp so headers / callbacks are fresh
            headers = [
                f"Authorization: {self._auth_token}",
                f"x-api-key: {self._api_key}",
                f"x-client-code: {self._client_code}",
                f"x-feed-token: {self._feed_token}",
            ]
            self._ws = websocket.WebSocketApp(
                _WS_URL,
                header=headers,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )

    # ------------------------------------------------------------------ #
    # WebSocket callbacks
    # ------------------------------------------------------------------ #

    def _on_open(self, ws) -> None:
        self.connected = True
        log.info("WebSocket connected")
        self._subscribe(ws)
        # Heartbeat: AngelOne requires a text "ping" every 30 s
        t = threading.Thread(target=self._heartbeat, daemon=True, name="AngelOneWSPing")
        t.start()

    def _subscribe(self, ws) -> None:
        tokens = list(self._token_map.values())
        for i in range(0, len(tokens), 1000):
            chunk = tokens[i : i + 1000]
            msg = json.dumps({
                "correlationID": "orb_bot",
                "action": 1,
                "params": {
                    "mode": _QUOTE_MODE,
                    "tokenList": [{"exchangeType": _NSE_CM, "tokens": chunk}],
                },
            })
            ws.send(msg)
            log.info(f"WebSocket: subscribed batch {i // 1000 + 1} ({len(chunk)} tokens)")

    def _on_message(self, ws, message) -> None:
        if isinstance(message, str):
            if message == "pong":
                log.debug("WebSocket pong")
            return
        if isinstance(message, (bytes, bytearray)):
            self._parse_binary(bytes(message))

    def _parse_binary(self, data: bytes) -> None:
        if len(data) < 51:
            return
        try:
            token = data[2:27].rstrip(b"\x00").decode("ascii", errors="ignore")
            sym   = self._token_to_sym.get(token)
            if sym is None:
                return

            # All price fields are int64 paise (8-byte slots from position 43)
            ltp   = struct.unpack_from("<q", data, 43)[0]  / 100.0
            entry: dict = {"ltp": ltp}

            if len(data) >= 123:
                vol   = struct.unpack_from("<q", data,  67)[0]
                open_ = struct.unpack_from("<q", data,  91)[0] / 100.0
                high  = struct.unpack_from("<q", data,  99)[0] / 100.0
                low   = struct.unpack_from("<q", data, 107)[0] / 100.0
                close = struct.unpack_from("<q", data, 115)[0] / 100.0
                entry.update({"open": open_, "high": high, "low": low,
                              "close": close, "volume": vol})

            self.prices[sym] = entry

        except Exception as e:
            log.debug(f"WS binary parse error ({len(data)} bytes): {e}")

    def _on_error(self, ws, error) -> None:
        log.error(f"WebSocket error: {error}")
        self.connected = False

    def _on_close(self, ws, code, msg) -> None:
        self.connected = False
        if not self._stop_event.is_set():
            log.warning(f"WebSocket closed ({code}: {msg})")

    def _heartbeat(self) -> None:
        while not self._stop_event.is_set():
            _time.sleep(_PING_INTERVAL)
            if self._stop_event.is_set():
                break
            try:
                self._ws.send("ping")
                log.debug("WS ping sent")
            except Exception as e:
                log.warning(f"WS heartbeat send failed: {e}")
                break
