"""Market Maker — provide liquidity and capture spreads."""

from __future__ import annotations

import logging
from typing import Optional

from polyedge.core.config import Settings
from polyedge.core.client import PolyClient
from polyedge.core.models import Market, OrderBook, Side

logger = logging.getLogger("polyedge.mm")


class MarketMakerStrategy:
    """Provide liquidity on both sides of a market and capture the spread.

    Risk: Adverse selection (informed traders fill your orders before you
    can cancel). Mitigated by:
    - Only market-making on liquid, slow-moving markets
    - Keeping inventory balanced
    - Auto-canceling near resolution or on big moves
    """

    name = "market_maker"

    def __init__(self, settings: Settings, client: PolyClient):
        self.settings = settings
        self.config = settings.strategies.market_maker
        self.client = client

        # Inventory tracking
        self.inventory: dict[str, float] = {}  # token_id -> net position
        self.active_orders: dict[str, dict] = {}  # order_id -> order info

    def should_quote(self, market: Market, book: OrderBook) -> bool:
        """Check if we should provide quotes for this market."""
        if not self.config.enabled:
            return False

        # Need sufficient spread to be profitable
        if book.spread is None or book.spread < self.config.min_spread:
            return False

        # Don't market-make near resolution (too much adverse selection)
        hours = market.hours_to_resolution
        if hours is not None and hours < self.settings.risk.min_time_to_resolution_hours:
            return False

        # Need sufficient liquidity
        if market.liquidity < self.settings.risk.min_liquidity:
            return False

        return True

    def calculate_quotes(
        self,
        market: Market,
        book: OrderBook,
        bankroll: float,
    ) -> Optional[dict]:
        """Calculate bid and ask prices for market making.

        Returns dict with bid_price, ask_price, bid_size, ask_size
        or None if we shouldn't quote.
        """
        if not self.should_quote(market, book):
            return None

        mid = book.midpoint
        if mid is None:
            return None

        spread = book.spread
        token_id = book.token_id

        # Our target spread: wider than current to reduce adverse selection
        our_half_spread = max(spread * 0.4, 0.02)  # At least 2 cents each side

        bid_price = round(mid - our_half_spread, 2)
        ask_price = round(mid + our_half_spread, 2)

        # Clamp prices
        bid_price = max(0.01, min(0.98, bid_price))
        ask_price = max(0.02, min(0.99, ask_price))

        if ask_price <= bid_price:
            return None

        # Size based on bankroll and max inventory
        max_inventory_usd = bankroll * self.config.max_inventory_pct
        current_inventory = abs(self.inventory.get(token_id, 0))

        remaining_capacity = max(0, max_inventory_usd - current_inventory)
        base_size = min(remaining_capacity / mid, 50)  # Max 50 contracts per side

        if base_size < 1:
            return None

        # Skew based on inventory
        net_position = self.inventory.get(token_id, 0)
        if net_position > 0:
            # Long inventory — reduce bid, increase ask
            bid_size = base_size * 0.5
            ask_size = base_size * 1.5
        elif net_position < 0:
            # Short inventory — increase bid, reduce ask
            bid_size = base_size * 1.5
            ask_size = base_size * 0.5
        else:
            bid_size = base_size
            ask_size = base_size

        return {
            "bid_price": bid_price,
            "ask_price": ask_price,
            "bid_size": round(bid_size, 1),
            "ask_size": round(ask_size, 1),
            "mid": mid,
            "spread": ask_price - bid_price,
        }

    def update_inventory(self, token_id: str, side: str, size: float):
        """Update inventory after a fill."""
        current = self.inventory.get(token_id, 0)
        if side.upper() in ("YES", "BUY"):
            self.inventory[token_id] = current + size
        else:
            self.inventory[token_id] = current - size

    async def place_quotes(
        self,
        market: Market,
        book: OrderBook,
        bankroll: float,
    ) -> list[dict]:
        """Place bid and ask orders for market making."""
        quotes = self.calculate_quotes(market, book, bankroll)
        if not quotes:
            return []

        orders = []

        # Place bid (buy)
        try:
            bid_result = self.client.place_limit_order(
                token_id=book.token_id,
                side="YES",  # Buying YES token
                price=quotes["bid_price"],
                size=quotes["bid_size"],
            )
            orders.append({"side": "BUY", "price": quotes["bid_price"], "result": bid_result})
        except Exception as e:
            logger.warning(f"Bid placement failed: {e}")

        # Place ask (sell)
        try:
            ask_result = self.client.place_limit_order(
                token_id=book.token_id,
                side="NO",  # Selling YES token (equivalent to selling)
                price=quotes["ask_price"],
                size=quotes["ask_size"],
            )
            orders.append({"side": "SELL", "price": quotes["ask_price"], "result": ask_result})
        except Exception as e:
            logger.warning(f"Ask placement failed: {e}")

        return orders

    async def cancel_all_quotes(self):
        """Cancel all active market-making orders."""
        try:
            self.client.cancel_all_orders()
            self.active_orders.clear()
        except Exception as e:
            logger.error(f"Failed to cancel orders: {e}")
