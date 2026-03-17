"""Market Maker — post-only limit orders on both sides, capture spread.

Pure spread capture: no directional prediction, no Binance CDF model.
Fair value = Polymarket book midpoint. Defense = Poly book dynamics
(imbalance velocity, whale detection, spread compression).

Works on ANY Polymarket market — crypto up/down windows, political
markets, weather, anything with a bid-ask spread.

Architecture:
- Quote engine: computes bid/ask from fair value + configurable spread
- Quote gating: only quotes when conditions are safe (book data, spread
  wide enough, inventory within limits, book not one-sided)
- Defense: pulls/widens quotes on imbalance velocity spikes and whales
- Inventory manager: tracks YES/NO, skews quotes to rebalance
- Force-sell: progressive price lowering near window expiry (crypto only)
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from polyedge.core.config import MarketMakerConfig
from polyedge.data.book_analyzer import BookIntelligence

logger = logging.getLogger("polyedge.mm")


@dataclass
class Quote:
    """A single bid or ask quote to post."""

    token_id: str
    side: str  # "BUY" or "SELL"
    price: float
    size: float
    expiration: int = 0  # Unix timestamp, 0 = GTC

    def as_order_dict(self) -> dict:
        """Convert to dict for post_orders_batch."""
        return {
            "token_id": self.token_id,
            "side": self.side,
            "price": self.price,
            "size": self.size,
            "post_only": True,
            "expiration": self.expiration,
        }


@dataclass
class QuoteSet:
    """Bid/ask quotes for both YES and NO tokens in a market."""

    yes_bid: Quote | None = None
    yes_ask: Quote | None = None
    no_bid: Quote | None = None
    no_ask: Quote | None = None
    fair_value: float = 0.50
    spread: float = 0.06
    reason_skipped: str | None = None  # Why quotes were not generated

    @property
    def is_active(self) -> bool:
        return any([self.yes_bid, self.yes_ask, self.no_bid, self.no_ask])

    def all_quotes(self) -> list[Quote]:
        return [q for q in [self.yes_bid, self.yes_ask, self.no_bid, self.no_ask] if q]


@dataclass
class Inventory:
    """Tracks YES and NO token holdings for a single market."""

    yes_tokens: float = 0.0
    no_tokens: float = 0.0
    yes_cost_basis: float = 0.0  # Total USD spent on YES
    no_cost_basis: float = 0.0  # Total USD spent on NO

    @property
    def imbalance(self) -> float:
        """Fraction of inventory on YES side. 0.5 = balanced.

        Returns 0.5 if no inventory.
        """
        total = self.yes_tokens + self.no_tokens
        if total == 0:
            return 0.5
        return self.yes_tokens / total

    @property
    def net_exposure(self) -> float:
        """Net directional exposure in token units.

        Positive = net long YES (bullish exposure).
        Negative = net long NO (bearish exposure).
        """
        return self.yes_tokens - self.no_tokens

    def avg_cost(self, token: str) -> float:
        """Average cost per token. Returns 0 if no position."""
        if token == "YES":
            return self.yes_cost_basis / self.yes_tokens if self.yes_tokens > 0 else 0.0
        return self.no_cost_basis / self.no_tokens if self.no_tokens > 0 else 0.0

    def record_fill(self, side: str, token: str, size: float, price: float):
        """Record a fill. side = BUY/SELL, token = YES/NO."""
        if side == "BUY":
            if token == "YES":
                self.yes_tokens += size
                self.yes_cost_basis += size * price
            else:
                self.no_tokens += size
                self.no_cost_basis += size * price
        else:  # SELL
            if token == "YES":
                if self.yes_tokens > 0:
                    sell_frac = min(size / self.yes_tokens, 1.0)
                    self.yes_cost_basis *= (1.0 - sell_frac)
                self.yes_tokens = max(0, self.yes_tokens - size)
            else:
                if self.no_tokens > 0:
                    sell_frac = min(size / self.no_tokens, 1.0)
                    self.no_cost_basis *= (1.0 - sell_frac)
                self.no_tokens = max(0, self.no_tokens - size)


@dataclass
class _ImbalanceReading:
    """A single imbalance observation for velocity tracking."""
    timestamp: float
    imbalance_5c: float


class MarketMakerStrategy:
    """Post-only market maker for any Polymarket market.

    Core loop (driven by runner):
    1. Check quote gates (book data, spread, inventory, adverse selection)
    2. Compute fair value from Polymarket book midpoint
    3. Calculate bid/ask spread (wider near expiry, on whale activity)
    4. Skew quotes based on inventory imbalance
    5. Post both sides as post-only GTD/GTC orders
    6. Pull quotes on imbalance velocity spikes (defense)

    Key invariant: ALL orders are post_only=True. We NEVER pay taker fees.
    """

    name = "market_maker"

    def __init__(self, config: MarketMakerConfig):
        self.config = config
        self.inventory: dict[str, Inventory] = {}  # condition_id -> Inventory
        self._last_fair_value: dict[str, float] = {}
        self._last_quote_time: dict[str, float] = {}
        self._pulled_until: dict[str, float] = {}
        self._window_pnl: dict[str, float] = {}

        # Imbalance velocity tracking — ring buffer per market
        self._imbalance_history: dict[str, deque[_ImbalanceReading]] = {}

    def get_inventory(self, condition_id: str) -> Inventory:
        """Get or create inventory tracker for a market."""
        if condition_id not in self.inventory:
            self.inventory[condition_id] = Inventory()
        return self.inventory[condition_id]

    def _get_imbalance_history(self, condition_id: str) -> deque[_ImbalanceReading]:
        """Get or create imbalance ring buffer for a market."""
        if condition_id not in self._imbalance_history:
            self._imbalance_history[condition_id] = deque(maxlen=20)
        return self._imbalance_history[condition_id]

    # --- Fair Value ---

    def compute_fair_value(
        self,
        yes_book: BookIntelligence | None,
        no_book: BookIntelligence | None,
    ) -> float | None:
        """Compute fair value for the YES token from Polymarket book.

        Returns None if insufficient book data (signals: don't quote).
        """
        if yes_book is None:
            return None

        # Primary: YES book midpoint
        if yes_book.best_bid > 0 and yes_book.best_ask > 0:
            fv = yes_book.midpoint
        elif yes_book.best_bid > 0:
            fv = yes_book.best_bid
        elif yes_book.best_ask > 0:
            fv = yes_book.best_ask
        else:
            return None  # No usable price data

        # Cross-check with NO book if available (YES + NO should ~= 1.0)
        if no_book and no_book.midpoint > 0:
            no_implied_yes = 1.0 - no_book.midpoint
            # Blend: 70% YES book, 30% NO-implied (adds stability)
            fv = 0.70 * fv + 0.30 * no_implied_yes

        return max(0.02, min(0.98, round(fv, 2)))

    # --- Quote Gating ---

    def should_quote(
        self,
        condition_id: str,
        yes_book: BookIntelligence | None,
        no_book: BookIntelligence | None,
        seconds_remaining: float | None,
    ) -> str | None:
        """Check if we should generate quotes. Returns skip reason or None (ok to quote).

        This is the gate that prevents reckless bidding.
        """
        # No book data — warmup not complete
        if yes_book is None:
            return "no_yes_book"

        if yes_book.best_bid <= 0 and yes_book.best_ask <= 0:
            return "yes_book_empty"

        # Spread too tight — can't profit, someone else is tighter
        if yes_book.spread_bps > 0 and yes_book.spread_bps < self.config.min_profitable_spread_bps:
            return f"spread_too_tight_{yes_book.spread_bps:.0f}bps"

        # Inventory limit hit
        inv = self.get_inventory(condition_id)
        fv = self.compute_fair_value(yes_book, no_book) or 0.50
        total_inv_usd = inv.yes_tokens * fv + inv.no_tokens * (1.0 - fv)
        if total_inv_usd >= self.config.max_inventory_usd:
            # Still allow sells to offload, but caller should handle this
            pass  # Don't block entirely — compute_quotes suppresses new bids

        # Window loss breaker
        window_pnl = self._window_pnl.get(condition_id, 0)
        if window_pnl < -self.config.max_loss_per_window_usd:
            return f"window_loss_{window_pnl:.2f}"

        # Book heavily one-sided — adverse selection risk
        if yes_book.imbalance_5c != 0:
            if abs(yes_book.imbalance_5c) > self.config.adverse_selection_threshold:
                return f"one_sided_book_{yes_book.imbalance_5c:+.2f}"

        # Time gate — but always allow sells to offload inventory
        if seconds_remaining is not None:
            has_inventory = inv.yes_tokens > 0 or inv.no_tokens > 0
            if seconds_remaining < self.config.min_seconds_remaining and not has_inventory:
                return "near_expiry"

        return None  # OK to quote

    # --- Defense ---

    def should_pull_quotes(
        self,
        condition_id: str,
        yes_book: BookIntelligence | None,
        depth_momentum: float = 0.0,
    ) -> str | None:
        """Check if we should pull all quotes. Returns reason or None.

        Defense based on Polymarket book dynamics, with optional Binance depth.
        """
        now = time.monotonic()

        # Already pulled and recovering
        resume_at = self._pulled_until.get(condition_id, 0)
        if now < resume_at:
            return "recovering"

        # Poly book defense: imbalance velocity
        if yes_book and yes_book.imbalance_5c != 0:
            history = self._get_imbalance_history(condition_id)
            history.append(_ImbalanceReading(now, yes_book.imbalance_5c))

            if len(history) >= 3:
                # Compute velocity: change in imbalance per second over recent readings
                oldest = history[0]
                dt = now - oldest.timestamp
                if dt > 0.5:  # Need at least 0.5s of data
                    velocity = abs(yes_book.imbalance_5c - oldest.imbalance_5c) / dt
                    if velocity > self.config.imbalance_velocity_pull_threshold:
                        self._pulled_until[condition_id] = now + self.config.depth_recovery_seconds
                        return f"imbalance_velocity_{velocity:.2f}/s"

        # Poly book defense: spread compression (market got tighter than us)
        if yes_book and yes_book.spread_bps > 0:
            if yes_book.spread_bps < self.config.min_profitable_spread_bps:
                return "spread_compressed"

        # Optional Binance depth defense (crypto markets only)
        if self.config.depth_defense_enabled and abs(depth_momentum) > self.config.depth_pull_threshold:
            self._pulled_until[condition_id] = now + self.config.depth_recovery_seconds
            return f"depth_spike_{depth_momentum:+.2f}"

        return None

    def _whale_spread_multiplier(
        self,
        yes_book: BookIntelligence | None,
        fair_value: float,
    ) -> float:
        """Returns spread multiplier based on whale proximity to our quotes."""
        if not yes_book:
            return 1.0

        half_spread = self.config.base_spread / 2.0
        our_bid = fair_value - half_spread
        our_ask = fair_value + half_spread

        # Check for whales within 3 cents of our quote prices
        for whale in yes_book.whale_asks:
            if abs(whale.price - our_ask) <= 0.03:
                return self.config.whale_widen_factor
        for whale in yes_book.whale_bids:
            if abs(whale.price - our_bid) <= 0.03:
                return self.config.whale_widen_factor

        return 1.0

    # --- Spread Computation ---

    def compute_spread(
        self,
        condition_id: str,
        seconds_remaining: float | None,
        yes_book: BookIntelligence | None,
        fair_value: float,
    ) -> float:
        """Compute the full spread (bid-ask gap).

        Wider = safer but fewer fills. Tighter = more fills but more adverse selection.
        """
        spread = self.config.base_spread

        # Widen on whale proximity
        spread *= self._whale_spread_multiplier(yes_book, fair_value)

        # Widen near window end (time decay — less time to recover from adverse fill)
        if seconds_remaining is not None and seconds_remaining < self.config.time_decay_widen_seconds:
            decay_frac = 1.0 - (seconds_remaining / self.config.time_decay_widen_seconds)
            time_mult = 1.0 + decay_frac * (self.config.time_decay_spread_mult - 1.0)
            spread *= time_mult

        return max(self.config.min_spread, min(self.config.max_spread, spread))

    # --- Profit Floor ---

    def _decayed_profit_pct(self, seconds_remaining: float | None) -> float:
        """Decay the profit floor as the window runs out.

        For static markets (seconds_remaining=None), returns full profit target.
        For crypto windows, linear decay from full target to 0% (breakeven).
        """
        full_profit = self.config.min_profit_pct

        if seconds_remaining is None:
            return full_profit

        decay_start = self.config.profit_decay_start_seconds
        force_sell = self.config.force_sell_seconds

        if seconds_remaining >= decay_start:
            return full_profit

        decay_window = decay_start - force_sell
        if decay_window <= 0:
            return full_profit

        progress = (seconds_remaining - force_sell) / decay_window
        progress = max(0.0, min(1.0, progress))
        return full_profit * progress

    # --- Main Quote Computation ---

    def compute_quotes(
        self,
        condition_id: str,
        yes_token_id: str,
        no_token_id: str,
        yes_book: BookIntelligence | None,
        no_book: BookIntelligence | None,
        seconds_remaining: float | None,
        tick_size: float = 0.01,
        depth_momentum: float = 0.0,
    ) -> QuoteSet:
        """Compute the full quote set for a market — both YES and NO sides.

        Quotes both tokens so the maker profits regardless of direction:
        - Price dips → YES drops, NO rises → sell NO at profit
        - Price rips → YES rises, NO drops → sell YES at profit
        """
        qs = QuoteSet()

        # Quote gating — are conditions safe to quote?
        skip_reason = self.should_quote(condition_id, yes_book, no_book, seconds_remaining)
        if skip_reason:
            qs.reason_skipped = skip_reason
            return qs

        # Defense — should we pull?
        pull_reason = self.should_pull_quotes(condition_id, yes_book, depth_momentum)
        if pull_reason:
            qs.reason_skipped = pull_reason
            return qs

        # Compute fair value
        fair_value = self.compute_fair_value(yes_book, no_book)
        if fair_value is None:
            qs.reason_skipped = "no_fair_value"
            return qs

        qs.fair_value = fair_value
        no_fair_value = round(1.0 - fair_value, 2)

        # Rate limit requotes (but always allow if we have inventory)
        now = time.monotonic()
        last_quote = self._last_quote_time.get(condition_id, 0)
        last_fv = self._last_fair_value.get(condition_id, 0)
        inv = self.get_inventory(condition_id)
        has_inventory = inv.yes_tokens > 0 or inv.no_tokens > 0

        fv_moved = abs(fair_value - last_fv) >= self.config.requote_threshold
        time_elapsed = (now - last_quote) >= self.config.min_requote_interval

        if not fv_moved and not time_elapsed and last_fv > 0 and not has_inventory:
            qs.reason_skipped = "no_requote_needed"
            return qs

        # Compute spread
        spread = self.compute_spread(condition_id, seconds_remaining, yes_book, fair_value)
        qs.spread = spread
        half_spread = spread / 2.0
        tick = tick_size

        near_window_end = (
            seconds_remaining is not None
            and seconds_remaining < self.config.force_sell_seconds
        )

        # GTD expiration (crypto windows only)
        expiration = 0
        if (
            self.config.use_gtd
            and seconds_remaining is not None
            and seconds_remaining > self.config.gtd_buffer_seconds
        ):
            expiration = int(time.time() + seconds_remaining - self.config.gtd_buffer_seconds)

        # Inventory limits
        max_inv = self.config.max_inventory_usd
        total_inv_usd = inv.yes_tokens * fair_value + inv.no_tokens * no_fair_value

        # Suppress new bids near expiry (but always allow sells)
        suppress_new_bids = (
            seconds_remaining is not None
            and seconds_remaining < self.config.min_seconds_remaining
        )

        # Inventory skew — linear offset proportional to net exposure
        max_tokens = max_inv / max(fair_value, 0.10)  # Rough max tokens
        skew_offset = 0.0
        if max_tokens > 0 and inv.net_exposure != 0:
            skew_offset = self.config.inventory_skew_factor * inv.net_exposure / max_tokens

        # ===================== YES SIDE =====================
        # YES bid — buy YES
        yes_bid_price = fair_value - half_spread - skew_offset
        yes_bid_price = _snap_price(yes_bid_price, tick)

        # Gate on the actual bid price, not just FV — prevents deep OTM bids
        yes_price_ok = self.config.min_entry_price <= yes_bid_price <= self.config.max_entry_price

        yes_bid_size = round(self.config.quote_size_usd / yes_bid_price, 1) if yes_bid_price > 0 else 0

        # Suppress YES bid when overweight YES
        yes_at_max = inv.yes_tokens * fair_value >= max_inv * self.config.max_inventory_imbalance
        can_buy_yes = yes_price_ok and not suppress_new_bids and total_inv_usd < max_inv and not yes_at_max

        if can_buy_yes and yes_bid_size >= 1:
            qs.yes_bid = Quote(
                token_id=yes_token_id, side="BUY",
                price=yes_bid_price, size=yes_bid_size, expiration=expiration,
            )

        # YES ask — sell YES when we hold it
        if inv.yes_tokens > 0:
            yes_ask_price = fair_value + half_spread
            # Profit floor — decays as window runs out
            yes_avg_cost = inv.avg_cost("YES")
            if yes_avg_cost > 0 and not near_window_end:
                effective_profit_pct = self._decayed_profit_pct(seconds_remaining)
                yes_ask_price = max(yes_ask_price, yes_avg_cost * (1.0 + effective_profit_pct))

            # Floor above best bid (post_only protection)
            if yes_book and yes_book.best_bid > 0:
                yes_ask_price = max(yes_ask_price, yes_book.best_bid + tick)

            yes_ask_price = _snap_price(yes_ask_price, tick)
            yes_ask_size = round(inv.yes_tokens, 1)

            # Ensure bid < ask
            if qs.yes_bid and qs.yes_bid.price >= yes_ask_price:
                yes_ask_price = round(qs.yes_bid.price + tick, 2)

            if yes_ask_size >= 1:
                qs.yes_ask = Quote(
                    token_id=yes_token_id, side="SELL",
                    price=yes_ask_price, size=yes_ask_size, expiration=expiration,
                )

        # ===================== NO SIDE =====================
        # NO bid — buy NO
        no_bid_price = no_fair_value - half_spread + skew_offset  # Opposite skew direction
        no_bid_price = _snap_price(no_bid_price, tick)

        # Gate on actual bid price, not just FV
        no_price_ok = self.config.min_entry_price <= no_bid_price <= self.config.max_entry_price

        no_bid_size = round(self.config.quote_size_usd / no_bid_price, 1) if no_bid_price > 0 else 0

        # Suppress NO bid when overweight NO
        no_at_max = inv.no_tokens * no_fair_value >= max_inv * self.config.max_inventory_imbalance
        can_buy_no = no_price_ok and not suppress_new_bids and total_inv_usd < max_inv and not no_at_max

        if can_buy_no and no_bid_size >= 1:
            qs.no_bid = Quote(
                token_id=no_token_id, side="BUY",
                price=no_bid_price, size=no_bid_size, expiration=expiration,
            )

        # NO ask — sell NO when we hold it
        if inv.no_tokens > 0:
            no_ask_price = no_fair_value + half_spread
            no_avg_cost = inv.avg_cost("NO")
            if no_avg_cost > 0 and not near_window_end:
                effective_profit_pct = self._decayed_profit_pct(seconds_remaining)
                no_ask_price = max(no_ask_price, no_avg_cost * (1.0 + effective_profit_pct))

            if no_book and no_book.best_bid > 0:
                no_ask_price = max(no_ask_price, no_book.best_bid + tick)

            no_ask_price = _snap_price(no_ask_price, tick)
            no_ask_size = round(inv.no_tokens, 1)

            if qs.no_bid and qs.no_bid.price >= no_ask_price:
                no_ask_price = round(qs.no_bid.price + tick, 2)

            if no_ask_size >= 1:
                qs.no_ask = Quote(
                    token_id=no_token_id, side="SELL",
                    price=no_ask_price, size=no_ask_size, expiration=expiration,
                )

        # Update tracking
        self._last_fair_value[condition_id] = fair_value
        self._last_quote_time[condition_id] = now

        return qs

    # --- Force-Sell (Crypto Windows) ---

    def compute_force_sell_quotes(
        self,
        condition_id: str,
        yes_token_id: str,
        no_token_id: str,
        seconds_remaining: float,
        yes_book: BookIntelligence | None,
        no_book: BookIntelligence | None,
        tick_size: float = 0.01,
    ) -> QuoteSet:
        """Compute aggressive sell quotes to liquidate inventory before window expiry.

        Progressive price floor lowering:
          >force_sell_seconds: handled by normal compute_quotes (decayed profit floor)
          force_sell → fire_sale_seconds: sell at cost basis (0% profit)
          fire_sale → 0: sell at any price (dump it)
        """
        qs = QuoteSet()
        inv = self.get_inventory(condition_id)

        if inv.yes_tokens <= 0 and inv.no_tokens <= 0:
            return qs  # Nothing to sell

        tick = tick_size
        fire_sale = seconds_remaining < self.config.force_sell_fire_sale_seconds

        # YES sell
        if inv.yes_tokens >= 1:
            if fire_sale:
                # Sell at any price — dump it
                yes_ask_price = max(tick, (yes_book.best_bid - 0.02) if yes_book and yes_book.best_bid > 0 else 0.01)
            else:
                # Sell at cost basis (breakeven)
                yes_avg = inv.avg_cost("YES")
                yes_ask_price = max(tick, yes_avg if yes_avg > 0 else 0.01)
                if yes_book and yes_book.best_bid > 0:
                    yes_ask_price = max(yes_ask_price, yes_book.best_bid + tick)

            yes_ask_price = _snap_price(yes_ask_price, tick)
            qs.yes_ask = Quote(
                token_id=yes_token_id, side="SELL",
                price=yes_ask_price, size=round(inv.yes_tokens, 1),
            )

        # NO sell
        if inv.no_tokens >= 1:
            if fire_sale:
                no_ask_price = max(tick, (no_book.best_bid - 0.02) if no_book and no_book.best_bid > 0 else 0.01)
            else:
                no_avg = inv.avg_cost("NO")
                no_ask_price = max(tick, no_avg if no_avg > 0 else 0.01)
                if no_book and no_book.best_bid > 0:
                    no_ask_price = max(no_ask_price, no_book.best_bid + tick)

            no_ask_price = _snap_price(no_ask_price, tick)
            qs.no_ask = Quote(
                token_id=no_token_id, side="SELL",
                price=no_ask_price, size=round(inv.no_tokens, 1),
            )

        qs.fair_value = self.compute_fair_value(yes_book, no_book) or 0.50
        return qs

    # --- Fill Recording ---

    def record_fill(
        self,
        condition_id: str,
        side: str,
        token: str,
        size: float,
        price: float,
    ) -> float | None:
        """Record a fill and update inventory + P&L tracking.

        Returns avg_entry price for SELL fills (computed BEFORE inventory
        is updated), or None for BUY fills.
        """
        inv = self.get_inventory(condition_id)

        avg_entry = None
        if side == "SELL":
            avg_entry = inv.avg_cost(token)
            if avg_entry > 0:
                pnl = (price - avg_entry) * size
                self._window_pnl[condition_id] = self._window_pnl.get(condition_id, 0) + pnl

        inv.record_fill(side, token, size, price)

        logger.info(
            f"Fill: {side} {size:.1f} {token} @ ${price:.2f} | "
            f"Inventory: YES={inv.yes_tokens:.1f} NO={inv.no_tokens:.1f} "
            f"(imbalance={inv.imbalance:.2f})"
        )
        return avg_entry

    # --- State Reset ---

    def reset_window(self, condition_id: str):
        """Reset per-window state when hopping to a new window."""
        self._window_pnl.pop(condition_id, None)
        self._pulled_until.pop(condition_id, None)
        self._last_fair_value.pop(condition_id, None)
        self._last_quote_time.pop(condition_id, None)
        self._imbalance_history.pop(condition_id, None)
        self.inventory.pop(condition_id, None)

    def reset_all(self):
        """Reset all state."""
        self.inventory.clear()
        self._last_fair_value.clear()
        self._last_quote_time.clear()
        self._pulled_until.clear()
        self._window_pnl.clear()
        self._imbalance_history.clear()


def _snap_price(price: float, tick: float) -> float:
    """Snap price to valid tick increment, clamped to [tick, 1-tick]."""
    if tick <= 0:
        tick = 0.01
    snapped = max(tick, min(1.0 - tick, round(price / tick) * tick))
    return round(snapped, 2)
