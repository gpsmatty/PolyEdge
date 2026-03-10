"""Tests for the weather sniper strategy.

Tests cover:
- EnsembleForecast probability calculations
- Market question parsing (location, date, bucket extraction)
- Temperature bucket probability computation
- Weather market identification
- Event grouping logic
- Confidence scoring
- Neg-risk detection
- WeatherSniperConfig defaults
"""

import pytest
from datetime import date
from unittest.mock import MagicMock

from polyedge.data.weather_feed import EnsembleForecast, find_location, LOCATIONS
from polyedge.strategies.weather_sniper import (
    WeatherSniperStrategy,
    WeatherOpportunity,
    find_weather_markets,
    group_weather_events,
    TEMP_BUCKET_RANGE,
    TEMP_BUCKET_BELOW,
    TEMP_BUCKET_ABOVE,
    WEATHER_KEYWORDS,
)
from polyedge.core.config import Settings, WeatherSniperConfig
from polyedge.core.models import Market, Side


# --- Fixtures ---


def make_market(
    question: str,
    yes_price: float = 0.5,
    category: str = "",
    condition_id: str = "test_cid",
    liquidity: float = 1000,
    raw: dict = None,
) -> Market:
    """Helper to create a Market for testing."""
    return Market(
        condition_id=condition_id,
        question=question,
        category=category,
        yes_price=yes_price,
        no_price=1.0 - yes_price,
        liquidity=liquidity,
        clob_token_ids=["token_yes", "token_no"],
        raw=raw or {},
    )


def make_settings() -> Settings:
    """Create Settings with default WeatherSniperConfig."""
    settings = Settings()
    return settings


def make_ensemble(
    values: list[float],
    location_id: str = "nyc",
    target_date: date = None,
    metric: str = "temperature_max",
) -> EnsembleForecast:
    """Helper to create an EnsembleForecast."""
    import statistics
    if target_date is None:
        target_date = date.today()

    return EnsembleForecast(
        location_id=location_id,
        target_date=target_date,
        metric=metric,
        ensemble_values=values,
        mean=statistics.mean(values) if values else 0,
        std=statistics.stdev(values) if len(values) > 1 else 0,
        min_val=min(values) if values else 0,
        max_val=max(values) if values else 0,
        source="open_meteo",
        model="gfs_seamless",
        fetched_at=1000000,
        unit="°F",
    )


# --- EnsembleForecast Tests ---


class TestEnsembleForecast:
    """Test ensemble probability calculations."""

    def test_probability_in_range_basic(self):
        """50 values: 20 in range 45-50, 30 outside → 40%."""
        values = [42, 43, 44, 44, 44] * 6 + [45, 46, 47, 48, 49] * 4
        forecast = make_ensemble(values)
        prob = forecast.probability_in_range(45, 50)
        assert abs(prob - 0.4) < 0.01

    def test_probability_in_range_all_inside(self):
        """All values in range → 100%."""
        values = [46, 47, 48, 49, 47, 48, 46, 49, 47, 48]
        forecast = make_ensemble(values)
        prob = forecast.probability_in_range(45, 50)
        assert prob == 1.0

    def test_probability_in_range_none_inside(self):
        """No values in range → 0%."""
        values = [30, 31, 32, 33, 34, 35, 36, 37, 38, 39]
        forecast = make_ensemble(values)
        prob = forecast.probability_in_range(45, 50)
        assert prob == 0.0

    def test_probability_above(self):
        """7 out of 10 above threshold → 70%."""
        values = [40, 42, 44, 50, 52, 54, 56, 58, 60, 62]
        forecast = make_ensemble(values)
        prob = forecast.probability_above(50)
        assert abs(prob - 0.7) < 0.01

    def test_probability_below(self):
        """3 out of 10 below threshold → 30%."""
        values = [40, 42, 44, 50, 52, 54, 56, 58, 60, 62]
        forecast = make_ensemble(values)
        prob = forecast.probability_below(50)
        assert abs(prob - 0.3) < 0.01

    def test_empty_ensemble(self):
        """Empty ensemble returns 0 probability."""
        forecast = make_ensemble([])
        assert forecast.probability_in_range(40, 50) == 0.0
        assert forecast.probability_above(50) == 0.0
        assert forecast.probability_below(50) == 0.0

    def test_n_members(self):
        forecast = make_ensemble([45, 46, 47, 48, 49])
        assert forecast.n_members == 5

    def test_boundary_behavior(self):
        """Range is [low, high) — inclusive lower, exclusive upper."""
        values = [45.0, 50.0]  # 45 is in [45, 50), 50 is NOT in [45, 50)
        forecast = make_ensemble(values)
        prob = forecast.probability_in_range(45, 50)
        assert prob == 0.5  # Only 45 counts


# --- Location Matching ---


class TestLocationMatching:
    def test_find_nyc(self):
        assert find_location("temperature in New York City") == "nyc"

    def test_find_nyc_alias(self):
        assert find_location("NYC precipitation") == "nyc"

    def test_find_london(self):
        assert find_location("highest temperature in London") == "london"

    def test_find_seoul(self):
        assert find_location("Seoul temperature on March 9") == "seoul"

    def test_no_match(self):
        assert find_location("temperature in Tokyo") is None

    def test_case_insensitive(self):
        assert find_location("LONDON weather") == "london"


# --- Market Question Parsing ---


class TestMarketParsing:
    def setup_method(self):
        self.strategy = WeatherSniperStrategy(make_settings())

    def test_parse_temp_bucket_range(self):
        market = make_market("45°F to 49°F", category="Highest temperature in NYC on March 12")
        parsed = self.strategy.parse_market(market)
        assert parsed is not None
        assert parsed["location_id"] == "nyc"
        assert parsed["bucket_low"] == 45
        assert parsed["bucket_high"] == 49

    def test_parse_temp_below(self):
        market = make_market("Below 35°F", category="Highest temperature in NYC on March 12")
        parsed = self.strategy.parse_market(market)
        assert parsed is not None
        assert parsed["bucket_high"] == 35
        assert parsed["is_below"] is True

    def test_parse_temp_above(self):
        market = make_market("55°F or above", category="Highest temperature in NYC on March 12")
        parsed = self.strategy.parse_market(market)
        assert parsed is not None
        assert parsed["bucket_low"] == 55
        assert parsed["is_above"] is True

    def test_parse_precipitation(self):
        market = make_market("Will precipitation in New York City exceed 3 inches?")
        parsed = self.strategy.parse_market(market)
        assert parsed is not None
        assert parsed["location_id"] == "nyc"
        assert parsed["weather_type"] == "precipitation"

    def test_parse_location_from_category(self):
        market = make_market("40°F to 44°F", category="Temperature in London")
        parsed = self.strategy.parse_market(market)
        assert parsed is not None
        assert parsed["location_id"] == "london"

    def test_parse_no_location(self):
        market = make_market("Will it rain in Tokyo?")
        parsed = self.strategy.parse_market(market)
        assert parsed is None

    def test_extract_date_march(self):
        market = make_market("45°F to 49°F on March 12", category="NYC temperature")
        parsed = self.strategy.parse_market(market)
        assert parsed is not None
        target = parsed["target_date"]
        assert target is not None
        assert target.month == 3
        assert target.day == 12


# --- Temperature Bucket Regex ---


class TestBucketRegex:
    def test_range_with_degrees(self):
        match = TEMP_BUCKET_RANGE.search("45°F to 49°F")
        assert match
        assert match.group(1) == "45"
        assert match.group(2) == "49"

    def test_range_without_degrees(self):
        match = TEMP_BUCKET_RANGE.search("40 to 44")
        assert match
        assert match.group(1) == "40"
        assert match.group(2) == "44"

    def test_range_with_dash(self):
        match = TEMP_BUCKET_RANGE.search("50-54°F")
        assert match
        assert match.group(1) == "50"
        assert match.group(2) == "54"

    def test_below_pattern(self):
        match = TEMP_BUCKET_BELOW.search("Below 35°F")
        assert match
        assert match.group(1) == "35"

    def test_above_pattern(self):
        match = TEMP_BUCKET_ABOVE.search("55°F or above")
        assert match
        assert match.group(1) == "55"

    def test_above_higher(self):
        match = TEMP_BUCKET_ABOVE.search("60°F or higher")
        assert match
        assert match.group(1) == "60"


# --- Weather Market Identification ---


class TestWeatherIdentification:
    def test_identify_temperature_market(self):
        markets = [
            make_market("45°F to 49°F", condition_id="1"),
            make_market("Will Trump win?", condition_id="2"),
            make_market("Precipitation in NYC in March", condition_id="3"),
            make_market("Bitcoin Up or Down", condition_id="4"),
        ]
        weather = find_weather_markets(markets)
        assert len(weather) == 2
        assert weather[0].condition_id == "1"
        assert weather[1].condition_id == "3"

    def test_weather_keywords(self):
        assert WEATHER_KEYWORDS.search("temperature in NYC")
        assert WEATHER_KEYWORDS.search("45°F bucket")
        assert WEATHER_KEYWORDS.search("precipitation forecast")
        assert not WEATHER_KEYWORDS.search("Bitcoin Up or Down")


# --- Event Grouping ---


class TestEventGrouping:
    def test_group_by_group_slug(self):
        markets = [
            make_market("45-49°F", condition_id="1", raw={"groupSlug": "nyc-temp-mar12"}),
            make_market("50-54°F", condition_id="2", raw={"groupSlug": "nyc-temp-mar12"}),
            make_market("55-59°F", condition_id="3", raw={"groupSlug": "nyc-temp-mar12"}),
            make_market("40-44°F", condition_id="4", raw={"groupSlug": "london-temp-mar12"}),
        ]
        groups = group_weather_events(markets)
        assert "nyc-temp-mar12" in groups
        assert len(groups["nyc-temp-mar12"]) == 3
        assert "london-temp-mar12" in groups
        assert len(groups["london-temp-mar12"]) == 1

    def test_group_by_category_fallback(self):
        markets = [
            make_market("45-49°F", condition_id="1", category="NYC Temperature March 12"),
            make_market("50-54°F", condition_id="2", category="NYC Temperature March 12"),
        ]
        groups = group_weather_events(markets)
        assert "NYC Temperature March 12" in groups
        assert len(groups["NYC Temperature March 12"]) == 2


# --- Evaluate with Forecast ---


class TestEvaluateWithForecast:
    def setup_method(self):
        self.strategy = WeatherSniperStrategy(make_settings())

    def test_clear_edge_buy_yes(self):
        """Forecast says 70% but market shows 40% → buy YES, 30% edge."""
        # 70 out of 100 values in 45-50 range
        values = [47.0] * 70 + [55.0] * 30
        forecast = make_ensemble(values, target_date=date.today())

        market = make_market("45°F to 49°F", yes_price=0.40, category="NYC temperature")
        parsed = {
            "location_id": "nyc",
            "target_date": date.today(),
            "weather_type": "temperature_max",
            "bucket_low": 45.0,
            "bucket_high": 50.0,
            "is_above": False,
            "is_below": False,
        }

        opp = self.strategy.evaluate_with_forecast(market, forecast, parsed)
        assert opp is not None
        assert opp.side == Side.YES
        assert abs(opp.edge - 0.30) < 0.01
        assert opp.forecast_prob == 0.70

    def test_clear_edge_buy_no(self):
        """Forecast says 20% but market shows 50% → buy NO."""
        values = [47.0] * 20 + [55.0] * 80
        forecast = make_ensemble(values, target_date=date.today())

        market = make_market("45°F to 49°F", yes_price=0.50, category="NYC temperature")
        parsed = {
            "location_id": "nyc",
            "target_date": date.today(),
            "weather_type": "temperature_max",
            "bucket_low": 45.0,
            "bucket_high": 50.0,
            "is_above": False,
            "is_below": False,
        }

        opp = self.strategy.evaluate_with_forecast(market, forecast, parsed)
        assert opp is not None
        assert opp.side == Side.NO
        assert opp.edge > 0.10

    def test_no_edge_when_forecast_agrees(self):
        """Forecast says 50% and market shows 50% → no trade."""
        values = [47.0] * 50 + [55.0] * 50
        forecast = make_ensemble(values, target_date=date.today())

        market = make_market("45°F to 49°F", yes_price=0.50, category="NYC temperature")
        parsed = {
            "location_id": "nyc",
            "target_date": date.today(),
            "weather_type": "temperature_max",
            "bucket_low": 45.0,
            "bucket_high": 50.0,
            "is_above": False,
            "is_below": False,
        }

        opp = self.strategy.evaluate_with_forecast(market, forecast, parsed)
        assert opp is None  # Edge < min_edge (10%)

    def test_extreme_market_filtered(self):
        """Market at $0.01 or $0.99 is filtered out."""
        values = [47.0] * 80 + [55.0] * 20
        forecast = make_ensemble(values, target_date=date.today())

        market = make_market("45°F to 49°F", yes_price=0.01, category="NYC temperature")
        parsed = {
            "location_id": "nyc",
            "target_date": date.today(),
            "weather_type": "temperature_max",
            "bucket_low": 45.0,
            "bucket_high": 50.0,
            "is_above": False,
            "is_below": False,
        }

        opp = self.strategy.evaluate_with_forecast(market, forecast, parsed)
        assert opp is None

    def test_disabled_strategy(self):
        """Strategy returns None when disabled."""
        settings = make_settings()
        settings.strategies.weather_sniper.enabled = False
        strategy = WeatherSniperStrategy(settings)

        values = [47.0] * 80 + [55.0] * 20
        forecast = make_ensemble(values, target_date=date.today())
        market = make_market("45°F to 49°F", yes_price=0.40, category="NYC temperature")
        parsed = {
            "location_id": "nyc",
            "target_date": date.today(),
            "weather_type": "temperature_max",
            "bucket_low": 45.0,
            "bucket_high": 50.0,
            "is_above": False,
            "is_below": False,
        }

        opp = strategy.evaluate_with_forecast(market, forecast, parsed)
        assert opp is None


# --- Neg-Risk Detection ---


class TestNegRiskDetection:
    def setup_method(self):
        self.strategy = WeatherSniperStrategy(make_settings())

    def test_detect_neg_risk_sum_over_one(self):
        """YES prices sum to 1.08 → 8% arb edge."""
        markets = [
            make_market("Below 40°F", yes_price=0.10, condition_id="1"),
            make_market("40-44°F", yes_price=0.20, condition_id="2"),
            make_market("45-49°F", yes_price=0.35, condition_id="3"),
            make_market("50-54°F", yes_price=0.25, condition_id="4"),
            make_market("55°F+", yes_price=0.18, condition_id="5"),
        ]
        # Sum = 1.08

        nr = self.strategy.detect_neg_risk(markets, "nyc", date.today())
        assert nr is not None
        assert abs(nr.yes_price_sum - 1.08) < 0.01
        assert nr.direction == "sell_all"
        assert nr.arb_edge > 0.03

    def test_detect_neg_risk_sum_under_one(self):
        """YES prices sum to 0.92 → 8% arb edge, buy_all."""
        markets = [
            make_market("Below 40°F", yes_price=0.08, condition_id="1"),
            make_market("40-44°F", yes_price=0.18, condition_id="2"),
            make_market("45-49°F", yes_price=0.30, condition_id="3"),
            make_market("50-54°F", yes_price=0.22, condition_id="4"),
            make_market("55°F+", yes_price=0.14, condition_id="5"),
        ]
        # Sum = 0.92

        nr = self.strategy.detect_neg_risk(markets, "nyc", date.today())
        assert nr is not None
        assert nr.direction == "buy_all"

    def test_no_neg_risk_when_fair(self):
        """YES prices sum to 1.0 → no arb."""
        markets = [
            make_market("Below 40°F", yes_price=0.10, condition_id="1"),
            make_market("40-44°F", yes_price=0.20, condition_id="2"),
            make_market("45-49°F", yes_price=0.35, condition_id="3"),
            make_market("50-54°F", yes_price=0.25, condition_id="4"),
            make_market("55°F+", yes_price=0.10, condition_id="5"),
        ]
        # Sum = 1.00

        nr = self.strategy.detect_neg_risk(markets, "nyc", date.today())
        assert nr is None

    def test_too_few_buckets(self):
        """Less than 3 buckets → skip."""
        markets = [
            make_market("Yes", yes_price=0.60, condition_id="1"),
            make_market("No", yes_price=0.60, condition_id="2"),
        ]
        nr = self.strategy.detect_neg_risk(markets, "nyc", date.today())
        assert nr is None


# --- Confidence Scoring ---


class TestConfidenceScoring:
    def setup_method(self):
        self.strategy = WeatherSniperStrategy(make_settings())

    def test_high_confidence_strong_consensus_near_date(self):
        """Strong ensemble consensus + tomorrow → high confidence."""
        import datetime
        tomorrow = date.today() + datetime.timedelta(days=1)
        values = [47.0] * 40 + [55.0] * 10  # 80% consensus
        forecast = make_ensemble(values, target_date=tomorrow)

        parsed = {
            "bucket_low": 45.0,
            "bucket_high": 50.0,
            "target_date": tomorrow,
        }
        conf = self.strategy._compute_confidence(forecast, parsed)
        assert conf > 0.7

    def test_low_confidence_weak_consensus_far_date(self):
        """Weak consensus + >7 days out → lower confidence."""
        import datetime
        far_date = date.today() + datetime.timedelta(days=10)
        values = [47.0] * 25 + [55.0] * 25  # 50% split
        forecast = make_ensemble(values, target_date=far_date)
        forecast.std = 6.0  # High disagreement

        parsed = {
            "bucket_low": 45.0,
            "bucket_high": 50.0,
            "target_date": far_date,
        }
        conf = self.strategy._compute_confidence(forecast, parsed)
        assert conf < 0.6


# --- Config Tests ---


class TestWeatherConfig:
    def test_default_config_values(self):
        config = WeatherSniperConfig()
        assert config.enabled is True
        assert config.min_edge == 0.10
        assert config.min_confidence == 0.60
        assert config.min_neg_risk_edge == 0.03
        assert config.max_position_per_trade == 0.08
        assert "nyc" in config.locations
        assert "london" in config.locations

    def test_config_in_strategies(self):
        settings = Settings()
        assert hasattr(settings.strategies, "weather_sniper")
        assert settings.strategies.weather_sniper.enabled is True


# --- Signal Conversion ---


class TestSignalConversion:
    def setup_method(self):
        self.strategy = WeatherSniperStrategy(make_settings())

    def test_opportunity_to_signal(self):
        opp = WeatherOpportunity(
            market=make_market("45°F to 49°F", yes_price=0.40),
            side=Side.YES,
            location_id="nyc",
            weather_type="temperature_max",
            forecast_prob=0.70,
            market_price=0.40,
            edge=0.30,
            confidence=0.80,
            bucket_low=45.0,
            bucket_high=49.0,
            target_date=date(2026, 3, 12),
            ensemble_mean=47.5,
            ensemble_std=2.3,
            n_ensemble_members=50,
        )

        signal = self.strategy.opportunity_to_signal(opp)
        assert signal.side == Side.YES
        assert signal.edge == 0.30
        assert signal.confidence == 0.80
        assert "New York" in signal.reasoning
        assert "45-49°F" in signal.reasoning
        assert "weather_sniper" in signal.strategy
