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
        self.yes_prices: dict[str, float] = {}  # condition_id -> YES price
        self.no_prices: dict[str, float] = {}  # condition_id -> NO price
        self._token_to_market: dict[str, tuple[Market, str]] = {}  # token_id -> (Market, "yes"|"no")

        # Depth feed
        self.depth_feed: Optional[BinanceDepthFeed] = None
        self.depth_structure: Optional[DepthStructure] = None

        # Polymarket WebSocket
        self.poly_feed: Optional[MarketFeed] = None
        self._subscribed_tokens: set[str] = set()

        # Order tracking
        self.live_order_ids: dict[str, list[str]] = {}  # condition_id -> [order_ids]
        self.heartbeat_id: str | None = None

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
            self.depth_structure = self.depth_feed.depth.get(
                symbols[0], None
            ) if symbols else None

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
                if old_cid in self.live_order_ids:
                    if not self.dry:
                        try:
                            if current.clob_token_ids:
                                self.client.cancel_market_orders(
                                    asset_id=current.clob_token_ids[0]
                                )
                        except Exception as e:
                            logger.warning(f"Cancel on hop failed: {e}")
                    self.live_order_ids.pop(old_cid, None)

                # Reset strategy state for old window
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

        # Register price update callback
        async def _on_price_event(event: dict):
            token_id = event.get("asset_id", "")
            price_str = event.get("price") or event.get("last_trade_price") or "0"
            try:
                price = float(price_str)
            except (ValueError, TypeError):
                return
            if not token_id or not price:
                return

            # Fast lookup via token map
            entry = self._token_to_market.get(token_id)
            if entry:
                m, side = entry
                cid = m.condition_id
                if side == "yes":
                    self.yes_prices[cid] = price
                else:
                    self.no_prices[cid] = price

        self.poly_feed.on(EVENT_BEST_BID_ASK, _on_price_event)
        self.poly_feed.on(EVENT_LAST_TRADE, _on_price_event)
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

                # Get depth momentum for defense
                depth_momentum = 0.0
                if self.depth_structure and self.depth_structure.is_active:
                    depth_momentum = self.depth_structure.depth_momentum

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

        # Compute quotes
        qs = self.strategy.compute_quotes(
            condition_id=condition_id,
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            yes_price=yes_price,
            no_price=no_price,
            seconds_remaining=seconds_remaining,
            depth_momentum=depth_momentum,
        )

        if qs.reason_pulled:
            # Cancel existing orders if we're pulling
            if condition_id in self.live_order_ids and self.live_order_ids[condition_id]:
                if not self.dry:
                    await self._cancel_market_quotes(condition_id, yes_token_id)
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
        if self.dry:
            self._log_dry_quotes(m, qs, depth_momentum)
        else:
            await self._post_quotes(condition_id, yes_token_id, qs)

    async def _post_quotes(self, condition_id: str, yes_token_id: str, qs: QuoteSet):
        """Cancel old quotes and post new ones."""
        try:
            # Cancel existing orders for this market
            if self.config.cancel_before_requote:
                await self._cancel_market_quotes(condition_id, yes_token_id)

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

            if self.verbose:
                logger.info(
                    f"Posted {len(orders)} quotes | FV={qs.fair_value:.2f} "
                    f"spread={qs.spread:.3f}"
                )

        except Exception as e:
            logger.warning(f"Post quotes failed: {e}")

    async def _cancel_market_quotes(self, condition_id: str, token_id: str):
        """Cancel all our orders on a market."""
        try:
            self.client.cancel_market_orders(asset_id=token_id)
            self.live_order_ids.pop(condition_id, None)
        except Exception as e:
            logger.warning(f"Cancel market orders failed: {e}")

    def _log_dry_quotes(self, market, qs: QuoteSet, depth_momentum: float = 0.0):
        """Log quotes in dry-run mode."""
        if self.quiet:
            return
        parts = []
        if qs.yes_bid:
            parts.append(f"BID ${qs.yes_bid.price:.2f}×{qs.yes_bid.size:.0f}")
        if qs.yes_ask:
            parts.append(f"ASK ${qs.yes_ask.price:.2f}×{qs.yes_ask.size:.0f}")
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
                current_orders = self.client.get_open_orders_for_market(
                    asset_id=m.clob_token_ids[0]
                )
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

            self.strategy.record_fill(condition_id, side, token, size, price)
            self._total_fills += 1

            console.print(
                f"  [green]FILL[/green] {side} {size:.1f} {token} @ ${price:.2f}"
            )

        except Exception as e:
            if self.verbose:
                logger.warning(f"Process fill error: {e}")

    # --- Status ---

    async def _status_loop(self):
        """Print periodic status updates."""
        while self._running:
            if not self.quiet:
                self._print_status()
            await asyncio.sleep(STATUS_INTERVAL)

    def _print_status(self):
        """Print market maker status line."""
        now = time.monotonic()

        # Depth info
        depth_str = ""
        if self.depth_structure and self.depth_structure.is_active:
            dm = self.depth_structure.depth_momentum
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
        console.print(
            f"\n[dim bold]── Maker Status ── {mode} ─ "
            f"Mkts: {len(self.active_markets)} (quoting: {quoting}) | "
            f"Orders: {total_orders} | Fills: {self._total_fills} | "
            f"Quotes: {self._total_quotes_posted} Pulls: {self._total_pulls} | "
            f"Inv: {total_inv:.1f} tokens | "
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
