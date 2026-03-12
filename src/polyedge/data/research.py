"""Research pipeline — market-state intelligence layer for the micro sniper.

Logs structured snapshots of market state and strategy signals continuously,
not just at trade time. This enables:
  - Replay: what did the bot see at any moment?
  - Attribution: which signal components drove wins/losses?
  - Regime analysis: where does the strategy work/fail?
  - Candidate-event analysis: what almost-signals looked like
  - Outcome labeling: what happened after each snapshot?

Design principles (from external audit):
  1. Log first, validate later — don't assume what matters
  2. Define prediction targets before features
  3. Simple deterministic regime labels (no ML, no narrative)
  4. Versioned feature schema for future-proof backtesting
  5. Event-driven + time-based hybrid snapshots

Schema version: 1 — bump when adding/removing/renaming fields.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

logger = logging.getLogger("polyedge.research")

# Bump this when snapshot schema changes.
# Stored with every row so backtests know which fields were available.
SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Regime labels — simple, deterministic, stable
# ---------------------------------------------------------------------------

class Regime(str, Enum):
    """Market regime classification.

    Computed deterministically from stored features.
    Must be stable — don't mutate definitions after bad sessions.
    """
    TREND_UP = "trend_up"           # 5m trend > +0.08%
    TREND_DOWN = "trend_down"       # 5m trend < -0.08%
    CHOP = "chop"                   # Moderate activity + frequent OFI flips
    VOL_EXPANSION = "vol_expansion" # Intensity surge + wide price swings
    LOW_VOL = "low_vol"             # Low intensity + tight range
    NORMAL = "normal"               # Active market, no strong trend or chop
    UNKNOWN = "unknown"             # Not enough data


def classify_regime(
    trend_5m: float,
    intensity_5s: float,
    intensity_30s: float,
    price_change_pct: float,
    ofi_flip_count_30s: int,
) -> Regime:
    """Classify current market regime from observable features.

    All thresholds are hardcoded and stable — not tuned per-session.
    The regime label can be recomputed deterministically from stored features.

    Args:
        trend_5m: 5-minute price trend (fractional, e.g. 0.002 = 0.2%)
        intensity_5s: Trades per second in 5s window
        intensity_30s: Trades per second in 30s window
        price_change_pct: Price change since window start (fractional)
        ofi_flip_count_30s: Number of OFI sign flips in last 30 seconds
    """
    abs_trend = abs(trend_5m)

    # Volatility expansion: intensity surge (5s >> 30s) + big price swing
    if intensity_30s > 0 and intensity_5s / intensity_30s > 2.0 and abs(price_change_pct) > 0.002:
        return Regime.VOL_EXPANSION

    # Trend: 5-minute directional move (lowered from 0.15% to 0.08%)
    if abs_trend > 0.0008:
        return Regime.TREND_UP if trend_5m > 0 else Regime.TREND_DOWN

    # Chop: frequent OFI direction flips (lowered from 4 to 3 flips)
    if ofi_flip_count_30s >= 3:
        return Regime.CHOP

    # Low vol: low intensity + tight range
    if intensity_30s < 5.0 and abs(price_change_pct) < 0.0005:
        return Regime.LOW_VOL

    # Normal: active market, no strong trend or chop signal
    if intensity_30s >= 5.0:
        return Regime.NORMAL

    # Default: not enough data to classify
    return Regime.UNKNOWN


# ---------------------------------------------------------------------------
# No-trade reasons — why a candidate didn't become a trade
# ---------------------------------------------------------------------------

class NoTradeReason(str, Enum):
    """Dominant blocker when a signal candidate fails to enter."""
    BELOW_THRESHOLD = "below_threshold"
    FAILED_PERSISTENCE = "failed_persistence"
    CONFIDENCE_TOO_LOW = "confidence_too_low"
    TREND_VETO = "trend_veto"              # 5m trend hard block
    COUNTER_TREND_BOOST = "counter_trend_boost"  # 30s counter-trend raised threshold
    PRICE_BAND = "price_band"              # Outside min/max entry price
    DEAD_MARKET = "dead_market"            # YES stuck near 0.50
    COOLDOWN = "cooldown"                  # Trade cooldown active
    LIQUIDITY = "liquidity"                # Insufficient liquidity
    MAX_TRADES = "max_trades"              # Hit max trades per window
    WINDOW_HOP_COOLDOWN = "window_hop_cooldown"
    MIN_SECONDS = "min_seconds"            # Too close to window end
    ACCELERATION = "acceleration"          # Momentum fading (not accelerating)
    PRICE_TO_BEAT = "price_to_beat"        # Fighting the window direction
    BOOK_VETO = "book_veto"                # Polymarket book disagrees
    BOOK_NO_LIQUIDITY = "book_no_liquidity"  # Thin exit book
    WARMUP = "warmup"                      # Waiting for fresh window
    FOK_REJECTED = "fok_rejected"          # FOK order couldn't fill
    SPARSE_DATA = "sparse_data"            # Not enough trades in window
    LOW_VOL = "low_vol"                    # Low volatility regime — momentum is noise
    NONE = "none"                          # Trade was taken (no block)


# ---------------------------------------------------------------------------
# Signal snapshot — the full feature vector at a point in time
# ---------------------------------------------------------------------------

@dataclass
class SignalSnapshot:
    """Complete market + strategy state at a single moment.

    This is the core research record. Logged continuously (every 2-3s)
    plus event-driven (threshold crossings, entry fires, exit triggers).

    Every field must be available in real-time — no lookahead.
    """
    # Identity
    timestamp: float                 # Unix timestamp (time.time())
    schema_version: int = SCHEMA_VERSION
    session_id: str = ""             # Groups snapshots within a single bot run
    symbol: str = ""                 # e.g. "btcusdt"
    market_id: str = ""              # condition_id of active window
    window_question: str = ""        # Human-readable window name

    # Binance price state
    btc_price: float = 0.0
    price_change_pct: float = 0.0    # From window start
    window_start_price: float = 0.0

    # Polymarket price state
    yes_price: float = 0.0
    no_price: float = 0.0

    # Raw signal components (pre-dampener)
    ofi_5s: float = 0.0
    ofi_15s: float = 0.0
    ofi_30s: float = 0.0
    ofi_5m: float = 0.0
    vwap_drift: float = 0.0          # Raw 15s VWAP drift
    vwap_drift_scaled: float = 0.0   # After vwap_drift_scale multiplication
    intensity_5s: float = 0.0        # Trades per second
    intensity_30s: float = 0.0

    # Composite signals
    raw_momentum: float = 0.0        # Before dampener
    dampener_factor: float = 1.0     # The agreement dampener multiplier
    dampened_momentum: float = 0.0   # After dampener (= what strategy sees)
    confidence: float = 0.0

    # Trend context
    trend_5m: float = 0.0            # 5-minute fractional price change
    trend_30s_ofi: float = 0.0       # 30s OFI (used for counter-trend filter)

    # Time context
    seconds_remaining: float = 0.0
    window_elapsed_pct: float = 0.0  # 0.0 = window start, 1.0 = window end

    # Regime
    regime: str = "unknown"

    # Position state
    current_position: str = ""       # "yes", "no", or "" (flat)
    entry_price: float = 0.0         # If holding, our entry price
    high_water_mark: float = 0.0     # If holding, best price seen
    unrealized_pnl_pct: float = 0.0  # If holding, % P&L from entry

    # Liquidity / spread
    spread: float = 0.0
    liquidity: float = 0.0

    # Event classification
    event_type: str = "periodic"     # "periodic", "threshold_cross", "entry",
                                     # "exit", "candidate", "persistence_start",
                                     # "persistence_reset", "window_hop", "gap_reset"

    # Trade decision (filled in when a trade fires or is blocked)
    trade_fired: bool = False
    trade_side: str = ""             # "yes" or "no" if trade fired
    trade_action: str = ""           # "buy_yes", "buy_no", "exit", "flip_yes", "flip_no"
    exit_reason: str = ""            # "trailing_stop", "reversal", "force_exit", "floor_exit", "faded"
    no_trade_reason: str = "none"    # NoTradeReason value

    # Near-threshold tracking (for candidate-event logging)
    near_threshold: bool = False     # Was momentum within 0.05 of entry threshold?
    distance_to_threshold: float = 0.0  # How far from firing

    # Attribution components (only populated at trade time)
    attribution: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Serialize for DB storage (JSONB-compatible)."""
        return {
            "timestamp": self.timestamp,
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "symbol": self.symbol,
            "market_id": self.market_id,
            "window_question": self.window_question,
            "btc_price": round(self.btc_price, 2),
            "price_change_pct": round(self.price_change_pct, 6),
            "window_start_price": round(self.window_start_price, 2),
            "yes_price": round(self.yes_price, 4),
            "no_price": round(self.no_price, 4),
            "ofi_5s": round(self.ofi_5s, 4),
            "ofi_15s": round(self.ofi_15s, 4),
            "ofi_30s": round(self.ofi_30s, 4),
            "ofi_5m": round(self.ofi_5m, 4),
            "vwap_drift": round(self.vwap_drift, 8),
            "vwap_drift_scaled": round(self.vwap_drift_scaled, 4),
            "intensity_5s": round(self.intensity_5s, 2),
            "intensity_30s": round(self.intensity_30s, 2),
            "raw_momentum": round(self.raw_momentum, 4),
            "dampener_factor": round(self.dampener_factor, 4),
            "dampened_momentum": round(self.dampened_momentum, 4),
            "confidence": round(self.confidence, 4),
            "trend_5m": round(self.trend_5m, 6),
            "trend_30s_ofi": round(self.trend_30s_ofi, 4),
            "seconds_remaining": round(self.seconds_remaining, 1),
            "window_elapsed_pct": round(self.window_elapsed_pct, 4),
            "regime": self.regime,
            "current_position": self.current_position,
            "entry_price": round(self.entry_price, 4),
            "high_water_mark": round(self.high_water_mark, 4),
            "unrealized_pnl_pct": round(self.unrealized_pnl_pct, 4),
            "spread": round(self.spread, 4),
            "liquidity": round(self.liquidity, 2),
            "event_type": self.event_type,
            "trade_fired": self.trade_fired,
            "trade_side": self.trade_side,
            "trade_action": self.trade_action,
            "exit_reason": self.exit_reason,
            "no_trade_reason": self.no_trade_reason,
            "near_threshold": self.near_threshold,
            "distance_to_threshold": round(self.distance_to_threshold, 4),
            "attribution": self.attribution,
        }


# ---------------------------------------------------------------------------
# Attribution — decompose wins/losses by signal component
# ---------------------------------------------------------------------------

def compute_attribution(
    ofi_5s: float,
    ofi_15s: float,
    vwap_drift_scaled: float,
    intensity_component: float,
    dampener_factor: float,
    weight_ofi_5s: float,
    weight_ofi_15s: float,
    weight_vwap_drift: float,
    weight_intensity: float,
    trade_side: str,  # "yes" or "no"
) -> dict:
    """Compute per-component attribution for a trade decision.

    For each component, shows:
    - raw: the raw signal value
    - weighted: after weight multiplication
    - contribution_pct: % of total absolute weighted signal
    - direction_agreed: did this component agree with the trade direction?

    This lets you answer "did OFI carry this trade?" or "did the dampener save us?"
    """
    is_bullish = trade_side == "yes"
    components = {
        "ofi_5s": {"raw": ofi_5s, "weight": weight_ofi_5s},
        "ofi_15s": {"raw": ofi_15s, "weight": weight_ofi_15s},
        "vwap_drift": {"raw": vwap_drift_scaled, "weight": weight_vwap_drift},
        "intensity": {"raw": intensity_component, "weight": weight_intensity},
    }

    total_abs = 0.0
    for name, comp in components.items():
        comp["weighted"] = round(comp["raw"] * comp["weight"], 4)
        total_abs += abs(comp["weighted"])

    for name, comp in components.items():
        if total_abs > 0:
            comp["contribution_pct"] = round(abs(comp["weighted"]) / total_abs * 100, 1)
        else:
            comp["contribution_pct"] = 0.0

        # Did this component agree with trade direction?
        if is_bullish:
            comp["direction_agreed"] = comp["weighted"] > 0
        else:
            comp["direction_agreed"] = comp["weighted"] < 0

    return {
        "components": {k: {kk: vv for kk, vv in v.items()} for k, v in components.items()},
        "dampener_factor": round(dampener_factor, 4),
        "dampener_saved": dampener_factor < 0.8,  # Dampener meaningfully reduced signal
        "total_pre_dampener": round(sum(c["weighted"] for c in components.values()), 4),
    }


# ---------------------------------------------------------------------------
# OFI flip counter — for regime classification
# ---------------------------------------------------------------------------

class OFIFlipTracker:
    """Counts how many times OFI flips sign in a rolling window.

    High flip count = chop. Low flip count = trend.
    """

    def __init__(self, window_seconds: float = 30.0):
        self.window_seconds = window_seconds
        self._flips: list[float] = []  # timestamps of sign flips
        self._last_sign: int = 0       # +1 or -1

    def update(self, ofi: float, now: float):
        """Call on each eval tick with current OFI value."""
        # Determine sign (ignore near-zero)
        if ofi > 0.05:
            sign = 1
        elif ofi < -0.05:
            sign = -1
        else:
            return  # Dead zone — don't count as flip

        if self._last_sign != 0 and sign != self._last_sign:
            self._flips.append(now)
        self._last_sign = sign

        # Prune old flips
        cutoff = now - self.window_seconds
        while self._flips and self._flips[0] < cutoff:
            self._flips.pop(0)

    @property
    def flip_count(self) -> int:
        return len(self._flips)


# ---------------------------------------------------------------------------
# Research Logger — the main interface for micro_runner
# ---------------------------------------------------------------------------

class ResearchLogger:
    """Manages all research pipeline logging.

    Instantiated once by MicroRunner. Provides methods for:
    - Periodic snapshots (every 2-3 seconds)
    - Event-driven snapshots (threshold crosses, trades, etc.)
    - Candidate-event logging (almost-signals)
    - No-trade reason logging
    - Session management

    All logging is async and fire-and-forget — never blocks trading.
    """

    def __init__(self, db, session_id: str = ""):
        self.db = db
        self.session_id = session_id or f"s_{int(time.time())}"
        self._last_periodic_time: dict[str, float] = {}  # symbol -> last log time
        self._ofi_trackers: dict[str, OFIFlipTracker] = {}
        self._snapshot_buffer: list[dict] = []
        self._buffer_flush_interval = 5.0  # seconds
        self._last_flush_time = time.time()
        self._total_snapshots = 0
        self._total_candidates = 0
        self._total_no_trade = 0
        self._total_trades = 0

    def get_ofi_tracker(self, symbol: str) -> OFIFlipTracker:
        if symbol not in self._ofi_trackers:
            self._ofi_trackers[symbol] = OFIFlipTracker()
        return self._ofi_trackers[symbol]

    def build_snapshot(
        self,
        micro,  # MicroStructure
        market,  # Market
        seconds_remaining: float,
        window_duration: float,
        current_position: str = "",
        entry_price: float = 0.0,
        high_water_mark: float = 0.0,
        event_type: str = "periodic",
    ) -> SignalSnapshot:
        """Build a complete SignalSnapshot from current state.

        All data must be real-time available — no lookahead.
        """
        # Compute raw momentum components (mirror MicroStructure.momentum_signal)
        ofi_5 = micro.flow_5s.ofi if micro.flow_5s.is_active else 0.0
        ofi_15 = micro.flow_15s.ofi if micro.flow_15s.is_active else 0.0
        ofi_30 = micro.flow_30s.ofi if micro.flow_30s.is_active else 0.0

        drift = micro.flow_15s.vwap_drift if micro.flow_15s.is_active else 0.0
        drift_scaled = max(-1.0, min(1.0, drift * micro.vwap_drift_scale))

        int_5 = micro.flow_5s.trade_intensity if micro.flow_5s.is_active else 0.0
        int_30 = micro.flow_30s.trade_intensity if micro.flow_30s.is_active else 0.0

        # Intensity component (same logic as momentum_signal)
        if int_30 > 0:
            intensity_ratio = int_5 / int_30
            intensity_signal = max(-1.0, min(1.0, (intensity_ratio - 1.0)))
        else:
            intensity_signal = 0.0
        dominant_direction = 1.0 if ofi_5 > 0 else (-1.0 if ofi_5 < 0 else 0.0)
        intensity_component = intensity_signal * dominant_direction

        raw_signal = (
            micro.weight_ofi_5s * ofi_5
            + micro.weight_ofi_15s * ofi_15
            + micro.weight_vwap_drift * drift_scaled
            + micro.weight_intensity * intensity_component
        )

        # Dampener factor (mirror MicroStructure logic)
        abs_drift = abs(drift_scaled)
        if abs(ofi_15) < 0.05:
            dampener = 1.0
        elif abs_drift < micro.dampener_price_deadzone:
            dampener = micro.dampener_flat_factor
        else:
            ofi_sign = 1.0 if ofi_15 > 0 else -1.0
            price_sign = 1.0 if drift_scaled > 0 else -1.0
            alignment = ofi_sign * price_sign
            price_strength = min(1.0, (abs_drift - micro.dampener_price_deadzone) / (1.0 - micro.dampener_price_deadzone))
            if alignment > 0:
                dampener = micro.dampener_flat_factor + (micro.dampener_agree_factor - micro.dampener_flat_factor) * price_strength
            else:
                dampener = micro.dampener_flat_factor + (micro.dampener_disagree_factor - micro.dampener_flat_factor) * price_strength

        dampened = max(-1.0, min(1.0, raw_signal * dampener))

        # Regime classification
        tracker = self.get_ofi_tracker(micro.symbol)
        tracker.update(ofi_15, time.time())
        regime = classify_regime(
            trend_5m=micro.trend_5m,
            intensity_5s=int_5,
            intensity_30s=int_30,
            price_change_pct=micro.price_change_pct,
            ofi_flip_count_30s=tracker.flip_count,
        )

        # Position P&L
        unrealized_pnl_pct = 0.0
        if current_position and entry_price > 0:
            our_price = market.yes_price if current_position == "yes" else market.no_price
            unrealized_pnl_pct = (our_price - entry_price) / entry_price

        # Window elapsed
        elapsed_pct = 0.0
        if window_duration > 0 and seconds_remaining >= 0:
            elapsed_pct = max(0.0, min(1.0, 1.0 - seconds_remaining / window_duration))

        return SignalSnapshot(
            timestamp=time.time(),
            session_id=self.session_id,
            symbol=micro.symbol,
            market_id=market.condition_id if market else "",
            window_question=market.question if market else "",
            btc_price=micro.current_price,
            price_change_pct=micro.price_change_pct,
            window_start_price=micro.window_start_price,
            yes_price=market.yes_price if market else 0.0,
            no_price=market.no_price if market else 0.0,
            ofi_5s=ofi_5,
            ofi_15s=ofi_15,
            ofi_30s=ofi_30,
            ofi_5m=micro.ofi_5m,
            vwap_drift=drift,
            vwap_drift_scaled=drift_scaled,
            intensity_5s=int_5,
            intensity_30s=int_30,
            raw_momentum=raw_signal,
            dampener_factor=dampener,
            dampened_momentum=dampened,
            confidence=micro.confidence,
            trend_5m=micro.trend_5m,
            trend_30s_ofi=ofi_30,
            seconds_remaining=seconds_remaining,
            window_elapsed_pct=elapsed_pct,
            regime=regime.value,
            current_position=current_position,
            entry_price=entry_price,
            high_water_mark=high_water_mark,
            unrealized_pnl_pct=unrealized_pnl_pct,
            spread=market.spread if market else 0.0,
            liquidity=market.liquidity if market else 0.0,
            event_type=event_type,
        )

    async def log_snapshot(self, snap: SignalSnapshot):
        """Log a snapshot to the buffer. Flushes periodically."""
        self._snapshot_buffer.append(snap.to_dict())
        self._total_snapshots += 1

        # Flush buffer periodically
        now = time.time()
        if now - self._last_flush_time >= self._buffer_flush_interval:
            await self.flush()

    async def log_candidate(self, snap: SignalSnapshot, distance_to_threshold: float):
        """Log a candidate event — an almost-signal.

        Called when momentum crosses ~80% of entry threshold or
        persistence begins but doesn't complete.
        """
        snap.event_type = "candidate"
        snap.near_threshold = True
        snap.distance_to_threshold = distance_to_threshold
        self._total_candidates += 1
        await self.log_snapshot(snap)

    async def log_no_trade(self, snap: SignalSnapshot, reason: NoTradeReason):
        """Log why a potential signal was blocked.

        Called when momentum crossed threshold but a filter blocked entry.
        """
        snap.no_trade_reason = reason.value
        snap.event_type = "no_trade"
        self._total_no_trade += 1
        await self.log_snapshot(snap)

    async def log_trade(
        self,
        snap: SignalSnapshot,
        trade_side: str,
        trade_action: str,
        attribution: dict,
        exit_reason: str = "",
    ):
        """Log a snapshot at trade time with attribution data."""
        snap.event_type = "trade"
        snap.trade_fired = True
        snap.trade_side = trade_side
        snap.trade_action = trade_action
        snap.exit_reason = exit_reason
        snap.attribution = attribution
        snap.no_trade_reason = NoTradeReason.NONE.value
        # Ensure current_position reflects the trade side for entries
        # (build_snapshot sets it from pre-trade state, which is empty for new entries)
        if not snap.current_position and trade_side:
            snap.current_position = trade_side
        self._total_trades += 1
        await self.log_snapshot(snap)

    async def flush(self) -> int:
        """Flush buffered snapshots to database. Returns count flushed."""
        if not self._snapshot_buffer:
            return 0

        batch = self._snapshot_buffer[:]
        self._snapshot_buffer.clear()
        self._last_flush_time = time.time()

        try:
            await self.db.bulk_insert_snapshots(batch)
            return len(batch)
        except Exception as e:
            logger.warning(f"Research snapshot flush failed ({len(batch)} rows): {e}")
            return 0

    def should_log_periodic(self, symbol: str, interval: float = 2.0) -> bool:
        """Check if it's time for a periodic snapshot for this symbol."""
        now = time.time()
        last = self._last_periodic_time.get(symbol, 0)
        if now - last >= interval:
            self._last_periodic_time[symbol] = now
            return True
        return False

    @property
    def stats(self) -> dict:
        return {
            "total_snapshots": self._total_snapshots,
            "total_candidates": self._total_candidates,
            "total_no_trade": self._total_no_trade,
            "buffer_size": len(self._snapshot_buffer),
        }
