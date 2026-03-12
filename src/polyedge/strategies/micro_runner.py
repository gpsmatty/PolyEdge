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
from polyedge.data.ws_feed import MarketFeed, EVENT_BEST_BID_ASK, EVENT_LAST_TRADE, EVENT_BOOK
from polyedge.data.book_analyzer import analyze_book, BookIntelligence
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
from polyedge.data.research import (
    ResearchLogger,
    NoTradeReason,
    compute_attribution,
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

        # Execution engine — instantiate once, not per-trade
        from polyedge.execution.engine import ExecutionEngine
        self.engine = ExecutionEngine(client, db, settings)

        # Market indexer
        self.indexer = MarketIndexer(settings, db)

        # Binance aggTrade feed (the core data source)
        self.agg_feed = BinanceAggTradeFeed(symbols=self.config.symbols)

        # Apply configurable momentum weights + score shaping to each MicroStructure
        for micro in self.agg_feed.micro.values():
            micro.weight_ofi_5s = self.config.weight_ofi_5s
            micro.weight_ofi_15s = self.config.weight_ofi_15s
            micro.weight_vwap_drift = self.config.weight_vwap_drift
            micro.weight_intensity = self.config.weight_intensity
            micro.vwap_drift_scale = self.config.vwap_drift_scale
            micro.dampener_agree_factor = self.config.dampener_agree_factor
            micro.dampener_disagree_factor = self.config.dampener_disagree_factor
            micro.dampener_flat_factor = self.config.dampener_flat_factor
            micro.dampener_price_deadzone = self.config.dampener_price_deadzone

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

        # Trailing stop tracking: condition_id -> {"entry_price": float, "hwm": float}
        # Maintained in parallel with _positions for minimal refactor risk.
        self._position_info: dict[str, dict] = {}

        # Exit attempt tracking: condition_id -> count of failed FOK sells
        self._exit_attempts: dict[str, int] = {}

        # Trade tracking
        self._trades_this_window: int = 0
        self._total_trades: int = 0
        self._total_flips: int = 0
        self._pnl_estimate: float = 0.0  # Rough P&L estimate

        # Stats
        self._total_markets = 0
        self._ws_price_updates = 0
        self._eval_count = 0
        self._eval_count_per_symbol: dict[str, int] = {}  # per-symbol tick counter
        self._last_status_time = 0.0
        self._last_trade_log: dict[str, float] = {}  # condition_id -> timestamp

        # Rate limiting: don't trade the same market more than once per N seconds
        self._trade_cooldown: float = self.config.trade_cooldown

        # On startup, wait for the next fresh window instead of jumping into
        # a partially-elapsed one with stale microstructure data.
        # --no-warmup skips this and trades immediately.
        self._waiting_for_fresh_window: bool = not skip_warmup
        self._startup_window_id: str | None = None  # condition_id of the window we're skipping

        # Research pipeline — logs signal snapshots, candidate events,
        # no-trade reasons, regime tags, and attribution data.
        self.research = ResearchLogger(db=db)

        # Window hop cooldown — seconds since last hop before allowing entries.
        # Lets stale cross-window momentum flush out of the 15s/30s OFI windows.
        self._last_hop_time: float = 0.0  # time.time() of most recent hop

    async def _quick_sync(self):
        """Targeted API fetch for crypto 5-min markets.

        The generic volume-sorted fetch misses short-duration crypto markets
        because they're buried behind high-volume political markets. Instead,
        sort by endDate ascending with end_date_min=now so the soonest-expiring
        (currently live) windows come first. This guarantees we always get the
        active window instead of accidentally skipping to a future one.
        """
        import aiohttp
        from polyedge.data.markets import _parse_market

        gamma_url = self.settings.polymarket.gamma_url
        found: list[Market] = []
        batch_size = 100
        now = datetime.now(timezone.utc)

        console.print("[dim]Targeted fetch (soonest-expiring first)...[/dim]")

        # Sort by endDate ASCENDING with end_date_min=now.
        # ASC puts the soonest-ending (currently live) windows first.
        # end_date_min filters out expired/ancient markets that used to
        # pollute the ascending sort with stale 2025 data.
        for page in range(20):  # Up to 2000 markets
            url = f"{gamma_url}/markets"
            params = {
                "limit": batch_size,
                "offset": page * batch_size,
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

        # --- Diagnostic: show book override config ---
        book_cfg = self.config
        console.print(
            f"[dim]Book override: enabled={book_cfg.poly_book_enabled} | "
            f"exit_depth={book_cfg.poly_book_exit_override_depth} | "
            f"exit_imbalance={book_cfg.poly_book_exit_override_imbalance} | "
            f"entry_depth={book_cfg.poly_book_min_exit_depth} | "
            f"imbalance_veto={book_cfg.poly_book_imbalance_veto}[/dim]"
        )
        # Also check strategy's config reference matches
        strat_enabled = self.strategy.config.poly_book_enabled
        if strat_enabled != book_cfg.poly_book_enabled:
            console.print(
                f"[bold red]CONFIG MISMATCH: runner.config.poly_book_enabled={book_cfg.poly_book_enabled} "
                f"vs strategy.config.poly_book_enabled={strat_enabled}[/bold red]"
            )
        logger.info(
            f"Book override config: poly_book_enabled={book_cfg.poly_book_enabled}, "
            f"exit_override_depth={book_cfg.poly_book_exit_override_depth}, "
            f"exit_override_imbalance={book_cfg.poly_book_exit_override_imbalance}"
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

        # Initial market load — always use _quick_sync for crypto 5-min markets.
        # The generic indexer.sync() sorts by volume and misses low-volume
        # short-duration markets. _quick_sync sorts by endDate ASC with
        # end_date_min=now, so the currently-live window is always first.
        console.print("[dim]Loading markets...[/dim]")
        try:
            fetched = await self._quick_sync()
            if fetched:
                await self._refresh_markets(prefetched=fetched)
        except Exception as e:
            console.print(f"[yellow]Quick sync failed ({e}), trying DB...[/yellow]")
            await self._refresh_markets()

        if self._total_markets == 0:
            console.print("[yellow]No matching markets found — retrying API...[/yellow]")
            try:
                fetched = await self._quick_sync()
                if fetched:
                    await self._refresh_markets(prefetched=fetched)
            except Exception as e:
                console.print(f"[yellow]Retry failed ({e})[/yellow]")

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

        # --- Load persistent price context from DB ---
        # On startup, load the last 30 min of price snapshots so the bot
        # immediately knows the macro trend without waiting for 5 min of
        # live data. This is the "persistence across restarts" feature.
        try:
            for sym in self.agg_feed.symbols:
                rows = await self.db.get_micro_price_context(sym, minutes=30)
                if rows:
                    micro = self.agg_feed.micro.get(sym)
                    if micro:
                        micro.price_history = [(r["price"], r["logged_at"].timestamp()) for r in rows]
                        latest = rows[-1]
                        oldest = rows[0]
                        trend_pct = (latest["price"] - oldest["price"]) / oldest["price"] if oldest["price"] > 0 else 0
                        console.print(
                            f"[dim]Loaded {len(rows)} price snapshots for {sym.upper()} "
                            f"(last {len(rows) * 30}s) — trend: {trend_pct:+.3%}[/dim]"
                        )
                else:
                    console.print(f"[dim]No price history for {sym.upper()} — starting fresh[/dim]")
        except Exception as e:
            console.print(f"[yellow]Failed to load price context: {e}[/yellow]")

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
            asyncio.create_task(self._price_log_loop()),
            asyncio.create_task(self._config_refresh_loop()),
            asyncio.create_task(self._research_flush_loop()),
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
                    # Prune expired windows from memory — don't touch DB.
                    # If the CURRENT (first) window expired, delegate to
                    # _hop_window so the banner, flow reset, cooldown, and
                    # WS re-subscribe all happen in one place. Only prune
                    # non-current expired windows silently here.
                    now = datetime.now(timezone.utc)
                    for symbol in list(self.updown_markets.keys()):
                        markets = self.updown_markets[symbol]
                        if not markets:
                            del self.updown_markets[symbol]
                            continue

                        current = markets[0][0]
                        if current.end_date and current.end_date <= now:
                            # Current window expired — use full hop logic
                            await self._hop_window(symbol, now)
                            continue

                        # Only prune future expired windows (shouldn't happen
                        # often, but defensive)
                        live = [
                            (m, p) for m, p in markets
                            if m.end_date and m.end_date > now
                        ]
                        if live:
                            self.updown_markets[symbol] = live
                        else:
                            del self.updown_markets[symbol]

                    self._total_markets = sum(
                        len(v) for v in self.updown_markets.values()
                    )

                    # If we're running low on windows, quick_sync to refill
                    if self._total_markets <= 3 and not self._sync_lock.locked():
                        try:
                            async with self._sync_lock:
                                fetched = await self._quick_sync()
                            if fetched:
                                await self._refresh_markets(prefetched=fetched)
                        except Exception as e:
                            logger.warning(f"Quick sync failed: {e}")
                else:
                    # Unfiltered mode — still use _quick_sync for accuracy
                    try:
                        fetched = await self._quick_sync()
                        if fetched:
                            await self._refresh_markets(prefetched=fetched)
                        else:
                            await self._refresh_markets()
                    except Exception as e:
                        logger.warning(f"Sync failed: {e}")
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
            self._position_info.pop(cid, None)
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
                # Mark the window start price so price_change_pct works.
                # This lets the strategy know how far BTC has moved from
                # the open — critical for the price-to-beat filter.
                if micro.current_price > 0:
                    micro.start_window(micro.current_price)
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
            # No next window pre-loaded — need to fetch from API
            console.print("[yellow]Window expired, no next window cached — refreshing...[/yellow]")
            self._trades_this_window = 0
            self._last_hop_time = time.time()

            # Use _quick_sync (targeted, endDate ASC) instead of generic
            # indexer.sync which misses low-volume 5-min crypto markets.
            if not self._sync_lock.locked():
                try:
                    async with self._sync_lock:
                        fetched = await self._quick_sync()
                    if fetched:
                        await self._refresh_markets(prefetched=fetched)
                except Exception as e:
                    logger.warning(f"Hop sync failed: {e}")

            # Fallback to DB if quick_sync didn't produce results
            if self._total_markets == 0:
                await self._refresh_markets()

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

        # Don't evaluate faster than needed — batch evals per symbol.
        # Per-symbol counter so adding ETH doesn't starve BTC evals.
        sym_count = self._eval_count_per_symbol.get(symbol, 0) + 1
        self._eval_count_per_symbol[symbol] = sym_count
        if sym_count % 5 != 0:
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
                # Mark window start price for price-to-beat filter
                if micro.current_price > 0:
                    micro.start_window(micro.current_price)
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

        # --- Trailing stop: update high water mark on every tick ---
        if current_pos and market.condition_id in self._position_info:
            info = self._position_info[market.condition_id]
            our_price = market.yes_price if current_pos == "yes" else market.no_price
            if our_price > info["hwm"]:
                info["hwm"] = our_price

        # --- Min hold time: don't exit within 5s of entry ---
        # The CLOB needs time to settle the token balance after a buy.
        # Exiting too soon causes stale balance reads (e.g., 0.2 instead of 8.2).
        # Force exit (<8s remaining) bypasses this check.
        if current_pos and market.condition_id in self._position_info:
            entry_t = self._position_info[market.condition_id].get("entry_time", 0)
            hold_secs = time.time() - entry_t
            if hold_secs < 5.0 and remaining > self.config.force_exit_seconds:
                return  # Too soon after entry — let CLOB settle

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

        # Look up Polymarket order book intelligence if enabled.
        # The poly_feed already maintains live book snapshots via WS —
        # we just need to run analyze_book() on the relevant token.
        book_intel = None
        if self.config.poly_book_enabled and market.clob_token_ids:
            book_intel = self._get_book_intel(market)
            if current_pos and not book_intel:
                console.print(
                    f"[dim yellow]BOOK DEBUG: no book data for "
                    f"{market.condition_id[:8]} (poly_books={len(self.poly_feed.books)})[/dim yellow]"
                )

        # Pass trailing stop info to strategy if we have a position
        entry_price = None
        high_water_mark = None
        if current_pos and market.condition_id in self._position_info:
            info = self._position_info[market.condition_id]
            entry_price = info["entry_price"]
            high_water_mark = info["hwm"]

        opp = self.strategy.evaluate(
            market=market,
            micro=micro,
            seconds_remaining=remaining,
            current_position=current_pos,
            book_intel=book_intel,
            entry_price=entry_price,
            high_water_mark=high_water_mark,
        )

        # --- Research pipeline: periodic + event-driven snapshots ---
        # Estimate window duration from the market (15m = 900s, 5m = 300s)
        window_duration = 900.0  # default 15m
        if market.end_date and micro.window_start_time > 0:
            window_duration = max(60.0, (market.end_date - datetime.fromtimestamp(micro.window_start_time, tz=timezone.utc)).total_seconds())

        try:
            # Periodic snapshots every ~2 seconds
            if self.research.should_log_periodic(symbol, interval=2.0):
                snap = self.research.build_snapshot(
                    micro=micro, market=market,
                    seconds_remaining=remaining,
                    window_duration=window_duration,
                    current_position=current_pos or "",
                    entry_price=entry_price or 0.0,
                    high_water_mark=high_water_mark or 0.0,
                    event_type="periodic",
                )
                # Candidate detection: momentum near threshold but not crossing
                abs_mom = abs(micro.momentum_signal)
                threshold = self.config.entry_threshold
                direction = "YES" if micro.momentum_signal > 0 else "NO"
                if not current_pos and abs_mom >= threshold * 0.80 and abs_mom < threshold:
                    await self.research.log_candidate(snap, distance_to_threshold=threshold - abs_mom)
                    if self.verbose:
                        console.print(
                            f"[dim cyan]CANDIDATE: {direction} Mom {micro.momentum_signal:+.2f} "
                            f"({abs_mom/threshold:.0%} of threshold)[/dim cyan]"
                        )
                elif not current_pos and opp is None and self.strategy.last_no_trade_reason:
                    # Signal was above threshold but got blocked by a filter
                    reason = self.strategy.last_no_trade_reason
                    if reason != NoTradeReason.BELOW_THRESHOLD:
                        # Build context string based on the reason
                        ctx = ""
                        if reason == NoTradeReason.FAILED_PERSISTENCE:
                            start = self.strategy._entry_signal_start.get(symbol)
                            if start:
                                elapsed = time.time() - start
                                ctx = f" ({elapsed:.1f}s/{self.config.entry_persistence_seconds:.1f}s)"
                        elif reason == NoTradeReason.TREND_VETO:
                            ctx = f" (5m: {micro.trend_5m:+.2%})"
                        elif reason == NoTradeReason.ACCELERATION:
                            detail = self.strategy._last_accel_detail
                            ctx = f" ({detail})" if detail else ""
                        elif reason == NoTradeReason.PRICE_TO_BEAT:
                            ctx = f" (window: {micro.price_change_pct:+.3%})"
                        elif reason == NoTradeReason.PRICE_BAND:
                            mp = market.yes_price if micro.momentum_signal > 0 else market.no_price
                            ctx = f" (mkt: {mp:.2f})"
                        elif reason == NoTradeReason.DEAD_MARKET:
                            ctx = f" (YES: {market.yes_price:.2f})"
                        elif reason == NoTradeReason.SPARSE_DATA:
                            ctx = f" ({micro.flow_15s.total_count} trades)"
                        elif reason == NoTradeReason.LOW_VOL:
                            int_30 = micro.flow_30s.trade_intensity if micro.flow_30s.is_active else 0.0
                            ctx = f" (int:{int_30:.1f}tps, Δ:{micro.price_change_pct:+.4%})"
                        elif reason == NoTradeReason.HIGH_INTENSITY:
                            int_30 = micro.flow_30s.trade_intensity if micro.flow_30s.is_active else 0.0
                            ctx = f" ({int_30:.0f}tps > {self.config.high_intensity_max_tps:.0f} cap)"
                        elif reason == NoTradeReason.BELOW_THRESHOLD:
                            thr_detail = getattr(self.strategy, '_last_threshold_detail', '')
                            if thr_detail:
                                ctx = f" (thr: {thr_detail})"

                        # Always append threshold info for non-threshold blocks too
                        thr_str = ""
                        thr_detail = getattr(self.strategy, '_last_threshold_detail', '')
                        if thr_detail and reason != NoTradeReason.BELOW_THRESHOLD:
                            thr_str = f" | thr: {thr_detail}"

                        if not self.quiet:
                            console.print(
                                f"[dim yellow]BLOCKED: {direction} Mom {micro.momentum_signal:+.2f} "
                                f"→ {reason.value}{ctx}{thr_str}[/dim yellow]"
                            )
                        await self.research.log_no_trade(snap, reason)
                    else:
                        await self.research.log_snapshot(snap)
                else:
                    await self.research.log_snapshot(snap)
        except Exception as e:
            logger.debug(f"Research snapshot failed (non-fatal): {e}")

        if opp:
            await self._handle_opportunity(opp, micro=micro, market=market,
                                            seconds_remaining=remaining,
                                            window_duration=window_duration,
                                            current_pos=current_pos,
                                            entry_price_val=entry_price,
                                            high_water_mark_val=high_water_mark)

    def _get_book_intel(self, market: Market) -> Optional[dict[str, BookIntelligence]]:
        """Get BookIntelligence for both YES and NO tokens of a market.

        Uses the live order book maintained by the Polymarket WebSocket.
        Returns None if no book data is available yet.

        Returns dict with "yes" and/or "no" keys.
        """
        if not market.clob_token_ids or len(market.clob_token_ids) < 2:
            return None

        result: dict[str, BookIntelligence] = {}
        for idx, side in enumerate(["yes", "no"]):
            token_id = market.clob_token_ids[idx]
            book = self.poly_feed.get_book(token_id)
            if book and (book.bids or book.asks):
                try:
                    result[side] = analyze_book(book)
                except Exception as e:
                    logger.debug(f"Book analysis failed for {side} {token_id}: {e}")

        return result if result else None

    # ------------------------------------------------------------------
    # Opportunity handling
    # ------------------------------------------------------------------

    async def _handle_opportunity(
        self, opp: MicroOpportunity,
        micro: MicroStructure | None = None,
        market: Market | None = None,
        seconds_remaining: float = 0.0,
        window_duration: float = 900.0,
        current_pos: str | None = None,
        entry_price_val: float | None = None,
        high_water_mark_val: float | None = None,
    ):
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
            book_str = ""
            if self.config.poly_book_enabled and opp.poly_book_imbalance != 0:
                book_str = f" | Book: {opp.poly_book_imbalance:+.2f}"
            # Show threshold breakdown
            thr_detail = getattr(self.strategy, '_last_threshold_detail', '')
            thr_str = f" | Thr: {thr_detail}" if thr_detail else ""
            console.print(
                f"[bold {action_color}]MICRO [{action_str}][/bold {action_color}] "
                f"{opp.symbol.replace('usdt','').upper()} "
                f"| Mom: {opp.momentum:+.2f} "
                f"| OFI: {opp.ofi_5s:+.2f}/{opp.ofi_15s:+.2f} "
                f"| Mkt: {opp.market_price:.2f} ({price_source}) "
                f"| ${opp.binance_price:,.2f} "
                f"| {opp.seconds_remaining:.0f}s left"
                f"{book_str}{thr_str}"
            )

        # --- Research pipeline: log trade event with attribution ---
        if micro:
            try:
                snap = self.research.build_snapshot(
                    micro=micro, market=opp.market,
                    seconds_remaining=opp.seconds_remaining,
                    window_duration=window_duration,
                    current_position=current_pos or "",
                    entry_price=entry_price_val or 0.0,
                    high_water_mark=high_water_mark_val or 0.0,
                    event_type="trade",
                )
                # Compute intensity component for attribution
                int_5 = micro.flow_5s.trade_intensity if micro.flow_5s.is_active else 0.0
                int_30 = micro.flow_30s.trade_intensity if micro.flow_30s.is_active else 0.0
                if int_30 > 0:
                    i_ratio = int_5 / int_30
                    i_signal = max(-1.0, min(1.0, (i_ratio - 1.0)))
                else:
                    i_signal = 0.0
                ofi_5_val = micro.flow_5s.ofi if micro.flow_5s.is_active else 0.0
                d_dir = 1.0 if ofi_5_val > 0 else (-1.0 if ofi_5_val < 0 else 0.0)
                i_comp = i_signal * d_dir

                attr = compute_attribution(
                    ofi_5s=opp.ofi_5s,
                    ofi_15s=opp.ofi_15s,
                    vwap_drift_scaled=snap.vwap_drift_scaled,
                    intensity_component=i_comp,
                    dampener_factor=snap.dampener_factor,
                    weight_ofi_5s=micro.weight_ofi_5s,
                    weight_ofi_15s=micro.weight_ofi_15s,
                    weight_vwap_drift=micro.weight_vwap_drift,
                    weight_intensity=micro.weight_intensity,
                    trade_side=opp.side.value.lower(),
                )
                await self.research.log_trade(
                    snap=snap,
                    trade_side=opp.side.value.lower(),
                    trade_action=action.value,
                    attribution=attr,
                    exit_reason=getattr(opp, 'exit_reason', ''),
                )
            except Exception as e:
                logger.debug(f"Research trade log failed (non-fatal): {e}")

        if self.dry_run:
            # Update virtual position tracking even in dry run
            dry_token = opp.market.yes_token_id if opp.side == Side.YES else opp.market.no_token_id
            if action == MicroAction.EXIT:
                self._positions.pop(cid, None)
                self._position_info.pop(cid, None)
            elif action == MicroAction.FLIP_YES:
                self._positions[cid] = "yes"
                self._position_info[cid] = {"entry_price": opp.market_price, "hwm": opp.market_price, "token_id": dry_token or "", "size": 10.0}
            elif action == MicroAction.FLIP_NO:
                self._positions[cid] = "no"
                self._position_info[cid] = {"entry_price": opp.market_price, "hwm": opp.market_price, "token_id": dry_token or "", "size": 10.0}
            elif action == MicroAction.BUY_YES:
                self._positions[cid] = "yes"
                self._position_info[cid] = {"entry_price": opp.market_price, "hwm": opp.market_price, "token_id": dry_token or "", "size": 10.0}
            elif action == MicroAction.BUY_NO:
                self._positions[cid] = "no"
                self._position_info[cid] = {"entry_price": opp.market_price, "hwm": opp.market_price, "token_id": dry_token or "", "size": 10.0}

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

    async def _get_fill_info(self, order_id: str, token_id: str, fallback_price: float, fallback_size: float = 0) -> tuple[float, float]:
        """Fetch actual fill price AND size from CLOB API after a FOK order fills.

        FOK orders can fill at a BETTER price than we asked for, and the actual
        share count can differ from our USD/price estimate due to CLOB rounding
        and multi-level fills. Using calculated shares causes "not enough balance"
        errors on exit.

        Returns (fill_price, fill_size). fill_size=0 means we couldn't get it
        (caller should use its own estimate).

        Strategy: ALWAYS prefer trade-level fill data for prices (most accurate).
        The order-level `average_price` field often returns the limit/floor price
        rather than the actual execution price. Use order-level only for size
        as a fallback.
        """
        fill_price = fallback_price
        fill_size = 0.0
        order_size = 0.0  # size from order-level data (fallback)

        try:
            await asyncio.sleep(0.5)  # Give CLOB time to settle fills

            # Step 1: get_order for size_matched (reliable for size, NOT for price)
            try:
                order = self.client.get_order(order_id)
                if order:
                    matched = order.get("size_matched") or order.get("matched_amount")
                    if matched:
                        order_size = float(matched)
            except Exception:
                pass

            # Step 2: search trades for exact order ID match — this has REAL fill prices
            trades = self.client.get_trades(asset_id=token_id)
            if trades:
                matched_trades = [
                    t for t in trades
                    if t.get("taker_order_id") == order_id or t.get("order_id") == order_id
                ]
                if matched_trades:
                    # Sum all fills for this order
                    total_size = sum(float(t.get("size", 0)) for t in matched_trades)
                    # Weighted average price across fills
                    total_value = sum(float(t.get("size", 0)) * float(t.get("price", fallback_price)) for t in matched_trades)
                    if total_size > 0:
                        fill_price = total_value / total_size
                        fill_size = total_size
                    else:
                        # Single trade match — use its price
                        fill_price = float(matched_trades[0].get("price", fallback_price))
                        fill_size = order_size  # fall back to order-level size
                    size_str = f", {fill_size:.2f} shares" if fill_size > 0 else ""
                    if not self.quiet:
                        console.print(f"  [dim]Actual fill price: ${fill_price:.3f} (asked ${fallback_price:.3f}){size_str}[/dim]")
                    return fill_price, fill_size

            # No trade matches found — fall back to order-level data
            if order_size > 0:
                # We have size from get_order but no trade-level price.
                # Use order's average_price if available, but flag it as uncertain.
                try:
                    order = self.client.get_order(order_id)
                    if order:
                        avg = order.get("average_price") or order.get("associate_trades_avg_price")
                        if avg:
                            fill_price = float(avg)
                except Exception:
                    pass
                fill_size = order_size
                if not self.quiet:
                    marker = " (order-level)" if fill_price == fallback_price else ""
                    console.print(f"  [dim]Actual fill price: ${fill_price:.3f} (asked ${fallback_price:.3f}), {fill_size:.2f} shares{marker}[/dim]")
                return fill_price, fill_size

            # No match found — use fallback
            if not self.quiet:
                console.print(f"  [dim]Fill price: ${fallback_price:.3f} (no CLOB match, using ask)[/dim]")
        except Exception as e:
            logger.warning(f"Could not fetch fill info: {e}")
        return fill_price, fill_size

    async def _execute_micro_trade(
        self,
        opp: MicroOpportunity,
        size_usd: float,
    ):
        """Execute a micro trade."""
        cid = opp.market.condition_id
        action = opp.action

        # For EXIT and FLIP, we'd need to sell the existing position first
        if action == MicroAction.EXIT:
            # Sell existing position
            success = await self._close_position(self.engine, opp)
            if success:
                self._positions.pop(cid, None)
                self._position_info.pop(cid, None)
                self._trades_this_window += 1
                self._total_trades += 1
                self._last_trade_log[cid] = time.time()
            return

        if action in (MicroAction.FLIP_YES, MicroAction.FLIP_NO):
            # Close existing position first
            await self._close_position(self.engine, opp)
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
                # Get actual fill price AND share count from CLOB API.
                # FOK can fill at better price than asked, and the actual share
                # count can differ from USD/price due to CLOB rounding and
                # multi-level fills. Using calculated shares causes "not enough
                # balance" errors on exit.
                actual_entry, clob_size = await self._get_fill_info(poly_order_id, token_id, entry_price, size)
                if clob_size > 0:
                    raw_size = clob_size
                else:
                    raw_size = round(size_usd / actual_entry, 2) if actual_entry > 0 else size

                # Apply 2% taker fee haircut — Polymarket deducts fees from
                # token balance, so we receive ~98% of the filled shares.
                # Without this, local tracking is always inflated and the sell
                # hits "not enough balance" every time.
                fee_adjusted = raw_size * 0.98
                actual_size = int(fee_adjusted * 100) / 100  # truncate to 2dp

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
                    # Snapshot active config + signal data for backtesting
                    config_snap = {
                        "entry_threshold": self.config.entry_threshold,
                        "counter_trend_threshold": self.config.counter_trend_threshold,
                        "exit_threshold": self.config.exit_threshold,
                        "hold_threshold": self.config.hold_threshold,
                        "counter_trend_exit_threshold": self.config.counter_trend_exit_threshold,
                        "min_entry_price": self.config.min_entry_price,
                        "max_entry_price": self.config.max_entry_price,
                        "entry_persistence_enabled": self.config.entry_persistence_enabled,
                        "entry_persistence_seconds": self.config.entry_persistence_seconds,
                        "trailing_stop_enabled": self.config.trailing_stop_enabled,
                        "trailing_stop_pct": self.config.trailing_stop_pct,
                        "take_profit_enabled": self.config.take_profit_enabled,
                        "take_profit_price": self.config.take_profit_price,
                        "exit_slippage": self.config.exit_slippage,
                        "entry_slippage": self.config.entry_slippage,
                        "trade_cooldown": self.config.trade_cooldown,
                        "weight_ofi_5s": self.config.weight_ofi_5s,
                        "weight_ofi_15s": self.config.weight_ofi_15s,
                        "weight_vwap_drift": self.config.weight_vwap_drift,
                        "weight_intensity": self.config.weight_intensity,
                        "vwap_drift_scale": self.config.vwap_drift_scale,
                        "dampener_agree_factor": self.config.dampener_agree_factor,
                        "dampener_disagree_factor": self.config.dampener_disagree_factor,
                        "dampener_flat_factor": self.config.dampener_flat_factor,
                        "dampener_price_deadzone": self.config.dampener_price_deadzone,
                        "high_intensity_block_enabled": self.config.high_intensity_block_enabled,
                        "high_intensity_max_tps": self.config.high_intensity_max_tps,
                        "chop_filter_enabled": self.config.chop_filter_enabled,
                        "chop_threshold": self.config.chop_threshold,
                        "acceleration_enabled": self.config.acceleration_enabled,
                        "acceleration_tolerance": self.config.acceleration_tolerance,
                    }
                    # Compute bias direction at entry for performance tracking
                    bias_direction = "NEUTRAL"
                    bias_adj = self.strategy._last_bias_adjustment
                    if self.config.adaptive_bias_enabled and abs(bias_adj) > 0.001:
                        bias_direction = "FAVORABLE" if bias_adj < 0 else "UNFAVORABLE"
                    signal_snap = {
                        "momentum": round(opp.momentum, 4),
                        "confidence": round(opp.confidence, 4),
                        "ofi_5s": round(opp.ofi_5s, 4),
                        "ofi_15s": round(opp.ofi_15s, 4),
                        "vwap_drift": round(opp.vwap_drift, 4),
                        "trade_intensity": round(opp.trade_intensity, 2),
                        "binance_price": opp.binance_price,
                        "market_price": opp.market_price,
                        "price_change_pct": round(opp.price_change_pct, 6),
                        "seconds_remaining": round(opp.seconds_remaining, 1),
                        "bias_direction": bias_direction,
                        "bias_adjustment": round(bias_adj, 4),
                        "chop_index": round(self.agg_feed.micro[opp.symbol].chop_index, 2) if opp.symbol in getattr(self.agg_feed, 'micro', {}) else 0,
                        "chop_boost": round(getattr(self.strategy, '_last_chop_boost', 0.0), 4),
                        "effective_threshold": round(getattr(self.strategy, '_last_effective_threshold', 0.0), 4),
                        "threshold_detail": getattr(self.strategy, '_last_threshold_detail', ''),
                    }
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
                        "config_snapshot": config_snap,
                        "signal_data": signal_snap,
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
                self._position_info[cid] = {
                    "entry_price": actual_entry,
                    "hwm": actual_entry,
                    "token_id": token_id,
                    "size": actual_size,
                    "entry_time": time.time(),  # Track when we entered for min-hold
                }
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
                # Apply trade cooldown so we don't spam retries on an empty book.
                self._last_trade_log[cid] = time.time()
                if not self.quiet:
                    console.print(f"[dim]  FOK rejected @ ${entry_price:.2f} (mkt ${price:.2f}) — no liquidity, cooling down[/dim]")
            else:
                console.print(f"[red]  Execution error: {e}[/red]")
                logger.error(f"Micro execution failed: {e}", exc_info=True)

    async def _close_position(self, engine, opp: MicroOpportunity) -> bool:
        """Close an existing position by placing a FOK SELL order.

        FAST PATH: Uses locally-tracked position info (token_id, size, entry_price)
        from the buy fill instead of querying DB and CLOB API. Skips allowance
        refresh (already approved at startup). This cuts exit latency from ~1s
        (3 API round-trips) to ~200ms (just the sell order).

        Falls back to slow path (API queries) if local tracking is missing.
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

        # --- FAST EXIT: use locally-tracked data instead of API calls ---
        info = self._position_info.get(cid, {})
        entry_price = info.get("entry_price", 0.0)
        local_size = info.get("size", 0)
        local_token_id = info.get("token_id", "")

        # Use wider exit slippage — price moves fast, tight slippage = FOK rejections.
        # ESCALATION: each failed FOK attempt adds 3 cents to slippage so we don't
        # loop forever while the price drops. After 3 failures the floor is 14 cents
        # below market, which practically guarantees a fill.
        attempts = self._exit_attempts.get(cid, 0)
        exit_slip = self.config.exit_slippage + (attempts * 0.03)
        sell_price = max(0.01, round(price - exit_slip, 2))

        # Log book for visibility but ALWAYS use floor price for the FOK.
        # FOK fills at the best available bid anyway — if bids are at $0.40
        # and we ask for $0.36, we get $0.40. But if those $0.40 bids get
        # pulled (which happened 9x in a row), asking $0.36 still fills at
        # $0.39/$0.38 instead of rejecting. Never try to be clever with the
        # sell price — just set the floor and let the CLOB give us the best fill.
        book = self.poly_feed.books.get(token_id)
        if book and book.bids and not self.quiet:
            top_bids = book.bids[:3]
            bid_str = " | ".join(f"${b.price:.2f}×{b.size:.0f}" for b in top_bids)
            console.print(f"  [dim]Book bids: {bid_str}[/dim]")

        effective_sell = sell_price  # Always use floor — FOK fills at best available

        # Determine share count — prefer local tracking, fall back to CLOB API
        size = 0
        if local_size > 0 and local_token_id == token_id:
            # FAST: use locally-tracked size from buy fill
            size = int(float(local_size) * 100) / 100  # truncate to 2dp
            if not self.quiet:
                console.print(f"  [dim]Fast exit: {size} shares (local tracking)[/dim]")
        else:
            # SLOW FALLBACK: query CLOB API for actual token balance
            if not self.quiet:
                console.print(f"  [dim]Slow exit: querying CLOB for balance...[/dim]")
            try:
                bal = self.client.get_token_balance(token_id)
                if isinstance(bal, dict):
                    raw_bal = bal.get("balance", 0)
                else:
                    raw_bal = bal
                raw_str = str(raw_bal)
                logger.info(f"Slow exit balance raw: {raw_str} (type={type(raw_bal).__name__}, full={bal})")
                raw_float = float(raw_str)
                # Try both interpretations
                as_micro = int(raw_float / 1e6 * 100) / 100  # e.g. 15625000 → 15.62
                as_direct = int(raw_float * 100) / 100         # e.g. 15.625 → 15.62
                # Pick the one that makes sense (< 10000 shares for a $5-$20 trade)
                if as_micro > 0 and as_micro < 10000:
                    size = as_micro
                elif as_direct > 0 and as_direct < 10000:
                    size = as_direct
                else:
                    size = as_direct if as_direct > 0 else as_micro
            except Exception as e:
                logger.warning(f"Token balance lookup failed: {e}")
                # Last resort: try DB
                try:
                    positions = await self.db.get_open_positions()
                    for p in positions:
                        if p.get("market_id") == cid and p.get("side", "").lower() == current_pos:
                            size = int(float(p.get("size", 0)) * 100) / 100
                            if not entry_price:
                                entry_price = p.get("entry_price", 0)
                            break
                except Exception:
                    pass

        if size <= 0:
            logger.info(f"No token balance for {token_id}, clearing tracking")
            return True

        # NO allowance refresh — already approved at startup. Skipping saves ~300ms.

        # Single FOK attempt — fire immediately
        total_sold = 0.0
        total_proceeds = 0.0
        remaining = size

        try:
            chunk = int(remaining * 100) / 100
            result = self.client.place_fok_order(
                token_id=token_id,
                side="SELL",
                amount=chunk,
                price=effective_sell,
            )
            order_id = result.get("orderID", result.get("id", ""))
            total_sold = chunk
            remaining = 0

            # Get actual fill price — but don't block on it
            actual_sell, _ = await self._get_fill_info(order_id, token_id, effective_sell)
            total_proceeds = chunk * actual_sell

            if not self.quiet:
                console.print(
                    f"[yellow]  SOLD {current_pos.upper()} {chunk:.1f} @ ${actual_sell:.2f} "
                    f"— Order: {order_id}[/yellow]"
                )

        except Exception as e:
            err_msg = str(e)
            if "fully filled" in err_msg or "FOK" in err_msg:
                self._exit_attempts[cid] = self._exit_attempts.get(cid, 0) + 1
                if not self.quiet:
                    console.print(f"[yellow]  FOK sell rejected @ ${effective_sell:.2f} (attempt {self._exit_attempts[cid]}) — will retry next tick[/yellow]")
            elif "not enough balance" in err_msg:
                # Local size was wrong — re-query CLOB and retry immediately
                # instead of waiting for next tick. The CLOB has settled by now
                # (exit happens seconds after buy).
                console.print(f"[yellow]  Not enough balance ({size} local) — re-querying CLOB...[/yellow]")
                try:
                    bal = self.client.get_token_balance(token_id)
                    raw_bal = bal.get("balance", 0) if isinstance(bal, dict) else bal
                    raw_float = float(str(raw_bal))
                    # Try multiple interpretations, pick closest to our estimate
                    best_size = 0
                    for divisor in [1e6, 1e4, 1e3, 1]:
                        val = int(raw_float / divisor * 100) / 100
                        if val > 0 and (best_size == 0 or abs(val - size) < abs(best_size - size)):
                            best_size = val
                    if best_size > 0 and best_size < size:
                        console.print(f"  [dim]CLOB says {best_size} shares — retrying sell...[/dim]")
                        retry_chunk = int(best_size * 100) / 100
                        retry_result = self.client.place_fok_order(
                            token_id=token_id,
                            side="SELL",
                            amount=retry_chunk,
                            price=effective_sell,
                        )
                        retry_order_id = retry_result.get("orderID", retry_result.get("id", ""))
                        actual_sell, _ = await self._get_fill_info(retry_order_id, token_id, effective_sell)
                        total_sold = retry_chunk
                        total_proceeds = retry_chunk * actual_sell
                        remaining = 0
                        if not self.quiet:
                            console.print(
                                f"[yellow]  SOLD {current_pos.upper()} {retry_chunk:.1f} @ ${actual_sell:.2f} "
                                f"— Order: {retry_order_id}[/yellow]"
                            )
                        # Update local tracking with real size for future reference
                        if cid in self._position_info:
                            self._position_info[cid]["size"] = best_size
                    else:
                        console.print(f"  [dim]CLOB balance: {raw_bal} — clearing local tracking[/dim]")
                        if cid in self._position_info:
                            self._position_info[cid]["size"] = 0
                except Exception as retry_err:
                    console.print(f"[red]  Retry failed: {retry_err} — will retry next tick[/red]")
                    # DON'T zero out size — keep the CLOB-reported size so next
                    # attempt uses fast path instead of slow path (+400ms latency).
                    if cid in self._position_info and best_size > 0:
                        self._position_info[cid]["size"] = best_size
            else:
                console.print(f"[red]  Sell failed: {e}[/red]")
                logger.error(f"Failed to close {current_pos} position on {cid}: {e}")

        if total_sold > 0:
            fill_price = total_proceeds / total_sold if total_sold > 0 else sell_price
            self._exit_attempts.pop(cid, None)  # Reset escalation on success

            gross_pnl = (fill_price - entry_price) * total_sold if entry_price > 0 else 0
            try:
                await self.db.close_trade_by_market(
                    market_id=cid,
                    exit_price=fill_price,
                    pnl=gross_pnl,
                    exit_reason=getattr(opp, 'exit_reason', ''),
                )
            except Exception as e:
                logger.warning(f"Trade close recording failed (non-fatal): {e}")

            if remaining <= 0.5:
                try:
                    await self.db.remove_position(cid, token_id, current_pos)
                except Exception as e:
                    logger.warning(f"DB position update failed (non-fatal): {e}")
            else:
                logger.warning(f"Partial sell: {total_sold:.1f}/{size:.1f} filled, {remaining:.1f} remaining")

            if not self.quiet and entry_price > 0:
                pnl_style = "green" if gross_pnl >= 0 else "red"
                sold_label = f"{total_sold:.1f}/{size:.1f}" if remaining > 0.5 else f"{total_sold:.1f}"
                console.print(
                    f"  [{pnl_style}]P&L: ${gross_pnl:+.2f} "
                    f"(entry ${entry_price:.2f} → exit ${fill_price:.2f}, {sold_label} shares)"
                    f"  ✓ filled[/{pnl_style}]"
                )

            if remaining <= 0.5:
                return True
            else:
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

                trend = micro.trend_5m
                trend_str = f" T5m:{trend:+.2%}" if abs(trend) > 0.0001 else ""

                # 30m adaptive bias indicator
                bias_str = ""
                if self.config.adaptive_bias_enabled:
                    t30 = micro.trend_lookback(self.config.adaptive_bias_lookback_minutes)
                    if abs(t30) >= self.config.adaptive_bias_min_move:
                        bias_dir = "BEAR" if t30 < 0 else "BULL"
                        half = self.config.adaptive_bias_spread / 2.0
                        bias_str = f" Bias:{bias_dir}(Y{'+'if t30<0 else '-'}{half:.2f}/N{'-'if t30<0 else '+'}{half:.2f})"
                    else:
                        bias_str = " Bias:NEUTRAL"

                # Chop index indicator — always show so you can monitor before enabling
                chop_str = ""
                chop = micro.chop_index
                if chop > 0:
                    if chop > self.config.chop_threshold:
                        # Above threshold — would block/boost if filter enabled
                        if self.config.chop_filter_enabled:
                            chop_str = f" CHOP:{chop:.1f}"
                        else:
                            chop_str = f" chop:{chop:.1f}"  # lowercase = observing only
                    elif chop > self.config.chop_threshold * 0.7:
                        # Approaching threshold — show in dim
                        chop_str = f" chop:{chop:.1f}"

                # Threshold breakdown — entry always, exit only when in position
                thr_str = ""
                entry_thr = getattr(self.strategy, '_last_threshold_detail', '')
                exit_thr = getattr(self.strategy, '_last_exit_threshold_detail', '')
                if entry_thr:
                    thr_str = f" Entry:{entry_thr}"
                if exit_thr and len(self._positions) > 0:
                    thr_str += f" {exit_thr}"

                micro_lines.append(
                    f"{sym_short}: ${price:,.2f} {arrow} "
                    f"Mom:{momentum:+.2f} OFI:{ofi:+.2f} "
                    f"{intensity:.0f}tps{trend_str}{bias_str}{chop_str}{thr_str}"
                )

            n_pos = len(self._positions)
            pos_str = f"Pos: {n_pos}" if n_pos > 0 else "Flat"
            poly_str = f"live/{self._ws_price_updates}" if self._poly_connected else "api"
            warmup_str = " | [yellow]WARMING UP — waiting for fresh window[/yellow]" if self._waiting_for_fresh_window else ""

            # Research pipeline stats
            r = self.research
            research_str = f" | Snaps: {r._total_snapshots} (T:{r._total_trades} C:{r._total_candidates})"

            console.print(
                f"\n[bold dim]── Micro Status ── "
                f"{' | '.join(micro_lines) if micro_lines else 'waiting for data'} | "
                f"Mkts: {self._total_markets} | "
                f"{pos_str} | "
                f"Trades: {self._total_trades} (flips: {self._total_flips}) | "
                f"Evals: {self._eval_count} | "
                f"Prices: {poly_str}{research_str}[/bold dim]"
                f"{warmup_str}"
            )

    async def _price_log_loop(self):
        """Periodically log price snapshots to DB for persistent trend context.

        Runs every trend_log_interval seconds (default 30s). Also prunes
        old entries to prevent unbounded growth.

        If the standalone `polyedge price-logger` is already running, it
        will be logging too. That's fine — extra snapshots are harmless
        and the DB deduplicates by timestamp proximity naturally.
        """
        interval = self.config.trend_log_interval
        prune_counter = 0

        while self.running:
            await asyncio.sleep(interval)

            for sym in self.agg_feed.symbols:
                micro = self.agg_feed.get_micro(sym)
                if not micro or micro.current_price <= 0:
                    continue

                try:
                    await self.db.log_micro_price(
                        symbol=sym,
                        price=micro.current_price,
                        ofi_30s=micro.flow_30s.ofi if micro.flow_30s.is_active else 0.0,
                        volume_30s=micro.flow_30s.total_volume,
                        trade_intensity=micro.flow_5s.trade_intensity,
                    )
                except Exception as e:
                    logger.warning(f"Failed to log price for {sym}: {e}")

            # Prune old entries every 10 cycles (~5 min at 30s interval)
            prune_counter += 1
            if prune_counter >= 10:
                prune_counter = 0
                try:
                    await self.db.prune_micro_price_log(keep_minutes=60)
                except Exception as e:
                    logger.warning(f"Failed to prune price log: {e}")

    async def _config_refresh_loop(self):
        """Hot-reload config from DB every 30 seconds.

        Reads polyedge.risk_config and pushes updated values into:
        1. self.settings (the Settings object)
        2. self.config (shortcut to settings.strategies.micro_sniper)
        3. self.strategy.config (same reference)
        4. MicroStructure instances (weights, dampener params)

        This means `polyedge config set ...` takes effect within 30s
        without restarting the bot.
        """
        from polyedge.core.config import apply_db_config

        while self.running:
            await asyncio.sleep(30)
            try:
                # Snapshot ALL config fields so any change triggers a notification
                old_config = {
                    field: getattr(self.config, field)
                    for field in self.config.model_fields
                }

                await apply_db_config(self.settings, self.db)

                # Push updated score-shaping params to MicroStructure instances
                for micro in self.agg_feed.micro.values():
                    micro.weight_ofi_5s = self.config.weight_ofi_5s
                    micro.weight_ofi_15s = self.config.weight_ofi_15s
                    micro.weight_vwap_drift = self.config.weight_vwap_drift
                    micro.weight_intensity = self.config.weight_intensity
                    micro.vwap_drift_scale = self.config.vwap_drift_scale
                    micro.dampener_agree_factor = self.config.dampener_agree_factor
                    micro.dampener_disagree_factor = self.config.dampener_disagree_factor
                    micro.dampener_flat_factor = self.config.dampener_flat_factor
                    micro.dampener_price_deadzone = self.config.dampener_price_deadzone

                # Log any changes
                changes = []
                for key, old_val in old_config.items():
                    new_val = getattr(self.config, key, None)
                    if new_val is not None and new_val != old_val:
                        changes.append(f"{key}: {old_val} → {new_val}")
                if changes:
                    console.print(f"[bold cyan]CONFIG RELOADED: {', '.join(changes)}[/bold cyan]")
            except Exception as e:
                logger.warning(f"Config refresh failed: {e}")

    async def _research_flush_loop(self):
        """Periodically flush research snapshot buffer to DB and prune old data."""
        while self.running:
            try:
                await asyncio.sleep(5.0)
                flushed = await self.research.flush()
                if flushed > 0 and self.verbose:
                    logger.debug(f"Research: flushed {flushed} snapshots to DB")
            except asyncio.CancelledError:
                # Final flush on shutdown
                try:
                    await self.research.flush()
                except Exception:
                    pass
                return
            except Exception as e:
                logger.debug(f"Research flush failed (non-fatal): {e}")

            # Prune old snapshots every ~10 minutes
            if hasattr(self, '_last_prune_time'):
                if time.time() - self._last_prune_time < 600:
                    continue
            self._last_prune_time = time.time()
            try:
                pruned = await self.db.prune_snapshots()
                if pruned > 0 and not self.quiet:
                    console.print(f"[dim]Research: pruned {pruned} old snapshots[/dim]")
            except Exception as e:
                logger.debug(f"Research prune failed (non-fatal): {e}")

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
