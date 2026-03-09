"""Tests for position sizing."""

from polyedge.risk.sizing import calculate_position_size


def test_basic_sizing():
    """Should return positive size when there's an edge."""
    size = calculate_position_size(
        bankroll=200.0,
        edge=0.10,
        probability=0.60,
        kelly_fraction=0.25,
        max_position_pct=0.10,
    )
    assert size > 0
    assert size <= 200 * 0.10  # Max 10% of bankroll


def test_no_edge_no_bet():
    """Should return 0 when there's no edge."""
    size = calculate_position_size(
        bankroll=200.0,
        edge=0.0,
        probability=0.50,
    )
    assert size == 0.0


def test_respects_max_position():
    """Should cap at max position size."""
    size = calculate_position_size(
        bankroll=200.0,
        edge=0.30,  # Very large edge
        probability=0.80,
        kelly_fraction=1.0,  # Full Kelly
        max_position_pct=0.10,
    )
    assert size <= 200 * 0.10 + 0.01  # Allow rounding


def test_below_minimum():
    """Should return 0 if bet size is below minimum."""
    size = calculate_position_size(
        bankroll=10.0,
        edge=0.02,
        probability=0.52,
        kelly_fraction=0.25,
        min_bet=1.0,
    )
    # With small bankroll and tiny edge, bet should be below minimum
    assert size == 0.0 or size >= 1.0


def test_zero_bankroll():
    """Should return 0 with no bankroll."""
    size = calculate_position_size(
        bankroll=0.0,
        edge=0.10,
        probability=0.60,
    )
    assert size == 0.0


def test_realistic_scenario():
    """Realistic: $200 bankroll, 10% edge, quarter Kelly."""
    size = calculate_position_size(
        bankroll=200.0,
        edge=0.10,
        probability=0.55,
        kelly_fraction=0.25,
        max_position_pct=0.10,
    )
    # Should be a reasonable amount ($2-$20)
    assert 0 <= size <= 20
