"""P&L tracking and trade history."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from rich.console import Console
from rich.table import Table

from polyedge.core.db import Database

console = Console()


class PnLTracker:
    """Track profit/loss across all trades."""

    def __init__(self, db: Database):
        self.db = db

    async def get_summary(self) -> dict:
        """Get overall P&L summary."""
        open_trades = await self.db.get_open_trades()
        trades_today = await self.db.get_trades_today()
        positions = await self.db.get_open_positions()

        total_realized = sum(t.get("pnl", 0) for t in trades_today if t.get("pnl"))
        total_unrealized = sum(p.get("unrealized_pnl", 0) for p in positions)
        total_exposure = sum(
            p.get("size", 0) * p.get("entry_price", 0) for p in positions
        )

        return {
            "open_positions": len(positions),
            "trades_today": len(trades_today),
            "realized_pnl_today": total_realized,
            "unrealized_pnl": total_unrealized,
            "total_pnl": total_realized + total_unrealized,
            "total_exposure": total_exposure,
        }

    async def display_positions(self):
        """Display current positions in a rich table."""
        positions = await self.db.get_open_positions()

        if not positions:
            console.print("[dim]No open positions")
            return

        table = Table(title="Open Positions")
        table.add_column("Market", max_width=40)
        table.add_column("Side", justify="center")
        table.add_column("Size", justify="right")
        table.add_column("Entry", justify="right")
        table.add_column("Current", justify="right")
        table.add_column("P&L", justify="right")
        table.add_column("Strategy", justify="center")

        for p in positions:
            pnl = p.get("unrealized_pnl", 0)
            pnl_style = "green" if pnl >= 0 else "red"
            table.add_row(
                p.get("question", "")[:40],
                p.get("side", ""),
                f"{p.get('size', 0):.1f}",
                f"${p.get('entry_price', 0):.3f}",
                f"${p.get('current_price', 0):.3f}",
                f"[{pnl_style}]${pnl:+.2f}[/{pnl_style}]",
                p.get("strategy", ""),
            )

        console.print(table)

    async def display_trades(self, limit: int = 20):
        """Display recent trades in a rich table."""
        trades_today = await self.db.get_trades_today()

        if not trades_today:
            console.print("[dim]No trades today")
            return

        table = Table(title="Today's Trades")
        table.add_column("Time", max_width=10)
        table.add_column("Market", max_width=35)
        table.add_column("Side", justify="center")
        table.add_column("Entry", justify="right")
        table.add_column("Exit", justify="right")
        table.add_column("Size", justify="right")
        table.add_column("P&L", justify="right")
        table.add_column("Status", justify="center")

        for t in trades_today[:limit]:
            pnl = t.get("pnl", 0)
            pnl_style = "green" if pnl >= 0 else "red"
            opened = t.get("opened_at")
            time_str = opened.strftime("%H:%M") if isinstance(opened, datetime) else ""

            table.add_row(
                time_str,
                t.get("question", "")[:35],
                t.get("side", ""),
                f"${t.get('entry_price', 0):.3f}",
                f"${t.get('exit_price', 0):.3f}" if t.get("exit_price") else "-",
                f"{t.get('size', 0):.1f}",
                f"[{pnl_style}]${pnl:+.2f}[/{pnl_style}]" if pnl else "-",
                t.get("status", ""),
            )

        console.print(table)

    async def display_pnl(self):
        """Display P&L summary."""
        summary = await self.get_summary()

        console.print("\n[bold]P&L Summary")
        console.print(f"  Open positions: {summary['open_positions']}")
        console.print(f"  Trades today:   {summary['trades_today']}")

        realized = summary["realized_pnl_today"]
        unrealized = summary["unrealized_pnl"]
        total = summary["total_pnl"]

        r_style = "green" if realized >= 0 else "red"
        u_style = "green" if unrealized >= 0 else "red"
        t_style = "green" if total >= 0 else "red"

        console.print(f"  Realized today: [{r_style}]${realized:+.2f}[/{r_style}]")
        console.print(f"  Unrealized:     [{u_style}]${unrealized:+.2f}[/{u_style}]")
        console.print(f"  Total P&L:      [{t_style}]${total:+.2f}[/{t_style}]")
        console.print(f"  Exposure:       ${summary['total_exposure']:.2f}")
