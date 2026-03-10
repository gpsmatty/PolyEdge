"""Weather Sniper — exploit mispricings on Polymarket weather markets using
free, professional-grade weather forecast data.

The edge: Polymarket weather markets price events based on crowd sentiment.
Professional weather models (NOAA GFS, ECMWF) provide objective ensemble
forecasts that directly predict the same outcomes.  When the ensemble
disagrees with the market, we trade.

Two edge types:
1. FORECAST EDGE — Ensemble probability for a temperature bucket diverges
   from market price by >X%.  Buy the underpriced bucket.
2. NEG-RISK ARBITRAGE — Multi-bucket temperature events where YES prices
   don't sum to $1.00.  If sum > $1: guaranteed profit from selling.
   If sum < $1: guaranteed profit from buying all buckets.

No AI needed — pure data comparison.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

from polyedge.core.config import Settings
from polyedge.core.models import Market, Signal, Side
from polyedge.data.weather_feed import (
    EnsembleForecast,
    LOCATIONS,
    find_location,
)
from polyedge.strategies.base import Strategy

logger = logging.getLogger("polyedge.weather_sniper")


# --- Market Identification Patterns ---

# Temperature markets (multi-bucket events)
# e.g. "What will the highest temperature be in New York City on March 12?"
#       with buckets like "45°F to 49°F"
# e.g. "Highest temperature in Seoul on March 9?"
TEMP_EVENT_PATTERN = re.compile(
    r"(?:highest|high|lowest|low|maximum|minimum)\s+temp(?:erature)?\s+"
    r"(?:be\s+)?(?:in\s+)?"
    r"(.+?)\s+"
    r"(?:on\s+)?"
    r"((?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2})",
    re.IGNORECASE,
)

# Temperature bucket patterns (individual market outcomes)
# e.g. "45°F to 49°F", "Below 35°F", "55°F or above", "50°F or higher"
TEMP_BUCKET_RANGE = re.compile(
    r"(\d+)\s*°?\s*F?\s*(?:to|[-–])\s*(\d+)\s*°?\s*F?",
    re.IGNORECASE,
)
TEMP_BUCKET_BELOW = re.compile(
    r"(?:below|under|less than)\s*(\d+)\s*°?\s*F?",
    re.IGNORECASE,
)
TEMP_BUCKET_ABOVE = re.compile(
    r"(\d+)\s*°?\s*F?\s*(?:or\s+)?(?:above|higher|more|over)",
    re.IGNORECASE,
)

# Precipitation markets
# e.g. "Will precipitation in New York City in March exceed 3 inches?"
# e.g. "NYC precipitation in February?"
PRECIP_PATTERN = re.compile(
    r"(?:precipitation|rain(?:fall)?|snow(?:fall)?)\s+"
    r"(?:in\s+)?"
    r"(.+?)\s+"
    r"(?:in\s+)?"
    r"((?:January|February|March|April|May|June|July|August|September|October|November|December))",
    re.IGNORECASE,
)

# Generic weather question — catches anything weather-related
WEATHER_KEYWORDS = re.compile(
    r"(?:temperature|temp|precipitation|rain|snow|weather|°F|degrees|fahrenheit)",
    re.IGNORECASE,
)

# Month name to number
MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


@dataclass
class WeatherOpportunity:
    """A weather market opportunity ready to evaluate."""
    market: Market
    side: Side
    location_id: str
    weather_type: str          # "temperature_max", "temperature_min", "precipitation"
    forecast_prob: float       # Probability from ensemble forecast
    market_price: float        # What Polymarket is showing
    edge: float                # forecast_prob - market_price
    confidence: float          # Based on ensemble agreement + forecast horizon
    bucket_low: Optional[float] = None   # For bucket markets
    bucket_high: Optional[float] = None
    target_date: Optional[date] = None
    ensemble_mean: float = 0.0
    ensemble_std: float = 0.0
    n_ensemble_members: int = 0


@dataclass
class NegRiskOpportunity:
    """Neg-risk arbitrage on a multi-bucket weather event."""
    event_markets: list[Market]
    location_id: str
    target_date: date
    yes_price_sum: float       # Sum of all YES prices (should be ~1.0)
    arb_edge: float            # abs(1.0 - yes_price_sum)
    direction: str             # "buy_all" if sum < 1, "sell_all" if sum > 1


class WeatherSniperStrategy(Strategy):
    """Snipe weather markets using ensemble forecast data.

    Zero AI cost — just data comparison.
    """

    name = "weather_sniper"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.config = settings.strategies.weather_sniper

    def evaluate(self, market: Market) -> Signal | None:
        """Basic evaluate — not used for weather (needs forecast data)."""
        return None

    def is_weather_market(self, market: Market) -> bool:
        """Check if a market is a weather-related market."""
        return bool(WEATHER_KEYWORDS.search(market.question))

    def parse_market(self, market: Market) -> Optional[dict]:
        """Parse a weather market question to extract location, date, type, bucket.

        Returns dict with:
            location_id: str
            target_date: date (if extractable)
            weather_type: "temperature_max" | "temperature_min" | "precipitation"
            bucket_low: float | None
            bucket_high: float | None
            is_above: bool (for threshold markets like "above 50°F")
            is_below: bool
        """
        question = market.question
        result = {
            "location_id": None,
            "target_date": None,
            "weather_type": "temperature_max",
            "bucket_low": None,
            "bucket_high": None,
            "is_above": False,
            "is_below": False,
        }

        # Extract location from question or category
        location_id = find_location(question) or find_location(market.category)
        if not location_id:
            return None
        result["location_id"] = location_id

        # Determine weather type
        q_lower = question.lower()
        if any(w in q_lower for w in ["precipitation", "rain", "snow"]):
            result["weather_type"] = "precipitation"
        elif any(w in q_lower for w in ["lowest", "low", "minimum"]):
            result["weather_type"] = "temperature_min"
        else:
            result["weather_type"] = "temperature_max"

        # Extract date
        result["target_date"] = self._extract_date(question)

        # Extract temperature bucket
        range_match = TEMP_BUCKET_RANGE.search(question)
        if range_match:
            result["bucket_low"] = float(range_match.group(1))
            result["bucket_high"] = float(range_match.group(2))
        else:
            below_match = TEMP_BUCKET_BELOW.search(question)
            if below_match:
                result["bucket_high"] = float(below_match.group(1))
                result["bucket_low"] = -100.0  # Effectively -infinity
                result["is_below"] = True
            else:
                above_match = TEMP_BUCKET_ABOVE.search(question)
                if above_match:
                    result["bucket_low"] = float(above_match.group(1))
                    result["bucket_high"] = 200.0  # Effectively +infinity
                    result["is_above"] = True

        return result

    def evaluate_with_forecast(
        self,
        market: Market,
        forecast: EnsembleForecast,
        parsed: dict,
    ) -> Optional[WeatherOpportunity]:
        """Evaluate a weather market against ensemble forecast data.

        Args:
            market: The Polymarket weather market
            forecast: Ensemble forecast for the location and date
            parsed: Output from parse_market()
        """
        if not self.config.enabled:
            return None

        bucket_low = parsed.get("bucket_low")
        bucket_high = parsed.get("bucket_high")

        if bucket_low is None or bucket_high is None:
            # Can't evaluate without a bucket range
            return None

        # Compute probability from ensemble
        forecast_prob = forecast.probability_in_range(bucket_low, bucket_high)

        # Current market price (YES side = probability this bucket wins)
        market_price = market.yes_price

        if market_price <= 0.01 or market_price >= 0.99:
            return None  # Market at extreme — no edge

        # Edge = how much forecast disagrees with market
        edge = forecast_prob - market_price

        # Determine side
        if abs(edge) < self.config.min_edge:
            return None

        if edge > 0:
            # Forecast says higher probability than market — buy YES
            side = Side.YES
        else:
            # Forecast says lower probability — buy NO (sell YES)
            side = Side.NO
            market_price = market.no_price
            edge = abs(edge)

        # Confidence based on ensemble agreement and forecast horizon
        confidence = self._compute_confidence(forecast, parsed)

        if confidence < self.config.min_confidence:
            return None

        return WeatherOpportunity(
            market=market,
            side=side,
            location_id=parsed["location_id"],
            weather_type=parsed["weather_type"],
            forecast_prob=forecast_prob,
            market_price=market_price,
            edge=edge,
            confidence=confidence,
            bucket_low=bucket_low,
            bucket_high=bucket_high,
            target_date=parsed.get("target_date"),
            ensemble_mean=forecast.mean,
            ensemble_std=forecast.std,
            n_ensemble_members=forecast.n_members,
        )

    def detect_neg_risk(
        self,
        event_markets: list[Market],
        location_id: str,
        target_date: date,
    ) -> Optional[NegRiskOpportunity]:
        """Detect neg-risk arbitrage in a multi-bucket weather event.

        If all YES prices for buckets in the same event don't sum to 1.0,
        there's a risk-free profit opportunity.

        Args:
            event_markets: All markets in the same event (all temp buckets)
            location_id: Location ID
            target_date: Event date
        """
        if len(event_markets) < 3:
            return None  # Need multiple buckets

        yes_sum = sum(m.yes_price for m in event_markets)

        # Account for the market's implicit spread/fees
        arb_edge = abs(1.0 - yes_sum)

        if arb_edge < self.config.min_neg_risk_edge:
            return None

        direction = "buy_all" if yes_sum < 1.0 else "sell_all"

        return NegRiskOpportunity(
            event_markets=event_markets,
            location_id=location_id,
            target_date=target_date,
            yes_price_sum=yes_sum,
            arb_edge=arb_edge,
            direction=direction,
        )

    def opportunity_to_signal(self, opp: WeatherOpportunity) -> Signal:
        """Convert a WeatherOpportunity to a tradeable Signal."""
        loc = LOCATIONS.get(opp.location_id, {})
        loc_name = loc.get("name", opp.location_id)

        bucket_str = ""
        if opp.bucket_low is not None and opp.bucket_high is not None:
            if opp.bucket_low <= -50:
                bucket_str = f"below {opp.bucket_high:.0f}°F"
            elif opp.bucket_high >= 150:
                bucket_str = f"above {opp.bucket_low:.0f}°F"
            else:
                bucket_str = f"{opp.bucket_low:.0f}-{opp.bucket_high:.0f}°F"

        date_str = opp.target_date.isoformat() if opp.target_date else "unknown"

        return Signal(
            market=opp.market,
            side=opp.side,
            confidence=opp.confidence,
            edge=opp.edge,
            ev=opp.edge / opp.market_price if opp.market_price > 0 else 0,
            reasoning=(
                f"Weather Sniper: {loc_name} {opp.weather_type} on {date_str}. "
                f"Bucket: {bucket_str}. "
                f"Ensemble: {opp.forecast_prob:.1%} ({opp.n_ensemble_members} members, "
                f"mean={opp.ensemble_mean:.1f}°F, std={opp.ensemble_std:.1f}°F) "
                f"vs market: {opp.market_price:.1%}. "
                f"Edge: {opp.edge:.1%}."
            ),
            strategy=self.name,
        )

    def _compute_confidence(
        self,
        forecast: EnsembleForecast,
        parsed: dict,
    ) -> float:
        """Compute confidence score based on forecast quality.

        Higher confidence when:
        - More ensemble members agree
        - Forecast is for closer dates (less uncertainty)
        - Lower standard deviation (models agree)
        """
        confidence = 0.5  # Base

        # Ensemble agreement boost
        bucket_low = parsed.get("bucket_low", 0)
        bucket_high = parsed.get("bucket_high", 100)
        prob = forecast.probability_in_range(bucket_low, bucket_high)

        # Strong consensus: if >80% or <20% of members agree, high confidence
        if prob > 0.8 or prob < 0.2:
            confidence += 0.2
        elif prob > 0.6 or prob < 0.4:
            confidence += 0.1

        # Forecast horizon: closer dates are more reliable
        target_date = parsed.get("target_date")
        if target_date:
            days_ahead = (target_date - date.today()).days
            if days_ahead <= 1:
                confidence += 0.2  # Tomorrow — very reliable
            elif days_ahead <= 3:
                confidence += 0.1  # 2-3 days — still good
            elif days_ahead > 7:
                confidence -= 0.1  # Beyond a week — less reliable

        # Ensemble size boost
        if forecast.n_members >= 30:
            confidence += 0.05
        if forecast.n_members >= 50:
            confidence += 0.05

        # Low spread (models agree) boost
        if forecast.std < 2.0:  # < 2°F standard deviation
            confidence += 0.1
        elif forecast.std > 5.0:  # > 5°F — models disagree
            confidence -= 0.1

        return max(0.0, min(1.0, confidence))

    def _extract_date(self, question: str) -> Optional[date]:
        """Extract target date from a weather market question."""
        # Pattern: "March 12", "April 5", etc.
        date_pattern = re.compile(
            r"(January|February|March|April|May|June|July|August|"
            r"September|October|November|December)\s+(\d{1,2})",
            re.IGNORECASE,
        )
        match = date_pattern.search(question)
        if not match:
            return None

        month_name = match.group(1).lower()
        day = int(match.group(2))
        month = MONTH_MAP.get(month_name)
        if not month:
            return None

        # Assume current year (or next year if date has passed)
        today = date.today()
        try:
            target = date(today.year, month, day)
            if target < today:
                target = date(today.year + 1, month, day)
            return target
        except ValueError:
            return None


# --- Module-level helpers ---


def find_weather_markets(markets: list[Market]) -> list[Market]:
    """Filter a list of markets to only weather-related markets."""
    return [m for m in markets if WEATHER_KEYWORDS.search(m.question)]


def group_weather_events(markets: list[Market]) -> dict[str, list[Market]]:
    """Group weather markets by event (same location + date + type).

    Returns dict mapping event_key -> list of markets (one per bucket).
    """
    events: dict[str, list[Market]] = {}

    for market in markets:
        # Try to group by raw event data from Gamma API
        raw = market.raw
        # Polymarket groups multi-outcome events by groupItemTitle or parent slug
        group_slug = raw.get("groupSlug") or raw.get("group_slug", "")
        if group_slug:
            key = group_slug
        else:
            # Fallback: group by category (which is groupItemTitle from _parse_market)
            key = market.category if market.category else market.condition_id

        if key not in events:
            events[key] = []
        events[key].append(market)

    return events
