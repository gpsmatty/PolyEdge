"""Cheap Event Hunter — find underpriced tail events with positive EV."""

from __future__ import annotations

from polyedge.core.config import Settings
from polyedge.core.models import Market, Signal, Side
from polyedge.strategies.base import Strategy


class CheapHunterStrategy(Strategy):
    """Find events priced below threshold where true probability may be higher.

    The core insight: retail traders exhibit longshot bias — they overpay for
    obvious long shots but sometimes underprice less flashy tail events.
    We look for the underpriced ones.
    """

    name = "cheap_hunter"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.config = settings.strategies.cheap_hunter

    def evaluate(self, market: Market) -> Signal | None:
        """Evaluate if a market has an underpriced cheap outcome."""
        if not self.config.enabled:
            return None

        # Check basic filters
        if market.volume < self.config.min_volume:
            return None

        if market.liquidity < self.settings.risk.min_liquidity:
            return None

        # Check time to resolution
        hours = market.hours_to_resolution
        if hours is not None and hours < self.settings.risk.min_time_to_resolution_hours:
            return None

        # Look for cheap YES
        yes_signal = self._evaluate_side(market, Side.YES, market.yes_price)

        # Look for cheap NO
        no_signal = self._evaluate_side(market, Side.NO, market.no_price)

        # Return the better signal
        if yes_signal and no_signal:
            return yes_signal if yes_signal.ev > no_signal.ev else no_signal
        return yes_signal or no_signal

    def _evaluate_side(
        self, market: Market, side: Side, price: float
    ) -> Signal | None:
        """Evaluate one side of a market for cheap event opportunity."""
        if price <= 0.01 or price > self.config.max_price:
            return None

        # Estimate true probability using heuristics
        # (This is the naive version — AI edge finder will be more sophisticated)
        estimated_prob = self._estimate_probability(market, side, price)

        # Calculate expected value
        # If we buy at `price`, we get $1 if right, $0 if wrong
        # EV = estimated_prob * (1 - price) - (1 - estimated_prob) * price
        # EV = estimated_prob - price
        edge = estimated_prob - price

        if edge < self.config.min_ev:
            return None

        # EV per dollar invested
        ev_per_dollar = edge / price if price > 0 else 0

        return Signal(
            market=market,
            side=side,
            confidence=min(0.5, edge * 2),  # Conservative confidence for cheap events
            edge=edge,
            ev=ev_per_dollar,
            reasoning=self._build_reasoning(market, side, price, estimated_prob, edge),
            strategy=self.name,
        )

    def _estimate_probability(
        self, market: Market, side: Side, price: float
    ) -> float:
        """Naive probability estimation for cheap events.

        Heuristics:
        1. Markets with higher liquidity are more efficient → less mispricing
        2. Events near resolution are more efficient
        3. Apply a small upward bias to very cheap events (markets often underweight)
        """
        # Start with market price as base
        base_prob = price

        # Liquidity discount: less liquid markets have bigger mispricings
        if market.liquidity < 5000:
            liquidity_boost = 0.03  # 3% probability boost for illiquid markets
        elif market.liquidity < 20000:
            liquidity_boost = 0.015
        else:
            liquidity_boost = 0.005

        # Very cheap events (< 5 cents) often have a small upward bias
        if price < 0.05:
            cheap_boost = 0.02
        elif price < 0.10:
            cheap_boost = 0.01
        else:
            cheap_boost = 0.005

        # Time factor: events further from resolution may be more mispriced
        hours = market.hours_to_resolution
        time_boost = 0.0
        if hours and hours > 168:  # > 1 week
            time_boost = 0.01

        estimated = base_prob + liquidity_boost + cheap_boost + time_boost
        return min(estimated, 0.30)  # Cap at 30% — we're looking for cheap events

    def _build_reasoning(
        self,
        market: Market,
        side: Side,
        price: float,
        estimated_prob: float,
        edge: float,
    ) -> str:
        return (
            f"Cheap {side.value} at ${price:.3f} "
            f"(implied {price*100:.1f}%, estimated {estimated_prob*100:.1f}%). "
            f"Edge: {edge*100:.1f}%. "
            f"Volume: ${market.volume:,.0f}, Liquidity: ${market.liquidity:,.0f}. "
            f"EV per dollar: {(edge/price if price > 0 else 0):.2f}"
        )
