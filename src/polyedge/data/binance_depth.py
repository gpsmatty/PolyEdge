"""Binance order book depth WebSocket feed — LEADING indicator for micro sniper.

Unlike aggTrade (which shows past filled trades = lagging), the depth stream
shows limit orders being placed and pulled in real-time = LEADING. When bids
start stacking faster than asks, buying pressure is building BEFORE price moves.

Streams:
  @depth20@100ms — top 20 bid/ask levels, snapshot every 100ms
  No REST snapshot needed, no lastUpdateId management.

Key metrics:
  - Near-touch imbalance: bid volume vs ask volume near best price
  - Imbalance velocity: HOW FAST imbalance is changing (the leading signal)
  - Depth delta: are bids growing or shrinking vs asks?
  - Large order detection: sudden volume spikes at specific levels

The imbalance velocity is the core insight: a book that's 60/40 bid-heavy
and stable is NOT a signal. A book that just went from 40/60 to 60/40 in
1 second IS a signal — it means participants are repositioning.
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

logger = logging.getLogger("polyedge.binance_depth")

BINANCE_WS_BASE = "wss://stream.binance.com:9443"

DEFAULT_SYMBOLS = ["btcusdt"]

RECONNECT_DELAY_BASE = 2
RECONNECT_DELAY_MAX = 30


@dataclass
class DepthLevel:
    """A single price level in the order book."""
    price: float
    quantity: float

    @property
    def notional(self) -> float:
        """Dollar value at this level."""
        return self.price * self.quantity


@dataclass
class DepthSnapshot:
    """Top N bids and asks from one @depth20@100ms tick.

    Bids are sorted highest-first (best bid = bids[0]).
    Asks are sorted lowest-first (best ask = asks[0]).
    """
    symbol: str
    bids: list[DepthLevel]
    asks: list[DepthLevel]
    timestamp: float  # time.time() when received

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 0.0

    @property
    def mid_price(self) -> float:
        bb, ba = self.best_bid, self.best_ask
        if bb > 0 and ba > 0:
            return (bb + ba) / 2.0
        return bb or ba

    @property
    def spread_bps(self) -> float:
        """Spread in basis points."""
        mid = self.mid_price
        if mid <= 0:
            return 0.0
        return ((self.best_ask - self.best_bid) / mid) * 10000

    def near_touch_imbalance(self, levels: int = 5) -> float:
        """Imbalance using top N levels on each side.

        Returns (bid_depth - ask_depth) / total. Range [-1, 1].
        Positive = more bid depth near touch = buy pressure.
        """
        bid_depth = sum(b.notional for b in self.bids[:levels])
        ask_depth = sum(a.notional for a in self.asks[:levels])
        total = bid_depth + ask_depth
        if total <= 0:
            return 0.0
        return (bid_depth - ask_depth) / total

    @property
    def total_bid_depth(self) -> float:
        """Total notional on bid side (all levels)."""
        return sum(b.notional for b in self.bids)

    @property
    def total_ask_depth(self) -> float:
        """Total notional on ask side (all levels)."""
        return sum(a.notional for a in self.asks)

    def weighted_imbalance(self, levels: int = 10) -> float:
        """Distance-weighted imbalance — closer levels count more.

        Levels closer to the touch are weighted higher because they
        represent more immediate intent. Level 1 gets weight N,
        level 2 gets weight N-1, etc.
        """
        n = min(levels, len(self.bids), len(self.asks))
        if n == 0:
            return 0.0

        bid_weighted = 0.0
        ask_weighted = 0.0
        for i in range(n):
            weight = n - i  # Closer = higher weight
            bid_weighted += self.bids[i].notional * weight
            ask_weighted += self.asks[i].notional * weight

        total = bid_weighted + ask_weighted
        if total <= 0:
            return 0.0
        return (bid_weighted - ask_weighted) / total


@dataclass
class DepthStructure:
    """Complete order book depth state for a symbol.

    Maintains a rolling window of DepthSnapshots and computes
    velocity/delta metrics that serve as LEADING indicators.

    The key insight: imbalance velocity (how fast the book is tilting)
    predicts price movement better than static imbalance level.
    """
    symbol: str

    # Rolling history of snapshots (100ms each, ~100 = 10 seconds)
    _snapshots: deque[DepthSnapshot] = field(default_factory=lambda: deque(maxlen=200))

    # Latest snapshot for instant reads
    _latest: Optional[DepthSnapshot] = field(default=None)

    # Configurable params (all pushed from DB config, no hardcoded values)
    imbalance_levels: int = 5
    velocity_window_s: float = 3.0
    large_order_threshold: float = 3.0

    # Momentum weights
    weight_imbalance_velocity: float = 0.50
    weight_depth_delta: float = 0.30
    weight_large_order: float = 0.20

    # Signal scaling
    velocity_scale: float = 2.0        # Normalizes velocity into [-1,1]
    pull_scale: float = 5.0            # Scales pull signal (pulls are small %)

    # Confidence weights
    confidence_weight_agreement: float = 0.4
    confidence_weight_strength: float = 0.4
    confidence_weight_data: float = 0.2
    confidence_min_snapshots: int = 10
    confidence_data_ok_snapshots: int = 30

    # Gap detection
    gap_clear_seconds: float = 2.0

    # Stats
    tick_count: int = 0
    last_update_time: float = 0.0  # monotonic

    def add_snapshot(self, snapshot: DepthSnapshot):
        """Add a new depth snapshot and update state."""
        now = time.monotonic()

        # Gap detection — if >500ms between ticks, we probably reconnected
        if self.last_update_time > 0 and (now - self.last_update_time) > 0.5:
            gap = now - self.last_update_time
            if gap > self.gap_clear_seconds:
                logger.info(
                    f"{self.symbol}: {gap:.1f}s depth gap detected, "
                    f"clearing history"
                )
                self._snapshots.clear()

        self._snapshots.append(snapshot)
        self._latest = snapshot
        self.tick_count += 1
        self.last_update_time = now

    @property
    def is_active(self) -> bool:
        """True if we have recent depth data."""
        return self._latest is not None and len(self._snapshots) > 0

    @property
    def imbalance(self) -> float:
        """Current near-touch imbalance. Range [-1, 1]."""
        if not self._latest:
            return 0.0
        return self._latest.near_touch_imbalance(self.imbalance_levels)

    @property
    def weighted_imbalance(self) -> float:
        """Current distance-weighted imbalance. Range [-1, 1]."""
        if not self._latest:
            return 0.0
        return self._latest.weighted_imbalance(self.imbalance_levels * 2)

    def _imbalance_at_age(self, seconds_ago: float) -> Optional[float]:
        """Get the imbalance from approximately N seconds ago."""
        if not self._snapshots:
            return None
        target_time = self._latest.timestamp - seconds_ago
        # Walk backwards to find the closest snapshot
        for snap in reversed(self._snapshots):
            if snap.timestamp <= target_time:
                return snap.near_touch_imbalance(self.imbalance_levels)
        # If all snapshots are newer than target, use the oldest
        return self._snapshots[0].near_touch_imbalance(self.imbalance_levels)

    def _depth_at_age(self, seconds_ago: float) -> Optional[tuple[float, float]]:
        """Get (bid_depth, ask_depth) from approximately N seconds ago."""
        if not self._snapshots:
            return None
        target_time = self._latest.timestamp - seconds_ago
        for snap in reversed(self._snapshots):
            if snap.timestamp <= target_time:
                return (snap.total_bid_depth, snap.total_ask_depth)
        return (self._snapshots[0].total_bid_depth, self._snapshots[0].total_ask_depth)

    @property
    def imbalance_velocity_1s(self) -> float:
        """Rate of change of imbalance over last 1 second.

        Positive = imbalance shifting toward bids (bullish pressure building).
        This is a LEADING indicator — book is repositioning before price moves.
        """
        return self._velocity(1.0)

    @property
    def imbalance_velocity_3s(self) -> float:
        """Rate of change of imbalance over last 3 seconds."""
        return self._velocity(3.0)

    @property
    def imbalance_velocity_5s(self) -> float:
        """Rate of change of imbalance over last 5 seconds."""
        return self._velocity(5.0)

    def _velocity(self, window_s: float) -> float:
        """Compute imbalance velocity over a time window.

        velocity = (current_imbalance - past_imbalance) / window_seconds
        Normalized to roughly [-1, 1] range by clamping.
        """
        if not self._latest or len(self._snapshots) < 2:
            return 0.0
        past = self._imbalance_at_age(window_s)
        if past is None:
            return 0.0
        current = self.imbalance
        # Raw velocity — scale by window to normalize
        # A full swing from -1 to +1 in 1s = velocity 2.0
        raw = (current - past) / window_s
        return max(-2.0, min(2.0, raw))

    @property
    def depth_delta(self) -> float:
        """How are bids growing vs asks over the last 1 second?

        Returns normalized delta: positive = bids growing faster.
        Computed as: (bid_change - ask_change) / max(total_now, total_then)
        """
        if not self._latest or len(self._snapshots) < 5:
            return 0.0
        past = self._depth_at_age(1.0)
        if past is None:
            return 0.0
        past_bid, past_ask = past
        now_bid = self._latest.total_bid_depth
        now_ask = self._latest.total_ask_depth

        bid_change = now_bid - past_bid
        ask_change = now_ask - past_ask

        # Normalize by total depth to get a relative measure
        total = max(now_bid + now_ask, past_bid + past_ask)
        if total <= 0:
            return 0.0

        raw = (bid_change - ask_change) / total
        return max(-1.0, min(1.0, raw))

    @property
    def large_order_signal(self) -> float:
        """Detect sudden large orders appearing on one side.

        Compares the max level size in the latest snapshot to the rolling
        average max level size. A spike on the bid side = bullish intent,
        on ask side = bearish intent.

        Returns [-1, 1]. Positive = large bid appeared, negative = large ask.
        """
        if not self._latest or len(self._snapshots) < 10:
            return 0.0

        # Current max levels
        max_bid = max((b.notional for b in self._latest.bids[:10]), default=0)
        max_ask = max((a.notional for a in self._latest.asks[:10]), default=0)

        # Rolling average max levels (over last ~50 snapshots = 5 seconds)
        n = min(50, len(self._snapshots) - 1)
        if n < 5:
            return 0.0

        avg_max_bid = 0.0
        avg_max_ask = 0.0
        count = 0
        for i in range(len(self._snapshots) - n - 1, len(self._snapshots) - 1):
            if i < 0:
                continue
            snap = self._snapshots[i]
            avg_max_bid += max((b.notional for b in snap.bids[:10]), default=0)
            avg_max_ask += max((a.notional for a in snap.asks[:10]), default=0)
            count += 1

        if count == 0:
            return 0.0
        avg_max_bid /= count
        avg_max_ask /= count

        # Detect spikes
        bid_spike = 0.0
        ask_spike = 0.0
        if avg_max_bid > 0:
            bid_ratio = max_bid / avg_max_bid
            if bid_ratio > self.large_order_threshold:
                bid_spike = min(1.0, (bid_ratio - 1.0) / (self.large_order_threshold))
        if avg_max_ask > 0:
            ask_ratio = max_ask / avg_max_ask
            if ask_ratio > self.large_order_threshold:
                ask_spike = min(1.0, (ask_ratio - 1.0) / (self.large_order_threshold))

        return bid_spike - ask_spike

    @property
    def pull_signal(self) -> float:
        """Detect when depth is being pulled from one side (sellers/buyers retreating).

        Compares current total depth to 1-second-ago depth on each side.
        A sudden drop in ask depth = sellers retreating = bullish.
        A sudden drop in bid depth = buyers retreating = bearish.

        Returns [-1, 1]. Positive = asks pulled (bullish), negative = bids pulled.
        """
        if not self._latest or len(self._snapshots) < 10:
            return 0.0

        past = self._depth_at_age(1.0)
        if past is None:
            return 0.0

        past_bid, past_ask = past
        now_bid = self._latest.total_bid_depth
        now_ask = self._latest.total_ask_depth

        # Only care about drops (pulls), not additions
        bid_drop = max(0, past_bid - now_bid)
        ask_drop = max(0, past_ask - now_ask)

        # Normalize by the larger of the two past depths
        norm = max(past_bid, past_ask)
        if norm <= 0:
            return 0.0

        bid_pull_pct = bid_drop / norm
        ask_pull_pct = ask_drop / norm

        # Ask pull = bullish (sellers retreating), bid pull = bearish
        raw = ask_pull_pct - bid_pull_pct
        return max(-1.0, min(1.0, raw * self.pull_scale))

    @property
    def depth_momentum(self) -> float:
        """Composite depth-based momentum signal. Range [-1, 1].

        Positive = bullish book pressure building.
        Negative = bearish book pressure building.

        Weighted combination of:
        - Imbalance velocity (how fast book is tilting) — leading signal
        - Depth delta (bid/ask growth differential)
        - Large order detection
        """
        if not self.is_active:
            return 0.0

        # Primary signal: imbalance velocity at the configured window
        velocity = self._velocity(self.velocity_window_s)
        # Normalize velocity to [-1, 1] — typical range is [-0.5, 0.5]
        velocity_signal = max(-1.0, min(1.0, velocity * self.velocity_scale))

        delta = self.depth_delta
        large = self.large_order_signal

        raw = (
            self.weight_imbalance_velocity * velocity_signal
            + self.weight_depth_delta * delta
            + self.weight_large_order * large
        )

        return max(-1.0, min(1.0, raw))

    @property
    def confidence(self) -> float:
        """How confident we are in the depth signal. Range [0, 1].

        Higher when:
        - Multiple depth metrics agree
        - We have sufficient history
        - Signal is strong
        """
        if not self.is_active or len(self._snapshots) < self.confidence_min_snapshots:
            return 0.0

        # Agreement: do velocity, delta, and pull all point the same way?
        v = self._velocity(self.velocity_window_s)
        d = self.depth_delta
        p = self.pull_signal

        signs = []
        for val in [v, d, p]:
            if val > 0.05:
                signs.append(1)
            elif val < -0.05:
                signs.append(-1)
            else:
                signs.append(0)

        nonzero = [s for s in signs if s != 0]
        if not nonzero:
            return 0.0

        # Agreement: what fraction point the same way?
        agreement = abs(sum(nonzero)) / len(signs)

        # Strength
        strength = min(1.0, abs(self.depth_momentum) * self.velocity_scale)

        # Data sufficiency
        data_ok = min(1.0, len(self._snapshots) / max(1, self.confidence_data_ok_snapshots))

        return (
            agreement * self.confidence_weight_agreement
            + strength * self.confidence_weight_strength
            + data_ok * self.confidence_weight_data
        )

    @property
    def latest_mid(self) -> float:
        """Latest mid price."""
        if self._latest:
            return self._latest.mid_price
        return 0.0

    def reset(self):
        """Clear all data (e.g., on window hop)."""
        self._snapshots.clear()
        self._latest = None
        self.tick_count = 0


class BinanceDepthFeed:
    """Real-time order book depth feed from Binance WebSocket API.

    Subscribes to @depth20@100ms — top 20 bid/ask levels every 100ms.
    Maintains DepthStructure state per symbol with rolling metrics.

    This is the LEADING indicator counterpart to BinanceAggTradeFeed.
    aggTrade tells you what already happened. Depth tells you what's
    about to happen.
    """

    def __init__(self, symbols: list[str] | None = None):
        self.symbols = [s.lower() for s in (symbols or DEFAULT_SYMBOLS)]
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._reconnect_attempts = 0

        # Depth state per symbol
        self.depth: dict[str, DepthStructure] = {
            s: DepthStructure(symbol=s) for s in self.symbols
        }

        # Callbacks
        self._callbacks: dict[str, list[Callable]] = {}
        self._global_callbacks: list[Callable] = []

        # Stats
        self.total_ticks_processed = 0

    def on_depth(self, symbol: str, callback: Callable):
        """Register a callback for depth updates on a specific symbol.

        Callback signature: async def handler(snapshot: DepthSnapshot, depth: DepthStructure)
        """
        sym = symbol.lower()
        if sym not in self._callbacks:
            self._callbacks[sym] = []
        self._callbacks[sym].append(callback)

    def on_any_depth(self, callback: Callable):
        """Register a callback for ALL depth updates.

        Callback signature: async def handler(snapshot: DepthSnapshot, depth: DepthStructure)
        """
        self._global_callbacks.append(callback)

    async def start(self):
        """Connect and start consuming depth data."""
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
                    f"Depth WS closed ({e.code}), reconnecting in {delay}s "
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
                    f"Depth WS error: {e}, reconnecting in {delay}s "
                    f"(attempt {self._reconnect_attempts})"
                )
                await asyncio.sleep(delay)

    async def stop(self):
        """Gracefully disconnect."""
        self._running = False
        if self._ws:
            await self._ws.close()

    def get_depth(self, symbol: str) -> Optional[DepthStructure]:
        """Get depth state for a symbol."""
        return self.depth.get(symbol.lower())

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
        streams = "/".join(f"{s}@depth20@100ms" for s in self.symbols)
        url = f"{BINANCE_WS_BASE}/stream?streams={streams}"

        async with websockets.connect(url, ping_interval=20) as ws:
            self._ws = ws
            self._reconnect_attempts = 0
            logger.info(
                f"Connected to Binance depth — tracking {len(self.symbols)} symbols: "
                f"{', '.join(s.upper() for s in self.symbols)}"
            )

            async for raw_msg in ws:
                if not self._running:
                    break

                try:
                    data = json.loads(raw_msg)
                except json.JSONDecodeError:
                    continue

                # Combined stream format: {"stream": "btcusdt@depth20@100ms", "data": {...}}
                stream_name = data.get("stream", "")
                stream_data = data.get("data", data)

                # Extract symbol from stream name: "btcusdt@depth20@100ms" -> "btcusdt"
                symbol = stream_name.split("@")[0] if "@" in stream_name else ""
                if not symbol:
                    # Fallback: try to infer from the data
                    # depth20 doesn't include a symbol field, so we rely on stream name
                    continue

                await self._handle_depth(symbol, stream_data)

            self._ws = None

    async def _handle_depth(self, symbol: str, data: dict):
        """Process a single @depth20 snapshot.

        Binance @depth20@100ms format:
        {
          "lastUpdateId": 160,
          "bids": [["0.0024", "10"], ...],  // [price, qty], sorted best-first
          "asks": [["0.0026", "100"], ...], // [price, qty], sorted best-first
        }
        """
        if symbol not in self.symbols:
            return

        try:
            bids = [
                DepthLevel(price=float(b[0]), quantity=float(b[1]))
                for b in data.get("bids", [])
                if float(b[0]) > 0 and float(b[1]) > 0
            ]
            asks = [
                DepthLevel(price=float(a[0]), quantity=float(a[1]))
                for a in data.get("asks", [])
                if float(a[0]) > 0 and float(a[1]) > 0
            ]
        except (ValueError, TypeError, IndexError):
            return

        if not bids or not asks:
            return

        snapshot = DepthSnapshot(
            symbol=symbol,
            bids=bids,
            asks=asks,
            timestamp=time.time(),
        )

        # Update depth state
        depth = self.depth.get(symbol)
        if depth:
            depth.add_snapshot(snapshot)

        self.total_ticks_processed += 1

        # Fire symbol-specific callbacks
        for cb in self._callbacks.get(symbol, []):
            try:
                await cb(snapshot, depth)
            except Exception as e:
                logger.error(f"Depth callback error for {symbol}: {e}")

        # Fire global callbacks
        for cb in self._global_callbacks:
            try:
                await cb(snapshot, depth)
            except Exception as e:
                logger.error(f"Global depth callback error: {e}")
