"""Tests for Cheap Event Hunter strategy."""

from datetime import datetime, timedelta, timezone

from polyedge.core.config import load_config
from polyedge.core.models import Market, Side
from polyedge.strategies.cheap_hunter import CheapHunterStrategy


def make_market(**kwargs) -> Market:
    defaults = {
        "condition_id": "test-123",
        "question": "Will X happen?",
        "yes_price": 0.05,
        "no_price": 0.95,
        "volume": 10000,
        "liquidity": 5000,
        "end_date": datetime.now(timezone.utc) + timedelta(days=7),
        "clob_token_ids": ["token-yes", "token-no"],
    }
    defaults.update(kwargs)
    return Market(**defaults)


def get_strategy() -> CheapHunterStrategy:
    settings = load_config()
    # Enable cheap_hunter for testing (disabled by default due to false positives)
    settings.strategies.cheap_hunter.enabled = True
    return CheapHunterStrategy(settings)


def test_finds_cheap_yes():
    """Should find cheap YES events."""
    strategy = get_strategy()
    market = make_market(yes_price=0.05, no_price=0.95, volume=5000, liquidity=5000)
    signal = strategy.evaluate(market)
    assert signal is not None
    assert signal.side == Side.YES
    assert signal.edge > 0


def test_ignores_expensive():
    """Should ignore events priced above threshold."""
    strategy = get_strategy()
    market = make_market(yes_price=0.50, no_price=0.50)
    signal = strategy.evaluate(market)
    assert signal is None


def test_ignores_low_volume():
    """Should ignore low-volume markets."""
    strategy = get_strategy()
    market = make_market(yes_price=0.05, volume=10, liquidity=5000)
    signal = strategy.evaluate(market)
    assert signal is None


def test_ignores_low_liquidity():
    """Should ignore low-liquidity markets."""
    strategy = get_strategy()
    market = make_market(yes_price=0.05, liquidity=100)
    signal = strategy.evaluate(market)
    assert signal is None


def test_ignores_near_resolution():
    """Should ignore markets resolving within min time."""
    strategy = get_strategy()
    market = make_market(
        yes_price=0.05,
        end_date=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    signal = strategy.evaluate(market)
    assert signal is None


def test_batch_sorts_by_ev():
    """Batch evaluation should sort by EV descending."""
    strategy = get_strategy()
    markets = [
        make_market(condition_id="a", yes_price=0.10, liquidity=5000, volume=5000),
        make_market(condition_id="b", yes_price=0.03, liquidity=5000, volume=5000),
        make_market(condition_id="c", yes_price=0.07, liquidity=5000, volume=5000),
    ]
    signals = strategy.evaluate_batch(markets)
    assert len(signals) > 0
    # Should be sorted by EV descending
    for i in range(len(signals) - 1):
        assert signals[i].ev >= signals[i + 1].ev


def test_finds_cheap_no():
    """Should also find cheap NO events."""
    strategy = get_strategy()
    market = make_market(yes_price=0.92, no_price=0.08, volume=5000, liquidity=5000)
    signal = strategy.evaluate(market)
    assert signal is not None
    assert signal.side == Side.NO
