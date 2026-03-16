"""Market Maker Runner — persistent async loop for market making on
Polymarket crypto up/down markets.

Connects to:
  - Binance @depth20@100ms WebSocket for adverse selection defense
  - Polymarket WebSocket for real-time YES/NO prices
  - CLOB API for posting/canceling post-only limit orders

Main loop:
1. Heartbeat task — sends heartbeat every 5s (dead-man switch)
2. Quote task — computes and posts bid/ask quotes every few seconds
3. Fill monitor — polls for fills, updates inventory
4. Depth callback — pulls or widens quotes on momentum spikes
5. Window management — hops to next window, pauses, resets state

Usage:
    polyedge maker              # Copilot mode (confirm trades)
    polyedge maker --auto       # Autopilot mode (auto-execute)
    polyedge maker --dry        # Dry run — watch and analyze only
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from polyedge.core.config import Settings, MarketMakerConfig
from polyedge.core.client import PolyClient
from polyedge.core.db import Database
from polyedge.core.models import Market
from polyedge.data.binance_depth import BinanceDepthFeed, DepthStructure
from polyedge.data.ws_feed import MarketFeed, EVENT_BEST_BID_ASK, EVENT_LAST_TRADE
from polyedge.data.indexer import MarketIndexer
from polyedge.strategies.market_maker import (
    MarketMakerStrategy,
    QuoteSet,
)
from polyedge.strategies.crypto_sniper import (
    UP_DOWN_PATTERN,
    CRYPTO_SYMBOL_MAP,
    EXCLUDED_PATTERNS,
)
from polyedge.core.console import console

logger = logging.getLogger("polyedge.maker_runner")

MARKET_REFRESH_INTERVAL = 120  # seconds
STATUS_INTERVAL = 15  # seconds


class MakerRunner:
    """Persistent async loop for market making on Polymarket.

    Posts post-only limit orders on both sides of crypto up/down markets.
    All orders are maker-only (zero fees + rebates).
    """

    def __init__(
        self,
        settings: Settings,
        client: PolyClient,
        db: Database,
        auto: bool = False,
        dry: bool = False,
        market_filter: str | None = None,
        verbose: bool = False,
        quiet: bool = False,
    ):
        self.settings = settings
        self.client = client
        self.db = db
        self.auto = auto
        self.dry = dry
        self.market_filter = market_filter.lower() if market_filter else None
        self.verbose = verbose
        self.quiet = quiet

        # Parse filter: "btc 15m" → text pattern "btc"/"bitcoin" + duration 15 min
        import re
        _filter_words = self.market_filter.split() if self.market_filter else []
        _ALIASES = {
            "btc": "bitcoin", "bitcoin": "btc",
            "eth": "ethereum", "ethereum": "eth",
            "sol": "solana", "solana": "sol",
            "xrp": "ripple", "ripple": "xrp",
            "doge": "dogecoin", "dogecoin": "doge",
        }
        _DURATION_RE = re.compile(r'^(\d+)(m|h)$')
        self._duration_filter_minutes: int | None = None
        self._filter_patterns = []
        for w in _filter_words:
            dur_match = _DURATION_RE.match(w)
            if dur_match:
                val, unit = int(dur_match.group(1)), dur_match.group(2)
                self._duration_filter_minutes = val * 60 if unit == 'h' else val
                continue
            alias = _ALIASES.get(w)
            if alias:
                pat = r'(?:^|[\s\-–,])(?:' + re.escape(w) + '|' + re.escape(alias) + r')(?:[\s\-–,]|$)'
            else:
                pat = r'(?:^|[\s\-–,])' + re.escape(w) + r'(?:[\s\-–,]|$)'
            self._filter_patterns.append(re.compile(pat, re.IGNORECASE))

        self.config: MarketMakerConfig = settings.strategies.market_maker
        self.strategy = MarketMakerStrategy(self.config)

        # Window management: symbol -> list of Markets sorted by end_date
        # First element = current active window, rest = upcoming for hopping
        self.windows: dict[str, list[Market]] = {}  # e.g. "btcusdt" -> [Market, Market, ...]
        self.active_markets: dict[str, Market] = {}  # condition_id -> Market (flat view)
        self.yes_prices: dict[str, float] = {}  # condition_id -> YES midpoint price
        self.no_prices: dict[str, float] = {}  # condition_id -> NO price
        self.yes_best_bids: dict[str, float] = {}  # condition_id -> actual YES best bid
        self.no_best_bids: dict[str, float] = {}  # condition_id -> actual NO best bid
        self._token_to_market: dict[str, tuple[Market, str]] = {}  # token_id -> (Market, "yes"|"no")

        # Depth feed
        self.depth_feed: Optional[BinanceDepthFeed] = None

        # Polymarket WebSocket
        self.poly_feed: Optional[MarketFeed] = None
        self._subscribed_tokens: set[str] = set()

        # Order tracking
        self.live_order_ids: dict[str, list[str]] = {}  # condition_id -> [order_ids]
        self.heartbeat_id: str | None = None

        # Locks
        self._quote_lock = asyncio.Lock()  # Prevent double cancel+post races

        # State
        self._running = False
        self._last_market_refresh = 0.0
        self._last_status = 0.0
        self._last_hop_time = 0.0
        self._total_fills = 0
        self._total_spread_captured = 0.0
        self._total_quotes_posted = 0
        self._total_pulls = 0

    async def run(self):
        """Main entry point — runs until cancelled."""
        self._running = True
        logger.info("Market Maker starting...")

        # Load markets (also starts Polymarket WS via _start_poly_feed)
        await self._refresh_markets()
        if not self.windows:
            logger.error("No active crypto up/down markets found. Exiting.")
            return

        # Connect Binance depth feed for adverse selection defense
        symbols = self._get_symbols()
        logger.info(f"Tracking symbols: {symbols}")

        if self.config.depth_enabled:
            self.depth_feed = BinanceDepthFeed(symbols=symbols)

        # Launch concurrent tasks
        tasks = []
        if self.depth_feed:
            tasks.append(asyncio.create_task(self.depth_feed.start()))
        tasks.append(asyncio.create_task(self._poly_feed_loop()))
        tasks.append(asyncio.create_task(self._quote_loop()))
        tasks.append(asyncio.create_task(self._status_loop()))

        if self.config.heartbeat_enabled and not self.dry:
            tasks.append(asyncio.create_task(self._heartbeat_loop()))

        if not self.dry:
            tasks.append(asyncio.create_task(self._fill_monitor_loop()))

        tasks.append(asyncio.create_task(self._config_refresh_loop()))

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Market Maker shutting down...")
        finally:
            await self._shutdown()

    async def _shutdown(self):
        """Clean shutdown — cancel all orders."""
        self._running = False
        if not self.dry:
            try:
                logger.info("Cancelling all open orders...")
                self.client.cancel_all_orders()
            except Exception as e:
                logger.error(f"Failed to cancel orders on shutdown: {e}")

    # --- Market Management ---

    def _matches_filter(self, market: Market) -> bool:
        """Check if a market matches the --market filter (text + duration)."""
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
            mins1 = (h1 % 12 + (12 if ap1 == 'PM' else 0)) * 60 + m1
            mins2 = (h2 % 12 + (12 if ap2 == 'PM' else 0)) * 60 + m2
            if mins2 <= mins1:
                mins2 += 24 * 60
            duration = mins2 - mins1
            if duration != self._duration_filter_minutes:
                return False

        return True

    async def _quick_sync(self) -> list[Market]:
        """Fetch live crypto markets directly from Gamma API (sorted by endDate ASC).

        Same as micro_runner's _quick_sync — bypasses the DB indexer which
        misses low-volume short-duration crypto windows.
        """
        import aiohttp
        from polyedge.data.markets import _parse_market

        gamma_url = self.settings.polymarket.gamma_url
        found: list[Market] = []
        now = datetime.now(timezone.utc)

        if not self.quiet:
            console.print("[dim]Fetching live markets from API...[/dim]")

        for page in range(20):
            url = f"{gamma_url}/markets"
            params = {
                "limit": 100,
                "offset": page * 100,
                "active": "true",
                "closed": "false",
                "order": "endDate",
                "ascending": "true",
                "end_date_min": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, params=params) as resp:
                        if resp.status != 200:
                            break
                        data = await resp.json()
            except Exception as e:
                logger.warning(f"Quick sync page {page} failed: {e}")
                break

            if not data:
                break

            for item in data:
                try:
                    m = _parse_market(item)
                    found.append(m)
                except (KeyError, ValueError):
                    continue

            # Stop early if we have enough crypto matches
            crypto_count = sum(1 for m in found if UP_DOWN_PATTERN.search(m.question))
            if crypto_count >= 30:
                break

        if not self.quiet:
            console.print(f"[dim]Fetched {len(found)} markets ({sum(1 for m in found if UP_DOWN_PATTERN.search(m.question))} crypto up/down)[/dim]")
        return found

    async def _refresh_markets(self, prefetched: list[Market] | None = None):
        """Refresh active crypto up/down markets from API.

        Mirrors micro_runner's window management:
        - Groups markets by Binance symbol
        - Sorts by end_date ascending (current window first)
        - Takes first 10 windows per symbol for seamless hopping
        - Subscribes to Polymarket WebSocket for price updates
        """
        try:
            all_markets = prefetched or await self._quick_sync()

            now = datetime.now(timezone.utc)
            candidates: dict[str, list[Market]] = {}  # symbol -> [Market, ...]

            for m in all_markets:
                if not UP_DOWN_PATTERN.search(m.question):
                    continue
                if EXCLUDED_PATTERNS.search(m.question):
                    continue
                if not m.end_date or m.end_date <= now:
                    continue
                if not self._matches_filter(m):
                    continue

                # Extract Binance symbol
                symbol = None
                q_lower = m.question.lower()
                for keyword in sorted(CRYPTO_SYMBOL_MAP.keys(), key=len, reverse=True):
                    if keyword in q_lower:
                        symbol = CRYPTO_SYMBOL_MAP[keyword]
                        break
                if not symbol or symbol not in self.config.symbols:
                    continue

                if symbol not in candidates:
                    candidates[symbol] = []
                candidates[symbol].append(m)

            # Sort by end_date, take first 10 per symbol (current + 9 upcoming)
            self.windows.clear()
            self.active_markets.clear()
            self._token_to_market.clear()
            new_token_ids: list[str] = []

            for symbol, market_list in candidates.items():
                market_list.sort(key=lambda m: m.end_date)
                selected = market_list[:10]
                self.windows[symbol] = selected

                for m in selected:
                    self.active_markets[m.condition_id] = m
                    if m.clob_token_ids and len(m.clob_token_ids) >= 2:
                        self._token_to_market[m.clob_token_ids[0]] = (m, "yes")
                        self._token_to_market[m.clob_token_ids[1]] = (m, "no")
                        new_token_ids.extend(m.clob_token_ids[:2])

                # Log current window
                if selected and not self.quiet:
                    current = selected[0]
                    remaining = (current.end_date - now).total_seconds()
                    console.print(
                        f"[cyan]{symbol.replace('usdt','').upper()}: "
                        f"[bold]{current.question}[/bold] "
                        f"({remaining:.0f}s left, {len(selected)} windows loaded)[/cyan]"
                    )

            self._last_market_refresh = time.monotonic()

            # Update Polymarket WS subscription
            new_token_set = set(new_token_ids)
            if new_token_set != self._subscribed_tokens and new_token_ids:
                await self._start_poly_feed(new_token_ids)

            if not self.quiet:
                total = sum(len(w) for w in self.windows.values())
                logger.info(f"Active: {len(self.windows)} symbols, {total} windows")

        except Exception as e:
            logger.error(f"Market refresh failed: {e}")

    def _get_symbols(self) -> list[str]:
        """Get unique Binance symbols from loaded windows."""
        return list(self.windows.keys()) or ["btcusdt"]

    def _get_current_markets(self) -> dict[str, Market]:
        """Get only the CURRENT (first) window per symbol — the one we're quoting."""
        result = {}
        now = datetime.now(timezone.utc)
        for symbol, market_list in self.windows.items():
            if market_list:
                current = market_list[0]
                if current.end_date and current.end_date > now:
                    result[current.condition_id] = current
        return result

    def _check_window_hops(self):
        """Check if any current windows have expired and hop to the next one."""
        now = datetime.now(timezone.utc)
        hopped = False
        for symbol in list(self.windows.keys()):
            market_list = self.windows[symbol]
            if not market_list:
                continue
            current = market_list[0]
            if current.end_date and current.end_date <= now:
                # Current window expired — cancel its orders and promote next
                old_cid = current.condition_id
                if not self.dry:
                    try:
                        # Cancel all orders for both YES and NO tokens
                        if current.clob_token_ids and len(current.clob_token_ids) >= 2:
                            self.client.cancel_market_orders(
                                asset_id=current.clob_token_ids[0]
                            )
                            self.client.cancel_market_orders(
                                asset_id=current.clob_token_ids[1]
                            )
                    except Exception as e:
                        logger.warning(f"Cancel on hop failed: {e}")
                self.live_order_ids.pop(old_cid, None)

                # Log stranded inventory warning
                inv = self.strategy.get_inventory(old_cid)
                if inv.yes_tokens > 0 or inv.no_tokens > 0:
                    logger.warning(
                        f"Window expired with inventory! YES={inv.yes_tokens:.1f} "
                        f"NO={inv.no_tokens:.1f} on {current.question[:40]}"
                    )

                # Reset strategy state for old window (also clears inventory)
                self.strategy.reset_window(old_cid)

                # Promote next window
                market_list.pop(0)
                self._last_hop_time = time.monotonic()
                hopped = True

                if market_list:
                    next_m = market_list[0]
                    remaining = (next_m.end_date - now).total_seconds()
                    console.print(
                        f"\n[yellow]⟳ WINDOW HOP[/yellow] {symbol.replace('usdt','').upper()}: "
                        f"{next_m.question[:50]} ({remaining:.0f}s left)"
                    )
                else:
                    logger.warning(f"No more windows for {symbol} — need refresh")

        # Rebuild flat view after hops
        if hopped:
            self.active_markets.clear()
            self._token_to_market.clear()
            for symbol, market_list in self.windows.items():
                for m in market_list:
                    self.active_markets[m.condition_id] = m
                    if m.clob_token_ids and len(m.clob_token_ids) >= 2:
                        self._token_to_market[m.clob_token_ids[0]] = (m, "yes")
                        self._token_to_market[m.clob_token_ids[1]] = (m, "no")

            # Refetch if running low on windows
            remaining_windows = sum(len(w) for w in self.windows.values())
            if remaining_windows <= 3:
                asyncio.create_task(self._refresh_markets())

    # --- Polymarket Feed ---

    async def _start_poly_feed(self, token_ids: list[str]):
        """Start or restart the Polymarket WebSocket with new token IDs."""
        if self.poly_feed:
            try:
                await self.poly_feed.stop()
            except Exception:
                pass

        self.poly_feed = MarketFeed(self.settings)

        # Register price update callbacks
        async def _on_best_bid_ask(event: dict):
            """best_bid_ask events have 'best_bid' and 'best_ask' fields."""
            token_id = event.get("asset_id", "")
            if not token_id:
                return
            entry = self._token_to_market.get(token_id)
            if not entry:
                return
            m, side = entry
            cid = m.condition_id
            # Use midpoint of bid/ask as price
            try:
                bid = float(event.get("best_bid", 0))
                ask = float(event.get("best_ask", 0))
            except (ValueError, TypeError):
                return
            if bid <= 0 and ask <= 0:
                return
            price = (bid + ask) / 2 if bid > 0 and ask > 0 else max(bid, ask)
            if side == "yes":
                self.yes_prices[cid] = price
                # Store actual best bid separately — needed to floor ask price
                if bid > 0:
                    self.yes_best_bids[cid] = bid
            else:
                self.no_prices[cid] = price
                if bid > 0:
                    self.no_best_bids[cid] = bid

        async def _on_last_trade(event: dict):
            """last_trade_price events have 'price' or 'last_trade_price' field."""
            token_id = event.get("asset_id", "")
            price_str = event.get("price") or event.get("last_trade_price") or "0"
            try:
                price = float(price_str)
            except (ValueError, TypeError):
                return
            if not token_id or not price:
                return
            entry = self._token_to_market.get(token_id)
            if not entry:
                return
            m, side = entry
            cid = m.condition_id
            if side == "yes":
                self.yes_prices[cid] = price
            else:
                self.no_prices[cid] = price

        self.poly_feed.on(EVENT_BEST_BID_ASK, _on_best_bid_ask)
        self.poly_feed.on(EVENT_LAST_TRADE, _on_last_trade)
        self._subscribed_tokens = set(token_ids)

        if not self.quiet:
            logger.info(f"Polymarket WS: subscribing to {len(token_ids)} tokens")

    async def _poly_feed_loop(self):
        """Run the Polymarket WebSocket feed."""
        while self._running:
            if not self.poly_feed:
                await asyncio.sleep(1)
                continue

            try:
                token_ids = list(self._subscribed_tokens)
                if token_ids:
                    await self.poly_feed.start(token_ids)
            except Exception as e:
                logger.warning(f"Poly feed error: {e}")
                await asyncio.sleep(2)

    # --- Depth Lookup ---

    def _get_depth_momentum(self) -> float:
        """Get current depth momentum from Binance feed.

        Looks up lazily each call — NOT cached at startup (the feed
        hasn't connected yet at that point, so caching would give None).
        """
        if not self.depth_feed:
            return 0.0
        symbols = self._get_symbols()
        for sym in symbols:
            ds = self.depth_feed.depth.get(sym)
            if ds and ds.is_active:
                return ds.depth_momentum
        return 0.0

    # --- Quote Loop ---

    async def _quote_loop(self):
        """Main quoting loop — compute and post quotes periodically."""
        while self._running:
            try:
                # Check for window expirations and hop
                self._check_window_hops()

                # Refresh markets periodically (or when running low on windows)
                if time.monotonic() - self._last_market_refresh > MARKET_REFRESH_INTERVAL:
                    await self._refresh_markets()

                # Window hop cooldown — don't quote immediately after hop
                hop_elapsed = time.monotonic() - self._last_hop_time
                if self._last_hop_time > 0 and hop_elapsed < self.config.window_hop_pause_seconds:
                    await asyncio.sleep(1)
                    continue

                # Get depth momentum for defense (looked up fresh each iteration)
                depth_momentum = self._get_depth_momentum()

                # Only quote CURRENT windows (first per symbol), not upcoming ones
                current_markets = self._get_current_markets()
                for cid, m in current_markets.items():
                    await self._quote_market(cid, m, depth_momentum)

            except Exception as e:
                logger.error(f"Quote loop error: {e}")

            await asyncio.sleep(self.config.requote_interval_seconds)

    async def _quote_market(
        self,
        condition_id: str,
        m: Market,
        depth_momentum: float,
    ):
        """Compute and post quotes for a single market."""

        # Get prices
        yes_price = self.yes_prices.get(condition_id, 0)
        no_price = self.no_prices.get(condition_id, 0)
        if not yes_price or not no_price:
            return

        # Get time remaining
        now = datetime.now(timezone.utc)
        if m.end_date:
            seconds_remaining = (m.end_date - now).total_seconds()
        else:
            seconds_remaining = 0
        if seconds_remaining <= 0:
            return

        # Get token IDs
        if not m.clob_token_ids or len(m.clob_token_ids) < 2:
            return
        yes_token_id = m.clob_token_ids[0]
        no_token_id = m.clob_token_ids[1]

        # Get actual best bids for ask floor
        yes_best_bid = self.yes_best_bids.get(condition_id, 0)
        no_best_bid = self.no_best_bids.get(condition_id, 0)

        # Compute quotes
        qs = self.strategy.compute_quotes(
            condition_id=condition_id,
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            yes_price=yes_price,
            no_price=no_price,
            seconds_remaining=seconds_remaining,
            depth_momentum=depth_momentum,
            yes_best_bid=yes_best_bid,
            no_best_bid=no_best_bid,
        )

        if qs.reason_pulled:
            # Cancel existing orders if we're pulling (both YES and NO)
            if condition_id in self.live_order_ids and self.live_order_ids[condition_id]:
                if not self.dry:
                    await self._cancel_market_quotes(condition_id, yes_token_id)
                    await self._cancel_market_quotes(condition_id, no_token_id)
            # Always log pulls — need to see defense working
            if qs.reason_pulled not in ("no_requote_needed",):
                self._total_pulls += 1
                if not self.quiet:
                    console.print(
                        f"  [yellow]⛔ PULL[/yellow] {qs.reason_pulled} | "
                        f"depth={depth_momentum:+.2f} | {seconds_remaining:.0f}s left"
                    )
            return

        if not qs.is_active:
            return

        # Post quotes
        self._total_quotes_posted += 1
        yes_token_id = m.clob_token_ids[0]
        no_token_id = m.clob_token_ids[1]
        if self.dry:
            self._log_dry_quotes(m, qs, depth_momentum)
        else:
            await self._post_quotes(condition_id, yes_token_id, no_token_id, qs)

    async def _post_quotes(self, condition_id: str, yes_token_id: str, no_token_id: str, qs: QuoteSet):
        """Cancel old quotes and post new ones for both YES and NO tokens."""
        async with self._quote_lock:  # Prevent race between concurrent cancel+post
            try:
                # Cancel existing orders for BOTH tokens
                if self.config.cancel_before_requote:
                    await self._cancel_market_quotes(condition_id, yes_token_id)
                    await self._cancel_market_quotes(condition_id, no_token_id)
                    # Brief pause for cancel propagation — prevents race where
                    # new orders post before old ones are fully removed
                    await asyncio.sleep(0.1)

                # Build batch
                orders = [q.as_order_dict() for q in qs.all_quotes()]
                if not orders:
                    return

                if len(orders) <= 15:
                    result = self.client.post_orders_batch(orders)
                else:
                    # Shouldn't happen but handle gracefully
                    result = self.client.post_orders_batch(orders[:15])

                # Track order IDs from response
                if isinstance(result, dict) and "orderIDs" in result:
                    self.live_order_ids[condition_id] = result["orderIDs"]
                elif isinstance(result, list):
                    ids = [r.get("orderID", r.get("id", "")) for r in result if isinstance(r, dict)]
                    self.live_order_ids[condition_id] = [i for i in ids if i]

                if not self.quiet:
                    parts = []
                    for q in qs.all_quotes():
                        # Label YES vs NO quotes
                        token_label = "Y" if q.token_id == yes_token_id else "N"
                        side_label = "BID" if q.side == "BUY" else "ASK"
                        parts.append(f"{token_label}:{side_label} ${q.price:.2f}×{q.size:.0f}")
                    console.print(
                        f"  [cyan]📋 POST[/cyan] {' | '.join(parts)} | "
                        f"FV={qs.fair_value:.2f} spread={qs.spread:.3f}"
                    )

            except Exception as e:
                logger.warning(f"Post quotes failed: {e}")

    async def _cancel_market_quotes(self, condition_id: str, token_id: str):
        """Cancel all our orders on a market.

        Uses both tracked order IDs AND blanket cancel for belt-and-suspenders.
        """
        try:
            # First: cancel by tracked order IDs (most reliable)
            tracked_ids = self.live_order_ids.get(condition_id, [])
            if tracked_ids:
                try:
                    self.client.cancel_orders_batch(tracked_ids)
                except Exception:
                    pass  # Fall through to blanket cancel

            # Second: blanket cancel by token (catches any we lost track of)
            self.client.cancel_market_orders(asset_id=token_id)
            self.live_order_ids.pop(condition_id, None)
        except Exception as e:
            logger.warning(f"Cancel market orders failed: {e}")
            # Still clear tracking — stale IDs cause problems
            self.live_order_ids.pop(condition_id, None)

    def _log_dry_quotes(self, market, qs: QuoteSet, depth_momentum: float = 0.0):
        """Log quotes in dry-run mode."""
        if self.quiet:
            return
        parts = []
        if qs.yes_bid:
            parts.append(f"Y:BID ${qs.yes_bid.price:.2f}×{qs.yes_bid.size:.0f}")
        if qs.yes_ask:
            parts.append(f"Y:ASK ${qs.yes_ask.price:.2f}×{qs.yes_ask.size:.0f}")
        if qs.no_bid:
            parts.append(f"N:BID ${qs.no_bid.price:.2f}×{qs.no_bid.size:.0f}")
        if qs.no_ask:
            parts.append(f"N:ASK ${qs.no_ask.price:.2f}×{qs.no_ask.size:.0f}")
        spread_str = f"spread={qs.spread:.3f}"
        q = market.question[:60] if hasattr(market, 'question') else "?"
        depth_str = f"d={depth_momentum:+.2f}" if depth_momentum else ""
        console.print(
            f"  [dim]MM[/dim] {q} | FV={qs.fair_value:.2f} {spread_str} {depth_str} | "
            + " | ".join(parts)
        )

    # --- Heartbeat ---

    async def _heartbeat_loop(self):
        """Send heartbeats to prevent stale orders on crash."""
        while self._running:
            try:
                result = self.client.post_heartbeat(self.heartbeat_id)
                if isinstance(result, dict):
                    self.heartbeat_id = result.get("heartbeat_id", self.heartbeat_id)
            except Exception as e:
                logger.error(f"Heartbeat failed: {e}")
            await asyncio.sleep(self.config.heartbeat_interval_seconds)

    # --- Fill Monitor ---

    async def _fill_monitor_loop(self):
        """Poll for fills and update inventory."""
        while self._running:
            try:
                await self._check_fills()
            except Exception as e:
                logger.warning(f"Fill monitor error: {e}")
            await asyncio.sleep(self.config.fill_check_interval_seconds)

    async def _check_fills(self):
        """Check for new fills across all markets."""
        for cid, m in list(self.active_markets.items()):
            if not m.clob_token_ids or len(m.clob_token_ids) < 2:
                continue

            # Check open orders — if any disappeared, they were filled
            order_ids = self.live_order_ids.get(cid, [])
            if not order_ids:
                continue

            try:
                # Check orders for BOTH YES and NO tokens
                yes_orders = self.client.get_open_orders_for_market(
                    asset_id=m.clob_token_ids[0]
                )
                no_orders = self.client.get_open_orders_for_market(
                    asset_id=m.clob_token_ids[1]
                )
                current_orders = yes_orders + no_orders
                current_ids = {o.get("id", o.get("order_id", "")) for o in current_orders}

                # Find filled orders (were live, now gone)
                for oid in order_ids:
                    if oid and oid not in current_ids:
                        # Order was filled or cancelled — check via API
                        await self._process_potential_fill(cid, oid)

                # Update live order list
                self.live_order_ids[cid] = [oid for oid in order_ids if oid in current_ids]

            except Exception as e:
                if self.verbose:
                    logger.warning(f"Fill check error for {cid[:8]}: {e}")

    async def _process_potential_fill(self, condition_id: str, order_id: str):
        """Check if an order was filled (vs cancelled) and record it."""
        try:
            order_info = self.client.get_order(order_id)
            if not order_info:
                return

            status = order_info.get("status", "")
            if status not in ("MATCHED", "FILLED"):
                return  # Was cancelled, not filled

            side = order_info.get("side", "BUY")
            price = float(order_info.get("price", 0))
            size = float(order_info.get("size_matched", order_info.get("original_size", 0)))
            token_id = order_info.get("asset_id", "")

            if size <= 0:
                return

            # Determine if YES or NO token
            token = "YES"
            for m in self.active_markets.values():
                if m.clob_token_ids and len(m.clob_token_ids) >= 2:
                    if token_id == m.clob_token_ids[1]:
                        token = "NO"
                        break

            # record_fill returns avg_entry for SELLs (computed BEFORE inventory update)
            avg_entry = self.strategy.record_fill(condition_id, side, token, size, price)
            self._total_fills += 1

            inv = self.strategy.get_inventory(condition_id)
            m = self.active_markets.get(condition_id)
            q_short = m.question[:40] if m else condition_id[:8]
            console.print(
                f"  [green bold]💰 FILL[/green bold] {side} {size:.1f} {token} @ ${price:.2f} | "
                f"{q_short} | Inv: YES={inv.yes_tokens:.1f} NO={inv.no_tokens:.1f}"
            )

            # --- DB trade logging ---
            await self._log_fill_to_db(
                condition_id, order_id, token_id, token, side, price, size, m, avg_entry
            )

        except Exception as e:
            if self.verbose:
                logger.warning(f"Process fill error: {e}")

    async def _log_fill_to_db(
        self,
        condition_id: str,
        order_id: str,
        token_id: str,
        token: str,  # "YES" or "NO"
        side: str,  # "BUY" or "SELL"
        price: float,
        size: float,
        market: Market | None,
        avg_entry: float | None,
    ):
        """Log a fill to the trades/positions/orders tables in DB.

        avg_entry is provided by record_fill() for SELL fills — computed
        BEFORE inventory was decremented, so the cost basis is accurate.
        """
        try:
            question = market.question if market else ""
            inv = self.strategy.get_inventory(condition_id)

            # Snapshot current state for analysis
            signal_data = {
                "fill_side": side,
                "fill_token": token,
                "fill_price": price,
                "fill_size": size,
                "fair_value": self.strategy._last_fair_value.get(condition_id, 0),
                "inventory_yes": inv.yes_tokens,
                "inventory_no": inv.no_tokens,
                "inventory_imbalance": inv.imbalance,
                "depth_momentum": self._get_depth_momentum(),
            }

            if side == "BUY":
                # Entry — new position or adding to existing
                await self.db.insert_trade({
                    "trade_id": order_id,
                    "market_id": condition_id,
                    "token_id": token_id,
                    "question": question,
                    "side": side,
                    "entry_price": price,
                    "size": size,
                    "status": "OPEN",
                    "strategy": "market_maker",
                    "signal_data": signal_data,
                })

                await self.db.upsert_position({
                    "market_id": condition_id,
                    "token_id": token_id,
                    "question": question,
                    "side": side,
                    "size": inv.yes_tokens if token == "YES" else inv.no_tokens,
                    "entry_price": inv.avg_cost(token),  # Weighted avg, not last fill
                    "current_price": price,
                    "unrealized_pnl": 0,
                    "strategy": "market_maker",
                })

            else:
                # Exit — selling tokens we hold
                # avg_entry was computed BEFORE inventory decrement, so it's accurate
                entry_price = avg_entry if avg_entry and avg_entry > 0 else price
                gross_pnl = (price - entry_price) * size

                self._total_spread_captured += gross_pnl

                await self.db.close_trade_by_market(
                    market_id=condition_id,
                    exit_price=price,
                    pnl=gross_pnl,
                    exit_reason="maker_sell",
                )

                # Check remaining inventory (already decremented by record_fill)
                remaining = inv.yes_tokens if token == "YES" else inv.no_tokens
                if remaining <= 0.1:
                    await self.db.remove_position(condition_id, token_id, "BUY")
                else:
                    # Update position with remaining size
                    await self.db.upsert_position({
                        "market_id": condition_id,
                        "token_id": token_id,
                        "question": question,
                        "side": "BUY",
                        "size": remaining,
                        "entry_price": entry_price,
                        "current_price": price,
                        "unrealized_pnl": 0,
                        "strategy": "market_maker",
                    })

                if not self.quiet:
                    console.print(
                        f"  [green]📊 P&L[/green] ${gross_pnl:+.2f} on {size:.1f} {token} "
                        f"(entry ${entry_price:.2f} → exit ${price:.2f})"
                    )

        except Exception as e:
            logger.warning(f"DB trade log failed: {e}")

    # --- Status ---

    async def _status_loop(self):
        """Print periodic status updates."""
        while self._running:
            if not self.quiet:
                self._print_status()
            await asyncio.sleep(STATUS_INTERVAL)

    def _print_status(self):
        """Print market maker status line."""
        # Depth info (lazy lookup)
        depth_str = ""
        dm = self._get_depth_momentum()
        if dm != 0:
            depth_str = f"Depth:{dm:+.2f}"

        # Inventory summary
        total_inv = sum(
            inv.yes_tokens + inv.no_tokens
            for inv in self.strategy.inventory.values()
        )

        # Count live orders
        total_orders = sum(len(ids) for ids in self.live_order_ids.values())

        # Count quoting markets
        quoting = sum(1 for ids in self.live_order_ids.values() if ids)

        mode = "DRY" if self.dry else ("AUTO" if self.auto else "COPILOT")
        pnl_str = f"P&L: ${self._total_spread_captured:+.2f}" if self._total_spread_captured else ""
        console.print(
            f"\n[dim bold]── Maker Status ── {mode} ─ "
            f"Mkts: {len(self.active_markets)} (quoting: {quoting}) | "
            f"Orders: {total_orders} | Fills: {self._total_fills} | "
            f"Quotes: {self._total_quotes_posted} Pulls: {self._total_pulls} | "
            f"Inv: {total_inv:.1f} tokens | {pnl_str} "
            f"{depth_str}[/dim bold]"
        )

    # --- Config Hot Reload ---

    async def _config_refresh_loop(self):
        """Reload config from DB every 30 seconds."""
        while self._running:
            try:
                from polyedge.core.config import apply_db_config
                await apply_db_config(self.settings, self.db)
                new_config = self.settings.strategies.market_maker
                self.config = new_config
                self.strategy.config = new_config
            except Exception as e:
                logger.warning(f"Config refresh failed: {e}")
            await asyncio.sleep(30)
