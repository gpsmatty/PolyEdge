"""Order book microstructure analysis — imbalance, depth, whale detection."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from polyedge.core.client import PolyClient
from polyedge.core.models import Market, OrderBook, OrderBookLevel

logger = logging.getLogger("polyedge.book_analyzer")


@dataclass
class BookIntelligence:
    """Distilled order book intelligence for a market."""

    market_id: str
    token_id: str

    # Price levels
    best_bid: float = 0.0
    best_ask: float = 0.0
    midpoint: float = 0.0
    spread: float = 0.0
    spread_bps: float = 0.0  # Spread in basis points

    # Depth (total size within N cents of best bid/ask)
    bid_depth_5c: float = 0.0  # Total bid size within 5 cents
    ask_depth_5c: float = 0.0
    bid_depth_10c: float = 0.0  # Total bid size within 10 cents
    ask_depth_10c: float = 0.0

    # Imbalance (positive = more buy pressure, negative = more sell pressure)
    imbalance_ratio: float = 0.0  # (bid_depth - ask_depth) / total, range [-1, 1]
    imbalance_5c: float = 0.0  # Near-touch imbalance
    imbalance_10c: float = 0.0  # Wider imbalance

    # Whale detection
    whale_bids: list[OrderBookLevel] = None  # Bids > 2x avg size
    whale_asks: list[OrderBookLevel] = None
    largest_bid: float = 0.0
    largest_ask: float = 0.0
    whale_bias: str = ""  # "buy", "sell", "neutral"

    # Wall detection
    bid_wall_price: Optional[float] = None  # Massive support level
    ask_wall_price: Optional[float] = None  # Massive resistance level

    # Book quality
    num_bid_levels: int = 0
    num_ask_levels: int = 0
    total_bid_size: float = 0.0
    total_ask_size: float = 0.0

    def __post_init__(self):
        if self.whale_bids is None:
            self.whale_bids = []
        if self.whale_asks is None:
            self.whale_asks = []

    def summary(self) -> str:
        """Human-readable summary for AI prompts."""
        lines = [
            f"Spread: {self.spread:.4f} ({self.spread_bps:.0f}bps)",
            f"Midpoint: {self.midpoint:.4f}",
            f"Book imbalance: {self.imbalance_ratio:+.2f} ({'BUY pressure' if self.imbalance_ratio > 0.1 else 'SELL pressure' if self.imbalance_ratio < -0.1 else 'balanced'})",
            f"Near-touch (5c) imbalance: {self.imbalance_5c:+.2f}",
            f"Bid depth (5c/10c): {self.bid_depth_5c:.0f} / {self.bid_depth_10c:.0f}",
            f"Ask depth (5c/10c): {self.ask_depth_5c:.0f} / {self.ask_depth_10c:.0f}",
            f"Bid levels: {self.num_bid_levels} | Ask levels: {self.num_ask_levels}",
        ]

        if self.whale_bids:
            lines.append(
                f"Whale bids: {len(self.whale_bids)} (largest: {self.largest_bid:.0f} contracts at ${self.whale_bids[0].price:.3f})"
            )
        if self.whale_asks:
            lines.append(
                f"Whale asks: {len(self.whale_asks)} (largest: {self.largest_ask:.0f} contracts at ${self.whale_asks[0].price:.3f})"
            )
        if self.bid_wall_price is not None:
            lines.append(f"Support wall at ${self.bid_wall_price:.3f}")
        if self.ask_wall_price is not None:
            lines.append(f"Resistance wall at ${self.ask_wall_price:.3f}")
        if self.whale_bias != "neutral":
            lines.append(f"Whale bias: {self.whale_bias.upper()}")

        return "\n".join(lines)


def analyze_book(book: OrderBook) -> BookIntelligence:
    """Analyze an order book and extract trading intelligence.

    This is the core function — takes raw bids/asks and produces
    actionable intelligence about market microstructure.
    """
    intel = BookIntelligence(
        market_id=book.market_id,
        token_id=book.token_id,
    )

    if not book.bids and not book.asks:
        return intel

    # Basic price levels
    intel.best_bid = book.best_bid or 0.0
    intel.best_ask = book.best_ask or 0.0
    intel.midpoint = book.midpoint or 0.0
    intel.spread = book.spread or 0.0
    intel.spread_bps = (intel.spread / intel.midpoint * 10000) if intel.midpoint > 0 else 0

    # Level counts
    intel.num_bid_levels = len(book.bids)
    intel.num_ask_levels = len(book.asks)

    # Depth analysis — how much size is within N cents of best bid/ask
    intel.bid_depth_5c = _depth_within(book.bids, intel.best_bid, 0.05, side="bid")
    intel.ask_depth_5c = _depth_within(book.asks, intel.best_ask, 0.05, side="ask")
    intel.bid_depth_10c = _depth_within(book.bids, intel.best_bid, 0.10, side="bid")
    intel.ask_depth_10c = _depth_within(book.asks, intel.best_ask, 0.10, side="ask")

    # Total sizes
    intel.total_bid_size = sum(b.size for b in book.bids)
    intel.total_ask_size = sum(a.size for a in book.asks)

    # Imbalance calculations
    total = intel.total_bid_size + intel.total_ask_size
    if total > 0:
        intel.imbalance_ratio = (intel.total_bid_size - intel.total_ask_size) / total

    total_5c = intel.bid_depth_5c + intel.ask_depth_5c
    if total_5c > 0:
        intel.imbalance_5c = (intel.bid_depth_5c - intel.ask_depth_5c) / total_5c

    total_10c = intel.bid_depth_10c + intel.ask_depth_10c
    if total_10c > 0:
        intel.imbalance_10c = (intel.bid_depth_10c - intel.ask_depth_10c) / total_10c

    # Whale detection — orders significantly larger than average
    avg_bid_size = intel.total_bid_size / len(book.bids) if book.bids else 0
    avg_ask_size = intel.total_ask_size / len(book.asks) if book.asks else 0
    whale_threshold = 2.0  # 2x average = whale

    if avg_bid_size > 0:
        intel.whale_bids = [
            b for b in book.bids if b.size >= avg_bid_size * whale_threshold
        ]
        intel.whale_bids.sort(key=lambda x: x.size, reverse=True)
        if intel.whale_bids:
            intel.largest_bid = intel.whale_bids[0].size

    if avg_ask_size > 0:
        intel.whale_asks = [
            a for a in book.asks if a.size >= avg_ask_size * whale_threshold
        ]
        intel.whale_asks.sort(key=lambda x: x.size, reverse=True)
        if intel.whale_asks:
            intel.largest_ask = intel.whale_asks[0].size

    # Whale bias
    whale_bid_total = sum(w.size for w in intel.whale_bids)
    whale_ask_total = sum(w.size for w in intel.whale_asks)
    if whale_bid_total > whale_ask_total * 1.5:
        intel.whale_bias = "buy"
    elif whale_ask_total > whale_bid_total * 1.5:
        intel.whale_bias = "sell"
    else:
        intel.whale_bias = "neutral"

    # Wall detection — single level with disproportionate size
    wall_threshold = 5.0  # 5x average = wall
    if avg_bid_size > 0:
        for bid in book.bids:
            if bid.size >= avg_bid_size * wall_threshold:
                intel.bid_wall_price = bid.price
                break  # First (highest) wall

    if avg_ask_size > 0:
        for ask in book.asks:
            if ask.size >= avg_ask_size * wall_threshold:
                intel.ask_wall_price = ask.price
                break  # First (lowest) wall

    return intel


def _depth_within(
    levels: list[OrderBookLevel],
    reference_price: float,
    range_cents: float,
    side: str,
) -> float:
    """Sum the size of levels within range_cents of the reference price."""
    total = 0.0
    for level in levels:
        if side == "bid":
            if reference_price - level.price <= range_cents:
                total += level.size
        else:  # ask
            if level.price - reference_price <= range_cents:
                total += level.size
    return total


def get_book_intelligence(
    client: PolyClient,
    market: Market,
    side: str = "YES",
) -> BookIntelligence:
    """Fetch order book and analyze it in one call.

    Convenience wrapper: fetches the raw book from the CLOB API,
    then runs full microstructure analysis.
    """
    from polyedge.data.orderbook import get_order_book

    book = get_order_book(client, market, side)
    return analyze_book(book)


def get_full_book_intelligence(
    client: PolyClient,
    market: Market,
) -> dict[str, BookIntelligence]:
    """Get book intelligence for both YES and NO sides of a market."""
    result = {}

    if market.yes_token_id:
        try:
            result["YES"] = get_book_intelligence(client, market, "YES")
        except Exception as e:
            logger.debug(f"Failed to get YES book for {market.condition_id}: {e}")

    if market.no_token_id:
        try:
            result["NO"] = get_book_intelligence(client, market, "NO")
        except Exception as e:
            logger.debug(f"Failed to get NO book for {market.condition_id}: {e}")

    return result


def format_book_for_ai(intel: BookIntelligence) -> str:
    """Format book intelligence as context for AI analysis prompts.

    This is what gets injected into the analyst prompt so the AI
    can factor in order book microstructure.
    """
    if intel.best_bid == 0 and intel.best_ask == 0:
        return "Order book data unavailable."

    return f"""## Order Book Intelligence
{intel.summary()}

Interpretation hints:
- Imbalance > +0.3 suggests strong buy pressure (price likely to rise)
- Imbalance < -0.3 suggests strong sell pressure (price likely to fall)
- Whale orders may indicate informed trading or market manipulation
- Wide spreads (>200bps) indicate thin liquidity — harder to enter/exit
- Walls indicate strong support/resistance — price may bounce off them"""
