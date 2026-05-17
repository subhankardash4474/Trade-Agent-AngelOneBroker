"""
WebSocket Client Module
Connects to broker WebSocket feeds (AngelOne / Kite) for real-time
tick data at ~500ms per instrument. Feeds ticks to the TickAggregator.
"""

import json
import threading
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional

import pytz
from loguru import logger

IST = pytz.timezone("Asia/Kolkata")


class WebSocketClient:
    """
    Abstraction over broker-specific WebSocket connections.

    Supports:
      - AngelOne SmartWebSocket
      - Zerodha KiteTicker

    Fires tick callbacks that flow into the TickAggregator.
    """

    def __init__(self, broker: str, config: dict, smart_api=None):
        self._broker = broker
        self._config = config
        self._api = smart_api
        self._ws = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self.on_tick: Optional[Callable] = None
        self.on_connect: Optional[Callable] = None
        self.on_error: Optional[Callable] = None

        # Map symbol -> token for subscription
        self._subscriptions: Dict[str, str] = {}

    def subscribe(self, instruments: List[dict]):
        """Register instruments for tick subscription (additive).

        Upserts each symbol into the subscription dict. Existing entries
        are NOT removed -- use ``set_subscriptions`` or ``unsubscribe``
        for that. See P1 #14 for why surgical removal matters."""
        for inst in instruments:
            self._subscriptions[inst["symbol"]] = str(inst.get("token", ""))
        logger.info(f"WebSocket subscriptions: {list(self._subscriptions.keys())}")

    def set_subscriptions(self, instruments: List[dict]):
        """P1 #14 (2026-05-17): replace the subscription set wholesale.

        Use this for held-only mode (or any caller that needs the broker
        to STOP receiving ticks for stale symbols on next reconnect). The
        old ``subscribe`` is upsert-only, so a position close left its
        symbol in ``_subscriptions`` forever -- the broker happily kept
        sending ticks for closed names, wasting bandwidth and the agent's
        WS-thread CPU."""
        new_map: Dict[str, str] = {}
        for inst in instruments:
            new_map[inst["symbol"]] = str(inst.get("token", ""))
        removed = sorted(set(self._subscriptions) - set(new_map))
        added = sorted(set(new_map) - set(self._subscriptions))
        self._subscriptions = new_map
        if added or removed:
            logger.info(
                f"WebSocket subscriptions replaced: "
                f"+{added} -{removed} (now {sorted(self._subscriptions)})"
            )

    def unsubscribe(self, symbol: str) -> bool:
        """P1 #14 (2026-05-17): surgically drop a symbol from subscriptions.

        Returns True if the symbol was present and removed."""
        return self._subscriptions.pop(symbol, None) is not None

    def start(self):
        """Start the WebSocket connection in a background thread."""
        if self._running:
            return

        self._running = True
        if self._broker == "angelone":
            self._thread = threading.Thread(target=self._run_angelone, daemon=True)
        elif self._broker == "kite":
            self._thread = threading.Thread(target=self._run_kite, daemon=True)
        else:
            logger.warning(f"Unknown broker '{self._broker}' for WebSocket, using simulation")
            self._thread = threading.Thread(target=self._run_simulation, daemon=True)

        self._thread.start()
        logger.info(f"WebSocket client started ({self._broker})")

    def stop(self):
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        logger.info("WebSocket client stopped")

    # ── AngelOne WebSocket ───────────────────────────────────

    def _run_angelone(self):
        try:
            from SmartApi.smartWebSocketV2 import SmartWebSocketV2

            broker_cfg = self._config.get("broker", {})
            auth_token = self._api.access_token if self._api else ""
            feed_token = broker_cfg.get("feed_token", "")
            client_id = broker_cfg.get("client_id", "")

            sws = SmartWebSocketV2(auth_token, broker_cfg.get("api_key", ""), client_id, feed_token)

            exchange = self._config.get("market", {}).get("exchange", "NSE")
            exchange_code = 1 if exchange == "NSE" else 2  # 1=NSE, 2=BSE

            token_list = [{"exchangeType": exchange_code, "tokens": list(self._subscriptions.values())}]
            correlation_id = "trading_agent_ws"

            def on_data(wsapp, message):
                self._handle_angelone_tick(message)

            def on_open(wsapp):
                logger.info("AngelOne WebSocket connected")
                sws.subscribe(correlation_id, 3, token_list)  # mode 3 = SNAP_QUOTE
                if self.on_connect:
                    self.on_connect()

            def on_error(wsapp, error):
                logger.error(f"AngelOne WebSocket error: {error}")
                if self.on_error:
                    self.on_error(error)

            def on_close(wsapp):
                # P1 #13 (2026-05-17) -- LIVE-MODE SAFETY: SmartWebSocketV2's
                # ``connect()`` returns normally when the server closes the
                # session (idle timeout, broker rollover, network drop). The
                # OLD code just logged and let the background thread die
                # while ``_running`` stayed True. Ticks then stopped silently
                # for the rest of the day -- no exits fired, no alarm. We
                # now kick the reconnect loop unless the daemon was asked
                # to stop. Exponential backoff caps at 60s in _reconnect_loop.
                logger.warning("AngelOne WebSocket closed")
                if self._running:
                    logger.warning(
                        "AngelOne WebSocket closed while running -- "
                        "scheduling reconnect."
                    )
                    self._reconnect_loop()

            sws.on_data = on_data
            sws.on_open = on_open
            sws.on_error = on_error
            sws.on_close = on_close

            self._ws = sws
            sws.connect()

        except ImportError:
            logger.error("SmartApi not installed, falling back to simulation mode")
            self._run_simulation()
        except Exception as e:
            logger.error(f"AngelOne WebSocket error: {e}")
            self._reconnect_loop()

    def _handle_angelone_tick(self, message):
        """Parse AngelOne tick data and fire callback."""
        try:
            if isinstance(message, str):
                message = json.loads(message)

            # Map token back to symbol
            token = str(message.get("token", ""))
            symbol = self._token_to_symbol(token)
            if not symbol:
                return

            tick = {
                "symbol": symbol,
                "ltp": float(message.get("last_traded_price", 0)) / 100,
                "volume": float(message.get("vol_traded_today", 0)),
                "bid": float(message.get("best_bid_price", 0)) / 100,
                "ask": float(message.get("best_ask_price", 0)) / 100,
                "open": float(message.get("open_price_day", 0)) / 100,
                "high": float(message.get("high_price_day", 0)) / 100,
                "low": float(message.get("low_price_day", 0)) / 100,
            }
            # P2 tick-path cluster (2026-05-17): pass the exchange-side tick
            # timestamp through so the aggregator can stamp candles with the
            # event time instead of host wall-clock. Closes the gap where
            # a slow consumer (laggy network, GC pause) would attribute
            # ticks to the WRONG candle bucket. Field is `exchange_timestamp`
            # in epoch millis on AngelOne; defensive: skip if absent.
            exch_ts_ms = message.get("exchange_timestamp")
            if exch_ts_ms:
                try:
                    tick["timestamp"] = datetime.fromtimestamp(
                        float(exch_ts_ms) / 1000.0, tz=IST
                    )
                except (TypeError, ValueError, OSError):
                    pass

            if self.on_tick:
                self.on_tick(tick)

        except Exception as e:
            logger.error(f"Error parsing AngelOne tick: {e}")

    # ── Kite WebSocket ───────────────────────────────────────

    def _run_kite(self):
        try:
            from kiteconnect import KiteTicker

            broker_cfg = self._config.get("broker", {})
            kws = KiteTicker(broker_cfg.get("api_key", ""), broker_cfg.get("access_token", ""))

            tokens = [int(t) for t in self._subscriptions.values() if t]

            def on_ticks(ws, ticks):
                for tick_data in ticks:
                    self._handle_kite_tick(tick_data)

            def on_connect(ws, response):
                logger.info("Kite WebSocket connected")
                ws.subscribe(tokens)
                ws.set_mode(ws.MODE_FULL, tokens)
                if self.on_connect:
                    self.on_connect()

            def on_close(ws, code, reason):
                # P1 #13 (2026-05-17): mirror angelone -- reconnect if still running.
                logger.warning(f"Kite WebSocket closed: {code} {reason}")
                if self._running:
                    self._reconnect_loop()

            def on_error(ws, code, reason):
                logger.error(f"Kite WebSocket error: {code} {reason}")
                if self.on_error:
                    self.on_error(reason)

            kws.on_ticks = on_ticks
            kws.on_connect = on_connect
            kws.on_close = on_close
            kws.on_error = on_error

            self._ws = kws
            kws.connect(threaded=False)

        except ImportError:
            logger.error("kiteconnect not installed, falling back to simulation")
            self._run_simulation()
        except Exception as e:
            logger.error(f"Kite WebSocket error: {e}")
            self._reconnect_loop()

    def _handle_kite_tick(self, tick_data: dict):
        """Parse Kite tick and fire callback."""
        try:
            token = str(tick_data.get("instrument_token", ""))
            symbol = self._token_to_symbol(token)
            if not symbol:
                return

            tick = {
                "symbol": symbol,
                "ltp": float(tick_data.get("last_price", 0)),
                "volume": float(tick_data.get("volume_traded", 0)),
                "bid": float(tick_data.get("depth", {}).get("buy", [{}])[0].get("price", 0)),
                "ask": float(tick_data.get("depth", {}).get("sell", [{}])[0].get("price", 0)),
                "open": float(tick_data.get("ohlc", {}).get("open", 0)),
                "high": float(tick_data.get("ohlc", {}).get("high", 0)),
                "low": float(tick_data.get("ohlc", {}).get("low", 0)),
            }

            if self.on_tick:
                self.on_tick(tick)

        except Exception as e:
            logger.error(f"Error parsing Kite tick: {e}")

    # ── Simulation Mode ──────────────────────────────────────

    def _run_simulation(self):
        """Generate simulated ticks for paper trading when no broker API is available."""
        import random
        logger.info("Running in WebSocket SIMULATION mode")

        base_prices = {}
        for symbol in self._subscriptions:
            base_prices[symbol] = random.uniform(100, 3000)

        while self._running:
            for symbol in self._subscriptions:
                base = base_prices[symbol]
                jitter = base * random.uniform(-0.002, 0.002)
                base_prices[symbol] = base + jitter

                tick = {
                    "symbol": symbol,
                    "ltp": round(base_prices[symbol], 2),
                    "volume": random.randint(100, 10000),
                    "bid": round(base_prices[symbol] - 0.05, 2),
                    "ask": round(base_prices[symbol] + 0.05, 2),
                }

                if self.on_tick:
                    self.on_tick(tick)

            time.sleep(0.5)

    # ── Helpers ──────────────────────────────────────────────

    def _token_to_symbol(self, token) -> Optional[str]:
        """Map a broker tick token back to its tradingsymbol.

        P2 fix (2026-05-17, audit "Live tick path cluster"): both arguments
        must be string-compared. ``_subscriptions`` stores tokens as strings
        (from instruments JSON) but AngelOne tick payloads sometimes deliver
        them as ints. Cast both to str so a tick on token 11536 (int) still
        matches the stored "11536" (str). Previously dropped ticks silently
        ate the exit signal for the symbol."""
        token_s = str(token).strip()
        for sym, tok in self._subscriptions.items():
            if str(tok).strip() == token_s:
                return sym
        return None

    def _reconnect_loop(self):
        """Auto-reconnect with exponential backoff.

        P1 #13 (2026-05-17): previously had ``break`` after the first
        successful reconnect attempt -- so when the SECOND session also
        ended (very common: broker rollover at midnight, 8h JWT, etc.),
        the loop exited and the daemon was tickless again. Now we stay
        in the reconnect loop until ``_running`` is False (stop()
        called). The inner ``_run_angelone``/``_run_kite`` blocks until
        the session ends; on return we back off and try again."""
        delay = 2
        while self._running:
            logger.info(f"Reconnecting WebSocket in {delay}s...")
            time.sleep(delay)
            try:
                if self._broker == "angelone":
                    self._run_angelone()
                elif self._broker == "kite":
                    self._run_kite()
                else:
                    return  # simulation mode: no reconnect needed
                # If the inner _run_* call returns without raising, the
                # session ended cleanly. We've already logged "closed"
                # in on_close. Reset the backoff and try again.
                delay = 2
            except Exception as e:
                logger.error(f"Reconnection failed: {e}")
                delay = min(delay * 2, 60)
