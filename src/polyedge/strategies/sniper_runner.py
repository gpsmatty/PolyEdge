"""Sniper Runner — real-time loop that connects Binance prices to ALL Polymarket
crypto markets and executes sniper trades.

Both price feeds are real-time:
- Binance WebSocket: live crypto spot prices (~100ms updates)
- Polymarket WebSocket: live market prices (best bid/ask, trades)

This ensures edges are computed against REAL current prices, not stale
60-second-old Gamma API snapshots.

Handles three market types via a unified pipeline:
1. Up/Down — short-duration direction bets (evaluated on every price tick)
2. Threshold — "above X" price level bets (evaluated periodically)
3. Bucket — "between X and Y" range bets (evaluated periodically)

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

from polyedge.core.config import Settings
from polyedge.core.client import PolyClient
from polyedge.core.db import Database
from polyedge.core.models import Market, Signal, Side
from polyedge.data.binance_feed import BinanceFeed, PriceSnapshot, PriceWindow
from polyedge.data.indexer import MarketIndexer
from polyedge.data.ws_feed import MarketFeed, EVENT_BEST_BID_ASK, EVENT_LAST_TRADE
from polyedge.risk.sizing import calculate_position_size
from polyedge.strategies.crypto_sniper import (
    CryptoSniperStrategy,
    CryptoMarketType,
    ParsedCryptoMarket,
    SniperOpportunity,
    find_crypto_markets,
)

logger = logging.getLogger("polyedge.sniper_runner")
console = Console(force_terminal=True, force_jupyter=False)

# How often to re-read crypto markets from the DB.
# The MarketIndexer handles API sync separately (default every 15 min).
MARKET_REFRESH_INTERVAL = 120  # seconds

# How often to evaluate threshold/bucket markets
SLOW_EVAL_INTERVAL = 30  # seconds


class SniperRunner:
    """Persistent real-time loop for crypto sniper strategy.

    Uses dual WebSocket feeds:
    - Binance: real-time crypto spot prices (the "oracle")
    - Polymarket: real-time market prices (what we're trading against)

    The Polymarket WS is optional — if it fails or disconnects, we fall
    back to Gamma API prices (updated every 60s). The status line shows
    whether prices are "live" or "api" so we know what we're working with.
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
    ):
        self.settings = settings
        self.client = client
        self.db = db
        self.auto_execute = auto_execute
        self.dry_run = dry_run
        # --quiet suppresses EVAL skip lines; --verbose forces them on.
        # Default: verbose in dry-run, quiet in live modes.
        if quiet:
            self.verbose = False
        elif verbose:
            self.verbose = True
        else:
            self.verbose = dry_run  # Default: verbose only in dry-run
        self.running = False

        # Strategy
        self.strategy = CryptoSniperStrategy(settings)
        self.config = settings.strategies.crypto_sniper
        sniper_config = self.config

        # Market indexer — reads from DB, syncs from API periodically
        self.indexer = MarketIndexer(settings, db)

        # Binance price feed (crypto spot prices — required)
        self.binance = BinanceFeed(symbols=sniper_config.symbols)

        # Polymarket price feed (market prices — optional, enhances accuracy)
        self.poly_feed = MarketFeed(settings)
        self._poly_task: Optional[asyncio.Task] = None
        self._poly_connected = False

        # Active crypto markets grouped by type
        self.updown_markets: dict[str, list[tuple[Market, ParsedCryptoMarket]]] = {}
        self.slow_markets: list[tuple[Market, ParsedCryptoMarket]] = []

        # Lookup: token_id -> (Market, "yes" | "no") for WS price updates
        self._token_to_market: dict[str, tuple[Market, str]] = {}

        # Track which token IDs the WS is currently subscribed to
        self._subscribed_tokens: set[str] = set()

        # Track which markets we've already traded to avoid double-entry
        self._traded_markets: set[str] = set()

        # Stats
        self.opportunities_seen = 0
        self.trades_executed = 0
        self.total_edge_captured = 0.0
        self._total_markets = 0
        self._ws_price_updates = 0

        # Verbose eval tracking — avoid spamming every tick
        self._last_verbose_updown: float = 0.0
        self._verbose_updown_interval = 30.0  # Print up/down evals every 30s

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
            f"Dual WebSocket: Binance + Polymarket[/dim]"
        )

        # Initial market sync — ensure DB has fresh data before we start
        console.print("[dim]Syncing all markets from Polymarket API...[/dim]")
        try:
            synced = await self.indexer.sync(force=True)
            console.print(f"[green]Synced {synced} markets to DB[/green]")
        except Exception as e:
            console.print(f"[yellow]Initial sync failed ({e}) — using existing DB data[/yellow]")

        # Register Binance price callback for up/down opportunity detection
        self.binance.on_any_price(self._on_price_update)

        # Register Polymarket WS callbacks for live market prices
        self.poly_feed.on(EVENT_BEST_BID_ASK, self._on_poly_price)
        self.poly_feed.on(EVENT_LAST_TRADE, self._on_poly_trade)

        # Start all managed tasks
        tasks = [
            asyncio.create_task(self.binance.start()),
            asyncio.create_task(self._market_refresh_loop()),
            asyncio.create_task(self._slow_eval_loop()),
            asyncio.create_task(self._status_loop()),
        ]

        try:
            # Wait for Binance connection (required)
            for _ in range(50):
                if self.binance.is_connected:
                    break
                await asyncio.sleep(0.1)

            if not self.binance.is_connected:
                console.print("[yellow]Waiting for Binance connection...")

            await asyncio.gather(*tasks, return_exceptions=True)
        except KeyboardInterrupt:
            console.print("\n[yellow]Sniper stopped by user")
        finally:
            self.running = False
            await self.binance.stop()
            await self._stop_poly_feed()
            for t in tasks:
                t.cancel()

    async def stop(self):
        self.running = False
        await self.binance.stop()
        await self._stop_poly_feed()

    # ------------------------------------------------------------------
    # Polymarket WebSocket — managed lifecycle
    # ------------------------------------------------------------------

    async def _start_poly_feed(self, token_ids: list[str]):
        """Start or restart the Polymarket WebSocket with the given token IDs.

        This is called from _refresh_crypto_markets whenever the market
        list changes. It cleanly stops any existing connection before
        starting a new one with the updated token IDs.
        """
        # Stop existing connection if any
        await self._stop_poly_feed()

        if not token_ids:
            return

        self._subscribed_tokens = set(token_ids)

        async def _run_poly():
            try:
                console.print(
                    f"[dim]Polymarket WS: connecting ({len(token_ids)} tokens)...[/dim]"
                )
                self._poly_connected = False
                await self.poly_feed.start(token_ids)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"Polymarket WS error: {e}")
                self._poly_connected = False

        self._poly_task = asyncio.create_task(_run_poly())

        # Give it a moment to connect
        for _ in range(30):  # 3 second timeout
            if self.poly_feed.is_connected:
                self._poly_connected = True
                console.print(
                    f"[dim]Polymarket WS: live ({len(token_ids)} tokens)[/dim]"
                )
                break
            await asyncio.sleep(0.1)

        if not self._poly_connected:
            console.print(
                "[dim]Polymarket WS: connecting in background (using API prices for now)[/dim]"
            )

    async def _stop_poly_feed(self):
        """Cleanly stop the Polymarket WebSocket."""
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
        """Handle Polymarket best_bid_ask — update market YES/NO prices live."""
        asset_id = event.get("asset_id", "")
        entry = self._token_to_market.get(asset_id)
        if not entry:
            return

        market, side = entry
        best_bid = float(event.get("best_bid", 0))
        best_ask = float(event.get("best_ask", 0))

        # Use midpoint as current price
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

        self._poly_connected = True  # Got data = connected
        self._ws_price_updates += 1

    async def _on_poly_trade(self, event: dict):
        """Handle Polymarket last_trade — update prices on actual trades."""
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
        """Periodically fetch active crypto markets from Polymarket."""
        while self.running:
            try:
                await self._refresh_crypto_markets()
            except Exception as e:
                logger.error(f"Market refresh failed: {e}")

            await asyncio.sleep(MARKET_REFRESH_INTERVAL)

    async def _refresh_crypto_markets(self):
        """Load crypto markets from the DB (synced by MarketIndexer).

        The indexer handles full API pagination (100/page until exhausted),
        upserts to Postgres, and deactivates stale/closed markets. We just
        read from the DB — fast, complete, no API rate limit worries.

        Also manages Polymarket WebSocket subscriptions — restarts the
        WS connection when the token ID set changes significantly.
        """
        try:
            # Indexer auto-syncs from API if data is stale (default: every 15 min)
            all_markets = await self.indexer.get_markets(
                min_liquidity=self.settings.strategies.crypto_sniper.min_liquidity,
                limit=5000,
            )
        except Exception as e:
            logger.warning(f"Failed to load markets from DB: {e}")
            return

        crypto = find_crypto_markets(all_markets)

        if self.verbose:
            sync_ago = self.indexer.minutes_since_sync
            sync_str = f"{sync_ago:.0f}m ago" if sync_ago else "just now"
            console.print(
                f"\n[bold cyan]── Market Refresh ──[/bold cyan] "
                f"{len(all_markets)} markets in DB (liq ≥ ${self.config.min_liquidity:,.0f}), "
                f"{len(crypto)} are crypto "
                f"(last sync: {sync_str})"
            )

        # Classify and group
        self.updown_markets.clear()
        self.slow_markets.clear()
        self._token_to_market.clear()

        new_token_ids: list[str] = []
        rejected_no_parse = 0
        rejected_symbol = 0

        for market in crypto:
            parsed = self.strategy.parse_market(market)
            if not parsed:
                if self.verbose:
                    # Show why it was rejected
                    mtype = self.strategy.classify_market(market)
                    reason = "unknown type"
                    if mtype is not None:
                        from polyedge.strategies.crypto_sniper import EXCLUDED_PATTERNS
                        if EXCLUDED_PATTERNS.search(market.question):
                            reason = "excluded pattern"
                        elif not self.strategy.get_symbol(market):
                            reason = "no symbol match"
                        elif not self.strategy._within_horizon(market):
                            reason = "beyond 7-day horizon"
                        elif mtype == CryptoMarketType.THRESHOLD:
                            reason = "can't extract strike"
                        elif mtype == CryptoMarketType.BUCKET:
                            reason = "can't parse bucket range"
                    console.print(
                        f"  [dim red]REJECT[/dim red] {market.question[:75]} "
                        f"[dim]({reason})[/dim]"
                    )
                rejected_no_parse += 1
                continue

            if parsed.symbol not in self.settings.strategies.crypto_sniper.symbols:
                if self.verbose:
                    console.print(
                        f"  [dim red]REJECT[/dim red] {market.question[:75]} "
                        f"[dim](symbol {parsed.symbol} not in config)[/dim]"
                    )
                rejected_symbol += 1
                continue

            # Register token IDs for WebSocket price tracking
            if len(market.clob_token_ids) >= 2:
                self._token_to_market[market.clob_token_ids[0]] = (market, "yes")
                self._token_to_market[market.clob_token_ids[1]] = (market, "no")
                new_token_ids.extend(market.clob_token_ids[:2])
            elif len(market.clob_token_ids) == 1:
                self._token_to_market[market.clob_token_ids[0]] = (market, "yes")
                new_token_ids.append(market.clob_token_ids[0])

            if parsed.market_type == CryptoMarketType.UP_DOWN:
                if parsed.symbol not in self.updown_markets:
                    self.updown_markets[parsed.symbol] = []
                self.updown_markets[parsed.symbol].append((market, parsed))

                window = self.binance.get_window(parsed.symbol)
                if window and window.window_start_price <= 0:
                    self.binance.start_window(parsed.symbol)

                if self.verbose:
                    console.print(
                        f"  [green]UP/DOWN[/green] {parsed.symbol.replace('usdt','').upper()} "
                        f"| YES: {market.yes_price:.2f} NO: {market.no_price:.2f} "
                        f"| Liq: ${market.liquidity:,.0f} "
                        f"| {market.question[:60]}"
                    )
            else:
                self.slow_markets.append((market, parsed))

                if self.verbose:
                    label = parsed.market_type.value.upper()
                    detail = ""
                    if parsed.strike:
                        detail = f"Strike: ${parsed.strike:,.2f}"
                        if parsed.is_bearish:
                            detail += " (bearish)"
                    elif parsed.bucket_low and parsed.bucket_high:
                        detail = f"Range: ${parsed.bucket_low:,.2f}-${parsed.bucket_high:,.2f}"
                    elif parsed.bucket_direction:
                        val = parsed.bucket_low or parsed.bucket_high
                        detail = f"{parsed.bucket_direction} ${val:,.2f}" if val else ""

                    remaining = ""
                    if market.end_date:
                        secs = (market.end_date - datetime.now(timezone.utc)).total_seconds()
                        if secs > 3600:
                            remaining = f"{secs/3600:.1f}h left"
                        else:
                            remaining = f"{secs/60:.0f}m left"

                    console.print(
                        f"  [yellow]{label:9s}[/yellow] {parsed.symbol.replace('usdt','').upper()} "
                        f"| {detail} "
                        f"| YES: {market.yes_price:.2f} NO: {market.no_price:.2f} "
                        f"| Liq: ${market.liquidity:,.0f} "
                        f"| {remaining} "
                        f"| {market.question[:55]}"
                    )

        n_updown = sum(len(v) for v in self.updown_markets.values())
        n_slow = len(self.slow_markets)
        self._total_markets = n_updown + n_slow

        if self.verbose:
            console.print(
                f"[cyan]  Tracking: {n_updown} up/down + {n_slow} threshold/bucket "
                f"| Rejected: {rejected_no_parse} (parse) + {rejected_symbol} (symbol)[/cyan]"
            )

        # Manage Polymarket WS — restart if token set changed
        new_token_set = set(new_token_ids)
        if new_token_set != self._subscribed_tokens and new_token_ids:
            await self._start_poly_feed(new_token_ids)

        if self._total_markets > 0 and not self.verbose:
            parts = []
            if n_updown > 0:
                ud_str = ", ".join(
                    f"{s.upper()}({len(m)})"
                    for s, m in self.updown_markets.items()
                )
                parts.append(f"Up/Down: {ud_str}")
            if n_slow > 0:
                parts.append(f"Threshold/Bucket: {n_slow}")
            ws_label = "live" if self._poly_connected else "connecting"
            logger.info(
                f"Tracking {self._total_markets} markets — {' | '.join(parts)} "
                f"| Poly WS: {ws_label}"
            )

    # ------------------------------------------------------------------
    # Binance price callback — evaluate up/down markets
    # ------------------------------------------------------------------

    async def _on_price_update(self, snapshot: PriceSnapshot):
        """Called on every Binance price tick — evaluate up/down markets."""
        symbol = snapshot.symbol
        markets = self.updown_markets.get(symbol, [])
        if not markets:
            return

        window = self.binance.get_window(symbol)
        if not window or window.window_start_price <= 0:
            return

        # Verbose: periodically dump all up/down evaluations
        now_mono = time.monotonic()
        show_verbose = (
            self.verbose
            and now_mono - self._last_verbose_updown >= self._verbose_updown_interval
        )

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
            elif show_verbose:
                # Show WHY there's no opportunity
                self._verbose_updown_eval(
                    market, parsed, window, snapshot, remaining,
                )

        if show_verbose:
            self._last_verbose_updown = now_mono

    def _verbose_updown_eval(
        self,
        market: Market,
        parsed: ParsedCryptoMarket,
        window: PriceWindow,
        snapshot: PriceSnapshot,
        remaining: float,
    ):
        """Print verbose evaluation for an up/down market (no opportunity)."""
        change_pct = window.change_pct
        abs_change = abs(change_pct)
        sym = parsed.symbol.replace("usdt", "").upper()

        if remaining > self.config.max_seconds_before_entry:
            console.print(
                f"  [dim]EVAL {sym} UP/DOWN | {remaining:.0f}s left "
                f"(waiting, entry window > {self.config.max_seconds_before_entry:.0f}s)[/dim]"
            )
            return

        if abs_change < self.config.min_price_move_pct:
            direction = "UP" if change_pct > 0 else ("DOWN" if change_pct < 0 else "FLAT")
            console.print(
                f"  [dim]EVAL {sym} UP/DOWN | ${snapshot.price:,.2f} "
                f"| Move: {change_pct:+.4%} ({direction}) "
                f"| {remaining:.0f}s left "
                f"| SKIP: move < {self.config.min_price_move_pct:.3%} min[/dim]"
            )
            return

        # Compute model probability for display
        implied_prob = self.strategy._compute_direction_probability(
            abs_change, remaining, window.volatility,
        )
        if change_pct > 0:
            mkt_price = market.yes_price
            side_label = "YES(up)"
        else:
            mkt_price = market.no_price
            side_label = "NO(down)"

        edge = implied_prob - mkt_price
        console.print(
            f"  [dim]EVAL {sym} UP/DOWN | ${snapshot.price:,.2f} "
            f"| Move: {change_pct:+.4%} | Model: {implied_prob:.1%} {side_label} "
            f"| Mkt: {mkt_price:.1%} | Edge: {edge:+.1%} "
            f"| {remaining:.0f}s left "
            f"| SKIP: edge < {self.config.min_edge:.0%}[/dim]"
        )

    def _verbose_slow_eval(
        self,
        market: Market,
        parsed: ParsedCryptoMarket,
        snapshot: PriceSnapshot,
        remaining: float,
    ):
        """Print verbose evaluation for a threshold/bucket market (no opportunity)."""
        sym = parsed.symbol.replace("usdt", "").upper()
        price = snapshot.price
        time_str = f"{remaining/3600:.1f}h" if remaining > 3600 else f"{remaining/60:.0f}m"

        if parsed.market_type == CryptoMarketType.THRESHOLD:
            strike = parsed.strike or 0

            if parsed.is_touch:
                # Touch/barrier market — use first-passage probability
                if parsed.is_bearish:
                    implied_yes = self.strategy._compute_touch_probability_lower(
                        current_price=price, barrier=strike,
                        seconds_remaining=remaining, symbol=parsed.symbol,
                    )
                    label = f"DIP TO ${strike:,.2f}"
                else:
                    implied_yes = self.strategy._compute_touch_probability_upper(
                        current_price=price, barrier=strike,
                        seconds_remaining=remaining, symbol=parsed.symbol,
                    )
                    label = f"REACH ${strike:,.2f}"
            else:
                # Terminal market — use log-normal terminal CDF
                prob_above = self.strategy._compute_threshold_probability(
                    current_price=price, strike=strike,
                    seconds_remaining=remaining, symbol=parsed.symbol,
                )
                if parsed.is_bearish:
                    implied_yes = 1 - prob_above
                    label = f"< ${strike:,.2f}"
                else:
                    implied_yes = prob_above
                    label = f"> ${strike:,.2f}"

            if implied_yes >= 0.5:
                side_label, mkt_price = "YES", market.yes_price
                edge = implied_yes - mkt_price
            else:
                side_label, mkt_price = "NO", market.no_price
                edge = (1 - implied_yes) - mkt_price

            console.print(
                f"  [dim]EVAL {sym} THRESHOLD {label} "
                f"| ${price:,.2f} "
                f"| Model: {implied_yes:.1%} YES "
                f"| Mkt: YES={market.yes_price:.2f} NO={market.no_price:.2f} "
                f"| Best: {side_label}={mkt_price:.2f} "
                f"| Edge: {edge:+.1%} "
                f"| {time_str} "
                f"| SKIP: edge < {self.config.min_edge:.0%}[/dim]"
            )

        elif parsed.market_type == CryptoMarketType.BUCKET:
            if parsed.bucket_direction == "above" and parsed.bucket_low:
                implied_prob = self.strategy._compute_threshold_probability(
                    price, parsed.bucket_low, remaining, parsed.symbol,
                )
                label = f"↑ ${parsed.bucket_low:,.2f}"
            elif parsed.bucket_direction == "below" and parsed.bucket_high:
                prob_above = self.strategy._compute_threshold_probability(
                    price, parsed.bucket_high, remaining, parsed.symbol,
                )
                implied_prob = 1 - prob_above
                label = f"↓ ${parsed.bucket_high:,.2f}"
            elif parsed.bucket_low and parsed.bucket_high:
                implied_prob = self.strategy._compute_bucket_probability(
                    price, parsed.bucket_low, parsed.bucket_high, remaining, parsed.symbol,
                )
                label = f"${parsed.bucket_low:,.2f}-${parsed.bucket_high:,.2f}"
            else:
                console.print(f"  [dim]EVAL {sym} BUCKET | unparseable range[/dim]")
                return

            if implied_prob >= 0.5:
                side_label, mkt_price = "YES", market.yes_price
                edge = implied_prob - mkt_price
            else:
                side_label, mkt_price = "NO", market.no_price
                edge = (1 - implied_prob) - mkt_price

            console.print(
                f"  [dim]EVAL {sym} BUCKET [{label}] "
                f"| ${price:,.2f} "
                f"| Model: {implied_prob:.1%} in-range "
                f"| Mkt: YES={market.yes_price:.2f} NO={market.no_price:.2f} "
                f"| Best: {side_label}={mkt_price:.2f} "
                f"| Edge: {edge:+.1%} "
                f"| {time_str} "
                f"| SKIP: edge < {self.config.min_edge:.0%}[/dim]"
            )

    # ------------------------------------------------------------------
    # Slow eval loop — threshold and bucket markets
    # ------------------------------------------------------------------

    async def _slow_eval_loop(self):
        """Periodically evaluate threshold and bucket markets."""
        while self.running:
            await asyncio.sleep(SLOW_EVAL_INTERVAL)

            if self.verbose and self.slow_markets:
                console.print(
                    f"\n[bold cyan]── Threshold/Bucket Eval "
                    f"({len(self.slow_markets)} markets) ──[/bold cyan]"
                )

            for market, parsed in self.slow_markets:
                if market.condition_id in self._traded_markets:
                    continue
                if not market.end_date:
                    continue

                now = datetime.now(timezone.utc)
                remaining = (market.end_date - now).total_seconds()
                if remaining <= 0:
                    if self.verbose:
                        console.print(f"  [dim red]EXPIRED[/dim red] {market.question[:70]}")
                    continue

                snapshot = self.binance.get_price(parsed.symbol)
                if not snapshot or not snapshot.is_fresh:
                    if self.verbose:
                        console.print(
                            f"  [dim red]NO PRICE[/dim red] {parsed.symbol.upper()} "
                            f"| {market.question[:60]}"
                        )
                    continue

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
                elif self.verbose:
                    self._verbose_slow_eval(market, parsed, snapshot, remaining)

    # ------------------------------------------------------------------
    # Opportunity handling and execution
    # ------------------------------------------------------------------

    async def _handle_opportunity(self, opp: SniperOpportunity):
        """Handle a detected sniper opportunity."""
        signal = self.strategy.opportunity_to_signal(opp)
        price_source = "live" if self._poly_connected else "api"

        type_label = opp.market_type.value.upper()
        console.print(
            f"\n[bold yellow]SNIPER [{type_label}][/bold yellow] "
            f"{opp.symbol.upper()} {opp.side.value} "
            f"| Edge: {opp.edge:.1%} "
            f"| Binance: ${opp.binance_price:,.2f} "
            f"{'| Strike: $' + f'{opp.strike:,.2f}' if opp.strike else ''}"
            f"| Mkt: {opp.market_price:.1%} ({price_source}) "
            f"| Model: {opp.implied_prob:.1%}"
        )

        if self.dry_run:
            console.print(f"[dim]  {opp.market.question[:80]}[/dim]")
            console.print("[dim]  (dry run — not trading)[/dim]")
            return

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
            # Side is always "BUY" for entry — token_id determines YES vs NO.
            order_id = await engine.place_order(
                market=opp.market,
                token_id=token_id,
                side="BUY",
                price=price,
                size=size,
                amount_usd=size_usd,
                strategy="crypto_sniper",
                reasoning=signal.reasoning,
                force=self.auto_execute,
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

    # ------------------------------------------------------------------
    # Status and utilities
    # ------------------------------------------------------------------

    async def _status_loop(self):
        """Periodically print status."""
        while self.running:
            await asyncio.sleep(30)

            # Check if poly WS is still alive
            if self._poly_task and self._poly_task.done():
                self._poly_connected = False

            prices = self.binance.get_all_prices()
            if prices:
                price_str = " | ".join(
                    f"{s.replace('usdt','').upper()}: ${p:,.2f}"
                    for s, p in prices.items()
                )
                n_updown = sum(len(v) for v in self.updown_markets.values())
                n_slow = len(self.slow_markets)

                if self._poly_connected:
                    poly_str = f"live/{self._ws_price_updates}"
                else:
                    poly_str = "api"

                console.print(
                    f"\n[bold dim]── Status ── "
                    f"{price_str} | "
                    f"Mkts: {n_updown} ud + {n_slow} th/bk | "
                    f"Prices: {poly_str} | "
                    f"Opps: {self.opportunities_seen} | "
                    f"Trades: {self.trades_executed}[/bold dim]"
                )

    async def _get_bankroll(self) -> float:
        """Get current bankroll."""
        try:
            positions = await self.db.get_open_positions()
            exposure = sum(p.get("size", 0) * p.get("entry_price", 0) for p in positions)
            return max(0, 200.0 - exposure)
        except Exception:
            return 200.0
