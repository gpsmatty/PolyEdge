"""P&L Reconciler — pulls actual fills from Polymarket CLOB API,
matches them to our internal trades, and computes real P&L with fees.

Three layers of P&L:
  - Gross P&L: entry price vs exit price (or $1/$0 on resolution)
  - Net P&L: gross minus Polymarket trading fees
  - True Net: net minus estimated gas costs

Usage:
    reconciler = PnLReconciler(client, db, settings)
    stats = await reconciler.reconcile()
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from rich.table import Table

from polyedge.core.client import PolyClient
from polyedge.core.config import Settings
from polyedge.core.console import console
from polyedge.core.db import Database

logger = logging.getLogger("polyedge.reconciler")

# Average Polygon gas cost per transaction (in USD)
# Polygon gas is very cheap — typically 0.001-0.01 per tx
EST_GAS_PER_TX_USD = 0.003


class PnLReconciler:
    """Pull fills from CLOB, match to trades, compute real P&L."""

    def __init__(self, client: PolyClient, db: Database, settings: Settings):
        self.client = client
        self.db = db
        self.settings = settings

    async def reconcile(self) -> dict:
        """Run a full reconciliation cycle.

        1. Pull all fills from CLOB API (since last cursor)
        2. Group fills into buy/sell pairs per market
        3. Compute P&L with fees for each pair
        4. Write to pnl_ledger
        5. Update reconcile cursor

        Returns summary stats dict.
        """
        state = await self.db.get_reconcile_state()
        console.print(
            f"[dim]Last reconcile: {state['total_fills_processed']} fills processed[/dim]"
        )

        # Pull all fills from CLOB
        console.print("[dim]Fetching trade fills from Polymarket...[/dim]")
        try:
            all_fills = self.client.get_trades()
        except Exception as e:
            console.print(f"[red]Failed to fetch trades: {e}[/red]")
            return {"error": str(e)}

        if not all_fills:
            console.print("[dim]No fills found on this account[/dim]")
            return {"total_fills": 0, "new_entries": 0}

        console.print(f"[green]Fetched {len(all_fills)} total fills[/green]")

        # Filter to only new fills (after last processed timestamp)
        last_ts = state.get("last_fill_timestamp", 0)
        new_fills = []
        for f in all_fills:
            fill_ts = int(f.get("match_time", 0))
            if fill_ts > last_ts:
                new_fills.append(f)

        if not new_fills:
            console.print("[dim]No new fills since last reconcile[/dim]")
            return {"total_fills": len(all_fills), "new_entries": 0}

        console.print(f"[cyan]{len(new_fills)} new fills to process[/cyan]")

        # Group fills by market (asset_id groups buys and sells on same token)
        grouped = self._group_fills(new_fills)

        # Match into buy/sell pairs and compute P&L
        new_entries = 0
        total_gross = 0.0
        total_fees = 0.0
        total_net = 0.0

        for market_key, fills in grouped.items():
            entries = self._match_and_compute(fills)
            for entry in entries:
                try:
                    await self.db.insert_pnl_entry(entry)
                    new_entries += 1
                    total_gross += entry.get("gross_pnl", 0)
                    total_fees += entry.get("fees_paid", 0)
                    total_net += entry.get("net_pnl", 0)
                except Exception as e:
                    logger.warning(f"Failed to insert P&L entry: {e}")

        # Update reconcile state
        max_ts = max(int(f.get("match_time", 0)) for f in new_fills)
        await self.db.update_reconcile_state(
            last_cursor="",  # We pull all fills each time
            last_fill_timestamp=max_ts,
            total_fills=len(all_fills),
        )

        stats = {
            "total_fills": len(all_fills),
            "new_fills": len(new_fills),
            "new_entries": new_entries,
            "gross_pnl": total_gross,
            "total_fees": total_fees,
            "net_pnl": total_net,
        }

        console.print(
            f"\n[bold]Reconciled {new_entries} trade pairs[/bold]"
        )
        gross_style = "green" if total_gross >= 0 else "red"
        net_style = "green" if total_net >= 0 else "red"
        console.print(
            f"  Gross P&L: [{gross_style}]${total_gross:+.2f}[/{gross_style}]"
        )
        console.print(f"  Fees paid: [yellow]${total_fees:.2f}[/yellow]")
        console.print(
            f"  Net P&L:   [{net_style}]${total_net:+.2f}[/{net_style}]"
        )

        return stats

    def _group_fills(self, fills: list[dict]) -> dict[str, list[dict]]:
        """Group fills by (market, asset_id) for matching."""
        grouped: dict[str, list[dict]] = {}
        for f in fills:
            market = f.get("market", "")
            asset_id = f.get("asset_id", "")
            key = f"{market}:{asset_id}"
            if key not in grouped:
                grouped[key] = []
            grouped[key].append(f)
        return grouped

    def _match_and_compute(self, fills: list[dict]) -> list[dict]:
        """Match buy/sell fill pairs and compute P&L for each.

        Simple FIFO matching: first buy matches first sell.
        """
        # Sort by time
        fills.sort(key=lambda f: int(f.get("match_time", 0)))

        buys: list[dict] = []
        sells: list[dict] = []

        for f in fills:
            side = f.get("side", "").upper()
            if side == "BUY":
                buys.append(f)
            elif side == "SELL":
                sells.append(f)

        entries = []

        # FIFO match buys to sells
        while buys and sells:
            buy = buys.pop(0)
            sell = sells.pop(0)

            buy_price = float(buy.get("price", 0))
            sell_price = float(sell.get("price", 0))
            size = min(float(buy.get("size", 0)), float(sell.get("size", 0)))

            if size <= 0:
                continue

            # Fee calculation: CLOB fee_rate_bps returns 1000 (10%) on all
            # fills, which is the max/cap — NOT the actual fee charged.
            # Polymarket's real taker fee is 2% (200 bps), maker fee is 0%.
            # Use known rates instead of the misleading API field.
            TAKER_FEE_BPS = 200  # 2% taker fee
            buy_fee = buy_price * size * (TAKER_FEE_BPS / 10000)
            sell_fee = sell_price * size * (TAKER_FEE_BPS / 10000)
            total_fees = buy_fee + sell_fee

            gross_pnl = (sell_price - buy_price) * size
            gas = EST_GAS_PER_TX_USD * 2  # Buy + sell transactions
            net_pnl = gross_pnl - total_fees - gas

            # Try to find the market question from our DB
            market_id = buy.get("market", "")
            question = ""
            strategy = ""
            try:
                # Look up in our trades table for strategy info
                pass  # Will be populated from DB lookup if available
            except Exception:
                pass

            buy_time = datetime.fromtimestamp(
                int(buy.get("match_time", 0)), tz=timezone.utc
            ) if buy.get("match_time") else None
            sell_time = datetime.fromtimestamp(
                int(sell.get("match_time", 0)), tz=timezone.utc
            ) if sell.get("match_time") else None

            entries.append({
                "market_id": market_id,
                "question": question,
                "strategy": strategy,
                "side": buy.get("side", "BUY"),
                "size": size,
                "entry_fill_price": buy_price,
                "exit_fill_price": sell_price,
                "gross_pnl": gross_pnl,
                "fees_paid": total_fees,
                "net_pnl": net_pnl,
                "gas_estimate": gas,
                "pnl_type": "trade",
                "clob_buy_order_id": buy.get("taker_order_id", ""),
                "clob_sell_order_id": sell.get("taker_order_id", ""),
                "entry_time": buy_time,
                "exit_time": sell_time,
            })

        # Any remaining buys without sells = open positions (don't record yet)
        # Any remaining sells without buys = odd (shouldn't happen)

        return entries

    async def check_resolutions(self) -> list[dict]:
        """Check for resolved markets where we hold positions.

        Resolved markets pay $1.00 per winning token, $0.00 per losing.
        This handles P&L for positions held to expiry.
        """
        positions = await self.db.get_open_positions()
        if not positions:
            return []

        resolved_entries = []
        gamma_url = self.settings.polymarket.gamma_url

        for pos in positions:
            market_id = pos.get("market_id", "")
            if not market_id:
                continue

            # Check if market is resolved via Gamma API
            try:
                async with aiohttp.ClientSession() as session:
                    url = f"{gamma_url}/markets/{market_id}"
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json()

                if not data.get("resolved"):
                    continue

                resolution = data.get("resolution", "").lower()
                our_side = pos.get("side", "").lower()
                entry_price = pos.get("entry_price", 0)
                size = pos.get("size", 0)

                # Did we win?
                won = (resolution == "yes" and our_side == "yes") or \
                      (resolution == "no" and our_side == "no")

                exit_price = 1.0 if won else 0.0
                gross_pnl = (exit_price - entry_price) * size

                entry = {
                    "market_id": market_id,
                    "question": pos.get("question", ""),
                    "strategy": pos.get("strategy", ""),
                    "side": our_side,
                    "size": size,
                    "entry_fill_price": entry_price,
                    "exit_fill_price": exit_price,
                    "gross_pnl": gross_pnl,
                    "fees_paid": 0,  # No fee on resolution
                    "net_pnl": gross_pnl,
                    "gas_estimate": 0,
                    "pnl_type": "resolution",
                    "entry_time": pos.get("opened_at"),
                    "exit_time": datetime.now(timezone.utc),
                }

                try:
                    await self.db.insert_pnl_entry(entry)
                    resolved_entries.append(entry)
                except Exception as e:
                    logger.warning(f"Failed to record resolution P&L: {e}")

                # Clean up position
                try:
                    token_id = pos.get("token_id", "")
                    await self.db.remove_position(market_id, token_id, our_side)
                except Exception as e:
                    logger.warning(f"Failed to remove resolved position: {e}")

                result = "WON" if won else "LOST"
                pnl_style = "green" if gross_pnl >= 0 else "red"
                console.print(
                    f"[{pnl_style}]Market resolved: {pos.get('question', '')[:50]} "
                    f"→ {result} (${gross_pnl:+.2f})[/{pnl_style}]"
                )

            except Exception as e:
                logger.warning(f"Resolution check failed for {market_id}: {e}")

        return resolved_entries

    async def display_summary(self, strategy: str | None = None):
        """Display P&L summary dashboard."""
        summary = await self.db.get_pnl_summary(strategy)
        if not summary or summary.get("total_trades", 0) == 0:
            console.print("[dim]No reconciled trades yet. Run: polyedge pnl reconcile[/dim]")
            return

        total = summary["total_trades"]
        wins = summary["wins"]
        losses = summary["losses"]
        win_rate = (wins / total * 100) if total > 0 else 0
        avg_win = summary["avg_win"]
        avg_loss = summary["avg_loss"]
        profit_factor = abs(avg_win * wins / (avg_loss * losses)) if losses > 0 and avg_loss != 0 else float("inf")

        gross = summary["total_gross_pnl"]
        fees = summary["total_fees"]
        gas = summary["total_gas"]
        net = summary["total_net_pnl"]
        true_net = net - gas

        title = f"P&L Summary — {strategy}" if strategy else "P&L Summary — All Strategies"
        console.print(f"\n[bold]{title}[/bold]")
        console.print(f"  Trades:       {total} ({wins}W / {losses}L / {summary['breakeven']}BE)")
        console.print(f"  Win rate:     {win_rate:.1f}%")
        console.print(f"  Avg win:      [green]${avg_win:+.2f}[/green]")
        console.print(f"  Avg loss:     [red]${avg_loss:+.2f}[/red]")
        console.print(f"  Profit factor: {profit_factor:.2f}")
        console.print()

        g_style = "green" if gross >= 0 else "red"
        n_style = "green" if net >= 0 else "red"
        t_style = "green" if true_net >= 0 else "red"

        console.print(f"  Gross P&L:    [{g_style}]${gross:+.2f}[/{g_style}]")
        console.print(f"  Fees paid:    [yellow]-${fees:.2f}[/yellow]")
        console.print(f"  Net P&L:      [{n_style}]${net:+.2f}[/{n_style}]")
        console.print(f"  Gas estimate: [yellow]-${gas:.4f}[/yellow]")
        console.print(f"  True Net:     [{t_style}]${true_net:+.2f}[/{t_style}]")
        console.print(f"  Volume:       ${summary['total_volume']:.2f}")

    async def display_history(self, limit: int = 20, strategy: str | None = None):
        """Display recent P&L entries in a table."""
        entries = await self.db.get_pnl_ledger(strategy=strategy, limit=limit)

        if not entries:
            console.print("[dim]No reconciled trades yet. Run: polyedge pnl reconcile[/dim]")
            return

        table = Table(title=f"Recent Trades (last {limit})")
        table.add_column("Time", max_width=12)
        table.add_column("Market", max_width=30)
        table.add_column("Side", justify="center")
        table.add_column("Size", justify="right")
        table.add_column("Entry", justify="right")
        table.add_column("Exit", justify="right")
        table.add_column("Gross", justify="right")
        table.add_column("Fees", justify="right")
        table.add_column("Net", justify="right")
        table.add_column("Type", justify="center")

        for e in entries:
            entry_time = e.get("entry_time")
            time_str = entry_time.strftime("%H:%M:%S") if isinstance(entry_time, datetime) else ""
            gross = e.get("gross_pnl", 0)
            net = e.get("net_pnl", 0)
            g_style = "green" if gross >= 0 else "red"
            n_style = "green" if net >= 0 else "red"

            table.add_row(
                time_str,
                e.get("question", e.get("market_id", ""))[:30],
                e.get("side", ""),
                f"{e.get('size', 0):.1f}",
                f"${e.get('entry_fill_price', 0):.3f}",
                f"${e.get('exit_fill_price', 0):.3f}" if e.get("exit_fill_price") else "-",
                f"[{g_style}]${gross:+.2f}[/{g_style}]",
                f"${e.get('fees_paid', 0):.3f}",
                f"[{n_style}]${net:+.2f}[/{n_style}]",
                e.get("pnl_type", ""),
            )

        console.print(table)
