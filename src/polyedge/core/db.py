"""PostgreSQL storage layer using asyncpg."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Optional

import asyncpg

SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS polyedge;

CREATE TABLE IF NOT EXISTS polyedge.markets (
    condition_id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    slug TEXT DEFAULT '',
    description TEXT DEFAULT '',
    category TEXT DEFAULT '',
    end_date TIMESTAMPTZ,
    active BOOLEAN DEFAULT TRUE,
    closed BOOLEAN DEFAULT FALSE,
    clob_token_ids JSONB DEFAULT '[]',
    yes_price FLOAT DEFAULT 0,
    no_price FLOAT DEFAULT 0,
    volume FLOAT DEFAULT 0,
    liquidity FLOAT DEFAULT 0,
    spread FLOAT DEFAULT 0,
    raw JSONB DEFAULT '{}',
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS polyedge.orders (
    order_id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT DEFAULT 'LIMIT',
    price FLOAT NOT NULL,
    size FLOAT NOT NULL,
    amount_usd FLOAT NOT NULL,
    status TEXT DEFAULT 'PENDING',
    filled_size FLOAT DEFAULT 0,
    filled_avg_price FLOAT DEFAULT 0,
    strategy TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS polyedge.trades (
    trade_id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    question TEXT DEFAULT '',
    side TEXT NOT NULL,
    entry_price FLOAT NOT NULL,
    exit_price FLOAT,
    size FLOAT NOT NULL,
    pnl FLOAT DEFAULT 0,
    status TEXT DEFAULT 'OPEN',
    strategy TEXT DEFAULT '',
    reasoning TEXT DEFAULT '',
    ai_probability FLOAT,
    opened_at TIMESTAMPTZ DEFAULT NOW(),
    closed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS polyedge.positions (
    id SERIAL PRIMARY KEY,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    question TEXT DEFAULT '',
    side TEXT NOT NULL,
    size FLOAT NOT NULL,
    entry_price FLOAT NOT NULL,
    current_price FLOAT DEFAULT 0,
    unrealized_pnl FLOAT DEFAULT 0,
    realized_pnl FLOAT DEFAULT 0,
    strategy TEXT DEFAULT '',
    opened_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(market_id, token_id, side)
);

CREATE TABLE IF NOT EXISTS polyedge.ai_analyses (
    id SERIAL PRIMARY KEY,
    market_id TEXT NOT NULL,
    question TEXT DEFAULT '',
    probability FLOAT NOT NULL,
    confidence FLOAT NOT NULL,
    reasoning TEXT DEFAULT '',
    risk_factors JSONB DEFAULT '[]',
    news_context TEXT DEFAULT '',
    provider TEXT DEFAULT '',
    model TEXT DEFAULT '',
    cost_usd FLOAT DEFAULT 0,
    analyzed_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS polyedge.portfolio_snapshots (
    id SERIAL PRIMARY KEY,
    bankroll FLOAT NOT NULL,
    total_exposure FLOAT DEFAULT 0,
    positions_count INT DEFAULT 0,
    unrealized_pnl FLOAT DEFAULT 0,
    realized_pnl_today FLOAT DEFAULT 0,
    trades_today INT DEFAULT 0,
    peak_bankroll FLOAT DEFAULT 0,
    drawdown_pct FLOAT DEFAULT 0,
    ai_cost_today FLOAT DEFAULT 0,
    timestamp TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS polyedge.risk_config (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS polyedge.price_history (
    id SERIAL PRIMARY KEY,
    market_id TEXT NOT NULL,
    yes_price FLOAT NOT NULL,
    no_price FLOAT NOT NULL,
    volume FLOAT DEFAULT 0,
    liquidity FLOAT DEFAULT 0,
    spread FLOAT DEFAULT 0,
    recorded_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS polyedge.ai_cost_log (
    id SERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INT DEFAULT 0,
    output_tokens INT DEFAULT 0,
    cost_usd FLOAT DEFAULT 0,
    purpose TEXT DEFAULT '',
    market_id TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS polyedge.agent_memory (
    id SERIAL PRIMARY KEY,
    market_id TEXT DEFAULT '',
    memory_type TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata JSONB DEFAULT '{}',
    importance FLOAT DEFAULT 0.5,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ,
    superseded_by INT
);

CREATE INDEX IF NOT EXISTS idx_trades_status ON polyedge.trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_market ON polyedge.trades(market_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON polyedge.orders(status);
CREATE INDEX IF NOT EXISTS idx_ai_analyses_market ON polyedge.ai_analyses(market_id);
CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_ts ON polyedge.portfolio_snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_price_history_market ON polyedge.price_history(market_id);
CREATE INDEX IF NOT EXISTS idx_price_history_ts ON polyedge.price_history(recorded_at);
CREATE INDEX IF NOT EXISTS idx_ai_cost_log_ts ON polyedge.ai_cost_log(created_at);
CREATE INDEX IF NOT EXISTS idx_agent_memory_market ON polyedge.agent_memory(market_id);
CREATE INDEX IF NOT EXISTS idx_agent_memory_type ON polyedge.agent_memory(memory_type);
"""


class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(self.dsn, min_size=2, max_size=10)

    async def close(self):
        if self.pool:
            await self.pool.close()

    async def init_schema(self):
        async with self.pool.acquire() as conn:
            await conn.execute(SCHEMA_SQL)

    # --- Markets ---

    async def upsert_market(self, market: dict):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO polyedge.markets
                    (condition_id, question, slug, description, category, end_date,
                     active, closed, clob_token_ids, yes_price, no_price,
                     volume, liquidity, spread, raw, updated_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,NOW())
                ON CONFLICT (condition_id) DO UPDATE SET
                    question=EXCLUDED.question, yes_price=EXCLUDED.yes_price,
                    no_price=EXCLUDED.no_price, volume=EXCLUDED.volume,
                    liquidity=EXCLUDED.liquidity, spread=EXCLUDED.spread,
                    active=EXCLUDED.active, closed=EXCLUDED.closed,
                    raw=EXCLUDED.raw, updated_at=NOW()
                """,
                market["condition_id"],
                market.get("question", ""),
                market.get("slug", ""),
                market.get("description", ""),
                market.get("category", ""),
                market.get("end_date"),
                market.get("active", True),
                market.get("closed", False),
                json.dumps(market.get("clob_token_ids", [])),
                market.get("yes_price", 0),
                market.get("no_price", 0),
                market.get("volume", 0),
                market.get("liquidity", 0),
                market.get("spread", 0),
                json.dumps(market.get("raw", {})),
            )

    async def bulk_upsert_markets(self, markets: list[dict]):
        """Batch upsert markets in a single transaction.

        Uses executemany for ~1 round trip instead of N individual inserts.
        Dramatically faster for remote databases (1800 markets: ~2s vs ~60s+).
        """
        if not markets:
            return
        rows = []
        for m in markets:
            rows.append((
                m["condition_id"],
                m.get("question", ""),
                m.get("slug", ""),
                m.get("description", ""),
                m.get("category", ""),
                m.get("end_date"),
                m.get("active", True),
                m.get("closed", False),
                json.dumps(m.get("clob_token_ids", [])),
                m.get("yes_price", 0),
                m.get("no_price", 0),
                m.get("volume", 0),
                m.get("liquidity", 0),
                m.get("spread", 0),
                json.dumps(m.get("raw", {})),
            ))
        async with self.pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO polyedge.markets
                    (condition_id, question, slug, description, category, end_date,
                     active, closed, clob_token_ids, yes_price, no_price,
                     volume, liquidity, spread, raw, updated_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,NOW())
                ON CONFLICT (condition_id) DO UPDATE SET
                    question=EXCLUDED.question, yes_price=EXCLUDED.yes_price,
                    no_price=EXCLUDED.no_price, volume=EXCLUDED.volume,
                    liquidity=EXCLUDED.liquidity, spread=EXCLUDED.spread,
                    active=EXCLUDED.active, closed=EXCLUDED.closed,
                    raw=EXCLUDED.raw, updated_at=NOW()
                """,
                rows,
            )

    async def get_active_markets(self, min_liquidity: float = 0) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM polyedge.markets
                WHERE active = TRUE AND closed = FALSE AND liquidity >= $1
                ORDER BY volume DESC
                """,
                min_liquidity,
            )
            return [dict(r) for r in rows]

    # --- Orders ---

    async def insert_order(self, order: dict) -> str:
        order_id = order.get("order_id") or str(uuid.uuid4())
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO polyedge.orders
                    (order_id, market_id, token_id, side, order_type, price,
                     size, amount_usd, status, strategy)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                """,
                order_id,
                order["market_id"],
                order["token_id"],
                order["side"],
                order.get("order_type", "LIMIT"),
                order["price"],
                order["size"],
                order["amount_usd"],
                order.get("status", "PENDING"),
                order.get("strategy", ""),
            )
        return order_id

    async def update_order_status(
        self, order_id: str, status: str, filled_size: float = 0, filled_avg_price: float = 0
    ):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE polyedge.orders
                SET status=$2, filled_size=$3, filled_avg_price=$4, updated_at=NOW()
                WHERE order_id=$1
                """,
                order_id,
                status,
                filled_size,
                filled_avg_price,
            )

    # --- Trades ---

    async def insert_trade(self, trade: dict) -> str:
        trade_id = trade.get("trade_id") or str(uuid.uuid4())
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO polyedge.trades
                    (trade_id, market_id, token_id, question, side, entry_price,
                     size, status, strategy, reasoning, ai_probability)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                """,
                trade_id,
                trade["market_id"],
                trade["token_id"],
                trade.get("question", ""),
                trade["side"],
                trade["entry_price"],
                trade["size"],
                trade.get("status", "OPEN"),
                trade.get("strategy", ""),
                trade.get("reasoning", ""),
                trade.get("ai_probability"),
            )
        return trade_id

    async def close_trade(self, trade_id: str, exit_price: float, pnl: float, status: str):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE polyedge.trades
                SET exit_price=$2, pnl=$3, status=$4, closed_at=NOW()
                WHERE trade_id=$1
                """,
                trade_id,
                exit_price,
                pnl,
                status,
            )

    async def get_open_trades(self) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM polyedge.trades WHERE status = 'OPEN' ORDER BY opened_at"
            )
            return [dict(r) for r in rows]

    async def get_trades_today(self) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM polyedge.trades
                WHERE opened_at >= CURRENT_DATE
                ORDER BY opened_at
                """
            )
            return [dict(r) for r in rows]

    # --- Positions ---

    async def upsert_position(self, position: dict):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO polyedge.positions
                    (market_id, token_id, question, side, size, entry_price,
                     current_price, unrealized_pnl, strategy)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                ON CONFLICT (market_id, token_id, side) DO UPDATE SET
                    size=EXCLUDED.size, current_price=EXCLUDED.current_price,
                    unrealized_pnl=EXCLUDED.unrealized_pnl
                """,
                position["market_id"],
                position["token_id"],
                position.get("question", ""),
                position["side"],
                position["size"],
                position["entry_price"],
                position.get("current_price", 0),
                position.get("unrealized_pnl", 0),
                position.get("strategy", ""),
            )

    async def get_open_positions(self) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM polyedge.positions WHERE size > 0 ORDER BY opened_at"
            )
            return [dict(r) for r in rows]

    async def remove_position(self, market_id: str, token_id: str, side: str):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                DELETE FROM polyedge.positions
                WHERE market_id=$1 AND token_id=$2 AND side=$3
                """,
                market_id,
                token_id,
                side,
            )

    # --- AI Analyses ---

    async def save_analysis(self, analysis: dict):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO polyedge.ai_analyses
                    (market_id, question, probability, confidence, reasoning,
                     risk_factors, news_context, provider, model, cost_usd)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                """,
                analysis["market_id"],
                analysis.get("question", ""),
                analysis["probability"],
                analysis["confidence"],
                analysis.get("reasoning", ""),
                json.dumps(analysis.get("risk_factors", [])),
                analysis.get("news_context", ""),
                analysis.get("provider", ""),
                analysis.get("model", ""),
                analysis.get("cost_usd", 0),
            )

    async def get_latest_analysis(self, market_id: str) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM polyedge.ai_analyses
                WHERE market_id = $1
                ORDER BY analyzed_at DESC LIMIT 1
                """,
                market_id,
            )
            return dict(row) if row else None

    async def get_ai_cost_today(self) -> float:
        async with self.pool.acquire() as conn:
            result = await conn.fetchval(
                """
                SELECT COALESCE(SUM(cost_usd), 0)
                FROM polyedge.ai_analyses
                WHERE analyzed_at >= CURRENT_DATE
                """
            )
            return float(result)

    # --- Portfolio ---

    async def save_portfolio_snapshot(self, snapshot: dict):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO polyedge.portfolio_snapshots
                    (bankroll, total_exposure, positions_count, unrealized_pnl,
                     realized_pnl_today, trades_today, peak_bankroll, drawdown_pct, ai_cost_today)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                """,
                snapshot["bankroll"],
                snapshot.get("total_exposure", 0),
                snapshot.get("positions_count", 0),
                snapshot.get("unrealized_pnl", 0),
                snapshot.get("realized_pnl_today", 0),
                snapshot.get("trades_today", 0),
                snapshot.get("peak_bankroll", 0),
                snapshot.get("drawdown_pct", 0),
                snapshot.get("ai_cost_today", 0),
            )

    # --- Risk Config (runtime overrides) ---

    async def get_risk_override(self, key: str) -> Optional[any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT value FROM polyedge.risk_config WHERE key = $1", key
            )
            if row:
                return json.loads(row["value"])
            return None

    async def set_risk_override(self, key: str, value: any):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO polyedge.risk_config (key, value, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (key) DO UPDATE SET value=$2, updated_at=NOW()
                """,
                key,
                json.dumps(value),
            )

    async def get_all_config(self) -> dict[str, any]:
        """Get all config key-value pairs from risk_config table."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, value FROM polyedge.risk_config")
            return {row["key"]: json.loads(row["value"]) for row in rows}

    async def set_config_bulk(self, configs: dict[str, any]):
        """Bulk upsert config values into risk_config table."""
        async with self.pool.acquire() as conn:
            for key, value in configs.items():
                await conn.execute(
                    """
                    INSERT INTO polyedge.risk_config (key, value, updated_at)
                    VALUES ($1, $2, NOW())
                    ON CONFLICT (key) DO UPDATE SET value=$2, updated_at=NOW()
                    """,
                    key,
                    json.dumps(value),
                )

    # --- Price History ---

    async def record_price_snapshot(self, market_id: str, yes_price: float, no_price: float,
                                     volume: float = 0, liquidity: float = 0, spread: float = 0):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO polyedge.price_history
                    (market_id, yes_price, no_price, volume, liquidity, spread)
                VALUES ($1,$2,$3,$4,$5,$6)
                """,
                market_id, yes_price, no_price, volume, liquidity, spread,
            )

    async def bulk_record_prices(self, snapshots: list[tuple]):
        """Batch insert price snapshots. Each tuple: (market_id, yes, no, vol, liq, spread)."""
        if not snapshots:
            return
        async with self.pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO polyedge.price_history
                    (market_id, yes_price, no_price, volume, liquidity, spread)
                VALUES ($1,$2,$3,$4,$5,$6)
                """,
                snapshots,
            )

    async def get_price_history(self, market_id: str, hours: int = 24) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM polyedge.price_history
                WHERE market_id = $1 AND recorded_at >= NOW() - INTERVAL '1 hour' * $2
                ORDER BY recorded_at
                """,
                market_id, hours,
            )
            return [dict(r) for r in rows]

    async def get_markets_from_db(self, active_only: bool = True, min_liquidity: float = 0,
                                   limit: int = 500) -> list[dict]:
        """Get markets from local DB instead of hitting the API."""
        async with self.pool.acquire() as conn:
            if active_only:
                rows = await conn.fetch(
                    """
                    SELECT * FROM polyedge.markets
                    WHERE active = TRUE AND closed = FALSE AND liquidity >= $1
                    ORDER BY volume DESC LIMIT $2
                    """,
                    min_liquidity, limit,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM polyedge.markets ORDER BY volume DESC LIMIT $1", limit,
                )
            return [dict(r) for r in rows]

    async def get_market_count(self) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM polyedge.markets WHERE active = TRUE AND closed = FALSE"
            )

    async def get_stale_market_count(self, minutes: int = 30) -> int:
        """Count markets not updated in the last N minutes."""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """
                SELECT COUNT(*) FROM polyedge.markets
                WHERE active = TRUE AND closed = FALSE
                AND updated_at < NOW() - INTERVAL '1 minute' * $1
                """,
                minutes,
            )

    # --- AI Cost Tracking ---

    async def log_ai_cost(self, provider: str, model: str, input_tokens: int,
                           output_tokens: int, cost_usd: float, purpose: str = "",
                           market_id: str = ""):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO polyedge.ai_cost_log
                    (provider, model, input_tokens, output_tokens, cost_usd, purpose, market_id)
                VALUES ($1,$2,$3,$4,$5,$6,$7)
                """,
                provider, model, input_tokens, output_tokens, cost_usd, purpose, market_id,
            )

    async def get_ai_cost_today_detailed(self) -> dict:
        """Get detailed AI cost breakdown for today."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT provider, model,
                    COUNT(*) as calls,
                    SUM(input_tokens) as total_input,
                    SUM(output_tokens) as total_output,
                    SUM(cost_usd) as total_cost
                FROM polyedge.ai_cost_log
                WHERE created_at >= CURRENT_DATE
                GROUP BY provider, model
                """
            )
            total = await conn.fetchval(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM polyedge.ai_cost_log WHERE created_at >= CURRENT_DATE"
            )
            return {
                "total_cost": float(total),
                "breakdown": [dict(r) for r in rows],
            }

    # --- Agent Memory ---

    async def save_memory(self, memory_type: str, content: str, market_id: str = "",
                           metadata: dict = None, importance: float = 0.5,
                           expires_at: datetime = None) -> int:
        """Store an agent memory. Returns the memory ID."""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO polyedge.agent_memory
                    (market_id, memory_type, content, metadata, importance, expires_at)
                VALUES ($1,$2,$3,$4,$5,$6)
                RETURNING id
                """,
                market_id,
                memory_type,
                content,
                json.dumps(metadata or {}),
                importance,
                expires_at,
            )

    async def get_memories(self, market_id: str = "", memory_type: str = "",
                            limit: int = 20) -> list[dict]:
        """Retrieve active (non-expired, non-superseded) memories."""
        async with self.pool.acquire() as conn:
            conditions = ["superseded_by IS NULL", "(expires_at IS NULL OR expires_at > NOW())"]
            params = []
            idx = 1

            if market_id:
                conditions.append(f"market_id = ${idx}")
                params.append(market_id)
                idx += 1
            if memory_type:
                conditions.append(f"memory_type = ${idx}")
                params.append(memory_type)
                idx += 1

            conditions.append(f"${idx}")
            params.append(limit)

            where = " AND ".join(conditions[:-1])
            query = f"""
                SELECT * FROM polyedge.agent_memory
                WHERE {where}
                ORDER BY importance DESC, created_at DESC
                LIMIT ${idx}
            """
            rows = await conn.fetch(query, *params)
            return [dict(r) for r in rows]

    async def get_market_memories(self, market_id: str) -> list[dict]:
        """Get all active memories for a specific market."""
        return await self.get_memories(market_id=market_id)

    async def get_global_memories(self, memory_type: str = "", limit: int = 20) -> list[dict]:
        """Get memories not tied to a specific market (lessons, patterns, etc)."""
        async with self.pool.acquire() as conn:
            conditions = [
                "superseded_by IS NULL",
                "(expires_at IS NULL OR expires_at > NOW())",
                "(market_id = '' OR market_id IS NULL)",
            ]
            params = []
            idx = 1

            if memory_type:
                conditions.append(f"memory_type = ${idx}")
                params.append(memory_type)
                idx += 1

            params.append(limit)
            where = " AND ".join(conditions)
            query = f"""
                SELECT * FROM polyedge.agent_memory
                WHERE {where}
                ORDER BY importance DESC, created_at DESC
                LIMIT ${idx}
            """
            rows = await conn.fetch(query, *params)
            return [dict(r) for r in rows]

    async def supersede_memory(self, old_id: int, new_id: int):
        """Mark a memory as superseded by a newer one."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE polyedge.agent_memory SET superseded_by = $2 WHERE id = $1",
                old_id, new_id,
            )

    async def cleanup_expired_memories(self) -> int:
        """Delete memories that have expired. Returns count deleted."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM polyedge.agent_memory WHERE expires_at IS NOT NULL AND expires_at < NOW()"
            )
            # asyncpg returns "DELETE N"
            return int(result.split()[-1]) if result else 0

    # --- Market Lifecycle ---

    async def deactivate_missing_markets(self, active_condition_ids: list[str]) -> int:
        """Mark markets as inactive if they weren't in the latest API sync.

        Markets that vanish from the API are resolved/closed/removed.
        We don't delete them — just mark active=FALSE so they stop appearing in scans.
        Returns count of markets deactivated.
        """
        if not active_condition_ids:
            return 0
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE polyedge.markets
                SET active = FALSE, updated_at = NOW()
                WHERE active = TRUE
                AND condition_id != ALL($1::text[])
                """,
                active_condition_ids,
            )
            return int(result.split()[-1]) if result else 0

    async def deactivate_past_end_date(self) -> int:
        """Mark markets as closed if their end_date has passed."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE polyedge.markets
                SET closed = TRUE, active = FALSE, updated_at = NOW()
                WHERE active = TRUE AND closed = FALSE
                AND end_date IS NOT NULL AND end_date < NOW()
                """
            )
            return int(result.split()[-1]) if result else 0

    async def get_market_lifecycle_stats(self) -> dict:
        """Get counts of markets in various states."""
        async with self.pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM polyedge.markets")
            active = await conn.fetchval(
                "SELECT COUNT(*) FROM polyedge.markets WHERE active = TRUE AND closed = FALSE"
            )
            closed = await conn.fetchval(
                "SELECT COUNT(*) FROM polyedge.markets WHERE closed = TRUE"
            )
            inactive = await conn.fetchval(
                "SELECT COUNT(*) FROM polyedge.markets WHERE active = FALSE AND closed = FALSE"
            )
            return {
                "total": total,
                "active": active,
                "closed": closed,
                "inactive": inactive,
            }
