"""Micro Sniper Runner — persistent async loop for high-frequency momentum
trading on Polymarket 5-minute crypto up/down markets.

Connects to:
  - Binance aggTrade WebSocket for tick-level order flow data
  - Polymarket WebSocket for real-time market prices

On every aggTrade tick (~10-50/sec for BTC), the runner:
1. Updates microstructure state (OFI, VWAP, intensity)
2. Evaluates momentum signal against active up/down markets
3. Enters, exits, or flips positions based on signal + confidence

Can produce 10-50+ trades per 5-minute window depending on volatility.

Usage:
    polyedge micro              # Copilot mode (confirm trades)
    polyedge micro --auto       # Autopilot mode (auto-execute)
    polyedge micro --dry        # Dry run — watch and analyze only
    polyedge micro --dry -q     # Quiet dry run (status only)
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from rich.console import Console

from polyedge.core.config import Settings
from polyedge.core.client import PolyClient
from polyedge.core.db import Database
from polyedge.core.models import Market, Side
from polyedge.data.binance_aggtrade import (
    BinanceAggTradeFeed,
    AggTrade,
    MicroStructure,
)
from polyedge.data.binance_feed import BinanceFeed
from polyedge.data.ws_feed import MarketFeed, EVENT_BEST_BID_ASK, EVENT_LAST_TRADE
from polyedge.data.indexer import MarketIndexer
from polyedge.risk.sizing import calculate_position_size
from polyedge.strategies.micro_sniper import (
    MicroSniperStrategy,
    MicroAction,
    MicroOpportunity,
)
from polyedge.strategies.crypto_sniper import (
    CryptoMarketType,
    ParsedCryptoMarket,
    find_crypto_markets,
    UP_DOWN_PATTERN,
    CRYPTO_SYMBOL_MAP,
    EXCLUDED_PATTERNS,
)

logger = logging.getLogger("polyedge.micro_runner")
console = Console()

# How often to refresh market list from DB
MARKET_REFRESH_INTERVAL = 120  # seconds (unfiltered)
MARKET_REFRESH_INTERVAL_FILTERED = 30  # seconds (with --market filter, need fast window transitions)

# How often to print status
STATUS_INTERVAL = 15  # seconds (shorter than regular sniper — more active)


class MicroRunner:
    """Persistent async loop for micro sniper momentum trading.

    Uses Binance aggTrade for order flow, Polymarket WS for live prices.
    Evaluates on every trade tick and can trade multiple times per window.
    """

    def __init__(
        self,
        settings: Settings,
        client: PolyClient,
        db: Database,
        auto_execute: bool = False,
        dry_run: bool = False,
        verbose: bool = False,
        quiet: bool = False,
        market_filter: Optional[str] = None,
    ):
        self.settings = settings
        self.client = client
        self.db = db
        self.auto_execute = auto_execute
        self.dry_run = dry_run
        if quiet:
            self.verbose = False
        elif verbose:
            self.verbose = True
        else:
            self.verbose = dry_run
        self.quiet = quiet
        self.running = False
        self.market_filter = market_filter.lower() if market_filter else None
        # Split filter into words so "btc 5m" matches slug "btc-updown-5m-..."
        self._filter_words = self.market_filter.split() if self.market_filter else []

        # Config
        self.config = settings.strategies.micro_sniper

        # Strategy
        self.strategy = MicroSniperStrategy(settings)

        # Market indexer
        self.indexer = MarketIndexer(settings, db)

        # Binance aggTrade feed (the core data source)
        self.agg_feed = BinanceAggTradeFeed(symbols=self.config.symbols)

        # Also keep the regular ticker feed for price reference
        self.ticker_feed = BinanceFeed(symbols=self.config.symbols)

        # Polymarket price feed
        self.poly_feed = MarketFeed(settings)
        self._poly_task: Optional[asyncio.Task] = None
        self._poly_connected = False

        # Active up/down markets: symbol -> list of (market, parsed)
        self.updown_markets: dict[str, list[tuple[Market, ParsedCryptoMarket]]] = {}

        # Token ID -> (Market, "yes"|"no") for WS price updates
        self._token_to_market: dict[str, tuple[Market, str]] = {}
        self._subscribed_tokens: set[str] = set()

        # Position tracking per market: condition_id -> "yes" | "no" | None
        self._positions: dict[str, str] = {}

        # Trade tracking
        self._trades_this_window: int = 0
        self._total_trades: int = 0
        self._total_flips: int = 0
        self._pnl_estimate: float = 0.0  # Rough P&L estimate

        # Stats
        self._total_markets = 0
        self._ws_price_updates = 0
        self._eval_count = 0
        self._last_status_time = 0.0
        self._last_trade_log: dict[str, float] = {}  # condition_id -> timestamp

        # Rate limiting: don't trade the same market more than once per N seconds
        self._trade_cooldown: float = 3.0  # seconds between trades on same market

    async def run(self):
        """Main micro sniper loop."""
        self.running = True

        mode = "DRY RUN" if self.dry_run else ("AUTOPILOT" if self.auto_execute else "COPILOT")
        console.print(f"\n[bold green]Micro Sniper started in {mode} mode[/bold green]")
        console.print(
            f"[dim]Tracking: {', '.join(s.upper() for s in self.config.symbols)} | "
            f"Entry: momentum > {self.config.entry_threshold:.0%} | "
            f"Flip: momentum > {self.config.flip_threshold:.0%} | "
            f"Max trades/window: {self.config.max_trades_per_window}[/dim]"
        )
        filter_str = f" | Filter: '{self.market_filter}'" if self.market_filter else ""
        console.print(
            f"[dim]Feed: Binance aggTrade (~10-50 tps) + Polymarket WS{filter_str}[/dim]"
        )

        # Initial market load — try DB first when --market filter is set,
        # only hit the API if DB comes back empty (stale data / first run)
        if self.market_filter:
            console.print("[dim]Loading markets from DB...[/dim]")
            await self._refresh_markets()
            if self._total_markets == 0:
                console.print("[yellow]No matching markets in DB — syncing from API...[/yellow]")
                try:
                    synced = await self.indexer.sync(force=True)
                    console.print(f"[green]Synced {synced} markets[/green]")
                    await self._refresh_markets()
                except Exception as e:
                    console.print(f"[yellow]Sync failed ({e})[/yellow]")
        else:
            console.print("[dim]Syncing markets from API...[/dim]")
            try:
                synced = await self.indexer.sync(force=True)
                console.print(f"[green]Synced {synced} markets[/green]")
            except Exception as e:
                console.print(f"[yellow]Sync failed ({e}) — using DB data[/yellow]")

        # Register aggTrade callback — this is the main eval loop
        self.agg_feed.on_any_trade(self._on_agg_trade)

        # Register Polymarket WS callbacks
        self.poly_feed.on(EVENT_BEST_BID_ASK, self._on_poly_price)
        self.poly_feed.on(EVENT_LAST_TRADE, self._on_poly_trade)

        # Start all tasks
        tasks = [
            asyncio.create_task(self.agg_feed.start()),
            asyncio.create_task(self.ticker_feed.start()),
            asyncio.create_task(self._market_refresh_loop()),
            asyncio.create_task(self._status_loop()),
        ]

        try:
            # Wait for connection
            for _ in range(50):
                if self.agg_feed.is_connected:
                    break
                await asyncio.sleep(0.1)

            if not self.agg_feed.is_connected:
                console.print("[yellow]Waiting for Binance aggTrade connection...[/yellow]")

            await asyncio.gather(*tasks, return_exceptions=True)
        except KeyboardInterrupt:
            console.print("\n[yellow]Micro sniper stopped by user[/yellow]")
        finally:
            self.running = False
            await self.agg_feed.stop()
            await self.ticker_feed.stop()
            await self._stop_poly_feed()
            for t in tasks:
                t.cancel()

    async def stop(self):
        self.running = False
        await self.agg_feed.stop()
        await self.ticker_feed.stop()
        await self._stop_poly_feed()

    # ------------------------------------------------------------------
    # Polymarket WebSocket management
    # ------------------------------------------------------------------

    async def _start_poly_feed(self, token_ids: list[str]):
        """Start or restart the Polymarket WebSocket."""
        await self._stop_poly_feed()
        if not token_ids:
            return

        self._subscribed_tokens = set(token_ids)

        async def _run_poly():
            try:
                self._poly_connected = False
                await self.poly_feed.start(token_ids)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"Polymarket WS error: {e}")
                self._poly_connected = False

        self._poly_task = asyncio.create_task(_run_poly())

        for _ in range(30):
            if self.poly_feed.is_connected:
                self._poly_connected = True
                console.print(f"[dim]Polymarket WS: live ({len(token_ids)} tokens)[/dim]")
                break
            await asyncio.sleep(0.1)

    async def _stop_poly_feed(self):
        self._poly_connected = False
        await self.poly_feed.stop()
        if self._poly_task and not self._poly_task.done():
            self._poly_task.cancel()
            try:
                await self._poly_task
            except asyncio.CancelledError:
                pass
        self._poly_task = None
        self._subscribed_tokens.clear()

    async def _on_poly_price(self, event: dict):
        """Handle Polymarket best_bid_ask."""
        asset_id = event.get("asset_id", "")
        entry = self._token_to_market.get(asset_id)
        if not entry:
            return

        market, side = entry
        best_bid = float(event.get("best_bid", 0))
        best_ask = float(event.get("best_ask", 0))

        if best_bid > 0 and best_ask > 0:
            mid = (best_bid + best_ask) / 2
        elif best_bid > 0:
            mid = best_bid
        elif best_ask > 0:
            mid = best_ask
        else:
            return

        if side == "yes":
            market.yes_price = mid
            market.no_price = max(0.01, 1.0 - mid)
        else:
            market.no_price = mid
            market.yes_price = max(0.01, 1.0 - mid)

        self._poly_connected = True
        self._ws_price_updates += 1

    async def _on_poly_trade(self, event: dict):
        """Handle Polymarket last_trade."""
        asset_id = event.get("asset_id", "")
        entry = self._token_to_market.get(asset_id)
        if not entry:
            return

        market, side = entry
        price = float(event.get("price", 0))
        if price <= 0 or price >= 1:
            return

        if side == "yes":
            market.yes_price = price
            market.no_price = max(0.01, 1.0 - price)
        else:
            market.no_price = price
            market.yes_price = max(0.01, 1.0 - price)

        self._poly_connected = True
        self._ws_price_updates += 1

    # ------------------------------------------------------------------
    # Market refresh
    # ------------------------------------------------------------------

    async def _market_refresh_loop(self):
        """Periodically refresh active up/down markets.

        When --market filter is set: skip API sync as long as we have
        matching markets.  If markets drop to 0 (window expired), do a
        sync to pick up the next window.
        """
        while self.running:
            try:
                need_sync = not self.market_filter or self._total_markets == 0
                if need_sync:
                    try:
                        await self.indexer.sync()
                    except Exception as e:
                        logger.warning(f"Background sync failed: {e}")

                await self._refresh_markets()
            except Exception as e:
                logger.error(f"Market refresh failed: {e}")
            interval = MARKET_REFRESH_INTERVAL_FILTERED if self.market_filter else MARKET_REFRESH_INTERVAL
            await asyncio.sleep(interval)

    async def _refresh_markets(self):
        """Load up/down crypto markets from DB.

        Only keeps markets that are currently live — end_date in the future.
        For each symbol, picks the NEAREST expiring window (the one that's
        active right now) so we're always on the current 5-min window.
        """
        now = datetime.now(timezone.utc)

        try:
            all_markets = await self.indexer.get_markets(
                min_liquidity=self.config.min_liquidity,
                limit=5000,
            )
        except Exception as e:
            logger.warning(f"Failed to load markets: {e}")
            return

        crypto = find_crypto_markets(all_markets)

        # Collect all matching markets per symbol, then pick the nearest live one
        candidates: dict[str, list[tuple[Market, ParsedCryptoMarket]]] = {}

        for market in crypto:
            q = market.question
            if not UP_DOWN_PATTERN.search(q):
                continue
            if EXCLUDED_PATTERNS.search(q):
                continue

            # Skip markets that have already ended
            if not market.end_date or market.end_date <= now:
                continue

            # Apply user's --market filter — every word must appear somewhere
            # in question or slug.  e.g. "btc 5m" matches slug "btc-updown-5m-..."
            # and "bitcoin" matches question "Bitcoin Up or Down ..."
            if self._filter_words:
                haystack = f"{q.lower()} {(market.slug or '').lower()}"
                if not all(w in haystack for w in self._filter_words):
                    continue

            # Extract symbol
            symbol = None
            q_lower = q.lower()
            for keyword in sorted(CRYPTO_SYMBOL_MAP.keys(), key=len, reverse=True):
                if keyword in q_lower:
                    symbol = CRYPTO_SYMBOL_MAP[keyword]
                    break
            if not symbol:
                continue
            if symbol not in self.config.symbols:
                continue

            parsed = ParsedCryptoMarket(
                market_type=CryptoMarketType.UP_DOWN,
                symbol=symbol,
            )

            if symbol not in candidates:
                candidates[symbol] = []
            candidates[symbol].append((market, parsed))

        # For each symbol, sort by end_date and pick the nearest window(s)
        # (the one expiring soonest is the currently active window)
        self.updown_markets.clear()
        self._token_to_market.clear()
        new_token_ids: list[str] = []

        for symbol, market_list in candidates.items():
            # Sort by end_date ascending — first entry is the current live window
            market_list.sort(key=lambda mp: mp[0].end_date)

            # Take the nearest window (current) + next few for seamless hopping
            selected = market_list[:3]

            self.updown_markets[symbol] = selected

            for market, parsed in selected:
                if len(market.clob_token_ids) >= 2:
                    self._token_to_market[market.clob_token_ids[0]] = (market, "yes")
                    self._token_to_market[market.clob_token_ids[1]] = (market, "no")
                    new_token_ids.extend(market.clob_token_ids[:2])

            # Log which window we're on
            current = selected[0][0]
            remaining = (current.end_date - now).total_seconds()
            if not self.quiet:
                console.print(
                    f"[cyan]{symbol.replace('usdt','').upper()}: "
                    f"[bold]{current.question}[/bold] "
                    f"({remaining:.0f}s left, "
                    f"YES={current.yes_price:.2f} NO={current.no_price:.2f})[/cyan]"
                )

        self._total_markets = sum(len(v) for v in self.updown_markets.values())

        # Reset window counters for new market set
        self._trades_this_window = 0

        # Manage Polymarket WS
        new_token_set = set(new_token_ids)
        if new_token_set != self._subscribed_tokens and new_token_ids:
            await self._start_poly_feed(new_token_ids)

        if not self.quiet:
            ud_str = ", ".join(
                f"{s.replace('usdt','').upper()}({len(m)})"
                for s, m in self.updown_markets.items()
            )
            ws_label = "live" if self._poly_connected else "connecting"
            console.print(
                f"[cyan]Markets: {self._total_markets} up/down ({ud_str}) "
                f"| Poly WS: {ws_label}[/cyan]"
            )

    # ------------------------------------------------------------------
    # Window hopping — seamless transition between 5-min windows
    # ------------------------------------------------------------------

    async def _hop_window(self, symbol: str, now: datetime):
        """Current window expired — immediately hop to the next one.

        If we pre-loaded the next window (selected[:2] in refresh), promote it.
        If no next window is available, trigger a fast refresh from DB/API to
        pick up newly created windows.
        """
        markets = self.updown_markets.get(symbol, [])
        expired = markets[0][0] if markets else None

        # Close any position on the expired market
        if expired:
            cid = expired.condition_id
            pos = self._positions.pop(cid, None)
            if pos and not self.quiet:
                console.print(
                    f"[yellow]Window expired — auto-closed {pos.upper()} position on "
                    f"{expired.question}[/yellow]"
                )

        # Remove expired window, promote next
        live = [(m, p) for m, p in markets if m.end_date and m.end_date > now]

        if live:
            # We have the next window pre-loaded — instant hop
            self.updown_markets[symbol] = live
            next_mkt = live[0][0]
            remaining = (next_mkt.end_date - now).total_seconds()
            self._trades_this_window = 0

            # Re-subscribe Polymarket WS to new token IDs
            new_tokens: list[str] = []
            self._token_to_market.clear()
            for sym, mlist in self.updown_markets.items():
                for m, _ in mlist:
                    if len(m.clob_token_ids) >= 2:
                        self._token_to_market[m.clob_token_ids[0]] = (m, "yes")
                        self._token_to_market[m.clob_token_ids[1]] = (m, "no")
                        new_tokens.extend(m.clob_token_ids[:2])

            new_set = set(new_tokens)
            if new_set != self._subscribed_tokens and new_tokens:
                await self._start_poly_feed(new_tokens)

            console.print(
                f"\n[bold green]{'='*60}[/bold green]"
                f"\n[bold green]WINDOW HOP → {next_mkt.question}[/bold green]"
                f"\n[bold green]{remaining:.0f}s remaining | "
                f"YES={next_mkt.yes_price:.2f} NO={next_mkt.no_price:.2f}[/bold green]"
                f"\n[bold green]{'='*60}[/bold green]\n"
            )
        else:
            # No next window pre-loaded — need to refresh from DB/API
            console.print("[yellow]Window expired, no next window cached — refreshing...[/yellow]")
            self._trades_this_window = 0

            # Quick DB read first
            await self._refresh_markets()

            if self._total_markets == 0:
                # DB doesn't have next window yet — sync from API
                console.print("[yellow]Syncing from API for next window...[/yellow]")
                try:
                    await self.indexer.sync(force=True)
                    await self._refresh_markets()
                except Exception as e:
                    logger.warning(f"Hop sync failed: {e}")

        self._total_markets = sum(len(v) for v in self.updown_markets.values())

    # ------------------------------------------------------------------
    # Core eval loop — called on every aggTrade tick
    # ------------------------------------------------------------------

    async def _on_agg_trade(self, trade: AggTrade, micro: MicroStructure):
        """Called on every Binance aggTrade — evaluate up/down markets."""
        symbol = trade.symbol
        markets = self.updown_markets.get(symbol, [])
        if not markets:
            return

        self._eval_count += 1

        # Don't evaluate faster than needed — batch evals per symbol
        # (aggTrade can fire 10-50/sec, we don't need to eval every single one)
        # Eval every 5th trade or every 0.5 seconds, whichever comes first
        if self._eval_count % 5 != 0:
            return

        now = datetime.now(timezone.utc)

        # Check if the current (first) window has expired — if so, hop to next
        current_market = markets[0][0]
        if current_market.end_date and current_market.end_date <= now:
            await self._hop_window(symbol, now)
            # Re-fetch after hop
            markets = self.updown_markets.get(symbol, [])
            if not markets:
                return

        for market, parsed in markets:
            if not market.end_date:
                continue

            remaining = (market.end_date - now).total_seconds()
            if remaining <= 0:
                continue

            # Check trade cooldown for this market
            last_trade_time = self._last_trade_log.get(market.condition_id, 0)
            if time.time() - last_trade_time < self._trade_cooldown:
                continue

            # Check max trades per window
            if self._trades_this_window >= self.config.max_trades_per_window:
                continue

            current_pos = self._positions.get(market.condition_id)

            opp = self.strategy.evaluate(
                market=market,
                micro=micro,
                seconds_remaining=remaining,
                current_position=current_pos,
            )

            if opp:
                await self._handle_opportunity(opp)

    # ------------------------------------------------------------------
    # Opportunity handling
    # ------------------------------------------------------------------

    async def _handle_opportunity(self, opp: MicroOpportunity):
        """Handle a micro sniper opportunity."""
        action = opp.action
        cid = opp.market.condition_id
        price_source = "live" if self._poly_connected else "api"

        # Format action display
        if action == MicroAction.EXIT:
            action_color = "yellow"
            action_str = "EXIT"
        elif action in (MicroAction.FLIP_YES, MicroAction.FLIP_NO):
            action_color = "magenta"
            action_str = f"FLIP → {opp.side.value}"
        else:
            action_color = "green" if opp.side == Side.YES else "red"
            action_str = f"BUY {opp.side.value}"

        if not self.quiet:
            console.print(
                f"[bold {action_color}]MICRO [{action_str}][/bold {action_color}] "
                f"{opp.symbol.replace('usdt','').upper()} "
                f"| Mom: {opp.momentum:+.2f} "
                f"| OFI: {opp.ofi_5s:+.2f}/{opp.ofi_15s:+.2f} "
                f"| Mkt: {opp.market_price:.2f} ({price_source}) "
                f"| ${opp.binance_price:,.2f} "
                f"| {opp.seconds_remaining:.0f}s left"
            )

        if self.dry_run:
            # Update virtual position tracking even in dry run
            if action == MicroAction.EXIT:
                self._positions.pop(cid, None)
            elif action == MicroAction.FLIP_YES:
                self._positions[cid] = "yes"
            elif action == MicroAction.FLIP_NO:
                self._positions[cid] = "no"
            elif action == MicroAction.BUY_YES:
                self._positions[cid] = "yes"
            elif action == MicroAction.BUY_NO:
                self._positions[cid] = "no"

            self._trades_this_window += 1
            self._total_trades += 1
            if opp.is_flip:
                self._total_flips += 1
            self._last_trade_log[cid] = time.time()
            return

        # Live trading
        bankroll = await self._get_bankroll()
        max_pct = min(
            self.config.max_position_per_trade,
            self.settings.risk.max_position_pct,
        )

        # Smaller position sizes for micro trades
        est_edge = abs(opp.momentum) * 0.15
        size_usd = calculate_position_size(
            bankroll=bankroll,
            edge=est_edge,
            probability=0.55,  # Conservative for momentum trades
            kelly_fraction=self.settings.risk.kelly_fraction,
            max_position_pct=max_pct,
        )

        if size_usd < 1.0:
            if not self.quiet:
                console.print("[dim]  Position too small — skipping[/dim]")
            return

        if not self.quiet:
            console.print(
                f"  [bold]Sizing: ${size_usd:.2f} "
                f"({size_usd/bankroll*100:.1f}% of ${bankroll:.0f})[/bold]"
            )

        if self.auto_execute:
            await self._execute_micro_trade(opp, size_usd)
        else:
            try:
                response = input(f"  Execute {action_str}? (y/n): ").strip().lower()
                if response == "y":
                    await self._execute_micro_trade(opp, size_usd)
                else:
                    console.print("[dim]  Skipped by user[/dim]")
            except EOFError:
                pass

    async def _execute_micro_trade(
        self,
        opp: MicroOpportunity,
        size_usd: float,
    ):
        """Execute a micro trade."""
        from polyedge.execution.engine import ExecutionEngine

        engine = ExecutionEngine(self.client, self.db, self.settings)
        cid = opp.market.condition_id
        action = opp.action

        # For EXIT and FLIP, we'd need to sell the existing position first
        if action == MicroAction.EXIT:
            # Sell existing position
            success = await self._close_position(engine, opp)
            if success:
                self._positions.pop(cid, None)
                self._trades_this_window += 1
                self._total_trades += 1
                self._last_trade_log[cid] = time.time()
            return

        if action in (MicroAction.FLIP_YES, MicroAction.FLIP_NO):
            # Close existing position first
            await self._close_position(engine, opp)
            self._total_flips += 1
            # Then fall through to open new position

        # Open new position (BUY_YES, BUY_NO, or second leg of FLIP)
        token_id = (
            opp.market.yes_token_id if opp.side == Side.YES
            else opp.market.no_token_id
        )
        if not token_id:
            console.print("[red]  No token ID — can't trade[/red]")
            return

        price = opp.market_price
        size = size_usd / price if price > 0 else 0

        signal = self.strategy.opportunity_to_signal(opp)

        try:
            order_id = await engine.place_order(
                market=opp.market,
                token_id=token_id,
                side=opp.side.value,
                price=price,
                size=size,
                amount_usd=size_usd,
                strategy="micro_sniper",
                reasoning=signal.reasoning,
                force=self.auto_execute,
            )

            if order_id:
                self._positions[cid] = "yes" if opp.side == Side.YES else "no"
                self._trades_this_window += 1
                self._total_trades += 1
                self._last_trade_log[cid] = time.time()
                console.print(f"[green]  MICRO SNIPED! Order: {order_id}[/green]")
            else:
                console.print("[red]  Trade failed[/red]")

        except Exception as e:
            console.print(f"[red]  Execution error: {e}[/red]")
            logger.error(f"Micro execution failed: {e}", exc_info=True)

    async def _close_position(self, engine, opp: MicroOpportunity) -> bool:
        """Close an existing position (sell back)."""
        cid = opp.market.condition_id
        current_pos = self._positions.get(cid)
        if not current_pos:
            return True  # Nothing to close

        # Determine which token to sell
        if current_pos == "yes":
            token_id = opp.market.yes_token_id
            price = opp.market.yes_price
        else:
            token_id = opp.market.no_token_id
            price = opp.market.no_price

        if not token_id:
            return False

        try:
            # TODO: Implement sell/close order via engine
            # For now, we just track the position state
            # In a real implementation, this would place a sell order
            logger.info(f"Closing {current_pos} position on {cid} at {price:.2f}")
            return True
        except Exception as e:
            logger.error(f"Failed to close position: {e}")
            return False

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    async def _status_loop(self):
        """Periodically print status."""
        while self.running:
            await asyncio.sleep(STATUS_INTERVAL)

            if self._poly_task and self._poly_task.done():
                self._poly_connected = False

            # Get microstructure state for display
            micro_lines = []
            for sym in self.config.symbols:
                micro = self.agg_feed.get_micro(sym)
                if not micro or not micro.flow_5s.is_active:
                    continue

                sym_short = sym.replace("usdt", "").upper()
                ofi = micro.flow_5s.ofi
                momentum = micro.momentum_signal
                intensity = micro.flow_5s.trade_intensity
                price = micro.current_price

                # Direction indicator
                if momentum > 0.15:
                    arrow = "▲"
                elif momentum < -0.15:
                    arrow = "▼"
                else:
                    arrow = "─"

                micro_lines.append(
                    f"{sym_short}: ${price:,.2f} {arrow} "
                    f"Mom:{momentum:+.2f} OFI:{ofi:+.2f} "
                    f"{intensity:.0f}tps"
                )

            n_pos = len(self._positions)
            pos_str = f"Pos: {n_pos}" if n_pos > 0 else "Flat"
            poly_str = f"live/{self._ws_price_updates}" if self._poly_connected else "api"

            console.print(
                f"\n[bold dim]── Micro Status ── "
                f"{' | '.join(micro_lines) if micro_lines else 'waiting for data'} | "
                f"Mkts: {self._total_markets} | "
                f"{pos_str} | "
                f"Trades: {self._total_trades} (flips: {self._total_flips}) | "
                f"Evals: {self._eval_count} | "
                f"Prices: {poly_str}[/bold dim]"
            )

    async def _get_bankroll(self) -> float:
        """Get current bankroll."""
        try:
            positions = await self.db.get_open_positions()
            exposure = sum(p.get("size", 0) * p.get("entry_price", 0) for p in positions)
            return max(0, 200.0 - exposure)
        except Exception:
            return 200.0
