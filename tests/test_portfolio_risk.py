"""Tests for portfolio risk management."""

from polyedge.core.config import RiskConfig
from polyedge.core.models import PortfolioSnapshot
from polyedge.risk.portfolio import PortfolioRiskManager


def make_snapshot(**kwargs) -> PortfolioSnapshot:
    defaults = {
        "bankroll": 200.0,
        "total_exposure": 50.0,
        "positions_count": 3,
        "unrealized_pnl": 0.0,
        "realized_pnl_today": 0.0,
        "trades_today": 5,
        "peak_bankroll": 200.0,
        "drawdown_pct": 0.0,
        "ai_cost_today": 1.0,
    }
    defaults.update(kwargs)
    return PortfolioSnapshot(**defaults)


def test_allows_trade():
    """Should allow trade when within limits."""
    rm = PortfolioRiskManager(RiskConfig())
    check = rm.check_can_trade(make_snapshot())
    assert check.passed


def test_blocks_max_positions():
    """Should block when max positions reached."""
    rm = PortfolioRiskManager(RiskConfig(max_positions=3))
    check = rm.check_can_trade(make_snapshot(positions_count=3))
    assert not check.passed
    assert "positions" in check.reason.lower()


def test_blocks_max_exposure():
    """Should block when max exposure reached."""
    rm = PortfolioRiskManager(RiskConfig(max_exposure_pct=0.50))
    check = rm.check_can_trade(make_snapshot(total_exposure=110.0, bankroll=200.0))
    assert not check.passed
    assert "exposure" in check.reason.lower()


def test_blocks_daily_trade_limit():
    """Should block when daily trade limit hit."""
    rm = PortfolioRiskManager(RiskConfig(max_trades_per_day=5))
    check = rm.check_can_trade(make_snapshot(trades_today=5))
    assert not check.passed
    assert "trade limit" in check.reason.lower()


def test_blocks_daily_loss_limit():
    """Should block when daily loss limit hit."""
    rm = PortfolioRiskManager(RiskConfig(daily_loss_limit_pct=0.15))
    check = rm.check_can_trade(make_snapshot(realized_pnl_today=-35.0))
    assert not check.passed
    assert "loss limit" in check.reason.lower()


def test_drawdown_circuit_breaker():
    """Should trigger circuit breaker on drawdown."""
    rm = PortfolioRiskManager(RiskConfig(drawdown_circuit_breaker=0.25))
    rm.peak_bankroll = 200.0
    check = rm.check_can_trade(make_snapshot(bankroll=140.0))
    assert not check.passed
    assert "circuit breaker" in check.reason.lower()


def test_position_size_check():
    """Should validate position size."""
    rm = PortfolioRiskManager(RiskConfig(max_position_pct=0.10))

    ok = rm.check_position_size(15.0, 200.0)
    assert ok.passed

    too_big = rm.check_position_size(25.0, 200.0)
    assert not too_big.passed
