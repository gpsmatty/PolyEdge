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
    config_snapshot JSONB,
    signal_data JSONB,
    exit_reason TEXT DEFAULT '',
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

CREATE TABLE IF NOT EXISTS polyedge.pnl_ledger (
    id SERIAL PRIMARY KEY,
    market_id TEXT NOT NULL,
    question TEXT DEFAULT '',
    strategy TEXT DEFAULT '',
    side TEXT NOT NULL,
    size FLOAT NOT NULL,
    entry_fill_price FLOAT NOT NULL,
    exit_fill_price FLOAT,
    gross_pnl FLOAT DEFAULT 0,
    fees_paid FLOAT DEFAULT 0,
    net_pnl FLOAT DEFAULT 0,
    gas_estimate FLOAT DEFAULT 0,
    pnl_type TEXT DEFAULT 'trade',
    clob_buy_order_id TEXT DEFAULT '',
    clob_sell_order_id TEXT DEFAULT '',
    entry_time TIMESTAMPTZ,
    exit_time TIMESTAMPTZ,
    reconciled_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS polyedge.reconcile_state (
    id SERIAL PRIMARY KEY,
    last_cursor TEXT DEFAULT 'MA==',
    last_fill_timestamp BIGINT DEFAULT 0,
    total_fills_processed INT DEFAULT 0,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pnl_ledger_market ON polyedge.pnl_ledger(market_id);
CREATE INDEX IF NOT EXISTS idx_pnl_ledger_strategy ON polyedge.pnl_ledger(strategy);
CREATE INDEX IF NOT EXISTS idx_pnl_ledger_time ON polyedge.pnl_ledger(entry_time);

-- Micro sniper persistent price context.
-- Logs a snapshot every ~30s per symbol so the bot has cross-window and
-- cross-restart context. On startup, load the last 30 min of rows and
-- the bot immediately knows "BTC has been trending up for 15 minutes".
CREATE TABLE IF NOT EXISTS polyedge.micro_price_log (
    id SERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    price DOUBLE PRECISION NOT NULL,
    ofi_30s DOUBLE PRECISION DEFAULT 0,
    volume_30s DOUBLE PRECISION DEFAULT 0,
    trade_intensity DOUBLE PRECISION DEFAULT 0,
    logged_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_micro_price_log_symbol_time
    ON polyedge.micro_price_log(symbol, logged_at DESC);

-- Research pipeline: signal snapshots, candidate events, no-trade reasons,
-- regime tags, and attribution data. Schema version tracked per row.
CREATE TABLE IF NOT EXISTS polyedge.signal_snapshots (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    schema_version INT NOT NULL DEFAULT 1,
    session_id TEXT NOT NULL DEFAULT '',
    symbol TEXT NOT NULL,
    market_id TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL DEFAULT 'periodic',  -- periodic, candidate, trade, no_trade, threshold_cross, window_hop, gap_reset
    -- Full feature vector stored as JSONB for flexibility.
    -- Schema may evolve — version field lets backtests know which fields exist.
    features JSONB NOT NULL DEFAULT '{}',
    -- Denormalized columns for fast queries (extracted from features)
    regime TEXT DEFAULT 'unknown',
    dampened_momentum DOUBLE PRECISION DEFAULT 0,
    btc_price DOUBLE PRECISION DEFAULT 0,
    yes_price DOUBLE PRECISION DEFAULT 0,
    seconds_remaining DOUBLE PRECISION DEFAULT 0,
    trade_fired BOOLEAN DEFAULT FALSE,
    trade_action TEXT DEFAULT '',
    no_trade_reason TEXT DEFAULT 'none',
    near_threshold BOOLEAN DEFAULT FALSE,
    -- Outcome labels (filled in offline by label_outcomes command)
    btc_move_5s DOUBLE PRECISION,   -- BTC price change 5s after snapshot
    btc_move_10s DOUBLE PRECISION,
    btc_move_20s DOUBLE PRECISION,
    btc_move_30s DOUBLE PRECISION,
    token_move_5s DOUBLE PRECISION,  -- YES/NO token price change 5s after
    token_move_10s DOUBLE PRECISION,
    token_move_20s DOUBLE PRECISION,
    token_move_30s DOUBLE PRECISION,
    max_favorable DOUBLE PRECISION,  -- Best price move in our direction within 30s
    max_adverse DOUBLE PRECISION,    -- Worst price move against us within 30s
    outcome_labeled BOOLEAN DEFAULT FALSE
);

-- Indexes for common research queries
CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON polyedge.signal_snapshots(ts);
CREATE INDEX IF NOT EXISTS idx_snapshots_symbol_ts ON polyedge.signal_snapshots(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_snapshots_session ON polyedge.signal_snapshots(session_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_event ON polyedge.signal_snapshots(event_type);
CREATE INDEX IF NOT EXISTS idx_snapshots_regime ON polyedge.signal_snapshots(regime);
CREATE INDEX IF NOT EXISTS idx_snapshots_trade ON polyedge.signal_snapshots(trade_fired) WHERE trade_fired = TRUE;
CREATE INDEX IF NOT EXISTS idx_snapshots_candidate ON polyedge.signal_snapshots(near_threshold) WHERE near_threshold = TRUE;
CREATE INDEX IF NOT EXISTS idx_snapshots_no_trade ON polyedge.signal_snapshots(no_trade_reason) WHERE no_trade_reason != 'none';
CREATE INDEX IF NOT EXISTS idx_snapshots_unlabeled ON polyedge.signal_snapshots(outcome_labeled) WHERE outcome_labeled = FALSE;

CREATE TABLE IF NOT EXISTS polyedge.tuning_log (
    id SERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source VARCHAR(20) NOT NULL DEFAULT 'manual',  -- 'manual', 'scheduled', 'skill'
    key TEXT NOT NULL,                              -- e.g. 'strategies.micro_sniper.min_seconds_remaining'
    old_value TEXT,
    new_value TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',               -- why the change was made
    data_window_hours INT DEFAULT NULL,            -- how many hours of data informed the decision
    win_rate_at_change FLOAT DEFAULT NULL,         -- snapshot of key metrics at time of change
    avg_pnl_at_change FLOAT DEFAULT NULL,
    trade_count_at_change INT DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_tuning_log_ts ON polyedge.tuning_log(ts);
CREATE INDEX IF NOT EXISTS idx_tuning_log_key ON polyedge.tuning_log(key);
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
            # Migrations — safe to re-run (IF NOT EXISTS / ADD COLUMN IF NOT EXISTS)
            await conn.execute("""
                ALTER TABLE polyedge.trades
                ADD COLUMN IF NOT EXISTS config_snapshot JSONB;
            """)
            await conn.execute("""
                ALTER TABLE polyedge.trades
                ADD COLUMN IF NOT EXISTS signal_data JSONB;
            """)
            await conn.execute("""
                ALTER TABLE polyedge.trades
                ADD COLUMN IF NOT EXISTS post_exit_mfe_5m FLOAT;
            """)
            await conn.execute("""
                ALTER TABLE polyedge.trades
                ADD COLUMN IF NOT EXISTS post_exit_mae_5m FLOAT;
            """)

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
        # Serialize JSONB fields
        config_snapshot = trade.get("config_snapshot")
        signal_data = trade.get("signal_data")
        if config_snapshot and not isinstance(config_snapshot, str):
            config_snapshot = json.dumps(config_snapshot)
        if signal_data and not isinstance(signal_data, str):
            signal_data = json.dumps(signal_data)
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO polyedge.trades
                    (trade_id, market_id, token_id, question, side, entry_price,
                     size, status, strategy, reasoning, ai_probability,
                     config_snapshot, signal_data)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
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
                config_snapshot,
                signal_data,
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

    async def close_trade_by_market(
        self, market_id: str, exit_price: float, pnl: float, exit_reason: str = ""
    ):
        """Close the most recent open trade for a market by condition_id."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE polyedge.trades
                SET exit_price=$2, pnl=$3, status='CLOSED', closed_at=NOW(), exit_reason=$4
                WHERE trade_id = (
                    SELECT trade_id FROM polyedge.trades
                    WHERE market_id=$1 AND status='OPEN'
                    ORDER BY opened_at DESC LIMIT 1
                )
                """,
                market_id,
                exit_price,
                pnl,
                exit_reason,
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
                    size=EXCLUDED.size, entry_price=EXCLUDED.entry_price,
                    current_price=EXCLUDED.current_price,
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

    # --- P&L Ledger ---

    async def insert_pnl_entry(self, entry: dict):
        """Insert a reconciled P&L ledger entry."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO polyedge.pnl_ledger
                    (market_id, question, strategy, side, size,
                     entry_fill_price, exit_fill_price, gross_pnl,
                     fees_paid, net_pnl, gas_estimate, pnl_type,
                     clob_buy_order_id, clob_sell_order_id,
                     entry_time, exit_time)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
                """,
                entry["market_id"],
                entry.get("question", ""),
                entry.get("strategy", ""),
                entry["side"],
                entry["size"],
                entry["entry_fill_price"],
                entry.get("exit_fill_price"),
                entry.get("gross_pnl", 0),
                entry.get("fees_paid", 0),
                entry.get("net_pnl", 0),
                entry.get("gas_estimate", 0),
                entry.get("pnl_type", "trade"),
                entry.get("clob_buy_order_id", ""),
                entry.get("clob_sell_order_id", ""),
                entry.get("entry_time"),
                entry.get("exit_time"),
            )

    async def get_pnl_ledger(
        self,
        strategy: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Get reconciled P&L entries."""
        async with self.pool.acquire() as conn:
            if strategy:
                rows = await conn.fetch(
                    """
                    SELECT * FROM polyedge.pnl_ledger
                    WHERE strategy = $1
                    ORDER BY entry_time DESC LIMIT $2
                    """,
                    strategy,
                    limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT * FROM polyedge.pnl_ledger
                    ORDER BY entry_time DESC LIMIT $1
                    """,
                    limit,
                )
            return [dict(r) for r in rows]

    async def get_pnl_summary(self, strategy: str | None = None) -> dict:
        """Get aggregate P&L summary from the ledger."""
        async with self.pool.acquire() as conn:
            where = "WHERE strategy = $1" if strategy else ""
            params = [strategy] if strategy else []

            row = await conn.fetchrow(
                f"""
                SELECT
                    COUNT(*) as total_trades,
                    COALESCE(SUM(gross_pnl), 0) as total_gross_pnl,
                    COALESCE(SUM(fees_paid), 0) as total_fees,
                    COALESCE(SUM(net_pnl), 0) as total_net_pnl,
                    COALESCE(SUM(gas_estimate), 0) as total_gas,
                    COUNT(*) FILTER (WHERE net_pnl > 0) as wins,
                    COUNT(*) FILTER (WHERE net_pnl < 0) as losses,
                    COUNT(*) FILTER (WHERE net_pnl = 0) as breakeven,
                    COALESCE(AVG(net_pnl) FILTER (WHERE net_pnl > 0), 0) as avg_win,
                    COALESCE(AVG(net_pnl) FILTER (WHERE net_pnl < 0), 0) as avg_loss,
                    COALESCE(SUM(size * entry_fill_price), 0) as total_volume
                FROM polyedge.pnl_ledger
                {where}
                """,
                *params,
            )
            return dict(row) if row else {}

    # --- Reconcile State ---

    async def get_reconcile_state(self) -> dict:
        """Get the last reconciliation cursor."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM polyedge.reconcile_state ORDER BY id DESC LIMIT 1"
            )
            if row:
                return dict(row)
            return {
                "last_cursor": "MA==",
                "last_fill_timestamp": 0,
                "total_fills_processed": 0,
            }

    async def update_reconcile_state(
        self, last_cursor: str, last_fill_timestamp: int, total_fills: int
    ):
        """Update or insert reconciliation state."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO polyedge.reconcile_state
                    (id, last_cursor, last_fill_timestamp,
                     total_fills_processed, updated_at)
                VALUES (1, $1, $2, $3, NOW())
                ON CONFLICT (id) DO UPDATE SET
                    last_cursor = EXCLUDED.last_cursor,
                    last_fill_timestamp = EXCLUDED.last_fill_timestamp,
                    total_fills_processed = EXCLUDED.total_fills_processed,
                    updated_at = NOW()
                """,
                last_cursor,
                last_fill_timestamp,
                total_fills,
            )

    # ------------------------------------------------------------------
    # Micro price context — persistent cross-window / cross-restart state
    # ------------------------------------------------------------------

    async def log_micro_price(
        self,
        symbol: str,
        price: float,
        ofi_30s: float = 0.0,
        volume_30s: float = 0.0,
        trade_intensity: float = 0.0,
    ):
        """Log a price snapshot for the micro sniper.

        Called every ~30 seconds per symbol. Provides persistent context
        that survives window hops and restarts.
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO polyedge.micro_price_log
                    (symbol, price, ofi_30s, volume_30s, trade_intensity)
                VALUES ($1, $2, $3, $4, $5)
                """,
                symbol,
                price,
                ofi_30s,
                volume_30s,
                trade_intensity,
            )

    async def get_micro_price_context(
        self, symbol: str, minutes: int = 30
    ) -> list[dict]:
        """Load recent price snapshots for a symbol.

        Returns rows ordered oldest-first so callers can compute trends
        by comparing first vs last entries.
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT symbol, price, ofi_30s, volume_30s, trade_intensity, logged_at
                FROM polyedge.micro_price_log
                WHERE symbol = $1
                  AND logged_at > NOW() - ($2 || ' minutes')::INTERVAL
                ORDER BY logged_at ASC
                """,
                symbol,
                str(minutes),
            )
            return [dict(r) for r in rows]

    async def prune_micro_price_log(self, keep_minutes: int = 60):
        """Delete old micro price log entries to prevent unbounded growth."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                DELETE FROM polyedge.micro_price_log
                WHERE logged_at < NOW() - ($1 || ' minutes')::INTERVAL
                """,
                str(keep_minutes),
            )

    # ------------------------------------------------------------------
    # Research pipeline — signal snapshots
    # ------------------------------------------------------------------

    async def bulk_insert_snapshots(self, snapshots: list[dict]):
        """Batch insert signal snapshots for the research pipeline.

        Each snapshot dict must have keys matching the SignalSnapshot.to_dict() output.
        The full feature vector is stored as JSONB; key fields are denormalized
        into columns for fast queries.
        """
        if not snapshots:
            return
        rows = []
        for s in snapshots:
            rows.append((
                s.get("symbol", ""),
                s.get("market_id", ""),
                s.get("event_type", "periodic"),
                s.get("schema_version", 1),
                s.get("session_id", ""),
                json.dumps(s),  # full feature vector as JSONB
                s.get("regime", "unknown"),
                s.get("dampened_momentum", 0),
                s.get("btc_price", 0),
                s.get("yes_price", 0),
                s.get("seconds_remaining", 0),
                s.get("trade_fired", False),
                s.get("trade_action", ""),
                s.get("no_trade_reason", "none"),
                s.get("near_threshold", False),
            ))
        async with self.pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO polyedge.signal_snapshots
                    (symbol, market_id, event_type, schema_version, session_id,
                     features, regime, dampened_momentum, btc_price, yes_price,
                     seconds_remaining, trade_fired, trade_action, no_trade_reason,
                     near_threshold)
                VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                """,
                rows,
            )

    async def get_snapshots_for_labeling(self, limit: int = 1000) -> list[dict]:
        """Get unlabeled snapshots for offline outcome labeling."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, ts, symbol, features
                FROM polyedge.signal_snapshots
                WHERE outcome_labeled = FALSE
                ORDER BY ts ASC
                LIMIT $1
                """,
                limit,
            )
            return [dict(r) for r in rows]

    async def label_snapshot_outcomes(self, labels: list[dict]):
        """Batch update outcome labels on snapshots.

        Each label dict: {id, btc_move_5s, btc_move_10s, btc_move_20s,
                          btc_move_30s, token_move_5s, ..., max_favorable, max_adverse}
        """
        if not labels:
            return
        async with self.pool.acquire() as conn:
            for label in labels:
                await conn.execute(
                    """
                    UPDATE polyedge.signal_snapshots SET
                        btc_move_5s = $2, btc_move_10s = $3,
                        btc_move_20s = $4, btc_move_30s = $5,
                        token_move_5s = $6, token_move_10s = $7,
                        token_move_20s = $8, token_move_30s = $9,
                        max_favorable = $10, max_adverse = $11,
                        outcome_labeled = TRUE
                    WHERE id = $1
                    """,
                    label["id"],
                    label.get("btc_move_5s"), label.get("btc_move_10s"),
                    label.get("btc_move_20s"), label.get("btc_move_30s"),
                    label.get("token_move_5s"), label.get("token_move_10s"),
                    label.get("token_move_20s"), label.get("token_move_30s"),
                    label.get("max_favorable"), label.get("max_adverse"),
                )

    async def get_trades_for_post_exit_labeling(self, limit: int = 500) -> list[dict]:
        """Get closed trades that don't yet have post-exit outcome labels."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT trade_id, market_id, side, exit_price, closed_at
                FROM polyedge.trades
                WHERE status = 'CLOSED'
                  AND closed_at IS NOT NULL
                  AND post_exit_mfe_5m IS NULL
                ORDER BY closed_at DESC
                LIMIT $1
                """,
                limit,
            )
            return [dict(r) for r in rows]

    async def label_trade_post_exit(self, labels: list[dict]):
        """Batch update post-exit MFE/MAE on closed trades.

        Each label dict: {trade_id, post_exit_mfe_5m, post_exit_mae_5m}
        """
        if not labels:
            return
        async with self.pool.acquire() as conn:
            for label in labels:
                await conn.execute(
                    """
                    UPDATE polyedge.trades
                    SET post_exit_mfe_5m = $2, post_exit_mae_5m = $3
                    WHERE trade_id = $1
                    """,
                    label["trade_id"],
                    label.get("post_exit_mfe_5m"),
                    label.get("post_exit_mae_5m"),
                )

    async def get_snapshot_stats(self) -> dict:
        """Get research pipeline stats."""
        async with self.pool.acquire() as conn:
            total = await conn.fetchval(
                "SELECT COUNT(*) FROM polyedge.signal_snapshots"
            )
            labeled = await conn.fetchval(
                "SELECT COUNT(*) FROM polyedge.signal_snapshots WHERE outcome_labeled = TRUE"
            )
            trades = await conn.fetchval(
                "SELECT COUNT(*) FROM polyedge.signal_snapshots WHERE trade_fired = TRUE"
            )
            candidates = await conn.fetchval(
                "SELECT COUNT(*) FROM polyedge.signal_snapshots WHERE near_threshold = TRUE"
            )
            no_trade = await conn.fetchval(
                "SELECT COUNT(*) FROM polyedge.signal_snapshots WHERE no_trade_reason != 'none'"
            )
            regimes = await conn.fetch(
                """
                SELECT regime, COUNT(*) as count
                FROM polyedge.signal_snapshots
                GROUP BY regime
                ORDER BY count DESC
                """
            )
            return {
                "total_snapshots": total,
                "labeled": labeled,
                "unlabeled": total - labeled,
                "trades": trades,
                "candidates": candidates,
                "no_trade_blocked": no_trade,
                "regimes": {r["regime"]: r["count"] for r in regimes},
            }

    async def prune_snapshots(self, keep_hours: int = 72):
        """Delete old snapshots to prevent unbounded growth.

        Keeps trade events and candidate events longer (7 days).
        """
        async with self.pool.acquire() as conn:
            # Periodic snapshots: keep for keep_hours
            await conn.execute(
                """
                DELETE FROM polyedge.signal_snapshots
                WHERE event_type = 'periodic'
                AND ts < NOW() - ($1 || ' hours')::INTERVAL
                """,
                str(keep_hours),
            )
            # Trade and candidate events: keep for 7 days
            await conn.execute(
                """
                DELETE FROM polyedge.signal_snapshots
                WHERE event_type IN ('trade', 'candidate', 'no_trade')
                AND ts < NOW() - INTERVAL '7 days'
                """
            )

    async def log_tuning_change(
        self,
        key: str,
        new_value: str,
        reason: str,
        old_value: str | None = None,
        source: str = "manual",
        data_window_hours: int | None = None,
        win_rate: float | None = None,
        avg_pnl: float | None = None,
        trade_count: int | None = None,
    ):
        """Log a config change made during a tuning session."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO polyedge.tuning_log
                    (source, key, old_value, new_value, reason,
                     data_window_hours, win_rate_at_change, avg_pnl_at_change, trade_count_at_change)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                source,
                key,
                old_value,
                new_value,
                reason,
                data_window_hours,
                win_rate,
                avg_pnl,
                trade_count,
            )

    async def get_tuning_history(self, key: str | None = None, limit: int = 50) -> list[dict]:
        """Fetch recent tuning changes, optionally filtered by config key."""
        async with self.pool.acquire() as conn:
            if key:
                rows = await conn.fetch(
                    """
                    SELECT * FROM polyedge.tuning_log
                    WHERE key = $1
                    ORDER BY ts DESC LIMIT $2
                    """,
                    key, limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT * FROM polyedge.tuning_log
                    ORDER BY ts DESC LIMIT $1
                    """,
                    limit,
                )
            return [dict(r) for r in rows]
