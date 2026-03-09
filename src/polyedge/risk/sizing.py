"""Position sizing — bankroll-aware bet sizing."""

from __future__ import annotations

from polyedge.risk.kelly import kelly_from_market_price


def calculate_position_size(
    bankroll: float,
    edge: float,
    probability: float,
    kelly_fraction: float = 0.25,
    max_position_pct: float = 0.10,
    min_bet: float = 1.0,
) -> float:
    """Calculate dollar amount to bet on a trade.

    Args:
        bankroll: Current total bankroll in USD
        edge: Estimated edge (our probability - market price)
        probability: Our estimated true probability
        kelly_fraction: Fraction of full Kelly to use
        max_position_pct: Maximum position as % of bankroll
        min_bet: Minimum bet size in USD

    Returns:
        Dollar amount to bet
    """
    if bankroll <= 0 or edge <= 0 or probability <= 0:
        return 0.0

    # Market price implied from our probability and edge
    market_price = probability - edge
    if market_price <= 0 or market_price >= 1:
        return 0.0

    # Kelly-optimal fraction
    kelly_pct = kelly_from_market_price(probability, market_price, kelly_fraction)

    if kelly_pct <= 0:
        return 0.0

    # Cap at max position size
    position_pct = min(kelly_pct, max_position_pct)

    # Dollar amount
    amount = bankroll * position_pct

    # Floor at minimum bet
    if amount < min_bet:
        return 0.0

    return round(amount, 2)
