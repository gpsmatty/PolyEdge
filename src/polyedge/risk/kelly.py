"""Kelly criterion for optimal bet sizing."""

from __future__ import annotations


def kelly_fraction(p_win: float, odds: float) -> float:
    """Calculate the Kelly criterion optimal bet fraction.

    Args:
        p_win: Probability of winning (0-1)
        odds: Payout odds (net profit per $1 bet if you win)
              For a binary market at price `p`: odds = (1 - p) / p

    Returns:
        Optimal fraction of bankroll to bet (can be negative = don't bet)

    Formula: f* = (b*p - q) / b
    Where b = odds, p = win probability, q = 1 - p
    """
    if odds <= 0 or p_win <= 0 or p_win >= 1:
        return 0.0

    q = 1 - p_win
    f = (odds * p_win - q) / odds
    return max(0.0, f)  # Never bet negative


def fractional_kelly(
    p_win: float,
    odds: float,
    fraction: float = 0.25,
) -> float:
    """Calculate fractional Kelly bet size.

    Using a fraction of Kelly (typically 1/4 to 1/2) reduces volatility
    dramatically while sacrificing only a small amount of long-term growth.

    Args:
        p_win: Probability of winning (0-1)
        odds: Payout odds
        fraction: Fraction of full Kelly to use (default 0.25 = quarter Kelly)

    Returns:
        Fraction of bankroll to bet
    """
    full_kelly = kelly_fraction(p_win, odds)
    return full_kelly * fraction


def kelly_from_market_price(
    estimated_probability: float,
    market_price: float,
    fraction: float = 0.25,
) -> float:
    """Calculate fractional Kelly for a prediction market trade.

    Args:
        estimated_probability: Your estimate of the true probability (0-1)
        market_price: Current market price for the outcome (0-1)
        fraction: Kelly fraction to use

    Returns:
        Fraction of bankroll to bet

    Example:
        Market price: $0.40 (40% implied)
        Your estimate: 60%
        Odds: (1 - 0.40) / 0.40 = 1.5 (you get $1.50 profit per $1 bet)
        Full Kelly: (1.5 * 0.6 - 0.4) / 1.5 = 0.333 (33.3%)
        Quarter Kelly: 0.333 * 0.25 = 0.083 (8.3%)
    """
    if market_price <= 0 or market_price >= 1:
        return 0.0

    odds = (1 - market_price) / market_price
    return fractional_kelly(estimated_probability, odds, fraction)
