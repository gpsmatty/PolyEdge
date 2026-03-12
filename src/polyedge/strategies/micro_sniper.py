"""Micro Sniper — high-frequency momentum trading on Polymarket 5-minute
crypto up/down markets.

Unlike the regular crypto sniper (which waits for a clear 0.2% price move
in the last 90 seconds), the micro sniper reads order flow microstructure
from Binance aggTrade data and trades momentum on EVERY swing.

Key insight: In a 5-minute BTC window, price swings of $20-50 cause
Polymarket's up/down market prices to move 20-40 cents.  Bots already
trade this — we join them by reading the same Binance order flow they read.

The strategy works in three phases:
1. MOMENTUM ENTRY — When order flow imbalance + VWAP drift + trade
   intensity all agree on direction, enter a position.
2. MOMENTUM EXIT — When the signal weakens or reverses, close.
3. FLIP — If momentum reverses strongly, close and open opposite side.

This can produce 10-50+ trades per 5-minute window depending on volatility.
Position sizing is small per trade (1-3% of bankroll) since edge per trade
is smaller but frequency is high.

No AI needed — pure microstructure math and speed.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from rich.console import Console

from polyedge.core.config import Settings
from polyedge.core.models import Market, Signal, Side
from polyedge.data.binance_aggtrade import MicroStructure, AggTrade
from polyedge.data.book_analyzer import BookIntelligence
from polyedge.data.research import NoTradeReason

logger = logging.getLogger("polyedge.micro_sniper")
console = Console()


# ---------------------------------------------------------------------------
# Configuration thresholds
# ---------------------------------------------------------------------------

class MicroAction(str, Enum):
    """What the micro sniper wants to do."""
    HOLD = "hold"           # Do nothing
    BUY_YES = "buy_yes"     # Enter/add YES position (bullish)
    BUY_NO = "buy_no"       # Enter/add NO position (bearish)
    EXIT = "exit"           # Close current position
    FLIP_YES = "flip_yes"   # Close NO and open YES
    FLIP_NO = "flip_no"     # Close YES and open NO


@dataclass
class MicroOpportunity:
    """A micro sniper trading opportunity."""
    market: Market
    symbol: str
    action: MicroAction
    side: Side                   # YES or NO for the trade
    momentum: float              # Momentum signal strength (-1 to +1)
    confidence: float            # How confident in the signal (0-1)
    ofi_5s: float               # Short-term order flow imbalance
    ofi_15s: float              # Medium-term order flow imbalance
    vwap_drift: float           # VWAP drift signal
    trade_intensity: float      # Trades per second
    binance_price: float        # Current Binance price
    price_change_pct: float     # Price change in window
    market_price: float         # Polymarket price for our side
    seconds_remaining: float    # Time left in the window
    is_flip: bool = False       # True if this is a position flip
    poly_book_imbalance: float = 0.0  # Polymarket order book imbalance (if enabled)
    exit_reason: str = ""       # Why we're exiting: "trailing_stop", "reversal", "force_exit", "floor_exit", "book_override_fail"


class MicroSniperStrategy:
    """Microstructure momentum strategy for 5-minute crypto up/down markets.

    Reads Binance aggTrade order flow to detect short-term momentum and
    trades Polymarket's up/down markets accordingly.

    Entry conditions (ALL must be true):
    1. Momentum signal > entry_threshold (0.40), or > counter_trend_threshold
       (0.55) if entering against the 30s trend
    2. Confidence > min_confidence (0.40)
    3. At least min_trades_in_window trades in 15s window
    4. Time remaining > min_seconds_remaining (don't enter in last 15s)
    5. Market price between min_entry_price (0.20) and max_entry_price (0.70)

    Exit conditions (ANY triggers exit):
    1. Momentum signal reverses past exit_threshold
    2. Momentum signal drops below hold_threshold for our direction
    3. Time remaining < force_exit_seconds (exit before window closes)

    Flip conditions (disabled by default — enable_flips=False):
    1. Momentum signal reverses past flip_threshold (0.50)
    2. Confidence > flip_min_confidence
    When disabled, strong reversals trigger EXIT instead.
    """

    name = "micro_sniper"

    def __init__(self, settings: Settings):
        self.config = settings.strategies.micro_sniper
        # Track previous momentum per symbol for acceleration filter.
        # Only enter when momentum is still rising, not fading.
        self._prev_momentum: dict[str, float] = {}
        # Entry persistence: time-based. Tracks when momentum first crossed
        # the entry threshold in a given direction. Resets when momentum drops
        # below threshold or direction flips. Entry only allowed after the
        # signal has persisted for entry_persistence_seconds.
        self._entry_streak: dict[str, int] = {}       # symbol -> consecutive count (deprecated)
        self._entry_streak_dir: dict[str, bool] = {}  # symbol -> was_bullish
        self._entry_signal_start: dict[str, float] = {}  # symbol -> time.time() when signal started
        # Last reason a potential entry was rejected (read by research logger)
        self.last_no_trade_reason: NoTradeReason | None = None
        self._last_bias_adjustment: float = 0.0  # Adaptive bias applied to threshold
        self._last_accel_detail: str = ""  # Detail string for acceleration blocks
        self._last_chop_boost: float = 0.0  # Chop filter threshold boost

    def evaluate(
        self,
        market: Market,
        micro: MicroStructure,
        seconds_remaining: float,
        current_position: Optional[str] = None,  # "yes", "no", or None
        book_intel: Optional[dict[str, BookIntelligence]] = None,  # {"yes": ..., "no": ...}
        entry_price: Optional[float] = None,       # For trailing stop
        high_water_mark: Optional[float] = None,   # For trailing stop
    ) -> Optional[MicroOpportunity]:
        """Evaluate a single up/down market against current microstructure.

        Args:
            market: The Polymarket up/down market.
            micro: Current microstructure state from aggTrade feed.
            seconds_remaining: Seconds left in the market window.
            current_position: "yes" if holding YES, "no" if holding NO, None if flat.
            book_intel: Polymarket order book intelligence for YES and NO tokens.
            entry_price: Entry price for current position (trailing stop).
            high_water_mark: Highest price seen since entry (trailing stop).

        Returns:
            MicroOpportunity if action should be taken, None otherwise.
        """
        self.last_no_trade_reason = None  # Reset on each eval

        if not self.config.enabled:
            return None

        if seconds_remaining <= 0:
            return None

        if not micro.flow_5s.is_active:
            return None

        momentum = micro.momentum_signal
        confidence = micro.confidence

        # --- Blend Polymarket book sentiment into momentum ---
        # The Binance momentum is pure order flow. The Poly book tells us
        # what Polymarket participants are actually betting. When they agree,
        # the signal is stronger. When they disagree, it's weaker.
        # Applied here so it affects EVERY downstream decision (entry/exit/flip).
        poly_adj = 0.0
        if self.config.poly_book_enabled and book_intel:
            yes_book = book_intel.get("yes")
            if yes_book:
                # imbalance_5c ranges [-1, +1]. Positive = more YES bids = bullish.
                # For a bullish momentum signal, positive imbalance confirms it.
                # For a bearish signal, negative imbalance confirms it.
                # So we just add weight * imbalance directly — it naturally
                # aligns: bullish momentum + bullish book = stronger signal,
                # bullish momentum + bearish book = weaker signal.
                poly_adj = self.config.poly_book_imbalance_weight * yes_book.imbalance_5c
                momentum = max(-1.0, min(1.0, momentum + poly_adj))

        abs_momentum = abs(momentum)
        is_bullish = momentum > 0

        # --- Force exit near window close ---
        if seconds_remaining < self.config.force_exit_seconds and current_position:
            return MicroOpportunity(
                market=market,
                symbol=micro.symbol,
                action=MicroAction.EXIT,
                side=Side.YES if current_position == "yes" else Side.NO,
                momentum=momentum,
                confidence=confidence,
                ofi_5s=micro.flow_5s.ofi,
                ofi_15s=micro.flow_15s.ofi,
                vwap_drift=micro.flow_15s.vwap_drift,
                trade_intensity=micro.flow_5s.trade_intensity,
                binance_price=micro.current_price,
                price_change_pct=micro.price_change_pct,
                market_price=market.yes_price if current_position == "yes" else market.no_price,
                seconds_remaining=seconds_remaining,
                exit_reason="force_exit",
            )

        # Don't enter new positions too close to end
        if seconds_remaining < self.config.min_seconds_remaining:
            return None

        # --- Check if we should exit or flip ---
        if current_position:
            return self._evaluate_with_position(
                market, micro, seconds_remaining, current_position,
                momentum, confidence, is_bullish, book_intel,
                entry_price, high_water_mark,
            )

        # --- Check if we should enter ---
        return self._evaluate_entry(
            market, micro, seconds_remaining,
            momentum, confidence, is_bullish, book_intel,
        )

    def _evaluate_entry(
        self,
        market: Market,
        micro: MicroStructure,
        seconds_remaining: float,
        momentum: float,
        confidence: float,
        is_bullish: bool,
        book_intel: Optional[dict[str, BookIntelligence]] = None,
    ) -> Optional[MicroOpportunity]:
        """Check if we should open a new position."""
        abs_momentum = abs(momentum)

        # --- Low volatility regime blocker ---
        # When trade intensity is low and price isn't moving, momentum signals
        # are noise. Data: 33% win rate, -18.68% avg move in low_vol regime.
        if self.config.low_vol_block_enabled:
            int_30 = micro.flow_30s.trade_intensity if micro.flow_30s.is_active else 0.0
            abs_price_chg = abs(micro.price_change_pct)
            if int_30 < self.config.low_vol_max_intensity and abs_price_chg < self.config.low_vol_max_price_change:
                self.last_no_trade_reason = NoTradeReason.LOW_VOL
                return None

        # --- High intensity blocker ---
        # Data shows losers average ~61 tps vs winners ~40 tps. High trade
        # intensity = chaotic price action where OFI signals are noise.
        # The book is getting slammed from both sides and momentum is unreliable.
        if self.config.high_intensity_block_enabled:
            int_30 = micro.flow_30s.trade_intensity if micro.flow_30s.is_active else 0.0
            if int_30 > self.config.high_intensity_max_tps:
                self.last_no_trade_reason = NoTradeReason.HIGH_INTENSITY
                return None

        # --- 5-minute persistent trend bias ---
        # The macro trend from the 5-minute rolling window (persists across
        # window hops and restarts via DB). If BTC has been trending up for
        # 5 minutes, don't buy NO on a brief dip — it's a pullback, not a
        # reversal. This is the "persistence" that was missing.
        trend_5m = micro.trend_5m
        if self.config.trend_bias_enabled and abs(trend_5m) > 0:
            # Check if we have enough data to trust the trend
            flow_age = 0.0
            if micro.flow_5m.is_active and micro.flow_5m._trades:
                flow_age = micro.flow_5m._trades[-1].timestamp - micro.flow_5m._trades[0].timestamp
            has_db_context = len(micro.price_history) > 0
            trend_trusted = flow_age >= self.config.trend_warmup_seconds or has_db_context

            if trend_trusted:
                is_counter_5m = (
                    (is_bullish and trend_5m < -self.config.trend_bias_min_pct) or
                    (not is_bullish and trend_5m > self.config.trend_bias_min_pct)
                )

                if is_counter_5m:
                    # Strong trend = hard block
                    if abs(trend_5m) >= self.config.trend_bias_strong_pct:
                        if not hasattr(self, '_trend_block_logged') or self._trend_block_logged.get(micro.symbol) != is_bullish:
                            if not hasattr(self, '_trend_block_logged'):
                                self._trend_block_logged = {}
                            self._trend_block_logged[micro.symbol] = is_bullish
                            console.print(
                                f"[dim red]TREND BLOCK: {'YES' if is_bullish else 'NO'} entry blocked — "
                                f"5m trend {trend_5m:+.3%} ({'up' if trend_5m > 0 else 'down'}) "
                                f"is too strong to fight[/dim red]"
                            )
                        self.last_no_trade_reason = NoTradeReason.TREND_VETO
                        return None

                    # Moderate trend = boost threshold
                    # (applied on top of the 30s counter-trend boost below)
                else:
                    # Clear the log tracker when direction aligns
                    if hasattr(self, '_trend_block_logged'):
                        self._trend_block_logged.pop(micro.symbol, None)

        # 30s trend filter — if entry direction disagrees with the dominant
        # 30-second trend, require a higher momentum threshold. This kills
        # counter-trend entries that are the #1 source of losses.
        trend_ofi = micro.flow_30s.ofi if micro.flow_30s.is_active else 0.0
        is_counter_trend = (is_bullish and trend_ofi < -0.05) or (not is_bullish and trend_ofi > 0.05)
        effective_threshold = (
            self.config.counter_trend_threshold if is_counter_trend
            else self.config.entry_threshold
        )

        # Apply 5m trend bias boost on top of 30s filter
        if self.config.trend_bias_enabled and abs(trend_5m) >= self.config.trend_bias_min_pct:
            is_counter_5m = (
                (is_bullish and trend_5m < -self.config.trend_bias_min_pct) or
                (not is_bullish and trend_5m > self.config.trend_bias_min_pct)
            )
            if is_counter_5m and abs(trend_5m) < self.config.trend_bias_strong_pct:
                # Moderate counter-trend: add boost to threshold
                effective_threshold += self.config.trend_bias_counter_boost

        # --- Adaptive directional bias (30m macro trend) ---
        # Shifts the threshold per-side based on the longer-term trend.
        # Bearish 30m → YES needs higher threshold, NO gets lower.
        # Bullish 30m → NO needs higher threshold, YES gets lower.
        # This is the "variable config" — not a hard block, just a bias.
        self._last_bias_adjustment = 0.0  # Track for logging
        if self.config.adaptive_bias_enabled:
            trend_30m = micro.trend_lookback(self.config.adaptive_bias_lookback_minutes)
            if abs(trend_30m) >= self.config.adaptive_bias_min_move:
                half_spread = self.config.adaptive_bias_spread / 2.0
                market_bearish = trend_30m < 0
                if is_bullish:
                    # Buying YES (bullish bet)
                    # Bearish market → harder (+spread/2), bullish → easier (-spread/2)
                    adjustment = half_spread if market_bearish else -half_spread
                else:
                    # Buying NO (bearish bet)
                    # Bearish market → easier (-spread/2), bullish → harder (+spread/2)
                    adjustment = -half_spread if market_bearish else half_spread
                effective_threshold += adjustment
                self._last_bias_adjustment = adjustment

        # --- Chop filter (volatility-scaled threshold) ---
        # When market is range-bound (big swings, no direction), momentum signals
        # are unreliable. Auto-boost threshold proportional to chop intensity.
        # No manual config changes needed — adapts to market conditions.
        self._last_chop_boost = 0.0
        if self.config.chop_filter_enabled:
            chop = micro.chop_index
            if chop > self.config.chop_threshold:
                # Linear scale: chop_threshold → 0 boost, chop_scale → max_boost
                chop_range = self.config.chop_scale - self.config.chop_threshold
                if chop_range > 0:
                    chop_frac = min(1.0, (chop - self.config.chop_threshold) / chop_range)
                    boost = chop_frac * self.config.chop_max_boost
                    effective_threshold += boost
                    self._last_chop_boost = boost

        # Need strong enough signal
        if abs_momentum < effective_threshold:
            # Even if we don't enter, track momentum for acceleration filter
            self._prev_momentum[micro.symbol] = momentum
            self._entry_streak[micro.symbol] = 0  # Reset persistence
            self._entry_signal_start.pop(micro.symbol, None)  # Reset time-based persistence
            self.last_no_trade_reason = NoTradeReason.BELOW_THRESHOLD
            return None

        # Need enough confidence
        if confidence < self.config.min_confidence:
            self._prev_momentum[micro.symbol] = momentum
            self._entry_streak[micro.symbol] = 0  # Reset persistence
            self._entry_signal_start.pop(micro.symbol, None)
            self.last_no_trade_reason = NoTradeReason.CONFIDENCE_TOO_LOW
            return None

        # Need enough trade activity
        if micro.flow_15s.total_count < self.config.min_trades_in_window:
            self._prev_momentum[micro.symbol] = momentum
            self._entry_streak[micro.symbol] = 0  # Reset persistence
            self._entry_signal_start.pop(micro.symbol, None)
            self.last_no_trade_reason = NoTradeReason.SPARSE_DATA
            return None

        # --- Acceleration filter ---
        # Don't chase fading signals. Only enter when momentum is still
        # building (accelerating), not when it's already rolling over.
        # This prevents buying at the top of an OFI spike.
        prev = self._prev_momentum.get(micro.symbol, 0.0)
        self._prev_momentum[micro.symbol] = momentum
        tol = self.config.acceleration_tolerance
        if is_bullish:
            # Bullish: current momentum should be >= previous (still rising)
            fade = prev - momentum
            if fade > tol:
                self.last_no_trade_reason = NoTradeReason.ACCELERATION
                self._last_accel_detail = f"fade={fade:.2f} tol={tol:.2f}"
                return None
        else:
            # Bearish: current momentum should be <= previous (still falling)
            fade = momentum - prev
            if fade > tol:
                self.last_no_trade_reason = NoTradeReason.ACCELERATION
                self._last_accel_detail = f"fade={fade:.2f} tol={tol:.2f}"
                return None
        self._last_accel_detail = ""

        # --- Price-to-beat filter ---
        # Don't buy the wrong side of a decided market. If BTC has moved
        # significantly from the window open, don't bet on a reversal.
        # A 5-second OFI spike doesn't override 3 minutes of price action.
        price_change = micro.price_change_pct
        if micro.window_start_price > 0 and seconds_remaining < 180:
            # Scale threshold: stricter as time runs out.
            # With 180s left, need 0.10% move to block. With 30s left, 0.03%.
            time_factor = max(0.3, seconds_remaining / 180.0)
            block_threshold = 0.001 * time_factor  # 0.03%-0.10%

            if is_bullish and price_change < -block_threshold:
                # Buying YES but BTC is below the open — fighting the window
                self.last_no_trade_reason = NoTradeReason.PRICE_TO_BEAT
                return None
            if not is_bullish and price_change > block_threshold:
                # Buying NO but BTC is above the open — fighting the window
                self.last_no_trade_reason = NoTradeReason.PRICE_TO_BEAT
                return None

        if is_bullish:
            action = MicroAction.BUY_YES
            side = Side.YES
            market_price = market.yes_price
        else:
            action = MicroAction.BUY_NO
            side = Side.NO
            market_price = market.no_price

        # Don't buy if the market already prices in the direction heavily
        if market_price > self.config.max_entry_price:
            self.last_no_trade_reason = NoTradeReason.PRICE_BAND
            return None

        # Don't buy a side the market has nearly killed — if YES is at 4¢,
        # the market says 96% chance of NO. A 5-second OFI blip doesn't
        # override the entire window's price action.
        if market_price < self.config.min_entry_price:
            self.last_no_trade_reason = NoTradeReason.PRICE_BAND
            return None

        # Dead market filter — if YES is stuck near 0.50, the market isn't
        # reacting to price moves. Our momentum signals are real but the
        # market doesn't care. Every trade is just paying spread for nothing.
        yes_price = market.yes_price
        band = self.config.dead_market_band
        if abs(yes_price - 0.50) < band:
            self.last_no_trade_reason = NoTradeReason.DEAD_MARKET
            return None

        # --- Polymarket order book checks (when poly_book_enabled) ---
        poly_imbalance = 0.0
        if self.config.poly_book_enabled and book_intel:
            # Pick the book for the token we're buying.
            # Buying YES → need YES book (bids = exit path)
            # Buying NO → need NO book (bids = exit path)
            our_side = "yes" if is_bullish else "no"
            our_book = book_intel.get(our_side)

            if our_book:
                # 1. Exit liquidity check — if the bid side of our token is
                #    too thin, we'll get trapped. Don't enter without exit depth.
                if our_book.bid_depth_5c < self.config.poly_book_min_exit_depth:
                    console.print(
                        f"[dim]BOOK ENTRY BLOCK: {our_side.upper()} bid depth "
                        f"{our_book.bid_depth_5c:.0f} < {self.config.poly_book_min_exit_depth:.0f} "
                        f"— no exit liquidity[/dim]"
                    )
                    self.last_no_trade_reason = NoTradeReason.BOOK_NO_LIQUIDITY
                    return None

            # 2. Book imbalance signal — use YES book imbalance as the
            #    sentiment indicator (it's the primary side on Polymarket).
            #    imbalance_5c > 0 = more bids = bullish market sentiment.
            yes_book = book_intel.get("yes")
            if yes_book:
                poly_imbalance = yes_book.imbalance_5c
                # For YES buys, positive imbalance is aligned.
                # For NO buys, flip the sign.
                directional_imbalance = poly_imbalance if is_bullish else -poly_imbalance

                # Veto: if the Poly book strongly disagrees, block entry
                if directional_imbalance < self.config.poly_book_imbalance_veto:
                    console.print(
                        f"[dim]BOOK ENTRY VETO: imbalance {directional_imbalance:+.2f} "
                        f"< {self.config.poly_book_imbalance_veto:+.2f} — book disagrees[/dim]"
                    )
                    self.last_no_trade_reason = NoTradeReason.BOOK_VETO
                    return None

        # --- Entry persistence filter (time-based) ---
        # Require momentum to stay above threshold for N seconds continuously.
        # Kills single-spike noise entries. A real move persists; a spike doesn't.
        # At 30 tps, count-based (3 evals) = ~450ms — way too fast to filter noise.
        # Time-based (2s) gives a real confirmation window regardless of tick rate.
        if self.config.entry_persistence_enabled:
            sym = micro.symbol
            now = time.time()
            prev_dir = self._entry_streak_dir.get(sym)

            # Reset timer if direction flipped
            if prev_dir is not None and prev_dir != is_bullish:
                self._entry_signal_start.pop(sym, None)
            self._entry_streak_dir[sym] = is_bullish

            # Start timer if this is the first qualifying eval
            if sym not in self._entry_signal_start:
                self._entry_signal_start[sym] = now

            elapsed = now - self._entry_signal_start[sym]
            if elapsed < self.config.entry_persistence_seconds:
                self.last_no_trade_reason = NoTradeReason.FAILED_PERSISTENCE
                return None  # Signal is real but hasn't persisted long enough

        return MicroOpportunity(
            market=market,
            symbol=micro.symbol,
            action=action,
            side=side,
            momentum=momentum,
            confidence=confidence,
            ofi_5s=micro.flow_5s.ofi,
            ofi_15s=micro.flow_15s.ofi,
            vwap_drift=micro.flow_15s.vwap_drift,
            trade_intensity=micro.flow_5s.trade_intensity,
            binance_price=micro.current_price,
            price_change_pct=micro.price_change_pct,
            market_price=market_price,
            seconds_remaining=seconds_remaining,
            poly_book_imbalance=poly_imbalance,
        )

    def _evaluate_with_position(
        self,
        market: Market,
        micro: MicroStructure,
        seconds_remaining: float,
        current_position: str,
        momentum: float,
        confidence: float,
        is_bullish: bool,
        book_intel: Optional[dict[str, BookIntelligence]] = None,
        entry_price: Optional[float] = None,
        high_water_mark: Optional[float] = None,
    ) -> Optional[MicroOpportunity]:
        """Evaluate when we already have a position — exit, hold, or flip."""
        abs_momentum = abs(momentum)
        holding_yes = current_position == "yes"

        # --- TAKE PROFIT ---
        # When our token price hits the target (e.g. 0.90), take the money.
        # Don't wait for momentum reversal or trailing stop — lock in the gain.
        if self.config.take_profit_enabled and entry_price:
            our_price = market.yes_price if holding_yes else market.no_price
            if our_price >= self.config.take_profit_price:
                console.print(
                    f"[bold green]TAKE PROFIT: {'YES' if holding_yes else 'NO'} "
                    f"entry=${entry_price:.2f} → now=${our_price:.2f} "
                    f"(≥ ${self.config.take_profit_price:.2f} target) "
                    f"— banking profit[/bold green]"
                )
                return MicroOpportunity(
                    market=market,
                    symbol=micro.symbol,
                    action=MicroAction.EXIT,
                    side=Side.YES if holding_yes else Side.NO,
                    momentum=momentum,
                    confidence=confidence,
                    ofi_5s=micro.flow_5s.ofi,
                    ofi_15s=micro.flow_15s.ofi,
                    vwap_drift=micro.flow_15s.vwap_drift,
                    trade_intensity=micro.flow_5s.trade_intensity,
                    binance_price=micro.current_price,
                    price_change_pct=micro.price_change_pct,
                    market_price=our_price,
                    seconds_remaining=seconds_remaining,
                    exit_reason="take_profit",
                )

        # --- TRAILING STOP LOSS ---
        # When enabled: tracks the high water mark (HWM) of our position's price.
        # Once price has risen above entry by min_profit_pct, the stop is "armed".
        # If price drops trailing_stop_pct from HWM, trigger exit.
        # Late in the window (last trailing_stop_late_seconds), tighten to trailing_stop_late_pct.
        if self.config.trailing_stop_enabled and entry_price and high_water_mark:
            our_price = market.yes_price if holding_yes else market.no_price
            profit_from_entry = (high_water_mark - entry_price) / entry_price if entry_price > 0 else 0

            # Only arm the stop after we've seen meaningful profit
            if profit_from_entry >= self.config.trailing_stop_min_profit_pct:
                # Time-scaled stop: tighter late in the window
                if seconds_remaining < self.config.trailing_stop_late_seconds:
                    stop_pct = self.config.trailing_stop_late_pct
                else:
                    stop_pct = self.config.trailing_stop_pct

                drawdown_from_hwm = (high_water_mark - our_price) / high_water_mark if high_water_mark > 0 else 0

                if drawdown_from_hwm >= stop_pct:
                    console.print(
                        f"[bold yellow]TRAILING STOP: {'YES' if holding_yes else 'NO'} "
                        f"entry=${entry_price:.2f} → HWM=${high_water_mark:.2f} → "
                        f"now=${our_price:.2f} (drawdown {drawdown_from_hwm:.0%} ≥ {stop_pct:.0%}) "
                        f"— locking in profit[/bold yellow]"
                    )
                    return MicroOpportunity(
                        market=market,
                        symbol=micro.symbol,
                        action=MicroAction.EXIT,
                        side=Side.YES if holding_yes else Side.NO,
                        momentum=momentum,
                        confidence=confidence,
                        ofi_5s=micro.flow_5s.ofi,
                        ofi_15s=micro.flow_15s.ofi,
                        vwap_drift=micro.flow_15s.vwap_drift,
                        trade_intensity=micro.flow_5s.trade_intensity,
                        binance_price=micro.current_price,
                        price_change_pct=micro.price_change_pct,
                        market_price=our_price,
                        seconds_remaining=seconds_remaining,
                        exit_reason="trailing_stop",
                    )

        # --- TIME-SCALED FLOOR EXIT ---
        # Prevents catastrophic ride-to-zero, but only when price is low AND
        # there's not enough time left to recover.  Winners routinely dip to
        # $0.25 mid-window and rip back — a hard floor kills those.  Instead,
        # the floor tightens as time runs out:
        #   >120s left → no floor (plenty of time to recover)
        #   60-120s    → floor at 0.15 (extreme — market has given up)
        #   30-60s     → floor at 0.20 (running out of time)
        #   <30s       → floor at min_entry_price (force_exit handles the rest)
        our_price = market.yes_price if holding_yes else market.no_price
        if seconds_remaining < 120:
            if seconds_remaining < 30:
                floor = self.config.min_entry_price
            elif seconds_remaining < 60:
                floor = 0.20
            else:
                floor = 0.15

            if our_price < floor:
                console.print(
                    f"[bold red]FLOOR EXIT: {'YES' if holding_yes else 'NO'} price "
                    f"${our_price:.2f} < floor ${floor:.2f} "
                    f"({seconds_remaining:.0f}s left) — emergency bail[/bold red]"
                )
                return MicroOpportunity(
                    market=market,
                    symbol=micro.symbol,
                    action=MicroAction.EXIT,
                    side=Side.YES if holding_yes else Side.NO,
                    momentum=momentum,
                    confidence=confidence,
                    ofi_5s=micro.flow_5s.ofi,
                    ofi_15s=micro.flow_15s.ofi,
                    vwap_drift=micro.flow_15s.vwap_drift,
                    trade_intensity=micro.flow_5s.trade_intensity,
                    binance_price=micro.current_price,
                    price_change_pct=micro.price_change_pct,
                    market_price=our_price,
                    seconds_remaining=seconds_remaining,
                    exit_reason="floor_exit",
                )

        # Is momentum aligned with our position?
        aligned = (holding_yes and is_bullish) or (not holding_yes and not is_bullish)

        # Guard: don't act on sparse data — OFI ±1.00 with only 1-2 trades
        # is noise, not momentum. Require minimum trade activity for flip/exit
        # signals just like we do for entry.
        if micro.flow_15s.total_count < self.config.min_trades_in_window:
            return None

        # --- FLIP: strong reversal ---
        # Flips are expensive (close + reopen) so require more trade activity.
        # Disabled by default (enable_flips=False) — strong reversals just EXIT.
        if self.config.enable_flips:
            has_enough_trades_for_flip = (
                micro.flow_15s.total_count >= self.config.min_trades_for_flip
            )
            if not aligned and abs_momentum >= self.config.flip_threshold:
                if confidence >= self.config.flip_min_confidence and has_enough_trades_for_flip:
                    if is_bullish:
                        action = MicroAction.FLIP_YES
                        side = Side.YES
                        market_price = market.yes_price
                    else:
                        action = MicroAction.FLIP_NO
                        side = Side.NO
                        market_price = market.no_price

                    # Don't flip into a nearly-dead or heavily priced side
                    if market_price < self.config.min_entry_price:
                        pass  # Fall through to exit check instead
                    elif market_price <= self.config.max_entry_price:
                        return MicroOpportunity(
                            market=market,
                            symbol=micro.symbol,
                            action=action,
                            side=side,
                            momentum=momentum,
                            confidence=confidence,
                            ofi_5s=micro.flow_5s.ofi,
                            ofi_15s=micro.flow_15s.ofi,
                            vwap_drift=micro.flow_15s.vwap_drift,
                            trade_intensity=micro.flow_5s.trade_intensity,
                            binance_price=micro.current_price,
                            price_change_pct=micro.price_change_pct,
                            market_price=market_price,
                            seconds_remaining=seconds_remaining,
                            is_flip=True,
                        )

        # --- EXIT: momentum reversed ---
        # 30s trend filter for exits — mirrors entry logic. If the 30s trend
        # still agrees with our position, require a higher exit threshold.
        # This prevents brief 5s/15s spikes from ejecting us when the broader
        # move still favours our side.
        trend_ofi = micro.flow_30s.ofi if micro.flow_30s.is_active else 0.0
        trend_agrees_with_position = (
            (holding_yes and trend_ofi > 0.05) or
            (not holding_yes and trend_ofi < -0.05)
        )
        effective_exit_threshold = (
            self.config.counter_trend_exit_threshold if trend_agrees_with_position
            else self.config.exit_threshold
        )

        should_exit_reversal = not aligned and abs_momentum >= effective_exit_threshold
        should_exit_faded = aligned and abs_momentum < self.config.hold_threshold

        # --- Polymarket book exit override ---
        # When momentum says exit but the Poly book still shows strong support
        # for our side (deep bids + favorable imbalance), override and hold.
        # The book reflects what Polymarket participants believe — if they're
        # still bidding our token, the momentum dip is likely noise.
        if (should_exit_reversal or should_exit_faded) and self.config.poly_book_enabled:
            if not book_intel:
                console.print("[dim yellow]BOOK EXIT CHECK: No book data — exiting without override[/dim yellow]")
            else:
                our_side = "yes" if holding_yes else "no"
                our_book = book_intel.get(our_side)
                yes_book = book_intel.get("yes")

                if our_book and yes_book:
                    depth_ok = our_book.bid_depth_5c >= self.config.poly_book_exit_override_depth
                    # Directional imbalance: positive = favors our position
                    raw_imbalance = yes_book.imbalance_5c
                    directional_imbalance = raw_imbalance if holding_yes else -raw_imbalance
                    imbalance_ok = directional_imbalance >= self.config.poly_book_exit_override_imbalance

                    result = "OVERRIDE — HOLD" if depth_ok and imbalance_ok else "EXIT"
                    color = "bold cyan" if depth_ok and imbalance_ok else "dim yellow"
                    console.print(
                        f"[{color}]BOOK EXIT CHECK: {our_side.upper()} "
                        f"depth={our_book.bid_depth_5c:.0f} "
                        f"(need {self.config.poly_book_exit_override_depth:.0f}), "
                        f"imbalance={directional_imbalance:+.2f} "
                        f"(need {self.config.poly_book_exit_override_imbalance:+.2f}) "
                        f"→ {result}[/{color}]"
                    )

                    if depth_ok and imbalance_ok:
                        # Book says hold — override the momentum exit
                        return None
                else:
                    console.print(
                        f"[dim yellow]BOOK EXIT CHECK: Missing book data "
                        f"(our_book={our_book is not None}, yes_book={yes_book is not None}) "
                        f"— exiting without override[/dim yellow]"
                    )

        if should_exit_reversal:
            return MicroOpportunity(
                market=market,
                symbol=micro.symbol,
                action=MicroAction.EXIT,
                side=Side.YES if holding_yes else Side.NO,
                momentum=momentum,
                confidence=confidence,
                ofi_5s=micro.flow_5s.ofi,
                ofi_15s=micro.flow_15s.ofi,
                vwap_drift=micro.flow_15s.vwap_drift,
                trade_intensity=micro.flow_5s.trade_intensity,
                binance_price=micro.current_price,
                price_change_pct=micro.price_change_pct,
                market_price=market.yes_price if holding_yes else market.no_price,
                seconds_remaining=seconds_remaining,
                exit_reason="reversal",
            )

        if should_exit_faded:
            return MicroOpportunity(
                market=market,
                symbol=micro.symbol,
                action=MicroAction.EXIT,
                side=Side.YES if holding_yes else Side.NO,
                momentum=momentum,
                confidence=confidence,
                ofi_5s=micro.flow_5s.ofi,
                ofi_15s=micro.flow_15s.ofi,
                vwap_drift=micro.flow_15s.vwap_drift,
                trade_intensity=micro.flow_5s.trade_intensity,
                binance_price=micro.current_price,
                price_change_pct=micro.price_change_pct,
                market_price=market.yes_price if holding_yes else market.no_price,
                seconds_remaining=seconds_remaining,
                exit_reason="faded",
            )

        # HOLD — momentum still aligned
        return None

    def opportunity_to_signal(self, opp: MicroOpportunity) -> Signal:
        """Convert a MicroOpportunity to a tradeable Signal."""
        action_label = opp.action.value.upper().replace("_", " ")
        book_str = f" | PolyBook: {opp.poly_book_imbalance:+.2f}" if opp.poly_book_imbalance != 0 else ""
        reasoning = (
            f"Micro Sniper [{action_label}]: {opp.symbol.upper()} "
            f"Momentum: {opp.momentum:+.2f} | "
            f"OFI(5s): {opp.ofi_5s:+.2f} OFI(15s): {opp.ofi_15s:+.2f} | "
            f"VWAP drift: {opp.vwap_drift:+.6f} | "
            f"Intensity: {opp.trade_intensity:.1f} tps | "
            f"BTC: ${opp.binance_price:,.2f} ({opp.price_change_pct:+.4%}) | "
            f"Mkt: {opp.market_price:.2f}{book_str} | "
            f"{opp.seconds_remaining:.0f}s left"
        )

        # Edge estimate: momentum-based strategies have smaller per-trade edge
        # but higher frequency. Rough estimate: momentum * 0.15
        est_edge = abs(opp.momentum) * 0.15

        return Signal(
            market=opp.market,
            side=opp.side,
            confidence=opp.confidence,
            edge=est_edge,
            ev=est_edge / opp.market_price if opp.market_price > 0 else 0,
            reasoning=reasoning,
            strategy=self.name,
        )
