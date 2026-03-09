"""Autonomous trading agent — scans, analyzes, and trades on autopilot."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from rich.console import Console

from polyedge.ai.analyst import analyze_market
from polyedge.ai.llm import LLMClient
from polyedge.ai.news import get_news_context
from polyedge.core.config import Settings
from polyedge.core.client import PolyClient
from polyedge.core.db import Database
from polyedge.core.models import AgentMode, Market, Signal, Side
from polyedge.data.markets import fetch_all_markets
from polyedge.risk.sizing import calculate_position_size
from polyedge.risk.kelly import fractional_kelly
from polyedge.strategies.edge_finder import EdgeFinderStrategy
from polyedge.strategies.cheap_hunter import CheapHunterStrategy

logger = logging.getLogger("polyedge.agent")
console = Console()


class TradingAgent:
    """Autonomous AI trading agent for Polymarket."""

    def __init__(
        self,
        settings: Settings,
        client: PolyClient,
        db: Database,
        llm: LLMClient,
    ):
        self.settings = settings
        self.client = client
        self.db = db
        self.llm = llm
        self.mode = AgentMode(settings.agent.mode)
        self.running = False

        # Strategies
        self.edge_finder = EdgeFinderStrategy(settings)
        self.cheap_hunter = CheapHunterStrategy(settings)

    async def run(self):
        """Main agent loop — scan, analyze, trade, repeat."""
        self.running = True
        console.print("[bold green]Agent started in [bold]{} mode[/bold]".format(self.mode.value))

        scan_interval = self.settings.agent.scan_interval_minutes * 60

        while self.running:
            try:
                await self._scan_cycle()
            except KeyboardInterrupt:
                console.print("\n[yellow]Agent stopped by user")
                break
            except Exception as e:
                logger.error(f"Scan cycle error: {e}", exc_info=True)
                console.print(f"[red]Error in scan cycle: {e}")

            if not self.running:
                break

            console.print(
                f"[dim]Next scan in {self.settings.agent.scan_interval_minutes} minutes...[/dim]"
            )
            try:
                await asyncio.sleep(scan_interval)
            except asyncio.CancelledError:
                break

    async def stop(self):
        self.running = False

    async def _scan_cycle(self):
        """One full scan-analyze-trade cycle."""
        console.print("\n[bold cyan]--- Scan Cycle ---")

        # Check risk limits
        if await self._check_circuit_breakers():
            console.print("[red]Circuit breaker triggered — skipping cycle")
            return

        # 1. Fetch markets
        console.print("[dim]Fetching markets...[/dim]")
        markets = await fetch_all_markets(
            self.settings,
            min_liquidity=self.settings.risk.min_liquidity,
            max_pages=3,
        )
        console.print(f"Found {len(markets)} active markets")

        # 2. Pre-filter with cheap hunter (no AI cost)
        cheap_signals = self.cheap_hunter.evaluate_batch(markets)
        if cheap_signals:
            console.print(f"[green]Cheap Hunter found {len(cheap_signals)} opportunities")

        # 3. AI analysis on top markets
        ai_signals = []
        if self.settings.strategies.edge_finder.enabled:
            top_markets = markets[: self.settings.agent.max_markets_per_scan]
            console.print(f"[dim]AI analyzing {len(top_markets)} markets...[/dim]")

            for market in top_markets:
                # Check if we already have a recent analysis
                existing = await self.db.get_latest_analysis(market.condition_id)
                if existing and self._analysis_still_fresh(existing, market):
                    continue

                try:
                    # Get news context
                    news = await get_news_context(
                        self.llm, market, self.settings.news_api_key
                    )

                    # Analyze
                    analysis = await analyze_market(self.llm, market, news_context=news)
                    await self.db.save_analysis(analysis.model_dump())

                    # Check for edge
                    edge = analysis.probability - market.yes_price
                    if abs(edge) >= self.settings.risk.min_edge_threshold:
                        side = Side.YES if edge > 0 else Side.NO
                        signal = Signal(
                            market=market,
                            side=side,
                            confidence=analysis.confidence,
                            edge=abs(edge),
                            ev=abs(edge) * analysis.confidence,
                            reasoning=analysis.reasoning,
                            strategy="edge_finder",
                            ai_probability=analysis.probability,
                        )
                        ai_signals.append(signal)

                except Exception as e:
                    logger.warning(f"Analysis failed for {market.question[:50]}: {e}")
                    continue

        # 4. Combine and rank all signals
        all_signals = cheap_signals + ai_signals
        all_signals.sort(key=lambda s: s.ev, reverse=True)

        if not all_signals:
            console.print("[dim]No opportunities found this cycle[/dim]")
            return

        console.print(f"\n[bold green]Found {len(all_signals)} signals:")
        for i, sig in enumerate(all_signals[:10], 1):
            console.print(
                f"  {i}. [{sig.strategy}] {sig.market.question[:60]} "
                f"| {sig.side.value} | edge={sig.edge:.1%} | EV={sig.ev:.3f}"
            )

        # 5. Execute based on mode
        if self.mode == AgentMode.AUTOPILOT:
            await self._autopilot_execute(all_signals)
        elif self.mode == AgentMode.COPILOT:
            await self._copilot_execute(all_signals)
        else:
            # Signals mode — just display, don't trade
            console.print("[dim]Signals mode — displaying only, no trades[/dim]")

    async def _autopilot_execute(self, signals: list[Signal]):
        """Auto-execute top signals within risk limits."""
        trades_today = await self.db.get_trades_today()
        remaining_trades = self.settings.risk.max_trades_per_day - len(trades_today)

        if remaining_trades <= 0:
            console.print("[yellow]Daily trade limit reached")
            return

        positions = await self.db.get_open_positions()
        if len(positions) >= self.settings.risk.max_positions:
            console.print("[yellow]Max positions reached")
            return

        for signal in signals[:remaining_trades]:
            if signal.confidence < self.settings.risk.min_confidence:
                continue

            # Size the position
            bankroll = await self._get_bankroll()
            size_usd = calculate_position_size(
                bankroll=bankroll,
                edge=signal.edge,
                probability=signal.ai_probability or signal.confidence,
                kelly_fraction=self.settings.risk.kelly_fraction,
                max_position_pct=self.settings.risk.max_position_pct,
            )

            if size_usd < 1.0:  # Minimum trade size
                continue

            console.print(
                f"\n[bold yellow]AUTO-TRADING: {signal.side.value} "
                f"${size_usd:.2f} on '{signal.market.question[:50]}'"
            )

            try:
                await self._execute_trade(signal, size_usd)
            except Exception as e:
                console.print(f"[red]Trade failed: {e}")

    async def _copilot_execute(self, signals: list[Signal]):
        """Show recommendations and ask for approval."""
        for signal in signals[:5]:
            bankroll = await self._get_bankroll()
            size_usd = calculate_position_size(
                bankroll=bankroll,
                edge=signal.edge,
                probability=signal.ai_probability or signal.confidence,
                kelly_fraction=self.settings.risk.kelly_fraction,
                max_position_pct=self.settings.risk.max_position_pct,
            )

            if size_usd < 1.0:
                continue

            console.print(
                f"\n[bold]Recommendation: {signal.side.value} ${size_usd:.2f} "
                f"on '{signal.market.question[:60]}'"
            )
            console.print(f"  Edge: {signal.edge:.1%} | Confidence: {signal.confidence:.1%}")
            console.print(f"  Reasoning: {signal.reasoning[:200]}")

            try:
                response = input("  Execute? (y/n/q): ").strip().lower()
            except EOFError:
                break

            if response == "q":
                break
            elif response == "y":
                try:
                    await self._execute_trade(signal, size_usd)
                    console.print("[green]Trade executed!")
                except Exception as e:
                    console.print(f"[red]Trade failed: {e}")

    async def _execute_trade(self, signal: Signal, size_usd: float):
        """Execute a trade from a signal."""
        from polyedge.execution.engine import ExecutionEngine

        engine = ExecutionEngine(self.client, self.db, self.settings)

        token_id = (
            signal.market.yes_token_id
            if signal.side == Side.YES
            else signal.market.no_token_id
        )
        if not token_id:
            raise ValueError("No token ID available")

        price = signal.market.yes_price if signal.side == Side.YES else signal.market.no_price
        size = size_usd / price if price > 0 else 0

        await engine.place_order(
            market=signal.market,
            token_id=token_id,
            side=signal.side.value,
            price=price,
            size=size,
            amount_usd=size_usd,
            strategy=signal.strategy,
            reasoning=signal.reasoning,
            ai_probability=signal.ai_probability,
        )

    async def _get_bankroll(self) -> float:
        """Get current bankroll from positions + available balance."""
        # TODO: Query actual USDC balance from wallet
        # For now, use a configured starting bankroll minus exposure
        positions = await self.db.get_open_positions()
        exposure = sum(p.get("size", 0) * p.get("entry_price", 0) for p in positions)
        return max(0, 200.0 - exposure)  # Hardcoded $200 start — make configurable

    async def _check_circuit_breakers(self) -> bool:
        """Check if any risk circuit breakers are triggered."""
        trades = await self.db.get_trades_today()
        if len(trades) >= self.settings.risk.max_trades_per_day:
            return True

        # Check daily loss
        daily_pnl = sum(t.get("pnl", 0) for t in trades if t.get("pnl"))
        bankroll = await self._get_bankroll()
        if bankroll > 0 and daily_pnl < -(bankroll * self.settings.risk.daily_loss_limit_pct):
            return True

        return False

    def _analysis_still_fresh(self, analysis: dict, market: Market) -> bool:
        """Check if an existing analysis is still valid."""
        analyzed_at = analysis.get("analyzed_at")
        if not analyzed_at:
            return False

        # Re-analyze if price moved significantly
        old_prob = analysis.get("probability", 0)
        if abs(market.yes_price - old_prob) > self.settings.agent.reanalyze_price_change_pct:
            return False

        # Re-analyze after scan interval
        if isinstance(analyzed_at, datetime):
            age_minutes = (datetime.now(timezone.utc) - analyzed_at.replace(tzinfo=timezone.utc)).total_seconds() / 60
            return age_minutes < self.settings.agent.scan_interval_minutes * 2

        return False
