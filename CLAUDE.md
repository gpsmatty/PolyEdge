# PolyEdge — AI Context Document

This document is for AI assistants working on this codebase. It contains everything needed to understand, modify, and extend PolyEdge.

## What This Project Is

PolyEdge is an AI-powered trading bot for Polymarket prediction markets. It is a monolith Python application that scans markets, uses LLMs to estimate true probabilities, detects mispricings, sizes positions with Kelly criterion, and executes real trades via the Polymarket CLOB API. It includes a real-time crypto sniper that exploits latency between Binance spot prices and Polymarket's short-duration crypto markets, a weather sniper that compares ensemble weather forecasts against Polymarket weather market prices, and a high-frequency micro sniper that reads Binance aggTrade order flow to momentum-trade 5-minute crypto up/down markets. Starting bankroll is $200 USD.

## Project Root

`/Users/gpsmatty/production/PolyEdge/`

## Stack

- Python 3.11+ (currently 3.13), async throughout
- `asyncpg` for PostgreSQL
- `py-clob-client` — official Polymarket CLOB SDK (REST only, no WebSocket)
- `websockets` — for real-time market data feed (Polymarket + Binance)
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
| `config.py` | All configuration. Loads from Keychain + `.env` + `config/default.yaml`. | `Settings`, `load_config()`, `AIConfig`, `RiskConfig`, `AgentConfig`, `CryptoSniperConfig`, `WeatherSniperConfig`, `MicroSniperConfig`, `_get_from_keychain()`, `_set_in_keychain()`, `load_keychain_secrets()`, `KEYCHAIN_KEYS` |
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
| `binance_feed.py` | Binance WebSocket feed for real-time crypto prices (BTC, ETH, SOL). No auth needed. Combined streams, auto-reconnect. | `BinanceFeed`, `PriceSnapshot`, `PriceWindow` |
| `binance_aggtrade.py` | Binance aggTrade WebSocket feed — tick-level trade data with buy/sell classification (~10-50 tps for BTC). Maintains rolling flow windows (5s/15s/30s) for OFI, VWAP drift, trade intensity. Gap detection resets stale data on reconnect. | `BinanceAggTradeFeed`, `AggTrade`, `MicroStructure`, `TradeFlowWindow` |
| `weather_feed.py` | Weather forecast feed from Open-Meteo (ensemble) and NOAA (official). Caches forecasts, supports multiple locations. No API key needed. | `WeatherFeed`, `EnsembleForecast`, `NOAAForecast`, `LOCATIONS`, `find_location()` |
| `ws_feed.py` | WebSocket feed for real-time Polymarket data. Connects to `wss://ws-subscriptions-clob.polymarket.com/ws/market`. No auth needed. Must send `PING` every 10s. | `MarketFeed` |
| `signals.py` | External data sources (stub). | — |

### Strategies (`src/polyedge/strategies/`)

| File | Purpose | Key exports |
|------|---------|-------------|
| `base.py` | Strategy ABC with `evaluate(market) -> Signal`. | `Strategy` |
| `crypto_sniper.py` | All crypto market arbitrage. Handles three types: Up/Down (direction via CDF), Threshold ("above X" via log-normal), Bucket ("what price" via range CDF). Supports BTC, ETH, SOL, XRP, DOGE. | `CryptoSniperStrategy`, `CryptoMarketType`, `ParsedCryptoMarket`, `SniperOpportunity`, `find_crypto_markets()`, `match_market_to_symbol()` |
| `sniper_runner.py` | Persistent async loop connecting Binance prices to all Polymarket crypto markets. Up/down evaluated on every tick, threshold/bucket evaluated every 30s. Fetches up to 1000 markets. | `SniperRunner` |
| `weather_sniper.py` | Weather forecast arbitrage. Compares Open-Meteo ensemble probabilities to Polymarket weather market prices. Detects neg-risk on multi-bucket events. | `WeatherSniperStrategy`, `WeatherOpportunity`, `NegRiskOpportunity`, `find_weather_markets()`, `group_weather_events()` |
| `weather_runner.py` | Persistent async loop for weather sniper. Refreshes markets (5 min) and forecasts (30 min), evaluates all weather markets. | `WeatherRunner` |
| `micro_sniper.py` | High-frequency momentum strategy for 5-min crypto up/down markets. Reads Binance aggTrade order flow (OFI, VWAP drift, intensity) and trades momentum on every swing. Entry/exit/flip logic with configurable thresholds. Zero AI cost. | `MicroSniperStrategy`, `MicroAction`, `MicroOpportunity` |
| `micro_runner.py` | Persistent async loop for micro sniper. Connects Binance aggTrade + Polymarket WS. Evaluates on every 5th trade tick. Handles window hopping (pre-loads 10 windows), position tracking, sell orders. Narrows Binance feeds to only matched symbols. | `MicroRunner` |
| `cheap_hunter.py` | Finds underpriced tail events (<$0.15). Zero AI cost. **Disabled** — heuristic boosts generate false positives. | `CheapHunterStrategy` |
| `edge_finder.py` | Converts AI analysis into trade signals when edge > 10% threshold. | `EdgeFinderStrategy` |
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

Click-based CLI. Commands: `setup`, `init`, `scan`, `search`, `price`, `hunt`, `edges`, `analyze`, `trade`, `positions`, `pnl`, `autopilot`, `sniper`, `weather`, `micro`, `dashboard`, `initdb`, `sync`, `costs`, `movers`, `book`, `feed`, `config`, `vault`.

The `sniper` command runs the crypto sniper as a persistent real-time loop (separate from the 5-minute `autopilot` agent). Flags: `--auto` (auto-execute), `--dry` (watch only).

The `weather` command runs the weather sniper as a persistent polling loop. Flags: `--auto` (auto-execute), `--dry` (watch only). Default is copilot mode.

The `micro` command runs the micro sniper as a persistent real-time loop (separate from both the `sniper` and `autopilot` commands). Reads Binance aggTrade tick data for order flow momentum. Flags: `--auto` (auto-execute), `--dry` (watch only), `--market "btc 5m"` (filter by keyword), `-v` (verbose), `-q` (quiet). Default is copilot mode.

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

## Binance API Details

**WebSocket** (`wss://stream.binance.com:9443`):
- Combined streams: `/stream?streams=btcusdt@ticker/ethusdt@ticker/solusdt@ticker`
- Format: `{"stream": "btcusdt@ticker", "data": {...}}`
- Key fields in ticker data: `c` (last price), `b` (best bid), `a` (best ask), `v` (24h volume), `P` (24h change %)
- No authentication needed — fully public API
- Ping interval: 20 seconds (handled by websockets library)
- Auto-reconnect with exponential backoff (2s base, 30s max)

Used exclusively by the crypto sniper strategy as a price oracle to compare against Polymarket's short-duration crypto markets.

## Agent Scan Cycle (How It Works)

Defined in `agent.py` `_scan_cycle()`. Every 5 minutes:

1. **Housekeeping** — Review resolved positions for lessons, clean expired memories.
2. **Circuit breaker check** — Daily trade limit, daily loss limit.
3. **Load markets** — From DB via `MarketIndexer` (auto-syncs from API if stale, every 15 min).
4. **Pick AI candidates** — `_pick_ai_candidates()`: scores markets 0-9 via `_candidate_score()` based on price range (mid-range 20-80% scores highest), liquidity ($2K-$100K sweet spot), volume, and time to resolution. Filters out blacklisted categories (meme, celebrity, entertainment) via `_is_blacklisted()` and short-duration crypto markets via `_is_short_duration_crypto()` (handled by sniper). Capped at `max_markets_per_scan` (default 10).
5. **Quick score** — `quick_score_market()` via compute model (Haiku). Scores 0-100 on **mispricing potential** (not trading potential). ~$0.001 each.
6. **Deep analysis** — Top half of scored candidates get `analyze_market()` via research model (Sonnet). AI operates with **market-efficiency prior** — defaults to agreeing with market price, needs strong evidence to diverge. Includes order book context, news context (only when API key configured), agent memory context. ~$0.003 each.
7. **Signal generation** — If `|AI_prob - market_price| > 10%` (min_edge_threshold) and `confidence > 65%` (min_confidence), create a `Signal`.
8. **Execution** — Based on mode: autopilot (auto-execute), copilot (recommend + confirm), signals (display only).
9. **Memory** — Record trade decisions and skip reasons.

## Crypto Sniper Loop (How It Works)

Defined in `sniper_runner.py` `SniperRunner.run()`. Runs as a persistent async process, separate from the agent scan cycle. Handles three market types:

1. **Connect** — Opens WebSocket to Binance.US combined streams for BTC, ETH, SOL, XRP, DOGE tickers.
2. **Market refresh** — Every 60 seconds, fetches up to 1000 active markets from Polymarket Gamma API. Classifies into three types: Up/Down, Threshold ("above X"), Bucket ("what price will X hit"). Groups up/down by symbol for tick evaluation; threshold/bucket go to slow eval.
3. **Up/Down evaluation** (every price tick) — On every Binance price tick (~1/sec per symbol), evaluates up/down markets. Computes P(direction holds) via normal CDF with sqrt(time) volatility scaling. Trades when edge > 8% in last 90 seconds of window.
4. **Threshold evaluation** (every 30s) — For "above X" markets, computes P(price > strike at expiry) using log-normal model (zero-drift GBM). Uses per-symbol annualized volatility estimates.
5. **Bucket evaluation** (every 30s) — For "what price" markets, computes P(price in [low, high]) = CDF(high) - CDF(low). Handles arrow-style buckets (↑ above, ↓ below) and range buckets.
6. **Edge check** — All types: if `implied_prob - market_price > 8%` (min_edge), generate a `SniperOpportunity`.
7. **Execution** — Size with Kelly (max 5% of bankroll per snipe). In `--dry` mode, display only. In copilot, ask for confirmation. In `--auto`, execute immediately. Tracks `_traded_markets` set to prevent double-entry.
8. **Status** — Prints price/stats summary every 30 seconds showing up/down and threshold/bucket market counts separately.

## Weather Sniper Loop (How It Works)

Defined in `weather_runner.py` `WeatherRunner.run()`. Runs as a persistent async process, separate from the agent and crypto sniper:

1. **Start** — Launches three concurrent async tasks: market refresh, forecast refresh, and status display.
2. **Market refresh** (every 5 min) — Fetches weather markets from Polymarket Gamma API via `find_weather_markets()`. Filters to configured locations. Groups multi-bucket events via `group_weather_events()` (uses `groupSlug` from raw Gamma data, falls back to `groupItemTitle`/category).
3. **Forecast refresh** (every 30 min) — For each tracked weather market, fetches ensemble forecast from Open-Meteo. Parses market question to extract location, target date, and bucket range via `WeatherSniperStrategy.parse_market()`.
4. **Evaluation** — For each market with a forecast, computes forecast probability (fraction of ensemble members in bucket range) and compares to market price. If `forecast_prob - market_price > 10%` (min_edge) and ensemble confidence > 60%, generates a `WeatherOpportunity`.
5. **Neg-risk check** — For each event group (multi-bucket temperature events), checks if YES prices sum != $1.00. If sum > $1.00 by more than 3% (min_neg_risk_edge), signals sell-all arbitrage. If sum < $1.00 by same margin, signals buy-all.
6. **Execution** — Size with Kelly (max 8% of bankroll per weather trade). In `--dry` mode, display only. In copilot, ask for confirmation. In `--auto`, execute immediately.
7. **Status** — Prints tracked market count and opportunity stats every 60 seconds.

## Micro Sniper Loop (How It Works)

Defined in `micro_runner.py` `MicroRunner.run()`. Runs as a persistent async process, separate from the agent, crypto sniper, and weather sniper. Reads tick-level Binance aggTrade data to momentum-trade Polymarket's 5-minute crypto up/down markets.

1. **Start** — Loads markets from DB/API, narrows Binance feeds to only matched symbols (e.g., only `btcusdt@aggTrade` when `--market btc 5m`), connects Binance aggTrade + Polymarket WebSocket.
2. **aggTrade callback** (every 5th trade tick, ~2-10 evals/sec) — On each Binance aggTrade, updates `MicroStructure` rolling flow windows (5s/15s/30s OFI, VWAP drift, trade intensity). Evaluates only the current (first) window — rest are pre-loaded for seamless hopping.
3. **Momentum signal** — Composite score from -1 (strong sell) to +1 (strong buy): 40% OFI_5s + 30% OFI_15s + 20% VWAP drift + 10% intensity surge.
4. **Entry** — When `|momentum| > 0.40` (entry_threshold), confidence > 0.40, at least 10 trades in the 15s window, and market price between 0.15-0.80, enter a position (BUY YES if bullish, BUY NO if bearish).
5. **Exit** — When momentum reverses past exit_threshold (0.15) against our position, or momentum drops below hold_threshold (0.08) even if aligned, or force exit with <8s remaining.
6. **Flip** — When momentum reverses past flip_threshold (0.50) with confidence > 0.50 and at least 25 trades in window, close current position and open opposite side. Flips require higher thresholds because they're expensive (close + reopen).
7. **Window hopping** — Pre-loads 10 upcoming windows. When current window expires, instantly promotes next window. Refetches from API when ≤3 windows remain. Uses `asyncio.Lock` to prevent concurrent API fetches.
8. **Gap detection** — If >5 seconds pass between aggTrade ticks (WebSocket disconnect), resets all flow windows to avoid trading on stale momentum.
9. **Trade cooldown** — 10 seconds between trades on the same market to prevent whipsaw.
10. **Status** — Prints microstructure state every 15 seconds.

The `micro` CLI command runs the micro sniper: `polyedge micro --dry --market "btc 5m"`. Flags: `--auto` (auto-execute), `--dry` (watch only), `--market` (filter by keyword), `-v` (verbose), `-q` (quiet).

**Micro sniper config keys** (all under `strategies.micro_sniper.*`):
- `enabled` (bool, default true)
- `symbols` (list[str], default ["btcusdt"])
- `entry_threshold` (float, default 0.40)
- `exit_threshold` (float, default 0.15)
- `hold_threshold` (float, default 0.08)
- `flip_threshold` (float, default 0.50)
- `flip_min_confidence` (float, default 0.50)
- `min_confidence` (float, default 0.40)
- `min_trades_in_window` (int, default 10)
- `min_trades_for_flip` (int, default 25)
- `min_seconds_remaining` (float, default 15.0)
- `force_exit_seconds` (float, default 8.0)
- `min_entry_price` (float, default 0.15)
- `max_entry_price` (float, default 0.80)
- `max_position_per_trade` (float, default 0.03 = 3% of bankroll)
- `max_trades_per_window` (int, default 50)
- `min_liquidity` (float, default 500)

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
- **Market-efficiency prior** — AI analyst defaults to agreeing with market price. Needs strong specific evidence to disagree by >5%. Prevents LLM from hallucinating edges.
- **Crypto sniper** — Separate persistent loop from the 5-minute agent. Evaluates on every Binance price tick. Zero AI cost — pure math.
- **Weather sniper** — Separate polling loop. Compares free ensemble weather forecasts against Polymarket prices. Also detects neg-risk arbitrage on multi-bucket temperature events. Zero AI cost — pure data comparison.
- **Agent memory** — Persistent across sessions. Lessons never expire. Trade decisions expire after 30 days. Skip reasons expire after 6 hours.
- **Market indexer** — Don't hit the Gamma API every scan cycle. Sync to DB periodically, read from DB always.
- **Keychain over .env** — Secrets encrypted at rest by macOS. No plaintext keys in the repo directory.
- **Config in DB** — Trading config stored in PostgreSQL, not local YAML files. Portable across Mac, VPS, App Platform, etc.

## Testing

```bash
.venv/bin/pytest tests/ -v
```

120+ tests. Pure logic tests (no DB, no API mocks needed for most). Test files:

| File | Tests |
|------|-------|
| `test_models.py` | Pydantic model validation, properties, edge cases |
| `test_kelly.py` | Kelly criterion math, fractional Kelly, market price conversion |
| `test_sizing.py` | Position sizing with bankroll limits |
| `test_cheap_hunter.py` | Cheap event detection, filtering, batch ranking |
| `test_portfolio_risk.py` | Risk checks: max positions, exposure, daily limits, circuit breaker |
| `test_book_analyzer.py` | Order book analysis: spread, imbalance, whales, walls, depth |
| `test_crypto_sniper.py` | Normal CDF accuracy, market type classification (up/down, threshold, bucket), symbol extraction (BTC/ETH/SOL/XRP/DOGE), threshold probability (log-normal), bucket probability (range CDF), direction probability, regex patterns, strike extraction, arrow/range bucket parsing |
| `test_weather_sniper.py` | Ensemble probability (in-range, above, below, boundary), location matching, market parsing (bucket ranges, below/above, precipitation), weather identification, event grouping, evaluate with forecast, neg-risk detection, confidence scoring, config defaults, signal conversion |
| `test_micro_sniper.py` | Momentum entry/exit/flip logic, threshold validation, sparse data guard, min_entry_price guard, confidence filtering, force exit, config defaults, trade count requirements for flips |

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

**Adding a real-time strategy (like crypto sniper):**
1. Create `strategies/new_strategy.py` with strategy logic
2. Create `strategies/new_runner.py` with persistent async loop
3. Add config model in `config.py` (see `CryptoSniperConfig` as template), add to `StrategiesConfig`
4. Add to `default.yaml`
5. Add CLI command in `cli.py` that wires up the runner
6. This runs separately from the 5-minute agent cycle

**Crypto sniper config keys** (all under `strategies.crypto_sniper.*`):
- `enabled` (bool, default true)
- `min_edge` (float, default 0.08 = 8%)
- `min_price_move_pct` (float, default 0.002 = 0.2%)
- `max_seconds_before_entry` (float, default 90)
- `symbols` (list[str], default ["btcusdt", "ethusdt", "solusdt", "xrpusdt", "dogeusdt"])
- `max_position_per_trade` (float, default 0.05 = 5% of bankroll)
- `min_liquidity` (float, default 500)

**Weather sniper config keys** (all under `strategies.weather_sniper.*`):
- `enabled` (bool, default true)
- `min_edge` (float, default 0.10 = 10%)
- `min_confidence` (float, default 0.60 = 60% ensemble agreement)
- `min_neg_risk_edge` (float, default 0.03 = 3% for multi-bucket arbitrage)
- `max_position_per_trade` (float, default 0.08 = 8% of bankroll)
- `min_liquidity` (float, default 200)
- `forecast_interval_minutes` (int, default 30)
- `locations` (list[str], default ["nyc", "london", "seoul", "chicago", "miami"])

## Weather API Details

**Open-Meteo Ensemble API** (`https://ensemble-api.open-meteo.com/v1/ensemble`):
- Free, no API key needed. 10,000 calls/day on free tier.
- GFS ensemble forecasts with multiple model runs (up to 50 ensemble members).
- Query params: `latitude`, `longitude`, `daily` (e.g., `temperature_2m_max`), `models=gfs_seamless`, `forecast_days`.
- Response includes `temperature_2m_max_member01`, `_member02`, etc. — each member is one model run.
- Natural probability distribution: count members in range / total members = probability estimate.

**NOAA API** (`https://api.weather.gov`):
- Free, no API key needed. Requires `User-Agent` header.
- Two-step process: `/points/{lat},{lon}` → get grid coordinates → `/gridpoints/{office}/{gridX},{gridY}/forecast`.
- Official US government data that directly matches Polymarket resolution source for US weather markets.
- Used as cross-reference for US locations alongside Open-Meteo ensemble data.

## Owner Preferences

- No `Co-Authored-By` in commit messages
- Handles credentials/wallet privately
- Wants live trading (strongly dislikes paper trading)
- Cost-conscious on AI API usage — always consider token cost
- Starting bankroll: $200 USDC on Polygon
- Wants MetaMask wallet for web UI access alongside the bot's hot wallet
- Prefers practical over perfect — ship it, iterate
