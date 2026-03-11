"""Binance aggTrade WebSocket feed — tick-level trade data for micro sniper.

Connects to Binance's aggTrade stream for real-time individual trade data.
Unlike the @ticker stream (1/sec summary), aggTrade delivers EVERY trade
as it happens (~10-50 messages/sec for BTCUSDT).

The key field is `m` (buyer is maker):
  - m=True  → seller was the aggressor (sell pressure / taker sell)
  - m=False → buyer was the aggressor (buy pressure / taker buy)

This lets us compute order flow imbalance: when aggressive buyers dominate,
price is likely to go up, and vice versa.

Streams:
  Combined: wss://stream.binance.com:9443/stream?streams=btcusdt@aggTrade/...
  Single:   wss://stream.binance.com:9443/ws/btcusdt@aggTrade

We maintain rolling windows of trade flow metrics:
  - Buy/sell volume over 5s, 15s, 30s windows
  - Trade intensity (trades per second)
  - VWAP drift (volume-weighted average price vs start)
  - Order flow imbalance (OFI) = (buy_vol - sell_vol) / total_vol
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger("polyedge.binance_aggtrade")

BINANCE_WS_BASE = "wss://stream.binance.com:9443"

DEFAULT_SYMBOLS = ["btcusdt"]

RECONNECT_DELAY_BASE = 2
RECONNECT_DELAY_MAX = 30


@dataclass
class AggTrade:
    """A single aggregated trade from Binance."""
    symbol: str
    price: float
    quantity: float
    is_buyer_maker: bool   # True = seller was aggressor (sell pressure)
    timestamp: float       # Event time from Binance (ms -> s)

    @property
    def is_buy(self) -> bool:
        """True if the buyer was the aggressor (buy pressure)."""
        return not self.is_buyer_maker

    @property
    def dollar_volume(self) -> float:
        return self.price * self.quantity


@dataclass
class TradeFlowWindow:
    """Rolling window of trade flow metrics over a configurable time span.

    Maintains a deque of recent trades and computes flow metrics on demand.
    Designed for fast incremental updates on every tick.
    """
    symbol: str
    window_seconds: float = 15.0
    _trades: deque = field(default_factory=deque)

    # Cached aggregates (updated on every add)
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    buy_count: int = 0
    sell_count: int = 0
    vwap_numerator: float = 0.0  # sum(price * quantity)
    total_quantity: float = 0.0

    def add(self, trade: AggTrade):
        """Add a trade and prune expired entries."""
        self._trades.append(trade)
        if trade.is_buy:
            self.buy_volume += trade.dollar_volume
            self.buy_count += 1
        else:
            self.sell_volume += trade.dollar_volume
            self.sell_count += 1
        self.vwap_numerator += trade.price * trade.quantity
        self.total_quantity += trade.quantity
        self._prune()

    def _prune(self):
        """Remove trades older than the window."""
        cutoff = time.time() - self.window_seconds
        while self._trades and self._trades[0].timestamp < cutoff:
            old = self._trades.popleft()
            if old.is_buy:
                self.buy_volume -= old.dollar_volume
                self.buy_count -= 1
            else:
                self.sell_volume -= old.dollar_volume
                self.sell_count -= 1
            self.vwap_numerator -= old.price * old.quantity
            self.total_quantity -= old.quantity

    @property
    def total_volume(self) -> float:
        return self.buy_volume + self.sell_volume

    @property
    def total_count(self) -> int:
        return self.buy_count + self.sell_count

    @property
    def ofi(self) -> float:
        """Order Flow Imbalance: (buy_vol - sell_vol) / total_vol.

        Range [-1, 1]. Positive = buy pressure, negative = sell pressure.
        """
        total = self.total_volume
        if total <= 0:
            return 0.0
        return (self.buy_volume - self.sell_volume) / total

    @property
    def trade_intensity(self) -> float:
        """Trades per second in the window."""
        if not self._trades or len(self._trades) < 2:
            return 0.0
        span = self._trades[-1].timestamp - self._trades[0].timestamp
        if span <= 0:
            return float(len(self._trades))
        return len(self._trades) / span

    @property
    def vwap(self) -> float:
        """Volume-weighted average price in the window."""
        if self.total_quantity <= 0:
            return 0.0
        return self.vwap_numerator / self.total_quantity

    @property
    def latest_price(self) -> float:
        if self._trades:
            return self._trades[-1].price
        return 0.0

    @property
    def vwap_drift(self) -> float:
        """VWAP relative to latest price. Positive = price above VWAP (bullish).

        Returns fractional change: (price - vwap) / vwap.
        """
        v = self.vwap
        p = self.latest_price
        if v <= 0 or p <= 0:
            return 0.0
        return (p - v) / v

    @property
    def is_active(self) -> bool:
        """True if we have recent trades in the window."""
        return len(self._trades) > 0

    def reset(self):
        """Clear all data."""
        self._trades.clear()
        self.buy_volume = 0.0
        self.sell_volume = 0.0
        self.buy_count = 0
        self.sell_count = 0
        self.vwap_numerator = 0.0
        self.total_quantity = 0.0


@dataclass
class MicroStructure:
    """Complete microstructure state for a symbol.

    Holds multiple time windows and provides a unified momentum signal.
    """
    symbol: str
    flow_5s: TradeFlowWindow = field(default=None)
    flow_15s: TradeFlowWindow = field(default=None)
    flow_30s: TradeFlowWindow = field(default=None)

    # Price tracking for the current prediction market window
    window_start_price: float = 0.0
    window_start_time: float = 0.0
    current_price: float = 0.0
    tick_count: int = 0
    last_update_time: float = 0.0  # monotonic time of last aggTrade

    # Configurable momentum weights (set from MicroSniperConfig)
    weight_ofi_5s: float = 0.20
    weight_ofi_15s: float = 0.40
    weight_vwap_drift: float = 0.25
    weight_intensity: float = 0.15

    def __post_init__(self):
        if self.flow_5s is None:
            self.flow_5s = TradeFlowWindow(symbol=self.symbol, window_seconds=5.0)
        if self.flow_15s is None:
            self.flow_15s = TradeFlowWindow(symbol=self.symbol, window_seconds=15.0)
        if self.flow_30s is None:
            self.flow_30s = TradeFlowWindow(symbol=self.symbol, window_seconds=30.0)

    def add_trade(self, trade: AggTrade):
        """Add a trade to all windows.

        Detects gaps (>5s since last trade) which indicate a WebSocket
        disconnect/reconnect. After a gap, reset all windows to avoid
        stale momentum signals from pre-disconnect data.
        """
        now = time.monotonic()
        if self.last_update_time > 0 and (now - self.last_update_time) > 5.0:
            # Gap detected — WS was likely disconnected. Reset windows
            # so we don't trade on stale momentum from before the gap.
            logger.info(
                f"{self.symbol}: {now - self.last_update_time:.1f}s gap detected, "
                f"resetting flow windows"
            )
            self.flow_5s.reset()
            self.flow_15s.reset()
            self.flow_30s.reset()
            self.tick_count = 0

        self.flow_5s.add(trade)
        self.flow_15s.add(trade)
        self.flow_30s.add(trade)
        self.current_price = trade.price
        self.tick_count += 1
        self.last_update_time = now

    def start_window(self, price: float):
        """Reset tracking for a new prediction market window."""
        self.window_start_price = price
        self.window_start_time = time.time()
        self.current_price = price
        self.tick_count = 0

    @property
    def price_change_pct(self) -> float:
        """Price change since window start."""
        if self.window_start_price <= 0:
            return 0.0
        return (self.current_price - self.window_start_price) / self.window_start_price

    @property
    def momentum_signal(self) -> float:
        """Composite momentum signal from -1 (strong sell) to +1 (strong buy).

        Combines (weights configurable via DB config):
        - Short-term OFI (5s) — default 0.20 (reactive but noisy)
        - Medium-term OFI (15s) — default 0.40 (more stable, primary signal)
        - VWAP drift (15s) — default 0.25
        - Trade intensity surge — default 0.15

        This is the core signal that drives micro sniper decisions.
        """
        if not self.flow_5s.is_active:
            return 0.0

        # OFI signals
        ofi_5 = self.flow_5s.ofi
        ofi_15 = self.flow_15s.ofi

        # VWAP drift — scale to [-1, 1] range
        # A 0.01% drift is a moderate signal for BTC in 15s
        drift = self.flow_15s.vwap_drift
        drift_signal = max(-1.0, min(1.0, drift * 5000))  # ±0.02% -> ±1.0

        # Trade intensity surge: compare 5s rate to 30s rate
        # High intensity = something is happening, strengthens the signal
        int_5 = self.flow_5s.trade_intensity
        int_30 = self.flow_30s.trade_intensity
        if int_30 > 0:
            intensity_ratio = int_5 / int_30
            # Ratio > 2 = surge, < 0.5 = lull
            intensity_signal = max(-1.0, min(1.0, (intensity_ratio - 1.0)))
        else:
            intensity_signal = 0.0

        # The intensity signal amplifies direction, not creates it
        # Use the sign of the dominant OFI
        dominant_direction = 1.0 if ofi_5 > 0 else (-1.0 if ofi_5 < 0 else 0.0)
        intensity_component = intensity_signal * dominant_direction

        signal = (
            self.weight_ofi_5s * ofi_5
            + self.weight_ofi_15s * ofi_15
            + self.weight_vwap_drift * drift_signal
            + self.weight_intensity * intensity_component
        )

        return max(-1.0, min(1.0, signal))

    @property
    def confidence(self) -> float:
        """How confident we are in the momentum signal (0-1).

        Higher when:
        - All time windows agree on direction
        - Trade volume is sufficient
        - Signal is strong
        """
        if not self.flow_5s.is_active:
            return 0.0

        # Agreement across windows
        ofi_5 = self.flow_5s.ofi
        ofi_15 = self.flow_15s.ofi
        ofi_30 = self.flow_30s.ofi

        # All same sign = high agreement
        signs = [1 if x > 0.05 else (-1 if x < -0.05 else 0) for x in [ofi_5, ofi_15, ofi_30]]
        nonzero = [s for s in signs if s != 0]
        if not nonzero:
            return 0.0

        agreement = abs(sum(nonzero)) / len(nonzero)  # 0-1

        # Signal strength
        strength = min(1.0, abs(self.momentum_signal) * 2)

        # Volume sufficiency — at least 10 trades in 15s window
        vol_ok = min(1.0, self.flow_15s.total_count / 10.0)

        return agreement * 0.4 + strength * 0.4 + vol_ok * 0.2


class BinanceAggTradeFeed:
    """Real-time aggTrade feed from Binance WebSocket API.

    Provides tick-level trade data with buy/sell classification for
    microstructure analysis. Maintains rolling MicroStructure state
    per symbol.
    """

    def __init__(self, symbols: list[str] | None = None):
        self.symbols = [s.lower() for s in (symbols or DEFAULT_SYMBOLS)]
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._reconnect_attempts = 0

        # Microstructure state per symbol
        self.micro: dict[str, MicroStructure] = {
            s: MicroStructure(symbol=s) for s in self.symbols
        }

        # Callbacks
        self._callbacks: dict[str, list[Callable]] = {}
        self._global_callbacks: list[Callable] = []

        # Stats
        self.total_trades_processed = 0

    def on_trade(self, symbol: str, callback: Callable):
        """Register a callback for trades on a specific symbol.

        Callback signature: async def handler(trade: AggTrade, micro: MicroStructure)
        """
        sym = symbol.lower()
        if sym not in self._callbacks:
            self._callbacks[sym] = []
        self._callbacks[sym].append(callback)

    def on_any_trade(self, callback: Callable):
        """Register a callback for ALL trades.

        Callback signature: async def handler(trade: AggTrade, micro: MicroStructure)
        """
        self._global_callbacks.append(callback)

    async def start(self):
        """Connect and start consuming aggTrade data."""
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
                    f"aggTrade WS closed ({e.code}), reconnecting in {delay}s "
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
                    f"aggTrade WS error: {e}, reconnecting in {delay}s "
                    f"(attempt {self._reconnect_attempts})"
                )
                await asyncio.sleep(delay)

    async def stop(self):
        """Gracefully disconnect."""
        self._running = False
        if self._ws:
            await self._ws.close()

    def get_micro(self, symbol: str) -> Optional[MicroStructure]:
        """Get microstructure state for a symbol."""
        return self.micro.get(symbol.lower())

    def start_window(self, symbol: str):
        """Start/reset a price tracking window for a symbol."""
        sym = symbol.lower()
        micro = self.micro.get(sym)
        if micro and micro.current_price > 0:
            micro.start_window(micro.current_price)
            logger.info(f"Micro window started for {sym} at ${micro.current_price:,.2f}")

    @property
    def is_connected(self) -> bool:
        if self._ws is None:
            return False
        try:
            return self._ws.state.name == "OPEN"
        except AttributeError:
            return getattr(self._ws, "open", False)

    async def _connect_and_consume(self):
        """Single connection lifecycle."""
        streams = "/".join(f"{s}@aggTrade" for s in self.symbols)
        url = f"{BINANCE_WS_BASE}/stream?streams={streams}"

        async with websockets.connect(url, ping_interval=20) as ws:
            self._ws = ws
            self._reconnect_attempts = 0
            logger.info(
                f"Connected to Binance aggTrade — tracking {len(self.symbols)} symbols: "
                f"{', '.join(s.upper() for s in self.symbols)}"
            )

            async for raw_msg in ws:
                if not self._running:
                    break

                try:
                    data = json.loads(raw_msg)
                except json.JSONDecodeError:
                    continue

                # Combined stream format: {"stream": "btcusdt@aggTrade", "data": {...}}
                stream_data = data.get("data", data)
                await self._handle_agg_trade(stream_data)

            self._ws = None

    async def _handle_agg_trade(self, data: dict):
        """Process a single aggTrade event.

        Binance aggTrade format:
        {
          "e": "aggTrade",     // Event type
          "s": "BTCUSDT",      // Symbol
          "p": "69500.00",     // Price
          "q": "0.001",        // Quantity
          "m": true,           // Is buyer maker? (true = sell pressure)
          "T": 1710000000000,  // Trade time (ms)
        }
        """
        symbol = data.get("s", "").lower()
        if not symbol or symbol not in self.symbols:
            return

        try:
            trade = AggTrade(
                symbol=symbol,
                price=float(data.get("p", 0)),
                quantity=float(data.get("q", 0)),
                is_buyer_maker=data.get("m", False),
                timestamp=float(data.get("T", 0)) / 1000.0,  # ms -> s
            )
        except (ValueError, TypeError):
            return

        if trade.price <= 0 or trade.quantity <= 0:
            return

        # Update microstructure state
        micro = self.micro.get(symbol)
        if micro:
            micro.add_trade(trade)

        self.total_trades_processed += 1

        # Fire symbol-specific callbacks
        for cb in self._callbacks.get(symbol, []):
            try:
                await cb(trade, micro)
            except Exception as e:
                logger.error(f"aggTrade callback error for {symbol}: {e}")

        # Fire global callbacks
        for cb in self._global_callbacks:
            try:
                await cb(trade, micro)
            except Exception as e:
                logger.error(f"Global aggTrade callback error: {e}")
