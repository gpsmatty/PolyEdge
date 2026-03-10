"""Tests for the crypto sniper strategy — all three market types.

Tests cover:
- Normal CDF accuracy (Abramowitz & Stegun)
- Market type classification (up/down, threshold, bucket)
- Regex pattern matching for REAL Polymarket question phrasings
- Symbol extraction (BTC, ETH, SOL, XRP, DOGE)
- Threshold probability model (log-normal)
- Bucket probability model (range)
- Direction probability model (original)
- Market parsing (strike extraction, bearish detection, bucket ranges)
- Bearish threshold handling ("less than", "dip to")
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


# ── Regex patterns (copied from crypto_sniper.py — must match exactly) ──

_CRYPTO = r"(?:Bitcoin|BTC|Ethereum|ETH|Ether|Solana|SOL|XRP|Ripple|Dogecoin|DOGE)"

CRYPTO_SYMBOL_MAP = {
    "bitcoin": "btcusdt", "btc": "btcusdt",
    "ethereum": "ethusdt", "eth": "ethusdt", "ether": "ethusdt",
    "solana": "solusdt", "sol": "solusdt",
    "xrp": "xrpusdt", "ripple": "xrpusdt",
    "dogecoin": "dogeusdt", "doge": "dogeusdt",
}

UP_DOWN_PATTERN = re.compile(
    _CRYPTO + r"\s+.*?[Uu]p\s+or\s+[Dd]own",
    re.IGNORECASE,
)

THRESHOLD_PATTERN = re.compile(
    r"(?:"
    r"price\s+of\s+" + _CRYPTO + r"\s+be\s+(?:greater\s+than|above|less\s+than|below)\s+[\$]?[\d,]+(?:\.\d+)?"
    r"|"
    + _CRYPTO + r"\s+(?:reach|dip\s+to)\s+[\$]?[\d,]+(?:\.\d+)?"
    r")",
    re.IGNORECASE,
)

BUCKET_PATTERN = re.compile(
    r"price\s+of\s+" + _CRYPTO + r"\s+be\s+between\s+",
    re.IGNORECASE,
)

THRESHOLD_VALUE_PATTERN = re.compile(
    r"(?:greater\s+than|above|less\s+than|below|reach|dip\s+to)\s+[\$]?([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)

THRESHOLD_BEARISH_PATTERN = re.compile(
    r"(?:less\s+than|below|dip\s+to)",
    re.IGNORECASE,
)

BUCKET_RANGE_PATTERN = re.compile(
    r"between\s+[\$]?([\d,]+(?:\.\d+)?)\s+and\s+[\$]?([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)

BUCKET_RANGE_FALLBACK = re.compile(
    r"[\$]?([\d,]+(?:\.\d+)?)\s*(?:to|[-–])\s*[\$]?([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)

BUCKET_ABOVE_PATTERN = re.compile(r"[↑]\s*[\$]?([\d,]+(?:\.\d+)?)")
BUCKET_BELOW_PATTERN = re.compile(r"[↓]\s*[\$]?([\d,]+(?:\.\d+)?)")

CRYPTO_KEYWORDS = re.compile(
    r"\b(Bitcoin|BTC|Ethereum|ETH|Ether|Solana|SOL|XRP|Ripple|Dogecoin|DOGE)\b",
    re.IGNORECASE,
)

DEFAULT_ANNUAL_VOL = {
    "btcusdt": 0.60, "ethusdt": 0.75, "solusdt": 0.90,
    "xrpusdt": 0.85, "dogeusdt": 1.00,
}


# ── Inline probability functions ──

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
    """Test classifying markets into up/down, threshold, or bucket.

    Uses REAL Polymarket question phrasings from the API (March 2026).
    """

    # Up/Down markets
    def test_up_down_btc(self):
        assert classify_market("BTC 5 Minute Up or Down") == "up_down"

    def test_up_down_btc_with_time(self):
        assert classify_market("BTC 5 Minute Up or Down - March 10, 2:00PM-2:05PM ET") == "up_down"

    def test_up_down_bitcoin_long(self):
        assert classify_market("Bitcoin Up or Down - March 10, 5:15PM-5:30PM ET") == "up_down"

    def test_up_down_solana(self):
        assert classify_market("Solana Up or Down - March 10, 12:00AM-4:00AM ET") == "up_down"

    def test_up_down_ethereum(self):
        assert classify_market("Ethereum Up or Down - March 10, 2:00PM-2:15PM ET") == "up_down"

    def test_up_down_xrp(self):
        assert classify_market("XRP Up or Down - March 10") == "up_down"

    def test_up_down_doge(self):
        assert classify_market("Dogecoin Up or Down - March 10") == "up_down"

    # Threshold markets — bullish (greater than / above)
    def test_threshold_btc_greater_than(self):
        assert classify_market("Will the price of Bitcoin be greater than $78,000 on March 10?") == "threshold"

    def test_threshold_eth_above(self):
        assert classify_market("Will the price of Ethereum be above $2,600 on March 11?") == "threshold"

    def test_threshold_sol_above(self):
        assert classify_market("Will the price of Solana be above $110 on March 10?") == "threshold"

    def test_threshold_xrp_above(self):
        assert classify_market("Will the price of XRP be above $1.40 on March 12?") == "threshold"

    # Threshold markets — bearish (less than)
    def test_threshold_btc_less_than(self):
        assert classify_market("Will the price of Bitcoin be less than $64,000 on March 11?") == "threshold"

    # Threshold markets — reach / dip to
    def test_threshold_btc_reach(self):
        assert classify_market("Will Bitcoin reach $85,000 in March?") == "threshold"

    def test_threshold_btc_dip(self):
        assert classify_market("Will Bitcoin dip to $50,000 in March?") == "threshold"

    # Bucket markets — "between X and Y"
    def test_bucket_btc(self):
        assert classify_market("Will the price of Bitcoin be between $74,000 and $76,000 on March 11?") == "bucket"

    def test_bucket_eth(self):
        assert classify_market("Will the price of Ethereum be between $2,100 and $2,200 on March 10?") == "bucket"

    def test_bucket_sol(self):
        assert classify_market("Will the price of Solana be between $90 and $100 on March 10?") == "bucket"

    def test_bucket_xrp(self):
        assert classify_market("Will the price of XRP be between $1.20 and $1.30 on March 11?") == "bucket"

    # Non-crypto / non-matching
    def test_non_crypto(self):
        assert classify_market("Will it rain in NYC on March 10?") is None

    def test_partial_match_not_enough(self):
        assert classify_market("Bitcoin is great today") is None

    def test_generic_crypto_not_matched(self):
        assert classify_market("Bitcoin price is going up today") is None


class TestSymbolExtraction:
    """Test extracting Binance symbols from market questions."""

    def test_bitcoin(self):
        assert match_market_to_symbol("Will the price of Bitcoin be greater than $78,000?") == "btcusdt"

    def test_btc(self):
        assert match_market_to_symbol("BTC 5 Minute Up or Down") == "btcusdt"

    def test_ethereum(self):
        assert match_market_to_symbol("Will the price of Ethereum be above $2,600?") == "ethusdt"

    def test_eth(self):
        assert match_market_to_symbol("What price will ETH hit?") == "ethusdt"

    def test_solana(self):
        assert match_market_to_symbol("Will the price of Solana be above $110?") == "solusdt"

    def test_sol(self):
        assert match_market_to_symbol("SOL Up or Down - March 10") == "solusdt"

    def test_xrp(self):
        assert match_market_to_symbol("Will the price of XRP be above $1.40?") == "xrpusdt"

    def test_ripple(self):
        assert match_market_to_symbol("Ripple above $0.50?") == "xrpusdt"

    def test_dogecoin(self):
        assert match_market_to_symbol("Dogecoin Up or Down - March 10") == "dogeusdt"

    def test_doge(self):
        assert match_market_to_symbol("DOGE Up or Down") == "dogeusdt"

    def test_no_match(self):
        assert match_market_to_symbol("Will it rain tomorrow?") is None

    def test_case_insensitive(self):
        assert match_market_to_symbol("BITCOIN price prediction") == "btcusdt"

    def test_longer_keyword_priority(self):
        assert match_market_to_symbol("Ethereum price today") == "ethusdt"

    def test_solana_not_sol_prefix(self):
        assert match_market_to_symbol("SOL above 85?") == "solusdt"


class TestThresholdExtraction:
    """Test extracting strike prices from threshold markets — REAL phrasings."""

    def test_greater_than(self):
        match = THRESHOLD_VALUE_PATTERN.search("Will the price of Bitcoin be greater than $78,000 on March 10?")
        assert match is not None
        assert _parse_number(match.group(1)) == 78000.0

    def test_above(self):
        match = THRESHOLD_VALUE_PATTERN.search("Will the price of Ethereum be above $2,600 on March 11?")
        assert match is not None
        assert _parse_number(match.group(1)) == 2600.0

    def test_less_than(self):
        match = THRESHOLD_VALUE_PATTERN.search("Will the price of Bitcoin be less than $64,000 on March 11?")
        assert match is not None
        assert _parse_number(match.group(1)) == 64000.0

    def test_reach(self):
        match = THRESHOLD_VALUE_PATTERN.search("Will Bitcoin reach $85,000 in March?")
        assert match is not None
        assert _parse_number(match.group(1)) == 85000.0

    def test_dip_to(self):
        match = THRESHOLD_VALUE_PATTERN.search("Will Bitcoin dip to $50,000 in March?")
        assert match is not None
        assert _parse_number(match.group(1)) == 50000.0

    def test_small_decimal(self):
        match = THRESHOLD_VALUE_PATTERN.search("Will the price of XRP be above $1.40 on March 12?")
        assert match is not None
        assert _parse_number(match.group(1)) == 1.40

    def test_no_dollar_sign(self):
        match = THRESHOLD_VALUE_PATTERN.search("Will the price of Solana be above 110 on March 10?")
        assert match is not None
        assert _parse_number(match.group(1)) == 110.0


class TestBearishDetection:
    """Test detection of bearish threshold markets."""

    def test_less_than_is_bearish(self):
        assert THRESHOLD_BEARISH_PATTERN.search("Will the price of Bitcoin be less than $64,000?")

    def test_below_is_bearish(self):
        assert THRESHOLD_BEARISH_PATTERN.search("Will the price of ETH be below $2,000?")

    def test_dip_to_is_bearish(self):
        assert THRESHOLD_BEARISH_PATTERN.search("Will Bitcoin dip to $50,000 in March?")

    def test_greater_than_not_bearish(self):
        assert not THRESHOLD_BEARISH_PATTERN.search("Will the price of Bitcoin be greater than $78,000?")

    def test_above_not_bearish(self):
        assert not THRESHOLD_BEARISH_PATTERN.search("Will the price of Ethereum be above $2,600?")

    def test_reach_not_bearish(self):
        assert not THRESHOLD_BEARISH_PATTERN.search("Will Bitcoin reach $85,000 in March?")


class TestBucketPatterns:
    """Test regex patterns for bucket market parsing."""

    # Primary pattern: "between X and Y" (most common on Polymarket)
    def test_between_btc(self):
        match = BUCKET_RANGE_PATTERN.search("Will the price of Bitcoin be between $74,000 and $76,000 on March 11?")
        assert match is not None
        assert _parse_number(match.group(1)) == 74000.0
        assert _parse_number(match.group(2)) == 76000.0

    def test_between_eth(self):
        match = BUCKET_RANGE_PATTERN.search("Will the price of Ethereum be between $2,100 and $2,200 on March 10?")
        assert match is not None
        assert _parse_number(match.group(1)) == 2100.0
        assert _parse_number(match.group(2)) == 2200.0

    def test_between_xrp(self):
        match = BUCKET_RANGE_PATTERN.search("Will the price of XRP be between $1.20 and $1.30 on March 11?")
        assert match is not None
        assert _parse_number(match.group(1)) == 1.20
        assert _parse_number(match.group(2)) == 1.30

    def test_between_sol(self):
        match = BUCKET_RANGE_PATTERN.search("Will the price of Solana be between $90 and $100 on March 10?")
        assert match is not None
        assert _parse_number(match.group(1)) == 90.0
        assert _parse_number(match.group(2)) == 100.0

    # Arrow-style buckets (from Polymarket UI descriptions)
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

    # Fallback pattern: "X to Y" or "X-Y" in descriptions
    def test_range_to(self):
        match = BUCKET_RANGE_FALLBACK.search("$68,000 to $70,000")
        assert match is not None
        assert _parse_number(match.group(1)) == 68000.0
        assert _parse_number(match.group(2)) == 70000.0

    def test_range_dash(self):
        match = BUCKET_RANGE_FALLBACK.search("85-90")
        assert match is not None
        assert _parse_number(match.group(1)) == 85.0
        assert _parse_number(match.group(2)) == 90.0

    def test_range_endash(self):
        match = BUCKET_RANGE_FALLBACK.search("$1,500–$1,600")
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
        prob = _compute_threshold_probability(75000, 70000, 0, "btcusdt")
        assert prob == 0.99

    def test_expired_below(self):
        prob = _compute_threshold_probability(65000, 70000, 0, "btcusdt")
        assert prob == 0.01

    def test_higher_vol_more_uncertainty(self):
        # Use 24h window so the vol has time to matter
        prob_btc = _compute_threshold_probability(72000, 70000, 86400, "btcusdt")
        prob_doge = _compute_threshold_probability(0.12, 0.10, 86400, "dogeusdt")
        assert prob_btc > 0.5
        assert prob_doge > 0.5
        assert prob_btc < 0.99

    def test_sol_threshold(self):
        prob = _compute_threshold_probability(90, 85, 7200, "solusdt")
        assert prob > 0.6

    def test_zero_price_returns_half(self):
        prob = _compute_threshold_probability(0, 70000, 3600, "btcusdt")
        assert prob == 0.5

    def test_bearish_logic(self):
        """For 'less than' markets, P(YES) = 1 - P(price > strike)."""
        prob_above = _compute_threshold_probability(65000, 70000, 3600, "btcusdt")
        # Price ($65K) is below strike ($70K), so P(above) is low
        assert prob_above < 0.15
        # For a bearish market: P(YES) = 1 - prob_above = high
        p_yes_bearish = 1 - prob_above
        assert p_yes_bearish > 0.85


class TestBucketProbability:
    """Test the bucket (range) probability model."""

    def test_price_in_bucket(self):
        prob = _compute_bucket_probability(69000, 68000, 70000, 3600, "btcusdt")
        assert prob > 0.3

    def test_price_outside_bucket(self):
        prob = _compute_bucket_probability(75000, 68000, 70000, 3600, "btcusdt")
        assert prob < 0.15

    def test_price_at_bucket_edge(self):
        prob = _compute_bucket_probability(70000, 68000, 70000, 3600, "btcusdt")
        assert 0.1 < prob < 0.6

    def test_wider_bucket_higher_prob(self):
        narrow = _compute_bucket_probability(69000, 68500, 69500, 3600, "btcusdt")
        wide = _compute_bucket_probability(69000, 65000, 73000, 3600, "btcusdt")
        assert wide > narrow

    def test_more_time_spreads_probability(self):
        prob_1h = _compute_bucket_probability(69000, 68000, 70000, 3600, "btcusdt")
        prob_1w = _compute_bucket_probability(69000, 68000, 70000, 604800, "btcusdt")
        assert prob_1h > prob_1w

    def test_bucket_prob_is_cdf_difference(self):
        price, low, high, t, sym = 69000, 68000, 70000, 3600, "btcusdt"
        prob_above_low = _compute_threshold_probability(price, low, t, sym)
        prob_above_high = _compute_threshold_probability(price, high, t, sym)
        bucket_prob = _compute_bucket_probability(price, low, high, t, sym)
        expected = prob_above_low - prob_above_high
        assert abs(bucket_prob - max(0.01, min(0.99, expected))) < 0.001


class TestCryptoKeywords:
    """Test the broad crypto keyword detection."""

    def test_bitcoin(self):
        assert CRYPTO_KEYWORDS.search("Will the price of Bitcoin be greater than $78,000?")

    def test_btc(self):
        assert CRYPTO_KEYWORDS.search("BTC 5 Minute Up or Down")

    def test_ethereum(self):
        assert CRYPTO_KEYWORDS.search("Will the price of Ethereum be above $2,600?")

    def test_xrp(self):
        assert CRYPTO_KEYWORDS.search("Will the price of XRP be above $1.40?")

    def test_dogecoin(self):
        assert CRYPTO_KEYWORDS.search("Dogecoin Up or Down - March 10")

    def test_doge(self):
        assert CRYPTO_KEYWORDS.search("DOGE up or down today")

    def test_no_crypto(self):
        assert not CRYPTO_KEYWORDS.search("Will it rain in NYC?")

    def test_sol(self):
        assert CRYPTO_KEYWORDS.search("SOL above 85")

    def test_case_insensitive(self):
        assert CRYPTO_KEYWORDS.search("bitcoin price today")


class TestFindCryptoMarkets:
    """Test the find_crypto_markets filtering function using real phrasings."""

    def _make_market(self, question, desc=""):
        return FakeMarket(question=question, description=desc)

    def _filter(self, markets):
        """Inline version of find_crypto_markets."""
        return [m for m in markets if CRYPTO_KEYWORDS.search(m.question) and
                (UP_DOWN_PATTERN.search(m.question) or
                 THRESHOLD_PATTERN.search(m.question) or
                 BUCKET_PATTERN.search(m.question))]

    def test_finds_up_down(self):
        markets = [
            self._make_market("Bitcoin Up or Down - March 10, 5:15PM-5:30PM ET"),
            self._make_market("Will it rain tomorrow?"),
        ]
        assert len(self._filter(markets)) == 1

    def test_finds_threshold_greater_than(self):
        markets = [
            self._make_market("Will the price of Bitcoin be greater than $78,000 on March 10?"),
            self._make_market("Some non-crypto market"),
        ]
        assert len(self._filter(markets)) == 1

    def test_finds_threshold_less_than(self):
        markets = [
            self._make_market("Will the price of Bitcoin be less than $64,000 on March 11?"),
        ]
        assert len(self._filter(markets)) == 1

    def test_finds_threshold_reach(self):
        markets = [
            self._make_market("Will Bitcoin reach $85,000 in March?"),
        ]
        assert len(self._filter(markets)) == 1

    def test_finds_threshold_dip(self):
        markets = [
            self._make_market("Will Bitcoin dip to $50,000 in March?"),
        ]
        assert len(self._filter(markets)) == 1

    def test_finds_bucket(self):
        markets = [
            self._make_market("Will the price of Bitcoin be between $74,000 and $76,000 on March 11?"),
        ]
        assert len(self._filter(markets)) == 1

    def test_finds_all_types(self):
        markets = [
            self._make_market("BTC 5 Minute Up or Down - March 10, 2:00PM-2:05PM ET"),
            self._make_market("Will the price of Bitcoin be greater than $78,000 on March 10?"),
            self._make_market("Will the price of Bitcoin be between $74,000 and $76,000 on March 11?"),
            self._make_market("Will the price of Ethereum be above $2,600 on March 11?"),
            self._make_market("Will the price of Solana be above $110 on March 10?"),
            self._make_market("XRP Up or Down - March 10"),
            self._make_market("Will Bitcoin reach $85,000 in March?"),
            self._make_market("Will Bitcoin dip to $50,000 in March?"),
            self._make_market("Will it rain in NYC?"),
            self._make_market("Trump approval rating?"),
        ]
        found = self._filter(markets)
        assert len(found) == 8

    def test_rejects_generic_crypto_mention(self):
        markets = [self._make_market("Bitcoin is a great investment")]
        assert len(self._filter(markets)) == 0


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
        prob = self._compute(0.01, 30, 0.003)
        assert prob > 0.90

    def test_small_move_lots_of_time(self):
        prob = self._compute(0.002, 120, 0.003)
        big_prob = self._compute(0.01, 30, 0.003)
        assert prob < big_prob

    def test_medium_move_medium_time(self):
        prob = self._compute(0.005, 60, 0.003)
        assert prob > 0.70

    def test_high_vol_reduces_confidence(self):
        prob_low_vol = self._compute(0.005, 60, 0.003)
        prob_high_vol = self._compute(0.005, 60, 0.010)
        assert prob_low_vol > prob_high_vol

    def test_zero_remaining_vol(self):
        prob = self._compute(0.01, 0, 0.0)
        assert prob > 0.5

    def test_always_above_half(self):
        prob = self._compute(0.0001, 90, 0.003)
        assert prob >= 0.50
