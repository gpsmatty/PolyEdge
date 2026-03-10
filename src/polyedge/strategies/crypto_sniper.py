"""Crypto Sniper — exploit ALL Polymarket crypto markets using real-time
price feeds from Binance.

Handles three market types:

1. UP/DOWN — "BTC 5 Minute Up or Down" (short-duration direction bets)
   Edge: Binance spot moves before Polymarket reprices.
   Model: P(direction holds) via normal CDF on z-score of price move.

2. THRESHOLD — "Bitcoin above 70,000 on March 10?" (price level bets)
   Edge: Current price trajectory vs market's implied probability.
   Model: P(price > strike at expiry) via normal CDF with drift.

3. BUCKET — "What price will Bitcoin hit on March 9?" (price range bets)
   Edge: Current price position relative to bucket boundaries.
   Model: P(low < price < high at expiry) = CDF(high) - CDF(low).

All three use the same core math: normal CDF with sqrt(time) volatility
scaling.  No AI needed — pure math and speed.
"""

from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from polyedge.core.config import Settings
from polyedge.core.models import Market, Signal, Side
from polyedge.data.binance_feed import PriceSnapshot, PriceWindow
from polyedge.strategies.base import Strategy

logger = logging.getLogger("polyedge.crypto_sniper")


# ---------------------------------------------------------------------------
# Market type classification
# ---------------------------------------------------------------------------

class CryptoMarketType(str, Enum):
    """The three types of crypto markets on Polymarket."""
    UP_DOWN = "up_down"         # "BTC 5 Minute Up or Down"
    THRESHOLD = "threshold"     # "Bitcoin above 70,000 on March 10?"
    BUCKET = "bucket"           # "What price will Bitcoin hit on March 9?"


# ---------------------------------------------------------------------------
# Symbol mapping — keywords in questions -> Binance trading pairs
# ---------------------------------------------------------------------------

CRYPTO_SYMBOL_MAP = {
    "bitcoin": "btcusdt",
    "btc": "btcusdt",
    "ethereum": "ethusdt",
    "eth": "ethusdt",
    "ether": "ethusdt",
    "solana": "solusdt",
    "sol": "solusdt",
    "xrp": "xrpusdt",
    "ripple": "xrpusdt",
    "dogecoin": "dogeusdt",
    "doge": "dogeusdt",
}

# Supported Binance symbols (must also be in config.symbols to actually track)
ALL_SUPPORTED_SYMBOLS = list(set(CRYPTO_SYMBOL_MAP.values()))


# ---------------------------------------------------------------------------
# Regex patterns for each market type
# ---------------------------------------------------------------------------

# Type 1: Up or Down markets
# "BTC 5 Minute Up or Down", "Solana Up or Down - March 10, 3:10PM-3:15PM ET"
UP_DOWN_PATTERN = re.compile(
    r"(Bitcoin|BTC|Ethereum|ETH|Ether|Solana|SOL|XRP|Ripple|Dogecoin|DOGE)\s+"
    r".*?[Uu]p\s+or\s+[Dd]own",
    re.IGNORECASE,
)

# Type 2: Threshold markets
# "Bitcoin above ___ on March 10?", "Ethereum above ___ on March 10?"
# "Solana above ___ on March 10?"
THRESHOLD_PATTERN = re.compile(
    r"(Bitcoin|BTC|Ethereum|ETH|Ether|Solana|SOL|XRP|Ripple|Dogecoin|DOGE)\s+"
    r"above\s+[\$]?([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)

# Type 3: Bucket / price target markets
# "What price will Bitcoin hit on March 9?"
# "What price will Solana hit in March?"
BUCKET_PATTERN = re.compile(
    r"[Ww]hat\s+price\s+will\s+"
    r"(Bitcoin|BTC|Ethereum|ETH|Ether|Solana|SOL|XRP|Ripple|Dogecoin|DOGE)\s+"
    r"hit",
    re.IGNORECASE,
)

# Extract threshold value from question text (for threshold markets)
# Matches: "above 70,000", "above $1,500", "above 85.50"
THRESHOLD_VALUE_PATTERN = re.compile(
    r"above\s+[\$]?([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)

# Extract "up" arrow price targets from bucket markets
# Matches: "↑ 70,000" or "↑ 110" (means price >= this value)
BUCKET_ABOVE_PATTERN = re.compile(
    r"[↑]\s*[\$]?([\d,]+(?:\.\d+)?)",
)

# Extract "down" arrow price targets from bucket markets
# Matches: "↓ 66,000" (means price <= this value)
BUCKET_BELOW_PATTERN = re.compile(
    r"[↓]\s*[\$]?([\d,]+(?:\.\d+)?)",
)

# Extract price range for bucket markets from question or description
# Matches: "$68,000 to $70,000", "85 to 90", "$1,500-$1,600"
BUCKET_RANGE_PATTERN = re.compile(
    r"[\$]?([\d,]+(?:\.\d+)?)\s*(?:to|[-–])\s*[\$]?([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)

# Pattern to extract time window duration from question
# "3:10PM-3:15PM" -> 5 minutes
DURATION_PATTERN = re.compile(
    r"(\d{1,2}):(\d{2})\s*(AM|PM)\s*[-–]\s*(\d{1,2}):(\d{2})\s*(AM|PM)",
    re.IGNORECASE,
)

# Broad pattern to identify ANY crypto market (used as pre-filter)
CRYPTO_KEYWORDS = re.compile(
    r"\b(Bitcoin|BTC|Ethereum|ETH|Ether|Solana|SOL|XRP|Ripple|Dogecoin|DOGE)\b",
    re.IGNORECASE,
)

# Annualized volatility estimates per symbol (conservative, for scaling)
# Used when we don't have a live price window yet
DEFAULT_ANNUAL_VOL = {
    "btcusdt": 0.60,    # ~60% annualized
    "ethusdt": 0.75,    # ~75% annualized
    "solusdt": 0.90,    # ~90% annualized
    "xrpusdt": 0.85,    # ~85% annualized
    "dogeusdt": 1.00,   # ~100% annualized
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ParsedCryptoMarket:
    """Result of parsing a crypto market question."""
    market_type: CryptoMarketType
    symbol: str                           # Binance symbol e.g. "btcusdt"
    strike: Optional[float] = None        # For threshold: the price level
    bucket_low: Optional[float] = None    # For bucket: lower bound
    bucket_high: Optional[float] = None   # For bucket: upper bound
    bucket_direction: Optional[str] = None  # "above" or "below" for arrow buckets


@dataclass
class SniperOpportunity:
    """A crypto sniper opportunity ready to trade."""
    market: Market
    market_type: CryptoMarketType
    symbol: str                  # e.g. "btcusdt"
    side: Side                   # YES or NO
    binance_price: float         # Current Binance spot price
    price_change_pct: float      # Price change in the window so far
    implied_prob: float          # Our estimated true probability from price data
    market_price: float          # What Polymarket is showing
    edge: float                  # implied_prob - market_price
    seconds_remaining: float     # Time left in the window
    confidence: float            # How confident we are
    strike: Optional[float] = None  # For threshold/bucket context


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

class CryptoSniperStrategy(Strategy):
    """Snipe all crypto prediction markets using Binance price feed.

    Handles up/down, threshold ("above X"), and bucket ("what price") markets.
    No AI — pure math and speed.
    """

    name = "crypto_sniper"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.config = settings.strategies.crypto_sniper

    def evaluate(self, market: Market) -> Signal | None:
        """Basic evaluate — not used for sniper (needs price data)."""
        return None

    # ------------------------------------------------------------------
    # Market classification and parsing
    # ------------------------------------------------------------------

    def classify_market(self, market: Market) -> Optional[CryptoMarketType]:
        """Determine what type of crypto market this is, or None."""
        q = market.question
        if UP_DOWN_PATTERN.search(q):
            return CryptoMarketType.UP_DOWN
        if THRESHOLD_PATTERN.search(q):
            return CryptoMarketType.THRESHOLD
        if BUCKET_PATTERN.search(q):
            return CryptoMarketType.BUCKET
        return None

    def parse_market(self, market: Market) -> Optional[ParsedCryptoMarket]:
        """Parse a crypto market question into structured data."""
        mtype = self.classify_market(market)
        if mtype is None:
            return None

        symbol = self.get_symbol(market)
        if not symbol:
            return None

        if mtype == CryptoMarketType.UP_DOWN:
            return ParsedCryptoMarket(market_type=mtype, symbol=symbol)

        if mtype == CryptoMarketType.THRESHOLD:
            strike = self._extract_threshold(market.question)
            if strike is None:
                return None
            return ParsedCryptoMarket(
                market_type=mtype, symbol=symbol, strike=strike,
            )

        if mtype == CryptoMarketType.BUCKET:
            return self._parse_bucket_market(market, symbol)

        return None

    def get_symbol(self, market: Market) -> Optional[str]:
        """Extract the Binance symbol from a crypto market question."""
        q = market.question.lower()
        # Check longer keywords first to avoid partial matches
        # e.g. "ethereum" before "eth", "solana" before "sol"
        for keyword in sorted(CRYPTO_SYMBOL_MAP.keys(), key=len, reverse=True):
            if keyword in q:
                return CRYPTO_SYMBOL_MAP[keyword]
        return None

    def get_window_duration_minutes(self, market: Market) -> Optional[int]:
        """Extract the window duration in minutes from the market question."""
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

    # ------------------------------------------------------------------
    # Evaluation — dispatches to the right model per market type
    # ------------------------------------------------------------------

    def evaluate_with_price(
        self,
        market: Market,
        price_window: PriceWindow,
        current_price: PriceSnapshot,
        seconds_remaining: float,
        parsed: Optional[ParsedCryptoMarket] = None,
    ) -> Optional[SniperOpportunity]:
        """Evaluate any crypto market type against real-time Binance price."""
        if not self.config.enabled:
            return None

        if seconds_remaining <= 0:
            return None

        # For up/down, keep the tight entry window. For threshold/bucket,
        # allow wider windows since the edge can persist longer.
        if parsed is None:
            parsed = self.parse_market(market)
        if parsed is None:
            return None

        if parsed.market_type == CryptoMarketType.UP_DOWN:
            if seconds_remaining > self.config.max_seconds_before_entry:
                return None
            return self._evaluate_up_down(
                market, parsed, price_window, current_price, seconds_remaining,
            )
        elif parsed.market_type == CryptoMarketType.THRESHOLD:
            return self._evaluate_threshold(
                market, parsed, current_price, seconds_remaining,
            )
        elif parsed.market_type == CryptoMarketType.BUCKET:
            return self._evaluate_bucket(
                market, parsed, current_price, seconds_remaining,
            )

        return None

    # ------------------------------------------------------------------
    # Type 1: Up/Down evaluation (original sniper logic)
    # ------------------------------------------------------------------

    def _evaluate_up_down(
        self,
        market: Market,
        parsed: ParsedCryptoMarket,
        price_window: PriceWindow,
        current_price: PriceSnapshot,
        seconds_remaining: float,
    ) -> Optional[SniperOpportunity]:
        """Evaluate an up/down market — original sniper logic."""
        change_pct = price_window.change_pct
        abs_change = abs(change_pct)

        if abs_change < self.config.min_price_move_pct:
            return None

        implied_prob = self._compute_direction_probability(
            abs_change, seconds_remaining, price_window.volatility
        )

        if change_pct > 0:
            side = Side.YES
            market_price = market.yes_price
        else:
            side = Side.NO
            market_price = market.no_price

        edge = implied_prob - market_price
        if edge < self.config.min_edge:
            return None

        confidence = min(0.95, implied_prob * (1 - seconds_remaining / 300))
        confidence = max(0.5, confidence)

        return SniperOpportunity(
            market=market,
            market_type=CryptoMarketType.UP_DOWN,
            symbol=parsed.symbol,
            side=side,
            binance_price=current_price.price,
            price_change_pct=change_pct,
            implied_prob=implied_prob,
            market_price=market_price,
            edge=edge,
            seconds_remaining=seconds_remaining,
            confidence=confidence,
        )

    # ------------------------------------------------------------------
    # Type 2: Threshold evaluation ("Bitcoin above 70,000")
    # ------------------------------------------------------------------

    def _evaluate_threshold(
        self,
        market: Market,
        parsed: ParsedCryptoMarket,
        current_price: PriceSnapshot,
        seconds_remaining: float,
    ) -> Optional[SniperOpportunity]:
        """Evaluate a threshold market — P(price > strike at expiry)."""
        strike = parsed.strike
        if strike is None or strike <= 0:
            return None

        price = current_price.price
        if price <= 0:
            return None

        # Compute P(price > strike at expiry) using log-normal model
        implied_prob = self._compute_threshold_probability(
            current_price=price,
            strike=strike,
            seconds_remaining=seconds_remaining,
            symbol=parsed.symbol,
        )

        # If prob > 0.5, the market should be pricing YES high
        # If prob < 0.5, the market should be pricing YES low (NO high)
        if implied_prob >= 0.5:
            side = Side.YES
            market_price = market.yes_price
            edge = implied_prob - market_price
        else:
            side = Side.NO
            market_price = market.no_price
            # Our implied NO prob = 1 - implied_prob
            edge = (1 - implied_prob) - market_price

        if edge < self.config.min_edge:
            return None

        # Confidence is higher when price is far from strike
        distance_pct = abs(price - strike) / strike
        time_factor = max(0.3, 1 - seconds_remaining / 86400)  # Decay over 1 day
        confidence = min(0.95, 0.5 + distance_pct * 5 * time_factor)
        confidence = max(0.5, confidence)

        return SniperOpportunity(
            market=market,
            market_type=CryptoMarketType.THRESHOLD,
            symbol=parsed.symbol,
            side=side,
            binance_price=price,
            price_change_pct=(price - strike) / strike,
            implied_prob=implied_prob if side == Side.YES else (1 - implied_prob),
            market_price=market_price,
            edge=edge,
            seconds_remaining=seconds_remaining,
            confidence=confidence,
            strike=strike,
        )

    # ------------------------------------------------------------------
    # Type 3: Bucket evaluation ("What price will Bitcoin hit")
    # ------------------------------------------------------------------

    def _evaluate_bucket(
        self,
        market: Market,
        parsed: ParsedCryptoMarket,
        current_price: PriceSnapshot,
        seconds_remaining: float,
    ) -> Optional[SniperOpportunity]:
        """Evaluate a bucket market — P(low < price < high at expiry)."""
        price = current_price.price
        if price <= 0:
            return None

        # Handle arrow-style buckets (↑ 110 means "above 110", ↓ 66000 means "below 66000")
        if parsed.bucket_direction == "above" and parsed.bucket_low is not None:
            implied_prob = self._compute_threshold_probability(
                current_price=price,
                strike=parsed.bucket_low,
                seconds_remaining=seconds_remaining,
                symbol=parsed.symbol,
            )
        elif parsed.bucket_direction == "below" and parsed.bucket_high is not None:
            prob_above = self._compute_threshold_probability(
                current_price=price,
                strike=parsed.bucket_high,
                seconds_remaining=seconds_remaining,
                symbol=parsed.symbol,
            )
            implied_prob = 1 - prob_above
        elif parsed.bucket_low is not None and parsed.bucket_high is not None:
            # Range bucket: P(low < price < high)
            implied_prob = self._compute_bucket_probability(
                current_price=price,
                bucket_low=parsed.bucket_low,
                bucket_high=parsed.bucket_high,
                seconds_remaining=seconds_remaining,
                symbol=parsed.symbol,
            )
        else:
            return None

        # Compare to YES price (YES = price lands in this bucket)
        if implied_prob >= 0.5:
            side = Side.YES
            market_price = market.yes_price
            edge = implied_prob - market_price
        else:
            side = Side.NO
            market_price = market.no_price
            edge = (1 - implied_prob) - market_price

        if edge < self.config.min_edge:
            return None

        # Confidence based on how far price is from bucket boundaries
        if parsed.bucket_low and parsed.bucket_high:
            bucket_mid = (parsed.bucket_low + parsed.bucket_high) / 2
            distance_pct = abs(price - bucket_mid) / bucket_mid
        elif parsed.bucket_low:
            distance_pct = abs(price - parsed.bucket_low) / parsed.bucket_low
        elif parsed.bucket_high:
            distance_pct = abs(price - parsed.bucket_high) / parsed.bucket_high
        else:
            distance_pct = 0

        time_factor = max(0.3, 1 - seconds_remaining / 86400)
        confidence = min(0.95, 0.5 + distance_pct * 5 * time_factor)
        confidence = max(0.5, confidence)

        return SniperOpportunity(
            market=market,
            market_type=CryptoMarketType.BUCKET,
            symbol=parsed.symbol,
            side=side,
            binance_price=price,
            price_change_pct=(price - (parsed.bucket_low or price)) / price,
            implied_prob=implied_prob if side == Side.YES else (1 - implied_prob),
            market_price=market_price,
            edge=edge,
            seconds_remaining=seconds_remaining,
            confidence=confidence,
            strike=parsed.bucket_low,
        )

    # ------------------------------------------------------------------
    # Probability models
    # ------------------------------------------------------------------

    def _compute_direction_probability(
        self,
        abs_price_change: float,
        seconds_remaining: float,
        volatility: float,
    ) -> float:
        """Compute P(price direction holds) for up/down markets.

        Original sniper model — unchanged.
        """
        base_vol_per_5min = max(volatility, 0.003)
        effective_remaining = seconds_remaining + 15.0
        remaining_fraction = max(effective_remaining / 300, 0.05)
        remaining_vol = base_vol_per_5min * math.sqrt(remaining_fraction)

        if remaining_vol <= 0:
            return 0.99

        z_score = abs_price_change / remaining_vol
        prob = _normal_cdf(z_score)
        return max(0.50, min(0.99, prob))

    def _compute_threshold_probability(
        self,
        current_price: float,
        strike: float,
        seconds_remaining: float,
        symbol: str,
    ) -> float:
        """Compute P(price > strike at expiry) using log-normal model.

        Uses GBM assumption: log(S_T/S_0) ~ N(-0.5*σ²*t, σ²*t)
        P(S_T > K) = Φ((log(S/K) + 0.5*σ²*t) / (σ*√t))

        For simplicity and conservatism, we use zero drift (no directional
        assumption): P(S_T > K) = Φ(log(S/K) / (σ*√t))
        """
        if current_price <= 0 or strike <= 0:
            return 0.5

        # Get annualized vol for this symbol
        annual_vol = DEFAULT_ANNUAL_VOL.get(symbol, 0.70)

        # Convert seconds to fraction of year
        t_years = seconds_remaining / (365.25 * 24 * 3600)
        if t_years <= 0:
            # Already expired — it's either above or below
            return 0.99 if current_price > strike else 0.01

        vol_t = annual_vol * math.sqrt(t_years)
        if vol_t <= 0:
            return 0.99 if current_price > strike else 0.01

        # z = log(S/K) / (σ√t) — zero-drift model (conservative)
        z = math.log(current_price / strike) / vol_t

        prob = _normal_cdf(z)
        return max(0.01, min(0.99, prob))

    def _compute_bucket_probability(
        self,
        current_price: float,
        bucket_low: float,
        bucket_high: float,
        seconds_remaining: float,
        symbol: str,
    ) -> float:
        """Compute P(bucket_low < price < bucket_high at expiry).

        = P(price > low) - P(price > high)
        """
        prob_above_low = self._compute_threshold_probability(
            current_price, bucket_low, seconds_remaining, symbol,
        )
        prob_above_high = self._compute_threshold_probability(
            current_price, bucket_high, seconds_remaining, symbol,
        )
        prob = prob_above_low - prob_above_high
        return max(0.01, min(0.99, prob))

    # ------------------------------------------------------------------
    # Signal conversion
    # ------------------------------------------------------------------

    def opportunity_to_signal(self, opp: SniperOpportunity) -> Signal:
        """Convert a SniperOpportunity to a tradeable Signal."""
        if opp.market_type == CryptoMarketType.UP_DOWN:
            reasoning = (
                f"Crypto Sniper [UP/DOWN]: {opp.symbol.upper()} moved "
                f"{opp.price_change_pct:+.3%} ({opp.side.value} direction). "
                f"Binance: ${opp.binance_price:,.2f}. "
                f"Implied: {opp.implied_prob:.1%} vs market: {opp.market_price:.1%}. "
                f"Edge: {opp.edge:.1%}. Time left: {opp.seconds_remaining:.0f}s."
            )
        elif opp.market_type == CryptoMarketType.THRESHOLD:
            reasoning = (
                f"Crypto Sniper [THRESHOLD]: {opp.symbol.upper()} at "
                f"${opp.binance_price:,.2f} vs strike ${opp.strike:,.2f}. "
                f"Side: {opp.side.value}. "
                f"Implied: {opp.implied_prob:.1%} vs market: {opp.market_price:.1%}. "
                f"Edge: {opp.edge:.1%}. Time left: {opp.seconds_remaining/3600:.1f}h."
            )
        else:  # BUCKET
            reasoning = (
                f"Crypto Sniper [BUCKET]: {opp.symbol.upper()} at "
                f"${opp.binance_price:,.2f}. "
                f"Side: {opp.side.value}. "
                f"Implied: {opp.implied_prob:.1%} vs market: {opp.market_price:.1%}. "
                f"Edge: {opp.edge:.1%}. Time left: {opp.seconds_remaining/3600:.1f}h."
            )

        return Signal(
            market=opp.market,
            side=opp.side,
            confidence=opp.confidence,
            edge=opp.edge,
            ev=opp.edge / opp.market_price if opp.market_price > 0 else 0,
            reasoning=reasoning,
            strategy=self.name,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_threshold(question: str) -> Optional[float]:
        """Extract the strike price from a threshold market question."""
        match = THRESHOLD_VALUE_PATTERN.search(question)
        if match:
            return _parse_number(match.group(1))
        return None

    def _parse_bucket_market(
        self, market: Market, symbol: str,
    ) -> Optional[ParsedCryptoMarket]:
        """Parse a bucket market, extracting price range from question/description."""
        q = market.question
        desc = market.description or ""

        # Check for arrow-style buckets in the question
        # "↑ 70,000" means "above $70,000"
        above_match = BUCKET_ABOVE_PATTERN.search(q) or BUCKET_ABOVE_PATTERN.search(desc)
        if above_match:
            val = _parse_number(above_match.group(1))
            if val:
                return ParsedCryptoMarket(
                    market_type=CryptoMarketType.BUCKET,
                    symbol=symbol,
                    bucket_low=val,
                    bucket_direction="above",
                )

        # "↓ 66,000" means "below $66,000"
        below_match = BUCKET_BELOW_PATTERN.search(q) or BUCKET_BELOW_PATTERN.search(desc)
        if below_match:
            val = _parse_number(below_match.group(1))
            if val:
                return ParsedCryptoMarket(
                    market_type=CryptoMarketType.BUCKET,
                    symbol=symbol,
                    bucket_high=val,
                    bucket_direction="below",
                )

        # Check for range-style buckets in description
        range_match = BUCKET_RANGE_PATTERN.search(desc) or BUCKET_RANGE_PATTERN.search(q)
        if range_match:
            low = _parse_number(range_match.group(1))
            high = _parse_number(range_match.group(2))
            if low and high and low < high:
                return ParsedCryptoMarket(
                    market_type=CryptoMarketType.BUCKET,
                    symbol=symbol,
                    bucket_low=low,
                    bucket_high=high,
                )

        # Couldn't parse bucket boundaries — skip
        return None

    # Legacy compatibility
    def is_crypto_market(self, market: Market) -> bool:
        """Check if a market is any type of crypto market we can trade."""
        return self.classify_market(market) is not None


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _normal_cdf(x: float) -> float:
    """Approximate the standard normal CDF.

    Uses the Abramowitz & Stegun approximation (formula 7.1.26).
    Accurate to within 0.0005 for all x.  Much faster than scipy.
    """
    if x < 0:
        return 1.0 - _normal_cdf(-x)

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


def _parse_number(s: str) -> Optional[float]:
    """Parse a number string, handling commas. '70,000' -> 70000.0"""
    try:
        return float(s.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def find_crypto_markets(markets: list[Market]) -> list[Market]:
    """Filter markets to ALL tradeable crypto markets (up/down + threshold + bucket)."""
    results = []
    for m in markets:
        if CRYPTO_KEYWORDS.search(m.question):
            # Check if it matches any of our supported types
            if (UP_DOWN_PATTERN.search(m.question) or
                    THRESHOLD_PATTERN.search(m.question) or
                    BUCKET_PATTERN.search(m.question)):
                results.append(m)
    return results


def match_market_to_symbol(market: Market) -> Optional[str]:
    """Extract Binance symbol from a crypto market question."""
    q = market.question.lower()
    for keyword in sorted(CRYPTO_SYMBOL_MAP.keys(), key=len, reverse=True):
        if keyword in q:
            return CRYPTO_SYMBOL_MAP[keyword]
    return None
