"""WebSocket feed — real-time price and order book updates from Polymarket CLOB.

Connects to wss://ws-subscriptions-clob.polymarket.com/ws/market
No authentication needed for the market channel.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosed

from polyedge.core.config import Settings
from polyedge.core.models import OrderBook, OrderBookLevel

logger = logging.getLogger("polyedge.ws_feed")

# Event types from the market channel
EVENT_BOOK = "book"
EVENT_PRICE_CHANGE = "price_change"
EVENT_LAST_TRADE = "last_trade_price"
EVENT_BEST_BID_ASK = "best_bid_ask"
EVENT_TICK_SIZE = "tick_size_change"
EVENT_NEW_MARKET = "new_market"
EVENT_MARKET_RESOLVED = "market_resolved"

PING_INTERVAL = 10  # seconds — required by Polymarket
RECONNECT_DELAY_BASE = 2  # seconds
RECONNECT_DELAY_MAX = 60  # seconds


class MarketFeed:
    """Real-time WebSocket feed for Polymarket market data.

    Subscribes to token IDs and dispatches events to registered callbacks.
    Handles heartbeats, reconnection, and dynamic subscribe/unsubscribe.
    """

    def __init__(self, settings: Settings):
        self.ws_url = settings.polymarket.ws_url
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._subscribed_assets: set[str] = set()
        self._running = False
        self._reconnect_attempts = 0

        # Event callbacks: event_type -> list of async callables
        self._callbacks: dict[str, list[Callable]] = {}

        # Local state — latest book snapshots and prices
        self.books: dict[str, OrderBook] = {}  # token_id -> OrderBook
        self.best_prices: dict[str, dict] = {}  # token_id -> {bid, ask, spread}
        self.last_trades: dict[str, dict] = {}  # token_id -> {price, size, side}

    def on(self, event_type: str, callback: Callable):
        """Register a callback for an event type.

        Callbacks are async functions: async def handler(event: dict)
        """
        if event_type not in self._callbacks:
            self._callbacks[event_type] = []
        self._callbacks[event_type].append(callback)

    async def start(self, asset_ids: list[str]):
        """Connect and start consuming events.

        Args:
            asset_ids: Token IDs to subscribe to (the long numeric strings).
        """
        self._subscribed_assets = set(asset_ids)
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
                    f"WebSocket closed ({e.code}), reconnecting in {delay}s "
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
                    f"WebSocket error: {e}, reconnecting in {delay}s "
                    f"(attempt {self._reconnect_attempts})"
                )
                await asyncio.sleep(delay)

    async def stop(self):
        """Gracefully disconnect."""
        self._running = False
        if self._ws:
            await self._ws.close()

    async def subscribe(self, asset_ids: list[str]):
        """Dynamically subscribe to additional token IDs."""
        new_ids = [a for a in asset_ids if a not in self._subscribed_assets]
        if not new_ids:
            return

        self._subscribed_assets.update(new_ids)
        if self._ws:
            msg = {
                "assets_ids": new_ids,
                "operation": "subscribe",
                "custom_feature_enabled": True,
            }
            await self._ws.send(json.dumps(msg))
            logger.info(f"Subscribed to {len(new_ids)} additional assets")

    async def unsubscribe(self, asset_ids: list[str]):
        """Dynamically unsubscribe from token IDs."""
        remove_ids = [a for a in asset_ids if a in self._subscribed_assets]
        if not remove_ids:
            return

        self._subscribed_assets -= set(remove_ids)
        if self._ws:
            msg = {
                "assets_ids": remove_ids,
                "operation": "unsubscribe",
            }
            await self._ws.send(json.dumps(msg))
            logger.info(f"Unsubscribed from {len(remove_ids)} assets")

    async def _connect_and_consume(self):
        """Single connection lifecycle: connect, subscribe, consume until disconnect."""
        async with websockets.connect(self.ws_url, ping_interval=None) as ws:
            self._ws = ws
            self._reconnect_attempts = 0
            logger.info(f"Connected to {self.ws_url}")

            # Send initial subscription
            if self._subscribed_assets:
                sub_msg = {
                    "assets_ids": list(self._subscribed_assets),
                    "type": "market",
                    "custom_feature_enabled": True,
                }
                await ws.send(json.dumps(sub_msg))
                logger.info(f"Subscribed to {len(self._subscribed_assets)} assets")

            # Start heartbeat in background
            heartbeat_task = asyncio.create_task(self._heartbeat(ws))

            try:
                async for raw_msg in ws:
                    if raw_msg == "PONG":
                        continue

                    try:
                        event = json.loads(raw_msg)
                    except json.JSONDecodeError:
                        logger.debug(f"Non-JSON message: {raw_msg[:100]}")
                        continue

                    await self._dispatch(event)
            finally:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
                self._ws = None

    async def _heartbeat(self, ws):
        """Send PING every 10 seconds to keep connection alive."""
        while True:
            await asyncio.sleep(PING_INTERVAL)
            try:
                await ws.send("PING")
            except ConnectionClosed:
                break

    async def _dispatch(self, event: dict):
        """Route an event to the appropriate handler + callbacks."""
        event_type = event.get("event_type", "")

        # Update local state
        if event_type == EVENT_BOOK:
            self._handle_book(event)
        elif event_type == EVENT_BEST_BID_ASK:
            self._handle_best_bid_ask(event)
        elif event_type == EVENT_LAST_TRADE:
            self._handle_last_trade(event)
        elif event_type == EVENT_PRICE_CHANGE:
            self._handle_price_change(event)

        # Fire registered callbacks
        callbacks = self._callbacks.get(event_type, [])
        for cb in callbacks:
            try:
                await cb(event)
            except Exception as e:
                logger.error(f"Callback error for {event_type}: {e}")

        # Also fire wildcard callbacks
        for cb in self._callbacks.get("*", []):
            try:
                await cb(event)
            except Exception as e:
                logger.error(f"Wildcard callback error: {e}")

    def _handle_book(self, event: dict):
        """Process a full order book snapshot."""
        asset_id = event.get("asset_id", "")
        market_id = event.get("market", "")

        bids = [
            OrderBookLevel(price=float(b["price"]), size=float(b["size"]))
            for b in event.get("bids", [])
        ]
        asks = [
            OrderBookLevel(price=float(a["price"]), size=float(a["size"]))
            for a in event.get("asks", [])
        ]

        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)

        self.books[asset_id] = OrderBook(
            market_id=market_id,
            token_id=asset_id,
            bids=bids,
            asks=asks,
        )

    def _handle_best_bid_ask(self, event: dict):
        """Process a top-of-book update."""
        asset_id = event.get("asset_id", "")
        self.best_prices[asset_id] = {
            "bid": float(event.get("best_bid", 0)),
            "ask": float(event.get("best_ask", 0)),
            "spread": float(event.get("spread", 0)),
            "timestamp": event.get("timestamp"),
        }

    def _handle_last_trade(self, event: dict):
        """Process a trade execution event."""
        asset_id = event.get("asset_id", "")
        self.last_trades[asset_id] = {
            "price": float(event.get("price", 0)),
            "size": float(event.get("size", 0)),
            "side": event.get("side", ""),
            "timestamp": event.get("timestamp"),
        }

    def _handle_price_change(self, event: dict):
        """Process incremental order book updates.

        Applies price_change deltas to the local book snapshot.
        A size of "0" means the level was removed.
        """
        market_id = event.get("market", "")

        for change in event.get("price_changes", []):
            asset_id = change.get("asset_id", "")
            price = float(change.get("price", 0))
            size = float(change.get("size", 0))
            side = change.get("side", "")

            book = self.books.get(asset_id)
            if not book:
                continue

            if side == "BUY":
                # Update bid side
                book.bids = [b for b in book.bids if b.price != price]
                if size > 0:
                    book.bids.append(OrderBookLevel(price=price, size=size))
                    book.bids.sort(key=lambda x: x.price, reverse=True)
            elif side == "SELL":
                # Update ask side
                book.asks = [a for a in book.asks if a.price != price]
                if size > 0:
                    book.asks.append(OrderBookLevel(price=price, size=size))
                    book.asks.sort(key=lambda x: x.price)

    def get_book(self, token_id: str) -> Optional[OrderBook]:
        """Get the latest order book snapshot for a token."""
        return self.books.get(token_id)

    def get_best_price(self, token_id: str) -> Optional[dict]:
        """Get the latest best bid/ask for a token."""
        return self.best_prices.get(token_id)

    @property
    def is_connected(self) -> bool:
        if self._ws is None:
            return False
        try:
            return self._ws.state.name == "OPEN"
        except AttributeError:
            return getattr(self._ws, "open", False)
