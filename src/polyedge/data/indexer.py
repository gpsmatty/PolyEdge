"""Market indexer — syncs Polymarket data to local DB to avoid excessive API calls."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from rich.console import Console

from polyedge.core.config import Settings
from polyedge.core.db import Database
from polyedge.core.models import Market
from polyedge.data.markets import fetch_all_markets, _parse_market

logger = logging.getLogger("polyedge.indexer")
console = Console()


class MarketIndexer:
    """Fetches markets from Gamma API and upserts them into Postgres.

    This avoids hitting the API on every scan. The agent reads from DB
    instead, and we only sync periodically (e.g. every 10-15 minutes).
    """

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self._last_sync: Optional[datetime] = None

    @property
    def minutes_since_sync(self) -> Optional[float]:
        if not self._last_sync:
            return None
        delta = datetime.now(timezone.utc) - self._last_sync
        return delta.total_seconds() / 60

    async def sync(self, force: bool = False) -> int:
        """Full sync: fetch all markets from API, upsert to DB, record prices.

        Returns the number of markets synced.
        """
        # Skip if recently synced (unless forced)
        if not force and self._last_sync:
            interval = self.settings.agent.sync_interval_minutes
            if self.minutes_since_sync < interval:
                logger.debug(
                    f"Skipping sync — last sync {self.minutes_since_sync:.1f}m ago "
                    f"(interval: {interval}m)"
                )
                return 0

        console.print("[dim]Syncing markets from Polymarket API...[/dim]")

        # Fetch all markets from Gamma API
        markets = await fetch_all_markets(
            self.settings,
            min_liquidity=0,  # Get everything, filter later
            max_pages=10,
        )

        if not markets:
            console.print("[yellow]No markets returned from API")
            return 0

        # Upsert each market + record price snapshot
        price_snapshots = []
        for market in markets:
            market_dict = {
                "condition_id": market.condition_id,
                "question": market.question,
                "slug": market.slug,
                "description": market.description,
                "category": market.category,
                "end_date": market.end_date,
                "active": market.active,
                "closed": market.closed,
                "clob_token_ids": market.clob_token_ids,
                "yes_price": market.yes_price,
                "no_price": market.no_price,
                "volume": market.volume,
                "liquidity": market.liquidity,
                "spread": market.spread,
                "raw": market.raw or {},
            }
            await self.db.upsert_market(market_dict)

            # Collect price snapshots for bulk insert
            price_snapshots.append((
                market.condition_id,
                market.yes_price,
                market.no_price,
                market.volume,
                market.liquidity,
                market.spread,
            ))

        # Bulk insert price history
        await self.db.bulk_record_prices(price_snapshots)

        # Deactivate markets that disappeared from API
        active_ids = [m.condition_id for m in markets]
        deactivated = await self.db.deactivate_missing_markets(active_ids)
        if deactivated:
            console.print(f"[dim]Deactivated {deactivated} markets no longer in API[/dim]")

        # Also close markets past their end date
        closed = await self.db.deactivate_past_end_date()
        if closed:
            console.print(f"[dim]Closed {closed} markets past end date[/dim]")

        self._last_sync = datetime.now(timezone.utc)
        console.print(
            f"[green]Synced {len(markets)} markets to DB "
            f"({len(price_snapshots)} price snapshots recorded)"
        )
        return len(markets)

    async def needs_sync(self) -> bool:
        """Check if we need a fresh sync based on stale data."""
        if self._last_sync is None:
            # Check if DB has any markets at all
            count = await self.db.get_market_count()
            return count == 0

        interval = self.settings.agent.sync_interval_minutes
        return self.minutes_since_sync >= interval

    async def get_markets(
        self,
        min_liquidity: float = 0,
        limit: int = 500,
    ) -> list[Market]:
        """Get markets from local DB. Falls back to API sync if DB is empty."""
        # Auto-sync if needed
        if await self.needs_sync():
            await self.sync()

        rows = await self.db.get_markets_from_db(
            active_only=True,
            min_liquidity=min_liquidity,
            limit=limit,
        )

        # Convert DB rows back to Market models
        markets = []
        for row in rows:
            try:
                import json
                clob_ids = row.get("clob_token_ids", "[]")
                if isinstance(clob_ids, str):
                    clob_ids = json.loads(clob_ids)

                markets.append(Market(
                    condition_id=row["condition_id"],
                    question=row.get("question", ""),
                    slug=row.get("slug", ""),
                    description=row.get("description", ""),
                    category=row.get("category", ""),
                    end_date=row.get("end_date"),
                    active=row.get("active", True),
                    closed=row.get("closed", False),
                    clob_token_ids=clob_ids,
                    yes_price=row.get("yes_price", 0),
                    no_price=row.get("no_price", 0),
                    volume=row.get("volume", 0),
                    liquidity=row.get("liquidity", 0),
                    spread=row.get("spread", 0),
                ))
            except Exception as e:
                logger.debug(f"Failed to parse market row: {e}")
                continue

        return markets

    async def get_price_movers(
        self,
        hours: int = 1,
        min_move_pct: float = 0.05,
        min_liquidity: float = 1000,
    ) -> list[dict]:
        """Find markets where price moved significantly in recent hours.

        These are the best candidates for AI analysis — something happened.
        """
        markets = await self.get_markets(min_liquidity=min_liquidity)
        movers = []

        for market in markets:
            history = await self.db.get_price_history(
                market.condition_id, hours=hours
            )
            if len(history) < 2:
                continue

            oldest = history[0]
            newest = history[-1]
            price_change = abs(newest["yes_price"] - oldest["yes_price"])

            if price_change >= min_move_pct:
                movers.append({
                    "market": market,
                    "price_change": price_change,
                    "old_price": oldest["yes_price"],
                    "new_price": newest["yes_price"],
                    "direction": "up" if newest["yes_price"] > oldest["yes_price"] else "down",
                })

        # Sort by biggest movers
        movers.sort(key=lambda m: m["price_change"], reverse=True)
        return movers
