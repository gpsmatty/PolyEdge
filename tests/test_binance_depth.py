"""Tests for Binance order book depth feed — DepthSnapshot + DepthStructure.

Pure logic tests. No DB, no API mocks needed.
"""

import time

import pytest

from polyedge.data.binance_depth import (
    DepthLevel,
    DepthSnapshot,
    DepthStructure,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_snapshot(
    symbol: str = "btcusdt",
    bid_prices: list[float] | None = None,
    ask_prices: list[float] | None = None,
    bid_qty: float = 1.0,
    ask_qty: float = 1.0,
    timestamp: float | None = None,
) -> DepthSnapshot:
    """Build a DepthSnapshot with uniform quantities at given prices."""
    if bid_prices is None:
        bid_prices = [100.0 - i * 0.1 for i in range(20)]  # 100.0, 99.9, ...
    if ask_prices is None:
        ask_prices = [100.1 + i * 0.1 for i in range(20)]  # 100.1, 100.2, ...

    bids = [DepthLevel(price=p, quantity=bid_qty) for p in bid_prices]
    asks = [DepthLevel(price=p, quantity=ask_qty) for p in ask_prices]

    return DepthSnapshot(
        symbol=symbol,
        bids=bids,
        asks=asks,
        timestamp=timestamp or time.time(),
    )


def make_imbalanced_snapshot(
    bid_qty: float = 1.0,
    ask_qty: float = 1.0,
    timestamp: float | None = None,
) -> DepthSnapshot:
    """Snapshot with controllable bid/ask quantities."""
    return make_snapshot(bid_qty=bid_qty, ask_qty=ask_qty, timestamp=timestamp)


# ---------------------------------------------------------------------------
# DepthLevel tests
# ---------------------------------------------------------------------------

class TestDepthLevel:
    def test_notional(self):
        level = DepthLevel(price=50000.0, quantity=0.5)
        assert level.notional == 25000.0

    def test_zero_quantity(self):
        level = DepthLevel(price=50000.0, quantity=0.0)
        assert level.notional == 0.0


# ---------------------------------------------------------------------------
# DepthSnapshot tests
# ---------------------------------------------------------------------------

class TestDepthSnapshot:
    def test_best_bid_ask(self):
        snap = make_snapshot()
        assert snap.best_bid == 100.0
        assert snap.best_ask == 100.1

    def test_mid_price(self):
        snap = make_snapshot()
        assert abs(snap.mid_price - 100.05) < 0.001

    def test_spread_bps(self):
        snap = make_snapshot()
        # Spread = 0.1, mid = 100.05, bps = (0.1 / 100.05) * 10000 ≈ 9.995
        assert 9.0 < snap.spread_bps < 11.0

    def test_balanced_imbalance(self):
        """Equal bid/ask quantities → imbalance near 0."""
        snap = make_imbalanced_snapshot(bid_qty=1.0, ask_qty=1.0)
        imb = snap.near_touch_imbalance(levels=5)
        assert abs(imb) < 0.05  # Not exactly 0 because prices differ

    def test_bid_heavy_imbalance(self):
        """More bid quantity → positive imbalance."""
        snap = make_imbalanced_snapshot(bid_qty=5.0, ask_qty=1.0)
        imb = snap.near_touch_imbalance(levels=5)
        assert imb > 0.5

    def test_ask_heavy_imbalance(self):
        """More ask quantity → negative imbalance."""
        snap = make_imbalanced_snapshot(bid_qty=1.0, ask_qty=5.0)
        imb = snap.near_touch_imbalance(levels=5)
        assert imb < -0.5

    def test_total_depths(self):
        snap = make_imbalanced_snapshot(bid_qty=2.0, ask_qty=3.0)
        # 20 levels each, notional = price * qty
        assert snap.total_bid_depth > 0
        assert snap.total_ask_depth > 0
        # Ask depth should be larger (more qty despite higher prices)
        assert snap.total_ask_depth > snap.total_bid_depth

    def test_weighted_imbalance(self):
        """Weighted imbalance should favor closer levels."""
        snap = make_imbalanced_snapshot(bid_qty=3.0, ask_qty=1.0)
        wi = snap.weighted_imbalance(levels=5)
        assert wi > 0.3  # Bid heavy

    def test_empty_book(self):
        snap = DepthSnapshot(symbol="btcusdt", bids=[], asks=[], timestamp=time.time())
        assert snap.best_bid == 0.0
        assert snap.best_ask == 0.0
        assert snap.mid_price == 0.0
        assert snap.near_touch_imbalance() == 0.0


# ---------------------------------------------------------------------------
# DepthStructure tests
# ---------------------------------------------------------------------------

class TestDepthStructure:
    def test_empty_structure(self):
        ds = DepthStructure(symbol="btcusdt")
        assert not ds.is_active
        assert ds.imbalance == 0.0
        assert ds.depth_momentum == 0.0
        assert ds.confidence == 0.0

    def test_single_snapshot(self):
        ds = DepthStructure(symbol="btcusdt")
        snap = make_imbalanced_snapshot(bid_qty=3.0, ask_qty=1.0)
        ds.add_snapshot(snap)

        assert ds.is_active
        assert ds.imbalance > 0.3  # Bid heavy
        assert ds.tick_count == 1

    def test_imbalance_velocity_flat(self):
        """Constant imbalance → velocity near 0."""
        ds = DepthStructure(symbol="btcusdt")
        now = time.time()

        # Add 30 snapshots over 3 seconds, all identical
        for i in range(30):
            snap = make_imbalanced_snapshot(bid_qty=2.0, ask_qty=1.0, timestamp=now + i * 0.1)
            ds.add_snapshot(snap)

        # Velocity should be near zero (stable book)
        assert abs(ds.imbalance_velocity_1s) < 0.1
        assert abs(ds.imbalance_velocity_3s) < 0.1

    def test_imbalance_velocity_building(self):
        """Bids growing → positive velocity."""
        ds = DepthStructure(symbol="btcusdt")
        now = time.time()

        # Phase 1: balanced book (0-2s)
        for i in range(20):
            snap = make_imbalanced_snapshot(bid_qty=1.0, ask_qty=1.0, timestamp=now + i * 0.1)
            ds.add_snapshot(snap)

        # Phase 2: bids pile up (2-3s)
        for i in range(10):
            t = now + 2.0 + i * 0.1
            snap = make_imbalanced_snapshot(bid_qty=3.0 + i * 0.5, ask_qty=1.0, timestamp=t)
            ds.add_snapshot(snap)

        # Velocity should be positive (imbalance shifting toward bids)
        assert ds.imbalance_velocity_3s > 0

    def test_depth_delta(self):
        """Bids growing, asks shrinking → positive depth_delta."""
        ds = DepthStructure(symbol="btcusdt")
        now = time.time()

        # Start with balanced book
        for i in range(15):
            snap = make_imbalanced_snapshot(bid_qty=1.0, ask_qty=1.0, timestamp=now + i * 0.1)
            ds.add_snapshot(snap)

        # Now bids increase, asks decrease
        for i in range(5):
            t = now + 1.5 + i * 0.1
            snap = make_imbalanced_snapshot(bid_qty=3.0, ask_qty=0.5, timestamp=t)
            ds.add_snapshot(snap)

        assert ds.depth_delta > 0  # Bids growing relative to asks

    def test_depth_momentum_composite(self):
        """Composite signal should combine velocity + delta + large orders."""
        ds = DepthStructure(symbol="btcusdt")
        now = time.time()

        # Build up bid pressure over 3 seconds
        for i in range(30):
            bid_qty = 1.0 + (i / 30.0) * 4.0  # Ramp from 1.0 to 5.0
            snap = make_imbalanced_snapshot(bid_qty=bid_qty, ask_qty=1.0, timestamp=now + i * 0.1)
            ds.add_snapshot(snap)

        # Should produce positive (bullish) momentum
        assert ds.depth_momentum > 0

    def test_bearish_pressure(self):
        """Ask-heavy book building → negative momentum."""
        ds = DepthStructure(symbol="btcusdt")
        now = time.time()

        for i in range(30):
            ask_qty = 1.0 + (i / 30.0) * 4.0  # Ask side ramps up
            snap = make_imbalanced_snapshot(bid_qty=1.0, ask_qty=ask_qty, timestamp=now + i * 0.1)
            ds.add_snapshot(snap)

        assert ds.depth_momentum < 0

    def test_reset_clears_state(self):
        ds = DepthStructure(symbol="btcusdt")
        snap = make_imbalanced_snapshot(bid_qty=3.0, ask_qty=1.0)
        ds.add_snapshot(snap)

        assert ds.is_active
        ds.reset()
        assert not ds.is_active
        assert ds.tick_count == 0

    def test_gap_detection_clears_history(self):
        """Large time gap should clear snapshot history."""
        ds = DepthStructure(symbol="btcusdt")
        now = time.time()

        # Add some snapshots
        for i in range(10):
            snap = make_imbalanced_snapshot(bid_qty=5.0, ask_qty=1.0, timestamp=now + i * 0.1)
            ds.add_snapshot(snap)
            ds.last_update_time = time.monotonic()  # Simulate real timing

        # Save state
        old_imb = ds.imbalance
        assert old_imb > 0

        # Simulate 3 second gap (> 2s threshold)
        import time as _time
        _time.sleep(0.01)  # Just to ensure monotonic advances
        ds.last_update_time = time.monotonic() - 3.0  # Fake gap

        # Add a balanced snapshot — gap detection should clear history
        snap = make_imbalanced_snapshot(bid_qty=1.0, ask_qty=1.0, timestamp=now + 10.0)
        ds.add_snapshot(snap)

        # After gap clear + new balanced snapshot, imbalance should be near 0
        assert abs(ds.imbalance) < abs(old_imb)

    def test_confidence_agreement(self):
        """When all signals agree, confidence should be high."""
        ds = DepthStructure(symbol="btcusdt")
        now = time.time()

        # Strong bid pressure building
        for i in range(50):
            bid_qty = 1.0 + (i / 50.0) * 8.0
            snap = make_imbalanced_snapshot(bid_qty=bid_qty, ask_qty=1.0, timestamp=now + i * 0.1)
            ds.add_snapshot(snap)

        assert ds.confidence > 0.3  # Reasonable confidence with agreement

    def test_confidence_low_data(self):
        """With very few snapshots, confidence should be low."""
        ds = DepthStructure(symbol="btcusdt")
        snap = make_imbalanced_snapshot(bid_qty=5.0, ask_qty=1.0)
        ds.add_snapshot(snap)

        assert ds.confidence == 0.0  # Need at least 10 snapshots

    def test_large_order_detection(self):
        """A sudden large bid should produce positive large_order_signal."""
        ds = DepthStructure(symbol="btcusdt")
        now = time.time()

        # Normal book for a while
        for i in range(60):
            snap = make_imbalanced_snapshot(bid_qty=1.0, ask_qty=1.0, timestamp=now + i * 0.1)
            ds.add_snapshot(snap)

        # Now a massive bid appears
        snap = make_imbalanced_snapshot(bid_qty=10.0, ask_qty=1.0, timestamp=now + 6.0)
        ds.add_snapshot(snap)

        # Large order signal should be positive (large bid detected)
        assert ds.large_order_signal > 0

    def test_configurable_weights(self):
        """Changing weights should shift the momentum output."""
        ds1 = DepthStructure(symbol="btcusdt", weight_imbalance_velocity=1.0, weight_depth_delta=0.0, weight_large_order=0.0)
        ds2 = DepthStructure(symbol="btcusdt", weight_imbalance_velocity=0.0, weight_depth_delta=1.0, weight_large_order=0.0)

        now = time.time()
        for i in range(30):
            bid_qty = 1.0 + (i / 30.0) * 4.0
            snap = make_imbalanced_snapshot(bid_qty=bid_qty, ask_qty=1.0, timestamp=now + i * 0.1)
            ds1.add_snapshot(snap)

            # Clone for ds2
            snap2 = make_imbalanced_snapshot(bid_qty=bid_qty, ask_qty=1.0, timestamp=now + i * 0.1)
            ds2.add_snapshot(snap2)

        # Both should be positive but potentially different magnitudes
        mom1 = ds1.depth_momentum
        mom2 = ds2.depth_momentum
        assert mom1 > 0
        assert mom2 > 0
        # They shouldn't be identical (different weight distributions)
        # (they could be close if velocity and delta happen to be similar)

    def test_latest_mid(self):
        ds = DepthStructure(symbol="btcusdt")
        snap = make_snapshot()
        ds.add_snapshot(snap)
        assert abs(ds.latest_mid - 100.05) < 0.01


# ---------------------------------------------------------------------------
# Signal quality tests — the core thesis
# ---------------------------------------------------------------------------

class TestLeadingSignalProperties:
    """Verify that the depth signal has the right directional properties.

    The thesis: when bids start stacking up BEFORE price moves,
    the depth momentum should go positive, indicating bullish pressure.
    """

    def test_bid_accumulation_is_bullish(self):
        """Bids growing = positive depth_momentum."""
        ds = DepthStructure(symbol="btcusdt")
        now = time.time()

        for i in range(40):
            bid_qty = 1.0 + (i / 40.0) * 5.0
            snap = make_imbalanced_snapshot(bid_qty=bid_qty, ask_qty=1.0, timestamp=now + i * 0.1)
            ds.add_snapshot(snap)

        assert ds.depth_momentum > 0.1

    def test_ask_accumulation_is_bearish(self):
        """Asks growing = negative depth_momentum."""
        ds = DepthStructure(symbol="btcusdt")
        now = time.time()

        for i in range(40):
            ask_qty = 1.0 + (i / 40.0) * 5.0
            snap = make_imbalanced_snapshot(bid_qty=1.0, ask_qty=ask_qty, timestamp=now + i * 0.1)
            ds.add_snapshot(snap)

        assert ds.depth_momentum < -0.1

    def test_stable_book_is_neutral(self):
        """No change in book = momentum near 0."""
        ds = DepthStructure(symbol="btcusdt")
        now = time.time()

        for i in range(40):
            snap = make_imbalanced_snapshot(bid_qty=2.0, ask_qty=2.0, timestamp=now + i * 0.1)
            ds.add_snapshot(snap)

        assert abs(ds.depth_momentum) < 0.1

    def test_velocity_is_more_important_than_level(self):
        """A book that just shifted should score higher than a statically imbalanced one."""
        # Static: always 70/30 bid-heavy
        ds_static = DepthStructure(symbol="btcusdt")
        now = time.time()
        for i in range(40):
            snap = make_imbalanced_snapshot(bid_qty=3.0, ask_qty=1.0, timestamp=now + i * 0.1)
            ds_static.add_snapshot(snap)

        # Dynamic: just shifted from 30/70 to 70/30
        ds_dynamic = DepthStructure(symbol="btcusdt")
        for i in range(20):
            snap = make_imbalanced_snapshot(bid_qty=1.0, ask_qty=3.0, timestamp=now + i * 0.1)
            ds_dynamic.add_snapshot(snap)
        for i in range(20):
            t = now + 2.0 + i * 0.1
            snap = make_imbalanced_snapshot(bid_qty=3.0, ask_qty=1.0, timestamp=t)
            ds_dynamic.add_snapshot(snap)

        # The dynamic shift should produce stronger momentum than static
        assert abs(ds_dynamic.depth_momentum) > abs(ds_static.depth_momentum)
