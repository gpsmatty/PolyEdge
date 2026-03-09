"""Fetch and filter markets from Polymarket's Gamma API."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import aiohttp

from polyedge.core.config import Settings
from polyedge.core.models import Market


async def fetch_active_markets(
    settings: Settings,
    limit: int = 100,
    offset: int = 0,
    category: Optional[str] = None,
    min_liquidity: float = 0,
    active: bool = True,
) -> list[Market]:
    """Fetch active markets from the Gamma API."""
    url = f"{settings.polymarket.gamma_url}/markets"
    params = {
        "limit": limit,
        "offset": offset,
        "active": str(active).lower(),
        "closed": "false",
        "order": "volume",
        "ascending": "false",
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Gamma API error: {resp.status}")
            data = await resp.json()

    markets = []
    for item in data:
        try:
            market = _parse_market(item)
            if market.liquidity >= min_liquidity:
                if category is None or market.category.lower() == category.lower():
                    markets.append(market)
        except (KeyError, ValueError):
            continue

    return markets


async def fetch_all_markets(
    settings: Settings,
    min_liquidity: float = 0,
    max_pages: int = 50,
) -> list[Market]:
    """Fetch ALL active markets, paginating with offset until exhausted.

    The Gamma API returns ~100 per page. We keep going until we get a
    page with fewer results than batch_size (meaning we hit the end).
    max_pages is a safety cap to prevent infinite loops.
    """
    all_markets = []
    batch_size = 100

    for page in range(max_pages):
        batch = await fetch_active_markets(
            settings,
            limit=batch_size,
            offset=page * batch_size,
            min_liquidity=min_liquidity,
        )
        all_markets.extend(batch)
        if len(batch) < batch_size:
            break  # Last page — no more results

    return all_markets


async def fetch_events(
    settings: Settings,
    limit: int = 50,
) -> list[dict]:
    """Fetch events (groups of markets) from Gamma API."""
    url = f"{settings.polymarket.gamma_url}/events"
    params = {
        "limit": limit,
        "active": "true",
        "closed": "false",
        "order": "volume",
        "ascending": "false",
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Gamma API error: {resp.status}")
            return await resp.json()


async def search_markets(
    settings: Settings,
    query: str,
    limit: int = 20,
) -> list[Market]:
    """Search markets by keyword."""
    all_markets = await fetch_all_markets(settings)
    query_lower = query.lower()
    return [
        m
        for m in all_markets
        if query_lower in m.question.lower() or query_lower in m.description.lower()
    ]


def _parse_market(data: dict) -> Market:
    """Parse a Gamma API market response into our Market model."""
    # Parse outcome prices
    outcome_prices = data.get("outcomePrices", "")
    yes_price = 0.0
    no_price = 0.0
    if outcome_prices:
        try:
            if isinstance(outcome_prices, str):
                import json

                prices = json.loads(outcome_prices)
            else:
                prices = outcome_prices
            if len(prices) >= 2:
                yes_price = float(prices[0])
                no_price = float(prices[1])
        except (json.JSONDecodeError, ValueError, IndexError):
            pass

    # Parse token IDs
    clob_token_ids = []
    token_ids_raw = data.get("clobTokenIds", "")
    if token_ids_raw:
        try:
            if isinstance(token_ids_raw, str):
                import json

                clob_token_ids = json.loads(token_ids_raw)
            else:
                clob_token_ids = token_ids_raw
        except (json.JSONDecodeError, ValueError):
            pass

    # Parse end date
    end_date = None
    end_date_raw = data.get("endDate") or data.get("end_date_iso")
    if end_date_raw:
        try:
            if isinstance(end_date_raw, str):
                end_date = datetime.fromisoformat(end_date_raw.replace("Z", "+00:00"))
        except ValueError:
            pass

    return Market(
        condition_id=data.get("conditionId") or data.get("condition_id", ""),
        question=data.get("question", ""),
        slug=data.get("slug", ""),
        description=data.get("description", ""),
        category=data.get("groupItemTitle", "") or data.get("category", ""),
        end_date=end_date,
        active=data.get("active", True),
        closed=data.get("closed", False),
        clob_token_ids=clob_token_ids,
        yes_price=yes_price,
        no_price=no_price,
        volume=float(data.get("volume", 0) or 0),
        liquidity=float(data.get("liquidity", 0) or 0),
        spread=abs(yes_price - (1 - no_price)) if yes_price and no_price else 0,
        raw=data,
    )
