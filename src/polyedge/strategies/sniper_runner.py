"""Sniper Runner — real-time loop that connects Binance prices to ALL Polymarket
crypto markets and executes sniper trades.

Handles three market types via a unified pipeline:
1. Up/Down — short-duration direction bets (evaluated on every price tick)
2. Threshold — "above X" price level bets (evaluated on price ticks + periodic)
3. Bucket — "what price will X hit" range bets (evaluated periodically)

Usage:
    polyedge sniper          # Run in copilot mode (confirm trades)
    polyedge sniper --auto   # Run in autopilot mode (auto-execute)
    polyedge sniper --dry    # Dry run — show opportunities but don't trade
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.panel import Panel

from polyedge.core.config import Settings
from polyedge.core.client import PolyClient
from polyedge.core.db import Database
from polyedge.core.models import Market, Signal, Side
from polyedge.data.binance_feed import BinanceFeed, PriceSnapshot
from polyedge.data.markets import fetch_active_markets, fetch_all_markets
from polyedge.risk.sizing import calculate_position_size
from polyedge.strategies.crypto_sniper import (
    CryptoSniperStrategy,
    CryptoMarketType,
    ParsedCryptoMarket,
    SniperOpportunity,
    find_crypto_markets,
    match_market_to_symbol,
)

logger = logging.getLogger("polyedge.sniper_runner")
console = Console()

# How often to refresh the crypto market list from Polymarket
MARKET_REFRESH_INTERVAL = 60  # seconds

# How often to evaluate threshold/bucket markets (they don't need tick-by-tick)
SLOW_EVAL_INTERVAL = 30  # seconds


class SniperRunner:
    """Persistent real-time loop for crypto sniper strategy."""

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
        self.strategy = CryptoSniperStrategy(settings)
        sniper_config = settings.strategies.crypto_sniper

        # Binance price feed
        self.binance = BinanceFeed(symbols=sniper_config.symbols)

        # Active crypto markets grouped by type
        # symbol -> list of (market, parsed) for up/down (tick-evaluated)
        self.updown_markets: dict[str, list[tuple[Market, ParsedCryptoMarket]]] = {}
        # All threshold/bucket markets (periodically evaluated)
        self.slow_markets: list[tuple[Market, ParsedCryptoMarket]] = []

        # Track which markets we've already traded to avoid double-entry
        self._traded_markets: set[str] = set()

        # Stats
        self.opportunities_seen = 0
        self.trades_executed = 0
        self.total_edge_captured = 0.0
        self._total_markets = 0

    async def run(self):
        """Main sniper loop."""
        self.running = True
        sniper_config = self.settings.strategies.crypto_sniper

        mode = "DRY RUN" if self.dry_run else ("AUTOPILOT" if self.auto_execute else "COPILOT")
        console.print(f"[bold green]Crypto Sniper started in {mode} mode")
        console.print(
            f"[dim]Tracking: {', '.join(s.upper() for s in sniper_config.symbols)} | "
            f"Min edge: {sniper_config.min_edge:.0%} | "
            f"Entry window (up/down): last {sniper_config.max_seconds_before_entry:.0f}s[/dim]"
        )
        console.print(
            f"[dim]Market types: Up/Down + Threshold + Bucket | "
            f"Fetching all crypto markets from Polymarket[/dim]"
        )

        # Register price callback for up/down opportunity detection
        self.binance.on_any_price(self._on_price_update)

        # Start tasks
        tasks = [
            asyncio.create_task(self.binance.start()),
            asyncio.create_task(self._market_refresh_loop()),
            asyncio.create_task(self._slow_eval_loop()),
            asyncio.create_task(self._status_loop()),
        ]

        try:
            # Wait for Binance connection before proceeding
            for _ in range(50):  # 5 second timeout
                if self.binance.is_connected:
                    break
                await asyncio.sleep(0.1)

            if not self.binance.is_connected:
                console.print("[yellow]Waiting for Binance connection...")

            # Run until stopped
            await asyncio.gather(*tasks, return_exceptions=True)
        except KeyboardInterrupt:
            console.print("\n[yellow]Sniper stopped by user")
        finally:
            self.running = False
            await self.binance.stop()
            for t in tasks:
                t.cancel()

    async def stop(self):
        self.running = False
        await self.binance.stop()

    async def _market_refresh_loop(self):
        """Periodically fetch active crypto markets from Polymarket."""
        while self.running:
            try:
                await self._refresh_crypto_markets()
            except Exception as e:
                logger.error(f"Market refresh failed: {e}")

            await asyncio.sleep(MARKET_REFRESH_INTERVAL)

    async def _refresh_crypto_markets(self):
        """Fetch and classify all active crypto markets."""
        try:
            # Fetch more markets to catch threshold/bucket markets beyond top 100
            all_markets = await fetch_all_markets(
                self.settings,
                min_liquidity=self.settings.strategies.crypto_sniper.min_liquidity,
                max_pages=10,  # Up to 1000 markets
            )
        except Exception as e:
            logger.warning(f"Failed to fetch markets: {e}")
            return

        crypto = find_crypto_markets(all_markets)

        # Classify and group
        self.updown_markets.clear()
        self.slow_markets.clear()

        for market in crypto:
            parsed = self.strategy.parse_market(market)
            if not parsed:
                continue

            # Only track markets with symbols we're actually streaming from Binance
            if parsed.symbol not in self.settings.strategies.crypto_sniper.symbols:
                continue

            if parsed.market_type == CryptoMarketType.UP_DOWN:
                if parsed.symbol not in self.updown_markets:
                    self.updown_markets[parsed.symbol] = []
                self.updown_markets[parsed.symbol].append((market, parsed))

                # Start price window for up/down markets
                window = self.binance.get_window(parsed.symbol)
                if window and window.window_start_price <= 0:
                    self.binance.start_window(parsed.symbol)
            else:
                # Threshold and bucket markets go to slow eval
                self.slow_markets.append((market, parsed))

        n_updown = sum(len(v) for v in self.updown_markets.values())
        n_slow = len(self.slow_markets)
        self._total_markets = n_updown + n_slow

        if self._total_markets > 0:
            parts = []
            if n_updown > 0:
                ud_str = ", ".join(
                    f"{s.upper()}({len(m)})"
                    for s, m in self.updown_markets.items()
                )
                parts.append(f"Up/Down: {ud_str}")
            if n_slow > 0:
                parts.append(f"Threshold/Bucket: {n_slow}")
            logger.info(f"Tracking {self._total_markets} crypto markets — {' | '.join(parts)}")

    async def _on_price_update(self, snapshot: PriceSnapshot):
        """Called on every Binance price tick — evaluate up/down markets."""
        symbol = snapshot.symbol
        markets = self.updown_markets.get(symbol, [])
        if not markets:
            return

        window = self.binance.get_window(symbol)
        if not window or window.window_start_price <= 0:
            return

        for market, parsed in markets:
            if market.condition_id in self._traded_markets:
                continue

            if not market.end_date:
                continue

            now = datetime.now(timezone.utc)
            remaining = (market.end_date - now).total_seconds()

            opp = self.strategy.evaluate_with_price(
                market=market,
                price_window=window,
                current_price=snapshot,
                seconds_remaining=remaining,
                parsed=parsed,
            )

            if opp:
                self.opportunities_seen += 1
                await self._handle_opportunity(opp)

    async def _slow_eval_loop(self):
        """Periodically evaluate threshold and bucket markets."""
        while self.running:
            await asyncio.sleep(SLOW_EVAL_INTERVAL)

            for market, parsed in self.slow_markets:
                if market.condition_id in self._traded_markets:
                    continue

                if not market.end_date:
                    continue

                now = datetime.now(timezone.utc)
                remaining = (market.end_date - now).total_seconds()
                if remaining <= 0:
                    continue

                # Get current price from Binance
                snapshot = self.binance.get_price(parsed.symbol)
                if not snapshot or not snapshot.is_fresh:
                    continue

                # Threshold/bucket don't need a price window
                from polyedge.data.binance_feed import PriceWindow
                dummy_window = PriceWindow(symbol=parsed.symbol)

                opp = self.strategy.evaluate_with_price(
                    market=market,
                    price_window=dummy_window,
                    current_price=snapshot,
                    seconds_remaining=remaining,
                    parsed=parsed,
                )

                if opp:
                    self.opportunities_seen += 1
                    await self._handle_opportunity(opp)

    async def _handle_opportunity(self, opp: SniperOpportunity):
        """Handle a detected sniper opportunity."""
        signal = self.strategy.opportunity_to_signal(opp)

        type_label = opp.market_type.value.upper()
        console.print(
            f"\n[bold yellow]SNIPER [{type_label}][/bold yellow] "
            f"{opp.symbol.upper()} {opp.side.value} "
            f"| Edge: {opp.edge:.1%} "
            f"| Price: ${opp.binance_price:,.2f} "
            f"{'| Strike: $' + f'{opp.strike:,.2f}' if opp.strike else ''}"
            f"| Market: {opp.market_price:.1%} "
            f"| Implied: {opp.implied_prob:.1%}"
        )

        if self.dry_run:
            console.print(f"[dim]  {opp.market.question[:80]}[/dim]")
            console.print("[dim]  (dry run — not trading)[/dim]")
            return

        # Size the position
        bankroll = await self._get_bankroll()
        sniper_config = self.settings.strategies.crypto_sniper
        max_pct = min(sniper_config.max_position_per_trade, self.settings.risk.max_position_pct)

        size_usd = calculate_position_size(
            bankroll=bankroll,
            edge=opp.edge,
            probability=opp.implied_prob,
            kelly_fraction=self.settings.risk.kelly_fraction,
            max_position_pct=max_pct,
        )

        if size_usd < 1.0:
            console.print("[dim]  Position too small — skipping[/dim]")
            return

        console.print(
            f"  [bold]Sizing: ${size_usd:.2f} "
            f"({size_usd/bankroll*100:.1f}% of bankroll)[/bold]"
        )

        if self.auto_execute:
            await self._execute_snipe(opp, signal, size_usd)
        else:
            # Copilot mode — ask for confirmation
            try:
                response = input("  Execute snipe? (y/n): ").strip().lower()
                if response == "y":
                    await self._execute_snipe(opp, signal, size_usd)
                else:
                    console.print("[dim]  Skipped by user[/dim]")
            except EOFError:
                pass

    async def _execute_snipe(
        self,
        opp: SniperOpportunity,
        signal: Signal,
        size_usd: float,
    ):
        """Execute a sniper trade."""
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
            order_id = await engine.place_order(
                market=opp.market,
                token_id=token_id,
                side=opp.side.value,
                price=price,
                size=size,
                amount_usd=size_usd,
                strategy="crypto_sniper",
                reasoning=signal.reasoning,
                force=self.auto_execute,  # Skip confirmation in autopilot
            )

            if order_id:
                self._traded_markets.add(opp.market.condition_id)
                self.trades_executed += 1
                self.total_edge_captured += opp.edge
                console.print(f"[green]  SNIPED! Order: {order_id}[/green]")
            else:
                console.print("[red]  Trade failed or rejected[/red]")

        except Exception as e:
            console.print(f"[red]  Execution error: {e}[/red]")
            logger.error(f"Sniper execution failed: {e}", exc_info=True)

    async def _status_loop(self):
        """Periodically print status."""
        while self.running:
            await asyncio.sleep(30)

            prices = self.binance.get_all_prices()
            if prices:
                price_str = " | ".join(
                    f"{s.replace('usdt','').upper()}: ${p:,.2f}"
                    for s, p in prices.items()
                )
                n_updown = sum(len(v) for v in self.updown_markets.values())
                n_slow = len(self.slow_markets)
                console.print(
                    f"[dim]{price_str} | "
                    f"Markets: {n_updown} ud + {n_slow} th/bk | "
                    f"Opps: {self.opportunities_seen} | "
                    f"Trades: {self.trades_executed}[/dim]"
                )

    async def _get_bankroll(self) -> float:
        """Get current bankroll."""
        try:
            positions = await self.db.get_open_positions()
            exposure = sum(p.get("size", 0) * p.get("entry_price", 0) for p in positions)
            return max(0, 200.0 - exposure)
        except Exception:
            return 200.0
