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

from polyedge.core.config import Settings
from polyedge.core.models import Market, Signal, Side
from polyedge.data.binance_aggtrade import MicroStructure, AggTrade
from polyedge.data.book_analyzer import BookIntelligence

logger = logging.getLogger("polyedge.micro_sniper")


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

    def evaluate(
        self,
        market: Market,
        micro: MicroStructure,
        seconds_remaining: float,
        current_position: Optional[str] = None,  # "yes", "no", or None
        book_intel: Optional[dict[str, BookIntelligence]] = None,  # {"yes": ..., "no": ...}
    ) -> Optional[MicroOpportunity]:
        """Evaluate a single up/down market against current microstructure.

        Args:
            market: The Polymarket up/down market.
            micro: Current microstructure state from aggTrade feed.
            seconds_remaining: Seconds left in the market window.
            current_position: "yes" if holding YES, "no" if holding NO, None if flat.
            book_intel: Polymarket order book intelligence for YES and NO tokens.

        Returns:
            MicroOpportunity if action should be taken, None otherwise.
        """
        if not self.config.enabled:
            return None

        if seconds_remaining <= 0:
            return None

        if not micro.flow_5s.is_active:
            return None

        momentum = micro.momentum_signal
        confidence = micro.confidence
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
            )

        # Don't enter new positions too close to end
        if seconds_remaining < self.config.min_seconds_remaining:
            return None

        # --- Check if we should exit or flip ---
        if current_position:
            return self._evaluate_with_position(
                market, micro, seconds_remaining, current_position,
                momentum, confidence, is_bullish, book_intel,
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

        # 30s trend filter — if entry direction disagrees with the dominant
        # 30-second trend, require a higher momentum threshold. This kills
        # counter-trend entries that are the #1 source of losses.
        trend_ofi = micro.flow_30s.ofi if micro.flow_30s.is_active else 0.0
        is_counter_trend = (is_bullish and trend_ofi < -0.05) or (not is_bullish and trend_ofi > 0.05)
        effective_threshold = (
            self.config.counter_trend_threshold if is_counter_trend
            else self.config.entry_threshold
        )

        # Need strong enough signal
        if abs_momentum < effective_threshold:
            # Even if we don't enter, track momentum for acceleration filter
            self._prev_momentum[micro.symbol] = momentum
            return None

        # Need enough confidence
        if confidence < self.config.min_confidence:
            self._prev_momentum[micro.symbol] = momentum
            return None

        # Need enough trade activity
        if micro.flow_15s.total_count < self.config.min_trades_in_window:
            self._prev_momentum[micro.symbol] = momentum
            return None

        # --- Acceleration filter ---
        # Don't chase fading signals. Only enter when momentum is still
        # building (accelerating), not when it's already rolling over.
        # This prevents buying at the top of an OFI spike.
        prev = self._prev_momentum.get(micro.symbol, 0.0)
        self._prev_momentum[micro.symbol] = momentum
        if is_bullish:
            # Bullish: current momentum should be >= previous (still rising)
            if momentum < prev - 0.05:  # Allow tiny dips (noise tolerance)
                return None
        else:
            # Bearish: current momentum should be <= previous (still falling)
            if momentum > prev + 0.05:
                return None

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
                return None
            if not is_bullish and price_change > block_threshold:
                # Buying NO but BTC is above the open — fighting the window
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
            return None

        # Don't buy a side the market has nearly killed — if YES is at 4¢,
        # the market says 96% chance of NO. A 5-second OFI blip doesn't
        # override the entire window's price action.
        if market_price < self.config.min_entry_price:
            return None

        # Dead market filter — if YES is stuck near 0.50, the market isn't
        # reacting to price moves. Our momentum signals are real but the
        # market doesn't care. Every trade is just paying spread for nothing.
        yes_price = market.yes_price
        band = self.config.dead_market_band
        if abs(yes_price - 0.50) < band:
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
                    return None

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
    ) -> Optional[MicroOpportunity]:
        """Evaluate when we already have a position — exit, hold, or flip."""
        abs_momentum = abs(momentum)
        holding_yes = current_position == "yes"

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

        if not aligned and abs_momentum >= effective_exit_threshold:
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
            )

        # Also exit if momentum towards our side has evaporated
        if aligned and abs_momentum < self.config.hold_threshold:
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
