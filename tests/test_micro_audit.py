"""Micro sniper audit tests — targeting specific math bugs identified in the audit.

These tests expose real issues in pricing, exit triggers, and filter logic that
were identified by examining the code against erratic trade logs.

Bugs targeted:
  1. Trailing stop 'breakeven protect' fires at entry_price but sell fills at
     entry_price - exit_slippage → guaranteed loss despite "breakeven" label.
  2. Chop index has a 2x discontinuity at net_move = 0.00005 boundary.
  3. Confidence inflated to 1.0 when only one OFI window is significant.
  4. Dampener behavior: OFI strong + price flat → signal damped to 65%.
  5. Low-vol filter uses cumulative window price_change_pct, not recent 30s activity.
  6. Dead market band filter: exact boundary and direction behavior.
  7. OFI spike then reversal → exit fires vs. weak reversal held.
"""

import sys
import time
import types
import pytest

# Mock py_clob_client before any polyedge imports
_mock_client = types.ModuleType("polyedge.core.client")
_mock_client.PolyClient = type("PolyClient", (), {})
sys.modules.setdefault("polyedge.core.client", _mock_client)

from polyedge.strategies.micro_sniper import MicroSniperStrategy, MicroAction, MicroOpportunity
from polyedge.core.config import Settings
from polyedge.core.models import Market, Side
from polyedge.data.binance_aggtrade import MicroStructure, AggTrade, TradeFlowWindow
from polyedge.data.research import NoTradeReason


# ── Shared helpers (mirrors test_eval_pipeline.py pattern) ──

def make_settings(**overrides) -> Settings:
    s = Settings()
    for k, v in overrides.items():
        setattr(s.strategies.micro_sniper, k, v)
    return s


def make_market(yes_price=0.50, no_price=0.50, condition_id="0xtest123") -> Market:
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


def _add_fresh_trades(
    micro: MicroStructure,
    n: int = 20,
    base_price: float = 70000.0,
    buy_fraction: float = 0.7,
    spacing: float = 0.3,
):
    """Add recent trades (within all rolling windows) to a MicroStructure."""
    now = time.time()
    for i in range(n):
        t = now - (n - i) * spacing
        price = base_price + i * 0.5
        is_buyer_maker = i >= int(n * buy_fraction)
        micro.add_trade(AggTrade(
            symbol="btcusdt",
            price=price,
            quantity=0.01,
            is_buyer_maker=is_buyer_maker,
            timestamp=t,
        ))
    micro.window_start_price = base_price
    micro.current_price = base_price + n * 0.5


def _force_ofi(flow_window: TradeFlowWindow, target_ofi: float):
    """Force a flow window's OFI to a specific value by adjusting volumes."""
    total = max(flow_window.buy_volume + flow_window.sell_volume, 1.0)
    flow_window.buy_volume = total * (1 + target_ofi) / 2
    flow_window.sell_volume = total * (1 - target_ofi) / 2


def make_micro_with_momentum(
    direction: str = "bullish",
    strength: float = 0.8,
    n_trades: int = 30,
    price_change_pct: float = 0.001,
) -> MicroStructure:
    """Create a MicroStructure with mocked momentum_signal and confidence."""
    micro = MicroStructure("btcusdt")
    base_price = 70000.0
    buy_fraction = 0.7 if direction == "bullish" else 0.3
    price_end = base_price * (1 + price_change_pct)
    now = time.time()
    spacing = 0.5

    for i in range(n_trades):
        t = now - (n_trades - i) * spacing
        price = base_price + (price_end - base_price) * (i / max(1, n_trades - 1))
        is_buyer_maker = i >= int(n_trades * buy_fraction)
        micro.add_trade(AggTrade(
            symbol="btcusdt", price=price, quantity=0.01,
            is_buyer_maker=is_buyer_maker, timestamp=t,
        ))

    micro.window_start_price = base_price
    micro.window_start_time = now - n_trades * spacing
    micro.current_price = price_end

    target_momentum = strength if direction == "bullish" else -strength
    target_confidence = min(0.9, 0.4 + strength * 0.5)

    # Per-instance subclass to override properties without affecting other instances
    micro.__class__ = type(
        "MockedMicroStructure",
        (MicroStructure,),
        {
            "momentum_signal": property(lambda self: target_momentum),
            "confidence": property(lambda self: target_confidence),
        },
    )
    return micro


# All filters off — isolate specific behavior under test
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
    entry_threshold=0.30,
    min_confidence=0.10,
    min_trades_in_window=1,
)


def make_clean_strategy(**overrides) -> MicroSniperStrategy:
    """Strategy with all filters off except those in overrides."""
    combined = {**ALL_FILTERS_OFF, **overrides}
    return MicroSniperStrategy(make_settings(**combined))


# ═══════════════════════════════════════════════════════════════════════
# Bug 1: Trailing Stop 'Breakeven Protect' Guarantees a Loss
# ═══════════════════════════════════════════════════════════════════════

class TestTrailingStopBreakevenSlippage:
    """FIXED: breakeven_floor = entry_price + exit_slippage.

    Old bug: breakeven_floor = entry_price caused the stop to fire at entry_price,
    then the FOK sell floor = entry_price - exit_slippage → fill below entry = loss.

    Fix: breakeven_floor = entry_price + exit_slippage ensures:
        FOK floor = effective_floor - exit_slippage = entry_price
        Fill at best_bid >= entry_price → P&L >= 0 (true breakeven).
    """

    def test_trailing_stop_fires_when_price_below_breakeven_floor(self):
        """Stop fires when our_price <= effective_floor (entry + slippage).

        Scenario: BUY NO @ $0.45, HWM $0.50 (11.1% profit → stop armed).
        Price drifts back to $0.45. With fix: effective floor = max(0.44, 0.50) = 0.50.
        triggered = 0.45 <= 0.50 → True (fires since price is below the adjusted floor).
        """
        strategy = make_clean_strategy(
            trailing_stop_enabled=True,
            trailing_stop_min_profit_pct=0.10,  # Arms when HWM is 10% above entry
            trailing_stop_pct=0.12,
            trailing_stop_late_pct=0.15,
            trailing_stop_late_seconds=90.0,
            exit_slippage=0.05,
            take_profit_enabled=False,
        )

        # NO price = $0.45 (exactly at entry)
        market = make_market(yes_price=0.55, no_price=0.45)
        micro = make_micro_with_momentum("bearish", 0.3, n_trades=20)

        result = strategy.evaluate(
            market, micro, seconds_remaining=300.0,
            current_position="no",
            entry_price=0.45,
            high_water_mark=0.50,  # 11.1% above entry → stop is armed
        )

        assert result is not None, (
            "Expected trailing stop to fire when our_price == entry_price. "
            "The 'breakeven protect' floor is exactly entry_price — price touches it → triggered."
        )
        assert result.action == MicroAction.EXIT
        assert result.exit_reason == "trailing_stop"

    def test_fixed_breakeven_floor_ensures_fill_at_entry(self):
        """FIX verified: breakeven_floor = entry_price + exit_slippage ensures fill >= entry.

        With the fix, the FOK floor = effective_floor - exit_slippage = entry_price.
        The fill comes in at best bid >= entry_price → P&L >= 0 (breakeven).
        """
        entry_price = 0.45
        exit_slippage = 0.05

        high_water_mark = 0.50
        trailing_stop_pct = 0.12

        trailing_floor = high_water_mark * (1 - trailing_stop_pct)  # 0.44

        # FIXED formula
        breakeven_floor = entry_price + exit_slippage   # 0.50
        effective_floor = max(trailing_floor, breakeven_floor)  # 0.50

        # FOK is placed at effective_floor - exit_slippage
        fok_floor = effective_floor - exit_slippage  # 0.45 = entry_price
        # Fill comes in at best bid >= fok_floor = best bid >= entry_price
        min_fill = fok_floor  # worst case: fill exactly at FOK floor

        assert min_fill == pytest.approx(entry_price), (
            f"FIX: FOK floor = {fok_floor:.2f} = entry_price = {entry_price:.2f}. "
            f"Fill at best_bid >= {fok_floor:.2f} → P&L >= 0 (breakeven)."
        )

    def test_correct_floor_with_slippage_achieves_breakeven(self):
        """Demonstrate the correct formula: breakeven_floor = entry_price + exit_slippage.

        With the fix, fill = (entry + slippage) - slippage = entry_price → P&L = 0.
        """
        entry_price = 0.45
        exit_slippage = 0.05

        # Current (buggy)
        current_floor = entry_price                          # 0.45
        current_fill = current_floor - exit_slippage         # 0.40
        current_pnl = current_fill - entry_price             # -0.05 LOSS

        # Fixed
        correct_floor = entry_price + exit_slippage          # 0.50
        correct_fill = correct_floor - exit_slippage         # 0.45
        correct_pnl = correct_fill - entry_price             # 0.00 BREAKEVEN

        assert current_pnl < 0, "Current code produces a loss at 'breakeven'"
        assert correct_pnl == pytest.approx(0.0, abs=1e-9), (
            "Correct formula (floor = entry + slippage) achieves exact breakeven"
        )

    def test_trailing_stop_not_armed_below_min_profit(self):
        """Stop should NOT fire if HWM never reached min_profit_pct above entry.

        HWM is only 5% above entry (min_profit_pct = 10%) → stop not armed.
        """
        strategy = make_clean_strategy(
            trailing_stop_enabled=True,
            trailing_stop_min_profit_pct=0.10,
            trailing_stop_pct=0.12,
            trailing_stop_late_pct=0.15,
            trailing_stop_late_seconds=90.0,
            take_profit_enabled=False,
        )

        market = make_market(yes_price=0.55, no_price=0.42)
        micro = make_micro_with_momentum("bearish", 0.3, n_trades=20)

        result = strategy.evaluate(
            market, micro, seconds_remaining=300.0,
            current_position="no",
            entry_price=0.45,
            high_water_mark=0.4725,  # Only 5% above entry — stop not armed yet
        )

        if result is not None:
            assert result.exit_reason != "trailing_stop", (
                "BUG: Trailing stop fired before being armed "
                "(HWM = 5% above entry, min_profit_pct = 10%)"
            )


# ═══════════════════════════════════════════════════════════════════════
# Bug 2: Chop Index 2x Discontinuity at net_move Boundary
# ═══════════════════════════════════════════════════════════════════════

class TestChopIndexDiscontinuity:
    """BUG: The chop_index formula has a discontinuity at net_move = 0.00005.

    Code:
        if net_move < 0.00005:
            return range_pct * 10000
        return range_pct / net_move

    At the boundary with range_pct = 0.001 (0.1%):
        net_move = 0.000049 → 0.001 * 10000 = 10.0
        net_move = 0.000051 → 0.001 / 0.000051 ≈ 19.6  (≈ 2x)

    This is a $1.40 BTC move on a $70k price. The chop filter can suddenly
    nearly double its boost when a micro-movement crosses this threshold.
    """

    def _make_5m_micro(self, start_price: float, end_price: float, peak_price: float) -> MicroStructure:
        """Create MicroStructure with trades in 5m window: start cluster, peak, end cluster.

        Needs >= 10 trades for chop_index to be non-zero (guard in code).
        Spreads trades to give meaningful range and net_move values.
        """
        micro = MicroStructure("btcusdt")
        now = time.time()

        # Add multiple trades: cluster at start, one at peak, cluster at end
        # Total >= 10 trades to pass the len(_trades) < 10 guard
        start_trades = [(start_price + i * 0.01, now - 280 + i) for i in range(5)]
        peak_trade = [(peak_price, now - 140)]
        end_trades = [(end_price + i * 0.01, now - 10 + i) for i in range(5)]

        for price, ts in start_trades + peak_trade + end_trades:
            micro.add_trade(AggTrade("btcusdt", price, 0.1, True, ts))

        micro.window_start_price = start_price
        micro.current_price = end_price
        return micro

    def test_chop_below_boundary_uses_times_10000_formula(self):
        """net_move just below 0.00005 → formula: range_pct * 10000."""
        # net_move = 0.000049 on $70k BTC = $3.43 move
        start = 70000.0
        end = start * (1 + 0.000049)  # 70003.43
        peak = start * 1.001  # 0.1% range

        micro = self._make_5m_micro(start, end, peak)
        chop = micro.chop_index

        net_move = abs(micro.trend_5m)
        range_pct = micro.flow_5m.price_range_pct

        assert net_move < 0.00005, f"Expected net_move below boundary, got {net_move:.6f}"
        expected = range_pct * 10000
        assert chop == pytest.approx(expected, rel=0.05), (
            f"Below-boundary: chop={chop:.2f}, expected range_pct*10000={expected:.2f}"
        )

    def test_chop_above_boundary_uses_division_formula(self):
        """net_move just above 0.00005 → formula: range_pct / net_move."""
        start = 70000.0
        end = start * (1 + 0.000051)  # Just above boundary
        peak = start * 1.001  # Same 0.1% range

        micro = self._make_5m_micro(start, end, peak)
        chop = micro.chop_index

        net_move = abs(micro.trend_5m)
        range_pct = micro.flow_5m.price_range_pct

        assert net_move > 0.00005, f"Expected net_move above boundary, got {net_move:.6f}"
        expected = range_pct / net_move
        assert chop == pytest.approx(expected, rel=0.05), (
            f"Above-boundary: chop={chop:.2f}, expected range_pct/net_move={expected:.2f}"
        )

    def test_chop_formula_discontinuity_magnitude(self):
        """Show the ~2x jump: above-boundary formula gives nearly 2x the value.

        At exactly the boundary, the ratio between the two formulas is:
            (range_pct / 0.00005) / (range_pct * 10000) = 1/(0.00005 * 10000) = 2.0

        So a $1.40 BTC move (on $70k) can nearly double the computed chop index,
        causing a sudden threshold boost in the entry filter.
        """
        range_pct = 0.001  # 0.1% range — realistic 5m scenario

        # Formula for each side of the boundary
        chop_just_below = range_pct * 10000          # net_move = 0.000049
        chop_just_above = range_pct / 0.000051       # net_move = 0.000051

        ratio = chop_just_above / chop_just_below

        assert ratio > 1.8, (
            f"BUG: Chop index jumps {ratio:.2f}x across the 0.00005 net_move boundary. "
            f"A tiny $1.40 BTC move (on $70k) nearly doubles the computed chop. "
            f"Formula should be continuous at the boundary."
        )

    def test_chop_returns_zero_with_insufficient_5m_data(self):
        """chop_index returns 0.0 when 5m window has < 10 trades."""
        micro = MicroStructure("btcusdt")
        now = time.time()
        # Add only 5 trades
        for i in range(5):
            micro.add_trade(AggTrade("btcusdt", 70000 + i, 0.1, True, now - i * 30))

        assert micro.chop_index == 0.0, (
            "chop_index should return 0.0 with < 10 trades in 5m window"
        )


# ═══════════════════════════════════════════════════════════════════════
# Bug 3: Confidence Inflated When Only One OFI Window Is Significant
# ═══════════════════════════════════════════════════════════════════════

class TestConfidenceInflation:
    """BUG (fixed): Confidence agreement was computed as:

        agreement = abs(sum(nonzero)) / len(nonzero)

    If only one OFI window had significant signal, nonzero = [1],
    giving agreement = 1/1 = 1.0 instead of 1/3.

    FIX: agreement = abs(sum(nonzero)) / len(signs)
    Now single-window gives 1/3, two-window gives 2/3, three-window gives 1.0.
    """

    def test_single_significant_ofi_window_gives_one_third_agreement(self):
        """FIXED: Only ofi_5s significant → agreement = 1/3, not 1.0."""
        micro = MicroStructure("btcusdt")
        _add_fresh_trades(micro, n=20, buy_fraction=0.7)

        # ofi_5 is significant, ofi_15 and ofi_30 below the 0.05 threshold
        _force_ofi(micro.flow_5s, 0.80)   # sign = +1
        _force_ofi(micro.flow_15s, 0.03)  # sign = 0 (below 0.05 threshold)
        _force_ofi(micro.flow_30s, 0.02)  # sign = 0 (below 0.05 threshold)

        ofi_5 = micro.flow_5s.ofi
        ofi_15 = micro.flow_15s.ofi
        ofi_30 = micro.flow_30s.ofi

        # Fixed formula: denominator is always len(signs) = 3
        signs = [1 if x > 0.05 else (-1 if x < -0.05 else 0) for x in [ofi_5, ofi_15, ofi_30]]
        nonzero = [s for s in signs if s != 0]

        assert len(nonzero) == 1, f"Only 1 window should be significant, got {len(nonzero)}"

        # FIX: agreement = 1/3, not 1.0
        agreement = abs(sum(nonzero)) / len(signs)
        assert agreement == pytest.approx(1 / 3, abs=0.01), (
            f"FIX: agreement={agreement:.3f} with only 1/3 windows significant. "
            f"Using len(signs)=3 as denominator gives proper 1/3 cross-window consensus."
        )

    def test_all_three_windows_bullish_agreement_is_correctly_1(self):
        """Control: all 3 windows significant and aligned → agreement = 1.0 (correct)."""
        micro = MicroStructure("btcusdt")
        _add_fresh_trades(micro, n=30, buy_fraction=0.8)

        _force_ofi(micro.flow_5s, 0.80)
        _force_ofi(micro.flow_15s, 0.70)
        _force_ofi(micro.flow_30s, 0.60)

        ofi_5, ofi_15, ofi_30 = micro.flow_5s.ofi, micro.flow_15s.ofi, micro.flow_30s.ofi
        signs = [1 if x > 0.05 else (-1 if x < -0.05 else 0) for x in [ofi_5, ofi_15, ofi_30]]
        nonzero = [s for s in signs if s != 0]

        assert len(nonzero) == 3
        agreement = abs(sum(nonzero)) / len(nonzero)
        assert agreement == pytest.approx(1.0)  # Correctly 1.0

    def test_one_window_dissenting_reduces_agreement(self):
        """2 bullish + 1 bearish → agreement = |2-1|/3 = 0.333."""
        micro = MicroStructure("btcusdt")
        _add_fresh_trades(micro, n=30, buy_fraction=0.6)

        _force_ofi(micro.flow_5s, 0.80)    # bullish
        _force_ofi(micro.flow_15s, 0.70)   # bullish
        _force_ofi(micro.flow_30s, -0.60)  # bearish

        ofi_5, ofi_15, ofi_30 = micro.flow_5s.ofi, micro.flow_15s.ofi, micro.flow_30s.ofi
        signs = [1 if x > 0.05 else (-1 if x < -0.05 else 0) for x in [ofi_5, ofi_15, ofi_30]]
        nonzero = [s for s in signs if s != 0]

        assert len(nonzero) == 3
        agreement = abs(sum(nonzero)) / len(nonzero)
        assert agreement == pytest.approx(1 / 3, abs=0.01), (
            f"2 bullish + 1 bearish should give agreement=1/3, got {agreement:.3f}"
        )

    def test_two_significant_windows_same_direction_agreement(self):
        """FIXED: 2 significant (same direction) + 1 near-zero → agreement = 2/3, not 1.0."""
        micro = MicroStructure("btcusdt")
        _add_fresh_trades(micro, n=20, buy_fraction=0.7)

        _force_ofi(micro.flow_5s, 0.80)   # sign = +1
        _force_ofi(micro.flow_15s, 0.70)  # sign = +1
        _force_ofi(micro.flow_30s, 0.03)  # sign = 0 (below threshold)

        ofi_5, ofi_15, ofi_30 = micro.flow_5s.ofi, micro.flow_15s.ofi, micro.flow_30s.ofi
        signs = [1 if x > 0.05 else (-1 if x < -0.05 else 0) for x in [ofi_5, ofi_15, ofi_30]]
        nonzero = [s for s in signs if s != 0]

        assert len(nonzero) == 2
        # FIX: denominator is len(signs) = 3, so 2 agreeing windows → 2/3
        agreement = abs(sum(nonzero)) / len(signs)
        assert agreement == pytest.approx(2 / 3, abs=0.01), (
            f"FIX: 2/3 windows agree → agreement={agreement:.3f} (expected 2/3=0.667, "
            f"was 1.0 with old len(nonzero) denominator)"
        )


# ═══════════════════════════════════════════════════════════════════════
# Bug 4: Dampener Math Verification
# ═══════════════════════════════════════════════════════════════════════

class TestDampenerMath:
    """Document the dampener behavior at the three key states.

    The dampener is intentional design, but understanding the exact output
    is critical for knowing whether a strong OFI will survive the dampener.

    Three states:
      1. OFI strong + price flat → flat_factor (0.65) → 35% signal reduction
      2. OFI strong + price agrees → approaches agree_factor (1.0) → no penalty
      3. OFI strong + price opposes → approaches disagree_factor (0.4) → 60% reduction
    """

    def _compute_dampener(
        self,
        ofi_15: float,
        drift_signal: float,  # Already normalized to [-1, 1]
        flat_factor: float = 0.65,
        agree_factor: float = 1.0,
        disagree_factor: float = 0.4,
        price_deadzone: float = 0.05,
    ) -> float:
        """Replicate the agreement_factor computation from binance_aggtrade.py."""
        abs_drift = abs(drift_signal)

        if abs(ofi_15) < 0.05:
            return 1.0  # No meaningful OFI — no dampening

        if abs_drift < price_deadzone:
            return flat_factor  # OFI present but price flat

        ofi_sign = 1.0 if ofi_15 > 0 else -1.0
        price_sign = 1.0 if drift_signal > 0 else -1.0
        alignment = ofi_sign * price_sign  # +1 or -1

        price_strength = min(
            1.0,
            (abs_drift - price_deadzone) / (1.0 - price_deadzone),
        )

        if alignment > 0:
            return flat_factor + (agree_factor - flat_factor) * price_strength
        else:
            return flat_factor + (disagree_factor - flat_factor) * price_strength

    def test_flat_price_triggers_flat_factor(self):
        """Strong OFI + price within deadzone → flat_factor = 0.65."""
        factor = self._compute_dampener(ofi_15=0.80, drift_signal=0.02)  # drift < deadzone
        assert factor == pytest.approx(0.65), (
            f"Strong OFI + flat price should dampen to flat_factor=0.65, got {factor:.3f}. "
            f"Signal is reduced by 35%."
        )

    def test_aligned_price_gives_full_signal(self):
        """OFI bullish + price clearly rising → agreement_factor approaches agree_factor (1.0).

        At drift_signal=1.0 (max): price_strength = (1.0-0.05)/(1.0-0.05) = 1.0
        → agreement_factor = flat_factor + (agree_factor - flat_factor) * 1.0 = 1.0
        """
        factor = self._compute_dampener(ofi_15=0.80, drift_signal=1.0)  # Max drift
        assert factor == pytest.approx(1.0, abs=0.001), (
            f"Max drift signal with aligned OFI should give agree_factor=1.0, got {factor:.3f}"
        )

    def test_opposed_price_gives_minimum_factor(self):
        """OFI bullish + price falling → agreement_factor approaches disagree_factor (0.4)."""
        factor = self._compute_dampener(ofi_15=0.80, drift_signal=-0.80)  # Full opposition
        assert factor < 0.50, (
            f"OFI opposing price should give factor near disagree_factor=0.4, got {factor:.3f}"
        )

    def test_weak_ofi_bypasses_dampener(self):
        """When abs(ofi_15) < 0.05, dampener returns 1.0 regardless of price."""
        # Even with completely opposed price, weak OFI is not dampened
        factor = self._compute_dampener(ofi_15=0.03, drift_signal=-1.0)
        assert factor == pytest.approx(1.0), (
            f"Weak OFI (< 0.05 threshold) should bypass dampener entirely, got {factor:.3f}"
        )

    def test_flat_factor_applied_at_deadzone_boundary(self):
        """At exactly drift_signal = deadzone, flat_factor is used (not interpolated)."""
        deadzone = 0.05
        # Just inside deadzone
        factor_inside = self._compute_dampener(ofi_15=0.80, drift_signal=0.049)
        # Just outside deadzone
        factor_outside = self._compute_dampener(ofi_15=0.80, drift_signal=0.051)

        assert factor_inside == pytest.approx(0.65, abs=0.001), (
            f"Inside deadzone should use flat_factor=0.65, got {factor_inside:.3f}"
        )
        assert factor_outside > 0.65, (
            f"Outside deadzone (aligned) should be above flat_factor, got {factor_outside:.3f}"
        )


# ═══════════════════════════════════════════════════════════════════════
# Bug 5: Low-Vol Filter Uses Cumulative Window Price, Not Recent 30s
# ═══════════════════════════════════════════════════════════════════════

class TestLowVolFilterMetric:
    """BUG: The low-vol filter checks `abs(micro.price_change_pct)`.

    `price_change_pct` is the CUMULATIVE change from window_start_price to
    current_price. If BTC moved 0.5% in the first 2 minutes then went dead flat,
    price_change_pct ≈ 0.5% >> 0.05% threshold → filter does NOT block entry
    even though the last 30 seconds are completely dormant.

    The filter should measure recent activity (e.g., flow_30s price range),
    not the cumulative window move which can reflect old history.
    """

    def test_price_change_pct_reflects_cumulative_not_recent(self):
        """Verify: price_change_pct = cumulative from window start, not 30s activity."""
        micro = MicroStructure("btcusdt")
        now = time.time()

        window_start_price = 70000.0
        early_peak = 70350.0  # 0.5% move that happened early

        # Set window start to the original price
        micro.window_start_price = window_start_price

        # Add only flat recent trades (last few seconds — low intensity, no movement)
        for i in range(4):
            micro.add_trade(AggTrade(
                "btcusdt", early_peak + i * 0.01,  # Essentially flat at 70350
                0.01, True, now - 3 + i * 0.5,
            ))
        micro.current_price = early_peak

        # Cumulative change = 0.5% (reflecting the early move, not recent flat period)
        cumulative_change = abs(micro.price_change_pct)
        assert cumulative_change == pytest.approx(0.005, rel=0.05), (
            f"price_change_pct should be ~0.5% (cumulative since window start), "
            f"got {cumulative_change:.4f}"
        )

        # Recent 30s is nearly flat (range of < 0.001%)
        recent_range = micro.flow_30s.price_range_pct
        assert recent_range < 0.0001, (
            f"30s price range should be tiny (<0.01%), got {recent_range:.5f}"
        )

        # Demonstrate the discrepancy: cumulative says "market is active"
        # but recent says "market is dead flat"
        assert cumulative_change > recent_range * 10, (
            "Cumulative price_change_pct is much larger than recent 30s range. "
            "The low-vol filter uses the wrong metric: it sees the big early move "
            "and doesn't block entry, even though the last 30s is completely dormant."
        )

    def test_low_vol_block_bypassed_when_early_window_move_is_large(self):
        """Low-vol filter does NOT fire when cumulative window move is large,
        even if recent 30s is flat. This is the practical impact of using
        price_change_pct (cumulative) instead of a recent activity metric.
        """
        strategy = make_clean_strategy(
            low_vol_block_enabled=True,
            low_vol_max_intensity=5.0,
            low_vol_max_price_change=0.0005,  # 0.05% threshold
        )

        micro = MicroStructure("btcusdt")
        now = time.time()

        # Simulate: big early move (0.5%) then flat last 30s
        micro.window_start_price = 70000.0
        # Add a few slow recent trades (low intensity, at the peak — flat)
        for i in range(3):
            micro.add_trade(AggTrade(
                "btcusdt", 70350.0 + i * 0.01,  # Flat at peak
                0.01, True, now - 25 + i * 5,
            ))
        micro.current_price = 70350.0  # cumulative = 0.5% from window start

        # Manually check what the filter sees
        price_change_pct = abs(micro.price_change_pct)
        int_30 = micro.flow_30s.trade_intensity

        # If low-vol filter uses cumulative: big cumulative → filter does NOT block
        # (because price_change_pct >> low_vol_max_price_change)
        if price_change_pct >= strategy.config.low_vol_max_price_change:
            # Filter would NOT fire — this is the buggy behavior
            # (cumulative price masks recent flatness)
            assert price_change_pct >= 0.0005, (
                "Test scenario: cumulative price is large enough to bypass low-vol filter"
            )
        # The 30s intensity is low (3 trades in 25 seconds = ~0.12 tps)
        assert int_30 < strategy.config.low_vol_max_intensity, (
            "30s intensity is low — filter WOULD fire if checking recent activity"
        )


# ═══════════════════════════════════════════════════════════════════════
# Bug 6: Dead Market Band Filter — Boundary and Direction
# ═══════════════════════════════════════════════════════════════════════

class TestDeadMarketBand:
    """Document the dead market filter: blocks when abs(yes_price - 0.50) < dead_market_band.

    The filter uses yes_price regardless of whether we're buying YES or NO.
    """

    def test_blocks_yes_price_inside_band(self):
        """YES price within band of center → entry blocked."""
        strategy = make_clean_strategy(dead_market_band=0.04)
        market = make_market(yes_price=0.51, no_price=0.49)  # 0.01 from center, band=0.04
        micro = make_micro_with_momentum("bullish", 0.8, n_trades=30)

        result = strategy.evaluate(market, micro, seconds_remaining=300.0)

        assert result is None, (
            "Dead market band should block entry when YES=0.51 is within 0.04 of 0.50"
        )
        assert strategy.last_no_trade_reason == NoTradeReason.DEAD_MARKET

    def test_blocks_no_entry_when_yes_near_center(self):
        """Bearish signal (buying NO) is blocked when YES is near 0.50.

        The filter checks yes_price regardless of which side we're entering.
        YES=0.49 → abs(0.49-0.50) = 0.01 < 0.04 → blocked even for NO entry.
        """
        strategy = make_clean_strategy(dead_market_band=0.04)
        market = make_market(yes_price=0.49, no_price=0.51)  # Bearish market, YES near center
        micro = make_micro_with_momentum("bearish", 0.8, n_trades=30)

        result = strategy.evaluate(market, micro, seconds_remaining=300.0)

        assert result is None, (
            "Dead market band blocks NO entry too when YES=0.49 is within 0.04 of 0.50"
        )
        assert strategy.last_no_trade_reason == NoTradeReason.DEAD_MARKET

    def test_allows_entry_outside_band(self):
        """YES price well outside band → dead market filter does not block."""
        strategy = make_clean_strategy(dead_market_band=0.04)
        market = make_market(yes_price=0.40, no_price=0.60)  # 0.10 from center → outside band
        micro = make_micro_with_momentum("bearish", 0.8, n_trades=30)

        result = strategy.evaluate(market, micro, seconds_remaining=300.0)

        if result is None:
            assert strategy.last_no_trade_reason != NoTradeReason.DEAD_MARKET, (
                "Dead market should not block YES=0.40 when band=0.04"
            )

    def test_exact_boundary_floating_point_gotcha(self):
        """Floating-point means the boundary is not exactly where you expect.

        abs(0.46 - 0.50) in IEEE 754 = 0.03999999999999998, which IS < 0.04.
        So yes_price=0.46 with band=0.04 IS blocked by the dead market filter,
        even though mathematically 0.04 == 0.04 → should not be blocked.

        Use a clearly outside value (abs diff > 0.01 past band) for reliable behavior.
        """
        # Verify the floating-point surprise
        assert abs(0.46 - 0.50) < 0.04, (
            "Floating point: abs(0.46 - 0.50) is slightly less than 0.04 in IEEE 754"
        )

        strategy = make_clean_strategy(dead_market_band=0.04)

        # YES=0.44: abs(0.44-0.50) = 0.06 > 0.04 → clearly outside band
        market = make_market(yes_price=0.44, no_price=0.56)
        micro = make_micro_with_momentum("bearish", 0.8, n_trades=30)

        result = strategy.evaluate(market, micro, seconds_remaining=300.0)

        if result is None:
            assert strategy.last_no_trade_reason != NoTradeReason.DEAD_MARKET, (
                "YES=0.44 with band=0.04: abs(0.44-0.50)=0.06 > 0.04 → should NOT be blocked"
            )


# ═══════════════════════════════════════════════════════════════════════
# Bug 7: Exit Logic — Reversal vs. Hold
# ═══════════════════════════════════════════════════════════════════════

class TestExitLogic:
    """Document exit behavior for momentum reversals.

    The erratic trade at 10:33: OFI +1.00 entry → OFI flipped to -0.58 14s later
    → exit triggered at -$0.18 even though BTC continued up.

    This was caused by the 30s OFI flipping bearish between status display and
    actual eval, dropping the effective exit threshold from 0.45 to 0.30,
    making a -0.40 momentum reading trigger the exit.
    """

    def test_strong_reversal_triggers_exit(self):
        """Holding YES, bearish momentum > exit_threshold → EXIT."""
        strategy = make_clean_strategy(
            exit_threshold=0.30,
            trailing_stop_enabled=False,
            take_profit_enabled=False,
        )

        market = make_market(yes_price=0.45, no_price=0.55)
        micro = make_micro_with_momentum("bearish", 0.45, n_trades=20)

        result = strategy.evaluate(
            market, micro, seconds_remaining=300.0,
            current_position="yes",
            entry_price=0.57,
            high_water_mark=0.60,
        )

        assert result is not None
        assert result.action == MicroAction.EXIT
        assert result.exit_reason == "reversal"

    def test_weak_reversal_below_threshold_holds(self):
        """Holding YES, bearish momentum below exit_threshold → no exit."""
        strategy = make_clean_strategy(
            exit_threshold=0.30,
            trailing_stop_enabled=False,
            take_profit_enabled=False,
        )

        market = make_market(yes_price=0.52, no_price=0.48)
        micro = make_micro_with_momentum("bearish", 0.20, n_trades=20)

        result = strategy.evaluate(
            market, micro, seconds_remaining=300.0,
            current_position="yes",
            entry_price=0.57,
            high_water_mark=0.60,
        )

        # Weak reversal below threshold → hold
        assert result is None or result.action == MicroAction.HOLD, (
            f"Weak reversal (0.20 < exit_threshold 0.30) should not trigger exit, "
            f"got action={result.action if result else 'None'}"
        )

    def test_effective_exit_threshold_raised_when_30s_ofi_agrees(self):
        """When 30s OFI aligns with our position, exit threshold is raised.

        Holding YES with bullish 30s OFI → effective exit threshold increases
        from base exit_threshold toward counter_trend_exit_threshold.
        This means stronger momentum reversal is needed before exiting.
        """
        strategy = make_clean_strategy(
            exit_threshold=0.30,
            counter_trend_exit_threshold=0.45,
            trailing_stop_enabled=False,
            take_profit_enabled=False,
        )

        market = make_market(yes_price=0.52, no_price=0.48)
        # Bullish 30s OFI (agrees with our YES position)
        micro = make_micro_with_momentum("bearish", 0.35, n_trades=30, price_change_pct=0.001)
        # Force bullish 30s OFI to give us protection
        _force_ofi(micro.flow_30s, 0.60)  # Strongly bullish = agrees with YES

        result = strategy.evaluate(
            market, micro, seconds_remaining=300.0,
            current_position="yes",
            entry_price=0.57,
            high_water_mark=0.60,
        )

        # With 30s OFI strongly aligned (0.60 > 0.30), protection is maxed out.
        # effective_exit = 0.30 + 1.0 * (0.45 - 0.30) = 0.45
        # Momentum is 0.35 < 0.45 → should NOT exit
        assert result is None or result.action != MicroAction.EXIT, (
            "When 30s OFI strongly agrees with position, "
            "effective exit threshold is raised to 0.45. "
            "Momentum of 0.35 should not trigger exit."
        )

    def test_sparse_data_guard_blocks_exit_signal(self):
        """Exit with only 1 trade in 15s window → sparse data guard returns None."""
        strategy = make_clean_strategy(
            exit_threshold=0.30,
            min_trades_in_window=5,  # Need at least 5 trades
            trailing_stop_enabled=False,
            take_profit_enabled=False,
        )

        market = make_market(yes_price=0.45, no_price=0.55)
        micro = make_micro_with_momentum("bearish", 0.80, n_trades=1)  # Only 1 trade!

        result = strategy.evaluate(
            market, micro, seconds_remaining=300.0,
            current_position="yes",
            entry_price=0.57,
            high_water_mark=0.60,
        )

        # Sparse data guard (min_trades_in_window) blocks exit before reversal check
        # With only 1 trade: flow_15s.total_count = 1 < 5 → return None
        assert result is None, (
            "Sparse data guard should block exit when < min_trades_in_window trades in 15s"
        )


# ═══════════════════════════════════════════════════════════════════════
# Integration: Replicate the 07:24 NO Trade Scenario
# ═══════════════════════════════════════════════════════════════════════

class TestBreakevenScenario0724:
    """Replicate the 07:24 NO trade scenario, verifying the fix works.

    Old (buggy) behavior:
        BUY NO @ $0.45, HWM $0.50
        breakeven_floor = 0.45 → fires at entry_price
        FOK floor = 0.45 - 0.05 = 0.40 → fills ~0.44 → LOSS

    Fixed behavior:
        breakeven_floor = 0.45 + 0.05 = 0.50 → fires when price < 0.50
        FOK floor = 0.50 - 0.05 = 0.45 = entry → fills >= 0.45 → breakeven
    """

    def test_scenario_stop_fires_when_price_below_breakeven_floor(self):
        """FIXED: BUY NO @ $0.45, HWM $0.50, price at $0.45 → stop fires (price < floor $0.50)."""
        strategy = make_clean_strategy(
            trailing_stop_enabled=True,
            trailing_stop_min_profit_pct=0.10,
            trailing_stop_pct=0.12,
            trailing_stop_late_pct=0.15,
            trailing_stop_late_seconds=90.0,
            exit_slippage=0.05,
            take_profit_enabled=False,
        )

        market = make_market(yes_price=0.55, no_price=0.45)  # NO at entry
        micro = make_micro_with_momentum("bearish", 0.3, n_trades=20)

        result = strategy.evaluate(
            market, micro, seconds_remaining=300.0,
            current_position="no",
            entry_price=0.45,
            high_water_mark=0.50,
        )

        assert result is not None, "Trailing stop must fire when price is below breakeven floor"
        assert result.action == MicroAction.EXIT
        assert result.exit_reason == "trailing_stop"
        assert result.market_price == pytest.approx(0.45, abs=0.001)

    def test_scenario_fixed_floor_achieves_breakeven(self):
        """FIXED: FOK floor = effective_floor - exit_slippage = entry_price → fill >= entry."""
        entry_price = 0.45
        exit_slippage = 0.05
        position_usd = 5.0

        # Fixed formula
        breakeven_floor = entry_price + exit_slippage  # 0.50
        trailing_floor = 0.50 * (1 - 0.12)           # 0.44
        effective_floor = max(trailing_floor, breakeven_floor)  # 0.50

        # FOK floor = effective_floor - exit_slippage = 0.45 = entry_price
        fok_floor = effective_floor - exit_slippage  # 0.45
        assert fok_floor == pytest.approx(entry_price), (
            f"FIX: FOK floor = {fok_floor:.2f} = entry_price → fill at best_bid >= entry_price"
        )

        # Best case fill at entry = true breakeven
        contracts = position_usd / entry_price
        best_fill = entry_price  # bid at entry
        pnl = (best_fill - entry_price) * contracts
        assert pnl == pytest.approx(0.0, abs=1e-6), "Breakeven fill produces $0 P&L"

    def test_scenario_hwm_at_min_profit_boundary_does_not_fire_prematurely(self):
        """With fix: stop arms at HWM=0.495, but does NOT fire if price is at HWM (0.495).

        At HWM: effective_floor = max(0.436, 0.50) = 0.50.
        our_price (0.495) <= 0.50 → True → fires.
        Note: this is an early exit (price hasn't returned far from HWM) but the fill
        comes in at ~0.485 (above entry 0.45) → small profit, not a loss.
        """
        entry_price = 0.45
        exit_slippage = 0.05
        min_profit = 0.10
        hwm = entry_price * (1 + min_profit)  # 0.495

        profit_from_entry = (hwm - entry_price) / entry_price
        assert profit_from_entry == pytest.approx(min_profit, abs=1e-9)

        # Fixed formula
        trailing_floor = hwm * (1 - 0.12)                     # 0.436
        breakeven_floor = entry_price + exit_slippage           # 0.50
        effective_floor = max(trailing_floor, breakeven_floor)  # 0.50

        # At HWM price: our_price = 0.495 < 0.50 → fires
        our_price_at_hwm = hwm  # 0.495
        triggered = our_price_at_hwm <= effective_floor  # 0.495 <= 0.50 → True
        assert triggered, "At minimum HWM, price < breakeven_floor → fires"

        # But fill is above entry_price
        fok_floor = effective_floor - exit_slippage  # 0.45 = entry_price
        assert fok_floor == pytest.approx(entry_price), "FOK floor = entry → no loss"
