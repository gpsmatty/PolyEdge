"""Base strategy interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from polyedge.core.config import Settings
from polyedge.core.models import Market, Signal


class Strategy(ABC):
    """Abstract base class for trading strategies."""

    name: str = "base"

    def __init__(self, settings: Settings):
        self.settings = settings

    @abstractmethod
    def evaluate(self, market: Market) -> Signal | None:
        """Evaluate a single market and return a signal, or None if no opportunity."""
        ...

    def evaluate_batch(self, markets: list[Market]) -> list[Signal]:
        """Evaluate multiple markets and return signals sorted by EV."""
        signals = []
        for market in markets:
            try:
                signal = self.evaluate(market)
                if signal is not None:
                    signals.append(signal)
            except Exception:
                continue
        signals.sort(key=lambda s: s.ev, reverse=True)
        return signals
