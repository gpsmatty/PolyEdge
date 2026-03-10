"""Tests for order book microstructure analysis."""

import pytest
from polyedge.core.models import OrderBook, OrderBookLevel
from polyedge.data.book_analyzer import analyze_book


def _make_book(bids: list[tuple], asks: list[tuple], market_id="test", token_id="tok1") -> OrderBook:
    """Helper: bids/asks as list of (price, size) tuples."""
    return OrderBook(
        market_id=market_id,
        token_id=token_id,
        bids=[OrderBookLevel(price=p, size=s) for p, s in bids],
        asks=[OrderBookLevel(price=p, size=s) for p, s in asks],
    )


def test_basic_spread():
    book = _make_book(
        bids=[(0.50, 100), (0.49, 200)],
        asks=[(0.52, 100), (0.53, 200)],
    )
    intel = analyze_book(book)
    assert intel.best_bid == 0.50
    assert intel.best_ask == 0.52
    assert intel.spread == pytest.approx(0.02)
    assert intel.midpoint == pytest.approx(0.51)


def test_imbalance_buy_heavy():
    book = _make_book(
        bids=[(0.50, 1000), (0.49, 500)],  # 1500 total bids
        asks=[(0.52, 100)],                  # 100 total asks
    )
    intel = analyze_book(book)
    assert intel.imbalance_ratio > 0.5  # Strong buy pressure
    assert intel.total_bid_size == 1500
    assert intel.total_ask_size == 100


def test_imbalance_sell_heavy():
    book = _make_book(
        bids=[(0.50, 100)],                  # 100 total bids
        asks=[(0.52, 1000), (0.53, 500)],    # 1500 total asks
    )
    intel = analyze_book(book)
    assert intel.imbalance_ratio < -0.5  # Strong sell pressure


def test_whale_detection():
    book = _make_book(
        bids=[(0.50, 10), (0.49, 10), (0.48, 10), (0.47, 500)],  # 500 is a whale
        asks=[(0.52, 10), (0.53, 10)],
    )
    intel = analyze_book(book)
    assert len(intel.whale_bids) >= 1
    assert intel.largest_bid == 500
    assert intel.whale_bias == "buy"


def test_wall_detection():
    # Wall = 5x avg. With many small levels + one huge, avg stays low
    book = _make_book(
        bids=[(0.50, 10), (0.49, 10), (0.48, 10), (0.47, 10), (0.45, 1000)],
        asks=[(0.52, 10), (0.53, 10)],
    )
    intel = analyze_book(book)
    # avg bid = (10+10+10+10+1000)/5 = 208, wall threshold = 5*208 = 1040
    # 1000 < 1040 so still not a wall. Let's make it bigger.
    book = _make_book(
        bids=[(0.50, 10), (0.49, 10), (0.48, 10), (0.47, 10),
               (0.46, 10), (0.45, 10), (0.44, 10), (0.43, 10),
               (0.40, 5000)],
        asks=[(0.52, 10)],
    )
    intel = analyze_book(book)
    assert intel.bid_wall_price == 0.40


def test_empty_book():
    book = _make_book(bids=[], asks=[])
    intel = analyze_book(book)
    assert intel.best_bid == 0.0
    assert intel.best_ask == 0.0
    assert intel.imbalance_ratio == 0.0


def test_depth_within_range():
    book = _make_book(
        bids=[(0.50, 100), (0.49, 200), (0.44, 300), (0.30, 1000)],
        asks=[(0.52, 100), (0.53, 200), (0.60, 500)],
    )
    intel = analyze_book(book)
    # Within 5 cents of best bid (0.50): 0.50, 0.49 (0.44 is 6 cents away)
    assert intel.bid_depth_5c == 300  # 100 + 200
    # Within 10 cents of best bid: 0.50, 0.49, 0.44 (0.30 is 20 cents away)
    assert intel.bid_depth_10c == 600  # 100 + 200 + 300


def test_summary_format():
    book = _make_book(
        bids=[(0.50, 100), (0.49, 200)],
        asks=[(0.52, 100), (0.53, 200)],
    )
    intel = analyze_book(book)
    summary = intel.summary()
    assert "Spread" in summary
    assert "Midpoint" in summary
    assert "imbalance" in summary.lower() or "balanced" in summary.lower()
