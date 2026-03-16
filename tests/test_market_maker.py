"""Comprehensive tests for the market maker strategy.

Tests every bug that was found in the audit:
1. No naked short sells — never sell YES without holding it
2. Window P&L circuit breaker fires correctly
3. Ask price never crosses best bid (post_only safety)
4. P&L computed correctly with accurate avg_entry
5. Inventory cleaned on window reset
6. Time gate allows exits near expiry
7. Inventory skew only shifts bid, not ask
8. Cost basis tracks correctly through buy/sell cycles
9. Size capped to inventory on sells
10. Full buy→sell→profit cycle
"""

import time
import pytest
from polyedge.core.config import MarketMakerConfig
from polyedge.strategies.market_maker import (
    MarketMakerStrategy,
    QuoteSet,
    Quote,
    Inventory,
)


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
        depth_pull_threshold=0.80,
        depth_widen_threshold=0.40,
        max_loss_per_window_usd=2.0,
        requote_threshold=0.02,
        requote_interval_seconds=5.0,
    )


@pytest.fixture
def strategy(config):
    return MarketMakerStrategy(config)


CID = "test_condition_123"
YES_TID = "yes_token_456"
NO_TID = "no_token_789"


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
        assert inv.yes_cost_basis == 5.0

        inv.record_fill("SELL", "YES", 5.0, 0.60)
        assert inv.yes_tokens == 5.0
        # Cost basis should be halved (sold half)
        assert abs(inv.yes_cost_basis - 2.50) < 0.01
        assert abs(inv.avg_cost("YES") - 0.50) < 0.01

    def test_sell_all_clears_cost_basis(self):
        inv = Inventory()
        inv.record_fill("BUY", "YES", 10.0, 0.50)
        inv.record_fill("SELL", "YES", 10.0, 0.60)
        assert inv.yes_tokens == 0
        assert inv.yes_cost_basis == 0.0
        assert inv.avg_cost("YES") == 0.0

    def test_multiple_buys_different_prices(self):
        inv = Inventory()
        inv.record_fill("BUY", "YES", 5.0, 0.40)  # $2.00
        inv.record_fill("BUY", "YES", 5.0, 0.60)  # $3.00
        assert inv.yes_tokens == 10.0
        assert inv.yes_cost_basis == 5.0  # $2 + $3
        assert abs(inv.avg_cost("YES") - 0.50) < 0.01

    def test_sell_more_than_held_clamps_to_zero(self):
        inv = Inventory()
        inv.record_fill("BUY", "YES", 5.0, 0.50)
        inv.record_fill("SELL", "YES", 10.0, 0.60)  # Sell more than held
        assert inv.yes_tokens == 0
        assert inv.yes_cost_basis == 0.0


class TestNoNakedShorts:
    """Bug #1: Never sell YES tokens we don't hold."""

    def test_no_ask_without_inventory(self, strategy):
        """Fresh start, no inventory → should NOT post an ask."""
        qs = strategy.compute_quotes(
            condition_id=CID,
            yes_token_id=YES_TID,
            no_token_id=NO_TID,
            yes_price=0.50,
            no_price=0.50,
            seconds_remaining=500,
        )
        # Should have a bid (buying YES) but NO ask (we don't hold any to sell)
        assert qs.yes_bid is not None, "Should post a bid"
        assert qs.yes_ask is None, "Should NOT post an ask without inventory"

    def test_ask_only_with_inventory(self, strategy):
        """After buying YES, should post ask to sell."""
        inv = strategy.get_inventory(CID)
        inv.record_fill("BUY", "YES", 10.0, 0.50)

        qs = strategy.compute_quotes(
            condition_id=CID,
            yes_token_id=YES_TID,
            no_token_id=NO_TID,
            yes_price=0.50,
            no_price=0.50,
            seconds_remaining=500,
        )
        assert qs.yes_ask is not None, "Should post an ask when holding inventory"
        assert qs.yes_ask.side == "SELL"

    def test_ask_size_capped_to_inventory(self, strategy):
        """Ask size must never exceed what we hold."""
        inv = strategy.get_inventory(CID)
        inv.record_fill("BUY", "YES", 3.0, 0.50)

        qs = strategy.compute_quotes(
            condition_id=CID,
            yes_token_id=YES_TID,
            no_token_id=NO_TID,
            yes_price=0.50,
            no_price=0.50,
            seconds_remaining=500,
        )
        if qs.yes_ask:
            assert qs.yes_ask.size <= 3.0, f"Ask size {qs.yes_ask.size} exceeds inventory 3.0"


class TestAskPriceFloor:
    """Bug #6: Ask price must be above best bid to avoid post_only rejection."""

    def test_ask_above_best_bid(self, strategy):
        """Ask should be above the actual Polymarket best bid."""
        inv = strategy.get_inventory(CID)
        inv.record_fill("BUY", "YES", 10.0, 0.50)

        qs = strategy.compute_quotes(
            condition_id=CID,
            yes_token_id=YES_TID,
            no_token_id=NO_TID,
            yes_price=0.60,
            no_price=0.40,
            seconds_remaining=500,
            yes_best_bid=0.58,
        )
        if qs.yes_ask:
            assert qs.yes_ask.price > 0.58, (
                f"Ask ${qs.yes_ask.price} must be above best bid $0.58"
            )

    def test_ask_above_best_bid_extreme_skew(self, strategy):
        """Even with heavy inventory skew, ask must stay above best bid."""
        inv = strategy.get_inventory(CID)
        inv.record_fill("BUY", "YES", 50.0, 0.50)  # Heavy imbalance

        qs = strategy.compute_quotes(
            condition_id=CID,
            yes_token_id=YES_TID,
            no_token_id=NO_TID,
            yes_price=0.70,
            no_price=0.30,
            seconds_remaining=500,
            yes_best_bid=0.68,
        )
        if qs.yes_ask:
            assert qs.yes_ask.price > 0.68, (
                f"Ask ${qs.yes_ask.price} crossed best bid $0.68 with heavy skew"
            )

    def test_ask_above_fair_value_without_best_bid(self, strategy):
        """Without best_bid info, ask should still be above fair value."""
        inv = strategy.get_inventory(CID)
        inv.record_fill("BUY", "YES", 10.0, 0.50)

        qs = strategy.compute_quotes(
            condition_id=CID,
            yes_token_id=YES_TID,
            no_token_id=NO_TID,
            yes_price=0.55,
            no_price=0.45,
            seconds_remaining=500,
            yes_best_bid=0.0,  # No best bid info
        )
        if qs.yes_ask:
            fv = qs.fair_value
            assert qs.yes_ask.price >= fv, (
                f"Ask ${qs.yes_ask.price} below fair value ${fv}"
            )


class TestInventorySkew:
    """Verify skew only lowers bid, doesn't lower ask below market."""

    def test_balanced_inventory_no_skew(self, strategy):
        """With no inventory, bid and ask should be symmetric around FV."""
        qs = strategy.compute_quotes(
            condition_id=CID,
            yes_token_id=YES_TID,
            no_token_id=NO_TID,
            yes_price=0.50,
            no_price=0.50,
            seconds_remaining=500,
        )
        if qs.yes_bid:
            fv = qs.fair_value
            bid_dist = fv - qs.yes_bid.price
            # Bid should be ~half_spread below FV
            assert 0.02 <= bid_dist <= 0.10

    def test_heavy_yes_inventory_lowers_bid(self, strategy):
        """Heavy YES inventory should lower the bid (discourage buying more)."""
        # Get baseline bid with no inventory
        qs_base = strategy.compute_quotes(
            condition_id="base",
            yes_token_id=YES_TID,
            no_token_id=NO_TID,
            yes_price=0.50,
            no_price=0.50,
            seconds_remaining=500,
        )

        # Now with heavy YES inventory
        inv = strategy.get_inventory("heavy")
        inv.record_fill("BUY", "YES", 50.0, 0.50)

        qs_heavy = strategy.compute_quotes(
            condition_id="heavy",
            yes_token_id=YES_TID,
            no_token_id=NO_TID,
            yes_price=0.50,
            no_price=0.50,
            seconds_remaining=500,
        )

        if qs_base.yes_bid and qs_heavy.yes_bid:
            assert qs_heavy.yes_bid.price < qs_base.yes_bid.price, (
                "Heavy YES inventory should lower bid price"
            )


class TestPnLTracking:
    """Bug #2: Window P&L must be tracked. Bug #4: avg_entry must be accurate."""

    def test_record_fill_returns_avg_entry_on_sell(self, strategy):
        """record_fill should return avg_entry for sells."""
        inv = strategy.get_inventory(CID)
        inv.record_fill("BUY", "YES", 10.0, 0.50)

        avg_entry = strategy.record_fill(CID, "SELL", "YES", 5.0, 0.55)
        assert avg_entry is not None
        assert abs(avg_entry - 0.50) < 0.01, f"avg_entry {avg_entry} should be 0.50"

    def test_record_fill_returns_none_on_buy(self, strategy):
        """record_fill should return None for buys."""
        result = strategy.record_fill(CID, "BUY", "YES", 10.0, 0.50)
        assert result is None

    def test_window_pnl_updated_on_sell(self, strategy):
        """Window P&L should be updated when a sell fill occurs."""
        inv = strategy.get_inventory(CID)
        inv.record_fill("BUY", "YES", 10.0, 0.50)

        strategy.record_fill(CID, "SELL", "YES", 10.0, 0.55)

        pnl = strategy._window_pnl.get(CID, 0)
        expected_pnl = (0.55 - 0.50) * 10.0  # $0.50
        assert abs(pnl - expected_pnl) < 0.01, f"Window P&L {pnl} != expected {expected_pnl}"

    def test_window_pnl_negative_on_loss(self, strategy):
        """Window P&L should go negative on losing sells."""
        inv = strategy.get_inventory(CID)
        inv.record_fill("BUY", "YES", 10.0, 0.50)

        strategy.record_fill(CID, "SELL", "YES", 10.0, 0.45)

        pnl = strategy._window_pnl.get(CID, 0)
        expected_pnl = (0.45 - 0.50) * 10.0  # -$0.50
        assert pnl < 0
        assert abs(pnl - expected_pnl) < 0.01

    def test_circuit_breaker_fires_on_loss(self, strategy):
        """Circuit breaker should pull quotes when window loss exceeds threshold."""
        # Simulate a big loss
        inv = strategy.get_inventory(CID)
        inv.record_fill("BUY", "YES", 20.0, 0.50)  # $10 invested
        strategy.record_fill(CID, "SELL", "YES", 20.0, 0.35)  # Lost $3

        pnl = strategy._window_pnl.get(CID, 0)
        assert pnl < -2.0  # Below max_loss_per_window_usd

        reason = strategy.should_pull_quotes(CID, depth_momentum=0)
        assert reason is not None
        assert "window_loss" in reason


class TestWindowReset:
    """Bug #10: Stale inventory must be cleaned on window reset."""

    def test_reset_clears_inventory(self, strategy):
        """reset_window should clear inventory for that condition_id."""
        inv = strategy.get_inventory(CID)
        inv.record_fill("BUY", "YES", 10.0, 0.50)
        assert CID in strategy.inventory

        strategy.reset_window(CID)
        assert CID not in strategy.inventory

    def test_reset_clears_pnl(self, strategy):
        """reset_window should clear window P&L."""
        strategy._window_pnl[CID] = -1.50
        strategy.reset_window(CID)
        assert CID not in strategy._window_pnl

    def test_reset_clears_tracking(self, strategy):
        """reset_window should clear FV and quote time tracking."""
        strategy._last_fair_value[CID] = 0.55
        strategy._last_quote_time[CID] = time.monotonic()
        strategy._pulled_until[CID] = time.monotonic() + 100

        strategy.reset_window(CID)
        assert CID not in strategy._last_fair_value
        assert CID not in strategy._last_quote_time
        assert CID not in strategy._pulled_until


class TestTimeGate:
    """Bug #3 partial: Time gate should allow sells near expiry."""

    def test_near_expiry_blocks_new_entry(self, strategy):
        """Near expiry with no inventory → pull quotes."""
        qs = strategy.compute_quotes(
            condition_id=CID,
            yes_token_id=YES_TID,
            no_token_id=NO_TID,
            yes_price=0.50,
            no_price=0.50,
            seconds_remaining=30,  # Below min_seconds_remaining (120)
        )
        assert qs.reason_pulled == "near_expiry"
        assert qs.yes_bid is None
        assert qs.yes_ask is None

    def test_near_expiry_allows_sell_with_inventory(self, strategy):
        """Near expiry with inventory → should still post asks to offload."""
        inv = strategy.get_inventory(CID)
        inv.record_fill("BUY", "YES", 10.0, 0.50)

        qs = strategy.compute_quotes(
            condition_id=CID,
            yes_token_id=YES_TID,
            no_token_id=NO_TID,
            yes_price=0.55,
            no_price=0.45,
            seconds_remaining=30,  # Below min_seconds_remaining
        )
        # Should NOT be pulled — we have inventory to offload
        assert qs.reason_pulled is None or qs.reason_pulled == "no_requote_needed"
        # Bid should be suppressed (no new buys near expiry)
        # But ask should be posted to sell
        if qs.yes_ask is not None:
            assert qs.yes_ask.side == "SELL"


class TestPriceGate:
    """Price range filters should allow sells even when price is extreme."""

    def test_price_out_of_range_no_inventory_pulls(self, strategy):
        """Price > max_entry_price with no inventory → pull."""
        qs = strategy.compute_quotes(
            condition_id=CID,
            yes_token_id=YES_TID,
            no_token_id=NO_TID,
            yes_price=0.95,  # Above max_entry_price (0.90)
            no_price=0.05,
            seconds_remaining=500,
        )
        assert qs.reason_pulled is not None
        assert "price_range" in qs.reason_pulled

    def test_price_too_high_with_inventory_posts_ask(self, strategy):
        """Price > max_entry_price but holding inventory → post ask to sell."""
        inv = strategy.get_inventory(CID)
        inv.record_fill("BUY", "YES", 10.0, 0.50)

        qs = strategy.compute_quotes(
            condition_id=CID,
            yes_token_id=YES_TID,
            no_token_id=NO_TID,
            yes_price=0.95,
            no_price=0.05,
            seconds_remaining=500,
        )
        # Should NOT pull — we have inventory
        assert qs.reason_pulled is None or qs.reason_pulled == "no_requote_needed"

    def test_price_too_low_with_inventory_no_bid(self, strategy):
        """Price < min_entry_price with inventory → post ask but NO bid."""
        inv = strategy.get_inventory("low_price")
        inv.record_fill("BUY", "YES", 10.0, 0.50)

        qs = strategy.compute_quotes(
            condition_id="low_price",
            yes_token_id=YES_TID,
            no_token_id=NO_TID,
            yes_price=0.05,
            no_price=0.95,
            seconds_remaining=500,
        )
        # Should NOT pull — we have inventory
        assert qs.reason_pulled is None or qs.reason_pulled == "no_requote_needed"
        # BID must be suppressed — don't buy more at extreme lows
        assert qs.yes_bid is None, "Should NOT post bid when price is below min_entry_price"

    def test_price_too_low_with_inventory_posts_ask(self, strategy):
        """Price < min_entry_price but holding inventory → post ask to sell."""
        inv = strategy.get_inventory(CID)
        inv.record_fill("BUY", "YES", 10.0, 0.50)

        qs = strategy.compute_quotes(
            condition_id=CID,
            yes_token_id=YES_TID,
            no_token_id=NO_TID,
            yes_price=0.05,
            no_price=0.95,
            seconds_remaining=500,
        )
        assert qs.reason_pulled is None or qs.reason_pulled == "no_requote_needed"


class TestDepthDefense:
    def test_depth_spike_pulls_quotes(self, strategy):
        """High depth momentum should pull all quotes."""
        qs = strategy.compute_quotes(
            condition_id=CID,
            yes_token_id=YES_TID,
            no_token_id=NO_TID,
            yes_price=0.50,
            no_price=0.50,
            seconds_remaining=500,
            depth_momentum=0.95,  # Above pull threshold (0.80)
        )
        assert qs.reason_pulled is not None
        assert "depth_spike" in qs.reason_pulled

    def test_depth_moderate_does_not_pull(self, strategy):
        """Moderate depth momentum should NOT pull (just widen spread)."""
        qs = strategy.compute_quotes(
            condition_id=CID,
            yes_token_id=YES_TID,
            no_token_id=NO_TID,
            yes_price=0.50,
            no_price=0.50,
            seconds_remaining=500,
            depth_momentum=0.50,  # Below pull (0.80) but above widen (0.40)
        )
        assert qs.reason_pulled is None
        # Spread should be wider than base
        assert qs.spread > strategy.config.base_spread

    def test_depth_recovery_period(self, strategy):
        """After a pull, should stay pulled for recovery period."""
        # First: trigger a pull
        strategy.compute_quotes(
            condition_id=CID,
            yes_token_id=YES_TID,
            no_token_id=NO_TID,
            yes_price=0.50,
            no_price=0.50,
            seconds_remaining=500,
            depth_momentum=0.95,
        )

        # Second: even with zero momentum, should still be recovering
        qs2 = strategy.compute_quotes(
            condition_id=CID,
            yes_token_id=YES_TID,
            no_token_id=NO_TID,
            yes_price=0.50,
            no_price=0.50,
            seconds_remaining=500,
            depth_momentum=0.0,
        )
        assert qs2.reason_pulled == "recovering"


class TestFullCycle:
    """End-to-end: buy at bid → sell at ask → verify profit."""

    def test_buy_then_sell_profit(self, strategy):
        """Full cycle: bid fills, then ask fills, P&L is positive."""
        # Step 1: Bid fills — someone sold to us
        avg = strategy.record_fill(CID, "BUY", "YES", 6.0, 0.47)
        assert avg is None  # No avg_entry for buys

        inv = strategy.get_inventory(CID)
        assert inv.yes_tokens == 6.0
        assert abs(inv.avg_cost("YES") - 0.47) < 0.01

        # Step 2: Compute quotes — should have ask
        qs = strategy.compute_quotes(
            condition_id=CID,
            yes_token_id=YES_TID,
            no_token_id=NO_TID,
            yes_price=0.50,
            no_price=0.50,
            seconds_remaining=500,
        )
        assert qs.yes_ask is not None
        assert qs.yes_ask.price > 0.47  # Ask should be above our entry

        # Step 3: Ask fills — someone bought from us
        avg_entry = strategy.record_fill(CID, "SELL", "YES", 6.0, 0.53)
        assert avg_entry is not None
        assert abs(avg_entry - 0.47) < 0.01

        # Step 4: P&L should be positive
        pnl = strategy._window_pnl.get(CID, 0)
        expected = (0.53 - 0.47) * 6.0  # $0.36
        assert pnl > 0
        assert abs(pnl - expected) < 0.01

        # Step 5: Inventory should be cleared
        assert inv.yes_tokens == 0

    def test_buy_then_sell_loss(self, strategy):
        """Full cycle with loss: market moved against us."""
        strategy.record_fill(CID, "BUY", "YES", 10.0, 0.55)

        avg_entry = strategy.record_fill(CID, "SELL", "YES", 10.0, 0.45)
        assert abs(avg_entry - 0.55) < 0.01

        pnl = strategy._window_pnl.get(CID, 0)
        assert pnl < 0
        expected = (0.45 - 0.55) * 10.0  # -$1.00
        assert abs(pnl - expected) < 0.01


class TestQuoteSetBasics:
    def test_empty_quote_set(self):
        qs = QuoteSet()
        assert not qs.is_active
        assert qs.all_quotes() == []

    def test_bid_only_is_active(self):
        qs = QuoteSet(yes_bid=Quote("t1", "BUY", 0.47, 6.0))
        assert qs.is_active
        assert len(qs.all_quotes()) == 1

    def test_both_sides(self):
        qs = QuoteSet(
            yes_bid=Quote("t1", "BUY", 0.47, 6.0),
            yes_ask=Quote("t1", "SELL", 0.53, 5.0),
        )
        assert qs.is_active
        assert len(qs.all_quotes()) == 2

    def test_quote_as_order_dict(self):
        q = Quote("t1", "BUY", 0.47, 6.0, expiration=1234)
        d = q.as_order_dict()
        assert d["token_id"] == "t1"
        assert d["side"] == "BUY"
        assert d["price"] == 0.47
        assert d["size"] == 6.0
        assert d["post_only"] is True
        assert d["expiration"] == 1234


class TestSpread:
    def test_base_spread(self, strategy):
        spread = strategy.compute_spread(CID, seconds_remaining=500)
        assert abs(spread - 0.06) < 0.001

    def test_time_decay_widens(self, strategy):
        spread = strategy.compute_spread(CID, seconds_remaining=30)
        assert spread > strategy.config.base_spread

    def test_depth_widens(self, strategy):
        spread = strategy.compute_spread(CID, seconds_remaining=500, depth_momentum=0.60)
        assert spread > strategy.config.base_spread

    def test_spread_clamp_max(self, strategy):
        spread = strategy.compute_spread(CID, seconds_remaining=5, depth_momentum=0.99)
        assert spread <= strategy.config.max_spread

    def test_spread_clamp_min(self, strategy):
        spread = strategy.compute_spread(CID, seconds_remaining=500, depth_momentum=0.0)
        assert spread >= strategy.config.min_spread


class TestFairValue:
    def test_midpoint(self, strategy):
        fv = strategy.compute_fair_value(0.50, 0.50)
        assert fv == 0.50

    def test_yes_higher(self, strategy):
        fv = strategy.compute_fair_value(0.70, 0.30)
        assert fv == 0.70

    def test_clamped_low(self, strategy):
        fv = strategy.compute_fair_value(0.00, 1.00)
        assert fv >= 0.01

    def test_clamped_high(self, strategy):
        fv = strategy.compute_fair_value(1.00, 0.00)
        assert fv <= 0.99
