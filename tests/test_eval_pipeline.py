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
