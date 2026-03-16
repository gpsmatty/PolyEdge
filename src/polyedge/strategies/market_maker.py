"""Market Maker — post-only limit orders on both sides, capture spread.

Zero taker fees + maker rebates. Uses Binance depth as DEFENSE (pull quotes
when momentum spikes), not as an offensive directional signal.

Architecture:
- Quote engine: computes bid/ask prices based on fair value, spread, inventory skew
- Depth defense: widens or pulls quotes when Binance book shifts fast
- Inventory manager: tracks YES/NO positions, skews quotes to rebalance
- Fill monitor: detects fills and updates inventory
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from polyedge.core.config import MarketMakerConfig

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
    reason_pulled: str | None = None  # Why quotes were pulled (depth, risk, etc.)

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
    def total_value_usd(self) -> float:
        """Approximate USD value at current fair value (assumes 0.50)."""
        return self.yes_tokens * 0.50 + self.no_tokens * 0.50

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
                # Reduce cost basis proportionally
                if self.yes_tokens > 0:
                    sell_frac = min(size / self.yes_tokens, 1.0)
                    self.yes_cost_basis *= (1.0 - sell_frac)
                self.yes_tokens = max(0, self.yes_tokens - size)
            else:
                if self.no_tokens > 0:
                    sell_frac = min(size / self.no_tokens, 1.0)
                    self.no_cost_basis *= (1.0 - sell_frac)
                self.no_tokens = max(0, self.no_tokens - size)


class MarketMakerStrategy:
    """Post-only market maker for Polymarket crypto up/down markets.

    Core loop (driven by runner):
    1. Compute fair value from Binance price + market mid
    2. Calculate bid/ask spread (wider in vol, near expiry, high depth momentum)
    3. Skew quotes based on inventory imbalance
    4. Post both sides as post-only GTD orders
    5. Monitor fills, update inventory
    6. Pull quotes on depth spike (adverse selection defense)
    7. Requote when fair value moves past threshold

    Key invariant: ALL orders are post_only=True. We NEVER pay taker fees.
    """

    name = "market_maker"

    def __init__(self, config: MarketMakerConfig):
        self.config = config
        self.inventory: dict[str, Inventory] = {}  # condition_id -> Inventory
        self._last_fair_value: dict[str, float] = {}  # condition_id -> last FV
        self._last_quote_time: dict[str, float] = {}  # condition_id -> timestamp
        self._pulled_until: dict[str, float] = {}  # condition_id -> resume timestamp
        self._window_pnl: dict[str, float] = {}  # condition_id -> net P&L this window

    def get_inventory(self, condition_id: str) -> Inventory:
        """Get or create inventory tracker for a market."""
        if condition_id not in self.inventory:
            self.inventory[condition_id] = Inventory()
        return self.inventory[condition_id]

    def compute_fair_value(
        self,
        yes_price: float,
        no_price: float,
    ) -> float:
        """Compute fair value for the YES token.

        For now, uses the midpoint of best bid/ask from the Polymarket book.
        Future: incorporate Binance-implied probability.

        Returns a value between 0.01 and 0.99.
        """
        # Simple midpoint of YES best bid/ask
        mid = (yes_price + (1.0 - no_price)) / 2.0
        return max(0.01, min(0.99, round(mid, 2)))

    def compute_spread(
        self,
        condition_id: str,
        seconds_remaining: float,
        depth_momentum: float = 0.0,
    ) -> float:
        """Compute the full spread (bid-ask gap) based on conditions.

        Wider spread = more safety but fewer fills.
        Tighter spread = more fills but more adverse selection risk.
        """
        spread = self.config.base_spread

        # Widen on depth momentum (defensive)
        abs_depth = abs(depth_momentum)
        if abs_depth > self.config.depth_widen_threshold:
            depth_mult = 1.0 + (abs_depth - self.config.depth_widen_threshold) * (
                self.config.depth_widen_factor - 1.0
            ) / (1.0 - self.config.depth_widen_threshold)
            spread *= min(depth_mult, self.config.depth_widen_factor)

        # Widen near window end (time decay — less time to recover from adverse fill)
        if seconds_remaining < self.config.time_decay_widen_seconds:
            decay_frac = 1.0 - (seconds_remaining / self.config.time_decay_widen_seconds)
            time_mult = 1.0 + decay_frac * (self.config.time_decay_spread_mult - 1.0)
            spread *= time_mult

        # Clamp
        return max(self.config.min_spread, min(self.config.max_spread, spread))

    def should_pull_quotes(
        self,
        condition_id: str,
        depth_momentum: float,
    ) -> str | None:
        """Check if we should pull all quotes (return reason string, or None).

        Pull = cancel all orders immediately. Safety mechanism.
        """
        now = time.monotonic()

        # Already pulled and recovering
        resume_at = self._pulled_until.get(condition_id, 0)
        if now < resume_at:
            return "recovering"

        # Depth spike — strong directional move in progress
        if abs(depth_momentum) > self.config.depth_pull_threshold:
            self._pulled_until[condition_id] = now + self.config.depth_recovery_seconds
            return f"depth_spike_{depth_momentum:+.2f}"

        # Window P&L circuit breaker
        window_pnl = self._window_pnl.get(condition_id, 0)
        if window_pnl < -self.config.max_loss_per_window_usd:
            return f"window_loss_{window_pnl:.2f}"

        return None

    def compute_quotes(
        self,
        condition_id: str,
        yes_token_id: str,
        no_token_id: str,
        yes_price: float,
        no_price: float,
        seconds_remaining: float,
        depth_momentum: float = 0.0,
        tick_size: float = 0.01,
        yes_best_bid: float = 0.0,
        no_best_bid: float = 0.0,
    ) -> QuoteSet:
        """Compute the full quote set for a market — both YES and NO sides.

        Quotes both tokens so the maker profits regardless of BTC direction:
        - BTC dips → YES drops, NO rises → sell NO at profit
        - BTC rips → YES rises, NO drops → sell YES at profit

        Args:
            yes_best_bid: Actual best bid on Polymarket for YES token.
            no_best_bid: Actual best bid on Polymarket for NO token.
        """
        qs = QuoteSet()

        # Check if we should pull
        pull_reason = self.should_pull_quotes(condition_id, depth_momentum)
        if pull_reason:
            qs.reason_pulled = pull_reason
            return qs

        inv = self.get_inventory(condition_id)
        has_yes_inventory = inv.yes_tokens > 0
        has_no_inventory = inv.no_tokens > 0
        has_any_inventory = has_yes_inventory or has_no_inventory

        # Time gate — but ALWAYS allow sells to offload inventory near expiry
        if seconds_remaining < self.config.min_seconds_remaining and not has_any_inventory:
            qs.reason_pulled = "near_expiry"
            return qs

        # Rate limit requotes (but always allow if we have inventory to offload)
        now = time.monotonic()
        last_quote = self._last_quote_time.get(condition_id, 0)
        last_fv = self._last_fair_value.get(condition_id, 0)
        fair_value = self.compute_fair_value(yes_price, no_price)
        qs.fair_value = fair_value
        no_fair_value = round(1.0 - fair_value, 2)

        fv_moved = abs(fair_value - last_fv) >= self.config.requote_threshold
        time_elapsed = (now - last_quote) >= self.config.requote_interval_seconds

        if not fv_moved and not time_elapsed and last_fv > 0 and not has_any_inventory:
            qs.reason_pulled = "no_requote_needed"
            return qs

        # Compute spread (same for both sides)
        spread = self.compute_spread(condition_id, seconds_remaining, depth_momentum)
        qs.spread = spread
        half_spread = spread / 2.0
        tick = tick_size
        near_window_end = seconds_remaining < self.config.force_sell_seconds

        # GTD expiration
        expiration = 0
        if self.config.use_gtd and seconds_remaining > self.config.gtd_buffer_seconds:
            expiration = int(time.time() + seconds_remaining - self.config.gtd_buffer_seconds)

        # Total inventory check — don't exceed max across BOTH sides combined
        max_inv = self.config.max_inventory_usd
        total_inv_usd = inv.yes_tokens * fair_value + inv.no_tokens * no_fair_value

        # Suppress new bids near expiry (but allow sells)
        suppress_new_bids = seconds_remaining < self.config.min_seconds_remaining

        # ===================== YES SIDE =====================
        yes_price_ok = self.config.min_entry_price <= yes_price <= self.config.max_entry_price

        # YES bid — buy YES when it's cheap (BTC dipping)
        yes_bid_price = fair_value - half_spread
        # Skew YES bid down when overweight YES
        yes_skew = (inv.imbalance - 0.5) * self.config.inventory_skew_factor * 10
        yes_bid_price -= yes_skew

        yes_bid_price = max(tick, min(1.0 - tick, round(yes_bid_price / tick) * tick))
        yes_bid_price = round(yes_bid_price, 2)
        yes_bid_size = round(self.config.quote_size_usd / yes_bid_price, 1) if yes_bid_price > 0 else 0

        can_buy_yes = yes_price_ok and not suppress_new_bids and total_inv_usd < max_inv
        if can_buy_yes and yes_bid_size >= 1:
            qs.yes_bid = Quote(
                token_id=yes_token_id, side="BUY",
                price=yes_bid_price, size=yes_bid_size, expiration=expiration,
            )

        # YES ask — sell YES when we hold it and price is up
        if has_yes_inventory:
            yes_ask_price = fair_value + half_spread
            # Profit floor
            yes_avg_cost = inv.avg_cost("YES")
            if yes_avg_cost > 0 and not near_window_end:
                yes_ask_price = max(yes_ask_price, yes_avg_cost * (1.0 + self.config.min_profit_pct))
            # Floor above best bid (post_only protection)
            if yes_best_bid > 0:
                yes_ask_price = max(yes_ask_price, yes_best_bid + tick)

            yes_ask_price = max(tick, min(1.0 - tick, round(yes_ask_price / tick) * tick))
            yes_ask_price = round(yes_ask_price, 2)
            yes_ask_size = round(inv.yes_tokens, 1)

            # Ensure bid < ask
            if qs.yes_bid and yes_bid_price >= yes_ask_price:
                yes_ask_price = round(yes_bid_price + tick, 2)

            if yes_ask_size >= 1:
                qs.yes_ask = Quote(
                    token_id=yes_token_id, side="SELL",
                    price=yes_ask_price, size=yes_ask_size, expiration=expiration,
                )

        # ===================== NO SIDE =====================
        no_price_ok = self.config.min_entry_price <= no_price <= self.config.max_entry_price

        # NO bid — buy NO when it's cheap (BTC ripping, so NO drops)
        no_bid_price = no_fair_value - half_spread
        # Skew NO bid down when overweight NO (imbalance < 0.5 = heavy NO)
        no_skew = (0.5 - inv.imbalance) * self.config.inventory_skew_factor * 10
        no_bid_price -= no_skew

        no_bid_price = max(tick, min(1.0 - tick, round(no_bid_price / tick) * tick))
        no_bid_price = round(no_bid_price, 2)
        no_bid_size = round(self.config.quote_size_usd / no_bid_price, 1) if no_bid_price > 0 else 0

        can_buy_no = no_price_ok and not suppress_new_bids and total_inv_usd < max_inv
        if can_buy_no and no_bid_size >= 1:
            qs.no_bid = Quote(
                token_id=no_token_id, side="BUY",
                price=no_bid_price, size=no_bid_size, expiration=expiration,
            )

        # NO ask — sell NO when we hold it and price is up
        if has_no_inventory:
            no_ask_price = no_fair_value + half_spread
            # Profit floor
            no_avg_cost = inv.avg_cost("NO")
            if no_avg_cost > 0 and not near_window_end:
                no_ask_price = max(no_ask_price, no_avg_cost * (1.0 + self.config.min_profit_pct))
            # Floor above best bid (post_only protection)
            if no_best_bid > 0:
                no_ask_price = max(no_ask_price, no_best_bid + tick)

            no_ask_price = max(tick, min(1.0 - tick, round(no_ask_price / tick) * tick))
            no_ask_price = round(no_ask_price, 2)
            no_ask_size = round(inv.no_tokens, 1)

            # Ensure bid < ask
            if qs.no_bid and no_bid_price >= no_ask_price:
                no_ask_price = round(no_bid_price + tick, 2)

            if no_ask_size >= 1:
                qs.no_ask = Quote(
                    token_id=no_token_id, side="SELL",
                    price=no_ask_price, size=no_ask_size, expiration=expiration,
                )

        # Update tracking
        self._last_fair_value[condition_id] = fair_value
        self._last_quote_time[condition_id] = now

        return qs

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

        # For SELL fills, capture avg entry BEFORE decrementing inventory
        avg_entry = None
        if side == "SELL":
            avg_entry = inv.avg_cost(token)
            if avg_entry > 0:
                # Update window P&L
                pnl = (price - avg_entry) * size
                self._window_pnl[condition_id] = self._window_pnl.get(condition_id, 0) + pnl

        # Now update inventory
        inv.record_fill(side, token, size, price)

        logger.info(
            f"Fill: {side} {size:.1f} {token} @ ${price:.2f} | "
            f"Inventory: YES={inv.yes_tokens:.1f} NO={inv.no_tokens:.1f} "
            f"(imbalance={inv.imbalance:.2f})"
        )
        return avg_entry

    def reset_window(self, condition_id: str):
        """Reset per-window state when hopping to a new window."""
        self._window_pnl.pop(condition_id, None)
        self._pulled_until.pop(condition_id, None)
        self._last_fair_value.pop(condition_id, None)
        self._last_quote_time.pop(condition_id, None)
        # Clear stale inventory for expired market
        self.inventory.pop(condition_id, None)

    def reset_all(self):
        """Reset all state."""
        self.inventory.clear()
        self._last_fair_value.clear()
        self._last_quote_time.clear()
        self._pulled_until.clear()
        self._window_pnl.clear()
