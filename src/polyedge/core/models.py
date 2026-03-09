"""Pydantic data models for PolyEdge."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Side(str, Enum):
    YES = "YES"
    NO = "NO"


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class TradeStatus(str, Enum):
    OPEN = "OPEN"
    WON = "WON"
    LOST = "LOST"
    EXITED = "EXITED"


class AgentMode(str, Enum):
    AUTOPILOT = "autopilot"
    COPILOT = "copilot"
    SIGNALS = "signals"


class Market(BaseModel):
    """A Polymarket prediction market."""

    condition_id: str
    question: str
    slug: str = ""
    description: str = ""
    category: str = ""
    end_date: Optional[datetime] = None
    active: bool = True
    closed: bool = False

    # Token IDs for YES/NO outcomes
    clob_token_ids: list[str] = Field(default_factory=list)

    # Current prices (0-1)
    yes_price: float = 0.0
    no_price: float = 0.0

    # Market stats
    volume: float = 0.0
    liquidity: float = 0.0
    spread: float = 0.0

    # Raw data
    raw: dict = Field(default_factory=dict)

    @property
    def yes_token_id(self) -> str | None:
        return self.clob_token_ids[0] if self.clob_token_ids else None

    @property
    def no_token_id(self) -> str | None:
        return self.clob_token_ids[1] if len(self.clob_token_ids) > 1 else None

    @property
    def implied_probability(self) -> float:
        return self.yes_price

    @property
    def hours_to_resolution(self) -> float | None:
        if not self.end_date:
            return None
        delta = self.end_date - datetime.now(UTC)
        return max(delta.total_seconds() / 3600, 0)


class OrderBookLevel(BaseModel):
    price: float
    size: float


class OrderBook(BaseModel):
    market_id: str
    token_id: str
    bids: list[OrderBookLevel] = Field(default_factory=list)
    asks: list[OrderBookLevel] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None

    @property
    def midpoint(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return None


class Signal(BaseModel):
    """A trading signal from a strategy."""

    market: Market
    side: Side
    confidence: float = Field(ge=0.0, le=1.0)
    edge: float  # Estimated edge (our probability - market price)
    ev: float  # Expected value per dollar
    reasoning: str = ""
    strategy: str = ""
    ai_probability: Optional[float] = None
    suggested_size_usd: float = 0.0
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Order(BaseModel):
    """An order on Polymarket."""

    order_id: str = ""
    market_id: str
    token_id: str
    side: Side
    order_type: OrderType = OrderType.LIMIT
    price: float
    size: float  # In contracts
    amount_usd: float  # Dollar value
    status: OrderStatus = OrderStatus.PENDING
    filled_size: float = 0.0
    filled_avg_price: float = 0.0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    strategy: str = ""


class Position(BaseModel):
    """An open position on Polymarket."""

    market_id: str
    token_id: str
    question: str = ""
    side: Side
    size: float  # Number of contracts
    entry_price: float
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    strategy: str = ""
    opened_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def cost_basis(self) -> float:
        return self.size * self.entry_price

    @property
    def current_value(self) -> float:
        return self.size * self.current_price

    @property
    def pnl_pct(self) -> float:
        if self.cost_basis == 0:
            return 0.0
        return (self.current_value - self.cost_basis) / self.cost_basis


class Trade(BaseModel):
    """A completed trade record."""

    trade_id: str = ""
    market_id: str
    token_id: str
    question: str = ""
    side: Side
    entry_price: float
    exit_price: Optional[float] = None
    size: float
    pnl: float = 0.0
    status: TradeStatus = TradeStatus.OPEN
    strategy: str = ""
    reasoning: str = ""
    ai_probability: Optional[float] = None
    opened_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    closed_at: Optional[datetime] = None


class AIAnalysis(BaseModel):
    """Result of AI market analysis."""

    market_id: str
    question: str
    probability: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    risk_factors: list[str] = Field(default_factory=list)
    news_context: str = ""
    provider: str = ""  # "claude" | "openai" | "ensemble"
    model: str = ""
    cost_usd: float = 0.0
    cached: bool = False
    analyzed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PortfolioSnapshot(BaseModel):
    """Point-in-time portfolio state."""

    bankroll: float
    total_exposure: float = 0.0
    positions_count: int = 0
    unrealized_pnl: float = 0.0
    realized_pnl_today: float = 0.0
    trades_today: int = 0
    peak_bankroll: float = 0.0
    drawdown_pct: float = 0.0
    ai_cost_today: float = 0.0
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def exposure_pct(self) -> float:
        if self.bankroll == 0:
            return 0.0
        return self.total_exposure / self.bankroll
