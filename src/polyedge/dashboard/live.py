"""Rich-based terminal dashboard for monitoring PolyEdge."""

from __future__ import annotations

import asyncio
from datetime import datetime

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from polyedge.core.db import Database
from polyedge.core.config import Settings

console = Console(force_terminal=True, force_jupyter=False)


class Dashboard:
    """Live terminal dashboard for PolyEdge."""

    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.running = False

    async def run(self, refresh_interval: float = 5.0):
        """Run the live dashboard."""
        self.running = True
        console.print("[bold green]PolyEdge Dashboard[/bold green] (Ctrl+C to exit)\n")

        while self.running:
            try:
                await self._render()
                await asyncio.sleep(refresh_interval)
            except KeyboardInterrupt:
                break
            except Exception as e:
                console.print(f"[red]Dashboard error: {e}")
                await asyncio.sleep(refresh_interval)

    async def _render(self):
        """Render one frame of the dashboard."""
        console.clear()

        # Header
        console.print(
            f"[bold cyan]PolyEdge Dashboard[/bold cyan] | "
            f"Mode: [bold]{self.settings.agent.mode}[/bold] | "
            f"{datetime.now().strftime('%H:%M:%S')}"
        )
        console.print("=" * 80)

        # Portfolio summary
        await self._render_portfolio()

        # Positions
        await self._render_positions()

        # Recent trades
        await self._render_recent_trades()

        # AI cost
        ai_cost = await self.db.get_ai_cost_today()
        console.print(
            f"\n[dim]AI cost today: ${ai_cost:.2f} / "
            f"${self.settings.ai.max_analysis_cost_per_day:.2f}[/dim]"
        )

    async def _render_portfolio(self):
        """Render portfolio summary panel."""
        positions = await self.db.get_open_positions()
        trades_today = await self.db.get_trades_today()

        total_exposure = sum(
            p.get("size", 0) * p.get("entry_price", 0) for p in positions
        )
        unrealized = sum(p.get("unrealized_pnl", 0) for p in positions)
        realized = sum(t.get("pnl", 0) for t in trades_today if t.get("pnl"))
        bankroll = 200.0 - total_exposure + unrealized

        console.print(
            f"\n[bold]Portfolio[/bold] | "
            f"Bankroll: ${bankroll:.2f} | "
            f"Exposure: ${total_exposure:.2f} ({total_exposure/200*100:.0f}%) | "
            f"Positions: {len(positions)}/{self.settings.risk.max_positions} | "
            f"Trades today: {len(trades_today)}/{self.settings.risk.max_trades_per_day}"
        )

        total_pnl = realized + unrealized
        pnl_style = "green" if total_pnl >= 0 else "red"
        console.print(
            f"  Realized: ${realized:+.2f} | "
            f"Unrealized: ${unrealized:+.2f} | "
            f"Total P&L: [{pnl_style}]${total_pnl:+.2f}[/{pnl_style}]"
        )

    async def _render_positions(self):
        """Render open positions table."""
        positions = await self.db.get_open_positions()

        if not positions:
            console.print("\n[dim]No open positions[/dim]")
            return

        table = Table(title="Open Positions", show_header=True, header_style="bold")
        table.add_column("Market", max_width=40)
        table.add_column("Side", justify="center", width=5)
        table.add_column("Size", justify="right", width=8)
        table.add_column("Entry", justify="right", width=8)
        table.add_column("Current", justify="right", width=8)
        table.add_column("P&L", justify="right", width=10)
        table.add_column("Strategy", width=12)

        for p in positions:
            pnl = p.get("unrealized_pnl", 0)
            style = "green" if pnl >= 0 else "red"
            table.add_row(
                p.get("question", "")[:40],
                p.get("side", ""),
                f"{p.get('size', 0):.1f}",
                f"${p.get('entry_price', 0):.3f}",
                f"${p.get('current_price', 0):.3f}",
                f"[{style}]${pnl:+.2f}[/{style}]",
                p.get("strategy", ""),
            )

        console.print(table)

    async def _render_recent_trades(self):
        """Render recent trades."""
        trades = await self.db.get_trades_today()

        if not trades:
            return

        table = Table(title="Today's Trades", show_header=True, header_style="bold")
        table.add_column("Time", width=6)
        table.add_column("Market", max_width=30)
        table.add_column("Side", width=5)
        table.add_column("Price", width=8)
        table.add_column("Size", width=8)
        table.add_column("P&L", width=10)
        table.add_column("Status", width=8)

        for t in trades[-10:]:  # Last 10
            pnl = t.get("pnl", 0)
            style = "green" if pnl > 0 else ("red" if pnl < 0 else "dim")
            opened = t.get("opened_at")
            time_str = opened.strftime("%H:%M") if isinstance(opened, datetime) else ""

            table.add_row(
                time_str,
                t.get("question", "")[:30],
                t.get("side", ""),
                f"${t.get('entry_price', 0):.3f}",
                f"{t.get('size', 0):.1f}",
                f"[{style}]${pnl:+.2f}[/{style}]" if pnl else "-",
                t.get("status", ""),
            )

        console.print(table)
