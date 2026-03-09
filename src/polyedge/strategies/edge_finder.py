"""Edge Finder — use AI and external data to find mispricings."""

from __future__ import annotations

from polyedge.core.config import Settings
from polyedge.core.models import AIAnalysis, Market, Signal, Side
from polyedge.strategies.base import Strategy


class EdgeFinderStrategy(Strategy):
    """Find markets where AI probability estimate differs from market price.

    This strategy requires AI analysis to be run separately — it takes
    pre-computed AIAnalysis objects and converts them to trade signals.
    """

    name = "edge_finder"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.config = settings.strategies.edge_finder

    def evaluate(self, market: Market) -> Signal | None:
        """Basic evaluation without AI (fallback)."""
        # Without AI analysis, we can only use simple heuristics
        # Real edge detection happens in evaluate_with_analysis()
        return None

    def evaluate_with_analysis(
        self, market: Market, analysis: AIAnalysis
    ) -> Signal | None:
        """Evaluate a market using AI analysis results."""
        if not self.config.enabled:
            return None

        if analysis.confidence < self.settings.risk.min_confidence:
            return None

        # Calculate edge
        edge = analysis.probability - market.yes_price

        if abs(edge) < self.config.min_edge:
            return None

        # Determine direction
        if edge > 0:
            side = Side.YES
            price = market.yes_price
        else:
            side = Side.NO
            price = market.no_price
            edge = abs(edge)

        # EV per dollar
        ev = edge / price if price > 0 else 0

        return Signal(
            market=market,
            side=side,
            confidence=analysis.confidence,
            edge=edge,
            ev=ev,
            reasoning=(
                f"AI estimates {analysis.probability*100:.1f}% vs market {market.yes_price*100:.1f}%. "
                f"Edge: {edge*100:.1f}%. "
                f"Reasoning: {analysis.reasoning[:200]}"
            ),
            strategy=self.name,
            ai_probability=analysis.probability,
        )

    def evaluate_batch_with_analyses(
        self,
        markets: list[Market],
        analyses: dict[str, AIAnalysis],
    ) -> list[Signal]:
        """Evaluate multiple markets with their AI analyses."""
        signals = []
        for market in markets:
            analysis = analyses.get(market.condition_id)
            if analysis:
                signal = self.evaluate_with_analysis(market, analysis)
                if signal:
                    signals.append(signal)
        signals.sort(key=lambda s: s.ev, reverse=True)
        return signals
