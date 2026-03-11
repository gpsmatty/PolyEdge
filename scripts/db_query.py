#!/usr/bin/env python3
"""Quick DB query tool for PolyEdge debugging.

Usage:
    python scripts/db_query.py positions          # Open positions
    python scripts/db_query.py trades [--hours N]  # Recent trades (default 24h)
    python scripts/db_query.py micro [--hours N]   # Micro sniper trades only
    python scripts/db_query.py pnl [--hours N]     # P&L summary
    python scripts/db_query.py bankroll            # Current USDC balance + exposure
    python scripts/db_query.py trend [--symbol S]  # Recent price trend from micro_price_log
    python scripts/db_query.py config [--key K]    # DB config values
    python scripts/db_query.py costs [--hours N]   # AI cost summary
    python scripts/db_query.py memory [--limit N]  # Agent memory entries
    python scripts/db_query.py sql "SELECT ..."    # Raw SQL query

Connects via load_config() (Keychain → env → .env → YAML).
Override with DATABASE_URL env var if needed.

Output is JSON by default. Add --table for Rich table format.
"""

import asyncio
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


async def query_positions(db, args):
    """Open positions."""
    rows = await db.pool.fetch("""
        SELECT market_id, question, side, size, entry_price, current_price,
               strategy, created_at
        FROM polyedge.positions
        ORDER BY created_at DESC
    """)
    return [dict(r) for r in rows]


async def query_trades(db, args):
    """Recent trades."""
    hours = args.hours or 24
    rows = await db.pool.fetch("""
        SELECT trade_id, market_id, question, side, order_type, price, size,
               amount_usd, status, strategy, created_at,
               config_snapshot, signal_data
        FROM polyedge.trades
        WHERE created_at > NOW() - INTERVAL '%s hours'
        ORDER BY created_at DESC
    """ % hours)
    return [dict(r) for r in rows]


async def query_micro(db, args):
    """Micro sniper trades only, with signal details."""
    hours = args.hours or 24
    rows = await db.pool.fetch("""
        SELECT trade_id, question, side, order_type, price, size,
               amount_usd, status, created_at,
               signal_data->>'momentum' as momentum,
               signal_data->>'ofi_15s' as ofi_15s,
               signal_data->>'trend_5m' as trend_5m,
               signal_data->>'seconds_remaining' as secs_left,
               signal_data->>'yes_price' as yes_price,
               signal_data->>'no_price' as no_price
        FROM polyedge.trades
        WHERE strategy = 'micro_sniper'
          AND created_at > NOW() - INTERVAL '%s hours'
        ORDER BY created_at DESC
    """ % hours)
    return [dict(r) for r in rows]


async def query_pnl(db, args):
    """P&L summary from trades."""
    hours = args.hours or 24
    rows = await db.pool.fetch("""
        WITH buys AS (
            SELECT market_id, question, side, price as entry_price, size, amount_usd,
                   strategy, created_at
            FROM polyedge.trades
            WHERE order_type = 'FOK' AND side = 'BUY' AND status = 'FILLED'
              AND created_at > NOW() - INTERVAL '%s hours'
        ),
        sells AS (
            SELECT market_id, side, price as exit_price, size as sold_size,
                   created_at as sold_at
            FROM polyedge.trades
            WHERE order_type = 'FOK' AND side = 'SELL' AND status = 'FILLED'
              AND created_at > NOW() - INTERVAL '%s hours'
        )
        SELECT b.market_id, b.question, b.side, b.entry_price, b.size, b.amount_usd,
               s.exit_price, s.sold_size,
               CASE WHEN s.exit_price IS NOT NULL
                    THEN (s.exit_price - b.entry_price) * LEAST(b.size, COALESCE(s.sold_size, 0))
                    ELSE NULL END as gross_pnl,
               b.strategy, b.created_at, s.sold_at
        FROM buys b
        LEFT JOIN sells s ON b.market_id = s.market_id
        ORDER BY b.created_at DESC
    """ % (hours, hours))

    results = [dict(r) for r in rows]

    # Summary
    total_pnl = sum(r.get("gross_pnl") or 0 for r in results)
    wins = sum(1 for r in results if (r.get("gross_pnl") or 0) > 0)
    losses = sum(1 for r in results if (r.get("gross_pnl") or 0) < 0)
    open_count = sum(1 for r in results if r.get("gross_pnl") is None)

    return {
        "summary": {
            "total_pnl": round(total_pnl, 4),
            "wins": wins,
            "losses": losses,
            "open": open_count,
            "total_trades": len(results),
            "win_rate": f"{wins/(wins+losses)*100:.1f}%" if (wins+losses) > 0 else "N/A",
        },
        "trades": results,
    }


async def query_bankroll(db, args):
    """Bankroll and exposure."""
    positions = await db.pool.fetch("""
        SELECT side, size, entry_price, current_price, strategy
        FROM polyedge.positions
    """)
    total_exposure = sum(float(p["size"]) * float(p["entry_price"]) for p in positions)

    # Get recent portfolio snapshot if available
    snapshot = await db.pool.fetchrow("""
        SELECT * FROM polyedge.portfolio_snapshots
        ORDER BY created_at DESC LIMIT 1
    """)

    return {
        "open_positions": len(positions),
        "total_exposure_usd": round(total_exposure, 2),
        "positions": [dict(p) for p in positions],
        "latest_snapshot": dict(snapshot) if snapshot else None,
    }


async def query_trend(db, args):
    """Price trend from micro_price_log."""
    symbol = (args.symbol or "btcusdt").lower()
    rows = await db.pool.fetch("""
        SELECT price, ofi_30s, volume_30s, trade_intensity, logged_at
        FROM polyedge.micro_price_log
        WHERE symbol = $1
        ORDER BY logged_at DESC
        LIMIT 60
    """, symbol)

    if not rows:
        return {"symbol": symbol, "message": "No price data"}

    rows = list(reversed(rows))  # Oldest first
    latest = rows[-1]
    oldest = rows[0]
    trend_pct = (float(latest["price"]) - float(oldest["price"])) / float(oldest["price"])
    duration_min = (latest["logged_at"] - oldest["logged_at"]).total_seconds() / 60

    return {
        "symbol": symbol.upper(),
        "current_price": float(latest["price"]),
        "oldest_price": float(oldest["price"]),
        "trend_pct": f"{trend_pct:+.4%}",
        "duration_minutes": round(duration_min, 1),
        "data_points": len(rows),
        "latest_ofi": float(latest["ofi_30s"]),
        "snapshots": [
            {
                "price": float(r["price"]),
                "ofi": float(r["ofi_30s"]),
                "intensity": float(r["trade_intensity"]),
                "time": r["logged_at"].isoformat(),
            }
            for r in rows[-10:]  # Last 10 snapshots
        ],
    }


async def query_config(db, args):
    """DB config values."""
    if args.key:
        row = await db.pool.fetchrow("""
            SELECT key, value FROM polyedge.risk_config WHERE key = $1
        """, args.key)
        return dict(row) if row else {"error": f"Key '{args.key}' not found"}
    else:
        rows = await db.pool.fetch("""
            SELECT key, value FROM polyedge.risk_config ORDER BY key
        """)
        return {r["key"]: json.loads(r["value"]) if r["value"].startswith(("{", "[", '"')) else r["value"]
                for r in rows}


async def query_costs(db, args):
    """AI cost summary."""
    hours = args.hours or 24
    rows = await db.pool.fetch("""
        SELECT model, purpose,
               COUNT(*) as calls,
               SUM(cost) as total_cost,
               SUM(input_tokens) as total_input,
               SUM(output_tokens) as total_output
        FROM polyedge.ai_cost_log
        WHERE created_at > NOW() - INTERVAL '%s hours'
        GROUP BY model, purpose
        ORDER BY total_cost DESC
    """ % hours)

    total = sum(float(r["total_cost"] or 0) for r in rows)
    return {
        "total_cost_usd": round(total, 4),
        "hours": hours,
        "breakdown": [dict(r) for r in rows],
    }


async def query_memory(db, args):
    """Agent memory entries."""
    limit = args.limit or 20
    rows = await db.pool.fetch("""
        SELECT memory_type, market_id, content, created_at, expires_at
        FROM polyedge.agent_memory
        ORDER BY created_at DESC
        LIMIT $1
    """, limit)
    return [dict(r) for r in rows]


async def query_sql(db, args):
    """Raw SQL query (read-only)."""
    sql = args.sql
    if not sql:
        return {"error": "No SQL provided"}

    # Safety check — only allow SELECT
    stripped = sql.strip().upper()
    if not stripped.startswith("SELECT"):
        return {"error": "Only SELECT queries allowed"}

    rows = await db.pool.fetch(sql)
    return [dict(r) for r in rows]


async def query_micro_stats(db, args):
    """Micro sniper performance stats."""
    hours = args.hours or 24
    row = await db.pool.fetchrow("""
        WITH micro AS (
            SELECT
                trade_id, side, price, size, amount_usd, status, created_at,
                signal_data->>'momentum' as momentum,
                signal_data->>'trend_5m' as trend_5m
            FROM polyedge.trades
            WHERE strategy = 'micro_sniper'
              AND created_at > NOW() - INTERVAL '%s hours'
        ),
        buys AS (
            SELECT trade_id, price as entry_price, size, amount_usd, created_at, momentum, trend_5m
            FROM micro WHERE side = 'BUY'
        ),
        sells AS (
            SELECT price as exit_price, size as sold_size, created_at as sold_at
            FROM micro WHERE side = 'SELL'
        )
        SELECT
            (SELECT COUNT(*) FROM buys) as total_buys,
            (SELECT COUNT(*) FROM sells) as total_sells,
            (SELECT SUM(amount_usd) FROM buys) as total_spent,
            (SELECT AVG(CAST(momentum AS FLOAT)) FROM buys WHERE momentum IS NOT NULL) as avg_entry_momentum,
            (SELECT AVG(CAST(trend_5m AS FLOAT)) FROM buys WHERE trend_5m IS NOT NULL) as avg_entry_trend
    """ % hours)

    # Get win/loss from paired trades
    pairs = await db.pool.fetch("""
        WITH ordered AS (
            SELECT side, price, size, created_at,
                   ROW_NUMBER() OVER (PARTITION BY side ORDER BY created_at) as rn
            FROM polyedge.trades
            WHERE strategy = 'micro_sniper' AND status = 'FILLED'
              AND created_at > NOW() - INTERVAL '%s hours'
        )
        SELECT
            b.price as entry_price, s.price as exit_price,
            (s.price - b.price) * LEAST(b.size, s.size) as pnl
        FROM ordered b
        JOIN ordered s ON b.rn = s.rn
        WHERE b.side = 'BUY' AND s.side = 'SELL'
        ORDER BY b.created_at DESC
    """ % hours)

    wins = sum(1 for p in pairs if float(p["pnl"] or 0) > 0)
    losses = sum(1 for p in pairs if float(p["pnl"] or 0) <= 0)
    total_pnl = sum(float(p["pnl"] or 0) for p in pairs)

    return {
        "hours": hours,
        "total_buys": row["total_buys"] if row else 0,
        "total_sells": row["total_sells"] if row else 0,
        "total_spent_usd": round(float(row["total_spent"] or 0), 2) if row else 0,
        "completed_trades": len(pairs),
        "wins": wins,
        "losses": losses,
        "win_rate": f"{wins/(wins+losses)*100:.1f}%" if (wins+losses) > 0 else "N/A",
        "total_pnl": round(total_pnl, 4),
        "avg_pnl_per_trade": round(total_pnl / len(pairs), 4) if pairs else 0,
        "avg_entry_momentum": round(float(row["avg_entry_momentum"] or 0), 3) if row else 0,
        "avg_entry_trend_5m": f"{float(row['avg_entry_trend'] or 0):.4%}" if row and row["avg_entry_trend"] else "N/A",
        "recent_trades": [
            {
                "entry": float(p["entry_price"]),
                "exit": float(p["exit_price"]),
                "pnl": round(float(p["pnl"] or 0), 4),
            }
            for p in pairs[:10]
        ],
    }


class DateTimeEncoder(json.JSONEncoder):
    """Handle datetime serialization."""
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        if hasattr(obj, '__float__'):
            return float(obj)
        return super().default(obj)


async def main():
    parser = argparse.ArgumentParser(description="PolyEdge DB Query Tool")
    parser.add_argument("command", choices=[
        "positions", "trades", "micro", "micro-stats", "pnl",
        "bankroll", "trend", "config", "costs", "memory", "sql",
    ])
    parser.add_argument("sql", nargs="?", default=None, help="SQL for 'sql' command")
    parser.add_argument("--hours", type=int, default=None)
    parser.add_argument("--symbol", "-s", type=str, default=None)
    parser.add_argument("--key", "-k", type=str, default=None)
    parser.add_argument("--limit", "-n", type=int, default=None)
    parser.add_argument("--compact", action="store_true", help="Compact JSON output")

    args = parser.parse_args()

    handlers = {
        "positions": query_positions,
        "trades": query_trades,
        "micro": query_micro,
        "micro-stats": query_micro_stats,
        "pnl": query_pnl,
        "bankroll": query_bankroll,
        "trend": query_trend,
        "config": query_config,
        "costs": query_costs,
        "memory": query_memory,
        "sql": query_sql,
    }

    db = await get_db()
    try:
        result = await handlers[args.command](db, args)
        indent = None if args.compact else 2
        print(json.dumps(result, indent=indent, cls=DateTimeEncoder))
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
