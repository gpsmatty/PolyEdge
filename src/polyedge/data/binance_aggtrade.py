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

        # Guard against floating point drift from thousands of subtractions.
        # Negative volumes flip OFI sign — a phantom signal.
        self.buy_volume = max(0.0, self.buy_volume)
        self.sell_volume = max(0.0, self.sell_volume)
        self.vwap_numerator = max(0.0, self.vwap_numerator)
        self.total_quantity = max(0.0, self.total_quantity)

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
    def price_range_pct(self) -> float:
        """Price range (high - low) / mid as a fraction over the window.

        Returns 0.0 if insufficient data. Used for chop detection:
        a high range with low net movement = choppy market.
        """
        if len(self._trades) < 2:
            return 0.0
        prices = [t.price for t in self._trades]
        hi = max(prices)
        lo = min(prices)
        mid = (hi + lo) / 2.0
        if mid <= 0:
            return 0.0
        return (hi - lo) / mid

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

    # Persistent 5-minute flow window — NEVER reset on window hops.
    # This provides cross-window context: "BTC has been trending up for
    # the last 5 minutes" even when we just hopped to a new Polymarket window.
    # The short windows (5s/15s/30s) reset on hop for clean per-window signals.
    flow_5m: TradeFlowWindow = field(default=None)

    # Price tracking for the current prediction market window
    window_start_price: float = 0.0
    window_start_time: float = 0.0
    current_price: float = 0.0
    tick_count: int = 0
    last_update_time: float = 0.0  # monotonic time of last aggTrade

    # Persistent price context loaded from DB on startup.
    # List of (price, timestamp) tuples from micro_price_log.
    # Updated on startup and periodically as new snapshots are logged.
    price_history: list = field(default_factory=list)

    # Configurable momentum weights (set from MicroSniperConfig at init)
    weight_ofi_5s: float = 0.10
    weight_ofi_15s: float = 0.50
    weight_vwap_drift: float = 0.25
    weight_intensity: float = 0.15

    # Score shaping params (set from MicroSniperConfig at init)
    vwap_drift_scale: float = 2000.0
    dampener_agree_factor: float = 1.0
    dampener_disagree_factor: float = 0.4
    dampener_flat_factor: float = 0.65
    dampener_price_deadzone: float = 0.05

    def __post_init__(self):
        if self.flow_5s is None:
            self.flow_5s = TradeFlowWindow(symbol=self.symbol, window_seconds=5.0)
        if self.flow_15s is None:
            self.flow_15s = TradeFlowWindow(symbol=self.symbol, window_seconds=15.0)
        if self.flow_30s is None:
            self.flow_30s = TradeFlowWindow(symbol=self.symbol, window_seconds=30.0)
        if self.flow_5m is None:
            self.flow_5m = TradeFlowWindow(symbol=self.symbol, window_seconds=300.0)

    def add_trade(self, trade: AggTrade):
        """Add a trade to all windows.

        Detects gaps (>5s since last trade) which indicate a WebSocket
        disconnect/reconnect. After a gap, reset short windows to avoid
        stale momentum signals from pre-disconnect data. The persistent
        5m window is NOT reset on gaps — it auto-prunes old data anyway.
        """
        now = time.monotonic()
        if self.last_update_time > 0 and (now - self.last_update_time) > 5.0:
            # Gap detected — WS was likely disconnected. Reset SHORT windows
            # so we don't trade on stale momentum from before the gap.
            # The 5m persistent window is left alone — it self-prunes and
            # provides cross-window context that survives brief disconnects.
            logger.info(
                f"{self.symbol}: {now - self.last_update_time:.1f}s gap detected, "
                f"resetting short flow windows"
            )
            self.flow_5s.reset()
            self.flow_15s.reset()
            self.flow_30s.reset()
            self.tick_count = 0

        self.flow_5s.add(trade)
        self.flow_15s.add(trade)
        self.flow_30s.add(trade)
        self.flow_5m.add(trade)
        self.current_price = trade.price
        self.tick_count += 1
        self.last_update_time = now

    def start_window(self, price: float):
        """Reset tracking for a new prediction market window.

        Only resets per-window state. The persistent 5m flow window is
        deliberately NOT reset — it provides cross-window context.
        """
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
    def trend_5m(self) -> float:
        """5-minute price trend from the persistent flow window.

        Returns fractional change: (current - oldest_in_5m) / oldest.
        Positive = BTC trending up over 5 minutes.
        Falls back to DB-loaded price_history if flow_5m has no data yet
        (e.g., right after startup before 5 min of live data).
        """
        # First try live 5m window
        if self.flow_5m.is_active and self.flow_5m._trades:
            oldest = self.flow_5m._trades[0].price
            newest = self.current_price
            if oldest > 0 and newest > 0:
                return (newest - oldest) / oldest

        # Fall back to DB-loaded price history — use only the last 5 minutes.
        # price_history can hold up to 30 minutes of snapshots; using price_history[0]
        # (the oldest) would make this a 30-minute trend, which is far too wide for
        # a threshold calibrated against 5-minute moves (0.30% in 30m is common,
        # in 5m it's a genuine strong move).
        if self.price_history and self.current_price > 0:
            cutoff = time.time() - 300  # 5 minutes ago
            # Walk forward from oldest to find the oldest snapshot within 5 minutes
            ref_price = None
            for price, ts in self.price_history:
                if ts >= cutoff:
                    ref_price = price
                    break
            # If all snapshots are older than 5m, use the most recent one as a
            # best-effort approximation rather than returning a stale 30m value.
            if ref_price is None and self.price_history:
                ref_price = self.price_history[-1][0]
            if ref_price and ref_price > 0:
                return (self.current_price - ref_price) / ref_price

        return 0.0

    def trend_lookback(self, minutes: float = 30.0) -> float:
        """Price trend over a configurable lookback period using DB price history.

        Returns fractional change: (current - oldest_in_window) / oldest.
        Positive = BTC trending up over the lookback period.
        Uses price_history from micro_price_log (30-60min of snapshots).

        Args:
            minutes: How far back to look (default 30 minutes).

        Returns:
            Fractional price change, or 0.0 if insufficient data.
        """
        import time as _time

        if not self.price_history or self.current_price <= 0:
            return 0.0

        cutoff = _time.time() - (minutes * 60)

        # Find the oldest price at or after the cutoff
        # price_history is [(price, timestamp), ...] sorted oldest-first
        for price, ts in self.price_history:
            if ts >= cutoff and price > 0:
                return (self.current_price - price) / price

        # All history is older than cutoff — use the newest entry as best effort
        if self.price_history:
            price, ts = self.price_history[-1]
            if price > 0:
                return (self.current_price - price) / price

        return 0.0

    @property
    def ofi_5m(self) -> float:
        """5-minute aggregate OFI from the persistent flow window."""
        return self.flow_5m.ofi if self.flow_5m.is_active else 0.0

    @property
    def trend_direction(self) -> str:
        """Human-readable trend direction: 'up', 'down', or 'flat'."""
        t = self.trend_5m
        if t > 0.001:  # >0.1%
            return "up"
        elif t < -0.001:
            return "down"
        return "flat"

    @property
    def chop_index(self) -> float:
        """Chop index: how much range vs directional movement over 5 minutes.

        Ratio of price range to abs(net movement). High values (>3) = choppy
        (big swings, little net direction). Low values (~1) = trending.

        Returns 0.0 if insufficient data.
        Used to auto-scale entry thresholds: choppy → raise threshold.
        """
        if not self.flow_5m.is_active or len(self.flow_5m._trades) < 10:
            return 0.0

        range_pct = self.flow_5m.price_range_pct
        net_move = abs(self.trend_5m)

        if net_move < 0.00005:  # <0.005% net move = basically flat
            # Range with no direction = pure chop
            return range_pct * 10000  # scale to meaningful numbers (0.1% range → 1.0)
        return range_pct / net_move if net_move > 0 else 0.0

    @property
    def momentum_signal(self) -> float:
        """Composite momentum signal from -1 (strong sell) to +1 (strong buy).

        Combines (weights configurable via DB config):
        - Short-term OFI (5s) — default 0.10 (reactive but noisy)
        - Medium-term OFI (15s) — default 0.50 (more stable, primary signal)
        - VWAP drift (15s) — default 0.25
        - Trade intensity surge — default 0.15

        Key design: OFI tells us WHO is trading. VWAP drift tells us IF PRICE
        MOVED. When OFI is extreme but price didn't move, the flow was absorbed
        by the book — that's not a signal, that's noise. The flow-price
        agreement dampener handles this: continuous scaling from disagree_factor
        (flow opposed price) through flat_factor (price didn't move) to
        agree_factor (flow confirmed by price).
        """
        if not self.flow_5s.is_active:
            return 0.0

        # OFI signals
        ofi_5 = self.flow_5s.ofi
        ofi_15 = self.flow_15s.ofi

        # VWAP drift — scale to [-1, 1] range
        # vwap_drift_scale controls sensitivity: higher = reacts to smaller moves
        drift = self.flow_15s.vwap_drift
        drift_signal = max(-1.0, min(1.0, drift * self.vwap_drift_scale))

        # Trade intensity surge: compare 5s rate to 30s rate
        # High intensity = something is happening, strengthens the signal
        int_5 = self.flow_5s.trade_intensity
        int_30 = self.flow_30s.trade_intensity
        if int_30 > 0:
            intensity_ratio = int_5 / int_30
            intensity_signal = max(-1.0, min(1.0, (intensity_ratio - 1.0)))
        else:
            intensity_signal = 0.0

        # The intensity signal amplifies direction, not creates it
        dominant_direction = 1.0 if ofi_5 > 0 else (-1.0 if ofi_5 < 0 else 0.0)
        intensity_component = intensity_signal * dominant_direction

        raw_signal = (
            self.weight_ofi_5s * ofi_5
            + self.weight_ofi_15s * ofi_15
            + self.weight_vwap_drift * drift_signal
            + self.weight_intensity * intensity_component
        )

        # --- Flow-price agreement dampener (continuous) ---
        # Measures how well OFI direction is confirmed by actual price movement.
        # "Aggressive flow that doesn't displace price was absorbed — not edge."
        #
        # Computes a continuous alignment score from -1 (fully opposed) to +1
        # (fully aligned), then maps it smoothly to the dampener factor range.
        abs_drift = abs(drift_signal)

        if abs(ofi_15) < 0.05:
            # No meaningful OFI — no dampening needed
            agreement_factor = 1.0
        elif abs_drift < self.dampener_price_deadzone:
            # OFI present but price flat — use flat factor
            # Interpolate between flat and agree based on how much OFI there is
            # Strong OFI + flat price = more suspicious
            agreement_factor = self.dampener_flat_factor
        else:
            # Both OFI and price have direction — measure alignment
            # alignment: +1 = same direction, -1 = opposite direction
            ofi_sign = 1.0 if ofi_15 > 0 else -1.0
            price_sign = 1.0 if drift_signal > 0 else -1.0
            alignment = ofi_sign * price_sign  # +1 or -1

            # Scale by price strength (stronger price confirmation = more extreme factor)
            # price_strength: 0 at deadzone edge, 1 at full saturation
            price_strength = min(1.0, (abs_drift - self.dampener_price_deadzone) / (1.0 - self.dampener_price_deadzone))

            if alignment > 0:
                # Agree: interpolate flat_factor → agree_factor as price strengthens
                agreement_factor = self.dampener_flat_factor + (self.dampener_agree_factor - self.dampener_flat_factor) * price_strength
            else:
                # Disagree: interpolate flat_factor → disagree_factor as price strengthens
                agreement_factor = self.dampener_flat_factor + (self.dampener_disagree_factor - self.dampener_flat_factor) * price_strength

        signal = raw_signal * agreement_factor

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

        # Use len(signs) not len(nonzero) — otherwise a single active window
        # gives agreement=1.0 (1/1) instead of 0.33 (1/3).  Flat windows
        # (ofi near zero) should reduce agreement, not be ignored.
        agreement = abs(sum(nonzero)) / len(signs)  # 0-1

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
