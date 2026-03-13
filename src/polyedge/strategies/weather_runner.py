"""Weather Runner — persistent loop that connects forecast data to Polymarket
weather markets and executes weather sniper trades.

This runs as a persistent async loop (separate from the 5-minute scan agent
and the crypto sniper).  It:
1. Periodically fetches active weather markets from Polymarket
2. Groups them by event (location + date + type)
3. Fetches ensemble forecasts from Open-Meteo / NOAA
4. Evaluates forecast probability vs market price
5. Detects neg-risk arbitrage on multi-bucket events
6. Executes trades when edge exceeds threshold

Usage:
    polyedge weather              # Run in copilot mode (confirm trades)
    polyedge weather --auto       # Run in autopilot mode (auto-execute)
    polyedge weather --dry        # Dry run — show opportunities but don't trade
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Optional

from rich.table import Table

from polyedge.core.config import Settings
from polyedge.core.client import PolyClient
from polyedge.core.console import console
from polyedge.core.db import Database
from polyedge.core.models import Market, Signal, Side
from polyedge.data.markets import fetch_active_markets
from polyedge.data.weather_feed import WeatherFeed, LOCATIONS
from polyedge.risk.sizing import calculate_position_size
from polyedge.strategies.weather_sniper import (
    WeatherSniperStrategy,
    WeatherOpportunity,
    NegRiskOpportunity,
    find_weather_markets,
    group_weather_events,
)

logger = logging.getLogger("polyedge.weather_runner")

# Intervals
MARKET_REFRESH_INTERVAL = 300     # 5 minutes — weather markets don't change fast
FORECAST_REFRESH_INTERVAL = 1800  # 30 minutes — forecasts update hourly
STATUS_INTERVAL = 60              # 1 minute


class WeatherRunner:
    """Persistent loop for weather sniper strategy."""

    def __init__(
        self,
        settings: Settings,
        client: PolyClient,
        db: Database,
        auto_execute: bool = False,
        dry_run: bool = False,
    ):
        self.settings = settings
        self.client = client
        self.db = db
        self.auto_execute = auto_execute
        self.dry_run = dry_run
        self.running = False

        # Strategy
        self.strategy = WeatherSniperStrategy(settings)
        self.weather_config = settings.strategies.weather_sniper

        # Weather data feed
        self.feed = WeatherFeed()

        # Active weather markets grouped by event
        self.weather_events: dict[str, list[Market]] = {}
        self.all_weather_markets: list[Market] = []

        # Track traded markets to avoid double-entry
        self._traded_markets: set[str] = set()

        # Stats
        self.opportunities_seen = 0
        self.neg_risk_seen = 0
        self.trades_executed = 0
        self.total_edge_captured = 0.0
        self.markets_evaluated = 0
        self.last_market_refresh = 0.0
        self.last_forecast_refresh = 0.0

    async def run(self):
        """Main weather sniper loop."""
        self.running = True

        mode = "DRY RUN" if self.dry_run else ("AUTOPILOT" if self.auto_execute else "COPILOT")
        console.print(f"[bold green]Weather Sniper started in {mode} mode")
        console.print(
            f"[dim]Min edge: {self.weather_config.min_edge:.0%} | "
            f"Min confidence: {self.weather_config.min_confidence:.0%} | "
            f"Neg-risk edge: {self.weather_config.min_neg_risk_edge:.0%} | "
            f"Locations: {', '.join(self.weather_config.locations)}[/dim]"
        )

        # Initial data load
        console.print("[dim]Fetching weather markets and forecasts...[/dim]")
        await self._refresh_markets()
        await self._refresh_forecasts_and_evaluate()

        # Start persistent loops
        tasks = [
            asyncio.create_task(self._market_refresh_loop()),
            asyncio.create_task(self._forecast_refresh_loop()),
            asyncio.create_task(self._status_loop()),
        ]

        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except KeyboardInterrupt:
            console.print("\n[yellow]Weather sniper stopped by user")
        finally:
            self.running = False
            await self.feed.close()
            for t in tasks:
                t.cancel()

    async def stop(self):
        self.running = False
        await self.feed.close()

    # --- Refresh loops ---

    async def _market_refresh_loop(self):
        """Periodically fetch active weather markets from Polymarket."""
        while self.running:
            await asyncio.sleep(MARKET_REFRESH_INTERVAL)
            try:
                await self._refresh_markets()
            except Exception as e:
                logger.error(f"Market refresh failed: {e}")

    async def _forecast_refresh_loop(self):
        """Periodically fetch forecasts and evaluate all markets."""
        while self.running:
            await asyncio.sleep(FORECAST_REFRESH_INTERVAL)
            try:
                self.feed.clear_cache()  # Force fresh data
                await self._refresh_forecasts_and_evaluate()
            except Exception as e:
                logger.error(f"Forecast refresh failed: {e}")

    async def _status_loop(self):
        """Periodically print status."""
        while self.running:
            await asyncio.sleep(STATUS_INTERVAL)
            n_markets = len(self.all_weather_markets)
            n_events = len(self.weather_events)
            console.print(
                f"[dim]Weather: {n_markets} markets in {n_events} events | "
                f"Evaluated: {self.markets_evaluated} | "
                f"Opportunities: {self.opportunities_seen} | "
                f"Neg-risk: {self.neg_risk_seen} | "
                f"Trades: {self.trades_executed}[/dim]"
            )

    # --- Market refresh ---

    async def _refresh_markets(self):
        """Fetch and filter for active weather markets."""
        try:
            all_markets, _ = await fetch_active_markets(
                self.settings,
                limit=100,
                min_liquidity=self.weather_config.min_liquidity,
            )
        except Exception as e:
            logger.warning(f"Failed to fetch markets: {e}")
            return

        # Filter to weather markets
        weather = find_weather_markets(all_markets)

        # Filter to configured locations only
        filtered = []
        for market in weather:
            parsed = self.strategy.parse_market(market)
            if parsed and parsed["location_id"] in self.weather_config.locations:
                filtered.append(market)

        self.all_weather_markets = filtered
        self.weather_events = group_weather_events(filtered)

        if filtered:
            logger.info(
                f"Tracking {len(filtered)} weather markets "
                f"in {len(self.weather_events)} events"
            )
            # Log a summary
            locations = set()
            for m in filtered:
                parsed = self.strategy.parse_market(m)
                if parsed:
                    locations.add(parsed["location_id"])
            console.print(
                f"[dim]Weather markets: {len(filtered)} across "
                f"{', '.join(LOCATIONS.get(l, {}).get('name', l) for l in locations)}[/dim]"
            )

    # --- Forecast & Evaluate ---

    async def _refresh_forecasts_and_evaluate(self):
        """Fetch forecasts and evaluate all weather markets."""
        if not self.all_weather_markets:
            return

        self.markets_evaluated = 0
        opportunities = []

        # Evaluate each market against ensemble forecast
        for market in self.all_weather_markets:
            if market.condition_id in self._traded_markets:
                continue

            parsed = self.strategy.parse_market(market)
            if not parsed or not parsed.get("location_id"):
                continue

            target_date = parsed.get("target_date")
            if not target_date:
                continue

            # Fetch ensemble forecast
            forecast = await self.feed.get_forecast(
                location_id=parsed["location_id"],
                target_date=target_date,
                metric=parsed["weather_type"],
            )

            if not forecast:
                continue

            self.markets_evaluated += 1

            # Evaluate
            opp = self.strategy.evaluate_with_forecast(market, forecast, parsed)
            if opp:
                opportunities.append(opp)

        # Check for neg-risk arbitrage on event groups
        neg_risk_opps = []
        for event_key, event_markets in self.weather_events.items():
            if len(event_markets) < 3:
                continue

            # Get location and date from first parseable market
            parsed = None
            for m in event_markets:
                parsed = self.strategy.parse_market(m)
                if parsed and parsed.get("target_date"):
                    break

            if not parsed or not parsed.get("target_date"):
                continue

            nr = self.strategy.detect_neg_risk(
                event_markets, parsed["location_id"], parsed["target_date"]
            )
            if nr:
                neg_risk_opps.append(nr)

        # Handle opportunities
        if opportunities:
            self.opportunities_seen += len(opportunities)

            # Sort by edge (highest first)
            opportunities.sort(key=lambda o: o.edge, reverse=True)

            console.print(
                f"\n[bold yellow]Found {len(opportunities)} weather opportunity(s)[/bold yellow]"
            )

            for opp in opportunities:
                await self._handle_opportunity(opp)

        if neg_risk_opps:
            self.neg_risk_seen += len(neg_risk_opps)
            for nr in neg_risk_opps:
                await self._handle_neg_risk(nr)

        if not opportunities and not neg_risk_opps and self.markets_evaluated > 0:
            console.print(
                f"[dim]Evaluated {self.markets_evaluated} markets — no edges found[/dim]"
            )

    # --- Opportunity handling ---

    async def _handle_opportunity(self, opp: WeatherOpportunity):
        """Handle a detected weather opportunity."""
        signal = self.strategy.opportunity_to_signal(opp)
        loc_name = LOCATIONS.get(opp.location_id, {}).get("name", opp.location_id)

        bucket_str = ""
        if opp.bucket_low is not None and opp.bucket_high is not None:
            if opp.bucket_low <= -50:
                bucket_str = f"below {opp.bucket_high:.0f}°F"
            elif opp.bucket_high >= 150:
                bucket_str = f"above {opp.bucket_low:.0f}°F"
            else:
                bucket_str = f"{opp.bucket_low:.0f}-{opp.bucket_high:.0f}°F"

        console.print(
            f"\n[bold yellow]WEATHER OPPORTUNITY[/bold yellow] "
            f"{loc_name} {bucket_str} {opp.side.value} "
            f"| Edge: {opp.edge:.1%} "
            f"| Forecast: {opp.forecast_prob:.1%} vs Market: {opp.market_price:.1%} "
            f"| Ensemble: mean={opp.ensemble_mean:.1f}°F, std={opp.ensemble_std:.1f}°F "
            f"({opp.n_ensemble_members} members) "
            f"| Confidence: {opp.confidence:.1%}"
        )

        if self.dry_run:
            console.print("[dim]  (dry run — not trading)[/dim]")
            return

        # Size the position
        bankroll = await self._get_bankroll()
        max_pct = min(
            self.weather_config.max_position_per_trade,
            self.settings.risk.max_position_pct,
        )

        size_usd = calculate_position_size(
            bankroll=bankroll,
            edge=opp.edge,
            probability=opp.forecast_prob,
            kelly_fraction=self.settings.risk.kelly_fraction,
            max_position_pct=max_pct,
        )

        if size_usd < 1.0:
            console.print("[dim]  Position too small — skipping[/dim]")
            return

        console.print(
            f"  [bold]Sizing: ${size_usd:.2f} "
            f"({size_usd / bankroll * 100:.1f}% of bankroll)[/bold]"
        )

        if self.auto_execute:
            await self._execute_trade(opp, signal, size_usd)
        else:
            try:
                response = input("  Execute trade? (y/n): ").strip().lower()
                if response == "y":
                    await self._execute_trade(opp, signal, size_usd)
                else:
                    console.print("[dim]  Skipped by user[/dim]")
            except EOFError:
                pass

    async def _handle_neg_risk(self, nr: NegRiskOpportunity):
        """Handle a neg-risk arbitrage opportunity."""
        loc_name = LOCATIONS.get(nr.location_id, {}).get("name", nr.location_id)

        console.print(
            f"\n[bold cyan]NEG-RISK ARBITRAGE[/bold cyan] "
            f"{loc_name} {nr.target_date} "
            f"| {len(nr.event_markets)} buckets "
            f"| YES sum: {nr.yes_price_sum:.3f} "
            f"| Edge: {nr.arb_edge:.1%} "
            f"| Direction: {nr.direction}"
        )

        if self.dry_run:
            console.print("[dim]  (dry run — not trading)[/dim]")
            return

        # Log for manual review — neg-risk execution is more complex
        # and needs careful order management across all buckets
        console.print(
            "[dim]  Neg-risk execution requires buying/selling across "
            f"{len(nr.event_markets)} markets simultaneously. "
            "Manual review recommended.[/dim]"
        )

    async def _execute_trade(
        self,
        opp: WeatherOpportunity,
        signal: Signal,
        size_usd: float,
    ):
        """Execute a weather trade."""
        from polyedge.execution.engine import ExecutionEngine

        engine = ExecutionEngine(self.client, self.db, self.settings)

        token_id = (
            opp.market.yes_token_id
            if opp.side == Side.YES
            else opp.market.no_token_id
        )
        if not token_id:
            console.print("[red]  No token ID — can't trade[/red]")
            return

        price = opp.market.yes_price if opp.side == Side.YES else opp.market.no_price
        size = size_usd / price if price > 0 else 0

        try:
            # Side is always "BUY" for entry — token_id determines YES vs NO.
            order_id = await engine.place_order(
                market=opp.market,
                token_id=token_id,
                side="BUY",
                price=price,
                size=size,
                amount_usd=size_usd,
                strategy="weather_sniper",
                reasoning=signal.reasoning,
                force=self.auto_execute,
            )

            if order_id:
                self._traded_markets.add(opp.market.condition_id)
                self.trades_executed += 1
                self.total_edge_captured += opp.edge
                console.print(f"[green]  TRADED! Order: {order_id}[/green]")
            else:
                console.print("[red]  Trade failed or rejected[/red]")

        except Exception as e:
            console.print(f"[red]  Execution error: {e}[/red]")
            logger.error(f"Weather execution failed: {e}", exc_info=True)

    async def _get_bankroll(self) -> float:
        """Get current bankroll."""
        try:
            positions = await self.db.get_open_positions()
            exposure = sum(p.get("size", 0) * p.get("entry_price", 0) for p in positions)
            return max(0, 200.0 - exposure)
        except Exception:
            return 200.0
