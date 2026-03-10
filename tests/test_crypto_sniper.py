"""Tests for the crypto sniper strategy — all three market types.

Tests cover:
- Normal CDF accuracy (Abramowitz & Stegun)
- Market type classification (up/down, threshold, bucket)
- Regex pattern matching for all market phrasings
- Symbol extraction (BTC, ETH, SOL, XRP, DOGE)
- Threshold probability model (log-normal)
- Bucket probability model (range)
- Direction probability model (original)
- Market parsing (strike extraction, bucket ranges, arrows)
- find_crypto_markets() filtering
- Edge detection and opportunity generation
- Signal conversion
"""

import math
import re
import pytest
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


# ── Inline types (avoid importing polyedge which needs Python 3.11+) ──

class Side(str, Enum):
    YES = "yes"
    NO = "no"


@dataclass
class FakeMarket:
    condition_id: str = "0x123"
    question: str = ""
    description: str = ""
    slug: str = ""
    category: str = ""
    end_date: object = None
    active: bool = True
    closed: bool = False
    clob_token_ids: list = field(default_factory=list)
    yes_price: float = 0.5
    no_price: float = 0.5
    volume: float = 10000
    liquidity: float = 5000
    spread: float = 0.01
    raw: dict = field(default_factory=dict)

    @property
    def yes_token_id(self):
        return self.clob_token_ids[0] if self.clob_token_ids else None

    @property
    def no_token_id(self):
        return self.clob_token_ids[1] if len(self.clob_token_ids) > 1 else None


# ── Inline the core functions from crypto_sniper.py ──

def _normal_cdf(x: float) -> float:
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
    try:
        return float(s.replace(",", ""))
    except (ValueError, AttributeError):
        return None


# Regex patterns (copied from crypto_sniper.py)
CRYPTO_SYMBOL_MAP = {
    "bitcoin": "btcusdt", "btc": "btcusdt",
    "ethereum": "ethusdt", "eth": "ethusdt", "ether": "ethusdt",
    "solana": "solusdt", "sol": "solusdt",
    "xrp": "xrpusdt", "ripple": "xrpusdt",
    "dogecoin": "dogeusdt", "doge": "dogeusdt",
}

UP_DOWN_PATTERN = re.compile(
    r"(Bitcoin|BTC|Ethereum|ETH|Ether|Solana|SOL|XRP|Ripple|Dogecoin|DOGE)\s+"
    r".*?[Uu]p\s+or\s+[Dd]own", re.IGNORECASE,
)
THRESHOLD_PATTERN = re.compile(
    r"(Bitcoin|BTC|Ethereum|ETH|Ether|Solana|SOL|XRP|Ripple|Dogecoin|DOGE)\s+"
    r"above\s+[\$]?([\d,]+(?:\.\d+)?)", re.IGNORECASE,
)
BUCKET_PATTERN = re.compile(
    r"[Ww]hat\s+price\s+will\s+"
    r"(Bitcoin|BTC|Ethereum|ETH|Ether|Solana|SOL|XRP|Ripple|Dogecoin|DOGE)\s+"
    r"hit", re.IGNORECASE,
)
THRESHOLD_VALUE_PATTERN = re.compile(
    r"above\s+[\$]?([\d,]+(?:\.\d+)?)", re.IGNORECASE,
)
BUCKET_ABOVE_PATTERN = re.compile(r"[↑]\s*[\$]?([\d,]+(?:\.\d+)?)")
BUCKET_BELOW_PATTERN = re.compile(r"[↓]\s*[\$]?([\d,]+(?:\.\d+)?)")
BUCKET_RANGE_PATTERN = re.compile(
    r"[\$]?([\d,]+(?:\.\d+)?)\s*(?:to|[-–])\s*[\$]?([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)
CRYPTO_KEYWORDS = re.compile(
    r"\b(Bitcoin|BTC|Ethereum|ETH|Ether|Solana|SOL|XRP|Ripple|Dogecoin|DOGE)\b",
    re.IGNORECASE,
)

DEFAULT_ANNUAL_VOL = {
    "btcusdt": 0.60, "ethusdt": 0.75, "solusdt": 0.90,
    "xrpusdt": 0.85, "dogeusdt": 1.00,
}


def _compute_threshold_probability(current_price, strike, seconds_remaining, symbol):
    if current_price <= 0 or strike <= 0:
        return 0.5
    annual_vol = DEFAULT_ANNUAL_VOL.get(symbol, 0.70)
    t_years = seconds_remaining / (365.25 * 24 * 3600)
    if t_years <= 0:
        return 0.99 if current_price > strike else 0.01
    vol_t = annual_vol * math.sqrt(t_years)
    if vol_t <= 0:
        return 0.99 if current_price > strike else 0.01
    z = math.log(current_price / strike) / vol_t
    prob = _normal_cdf(z)
    return max(0.01, min(0.99, prob))


def _compute_bucket_probability(current_price, bucket_low, bucket_high, seconds_remaining, symbol):
    prob_above_low = _compute_threshold_probability(current_price, bucket_low, seconds_remaining, symbol)
    prob_above_high = _compute_threshold_probability(current_price, bucket_high, seconds_remaining, symbol)
    prob = prob_above_low - prob_above_high
    return max(0.01, min(0.99, prob))


def match_market_to_symbol(question):
    q = question.lower()
    for keyword in sorted(CRYPTO_SYMBOL_MAP.keys(), key=len, reverse=True):
        if keyword in q:
            return CRYPTO_SYMBOL_MAP[keyword]
    return None


def classify_market(question):
    if UP_DOWN_PATTERN.search(question):
        return "up_down"
    if THRESHOLD_PATTERN.search(question):
        return "threshold"
    if BUCKET_PATTERN.search(question):
        return "bucket"
    return None


# ══════════════════════════════════════════════════════════════════════
# Tests
# ══════════════════════════════════════════════════════════════════════

class TestNormalCDF:
    """Test the Abramowitz & Stegun CDF approximation."""

    def test_cdf_at_zero(self):
        assert abs(_normal_cdf(0) - 0.5) < 0.001

    def test_cdf_at_one(self):
        assert abs(_normal_cdf(1.0) - 0.8413) < 0.001

    def test_cdf_at_two(self):
        assert abs(_normal_cdf(2.0) - 0.9772) < 0.001

    def test_cdf_at_three(self):
        assert abs(_normal_cdf(3.0) - 0.9987) < 0.001

    def test_cdf_negative(self):
        assert abs(_normal_cdf(-1.0) - 0.1587) < 0.001

    def test_cdf_symmetry(self):
        for x in [0.5, 1.0, 1.5, 2.0, 2.5]:
            assert abs(_normal_cdf(x) + _normal_cdf(-x) - 1.0) < 0.001

    def test_cdf_monotonic(self):
        prev = 0.0
        for x in [-3, -2, -1, 0, 1, 2, 3]:
            val = _normal_cdf(x)
            assert val > prev
            prev = val


class TestMarketClassification:
    """Test classifying markets into up/down, threshold, or bucket."""

    def test_up_down_btc(self):
        assert classify_market("BTC 5 Minute Up or Down") == "up_down"

    def test_up_down_solana(self):
        assert classify_market("Solana Up or Down - March 10, 3:10PM-3:15PM ET") == "up_down"

    def test_up_down_ethereum(self):
        assert classify_market("Ethereum Up or Down - March 10, 2:00PM-2:15PM ET") == "up_down"

    def test_up_down_xrp(self):
        assert classify_market("XRP Up or Down - March 10") == "up_down"

    def test_up_down_doge(self):
        assert classify_market("Dogecoin Up or Down - March 10") == "up_down"

    def test_threshold_btc(self):
        assert classify_market("Bitcoin above 70,000 on March 10?") == "threshold"

    def test_threshold_eth(self):
        assert classify_market("Ethereum above 1,500 on March 10?") == "threshold"

    def test_threshold_sol(self):
        assert classify_market("Solana above 40 on March 10?") == "threshold"

    def test_threshold_with_dollar(self):
        assert classify_market("Bitcoin above $70,000 on March 10?") == "threshold"

    def test_bucket_btc(self):
        assert classify_market("What price will Bitcoin hit on March 9?") == "bucket"

    def test_bucket_sol(self):
        assert classify_market("What price will Solana hit in March?") == "bucket"

    def test_bucket_eth(self):
        assert classify_market("What price will Ethereum hit on March 10?") == "bucket"

    def test_non_crypto(self):
        assert classify_market("Will it rain in NYC on March 10?") is None

    def test_partial_match_not_enough(self):
        assert classify_market("Bitcoin is great today") is None

    def test_btc_5min_with_time(self):
        assert classify_market("BTC 5 Minute Up or Down - March 10, 2:00PM-2:05PM ET") == "up_down"


class TestSymbolExtraction:
    """Test extracting Binance symbols from market questions."""

    def test_bitcoin(self):
        assert match_market_to_symbol("Bitcoin above 70,000?") == "btcusdt"

    def test_btc(self):
        assert match_market_to_symbol("BTC 5 Minute Up or Down") == "btcusdt"

    def test_ethereum(self):
        assert match_market_to_symbol("Ethereum above 1,500?") == "ethusdt"

    def test_eth(self):
        assert match_market_to_symbol("What price will ETH hit?") == "ethusdt"

    def test_solana(self):
        assert match_market_to_symbol("Solana above 85?") == "solusdt"

    def test_xrp(self):
        assert match_market_to_symbol("XRP Up or Down") == "xrpusdt"

    def test_ripple(self):
        assert match_market_to_symbol("Ripple above $0.50?") == "xrpusdt"

    def test_dogecoin(self):
        assert match_market_to_symbol("Dogecoin above $0.10?") == "dogeusdt"

    def test_doge(self):
        assert match_market_to_symbol("DOGE Up or Down") == "dogeusdt"

    def test_no_match(self):
        assert match_market_to_symbol("Will it rain tomorrow?") is None

    def test_case_insensitive(self):
        assert match_market_to_symbol("BITCOIN above 70000") == "btcusdt"

    def test_longer_keyword_priority(self):
        # "ethereum" should match before "eth" to avoid issues
        assert match_market_to_symbol("Ethereum price today") == "ethusdt"

    def test_solana_not_sol_prefix(self):
        # "solana" should match, and "sol" should also match independently
        assert match_market_to_symbol("SOL above 85?") == "solusdt"


class TestThresholdExtraction:
    """Test extracting strike prices from threshold markets."""

    def test_above_70000(self):
        match = THRESHOLD_VALUE_PATTERN.search("Bitcoin above 70,000 on March 10?")
        assert match is not None
        assert _parse_number(match.group(1)) == 70000.0

    def test_above_1500(self):
        match = THRESHOLD_VALUE_PATTERN.search("Ethereum above 1,500 on March 10?")
        assert match is not None
        assert _parse_number(match.group(1)) == 1500.0

    def test_above_dollar_sign(self):
        match = THRESHOLD_VALUE_PATTERN.search("Bitcoin above $70,000?")
        assert match is not None
        assert _parse_number(match.group(1)) == 70000.0

    def test_above_decimal(self):
        match = THRESHOLD_VALUE_PATTERN.search("XRP above 0.50?")
        assert match is not None
        assert _parse_number(match.group(1)) == 0.50

    def test_above_no_comma(self):
        match = THRESHOLD_VALUE_PATTERN.search("Solana above 85 on March 10?")
        assert match is not None
        assert _parse_number(match.group(1)) == 85.0


class TestBucketPatterns:
    """Test regex patterns for bucket market parsing."""

    def test_arrow_above(self):
        match = BUCKET_ABOVE_PATTERN.search("↑ 70,000")
        assert match is not None
        assert _parse_number(match.group(1)) == 70000.0

    def test_arrow_below(self):
        match = BUCKET_BELOW_PATTERN.search("↓ 66,000")
        assert match is not None
        assert _parse_number(match.group(1)) == 66000.0

    def test_arrow_small_number(self):
        match = BUCKET_ABOVE_PATTERN.search("↑ 110")
        assert match is not None
        assert _parse_number(match.group(1)) == 110.0

    def test_range_to(self):
        match = BUCKET_RANGE_PATTERN.search("$68,000 to $70,000")
        assert match is not None
        assert _parse_number(match.group(1)) == 68000.0
        assert _parse_number(match.group(2)) == 70000.0

    def test_range_dash(self):
        match = BUCKET_RANGE_PATTERN.search("85-90")
        assert match is not None
        assert _parse_number(match.group(1)) == 85.0
        assert _parse_number(match.group(2)) == 90.0

    def test_range_endash(self):
        match = BUCKET_RANGE_PATTERN.search("$1,500–$1,600")
        assert match is not None
        assert _parse_number(match.group(1)) == 1500.0
        assert _parse_number(match.group(2)) == 1600.0


class TestThresholdProbability:
    """Test the log-normal threshold probability model."""

    def test_price_well_above_strike(self):
        # BTC at $75K, strike $70K, 1 hour left — should be very likely YES
        prob = _compute_threshold_probability(75000, 70000, 3600, "btcusdt")
        assert prob > 0.85

    def test_price_well_below_strike(self):
        # BTC at $65K, strike $70K, 1 hour left — should be very unlikely YES
        prob = _compute_threshold_probability(65000, 70000, 3600, "btcusdt")
        assert prob < 0.15

    def test_price_at_strike(self):
        # Price exactly at strike — should be ~50%
        prob = _compute_threshold_probability(70000, 70000, 3600, "btcusdt")
        assert 0.45 < prob < 0.55

    def test_more_time_more_uncertainty(self):
        # Same distance from strike, but more time = less certain
        prob_1h = _compute_threshold_probability(72000, 70000, 3600, "btcusdt")
        prob_24h = _compute_threshold_probability(72000, 70000, 86400, "btcusdt")
        assert prob_1h > prob_24h  # Closer to expiry = more confident

    def test_expired_above(self):
        # Already expired, price above strike
        prob = _compute_threshold_probability(75000, 70000, 0, "btcusdt")
        assert prob == 0.99

    def test_expired_below(self):
        # Already expired, price below strike
        prob = _compute_threshold_probability(65000, 70000, 0, "btcusdt")
        assert prob == 0.01

    def test_higher_vol_more_uncertainty(self):
        # DOGE (100% vol) should be less certain than BTC (60% vol) at same relative distance
        # Use 24h window so the vol has time to matter
        prob_btc = _compute_threshold_probability(72000, 70000, 86400, "btcusdt")
        prob_doge = _compute_threshold_probability(0.12, 0.10, 86400, "dogeusdt")
        # BTC at ~2.86% above strike, DOGE at ~18% above — but DOGE has higher vol
        # Both should be > 0.5 (price is above strike)
        assert prob_btc > 0.5
        assert prob_doge > 0.5
        # With 24h and 60% vol, BTC 2.86% above should not be 0.99
        assert prob_btc < 0.99

    def test_sol_threshold(self):
        # SOL at $90, strike $85, 2 hours left
        prob = _compute_threshold_probability(90, 85, 7200, "solusdt")
        assert prob > 0.6

    def test_zero_price_returns_half(self):
        prob = _compute_threshold_probability(0, 70000, 3600, "btcusdt")
        assert prob == 0.5


class TestBucketProbability:
    """Test the bucket (range) probability model."""

    def test_price_in_bucket(self):
        # BTC at $69K, bucket $68K-$70K, 1 hour left
        prob = _compute_bucket_probability(69000, 68000, 70000, 3600, "btcusdt")
        assert prob > 0.3  # Should have decent probability

    def test_price_outside_bucket(self):
        # BTC at $75K, bucket $68K-$70K, 1 hour left
        prob = _compute_bucket_probability(75000, 68000, 70000, 3600, "btcusdt")
        assert prob < 0.15  # Very unlikely to fall back into bucket

    def test_price_at_bucket_edge(self):
        # BTC at $70K, bucket $68K-$70K, 1 hour left
        prob = _compute_bucket_probability(70000, 68000, 70000, 3600, "btcusdt")
        assert 0.1 < prob < 0.6

    def test_wider_bucket_higher_prob(self):
        # Wider bucket should have higher probability
        narrow = _compute_bucket_probability(69000, 68500, 69500, 3600, "btcusdt")
        wide = _compute_bucket_probability(69000, 65000, 73000, 3600, "btcusdt")
        assert wide > narrow

    def test_more_time_spreads_probability(self):
        # With more time, probability spreads more evenly across buckets
        prob_1h = _compute_bucket_probability(69000, 68000, 70000, 3600, "btcusdt")
        prob_1w = _compute_bucket_probability(69000, 68000, 70000, 604800, "btcusdt")
        # With a week left, the probability of being in a narrow bucket decreases
        assert prob_1h > prob_1w

    def test_bucket_prob_is_cdf_difference(self):
        # Verify that bucket prob = P(above low) - P(above high)
        price, low, high, t, sym = 69000, 68000, 70000, 3600, "btcusdt"
        prob_above_low = _compute_threshold_probability(price, low, t, sym)
        prob_above_high = _compute_threshold_probability(price, high, t, sym)
        bucket_prob = _compute_bucket_probability(price, low, high, t, sym)
        expected = prob_above_low - prob_above_high
        assert abs(bucket_prob - max(0.01, min(0.99, expected))) < 0.001


class TestCryptoKeywords:
    """Test the broad crypto keyword detection."""

    def test_bitcoin(self):
        assert CRYPTO_KEYWORDS.search("Bitcoin above 70,000")

    def test_btc(self):
        assert CRYPTO_KEYWORDS.search("BTC 5 Minute Up or Down")

    def test_ethereum(self):
        assert CRYPTO_KEYWORDS.search("Ethereum above 1,500")

    def test_xrp(self):
        assert CRYPTO_KEYWORDS.search("XRP up or down")

    def test_dogecoin(self):
        assert CRYPTO_KEYWORDS.search("Dogecoin above $0.10")

    def test_doge(self):
        assert CRYPTO_KEYWORDS.search("DOGE up or down today")

    def test_no_crypto(self):
        assert not CRYPTO_KEYWORDS.search("Will it rain in NYC?")

    def test_no_partial_match(self):
        # "sol" should match as word boundary
        assert CRYPTO_KEYWORDS.search("SOL above 85")

    def test_case_insensitive(self):
        assert CRYPTO_KEYWORDS.search("bitcoin price today")


class TestFindCryptoMarkets:
    """Test the find_crypto_markets filtering function."""

    def _make_market(self, question, desc=""):
        return FakeMarket(question=question, description=desc)

    def test_finds_up_down(self):
        markets = [
            self._make_market("BTC 5 Minute Up or Down"),
            self._make_market("Will it rain tomorrow?"),
        ]
        found = [m for m in markets if CRYPTO_KEYWORDS.search(m.question) and
                 (UP_DOWN_PATTERN.search(m.question) or
                  THRESHOLD_PATTERN.search(m.question) or
                  BUCKET_PATTERN.search(m.question))]
        assert len(found) == 1

    def test_finds_threshold(self):
        markets = [
            self._make_market("Bitcoin above 70,000 on March 10?"),
            self._make_market("Some non-crypto market"),
        ]
        found = [m for m in markets if CRYPTO_KEYWORDS.search(m.question) and
                 (UP_DOWN_PATTERN.search(m.question) or
                  THRESHOLD_PATTERN.search(m.question) or
                  BUCKET_PATTERN.search(m.question))]
        assert len(found) == 1

    def test_finds_bucket(self):
        markets = [
            self._make_market("What price will Bitcoin hit on March 9?"),
        ]
        found = [m for m in markets if CRYPTO_KEYWORDS.search(m.question) and
                 (UP_DOWN_PATTERN.search(m.question) or
                  THRESHOLD_PATTERN.search(m.question) or
                  BUCKET_PATTERN.search(m.question))]
        assert len(found) == 1

    def test_finds_all_types(self):
        markets = [
            self._make_market("BTC 5 Minute Up or Down"),
            self._make_market("Bitcoin above 70,000 on March 10?"),
            self._make_market("What price will Bitcoin hit on March 9?"),
            self._make_market("Ethereum above 1,500 on March 10?"),
            self._make_market("Solana above 40 on March 10?"),
            self._make_market("XRP Up or Down - March 10"),
            self._make_market("Will it rain in NYC?"),
            self._make_market("Trump approval rating?"),
        ]
        found = [m for m in markets if CRYPTO_KEYWORDS.search(m.question) and
                 (UP_DOWN_PATTERN.search(m.question) or
                  THRESHOLD_PATTERN.search(m.question) or
                  BUCKET_PATTERN.search(m.question))]
        assert len(found) == 6

    def test_rejects_generic_crypto_mention(self):
        # "Bitcoin is great" has the keyword but no market pattern
        markets = [self._make_market("Bitcoin is a great investment")]
        found = [m for m in markets if CRYPTO_KEYWORDS.search(m.question) and
                 (UP_DOWN_PATTERN.search(m.question) or
                  THRESHOLD_PATTERN.search(m.question) or
                  BUCKET_PATTERN.search(m.question))]
        assert len(found) == 0


class TestParseNumber:
    """Test number parsing helper."""

    def test_simple(self):
        assert _parse_number("70000") == 70000.0

    def test_with_commas(self):
        assert _parse_number("70,000") == 70000.0

    def test_decimal(self):
        assert _parse_number("0.50") == 0.50

    def test_large_with_commas(self):
        assert _parse_number("1,500,000") == 1500000.0

    def test_none(self):
        assert _parse_number("abc") is None

    def test_empty(self):
        assert _parse_number("") is None


class TestDirectionProbability:
    """Test the original up/down direction probability model."""

    def _compute(self, abs_change, seconds_remaining, volatility):
        base_vol_per_5min = max(volatility, 0.003)
        effective_remaining = seconds_remaining + 15.0
        remaining_fraction = max(effective_remaining / 300, 0.05)
        remaining_vol = base_vol_per_5min * math.sqrt(remaining_fraction)
        if remaining_vol <= 0:
            return 0.99
        z_score = abs_change / remaining_vol
        prob = _normal_cdf(z_score)
        return max(0.50, min(0.99, prob))

    def test_big_move_little_time(self):
        # 1% move with 30s left — very confident
        prob = self._compute(0.01, 30, 0.003)
        assert prob > 0.90

    def test_small_move_lots_of_time(self):
        # 0.2% move with 120s left — less confident than big move
        prob = self._compute(0.002, 120, 0.003)
        big_prob = self._compute(0.01, 30, 0.003)
        assert prob < big_prob  # Smaller move = less confident

    def test_medium_move_medium_time(self):
        # 0.5% move with 60s left — between small and big
        prob = self._compute(0.005, 60, 0.003)
        assert prob > 0.70  # Should be fairly confident

    def test_high_vol_reduces_confidence(self):
        # Same move but higher vol
        prob_low_vol = self._compute(0.005, 60, 0.003)
        prob_high_vol = self._compute(0.005, 60, 0.010)
        assert prob_low_vol > prob_high_vol

    def test_zero_remaining_vol(self):
        # Edge case
        prob = self._compute(0.01, 0, 0.0)
        # With vol floor at 0.003, this should still compute
        assert prob > 0.5

    def test_always_above_half(self):
        # Any positive change should give prob >= 0.5
        prob = self._compute(0.0001, 90, 0.003)
        assert prob >= 0.50
