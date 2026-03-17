"""Market Maker Runner — persistent async loop for spread capture on Polymarket.

Two modes:
  Crypto mode (--market "btc 15m"): Window hopping on crypto up/down markets.
  Static mode (--condition-id <cid>): Any Polymarket market, no expiry.

Connects to:
  - Polymarket WebSocket for real-time YES/NO book data
  - CLOB API for posting/canceling post-only limit orders
  - CLOB trade history for fill detection with actual prices
  - [Optional] Binance depth for crypto defense signal

Main loop:
1. Warmup — wait for Poly book data before quoting
2. Quote loop — event-driven requoting with floor interval
3. Fill monitor — polls CLOB trade history for actual fill prices
4. Heartbeat — dead-man switch (10s timeout)
5. Window management — hops, force-sell, market refresh (crypto only)
6. Status — periodic stats

Usage:
    polyedge maker --dry --market "btc 15m"     # Crypto mode, dry run
    polyedge maker --dry --condition-id abc123   # Static mode, any market
    polyedge maker --auto --market "btc"         # Auto-trade all BTC windows
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
from polyedge.data.book_analyzer import analyze_book, BookIntelligence
from polyedge.data.ws_feed import (
    MarketFeed, EVENT_BEST_BID_ASK, EVENT_LAST_TRADE, EVENT_BOOK,
)
from polyedge.strategies.market_maker import (
    MarketMakerStrategy,
    QuoteSet,
)
from polyedge.core.console import console

logger = logging.getLogger("polyedge.maker_runner")

MARKET_REFRESH_INTERVAL = 120  # seconds
STATUS_INTERVAL = 15  # seconds

# CLOB API returns token balances in micro-units (6 decimals).
# 3770000 raw = 3.77 actual tokens.
TOKEN_BALANCE_DIVISOR = 1e6


def _parse_token_balance(bal_resp: dict | float) -> float:
    """Convert raw CLOB token balance response to actual token count."""
    if isinstance(bal_resp, dict):
        raw = bal_resp.get("balance", 0)
    else:
        raw = bal_resp
    raw_float = float(str(raw))
    # If the value is huge (>10000), it's in micro-units
    if raw_float > 10000:
        return round(raw_float / TOKEN_BALANCE_DIVISOR, 2)
    return round(raw_float, 2)


class MakerRunner:
    """Persistent async loop for market making on Polymarket.

    Posts post-only limit orders on both sides of any market.
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
        condition_ids: list[str] | None = None,
        verbose: bool = False,
        quiet: bool = False,
    ):
        self.settings = settings
        self.client = client
        self.db = db
        self.auto = auto
        self.dry = dry
        self.verbose = verbose
        self.quiet = quiet

        # Mode detection
        self.mode: str = "static" if condition_ids else "crypto"
        self._condition_ids = condition_ids or []

        # Crypto mode: parse --market filter
        self.market_filter = market_filter.lower() if market_filter else None
        self._duration_filter_minutes: int | None = None
        self._filter_patterns = []
        if self.market_filter:
            self._parse_market_filter()

        self.config: MarketMakerConfig = settings.strategies.market_maker
        self.strategy = MarketMakerStrategy(self.config)

        # Market state
        # Crypto mode: symbol -> [Market, ...] sorted by end_date (window hopping)
        self.windows: dict[str, list[Market]] = {}
        # Both modes: condition_id -> Market
        self.active_markets: dict[str, Market] = {}
        # token_id -> (Market, "yes"|"no")
        self._token_to_market: dict[str, tuple[Market, str]] = {}

        # Polymarket WebSocket
        self.poly_feed: Optional[MarketFeed] = None
        self._subscribed_tokens: set[str] = set()

        # Optional Binance depth (crypto mode only)
        self.depth_feed = None

        # Order tracking
        self.live_order_ids: dict[str, list[str]] = {}  # condition_id -> [order_ids]
        self.heartbeat_id: str | None = None

        # Fill tracking — cursor for CLOB trade history polling
        self._fill_cursor_ts: int = int(time.time())

        # Locks
        self._quote_lock = asyncio.Lock()
        self._requote_event = asyncio.Event()

        # Warmup gate
        self._warmup_complete = False
        self._warmup_start: float = 0.0

        # Track last known best prices per token to detect material changes
        self._last_best: dict[str, tuple[float, float]] = {}  # token_id -> (bid, ask)

        # State
        self._running = False
        self._last_market_refresh = 0.0
        self._last_status = 0.0
        self._last_hop_time = 0.0
        self._total_fills = 0
        self._total_spread_captured = 0.0
        self._total_quotes_posted = 0
        self._total_pulls = 0

    def _parse_market_filter(self):
        """Parse --market "btc 15m" into text patterns + duration filter."""
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

    # ===================================================================
    # Main entry point
    # ===================================================================

    async def run(self):
        """Main entry point — runs until cancelled."""
        self._running = True
        logger.info(f"Market Maker starting in {self.mode.upper()} mode...")

        # Load markets
        if self.mode == "crypto":
            await self._refresh_markets()
            if not self.windows:
                logger.error("No active crypto up/down markets found. Exiting.")
                return
        else:
            await self._load_static_markets()
            if not self.active_markets:
                logger.error("No markets loaded. Check --condition-id values.")
                return

        # Start Polymarket WebSocket
        all_token_ids = self._collect_token_ids()
        await self._start_poly_feed(all_token_ids)

        # Reconcile inventory from actual CLOB balances
        if not self.dry:
            await self._reconcile_inventory()

        # Optional Binance depth for crypto mode
        if self.mode == "crypto" and self.config.depth_defense_enabled:
            from polyedge.data.binance_depth import BinanceDepthFeed
            symbols = list(self.windows.keys()) or ["btcusdt"]
            self.depth_feed = BinanceDepthFeed(symbols=symbols)

        # Launch concurrent tasks
        tasks = []
        if self.depth_feed:
            tasks.append(asyncio.create_task(self.depth_feed.start()))
        tasks.append(asyncio.create_task(self._poly_feed_loop()))
        tasks.append(asyncio.create_task(self._quote_loop()))
        tasks.append(asyncio.create_task(self._status_loop()))
        tasks.append(asyncio.create_task(self._config_refresh_loop()))

        if self.config.heartbeat_enabled and not self.dry:
            tasks.append(asyncio.create_task(self._heartbeat_loop()))

        if not self.dry:
            tasks.append(asyncio.create_task(self._fill_monitor_loop()))
            tasks.append(asyncio.create_task(self._balance_reconcile_loop()))

        if self.mode == "crypto":
            tasks.append(asyncio.create_task(self._force_sell_loop()))

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Market Maker shutting down...")
        finally:
            await self._shutdown()

    # ===================================================================
    # Market Loading
    # ===================================================================

    async def _load_static_markets(self):
        """Load markets by condition_id (static mode)."""
        import aiohttp
        from polyedge.data.markets import _parse_market

        gamma_url = self.settings.polymarket.gamma_url
        self.active_markets.clear()
        self._token_to_market.clear()

        for cid in self._condition_ids:
            try:
                async with aiohttp.ClientSession() as session:
                    url = f"{gamma_url}/markets/{cid}"
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            logger.warning(f"Market {cid[:12]} not found (HTTP {resp.status})")
                            continue
                        data = await resp.json()

                m = _parse_market(data)
                self.active_markets[m.condition_id] = m
                if m.clob_token_ids and len(m.clob_token_ids) >= 2:
                    self._token_to_market[m.clob_token_ids[0]] = (m, "yes")
                    self._token_to_market[m.clob_token_ids[1]] = (m, "no")

                if not self.quiet:
                    console.print(f"[cyan]Loaded: {m.question[:70]}[/cyan]")

            except Exception as e:
                logger.warning(f"Failed to load market {cid[:12]}: {e}")

        if not self.quiet:
            console.print(f"[dim]Loaded {len(self.active_markets)} static market(s)[/dim]")

    def _matches_filter(self, market: Market) -> bool:
        """Check if a market matches the --market filter (text + duration)."""
        if not self._filter_patterns and self._duration_filter_minutes is None:
            return True

        haystack = f"{market.question} {market.slug or ''}"
        if not all(p.search(haystack) for p in self._filter_patterns):
            return False

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
        """Fetch live crypto markets from Gamma API (sorted by endDate ASC)."""
        import aiohttp
        from polyedge.data.markets import _parse_market
        from polyedge.strategies.crypto_sniper import UP_DOWN_PATTERN

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

            crypto_count = sum(1 for m in found if UP_DOWN_PATTERN.search(m.question))
            if crypto_count >= 30:
                break

        if not self.quiet:
            from polyedge.strategies.crypto_sniper import UP_DOWN_PATTERN
            console.print(
                f"[dim]Fetched {len(found)} markets "
                f"({sum(1 for m in found if UP_DOWN_PATTERN.search(m.question))} crypto up/down)[/dim]"
            )
        return found

    async def _refresh_markets(self, prefetched: list[Market] | None = None):
        """Refresh active crypto up/down markets (crypto mode)."""
        from polyedge.strategies.crypto_sniper import (
            UP_DOWN_PATTERN, CRYPTO_SYMBOL_MAP, EXCLUDED_PATTERNS,
        )

        try:
            all_markets = prefetched or await self._quick_sync()
            now = datetime.now(timezone.utc)
            candidates: dict[str, list[Market]] = {}

            for m in all_markets:
                if not UP_DOWN_PATTERN.search(m.question):
                    continue
                if EXCLUDED_PATTERNS.search(m.question):
                    continue
                if not m.end_date or m.end_date <= now:
                    continue
                if not self._matches_filter(m):
                    continue

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

            self.windows.clear()
            self.active_markets.clear()
            self._token_to_market.clear()

            for symbol, market_list in candidates.items():
                market_list.sort(key=lambda m: m.end_date)
                selected = market_list[:10]
                self.windows[symbol] = selected

                for m in selected:
                    self.active_markets[m.condition_id] = m
                    if m.clob_token_ids and len(m.clob_token_ids) >= 2:
                        self._token_to_market[m.clob_token_ids[0]] = (m, "yes")
                        self._token_to_market[m.clob_token_ids[1]] = (m, "no")

                if selected and not self.quiet:
                    current = selected[0]
                    remaining = (current.end_date - now).total_seconds()
                    console.print(
                        f"[cyan]{symbol.replace('usdt','').upper()}: "
                        f"[bold]{current.question}[/bold] "
                        f"({remaining:.0f}s left, {len(selected)} windows loaded)[/cyan]"
                    )

            self._last_market_refresh = time.monotonic()

            # Update Poly WS subscriptions
            new_tokens = self._collect_token_ids()
            if set(new_tokens) != self._subscribed_tokens and new_tokens:
                await self._start_poly_feed(new_tokens)

            if not self.quiet:
                total = sum(len(w) for w in self.windows.values())
                logger.info(f"Active: {len(self.windows)} symbols, {total} windows")

        except Exception as e:
            logger.error(f"Market refresh failed: {e}")

    def _collect_token_ids(self) -> list[str]:
        """Collect all token IDs from active markets."""
        token_ids = []
        for m in self.active_markets.values():
            if m.clob_token_ids and len(m.clob_token_ids) >= 2:
                token_ids.extend(m.clob_token_ids[:2])
        return token_ids

    def _get_current_markets(self) -> dict[str, Market]:
        """Get markets we should be quoting right now.

        Crypto mode: only the CURRENT (first) window per symbol.
        Static mode: all loaded markets.
        """
        if self.mode == "static":
            return dict(self.active_markets)

        result = {}
        now = datetime.now(timezone.utc)
        for symbol, market_list in self.windows.items():
            if market_list:
                current = market_list[0]
                if current.end_date and current.end_date > now:
                    result[current.condition_id] = current
        return result

    # ===================================================================
    # Window Hopping (crypto mode only)
    # ===================================================================

    def _check_window_hops(self):
        """Check if current windows expired and hop to next one."""
        if self.mode != "crypto":
            return

        now = datetime.now(timezone.utc)
        hopped = False

        for symbol in list(self.windows.keys()):
            market_list = self.windows[symbol]
            if not market_list:
                continue
            current = market_list[0]
            if not current.end_date or current.end_date > now:
                continue

            # Window expired
            old_cid = current.condition_id

            # Cancel orders on expired window
            if not self.dry:
                try:
                    if current.clob_token_ids and len(current.clob_token_ids) >= 2:
                        self.client.cancel_market_orders(asset_id=current.clob_token_ids[0])
                        self.client.cancel_market_orders(asset_id=current.clob_token_ids[1])
                except Exception as e:
                    logger.warning(f"Cancel on hop failed: {e}")
            self.live_order_ids.pop(old_cid, None)

            # Warn about stranded inventory
            inv = self.strategy.get_inventory(old_cid)
            if inv.yes_tokens > 0 or inv.no_tokens > 0:
                logger.warning(
                    f"Window expired with inventory! YES={inv.yes_tokens:.1f} "
                    f"NO={inv.no_tokens:.1f} on {current.question[:40]}"
                )

            # Reset strategy state
            self.strategy.reset_window(old_cid)

            # Promote next window
            market_list.pop(0)
            self._last_hop_time = time.monotonic()
            hopped = True

            if market_list:
                next_m = market_list[0]
                remaining = (next_m.end_date - now).total_seconds()
                console.print(
                    f"\n[yellow]HOP[/yellow] {symbol.replace('usdt','').upper()}: "
                    f"{next_m.question[:50]} ({remaining:.0f}s left)"
                )
            else:
                logger.warning(f"No more windows for {symbol} — need refresh")

        if hopped:
            # Rebuild flat view
            self.active_markets.clear()
            self._token_to_market.clear()
            for symbol, market_list in self.windows.items():
                for m in market_list:
                    self.active_markets[m.condition_id] = m
                    if m.clob_token_ids and len(m.clob_token_ids) >= 2:
                        self._token_to_market[m.clob_token_ids[0]] = (m, "yes")
                        self._token_to_market[m.clob_token_ids[1]] = (m, "no")

            remaining_windows = sum(len(w) for w in self.windows.values())
            if remaining_windows <= 3:
                asyncio.create_task(self._refresh_markets())

    # ===================================================================
    # Polymarket Feed
    # ===================================================================

    async def _start_poly_feed(self, token_ids: list[str]):
        """Start or restart the Polymarket WebSocket."""
        if self.poly_feed:
            try:
                await self.poly_feed.stop()
            except Exception:
                pass

        self.poly_feed = MarketFeed(self.settings)

        async def _on_best_bid_ask(event: dict):
            token_id = event.get("asset_id", "")
            if not token_id:
                return
            entry = self._token_to_market.get(token_id)
            if not entry:
                return
            # Only trigger requote if mid price moved >= 0.5 cents
            try:
                bid = float(event.get("best_bid", 0))
                ask = float(event.get("best_ask", 0))
            except (ValueError, TypeError):
                return
            prev = self._last_best.get(token_id)
            if prev:
                old_mid = (prev[0] + prev[1]) / 2
                new_mid = (bid + ask) / 2
                if abs(new_mid - old_mid) < 0.005:
                    self._last_best[token_id] = (bid, ask)
                    return  # Not material — skip requote
            self._last_best[token_id] = (bid, ask)
            self._requote_event.set()

        async def _on_book(event: dict):
            """Full book snapshot — trigger requote (infrequent event)."""
            self._requote_event.set()

        self.poly_feed.on(EVENT_BEST_BID_ASK, _on_best_bid_ask)
        self.poly_feed.on(EVENT_BOOK, _on_book)
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

    # ===================================================================
    # BookIntelligence
    # ===================================================================

    def _get_book_intel(self, market: Market) -> tuple[BookIntelligence | None, BookIntelligence | None]:
        """Get BookIntelligence for YES and NO tokens from Poly WS."""
        if not self.poly_feed or not market.clob_token_ids or len(market.clob_token_ids) < 2:
            return None, None

        yes_book = self.poly_feed.get_book(market.clob_token_ids[0])
        no_book = self.poly_feed.get_book(market.clob_token_ids[1])

        yes_intel = analyze_book(yes_book) if yes_book else None
        no_intel = analyze_book(no_book) if no_book else None
        return yes_intel, no_intel

    def _get_depth_momentum(self) -> float:
        """Get Binance depth momentum (crypto mode only)."""
        if not self.depth_feed:
            return 0.0
        symbols = list(self.windows.keys()) or ["btcusdt"]
        for sym in symbols:
            ds = self.depth_feed.depth.get(sym)
            if ds and ds.is_active:
                return ds.depth_momentum
        return 0.0

    # ===================================================================
    # Warmup Gate
    # ===================================================================

    def _check_warmup(self) -> bool:
        """Check if warmup is complete (book data received + time elapsed)."""
        if self._warmup_complete:
            return True

        if not self.poly_feed or not self.poly_feed.is_connected:
            return False

        # Need book data for at least one market
        for m in self._get_current_markets().values():
            if not m.clob_token_ids or len(m.clob_token_ids) < 2:
                continue
            book = self.poly_feed.get_book(m.clob_token_ids[0])
            if book and (book.bids or book.asks):
                # Have data — check time
                if self._warmup_start == 0:
                    self._warmup_start = time.monotonic()
                    if not self.quiet:
                        console.print("[yellow]Warming up — got first book data...[/yellow]")

                elapsed = time.monotonic() - self._warmup_start
                if elapsed >= self.config.warmup_seconds:
                    self._warmup_complete = True
                    if not self.quiet:
                        console.print("[green]Warmup complete — quoting enabled.[/green]")
                    return True

        return False

    # ===================================================================
    # Quote Loop
    # ===================================================================

    async def _quote_loop(self):
        """Main quoting loop — event-driven with floor interval.

        Orders stay live until fair value moves materially (>= 0.5 cent).
        The floor interval (min_requote_interval, default 3s) fires periodic
        refreshes even when the book is quiet, to update time-decay spreads.
        """
        last_post_time = 0.0
        while self._running:
            try:
                # Wait for event or floor interval
                try:
                    await asyncio.wait_for(
                        self._requote_event.wait(),
                        timeout=self.config.min_requote_interval,
                    )
                except asyncio.TimeoutError:
                    pass
                self._requote_event.clear()

                # Enforce minimum time between actual order posts
                now = time.monotonic()
                since_last_post = now - last_post_time
                if since_last_post < self.config.min_requote_interval:
                    continue

                # Check window hops (crypto mode)
                self._check_window_hops()

                # Refresh markets periodically (crypto mode)
                if (
                    self.mode == "crypto"
                    and time.monotonic() - self._last_market_refresh > MARKET_REFRESH_INTERVAL
                ):
                    await self._refresh_markets()

                # Window hop cooldown
                if self._last_hop_time > 0:
                    hop_elapsed = time.monotonic() - self._last_hop_time
                    if hop_elapsed < self.config.window_hop_pause_seconds:
                        continue

                # Warmup gate
                if not self._check_warmup():
                    continue

                # Quote each current market
                depth_momentum = self._get_depth_momentum()
                current_markets = self._get_current_markets()
                for cid, m in current_markets.items():
                    await self._quote_market(cid, m, depth_momentum)
                last_post_time = time.monotonic()

            except Exception as e:
                logger.error(f"Quote loop error: {e}")

    async def _quote_market(
        self,
        condition_id: str,
        m: Market,
        depth_momentum: float,
    ):
        """Compute and post quotes for a single market."""
        if not m.clob_token_ids or len(m.clob_token_ids) < 2:
            return

        yes_token_id = m.clob_token_ids[0]
        no_token_id = m.clob_token_ids[1]

        # Get time remaining (None for static markets)
        seconds_remaining: float | None = None
        if m.end_date:
            now = datetime.now(timezone.utc)
            remaining = (m.end_date - now).total_seconds()
            if remaining <= 0:
                return
            seconds_remaining = remaining

        # Get BookIntelligence from Poly WS
        yes_intel, no_intel = self._get_book_intel(m)

        # Compute quotes
        qs = self.strategy.compute_quotes(
            condition_id=condition_id,
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            yes_book=yes_intel,
            no_book=no_intel,
            seconds_remaining=seconds_remaining,
            depth_momentum=depth_momentum,
        )

        if qs.reason_skipped:
            # Cancel existing orders if we're pulling
            if condition_id in self.live_order_ids and self.live_order_ids[condition_id]:
                if not self.dry:
                    await self._cancel_market_quotes(condition_id, yes_token_id)
                    await self._cancel_market_quotes(condition_id, no_token_id)
            if qs.reason_skipped not in ("no_requote_needed",):
                self._total_pulls += 1
                if self.verbose:
                    console.print(
                        f"  [yellow]PULL[/yellow] {qs.reason_skipped} | "
                        f"depth={depth_momentum:+.2f}"
                    )
            return

        if not qs.is_active:
            return

        self._total_quotes_posted += 1
        if self.dry:
            self._log_dry_quotes(m, qs)
        else:
            await self._post_quotes(condition_id, yes_token_id, no_token_id, qs)

    async def _post_quotes(
        self, condition_id: str, yes_token_id: str, no_token_id: str, qs: QuoteSet,
    ):
        """Cancel old quotes and post new ones."""
        async with self._quote_lock:
            try:
                if self.config.cancel_before_requote:
                    await self._cancel_market_quotes(condition_id, yes_token_id)
                    await self._cancel_market_quotes(condition_id, no_token_id)
                    await asyncio.sleep(0.1)  # Cancel propagation

                orders = [q.as_order_dict() for q in qs.all_quotes()]
                if not orders:
                    return

                result = self.client.post_orders_batch(orders[:15])

                # Track order IDs
                if isinstance(result, dict) and "orderIDs" in result:
                    self.live_order_ids[condition_id] = result["orderIDs"]
                elif isinstance(result, list):
                    ids = [r.get("orderID", r.get("id", "")) for r in result if isinstance(r, dict)]
                    self.live_order_ids[condition_id] = [i for i in ids if i]

                if not self.quiet:
                    parts = []
                    for q in qs.all_quotes():
                        token_label = "Y" if q.token_id == yes_token_id else "N"
                        side_label = "BID" if q.side == "BUY" else "ASK"
                        parts.append(f"{token_label}:{side_label} ${q.price:.2f}x{q.size:.0f}")
                    console.print(
                        f"  [cyan]POST[/cyan] {' | '.join(parts)} | "
                        f"FV={qs.fair_value:.2f} spread={qs.spread:.3f}"
                    )

            except Exception as e:
                logger.warning(f"Post quotes failed: {e}")

    async def _cancel_market_quotes(self, condition_id: str, token_id: str):
        """Cancel all our orders on a token (tracked IDs + blanket)."""
        try:
            tracked_ids = self.live_order_ids.get(condition_id, [])
            if tracked_ids:
                try:
                    self.client.cancel_orders_batch(tracked_ids)
                except Exception:
                    pass
            self.client.cancel_market_orders(asset_id=token_id)
            self.live_order_ids.pop(condition_id, None)
        except Exception as e:
            logger.warning(f"Cancel market orders failed: {e}")
            self.live_order_ids.pop(condition_id, None)

    def _log_dry_quotes(self, market: Market, qs: QuoteSet):
        """Log quotes in dry-run mode."""
        if self.quiet:
            return
        parts = []
        yes_tid = market.clob_token_ids[0] if market.clob_token_ids else ""
        for q in qs.all_quotes():
            token_label = "Y" if q.token_id == yes_tid else "N"
            side_label = "BID" if q.side == "BUY" else "ASK"
            parts.append(f"{token_label}:{side_label} ${q.price:.2f}x{q.size:.0f}")
        q_text = market.question[:60] if hasattr(market, 'question') else "?"
        inv = self.strategy.get_inventory(market.condition_id)
        inv_str = f"Inv: Y={inv.yes_tokens:.0f} N={inv.no_tokens:.0f}" if (inv.yes_tokens or inv.no_tokens) else ""
        console.print(
            f"  [dim]MM[/dim] {q_text} | FV={qs.fair_value:.2f} spread={qs.spread:.3f} | "
            + " | ".join(parts) + (f" | {inv_str}" if inv_str else "")
        )

    # ===================================================================
    # Force-Sell Loop (crypto mode)
    # ===================================================================

    async def _force_sell_loop(self):
        """Force-sell inventory before crypto windows expire."""
        while self._running:
            try:
                now = datetime.now(timezone.utc)
                for symbol, market_list in self.windows.items():
                    if not market_list:
                        continue
                    current = market_list[0]
                    if not current.end_date:
                        continue

                    remaining = (current.end_date - now).total_seconds()
                    cid = current.condition_id
                    inv = self.strategy.get_inventory(cid)

                    if remaining < self.config.force_sell_seconds and (inv.yes_tokens > 0 or inv.no_tokens > 0):
                        if not current.clob_token_ids or len(current.clob_token_ids) < 2:
                            continue

                        yes_intel, no_intel = self._get_book_intel(current)
                        force_qs = self.strategy.compute_force_sell_quotes(
                            condition_id=cid,
                            yes_token_id=current.clob_token_ids[0],
                            no_token_id=current.clob_token_ids[1],
                            seconds_remaining=remaining,
                            yes_book=yes_intel,
                            no_book=no_intel,
                        )

                        if force_qs.is_active:
                            if not self.quiet:
                                console.print(
                                    f"  [red]FORCE SELL[/red] {remaining:.0f}s left | "
                                    f"YES={inv.yes_tokens:.1f} NO={inv.no_tokens:.1f}"
                                )
                            if not self.dry:
                                await self._post_quotes(
                                    cid,
                                    current.clob_token_ids[0],
                                    current.clob_token_ids[1],
                                    force_qs,
                                )
            except Exception as e:
                logger.warning(f"Force-sell loop error: {e}")

            await asyncio.sleep(2)

    # ===================================================================
    # Fill Monitor — actual fill prices from CLOB API
    # ===================================================================

    async def _fill_monitor_loop(self):
        """Poll CLOB trade history for fills with actual prices."""
        while self._running:
            try:
                await self._check_fills()
            except Exception as e:
                logger.warning(f"Fill monitor error: {e}")
            await asyncio.sleep(self.config.fill_check_interval_seconds)

    async def _check_fills(self):
        """Fetch new fills from CLOB API since last check."""
        try:
            fills = self.client.get_trades(after=self._fill_cursor_ts)
        except Exception as e:
            logger.warning(f"get_trades failed: {e}")
            return

        if not fills:
            return

        for fill in fills:
            token_id = fill.get("asset_id", "")
            if token_id not in self._token_to_market:
                continue  # Not our market

            market, token_side = self._token_to_market[token_id]
            cid = market.condition_id
            token = "YES" if token_side == "yes" else "NO"

            try:
                price = float(fill.get("price", 0))
                size = float(fill.get("size", 0))
                side = fill.get("side", "").upper()
            except (ValueError, TypeError):
                continue

            if price <= 0 or size <= 0 or side not in ("BUY", "SELL"):
                continue

            # Record fill in strategy inventory
            avg_entry = self.strategy.record_fill(cid, side, token, size, price)
            self._total_fills += 1

            # Trigger requote (inventory changed)
            self._requote_event.set()

            # Display
            q_short = market.question[:40]
            console.print(
                f"  [green bold]FILL[/green bold] {side} {size:.1f} {token} @ ${price:.2f} | {q_short}"
            )

            # Log to DB
            fill_id = fill.get("id", "")
            await self._log_fill_to_db(
                cid, fill_id, token_id, token, side, price, size, market, avg_entry,
            )

            # Advance cursor
            match_time = fill.get("match_time")
            if match_time:
                try:
                    ts = int(match_time)
                    if ts > self._fill_cursor_ts:
                        self._fill_cursor_ts = ts
                except (ValueError, TypeError):
                    pass

    async def _log_fill_to_db(
        self,
        condition_id: str,
        fill_id: str,
        token_id: str,
        token: str,
        side: str,
        price: float,
        size: float,
        market: Market | None,
        avg_entry: float | None,
    ):
        """Log a fill to the trades/positions tables."""
        try:
            question = market.question if market else ""
            inv = self.strategy.get_inventory(condition_id)

            signal_data = {
                "fill_side": side,
                "fill_token": token,
                "fill_price": price,
                "fill_size": size,
                "fair_value": self.strategy._last_fair_value.get(condition_id, 0),
                "inventory_yes": inv.yes_tokens,
                "inventory_no": inv.no_tokens,
                "inventory_imbalance": inv.imbalance,
            }

            if side == "BUY":
                await self.db.insert_trade({
                    "trade_id": fill_id or None,
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
                    "entry_price": inv.avg_cost(token),
                    "current_price": price,
                    "unrealized_pnl": 0,
                    "strategy": "market_maker",
                })

            else:
                entry_price = avg_entry if avg_entry and avg_entry > 0 else price
                gross_pnl = (price - entry_price) * size
                self._total_spread_captured += gross_pnl

                await self.db.close_trade_by_market(
                    market_id=condition_id,
                    exit_price=price,
                    pnl=gross_pnl,
                    exit_reason="maker_sell",
                )

                remaining = inv.yes_tokens if token == "YES" else inv.no_tokens
                if remaining <= 0.1:
                    await self.db.remove_position(condition_id, token_id, "BUY")
                else:
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
                        f"  [green]P&L[/green] ${gross_pnl:+.2f} on {size:.1f} {token} "
                        f"(entry ${entry_price:.2f} -> exit ${price:.2f})"
                    )

        except Exception as e:
            logger.warning(f"DB trade log failed: {e}")

    # ===================================================================
    # Balance Reconciliation (belt-and-suspenders)
    # ===================================================================

    async def _balance_reconcile_loop(self):
        """Periodic balance check to catch any fills the trade history missed."""
        while self._running:
            await asyncio.sleep(self.config.balance_reconcile_interval)
            try:
                for cid, m in list(self.active_markets.items()):
                    if not m.clob_token_ids or len(m.clob_token_ids) < 2:
                        continue

                    yes_bal = self.client.get_token_balance(m.clob_token_ids[0])
                    no_bal = self.client.get_token_balance(m.clob_token_ids[1])

                    actual_yes = _parse_token_balance(yes_bal)
                    actual_no = _parse_token_balance(no_bal)

                    inv = self.strategy.get_inventory(cid)

                    # Warn on significant mismatch (don't auto-fix — trade history is source of truth)
                    if abs(actual_yes - inv.yes_tokens) > 1.0 or abs(actual_no - inv.no_tokens) > 1.0:
                        logger.warning(
                            f"Balance mismatch on {cid[:8]}: "
                            f"tracked YES={inv.yes_tokens:.1f} actual={actual_yes:.1f} | "
                            f"tracked NO={inv.no_tokens:.1f} actual={actual_no:.1f}"
                        )
                        # Sync to actual (our trade history may have missed something)
                        inv.yes_tokens = actual_yes
                        inv.no_tokens = actual_no

            except Exception as e:
                logger.warning(f"Balance reconcile error: {e}")

    # ===================================================================
    # Startup Reconciliation
    # ===================================================================

    async def _reconcile_inventory(self):
        """Check actual CLOB balances on startup and reconstruct cost basis."""
        for cid, m in self.active_markets.items():
            if not m.clob_token_ids or len(m.clob_token_ids) < 2:
                continue
            try:
                yes_bal = self.client.get_token_balance(m.clob_token_ids[0])
                no_bal = self.client.get_token_balance(m.clob_token_ids[1])

                actual_yes = _parse_token_balance(yes_bal)
                actual_no = _parse_token_balance(no_bal)

                if actual_yes > 0.5 or actual_no > 0.5:
                    inv = self.strategy.get_inventory(cid)

                    # Try to find actual entry prices from recent trade history
                    recent_fills = self.client.get_trades(
                        market=cid,
                        after=int(time.time()) - 3600,  # Last hour
                    )

                    yes_prices = [
                        float(f["price"]) for f in recent_fills
                        if f.get("asset_id") == m.clob_token_ids[0]
                        and f.get("side", "").upper() == "BUY"
                        and float(f.get("price", 0)) > 0
                    ]
                    no_prices = [
                        float(f["price"]) for f in recent_fills
                        if f.get("asset_id") == m.clob_token_ids[1]
                        and f.get("side", "").upper() == "BUY"
                        and float(f.get("price", 0)) > 0
                    ]

                    if actual_yes > 0.5:
                        inv.yes_tokens = actual_yes
                        # Use actual avg fill price if available, else estimate from book
                        avg_price = (sum(yes_prices) / len(yes_prices)) if yes_prices else 0.50
                        inv.yes_cost_basis = actual_yes * avg_price

                    if actual_no > 0.5:
                        inv.no_tokens = actual_no
                        avg_price = (sum(no_prices) / len(no_prices)) if no_prices else 0.50
                        inv.no_cost_basis = actual_no * avg_price

                    q = m.question[:50]
                    price_info = ""
                    if yes_prices or no_prices:
                        price_info = " (from trade history)"
                    else:
                        price_info = " (estimated)"
                    console.print(
                        f"  [yellow]RECONCILE[/yellow] {q} | "
                        f"YES={actual_yes:.1f} NO={actual_no:.1f}{price_info}"
                    )
                    logger.info(f"Reconciled: {q} YES={actual_yes:.1f} NO={actual_no:.1f}")

            except Exception as e:
                logger.warning(f"Reconcile failed for {cid[:8]}: {e}")

    # ===================================================================
    # Heartbeat
    # ===================================================================

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

    # ===================================================================
    # Status
    # ===================================================================

    async def _status_loop(self):
        """Print periodic status updates."""
        while self._running:
            if not self.quiet:
                self._print_status()
            await asyncio.sleep(STATUS_INTERVAL)

    def _print_status(self):
        """Print market maker status line."""
        total_inv = sum(
            inv.yes_tokens + inv.no_tokens
            for inv in self.strategy.inventory.values()
        )
        total_orders = sum(len(ids) for ids in self.live_order_ids.values())
        quoting = sum(1 for ids in self.live_order_ids.values() if ids)

        mode_str = "DRY" if self.dry else ("AUTO" if self.auto else "COPILOT")
        market_mode = self.mode.upper()
        pnl_str = f"P&L: ${self._total_spread_captured:+.2f}" if self._total_spread_captured else ""
        warmup_str = "" if self._warmup_complete else " [WARMING UP]"

        console.print(
            f"\n[dim bold]-- Maker {market_mode} {mode_str}{warmup_str} -- "
            f"Mkts: {len(self._get_current_markets())} (quoting: {quoting}) | "
            f"Orders: {total_orders} | Fills: {self._total_fills} | "
            f"Quotes: {self._total_quotes_posted} Pulls: {self._total_pulls} | "
            f"Inv: {total_inv:.1f} tokens | {pnl_str}[/dim bold]"
        )

    # ===================================================================
    # Config Hot Reload
    # ===================================================================

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

    # ===================================================================
    # Shutdown
    # ===================================================================

    async def _shutdown(self):
        """Clean shutdown — cancel all orders."""
        self._running = False
        if not self.dry:
            try:
                logger.info("Cancelling all open orders...")
                self.client.cancel_all_orders()
            except Exception as e:
                logger.error(f"Failed to cancel orders on shutdown: {e}")
