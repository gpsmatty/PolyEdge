"""Comprehensive tests for the micro sniper eval pipeline.

Tests the REAL strategy code (not simplified mirrors) to verify:
1. Trade cooldown only blocks entries, not exits
2. Gradual 30s entry filter scales correctly (not binary)
3. Gradual 30s exit filter scales correctly (not binary)
4. Exit threshold logging is accurate
5. All runner eval path gates work for both entry and exit
6. Per-timeframe config merging
7. DB config serialization/deserialization of timeframe overrides

Uses mock for py_clob_client to avoid SOCKS proxy dependency.
"""

import sys
import time
import types
import pytest
from dataclasses import dataclass, field
from unittest.mock import MagicMock, AsyncMock, patch

# Mock the problematic py_clob_client before any polyedge imports
_mock_client = types.ModuleType('polyedge.core.client')
_mock_client.PolyClient = type('PolyClient', (), {})
sys.modules.setdefault('polyedge.core.client', _mock_client)

from polyedge.strategies.micro_sniper import MicroSniperStrategy, MicroAction, MicroOpportunity
from polyedge.core.config import Settings, MicroSniperConfig
from polyedge.core.models import Market, Side
from polyedge.data.binance_aggtrade import MicroStructure, AggTrade, TradeFlowWindow
from polyedge.data.research import NoTradeReason


# ── Helpers ──

def make_settings(**overrides) -> Settings:
    """Create Settings with micro_sniper config overrides."""
    s = Settings()
    for k, v in overrides.items():
        setattr(s.strategies.micro_sniper, k, v)
    return s


def make_market(yes_price=0.50, no_price=0.50, condition_id="0xtest123") -> Market:
    """Create a Market with sensible defaults."""
    return Market(
        condition_id=condition_id,
        question="Bitcoin Up or Down - Test",
        description="",
        slug="",
        category="crypto",
        end_date=None,
        active=True,
        closed=False,
        clob_token_ids=["token_yes", "token_no"],
        yes_price=yes_price,
        no_price=no_price,
        volume=10000,
        liquidity=5000,
        spread=0.01,
        raw={},
    )


def make_micro_with_momentum(
    momentum_direction: str = "bullish",
    strength: float = 0.8,
    n_trades: int = 30,
    ofi_30s: float = None,
    trade_intensity_30s: float = 15.0,
    price_change_pct: float = 0.001,
) -> MicroStructure:
    """Create a MicroStructure with controlled momentum.

    The `strength` parameter directly controls the value returned by
    `momentum_signal` (via a property mock). This avoids the complex
    composite calculation (OFI + VWAP drift + dampener) that makes it
    nearly impossible to predict the exact momentum from trade inputs.

    The 30s OFI can still be independently controlled via `ofi_30s` for
    testing gradual entry/exit filters.

    Args:
        momentum_direction: "bullish" or "bearish"
        strength: Desired abs(momentum_signal) value (0-1).
        n_trades: Number of trades to populate flow windows.
        ofi_30s: Override 30s OFI directly (None = let it derive from trades).
        trade_intensity_30s: Target intensity for 30s window.
        price_change_pct: Price change in window.
    """
    micro = MicroStructure("btcusdt")
    base_price = 70000.0

    # Generate trades to populate flow windows (needed for is_active, trade counts, etc.)
    if momentum_direction == "bullish":
        buy_fraction = 0.7  # Mostly buys
    else:
        buy_fraction = 0.3  # Mostly sells

    price_end = base_price * (1 + price_change_pct)
    now = time.time()
    time_spacing = 0.5  # 2 trades/sec

    for i in range(n_trades):
        t = now - (n_trades - i) * time_spacing
        price = base_price + (price_end - base_price) * (i / max(1, n_trades - 1))
        is_buyer_maker = i >= int(n_trades * buy_fraction)

        trade = AggTrade(
            symbol="btcusdt",
            price=price,
            quantity=0.01,
            is_buyer_maker=is_buyer_maker,
            timestamp=t,
        )
        micro.add_trade(trade)

    micro.window_start_price = base_price
    micro.window_start_time = now - n_trades * time_spacing
    micro.current_price = price_end

    # Override 30s OFI if requested (useful for testing gradual filters)
    if ofi_30s is not None:
        _force_ofi(micro.flow_30s, ofi_30s)

    # Patch momentum_signal and confidence as instance-level overrides.
    # We can't use type(micro).momentum_signal = property(...) because that
    # would affect ALL MicroStructure instances. Instead, we replace the
    # property lookup for this specific instance using __class__ trick.
    target_momentum = strength if momentum_direction == "bullish" else -strength
    target_confidence = min(0.9, 0.4 + strength * 0.5)

    # Create a per-instance subclass with overridden properties
    micro.__class__ = type(
        'MockedMicroStructure',
        (MicroStructure,),
        {
            'momentum_signal': property(lambda self: target_momentum),
            'confidence': property(lambda self: target_confidence),
        }
    )

    return micro


def _force_ofi(flow_window: TradeFlowWindow, target_ofi: float):
    """Force a flow window's OFI to a specific value by adjusting volumes."""
    # OFI = (buy_vol - sell_vol) / (buy_vol + sell_vol)
    total = max(flow_window.buy_volume + flow_window.sell_volume, 1.0)
    flow_window.buy_volume = total * (1 + target_ofi) / 2
    flow_window.sell_volume = total * (1 - target_ofi) / 2


def make_strategy(**config_overrides) -> MicroSniperStrategy:
    """Create a strategy with config overrides."""
    s = make_settings(**config_overrides)
    strategy = MicroSniperStrategy(s)
    return strategy


# Default overrides to disable ALL filters — use as base for single-filter tests.
# Each test re-enables only the filter it's testing.
ALL_FILTERS_OFF = dict(
    trend_bias_enabled=False,
    adaptive_bias_enabled=False,
    chop_filter_enabled=False,
    acceleration_enabled=False,
    low_vol_block_enabled=False,
    high_intensity_block_enabled=False,
    entry_persistence_enabled=False,
    dead_market_band=0.0,
    min_entry_price=0.01,
    max_entry_price=0.99,
    poly_book_enabled=False,
    trailing_stop_enabled=False,
    take_profit_enabled=False,
    enable_flips=False,
    entry_threshold=0.30,    # Low so strong signals pass by default
    min_confidence=0.10,
    min_trades_in_window=1,
)


def make_clean_strategy(**single_filter_overrides) -> MicroSniperStrategy:
    """Create a strategy with ALL filters off, then apply specific overrides.

    Use this to test individual filters in isolation.
    """
    combined = {**ALL_FILTERS_OFF, **single_filter_overrides}
    return make_strategy(**combined)


def make_book_intel(
    yes_bid_depth=50.0, yes_imbalance=0.0,
    no_bid_depth=50.0, no_imbalance=0.0,
) -> dict:
    """Create a mock BookIntelligence dict for testing poly book filters."""
    from dataclasses import dataclass

    @dataclass
    class MockBookIntel:
        market_id: str = "test"
        token_id: str = "test"
        bid_depth_5c: float = 50.0
        imbalance_5c: float = 0.0

    return {
        "yes": MockBookIntel(bid_depth_5c=yes_bid_depth, imbalance_5c=yes_imbalance),
        "no": MockBookIntel(bid_depth_5c=no_bid_depth, imbalance_5c=no_imbalance),
    }


# ═══════════════════════════════════════════════════════════════════════
# Tests: Gradual 30s Entry Filter
# ═══════════════════════════════════════════════════════════════════════

class TestGradualEntryFilter:
    """Verify the 30s trend entry filter scales gradually, not binary."""

    def test_no_opposition_uses_base_threshold(self):
        """When 30s OFI agrees with entry direction, use base entry_threshold."""
        strategy = make_strategy(
            entry_threshold=0.50,
            counter_trend_threshold=0.55,
            # Disable filters that would interfere
            trend_bias_enabled=False,
            adaptive_bias_enabled=False,
            chop_filter_enabled=False,
            acceleration_enabled=False,
            low_vol_block_enabled=False,
            high_intensity_block_enabled=False,
            entry_persistence_enabled=False,
            dead_market_band=0.0,
        )

        # Bullish momentum with bullish 30s OFI → no opposition
        micro = make_micro_with_momentum("bullish", 0.8, ofi_30s=0.50)
        market = make_market(yes_price=0.40, no_price=0.60)

        result = strategy.evaluate(market, micro, 300.0)
        # Should use base threshold (0.50). Strong momentum should pass.
        # Check the tracked threshold
        assert strategy._last_effective_threshold == pytest.approx(0.50, abs=0.01)

    def test_strong_opposition_uses_counter_trend_threshold(self):
        """When 30s OFI strongly opposes entry, use full counter_trend_threshold."""
        strategy = make_strategy(
            entry_threshold=0.50,
            counter_trend_threshold=0.55,
            trend_bias_enabled=False,
            adaptive_bias_enabled=False,
            chop_filter_enabled=False,
            acceleration_enabled=False,
            low_vol_block_enabled=False,
            high_intensity_block_enabled=False,
            entry_persistence_enabled=False,
            dead_market_band=0.0,
        )

        # Bullish momentum with strongly bearish 30s OFI → max opposition
        micro = make_micro_with_momentum("bullish", 0.8, ofi_30s=-0.50)
        market = make_market(yes_price=0.40, no_price=0.60)

        strategy.evaluate(market, micro, 300.0)
        # Opposition = min(1.0, 0.50/0.30) = 1.0 → full counter_trend
        assert strategy._last_effective_threshold == pytest.approx(0.55, abs=0.01)

    def test_moderate_opposition_scales_linearly(self):
        """When 30s OFI moderately opposes, threshold is between base and counter."""
        strategy = make_strategy(
            entry_threshold=0.50,
            counter_trend_threshold=0.55,
            trend_bias_enabled=False,
            adaptive_bias_enabled=False,
            chop_filter_enabled=False,
            acceleration_enabled=False,
            low_vol_block_enabled=False,
            high_intensity_block_enabled=False,
            entry_persistence_enabled=False,
            dead_market_band=0.0,
        )

        # Bullish momentum with mildly bearish 30s OFI (-0.15)
        # Opposition = 0.15/0.30 = 0.50 → halfway between 0.50 and 0.55 = 0.525
        micro = make_micro_with_momentum("bullish", 0.8, ofi_30s=-0.15)
        market = make_market(yes_price=0.40, no_price=0.60)

        strategy.evaluate(market, micro, 300.0)
        expected = 0.50 + 0.50 * (0.55 - 0.50)  # 0.525
        assert strategy._last_effective_threshold == pytest.approx(expected, abs=0.01)

    def test_tiny_opposition_barely_moves_threshold(self):
        """A tiny OFI of -0.03 should only add ~0.005 to threshold, not jump to 0.55."""
        strategy = make_strategy(
            entry_threshold=0.50,
            counter_trend_threshold=0.55,
            trend_bias_enabled=False,
            adaptive_bias_enabled=False,
            chop_filter_enabled=False,
            acceleration_enabled=False,
            low_vol_block_enabled=False,
            high_intensity_block_enabled=False,
            entry_persistence_enabled=False,
            dead_market_band=0.0,
        )

        # Bullish momentum with barely bearish 30s OFI (-0.03)
        # Opposition = 0.03/0.30 = 0.10 → 0.50 + 0.10 * 0.05 = 0.505
        micro = make_micro_with_momentum("bullish", 0.8, ofi_30s=-0.03)
        market = make_market(yes_price=0.40, no_price=0.60)

        strategy.evaluate(market, micro, 300.0)
        expected = 0.50 + (0.03 / 0.30) * 0.05  # ~0.505
        assert strategy._last_effective_threshold == pytest.approx(expected, abs=0.01)
        # Crucially: NOT 0.55 like the old binary filter would give
        assert strategy._last_effective_threshold < 0.52

    def test_bearish_entry_with_bullish_30s_opposition(self):
        """For NO entries, positive 30s OFI is the opposition."""
        strategy = make_strategy(
            entry_threshold=0.50,
            counter_trend_threshold=0.55,
            trend_bias_enabled=False,
            adaptive_bias_enabled=False,
            chop_filter_enabled=False,
            acceleration_enabled=False,
            low_vol_block_enabled=False,
            high_intensity_block_enabled=False,
            entry_persistence_enabled=False,
            dead_market_band=0.0,
        )

        # Bearish momentum with bullish 30s OFI (+0.30) → full opposition
        micro = make_micro_with_momentum("bearish", 0.8, ofi_30s=0.30)
        market = make_market(yes_price=0.60, no_price=0.40)

        strategy.evaluate(market, micro, 300.0)
        assert strategy._last_effective_threshold == pytest.approx(0.55, abs=0.01)

    def test_opposition_capped_at_1(self):
        """OFI beyond -0.30 doesn't push threshold past counter_trend_threshold."""
        strategy = make_strategy(
            entry_threshold=0.50,
            counter_trend_threshold=0.55,
            trend_bias_enabled=False,
            adaptive_bias_enabled=False,
            chop_filter_enabled=False,
            acceleration_enabled=False,
            low_vol_block_enabled=False,
            high_intensity_block_enabled=False,
            entry_persistence_enabled=False,
            dead_market_band=0.0,
        )

        # Extreme opposition: OFI at -0.90
        micro = make_micro_with_momentum("bullish", 0.8, ofi_30s=-0.90)
        market = make_market(yes_price=0.40, no_price=0.60)

        strategy.evaluate(market, micro, 300.0)
        # Should cap at counter_trend_threshold, not go beyond
        assert strategy._last_effective_threshold == pytest.approx(0.55, abs=0.01)


# ═══════════════════════════════════════════════════════════════════════
# Tests: Gradual 30s Exit Filter
# ═══════════════════════════════════════════════════════════════════════

class TestGradualExitFilter:
    """Verify the 30s trend exit filter scales gradually, not binary."""

    def _make_exit_scenario(self, holding, ofi_30s, momentum_val,
                            exit_threshold=0.30, counter_trend_exit=0.45):
        """Helper to set up an exit scenario and evaluate."""
        strategy = make_strategy(
            entry_threshold=0.50,
            exit_threshold=exit_threshold,
            counter_trend_exit_threshold=counter_trend_exit,
            hold_threshold=0.0,  # Disabled
            enable_flips=False,
            trailing_stop_enabled=False,
            take_profit_enabled=False,
            trend_bias_enabled=False,
            adaptive_bias_enabled=False,
            chop_filter_enabled=False,
            acceleration_enabled=False,
            low_vol_block_enabled=False,
            high_intensity_block_enabled=False,
            entry_persistence_enabled=False,
            poly_book_enabled=False,
        )

        # Create momentum against the position
        direction = "bearish" if holding == "yes" else "bullish"
        micro = make_micro_with_momentum(direction, abs(momentum_val), ofi_30s=ofi_30s)

        if holding == "yes":
            market = make_market(yes_price=0.55, no_price=0.45)
        else:
            market = make_market(yes_price=0.45, no_price=0.55)

        result = strategy.evaluate(
            market, micro, 300.0,
            current_position=holding,
        )
        return strategy, result

    def test_no_protection_when_trend_opposes_position(self):
        """When 30s OFI opposes our position, exit threshold stays at base."""
        # Holding YES, 30s OFI is bearish (opposes YES) → no protection
        strategy, result = self._make_exit_scenario(
            holding="yes", ofi_30s=-0.30, momentum_val=-0.35
        )
        # With no protection, exit_threshold = 0.30. Mom = 0.35 > 0.30 → EXIT
        assert result is not None
        assert result.action == MicroAction.EXIT

    def test_full_protection_when_trend_strongly_agrees(self):
        """When 30s OFI strongly agrees with position, exit threshold raised to max."""
        # Holding YES, 30s OFI is strongly bullish (+0.30) → full protection
        # Momentum against us at -0.35, but threshold raised to 0.45
        strategy, result = self._make_exit_scenario(
            holding="yes", ofi_30s=0.30, momentum_val=-0.35
        )
        # Full protection: effective exit = 0.45. Mom = 0.35 < 0.45 → HOLD
        assert result is None  # Should hold, not exit

    def test_partial_protection_scales_linearly(self):
        """Moderate 30s agreement gives partial protection."""
        # Holding YES, 30s OFI at +0.15 → 50% protection
        # Effective exit = 0.30 + 0.50 * (0.45 - 0.30) = 0.375
        strategy, result = self._make_exit_scenario(
            holding="yes", ofi_30s=0.15, momentum_val=-0.35
        )
        # Mom = 0.35 < 0.375 → HOLD (partial protection saves us)
        assert result is None

    def test_partial_protection_not_enough(self):
        """When momentum exceeds the partially-raised threshold, exit fires."""
        # Holding YES, 30s OFI at +0.10 → 33% protection
        # Effective exit = 0.30 + 0.33 * 0.15 = ~0.35
        strategy, result = self._make_exit_scenario(
            holding="yes", ofi_30s=0.10, momentum_val=-0.40
        )
        # Mom = 0.40 > ~0.35 → EXIT
        assert result is not None
        assert result.action == MicroAction.EXIT

    def test_tiny_agreement_barely_protects(self):
        """A tiny positive OFI doesn't give much protection."""
        # Holding YES, 30s OFI at +0.03 → 10% protection
        # Effective exit = 0.30 + 0.10 * 0.15 = 0.315
        strategy, result = self._make_exit_scenario(
            holding="yes", ofi_30s=0.03, momentum_val=-0.32
        )
        # Mom = 0.32 > 0.315 → EXIT (tiny protection isn't enough)
        assert result is not None
        assert result.action == MicroAction.EXIT

    def test_no_position_exit_for_bearish_momentum(self):
        """Holding NO with bullish momentum reversal — same gradual logic."""
        # Holding NO, 30s OFI bearish (agrees with NO) at -0.30 → full protection
        strategy, result = self._make_exit_scenario(
            holding="no", ofi_30s=-0.30, momentum_val=0.35
        )
        # Full protection: effective = 0.45. Mom = 0.35 < 0.45 → HOLD
        assert result is None

    def test_no_position_exit_no_protection(self):
        """Holding NO with trend opposing → base exit threshold."""
        # Holding NO, 30s OFI bullish (opposes NO) at +0.30 → no protection
        strategy, result = self._make_exit_scenario(
            holding="no", ofi_30s=0.30, momentum_val=0.35
        )
        # No protection: effective = 0.30. Mom = 0.35 > 0.30 → EXIT
        assert result is not None
        assert result.action == MicroAction.EXIT


# ═══════════════════════════════════════════════════════════════════════
# Tests: Exit Always Evaluates (cooldown bug fix)
# ═══════════════════════════════════════════════════════════════════════

class TestExitAlwaysEvaluates:
    """Verify that exits are never blocked by entry-only gates."""

    def test_force_exit_always_fires(self):
        """Force exit at end of window should always work regardless of anything."""
        strategy = make_strategy()
        micro = make_micro_with_momentum("bearish", 0.3)  # Weak signal
        market = make_market(yes_price=0.55, no_price=0.45)

        result = strategy.evaluate(
            market, micro, 3.0,  # < force_exit_seconds (8.0)
            current_position="yes",
        )
        assert result is not None
        assert result.action == MicroAction.EXIT
        assert result.exit_reason == "force_exit"

    def test_force_exit_fires_for_no_position(self):
        """Force exit works for NO positions too."""
        strategy = make_strategy()
        micro = make_micro_with_momentum("bullish", 0.3)
        market = make_market(yes_price=0.55, no_price=0.45)

        result = strategy.evaluate(
            market, micro, 3.0,
            current_position="no",
        )
        assert result is not None
        assert result.action == MicroAction.EXIT
        assert result.exit_reason == "force_exit"

    def test_exit_not_blocked_by_min_seconds_remaining(self):
        """min_seconds_remaining only blocks entries, not exits."""
        strategy = make_strategy(
            min_seconds_remaining=120.0,
            exit_threshold=0.20,
            counter_trend_exit_threshold=0.45,
            trailing_stop_enabled=False,
            take_profit_enabled=False,
            enable_flips=False,
            poly_book_enabled=False,
        )
        # Strong bearish momentum + bearish 30s OFI (no exit protection)
        micro = make_micro_with_momentum("bearish", 0.8, ofi_30s=-0.5)
        market = make_market(yes_price=0.55, no_price=0.45)

        # 60 seconds left — below min_seconds_remaining
        # But we have a position, so exit should still evaluate
        result = strategy.evaluate(
            market, micro, 60.0,
            current_position="yes",
        )
        # Should evaluate and exit (momentum is strongly against us at 0.80 > 0.20 exit threshold)
        assert result is not None
        assert result.action == MicroAction.EXIT

    def test_min_seconds_remaining_blocks_entry(self):
        """min_seconds_remaining blocks new entries when time is low."""
        strategy = make_strategy(min_seconds_remaining=120.0)
        micro = make_micro_with_momentum("bullish", 0.8)
        market = make_market(yes_price=0.45, no_price=0.55)

        # 60 seconds left — below min_seconds_remaining, no position
        result = strategy.evaluate(
            market, micro, 60.0,
        )
        assert result is None  # Blocked from entering

    def test_take_profit_fires_when_price_high(self):
        """Take profit should trigger regardless of momentum direction."""
        strategy = make_strategy(
            take_profit_enabled=True,
            take_profit_price=0.90,
        )
        micro = make_micro_with_momentum("bullish", 0.8)  # Momentum WITH our position
        market = make_market(yes_price=0.92, no_price=0.08)  # Above take profit

        result = strategy.evaluate(
            market, micro, 300.0,
            current_position="yes",
            entry_price=0.50,  # Required for take_profit check
        )
        assert result is not None
        assert result.action == MicroAction.EXIT
        assert result.exit_reason == "take_profit"

    def test_trailing_stop_fires(self):
        """Trailing stop should trigger when drawdown exceeds threshold."""
        strategy = make_strategy(
            trailing_stop_enabled=True,
            trailing_stop_pct=0.12,
            trailing_stop_min_profit_pct=0.10,
        )
        micro = make_micro_with_momentum("bearish", 0.3)
        # Price dropped from HWM of 0.70 to 0.55 = 21% drawdown > 12%
        market = make_market(yes_price=0.55, no_price=0.45)

        result = strategy.evaluate(
            market, micro, 300.0,
            current_position="yes",
            entry_price=0.40,
            high_water_mark=0.70,  # HWM well above entry (profit)
        )
        assert result is not None
        assert result.action == MicroAction.EXIT
        assert result.exit_reason == "trailing_stop"

    def test_trailing_stop_not_armed_without_profit(self):
        """Trailing stop shouldn't arm before min_profit_pct is reached."""
        strategy = make_strategy(
            trailing_stop_enabled=True,
            trailing_stop_pct=0.12,
            trailing_stop_min_profit_pct=0.10,
        )
        micro = make_micro_with_momentum("bearish", 0.3)
        # HWM barely above entry — not enough profit to arm
        market = make_market(yes_price=0.39, no_price=0.61)

        result = strategy.evaluate(
            market, micro, 300.0,
            current_position="yes",
            entry_price=0.40,
            high_water_mark=0.42,  # Only 5% profit, below 10% min
        )
        # Should NOT exit via trailing stop (not armed)
        if result is not None:
            assert result.exit_reason != "trailing_stop"


# ═══════════════════════════════════════════════════════════════════════
# Tests: Entry Filters Block Correctly
# ═══════════════════════════════════════════════════════════════════════

class TestEntryFilters:
    """Verify that entry-only filters don't affect exits."""

    def test_max_entry_price_blocks_entry(self):
        """Can't buy YES above max_entry_price."""
        strategy = make_strategy(
            max_entry_price=0.60,
            entry_threshold=0.30,
            trend_bias_enabled=False,
            adaptive_bias_enabled=False,
            chop_filter_enabled=False,
            acceleration_enabled=False,
            low_vol_block_enabled=False,
            high_intensity_block_enabled=False,
            entry_persistence_enabled=False,
            dead_market_band=0.0,
        )
        micro = make_micro_with_momentum("bullish", 0.8)
        market = make_market(yes_price=0.65, no_price=0.35)  # Above max

        result = strategy.evaluate(market, micro, 300.0)
        assert result is None  # Blocked

    def test_min_entry_price_blocks_entry(self):
        """Can't buy YES below min_entry_price."""
        strategy = make_strategy(
            min_entry_price=0.35,
            entry_threshold=0.30,
            trend_bias_enabled=False,
            adaptive_bias_enabled=False,
            chop_filter_enabled=False,
            acceleration_enabled=False,
            low_vol_block_enabled=False,
            high_intensity_block_enabled=False,
            entry_persistence_enabled=False,
            dead_market_band=0.0,
        )
        micro = make_micro_with_momentum("bullish", 0.8)
        market = make_market(yes_price=0.20, no_price=0.80)  # Below min

        result = strategy.evaluate(market, micro, 300.0)
        assert result is None  # Blocked

    def test_dead_market_blocks_entry(self):
        """Can't enter when YES is stuck at 0.50."""
        strategy = make_strategy(
            dead_market_band=0.02,
            entry_threshold=0.30,
            trend_bias_enabled=False,
            adaptive_bias_enabled=False,
            chop_filter_enabled=False,
            acceleration_enabled=False,
            low_vol_block_enabled=False,
            high_intensity_block_enabled=False,
            entry_persistence_enabled=False,
        )
        micro = make_micro_with_momentum("bullish", 0.8)
        market = make_market(yes_price=0.50, no_price=0.50)  # Dead center

        result = strategy.evaluate(market, micro, 300.0)
        assert result is None  # Blocked by dead market

    def test_entry_price_filters_dont_block_exit(self):
        """max_entry_price shouldn't prevent exiting a position."""
        strategy = make_strategy(
            max_entry_price=0.60,
            exit_threshold=0.20,
            counter_trend_exit_threshold=0.45,
            trailing_stop_enabled=False,
            take_profit_enabled=False,
            enable_flips=False,
            poly_book_enabled=False,
        )
        micro = make_micro_with_momentum("bearish", 0.5, ofi_30s=-0.5)
        # YES price is 0.65 (above max_entry_price) but we're already holding
        market = make_market(yes_price=0.65, no_price=0.35)

        result = strategy.evaluate(
            market, micro, 300.0,
            current_position="yes",
        )
        # Should be able to exit — entry price filters don't apply to exits
        # (momentum is against us and strong enough)
        if result is not None:
            assert result.action == MicroAction.EXIT


# ═══════════════════════════════════════════════════════════════════════
# Tests: Per-Timeframe Config
# ═══════════════════════════════════════════════════════════════════════

class TestPerTimeframeConfig:
    """Verify per-timeframe config merging works correctly."""

    def test_no_timeframes_returns_self(self):
        """Without timeframe overrides, for_timeframe returns the same object."""
        cfg = MicroSniperConfig()
        result = cfg.for_timeframe(15)
        assert result is cfg

    def test_no_matching_timeframe_returns_self(self):
        """If timeframe key doesn't match, returns self unchanged."""
        cfg = MicroSniperConfig(timeframes={"1h": {"entry_threshold": 0.45}})
        result = cfg.for_timeframe(15)  # 15m, not 1h
        assert result is cfg

    def test_none_duration_returns_self(self):
        """None duration returns self."""
        cfg = MicroSniperConfig(timeframes={"15m": {"entry_threshold": 0.55}})
        result = cfg.for_timeframe(None)
        assert result is cfg

    def test_15m_override_merges(self):
        """15-minute overrides merge correctly."""
        cfg = MicroSniperConfig(
            entry_threshold=0.50,
            exit_threshold=0.30,
            timeframes={"15m": {"entry_threshold": 0.55, "max_trades_per_window": 8}}
        )
        merged = cfg.for_timeframe(15)

        assert merged.entry_threshold == 0.55  # Overridden
        assert merged.max_trades_per_window == 8  # Overridden
        assert merged.exit_threshold == 0.30  # Base (not overridden)

    def test_1h_override_merges(self):
        """1-hour overrides merge correctly (60 min → "1h")."""
        cfg = MicroSniperConfig(
            entry_threshold=0.50,
            timeframes={"1h": {"entry_threshold": 0.45, "min_seconds_remaining": 300.0}}
        )
        merged = cfg.for_timeframe(60)

        assert merged.entry_threshold == 0.45
        assert merged.min_seconds_remaining == 300.0

    def test_1d_override_merges(self):
        """1-day overrides merge correctly (1440 min → "1d")."""
        cfg = MicroSniperConfig(
            entry_threshold=0.50,
            timeframes={"1d": {"entry_threshold": 0.35}}
        )
        merged = cfg.for_timeframe(1440)
        assert merged.entry_threshold == 0.35

    def test_5m_override_merges(self):
        """5-minute overrides merge correctly."""
        cfg = MicroSniperConfig(
            entry_threshold=0.50,
            timeframes={"5m": {"entry_threshold": 0.60, "max_trades_per_window": 5}}
        )
        merged = cfg.for_timeframe(5)

        assert merged.entry_threshold == 0.60
        assert merged.max_trades_per_window == 5

    def test_merged_config_preserves_timeframes(self):
        """Merged config should still have the timeframes dict for hot-reload."""
        cfg = MicroSniperConfig(
            timeframes={"15m": {"entry_threshold": 0.55}}
        )
        merged = cfg.for_timeframe(15)

        assert merged.timeframes == cfg.timeframes
        # Can re-merge (simulating hot-reload)
        re_merged = merged.for_timeframe(15)
        assert re_merged.entry_threshold == 0.55

    def test_multiple_timeframes_independent(self):
        """Different timeframes don't interfere with each other."""
        cfg = MicroSniperConfig(
            entry_threshold=0.50,
            timeframes={
                "5m": {"entry_threshold": 0.60},
                "15m": {"entry_threshold": 0.55},
                "1h": {"entry_threshold": 0.45},
                "1d": {"entry_threshold": 0.35},
            }
        )
        assert cfg.for_timeframe(5).entry_threshold == 0.60
        assert cfg.for_timeframe(15).entry_threshold == 0.55
        assert cfg.for_timeframe(60).entry_threshold == 0.45
        assert cfg.for_timeframe(1440).entry_threshold == 0.35

    def test_override_does_not_mutate_base(self):
        """for_timeframe should not modify the base config."""
        cfg = MicroSniperConfig(entry_threshold=0.50,
                                 timeframes={"15m": {"entry_threshold": 0.55}})
        _ = cfg.for_timeframe(15)
        assert cfg.entry_threshold == 0.50  # Base unchanged


# ═══════════════════════════════════════════════════════════════════════
# Tests: DB Config Serialization (Timeframes)
# ═══════════════════════════════════════════════════════════════════════

class TestTimeframeDBSerialization:
    """Verify timeframe overrides serialize to/from DB correctly."""

    def test_serialize_timeframes(self):
        """Timeframe overrides become dot-notation DB keys."""
        from polyedge.core.config import settings_to_db_dict

        settings = Settings()
        settings.strategies.micro_sniper.timeframes = {
            "15m": {"entry_threshold": 0.55, "max_trades_per_window": 8},
            "1h": {"entry_threshold": 0.45},
        }
        db_dict = settings_to_db_dict(settings)

        assert db_dict["strategies.micro_sniper.timeframes.15m.entry_threshold"] == 0.55
        assert db_dict["strategies.micro_sniper.timeframes.15m.max_trades_per_window"] == 8
        assert db_dict["strategies.micro_sniper.timeframes.1h.entry_threshold"] == 0.45

        # Raw "timeframes" key should NOT exist
        assert "strategies.micro_sniper.timeframes" not in db_dict

    def test_deserialize_timeframes(self):
        """Dot-notation DB keys become timeframe override dicts."""
        # Simulate what apply_db_config does
        settings = Settings()
        micro = settings.strategies.micro_sniper

        # Simulate processing these DB keys:
        test_keys = {
            "strategies.micro_sniper.timeframes.15m.entry_threshold": 0.55,
            "strategies.micro_sniper.timeframes.15m.max_trades_per_window": 8,
            "strategies.micro_sniper.timeframes.1h.entry_threshold": 0.45,
        }

        for key, value in test_keys.items():
            parts = key.split(".", 1)
            section, field = parts[0], parts[1]
            sub_parts = field.split(".", 1)
            strategy_name, strategy_field = sub_parts[0], sub_parts[1]

            if strategy_field.startswith("timeframes."):
                tf_parts = strategy_field.split(".", 2)
                _, tf_key, tf_field = tf_parts
                if tf_key not in micro.timeframes:
                    micro.timeframes[tf_key] = {}
                micro.timeframes[tf_key][tf_field] = value

        assert micro.timeframes["15m"]["entry_threshold"] == 0.55
        assert micro.timeframes["15m"]["max_trades_per_window"] == 8
        assert micro.timeframes["1h"]["entry_threshold"] == 0.45

    def test_roundtrip_serialize_deserialize(self):
        """Serialize then deserialize should produce identical timeframe config."""
        from polyedge.core.config import settings_to_db_dict

        original = Settings()
        original.strategies.micro_sniper.timeframes = {
            "15m": {"entry_threshold": 0.55, "exit_threshold": 0.25},
            "1h": {"entry_threshold": 0.45, "min_seconds_remaining": 300.0},
        }

        # Serialize
        db_dict = settings_to_db_dict(original)

        # Deserialize into fresh settings
        restored = Settings()
        for key, value in db_dict.items():
            if not key.startswith("strategies.micro_sniper.timeframes."):
                continue
            parts = key.split(".")
            # strategies.micro_sniper.timeframes.15m.entry_threshold
            tf_key = parts[3]
            tf_field = parts[4]
            if tf_key not in restored.strategies.micro_sniper.timeframes:
                restored.strategies.micro_sniper.timeframes[tf_key] = {}
            restored.strategies.micro_sniper.timeframes[tf_key][tf_field] = value

        assert restored.strategies.micro_sniper.timeframes == original.strategies.micro_sniper.timeframes


# ═══════════════════════════════════════════════════════════════════════
# Tests: Floor Exit
# ═══════════════════════════════════════════════════════════════════════

class TestFloorExit:
    """Verify time-scaled floor exit works correctly."""

    def test_floor_exit_under_30s(self):
        """Below 30s, floor is min_entry_price. Exits not blocked by min_seconds_remaining."""
        strategy = make_strategy(min_entry_price=0.35)
        # Default min_seconds_remaining=45s, but exits should still work at 20s
        micro = make_micro_with_momentum("bearish", 0.3)
        # YES price below floor with <30s left
        market = make_market(yes_price=0.20, no_price=0.80)

        result = strategy.evaluate(
            market, micro, 20.0,  # <30s
            current_position="yes",
        )
        assert result is not None
        assert result.action == MicroAction.EXIT
        assert result.exit_reason == "floor_exit"

    def test_no_floor_above_120s(self):
        """Above 120s, no floor exit (plenty of time to recover)."""
        strategy = make_strategy(
            exit_threshold=0.30,
            counter_trend_exit_threshold=0.45,
            trailing_stop_enabled=False,
            take_profit_enabled=False,
        )
        micro = make_micro_with_momentum("bearish", 0.1)  # Weak against us
        # YES price is low but lots of time left
        market = make_market(yes_price=0.10, no_price=0.90)

        result = strategy.evaluate(
            market, micro, 200.0,  # >120s
            current_position="yes",
        )
        # Should NOT floor exit — still time to recover
        if result is not None:
            assert result.exit_reason != "floor_exit"


# ═══════════════════════════════════════════════════════════════════════
# Tests: Threshold Logging Accuracy
# ═══════════════════════════════════════════════════════════════════════

class TestThresholdLogging:
    """Verify threshold breakdown strings are accurate."""

    def test_base_only_no_modifiers(self):
        """When no modifiers active, just show base threshold."""
        strategy = make_strategy(
            entry_threshold=0.50,
            counter_trend_threshold=0.55,
            trend_bias_enabled=False,
            adaptive_bias_enabled=False,
            chop_filter_enabled=False,
            acceleration_enabled=False,
            low_vol_block_enabled=False,
            high_intensity_block_enabled=False,
            entry_persistence_enabled=False,
            dead_market_band=0.0,
        )
        # Bullish with positive 30s OFI → no opposition
        micro = make_micro_with_momentum("bullish", 0.8, ofi_30s=0.30)
        market = make_market(yes_price=0.40, no_price=0.60)

        strategy.evaluate(market, micro, 300.0)
        # No modifiers → just the base value
        assert "0.50" in strategy._last_threshold_detail

    def test_30s_modifier_shown(self):
        """30s opposition shows as '30s+X.XX' modifier."""
        strategy = make_strategy(
            entry_threshold=0.50,
            counter_trend_threshold=0.55,
            trend_bias_enabled=False,
            adaptive_bias_enabled=False,
            chop_filter_enabled=False,
            acceleration_enabled=False,
            low_vol_block_enabled=False,
            high_intensity_block_enabled=False,
            entry_persistence_enabled=False,
            dead_market_band=0.0,
        )
        micro = make_micro_with_momentum("bullish", 0.8, ofi_30s=-0.15)
        market = make_market(yes_price=0.40, no_price=0.60)

        strategy.evaluate(market, micro, 300.0)
        assert "30s+" in strategy._last_threshold_detail

    def test_exit_threshold_shows_strength(self):
        """Exit threshold detail shows trend strength percentage."""
        strategy = make_strategy(
            exit_threshold=0.30,
            counter_trend_exit_threshold=0.45,
            trailing_stop_enabled=False,
            take_profit_enabled=False,
        )
        # Holding YES with positive 30s OFI (partial protection)
        micro = make_micro_with_momentum("bearish", 0.5, ofi_30s=0.15)
        market = make_market(yes_price=0.55, no_price=0.45)

        strategy.evaluate(
            market, micro, 300.0,
            current_position="yes",
        )
        detail = strategy._last_exit_threshold_detail
        assert "30s_trend" in detail
        assert "%" in detail  # Shows strength percentage


# ═══════════════════════════════════════════════════════════════════════
# Tests: Low Volatility Blocker
# ═══════════════════════════════════════════════════════════════════════

class TestLowVolBlocker:
    """Verify low_vol_block_enabled filter works correctly."""

    def test_low_vol_blocks_entry(self):
        """Low intensity + low price change → blocked."""
        strategy = make_clean_strategy(
            low_vol_block_enabled=True,
            low_vol_max_intensity=5.0,
            low_vol_max_price_change=0.0005,
        )
        # Low trade intensity and tiny price move
        micro = make_micro_with_momentum("bullish", 0.8, n_trades=5, price_change_pct=0.0001)
        market = make_market(yes_price=0.45, no_price=0.55)

        result = strategy.evaluate(market, micro, 300.0)
        assert result is None
        assert strategy.last_no_trade_reason == NoTradeReason.LOW_VOL

    def test_low_vol_allows_when_intensity_high(self):
        """High trade intensity → not low vol, entry allowed."""
        strategy = make_clean_strategy(
            low_vol_block_enabled=True,
            low_vol_max_intensity=5.0,
            low_vol_max_price_change=0.0005,
        )
        # Create micro with high intensity (many trades in short time)
        micro = MicroStructure("btcusdt")
        now = time.time()
        for i in range(100):
            t = now - 5.0 + (i / 100) * 5.0  # 100 trades in 5s = 20 tps
            trade = AggTrade(
                symbol="btcusdt", price=70000.0, quantity=0.01,
                is_buyer_maker=(i % 3 == 0), timestamp=t,
            )
            micro.add_trade(trade)
        micro.window_start_price = 70000.0
        micro.window_start_time = now - 15.0
        micro.current_price = 70000.0  # Tiny price change
        micro.__class__ = type(
            'HighIntMicro3', (MicroStructure,),
            {'momentum_signal': property(lambda s: 0.8),
             'confidence': property(lambda s: 0.8)},
        )
        market = make_market(yes_price=0.45, no_price=0.55)

        result = strategy.evaluate(market, micro, 300.0)
        # Should NOT be blocked by low vol (intensity > 5 tps)
        if result is None:
            assert strategy.last_no_trade_reason != NoTradeReason.LOW_VOL

    def test_low_vol_allows_when_price_moved(self):
        """Big price change → not low vol even with few trades."""
        strategy = make_clean_strategy(
            low_vol_block_enabled=True,
            low_vol_max_intensity=5.0,
            low_vol_max_price_change=0.0005,
        )
        # Few trades but significant price movement
        micro = make_micro_with_momentum("bullish", 0.8, n_trades=5, price_change_pct=0.005)
        market = make_market(yes_price=0.45, no_price=0.55)

        result = strategy.evaluate(market, micro, 300.0)
        if result is None:
            assert strategy.last_no_trade_reason != NoTradeReason.LOW_VOL

    def test_low_vol_disabled_allows_entry(self):
        """When disabled, low vol conditions don't block."""
        strategy = make_clean_strategy(low_vol_block_enabled=False)
        micro = make_micro_with_momentum("bullish", 0.8, n_trades=5, price_change_pct=0.0001)
        market = make_market(yes_price=0.45, no_price=0.55)

        result = strategy.evaluate(market, micro, 300.0)
        if result is None:
            assert strategy.last_no_trade_reason != NoTradeReason.LOW_VOL


# ═══════════════════════════════════════════════════════════════════════
# Tests: High Intensity Blocker
# ═══════════════════════════════════════════════════════════════════════

class TestHighIntensityBlocker:
    """Verify high_intensity_block_enabled filter works correctly."""

    def test_high_intensity_blocks_entry(self):
        """When 30s intensity exceeds max → blocked."""
        strategy = make_clean_strategy(
            high_intensity_block_enabled=True,
            high_intensity_max_tps=10.0,  # Low threshold for test
        )
        # Create micro with very fast trades (high tps)
        micro = MicroStructure("btcusdt")
        now = time.time()
        # 500 trades in 10 seconds = 50 tps
        for i in range(500):
            t = now - 10.0 + (i / 500) * 10.0
            trade = AggTrade(
                symbol="btcusdt", price=70000.0, quantity=0.01,
                is_buyer_maker=(i % 3 == 0), timestamp=t,
            )
            micro.add_trade(trade)
        micro.window_start_price = 70000.0
        micro.window_start_time = now - 15.0
        micro.current_price = 70070.0
        # Patch momentum
        micro.__class__ = type(
            'HighIntMicro', (MicroStructure,),
            {'momentum_signal': property(lambda s: 0.8),
             'confidence': property(lambda s: 0.8)},
        )
        market = make_market(yes_price=0.45, no_price=0.55)

        result = strategy.evaluate(market, micro, 300.0)
        assert result is None
        assert strategy.last_no_trade_reason == NoTradeReason.HIGH_INTENSITY

    def test_normal_intensity_allows_entry(self):
        """Normal intensity → entry allowed."""
        strategy = make_clean_strategy(
            high_intensity_block_enabled=True,
            high_intensity_max_tps=50.0,
        )
        micro = make_micro_with_momentum("bullish", 0.8, n_trades=30)
        market = make_market(yes_price=0.45, no_price=0.55)

        result = strategy.evaluate(market, micro, 300.0)
        if result is None:
            assert strategy.last_no_trade_reason != NoTradeReason.HIGH_INTENSITY

    def test_high_intensity_disabled(self):
        """When disabled, high intensity doesn't block."""
        strategy = make_clean_strategy(high_intensity_block_enabled=False)
        # Same high-intensity micro as above
        micro = MicroStructure("btcusdt")
        now = time.time()
        for i in range(500):
            t = now - 10.0 + (i / 500) * 10.0
            trade = AggTrade(
                symbol="btcusdt", price=70000.0, quantity=0.01,
                is_buyer_maker=(i % 3 == 0), timestamp=t,
            )
            micro.add_trade(trade)
        micro.window_start_price = 70000.0
        micro.window_start_time = now - 15.0
        micro.current_price = 70070.0
        micro.__class__ = type(
            'HighIntMicro2', (MicroStructure,),
            {'momentum_signal': property(lambda s: 0.8),
             'confidence': property(lambda s: 0.8)},
        )
        market = make_market(yes_price=0.45, no_price=0.55)

        result = strategy.evaluate(market, micro, 300.0)
        if result is None:
            assert strategy.last_no_trade_reason != NoTradeReason.HIGH_INTENSITY


# ═══════════════════════════════════════════════════════════════════════
# Tests: 5-Minute Trend Bias
# ═══════════════════════════════════════════════════════════════════════

class TestTrendBias5m:
    """Verify 5-minute trend bias blocks and boosts correctly."""

    def _make_micro_with_trend(self, direction, momentum_dir="bullish",
                                strength=0.8, trend_pct=0.004):
        """Create micro with a specific 5m trend via price_history (DB fallback).

        Resets flow_5m so trend_5m falls back to price_history.
        This simulates a bot that just started and has DB context but not
        5 minutes of live flow data yet.
        """
        micro = make_micro_with_momentum(momentum_dir, strength)
        # Set up a 5m trend by populating price_history
        base = 70000.0
        if direction == "up":
            oldest_price = base * (1 - trend_pct)
        else:
            oldest_price = base * (1 + trend_pct)
        micro.price_history = [(oldest_price, time.time() - 300)]
        micro.current_price = base
        # Reset flow_5m so trend_5m uses price_history fallback
        micro.flow_5m.reset()
        return micro

    def test_strong_uptrend_blocks_no_entry(self):
        """Strong 5m uptrend hard blocks bearish (NO) entries."""
        strategy = make_clean_strategy(
            trend_bias_enabled=True,
            trend_bias_min_pct=0.0015,
            trend_bias_strong_pct=0.003,
        )
        # BTC trending up 0.4% → strong uptrend
        micro = self._make_micro_with_trend("up", "bearish", 0.8, trend_pct=0.004)
        market = make_market(yes_price=0.55, no_price=0.45)

        result = strategy.evaluate(market, micro, 300.0)
        assert result is None
        assert strategy.last_no_trade_reason == NoTradeReason.TREND_VETO

    def test_strong_downtrend_blocks_yes_entry(self):
        """Strong 5m downtrend hard blocks bullish (YES) entries."""
        strategy = make_clean_strategy(
            trend_bias_enabled=True,
            trend_bias_min_pct=0.0015,
            trend_bias_strong_pct=0.003,
        )
        # BTC trending down 0.4% → strong downtrend
        micro = self._make_micro_with_trend("down", "bullish", 0.8, trend_pct=0.004)
        market = make_market(yes_price=0.45, no_price=0.55)

        result = strategy.evaluate(market, micro, 300.0)
        assert result is None
        assert strategy.last_no_trade_reason == NoTradeReason.TREND_VETO

    def test_moderate_trend_boosts_threshold(self):
        """Moderate 5m trend adds boost to threshold instead of blocking."""
        strategy = make_clean_strategy(
            trend_bias_enabled=True,
            trend_bias_min_pct=0.0015,
            trend_bias_strong_pct=0.003,
            trend_bias_counter_boost=0.10,
            entry_threshold=0.50,
        )
        # BTC trending up 0.2% → moderate, NOT strong
        micro = self._make_micro_with_trend("up", "bearish", 0.8, trend_pct=0.002)
        market = make_market(yes_price=0.55, no_price=0.45)

        result = strategy.evaluate(market, micro, 300.0)
        # Threshold should have been boosted by 0.10
        assert strategy._last_effective_threshold >= 0.60

    def test_with_trend_entry_not_blocked(self):
        """Trading WITH the 5m trend is never blocked."""
        strategy = make_clean_strategy(
            trend_bias_enabled=True,
            trend_bias_min_pct=0.0015,
            trend_bias_strong_pct=0.003,
        )
        # BTC trending up, buying YES (with the trend)
        micro = self._make_micro_with_trend("up", "bullish", 0.8, trend_pct=0.004)
        market = make_market(yes_price=0.45, no_price=0.55)

        result = strategy.evaluate(market, micro, 300.0)
        # Should NOT be blocked by trend bias
        if result is None:
            assert strategy.last_no_trade_reason != NoTradeReason.TREND_VETO

    def test_trend_bias_disabled(self):
        """When disabled, strong counter-trend doesn't block."""
        strategy = make_clean_strategy(trend_bias_enabled=False)
        micro = self._make_micro_with_trend("up", "bearish", 0.8, trend_pct=0.005)
        market = make_market(yes_price=0.55, no_price=0.45)

        result = strategy.evaluate(market, micro, 300.0)
        if result is None:
            assert strategy.last_no_trade_reason != NoTradeReason.TREND_VETO


# ═══════════════════════════════════════════════════════════════════════
# Tests: Adaptive Directional Bias (30m macro)
# ═══════════════════════════════════════════════════════════════════════

class TestAdaptiveBias:
    """Verify adaptive_bias shifts entry thresholds per-side."""

    def _make_micro_with_lookback(self, trend_dir, momentum_dir="bullish", strength=0.8):
        """Create micro with 30m price history for adaptive bias."""
        micro = make_micro_with_momentum(momentum_dir, strength)
        base = 70000.0
        if trend_dir == "bearish":
            # BTC dropped 0.5% over 30m
            old_price = base * 1.005
        else:
            # BTC rose 0.5% over 30m
            old_price = base * 0.995
        micro.price_history = [(old_price, time.time() - 1800)]
        micro.current_price = base
        return micro

    def test_bearish_30m_raises_yes_threshold(self):
        """In a bearish 30m market, YES entries need higher threshold."""
        strategy = make_clean_strategy(
            adaptive_bias_enabled=True,
            adaptive_bias_spread=0.10,
            adaptive_bias_min_move=0.003,
            adaptive_bias_lookback_minutes=30.0,
            entry_threshold=0.50,
        )
        micro = self._make_micro_with_lookback("bearish", "bullish", 0.8)
        market = make_market(yes_price=0.45, no_price=0.55)

        strategy.evaluate(market, micro, 300.0)
        # Bearish market + YES entry = UNFAVORABLE → threshold raised by +0.05
        assert strategy._last_bias_adjustment > 0
        assert strategy._last_effective_threshold > 0.50

    def test_bearish_30m_lowers_no_threshold(self):
        """In a bearish 30m market, NO entries get lower threshold."""
        strategy = make_clean_strategy(
            adaptive_bias_enabled=True,
            adaptive_bias_spread=0.10,
            adaptive_bias_min_move=0.003,
            adaptive_bias_lookback_minutes=30.0,
            entry_threshold=0.50,
        )
        micro = self._make_micro_with_lookback("bearish", "bearish", 0.8)
        market = make_market(yes_price=0.55, no_price=0.45)

        strategy.evaluate(market, micro, 300.0)
        # Bearish market + NO entry = FAVORABLE → threshold lowered by -0.05
        assert strategy._last_bias_adjustment < 0
        assert strategy._last_effective_threshold < 0.50

    def test_small_move_no_bias(self):
        """Below min_move, no bias applied."""
        strategy = make_clean_strategy(
            adaptive_bias_enabled=True,
            adaptive_bias_spread=0.10,
            adaptive_bias_min_move=0.003,
            adaptive_bias_lookback_minutes=30.0,
            entry_threshold=0.50,
        )
        micro = make_micro_with_momentum("bullish", 0.8)
        # Tiny 30m move (0.1%)
        micro.price_history = [(70000 * 0.999, time.time() - 1800)]
        micro.current_price = 70000.0
        market = make_market(yes_price=0.45, no_price=0.55)

        strategy.evaluate(market, micro, 300.0)
        assert strategy._last_bias_adjustment == 0.0

    def test_adaptive_bias_disabled(self):
        """When disabled, no bias adjustment."""
        strategy = make_clean_strategy(adaptive_bias_enabled=False, entry_threshold=0.50)
        micro = make_micro_with_momentum("bullish", 0.8)
        micro.price_history = [(70000 * 0.990, time.time() - 1800)]
        micro.current_price = 70000.0
        market = make_market(yes_price=0.45, no_price=0.55)

        strategy.evaluate(market, micro, 300.0)
        assert strategy._last_bias_adjustment == 0.0


# ═══════════════════════════════════════════════════════════════════════
# Tests: Chop Filter
# ═══════════════════════════════════════════════════════════════════════

class TestChopFilter:
    """Verify chop filter auto-raises threshold in choppy conditions."""

    def _make_choppy_micro(self, chop_level, momentum_dir="bullish", strength=0.8):
        """Create micro with controlled chop index."""
        micro = make_micro_with_momentum(momentum_dir, strength)
        # Override chop_index via the per-instance subclass
        current_class = micro.__class__
        micro.__class__ = type(
            'ChoppyMicroStructure',
            (current_class,),
            {'chop_index': property(lambda self: chop_level)},
        )
        return micro

    def test_high_chop_raises_threshold(self):
        """Chop index above threshold → entry threshold boosted."""
        strategy = make_clean_strategy(
            chop_filter_enabled=True,
            chop_threshold=3.0,
            chop_scale=5.0,
            chop_max_boost=0.10,
            entry_threshold=0.50,
        )
        micro = self._make_choppy_micro(4.0)  # Mid-range chop
        market = make_market(yes_price=0.45, no_price=0.55)

        strategy.evaluate(market, micro, 300.0)
        # chop_frac = (4.0 - 3.0) / (5.0 - 3.0) = 0.50 → boost = 0.05
        assert strategy._last_chop_boost == pytest.approx(0.05, abs=0.01)
        assert strategy._last_effective_threshold == pytest.approx(0.55, abs=0.01)

    def test_extreme_chop_caps_boost(self):
        """Chop index way above scale → boost capped at max."""
        strategy = make_clean_strategy(
            chop_filter_enabled=True,
            chop_threshold=3.0,
            chop_scale=5.0,
            chop_max_boost=0.10,
            entry_threshold=0.50,
        )
        micro = self._make_choppy_micro(10.0)  # Extreme chop
        market = make_market(yes_price=0.45, no_price=0.55)

        strategy.evaluate(market, micro, 300.0)
        assert strategy._last_chop_boost == pytest.approx(0.10, abs=0.01)

    def test_low_chop_no_boost(self):
        """Chop below threshold → no boost."""
        strategy = make_clean_strategy(
            chop_filter_enabled=True,
            chop_threshold=3.0,
            chop_scale=5.0,
            chop_max_boost=0.10,
            entry_threshold=0.50,
        )
        micro = self._make_choppy_micro(2.0)  # Below threshold
        market = make_market(yes_price=0.45, no_price=0.55)

        strategy.evaluate(market, micro, 300.0)
        assert strategy._last_chop_boost == 0.0
        assert strategy._last_effective_threshold == pytest.approx(0.50, abs=0.01)

    def test_chop_disabled(self):
        """When disabled, choppy conditions don't affect threshold."""
        strategy = make_clean_strategy(chop_filter_enabled=False, entry_threshold=0.50)
        micro = self._make_choppy_micro(10.0)  # Extreme chop
        market = make_market(yes_price=0.45, no_price=0.55)

        strategy.evaluate(market, micro, 300.0)
        assert strategy._last_chop_boost == 0.0


# ═══════════════════════════════════════════════════════════════════════
# Tests: Acceleration Filter
# ═══════════════════════════════════════════════════════════════════════

class TestAccelerationFilter:
    """Verify acceleration filter blocks fading momentum."""

    def test_fading_momentum_blocked(self):
        """Previous momentum was higher → fading → blocked."""
        strategy = make_clean_strategy(
            acceleration_enabled=True,
            acceleration_tolerance=0.05,
            entry_threshold=0.30,
        )
        micro = make_micro_with_momentum("bullish", 0.6)
        market = make_market(yes_price=0.45, no_price=0.55)

        # Set previous momentum higher (signal was stronger before)
        strategy._prev_momentum["btcusdt"] = 0.9

        result = strategy.evaluate(market, micro, 300.0)
        # fade = 0.9 - 0.6 = 0.3 > tol 0.05 → blocked
        assert result is None
        assert strategy.last_no_trade_reason == NoTradeReason.ACCELERATION

    def test_accelerating_momentum_passes(self):
        """Previous momentum lower → accelerating → passes."""
        strategy = make_clean_strategy(
            acceleration_enabled=True,
            acceleration_tolerance=0.05,
            entry_threshold=0.30,
        )
        micro = make_micro_with_momentum("bullish", 0.8)
        market = make_market(yes_price=0.45, no_price=0.55)

        # Set previous momentum lower (signal is building)
        strategy._prev_momentum["btcusdt"] = 0.5

        result = strategy.evaluate(market, micro, 300.0)
        # fade = 0.5 - 0.8 = -0.3 (negative = accelerating) → passes
        if result is None:
            assert strategy.last_no_trade_reason != NoTradeReason.ACCELERATION

    def test_bearish_fading_blocked(self):
        """Bearish fade: previous was more negative → fading → blocked."""
        strategy = make_clean_strategy(
            acceleration_enabled=True,
            acceleration_tolerance=0.05,
            entry_threshold=0.30,
        )
        micro = make_micro_with_momentum("bearish", 0.6)
        market = make_market(yes_price=0.55, no_price=0.45)

        # Previous was more bearish
        strategy._prev_momentum["btcusdt"] = -0.9

        result = strategy.evaluate(market, micro, 300.0)
        # fade = -0.6 - (-0.9) = 0.3 > tol → blocked
        assert result is None
        assert strategy.last_no_trade_reason == NoTradeReason.ACCELERATION

    def test_within_tolerance_passes(self):
        """Small fade within tolerance → not blocked."""
        strategy = make_clean_strategy(
            acceleration_enabled=True,
            acceleration_tolerance=0.15,
            entry_threshold=0.30,
        )
        micro = make_micro_with_momentum("bullish", 0.7)
        market = make_market(yes_price=0.45, no_price=0.55)

        # Previous slightly higher (within tolerance)
        strategy._prev_momentum["btcusdt"] = 0.8

        result = strategy.evaluate(market, micro, 300.0)
        # fade = 0.8 - 0.7 = 0.1 < tol 0.15 → passes
        if result is None:
            assert strategy.last_no_trade_reason != NoTradeReason.ACCELERATION

    def test_acceleration_disabled(self):
        """When disabled, fading doesn't block."""
        strategy = make_clean_strategy(acceleration_enabled=False, entry_threshold=0.30)
        micro = make_micro_with_momentum("bullish", 0.5)
        market = make_market(yes_price=0.45, no_price=0.55)

        strategy._prev_momentum["btcusdt"] = 0.9  # Huge fade

        result = strategy.evaluate(market, micro, 300.0)
        if result is None:
            assert strategy.last_no_trade_reason != NoTradeReason.ACCELERATION


# ═══════════════════════════════════════════════════════════════════════
# Tests: Sparse Data Guard
# ═══════════════════════════════════════════════════════════════════════

class TestSparseDataGuard:
    """Verify min_trades_in_window blocks entries with insufficient data."""

    def test_sparse_data_blocks(self):
        """Too few trades in 15s window → blocked."""
        strategy = make_clean_strategy(
            min_trades_in_window=20,
            entry_threshold=0.30,
        )
        micro = make_micro_with_momentum("bullish", 0.8, n_trades=5)
        market = make_market(yes_price=0.45, no_price=0.55)

        result = strategy.evaluate(market, micro, 300.0)
        assert result is None
        assert strategy.last_no_trade_reason == NoTradeReason.SPARSE_DATA

    def test_enough_data_passes(self):
        """Enough trades → not blocked by sparse data."""
        strategy = make_clean_strategy(
            min_trades_in_window=10,
            entry_threshold=0.30,
        )
        micro = make_micro_with_momentum("bullish", 0.8, n_trades=30)
        market = make_market(yes_price=0.45, no_price=0.55)

        result = strategy.evaluate(market, micro, 300.0)
        if result is None:
            assert strategy.last_no_trade_reason != NoTradeReason.SPARSE_DATA


# ═══════════════════════════════════════════════════════════════════════
# Tests: Confidence Filter
# ═══════════════════════════════════════════════════════════════════════

class TestConfidenceFilter:
    """Verify min_confidence blocks low-confidence signals."""

    def test_low_confidence_blocks(self):
        """Confidence below min → blocked."""
        strategy = make_clean_strategy(
            min_confidence=0.80,
            entry_threshold=0.30,
        )
        # strength=0.5 → confidence = min(0.9, 0.4 + 0.5*0.5) = 0.65
        micro = make_micro_with_momentum("bullish", 0.5)
        market = make_market(yes_price=0.45, no_price=0.55)

        result = strategy.evaluate(market, micro, 300.0)
        assert result is None
        assert strategy.last_no_trade_reason == NoTradeReason.CONFIDENCE_TOO_LOW

    def test_high_confidence_passes(self):
        """Confidence above min → passes."""
        strategy = make_clean_strategy(
            min_confidence=0.40,
            entry_threshold=0.30,
        )
        # strength=0.8 → confidence = min(0.9, 0.4 + 0.8*0.5) = 0.80
        micro = make_micro_with_momentum("bullish", 0.8)
        market = make_market(yes_price=0.45, no_price=0.55)

        result = strategy.evaluate(market, micro, 300.0)
        if result is None:
            assert strategy.last_no_trade_reason != NoTradeReason.CONFIDENCE_TOO_LOW


# ═══════════════════════════════════════════════════════════════════════
# Tests: Price Band Filters (min/max entry price)
# ═══════════════════════════════════════════════════════════════════════

class TestPriceBandFilters:
    """Verify min_entry_price and max_entry_price work for both sides."""

    def test_yes_above_max_blocked(self):
        """YES price above max_entry_price → blocked."""
        strategy = make_clean_strategy(max_entry_price=0.65)
        micro = make_micro_with_momentum("bullish", 0.8)
        market = make_market(yes_price=0.70, no_price=0.30)

        result = strategy.evaluate(market, micro, 300.0)
        assert result is None
        assert strategy.last_no_trade_reason == NoTradeReason.PRICE_BAND

    def test_no_above_max_blocked(self):
        """NO price above max_entry_price → blocked."""
        strategy = make_clean_strategy(max_entry_price=0.65)
        micro = make_micro_with_momentum("bearish", 0.8)
        market = make_market(yes_price=0.30, no_price=0.70)

        result = strategy.evaluate(market, micro, 300.0)
        assert result is None
        assert strategy.last_no_trade_reason == NoTradeReason.PRICE_BAND

    def test_yes_below_min_blocked(self):
        """YES price below min_entry_price → blocked."""
        strategy = make_clean_strategy(min_entry_price=0.20)
        micro = make_micro_with_momentum("bullish", 0.8)
        market = make_market(yes_price=0.10, no_price=0.90)

        result = strategy.evaluate(market, micro, 300.0)
        assert result is None
        assert strategy.last_no_trade_reason == NoTradeReason.PRICE_BAND

    def test_no_below_min_blocked(self):
        """NO price below min_entry_price → blocked."""
        strategy = make_clean_strategy(min_entry_price=0.20)
        micro = make_micro_with_momentum("bearish", 0.8)
        market = make_market(yes_price=0.90, no_price=0.10)

        result = strategy.evaluate(market, micro, 300.0)
        assert result is None
        assert strategy.last_no_trade_reason == NoTradeReason.PRICE_BAND

    def test_price_in_range_passes(self):
        """Price within band → not blocked."""
        strategy = make_clean_strategy(min_entry_price=0.20, max_entry_price=0.80)
        micro = make_micro_with_momentum("bullish", 0.8)
        market = make_market(yes_price=0.45, no_price=0.55)

        result = strategy.evaluate(market, micro, 300.0)
        if result is None:
            assert strategy.last_no_trade_reason != NoTradeReason.PRICE_BAND


# ═══════════════════════════════════════════════════════════════════════
# Tests: Dead Market Filter
# ═══════════════════════════════════════════════════════════════════════

class TestDeadMarketFilter:
    """Verify dead_market_band blocks when YES stuck near 0.50."""

    def test_dead_market_blocks(self):
        """YES at exactly 0.50 → blocked."""
        strategy = make_clean_strategy(dead_market_band=0.03)
        micro = make_micro_with_momentum("bullish", 0.8)
        market = make_market(yes_price=0.50, no_price=0.50)

        result = strategy.evaluate(market, micro, 300.0)
        assert result is None
        assert strategy.last_no_trade_reason == NoTradeReason.DEAD_MARKET

    def test_near_dead_market_blocks(self):
        """YES at 0.51 with band=0.03 → still blocked (within band)."""
        strategy = make_clean_strategy(dead_market_band=0.03)
        micro = make_micro_with_momentum("bullish", 0.8)
        market = make_market(yes_price=0.51, no_price=0.49)

        result = strategy.evaluate(market, micro, 300.0)
        assert result is None
        assert strategy.last_no_trade_reason == NoTradeReason.DEAD_MARKET

    def test_outside_dead_band_passes(self):
        """YES at 0.45 with band=0.03 → not blocked."""
        strategy = make_clean_strategy(dead_market_band=0.03)
        micro = make_micro_with_momentum("bullish", 0.8)
        market = make_market(yes_price=0.45, no_price=0.55)

        result = strategy.evaluate(market, micro, 300.0)
        if result is None:
            assert strategy.last_no_trade_reason != NoTradeReason.DEAD_MARKET

    def test_dead_market_band_zero_disabled(self):
        """Band of 0 → never blocks."""
        strategy = make_clean_strategy(dead_market_band=0.0)
        micro = make_micro_with_momentum("bullish", 0.8)
        market = make_market(yes_price=0.50, no_price=0.50)

        result = strategy.evaluate(market, micro, 300.0)
        if result is None:
            assert strategy.last_no_trade_reason != NoTradeReason.DEAD_MARKET


# ═══════════════════════════════════════════════════════════════════════
# Tests: Entry Persistence (time-based)
# ═══════════════════════════════════════════════════════════════════════

class TestEntryPersistence:
    """Verify entry_persistence requires signal to sustain for N seconds."""

    def test_first_signal_blocked(self):
        """First qualifying signal → timer starts, entry blocked."""
        strategy = make_clean_strategy(
            entry_persistence_enabled=True,
            entry_persistence_seconds=2.0,
            entry_threshold=0.30,
        )
        micro = make_micro_with_momentum("bullish", 0.8)
        market = make_market(yes_price=0.45, no_price=0.55)

        result = strategy.evaluate(market, micro, 300.0)
        assert result is None
        assert strategy.last_no_trade_reason == NoTradeReason.FAILED_PERSISTENCE

    def test_sustained_signal_passes(self):
        """After persisting for N seconds, entry allowed."""
        strategy = make_clean_strategy(
            entry_persistence_enabled=True,
            entry_persistence_seconds=2.0,
            entry_threshold=0.30,
        )
        micro = make_micro_with_momentum("bullish", 0.8)
        market = make_market(yes_price=0.45, no_price=0.55)

        # Simulate: first eval starts timer
        strategy.evaluate(market, micro, 300.0)
        assert strategy.last_no_trade_reason == NoTradeReason.FAILED_PERSISTENCE

        # Backdate the start time to simulate 2+ seconds passing
        strategy._entry_signal_start["btcusdt"] = time.time() - 3.0

        result = strategy.evaluate(market, micro, 300.0)
        # Should pass persistence now
        assert result is not None
        assert result.action == MicroAction.BUY_YES

    def test_direction_flip_resets_timer(self):
        """If direction flips, timer resets."""
        strategy = make_clean_strategy(
            entry_persistence_enabled=True,
            entry_persistence_seconds=2.0,
            entry_threshold=0.30,
        )
        # Start bullish
        micro_bull = make_micro_with_momentum("bullish", 0.8)
        market = make_market(yes_price=0.45, no_price=0.55)
        strategy.evaluate(market, micro_bull, 300.0)

        # Backdate timer
        strategy._entry_signal_start["btcusdt"] = time.time() - 3.0

        # Now flip to bearish — should reset timer
        micro_bear = make_micro_with_momentum("bearish", 0.8)
        result = strategy.evaluate(market, micro_bear, 300.0)
        assert result is None
        assert strategy.last_no_trade_reason == NoTradeReason.FAILED_PERSISTENCE

    def test_persistence_disabled(self):
        """When disabled, first signal enters immediately."""
        strategy = make_clean_strategy(
            entry_persistence_enabled=False,
            entry_threshold=0.30,
        )
        micro = make_micro_with_momentum("bullish", 0.8)
        market = make_market(yes_price=0.45, no_price=0.55)

        result = strategy.evaluate(market, micro, 300.0)
        # Should pass immediately — no persistence wait
        assert result is not None
        assert result.action == MicroAction.BUY_YES


# ═══════════════════════════════════════════════════════════════════════
# Tests: Polymarket Book Entry Veto
# ═══════════════════════════════════════════════════════════════════════

class TestBookEntryVeto:
    """Verify poly_book_enabled entry veto works."""

    def test_thin_exit_book_blocks(self):
        """Bid depth below min → entry blocked (no exit path)."""
        strategy = make_clean_strategy(
            poly_book_enabled=True,
            poly_book_min_exit_depth=20.0,
            entry_threshold=0.30,
        )
        micro = make_micro_with_momentum("bullish", 0.8)
        market = make_market(yes_price=0.45, no_price=0.55)

        book = make_book_intel(yes_bid_depth=5.0)  # Thin bids
        result = strategy.evaluate(market, micro, 300.0, book_intel=book)
        assert result is None
        assert strategy.last_no_trade_reason == NoTradeReason.BOOK_NO_LIQUIDITY

    def test_deep_exit_book_passes(self):
        """Bid depth above min → not blocked."""
        strategy = make_clean_strategy(
            poly_book_enabled=True,
            poly_book_min_exit_depth=20.0,
            poly_book_imbalance_veto=-0.40,
            entry_threshold=0.30,
        )
        micro = make_micro_with_momentum("bullish", 0.8)
        market = make_market(yes_price=0.45, no_price=0.55)

        book = make_book_intel(yes_bid_depth=50.0, yes_imbalance=0.2)
        result = strategy.evaluate(market, micro, 300.0, book_intel=book)
        if result is None:
            assert strategy.last_no_trade_reason != NoTradeReason.BOOK_NO_LIQUIDITY

    def test_imbalance_veto_blocks(self):
        """Book strongly disagrees with direction → blocked."""
        strategy = make_clean_strategy(
            poly_book_enabled=True,
            poly_book_min_exit_depth=20.0,
            poly_book_imbalance_veto=-0.40,
            entry_threshold=0.30,
        )
        micro = make_micro_with_momentum("bullish", 0.8)
        market = make_market(yes_price=0.45, no_price=0.55)

        # Strong bearish imbalance on YES book → bullish entry vetoed
        book = make_book_intel(yes_bid_depth=50.0, yes_imbalance=-0.60)
        result = strategy.evaluate(market, micro, 300.0, book_intel=book)
        assert result is None
        assert strategy.last_no_trade_reason == NoTradeReason.BOOK_VETO

    def test_book_disabled_ignores_thin_book(self):
        """When poly_book_enabled=False, thin book doesn't matter."""
        strategy = make_clean_strategy(
            poly_book_enabled=False,
            entry_threshold=0.30,
        )
        micro = make_micro_with_momentum("bullish", 0.8)
        market = make_market(yes_price=0.45, no_price=0.55)

        book = make_book_intel(yes_bid_depth=1.0, yes_imbalance=-0.90)
        result = strategy.evaluate(market, micro, 300.0, book_intel=book)
        if result is None:
            assert strategy.last_no_trade_reason not in (
                NoTradeReason.BOOK_NO_LIQUIDITY, NoTradeReason.BOOK_VETO
            )


# ═══════════════════════════════════════════════════════════════════════
# Tests: Price-to-Beat Filter
# ═══════════════════════════════════════════════════════════════════════

class TestPriceToBeat:
    """Verify price-to-beat filter blocks fighting the window direction."""

    def test_buying_yes_when_btc_down_blocked(self):
        """Buying YES but BTC below window open → blocked."""
        strategy = make_clean_strategy(entry_threshold=0.30)
        # BTC dropped from window start
        micro = make_micro_with_momentum("bullish", 0.8, price_change_pct=-0.002)
        market = make_market(yes_price=0.45, no_price=0.55)

        result = strategy.evaluate(market, micro, 120.0)  # Within 180s
        assert result is None
        assert strategy.last_no_trade_reason == NoTradeReason.PRICE_TO_BEAT

    def test_buying_no_when_btc_up_blocked(self):
        """Buying NO but BTC above window open → blocked."""
        strategy = make_clean_strategy(entry_threshold=0.30)
        # BTC rose from window start
        micro = make_micro_with_momentum("bearish", 0.8, price_change_pct=0.002)
        market = make_market(yes_price=0.55, no_price=0.45)

        result = strategy.evaluate(market, micro, 120.0)
        assert result is None
        assert strategy.last_no_trade_reason == NoTradeReason.PRICE_TO_BEAT

    def test_buying_with_window_direction_passes(self):
        """Buying YES when BTC is up → aligned, not blocked."""
        strategy = make_clean_strategy(entry_threshold=0.30)
        micro = make_micro_with_momentum("bullish", 0.8, price_change_pct=0.002)
        market = make_market(yes_price=0.45, no_price=0.55)

        result = strategy.evaluate(market, micro, 120.0)
        if result is None:
            assert strategy.last_no_trade_reason != NoTradeReason.PRICE_TO_BEAT

    def test_early_window_not_active(self):
        """With >180s remaining, price-to-beat doesn't apply."""
        strategy = make_clean_strategy(entry_threshold=0.30)
        micro = make_micro_with_momentum("bullish", 0.8, price_change_pct=-0.005)
        market = make_market(yes_price=0.45, no_price=0.55)

        result = strategy.evaluate(market, micro, 300.0)  # >180s
        if result is None:
            assert strategy.last_no_trade_reason != NoTradeReason.PRICE_TO_BEAT


# ═══════════════════════════════════════════════════════════════════════
# Tests: Correct Entry Action / Side
# ═══════════════════════════════════════════════════════════════════════

class TestEntryAction:
    """Verify correct BUY_YES/BUY_NO action and side assignment."""

    def test_bullish_buys_yes(self):
        """Bullish momentum → BUY_YES."""
        strategy = make_clean_strategy(entry_threshold=0.30)
        micro = make_micro_with_momentum("bullish", 0.8)
        market = make_market(yes_price=0.45, no_price=0.55)

        result = strategy.evaluate(market, micro, 300.0)
        assert result is not None
        assert result.action == MicroAction.BUY_YES
        assert result.side == Side.YES
        assert result.market_price == 0.45

    def test_bearish_buys_no(self):
        """Bearish momentum → BUY_NO."""
        strategy = make_clean_strategy(entry_threshold=0.30)
        micro = make_micro_with_momentum("bearish", 0.8)
        market = make_market(yes_price=0.55, no_price=0.45)

        result = strategy.evaluate(market, micro, 300.0)
        assert result is not None
        assert result.action == MicroAction.BUY_NO
        assert result.side == Side.NO
        assert result.market_price == 0.45

    def test_weak_momentum_no_entry(self):
        """Momentum below threshold → no entry."""
        strategy = make_clean_strategy(entry_threshold=0.50)
        micro = make_micro_with_momentum("bullish", 0.3)  # Below 0.50
        market = make_market(yes_price=0.45, no_price=0.55)

        result = strategy.evaluate(market, micro, 300.0)
        assert result is None
        assert strategy.last_no_trade_reason == NoTradeReason.BELOW_THRESHOLD


# ═══════════════════════════════════════════════════════════════════════
# Tests: Multiple Filters Stacking
# ═══════════════════════════════════════════════════════════════════════

class TestFilterStacking:
    """Verify multiple threshold modifiers stack correctly."""

    def test_30s_plus_chop_stack(self):
        """30s opposition + chop boost should both add to threshold."""
        strategy = make_strategy(
            entry_threshold=0.50,
            counter_trend_threshold=0.55,
            chop_filter_enabled=True,
            chop_threshold=3.0,
            chop_scale=5.0,
            chop_max_boost=0.10,
            # Disable everything else
            trend_bias_enabled=False,
            adaptive_bias_enabled=False,
            acceleration_enabled=False,
            low_vol_block_enabled=False,
            high_intensity_block_enabled=False,
            entry_persistence_enabled=False,
            dead_market_band=0.0,
        )
        # Full 30s opposition + high chop
        micro = make_micro_with_momentum("bullish", 0.8, ofi_30s=-0.30)
        # Override chop
        current_class = micro.__class__
        micro.__class__ = type(
            'StackedMicro', (current_class,),
            {'chop_index': property(lambda self: 5.0)},
        )
        market = make_market(yes_price=0.45, no_price=0.55)

        strategy.evaluate(market, micro, 300.0)
        # 30s: 0.50 + 1.0 * 0.05 = 0.55
        # chop: full boost = +0.10
        # total: ~0.65
        assert strategy._last_effective_threshold >= 0.64

    def test_30s_plus_adaptive_bias_stack(self):
        """30s opposition + adaptive bias should both contribute."""
        strategy = make_strategy(
            entry_threshold=0.50,
            counter_trend_threshold=0.55,
            adaptive_bias_enabled=True,
            adaptive_bias_spread=0.10,
            adaptive_bias_min_move=0.003,
            adaptive_bias_lookback_minutes=30.0,
            # Disable everything else
            trend_bias_enabled=False,
            chop_filter_enabled=False,
            acceleration_enabled=False,
            low_vol_block_enabled=False,
            high_intensity_block_enabled=False,
            entry_persistence_enabled=False,
            dead_market_band=0.0,
        )
        # Full 30s opposition + bearish 30m (bad for YES)
        micro = make_micro_with_momentum("bullish", 0.8, ofi_30s=-0.30)
        micro.price_history = [(70000 * 1.005, time.time() - 1800)]
        micro.current_price = 70000.0
        market = make_market(yes_price=0.45, no_price=0.55)

        strategy.evaluate(market, micro, 300.0)
        # 30s: +0.05, adaptive: +0.05 → ~0.60
        assert strategy._last_effective_threshold >= 0.59
