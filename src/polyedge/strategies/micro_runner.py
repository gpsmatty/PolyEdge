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
        # but NOT "btc-updown-15m-..." (5m != 15m)
        self._filter_words = self.market_filter.split() if self.market_filter else []

        # Pre-compile regex patterns for each filter word — match as whole
        # segments in slug (split by -) or whole words in question text.
        # This prevents "5m" from matching inside "15m".
        import re
        self._filter_patterns = [
            re.compile(r'(?:^|[\s\-–,])' + re.escape(w) + r'(?:[\s\-–,]|$)', re.IGNORECASE)
            for w in self._filter_words
        ]

        # Config
        self.config = settings.strategies.micro_sniper

        # Lock to prevent concurrent _quick_sync calls (refresh loop + hop can race)
        self._sync_lock = asyncio.Lock()

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
        self._trade_cooldown: float = 10.0  # seconds between trades on same market

        # On startup, wait for the next fresh window instead of jumping into
        # a partially-elapsed one with stale microstructure data.
        self._waiting_for_fresh_window: bool = True
        self._startup_window_id: str | None = None  # condition_id of the window we're skipping

    async def _quick_sync(self):
        """Targeted API fetch for crypto 5-min markets.

        The generic volume-sorted fetch misses short-duration crypto markets
        because they're buried behind high-volume political markets. Instead,
        sort by endDate ascending (soonest-ending = currently live) and page
        through until we find matches. Typically finds them within 2-5 pages.
        """
        import aiohttp
        from polyedge.data.markets import _parse_market

        gamma_url = self.settings.polymarket.gamma_url
        found: list[Market] = []
        batch_size = 100

        console.print("[dim]Targeted fetch (sorting by endDate)...[/dim]")

        # Fetch markets sorted by endDate ascending — currently-live markets
        # appear first since they expire soonest
        for page in range(20):  # Up to 2000 markets, usually find matches in <5 pages
            url = f"{gamma_url}/markets"
            params = {
                "limit": batch_size,
                "offset": page * batch_size,
                "active": "true",
                "closed": "false",
                "order": "endDate",
                "ascending": "true",
            }

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, params=params) as resp:
                        if resp.status != 200:
                            logger.warning(f"Gamma API error: {resp.status}")
                            break
                        data = await resp.json()
            except Exception as e:
                logger.warning(f"Quick sync page {page} failed: {e}")
                break

            if not data:
                break

            # Parse and check for matches
            page_markets = []
            for item in data:
                try:
                    m = _parse_market(item)
                    page_markets.append(m)
                except (KeyError, ValueError):
                    continue

            found.extend(page_markets)

            # Check if we have any matching crypto markets yet
            matches = self._count_filter_matches(page_markets)
            if matches > 0:
                console.print(
                    f"[green]Found {matches} matching markets on page {page + 1} "
                    f"({len(found)} total fetched)[/green]"
                )
                # Grab 5 more pages to pre-load lots of upcoming windows
                # (5-min markets = ~12 per hour, 5 pages = ~50 windows)
                for extra_page in range(1, 6):
                    extra_offset = (page + extra_page) * batch_size
                    params["offset"] = extra_offset
                    try:
                        async with aiohttp.ClientSession() as session:
                            async with session.get(url, params=params) as resp:
                                if resp.status == 200:
                                    extra = await resp.json()
                                    if not extra:
                                        break
                                    for item in extra:
                                        try:
                                            found.append(_parse_market(item))
                                        except (KeyError, ValueError):
                                            continue
                    except Exception:
                        break
                break

            if len(data) < batch_size:
                break  # Last page

        if not found:
            console.print("[yellow]Targeted fetch returned 0 markets[/yellow]")
            return []

        # Upsert to DB for persistence
        market_dicts = []
        for m in found:
            market_dicts.append({
                "condition_id": m.condition_id,
                "question": m.question,
                "slug": m.slug,
                "description": m.description,
                "category": m.category,
                "end_date": m.end_date,
                "active": m.active,
                "closed": m.closed,
                "clob_token_ids": m.clob_token_ids,
                "yes_price": m.yes_price,
                "no_price": m.no_price,
                "volume": m.volume,
                "liquidity": m.liquidity,
                "spread": m.spread,
                "raw": m.raw or {},
            })

        try:
            await self.db.bulk_upsert_markets(market_dicts)
        except Exception as e:
            logger.warning(f"DB upsert failed (non-fatal): {e}")

        console.print(f"[green]Fetched {len(found)} markets[/green]")
        return found

    def _count_filter_matches(self, markets: list[Market]) -> int:
        """Count how many markets match the current filter + crypto pattern."""
        count = 0
        now = datetime.now(timezone.utc)
        for m in markets:
            if not m.end_date or m.end_date <= now:
                continue
            if not UP_DOWN_PATTERN.search(m.question):
                continue
            if not self._matches_filter(m):
                continue
            count += 1
        return count

    def _matches_filter(self, market: Market) -> bool:
        """Check if a market matches the --market filter words.

        Uses word-boundary matching so "5m" matches "btc-updown-5m-123"
        but NOT "btc-updown-15m-123".
        """
        if not self._filter_patterns:
            return True
        haystack = f"{market.question} {market.slug or ''}"
        return all(p.search(haystack) for p in self._filter_patterns)

    async def run(self):
        """Main micro sniper loop."""
        self.running = True

        mode = "DRY RUN" if self.dry_run else ("AUTOPILOT" if self.auto_execute else "COPILOT")
        console.print(f"\n[bold green]Micro Sniper started in {mode} mode[/bold green]")
        flip_str = f"Flip: momentum > {self.config.flip_threshold:.0%}" if self.config.enable_flips else "Flips: OFF"
        size_str = f"${self.config.fixed_position_usd:.0f}/trade" if self.config.fixed_position_usd > 0 else f"{self.config.max_position_per_trade:.0%} Kelly"
        console.print(
            f"[dim]Entry: momentum > {self.config.entry_threshold:.0%} "
            f"(counter-trend > {self.config.counter_trend_threshold:.0%}) | "
            f"{flip_str} | {size_str} | "
            f"Price range: {self.config.min_entry_price:.2f}-{self.config.max_entry_price:.2f}[/dim]"
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
            if self._total_markets <= 3:
                if self._total_markets == 0:
                    console.print("[yellow]No matching markets in DB — fetching from API...[/yellow]")
                else:
                    console.print(f"[yellow]Only {self._total_markets} window(s) from DB — fetching more from API...[/yellow]")
                try:
                    fetched = await self._quick_sync()
                    if fetched:
                        await self._refresh_markets(prefetched=fetched)
                except Exception as e:
                    console.print(f"[yellow]Quick fetch failed ({e})[/yellow]")
        else:
            console.print("[dim]Syncing markets from API...[/dim]")
            try:
                synced = await self.indexer.sync(force=True)
                console.print(f"[green]Synced {synced} markets[/green]")
            except Exception as e:
                console.print(f"[yellow]Sync failed ({e}) — using DB data[/yellow]")

        # Narrow Binance feeds to ONLY symbols with matching markets.
        # No point subscribing to ETH/SOL/XRP/DOGE streams when we're
        # only trading BTC — saves bandwidth and latency.
        active_symbols = list(self.updown_markets.keys())
        if active_symbols and set(active_symbols) != set(self.agg_feed.symbols):
            console.print(
                f"[dim]Binance feeds narrowed to: "
                f"{', '.join(s.replace('usdt','').upper() for s in active_symbols)}[/dim]"
            )
            self.agg_feed = BinanceAggTradeFeed(symbols=active_symbols)
            self.ticker_feed = BinanceFeed(symbols=active_symbols)

        # Log that we're waiting for a fresh window on startup
        if self._waiting_for_fresh_window and self._total_markets > 0:
            for sym, mlist in self.updown_markets.items():
                current = mlist[0][0]
                remaining = (current.end_date - datetime.now(timezone.utc)).total_seconds()
                self._startup_window_id = current.condition_id
                console.print(
                    f"[yellow]Waiting for fresh window — skipping "
                    f"{current.question} ({remaining:.0f}s left). "
                    f"Warming up microstructure data...[/yellow]"
                )

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

        When --market filter is set: DON'T re-read from DB (the DB query
        filters out 5-min markets). Instead, just prune expired windows from
        the in-memory list. Only fetch from API when we run out of windows.
        """
        while self.running:
            try:
                if self.market_filter:
                    # Prune expired windows from memory — don't touch DB
                    now = datetime.now(timezone.utc)
                    for symbol in list(self.updown_markets.keys()):
                        live = [
                            (m, p) for m, p in self.updown_markets[symbol]
                            if m.end_date and m.end_date > now
                        ]
                        if live:
                            self.updown_markets[symbol] = live
                        else:
                            del self.updown_markets[symbol]

                    self._total_markets = sum(
                        len(v) for v in self.updown_markets.values()
                    )

                    # If we're running low on windows, fetch more from API
                    # Trigger early (≤3) so we refetch BEFORE running out
                    if self._total_markets <= 3 and not self._sync_lock.locked():
                        try:
                            async with self._sync_lock:
                                fetched = await self._quick_sync()
                            if fetched:
                                await self._refresh_markets(prefetched=fetched)
                        except Exception as e:
                            logger.warning(f"Quick sync failed: {e}")
                else:
                    # Full sync for unfiltered mode
                    try:
                        await self.indexer.sync()
                    except Exception as e:
                        logger.warning(f"Background sync failed: {e}")
                    await self._refresh_markets()

            except Exception as e:
                logger.error(f"Market refresh failed: {e}")
            interval = MARKET_REFRESH_INTERVAL_FILTERED if self.market_filter else MARKET_REFRESH_INTERVAL
            await asyncio.sleep(interval)

    async def _refresh_markets(self, prefetched: list[Market] | None = None):
        """Load up/down crypto markets.

        If `prefetched` is provided, use those directly (from _quick_sync).
        Otherwise fall back to reading from DB via the indexer.

        Only keeps markets that are currently live — end_date in the future.
        For each symbol, picks the NEAREST expiring window (the one that's
        active right now) so we're always on the current 5-min window.
        """
        now = datetime.now(timezone.utc)

        if prefetched is not None:
            all_markets = prefetched
        else:
            try:
                all_markets = await self.indexer.get_markets(
                    min_liquidity=0,
                    limit=5000,
                )
            except Exception as e:
                logger.warning(f"Failed to load markets: {e}")
                return

        # Simple filter: UP_DOWN_PATTERN + user filter + live (not expired)
        candidates: dict[str, list[tuple[Market, ParsedCryptoMarket]]] = {}

        for market in all_markets:
            q = market.question
            if not UP_DOWN_PATTERN.search(q):
                continue

            # Skip markets that have already ended
            if not market.end_date or market.end_date <= now:
                continue

            # Apply user's --market filter with word-boundary matching
            if not self._matches_filter(market):
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

            # Take the nearest window (current) + many future windows for
            # seamless hopping — Polymarket has 40+ upcoming 5-min windows
            selected = market_list[:10]

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

            # Clear startup wait flag — this is a fresh window, start trading
            if self._waiting_for_fresh_window:
                self._waiting_for_fresh_window = False
                console.print(
                    "[bold green]Fresh window ready — microstructure warmed up, "
                    "starting to trade![/bold green]"
                )

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

            if self._total_markets == 0 and not self._sync_lock.locked():
                # DB doesn't have next window yet — quick fetch from API
                console.print("[yellow]Fetching next window from API...[/yellow]")
                try:
                    async with self._sync_lock:
                        fetched = await self._quick_sync()
                    if fetched:
                        await self._refresh_markets(prefetched=fetched)
                except Exception as e:
                    logger.warning(f"Hop sync failed: {e}")

            # Clear startup wait flag after refresh too
            if self._waiting_for_fresh_window and self._total_markets > 0:
                self._waiting_for_fresh_window = False
                console.print(
                    "[bold green]Fresh window ready — microstructure warmed up, "
                    "starting to trade![/bold green]"
                )

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

        # On startup, skip trading until the first fresh window starts.
        # The aggTrade callback still fires (warming up microstructure).
        # We detect the new window by checking if markets[0] is different
        # from the window we started on (refresh loop prunes expired ones).
        if self._waiting_for_fresh_window:
            current_cid = markets[0][0].condition_id if markets else None
            if current_cid and current_cid != self._startup_window_id:
                # The startup window expired and was pruned — we're on a fresh one
                self._waiting_for_fresh_window = False
                self._trades_this_window = 0
                next_mkt = markets[0][0]
                remaining = (next_mkt.end_date - now).total_seconds()
                console.print(
                    "[bold green]Fresh window ready — microstructure warmed up, "
                    "starting to trade![/bold green]"
                )
                console.print(
                    f"\n[bold green]{'='*60}[/bold green]"
                    f"\n[bold green]WINDOW HOP → {next_mkt.question}[/bold green]"
                    f"\n[bold green]{remaining:.0f}s remaining | "
                    f"YES={next_mkt.yes_price:.2f} NO={next_mkt.no_price:.2f}[/bold green]"
                    f"\n[bold green]{'='*60}[/bold green]\n"
                )
            else:
                return

        # Only evaluate the CURRENT (first) window — the rest are pre-loaded
        # for seamless hopping, NOT for simultaneous trading.
        market, parsed = markets[0]
        if not market.end_date:
            return

        remaining = (market.end_date - now).total_seconds()
        if remaining <= 0:
            return

        # Check trade cooldown for this market
        last_trade_time = self._last_trade_log.get(market.condition_id, 0)
        if time.time() - last_trade_time < self._trade_cooldown:
            return

        # Check max trades per window
        if self._trades_this_window >= self.config.max_trades_per_window:
            return

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

        # Fixed dollar sizing (simpler, more predictable for micro trades)
        if self.config.fixed_position_usd > 0:
            size_usd = min(self.config.fixed_position_usd, bankroll * 0.10)
        else:
            # Fall back to Kelly sizing
            max_pct = min(
                self.config.max_position_per_trade,
                self.settings.risk.max_position_pct,
            )
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

        # Use FOK with slippage so the order fills instantly or cancels.
        # GTC limit orders can sit unfilled and the bot thinks it has a position.
        # place_fok_order takes USD amount directly — SDK handles rounding to avoid
        # the "invalid amounts" error where maker_amount has >2 decimal places.
        price = opp.market_price
        entry_price = min(round(price + 0.03, 2), 0.99)  # Pay up to 3c more for instant fill
        size = round(size_usd / entry_price, 2) if entry_price > 0 else 0

        signal = self.strategy.opportunity_to_signal(opp)

        try:
            # Place FOK order directly — bypasses engine's place_limit_order
            # to use create_market_order which handles USD rounding correctly.
            result = self.client.place_fok_order(
                token_id=token_id,
                side="BUY",
                amount=size_usd,
                price=entry_price,
            )

            poly_order_id = result.get("orderID", result.get("id", ""))
            if poly_order_id:
                # Update allowance so we can SELL these tokens later.
                # Without this, the exchange contract can't move our conditional tokens.
                try:
                    self.client.update_token_allowance(token_id)
                except Exception as e:
                    logger.warning(f"Token allowance update failed (non-fatal): {e}")

                # Record in DB
                try:
                    order_id = await self.db.insert_order({
                        "market_id": cid,
                        "token_id": token_id,
                        "side": "BUY",
                        "order_type": "FOK",
                        "price": entry_price,
                        "size": size,
                        "amount_usd": size_usd,
                        "status": "FILLED",
                        "strategy": "micro_sniper",
                    })
                    await self.db.insert_trade({
                        "trade_id": order_id,
                        "market_id": cid,
                        "token_id": token_id,
                        "question": opp.market.question,
                        "side": opp.side.value,
                        "entry_price": entry_price,
                        "size": size,
                        "status": "OPEN",
                        "strategy": "micro_sniper",
                        "reasoning": signal.reasoning,
                    })
                    await self.db.upsert_position({
                        "market_id": cid,
                        "token_id": token_id,
                        "question": opp.market.question,
                        "side": opp.side.value,
                        "size": size,
                        "entry_price": entry_price,
                        "current_price": entry_price,
                        "strategy": "micro_sniper",
                    })
                except Exception as e:
                    logger.warning(f"DB recording failed (non-fatal): {e}")

                self._positions[cid] = "yes" if opp.side == Side.YES else "no"
                self._trades_this_window += 1
                self._total_trades += 1
                self._last_trade_log[cid] = time.time()
                console.print(
                    f"[green]Order placed: {opp.side.value} {size:.1f} @ ${entry_price:.3f} "
                    f"(${size_usd:.2f}) — ID: {poly_order_id}[/green]"
                )
                console.print(f"[green]  MICRO SNIPED! Order: {poly_order_id}[/green]")
            else:
                console.print("[red]  Trade failed — no order ID returned[/red]")

        except Exception as e:
            err_msg = str(e)
            if "fully filled" in err_msg or "FOK" in err_msg:
                # FOK rejected — not enough liquidity at our price. Normal, just skip.
                if not self.quiet:
                    console.print("[dim]  FOK rejected — no liquidity, skipping[/dim]")
            else:
                console.print(f"[red]  Execution error: {e}[/red]")
                logger.error(f"Micro execution failed: {e}", exc_info=True)

    async def _close_position(self, engine, opp: MicroOpportunity) -> bool:
        """Close an existing position by placing a FOK SELL order.

        On Polymarket CLOB, selling tokens you hold = SELL side on the token_id.
        Uses FOK (Fill or Kill) with aggressive pricing for instant fills.
        Records exit price and P&L in the trades table.
        """
        cid = opp.market.condition_id
        current_pos = self._positions.get(cid)
        if not current_pos:
            return True  # Nothing to close

        # Determine which token to sell and at what price
        if current_pos == "yes":
            token_id = opp.market.yes_token_id
            price = opp.market.yes_price
        else:
            token_id = opp.market.no_token_id
            price = opp.market.no_price

        if not token_id:
            console.print("[red]  No token ID — can't close position[/red]")
            return False

        # Aggressive FOK sell price: 5 cents below market for instant fill.
        # FOK will fill at best available bid, not necessarily our limit price.
        # The limit just sets the floor — "fill at best bid, but not below this".
        sell_price = max(0.01, round(price - 0.05, 2))

        # Get entry price from DB for P&L calculation
        entry_price = 0.0
        db_size = 0
        try:
            positions = await self.db.get_open_positions()
            for p in positions:
                if p.get("market_id") == cid and p.get("side", "").lower() == current_pos:
                    db_size = p.get("size", 0)
                    entry_price = p.get("entry_price", 0)
                    break
        except Exception:
            pass

        # Get ACTUAL token balance from CLOB API — this is the truth.
        # The DB size can be wrong because FOK buy fills may produce a
        # different share count than we estimated from USD/price.
        size = 0
        try:
            bal = self.client.get_token_balance(token_id)
            logger.info(f"Token balance response for {token_id}: {bal}")
            # Balance may be a raw string number or dict with 'balance' key
            if isinstance(bal, dict):
                raw_bal = bal.get("balance", 0)
            else:
                raw_bal = bal
            # Convert from 6-decimal USDC-style wei to human-readable
            raw_int = int(float(str(raw_bal)))
            if raw_int > 1_000_000:
                # Looks like wei (e.g. 15600000 = 15.6 shares)
                size = round(raw_int / 1e6, 2)
            else:
                # Already in human-readable units
                size = round(float(str(raw_bal)), 2)
        except Exception as e:
            logger.warning(f"Token balance lookup failed, falling back to DB: {e}")
            size = round(db_size, 2)

        if not self.quiet:
            console.print(f"  [dim]Token balance: {size} shares (DB: {db_size})[/dim]")

        if size <= 0:
            # No tokens to sell — just clear our tracking
            logger.info(f"No token balance for {token_id}, clearing tracking")
            return True

        # Ensure exchange has allowance to move our conditional tokens.
        # This is required before any SELL — the exchange contract needs
        # approval to transfer tokens from our wallet.
        try:
            self.client.update_token_allowance(token_id)
        except Exception as e:
            logger.warning(f"Token allowance update before sell failed: {e}")

        try:
            # For SELL FOK, amount = shares to sell (not USD)
            result = self.client.place_fok_order(
                token_id=token_id,
                side="SELL",
                amount=size,
                price=sell_price,
            )

            order_id = result.get("orderID", result.get("id", ""))

            # FOK fills instantly or cancels — no need to poll.
            # Trust the API response: if post_order didn't throw, assume filled.
            fill_price = sell_price

            if not self.quiet:
                console.print(
                    f"[yellow]  SOLD {current_pos.upper()} {size:.1f} @ ${sell_price:.2f} "
                    f"— Order: {order_id}[/yellow]"
                )

            # Record exit price + P&L in trades table
            gross_pnl = (fill_price - entry_price) * size if entry_price > 0 else 0
            try:
                await self.db.close_trade_by_market(
                    market_id=cid,
                    exit_price=fill_price,
                    pnl=gross_pnl,
                )
            except Exception as e:
                logger.warning(f"Trade close recording failed (non-fatal): {e}")

            # Remove DB position
            try:
                await self.db.remove_position(cid, token_id, current_pos)
            except Exception as e:
                logger.warning(f"DB position update failed (non-fatal): {e}")

            if not self.quiet and entry_price > 0:
                pnl_style = "green" if gross_pnl >= 0 else "red"
                console.print(
                    f"  [{pnl_style}]P&L: ${gross_pnl:+.2f} "
                    f"(entry ${entry_price:.2f} → exit ${fill_price:.2f})"
                    f"  ✓ filled[/{pnl_style}]"
                )

            return True

        except Exception as e:
            err_msg = str(e)
            if "fully filled" in err_msg or "FOK" in err_msg:
                console.print(f"[yellow]  FOK sell rejected — no liquidity at ${sell_price:.2f}[/yellow]")
            else:
                console.print(f"[red]  Sell failed: {e}[/red]")
                logger.error(f"Failed to close {current_pos} position on {cid}: {e}")
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
            warmup_str = " | [yellow]WARMING UP — waiting for fresh window[/yellow]" if self._waiting_for_fresh_window else ""

            console.print(
                f"\n[bold dim]── Micro Status ── "
                f"{' | '.join(micro_lines) if micro_lines else 'waiting for data'} | "
                f"Mkts: {self._total_markets} | "
                f"{pos_str} | "
                f"Trades: {self._total_trades} (flips: {self._total_flips}) | "
                f"Evals: {self._eval_count} | "
                f"Prices: {poly_str}[/bold dim]"
                f"{warmup_str}"
            )

    async def _get_bankroll(self) -> float:
        """Get current bankroll."""
        try:
            positions = await self.db.get_open_positions()
            exposure = sum(p.get("size", 0) * p.get("entry_price", 0) for p in positions)
            return max(0, 200.0 - exposure)
        except Exception:
            return 200.0
