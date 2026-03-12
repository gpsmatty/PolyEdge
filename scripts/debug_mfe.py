#!/usr/bin/env python3
"""Quick debug: check MFE values on blocked signals and position_side on trades."""

import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


async def check():
    from polyedge.core.config import load_config
    from polyedge.core.db import Database

    settings = load_config()
    db = Database(settings.database_url)
    await db.connect()

    async with db.pool.acquire() as conn:
        # Top blocked signals by MFE
        rows = await conn.fetch("""
            SELECT no_trade_reason, max_favorable, max_adverse,
                   token_move_10s, btc_move_10s, features
            FROM polyedge.signal_snapshots
            WHERE no_trade_reason != 'none'
              AND outcome_labeled = true
            ORDER BY max_favorable DESC NULLS LAST
            LIMIT 15
        """)

        print("Top 15 blocked signals by MFE:")
        for r in rows:
            f = r["features"] if isinstance(r["features"], dict) else json.loads(r["features"])
            pos = f.get("position_side", "none")
            mom = f.get("dampened_momentum", 0)
            print(
                f"  {r['no_trade_reason']:20s} MFE={r['max_favorable'] or 0:+.6f} "
                f"MAE={r['max_adverse'] or 0:+.6f} "
                f"tok10={r['token_move_10s'] or 0:+.6f} "
                f"btc10={r['btc_move_10s'] or 0:+.6f} "
                f"pos={pos} mom={mom:+.3f}"
            )

        # Count nulls/zeros
        nulls = await conn.fetchval(
            "SELECT COUNT(*) FROM polyedge.signal_snapshots WHERE no_trade_reason != 'none' AND max_favorable IS NULL"
        )
        zeros = await conn.fetchval(
            "SELECT COUNT(*) FROM polyedge.signal_snapshots WHERE no_trade_reason != 'none' AND outcome_labeled = true AND max_favorable = 0"
        )
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM polyedge.signal_snapshots WHERE no_trade_reason != 'none' AND outcome_labeled = true"
        )
        print(f"\nBlocked: {total} labeled, {nulls} NULL MFE, {zeros} zero MFE")

        # Check what position_side looks like on trade snapshots
        trade_sides = await conn.fetch("""
            SELECT features->>'position_side' as pos, COUNT(*) as cnt
            FROM polyedge.signal_snapshots
            WHERE trade_fired = true
            GROUP BY features->>'position_side'
        """)
        print("\nTrade snapshot position_side values:")
        for r in trade_sides:
            print(f"  '{r['pos']}': {r['cnt']}")

        # Sample a trade snapshot to see what keys are in features
        sample = await conn.fetchval("""
            SELECT features FROM polyedge.signal_snapshots
            WHERE trade_fired = true LIMIT 1
        """)
        if sample:
            f = sample if isinstance(sample, dict) else json.loads(sample)
            print(f"\nSample trade features keys: {sorted(f.keys())}")

    await db.close()


asyncio.run(check())
