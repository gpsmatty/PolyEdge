"""Autonomous trading agent — scans, analyzes, and trades on autopilot."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from polyedge.ai.analyst import analyze_market, quick_score_market
from polyedge.ai.llm import LLMClient
from polyedge.ai.news import get_news_context
from polyedge.core.config import Settings
from polyedge.core.client import PolyClient
from polyedge.core.console import console
from polyedge.core.db import Database
from polyedge.core.models import AgentMode, Market, Signal, Side
from polyedge.data.book_analyzer import get_book_intelligence, format_book_for_ai
from polyedge.data.indexer import MarketIndexer
from polyedge.risk.sizing import calculate_position_size
from polyedge.risk.kelly import fractional_kelly
from polyedge.strategies.edge_finder import EdgeFinderStrategy
from polyedge.strategies.cheap_hunter import CheapHunterStrategy

logger = logging.getLogger("polyedge.agent")


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

        # Market indexer — reads from DB, syncs periodically
        self.indexer = MarketIndexer(settings, db)

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
        """One full scan-analyze-trade cycle.

        Tiered approach to minimize AI costs:
        1. Sync markets from API → DB (only if stale)
        2. Read ALL markets from DB (free, no API call)
        3. Run cheap_hunter on all markets (zero AI cost)
        4. Pick top N candidates for AI analysis (expensive)
        5. Only AI-analyze markets that look promising OR moved in price
        """
        console.print("\n[bold cyan]--- Scan Cycle ---")

        # Housekeeping: review resolved positions + clean expired memories
        await self._review_resolved_positions()
        await self._cleanup_memory()

        # Check risk limits
        if await self._check_circuit_breakers():
            console.print("[red]Circuit breaker triggered — skipping cycle")
            return

        # 1. Get markets from DB (auto-syncs if stale)
        console.print("[dim]Loading markets from DB...[/dim]")
        markets = await self.indexer.get_markets(
            min_liquidity=self.settings.risk.min_liquidity,
        )
        console.print(f"Found {len(markets)} active markets in DB")

        if not markets:
            console.print("[yellow]No markets available — forcing sync")
            await self.indexer.sync(force=True)
            markets = await self.indexer.get_markets(
                min_liquidity=self.settings.risk.min_liquidity,
            )
            if not markets:
                console.print("[red]Still no markets after sync")
                return

        # 2. Pre-filter with cheap hunter (no AI cost)
        cheap_signals = self.cheap_hunter.evaluate_batch(markets)
        if cheap_signals:
            console.print(f"[green]Cheap Hunter found {len(cheap_signals)} opportunities")

        # 3. Tiered AI analysis — only analyze top candidates
        ai_signals = []
        if self.settings.strategies.edge_finder.enabled:
            # Check remaining AI budget before spending anything
            budget_left = await self.llm.get_budget_remaining()
            if budget_left <= 0:
                console.print("[yellow]AI budget exhausted for today — skipping AI analysis")
            else:
                # Pick candidates smartly: price movers + highest volume
                candidates = await self._pick_ai_candidates(markets)
                console.print(
                    f"[dim]AI analyzing {len(candidates)} candidates "
                    f"(budget remaining: ${budget_left:.2f})...[/dim]"
                )

                # Step 1: Quick-score with cheap compute model to pre-filter
                scored = []
                for market in candidates:
                    existing = await self.db.get_latest_analysis(market.condition_id)
                    if existing and self._analysis_still_fresh(existing, market):
                        continue

                    if await self.llm.get_budget_remaining() <= 0.01:
                        console.print("[yellow]AI budget hit mid-scan — stopping")
                        break

                    try:
                        score = await quick_score_market(self.llm, market)
                        scored.append((market, score))
                    except Exception as e:
                        logger.debug(f"Quick score failed for {market.question[:50]}: {e}")
                        scored.append((market, {"score": 50, "reason": "score failed"}))

                # Sort by score descending — only deep-analyze top half
                scored.sort(key=lambda x: x[1].get("score", 0), reverse=True)
                top_candidates = scored[:max(len(scored) // 2, 5)]

                console.print(
                    f"[dim]Quick-scored {len(scored)} markets, "
                    f"deep-analyzing top {len(top_candidates)}[/dim]"
                )

                # Step 2: Deep analysis with research model on top candidates
                for market, score_data in top_candidates:
                    if await self.llm.get_budget_remaining() <= 0.01:
                        console.print("[yellow]AI budget hit mid-scan — stopping analysis")
                        break

                    try:
                        # Get order book intelligence
                        book_context = ""
                        try:
                            book_intel = get_book_intelligence(self.client, market)
                            book_context = format_book_for_ai(book_intel)
                        except Exception:
                            pass  # Book data is nice-to-have, not critical

                        # Get agent memory for this market
                        memory_context = await self._get_market_context(market)

                        # Get news context (empty if no API key configured)
                        news = ""
                        if self.settings.news_api_key:
                            news = await get_news_context(
                                self.llm, market, self.settings.news_api_key
                            )

                        # Deep analysis with research model
                        analysis = await analyze_market(
                            self.llm, market,
                            news_context=news,
                            book_context=book_context,
                            memory_context=memory_context,
                        )
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
                        else:
                            await self._remember_skip(
                                market,
                                f"Edge too small ({abs(edge):.1%}). "
                                f"AI: {analysis.probability:.0%} vs Market: {market.yes_price:.0%}"
                            )

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

    async def _pick_ai_candidates(self, markets: list[Market]) -> list[Market]:
        """Pick the best candidates for AI analysis.

        Smart selection focused on markets where we're most likely to find real edge:
        1. Price movers (something happened — news, event, catalyst)
        2. Mid-range prices (20-80% implied) — most room for mispricing
        3. Moderate liquidity ($1K-$100K) — enough to trade, thin enough to misprice
        4. Filter out categories where LLMs have no edge

        Capped at max_markets_per_scan.
        """
        max_candidates = self.settings.agent.max_markets_per_scan
        candidates = []
        seen_ids = set()
        blacklist = set(c.lower() for c in self.settings.risk.categories_blacklist)

        # Priority 1: Markets that moved in price (something happened)
        try:
            movers = await self.indexer.get_price_movers(
                hours=1, min_move_pct=0.03, min_liquidity=self.settings.risk.min_liquidity
            )
            for mover in movers[:max_candidates // 2]:
                m = mover["market"]
                if m.condition_id not in seen_ids and not self._is_blacklisted(m, blacklist):
                    candidates.append(m)
                    seen_ids.add(m.condition_id)
        except Exception:
            pass

        # Priority 2: Score remaining markets on mispricing potential
        scored_markets = []
        for market in markets:
            if market.condition_id in seen_ids:
                continue
            if self._is_blacklisted(market, blacklist):
                continue
            if self._is_short_duration_crypto(market):
                continue  # Handled by crypto sniper

            score = self._candidate_score(market)
            if score > 0:
                scored_markets.append((market, score))

        scored_markets.sort(key=lambda x: x[1], reverse=True)
        for market, score in scored_markets:
            if len(candidates) >= max_candidates:
                break
            candidates.append(market)
            seen_ids.add(market.condition_id)

        return candidates

    def _candidate_score(self, market: Market) -> float:
        """Score a market for AI analysis potential.

        Higher score = more likely to have exploitable mispricing.
        Returns 0 to skip entirely.
        """
        price = market.yes_price
        score = 0.0

        # Mid-range prices: 20-80% implied probability
        # Extremes (<10% or >90%) rarely have real edge
        if 0.20 <= price <= 0.80:
            score += 3.0
            if 0.30 <= price <= 0.70:
                score += 2.0  # Sweet spot
        elif 0.10 <= price <= 0.90:
            score += 1.0
        else:
            return 0.0  # Too extreme

        # Moderate liquidity: enough to trade, thin enough to misprice
        liq = market.liquidity
        if 2000 <= liq <= 100_000:
            score += 2.0
        elif 100_000 < liq <= 300_000:
            score += 1.0
        elif 300_000 < liq <= 500_000:
            score += 0.5  # Getting efficient
        elif liq > 500_000:
            score += 0.0  # Almost certainly efficiently priced
        else:
            return 0.0

        # Volume = real interest
        if market.volume >= 10_000:
            score += 1.0
        elif market.volume >= 5_000:
            score += 0.5

        # Time to resolution: 1-30 days is the sweet spot
        hours = market.hours_to_resolution
        if hours and 24 <= hours <= 720:
            score += 1.0
        elif hours and hours < 24:
            score += 0.5

        return score

    def _is_blacklisted(self, market: Market, blacklist: set[str]) -> bool:
        """Check if a market's category is blacklisted."""
        if not blacklist:
            return False
        cat = market.category.lower()
        q = market.question.lower()
        return any(b in cat or b in q for b in blacklist)

    def _is_short_duration_crypto(self, market: Market) -> bool:
        """Check if this is a short-duration crypto market (handled by sniper)."""
        import re
        q = market.question.lower()
        return bool(re.search(
            r"(bitcoin|btc|ethereum|eth|solana|sol)\s+up\s+or\s+down", q
        ))

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

        # Record trade in agent memory
        await self._remember_trade(signal, size_usd)

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

    # --- Agent Memory ---

    async def _remember_trade(self, signal: Signal, size_usd: float):
        """Record a trade decision in memory so the agent can build context."""
        content = (
            f"Traded {signal.side.value} ${size_usd:.2f} on '{signal.market.question[:80]}'. "
            f"Edge: {signal.edge:.1%}, Confidence: {signal.confidence:.1%}. "
            f"Strategy: {signal.strategy}. "
            f"Entry price: {signal.market.yes_price if signal.side == Side.YES else signal.market.no_price:.3f}."
        )
        if signal.reasoning:
            content += f" Reasoning: {signal.reasoning[:200]}"

        await self.db.save_memory(
            memory_type="trade_decision",
            content=content,
            market_id=signal.market.condition_id,
            metadata={
                "side": signal.side.value,
                "size_usd": size_usd,
                "edge": signal.edge,
                "strategy": signal.strategy,
                "entry_price": signal.market.yes_price if signal.side == Side.YES else signal.market.no_price,
            },
            importance=0.7,
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )

    async def _remember_skip(self, market: Market, reason: str):
        """Record why we skipped a market — helps avoid re-analyzing bad candidates."""
        await self.db.save_memory(
            memory_type="skip_reason",
            content=f"Skipped '{market.question[:60]}': {reason}",
            market_id=market.condition_id,
            importance=0.3,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=6),
        )

    async def _remember_lesson(self, content: str, importance: float = 0.8,
                                 market_id: str = ""):
        """Record a lesson learned (e.g. from a resolved trade)."""
        await self.db.save_memory(
            memory_type="lesson",
            content=content,
            market_id=market_id,
            importance=importance,
            # Lessons don't expire — they're valuable long-term
        )

    async def _get_market_context(self, market: Market) -> str:
        """Build memory context for a market to feed into AI analysis.

        Returns a string summary of what the agent remembers about this market.
        """
        memories = await self.db.get_market_memories(market.condition_id)
        if not memories:
            return ""

        lines = []
        for mem in memories[:5]:  # Cap at 5 most important
            lines.append(f"- [{mem['memory_type']}] {mem['content']}")

        return "Agent memory for this market:\n" + "\n".join(lines)

    async def _get_global_context(self) -> str:
        """Get global agent lessons to feed into AI analysis."""
        lessons = await self.db.get_global_memories(memory_type="lesson", limit=5)
        if not lessons:
            return ""

        lines = [f"- {m['content']}" for m in lessons]
        return "Lessons learned from past trades:\n" + "\n".join(lines)

    async def _review_resolved_positions(self):
        """Check if any positions resolved and record lessons.

        Called at the start of each scan cycle.
        """
        try:
            trades = await self.db.get_open_trades()
            for trade in trades:
                market_id = trade.get("market_id", "")
                # Check if the market is now closed/inactive
                market_rows = await self.db.get_markets_from_db(active_only=False, limit=1)
                # Look up this specific market
                market_row = None
                async with self.db.pool.acquire() as conn:
                    market_row = await conn.fetchrow(
                        "SELECT * FROM polyedge.markets WHERE condition_id = $1",
                        market_id,
                    )

                if market_row and (market_row["closed"] or not market_row["active"]):
                    # Market resolved — calculate outcome
                    entry = trade.get("entry_price", 0)
                    side = trade.get("side", "")
                    ai_prob = trade.get("ai_probability")

                    # Record lesson about calibration
                    if ai_prob is not None:
                        final_price = market_row.get("yes_price", 0)
                        if final_price >= 0.95:
                            outcome = "YES won"
                            correct = side == "YES"
                        elif final_price <= 0.05:
                            outcome = "NO won"
                            correct = side == "NO"
                        else:
                            continue  # Not clearly resolved yet

                        pnl_sign = "profit" if correct else "loss"
                        await self._remember_lesson(
                            f"Market '{trade.get('question', '')[:60]}' resolved: {outcome}. "
                            f"We bet {side} at {entry:.2f} (AI said {ai_prob:.0%}). "
                            f"Result: {pnl_sign}. "
                            f"Category: {market_row.get('category', 'unknown')}.",
                            importance=0.9,
                            market_id=market_id,
                        )
        except Exception as e:
            logger.debug(f"Error reviewing resolved positions: {e}")

    async def _cleanup_memory(self):
        """Periodic memory maintenance."""
        try:
            deleted = await self.db.cleanup_expired_memories()
            if deleted > 0:
                logger.info(f"Cleaned up {deleted} expired memories")
        except Exception:
            pass
