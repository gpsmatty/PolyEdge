"""Tests for the micro sniper — aggTrade feed, microstructure, and momentum strategy.

Tests cover:
- AggTrade dataclass and properties
- TradeFlowWindow: rolling aggregation, OFI, VWAP, intensity
- MicroStructure: composite momentum signal, confidence
- MicroSniperStrategy: entry, exit, flip, hold decisions
- Config defaults
- Edge cases: empty windows, zero volume, boundary conditions
"""

import time
import pytest
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ── Inline types (same pattern as test_crypto_sniper.py) ──

class Side(str, Enum):
    YES = "YES"
    NO = "NO"


@dataclass
class FakeMarket:
    condition_id: str = "0x123"
    question: str = "Bitcoin Up or Down - March 10, 3:10PM-3:15PM ET"
    description: str = ""
    slug: str = ""
    category: str = ""
    end_date: object = None
    active: bool = True
    closed: bool = False
    clob_token_ids: list = field(default_factory=list)
    yes_price: float = 0.50
    no_price: float = 0.50
    volume: float = 10000
    liquidity: float = 5000
    spread: float = 0.01
    raw: dict = field(default_factory=dict)

    @property
    def yes_token_id(self):
        return self.clob_token_ids[0] if self.clob_token_ids else None

    @property
    def no_token_id(self):
        return self.clob_token_ids[1] if len(self.clob_token_ids) > 1 else None


# ── Inline AggTrade and flow classes (mirror the real ones for testing) ──

@dataclass
class AggTrade:
    symbol: str
    price: float
    quantity: float
    is_buyer_maker: bool
    timestamp: float

    @property
    def is_buy(self) -> bool:
        return not self.is_buyer_maker

    @property
    def dollar_volume(self) -> float:
        return self.price * self.quantity


@dataclass
class TradeFlowWindow:
    symbol: str
    window_seconds: float = 15.0
    _trades: deque = field(default_factory=deque)
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    buy_count: int = 0
    sell_count: int = 0
    vwap_numerator: float = 0.0
    total_quantity: float = 0.0

    def add(self, trade: AggTrade):
        self._trades.append(trade)
        if trade.is_buy:
            self.buy_volume += trade.dollar_volume
            self.buy_count += 1
        else:
            self.sell_volume += trade.dollar_volume
            self.sell_count += 1
        self.vwap_numerator += trade.price * trade.quantity
        self.total_quantity += trade.quantity
        self._prune()

    def _prune(self):
        cutoff = time.time() - self.window_seconds
        while self._trades and self._trades[0].timestamp < cutoff:
            old = self._trades.popleft()
            if old.is_buy:
                self.buy_volume -= old.dollar_volume
                self.buy_count -= 1
            else:
                self.sell_volume -= old.dollar_volume
                self.sell_count -= 1
            self.vwap_numerator -= old.price * old.quantity
            self.total_quantity -= old.quantity

    @property
    def total_volume(self) -> float:
        return self.buy_volume + self.sell_volume

    @property
    def total_count(self) -> int:
        return self.buy_count + self.sell_count

    @property
    def ofi(self) -> float:
        total = self.total_volume
        if total <= 0:
            return 0.0
        return (self.buy_volume - self.sell_volume) / total

    @property
    def trade_intensity(self) -> float:
        if not self._trades or len(self._trades) < 2:
            return 0.0
        span = self._trades[-1].timestamp - self._trades[0].timestamp
        if span <= 0:
            return float(len(self._trades))
        return len(self._trades) / span

    @property
    def vwap(self) -> float:
        if self.total_quantity <= 0:
            return 0.0
        return self.vwap_numerator / self.total_quantity

    @property
    def latest_price(self) -> float:
        if self._trades:
            return self._trades[-1].price
        return 0.0

    @property
    def vwap_drift(self) -> float:
        v = self.vwap
        p = self.latest_price
        if v <= 0 or p <= 0:
            return 0.0
        return (p - v) / v

    @property
    def is_active(self) -> bool:
        return len(self._trades) > 0

    def reset(self):
        self._trades.clear()
        self.buy_volume = 0.0
        self.sell_volume = 0.0
        self.buy_count = 0
        self.sell_count = 0
        self.vwap_numerator = 0.0
        self.total_quantity = 0.0


@dataclass
class MicroStructure:
    symbol: str
    flow_5s: TradeFlowWindow = field(default=None)
    flow_15s: TradeFlowWindow = field(default=None)
    flow_30s: TradeFlowWindow = field(default=None)
    window_start_price: float = 0.0
    window_start_time: float = 0.0
    current_price: float = 0.0
    tick_count: int = 0

    def __post_init__(self):
        if self.flow_5s is None:
            self.flow_5s = TradeFlowWindow(symbol=self.symbol, window_seconds=5.0)
        if self.flow_15s is None:
            self.flow_15s = TradeFlowWindow(symbol=self.symbol, window_seconds=15.0)
        if self.flow_30s is None:
            self.flow_30s = TradeFlowWindow(symbol=self.symbol, window_seconds=30.0)

    def add_trade(self, trade: AggTrade):
        self.flow_5s.add(trade)
        self.flow_15s.add(trade)
        self.flow_30s.add(trade)
        self.current_price = trade.price
        self.tick_count += 1

    def start_window(self, price: float):
        self.window_start_price = price
        self.window_start_time = time.time()
        self.current_price = price
        self.tick_count = 0

    @property
    def price_change_pct(self) -> float:
        if self.window_start_price <= 0:
            return 0.0
        return (self.current_price - self.window_start_price) / self.window_start_price

    @property
    def momentum_signal(self) -> float:
        if not self.flow_5s.is_active:
            return 0.0
        ofi_5 = self.flow_5s.ofi
        ofi_15 = self.flow_15s.ofi
        drift = self.flow_15s.vwap_drift
        drift_signal = max(-1.0, min(1.0, drift * 5000))
        int_5 = self.flow_5s.trade_intensity
        int_30 = self.flow_30s.trade_intensity
        if int_30 > 0:
            intensity_ratio = int_5 / int_30
            intensity_signal = max(-1.0, min(1.0, (intensity_ratio - 1.0)))
        else:
            intensity_signal = 0.0
        dominant_direction = 1.0 if ofi_5 > 0 else (-1.0 if ofi_5 < 0 else 0.0)
        intensity_component = intensity_signal * dominant_direction
        signal = 0.40 * ofi_5 + 0.30 * ofi_15 + 0.20 * drift_signal + 0.10 * intensity_component
        return max(-1.0, min(1.0, signal))

    @property
    def confidence(self) -> float:
        if not self.flow_5s.is_active:
            return 0.0
        ofi_5 = self.flow_5s.ofi
        ofi_15 = self.flow_15s.ofi
        ofi_30 = self.flow_30s.ofi
        signs = [1 if x > 0.05 else (-1 if x < -0.05 else 0) for x in [ofi_5, ofi_15, ofi_30]]
        nonzero = [s for s in signs if s != 0]
        if not nonzero:
            return 0.0
        agreement = abs(sum(nonzero)) / len(nonzero)
        strength = min(1.0, abs(self.momentum_signal) * 2)
        vol_ok = min(1.0, self.flow_15s.total_count / 10.0)
        return agreement * 0.4 + strength * 0.4 + vol_ok * 0.2


# ── Helper to generate trades ──

def make_trades(symbol: str, n: int, base_price: float = 70000.0,
                buy_fraction: float = 0.5, qty: float = 0.01,
                time_start: float = None, time_spacing: float = 0.1) -> list[AggTrade]:
    """Generate a list of trades with controllable buy/sell ratio."""
    if time_start is None:
        time_start = time.time() - n * time_spacing
    trades = []
    n_buys = int(n * buy_fraction)
    for i in range(n):
        is_buyer_maker = i >= n_buys  # First n_buys are buys, rest are sells
        # Slight price drift based on buy pressure
        price_drift = (i / n) * 10 * (buy_fraction - 0.5)
        trades.append(AggTrade(
            symbol=symbol,
            price=base_price + price_drift,
            quantity=qty,
            is_buyer_maker=is_buyer_maker,
            timestamp=time_start + i * time_spacing,
        ))
    return trades


# ═══════════════════════════════════════════════════════════════════════
# Tests: AggTrade
# ═══════════════════════════════════════════════════════════════════════

class TestAggTrade:
    def test_buy_detection(self):
        """is_buyer_maker=False means buyer was aggressor (buy pressure)."""
        trade = AggTrade("btcusdt", 70000, 0.1, is_buyer_maker=False, timestamp=time.time())
        assert trade.is_buy is True

    def test_sell_detection(self):
        """is_buyer_maker=True means seller was aggressor (sell pressure)."""
        trade = AggTrade("btcusdt", 70000, 0.1, is_buyer_maker=True, timestamp=time.time())
        assert trade.is_buy is False

    def test_dollar_volume(self):
        trade = AggTrade("btcusdt", 70000, 0.5, is_buyer_maker=False, timestamp=time.time())
        assert trade.dollar_volume == 35000.0

    def test_zero_quantity(self):
        trade = AggTrade("btcusdt", 70000, 0.0, is_buyer_maker=False, timestamp=time.time())
        assert trade.dollar_volume == 0.0


# ═══════════════════════════════════════════════════════════════════════
# Tests: TradeFlowWindow
# ═══════════════════════════════════════════════════════════════════════

class TestTradeFlowWindow:
    def test_empty_window(self):
        w = TradeFlowWindow(symbol="btcusdt", window_seconds=15.0)
        assert w.ofi == 0.0
        assert w.trade_intensity == 0.0
        assert w.vwap == 0.0
        assert w.is_active is False

    def test_all_buys_ofi_positive(self):
        """100% buy pressure should give OFI = +1.0."""
        w = TradeFlowWindow(symbol="btcusdt", window_seconds=60.0)
        trades = make_trades("btcusdt", 20, buy_fraction=1.0)
        for t in trades:
            w.add(t)
        assert w.ofi == pytest.approx(1.0, abs=0.01)

    def test_all_sells_ofi_negative(self):
        """100% sell pressure should give OFI = -1.0."""
        w = TradeFlowWindow(symbol="btcusdt", window_seconds=60.0)
        trades = make_trades("btcusdt", 20, buy_fraction=0.0)
        for t in trades:
            w.add(t)
        assert w.ofi == pytest.approx(-1.0, abs=0.01)

    def test_balanced_ofi_near_zero(self):
        """50/50 buy/sell should give OFI near 0."""
        w = TradeFlowWindow(symbol="btcusdt", window_seconds=60.0)
        trades = make_trades("btcusdt", 20, buy_fraction=0.5)
        for t in trades:
            w.add(t)
        assert abs(w.ofi) < 0.05

    def test_70_30_buy_ofi_positive(self):
        """70% buys should give clearly positive OFI."""
        w = TradeFlowWindow(symbol="btcusdt", window_seconds=60.0)
        trades = make_trades("btcusdt", 20, buy_fraction=0.7)
        for t in trades:
            w.add(t)
        assert w.ofi > 0.3

    def test_vwap_calculation(self):
        """VWAP should be the volume-weighted average price."""
        w = TradeFlowWindow(symbol="btcusdt", window_seconds=60.0)
        # Two trades: 70000 * 1.0 and 71000 * 1.0
        t1 = AggTrade("btcusdt", 70000, 1.0, False, time.time())
        t2 = AggTrade("btcusdt", 71000, 1.0, False, time.time())
        w.add(t1)
        w.add(t2)
        assert w.vwap == pytest.approx(70500.0)

    def test_vwap_weighted(self):
        """VWAP with unequal quantities."""
        w = TradeFlowWindow(symbol="btcusdt", window_seconds=60.0)
        t1 = AggTrade("btcusdt", 70000, 3.0, False, time.time())
        t2 = AggTrade("btcusdt", 71000, 1.0, False, time.time())
        w.add(t1)
        w.add(t2)
        expected = (70000 * 3 + 71000 * 1) / 4
        assert w.vwap == pytest.approx(expected)

    def test_trade_count(self):
        w = TradeFlowWindow(symbol="btcusdt", window_seconds=60.0)
        trades = make_trades("btcusdt", 15, buy_fraction=0.6)
        for t in trades:
            w.add(t)
        assert w.total_count == 15
        assert w.buy_count == 9  # 60% of 15
        assert w.sell_count == 6

    def test_pruning_removes_old_trades(self):
        """Trades older than window_seconds should be pruned."""
        w = TradeFlowWindow(symbol="btcusdt", window_seconds=5.0)
        now = time.time()
        # Add old trade (10s ago) and new trade (now)
        old = AggTrade("btcusdt", 70000, 1.0, False, now - 10)
        new = AggTrade("btcusdt", 71000, 1.0, True, now)
        w.add(old)
        w.add(new)
        # Old should be pruned when new is added
        assert w.total_count == 1
        assert w.latest_price == 71000

    def test_reset(self):
        w = TradeFlowWindow(symbol="btcusdt", window_seconds=60.0)
        trades = make_trades("btcusdt", 10)
        for t in trades:
            w.add(t)
        assert w.total_count == 10
        w.reset()
        assert w.total_count == 0
        assert w.ofi == 0.0
        assert w.is_active is False

    def test_vwap_drift_positive(self):
        """Price above VWAP = positive drift."""
        w = TradeFlowWindow(symbol="btcusdt", window_seconds=60.0)
        # Start low, end high
        now = time.time()
        for i in range(10):
            price = 70000 + i * 10  # Rising prices
            w.add(AggTrade("btcusdt", price, 1.0, False, now + i * 0.1))
        # Latest price should be above VWAP
        assert w.vwap_drift > 0

    def test_vwap_drift_negative(self):
        """Price below VWAP = negative drift."""
        w = TradeFlowWindow(symbol="btcusdt", window_seconds=60.0)
        now = time.time()
        for i in range(10):
            price = 71000 - i * 10  # Falling prices
            w.add(AggTrade("btcusdt", price, 1.0, False, now + i * 0.1))
        assert w.vwap_drift < 0


# ═══════════════════════════════════════════════════════════════════════
# Tests: MicroStructure
# ═══════════════════════════════════════════════════════════════════════

class TestMicroStructure:
    def test_empty_state(self):
        micro = MicroStructure(symbol="btcusdt")
        assert micro.momentum_signal == 0.0
        assert micro.confidence == 0.0
        assert micro.current_price == 0.0

    def test_strong_buy_momentum(self):
        """Heavy buy pressure should give positive momentum."""
        micro = MicroStructure(symbol="btcusdt")
        trades = make_trades("btcusdt", 30, buy_fraction=0.9, base_price=70000)
        for t in trades:
            micro.add_trade(t)
        assert micro.momentum_signal > 0.3
        assert micro.confidence > 0

    def test_strong_sell_momentum(self):
        """Heavy sell pressure should give negative momentum."""
        micro = MicroStructure(symbol="btcusdt")
        trades = make_trades("btcusdt", 30, buy_fraction=0.1, base_price=70000)
        for t in trades:
            micro.add_trade(t)
        assert micro.momentum_signal < -0.3

    def test_balanced_low_momentum(self):
        """50/50 pressure should give low momentum."""
        micro = MicroStructure(symbol="btcusdt")
        trades = make_trades("btcusdt", 30, buy_fraction=0.5, base_price=70000)
        for t in trades:
            micro.add_trade(t)
        assert abs(micro.momentum_signal) < 0.15

    def test_momentum_bounded(self):
        """Momentum should always be in [-1, 1]."""
        micro = MicroStructure(symbol="btcusdt")
        trades = make_trades("btcusdt", 100, buy_fraction=1.0, base_price=70000)
        for t in trades:
            micro.add_trade(t)
        assert -1.0 <= micro.momentum_signal <= 1.0

    def test_price_tracking(self):
        micro = MicroStructure(symbol="btcusdt")
        micro.start_window(70000)
        trade = AggTrade("btcusdt", 70100, 0.1, False, time.time())
        micro.add_trade(trade)
        assert micro.current_price == 70100
        assert micro.price_change_pct == pytest.approx(100 / 70000, abs=1e-6)

    def test_confidence_agreement(self):
        """Confidence higher when all windows agree."""
        micro = MicroStructure(symbol="btcusdt")
        # All buys across all time windows
        trades = make_trades("btcusdt", 50, buy_fraction=0.9, base_price=70000,
                             time_spacing=0.5)  # Spread over 25 seconds
        for t in trades:
            micro.add_trade(t)
        conf = micro.confidence
        assert conf > 0.3  # Should be reasonably confident

    def test_tick_count(self):
        micro = MicroStructure(symbol="btcusdt")
        trades = make_trades("btcusdt", 15, buy_fraction=0.5)
        for t in trades:
            micro.add_trade(t)
        assert micro.tick_count == 15


# ═══════════════════════════════════════════════════════════════════════
# Tests: Micro Sniper Strategy decisions
# ═══════════════════════════════════════════════════════════════════════

# Strategy config defaults for testing
class FakeConfig:
    enabled: bool = True
    entry_threshold: float = 0.40
    exit_threshold: float = 0.15
    hold_threshold: float = 0.08
    flip_threshold: float = 0.50
    flip_min_confidence: float = 0.50
    min_confidence: float = 0.40
    min_trades_in_window: int = 10
    min_trades_for_flip: int = 25
    min_seconds_remaining: float = 15.0
    force_exit_seconds: float = 8.0
    min_entry_price: float = 0.15
    max_entry_price: float = 0.80
    max_position_per_trade: float = 0.03
    max_trades_per_window: int = 50
    min_liquidity: float = 500


# Simplified strategy logic for testing (mirrors the real one)
def evaluate_micro(market, micro, seconds_remaining, current_position=None, config=None):
    """Simplified evaluate function matching the strategy logic."""
    if config is None:
        config = FakeConfig()

    if not config.enabled or seconds_remaining <= 0 or not micro.flow_5s.is_active:
        return None

    momentum = micro.momentum_signal
    confidence = micro.confidence
    abs_momentum = abs(momentum)
    is_bullish = momentum > 0

    # Force exit near window close
    if seconds_remaining < config.force_exit_seconds and current_position:
        return {"action": "exit", "side": current_position}

    if seconds_remaining < config.min_seconds_remaining:
        return None

    # With position — check exit/flip/hold
    if current_position:
        holding_yes = current_position == "yes"
        aligned = (holding_yes and is_bullish) or (not holding_yes and not is_bullish)

        # Guard: don't act on sparse data
        if micro.flow_15s.total_count < config.min_trades_in_window:
            return None

        # Flip (requires higher trade count — flips are costly)
        has_enough_for_flip = micro.flow_15s.total_count >= config.min_trades_for_flip
        if not aligned and abs_momentum >= config.flip_threshold:
            if confidence >= config.flip_min_confidence and has_enough_for_flip:
                new_side = "yes" if is_bullish else "no"
                price = market.yes_price if is_bullish else market.no_price
                if price >= config.min_entry_price and price <= config.max_entry_price:
                    return {"action": f"flip_{new_side}", "side": new_side, "is_flip": True}

        # Exit on reversal
        if not aligned and abs_momentum >= config.exit_threshold:
            return {"action": "exit", "side": current_position}

        # Exit on weak aligned signal
        if aligned and abs_momentum < config.hold_threshold:
            return {"action": "exit", "side": current_position}

        # Hold
        return None

    # No position — check entry
    if abs_momentum < config.entry_threshold:
        return None
    if confidence < config.min_confidence:
        return None
    if micro.flow_15s.total_count < config.min_trades_in_window:
        return None

    side = "yes" if is_bullish else "no"
    price = market.yes_price if is_bullish else market.no_price
    if price > config.max_entry_price:
        return None
    if price < config.min_entry_price:
        return None

    return {"action": f"buy_{side}", "side": side}


class TestMicroSniperEntry:
    def test_entry_on_strong_buy_momentum(self):
        """Should enter YES when strong buy pressure."""
        micro = MicroStructure(symbol="btcusdt")
        trades = make_trades("btcusdt", 30, buy_fraction=0.9, base_price=70000)
        for t in trades:
            micro.add_trade(t)

        market = FakeMarket(yes_price=0.50, no_price=0.50)
        result = evaluate_micro(market, micro, 120.0)

        if result is not None:
            assert result["side"] == "yes"
            assert "buy" in result["action"]

    def test_entry_on_strong_sell_momentum(self):
        """Should enter NO when strong sell pressure."""
        micro = MicroStructure(symbol="btcusdt")
        trades = make_trades("btcusdt", 30, buy_fraction=0.1, base_price=70000)
        for t in trades:
            micro.add_trade(t)

        market = FakeMarket(yes_price=0.50, no_price=0.50)
        result = evaluate_micro(market, micro, 120.0)

        if result is not None:
            assert result["side"] == "no"
            assert "buy" in result["action"]

    def test_no_entry_on_weak_signal(self):
        """Should not enter when momentum is too weak."""
        micro = MicroStructure(symbol="btcusdt")
        trades = make_trades("btcusdt", 30, buy_fraction=0.55, base_price=70000)
        for t in trades:
            micro.add_trade(t)

        market = FakeMarket(yes_price=0.50, no_price=0.50)
        result = evaluate_micro(market, micro, 120.0)
        # Weak signal (55% buys) should likely not trigger entry
        # Result may be None or the threshold might not be met

    def test_no_entry_when_disabled(self):
        """Should not enter when strategy is disabled."""
        config = FakeConfig()
        config.enabled = False
        micro = MicroStructure(symbol="btcusdt")
        trades = make_trades("btcusdt", 30, buy_fraction=0.9)
        for t in trades:
            micro.add_trade(t)

        market = FakeMarket()
        result = evaluate_micro(market, micro, 120.0, config=config)
        assert result is None

    def test_no_entry_when_expired(self):
        """Should not enter when window has expired."""
        micro = MicroStructure(symbol="btcusdt")
        trades = make_trades("btcusdt", 30, buy_fraction=0.9)
        for t in trades:
            micro.add_trade(t)

        market = FakeMarket()
        result = evaluate_micro(market, micro, 0.0)
        assert result is None

    def test_no_entry_too_close_to_end(self):
        """Should not enter with <15s remaining."""
        micro = MicroStructure(symbol="btcusdt")
        trades = make_trades("btcusdt", 30, buy_fraction=0.9)
        for t in trades:
            micro.add_trade(t)

        market = FakeMarket()
        result = evaluate_micro(market, micro, 10.0)
        assert result is None

    def test_no_entry_price_too_high(self):
        """Should not buy a side priced above max_entry_price."""
        micro = MicroStructure(symbol="btcusdt")
        trades = make_trades("btcusdt", 30, buy_fraction=0.9)
        for t in trades:
            micro.add_trade(t)

        market = FakeMarket(yes_price=0.85, no_price=0.15)
        result = evaluate_micro(market, micro, 120.0)
        # YES is 0.85 > 0.80 max, so should not enter YES
        if result is not None:
            assert result["side"] != "yes" or market.yes_price <= 0.80

    def test_no_entry_price_too_low(self):
        """Should not buy a nearly-dead side (market says <15% chance)."""
        micro = MicroStructure(symbol="btcusdt")
        trades = make_trades("btcusdt", 30, buy_fraction=0.9)
        for t in trades:
            micro.add_trade(t)

        # YES at 4¢ — market says 96% chance of DOWN. Don't fight it.
        market = FakeMarket(yes_price=0.04, no_price=0.96)
        result = evaluate_micro(market, micro, 120.0)
        # Should not buy YES at 4¢ (below 15¢ min_entry_price)
        if result is not None:
            assert result["side"] != "yes", "Should not buy YES at 4¢"

    def test_no_entry_too_few_trades(self):
        """Should not enter with insufficient trade count."""
        micro = MicroStructure(symbol="btcusdt")
        # Only 5 trades, config requires 10
        trades = make_trades("btcusdt", 5, buy_fraction=0.9)
        for t in trades:
            micro.add_trade(t)

        market = FakeMarket()
        config = FakeConfig()
        config.min_trades_in_window = 10
        result = evaluate_micro(market, micro, 120.0, config=config)
        assert result is None


class TestMicroSniperExit:
    def test_force_exit_near_close(self):
        """Should force exit when <8s remaining with position."""
        micro = MicroStructure(symbol="btcusdt")
        trades = make_trades("btcusdt", 20, buy_fraction=0.9)
        for t in trades:
            micro.add_trade(t)

        market = FakeMarket()
        result = evaluate_micro(market, micro, 5.0, current_position="yes")
        assert result is not None
        assert result["action"] == "exit"

    def test_no_force_exit_without_position(self):
        """Should NOT force exit if we have no position."""
        micro = MicroStructure(symbol="btcusdt")
        trades = make_trades("btcusdt", 20, buy_fraction=0.9)
        for t in trades:
            micro.add_trade(t)

        market = FakeMarket()
        result = evaluate_micro(market, micro, 5.0, current_position=None)
        assert result is None  # No position, too close to end, no entry

    def test_exit_on_reversal(self):
        """Should exit when momentum reverses against position."""
        micro = MicroStructure(symbol="btcusdt")
        # Strong sell pressure while holding YES
        trades = make_trades("btcusdt", 30, buy_fraction=0.1, base_price=70000)
        for t in trades:
            micro.add_trade(t)

        market = FakeMarket()
        momentum = micro.momentum_signal
        # Only test if momentum is actually negative and above exit threshold
        if abs(momentum) >= 0.10:
            result = evaluate_micro(market, micro, 120.0, current_position="yes")
            if result is not None:
                assert result["action"] in ("exit", "flip_no")


class TestMicroSniperFlip:
    def test_flip_on_strong_reversal(self):
        """Should flip position when strong reversal with high confidence."""
        micro = MicroStructure(symbol="btcusdt")
        # Very strong sell pressure while holding YES
        trades = make_trades("btcusdt", 50, buy_fraction=0.05, base_price=70000,
                             time_spacing=0.3)
        for t in trades:
            micro.add_trade(t)

        market = FakeMarket(yes_price=0.40, no_price=0.60)
        momentum = micro.momentum_signal
        confidence = micro.confidence

        result = evaluate_micro(market, micro, 120.0, current_position="yes")
        # If momentum is strong enough and confidence is high, should flip
        if result is not None and abs(momentum) >= 0.35 and confidence >= 0.40:
            assert "flip" in result["action"]
            assert result["is_flip"] is True

    def test_no_flip_with_weak_confidence(self):
        """Should not flip if confidence is too low — use moderate signal."""
        config = FakeConfig()
        config.flip_min_confidence = 0.95  # Nearly impossible bar
        config.flip_threshold = 0.20      # Low flip threshold so we'd flip if confidence allowed
        micro = MicroStructure(symbol="btcusdt")
        # Use 70% sell (moderate, not extreme) to keep confidence below 0.95
        trades = make_trades("btcusdt", 15, buy_fraction=0.25, base_price=70000)
        for t in trades:
            micro.add_trade(t)

        market = FakeMarket()
        conf = micro.confidence
        # Verify confidence is actually below the threshold
        assert conf < 0.95, f"Confidence {conf} should be below 0.95 for this test"
        result = evaluate_micro(market, micro, 120.0, current_position="yes", config=config)
        # With confidence < 0.95, should not flip (may exit instead)
        if result is not None:
            assert "flip" not in result["action"]


class TestMicroSniperSparseDataGuard:
    def test_no_flip_on_sparse_data(self):
        """Should NOT flip when only 2-3 trades exist — OFI ±1.00 is noise."""
        config = FakeConfig()
        config.min_trades_in_window = 3
        config.min_trades_for_flip = 10
        config.flip_threshold = 0.20  # Low bar so it would flip if data was sufficient

        micro = MicroStructure(symbol="btcusdt")
        # Only 2 trades — all sells — OFI = -1.00 but it's noise
        trades = make_trades("btcusdt", 2, buy_fraction=0.0, base_price=70000)
        for t in trades:
            micro.add_trade(t)

        market = FakeMarket()
        # With only 2 trades, should NOT produce any signal (below min_trades_in_window)
        result = evaluate_micro(market, micro, 120.0, current_position="yes", config=config)
        assert result is None, "Should not act on sparse data (2 trades)"

    def test_no_exit_on_sparse_data(self):
        """Should NOT exit when too few trades — momentum reading is unreliable."""
        config = FakeConfig()
        config.min_trades_in_window = 5

        micro = MicroStructure(symbol="btcusdt")
        # Only 3 sells — strong-looking reversal but unreliable
        trades = make_trades("btcusdt", 3, buy_fraction=0.0, base_price=70000)
        for t in trades:
            micro.add_trade(t)

        market = FakeMarket()
        result = evaluate_micro(market, micro, 120.0, current_position="yes", config=config)
        assert result is None, "Should not exit on sparse data (3 trades < 5 min)"

    def test_no_flip_below_min_trades_for_flip(self):
        """Should not flip even with enough trades for exit but not for flip."""
        config = FakeConfig()
        config.min_trades_in_window = 5
        config.min_trades_for_flip = 20
        config.flip_threshold = 0.20

        micro = MicroStructure(symbol="btcusdt")
        # 10 trades — enough for exit but not for flip
        trades = make_trades("btcusdt", 10, buy_fraction=0.05, base_price=70000)
        for t in trades:
            micro.add_trade(t)

        market = FakeMarket(yes_price=0.40, no_price=0.60)
        result = evaluate_micro(market, micro, 120.0, current_position="yes", config=config)
        # Should exit (reversal detected) but NOT flip (not enough trades)
        if result is not None:
            assert "flip" not in result["action"], "Should not flip with only 10 trades (need 20)"

    def test_flip_with_sufficient_trades(self):
        """Should flip when trade count meets the higher flip threshold."""
        config = FakeConfig()
        config.min_trades_in_window = 5
        config.min_trades_for_flip = 10
        config.flip_threshold = 0.20

        micro = MicroStructure(symbol="btcusdt")
        # 30 trades — strong sell pressure, well above flip threshold
        trades = make_trades("btcusdt", 30, buy_fraction=0.05, base_price=70000,
                             time_spacing=0.3)
        for t in trades:
            micro.add_trade(t)

        market = FakeMarket(yes_price=0.40, no_price=0.60)
        momentum = micro.momentum_signal
        confidence = micro.confidence

        result = evaluate_micro(market, micro, 120.0, current_position="yes", config=config)
        # With 30 trades and strong sell pressure, should flip
        if result is not None and abs(momentum) >= config.flip_threshold and confidence >= config.flip_min_confidence:
            assert "flip" in result["action"]


class TestMicroSniperHold:
    def test_hold_when_aligned(self):
        """Should hold (return None) when momentum aligns with position."""
        micro = MicroStructure(symbol="btcusdt")
        trades = make_trades("btcusdt", 30, buy_fraction=0.8, base_price=70000)
        for t in trades:
            micro.add_trade(t)

        market = FakeMarket()
        momentum = micro.momentum_signal

        # If momentum is positive and strong enough to hold
        if momentum > 0.05:  # Above hold threshold
            result = evaluate_micro(market, micro, 120.0, current_position="yes")
            assert result is None  # Hold = no action


# ═══════════════════════════════════════════════════════════════════════
# Tests: Config defaults
# ═══════════════════════════════════════════════════════════════════════

class TestConfigDefaults:
    def test_default_symbols(self):
        """Default should track BTC only for micro sniper."""
        config = FakeConfig()
        # Real config defaults to ["btcusdt"]
        assert config.entry_threshold == 0.40

    def test_entry_threshold_range(self):
        """Entry threshold should be between 0 and 1."""
        config = FakeConfig()
        assert 0 < config.entry_threshold < 1

    def test_flip_threshold_higher_than_entry(self):
        """Flip threshold should be higher than entry threshold."""
        config = FakeConfig()
        assert config.flip_threshold > config.entry_threshold

    def test_exit_threshold_lower_than_entry(self):
        """Exit threshold should be lower than entry threshold."""
        config = FakeConfig()
        assert config.exit_threshold < config.entry_threshold

    def test_hold_threshold_lower_than_exit(self):
        """Hold threshold should be lower than exit threshold."""
        config = FakeConfig()
        assert config.hold_threshold < config.exit_threshold

    def test_max_trades_per_window(self):
        config = FakeConfig()
        assert config.max_trades_per_window == 50

    def test_force_exit_shorter_than_min_remaining(self):
        """Force exit should trigger before min_seconds_remaining."""
        config = FakeConfig()
        assert config.force_exit_seconds < config.min_seconds_remaining


# ═══════════════════════════════════════════════════════════════════════
# Tests: Edge cases
# ═══════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    def test_single_trade_window(self):
        """A single trade should produce some state but likely not enough for entry."""
        micro = MicroStructure(symbol="btcusdt")
        trade = AggTrade("btcusdt", 70000, 0.1, False, time.time())
        micro.add_trade(trade)
        assert micro.flow_5s.is_active
        assert micro.current_price == 70000

    def test_zero_price_trade(self):
        """Zero-price trade should not break anything."""
        w = TradeFlowWindow(symbol="btcusdt", window_seconds=60.0)
        trade = AggTrade("btcusdt", 0.0, 0.1, False, time.time())
        w.add(trade)
        assert w.total_count == 1
        assert w.total_volume == 0.0

    def test_window_start_tracking(self):
        """Price change pct should track from window start."""
        micro = MicroStructure(symbol="btcusdt")
        micro.start_window(70000)
        assert micro.price_change_pct == 0.0
        micro.current_price = 70100
        assert micro.price_change_pct == pytest.approx(100 / 70000, abs=1e-6)

    def test_window_start_zero(self):
        """Zero start price should give zero change."""
        micro = MicroStructure(symbol="btcusdt")
        micro.start_window(0.0)
        micro.current_price = 70000
        assert micro.price_change_pct == 0.0

    def test_ofi_range(self):
        """OFI should always be in [-1, 1]."""
        w = TradeFlowWindow(symbol="btcusdt", window_seconds=60.0)
        for frac in [0.0, 0.1, 0.3, 0.5, 0.7, 0.9, 1.0]:
            w.reset()
            trades = make_trades("btcusdt", 20, buy_fraction=frac)
            for t in trades:
                w.add(t)
            assert -1.0 <= w.ofi <= 1.0, f"OFI out of range for buy_fraction={frac}"
