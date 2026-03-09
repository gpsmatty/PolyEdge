"""Tests for Kelly criterion calculations."""

from polyedge.risk.kelly import kelly_fraction, fractional_kelly, kelly_from_market_price


def test_kelly_basic():
    """Fair coin with 2:1 odds -> bet 25% of bankroll."""
    f = kelly_fraction(p_win=0.5, odds=2.0)
    assert abs(f - 0.25) < 0.001


def test_kelly_no_edge():
    """Fair coin with 1:1 odds -> don't bet."""
    f = kelly_fraction(p_win=0.5, odds=1.0)
    assert f == 0.0


def test_kelly_negative_edge():
    """Unfavorable odds -> don't bet (returns 0, not negative)."""
    f = kelly_fraction(p_win=0.3, odds=1.0)
    assert f == 0.0


def test_kelly_certain_win():
    """Very high probability -> bet big."""
    f = kelly_fraction(p_win=0.95, odds=1.0)
    assert f > 0.8


def test_kelly_edge_cases():
    """Edge cases should return 0."""
    assert kelly_fraction(0, 1.0) == 0.0
    assert kelly_fraction(1.0, 1.0) == 0.0
    assert kelly_fraction(0.5, 0) == 0.0
    assert kelly_fraction(0.5, -1) == 0.0


def test_fractional_kelly():
    """Quarter Kelly should be 25% of full Kelly."""
    full = kelly_fraction(p_win=0.5, odds=2.0)
    quarter = fractional_kelly(p_win=0.5, odds=2.0, fraction=0.25)
    assert abs(quarter - full * 0.25) < 0.001


def test_kelly_from_market_price():
    """Test market price-based Kelly calculation."""
    # Market price 0.40 (40%), we think 60% -> should bet
    f = kelly_from_market_price(
        estimated_probability=0.60,
        market_price=0.40,
        fraction=0.25,
    )
    assert f > 0

    # Market price 0.60, we think 40% -> should not bet on YES
    f = kelly_from_market_price(
        estimated_probability=0.40,
        market_price=0.60,
        fraction=0.25,
    )
    assert f == 0.0


def test_kelly_market_price_edge_cases():
    """Edge cases for market price Kelly."""
    assert kelly_from_market_price(0.5, 0.0) == 0.0
    assert kelly_from_market_price(0.5, 1.0) == 0.0


def test_kelly_realistic_polymarket():
    """Realistic Polymarket scenario.

    Market: YES at $0.52 (52% implied)
    Our estimate: 65% probability
    Quarter Kelly sizing
    """
    f = kelly_from_market_price(
        estimated_probability=0.65,
        market_price=0.52,
        fraction=0.25,
    )
    # Should recommend betting ~5-10% of bankroll
    assert 0.01 < f < 0.15
    # On $200 bankroll: ~$2-30
    bet_amount = 200 * f
    assert 2 < bet_amount < 30
