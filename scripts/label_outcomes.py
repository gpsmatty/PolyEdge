"""Offline outcome labeling for signal snapshots.

For every unlabeled snapshot, computes future outcomes:
  - BTC price move after 5s / 10s / 20s / 30s
  - Token (YES/NO) price move after 5s / 10s / 20s / 30s
  - Max favorable excursion (best move in our direction within 30s)
  - Max adverse excursion (worst move against us within 30s)

Uses the signal_snapshots table itself as the price source — snapshots
are logged every 2-3 seconds, so we interpolate between them.

Usage:
    python scripts/label_outcomes.py              # Label up to 1000 unlabeled
    python scripts/label_outcomes.py --limit 5000 # Label more
    python scripts/label_outcomes.py --stats       # Show pipeline stats
"""

import asyncio
import json
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import click
from rich.console import Console
from rich.table import Table

console = Console()


async def _label(limit: int):
    """Label unlabeled snapshots with future outcomes."""
    from polyedge.core.config import load_config
    from polyedge.core.db import Database

    settings = load_config()
    db = Database(settings.database_url)
    await db.connect()

    try:
        # Get unlabeled snapshots
        unlabeled = await db.get_snapshots_for_labeling(limit=limit)
        if not unlabeled:
            console.print("[green]All snapshots are labeled![/green]")
            return

        console.print(f"[cyan]Found {len(unlabeled)} unlabeled snapshots to process[/cyan]")

        # Load ALL snapshots in the time range for price lookups
        # (we need future prices relative to each unlabeled snapshot)
        min_ts = unlabeled[0]["ts"]
        max_ts = unlabeled[-1]["ts"]

        async with db.pool.acquire() as conn:
            # Get all snapshots in range + 60s buffer for future lookups
            all_rows = await conn.fetch(
                """
                SELECT id, ts, symbol, features
                FROM polyedge.signal_snapshots
                WHERE ts >= $1 AND ts <= ($2::timestamptz + INTERVAL '60 seconds')
                ORDER BY ts ASC
                """,
                min_ts, max_ts,
            )

        # Build price timeline per symbol: [(timestamp, btc_price, yes_price, no_price)]
        timelines: dict[str, list[tuple]] = {}
        for row in all_rows:
            features = row["features"] if isinstance(row["features"], dict) else json.loads(row["features"])
            sym = features.get("symbol", "")
            ts = row["ts"].timestamp()
            btc = features.get("btc_price", 0)
            yes_p = features.get("yes_price", 0)
            no_p = features.get("no_price", 0)
            if sym and btc > 0:
                if sym not in timelines:
                    timelines[sym] = []
                timelines[sym].append((ts, btc, yes_p, no_p))

        # Label each unlabeled snapshot
        labels = []
        labeled_count = 0
        horizons = [5, 10, 20, 30]

        for snap in unlabeled:
            features = snap["features"] if isinstance(snap["features"], dict) else json.loads(snap["features"])
            sym = features.get("symbol", "")
            snap_ts = snap["ts"].timestamp()
            snap_btc = features.get("btc_price", 0)
            snap_yes = features.get("yes_price", 0)
            snap_no = features.get("no_price", 0)
            position = features.get("current_position", "")

            timeline = timelines.get(sym, [])
            if not timeline or snap_btc <= 0:
                continue

            label = {"id": snap["id"]}

            # Find future prices at each horizon
            for h in horizons:
                target_ts = snap_ts + h
                # Find closest snapshot at or after target time
                future_btc = None
                future_yes = None
                future_no = None
                for ts, btc, yes_p, no_p in timeline:
                    if ts >= target_ts:
                        future_btc = btc
                        future_yes = yes_p
                        future_no = no_p
                        break

                if future_btc is not None and snap_btc > 0:
                    label[f"btc_move_{h}s"] = (future_btc - snap_btc) / snap_btc
                else:
                    label[f"btc_move_{h}s"] = None

                # Token move depends on which token we care about
                if position == "yes" and future_yes is not None and snap_yes > 0:
                    label[f"token_move_{h}s"] = (future_yes - snap_yes) / snap_yes
                elif position == "no" and future_no is not None and snap_no > 0:
                    label[f"token_move_{h}s"] = (future_no - snap_no) / snap_no
                elif future_yes is not None and snap_yes > 0:
                    # Default to YES token for flat positions
                    label[f"token_move_{h}s"] = (future_yes - snap_yes) / snap_yes
                else:
                    label[f"token_move_{h}s"] = None

            # Max favorable / adverse excursion within 30s
            max_fav = 0.0
            max_adv = 0.0
            for ts, btc, yes_p, no_p in timeline:
                if ts <= snap_ts:
                    continue
                if ts > snap_ts + 30:
                    break
                btc_move = (btc - snap_btc) / snap_btc if snap_btc > 0 else 0

                if position == "yes":
                    token_move = (yes_p - snap_yes) / snap_yes if snap_yes > 0 else 0
                elif position == "no":
                    token_move = (no_p - snap_no) / snap_no if snap_no > 0 else 0
                else:
                    token_move = btc_move  # Use BTC as proxy for flat

                max_fav = max(max_fav, token_move)
                max_adv = min(max_adv, token_move)

            label["max_favorable"] = max_fav
            label["max_adverse"] = max_adv
            labels.append(label)
            labeled_count += 1

        if labels:
            await db.label_snapshot_outcomes(labels)
            console.print(f"[green]Labeled {labeled_count} snapshots with future outcomes[/green]")
        else:
            console.print("[yellow]No snapshots could be labeled (missing future price data)[/yellow]")

    finally:
        await db.close()


async def _stats():
    """Show research pipeline statistics."""
    from polyedge.core.config import load_config
    from polyedge.core.db import Database

    settings = load_config()
    db = Database(settings.database_url)
    await db.connect()

    try:
        stats = await db.get_snapshot_stats()

        table = Table(title="Research Pipeline Stats")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green")

        table.add_row("Total snapshots", f"{stats['total_snapshots']:,}")
        table.add_row("Labeled", f"{stats['labeled']:,}")
        table.add_row("Unlabeled", f"{stats['unlabeled']:,}")
        table.add_row("Trade events", f"{stats['trades']:,}")
        table.add_row("Candidate events", f"{stats['candidates']:,}")
        table.add_row("No-trade blocked", f"{stats['no_trade_blocked']:,}")

        console.print(table)

        if stats["regimes"]:
            regime_table = Table(title="Regime Distribution")
            regime_table.add_column("Regime", style="cyan")
            regime_table.add_column("Count", style="green")
            regime_table.add_column("Pct", style="yellow")
            total = stats["total_snapshots"]
            for regime, count in stats["regimes"].items():
                pct = count / total * 100 if total > 0 else 0
                regime_table.add_row(regime, f"{count:,}", f"{pct:.1f}%")
            console.print(regime_table)

    finally:
        await db.close()


@click.command()
@click.option("--limit", default=1000, help="Max snapshots to label per run")
@click.option("--stats", "show_stats", is_flag=True, help="Show pipeline stats")
def main(limit: int, show_stats: bool):
    if show_stats:
        asyncio.run(_stats())
    else:
        asyncio.run(_label(limit))


if __name__ == "__main__":
    main()
