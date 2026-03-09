"""Tests for data models."""

from datetime import UTC, datetime, timedelta

from polyedge.core.models import (
    Market,
    OrderBook,
    OrderBookLevel,
    Signal,
    Side,
    AIAnalysis,
    PortfolioSnapshot,
)


def test_market_properties():
    m = Market(
        condition_id="test",
        question="Will it rain?",
        yes_price=0.65,
        no_price=0.35,
        clob_token_ids=["yes-token", "no-token"],
        end_date=datetime.now(UTC) + timedelta(hours=48),
    )
    assert m.yes_token_id == "yes-token"
    assert m.no_token_id == "no-token"
    assert m.implied_probability == 0.65
    assert m.hours_to_resolution is not None
    assert 47 < m.hours_to_resolution < 49


def test_market_no_end_date():
    m = Market(condition_id="test", question="Test?")
    assert m.hours_to_resolution is None


def test_orderbook_spread():
    book = OrderBook(
        market_id="test",
        token_id="tok",
        bids=[OrderBookLevel(price=0.48, size=100)],
        asks=[OrderBookLevel(price=0.52, size=100)],
    )
    assert book.best_bid == 0.48
    assert book.best_ask == 0.52
    assert abs(book.spread - 0.04) < 0.001
    assert abs(book.midpoint - 0.50) < 0.001


def test_orderbook_empty():
    book = OrderBook(market_id="test", token_id="tok")
    assert book.best_bid is None
    assert book.best_ask is None
    assert book.spread is None
    assert book.midpoint is None


def test_signal_creation():
    m = Market(condition_id="test", question="Test?", yes_price=0.40)
    sig = Signal(
        market=m,
        side=Side.YES,
        confidence=0.7,
        edge=0.15,
        ev=0.30,
        strategy="test",
    )
    assert sig.side == Side.YES
    assert sig.edge == 0.15


def test_ai_analysis_bounds():
    a = AIAnalysis(
        market_id="test",
        question="Test?",
        probability=0.75,
        confidence=0.80,
        reasoning="test reasoning",
        provider="claude",
        model="sonnet",
    )
    assert 0 <= a.probability <= 1
    assert 0 <= a.confidence <= 1


def test_portfolio_snapshot():
    snap = PortfolioSnapshot(
        bankroll=200.0,
        total_exposure=50.0,
    )
    assert snap.exposure_pct == 0.25

    empty = PortfolioSnapshot(bankroll=0.0)
    assert empty.exposure_pct == 0.0
