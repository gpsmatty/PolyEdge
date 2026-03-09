"""Order book and pricing data from Polymarket CLOB."""

from __future__ import annotations

from polyedge.core.client import PolyClient
from polyedge.core.models import Market, OrderBook, OrderBookLevel


def get_order_book(client: PolyClient, market: Market, side: str = "YES") -> OrderBook:
    """Get the order book for a market's YES or NO token."""
    token_id = market.yes_token_id if side.upper() == "YES" else market.no_token_id
    if not token_id:
        raise ValueError(f"No {side} token ID for market {market.condition_id}")

    raw = client.get_order_book(token_id)

    bids = [
        OrderBookLevel(price=float(b["price"]), size=float(b["size"]))
        for b in raw.get("bids", [])
    ]
    asks = [
        OrderBookLevel(price=float(a["price"]), size=float(a["size"]))
        for a in raw.get("asks", [])
    ]

    # Sort bids descending, asks ascending
    bids.sort(key=lambda x: x.price, reverse=True)
    asks.sort(key=lambda x: x.price)

    return OrderBook(
        market_id=market.condition_id,
        token_id=token_id,
        bids=bids,
        asks=asks,
    )


def get_prices(client: PolyClient, market: Market) -> dict:
    """Get current YES and NO prices for a market."""
    prices = {}
    if market.yes_token_id:
        try:
            resp = client.get_price(market.yes_token_id)
            prices["yes"] = float(resp.get("price", 0))
        except Exception:
            prices["yes"] = market.yes_price

    if market.no_token_id:
        try:
            resp = client.get_price(market.no_token_id)
            prices["no"] = float(resp.get("price", 0))
        except Exception:
            prices["no"] = market.no_price

    prices["spread"] = abs(prices.get("yes", 0) + prices.get("no", 0) - 1.0)
    return prices


def get_midpoint(client: PolyClient, market: Market) -> float:
    """Get the midpoint price for a market's YES token."""
    if not market.yes_token_id:
        return market.yes_price
    return client.get_midpoint(market.yes_token_id)
