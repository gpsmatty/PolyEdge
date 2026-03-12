#!/usr/bin/env python3
"""Comprehensive trade attribution analysis for PolyEdge micro sniper.

Usage:
    python scripts/trade_analysis.py                    # Full report (last 24h)
    python scripts/trade_analysis.py --hours 4          # Last 4 hours
    python scripts/trade_analysis.py --strategy micro   # Micro sniper only
    python scripts/trade_analysis.py --from-date 2026-03-10  # From specific date
    python scripts/trade_analysis.py --json             # JSON output for parsing

Connects via load_config() (Keychain → env → .env → YAML).
Override with DATABASE_URL env var if needed.

Data Source: Pulls from polyedge.trades table with signal_data and config_snapshot
JSONB fields populated by micro_runner.py at entry time.

Analysis Dimensions:
- Overall stats (win rate, P&L, hold time, gross profit/loss)
- By Entry Quality: Buckets by momentum score at entry (0.55-0.65, 0.65-0.75, etc.)
- By Direction: YES entries vs NO entries
- By Trend Alignment: With-trend vs counter-trend vs no-trend
- By Time Remaining: Entry timing relative to market close
- By Exit Reason: Momentum reversal, trailing stop, force exit, floor exit
- Signal Components: OFI, VWAP drift, intensity — winners vs losers
- FOK Rejections: Unpaired buy orders that never closed
- Execution Quality: Slippage analysis on entry and exit

Note: Older trades without signal_data/config_snapshot are gracefully skipped
from component analysis but included in overall P&L calculation.
"""

import asyncio
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from collections import defaultdict
import statistics

# Add project root to path so we can import polyedge
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))


async def get_db():
    """Connect to DB using polyedge config."""
    from polyedge.core.config import load_config
    from polyedge.core.db import Database

    settings = load_config()
    db = Database(settings.database_url)
    await db.connect()
    return db


class TradeAnalyzer:
    """Comprehensive trade attribution analysis."""

    def __init__(self):
        self.trades = []  # All trades with entry and exit
        self.entry_trades = []  # Entry (BUY) trades
        self.exit_trades = []  # Exit (SELL) trades
        self.signal_data = {}  # Market ID -> signal data at entry
        self.config_data = {}  # Market ID -> config snapshot at entry
        self.paired_trades = []  # (entry_trade, exit_trade, pnl, hold_time)

    async def load_trades(self, db, hours: Optional[int] = None, from_date: Optional[str] = None, strategy: str = "micro_sniper"):
        """Load all micro sniper trades (entry and exit) with signal data."""

        # Build WHERE clause
        where_clauses = [f"strategy = '{strategy}'"]

        if from_date:
            where_clauses.append(f"opened_at >= '{from_date}'::date")
        elif hours:
            where_clauses.append(f"opened_at > NOW() - INTERVAL '{hours} hours'")
        else:
            # Default to last 24 hours
            where_clauses.append("opened_at > NOW() - INTERVAL '24 hours'")

        where_sql = " AND ".join(where_clauses)

        sql = f"""
            SELECT
                trade_id, market_id, token_id, question, side,
                entry_price, exit_price, size, pnl,
                status, strategy, reasoning,
                ai_probability,
                config_snapshot, signal_data,
                exit_reason,
                opened_at, closed_at
            FROM polyedge.trades
            WHERE {where_sql}
            ORDER BY opened_at ASC
        """

        rows = await db.pool.fetch(sql)
        self.trades = [dict(r) for r in rows]

        # Organize by entry/exit and pair them up
        self._organize_trades()

    def _organize_trades(self):
        """Organize trades into completed pairs.

        Micro sniper stores ONE row per trade with side='YES'/'NO',
        entry_price, exit_price, pnl, status='CLOSED'/'OPEN'.
        A completed trade has status='CLOSED' with both entry and exit prices.
        """
        for trade in self.trades:
            market_id = trade['market_id']

            # Parse signal and config JSONB data
            if trade['signal_data']:
                if isinstance(trade['signal_data'], dict):
                    self.signal_data[market_id] = trade['signal_data']
                elif isinstance(trade['signal_data'], str):
                    try:
                        self.signal_data[market_id] = json.loads(trade['signal_data'])
                    except (json.JSONDecodeError, TypeError):
                        pass
            if trade['config_snapshot']:
                if isinstance(trade['config_snapshot'], dict):
                    self.config_data[market_id] = trade['config_snapshot']
                elif isinstance(trade['config_snapshot'], str):
                    try:
                        self.config_data[market_id] = json.loads(trade['config_snapshot'])
                    except (json.JSONDecodeError, TypeError):
                        pass

            self.entry_trades.append(trade)

            # Completed trade: has exit_price and is CLOSED
            if trade['status'] == 'CLOSED' and trade['exit_price'] is not None:
                entry_price = float(trade['entry_price'])
                exit_price = float(trade['exit_price'])
                size = float(trade['size'])

                # Use stored pnl if available, else compute
                if trade['pnl'] is not None:
                    gross_pnl = float(trade['pnl'])
                else:
                    gross_pnl = (exit_price - entry_price) * size

                # Hold time
                if trade['opened_at'] and trade['closed_at']:
                    hold_time = (trade['closed_at'] - trade['opened_at']).total_seconds()
                else:
                    hold_time = 0

                self.paired_trades.append({
                    'entry': trade,
                    'exit': trade,  # Same row — exit data is on the same trade row
                    'gross_pnl': gross_pnl,
                    'hold_time_seconds': hold_time,
                    'market_id': market_id,
                })
            elif trade['status'] == 'OPEN':
                # Still open — track but don't pair
                pass

    def get_overall_stats(self) -> dict:
        """Overall statistics."""
        if not self.paired_trades:
            return {
                'total_completed_trades': 0,
                'total_p_and_l': 0,
                'win_rate': 'N/A',
                'total_wins': 0,
                'total_losses': 0,
                'gross_profit': 0,
                'gross_loss': 0,
                'largest_winner': 0,
                'largest_loser': 0,
                'avg_p_and_l_per_trade': 0,
                'avg_hold_time_seconds': 0,
                'avg_hold_time_readable': '0s',
            }

        pnls = [t['gross_pnl'] for t in self.paired_trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]

        total_pnl = sum(pnls)
        gross_profit = sum(wins) if wins else 0
        gross_loss = sum(losses) if losses else 0

        hold_times = [t['hold_time_seconds'] for t in self.paired_trades if t['hold_time_seconds'] > 0]
        avg_hold_time = statistics.mean(hold_times) if hold_times else 0

        return {
            'total_completed_trades': len(self.paired_trades),
            'total_p_and_l': round(total_pnl, 4),
            'win_rate': f"{len(wins)/len(self.paired_trades)*100:.1f}%" if self.paired_trades else "N/A",
            'total_wins': len(wins),
            'total_losses': len(losses),
            'gross_profit': round(gross_profit, 4),
            'gross_loss': round(gross_loss, 4),
            'largest_winner': round(max(wins, default=0), 4),
            'largest_loser': round(min(losses, default=0), 4),
            'avg_p_and_l_per_trade': round(total_pnl / len(self.paired_trades), 4) if self.paired_trades else 0,
            'avg_hold_time_seconds': round(avg_hold_time, 1),
            'avg_hold_time_readable': self._format_seconds(avg_hold_time),
        }

    def analyze_by_entry_quality(self) -> dict:
        """Analyze by momentum score at entry."""
        buckets = {
            '0.55-0.65': [],
            '0.65-0.75': [],
            '0.75-0.85': [],
            '0.85+': [],
        }

        for trade in self.paired_trades:
            market_id = trade['market_id']
            if market_id not in self.signal_data:
                continue

            signal = self.signal_data[market_id]
            momentum = float(signal.get('momentum', 0))

            if 0.55 <= momentum < 0.65:
                bucket = '0.55-0.65'
            elif 0.65 <= momentum < 0.75:
                bucket = '0.65-0.75'
            elif 0.75 <= momentum < 0.85:
                bucket = '0.75-0.85'
            elif momentum >= 0.85:
                bucket = '0.85+'
            else:
                continue

            buckets[bucket].append(trade)

        results = {}
        for bucket, trades in buckets.items():
            if not trades:
                results[bucket] = {
                    'count': 0,
                    'win_rate': 'N/A',
                    'avg_pnl': 0,
                }
                continue

            pnls = [t['gross_pnl'] for t in trades]
            wins = sum(1 for p in pnls if p > 0)

            results[bucket] = {
                'count': len(trades),
                'win_rate': f"{wins/len(trades)*100:.1f}%",
                'avg_pnl': round(sum(pnls) / len(trades), 4),
                'total_pnl': round(sum(pnls), 4),
            }

        return results

    def analyze_by_direction(self) -> dict:
        """YES entries vs NO entries."""
        yes_trades = [t for t in self.paired_trades if t['entry']['side'] == 'YES']
        no_trades = [t for t in self.paired_trades if t['entry']['side'] == 'NO']

        def analyze_group(trades):
            if not trades:
                return {'count': 0, 'win_rate': 'N/A', 'avg_pnl': 0}
            pnls = [t['gross_pnl'] for t in trades]
            wins = sum(1 for p in pnls if p > 0)
            return {
                'count': len(trades),
                'win_rate': f"{wins/len(trades)*100:.1f}%",
                'avg_pnl': round(sum(pnls) / len(trades), 4),
                'total_pnl': round(sum(pnls), 4),
            }

        return {
            'YES': analyze_group(yes_trades),
            'NO': analyze_group(no_trades),
        }

    def analyze_by_trend_alignment(self) -> dict:
        """Analyze by trend alignment at entry."""
        with_trend = []
        counter_trend = []
        no_trend = []

        for trade in self.paired_trades:
            market_id = trade['market_id']
            if market_id not in self.signal_data:
                continue

            signal = self.signal_data[market_id]
            trend_5m = signal.get('trend_5m', 0)
            momentum = float(signal.get('momentum', 0))
            entry_side = trade['entry']['side']

            # Determine trend direction
            if momentum > 0.50:  # Bullish momentum
                expected_side = 'YES'
            elif momentum < -0.50:  # Bearish momentum
                expected_side = 'NO'
            else:
                no_trend.append(trade)
                continue

            # Check if entry aligns with trend
            if str(entry_side) == expected_side:
                with_trend.append(trade)
            else:
                counter_trend.append(trade)

        def analyze_group(trades):
            if not trades:
                return {'count': 0, 'win_rate': 'N/A', 'avg_pnl': 0}
            pnls = [t['gross_pnl'] for t in trades]
            wins = sum(1 for p in pnls if p > 0)
            return {
                'count': len(trades),
                'win_rate': f"{wins/len(trades)*100:.1f}%",
                'avg_pnl': round(sum(pnls) / len(trades), 4),
                'total_pnl': round(sum(pnls), 4),
            }

        return {
            'with_trend': analyze_group(with_trend),
            'counter_trend': analyze_group(counter_trend),
            'no_trend': analyze_group(no_trend),
        }

    def analyze_by_time_remaining(self) -> dict:
        """Analyze by seconds remaining at entry."""
        buckets = {
            '800-600s': [],
            '600-400s': [],
            '400-200s': [],
            '200-120s': [],
            '<120s': [],
        }

        for trade in self.paired_trades:
            market_id = trade['market_id']
            if market_id not in self.signal_data:
                continue

            signal = self.signal_data[market_id]
            secs = float(signal.get('seconds_remaining', 0))

            if 600 <= secs <= 800:
                bucket = '800-600s'
            elif 400 <= secs < 600:
                bucket = '600-400s'
            elif 200 <= secs < 400:
                bucket = '400-200s'
            elif 120 <= secs < 200:
                bucket = '200-120s'
            elif secs < 120:
                bucket = '<120s'
            else:
                continue

            buckets[bucket].append(trade)

        results = {}
        for bucket, trades in buckets.items():
            if not trades:
                results[bucket] = {
                    'count': 0,
                    'win_rate': 'N/A',
                    'avg_pnl': 0,
                }
                continue

            pnls = [t['gross_pnl'] for t in trades]
            wins = sum(1 for p in pnls if p > 0)

            results[bucket] = {
                'count': len(trades),
                'win_rate': f"{wins/len(trades)*100:.1f}%",
                'avg_pnl': round(sum(pnls) / len(trades), 4),
                'total_pnl': round(sum(pnls), 4),
            }

        return results

    def analyze_by_exit_reason(self) -> dict:
        """Analyze by exit reason from DB exit_reason column."""
        results = defaultdict(list)

        for trade in self.paired_trades:
            exit_trade = trade['exit']
            reason = exit_trade.get('exit_reason', '').strip()

            # Use DB exit_reason if available, otherwise infer
            if not reason:
                reasoning = exit_trade.get('reasoning', '').lower()
                if 'trailing' in reasoning or 'stop' in reasoning:
                    reason = 'trailing_stop'
                elif 'force' in reasoning or 'forced' in reasoning:
                    reason = 'force_exit'
                elif 'floor' in reasoning or 'slippage' in reasoning:
                    reason = 'floor_exit'
                elif trade['hold_time_seconds'] < 5:
                    reason = 'force_exit'
                else:
                    reason = 'unknown'

            results[reason].append(trade)

        analyzed = {}
        for reason, trades in results.items():
            if not trades:
                continue
            pnls = [t['gross_pnl'] for t in trades]
            wins = sum(1 for p in pnls if p > 0)

            analyzed[reason] = {
                'count': len(trades),
                'win_rate': f"{wins/len(trades)*100:.1f}%" if trades else 'N/A',
                'avg_pnl': round(sum(pnls) / len(trades), 4) if trades else 0,
                'total_pnl': round(sum(pnls), 4),
            }

        return analyzed

    def analyze_signal_components(self) -> dict:
        """Analyze signal component correlation with P&L."""
        winners = [t for t in self.paired_trades if t['gross_pnl'] > 0]
        losers = [t for t in self.paired_trades if t['gross_pnl'] < 0]

        def extract_components(trades):
            components = {
                'momentum': [],
                'confidence': [],
                'ofi_5s': [],
                'ofi_15s': [],
                'vwap_drift': [],
                'trade_intensity': [],
            }

            for trade in trades:
                market_id = trade['market_id']
                if market_id not in self.signal_data:
                    continue

                signal = self.signal_data[market_id]
                for key in components:
                    val = signal.get(key)
                    if val is not None:
                        components[key].append(float(val))

            return components

        winner_components = extract_components(winners)
        loser_components = extract_components(losers)

        results = {}
        for key in winner_components:
            winner_vals = winner_components[key]
            loser_vals = loser_components[key]

            winner_avg = statistics.mean(winner_vals) if winner_vals else 0
            loser_avg = statistics.mean(loser_vals) if loser_vals else 0

            results[key] = {
                'winner_avg': round(winner_avg, 4),
                'loser_avg': round(loser_avg, 4),
                'difference': round(winner_avg - loser_avg, 4),
            }

        return results

    def analyze_fok_rejections(self) -> dict:
        """Analyze FOK rejections (trades that are in entry_trades but not paired)."""
        unpaired_entries = [t for t in self.entry_trades
                           if not any(pair['entry']['trade_id'] == t['trade_id']
                                     for pair in self.paired_trades)]

        if not unpaired_entries:
            return {
                'total_fok_rejections': 0,
                'rejection_rate': 'N/A',
            }

        return {
            'total_fok_rejections': len(unpaired_entries),
            'total_entry_attempts': len(self.entry_trades),
            'rejection_rate': f"{len(unpaired_entries)/len(self.entry_trades)*100:.1f}%",
            'total_rejected_usd': round(sum(float(t['entry_price']) * float(t['size'])
                                            for t in unpaired_entries), 2),
        }

    def analyze_by_bias(self) -> dict:
        """Analyze performance by adaptive bias state at entry.

        Reads bias_direction from signal_data JSONB (FAVORABLE / UNFAVORABLE / NEUTRAL).
        Shows whether the adaptive bias is actually improving trade selection.
        """
        results = defaultdict(list)

        for trade in self.paired_trades:
            market_id = trade['market_id']
            signal = self.signal_data.get(market_id, {})

            bias_dir = signal.get('bias_direction', 'UNKNOWN')
            if not bias_dir or bias_dir == 'UNKNOWN':
                # Fallback: check config_snapshot for whether bias was enabled
                config = self.config_data.get(market_id, {})
                if not config.get('adaptive_bias_enabled', True):
                    bias_dir = 'DISABLED'
                else:
                    bias_dir = 'NEUTRAL'  # No bias_direction field = pre-tracking era

            results[bias_dir].append(trade)

        analyzed = {}
        for bias_state, trades in sorted(results.items()):
            if not trades:
                continue
            pnls = [t['gross_pnl'] for t in trades]
            wins = sum(1 for p in pnls if p > 0)
            analyzed[bias_state] = {
                'count': len(trades),
                'win_rate': f"{wins/len(trades)*100:.1f}%" if trades else 'N/A',
                'avg_pnl': round(sum(pnls) / len(trades), 4) if trades else 0,
                'total_pnl': round(sum(pnls), 4),
            }

        return analyzed

    def analyze_by_intensity(self) -> dict:
        """Analyze performance by trade intensity buckets at entry.

        Helps validate the intensity cap threshold. Data shows losers
        average ~61 tps vs winners ~40 tps.
        """
        buckets = [
            ('0-20 tps', 0, 20),
            ('20-40 tps', 20, 40),
            ('40-50 tps', 40, 50),
            ('50-60 tps', 50, 60),
            ('60+ tps', 60, 999),
        ]
        results = {}

        for label, lo, hi in buckets:
            trades = []
            for trade in self.paired_trades:
                market_id = trade['market_id']
                signal = self.signal_data.get(market_id, {})
                intensity = signal.get('trade_intensity')
                if intensity is not None and lo <= float(intensity) < hi:
                    trades.append(trade)

            if not trades:
                continue
            pnls = [t['gross_pnl'] for t in trades]
            wins = sum(1 for p in pnls if p > 0)
            results[label] = {
                'count': len(trades),
                'win_rate': f"{wins/len(trades)*100:.1f}%",
                'avg_pnl': round(sum(pnls) / len(trades), 4),
                'total_pnl': round(sum(pnls), 4),
            }

        return results

    def analyze_execution_quality(self) -> dict:
        """Analyze execution quality (slippage, fill rates)."""
        if not self.signal_data or not self.paired_trades:
            return {'avg_entry_slippage': 'N/A', 'avg_exit_slippage': 'N/A', 'entry_trades_analyzed': 0, 'exit_trades_analyzed': 0}

        entry_slippages = []
        exit_slippages = []

        for trade in self.paired_trades:
            market_id = trade['market_id']
            if market_id not in self.signal_data:
                continue

            signal = self.signal_data[market_id]
            entry_trade = trade['entry']
            exit_trade = trade['exit']

            # Entry slippage: actual fill price vs market price at entry
            market_price = float(signal.get('market_price', 0))
            actual_entry = float(entry_trade['entry_price'])
            if market_price > 0:
                entry_slip = abs(actual_entry - market_price) / market_price
                entry_slippages.append(entry_slip)

            # Exit slippage: actual exit price vs entry price
            exit_price = float(trade['exit'].get('exit_price', 0) or trade['exit']['entry_price'])
            if actual_entry > 0:
                exit_slip = abs(exit_price - actual_entry) / actual_entry
                exit_slippages.append(exit_slip)

        return {
            'avg_entry_slippage': f"{statistics.mean(entry_slippages)*100:.2f}%" if entry_slippages else 'N/A',
            'avg_exit_slippage': f"{statistics.mean(exit_slippages)*100:.2f}%" if exit_slippages else 'N/A',
            'entry_trades_analyzed': len(entry_slippages),
            'exit_trades_analyzed': len(exit_slippages),
        }

    def _format_seconds(self, seconds: float) -> str:
        """Format seconds as human-readable string."""
        if seconds < 60:
            return f"{int(seconds)}s"
        elif seconds < 3600:
            minutes = seconds / 60
            return f"{minutes:.1f}m"
        else:
            hours = seconds / 3600
            return f"{hours:.1f}h"


async def print_report(analyzer: TradeAnalyzer):
    """Print a formatted analysis report using Rich."""
    try:
        from rich.console import Console
        from rich.table import Table
        from rich.panel import Panel
        from rich import print as rprint
    except ImportError:
        print("Rich library not found. Installing would enhance output formatting.")
        print_simple_report(analyzer)
        return

    console = Console()

    # Overall stats
    stats = analyzer.get_overall_stats()
    console.print(Panel(
        f"[bold green]Trade Analysis Report[/bold green]\n"
        f"Total Completed Trades: {stats['total_completed_trades']}\n"
        f"Total P&L: ${stats['total_p_and_l']}\n"
        f"Win Rate: {stats['win_rate']}\n"
        f"Wins: {stats['total_wins']} | Losses: {stats['total_losses']}\n"
        f"Gross Profit: ${stats['gross_profit']} | Gross Loss: ${stats['gross_loss']}\n"
        f"Largest Winner: ${stats['largest_winner']} | Largest Loser: ${stats['largest_loser']}\n"
        f"Avg P&L/Trade: ${stats['avg_p_and_l_per_trade']}\n"
        f"Avg Hold Time: {stats['avg_hold_time_readable']}",
        title="Overall Statistics"
    ))

    # By entry quality
    by_quality = analyzer.analyze_by_entry_quality()
    table = Table(title="Performance by Entry Quality (Momentum Score)")
    table.add_column("Momentum Range")
    table.add_column("Count", justify="right")
    table.add_column("Win Rate", justify="right")
    table.add_column("Avg P&L", justify="right")
    table.add_column("Total P&L", justify="right")
    for bucket, data in by_quality.items():
        if data['count'] > 0:
            table.add_row(
                bucket,
                str(data['count']),
                data['win_rate'],
                f"${data['avg_pnl']}",
                f"${data['total_pnl']}",
            )
    console.print(table)

    # By direction
    by_direction = analyzer.analyze_by_direction()
    table = Table(title="Performance by Entry Direction")
    table.add_column("Direction")
    table.add_column("Count", justify="right")
    table.add_column("Win Rate", justify="right")
    table.add_column("Avg P&L", justify="right")
    table.add_column("Total P&L", justify="right")
    for direction, data in by_direction.items():
        if data['count'] > 0:
            table.add_row(
                direction,
                str(data['count']),
                data['win_rate'],
                f"${data['avg_pnl']}",
                f"${data['total_pnl']}",
            )
    console.print(table)

    # By trend alignment
    by_trend = analyzer.analyze_by_trend_alignment()
    table = Table(title="Performance by Trend Alignment")
    table.add_column("Alignment")
    table.add_column("Count", justify="right")
    table.add_column("Win Rate", justify="right")
    table.add_column("Avg P&L", justify="right")
    table.add_column("Total P&L", justify="right")
    for alignment, data in by_trend.items():
        if data['count'] > 0:
            table.add_row(
                alignment,
                str(data['count']),
                data['win_rate'],
                f"${data['avg_pnl']}",
                f"${data['total_pnl']}",
            )
    console.print(table)

    # By time remaining
    by_time = analyzer.analyze_by_time_remaining()
    table = Table(title="Performance by Time Remaining at Entry")
    table.add_column("Time Bucket")
    table.add_column("Count", justify="right")
    table.add_column("Win Rate", justify="right")
    table.add_column("Avg P&L", justify="right")
    table.add_column("Total P&L", justify="right")
    for bucket, data in by_time.items():
        if data['count'] > 0:
            table.add_row(
                bucket,
                str(data['count']),
                data['win_rate'],
                f"${data['avg_pnl']}",
                f"${data['total_pnl']}",
            )
    console.print(table)

    # By exit reason
    by_exit = analyzer.analyze_by_exit_reason()
    table = Table(title="Performance by Exit Reason")
    table.add_column("Exit Reason")
    table.add_column("Count", justify="right")
    table.add_column("Win Rate", justify="right")
    table.add_column("Avg P&L", justify="right")
    table.add_column("Total P&L", justify="right")
    for reason, data in by_exit.items():
        if data['count'] > 0:
            table.add_row(
                reason.replace('_', ' ').title(),
                str(data['count']),
                data['win_rate'],
                f"${data['avg_pnl']}",
                f"${data['total_pnl']}",
            )
    console.print(table)

    # By adaptive bias state
    by_bias = analyzer.analyze_by_bias()
    if by_bias:
        table = Table(title="Performance by Adaptive Bias State")
        table.add_column("Bias State")
        table.add_column("Count", justify="right")
        table.add_column("Win Rate", justify="right")
        table.add_column("Avg P&L", justify="right")
        table.add_column("Total P&L", justify="right")
        for state, data in by_bias.items():
            if data['count'] > 0:
                table.add_row(
                    state,
                    str(data['count']),
                    data['win_rate'],
                    f"${data['avg_pnl']}",
                    f"${data['total_pnl']}",
                )
        console.print(table)

    # By trade intensity
    by_intensity = analyzer.analyze_by_intensity()
    if by_intensity:
        table = Table(title="Performance by Trade Intensity at Entry")
        table.add_column("Intensity")
        table.add_column("Count", justify="right")
        table.add_column("Win Rate", justify="right")
        table.add_column("Avg P&L", justify="right")
        table.add_column("Total P&L", justify="right")
        for bucket, data in by_intensity.items():
            if data['count'] > 0:
                table.add_row(
                    bucket,
                    str(data['count']),
                    data['win_rate'],
                    f"${data['avg_pnl']}",
                    f"${data['total_pnl']}",
                )
        console.print(table)

    # Signal components
    signal_comp = analyzer.analyze_signal_components()
    table = Table(title="Signal Component Analysis: Winners vs Losers")
    table.add_column("Component")
    table.add_column("Winner Avg", justify="right")
    table.add_column("Loser Avg", justify="right")
    table.add_column("Difference", justify="right")
    for component, data in signal_comp.items():
        table.add_row(
            component.replace('_', ' ').title(),
            str(data['winner_avg']),
            str(data['loser_avg']),
            str(data['difference']),
        )
    console.print(table)

    # FOK rejections
    fok = analyzer.analyze_fok_rejections()
    if fok['total_fok_rejections'] > 0:
        console.print(Panel(
            f"[bold]FOK Rejections[/bold]\n"
            f"Total Rejections: {fok['total_fok_rejections']}\n"
            f"Rejection Rate: {fok.get('rejection_rate', 'N/A')}\n"
            f"Total Rejected USD: ${fok.get('total_rejected_usd', 0)}",
            title="FOK Rejection Analysis"
        ))

    # Execution quality
    exec_quality = analyzer.analyze_execution_quality()
    console.print(Panel(
        f"[bold]Execution Quality[/bold]\n"
        f"Avg Entry Slippage: {exec_quality['avg_entry_slippage']}\n"
        f"Avg Exit Slippage: {exec_quality['avg_exit_slippage']}\n"
        f"Trades Analyzed: {exec_quality['entry_trades_analyzed']}",
        title="Execution Quality"
    ))


def print_simple_report(analyzer: TradeAnalyzer):
    """Print a simple text report (no Rich dependency)."""
    stats = analyzer.get_overall_stats()
    print("\n" + "="*60)
    print("TRADE ANALYSIS REPORT")
    print("="*60)
    print(f"Total Completed Trades: {stats['total_completed_trades']}")
    print(f"Total P&L: ${stats['total_p_and_l']}")
    print(f"Win Rate: {stats['win_rate']}")
    print(f"Wins: {stats['total_wins']} | Losses: {stats['total_losses']}")
    print(f"Gross Profit: ${stats['gross_profit']} | Gross Loss: ${stats['gross_loss']}")
    print(f"Largest Winner: ${stats['largest_winner']} | Largest Loser: ${stats['largest_loser']}")
    print(f"Avg P&L/Trade: ${stats['avg_p_and_l_per_trade']}")
    print(f"Avg Hold Time: {stats['avg_hold_time_readable']}")
    print()

    by_quality = analyzer.analyze_by_entry_quality()
    print("By Entry Quality (Momentum Score):")
    for bucket, data in by_quality.items():
        if data['count'] > 0:
            print(f"  {bucket}: {data['count']} trades, WR={data['win_rate']}, Avg P&L=${data['avg_pnl']}")
    print()

    by_direction = analyzer.analyze_by_direction()
    print("By Direction:")
    for direction, data in by_direction.items():
        if data['count'] > 0:
            print(f"  {direction}: {data['count']} trades, WR={data['win_rate']}, Avg P&L=${data['avg_pnl']}")
    print()

    signal_comp = analyzer.analyze_signal_components()
    print("Signal Components (Winners vs Losers):")
    for component, data in signal_comp.items():
        print(f"  {component}: Winner={data['winner_avg']}, Loser={data['loser_avg']}, Diff={data['difference']}")
    print()


async def main():
    parser = argparse.ArgumentParser(description="PolyEdge Trade Analysis")
    parser.add_argument("--hours", type=int, default=None, help="Last N hours (default 24)")
    parser.add_argument("--from-date", type=str, default=None, help="From date (YYYY-MM-DD)")
    parser.add_argument("--strategy", type=str, default="micro_sniper", help="Strategy to analyze")
    parser.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()

    db = await get_db()
    try:
        analyzer = TradeAnalyzer()
        await analyzer.load_trades(db, hours=args.hours, from_date=args.from_date, strategy=args.strategy)

        if args.json:
            # Output as JSON
            output = {
                'overall_stats': analyzer.get_overall_stats(),
                'by_entry_quality': analyzer.analyze_by_entry_quality(),
                'by_direction': analyzer.analyze_by_direction(),
                'by_trend_alignment': analyzer.analyze_by_trend_alignment(),
                'by_time_remaining': analyzer.analyze_by_time_remaining(),
                'by_exit_reason': analyzer.analyze_by_exit_reason(),
                'signal_components': analyzer.analyze_signal_components(),
                'fok_rejections': analyzer.analyze_fok_rejections(),
                'execution_quality': analyzer.analyze_execution_quality(),
            }
            print(json.dumps(output, indent=2, default=str))
        else:
            # Rich formatted output
            await print_report(analyzer)
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
