#!/usr/bin/env python3
"""Research pipeline analysis — dig into labeled signal snapshots.

Usage:
    .venv/bin/python scripts/research_analysis.py              # Full overnight report
    .venv/bin/python scripts/research_analysis.py --hours 4    # Last 4 hours only
"""

import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import click
from rich.console import Console
from rich.table import Table

console = Console()


async def _analyze(hours: int):
    from polyedge.core.config import load_config
    from polyedge.core.db import Database

    settings = load_config()
    db = Database(settings.database_url)
    await db.connect()

    try:
        async with db.pool.acquire() as conn:
            # --- Trade event snapshots with outcomes ---
            trades = await conn.fetch("""
                SELECT id, ts, features,
                       token_move_5s, token_move_10s, token_move_20s, token_move_30s,
                       max_favorable, max_adverse
                FROM polyedge.signal_snapshots
                WHERE trade_fired = true
                  AND outcome_labeled = true
                  AND ts >= NOW() - make_interval(hours => $1)
                ORDER BY ts ASC
            """, hours)

            if not trades:
                console.print(f"[yellow]No labeled trade snapshots in the last {hours} hours[/yellow]")
                return

            # --- Overall trade stats ---
            wins = 0
            losses = 0
            total_pnl_5s = 0.0
            total_pnl_10s = 0.0
            total_pnl_20s = 0.0
            total_pnl_30s = 0.0
            yes_wins = 0
            yes_losses = 0
            no_wins = 0
            no_losses = 0
            mfe_list = []
            mae_list = []
            exit_wouldve_won = 0
            exit_total = 0

            # By momentum bucket
            mom_buckets = {
                "0.50-0.60": {"wins": 0, "losses": 0, "pnl_10s": 0.0},
                "0.60-0.70": {"wins": 0, "losses": 0, "pnl_10s": 0.0},
                "0.70-0.80": {"wins": 0, "losses": 0, "pnl_10s": 0.0},
                "0.80+": {"wins": 0, "losses": 0, "pnl_10s": 0.0},
            }

            # By regime
            regime_stats = {}

            # By regime + direction (cross-tab)
            regime_dir_stats = {}

            # By exit reason
            exit_reason_stats = {}

            for row in trades:
                features = row["features"] if isinstance(row["features"], dict) else json.loads(row["features"])

                token_5s = row["token_move_5s"] or 0
                token_10s = row["token_move_10s"] or 0
                token_20s = row["token_move_20s"] or 0
                token_30s = row["token_move_30s"] or 0
                max_fav = row["max_favorable"] or 0
                max_adv = row["max_adverse"] or 0
                mfe_list.append(max_fav)
                mae_list.append(max_adv)

                total_pnl_5s += token_5s
                total_pnl_10s += token_10s
                total_pnl_20s += token_20s
                total_pnl_30s += token_30s

                # Win = token moved in our favor at 10s
                is_win = token_10s > 0
                if is_win:
                    wins += 1
                else:
                    losses += 1

                # Direction breakdown — trade_side has "yes"/"no", position_side is bugged
                position = features.get("trade_side", features.get("position_side", ""))
                if position and position.lower() == "yes":
                    if is_win:
                        yes_wins += 1
                    else:
                        yes_losses += 1
                elif position and position.lower() == "no":
                    if is_win:
                        no_wins += 1
                    else:
                        no_losses += 1

                # MFE check: would token have gone >2% in our favor within 30s?
                # (token_move_30s > 0 means it ended higher, which is good for our side)
                if token_30s > 0.02:
                    exit_wouldve_won += 1
                exit_total += 1

                # Momentum bucket
                mom = abs(features.get("dampened_momentum", features.get("raw_momentum", 0)))
                if mom >= 0.80:
                    bucket = "0.80+"
                elif mom >= 0.70:
                    bucket = "0.70-0.80"
                elif mom >= 0.60:
                    bucket = "0.60-0.70"
                else:
                    bucket = "0.50-0.60"

                mom_buckets[bucket]["pnl_10s"] += token_10s
                if is_win:
                    mom_buckets[bucket]["wins"] += 1
                else:
                    mom_buckets[bucket]["losses"] += 1

                # Regime tracking
                regime = features.get("regime", "unknown")
                if regime not in regime_stats:
                    regime_stats[regime] = {"wins": 0, "losses": 0, "pnl_10s": 0.0, "pnl_30s": 0.0}
                regime_stats[regime]["pnl_10s"] += token_10s
                regime_stats[regime]["pnl_30s"] += token_30s
                if is_win:
                    regime_stats[regime]["wins"] += 1
                else:
                    regime_stats[regime]["losses"] += 1

                # Exit reason tracking
                exit_reason = features.get("exit_reason", "")
                trade_action = features.get("trade_action", "")
                if trade_action == "exit" and exit_reason:
                    if exit_reason not in exit_reason_stats:
                        exit_reason_stats[exit_reason] = {"wins": 0, "losses": 0, "pnl_10s": 0.0, "pnl_30s": 0.0}
                    exit_reason_stats[exit_reason]["pnl_10s"] += token_10s
                    exit_reason_stats[exit_reason]["pnl_30s"] += token_30s
                    if is_win:
                        exit_reason_stats[exit_reason]["wins"] += 1
                    else:
                        exit_reason_stats[exit_reason]["losses"] += 1

                # Regime + direction cross-tab
                regime_dir_key = f"{regime}|{position.lower() if position else 'unknown'}"
                if regime_dir_key not in regime_dir_stats:
                    regime_dir_stats[regime_dir_key] = {"wins": 0, "losses": 0, "pnl_10s": 0.0}
                regime_dir_stats[regime_dir_key]["pnl_10s"] += token_10s
                if is_win:
                    regime_dir_stats[regime_dir_key]["wins"] += 1
                else:
                    regime_dir_stats[regime_dir_key]["losses"] += 1

            # --- Print results ---
            total = wins + losses
            console.print(f"\n[bold cyan]═══ Research Analysis ({hours}h) ═══[/bold cyan]\n")

            # Overall
            t = Table(title="Overall Trade Snapshots")
            t.add_column("Metric", style="cyan")
            t.add_column("Value", style="green")
            t.add_row("Total trades", str(total))
            t.add_row("Win rate (10s)", f"{wins/total*100:.1f}%" if total > 0 else "N/A")
            t.add_row("Wins / Losses", f"{wins} / {losses}")
            t.add_row("Avg token move 5s", f"{total_pnl_5s/total*100:+.2f}%" if total > 0 else "N/A")
            t.add_row("Avg token move 10s", f"{total_pnl_10s/total*100:+.2f}%" if total > 0 else "N/A")
            t.add_row("Avg token move 20s", f"{total_pnl_20s/total*100:+.2f}%" if total > 0 else "N/A")
            t.add_row("Avg token move 30s", f"{total_pnl_30s/total*100:+.2f}%" if total > 0 else "N/A")
            avg_mfe = sum(mfe_list) / len(mfe_list) if mfe_list else 0
            avg_mae = sum(mae_list) / len(mae_list) if mae_list else 0
            t.add_row("Avg MFE (max favorable)", f"{avg_mfe*100:+.2f}%")
            t.add_row("Avg MAE (max adverse)", f"{avg_mae*100:+.2f}%")
            t.add_row("Would've won if held", f"{exit_wouldve_won}/{exit_total} ({exit_wouldve_won/exit_total*100:.0f}%)" if exit_total > 0 else "N/A")
            console.print(t)

            # Direction breakdown
            t2 = Table(title="By Direction")
            t2.add_column("Side", style="cyan")
            t2.add_column("Wins", style="green")
            t2.add_column("Losses", style="red")
            t2.add_column("Win Rate", style="yellow")
            yes_total = yes_wins + yes_losses
            no_total = no_wins + no_losses
            t2.add_row("YES", str(yes_wins), str(yes_losses), f"{yes_wins/yes_total*100:.0f}%" if yes_total > 0 else "N/A")
            t2.add_row("NO", str(no_wins), str(no_losses), f"{no_wins/no_total*100:.0f}%" if no_total > 0 else "N/A")
            console.print(t2)

            # Momentum buckets
            t3 = Table(title="By Momentum at Entry")
            t3.add_column("Momentum", style="cyan")
            t3.add_column("Trades", style="white")
            t3.add_column("Win Rate", style="yellow")
            t3.add_column("Avg Move 10s", style="green")
            for bucket, data in mom_buckets.items():
                bt = data["wins"] + data["losses"]
                if bt == 0:
                    continue
                wr = data["wins"] / bt * 100
                avg = data["pnl_10s"] / bt * 100
                t3.add_row(bucket, str(bt), f"{wr:.0f}%", f"{avg:+.2f}%")
            console.print(t3)

            # By regime
            t_regime = Table(title="By Market Regime")
            t_regime.add_column("Regime", style="cyan")
            t_regime.add_column("Trades", style="white")
            t_regime.add_column("Win Rate", style="yellow")
            t_regime.add_column("Avg Move 10s", style="green")
            t_regime.add_column("Avg Move 30s", style="blue")
            for regime, data in sorted(regime_stats.items(), key=lambda x: x[1]["wins"] + x[1]["losses"], reverse=True):
                rt = data["wins"] + data["losses"]
                if rt == 0:
                    continue
                wr = data["wins"] / rt * 100
                avg_10 = data["pnl_10s"] / rt * 100
                avg_30 = data["pnl_30s"] / rt * 100
                t_regime.add_row(regime, str(rt), f"{wr:.0f}%", f"{avg_10:+.2f}%", f"{avg_30:+.2f}%")
            console.print(t_regime)

            # Regime x Direction cross-tab
            t_rd = Table(title="Regime × Direction")
            t_rd.add_column("Regime", style="cyan")
            t_rd.add_column("Side", style="white")
            t_rd.add_column("Trades", style="white")
            t_rd.add_column("Win Rate", style="yellow")
            t_rd.add_column("Avg Move 10s", style="green")
            for key in sorted(regime_dir_stats.keys()):
                data = regime_dir_stats[key]
                rt = data["wins"] + data["losses"]
                if rt < 2:  # Skip tiny samples
                    continue
                regime, direction = key.split("|")
                wr = data["wins"] / rt * 100
                avg = data["pnl_10s"] / rt * 100
                t_rd.add_row(regime, direction.upper(), str(rt), f"{wr:.0f}%", f"{avg:+.2f}%")
            console.print(t_rd)

            # By exit reason
            if exit_reason_stats:
                t_exit = Table(title="By Exit Reason")
                t_exit.add_column("Exit Reason", style="cyan")
                t_exit.add_column("Exits", style="white")
                t_exit.add_column("Win Rate", style="yellow")
                t_exit.add_column("Avg Move 10s", style="green")
                t_exit.add_column("Avg Move 30s", style="blue")
                for reason, data in sorted(exit_reason_stats.items(), key=lambda x: x[1]["wins"] + x[1]["losses"], reverse=True):
                    rt = data["wins"] + data["losses"]
                    if rt == 0:
                        continue
                    wr = data["wins"] / rt * 100
                    avg_10 = data["pnl_10s"] / rt * 100
                    avg_30 = data["pnl_30s"] / rt * 100
                    t_exit.add_row(reason, str(rt), f"{wr:.0f}%", f"{avg_10:+.2f}%", f"{avg_30:+.2f}%")
                console.print(t_exit)
            else:
                console.print("[dim]No exit reason data yet (needs bot restart to start logging)[/dim]")

            # --- Blocked signal analysis ---
            # For blocked signals, we check: if we HAD entered in the direction
            # the momentum was pointing, would it have been profitable?
            # Positive momentum → would've bought YES → BTC up = win
            # Negative momentum → would've bought NO → BTC down = win
            blocked = await conn.fetch("""
                SELECT features, btc_move_10s, btc_move_30s, token_move_10s, token_move_30s,
                       max_favorable
                FROM polyedge.signal_snapshots
                WHERE no_trade_reason != 'none'
                  AND outcome_labeled = true
                  AND ts >= NOW() - make_interval(hours => $1)
            """, hours)

            if blocked:
                blocked_wouldve_won = 0
                blocked_total = 0
                reason_stats = {}

                for row in blocked:
                    features = row["features"] if isinstance(row["features"], dict) else json.loads(row["features"])
                    btc_10s = row["btc_move_10s"] or 0
                    btc_30s = row["btc_move_30s"] or 0
                    mom = features.get("dampened_momentum", features.get("raw_momentum", 0))

                    reason = features.get("no_trade_reason", "unknown")

                    if reason not in reason_stats:
                        reason_stats[reason] = {"total": 0, "wouldve_won": 0}
                    reason_stats[reason]["total"] += 1
                    blocked_total += 1

                    # Would this have been a winner at 30s?
                    # Bullish signal (mom > 0) wins if BTC went up, bearish wins if BTC went down
                    wouldve_won = (mom > 0 and btc_30s > 0.0001) or (mom < 0 and btc_30s < -0.0001)
                    if wouldve_won:
                        reason_stats[reason]["wouldve_won"] += 1
                        blocked_wouldve_won += 1

                t4 = Table(title="Blocked Signals — Would They Have Won?")
                t4.add_column("Reason", style="cyan")
                t4.add_column("Blocked", style="white")
                t4.add_column("Would've Won", style="green")
                t4.add_column("Saved by Block", style="red")
                for reason, data in sorted(reason_stats.items(), key=lambda x: x[1]["total"], reverse=True):
                    saved = data["total"] - data["wouldve_won"]
                    t4.add_row(
                        reason,
                        str(data["total"]),
                        str(data["wouldve_won"]),
                        str(saved),
                    )
                t4.add_row(
                    "[bold]TOTAL[/bold]",
                    str(blocked_total),
                    str(blocked_wouldve_won),
                    str(blocked_total - blocked_wouldve_won),
                )
                console.print(t4)

    finally:
        await db.close()


@click.command()
@click.option("--hours", default=12, help="Hours of data to analyze")
def main(hours: int):
    asyncio.run(_analyze(hours))


if __name__ == "__main__":
    main()
