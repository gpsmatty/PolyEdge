"""Binance WebSocket feed — real-time crypto price data for sniper strategy.

Connects to Binance's public WebSocket API (no auth needed) for real-time
price data on BTC, ETH, SOL, etc.  We use this as a price oracle to compare
against Polymarket's short-duration crypto prediction markets.

Streams:
  - Individual symbol ticker: wss://stream.binance.com:9443/ws/<symbol>@ticker
  - Combined streams: wss://stream.binance.com:9443/stream?streams=<s1>/<s2>/...

The key insight: Binance spot price moves BEFORE Polymarket's 5-minute crypto
markets adjust.  If BTC pumps 2% with 30 seconds left in a "BTC Up or Down"
window, the outcome is near-certain but Polymarket may still show ~60/40.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger("polyedge.binance_feed")

BINANCE_WS_BASE = "wss://stream.binance.us:9443"

# Symbols we care about — these map to Polymarket crypto markets
DEFAULT_SYMBOLS = ["btcusdt", "ethusdt", "solusdt"]

RECONNECT_DELAY_BASE = 2
RECONNECT_DELAY_MAX = 30


@dataclass
class PriceSnapshot:
    """A point-in-time price snapshot from Binance."""
    symbol: str
    price: float
    bid: float = 0.0
    ask: float = 0.0
    volume_24h: float = 0.0
    price_change_pct_24h: float = 0.0
    timestamp: float = field(default_factory=time.time)

    @property
    def age_seconds(self) -> float:
        return time.time() - self.timestamp

    @property
    def is_fresh(self) -> bool:
        """Price is less than 5 seconds old."""
        return self.age_seconds < 5.0


@dataclass
class PriceWindow:
    """Tracks price movement over a time window for sniper decisions."""
    symbol: str
    window_start_price: float = 0.0
    window_start_time: float = 0.0
    current_price: float = 0.0
    high: float = 0.0
    low: float = float("inf")
    tick_count: int = 0

    @property
    def change_pct(self) -> float:
        if self.window_start_price <= 0:
            return 0.0
        return (self.current_price - self.window_start_price) / self.window_start_price

    @property
    def direction(self) -> str:
        """'up', 'down', or 'flat' based on price change."""
        pct = self.change_pct
        if pct > 0.0001:  # > 0.01%
            return "up"
        elif pct < -0.0001:
            return "down"
        return "flat"

    @property
    def volatility(self) -> float:
        """High-low range as percentage of start price."""
        if self.window_start_price <= 0 or self.high <= 0:
            return 0.0
        return (self.high - self.low) / self.window_start_price

    def reset(self, price: float):
        """Reset window with a new starting price."""
        self.window_start_price = price
        self.window_start_time = time.time()
        self.current_price = price
        self.high = price
        self.low = price
        self.tick_count = 0

    def update(self, price: float):
        """Update window with a new price tick."""
        self.current_price = price
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.tick_count += 1


class BinanceFeed:
    """Real-time price feed from Binance public WebSocket API.

    No API key needed.  Connects to combined streams for multiple symbols
    and maintains latest price snapshots + rolling price windows.
    """

    def __init__(self, symbols: list[str] | None = None):
        self.symbols = [s.lower() for s in (symbols or DEFAULT_SYMBOLS)]
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._reconnect_attempts = 0

        # Latest price per symbol
        self.prices: dict[str, PriceSnapshot] = {}

        # Rolling price windows per symbol (for sniper decisions)
        self.windows: dict[str, PriceWindow] = {
            s: PriceWindow(symbol=s) for s in self.symbols
        }

        # Callbacks: symbol -> list of async callables
        self._callbacks: dict[str, list[Callable]] = {}
        self._global_callbacks: list[Callable] = []

    def on_price(self, symbol: str, callback: Callable):
        """Register a callback for price updates on a specific symbol.

        Callback signature: async def handler(snapshot: PriceSnapshot)
        """
        sym = symbol.lower()
        if sym not in self._callbacks:
            self._callbacks[sym] = []
        self._callbacks[sym].append(callback)

    def on_any_price(self, callback: Callable):
        """Register a callback for ALL price updates.

        Callback signature: async def handler(snapshot: PriceSnapshot)
        """
        self._global_callbacks.append(callback)

    async def start(self):
        """Connect and start consuming price data."""
        self._running = True
        self._reconnect_attempts = 0

        while self._running:
            try:
                await self._connect_and_consume()
            except ConnectionClosed as e:
                if not self._running:
                    break
                delay = min(
                    RECONNECT_DELAY_BASE * (2 ** self._reconnect_attempts),
                    RECONNECT_DELAY_MAX,
                )
                self._reconnect_attempts += 1
                logger.warning(
                    f"Binance WS closed ({e.code}), reconnecting in {delay}s "
                    f"(attempt {self._reconnect_attempts})"
                )
                await asyncio.sleep(delay)
            except Exception as e:
                if not self._running:
                    break
                delay = min(
                    RECONNECT_DELAY_BASE * (2 ** self._reconnect_attempts),
                    RECONNECT_DELAY_MAX,
                )
                self._reconnect_attempts += 1
                logger.error(
                    f"Binance WS error: {e}, reconnecting in {delay}s "
                    f"(attempt {self._reconnect_attempts})"
                )
                await asyncio.sleep(delay)

    async def stop(self):
        """Gracefully disconnect."""
        self._running = False
        if self._ws:
            await self._ws.close()

    def get_price(self, symbol: str) -> Optional[PriceSnapshot]:
        """Get the latest price snapshot for a symbol."""
        return self.prices.get(symbol.lower())

    def get_window(self, symbol: str) -> Optional[PriceWindow]:
        """Get the rolling price window for a symbol."""
        return self.windows.get(symbol.lower())

    def start_window(self, symbol: str):
        """Start/reset a price tracking window for a symbol.

        Call this at the start of a Polymarket 5-min window.
        """
        sym = symbol.lower()
        snap = self.prices.get(sym)
        if snap and sym in self.windows:
            self.windows[sym].reset(snap.price)
            logger.info(f"Price window started for {sym} at ${snap.price:,.2f}")

    def get_all_prices(self) -> dict[str, float]:
        """Get latest price for all tracked symbols."""
        return {
            sym: snap.price
            for sym, snap in self.prices.items()
            if snap.is_fresh
        }

    @property
    def is_connected(self) -> bool:
        if self._ws is None:
            return False
        try:
            return self._ws.state.name == "OPEN"
        except AttributeError:
            # Fallback for older websockets versions
            return getattr(self._ws, "open", False)

    async def _connect_and_consume(self):
        """Single connection lifecycle."""
        # Build combined streams URL
        streams = "/".join(f"{s}@ticker" for s in self.symbols)
        url = f"{BINANCE_WS_BASE}/stream?streams={streams}"

        async with websockets.connect(url, ping_interval=20) as ws:
            self._ws = ws
            self._reconnect_attempts = 0
            logger.info(
                f"Connected to Binance WS — tracking {len(self.symbols)} symbols: "
                f"{', '.join(s.upper() for s in self.symbols)}"
            )

            async for raw_msg in ws:
                if not self._running:
                    break

                try:
                    data = json.loads(raw_msg)
                except json.JSONDecodeError:
                    continue

                # Combined stream format: {"stream": "btcusdt@ticker", "data": {...}}
                stream_data = data.get("data", data)
                await self._handle_ticker(stream_data)

            self._ws = None

    async def _handle_ticker(self, data: dict):
        """Process a 24hr ticker update."""
        symbol = data.get("s", "").lower()
        if not symbol or symbol not in self.symbols:
            return

        try:
            snapshot = PriceSnapshot(
                symbol=symbol,
                price=float(data.get("c", 0)),       # Last price
                bid=float(data.get("b", 0)),          # Best bid
                ask=float(data.get("a", 0)),          # Best ask
                volume_24h=float(data.get("v", 0)),   # 24h volume
                price_change_pct_24h=float(data.get("P", 0)),  # 24h change %
            )
        except (ValueError, TypeError):
            return

        if snapshot.price <= 0:
            return

        self.prices[symbol] = snapshot

        # Update rolling window
        window = self.windows.get(symbol)
        if window and window.window_start_price > 0:
            window.update(snapshot.price)

        # Fire callbacks
        for cb in self._callbacks.get(symbol, []):
            try:
                await cb(snapshot)
            except Exception as e:
                logger.error(f"Price callback error for {symbol}: {e}")

        for cb in self._global_callbacks:
            try:
                await cb(snapshot)
            except Exception as e:
                logger.error(f"Global price callback error: {e}")
