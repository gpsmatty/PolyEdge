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
        skip_warmup: bool = False,
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
        self._filter_words = self.market_filter.split() if self.market_filter else []

        # Expand crypto ticker aliases so "btc" also matches "bitcoin" in
        # question text (Gamma API questions use full names like "Bitcoin").
        _ALIASES = {
            "btc": "bitcoin", "bitcoin": "btc",
            "eth": "ethereum", "ethereum": "eth",
            "sol": "solana", "solana": "sol",
            "xrp": "ripple", "ripple": "xrp",
            "doge": "dogecoin", "dogecoin": "doge",
        }

        # Duration filter: "5m" → 5 minutes, "15m" → 15 minutes, "1h" → 60 min.
        # Parsed from the time range in the question (e.g. "3:20PM-3:25PM" = 5 min).
        import re
        _DURATION_RE = re.compile(r'^(\d+)(m|h)$')
        self._duration_filter_minutes: int | None = None

        # Pre-compile regex patterns for each filter word.
        self._filter_patterns = []
        for w in self._filter_words:
            # Check if this is a duration filter like "5m" or "1h"
            dur_match = _DURATION_RE.match(w)
            if dur_match:
                val, unit = int(dur_match.group(1)), dur_match.group(2)
                self._duration_filter_minutes = val * 60 if unit == 'h' else val
                continue  # Don't add as a text pattern

            # Build pattern that matches the word OR its alias
            alias = _ALIASES.get(w)
            if alias:
                pat = r'(?:^|[\s\-–,])(?:' + re.escape(w) + '|' + re.escape(alias) + r')(?:[\s\-–,]|$)'
            else:
                pat = r'(?:^|[\s\-–,])' + re.escape(w) + r'(?:[\s\-–,]|$)'
            self._filter_patterns.append(re.compile(pat, re.IGNORECASE))

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

        # Apply configurable momentum weights to each MicroStructure
        for micro in self.agg_feed.micro.values():
            micro.weight_ofi_5s = self.config.weight_ofi_5s
            micro.weight_ofi_15s = self.config.weight_ofi_15s
            micro.weight_vwap_drift = self.config.weight_vwap_drift
            micro.weight_intensity = self.config.weight_intensity

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
        # --no-warmup skips this and trades immediately.
        self._waiting_for_fresh_window: bool = not skip_warmup
        self._startup_window_id: str | None = None  # condition_id of the window we're skipping

        # Window hop cooldown — seconds since last hop before allowing entries.
        # Lets stale cross-window momentum flush out of the 15s/30s OFI windows.
        self._last_hop_time: float = 0.0  # time.time() of most recent hop

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

        console.print("[dim]Targeted fetch (newest markets first)...[/dim]")

        # Sort by endDate DESCENDING — newest/soonest-expiring markets first.
        # The ascending sort returned ancient 2025 markets and never reached
        # the March 2026 crypto markets within the page limit.
        for page in range(20):  # Up to 2000 markets, newest first
            url = f"{gamma_url}/markets"
            params = {
                "limit": batch_size,
                "offset": page * batch_size,
                "active": "true",
                "closed": "false",
                "order": "endDate",
                "ascending": "false",
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

        Text patterns use word-boundary matching. Duration filters like "5m"
        are matched by parsing the time range from the question text
        (e.g. "3:20PM-3:25PM" = 5 minutes).
        """
        if not self._filter_patterns and self._duration_filter_minutes is None:
            return True

        haystack = f"{market.question} {market.slug or ''}"
        if not all(p.search(haystack) for p in self._filter_patterns):
            return False

        # Duration filter — parse time range from question like "3:20PM-3:25PM"
        if self._duration_filter_minutes is not None:
            import re
            time_range = re.search(
                r'(\d{1,2}):(\d{2})\s*(AM|PM)\s*[-–]\s*(\d{1,2}):(\d{2})\s*(AM|PM)',
                market.question, re.IGNORECASE,
            )
            if not time_range:
                return False
            h1, m1, ap1 = int(time_range.group(1)), int(time_range.group(2)), time_range.group(3).upper()
            h2, m2, ap2 = int(time_range.group(4)), int(time_range.group(5)), time_range.group(6).upper()
            # Convert to minutes since midnight
            mins1 = (h1 % 12 + (12 if ap1 == 'PM' else 0)) * 60 + m1
            mins2 = (h2 % 12 + (12 if ap2 == 'PM' else 0)) * 60 + m2
            if mins2 <= mins1:
                mins2 += 24 * 60  # Crosses midnight
            duration = mins2 - mins1
            if duration != self._duration_filter_minutes:
                return False

        return True

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

        # Ensure the exchange contracts have on-chain approval to move
        # our conditional tokens. Without this, SELL orders fail with
        # "not enough balance / allowance". Only sends a tx if not yet approved.
        if not self.dry_run:
            console.print("[dim]Checking exchange approvals...[/dim]")
            try:
                self.client.ensure_exchange_approved()
                console.print("[green]Exchange approvals: OK[/green]")
            except Exception as e:
                console.print(f"[yellow]Exchange approval check failed: {e}[/yellow]")

        # Initial market load — try DB first (fast), only full sync if empty.
        # DB has markets from prior syncs. The key fix: limit=50000 so the
        # volume-sorted DB query doesn't cut off low-volume 5-min crypto markets.
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

                    # If we're running low on windows, do a full sync to refill
                    if self._total_markets <= 3 and not self._sync_lock.locked():
                        try:
                            async with self._sync_lock:
                                await self.indexer.sync(force=True)
                            await self._refresh_markets()
                        except Exception as e:
                            logger.warning(f"Sync failed: {e}")
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
                    limit=50000,  # Need ALL — 5-min crypto markets have very low volume
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
            if not market.end_date or market.end_date <= now:
                continue
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

            # Record hop time for cooldown enforcement
            self._last_hop_time = time.time()

            # Reset flow windows so stale cross-window momentum doesn't
            # trigger entries in the new window. Fresh data builds in ~5-15s.
            binance_symbol = symbol if symbol in self.agg_feed.micro else None
            if not binance_symbol:
                # Try common mappings
                for sym in self.agg_feed.micro:
                    if sym.startswith(symbol.replace("usdt", "")):
                        binance_symbol = sym
                        break
            if binance_symbol and binance_symbol in self.agg_feed.micro:
                micro = self.agg_feed.micro[binance_symbol]
                micro.flow_5s.reset()
                micro.flow_15s.reset()
                micro.flow_30s.reset()
                logger.info(f"Reset flow windows on hop for {binance_symbol}")

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
            self._last_hop_time = time.time()

            # Quick DB read first
            await self._refresh_markets()

            if self._total_markets == 0 and not self._sync_lock.locked():
                # DB doesn't have next window yet — full sync from API
                console.print("[yellow]Fetching next window from API...[/yellow]")
                try:
                    async with self._sync_lock:
                        await self.indexer.sync(force=True)
                    await self._refresh_markets()
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

        current_pos = self._positions.get(market.condition_id)

        # Check max trades per window — only block NEW entries, never exits.
        # A position must always be able to exit regardless of trade count.
        if not current_pos and self._trades_this_window >= self.config.max_trades_per_window:
            return

        # Window hop cooldown — don't enter new positions while stale
        # cross-window momentum is still in the flow windows. Exits are
        # still allowed so we're never trapped in a position.
        hop_elapsed = time.time() - self._last_hop_time
        if not current_pos and self._last_hop_time > 0 and hop_elapsed < self.config.window_hop_cooldown:
            return  # Still in cooldown — skip entry evaluation

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
        # Cap at bankroll (can't spend more than you have), but don't cap at 10% —
        # if the user set a fixed size, respect it.
        if self.config.fixed_position_usd > 0:
            size_usd = min(self.config.fixed_position_usd, bankroll)
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

    async def _get_fill_price(self, order_id: str, token_id: str, fallback: float) -> float:
        """Fetch actual fill price from CLOB API after a FOK order fills.

        FOK orders can fill at a BETTER price than we asked for. Using our
        ask price as the fill price gives wrong P&L.

        Strategy: try get_order first (exact match), then search trades by
        order ID. NEVER fall back to "most recent trade" — that grabs
        previous fills and gives wildly wrong P&L.
        """
        try:
            await asyncio.sleep(0.3)

            # Try 1: get_order returns the order with its average fill price
            try:
                order = self.client.get_order(order_id)
                if order:
                    # The order response may have 'price' (our ask) or 'average_price' (fill)
                    avg = order.get("average_price") or order.get("associate_trades_avg_price")
                    if avg:
                        fill_price = float(avg)
                        if not self.quiet:
                            console.print(f"  [dim]Actual fill price: ${fill_price:.3f} (asked ${fallback:.3f})[/dim]")
                        return fill_price
            except Exception:
                pass

            # Try 2: search trades for exact order ID match
            trades = self.client.get_trades(asset_id=token_id)
            if trades:
                for t in trades:
                    if t.get("taker_order_id") == order_id or t.get("order_id") == order_id:
                        fill_price = float(t.get("price", fallback))
                        if not self.quiet:
                            console.print(f"  [dim]Actual fill price: ${fill_price:.3f} (asked ${fallback:.3f})[/dim]")
                        return fill_price

            # No match found — use our ask price. Better to be slightly wrong
            # than grab a completely wrong trade.
            if not self.quiet:
                console.print(f"  [dim]Fill price: ${fallback:.3f} (no CLOB match, using ask)[/dim]")
        except Exception as e:
            logger.warning(f"Could not fetch fill price: {e}")
        return fallback

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
        slippage = self.config.entry_slippage
        entry_price = min(round(price + slippage, 2), 0.99)  # Pay up to Nc more for instant fill
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
                # Get actual fill price from CLOB API (FOK can fill better than asked)
                actual_entry = await self._get_fill_price(poly_order_id, token_id, entry_price)
                actual_size = round(size_usd / actual_entry, 2) if actual_entry > 0 else size

                # Record in DB with actual fill price
                try:
                    order_id = await self.db.insert_order({
                        "market_id": cid,
                        "token_id": token_id,
                        "side": "BUY",
                        "order_type": "FOK",
                        "price": actual_entry,
                        "size": actual_size,
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
                        "entry_price": actual_entry,
                        "size": actual_size,
                        "status": "OPEN",
                        "strategy": "micro_sniper",
                        "reasoning": signal.reasoning,
                    })
                    await self.db.upsert_position({
                        "market_id": cid,
                        "token_id": token_id,
                        "question": opp.market.question,
                        "side": opp.side.value,
                        "size": actual_size,
                        "entry_price": actual_entry,
                        "current_price": actual_entry,
                        "strategy": "micro_sniper",
                    })
                except Exception as e:
                    logger.warning(f"DB recording failed (non-fatal): {e}")

                self._positions[cid] = "yes" if opp.side == Side.YES else "no"
                self._trades_this_window += 1
                self._total_trades += 1
                self._last_trade_log[cid] = time.time()
                console.print(
                    f"[green]Order placed: {opp.side.value} {actual_size:.1f} @ ${actual_entry:.3f} "
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
                    console.print(f"[dim]  FOK rejected @ ${entry_price:.2f} (mkt ${price:.2f}) — no liquidity[/dim]")
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

        # Sell price floor: Nc below market (configurable). FOK fills at best
        # available bid but won't go below this floor. If FOK rejects, we retry
        # on next eval tick when the book refreshes.
        exit_slip = self.config.exit_slippage
        sell_price = max(0.01, round(price - exit_slip, 2))

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
            if not self.quiet:
                console.print(f"  [dim]CLOB response: {bal}[/dim]")
            # Balance may be a raw string number or dict with 'balance' key
            if isinstance(bal, dict):
                raw_bal = bal.get("balance", 0)
            else:
                raw_bal = bal
            # Convert from 6-decimal USDC-style wei to human-readable.
            # CRITICAL: Always truncate DOWN, never round().  round() can
            # round UP (e.g. 5.926176 → 5.93) making us try to sell more
            # tokens than we actually own → "not enough balance" error.
            raw_int = int(float(str(raw_bal)))
            if raw_int > 1_000_000:
                # Looks like wei (e.g. 15600000 = 15.6 shares)
                size = int(raw_int / 1e6 * 100) / 100  # truncate to 2 dp
            else:
                # Already in human-readable units
                size = int(float(str(raw_bal)) * 100) / 100
        except Exception as e:
            logger.warning(f"Token balance lookup failed, falling back to DB: {e}")
            size = int(float(db_size) * 100) / 100

        if not self.quiet:
            console.print(f"  [dim]Token balance: {size} shares (DB: {db_size})[/dim]")

        if size <= 0:
            # No tokens to sell — just clear our tracking
            logger.info(f"No token balance for {token_id}, clearing tracking")
            return True

        # Refresh the CLOB backend's cached balance/allowance state.
        # The actual on-chain approval is done once at startup via
        # ensure_exchange_approved(). This just syncs the cache.
        try:
            refresh = self.client.update_token_allowance(token_id)
            if not self.quiet:
                console.print(f"  [dim]Allowance refresh: {refresh}[/dim]")
        except Exception as e:
            if not self.quiet:
                console.print(f"  [dim]Allowance refresh failed: {e}[/dim]")

        # Check order book bid depth from WebSocket to see what's available.
        # Bids = people willing to buy our tokens.
        book = self.poly_feed.books.get(token_id)
        best_bid = sell_price  # fallback to our floor
        available_at_floor = 0.0
        if book and book.bids:
            best_bid = book.bids[0].price  # highest bid in the book
            for bid in book.bids:
                if bid.price >= sell_price:
                    available_at_floor += bid.size
            if not self.quiet:
                top_bids = book.bids[:3]
                bid_str = " | ".join(f"${b.price:.2f}×{b.size:.0f}" for b in top_bids)
                console.print(f"  [dim]Book bids: {bid_str} — {available_at_floor:.0f} shares above ${sell_price:.2f}[/dim]")

        # Use best bid minus 1c as sell price if it's better than our floor.
        # This way we sell near market instead of panic-selling at floor.
        effective_sell = max(sell_price, round(best_bid, 2))

        # Single FOK attempt — don't halve and retry at worse prices.
        # If rejected, we'll retry on next eval tick with a fresh book.
        remaining = size
        total_sold = 0.0
        total_proceeds = 0.0

        try:
            chunk = int(remaining * 100) / 100  # truncate, don't round
            result = self.client.place_fok_order(
                token_id=token_id,
                side="SELL",
                amount=chunk,
                price=effective_sell,
            )
            order_id = result.get("orderID", result.get("id", ""))
            total_sold = chunk
            remaining = 0

            # Get actual fill price from CLOB API
            actual_sell = await self._get_fill_price(order_id, token_id, effective_sell)
            total_proceeds = chunk * actual_sell

            if not self.quiet:
                console.print(
                    f"[yellow]  SOLD {current_pos.upper()} {chunk:.1f} @ ${actual_sell:.2f} "
                    f"— Order: {order_id}[/yellow]"
                )

        except Exception as e:
            err_msg = str(e)
            if "fully filled" in err_msg or "FOK" in err_msg:
                if not self.quiet:
                    console.print(f"[yellow]  FOK sell rejected @ ${effective_sell:.2f} — will retry next tick[/yellow]")
            elif "not enough balance" in err_msg:
                console.print(f"[red]  Sell failed: not enough balance/allowance[/red]")
            else:
                console.print(f"[red]  Sell failed: {e}[/red]")
                logger.error(f"Failed to close {current_pos} position on {cid}: {e}")

        if total_sold > 0:
            fill_price = total_proceeds / total_sold if total_sold > 0 else sell_price

            # Record exit price + P&L in trades table
            gross_pnl = (fill_price - entry_price) * total_sold if entry_price > 0 else 0
            try:
                await self.db.close_trade_by_market(
                    market_id=cid,
                    exit_price=fill_price,
                    pnl=gross_pnl,
                )
            except Exception as e:
                logger.warning(f"Trade close recording failed (non-fatal): {e}")

            # Remove DB position (or update size if partial fill)
            if remaining <= 0.5:
                try:
                    await self.db.remove_position(cid, token_id, current_pos)
                except Exception as e:
                    logger.warning(f"DB position update failed (non-fatal): {e}")
            else:
                # Partial fill — update remaining size in DB
                logger.warning(f"Partial sell: {total_sold:.1f}/{size:.1f} filled, {remaining:.1f} remaining")

            if not self.quiet and entry_price > 0:
                pnl_style = "green" if gross_pnl >= 0 else "red"
                sold_label = f"{total_sold:.1f}/{size:.1f}" if remaining > 0.5 else f"{total_sold:.1f}"
                console.print(
                    f"  [{pnl_style}]P&L: ${gross_pnl:+.2f} "
                    f"(entry ${entry_price:.2f} → exit ${fill_price:.2f}, {sold_label} shares)"
                    f"  ✓ filled[/{pnl_style}]"
                )

            # If fully sold, clear position tracking
            if remaining <= 0.5:
                return True
            else:
                # Partial — will retry on next eval tick
                return False
        else:
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
        """Get current bankroll from actual USDC balance on chain."""
        try:
            bal = self.client.get_collateral_balance()
            # Returns dict like {'balance': '130123456', 'allowances': {...}}
            # Balance is in raw units (USDC has 6 decimals)
            raw = int(float(str(bal.get("balance", 0))))
            usdc = raw / 1e6
            if not self.quiet:
                logger.debug(f"Bankroll: ${usdc:.2f} (raw: {raw})")
            return usdc if usdc > 0 else 50.0
        except Exception as e:
            logger.warning(f"Balance check failed: {e}")
            return 50.0
