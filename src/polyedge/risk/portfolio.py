"""Portfolio-level risk management."""

from __future__ import annotations

from dataclasses import dataclass

from polyedge.core.config import RiskConfig
from polyedge.core.models import PortfolioSnapshot


@dataclass
class RiskCheck:
    passed: bool
    reason: str = ""


class PortfolioRiskManager:
    """Enforces portfolio-level risk limits."""

    def __init__(self, config: RiskConfig):
        self.config = config
        self.peak_bankroll: float = 0.0

    def check_can_trade(self, snapshot: PortfolioSnapshot) -> RiskCheck:
        """Check all risk limits before allowing a new trade."""
        # Max positions
        if snapshot.positions_count >= self.config.max_positions:
            return RiskCheck(False, f"Max positions ({self.config.max_positions}) reached")

        # Max exposure
        if snapshot.exposure_pct >= self.config.max_exposure_pct:
            return RiskCheck(
                False,
                f"Max exposure ({self.config.max_exposure_pct*100:.0f}%) reached "
                f"(current: {snapshot.exposure_pct*100:.1f}%)",
            )

        # Daily trade limit
        if snapshot.trades_today >= self.config.max_trades_per_day:
            return RiskCheck(False, f"Daily trade limit ({self.config.max_trades_per_day}) reached")

        # Daily loss limit
        if snapshot.bankroll > 0:
            daily_loss_pct = abs(snapshot.realized_pnl_today) / snapshot.bankroll
            if snapshot.realized_pnl_today < 0 and daily_loss_pct >= self.config.daily_loss_limit_pct:
                return RiskCheck(
                    False,
                    f"Daily loss limit ({self.config.daily_loss_limit_pct*100:.0f}%) reached "
                    f"(current: {daily_loss_pct*100:.1f}%)",
                )

        # Drawdown circuit breaker
        self.peak_bankroll = max(self.peak_bankroll, snapshot.bankroll)
        if self.peak_bankroll > 0:
            drawdown = (self.peak_bankroll - snapshot.bankroll) / self.peak_bankroll
            if drawdown >= self.config.drawdown_circuit_breaker:
                return RiskCheck(
                    False,
                    f"Drawdown circuit breaker ({self.config.drawdown_circuit_breaker*100:.0f}%) triggered "
                    f"(current: {drawdown*100:.1f}%)",
                )

        # AI cost budget
        if snapshot.ai_cost_today >= 10.0:  # Hard cap on AI spend
            return RiskCheck(False, f"AI cost budget exceeded (${snapshot.ai_cost_today:.2f})")

        return RiskCheck(True)

    def check_position_size(
        self,
        amount_usd: float,
        bankroll: float,
    ) -> RiskCheck:
        """Check if a position size is within limits."""
        if bankroll <= 0:
            return RiskCheck(False, "No bankroll available")

        position_pct = amount_usd / bankroll
        if position_pct > self.config.max_position_pct:
            return RiskCheck(
                False,
                f"Position size ({position_pct*100:.1f}%) exceeds max ({self.config.max_position_pct*100:.0f}%)",
            )

        return RiskCheck(True)

    def check_category_exposure(
        self,
        category: str,
        positions: list[dict],
        amount_usd: float,
        bankroll: float,
    ) -> RiskCheck:
        """Check if we're overexposed to a single category."""
        if category in self.config.categories_blacklist:
            return RiskCheck(False, f"Category '{category}' is blacklisted")

        # Max 30% in any single category
        category_exposure = sum(
            p.get("size", 0) * p.get("entry_price", 0)
            for p in positions
            if p.get("category", "").lower() == category.lower()
        )
        new_exposure = (category_exposure + amount_usd) / bankroll if bankroll > 0 else 1
        if new_exposure > 0.30:
            return RiskCheck(
                False,
                f"Category '{category}' exposure would be {new_exposure*100:.0f}% (max 30%)",
            )

        return RiskCheck(True)
