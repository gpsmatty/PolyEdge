# PolyEdge — AI Context Document

This document is for AI assistants working on this codebase. It contains everything needed to understand, modify, and extend PolyEdge.

## What This Project Is

PolyEdge is an AI-powered trading bot for Polymarket prediction markets. It is a monolith Python application that scans markets, uses LLMs to estimate true probabilities, detects mispricings, sizes positions with Kelly criterion, and executes real trades via the Polymarket CLOB API. Starting bankroll is $200 USD.

## Project Root

`/Users/gpsmatty/production/PolyEdge/`

## Stack

- Python 3.11+ (currently 3.13), async throughout
- `asyncpg` for PostgreSQL
- `py-clob-client` — official Polymarket CLOB SDK (REST only, no WebSocket)
- `websockets` — for real-time market data feed
- `anthropic` SDK + `openai` SDK — LLM backends
- `pydantic` v2 + `pydantic-settings` — all models and config
- `click` — CLI framework
- `rich` — terminal UI
- `aiohttp` — async HTTP for Gamma API
- `eth-account` / `web3` — wallet management
- Package manager: `uv` (but `pip install -e .` also works)
- Tests: `pytest` + `pytest-asyncio` (run via `.venv/bin/pytest tests/ -v`)

## File Map

### Core (`src/polyedge/core/`)

| File | Purpose | Key exports |
|------|---------|-------------|
| `config.py` | All configuration. Loads from Keychain + `.env` + `config/default.yaml`. | `Settings`, `load_config()`, `AIConfig`, `RiskConfig`, `AgentConfig`, `_get_from_keychain()`, `_set_in_keychain()`, `load_keychain_secrets()`, `KEYCHAIN_KEYS` |
| `models.py` | Every data model (Pydantic). | `Market`, `OrderBook`, `OrderBookLevel`, `Signal`, `Order`, `Position`, `Trade`, `AIAnalysis`, `PortfolioSnapshot`, `Side`, `AgentMode` |
| `client.py` | Wrapper around `py-clob-client`. Handles auth, order placement, market data. | `PolyClient` |
| `db.py` | PostgreSQL storage. All tables defined in `SCHEMA_SQL` string at top of file. | `Database` (async, uses connection pool) |

### AI (`src/polyedge/ai/`)

| File | Purpose | Key exports |
|------|---------|-------------|
| `llm.py` | LLM abstraction layer. Supports Claude + OpenAI + ensemble. Has tiered model methods. Tracks cost in DB. | `LLMClient` (methods: `analyze()`, `research()`, `compute()`), `LLMResponse` |
| `analyst.py` | Market analysis prompts. Builds context, parses JSON responses. | `analyze_market()`, `quick_score_market()`, `_build_analysis_prompt()` |
| `agent.py` | The autonomous trading agent. Main loop: scan → analyze → trade. | `TradingAgent` |
| `news.py` | News context retrieval for market analysis. | `get_news_context()` |
| `probability.py` | Probability calibration tracking (stub). | — |

### Data (`src/polyedge/data/`)

| File | Purpose | Key exports |
|------|---------|-------------|
| `markets.py` | Fetches markets from Polymarket Gamma API (`https://gamma-api.polymarket.com`). Handles pagination (100 per page, up to 5000 markets). | `fetch_active_markets()`, `fetch_all_markets()`, `search_markets()` |
| `indexer.py` | Syncs all markets from API to PostgreSQL. Deactivates stale/closed markets. Tracks price history. | `MarketIndexer` |
| `orderbook.py` | Fetches raw order book from CLOB API. | `get_order_book()`, `get_prices()` |
| `book_analyzer.py` | Order book microstructure analysis. Computes imbalance, depth, whale detection, wall detection. | `analyze_book()`, `get_book_intelligence()`, `format_book_for_ai()`, `BookIntelligence` |
| `ws_feed.py` | WebSocket feed for real-time market data. Connects to `wss://ws-subscriptions-clob.polymarket.com/ws/market`. No auth needed. Must send `PING` every 10s. | `MarketFeed` |
| `signals.py` | External data sources (stub). | — |

### Strategies (`src/polyedge/strategies/`)

| File | Purpose | Key exports |
|------|---------|-------------|
| `base.py` | Strategy ABC with `evaluate(market) -> Signal`. | `Strategy` |
| `cheap_hunter.py` | Finds underpriced tail events (<$0.15). Zero AI cost. Uses liquidity/time/price heuristics. | `CheapHunterStrategy` |
| `edge_finder.py` | Converts AI analysis into trade signals when edge > threshold. | `EdgeFinderStrategy` |
| `market_maker.py` | Spread capture strategy (WIP, not fully implemented). | `MarketMakerStrategy` |

### Risk (`src/polyedge/risk/`)

| File | Purpose | Key exports |
|------|---------|-------------|
| `kelly.py` | Kelly criterion: `f* = (bp - q) / b`. Supports fractional Kelly. | `kelly_fraction()`, `fractional_kelly()`, `kelly_from_market_price()` |
| `sizing.py` | Bankroll-aware position sizing. Combines Kelly with max position limits. | `calculate_position_size()` |
| `portfolio.py` | Portfolio-level risk checks: max positions, max exposure, daily limits, drawdown circuit breaker. | `PortfolioRiskManager`, `PortfolioSnapshot` |

### Execution (`src/polyedge/execution/`)

| File | Purpose | Key exports |
|------|---------|-------------|
| `engine.py` | Places orders via CLOB API. Runs risk checks before every trade. Requires user confirmation in copilot mode. | `ExecutionEngine` |
| `tracker.py` | P&L tracking and display. | `PnLTracker` |

### CLI (`src/polyedge/cli.py`)

Click-based CLI. Commands: `setup`, `init`, `scan`, `search`, `price`, `hunt`, `edges`, `analyze`, `trade`, `positions`, `pnl`, `autopilot`, `dashboard`, `initdb`, `sync`, `costs`, `movers`, `book`, `feed`, `config`, `vault`.

## Database Schema

All tables in the `polyedge` schema (PostgreSQL). Defined in `core/db.py` as the `SCHEMA_SQL` string.

| Table | Purpose |
|-------|---------|
| `markets` | All known markets. Upserted from Gamma API. PK: `condition_id`. |
| `orders` | Order log. PK: `order_id` (UUID). |
| `trades` | Trade log with entry/exit/P&L. PK: `trade_id` (UUID). |
| `positions` | Open positions. Unique on `(market_id, token_id, side)`. |
| `ai_analyses` | Every AI analysis result with probability, confidence, reasoning, cost. |
| `portfolio_snapshots` | Point-in-time portfolio state. |
| `risk_config` | All trading config (risk, AI, agent, strategies). Key-value JSONB. Portable across environments. |
| `price_history` | Price snapshots per market per sync. Used for price mover detection. |
| `ai_cost_log` | Every AI API call with tokens, cost, purpose. Used for budget tracking. |
| `agent_memory` | Persistent agent memory: trade decisions, skip reasons, lessons. |

## Polymarket API Details

**Gamma API** (`https://gamma-api.polymarket.com`):
- `/markets` — Returns market metadata. Paginated, 100 per page, use `offset` parameter.
- `/events` — Event groups.
- No auth needed.

**CLOB API** (`https://clob.polymarket.com`):
- Order placement, cancellation, order books, prices.
- Auth via API key/secret/passphrase derived from wallet private key (EIP-712 signing).
- Accessed through `py-clob-client` SDK.

**WebSocket** (`wss://ws-subscriptions-clob.polymarket.com/ws/market`):
- Subscribe with `assets_ids` (token IDs, NOT condition IDs), `type: "market"`, `custom_feature_enabled: true`.
- Events: `book` (full snapshot), `price_change` (delta), `last_trade_price`, `best_bid_ask`, `tick_size_change`, `new_market`, `market_resolved`.
- Must send `PING` text every 10 seconds, receive `PONG`.
- Dynamic subscribe/unsubscribe with `operation` field.
- No auth needed for market channel.

**Key distinction:** `condition_id` identifies a market. `clob_token_ids` are the numeric token IDs for YES (index 0) and NO (index 1) outcomes. The CLOB and WebSocket APIs use token IDs, not condition IDs.

## Agent Scan Cycle (How It Works)

Defined in `agent.py` `_scan_cycle()`. Every 5 minutes:

1. **Housekeeping** — Review resolved positions for lessons, clean expired memories.
2. **Circuit breaker check** — Daily trade limit, daily loss limit.
3. **Load markets** — From DB via `MarketIndexer` (auto-syncs from API if stale, every 15 min).
4. **Cheap Hunter** — Run on all markets. Zero AI cost. Generates `Signal` objects.
5. **Pick AI candidates** — `_pick_ai_candidates()`: price movers first (markets that moved >3% in last hour), then highest volume. Capped at `max_markets_per_scan` (default 20).
6. **Quick score** — `quick_score_market()` via compute model (Haiku). Scores 0-100. ~$0.001 each.
7. **Deep analysis** — Top half of scored candidates get `analyze_market()` via research model (Sonnet). Includes order book context (`format_book_for_ai()`), news context, agent memory context. ~$0.003 each.
8. **Signal generation** — If `|AI_prob - market_price| > min_edge_threshold` and `confidence > min_confidence`, create a `Signal`.
9. **Execution** — Based on mode: autopilot (auto-execute), copilot (recommend + confirm), signals (display only).
10. **Memory** — Record trade decisions and skip reasons.

## Tiered AI Model System

| Tier | Config Key | Default Model | Cost | Used For |
|------|-----------|---------------|------|----------|
| Research | `ai.research_model` | `claude-sonnet-4-6` | ~$0.003/market | Deep probability estimation, news interpretation, complex reasoning |
| Compute | `ai.compute_model` | `claude-haiku-4-5-20251001` | ~$0.001/market | Quick scoring, EV calculations, pre-filtering |

Called via `llm.research(prompt, system)` and `llm.compute(prompt, system)`. Both route through `llm.analyze()` which handles budget checks and cost logging.

**Budget control:** Checked before every API call and mid-loop during scan cycles. Tracked in `ai_cost_log` table. Default limit: $5/day.

## Order Book Intelligence

`book_analyzer.py` produces a `BookIntelligence` dataclass from raw `OrderBook` data. Key metrics:

- `imbalance_ratio` — `(bid_depth - ask_depth) / total`. Range [-1, 1]. >+0.3 = buy pressure.
- `imbalance_5c` / `imbalance_10c` — Same but only for levels within 5/10 cents of best price.
- `whale_bids` / `whale_asks` — Orders >2x average size at their side.
- `bid_wall_price` / `ask_wall_price` — Levels with >5x average size (support/resistance).
- `spread_bps` — Spread in basis points.

The AI sees a ~100 token text summary via `format_book_for_ai()`, not the raw order book.

## Configuration

**Priority order** (highest wins):
1. **Database** (`risk_config` table) — All trading config. Portable across environments (Mac, VPS, App Platform). Managed via `polyedge config` CLI or `polyedge init` wizard.
2. **macOS Keychain** — Secrets only (wallet key, API keys, DB URL). Managed via `polyedge vault` CLI.
3. **Environment variables** — Explicit `export` in shell overrides Keychain.
4. **`.env` file** — Legacy fallback. Not needed if using Keychain.
5. **YAML** (`config/default.yaml`) — Defaults/fallback when DB has no value.
6. **Pydantic defaults** — Hardcoded fallbacks in `config.py`.

**DB config keys** use dot notation: `risk.kelly_fraction`, `ai.max_analysis_cost_per_day`, `agent.mode`, `strategies.cheap_hunter.enabled`, etc. All non-secret config lives in the `risk_config` table as JSONB key-value pairs.

**Config CLI:**
- `polyedge config show` — display all config from DB
- `polyedge config set <key> <value>` — change a single value
- `polyedge config save` — push current in-memory settings to DB

## Secrets / Keychain Integration

Secrets are stored in macOS Keychain under the service name `polyedge`. The `config.py` module handles this:

- `KEYCHAIN_KEYS` — List of all secret field names: `poly_private_key`, `poly_wallet_address`, `poly_api_key`, `poly_api_secret`, `poly_api_passphrase`, `database_url`, `anthropic_api_key`, `openai_api_key`, `news_api_key`.
- `_get_from_keychain(account)` — Reads a secret via `security find-generic-password`.
- `_set_in_keychain(account, value)` — Writes a secret via `security add-generic-password`.
- `load_keychain_secrets()` — Loads all known secrets from Keychain into a dict.
- `load_config()` — Injects Keychain secrets into `os.environ` before `Settings()` reads them.
- `apply_db_config(settings, db)` — Async. Overlays DB config values onto a `Settings` object. DB wins over YAML.
- `save_config_to_db(settings, db)` — Async. Writes all non-secret settings to DB for portability.
- `settings_to_db_dict(settings)` — Flattens `Settings` into namespaced key-value pairs (`risk.kelly_fraction`, etc).

CLI: `polyedge vault store|list|remove [key] [value]` — manages Keychain entries. `store` without a value prompts with hidden input.

**Important:** Never log, print, or expose secret values. The `vault list` command masks values (shows first/last 4 chars only).

## Key Design Decisions

- **Monolith** — Previous project (PredictMcap) was over-engineered microservices. Never shipped.
- **No paper trading** — Real money from day one. $200 bankroll.
- **Quarter Kelly** — Full Kelly is mathematically optimal but assumes perfect probability estimates. Quarter Kelly dramatically reduces variance.
- **Two-tier AI** — Cheap model filters before expensive model analyzes. Keeps costs under $1/hour.
- **Agent memory** — Persistent across sessions. Lessons never expire. Trade decisions expire after 30 days. Skip reasons expire after 6 hours.
- **Market indexer** — Don't hit the Gamma API every scan cycle. Sync to DB periodically, read from DB always.
- **Keychain over .env** — Secrets encrypted at rest by macOS. No plaintext keys in the repo directory.
- **Config in DB** — Trading config stored in PostgreSQL, not local YAML files. Portable across Mac, VPS, App Platform, etc.

## Testing

```bash
.venv/bin/pytest tests/ -v
```

44 tests. Pure logic tests (no DB, no API mocks needed for most). Test files:

| File | Tests |
|------|-------|
| `test_models.py` | Pydantic model validation, properties, edge cases |
| `test_kelly.py` | Kelly criterion math, fractional Kelly, market price conversion |
| `test_sizing.py` | Position sizing with bankroll limits |
| `test_cheap_hunter.py` | Cheap event detection, filtering, batch ranking |
| `test_portfolio_risk.py` | Risk checks: max positions, exposure, daily limits, circuit breaker |
| `test_book_analyzer.py` | Order book analysis: spread, imbalance, whales, walls, depth |

## Common Modification Patterns

**Adding a new strategy:**
1. Create `strategies/new_strategy.py` inheriting from `Strategy` (in `base.py`)
2. Implement `evaluate(market) -> Signal | None`
3. Add config model in `config.py`, add to `StrategiesConfig`
4. Add to `default.yaml`
5. Wire into agent's `_scan_cycle()` in `agent.py`

**Adding a new CLI command:**
1. Add function in `cli.py` with `@cli.command()` decorator
2. Use `async def _inner():` pattern with `run_async(_inner())`

**Adding a new DB table:**
1. Add CREATE TABLE to `SCHEMA_SQL` in `db.py`
2. Add index if needed
3. Add async methods on `Database` class
4. Run `polyedge initdb` to apply

**Changing AI models:**
- DB: `polyedge config set ai.research_model claude-sonnet-4-6`
- Or YAML fallback: `config/default.yaml` under `ai.research_model` and `ai.compute_model`
- Cost table: `COST_TABLE` dict in `llm.py`

**Changing risk params at runtime:**
- `polyedge config set risk.kelly_fraction 0.5`
- Takes effect on next scan cycle (no restart needed)

**Adding a new data source to AI analysis:**
1. Build the data fetcher in `data/`
2. Format as text context string
3. Pass to `analyze_market()` via `additional_context` parameter
4. Or add a new dedicated parameter to `_build_analysis_prompt()` in `analyst.py`

## Owner Preferences

- No `Co-Authored-By` in commit messages
- Handles credentials/wallet privately
- Wants live trading (strongly dislikes paper trading)
- Cost-conscious on AI API usage — always consider token cost
- Starting bankroll: $200 USDC on Polygon
- Wants MetaMask wallet for web UI access alongside the bot's hot wallet
- Prefers practical over perfect — ship it, iterate
