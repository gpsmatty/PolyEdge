"""Crypto Sniper — exploit Polymarket's short-duration crypto markets using
real-time price feeds from Binance.

The edge: Polymarket runs 5-minute and 15-minute "Up or Down" markets on BTC,
ETH, SOL, etc.  These markets ask "Will BTC be higher or lower at time T than
at time T-5min?"  The market starts around 50/50 and adjusts as price moves.

But Binance spot price moves FASTER than Polymarket reprices.  If BTC pumps 1%
with 60 seconds left, the outcome is ~90%+ certain, but Polymarket may still
show 65/35.  We buy the near-certain outcome at a discount.

This strategy:
1. Watches Binance real-time price feed
2. Identifies Polymarket crypto "Up or Down" markets nearing expiry
3. Computes implied probability from actual price movement
4. Compares to Polymarket's current price
5. Trades when edge exceeds threshold

No AI needed — pure math and speed.
"""

from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from polyedge.core.config import Settings
from polyedge.core.models import Market, Signal, Side
from polyedge.data.binance_feed import PriceSnapshot, PriceWindow
from polyedge.strategies.base import Strategy

logger = logging.getLogger("polyedge.crypto_sniper")

# Map Polymarket question keywords to Binance symbols
CRYPTO_SYMBOL_MAP = {
    "bitcoin": "btcusdt",
    "btc": "btcusdt",
    "ethereum": "ethusdt",
    "eth": "ethusdt",
    "ether": "ethusdt",
    "solana": "solusdt",
    "sol": "solusdt",
}

# Patterns to identify short-duration crypto markets
# e.g. "Solana Up or Down - March 10, 3:10PM-3:15PM ET"
# e.g. "Bitcoin Up or Down - March 10, 2:00PM-2:15PM ET"
UP_DOWN_PATTERN = re.compile(
    r"(Bitcoin|BTC|Ethereum|ETH|Ether|Solana|SOL)\s+"
    r"[Uu]p\s+or\s+[Dd]own",
    re.IGNORECASE,
)

# Pattern to extract time window duration from question
DURATION_PATTERN = re.compile(
    r"(\d{1,2}):(\d{2})\s*(AM|PM)\s*[-–]\s*(\d{1,2}):(\d{2})\s*(AM|PM)",
    re.IGNORECASE,
)


@dataclass
class SniperOpportunity:
    """A crypto sniper opportunity ready to trade."""
    market: Market
    symbol: str                  # e.g. "btcusdt"
    side: Side                   # YES = price goes up, NO = price goes down
    binance_price: float         # Current Binance spot price
    price_change_pct: float      # Price change in the window so far
    implied_prob: float          # Our estimated true probability from price data
    market_price: float          # What Polymarket is showing
    edge: float                  # implied_prob - market_price
    seconds_remaining: float     # Time left in the window
    confidence: float            # How confident we are (higher with more time elapsed + bigger move)


class CryptoSniperStrategy(Strategy):
    """Snipe short-duration crypto prediction markets using Binance price feed.

    This is a latency-edge strategy — no AI, just math and speed.
    """

    name = "crypto_sniper"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.config = settings.strategies.crypto_sniper

    def evaluate(self, market: Market) -> Signal | None:
        """Basic evaluate — not used for sniper (needs price data)."""
        return None

    def is_crypto_market(self, market: Market) -> bool:
        """Check if a market is a short-duration crypto up/down market."""
        return bool(UP_DOWN_PATTERN.search(market.question))

    def get_symbol(self, market: Market) -> Optional[str]:
        """Extract the Binance symbol from a crypto market question."""
        q = market.question.lower()
        for keyword, symbol in CRYPTO_SYMBOL_MAP.items():
            if keyword in q:
                return symbol
        return None

    def get_window_duration_minutes(self, market: Market) -> Optional[int]:
        """Extract the window duration in minutes from the market question.

        Parses things like "3:10PM-3:15PM" -> 5 minutes.
        """
        match = DURATION_PATTERN.search(market.question)
        if not match:
            return None

        h1, m1, ap1, h2, m2, ap2 = match.groups()
        t1 = int(h1) * 60 + int(m1)
        if ap1.upper() == "PM" and int(h1) != 12:
            t1 += 12 * 60
        elif ap1.upper() == "AM" and int(h1) == 12:
            t1 -= 12 * 60

        t2 = int(h2) * 60 + int(m2)
        if ap2.upper() == "PM" and int(h2) != 12:
            t2 += 12 * 60
        elif ap2.upper() == "AM" and int(h2) == 12:
            t2 -= 12 * 60

        duration = t2 - t1
        if duration <= 0:
            duration += 24 * 60  # Crosses midnight

        return duration

    def evaluate_with_price(
        self,
        market: Market,
        price_window: PriceWindow,
        current_price: PriceSnapshot,
        seconds_remaining: float,
    ) -> Optional[SniperOpportunity]:
        """Evaluate a crypto market against real-time Binance price data.

        This is the core logic.  We compute the probability that the price
        will be UP vs DOWN at expiry based on:
        1. Current price movement direction and magnitude
        2. Time remaining (less time = more certainty about outcome)
        3. Recent volatility (more volatile = less certain)

        Args:
            market: The Polymarket crypto market
            price_window: Rolling price window from start of market
            current_price: Latest Binance price snapshot
            seconds_remaining: Seconds until market closes
        """
        if not self.config.enabled:
            return None

        if seconds_remaining <= 0 or seconds_remaining > self.config.max_seconds_before_entry:
            return None

        symbol = self.get_symbol(market)
        if not symbol:
            return None

        # Price change since window opened
        change_pct = price_window.change_pct
        abs_change = abs(change_pct)

        if abs_change < self.config.min_price_move_pct:
            return None  # Not enough movement to be confident

        # Compute implied probability using a simple model:
        #
        # The probability that price stays in the same direction depends on:
        # 1. Magnitude of current move (bigger move = more momentum = less likely to reverse)
        # 2. Time remaining (less time = less chance of reversal)
        # 3. Volatility in the window (higher vol = more uncertainty)
        #
        # We use a sigmoid-like function calibrated to crypto 5-min dynamics.
        # A 0.5% move with 30s left is ~85% certain.
        # A 1.0% move with 30s left is ~95% certain.
        # A 0.2% move with 120s left is only ~60% certain.

        implied_prob = self._compute_implied_probability(
            abs_change, seconds_remaining, price_window.volatility
        )

        # Determine direction
        if change_pct > 0:
            # Price is UP — YES side should be winning
            side = Side.YES
            market_price = market.yes_price
        else:
            # Price is DOWN — NO side should be winning
            side = Side.NO
            market_price = market.no_price

        edge = implied_prob - market_price

        if edge < self.config.min_edge:
            return None

        # Confidence scales with edge size and time certainty
        confidence = min(0.95, implied_prob * (1 - seconds_remaining / 300))
        confidence = max(0.5, confidence)

        return SniperOpportunity(
            market=market,
            symbol=symbol,
            side=side,
            binance_price=current_price.price,
            price_change_pct=change_pct,
            implied_prob=implied_prob,
            market_price=market_price,
            edge=edge,
            seconds_remaining=seconds_remaining,
            confidence=confidence,
        )

    def opportunity_to_signal(self, opp: SniperOpportunity) -> Signal:
        """Convert a SniperOpportunity to a tradeable Signal."""
        return Signal(
            market=opp.market,
            side=opp.side,
            confidence=opp.confidence,
            edge=opp.edge,
            ev=opp.edge / opp.market_price if opp.market_price > 0 else 0,
            reasoning=(
                f"Crypto Sniper: {opp.symbol.upper()} moved {opp.price_change_pct:+.3%} "
                f"({opp.side.value} direction). "
                f"Binance: ${opp.binance_price:,.2f}. "
                f"Implied prob: {opp.implied_prob:.1%} vs market: {opp.market_price:.1%}. "
                f"Edge: {opp.edge:.1%}. "
                f"Time remaining: {opp.seconds_remaining:.0f}s."
            ),
            strategy=self.name,
        )

    def _compute_implied_probability(
        self,
        abs_price_change: float,
        seconds_remaining: float,
        volatility: float,
    ) -> float:
        """Compute the probability that price direction holds through expiry.

        Uses a model based on:
        - Larger moves are harder to reverse in limited time
        - Less time remaining = less room for reversal
        - Higher intra-window volatility = more uncertainty

        Calibrated against typical 5-minute BTC price dynamics:
        - BTC 5-min std dev is roughly 0.15-0.25%
        - A 2-sigma move with <60s left holds direction ~92% of the time
        - A 1-sigma move with <30s left holds ~80% of the time

        Returns probability in range [0.5, 0.99].
        """
        # Estimate expected remaining volatility
        # sqrt(time) scaling for random walk
        # Typical 5-min BTC volatility: ~0.3% (expressed as 0.003)
        # Using a slightly higher floor than raw data to be conservative
        # — overconfidence kills you faster than missed opportunities
        base_vol_per_5min = max(volatility, 0.003)  # Floor at 0.3%

        # Scale volatility by remaining time (sqrt scaling)
        # Add a buffer: use remaining_time + 15s to account for execution latency
        effective_remaining = seconds_remaining + 15.0
        remaining_fraction = max(effective_remaining / 300, 0.05)  # Assume 5-min window
        remaining_vol = base_vol_per_5min * math.sqrt(remaining_fraction)

        if remaining_vol <= 0:
            return 0.99

        # Z-score: how many standard deviations is our current move?
        z_score = abs_price_change / remaining_vol

        # Convert z-score to probability using normal CDF approximation
        # P(price stays in same direction) ≈ Φ(z)
        prob = _normal_cdf(z_score)

        # Clamp to reasonable range
        return max(0.50, min(0.99, prob))


def _normal_cdf(x: float) -> float:
    """Approximate the standard normal CDF.

    Uses the Abramowitz & Stegun approximation (formula 7.1.26).
    Accurate to within 0.0005 for all x. Much faster than scipy.
    """
    # Handle negative values via symmetry
    if x < 0:
        return 1.0 - _normal_cdf(-x)

    # Constants for the approximation
    b0 = 0.2316419
    b1 = 0.319381530
    b2 = -0.356563782
    b3 = 1.781477937
    b4 = -1.821255978
    b5 = 1.330274429

    t = 1.0 / (1.0 + b0 * x)
    pdf = math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)
    poly = t * (b1 + t * (b2 + t * (b3 + t * (b4 + t * b5))))

    return 1.0 - pdf * poly


def find_crypto_markets(markets: list[Market]) -> list[Market]:
    """Filter a list of markets to only short-duration crypto up/down markets."""
    return [m for m in markets if UP_DOWN_PATTERN.search(m.question)]


def match_market_to_symbol(market: Market) -> Optional[str]:
    """Extract Binance symbol from a crypto market question."""
    q = market.question.lower()
    for keyword, symbol in CRYPTO_SYMBOL_MAP.items():
        if keyword in q:
            return symbol
    return None
