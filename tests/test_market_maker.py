"""Tests for the market maker strategy.

Tests the core logic:
1. Fair value from Poly book midpoint
2. Quote gating (no book data, tight spread, max inventory, one-sided book)
3. Defense (imbalance velocity, depth spike, spread compression)
4. Inventory tracking and skew
5. No naked shorts — never sell tokens we don't hold
6. P&L tracking through buy/sell cycles
7. Window reset clears all state
8. Force-sell progressive pricing
9. Static market mode (no time decay)
"""

import time
import pytest
from dataclasses import dataclass
from polyedge.core.config import MarketMakerConfig
from polyedge.strategies.market_maker import (
    MarketMakerStrategy,
    QuoteSet,
    Quote,
    Inventory,
    _snap_price,
)
from polyedge.data.book_analyzer import BookIntelligence


@pytest.fixture
def config():
    return MarketMakerConfig(
        enabled=True,
        base_spread=0.06,
        min_spread=0.04,
        max_spread=0.12,
        min_entry_price=0.10,
        max_entry_price=0.90,
        min_seconds_remaining=120.0,
        quote_size_usd=3.0,
        max_inventory_usd=15.0,
        max_inventory_imbalance=0.70,
        inventory_skew_factor=0.02,
        min_profitable_spread_bps=200.0,
        adverse_selection_threshold=0.70,
        imbalance_velocity_pull_threshold=0.15,
        whale_widen_factor=1.5,
        depth_defense_enabled=True,
        depth_pull_threshold=0.80,
        depth_recovery_seconds=3.0,
        max_loss_per_window_usd=2.0,
        requote_threshold=0.02,
        min_requote_interval=0.0,  # Disable for tests
        min_profit_pct=0.20,
        force_sell_seconds=60.0,
        force_sell_fire_sale_seconds=5.0,
    )


@pytest.fixture
def strategy(config):
    return MarketMakerStrategy(config)


CID = "test_condition_123"
YES_TID = "yes_token_456"
NO_TID = "no_token_789"


def _make_book(
    market_id: str = CID,
    token_id: str = YES_TID,
    best_bid: float = 0.47,
    best_ask: float = 0.53,
    imbalance_5c: float = 0.0,
    bid_depth_5c: float = 50.0,
    ask_depth_5c: float = 50.0,
    whale_bids=None,
    whale_asks=None,
) -> BookIntelligence:
    """Helper to create BookIntelligence for tests."""
    midpoint = (best_bid + best_ask) / 2 if best_bid > 0 and best_ask > 0 else max(best_bid, best_ask)
    spread = best_ask - best_bid if best_bid > 0 and best_ask > 0 else 0
    spread_bps = (spread / midpoint * 10000) if midpoint > 0 else 0
    return BookIntelligence(
        market_id=market_id,
        token_id=token_id,
        best_bid=best_bid,
        best_ask=best_ask,
        midpoint=midpoint,
        spread=spread,
        spread_bps=spread_bps,
        imbalance_5c=imbalance_5c,
        bid_depth_5c=bid_depth_5c,
        ask_depth_5c=ask_depth_5c,
        whale_bids=whale_bids or [],
        whale_asks=whale_asks or [],
    )


def _yes_no_books(
    yes_mid: float = 0.50,
    spread: float = 0.06,
    imbalance_5c: float = 0.0,
) -> tuple[BookIntelligence, BookIntelligence]:
    """Create YES and NO books from a fair value midpoint."""
    half = spread / 2
    yes_book = _make_book(
        token_id=YES_TID,
        best_bid=yes_mid - half,
        best_ask=yes_mid + half,
        imbalance_5c=imbalance_5c,
    )
    no_mid = 1.0 - yes_mid
    no_book = _make_book(
        token_id=NO_TID,
        best_bid=no_mid - half,
        best_ask=no_mid + half,
    )
    return yes_book, no_book


# ===================================================================
# Inventory
# ===================================================================


class TestInventory:
    def test_initial_state(self):
        inv = Inventory()
        assert inv.yes_tokens == 0
        assert inv.no_tokens == 0
        assert inv.imbalance == 0.5
        assert inv.avg_cost("YES") == 0.0

    def test_buy_yes(self):
        inv = Inventory()
        inv.record_fill("BUY", "YES", 10.0, 0.50)
        assert inv.yes_tokens == 10.0
        assert inv.yes_cost_basis == 5.0
        assert inv.avg_cost("YES") == 0.50
        assert inv.imbalance == 1.0

    def test_sell_yes_reduces_cost_basis(self):
        inv = Inventory()
        inv.record_fill("BUY", "YES", 10.0, 0.50)
        inv.record_fill("SELL", "YES", 5.0, 0.60)
        assert inv.yes_tokens == 5.0
        assert abs(inv.yes_cost_basis - 2.50) < 0.01
        assert abs(inv.avg_cost("YES") - 0.50) < 0.01

    def test_sell_all_clears_cost_basis(self):
        inv = Inventory()
        inv.record_fill("BUY", "YES", 10.0, 0.50)
        inv.record_fill("SELL", "YES", 10.0, 0.60)
        assert inv.yes_tokens == 0
        assert inv.yes_cost_basis == 0.0

    def test_multiple_buys_different_prices(self):
        inv = Inventory()
        inv.record_fill("BUY", "YES", 5.0, 0.40)
        inv.record_fill("BUY", "YES", 5.0, 0.60)
        assert inv.yes_tokens == 10.0
        assert inv.yes_cost_basis == 5.0
        assert abs(inv.avg_cost("YES") - 0.50) < 0.01

    def test_sell_more_than_held_clamps_to_zero(self):
        inv = Inventory()
        inv.record_fill("BUY", "YES", 5.0, 0.50)
        inv.record_fill("SELL", "YES", 10.0, 0.60)
        assert inv.yes_tokens == 0
        assert inv.yes_cost_basis == 0.0

    def test_net_exposure(self):
        inv = Inventory()
        inv.record_fill("BUY", "YES", 10.0, 0.50)
        inv.record_fill("BUY", "NO", 5.0, 0.50)
        assert inv.net_exposure == 5.0  # 10 YES - 5 NO


# ===================================================================
# Fair Value
# ===================================================================


class TestFairValue:
    def test_from_yes_book_midpoint(self, strategy):
        yes_book = _make_book(best_bid=0.47, best_ask=0.53)
        fv = strategy.compute_fair_value(yes_book, None)
        assert fv == 0.50

    def test_blended_with_no_book(self, strategy):
        yes_book = _make_book(best_bid=0.47, best_ask=0.53)  # mid=0.50
        no_book = _make_book(token_id=NO_TID, best_bid=0.47, best_ask=0.53)  # mid=0.50, implied YES=0.50
        fv = strategy.compute_fair_value(yes_book, no_book)
        assert fv == 0.50

    def test_no_book_data_returns_none(self, strategy):
        assert strategy.compute_fair_value(None, None) is None

    def test_empty_book_returns_none(self, strategy):
        book = _make_book(best_bid=0, best_ask=0)
        assert strategy.compute_fair_value(book, None) is None

    def test_bid_only_uses_bid(self, strategy):
        book = _make_book(best_bid=0.45, best_ask=0)
        fv = strategy.compute_fair_value(book, None)
        assert fv == 0.45

    def test_clamped_to_range(self, strategy):
        book = _make_book(best_bid=0.99, best_ask=1.00)
        fv = strategy.compute_fair_value(book, None)
        assert fv <= 0.98


# ===================================================================
# Quote Gating
# ===================================================================


class TestQuoteGating:
    def test_no_book_data_blocks(self, strategy):
        reason = strategy.should_quote(CID, None, None, 500.0)
        assert reason == "no_yes_book"

    def test_empty_book_blocks(self, strategy):
        book = _make_book(best_bid=0, best_ask=0)
        reason = strategy.should_quote(CID, book, None, 500.0)
        assert reason == "yes_book_empty"

    def test_tight_spread_blocks(self, strategy):
        # Spread = 1c = 200bps at midpoint 0.50 — exactly at threshold
        book = _make_book(best_bid=0.495, best_ask=0.505)
        # spread_bps = 0.01 / 0.50 * 10000 = 200 — need it BELOW threshold
        tight_book = _make_book(best_bid=0.498, best_ask=0.502)
        # spread_bps = 0.004 / 0.50 * 10000 = 80
        reason = strategy.should_quote(CID, tight_book, None, 500.0)
        assert reason is not None
        assert "spread_too_tight" in reason

    def test_one_sided_book_blocks(self, strategy):
        book = _make_book(imbalance_5c=0.85)  # Above 0.70 threshold
        reason = strategy.should_quote(CID, book, None, 500.0)
        assert reason is not None
        assert "one_sided" in reason

    def test_window_loss_blocks(self, strategy):
        strategy._window_pnl[CID] = -3.0  # Below -2.0 threshold
        book = _make_book()
        reason = strategy.should_quote(CID, book, None, 500.0)
        assert reason is not None
        assert "window_loss" in reason

    def test_near_expiry_blocks_without_inventory(self, strategy):
        book = _make_book()
        reason = strategy.should_quote(CID, book, None, 30.0)  # Below 120s
        assert reason == "near_expiry"

    def test_near_expiry_allows_with_inventory(self, strategy):
        inv = strategy.get_inventory(CID)
        inv.record_fill("BUY", "YES", 10.0, 0.50)
        book = _make_book()
        reason = strategy.should_quote(CID, book, None, 30.0)
        assert reason is None  # Should not block

    def test_good_conditions_pass(self, strategy):
        book = _make_book()
        reason = strategy.should_quote(CID, book, None, 500.0)
        assert reason is None


# ===================================================================
# Defense
# ===================================================================


class TestDefense:
    def test_depth_spike_pulls(self, strategy):
        strategy.config.depth_defense_enabled = True
        book = _make_book()
        reason = strategy.should_pull_quotes(CID, book, depth_momentum=0.95)
        assert reason is not None
        assert "depth_spike" in reason

    def test_depth_moderate_does_not_pull(self, strategy):
        strategy.config.depth_defense_enabled = True
        book = _make_book()
        reason = strategy.should_pull_quotes(CID, book, depth_momentum=0.50)
        assert reason is None

    def test_recovery_period(self, strategy):
        strategy.config.depth_defense_enabled = True
        book = _make_book()
        strategy.should_pull_quotes(CID, book, depth_momentum=0.95)
        # Should still be recovering
        reason = strategy.should_pull_quotes(CID, book, depth_momentum=0.0)
        assert reason == "recovering"

    def test_spread_compression_pulls(self, strategy):
        # Spread = 80bps < 200bps threshold
        tight_book = _make_book(best_bid=0.498, best_ask=0.502)
        reason = strategy.should_pull_quotes(CID, tight_book)
        assert reason == "spread_compressed"

    def test_imbalance_velocity_pulls(self, strategy):
        """Fast imbalance change should trigger a pull."""
        now = time.monotonic()
        # Simulate rapid imbalance readings
        history = strategy._get_imbalance_history(CID)
        # Add readings 0.5s apart with big imbalance change
        from polyedge.strategies.market_maker import _ImbalanceReading
        history.append(_ImbalanceReading(now - 1.0, 0.0))
        history.append(_ImbalanceReading(now - 0.5, 0.05))

        # Now call with high imbalance — velocity = (0.20 - 0.0) / 1.0 = 0.20 > 0.15
        book = _make_book(imbalance_5c=0.20)
        reason = strategy.should_pull_quotes(CID, book)
        assert reason is not None
        assert "imbalance_velocity" in reason


# ===================================================================
# No Naked Shorts
# ===================================================================


class TestNoNakedShorts:
    def test_no_ask_without_inventory(self, strategy):
        yes_book, no_book = _yes_no_books(0.50)
        qs = strategy.compute_quotes(CID, YES_TID, NO_TID, yes_book, no_book, 500)
        assert qs.yes_ask is None
        assert qs.no_ask is None

    def test_ask_only_with_inventory(self, strategy):
        inv = strategy.get_inventory(CID)
        inv.record_fill("BUY", "YES", 10.0, 0.50)

        yes_book, no_book = _yes_no_books(0.50)
        qs = strategy.compute_quotes(CID, YES_TID, NO_TID, yes_book, no_book, 500)
        assert qs.yes_ask is not None
        assert qs.yes_ask.side == "SELL"

    def test_ask_size_capped_to_inventory(self, strategy):
        inv = strategy.get_inventory(CID)
        inv.record_fill("BUY", "YES", 3.0, 0.50)

        yes_book, no_book = _yes_no_books(0.50)
        qs = strategy.compute_quotes(CID, YES_TID, NO_TID, yes_book, no_book, 500)
        if qs.yes_ask:
            assert qs.yes_ask.size <= 3.0

    def test_no_naked_short_no(self, strategy):
        yes_book, no_book = _yes_no_books(0.50)
        qs = strategy.compute_quotes(CID, YES_TID, NO_TID, yes_book, no_book, 500)
        assert qs.no_ask is None


# ===================================================================
# Both-Side Quoting
# ===================================================================


class TestBothSideQuoting:
    def test_both_sides_get_bids(self, strategy):
        yes_book, no_book = _yes_no_books(0.50)
        qs = strategy.compute_quotes(CID, YES_TID, NO_TID, yes_book, no_book, 500)
        assert qs.yes_bid is not None
        assert qs.no_bid is not None
        assert qs.yes_bid.token_id == YES_TID
        assert qs.no_bid.token_id == NO_TID

    def test_no_inventory_posts_sell(self, strategy):
        inv = strategy.get_inventory(CID)
        inv.record_fill("BUY", "NO", 10.0, 0.40)

        yes_book, no_book = _yes_no_books(0.60)
        qs = strategy.compute_quotes(CID, YES_TID, NO_TID, yes_book, no_book, 500)
        assert qs.no_ask is not None
        assert qs.no_ask.side == "SELL"


# ===================================================================
# Inventory Skew
# ===================================================================


class TestInventorySkew:
    def test_heavy_yes_lowers_yes_bid(self, strategy):
        # Baseline with no inventory
        yes_book, no_book = _yes_no_books(0.50)
        qs_base = strategy.compute_quotes("base", YES_TID, NO_TID, yes_book, no_book, 500)

        # Heavy YES inventory
        inv = strategy.get_inventory("heavy")
        inv.record_fill("BUY", "YES", 50.0, 0.50)
        qs_heavy = strategy.compute_quotes("heavy", YES_TID, NO_TID, yes_book, no_book, 500)

        if qs_base.yes_bid and qs_heavy.yes_bid:
            assert qs_heavy.yes_bid.price < qs_base.yes_bid.price

    def test_max_one_side_suppresses_bid(self, strategy):
        """At max inventory on YES side, YES bid should be suppressed."""
        inv = strategy.get_inventory(CID)
        # $15 max inventory, buy $12 worth (above 70% imbalance)
        inv.record_fill("BUY", "YES", 24.0, 0.50)  # $12 at 0.50

        yes_book, no_book = _yes_no_books(0.50)
        qs = strategy.compute_quotes(CID, YES_TID, NO_TID, yes_book, no_book, 500)
        assert qs.yes_bid is None  # Suppressed — overweight YES


# ===================================================================
# P&L Tracking
# ===================================================================


class TestPnLTracking:
    def test_record_fill_returns_avg_entry_on_sell(self, strategy):
        inv = strategy.get_inventory(CID)
        inv.record_fill("BUY", "YES", 10.0, 0.50)
        avg_entry = strategy.record_fill(CID, "SELL", "YES", 5.0, 0.55)
        assert avg_entry is not None
        assert abs(avg_entry - 0.50) < 0.01

    def test_record_fill_returns_none_on_buy(self, strategy):
        result = strategy.record_fill(CID, "BUY", "YES", 10.0, 0.50)
        assert result is None

    def test_window_pnl_positive(self, strategy):
        inv = strategy.get_inventory(CID)
        inv.record_fill("BUY", "YES", 10.0, 0.50)
        strategy.record_fill(CID, "SELL", "YES", 10.0, 0.55)
        pnl = strategy._window_pnl.get(CID, 0)
        assert abs(pnl - 0.50) < 0.01  # (0.55-0.50)*10

    def test_window_pnl_negative(self, strategy):
        inv = strategy.get_inventory(CID)
        inv.record_fill("BUY", "YES", 10.0, 0.50)
        strategy.record_fill(CID, "SELL", "YES", 10.0, 0.45)
        pnl = strategy._window_pnl.get(CID, 0)
        assert pnl < 0
        assert abs(pnl - (-0.50)) < 0.01

    def test_circuit_breaker(self, strategy):
        inv = strategy.get_inventory(CID)
        inv.record_fill("BUY", "YES", 20.0, 0.50)
        strategy.record_fill(CID, "SELL", "YES", 20.0, 0.35)  # Lost $3

        book = _make_book()
        reason = strategy.should_quote(CID, book, None, 500.0)
        assert reason is not None
        assert "window_loss" in reason


# ===================================================================
# Window Reset
# ===================================================================


class TestWindowReset:
    def test_clears_inventory(self, strategy):
        inv = strategy.get_inventory(CID)
        inv.record_fill("BUY", "YES", 10.0, 0.50)
        strategy.reset_window(CID)
        assert CID not in strategy.inventory

    def test_clears_pnl(self, strategy):
        strategy._window_pnl[CID] = -1.50
        strategy.reset_window(CID)
        assert CID not in strategy._window_pnl

    def test_clears_tracking(self, strategy):
        strategy._last_fair_value[CID] = 0.55
        strategy._last_quote_time[CID] = time.monotonic()
        strategy._pulled_until[CID] = time.monotonic() + 100
        strategy.reset_window(CID)
        assert CID not in strategy._last_fair_value
        assert CID not in strategy._last_quote_time
        assert CID not in strategy._pulled_until

    def test_clears_imbalance_history(self, strategy):
        history = strategy._get_imbalance_history(CID)
        from polyedge.strategies.market_maker import _ImbalanceReading
        history.append(_ImbalanceReading(time.monotonic(), 0.5))
        strategy.reset_window(CID)
        assert CID not in strategy._imbalance_history


# ===================================================================
# Spread Computation
# ===================================================================


class TestSpread:
    def test_base_spread(self, strategy):
        spread = strategy.compute_spread(CID, 500, None, 0.50)
        assert abs(spread - 0.06) < 0.001

    def test_time_decay_widens(self, strategy):
        spread = strategy.compute_spread(CID, 30, None, 0.50)
        assert spread > strategy.config.base_spread

    def test_clamp_max(self, strategy):
        spread = strategy.compute_spread(CID, 5, None, 0.50)
        assert spread <= strategy.config.max_spread

    def test_clamp_min(self, strategy):
        spread = strategy.compute_spread(CID, 500, None, 0.50)
        assert spread >= strategy.config.min_spread

    def test_static_market_no_time_decay(self, strategy):
        """seconds_remaining=None should use base spread (no time decay)."""
        spread = strategy.compute_spread(CID, None, None, 0.50)
        assert abs(spread - 0.06) < 0.001


# ===================================================================
# Force-Sell
# ===================================================================


class TestForceSell:
    def test_no_inventory_returns_empty(self, strategy):
        qs = strategy.compute_force_sell_quotes(CID, YES_TID, NO_TID, 30.0, None, None)
        assert not qs.is_active

    def test_with_inventory_returns_asks(self, strategy):
        inv = strategy.get_inventory(CID)
        inv.record_fill("BUY", "YES", 10.0, 0.50)

        yes_book = _make_book(best_bid=0.48, best_ask=0.52)
        qs = strategy.compute_force_sell_quotes(CID, YES_TID, NO_TID, 30.0, yes_book, None)
        assert qs.yes_ask is not None
        assert qs.yes_ask.side == "SELL"

    def test_fire_sale_uses_low_price(self, strategy):
        inv = strategy.get_inventory(CID)
        inv.record_fill("BUY", "YES", 10.0, 0.50)

        yes_book = _make_book(best_bid=0.48, best_ask=0.52)
        # 3 seconds left = fire sale territory (<5s threshold)
        qs = strategy.compute_force_sell_quotes(CID, YES_TID, NO_TID, 3.0, yes_book, None)
        assert qs.yes_ask is not None
        # Fire sale should be below cost basis
        assert qs.yes_ask.price < 0.50

    def test_normal_force_sell_at_cost(self, strategy):
        inv = strategy.get_inventory(CID)
        inv.record_fill("BUY", "YES", 10.0, 0.40)

        yes_book = _make_book(best_bid=0.48, best_ask=0.52)
        # 30 seconds left = force-sell but not fire-sale
        qs = strategy.compute_force_sell_quotes(CID, YES_TID, NO_TID, 30.0, yes_book, None)
        assert qs.yes_ask is not None
        # Should be at or above cost basis (breakeven)
        assert qs.yes_ask.price >= 0.40


# ===================================================================
# Full Cycle
# ===================================================================


class TestFullCycle:
    def test_buy_then_sell_profit(self, strategy):
        avg = strategy.record_fill(CID, "BUY", "YES", 6.0, 0.47)
        assert avg is None

        inv = strategy.get_inventory(CID)
        assert inv.yes_tokens == 6.0
        assert abs(inv.avg_cost("YES") - 0.47) < 0.01

        yes_book, no_book = _yes_no_books(0.50)
        qs = strategy.compute_quotes(CID, YES_TID, NO_TID, yes_book, no_book, 500)
        assert qs.yes_ask is not None
        assert qs.yes_ask.price > 0.47

        avg_entry = strategy.record_fill(CID, "SELL", "YES", 6.0, 0.53)
        assert abs(avg_entry - 0.47) < 0.01

        pnl = strategy._window_pnl.get(CID, 0)
        expected = (0.53 - 0.47) * 6.0
        assert abs(pnl - expected) < 0.01
        assert inv.yes_tokens == 0

    def test_no_token_cycle(self, strategy):
        strategy.record_fill(CID, "BUY", "NO", 10.0, 0.30)
        avg_entry = strategy.record_fill(CID, "SELL", "NO", 10.0, 0.50)
        assert abs(avg_entry - 0.30) < 0.01
        pnl = strategy._window_pnl.get(CID, 0)
        assert abs(pnl - 2.0) < 0.01


# ===================================================================
# QuoteSet and Quote basics
# ===================================================================


class TestQuoteSetBasics:
    def test_empty_quote_set(self):
        qs = QuoteSet()
        assert not qs.is_active
        assert qs.all_quotes() == []

    def test_bid_only_is_active(self):
        qs = QuoteSet(yes_bid=Quote("t1", "BUY", 0.47, 6.0))
        assert qs.is_active
        assert len(qs.all_quotes()) == 1

    def test_quote_as_order_dict(self):
        q = Quote("t1", "BUY", 0.47, 6.0, expiration=1234)
        d = q.as_order_dict()
        assert d["token_id"] == "t1"
        assert d["side"] == "BUY"
        assert d["price"] == 0.47
        assert d["size"] == 6.0
        assert d["post_only"] is True
        assert d["expiration"] == 1234


# ===================================================================
# Snap Price
# ===================================================================


class TestSnapPrice:
    def test_snap_to_cent(self):
        assert _snap_price(0.473, 0.01) == 0.47
        assert _snap_price(0.475, 0.01) == 0.48

    def test_clamp_low(self):
        assert _snap_price(0.001, 0.01) == 0.01

    def test_clamp_high(self):
        assert _snap_price(0.999, 0.01) == 0.99

    def test_half_cent_tick(self):
        # Polymarket uses 2 decimal places, so 0.475 rounds to 0.48
        assert _snap_price(0.475, 0.005) == 0.48


# ===================================================================
# Static Market Mode
# ===================================================================


class TestStaticMarketMode:
    def test_no_time_decay_on_sell(self, strategy):
        """Static markets (seconds_remaining=None) should use full profit floor."""
        inv = strategy.get_inventory(CID)
        inv.record_fill("BUY", "YES", 10.0, 0.40)

        yes_book, no_book = _yes_no_books(0.50)
        qs = strategy.compute_quotes(CID, YES_TID, NO_TID, yes_book, no_book, None)
        if qs.yes_ask:
            # Full 20% profit floor: 0.40 * 1.20 = 0.48
            assert qs.yes_ask.price >= 0.48

    def test_no_expiry_suppression(self, strategy):
        """Static markets should always allow new bids."""
        yes_book, no_book = _yes_no_books(0.50)
        qs = strategy.compute_quotes(CID, YES_TID, NO_TID, yes_book, no_book, None)
        assert qs.yes_bid is not None
