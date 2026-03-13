"""Execution engine — places and manages orders on Polymarket."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from polyedge.core.client import PolyClient
from polyedge.core.config import Settings
from polyedge.core.console import console
from polyedge.core.db import Database
from polyedge.core.models import Market, OrderStatus, Side, TradeStatus
from polyedge.risk.portfolio import PortfolioRiskManager, PortfolioSnapshot

logger = logging.getLogger("polyedge.execution")


class ExecutionEngine:
    """Handles order placement, tracking, and position management."""

    def __init__(self, client: PolyClient, db: Database, settings: Settings):
        self.client = client
        self.db = db
        self.settings = settings
        self.risk_manager = PortfolioRiskManager(settings.risk)

    async def place_order(
        self,
        market: Market,
        token_id: str,
        side: str,
        price: float,
        size: float,
        amount_usd: float,
        strategy: str = "",
        reasoning: str = "",
        ai_probability: Optional[float] = None,
        force: bool = False,
        order_type: str = "GTC",
    ) -> Optional[str]:
        """Place an order with risk checks.

        Args:
            order_type: "GTC" (default, rests on book), "FOK" (fill all or cancel).

        Returns order_id on success, None on failure.
        """
        # Risk checks (skip with --yolo / force)
        if not force:
            snapshot = await self._get_portfolio_snapshot()
            risk_check = self.risk_manager.check_can_trade(snapshot)
            if not risk_check.passed:
                console.print(f"[red]Risk check failed: {risk_check.reason}")
                return None

            size_check = self.risk_manager.check_position_size(amount_usd, snapshot.bankroll)
            if not size_check.passed:
                console.print(f"[red]Position size check failed: {size_check.reason}")
                return None

        # Confirmation (unless disabled)
        if self.settings.risk.confirm_trades and not force:
            console.print(
                f"\n[bold]Order: {side} {size:.1f} contracts at ${price:.3f} "
                f"(${amount_usd:.2f}) on '{market.question[:50]}'"
            )
            try:
                confirm = input("Confirm? (y/n): ").strip().lower()
                if confirm != "y":
                    console.print("[yellow]Order cancelled by user")
                    return None
            except EOFError:
                return None

        # Record order in DB
        order_id = await self.db.insert_order({
            "market_id": market.condition_id,
            "token_id": token_id,
            "side": side,
            "order_type": order_type,
            "price": price,
            "size": size,
            "amount_usd": amount_usd,
            "status": "PENDING",
            "strategy": strategy,
        })

        # Place on Polymarket
        try:
            result = self.client.place_limit_order(
                token_id=token_id,
                side=side,
                price=price,
                size=size,
                order_type=order_type,
            )

            poly_order_id = result.get("orderID", result.get("id", order_id))

            # Update order with Polymarket order ID
            await self.db.update_order_status(order_id, "OPEN")

            # Record trade
            await self.db.insert_trade({
                "trade_id": order_id,
                "market_id": market.condition_id,
                "token_id": token_id,
                "question": market.question,
                "side": side,
                "entry_price": price,
                "size": size,
                "status": "OPEN",
                "strategy": strategy,
                "reasoning": reasoning,
                "ai_probability": ai_probability,
            })

            # Update position
            await self.db.upsert_position({
                "market_id": market.condition_id,
                "token_id": token_id,
                "question": market.question,
                "side": side,
                "size": size,
                "entry_price": price,
                "current_price": price,
                "strategy": strategy,
            })

            console.print(
                f"[green]Order placed: {side} {size:.1f} @ ${price:.3f} "
                f"(${amount_usd:.2f}) — ID: {poly_order_id}"
            )
            return order_id

        except Exception as e:
            await self.db.update_order_status(order_id, "FAILED")
            console.print(f"[red]Order failed: {e}")
            logger.error(f"Order placement failed: {e}", exc_info=True)
            return None

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        try:
            self.client.cancel_order(order_id)
            await self.db.update_order_status(order_id, "CANCELLED")
            console.print(f"[yellow]Order {order_id} cancelled")
            return True
        except Exception as e:
            console.print(f"[red]Cancel failed: {e}")
            return False

    async def cancel_all(self) -> bool:
        """Cancel all open orders."""
        try:
            self.client.cancel_all_orders()
            console.print("[yellow]All orders cancelled")
            return True
        except Exception as e:
            console.print(f"[red]Cancel all failed: {e}")
            return False

    async def sync_positions(self):
        """Sync local position state with Polymarket.

        Checks open orders for fills and updates positions.
        """
        try:
            open_orders = self.client.get_open_orders()
            # Update order statuses based on Polymarket state
            for order in open_orders:
                poly_id = order.get("id", "")
                status = order.get("status", "OPEN")
                filled = float(order.get("size_matched", 0))
                total_size = float(order.get("original_size", 0))

                if filled > 0 and filled >= total_size:
                    await self.db.update_order_status(
                        poly_id, "FILLED", filled, float(order.get("price", 0))
                    )
                elif filled > 0:
                    await self.db.update_order_status(
                        poly_id, "PARTIALLY_FILLED", filled, float(order.get("price", 0))
                    )
        except Exception as e:
            logger.warning(f"Position sync failed: {e}")

    async def _get_portfolio_snapshot(self) -> PortfolioSnapshot:
        """Build current portfolio snapshot for risk checks."""
        positions = await self.db.get_open_positions()
        trades_today = await self.db.get_trades_today()

        total_exposure = sum(
            p.get("size", 0) * p.get("entry_price", 0) for p in positions
        )
        unrealized_pnl = sum(p.get("unrealized_pnl", 0) for p in positions)
        realized_today = sum(t.get("pnl", 0) for t in trades_today if t.get("pnl"))

        # TODO: Get actual bankroll from wallet balance
        bankroll = 200.0 - total_exposure + unrealized_pnl

        ai_cost = await self.db.get_ai_cost_today()

        return PortfolioSnapshot(
            bankroll=bankroll,
            total_exposure=total_exposure,
            positions_count=len(positions),
            unrealized_pnl=unrealized_pnl,
            realized_pnl_today=realized_today,
            trades_today=len(trades_today),
            peak_bankroll=max(200.0, bankroll),
            drawdown_pct=max(0, (200.0 - bankroll) / 200.0) if bankroll < 200 else 0,
            ai_cost_today=ai_cost,
        )
